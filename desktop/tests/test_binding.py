import importlib.util
import json
import threading
import time
import unittest
from unittest.mock import Mock


class MemoryStore:
    value = None
    deleted = False
    invalidated = False

    def has_license(self):
        return self.value is not None

    def load(self, login_code):
        if login_code != 'recovery-secret' and login_code != 'new-recovery':
            raise ValueError('登录码错误')
        return self.value

    def save(self, value, login_code):
        self.value = value

    def delete(self):
        self.value = None
        self.deleted = True

    def invalidate(self):
        self.invalidated = True


class FakeCloud:
    def __init__(self):
        self.calls = []
        self.on_prepare = None
        self.prepare_started = threading.Event()
        self.prepare_release = threading.Event()
        self.prepare_release.set()
        self.prepare_gates = {}
        self.prepare_error = None
        self.action_started = {}
        self.action_gates = {}
        self.action_errors = {}
        self.auth_error = None
        self.deadlines = []
        self.base_url = 'https://lynkco.ltools.asia'

    def wait_for_action(self, path):
        self.action_started.setdefault(path, threading.Event()).set()
        gate = self.action_gates.get(path)
        if gate:
            gate.wait(2)
        error = self.action_errors.get(path)
        if error:
            raise error

    def request(self, method, path, body=None, token=None, deadline=None):
        if deadline is not None:
            self.deadlines.append((path, deadline))
        self.calls.append((method, path, body))
        if path.startswith('/v1/claim/'):
            return dict(userId='owner', managementToken='management-secret', loginCode='recovery-secret')
        if path == '/v1/users/recover':
            self.wait_for_action(path)
            if body and body.get('recoveryToken') == 'admin-reset-token':
                return dict(userId='owner', managementToken='new-management', loginCode='new-recovery')
            return dict(userId='owner', managementToken='management-secret', loginCode='recovery-secret')
        if path == '/v1/users/me':
            if self.auth_error:
                raise self.auth_error
            return {'userId': 'owner'}
        if path == '/v1/binding-candidates':
            self.prepare_started.set()
            self.prepare_gates.get(body['session'].get('refreshToken'), self.prepare_release).wait(2)
            if self.on_prepare:
                self.on_prepare()
            if self.prepare_error:
                raise self.prepare_error
            candidate_id = 'candidate-new-account' if body['session'].get('refreshToken') == 'new-refresh' else 'candidate-current-account'
            return dict(id=candidate_id, expiresAt=9999999999999,
                        preview=dict(points='10', alreadySigned=True), capabilities=dict(share=False))
        if path.endswith('/activate'):
            return dict(id='binding', label='car', status='active', doShare=False, canShare=False, scheduleTime='08:10', nextRunAt=0)
        if path == '/v1/binding' and method == 'DELETE':
            self.wait_for_action(path)
            return {'deleted': True}
        if path.endswith('/runs'):
            return dict(items=[], nextCursor=None)
        if path == '/health':
            if self.auth_error:
                raise self.auth_error
            return dict(service='lynkco-helper', configured=True)
        if path == '/v1/schedule-windows':
            return {'items':[{'value':'08:00-10:00','limit':10,'used':1,'remaining':9,'current':False}]}
        return None


SESSION = dict(token='mobile-token-secret', refreshToken='mobile-refresh-secret', deviceId='device', platform='IOS',
               appVersion='4.2.7', appBuild='427', deviceName='phone', deviceModel='model', deviceBrand='brand',
               osVersion='17', glDevId='gl-device', devicePlatform='ios', userAgent='Lynkco/4.2.7',
               shareContext={'capturedAt': 1700000000000,
                             'headers': {'user-agent': 'Lynkco/4.2.7', 'gl_dev_id': 'gl-device'},
                             'security': {'appVersion': '4.2.7', 'platform': 'ios'}})


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('desktop.binding'), 'binding controller must exist')
        from desktop.binding import Controller
        self.cloud = FakeCloud()
        self.controller = Controller(self.cloud, MemoryStore())
        self.controller.claim('https://lynkco.ltools.asia/claim/claim-token_123456')
        self.controller._refresh_schedule_windows()

    def wait_for_stage(self, stage):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if self.controller.public_state()['capture']['stage'] == stage:
                return self.controller.public_state()
            time.sleep(.01)
        self.fail(f'capture did not reach {stage}: {self.controller.public_state()}')

    def capture_and_verify(self, session=SESSION):
        self.assertTrue(self.controller.receive_capture({key: value for key, value in session.items() if key != 'shareContext'}))
        self.assertTrue(self.controller.receive_share_capture({'token': session['token'], 'shareContext': session['shareContext']}))
        self.assertTrue(self.controller.receive_vehicle_capture({'vin': 'L1234567890123456'}))
        self.assertEqual(self.controller.cloud.calls[-1][1], '/v1/schedule-windows')
        self.controller.prepare()
        return self.wait_for_stage('verified')

    def test_unlock_only_verifies_management_token_and_never_recovers(self):
        store = MemoryStore()
        first = __import__('desktop.binding', fromlist=['Controller']).Controller(self.cloud, store)
        first.claim('claim-token_123456')
        first.lock_identity()
        self.cloud.calls.clear()
        self.assertFalse(first.public_state()['hasIdentity'])
        self.assertTrue(first.has_license())
        first.unlock('recovery-secret')
        self.assertTrue(first.public_state()['hasIdentity'])
        self.assertEqual(self.cloud.calls, [('GET', '/v1/users/me', None)])

    def test_reset_rotates_login_code_and_token(self):
        store = MemoryStore()
        controller = __import__('desktop.binding', fromlist=['Controller']).Controller(self.cloud, store)
        result = controller.reset('admin-reset-token', 'L1234567890123456')
        self.assertEqual(result['loginCode'], 'new-recovery')
        self.assertEqual(store.value['managementToken'], 'new-management')
        self.assertIn(('POST', '/v1/users/recover', {'recoveryToken': 'admin-reset-token', 'vin': 'L1234567890123456'}), self.cloud.calls)

    def test_claim_link_redeems_without_persisting_link(self):
        controller = __import__('desktop.binding', fromlist=['Controller']).Controller(self.cloud, MemoryStore())
        result = controller.claim('https://lynkco.ltools.asia/claim/claim-token_123456')
        self.assertTrue(result['saved'])
        self.assertIn(('POST', '/v1/claim/claim-token_123456', {}), self.cloud.calls)

    def test_probe_article_id_stays_local_and_is_not_uploaded(self):
        session = {**SESSION, 'probeArticleId': 'captured-article'}
        auth = {key: value for key, value in session.items() if key != 'shareContext'}
        self.assertTrue(self.controller.receive_capture(auth))
        self.assertTrue(self.controller.receive_share_capture({
            'token': session['token'],
            'shareContext': session['shareContext'],
            'probeArticleId': session['probeArticleId'],
        }))
        self.controller.receive_vehicle_capture({'vin': 'L1234567890123456'})
        self.controller.prepare()
        self.wait_for_stage('verified')
        candidate_call = next(call for call in self.cloud.calls if call[1] == '/v1/binding-candidates')
        self.assertNotIn('probeArticleId', candidate_call[2]['session'])

    def test_vehicle_capture_is_private_epoch_scoped_and_uploaded_on_confirmation(self):
        self.controller.begin_capture('IOS', 'epoch-new')
        self.assertFalse(self.controller.receive_vehicle_capture({'vin': 'L1234567890123456'}, 'epoch-old'))
        self.assertTrue(self.controller.receive_vehicle_capture({'vin': 'L1234567890123456'}, 'epoch-new'))
        self.assertTrue(self.controller.receive_capture(SESSION, proxy_epoch='epoch-new'))
        self.assertTrue(self.controller.receive_share_capture({'token': SESSION['token'], 'shareContext': SESSION['shareContext']}, 'epoch-new'))
        state = json.dumps(self.controller.public_state())
        self.assertNotIn('L1234567890123456', state)
        self.assertTrue(self.controller.public_state()['capture']['readiness']['vehicle'])
        self.controller.prepare()
        self.assertEqual(self.cloud.calls[-1][2]['session']['vehicle'], {'vin': 'L1234567890123456'})
        self.controller.activate({'label': 'car', 'scheduleTime': '08:00-10:00'})
        self.assertEqual(self.cloud.calls[-2][2]['session']['vehicle'], {'vin': 'L1234567890123456'})

    def test_save_requires_captured_vehicle_and_rejects_old_epoch(self):
        self.controller.begin_capture('IOS', 'current')
        self.controller.receive_capture(SESSION, proxy_epoch='current')
        self.controller.receive_share_capture({'token': SESSION['token'], 'shareContext': SESSION['shareContext']}, 'current')
        with self.assertRaisesRegex(ValueError, '车架号'):
            self.controller.prepare()
        self.assertFalse(self.controller.receive_vehicle_capture({'vin': 'L1234567890123456'}, 'previous'))

    def test_reset_requires_code_and_vin_without_accepting_link(self):
        with self.assertRaisesRegex(ValueError, '车架号'):
            self.controller.reset('admin-reset-token', '')
        with self.assertRaisesRegex(ValueError, '重置码'):
            self.controller.reset('https://lynkco.ltools.asia/recover#token=admin-reset-token', 'L1234567890123456')
        self.controller.reset('admin-reset-token', 'l1234567890123456')
        self.assertIn(('POST', '/v1/users/recover', {'recoveryToken': 'admin-reset-token', 'vin': 'L1234567890123456'}), self.cloud.calls)

    def test_clear_local_identity_removes_only_local_credential(self):
        store = MemoryStore()
        controller = __import__('desktop.binding', fromlist=['Controller']).Controller(self.cloud, store)
        controller.claim('claim-token_123456')
        controller.binding = {'id': 'binding'}

        result = controller.clear_local_identity()

        self.assertEqual(result, {'cleared': True})
        self.assertIsNone(controller.identity)
        self.assertIsNone(controller.binding)
        self.assertTrue(store.deleted)

    def test_existing_license_stays_locked_until_explicit_unlock(self):
        store = MemoryStore()
        store.value = {'userId': 'owner', 'managementToken': 'management-secret'}
        controller = __import__('desktop.binding', fromlist=['Controller']).Controller(self.cloud, store)
        self.assertTrue(controller.has_license())
        self.assertFalse(controller.public_state()['hasIdentity'])
        controller.unlock('recovery-secret')
        self.assertTrue(controller.public_state()['hasIdentity'])

    def test_expired_access_token_does_not_shorten_capture_confirmation(self):
        now = int(time.time() * 1000)
        self.controller.receive_capture({**SESSION, 'expireAt': now - 1000})
        self.controller.receive_share_capture({'token': SESSION['token'], 'shareContext': SESSION['shareContext']})
        self.assertEqual(self.controller.public_state()['capture']['stage'], 'captured')
        with self.assertRaisesRegex(ValueError, '访问凭证已过期'):
            self.controller.consume_capture_access_token()
        self.assertEqual(self.controller.public_state()['capture']['stage'], 'captured')
        with self.assertRaisesRegex(ValueError, '不可用'):
            self.controller.probe_input()
        self.controller.receive_vehicle_capture({'vin': 'L1234567890123456'})
        self.controller.prepare()
        self.assertEqual(self.wait_for_stage('verified')['candidate']['id'], 'candidate-current-account')
        self.assertEqual(self.cloud.calls[-1][2]['session']['refreshToken'], SESSION['refreshToken'])
        self.assertEqual(self.cloud.calls[-1][2]['session']['token'], SESSION['token'])
        self.assertEqual(self.cloud.calls[-1][2]['session']['expireAt'], now - 1000)

    def test_capture_confirmation_expires_after_thirty_minutes_despite_access_expiry(self):
        now = int(time.time() * 1000)
        self.controller.receive_capture({**SESSION, 'expireAt': now + 60 * 60 * 1000})
        self.controller.receive_share_capture({'token': SESSION['token'], 'shareContext': SESSION['shareContext']})
        self.controller.session['capturedAt'] = now - 30 * 60 * 1000 - 1
        with self.assertRaisesRegex(ValueError, '过期'):
            self.controller.prepare()

    def test_browser_access_token_never_outlives_its_expiry(self):
        now = int(time.time() * 1000)
        self.controller.receive_capture({**SESSION, 'expireAt': now + 60 * 1000})
        result = self.controller.consume_capture_access_token()
        self.assertEqual(result['expireAt'], now + 60 * 1000)

    def test_refresh_uses_one_operation_deadline_and_releases_lock_after_timeout(self):
        from desktop.cloud_client import CloudError

        self.controller.OPERATION_TIMEOUT = 0.01
        self.cloud.deadlines.clear()
        original = self.cloud.request

        def request(method, path, body=None, token=None, deadline=None):
            if path == '/health':
                self.assertIsNotNone(deadline)
                self.cloud.deadlines.append((path, deadline))
                raise CloudError('NETWORK', 'fixture timeout')
            return original(method, path, body, token, deadline)

        self.cloud.request = request
        with self.assertRaises(CloudError):
            self.controller.refresh()
        self.assertEqual(len(self.cloud.deadlines), 1)
        self.assertEqual(self.controller.run(), {'items': [], 'nextCursor': None})
        self.assertIn(('POST', '/v1/binding/runs', {'mode': 'job'}), self.cloud.calls)

    def test_run_mode_is_validated_and_forwarded(self):
        self.assertEqual(self.controller.run('share'), {'items': [], 'nextCursor': None})
        self.assertIn(('POST', '/v1/binding/runs', {'mode': 'share'}), self.cloud.calls)
        with self.assertRaisesRegex(ValueError, '任务类型无效'):
            self.controller.run('unknown')

    def test_refresh_clears_expired_management_identity_and_persisted_credential(self):
        from desktop.cloud_client import CloudError

        store = MemoryStore()
        controller = __import__('desktop.binding', fromlist=['Controller']).Controller(self.cloud, store)
        controller.claim('claim-token_123456')
        self.cloud.auth_error = CloudError('UNAUTHORIZED', 'expired management credential')

        state = controller.refresh()

        self.assertFalse(state['hasIdentity'])
        self.assertIsNone(controller.identity)
        self.assertIsNone(state['binding'])
        self.assertTrue(store.invalidated)

    def test_deadline_adapter_failure_is_not_retried_without_a_deadline(self):
        calls = []

        def reject_deadline(*args, **kwargs):
            calls.append(kwargs)
            raise TypeError("unexpected keyword argument 'deadline'")

        self.cloud.request = reject_deadline
        with self.assertRaisesRegex(TypeError, 'deadline'):
            self.controller._cloud_request('GET', '/health', deadline=time.monotonic() + 1)
        self.assertEqual(len(calls), 1)
        self.assertIn('deadline', calls[0])

    def test_composite_actions_share_the_same_deadline_for_follow_up_refresh(self):
        self.controller.OPERATION_TIMEOUT = 5

        def assert_shared_deadline(action, primary_path):
            self.cloud.deadlines.clear()
            action()
            paths = [path for path, _ in self.cloud.deadlines]
            self.assertIn(primary_path, paths)
            self.assertIn('/v1/schedule-windows', paths)
            deadlines = [deadline for path, deadline in self.cloud.deadlines
                         if path in (primary_path, '/v1/schedule-windows')]
            self.assertEqual(len(set(deadlines)), 1)

        self.controller.binding = {'id': 'binding'}
        assert_shared_deadline(lambda: self.controller.settings({'scheduleTime': '08:00-10:00'}), '/v1/binding')
        self.controller.candidate = {'id': 'candidate', 'expiresAt': 9999999999999}
        self.controller.session = {**SESSION, 'capturedAt': int(time.time() * 1000), 'vehicle': {'vin': 'L1234567890123456'}}
        assert_shared_deadline(lambda: self.controller.activate({'scheduleTime': '08:00-10:00'}), '/v1/binding')
        self.controller.binding = {'id': 'binding'}
        assert_shared_deadline(self.controller.delete, '/v1/binding')

    def test_claim_accepts_invitation_code_without_full_link(self):
        controller = __import__('desktop.binding', fromlist=['Controller']).Controller(self.cloud, MemoryStore())
        result = controller.claim('claim-token_123456')
        self.assertTrue(result['saved'])
        self.assertIn(('POST', '/v1/claim/claim-token_123456', {}), self.cloud.calls)

    def test_claim_link_rejects_other_hosts(self):
        controller = __import__('desktop.binding', fromlist=['Controller']).Controller(self.cloud, MemoryStore())
        with self.assertRaisesRegex(ValueError, '不是本助手的云端链接'):
            controller.claim('https://example.com/claim/claim-token_123456')

    def test_claim_rejects_tokens_that_worker_would_report_as_missing(self):
        controller = __import__('desktop.binding', fromlist=['Controller']).Controller(self.cloud, MemoryStore())
        with self.assertRaisesRegex(ValueError, '格式无效'):
            controller.claim('short-token')

    def test_claim_link_rejects_query_or_fragment_suffixes(self):
        controller = __import__('desktop.binding', fromlist=['Controller']).Controller(self.cloud, MemoryStore())
        for suffix in ('?source=copy', '#section'):
            with self.assertRaisesRegex(ValueError, '格式无效'):
                controller.claim('https://lynkco.ltools.asia/claim/claim-token_123456' + suffix)

    def test_claim_link_rejects_embedded_credentials(self):
        controller = __import__('desktop.binding', fromlist=['Controller']).Controller(self.cloud, MemoryStore())
        with self.assertRaisesRegex(ValueError, '不是本助手的云端链接'):
            controller.claim('https://user@lynkco.ltools.asia/claim/claim-token_123456')

    def test_recover_accepts_admin_reset_code_with_vin(self):
        controller = __import__('desktop.binding', fromlist=['Controller']).Controller(self.cloud, MemoryStore())
        result = controller.register('admin-reset-token', recover=True, vin='L1234567890123456')
        self.assertTrue(result['saved'])
        self.assertIn(('POST', '/v1/users/recover', {'recoveryToken': 'admin-reset-token', 'vin': 'L1234567890123456'}), self.cloud.calls)

    def test_capture_does_not_upload_before_confirmation(self):
        self.cloud.prepare_release.clear()
        before = len(self.cloud.calls)
        started = time.monotonic()
        self.assertTrue(self.controller.receive_capture(SESSION))
        self.assertLess(time.monotonic() - started, .1)
        self.assertFalse(self.cloud.prepare_started.wait(.1))
        self.assertEqual(self.controller.public_state()['capture']['stage'], 'captured')
        self.assertEqual(len(self.cloud.calls), before)
        self.cloud.prepare_release.set()
        self.controller.receive_share_capture({'token': SESSION['token'], 'shareContext': SESSION['shareContext']})
        self.controller.receive_vehicle_capture({'vin': 'L1234567890123456'})
        self.controller.prepare()
        self.assertEqual(len(self.cloud.calls), before + 1)
        self.assertEqual(self.cloud.calls[-1][2]['session']['token'], SESSION['token'])

    def test_incomplete_auth_cannot_prepare_until_matching_share_enriches_it(self):
        incomplete = {key: value for key, value in SESSION.items() if key != 'shareContext'}
        self.assertTrue(self.controller.receive_capture(incomplete))
        self.assertFalse(self.controller.public_state()['capture']['readiness']['share'])
        with self.assertRaisesRegex(ValueError, '分享信息'):
            self.controller.prepare()
        self.assertTrue(self.controller.receive_share_capture({'token': SESSION['token'], 'shareContext': SESSION['shareContext']}))
        self.assertTrue(self.controller.public_state()['capture']['readiness']['share'])

    def test_same_capture_metadata_enrichment_keeps_deadline_and_access_consumption(self):
        incomplete = {key: value for key, value in SESSION.items() if key not in ('shareContext', 'deviceName')}
        self.controller.receive_capture(incomplete)
        first = self.controller.consume_capture_access_token()
        captured_at = self.controller.session['capturedAt']
        self.assertTrue(self.controller.receive_capture({**incomplete, 'deviceName': 'enriched'}))
        self.assertEqual(self.controller.session['capturedAt'], captured_at)
        with self.assertRaises(ValueError):
            self.controller.consume_capture_access_token()

    def test_linked_refresh_rotation_keeps_context_but_rejects_out_of_order_same_refresh_response(self):
        self.controller.receive_capture({key: value for key, value in SESSION.items() if key != 'shareContext'}, request_started_at=100)
        self.controller.receive_share_capture({'token': SESSION['token'], 'shareContext': SESSION['shareContext']})
        older = {**SESSION, 'token': 'older-token', 'previousRefreshToken': SESSION['refreshToken']}
        newer = {**SESSION, 'token': 'newer-token', 'previousRefreshToken': SESSION['refreshToken']}
        self.assertTrue(self.controller.receive_capture(newer, request_started_at=300))
        self.assertFalse(self.controller.receive_capture(older, request_started_at=200))
        self.assertEqual(self.controller.session['token'], 'newer-token')
        self.assertEqual(self.controller.session['shareContext'], SESSION['shareContext'])

    def test_sparse_linked_rotation_retains_device_context_and_same_token_is_not_retired(self):
        auth = {key: value for key, value in SESSION.items() if key != 'shareContext'}
        self.controller.receive_capture(auth, request_started_at=100)
        self.controller.receive_share_capture({'token': SESSION['token'], 'shareContext': SESSION['shareContext']})
        sparse = {'token': SESSION['token'], 'refreshToken': 'rotated-refresh', 'previousRefreshToken': SESSION['refreshToken'],
                  'deviceId': SESSION['deviceId'], 'platform': SESSION['platform']}
        self.assertTrue(self.controller.receive_capture(sparse, request_started_at=200))
        self.assertTrue(self.controller.public_state()['capture']['readiness']['device'])
        self.assertNotIn(SESSION['token'], self.controller.retired_identities)

    def test_new_proxy_epoch_allows_the_same_credentials_after_expiration_cleanup(self):
        self.controller.receive_capture(SESSION)
        self.controller.session['capturedAt'] = 1
        self.controller.public_state()
        self.assertIsNone(self.controller.session)
        self.controller.begin_capture('IOS', 'new-epoch')
        self.assertTrue(self.controller.receive_capture(SESSION, proxy_epoch='new-epoch'))

    def test_late_auth_from_different_device_identity_cannot_replace_new_account(self):
        self.controller.receive_capture(SESSION, request_started_at=100)
        newer = {**SESSION, 'token': 'account-b-token', 'refreshToken': 'account-b-refresh', 'deviceId': 'account-b-device'}
        self.assertTrue(self.controller.receive_capture(newer, request_started_at=300))
        late = {**SESSION, 'token': 'old-account-rotated-token', 'refreshToken': 'old-account-rotated-refresh'}
        self.assertFalse(self.controller.receive_capture(late, request_started_at=200))
        self.assertEqual(self.controller.session['token'], newer['token'])

    def test_refresh_started_before_capture_expiry_cannot_revive_its_window(self):
        now = int(time.time() * 1000)
        deadline = now - 1000
        self.controller.receive_capture({**SESSION, 'capturedAt': deadline - 30 * 60 * 1000}, request_started_at=deadline - 2000)
        self.controller.public_state()
        late = {**SESSION, 'token': 'late-token', 'refreshToken': 'late-refresh', 'previousRefreshToken': SESSION['refreshToken']}
        self.assertFalse(self.controller.receive_capture(late, request_started_at=deadline - 1))
        self.assertIsNone(self.controller.session)
        self.assertTrue(self.controller.receive_capture(late, request_started_at=now))

    def test_expired_capture_is_rejected_and_cleared(self):
        self.assertTrue(self.controller.receive_capture({**SESSION, 'capturedAt': 1}))
        with self.assertRaisesRegex(ValueError, '过期'):
            self.controller.prepare()
        self.assertEqual(self.controller.public_state()['capture']['stage'], 'idle')

    def test_access_token_endpoint_value_is_one_time_and_public_state_safe(self):
        self.assertTrue(self.controller.receive_capture(SESSION))
        value = self.controller.consume_capture_access_token()
        self.assertEqual(value['accessToken'], SESSION['token'])
        with self.assertRaisesRegex(ValueError, '已使用'):
            self.controller.consume_capture_access_token()
        self.assertNotIn(SESSION['token'], json.dumps(self.controller.public_state()))

    def test_duplicate_capture_does_not_start_a_second_verification(self):
        self.assertTrue(self.controller.receive_capture(SESSION))
        self.assertTrue(self.controller.receive_capture(dict(SESSION)))
        self.assertEqual(len([call for call in self.cloud.calls if call[1] == '/v1/binding-candidates']), 0)

    @unittest.skip('replaced by explicit confirmation contract')
    def test_failed_verification_exposes_a_safe_error_and_retry_keeps_session(self):
        self.cloud.prepare_error = ValueError('upstream token=mobile-token-secret')
        self.assertTrue(self.controller.receive_capture(SESSION))
        state = self.wait_for_stage('verification_failed')
        self.assertEqual(state['capture']['verificationError'], '个人信息验证暂时失败，请稍后重试')
        self.assertNotIn('mobile-token-secret', json.dumps(state))
        self.cloud.prepare_error = None
        self.controller.retry_verification()
        self.wait_for_stage('verified')

    @unittest.skip('no background verification after capture')
    def test_new_capture_prevents_the_old_verification_from_publishing(self):
        old_request = threading.Event()
        self.cloud.prepare_gates[SESSION['token']] = old_request
        self.assertTrue(self.controller.receive_capture(SESSION))
        self.assertTrue(self.cloud.prepare_started.wait(.5))
        self.assertTrue(self.controller.receive_capture({**SESSION, 'token': 'new-account'}))
        state = self.wait_for_stage('verified')
        old_request.set()
        time.sleep(.05)
        self.assertEqual(len([call for call in self.cloud.calls if call[1] == '/v1/binding-candidates']), 2)
        self.assertEqual(state['candidate']['id'], 'candidate-new-account')

    @unittest.skip('no background verification after capture')
    def test_reset_prevents_inflight_verification_from_publishing(self):
        self.cloud.prepare_release.clear()
        self.assertTrue(self.controller.receive_capture(SESSION))
        self.assertTrue(self.cloud.prepare_started.wait(.5))
        self.controller.reset_capture()
        self.cloud.prepare_release.set()
        time.sleep(.05)
        state = self.controller.public_state()
        self.assertEqual(state['capture']['stage'], 'idle')
        self.assertIsNone(state['candidate'])

    @unittest.skip('no background verification after capture')
    def test_activation_prevents_an_older_verification_from_publishing(self):
        self.cloud.prepare_release.clear()
        self.assertTrue(self.controller.receive_capture(SESSION))
        self.assertTrue(self.cloud.prepare_started.wait(.5))
        self.controller.candidate = {'id': 'previous-candidate', 'expiresAt': 9999999999999}
        self.controller.activate({'label': 'car', 'scheduleTime': '08:00-10:00', 'doShare': False})
        self.cloud.prepare_release.set()
        time.sleep(.05)
        state = self.controller.public_state()
        self.assertEqual(state['capture']['stage'], 'cleanup')
        self.assertIsNone(state['candidate'])

    @unittest.skip('no background verification after capture')
    def test_identity_adoption_prevents_an_older_verification_from_publishing(self):
        self.cloud.prepare_release.clear()
        self.assertTrue(self.controller.receive_capture(SESSION))
        self.assertTrue(self.cloud.prepare_started.wait(.5))
        self.controller.register('admin-reset-token', recover=True)
        self.cloud.prepare_release.set()
        time.sleep(.05)
        state = self.controller.public_state()
        self.assertEqual(state['capture']['stage'], 'idle')
        self.assertIsNone(state['candidate'])

    @unittest.skip('no background verification after capture')
    def test_binding_deletion_clears_capture_state_and_blocks_stale_verification(self):
        delete_release = threading.Event()
        self.cloud.action_gates['/v1/binding'] = delete_release
        self.cloud.prepare_release.clear()
        self.controller.binding = {'id': 'binding'}
        self.controller.record_capture({'host': 'app-services.lynkco.com.cn', 'path': '/auth/login/refresh',
                                        'method': 'POST', 'status': 200, 'outcome': 'captured', 'id': 'deadbeef',
                                        'fields': {'token': True, 'refreshToken': True, 'deviceId': True, 'platform': True}})
        self.assertTrue(self.controller.receive_capture(SESSION))
        self.assertTrue(self.cloud.prepare_started.wait(.5))
        thread = threading.Thread(target=self.controller.delete)
        thread.start()
        self.assertTrue(self.cloud.action_started['/v1/binding'].wait(.5))
        self.cloud.prepare_release.set()
        time.sleep(.05)
        self.assertEqual(self.controller.public_state()['capture']['stage'], 'cleanup')
        delete_release.set()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        state = self.controller.public_state()
        self.assertEqual(state['capture']['stage'], 'idle')
        self.assertIsNone(state['candidate'])
        self.assertIsNone(state['capture']['platform'])
        self.assertIsNone(state['capture']['verificationError'])
        self.assertEqual(state['capture']['events'], [])

    @unittest.skip('no background verification after capture')
    def test_recovery_freezes_verification_before_the_remote_action_completes(self):
        recovery_release = threading.Event()
        self.cloud.action_gates['/v1/users/recover'] = recovery_release
        self.cloud.prepare_release.clear()
        self.assertTrue(self.controller.receive_capture(SESSION))
        self.assertTrue(self.cloud.prepare_started.wait(.5))
        thread = threading.Thread(target=lambda: self.controller.register('admin-reset-token', recover=True))
        thread.start()
        self.assertTrue(self.cloud.action_started['/v1/users/recover'].wait(.5))
        self.cloud.prepare_release.set()
        time.sleep(.05)
        self.assertEqual(self.controller.public_state()['capture']['stage'], 'cleanup')
        recovery_release.set()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        state = self.controller.public_state()
        self.assertEqual(state['capture']['stage'], 'idle')
        self.assertIsNone(state['candidate'])

    @unittest.skip('no background verification after capture')
    def test_failed_recovery_restores_a_retryable_capture_state(self):
        self.cloud.prepare_release.clear()
        self.cloud.action_errors['/v1/users/recover'] = ValueError('fixture recovery failure')
        self.assertTrue(self.controller.receive_capture(SESSION))
        self.assertTrue(self.cloud.prepare_started.wait(.5))
        with self.assertRaisesRegex(ValueError, 'fixture recovery failure'):
            self.controller.register('admin-reset-token', recover=True)
        self.cloud.prepare_release.set()
        state = self.wait_for_stage('verification_failed')
        self.assertIsNone(state['candidate'])
        self.assertEqual(state['capture']['verificationError'], '个人信息验证已中断，请重试')

    @unittest.skip('no background verification after capture')
    def test_failed_deletion_restores_a_retryable_capture_state(self):
        self.cloud.prepare_release.clear()
        self.cloud.action_errors['/v1/binding'] = ValueError('fixture delete failure')
        self.controller.binding = {'id': 'binding'}
        self.assertTrue(self.controller.receive_capture(SESSION))
        self.assertTrue(self.cloud.prepare_started.wait(.5))
        with self.assertRaisesRegex(ValueError, 'fixture delete failure'):
            self.controller.delete()
        self.cloud.prepare_release.set()
        state = self.wait_for_stage('verification_failed')
        self.assertEqual(state['capture']['verificationError'], '个人信息验证已中断，请重试')
        self.assertIsNone(state['candidate'])

    def test_slot_counts_are_cached_locally_until_explicit_refresh(self):
        self.assertEqual(self.controller.public_state()['scheduleWindows']['items'][0]['remaining'],9)
        before = len(self.cloud.calls)
        self.controller.public_state()
        self.controller.public_state()
        self.assertEqual(len(self.cloud.calls),before)

    def test_full_slot_refreshes_counts_and_preserves_candidate(self):
        from desktop.cloud_client import CloudError
        self.capture_and_verify()
        original = self.cloud.request
        def request(method,path,body=None,token=None,deadline=None):
            if path == '/v1/binding' and method == 'POST':
                raise CloudError('SLOT_FULL','该区间名额已满')
            if path == '/v1/schedule-windows':
                return {'items':[{'value':'08:00-10:00','limit':10,'used':10,'remaining':0,'current':False}]}
            return original(method,path,body,token,deadline)
        self.cloud.request = request
        with self.assertRaisesRegex(ValueError,'名额已满'):
            self.controller.activate({'scheduleTime':'08:00-10:00'})
        state = self.controller.public_state()
        self.assertEqual(state['scheduleWindows']['items'][0]['remaining'],0)
        self.assertEqual(state['capture']['stage'],'verified')
        self.assertIsNotNone(state['candidate'])

    def test_public_state_never_exposes_credentials(self):
        self.capture_and_verify()
        output = json.dumps(self.controller.public_state())
        for secret in ('mobile-token-secret', 'mobile-refresh-secret', 'management-secret', 'recovery-secret'):
            self.assertNotIn(secret, output)

    def test_verification_payload_can_include_access_expiry_without_persisting_capture_secrets(self):
        self.controller.receive_capture({**SESSION, 'expireAt': 1789869600000, 'refreshExpireAt': 1789956000000})
        self.controller.receive_share_capture({'token': SESSION['token'], 'shareContext': SESSION['shareContext']})
        self.controller.receive_vehicle_capture({'vin': 'L1234567890123456'})
        self.controller.prepare()
        payload = self.cloud.calls[-1][2]['session']
        self.assertEqual(payload['token'], SESSION['token'])
        self.assertEqual(payload['expireAt'], 1789869600000)
        self.assertEqual(payload['refreshExpireAt'], 1789956000000)
        self.assertNotIn('token', self.controller.public_state())
        self.assertNotIn(SESSION['token'], json.dumps(self.controller.public_state()))

    def test_network_reconfiguration_clears_capture_state(self):
        self.controller.proxy = Mock()
        self.controller.proxy.public_state.return_value = {
            'running': False, 'paired': False, 'pairUrl': None,
            'captureEnabled': False, 'networkChanged': True,
            'needsReconfigure': True,
        }
        self.controller.session = dict(SESSION)
        self.controller.candidate = {'id': 'stale-candidate'}
        self.controller.capture_events = [{'id': 'stale-event'}]
        self.controller.stage = 'verified'

        state = self.controller.public_state()

        self.assertEqual(state['capture']['stage'], 'idle')
        self.assertIsNone(state['candidate'])
        self.assertEqual(state['capture']['events'], [])
        self.assertIsNone(self.controller.session)

    def test_activation_clears_local_session(self):
        self.capture_and_verify()
        self.controller.activate(dict(label='car', scheduleTime='08:10', doShare=False))
        self.assertEqual(self.controller.public_state()['capture']['stage'], 'cleanup')
        with self.assertRaises(ValueError):
            self.controller.prepare()

    def test_token_only_capture_rejected(self):
        self.assertFalse(self.controller.receive_capture(dict(token='short-only')))
        self.assertEqual(self.controller.public_state()['capture']['stage'], 'idle')

    def test_expired_preview_returns_to_capture_step(self):
        self.capture_and_verify()
        self.controller.candidate['expiresAt'] = 1
        with self.assertRaisesRegex(ValueError, '过期'):
            self.controller.activate(dict(label='car', scheduleTime='08:10', doShare=False))
        self.assertEqual(self.controller.public_state()['capture']['stage'], 'captured')
        self.assertIsNone(self.controller.public_state()['candidate'])

    def test_activation_failure_keeps_preview_and_does_not_claim_success(self):
        self.capture_and_verify()
        original = self.cloud.request
        def failing(method, path, body=None, token=None, deadline=None):
            if path == '/v1/binding' and method == 'POST':
                raise ValueError('fixture unavailable')
            return original(method, path, body, token, deadline)
        self.cloud.request = failing
        with self.assertRaises(ValueError):
            self.controller.activate(dict(label='car', scheduleTime='08:10', doShare=False))
        self.assertEqual(self.controller.public_state()['capture']['stage'], 'verified')
        self.assertIsNone(self.controller.public_state()['binding'])

    def test_reset_capture_returns_replacement_flow_to_first_step(self):
        self.capture_and_verify()
        self.controller.reset_capture()
        capture = self.controller.public_state()['capture']
        self.assertEqual(capture['stage'], 'idle')
        self.assertIsNone(self.controller.public_state()['candidate'])
        self.assertEqual(capture['events'], [])

    def test_notification_test_is_forwarded_to_cloud(self):
        result = self.controller.test_notification()
        self.assertIsNone(result)
        self.assertIn(('POST', '/v1/binding/notifications/test', {}), self.cloud.calls)


if __name__ == '__main__':
    unittest.main()
