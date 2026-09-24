"""Local, opt-in share-header omission experiments; never reports a share.

Run ``python -m desktop.share_probe`` while the desktop client holds a complete
capture. Only the sanitized experiment report is printed. Captured credentials
stay in process memory; the tool never writes a capture or changes cloud policy.
"""

import argparse
import base64
import copy
from datetime import datetime, timezone, timedelta
from email.utils import formatdate
import hashlib
import hmac
import json
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit, quote
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import uuid

from desktop.capture_context import capture_readiness, positive_number


SHARE_PATH = '/app/v1/task/getShareCode'
SHARE_URL = 'https://app-api-gw-toc.lynkco.com' + SHARE_PATH
GROUPS = {
    'power': ('battery', 'isCharging'),
    'environment': ('isSetProxy', 'isUsbDebug', 'isMockLocation', 'isRoot'),
    'network': ('networkType', 'ip', 'wifiName', 'wifiSignalLevel'),
    'location': ('isLbsEnabled', 'lbsLatitude', 'lbsLongitude'),
    'device_token': ('deviceToken',),
}
DEVICE_HEADERS = {
    'gl_dev_name': 'deviceName', 'gl_dev_model': 'deviceModel',
    'gl_dev_brand': 'deviceBrand', 'gl_dev_platform': 'devicePlatform',
    'gl_os_version': 'osVersion', 'gl_app_version': 'appVersion',
    'gl_app_build': 'appBuild', 'gl_dev_id': 'glDevId', 'user-agent': 'userAgent',
}
CAPTURED_HEADERS = set(DEVICE_HEADERS) | {
    'appversion', 'appversioncode', 'appversionname', 'publicplatform',
    'os', 'gl_user_id', 'risk_type',
}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _read_response(opener, request):
    try:
        response = opener.open(request, timeout=15)
    except HTTPError as error:
        response = error
    with response:
        raw = response.read(65537)
        if len(raw) > 65536:
            return response.status, None
        try:
            return response.status, json.loads(raw)
        except (ValueError, UnicodeError):
            return response.status, None


def _transport(method, url, headers):
    # Both destination and method are fixed independently of captured input.
    if method != 'GET' or url != SHARE_URL:
        raise ValueError('Unsupported probe request')
    return _read_response(build_opener(ProxyHandler({}), NoRedirect()),
                          Request(SHARE_URL, headers=headers, method='GET'))


def _headers(session, secrets, security, now):
    timestamp = str(int(now * 1000))
    ca = {'x-ca-key': secrets['nativeAppKey'], 'x-ca-nonce': str(uuid.uuid4()),
          'x-ca-timestamp': timestamp}
    accept = 'application/json; charset=utf-8'
    content_type = 'application/x-www-form-urlencoded; charset=utf-8'
    date = formatdate(now, usegmt=True)
    source = f'GET\n{accept}\n\n{content_type}\n{date}\n'
    source += ''.join(f'{key}:{ca[key]}\n' for key in sorted(ca)) + SHARE_PATH
    signature = base64.b64encode(hmac.new(secrets['nativeAppSecret'].encode(),
                                        source.encode(), hashlib.sha256).digest()).decode()
    headers = {header: session[key] for header, key in DEVICE_HEADERS.items() if key in session}
    headers.update({key: value for key, value in session['shareContext']['headers'].items()
                    if key in CAPTURED_HEADERS})
    token = session['token']
    token = token if token.startswith('bearer') else 'bearer' + token
    article = quote(session['probeArticleId'], safe='')
    article_url = ('https://h5.lynkco.com/app-h5/dist/web/pages/exploration/article/index.html'
                   f'?id={article}&isShare=lynkco%3A%2F%2Fwx%2F%3FrouteUrl%3D%2Fpages%2F'
                   f'exploration%2Farticle%2Findex.js%3Fid%3D{article}')
    risk = {'openTimeStamp': datetime.fromtimestamp(now, timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S'),
            'shareContentType': 1, 'shareContentURL': article_url}
    headers.update(ca)
    headers.update({'accept': accept, 'content-type': content_type, 'date': date,
                    'x-ca-signature-headers': 'x-ca-nonce,x-ca-key,x-ca-timestamp',
                    'x-ca-signature': signature, 'token': token, 'svcsid': token,
                    'ca_version': '1',
                    'risk_request_info': json.dumps(risk, ensure_ascii=True, separators=(',', ':'))})
    headers.setdefault('risk_type', '1')
    if security is not None:
        headers['sweet_security_info'] = json.dumps(security, ensure_ascii=True, separators=(',', ':'))
    return headers


def _summary(status, payload):
    if status == 401: category = 'credential'
    elif status == 429: category = 'rate_limit'
    elif not 200 <= status < 300: category = 'http'
    elif not isinstance(payload, dict): category = 'invalid_response'
    else:
        success = (payload.get('success') is True or
                   ('success' not in payload and str(payload.get('code')) in ('200', 'success')))
        valid_code = 'code' not in payload or str(payload['code']) in ('200', 'success', '0')
        value = payload.get('data')
        category = ('success' if success and valid_code and isinstance(value, str) and value.strip()
                    else 'business_or_empty_code')
    # Never include the upstream code/message: it could echo submitted secrets.
    return {'ok': category == 'success', 'httpStatus': status, 'category': category}


def run_probe(session, secrets, transport=None, now=time.time, pause=None):
    """Compare omissions with a true full snapshot; returned data is redacted."""
    session = copy.deepcopy(session)
    try:
        current = now() * 1000
        context = session['shareContext']
        deadline = min(session['capturedAt'] + 30 * 60 * 1000, session.get('expireAt') or float('inf'))
        readiness = capture_readiness(session)['readiness']
        valid = (all(readiness[key] for key in ('login', 'device', 'share')) and
                 positive_number(session['capturedAt']) and
                 (session.get('expireAt') is None or positive_number(session['expireAt'])) and
                 isinstance(session['probeArticleId'], str) and bool(session['probeArticleId']) and
                 current < deadline and all(isinstance(secrets.get(key), str) and secrets[key]
                                            for key in ('nativeAppKey', 'nativeAppSecret')))
    except (KeyError, TypeError):
        valid = False
    if not valid:
        raise ValueError('请先完成有效抓包，并配置本地应用签名信息')
    send = transport or _transport
    pause = pause or (lambda: time.sleep(.3))
    original = context['security']
    report = {'scope': 'getShareCode_only', 'provesReward': False,
              'platform': session.get('platform'), 'appVersion': session.get('appVersion'),
              'testedAt': int(current), 'status': 'completed', 'experiments': []}
    requests = 0
    started = time.monotonic()

    def request(security):
        nonlocal requests
        if requests >= 48 or time.monotonic() - started > 180 or now() * 1000 >= deadline:
            return {'ok': False, 'httpStatus': None, 'category': 'probe_limit'}
        if requests: pause()
        requests += 1
        try:
            status, payload = send('GET', SHARE_URL, _headers(session, secrets, security, now()))
            return _summary(status, payload)
        except Exception:
            return {'ok': False, 'httpStatus': None, 'category': 'transport'}

    report['baseline'] = request(original)
    if not report['baseline']['ok']:
        report['status'] = 'baseline_failed'
        return report

    def experiment(name, removed, omit_header=False):
        security = None if omit_header else {k: v for k, v in original.items() if k not in removed}
        result = request(security)
        item = {'name': name, 'removedFields': sorted(removed), 'result': result}
        if result['ok']:
            item['outcome'] = 'accepted_for_code_only'
        else:
            control = request(original)
            item['baselineRecheck'] = control
            deterministic_rejection = (result['category'] == 'business_or_empty_code' or
                                       (result['category'] == 'http' and result['httpStatus'] in (400, 422)))
            if control['ok'] and deterministic_rejection:
                item['outcome'] = 'rejected_with_baseline_success'
            else:
                item['outcome'] = 'inconclusive'
                report['status'] = 'inconclusive'
        report['experiments'].append(item)
        return item['outcome']

    experiment('entire_security_header', list(original), omit_header=True)
    for name, keys in GROUPS.items():
        if report['status'] != 'completed': break
        present = [key for key in keys if key in original]
        if not present: continue
        outcome = experiment('group:' + name, present)
        if outcome == 'rejected_with_baseline_success':
            for key in present:
                if report['status'] != 'completed': break
                experiment('field:' + key, [key])
    if report['status'] == 'completed':
        report['finalBaseline'] = request(original)
        if not report['finalBaseline']['ok']:
            report['status'] = 'inconclusive'
    report['requestCount'] = requests
    return report


def load_live_capture(instance_file):
    """Read the protected loopback-only source without printing its URL/token."""
    try:
        record = json.loads(Path(instance_file).read_text())
        url = urlsplit(record['url'])
        if (url.scheme != 'http' or url.hostname != '127.0.0.1' or not url.port or
                url.username or url.password or url.path not in ('', '/') or url.query or not url.fragment):
            raise ValueError()
        endpoint = f'http://127.0.0.1:{url.port}/api/capture/probe-input'
        status, payload = _read_response(build_opener(ProxyHandler({}), NoRedirect()),
                                        Request(endpoint, headers={'Authorization': 'Bearer ' + url.fragment}))
        if status != 200 or not isinstance(payload, dict) or payload.get('ok') is not True:
            raise ValueError()
        return payload['data']
    except (OSError, ValueError, KeyError, TypeError):
        raise ValueError('请启动本地客户端，并完成登录态、设备信息和分享信息抓包') from None


def main():
    from desktop.launcher import state_directory
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--instance-file', type=Path, default=state_directory() / 'instance.json')
    parser.add_argument('--config', type=Path, default=Path(__file__).resolve().parent.parent / 'LynkCoHelper' / 'env.json')
    args = parser.parse_args()
    try:
        session = load_live_capture(args.instance_file)
        try:
            secrets = json.loads(args.config.read_text()).get('secrets', {})
        except (OSError, ValueError, AttributeError):
            raise ValueError('本地应用签名配置无法读取') from None
        report = run_probe(session, secrets)
    except ValueError as error:
        print(json.dumps({'status': 'capture_required', 'message': str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['status'] == 'completed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
