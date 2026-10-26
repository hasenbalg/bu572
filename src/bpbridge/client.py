"""Top-level API: scan for blood pressure monitors and open the right backend."""
from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass, field
from typing import Optional, Union

from bleak import BleakScanner
from bleak.backends.device import BLEDevice

from . import gatt
from .backends.bps import BpMeter

log = logging.getLogger("bpbridge.client")

# name patterns -> backend, matched case-insensitively on the adv name
NAME_PATTERNS = [
    ("bu?5*", "bps"),             # Medisana BU 5xx connect family ("BU 572")
    ("bu5*", "bps"),              # ...advertised without the space
    ("medisana*", "bps"),
]

# service UUIDs in advertisements -> backend (order = priority)
SERVICE_HINTS = {
    gatt.BP_SERVICE: "bps",
}


@dataclass
class Advertisement:
    address: str
    name: Optional[str]
    rssi: Optional[int]
    has_bp_service: bool
    backend: str                      # "bps" | "unknown"
    service_uuids: list = field(default_factory=list)

    def __str__(self) -> str:
        name = self.name or "(unnamed)"
        return (f"{self.address}  {name:<28} rssi={self.rssi} "
                f"backend={self.backend}")


def classify(name: Optional[str], service_uuids: list[str]) -> str:
    lname = (name or "").lower().strip()
    if lname:
        for pattern, backend in NAME_PATTERNS:
            if fnmatch.fnmatch(lname, pattern):
                return backend
    uuids = {u.lower() for u in service_uuids or []}
    for uuid, backend in SERVICE_HINTS.items():
        if uuid in uuids:
            return backend
    return "unknown"


def _dbus_val(x):
    """dbus-fast wraps 'v'-signature fields in Variant objects; unwrap them.

    Without this, iterating e.g. org.bluez.Device1.UUIDs raises
    "'dbus_fast.signature.Variant' object is not iterable", and str(Variant)
    corrupts Address/Name into repr garbage.
    """
    return x.value if hasattr(x, "value") and hasattr(x, "signature") else x


def _bus_devices_to_advs(objects) -> list[Advertisement]:
    """org.bluez GetManagedObjects payload -> Advertisement list."""
    out = []
    for path, ifaces in objects.items():
        dev = ifaces.get("org.bluez.Device1")
        if not dev:
            continue
        addr = _dbus_val(dev.get("Address"))
        if not addr:
            continue
        name = _dbus_val(dev.get("Name")) or _dbus_val(dev.get("Alias"))
        uuids = [str(u).lower() for u in (_dbus_val(dev.get("UUIDs")) or [])]
        rssi = _dbus_val(dev.get("RSSI"))
        out.append(Advertisement(
            address=str(addr),
            name=str(name) if name else None,
            rssi=int(rssi) if rssi is not None else None,
            has_bp_service=any(u.lower() == gatt.BP_SERVICE for u in uuids),
            backend=classify(str(name) if name else None, uuids),
            service_uuids=uuids,
        ))
    return out


async def _cached_devices() -> list[Advertisement]:
    """Devices BlueZ already knows from earlier discoveries.

    bleak's discover() only reports devices that *appear* during the scan
    window; anything cached by bluetoothctl would make scans look empty.
    """
    try:
        from dbus_fast import BusType
        from dbus_fast.aio import MessageBus
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        intro = await bus.introspect("org.bluez", "/")
        obj = bus.get_proxy_object("org.bluez", "/", intro)
        manager = obj.get_interface("org.freedesktop.DBus.ObjectManager")
        objects = await manager.call_get_managed_objects()
        bus.disconnect()
    except Exception:
        return []
    return _bus_devices_to_advs(objects)


async def scan(timeout: float = 10.0, all_devices: bool = False) -> list[Advertisement]:
    """Scan for BLE blood pressure monitors.

    By default only devices advertising the Blood Pressure Service or
    matching a known monitor name are returned; pass all_devices=True for
    everything.  BlueZ-cached devices are included as well.
    """
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    results = []
    for dev, adv in found.values():
        uuids = list(adv.service_uuids or [])
        backend = classify(adv.local_name or dev.name, uuids)
        if not all_devices and backend == "unknown":
            continue
        results.append(Advertisement(
            address=dev.address,
            name=adv.local_name or dev.name,
            rssi=adv.rssi,
            has_bp_service=any(u.lower() == gatt.BP_SERVICE for u in uuids),
            backend=backend,
            service_uuids=uuids,
        ))
    seen = {a.address for a in results}
    for cached in await _cached_devices():
        if cached.address in seen:
            continue
        if not all_devices and cached.backend == "unknown":
            continue
        seen.add(cached.address)
        results.append(cached)
    results.sort(key=lambda a: -(a.rssi if a.rssi is not None else -999))
    return results


class BpBridge:
    """Entry point tying scan + backend selection together.

    Usage::

        async with BpBridge() as bp:
            devices = await bp.scan()
            readings = await bp.dump(devices[0])
    """

    def __init__(self, adapter=None):
        self.adapter = adapter
        self._meter = None

    async def scan(self, timeout: float = 10.0,
                   all_devices: bool = False) -> list[Advertisement]:
        return await scan(timeout, all_devices)

    async def _resolve_backend(self, address: str) -> Optional[str]:
        """Classify a bare MAC: BlueZ cache first, then a short scan.

        Connecting by bare address carries no name/UUID — resolving first
        keeps the log and any future multi-backend routing honest.
        """
        for adv in await _cached_devices():
            if adv.address == address and adv.backend != "unknown":
                return adv.backend
        try:
            found = await BleakScanner.discover(timeout=5.0, return_adv=True)
            for dev, adv in found.values():
                if dev.address == address:
                    b = classify(adv.local_name or dev.name,
                                 list(adv.service_uuids or []))
                    if b != "unknown":
                        return b
        except Exception:
            pass
        return None

    async def connect(self, target: Union[str, BLEDevice, Advertisement]) -> BpMeter:
        """Open a Blood Pressure Service connection."""
        if isinstance(target, Advertisement):
            target = target.address
        if isinstance(target, str):
            backend = await self._resolve_backend(target)
            if backend is None:
                log.info("no name/service hint for %s; using standard BLP "
                         "backend anyway", target)
        self._meter = BpMeter(target)
        await self._meter.connect()
        return self._meter

    async def dump(self, target, quiet_seconds: float = 1.5,
                   max_seconds: float = 25.0,
                   timeout_first: float = 6.0) -> list:
        """Connect and collect the records the device pushes."""
        meter = await self.connect(target)
        try:
            return await meter.collect(quiet_seconds=quiet_seconds,
                                       max_seconds=max_seconds,
                                       timeout_first=timeout_first)
        finally:
            try:
                await meter.disconnect()
            except Exception:
                pass

    async def close(self):
        if self._meter:
            await self._meter.disconnect()
            self._meter = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()
