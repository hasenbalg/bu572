"""BlueZ pairing helpers (headless, own agent).

The BU 572's BP Measurement indications work on a plain connection, but its
Current Time characteristic (and possibly future firmware) sits behind an
encrypted link; bonding once removes any such hurdle permanently.  This
module registers a minimal org.bluez.Agent1 that auto-answers everything a
keypad-less, display-less device can throw at us (Just Works / Yes-No), and
provides an idempotent pair + an unpair.

Pairing is the ONLY mutation this library performs, and it is always
user-invoked (see README "Design decisions" on keeping the meter read-only).
"""
from __future__ import annotations

import logging

from dbus_fast import BusType, DBusError
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, method

log = logging.getLogger("bpbridge.pairing")

AGENT_PATH = "/org/bluez/bpbridgeagent"


def _unwrap(x):
    return x.value if hasattr(x, "value") and hasattr(x, "signature") else x


class BpAgentInterface(ServiceInterface):
    """Auto-answering BlueZ pairing agent (no display, no keyboard)."""

    def __init__(self):
        super().__init__("org.bluez.Agent1")

    @method()
    def Release(self):                                        # noqa: N802
        log.debug("agent released")

    @method()
    def RequestPinCode(self, device: "o") -> "s":             # noqa: N802,F821
        log.debug("RequestPinCode %s -> ''", device)
        return ""

    @method()
    def RequestPasskey(self, device: "o") -> "u":             # noqa: N802,F821
        # Neither side can show/enter a code on a BP monitor; reject rather
        # than stall — real BP devices pair via Just Works.
        raise DBusError("org.bluez.Error.Rejected", "no passkey available")

    @method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):  # noqa: N802,F821
        log.info("passkey shown by %s: %06u (enter %d digits)",
                 device, passkey, entered)

    @method()
    def DisplayPinCode(self, device: "o", pincode: "s"):      # noqa: N802,F821
        log.info("pincode shown by %s: %s", device, pincode)

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u"):  # noqa: N802,F821
        log.debug("RequestConfirmation %s -> auto-yes", device)

    @method()
    def RequestAuthorization(self, device: "o"):              # noqa: N802,F821
        log.debug("RequestAuthorization %s -> auto-yes", device)

    @method()
    def AuthorizeService(self, device: "o", uuid: "s"):       # noqa: N802,F821
        log.debug("AuthorizeService %s %s -> auto-yes", device, uuid)

    @method()
    def Cancel(self, device: "o"):                            # noqa: N802,F821
        log.debug("agent cancelled for %s", device)


def dev_path(address: str) -> str:
    return "/org/bluez/hci0/dev_" + address.upper().replace(":", "_")


async def _read_paired(bus, path: str) -> bool:
    props = bus.get_proxy_object(
        "org.bluez", path, await bus.introspect("org.bluez", path)
    ).get_interface("org.freedesktop.DBus.Properties")
    v = await props.call_get("org.bluez.Device1", "Paired")
    return bool(_unwrap(v))


async def _device_known(bus, path: str) -> bool:
    """True iff BlueZ currently exposes org.bluez.Device1 at path.

    Introspection alone is NOT proof: BlueZ answers introspect on stale
    paths, and get_interface then dies with the misleading
    "interface not found on this object: org.bluez.Device1".
    ObjectManager is the authoritative object list.
    """
    root_intro = await bus.introspect("org.bluez", "/")
    root = bus.get_proxy_object("org.bluez", "/", root_intro)
    mgr = root.get_interface("org.freedesktop.DBus.ObjectManager")
    objects = await mgr.call_get_managed_objects()
    return path in objects and "org.bluez.Device1" in objects[path]


async def _rediscover(address: str, timeout: float = 10.0) -> None:
    """Kick a BlueZ discovery pass so the Device1 object (re)appears.

    The meter must be awake/advertising while this runs.
    """
    try:
        from bleak import BleakScanner
        await BleakScanner.discover(timeout=timeout, device_address=address)
    except Exception as e:
        log.debug("rediscovery scan failed: %s", e)


async def pair(address: str, renew: bool = False) -> bool:
    """Bond with a device (idempotent).  Returns True when bonded.

    Already paired -> only mark Trusted.  Never re-run SMP while a bond
    exists unless ``renew`` is passed: cheap firmware refuses renegotiation
    and can destroy the working bond on BOTH sides.  ``renew`` removes the
    host-side bond first — the device may keep its half; power-cycle it if
    the fresh pairing then stalls silently.
    """
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        path = dev_path(address)
        if not await _device_known(bus, path):
            log.info("no cached BlueZ object for %s — running discovery "
                     "(keep the monitor awake)", address)
            await _rediscover(address)
            if not await _device_known(bus, path):
                raise RuntimeError(
                    f"BlueZ still has no Device1 object for {address} — "
                    f"the monitor is not advertising: wake it (power button) "
                    f"and retry")
        intro = await bus.introspect("org.bluez", path)
        dev = bus.get_proxy_object("org.bluez", path, intro)
        dev1 = dev.get_interface("org.bluez.Device1")

        if await _read_paired(bus, path) and not renew:
            log.info("%s already paired — marking trusted", address)
            await dev1.call_set_trusted(True)
            return True

        if renew:
            log.info("removing old host-side bond for %s", address)
            await _remove(bus, path)

        root_intro = await bus.introspect("org.bluez", "/")
        root = bus.get_proxy_object("org.bluez", "/", root_intro)
        mgr = root.get_interface("org.bluez.AgentManager1")
        agent = BpAgentInterface()
        bus.export(AGENT_PATH, agent)
        await mgr.call_register_agent(AGENT_PATH, "DisplayYesNo")
        try:
            await mgr.call_request_default_agent(AGENT_PATH)
        except Exception as e:
            log.debug("RequestDefaultAgent failed (%s); continuing", e)

        log.info("pairing %s ... (have the meter advertising)", address)
        try:
            await dev1.call_pair()
        finally:
            try:
                await mgr.call_unregister_agent(AGENT_PATH)
            except Exception:
                pass

        await dev1.call_set_trusted(True)
        log.info("paired and trusted: %s", address)
        return True
    finally:
        bus.disconnect()


async def _remove(bus, path: str) -> bool:
    """Device removal with Adapter1.RemoveDevice / Device1.Remove fallback."""
    adapter_path = path.rsplit("/", 1)[0]
    try:
        a_intro = await bus.introspect("org.bluez", adapter_path)
        adapter = bus.get_proxy_object("org.bluez", adapter_path, a_intro)
        ai = adapter.get_interface("org.bluez.Adapter1")
        if hasattr(ai, "call_remove_device"):
            await ai.call_remove_device(path)
            return True
    except Exception as e:
        log.debug("Adapter1.RemoveDevice failed (%s); trying Device1.Remove", e)
    dev_intro = await bus.introspect("org.bluez", path)
    dev = bus.get_proxy_object("org.bluez", path, dev_intro)
    await dev.get_interface("org.bluez.Device1").call_remove()
    return True


async def unpair(address: str) -> bool:
    """Remove this host's bond (idempotent).  Returns True if one existed.

    The device keeps its half of the keys — if a later fresh pair stalls
    silently for ~30 s, power-cycle the meter so it forgets too.
    """
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        path = dev_path(address)
        if not await _device_known(bus, path):
            log.info("no BlueZ object for %s — nothing to remove", address)
            return False
        await _remove(bus, path)
        log.info("removed %s from BlueZ", address)
        return True
    finally:
        bus.disconnect()
