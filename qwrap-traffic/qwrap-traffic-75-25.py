#!/usr/bin/env python3
"""
Reduced-load traffic configuration for WifiAgent Qwrap APs.

Per virtual-client interface (4 sessions total):
  - 2 HTTPS GET sessions to random public websites (lightweight browsing simulation)
  - 1 ClientOp session : randomly QUICT or TCPT (from discovery endpoints)
  - 1 FileOp session   : randomly sftp / ftp / tftp (from discovery endpoints)

Additional features vs original:
  - 75/25 active/idle split — 25% of virtual clients are idle at any time
  - Per-AP config dict specifying ip_mode (IPv4/IPv6/Dual) and target_type (hostname/ip)
  - Band-wise ip_mode defaults: 2.4G -> IPv4, 5G -> IPv6, 6G -> Dual
  - Dual-stack endpoint pools (v4 + v6) built from discovery API
  - Time-based scheduling support (weekdays_schedule per session)
  - 75/25 download/upload weighting on all fileop and clientop sessions

Usage:
  python3 qwrap-traffic-75-25.py [--dry-run] [--ap HOST[,HOST...]] [--debug]
"""

import argparse
import json
import logging
import random
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import urllib3
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- AP inventory -------------------------------------------------------------
# ip_mode    : "IPv4" | "IPv6" | "Dual"
# target_type: "hostname" | "ip"

# Per-AP, per-band configuration.
# Each radio entry sets its own ip_mode and target_type independently.
#   radio 0 -> 2.4 GHz   radio 1 -> 5 GHz   radio 2 -> 6 GHz
# ip_mode    : "IPv4" | "IPv6" | "Dual"
# target_type: "hostname" | "ip"
AP_CONFIG: dict[str, dict] = {
    "10.86.205.240": {"bands": {
        0: {"ip_mode": "IPv6", "target_type": "hostname"},
        1: {"ip_mode": "IPv6", "target_type": "hostname"},
        2: {"ip_mode": "IPv6", "target_type": "hostname"},
    }},
}

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

RADIO_2_4G_CLIENTS    = 28
RADIO_5G_CLIENTS      = 28
RADIO_6G_CLIENTS      = 28
MAX_CLIENTS_PER_RADIO = 28

# 75/25 active/idle split: every 4th client (global_idx % 4 == 3) is idle.
IDLE_MODULO = 4

# --- Traffic pools ------------------------------------------------------------

DSCP_VALUES  = [0, 10, 26, 34, 46]
PACKET_SIZES = [64, 128, 256, 512, 1024, 1280, 1400, 1500, 2048, 4096, 9000]
FILE_SIZES   = [25, 50, 75, 100, 125]
DATA_SIZES   = [25, 50, 75, 100, 125]
FILE_IVALS   = [300, 450, 600, 900, 1100]
CLIENT_IVALS = [180, 300, 450, 600, 900]
CONN_IVALS   = [0, 30, 60, 120, 300]
BROWSE_IVALS = [300, 450, 600, 900]

# Daytime active hours (9 AM - 9 PM). Each client gets a 3-hour idle window
# carved out via IDLE_SHIFTS, giving a 75/25 session-level active/idle split
# within these hours in addition to the 25% fully-idle clients above.
HOURS_BASE  = list(range(9, 21))
IDLE_SHIFTS = [
    [9, 10, 11],
    [12, 13, 14],
    [15, 16, 17],
    [18, 19, 20],
]

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

# --- Logging ------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
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

    try:
        from urllib.parse import urlparse
        host = urlparse(discovery_url).hostname or "discovery"
        with open(f"discovery_raw_{host}.json", "w") as fh:
            json.dump(raw, fh, indent=2)
    except Exception as exc:
        log.debug("Could not write discovery json: %s", exc)

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

def _get_schedule(hour_list: list[int]) -> dict:
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    return {day: {h: True for h in hour_list} for day in days}


def _active_schedule(client_global_idx: int) -> dict:
    """Returns weekdays_schedule for the 9 active hours (excludes the client's idle shift)."""
    idle_shift = IDLE_SHIFTS[client_global_idx % len(IDLE_SHIFTS)]
    active_hours = [h for h in HOURS_BASE if h not in idle_shift]
    return {"traffic_default": False, "weekdays_schedule": _get_schedule(active_hours)}


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
    """2 HTTPS GET sessions to random public websites. datasize=1 bypasses zero-validation."""
    sessions = []
    for site in random.sample(BROWSING_SITES, 2):
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


def build_one_clientop_session(
    interface: str, endpoints: dict, band_cfg: dict, schedule: dict,
) -> list[dict]:
    """1 ClientOp session — randomly QUICT or TCPT."""
    band_ip_mode = band_cfg["ip_mode"]
    target_type  = band_cfg["target_type"]
    version = _resolve_version(band_ip_mode)
    ver_key = _pool_key(version)
    pool    = endpoints[ver_key][target_type]

    available = [p for p in ("quic", "tcp") if pool[p]]
    if not available:
        pool      = endpoints["v4"][target_type]
        available = [p for p in ("quic", "tcp") if pool[p]]
        version   = "IPv4"
    if not available:
        log.warning("No QUICT/TCPT endpoints; skipping clientop for %s", interface)
        return []

    proto       = random.choice(available)
    ep          = random.choice(pool[proto])
    traffictype = "QUICT" if proto == "quic" else "TCPT"

    return [{
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
    }]


def build_one_fileop_session(
    interface: str, endpoints: dict, band_cfg: dict, schedule: dict,
) -> list[dict]:
    """1 FileOp session — randomly sftp/ftp/tftp."""
    band_ip_mode = band_cfg["ip_mode"]
    target_type  = band_cfg["target_type"]
    version = _resolve_version(band_ip_mode)
    ver_key = _pool_key(version)
    pool    = endpoints[ver_key][target_type]

    available = [p for p in ("sftp", "ftp", "tftp") if pool[p]]
    if not available:
        pool      = endpoints["v4"][target_type]
        available = [p for p in ("sftp", "ftp", "tftp") if pool[p]]
        version   = "IPv4"
    if not available:
        log.warning("No fileop endpoints; skipping fileop for %s", interface)
        return []

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

    return [session]


# --- Per-AP payload assembly --------------------------------------------------

def build_idle_sessions(interface: str, endpoints: dict, band_cfg: dict) -> list[dict]:
    """
    Lightweight HTTP GET to discovery server for idle clients.
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
        "traffic_schedule":   {},         # runs any time — idle clients have no active window
    }]


def build_ap_payloads(
    clients: list[dict], endpoints: dict, ap_bands: dict,
) -> tuple[dict | None, dict | None, int]:
    """
    Per virtual client:
      - global_idx % 4 == 3  ->  fully idle, skip (25%)
      - otherwise             ->  2 HTTPS GETs + 1 clientop + 1 fileop
                                  each session scoped to active hours via schedule
    ap_bands: {radio_int: {"ip_mode": ..., "target_type": ...}}
    Returns (fileop_payload, client_payload, idle_count).
    """
    all_fileop: list[dict] = []
    all_client: list[dict] = []
    idle_count = 0

    for client in clients:
        iface      = client["interface"]
        radio      = client["radio"]
        global_idx = client["global_idx"]
        band_cfg   = ap_bands[radio]

        if global_idx % IDLE_MODULO == (IDLE_MODULO - 1):
            idle_count += 1
            # Idle clients get a single lightweight HTTP GET heartbeat — not silent
            all_client.extend(build_idle_sessions(iface, endpoints, band_cfg))
            continue

        schedule = _active_schedule(global_idx)

        all_client.extend(build_browsing_sessions(iface, schedule))
        all_client.extend(build_one_clientop_session(iface, endpoints, band_cfg, schedule))
        all_fileop.extend(build_one_fileop_session(iface, endpoints, band_cfg, schedule))

    fileop_payload = {"status": "enable", "fileoperations": all_fileop} if all_fileop else None
    client_payload = {"status": "enable", "clientoperation": all_client} if all_client else None
    return fileop_payload, client_payload, idle_count


def configure_ap(ap_ip: str, clients: list[dict], endpoints: dict, ap_cfg: dict) -> dict:
    result   = {"ap": ap_ip}
    ap_bands = ap_cfg["bands"]

    fileop_payload, client_payload, idle_count = build_ap_payloads(
        clients, endpoints, ap_bands)

    active = len(clients) - idle_count
    band_summary = {_RADIO_LABEL[r]: f"{b['ip_mode']}/{b['target_type']}" for r, b in ap_bands.items()}
    log.info("[%s] active=%d idle=%d bands=%s", ap_ip, active, idle_count, band_summary)

    if fileop_payload:
        ok, msg = _api_post(ap_ip, "/device/traffic/fileop/config", fileop_payload)
        n = len(fileop_payload["fileoperations"])
        result["fileop"] = f"ok ({n} sessions)" if ok else f"FAIL: {msg}"
    else:
        result["fileop"] = "skipped – no fileop endpoints"
    log.info("[%s] fileop  -> %s", ap_ip, result["fileop"])

    if client_payload:
        ok, msg = _api_post(ap_ip, "/device/traffic/client/config", client_payload)
        n = len(client_payload["clientoperation"])
        result["client"] = f"ok ({n} sessions)" if ok else f"FAIL: {msg}"
    else:
        result["client"] = "skipped – no clientop endpoints"
    log.info("[%s] client  -> %s", ap_ip, result["client"])

    return result


# --- Virtual-client list ------------------------------------------------------

_RADIO_LABEL = {0: "2.4G", 1: "5G", 2: "6G"}


def build_clients() -> list[dict]:
    counts = {
        0: max(0, min(RADIO_2_4G_CLIENTS, MAX_CLIENTS_PER_RADIO)),
        1: max(0, min(RADIO_5G_CLIENTS,   MAX_CLIENTS_PER_RADIO)),
        2: max(0, min(RADIO_6G_CLIENTS,   MAX_CLIENTS_PER_RADIO)),
    }
    clients = []
    global_idx = 0
    for radio, n in counts.items():
        for c in range(1, n + 1):
            clients.append({"interface": f"veth_in_{radio}_{c}",
                            "radio": radio, "global_idx": global_idx})
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

Per active client: 2 HTTPS GETs + 1 QUICT/TCPT + 1 fileop, scheduled 9AM-9PM.
25% of clients are fully idle. Band ip_mode: 2.4G=IPv4, 5G=IPv6, 6G=Dual.
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

    print('-' * 40)
    print('Build client payloads')
    print('-' * 40)
    clients = build_clients()
    if not clients:
        log.error("All RADIO_*_CLIENTS are 0 — nothing to configure.")
        sys.exit(1)

    total    = len(clients)
    idle_n   = total // IDLE_MODULO
    active_n = total - idle_n
    log.info("Per-AP virtual clients: %d total — %d active (75%%), %d idle (25%%)",
             total, active_n, idle_n)
    log.info("Per-AP band config set in AP_CONFIG (bands dict per AP)")

    ap_url: dict[str, str] = {}
    for ip in ap_config:
        url = _discovery_url_for(ip)
        if not url:
            log.error("No discovery URL for AP %s — add prefix to DISCOVERY_URLS", ip)
            sys.exit(1)
        ap_url[ip] = url

    endpoints_cache: dict[str, dict] = {}
    for url in sorted(set(ap_url.values())):
        endpoints_cache[url] = fetch_endpoints(url)

    ap_endpoints = {ip: endpoints_cache[ap_url[ip]] for ip in ap_config}

    if args.dry_run:
        log.info("DRY-RUN: writing payloads to %s", args.out)
        bundle: dict[str, dict] = {}
        for ip, ap_cfg in ap_config.items():
            fileop_payload, client_payload, idle_count = build_ap_payloads(
                clients, ap_endpoints[ip], ap_cfg["bands"])
            bundle[ip] = {
                "ap_config":            ap_cfg,
                "fileop_url":           f"http://{ip}:{WIFIAGENT_PORT}/device/traffic/fileop/config",
                "client_url":           f"http://{ip}:{WIFIAGENT_PORT}/device/traffic/client/config",
                "fileop_payload":       fileop_payload,
                "client_payload":       client_payload,
                "active_clients":       len(clients) - idle_count,
                "idle_clients":         idle_count,
                "fileop_session_count": len(fileop_payload["fileoperations"]) if fileop_payload else 0,
                "client_session_count": len(client_payload["clientoperation"]) if client_payload else 0,
            }
            log.info("  [%s] active=%d idle=%d fileop=%d client=%d",
                     ip, bundle[ip]["active_clients"], bundle[ip]["idle_clients"],
                     bundle[ip]["fileop_session_count"], bundle[ip]["client_session_count"])
        with open(args.out, "w") as fh:
            json.dump(bundle, fh, indent=2)
        log.info("Wrote %d AP payload(s) -> %s", len(bundle), args.out)
        return

    print('-' * 40)
    print('Push config to AP(s) concurrently')
    print('-' * 40)
    log.info("Configuring %d APs ...", len(ap_config))

    results = runConcurrently(
        lambda ip: configure_ap(ip, clients, ap_endpoints[ip], ap_config[ip]),
        list(ap_config),
        "configure",
    )
    summary: list[dict] = list(results.values())

    W = 20
    print("\n" + "\u2500" * (W * 3 + 2))
    print(f"{'AP IP':<{W}}  {'FileOp':<{W}}  {'Client'}")
    print("\u2500" * (W * 3 + 2))
    for r in sorted(summary, key=lambda x: x["ap"]):
        print(f"{r['ap']:<{W}}  {r.get('fileop', '\u2014'):<{W}}  {r.get('client', '\u2014')}")
    print("\u2500" * (W * 3 + 2) + "\n")


if __name__ == "__main__":
    main()