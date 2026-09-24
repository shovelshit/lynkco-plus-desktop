"""Archive the already-built PyInstaller desktop application for release."""

import argparse
import hashlib
from pathlib import Path
import re
import shutil
import subprocess

parser = argparse.ArgumentParser()
parser.add_argument('--tag', required=True)
parser.add_argument('--platform', choices=['windows-x64', 'macos-arm64', 'macos-intel'], required=True)
args = parser.parse_args()
if not re.fullmatch(r'cloud-v[0-9][A-Za-z0-9._-]*', args.tag):
    parser.error('tag must start with cloud-v followed by a version')
root = Path(__file__).resolve().parents[2]
out = root / 'release'
out.mkdir(exist_ok=True)
name = f'LynkCoHelper-cloud-{args.platform}'
source = root / 'dist' / ('LynkCoHelper' if args.platform == 'windows-x64' else 'LynkCoHelper.app')
if not source.exists():
    raise SystemExit(f'Built desktop app not found: {source}')
archive = out / f'{name}.zip'
if args.platform.startswith('macos-'):
    subprocess.run(['ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', str(source), str(archive)], check=True)
else:
    shutil.make_archive(str(archive.with_suffix('')), 'zip', root_dir=source.parent, base_dir=source.name)
with archive.open('rb') as stream:
    checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
(out / f'{archive.name}.sha256').write_text(f'{checksum}  {archive.name}\n')
