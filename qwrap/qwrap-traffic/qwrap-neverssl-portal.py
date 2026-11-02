#!/usr/bin/env python3
"""
HTTP periodic traffic configuration (neverssl.com) for WifiAgent Qwrap APs.

Configures an HTTP GET session targeting 'http://neverssl.com' every 61 seconds
per virtual-client interface (veth_in_<radio>_<index>).

By default, this script queries GET /device/traffic/client/profile/get to retrieve
existing sessions and appends the neverssl sessions without overwriting.
Use --override to replace the configuration completely.

Radio -> veth mapping:
    radio 0 -> 2.4 GHz  -> veth_in_0_*
    radio 1 -> 5 GHz    -> veth_in_1_*
    radio 2 -> 6 GHz    -> veth_in_2_*

Usage:
  python3 qwrap-neverssl-config.py [--ap HOST[,HOST...]] [--override] [--dry-run] [--out FILE] [--debug]
"""

import argparse
import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ─── Traffic Configuration ───────────────────────────────────────────────────

TARGET_URL = "http://neverssl.com"
INTERVAL_SECONDS = 61  # Must be > 60s per API documentation
DSCP_VALUE = 0
WIFIAGENT_PORT = 8083
TIMEOUT = 60  # seconds per agent request
MAX_VETH_PER_RADIO = 28

"""
DEFAULT_BAND_CFG:
  radios     : list of radios to configure -> [0], [0,1], [0,1,2] ...
                radio 0 = 2.4 GHz, radio 1 = 5 GHz, radio 2 = 6 GHz
  veth_count : how many veth_in_<radio>_* interfaces to configure (1..28)
  ip_mode    : "IPv4" | "IPv6"
"""
DEFAULT_BAND_CFG = {
    "radios": [0,1],
    "veth_count": 28,
    "ip_mode": "IPv6",
}

# AP Inventory
AP_LIST: list[str] = [
    # # S1 qwrap:
    # '10.86.205.122',
    # '10.86.204.204',
    # '10.86.205.60',
    # '10.86.205.223',
    # '10.86.204.227',
    # '10.86.205.165',
    # # For S2 QWRAP
    '10.86.205.82',
    '10.86.205.240',
    '10.86.205.157',
    '10.86.205.90',
    '10.86.205.88',
    '10.86.205.49',
    # # For S3 Qwrap - ABZ
    # '10.87.169.175',
    # '10.87.169.130',
    # '10.87.169.87',
    # '10.87.169.17',
    # '10.87.169.113',
    # '10.87.169.112',
    # '10.87.169.243',
]

_RADIO_LABEL = {0: "2.4G", 1: "5G", 2: "6G"}

# ─── Logging Setup ────────────────────────────────────────────────────────────

_log_handler = logging.StreamHandler()
_log_handler.setFormatter(
    logging.Formatter(
        fmt="%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S"
    )
)
logging.basicConfig(level=logging.INFO, handlers=[_log_handler])
log = logging.getLogger("neverssl_traffic")

# ─── HTTP Connection to WifiAgent ─────────────────────────────────────────────

def _make_http_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=3, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504]
    )
    s.mount("http://", HTTPAdapter(max_retries=retry))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s

_HTTP = _make_http_session()


def _api_get(ap_ip: str, path: str) -> tuple[bool, dict | list | str]:
    """Fetch endpoint data from WifiAgent."""
    url = f"http://{ap_ip}:{WIFIAGENT_PORT}{path}"
    try:
        r = _HTTP.get(url, timeout=TIMEOUT)
    except Exception as exc:
        return False, f"request error: {exc}"

    if not (200 <= r.status_code < 300):
        return False, f"HTTP {r.status_code}: {r.text[:200]}"

    try:
        return True, r.json()
    except ValueError:
        return True, r.text


def _api_post(ap_ip: str, path: str, body: dict) -> tuple[bool, str]:
    """Post configuration payload to WifiAgent."""
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
        ok = 200 <= r.status_code < 300
        return ok, f"HTTP {r.status_code}: {text[:300]!r}"

    if data.get("status") == "success":
        return True, "success"
    if 200 <= r.status_code < 300 and "status" not in data:
        return True, f"HTTP {r.status_code} (no status field)"
    msg = data.get("message") or repr(data)
    return False, f"HTTP {r.status_code}: {msg}"

# ─── Robust Existing Config Extraction ────────────────────────────────────────

def _extract_sessions(obj) -> list[dict]:
    """Extract list of existing client sessions from the response payload."""
    if isinstance(obj, dict):
        # Direct check for 'clientoperation' per documentation
        if "clientoperation" in obj and isinstance(obj["clientoperation"], list):
            return obj["clientoperation"]
        # Nested check in 'data' or 'result' envelopes
        for key in ("data", "result", "profile"):
            if key in obj and isinstance(obj[key], dict):
                sub = obj[key].get("clientoperation")
                if isinstance(sub, list):
                    return sub
        # Recursive check across all dictionary values
        for v in obj.values():
            found = _extract_sessions(v)
            if found:
                return found
    elif isinstance(obj, list):
        if obj and isinstance(obj[0], dict) and "traffictype" in obj[0]:
            return obj
        for item in obj:
            found = _extract_sessions(item)
            if found:
                return found
    return []


def _fetch_existing_client_sessions(ap_ip: str) -> list[dict]:
    """Retrieve existing client sessions via GET /device/traffic/client/profile/get."""
    endpoint = "/device/traffic/client/profile/get"
    ok, res = _api_get(ap_ip, endpoint)
    if not ok or not res:
        log.warning("[%s] Failed to query GET %s: %s", ap_ip, endpoint, res)
        return []

    log.debug("[%s] Response from %s: %s", ap_ip, endpoint, str(res)[:300])
    sessions = _extract_sessions(res)
    if sessions:
        log.info("[%s] Successfully retrieved %d active client sessions.", ap_ip, len(sessions))
        return sessions

    log.info("[%s] Endpoint %s returned no active sessions.", ap_ip, endpoint)
    return []

# ─── Client Interface & Session Builders ──────────────────────────────────────

def build_clients(band_cfg: dict) -> list[dict]:
    """Build the veth interface list respecting radios, clamped veth_count, and ip_mode."""
    requested = band_cfg.get("veth_count", 0)
    clamped_count = max(0, min(requested, MAX_VETH_PER_RADIO))
    if clamped_count != requested:
        log.warning(
            "veth_count %d clamped to %d (max %d)",
            requested,
            clamped_count,
            MAX_VETH_PER_RADIO,
        )

    ip_mode = band_cfg.get("ip_mode", "IPv4")
    if ip_mode not in ("IPv4", "IPv6"):
        log.warning("Invalid ip_mode '%s', defaulting to IPv4", ip_mode)
        ip_mode = "IPv4"

    clients = []
    for radio in band_cfg.get("radios", []):
        for c in range(1, clamped_count + 1):
            clients.append({
                "interface": f"veth_in_{radio}_{c}",
                "radio": radio,
                "network": ip_mode,
            })
    return clients


def build_neverssl_session(interface: str, network: str) -> dict:
    """Build a WifiAgent HTTP GET session to http://neverssl.com with network ip_mode."""
    return {
        "status": "enable",
        "traffictype": "HTTP",
        "host": TARGET_URL,
        "operation": "GET",
        "filename": "",
        "interval": INTERVAL_SECONDS,
        "datasize": 0,
        "packetsize": 1400,
        "connectioninterval": 0,
        "network": network,
        "dscpvalue": DSCP_VALUE,
        "interface": interface,
        "traffic_schedule": {},
    }


def build_ap_payload(clients: list[dict], existing_sessions: list[dict] | None = None) -> dict:
    """Build payload, combining existing sessions with the new neverssl sessions."""
    new_sessions = [
        build_neverssl_session(c["interface"], c["network"]) for c in clients
    ]
    
    # Avoid duplicate neverssl sessions if re-running without --override
    filtered_existing = []
    for s in (existing_sessions or []):
        # Keep existing non-neverssl sessions, or sessions on interfaces not managed in this run
        managed_interfaces = {c["interface"] for c in clients}
        if s.get("interface") in managed_interfaces and s.get("host") == TARGET_URL:
            continue
        filtered_existing.append(s)

    all_sessions = filtered_existing + new_sessions
    return {"status": "enable", "clientoperation": all_sessions}


def configure_ap(ap_ip: str, clients: list[dict], override: bool) -> dict:
    result = {"ap": ap_ip}
    existing_sessions = []

    if not override:
        existing_sessions = _fetch_existing_client_sessions(ap_ip)
        if existing_sessions:
            log.info(
                "[%s] Preserving %d existing sessions, appending %d new sessions.",
                ap_ip,
                len(existing_sessions),
                len(clients),
            )

    payload = build_ap_payload(clients, existing_sessions=existing_sessions)
    ok, msg = _api_post(ap_ip, "/device/traffic/client/config", payload)

    total = len(payload["clientoperation"])
    added = len(clients)
    if ok:
        mode_str = (
            f"overwritten ({total} sessions)"
            if override
            else f"appended (+{added}, total {total})"
        )
        result["client"] = f"ok ({mode_str})"
    else:
        result["client"] = f"FAIL: {msg}"

    log.info("[%s] client -> %s", ap_ip, result["client"])
    return result

# ─── Concurrency ──────────────────────────────────────────────────────────────

def runConcurrently(func, apList: list[str], actionName: str) -> dict:
    errors = []
    results = {}
    with ThreadPoolExecutor(max_workers=len(apList)) as executor:
        futureToHost = {executor.submit(func, ip): ip for ip in apList}
        for future in as_completed(futureToHost):
            host = futureToHost[future]
            try:
                results[host] = future.result()
                log.info("%s: %s succeeded", host, actionName)
            except Exception as e:
                log.error("%s: %s FAILED: %s", host, actionName, e)
                errors.append(host)

    if errors:
        log.error("%s failed on: %s", actionName, errors)
        sys.exit(1)
    return results

# ─── CLI & Entrypoint ─────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="qwrap-neverssl-config.py",
        description="Configure periodic HTTP GET to http://neverssl.com on Qwrap APs",
    )
    p.add_argument(
        "--ap",
        metavar="HOST[,HOST...]",
        default=None,
        help="Comma-separated list of target AP IPs (default: all APs in AP_LIST)",
    )
    p.add_argument(
        "--override",
        action="store_true",
        help="Overwrite existing configuration instead of appending on top of it",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Output payload JSON without POSTing to APs",
    )
    p.add_argument(
        "--out",
        default="neverssl_payloads.json",
        metavar="FILE",
        help="Output file for --dry-run (default: neverssl_payloads.json)",
    )
    p.add_argument("--debug", action="store_true", help="Enable debug logging")
    return p.parse_args()


def resolveApList(args: argparse.Namespace) -> list[str]:
    if not args.ap:
        return AP_LIST
    requested_hosts = [h.strip() for h in args.ap.split(",") if h.strip()]
    missing = sorted(set(requested_hosts) - set(AP_LIST))
    if missing:
        log.error("Host(s) not found in AP_LIST: %s", missing)
        sys.exit(1)
    return requested_hosts


def main() -> None:
    args = _parse_args()
    if args.debug:
        log.setLevel(logging.DEBUG)

    print("-" * 50)
    print("Resolve target AP(s) and client configuration")
    print("-" * 50)

    ap_ips = resolveApList(args)
    clients = build_clients(DEFAULT_BAND_CFG)
    if not clients:
        log.error(
            "No virtual clients configured. Check DEFAULT_BAND_CFG (radios/veth_count)."
        )
        sys.exit(1)

    radio_breakdown = ", ".join(
        f"{_RADIO_LABEL.get(r, str(r))}={min(DEFAULT_BAND_CFG['veth_count'], MAX_VETH_PER_RADIO)}v"
        for r in sorted(DEFAULT_BAND_CFG.get("radios", []))
    )

    log.info("Target AP count : %d", len(ap_ips))
    log.info(
        "Clients per AP  : %d total (%s) | Network: %s",
        len(clients),
        radio_breakdown,
        DEFAULT_BAND_CFG["ip_mode"],
    )
    log.info("Session Target  : %s (every %ds)", TARGET_URL, INTERVAL_SECONDS)
    log.info(
        "Config Mode     : %s",
        "OVERWRITE (--override passed)" if args.override else "APPEND (default)",
    )

    if args.dry_run:
        print("\n" + "-" * 50)
        print("Dry Run: Writing payloads")
        print("-" * 50)
        log.info("DRY-RUN mode enabled: writing payloads to %s", args.out)
        payload = build_ap_payload(clients)
        bundle = {
            ip: {
                "client_url": f"http://{ip}:{WIFIAGENT_PORT}/device/traffic/client/config",
                "client_payload": payload,
                "session_count": len(payload["clientoperation"]),
                "note": "Dry run generates new session payload without modifying AP state",
            }
            for ip in ap_ips
        }
        with open(args.out, "w") as fh:
            json.dump(bundle, fh, indent=2)
        log.info("Saved payloads for %d APs -> %s", len(bundle), args.out)
        return

    print("\n" + "-" * 50)
    print("Pushing config to AP(s) concurrently")
    print("-" * 50)
    results = runConcurrently(
        lambda ip: configure_ap(ip, clients, override=args.override),
        ap_ips,
        "configure",
    )

    # Output Summary Table
    print("\n" + "=" * 55)
    print(f"{'AP IP':<25}  {'Result':<25}")
    print("=" * 55)
    for r in sorted(results.values(), key=lambda x: x["ap"]):
        print(f"{r['ap']:<25}  {r.get('client', '—'):<25}")
    print("=" * 55 + "\n")


if __name__ == "__main__":
    main()