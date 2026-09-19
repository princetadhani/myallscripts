#!/usr/bin/env python3
'''
* Standalone AFC 6GHz-on-outdoor-model configuration tool.

* Logs into each AP's ROOT shell (same SSH/OTP login logic as
kickmac-deploy.py / qwrap-manager.py — arista-ssh-agent Response[...]
challenge signed via the Wifi OTP endpoint) and:
    1. Creates /root/ap.conf.diff.afc.12345 with the AFC config block
       (content per outdoor_models_6ghz_start.txt).
    2. Copies it to /tmp/trigger to apply it.

* Targets APs from AP_LIST below by default, or from --ap HOST[,HOST...]
if given. --ap accepts ANY host/IP (no dependency on AP_LIST — an AP does
not need to be present in AP_LIST to be targeted via --ap).

* All APs are processed concurrently (ThreadPoolExecutor).
'''

import argparse
import base64
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
# Remote AFC config file + trigger path
# ---------------------------------------------------------------------------
REMOTE_CONF_PATH = '/root/ap.conf.diff.afc.12345'
REMOTE_TRIGGER_PATH = '/tmp/trigger'

# Content per outdoor_models_6ghz_start.txt — "To Start 6GHz on Outdoor Models"
AFC_CONF_CONTENT = '''\
[ RADIO_START=4 F_MOD ]
AFC_RESP_CONFIG=1
AFC_RADIO_ENABLED=1
REQ_ID=E4D124A0DB5F-1732189015
RESP_CODE=0
EXPIRY_TIME=2024-11-22T11:36:58.124Z
AFC_FREQ_INFO_NUM=2
AVAILABLE_FREQ_START=0
LOW_FREQ=5925
HIGH_FREQ=6425
MAX_PSD=23
AVAILABLE_FREQ_END=0
AVAILABLE_FREQ_START=1
LOW_FREQ=6525
HIGH_FREQ=6865
MAX_PSD=23
AVAILABLE_FREQ_END=1
AVAILABLE_CHANNEL_START=0
AFC_GLOBAL_OPERATING_CLASS=131
AFC_CHANNELS_CFI=1,5,9,13,17,21,25,29,33,37,41,45,49,53,57,61,65,69,73,77,81,85,89,93,117,121,125,129,133,137,141,145,149,153,157,161,165,169,173,177,181
AFC_MAX_EIRP=36,36,36,36,36,36,36,36,36,36,36,36,36,36,36,36,36,36,36,36,36,36,36,36,36,36,36,36,36,36,36,36,36,36,36,36,36,36,36,36,36
AVAILABLE_CHANNEL_END=0
AVAILABLE_CHANNEL_START=1
AFC_GLOBAL_OPERATING_CLASS=132
AFC_CHANNELS_CFI=3,11,19,27,35,43,51,59,67,75,83,91,123,131,139,147,155,163,171,179
AFC_MAX_EIRP=36,36,36,36,36,36,36,36,36,36,36,36,36,36,36,36,36,36,36,36
AVAILABLE_CHANNEL_END=1
AVAILABLE_CHANNEL_START=2
AFC_GLOBAL_OPERATING_CLASS=133
AFC_CHANNELS_CFI=7,23,39,55,71,87,135,151,167
AFC_MAX_EIRP=36,36,36,36,36,36,36,36,36
AVAILABLE_CHANNEL_END=2
AVAILABLE_CHANNEL_START=3
AFC_GLOBAL_OPERATING_CLASS=134
AFC_CHANNELS_CFI=15,47,79,143
AFC_MAX_EIRP=36,36,36,36
AVAILABLE_CHANNEL_END=3
AVAILABLE_CHANNEL_START=4
AFC_GLOBAL_OPERATING_CLASS=136
AFC_CHANNELS_CFI=2
AFC_MAX_EIRP=36
AVAILABLE_CHANNEL_END=4
AVAILABLE_CHANNEL_START=5
AFC_GLOBAL_OPERATING_CLASS=137
AFC_CHANNELS_CFI=31,63
AFC_MAX_EIRP=36,36
AVAILABLE_CHANNEL_END=5
[ RADIO_END=4 ]
'''

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


def _authenticate(child, password, host, promptPattern, timeout=60):
    '''Shared root-shell login challenge/response loop (host-key/password/OTP),
    same as kickmac-deploy.py / qwrap-manager.py.'''
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
            raise RuntimeError(f'{host}: MFA/OTP prompt received but not supported by this script')
        else:
            raise RuntimeError(f'Failed to connect/login to {host}')


ROOT_PROMPT = r'#\s*$'


def configureAfcOnAp(host, username, password, confContent, confPath, triggerPath, timeout=30):
    '''Open an interactive root SSH session, write confContent to confPath
    (via base64 to safely handle multi-line content over the SSH channel),
    then cp confPath -> triggerPath to apply it.'''
    cmd = f'ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null {username}@{host}'
    child = pexpect.spawn(cmd, env=_sshEnv(), timeout=timeout, encoding='utf-8')
    try:
        _authenticate(child, password, host, ROOT_PROMPT, timeout=timeout)

        encoded = base64.b64encode(confContent.encode()).decode()
        writeCmd = f'echo {encoded} | base64 -d > {confPath}'
        log.info(f'[{host}] [ROOT] create {confPath}')
        child.sendline(writeCmd)
        child.expect(ROOT_PROMPT, timeout=timeout)
        log.debug(f'[{host}] write output: {child.before!r}')

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
        description=f'Create {REMOTE_CONF_PATH} on target AP(s) and apply it via {REMOTE_TRIGGER_PATH}',
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

    print('-' * 40)
    print('Resolve target AP(s)')
    print('-' * 40)
    apList = resolveApList(args)

    print('-' * 40)
    print('Configure AFC 6GHz on AP(s)')
    print('-' * 40)
    results, errors = runConcurrently(
        lambda host: configureAfcOnAp(host, CLI_USERNAME, CLI_PASSWORD,
                                       AFC_CONF_CONTENT, REMOTE_CONF_PATH, REMOTE_TRIGGER_PATH),
        apList, 'afc-configure',
    )

    W = 20
    print("\n" + "-" * (W * 3 + 2))
    print(f"{'AP IP':<{W}}  {'Config File':<{W}}  {'Trigger'}")
    print("-" * (W * 3 + 2))
    for host in sorted(results):
        r = results[host]
        print(f"{host:<{W}}  {r['confFile']:<{W}}  {r['trigger']}")
    print("-" * (W * 3 + 2) + "\n")

    if errors:
        sys.exit(1)


if __name__ == '__main__':
    main()
