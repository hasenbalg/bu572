"""Pairing helper tests with a fake BlueZ bus (no D-Bus, no radio)."""

from bpbridge.pairing import _device_known, dev_path


class FakeMgr:
    def __init__(self, objects):
        self._objects = objects

    async def call_get_managed_objects(self):
        return self._objects


class FakeIfaceHolder:
    def __init__(self, ifaces):
        self._ifaces = ifaces

    def get_interface(self, name):
        if name == "org.freedesktop.DBus.ObjectManager":
            return FakeMgr(self._ifaces)
        raise AssertionError(f"unexpected interface {name}")


class FakeBus:
    def __init__(self, objects):
        self._objects = objects

    async def introspect(self, service, path):
        return object()

    def get_proxy_object(self, service, path, intro):
        return FakeIfaceHolder(self._objects)


async def test_device_known_requires_device1_interface():
    path = dev_path("B8:B7:7D:23:38:F6")
    assert path == "/org/bluez/hci0/dev_B8_B7_7D_23_38_F6"

    good = {path: {"org.bluez.Device1": {}}, "/org/bluez/hci0": {}}
    assert await _device_known(FakeBus(good), path) is True

    # stale path: object exists but lost its Device1 interface
    stale = {path: {"org.freedesktop.DBus.Properties": {}}}
    assert await _device_known(FakeBus(stale), path) is False

    # path gone entirely
    assert await _device_known(FakeBus({}), path) is False
