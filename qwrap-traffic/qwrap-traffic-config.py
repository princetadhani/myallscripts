#!/usr/bin/env python3
"""
Stress and stability traffic configuration for WifiAgent Qwrap APs.

Per virtual-client interface this script configures:
  - 2 File Operation sessions  : random protocol (tftp/ftp/sftp), random op (upload/download)
  - 3 Client Operation sessions : QUICT + TCPT + one of (HTTP | HTTPS), random op

Endpoints (host, port, credentials) come from the discovery API at runtime.
Virtual clients per radio are picked via the RADIO_2_4G_CLIENTS / RADIO_5G_CLIENTS /
RADIO_6G_CLIENTS constants below — each 0..28 — which expand into
veth_in_<radio>_<index> interfaces (radio 0 = 2.4G, 1 = 5G, 2 = 6G).

Usage:
  python3 QwrapApTrafficConfigure.py [--dry-run] [--workers N] [--debug]
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

# ─── AP inventory ─────────────────────────────────────────────────────────────

AP_IPS: list[str] = [
#S1 qwrap:
'10.86.205.122',
'10.86.204.204',
'10.86.205.60',
'10.86.205.223',
'10.86.204.227',
'10.86.205.165',
# For S2 QWRAP
'10.86.205.82',
'10.86.205.240',
'10.86.205.157',
'10.86.205.90',
'10.86.205.88',
'10.86.205.49',
# For S3 Qwrap - ABZ
'10.87.169.175',
'10.87.169.130',
'10.87.169.87',
'10.87.169.17',
'10.87.169.113',
'10.87.169.112',
'10.87.169.243'
]

# Discovery endpoint is chosen per-AP based on the AP IP's leading octets.
# Add new prefix → URL entries here as more sites come online.
DISCOVERY_URLS: dict[str, str] = {
    "10.87": "http://pune-abz-traffic-enpoint.dt1.wifi.arista.cloud/api/discovery",
    "10.86": "http://pune-traffic-enpoint.dt1.wifi.arista.cloud/api/discovery",
    "10.85": "http://blr-traffic-endpoint.dt1.wifi.arista.cloud/api/discovery",
    "10.81": "http://hq-traffic-endpoint.dt1.wifi.arista.cloud/api/discovery",
    "10.76": "http://hq-traffic-endpoint.dt1.wifi.arista.cloud/api/discovery",
}

WIFIAGENT_PORT = 8083
TIMEOUT = 60  # seconds per request

# ─── Virtual-client selection per radio ──────────────────────────────────────
# Each radio supports up to 28 virtual clients (veth_in_<radio>_1 .. _28).
#   radio 0 → 2.4 GHz   (veth_in_0_*)
#   radio 1 → 5 GHz     (veth_in_1_*)
#   radio 2 → 6 GHz     (veth_in_2_*)
# Set how many veth interfaces (1..28) to drive per radio. 0 = skip the radio.
RADIO_2_4G_CLIENTS = 28
RADIO_5G_CLIENTS   = 28
RADIO_6G_CLIENTS   = 28
MAX_CLIENTS_PER_RADIO = 28


def _discovery_url_for(ap_ip: str) -> str | None:
    for prefix, url in DISCOVERY_URLS.items():
        if ap_ip.startswith(prefix + "."):
            return url
    return None

# ─── Traffic variety pools ────────────────────────────────────────────────────

DSCP_VALUES  = [0, 10, 26, 34, 46]               # BE, AF11, AF31, AF41, EF
PACKET_SIZES = [64, 128, 256, 512, 1024, 1280, 1400, 1500]  # bytes (Ethernet MTU = 1500)
                                                              # 64   = min Ethernet frame / TCP ACK
                                                              # 128  = small control / mgmt
                                                              # 256  = VoIP / small data
                                                              # 512  = medium data
                                                              # 1024 = common chunk size
                                                              # 1280 = IPv6 min MTU
                                                              # 1400 = common tunnel-safe MTU
                                                              # 1500 = standard Ethernet MTU
FILE_SIZES   = [25, 50, 75, 100, 125]        # MB  (fileop filesize, max 100)
DATA_SIZES   = [25, 50, 75, 100, 125]        # MB  (client datasize, max 100)
FILE_IVALS   = [300, 450, 600, 900, 1100]        # seconds  (must be > 120; sized for 25-125 MB transfers)
CLIENT_IVALS = [180, 300, 450, 600, 900]         # seconds  (must be > 60;  sized for 25-125 MB transfers)
CONN_IVALS   = [0, 30, 60, 120, 300]             # seconds  (keep-alive after transfer; 0 = close immediately)

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("traffic_config")

# ─── HTTP session (shared) ────────────────────────────────────────────────────

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
    # WifiAgent sometimes returns 2xx with an empty body on success.
    if 200 <= r.status_code < 300 and not text:
        return True, f"HTTP {r.status_code} (empty body)"

    try:
        data = r.json()
    except ValueError:
        ok = 200 <= r.status_code < 300
        return ok, f"HTTP {r.status_code}: {text[:300]!r}"

    if data.get("status") == "success":
        return True, "success"
    if 200 <= r.status_code < 300 and "status" not in data:
        return True, f"HTTP {r.status_code} (no status field)"
    msg = data.get("message") or repr(data)
    return False, f"HTTP {r.status_code}: {msg}"


# ─── Discovery ────────────────────────────────────────────────────────────────

def fetch_endpoints(discovery_url: str) -> dict[str, list[dict]]:
    """
    Call the discovery API and return endpoints grouped by protocol.

    Discovery response schema (one container per traffic server farm):
      {
        "containers": [
          {
            "name": "scale_box_3",
            "ipv4_hostnames": ["host1.fqdn", "host2.fqdn", ...],
            "QUICT": {"ports": [4046, 4047, ...]},
            "FTP":   {"ports": [...], "credentials": {"username": "...", "password": "..."}},
            "SFTP":  {"ports": [...], "credentials": {"username": "...", "password": "..."}},
            "TFTP":  {"ports": [...]},
            "HTTP":  {"ports": [...]},
            "HTTPS": {"ports": [...]},
            "TCPT":  {"ports": [...]}
          },
          ...
        ]
      }

    Each (hostname × port) pair is expanded into its own endpoint entry so
    random.choice() across the resulting list gives maximum variety.

    Returned shape:
      {
        "ftp":   [{"host","port","username","password"}, ...],
        "sftp":  [{"host","port","username","password"}, ...],
        "tftp":  [{"host","port"}, ...],
        "http":  [{"base_url"}, ...],
        "https": [{"base_url"}, ...],
        "quic":  [{"host","port"}, ...],
        "tcp":   [{"host","port"}, ...],
      }
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

    result: dict[str, list] = {
        "ftp": [], "sftp": [], "tftp": [],
        "http": [], "https": [], "quic": [], "tcp": [],
    }

    for box in containers:
        hostnames = box.get("ipv4_hostnames") or []
        if not hostnames:
            log.debug("Container %s has no ipv4_hostnames; skipping",
                      box.get("name"))
            continue

        def _ports(proto_key: str) -> list[int]:
            block = box.get(proto_key) or {}
            return block.get("ports") or []

        def _creds(proto_key: str) -> tuple[str, str]:
            block = box.get(proto_key) or {}
            creds = block.get("credentials") or {}
            return creds.get("username", ""), creds.get("password", "")

        # ── FTP ─────────────────────────────────────────────────────
        ftp_user, ftp_pass = _creds("FTP")
        for host in hostnames:
            for port in _ports("FTP"):
                result["ftp"].append({
                    "host":     host,
                    "port":     str(port),
                    "username": ftp_user or "anonymous",
                    "password": ftp_pass or "anonymous",
                })

        # ── SFTP ────────────────────────────────────────────────────
        sftp_user, sftp_pass = _creds("SFTP")
        if sftp_user and sftp_pass:
            for host in hostnames:
                for port in _ports("SFTP"):
                    result["sftp"].append({
                        "host":     host,
                        "port":     str(port),
                        "username": sftp_user,
                        "password": sftp_pass,
                    })

        # ── TFTP ────────────────────────────────────────────────────
        for host in hostnames:
            for port in _ports("TFTP"):
                result["tftp"].append({"host": host, "port": str(port)})

        # ── HTTP ────────────────────────────────────────────────────
        for host in hostnames:
            for port in _ports("HTTP"):
                result["http"].append(
                    {"base_url": f"http://{host}:{port}"}
                )

        # ── HTTPS ───────────────────────────────────────────────────
        for host in hostnames:
            for port in _ports("HTTPS"):
                result["https"].append(
                    {"base_url": f"https://{host}:{port}"}
                )

        # ── QUICT ───────────────────────────────────────────────────
        for host in hostnames:
            for port in _ports("QUICT"):
                result["quic"].append({"host": host, "port": str(port)})

        # ── TCPT ────────────────────────────────────────────────────
        for host in hostnames:
            for port in _ports("TCPT"):
                result["tcp"].append({"host": host, "port": str(port)})

    log.info("Endpoint pool (hostname × port combinations):")
    for proto, eps in result.items():
        log.info("  %-6s : %d", proto, len(eps))

    return result

# ─── Random helpers ───────────────────────────────────────────────────────────

def _rnd_dscp()   -> int: return random.choice(DSCP_VALUES)
def _rnd_pkt()    -> int: return random.choice(PACKET_SIZES)
def _rnd_fsize()  -> int: return random.choice(FILE_SIZES)
def _rnd_dsize()  -> int: return random.choice(DATA_SIZES)
def _rnd_fival()  -> int: return random.choice(FILE_IVALS)
def _rnd_cival()  -> int: return random.choice(CLIENT_IVALS)
def _rnd_conn()   -> int: return random.choice(CONN_IVALS)

# ─── Session builders ─────────────────────────────────────────────────────────

def build_fileop_sessions(interface: str, endpoints: dict[str, list]) -> list[dict]:
    """
    Build 2 file-operation sessions for one virtual-client interface.
    Protocol is chosen randomly from whatever the discovery API provides.
    """
    available = [p for p in ("sftp", "ftp", "tftp") if endpoints[p]]
    if not available:
        log.warning("No file-op endpoints available; skipping fileop for %s", interface)
        return []

    sessions: list[dict] = []
    for _ in range(2):
        proto = random.choice(available)
        ep    = random.choice(endpoints[proto])
        op    = random.choice(["upload", "download"])

        session: dict = {
            "status":      "enable",
            "traffictype": proto,
            "host":        ep["host"],
            "operation":   op,
            "filesize":    _rnd_fsize(),
            "packetsize":  _rnd_pkt(),
            "interval":    _rnd_fival(),
            "network":     "IPv4",
            "dscpvalue":   _rnd_dscp(),
            "interface":   interface,
            "traffic_schedule": {},
        }
        if proto == "tftp":
            session["port"] = ep["port"]
            session["mode"] = "octet"
        else:  # ftp / sftp
            session["port"]     = ep["port"]
            session["username"] = ep["username"]
            session["password"] = ep["password"]

        sessions.append(session)

    return sessions


def _build_quict_session(interface: str, ep: dict) -> dict:
    return {
        "status":             "enable",
        "traffictype":        "QUICT",
        "host":               ep["host"],
        "port":               ep["port"],
        "operation":          random.choice(["upload", "download"]),
        "interval":           _rnd_cival(),
        "datasize":           _rnd_dsize(),
        "packetsize":         _rnd_pkt(),
        "connectioninterval": _rnd_conn(),
        "network":            "IPv4",
        "dscpvalue":          _rnd_dscp(),
        "interface":          interface,
        "traffic_schedule":   {},
    }


def _build_tcpt_session(interface: str, ep: dict) -> dict:
    return {
        "status":             "enable",
        "traffictype":        "TCPT",
        "host":               ep["host"],
        "port":               ep["port"],
        "operation":          random.choice(["upload", "download"]),
        "interval":           _rnd_cival(),
        "datasize":           _rnd_dsize(),
        "packetsize":         _rnd_pkt(),
        "connectioninterval": _rnd_conn(),
        "network":            "IPv4",
        "dscpvalue":          _rnd_dscp(),
        "interface":          interface,
        "traffic_schedule":   {},
    }


def _build_http_session(interface: str, proto: str, ep: dict) -> dict:
    """proto is 'http' or 'https'."""
    op       = random.choice(["upload", "download"])
    http_op  = "POST" if op == "upload" else "GET"
    url_path = "/wifiagent/upload" if op == "upload" else "/wifiagent/download"
    fsize    = _rnd_fsize()
    pkt      = _rnd_pkt()

    # Encode packet-size and file-size into the filename so the wifiagent
    # HTTP server can generate synthetic data without needing real files.
    if op == "download":
        filename = f"wifiagent_download_{fsize}MB_{pkt}PS.txt"
    else:
        suffix   = random.randint(100000, 999999)
        filename = f"wifiagent_upload_{fsize}MB_{pkt}PS_{suffix}.txt"

    return {
        "status":             "enable",
        "traffictype":        proto.upper(),               # "HTTP" or "HTTPS"
        "host":               ep["base_url"] + url_path,
        "operation":          http_op,
        "filename":           filename,
        "interval":           _rnd_cival(),
        "datasize":           fsize,
        "packetsize":         pkt,
        "connectioninterval": _rnd_conn(),
        "network":            "IPv4",
        "dscpvalue":          _rnd_dscp(),
        "interface":          interface,
        "traffic_schedule":   {},
    }


def build_client_sessions(interface: str, endpoints: dict[str, list]) -> list[dict]:
    """
    Build 2 client-operation sessions for one virtual-client interface.
    Each session's protocol is picked randomly from whichever of
    {HTTP, HTTPS, QUICT, TCPT} the discovery API exposes.
    """
    available = [p for p in ("http", "https", "quic", "tcp") if endpoints[p]]
    if not available:
        log.warning("No client endpoints available; skipping client ops for %s", interface)
        return []

    sessions: list[dict] = []
    for _ in range(2):
        proto = random.choice(available)
        ep    = random.choice(endpoints[proto])
        if proto == "quic":
            sessions.append(_build_quict_session(interface, ep))
        elif proto == "tcp":
            sessions.append(_build_tcpt_session(interface, ep))
        else:  # http or https
            sessions.append(_build_http_session(interface, proto, ep))
    return sessions

# ─── Per-AP configuration ────────────────────────────────────────────────────

def build_ap_payloads(
    clients:    list[dict],
    endpoints:  dict[str, list],
) -> tuple[dict | None, dict | None]:
    """Build the fileop and client payloads for one AP (no API calls)."""
    all_fileop: list[dict] = []
    all_client: list[dict] = []
    for client in clients:
        iface = client["interface"]
        all_fileop.extend(build_fileop_sessions(iface, endpoints))
        all_client.extend(build_client_sessions(iface, endpoints))
    fileop_payload = {"status": "enable", "fileoperations": all_fileop} if all_fileop else None
    client_payload = {"status": "enable", "clientoperation": all_client} if all_client else None
    return fileop_payload, client_payload


def configure_ap(
    ap_ip:      str,
    clients:    list[dict],           # [{"interface": "veth_in_0_1", "ipv4": "..."}, ...]
    endpoints:  dict[str, list],
) -> dict:
    result = {"ap": ap_ip}

    # No stop call – /config endpoint creates-or-updates in place.
    fileop_payload, client_payload = build_ap_payloads(clients, endpoints)

    # ── File Operations ────────────────────────────────────────────────
    if fileop_payload:
        ok, msg = _api_post(ap_ip, "/device/traffic/fileop/config", fileop_payload)
        n = len(fileop_payload["fileoperations"])
        result["fileop"] = f"ok ({n} sessions)" if ok else f"FAIL: {msg}"
    else:
        result["fileop"] = "skipped – no file-op endpoints"

    log.info("[%s] fileop  → %s", ap_ip, result["fileop"])

    # ── Client Operations ──────────────────────────────────────────────
    if client_payload:
        ok, msg = _api_post(ap_ip, "/device/traffic/client/config", client_payload)
        n = len(client_payload["clientoperation"])
        result["client"] = f"ok ({n} sessions)" if ok else f"FAIL: {msg}"
    else:
        result["client"] = "skipped – no client-op endpoints"

    log.info("[%s] client  → %s", ap_ip, result["client"])
    return result

# ─── Virtual-client config loading ───────────────────────────────────────────

_RADIO_LABEL = {0: "2.4G", 1: "5G", 2: "6G"}


def build_clients() -> list[dict]:
    """
    Build the veth interface list from RADIO_*_CLIENTS counts.
    Radio→veth mapping:
        radio 0 (2.4 GHz) → veth_in_0_1 .. _<RADIO_2_4G_CLIENTS>
        radio 1 (5 GHz)   → veth_in_1_1 .. _<RADIO_5G_CLIENTS>
        radio 2 (6 GHz)   → veth_in_2_1 .. _<RADIO_6G_CLIENTS>
    Each count is clamped to [0, MAX_CLIENTS_PER_RADIO].
    """
    counts = {
        0: max(0, min(RADIO_2_4G_CLIENTS, MAX_CLIENTS_PER_RADIO)),
        1: max(0, min(RADIO_5G_CLIENTS,   MAX_CLIENTS_PER_RADIO)),
        2: max(0, min(RADIO_6G_CLIENTS,   MAX_CLIENTS_PER_RADIO)),
    }
    for radio, requested in (
        (0, RADIO_2_4G_CLIENTS),
        (1, RADIO_5G_CLIENTS),
        (2, RADIO_6G_CLIENTS),
    ):
        if requested != counts[radio]:
            log.warning(
                "%s client count %d clamped to %d (max %d)",
                _RADIO_LABEL[radio], requested, counts[radio], MAX_CLIENTS_PER_RADIO,
            )
    return [
        {"interface": f"veth_in_{radio}_{c}", "ipv4": ""}
        for radio, n in counts.items()
        for c in range(1, n + 1)
    ]

# ─── CLI ──────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Configure WifiAgent stress traffic on Qwrap APs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Virtual-client selection is controlled by the RADIO_2_4G_CLIENTS, "
            "RADIO_5G_CLIENTS and RADIO_6G_CLIENTS constants at the top of this "
            "file (each 0..28). Radio 0 → 2.4G, radio 1 → 5G, radio 2 → 6G."
        ),
    )
    p.add_argument(
        "--workers", type=int, default=5,
        help="Parallel worker threads (default: 5)",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Build payloads and write them to --out instead of POSTing to APs",
    )
    p.add_argument(
        "--out", default="traffic_payloads.json", metavar="FILE",
        help="Output file for --dry-run (default: traffic_payloads.json)",
    )
    p.add_argument("--debug", action="store_true", help="Enable debug logging")
    return p.parse_args()

# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    args = _parse_args()
    if args.debug:
        log.setLevel(logging.DEBUG)

    clients = build_clients()
    if not clients:
        log.error("All RADIO_*_CLIENTS are 0 — nothing to configure.")
        sys.exit(1)
    log.info(
        "Per-AP virtual clients: %d total  (2.4G=%d, 5G=%d, 6G=%d)",
        len(clients),
        min(RADIO_2_4G_CLIENTS, MAX_CLIENTS_PER_RADIO),
        min(RADIO_5G_CLIENTS,   MAX_CLIENTS_PER_RADIO),
        min(RADIO_6G_CLIENTS,   MAX_CLIENTS_PER_RADIO),
    )
    ap_clients: dict[str, list[dict]] = {ip: clients for ip in AP_IPS}

    # Resolve discovery URL per AP and fetch each unique URL only once.
    ap_url: dict[str, str] = {}
    for ip in AP_IPS:
        url = _discovery_url_for(ip)
        if not url:
            log.error("No discovery URL configured for AP %s — add its prefix to DISCOVERY_URLS", ip)
            sys.exit(1)
        ap_url[ip] = url

    endpoints_cache: dict[str, dict[str, list]] = {}
    for url in sorted(set(ap_url.values())):
        endpoints_cache[url] = fetch_endpoints(url)

    ap_endpoints: dict[str, dict[str, list]] = {ip: endpoints_cache[ap_url[ip]] for ip in AP_IPS}

    # Show per-AP client counts + which discovery endpoint feeds it
    for ip in AP_IPS:
        log.info("  %-18s  %d virtual client(s)  via %s",
                 ip, len(ap_clients[ip]), ap_url[ip])

    # ── Dry-run: build payloads and dump to file, no API calls ─────────
    if args.dry_run:
        log.info("DRY-RUN: building payloads, will write to %s", args.out)
        bundle: dict[str, dict] = {}
        for ip in AP_IPS:
            fileop_payload, client_payload = build_ap_payloads(ap_clients[ip], ap_endpoints[ip])
            bundle[ip] = {
                "fileop_url": f"http://{ip}:{WIFIAGENT_PORT}/device/traffic/fileop/config",
                "client_url": f"http://{ip}:{WIFIAGENT_PORT}/device/traffic/client/config",
                "fileop_payload": fileop_payload,
                "client_payload": client_payload,
                "fileop_session_count": len(fileop_payload["fileoperations"]) if fileop_payload else 0,
                "client_session_count": len(client_payload["clientoperation"]) if client_payload else 0,
            }
            log.info(
                "  [%s] fileop=%d  client=%d  sessions",
                ip, bundle[ip]["fileop_session_count"], bundle[ip]["client_session_count"],
            )
        with open(args.out, "w") as fh:
            json.dump(bundle, fh, indent=2)
        log.info("Wrote %d AP payload(s) → %s", len(bundle), args.out)
        return

    log.info("Configuring %d APs (workers=%d) …", len(AP_IPS), args.workers)
    summary: list[dict] = []

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(
                configure_ap,
                ip,
                ap_clients[ip],
                ap_endpoints[ip],
            ): ip
            for ip in AP_IPS
        }
        for fut in as_completed(futures):
            try:
                summary.append(fut.result())
            except Exception as exc:
                ip = futures[fut]
                summary.append({
                    "ap":     ip,
                    "fileop": f"ERROR: {exc}",
                    "client": f"ERROR: {exc}",
                })

    # ── Summary table ──────────────────────────────────────────────────
    W = 20
    print("\n" + "─" * (W * 3 + 2))
    print(f"{'AP IP':<{W}}  {'FileOp':<{W}}  {'Client'}")
    print("─" * (W * 3 + 2))
    for r in sorted(summary, key=lambda x: x["ap"]):
        print(f"{r['ap']:<{W}}  {r.get('fileop','—'):<{W}}  {r.get('client','—')}")
    print("─" * (W * 3 + 2) + "\n")


if __name__ == "__main__":
    main()