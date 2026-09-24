import json
import tempfile
import unittest
from pathlib import Path

from desktop.integrity import MANIFEST_NAME, RELEASE_FILES, verify_release, write_manifest
from desktop.packaging.local_resources import build_resources


class LocalResourcesTests(unittest.TestCase):
    def test_local_service_is_embedded_and_integrity_matches_without_editing_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for relative in RELEASE_FILES:
                path = root / 'desktop' / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('release' if relative != 'service.json' else '{"cloudUrl":"https://release.example"}')
            resources = build_resources(root, 'http://127.0.0.1:8787')
            write_manifest(resources, resources / MANIFEST_NAME)
            self.assertEqual(verify_release(resources), (True, ''))
            self.assertEqual(json.loads((resources / 'service.json').read_text()), {'cloudUrl': 'http://127.0.0.1:8787'})
            self.assertEqual(json.loads((root / 'desktop' / 'service.json').read_text()), {'cloudUrl': 'https://release.example'})
            self.assertEqual(build_resources(root, None), root / 'desktop')

    def test_local_build_rejects_non_loopback_or_ambiguous_urls(self):
        with tempfile.TemporaryDirectory() as temporary:
            for url in ('https://release.example', 'http://192.168.1.10:8787',
                        'http://127.0.0.1:8787/extra', 'http://user@localhost:8787',
                        'http://localhost:8787/?x=1', 'http://localhost'):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    build_resources(Path(temporary), url)
