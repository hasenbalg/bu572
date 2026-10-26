"""Golden-vector tests for BP Measurement (0x2A35) parsing.

Vectors are hand-built from the Bluetooth SIG Blood Pressure Service
layout (see records.py docstring); values chosen to be unambiguous.
"""
import datetime as dt

import pytest

from bpbridge.records import RecordError, parse_bp_measurement, parse_intermediate_cuff

# flags=0x0E (ts|pulse|user), mmHg
# sys 120, dia 80, MAP 93, 2026-10-26 12:34:56, pulse 72, user_raw 0
V_STD = bytes.fromhex("0e" "7800" "5000" "5d00" "ea070a1a0c2238" "4800" "00")

# flags=0x0F: same fields but the device sent kPa
# sys 16.0 kPa (~120.01 mmHg), dia 10.7 (~80.26), MAP 12.4 (~93.01)
# (mantissa 107/124, exponent -1), same timestamp, pulse 72, user_raw 1
V_KPA = bytes.fromhex("0f" "1000" "6bf0" "7cf0" "ea070a1a0c2238" "4800" "01")

# flags=0x1E: also UINT16 status = 0x0003 (body movement | cuff too loose)
V_STATUS = bytes.fromhex("1e") + V_STD[1:-1] + bytes.fromhex("00") + bytes.fromhex("0300")


def test_std_record():
    r = parse_bp_measurement(V_STD)
    assert r.systolic == 120.0
    assert r.diastolic == 80.0
    assert r.mean == 93.0
    assert r.pulse == 72.0
    assert r.timestamp == dt.datetime(2026, 10, 26, 12, 34, 56)
    assert r.user == 1 and r.user_raw == 0
    assert not r.guest
    assert r.unit == "mmHg"
    assert r.status is None
    assert r.raw == V_STD


def test_kpa_conversion():
    r = parse_bp_measurement(V_KPA)
    assert r.systolic == pytest.approx(120.01, abs=0.1)
    assert r.diastolic == pytest.approx(80.26, abs=0.1)
    assert r.mean == pytest.approx(93.01, abs=0.1)
    assert r.pulse == 72.0            # pulse untouched by the unit flag
    assert r.user == 2 and r.user_raw == 1
    assert r.unit == "mmHg"           # normalised


def test_status_uint16():
    r = parse_bp_measurement(V_STATUS)
    assert r.status == 0x0003
    assert r.status_flags == ["body_movement", "cuff_too_loose"]
    assert r.user == 1


def test_status_one_byte_lenient():
    # firmware variant: status present but only one byte on the wire
    data = bytes.fromhex("1e") + V_STD[1:-1] + bytes.fromhex("00") + bytes.fromhex("04")
    r = parse_bp_measurement(data)
    assert r.status == 0x04
    assert r.status_flags == ["speech_detected"]


def test_flags_none_only_core():
    # flags=0x00: core triple only
    data = bytes.fromhex("00") + bytes.fromhex("7800" "5000" "5d00")
    r = parse_bp_measurement(data)
    assert (r.systolic, r.diastolic, r.mean) == (120.0, 80.0, 93.0)
    assert r.timestamp is None and r.pulse is None and r.user is None


def test_guest_user_id():
    data = V_STD[:-1] + bytes.fromhex("ff")
    r = parse_bp_measurement(data)
    assert r.user is None and r.user_raw == 0xFF
    assert r.guest


def test_broken_date_keeps_values():
    data = bytearray(V_STD)
    data[9] = 13                      # month = 13 (offset: flags+3 sfloats)
    r = parse_bp_measurement(bytes(data))
    assert r.timestamp is None
    assert r.systolic == 120.0        # values still usable


def test_truncations_raise():
    with pytest.raises(RecordError):
        parse_bp_measurement(b"\x00\x78\x00")
    with pytest.raises(RecordError):
        parse_bp_measurement(bytes.fromhex("02") + bytes.fromhex("78005000" "5d00")
                             + b"\xea\x07\x0a")      # ts truncated
    with pytest.raises(RecordError):
        parse_bp_measurement(bytes.fromhex("1e") + bytes.fromhex("78005000" "5d00")
                             + b"\xea\x07\x0a\x1a\x0c\x22\x38"
                             + b"\x48\x00")          # user flag, no byte


def test_intermediate_cuff():
    # flags 0, cuff pressure 140 in the "systolic" SFLOAT slot
    info, cuff = parse_intermediate_cuff(bytes.fromhex("00") + bytes.fromhex("8c00"))
    assert cuff == 140.0 and info["unit"] == "mmHg"
    # kPa variant: 18.0 kPa
    _, cuff = parse_intermediate_cuff(bytes.fromhex("01") + bytes.fromhex("1200"))
    assert cuff == pytest.approx(135.01, abs=0.1)


def test_csv_and_dict_export():
    import csv
    import io

    r = parse_bp_measurement(V_STD)
    d = r.to_dict()
    assert d["timestamp"] == "2026-10-26T12:34:56"
    assert d["raw"] == V_STD.hex()
    row = r.to_csv_row()
    assert row[0] == "2026-10-26T12:34:56"
    assert row[1] == 1
    buf = io.StringIO()
    w = csv.writer(buf)
    r.write_csv_header(w)
    w.writerow(r.to_csv_row())
    lines = buf.getvalue().strip().splitlines()
    assert lines[0].split(",")[:7] == ["timestamp", "user", "guest", "systolic",
                                       "diastolic", "mean", "pulse"]
    assert len(lines[1].split(",")) == len(lines[0].split(","))
