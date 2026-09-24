"""Integrity manifest generation and verification for packaged client resources."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


MANIFEST_NAME = "integrity-manifest.json"
RELEASE_FILES = (
    "service.json",
    "capture_addon.py",
    "web/index.html",
    "web/app.js",
    "web/style.css",
    "web/lucide.js",
    "web/LUCIDE-LICENSE",
)


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_manifest(root: Path, output: Path) -> None:
    """Hash release resources below root and write a deterministic manifest."""
    files = {}
    for relative in RELEASE_FILES:
        path = root / relative
        if not path.is_file():
            raise FileNotFoundError(f"release resource missing: {relative}")
        files[relative] = _digest(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"version": 1, "files": files}, indent=2) + "\n", encoding="utf-8")


def verify_release(root: Path, manifest_digest: str | None = None) -> tuple[bool, str]:
    """Verify the packaged resource manifest without exposing file contents."""
    manifest_path = root / MANIFEST_NAME
    try:
        if manifest_digest is not None and _digest(manifest_path) != manifest_digest:
            return False, "完整性清单已被修改"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("version") != 1 or not isinstance(manifest.get("files"), dict):
            return False, "完整性清单版本无效"
        expected = manifest["files"]
        if set(expected) != set(RELEASE_FILES):
            return False, "完整性清单内容不完整"
        for relative in RELEASE_FILES:
            path = root / relative
            if not path.is_file():
                return False, f"缺少资源：{relative}"
            if _digest(path) != expected[relative]:
                return False, f"资源已被修改：{relative}"
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return False, "完整性清单无法读取"
    return True, ""
