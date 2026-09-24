"""Bounded cloud requests; credentials never cross a redirect."""

import hashlib
import json
import ssl
import threading
import time
import uuid
import certifi
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, ProxyHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class CloudError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


class CloudClient:
    NORMAL_TIMEOUT = 35
    RUN_TIMEOUT = 120
    MAX_RESPONSE_BYTES = 262144

    def __init__(self, base_url):
        parsed = urlsplit(base_url)
        if (parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in {'127.0.0.1', 'localhost'})) or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
            raise ValueError('云端地址必须是 HTTPS 服务地址')
        self.base_url = base_url.rstrip('/')
        context = ssl.create_default_context(cafile=certifi.where())
        self.opener = build_opener(HTTPSHandler(context=context), NoRedirect())
        self.direct_opener = build_opener(ProxyHandler({}), HTTPSHandler(context=context), NoRedirect())
        self.loopback = parsed.hostname in {'127.0.0.1', 'localhost'}
        self.keys = {}
        self.lock = threading.Lock()

    def request(self, method, path, body=None, token=None, deadline=None):
        if not path.startswith('/v1/') and path != '/health':
            raise ValueError('无效的云端接口')
        data = json.dumps(body, separators=(',', ':')).encode() if body is not None else None
        headers = {'Accept': 'application/json', 'Content-Type': 'application/json',
                   'User-Agent': 'LynkCoHelper/0.1 (Desktop)'}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        if method != 'GET':
            digest = hashlib.sha256(method.encode() + path.encode() + (data or b'') + (token or '').encode()).hexdigest()
            with self.lock:
                now = time.monotonic()
                self.keys = {k: v for k, v in self.keys.items() if now - v[1] < 540}
                headers['Idempotency-Key'] = self.keys.setdefault(digest, (str(uuid.uuid4()), now))[0]
        request = Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            request_deadline = time.monotonic() + self._timeout_budget(method, path)
            if deadline is not None:
                request_deadline = min(request_deadline, deadline)
            last_error = None
            openers = (self.opener,) if self.loopback else (self.opener, self.direct_opener)
            payload = None
            for index, opener in enumerate(openers):
                remaining = request_deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    response = opener.open(request, timeout=remaining)
                except HTTPError as error:
                    response = error
                except (URLError, OSError, TimeoutError) as error:
                    last_error = error
                    continue
                try:
                    with response:
                        payload = self._read_response(response, request_deadline)
                    break
                except (URLError, OSError, TimeoutError) as error:
                    last_error = error
                    if index + 1 >= len(openers):
                        raise
            if payload is None:
                raise last_error or TimeoutError()
            if len(payload) > self.MAX_RESPONSE_BYTES:
                raise ValueError()
            result = json.loads(payload)
            if not isinstance(result, dict) or not isinstance(result.get('ok'), bool):
                raise ValueError()
        except (URLError, OSError, TimeoutError):
            raise CloudError('NETWORK', '暂时无法连接云端，请检查网络后重试') from None
        except (ValueError, TypeError):
            raise CloudError('BAD_RESPONSE', '云端返回异常，请稍后重试') from None
        if not result['ok']:
            code = result.get('error', {}).get('code', 'UNKNOWN')
            messages = {
                'CREDENTIAL_INVALID': '登录状态已失效，请重新获取',
                'UNAUTHORIZED': ('重置码无效或已过期，请让管理员重新生成'
                                 if path == '/v1/users/recover' else '管理凭证已失效，请使用登录码重置'),
                'INVITATION_INVALID': '邀请码无效或已使用',
                'INVITE_INVALID': '邀请码无效或已使用',
                'CANDIDATE_EXPIRED': '验证结果已过期，请重新获取登录状态',
                'RATE_LIMITED': '操作太频繁，请稍后重试',
                'SERVICE_NOT_CONFIGURED': '云端尚未完成配置，请联系维护者',
                'APP_CONFIG_UNAVAILABLE': '云端应用配置需要维护，请联系维护者',
                'CAPTURE_INCOMPLETE': '登录状态不完整，请重新连接手机获取',
                'UPSTREAM_UNAVAILABLE': '上游服务暂时无法连接，请稍后重试',
                'BINDING_PAUSED': '请先恢复每日任务再执行',
                'QUOTA_REACHED': '试用名额已满，请联系维护者',
                'BATCH_FULL': '领取批次已领完或已过期，请联系管理员获取新邀请码',
                'SLOT_FULL': '该执行区间名额已满，请选择其他区间',
                'SHARE_UNAVAILABLE': '本次设备信息不支持分享，请重新连接手机抓包',
                'NOTIFICATION_CONFLICT': 'Bark 和 Server 酱只能选择一个',
                'NOTIFICATION_NOT_CONFIGURED': '请先选择并保存一个推送渠道',
                'NOTIFICATION_TEST_FAILED': '配置已保存，但测试消息发送失败，请检查密钥',
                'SERVICE_UNAVAILABLE': '云端服务暂时不可用，请稍后重试',
                'CONFLICT': '状态正在更新，请刷新后重试',
                'NOT_FOUND': '记录不存在或已过期，请刷新后重试',
                'INVALID_REQUEST': '输入内容无效，请检查邀请码、登录码或设置',
                'RESULT_UNKNOWN': '操作结果尚未确认，请先刷新状态',
            }
            message = messages.get(code, '本次操作未完成，请刷新状态后重试')
            if code == 'NOT_FOUND' and path.startswith('/v1/claim/'):
                message = ('领取批次不存在或不属于当前云端环境，请确认邀请码由管理员提供，'
                           '并确保客户端使用对应的云端服务')
            raise CloudError(code, message)
        if method != 'GET':
            with self.lock:
                self.keys.pop(digest, None)
        return result.get('data')

    @classmethod
    def _timeout_budget(cls, method, path):
        return cls.RUN_TIMEOUT if method == 'POST' and path == '/v1/binding/runs' else cls.NORMAL_TIMEOUT

    @staticmethod
    def _read_response(response, deadline):
        """Read a response without allowing a slow body to exceed the request budget."""
        payload, errors = [], []

        def read_body():
            try:
                payload.append(response.read(CloudClient.MAX_RESPONSE_BYTES + 1))
            except Exception as error:  # pragma: no cover - socket implementations vary
                errors.append(error)

        worker = threading.Thread(target=read_body, daemon=True)
        worker.start()
        worker.join(max(0, deadline - time.monotonic()))
        if worker.is_alive():
            close = getattr(response, 'close', None)
            if close:
                close()
            raise TimeoutError()
        if errors:
            raise errors[0]
        return payload[0] if payload else b''
