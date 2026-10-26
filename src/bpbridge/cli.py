"""Command line interface: scan | info | dump | watch | pair | unpair."""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import sys

from . import __version__
from .client import BpBridge
from .pairing import pair, unpair


def _print_readings(readings, as_json: bool, csv_file) -> None:
    if csv_file:
        from .models import BpReading
        with open(csv_file, "w", newline="") as f:
            w = csv.writer(f)
            (readings[0] if readings else BpReading()).write_csv_header(w)
            for r in readings:
                w.writerow(r.to_csv_row())
        print(f"wrote {len(readings)} readings to {csv_file}")
    elif as_json:
        print(json.dumps([r.to_dict() for r in readings], indent=2))
    else:
        for r in readings:
            print(r)
        print(f"-- {len(readings)} reading(s)")


async def _cmd_scan(args) -> int:
    async with BpBridge() as bp:
        devices = await bp.scan(timeout=args.timeout, all_devices=args.all)
    for d in devices:
        print(d)
    if not devices:
        print("no devices found — wake the monitor (press its power/"
              "memory button) and keep its phone-app Bluetooth off")
        return 1
    return 0


async def _cmd_connect_session(args, what: str) -> int:
    async with BpBridge() as bp:
        meter = await bp.connect(args.address)
        try:
            if args.info_first:
                info = await meter.device_info()
                for k, v in info.items():
                    print(f"{k}: {v}")
            if what == "dump":
                readings = await meter.collect(
                    quiet_seconds=args.quiet, max_seconds=args.max_seconds)
                _print_readings(readings, args.json, args.csv)
                return 0 if readings else 1
            # watch
            print("waiting for measurements (Ctrl-C to stop)...")
            n = 0
            async for r in meter.watch(intermediate=args.intermediate):
                print(r)
                n += 1
            return 0
        finally:
            try:
                await meter.disconnect()
            except Exception:
                pass


async def _cmd_info(args) -> int:
    async with BpBridge() as bp:
        meter = await bp.connect(args.address)
        try:
            print(await meter.gatt_dump())
            info = await meter.device_info()
            if info:
                print("\n-- device info --")
                for k, v in info.items():
                    print(f"{k}: {v}")
            return 0
        finally:
            try:
                await meter.disconnect()
            except Exception:
                pass


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bpbridge",
        description="Read blood pressure monitors over BLE — no vendor cloud.")
    p.add_argument("--version", action="version", version=f"bpbridge {__version__}")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="debug logging (logs every parse error)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="find blood pressure monitors")
    s.add_argument("--timeout", type=float, default=10.0)
    s.add_argument("--all", action="store_true", help="include unmatched devices")

    s = sub.add_parser("info", help="GATT dump + device info (diagnostics)")
    s.add_argument("address")

    s = sub.add_parser("dump", help="connect and collect pushed readings")
    s.add_argument("address")
    s.add_argument("--csv", metavar="FILE", help="write readings to CSV")
    s.add_argument("--json", action="store_true", help="JSON to stdout")
    s.add_argument("--quiet", type=float, default=1.5,
                   help="end session after N s without a new record (1.5)")
    s.add_argument("--max-seconds", type=float, default=25.0,
                   help="hard cap for one session (25)")
    s.add_argument("--info-first", action="store_true",
                   help="print device info before the transfer")

    s = sub.add_parser("watch", help="keep the session open for live readings")
    s.add_argument("address")
    s.add_argument("--intermediate", action="store_true",
                   help="(reserved) log cuff pressure while inflating")

    s = sub.add_parser("pair", help="bond with the device (one-time)")
    s.add_argument("address")
    s.add_argument("--renew", action="store_true",
                   help="remove the existing host-side bond first")

    s = sub.add_parser("unpair", help="remove this host's bond")
    s.add_argument("address")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s")
    try:
        if args.cmd == "scan":
            return asyncio.run(_cmd_scan(args))
        if args.cmd == "info":
            return asyncio.run(_cmd_info(args))
        if args.cmd == "dump":
            return asyncio.run(_cmd_connect_session(args, "dump"))
        if args.cmd == "watch":
            return asyncio.run(_cmd_connect_session(args, "watch"))
        if args.cmd == "pair":
            return 0 if asyncio.run(pair(args.address, renew=args.renew)) else 1
        if args.cmd == "unpair":
            asyncio.run(unpair(args.address))
            return 0
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        if args.verbose:
            raise
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
