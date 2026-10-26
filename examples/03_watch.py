#!/usr/bin/env python3
"""03 — live watch: keep the session open and print new measurements.

The meter indicates the initial stored records, then typically a fresh
measurement as soon as you finish one on the device.
"""
import asyncio

from bpbridge import BpBridge


async def main():
    address = "AA:BB:CC:DD:EE:FF"

    async with BpBridge() as bp:
        meter = await bp.connect(address)
        try:
            async for r in meter.watch():
                print(r)
        except KeyboardInterrupt:
            pass
        finally:
            await meter.disconnect()


asyncio.run(main())
