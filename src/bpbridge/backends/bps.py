"""Backend for meters implementing the standard Bluetooth Blood Pressure
Service (0x1810): Medisana BU 572 connect and other BLP-certified monitors.

Transfer model (unlike glucose meters there is no RACP here): after a
connection is opened and the BP Measurement characteristic is subscribed,
the device PUSHES its stored records as indications, one per record.  The
transfer ends silently — we close the session after a quiet period with no
further packets.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import struct
import time
from typing import AsyncIterator, Callable, Optional

from bleak import BleakClient
from bleak.backends.device import BLEDevice

from .. import gatt
from ..models import BpReading
from ..records import RecordError, parse_bp_measurement, parse_intermediate_cuff

log = logging.getLogger("bpbridge.bps")

# the BU 572 ends its push silently; prior art uses 1.5 s quiet / 25 s cap
DEFAULT_QUIET_SECONDS = 1.5
DEFAULT_MAX_SESSION_SECONDS = 25.0


class BpMeter:
    """One connection to a Blood Pressure Service meter."""

    def __init__(self, target: "str | BLEDevice", name: Optional[str] = None):
        self.target = target
        self.name = name or (target if isinstance(target, str)
                             else getattr(target, "name", None))
        self._client: Optional[BleakClient] = None
        self._readings: dict[tuple, BpReading] = {}    # dedupe_key -> reading
        self._last_pkt: float = 0.0
        self._event = asyncio.Event()
        self._live_cb: Optional[Callable[[BpReading], None]] = None
        self._intermediate_cb: Optional[Callable[[dict, float], None]] = None
        self._errors: list[str] = []

    # ------------------------------------------------------------- lifecycle
    async def connect(self) -> None:
        if self._client is None:
            self._client = BleakClient(self.target)
            await self._client.connect()
        log.info("connected to %s", self.name)

    async def disconnect(self) -> None:
        if self._client:
            await self._client.disconnect()
            self._client = None

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *exc):
        await self.disconnect()

    @property
    def connected(self) -> bool:
        return bool(self._client and self._client.is_connected)

    # ------------------------------------------------------------- internals
    @staticmethod
    def _norm_uuid(u: str) -> str:
        h = u.replace("-", "").lower()
        if len(h) == 4:
            h = "0000" + h + "00001000800000805f9b34fb"
        return h

    def _char(self, uuid: str):
        """Resolve a characteristic, preferring the Blood Pressure Service.

        Guards against meters exposing duplicate characteristic UUIDs across
        services (bleak rejects bare-UUID lookups then).
        """
        c = self._require()
        want = self._norm_uuid(uuid)
        want_svc = self._norm_uuid(gatt.BP_SERVICE)
        fallback = None
        for svc in c.services:
            for ch in svc.characteristics:
                if self._norm_uuid(ch.uuid) != want:
                    continue
                if self._norm_uuid(svc.uuid) == want_svc:
                    return ch
                fallback = fallback or ch
        if fallback is not None:
            return fallback
        raise KeyError(uuid)

    def _has_char(self, uuid: str) -> bool:
        try:
            self._char(uuid)
            return True
        except KeyError:
            return False

    def _require(self):
        if not self.connected:
            raise RuntimeError("not connected — call connect() first")
        return self._client

    def _on_measurement(self, _handle: int, data: bytearray) -> None:
        try:
            r = parse_bp_measurement(bytes(data))
        except RecordError as e:
            log.warning("bad BP record: %s (%s)", e, bytes(data).hex())
            self._errors.append(str(e))
            return
        r.device = self.name
        self._readings.setdefault(r.dedupe_key(), r)
        self._last_pkt = time.monotonic()
        self._event.set()
        if self._live_cb:
            self._live_cb(r)

    def _on_intermediate(self, _handle: int, data: bytearray) -> None:
        try:
            info, cuff = parse_intermediate_cuff(bytes(data))
        except RecordError as e:
            log.debug("bad intermediate record: %s (%s)", e, bytes(data).hex())
            return
        if self._intermediate_cb:
            self._intermediate_cb(info, cuff)

    async def _subscribe(self, intermediate: bool = False):
        c = self._require()
        await c.start_notify(self._char(gatt.BP_MEASUREMENT), self._on_measurement)
        if intermediate and self._has_char(gatt.INTERMEDIATE_CUFF):
            try:
                await c.start_notify(self._char(gatt.INTERMEDIATE_CUFF),
                                     self._on_intermediate)
            except Exception as e:
                log.debug("no intermediate cuff notifications: %s", e)

    # ---------------------------------------------------------------- public
    async def collect(self, quiet_seconds: float = DEFAULT_QUIET_SECONDS,
                      max_seconds: float = DEFAULT_MAX_SESSION_SECONDS,
                      timeout_first: float = 6.0) -> list[BpReading]:
        """Subscribe and gather the device's pushed records.

        Waits up to ``timeout_first`` for the first indication, then keeps
        collecting until ``quiet_seconds`` pass without a new packet
        (hard cap ``max_seconds``).  Records are deduplicated by content and
        returned oldest-first.
        """
        self._require()
        self._readings.clear()
        self._errors.clear()
        self._last_pkt = 0.0
        await self._subscribe()

        start = time.monotonic()
        first_deadline = start + timeout_first
        while True:
            now = time.monotonic()
            if now - start >= max_seconds:
                break
            if not self._readings:
                timeout = max(0.1, first_deadline - now)
                if timeout <= 0.1:
                    break
            else:
                timeout = quiet_seconds - (now - self._last_pkt)
                if timeout <= 0:
                    break
            self._event.clear()
            try:
                await asyncio.wait_for(self._event.wait(), timeout=timeout)
            except (asyncio.TimeoutError, TimeoutError):
                pass
        return self.readings()

    def readings(self) -> list[BpReading]:
        out = list(self._readings.values())
        out.sort(key=lambda r: (r.timestamp is None, r.timestamp or 0))
        return out

    async def watch(self, intermediate: bool = False) -> AsyncIterator[BpReading]:
        """Yield records as the device indicates them (kept subscribed).

        After the initial history push, keep the connection open: many
        devices also indicate a fresh measurement shortly after you take one.
        """
        self._require()
        await self._subscribe(intermediate=intermediate)
        queue: asyncio.Queue[BpReading] = asyncio.Queue()

        def cb(r: BpReading) -> None:
            queue.put_nowait(r)

        self._live_cb = cb
        try:
            while True:
                yield await queue.get()
        finally:
            self._live_cb = None

    async def battery(self) -> Optional[int]:
        c = self._require()
        try:
            v = await c.read_gatt_char(self._char(gatt.BATTERY_LEVEL))
            return int(v[0]) if v else None
        except Exception as e:
            log.debug("battery read failed: %s", e)
            return None

    # --------------------------------------------------- Current Time (opt-in)
    async def current_time(self) -> Optional[dt.datetime]:
        """Read the meter's wall clock (device-local, naive datetime)."""
        c = self._require()
        raw = await c.read_gatt_char(self._char(gatt.CURRENT_TIME))
        if len(raw) < 7:
            return None
        y, mo, d, h, mi, s = struct.unpack_from("<H5B", raw, 0)
        try:
            return dt.datetime(y, mo, d, h, mi, s)
        except ValueError:
            return None

    async def set_current_time(self, when: dt.datetime) -> bytes:
        """Write the Current Time characteristic (0x2A2B).

        Explicit opt-in only — no sync path calls this automatically
        (see README "Design decisions").  Payload is the SIG Current Time
        layout, 10 bytes: Date Time (u16 LE year, month, day, hour, min,
        sec — device-local wall time) + Day of Week (1=Mon..7=Sun) +
        Fractions256 (0) + Adjust Reason (1 = ODP field update).
        Requires encryption on this firmware, i.e. a bond — `pair` first.
        """
        c = self._require()
        payload = struct.pack("<H8B", when.year, when.month, when.day,
                              when.hour, when.minute, when.second,
                              when.isoweekday(), 0, 1)
        await c.write_gatt_char(self._char(gatt.CURRENT_TIME), payload,
                                response=True)
        log.info("wrote current time %s to %s", when.isoformat(), self.name)
        return payload

    async def device_info(self) -> dict:
        c = self._require()
        info = {}
        for uuid, key in ((gatt.MANUFACTURER_NAME, "manufacturer"),
                          (gatt.MODEL_NUMBER, "model"),
                          (gatt.SERIAL_NUMBER, "serial"),
                          (gatt.FIRMWARE_REVISION, "firmware"),
                          (gatt.HARDWARE_REVISION, "hardware"),
                          (gatt.SOFTWARE_REVISION, "software")):
            try:
                v = await c.read_gatt_char(self._char(uuid))
                info[key] = v.decode("utf-8", "replace")
            except Exception:
                pass
        try:
            raw = await c.read_gatt_char(self._char(gatt.CURRENT_TIME))
            info["current_time_raw"] = raw.hex()
            t = await self.current_time()
            if t is not None:
                info["current_time"] = t.isoformat()
        except Exception:
            pass
        b = await self.battery()
        if b is not None:
            info["battery"] = b
        return info

    async def gatt_dump(self) -> str:
        """Human-readable GATT tree — diagnostic aid for unknown meters."""
        c = self._require()
        lines = []
        for svc in c.services:
            lines.append(f"service {svc.uuid}  ({svc.description or 'primary'})")
            for ch in svc.characteristics:
                props = ",".join(ch.properties)
                lines.append(f"  char {ch.uuid}  [{props}]")
                for d in ch.descriptors:
                    lines.append(f"    desc {d.uuid}")
        return "\n".join(lines)
