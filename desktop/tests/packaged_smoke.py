"""Exercise the shipped GUI, headless HTTP server, and bundled proxy runtime."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

executable = Path(sys.argv[1]).resolve()
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    gui_state = root / 'gui-state'
    # The first-run notice is covered by the native-auth tests. Mark it as
    # acknowledged here so the packaged-process smoke test can exercise the
    # GUI lifecycle without a modal dialog blocking SIGTERM.
    (gui_state).mkdir(parents=True)
    (gui_state / 'first-run-notice-v1').write_text('acknowledged')
    gui = subprocess.Popen([str(executable), '--state-dir', str(gui_state),
                            '--cloud-url', 'http://127.0.0.1:1'],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 45
        while not (gui_state / 'instance.json').exists():
            if gui.poll() is not None or time.monotonic() > deadline:
                detail = gui.stderr.read().decode(errors='replace') if gui.poll() is not None else 'startup timeout'
                raise AssertionError('Packaged GUI did not start: ' + detail)
            time.sleep(.1)
        assert json.loads((gui_state / 'instance.json').read_text())['pid'] == gui.pid
        time.sleep(.5)
        assert gui.poll() is None, 'Packaged GUI did not remain alive'
        gui.terminate()
        result = gui.wait(timeout=8)
        if sys.platform != 'win32':
            assert result == 0, f'Packaged GUI did not shut down cleanly: {result}: {gui.stderr.read().decode(errors="replace")}'
    finally:
        if gui.poll() is None:
            gui.terminate()
            gui.wait(timeout=8)
    ports = []
    for _ in range(3):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            ports.append(probe.getsockname()[1])
    (root / 'state').mkdir()
    (root / 'state' / 'ports.json').write_text(json.dumps({'proxy': ports[0], 'certificate': ports[1]}))
    app = subprocess.Popen([str(executable), '--no-browser', '--state-dir', str(root / 'state'),
                            '--cloud-url', f'http://127.0.0.1:{ports[2]}'],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 45
        while not (root / 'state' / 'instance.json').exists():
            if app.poll() is not None or time.monotonic() > deadline:
                detail = app.stderr.read().decode(errors='replace') if app.poll() is not None else 'still waiting for startup'
                raise AssertionError('Packaged application did not start: ' + detail)
            time.sleep(.1)
        info = json.loads((root / 'state' / 'instance.json').read_text())
        assert set(info) == {'pid', 'created', 'port'}
        assert info['pid'] == app.pid
        base = f"http://127.0.0.1:{info['port']}/"
        for asset in ('', 'app.js', 'style.css', 'lucide.js', 'api/status'):
            for headers in ({}, {'Authorization': 'Bearer invalid'}):
                try:
                    urlopen(Request(base + asset, headers=headers), timeout=3)
                except HTTPError as error:
                    assert error.code == 401, f'{asset}: expected locked session, got {error.code}'
                else:
                    raise AssertionError(f'{asset}: locked session leaked data')
        if sys.platform != 'win32':
            app.terminate()
            assert app.wait(timeout=8) == 0, 'Packaged application did not shut down cleanly'
    finally:
        if app.poll() is None:
            app.terminate()
            app.wait(timeout=8)
    port = ports[0]
    proxy_state = root / 'proxy.json'
    proxy_state.write_text(json.dumps({'peerIp':'127.0.0.1','captureEnabled':False}))
    bundle = executable.parent.parent.parent if sys.platform == 'darwin' else executable.parent
    addons = list(bundle.rglob('capture_addon.py'))
    assert len(addons) == 1, 'Bundled addon must have a unique unpacked resource path'
    addon = addons[0]
    assert addon.is_file(), 'Bundled addon missing'
    process = subprocess.Popen([str(executable), '--proxy', '-q', '--listen-host', '127.0.0.1', '--listen-port', str(port),
                                '--set', 'confdir=' + str(root / 'ca'), '-s', str(addon)],
                               env=dict(os.environ, LYNKCO_PROXY_STATE=str(proxy_state), LYNKCO_CALLBACK_URL='http://127.0.0.1:1/internal/capture',
                                        LYNKCO_CALLBACK_TOKEN='fixture', LYNKCO_PHONE_PLATFORM='IOS'), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError('Packaged proxy exited early')
            try:
                with socket.create_connection(('127.0.0.1',port),timeout=.2):
                    break
            except OSError:
                time.sleep(.1)
        else:
            raise AssertionError('Packaged proxy did not listen')
        assert (root / 'ca' / 'mitmproxy-ca-cert.cer').is_file()
    finally:
        process.terminate()
        process.wait(timeout=8)
print('PASS: packaged GUI, locked HTTP/auth boundaries, graceful exit, bundled proxy and local CA')
