"""Local binding state, with explicit upload and stale-result protection."""

import copy
import threading
import time
import uuid
from urllib.parse import urlsplit
from desktop.capture import clean_string, valid_vin
from desktop.capture_context import DEVICE_FIELDS, capture_readiness, normalize_share_context, valid_text


class Controller:
    OPERATION_TIMEOUT = 35

    def __init__(self, cloud, store):
        self.cloud, self.store = cloud, store
        self.lock = threading.RLock()
        self.operation = threading.Lock()
        self.identity = None
        self.error = None
        self.vault_unavailable = False
        self.session = None
        self.generation = 0
        self.stage = 'idle'
        self.verification_error = None
        self.platform = None
        self.candidate = None
        self.binding = None
        self.runs = {'items': [], 'nextCursor': None}
        self.connected = False
        self.configured = False
        self.last_refresh = 0
        self.proxy = None
        self.capture_events = []
        self.schedule_windows = None
        self.schedule_windows_error = None
        self.access_token_consumed = False
        self.proxy_epoch = None
        self.retired_identities = set()
        self.latest_auth_started_at = 0
        self.pending_vehicle = None

    def _operation_deadline(self):
        return time.monotonic() + self.OPERATION_TIMEOUT

    def _refresh_schedule_windows(self, deadline=None):
        try:
            result = self._request('GET', '/v1/schedule-windows', deadline=deadline) if self.identity else None
            if self.identity and (not isinstance(result, dict) or not isinstance(result.get('items'), list)):
                raise ValueError('暂时无法查询区间名额，请刷新后重试')
            with self.lock:
                self.schedule_windows, self.schedule_windows_error = result, None
        except ValueError:
            with self.lock:
                self.schedule_windows = None
                self.schedule_windows_error = '暂时无法查询区间名额，请刷新后重试'

    def record_capture(self, summary):
        from desktop.capture import AUTH_HOSTS, AUTH_PATHS, display_path
        if not isinstance(summary, dict):
            return
        host = clean_string(summary.get('host'), 253)
        if not host or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-:' for c in host):
            return
        outcomes = ('captured', 'http_error', 'invalid_json', 'business_error', 'incomplete', 'pending', 'completed', 'network_error', 'tunnel',
                    'vehicle_captured', 'vehicle_incomplete')
        if summary.get('outcome') not in outcomes or (summary.get('status') is not None and type(summary.get('status')) is not int):
            return
        fields = summary.get('fields')
        if not isinstance(fields, dict):
            return
        event = {k: summary[k] for k in ('host', 'path', 'outcome', 'status')}
        is_auth = host in AUTH_HOSTS and event['path'] in AUTH_PATHS
        if not is_auth:
            path = clean_string(event['path'], 2048)
            event['path'] = display_path(path) if path and path.startswith('/') else '/[redacted]'
        event['fields'] = {k: fields.get(k) is True for k in ('token', 'refreshToken', 'deviceId', 'platform')} if is_auth else {}
        method = summary.get('method')
        event['method'] = method if method in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS', 'CONNECT') else 'OTHER'
        event_id = clean_string(summary.get('id'), 64)
        event['id'] = event_id if event_id and all(c in '0123456789abcdef-' for c in event_id) else None
        event['at'] = int(time.time() * 1000)
        with self.lock:
            if event['id']:
                self.capture_events = [e for e in self.capture_events if e.get('id') != event['id']]
            self.capture_events = (self.capture_events + [event])[-200:]
        if self.proxy:
            self.proxy.note_activity()

    def _cloud_request(self, method, path, body=None, token=None, deadline=None):
        if deadline is not None and deadline <= time.monotonic():
            raise ValueError('云端请求超时，请稍后重试')
        if deadline is None:
            return self.cloud.request(method, path, body, token)
        return self.cloud.request(method, path, body, token, deadline=deadline)

    def _request(self, method, path, body=None, deadline=None):
        if not self.identity:
            raise ValueError('请先在客户端登录')
        return self._cloud_request(method, path, body, self.identity['managementToken'], deadline)

    def has_license(self):
        return self.store.has_license()

    def unlock(self, login_code):
        """Unlock locally, then verify the management token without touching LynkCo."""
        with self.operation:
            deadline = self._operation_deadline()
            identity = self.store.load(clean_string(login_code, 512))
            try:
                result = self._cloud_request('GET', '/v1/users/me', token=identity['managementToken'], deadline=deadline)
            except ValueError as error:
                if getattr(error, 'code', None) in ('UNAUTHORIZED', 'CREDENTIAL_INVALID'):
                    self.store.invalidate()
                    raise ValueError('云端登录已失效，请重置登录码') from None
                raise
            if not isinstance(result, dict) or result.get('userId') != identity['userId']:
                raise ValueError('云端身份校验失败，请重试')
            with self.lock:
                self.identity = identity
                self.error = None
            try:
                summary = self._request('GET', '/v1/binding/summary', deadline=deadline)
            except ValueError:
                # Older cloud deployments do not expose the fast summary route;
                # the normal refresh below remains the source of truth.
                summary = None
            with self.lock:
                self.binding = summary
            return self.public_state()

    def reset(self, reset_code, vin):
        with self.operation:
            deadline = self._operation_deadline()
            code = clean_string(reset_code, 128)
            if not code or len(code) < 16 or not all(char.isalnum() or char in '-_' for char in code):
                raise ValueError('重置码格式无效，请输入管理员提供的重置码')
            normalized_vin = valid_vin(vin)
            if not normalized_vin:
                raise ValueError('请输入有效的17位车架号')
            payload = {'recoveryToken': code, 'vin': normalized_vin}
            snapshot, version = self._freeze_capture()
            try:
                identity = self._cloud_request('POST', '/v1/users/recover', payload, deadline=deadline)
                result = self._adopt_identity(identity, deadline)
            except Exception:
                self._restore_capture(snapshot, version)
                raise
            if not result['saved']:
                self._restore_capture(snapshot, version)
            return result

    def register(self, code, recover=False, vin=None):
        if not recover:
            raise ValueError('请使用邀请码领取账号')
        return self.reset(code, vin)

    def claim(self, claim_code):
        """Redeem an invitation code from the native login window."""
        with self.operation:
            deadline = self._operation_deadline()
            value = clean_string(claim_code, 2048)
            if not value:
                raise ValueError('请输入有效的邀请码')
            parsed = urlsplit(value)
            token = value
            if parsed.scheme or parsed.netloc:
                base = urlsplit(self.cloud.base_url)
                if (parsed.scheme != base.scheme or parsed.hostname != base.hostname or
                        parsed.port != base.port or parsed.username or parsed.password):
                    raise ValueError('领取链接不是本助手的云端链接')
                if parsed.query or parsed.fragment:
                    raise ValueError('领取链接格式无效')
                parts = parsed.path.rstrip('/').split('/')
                if len(parts) != 3 or parts[1] != 'claim':
                    raise ValueError('领取链接格式无效')
                token = parts[2]
            # The Worker rejects tokens shorter than 16 characters before hashing;
            # reject them here so malformed input never becomes a vague 404.
            if not clean_string(token, 128) or len(token) < 16 or not all(c.isalnum() or c in '-_' for c in token):
                raise ValueError('邀请码格式无效')
            if self.identity:
                raise ValueError('当前设备已经连接云端')
            snapshot, version = self._freeze_capture()
            try:
                identity = self._cloud_request('POST', '/v1/claim/' + token, {}, deadline=deadline)
                result = self._adopt_identity(identity, deadline)
            except Exception:
                self._restore_capture(snapshot, version)
                raise
            if not result['saved']:
                self._restore_capture(snapshot, version)
            return result

    def reset_capture(self):
        """Discard a stale local binding flow before replacing an account."""
        with self.operation:
            if self.proxy and self.proxy.public_state().get('running'):
                raise ValueError('请先关闭手机代理并断开当前连接')
            with self.lock:
                self._retire_current_locked()
                self.session = self.candidate = None
                self.pending_vehicle = None
                self.capture_events = []
                self.stage = 'idle'
                self.verification_error = None
                self.generation += 1
            return {'reset': True}

    def begin_capture(self, platform, proxy_epoch=None):
        """Start a proxy epoch and invalidate all late callbacks from the prior flow."""
        with self.lock:
            self._retire_current_locked()
            self.session = self.candidate = None
            self.pending_vehicle = None
            self.capture_events = []
            self.platform = platform
            self.proxy_epoch = proxy_epoch
            self.retired_identities = set()
            self.latest_auth_started_at = 0
            self.access_token_consumed = False
            self.stage = 'waiting'
            self.verification_error = None
            self.generation += 1

    def _retire_current_locked(self):
        if self.session:
            for key in ('token', 'refreshToken'):
                value = self.session.get(key)
                if isinstance(value, str):
                    self.retired_identities.add(value)
        if len(self.retired_identities) > 32:
            self.retired_identities = set(list(self.retired_identities)[-32:])

    def clear_local_identity(self):
        """Remove the local management credential without deleting the cloud binding."""
        with self.operation:
            if self.proxy and self.proxy.public_state().get('running'):
                raise ValueError('请先关闭手机代理并断开当前连接，再清除本地登录态')
            self.store.delete()
            with self.lock:
                self.identity = None
                self.connected = False
                self.configured = False
                self.binding = None
                self.runs = {'items': [], 'nextCursor': None}
                self.error = None
                self.vault_unavailable = False
                self.session = self.candidate = None
                self.pending_vehicle = None
                self.platform = None
                self.capture_events = []
                self.stage = 'idle'
                self.verification_error = None
                self.schedule_windows = None
                self.schedule_windows_error = None
                self.generation += 1
            return {'cleared': True}

    def lock_identity(self):
        """Clear all in-memory identity and captured secrets on idle lock."""
        with self.operation:
            if self.proxy:
                self.proxy.stop()
            with self.lock:
                self._retire_current_locked()
                self.identity = None
                self.session = self.candidate = self.binding = None
                self.pending_vehicle = None
                self.runs = {'items': [], 'nextCursor': None}
                self.capture_events = []
                self.stage = 'idle'
                self.verification_error = None
                self.connected = False
                self.generation += 1

    def force_stop_proxy(self):
        """Stop the desktop proxy and report whether the phone proxy may still be enabled."""
        with self.operation:
            proxy_state = self.proxy.public_state() if self.proxy else {}
            was_running = isinstance(proxy_state, dict) and proxy_state.get('running') is True
            if self.proxy:
                self.proxy.stop()
            self.stop_capture()
            return {'stopped': True, 'phoneMustDisable': was_running}

    def _adopt_identity(self, identity, deadline=None):
        login_code = identity['loginCode']
        saved = {key: identity[key] for key in ('userId', 'managementToken')}
        # The code is returned once for display; neither it nor the token is stored in plaintext.
        try:
            self.store.save(saved, login_code)
        except (ValueError, OSError) as error:
            raise ValueError('本机许可保存失败，请保留登录码并联系管理员重置') from error
        with self.lock:
            self.identity = saved
            self.session = self.candidate = self.binding = None
            self.pending_vehicle = None
            self.runs = {'items': [], 'nextCursor': None}
            self.capture_events = []
            self.generation += 1
            self.stage = 'idle'
            self.platform = None
            self.verification_error = None
            self.error = None
            self.vault_unavailable = False
        return {'loginCode': login_code, 'saved': True}

    def _freeze_capture(self):
        """Block a long-running account action from racing an active verification."""
        with self.lock:
            snapshot = {
                'session': copy.deepcopy(self.session),
                'pending_vehicle': copy.deepcopy(self.pending_vehicle),
                'candidate': copy.deepcopy(self.candidate),
                'stage': self.stage,
                'platform': self.platform,
                'verification_error': self.verification_error,
                'capture_events': copy.deepcopy(self.capture_events),
            }
            self.generation += 1
            version = self.generation
            self.candidate = None
            self.stage = 'cleanup'
            self.verification_error = None
            return snapshot, version

    def _restore_capture(self, snapshot, version):
        """Restore a cancelled account action without reviving its old worker."""
        with self.lock:
            if self.generation != version:
                return
            self.session = snapshot['session']
            self.pending_vehicle = snapshot['pending_vehicle']
            self.platform = snapshot['platform']
            self.capture_events = snapshot['capture_events']
            if snapshot['stage'] == 'verifying':
                self.candidate = None
                self.stage = 'verification_failed'
                self.verification_error = '个人信息验证已中断，请重试'
                return
            self.candidate = snapshot['candidate']
            self.stage = snapshot['stage']
            self.verification_error = snapshot['verification_error']

    def receive_capture(self, session, request_started_at=None, proxy_epoch=None):
        if not isinstance(session, dict) or session.get('platform') not in ('IOS', 'ANDROID'):
            return False
        if not all(clean_string(session.get(key), 256 if key == 'deviceId' else 4096) for key in ('token', 'refreshToken', 'deviceId')):
            return False
        allowed = ('token', 'refreshToken', 'deviceId', 'platform', 'expireAt', 'refreshExpireAt', 'capturedAt', 'appVersion', 'appBuild',
                   'deviceName', 'deviceModel', 'deviceBrand', 'osVersion', 'glDevId', 'deviceImei', 'devicePlatform',
                   'userAgent', 'previousRefreshToken')
        captured = {k: session[k] for k in allowed if k in session and
                    ((isinstance(session[k], (int, float)) and not isinstance(session[k], bool)) or clean_string(session[k]))}
        captured['capturedAt'] = int(session.get('capturedAt') or time.time() * 1000)
        started = int(request_started_at or captured['capturedAt'])
        with self.lock:
            self._clear_expired_capture_locked()
            if self.stage == 'cleanup':
                return False
            if self.proxy_epoch and proxy_epoch != self.proxy_epoch:
                return False
            if captured['token'] in self.retired_identities or captured['refreshToken'] in self.retired_identities:
                return False
            current = self.session
            same_device = current and all(current.get(key) == captured.get(key) for key in ('deviceId', 'platform'))
            exact = same_device and all(current.get(key) == captured.get(key) for key in ('token', 'refreshToken'))
            linked_rotation = (same_device and captured.get('previousRefreshToken') == current.get('refreshToken')) if current else False
            # Ordering belongs to the paired proxy epoch, including account/device changes.
            if started < self.latest_auth_started_at:
                return False
            if exact:
                for key in DEVICE_FIELDS:
                    if key in captured:
                        current[key] = captured[key]
                self.latest_auth_started_at = max(self.latest_auth_started_at, started)
                return True
            if linked_rotation:
                if current['token'] != captured['token']:
                    self.retired_identities.add(current['token'])
                captured['capturedAt'] = current['capturedAt']
                captured['captureId'] = current['captureId']
                captured['accessRevision'] = uuid.uuid4().hex
                for key in DEVICE_FIELDS:
                    if key not in captured and key in current:
                        captured[key] = current[key]
                if 'deviceImei' not in captured and current.get('deviceImei'):
                    captured['deviceImei'] = current['deviceImei']
                if current.get('shareContext'):
                    captured['shareContext'] = copy.deepcopy(current['shareContext'])
                if current.get('probeArticleId'):
                    captured['probeArticleId'] = current['probeArticleId']
                if current.get('vehicle'):
                    captured['vehicle'] = dict(current['vehicle'])
            else:
                self._retire_current_locked()
                captured['captureId'] = uuid.uuid4().hex
                captured['accessRevision'] = uuid.uuid4().hex
                if self.pending_vehicle:
                    captured['vehicle'] = self.pending_vehicle
            self.pending_vehicle = None
            self.session = captured
            self.access_token_consumed = False
            self.platform = session['platform']
            self.latest_auth_started_at = started
            self.generation += 1
            self.candidate = None
            self.stage = 'captured'
            self.verification_error = None
            version = self.generation
            captured = dict(self.session)
        return True

    def receive_share_capture(self, event, proxy_epoch=None):
        if not isinstance(event, dict) or not valid_text(event.get('token'), 4096):
            return False
        context = normalize_share_context(event.get('shareContext'))
        if not context:
            return False
        article_id = clean_string(event.get('probeArticleId'), 256) if event.get('probeArticleId') is not None else None
        with self.lock:
            self._clear_expired_capture_locked()
            if self.proxy_epoch and proxy_epoch != self.proxy_epoch:
                return False
            if not self.session or self._capture_expired() or self.stage not in ('waiting', 'captured', 'verification_failed'):
                return False
            if event['token'] != self.session.get('token'):
                return False
            self.session['shareContext'] = context
            if article_id:
                self.session['probeArticleId'] = article_id
            self.generation += 1
            self.candidate = None
            self.stage = 'captured'
            self.verification_error = None
            return True

    def receive_vehicle_capture(self, event, proxy_epoch=None):
        vin = valid_vin(event.get('vin')) if isinstance(event, dict) else None
        if not vin:
            return False
        with self.lock:
            self._clear_expired_capture_locked()
            if self.stage not in ('waiting', 'captured', 'verification_failed'):
                return False
            if self.proxy_epoch and proxy_epoch != self.proxy_epoch:
                return False
            if self.session and not self._capture_expired():
                self.session['vehicle'] = {'vin': vin}
                self.generation += 1
                self.candidate = None
                self.stage = 'captured'
                self.verification_error = None
            else:
                self.pending_vehicle = {'vin': vin}
            return True

    @staticmethod
    def _verification_error(error):
        if getattr(error, 'code', None) == 'CREDENTIAL_INVALID':
            return '登录状态已失效，请重新获取'
        return '个人信息验证暂时失败，请稍后重试'

    def _verify_capture(self, session, version):
        try:
            candidate = self._request('POST', '/v1/binding-candidates', {'session': self._refresh_only(session)})
        except Exception as error:
            with self.lock:
                if version != self.generation or self.stage != 'verifying':
                    return
                self.candidate = None
                self.stage = 'verification_failed'
                self.verification_error = self._verification_error(error)
            return
        with self.lock:
            if version != self.generation or self.stage != 'verifying':
                return
            self.candidate = candidate
            self.stage = 'verified'
            self.verification_error = None

    @staticmethod
    def _refresh_only(session):
        """Build a transient verification payload; access credentials are never persisted."""
        allowed = ('token', 'expireAt', 'refreshExpireAt', 'refreshToken', 'deviceId', 'platform', 'appVersion', 'appBuild', 'deviceName',
                   'deviceModel', 'deviceBrand', 'osVersion', 'glDevId', 'deviceImei', 'devicePlatform', 'userAgent')
        result = {key: session[key] for key in allowed if key in session and session[key] is not None}
        context = normalize_share_context(session.get('shareContext'))
        if context:
            result['shareContext'] = context
        vin = valid_vin(session.get('vehicle', {}).get('vin')) if isinstance(session.get('vehicle'), dict) else None
        if vin:
            result['vehicle'] = {'vin': vin}
        return result

    @staticmethod
    def _capture_deadline(session):
        captured = session.get('capturedAt') or int(time.time() * 1000)
        return int(captured) + 30 * 60 * 1000

    def _capture_expired(self, session=None):
        session = session or self.session
        return not session or time.time() * 1000 >= self._capture_deadline(session)

    def _clear_expired_capture_locked(self):
        if self.session and self._capture_expired():
            self.latest_auth_started_at = max(self.latest_auth_started_at, self._capture_deadline(self.session))
            self._retire_current_locked()
            self.session = self.candidate = None
            self.pending_vehicle = None
            self.stage = 'idle'
            self.verification_error = None
            self.generation += 1
            return True
        return False

    def consume_capture_access_token(self):
        with self.lock:
            if self._clear_expired_capture_locked():
                raise ValueError('登录态已过期，请重新抓包')
            if self.access_token_consumed:
                raise ValueError('登录态访问凭证已使用，请重新抓包')
            if not self.session or self._capture_expired():
                self.session = self.candidate = None
                self.stage = 'idle'
                self.generation += 1
                raise ValueError('登录态已过期，请重新抓包')
            token = self.session.get('token')
            expire_at = self.session.get('expireAt')
            if isinstance(expire_at, (int, float)) and expire_at <= time.time() * 1000:
                raise ValueError('登录态访问凭证已过期，请重新抓包')
            result = {'accessToken': token, 'expireAt': min(int(self.session.get('expireAt') or 0) or int(self.session['capturedAt']) + 30 * 60 * 1000,
                                                             int(self.session['capturedAt']) + 30 * 60 * 1000),
                      'captureId': self.session.get('captureId'), 'accessRevision': self.session.get('accessRevision')}
            self.access_token_consumed = True
            return result

    def probe_input(self):
        """Return an in-memory complete snapshot only while this capture is active."""
        with self.lock:
            if (self._clear_expired_capture_locked() or not self.session
                    or (isinstance(self.session.get('expireAt'), (int, float)) and self.session['expireAt'] <= time.time() * 1000)
                    or not all(capture_readiness(self.session)['readiness'][key] for key in ('login', 'device', 'share'))):
                raise ValueError('当前抓包信息不可用，请重新完成登录、设备和分享')
            result = copy.deepcopy(self.session)
            result.pop('captureId', None)
            result.pop('accessRevision', None)
            result.pop('vehicle', None)
            return result

    def retry_verification(self):
        with self.lock:
            if not self.session:
                raise ValueError('尚未获取完整登录状态，请在手机上打开对应 App')
            if self.stage not in ('captured', 'verification_failed'):
                raise ValueError('个人信息正在验证或已经验证完成')
            if self._capture_expired():
                self.session = self.candidate = None
                self.stage = 'idle'
                raise ValueError('登录态已过期，请重新抓包')
            self.stage = 'verifying'
            self.verification_error = None
            version, session = self.generation, dict(self.session)
        threading.Thread(target=self._verify_capture, args=(session, version), daemon=True).start()
        return {'started': True}

    def prepare(self):
        """Upload a refresh-only session after explicit user confirmation."""
        with self.operation:
            with self.lock:
                if self._clear_expired_capture_locked():
                    raise ValueError('登录态已过期，请重新抓包')
                if not self.session:
                    raise ValueError('尚未获取完整登录状态，请在手机上打开对应 App')
                if self._capture_expired():
                    self.session = self.candidate = None
                    self.stage = 'idle'
                    self.generation += 1
                    raise ValueError('登录态已过期，请重新抓包')
                readiness = capture_readiness(self.session)
                if not all(readiness['readiness'].values()):
                    raise ValueError('请先完成登录态、设备信息、分享信息和车架号识别')
                self.stage = 'verifying'
                self.verification_error = None
                version, session = self.generation, dict(self.session)
            try:
                candidate = self._request('POST', '/v1/binding-candidates', {'session': self._refresh_only(session)})
            except Exception as error:
                with self.lock:
                    if version == self.generation:
                        self.stage = 'verification_failed'
                        self.verification_error = self._verification_error(error)
                raise
            with self.lock:
                if version != self.generation or self.stage != 'verifying':
                    raise ValueError('登录状态已更新，请重新确认')
                self.candidate = candidate
                self.stage = 'verified'
                return candidate

    def stop_capture(self):
        """Invalidate background verification before the phone proxy is stopped."""
        with self.lock:
            self._retire_current_locked()
            self.session = self.candidate = None
            self.pending_vehicle = None
            self.stage = 'idle'
            self.verification_error = None
            self.generation += 1

    def activate(self, settings):
        with self.operation:
            deadline = self._operation_deadline()
            with self.lock:
                candidate = self.candidate
                if not candidate or candidate['expiresAt'] <= time.time() * 1000:
                    self.candidate = None
                    self.stage = 'captured' if self.session else 'waiting'
                    raise ValueError('验证结果已过期，请重新获取登录状态')
                version = self.generation
                session = dict(self.session or {})
                vehicle = session.get('vehicle')
                if not isinstance(vehicle, dict) or not valid_vin(vehicle.get('vin')):
                    raise ValueError('请先在手机上打开车辆页面，抓取车架号')
                # Freeze capture during activation so a second phone flow cannot replace the preview.
                self.stage = 'cleanup'
            try:
                payload = dict(settings)
                payload['session'] = self._refresh_only(session)
                binding = self._request('POST', '/v1/binding', payload, deadline=deadline)
            except Exception as error:
                with self.lock:
                    if version == self.generation:
                        self.stage = 'verified'
                if getattr(error, 'code', None) == 'SLOT_FULL':
                    self._refresh_schedule_windows(deadline)
                raise
            with self.lock:
                self.binding = binding
                self.session = self.candidate = None
                self.generation += 1
                self.verification_error = None
            if self.proxy:
                self.proxy.disable_capture()
            self._refresh_schedule_windows(deadline)
            return binding

    def refresh(self):
        with self.operation:
            deadline = self._operation_deadline()
            try:
                health = self._cloud_request('GET', '/health', deadline=deadline)
                binding = self._request('GET', '/v1/binding', deadline=deadline) if self.identity else None
                runs = self._request('GET', '/v1/binding/runs', deadline=deadline) if binding else {'items': [], 'nextCursor': None}
                self._refresh_schedule_windows(deadline)
                with self.lock:
                    self.connected, self.configured = True, health.get('configured', False)
                    self.binding, self.runs = binding, runs
                    if not self.vault_unavailable:
                        self.error = None
                    self.last_refresh = time.time()
            except ValueError as error:
                if getattr(error, 'code', None) in ('UNAUTHORIZED', 'CREDENTIAL_INVALID'):
                    with self.lock:
                        self.identity = None
                        self.connected = False
                        self.configured = False
                        self.binding = None
                        self.runs = {'items': [], 'nextCursor': None}
                        self.error = '云端账号已失效，请重新连接账号'
                    invalidate = getattr(self.store, 'invalidate', None)
                    if invalidate:
                        try:
                            invalidate()
                        except ValueError:
                            pass
                    return self.public_state()
                with self.lock:
                    self.connected, self.error = False, str(error)
                raise
        return self.public_state()

    def settings(self, body):
        with self.operation:
            deadline = self._operation_deadline()
            try:
                binding = self._request('PATCH', '/v1/binding', body, deadline)
            except ValueError as error:
                if getattr(error, 'code', None) == 'SLOT_FULL':
                    self._refresh_schedule_windows(deadline)
                raise
            with self.lock:
                for key in ('inventory', 'inventoryError', 'avatarUrl'):
                    if self.binding and key in self.binding and key not in binding:
                        binding[key] = self.binding[key]
                self.binding = binding
            self._refresh_schedule_windows(deadline)
            return binding

    def test_notification(self):
        with self.operation:
            return self._request('POST', '/v1/binding/notifications/test', {})

    def run(self, mode='job'):
        if mode not in ('sign', 'share', 'job'):
            raise ValueError('任务类型无效')
        with self.operation:
            return self._request('POST', '/v1/binding/runs', {'mode': mode})

    def delete(self):
        with self.operation:
            deadline = self._operation_deadline()
            snapshot, version = self._freeze_capture()
            try:
                result = self._request('DELETE', '/v1/binding', deadline=deadline)
            except Exception:
                self._restore_capture(snapshot, version)
                raise
            with self.lock:
                self.binding = None
                self.runs = {'items': [], 'nextCursor': None}
                if self.generation == version:
                    self.session = self.candidate = None
                    self.pending_vehicle = None
                    self.capture_events = []
                    self.platform = None
                    self.stage = 'idle'
                    self.verification_error = None
            self._refresh_schedule_windows(deadline)
            return result

    def history(self, cursor):
        from urllib.parse import urlencode
        with self.operation:
            return self._request('GET', '/v1/binding/runs?' + urlencode({'cursor': cursor}))

    def public_state(self):
        proxy_state = self.proxy.public_state() if self.proxy else {'running': False}
        with self.lock:
            self._clear_expired_capture_locked()
            if proxy_state.get('needsReconfigure'):
                if self.session is not None or self.candidate is not None or self.capture_events or self.stage != 'idle':
                    self.session = self.candidate = None
                    self.pending_vehicle = None
                    self.capture_events = []
                    self.platform = None
                    self.stage = 'idle'
                    self.verification_error = None
                    self.generation += 1
            readiness = capture_readiness(self.session)
            return copy.deepcopy({
                'hasIdentity': bool(self.identity), 'vaultUnavailable': self.vault_unavailable,
                'connected': self.connected, 'configured': self.configured,
                'error': self.error, 'lastRefreshAt': int(self.last_refresh * 1000),
                'binding': self.binding, 'runs': self.runs, 'candidate': self.candidate,
                'scheduleWindows': self.schedule_windows, 'scheduleWindowsError': self.schedule_windows_error,
                'capture': {'stage': self.stage, 'platform': self.platform,
                            'capturedAt': self.session.get('capturedAt') if self.session else None,
                            'expireAt': self.session.get('expireAt') if self.session else None,
                            'captureId': self.session.get('captureId') if self.session else None,
                            'accessRevision': self.session.get('accessRevision') if self.session else None,
                            'shareCapturedAt': self.session.get('shareContext', {}).get('capturedAt') if self.session else None,
                            'readiness': readiness['readiness'], 'missing': readiness['missing'],
                            'verificationError': self.verification_error, 'events': self.capture_events},
                'proxy': proxy_state,
            })
