#!/usr/bin/env python3
"""Continuously reboot APs after a server-connected dwell.

Add the AP addresses to ``AP_IPS`` below, then run this script. Each AP gets
its own worker and retries ping, port 22, SSH, and device readiness forever.
The per-AP reboot command counts are saved in
``continuous_reboot_counts.json`` beside this script.

The APs should already be configured to advertise their SSIDs and any test
clients/traffic should be managed by the lab setup. This script waits for
``show device status`` to report ``Connected``; it does not create clients or
generate traffic itself.
"""

import json
import logging
import os
import platform
import re
import socket
import subprocess
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

import paramiko


# Add all AP IP addresses to this list. Example: "10.87.169.15"
AP_IPS = ["10.87.169.240","10.87.169.88","10.87.169.135","10.87.169.15","10.87.169.82","10.87.169.209","10.87.169.102","10.87.169.74"]

SSH_USERNAME = "config"
SSH_PASSWORD = "Config@123"
REBOOT_COMMAND = "reboot"

CONNECTED_DWELL_SECONDS = 5 * 60
STATUS_POLL_INTERVAL_SECONDS = 15
RETRY_INTERVAL_SECONDS = 3
PING_ATTEMPTS_PER_CHECK = 3
PING_PROCESS_TIMEOUT_SECONDS = 5
PORT_CONNECT_TIMEOUT_SECONDS = 3
SSH_CONNECT_TIMEOUT_SECONDS = 8
SSH_BANNER_TIMEOUT_SECONDS = 12
SSH_AUTH_TIMEOUT_SECONDS = 12
CLI_COMMAND_TIMEOUT_SECONDS = 30

COUNT_FILE = Path(__file__).with_name("continuous_reboot_counts.json")
LOG_FILE = Path(__file__).with_name("continuous_reboot.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s [%(threadName)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(
            LOG_FILE,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        ),
    ],
)
log = logging.getLogger("continuous_reboot_test")
logging.getLogger("paramiko").setLevel(logging.CRITICAL)

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
PROMPT_LINE_RE = re.compile(r"^\[[^\]\r\n]+\][#$>]\s*$")
CONNECTED_LINE_RE = re.compile(
    r"^\s*Server connection state\s*:\s*(.*?)\s*$", re.IGNORECASE
)
CONFIRM_RE = re.compile(
    r"\b(confirm|are you sure|continue|proceed)\b|"
    r"\(\s*y\s*/\s*n\s*\)|\[\s*y\s*/\s*n\s*\]",
    re.IGNORECASE,
)
REJECTED_COMMAND_RE = re.compile(
    r"\b(unknown command|unrecognized command|invalid command|permission denied)\b",
    re.IGNORECASE,
)


class RebootCountStore:
    """Thread-safe reboot command counts, persisted after each increment."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.counts: dict[str, int] = {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self.counts = {
                    str(ip): int(count)
                    for ip, count in raw.items()
                    if isinstance(count, int) and count >= 0
                }
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError) as exc:
            log.warning("Could not load reboot counts from %s: %s", path, exc)

    def get(self, ip: str) -> int:
        with self.lock:
            return self.counts.get(ip, 0)

    def initialize(self, ips: list[str]) -> None:
        """Create the count file at startup and add unseen APs at zero."""
        with self.lock:
            for ip in ips:
                self.counts.setdefault(ip, 0)
            self._save_locked()

    def increment(self, ip: str) -> int:
        with self.lock:
            self.counts[ip] = self.counts.get(ip, 0) + 1
            count = self.counts[ip]
            self._save_locked()
            return count

    def _save_locked(self) -> None:
        temporary_path = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            temporary_path.write_text(
                json.dumps(self.counts, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary_path, self.path)
        except OSError as exc:
            log.error("Could not persist reboot counts to %s: %s", self.path, exc)
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


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
        if not ping_ap(ip):
            message = "waiting for ping response"
        else:
            try:
                with socket.create_connection(
                    (ip, 22), timeout=PORT_CONNECT_TIMEOUT_SECONDS
                ):
                    log.info("%s responds to ping and port 22 is open", ip)
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


def issue_reboot(ip: str, channel: paramiko.Channel, counts: RebootCountStore) -> int:
    """Issue the reboot command and answer a simple CLI confirmation if asked."""
    if channel.closed or not channel.get_transport().is_active():
        raise paramiko.SSHException("SSH shell disconnected before reboot command")

    channel.sendall(REBOOT_COMMAND + "\n")
    response_parts: list[str] = []
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if channel.closed or not channel.get_transport().is_active():
            break
        if not channel.recv_ready():
            time.sleep(0.1)
            continue
        data = channel.recv(65535)
        if not data:
            break
        response_parts.append(data.decode("utf-8", errors="replace"))
        response = "".join(response_parts)
        if CONFIRM_RE.search(response):
            channel.sendall("y\n")
            response_parts.clear()
            continue
        if _has_cli_prompt(response):
            if REJECTED_COMMAND_RE.search(response):
                raise RuntimeError(f"AP rejected reboot command: {response[-300:]!r}")
            break

    count = counts.increment(ip)
    log.info("%s reboot command issued; recorded reboot count is %d", ip, count)
    return count


def run_one_ap_forever(ip: str, counts: RebootCountStore) -> None:
    log.info("Starting continuous reboot worker; prior reboot count: %d", counts.get(ip))
    dwell_deadline = None
    while True:
        client = None
        try:
            wait_for_ping_and_ssh_port(ip)
            log.info("%s attempting SSH login as %s", ip, SSH_USERNAME)
            client = connect_ssh(ip)
            channel, _initial_output = open_ap_shell(client)
            log.info("%s SSH shell is ready", ip)

            if dwell_deadline is None:
                wait_until_connected(ip, channel)
                dwell_deadline = time.monotonic() + CONNECTED_DWELL_SECONDS
                log.info(
                    "%s reports Connected; starting %d-minute dwell. "
                    "The deadline will survive SSH reconnects and status changes",
                    ip,
                    CONNECTED_DWELL_SECONDS // 60,
                )
            else:
                # A pending dwell survives SSH loss. Check failsafe mode on
                # every reconnect before resuming it or issuing its reboot.
                output = run_cli_command(channel, "show device status")
                if _is_connected_failsafe(connection_state(output)):
                    _recover_failsafe_mode(ip, channel)

            remaining = max(0, dwell_deadline - time.monotonic())
            if remaining:
                log.info(
                    "%s has %.0f seconds remaining in its dwell",
                    ip,
                    remaining,
                )
                wait_for_dwell_deadline(ip, channel, dwell_deadline)

            log.info("%s completed the dwell; issuing reboot", ip)
            issue_reboot(ip, channel, counts)
            dwell_deadline = None
            log.info("%s waiting for the AP to reboot and return", ip)
        except Exception as exc:
            log.warning("%s recovery required; returning to ping/SSH checks: %s", ip, exc)
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
        time.sleep(RETRY_INTERVAL_SECONDS)


def main() -> None:
    # Preserve order while ensuring each AP has exactly one worker.
    ap_ips = list(dict.fromkeys(ip.strip() for ip in AP_IPS if ip.strip()))
    if not ap_ips:
        log.error("AP_IPS is empty. Add one or more AP IP addresses in the code array.")
        return

    counts = RebootCountStore(COUNT_FILE)
    counts.initialize(ap_ips)
    log.info("Running continuous reboot test on %d AP(s)", len(ap_ips))
    log.info("Per-AP reboot counts are stored in %s", COUNT_FILE)
    for ip in ap_ips:
        thread = threading.Thread(
            target=run_one_ap_forever,
            args=(ip, counts),
            name=f"AP-{ip}",
            daemon=True,
        )
        thread.start()

    try:
        # Daemon workers keep each AP running until the operator stops the script.
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        log.info("Stopped by operator. Persisted reboot counts are in %s", COUNT_FILE)


if __name__ == "__main__":
    main()