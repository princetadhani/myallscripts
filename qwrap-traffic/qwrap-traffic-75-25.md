# qwrap-traffic-75-25.py — Reference Guide

Plain-language reference for what this script does, how to configure it, and
what each toggle/flag means. Written so you (or anyone else) can pick this up
cold after months and understand it without re-reading the code.

## 1. What this script does

It pushes **simulated QWRAP Wi-Fi client traffic** config to one or more Qwrap APs'
WifiAgent, over HTTP. For each AP, it creates a bunch of **virtual clients**
(veth interfaces) and tells each one to run a mix of traffic:

- **Browsing** — a couple of HTTPS GET requests to random public websites
  (simulates someone casually browsing).
- **ClientOp** — QUIC/TCP sessions to internal discovery-server endpoints
  (simulates heavier app/streaming-like traffic).
- **FileOp** — SFTP/FTP/TFTP upload/download sessions (simulates file
  transfers).
- **Heartbeat** (optional) — a tiny, low-priority HTTP GET during idle hours,
  just so an "idle" client doesn't go 100% silent (see section 4).

Not all clients run traffic all the time — only a configured percentage are
"active" at any given hour (see section 3), to mimic real-world usage where
most devices are idle most of the time and only ~10-15% are actively doing
something at any moment.

## 2. How to run it

```bash
python3 qwrap-traffic-75-25.py                      # push to every AP in AP_CONFIG
python3 qwrap-traffic-75-25.py --ap 10.86.205.223    # push to one AP only
python3 qwrap-traffic-75-25.py --ap 10.86.205.223,10.86.204.227   # multiple APs
python3 qwrap-traffic-75-25.py --dry-run             # build payloads, write to JSON, don't push
python3 qwrap-traffic-75-25.py --dry-run --out foo.json
python3 qwrap-traffic-75-25.py --debug               # verbose logging
```

`--dry-run` is the safe way to preview exactly what would be sent (session
counts, schedules) without touching any real AP.

## 3. Configuring an AP — `AP_CONFIG`

Each AP is one entry in the `AP_CONFIG` dict near the top of the file. There's
a blank copy/paste skeleton right above it in the code comments. Example:

```python
"10.86.205.223": {
    "idle_percent": 70,
    "bands": {
        0: {"veth_count": 28, "fileop_count": 3, "clientop_count": 3, "ip_mode": "IPv4", "target_type": "hostname"},
        1: {"veth_count": 28, "fileop_count": 3, "clientop_count": 3, "ip_mode": "IPv4", "target_type": "hostname"},
        2: {"veth_count": 28, "fileop_count": 3, "clientop_count": 3, "ip_mode": "IPv4", "target_type": "hostname"},
    },
},
```

| Field            | Meaning                                                                                                                                                                    |
| ---------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `idle_percent`   | % of this AP's virtual clients that stay idle at any given hour. Optional — falls back to global `IDLE_PERCENT` if omitted.                                                |
| `bands`          | One entry per radio: `0` = 2.4GHz, `1` = 5GHz, `2` = 6GHz. Skip a radio key entirely to not configure that radio.                                                          |
| `veth_count`     | How many virtual client interfaces to create on this radio (max 28).                                                                                                       |
| `fileop_count`   | How many FileOp (SFTP/FTP/TFTP) sessions each **active** client on this radio runs.                                                                                        |
| `clientop_count` | How many ClientOp (QUIC/TCP) sessions each **active** client on this radio runs (in addition to the fixed browsing sessions — see `BROWSING_SESSIONS_PER_CLIENT` below).   |
| `ip_mode`        | `"IPv4"`, `"IPv6"`, or `"Dual"` (randomly picks one per session if Dual).                                                                                                  |
| `target_type`    | `"hostname"` or `"ip"` — which discovery-endpoint pool to use.                                                                                                             |
| `schedule_group` | Optional int. Which active-hour rotation starting-point this AP uses — see section 4a below. If omitted, auto-derived from the AP's own IP so every AP differs by default. |

## 3a. Making different APs have different schedules — `schedule_group`

**The problem this solves:** by default, if two APs have the exact same number
of clients (e.g. both have 84 veth interfaces), client #1 on AP-1 and client #1
on AP-2 would end up with the **exact same active-hour schedule** — same hours
active on the same days — because the rotation math only looks at the client's
position (index) and the day/hour, not which AP it belongs to. Nothing
previously made one AP's rotation different from another AP's rotation.

**The fix — `schedule_group` (int, optional, per-AP):**

Think of `schedule_group` as the **starting point** for that AP's rotation
calculation — like dealing a deck of cards starting from a different spot in
the deck:

- APs with the **same** `schedule_group` value → **identical** active-hour
  schedules for their same-index clients (useful if you deliberately want two
  APs to mirror each other for a test).
- APs with **different** `schedule_group` values → **different** schedules.

Mechanically, it works exactly like the day-of-week shift (`DAY_PHASE_STRIDE`)
described above, just keyed by AP instead of by day — it's one more
phase-shift added into the same rotation formula:

```python
ap_phase = (schedule_group * AP_PHASE_STRIDE) % n_clients
start = (ap_phase + day_phase + hour_idx * active_count) % n_clients
```

**Default behavior (important):** if you don't set `schedule_group` for an AP,
it does **not** default to `0` for everyone (which would silently bring back
the "all APs look the same" problem for anyone who forgets to set it).
Instead, it's auto-derived from a stable hash of the AP's own IP address. So:

- **Omit the key entirely** → every AP is automatically different out of the
  box. This is the fix for the "all clients across APs look identical"
  problem, with zero config needed.
- **Set `schedule_group` explicitly** → you get manual control. E.g. put
  `"schedule_group": 1` on 5 APs and `"schedule_group": 2` on 3 other APs —
  the 5 will all schedule identically to each other, the 3 will schedule
  identically to each other, and the two groups will differ from one another.

Example:

```python
"10.86.205.165": {
    "idle_percent": 75,
    "schedule_group": 1,     # <- optional; shares schedule with any other AP using group 1
    "bands": { ... },
},
"10.86.205.223": {
    "idle_percent": 70,
    # no schedule_group set -> auto-derived from this AP's own IP, guaranteed
    # different from 10.86.205.165 (and from any other AP) by default
    "bands": { ... },
},
```

The logs print the effective `schedule_group` for every AP on every run (and
note whether it came from your config or was auto-derived), so you can always
confirm which APs are grouped together.

## 4. Global constants (the "knobs")

All near the top of the file, just below `AP_CONFIG`:

### `IDLE_PERCENT = 25`

Default idle percentage used for any AP that doesn't set its own
`idle_percent`. E.g. `25` means 25% of clients are idle, 75% active, at any
given hour.

### `BROWSING_SESSIONS_PER_CLIENT = 2`

Fixed number of HTTPS "browsing" sessions every **active** client gets, on
top of its `clientop_count`. This is a global constant, not configurable
per-AP/per-band (unlike `fileop_count`/`clientop_count`).

### `SEND_HEARTBEAT = 1`

Controls whether idle clients are **completely silent** or send a tiny
keep-alive request during their idle hours.

- **`SEND_HEARTBEAT = 1` (heartbeat ON):** During its idle hours, a client
  still sends one small, low-priority HTTP GET every 10-20 minutes. This
  keeps the client looking "alive and present" on the AP (like a real idle
  phone still doing background checks — notifications, keepalives, etc.)
  instead of looking like a dead/disconnected device.
- **`SEND_HEARTBEAT = 0` (heartbeat OFF):** During idle hours, the client
  sends **zero traffic at all** — fully silent, as if the device weren't
  doing anything in the background. This was the original behavior before
  the heartbeat feature was added.

### `SEND_DAYWISE_VARIATION = 1`

Controls whether each client's active/idle hours are the **same every day of
the week**, or **different each day**.

- **`SEND_DAYWISE_VARIATION = 0` (OFF — "weekly recurring" schedule):**
  Imagine client #5 is scheduled active at 1pm and 4pm. With this OFF, it
  will be active at 1pm and 4pm on **every single day** — Monday, Tuesday,
  Wednesday... all identical. If you opened this client's schedule in the UI,
  you'd see the exact same checkboxes ticked on every row (Mon through Sun).
  This is a classic "recurring weekly schedule" — set once, repeats forever,
  same pattern every day.
- **`SEND_DAYWISE_VARIATION = 1` (ON — "day-of-week variation"):** Client #5
  might be active at 1pm/4pm on Monday, but 2am/6am on Tuesday, and something
  else again on Wednesday. Each day of the week gets a different active-hour
  pattern for the same client. This is more realistic — real people don't use
  their devices at the exact same minute every single day.

**In both cases**, the overall ratio is preserved: at any given hour, on any
given day, `idle_percent`% of clients are idle and the rest are active — only
_which_ clients fill that active slot changes.

### `HOURS_BASE = list(range(0, 24))`

The hours of the day across which the active/idle rotation happens.

- `list(range(9, 21))` → 9 AM to 9 PM (12-hour daytime window).
- `list(range(0, 24))` → all 24 hours (full day, used currently).

Change this single line to switch between a daytime-only run and a full
24-hour run.

## 5. How the active/idle rotation actually works (plain English)

Think of it like a rotating schedule, not a fixed split:

1. At any single hour, only `idle_percent`% of clients are idle — the rest
   are "active" (running full browsing+clientop+fileop traffic).
2. **Which** clients are in the active group **changes every hour** — it
   rotates round-robin so that over the course of the day, every client gets
   several turns being "active," not just one fixed subset of clients forever.
3. If `SEND_DAYWISE_VARIATION = 1`, the starting point of that rotation also
   shifts for each day of the week, so the same client's active hours look
   different on Monday vs Tuesday vs Wednesday, etc.
4. If a client never lands in the active group on any day (possible with a
   very high `idle_percent` and few hours), it's counted as "uncovered" and
   you'll see a warning in the logs telling you to lower `idle_percent` or
   reduce `veth_count`.

## 6. Reading the logs

Each AP run prints, in order:

1. How many total clients, how many active/hour, and the band config summary.
2. A warning if any clients got zero active hours this run (rotation coverage
   issue).
3. The current `BROWSING_SESSIONS_PER_CLIENT`, `SEND_HEARTBEAT`, and
   `SEND_DAYWISE_VARIATION` values (so you always know what mode you ran in).
4. `fileop -> ok (N sessions) [radio breakdown]` — e.g.
   `2.4G=28x3=84, 5G=28x3=84, 6G=28x3=84 (total=252)`
   meaning: 28 active clients × 3 fileop sessions each = 84, per radio.
5. `client -> ok (N sessions) [radio breakdown]` — same idea, but broken into
   `browsing(...)+clientop(...)+heartbeat(...)` per radio since "client"
   sessions are actually three different kinds combined.

## 7. Quick FAQ

**Q: Why are fileop and client session counts sometimes the same number?**
Coincidence of your config values — `fileop_count` just happens to equal
`BROWSING_SESSIONS_PER_CLIENT + clientop_count + heartbeat(1)`. They're
computed completely independently.

**Q: I don't want heartbeat traffic at all, how do I disable it?**
Set `SEND_HEARTBEAT = 0`.

**Q: I want every client to repeat the exact same hours every day (simpler,
less realistic) instead of varying by weekday?**
Set `SEND_DAYWISE_VARIATION = 0`.

**Q: How do I make this run for the full day instead of just daytime?**
Change `HOURS_BASE = list(range(9, 21))` to `list(range(0, 24))` (or vice
versa).

**Q: Why do client #1 on AP-1 and client #1 on AP-2 have the exact same
active-hour schedule?**
Set `schedule_group` to a different value on each AP (or just leave it unset
— it auto-derives a different value per AP from the AP's IP). See section 3a.

**Q: I want two specific APs to have identical schedules on purpose (e.g. for
a side-by-side test)?**
Give both of them the same explicit `schedule_group` value.

**Q: Where do I add a new AP?**
Copy the blank skeleton comment block above `AP_CONFIG`, paste it in, fill in
the IP and values.
