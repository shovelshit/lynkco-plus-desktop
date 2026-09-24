"""Behavior checks for the standalone PyInstaller desktop bundle."""

import hashlib
import json
import runpy
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from desktop.integrity import MANIFEST_NAME, RELEASE_FILES, verify_release


class BuildPackageTests(unittest.TestCase):
    def test_ci_uses_pip_cache_and_packaged_smoke_without_flutter_toolchain(self):
        from ruamel.yaml import YAML

        workflow = Path(__file__).resolve().parents[2] / '.github' / 'workflows' / 'build-desktop.yml'
        with workflow.open() as source:
            steps = YAML(typ='safe').load(source)['jobs']['build']['steps']
        python = next(step for step in steps if step.get('uses', '').startswith('actions/setup-python@'))
        self.assertEqual(python['with']['cache'], 'pip')
        self.assertEqual(python['with']['cache-dependency-path'], 'desktop/requirements.lock')
        self.assertFalse(any(step.get('uses', '').startswith('actions/cache@') for step in steps))
        self.assertFalse(any(step.get('name') == 'Prepare macOS CocoaPods' for step in steps))
        self.assertTrue(any('desktop/tests/packaged_smoke.py' in step.get('run', '') for step in steps))

    def fixture(self, directory):
        root = Path(directory).resolve()
        script = root / 'desktop' / 'packaging' / 'build.py'
        script.parent.mkdir(parents=True)
        shutil.copy2(Path(__file__).resolve().parents[1] / 'packaging' / 'build.py', script)
        for relative in RELEASE_FILES:
            destination = root / 'desktop' / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text('{"cloudUrl":"https://release.example"}' if relative == 'service.json' else relative)
        (root / 'desktop' / 'launcher.py').write_text('def main(): pass\n')
        (root / 'desktop' / '__init__.py').write_text('')
        shutil.copy2(Path(__file__).resolve().parents[1] / 'requirements.lock', root / 'desktop' / 'requirements.lock')
        return root, script

    def test_local_build_invokes_pyinstaller_and_preserves_other_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root, script = self.fixture(directory)
            dist = root / 'dist' / 'local-worker'
            dist.mkdir(parents=True)
            (dist / 'keep.txt').write_text('sibling')
            name = 'LynkCoHelper-dev.app' if sys.platform == 'darwin' else 'LynkCoHelper-dev'
            (dist / name).mkdir()
            (dist / name / 'marker').write_text('old bundle')
            commands = []

            def fake_pyinstaller(command, **kwargs):
                commands.append((command, kwargs))
                output = Path(command[command.index('--distpath') + 1])
                built = output / name
                resources = built / 'Contents' / 'Frameworks' if sys.platform == 'darwin' else built / '_internal'
                bundled_desktop = resources / 'desktop'
                shutil.copytree(root / 'build' / 'local-worker-build' / 'stage' / 'desktop', bundled_desktop)
                shutil.copy2(root / 'build' / 'local-worker-build' / 'stage' / '_release_integrity.py',
                             resources / '_release_integrity.py')
                binary = built / 'Contents' / 'MacOS' / 'LynkCoHelper-dev' if sys.platform == 'darwin' else built / 'LynkCoHelper-dev.exe'
                binary.parent.mkdir(parents=True, exist_ok=True)
                binary.write_text('executable')

            with patch.object(sys, 'argv', [str(script), '--cloud-url', 'http://127.0.0.1:8787']), \
                    patch('subprocess.run', side_effect=fake_pyinstaller):
                runpy.run_path(str(script), run_name='__main__')

            self.assertEqual(len(commands), 1)
            command, kwargs = commands[0]
            self.assertEqual(command[:3], [sys.executable, '-m', 'PyInstaller'])
            self.assertIn('--windowed', command)
            self.assertIn('--onedir', command)
            self.assertNotIn('flet', ' '.join(command).lower())
            self.assertEqual(kwargs['cwd'], root)
            self.assertEqual(Path(command[command.index('--distpath') + 1]), root / 'build' / 'local-worker-build' / 'pyinstaller-output')
            self.assertEqual((dist / 'keep.txt').read_text(), 'sibling')
            self.assertTrue((dist / name).exists())
            self.assertEqual(len(list((root / 'build' / 'local-worker-build' / 'previous-artifacts').glob('*/marker'))), 1)
            staged = root / 'build' / 'local-worker-build' / 'stage'
            self.assertEqual(json.loads((staged / 'desktop' / 'service.json').read_text()),
                             {'cloudUrl': 'http://127.0.0.1:8787'})
            self.assertEqual(json.loads((root / 'desktop' / 'service.json').read_text()),
                             {'cloudUrl': 'https://release.example'})
            digest = hashlib.sha256((staged / 'desktop' / MANIFEST_NAME).read_bytes()).hexdigest()
            self.assertEqual(verify_release(staged / 'desktop', digest), (True, ''))

    def test_with_local_worker_builds_release_and_local_apps_side_by_side(self):
        with tempfile.TemporaryDirectory() as directory:
            root, script = self.fixture(directory)
            dist = root / 'dist'
            release = dist / ('LynkCoHelper.app' if sys.platform == 'darwin' else 'LynkCoHelper')
            release.mkdir(parents=True)
            (release / 'marker').write_text('existing release')
            commands = []

            def fake_pyinstaller(command, **kwargs):
                commands.append((command, kwargs))
                name = command[command.index('--name') + 1]
                work_dir = 'local-worker-build' if name.endswith('-dev') else 'release-build'
                output = Path(command[command.index('--distpath') + 1])
                bundle = output / (f'{name}.app' if sys.platform == 'darwin' else name)
                resources = bundle / 'Contents' / 'Frameworks' if sys.platform == 'darwin' else bundle / '_internal'
                stage = root / 'build' / work_dir / 'stage'
                shutil.copytree(stage / 'desktop', resources / 'desktop')
                shutil.copy2(stage / '_release_integrity.py', resources / '_release_integrity.py')
                binary = bundle / 'Contents' / 'MacOS' / name if sys.platform == 'darwin' else bundle / f'{name}.exe'
                binary.parent.mkdir(parents=True, exist_ok=True)
                binary.write_text('executable')

            with patch.object(sys, 'argv', [str(script), '--with-local-worker']), \
                    patch('subprocess.run', side_effect=fake_pyinstaller):
                runpy.run_path(str(script), run_name='__main__')

            self.assertEqual(len(commands), 2)
            self.assertEqual([command[command.index('--name') + 1] for command, _ in commands],
                             ['LynkCoHelper', 'LynkCoHelper-dev'])
            self.assertTrue(release.exists())
            local_name = 'LynkCoHelper-dev.app' if sys.platform == 'darwin' else 'LynkCoHelper-dev'
            self.assertTrue((dist / 'local-worker' / local_name).exists())
            release_service = json.loads((root / 'build' / 'release-build' / 'stage' /
                                          'desktop' / 'service.json').read_text())
            local_service = json.loads((root / 'build' / 'local-worker-build' / 'stage' /
                                        'desktop' / 'service.json').read_text())
            self.assertEqual(release_service, {'cloudUrl': 'https://release.example'})
            self.assertEqual(local_service, {'cloudUrl': 'http://127.0.0.1:8787'})

    def test_incomplete_bundle_does_not_replace_previous_app(self):
        with tempfile.TemporaryDirectory() as directory:
            root, script = self.fixture(directory)
            name = 'LynkCoHelper.app' if sys.platform == 'darwin' else 'LynkCoHelper'
            old = root / 'dist' / name
            old.mkdir(parents=True)
            (old / 'marker').write_text('old bundle')

            def fake_empty(command, **kwargs):
                (Path(command[command.index('--distpath') + 1]) / name).mkdir(parents=True)

            with patch.object(sys, 'argv', [str(script)]), patch('subprocess.run', side_effect=fake_empty):
                with self.assertRaises((FileNotFoundError, ValueError)):
                    runpy.run_path(str(script), run_name='__main__')
            self.assertEqual((old / 'marker').read_text(), 'old bundle')

    def test_failed_install_restores_previous_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            root, script = self.fixture(directory)
            name = 'LynkCoHelper.app' if sys.platform == 'darwin' else 'LynkCoHelper'
            old = root / 'dist' / name
            old.mkdir(parents=True)
            (old / 'marker').write_text('old bundle')

            def fake_pyinstaller(command, **kwargs):
                built = Path(command[command.index('--distpath') + 1]) / name
                resources = built / 'Contents' / 'Frameworks' if sys.platform == 'darwin' else built / '_internal'
                shutil.copytree(root / 'build' / 'release-build' / 'stage' / 'desktop', resources / 'desktop')
                binary = built / 'Contents' / 'MacOS' / 'LynkCoHelper' if sys.platform == 'darwin' else built / 'LynkCoHelper.exe'
                binary.parent.mkdir(parents=True, exist_ok=True)
                binary.write_text('executable')

            original_rename = Path.rename

            def fail_incoming(self, destination):
                if self.parent.name.startswith('.lynkco-package-'):
                    raise OSError('destination temporarily unavailable')
                return original_rename(self, destination)

            with patch.object(sys, 'argv', [str(script)]), patch('subprocess.run', side_effect=fake_pyinstaller), \
                    patch.object(Path, 'rename', fail_incoming):
                with self.assertRaisesRegex(OSError, 'destination temporarily unavailable'):
                    runpy.run_path(str(script), run_name='__main__')
            self.assertEqual((old / 'marker').read_text(), 'old bundle')

    def test_pyinstaller_collects_native_widget_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            root, script = self.fixture(directory)
            commands = []

            def fake_pyinstaller(command, **kwargs):
                commands.append(command)
                built = Path(command[command.index('--distpath') + 1]) / ('LynkCoHelper.app' if sys.platform == 'darwin' else 'LynkCoHelper')
                resources = built / 'Contents' / 'Frameworks' if sys.platform == 'darwin' else built / '_internal'
                shutil.copytree(root / 'build' / 'release-build' / 'stage' / 'desktop', resources / 'desktop')
                binary = built / 'Contents' / 'MacOS' / 'LynkCoHelper' if sys.platform == 'darwin' else built / 'LynkCoHelper.exe'
                binary.parent.mkdir(parents=True, exist_ok=True)
                binary.write_text('executable')

            with patch.object(sys, 'argv', [str(script)]), patch('subprocess.run', side_effect=fake_pyinstaller):
                runpy.run_path(str(script), run_name='__main__')
            self.assertIn(['--collect-all', 'customtkinter'], [commands[0][index:index + 2]
                          for index in range(len(commands[0]) - 1)])


class ReleaseArchiveTests(unittest.TestCase):
    def run_release(self, directory, platform):
        root = Path(directory).resolve()
        script = root / 'desktop' / 'packaging' / 'build_release.py'
        script.parent.mkdir(parents=True)
        shutil.copy2(Path(__file__).resolve().parents[1] / 'packaging' / 'build_release.py', script)
        source = root / 'dist' / ('LynkCoHelper' if platform == 'windows-x64' else 'LynkCoHelper.app')
        source.mkdir(parents=True)
        (source / 'binary').write_text('payload')
        calls = []

        def fake_ditto(command, **kwargs):
            calls.append(command)
            Path(command[-1]).write_bytes(b'archive')

        def fake_zip(*args, **kwargs):
            calls.append((args, kwargs))
            (root / 'release' / f'LynkCoHelper-cloud-{platform}.zip').write_bytes(b'archive')

        with patch.object(sys, 'argv', [str(script), '--tag', 'cloud-v1', '--platform', platform]), \
                patch('subprocess.run', side_effect=fake_ditto), patch('shutil.make_archive', side_effect=fake_zip):
            runpy.run_path(str(script), run_name='__main__')
        return calls, root / 'release' / f'LynkCoHelper-cloud-{platform}.zip'

    def test_macos_release_uses_ditto_to_preserve_bundle_links(self):
        with tempfile.TemporaryDirectory() as directory:
            calls, archive = self.run_release(directory, 'macos-arm64')
            self.assertEqual(calls, [['ditto', '-c', '-k', '--sequesterRsrc', '--keepParent',
                                      str(Path(directory).resolve() / 'dist' / 'LynkCoHelper.app'), str(archive)]])
            self.assertTrue(archive.with_name(archive.name + '.sha256').exists())

    def test_windows_release_uses_zip_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            calls, _ = self.run_release(directory, 'windows-x64')
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][1]['base_dir'], 'LynkCoHelper')


if __name__ == '__main__':
    unittest.main()
