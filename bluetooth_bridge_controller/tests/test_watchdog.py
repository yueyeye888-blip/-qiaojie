import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from watchdog import Watchdog


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.watchdog = Watchdog(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_grant_is_peer_bound_single_use_and_secrets_are_not_persisted(self):
        token = self.watchdog.create('100.64.0.20')['enrollment_token']
        with self.assertRaises(PermissionError):
            self.watchdog.enroll(token, '100.64.0.2')
        with patch('watchdog.read_secret', return_value=json.dumps({'token':'bot-secret','chat_id':'123'})):
            result = self.watchdog.enroll(token, '100.64.0.20')
        with self.assertRaises(PermissionError):
            self.watchdog.enroll(token, '100.64.0.20')
        text = self.watchdog.path.read_text()
        for secret in ('bot-secret', result['report_token'], token):
            self.assertNotIn(secret, text)
        self.assertEqual(self.watchdog.path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('token_hash', self.watchdog.status())

    def test_expired_or_superseded_grants_and_public_addresses_are_rejected(self):
        for peer in ('203.0.113.20','127.0.0.1','::1'):
            with self.assertRaises(ValueError): self.watchdog.create(peer)
        old = self.watchdog.create('100.64.0.20')['enrollment_token']
        new = self.watchdog.create('100.64.0.20')['enrollment_token']
        with self.assertRaises(PermissionError): self.watchdog.enroll(old,'100.64.0.20')
        with patch('watchdog.time.time',return_value=99999999999):
            with self.assertRaises(PermissionError): self.watchdog.enroll(new,'100.64.0.20')

    def test_report_survives_controller_restart_and_has_no_management_authority(self):
        token = self.watchdog.create('100.64.0.20')['enrollment_token']
        with patch('watchdog.read_secret',return_value='{"token":"bot","chat_id":"123"}'):
            result = self.watchdog.enroll(token,'100.64.0.20')
        for peer, bearer in (('100.64.0.1',result['report_token']),('100.64.0.20','wrong')):
            with self.assertRaises(PermissionError): self.watchdog.report(bearer,peer,{'state':'up'})
        self.watchdog.report(result['report_token'],'100.64.0.20',{'state':'up','startup_sent':True,'arbitrary':'ignore'})
        restarted=Watchdog(Path(self.tmp.name))
        self.assertTrue(restarted.status()['online'])
        self.assertTrue(restarted.status()['startup_sent'])
        self.assertNotIn('arbitrary',restarted.status())


if __name__ == '__main__': unittest.main()
