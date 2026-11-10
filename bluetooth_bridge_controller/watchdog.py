"""Single-use, peer-bound watchdog enrollment over the existing Tailscale API."""
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import threading
import time

from integrations import read_secret


class Watchdog:
    def __init__(self, runtime):
        self.path = runtime / 'watchdog.json'
        self.lock = threading.Lock()
        self.grants = {}
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {}

    def create(self, peer):
        address = ipaddress.ip_address(peer)
        if address.version != 4 or address not in ipaddress.ip_network('100.64.0.0/10'):
            raise ValueError('Monitor must use its Tailscale IPv4 address')
        token = secrets.token_urlsafe(32)
        expires = int(time.time()) + 600
        with self.lock:
            # Creating a new grant revokes any unused earlier grant.
            self.grants = {hashlib.sha256(token.encode()).hexdigest(): (str(address), expires)}
        return {'enrollment_token': token, 'expires_at': expires}

    def enroll(self, token, peer):
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.lock:
            grant = self.grants.get(digest)
            if not grant or grant[0] != peer or grant[1] <= time.time():
                raise PermissionError('Watchdog enrollment invalid, expired or wrong peer')
            credentials = json.loads(read_secret('telegram.credentials'))
            report_token = secrets.token_urlsafe(32)
            self.state = {'peer': peer, 'token_hash': hashlib.sha256(report_token.encode()).hexdigest(),
                          'enrolled_at': int(time.time()), 'last_seen': 0}
            self.save()
            del self.grants[digest]
        return {'token': credentials['token'], 'chat_id': credentials['chat_id'], 'report_token': report_token}

    def report(self, token, peer, body):
        with self.lock:
            if (self.state.get('peer') != peer or not hmac.compare_digest(
                    self.state.get('token_hash', ''), hashlib.sha256(token.encode()).hexdigest())):
                raise PermissionError('Watchdog identity mismatch')
            state = body.get('state')
            if state not in ('up', 'down', 'checking'):
                raise ValueError('Invalid watchdog state')
            self.state.update(last_seen=int(time.time()), state=state,
                              alert_sent=bool(body.get('alert_sent')),
                              recovery_sent=bool(body.get('recovery_sent')),
                              startup_sent=bool(body.get('startup_sent')))
            for key in ('last_alert_at','last_recovery_at'):
                self.state[key] = str(body.get(key, ''))[:40]
            self.save()

    def save(self):
        temporary = self.path.with_suffix('.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(self.state, stream)
        os.replace(temporary, self.path)

    def status(self):
        with self.lock:
            result = {k: v for k, v in self.state.items() if k != 'token_hash'}
        result['online'] = bool(result.get('last_seen') and time.time() - result['last_seen'] <= 60)
        return result
