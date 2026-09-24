"""Ephemeral LAN pairing and proxy process lifecycle."""

import html
import ipaddress
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from desktop.http_server import LocalHTTPServer
import psutil


def network_addresses():
    import psutil
    result = []
    stats = psutil.net_if_stats()
    for name, entries in psutil.net_if_addrs().items():
        if name.startswith(('utun', 'lo', 'docker', 'veth', 'bridge', 'awdl', 'llw')):
            continue
        if name not in stats or not stats[name].isup:
            continue
        for entry in entries:
            if entry.family == socket.AF_INET:
                ip = ipaddress.ip_address(entry.address)
                if not (ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved):
                    result.append({'name': name, 'address': str(ip)})
    return result


class ProxyManager:
    def __init__(self, state_dir, callback_url, callback_token):
        self.root = Path(state_dir)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.callback_url, self.callback_token = callback_url, callback_token
        self.process = self.server = None
        self.lock = threading.RLock()
        self.address = self.peer = self.pair_token = self.platform = None
        self.proxy_epoch = None
        self.port = 0
        self.last_activity_at = None
        self.capture_enabled = False
        self.network_changed = False
        self.needs_reconfigure = False
        self._monitor_network_change = True
        self.state_path = self.root / 'proxy-state.json'
        self.ports_path = self.root / 'ports.json'
        if self.ports_path.exists():
            ports = json.loads(self.ports_path.read_text())
        else:
            ports = {'proxy': 55255, 'certificate': 55268}
            self.ports_path.write_text(json.dumps(ports))
            if os.name != 'nt':
                self.ports_path.chmod(0o600)
        if any(type(ports.get(k)) is not int or not 1024 <= ports[k] <= 65535 for k in ('proxy', 'certificate')) or ports['proxy'] == ports['certificate']:
            raise ValueError('固定端口配置无效')
        self.fixed_ports = ports

    def _save_ports(self):
        self.ports_path.write_text(json.dumps(self.fixed_ports))
        if os.name != 'nt':
            self.ports_path.chmod(0o600)

    def _choose_proxy_port(self, address):
        """Let the OS choose an available LAN port for each proxy session."""
        certificate_port = self.fixed_ports['certificate']
        for _ in range(8):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind((address, 0))
                port = probe.getsockname()[1]
            if port != certificate_port:
                return port
        raise ValueError('无法分配可用的手机代理端口，请重试')

    def _write_state(self):
        temporary = self.state_path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'peerIp': self.peer, 'captureEnabled': self.capture_enabled,
                                         'proxyEpoch': self.proxy_epoch}))
        if os.name != 'nt':
            temporary.chmod(0o600)
        temporary.replace(self.state_path)

    def accepts_peer(self, peer):
        with self.lock:
            return bool(self.process and self.process.poll() is None and self.capture_enabled and self.peer and self.peer == peer)

    def disable_capture(self):
        with self.lock:
            self.capture_enabled = False
            self._write_state()

    def note_activity(self):
        """Record a request accepted from the paired phone without retaining its data."""
        with self.lock:
            self.last_activity_at = int(time.time() * 1000)

    def _active_connections(self):
        if not self.port:
            return 0
        try:
            connections = psutil.net_connections(kind='tcp')
        except (OSError, psutil.AccessDenied):
            return 0
        active = 0
        for connection in connections:
            local = getattr(connection, 'laddr', None)
            remote = getattr(connection, 'raddr', None)
            local_port = getattr(local, 'port', local[1] if isinstance(local, tuple) and len(local) > 1 else None)
            remote_host = getattr(remote, 'ip', remote[0] if isinstance(remote, tuple) and remote else None)
            if (local_port == self.port and remote and remote_host == self.peer
                    and getattr(connection, 'status', None) == 'ESTABLISHED'):
                active += 1
        return active

    def start(self, address, platform, proxy_port=None):
        with self.lock:
            if self.process or self.server:
                raise ValueError('已有手机连接流程，请先关闭手机代理并断开连接')
            if address not in {item['address'] for item in network_addresses()}:
                raise ValueError('请选择当前电脑的 Wi-Fi 或有线网络地址')
            # The port is deliberately not user-configurable.  A fresh OS-selected
            # port avoids stale/conflicting local settings while the runtime state
            # remains the single source of truth for the phone configuration.
            selected_proxy_port = self._choose_proxy_port(address)
            selected_ports = dict(self.fixed_ports, proxy=selected_proxy_port)
            for port in selected_ports.values():
                try:
                    with socket.socket() as probe:
                        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                        probe.bind((address, port))
                except OSError:
                    raise ValueError(f'端口 {port} 已被占用，请重试') from None
            self.address, self.platform = address, platform
            self.proxy_epoch = secrets.token_urlsafe(18)
            self._monitor_network_change = not ipaddress.ip_address(address).is_loopback
            self.peer = None
            self.pair_token = secrets.token_urlsafe(24)
            self.capture_enabled = True
            self.last_activity_at = None
            self.network_changed = False
            self.needs_reconfigure = False
            self._write_state()
            self.port = selected_proxy_port
            addon = Path(__file__).with_name('capture_addon.py')
            packaged = getattr(sys, 'frozen', False)
            command = [sys.executable] + ([] if packaged else ['-m', 'desktop.launcher'])
            command += ['--proxy', '--listen-host', address, '--listen-port', str(self.port), '-q',
                        '--set', 'flow_detail=0', '--set', 'termlog_verbosity=error', '--set', 'connection_strategy=lazy',
                        '--set', 'confdir=' + str(self.root / 'ca'), '--set', 'block_private=false',
                        '-s', str(addon)]
            environment = dict(os.environ, LYNKCO_CALLBACK_URL=self.callback_url, LYNKCO_CALLBACK_TOKEN=self.callback_token,
                               LYNKCO_PROXY_STATE=str(self.state_path), LYNKCO_PHONE_PLATFORM=platform,
                               LYNKCO_PROXY_PARENT=json.dumps({
                                   'pid': os.getpid(),
                                   'created': psutil.Process(os.getpid()).create_time(),
                               }))
            self.process = subprocess.Popen(command, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL, cwd=str(Path(__file__).resolve().parent.parent))
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    self.process = None
                    raise ValueError('手机代理启动失败，请检查防火墙或重新启动助手')
                try:
                    with socket.create_connection((address, self.port), timeout=.2):
                        break
                except OSError:
                    time.sleep(.1)
            else:
                self.stop()
                raise ValueError('手机代理启动超时，请重试')
            manager = self

            class CertificateHandler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def do_GET(self):
                    expected_host = f'{manager.address}:{self.server.server_port}'
                    if self.headers.get('Host') != expected_host or self.path not in (f'/{manager.pair_token}', f'/{manager.pair_token}/certificate.cer'):
                        self.send_error(404)
                        return
                    peer = self.client_address[0]
                    with manager.lock:
                        if manager.peer and manager.peer != peer:
                            self.send_error(403)
                            return
                        manager.peer = peer
                        manager._write_state()
                    if self.path.endswith('/certificate.cer'):
                        ca = manager.root / 'ca' / 'mitmproxy-ca-cert.cer'
                        if not ca.is_file():
                            self.send_error(503)
                            return
                        body, mime = ca.read_bytes(), 'application/x-x509-ca-cert'
                    else:
                        body = (f'<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                                f'<title>领+ · 手机连接</title><body><h1>领+</h1><p>手机已配对。</p>'
                                f'<p><a href="/{html.escape(manager.pair_token)}/certificate.cer">下载本机证书</a></p>'
                                '<p>已安装并信任过本机证书，无需重复安装。</p>'
                                '<p>iPhone：安装描述文件后，在「设置 → 通用 → 关于本机 → 证书信任设置」中开启完全信任。</p>'
                                '<p>安卓：在系统安全设置中安装 CA 证书，具体入口因机型而异。</p>'
                                f'<p>Wi-Fi 手动代理服务器：{html.escape(manager.address)}<br>端口：{manager.port}</p>'
                                '<p>完成后打开对应 App，再查看电脑上的绑定进度。绑定结束后只需关闭 Wi-Fi 代理。</p></body></html>').encode()
                        mime = 'text/html; charset=utf-8'
                    self.send_response(200)
                    self.send_header('Content-Type', mime)
                    self.send_header('Content-Length', str(len(body)))
                    if self.path.endswith('/certificate.cer'):
                        self.send_header('Content-Disposition', 'attachment; filename="LynkCoHelper-CA.cer"')
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('Referrer-Policy', 'no-referrer')
                    self.send_header('X-Content-Type-Options', 'nosniff')
                    self.end_headers()
                    self.wfile.write(body)

            try:
                self.server = LocalHTTPServer((address, self.fixed_ports['certificate']), CertificateHandler)
                self.server.daemon_threads = True
                threading.Thread(target=self.server.serve_forever, daemon=True).start()
            except OSError:
                self.stop()
                raise ValueError('证书下载服务启动失败，请检查网络') from None
            self.fixed_ports['proxy'] = selected_proxy_port
            self._save_ports()
            return self.public_state()

    def restart(self, address, platform, proxy_port=None):
        """Rebind the local proxy after the computer changes Wi-Fi networks."""
        self.stop()
        return self.start(address, platform, proxy_port)

    def _release_resources_locked(self):
        process, server = self.process, self.server
        self.process = self.server = None
        self.capture_enabled = False
        self.peer = self.pair_token = self.proxy_epoch = None
        self.last_activity_at = None
        self._write_state()
        return process, server

    @staticmethod
    def _close_resources(process, server):
        if server:
            server.shutdown()
            server.server_close()
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def _invalidate_network(self, observed_process, observed_address):
        with self.lock:
            if (self.needs_reconfigure or self.process is not observed_process
                    or self.address != observed_address
                    or not self.process or self.process.poll() is not None):
                return
            self.network_changed = True
            self.needs_reconfigure = True
            resources = self._release_resources_locked()
        self._close_resources(*resources)

    def stop(self):
        with self.lock:
            self.network_changed = False
            self.needs_reconfigure = False
            resources = self._release_resources_locked()
        self._close_resources(*resources)

    def public_state(self):
        with self.lock:
            running = bool(self.process and self.process.poll() is None)
            process = self.process
            address = self.address
        if running and address and self._monitor_network_change:
            try:
                network_changed = address not in {item['address'] for item in network_addresses()}
            except OSError:
                network_changed = False
            if network_changed:
                self._invalidate_network(process, address)
        with self.lock:
            running = bool(self.process and self.process.poll() is None)
            paired = bool(self.peer)
            active_connections = self._active_connections() if running and paired else 0
            now = int(time.time() * 1000)
            recent = bool(self.last_activity_at and now - self.last_activity_at <= 30_000)
            phone_state = 'stopped' if not running else 'not_paired' if not paired else 'active' if active_connections else 'recent' if recent else 'idle'
            return {'running': running, 'address': self.address, 'port': self.port or None, 'paired': paired,
                    'phoneProxyState': phone_state, 'networkChanged': self.network_changed,
                    'needsReconfigure': self.needs_reconfigure,
                    'phoneRequests': {'active': active_connections, 'lastAt': self.last_activity_at},
                    'captureEnabled': self.capture_enabled and running,
                    'pairUrl': f'http://{self.address}:{self.server.server_port}/{self.pair_token}' if running and self.server else None,
                    'proxyEpoch': self.proxy_epoch if running else None}
