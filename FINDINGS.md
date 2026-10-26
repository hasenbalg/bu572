# FINDINGS — Medisana BU 572 connect (TMB-2276-B) over BLE

Protocol notes for the `bps` backend.  Status: **implemented from prior
art, live verification pending** — everything below marked *(prior art)*
was established by the community projects credited at the bottom and is
encoded in the parser/simulator tests; mark *(live)* items as they are
confirmed on Frank's hardware.

## Device identity

* Advertises name `BU 572` *(prior art: jagula config_flow matches the
  exact string)*.
* Device Info (expected, pending live read): manufacturer `medisana`,
  model marking `TMB-2276-B`.
* Pairs via Just Works (no numeric display/keypad); bonding is optional
  for the BP push but required for the writable Current Time characteristic
  *(prior art)*.

## GATT surface *(prior art)*

| Function | UUID |
|---|---|
| Blood Pressure Service | `0x1810` |
| Blood Pressure Measurement (indicate) | `0x2A35` |
| Battery Level (read) | `0x2A19` |
| Current Time Service / Time (read, **write on purpose unused**) | `0x1805` / `0x2A2B` |

### Live GATT capture (2026-10-26, unit `B8:B7:7D:23:38:F6`) *(live)*

* Advertises `BU 572`, classified and connected fine via the standard path.
* Device Info (live reads): manufacturer **`LifeSense`** (the ODM behind
  the Medisana connect line, not "medisana"), model `BU 572`, firmware
  `1.2`, hardware `7600`, software `BLP.0` (Blood Pressure Profile),
  serial `000000`, IEEE cert data = station address reversed
  (`ff ee dd cc bb aa`). `0x2A50` reads `02 5e 04 40 00 00 03`
  (raw, meaning unknown).
* BP Service 0x1810 carries only **2A49 (read)** and **2A35 (indicate)** —
  there is NO 0x2A36 Intermediate Cuff Pressure on this model, so live cuff
  pressure is not available; 2A49 (cuff position per SIG) is readable.
* Battery 0x2A19 is read **and notify**; Current Time 0x2A2B is
  read/write/notify (write still deliberately unused).
* Device Info exposes the full 2A23..2A2A + 0x2A50 set.
* **Vendor service `f000ffc0-0451-4000-b000-000000000000`** with chars
  `ffc1`/`ffc2` (write + notify): the VitaDock app channel. This
  f000ffc0-0451-4000 platform appears across Medisana/Beurer/Sanitas
  devices (ble-scale-sync's Beurer/Sanitas adapter keys on it; the
  companion channel on scales uses `E7`-framed commands). Kept OUT of
  bpbridge's standard path — see "Vendor channel" below.
* **Plain (unpaired) connection: subscribing to 2A35 produced no push**
  within the session window. **CONFIRMED LIVE:** after `bpbridge pair`
  (Just Works), the very next `dump` received the device's stored records
  — bonding is a hard precondition for the indication push.
* First live dump (same day, right after bonding): 5 records pushed in one
  session, **mmHg** values (flag unit bit clear) carrying timestamp, pulse,
  user (=1) and status (=0); oldest record ~5 weeks back, so the push
  includes stored history, not just the newest.
* Device-local timestamps behave as Berlin wall clock (device shows local
  time; stored naive by design).
* **Transfer semantics (live, same day): the meter pushes only records not
  yet transferred to a central.** Two immediate re-dumps returned 0
  records; after one fresh measurement the next dump returned exactly 1.
  Corollaries: (a) periodic dump = incremental sync (cron-friendly),
  (b) the FIRST central to connect after measurements consumes them — a
  VitaDock phone app sync would steal records from bpbridge's view (and
  vice versa), so pick one designated sync host,
  (c) historical deep-backlog behaviour (all 199 slots?) untested — the
  first dump delivered the backlog that existed.

## Vendor channel (f000ffc0-0451-4000-b000-000000000000)

Exists but unexplored. Candidates for what it adds over the standard
push: full 199-slot-per-user history export, user profile sync, device
settings. bpbridge stays on the certified path; if the standard push turns
out to only ever deliver new records, this service is where the RE work
would continue (read-only observation via `tools/raw_capture.py` first).
Vendor clouds / VitaDock REST remain out of scope entirely.

## Transfer model *(prior art)*

1. Central connects (plain connection suffices for the push).
2. Central subscribes to 0x2A35 indications.
3. Device pushes stored records, one indication per record, starting
   within seconds; ends silently.  Tuning values carried over: first
   packet within ~6 s, session quiet-timeout 1.5 s, hard cap 25 s.
4. Freshly taken measurements are indicated while the central stays
   connected (and pushed on the next session otherwise).

Dedup key used by bpbridge: (timestamp, sys, dia, map, pulse, user_raw,
status) — same field tuple prior art uses, content-addressed so repeat
syncs are idempotent.

**Open question for the live run:** does the meter re-push the complete
memory on every connect, or only records not yet marked transferred by a
previous central (the VitaDock app or an HA sync could have "consumed"
them)?  Test: `dump` twice; then take a new measurement and `dump` again.

## Record layout (0x2A35, Bluetooth SIG BLP spec)

```
flags u8:  bit0 unit (0=mmHg, 1=kPa)   bit1 timestamp   bit2 pulse
           bit3 user id                bit4 status
sys,dia,map  3 × SFLOAT16 LE
[timestamp]  u16 LE year, u8 month, day, hour, min, sec  (device-local)
[pulse]      SFLOAT16 LE
[user]       u8   0 → user 1, 1 → user 2, 0xFF → guest mode
[status]     u16 LE  (lenient: some firmwares send 1 byte)
```

* The **kPa flag matters**: the BU 572 may deliver pressures in kPa
  (flag bit0), requiring ×7.50061683 → mmHg *(prior art, encoded in
  `test_records.py`)*.
* MAP is taken from the device, never recomputed *(prior art)*.
* Measurement status bits: 0 body movement, 1 cuff too loose,
  2 speech detected.  The BU 572 also markets an "irregular heartbeat"
  indicator — where that bit lives is unconfirmed; keep `status` raw in
  JSON export until seen live.
* Guest mode: user raw `0xFF` → `BpReading.guest == True` *(inference from
  the marketing "guest mode" feature + SIG 0xFF semantics; confirm live)*.

## Time sync (opt-in only)

* Current Time (0x2A2B) is read/write/notify *(live)*.  Prior art rewrites
  the host clock on every session; bpbridge writes ONLY via the explicit
  `settime` command (SIG layout: Date Time LE + Day of Week + Fractions256
  + Adjust Reason 1 = ODP field update; 10 bytes; encrypted, i.e. bonded —
  write on a plain link fails, prior art pairs then retries).
* Live status of the write path: pending hardware confirmation.

## Prior art / sources

* https://github.com/jagula/home-assistant-medisana-bu572 — first public
  BU 572 integration (MIT); the primary factual source for this file.
  All code in this repo is written independently; protocol *facts* were
  learned from its README and behaviour.
* https://github.com/bkbilly/medisanabp_ble — sibling Medisana BP models.
* https://community.home-assistant.io/t/esphome-bluetooth-tracker-for-medisana-bu-570-575-584-blood-pressure-monitor/967208
* Bluetooth SIG: Blood Pressure Service specification (record layout,
  SFLOAT16, Measurement Status values).
