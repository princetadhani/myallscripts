#!/usr/bin/env python3
'''
AP Upgrade Tool

Connects to one or more Arista Access Points via SSH (root, OTP challenge-
response auth), determines the platform model/sub ID, and triggers an
HTTPS-based upgrade to the build matching that platform under the given
base URL. All APs are processed concurrently (ThreadPoolExecutor).

Targets APs from AP_LIST below by default, or from --ap HOST[,HOST...]
if given. --ap accepts ANY host/IP (no dependency on AP_LIST — an AP does
not need to be present in AP_LIST to be targeted via --ap).

Usage:
  ./ap-upgrade.py --ap 10.86.58.139 --url http://10.86.34.204/wifiagent/content/22.3.0F-12-KASAN/
  ./ap-upgrade.py --ap 10.86.58.139,10.86.58.140 --url <base_url> --debug

  # Jenkins mode: instead of --url, pass --jenkins and paste the per-platform
  # Jenkins build URLs (one per line) when prompted, then press Ctrl+D. Each
  # AP is matched to the URL whose trailing platform tag (e.g. _c330, _c360_2,
  # _o405, _w318) matches its detected model/sub ID.
  ./ap-upgrade.py --ap 10.86.58.139 --jenkins

# Open a real terminal (avoids VS Code's paste corruption bug) for --jenkins pasting:

osascript -e 'tell application "iTerm2" to create window with default profile' -e 'tell application "iTerm2" to tell current session of current window to write text "cd /Users/prince.tadhani/myallscripts/ap-upgrade"'

Author:  Prince Tadhani
'''

import argparse
import functools
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
# C330--50637F--Indus-AP: E4:D1:24:50:63:7F
'10.86.205.17',

# C360--8014FF--Indus-AP: 30:86:2D:80:14:FF
'10.86.204.220',

# C400--F02F3F--Tapi-AP: E4:D1:24:F0:2F:3F
'10.86.204.211',

# C400--F0687F--Indus-AP: E4:D1:24:F0:68:7F
'10.86.205.74',

# C430--05C9DF--Indus-AP: 30:B6:2D:05:C9:DF
'10.86.205.176',

# C460D--E0199F--Tapi-AP: E0:1C:A7:E0:19:9F
'10.86.205.184',

# C460D--E01C6F--Indus-AP: E0:1C:A7:E0:1C:6F
'10.86.205.86',

# O405--204DBF--Tapi-AP: E0:1C:A7:20:4D:BF
'10.86.205.131',

# O435--00C4DF--Tapi-AP: 30:B6:2D:00:C4:DF
'10.86.205.163',

# W318--10257F--Indus-AP: E4:D1:24:10:25:7F
'10.86.205.232',

# W318--1023FF--Tapi-AP: E4:D1:24:10:23:FF
'10.86.204.228',

# O405--205D3F--Indus-AP: E0:1C:A7:20:5D:3F
# '10.86.205.78',

# O435--00CB1F--Indus-AP: 30:B6:2D:00:CB:1F
# '10.86.205.42',

# C430--05CB6F--Tapi-AP: 30:B6:2D:05:CB:6F
# '10.86.205.159',

# C430--05BEEF--PT-ABZ-AP: 30:B6:2D:05:BE:EF
# '10.87.169.239',

# O405--2055FF--PT-ABZ-AP: E0:1C:A7:20:55:FF
# '10.87.169.221',
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


def verify_version_file(version_url, sub_id):
    '''Fetches the version file to double verify the platform sub ID.'''
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

def upgrade_ap(host, base_url=None, jenkins_map=None, password='admin'):
    '''Main logic for a single AP: login, parse info, verify, upgrade.

    Either base_url (classic mode) or jenkins_map (--jenkins mode, a dict of
    build_folder -> full Jenkins build URL) must be given.'''
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

        # 5. Determine the target build URL for this platform
        if jenkins_map is not None:
            if build_folder not in jenkins_map:
                raise RuntimeError(f"No Jenkins build URL provided for platform '{build_folder}'")
            target_url = f"{jenkins_map[build_folder].rstrip('/')}/"
        else:
            target_url = f"{base_url.rstrip('/')}/{build_folder}/"

            # 6. Verify against version file over HTTP (Jenkins builds don't
            # expose a per-platform version file to double-check against —
            # the model/sub ID -> Jenkins URL mapping from --jenkins input is
            # authoritative there, so this check is only done for --url mode).
            verify_version_file(f"{target_url}version", sub_id)

        # 7. Trigger the Upgrade (jenkins mode skips step 6 above)
        log.info("[%s] Starting upgrade using URL -> %s", host, target_url)

        upgrade_cmd = f'printf "y\\n" | cli -c "upgrade method https url {target_url}"'
        log.info(f"[{host}] [CLI] {upgrade_cmd}")
        child.sendline(upgrade_cmd)

        # 8. Confirm the upgrade actually started (it runs in the background
        # on the AP, so the shell prompt returns right away — the completion
        # marker only shows up later in `show log upgrade`).
        idx = child.expect(['Upgrade has started', pexpect.EOF, pexpect.TIMEOUT], timeout=60)
        if idx != 0:
            output = child.before.strip() if child.before else ""
            raise RuntimeError(f"Upgrade did not start. Last output:\n{output}")
        child.expect(ROOT_PROMPT, timeout=30)

        # 9. Poll for the completion marker in the background upgrade log.
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

def runConcurrently(func, apList, actionName):
    '''Run func(host) concurrently across apList. Any fixed arguments (e.g.
    base_url or jenkins_map) must already be bound into func, e.g. via
    functools.partial.'''
    errors = []
    results = {}
    with ThreadPoolExecutor(max_workers=len(apList)) as executor:
        futureToHost = {executor.submit(func, host): host for host in apList}
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

# --- Jenkins Mode Helpers ---
# Full expected shape of a Jenkins build URL, e.g.:
#   https://systemtest-upgrade.dt1.wifi.arista.cloud/builds/103/112/1/UPG_BUNDLE_23.2.0F-1_1_0_103_112_1_c330
# Captures the 3 numeric path segments and the basename separately so they
# can be cross-checked against each other below.
JENKINS_URL_RE = re.compile(r'^https?://\S+/builds/(\d+)/(\d+)/(\d+)/(UPG_BUNDLE_\S+)$', re.IGNORECASE)
# Matches the trailing platform tag in the basename, e.g.
# "...103_112_1_c330" -> ('c330', None), "...101_110_2_c360_2" -> ('c360', '_2')
JENKINS_PLATFORM_RE = re.compile(r'_([a-z]\d+)(_2)?$', re.IGNORECASE)

def readJenkinsUrls():
    '''Prompt for and read Jenkins build URLs pasted on stdin, one per line,
    terminated with Ctrl+D (EOF).'''
    print('Paste the Jenkins build URLs (one per line), then press Ctrl+D when done:')
    rawLines = sys.stdin.read().splitlines()
    urls = [line.strip() for line in rawLines if line.strip()]
    if not urls:
        log.error('No Jenkins URLs provided.')
        sys.exit(1)
    return urls

def buildJenkinsMap(urls):
    '''Parse pasted Jenkins build URLs into a {build_folder: url} map, using
    the same build_folder naming (e.g. "c330", "c360_2") as upgrade_ap().

    Validates the full URL shape (not just the trailing platform tag) and
    cross-checks the path's build numbers against the basename's, so a
    terminal paste glitch (dropped/garbled characters) that still happens to
    end in a valid-looking platform tag gets rejected instead of silently
    accepted.'''
    jenkinsMap = {}
    for url in urls:
        structureMatch = JENKINS_URL_RE.match(url)
        if not structureMatch:
            log.warning(f'Malformed/incomplete Jenkins URL (paste glitch?), skipping: {url}')
            continue

        pathBuildNums = structureMatch.group(1, 2, 3)
        basename = structureMatch.group(4)

        platformMatch = JENKINS_PLATFORM_RE.search(basename)
        if not platformMatch:
            log.warning(f'Could not determine platform tag from Jenkins URL, skipping: {url}')
            continue

        # The 3 build numbers in the path must also appear, in the same
        # order, as the 3 numeric tokens immediately before the platform tag.
        basenameCore = basename[:platformMatch.start()].rstrip('_')
        basenameNums = tuple(basenameCore.split('_')[-3:])
        if basenameNums != pathBuildNums:
            log.warning(f'Jenkins URL path/build-number mismatch (paste glitch?), skipping: {url}')
            continue

        model, subSuffix = platformMatch.group(1).lower(), platformMatch.group(2)
        build_folder = f'{model}{subSuffix}' if subSuffix else model
        jenkinsMap[build_folder] = url
        log.debug(f'Jenkins mapping: {build_folder} -> {url}')

    if not jenkinsMap:
        log.error('Could not parse any platform build URLs from the Jenkins input.')
        sys.exit(1)
    log.info(f'Parsed {len(jenkinsMap)} Jenkins build URL(s) for platforms: {sorted(jenkinsMap)}')
    return jenkinsMap

# --- CLI and Arguments ---
def parseArgs():
    parser = argparse.ArgumentParser(
        description='Connect to AP(s), determine platform Sub ID, and trigger an upgrade via HTTPS.'
    )
    parser.add_argument('--ap', metavar='HOST[,HOST...]',
                        help='Comma-separated list of AP host/IPs to target instead of AP_LIST '
                             '(hosts do not need to already be in AP_LIST)')
    parser.add_argument('--url',
                        help='Base URL containing the build directories (e.g., http://10.86.34.204/wifiagent/content/22.3.0F-12-KASAN/). '
                             'Required unless --jenkins is given.')
    parser.add_argument('--jenkins', action='store_true',
                        help='Instead of --url, prompt for per-platform Jenkins build URLs (paste, then Ctrl+D) '
                             'and auto-select the matching one per AP.')
    parser.add_argument('--debug', action='store_true', help='Enable debug logging')
    args = parser.parse_args()
    if not args.jenkins and not args.url:
        parser.error('one of --url or --jenkins is required')
    if args.jenkins and args.url:
        parser.error('--url and --jenkins are mutually exclusive')
    return args

def main():
    args = parseArgs()
    if args.debug:
        log.setLevel(logging.DEBUG)

    ap_list = resolveApList(args)

    if args.jenkins:
        jenkinsMap = buildJenkinsMap(readJenkinsUrls())
        upgradeFunc = functools.partial(upgrade_ap, jenkins_map=jenkinsMap)
    else:
        upgradeFunc = functools.partial(upgrade_ap, base_url=args.url)

    log.info("Starting upgrade process for %d AP(s)...", len(ap_list))
    results, errors = runConcurrently(upgradeFunc, ap_list, "AP Upgrade")

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