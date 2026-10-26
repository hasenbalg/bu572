#!/usr/bin/env python3
"""02 — download the records the monitor pushes on connect.

The BU 572 pushes its stored measurements as BP Measurement indications
right after the subscription opens; the session ends after a quiet period.
Deduplication is by record content, so re-running is cheap and safe.
"""
import asyncio
import json

from bpbridge import BpBridge


async def main():
    address = "AA:BB:CC:DD:EE:FF"      # from: bpbridge scan

    async with BpBridge() as bp:
        readings = await bp.dump(address)

    for r in readings:
        print(r)
    print(json.dumps([r.to_dict() for r in readings], indent=2)[:80], "...")


asyncio.run(main())
