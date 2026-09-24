"""Shared allowlists and completeness checks for locally captured context."""

import math


DEVICE_FIELDS = ('appVersion', 'appBuild', 'deviceName', 'deviceModel', 'deviceBrand',
                 'osVersion', 'glDevId', 'devicePlatform', 'userAgent')
SHARE_HEADERS = frozenset(('user-agent', 'gl_dev_name', 'gl_dev_model', 'gl_dev_brand',
                          'gl_dev_platform', 'gl_os_version', 'gl_app_version', 'gl_app_build',
                          'gl_dev_id', 'appversion', 'appversioncode', 'appversionname',
                          'publicplatform', 'os', 'gl_user_id', 'risk_type'))
SECURITY_FIELDS = frozenset(('appVersion', 'platform', 'battery', 'isCharging', 'isSetProxy',
                            'isUsbDebug', 'isMockLocation', 'isRoot', 'appSignature', 'channel',
                            'screenResolution', 'brand', 'model', 'geelyDeviceId', 'os',
                            'osVersion', 'androidVersion', 'networkType', 'ip', 'wifiName',
                            'wifiSignalLevel', 'isLbsEnabled', 'lbsLatitude', 'lbsLongitude',
                            'deviceToken', 'deviceUUID', 'isJailbreak', 'isLBSEnabled',
                            'isUsingVpn', 'os_version', 'ua', 'wifiMAC'))


def valid_text(value, maximum=256, empty=False, ascii_only=False):
    if not isinstance(value, str) or len(value.encode('utf-8')) > maximum:
        return False
    if not empty and not value.strip():
        return False
    return all(32 <= ord(char) != 127 and (not ascii_only or ord(char) <= 126) for char in value)


def positive_number(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def normalize_share_context(value):
    """Return a validated deep scalar copy; no runtime auth or unknown fields."""
    if not isinstance(value, dict) or not positive_number(value.get('capturedAt')):
        return None
    headers, security = value.get('headers'), value.get('security')
    if not isinstance(headers, dict) or not isinstance(security, dict) or not security:
        return None
    if not valid_text(headers.get('user-agent'), 1024, ascii_only=True):
        return None
    for key, item in headers.items():
        if key not in SHARE_HEADERS or not valid_text(item, 1024):
            return None
    for key, item in security.items():
        if key not in SECURITY_FIELDS:
            return None
        if isinstance(item, str):
            if not valid_text(item, 1024, empty=True):
                return None
        elif type(item) is bool:
            pass
        elif type(item) in (int, float):
            if not math.isfinite(item):
                return None
        else:
            return None
    return {'capturedAt': value['capturedAt'], 'headers': dict(headers), 'security': dict(security)}


def capture_readiness(session):
    session = session if isinstance(session, dict) else {}
    login = (valid_text(session.get('token'), 4096) and valid_text(session.get('refreshToken'), 4096)
             and valid_text(session.get('deviceId')) and session.get('platform') in ('IOS', 'ANDROID'))
    missing_device = [key for key in DEVICE_FIELDS
                      if not valid_text(session.get(key), 1024 if key == 'userAgent' else 256,
                                        ascii_only=key == 'userAgent')]
    share = normalize_share_context(session.get('shareContext')) is not None
    from desktop.capture import valid_vin
    vehicle = session.get('vehicle')
    vehicle_ready = isinstance(vehicle, dict) and valid_vin(vehicle.get('vin')) is not None
    return {'readiness': {'login': login, 'device': not missing_device, 'share': share, 'vehicle': vehicle_ready},
            'missing': {'device': missing_device, 'share': [] if share else ['shareContext'],
                        'vehicle': [] if vehicle_ready else ['vin']}}
