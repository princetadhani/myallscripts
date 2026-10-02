#!/usr/bin/env python3
'''
Reverse of qwrap-manager.py's buildConfigureCmd()/buildAddClientsCmd(): given
"qwrap configure radios ..." and/or "qwrap client add radios ..." CLI
command(s) (as produced by a QWRAP AP / qwraptor), parse them back into the
per-radio Python dict format used by S1/S2/S3-qwrap-config-file.py's AP_LIST.

Just 2 ways to use this script:

1) Single command, single radio, no clients - pass it directly as one
   quoted argument (use SINGLE quotes on the shell since the command itself
   uses double quotes internally for its own values):

    python3 qwrap-cli-to-config.py --host 10.86.205.122 \\
        'qwrap configure radios 2 security wpa3-dot1x ssid "S1-Agni-Corp" eap-type tls username "qwrapAP3004@qwrap.com" ca-cert "/root/qwrapAP3004@qwrap.com/ca.pem" client-cert "/root/qwrapAP3004@qwrap.com/client.pem" private-key "/root/qwrapAP3004@qwrap.com/key.pem" group "GCMP-256" pairwise "GCMP-256" ieee80211w "required"'

2) Anything more than that (multiple radios and/or configure+client-add
   combos) - put one command per line in a text file and use --file. This
   also sidesteps shell-quoting issues entirely:

    python3 qwrap-cli-to-config.py --host 10.86.205.122 --file commands.txt

   commands.txt:
       qwrap configure radios 0 security wpa2 ssid "S1-GPSK" passphrase "welcome3005" group "CCMP" pairwise "CCMP"
       qwrap client add radios 0 num 28 dhcp 1 ipv6 1 hostname-prefix GPSK-R1
       qwrap configure radios 1,2 security wpa3-dot1x ssid "S1-Agni-Corp" eap-type tls username "qwrapAP3004@qwrap.com" ca-cert "/root/qwrapAP3004@qwrap.com/ca.pem" client-cert "/root/qwrapAP3004@qwrap.com/client.pem" private-key "/root/qwrapAP3004@qwrap.com/key.pem" group "GCMP-256" pairwise "GCMP-256" ieee80211w "required"

--host is optional - if omitted, "host" is left blank ("") for you to fill in.

Radios with no "qwrap configure" command given are left as {} (untouched, per
S1/S2/S3-qwrap-config-file.py convention). Radios that ARE configured but have
no matching "qwrap client add" command still get a "clients" block filled in
with template defaults (count 28, hostnamePrefix "", ipv4 1, ipv6 1) for you
to adjust/remove as needed.
'''

import argparse
import shlex

RADIO_IDS   = (0, 1, 2)
RADIO_NAMES = {0: '2.4ghz', 1: '5ghz', 2: '6ghz'}

DEFAULT_CLIENTS = {'count': 28, 'hostnamePrefix': '', 'ipv4': 1, 'ipv6': 1}

# CLI flag -> field name (mirrors qwrap-manager.py's MISC_KEY_MAP, reversed,
# plus the extra flags handled specially in buildConfigureCmd()).
FLAG_TO_FIELD = {
    'key-mgmt':      'keyMgmt',
    'bssid':         'bssid',
    'group':         'groupCipher',
    'pairwise':      'pairwiseCipher',
    'ieee80211w':    'ieee80211w',
    'portal-type':   'portalType',
    'protocol':      'protocol',
    'channel-width': 'channelWidth',
}

# Field ordering used when printing a radio dict (matches the config file template).
FIELD_ORDER = [
    'ssid', 'security', 'passphrase', 'eapType', 'username', 'password',
    'digitalCertPath', 'privateKeyPath', 'serverCaCertPath', 'keyMgmt',
    'bssid', 'ieee80211w', 'groupCipher', 'pairwiseCipher', 'portalType',
    'protocol', 'channelWidth',
]


def parseConfigureCmd(cmd):
    '''Parse one "qwrap configure radios <id[,id...]> ..." CLI command string
    into (radioIds, paramsDict). radioIds is a list - the "radios" arg may be
    a single id (e.g. "2") or a comma-separated list (e.g. "0,1,2"), in which
    case the same params apply to all of them.'''
    tokens = shlex.split(cmd)
    if tokens[:2] != ['qwrap', 'configure'] or 'radios' not in tokens:
        raise ValueError(f'Not a "qwrap configure radios ..." command: {cmd!r}')

    idx = tokens.index('radios')
    radioIds = [int(r) for r in tokens[idx + 1].split(',')]
    params = {}
    i = idx + 2
    n = len(tokens)
    while i < n:
        flag = tokens[i]
        if flag == 'security':
            params['security'] = tokens[i + 1]
            i += 2
        elif flag == 'ssid':
            params['ssid'] = tokens[i + 1]
            i += 2
        elif flag == 'passphrase':
            params['passphrase'] = tokens[i + 1]
            i += 2
        elif flag == 'eap-type':
            eapType = tokens[i + 1]
            params['eapType'] = eapType
            i += 2
        elif flag == 'username':
            params['username'] = tokens[i + 1]
            i += 2
        elif flag == 'password':
            params['password'] = tokens[i + 1]
            i += 2
        elif flag == 'ca-cert':
            params['serverCaCertPath'] = tokens[i + 1]
            i += 2
        elif flag == 'client-cert':
            params['digitalCertPath'] = tokens[i + 1]
            i += 2
        elif flag == 'private-key':
            params['privateKeyPath'] = tokens[i + 1]
            i += 2
        elif flag in FLAG_TO_FIELD:
            params[FLAG_TO_FIELD[flag]] = tokens[i + 1]
            i += 2
        else:
            raise ValueError(f'Unrecognized token {flag!r} at position {i} in command: {cmd!r}')

    return radioIds, params


def parseClientAddCmd(cmd):
    '''Parse one "qwrap client add radios <id[,id...]> num <count> ..." CLI
    command string into (radioIds, clientsDict), mirroring
    buildAddClientsCmd(). radioIds is a list - see parseConfigureCmd().'''
    tokens = shlex.split(cmd)
    if tokens[:3] != ['qwrap', 'client', 'add'] or 'radios' not in tokens or 'num' not in tokens:
        raise ValueError(f'Not a "qwrap client add radios ... num ..." command: {cmd!r}')

    radioIds = [int(r) for r in tokens[tokens.index('radios') + 1].split(',')]
    clients = {'count': int(tokens[tokens.index('num') + 1]), 'hostnamePrefix': '', 'ipv4': 0, 'ipv6': 0}
    if 'dhcp' in tokens:
        clients['ipv4'] = 1
    if 'ipv6' in tokens:
        clients['ipv6'] = 1
    if 'hostname-prefix' in tokens:
        clients['hostnamePrefix'] = tokens[tokens.index('hostname-prefix') + 1]
    return radioIds, clients


def formatValue(value):
    if isinstance(value, str) and value.isdigit():
        return value  # e.g. channelWidth "40" -> keep as string; caller can hand-adjust to int if desired
    return f'"{value}"'


def renderFieldLines(params, pad):
    lines = []
    for field in FIELD_ORDER:
        if field in params:
            lines.append(f'{pad}"{field}":'.ljust(len(pad) + 20) + f'{formatValue(params[field])},')
    return lines


def renderClientsBlock(clients, pad):
    lines = [f'{pad}"clients": {{']
    inner = pad + '    '
    for key, cliLabel in (('count', 'count'), ('hostnamePrefix', 'hostnamePrefix'), ('ipv4', 'ipv4'), ('ipv6', 'ipv6')):
        value = clients.get(cliLabel, DEFAULT_CLIENTS[key])
        rendered = value if isinstance(value, int) else f'"{value}"'
        lines.append(f'{inner}"{key}":'.ljust(len(inner) + 18) + f'{rendered},')
    lines.append(f'{pad}}},')
    return lines


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--host', default='', help='AP host/IP for the "host" field (default: blank, fill in yourself)')
    parser.add_argument('--file', default=None, metavar='PATH',
                         help='Read commands from this file (one "qwrap ..." command per line) - use this for '
                              'multiple radios and/or configure+client-add combos')
    parser.add_argument('command', nargs='?',
                         help='A single full "qwrap configure radios ..." command string (one radio, no clients) - '
                              'use --file PATH instead for anything more than that')
    args = parser.parse_args()

    if args.file and args.command:
        parser.error('Pass either a single command OR --file PATH, not both')
    if args.file:
        with open(args.file) as f:
            commands = [line.strip() for line in f if line.strip()]
    elif args.command:
        commands = [args.command]
    else:
        parser.error('No command given - pass a single command, or --file PATH for multiple commands/clients')

    radios      = {}
    clientsById = {}
    for cmd in commands:
        if cmd.startswith('qwrap configure'):
            radioIds, params = parseConfigureCmd(cmd)
            for radioId in radioIds:
                radios[radioId] = params
        elif cmd.startswith('qwrap client add'):
            radioIds, clients = parseClientAddCmd(cmd)
            for radioId in radioIds:
                clientsById[radioId] = clients
        else:
            raise ValueError(f'Unrecognized command (expected "qwrap configure ..." or "qwrap client add ..."): {cmd!r}')

    print('    {')
    print(f'        "host": "{args.host}",')
    print('        "radios": {')
    for radioId in RADIO_IDS:
        if radioId not in radios:
            print(f'            {radioId}: {{}},  # {RADIO_NAMES[radioId]}')
            continue
        print(f'            {radioId}: {{  # {RADIO_NAMES[radioId]}')
        for line in renderFieldLines(radios[radioId], pad=' ' * 16):
            print(line)
        clients = clientsById.get(radioId, DEFAULT_CLIENTS)
        for line in renderClientsBlock(clients, pad=' ' * 16):
            print(line)
        print('            },')
    print('        },')
    print('    },')


if __name__ == '__main__':
    main()
