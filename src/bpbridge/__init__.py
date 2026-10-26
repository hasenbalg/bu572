"""bpbridge — read blood pressure monitors over Bluetooth LE.

No vendor cloud, no vendor app.  Standard Bluetooth Blood Pressure Service
backend, live-verified with the Medisana BU 572 connect (TMB-2276-B).
"""
from .backends.bps import BpMeter
from .client import Advertisement, BpBridge, classify, scan
from .models import BpReading
from .pairing import pair, unpair
from .records import RecordError, parse_bp_measurement, parse_intermediate_cuff
from .sfloat import SFloatError, decode_sfloat16, encode_sfloat16

__version__ = "0.1.0"

__all__ = [
    "Advertisement",
    "BpBridge",
    "BpMeter",
    "BpReading",
    "RecordError",
    "SFloatError",
    "classify",
    "decode_sfloat16",
    "encode_sfloat16",
    "pair",
    "parse_bp_measurement",
    "parse_intermediate_cuff",
    "scan",
    "unpair",
    "__version__",
]
