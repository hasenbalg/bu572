"""Beurer BM 59 NUS tunnel: parse goldens from the published capture and
the full session flow (handshake, confirms, dedupe, errors) over a fake
ATT transport — no radio needed."""
import datetime as dt
import struct

import pytest

from bpbridge.backends.beurer import (
    ATT_ERROR_RSP,
    ATT_HANDLE_VALUE_CNF,
    ATT_HANDLE_VALUE_IND,
    ATT_WRITE_CMD,
    ATT_WRITE_REQ,
    ATT_WRITE_RSP,
    HANDLE_NUS_CCCD,
    HANDLE_NUS_RX,
    HANDLE_NUS_TX,
    BeurerError,
    sync_memory,
)
from bpbridge.models import KPA_TO_MMHG
from bpbridge.records import (
    RecordError,
    build_current_time_packet,
    parse_beurer_nus_record,
)

# golden vectors from the ipanalytics/Beurer-BM59-Unlocked capture
SPEC_RECORD = bytes.fromhex(
    "16 80 00 52 00 4a 00 e8 07 01 0f 09 0c 21 57 00 00 00".replace(" ", ""))
SPEC_TIME = bytes.fromhex("e8 07 01 0f 09 0c 21 01 00 00".replace(" ", ""))
WHEN = dt.datetime(2024, 1, 15, 9, 12, 33)          # a Monday


def _ind(payload: bytes, handle: int = HANDLE_NUS_TX) -> bytes:
    return bytes([ATT_HANDLE_VALUE_IND]) + struct.pack("<H", handle) + payload


class FakeTransport:
    """Scripted recv (None = timeout); Exceptions in the script are raised."""

    def __init__(self, script):
        self.script = list(script)
        self.sent = []
        self.closed = False

    def send(self, pdu):
        self.sent.append(pdu)

    def recv(self, timeout):
        if not self.script:
            return None
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed = True


# ------------------------------------------------------------------ packets
def test_build_time_packet_golden():
    assert build_current_time_packet(WHEN) == SPEC_TIME
    assert build_current_time_packet(WHEN, adjust_reason=1) == SPEC_TIME[:-1] + b"\x01"


def test_parse_spec_record():
    r = parse_beurer_nus_record(SPEC_RECORD)
    assert (r.systolic, r.diastolic) == (128.0, 82.0)
    assert r.pulse == 74.0            # device puts pulse in the MAP slot
    assert r.mean is None             # and no MAP at all
    assert r.timestamp == dt.datetime(2024, 1, 15, 9, 12, 33)
    assert r.status == 0x57
    assert r.status_flags == ["body_movement", "cuff_too_loose",
                              "irregular_heartbeat", "speech_detected"]
    assert r.user == 1 and r.user_raw == 0
    assert r.unit == "mmHg"


def test_parse_user_two():
    r = parse_beurer_nus_record(SPEC_RECORD[:16] + bytes.fromhex("0100"))
    assert r.user == 2 and r.user_raw == 1


def test_parse_kpa_normalised():
    data = bytes([0x17]) + SPEC_RECORD[1:]          # kPa flag set
    r = parse_beurer_nus_record(data)
    assert r.unit == "mmHg"                          # normalised
    assert r.systolic == round(128 * KPA_TO_MMHG, 1)


def test_parse_too_short():
    with pytest.raises(RecordError):
        parse_beurer_nus_record(bytes(10))


# ------------------------------------------------------------------ session
def test_session_flow_and_handshake():
    rec2 = SPEC_RECORD[:5] + bytes.fromhex("4b00") + SPEC_RECORD[7:]
    tr = FakeTransport([
        bytes([ATT_WRITE_RSP]),          # CCCD ack
        _ind(SPEC_RECORD),
        _ind(rec2),
    ])
    readings = sync_memory("AA:BB:CC:DD:EE:FF", WHEN, transport=tr,
                           idle_timeout=0.01)

    assert [ (r.systolic, r.pulse) for r in readings ] == [(128.0, 74.0),
                                                           (128.0, 75.0)]
    assert all(r.device == "AA:BB:CC:DD:EE:FF" for r in readings)

    # handshake order and opcodes matter: CCCD = Write Request ...
    assert tr.sent[0] == struct.pack("<BHH", ATT_WRITE_REQ,
                                     HANDLE_NUS_CCCD, 0x0002)
    # ... time packet = Write Command with the 10-byte Current Time struct
    assert (tr.sent[1] == bytes([ATT_WRITE_CMD])
            + struct.pack("<H", HANDLE_NUS_RX) + SPEC_TIME)
    # every indication confirmed, before any parsing work
    assert tr.sent[2] == bytes([ATT_HANDLE_VALUE_CNF])
    assert tr.sent[3] == bytes([ATT_HANDLE_VALUE_CNF])
    assert tr.closed


def test_session_dedupes_identical_records():
    tr = FakeTransport([
        bytes([ATT_WRITE_RSP]),
        _ind(SPEC_RECORD),
        _ind(SPEC_RECORD),
    ])
    readings = sync_memory("AA:BB:CC:DD:EE:FF", WHEN, transport=tr,
                           idle_timeout=0.01)
    assert len(readings) == 1


def test_session_cccd_rejected():
    tr = FakeTransport([
        bytes([ATT_ERROR_RSP, ATT_WRITE_REQ, HANDLE_NUS_CCCD, 0x06]),
    ])
    with pytest.raises(BeurerError):
        sync_memory("AA:BB:CC:DD:EE:FF", WHEN, transport=tr,
                    idle_timeout=0.01)
    assert tr.closed                       # transport closed on error too


def test_session_midstream_error():
    tr = FakeTransport([
        bytes([ATT_WRITE_RSP]),
        bytes([ATT_ERROR_RSP, ATT_WRITE_REQ, 0x00, 0x0a]),
    ])
    with pytest.raises(BeurerError):
        sync_memory("AA:BB:CC:DD:EE:FF", WHEN, transport=tr,
                    idle_timeout=0.01)


def test_session_link_closed_by_monitor_is_clean():
    """Device hangs up after streaming — collected records still returned."""
    tr = FakeTransport([
        bytes([ATT_WRITE_RSP]),
        _ind(SPEC_RECORD),
        ConnectionError("link closed: Connection reset by peer"),
    ])
    readings = sync_memory("AA:BB:CC:DD:EE:FF", WHEN, transport=tr,
                           idle_timeout=0.01)
    assert len(readings) == 1
    assert tr.closed


def test_session_silently_broken_record_skipped():
    tr = FakeTransport([
        bytes([ATT_WRITE_RSP]),
        _ind(b"\x16\x01"),                 # unparseable garbage
        _ind(SPEC_RECORD),
    ])
    readings = sync_memory("AA:BB:CC:DD:EE:FF", WHEN, transport=tr,
                           idle_timeout=0.01)
    assert len(readings) == 1
    # garbage was still confirmed (watchdog doesn't care about contents)
    assert tr.sent.count(bytes([ATT_HANDLE_VALUE_CNF])) == 2
