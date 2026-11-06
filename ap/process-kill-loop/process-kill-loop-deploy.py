#!/usr/bin/env python3
"""Deploy the adjacent process-kill-loop.sh to APs and launch it with nohup.

Uses the same AP selection, SSH/password/Wifi OTP authentication, concurrent
SCP + chmod + launch flow, and summary table as kickmac-deploy.py. Existing
instances are stopped before copying. Output goes to /dev/null; the shell
script writes its own logs under /root/process-kill-loop/ on each AP.

Examples:
    python3 process-kill-loop-deploy.py
    python3 process-kill-loop-deploy.py --ap 10.86.205.157
    python3 process-kill-loop-deploy.py --ap 10.86.205.157,10.86.205.158 --debug

Enter shell script options when prompted, e.g. --random --interval 30.
"""

import argparse
import json
import logging
import os
import shlex
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import pexpect
import requests

# ---------------------------------------------------------------------------
# AP IP List  –  edit this list to target different APs
# ---------------------------------------------------------------------------
AP_LIST: list[str] = [
# '10.86.205.78',
'10.86.205.42'
]

_log_handler = logging.StreamHandler()
_log_handler.setFormatter(logging.Formatter(
    fmt='%(asctime)s  %(levelname)-9s%(message)s', datefmt='%H:%M:%S'))
logging.basicConfig(level=logging.INFO, handlers=[_log_handler])
log = logging.getLogger('process-kill-loop-deploy')

# ---------------------------------------------------------------------------
# AP SSH credentials
# ---------------------------------------------------------------------------
CLI_USERNAME = 'root'
CLI_PASSWORD = 'arastra'

# ---------------------------------------------------------------------------
# Local process-kill-loop shell script + remote destination
# ---------------------------------------------------------------------------
LOCAL_PROCESS_KILL_LOOP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'process-kill-loop.sh')
REMOTE_PROCESS_KILL_LOOP_PATH = '/root/process-kill-loop.sh'

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


def startProcessKillLoopOnAp(host, username, password, processKillLoopArgsLine, timeout=30):
    '''Open an interactive SSH session, launch process-kill-loop in the background with
    nohup (output redirected to /dev/null, so it keeps running on the AP
    after the SSH session closes), then read back its PID via "echo $!".
    Returns the PID as a string; raises if the process exits immediately.'''
    cmd = f'ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null {username}@{host}'
    child = pexpect.spawn(cmd, env=_sshEnv(), timeout=timeout, encoding='utf-8')
    try:
        _authenticate(child, password, host, promptPattern=ROOT_PROMPT, timeout=timeout)
        remoteCmd = f'nohup {REMOTE_PROCESS_KILL_LOOP_PATH} {processKillLoopArgsLine} > /dev/null 2>&1 &'
        log.info(f'[{host}] [ROOT] {remoteCmd}')
        child.sendline(remoteCmd)
        child.expect(ROOT_PROMPT, timeout=timeout)
        log.debug(f'[{host}] start output: {child.before!r}')

        child.sendline('echo $!')
        child.expect(ROOT_PROMPT, timeout=timeout)
        pidOutput = (child.before or '').strip()
        pid = next((line.strip() for line in pidOutput.splitlines()[::-1] if line.strip().isdigit()), None)
        if not pid:
            raise RuntimeError(f'{host}: could not determine process-kill-loop PID')
        child.sendline(f'sleep 1; if kill -0 {pid} 2>/dev/null; then echo RUNNING; else echo STOPPED; fi')
        child.expect(ROOT_PROMPT, timeout=timeout)
        if 'RUNNING' not in (child.before or '').splitlines():
            raise RuntimeError(f'{host}: process-kill-loop exited immediately after launch')
        log.info(f'[{host}] process-kill-loop started with pid={pid}')
        return pid
    finally:
        try:
            child.sendline('exit')
        except Exception:
            pass
        if child.isalive():
            child.close(force=True)


def chmodOnAp(host, username, password, remotePath, timeout=30):
    '''Open an interactive SSH session and chmod +x the just-copied script
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


def killExistingProcessKillLoopOnAp(host, username, password, remotePath, timeout=30):
    '''Stop existing instances before copying, preventing duplicate loops.
    Look up and log matching PIDs, then kill them if any were found.'''
    cmd = f'ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null {username}@{host}'
    child = pexpect.spawn(cmd, env=_sshEnv(), timeout=timeout, encoding='utf-8')
    try:
        _authenticate(child, password, host, promptPattern=ROOT_PROMPT, timeout=timeout)
        log.info(f'[{host}] [ROOT] Checking for existing process-kill-loop process')
        pattern = remotePath.replace('process-kill-loop', '[p]rocess-kill-loop')
        child.sendline(f'pgrep -f {shlex.quote(pattern)}')
        child.expect(ROOT_PROMPT, timeout=timeout)
        pgrepOutput = child.before or ''
        log.debug(f'[{host}] pgrep output: {pgrepOutput!r}')
        pids = [line.strip() for line in pgrepOutput.splitlines() if line.strip().isdigit()]

        if not pids:
            log.info(f'[{host}] No existing process-kill-loop process found')
            return

        log.info(f'[{host}] Found existing process-kill-loop process(es) pid={",".join(pids)} — killing')
        killCmd = f'kill -9 {" ".join(pids)}'
        log.info(f'[{host}] [ROOT] {killCmd}')
        child.sendline(killCmd)
        child.expect(ROOT_PROMPT, timeout=timeout)
        log.debug(f'[{host}] kill output: {child.before!r}')
        log.info(f'[{host}] Killed old process-kill-loop pid={",".join(pids)}')
    finally:
        try:
            child.sendline('exit')
        except Exception:
            pass
        if child.isalive():
            child.close(force=True)


def deployToAp(host, processKillLoopArgsLine):
    '''Runs the deploy steps in order, tracking per-step status for the
    final summary table. Raises (after recording which step got to) if a
    step fails, so runConcurrently's error handling/logging still applies.'''
    result = {'ap': host, 'scp': 'FAILED', 'running': 'FAILED', 'pid': '-'}
    try:
        killExistingProcessKillLoopOnAp(host, CLI_USERNAME, CLI_PASSWORD, REMOTE_PROCESS_KILL_LOOP_PATH)
        scpToAp(host, CLI_USERNAME, CLI_PASSWORD, LOCAL_PROCESS_KILL_LOOP_PATH, REMOTE_PROCESS_KILL_LOOP_PATH)
        result['scp'] = 'ok'
        chmodOnAp(host, CLI_USERNAME, CLI_PASSWORD, REMOTE_PROCESS_KILL_LOOP_PATH)
        pid = startProcessKillLoopOnAp(host, CLI_USERNAME, CLI_PASSWORD, processKillLoopArgsLine)
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


_EXAMPLES = """examples:
  python3 process-kill-loop-deploy.py
  python3 process-kill-loop-deploy.py --ap 10.86.205.157
  python3 process-kill-loop-deploy.py --ap 10.86.205.157,10.86.205.158
  python3 process-kill-loop-deploy.py --debug

Before deployment, enter the shell script options when prompted.
"""

PROCESS_KILL_LOOP_HELP = """process-kill-loop.sh usage:
  process-kill-loop.sh (--random | --all | --specific PROCESS[,PROCESS...]) [--interval SECONDS]

Modes (choose one):
  --random               Kill one randomly selected daemon per loop
  --all                  Kill all tracked daemons in sequence per loop
  --specific PROCESSES   Kill only the comma-separated processes provided

Options:
  --interval SECONDS     Delay between loops (default: 15 seconds)

Tracked processes:
  wl_evt_handler,hostapd,synch_agent,gobin,rrmagent,configagent,
  rsyslogd,app_visibility,wgd,sensord-linux,portald

Examples:
  --random --interval 30
  --all --interval 10
  --specific hostapd,sensord-linux,configagent --interval 20

AP logs: /root/process-kill-loop/process-kill-loop.log
"""

class _HelpFormatter(argparse.RawDescriptionHelpFormatter):
    def __init__(self, prog):
        super().__init__(prog, max_help_position=40, width=200)


def parseArgs():
    parser = argparse.ArgumentParser(
        prog='process-kill-loop-deploy.py',
        usage=argparse.SUPPRESS,
        description=f'SCP {LOCAL_PROCESS_KILL_LOOP_PATH} to {REMOTE_PROCESS_KILL_LOOP_PATH} and start it on all APs in AP_LIST',
        epilog=_EXAMPLES,
        formatter_class=_HelpFormatter,
    )
    parser.add_argument('--ap', metavar='HOST[,HOST...]', default=None,
                         help='Comma-separated list of AP host/IPs to target instead of all APs in AP_LIST')
    parser.add_argument('--debug', action='store_true', help='Enable debug logging (raw scp/ssh output)')
    return parser.parse_args()


def validateScriptArgs(argsLine):
    """Validate options locally and quote each argument for the AP shell."""
    parser = argparse.ArgumentParser(prog='process-kill-loop.sh')
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--random', action='store_true')
    modes.add_argument('--all', action='store_true')
    modes.add_argument('--specific', metavar='PROCESS[,PROCESS...]')
    parser.add_argument('--interval', type=int)
    try:
        tokens = shlex.split(argsLine)
    except ValueError as exc:
        parser.error(str(exc))
    options = parser.parse_args(tokens)
    if options.interval is not None and options.interval <= 0:
        parser.error('--interval must be a positive integer')
    if options.specific is not None:
        processes = options.specific.split(',')
        if any(not process or not all(c.isalnum() or c in '_.-' for c in process)
               for process in processes):
            parser.error('--specific requires comma-separated process names without spaces')
    return shlex.join(tokens)


def promptForProcessKillLoopArgsLine():
    """Show the shell script options and prompt once before changing any AP."""
    print(PROCESS_KILL_LOOP_HELP)
    try:
        argsLine = input('Enter process-kill-loop options to run on the AP(s): ').strip()
    except EOFError:
        log.error('No process-kill-loop options entered. Aborting.')
        sys.exit(1)
    return validateScriptArgs(argsLine)


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

    if not os.path.isfile(LOCAL_PROCESS_KILL_LOOP_PATH):
        log.error(f'process-kill-loop shell script not found at {LOCAL_PROCESS_KILL_LOOP_PATH}')
        sys.exit(1)

    print('-' * 40)
    print('Resolve target AP(s)')
    print('-' * 40)
    apList = list(dict.fromkeys(resolveApList(args)))
    if not apList:
        log.error('No target APs specified. Set AP_LIST or pass --ap HOST[,HOST...].')
        sys.exit(1)

    print('\n' + '-' * 40)
    print('process-kill-loop options')
    print('-' * 40)
    processKillLoopArgsLine = promptForProcessKillLoopArgsLine()

    print('\n' + '-' * 40)
    print('Deploy + start process-kill-loop on AP(s)')
    print('-' * 40)
    results, errors = runConcurrently(lambda host: deployToAp(host, processKillLoopArgsLine), apList, 'deploy')

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
    log.info(f'process-kill-loop deployed and started successfully on all {len(apList)} AP(s)')


if __name__ == '__main__':
    main()
