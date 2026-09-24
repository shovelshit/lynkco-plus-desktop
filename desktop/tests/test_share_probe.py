import copy
import json
import unittest
from unittest.mock import patch


class ShareProbeTests(unittest.TestCase):
    def setUp(self):
        self.session = {
            'token': 'private-access', 'capturedAt': 1789866000000,
            'refreshToken': 'private-refresh', 'deviceId': 'private-device',
            'expireAt': 1789867800000, 'platform': 'ANDROID',
            'deviceName': 'My phone', 'deviceModel': 'actual-model',
            'deviceBrand': 'ActualBrand', 'devicePlatform': 'android',
            'osVersion': '35', 'appVersion': '5.1', 'appBuild': '50123',
            'glDevId': 'private-gl', 'userAgent': 'captured-agent',
            'probeArticleId': 'live-article',
            'shareContext': {'capturedAt': 1789866000000, 'headers': {
                'user-agent': 'share-agent', 'os': '15', 'gl_dev_id': 'share-gl',
                'appversionname': '50124',
            }, 'security': {'geelyDeviceId': 'different-private-id', 'osVersion': '15',
                            'androidVersion': '35', 'battery': '42', 'wifiName': 'private-wifi',
                            'lbsLatitude': '', 'channel': '%E5%90%89%E5%88%A9'}},
        }
        self.secrets = {'nativeAppKey': 'private-key', 'nativeAppSecret': 'private-secret'}

    def run_probe(self, transport):
        from desktop.share_probe import run_probe
        return run_probe(self.session, self.secrets, transport=transport,
                         now=lambda: 1789866000, pause=lambda: None)

    def test_uses_captured_snapshot_and_only_gets_fresh_share_codes(self):
        before = copy.deepcopy(self.session)
        calls = []

        def transport(method, url, headers):
            self.assertEqual(method, 'GET')
            self.assertEqual(url, 'https://app-api-gw-toc.lynkco.com/app/v1/task/getShareCode')
            self.assertEqual(headers['token'], 'bearerprivate-access')
            self.assertEqual(headers['svcsid'], headers['token'])
            self.assertEqual(headers['user-agent'], 'share-agent')
            self.assertEqual(headers['gl_dev_id'], 'share-gl')
            self.assertEqual(headers['appversionname'], '50124')
            self.assertIn('live-article', json.loads(headers['risk_request_info'])['shareContentURL'])
            self.assertIn('x-ca-signature', headers)
            calls.append(headers)
            return 200, {'success': True, 'code': 200, 'data': 'private-share-code'}

        result = self.run_probe(transport)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['scope'], 'getShareCode_only')
        self.assertEqual(json.loads(calls[0]['sweet_security_info']), self.session['shareContext']['security'])
        self.assertTrue(any('sweet_security_info' not in headers for headers in calls))
        self.assertEqual(len({h['x-ca-nonce'] for h in calls}), len(calls))
        self.assertEqual(self.session, before)
        report = json.dumps(result)
        for secret in ('private-access', 'private-key', 'private-secret', 'private-gl',
                       'different-private-id', 'private-wifi', 'private-share-code', 'live-article'):
            self.assertNotIn(secret, report)
        self.assertFalse(result['provesReward'])

    def test_failed_omission_is_rechecked_against_full_baseline_and_split(self):
        def transport(method, url, headers):
            security = json.loads(headers.get('sweet_security_info', '{}'))
            if 'wifiName' not in security:
                return 200, {'success': False, 'code': 'FIELD_REQUIRED'}
            return 200, {'success': True, 'data': 'code'}
        report = self.run_probe(transport)
        wifi = next(item for item in report['experiments'] if item['name'] == 'field:wifiName')
        self.assertEqual(wifi['outcome'], 'rejected_with_baseline_success')
        self.assertEqual(report['status'], 'completed')

    def test_expired_or_unavailable_baseline_does_not_claim_fields_required(self):
        calls = []
        def transport(method, url, headers):
            calls.append(headers)
            if len(calls) == 1:
                return 200, {'success': True, 'data': 'code'}
            return 401, {'code': 'expired'}
        report = self.run_probe(transport)
        self.assertEqual(report['status'], 'inconclusive')
        self.assertEqual(report['experiments'][0]['outcome'], 'inconclusive')
        self.assertEqual(len(calls), 3)

    def test_http_bad_request_with_successful_control_splits_the_failed_group(self):
        def transport(method, url, headers):
            security = json.loads(headers.get('sweet_security_info', '{}'))
            if 'wifiName' not in security:
                return 400, {'message': 'private-wifi'}
            return 200, {'success': True, 'data': 'code'}
        report = self.run_probe(transport)
        wifi = next(item for item in report['experiments'] if item['name'] == 'field:wifiName')
        self.assertEqual(wifi['outcome'], 'rejected_with_baseline_success')
        self.assertEqual(wifi['result']['httpStatus'], 400)

    def test_unsuccessful_baseline_stops_without_sending_variants(self):
        calls = []
        def transport(*args):
            calls.append(args)
            return 200, {'success': True, 'data': ''}
        report = self.run_probe(transport)
        self.assertEqual(report['status'], 'baseline_failed')
        self.assertEqual(len(calls), 1)
        self.assertEqual(report['experiments'], [])

    def test_missing_or_expired_input_fails_without_network_or_secret_exception(self):
        from desktop.share_probe import run_probe
        for mutation in ('missing-snapshot', 'expired', 'missing-article'):
            session = copy.deepcopy(self.session)
            if mutation == 'missing-snapshot': session.pop('shareContext')
            elif mutation == 'expired': session['expireAt'] = 1
            else: session.pop('probeArticleId')
            with self.subTest(mutation=mutation):
                with self.assertRaisesRegex(ValueError, '抓包'):
                    run_probe(session, self.secrets, transport=lambda *a: self.fail('network'),
                              now=lambda: 1789866000, pause=lambda: None)

    def test_transport_failure_is_sanitized(self):
        def transport(*args):
            raise RuntimeError('private-access private-key private-secret')
        report = self.run_probe(transport)
        self.assertEqual(report['baseline']['category'], 'transport')
        self.assertNotIn('private', json.dumps(report))

    def test_incomplete_and_hostile_context_is_rejected_before_network(self):
        from desktop.share_probe import run_probe
        for key in ('refreshToken', 'deviceId', 'platform', 'userAgent', 'devicePlatform', 'deviceName', 'appBuild'):
            session = copy.deepcopy(self.session)
            session.pop(key)
            with self.subTest(missing=key), self.assertRaises(ValueError):
                run_probe(session, self.secrets, transport=lambda *a: self.fail('network'), now=lambda: 1789866000)
        for security in ({'battery': {}}, {'token': 'private-access'}):
            session = copy.deepcopy(self.session)
            session['shareContext']['security'] = security
            with self.assertRaises(ValueError):
                run_probe(session, self.secrets, transport=lambda *a: self.fail('network'), now=lambda: 1789866000)

    def test_signature_matches_existing_python_request_builder(self):
        import base64
        import hashlib
        import hmac
        import time
        import uuid
        from pathlib import Path
        from desktop.share_probe import _headers, SHARE_PATH
        from email.utils import formatdate
        # Keep a small protocol-level reference here so the standalone client
        # repository does not depend on the legacy CLI repository.
        nonce = 'fixed-nonce'
        timestamp = str(1789866000 * 1000)
        date = formatdate(1789866000, usegmt=True)
        ca = {'x-ca-key': self.secrets['nativeAppKey'], 'x-ca-nonce': nonce,
              'x-ca-timestamp': timestamp}
        source = ('GET\napplication/json; charset=utf-8\n\n'
                  'application/x-www-form-urlencoded; charset=utf-8\n'
                  f'{date}\n')
        source += ''.join(f'{key}:{ca[key]}\n' for key in sorted(ca)) + SHARE_PATH
        reference = dict(ca)
        reference.update({
            'x-ca-signature-headers': 'x-ca-nonce,x-ca-key,x-ca-timestamp',
            'x-ca-signature': base64.b64encode(hmac.new(
                self.secrets['nativeAppSecret'].encode(), source.encode(), hashlib.sha256
            ).digest()).decode(),
            'date': date,
            'accept': 'application/json; charset=utf-8',
            'content-type': 'application/x-www-form-urlencoded; charset=utf-8',
        })
        with patch('uuid.uuid4', return_value='fixed-nonce'), patch('time.time', return_value=1789866000):
            actual = _headers(self.session, self.secrets, self.session['shareContext']['security'], 1789866000)
        for key in ('x-ca-key', 'x-ca-nonce', 'x-ca-timestamp', 'date', 'accept', 'content-type', 'x-ca-signature', 'x-ca-signature-headers'):
            self.assertEqual(actual[key], reference[key], key)


if __name__ == '__main__':
    unittest.main()
