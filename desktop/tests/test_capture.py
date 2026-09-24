import importlib
import importlib.util
import unittest
import asyncio
from unittest.mock import AsyncMock, Mock


class CaptureTests(unittest.TestCase):
    def test_vehicle_detail_extracts_only_successful_vin(self):
        from desktop.capture import parse_vehicle_capture
        url = 'https://gric-hf-api.geely.com/ms-vehicle-account/api/v1.0/vehicle-detail'
        payload = {'code': '0', 'data': {'vin': 'l1234567890123456', 'plateNo': 'private'}}
        self.assertEqual(parse_vehicle_capture(url, 200, payload), {'vin': 'L1234567890123456'})
        for bad_url in (url.replace('https:', 'http:'), url.replace('geely.com', 'geely.com.evil.test'), url + '/other'):
            self.assertIsNone(parse_vehicle_capture(bad_url, 200, payload))
        for bad in ({'code': '1', 'data': payload['data']}, {'code': '0', 'data': {'vin': 'IOQ34567890123456'}},
                    {'code': '0', 'data': {'vin': 'short'}}, {'code': '0', 'data': []}):
            self.assertIsNone(parse_vehicle_capture(url, 200, bad))
        self.assertIsNone(parse_vehicle_capture(url, 500, payload))

    def test_proxy_observes_vehicle_detail_without_requesting_it(self):
        from desktop.capture_addon import CaptureAddon
        addon = CaptureAddon()
        addon.state = lambda: {'captureEnabled': True, 'peerIp': '192.0.2.4'}
        addon.report = AsyncMock()
        flow = Mock()
        flow.client_conn.peername = ('192.0.2.4', 1234)
        flow.request.host = 'gric-hf-api.geely.com'
        flow.request.method = 'GET'
        flow.request.path = '/ms-vehicle-account/api/v1.0/vehicle-detail'
        flow.request.pretty_url = 'https://gric-hf-api.geely.com/ms-vehicle-account/api/v1.0/vehicle-detail'
        flow.response.status_code = 200
        flow.response.raw_content = b'{}'
        flow.response.json.return_value = {'code': '0', 'data': {'vin': 'L1234567890123456'}}
        asyncio.run(addon.response(flow))
        self.assertEqual(addon.report.await_args.kwargs['vehicle'], {'vin': 'L1234567890123456'})
        self.assertNotIn('L1234567890123456', str(addon.report.await_args.kwargs['summary']))

    def test_general_request_metadata_retains_route_without_query_secrets(self):
        from desktop import capture
        self.assertTrue(hasattr(capture, 'request_summary'))
        result = capture.request_summary('https://example.com/app/user/info?token=secret#secret', 'POST', None, 'pending')
        self.assertEqual(result['host'], 'example.com')
        self.assertEqual(result['method'], 'POST')
        self.assertEqual(result['path'], '/app/user/info')
        self.assertNotIn('secret', str(result))

    def test_path_redacts_identifier_and_credential_segments(self):
        from desktop.capture import display_path
        self.assertEqual(display_path('/app/user/13800000000/info?token=secret'), '/app/user/[redacted]/info')
        self.assertEqual(display_path('/auth/token/secret-value'), '/auth/token/[redacted]')

    def test_diagnostic_contains_only_allowlisted_metadata(self):
        from desktop import capture
        self.assertTrue(hasattr(capture, 'capture_summary'))
        result = capture.capture_summary(self.url + '&mobile=13800000000', self.headers, 200, self.response, 'IOS')
        self.assertEqual(result['outcome'], 'captured')
        self.assertTrue(result['fields']['refreshToken'])
        import json
        for secret in ('new-token', 'new-refresh', 'old-refresh', 'phone-a', '13800000000'):
            self.assertNotIn(secret, json.dumps(result))
        self.assertEqual(result['path'], '/auth/login/refresh')

    def test_diagnostic_distinguishes_failure_and_missing_fields(self):
        from desktop import capture
        self.assertTrue(hasattr(capture, 'capture_summary'))
        self.assertEqual(capture.capture_summary(self.url, self.headers, 401, {}, 'IOS')['outcome'], 'http_error')
        self.assertEqual(capture.capture_summary(self.url, self.headers, 200, None, 'IOS')['outcome'], 'invalid_json')
        self.assertEqual(capture.capture_summary(self.url, self.headers, 200, {'code': 'success', 'data': {}}, 'IOS')['outcome'], 'incomplete')

    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("desktop.capture"), "capture parser is not implemented")
        self.parse = importlib.import_module("desktop.capture").parse_session
        self.url = "https://app-services.lynkco.com.cn/auth/login/refresh?deviceId=phone-a&refreshToken=old-refresh"
        self.headers = {"publicplatform": "iOS", "gl_dev_id": "gl-phone-a", "appversioncode": "4.2.7"}
        self.response = {"code": "success", "data": {"centerTokenDto": {"token": "new-token", "refreshToken": "new-refresh"}}}

    def test_pairs_device_from_request_with_new_refresh_token_from_response(self):
        result = self.parse(self.url, self.headers, 200, self.response)
        self.assertEqual({key: result[key] for key in ("token", "refreshToken", "deviceId", "platform", "glDevId", "appVersion")}, {
            "token": "new-token", "refreshToken": "new-refresh", "deviceId": "phone-a",
            "platform": "IOS", "glDevId": "gl-phone-a", "appVersion": "4.2.7"})
        self.assertIsInstance(result["capturedAt"], int)

    def test_refresh_without_rotated_value_retains_same_request_refresh(self):
        self.response["data"]["centerTokenDto"].pop("refreshToken")
        self.assertEqual(self.parse(self.url, self.headers, 200, self.response)["refreshToken"], "old-refresh")

    def test_refresh_captures_device_fingerprint_metadata_for_cloud_renewal(self):
        headers = {**self.headers, "gl_dev_name": "iPhone", "gl_dev_model": "iPhone 15 Pro",
                   "gl_dev_brand": "Apple", "gl_os_version": "27.0"}
        result = self.parse(self.url, headers, 200, self.response)
        self.assertEqual({key: result[key] for key in result if key not in ("capturedAt", "expireAt", "sourcePath", "previousRefreshToken")}, {
            "token": "new-token", "refreshToken": "new-refresh", "deviceId": "phone-a",
            "platform": "IOS", "glDevId": "gl-phone-a", "appVersion": "4.2.7",
            "deviceName": "iPhone", "deviceModel": "iPhone 15 Pro",
            "deviceBrand": "Apple", "osVersion": "27.0",
        })

    def test_login_uses_hardware_id_and_discards_phone_and_sms(self):
        url = "https://app-services.lynkco.com.cn/auth/login/mobileCodeLogin?hardwareDeviceId=android-a&deviceType=ANDROID&mobile=13800000000&verificationCode=123456"
        result = self.parse(url, {}, 200, self.response)
        self.assertEqual({key: result[key] for key in result if key not in ("capturedAt", "expireAt", "sourcePath", "previousRefreshToken")}, {
            "token": "new-token", "refreshToken": "new-refresh", "deviceId": "android-a", "platform": "ANDROID",
        })

    def test_password_login_path_is_captured_with_the_same_device_contract(self):
        url = ("https://app-services.lynkco.com.cn/auth/login/sliding/login"
               "?hardwareDeviceId=android-a&deviceType=ANDROID&deviceModel=sdk&username=13800000000")
        result = self.parse(url, {}, 200, self.response)
        self.assertEqual(result["token"], "new-token")
        self.assertEqual(result["refreshToken"], "new-refresh")
        self.assertEqual(result["deviceId"], "android-a")
        self.assertEqual(result["platform"], "ANDROID")
        self.assertEqual(result["sourcePath"], "/auth/login/sliding/login")

    def test_password_login_allowlist_does_not_match_similar_paths(self):
        url = ("https://app-services.lynkco.com.cn/auth/login/sliding/login/extra"
               "?hardwareDeviceId=android-a&deviceType=ANDROID")
        self.assertIsNone(self.parse(url, {}, 200, self.response))

    def test_unrelated_domains_and_business_requests_never_produce_credentials(self):
        for url in [self.url.replace("app-services.lynkco.com.cn", "app-services.lynkco.com.cn.evil.test"),
                    self.url.replace("https://", "http://"),
                    "https://app-api-gw-toc.lynkco.com/app/energy/myEnergy"]:
            with self.subTest(url=url):
                self.assertIsNone(self.parse(url, self.headers, 200, self.response))

    def test_failed_business_or_http_response_is_rejected(self):
        self.assertIsNone(self.parse(self.url, self.headers, 401, self.response))
        self.response["code"] = "invalid.token"
        self.assertIsNone(self.parse(self.url, self.headers, 200, self.response))

    def test_incomplete_or_malformed_credentials_are_rejected(self):
        for dto in [{"token": "short-token"}, {"token": ["not-a-string"], "refreshToken": "refresh"},
                    {"token": "token\r\ninjected", "refreshToken": "refresh"}]:
            with self.subTest(dto=dto):
                url = "https://app-services.lynkco.com.cn/auth/login/mobileCodeLogin?hardwareDeviceId=phone"
                self.assertIsNone(self.parse(url, self.headers, 200, {"code": "success", "data": {"centerTokenDto": dto}}))

    def test_missing_device_cannot_be_replaced_by_another_flow(self):
        url = "https://app-services.lynkco.com.cn/auth/login/refresh?refreshToken=old"
        self.assertIsNone(self.parse(url, self.headers, 200, self.response))

    def test_malformed_urls_and_payloads_are_ignored(self):
        for url, payload in [("https://[broken", self.response), (self.url, []), (self.url, {"data": []}),
                             (self.url, {"code": "success", "data": {"centerTokenDto": []}})]:
            with self.subTest(url=url, payload=payload):
                self.assertIsNone(self.parse(url, self.headers, 200, payload))

    def test_share_capture_requires_success_code_agreeing_token_and_share_code(self):
        from desktop.capture import parse_share_capture
        url = 'https://app-api-gw-toc.lynkco.com/app/v1/task/getShareCode'
        headers = {'token': 'Bearer access-value', 'svcsid': 'access-value', 'user-agent': 'Lynkco/4.2.7',
                   'gl_dev_id': 'gl-device', 'risk_request_info': '{"shareContentURL":"https://h5.example/?id=article-1"}',
                   'sweet_security_info': '{"appVersion":"4.2.7","platform":"ios"}'}
        event = parse_share_capture(url, headers, 200, {'code': 'success', 'data': 'opaque-code'})
        self.assertEqual(event['token'], 'access-value')
        self.assertEqual(event['probeArticleId'], 'article-1')
        self.assertNotIn('shareCode', str(event))
        self.assertIsNone(parse_share_capture(url, {**headers, 'svcsid': 'other'}, 200, {'code': 'success', 'data': 'opaque-code'}))
        self.assertIsNone(parse_share_capture(url, headers, 200, {'code': 'success', 'data': {}}))
        self.assertIsNotNone(parse_share_capture(url, headers, 200, {'success': True, 'code': 0, 'data': 'opaque-code'}))
        self.assertIsNone(parse_share_capture(url, headers, 200, {'success': False, 'code': 200, 'data': 'opaque-code'}))

    def test_share_capture_preserves_distinct_security_fields_as_strings(self):
        import json
        from desktop.capture import parse_share_capture
        security = {'deviceUUID': 'security-device', 'isJailbreak': 'false',
                    'isLBSEnabled': 'true', 'isUsingVpn': 'false',
                    'os_version': '18.0', 'ua': 'captured-ua', 'wifiMAC': 'masked-mac',
                    'geelyDeviceId': 'other-device', 'osVersion': '18.1'}
        headers = {'token': 'Bearer access-value', 'svcsid': 'access-value',
                   'user-agent': 'Captured/9.8', 'sweet_security_info': json.dumps(security)}
        event = parse_share_capture('https://app-api-gw-toc.lynkco.com/app/v1/task/getShareCode',
                                    headers, 200, {'code': 'success', 'data': 'opaque-code'})
        self.assertIsNotNone(event)
        self.assertEqual(event['shareContext']['security'], security)

    def test_share_capture_accepts_nested_code_and_common_header_variants(self):
        from desktop.capture import parse_share_capture, is_share_request
        self.assertTrue(is_share_request('https://app-api-gw-toc.lynkco.com/app/v1/task/getShareCode/'))
        self.assertTrue(is_share_request('https://app-api-gw-toc.lynkco.com.cn/app/v1/task/getShareCode'))
        headers = {
            'Authorization': 'Bearer access-value',
            'User-Agent': 'Lynkco/9.0',
            'sweet-security-info': '{"platform":"ios","appVersion":"9.0"}',
            'risk_request_info': '{"shareContentUrl":"https://h5.example/?id=article-2"}',
        }
        event = parse_share_capture(
            'https://app-api-gw-toc.lynkco.com/app/v1/task/getShareCode/', headers, 200,
            {'code': 'success', 'data': {'shareCode': 'opaque-code'}})
        self.assertEqual(event['token'], 'access-value')
        self.assertEqual(event['probeArticleId'], 'article-2')

    def test_har_like_bearer_tokens_are_canonicalized_across_auth_and_share(self):
        from desktop.capture import parse_share_capture
        auth_response = {"code": "success", "data": {"centerTokenDto": {
            "token": "Bearer har-access-token", "refreshToken": "har-refresh-token"}}}
        session = self.parse(
            'https://app-services.lynkco.com.cn/auth/login/refresh'
            '?deviceId=har-device&refreshToken=old-refresh&deviceType=ANDROID',
            {}, 200, auth_response)
        self.assertEqual(session['token'], 'har-access-token')
        share = parse_share_capture(
            'https://app-api-gw-toc.lynkco.com/app/v1/task/getShareCode',
            {'token': 'Bearer har-access-token', 'svcsid': 'Bearer har-access-token',
             'user-agent': 'HAR/1.0',
             'sweet_security_info': '{"platform":"android"}'},
            200, {'code': 'success', 'data': 'share-code'})
        self.assertEqual(share['token'], session['token'])
        self.assertIsNone(parse_share_capture(
            'https://app-api-gw-toc.lynkco.com/app/v1/task/getShareCode',
            {'token': 'Bearer har-access-token', 'svcsid': 'Bearer other-token',
             'user-agent': 'HAR/1.0',
             'sweet_security_info': '{"platform":"android"}'},
            200, {'code': 'success', 'data': 'share-code'}))

    def test_bearer_prefix_without_space_is_canonicalized(self):
        from desktop.capture import parse_share_capture
        headers = {
            'token': 'bearerhar-access-token',
            'svcsid': 'Bearer har-access-token',
            'Authorization': 'bearerhar-access-token',
            'user-agent': 'HAR/1.0',
            'sweet_security_info': '{"platform":"android"}',
        }
        event = parse_share_capture(
            'https://app-api-gw-toc.lynkco.com/app/v1/task/getShareCode', headers,
            200, {'code': 'success', 'data': 'share-code'})
        self.assertEqual(event['token'], 'har-access-token')


if __name__ == "__main__":
    unittest.main()
