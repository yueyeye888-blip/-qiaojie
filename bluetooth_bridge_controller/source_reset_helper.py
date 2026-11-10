#!/usr/bin/env python3
"""Root-owned local helper: ping or restart one fixed USB source service only.

No caller-controlled paths, labels, commands, environment, or network addresses.
The controller remains an ordinary user process. Installed copy runs with -I.
"""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import socket
import stat
import subprocess
import time
import urllib.request

SUPPORT = Path('/Library/Application Support/BluetoothBridge')
SOCKET = SUPPORT / 'reset.sock'
MANIFEST = SUPPORT / 'reset-helper.json'
RECEIPT = SUPPORT / 'reset-receipt.json'
DAEMON = Path('/Library/LaunchDaemons/com.virtualhere.vhusbd.plist')
BINARY = SUPPORT / 'VirtualHereServer.app/Contents/MacOS/vhusbdosx'
CONFIG = SUPPORT / 'virtualhere.ini'
LABEL = 'system/com.virtualhere.vhusbd'


def protected(path):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise RuntimeError('ProtectedPathPermissions')


def peer_uid(connection):
    library = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
    function = library.getpeereid
    function.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)]
    function.restype = ctypes.c_int
    uid, gid = ctypes.c_uint(), ctypes.c_uint()
    if function(connection.fileno(), ctypes.byref(uid), ctypes.byref(gid)) != 0:
        raise OSError(ctypes.get_errno(), 'PeerIdentityUnavailable')
    return uid.value


def receive(connection):
    connection.settimeout(2)
    data = bytearray()
    while b'\n' not in data:
        chunk = connection.recv(512)
        if not chunk:
            raise ValueError('IncompleteRequest')
        data.extend(chunk)
        if len(data) > 1024:
            raise ValueError('RequestTooLarge')
    if bytes(data).count(b'\n') != 1 or not data.endswith(b'\n'):
        raise ValueError('SingleRequestRequired')
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError('InvalidRequest')
    return value


def launch(*arguments):
    return subprocess.run(['/bin/launchctl', *arguments], check=True,
                          capture_output=True, text=True, timeout=15)


def source_pid():
    match = re.search(r'^\s*pid = (\d+)$', launch('print', LABEL).stdout, re.M)
    if not match:
        raise RuntimeError('SourcePidMissing')
    return int(match[1])


def validate_service(manifest):
    for path in (SUPPORT,*list(SUPPORT.parents)[:-1],DAEMON,DAEMON.parent,BINARY,BINARY.parent,BINARY.parent.parent,BINARY.parents[2],CONFIG):
        protected(path)
    expected = [str(BINARY), '-o', '-r', '/var/log/vhusbd.log', '-c', str(CONFIG)]
    if plistlib.loads(DAEMON.read_bytes()).get('ProgramArguments') != expected:
        raise RuntimeError('UnexpectedSourceService')
    if hashlib.sha256(BINARY.read_bytes()).hexdigest() != manifest['binary_sha256']:
        raise RuntimeError('SourceBinaryChanged')
    settings = {line.split('=', 1)[0].strip().lower(): line.split('=', 1)[1].strip()
                for line in CONFIG.read_text().splitlines() if '=' in line}
    if (settings.get('networkinterface') != '127.0.0.1' or settings.get('tcpport') != '7576'
            or settings.get('alloweddevices', '').lower() != '10d7/b012'):
        raise RuntimeError('UnexpectedSourceIsolation')


def controller_status(manifest):
    # This is a fixed, installer-created path. File content is only a bounded
    # HTTP header; it is never parsed as code or used in a shell command.
    path = Path(manifest['admin_token_path'])
    descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(descriptor,'r') as stream:
        info=os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != manifest['uid']
                or info.st_mode&0o077 or info.st_size>256):
            raise RuntimeError('UnexpectedControllerTokenFile')
        token=stream.read(257).strip()
        if len(token)>256: raise RuntimeError('UnexpectedControllerTokenFile')
    request = urllib.request.Request('http://127.0.0.1:8766/api/status',
                                     headers={'X-Admin-Token': token})
    with urllib.request.urlopen(request, timeout=2) as response:
        return json.load(response)


def eligible(state, nonce):
    lease = state['lease']
    return (lease['state'] == 'resetting' and lease['target_id'] is None
            and lease['pending_target_id'] is not None and lease.get('reset_nonce') == nonce
            and lease.get('expires_at', 0) > int(time.time())
            and state.get('management', {}).get('usb_active_tunnels') == 0)


def save_receipt(value):
    temporary = RECEIPT.with_suffix('.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump(value, stream)
        stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, RECEIPT)


class ResetService:
    def __init__(self, manifest):
        self.manifest = manifest
        self.last_reset = None

    def handle(self, request, uid):
        if uid not in (self.manifest['uid'], 0):
            raise PermissionError('WrongLocalUser')
        if request == {'op': 'ping'}:
            return {'ok': True, 'version': 1}
        if uid != self.manifest['uid']:
            raise PermissionError('WrongLocalUser')
        if (set(request) != {'op', 'nonce'} or request['op'] != 'reset'
                or not isinstance(request['nonce'], str)
                or not re.fullmatch('[a-f0-9]{32}', request['nonce'])):
            raise ValueError('UnsupportedRequest')
        nonce = request['nonce']
        if RECEIPT.exists():
            protected(RECEIPT)
            previous = json.loads(RECEIPT.read_text())
            if previous.get('nonce') == nonce:
                if previous.get('ok') and previous.get('new_pid') == source_pid():
                    return previous
                raise RuntimeError('PreviousResetOutcomeUnknown')
        if self.last_reset is not None and time.monotonic() - self.last_reset < 10:
            raise RuntimeError('ResetRateLimited')
        validate_service(self.manifest)
        # The old target has already confirmed release. Wait only for the
        # forwarder's existing socket handlers to finish closing (max 3 sec).
        until = time.monotonic() + 3
        while True:
            state = controller_status(self.manifest)
            if eligible(state, nonce):
                break
            if time.monotonic() >= until:
                raise RuntimeError('ControllerHasNotConfirmedSafeReset')
            time.sleep(0.2)
        old = source_pid()
        save_receipt({'nonce': nonce, 'ok': False, 'stage': 'started', 'old_pid': old})
        self.last_reset = time.monotonic()
        launch('kickstart', '-k', LABEL)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                new = source_pid()
                if new != old:
                    with socket.create_connection(('127.0.0.1', 7576), timeout=1):
                        result = {'ok': True, 'nonce': nonce, 'old_pid': old, 'new_pid': new}
                        save_receipt(result)
                        return result
            except (OSError, RuntimeError, subprocess.SubprocessError):
                pass
            time.sleep(0.5)
        raise RuntimeError('SourceResetDidNotBecomeReady')


def main():
    if os.geteuid() != 0:
        raise SystemExit('RootHelperOnly')
    protected(SUPPORT); protected(MANIFEST); protected(Path(__file__))
    manifest = json.loads(MANIFEST.read_text())
    validate_service(manifest)
    if SOCKET.exists() or SOCKET.is_symlink():
        if not stat.S_ISSOCK(SOCKET.lstat().st_mode):
            raise RuntimeError('UnexpectedSocketPath')
        SOCKET.unlink()
    service = ResetService(manifest)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(SOCKET))
        os.chmod(SOCKET, 0o600); os.chown(SOCKET, manifest['uid'], -1)
        server.listen(4)
        while True:
            connection, _ = server.accept()
            with connection:
                try:
                    result = service.handle(receive(connection), peer_uid(connection))
                except Exception as exc:
                    # Do not return root file contents, command output or tokens.
                    result = {'ok': False, 'error': type(exc).__name__ + ':' + str(exc)[:120]
                              if isinstance(exc, (ValueError, RuntimeError, PermissionError)) else 'HelperOperationFailed'}
                try:
                    connection.sendall(json.dumps(result).encode() + b'\n')
                except OSError:
                    pass


if __name__ == '__main__':
    main()
