#!/usr/bin/env python3
'''
AP Upgrade Tool

Connects to one or more Arista Access Points via SSH (root, OTP challenge-
response auth), determines the platform model/sub ID, and triggers an
HTTPS-based upgrade to the build matching that platform under the given
base URL. All APs are processed concurrently (ThreadPoolExecutor).

Usage:
  ./ap-upgrade.py --ap 10.86.58.139 --url http://10.86.34.204/wifiagent/content/22.3.0F-12-KASAN/
  ./ap-upgrade.py --ap 10.86.58.139,10.86.58.140 --url <base_url> --debug

Author:  Prince Tadhani
'''

import argparse
import json
import logging
import os
import pexpect
import re
import requests
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

# --- Constants & Configuration ---
WIFI_OTP_URL = 'https://license.aristanetworks.com/sign/wifi-otp/'
WIFI_OTP_KEY = 'd9ca932a4bc8fbaaa5021b00e14dd453d469eaaf'
ROOT_PROMPT = r'#\s*$'

# ---------------------------------------------------------------------------
# AP IP List  –  edit this list to target different APs
# ---------------------------------------------------------------------------
AP_LIST: list[str] = [
# '10.86.205.78',
'10.86.205.163',
]

logging.basicConfig(level=logging.INFO, format='%(asctime)s  %(levelname)-9s%(message)s', datefmt='%H:%M:%S')
log = logging.getLogger('ap-upgrade')

# Silence noisy/irrelevant third-party logging (requests/urllib3 per-request
# connection logs) so --debug output stays focused on this tool's own messages.
for _noisyLogger in ('urllib3', 'requests'):
    logging.getLogger(_noisyLogger).setLevel(logging.WARNING)

# --- Authentication & Connection Helpers ---
def getWifiOtp(challenge):
    '''Sign an arista-ssh-agent Response[...] challenge via the Wifi OTP endpoint.'''
    header = {
        'Authorization': f'Token {WIFI_OTP_KEY}',
        'Content-Type':  'application/json',
    }
    payload = json.dumps({'message': challenge})
    response = requests.post(WIFI_OTP_URL, headers=header, data=payload, allow_redirects=True)
    if response.status_code != 201:
        raise RuntimeError(f'Wifi OTP request failed ({response.status_code}) for challenge {challenge}')
    return response.json()['signature']

def _sshEnv():
    '''Env needed so pexpect can see the Arista SSH Forwarding Agent.'''
    env = {'TERM': 'dumb', 'HOME': os.environ.get('HOME')}
    sshAgentSocket = os.environ.get('SSH_AUTH_SOCK')
    if sshAgentSocket:
        env['SSH_AUTH_SOCK'] = sshAgentSocket
    return env

def _authenticate(child, password, host, promptPattern, timeout=60):
    '''Shared root-shell login challenge/response loop.'''
    patterns = [promptPattern, 'continue connecting', '[Pp]assword:', r'Response\[([^\]]{54})',
                'Enter .*code', pexpect.EOF, pexpect.TIMEOUT]
    while True:
        result = child.expect(patterns, timeout=timeout)
        if result == 0:
            return
        if result == 1:                     # host-key prompt
            child.sendline('yes')
        elif result == 2:                   # password prompt
            child.sendline(password)
        elif result == 3:
            challenge = child.match.group(1)
            child.sendline(getWifiOtp(challenge))
        elif result == 4:
            raise RuntimeError(f'{host}: MFA/OTP prompt received but not supported')
        else:
            raise RuntimeError(f'Failed to connect/login to {host}')

UPGRADE_COMPLETE_MARKER = 'Upgrade completed. Going for Reboot'

# --- Core Upgrade Logic ---
def _pollUpgradeCompletion(child, host, timeout=180, interval=10):
    '''Poll `show log upgrade` until the completion marker shows up, or the
    AP drops the connection (which happens once it actually reboots).
    The initial upgrade command returns to the shell prompt immediately
    since the upgrade itself runs in the background on the AP.'''
    elapsed = 0
    while elapsed < timeout:
        log.debug("[%s] [CLI] show log upgrade", host)
        child.sendline('cli -c "show log upgrade"')
        idx = child.expect([UPGRADE_COMPLETE_MARKER, ROOT_PROMPT, pexpect.EOF, pexpect.TIMEOUT], timeout=interval)
        output = child.before or ''
        if idx == 0 or UPGRADE_COMPLETE_MARKER in output:
            return True
        if idx == 2:
            log.info("[%s] Connection closed by AP (rebooting) while polling upgrade log", host)
            return True
        elapsed += interval
    return False


def verify_version_file(base_url, build_folder, sub_id):
    '''Fetches the version file to double verify the platform sub ID.'''
    version_url = f"{base_url.rstrip('/')}/{build_folder}/version"
    try:
        response = requests.get(version_url, timeout=10)
        if response.status_code == 200:
            content = response.text
            if str(sub_id) in content:
                log.info("Successfully verified Platform sub ID %s in %s", sub_id, version_url)
                return True
            else:
                log.warning("Platform sub ID %s not explicitly found in %s, continuing anyway based on fallback.", sub_id, version_url)
                return True
        else:
            log.warning("Failed to fetch %s (HTTP %s). Skipping double-check.", version_url, response.status_code)
            return True
    except Exception as e:
        log.warning("Exception while verifying version file: %s", e)
        return False

def upgrade_ap(host, base_url, password='admin'):
    '''Main logic for a single AP: login, parse info, verify, upgrade.'''
    child = None
    try:
        # 1. Connect via SSH
        log.info(f"[{host}] [ROOT] ssh root@{host}")
        child = pexpect.spawn('ssh', ['-o', 'StrictHostKeyChecking=no', f'root@{host}'],
                              env=_sshEnv(), encoding='utf-8')
        _authenticate(child, password, host, ROOT_PROMPT)

        # 2. Get Device Info
        log.info(f"[{host}] [CLI] show device info")
        child.sendline('cli -c "show device info"')
        child.expect(ROOT_PROMPT)
        output = child.before

        # 3. Parse Device Model & Platform sub ID
        model_match = re.search(r'Device model:\s*(.+)', output)
        sub_id_match = re.search(r'Platform sub ID:\s*(\d+)', output)

        if not model_match or not sub_id_match:
            raise RuntimeError(f"Could not parse Device model or Platform sub ID from output:\n{output}")

        raw_model = model_match.group(1).strip()
        sub_id = sub_id_match.group(1).strip()

        log.info("[%s] Found Model: %s, Platform sub ID: %s", host, raw_model, sub_id)

        # 4. Map Model + Sub ID to folder name
        # Extract base model (e.g., "C-460D" -> "C-460", "C-430E" -> "C-430")
        base_model_match = re.search(r'([A-Za-z]+-?\d+)', raw_model)
        if not base_model_match:
             raise RuntimeError(f"Could not extract base model string from {raw_model}")
             
        # Convert "C-460" to "c460"
        clean_model = base_model_match.group(1).lower().replace('-', '')
        
        if sub_id == '2':
            build_folder = f"{clean_model}_2"
        else:
            build_folder = f"{clean_model}"

        # 5. Verify against version file over HTTP
        verify_version_file(base_url, build_folder, sub_id)

        # 6. Trigger the Upgrade
        target_url = f"{base_url.rstrip('/')}/{build_folder}/"
        log.info("[%s] Starting upgrade using URL -> %s", host, target_url)

        upgrade_cmd = f'printf "y\\n" | cli -c "upgrade method https url {target_url}"'
        log.info(f"[{host}] [CLI] {upgrade_cmd}")
        child.sendline(upgrade_cmd)

        # 7. Confirm the upgrade actually started (it runs in the background
        # on the AP, so the shell prompt returns right away — the completion
        # marker only shows up later in `show log upgrade`).
        idx = child.expect(['Upgrade has started', pexpect.EOF, pexpect.TIMEOUT], timeout=60)
        if idx != 0:
            output = child.before.strip() if child.before else ""
            raise RuntimeError(f"Upgrade did not start. Last output:\n{output}")
        child.expect(ROOT_PROMPT, timeout=30)

        # 8. Poll for the completion marker in the background upgrade log.
        log.info("[%s] Upgrade started; polling 'show log upgrade' for completion...", host)
        if _pollUpgradeCompletion(child, host):
            log.info("[%s] Upgrade initiated properly (Upgrade completed. Going for Reboot).", host)
            return "Upgrade triggered successfully"
        else:
            raise RuntimeError("Timed out waiting for upgrade completion marker in 'show log upgrade'.")

    finally:
        if child and child.isalive():
            child.sendline('exit')
            child.close()

def runConcurrently(func, apList, base_url, actionName):
    '''Run func concurrently across apList.'''
    errors = []
    results = {}
    with ThreadPoolExecutor(max_workers=len(apList)) as executor:
        futureToHost = {executor.submit(func, host, base_url): host for host in apList}
        for future in as_completed(futureToHost):
            host = futureToHost[future]
            try:
                results[host] = future.result()
                log.info("%s: %s succeeded", host, actionName)
            except Exception as e:
                log.error("%s: %s failed - %s", host, actionName, e)
                errors.append(host)
                results[host] = f'FAILED: {e}'

    if errors:
        log.error("%s failed on: %s", actionName, errors)
    return results, errors

def resolveApList(args):
    '''Prefer --ap if given (accepts any host, no AP_LIST membership check);
    otherwise fall back to AP_LIST defined in this file.'''
    if args.ap:
        apList = [h.strip() for h in args.ap.split(',') if h.strip()]
        for host in apList:
            if host not in AP_LIST:
                log.warning(f'{host} not present in AP_LIST — proceeding anyway')
        log.info(f'Targeting {len(apList)} AP(s) via --ap: {apList}')
        return apList
    if AP_LIST:
        log.info(f'Targeting {len(AP_LIST)} AP(s) from AP_LIST: {AP_LIST}')
        return list(AP_LIST)
    log.error('No APs to target — pass --ap HOST[,HOST...] or populate AP_LIST in this file.')
    sys.exit(1)

# --- CLI and Arguments ---
def parseArgs():
    parser = argparse.ArgumentParser(
        description='Connect to AP(s), determine platform Sub ID, and trigger an upgrade via HTTPS.'
    )
    parser.add_argument('--ap', metavar='HOST[,HOST...]',
                        help='Comma-separated list of AP host/IPs to target (defaults to AP_LIST in this file)')
    parser.add_argument('--url', required=True,
                        help='Base URL containing the build directories (e.g., http://10.86.34.204/wifiagent/content/22.3.0F-12-KASAN/)')
    parser.add_argument('--debug', action='store_true', help='Enable debug logging')
    return parser.parse_args()

def main():
    args = parseArgs()
    if args.debug:
        log.setLevel(logging.DEBUG)

    ap_list = resolveApList(args)

    log.info("Starting upgrade process for %d AP(s)...", len(ap_list))
    results, errors = runConcurrently(upgrade_ap, ap_list, args.url, "AP Upgrade")

    W = 20
    numCols = 2
    ruleWidth = W * numCols + (numCols - 1) * 2
    print("\n" + "=" * ruleWidth)
    print(f"{'AP IP':<{W}}  {'Result':<{W}}")
    print("=" * ruleWidth)
    for ip in sorted(results):
        status = 'ok' if ip not in errors else results[ip]
        print(f"{ip:<{W}}  {status:<{W}}")
    print("=" * ruleWidth + "\n")

if __name__ == '__main__':
    main()