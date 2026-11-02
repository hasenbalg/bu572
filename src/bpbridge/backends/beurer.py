"""Beurer BM 59 "Sync/Memory" mode — Nordic UART tunnel over raw L2CAP/ATT.

Protocol source: ipanalytics/Beurer-BM59-Unlocked-Reverse-Engineering
(byte-level spec from a captured HCI snoop log, verified on a live unit;
full credit in README/FINDINGS).  The facts that drive this design:

* Two advertise modes.  Live mode (auto after measuring, name ``Beurer
  BM59``) is standard Blood Pressure Service 0x1810 and belongs to the
  ``bps`` backend.  This module is Sync mode: hold the power/memory button
  ~3 s until the Bluetooth icon blinks; the device then advertises its
  *serial number* for 60-90 s and exports its memory over a Nordic UART
  service (6e400001-...) tunneling SIG structures.
* **1500 ms connection watchdog**: unless the CCCD write and a 10-byte
  Current Time packet land within ~1.5 s of connect, the firmware
  disconnects.  bleak/BlueZ performs a full service discovery
  (``ServicesResolved``) before a write is possible — on slow links that
  alone exceeds the window.  Hence: raw ATT over an AF_BLUETOOTH L2CAP
  socket (kernel CID 4), fixed handles, no discovery.
* LE **static random** address type; security is Mode 1 Level 2
  (Just Works encryption, brought up by the kernel via the L2CAP socket
  security option — no pairing keys needed).
* CCCD must be written with ATT Write **Request** (0x12); the time packet
  with ATT Write **Command** (0x52) — swapped, the device silently stalls.
* Every indication (0x1D) needs an immediate Handle Value Confirmation
  (0x1E) or the stream freezes and the watchdog bites.
* Only records with ``is_sent == 0`` are streamed; confirming marks them
  sent — same "one designated sync host / vendor app steals records"
  caveat as the BU 572.

Requires root (raw L2CAP sockets + security level setsockopt).
"""
from __future__ import annotations

import asyncio
import ctypes
import datetime as dt
import logging
import socket
import struct
import time
from typing import Callable, Optional

from ..models import BpReading
from ..records import RecordError, build_current_time_packet, parse_beurer_nus_record

log = logging.getLogger("bpbridge.beurer")

# ATT opcodes (Bluetooth Core Spec, GATT chapter)
ATT_ERROR_RSP = 0x01
ATT_WRITE_REQ = 0x12
ATT_WRITE_RSP = 0x13
ATT_HANDLE_VALUE_IND = 0x1D
ATT_HANDLE_VALUE_CNF = 0x1E
ATT_WRITE_CMD = 0x52

# Attribute handles of the NUS service on the BM 59 firmware (verified
# against the reference capture; constructor-overridable for other builds).
HANDLE_NUS_RX = 0x0012    # host -> device  (write without response)
HANDLE_NUS_TX = 0x0015    # device -> host  (indicate)
HANDLE_NUS_CCCD = 0x0016  # CCCD of NUS_TX  (write with response)

# Linux Bluetooth socket constants (not all exposed by the socket module)
_BTPROTO_L2CAP = getattr(socket, "BTPROTO_L2CAP", 3)
_AF_BLUETOOTH = getattr(socket, "AF_BLUETOOTH", 31)
_SOL_BLUETOOTH = 274
_SO_L2CAP_SECURITY_LEVEL = 4
_BT_SECURITY_MEDIUM = 2
_ATT_CID = 4
_BDADDR_LE_RANDOM = 2


class BeurerError(RuntimeError):
    pass


def detect_address_type(mac: str) -> int:
    """LE address type as BlueZ saw it in advertisements (1=public, 2=random).

    The published BM 59 RE claims sync mode advertises under a NEW static
    random address; Frank's firmware was observed KEEPING its public
    address — hardcoding type 2 then yields EINVAL.  Default to random
    when BlueZ has no record.
    """
    try:
        from dbus_fast import BusType
        from dbus_fast.main import MessageBus
        bus = MessageBus(bus_type=BusType.SYSTEM)
        bus.connect()
        try:
            intro = bus.introspect("org.bluez", "/")
            obj = bus.get_proxy_object("org.bluez", "/", intro)
            mgr = obj.get_interface("org.freedesktop.DBus.ObjectManager")
            objects = mgr.call_get_managed_objects()
        finally:
            bus.disconnect()
        for _path, ifaces in objects.items():
            dev = ifaces.get("org.bluez.Device1")
            if not dev:
                continue
            addr = getattr(dev.get("Address"), "value", dev.get("Address"))
            if str(addr) != mac:
                continue
            at = dev.get("AddressType")
            at = str(getattr(at, "value", at)).lower()
            return 1 if at == "public" else 2
    except Exception as e:
        log.debug("address-type detection failed (%s); assuming random", e)
    return 2


class L2capAttTransport:
    """SEQPACKET L2CAP socket on the fixed ATT channel, encrypted link.

    Each send/recv is exactly one ATT PDU (kernel framing).
    """

    def __init__(self, mac: str, address_type: Optional[int] = None):
        if address_type is None:
            address_type = detect_address_type(mac)
            # the detected type can be stale (the modes may differ) — let a
            # fast EINVAL fall back to the other one
            types = [address_type, 2 if address_type == 1 else 1]
        else:
            types = [address_type]
        last: Optional[Exception] = None
        tried = []
        for at in types:
            label = "public" if at == 1 else "random"
            tried.append(label)
            s = socket.socket(_AF_BLUETOOTH, socket.SOCK_SEQPACKET,
                              _BTPROTO_L2CAP)
            try:
                # medium security: Just Works encryption brought up on
                # connect; key_size 0 = adapter default
                s.setsockopt(_SOL_BLUETOOTH, _SO_L2CAP_SECURITY_LEVEL,
                             struct.pack("BB", _BT_SECURITY_MEDIUM, 0))
                self._bind_att(s, at)
                self._connect(s, mac, at)
            except Exception as e:
                last = e
                s.close()
                continue
            self.sock = s
            # give the kernel time to finish the SMP exchange before the
            # first write — the reference implementation waits ~250 ms
            time.sleep(0.30)
            return
        assert last is not None
        raise type(last)(f"{last} (address types tried: "
                         f"{', '.join(tried)})") from last

    @staticmethod
    def _bind_att(s: socket.socket, address_type: int) -> None:
        """bind() the fixed ATT source CID before connect().

        Since the src_type fix-up landed, l2cap_sock_connect() rejects an
        unbound socket to CID 4 from a BR/EDR adapter with EINVAL because
        chan->scid is still 0 ("accept only when both source and
        destination CID indicate ATT") — seen live on kernel 6.18-rpi.
        BlueZ's own btio (gatttool et al.) always binds first; do the
        same.  Older kernels simply accept it.
        """
        try:
            s.bind(("00:00:00:00:00:00", 0, _ATT_CID, address_type))
            return
        except (ValueError, OSError):
            pass  # old CPython tuple form: fall through to libc bind
        # struct sockaddr_l2 is identical to the connect one; reuse it.
        libc = ctypes.CDLL("libc.so.6", use_errno=True)

        class _BdAddr(ctypes.Structure):
            _fields_ = [("b", ctypes.c_uint8 * 6)]

        class _SockAddrL2(ctypes.Structure):
            _fields_ = [
                ("l2_family", ctypes.c_uint16),
                ("l2_psm", ctypes.c_uint16),
                ("l2_bdaddr", _BdAddr),
                ("l2_cid", ctypes.c_uint16),
                ("l2_bdaddr_type", ctypes.c_uint8),
                ("l2_padding", ctypes.c_uint8),
            ]

        addr = _SockAddrL2()
        addr.l2_family = _AF_BLUETOOTH
        addr.l2_cid = _ATT_CID
        addr.l2_bdaddr_type = address_type
        if libc.bind(s.fileno(), ctypes.byref(addr), ctypes.sizeof(addr)) != 0:
            err = ctypes.get_errno()
            raise OSError(err, f"L2CAP bind(CID ATT) failed: {os_strerror(err)}")

    @staticmethod
    def _connect(s: socket.socket, mac: str, address_type: int) -> None:
        try:
            # Python >= 3.12 accepts the LE address type in the tuple
            s.connect((mac, 0, _ATT_CID, address_type))
            return
        except (ValueError, OSError):
            pass  # older CPython: 4-tuple rejected, fall back to libc
        L2capAttTransport._connect_libc(s, mac, address_type)

    @staticmethod
    def _connect_libc(s: socket.socket, mac: str, address_type: int) -> None:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)

        class _BdAddr(ctypes.Structure):
            _fields_ = [("b", ctypes.c_uint8 * 6)]

        class _SockAddrL2(ctypes.Structure):
            _fields_ = [
                ("l2_family", ctypes.c_uint16),
                ("l2_psm", ctypes.c_uint16),
                ("l2_bdaddr", _BdAddr),
                ("l2_cid", ctypes.c_uint16),
                ("l2_bdaddr_type", ctypes.c_uint8),
                ("l2_padding", ctypes.c_uint8),   # kernel struct is 14 bytes
            ]

        addr = _SockAddrL2()
        addr.l2_family = _AF_BLUETOOTH
        addr.l2_psm = 0
        addr.l2_cid = _ATT_CID
        addr.l2_bdaddr_type = address_type
        for i, octet in enumerate(reversed(mac.split(":"))):
            addr.l2_bdaddr.b[i] = int(octet, 16)
        res = libc.connect(s.fileno(), ctypes.byref(addr), ctypes.sizeof(addr))
        if res != 0:
            err = ctypes.get_errno()
            raise OSError(err, f"L2CAP connect to {mac} failed: "
                               f"{os_strerror(err)}"
                               + (" — address type mismatch or the monitor "
                                  "is not advertising right now (memory/"
                                  "sync mode must be active during the "
                                  "connect)" if err == 22 else ""))

    def send(self, pdu: bytes) -> None:
        self.sock.send(pdu)

    def recv(self, timeout: float) -> Optional[bytes]:
        """One ATT PDU, or None on timeout.  Raises ConnectionError when
        the monitor ended the session."""
        self.sock.settimeout(timeout)
        try:
            data = self.sock.recv(1024)
        except (TimeoutError, socket.timeout):
            return None
        except OSError as e:
            raise ConnectionError(f"link closed: {e}") from e
        if not data:
            raise ConnectionError("link closed by monitor")
        return data

    def close(self) -> None:
        try:
            self.sock.close()
        except Exception:
            pass


def os_strerror(err: int) -> str:
    import os
    return os.strerror(err)


def sync_memory(
    mac: str,
    when: Optional[dt.datetime] = None,
    *,
    on_record: Optional[Callable[[BpReading], None]] = None,
    idle_timeout: float = 3.0,
    max_seconds: float = 180.0,
    rx_handle: int = HANDLE_NUS_RX,
    tx_handle: int = HANDLE_NUS_TX,
    cccd_handle: int = HANDLE_NUS_CCCD,
    transport=None,
) -> list[BpReading]:
    """Run one BM 59 memory-export session; return the streamed records.

    ``when`` is the wall time handed to the device (protocol-mandated
    handshake — the export is impossible without writing the clock; this
    is the ONE place a sync writes time, and only for this device).
    ``transport`` is injectable for tests.
    """
    if when is None:
        when = dt.datetime.now()
    if transport is None:
        transport = L2capAttTransport(mac)
    readings: list[BpReading] = []
    seen: set = set()
    deadline = time.monotonic() + max_seconds
    try:
        # 1) enable indications — MUST be an acknowledged Write Request
        transport.send(struct.pack("<BHH", ATT_WRITE_REQ, cccd_handle, 0x0002))
        _expect_write_response(transport, tx_handle, deadline)

        # 2) time packet — MUST be an unacknowledged Write Command;
        #    this also resets the 1500 ms watchdog
        transport.send(bytes([ATT_WRITE_CMD])
                       + struct.pack("<H", rx_handle)
                       + build_current_time_packet(when))
        log.info("handshake done for %s — streaming memory", mac)

        # 3) stream until the device goes quiet or hangs up
        while time.monotonic() < deadline:
            pdu = transport.recv(idle_timeout)
            if pdu is None:
                log.info("quiet for %.1f s — session complete", idle_timeout)
                break
            opcode = pdu[0]
            if opcode == ATT_HANDLE_VALUE_IND:
                handle = struct.unpack_from("<H", pdu, 1)[0]
                payload = pdu[3:]
                # confirm FIRST: the firmware waits for 0x1E before the
                # next record and disconnects if it never arrives
                transport.send(bytes([ATT_HANDLE_VALUE_CNF]))
                if handle != tx_handle:
                    log.debug("indication on unexpected handle 0x%04x", handle)
                    continue
                try:
                    reading = parse_beurer_nus_record(payload)
                except RecordError as e:
                    log.warning("skipping unparseable record: %s (%s)",
                                e, payload.hex())
                    continue
                reading.device = mac
                key = reading.dedupe_key()
                if key in seen:
                    log.debug("duplicate record skipped: %s", reading)
                    continue
                seen.add(key)
                readings.append(reading)
                if on_record is not None:
                    try:
                        on_record(reading)
                    except Exception:
                        log.exception("on_record callback failed")
            elif opcode == ATT_ERROR_RSP:
                req, err_handle, err_code = struct.unpack_from("<BBB", pdu, 1)
                raise BeurerError(
                    f"ATT error: opcode 0x{req:02x} on handle "
                    f"0x{err_handle:04x} -> 0x{err_code:02x}")
            else:
                log.debug("ignoring ATT PDU 0x%02x (%d bytes)",
                          opcode, len(pdu))
    except ConnectionError as e:
        log.info("monitor ended the session (%s) — %d record(s)",
                 e, len(readings))
    finally:
        transport.close()
    return readings


def _expect_write_response(transport, tx_handle: int,
                           deadline: float) -> None:
    while time.monotonic() < deadline:
        pdu = transport.recv(2.0)
        if pdu is None:
            raise BeurerError("no Write Response for the CCCD — is the "
                              "monitor in sync mode (blinking Bluetooth "
                              "icon)?")
        if pdu[0] == ATT_WRITE_RSP:
            return
        if pdu[0] == ATT_ERROR_RSP:
            req, err_handle, err_code = struct.unpack_from("<BBB", pdu, 1)
            raise BeurerError(f"CCCD write rejected: error 0x{err_code:02x} "
                              f"on handle 0x{err_handle:04x}")
        if pdu[0] == ATT_HANDLE_VALUE_IND:
            # early indication — confirm it and keep waiting
            transport.send(bytes([ATT_HANDLE_VALUE_CNF]))
            log.debug("early indication during handshake confirmed")
            continue
        log.debug("unexpected PDU 0x%02x during handshake", pdu[0])
    raise BeurerError("timed out waiting for the CCCD Write Response")


async def sync_memory_async(mac: str, when: Optional[dt.datetime] = None,
                            **kwargs) -> list[BpReading]:
    """Async wrapper (the raw socket session is blocking by design)."""
    return await asyncio.to_thread(sync_memory, mac, when, **kwargs)
