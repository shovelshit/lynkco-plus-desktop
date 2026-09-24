"""Extract a complete session from a single successful authentication exchange."""

import re
import time
import json
from urllib.parse import parse_qs, urlsplit, unquote

from desktop.capture_context import normalize_share_context

AUTH_HOSTS = frozenset({"app-services.lynkco.com.cn", "app-api-gw-toc.lynkco.com"})
AUTH_PATHS = frozenset({
    "/auth/login/refresh",
    "/auth/login/mobileCodeLogin",
    "/auth/login/sliding/login",
})
SHARE_HOSTS = frozenset({"app-api-gw-toc.lynkco.com", "app-api-gw-toc.lynkco.com.cn"})
SHARE_PATH = "/app/v1/task/getShareCode"
SHARE_PATHS = frozenset({SHARE_PATH, SHARE_PATH + "/"})
VEHICLE_HOST = 'gric-hf-api.geely.com'
VEHICLE_PATH = '/ms-vehicle-account/api/v1.0/vehicle-detail'
VIN_PATTERN = re.compile(r'^[A-HJ-NPR-Z0-9]{17}$')


def valid_vin(value):
    value = clean_string(value, 17)
    value = value.upper() if value else None
    return value if value and VIN_PATTERN.fullmatch(value) else None


def parse_vehicle_capture(url, status, payload):
    """Extract only the VIN from a successful captured vehicle-detail response."""
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or parsed.hostname != VEHICLE_HOST or parsed.port not in (None, 443)
                or parsed.path != VEHICLE_PATH or status != 200 or not isinstance(payload, dict)
                or str(payload.get('code')) != '0'):
            return None
        data = payload.get('data')
        vin = valid_vin(data.get('vin')) if isinstance(data, dict) else None
        return {'vin': vin} if vin else None
    except (TypeError, ValueError, AttributeError):
        return None


def is_share_request(url):
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == 'https' and parsed.hostname in SHARE_HOSTS and
                parsed.port in (None, 443) and parsed.path.rstrip('/') == SHARE_PATH)
    except (TypeError, ValueError, AttributeError):
        return False


def display_path(value):
    path = urlsplit(value).path
    parts = path.split('/')
    sensitive = {'token', 'refreshtoken', 'authorization', 'password', 'mobile', 'phone', 'email'}
    result = []
    for index, part in enumerate(parts):
        decoded = unquote(part)
        previous = unquote(parts[index - 1]).lower() if index else ''
        hidden = (previous in sensitive or len(decoded) > 48 or '@' in decoded or
                  re.search(r'\d{6,}|[a-fA-F0-9]{24,}', decoded) or
                  any(c in decoded for c in '?;=\r\n'))
        result.append('[redacted]' if hidden else part)
    return '/'.join(result)[:1024]


def request_summary(url, method, status, outcome):
    parsed = urlsplit(url)
    return {'host': parsed.hostname or '',
            'path': display_path(url),
            'method': method if method in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS', 'CONNECT') else 'OTHER',
            'status': status, 'outcome': outcome, 'fields': {}}


def capture_summary(url, headers, status, response, platform_hint=None):
    """Never return query values, headers, response values, or exception text."""
    parsed = urlsplit(url)
    if parsed.hostname not in AUTH_HOSTS or parsed.path not in AUTH_PATHS:
        return None
    data = response.get('data') if isinstance(response, dict) else None
    dto = data.get('centerTokenDto') if isinstance(data, dict) else None
    dto = dto if isinstance(dto, dict) else {}
    query = parse_qs(parsed.query, max_num_fields=64)
    header = {str(k).lower(): v for k, v in headers.items()}
    def parameter(name):
        values = query.get(name, [])
        return values[0] if len(values) == 1 else None
    fields = {
        'token': bool(clean_string(dto.get('token'))),
        'refreshToken': bool(clean_string(dto.get('refreshToken')) or
                             (parsed.path == '/auth/login/refresh' and clean_string(parameter('refreshToken')))),
        'deviceId': bool(clean_string(parameter('deviceId') or parameter('hardwareDeviceId'), 256)),
        'platform': (clean_string(parameter('deviceType') or header.get('publicplatform') or platform_hint, 32) or '').upper() in ('IOS', 'ANDROID'),
    }
    outcome = ('http_error' if status != 200 else 'invalid_json' if not isinstance(response, dict)
               else 'business_error' if response.get('code') != 'success'
               else 'captured' if parse_session(url, headers, status, response, platform_hint)
               else 'incomplete')
    return {'host': parsed.hostname, 'path': parsed.path, 'status': status,
            'outcome': outcome, 'fields': fields}


def clean_string(value, maximum=4096):
    if not isinstance(value, str) or len(value) > maximum:
        return None
    value = value.strip()
    if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    return value


def _canonical_access_token(value):
    """Normalize the app's raw token fields to the value used by the controller."""
    value = clean_string(value)
    if not value:
        return None
    # The mobile client has emitted both `Bearer token` and `bearertoken`.
    match = re.match(r'^bearer(?:\s+)?', value, re.IGNORECASE)
    if match:
        value = value[match.end():].strip()
    return value or None


def parse_session(url, headers, status, response, platform_hint=None):
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in AUTH_HOSTS or parsed.port not in (None, 443):
            return None
        if parsed.path not in AUTH_PATHS or status != 200 or not isinstance(response, dict):
            return None
        if response.get("code") != "success":
            return None
        data = response.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("centerTokenDto"), dict):
            return None
        dto = data["centerTokenDto"]
        query = parse_qs(parsed.query, max_num_fields=64)
        header = {str(key).lower(): value for key, value in headers.items()}

        def parameter(name):
            values = query.get(name, [])
            return values[0] if len(values) == 1 else None

        token = _canonical_access_token(dto.get("token"))
        refresh = clean_string(dto.get("refreshToken"))
        if not refresh and parsed.path == "/auth/login/refresh":
            refresh = clean_string(parameter("refreshToken"))
        device = clean_string(parameter("deviceId") or parameter("hardwareDeviceId"), 256)
        platform = clean_string(parameter("deviceType") or header.get("publicplatform") or platform_hint, 32)
        platform = platform.upper() if platform else None
        if not token or not refresh or not device or platform not in {"IOS", "ANDROID"}:
            return None
        session = {"token": token, "refreshToken": refresh, "deviceId": device, "platform": platform,
                   "capturedAt": int(time.time() * 1000)}
        if parsed.path == '/auth/login/refresh':
            previous_refresh = clean_string(parameter('refreshToken'))
            if previous_refresh:
                session['previousRefreshToken'] = previous_refresh
        session['sourcePath'] = parsed.path
        expire_at = dto.get("expireAt")
        if isinstance(expire_at, str) and expire_at.strip().isdigit():
            expire_at = int(expire_at.strip())
        if isinstance(expire_at, (int, float)) and not isinstance(expire_at, bool) and expire_at > 0:
            session["expireAt"] = int(expire_at)
        for target, value in {
            "glDevId": header.get("gl_dev_id"),
            "deviceImei": header.get("imei"),
            "appVersion": parameter("appVersion") or header.get("appversioncode") or header.get("gl_app_version"),
            "appBuild": header.get("appversionname") or header.get("gl_app_build"),
            "deviceName": header.get("gl_dev_name"),
            "deviceModel": parameter("deviceModel") or header.get("gl_dev_model"),
            "deviceBrand": header.get("gl_dev_brand"),
            "osVersion": header.get("gl_os_version"),
            "devicePlatform": header.get("gl_dev_platform"),
            "userAgent": header.get("user-agent"),
        }.items():
            cleaned = clean_string(value, 1024 if target == 'userAgent' else 256)
            if cleaned:
                session[target] = cleaned
        return session
    except (ValueError, TypeError, AttributeError):
        return None


def _normalized_access_token(headers):
    values = []
    for key in ('token', 'svcsid'):
        value = headers.get(key)
        if value is None:
            continue
        value = _canonical_access_token(value)
        if not value:
            return None
        values.append(value)
    authorization = headers.get('authorization')
    if isinstance(authorization, str):
        match = re.match(r'^bearer(?:\s+)?(.+)$', authorization, re.IGNORECASE)
        if match:
            token = _canonical_access_token(match.group(1))
            if token:
                values.append(token)
    return values[0] if values and all(value == values[0] for value in values) else None


def _probe_article_id(risk_request_info):
    if not isinstance(risk_request_info, str) or len(risk_request_info.encode('utf-8')) > 8192:
        return None
    try:
        value = json.loads(risk_request_info)
        url = value.get('shareContentURL') or value.get('shareContentUrl') if isinstance(value, dict) else None
        parsed = urlsplit(url) if isinstance(url, str) else None
        values = parse_qs(parsed.query, max_num_fields=32).get('id', []) if parsed else []
        if len(values) == 1:
            return clean_string(values[0], 256)
        if isinstance(value, dict):
            return clean_string(value.get('contentId') or value.get('articleId'), 256)
        return None
    except (TypeError, ValueError, AttributeError):
        return None


def parse_share_capture(url, headers, status, response):
    """Extract only an allowlisted successful getShareCode request snapshot."""
    try:
        parsed = urlsplit(url)
        if (not is_share_request(url) or status != 200 or
                not isinstance(response, dict)):
            return None
        code = response.get('code')
        success = response.get('success')
        valid_code = str(code) in ('200', 'success', '0')
        if ('code' in response and not valid_code) or not (
                success is True or ('success' not in response and str(code) in ('200', 'success'))):
            return None
        share_code = response.get('data')
        if isinstance(share_code, dict):
            # App versions have returned both a scalar data value and an
            # object containing the same one-time code.
            for key in ('shareCode', 'share_code', 'code'):
                candidate = share_code.get(key)
                if isinstance(candidate, str):
                    share_code = candidate
                    break
        if not clean_string(share_code, 4096):
            return None
        header = {str(key).lower(): value for key, value in headers.items()}
        token = _normalized_access_token(header)
        if not token:
            return None
        security_value = (header.get('sweet_security_info') or
                          header.get('sweet-security-info') or
                          header.get('sweetsecurityinfo'))
        security = json.loads(security_value) if isinstance(security_value, str) else None
        share_context = normalize_share_context({
            'capturedAt': int(time.time() * 1000),
            'headers': {key: value for key, value in header.items()
                        if key in ('user-agent', 'gl_dev_name', 'gl_dev_model', 'gl_dev_brand',
                                   'gl_dev_platform', 'gl_os_version', 'gl_app_version', 'gl_app_build',
                                   'gl_dev_id', 'appversion', 'appversioncode', 'appversionname',
                                   'publicplatform', 'os', 'gl_user_id', 'risk_type')},
            'security': security,
        })
        if not share_context:
            return None
        event = {'token': token, 'shareContext': share_context}
        article_id = _probe_article_id(header.get('risk_request_info'))
        if article_id:
            event['probeArticleId'] = article_id
        return event
    except (TypeError, ValueError, AttributeError, UnicodeError):
        return None
