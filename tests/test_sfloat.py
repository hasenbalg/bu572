"""Golden-vector tests for SFLOAT16."""
import math

import pytest

from bpbridge.sfloat import SFloatError, decode_sfloat16, encode_sfloat16


def test_decode_plain():
    # 120 with exponent 0
    assert decode_sfloat16(encode_sfloat16(120.0)) == 120.0
    # exponent -1: 1.2
    assert decode_sfloat16(0xF00C) == pytest.approx(1.2)
    # negative mantissa
    assert decode_sfloat16(encode_sfloat16(-3.5, -1)) == -3.5


def test_decode_kpa_style():
    # 16.0 kPa = mantissa 16 exponent 0
    assert decode_sfloat16(0x0010) == 16.0
    # exponent +1: 120 as mantissa 12 exp 1
    assert decode_sfloat16(encode_sfloat16(120.0, 1)) == 120.0


def test_specials():
    assert decode_sfloat16(0x07FC) == math.inf
    assert decode_sfloat16(0x07FB) == -math.inf
    for raw in (0x07FF, 0x07FE, 0x07FD, 0x07F8):
        with pytest.raises(SFloatError):
            decode_sfloat16(raw)


def test_roundtrip_range():
    for v in (0.0, 1.0, 99.0, 250.0, 0.5, -12.0, 1e-3):
        assert abs(decode_sfloat16(encode_sfloat16(v)) - v) < 1e-9


def test_out_of_range():
    with pytest.raises(SFloatError):
        decode_sfloat16(0x1_0000)
