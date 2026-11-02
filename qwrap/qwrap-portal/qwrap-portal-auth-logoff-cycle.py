#!/usr/bin/env python3
"""Repeat QWRAP portal auth/logoff independently on each AP.

Install: python3 -m pip install pexpect requests
Run: nohup python3 qwrap-portal-auth-logoff-cycle.py > /dev/null 2>&1 &
Edit AP_LIST below before running. Logs rotate beside this script.
Each action opens a fresh root SSH session and closes it before waiting.
Runs auth → wait 8to10 minutes → logoff → wait 2to4 minutes → repeat, independently per AP.
Stopping leaves clients in their current state; no final logoff is sent.
"""

import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import random
import re
import signal
import threading
from concurrent.futures import ThreadPoolExecutor

import pexpect
import requests

# Add your APs here (uncomment the examples if these are your targets).
AP_LIST: list[str] = [
# For S2 QWRAP
'10.86.205.82',
'10.86.205.240',
'10.86.205.157',
'10.86.205.90',
'10.86.205.88',
'10.86.205.49',
]
CONFIG_USER = os.environ.get('QWRAP_CONFIG_USER', 'config')
CONFIG_PASSWORD = os.environ.get('QWRAP_CONFIG_PASSWORD', 'Config@123')
ROOT_PASSWORD = os.environ.get('QWRAP_ROOT_PASSWORD', 'arastra')
AUTH_WAIT_SECONDS = (8 * 60, 10 * 60)
LOGOFF_WAIT_SECONDS = (2 * 60, 4 * 60)
RETRY_SECONDS = 60
SSH_TIMEOUT = 30
COMMAND_TIMEOUT = 120
SCRIPT_DIR = Path(__file__).resolve().parent
LOG_FILE = SCRIPT_DIR / 'qwrap-portal-cycle.log'
ROOT_PROMPT = r'(?m)^[^\r\n]*#\s*$'
CONFIG_PROMPT = r'(?m)^[^\r\n]*\]\$\s*$'
WIFI_OTP_URL = 'https://license.aristanetworks.com/sign/wifi-otp/'
# Built-in signing token; optionally override it through the environment.
WIFI_OTP_KEY = os.environ.get(
    'QWRAP_WIFI_OTP_KEY', 'd9ca932a4bc8fbaaa5021b00e14dd453d469eaaf'
)
STOP = threading.Event()
log = logging.getLogger('qwrap_portal_cycle')


def get_wifi_otp(challenge):
    if not WIFI_OTP_KEY:
        raise RuntimeError('Set WIFI_OTP_KEY in this script or QWRAP_WIFI_OTP_KEY in the environment')
    response = requests.post(
        WIFI_OTP_URL,
        headers={'Authorization': f'Token {WIFI_OTP_KEY}'},
        json={'message': challenge}, timeout=(10, 20),
    )
    if response.status_code != 201:
        raise RuntimeError(f'WiFi OTP signing failed (HTTP {response.status_code})')
    return response.json()['signature']


def login(host, username, password, prompt):
    """Handle password and agent challenge/response with bounded attempts."""
    env = os.environ.copy()  # Preserve SSH_AUTH_SOCK for the Arista agent.
    env['TERM'] = 'dumb'
    child = pexpect.spawn('ssh', [
        '-o', 'StrictHostKeyChecking=no',
        '-o', 'UserKnownHostsFile=/dev/null',
        '-o', 'ConnectTimeout=15',
        '-o', 'ServerAliveInterval=15',
        '-o', 'ServerAliveCountMax=2',
        '-o', 'LogLevel=ERROR', f'{username}@{host}',
    ], env=env, encoding='utf-8', codec_errors='replace', timeout=SSH_TIMEOUT)
    try:
        passwords = challenges = 0
        for _ in range(10):
            index = child.expect([
                prompt, 'continue connecting', r'[Pp]assword:',
                r'Response\[([^\]]{54})', r'Enter .*code',
                pexpect.EOF, pexpect.TIMEOUT,
            ])
            if index == 0:
                return child
            if STOP.is_set():
                raise RuntimeError('Stopping')
            if index == 1:
                child.sendline('yes')
            elif index == 2:
                passwords += 1
                if passwords > 1:
                    raise RuntimeError('SSH password rejected; account may be locked')
                child.sendline(password)
            elif index == 3:
                challenges += 1
                if challenges > 2:
                    raise RuntimeError('SSH OTP authentication rejected')
                child.sendline(get_wifi_otp(child.match.group(1)))
            elif index == 4:
                raise RuntimeError('Interactive MFA is unavailable under nohup')
            else:
                raise RuntimeError('SSH closed or timed out before reaching shell')
        raise RuntimeError('Too many SSH authentication prompts')
    except Exception:
        child.close(force=True)
        raise


def config_command(child, command):
    child.sendline(command)
    child.expect(CONFIG_PROMPT)
    output = child.before.strip()
    if re.search(r'\b(error|failed|invalid|denied)\b', output, re.I):
        raise RuntimeError(f'{command}: {output}')


def root_session(host):
    try:
        return login(host, 'root', ROOT_PASSWORD, ROOT_PROMPT)
    except RuntimeError as error:
        if STOP.is_set():
            raise
        log.warning('[%s] Direct root login failed (%s); trying config unlock', host, error)
    child = login(host, CONFIG_USER, CONFIG_PASSWORD, CONFIG_PROMPT)
    try:
        config_command(child, 'privilege access')
        config_command(child, 'rootuser unlock')
    finally:
        child.close(force=True)
    log.info('[%s] Root unlock commands completed; reconnecting as root', host)
    return login(host, 'root', ROOT_PASSWORD, ROOT_PROMPT)


def run_action(host, action):
    child = root_session(host)
    try:
        if STOP.is_set():
            return False
        command = f'cli -c "qwrap portal radios 1 id 1-28 action {action} version 6"'
        log.info('[%s] Running %s', host, command)
        # A unique marker captures the exit status without confusing command
        # output containing '#' with the root prompt. Split the marker in the
        # sent command so its terminal echo cannot match the result pattern.
        marker = '__QWRAP_PORTAL_RC__'
        child.sendline(command + "; rc=$?; printf '\\n%s%s%d\\n' '__QWRAP_' 'PORTAL_RC__' \"$rc\"")
        child.expect(re.escape(marker) + r'(\d+)\r?\n', timeout=COMMAND_TIMEOUT)
        output = child.before
        status = int(child.match.group(1))
        child.expect(ROOT_PROMPT)
        log.info('[%s] %s exit=%d output:\n%s', host, action, status, output.strip())
        if status or re.search(r'\b(error|failed|invalid|denied)\b', output, re.I):
            raise RuntimeError(f'{action} command reported failure (exit={status}); see log output')
        return True
    finally:
        child.close(force=True)


def cycle_ap(host):
    action = 'auth'
    cycle = 1
    while not STOP.is_set():
        try:
            if not run_action(host, action):
                return
        except Exception as error:
            # Preserve the pending action; one AP never blocks the others.
            log.error('[%s] Cycle %d %s failed: %s; retry in %ds',
                      host, cycle, action, error, RETRY_SECONDS)
            STOP.wait(RETRY_SECONDS)
            continue
        bounds = AUTH_WAIT_SECONDS if action == 'auth' else LOGOFF_WAIT_SECONDS
        delay = random.uniform(*bounds)
        log.info('[%s] Cycle %d %s completed; next action in %.1f minutes',
                 host, cycle, action, delay / 60)
        if STOP.wait(delay):
            break
        if action == 'auth':
            action = 'logoff'
        else:
            action = 'auth'
            cycle += 1
    log.info('[%s] Worker stopped', host)


def main():
    handler = RotatingFileHandler(LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
                        handlers=[handler, logging.StreamHandler()])
    hosts = list(dict.fromkeys(AP_LIST))
    if not hosts:
        log.error('AP_LIST is empty. Edit AP_LIST in %s before running.', __file__)
        return 1
    if any(not isinstance(host, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9.:-]*', host)
           for host in hosts):
        log.error('AP_LIST must contain only IP addresses or hostnames')
        return 1

    def stop(signum, frame):
        STOP.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info('Starting PID=%d on %d APs; log=%s', os.getpid(), len(hosts), LOG_FILE)
    with ThreadPoolExecutor(max_workers=len(hosts)) as pool:
        futures = [pool.submit(cycle_ap, host) for host in hosts]
        for future in futures:
            future.result()
    log.info('Stopped; clients remain in their last portal state')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
