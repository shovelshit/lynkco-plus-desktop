import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from desktop.credential_store import CredentialStore


class CredentialStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def store(self, machine='machine-one'):
        with patch('desktop.credential_store.machine_identifier', return_value=machine):
            return CredentialStore('https://lynkco.ltools.asia', state_dir=self.root)

    def test_license_roundtrip_is_encrypted_and_bound_to_code_and_machine(self):
        store = self.store()
        identity = {'userId': 'owner', 'managementToken': 'private-management-token'}
        self.assertFalse(store.has_license())
        store.save(identity, 'login-secret')
        self.assertTrue(store.has_license())
        contents = store.path.read_text()
        self.assertNotIn('login-secret', contents)
        self.assertNotIn('private-management-token', contents)
        self.assertEqual(store.load('login-secret'), identity)
        with self.assertRaisesRegex(ValueError, '登录码|许可'):
            store.load('incorrect')
        with self.assertRaisesRegex(ValueError, '登录码|许可'):
            self.store('machine-two').load('login-secret')

    def test_tampered_license_is_rejected_and_delete_removes_it(self):
        store = self.store()
        store.save({'userId': 'owner', 'managementToken': 'secret'}, 'login-secret')
        envelope = json.loads(store.path.read_text())
        envelope['ciphertext'] = envelope['ciphertext'][:-3] + 'abc'
        store.path.write_text(json.dumps(envelope))
        with self.assertRaises(ValueError):
            store.load('login-secret')
        store.delete()
        self.assertFalse(store.has_license())


if __name__ == '__main__':
    unittest.main()
