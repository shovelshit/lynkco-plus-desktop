import json
import io
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch
from urllib.error import URLError
from desktop.cloud_client import CloudClient, CloudError


class CloudClientTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.response = {'ok': True, 'data': {'value': 1}}
        self.redirect = False
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                owner.requests.append(dict(self.headers))
                self.rfile.read(int(self.headers.get('Content-Length', '0')))
                self.send_response(302 if owner.redirect else 200)
                self.send_header('Content-Type', 'application/json')
                if owner.redirect:
                    self.send_header('Location', '/v1/stolen')
                self.end_headers()
                self.wfile.write(json.dumps(owner.response).encode())

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = CloudClient('http://127.0.0.1:' + str(self.server.server_port))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_failed_retry_reuses_idempotency_key(self):
        self.response = {'ok': False, 'error': {'code': 'CONFLICT', 'message': 'secret-raw-upstream'}}
        for _ in range(2):
            with self.assertRaises(CloudError) as caught:
                self.client.request('POST', '/v1/owners', {'inviteCode': 'fixture'})
            self.assertNotIn('secret-raw-upstream', str(caught.exception))
        self.assertEqual(self.requests[0]['Idempotency-Key'], self.requests[1]['Idempotency-Key'])

    def test_successful_operations_get_new_keys(self):
        for _ in range(2):
            self.client.request('POST', '/v1/owners', {'inviteCode': 'fixture'})
        self.assertNotEqual(self.requests[0]['Idempotency-Key'], self.requests[1]['Idempotency-Key'])

    def test_request_identifies_the_desktop_application(self):
        self.client.request('POST', '/v1/owners', {'inviteCode':'fixture'})
        self.assertEqual(self.requests[0].get('User-Agent'), 'LynkCoHelper/0.1 (Desktop)')

    def test_missing_claim_batch_explains_environment_mismatch(self):
        self.response = {'ok': False, 'error': {'code': 'NOT_FOUND', 'message': 'The requested record does not exist.'}}
        with self.assertRaises(CloudError) as caught:
            self.client.request('POST', '/v1/claim/claim-token_123456', {})
        self.assertEqual(caught.exception.code, 'NOT_FOUND')
        self.assertIn('当前云端环境', str(caught.exception))

    def test_recovery_unauthorized_explains_expired_reset_code(self):
        self.response = {'ok': False, 'error': {'code': 'UNAUTHORIZED', 'message': 'Management credentials are missing or invalid.'}}
        with self.assertRaises(CloudError) as caught:
            self.client.request('POST', '/v1/users/recover', {'recoveryToken': 'fixture'})
        self.assertEqual(caught.exception.code, 'UNAUTHORIZED')
        self.assertIn('重置码无效或已过期', str(caught.exception))

    def test_http_remote_and_embedded_credentials_rejected(self):
        for url in ['http://example.com', 'https://user:password@example.com', 'https://example.com/redirect', 'https://example.com#token']:
            with self.assertRaises(ValueError):
                CloudClient(url)

    def test_redirect_never_receives_bearer(self):
        self.redirect = True
        self.response = {}
        with self.assertRaises(CloudError):
            self.client.request('POST', '/v1/owners', {}, 'secret')
        self.assertEqual(len(self.requests), 1)

    def test_remote_request_retries_direct_when_system_proxy_cannot_connect(self):
        client = CloudClient('https://cloud.example')
        client.opener = Mock()
        client.opener.open.side_effect = URLError('proxy unavailable')
        client.direct_opener = Mock()
        client.direct_opener.open.return_value = io.BytesIO(b'{"ok":true,"data":{"value":1}}')

        self.assertEqual(client.request('GET', '/health'), {'value': 1})

        client.opener.open.assert_called_once()
        client.direct_opener.open.assert_called_once()

    def test_proxy_and_direct_fallback_share_one_remaining_budget(self):
        client = CloudClient('https://cloud.example')
        client.opener = Mock()
        client.direct_opener = Mock()
        observed = []

        def proxy_open(request, timeout):
            observed.append(('proxy', timeout))
            time.sleep(0.02)
            raise URLError('proxy unavailable')

        def direct_open(request, timeout):
            observed.append(('direct', timeout))
            return io.BytesIO(b'{"ok":true,"data":{"value":1}}')

        client.opener.open.side_effect = proxy_open
        client.direct_opener.open.side_effect = direct_open

        self.assertEqual(client.request('GET', '/health'), {'value': 1})
        self.assertEqual([item[0] for item in observed], ['proxy', 'direct'])
        self.assertLess(observed[1][1], observed[0][1])
        self.assertLessEqual(observed[0][1], 35)

    def test_read_timeout_does_not_extend_the_shared_budget(self):
        client = CloudClient('https://cloud.example')
        client.loopback = True
        client.opener = Mock()
        client.direct_opener = Mock()
        started = threading.Event()

        class SlowResponse(io.BytesIO):
            def read(self, *args, **kwargs):
                started.set()
                time.sleep(0.5)
                return super().read(*args, **kwargs)

        client.opener.open.return_value = SlowResponse(b'{"ok":true,"data":{}}')
        client._timeout_budget = lambda method, path: 0.02
        started_at = time.monotonic()
        with self.assertRaises(CloudError) as caught:
            client.request('GET', '/health')
        self.assertEqual(caught.exception.code, 'NETWORK')
        self.assertLess(time.monotonic() - started_at, 0.25)

    def test_run_requests_get_the_longer_but_single_budget(self):
        client = CloudClient('https://cloud.example')
        client.opener = Mock()
        client.opener.open.return_value = io.BytesIO(b'{"ok":true,"data":{}}')
        client.request('POST', '/v1/binding/runs', {})
        timeout = client.opener.open.call_args.kwargs['timeout']
        self.assertLess(timeout, 120.001)
        self.assertGreater(timeout, 119)

    def test_caller_deadline_shortens_an_individual_request_budget(self):
        client = CloudClient('https://cloud.example')
        client.opener = Mock()
        client.opener.open.return_value = io.BytesIO(b'{"ok":true,"data":{}}')
        client.request('GET', '/health', deadline=time.monotonic() + 0.02)
        timeout = client.opener.open.call_args.kwargs['timeout']
        self.assertGreater(timeout, 0)
        self.assertLess(timeout, 0.021)

    @patch('desktop.cloud_client.build_opener')
    @patch('desktop.cloud_client.ssl.create_default_context')
    @patch('desktop.cloud_client.certifi.where', return_value='/bundled/cacert.pem')
    def test_https_uses_the_bundled_ca_bundle(self, certifi_where, create_context, build_opener):
        context = Mock()
        create_context.return_value = context

        CloudClient('https://cloud.example')

        create_context.assert_called_once_with(cafile='/bundled/cacert.pem')
        self.assertEqual(build_opener.call_count, 2)
        for call in build_opener.call_args_list:
            handlers = call.args
            self.assertTrue(any(getattr(handler, '_context', None) is context for handler in handlers))


if __name__ == '__main__':
    unittest.main()
