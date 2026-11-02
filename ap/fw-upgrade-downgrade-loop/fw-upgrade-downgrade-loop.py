#!/usr/bin/env python3
"""TC-3: repeat firmware upgrade/downgrade with a WM-connected cooldown.

Standalone: pip install paramiko pexpect requests
Run: nohup python3 fw-upgrade-downgrade-loop.py > /dev/null 2>&1 &
Edit AP_CONFIG below. URLs are COMPLETE platform-specific firmware URLs;
no model folder is appended. Each worker reads the installed firmware at
startup: if it matches upgrade_url/version, downgrade first; otherwise upgrade.

Like consecutive-reboots.py, client configuration/traffic is external. This
script checks WM Connected, not client association. The ten-minute dwell
survives SSH reconnects and server-state changes. Failsafe recovery is
confirmed automatically, then the worker reconnects without counting recovery
as a firmware transition.

State is persisted before issuing firmware commands: restarts resume waiting
for that transition, never blindly resubmit it. A different boot ID and WM
Connected are required before a transition is counted. The target version is
read from its URL/version and checked against show device info. Review logs/state before
manually clearing a failed/pending transition. Never run two copies together.

This detects boot/readiness/failsafe failures; it does not establish partition
integrity or validate that clients have associated. Device info and upgrade
logs are recorded for investigation. Use immutable image URLs and expected
version strings when confirming the actual build is important.
"""

import json
import logging
import os
import platform
import re
import shlex
import signal
import socket
import subprocess
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
    "10.86.205.176": {
        "version_verify_tls": False,  # Lab firmware server uses a self-signed certificate.
        "upgrade_url": "https://10.86.107.57/sensorimages/airtight/fw_compilation_c4xx_rfe/c430/",
        "downgrade_url": "https://systemtest-upgrade.dt1.wifi.arista.cloud/builds/103/116/1/UPG_BUNDLE_23.1.0F-34_1_0_103_116_1_c430",
    },
    # "AP-2-IP": {"upgrade_url": "https://...", "downgrade_url": "https://..."},
}
SSH_USERNAME = os.environ.get("AP_CONFIG_USER", "config")
SSH_PASSWORD = os.environ.get("AP_CONFIG_PASSWORD", "Config@123")
ROOT_PASSWORD = os.environ.get("AP_ROOT_PASSWORD", "arastra")
WIFI_OTP_URL = "https://license.aristanetworks.com/sign/wifi-otp/"
WIFI_OTP_KEY = os.environ.get("AP_WIFI_OTP_KEY", "d9ca932a4bc8fbaaa5021b00e14dd453d469eaaf")
CONNECTED_DWELL_SECONDS = 10 * 60
STATUS_POLL_INTERVAL_SECONDS = 15
RETRY_INTERVAL_SECONDS = 3
PING_ATTEMPTS_PER_CHECK = 3
PING_PROCESS_TIMEOUT_SECONDS = 5
PORT_CONNECT_TIMEOUT_SECONDS = 3
SSH_CONNECT_TIMEOUT_SECONDS = 8
SSH_BANNER_TIMEOUT_SECONDS = 12
SSH_AUTH_TIMEOUT_SECONDS = 12
CLI_COMMAND_TIMEOUT_SECONDS = 30
# Includes image download, flashing, reboot and reconnect to WM.
TRANSITION_TIMEOUT_SECONDS = 30 * 60
ROOT_PROMPT = r"(?m)^[^\r\n]*#\s*$"
CONFIG_PROMPT = r"(?m)^[^\r\n]*\]\$\s*$"
STATE_FILE = Path(__file__).with_name("firmware_loop_state.json")
LOG_FILE = Path(__file__).with_name("firmware_loop.log")
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
    """Stop a worker without issuing another firmware command."""


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
                    log.debug("%s responds to ping and port 22 is open", ip)
                    return
            except OSError:
                message = "ping responds; waiting for port 22"

        if message != last_message:
            log.info("%s: %s", ip, message)
            last_message = message
        time.sleep(RETRY_INTERVAL_SECONDS)

def connect_ssh(ip: str) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        ip,
        port=22,
        username=SSH_USERNAME,
        password=SSH_PASSWORD,
        timeout=SSH_CONNECT_TIMEOUT_SECONDS,
        banner_timeout=SSH_BANNER_TIMEOUT_SECONDS,
        auth_timeout=SSH_AUTH_TIMEOUT_SECONDS,
        look_for_keys=False,
        allow_agent=False,
    )
    transport = client.get_transport()
    if transport is None or not transport.is_active():
        client.close()
        raise paramiko.SSHException("SSH transport did not become active")
    transport.set_keepalive(10)
    return client

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
    deadline = getattr(LOCAL, "deadline", None)
    if deadline is not None and time.time() >= deadline:
        raise TestFailure("Firmware transition exceeded its deadline; inspect AP and upgrade log")


def otp(challenge):
    response = requests.post(WIFI_OTP_URL,
                             headers={"Authorization": f"Token {WIFI_OTP_KEY}"},
                             json={"message": challenge}, timeout=(10, 20))
    if response.status_code != 201:
        raise RuntimeError(f"OTP signing failed: HTTP {response.status_code}")
    return response.json()["signature"]


def login_root_or_config(ip, user, password, prompt):
    env = os.environ.copy()
    env["TERM"] = "dumb"
    child = pexpect.spawn("ssh", [
        "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=10",
        "-o", "ServerAliveCountMax=2", f"{user}@{ip}",
    ], env=env, encoding="utf-8", codec_errors="replace", timeout=30)
    try:
        passwords = 0
        for _ in range(8):
            check_deadline()
            idx = child.expect([prompt, "continue connecting", r"[Pp]assword:",
                                r"Response\[([^\]]{54})", pexpect.EOF, pexpect.TIMEOUT])
            if idx == 0:
                return child
            if idx == 1:
                child.sendline("yes")
            elif idx == 2:
                passwords += 1
                if passwords > 1:
                    raise RuntimeError("SSH password rejected")
                child.sendline(password)
            elif idx == 3:
                child.sendline(otp(child.match.group(1)))
            else:
                raise RuntimeError("SSH closed/timed out before prompt")
        raise RuntimeError("Too many authentication prompts")
    except Exception:
        child.close(force=True)
        raise


def root_shell(ip):
    try:
        return login_root_or_config(ip, "root", ROOT_PASSWORD, ROOT_PROMPT)
    except RuntimeError:
        log.warning("%s direct root login failed; trying config rootuser unlock", ip)
    child = login_root_or_config(ip, SSH_USERNAME, SSH_PASSWORD, CONFIG_PROMPT)
    try:
        for command in ("privilege access", "rootuser unlock"):
            child.sendline(command)
            child.expect(CONFIG_PROMPT)
            if re.search(r"\b(error|failed|invalid|denied)\b", child.before, re.I):
                raise RuntimeError(f"Root unlock rejected: {child.before}")
    finally:
        child.close(force=True)
    return login_root_or_config(ip, "root", ROOT_PASSWORD, ROOT_PROMPT)


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
    # Both URL types follow ap-upgrade.py's HTTPS URL command. No folder added.
    cli = f"upgrade method {urlsplit(url).scheme} url {url.rstrip('/')}/"
    command = "printf 'y\\n' | cli -c " + shlex.quote(cli)
    log.info("%s starting firmware command: %s", ip, cli)
    child.sendline(command)
    idx = child.expect(["Upgrade has started", pexpect.EOF, pexpect.TIMEOUT], timeout=60)
    output = child.before or ""
    log.debug("%s firmware start output: %s", ip, output.strip())
    if idx != 0:
        # Command may have reached the AP. Keep the persisted pending state;
        # never resubmit automatically on an ambiguous SSH failure.
        raise RuntimeError("No upgrade-start acknowledgement; waiting for boot evidence, not resending")
    log.info("%s firmware request accepted; waiting for download/flash and reboot", ip)
    deadline = time.time() + TRANSITION_TIMEOUT_SECONDS
    # Keep the initiating SSH session open while the firmware CLI may still be
    # downloading in the foreground. A 30-second prompt delay is normal here.
    while time.time() < deadline:
        idx = child.expect([ROOT_PROMPT, pexpect.EOF, pexpect.TIMEOUT], timeout=30)
        if idx == 0:
            log.info("%s firmware command returned; waiting for reboot and WM connection", ip)
            return
        if idx == 1:
            log.info("%s SSH disconnected after firmware started; checking reboot and WM connection", ip)
            return
        log.info("%s firmware download/flash still running; waiting for completion", ip)
    raise TestFailure("Firmware command did not finish within the transition timeout")


def verify_transition(ip, config, state, store, channel):
    pending = state["pending"]
    child = root_shell(ip)
    try:
        current_boot = boot_id(child)
        if current_boot == pending["previous_boot_id"]:
            log.info("%s waiting for a new boot after %s", ip, pending["action"])
            return False
        info = root_command(child, 'cli -c "show device info"')
        log.debug("%s post-%s device info:\n%s", ip, pending["action"], info.strip())
    finally:
        child.close(force=True)
    expected = pending["expected_version"]
    if expected and parse_device_version(info) != expected:
        raise TestFailure(f"Expected version {expected!r} differs from post-boot device version")
    wait_until_connected(ip, channel)
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
        log.error("%s has a persisted test failure: %s. Worker will not continue.", ip, state["failure"])
        return
    startup_checked = False
    while True:
        client = None
        try:
            pending = state.get("pending")
            LOCAL.deadline = pending["deadline"] if pending else None
            check_deadline()
            wait_for_ping_and_ssh_port(ip)
            client = connect_ssh(ip)
            channel, _ = open_ap_shell(client)
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
                # Write intent BEFORE sending to prevent duplicate flashes after a crash.
                state["pending"] = {
                    "action": action, "url": config[action + "_url"],
                    "expected_version": expected_version,
                    "previous_boot_id": previous_boot,
                    "deadline": time.time() + TRANSITION_TIMEOUT_SECONDS,
                }
                state["dwell_until"] = None
                store.put(ip, state)
                issue_firmware(ip, child, state["pending"]["url"])
            finally:
                child.close(force=True)
        except TestFailure as exc:
            state["failure"] = str(exc)
            store.put(ip, state)
            log.error("%s TEST FAILURE: %s; worker stopped, no further firmware commands", ip, exc)
            return
        except Exception as exc:
            if isinstance(exc, pexpect.TIMEOUT):
                reason = "SSH shell prompt timed out"
            elif isinstance(exc, pexpect.EOF):
                reason = "SSH connection closed"
            else:
                reason = " ".join(str(exc).split())[:350]
            phase = ("awaiting " + state["pending"]["action"] + " reboot/WM readiness"
                     if state.get("pending") else "checking AP readiness")
            log.warning("%s %s: %s; retry in %ds", ip, phase, reason, RETRY_INTERVAL_SECONDS)
            log.debug("%s diagnostic details", ip, exc_info=True)
        finally:
            if client is not None:
                client.close()
        time.sleep(RETRY_INTERVAL_SECONDS)


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


def main():
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
