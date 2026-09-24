import json
import tempfile
import unittest
import socket
import os
import threading
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import urlopen

from desktop.proxy import ProxyManager, network_addresses


class ProxyLifecycleTests(unittest.TestCase):
    def test_source_proxy_uses_python_module_even_with_stale_flet_environment(self):
        import sys
        with tempfile.TemporaryDirectory() as directory:
            manager = ProxyManager(directory, 'http://127.0.0.1:1/internal/capture', 'fixture')
            child = MagicMock()
            child.poll.return_value = None
            with patch.dict(os.environ, {'FLET_PLATFORM': 'macos'}), \
                    patch.object(sys, 'frozen', False, create=True), \
                    patch('desktop.proxy.network_addresses', return_value=[{'name': 'fixture', 'address': '127.0.0.1'}]), \
                    patch('desktop.proxy.subprocess.Popen', return_value=child) as launch, \
                    patch('desktop.proxy.socket.create_connection', return_value=MagicMock()):
                manager.start('127.0.0.1', 'IOS')
            try:
                self.assertEqual(launch.call_args.args[0][:4], [sys.executable, '-m', 'desktop.launcher', '--proxy'])
            finally:
                manager.stop()

    def test_packaged_proxy_starts_from_app_executable_without_python_module(self):
        import sys
        with tempfile.TemporaryDirectory() as directory:
            manager = ProxyManager(directory, 'http://127.0.0.1:1/internal/capture', 'fixture')
            child = MagicMock()
            child.poll.return_value = None
            with patch.object(sys, 'frozen', True, create=True), \
                    patch.object(sys, 'executable', '/Applications/LynkCoHelper.app/Contents/MacOS/LynkCoHelper'), \
                    patch('desktop.proxy.network_addresses', return_value=[{'name': 'fixture', 'address': '127.0.0.1'}]), \
                    patch('desktop.proxy.subprocess.Popen', return_value=child) as launch, \
                    patch('desktop.proxy.socket.create_connection', return_value=MagicMock()):
                manager.start('127.0.0.1', 'IOS')
            try:
                self.assertEqual(launch.call_args.args[0][:2], ['/Applications/LynkCoHelper.app/Contents/MacOS/LynkCoHelper', '--proxy'])
            finally:
                manager.stop()

    def test_start_chooses_an_ephemeral_proxy_port_instead_of_user_port(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'ports.json').write_text(json.dumps({'proxy': 55255, 'certificate': 55268}))
            manager = ProxyManager(directory, 'http://127.0.0.1:1/internal/capture', 'fixture')
            child = MagicMock()
            child.poll.return_value = None
            with patch('desktop.proxy.network_addresses', return_value=[{'name': 'fixture', 'address': '127.0.0.1'}]), \
                    patch('desktop.proxy.subprocess.Popen', return_value=child) as launch, \
                    patch('desktop.proxy.socket.create_connection', return_value=MagicMock()), \
                    patch.object(manager, '_choose_proxy_port', return_value=49152) as choose:
                state = manager.start('127.0.0.1', 'IOS', proxy_port=55255)
            try:
                self.assertEqual(state['port'], 49152)
                choose.assert_called_once_with('127.0.0.1')
                command = launch.call_args.args[0]
                self.assertEqual(command[command.index('--listen-port') + 1], '49152')
            finally:
                manager.stop()

    def test_start_ignores_legacy_configured_proxy_port(self):
        with tempfile.TemporaryDirectory() as directory:
            ports = []
            for _ in range(2):
                with socket.socket() as probe:
                    probe.bind(('127.0.0.1', 0))
                    ports.append(probe.getsockname()[1])
            (Path(directory) / 'ports.json').write_text(json.dumps({'proxy': ports[0], 'certificate': ports[1]}))
            manager = ProxyManager(directory, 'http://127.0.0.1:1/internal/capture', 'fixture')
            child = MagicMock()
            child.poll.return_value = None
            with patch('desktop.proxy.network_addresses', return_value=[{'name':'fixture','address':'127.0.0.1'}]), \
                    patch('desktop.proxy.subprocess.Popen', return_value=child) as launch, \
                    patch('desktop.proxy.socket.create_connection', return_value=MagicMock()), \
                    patch.object(manager, '_choose_proxy_port', return_value=ports[0] + 2) as choose:
                state = manager.start('127.0.0.1', 'IOS', proxy_port=ports[0] + 2)
            try:
                self.assertEqual(state['port'], ports[0] + 2)
                choose.assert_called_once_with('127.0.0.1')
                command = launch.call_args.args[0]
                self.assertEqual(command[command.index('--listen-port') + 1], str(ports[0] + 2))
            finally:
                manager.stop()

    def test_start_does_not_use_certificate_port_for_random_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'ports.json').write_text(json.dumps({'proxy': 55255, 'certificate': 55268}))
            manager = ProxyManager(directory, 'http://127.0.0.1:1/internal/capture', 'fixture')
            with patch.object(manager, '_choose_proxy_port', return_value=55267):
                self.assertEqual(manager._choose_proxy_port('127.0.0.1'), 55267)

    def test_public_state_reports_phone_request_activity_separately_from_proxy_process(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = ProxyManager(directory, 'http://127.0.0.1:1/internal/capture', 'fixture')
            manager.process = MagicMock()
            manager.process.poll.return_value = None
            manager.peer = '127.0.0.1'
            with patch('desktop.proxy.network_addresses', return_value=[{'address': '127.0.0.1'}]), \
                    patch('desktop.proxy.psutil.net_connections', return_value=[]):
                state = manager.public_state()
            self.assertEqual(state['phoneProxyState'], 'idle')
            self.assertEqual(state['phoneRequests']['active'], 0)

            manager.note_activity()
            with patch('desktop.proxy.network_addresses', return_value=[{'address': '127.0.0.1'}]), \
                    patch('desktop.proxy.psutil.net_connections', return_value=[]):
                state = manager.public_state()
            self.assertEqual(state['phoneProxyState'], 'recent')
            self.assertIsNotNone(state['phoneRequests']['lastAt'])

    def test_public_state_invalidates_lifecycle_when_bound_address_disappears(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = ProxyManager(directory, 'http://127.0.0.1:1/internal/capture', 'fixture')
            process = MagicMock()
            process.poll.return_value = None
            server = MagicMock()
            manager.process = process
            manager.server = server
            manager.address = '192.0.2.10'
            manager.peer = '192.0.2.20'
            manager.pair_token = 'stale-token'
            manager.capture_enabled = True
            manager.last_activity_at = 123
            manager.port = 55255
            with patch('desktop.proxy.network_addresses', return_value=[]):
                state = manager.public_state()
                repeated = manager.public_state()

            self.assertFalse(state['running'])
            self.assertFalse(state['paired'])
            self.assertIsNone(state['pairUrl'])
            self.assertFalse(state['captureEnabled'])
            self.assertTrue(state['networkChanged'])
            self.assertTrue(state['needsReconfigure'])
            self.assertEqual(state, repeated)
            self.assertIsNone(manager.peer)
            self.assertIsNone(manager.pair_token)
            self.assertIsNone(manager.last_activity_at)
            process.terminate.assert_called_once_with()
            server.shutdown.assert_called_once_with()

    def test_network_invalidation_is_idempotent_under_concurrent_status_polling(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = ProxyManager(directory, 'http://127.0.0.1:1/internal/capture', 'fixture')
            process = MagicMock()
            process.poll.return_value = None
            manager.process = process
            manager.server = MagicMock()
            manager.address = '192.0.2.10'
            manager.peer = '192.0.2.20'
            manager.pair_token = 'stale-token'
            manager.capture_enabled = True
            with patch('desktop.proxy.network_addresses', return_value=[]):
                threads = [threading.Thread(target=manager.public_state) for _ in range(8)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
            self.assertEqual(process.terminate.call_count, 1)

    def test_network_poll_cannot_resurrect_reconfigure_after_explicit_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = ProxyManager(directory, 'http://127.0.0.1:1/internal/capture', 'fixture')
            process = MagicMock()
            process.poll.return_value = None
            server = MagicMock()
            manager.process = process
            manager.server = server
            manager.address = '192.0.2.10'
            entered = threading.Event()
            release = threading.Event()

            def network_probe():
                entered.set()
                release.wait(2)
                return []

            with patch('desktop.proxy.network_addresses', side_effect=network_probe):
                polling = threading.Thread(target=manager.public_state)
                polling.start()
                self.assertTrue(entered.wait(1))
                manager.stop()
                release.set()
                polling.join(2)

            state = manager.public_state()
            self.assertFalse(state['networkChanged'])
            self.assertFalse(state['needsReconfigure'])
            self.assertFalse(state['running'])

    def test_proxy_child_receives_parent_process_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            ports = []
            for _ in range(2):
                with socket.socket() as probe:
                    probe.bind(('127.0.0.1', 0))
                    ports.append(probe.getsockname()[1])
            (Path(directory) / 'ports.json').write_text(json.dumps({'proxy': ports[0], 'certificate': ports[1]}))
            manager = ProxyManager(directory, 'http://127.0.0.1:1/internal/capture', 'fixture')
            child = MagicMock()
            child.poll.return_value = None
            with patch('desktop.proxy.network_addresses', return_value=[{'name':'fixture','address':'127.0.0.1'}]), \
                    patch('desktop.proxy.subprocess.Popen', return_value=child) as launch, \
                    patch('desktop.proxy.socket.create_connection', return_value=MagicMock()):
                manager.start('127.0.0.1', 'IOS')
            try:
                parent = json.loads(launch.call_args.kwargs['env']['LYNKCO_PROXY_PARENT'])
                self.assertEqual(parent['pid'], os.getpid())
                self.assertGreater(parent['created'], 0)
            finally:
                manager.stop()

    def test_proxy_port_is_not_read_from_fixed_port_configuration(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket() as occupied:
            occupied.bind(('127.0.0.1', 0))
            port = occupied.getsockname()[1]
            (Path(directory) / 'ports.json').write_text(json.dumps({'proxy': port, 'certificate': port + 1}))
            manager = ProxyManager(directory, 'http://127.0.0.1:1/internal/capture', 'fixture')
            with patch.object(manager, '_choose_proxy_port', return_value=port + 2):
                self.assertEqual(manager._choose_proxy_port('127.0.0.1'), port + 2)

    def test_active_wifi_can_use_non_rfc1918_address(self):
        entry = lambda address: SimpleNamespace(family=socket.AF_INET, address=address)
        interfaces = {'en0':[entry('11.39.142.1')], 'en1':[entry('100.64.1.2')],
                      'utun8':[entry('11.39.142.1')], 'lo0':[entry('127.0.0.1')],
                      'en2':[entry('192.168.1.2')]}
        stats = {name:SimpleNamespace(isup=name != 'en2') for name in interfaces}
        with patch('psutil.net_if_addrs',return_value=interfaces), patch('psutil.net_if_stats',return_value=stats):
            self.assertEqual(network_addresses(), [{'name':'en0','address':'11.39.142.1'}, {'name':'en1','address':'100.64.1.2'}])

    def test_real_proxy_certificate_pairing_passthrough_and_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = ProxyManager(directory, 'http://127.0.0.1:1/internal/capture', 'fixture-callback')
            try:
                with patch('desktop.proxy.network_addresses', return_value=[{'name':'fixture','address':'127.0.0.1'}]):
                    state = manager.start('127.0.0.1', 'IOS')
                process = manager.process
                self.assertTrue(state['running'])
                self.assertEqual(state['phoneProxyState'], 'not_paired')
                self.assertFalse(manager.accepts_peer('127.0.0.1'))
                with urlopen(state['pairUrl'], timeout=3) as response:
                    self.assertEqual(response.status, 200)
                    pairing_page = response.read().decode('utf-8')
                self.assertEqual(manager.public_state()['phoneProxyState'], 'idle')
                self.assertIn('已安装并信任过本机证书，无需重复安装', pairing_page)
                self.assertNotIn('移除本次证书', pairing_page)
                self.assertNotIn('领克', pairing_page)
                self.assertTrue(manager.accepts_peer('127.0.0.1'))
                with urlopen(state['pairUrl'] + '/certificate.cer', timeout=3) as response:
                    certificate = response.read()
                    self.assertIn(b'BEGIN CERTIFICATE', certificate)
                    self.assertNotIn(b'PRIVATE KEY', certificate)
                    self.assertEqual(response.headers.get('Content-Disposition'), 'attachment; filename="LynkCoHelper-CA.cer"')
                for path in ['/mitmproxy-ca.pem', '/../ca/mitmproxy-ca.pem', '/env.json']:
                    from urllib.parse import urlsplit
                    parsed = urlsplit(state['pairUrl'])
                    with self.assertRaises(HTTPError) as caught:
                        urlopen(f'{parsed.scheme}://{parsed.netloc}' + path, timeout=3)
                    self.assertEqual(caught.exception.code, 404)
                manager.disable_capture()
                self.assertTrue(manager.public_state()['running'])
                self.assertFalse(manager.accepts_peer('127.0.0.1'))
                self.assertFalse(json.loads((Path(directory)/'proxy-state.json').read_text())['captureEnabled'])
                manager.stop()
                self.assertIsNotNone(process.poll())
                self.assertFalse(manager.public_state()['running'])
                with patch('desktop.proxy.network_addresses', return_value=[{'name':'fixture','address':'127.0.0.1'}]), \
                        patch.object(manager, '_choose_proxy_port', side_effect=[55255, 55256]):
                    restarted = manager.start('127.0.0.1', 'IOS')
                with self.assertRaises(HTTPError) as caught:
                    urlopen(state['pairUrl'], timeout=3)
                self.assertEqual(caught.exception.code, 404)
                self.assertNotEqual(restarted['port'], state['port'])
                self.assertEqual(urlsplit(restarted['pairUrl']).port, urlsplit(state['pairUrl']).port)
            finally:
                manager.stop()


if __name__ == '__main__':
    unittest.main()
