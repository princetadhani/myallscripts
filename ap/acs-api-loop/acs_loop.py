#!/usr/bin/env python3
"""Discover radios at startup, then trigger ACS every 300 seconds.

Run: python3 acs_loop.py discovery.json [--dry-run] [--once]
Authentication: set API_KEY_ID and API_KEY_VALUE below.
"""
import argparse
import json
import logging
import time
from urllib.parse import quote, urlencode

import requests


# Enter your WM API credentials here. These values are never printed.
API_KEY_ID = 'KEY-ATN13-64'
API_KEY_VALUE = '3ec879ec685ae75402694d37664aae6f'


class WmSession(requests.Session):
    """Standalone adaptation of SWAT CueWm session creation and _apiSend.

    Keep the cloud login body, CID scoping, and JSON request headers from SWAT.
    requests.Session handles cookie storage and forwarding without SWAT imports.
    """

    def __init__(self, base, key_id, key_value, timeout, lifetime, customer_id=None):
        super().__init__()
        self.headers.update({'Content-Type': 'application/json', 'Accept': 'application/json'})
        self.login_url = base + '/wifi/api/session'
        if customer_id:
            self.login_url += '?' + urlencode({'CID': customer_id})
        self.key_id = key_id
        self.key_value = key_value
        self.request_timeout = timeout
        self.lifetime = lifetime
        self.logged_in = False

    def login(self):
        logging.info('Creating WM API session')
        self.cookies.clear()
        response = super().request(
            'POST', self.login_url, json={
                'type': 'apikeycredentials', 'clientIdentifier': 'SWAT-Client',
                'timeout': self.lifetime, 'fetchClusterChildrenData': True,
                'keyId': self.key_id, 'keyValue': self.key_value,
            }, timeout=self.request_timeout, allow_redirects=False)
        logging.info('Login response: HTTP %d', response.status_code)
        if not 200 <= response.status_code < 300:
            raise RuntimeError(f'API-key login returned HTTP {response.status_code}')
        if not self.cookies:
            raise RuntimeError('API-key login returned no session cookies')
        self.logged_in = True

    def request(self, method, url, **kwargs):
        response = super().request(method, url, **kwargs)
        if response.status_code == 401:
            response.close()
            logging.info('Session rejected (401); logging in and retrying once')
            self.login()
            response = super().request(method, url, **kwargs)
        return response

    def __exit__(self, *args):
        try:
            if self.logged_in:
                try:
                    response = super().request('DELETE', self.login_url,
                                              timeout=self.request_timeout,
                                              allow_redirects=False)
                    if not 200 <= response.status_code < 300:
                        logging.warning('Logout returned HTTP %d', response.status_code)
                except requests.RequestException as exc:
                    logging.warning('Logout failed: %s', type(exc).__name__)
        finally:
            super().__exit__(*args)


def targets(radios, base, query):
    """Build a unique list using the ACS configuration ID, not radioId."""
    result = {}
    for radio in radios:
        mac = radio['apMac']
        config_id = int(radio['radioConfigId'])
        location = int(radio['locationId']['id'])
        if config_id <= 0 or location < 0:
            raise ValueError('Invalid radio configuration or location ID')
        params = {'action': 'ACS', 'locationid': location,
                  'nodeid': query['nodeid'], 'CID': query['CID']}
        url = (f'{base}/wifi/api/manageddevices/aps/{quote(mac, safe="")}'
               f'/radios/{config_id}?{urlencode(params)}')
        result[(mac.upper(), config_id)] = url
    return list(result.values())


def discover(session, base, path, query, timeout):
    # Discovery runs only at startup. Pagination is part of that discovery.
    query = dict(query, startindex=0)
    page_size = int(query['pagesize'])
    if page_size <= 0:
        raise ValueError('pagesize must be positive')
    radios = []
    while True:
        response = session.get(base + path, params=query, timeout=timeout,
                               allow_redirects=False)
        if response.status_code != 200:
            raise RuntimeError(f'Discovery returned HTTP {response.status_code}')
        data = response.json()
        page = data['managedWifiRadios']
        radios.extend(page)
        total = data.get('totalCount')
        if total is not None and len(radios) >= int(total):
            break
        if not page:
            if total is not None and len(radios) < int(total):
                raise RuntimeError('Discovery ended before totalCount was reached')
            break
        if total is None and not data.get('nextLink') and len(page) < page_size:
            break
        query['startindex'] += len(page)
    return targets(radios, base, query)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config', help='Firefox GET request JSON')
    parser.add_argument('--interval', type=float, default=300)
    parser.add_argument('--timeout', type=float, default=30)
    parser.add_argument('--session-timeout', type=int, default=18000,
                        help='Requested server session lifetime in seconds (default: 18000)')
    parser.add_argument('--dry-run', action='store_true', help='Discover and print PUT URLs only')
    parser.add_argument('--once', action='store_true', help='Send a single ACS round and exit')
    args = parser.parse_args()
    if args.interval <= 0 or args.timeout <= 0 or args.session_timeout <= 0:
        parser.error('interval, timeout, and session-timeout must be positive')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    with open(args.config) as source:
        config = json.load(source)['GET']
    if config['scheme'] != 'https' or '/' in config['host']:
        raise ValueError('Expected an HTTPS host without a path')
    base = 'https://' + config['host']
    if not API_KEY_ID.strip() or not API_KEY_VALUE.strip():
        raise ValueError('Set API_KEY_ID and API_KEY_VALUE at the top of acs_loop.py')
    with WmSession(base, API_KEY_ID.strip(), API_KEY_VALUE.strip(), args.timeout,
                   args.session_timeout, config['query']['CID']) as session:
        session.login()
        urls = discover(session, base, config['filename'], config['query'], args.timeout)
        logging.info('Discovered %d unique radios; target list is fixed until restart', len(urls))
        for url in urls:
            print('PUT ' + url, flush=True)
        if args.dry_run or not urls:
            return
        while True:
            started = time.monotonic()
            failures = 0
            for url in urls:
                request_start = time.monotonic()
                try:
                    response = session.put(url, data=b'', timeout=args.timeout,
                                           allow_redirects=False)
                    logging.info('HTTP %d %.3fs %s', response.status_code,
                                 time.monotonic() - request_start, url)
                    if response.status_code in (401, 403) or 300 <= response.status_code < 400:
                        raise RuntimeError('Authentication/access failure or redirect; check credentials and permissions')
                    if response.status_code != 204:
                        failures += 1
                except requests.RequestException as exc:
                    failures += 1
                    logging.error('%s %s', type(exc).__name__, url)
            logging.info('Round complete: %d requests, %d failures', len(urls), failures)
            if args.once:
                if failures:
                    raise RuntimeError('One or more ACS requests failed')
                return
            # No overlapping rounds or automatic retries of timed-out ACS actions.
            time.sleep(max(0, args.interval - (time.monotonic() - started)))


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nStopped.')
    except (ValueError, KeyError, OSError, RuntimeError, requests.RequestException) as error:
        logging.error('%s', error)
        raise SystemExit(1)
