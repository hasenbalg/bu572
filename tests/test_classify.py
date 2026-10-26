"""Classification and BlueZ-cache parsing tests (no bus needed)."""
from bpbridge import gatt
from bpbridge.client import _bus_devices_to_advs, classify


def test_classify_name():
    assert classify("BU 572", []) == "bps"
    assert classify("BU572", []) == "bps"
    assert classify("MEDISANA BU 510", []) == "bps"
    assert classify("bu 572", []) == "bps"


def test_classify_service_uuid():
    assert classify(None, [gatt.BP_SERVICE]) == "bps"
    assert classify("random thing", ["00001810-0000-1000-8000-00805f9b34fb"]) == "bps"


def test_classify_unknown():
    assert classify("FitBit", []) == "unknown"
    assert classify(None, []) == "unknown"


class V:
    """Stand-in for dbus_fast.signature.Variant."""

    def __init__(self, value):
        self.value = value
        self.signature = "v"


def test_bus_devices_parsing_unwraps_variants():
    objects = {
        "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF": {
            "org.bluez.Device1": {
                "Address": V("AA:BB:CC:DD:EE:FF"),
                "Name": V("BU 572"),
                "UUIDs": V([gatt.BP_SERVICE.upper()]),
                "RSSI": -57,
            }
        },
        "/org/bluez/hci0/dev_11_22_33_44_55_66": {
            "org.bluez.Device1": {
                "Address": "11:22:33:44:55:66",
                "Alias": "thing",
                "UUIDs": [],
                "RSSI": None,
            }
        },
        "/org/bluez/hci0": {"org.bluez.Adapter1": {}},
    }
    advs = _bus_devices_to_advs(objects)
    by_addr = {a.address: a for a in advs}
    assert by_addr["AA:BB:CC:DD:EE:FF"].name == "BU 572"
    assert by_addr["AA:BB:CC:DD:EE:FF"].backend == "bps"
    assert by_addr["AA:BB:CC:DD:EE:FF"].has_bp_service
    assert by_addr["AA:BB:CC:DD:EE:FF"].rssi == -57
    assert by_addr["11:22:33:44:55:66"].backend == "unknown"
    assert by_addr["11:22:33:44:55:66"].rssi is None
