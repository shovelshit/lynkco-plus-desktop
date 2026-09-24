import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import threading
import time
import unittest
import sys
from types import SimpleNamespace
from unittest.mock import patch

from desktop.bootstrap import (
    ExistingInstanceBusy,
    ProgressUI,
    cached_archive,
    cleanup_stale,
    download,
    download_with_retries,
    existing_instance_url,
    extract,
    main,
    run_worker,
    wait_for_child,
)


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def archive(self, name, link=None):
        archive = self.root / 'test.tar.gz'
        with tarfile.open(archive, 'w:gz') as bundle:
            info = tarfile.TarInfo(name)
            info.mode = 0o755
            if link:
                info.type = tarfile.SYMTYPE
                info.linkname = link
                bundle.addfile(info)
            else:
                info.size = 4
                bundle.addfile(info, io.BytesIO(b'test'))
        return archive

    def test_extract_preserves_executable(self):
        extract(self.archive('client/run'), self.root / 'out')
        path = self.root / 'out/client/run'
        self.assertEqual(path.read_bytes(), b'test')
        if os.name != 'nt':
            self.assertTrue(path.stat().st_mode & 0o100)

    def test_extract_accepts_filtered_directory_and_hardlink_modes(self):
        archive = self.root / 'filtered-modes.tar.gz'
        with tarfile.open(archive, 'w:gz') as bundle:
            directory = tarfile.TarInfo('client')
            directory.type = tarfile.DIRTYPE
            directory.mode = 0o755
            bundle.addfile(directory)
            original = tarfile.TarInfo('client/original')
            original.mode = 0o755
            original.size = 4
            bundle.addfile(original, io.BytesIO(b'test'))
            hardlink = tarfile.TarInfo('client/alias')
            hardlink.type = tarfile.LNKTYPE
            hardlink.linkname = 'client/original'
            bundle.addfile(hardlink)

        extract(archive, self.root / 'out')
        self.assertEqual((self.root / 'out/client/original').read_bytes(), b'test')
        self.assertEqual((self.root / 'out/client/alias').read_bytes(), b'test')

    def test_wait_for_child_keeps_graphical_progress_responsive(self):
        child = unittest.mock.Mock()
        child.wait.side_effect = [__import__('subprocess').TimeoutExpired('client', .1), 0]
        ui = unittest.mock.Mock()

        self.assertEqual(wait_for_child(child, ui), 0)

        ui.pump.assert_called_once_with()

    def test_local_clear_waits_for_system_prompt_other_actions_keep_timeout(self):
        from desktop.bootstrap import local_action

        opener = unittest.mock.MagicMock()
        response = opener.open.return_value.__enter__.return_value
        response.read.return_value = b'{"ok":true,"data":{"cleared":true}}'
        url = 'http://127.0.0.1:54321/#local-token'

        with patch('desktop.bootstrap.build_opener', return_value=opener):
            self.assertEqual(local_action(url, '/api/identity/clear'), {'cleared': True})
            self.assertEqual(opener.open.call_args.kwargs['timeout'], None)
            local_action(url, '/api/capture/force-stop')
            self.assertEqual(opener.open.call_args.kwargs['timeout'], 10)
            local_action(url, '/api/capture/reset')
            self.assertEqual(opener.open.call_args.kwargs['timeout'], 10)

    def test_wait_for_child_start_returns_after_client_publishes_instance(self):
        from desktop.bootstrap import wait_for_child_start

        child = unittest.mock.Mock(pid=321)
        child.poll.return_value = None
        (self.root / 'instance.json').write_text(json.dumps({
            'pid': 321,
            'port': 54321,
            'releaseSha': 'a' * 64,
        }))
        ui = unittest.mock.Mock()

        with patch('desktop.bootstrap.time.sleep'):
            self.assertTrue(wait_for_child_start(child, self.root, 'a' * 64, ui, timeout=1))

        ui.pump.assert_called()

    def test_wait_for_child_start_waits_for_native_window(self):
        from desktop.bootstrap import wait_for_child_start

        child = unittest.mock.Mock(pid=321)
        child.poll.return_value = None
        ui = unittest.mock.Mock()
        elapsed = [0]

        def advance(_):
            elapsed[0] += 1
            if elapsed[0] == 60:
                (self.root / 'instance.json').write_text(json.dumps({
                    'pid': 321, 'port': 54321,
                    'releaseSha': 'a' * 64,
                }))

        with patch('desktop.bootstrap.time.monotonic', side_effect=lambda: elapsed[0]), \
                patch('desktop.bootstrap.time.sleep', side_effect=advance):
            self.assertTrue(wait_for_child_start(child, self.root, 'a' * 64, ui))
        self.assertEqual(elapsed[0], 60)
        self.assertGreaterEqual(ui.pump.call_count, 60)

    def test_wait_for_child_start_still_honors_close_and_child_exit(self):
        from desktop.bootstrap import wait_for_child_start

        child = unittest.mock.Mock(pid=321)
        child.poll.return_value = None
        ui = unittest.mock.Mock()
        ui.pump.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            wait_for_child_start(child, self.root, 'a' * 64, ui)

        child.poll.return_value = 1
        ui.pump.reset_mock(side_effect=True)
        self.assertFalse(wait_for_child_start(child, self.root, 'a' * 64, ui))
        ui.pump.assert_not_called()

    def test_progress_window_close_requests_clean_shutdown(self):
        ui = ProgressUI.__new__(ProgressUI)
        ui.root = unittest.mock.Mock()
        ui.cancelled = False
        ui._runtime_refresh = None
        ui.request_close()

        with self.assertRaises(KeyboardInterrupt):
            ui.pump()

    def test_progress_window_cannot_close_while_phone_proxy_is_running(self):
        ui = ProgressUI.__new__(ProgressUI)
        ui.root = unittest.mock.Mock()
        ui.detail = unittest.mock.Mock()
        ui.cancelled = False
        ui._runtime_refresh = lambda: {'proxy': {'running': True}}

        ui.request_close()

        self.assertFalse(ui.cancelled)
        ui.detail.config.assert_called_once_with(text='当前暂无手机请求，但电脑无法证明手机代理已关闭；请在网页完成“断开手机连接”后再关闭此窗口')

    def test_progress_window_explains_that_phone_proxy_must_be_confirmed_manually(self):
        ui = ProgressUI.__new__(ProgressUI)
        ui.root = unittest.mock.Mock()
        ui.proxy_status = unittest.mock.Mock()
        ui.backend_status = unittest.mock.Mock()
        ui.open_button = unittest.mock.Mock()
        ui.detail = unittest.mock.Mock()
        ui.cancelled = False
        ui._runtime_refresh = lambda: {'proxy': {'running': True, 'paired': True}}

        ui.refresh_runtime()

        ui.proxy_status.config.assert_called_once_with(text='手机请求：暂无请求')

    def test_progress_window_can_stop_desktop_proxy_before_closing(self):
        ui = ProgressUI.__new__(ProgressUI)
        ui.root = unittest.mock.Mock()
        ui.detail = unittest.mock.Mock()
        ui.cancelled = False
        stopped = []
        states = iter(({'proxy': {'running': True, 'phoneProxyState': 'idle'}}, {'proxy': {'running': False}}))
        ui._runtime_refresh = lambda: next(states)
        ui._runtime_stop = lambda: stopped.append(True)

        ui.request_close()

        self.assertEqual(stopped, [True])
        self.assertTrue(ui.cancelled)

    def test_indeterminate_progress_starts_without_interval_argument(self):
        class NoIntervalProgress:
            def __init__(self):
                self.started = False
                self.configured = []

            def config(self, **kwargs):
                self.configured.append(kwargs)

            def start(self):
                self.started = True

            def stop(self):
                pass

        ui = ProgressUI.__new__(ProgressUI)
        ui.root = unittest.mock.Mock()
        ui.progress = NoIntervalProgress()
        ui.detail = unittest.mock.Mock()
        ui.cancelled = False

        ui.download(0, 0)

        self.assertTrue(ui.progress.started)
        self.assertEqual(ui.progress.configured, [{'mode': 'indeterminate'}])

    def test_delayed_download_keeps_the_progress_window_pumping(self):
        payload = b'release archive'
        digest = hashlib.sha256(payload).hexdigest()
        config = SimpleNamespace(URL='https://github.com/fixture', SHA256=digest, EXECUTABLE='client')
        started = threading.Event()
        release = threading.Event()
        ui = unittest.mock.Mock(cancelled=False)

        def fetch(url, destination, expected, progress=None, deadline=None):
            started.set()
            release.wait(1)
            destination.write_bytes(payload)

        def unpack(archive, destination, progress=None):
            destination.mkdir()
            (destination / 'client').touch()

        result = []
        state_root = self.root / 'LynkCoHelper'
        state_root.mkdir()
        (state_root / 'instance.json').write_text(json.dumps({
            'pid': os.getpid(), 'url': 'http://127.0.0.1:54321/#local-token', 'releaseSha': digest,
        }))
        with patch.dict(sys.modules, {'_bootstrap_release': config}), \
                patch.dict(os.environ, {'LOCALAPPDATA': str(self.root)}), \
                patch('desktop.bootstrap.ProgressUI', return_value=ui), \
                patch('desktop.bootstrap.download', side_effect=fetch), \
                patch('desktop.bootstrap.extract', side_effect=unpack), \
                patch('desktop.bootstrap.signal.signal'), \
                patch('desktop.bootstrap.subprocess.Popen') as launch, \
                patch('desktop.bootstrap.wait_for_child_start', return_value=True):
            launch.return_value.pid = os.getpid()
            launch.return_value.wait.return_value = 0
            launch.return_value.poll.return_value = 0
            thread = threading.Thread(target=lambda: result.append(main()))
            thread.start()
            try:
                self.assertTrue(started.wait(5))
                time.sleep(.05)
                self.assertGreater(ui.pump.call_count, 0)
            finally:
                release.set()
                thread.join(5)

        self.assertEqual(result, [0])

    def test_closing_progress_window_cancels_a_delayed_download(self):
        payload = b'release archive'
        digest = hashlib.sha256(payload).hexdigest()
        config = SimpleNamespace(URL='https://github.com/fixture', SHA256=digest, EXECUTABLE='client')
        started = threading.Event()
        release = threading.Event()
        ui = unittest.mock.Mock(cancelled=False)

        def cancel():
            if started.is_set():
                release.set()
                raise KeyboardInterrupt

        def fetch(url, destination, expected, progress=None, deadline=None):
            started.set()
            release.wait(1)
            destination.write_bytes(payload)

        result = []
        with patch.dict(sys.modules, {'_bootstrap_release': config}), \
                patch.dict(os.environ, {'LOCALAPPDATA': str(self.root)}), \
                patch('desktop.bootstrap.ProgressUI', return_value=ui), \
                patch('desktop.bootstrap.download', side_effect=fetch), \
                patch('desktop.bootstrap.signal.signal'):
            ui.pump.side_effect = cancel
            thread = threading.Thread(target=lambda: result.append(main()))
            thread.start()
            try:
                self.assertTrue(started.wait(1))
                thread.join(.5)
                self.assertFalse(thread.is_alive())
            finally:
                release.set()
                thread.join(1)

        self.assertEqual(result, [130])

    def test_cancellation_closes_a_blocked_response_read(self):
        opened = threading.Event()
        released = threading.Event()
        closed = threading.Event()

        class BlockingResponse:
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *unused):
                self.close()

            def read(self, size):
                released.wait(1)
                return b''

            def close(self):
                closed.set()
                released.set()

        class ClosingUI:
            def pump(self):
                if opened.is_set():
                    raise KeyboardInterrupt

        response = BlockingResponse()
        result = []

        def work():
            try:
                run_worker(
                    lambda events: download(
                        'https://github.com/example/asset', self.root / 'download', hashlib.sha256(b'').hexdigest(),
                        events, time.monotonic() + 5,
                    ),
                    ClosingUI(),
                )
            except KeyboardInterrupt:
                result.append('cancelled')

        with patch('desktop.bootstrap.build_opener') as opener:
            def open_response(*unused, **kwargs):
                opened.set()
                return response

            opener.return_value.open.side_effect = open_response
            thread = threading.Thread(target=work)
            thread.start()
            try:
                self.assertTrue(opened.wait(1))
                thread.join(.5)
                self.assertFalse(thread.is_alive())
                self.assertTrue(closed.is_set())
            finally:
                released.set()
                thread.join(1)

        self.assertEqual(result, ['cancelled'])

    def test_cancellation_abandons_a_body_read_that_ignores_close(self):
        opened = threading.Event()
        read_started = threading.Event()
        released = threading.Event()
        closed = threading.Event()

        class BlockingResponse:
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *unused):
                self.close()

            def read(self, size):
                read_started.set()
                released.wait(1)
                return b''

            def close(self):
                closed.set()

        class ClosingUI:
            def pump(self):
                if read_started.is_set():
                    raise KeyboardInterrupt

        response = BlockingResponse()
        result = []

        def work():
            try:
                run_worker(
                    lambda events: download(
                        'https://github.com/example/asset', self.root / 'download', hashlib.sha256(b'').hexdigest(),
                        events, time.monotonic() + 5,
                    ),
                    ClosingUI(),
                )
            except KeyboardInterrupt:
                result.append('cancelled')

        with patch('desktop.bootstrap.build_opener') as opener:
            def open_response(*unused, **kwargs):
                opened.set()
                return response

            opener.return_value.open.side_effect = open_response
            thread = threading.Thread(target=work)
            thread.start()
            try:
                self.assertTrue(opened.wait(1))
                self.assertTrue(read_started.wait(1))
                thread.join(.5)
                self.assertFalse(thread.is_alive())
                self.assertTrue(closed.is_set())
                self.assertEqual(result, ['cancelled'])
            finally:
                released.set()
                thread.join(1)

    def test_cancellation_abandons_a_blocked_connection_attempt(self):
        opened = threading.Event()
        release = threading.Event()
        response_closed = threading.Event()

        class Response:
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *unused):
                self.close()

            def read(self, size):
                return b''

            def close(self):
                response_closed.set()

        class ClosingUI:
            def pump(self):
                if opened.is_set():
                    raise KeyboardInterrupt

        result = []

        def work():
            try:
                run_worker(
                    lambda events: download(
                        'https://github.com/example/asset', self.root / 'download', hashlib.sha256(b'').hexdigest(),
                        events, time.monotonic() + 5,
                    ),
                    ClosingUI(),
                )
            except KeyboardInterrupt:
                result.append('cancelled')

        with patch('desktop.bootstrap.build_opener') as opener:
            def open_response(*unused, **kwargs):
                opened.set()
                release.wait(1)
                return Response()

            opener.return_value.open.side_effect = open_response
            thread = threading.Thread(target=work)
            thread.start()
            try:
                self.assertTrue(opened.wait(1))
                thread.join(.5)
                self.assertFalse(thread.is_alive())
            finally:
                release.set()
                thread.join(1)

        self.assertEqual(result, ['cancelled'])
        self.assertTrue(response_closed.wait(1))

    def test_extract_cancellation_interrupts_multi_chunk_copy(self):
        archive = self.root / 'large.tar.gz'
        payload = b'x' * (2 * 1024 * 1024)
        with tarfile.open(archive, 'w:gz') as bundle:
            info = tarfile.TarInfo('client/data.bin')
            info.size = len(payload)
            bundle.addfile(info, io.BytesIO(payload))

        class CancellingUI:
            checks = 0

            def check_cancelled(self):
                self.checks += 1
                if self.checks >= 4:
                    raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            extract(archive, self.root / 'out', CancellingUI())
        path = self.root / 'out' / 'client' / 'data.bin'
        self.assertTrue(path.exists())
        self.assertLess(path.stat().st_size, len(payload))

    def test_download_retries_at_most_three_times(self):
        config = SimpleNamespace(URL='https://github.com/fixture', SHA256='a' * 64, EXECUTABLE='client')
        with patch.dict(sys.modules, {'_bootstrap_release': config}), \
                patch.dict(os.environ, {'LOCALAPPDATA': str(self.root)}), \
                patch('desktop.bootstrap.download', side_effect=TimeoutError('offline')) as fetch, \
                patch('desktop.bootstrap.signal.signal'), patch('builtins.input'):
            self.assertEqual(main(), 1)

        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(list((self.root / 'LynkCoHelper/downloads' / 'cache').glob('download-*.tmp')), [])

    def test_windows_manifest_declares_per_monitor_v2_dpi_awareness(self):
        manifest = Path(__file__).parents[1] / 'packaging' / 'windows.manifest'
        self.assertTrue(manifest.is_file())
        self.assertIn('PerMonitorV2</dpiAwareness>', manifest.read_text(encoding='utf-8'))

    def test_windows_ci_checks_the_packaged_desktop_executable(self):
        from ruamel.yaml import YAML

        workflow = Path(__file__).parents[2] / '.github' / 'workflows' / 'build-desktop.yml'
        with workflow.open() as source:
            steps = YAML(typ='safe').load(source)['jobs']['build']['steps']
        desktop = next(step['run'] for step in steps if step.get('name') == 'Smoke check Windows desktop package')
        self.assertIn('& $mt "-inputresource:$package;#1"', desktop)
        self.assertIn("Where-Object { $_.FullName -match '\\\\x64\\\\mt\\.exe$' }", desktop)
        self.assertIn('PerMonitorV2</dpiAwareness>', desktop)
        self.assertFalse(any(step.get('name') == 'Smoke check Windows release bootstrap and DPI manifest' for step in steps))

    def test_path_traversal_and_external_symlink_rejected(self):
        for name, link in [('../outside', None), ('escape', '../../outside')]:
            with self.assertRaises(tarfile.FilterError):
                extract(self.archive(name, link), self.root / 'out')
        self.assertFalse((self.root / 'outside').exists())

    def test_hash_mismatch_rejected(self):
        for expected, valid in [(hashlib.sha256(b'test').hexdigest(), True), ('0' * 64, False)]:
            with patch('desktop.bootstrap.build_opener') as opener:
                opener.return_value.open.return_value = io.BytesIO(b'test')
                if valid:
                    download('https://github.com/example/asset', self.root / 'download', expected)
                else:
                    with self.assertRaisesRegex(ValueError, 'SHA-256'):
                        download('https://github.com/example/asset', self.root / 'download', expected)

    def test_cleanup_preserves_active_and_recent_sessions(self):
        for name in ['active', 'dead', 'recent']:
            directory = self.root / f'session-{name}'
            directory.mkdir()
            (directory / 'processes.json').write_text(json.dumps([{'pid': name}]))
            if name != 'recent':
                os.utime(directory, (0, 0))
        with patch('desktop.bootstrap.alive', side_effect=lambda record: record['pid'] == 'active'):
            cleanup_stale(self.root)
        self.assertTrue((self.root / 'session-active').exists())
        self.assertTrue((self.root / 'session-recent').exists())
        self.assertFalse((self.root / 'session-dead').exists())

    def test_cached_archive_downloads_once_and_reuses_verified_content(self):
        payload = b'verified release archive'
        expected = hashlib.sha256(payload).hexdigest()
        calls = []

        def fetch(url, destination, digest, ui=None, deadline=None):
            calls.append(url)
            self.assertEqual(digest, expected)
            destination.write_bytes(payload)

        with patch('desktop.bootstrap.download', side_effect=fetch):
            first = download_with_retries(self.root, 'https://github.com/example/asset', expected)
            second = download_with_retries(self.root, 'https://github.com/example/asset', expected)

        self.assertEqual(first, second)
        self.assertEqual(first.read_bytes(), payload)
        self.assertEqual(calls, ['https://github.com/example/asset'])

    def test_cached_archive_replaces_corrupt_content(self):
        payload = b'fresh release archive'
        expected = hashlib.sha256(payload).hexdigest()
        cache = self.root / 'cache' / f'{expected}.tar.gz'
        cache.parent.mkdir()
        cache.write_bytes(b'corrupt')

        def fetch(url, destination, digest, ui=None, deadline=None):
            destination.write_bytes(payload)

        with patch('desktop.bootstrap.download', side_effect=fetch) as mocked:
            result = cached_archive(self.root, 'https://github.com/example/asset', expected)

        mocked.assert_called_once()
        self.assertEqual(result.read_bytes(), payload)

    def test_cached_archive_removes_other_release_archives(self):
        payload = b'current release archive'
        expected = hashlib.sha256(payload).hexdigest()
        cache = self.root / 'cache'
        cache.mkdir()
        current = cache / f'{expected}.tar.gz'
        current.write_bytes(payload)
        old = cache / f'{"a" * 64}.tar.gz'
        old.write_bytes(b'old release')
        unrelated = cache / 'notes.txt'
        unrelated.write_text('keep')

        self.assertEqual(cached_archive(self.root, 'https://github.com/example/asset', expected), current)

        self.assertFalse(old.exists())
        self.assertTrue(unrelated.exists())

    def test_existing_instance_record_has_no_browser_secret(self):
        instance = self.root / 'instance.json'
        instance.write_text(json.dumps({'pid': os.getpid(), 'port': 54321}))
        with patch('desktop.bootstrap.build_opener') as opener:
            self.assertTrue(existing_instance_url(self.root))
            opener.assert_not_called()

    def test_same_release_instance_is_reused_without_quitting(self):
        (self.root / 'instance.json').write_text(json.dumps({
            'pid': os.getpid(), 'port': 54321, 'releaseSha': 'a' * 64,
        }))
        with patch('desktop.bootstrap.build_opener') as opener:
            self.assertTrue(existing_instance_url(self.root, 'a' * 64))
        opener.assert_not_called()

    def test_old_release_requires_explicit_exit_before_starting_new_release(self):
        (self.root / 'instance.json').write_text(json.dumps({
            'pid': os.getpid(), 'port': 54321, 'releaseSha': 'a' * 64,
        }))
        with patch('desktop.bootstrap.build_opener') as opener:
            with self.assertRaisesRegex(ExistingInstanceBusy, '另一版本'):
                existing_instance_url(self.root, 'b' * 64)
            opener.assert_not_called()

    def test_legacy_instance_bearer_url_is_never_opened(self):
        url = 'http://127.0.0.1:54321/#local-token'
        (self.root / 'instance.json').write_text(json.dumps({
            'pid': os.getpid(), 'url': url, 'port': 54321, 'releaseSha': 'a' * 64,
        }))
        with patch('desktop.bootstrap.build_opener') as opener:
            with self.assertRaisesRegex(ExistingInstanceBusy, '旧版') as raised:
                existing_instance_url(self.root, 'b' * 64)
        self.assertIsNone(raised.exception.url)
        opener.assert_not_called()

    def test_existing_instance_rejects_invalid_port(self):
        (self.root / 'instance.json').write_text(json.dumps({
            'pid': os.getpid(), 'port': 0,
        }))
        with patch('desktop.bootstrap.build_opener') as opener:
            self.assertIsNone(existing_instance_url(self.root))
            opener.assert_not_called()

    def test_exit_removes_session_but_preserves_other_user_files(self):
        sentinel = self.root / 'credentials'
        sentinel.write_text('preserve')
        payload = b'cached resource'
        digest = hashlib.sha256(payload).hexdigest()
        config = SimpleNamespace(URL='https://github.com/fixture', SHA256=digest, EXECUTABLE='client')
        def unpack(archive, destination, progress=None):
            destination.mkdir()
            (destination / 'client').touch()
        def fetch(url, destination, expected, ui=None, deadline=None):
            destination.write_bytes(payload)
        state_root = self.root / 'LynkCoHelper'
        state_root.mkdir()
        (state_root / 'instance.json').write_text(json.dumps({
            'pid': os.getpid(), 'port': 54321, 'releaseSha': 'old',
        }))
        with patch.dict(sys.modules, {'_bootstrap_release': config}), \
                patch.dict(os.environ, {'LOCALAPPDATA': str(self.root)}), \
                patch('desktop.bootstrap.download', side_effect=fetch), patch('desktop.bootstrap.extract', side_effect=unpack), \
                patch('desktop.bootstrap.signal.signal'), patch('desktop.bootstrap.subprocess.Popen') as launch, \
                patch('desktop.bootstrap.wait_for_child_start', return_value=True), \
                patch('desktop.bootstrap.existing_instance_url', return_value=None):
            launch.return_value.pid = os.getpid()
            launch.return_value.wait.return_value = 0
            launch.return_value.poll.return_value = 0
            self.assertEqual(main(), 0)
            command = launch.call_args.args[0]
            self.assertIn('--bootstrap-parent', command)
            self.assertIn('--bootstrap-stop', command)
        downloads = self.root / 'LynkCoHelper/downloads'
        self.assertEqual(list(downloads.glob('session-*')), [])
        self.assertEqual((downloads / 'cache' / f'{digest}.tar.gz').read_bytes(), payload)
        self.assertEqual(sentinel.read_text(), 'preserve')

    def test_bad_download_never_starts_client_and_is_cleaned(self):
        config = SimpleNamespace(URL='https://github.com/fixture', SHA256='fixture', EXECUTABLE='client')
        with patch.dict(sys.modules, {'_bootstrap_release': config}), \
                patch.dict(os.environ, {'LOCALAPPDATA': str(self.root)}), \
                patch('desktop.bootstrap.download', side_effect=ValueError('SHA-256 mismatch')), \
                patch('desktop.bootstrap.signal.signal'), patch('builtins.input'), \
                patch('desktop.bootstrap.subprocess.Popen') as launch:
            self.assertEqual(main(), 1)
            launch.assert_not_called()
        self.assertEqual(list((self.root / 'LynkCoHelper/downloads').iterdir()), [])
