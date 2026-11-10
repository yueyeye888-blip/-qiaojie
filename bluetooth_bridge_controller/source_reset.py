"""Ordinary-user coordinator for the one-method local source reset helper."""
import json
import socket
import time
from pathlib import Path

SOCKET = Path('/Library/Application Support/BluetoothBridge/reset.sock')


def request(operation, nonce=None):
    payload = {'op': operation}
    if nonce is not None:
        payload['nonce'] = nonce
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(2 if operation=='ping' else 40)
        connection.connect(str(SOCKET))
        connection.sendall(json.dumps(payload).encode() + b'\n')
        response = bytearray()
        while b'\n' not in response:
            chunk = connection.recv(1024)
            if not chunk:
                raise RuntimeError('ResetHelperResponseMissing')
            response.extend(chunk)
            if len(response) > 4096:
                raise RuntimeError('ResetHelperResponseTooLarge')
    result = json.loads(response)
    if not result.get('ok'):
        raise RuntimeError('ResetHelper: ' + str(result.get('error', 'Unknown')))
    if operation == 'reset' and (result.get('nonce') != nonce or result.get('old_pid') == result.get('new_pid')):
        raise RuntimeError('ResetHelperProofMismatch')
    return result


class Coordinator:
    def __init__(self, app, rpc=request):
        self.app, self.rpc = app, rpc
        self.helper_ready=False
        self.last_helper_check=0

    def tick(self):
        store = self.app.store
        if not self.app.cfg.get('source_reset_enabled'):
            return
        lease = store.snapshot()['lease']
        if lease['state'] != 'resetting':
            if time.monotonic()-self.last_helper_check>=15:
                try:
                    self.rpc('ping'); self.helper_ready=True
                except Exception:
                    self.helper_ready=False
                self.last_helper_check=time.monotonic()
            return
        nonce = lease['reset_nonce']
        self.app.source_maintenance = True
        try:
            result = self.rpc('reset', nonce)
            self.helper_ready=True
            self.last_helper_check=time.monotonic()
            proxy = self.app.proxy
            if proxy:
                proxy.probe_at = None
            self.app.management.probe_source()
            if not proxy or not proxy.ready() or not store.source_ready:
                raise RuntimeError('SourceNotReadyAfterReset')
            store.finish_source_reset(nonce, result)
        except Exception as exc:
            store.fail_source_reset(nonce, str(exc)[:200])
        finally:
            self.app.source_maintenance = False
