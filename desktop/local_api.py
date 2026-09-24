"""Loopback-only management surface with independent capture authorization."""

import hmac
import io
import json
import threading
import secrets
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlsplit
from desktop.http_server import LocalHTTPServer


def make_server(controller, web_root, callback_token, port=0):
    session = {'token': None, 'last_activity': 0.0}
    session_lock = threading.RLock()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, value, content_type='application/json; charset=utf-8'):
            body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' data: blob: https:; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(body)

        def discard_request_body(self):
            """Drain a bounded body before rejecting auth so keep-alive stays usable on Windows."""
            if self.command != 'POST':
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
            except (TypeError, ValueError):
                return
            if 0 < length <= 32768:
                self.rfile.read(length)

        def dispatch(self):
            origin = 'http://127.0.0.1:' + str(self.server.server_port)
            if self.headers.get('Host') != origin[7:] or self.headers.get('Origin', origin) != origin:
                return self.send(403, {'ok': False})
            path = urlsplit(self.path).path
            internal = path == '/internal/capture'
            if path.startswith(('/api/', '/internal/')):
                with session_lock:
                    expected = callback_token if internal else session['token']
                if not internal and (not controller.identity or not expected):
                    self.discard_request_body()
                    return self.send(401, {'ok': False, 'error': '客户端已锁定，请重新登录'})
                if not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + expected):
                    self.discard_request_body()
                    return self.send(401, {'ok': False})
            elif self.command == 'GET':
                with session_lock:
                    if not controller.identity or not session['token']:
                        return self.send(401, {'ok': False, 'error': '客户端已锁定，请重新登录'})
                assets = {'/': ('index.html', 'text/html; charset=utf-8'), '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                          '/style.css': ('style.css', 'text/css; charset=utf-8'), '/lucide.js': ('lucide.js', 'text/javascript; charset=utf-8')}
                if path in assets:
                    name, mime = assets[path]
                    file = web_root / name
                    if file.is_file():
                        return self.send(200, file.read_bytes(), mime)
                return self.send(404, {'ok': False})
            body = {}
            if self.command == 'POST':
                if self.headers.get_content_type() != 'application/json' or self.headers.get('Transfer-Encoding'):
                    return self.send(415, {'ok': False})
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= 32768:
                        return self.send(413, {'ok': False})
                    body = json.loads(self.rfile.read(length))
                    if not isinstance(body, dict):
                        raise ValueError()
                except (ValueError, UnicodeError):
                    return self.send(400, {'ok': False, 'error': '请求格式无效'})
            try:
                result = None
                if self.command == 'GET' and path == '/api/status':
                    result = controller.public_state()
                    if isinstance(result, dict):
                        from desktop.version import current_version
                        result['clientVersion'] = current_version()
                elif self.command == 'POST' and path == '/api/activity':
                    with session_lock:
                        session['last_activity'] = time.monotonic()
                    result = {'updated': True}
                elif self.command == 'GET' and path == '/api/networks':
                    from desktop.proxy import network_addresses
                    result = network_addresses()
                elif self.command == 'GET' and path == '/api/capture/qr':
                    proxy_state = controller.proxy.public_state() if controller.proxy else {}
                    pair_url = proxy_state.get('pairUrl')
                    if not pair_url:
                        if proxy_state.get('needsReconfigure') or proxy_state.get('networkChanged'):
                            raise ValueError('网络已变化，请重新绑定手机')
                        raise ValueError('请先开始连接手机')
                    import qrcode
                    import qrcode.image.svg
                    buffer = io.BytesIO()
                    qrcode.make(pair_url, image_factory=qrcode.image.svg.SvgPathImage).save(buffer)
                    return self.send(200, buffer.getvalue(), 'image/svg+xml')
                elif self.command == 'GET' and path == '/api/capture/access-token-once':
                    result = controller.consume_capture_access_token()
                elif self.command == 'GET' and path == '/api/capture/probe-input':
                    result = controller.probe_input()
                elif self.command == 'POST' and path == '/api/capture/access-token-once':
                    # The browser may cache this in sessionStorage; it is never part of public_state.
                    result = controller.consume_capture_access_token()
                elif self.command == 'POST':
                    if internal:
                        proxy = controller.proxy
                        if not proxy or not proxy.accepts_peer(body.get('peer')):
                            return self.send(403, {'ok': False})
                        controller.record_capture(body.get('summary'))
                        accepted = controller.receive_capture(body.get('session'), body.get('requestStartedAt'), body.get('proxyEpoch'))
                        share_accepted = controller.receive_share_capture(body.get('share'), body.get('proxyEpoch'))
                        vehicle_accepted = controller.receive_vehicle_capture(body.get('vehicle'), body.get('proxyEpoch'))
                        result = {'accepted': accepted or share_accepted or vehicle_accepted}
                    elif path == '/api/refresh':
                        result = controller.refresh()
                    elif path == '/api/candidates/prepare':
                        if body.get('consent') is not True:
                            raise ValueError('请确认将登录状态上传到云端')
                        result = controller.prepare()
                    elif path == '/api/candidates/activate':
                        result = controller.activate(body)
                    elif path == '/api/binding/settings':
                        result = controller.settings(body)
                    elif path == '/api/binding/notification-test':
                        result = controller.test_notification()
                    elif path == '/api/binding/run':
                        result = controller.run(body.get('mode', 'job'))
                    elif path == '/api/binding/delete':
                        if body.get('confirmed') is not True:
                            raise ValueError('请确认解除绑定')
                        result = controller.delete()
                    elif path == '/api/history':
                        result = controller.history(body.get('cursor', ''))
                    elif path == '/api/capture/start':
                        if not controller.identity:
                            raise ValueError('请先连接云端账号')
                        if body.get('platform') not in ('IOS', 'ANDROID'):
                            raise ValueError('请选择手机系统')
                        result = controller.proxy.start(body.get('address'), body['platform'])
                        controller.begin_capture(body['platform'], result.get('proxyEpoch'))
                    elif path == '/api/capture/rebind':
                        if not controller.identity:
                            raise ValueError('请先连接云端账号')
                        if not controller.proxy:
                            raise ValueError('手机代理尚未启动，请先开始连接')
                        if body.get('platform') not in ('IOS', 'ANDROID'):
                            raise ValueError('请选择手机系统')
                        result = controller.proxy.restart(body.get('address'), body['platform'])
                        controller.begin_capture(body['platform'], result.get('proxyEpoch'))
                    elif path == '/api/capture/reset':
                        result = controller.reset_capture()
                    elif path == '/api/capture/stop':
                        if body.get('proxyRemoved') is not True:
                            raise ValueError('请先关闭手机 Wi-Fi 代理')
                        controller.proxy.stop()
                        controller.stop_capture()
                        result = {'stopped': True}
                    elif path == '/api/capture/force-stop':
                        result = controller.force_stop_proxy()
                    elif path == '/api/quit':
                        result = controller.force_stop_proxy()
                        threading.Thread(target=self.server.shutdown, daemon=True).start()
                    else:
                        return self.send(404, {'ok': False})
                else:
                    return self.send(404, {'ok': False})
                return self.send(200, {'ok': True, 'data': result})
            except ValueError as error:
                return self.send(400, {'ok': False, 'error': str(error)})
            except Exception:
                return self.send(500, {'ok': False, 'error': '操作未完成，请重试或重新打开助手'})

        def do_GET(self):
            self.dispatch()

        def do_POST(self):
            self.dispatch()

    server = LocalHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    def issue_browser_session():
        if not controller.identity:
            raise ValueError('请先登录客户端')
        with session_lock:
            session['token'] = secrets.token_urlsafe(32)
            session['last_activity'] = time.monotonic()
            return session['token']

    def lock_browser_session():
        with session_lock:
            session['token'] = None
            session['last_activity'] = 0.0

    def browser_last_activity():
        with session_lock:
            return session['last_activity']

    server.issue_browser_session = issue_browser_session
    server.lock_browser_session = lock_browser_session
    server.browser_last_activity = browser_last_activity
    return server
