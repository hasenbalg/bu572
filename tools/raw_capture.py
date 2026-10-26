#!/usr/bin/env python3
"""Raw GATT capture — the reverse-engineering probe for unknown meters.

Connects, prints the full GATT tree, then subscribes to EVERY
notify/indicate characteristic and prints each packet as timestamped hex
until Ctrl-C.  Use this when the decoder misbehaves on live hardware or to
profile a new device model:

    python3 tools/raw_capture.py AA:BB:CC:DD:EE:FF

Also dumps the readable characteristics (device info, battery, current
time) once before subscribing.  READ-ONLY by design — never writes.
"""
import asyncio
import sys
import time

from bleak import BleakClient


def short(uuid: str) -> str:
    u = uuid.lower().replace("-", "")
    return f"0x{u[4:8]}" if len(u) == 32 and u.startswith("0000") and \
        u.endswith("00001000800000805f9b34fb") else uuid


def on_packet(sender, data: bytearray):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {short(str(sender))}: {bytes(data).hex(' ')}")


async def dump_reads(client: BleakClient):
    print("== readable characteristics ==")
    for svc in client.services:
        for ch in svc.characteristics:
            if "read" not in ch.properties:
                continue
            try:
                v = await client.read_gatt_char(ch)
                txt = v.decode("utf-8", "replace").strip("\x00")
                printable = bool(txt) and all(32 <= ord(c) < 127 for c in txt)
                print(f"  {short(ch.uuid)}: "
                      f"{txt!r}" if printable else f"  {short(ch.uuid)}: {v.hex(' ')}")
            except Exception as e:
                print(f"  {short(ch.uuid)}: <read failed: {e}>")


async def main(address: str):
    async with BleakClient(address, timeout=30) as client:
        print("== GATT tree ==")
        for svc in client.services:
            print(f"service {short(svc.uuid)}  ({svc.description or 'primary'})")
            for ch in svc.characteristics:
                print(f"  char {short(ch.uuid)}  [{','.join(ch.properties)}]")

        await dump_reads(client)

        print("== notifications (Ctrl-C to stop) ==")
        subscribed = 0
        for svc in client.services:
            for ch in svc.characteristics:
                if "notify" in ch.properties or "indicate" in ch.properties:
                    try:
                        await client.start_notify(ch, on_packet)
                        subscribed += 1
                        print(f"  subscribed {short(ch.uuid)}")
                    except Exception as e:
                        print(f"  could not subscribe {short(ch.uuid)}: {e}")
        if not subscribed:
            print("  (nothing to subscribe to)")
        print("waiting... take a measurement on the device / press its "
              "memory or send button")
        while True:
            await asyncio.sleep(1)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    try:
        asyncio.run(main(sys.argv[1]))
    except KeyboardInterrupt:
        pass
