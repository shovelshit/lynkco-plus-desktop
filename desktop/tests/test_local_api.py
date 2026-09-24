import http.client
import importlib.util
import json
import threading
import time
import unittest
from unittest.mock import Mock, patch
from pathlib import Path

from test_binding import FakeCloud, MemoryStore, SESSION
from desktop.binding import Controller


class LocalAPITests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('desktop.local_api'), 'local API must exist')
        from desktop.local_api import make_server
        self.controller = Controller(FakeCloud(), MemoryStore())
        self.server = make_server(self.controller, Path('desktop/web'), 'callback-secret')
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = 'http://127.0.0.1:' + str(self.server.server_port)
        self.controller.claim('claim-token_123456')
        self.browser_token = self.server.issue_browser_session()

    def tearDown(self):
        if hasattr(self, 'server'):
            self.server.shutdown()
            self.server.server_close()
            self.thread.join()

    def request(self, path, token=None, origin=None, host=None, body=None):
        token = self.browser_token if token is None else token
        client = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=2)
        headers = {'Authorization': 'Bearer ' + token}
        if origin:
            headers['Origin'] = origin
        if host:
            headers['Host'] = host
        if body is not None:
            headers['Content-Type'] = 'application/json'
        client.request('POST' if body is not None else 'GET', path, json.dumps(body) if body is not None else None, headers)
        response = client.getresponse()
        result = response.status, response.read()
        client.close()
        return result

    def wait_for_capture_stage(self, stage):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            state = self.controller.public_state()
            if state['capture']['stage'] == stage:
                return state
            time.sleep(.01)
        self.fail(f'capture did not reach {stage}: {self.controller.public_state()}')

    def test_status_requires_token(self):
        self.assertEqual(self.request('/api/status', token='')[0], 401)
        self.assertEqual(self.request('/api/status')[0], 200)

    def test_browser_session_requires_unlock_and_old_token_stops_after_lock(self):
        self.controller.lock_identity()
        self.server.lock_browser_session()
        with self.assertRaises(ValueError):
            self.server.issue_browser_session()
        self.assertEqual(self.request('/api/status')[0], 401)
        self.controller.unlock('recovery-secret')
        first = self.server.issue_browser_session()
        self.assertEqual(self.request('/api/status', token=first)[0], 200)
        self.assertEqual(self.request('/api/activity', token=first, body={})[0], 200)
        self.controller.lock_identity()
        self.server.lock_browser_session()
        self.assertEqual(self.request('/api/status', token=first)[0], 401)
        self.controller.unlock('recovery-secret')
        second = self.server.issue_browser_session()
        self.assertNotEqual(first, second)
        self.assertEqual(self.request('/api/status', token=first)[0], 401)
        self.assertEqual(self.request('/api/status', token=second)[0], 200)

    def test_access_token_endpoint_is_one_time_and_does_not_put_token_in_status(self):
        self.controller.receive_capture(SESSION)
        status, body = self.request('/api/capture/access-token-once', body={})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['data']['accessToken'], SESSION['token'])
        status, _ = self.request('/api/capture/access-token-once', body={})
        self.assertEqual(status, 400)
        status, body = self.request('/api/status')
        self.assertNotIn(SESSION['token'], body.decode())

    def test_new_capture_gets_a_new_one_time_access_token(self):
        first = {**SESSION, 'token': 'first-access'}
        second = {**SESSION, 'token': 'second-access', 'refreshToken': 'second-refresh'}
        self.controller.receive_capture(first)
        status, body = self.request('/api/capture/access-token-once', body={})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['data']['accessToken'], 'first-access')
        self.controller.receive_capture(second)
        status, body = self.request('/api/capture/access-token-once', body={})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['data']['accessToken'], 'second-access')

    def test_probe_input_is_authenticated_complete_and_transient(self):
        auth = {key: value for key, value in SESSION.items() if key != 'shareContext'}
        self.controller.receive_capture(auth)
        self.controller.receive_share_capture({'token': SESSION['token'], 'shareContext': SESSION['shareContext'], 'probeArticleId': 'article-1'})
        status, body = self.request('/api/capture/probe-input')
        self.assertEqual(status, 200)
        payload = json.loads(body)['data']
        self.assertEqual(payload['probeArticleId'], 'article-1')
        self.assertEqual(payload['shareContext']['headers']['user-agent'], 'Lynkco/4.2.7')
        self.controller.receive_capture({**{key: value for key, value in SESSION.items() if key != 'shareContext'}, 'token': 'new-access'})
        self.assertEqual(self.request('/api/capture/probe-input')[0], 400)

    def test_network_refresh_is_read_only_for_running_proxy(self):
        import desktop.proxy
        self.controller.proxy = Mock()
        self.controller.proxy.public_state.return_value = {'running': True}
        with patch('desktop.proxy.network_addresses', return_value=[{'name': 'Wi-Fi', 'address': '192.0.2.10'}]):
            status, body = self.request('/api/networks')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['data'], [{'name': 'Wi-Fi', 'address': '192.0.2.10'}])
        self.controller.proxy.stop.assert_not_called()

    def test_capture_qr_rejects_after_network_invalidation(self):
        self.controller.proxy = Mock()
        self.controller.proxy.public_state.return_value = {
            'running': False, 'paired': False, 'pairUrl': None,
            'captureEnabled': False, 'networkChanged': True, 'needsReconfigure': True,
        }
        status, body = self.request('/api/capture/qr')
        self.assertEqual(status, 400)
        self.assertIn('网络已变化', json.loads(body)['error'])

    def test_static_resources_are_never_cached(self):
        client = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=2)
        for path in ('/', '/app.js', '/style.css'):
            client.request('GET', path)
            response = client.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader('Cache-Control'), 'no-store')
            self.assertIn("img-src 'self' data: blob: https:", response.getheader('Content-Security-Policy'))
            response.read()
        client.close()

    def test_static_resources_load_before_fragment_token_is_available(self):
        client = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=2)
        client.request('GET', '/')
        response = client.getresponse()
        self.assertEqual(response.status, 200)
        self.assertIn(b'<title>', response.read())
        client.close()

    def test_browser_claim_link_route_is_not_available(self):
        status, body = self.request('/api/claim', body={'claimUrl': 'https://lynkco.ltools.asia/claim/claim-token_123456'})
        self.assertEqual(status, 404)

    def test_browser_claim_and_recovery_routes_are_not_available(self):
        status, body = self.request('/api/claim', body={'claimCode': 'claim-token_123456'})
        self.assertEqual(status, 404)
        self.assertEqual(self.request('/api/recover', body={'loginCode': 'recovery-secret'})[0], 404)

    def test_capture_reset_route_clears_stale_replacement_state(self):
        self.controller.receive_capture(SESSION)
        status, body = self.request('/api/capture/reset', body={})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)['data']['reset'])
        self.assertEqual(self.controller.public_state()['capture']['stage'], 'idle')

    def test_browser_cannot_clear_native_identity(self):
        status, body = self.request('/api/identity/clear', body={})
        self.assertEqual(status, 404)
        self.assertTrue(self.controller.public_state()['hasIdentity'])

    def test_browser_cannot_unlock_native_identity(self):
        self.controller.lock_identity()
        self.server.lock_browser_session()
        self.assertEqual(self.request('/api/identity/retry', body={})[0], 401)
        self.assertFalse(self.controller.public_state()['hasIdentity'])

    def test_force_stop_route_stops_desktop_proxy_without_phone_confirmation(self):
        self.controller.proxy = Mock()
        self.controller.proxy.public_state.side_effect = [{'running': True}, {'running': False}]
        status, body = self.request('/api/capture/force-stop', body={})

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['data']['stopped'], True)
        self.assertTrue(json.loads(body)['data']['phoneMustDisable'])
        self.controller.proxy.stop.assert_called_once_with()

    def test_force_stop_route_only_warns_when_proxy_was_running(self):
        self.controller.proxy = Mock()
        self.controller.proxy.public_state.return_value = {'running': False}
        status, body = self.request('/api/capture/force-stop', body={})
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)['data']['phoneMustDisable'])

    def test_run_route_forwards_independent_task_mode(self):
        status, body = self.request('/api/binding/run', body={'mode': 'share'})
        self.assertEqual(status, 200)
        self.assertIn(('POST', '/v1/binding/runs', {'mode': 'share'}), self.controller.cloud.calls)

    @unittest.skip('replaced by explicit /api/candidates/prepare confirmation')
    def test_retry_route_restarts_failed_verification_without_resending_credentials(self):
        self.controller.claim('claim-token_123456')
        self.controller.cloud.prepare_error = ValueError('raw token=mobile-token-secret')
        self.assertTrue(self.controller.receive_capture(SESSION))
        failed = self.wait_for_capture_stage('verification_failed')
        self.assertEqual(failed['capture']['verificationError'], '个人信息验证暂时失败，请稍后重试')
        self.controller.cloud.prepare_error = None
        status, body = self.request('/api/candidates/retry', body={})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)['data']['started'])
        self.wait_for_capture_stage('verified')

    @unittest.skip('no background verification after capture')
    def test_stop_route_prevents_a_delayed_verification_from_reappearing(self):
        self.controller.claim('claim-token_123456')
        self.controller.proxy = Mock()
        self.controller.cloud.prepare_release.clear()
        self.assertTrue(self.controller.receive_capture(SESSION))
        self.assertTrue(self.controller.cloud.prepare_started.wait(.5))
        status, body = self.request('/api/capture/stop', body={'proxyRemoved': True})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)['data']['stopped'])
        self.controller.cloud.prepare_release.set()
        time.sleep(.05)
        self.assertEqual(self.controller.public_state()['capture']['stage'], 'idle')

    def test_notification_test_route_calls_cloud(self):
        status, body = self.request('/api/binding/notification-test', body={})
        self.assertEqual(status, 200)
        self.assertIsNone(json.loads(body)['data'])
        self.assertIn(('POST', '/v1/binding/notifications/test', {}), self.controller.cloud.calls)

    def test_startup_never_uses_reverse_dns(self):
        from desktop.local_api import make_server
        with patch('socket.getfqdn', side_effect=AssertionError('unexpected DNS dependency')):
            server = make_server(self.controller, Path('desktop/web'), 'callback')
            server.server_close()

    def test_host_and_origin_are_checked(self):
        self.assertEqual(self.request('/api/status', host='evil.example')[0], 403)
        self.assertEqual(self.request('/api/status', origin='https://evil.example')[0], 403)
        self.assertEqual(self.request('/api/status', origin=self.origin)[0], 200)

    def test_internal_callback_has_separate_authorization(self):
        self.assertEqual(self.request('/internal/capture', body={'session': SESSION})[0], 401)

    def test_rejected_post_does_not_poison_keepalive_connection(self):
        client = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=2)
        headers = {'Authorization': 'Bearer ' + self.browser_token, 'Content-Type': 'application/json'}
        client.request('POST', '/internal/capture', json.dumps({'session': SESSION}), headers)
        self.assertEqual(client.getresponse().status, 401)
        client.request('GET', '/api/status', headers={'Authorization': 'Bearer ' + self.browser_token})
        self.assertEqual(client.getresponse().status, 200)
        client.close()

    def test_traversal_does_not_serve_local_files(self):
        for path in ['/../capture.py', '/%2e%2e/capture.py', '/env.json', '/desktop/capture.py']:
            self.assertEqual(self.request(path)[0], 404)

    def test_capture_secrets_are_not_in_status(self):
        self.controller.receive_capture(SESSION)
        status, body = self.request('/api/status')
        self.assertEqual(status, 200)
        self.assertNotIn(b'mobile-token-secret', body)
        self.assertNotIn(b'mobile-refresh-secret', body)

    def test_diagnostics_are_bounded_and_strip_unknown_values(self):
        summary = {'host': 'app-services.lynkco.com.cn', 'path': '/auth/login/refresh',
                   'status': 200, 'outcome': 'captured', 'token': 'secret-value',
                   'fields': {'token': True, 'refreshToken': 'secret-value', 'deviceId': True}}
        for _ in range(205):
            self.controller.record_capture(summary)
        status, body = self.request('/api/status')
        self.assertEqual(status, 200)
        self.assertNotIn(b'secret-value', body)
        events = json.loads(body)['data']['capture']['events']
        self.assertEqual(len(events), 200)
        self.assertFalse(events[0]['fields']['refreshToken'])

    def test_diagnostics_callback_requires_paired_peer(self):
        from unittest.mock import Mock
        self.controller.proxy = Mock()
        self.controller.proxy.accepts_peer.return_value = False
        self.assertEqual(self.request('/internal/capture', token='callback-secret', body={'peer': 'other'})[0], 403)
        self.assertEqual(self.controller.capture_events, [])

    def test_vehicle_callback_accepts_paired_epoch_without_exposing_vin(self):
        self.controller.proxy = Mock()
        self.controller.proxy.accepts_peer.return_value = True
        self.controller.proxy.public_state.return_value = {'running': True}
        self.controller.begin_capture('IOS', 'current-epoch')
        event = {'peer': '192.0.2.4', 'proxyEpoch': 'current-epoch',
                 'vehicle': {'vin': 'L1234567890123456'}}
        status, body = self.request('/internal/capture', token='callback-secret', body=event)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)['data']['accepted'])
        status, body = self.request('/api/status')
        self.assertEqual(status, 200)
        self.assertNotIn(b'L1234567890123456', body)
        self.assertFalse(json.loads(body)['data']['capture']['readiness']['vehicle'])
        self.controller.receive_capture(SESSION, proxy_epoch='current-epoch')
        status, body = self.request('/api/status')
        self.assertTrue(json.loads(body)['data']['capture']['readiness']['vehicle'])
        self.assertNotIn(b'L1234567890123456', body)
        event['proxyEpoch'] = 'previous-epoch'
        event['vehicle'] = {'vin': 'L9999999999999999'}
        status, body = self.request('/internal/capture', token='callback-secret', body=event)
        self.assertFalse(json.loads(body)['data']['accepted'])


if __name__ == '__main__':
    unittest.main()
