# bpbridge

Read blood pressure monitors over Bluetooth LE — no vendor cloud, no vendor
app.  Pure Python ([`bleak`](https://github.com/hbbluetoothtools/bleak)),
mirroring the design of
[glucobridge](https://github.com/hasenbalg/gl49) (glucose meters) and
[scalebridge](https://github.com/hasenbalg/bs440) (body scales).

Primary target: **Medisana BU 572 connect** (upper-arm monitor, device
marking `TMB-2276-B`).  The backend is the *standard* Bluetooth SIG Blood
Pressure Service, so other BLP-certified monitors are expected to work
with the same code path.

## Verification status

| Device | BLE `dump` | BLE `watch` | Notes |
|---|---|---|---|
| **Medisana BU 572 connect** (TMB-2276-B) | **live-verified** — bonded session pushed 5 stored records (mmHg, ts+pulse+user+status), CSV/JSON export OK | simulated tests; same subscribe path, live run pending | **pairing required** before the push; live probe: `tools/raw_capture.py` |

The table lists only what has actually been run on hardware; anything
implemented but not yet verified on a physical meter would be listed
below it instead.

## How the transfer works

Unlike glucose meters there is no Record Access Control Point here.  The
BU 572 pushes its stored records as **BP Measurement (0x2A35) indications**
immediately after a central subscribes, then goes silent — the session is
closed after a quiet period (1.5 s default, 25 s cap, both tunable).
**The push requires a bond**: on a plain link the subscription succeeds but
nothing is ever sent — run `bpbridge pair` once (Just Works).
The meter pushes **only records not yet transferred to a central**
(live-verified): the first dump gets the stored backlog, later dumps only
whatever was measured since — so a periodic `dump` is an incremental
sync.  Records are additionally deduplicated by content, so repeated or
overlapping syncs are harmless.  The device advertises as `BU 572` (and
only while awake).

## Install

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
```

Requires BlueZ (Linux) with a Bluetooth adapter.  On a Raspberry Pi the
adapter ships soft-blocked: `rfkill unblock bluetooth` first.

## CLI

```bash
bpbridge scan                       # find monitors (wake it first!)
bpbridge info AA:BB:CC:DD:EE:FF     # GATT dump + device info (diagnostics)
bpbridge dump AA:BB:CC:DD:EE:FF --csv readings.csv   # full pushed history
bpbridge dump AA:BB:CC:DD:EE:FF --json               # JSON to stdout
bpbridge watch AA:BB:CC:DD:EE:FF    # keep the session open for live values
bpbridge settime AA:BB:CC:DD:EE:FF --tz Europe/Berlin  # write the clock
                                    # (opt-in; --at for an exact instant)
bpbridge pair AA:BB:CC:DD:EE:FF     # bond once (Just Works agent)
bpbridge unpair AA:BB:CC:DD:EE:FF   # remove this host's bond
```

Keep a phone running the Medisana/VitaDock app away while syncing: the
monitor serves exactly one central.

## Library

```python
import asyncio
from bpbridge import BpBridge

async def main():
    async with BpBridge() as bp:
        devices = await bp.scan()
        for r in await bp.dump(devices[0].address):
            print(r)                      # 2026-10-26 08:15  user 1  118/76 ...

asyncio.run(main())
```

Each `BpReading` carries: systolic / diastolic / MAP / pulse (normalised to
mmHg — a kPa-flagged record is converted), the meter's local measurement
timestamp, user (1 / 2 / guest), measurement status flags (body movement,
cuff too loose, ...), and the raw notification bytes for debugging.
`to_dict()` / `to_csv_row()` export everything.

## Design decisions

* **Time writes are explicit-only.** The BU 572 exposes a writable Current
  Time (0x2A2B) characteristic; prior art rewrites the host clock on every
  sync session.  This library NEVER writes the clock as a side effect of
  scan/dump/watch — the only way a write happens is you running
  `bpbridge settime --tz <zone>` yourself (the timezone argument is
  required so the written wall time is always deliberate).
  Records are timestamped by the meter's own RTC, so keeping it honest is
  worth a `settime` after battery changes.
* **No history protocol beyond the push.** The 199-slots-per-user memory is
  delivered via the indication push, and only records NOT yet transferred
  to a central are pushed (live-verified: backlog on first bonded dump,
  empty dumps afterwards, exactly one new record after one fresh
  measurement).  Consumed records stay visible on the device display but are never
  re-offered to the central that read them; whether the transferred-flag
  is global or per-bond is untested — if global, a VitaDock phone-app sync
  would consume records before bpbridge sees them, so keep one designated
  sync host and keep phone Bluetooth off around it.
* **Vendor cloud untouched.** Nothing here talks to Medisana/VitaDock.

## Development

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest tests/ -q
.venv/bin/ruff check src tests tools examples
```

The test suite carries spec-derived golden record vectors (mmHg, kPa,
status, guest, broken-date, truncations) and a simulated meter exercising
the full session/dedupe/watch flows — no hardware needed.
`tools/raw_capture.py` is the live reverse-engineering probe: it dumps the
GATT tree, reads every readable characteristic, and prints raw hex of
every notification until Ctrl-C.

Protocol notes and prior art: [FINDINGS.md](FINDINGS.md).
Runnable API examples: [`examples/`](examples/).

## Prior art / credits

This project is an independent implementation (own code throughout).  The
BLE behaviour of the BU 572 was first documented publicly by:

* [jagula/home-assistant-medisana-bu572](https://github.com/jagula/home-assistant-medisana-bu572)
  (MIT) — first public Home Assistant integration for this exact model;
  its README/coordinator documented the characteristic set, the push
  session behaviour, the kPa flag, the user-ID mapping and the
  advertisement name, which guided this implementation.
* [bkbilly/medisanabp_ble](https://github.com/bkbilly/medisanabp_ble) — HA
  integration for other Medisana BP models (BU 530/570), same service.
* [HA community: ESPHome tracker for BU 570/575/584](https://community.home-assistant.io/t/esphome-bluetooth-tracker-for-medisana-bu-570-575-584-blood-pressure-monitor/967208)
  — early packet notes.
* Bluetooth SIG Blood Pressure Service specification for the record layout.

Thanks to all of them — go star their repos.
