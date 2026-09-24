#!/usr/bin/env python3
'''
* Standalone AFC 6GHz-on-outdoor-model configuration tool.

* Per AP (same SSH/OTP login logic as kickmac-deploy.py / qwrap-manager.py —
arista-ssh-agent Response[...] challenge signed via the Wifi OTP endpoint):
    1. SCPs the local ap.conf.diff.afc.12345 (in this same folder) to
       /root/ap.conf.diff.afc.12345 on the AP (content per
       outdoor_models_6ghz_start.txt).
    2. Opens a root shell and copies it to /tmp/trigger to apply it.

* Targets APs from AP_LIST below by default, or from --ap HOST[,HOST...]
if given. --ap accepts ANY host/IP (no dependency on AP_LIST — an AP does
not need to be present in AP_LIST to be targeted via --ap).

* All APs are processed concurrently (ThreadPoolExecutor).

usage:
  python3 afc-configure.py (It will use AP_LIST of present in .py file)
  python3 afc-configure.py --ap 10.86.205.157
  python3 afc-configure.py --ap 10.86.205.157,10.86.205.158
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
log = logging.getLogger('afc-configure')

# ---------------------------------------------------------------------------
# AP IP List  –  edit this list to target different APs (or use --ap)
# ---------------------------------------------------------------------------
AP_LIST: list[str] = [
]

# ---------------------------------------------------------------------------
# AP SSH credentials (root shell)
# ---------------------------------------------------------------------------
CLI_USERNAME = 'root'
CLI_PASSWORD = 'arastra'

# ---------------------------------------------------------------------------
# Local AFC config file (content per outdoor_models_6ghz_start.txt) +
# remote config/trigger paths
# ---------------------------------------------------------------------------
LOCAL_CONF_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ap.conf.diff.afc.12345')
REMOTE_CONF_PATH = '/root/ap.conf.diff.afc.12345'
REMOTE_TRIGGER_PATH = '/tmp/trigger'

# ---------------------------------------------------------------------------
# Wifi OTP signing endpoint used by the arista-ssh-agent Response[...] challenge/
# response prompt (same as kickmac-deploy.py / qwrap-manager.py).
# ---------------------------------------------------------------------------
WIFI_OTP_URL = 'https://license.aristanetworks.com/sign/wifi-otp/'
WIFI_OTP_KEY = 'd9ca932a4bc8fbaaa5021b00e14dd453d469eaaf'


def getWifiOtp(challenge):
    '''Sign an arista-ssh-agent Response[...] challenge via the Wifi OTP
    endpoint (same as kickmac-deploy.py / qwrap-manager.py).'''
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
    'arista-ssh-agent' Response[...] challenge/response prompt).'''
    env = {'TERM': 'dumb', 'HOME': os.environ.get('HOME')}
    sshAgentSocket = os.environ.get('SSH_AUTH_SOCK')
    if sshAgentSocket:
        env['SSH_AUTH_SOCK'] = sshAgentSocket
    return env


def _authenticate(child, password, host, promptPattern=None, timeout=60):
    '''Shared login challenge/response loop (host-key/password/OTP), same as
    kickmac-deploy.py / qwrap-manager.py. Used for one-shot scp transfers
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
        elif idx == 4:                       # EOF — scp finished
            return
        else:
            raise RuntimeError(f'Failed to connect/login to {host}')


ROOT_PROMPT = r'#\s*$'


def scpToAp(host, username, password, localPath, remoteDst, timeout=60):
    '''SCP a local file to the AP, handling the same login challenge/response
    as kickmac-deploy.py's scpToAp().'''
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


def triggerAfcOnAp(host, username, password, confPath, triggerPath, timeout=30):
    '''Open an interactive root SSH session and cp confPath -> triggerPath
    to apply the just-SCP'd AFC config.'''
    cmd = f'ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null {username}@{host}'
    child = pexpect.spawn(cmd, env=_sshEnv(), timeout=timeout, encoding='utf-8')
    try:
        _authenticate(child, password, host, ROOT_PROMPT, timeout=timeout)

        copyCmd = f'cp {confPath} {triggerPath}'
        log.info(f'[{host}] [ROOT] {copyCmd}')
        child.sendline(copyCmd)
        child.expect(ROOT_PROMPT, timeout=timeout)
        log.debug(f'[{host}] cp output: {child.before!r}')
    finally:
        try:
            child.sendline('exit')
        except Exception:
            pass
        if child.isalive():
            child.close(force=True)


def configureAfcOnAp(host, username, password, localConfPath, confPath, triggerPath):
    '''SCP localConfPath -> confPath on the AP, then cp confPath ->
    triggerPath (in a root shell) to apply it.'''
    scpToAp(host, username, password, localConfPath, confPath)
    triggerAfcOnAp(host, username, password, confPath, triggerPath)
    return {'ap': host, 'confFile': 'ok', 'trigger': 'ok'}


def runConcurrently(func, apList, actionName):
    '''Run func(host) concurrently across apList (one worker per AP), same
    pattern as kickmac-deploy.py / qwrap-manager.py.'''
    errors = []
    results = {}
    with ThreadPoolExecutor(max_workers=len(apList)) as executor:
        futureToHost = {executor.submit(func, host): host for host in apList}
        for future in as_completed(futureToHost):
            host = futureToHost[future]
            try:
                results[host] = future.result()
                log.info(f'{host}: {actionName} succeeded')
            except Exception as e:
                log.error(f'{host}: {actionName} FAILED: {e}')
                errors.append(host)
                results[host] = {'ap': host, 'confFile': 'FAILED', 'trigger': 'FAILED'}
    if errors:
        log.error(f'{actionName} failed on: {errors}')
    return results, errors


_EXAMPLES = '''\
examples:
  python3 afc-configure.py
  python3 afc-configure.py --ap 10.86.205.157
  python3 afc-configure.py --ap 10.86.205.157,10.86.205.158
  python3 afc-configure.py --debug

--ap accepts ANY AP host/IP, whether or not it is present in AP_LIST.
'''


class _HelpFormatter(argparse.RawDescriptionHelpFormatter):
    def __init__(self, prog):
        super().__init__(prog, max_help_position=40, width=200)


def parseArgs():
    parser = argparse.ArgumentParser(
        prog='afc-configure.py',
        usage=argparse.SUPPRESS,
        description=f'SCP {LOCAL_CONF_PATH} to {REMOTE_CONF_PATH} on target AP(s) and apply it via {REMOTE_TRIGGER_PATH}',
        epilog=_EXAMPLES,
        formatter_class=_HelpFormatter,
    )
    parser.add_argument('--ap', metavar='HOST[,HOST...]', default=None,
                         help='Comma-separated list of AP host/IPs to target instead of AP_LIST '
                              '(hosts do not need to already be in AP_LIST)')
    parser.add_argument('--debug', action='store_true', help='Enable debug logging (raw ssh output)')
    return parser.parse_args()


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


def main():
    args = parseArgs()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    if not os.path.isfile(LOCAL_CONF_PATH):
        log.error(f'ap.conf.diff.afc.12345 not found at {LOCAL_CONF_PATH}')
        sys.exit(1)

    print('-' * 40)
    print('Resolve target AP(s)')
    print('-' * 40)
    apList = resolveApList(args)

    print('\n' + '-' * 40)
    print('Configure AFC 6GHz on AP(s)')
    print('-' * 40)
    results, errors = runConcurrently(
        lambda host: configureAfcOnAp(host, CLI_USERNAME, CLI_PASSWORD,
                                       LOCAL_CONF_PATH, REMOTE_CONF_PATH, REMOTE_TRIGGER_PATH),
        apList, 'afc-configure',
    )

    W = 20
    numCols = 3
    ruleWidth = W * numCols + (numCols - 1) * 2
    print("\n" + "=" * ruleWidth)
    print(f"{'AP IP':<{W}}  {'Config File':<{W}}  {'Trigger':<{W}}")
    print("=" * ruleWidth)
    for host in sorted(results):
        r = results[host]
        print(f"{host:<{W}}  {r['confFile']:<{W}}  {r['trigger']:<{W}}")
    print("=" * ruleWidth + "\n")

    if errors:
        sys.exit(1)


if __name__ == '__main__':
    main()
