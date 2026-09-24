"""Double-click entrypoint; a single background process owns each user profile."""

import argparse
import atexit
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path
import psutil


def proxy_parent_alive(record):
    try:
        pid = record['pid']
        created = record['created']
        if type(pid) is not int or pid <= 0 or not isinstance(created, (int, float)):
            return False
        return psutil.Process(pid).create_time() == created
    except (KeyError, TypeError, psutil.NoSuchProcess):
        return False
    except psutil.AccessDenied:
        return True


def watch_proxy_parent(raw):
    if not raw:
        return
    try:
        record = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        raise SystemExit('Invalid proxy parent identity') from None

    def watch():
        import time
        while proxy_parent_alive(record):
            time.sleep(.5)
        os._exit(0)

    threading.Thread(target=watch, daemon=True).start()


def state_directory():
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Application Support' / 'LynkCoHelper'
    return Path(os.environ.get('LOCALAPPDATA', str(Path.home() / '.local' / 'share'))) / 'LynkCoHelper'


class InstanceLock:
    def __init__(self, path):
        self.file = open(path, 'a+b')
        os.chmod(path, 0o600)

    def acquire(self):
        try:
            if sys.platform == 'win32':
                import msvcrt
                self.file.seek(0)
                self.file.write(b'0')
                self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False


def instance_record(port):
    return {'pid': os.getpid(), 'created': psutil.Process().create_time(), 'port': port}


def running_instance(path):
    try:
        record = json.loads(path.read_text())
        pid = record['pid']
        if type(pid) is not int or pid <= 0 or not psutil.pid_exists(pid):
            return False
        if 'created' in record and psutil.Process(pid).create_time() != record['created']:
            return False
        # An instance record is never a bearer credential, including stale records.
        path.write_text(json.dumps({key: value for key, value in record.items() if key != 'url'}))
        path.chmod(0o600)
        return True
    except (OSError, ValueError, TypeError, KeyError, psutil.NoSuchProcess):
        return False


def check_integrity():
    if not getattr(sys, 'frozen', False):
        return
    import desktop
    from desktop.integrity import verify_release
    try:
        from _release_integrity import MANIFEST_DIGEST
        valid, reason = verify_release(Path(desktop.__file__).parent, MANIFEST_DIGEST)
    except ImportError:
        valid, reason = False, '缺少发布校验信息'
    if not valid:
        message = '客户端文件校验失败，请重新下载客户端。' + reason
        if '--no-browser' not in sys.argv and '--proxy' not in sys.argv:
            try:
                import tkinter
                from tkinter import messagebox
                window = tkinter.Tk()
                window.withdraw()
                messagebox.showerror('客户端校验失败', message)
                window.destroy()
            except Exception:
                pass
        raise SystemExit(message)


def open_user_page(url):
    """Open the local user page through the platform's foreground launcher."""
    if sys.platform == 'darwin':
        try:
            subprocess.Popen(['open', url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except OSError:
            pass
    return webbrowser.open(url)


def apply_update_and_restart(incoming, current, parent_pid):
    """Run in the short-lived helper process after the original app exits."""
    import time
    from desktop.updater import install_update
    for _ in range(300):
        if not psutil.pid_exists(parent_pid):
            break
        time.sleep(.1)
    current = Path(current)
    if not install_update(Path(incoming), current):
        return 1
    if sys.platform == 'darwin' and current.suffix == '.app':
        executable = current / 'Contents' / 'MacOS' / current.stem
    elif sys.platform == 'win32':
        executable = current / (current.name + '.exe') if current.is_dir() else current
    else:
        executable = current / current.name if current.is_dir() else current
    if not executable.is_file():
        return 1
    subprocess.Popen([str(executable)], close_fds=True, start_new_session=True)
    return 0


def main():
    if '--apply-update' in sys.argv:
        position = sys.argv.index('--apply-update')
        try:
            incoming, current = sys.argv[position + 1:position + 3]
            parent = int(sys.argv[position + 3]) if len(sys.argv) > position + 3 else 0
        except (ValueError, IndexError):
            raise SystemExit('Invalid update arguments') from None
        raise SystemExit(apply_update_and_restart(incoming, current, parent))
    check_integrity()
    if '--proxy' in sys.argv:
        watch_proxy_parent(os.environ.get('LYNKCO_PROXY_PARENT'))
        from mitmproxy.tools.main import mitmdump
        position = sys.argv.index('--proxy')
        mitmdump(sys.argv[position + 1:])
        return
    parser = argparse.ArgumentParser(description='LynkCo desktop assistant')
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--cloud-url')
    parser.add_argument('--state-dir', type=Path)
    options = parser.parse_args()
    import desktop
    package_root = Path(desktop.__file__).parent
    root = options.state_dir or state_directory()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    lock = InstanceLock(root / 'instance.lock')
    instance_path = root / 'instance.json'
    if not lock.acquire():
        running_instance(instance_path)
        return
    from desktop.binding import Controller
    from desktop.cloud_client import CloudClient
    from desktop.credential_store import CredentialStore
    from desktop.local_api import make_server
    from desktop.proxy import ProxyManager

    config_path = package_root / 'service.json'
    config = json.loads(config_path.read_text())
    cloud_url = options.cloud_url or config['cloudUrl']
    controller = Controller(CloudClient(cloud_url), CredentialStore(cloud_url, root))
    callback_token = secrets.token_urlsafe(32)
    server = make_server(controller, package_root / 'web', callback_token=callback_token)
    base = 'http://127.0.0.1:' + str(server.server_port)
    controller.proxy = ProxyManager(root, base + '/internal/capture', callback_token)
    instance_path.write_text(json.dumps(instance_record(server.server_port)))
    instance_path.chmod(0o600)

    def cleanup():
        controller.proxy.stop()
        instance_path.unlink(missing_ok=True)

    atexit.register(cleanup)
    stop_requested = threading.Event()

    def stop(signum, frame):
        stop_requested.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    if options.no_browser:
        try:
            server.serve_forever()
        finally:
            server.server_close()
        return

    from desktop.native_auth import NativeSession, run_window
    session = NativeSession(controller, server, base + '/')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        run_window(session, open_user_page, stop_requested)
    finally:
        session.lock()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == '__main__':
    main()
