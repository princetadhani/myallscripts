#!/usr/bin/env python3
"""
Fully-configurable stress/stability traffic configuration for WifiAgent for Qwrap APs.

Everything is controlled from the "Traffic configuration" section below —
no CLI flags are needed for AP list, radio counts, session counts or IP mode.

Radio -> veth mapping (fixed, do not change):
    radio 0 -> 2.4 GHz  -> veth_in_0_*
    radio 1 -> 5 GHz    -> veth_in_1_*
    radio 2 -> 6 GHz    -> veth_in_2_*

Usage:
  python3 qwrap-traffic-config-dynamic-input.py [--ap HOST[,HOST...]] [--dry-run] [--debug]


py /Users/prince.tadhani/myallscripts/qwrap-traffic/qwrap-traffic-config-dynamic-input.py --ap 10.86.205.122,10.86.204.204,10.86.205.60,10.86.205.223,10.86.204.227,10.86.205.165

py /Users/prince.tadhani/myallscripts/qwrap-traffic/qwrap-traffic-config-dynamic-input.py --ap 10.86.205.82,10.86.205.240,10.86.205.157,10.86.205.90,10.86.205.88,10.86.205.49

py /Users/prince.tadhani/myallscripts/qwrap-traffic/qwrap-traffic-config-dynamic-input.py --ap 10.87.169.175,10.87.169.130,10.87.169.87,10.87.169.17,10.87.169.113,10.87.169.112,10.87.169.243
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

"""
─── Traffic configuration ───────────────────────────────────────────────
Everything you need to change day-to-day lives in this block.

DEFAULT_MODE:
  1 -> every AP in AP_LIST uses DEFAULT_BAND_CFG for the radios listed
       in DEFAULT_BAND_CFG["radios"]. Simplest option — one config for all.

  0 -> every AP uses its own per-radio config from AP_CONFIG.
       Any radio (0/1/2) NOT listed under an AP's "bands" is skipped
       entirely for that AP (no veth interfaces configured on it).
"""
DEFAULT_MODE = 0

"""
─── DEFAULT_BAND_CFG (used only when DEFAULT_MODE = 1) ─────────────────
  radios         : list of radios to configure -> [0], [0,1], [0,1,2] ...
                    radio 0 = 2.4 GHz, radio 1 = 5 GHz, radio 2 = 6 GHz
  veth_count     : how many veth_in_<radio>_* interfaces to configure
                    (1..28, clamped to MAX_VETH_PER_RADIO)
  fileop_count   : number of file-op sessions per veth interface
                    (protocol randomly sftp/ftp/tftp from discovery)
  clientop_count : number of client-op sessions per veth interface
                    (protocol randomly QUICT/TCPT/HTTP/HTTPS from discovery)
  ip_mode        : "IPv4" | "IPv6" | "Dual"
                    Dual = each session randomly picks IPv4 or IPv6
  target_type    : "hostname" | "ip"
                    which discovery pool (hostnames vs raw IPs) is used

─── Blank skeleton — copy/paste this and fill in the values ────────────
DEFAULT_BAND_CFG = {
    "radios":         [],
    "veth_count":     ,
    "fileop_count":   ,
    "clientop_count": ,
    "ip_mode":        "",
    "target_type":    "",
}
"""

DEFAULT_BAND_CFG = {
    "radios":         [0 ,1, 2],
    "veth_count":     28,
    "fileop_count":   4,
    "clientop_count": 3,
    "ip_mode":        "IPv6",
    "target_type":    "hostname",
}

MAX_VETH_PER_RADIO = 28

# ─── AP_LIST (used only when DEFAULT_MODE = 1) ──────────────────────────
# Plain list of AP IPs — add/remove/comment out lines as needed.
AP_LIST: list[str] = [
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

"""
─── AP_CONFIG (used only when DEFAULT_MODE = 0) ─────────────────────────
Full per-AP, per-radio control. Same fields as DEFAULT_BAND_CFG above,
minus "radios" (the radio number is the dict key itself: 0, 1 or 2).
Any radio key you don't list is skipped for that AP.

Template to copy per radio:
  <radio>: {"veth_count": 28, "fileop_count": 2, "clientop_count": 2, "ip_mode": "IPv4/IPv6/Dual", "target_type": "ip / hostname"},

─── Blank skeleton — copy/paste this per AP and fill in the values ─────
Add/remove radio lines (0/1/2) as needed; an omitted radio is skipped.
Copy from here and paste it:

"ap_ip": {
    "bands": {
            0: {"veth_count": , "fileop_count": , "clientop_count": , "ip_mode": "", "target_type": ""},
            1: {"veth_count": , "fileop_count": , "clientop_count": , "ip_mode": "", "target_type": ""},
            2: {"veth_count": , "fileop_count": , "clientop_count": , "ip_mode": "", "target_type": ""},
        }
    },


"""
AP_CONFIG: dict[str, dict] = {
# AP-1 1-C430--00923F--S1-QWRAP 
"10.86.205.122": {
    "bands": {
            0: {"veth_count": 28, "fileop_count": 1, "clientop_count": 3, "ip_mode": "IPv4", "target_type": "ip"},
            1: {"veth_count": 28, "fileop_count": 2, "clientop_count": 3, "ip_mode": "IPv4", "target_type": "hostname"},
            2: {"veth_count": 28, "fileop_count": 2, "clientop_count": 3, "ip_mode": "IPv4", "target_type": "hostname"},
        }
    },

# AP-2: 2-C430--007F7F--S1-QWRAP
"10.86.204.204": {
    "bands": {
            0: {"veth_count": 28, "fileop_count": 1, "clientop_count": 2, "ip_mode": "IPv4", "target_type": "hostname"},
            1: {"veth_count": 28, "fileop_count": 2, "clientop_count": 3, "ip_mode": "IPv4", "target_type": "hostname"},
            2: {"veth_count": 28, "fileop_count": 2, "clientop_count": 3, "ip_mode": "IPv4", "target_type": "hostname"},
        }
    },

# AP-3 3-C430--00978F--S1-QWRAP
"10.86.205.60": {
    "bands": {
            0: {"veth_count": 28, "fileop_count": 1, "clientop_count": 1, "ip_mode": "IPv4", "target_type": "hostname"},
            1: {"veth_count": 28, "fileop_count": 2, "clientop_count": 2, "ip_mode": "IPv4", "target_type": "hostname"},
            2: {"veth_count": 28, "fileop_count": 2, "clientop_count": 3, "ip_mode": "IPv4", "target_type": "hostname"},
        }
    },

# AP-4 1-C460D--C2755F--S1-QWRAP
"10.86.205.223": {
    "bands": {
            0: {"veth_count": 28, "fileop_count": 2, "clientop_count": 3, "ip_mode": "IPv4", "target_type": "hostname"},
            1: {"veth_count": 28, "fileop_count": 3, "clientop_count": 4, "ip_mode": "IPv4", "target_type": "hostname"},
            2: {"veth_count": 28, "fileop_count": 3, "clientop_count": 4, "ip_mode": "IPv4", "target_type": "hostname"},
        }
    },

# AP-5 2-C460D--C2714F--S1-QWRAP
"10.86.204.227": {
    "bands": {
            0: {"veth_count": 28, "fileop_count": 2, "clientop_count": 2, "ip_mode": "IPv4", "target_type": "hostname"},
            1: {"veth_count": 28, "fileop_count": 3, "clientop_count": 4, "ip_mode": "IPv4", "target_type": "hostname"},
            2: {"veth_count": 28, "fileop_count": 4, "clientop_count": 4, "ip_mode": "IPv4", "target_type": "hostname"},
        }
    },
# AP-6 1-O405--201EFF--S1-QWRAP
"10.86.205.165": {
    "bands": {
            0: {"veth_count": 28, "fileop_count": 2, "clientop_count": 2, "ip_mode": "IPv4", "target_type": "hostname"},
            1: {"veth_count": 28, "fileop_count": 3, "clientop_count": 4, "ip_mode": "IPv4", "target_type": "hostname"},
            2: {"veth_count": 28, "fileop_count": 3, "clientop_count": 4, "ip_mode": "IPv4", "target_type": "hostname"},
        }
    },
#========================================================================================================================
# AP-1 1-C400--F031BF--S2-QWRAP
"10.86.205.82": {
    "bands": {
            0: {"veth_count": 28, "fileop_count": 2, "clientop_count": 2, "ip_mode": "IPv6", "target_type": "hostname"},
            1: {"veth_count": 28, "fileop_count": 3, "clientop_count": 4, "ip_mode": "IPv6", "target_type": "hostname"},
            2: {"veth_count": 28, "fileop_count": 3, "clientop_count": 4, "ip_mode": "IPv6", "target_type": "hostname"},
        }
    },

# AP-2 1-C430--05D47F--S2-QWRAP
"10.86.205.240": {
    "bands": {
            0: {"veth_count": 28, "fileop_count": 2, "clientop_count": 3, "ip_mode": "IPv6", "target_type": "hostname"},
            1: {"veth_count": 28, "fileop_count": 2, "clientop_count": 4, "ip_mode": "IPv6", "target_type": "hostname"},
            2: {"veth_count": 28, "fileop_count": 3, "clientop_count": 3, "ip_mode": "IPv6", "target_type": "hostname"},
        }
    },

# Ap-3 1-C460D--C2750F--S2-QWRAP
"10.86.205.157": {
    "bands": {
            0: {"veth_count": 28, "fileop_count": 2, "clientop_count": 3, "ip_mode": "IPv6", "target_type": "hostname"},
            1: {"veth_count": 28, "fileop_count": 3, "clientop_count": 4, "ip_mode": "IPv6", "target_type": "hostname"},
            2: {"veth_count": 28, "fileop_count": 4, "clientop_count": 4, "ip_mode": "IPv6", "target_type": "hostname"},
        }
    },

# AP-4 2-C460D--C274BF--S2-QWRAP
"10.86.205.90": {
    "bands": {
            0: {"veth_count": 28, "fileop_count": 1, "clientop_count": 4, "ip_mode": "IPv6", "target_type": "hostname"},
            1: {"veth_count": 28, "fileop_count": 4, "clientop_count": 4, "ip_mode": "IPv6", "target_type": "hostname"},
            2: {"veth_count": 28, "fileop_count": 4, "clientop_count": 4, "ip_mode": "IPv6", "target_type": "hostname"},
        }
    },

# AP-5 3-C460D--C27D2F--S2-QWRAP
"10.86.205.88": {
    "bands": {
            0: {"veth_count": 28, "fileop_count": 3, "clientop_count": 4, "ip_mode": "IPv6", "target_type": "hostname"},
            1: {"veth_count": 28, "fileop_count": 5, "clientop_count": 4, "ip_mode": "IPv6", "target_type": "hostname"},
            2: {"veth_count": 28, "fileop_count": 4, "clientop_count": 5, "ip_mode": "IPv6", "target_type": "hostname"},
        }
    },

# AP-6 1-O405--20203F--S2-QWRAP
"10.86.205.49": {
    "bands": {
            0: {"veth_count": 28, "fileop_count": 2, "clientop_count": 3, "ip_mode": "IPv6", "target_type": "hostname"},
            1: {"veth_count": 28, "fileop_count": 3, "clientop_count": 4, "ip_mode": "IPv6", "target_type": "hostname"},
            2: {"veth_count": 28, "fileop_count": 3, "clientop_count": 4, "ip_mode": "IPv6", "target_type": "hostname"},
        }
    },
}

# ─── Discovery URLs / connection settings ────────────────────────────────
DISCOVERY_URLS: dict[str, str] = {
    "10.87": "http://pune-abz-traffic-enpoint.dt1.wifi.arista.cloud/api/discovery",
    "10.86": "http://pune-traffic-enpoint.dt1.wifi.arista.cloud/api/discovery",
    "10.85": "http://blr-traffic-endpoint.dt1.wifi.arista.cloud/api/discovery",
    "10.81": "http://hq-traffic-endpoint.dt1.wifi.arista.cloud/api/discovery",
    "10.76": "http://hq-traffic-endpoint.dt1.wifi.arista.cloud/api/discovery",
}

WIFIAGENT_PORT = 8083
TIMEOUT = 60  # seconds per request

_RADIO_LABEL = {0: "2.4G", 1: "5G", 2: "6G"}

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

# ─── Logging ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("traffic_config")

# ─── HTTP session (shared) ────────────────────────────────────────────────
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


def _discovery_url_for(ap_ip: str) -> str | None:
    for prefix, url in DISCOVERY_URLS.items():
        if ap_ip.startswith(prefix + "."):
            return url
    return None


# ─── Discovery ────────────────────────────────────────────────────────────

def fetch_endpoints(discovery_url: str) -> dict:
    """
    Returns a dual-stack endpoint pool:
      {
        "v4": {"hostname": {"ftp":[], "sftp":[], "tftp":[], "quic":[], "tcp":[], "http":[], "https":[]},
               "ip":       {...}},
        "v6": {"hostname": {...}, "ip": {...}},
      }
    Indexed as endpoints[ver][target_type][protocol] -> list of endpoint dicts.
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

    _protos = ["ftp", "sftp", "tftp", "quic", "tcp", "http", "https"]
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
                        pools[ver][ttype]["http"].append({"base_url": f"http://{host}:{port}"})
                    for port in _ports("HTTPS"):
                        pools[ver][ttype]["https"].append({"base_url": f"https://{host}:{port}"})

    log.info("Endpoint pool (protocol : v4/hostname, v4/ip, v6/hostname, v6/ip):")
    for p in _protos:
        log.info(
            "  %-6s : v4/hostname=%-5d  v4/ip=%-5d  v6/hostname=%-5d  v6/ip=%-5d",
            p,
            len(pools["v4"]["hostname"][p]),
            len(pools["v4"]["ip"][p]),
            len(pools["v6"]["hostname"][p]),
            len(pools["v6"]["ip"][p]),
        )
    return pools


# ─── IP version resolution ────────────────────────────────────────────────

def _resolve_version(ip_mode: str) -> str:
    """Resolve "Dual" to a concrete version per call; pass IPv4/IPv6 through."""
    if ip_mode == "Dual":
        return random.choice(["IPv4", "IPv6"])
    return ip_mode


def _pool_key(version: str) -> str:
    return "v4" if version == "IPv4" else "v6"


# ─── Random helpers ───────────────────────────────────────────────────────

def _rnd_dscp()  -> int: return random.choice(DSCP_VALUES)
def _rnd_pkt()   -> int: return random.choice(PACKET_SIZES)
def _rnd_fsize() -> int: return random.choice(FILE_SIZES)
def _rnd_dsize() -> int: return random.choice(DATA_SIZES)
def _rnd_fival() -> int: return random.choice(FILE_IVALS)
def _rnd_cival() -> int: return random.choice(CLIENT_IVALS)
def _rnd_conn()  -> int: return random.choice(CONN_IVALS)


# ─── Session builders ─────────────────────────────────────────────────────

def _pool_for(endpoints: dict, band_cfg: dict) -> tuple[dict, str]:
    """Resolve (pool, version) for one session using band_cfg's ip_mode/target_type.
    Falls back to v4/hostname if the resolved pool is completely empty."""
    version = _resolve_version(band_cfg["ip_mode"])
    ver_key = _pool_key(version)
    ttype   = band_cfg["target_type"]
    pool    = endpoints[ver_key][ttype]
    if not any(pool.values()):
        pool, version = endpoints["v4"]["hostname"], "IPv4"
    return pool, version


def build_fileop_sessions(interface: str, endpoints: dict, band_cfg: dict) -> list[dict]:
    """Build band_cfg["fileop_count"] file-op sessions for one veth interface."""
    sessions: list[dict] = []
    for _ in range(band_cfg["fileop_count"]):
        pool, version = _pool_for(endpoints, band_cfg)
        available = [p for p in ("sftp", "ftp", "tftp") if pool[p]]
        if not available:
            log.warning("No file-op endpoints available for %s; skipping one fileop session", interface)
            continue

        proto = random.choice(available)
        ep    = random.choice(pool[proto])
        op    = random.choice(["upload", "download"])

        session: dict = {
            "status":      "enable",
            "traffictype": proto,
            "host":        ep["host"],
            "operation":   op,
            "filesize":    _rnd_fsize(),
            "packetsize":  _rnd_pkt(),
            "interval":    _rnd_fival(),
            "network":     version,
            "dscpvalue":   _rnd_dscp(),
            "interface":   interface,
            "traffic_schedule": {},
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


def _build_quic_or_tcp_session(interface: str, proto: str, ep: dict, version: str) -> dict:
    return {
        "status":             "enable",
        "traffictype":        "QUICT" if proto == "quic" else "TCPT",
        "host":               ep["host"],
        "port":               ep["port"],
        "operation":          random.choice(["upload", "download"]),
        "interval":           _rnd_cival(),
        "datasize":           _rnd_dsize(),
        "packetsize":         _rnd_pkt(),
        "connectioninterval": _rnd_conn(),
        "network":            version,
        "dscpvalue":          _rnd_dscp(),
        "interface":          interface,
        "traffic_schedule":   {},
    }


def _build_http_session(interface: str, proto: str, ep: dict, version: str) -> dict:
    op       = random.choice(["upload", "download"])
    http_op  = "POST" if op == "upload" else "GET"
    url_path = "/wifiagent/upload" if op == "upload" else "/wifiagent/download"
    fsize    = _rnd_fsize()
    pkt      = _rnd_pkt()
    if op == "download":
        filename = f"wifiagent_download_{fsize}MB_{pkt}PS.txt"
    else:
        filename = f"wifiagent_upload_{fsize}MB_{pkt}PS_{random.randint(100000, 999999)}.txt"

    return {
        "status":             "enable",
        "traffictype":        proto.upper(),
        "host":               ep["base_url"] + url_path,
        "operation":          http_op,
        "filename":           filename,
        "interval":           _rnd_cival(),
        "datasize":           fsize,
        "packetsize":         pkt,
        "connectioninterval": _rnd_conn(),
        "network":            version,
        "dscpvalue":          _rnd_dscp(),
        "interface":          interface,
        "traffic_schedule":   {},
    }


def build_client_sessions(interface: str, endpoints: dict, band_cfg: dict) -> list[dict]:
    """Build band_cfg["clientop_count"] client-op sessions for one veth interface."""
    sessions: list[dict] = []
    for _ in range(band_cfg["clientop_count"]):
        pool, version = _pool_for(endpoints, band_cfg)
        available = [p for p in ("http", "https", "quic", "tcp") if pool[p]]
        if not available:
            log.warning("No client-op endpoints available for %s; skipping one clientop session", interface)
            continue

        proto = random.choice(available)
        ep    = random.choice(pool[proto])
        if proto in ("quic", "tcp"):
            sessions.append(_build_quic_or_tcp_session(interface, proto, ep, version))
        else:
            sessions.append(_build_http_session(interface, proto, ep, version))
    return sessions


# ─── Per-AP payload assembly ──────────────────────────────────────────────

def build_ap_payloads(
    clients: list[dict], endpoints: dict
) -> tuple[dict | None, dict | None, dict[int, int], dict[int, int]]:
    """clients: [{"interface": "veth_in_0_1", "radio": 0, "band_cfg": {...}}, ...]

    Returns (fileop_payload, client_payload, fileop_by_radio, client_by_radio),
    where *_by_radio maps radio -> actual session count built for that radio
    (used for the per-radio breakdown log line).
    """
    all_fileop: list[dict] = []
    all_client: list[dict] = []
    fileop_by_radio: dict[int, int] = {}
    client_by_radio: dict[int, int] = {}
    for c in clients:
        iface, radio, band_cfg = c["interface"], c["radio"], c["band_cfg"]
        fileop_sessions = build_fileop_sessions(iface, endpoints, band_cfg)
        client_sessions = build_client_sessions(iface, endpoints, band_cfg)
        all_fileop.extend(fileop_sessions)
        all_client.extend(client_sessions)
        fileop_by_radio[radio] = fileop_by_radio.get(radio, 0) + len(fileop_sessions)
        client_by_radio[radio] = client_by_radio.get(radio, 0) + len(client_sessions)
    fileop_payload = {"status": "enable", "fileoperations": all_fileop} if all_fileop else None
    client_payload = {"status": "enable", "clientoperation": all_client} if all_client else None
    return fileop_payload, client_payload, fileop_by_radio, client_by_radio


def _breakdown_str(counts_by_radio: dict[int, int], ap_bands: dict[int, dict], per_veth_key: str) -> str:
    """Render 'label=veth×per_veth=count, ...  (total=N)' across configured radios."""
    parts = []
    for radio in sorted(counts_by_radio):
        veth = ap_bands[radio]["veth_count"]
        per_veth = ap_bands[radio][per_veth_key]
        parts.append(f"{_RADIO_LABEL.get(radio, radio)}={veth}x{per_veth}={counts_by_radio[radio]}")
    total = sum(counts_by_radio.values())
    return ", ".join(parts) + f"  (total={total})" if parts else "(total=0)"


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


def configure_ap(ap_ip: str, clients: list[dict], endpoints: dict, ap_bands: dict[int, dict]) -> dict:
    result = {"ap": ap_ip}
    fileop_payload, client_payload, fileop_by_radio, client_by_radio = build_ap_payloads(clients, endpoints)

    if fileop_payload:
        ok, msg = _api_post(ap_ip, "/device/traffic/fileop/config", fileop_payload)
        n = len(fileop_payload["fileoperations"])
        result["fileop"] = f"ok ({n} sessions)" if ok else f"FAIL: {msg}"
    else:
        result["fileop"] = "skipped - no fileop endpoints"
    log.info("[%s] fileop  -> %s  [%s]", ap_ip, result["fileop"],
              _breakdown_str(fileop_by_radio, ap_bands, "fileop_count"))

    if client_payload:
        ok, msg = _api_post(ap_ip, "/device/traffic/client/config", client_payload)
        n = len(client_payload["clientoperation"])
        result["client"] = f"ok ({n} sessions)" if ok else f"FAIL: {msg}"
    else:
        result["client"] = "skipped - no clientop endpoints"
    log.info("[%s] client  -> %s  [%s]", ap_ip, result["client"],
              _breakdown_str(client_by_radio, ap_bands, "clientop_count"))
    return result


# ─── Config resolution (DEFAULT_MODE handling) ────────────────────────────

def _clamp_band_cfg(radio: int, band_cfg: dict) -> dict:
    cfg = dict(band_cfg)
    requested = cfg["veth_count"]
    cfg["veth_count"] = max(0, min(requested, MAX_VETH_PER_RADIO))
    if cfg["veth_count"] != requested:
        log.warning("%s veth_count %d clamped to %d (max %d)",
                    _RADIO_LABEL.get(radio, radio), requested, cfg["veth_count"], MAX_VETH_PER_RADIO)
    return cfg


def effective_ap_config() -> dict[str, dict]:
    """Build {ip: {"bands": {radio: band_cfg}}} from DEFAULT_MODE / AP_LIST / AP_CONFIG."""
    if DEFAULT_MODE == 1:
        base = {k: v for k, v in DEFAULT_BAND_CFG.items() if k != "radios"}
        return {
            ip: {"bands": {r: _clamp_band_cfg(r, base) for r in DEFAULT_BAND_CFG["radios"]}}
            for ip in AP_LIST
        }

    # DEFAULT_MODE == 0
    result: dict[str, dict] = {}
    for ip, cfg in AP_CONFIG.items():
        bands = {}
        for radio, band_cfg in cfg.get("bands", {}).items():
            bands[radio] = _clamp_band_cfg(radio, band_cfg)
        result[ip] = {"bands": bands}
    return result


def build_clients(ap_bands: dict[int, dict]) -> list[dict]:
    """Build the veth interface list for one AP from its resolved band config."""
    clients: list[dict] = []
    for radio, band_cfg in sorted(ap_bands.items()):
        for c in range(1, band_cfg["veth_count"] + 1):
            clients.append({"interface": f"veth_in_{radio}_{c}", "radio": radio, "band_cfg": band_cfg})
    return clients


# ─── CLI ────────────────────────────────────────────────────────────────────

class _HelpFormatter(argparse.RawDescriptionHelpFormatter):
    def __init__(self, prog):
        super().__init__(prog, max_help_position=40, width=200)


_EPILOG = '''\
All AP / radio / session-count / ip_mode settings are controlled in the 'Traffic configuration'
block at the top of this file (DEFAULT_MODE, DEFAULT_BAND_CFG, AP_LIST, AP_CONFIG) — no CLI flags
for those.

examples:
  python3 qwrap-traffic-config-dynamic-input.py
  python3 qwrap-traffic-config-dynamic-input.py --ap 10.86.205.157
  python3 qwrap-traffic-config-dynamic-input.py --ap 10.86.205.157,10.86.205.158
  python3 qwrap-traffic-config-dynamic-input.py --dry-run
  python3 qwrap-traffic-config-dynamic-input.py --dry-run --out payloads.json
  python3 qwrap-traffic-config-dynamic-input.py --ap 10.86.205.157 --dry-run
  python3 qwrap-traffic-config-dynamic-input.py --debug
'''


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="qwrap-traffic-config-dynamic-input.py",
        usage=argparse.SUPPRESS,
        description="Configure WifiAgent stress traffic on Qwrap APs (fully script-configured)",
        epilog=_EPILOG,
        formatter_class=_HelpFormatter,
    )
    p.add_argument("--ap", metavar="HOST[,HOST...]", default=None,
                   help="Comma-separated list of AP host/IPs to target instead of all APs in AP_LIST or AP_CONFIG.")
    p.add_argument("--dry-run", action="store_true",
                   help="Build payloads and write them to --out instead of POSTing to APs")
    p.add_argument("--out", default="traffic_payloads.json", metavar="FILE",
                   help="Output file for --dry-run (default: traffic_payloads.json)")
    p.add_argument("--debug", action="store_true", help="Enable debug logging")
    return p.parse_args()


# ─── Main ───────────────────────────────────────────────────────────────────

def resolveApList(args: argparse.Namespace, ap_config: dict) -> dict:
    '''Turn --ap into a filtered ap_config dict, hard-exiting on unknown host(s).'''
    if not args.ap:
        return ap_config
    requested_hosts = {h.strip() for h in args.ap.split(",") if h.strip()}
    missing_hosts = requested_hosts - ap_config.keys()
    if missing_hosts:
        log.error("Host(s) not found in AP_LIST/AP_CONFIG: %s", sorted(missing_hosts))
        sys.exit(1)
    ap_config = {ip: cfg for ip, cfg in ap_config.items() if ip in requested_hosts}
    log.info("Targeting %d AP(s): %s", len(ap_config), sorted(ap_config))
    return ap_config


def main() -> None:
    args = _parse_args()
    if args.debug:
        log.setLevel(logging.DEBUG)

    print('-' * 40)
    print('Resolve target AP(s) + band config')
    print('-' * 40)
    log.info("DEFAULT_MODE = %d (%s)", DEFAULT_MODE,
              "AP_LIST + DEFAULT_BAND_CFG" if DEFAULT_MODE == 1 else "AP_CONFIG")

    ap_config = effective_ap_config()
    if not ap_config:
        log.error("No APs resolved — check AP_LIST/AP_CONFIG and DEFAULT_MODE.")
        sys.exit(1)

    ap_config = resolveApList(args, ap_config)

    print('\n' + '-' * 40)
    print('Build client payloads')
    print('-' * 40)
    ap_clients: dict[str, list[dict]] = {}
    ap_bands_map: dict[str, dict[int, dict]] = {}
    for ip, cfg in ap_config.items():
        clients = build_clients(cfg["bands"])
        if not clients:
            log.warning("[%s] all configured radios have veth_count=0 or no radios configured; skipping AP", ip)
            continue
        ap_clients[ip] = clients
        ap_bands_map[ip] = cfg["bands"]
        summary = ", ".join(
            f"{_RADIO_LABEL.get(r, r)}={b['veth_count']}v/{b['fileop_count']}f/{b['clientop_count']}c/{b['ip_mode']}/{b['target_type']}"
            for r, b in sorted(cfg["bands"].items())
        )
        log.info("[%s] %s", ip, summary)

    if not ap_clients:
        log.error("Nothing to configure across all APs.")
        sys.exit(1)

    # Resolve discovery URL per AP and fetch each unique URL only once.
    ap_url: dict[str, str] = {}
    for ip in ap_clients:
        url = _discovery_url_for(ip)
        if not url:
            log.error("No discovery URL configured for AP %s — add its prefix to DISCOVERY_URLS", ip)
            sys.exit(1)
        ap_url[ip] = url

    endpoints_cache: dict[str, dict] = {}
    for url in sorted(set(ap_url.values())):
        endpoints_cache[url] = fetch_endpoints(url)

    ap_endpoints = {ip: endpoints_cache[ap_url[ip]] for ip in ap_clients}

    if args.dry_run:
        log.info("DRY-RUN: building payloads, will write to %s", args.out)
        bundle: dict[str, dict] = {}
        for ip in ap_clients:
            fileop_payload, client_payload, fileop_by_radio, client_by_radio = build_ap_payloads(
                ap_clients[ip], ap_endpoints[ip]
            )
            bundle[ip] = {
                "fileop_url":           f"http://{ip}:{WIFIAGENT_PORT}/device/traffic/fileop/config",
                "client_url":           f"http://{ip}:{WIFIAGENT_PORT}/device/traffic/client/config",
                "fileop_payload":       fileop_payload,
                "client_payload":       client_payload,
                "fileop_session_count": len(fileop_payload["fileoperations"]) if fileop_payload else 0,
                "client_session_count": len(client_payload["clientoperation"]) if client_payload else 0,
            }
            log.info("  [%s] fileop  -> [%s]", ip,
                      _breakdown_str(fileop_by_radio, ap_bands_map[ip], "fileop_count"))
            log.info("  [%s] client  -> [%s]", ip,
                      _breakdown_str(client_by_radio, ap_bands_map[ip], "clientop_count"))
        with open(args.out, "w") as fh:
            json.dump(bundle, fh, indent=2)
        log.info("Wrote %d AP payload(s) -> %s", len(bundle), args.out)
        return

    print('\n' + '-' * 40)
    print('Push config to AP(s) concurrently')
    print('-' * 40)
    log.info("Configuring %d APs ...", len(ap_clients))
    apIpList = list(ap_clients)
    results = runConcurrently(
        lambda ip: configure_ap(ip, ap_clients[ip], ap_endpoints[ip], ap_bands_map[ip]),
        apIpList,
        "configure",
    )
    summary: list[dict] = [results[ip] for ip in apIpList]

    W = 20
    numCols = 3
    ruleWidth = W * numCols + (numCols - 1) * 2
    print("\n" + "=" * ruleWidth)
    print(f"{'AP IP':<{W}}  {'FileOp':<{W}}  {'Client':<{W}}")
    print("=" * ruleWidth)
    for r in sorted(summary, key=lambda x: x["ap"]):
        print(f"{r['ap']:<{W}}  {r.get('fileop', '-'):<{W}}  {r.get('client', '-'):<{W}}")
    print("=" * ruleWidth + "\n")


if __name__ == "__main__":
    main()
