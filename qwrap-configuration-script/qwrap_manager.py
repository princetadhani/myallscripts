#!/usr/bin/env python3
'''
Standalone, non-interactive QWRAP AP configure/deconfigure tool.

Drives the QWRAP AP CLI directly over SSH (via pexpect), with no SWAT library
dependency. AP list/params come from qwrap_config.py (AP_LIST) in this folder.

All APs are processed concurrently (ThreadPoolExecutor). Within a single AP,
its own radios are configured sequentially (few CLI commands per AP).
'''

import argparse
import glob
import json
import logging
import os
import re
import sys
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed

import pexpect
import requests
from OpenSSL import crypto

# Silence noisy/irrelevant warnings and library-internal DEBUG/INFO logging
# (e.g. urllib3's LibreSSL notice, requests/urllib3 per-request connection
# logs) so --debug output stays focused on this tool's own messages.
warnings.filterwarnings('ignore', message='.*OpenSSL.*')
for _noisyLogger in ('urllib3', 'requests'):
    logging.getLogger(_noisyLogger).setLevel(logging.WARNING)

from qwrap_config import AP_LIST, DEFAULT_CLI_USERNAME, DEFAULT_CLI_PASSWORD

# Wifi OTP signing endpoint used by the arista-ssh-agent Response[...] challenge/
# response prompt (same as SWAT's otpLib.getWifiOTP()).
WIFI_OTP_URL = 'https://license.aristanetworks.com/sign/wifi-otp/'
WIFI_OTP_KEY = 'd9ca932a4bc8fbaaa5021b00e14dd453d469eaaf'


def getWifiOtp(challenge):
    header = {
        'Authorization': f'Token {WIFI_OTP_KEY}',
        'Content-Type':  'application/json',
    }
    payload = json.dumps({'message': challenge})
    response = requests.post(WIFI_OTP_URL, headers=header, data=payload, allow_redirects=True)
    if response.status_code != 201:
        raise RuntimeError(f'Wifi OTP request failed ({response.status_code}) for challenge {challenge}')
    return response.json()['signature']

logging.basicConfig(level=logging.INFO, format='%(asctime)s  %(levelname)-9s%(message)s', datefmt='%H:%M:%S')
log = logging.getLogger('qwrap_manager')

ROOT_PROMPT   = r'#\s*$'
CONFIG_PROMPT = r'\[config\]\$ '
MISC_KEY_MAP  = {
    'keyMgmt':        'key-mgmt',
    'bssid':          'bssid',
    'groupCipher':    'group',
    'pairwiseCipher': 'pairwise',
    'ieee80211w':     'ieee80211w',
    'portalType':     'portal-type',
    'protocol':       'protocol',
    'channelWidth':   'channel-width',
}


def _sshEnv():
    '''Env needed so pexpect can see the Arista SSH Forwarding Agent (for the
    'arista-ssh-agent' Response[...] challenge/response prompt), same as SWAT's
    cliLib._createSshSession().'''
    env = {'TERM': 'dumb', 'HOME': os.environ.get('HOME')}
    sshAgentSocket = os.environ.get('SSH_AUTH_SOCK')
    if sshAgentSocket:
        env['SSH_AUTH_SOCK'] = sshAgentSocket
    return env


def _authenticate(child, password, host, promptPattern=None, timeout=30):
    '''Shared login challenge/response loop (RSA/password/OTP), same as SWAT's
    cliLib._createSshSession(). Used for both interactive SSH sessions (where
    promptPattern is the shell prompt to land on) and one-shot scp transfers
    (where promptPattern is None and we just wait for EOF once authenticated).'''
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
        if idx == 0:
            child.sendline('yes')
        elif idx == 1:
            child.sendline(password)
        elif idx == 2:
            # arista-ssh-agent challenge/response prompt. Sign the challenge
            # via the Wifi OTP endpoint and send the resulting one-time
            # password back, same as SWAT's cueBase.createCliSession().
            challenge = child.match.group(1)
            child.sendline(getWifiOtp(challenge))
        elif idx == 3:
            raise RuntimeError(f'{host}: MFA/OTP prompt received but not supported by this script')
        elif idx == 4:
            # EOF: fine for scp (transfer completed after auth), an error if we
            # were still waiting to land on an interactive shell prompt.
            if promptPattern:
                raise RuntimeError(f'Failed to connect/login to {host}')
            return
        else:
            raise RuntimeError(f'Failed to connect/login to {host}')


def scpToAp(host, username, password, localPath, remoteDst, timeout=60):
    '''SCP a local file to the AP, handling the same login challenge/response as
    QwrapSession. Mirrors CueQwrapCluster.configureQwrapRadios()'s cert upload.'''
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


def scpFromAp(host, username, password, remoteSrc, localPath, timeout=60):
    '''SCP a remote file from the AP to the local machine, handling the same
    login challenge/response as QwrapSession. Mirrors scpToAp() but reversed.'''
    localPath = os.path.expanduser(localPath)
    cmd = (f'scp -O -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null '
           f'{username}@{host}:"{remoteSrc}" "{localPath}"')
    log.info(f'[{host}] [SCP] {remoteSrc} -> {localPath}')
    child = pexpect.spawn(cmd, env=_sshEnv(), timeout=timeout, encoding='utf-8')
    try:
        _authenticate(child, password, host, promptPattern=None, timeout=timeout)
        transcript = child.before or ''
        child.close()
        log.debug(f'[{host}] scp transcript: {transcript.strip()!r} (exit={child.exitstatus})')
        if child.exitstatus not in (0, None):
            raise RuntimeError(f'{host}: scp {remoteSrc} -> {localPath} failed '
                                f'(exit={child.exitstatus}): {transcript.strip()!r}')
    finally:
        if child.isalive():
            child.close(force=True)


class QwrapSession:
    '''Minimal SSH CLI session to a QWRAP AP (root -> config shell), no SWAT dependency.'''

    def __init__(self, host, username, password, timeout=30):
        self.host    = host
        self.timeout = timeout

        cmd = f'ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null {username}@{host}'
        self.child = pexpect.spawn(cmd, env=_sshEnv(), timeout=timeout, encoding='utf-8')
        _authenticate(self.child, password, host, promptPattern=ROOT_PROMPT, timeout=timeout)
        self._inConfig = False

    def _toConfig(self):
        if not self._inConfig:
            self.child.sendline('su - config')
            self.child.expect(CONFIG_PROMPT)
            self._inConfig = True

    def configSend(self, cmd, timeout=120):
        self._toConfig()
        log.info(f'[{self.host}] [CLI] {cmd}')
        self.child.sendline(cmd)
        self.child.expect(CONFIG_PROMPT, timeout=timeout)
        output = self.child.before
        log.debug(f'[{self.host}] CLI raw output: {output!r}')
        return output

    def rootSend(self, cmd, timeout=60):
        if self._inConfig:
            self.child.sendline('exit')
            self.child.expect(ROOT_PROMPT)
            self._inConfig = False
        log.info(f'[{self.host}] [CLI] {cmd}')
        self.child.sendline(cmd)
        self.child.expect(ROOT_PROMPT, timeout=timeout)
        output = self.child.before
        log.debug(f'[{self.host}] CLI raw output: {output!r}')
        return output

    def rebootAp(self, timeout=10):
        '''Send "reboot" at the root shell (mirrors CueApCommon.reboot()'s
        self._rootSend('reboot', reboot=True)) and don't wait for a prompt
        afterward, since the AP disconnects the SSH session almost
        immediately. Fire-and-forget: does not wait for the AP to come back.'''
        if self._inConfig:
            self.child.sendline('exit')
            try:
                self.child.expect(ROOT_PROMPT, timeout=self.timeout)
            except (pexpect.EOF, pexpect.TIMEOUT):
                pass
            self._inConfig = False
        self.child.sendline('reboot')
        try:
            self.child.expect(ROOT_PROMPT, timeout=timeout)
        except (pexpect.EOF, pexpect.TIMEOUT):
            pass

    def close(self):
        try:
            self.child.sendline('exit')
            self.child.close(force=True)
        except Exception:
            pass


def buildConfigureCmd(radioId, params):
    ssid     = params['ssid']
    security = params['security']

    if bool(params.get('protocol')) != bool(params.get('channelWidth')):
        raise ValueError(
            f'radio {radioId}: protocol and channelWidth must both be set or both left empty '
            f'(got protocol={params.get("protocol")!r}, channelWidth={params.get("channelWidth")!r})')

    cmd      = f'qwrap configure radios {radioId} security {security} ssid "{ssid}"'

    if params.get('passphrase'):
        cmd += f' passphrase "{params["passphrase"]}"'

    if params.get('eapType'):
        eapType = params['eapType']
        cmd += f' eap-type {eapType}'
        if eapType == 'peap':
            cmd += f' username "{params["username"]}" password "{params["password"]}"'
        elif eapType == 'tls':
            cmd += (f' username "{params["username"]}" ca-cert "{params["serverCaCertPath"]}"'
                     f' client-cert "{params["digitalCertPath"]}" private-key "{params["privateKeyPath"]}"')

    for key, cliKey in MISC_KEY_MAP.items():
        if params.get(key) is not None:
            cmd += f' {cliKey} "{params[key]}"'

    return cmd


def buildAddClientsCmd(radioId, clientParams):
    '''Build the "qwrap client add" command for a radio's "clients" config,
    mirroring CueApQwrap.addQwrapClients()'s command construction.'''
    count = clientParams.get('count')
    if not count:
        return None
    if not 1 <= count <= 28:
        raise ValueError(f'radio {radioId}: clients.count must be 1-28 (got {count})')

    cmd = f'qwrap client add radios {radioId} num {count}'
    if clientParams.get('ipv4'):
        cmd += ' dhcp 1'
    if clientParams.get('ipv6'):
        cmd += ' ipv6 1'
    if clientParams.get('hostnamePrefix'):
        cmd += f' hostname-prefix {clientParams["hostnamePrefix"]}'
    return cmd


def collapseRange(ids):
    '''Collapse a list of client-id ints into a compact CLI range string, e.g.
    [1,2,3,5] -> "1-3,5", mirroring utilLib.collapseRange().'''
    ids = sorted(ids)
    ranges = []
    start = prev = ids[0]
    for n in ids[1:]:
        if n == prev + 1:
            prev = n
            continue
        ranges.append(f'{start}-{prev}' if start != prev else str(start))
        start = prev = n
    ranges.append(f'{start}-{prev}' if start != prev else str(start))
    return ','.join(ranges)


def getCurrentClientIds(session, radioId):
    '''Query the AP's current virtual client IDs for one radio by parsing
    "show qwrap" output, mirroring CueApQwrap._getCurrentClientIds()/getQwrapInfo().'''
    output      = session.configSend('show qwrap', timeout=300)
    radioBlocks = re.split(r'(?=RADIO-ID:\s*\d+)', output)
    for block in radioBlocks:
        match = re.match(r'RADIO-ID:\s*(\d+)', block)
        if not match or int(match.group(1)) != radioId:
            continue
        clientIds = []
        for line in block.splitlines():
            idMatch = re.match(r'^\s*(\d+)\s+[0-9a-fA-F:]{17}\s', line)
            if idMatch:
                clientIds.append(int(idMatch.group(1)))
        return clientIds
    return []


def getDeviceInfo(session):
    '''Query "Device model" and "Device MAC address" via "show device info",
    e.g. for naming per-AP artifact files.'''
    output = session.configSend('show device info', timeout=30)
    modelMatch = re.search(r'Device model:\s*(\S+)', output)
    macMatch   = re.search(r'Device MAC address:\s*([0-9A-Fa-f:]+)', output)
    if not modelMatch or not macMatch:
        raise RuntimeError(f'Could not parse device model/MAC from "show device info" output: {output!r}')
    model = modelMatch.group(1)
    mac   = macMatch.group(1).replace(':', '').upper()
    return model, mac


QWRAP_CONFIG_YAML_REMOTE_PATH = '/opt/qwrap/qwrap_config.yaml'
QWRAP_CONFIG_YAML_LOCAL_DIR   = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'qwrap-config-yaml-files')


def saveQwrapConfigYaml(apInfo):
    '''Save the AP's /opt/qwrap/qwrap_config.yaml to a local
    qwrap-config-yaml-files/ directory (created next to this script if it
    doesn't exist), named <Model>-<IP>-<MAC>.yaml (e.g.
    C-460D-10.86.205.157-E01CA7C2750F.yaml). Overwrites any existing file with
    the same name (used by --action save-qwrap-config-yaml).'''
    host                = apInfo['host']
    username, password  = _apCredentials(apInfo)
    session             = QwrapSession(host, username, password)
    try:
        model, mac = getDeviceInfo(session)
    finally:
        session.close()

    os.makedirs(QWRAP_CONFIG_YAML_LOCAL_DIR, exist_ok=True)
    localPath = os.path.join(QWRAP_CONFIG_YAML_LOCAL_DIR, f'{model}-{host}-{mac}.yaml')
    scpFromAp(host, username, password, QWRAP_CONFIG_YAML_REMOTE_PATH, localPath)


def applyQwrapConfigYaml(apInfo):
    '''Apply a previously-saved qwrap_config.yaml (see save-qwrap-config-yaml)
    back onto this AP: find the saved file by the AP's live MAC address,
    enable QWRAP config persistence (creates /opt/qwrap/qwrap_config.yaml if
    missing), SCP the saved file over it, then reboot the AP (fire-and-
    forget - does not wait for the AP to come back up).'''
    host                = apInfo['host']
    username, password  = _apCredentials(apInfo)
    session             = QwrapSession(host, username, password)
    try:
        _, mac = getDeviceInfo(session)
        matches = glob.glob(os.path.join(QWRAP_CONFIG_YAML_LOCAL_DIR, f'*-{mac}.yaml'))
        if not matches:
            raise RuntimeError(f'{host}: no saved yaml found for MAC {mac} in {QWRAP_CONFIG_YAML_LOCAL_DIR}')
        if len(matches) > 1:
            raise RuntimeError(f'{host}: multiple saved yaml files found for MAC {mac}: {matches} '
                                '- remove the stale one(s) and retry')
        localPath = matches[0]
        session.configSend('qwrap config persist', timeout=60)
        log.info(f'{host}: QWRAP config persistence enabled')
    finally:
        session.close()

    scpToAp(host, username, password, localPath, QWRAP_CONFIG_YAML_REMOTE_PATH)

    session = QwrapSession(host, username, password)
    try:
        session.rebootAp()
        log.info(f'{host}: reboot triggered (fire-and-forget)')
    finally:
        session.close()


def _apCredentials(apInfo):
    username = apInfo.get('cliUsername') or DEFAULT_CLI_USERNAME
    password = apInfo.get('cliPassword') or DEFAULT_CLI_PASSWORD
    return username, password


CERT_KEYS = ('digitalCertPath', 'privateKeyPath', 'serverCaCertPath')


def _extractCommonNameFromCert(certPath):
    '''Parse the client cert's subject commonName (CN), same as SWAT's
    securityLib.parseCertificate(), so "username" can be auto-derived instead
    of hand-entered in qwrap_config.py for eapType=="tls".'''
    certPath = os.path.expanduser(certPath)
    with open(certPath, 'r') as f:
        certData = f.read()
    cert = crypto.load_certificate(crypto.FILETYPE_PEM, certData)
    for attr in cert.get_subject().get_components():
        if attr[0].decode('utf-8') == 'CN':
            return attr[1].decode('utf-8')
    raise RuntimeError(f'No commonName (CN) found in certificate {certPath}')


def _uploadEapTlsCerts(session, host, username, password, params):
    '''For eapType=="tls" radios, SCP the local (laptop) cert paths in params to
    the AP under /root/<username>/ and rewrite params to the AP-side paths
    before the CLI command is built, mirroring CueQwrapCluster's cert upload.
    If "username" isn't set, it's auto-derived from digitalCertPath's CN
    (mirroring CueQwrapCluster.configureQwrapRadios()).'''
    if not params.get('username'):
        if not params.get('digitalCertPath'):
            raise RuntimeError(f'{host}: eapType "tls" requires "digitalCertPath" '
                                'to auto-derive "username" from its CN, or set "username" explicitly')
        params['username'] = _extractCommonNameFromCert(params['digitalCertPath'])
        log.info(f'{host}: derived username "{params["username"]}" from digitalCertPath CN')

    remoteCertDir = f'/root/{params["username"]}'
    session.rootSend(f'mkdir -p "{remoteCertDir}"')
    for key in CERT_KEYS:
        localPath = params.get(key)
        if not localPath:
            continue
        localPath = os.path.expanduser(localPath)
        fileName  = os.path.basename(localPath)
        remoteDst = f'{remoteCertDir}/{fileName}'
        scpToAp(host, username, password, localPath, remoteDst)
        params[key] = remoteDst


def _applyPersist(session, host, persistOption):
    '''If persistOption is 'enable'/'disable', send "qwrap config persist"/"no
    qwrap config persist" over the given (already-open) session before the
    caller's main action runs. persistOption=None (default) does nothing.'''
    if persistOption is None:
        return
    cmd = 'qwrap config persist' if persistOption == 'enable' else 'no qwrap config persist'
    session.configSend(cmd, timeout=60)
    log.info(f'{host}: QWRAP config persistence {persistOption}d')


def configureAp(apInfo, addClients=True, persistOption=None):
    '''Configure each radio's qwrap SSID. If addClients is True (default),
    also add virtual clients per radio's "clients" config right after it's
    configured (used by --action configure). Pass addClients=False to only
    configure radios and skip client add (used by --action radio-configure).
    If persistOption is 'enable'/'disable', toggles QWRAP config persistence
    on this AP before doing anything else (see --persist).'''
    host              = apInfo['host']
    username, password = _apCredentials(apInfo)
    session           = QwrapSession(host, username, password)
    try:
        _applyPersist(session, host, persistOption)
        for radioId, params in apInfo.get('radios', {}).items():
            if not params:
                log.info(f'{host}: radio {radioId} has no params, skipping')
                continue
            if params.get('eapType') == 'tls':
                _uploadEapTlsCerts(session, host, username, password, params)
            cmd    = buildConfigureCmd(radioId, params)
            output = session.configSend(cmd, timeout=120)
            if 'error' in output.lower() or 'Qwrap configured on radios' not in output:
                raise RuntimeError(f'{host} radio {radioId}: configure failed. Output:\n{output}')
            log.info(f'{host}: radio {radioId} configured (ssid={params["ssid"]!r}, security={params["security"]!r})')

            if not addClients:
                continue
            clientParams = params.get('clients')
            if not clientParams:
                log.info(f'{host}: radio {radioId} has no "clients" config, skipping client add')
                continue
            clientCmd = buildAddClientsCmd(radioId, clientParams)
            if not clientCmd:
                log.info(f'{host}: radio {radioId} "clients" has no/zero count, skipping client add')
                continue
            output = session.configSend(clientCmd, timeout=300)
            if 'error' in output.lower() or 'Adding only' in output:
                raise RuntimeError(f'{host} radio {radioId}: add clients failed. Output:\n{output}')
            log.info(f'{host}: radio {radioId} added {clientParams["count"]} client(s)')
    finally:
        session.close()


def deconfigureAp(apInfo, persistOption=None):
    '''Remove all clients on each radio, then deconfigure the radios (used by
    --action deconfigure). Client removal is implicit qwrap behavior. If
    persistOption is 'enable'/'disable', toggles QWRAP config persistence on
    this AP before doing anything else (see --persist).'''
    host                = apInfo['host']
    radios              = [radioId for radioId, params in apInfo.get('radios', {}).items() if params]
    username, password  = _apCredentials(apInfo)
    session             = QwrapSession(host, username, password)
    try:
        _applyPersist(session, host, persistOption)
        if not radios:
            log.info(f'{host}: no radios configured in params, skipping')
            return

        # Remove all clients on each radio first, mirroring rmAllQwrapClients():
        # query actual client IDs and remove only those (blind "id 1-28" causes
        # the AP to print "error in removing client with id X" for every empty
        # slot, which would false-trigger our error check below).
        for radioId in radios:
            clientIds = getCurrentClientIds(session, radioId)
            if not clientIds:
                continue
            idRange = collapseRange(clientIds)
            session.configSend(f'qwrap client remove id {idRange} radio {radioId}', timeout=300)

        # Deconfigure the radios
        radioStr = ','.join(str(r) for r in radios)
        output   = session.configSend(f'no qwrap configure radio {radioStr}', timeout=300)
        if 'error' in output.lower():
            raise RuntimeError(f'{host}: deconfigure failed. Output:\n{output}')
        log.info(f'{host}: deconfigured radios {radioStr}')
    finally:
        session.close()


def addClientsOnly(apInfo, persistOption=None):
    '''Only add virtual clients per radio's "clients" config (used by --action
    client-add). Assumes each radio's qwrap SSID is already configured. If
    persistOption is 'enable'/'disable', toggles QWRAP config persistence on
    this AP before doing anything else (see --persist).'''
    host                = apInfo['host']
    username, password  = _apCredentials(apInfo)
    session             = QwrapSession(host, username, password)
    try:
        _applyPersist(session, host, persistOption)
        for radioId, params in apInfo.get('radios', {}).items():
            if not params:
                log.info(f'{host}: radio {radioId} has no params, skipping')
                continue
            clientParams = params.get('clients')
            if not clientParams:
                log.info(f'{host}: radio {radioId} has no "clients" config, skipping')
                continue
            clientCmd = buildAddClientsCmd(radioId, clientParams)
            if not clientCmd:
                log.info(f'{host}: radio {radioId} "clients" has no/zero count, skipping')
                continue
            output = session.configSend(clientCmd, timeout=300)
            if 'error' in output.lower() or 'Adding only' in output:
                raise RuntimeError(f'{host} radio {radioId}: add clients failed. Output:\n{output}')
            log.info(f'{host}: radio {radioId} added {clientParams["count"]} client(s)')
    finally:
        session.close()


def removeClientsOnly(apInfo, persistOption=None):
    '''Only remove virtual clients on each configured radio (used by --action
    client-remove). Does NOT deconfigure the radios themselves. If
    persistOption is 'enable'/'disable', toggles QWRAP config persistence on
    this AP before doing anything else (see --persist).'''
    host                = apInfo['host']
    radios              = [radioId for radioId, params in apInfo.get('radios', {}).items() if params]
    username, password  = _apCredentials(apInfo)
    session             = QwrapSession(host, username, password)
    try:
        _applyPersist(session, host, persistOption)
        if not radios:
            log.info(f'{host}: no radios configured in params, skipping')
            return
        for radioId in radios:
            clientIds = getCurrentClientIds(session, radioId)
            if not clientIds:
                log.info(f'{host}: radio {radioId} has no clients, skipping')
                continue
            idRange = collapseRange(clientIds)
            output  = session.configSend(f'qwrap client remove id {idRange} radio {radioId}', timeout=300)
            if 'error' in output.lower():
                raise RuntimeError(f'{host} radio {radioId}: client remove failed. Output:\n{output}')
            log.info(f'{host}: radio {radioId} clients removed (ids {idRange})')
    finally:
        session.close()


def removeClientCount(apInfo, count, persistOption=None):
    '''Remove the last <count> virtual clients (most-recently-added first) from
    each configured radio (used by --action client-remove-count --count N),
    mirroring CueApQwrap.rmQwrapClients(). If persistOption is 'enable'/
    'disable', toggles QWRAP config persistence on this AP before doing
    anything else (see --persist).'''
    host                = apInfo['host']
    radios              = [radioId for radioId, params in apInfo.get('radios', {}).items() if params]
    username, password  = _apCredentials(apInfo)
    session             = QwrapSession(host, username, password)
    try:
        _applyPersist(session, host, persistOption)
        if not radios:
            log.info(f'{host}: no radios configured in params, skipping')
            return
        for radioId in radios:
            clientIds = getCurrentClientIds(session, radioId)
            if not clientIds:
                log.info(f'{host}: radio {radioId} has no clients, skipping')
                continue
            if len(clientIds) < count:
                raise RuntimeError(f'{host} radio {radioId}: cannot remove {count} clients, '
                                    f'only {len(clientIds)} present')
            idRange = collapseRange(sorted(clientIds)[-count:])
            output  = session.configSend(f'qwrap client remove id {idRange} radio {radioId}', timeout=300)
            if 'error' in output.lower():
                raise RuntimeError(f'{host} radio {radioId}: client remove failed. Output:\n{output}')
            log.info(f'{host}: radio {radioId} removed {count} client(s) (ids {idRange})')
    finally:
        session.close()


def disassociateClients(apInfo, persistOption=None):
    '''Disassociate all currently-connected virtual clients on each configured
    radio (used by --action client-disassociate), mirroring
    CueApQwrap.disassociateQwrapClients(). If persistOption is 'enable'/
    'disable', toggles QWRAP config persistence on this AP before doing
    anything else (see --persist).'''
    host                = apInfo['host']
    radios              = [radioId for radioId, params in apInfo.get('radios', {}).items() if params]
    username, password  = _apCredentials(apInfo)
    session             = QwrapSession(host, username, password)
    try:
        _applyPersist(session, host, persistOption)
        if not radios:
            log.info(f'{host}: no radios configured in params, skipping')
            return
        for radioId in radios:
            clientIds = getCurrentClientIds(session, radioId)
            if not clientIds:
                log.info(f'{host}: radio {radioId} has no clients to disassociate, skipping')
                continue
            idRange = collapseRange(clientIds)
            output  = session.configSend(f'qwrap client disassociate radio {radioId} id {idRange}', timeout=300)
            if 'error' in output.lower():
                raise RuntimeError(f'{host} radio {radioId}: client disassociate failed. Output:\n{output}')
            log.info(f'{host}: radio {radioId} disassociated client(s) (ids {idRange})')
    finally:
        session.close()


def reassociateClients(apInfo, reAcquireIp, persistOption=None):
    '''Reassociate all currently-connected virtual clients on each configured
    radio (used by --action client-reassociate [--reacquire-ip]), mirroring
    CueApQwrap.reassociateQwrapClients(). If persistOption is 'enable'/
    'disable', toggles QWRAP config persistence on this AP before doing
    anything else (see --persist).'''
    host                = apInfo['host']
    radios              = [radioId for radioId, params in apInfo.get('radios', {}).items() if params]
    username, password  = _apCredentials(apInfo)
    session             = QwrapSession(host, username, password)
    try:
        _applyPersist(session, host, persistOption)
        if not radios:
            log.info(f'{host}: no radios configured in params, skipping')
            return
        for radioId in radios:
            clientIds = getCurrentClientIds(session, radioId)
            if not clientIds:
                log.info(f'{host}: radio {radioId} has no clients to reassociate, skipping')
                continue
            idRange = collapseRange(clientIds)
            cmd     = f'qwrap client reassociate radio {radioId} id {idRange}'
            if reAcquireIp:
                cmd += ' restart-dhcp 1'
            output  = session.configSend(cmd, timeout=300)
            if 'error' in output.lower():
                raise RuntimeError(f'{host} radio {radioId}: client reassociate failed. Output:\n{output}')
            log.info(f'{host}: radio {radioId} reassociated client(s) (ids {idRange})')
    finally:
        session.close()


def enableConfigPersist(apInfo):
    '''Enable QWRAP config persistence (config written to disk in real time, so
    it survives reboot) on this AP (used by --action config-persist-enable).
    Mirrors CueApQwrap.enableQwrapConfigPersistence(). Independent of radio/
    client state - typically run once, e.g. after an AP image upgrade.'''
    host                = apInfo['host']
    username, password  = _apCredentials(apInfo)
    session             = QwrapSession(host, username, password)
    try:
        session.configSend('qwrap config persist', timeout=60)
        log.info(f'{host}: QWRAP config persistence enabled')
    finally:
        session.close()


def disableConfigPersist(apInfo):
    '''Disable QWRAP config persistence (deletes saved config, stops writing to
    disk) on this AP (used by --action config-persist-disable). Mirrors
    CueApQwrap.disableQwrapConfigPersistence().'''
    host                = apInfo['host']
    username, password  = _apCredentials(apInfo)
    session             = QwrapSession(host, username, password)
    try:
        session.configSend('no qwrap config persist', timeout=60)
        log.info(f'{host}: QWRAP config persistence disabled')
    finally:
        session.close()


def runConcurrently(func, apList, actionName):
    errors = []
    with ThreadPoolExecutor(max_workers=len(apList)) as executor:
        futureToHost = {executor.submit(func, ap): ap['host'] for ap in apList}
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


EPILOG = '''\
actions:
  configure             Configure radios and add clients (full setup)
  deconfigure           Remove clients and deconfigure radios (full teardown)
  radio-configure       Configure radios only, skip adding clients
  client-add            Add clients only (radios must already be configured)
  client-remove         Remove all clients only, radios stay configured
  client-remove-count   Remove last N clients only, requires --count N
  client-disassociate   Disassociate all currently-connected clients
  client-reassociate      Reassociate all currently-connected clients, optionally with --reacquire-ip
  config-persist-enable   Enable QWRAP config persistence (survives reboot), typically run once after an AP image upgrade
  config-persist-disable  Disable QWRAP config persistence (deletes saved config, stops writing to disk)
  save-qwrap-config-yaml   Save /opt/qwrap/qwrap_config.yaml from each AP to ./qwrap-config-yaml-files/<Model>-<IP>-<MAC>.yaml
  apply-qwrap-config-yaml  Apply a saved yaml (matched by the AP's live MAC) back to /opt/qwrap/qwrap_config.yaml on the AP, then reboot (fire-and-forget)

examples:
  python3 qwrap_manager.py --action configure
  python3 qwrap_manager.py --action deconfigure
  python3 qwrap_manager.py --action configure --ap 10.86.205.157,10.86.205.158
  python3 qwrap_manager.py --action radio-configure --ap 10.86.205.157
  python3 qwrap_manager.py --action client-add --ap 10.86.205.157
  python3 qwrap_manager.py --action client-remove --ap 10.86.205.157
  python3 qwrap_manager.py --action client-remove-count --count 2
  python3 qwrap_manager.py --action client-remove-count --count 2 --ap 10.86.205.157
  python3 qwrap_manager.py --action client-disassociate
  python3 qwrap_manager.py --action client-reassociate
  python3 qwrap_manager.py --action client-reassociate --reacquire-ip
  python3 qwrap_manager.py --action config-persist-enable
  python3 qwrap_manager.py --action config-persist-enable --ap 10.86.205.157
  python3 qwrap_manager.py --action config-persist-disable
  python3 qwrap_manager.py --action configure --config-persist enable
  python3 qwrap_manager.py --action deconfigure --config-persist disable --ap 10.86.205.157
  python3 qwrap_manager.py --action save-qwrap-config-yaml
  python3 qwrap_manager.py --action save-qwrap-config-yaml --ap 10.86.205.157
  python3 qwrap_manager.py --action apply-qwrap-config-yaml
  python3 qwrap_manager.py --action apply-qwrap-config-yaml --ap 10.86.205.157
  python3 qwrap_manager.py --action configure --debug
'''


class _HelpFormatter(argparse.RawDescriptionHelpFormatter):
    def __init__(self, prog):
        super().__init__(prog, max_help_position=50, width=200)


def parseArgs():
    parser = argparse.ArgumentParser(
        prog='qwrap_manager.py',
        description=__doc__,
        epilog=EPILOG,
        formatter_class=_HelpFormatter,
        add_help=False,
    )

    required = parser.add_argument_group('required arguments')
    required.add_argument('--action', required=True,
                           choices=['configure', 'deconfigure', 'radio-configure', 'client-add', 'client-remove',
                                    'client-remove-count', 'client-disassociate', 'client-reassociate',
                                    'config-persist-enable', 'config-persist-disable',
                                    'save-qwrap-config-yaml', 'apply-qwrap-config-yaml'],
                           help='Action to perform on the target AP(s) (see "actions" below)')

    optional = parser.add_argument_group('optional arguments')
    optional.add_argument('-h', '--help', action='help', help='show this help message and exit')
    optional.add_argument('--ap', metavar='HOST[,HOST...]', default=None,
                           help='Comma-separated list of AP host/IPs to target instead of all APs in AP_LIST')
    optional.add_argument('--count', type=int, default=None, metavar='N',
                           help='Number of last-added clients to remove per radio (required for --action client-remove-count --count N')
    optional.add_argument('--reacquire-ip', action='store_true',
                           help='Re-acquire an IP address on reassociate (only used with --action client-reassociate --reacquire-ip)')
    optional.add_argument('--config-persist', choices=['enable', 'disable'], default=None,
                           help='Toggle QWRAP config persistence on each targeted AP before running --action (same session)')
    optional.add_argument('--debug', action='store_true',
                           help='Enable debug logging (raw CLI/scp output, per-command detail)')

    args = parser.parse_args()

    if args.action == 'client-remove-count':
        if args.count is None:
            parser.error('--action client-remove-count requires --count N')
        if not 1 <= args.count <= 28:
            parser.error(f'--count must be 1-28 (got {args.count})')
    elif args.count is not None:
        parser.error('--count is only valid with --action client-remove-count')

    if args.reacquire_ip and args.action != 'client-reassociate':
        parser.error('--reacquire-ip is only valid with --action client-reassociate')

    if args.config_persist is not None and args.action in ('config-persist-enable', 'config-persist-disable'):
        parser.error('--config-persist is not valid with --action config-persist-enable/config-persist-disable')

    return args


def resolveApList(args):
    if not AP_LIST:
        log.error('AP_LIST in qwrap_config.py is empty. Nothing to do.')
        sys.exit(1)

    if not args.ap:
        return AP_LIST

    requestedHosts = {h.strip() for h in args.ap.split(',') if h.strip()}
    apByHost       = {ap['host']: ap for ap in AP_LIST}
    missingHosts   = requestedHosts - apByHost.keys()
    if missingHosts:
        log.error(f'Host(s) not found in qwrap_config.py AP_LIST: {sorted(missingHosts)}')
        sys.exit(1)

    apList = [apByHost[host] for host in requestedHosts]
    log.info(f'Targeting {len(apList)} AP(s): {[ap["host"] for ap in apList]}')
    return apList


def main():
    args = parseArgs()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    apList = resolveApList(args)

    if args.action == 'configure':
        runConcurrently(lambda apInfo: configureAp(apInfo, persistOption=args.config_persist), apList, 'configure')
    elif args.action == 'deconfigure':
        runConcurrently(lambda apInfo: deconfigureAp(apInfo, persistOption=args.config_persist), apList, 'deconfigure')
    elif args.action == 'radio-configure':
        runConcurrently(lambda apInfo: configureAp(apInfo, addClients=False, persistOption=args.config_persist),
                         apList, 'radio-configure')
    elif args.action == 'client-add':
        runConcurrently(lambda apInfo: addClientsOnly(apInfo, persistOption=args.config_persist), apList, 'client-add')
    elif args.action == 'client-remove':
        runConcurrently(lambda apInfo: removeClientsOnly(apInfo, persistOption=args.config_persist),
                         apList, 'client-remove')
    elif args.action == 'client-remove-count':
        runConcurrently(lambda apInfo: removeClientCount(apInfo, args.count, persistOption=args.config_persist),
                         apList, 'client-remove-count')
    elif args.action == 'client-disassociate':
        runConcurrently(lambda apInfo: disassociateClients(apInfo, persistOption=args.config_persist),
                         apList, 'client-disassociate')
    elif args.action == 'client-reassociate':
        runConcurrently(lambda apInfo: reassociateClients(apInfo, args.reacquire_ip, persistOption=args.config_persist),
                         apList, 'client-reassociate')
    elif args.action == 'config-persist-enable':
        runConcurrently(enableConfigPersist, apList, 'config-persist-enable')
    elif args.action == 'config-persist-disable':
        runConcurrently(disableConfigPersist, apList, 'config-persist-disable')
    elif args.action == 'save-qwrap-config-yaml':
        runConcurrently(saveQwrapConfigYaml, apList, 'save-qwrap-config-yaml')
    elif args.action == 'apply-qwrap-config-yaml':
        runConcurrently(applyQwrapConfigYaml, apList, 'apply-qwrap-config-yaml')


if __name__ == '__main__':
    main()
