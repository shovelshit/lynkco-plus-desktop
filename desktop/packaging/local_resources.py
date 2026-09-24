"""Prepare embedded build assets without changing release configuration."""

import json
import shutil
from pathlib import Path
from urllib.parse import urlsplit

from desktop.integrity import RELEASE_FILES


def build_resources(root: Path, cloud_url: str | None) -> Path:
    source = root / 'desktop'
    if cloud_url is None:
        return source
    parsed = urlsplit(cloud_url)
    if (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost') or
            not parsed.port or parsed.username or parsed.password or parsed.path not in ('', '/') or
            parsed.query or parsed.fragment):
        raise ValueError('Local build requires a loopback HTTP address, such as http://127.0.0.1:8787')
    output = root / 'build' / 'local-resources'
    for relative in RELEASE_FILES:
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if relative == 'service.json':
            destination.write_text(json.dumps({'cloudUrl': cloud_url.rstrip('/')}) + '\n', encoding='utf-8')
        else:
            shutil.copy2(source / relative, destination)
    return output
