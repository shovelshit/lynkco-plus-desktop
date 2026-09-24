"""Small, defensive GitHub release updater helpers."""

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
import platform
import re
import shutil
import tempfile
import urllib.request
import zipfile
import subprocess
import sys

from .version import is_newer, is_release_version, normalize_version

MAX_DOWNLOAD_BYTES = 250 * 1024 * 1024
REPOSITORY = "shovelshit/LynkCoHelper"


@dataclass(frozen=True)
class Release:
    version: str
    assets: dict


def parse_latest_release(payload):
    tag = payload.get("tag_name")
    version = normalize_version(tag)
    assets = {item.get("name"): item.get("browser_download_url") for item in payload.get("assets", [])
              if item.get("name") and item.get("browser_download_url")}
    if not is_release_version(version):
        raise ValueError("invalid release tag")
    return Release(version, assets)


def platform_name():
    if os.name == "nt":
        return "windows-x64"
    if platform.machine().lower() in ("x86_64", "amd64"):
        return "macos-intel"
    return "macos-arm64"


def platform_assets(release, platform_id=None):
    platform_id = platform_id or platform_name()
    prefix = f"LynkCoHelper-cloud-{platform_id}.zip"
    try:
        return release.assets[prefix], release.assets[prefix + ".sha256"]
    except KeyError as error:
        raise ValueError("release assets missing for this platform") from error


def should_check_for_updates(version, local_worker=False):
    return not local_worker and is_release_version(version)


def verify_sha256(path, checksum_text):
    match = re.search(r"([0-9a-fA-F]{64})", checksum_text or "")
    if not match:
        return False
    digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    return digest.lower() == match.group(1).lower()


def _replace_directory(incoming, current):
    backup = current.with_name(current.name + ".backup")
    if backup.exists():
        shutil.rmtree(backup)
    if current.exists():
        current.rename(backup)
    try:
        Path(incoming).rename(current)
    except OSError:
        if backup.exists() and not current.exists():
            backup.rename(current)
        raise
    if backup.exists():
        shutil.rmtree(backup)


def install_update(incoming, current):
    try:
        _replace_directory(Path(incoming), Path(current))
        return True
    except OSError:
        return False


def fetch_latest(timeout=3):
    request = urllib.request.Request(f"https://api.github.com/repos/{REPOSITORY}/releases/latest",
                                     headers={"Accept": "application/vnd.github+json", "User-Agent": "LynkCoHelper"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return parse_latest_release(json.loads(response.read(2 * 1024 * 1024)))


def download_asset(url, destination, timeout=10):
    request = urllib.request.Request(url, headers={"User-Agent": "LynkCoHelper"})
    with urllib.request.urlopen(request, timeout=timeout) as response, Path(destination).open("wb") as stream:
        total = 0
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_DOWNLOAD_BYTES:
                raise ValueError("update package is too large")
            stream.write(chunk)
    return Path(destination)


def prepare_update(archive, extract_root):
    extract_root = Path(extract_root)
    extract_root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        if not names or any(name.startswith(("/", "\\")) or ".." in Path(name.replace("\\", "/")).parts
                           or Path(name.replace("\\", "/")).is_absolute() for name in names):
            raise ValueError("invalid update archive")
        if any((info.external_attr >> 16) & 0o170000 == 0o120000
               for info in bundle.infolist()):
            raise ValueError("symbolic links are not allowed")
        bundle.extractall(extract_root)
    candidates = [item for item in extract_root.iterdir() if item.is_dir()]
    if len(candidates) != 1 or any(item.is_file() for item in extract_root.iterdir()):
        raise ValueError("update archive must contain one application directory")
    return candidates[0]


def download_and_prepare(archive_url, checksum_url, work_root, expected_checksum=None, timeout=10):
    """Download, verify and safely unpack a release into a disposable directory."""
    work_root = Path(work_root)
    work_root.mkdir(parents=True, exist_ok=True)
    archive = work_root / "update.zip"
    download_asset(archive_url, archive, timeout=timeout)
    checksum = expected_checksum
    if checksum is None:
        checksum_path = work_root / "update.sha256"
        download_asset(checksum_url, checksum_path, timeout=timeout)
        checksum = checksum_path.read_text(encoding="utf-8", errors="replace")
    if not verify_sha256(archive, checksum):
        raise ValueError("update checksum mismatch")
    return prepare_update(archive, work_root / "extracted")


def launch_updater(incoming, current, pid=None):
    executable = Path(sys.executable)
    if getattr(sys, "frozen", False):
        command = [str(executable), "--apply-update", str(incoming), str(current), str(pid or 0)]
    else:
        command = [str(executable), "-m", "desktop.updater", "--apply", str(incoming), str(current)]
    if pid:
        command.extend(["--parent-pid", str(pid)])
    return subprocess.Popen(command, close_fds=True, start_new_session=True)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--parent-pid", type=int)
    parser.add_argument("incoming")
    parser.add_argument("current")
    args = parser.parse_args()
    if args.parent_pid:
        import time
        for _ in range(200):
            try:
                os.kill(args.parent_pid, 0)
            except OSError:
                break
            time.sleep(.1)
    if not install_update(Path(args.incoming), Path(args.current)):
        raise SystemExit(1)
