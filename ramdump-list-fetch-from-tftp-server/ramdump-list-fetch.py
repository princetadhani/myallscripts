#!/usr/bin/env python3
'''
Ramdump List Fetcher (TFTP backup_panic server)

Lists ramdump (.zip) file links for given AP MAC address(es) found on the
TFTP backup_panic directory listing, filtered to a recent time window
(default: past 2 days).

Ramdump files look like:  E01CA7C2755F_20260921122244.zip
                           <MAC-no-colon>_<YYYYMMDDHHMMSS>.zip

Targets APs from AP_LIST below by default, or from --ap MAC[,MAC...]
if given. --ap accepts ANY MAC (no dependency on AP_LIST — an AP does
not need to be present in AP_LIST to be targeted via --ap).

Usage:
  ./ramdump-list-fetch.py --ap E0:1C:A7:20:5D:3F
  ./ramdump-list-fetch.py --ap E01CA7205D3F,30B62D00978F
  ./ramdump-list-fetch.py --ap E01CA7205D3F --time "1 day"
  ./ramdump-list-fetch.py --ap E01CA7205D3F --debug
'''

import argparse
import logging
import re
import sys
from datetime import datetime, timedelta

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- Constants & Configuration ---
TFTP_LISTING_URL = 'https://10.86.130.130/tftp/backup_panic/?C=M;O=D'
TFTP_BASE_URL     = 'https://10.86.130.130/tftp/backup_panic/'
FILENAME_RE       = re.compile(r'^([0-9A-Fa-f]{12})_(\d{14})\.zip$')

# ---------------------------------------------------------------------------
# AP MAC List  –  edit this list to target different APs
# ---------------------------------------------------------------------------
AP_LIST: list[str] = [
# 'E0:1C:A7:20:5D:3F',
# '30:B6:2D:00:97:8F'
]

logging.basicConfig(level=logging.INFO, format='%(asctime)s  %(levelname)-9s%(message)s', datefmt='%H:%M:%S')
log = logging.getLogger('ramdump-list-fetch')

# --- Helpers ---
def normalizeMac(mac):
    '''Accept MAC with or without colons, return upper-case, colon-free form.'''
    return mac.strip().replace(':', '').replace('-', '').upper()

def parseTimeWindow(text):
    '''Parse a duration string like "1 day", "2 days", "12 hours" into a
    timedelta. Bare numbers are treated as days. Defaults to 2 days.'''
    if not text:
        return timedelta(days=2)
    match = re.match(r'^\s*(\d+)\s*(day|days|d|hour|hours|hr|hrs|h)?\s*$', text, re.IGNORECASE)
    if not match:
        raise ValueError(f'Could not parse --time value: {text!r}')
    amount = int(match.group(1))
    unit = (match.group(2) or 'day').lower()
    if unit.startswith('h'):
        return timedelta(hours=amount)
    return timedelta(days=amount)

def fetchListing(url):
    '''Fetch the TFTP directory listing HTML.'''
    log.info('Fetching directory listing from %s', url)
    response = requests.get(url, verify=False, timeout=30)
    response.raise_for_status()
    return response.text

ROW_RE = re.compile(
    r'href="([^"]+\.zip)"[^<]*</a>\s*</td>\s*<td[^>]*>\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})'
)

def parseRamdumps(html):
    '''Extract (mac, timestamp, filename, lastModified) for every ramdump
    .zip entry. "timestamp" is parsed from the filename (AP initiated time);
    "lastModified" is the server directory-listing column (upload-complete
    time) — the two can differ for large files that take time to transfer.'''
    entries = []
    for filename, lastModified in ROW_RE.findall(html):
        m = FILENAME_RE.match(filename)
        if not m:
            continue
        mac, tsRaw = m.group(1).upper(), m.group(2)
        try:
            ts = datetime.strptime(tsRaw, '%Y%m%d%H%M%S')
        except ValueError:
            continue
        entries.append((mac, ts, filename, lastModified))
    return entries

def findRamdumps(entries, mac, since):
    '''Return matching entries for mac with timestamp >= since, newest first.'''
    matches = [e for e in entries if e[0] == mac and e[1] >= since]
    return sorted(matches, key=lambda e: e[1], reverse=True)

def resolveApList(args):
    '''Prefer --ap if given (accepts any MAC, no AP_LIST membership check);
    otherwise fall back to AP_LIST defined in this file.'''
    normalizedApList = [normalizeMac(m) for m in AP_LIST]
    if args.ap:
        macList = [normalizeMac(m) for m in args.ap.split(',') if m.strip()]
        for mac in macList:
            if mac not in normalizedApList:
                log.warning(f'{mac} not present in AP_LIST — proceeding anyway')
        log.info(f'Targeting {len(macList)} AP(s) via --ap: {macList}')
        return macList
    if normalizedApList:
        log.info(f'Targeting {len(normalizedApList)} AP(s) from AP_LIST: {normalizedApList}')
        return normalizedApList
    log.error('No APs to target — pass --ap MAC[,MAC...] or populate AP_LIST in this file.')
    sys.exit(1)

# --- CLI and Arguments ---
def parseArgs():
    parser = argparse.ArgumentParser(
        description='List ramdump links for AP MAC address(es) from the TFTP backup_panic server.'
    )
    parser.add_argument('--ap', metavar='MAC[,MAC...]',
                         help='Comma-separated list of AP MAC addresses (defaults to AP_LIST in this file)')
    parser.add_argument('--time', default='2 days',
                         help='How far back to look, e.g. "1 day", "12 hours" (default: 2 days)')
    parser.add_argument('--debug', action='store_true', help='Enable debug logging')
    return parser.parse_args()

def main():
    args = parseArgs()
    if args.debug:
        log.setLevel(logging.DEBUG)

    print('-' * 40)
    print('Parse Input MAC(s)')
    print('-' * 40)
    macList = resolveApList(args)
    timeWindow = parseTimeWindow(args.time)
    since = datetime.now() - timeWindow
    log.info('Target MAC(s): %s', ', '.join(macList))
    log.info('Time window: past %s (since %s)', args.time, since.strftime('%Y-%m-%d %H:%M:%S'))

    print('\n' + '-' * 40)
    print('Fetch TFTP Listing')
    print('-' * 40)
    html = fetchListing(TFTP_LISTING_URL)
    entries = parseRamdumps(html)
    log.info('Parsed %d ramdump entr(y/ies) from listing', len(entries))

    print('\n' + '-' * 40)
    print('Match Ramdumps Per MAC')
    print('-' * 40)
    summary = {}
    for mac in macList:
        matches = findRamdumps(entries, mac, since)
        summary[mac] = matches
        if not matches:
            log.warning('%s: no ramdump found in past %s', mac, args.time)
            continue
        log.info('[%s] Found %d ramdump(s) in past %s', mac, len(matches), args.time)
        for _, ts, filename, lastModified in matches:
            log.info('[%s] [AP initiated ramdump: %s] [last modified/completed time: %s] -> %s',
                     mac, ts.strftime('%Y-%m-%d %H:%M:%S'), lastModified, TFTP_BASE_URL + filename)

    print('\n' + '-' * 40)
    print('Summary')
    print('-' * 40)
    for mac in sorted(summary):
        matches = summary[mac]
        if matches:
            log.info('%s: latest ramdump %s', mac, TFTP_BASE_URL + matches[0][2])
        else:
            log.error('%s: no ramdump found in past %s', mac, args.time)

if __name__ == '__main__':
    main()
