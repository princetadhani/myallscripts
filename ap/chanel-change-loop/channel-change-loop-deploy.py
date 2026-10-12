#!/usr/bin/env python3
"""Copy channel-change-loop.sh to APs and start it with nohup.

Examples:
  python3 channel-change-loop-deploy.py --ap 10.86.205.157
  python3 channel-change-loop-deploy.py --ap 10.86.205.157,10.86.205.158
  python3 channel-change-loop-deploy.py --ap 10.86.205.157 --debug
"""

import argparse
import json
import logging
import os
import re
import shlex
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import pexpect
import requests

AP_LIST = []
CLI_USERNAME = "root"
CLI_PASSWORD = "arastra"

LOCAL_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "channel-change-loop.sh")
REMOTE_SCRIPT = "/root/channel-change-loop.sh"
REMOTE_LOG = "/opt/pstore_logs/channel-change-loop.log"
REMOTE_PID = "/opt/pstore_logs/channel-change-loop.pid"

WIFI_OTP_URL = "https://license.aristanetworks.com/sign/wifi-otp/"
WIFI_OTP_KEY = "d9ca932a4bc8fbaaa5021b00e14dd453d469eaaf"
ROOT_PROMPT = r"#\s*$"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("channel-change-loop-deploy")


def get_wifi_otp(challenge):
    response = requests.post(
        WIFI_OTP_URL,
        headers={"Authorization": f"Token {WIFI_OTP_KEY}", "Content-Type": "application/json"},
        data=json.dumps({"message": challenge}),
        allow_redirects=True,
        timeout=30,
    )
    if response.status_code != 201:
        raise RuntimeError(f"Wifi OTP request failed ({response.status_code})")
    return response.json()["signature"]


def ssh_env():
    env = {"TERM": "dumb", "HOME": os.environ.get("HOME", "")}
    if os.environ.get("SSH_AUTH_SOCK"):
        env["SSH_AUTH_SOCK"] = os.environ["SSH_AUTH_SOCK"]
    return env


def authenticate(child, host, prompt_pattern=None, timeout=60):
    patterns = [
        "continue connecting",
        "[Pp]assword:",
        r"Response\[([^\]]{54})",
        "Enter .*code",
        pexpect.EOF,
        pexpect.TIMEOUT,
    ]
    offset = 0
    if prompt_pattern:
        patterns.insert(0, prompt_pattern)
        offset = 1

    while True:
        result = child.expect(patterns, timeout=timeout)
        if prompt_pattern and result == 0:
            return
        result -= offset
        if result == 0:
            child.sendline("yes")
        elif result == 1:
            child.sendline(CLI_PASSWORD)
        elif result == 2:
            child.sendline(get_wifi_otp(child.match.group(1)))
        elif result == 3:
            raise RuntimeError(f"{host}: unsupported MFA prompt")
        elif result == 4 and not prompt_pattern:
            return
        else:
            raise RuntimeError(f"{host}: failed to connect/login")


def scp_to_ap(host):
    command = (
        "scp -O -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
        f"{shlex.quote(LOCAL_SCRIPT)} {CLI_USERNAME}@{host}:{shlex.quote(REMOTE_SCRIPT)}"
    )
    log.info("[%s] copying %s", host, REMOTE_SCRIPT)
    child = pexpect.spawn(command, env=ssh_env(), encoding="utf-8", timeout=60)
    try:
        authenticate(child, host, timeout=60)
        transcript = child.before or ""
        child.close()
        if child.exitstatus not in (0, None):
            raise RuntimeError(f"{host}: scp failed (exit={child.exitstatus}): {transcript.strip()}")
    finally:
        if child.isalive():
            child.close(force=True)


def run_remote(host, command, timeout=30):
    ssh_command = (
        "ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
        f"{CLI_USERNAME}@{host}"
    )
    child = pexpect.spawn(ssh_command, env=ssh_env(), encoding="utf-8", timeout=timeout)
    try:
        authenticate(child, host, ROOT_PROMPT, timeout)
        log.info("[%s] running: %s", host, command)
        child.sendline(command)
        child.expect(ROOT_PROMPT, timeout=timeout)
        output = child.before or ""
        log.debug("[%s] remote output: %r", host, output)
        return output
    finally:
        try:
            child.sendline("exit")
        except Exception:
            pass
        if child.isalive():
            child.close(force=True)


def deploy(host):
    # Stop only the process recorded by this tool; do not broadly kill shells.
    stop_command = (
        f'if [ -f {REMOTE_PID} ]; then '
        f'old_pid=$(cat {REMOTE_PID}); '
        'case "$old_pid" in *[!0-9]*|"") ;; *) kill "$old_pid" 2>/dev/null || true ;; esac; '
        f'rm -f {REMOTE_PID}; fi'
    )
    run_remote(host, stop_command)
    scp_to_ap(host)
    start_command = (
        f"chmod +x {REMOTE_SCRIPT} && "
        f"nohup {REMOTE_SCRIPT} >> {REMOTE_LOG} 2>&1 & echo $! > {REMOTE_PID}"
    )
    run_remote(host, start_command)
    output = run_remote(host, f"cat {REMOTE_PID}")
    pids = re.findall(r"(?m)^\s*(\d+)\s*$", output)
    pid = pids[-1] if pids else "unknown"
    log.info("[%s] started pid=%s; log=%s", host, pid, REMOTE_LOG)
    return pid


def parse_args():
    parser = argparse.ArgumentParser(description="Deploy and start the channel-change loop on APs")
    parser.add_argument("--ap", metavar="HOST[,HOST...]", help="AP host/IP list (overrides AP_LIST)")
    parser.add_argument("--debug", action="store_true", help="enable debug logging")
    return parser.parse_args()


def main():
    args = parse_args()
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    if not os.path.isfile(LOCAL_SCRIPT):
        log.error("local script not found: %s", LOCAL_SCRIPT)
        return 1

    hosts = [item.strip() for item in args.ap.split(",") if item.strip()] if args.ap else list(AP_LIST)
    if not hosts:
        log.error("no APs specified; use --ap HOST[,HOST...] or populate AP_LIST")
        return 1

    failures = []
    with ThreadPoolExecutor(max_workers=len(hosts)) as executor:
        futures = {executor.submit(deploy, host): host for host in hosts}
        for future in as_completed(futures):
            host = futures[future]
            try:
                future.result()
            except Exception as exc:
                failures.append(host)
                log.error("[%s] deployment failed: %s", host, exc)

    if failures:
        log.error("failed APs: %s", ", ".join(failures))
        return 1
    log.info("deployment succeeded on all %d AP(s)", len(hosts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
