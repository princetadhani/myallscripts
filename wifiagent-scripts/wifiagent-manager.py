#!/usr/bin/env python3
'''
AP WiFi Agent Manager

Manages the wifiagent process on Arista Access Points via SSH using OTP
challenge-response authentication.

Actions:
  running   - Start wifiagent if stopped; no-op if already running (default)
  start     - Start wifiagent (installs if binary is missing on the AP)
  stop      - Stop wifiagent process
  restart   - Stop then start wifiagent
  status    - Show wifiagent running state and PID
  install   - Force re-download and install wifiagent from source URL

The AP IP list is hard-coded in AP_IPS at the top of this file.

Examples:
---------
  ./wifiagent-manager.py                      # Ensure running on all APs
  ./wifiagent-manager.py --action status      # Check status
  ./wifiagent-manager.py --action start       # Start
  ./wifiagent-manager.py --action restart     # Restart
  ./wifiagent-manager.py --action install     # Force reinstall
  ./wifiagent-manager.py --ap 10.86.58.139    # Target a single AP
  ./wifiagent-manager.py --ap 10.86.58.139,10.86.58.140  # Target multiple APs
  ./wifiagent-manager.py --help               # Show help

Author:  Prince Tadhani
Created: 2026-06-25

py wifiagent-manager.py --action status --ap 10.86.205.122,10.86.204.204,10.86.205.60,10.86.205.223,10.86.204.227,10.86.205.165

py wifiagent-manager.py --action status --ap 10.86.205.82,10.86.205.240,10.86.205.157,10.86.205.90,10.86.205.88,10.86.205.49

py wifiagent-manager.py --action status --ap 10.87.169.175,10.87.169.130,10.87.169.87,10.87.169.17,10.87.169.113,10.87.169.112,10.87.169.243
'''

# Standard Library Modules
import argparse
import json
import logging
import sys
import textwrap
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# Third-Party Modules
import pexpect
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_print_lock  = threading.Lock()


def _print(msg=''):
    '''Thread-safe print so parallel AP output does not interleave.'''
    with _print_lock:
        print(msg)

# ---------------------------------------------------------------------------
# AP IP List  –  edit this list to target different APs
# ---------------------------------------------------------------------------
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
# ---------------------------------------------------------------------------
# Wifi OTP signing endpoint used by the arista-ssh-agent Response[...] challenge/
# response prompt (same as SWAT's otpLib.getWifiOTP(), and qwrap-manager.py).
# ---------------------------------------------------------------------------
WIFI_OTP_URL = 'https://license.aristanetworks.com/sign/wifi-otp/'
WIFI_OTP_KEY = 'd9ca932a4bc8fbaaa5021b00e14dd453d469eaaf'

# -------------------------------\--------------------------------------------
# AP SSH credentials
# ---------------------------------------------------------------------------
CONFIG_USER = 'config'
CONFIG_PASS = 'Config@123'
ROOT_USER   = 'root'
DEBUG_ROOT_PASS = 'arastra'   # plain root password accepted on debug builds (no OTP)

# ---------------------------------------------------------------------------
# SSH / pexpect constants
# ---------------------------------------------------------------------------
SSH_TIMEOUT   = 30
CONFIG_PROMPT = r']\$\s*'          # e.g. "hostname]$ "
ROOT_PROMPT   = r'~ # '            # root shell prompt, e.g. "~ # " (consume the leading "~" too)
SSH_OPTS      = (
    '-o StrictHostKeyChecking=no '
    '-o UserKnownHostsFile=/dev/null '
    '-o ConnectTimeout=15 '
    '-o LogLevel=ERROR'
)

# ---------------------------------------------------------------------------
# WifiAgent file paths
# ---------------------------------------------------------------------------
AP_DIR       = '/root/wifiagent'
AP_BIN_PATH  = f'{AP_DIR}/wifiagent.app/wifiagent'
AP_CONF_PATH = f'{AP_DIR}/conf/config.yaml'

# WIFIAGENT_BASE_URL = 'https://pune-abz-tftp.dt1.wifi.arista.cloud/tftp/misc/wifiagent/LinuxARM64Install'
# WIFIAGENT_BIN_URL  = f'{WIFIAGENT_BASE_URL}/wifiagent.app/wifiagent'
# WIFIAGENT_CONF_URL = f'{WIFIAGENT_BASE_URL}/config.yaml'

WIFIAGENT_BASE_URL = 'https://10.86.107.57/sensorimages/airtight/please-dont-delete/qwrap-wifiagent'
WIFIAGENT_BIN_URL  = f'{WIFIAGENT_BASE_URL}/wifiagent.app/wifiagent'
WIFIAGENT_CONF_URL = f'{WIFIAGENT_BASE_URL}/config.yaml'
# ===========================================================================
# OTP helper – stateless token-based signing (same as qwrap-manager.py)
# ===========================================================================

def getWifiOtp(challenge):
    '''Sign an arista-ssh-agent Response[...] challenge via the Wifi OTP
    endpoint. Stateless (no session/login), so it is safe to call from any
    number of threads at once.'''
    header = {
        'Authorization': f'Token {WIFI_OTP_KEY}',
        'Content-Type':  'application/json',
    }
    payload = json.dumps({'message': challenge})
    response = requests.post(WIFI_OTP_URL, headers=header, data=payload, allow_redirects=True)
    if response.status_code != 201:
        raise RuntimeError(f'Wifi OTP request failed ({response.status_code}) for challenge {challenge}')
    return response.json()['signature']


# ===========================================================================
# SSH helpers
# ===========================================================================

def _authenticate(child, password, host, promptPattern=None, timeout=SSH_TIMEOUT):
    '''Shared login challenge/response loop (host-key/password/OTP), same as
    qwrap-manager.py's _authenticate() / SWAT's cliLib._createSshSession().
    Used for both interactive SSH sessions (where promptPattern is the shell
    prompt to land on) and one-shot commands (where promptPattern is None and
    we just wait for EOF once authenticated).'''
    patterns = ['continue connecting', '[Pp]assword:', r'Response\[([^\]]{54})', 'Enter .*code',
                pexpect.EOF, pexpect.TIMEOUT]
    if promptPattern:
        patterns = [promptPattern] + patterns
        offset   = 1
    else:
        offset = 0

    while True:
        result = child.expect(patterns, timeout=timeout)
        if promptPattern and result == 0:
            return
        idx = result - offset
        if idx == 0:                        # host-key prompt
            child.sendline('yes')
        elif idx == 1:                      # password prompt
            child.sendline(password)
        elif idx == 2:
            # arista-ssh-agent challenge/response prompt. Sign the challenge
            # via the Wifi OTP endpoint and send the resulting one-time
            # password back.
            challenge = child.match.group(1)
            child.sendline(getWifiOtp(challenge))
        elif idx == 3:
            raise RuntimeError(f'{host}: MFA/OTP prompt received but not supported by this script')
        elif idx == 4:
            # EOF: fine for one-shot commands (finished after auth), an error
            # if we were still waiting to land on an interactive shell prompt.
            if promptPattern:
                raise RuntimeError(f'Failed to connect/login to {host}')
            return
        else:
            raise RuntimeError(f'Failed to connect/login to {host}')


def runConcurrently(func, apList, actionName):
    '''Run func(ip) concurrently across apList (one worker per AP), same
    pattern as qwrap-manager.py's runConcurrently(). A raised exception or a
    falsy return value both count as a per-AP failure. Exits the process if
    any AP fails.'''
    errors  = []
    results = {}
    with ThreadPoolExecutor(max_workers=len(apList)) as executor:
        futureToHost = {executor.submit(func, ip): ip for ip in apList}
        for future in as_completed(futureToHost):
            host = futureToHost[future]
            try:
                result = future.result()
                results[host] = result
                if result:
                    logging.info(f'{host}: {actionName} succeeded')
                else:
                    logging.error(f'{host}: {actionName} FAILED')
                    errors.append(host)
            except Exception as e:
                logging.error(f'{host}: {actionName} FAILED: {e}')
                results[host] = False
                errors.append(host)
    if errors:
        logging.error(f'{actionName} failed on: {errors}')
        sys.exit(1)
    return results


def _unlock_rootuser(ip):
    '''
    Open a config-user SSH session and run "rootuser unlock" so that a
    subsequent root SSH session can authenticate.
    '''
    logging.info(f'[{ip}] Unlocking rootuser via config shell')
    cmd = f'ssh {SSH_OPTS} {CONFIG_USER}@{ip}'
    conn = pexpect.spawn(cmd, timeout=SSH_TIMEOUT, encoding='utf-8')

    try:
        _authenticate(conn, CONFIG_PASS, ip, promptPattern=CONFIG_PROMPT, timeout=SSH_TIMEOUT)
        conn.sendline('privilege access')
        conn.expect(CONFIG_PROMPT, timeout=SSH_TIMEOUT)
        conn.sendline('rootuser unlock')
        conn.expect(CONFIG_PROMPT, timeout=SSH_TIMEOUT)
        conn.sendline('exit')
        conn.expect(pexpect.EOF, timeout=SSH_TIMEOUT)

    finally:
        try:
            conn.close(force=True)
        except Exception:
            pass

    logging.info(f'[{ip}] Rootuser unlocked')


def _try_root_direct(ip, timeout=SSH_TIMEOUT):
    '''
    Attempt SSH login directly as root, skipping the config-user unlock hop.
    Handles three cases seen in the field:
      - Normal case: arista-ssh-agent Response[...] challenge -> sign with
        the Wifi OTP endpoint and reply.
      - Debug build: root accepts a plain password ("arastra"), no OTP.
      - Rootuser locked: a password prompt appears but any password is
        rejected (re-prompted, or the connection is closed) -> rootuser
        needs to be unlocked via the config hop first.

    Returns an open pexpect conn sitting at the root prompt on success, or
    None if the config-hop fallback is required.
    '''
    logging.info(f'[{ip}] Trying direct root login (no config hop)')
    cmd = f'ssh {SSH_OPTS} {ROOT_USER}@{ip}'
    conn = pexpect.spawn(cmd, timeout=timeout, encoding='utf-8')
    patterns = [ROOT_PROMPT, 'continue connecting', r'[Pp]assword:',
                r'Response\[([^\]]{54})', pexpect.EOF, pexpect.TIMEOUT]
    triedDebugPassword = False

    while True:
        idx = conn.expect(patterns, timeout=timeout)
        if idx == 0:                        # root prompt reached
            logging.info(f'[{ip}] Direct root login succeeded')
            return conn
        elif idx == 1:                      # host-key prompt
            conn.sendline('yes')
        elif idx == 2:                      # password prompt
            if triedDebugPassword:
                # Debug password rejected too -> rootuser is locked.
                logging.info(f'[{ip}] Root password rejected – rootuser is locked')
                conn.close(force=True)
                return None
            triedDebugPassword = True
            conn.sendline(DEBUG_ROOT_PASS)
        elif idx == 3:                      # OTP challenge
            challenge = conn.match.group(1)
            conn.sendline(getWifiOtp(challenge))
        elif idx == 4:                      # EOF – closed before a shell prompt
            logging.info(f'[{ip}] Direct root SSH closed early – rootuser likely locked')
            return None
        else:                                # TIMEOUT
            conn.close(force=True)
            return None


def _ssh_root(ip):
    '''
    SSH as root. Tries a direct root login first (OTP challenge, or on debug
    builds a plain password) to avoid the slower config-user unlock hop on
    every call. Falls back to unlocking rootuser via the config shell only
    when the direct attempt shows rootuser is locked.
    Returns an open pexpect session sitting at the root shell prompt.
    '''
    conn = _try_root_direct(ip)
    if conn:
        return conn

    logging.info(f'[{ip}] Falling back to config-user unlock + root login')
    _unlock_rootuser(ip)

    logging.info(f'[{ip}] Connecting as root')
    cmd = f'ssh {SSH_OPTS} {ROOT_USER}@{ip}'
    conn = pexpect.spawn(cmd, timeout=SSH_TIMEOUT, encoding='utf-8')
    _authenticate(conn, '', ip, promptPattern=ROOT_PROMPT, timeout=SSH_TIMEOUT)
    logging.info(f'[{ip}] Root shell ready')
    return conn


def _close(conn):
    '''Quietly close a pexpect session.'''
    try:
        conn.sendline('exit')
        conn.close()
    except Exception:
        pass


def _get_exit_code(conn):
    '''Read the exit code of the last command via a separate echo $?.'''
    conn.sendline('echo $?')
    conn.expect(ROOT_PROMPT, timeout=10)
    exitLines = [l.strip() for l in (conn.before or '').splitlines() if l.strip()]
    return next((l for l in reversed(exitLines) if l.isdigit()), None)


def _verify_file_size(conn, ip, path):
    '''Return file size in bytes; raises if file is empty or missing.'''
    conn.sendline(f'wc -c < {path} 2>/dev/null || echo 0')
    conn.expect(ROOT_PROMPT, timeout=15)
    sizeLines = [l.strip() for l in (conn.before or '').splitlines() if l.strip()]
    size = int(next((l for l in reversed(sizeLines) if l.isdigit()), '0'))
    if size == 0:
        raise RuntimeError(f'[{ip}] downloaded file is empty: {path}')
    return size


def _download_on_ap(conn, ip, url, dstPath, timeout=120):
    '''
    Download url to dstPath on the AP using curl.
    BusyBox wget lacks SSL support so curl is used for HTTPS URLs.
    '''
    errLog = '/tmp/.dl_err.txt'
    _print(f'[{ip}]  downloading {url}')

    conn.sendline(f'curl -fsSLk -o {dstPath} "{url}" 2>{errLog}')
    conn.expect(ROOT_PROMPT, timeout=timeout)
    exitCode = _get_exit_code(conn)

    if exitCode != '0':
        conn.sendline(f'cat {errLog} 2>/dev/null')
        conn.expect(ROOT_PROMPT, timeout=10)
        errLines = [l.strip() for l in (conn.before or '').splitlines() if l.strip()]
        _print(f'[{ip}]  curl exit={exitCode}  error: {" | ".join(errLines) or "(no output)"}')
        raise RuntimeError(f'[{ip}] download failed (exit {exitCode}) for {url}')

    size = _verify_file_size(conn, ip, dstPath)
    _print(f'[{ip}]    -> {dstPath}  ({size:,} bytes)')


def _run(conn, ip, cmd, timeout=30):
    '''Send a shell command and return the output lines (echoed command stripped).'''
    conn.sendline(cmd)
    conn.expect(ROOT_PROMPT, timeout=timeout)
    output = conn.before or ''
    lines  = [l.strip() for l in output.splitlines() if l.strip()]
    if lines and cmd.strip() in lines[0]:
        lines = lines[1:]
    return lines


def _file_exists(conn, ip, path):
    '''Return True if path exists on the AP.'''
    conn.sendline(f'test -f {path} && echo __FILE_EXISTS__ || echo __FILE_MISSING__')
    conn.expect(ROOT_PROMPT, timeout=30)
    return '__FILE_EXISTS__' in (conn.before or '')


def _get_pid(conn, ip):
    '''
    Return the wifiagent PID string, or None if not running.
    Uses ps instead of pidof because busybox pidof is unreliable on some AP images.
    Pattern [w]ifiagent prevents the grep process itself from matching.
    '''
    conn.sendline('ps | grep "[w]ifiagent"')
    conn.expect(ROOT_PROMPT, timeout=30)
    out = (conn.before or '').strip()
    for line in out.splitlines():
        parts = line.strip().split()
        if parts and parts[0].isdigit() and 'wifiagent' in line:
            return parts[0]
    return None


def _start_binary(conn, ip):
    '''
    chmod + launch wifiagent in the background.
    Captures any immediate stderr to help diagnose startup failures.
    Returns PID string on success, None otherwise.
    '''
    _print(f'[{ip}]  chmod +x {AP_BIN_PATH}')
    _run(conn, ip, f'chmod +x {AP_BIN_PATH}')

    # Run with stderr captured to a temp file so we can show it on failure
    errLog = '/tmp/wifiagent_start.log'
    launchCmd = f'{AP_BIN_PATH} -conf {AP_CONF_PATH} > /dev/null 2>{errLog} &'
    _print(f'[{ip}]  {launchCmd}')
    _run(conn, ip, launchCmd, timeout=10)

    # Poll for the process instead of a single fixed sleep+check: on a slow/busy
    # AP the binary can take longer than 2s to appear in `ps`, which otherwise
    # causes intermittent false "may not have started" warnings.
    pid = None
    for _ in range(5):
        time.sleep(1)
        pid = _get_pid(conn, ip)
        if pid:
            break

    if not pid:
        # Show any error output the binary wrote before it died
        errLines = _run(conn, ip, f'cat {errLog} 2>/dev/null || true')
        if errLines:
            _print(f'[{ip}]  -- binary stderr --')
            for line in errLines:
                _print(f'[{ip}]    {line}')
            _print(f'[{ip}]  -- end stderr --')

        # Also show what is actually running to aid diagnosis
        psLines = _run(conn, ip, 'ps | grep -v grep | grep -i wifi || echo "(no wifi process found)"')
        _print(f'[{ip}]  ps check: {" | ".join(psLines)}')

    return pid


# ===========================================================================
# WiFiAgent actions
# ===========================================================================

def action_status(ip):
    '''
    Check and print wifiagent status on an AP.

    Inputs:   <ip> - AP IP address

    Output:   Prints RUNNING/STOPPED status and PID
    '''
    logging.info(f'[{ip}] Checking wifiagent status')
    _print(f'\n--- {ip} ---')
    try:
        conn = _ssh_root(ip)
        try:
            pid = _get_pid(conn, ip)
            if pid:
                _print(f'[{ip}]  wifiagent  RUNNING   PID={pid}')
            else:
                _print(f'[{ip}]  wifiagent  STOPPED')
        finally:
            _close(conn)
        return True
    except Exception as e:
        _print(f'[{ip}]  ERROR: {e}')
        logging.error(f'[{ip}] status failed: {e}')
        return False


def action_stop(ip):
    '''
    Stop the wifiagent process on an AP.

    Inputs:   <ip> - AP IP address

    Output:   None
    '''
    logging.info(f'[{ip}] Stopping wifiagent')
    _print(f'\n--- {ip} ---')
    try:
        conn = _ssh_root(ip)
        try:
            pid = _get_pid(conn, ip)
            if not pid:
                _print(f'[{ip}]  wifiagent is already stopped')
                return True
            _run(conn, ip, 'killall wifiagent 2>/dev/null || pkill -x wifiagent 2>/dev/null || true')
            time.sleep(1)
            pid = _get_pid(conn, ip)
            if not pid:
                _print(f'[{ip}]  wifiagent stopped')
                return True
            _print(f'[{ip}]  WARNING: wifiagent still running (PID={pid})')
            return False
        finally:
            _close(conn)
    except Exception as e:
        _print(f'[{ip}]  ERROR: {e}')
        logging.error(f'[{ip}] stop failed: {e}')
        return False


def action_start(ip):
    '''
    Start wifiagent on an AP. Installs the binary first if it is missing.

    Inputs:   <ip> - AP IP address

    Output:   None
    '''
    logging.info(f'[{ip}] Starting wifiagent')
    _print(f'\n--- {ip} ---')
    try:
        conn = _ssh_root(ip)
        try:
            pid = _get_pid(conn, ip)
            if pid:
                _print(f'[{ip}]  wifiagent already running (PID={pid})')
                return True

            binOk  = _file_exists(conn, ip, AP_BIN_PATH)
            confOk = _file_exists(conn, ip, AP_CONF_PATH)

            if not binOk or not confOk:
                _print(f'[{ip}]  Binary/conf missing – installing wifiagent first')
                _close(conn)
                return _do_install(ip, force=False, skipRunningCheck=True)

            pid = _start_binary(conn, ip)
            if pid:
                _print(f'[{ip}]  wifiagent started  PID={pid}')
                return True
            _print(f'[{ip}]  WARNING: wifiagent may not have started')
            return False
        finally:
            _close(conn)
    except Exception as e:
        _print(f'[{ip}]  ERROR: {e}')
        logging.error(f'[{ip}] start failed: {e}')
        return False


def action_restart(ip):
    '''
    Stop then start wifiagent on an AP.

    Inputs:   <ip> - AP IP address

    Output:   None
    '''
    logging.info(f'[{ip}] Restarting wifiagent')
    _print(f'\n--- {ip} ---')
    try:
        conn = _ssh_root(ip)
        try:
            pid = _get_pid(conn, ip)
            if pid:
                _run(conn, ip, 'killall wifiagent 2>/dev/null || pkill -x wifiagent 2>/dev/null || true')
                time.sleep(1)
                _print(f'[{ip}]  wifiagent stopped')

            binOk  = _file_exists(conn, ip, AP_BIN_PATH)
            confOk = _file_exists(conn, ip, AP_CONF_PATH)

            if not binOk or not confOk:
                _print(f'[{ip}]  Binary/conf missing – installing wifiagent first')
                _close(conn)
                return _do_install(ip, force=False, skipRunningCheck=True)

            pid = _start_binary(conn, ip)
            if pid:
                _print(f'[{ip}]  wifiagent restarted  PID={pid}')
                return True
            _print(f'[{ip}]  WARNING: wifiagent may not have restarted')
            return False
        finally:
            _close(conn)
    except Exception as e:
        _print(f'[{ip}]  ERROR: {e}')
        logging.error(f'[{ip}] restart failed: {e}')
        return False


def _do_install(ip, force=True, skipRunningCheck=False):
    '''
    Install wifiagent on the AP.

    Steps:
      1. Optionally check running state and bail early if already OK.
      2. SSH: kill running instance, mkdir, download both files directly on the AP.
      3. SSH: chmod + launch the binary.
    '''
    # Step 1 – early-exit checks when not forcing a reinstall
    if not force and not skipRunningCheck:
        conn = _ssh_root(ip)
        try:
            pid = _get_pid(conn, ip)
            if pid:
                _print(f'[{ip}]  wifiagent already running (PID={pid}) – skipping install')
                return True
            binOk  = _file_exists(conn, ip, AP_BIN_PATH)
            confOk = _file_exists(conn, ip, AP_CONF_PATH)
            if binOk and confOk:
                _print(f'[{ip}]  Binary already present – starting without reinstall')
                pid = _start_binary(conn, ip)
                if pid:
                    _print(f'[{ip}]  wifiagent started  PID={pid}')
                    return True
                return False
        finally:
            _close(conn)

    # Steps 2-4 in a single SSH session
    conn = _ssh_root(ip)
    try:
        # Step 2 – kill running instance + create dirs on AP
        _run(conn, ip, 'killall wifiagent 2>/dev/null || pkill -x wifiagent 2>/dev/null || true')
        time.sleep(1)
        _run(conn, ip, f'mkdir -p {AP_DIR}/wifiagent.app {AP_DIR}/conf')

        # Step 3 – AP downloads directly from the source server
        _print(f'[{ip}]  AP downloading wifiagent binary...')
        _download_on_ap(conn, ip, WIFIAGENT_BIN_URL, AP_BIN_PATH, timeout=120)
        _print(f'[{ip}]  AP downloading config...')
        _download_on_ap(conn, ip, WIFIAGENT_CONF_URL, AP_CONF_PATH, timeout=30)

        # Step 4 – chmod + launch
        pid = _start_binary(conn, ip)
        if pid:
            _print(f'[{ip}]  wifiagent installed and started  PID={pid}')
            return True
        _print(f'[{ip}]  WARNING: install done but wifiagent may not have started')
        return False
    finally:
        _close(conn)


def action_install(ip):
    '''
    Force re-download and install wifiagent from source URL on an AP.

    Inputs:   <ip> - AP IP address

    Output:   None
    '''
    logging.info(f'[{ip}] Installing wifiagent (force)')
    _print(f'\n--- {ip} ---')
    try:
        return _do_install(ip, force=True)
    except Exception as e:
        _print(f'[{ip}]  ERROR: {e}')
        logging.error(f'[{ip}] install failed: {e}')
        return False


# ===========================================================================
# running action
# ===========================================================================

def action_running(ip):
    '''
    Ensure wifiagent is running on an AP. If stopped, start it;
    if binary is missing, install first. No-op if already running.

    Inputs:   <ip> - AP IP address

    Output:   None
    '''
    logging.info(f'[{ip}] Ensuring wifiagent is running')
    _print(f'\n--- {ip} ---')
    try:
        conn = _ssh_root(ip)
        try:
            pid = _get_pid(conn, ip)
            if pid:
                _print(f'[{ip}]  wifiagent already running (PID={pid}) – nothing to do')
                return True

            binOk  = _file_exists(conn, ip, AP_BIN_PATH)
            confOk = _file_exists(conn, ip, AP_CONF_PATH)

            if not binOk or not confOk:
                _print(f'[{ip}]  Binary/conf missing – installing wifiagent')
                _close(conn)
                return _do_install(ip, force=False, skipRunningCheck=True)

            pid = _start_binary(conn, ip)
            if pid:
                _print(f'[{ip}]  wifiagent was stopped – now started  PID={pid}')
                return True
            _print(f'[{ip}]  WARNING: wifiagent may not have started')
            return False
        finally:
            _close(conn)
    except Exception as e:
        _print(f'[{ip}]  ERROR: {e}')
        logging.error(f'[{ip}] running failed: {e}')
        return False


# ===========================================================================
# Entry point
# ===========================================================================

ACTIONS = {
    'running': action_running,
    'start':          action_start,
    'stop':           action_stop,
    'restart':        action_restart,
    'status':         action_status,
    'install':        action_install,
}

_EXAMPLES = textwrap.dedent('''\
    examples:
      wifiagent-manager.py                                  # ensure agent is running on all APs
      wifiagent-manager.py --action status                  # check status on all APs
      wifiagent-manager.py --action start                   # start agent on all APs
      wifiagent-manager.py --action restart                 # restart agent on all APs
      wifiagent-manager.py --action install                 # force reinstall on all APs
      wifiagent-manager.py --action stop                    # stop agent on all APs
      wifiagent-manager.py --ap 10.86.58.139                # only manage a specific AP
      wifiagent-manager.py --ap 10.86.58.139,10.86.58.140   # manage multiple APs
      wifiagent-manager.py --no-parallel                    # run sequentially instead of in parallel
''')


class _HelpFormatter(argparse.RawDescriptionHelpFormatter):
    def __init__(self, prog):
        super().__init__(prog, max_help_position=40, width=200)


def resolveApList(options) -> list[str]:
    '''Turn --ap into a filtered AP_IPS list. Warns (does not exit) on unknown
    host(s), matching this script's existing lenient behavior.'''
    if not options.ap:
        return AP_IPS
    targets = [ip.strip() for ip in options.ap.split(',') if ip.strip()]
    for ip in targets:
        if ip not in AP_IPS:
            print(f'Warning: {ip} is not in the AP_IPS list – proceeding anyway')
    return targets


def main():
    parser = argparse.ArgumentParser(
        prog='wifiagent-manager.py',
        usage=argparse.SUPPRESS,
        description='Manage WiFi agents on Access Points via SSH',
        epilog=_EXAMPLES,
        formatter_class=_HelpFormatter,
    )
    parser.add_argument(
        '--action',
        choices=sorted(ACTIONS),
        default='running',
        help='Action to perform (default: running)',
    )
    parser.add_argument(
        '--ap',
        default=None,
        metavar='AP[,AP...]',
        help='Specific AP IP(s) to manage, comma-separated (default: manage all APs)',
    )
    parser.add_argument(
        '--parallel',
        action=argparse.BooleanOptionalAction,
        default=True,
        help='Run operations in parallel (default: True; use --no-parallel to run sequentially)',
    )
    parser.add_argument(
        '--logLevel',
        default='WARNING',
        metavar='LEVEL',
        help='Logging level (default: WARNING)',
    )
    options = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, options.logLevel.upper(), logging.WARNING),
        format='%(asctime)s %(levelname)s %(message)s',
        stream=sys.stdout,
    )

    # Resolve target AP list (comma-separated: --ap ip1,ip2,...)
    targets = resolveApList(options)

    actionFn = ACTIONS[options.action]
    print(f'Action: {options.action}  |  APs ({len(targets)}): {", ".join(targets)}'
          f'  |  parallel={options.parallel}')

    if options.parallel and len(targets) > 1:
        runConcurrently(actionFn, targets, options.action)
    else:
        results = {}
        for ip in targets:
            try:
                results[ip] = bool(actionFn(ip))
            except Exception as exc:
                _print(f'[{ip}]  UNHANDLED ERROR: {exc}')
                results[ip] = False
        failed = [ip for ip in targets if not results.get(ip)]
        if failed:
            print(f'\nFailed APs ({len(failed)}/{len(targets)}): {", ".join(failed)}')
            sys.exit(1)

    print(f'\nAll {len(targets)} AP(s) succeeded.')


if __name__ == '__main__':
    main()
