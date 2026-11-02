"""Record parsing for the standard Blood Pressure Service (0x1810).

BP Measurement characteristic (0x2A35) layout, per the Bluetooth SIG
Blood Pressure Service specification:

  flags        uint8   bit0 unit (0=mmHg, 1=kPa)
                       bit1 timestamp present
                       bit2 pulse rate present
                       bit3 user id present
                       bit4 measurement status present
  systolic     SFLOAT  LE
  diastolic    SFLOAT  LE
  mean arterial SFLOAT LE
  [timestamp]  7 B     Date Time: uint16 LE year, month, day, hour, min, sec
  [pulse]      SFLOAT  LE
  [user id]    uint8   0xFF = not available (the BU 572 uses this for guest)
  [status]     uint16  LE  (lenient parsers must also accept 1 byte)

Everything except the first 7 bytes is optional per the flags byte.
"""
from __future__ import annotations

import datetime as dt
import struct

from .models import KPA_TO_MMHG, BpReading, status_flags
from .sfloat import SFloatError, decode_sfloat16


class RecordError(ValueError):
    pass


def _sfloat(data: bytes, pos: int) -> float:
    if pos + 2 > len(data):
        raise RecordError(f"truncated SFLOAT at offset {pos}")
    try:
        return decode_sfloat16(struct.unpack_from("<H", data, pos)[0])
    except SFloatError as e:
        raise RecordError(f"bad SFLOAT at offset {pos}: {e}") from None


def parse_bp_measurement(data: bytes) -> BpReading:
    """Parse a BP Measurement (0x2A35) notification into a BpReading.

    Raises RecordError on structurally invalid input.  Field values are
    converted to mmHg when the device sends kPa (flag bit 0).
    """
    if len(data) < 7:
        raise RecordError(f"record too short: {len(data)} bytes")
    flags = data[0]
    reading = BpReading(raw=bytes(data))

    reading.systolic = _sfloat(data, 1)
    reading.diastolic = _sfloat(data, 3)
    reading.mean = _sfloat(data, 5)
    pos = 7

    if flags & 0x01:                      # unit flag: values are kPa
        reading.unit = "kPa"
    else:
        reading.unit = "mmHg"

    if flags & 0x02:                      # timestamp
        if pos + 7 > len(data):
            raise RecordError("timestamp flag set but record truncated")
        year, month, day, hour, minute, second = struct.unpack_from("<H5B", data, pos)
        pos += 7
        try:
            reading.timestamp = dt.datetime(year, month, day, hour, minute, second)
        except ValueError:
            reading.timestamp = None      # keep the values, drop the broken date

    if flags & 0x04:                      # pulse rate
        reading.pulse = _sfloat(data, pos)
        pos += 2

    if flags & 0x08:                      # user id
        if pos + 1 > len(data):
            raise RecordError("user id flag set but record truncated")
        reading.user_raw = data[pos]
        pos += 1
        if reading.user_raw in (0, 1):
            reading.user = reading.user_raw + 1

    if flags & 0x10:                      # measurement status
        # spec says UINT16; some firmwares send a single byte — accept both.
        if pos + 2 <= len(data):
            reading.status = struct.unpack_from("<H", data, pos)[0]
            pos += 2
        elif pos + 1 == len(data):
            reading.status = data[pos]
            pos += 1
        else:
            raise RecordError("status flag set but record truncated")
    reading.status_flags = status_flags(reading.status)

    # normalise pressure values to mmHg
    if reading.unit == "kPa":
        reading.systolic = round(reading.systolic * KPA_TO_MMHG, 1)
        reading.diastolic = round(reading.diastolic * KPA_TO_MMHG, 1)
        if reading.mean is not None:
            reading.mean = round(reading.mean * KPA_TO_MMHG, 1)
        # the pulse field is BPM and unaffected by the pressure unit flag
        reading.unit = "mmHg"

    # unknown trailing bytes (pos < len(data)) stay on `raw` — lenient, the
    # record itself already parsed cleanly.
    return reading


def parse_intermediate_cuff(data: bytes) -> tuple[dict, float]:
    """Parse an Intermediate Cuff Pressure (0x2A36) notification.

    Returns (flags_dict, cuff_pressure_mmHg).  Layout: flags uint8, then
    SFLOAT "systolic" — which carries the *current cuff pressure* while the
    cuff inflates; the diastolic field is unused during measurement.
    """
    if len(data) < 3:
        raise RecordError(f"intermediate record too short: {len(data)} bytes")
    flags = data[0]
    cuff = _sfloat(data, 1)
    if flags & 0x01:
        cuff = cuff * KPA_TO_MMHG
    return {"unit": "kPa" if flags & 0x01 else "mmHg"}, cuff


# --------------------------------------------------------- Beurer BM 59 tunnel
#
# The BM 59's "Sync/Memory" mode tunnels SIG structures through a Nordic
# UART service.  Packets are NOT the plain 0x2A35 wire format: the device
# puts raw uint16 (not SFLOAT) values and a *pulse* in the MAP slot, and
# appends a uint16 user index.  Layout (fixed 18 bytes, verified against
# the ipanalytics/Beurer-BM59-Unlocked capture):
#
#   0     flags u8      (0x16 observed: mmHg + timestamp + pulse + status)
#   1..2  systolic  u16 LE
#   3..4  diastolic u16 LE
#   5..6  pulse     u16 LE   <- NOT mean arterial pressure
#   7..13 Date Time 7 B (u16 LE year + month/day/hour/min/sec, device-local)
#   14..15 status    u16 LE  (SIG Measurement Status bits)
#   16..17 user index u16 LE (0-based; 0 -> user 1, 1 -> user 2)
def parse_beurer_nus_record(data: bytes) -> BpReading:
    """Parse one Beurer NUS-tunnel memory record into a BpReading.

    ``mean`` stays None: this device does not transmit MAP (the MAP slot
    carries pulse).
    """
    if len(data) < 18:
        raise RecordError(f"Beurer NUS record too short: {len(data)} bytes")
    flags = data[0]
    reading = BpReading(raw=bytes(data))
    sys_v, dia_v, pulse_v = struct.unpack_from("<3H", data, 1)
    reading.pulse = float(pulse_v)
    pos = 7
    year, month, day, hour, minute, second = struct.unpack_from("<H5B", data, pos)
    pos += 7
    try:
        reading.timestamp = dt.datetime(year, month, day, hour, minute, second)
    except ValueError:
        reading.timestamp = None      # keep the values, drop the broken date
    reading.status = struct.unpack_from("<H", data, pos)[0]
    pos += 2
    reading.user_raw = struct.unpack_from("<H", data, pos)[0]
    if reading.user_raw in (0, 1):
        reading.user = reading.user_raw + 1
    reading.status_flags = status_flags(reading.status)

    reading.unit = "kPa" if flags & 0x01 else "mmHg"
    reading.systolic, reading.diastolic = float(sys_v), float(dia_v)
    if reading.unit == "kPa":
        reading.systolic = round(reading.systolic * KPA_TO_MMHG, 1)
        reading.diastolic = round(reading.diastolic * KPA_TO_MMHG, 1)
        reading.unit = "mmHg"
    return reading


def build_current_time_packet(when: dt.datetime, adjust_reason: int = 0) -> bytes:
    """10-byte Current Time (0x2A2B) struct: Date Time + Day of Week +
    Fractions256 + Adjust Reason.  Naive datetime = device-local wall time."""
    return struct.pack("<H8B", when.year, when.month, when.day,
                       when.hour, when.minute, when.second,
                       when.isoweekday(), 0, adjust_reason)
