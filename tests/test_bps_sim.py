"""End-to-end tests against a simulated meter (fake bleak client).

The fake exposes the real BLP GATT surface of the BU 572: BP Service with
indicate-able 0x2A35, battery, device info.  It pushes stored records with
one duplicate, exercising collect()'s session logic and dedupe.
"""
from __future__ import annotations

import asyncio

from bpbridge import gatt
from bpbridge.backends.bps import BpMeter
from bpbridge.models import BpReading
from bpbridge.records import parse_bp_measurement

V_OLDER = bytes.fromhex("0e" "7200" "4e00" "5900" "ea070a19081e00" "4600" "00")
V_NEWER = bytes.fromhex("0e" "7800" "5000" "5d00" "ea070a1a0c2238" "4800" "01")
V_BAD = bytes.fromhex("0e" "ff")


class FakeChar:
    def __init__(self, uuid, props=("read",), descriptors=()):
        self.uuid = uuid
        self.properties = list(props)
        self.descriptors = list(descriptors)


class FakeSvc:
    def __init__(self, uuid, chars, description="primary"):
        self.uuid = uuid
        self.characteristics = chars
        self.description = description


class FakeClient:
    """Minimal bleak.BleakClient surface used by BpMeter."""

    def __init__(self):
        self.is_connected = True
        self.callbacks = {}
        self.bp = FakeChar(gatt.BP_MEASUREMENT, ("indicate",))
        self.cuff = FakeChar(gatt.INTERMEDIATE_CUFF, ("notify",))
        self.services = [
            FakeSvc(gatt.BP_SERVICE, [self.bp, self.cuff]),
            FakeSvc(gatt.BATTERY_SERVICE, [FakeChar(gatt.BATTERY_LEVEL)]),
            FakeSvc(gatt.DEVICE_INFO_SERVICE, [
                FakeChar(gatt.MANUFACTURER_NAME), FakeChar(gatt.MODEL_NUMBER),
                FakeChar(gatt.FIRMWARE_REVISION)]),
            FakeSvc("00001801-0000-1000-8000-00805f9b34fb",
                    [FakeChar("00002a05-0000-1000-8000-00805f9b34fb",
                              ("indicate",))]),
        ]
        self._reads = {
            gatt.BATTERY_LEVEL: b"\x64",
            gatt.MANUFACTURER_NAME: b"medisana",
            gatt.MODEL_NUMBER: b"TMB-2276-B",
            gatt.FIRMWARE_REVISION: b"V1.0.7",
        }

    async def start_notify(self, char, cb):
        self.callbacks[char.uuid] = cb

    async def read_gatt_char(self, char):
        uuid = char.uuid if hasattr(char, "uuid") else char
        if uuid in self._reads:
            return self._reads[uuid]
        raise Exception(f"read not permitted: {uuid}")

    async def disconnect(self):
        self.is_connected = False

    def push(self, payload: bytes):
        self.callbacks[gatt.BP_MEASUREMENT](0, bytearray(payload))


def _meter_with_fake():
    m = BpMeter("AA:BB:CC:DD:EE:FF", name="BU 572")
    fake = FakeClient()
    m._client = fake
    return m, fake


async def test_collect_dedupes_and_sorts():
    m, fake = _meter_with_fake()

    async def drip():
        await asyncio.sleep(0.05)
        fake.push(V_NEWER)
        await asyncio.sleep(0.05)
        fake.push(V_OLDER)
        await asyncio.sleep(0.05)
        fake.push(V_NEWER)          # duplicate push (device retries)
        fake.push(V_BAD)            # unparseable -> ignored, not fatal

    task = asyncio.create_task(drip())
    readings = await m.collect(quiet_seconds=0.3, max_seconds=5, timeout_first=2)
    await task

    assert len(readings) == 2
    assert readings[0] == m.readings()[0]
    assert readings[0].systolic == parse_bp_measurement(V_OLDER).systolic
    assert readings[1].systolic == 120.0
    assert readings[0].timestamp < readings[1].timestamp
    assert readings[1].device == "BU 572"


async def test_collect_first_packet_timeout():
    m, fake = _meter_with_fake()
    readings = await m.collect(quiet_seconds=0.2, max_seconds=5, timeout_first=0.3)
    assert readings == []


async def test_battery_and_device_info():
    m, fake = _meter_with_fake()
    assert await m.battery() == 100
    info = await m.device_info()
    assert info["manufacturer"] == "medisana"
    assert info["model"] == "TMB-2276-B"
    assert info["firmware"] == "V1.0.7"
    assert info["battery"] == 100


async def test_watch_yields_live():
    m, fake = _meter_with_fake()
    gen = m.watch()
    # let watch subscribe
    pusher = asyncio.create_task(_delayed_push(fake))
    r = await asyncio.wait_for(gen.__anext__(), timeout=3)
    assert isinstance(r, BpReading)
    assert r.systolic == 120.0
    await pusher
    await gen.aclose()


async def _delayed_push(fake):
    await asyncio.sleep(0.05)
    fake.push(bytes.fromhex("0e" "7800" "5000" "5d00"
                            "ea070a1a0c2238" "4800" "01"))


async def test_gatt_dump_lists_services():
    m, fake = _meter_with_fake()
    dump = await m.gatt_dump()
    assert gatt.BP_SERVICE in dump
    assert gatt.BP_MEASUREMENT in dump
    assert "[indicate]" in dump
