#!/usr/bin/env python3
"""TC-3 firmware upgrade/downgrade loop.

Dependencies: paramiko, pexpect, requests and the colocated AFC helper. Edit AP_CONFIG below.
Config SSH unlocks root first. Firmware, device info, boot-ID inspection
and the outdoor AFC file trigger use root SSH. WM readiness uses config CLI.
Connection/readiness failures retry indefinitely, like consecutive-reboots.py.
Pending intent survives restarts; a pending firmware command is never resent.
After verified reboot/build and WM Connected, wait ten minutes and alternate.
The initial firmware change skips cooldown. Client traffic is managed externally.
"""

import argparse
import json
import logging
import os
import platform
import re
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
import warnings
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import paramiko
import pexpect
import requests
from urllib3.exceptions import InsecureRequestWarning

# Replace AP-1-IP with the actual AP address, then uncomment the entry.
AP_CONFIG = {
    "10.86.205.159": {
        "version_verify_tls": False,  # Lab firmware server uses a self-signed certificate.
        "upgrade_url": "https://10.86.107.57/sensorimages/airtight/fw_compilation_c4xx_rfe/c430/",
        "downgrade_url": "https://systemtest-upgrade.dt1.wifi.arista.cloud/builds/103/116/1/UPG_BUNDLE_23.1.0F-34_1_0_103_116_1_c430",
    },

    "10.86.205.131": {
        "version_verify_tls": False,  # Lab firmware server uses a self-signed certificate.
        "upgrade_url": "https://10.86.107.57/sensorimages/airtight/fw_compilation_c4xx_rfe/o405/",
        "downgrade_url": "https://systemtest-upgrade.dt1.wifi.arista.cloud/builds/104/117/1/UPG_BUNDLE_23.1.0F-34_1_0_104_117_1_o405",
    },

    "10.86.205.163": {
        "version_verify_tls": False,  # Lab firmware server uses a self-signed certificate.
        "upgrade_url": "https://10.86.107.57/sensorimages/airtight/fw_compilation_c4xx_rfe/o435/",
        "downgrade_url": "https://systemtest-upgrade.dt1.wifi.arista.cloud/builds/103/115/1/UPG_BUNDLE_23.1.0F-34_1_0_103_115_1_o435",
    },

    
    
}
SSH_USERNAME = "config"
SSH_PASSWORD = "Config@123"
ROOT_PASSWORD = os.environ.get("AP_ROOT_PASSWORD", "arastra")
WIFI_OTP_URL = "https://license.aristanetworks.com/sign/wifi-otp/"
WIFI_OTP_KEY = os.environ.get("AP_WIFI_OTP_KEY", "d9ca932a4bc8fbaaa5021b00e14dd453d469eaaf")
CONNECTED_DWELL_SECONDS = 10 * 60
STATUS_POLL_INTERVAL_SECONDS = 15
RETRY_INTERVAL_SECONDS = 3
SSH_RETRY_INITIAL_SECONDS = 60
POST_FIRMWARE_SSH_GRACE_SECONDS = 60
PING_ATTEMPTS_PER_CHECK = 3
PING_PROCESS_TIMEOUT_SECONDS = 5
PORT_CONNECT_TIMEOUT_SECONDS = 3
SSH_CONNECT_TIMEOUT_SECONDS = 8
SSH_BANNER_TIMEOUT_SECONDS = 12
SSH_AUTH_TIMEOUT_SECONDS = 12
CLI_COMMAND_TIMEOUT_SECONDS = 30
ROOT_PROMPT = r"(?m)^[^\r\n]*#\s*$"
CONFIG_PROMPT = r"(?m)^[^\r\n]*\]\$\s*$"
STATE_FILE = Path(__file__).with_name("firmware_loop_state.json")
LOG_FILE = Path(__file__).with_name("firmware_loop.log")
AFC_SCRIPT = Path(__file__).resolve().parent / "afc-configure-6ghz-outdoor" / "afc-configure.py"
log = logging.getLogger("firmware_loop")
logging.getLogger("paramiko").setLevel(logging.CRITICAL)
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
PROMPT_LINE_RE = re.compile(r"^\[[^\]\r\n]+\][#$>]\s*$")
CONNECTED_LINE_RE = re.compile(r"^\s*Server connection state\s*:\s*(.*?)\s*$", re.I)


CONFIRM_RE = re.compile(
    r"\b(confirm|are you sure|continue|proceed)\b|"
    r"\(\s*y\s*/\s*n\s*\)|\[\s*y\s*/\s*n\s*\]",
    re.IGNORECASE,
)
REJECTED_COMMAND_RE = re.compile(
    r"\b(unknown command|unrecognized command|invalid command|permission denied)\b",
    re.IGNORECASE,
)


class TestFailure(RuntimeError):
    """Verification problem; the worker retains pending intent and retries."""


class AuthenticationFailure(TestFailure):
    """Abort this login attempt before sending repeated credentials."""


class StateStore:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        # Fail startup on malformed state; never silently restart at upgrade.
        self.data = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(self.data, dict):
            raise ValueError("State file must contain an object")

    def get(self, ip):
        with self.lock:
            return dict(self.data.get(ip, {
                "next_action": "upgrade", "upgrades": 0, "downgrades": 0,
                "completed_loops": 0, "pending": None, "failure": None,
            }))

    def put(self, ip, state):
        with self.lock:
            self.data[ip] = dict(state)
            temporary = self.path.with_suffix(".json.tmp")
            with temporary.open("w") as stream:
                json.dump(self.data, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)


def ping_ap(ip: str) -> bool:
    """Try up to three probes so an initial ARP-related loss can recover."""
    system = platform.system()
    if system == "Windows":
        command = ["ping", "-n", "1", "-w", "2000", ip]
    elif system == "Darwin":
        command = ["ping", "-c", "1", "-W", "2000", ip]
    else:
        command = ["ping", "-c", "1", "-W", "2", ip]

    for _attempt in range(PING_ATTEMPTS_PER_CHECK):
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=PING_PROCESS_TIMEOUT_SECONDS,
            )
            if result.returncode == 0:
                return True
        except (OSError, subprocess.TimeoutExpired):
            continue
    return False

def wait_for_ping_and_ssh_port(ip: str) -> None:
    """Repeat ping and port checks until the AP is ready for SSH."""
    last_message = None
    while True:
        check_deadline()
        if not ping_ap(ip):
            message = "waiting for ping response"
        else:
            try:
                with socket.create_connection(
                    (ip, 22), timeout=PORT_CONNECT_TIMEOUT_SECONDS
                ):
                    log.info("%s [NETWORK] ping responds and port 22 is open; attempting config SSH", ip)
                    return
            except OSError:
                message = "ping responds; waiting for port 22"

        if message != last_message:
            log.info("%s: %s", ip, message)
            last_message = message
        time.sleep(RETRY_INTERVAL_SECONDS)

def connect_ssh(ip: str) -> paramiko.SSHClient:
    """Config login uses only the exact Config@123 password."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            ip, port=22, username=SSH_USERNAME, password=SSH_PASSWORD,
            timeout=SSH_CONNECT_TIMEOUT_SECONDS,
            banner_timeout=SSH_BANNER_TIMEOUT_SECONDS,
            auth_timeout=SSH_AUTH_TIMEOUT_SECONDS,
            look_for_keys=False, allow_agent=False,
        )
        transport = client.get_transport()
        if transport is None or not transport.is_active():
            raise paramiko.SSHException("SSH transport did not become active")
        transport.set_keepalive(10)
        return client
    except paramiko.AuthenticationException:
        client.close()
        raise AuthenticationFailure(
            "Config SSH authentication rejected; reconnecting on the next attempt") from None
    except Exception:
        client.close()
        raise


def _has_cli_prompt(buffer: str) -> bool:
    cleaned = ANSI_ESCAPE_RE.sub("", buffer.replace("\r", ""))
    lines = cleaned.rstrip().splitlines()
    return bool(lines and PROMPT_LINE_RE.fullmatch(lines[-1].strip()))

def read_until_prompt(
    channel: paramiko.Channel, timeout: float = CLI_COMMAND_TIMEOUT_SECONDS
) -> str:
    deadline = time.monotonic() + timeout
    chunks: list[str] = []
    while time.monotonic() < deadline:
        if channel.closed or not channel.get_transport().is_active():
            raise paramiko.SSHException("SSH shell closed while waiting for AP prompt")
        if channel.recv_ready():
            data = channel.recv(65535)
            if not data:
                raise paramiko.SSHException("SSH shell closed by AP")
            chunks.append(data.decode("utf-8", errors="replace"))
            output = "".join(chunks)
            if _has_cli_prompt(output):
                return output
        else:
            time.sleep(0.1)
    raise TimeoutError(f"AP CLI did not return a prompt within {timeout:g} seconds")

def open_ap_shell(client: paramiko.SSHClient) -> tuple[paramiko.Channel, str]:
    channel = client.invoke_shell(width=200, height=40)
    channel.settimeout(1.0)
    initial_output = read_until_prompt(channel, timeout=SSH_BANNER_TIMEOUT_SECONDS + 10)
    return channel, initial_output

def run_cli_command(channel: paramiko.Channel, command: str) -> str:
    if channel.closed or not channel.get_transport().is_active():
        raise paramiko.SSHException("SSH shell is no longer active")
    channel.sendall(command + "\n")
    return read_until_prompt(channel)

def connection_state(output: str) -> Optional[str]:
    for line in output.splitlines():
        match = CONNECTED_LINE_RE.match(line)
        if match:
            return match.group(1).strip()
    return None

def _is_connected_failsafe(state: Optional[str]) -> bool:
    if not state:
        return False
    normalized = re.sub(r"\s+", " ", state).strip().casefold()
    return normalized.startswith("connected") and "failsafe" in normalized

def _recover_failsafe_mode(ip: str, channel: paramiko.Channel) -> None:
    """Run failsafe recovery, confirm it, and hand control back to reconnect."""
    log.warning("%s reports Connected - Failsafe-mode; requesting failsafe recovery", ip)
    channel.sendall("failsafe recover\n")
    response_parts: list[str] = []
    confirmation_sent = False
    deadline = time.monotonic() + CLI_COMMAND_TIMEOUT_SECONDS

    while time.monotonic() < deadline:
        transport = channel.get_transport()
        if channel.closed or transport is None or not transport.is_active():
            if confirmation_sent:
                break
            raise paramiko.SSHException(
                "SSH disconnected before failsafe recovery was confirmed"
            )

        if not channel.recv_ready():
            time.sleep(0.1)
            continue

        data = channel.recv(65535)
        if not data:
            if confirmation_sent:
                break
            raise paramiko.SSHException(
                "SSH closed before failsafe recovery was confirmed"
            )

        response_parts.append(data.decode("utf-8", errors="replace"))
        response = "".join(response_parts)
        if REJECTED_COMMAND_RE.search(response):
            raise RuntimeError(f"AP rejected failsafe recovery: {response[-300:]!r}")

        if not confirmation_sent and CONFIRM_RE.search(response):
            channel.sendall("y\n")
            confirmation_sent = True
            response_parts.clear()
            log.info("%s confirmed failsafe recovery", ip)
            continue

        if _has_cli_prompt(response):
            if confirmation_sent or "will reboot" in response.casefold():
                break
            raise RuntimeError(
                f"Failsafe recovery returned to the prompt without confirmation: "
                f"{response[-300:]!r}"
            )

    if not confirmation_sent:
        raise TimeoutError("AP did not request confirmation for failsafe recovery")

    log.info(
        "%s failsafe recovery reboot initiated; test reboot count is unchanged",
        ip,
    )
    raise paramiko.SSHException("failsafe recovery reboot initiated; reconnecting")

def wait_until_connected(ip: str, channel: paramiko.Channel) -> None:
    """Wait until this cycle first observes Connected."""
    while True:
        check_deadline()
        output = run_cli_command(channel, "show device status")
        state = connection_state(output)
        if _is_connected_failsafe(state):
            _recover_failsafe_mode(ip, channel)
        if state and state.casefold() == "connected":
            return
        log.info(
            "%s server connection state: %s; retrying in %d seconds",
            ip,
            state or "not reported",
            STATUS_POLL_INTERVAL_SECONDS,
        )
        time.sleep(STATUS_POLL_INTERVAL_SECONDS)

def wait_for_dwell_deadline(
    ip: str,
    channel: paramiko.Channel,
    dwell_deadline: float,
) -> None:
    """Wait to the original deadline; status changes never restart the dwell."""
    while True:
        check_deadline()
        remaining = dwell_deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(STATUS_POLL_INTERVAL_SECONDS, remaining))
        if time.monotonic() < dwell_deadline:
            # Keep the CLI session active. The reported server state is ignored
            # after the dwell starts, so Connected -> Not connected won't reset it.
            output = run_cli_command(channel, "show device status")
            if _is_connected_failsafe(connection_state(output)):
                _recover_failsafe_mode(ip, channel)

LOCAL = threading.local()


def check_deadline():
    """Compatibility hook: reconnect/readiness waits have no overall deadline."""
    return


def otp(challenge):
    response = requests.post(WIFI_OTP_URL,
                             headers={"Authorization": f"Token {WIFI_OTP_KEY}"},
                             json={"message": challenge}, timeout=(10, 20))
    if response.status_code != 201:
        raise RuntimeError(f"OTP signing failed: HTTP {response.status_code}")
    return response.json()["signature"]


def login_root(ip):
    """Root-only OTP/password login; config credentials never use this path."""
    user, password, prompt = "root", ROOT_PASSWORD, ROOT_PROMPT
    env = os.environ.copy()
    env["TERM"] = "dumb"
    child = pexpect.spawn("ssh", [
        "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=10",
        "-o", "ServerAliveCountMax=2", "-o", "NumberOfPasswordPrompts=1",
        "-o", "PubkeyAuthentication=no", f"{user}@{ip}",
    ], env=env, encoding="utf-8", codec_errors="replace", timeout=30)
    try:
        response_sent = False
        for _ in range(8):
            check_deadline()
            idx = child.expect([
                r"(?i)(?:the )?account is locked[^\r\n]*",
                r"(?i)permission denied|authentication failed|too many authentication failures",
                prompt, "continue connecting", r"[Pp]assword:",
                r"Response\[([^\]]{54})", pexpect.EOF, pexpect.TIMEOUT,
            ])
            if idx == 0:
                raise AuthenticationFailure(
                    f"{user} account is locked; login attempt aborted; reconnecting after retry interval")
            if idx == 1:
                raise AuthenticationFailure(f"{user} SSH authentication rejected; ending this login attempt")
            if idx == 2:
                return child
            if idx == 3:
                child.sendline("yes")
            elif idx in (4, 5):
                if response_sent:
                    raise AuthenticationFailure(
                        f"{user} SSH requested credentials again; ending attempt before a second response")
                response_sent = True
                if idx == 4:
                    child.sendline(password)
                else:
                    child.sendline(otp(child.match.group(1)))
            elif idx in (6, 7) and response_sent:
                raise ConnectionError(
                    f"{user} SSH closed/timed out after authentication response; "
                    "authentication result unknown, reconnecting with backoff")
            else:
                raise ConnectionError(f"{user} SSH closed/timed out before prompt")
        raise AuthenticationFailure(f"Too many {user} authentication prompts; stopping")
    except Exception:
        child.close(force=True)
        raise


def unlock_root(ip):
    """Unlock through config BEFORE any root login, especially after a reboot."""
    channel = getattr(LOCAL, "config_channel", None)
    if channel is not None:
        log.info("%s [UNLOCK] config: privilege access -> rootuser unlock", ip)
        for command in ("privilege access", "rootuser unlock"):
            output = run_cli_command(channel, command)
            if re.search(r"\b(error|failed|invalid|denied)\b", output, re.I):
                raise AuthenticationFailure(f"Config rejected {command}; root login not attempted")
        return
    # All config authentication uses the same Paramiko password API: no
    # terminal paste, root password, OTP handler or environment override.
    client = connect_ssh(ip)
    try:
        channel, _ = open_ap_shell(client)
        for command in ("privilege access", "rootuser unlock"):
            output = run_cli_command(channel, command)
            if re.search(r"\b(error|failed|invalid|denied)\b", output, re.I):
                raise AuthenticationFailure(f"Config rejected {command}; root login not attempted")
    finally:
        client.close()


def root_shell(ip):
    unlock_root(ip)
    log.debug("%s rootuser unlock completed; opening root SSH", ip)
    return login_root(ip)


def root_command(child, command):
    child.sendline(command)
    try:
        child.expect(ROOT_PROMPT, timeout=CLI_COMMAND_TIMEOUT_SECONDS)
    except pexpect.TIMEOUT:
        raise TimeoutError(f"No shell prompt within {CLI_COMMAND_TIMEOUT_SECONDS}s after {command}") from None
    except pexpect.EOF:
        raise ConnectionError(f"SSH disconnected while running {command}") from None
    return child.before



# Compare the firmware release token, excluding display annotations such as
# "(active)". MWM_VER and the CLI must both provide a recognizable version.
FIRMWARE_VERSION_RE = re.compile(r"(?<![\w.])\d+\.\d+\.\d+[A-Za-z]*-\d+(?![\w])")


def parse_device_version(info):
    cleaned = ANSI_ESCAPE_RE.sub("", info.replace("\r", ""))
    # Arista splits the release and complete build into separate fields:
    # Device version: 23.2.0F / Device build: 23.2.0F-7.
    # MWM_VER contains the complete build, so prefer Device build.
    labels = (
        r"device\s+build",
        r"(?:(?:firmware|software|device|AP)\s+version|version)",
    )
    for label in labels:
        for line in cleaned.splitlines():
            if re.match(r"^\s*" + label + r"\s*:", line, re.I):
                match = FIRMWARE_VERSION_RE.search(line.split(":", 1)[1])
                if match:
                    return match.group(0)
    raise RuntimeError(
        "Could not parse firmware build from show device info; "
        f"no flash sent. Device info: {cleaned.strip()!r}")


def fetch_firmware_version(url, verify_tls=True):
    version_url = url.rstrip("/") + "/version"
    if not verify_tls:
        # Only silence this explicitly opted-out host's repeated warning.
        host = re.escape(urlsplit(version_url).hostname or "")
        warnings.filterwarnings(
            "ignore", message=r"Unverified HTTPS request.*host '" + host + r"'",
            category=InsecureRequestWarning, module=r"urllib3\.connectionpool")
    response = requests.get(version_url, timeout=(10, 20), verify=verify_tls)
    response.raise_for_status()
    match = re.search(r"^\s*MWM_VER\s*:\s*(\S+)\s*$", response.text, re.M)
    if not match or not FIRMWARE_VERSION_RE.fullmatch(match.group(1)):
        raise RuntimeError(f"Missing/invalid MWM_VER in {version_url}; no flash sent")
    return match.group(1)


def select_start_action(ip, config, state, store):
    upgrade_version = fetch_firmware_version(
        config["upgrade_url"], config.get("version_verify_tls", True))
    child = root_shell(ip)
    try:
        info = root_command(child, 'cli -c "show device info"')
    finally:
        child.close(force=True)
    installed = parse_device_version(info)
    state["next_action"] = "downgrade" if installed == upgrade_version else "upgrade"
    state["startup_installed_version"] = installed
    state["startup_upgrade_version"] = upgrade_version
    store.put(ip, state)
    log.info("%s installed=%s upgrade URL version=%s; starting with %s",
             ip, installed, upgrade_version, state["next_action"])


def boot_id(child):
    output = root_command(child, "cat /proc/sys/kernel/random/boot_id")
    match = re.search(r"(?m)^([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\r?$", output)
    if not match:
        raise RuntimeError("Could not read boot ID; refusing unverified firmware transition")
    return match.group(1)


def issue_firmware(ip, child, url):
    """Run firmware CLI in root; wait without an overall transition timeout."""
    cli = f"upgrade method {urlsplit(url).scheme} url {url.rstrip('/')}/"
    command = "printf 'y\\n' | cli -c " + shlex.quote(cli)
    log.info("%s [FIRMWARE] ROOT: %s", ip, cli)
    child.sendline(command)
    accepted = False
    while True:
        idx = child.expect([
            "Upgrade has started", ROOT_PROMPT, pexpect.EOF, pexpect.TIMEOUT,
        ], timeout=30)
        output = child.before or ""
        if REJECTED_COMMAND_RE.search(output):
            raise RuntimeError("Firmware command rejected; retaining pending intent for verification")
        if idx == 0:
            accepted = True
            log.info("%s [FIRMWARE] request accepted; downloading/flashing", ip)
        elif idx == 1:
            log.info("%s [REBOOT] firmware CLI returned; verifying reboot/build%s", ip,
                     "" if accepted else " (start acknowledgement not seen)")
            return
        elif idx == 2:
            log.info("%s [REBOOT] firmware SSH closed; waiting for AP return", ip)
            return
        else:
            log.info("%s [FIRMWARE] waiting for download/flash and automatic reboot", ip)


# Use the proven SCP + trigger helper, kept beside this firmware loop.
OUTDOOR_AFC_MODELS = {"O-405", "O-435"}


def apply_outdoor_afc(ip, child, info, current_boot, state, store):
    """Apply the reference AFC trigger once per outdoor AP boot, before WM wait."""
    match = re.search(r"^\s*Device model\s*:\s*(\S+)", info, re.M | re.I)
    if not match:
        raise RuntimeError("Device model not reported; cannot decide outdoor AFC configuration")
    model = match.group(1).upper()
    if model not in OUTDOOR_AFC_MODELS:
        return
    if state.get("afc_configured_boot_id") == current_boot:
        return
    if not AFC_SCRIPT.is_file():
        raise FileNotFoundError(f"AFC helper missing: {AFC_SCRIPT}")
    payload = AFC_SCRIPT.with_name("ap.conf.diff.afc.12345")
    if not payload.is_file():
        raise FileNotFoundError(f"AFC config file missing: {payload}")
    command = [sys.executable, "-u", str(AFC_SCRIPT), "--ap", ip]
    log.info("%s [AFC] %s: executing %s --ap %s", ip, model, AFC_SCRIPT.name, ip)
    success_reported = False
    # The helper prints failures but may still exit zero. Require its explicit
    # per-AP success report as well as a successful process exit.
    with subprocess.Popen(command, cwd=str(AFC_SCRIPT.parent),
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding="utf-8", errors="replace", bufsize=1) as process:
        for line in process.stdout:
            line = line.rstrip()
            if line:
                log.info("%s [AFC-HELPER] %s", ip, line)
            if re.search(re.escape(ip) + r":\s*afc-configure succeeded\b", line):
                success_reported = True
        returncode = process.wait()
    if returncode != 0 or not success_reported:
        raise RuntimeError(
            f"AFC helper did not confirm success (exit={returncode}); "
            "see AFC-HELPER output; retrying on reconnect")
    state["afc_configured_boot_id"] = current_boot
    store.put(ip, state)
    log.info("%s [AFC] trigger success saved locally for boot %s; reconnects will skip AFC", ip, current_boot)


def verify_transition(ip, config, state, store, channel):
    pending = state["pending"]
    child = root_shell(ip)
    try:
        current_boot = boot_id(child)
        if current_boot == pending["previous_boot_id"]:
            log.info("%s [REBOOT] still on previous boot after %s; waiting", ip, pending["action"])
            return False
        info = root_command(child, 'cli -c "show device info"')
        log.debug("%s post-%s device info:\n%s", ip, pending["action"], info.strip())
        apply_outdoor_afc(ip, child, info, current_boot, state, store)
    finally:
        child.close(force=True)
    expected = pending["expected_version"]
    if expected and parse_device_version(info) != expected:
        raise TestFailure(f"Expected version {expected!r} differs from post-boot device version")
    log.info("%s [VERIFY] new boot and build %s confirmed; waiting for WM Connected", ip, expected)
    wait_until_connected(ip, channel)
    log.info("%s [WM] Connected; starting 10-minute cooldown", ip)
    action = pending["action"]
    state[action + "s"] += 1
    state["completed_loops"] = min(state["upgrades"], state["downgrades"])
    state["next_action"] = "downgrade" if action == "upgrade" else "upgrade"
    state["pending"] = None
    state["last_device_info"] = info.strip()
    state["last_boot_id"] = current_boot
    # Persist the cooldown across process restarts as well as SSH reconnects.
    state["dwell_until"] = time.time() + CONNECTED_DWELL_SECONDS
    store.put(ip, state)
    log.info("%s %s boot/WM checks passed; upgrades=%d downgrades=%d loops=%d",
             ip, action, state["upgrades"], state["downgrades"], state["completed_loops"])
    if not expected:
        log.warning("%s no expected version configured for %s; build identity was not asserted", ip, action)
    return True


def run_one_ap_forever(ip, config, store):
    state = store.get(ip)
    if state.get("failure"):
        state["last_failure"] = state["failure"]
        state["failure"] = None
        store.put(ip, state)
        log.info("%s [RESUME] previous failure retained as history; retrying saved phase", ip)
    startup_checked = False
    while True:
        client = None
        wait_seconds = RETRY_INTERVAL_SECONDS
        try:
            pending = state.get("pending")
            LOCAL.deadline = None
            check_deadline()
            if pending:
                grace = pending.get("reconnect_not_before", 0) - time.time()
                if grace > 0:
                    log.info("%s allowing %.0fs for firmware reboot before reconnecting", ip, grace)
                    time.sleep(min(grace, POST_FIRMWARE_SSH_GRACE_SECONDS))
                    continue
            log.info("%s [NETWORK] checking ping, then port 22 before SSH", ip)
            wait_for_ping_and_ssh_port(ip)
            client = connect_ssh(ip)
            channel, _ = open_ap_shell(client)
            LOCAL.config_channel = channel
            log.info("%s [SSH] config login successful", ip)
            output = run_cli_command(channel, "show device status")
            if _is_connected_failsafe(connection_state(output)):
                _recover_failsafe_mode(ip, channel)
            if pending:
                if not verify_transition(ip, config, state, store, channel):
                    time.sleep(STATUS_POLL_INTERVAL_SECONDS)
                    continue
                LOCAL.deadline = None
                startup_checked = True
            if not startup_checked:
                select_start_action(ip, config, state, store)
                if not state.get("upgrades", 0) and not state.get("downgrades", 0):
                    # Also discard a saved startup dwell from older script versions.
                    # Pending transitions were handled above and never reach here.
                    wait_until_connected(ip, channel)
                    state["dwell_until"] = time.time()
                    store.put(ip, state)
                    log.info("%s skipping initial cooldown; starting %s once ready",
                             ip, state["next_action"])
                startup_checked = True
            if not state.get("dwell_until"):
                wait_until_connected(ip, channel)
                state["dwell_until"] = time.time() + CONNECTED_DWELL_SECONDS
                store.put(ip, state)
            output = run_cli_command(channel, "show device status")
            if _is_connected_failsafe(connection_state(output)):
                _recover_failsafe_mode(ip, channel)
            remaining = max(0, state["dwell_until"] - time.time())
            if remaining:
                log.info("%s WM connected; cooldown %.1f minutes before %s", ip, remaining / 60, state["next_action"])
                wait_for_dwell_deadline(ip, channel, time.monotonic() + remaining)
            child = root_shell(ip)
            try:
                previous_boot = boot_id(child)
                action = state["next_action"]
                expected_version = fetch_firmware_version(
                    config[action + "_url"], config.get("version_verify_tls", True))
                state["pending"] = {
                    "action": action, "url": config[action + "_url"],
                    "expected_version": expected_version,
                    "previous_boot_id": previous_boot,
                }
                state["dwell_until"] = None
                store.put(ip, state)
                issue_firmware(ip, child, state["pending"]["url"])
                state["pending"]["reconnect_not_before"] = time.time() + POST_FIRMWARE_SSH_GRACE_SECONDS
                store.put(ip, state)
            finally:
                child.close(force=True)
        except Exception as exc:
            if isinstance(exc, pexpect.TIMEOUT):
                reason = "SSH shell prompt timed out"
            elif isinstance(exc, pexpect.EOF):
                reason = "SSH connection closed"
            else:
                reason = " ".join(str(exc).split())[:350]
            phase = ("awaiting " + state["pending"]["action"] + " reboot/verification"
                     if state.get("pending") else "connecting/preparing firmware")
            log.warning("%s [RETRY] %s: %s; retry in %ds (no overall timeout)",
                        ip, phase, reason, RETRY_INTERVAL_SECONDS)
            log.debug("%s diagnostic details", ip, exc_info=True)
        finally:
            LOCAL.config_channel = None
            if client is not None:
                client.close()
        time.sleep(wait_seconds)


def validate_config():
    if not AP_CONFIG:
        raise ValueError("AP_CONFIG is empty; add AP addresses and their two firmware URLs")
    for ip, config in AP_CONFIG.items():
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.:-]*", ip):
            raise ValueError(f"Invalid AP address: {ip!r}")
        for action in ("upgrade", "downgrade"):
            url = config.get(action + "_url", "")
            parsed = urlsplit(url)
            if (parsed.scheme not in ("http", "https") or not parsed.netloc
                    or not re.fullmatch(r"[a-zA-Z0-9:/._~%-]+", url)):
                raise ValueError(f"{ip}: supply a full HTTP(S) {action}_url without query/fragment/whitespace")
        if config["upgrade_url"].rstrip("/") == config["downgrade_url"].rstrip("/"):
            raise ValueError(f"{ip}: upgrade and downgrade URLs must differ")



def delete_generated_files():
    """Clean only known local artifacts; never connect to APs or start workers."""
    import fcntl
    lock_path = STATE_FILE.with_suffix(".lock")
    removed = 0
    # Acquire the same lock as a normal run before touching persisted state.
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Cleanup refused: firmware loop is running. Stop it first.")
        paths = [LOG_FILE, STATE_FILE, STATE_FILE.with_suffix(".json.tmp"),
                 STATE_FILE.parent / "mock.ap.conf.diff.afc.12345"]
        paths.extend(path for path in LOG_FILE.parent.glob(LOG_FILE.name + ".*")
                     if path.name[len(LOG_FILE.name) + 1:].isdigit())
        cache = STATE_FILE.parent / "__pycache__"
        paths.extend(cache.glob(Path(__file__).stem + ".*.pyc"))
        for path in paths:
            if path.exists() or path.is_symlink():
                path.unlink()
                print(f"Deleted: {path.name}")
                removed += 1
        if cache.is_dir() and not any(cache.iterdir()):
            cache.rmdir()
        lock_path.unlink(missing_ok=True)
        print(f"Deleted: {lock_path.name}")
        removed += 1
    print(f"Cleanup complete: {removed} file(s) removed. Script and README preserved.")


def resume_failed_states(store, ips):
    """Explicit operator resume: retain pending intent, never send it again."""
    for ip in ips:
        state = store.get(ip)
        if not state.get("failure"):
            continue
        state["last_failure"] = state["failure"]
        state["failure"] = None
        if state.get("pending"):
            state["pending"].pop("deadline", None)
        store.put(ip, state)
        log.info("%s resuming verification without an overall timeout; pending firmware will not be resent", ip)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delete", action="store_true",
                        help="Delete local logs, state, lock, mock and script bytecode; do not run the test")
    parser.add_argument("--resume-failed", action="store_true",
                        help="Compatibility flag: clear saved failure history, retain pending verification")
    args = parser.parse_args()
    if args.delete and args.resume_failed:
        parser.error("--delete and --resume-failed cannot be combined")
    if args.delete:
        delete_generated_files()
        return
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-8s [%(threadName)s] %(message)s",
                        handlers=[logging.StreamHandler(), RotatingFileHandler(
                            LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")])
    validate_config()
    # Prevent competing processes from flashing the same AP concurrently.
    import fcntl
    with STATE_FILE.with_suffix(".lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Another firmware loop is already running in this directory")
        store = StateStore(STATE_FILE)
        if args.resume_failed:
            resume_failed_states(store, AP_CONFIG)
        for ip in AP_CONFIG:
            store.put(ip, store.get(ip))
        threads = []
        for ip, config in AP_CONFIG.items():
            thread = threading.Thread(target=run_one_ap_forever, args=(ip, config, store),
                                      name=f"AP-{ip}", daemon=True)
            thread.start()
            threads.append(thread)
        def stop(signum, frame):
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, stop)
        try:
            while any(thread.is_alive() for thread in threads):
                time.sleep(1)
        except KeyboardInterrupt:
            log.info("Stopped; pending transitions remain saved for the next run")


if __name__ == "__main__":
    main()
