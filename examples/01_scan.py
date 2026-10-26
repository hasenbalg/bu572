#!/usr/bin/env python3
"""01 — scan for blood pressure monitors.

Wake the monitor first (power button); if a phone with the vendor app is
nearby, turn its Bluetooth off — these devices serve one central at a time.
"""
import asyncio

from bpbridge import scan


async def main():
    for d in await scan(timeout=10.0, all_devices=False):
        print(d)
    # everything, unnamed or not:
    # for d in await scan(timeout=10.0, all_devices=True):
    #     print(d)


asyncio.run(main())
