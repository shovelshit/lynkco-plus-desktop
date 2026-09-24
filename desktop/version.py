"""Single source of truth for the desktop client version."""

import json
import os
from pathlib import Path
import re

VERSION = "开发版"


def current_version():
    value = os.environ.get("LYNKCO_VERSION")
    if value:
        return normalize_version(value)
    try:
        payload = json.loads((Path(__file__).with_name("version.json")).read_text())
        return normalize_version(payload.get("version", VERSION))
    except (OSError, ValueError, TypeError):
        return VERSION


def normalize_version(value):
    text = str(value or "").strip()
    match = re.fullmatch(r"(?:cloud-)?v?(\d+\.\d+\.\d+)", text)
    return match.group(1) if match else (VERSION if text in ("", VERSION) else text)


def is_release_version(value=None):
    value = current_version() if value is None else value
    return bool(re.fullmatch(r"\d+\.\d+\.\d+", str(value)))


def is_newer(candidate, current=None):
    current = current_version() if current is None else current
    if not (is_release_version(candidate) and is_release_version(current)):
        return False
    return tuple(map(int, normalize_version(candidate).split("."))) > tuple(map(int, normalize_version(current).split(".")))

