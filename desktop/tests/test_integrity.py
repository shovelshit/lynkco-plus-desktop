import json
import hashlib
import sys
from types import SimpleNamespace
from unittest.mock import patch
import tempfile
import unittest
from pathlib import Path

from desktop.integrity import MANIFEST_NAME, RELEASE_FILES, verify_release, write_manifest


class IntegrityTests(unittest.TestCase):
    def make_release(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for relative in RELEASE_FILES:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(relative, encoding="utf-8")
        return root

    def test_manifest_accepts_unchanged_resources(self):
        root = self.make_release()
        write_manifest(root, root / MANIFEST_NAME)
        self.assertEqual(verify_release(root), (True, ""))

    def test_manifest_rejects_modified_resource(self):
        root = self.make_release()
        write_manifest(root, root / MANIFEST_NAME)
        (root / "web/app.js").write_text("changed", encoding="utf-8")
        self.assertEqual(verify_release(root), (False, "资源已被修改：web/app.js"))

    def test_manifest_rejects_missing_resource(self):
        root = self.make_release()
        write_manifest(root, root / MANIFEST_NAME)
        (root / "service.json").unlink()
        self.assertEqual(verify_release(root), (False, "缺少资源：service.json"))

    def test_manifest_rejects_invalid_manifest(self):
        root = self.make_release()
        (root / MANIFEST_NAME).write_text(json.dumps({"version": 2}), encoding="utf-8")
        self.assertEqual(verify_release(root), (False, "完整性清单版本无效"))

    def test_replacing_both_resource_and_manifest_is_rejected(self):
        root = self.make_release()
        write_manifest(root, root / MANIFEST_NAME)
        anchor = hashlib.sha256((root / MANIFEST_NAME).read_bytes()).hexdigest()
        (root / 'service.json').write_text('changed')
        write_manifest(root, root / MANIFEST_NAME)
        self.assertFalse(verify_release(root, anchor)[0])

    def test_missing_or_malformed_manifest_fails_closed(self):
        root = self.make_release()
        self.assertFalse(verify_release(root)[0])
        for value in ('null', '[]', '{', '{"version":1,"files":{}}'):
            (root / MANIFEST_NAME).write_text(value)
            self.assertFalse(verify_release(root)[0])

    def test_source_run_skips_release_check(self):
        from desktop.launcher import check_integrity
        with patch.object(sys, 'frozen', False, create=True), patch.dict('os.environ', {'FLET_PLATFORM': 'macos'}), \
                patch.object(sys, 'argv', ['helper', '--no-browser']), \
                patch('desktop.integrity.verify_release') as verify:
            check_integrity()
            verify.assert_not_called()

    def test_packaged_run_checks_release(self):
        from desktop.launcher import check_integrity
        with patch.object(sys, 'frozen', True, create=True), \
                patch.object(sys, 'argv', ['helper', '--no-browser']), \
                patch('desktop.integrity.verify_release', return_value=(False, 'modified')):
            with self.assertRaises(SystemExit):
                check_integrity()

    def test_proxy_parent_identity_detects_pid_reuse(self):
        import os
        import psutil
        from desktop.launcher import proxy_parent_alive
        created = psutil.Process(os.getpid()).create_time()
        self.assertTrue(proxy_parent_alive({'pid': os.getpid(), 'created': created}))
        self.assertFalse(proxy_parent_alive({'pid': os.getpid(), 'created': created - 1}))
        self.assertFalse(proxy_parent_alive({'pid': -1, 'created': created}))

    def test_packaged_failure_stops_before_proxy_start(self):
        from desktop.launcher import main
        with patch.object(sys, 'frozen', True, create=True), patch.object(sys, 'argv', ['helper', '--proxy']), \
                patch.dict(sys.modules, {'_release_integrity': SimpleNamespace(MANIFEST_DIGEST='fixture')}), \
                patch('desktop.integrity.verify_release', return_value=(False, 'modified')):
            with self.assertRaises(SystemExit):
                main()
