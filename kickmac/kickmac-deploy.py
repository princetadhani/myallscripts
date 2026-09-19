#!/usr/bin/env python3
'''
* Standalone kickmac distribution + launch tool.

* Kills any already-running kickmac process on the AP (pkill -f), then SCPs
the local kickmac binary (in this same folder) to /root/kickmac on each AP
in AP_LIST below (or the --ap-filtered subset), using the same SSH/OTP
login logic as qwrap-manager.py / wifiagent-manager.py (arista-ssh-agent
Response[...] challenge signed via the Wifi OTP endpoint). The kill step
avoids "scp: /root/kickmac: Text file busy" when redeploying over a
binary that a previous run still has open.

* After the binary is copied, prompts once for the kickmac options to run
(e.g. "--stateless --mode RANDOM --sleep-seconds 90") and starts kickmac in
the background on each targeted AP via "nohup ... &", output redirected to
/dev/null, so it keeps running after the SSH session closes.

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
# '10.86.205.78',
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


ROOT_PROMPT = r'#\s*$'


def startKickmacOnAp(host, username, password, kickmacArgsLine, timeout=30):
    '''Open an interactive SSH session, launch kickmac in the background with
    nohup (output redirected to /dev/null, so it keeps running on the AP
    after the SSH session closes), then read back its PID via "echo $!".
    Returns the PID as a string (or None if it could not be determined).'''
    cmd = f'ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null {username}@{host}'
    child = pexpect.spawn(cmd, env=_sshEnv(), timeout=timeout, encoding='utf-8')
    try:
        _authenticate(child, password, host, promptPattern=ROOT_PROMPT, timeout=timeout)
        remoteCmd = f'nohup {REMOTE_KICKMAC_PATH} {kickmacArgsLine} > /dev/null 2>&1 &'
        log.info(f'[{host}] [ROOT] {remoteCmd}')
        child.sendline(remoteCmd)
        child.expect(ROOT_PROMPT, timeout=timeout)
        log.debug(f'[{host}] start output: {child.before!r}')

        child.sendline('echo $!')
        child.expect(ROOT_PROMPT, timeout=timeout)
        pidOutput = (child.before or '').strip()
        pid = next((line.strip() for line in pidOutput.splitlines()[::-1] if line.strip().isdigit()), None)
        log.info(f'[{host}] kickmac started with pid={pid}')
        return pid
    finally:
        try:
            child.sendline('exit')
        except Exception:
            pass
        if child.isalive():
            child.close(force=True)


def chmodOnAp(host, username, password, remotePath, timeout=30):
    '''Open an interactive SSH session and chmod +x the just-copied binary
    (scp does not preserve the executable bit here).'''
    cmd = f'ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null {username}@{host}'
    child = pexpect.spawn(cmd, env=_sshEnv(), timeout=timeout, encoding='utf-8')
    try:
        _authenticate(child, password, host, promptPattern=ROOT_PROMPT, timeout=timeout)
        remoteCmd = f'chmod +x {remotePath}'
        log.info(f'[{host}] [ROOT] {remoteCmd}')
        child.sendline(remoteCmd)
        child.expect(ROOT_PROMPT, timeout=timeout)
        log.debug(f'[{host}] chmod output: {child.before!r}')
    finally:
        try:
            child.sendline('exit')
        except Exception:
            pass
        if child.isalive():
            child.close(force=True)


def killExistingKickmacOnAp(host, username, password, remotePath, timeout=30):
    '''Kill any already-running kickmac process on the AP before we SCP a
    fresh binary over it. Without this, a previous run still holding the
    binary open causes "scp: /root/kickmac: Text file busy" on redeploy.
    Looks up the PID(s) via pgrep first (so they can be logged/tracked),
    then kills them if any were found; logs "no existing process" otherwise.'''
    cmd = f'ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null {username}@{host}'
    child = pexpect.spawn(cmd, env=_sshEnv(), timeout=timeout, encoding='utf-8')
    try:
        _authenticate(child, password, host, promptPattern=ROOT_PROMPT, timeout=timeout)
        log.info(f'[{host}] [ROOT] Checking for existing kickmac process')
        child.sendline(f'pgrep -f {remotePath}')
        child.expect(ROOT_PROMPT, timeout=timeout)
        pgrepOutput = child.before or ''
        log.debug(f'[{host}] pgrep output: {pgrepOutput!r}')
        pids = [line.strip() for line in pgrepOutput.splitlines() if line.strip().isdigit()]

        if not pids:
            log.info(f'[{host}] No existing kickmac process found')
            return

        log.info(f'[{host}] Found existing kickmac process(es) pid={",".join(pids)} — killing')
        killCmd = f'kill -9 {" ".join(pids)}'
        log.info(f'[{host}] [ROOT] {killCmd}')
        child.sendline(killCmd)
        child.expect(ROOT_PROMPT, timeout=timeout)
        log.debug(f'[{host}] kill output: {child.before!r}')
        log.info(f'[{host}] Killed old kickmac pid={",".join(pids)}')
    finally:
        try:
            child.sendline('exit')
        except Exception:
            pass
        if child.isalive():
            child.close(force=True)


def deployToAp(host, kickmacArgsLine):
    '''Runs the 3 deploy steps in order, tracking per-step status for the
    final summary table. Raises (after recording which step got to) if a
    step fails, so runConcurrently's error handling/logging still applies.'''
    result = {'ap': host, 'scp': 'FAILED', 'running': 'FAILED', 'pid': '-'}
    try:
        killExistingKickmacOnAp(host, CLI_USERNAME, CLI_PASSWORD, REMOTE_KICKMAC_PATH)
        scpToAp(host, CLI_USERNAME, CLI_PASSWORD, LOCAL_KICKMAC_PATH, REMOTE_KICKMAC_PATH)
        result['scp'] = 'ok'
        chmodOnAp(host, CLI_USERNAME, CLI_PASSWORD, REMOTE_KICKMAC_PATH)
        pid = startKickmacOnAp(host, CLI_USERNAME, CLI_PASSWORD, kickmacArgsLine)
        result['running'] = 'ok'
        result['pid'] = pid or '-'
    except Exception as e:
        e.deployResult = result
        raise
    return result


def runConcurrently(func, apList, actionName):
    '''Run func(host) concurrently across apList (one worker per AP), same
    pattern as qwrap-manager.py's runConcurrently(). Returns (results, errors)
    so the caller can print a final summary table before deciding to exit.'''
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
                partial = getattr(e, 'deployResult', {})
                results[host] = {'ap': host, 'scp': partial.get('scp', 'FAILED'),
                                  'running': partial.get('running', 'FAILED'),
                                  'pid': partial.get('pid', '-')}
    if errors:
        log.error(f'{actionName} failed on: {errors}')
    return results, errors


_EXAMPLES = '''\
examples:
  python3 kickmac-deploy.py
  python3 kickmac-deploy.py --ap 10.86.205.157
  python3 kickmac-deploy.py --ap 10.86.205.157,10.86.205.158
  python3 kickmac-deploy.py --debug

After the kickmac binary is SCP'd to the targeted AP(s), you will be prompted
to enter the kickmac options to run on the AP(s) as a single line, e.g.:
  --stateless --mode RANDOM --sleep-seconds 90
'''

# Real kickmac usage/options, shown to the user right before the input()
# prompt so they can build their one-liner.
KICKMAC_HELP = '''\
kickmac usage:
  kickmac (--stateful | --stateless | --kickmac) --mode <RANDOM|ALL> --sleep-seconds <seconds> [--clear-cache] [--check-ping] [--debug] [--cache-yaml path] [--report path]

kickmac options:
  --stateful               Run kickmac in stateful mode
  --stateless              Run kickmac in stateless mode
  --kickmac                Run kickmac in kickmac mode
  --mode {RANDOM,ALL}      kickmac --mode (RANDOM or ALL)
  --sleep-seconds SECONDS  kickmac --sleep-seconds
  --clear-cache            Pass --clear-cache to kickmac
  --check-ping             Pass --check-ping to kickmac
  --cache-yaml PATH        Pass --cache-yaml PATH to kickmac
  --report PATH            Pass --report PATH to kickmac
  --debug                  Pass --debug to kickmac itself

example: --stateless --mode RANDOM --sleep-seconds 90
'''


class _HelpFormatter(argparse.RawDescriptionHelpFormatter):
    def __init__(self, prog):
        super().__init__(prog, max_help_position=40, width=200)


def parseArgs():
    parser = argparse.ArgumentParser(
        prog='kickmac-deploy.py',
        usage=argparse.SUPPRESS,
        description=f'SCP {LOCAL_KICKMAC_PATH} to {REMOTE_KICKMAC_PATH} and start it on all APs in AP_LIST',
        epilog=_EXAMPLES,
        formatter_class=_HelpFormatter,
    )
    parser.add_argument('--ap', metavar='HOST[,HOST...]', default=None,
                         help='Comma-separated list of AP host/IPs to target instead of all APs in AP_LIST')
    parser.add_argument('--debug', action='store_true', help='Enable debug logging (raw scp/ssh output)')
    return parser.parse_args()


def promptForKickmacArgsLine():
    '''Show kickmac's own --help options, then prompt the user to enter the
    kickmac invocation as a single line (e.g. "--stateless --mode RANDOM
    --sleep-seconds 90"). Hard-exits if left empty.'''
    print(KICKMAC_HELP)
    kickmacArgsLine = input('Enter kickmac options to run on the AP(s): ').strip()
    if not kickmacArgsLine:
        log.error('No kickmac options entered. Aborting.')
        sys.exit(1)
    return kickmacArgsLine


def resolveApList(args):
    '''Prefer --ap if given (accepts any host, no AP_LIST membership check);
    otherwise fall back to AP_LIST defined in this file.'''
    if not args.ap:
        log.info(f'Targeting {len(AP_LIST)} AP(s) from AP_LIST: {AP_LIST}')
        return list(AP_LIST)
    requestedHosts = [h.strip() for h in args.ap.split(',') if h.strip()]
    for host in requestedHosts:
        if host not in AP_LIST:
            log.warning(f'{host} not present in AP_LIST — proceeding anyway')
    log.info(f'Targeting {len(requestedHosts)} AP(s): {requestedHosts}')
    return requestedHosts


def main():
    args = parseArgs()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    if not os.path.isfile(LOCAL_KICKMAC_PATH):
        log.error(f'kickmac binary not found at {LOCAL_KICKMAC_PATH}')
        sys.exit(1)

    print('-' * 40)
    print('Resolve target AP(s)')
    print('-' * 40)
    apList = resolveApList(args)

    print('\n' + '-' * 40)
    print('kickmac options')
    print('-' * 40)
    kickmacArgsLine = promptForKickmacArgsLine()

    print('\n' + '-' * 40)
    print('Deploy + start kickmac on AP(s)')
    print('-' * 40)
    results, errors = runConcurrently(lambda host: deployToAp(host, kickmacArgsLine), apList, 'deploy')

    W = 20
    ruleWidth = W * 4 + 6   # 4 fixed-width columns + 3 two-space separators
    print('\n' + '=' * ruleWidth)
    print(f"{'AP IP':<{W}}  {'SCP':<{W}}  {'Running':<{W}}  {'PID':<{W}}")
    print('=' * ruleWidth)
    for host in sorted(results):
        r = results[host]
        print(f"{host:<{W}}  {r['scp']:<{W}}  {r['running']:<{W}}  {str(r['pid']):<{W}}")
    print('=' * ruleWidth + '\n')

    if errors:
        sys.exit(1)
    log.info(f'kickmac deployed and started successfully on all {len(apList)} AP(s)')


if __name__ == '__main__':
    main()
