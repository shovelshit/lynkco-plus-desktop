import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import zipfile


class VersionTests(unittest.TestCase):
    def test_source_is_development_and_cloud_tag_is_normalized(self):
        from desktop.version import normalize_version, is_release_version, is_newer

        self.assertEqual(normalize_version("cloud-v1.2.3"), "1.2.3")
        self.assertEqual(normalize_version("v1.2.3"), "1.2.3")
        self.assertFalse(is_release_version("开发版"))
        self.assertTrue(is_newer("1.3.0", "1.2.9"))
        self.assertFalse(is_newer("1.2.9", "1.3.0"))

    def test_latest_release_and_assets_are_parsed(self):
        from desktop.updater import parse_latest_release, platform_assets

        release = parse_latest_release({
            "tag_name": "cloud-v1.4.0",
            "assets": [
                {"name": "LynkCoHelper-cloud-macos-arm64.zip", "browser_download_url": "zip"},
                {"name": "LynkCoHelper-cloud-macos-arm64.zip.sha256", "browser_download_url": "sha"},
            ],
        })
        self.assertEqual(release.version, "1.4.0")
        self.assertEqual(platform_assets(release, "macos-arm64"), ("zip", "sha"))

    def test_sha256_verification_rejects_bad_digest(self):
        from desktop.updater import verify_sha256

        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "a.zip"
            archive.write_bytes(b"payload")
            digest = hashlib.sha256(b"payload").hexdigest()
            self.assertTrue(verify_sha256(archive, f"{digest}  a.zip\n"))
            self.assertFalse(verify_sha256(archive, "0" * 64))

    def test_failed_install_keeps_previous_directory(self):
        from desktop.updater import install_update

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = root / "LynkCoHelper"
            incoming = root / "incoming"
            current.mkdir()
            incoming.mkdir()
            (current / "marker").write_text("old")
            (incoming / "marker").write_text("new")
            with patch("desktop.updater._replace_directory", side_effect=OSError("nope")):
                self.assertFalse(install_update(incoming, current))
            self.assertEqual((current / "marker").read_text(), "old")


class UpdatePolicyTests(unittest.TestCase):
    def test_development_and_local_worker_do_not_check(self):
        from desktop.updater import should_check_for_updates

        self.assertFalse(should_check_for_updates("开发版"))
        self.assertFalse(should_check_for_updates("1.2.3", local_worker=True))
        self.assertTrue(should_check_for_updates("1.2.3", local_worker=False))

    def test_prepare_update_rejects_unsafe_or_ambiguous_archives(self):
        from desktop.updater import prepare_update

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unsafe = root / "unsafe.zip"
            with zipfile.ZipFile(unsafe, "w") as bundle:
                bundle.writestr("../escape.txt", "bad")
            with self.assertRaises(ValueError):
                prepare_update(unsafe, root / "unsafe-out")

            ambiguous = root / "ambiguous.zip"
            with zipfile.ZipFile(ambiguous, "w") as bundle:
                bundle.writestr("One/a", "a")
                bundle.writestr("Two/b", "b")
            with self.assertRaises(ValueError):
                prepare_update(ambiguous, root / "ambiguous-out")

    def test_update_download_pipeline_verifies_checksum_before_install(self):
        from desktop.updater import download_and_prepare

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "source.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("LynkCoHelper/marker", "new")
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            with patch("desktop.updater.download_asset", side_effect=lambda url, path, timeout=10: Path(path).write_bytes(archive.read_bytes()) or Path(path)):
                incoming = download_and_prepare("zip", "sha", root / "work", digest)
            self.assertEqual((incoming / "marker").read_text(), "new")

    def test_frozen_update_uses_launcher_entrypoint(self):
        from desktop.updater import launch_updater

        with patch("desktop.updater.subprocess.Popen") as process, \
             patch.object(__import__("desktop.updater", fromlist=["sys"]).sys, "frozen", True, create=True):
            launch_updater("incoming", "current", pid=42)
        command = process.call_args.args[0]
        self.assertEqual(command[1], "--apply-update")
        self.assertEqual(command[-1], "42")


class FirstRunTests(unittest.TestCase):
    def test_first_run_notice_is_written_only_after_confirmation(self):
        from desktop.native_auth import first_run_notice_needed, mark_first_run_notice

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            self.assertTrue(first_run_notice_needed(state))
            # Closing the dialog must not write the marker.
            self.assertTrue(first_run_notice_needed(state))
            mark_first_run_notice(state)
            self.assertFalse(first_run_notice_needed(state))


if __name__ == "__main__":
    unittest.main()
