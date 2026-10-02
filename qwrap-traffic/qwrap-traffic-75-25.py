#!/usr/bin/env python3
"""
Reduced-load traffic configuration for WifiAgent Qwrap APs.

Per active (non-idle) virtual-client interface:
  - BROWSING_SESSIONS_PER_CLIENT HTTPS GET sessions to random public websites
    (lightweight browsing simulation; count set via BROWSING_SESSIONS_PER_CLIENT)
  - clientop_count ClientOp sessions : randomly QUICT or TCPT (from discovery endpoints)
  - fileop_count   FileOp sessions   : randomly sftp / ftp / tftp (from discovery endpoints)

Additional features vs original:
  - Configurable active/idle split via per-AP "idle_percent" in AP_CONFIG
    (falls back to the global IDLE_PERCENT constant if omitted)
  - Per-AP, per-radio config dict (AP_CONFIG) specifying veth_count, fileop_count,
    clientop_count, ip_mode (IPv4/IPv6/Dual) and target_type (hostname/ip)
  - Dual-stack endpoint pools (v4 + v6) built from discovery API
  - Time-based scheduling support (weekdays_schedule per session)
  - 75/25 download/upload weighting on all fileop and clientop sessions
  - Hourly active/idle rotation (see HOURS_BASE below) so every client gets a
    turn running full traffic, with an optional idle-hour heartbeat (see
    SEND_HEARTBEAT below) instead of going completely silent during its idle
    hours.

Why the idle-hour heartbeat (SEND_HEARTBEAT) exists:
  Without it, a client is completely silent during its idle hours — no
  traffic at all (this was the behavior before the per-hour idle heartbeat
  was added). A client's veth/virtual interface is a simulated Wi-Fi client
  association on the AP; if it sends zero traffic for its idle hours, from
  the AP's/controller's perspective it looks like a client that's associated
  but completely inactive — which is unrealistic for most real-world
  traffic-simulation scenarios (real idle devices still do background
  checks: DNS lookups, keepalives, notification polling, OS/app background
  sync, captive-portal checks, etc.).
  The heartbeat (build_idle_sessions — one lightweight HTTP GET
  /wifiagent/dynamic, datasize=1, long interval of 600/900/1200s, lowest DSCP
  priority) exists to:
    - Keep the client "visibly alive" on the AP (still generating minimal
      periodic activity) instead of going completely silent — more realistic
      idle-device emulation.
    - Prevent the AP/controller from potentially aging out, disassociating,
      or flagging the client as inactive/dead due to total silence over a
      multi-hour idle window.
    - Avoid skewing traffic-pattern statistics on the AP side where a
      "fully silent for hours" client might look anomalous compared to a
      genuinely idle-but-present device.
  It's intentionally tiny and low-priority (dscpvalue=0, 10-20 minute
  intervals) so it doesn't meaningfully add load or count as "real" traffic —
  it's just a presence signal, which is why it's tracked separately in
  session counts rather than folded into the active browsing/clientop
  numbers. Set SEND_HEARTBEAT = 0 to disable it entirely and go back to
  fully silent idle hours.

Usage:
  python3 qwrap-traffic-75-25.py [--dry-run] [--ap HOST[,HOST...]] [--debug]
"""

import argparse
import hashlib
import json
import logging
import os
import random
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import urllib3
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- AP inventory -------------------------------------------------------------
# 
# Per-AP, per-band configuration.
# Each radio entry fully controls its own veth count + session counts + IP settings.
# 
#   radio 0 -> 2.4 GHz   radio 1 -> 5 GHz   radio 2 -> 6 GHz
#   veth_count     : how many veth_in_<radio>_* interfaces to configure (1..28, clamped to MAX_VETH_PER_RADIO)
#   fileop_count   : number of FileOp sessions per active (non-idle) veth interface
#   clientop_count : number of ClientOp (QUICT/TCPT) sessions per active veth interface (in addition to the 2 fixed HTTPS browsing sessions)
#   ip_mode        : "IPv4" | "IPv6" | "Dual"
#   target_type    : "hostname" | "ip"
#
# "idle_percent": , (optional, per-AP): % of this AP's virtual clients that stay
# fully idle. Falls back to IDLE_PERCENT (below) if omitted.
#
# "schedule_group": , (optional, per-AP, int): which active-hour rotation
# starting-point this AP uses. APs sharing the same schedule_group get
# IDENTICAL active-hour schedules for their same-index clients; APs with
# different schedule_group values get different schedules. If omitted, it's
# auto-derived from the AP's own IP, so every AP differs by default — set it
# explicitly only if you want specific APs to mirror (or deliberately differ
# from) each other.
#
# ─── Blank skeleton — copy/paste this per AP and fill in the values ─────
# Add/remove radio lines (0/1/2) as needed; an omitted radio is skipped.
# Copy from here and paste it:
#
#     "ap_ip": {
#         "idle_percent": ,
#         "schedule_group": ,
#         "bands": {
#             0: {"veth_count": , "fileop_count": , "clientop_count": , "ip_mode": "", "target_type": ""},
#             1: {"veth_count": , "fileop_count": , "clientop_count": , "ip_mode": "", "target_type": ""},
#             2: {"veth_count": , "fileop_count": , "clientop_count": , "ip_mode": "", "target_type": ""},
#         },
#     },
#
AP_CONFIG: dict[str, dict] = {
    "10.86.205.165": {
        "idle_percent": 75,
        "bands": {
            0: {"veth_count": 28, "fileop_count": 2, "clientop_count": 2, "ip_mode": "IPv4", "target_type": "ip"},
            1: {"veth_count": 28, "fileop_count": 2, "clientop_count": 2, "ip_mode": "IPv4", "target_type": "hostname"},
            2: {"veth_count": 28, "fileop_count": 2, "clientop_count": 2, "ip_mode": "IPv4", "target_type": "ip"},
        },
    },
     "10.86.205.223": {
        "idle_percent": 70,
        "bands": {
            0: {"veth_count": 28, "fileop_count": 4, "clientop_count": 4, "ip_mode": "IPv4", "target_type": "hostname"},
            1: {"veth_count": 28, "fileop_count": 4, "clientop_count": 4, "ip_mode": "IPv4", "target_type": "hostname"},
            2: {"veth_count": 28, "fileop_count": 4, "clientop_count": 4, "ip_mode": "IPv4", "target_type": "hostname"},
        },
    },
    "10.86.204.227": {
        "idle_percent": 70,
        "bands": {
            0: {"veth_count": 28, "fileop_count": 4, "clientop_count": 4, "ip_mode": "IPv4", "target_type": "ip"},
            1: {"veth_count": 28, "fileop_count": 4, "clientop_count": 4, "ip_mode": "IPv4", "target_type": "hostname"},
            2: {"veth_count": 28, "fileop_count": 4, "clientop_count": 4, "ip_mode": "IPv4", "target_type": "ip"},
        },
    },   
    "10.87.1.23": {
        "idle_percent": 70,
        "bands": {
            0: {"veth_count": 28, "fileop_count": 4, "clientop_count": 4, "ip_mode": "IPv4", "target_type": "ip"},
            1: {"veth_count": 28, "fileop_count": 4, "clientop_count": 4, "ip_mode": "IPv4", "target_type": "hostname"},
            2: {"veth_count": 28, "fileop_count": 4, "clientop_count": 4, "ip_mode": "IPv4", "target_type": "ip"},
        },
    },   
}

MAX_VETH_PER_RADIO = 28

DISCOVERY_URLS: dict[str, str] = {
    "10.87": "http://pune-abz-traffic-enpoint.dt1.wifi.arista.cloud/api/discovery",
    "10.86": "http://pune-traffic-enpoint.dt1.wifi.arista.cloud/api/discovery",
    "10.85": "http://blr-traffic-endpoint.dt1.wifi.arista.cloud/api/discovery",
    "10.81": "http://hq-traffic-endpoint.dt1.wifi.arista.cloud/api/discovery",
    "10.76": "http://hq-traffic-endpoint.dt1.wifi.arista.cloud/api/discovery",
}

WIFIAGENT_PORT = 8083
TIMEOUT = 60

# --- Virtual-client selection -------------------------------------------------
# Per-AP veth/session counts now come from AP_CONFIG (see "bands" above) —
# no global RADIO_*_CLIENTS constants needed.

# Default percentage of virtual clients that are idle at any time (0-100),
# used for any AP whose AP_CONFIG entry doesn't set its own "idle_percent".
# The remainder (100 - idle_percent) are active. Idle clients are spread
# evenly across the client list (e.g. idle_percent=25 idles every 4th client).
IDLE_PERCENT = 25

# Number of HTTPS GET "browsing" sessions built per active (non-idle) client.
BROWSING_SESSIONS_PER_CLIENT = 2

# Whether idle clients send a lightweight heartbeat GET during their idle
# hours (see module docstring above for rationale).
#   1 -> idle hours get a small HTTP GET heartbeat (build_idle_sessions)
#   0 -> idle hours are completely silent (no heartbeat, no traffic at all)
SEND_HEARTBEAT = 1

# Whether each client's active/idle hours differ from one day of the week to
# the next, on top of the existing hourly rotation:
#   1 -> each client gets a DIFFERENT active-hour pattern per weekday (e.g.
#        client-5 active at 1pm/4pm on Monday, but 2am/6am on Tuesday) while
#        still keeping idle_percent% of clients idle at any given hour.
#   0 -> each client repeats the SAME active-hour pattern every day of the
#        week (classic weekly-recurring schedule — Mon through Sun identical
#        for a given client); only the hour-of-day rotation applies.
SEND_DAYWISE_VARIATION = 0

# Whether log lines are colored per-AP in the terminal (see _AP_COLOR_PALETTE
# below for how colors are assigned):
#   1 -> colorize log output (still auto-disabled when stdout isn't a TTY,
#        e.g. piped to a file, or when NO_COLOR env var is set)
#   0 -> never colorize, even on a terminal (plain text always)
COLOR_LOGS = 1

# --- Traffic pools ------------------------------------------------------------

DSCP_VALUES  = [0, 10, 26, 34, 46]
PACKET_SIZES = [64, 128, 256, 512, 1024, 1280, 1400, 1500, 2048, 4096, 9000]
FILE_SIZES   = [25, 50, 75, 100, 125]
DATA_SIZES   = [25, 50, 75, 100, 125]
FILE_IVALS   = [300, 450, 600, 900]
CLIENT_IVALS = [180, 300, 450, 600, 750]
CONN_IVALS   = [0, 30, 60, 120, 300]
BROWSE_IVALS = [300, 450, 600, 900]

# Daytime active hours (9 AM - 9 PM). The idle_percent split is applied PER HOUR
# and rotated round-robin across clients AND across days of the week (see
# _client_hour_schedule below), so at any single hour idle_percent% of clients
# are idle while the rest run full traffic — and which specific clients are
# idle/active changes every hour AND differs from one weekday to the next, so
# every client gets a turn at full traffic over the course of the run.
HOURS_BASE = list(range(0, 24))  # active window = 9AM-9PM; for 24hr run, use list(range(0, 24))

BROWSING_SITES = [
    "https://fortune.com", "https://azure.microsoft.com/en-in", "https://arista.com",
    "https://www.nytimes.com/", "https://microsoft.com", "https://slack.com/intl/en-in/",
    "https://www.teamviewer.com/en-in/", "https://washington.edu",
    "https://www.goto.com/meeting", "https://indiatimes.com",
    "https://samsung.com", "https://www.apple.com/in/", "https://wikihow.com",
    "https://teams.live.com/free", "https://www.zoom.com/",
    "https://moneycontrol.com", "https://workspace.google.com/products/meet/",
    "https://www.theguardian.com/international", "https://www.dropbox.com/",
    "https://cloud.google.com/apis", "https://hollywoodreporter.com",
    "https://cornell.edu", "https://cnbc.com", "https://oracle.com",
    "https://facebook.com", "https://paypal.com", "https://github.com",
    "https://news.google.com", "https://forbes.com", "https://flipkart.com",
    "https://amazon.com", "https://newsweek.com", "https://usatoday.com",
    "https://abcnews.go.com", "https://cbsnews.com", "https://zdnet.com",
    "https://hotstar.com", "https://primevideo.com", "https://cnet.com",
    "https://whatsapp.com", "https://adobe.com", "https://shopify.com",
    "https://mit.edu", "https://reddit.com", "https://medium.com",
    "https://cnn.com", "https://claude.ai/", "https://salesforce.com",
    "https://servicenow.com", "https://workday.com", "https://hubspot.com",
    "https://zendesk.com", "https://sap.com", "https://jira.atlassian.com",
    "https://figma.com", "https://notion.so", "https://asana.com",
    "https://miro.com", "https://monday.com", "https://box.com",
    "https://webex.com", "https://okta.com", "https://aws.amazon.com",
    "https://cloud.google.com", "https://cloudflare.com", "https://datadoghq.com",
    "https://splunk.com", "https://gitlab.com", "https://stackoverflow.com",
    "https://docker.com", "https://npmjs.com", "https://chatgpt.com",
    "https://gemini.google.com", "https://perplexity.ai",
]
###############################################################################################################################################################################################
###############################################################################################################################################################################################
###############################################################################################################################################################################################
# --- Logging --------------------------------------------------------------------
#
# Per-AP log colouring: every log line that starts with an AP IP prefix (e.g.
# "[10.86.205.165] ...") gets coloured with a colour assigned to that IP, so
# interleaved output from multiple APs (concurrent push) is easy to tell apart
# at a glance. Colours are a fixed 20-entry palette (chosen to read clearly on
# a dark-mode terminal background) assigned in AP_CONFIG order, so the same AP
# always gets the same colour across runs. Falls back to plain/uncoloured
# output automatically when stdout isn't a terminal (e.g. piped to a file),
# when NO_COLOR is set, or when COLOR_LOGS = 0 (see global toggles above).

_LOG_COLOR_ENABLED = COLOR_LOGS and sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

# 256-color ANSI codes — bright/saturated hues that stay readable on a dark
# terminal background. Supports up to 20 APs before colours repeat.
_AP_COLOR_PALETTE = [
    "\033[38;5;39m",   # blue
    "\033[38;5;208m",  # orange
    "\033[38;5;82m",   # green
    "\033[38;5;213m",  # pink
    "\033[38;5;226m",  # yellow
    "\033[38;5;51m",   # cyan
    "\033[38;5;203m",  # salmon/red
    "\033[38;5;141m",  # purple
    "\033[38;5;214m",  # amber
    "\033[38;5;120m",  # light green
    "\033[38;5;75m",   # light blue
    "\033[38;5;219m",  # light pink
    "\033[38;5;190m",  # lime
    "\033[38;5;201m",  # magenta
    "\033[38;5;87m",   # aqua
    "\033[38;5;215m",  # peach
    "\033[38;5;159m",  # pale cyan
    "\033[38;5;183m",  # lavender
    "\033[38;5;228m",  # pale yellow
    "\033[38;5;111m",  # periwinkle
]
_LOG_RESET = "\033[0m"

# Assigned in AP_CONFIG's (insertion-ordered) key order, so a given AP keeps
# the same colour across runs regardless of which APs are targeted via --ap.
_AP_LOG_COLORS: dict[str, str] = {
    ip: _AP_COLOR_PALETTE[i % len(_AP_COLOR_PALETTE)]
    for i, ip in enumerate(AP_CONFIG)
}

_IP_BRACKET_RE = re.compile(r"\[(\d{1,3}(?:\.\d{1,3}){3})\]")


class _PerApColorFormatter(logging.Formatter):
    """Colours a log line by the AP IP found in its leading "[ip]" prefix (if
    any). Lines with no recognizable AP IP (general/global log lines) are left
    uncoloured. No-ops entirely when _LOG_COLOR_ENABLED is False."""

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        if not _LOG_COLOR_ENABLED:
            return line
        match = _IP_BRACKET_RE.search(record.getMessage())
        color = _AP_LOG_COLORS.get(match.group(1)) if match else None
        return f"{color}{line}{_LOG_RESET}" if color else line


_log_handler = logging.StreamHandler()
_log_handler.setFormatter(_PerApColorFormatter(
    fmt="%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S"))
logging.basicConfig(level=logging.INFO, handlers=[_log_handler])
log = logging.getLogger("traffic_config")

# --- HTTP session (shared) ----------------------------------------------------

def _make_http_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
    s.mount("http://",  HTTPAdapter(max_retries=retry))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s

_HTTP = _make_http_session()


def _api_post(ap_ip: str, path: str, body: dict) -> tuple[bool, str]:
    url = f"http://{ap_ip}:{WIFIAGENT_PORT}{path}"
    try:
        r = _HTTP.post(url, json=body, timeout=TIMEOUT)
    except Exception as exc:
        return False, f"request error: {exc}"
    text = (r.text or "").strip()
    if 200 <= r.status_code < 300 and not text:
        return True, f"HTTP {r.status_code} (empty body)"
    try:
        data = r.json()
    except ValueError:
        return 200 <= r.status_code < 300, f"HTTP {r.status_code}: {text[:300]!r}"
    if data.get("status") == "success":
        return True, "success"
    if 200 <= r.status_code < 300 and "status" not in data:
        return True, f"HTTP {r.status_code} (no status field)"
    return False, f"HTTP {r.status_code}: {data.get('message') or repr(data)}"


# --- Discovery ----------------------------------------------------------------

def _discovery_url_for(ap_ip: str) -> str | None:
    for prefix, url in DISCOVERY_URLS.items():
        if ap_ip.startswith(prefix + "."):
            return url
    return None


def fetch_endpoints(discovery_url: str) -> dict:
    """
    Returns dual-stack endpoint pool:
      { "v4": { "hostname": { "ftp":[], "sftp":[], "tftp":[], "quic":[], "tcp":[] },
                "ip":       { ... } },
        "v6": { "hostname": { ... }, "ip": { ... } } }
    """
    log.info("Fetching endpoints from %s", discovery_url)
    try:
        r = _HTTP.get(discovery_url, timeout=TIMEOUT, verify=False)
        r.raise_for_status()
        raw = r.json()
    except Exception as exc:
        log.error("Discovery failed for %s: %s", discovery_url, exc)
        sys.exit(1)

    containers = raw.get("containers", []) if isinstance(raw, dict) else []
    if not containers:
        log.error("Discovery response missing 'containers' array")
        sys.exit(1)

    _protos = ["ftp", "sftp", "tftp", "quic", "tcp", "http"]
    pools: dict = {
        ver: {ttype: {p: [] for p in _protos} for ttype in ("hostname", "ip")}
        for ver in ("v4", "v6")
    }

    for box in containers:
        def _ports(key: str) -> list[int]:
            return (box.get(key) or {}).get("ports") or []
        def _creds(key: str) -> tuple[str, str]:
            creds = (box.get(key) or {}).get("credentials") or {}
            return creds.get("username", ""), creds.get("password", "")

        v4_hn = [h for h in (box.get("ipv4_hostnames") or []) if ":" not in h]
        v4_ip = [h for h in (box.get("ipv4") or [])          if ":" not in h]
        v6_hn = box.get("ipv6_hostnames") or []
        v6_ip = box.get("ipv6") or []

        ftp_u,  ftp_p  = _creds("FTP")
        sftp_u, sftp_p = _creds("SFTP")

        for ver, hn_list, ip_list in [("v4", v4_hn, v4_ip), ("v6", v6_hn, v6_ip)]:
            for ttype, hosts in [("hostname", hn_list), ("ip", ip_list)]:
                for host in hosts:
                    for port in _ports("FTP"):
                        pools[ver][ttype]["ftp"].append(
                            {"host": host, "port": str(port),
                             "username": ftp_u or "anonymous", "password": ftp_p or "anonymous"})
                    if sftp_u and sftp_p:
                        for port in _ports("SFTP"):
                            pools[ver][ttype]["sftp"].append(
                                {"host": host, "port": str(port),
                                 "username": sftp_u, "password": sftp_p})
                    for port in _ports("TFTP"):
                        pools[ver][ttype]["tftp"].append({"host": host, "port": str(port)})
                    for port in _ports("QUICT"):
                        pools[ver][ttype]["quic"].append({"host": host, "port": str(port)})
                    for port in _ports("TCPT"):
                        pools[ver][ttype]["tcp"].append({"host": host, "port": str(port)})
                    for port in _ports("HTTP"):
                        pools[ver][ttype]["http"].append({"host": host, "port": str(port)})

    log.info("Endpoint pool sizes:")
    for ver in ("v4", "v6"):
        for ttype in ("hostname", "ip"):
            counts = {p: len(pools[ver][ttype][p]) for p in _protos}
            log.info("  %s/%-8s  %s", ver, ttype, counts)
    return pools


# --- Schedule helpers ---------------------------------------------------------

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# Stride (in client-index units) used to shift the rotation's starting point
# from one day to the next, so a client's active hours differ day-to-day
# instead of repeating the same hour pattern every day of the week. Any value
# that isn't a small divisor of typical client counts works; 37 is just an
# arbitrary prime-ish choice for good spread.
DAY_PHASE_STRIDE = 37

# Stride (in client-index units) used to shift the rotation's starting point
# from one AP's "schedule_group" to the next, so two APs with different
# schedule_group values get different active-hour patterns even when they
# have identical veth/idle_percent config. Same spread idea as
# DAY_PHASE_STRIDE, just a different prime-ish constant to avoid the two
# shifts canceling each other out.
AP_PHASE_STRIDE = 53


def _default_schedule_group(ap_ip: str) -> int:
    """
    Stable per-AP default for `schedule_group` when an AP's config doesn't set
    one explicitly. Derived from the AP's IP/host string (not from dict order
    or any other address), so every AP gets a different schedule out of the
    box, but the value is deterministic across runs for the same AP.
    """
    return int(hashlib.md5(ap_ip.encode()).hexdigest(), 16) % 10_000


def _get_schedule(day_hours: dict[str, list[int]]) -> dict:
    """Build the weekdays_schedule dict from a per-day {day_name: [hours]} map."""
    return {day: {h: True for h in hours} for day, hours in day_hours.items()}


def _active_count_per_hour(n_clients: int, idle_percent: int) -> int:
    """How many clients may be concurrently active at any single hour, given idle_percent."""
    if n_clients <= 0:
        return 0
    return round(n_clients * (100 - idle_percent) / 100)


def _is_active_this_hour(
    global_idx: int, day_idx: int, hour_idx: int, n_clients: int, active_count: int,
    schedule_group: int = 0,
) -> bool:
    """
    Round-robin rotation: the "active" window of `active_count` client indices
    shifts by `active_count` positions every hour, so a different slice of
    clients is active each hour while the concurrently-active COUNT stays fixed
    (this is what keeps the idle/active ratio correct at any point in time).
    `day_idx` additionally phase-shifts the rotation's starting point per day
    of the week (only when SEND_DAYWISE_VARIATION=1), so the same client's
    active hours vary day-to-day instead of repeating identically every day.
    `schedule_group` phase-shifts the rotation's starting point per AP, so two
    APs with different schedule_group values don't produce identical
    schedules for their same-index clients (same mechanism as the day-of-week
    shift, just keyed by AP instead of by day).
    """
    if active_count <= 0 or n_clients <= 0:
        return False
    if active_count >= n_clients:
        return True
    day_phase = (day_idx * DAY_PHASE_STRIDE) % n_clients if SEND_DAYWISE_VARIATION else 0
    ap_phase  = (schedule_group * AP_PHASE_STRIDE) % n_clients
    start = (ap_phase + day_phase + hour_idx * active_count) % n_clients
    return (global_idx - start) % n_clients < active_count


def _client_hour_schedule(
    global_idx: int, n_clients: int, idle_percent: int, schedule_group: int = 0,
) -> tuple[dict[str, list[int]], dict[str, list[int]]]:
    """
    Returns (active_by_day, idle_by_day) — dict[day_name -> hours (from
    HOURS_BASE)] this client is active vs idle, per the rotating idle_percent
    schedule for this run. Both the hour-of-day AND the day-of-week rotation
    vary, so a client's active hours differ across Mon/Tue/Wed/... instead of
    repeating the same pattern every day. `schedule_group` additionally
    differentiates the schedule across APs (see `_is_active_this_hour`).
    """
    active_count = _active_count_per_hour(n_clients, idle_percent)
    active_by_day: dict[str, list[int]] = {}
    idle_by_day: dict[str, list[int]] = {}
    for day_idx, day in enumerate(DAY_NAMES):
        active_hours: list[int] = []
        idle_hours: list[int] = []
        for hour_idx, hour in enumerate(HOURS_BASE):
            if _is_active_this_hour(global_idx, day_idx, hour_idx, n_clients, active_count, schedule_group):
                active_hours.append(hour)
            else:
                idle_hours.append(hour)
        active_by_day[day] = active_hours
        idle_by_day[day]   = idle_hours
    return active_by_day, idle_by_day


def _rotation_coverage_ok(n_clients: int, idle_percent: int) -> bool:
    """
    True if every client is guaranteed at least one active hour (on at least
    one day) across HOURS_BASE this run. idle_percent=100 is an explicit
    "always idle" config, not a gap.
    """
    active_count = _active_count_per_hour(n_clients, idle_percent)
    if active_count <= 0:
        return idle_percent >= 100
    return active_count * len(HOURS_BASE) >= n_clients


# --- IP version resolution ----------------------------------------------------

def _resolve_version(band_ip_mode: str) -> str:
    """Resolve "Dual" to a concrete version; pass IPv4/IPv6 through unchanged."""
    if band_ip_mode == "Dual":
        return random.choice(["IPv4", "IPv6"])
    return band_ip_mode


def _pool_key(version: str) -> str:
    return "v4" if version == "IPv4" else "v6"


# --- Random helpers -----------------------------------------------------------

def _rnd_dscp()  -> int: return random.choice(DSCP_VALUES)
def _rnd_pkt()   -> int: return random.choice(PACKET_SIZES)
def _rnd_fsize() -> int: return random.choice(FILE_SIZES)
def _rnd_dsize() -> int: return random.choice(DATA_SIZES)
def _rnd_fival() -> int: return random.choice(FILE_IVALS)
def _rnd_cival() -> int: return random.choice(CLIENT_IVALS)
def _rnd_conn()  -> int: return random.choice(CONN_IVALS)


# --- Session builders ---------------------------------------------------------

def build_browsing_sessions(interface: str, schedule: dict) -> list[dict]:
    """BROWSING_SESSIONS_PER_CLIENT HTTPS GET sessions to random public websites.
    datasize=1 bypasses zero-validation."""
    sessions = []
    for site in random.sample(BROWSING_SITES, BROWSING_SESSIONS_PER_CLIENT):
        sessions.append({
            "status":             "enable",
            "traffictype":        "HTTPS",
            "host":               site,
            "operation":          "GET",
            "interval":           random.choice(BROWSE_IVALS),
            "datasize":           1,
            "packetsize":         0,
            "connectioninterval": 0,
            "network":            "IPv4",
            "dscpvalue":          _rnd_dscp(),
            "interface":          interface,
            "port":               "",
            "filename":           "",
            "username":           "",
            "password":           "",
            "useragent":          "",
            "payload":            "",
            "traffic_schedule":   schedule,
        })
    return sessions


def _clientop_pool(endpoints: dict, band_cfg: dict) -> tuple[dict, str]:
    """Resolve (pool, version) for a clientop session, falling back to v4 if empty."""
    version = _resolve_version(band_cfg["ip_mode"])
    ver_key = _pool_key(version)
    target_type = band_cfg["target_type"]
    pool = endpoints[ver_key][target_type]
    if not any(pool[p] for p in ("quic", "tcp")):
        pool, version = endpoints["v4"][target_type], "IPv4"
    return pool, version


def build_clientop_sessions(
    interface: str, endpoints: dict, band_cfg: dict, schedule: dict,
) -> list[dict]:
    """Build band_cfg["clientop_count"] ClientOp sessions — randomly QUICT or TCPT."""
    sessions: list[dict] = []
    for _ in range(band_cfg["clientop_count"]):
        pool, version = _clientop_pool(endpoints, band_cfg)
        available = [p for p in ("quic", "tcp") if pool[p]]
        if not available:
            log.warning("No QUICT/TCPT endpoints; skipping one clientop session for %s", interface)
            continue

        proto       = random.choice(available)
        ep          = random.choice(pool[proto])
        traffictype = "QUICT" if proto == "quic" else "TCPT"

        sessions.append({
            "status":             "enable",
            "traffictype":        traffictype,
            "host":               ep["host"],
            "port":               ep["port"],
            "operation":          random.choices(["download", "upload"], weights=[0.75, 0.25])[0],
            "interval":           _rnd_cival(),
            "datasize":           _rnd_dsize(),
            "packetsize":         _rnd_pkt(),
            "connectioninterval": _rnd_conn(),
            "network":            version,
            "dscpvalue":          _rnd_dscp(),
            "interface":          interface,
            "filename":           "",
            "username":           "",
            "password":           "",
            "useragent":          "",
            "payload":            "",
            "traffic_schedule":   schedule,
        })
    return sessions


def _fileop_pool(endpoints: dict, band_cfg: dict) -> tuple[dict, str]:
    """Resolve (pool, version) for a fileop session, falling back to v4 if empty."""
    version = _resolve_version(band_cfg["ip_mode"])
    ver_key = _pool_key(version)
    target_type = band_cfg["target_type"]
    pool = endpoints[ver_key][target_type]
    if not any(pool[p] for p in ("sftp", "ftp", "tftp")):
        pool, version = endpoints["v4"][target_type], "IPv4"
    return pool, version


def build_fileop_sessions(
    interface: str, endpoints: dict, band_cfg: dict, schedule: dict,
) -> list[dict]:
    """Build band_cfg["fileop_count"] FileOp sessions — randomly sftp/ftp/tftp."""
    sessions: list[dict] = []
    for _ in range(band_cfg["fileop_count"]):
        pool, version = _fileop_pool(endpoints, band_cfg)
        available = [p for p in ("sftp", "ftp", "tftp") if pool[p]]
        if not available:
            log.warning("No fileop endpoints; skipping one fileop session for %s", interface)
            continue

        proto    = random.choice(available)
        ep       = random.choice(pool[proto])
        filesize = _rnd_fsize()

        session: dict = {
            "status":           "enable",
            "traffictype":      proto,
            "host":             ep["host"],
            "operation":        random.choices(["download", "upload"], weights=[0.75, 0.25])[0],
            "filesize":         min(filesize, 8) if proto == "tftp" else filesize,
            "packetsize":       _rnd_pkt(),
            "interval":         _rnd_fival(),
            "network":          version,
            "dscpvalue":        _rnd_dscp(),
            "interface":        interface,
            "traffic_schedule": schedule,
        }
        if proto == "tftp":
            session["port"] = ep["port"]
            session["mode"] = "octet"
        else:
            session["port"]     = ep["port"]
            session["username"] = ep["username"]
            session["password"] = ep["password"]
        sessions.append(session)
    return sessions


# --- Per-AP payload assembly --------------------------------------------------

def build_idle_sessions(interface: str, endpoints: dict, band_cfg: dict, schedule: dict) -> list[dict]:
    """
    Lightweight HTTP GET to discovery server for this client's idle hours.
    Keeps the client visible on the AP without generating real load.
    Uses /wifiagent/dynamic which returns a small dynamic response.
    """
    target_type  = band_cfg["target_type"]
    band_ip_mode = band_cfg["ip_mode"]
    version = _resolve_version(band_ip_mode)
    ver_key = _pool_key(version)

    pool = endpoints[ver_key][target_type]["http"]
    if not pool:
        pool = endpoints["v4"][target_type]["http"]
    if not pool:
        return []

    ep  = random.choice(pool)
    url = f"http://{ep['host']}:{ep['port']}/wifiagent/dynamic"

    return [{
        "status":             "enable",
        "traffictype":        "HTTP",
        "host":               url,
        "operation":          "GET",
        "interval":           random.choice([600, 900, 1200]),  # long interval — truly idle
        "datasize":           1,                                # bypass zero-validation
        "packetsize":         0,
        "connectioninterval": 0,
        "network":            version,
        "dscpvalue":          0,          # BE — idle traffic gets lowest priority
        "interface":          interface,
        "port":               "",
        "filename":           "",
        "username":           "",
        "password":           "",
        "useragent":          "",
        "payload":            "",
        "traffic_schedule":   schedule,   # scoped to this client's idle hours only
    }]


def build_ap_payloads(
    clients: list[dict], endpoints: dict, ap_bands: dict, idle_percent: int = IDLE_PERCENT,
    schedule_group: int = 0,
) -> tuple[dict | None, dict | None, dict]:
    """
    Rotating per-hour active/idle schedule (single run, hour-by-hour over HOURS_BASE):
      - At any given hour, idle_percent% of clients are idle (lightweight HTTP
        heartbeat) and the rest run full traffic (BROWSING_SESSIONS_PER_CLIENT
        HTTPS GETs + band_cfg["clientop_count"] clientop + band_cfg["fileop_count"]
        fileop sessions).
      - WHICH clients are idle/active rotates every hour (round-robin via
        _client_hour_schedule), so every client gets a turn at full traffic over
        the course of the run while the instantaneous idle/active ratio holds.
    ap_bands: {radio_int: {"veth_count", "fileop_count", "clientop_count", "ip_mode", "target_type"}}
    Returns (fileop_payload, client_payload, stats) where
      stats = {
          "active_count_per_hour": int, "uncovered": int, "total": int,
          "fileop_by_radio":    {radio: session_count},
          "browsing_by_radio":  {radio: session_count},
          "clientop_by_radio":  {radio: session_count},
          "heartbeat_by_radio": {radio: session_count},
          "active_by_radio":    {radio: active_client_count},
          "heartbeat_clients_by_radio": {radio: heartbeat_client_count},
      }
      "uncovered" = clients that got zero active hours this run (see _rotation_coverage_ok).
    """
    all_fileop: list[dict] = []
    all_client: list[dict] = []
    n_clients  = len(clients)
    uncovered  = 0
    fileop_by_radio:            dict[int, int] = {}
    browsing_by_radio:          dict[int, int] = {}
    clientop_by_radio:          dict[int, int] = {}
    heartbeat_by_radio:         dict[int, int] = {}
    active_by_radio:            dict[int, int] = {}
    heartbeat_clients_by_radio: dict[int, int] = {}

    for client in clients:
        iface      = client["interface"]
        radio      = client["radio"]
        global_idx = client["global_idx"]
        band_cfg   = client["band_cfg"]

        active_by_day, idle_by_day = _client_hour_schedule(global_idx, n_clients, idle_percent, schedule_group)
        has_active = any(active_by_day.values())
        has_idle   = any(idle_by_day.values())

        if has_active:
            active_schedule = {"traffic_default": False, "weekdays_schedule": _get_schedule(active_by_day)}
            browsing = build_browsing_sessions(iface, active_schedule)
            clientop = build_clientop_sessions(iface, endpoints, band_cfg, active_schedule)
            fileop   = build_fileop_sessions(iface, endpoints, band_cfg, active_schedule)
            all_client.extend(browsing)
            all_client.extend(clientop)
            all_fileop.extend(fileop)
            active_by_radio[radio]   = active_by_radio.get(radio, 0) + 1
            browsing_by_radio[radio] = browsing_by_radio.get(radio, 0) + len(browsing)
            clientop_by_radio[radio] = clientop_by_radio.get(radio, 0) + len(clientop)
            fileop_by_radio[radio]   = fileop_by_radio.get(radio, 0) + len(fileop)
        else:
            uncovered += 1

        if has_idle and SEND_HEARTBEAT:
            idle_schedule = {"traffic_default": False, "weekdays_schedule": _get_schedule(idle_by_day)}
            heartbeat = build_idle_sessions(iface, endpoints, band_cfg, idle_schedule)
            all_client.extend(heartbeat)
            if heartbeat:
                heartbeat_by_radio[radio]         = heartbeat_by_radio.get(radio, 0) + len(heartbeat)
                heartbeat_clients_by_radio[radio] = heartbeat_clients_by_radio.get(radio, 0) + 1

    fileop_payload = {"status": "enable", "fileoperations": all_fileop} if all_fileop else None
    client_payload = {"status": "enable", "clientoperation": all_client} if all_client else None
    stats = {
        "active_count_per_hour":       _active_count_per_hour(n_clients, idle_percent),
        "uncovered":                   uncovered,
        "total":                       n_clients,
        "fileop_by_radio":             fileop_by_radio,
        "browsing_by_radio":           browsing_by_radio,
        "clientop_by_radio":           clientop_by_radio,
        "heartbeat_by_radio":          heartbeat_by_radio,
        "active_by_radio":             active_by_radio,
        "heartbeat_clients_by_radio":  heartbeat_clients_by_radio,
    }
    return fileop_payload, client_payload, stats


def _fileop_breakdown_str(stats: dict, ap_bands: dict) -> str:
    """Render 'label=active×fileop_count=count, ...  (total=N)' per radio,
    same style as qwrap-traffic-config-dynamic-input.py's _breakdown_str."""
    fileop_by_radio = stats["fileop_by_radio"]
    parts = []
    for radio in sorted(fileop_by_radio):
        active   = stats["active_by_radio"].get(radio, 0)
        per_veth = ap_bands[radio]["fileop_count"]
        parts.append(f"{_RADIO_LABEL.get(radio, radio)}={active}x{per_veth}={fileop_by_radio[radio]}")
    total = sum(fileop_by_radio.values())
    return ", ".join(parts) + f"  (total={total})" if parts else "(total=0)"


def _client_breakdown_str(stats: dict, ap_bands: dict) -> str:
    """Render per-radio 'label=browsing(active×2)+clientop(active×N)+heartbeat(idle×1)=count'
    breakdown, same style as qwrap-traffic-config-dynamic-input.py's _breakdown_str,
    extended to cover the three session kinds that make up client sessions here."""
    radios = sorted(set(stats["browsing_by_radio"]) | set(stats["clientop_by_radio"]) | set(stats["heartbeat_by_radio"]))
    parts = []
    for radio in radios:
        active       = stats["active_by_radio"].get(radio, 0)
        heartbeat_n  = stats["heartbeat_clients_by_radio"].get(radio, 0)
        clientop_cnt = ap_bands[radio]["clientop_count"]
        radio_total  = (stats["browsing_by_radio"].get(radio, 0)
                        + stats["clientop_by_radio"].get(radio, 0)
                        + stats["heartbeat_by_radio"].get(radio, 0))
        parts.append(
            f"{_RADIO_LABEL.get(radio, radio)}=browsing({active}x{BROWSING_SESSIONS_PER_CLIENT})"
            f"+clientop({active}x{clientop_cnt})+heartbeat({heartbeat_n}x1)={radio_total}"
        )
    total = (sum(stats["browsing_by_radio"].values())
             + sum(stats["clientop_by_radio"].values())
             + sum(stats["heartbeat_by_radio"].values()))
    return ", ".join(parts) + f"  (total={total})" if parts else "(total=0)"


def configure_ap(ap_ip: str, clients: list[dict], endpoints: dict, ap_cfg: dict) -> dict:
    result         = {"ap": ap_ip}
    ap_bands       = ap_cfg["bands"]
    idle_percent   = ap_cfg.get("idle_percent", IDLE_PERCENT)
    schedule_group = ap_cfg.get("schedule_group", _default_schedule_group(ap_ip))

    fileop_payload, client_payload, stats = build_ap_payloads(
        clients, endpoints, ap_bands, idle_percent, schedule_group)

    band_summary = {
        _RADIO_LABEL[r]: f"{b['veth_count']}v/{b['fileop_count']}f/{b['clientop_count']}c/{b['ip_mode']}/{b['target_type']}"
        for r, b in ap_bands.items()
    }
    rotation_desc = "rotating hourly only (same hours every day)" if not SEND_DAYWISE_VARIATION \
        else "rotating hourly + varying by day-of-week"
    log.info("[%s] %d clients — ~%d active/hour (%s, idle_percent=%d%%) bands=%s",
             ap_ip, stats["total"], stats["active_count_per_hour"], rotation_desc, idle_percent, band_summary)

    if stats["uncovered"]:
        log.warning(
            "[%s] %d/%d client(s) got ZERO active hours this run — idle_percent=%d%% only allows "
            "%d active slot(s)/hour x %d hours, too few to rotate through all clients; "
            "lower idle_percent or reduce veth_count for full coverage.",
            ap_ip, stats["uncovered"], stats["total"], idle_percent,
            stats["active_count_per_hour"], len(HOURS_BASE),
        )

    log.info("[%s] BROWSING_SESSIONS_PER_CLIENT = %d  # fixed, applies to every active client",
             ap_ip, BROWSING_SESSIONS_PER_CLIENT)
    log.info("[%s] SEND_HEARTBEAT = %d  # %s", ap_ip, SEND_HEARTBEAT,
              "idle-hour heartbeat GET enabled" if SEND_HEARTBEAT else "idle hours fully silent")
    log.info("[%s] SEND_DAYWISE_VARIATION = %d  # %s", ap_ip, SEND_DAYWISE_VARIATION,
              "active hours differ per day-of-week" if SEND_DAYWISE_VARIATION
              else "same active hours repeat every day (weekly recurring)")
    log.info("[%s] schedule_group = %d  # %s", ap_ip, schedule_group,
              "explicitly set in AP_CONFIG" if "schedule_group" in ap_cfg
              else "auto-derived from AP IP (no schedule_group set)")

    if fileop_payload:
        ok, msg = _api_post(ap_ip, "/device/traffic/fileop/config", fileop_payload)
        n = len(fileop_payload["fileoperations"])
        result["fileop"] = f"ok ({n} sessions)" if ok else f"FAIL: {msg}"
    else:
        result["fileop"] = "skipped – no fileop endpoints"
    log.info("[%s] fileop  -> %s  [%s]", ap_ip, result["fileop"],
              _fileop_breakdown_str(stats, ap_bands))

    if client_payload:
        ok, msg = _api_post(ap_ip, "/device/traffic/client/config", client_payload)
        n = len(client_payload["clientoperation"])
        result["client"] = f"ok ({n} sessions)" if ok else f"FAIL: {msg}"
    else:
        result["client"] = "skipped – no clientop endpoints"
    log.info("[%s] client  -> %s  [%s]", ap_ip, result["client"],
              _client_breakdown_str(stats, ap_bands))

    return result


# --- Virtual-client list ------------------------------------------------------

_RADIO_LABEL = {0: "2.4G", 1: "5G", 2: "6G"}


def _clamp_band_cfg(radio: int, band_cfg: dict) -> dict:
    """Clamp veth_count to [0, MAX_VETH_PER_RADIO], warning if it was out of range."""
    cfg = dict(band_cfg)
    requested = cfg["veth_count"]
    cfg["veth_count"] = max(0, min(requested, MAX_VETH_PER_RADIO))
    if cfg["veth_count"] != requested:
        log.warning("%s veth_count %d clamped to %d (max %d)",
                    _RADIO_LABEL.get(radio, radio), requested, cfg["veth_count"], MAX_VETH_PER_RADIO)
    return cfg


def build_clients(ap_bands: dict[int, dict]) -> list[dict]:
    """Build the veth interface list for one AP from its resolved band config.
    ap_bands: {radio_int: {"veth_count", "fileop_count", "clientop_count", "ip_mode", "target_type"}}
    """
    clients: list[dict] = []
    global_idx = 0
    for radio, band_cfg in sorted(ap_bands.items()):
        for c in range(1, band_cfg["veth_count"] + 1):
            clients.append({
                "interface":  f"veth_in_{radio}_{c}",
                "radio":      radio,
                "global_idx": global_idx,
                "band_cfg":   band_cfg,
            })
            global_idx += 1
    return clients


# --- CLI ----------------------------------------------------------------------

_EXAMPLES = '''\
examples:
  python3 qwrap-traffic-75-25.py
  python3 qwrap-traffic-75-25.py --ap 10.86.205.240
  python3 qwrap-traffic-75-25.py --ap 10.86.205.240,10.86.205.157
  python3 qwrap-traffic-75-25.py --dry-run --out payloads.json
  python3 qwrap-traffic-75-25.py --debug

Per active client: BROWSING_SESSIONS_PER_CLIENT HTTPS GETs + fileop_count/clientop_count
sessions, scheduled 9AM-9PM. idle_percent% of clients are fully idle — set per-AP via
AP_CONFIG[ip]["idle_percent"], falling back to the global IDLE_PERCENT constant.
veth_count, fileop_count, clientop_count, ip_mode and target_type are all set
per-AP, per-radio in the AP_CONFIG dict at the top of this file.
'''


class _HelpFormatter(argparse.RawDescriptionHelpFormatter):
    def __init__(self, prog):
        super().__init__(prog, max_help_position=40, width=200)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="qwrap-traffic-75-25.py",
        usage=argparse.SUPPRESS,
        description="Configure reduced-load WifiAgent traffic on Qwrap APs",
        epilog=_EXAMPLES,
        formatter_class=_HelpFormatter,
    )
    p.add_argument("--ap", metavar="HOST[,HOST...]", default=None,
                   help="Comma-separated list of AP host/IPs to target instead of all APs in AP_CONFIG")
    p.add_argument("--dry-run", action="store_true",
                   help="Build payloads and write to --out without POSTing")
    p.add_argument("--out", default="traffic_payloads_claude.json", metavar="FILE")
    p.add_argument("--debug", action="store_true")
    return p.parse_args()


def resolveApList(args: argparse.Namespace) -> dict:
    '''Turn --ap into a filtered AP_CONFIG dict, hard-exiting on unknown host(s).'''
    if not args.ap:
        return AP_CONFIG
    requested_hosts = [h.strip() for h in args.ap.split(",") if h.strip()]
    missing_hosts = sorted(set(requested_hosts) - set(AP_CONFIG))
    if missing_hosts:
        log.error("Host(s) not found in AP_CONFIG: %s", missing_hosts)
        sys.exit(1)
    ap_config = {ip: AP_CONFIG[ip] for ip in requested_hosts}
    log.info("Targeting %d AP(s): %s", len(ap_config), sorted(ap_config))
    return ap_config


def runConcurrently(func, apList: list[str], actionName: str) -> dict:
    '''Run func(ip) concurrently across apList (one worker per AP), same
    pattern as qwrap-manager.py's runConcurrently(). Exits the process if any
    AP fails.'''
    errors = []
    results = {}
    with ThreadPoolExecutor(max_workers=len(apList)) as executor:
        futureToHost = {executor.submit(func, ip): ip for ip in apList}
        for future in as_completed(futureToHost):
            host = futureToHost[future]
            try:
                results[host] = future.result()
                log.info(f'{host}: {actionName} succeeded')
            except Exception as e:
                log.error(f'{host}: {actionName} FAILED: {e}')
                errors.append(host)

    if errors:
        log.error(f'{actionName} failed on: {errors}')
        sys.exit(1)
    return results


# --- Main ---------------------------------------------------------------------

def main() -> None:
    args = _parse_args()
    if args.debug:
        log.setLevel(logging.DEBUG)

    print('-' * 40)
    print('Resolve target AP(s)')
    print('-' * 40)
    ap_config = resolveApList(args)

    print('\n' + '-' * 40)
    print('Build client payloads')
    print('-' * 40)
    ap_clients: dict[str, list[dict]] = {}
    for ip, cfg in ap_config.items():
        bands        = {r: _clamp_band_cfg(r, b) for r, b in cfg["bands"].items()}
        idle_percent = cfg.get("idle_percent", IDLE_PERCENT)
        ap_config_entry = {"bands": bands, "idle_percent": idle_percent}
        if "schedule_group" in cfg:
            ap_config_entry["schedule_group"] = cfg["schedule_group"]
        ap_config[ip] = ap_config_entry
        schedule_group = ap_config_entry.get("schedule_group", _default_schedule_group(ip))
        clients = build_clients(bands)
        if not clients:
            log.warning("[%s] all configured radios have veth_count=0 or no radios configured; skipping AP", ip)
            continue
        ap_clients[ip] = clients

        total        = len(clients)
        active_count = _active_count_per_hour(total, idle_percent)
        summary      = ", ".join(
            f"{_RADIO_LABEL.get(r, r)}={b['veth_count']}v/{b['fileop_count']}f/{b['clientop_count']}c/{b['ip_mode']}/{b['target_type']}"
            for r, b in sorted(bands.items())
        )
        schedule_desc = ("same active hours repeat every day (weekly recurring)" if not SEND_DAYWISE_VARIATION
                          else "active hours differ per day-of-week")
        log.info("[%s] %d total clients — ~%d active/hour (idle_percent=%d%%, rotating hourly "
                 "over %d-hour window; %s; schedule_group=%d)  bands=[%s]",
                 ip, total, active_count, idle_percent, len(HOURS_BASE), schedule_desc, schedule_group, summary)
        if not _rotation_coverage_ok(total, idle_percent):
            log.warning(
                "[%s] idle_percent=%d%% with %d clients only allows %d active slot(s)/hour x %d hours "
                "= %d total active-slots this run — some client(s) will get ZERO active hours. "
                "Lower idle_percent or reduce veth_count for full rotation coverage.",
                ip, idle_percent, total, active_count, len(HOURS_BASE), active_count * len(HOURS_BASE),
            )

    if not ap_clients:
        log.error("Nothing to configure across all APs.")
        sys.exit(1)

    ap_url: dict[str, str] = {}
    for ip in ap_clients:
        url = _discovery_url_for(ip)
        if not url:
            log.error("No discovery URL for AP %s — add prefix to DISCOVERY_URLS", ip)
            sys.exit(1)
        ap_url[ip] = url

    endpoints_cache: dict[str, dict] = {}
    for url in sorted(set(ap_url.values())):
        endpoints_cache[url] = fetch_endpoints(url)

    ap_endpoints = {ip: endpoints_cache[ap_url[ip]] for ip in ap_clients}

    if args.dry_run:
        log.info("DRY-RUN: writing payloads to %s", args.out)
        bundle: dict[str, dict] = {}
        for ip in ap_clients:
            ap_cfg = ap_config[ip]
            ap_schedule_group = ap_cfg.get("schedule_group", _default_schedule_group(ip))
            fileop_payload, client_payload, stats = build_ap_payloads(
                ap_clients[ip], ap_endpoints[ip], ap_cfg["bands"], ap_cfg["idle_percent"], ap_schedule_group)
            bundle[ip] = {
                "ap_config":             ap_cfg,
                "fileop_url":            f"http://{ip}:{WIFIAGENT_PORT}/device/traffic/fileop/config",
                "client_url":            f"http://{ip}:{WIFIAGENT_PORT}/device/traffic/client/config",
                "fileop_payload":        fileop_payload,
                "client_payload":        client_payload,
                "active_count_per_hour": stats["active_count_per_hour"],
                "uncovered_clients":     stats["uncovered"],
                "fileop_session_count":  len(fileop_payload["fileoperations"]) if fileop_payload else 0,
                "client_session_count":  len(client_payload["clientoperation"]) if client_payload else 0,
            }
            log.info("  [%s] active/hour=%d uncovered=%d fileop=%d client=%d",
                     ip, bundle[ip]["active_count_per_hour"], bundle[ip]["uncovered_clients"],
                     bundle[ip]["fileop_session_count"], bundle[ip]["client_session_count"])
            log.info("  [%s] fileop breakdown [%s]", ip, _fileop_breakdown_str(stats, ap_cfg["bands"]))
            log.info("  [%s] client breakdown [%s]", ip, _client_breakdown_str(stats, ap_cfg["bands"]))
            if bundle[ip]["uncovered_clients"]:
                log.warning("[%s] %d client(s) got ZERO active hours this run (idle_percent=%d%% too high "
                            "for %d clients x %d hours)", ip, bundle[ip]["uncovered_clients"],
                            ap_cfg["idle_percent"], len(ap_clients[ip]), len(HOURS_BASE))
        with open(args.out, "w") as fh:
            json.dump(bundle, fh, indent=2)
        log.info("Wrote %d AP payload(s) -> %s", len(bundle), args.out)
        return

    print('\n' + '-' * 40)
    print('Push config to AP(s) concurrently')
    print('-' * 40)
    log.info("Configuring %d APs ...", len(ap_clients))

    results = runConcurrently(
        lambda ip: configure_ap(ip, ap_clients[ip], ap_endpoints[ip], ap_config[ip]),
        list(ap_clients),
        "configure",
    )
    summary: list[dict] = list(results.values())

    W = 20
    numCols = 3
    ruleWidth = W * numCols + (numCols - 1) * 2
    print("\n" + "=" * ruleWidth)
    print(f"{'AP IP':<{W}}  {'FileOp':<{W}}  {'Client':<{W}}")
    print("=" * ruleWidth)
    for r in sorted(summary, key=lambda x: x["ap"]):
        print(f"{r['ap']:<{W}}  {r.get('fileop', '\u2014'):<{W}}  {r.get('client', '\u2014'):<{W}}")
    print("=" * ruleWidth + "\n")


if __name__ == "__main__":
    main()