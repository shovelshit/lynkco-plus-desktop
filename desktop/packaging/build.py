"""Build an offline, single-app desktop bundle with PyInstaller."""

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root))
from desktop.integrity import MANIFEST_NAME, RELEASE_FILES, verify_release, write_manifest
from desktop.packaging.local_resources import build_resources

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--cloud-url', help='Embed a loopback Worker address in this local build')
parser.add_argument('--with-local-worker', action='store_true',
                    help='Build the release app and an additional local-worker app')
parser.add_argument('--version', help='Embed a release version such as cloud-v1.2.3')
options = parser.parse_args()


def build_bundle(cloud_url):
    """Build and atomically install one release or local-worker bundle."""
    resources = build_resources(root, cloud_url)
    local_build = cloud_url is not None
    work_root = root / 'build' / ('local-worker-build' if local_build else 'release-build')
    stage = work_root / 'stage'
    if stage.exists():
        shutil.rmtree(stage)
    package = stage / 'desktop'
    package.mkdir(parents=True)

    for source in (root / 'desktop').glob('*.py'):
        shutil.copy2(source, package / source.name)
    for relative in RELEASE_FILES:
        destination = package / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(resources / relative, destination)
    version = options.version or os.environ.get('LYNKCO_BUILD_VERSION')
    (package / 'version.json').write_text(
        __import__('json').dumps({'version': version or ('开发版' if local_build else '开发版')}) + '\n',
        encoding='utf-8')
    manifest_path = package / MANIFEST_NAME
    write_manifest(package, manifest_path)
    digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    (stage / '_release_integrity.py').write_text(
        'MANIFEST_DIGEST = ' + repr(digest) + '\n', encoding='utf-8')

    bundle_name = 'LynkCoHelper-dev' if local_build else 'LynkCoHelper'
    dist_root = root / 'dist' / 'local-worker' if local_build else root / 'dist'
    output = work_root / 'pyinstaller-output'
    separator = os.pathsep
    command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--windowed', '--onedir',
               '--name', bundle_name, '--paths', str(stage), '--hidden-import', '_release_integrity',
               '--distpath', str(output), '--workpath', str(work_root / 'pyinstaller-work'),
               '--specpath', str(work_root),
               '--add-data', f'{package / "web"}{separator}desktop/web',
               '--add-data', f'{package / "service.json"}{separator}desktop',
               '--add-data', f'{package / "version.json"}{separator}desktop',
               '--add-data', f'{package / "capture_addon.py"}{separator}desktop',
               '--add-data', f'{manifest_path}{separator}desktop',
               '--collect-all', 'mitmproxy', '--collect-all', 'mitmproxy_rs',
               '--collect-all', 'certifi', '--collect-all', 'customtkinter',
               '--hidden-import', 'qrcode.image.svg', '--hidden-import', 'psutil',
               str(package / 'launcher.py')]
    if sys.platform == 'win32':
        command[3:3] = ['--manifest', str(root / 'desktop' / 'packaging' / 'windows.manifest')]
    if sys.platform == 'darwin':
        command[3:3] = ['--osx-bundle-identifier',
                        'community.lynkco.helper.dev' if local_build else 'community.lynkco.helper']
    subprocess.run(command, cwd=root, check=True)

    built = output / (f'{bundle_name}.app' if sys.platform == 'darwin' else bundle_name)
    binary = built / 'Contents' / 'MacOS' / bundle_name if sys.platform == 'darwin' else built / f'{bundle_name}.exe'
    if not binary.is_file():
        raise FileNotFoundError(f'PyInstaller did not produce the expected executable: {binary}')
    manifests = list(built.rglob(MANIFEST_NAME))
    if len(manifests) != 1:
        raise FileNotFoundError('Desktop bundle must contain exactly one release resource manifest')
    valid, reason = verify_release(manifests[0].parent, digest)
    if not valid:
        raise ValueError(f'Desktop bundle resource verification failed: {reason}')

    destination = dist_root / built.name
    dist_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.lynkco-package-', dir=dist_root) as temporary:
        incoming = Path(temporary) / destination.name
        shutil.copytree(built, incoming, symlinks=True)
        backup = None
        if destination.exists():
            backup_root = work_root / 'previous-artifacts'
            backup_root.mkdir(parents=True, exist_ok=True)
            backup = backup_root / f'{destination.name}.{time.time_ns()}'
            destination.rename(backup)
        try:
            incoming.rename(destination)
        except OSError:
            if backup is not None and not destination.exists():
                backup.rename(destination)
            raise


if options.with_local_worker:
    if options.cloud_url is not None:
        build_bundle(None)
        build_bundle(options.cloud_url)
    else:
        build_bundle(None)
        build_bundle('http://127.0.0.1:8787')
else:
    build_bundle(options.cloud_url)
