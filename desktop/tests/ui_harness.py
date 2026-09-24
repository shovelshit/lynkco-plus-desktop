"""Browser acceptance fixture. Never bundled or imported by the desktop app."""

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from desktop.binding import Controller
from desktop.local_api import make_server
import desktop.proxy


def fixture_mode():
    path = Path('/tmp/lynkco-ui-test-mode')
    return path.read_text().strip() if path.exists() else ''


class Store:
    def has_license(self):
        return False

    def load(self, login_code):
        return None

    def save(self, value, login_code):
        pass


class Cloud:
    base_url = 'https://lynkco.ltools.asia'
    binding = None
    runs = []

    def request(self, method, path, body=None, token=None, deadline=None):
        if path == '/health':
            return {'service': 'lynkco-helper', 'configured': True}
        if path in ('/v1/owners', '/v1/owners/recover', '/v1/users/recover'):
            return {'userId': 'fixture-owner', 'managementToken': 'fixture-management', 'loginCode': 'fixture-login-code-save-me'}
        if path.startswith('/v1/claim/'):
            return {'userId': 'fixture-owner', 'managementToken': 'fixture-management', 'loginCode': 'fixture-login-code-save-me'}
        if path == '/v1/users/me':
            return {'userId': 'fixture-owner'}
        if path == '/v1/schedule-windows':
            mode = fixture_mode()
            if mode == 'quota-unavailable':
                raise ValueError('暂时无法读取剩余名额')
            items = []
            for hour in range(0, 24, 2):
                value = f'{hour:02d}:00-{(hour + 2) % 24:02d}:00'
                current = bool(self.binding and self.binding['scheduleTime'] == value)
                used = 10 if hour == 10 or (mode == 'quota-full' and hour == 8) else int(current)
                items.append({'value': value, 'limit': 10, 'used': used, 'remaining': 10 - used, 'current': current})
            return {'items': items}
        if path == '/v1/binding-candidates':
            mode = fixture_mode()
            if mode == 'verification-delayed':
                time.sleep(.75)
            if mode == 'verification-failed':
                raise ValueError('fixture verification unavailable')
            return {'id': 'fixture-candidate', 'expiresAt': (time.time() + (-1 if mode == 'expired' else 600)) * 1000,
                    'preview': {'verified': True, 'displayName': '测试账号'}, 'capabilities': {'share': True}}
        if path.endswith('/activate'):
            mode = fixture_mode()
            if mode == 'activation-failure':
                raise ValueError('云端暂时不可用，请稍后重试')
            self.binding = {'id': 'fixture-binding', 'label': body['label'], 'status': 'active', 'scheduleTime': body.get('scheduleTime', '08:00-10:00'),
                            'doShare': body['doShare'], 'canShare': True, 'nextRunAt': time.time() * 1000 + 3600000,
                            'inventory': {
                                'points': '780', 'cards': 12, 'energy': 18, 'days': 16,
                                'details': [
                                    {'key': 'memberLevel.levelName', 'label': '会员等级', 'value': '银色探索者',
                                     'iconUrl': 'https://github.githubassets.com/favicons/favicon.png'},
                                    {'key': 'memberLevel.growthValue', 'label': '成长值', 'value': '1260'},
                                ],
                                'medals': [
                                    {'name': '连续签到', 'description': '连续签到 7 天',
                                     'iconUrl': 'https://github.githubassets.com/favicons/favicon.png'},
                                    {'name': '分享达人'},
                                ],
                            }}
            return self.binding
        if path.startswith('/v1/binding/runs'):
            if method == 'POST':
                today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
                self.runs = [{'id': 'fixture-run', 'businessDate': today, 'status': 'completed', 'startedAt': time.time()*1000,
                              'finishedAt': time.time()*1000, 'pointsBefore': '2680', 'pointsAfter': '2680', 'signStatus': 'already_signed',
                              'shareStatus': 'skipped', 'message': None, 'errorCode': None}]
                return {'id': 'fixture-run', 'status': 'completed'}
            return {'items': self.runs, 'nextCursor': None}
        if path == '/v1/binding/notifications/test':
            return {'sent': True, 'channel': next((name for name, config in self.binding.get('notifications', {}).items() if config.get('enabled')), None)}
        if path == '/v1/binding':
            if method == 'PATCH':
                settings = {k:v for k,v in body.items() if k != 'notifications'}
                self.binding.update(settings)
                for channel, config in body.get('notifications', {}).items():
                    previous = self.binding.setdefault('notifications', {}).get(channel, {'enabled':False,'configured':False})
                    self.binding['notifications'][channel] = {'enabled':False,'configured':False} if config.get('clear') else {
                        'enabled': config.get('enabled',previous['enabled']), 'configured': bool(config.get('key')) or previous['configured']}
            if method == 'DELETE':
                self.binding = None
                self.runs = []
                return {'deleted': True}
            return self.binding
        raise ValueError('Unknown fixture request')


class Proxy:
    running = False
    capture_enabled = False

    def start(self, address, platform, proxy_port=None):
        self.running = self.capture_enabled = True
        def capture():
            controller.record_capture({
                'host': 'app-services.lynkco.com.cn', 'path': '/auth/login/refresh', 'method': 'POST',
                'status': 200, 'outcome': 'captured', 'id': 'fixture-capture',
                'fields': {'token': True, 'refreshToken': True, 'deviceId': True, 'platform': True},
            })
            controller.receive_capture({'token': 'fixture-token', 'refreshToken': 'fixture-refresh',
                                        'deviceId': 'fixture-device', 'platform': platform,
                                        'appVersion': '4.2.7', 'appBuild': '427', 'deviceName': 'fixture',
                                        'deviceModel': 'fixture-model', 'deviceBrand': 'fixture-brand',
                                        'osVersion': '17', 'glDevId': 'fixture-gl', 'devicePlatform': platform.lower(),
                                        'userAgent': 'Lynkco/4.2.7'})
            controller.receive_share_capture({'token': 'fixture-token', 'shareContext': {
                'capturedAt': int(time.time() * 1000),
                'headers': {'user-agent': 'Lynkco/4.2.7', 'gl_dev_id': 'fixture-gl'},
                'security': {'appVersion': '4.2.7', 'platform': platform.lower()},
            }, 'probeArticleId': 'fixture-article'})
        threading.Timer(.5, capture).start()
        return self.public_state()

    def restart(self, address, platform, proxy_port=None):
        self.stop()
        return self.start(address, platform, proxy_port)

    def stop(self):
        self.running = self.capture_enabled = False

    def disable_capture(self):
        self.capture_enabled = False

    def note_activity(self):
        pass

    def public_state(self):
        return {'running': self.running, 'captureEnabled': self.capture_enabled, 'paired': self.running,
                'address': '192.168.1.10', 'port': 8080, 'networkChanged': False,
                'needsReconfigure': False,
                'phoneRequests': {'active': 0, 'lastAt': None},
                'pairUrl': 'http://192.168.1.10:8081/fixture' if self.running else None}


controller = Controller(Cloud(), Store())
controller.proxy = Proxy()
server = make_server(controller, Path(__file__).resolve().parents[2] / 'desktop' / 'web', 'ui-callback-token', 18743)
controller.claim('fixture-claim-code')
browser_token = server.issue_browser_session()
with patch('desktop.proxy.network_addresses', return_value=[{'name': 'Wi-Fi', 'address': '192.168.1.10'}]):
    server.serve_forever()
