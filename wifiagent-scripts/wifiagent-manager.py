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
  ./wifiagent-manager.py --help               # Show help

Author:  Prince Tadhani
Created: 2026-06-25
'''

# Standard Library Modules
import argparse
import logging
import os
import sys
import textwrap
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# Third-Party Modules
import pexpect
import requests
import urllib3
from dotenv import load_dotenv

load_dotenv()
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_print_lock  = threading.Lock()
_portal_lock = threading.Lock()   # one portal request at a time to avoid auth conflicts


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
'10.87.169.243'
]
# ---------------------------------------------------------------------------
# Portal credentials for OTP challenge-response  (loaded from .env)
# ---------------------------------------------------------------------------
CACHE_FILE    = '/tmp/nssh.txt'
ONELOGIN_USER = os.environ["ONELOGIN_USER"]
ONELOGIN_PASS = os.environ["ONELOGIN_PASS"]
PORTAL_LOGIN  = 'https://license.aristanetworks.com/api-auth/login/'
PORTAL_OTP    = 'https://license.aristanetworks.com/sign/wifi_otp/'

# ---------------------------------------------------------------------------
# AP SSH credentials  (loaded from .env)
# ---------------------------------------------------------------------------
CONFIG_USER = 'config'
CONFIG_PASS = os.environ["CONFIG_PASS"]
ROOT_USER   = 'root'

# ---------------------------------------------------------------------------
# SSH / pexpect constants
# ---------------------------------------------------------------------------
SSH_TIMEOUT   = 30
CONFIG_PROMPT = r']\$\s*'          # e.g. "hostname]$ "
ROOT_PROMPT   = r' # '             # root shell " # " (space-hash-space)
CHALLENGE_RE  = r'Response\[([^\]]{54})\]'
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
# OTP helpers  –  cache-first, then portal
# ===========================================================================

def check_cache(challenge):
    '''Return cached OTP response for a challenge, or None.'''
    if not os.path.exists(CACHE_FILE):
        return None
    with open(CACHE_FILE) as f:
        lines = f.read().splitlines()
    for i, line in enumerate(lines):
        if line == challenge and i + 1 < len(lines):
            return lines[i + 1]
    return None


def save_cache(challenge, response):
    '''Append challenge/response pair; trim file if > 200 lines.'''
    with open(CACHE_FILE, 'a') as f:
        f.write(f'{challenge}\n{response}\n')
    with open(CACHE_FILE) as f:
        lines = f.readlines()
    if len(lines) > 200:
        with open(CACHE_FILE, 'w') as f:
            f.writelines(lines[2:])


def get_portal_response(challenge):
    '''Fetch OTP response from the Arista license portal. Retries up to 3 times.'''
    for attempt in range(1, 4):
        try:
            session = requests.Session()
            session.verify = False

            # Grab CSRF token from login page
            session.get(PORTAL_LOGIN, timeout=15)
            csrf = session.cookies.get('csrftoken', '')

            # Log in
            session.post(
                PORTAL_LOGIN,
                data={
                    'username':            ONELOGIN_USER,
                    'password':            ONELOGIN_PASS,
                    'submit':              'Log in',
                    'csrfmiddlewaretoken': csrf,
                },
                headers={'Referer': PORTAL_LOGIN},
                timeout=15,
            )
            csrf = session.cookies.get('csrftoken', csrf)

            # Request OTP signature
            # Referer must be PORTAL_LOGIN (Django validates the origin matches the domain)
            r = session.post(
                PORTAL_OTP,
                data={'message': challenge, 'csrfmiddlewaretoken': csrf},
                headers={'Referer': PORTAL_LOGIN},
                timeout=15,
            )

            sig = r.json().get('signature', '')
            if sig:
                return sig
            logging.warning(f'Portal attempt {attempt}: empty signature – retrying')

        except Exception as exc:
            logging.warning(f'Portal attempt {attempt} failed: {exc}')

        if attempt < 3:
            time.sleep(2 * attempt)

    return ''


def get_response(challenge):
    '''Cache-first lookup, then hit the portal (serialized to avoid concurrent auth conflicts).'''
    cached = check_cache(challenge)
    if cached:
        logging.info(f'   [cache] Challenge {challenge[:12]}... -> using cached response')
        return cached

    with _portal_lock:
        # Re-check cache inside lock: another thread may have just fetched this challenge
        cached = check_cache(challenge)
        if cached:
            logging.info(f'   [cache] Challenge {challenge[:12]}... -> using cached response')
            return cached

        logging.info(f'   [portal] Fetching response for challenge: {challenge[:12]}...')
        response = get_portal_response(challenge)
        if response:
            save_cache(challenge, response)
            return response

    logging.error('[ERROR] Could not get OTP response from portal.')
    return ''


# ===========================================================================
# SSH helpers
# ===========================================================================

def _unlock_rootuser(ip):
    '''
    Open a config-user SSH session and run "rootuser unlock" so that a
    subsequent root SSH session can authenticate.
    '''
    logging.info(f'[{ip}] Unlocking rootuser via config shell')
    cmd = f'ssh {SSH_OPTS} {CONFIG_USER}@{ip}'
    conn = pexpect.spawn(cmd, timeout=SSH_TIMEOUT, encoding='utf-8')

    try:
        idx = conn.expect(
            ['continue connecting', r'[Pp]assword:', pexpect.EOF, pexpect.TIMEOUT],
            timeout=SSH_TIMEOUT,
        )
        if idx == 0:                        # host-key prompt
            conn.sendline('yes')
            conn.expect(r'[Pp]assword:', timeout=SSH_TIMEOUT)
            conn.sendline(CONFIG_PASS)
        elif idx == 1:                      # password prompt directly
            conn.sendline(CONFIG_PASS)
        elif idx == 2:
            raise RuntimeError(f'[{ip}] config SSH: connection closed unexpectedly')
        else:
            raise RuntimeError(f'[{ip}] config SSH: timed out waiting for password prompt')

        conn.expect(CONFIG_PROMPT, timeout=SSH_TIMEOUT)
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


def _ssh_root(ip):
    '''
    Unlock rootuser then SSH as root with OTP challenge-response.
    Returns an open pexpect session sitting at the root shell prompt.
    '''
    _unlock_rootuser(ip)

    logging.info(f'[{ip}] Connecting as root')
    cmd = f'ssh {SSH_OPTS} {ROOT_USER}@{ip}'
    conn = pexpect.spawn(cmd, timeout=SSH_TIMEOUT, encoding='utf-8')

    while True:
        idx = conn.expect(
            [ROOT_PROMPT, 'continue connecting', r'[Pp]assword:', CHALLENGE_RE,
             pexpect.EOF, pexpect.TIMEOUT],
            timeout=SSH_TIMEOUT,
        )
        if idx == 0:
            logging.info(f'[{ip}] Root shell ready')
            return conn
        elif idx == 1:                      # host-key prompt
            conn.sendline('yes')
        elif idx == 2:                      # password prompt (unexpected)
            conn.sendline('')
        elif idx == 3:                      # OTP challenge
            challenge = conn.match.group(1)
            otp = get_response(challenge)
            if not otp:
                conn.close(force=True)
                raise RuntimeError(f'[{ip}] Failed to obtain OTP from portal')
            conn.sendline(otp)
        elif idx == 4:
            raise RuntimeError(f'[{ip}] SSH connection closed unexpectedly')
        else:
            conn.close(force=True)
            raise RuntimeError(f'[{ip}] SSH timed out waiting for root prompt')


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

    time.sleep(2)
    pid = _get_pid(conn, ip)

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
    except Exception as e:
        _print(f'[{ip}]  ERROR: {e}')
        logging.error(f'[{ip}] status failed: {e}')


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
                return
            _run(conn, ip, 'killall wifiagent 2>/dev/null || pkill -x wifiagent 2>/dev/null || true')
            time.sleep(1)
            pid = _get_pid(conn, ip)
            if not pid:
                _print(f'[{ip}]  wifiagent stopped')
            else:
                _print(f'[{ip}]  WARNING: wifiagent still running (PID={pid})')
        finally:
            _close(conn)
    except Exception as e:
        _print(f'[{ip}]  ERROR: {e}')
        logging.error(f'[{ip}] stop failed: {e}')


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
                return

            binOk  = _file_exists(conn, ip, AP_BIN_PATH)
            confOk = _file_exists(conn, ip, AP_CONF_PATH)

            if not binOk or not confOk:
                _print(f'[{ip}]  Binary/conf missing – installing wifiagent first')
                _close(conn)
                _do_install(ip, force=False, skipRunningCheck=True)
                return

            pid = _start_binary(conn, ip)
            if pid:
                _print(f'[{ip}]  wifiagent started  PID={pid}')
            else:
                _print(f'[{ip}]  WARNING: wifiagent may not have started')
        finally:
            _close(conn)
    except Exception as e:
        _print(f'[{ip}]  ERROR: {e}')
        logging.error(f'[{ip}] start failed: {e}')


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
                _do_install(ip, force=False, skipRunningCheck=True)
                return

            pid = _start_binary(conn, ip)
            if pid:
                _print(f'[{ip}]  wifiagent restarted  PID={pid}')
            else:
                _print(f'[{ip}]  WARNING: wifiagent may not have restarted')
        finally:
            _close(conn)
    except Exception as e:
        _print(f'[{ip}]  ERROR: {e}')
        logging.error(f'[{ip}] restart failed: {e}')


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
                return
            binOk  = _file_exists(conn, ip, AP_BIN_PATH)
            confOk = _file_exists(conn, ip, AP_CONF_PATH)
            if binOk and confOk:
                _print(f'[{ip}]  Binary already present – starting without reinstall')
                pid = _start_binary(conn, ip)
                if pid:
                    _print(f'[{ip}]  wifiagent started  PID={pid}')
                return
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
        else:
            _print(f'[{ip}]  WARNING: install done but wifiagent may not have started')
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
        _do_install(ip, force=True)
    except Exception as e:
        _print(f'[{ip}]  ERROR: {e}')
        logging.error(f'[{ip}] install failed: {e}')


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
                return

            binOk  = _file_exists(conn, ip, AP_BIN_PATH)
            confOk = _file_exists(conn, ip, AP_CONF_PATH)

            if not binOk or not confOk:
                _print(f'[{ip}]  Binary/conf missing – installing wifiagent')
                _close(conn)
                _do_install(ip, force=False, skipRunningCheck=True)
                return

            pid = _start_binary(conn, ip)
            if pid:
                _print(f'[{ip}]  wifiagent was stopped – now started  PID={pid}')
            else:
                _print(f'[{ip}]  WARNING: wifiagent may not have started')
        finally:
            _close(conn)
    except Exception as e:
        _print(f'[{ip}]  ERROR: {e}')
        logging.error(f'[{ip}] running failed: {e}')


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
      wifiagent-manager.py                         # ensure agent is running on all APs
      wifiagent-manager.py --action status         # check status on all APs
      wifiagent-manager.py --action start          # start agent on all APs
      wifiagent-manager.py --action restart        # restart agent on all APs
      wifiagent-manager.py --action install        # force reinstall on all APs
      wifiagent-manager.py --ap 10.86.58.139       # only manage a specific AP
      wifiagent-manager.py --no-parallel           # run sequentially instead of in parallel
''')


def main():
    parser = argparse.ArgumentParser(
        description='Manage WiFi agents on Access Points via SSH',
        epilog=_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
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
        metavar='AP',
        help='Specific AP IP to manage (default: manage all APs)',
    )
    parser.add_argument(
        '--parallel',
        action=argparse.BooleanOptionalAction,
        default=False,
        help='Run operations in parallel (default: False)',
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

    # Resolve target AP list
    if options.ap:
        if options.ap not in AP_IPS:
            print(f'Warning: {options.ap} is not in the AP_IPS list – proceeding anyway')
        targets = [options.ap]
    else:
        targets = AP_IPS

    actionFn = ACTIONS[options.action]
    print(f'Action: {options.action}  |  APs ({len(targets)}): {", ".join(targets)}'
          f'  |  parallel={options.parallel}')

    if options.parallel and len(targets) > 1:
        with ThreadPoolExecutor(max_workers=len(targets)) as pool:
            futures = {pool.submit(actionFn, ip): ip for ip in targets}
            for future in as_completed(futures):
                exc = future.exception()
                if exc:
                    _print(f'[{futures[future]}]  UNHANDLED ERROR: {exc}')
    else:
        for ip in targets:
            actionFn(ip)

    print('\nDone.')


if __name__ == '__main__':
    main()
