#!/usr/bin/env python3
'''
* Standalone, non-interactive kickmac distribution tool.

* SCPs the local kickmac binary (in this same folder) to /root/kickmac on
each AP in AP_LIST below, using the same SSH/OTP login logic as
qwrap-manager.py / wifiagent-manager.py (arista-ssh-agent Response[...]
challenge signed via the Wifi OTP endpoint).

* All APs are processed concurrently (ThreadPoolExecutor).
'''

import argparse
import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import pexpect
import requests

logging.basicConfig(level=logging.INFO, format='%(asctime)s  %(levelname)-9s%(message)s', datefmt='%H:%M:%S')
log = logging.getLogger('kickmac-deploy')

# ---------------------------------------------------------------------------
# AP IP List  –  edit this list to target different APs
# ---------------------------------------------------------------------------
AP_LIST: list[str] = [
'10.86.205.78',
'10.86.205.42'
]

# ---------------------------------------------------------------------------
# AP SSH credentials
# ---------------------------------------------------------------------------
CLI_USERNAME = 'root'
CLI_PASSWORD = 'arastra'

# ---------------------------------------------------------------------------
# Local kickmac binary + remote destination
# ---------------------------------------------------------------------------
LOCAL_KICKMAC_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'kickmac')
REMOTE_KICKMAC_PATH = '/root/kickmac'

# ---------------------------------------------------------------------------
# Wifi OTP signing endpoint used by the arista-ssh-agent Response[...] challenge/
# response prompt (same as qwrap-manager.py / wifiagent-manager.py).
# ---------------------------------------------------------------------------
WIFI_OTP_URL = 'https://license.aristanetworks.com/sign/wifi-otp/'
WIFI_OTP_KEY = 'd9ca932a4bc8fbaaa5021b00e14dd453d469eaaf'


def getWifiOtp(challenge):
    '''Sign an arista-ssh-agent Response[...] challenge via the Wifi OTP
    endpoint (same as qwrap-manager.py / wifiagent-manager.py).'''
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
    '''Env needed so pexpect can see the Arista SSH Forwarding Agent (for the
    'arista-ssh-agent' Response[...] challenge/response prompt), same as
    qwrap-manager.py.'''
    env = {'TERM': 'dumb', 'HOME': os.environ.get('HOME')}
    sshAgentSocket = os.environ.get('SSH_AUTH_SOCK')
    if sshAgentSocket:
        env['SSH_AUTH_SOCK'] = sshAgentSocket
    return env


def _authenticate(child, password, host, promptPattern=None, timeout=60):
    '''Shared login challenge/response loop (host-key/password/OTP), same as
    qwrap-manager.py / wifiagent-manager.py. Used for one-shot scp transfers
    (promptPattern is None; we just wait for EOF once authenticated).'''
    patterns = ['continue connecting', '[Pp]assword:', r'Response\[([^\]]{54})', 'Enter .*code',
                pexpect.EOF, pexpect.TIMEOUT]
    if promptPattern:
        patterns = [promptPattern] + patterns
        offset = 1
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
            challenge = child.match.group(1)
            child.sendline(getWifiOtp(challenge))
        elif idx == 3:
            raise RuntimeError(f'{host}: MFA/OTP prompt received but not supported by this script')
        elif idx == 4:
            if promptPattern:
                raise RuntimeError(f'Failed to connect/login to {host}')
            return
        else:
            raise RuntimeError(f'Failed to connect/login to {host}')


def scpToAp(host, username, password, localPath, remoteDst, timeout=60):
    '''SCP a local file to the AP, handling the same login challenge/response
    as qwrap-manager.py's scpToAp().'''
    localPath = os.path.expanduser(localPath)
    cmd = (f'scp -O -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null '
           f'"{localPath}" {username}@{host}:"{remoteDst}"')
    log.info(f'[{host}] [SCP] {localPath} -> {remoteDst}')
    child = pexpect.spawn(cmd, env=_sshEnv(), timeout=timeout, encoding='utf-8')
    try:
        _authenticate(child, password, host, promptPattern=None, timeout=timeout)
        transcript = child.before or ''
        child.close()
        log.debug(f'[{host}] scp transcript: {transcript.strip()!r} (exit={child.exitstatus})')
        if child.exitstatus not in (0, None):
            raise RuntimeError(f'{host}: scp {localPath} -> {remoteDst} failed '
                                f'(exit={child.exitstatus}): {transcript.strip()!r}')
    finally:
        if child.isalive():
            child.close(force=True)


def deployToAp(host):
    scpToAp(host, CLI_USERNAME, CLI_PASSWORD, LOCAL_KICKMAC_PATH, REMOTE_KICKMAC_PATH)
    return host


def runConcurrently(func, apList, actionName):
    '''Run func(host) concurrently across apList (one worker per AP), same
    pattern as qwrap-manager.py's runConcurrently().'''
    errors = []
    with ThreadPoolExecutor(max_workers=len(apList)) as executor:
        futureToHost = {executor.submit(func, host): host for host in apList}
        results = {}
        for future in as_completed(futureToHost):
            host = futureToHost[future]
            try:
                results[host] = future.result()
                log.info(f'{host}: {actionName} succeeded')
            except Exception as e:
                log.error(f'{host}: {actionName} FAILED: {e}')
                errors.append(host)
    if errors:
        log.error(f'{actionName} failed on: {errors}')
        sys.exit(1)
    return results


_EXAMPLES = '''\
examples:
  python3 kickmac-deploy.py
  python3 kickmac-deploy.py --ap 10.86.205.157
  python3 kickmac-deploy.py --ap 10.86.205.157,10.86.205.158
  python3 kickmac-deploy.py --debug
'''


class _HelpFormatter(argparse.RawDescriptionHelpFormatter):
    def __init__(self, prog):
        super().__init__(prog, max_help_position=40, width=200)


def parseArgs():
    parser = argparse.ArgumentParser(
        prog='kickmac-deploy.py',
        usage=argparse.SUPPRESS,
        description=f'SCP {LOCAL_KICKMAC_PATH} to {REMOTE_KICKMAC_PATH} on all APs in AP_LIST',
        epilog=_EXAMPLES,
        formatter_class=_HelpFormatter,
    )
    parser.add_argument('--ap', metavar='HOST[,HOST...]', default=None,
                         help='Comma-separated list of AP host/IPs to target instead of all APs in AP_LIST')
    parser.add_argument('--debug', action='store_true', help='Enable debug logging (raw scp output)')
    return parser.parse_args()


def resolveApList(args):
    if not args.ap:
        return list(AP_LIST)
    requestedHosts = [h.strip() for h in args.ap.split(',') if h.strip()]
    unknown = sorted(set(requestedHosts) - set(AP_LIST))
    if unknown:
        log.error(f'Host(s) not found in AP_LIST: {unknown}')
        sys.exit(1)
    log.info(f'Targeting {len(requestedHosts)} AP(s): {requestedHosts}')
    return requestedHosts


def main():
    args = parseArgs()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    if not os.path.isfile(LOCAL_KICKMAC_PATH):
        log.error(f'kickmac binary not found at {LOCAL_KICKMAC_PATH}')
        sys.exit(1)

    apList = resolveApList(args)
    runConcurrently(deployToAp, apList, 'deploy')
    log.info(f'kickmac deployed successfully to all {len(apList)} AP(s)')


if __name__ == '__main__':
    main()
