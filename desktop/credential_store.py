"""Encrypted, machine-bound management license; never stores the login code."""

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def machine_identifier():
    """Read the operating system's stable machine identifier, failing closed."""
    if sys.platform == 'darwin':
        try:
            output = subprocess.check_output(['ioreg', '-rd1', '-c', 'IOPlatformExpertDevice'], timeout=5, text=True)
        except (OSError, subprocess.SubprocessError):
            raise ValueError('无法读取本机标识，请稍后重试') from None
        match = re.search(r'"IOPlatformUUID"\s*=\s*"([0-9a-fA-F-]{36})"', output)
        value = match.group(1) if match else None
    elif sys.platform == 'win32':
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Cryptography') as key:
                value = winreg.QueryValueEx(key, 'MachineGuid')[0]
        except OSError:
            raise ValueError('无法读取本机标识，请稍后重试') from None
    else:
        try:
            value = Path('/etc/machine-id').read_text().strip()
        except OSError:
            raise ValueError('无法读取本机标识，请稍后重试') from None
    if not isinstance(value, str) or not 16 <= len(value) <= 128:
        raise ValueError('本机标识无效，请稍后重试')
    return value.lower()


def _encode(value):
    return base64.b64encode(value).decode('ascii')


def _decode(value):
    return base64.b64decode(value, validate=True)


class CredentialStore:
    def __init__(self, cloud_url, state_dir=None):
        self.cloud_url = cloud_url.rstrip('/')
        self.machine_id = machine_identifier()
        if state_dir is None:
            if sys.platform == 'darwin':
                state_dir = Path.home() / 'Library' / 'Application Support' / 'LynkCoHelper'
            else:
                state_dir = Path(os.environ.get('LOCALAPPDATA', str(Path.home() / '.local' / 'share'))) / 'LynkCoHelper'
        self.root = Path(state_dir)
        self.path = self.root / ('license-' + hashlib.sha256(self.cloud_url.encode()).hexdigest() + '.json')

    def has_license(self):
        return self.path.is_file()

    def _key(self, login_code, salt):
        if not isinstance(login_code, str) or not 8 <= len(login_code) <= 512:
            raise ValueError('请输入有效的登录码')
        return hashlib.scrypt((login_code + '\0' + self.machine_id).encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)

    def save(self, identity, login_code):
        if not isinstance(identity, dict) or not all(isinstance(identity.get(key), str) and identity[key] for key in ('userId', 'managementToken')):
            raise ValueError('云端身份格式无效')
        salt, nonce = os.urandom(16), os.urandom(12)
        ciphertext = AESGCM(self._key(login_code, salt)).encrypt(
            nonce, json.dumps({key: identity[key] for key in ('userId', 'managementToken')}).encode(), self.cloud_url.encode())
        envelope = {'version': 1, 'salt': _encode(salt), 'nonce': _encode(nonce), 'ciphertext': _encode(ciphertext)}
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(prefix='.license-', dir=self.root)
        try:
            with os.fdopen(fd, 'w') as output:
                os.chmod(name, 0o600)
                json.dump(envelope, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def load(self, login_code):
        try:
            envelope = json.loads(self.path.read_text())
            if envelope['version'] != 1:
                raise ValueError()
            salt, nonce, ciphertext = (_decode(envelope[key]) for key in ('salt', 'nonce', 'ciphertext'))
            if len(salt) != 16 or len(nonce) != 12 or len(ciphertext) > 8192:
                raise ValueError()
            identity = json.loads(AESGCM(self._key(login_code, salt)).decrypt(nonce, ciphertext, self.cloud_url.encode()))
            if not isinstance(identity, dict) or not all(isinstance(identity.get(key), str) and identity[key] for key in ('userId', 'managementToken')):
                raise ValueError()
            return identity
        except (OSError, KeyError, TypeError, ValueError, InvalidTag, json.JSONDecodeError):
            raise ValueError('登录码错误或本机许可不可用') from None

    def delete(self):
        self.path.unlink(missing_ok=True)

    def invalidate(self):
        self.delete()
