"""Data model for blood pressure readings."""
from __future__ import annotations

import csv
import datetime as dt
from dataclasses import asdict, dataclass, field
from typing import Optional

KPA_TO_MMHG = 7.50061683

# Measurement Status (UINT16, values shared with the 0x2A39 characteristic).
STATUS_BITS = [
    "body_movement",        # bit 0
    "cuff_too_loose",       # bit 1
    "speech_detected",      # bit 2
]


def status_flags(value: Optional[int]) -> list[str]:
    if value is None:
        return []
    return [name for i, name in enumerate(STATUS_BITS) if value & (1 << i)]


@dataclass
class BpReading:
    """One blood pressure measurement record.

    Values are normalised to mmHg (the record's kPa flag is applied at parse
    time).  ``timestamp`` is the meter's wall clock at measurement time,
    stored naive — the device has no timezone concept.
    """

    systolic: Optional[float] = None
    diastolic: Optional[float] = None
    mean: Optional[float] = None            # MAP, as delivered by the device
    pulse: Optional[float] = None
    timestamp: Optional[dt.datetime] = None
    user: Optional[int] = None              # 1 or 2; None for guest/unknown
    user_raw: Optional[int] = None          # raw User ID byte (guest = 0xFF)
    status: Optional[int] = None            # raw UINT16 measurement status
    status_flags: list = field(default_factory=list)
    unit: str = "mmHg"
    device: Optional[str] = None            # address or name of the source
    raw: Optional[bytes] = None             # notification bytes, for RE/debug

    @property
    def guest(self) -> bool:
        return self.user_raw is not None and self.user not in (1, 2)

    # ---------------------------------------------------------------- export
    def dedupe_key(self) -> tuple:
        return (self.timestamp, self.systolic, self.diastolic, self.mean,
                self.pulse, self.user_raw, self.status)

    def to_dict(self) -> dict:
        d = asdict(self)
        raw = d.pop("raw", None)
        if raw is not None:
            d["raw"] = bytes(raw).hex()
        if self.timestamp is not None:
            d["timestamp"] = self.timestamp.isoformat()
        return d

    _CSV_FIELDS = [
        "timestamp", "user", "guest", "systolic", "diastolic", "mean",
        "pulse", "unit", "status", "status_flags", "device",
    ]

    def write_csv_header(self, w: csv.writer) -> None:
        w.writerow(self._CSV_FIELDS)

    def to_csv_row(self) -> list:
        d = self.to_dict()
        row = []
        for k in self._CSV_FIELDS:
            v = d.get(k)
            if isinstance(v, list):
                v = "|".join(map(str, v))
            row.append("" if v is None else v)
        return row

    def __str__(self) -> str:
        ts = self.timestamp.strftime("%Y-%m-%d %H:%M") if self.timestamp else "?"
        who = f"user {self.user}" if self.user else ("guest" if self.guest else "user ?")
        pulse = f" pulse {self.pulse:g}" if self.pulse is not None else ""
        flags = f" [{','.join(self.status_flags)}]" if self.status_flags else ""
        return (f"{ts}  {who}  {self.systolic:g}/{self.diastolic:g} mmHg"
                f" (MAP {self.mean:g}){pulse}{flags}")
