"""IEEE 11073-20601 / Bluetooth "medfloat16" (SFLOAT16) decoding.

Layout: 4-bit signed decimal exponent in bits 12-15, 12-bit signed
two's-complement mantissa in bits 0-11; value = mantissa * 10**exponent.
Special values occupy the range raw & 0xFF80 == 0x0780 .. 0x07FF.
"""
from __future__ import annotations

import math

_NAN = 0x07FF
_POS_NRES = 0x07FE
_NEG_NRES = 0x07FD
_POS_INF = 0x07FC
_NEG_INF = 0x07FB
_RESERVED_LO = 0x07F8


class SFloatError(ValueError):
    pass


def decode_sfloat16(raw: int) -> float:
    """Decode a 16-bit SFLOAT into a float.

    Raises SFloatError for NaN/NRes/reserved codes; returns +/-inf for the
    infinity codes.
    """
    if not 0 <= raw <= 0xFFFF:
        raise SFloatError(f"raw out of range: {raw:#06x}")

    # Special values occupy exactly the raw range 0x07F8..0x07FF
    # (exponent 0, top mantissa bits set — IEEE 1107-20601).
    if _RESERVED_LO <= raw <= _NAN:
        if raw == _POS_INF:
            return math.inf
        if raw == _NEG_INF:
            return -math.inf
        raise SFloatError(f"SFLOAT special code {raw:#06x}")

    exp = (raw >> 12) & 0xF
    if exp >= 8:
        exp -= 16
    man = raw & 0xFFF
    if man >= 0x800:
        man -= 0x1000
    return man * (10.0 ** exp)


def encode_sfloat16(value: float, exponent: int | None = None) -> int:
    """Encode a float as SFLOAT16 (used by tests to build golden vectors)."""
    if exponent is None:
        for e in range(7, -9, -1):
            m = round(value * (10 ** -e))
            if -0x800 <= m <= 0x7FF and abs(m * 10.0 ** e - value) < 1e-9 * max(
                1.0, abs(value)
            ):
                exponent = e
                break
        else:
            raise SFloatError(f"cannot encode {value!r} as SFLOAT16")
    m = round(value * (10 ** -exponent))
    if not -0x800 <= m <= 0x7FF:
        raise SFloatError(f"mantissa out of range for {value!r}")
    if not -8 <= exponent <= 7:
        raise SFloatError("exponent out of range")
    return ((exponent & 0xF) << 12) | (m & 0xFFF)
