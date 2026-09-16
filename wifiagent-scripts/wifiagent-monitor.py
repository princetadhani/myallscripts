#!/usr/bin/env python3
'''
WiFi Agent Monitoring Script (Watchdog) - Standalone Version

Continuously monitors the WiFi Agent status on a list of Access Points and ensures
that the WiFi Agent is always installed and running.

Monitoring Logic:
  - Checks each AP every 5 minutes
  - If agent is running: Do nothing
  - If agent is stopped: Start it
  - If agent is not installed: Install and start it
  - Continues indefinitely until manually stopped (Ctrl+C)

This is a standalone script with all necessary functionality embedded.

Usage:
  ./wifiagent_monitor.py                    # Start monitoring with default settings
  ./wifiagent_monitor.py --interval 300     # Custom interval (seconds)
  ./wifiagent_monitor.py --max-retries 2    # Custom retry count per AP

Author:  Prince Tadhani
Created: 2026-08-15
'''

'''
# Restart with unbuffered output
nohup python3 -u ./wifiagent_monitor.py > monitor.log 2>&1 &

# Now watch logs in real-time
tail -f monitor.log
'''

# Standard Library Modules
import argparse
import json
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

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

# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# Monitoring Configuration
# ---------------------------------------------------------------------------
DEFAULT_INTERVAL_SECONDS = 300  # 5 minutes
DEFAULT_MAX_RETRIES = 2         # Max retries per AP before moving to next


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


def _do_install(ip, force=True, skipRunningCheck=False):
    '''
    Install wifiagent on the AP.

    Steps:
      1. Optionally check running state and bail early if already OK.
      2. SSH: kill running instance, mkdir, download both files from source server.
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

    # Steps 2-3 in a single SSH session
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


# ===========================================================================
# Monitoring Logic
# ===========================================================================

def check_and_fix_agent(ip, max_retries=DEFAULT_MAX_RETRIES):
    '''
    Check WiFi Agent status on a single AP and take corrective action if needed.
    
    Logic:
      1. Check if agent is running → do nothing
      2. Check if agent is installed but stopped → start it
      3. If agent is not installed → install and start it
    
    Returns:
      True if agent is running after the check, False otherwise
    '''
    attempt = 0
    while attempt < max_retries:
        attempt += 1
        try:
            logging.info(f'[{ip}] Attempt {attempt}/{max_retries}: Checking wifiagent status')
            conn = _ssh_root(ip)
            
            try:
                # Step 1: Check if running
                pid = _get_pid(conn, ip)
                if pid:
                    _print(f'[{ip}]  ✓ wifiagent running (PID={pid}) – OK')
                    return True
                
                # Step 2: Check if installed
                binOk = _file_exists(conn, ip, AP_BIN_PATH)
                confOk = _file_exists(conn, ip, AP_CONF_PATH)
                
                if binOk and confOk:
                    # Agent is installed but stopped → start it
                    _print(f'[{ip}]  ⚠ wifiagent stopped – attempting to start')
                    pid = _start_binary(conn, ip)
                    if pid:
                        _print(f'[{ip}]  ✓ wifiagent started successfully (PID={pid})')
                        return True
                    else:
                        _print(f'[{ip}]  ✗ Failed to start wifiagent (attempt {attempt}/{max_retries})')
                        if attempt < max_retries:
                            _print(f'[{ip}]  → Retrying in next cycle')
                        return False
                else:
                    # Agent is not installed → install and start
                    _print(f'[{ip}]  ⚠ wifiagent not installed – attempting installation')
                    _close(conn)  # Close current connection before install
                    _do_install(ip, force=False, skipRunningCheck=True)
                    _print(f'[{ip}]  ✓ Installation completed')
                    return True
                    
            finally:
                _close(conn)
                
        except Exception as e:
            _print(f'[{ip}]  ✗ ERROR (attempt {attempt}/{max_retries}): {e}')
            logging.error(f'[{ip}] Attempt {attempt} failed: {e}')
            if attempt < max_retries:
                time.sleep(2)  # Brief pause before retry
            
    # All retries exhausted
    _print(f'[{ip}]  ✗ All {max_retries} attempts failed – will retry in next cycle')
    return False


def monitor_cycle(ap_list, max_retries, parallel=True):
    '''
    Run one complete monitoring cycle across all APs in the list.
    Each AP is checked independently; failures don't stop the cycle.

    Inputs:
      ap_list      - list of AP IPs to check this cycle
      max_retries  - max retry attempts per AP (passed to check_and_fix_agent)
      parallel     - if True (default) and there is more than one AP, check
                     all APs concurrently via a thread pool; otherwise check
                     them one at a time.

    Per-AP state is tracked in a dict keyed by IP so that every AP submitted
    is guaranteed to have a recorded result (True/False) once the cycle
    finishes, regardless of execution order or thread completion order –
    none can be silently skipped.
    '''
    cycle_start = datetime.now()
    _print(f'\n{"="*80}')
    _print(f'Monitoring Cycle Started: {cycle_start.strftime("%Y-%m-%d %H:%M:%S")}')
    _print(f'APs to monitor: {len(ap_list)}  |  parallel={parallel}')
    _print(f'{"="*80}\n')

    apResults = {}   # ip -> True (ok) / False (failed) – one entry per AP, no exceptions

    if parallel and len(ap_list) > 1:
        with ThreadPoolExecutor(max_workers=len(ap_list)) as pool:
            futures = {
                pool.submit(check_and_fix_agent, ip, max_retries): ip
                for ip in ap_list
            }
            for future in as_completed(futures):
                ip  = futures[future]
                exc = future.exception()
                if exc:
                    _print(f'[{ip}]  ✗ UNHANDLED ERROR: {exc}')
                    logging.error(f'[{ip}] monitor_cycle unhandled exception: {exc}')
                    apResults[ip] = False
                else:
                    apResults[ip] = bool(future.result())
    else:
        for ip in ap_list:
            _print(f'\n--- Checking {ip} ---')
            try:
                apResults[ip] = bool(check_and_fix_agent(ip, max_retries=max_retries))
            except Exception as exc:
                _print(f'[{ip}]  ✗ UNHANDLED ERROR: {exc}')
                logging.error(f'[{ip}] monitor_cycle unhandled exception: {exc}')
                apResults[ip] = False

    # Guard against any AP somehow missing a recorded result (should not
    # happen given the above, but keeps the summary accurate either way).
    missing = [ip for ip in ap_list if ip not in apResults]
    for ip in missing:
        _print(f'[{ip}]  ✗ No result recorded – treating as failed')
        apResults[ip] = False

    okCount     = sum(1 for ip in ap_list if apResults.get(ip))
    failedIps   = [ip for ip in ap_list if not apResults.get(ip)]

    cycle_end = datetime.now()
    duration = (cycle_end - cycle_start).total_seconds()

    _print(f'\n{"="*80}')
    _print(f'Cycle Completed: {cycle_end.strftime("%Y-%m-%d %H:%M:%S")}')
    _print(f'Duration: {duration:.1f}s  |  Success: {okCount}/{len(ap_list)}  |  Failed: {len(failedIps)}/{len(ap_list)}')
    if failedIps:
        _print(f'Failed APs: {", ".join(failedIps)}')
    _print(f'{"="*80}\n')


def main():
    '''Main entry point for the monitoring script.'''
    parser = argparse.ArgumentParser(
        description='WiFi Agent Monitoring Script - Continuously monitors and maintains WiFi Agent on APs',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  ./wifiagent_monitor.py                      # Start monitoring with defaults (5min interval)
  ./wifiagent_monitor.py --interval 180       # Check every 3 minutes
  ./wifiagent_monitor.py --max-retries 3      # 3 retries per AP per cycle
  ./wifiagent_monitor.py --once               # Run only one cycle (for testing)
  ./wifiagent_monitor.py --no-parallel        # Check APs sequentially instead of in parallel

Press Ctrl+C to stop monitoring.
        '''
    )

    parser.add_argument(
        '--interval',
        type=int,
        default=DEFAULT_INTERVAL_SECONDS,
        metavar='SECONDS',
        help=f'Monitoring interval in seconds (default: {DEFAULT_INTERVAL_SECONDS}s = 5min)',
    )

    parser.add_argument(
        '--max-retries',
        type=int,
        default=DEFAULT_MAX_RETRIES,
        metavar='N',
        help=f'Max retry attempts per AP before moving to next (default: {DEFAULT_MAX_RETRIES})',
    )

    parser.add_argument(
        '--once',
        action='store_true',
        help='Run monitoring cycle only once (for testing)',
    )

    parser.add_argument(
        '--parallel',
        action=argparse.BooleanOptionalAction,
        default=True,
        help='Check all APs concurrently each cycle (default: True; use --no-parallel to check sequentially)',
    )

    parser.add_argument(
        '--logLevel',
        default='WARNING',
        metavar='LEVEL',
        help='Logging level: DEBUG, INFO, WARNING, ERROR (default: WARNING)',
    )

    options = parser.parse_args()

    # Configure logging
    logging.basicConfig(
        level=getattr(logging, options.logLevel.upper(), logging.WARNING),
        format='%(asctime)s %(levelname)s %(message)s',
        stream=sys.stdout,
    )

    # Display startup banner
    print('\n' + '='*80)
    print('WiFi Agent Monitoring Script')
    print('='*80)
    print(f'Monitoring {len(AP_IPS)} Access Points:')
    for ip in AP_IPS:
        print(f'  • {ip}')
    print(f'\nMonitoring interval: {options.interval}s ({options.interval/60:.1f} minutes)')
    print(f'Max retries per AP: {options.max_retries}')
    print(f'Mode: {"Single cycle" if options.once else "Continuous monitoring"}')
    print(f'Execution: {"Parallel" if options.parallel else "Sequential"}')
    print('\nPress Ctrl+C to stop monitoring')
    print('='*80 + '\n')

    if not AP_IPS:
        print('ERROR: AP_IPS list is empty. Please configure APs in wifiagent.py')
        return 1

    # Start monitoring
    try:
        cycle_count = 0
        while True:
            cycle_count += 1

            # Run monitoring cycle
            monitor_cycle(AP_IPS, max_retries=options.max_retries, parallel=options.parallel)

            # Exit if running only once
            if options.once:
                print('Single cycle completed. Exiting.')
                break

            # Wait for next cycle
            _print(f'Next cycle ({cycle_count + 1}) in {options.interval}s... (Ctrl+C to stop)\n')
            time.sleep(options.interval)

    except KeyboardInterrupt:
        print('\n\nMonitoring stopped by user (Ctrl+C)')
        print(f'Total cycles completed: {cycle_count}')
        print('Exiting gracefully...\n')
        return 0
    except Exception as e:
        print(f'\n\nFATAL ERROR: {e}')
        logging.exception('Unhandled exception in main loop')
        return 1

    return 0


if __name__ == '__main__':
    sys.exit(main())
