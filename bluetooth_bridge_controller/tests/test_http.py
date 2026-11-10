import http.client
import json
import tempfile
import threading
import unittest
from unittest.mock import patch

from controller import App, Server
from test_controller import cfg


class HttpBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        config = dict(cfg(), runtime_dir=self.tmp.name, ui_bind='127.0.0.1', ui_port=0)
        with patch('management.Management._has', return_value=False):
            self.app = App(config)
        self.server = Server(('127.0.0.1', 0), self.app, 'ui')
        self.app.cfg['ui_port'] = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def request(self, method, path, headers=None, body=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def test_launch_nonce_is_single_use_and_old_token_url_is_rejected(self):
        token = self.app.create_launch_token()
        status, headers, _ = self.request('GET', '/?launch=' + token)
        self.assertEqual(status, 302)
        self.assertEqual(headers['Location'], '/')
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        self.assertEqual(self.request('GET', '/?launch=' + token)[0], 401)
        self.assertEqual(self.request('GET', '/?token=' + self.app.admin_token)[0], 401)

    def test_authenticated_mutation_requires_same_origin_and_source_gate(self):
        headers = {'X-Admin-Token': self.app.admin_token, 'Content-Type': 'application/json'}
        self.assertEqual(self.request('POST', '/api/launch-token', headers, '{}')[0], 403)
        headers['Origin'] = f'http://127.0.0.1:{self.server.server_port}'
        status, _, body = self.request('POST', '/api/jobs', headers, json.dumps({
            'operation': 'install', 'desktop_ids': ['ecd-test'], 'batch_id': 'a' * 20}))
        self.assertEqual(status, 400)
        self.assertIn('Yorke', json.loads(body)['error'])
        self.assertFalse(self.app.bulk_ready())

    def test_diagnostic_export_omits_identity_and_runtime_secrets(self):
        enrollment = self.app.store.new_enrollment('Yorke', 'ecd-test')
        agent = self.app.store.enroll(enrollment['enrollment_token'], 'private-machine-id', 'YORKE', '100.64.0.10')
        status, _, body = self.request('GET', '/api/diagnostics', {'X-Admin-Token': self.app.admin_token})
        self.assertEqual(status, 200)
        for secret in ('private-machine-id', agent['agent_token'], self.app.admin_token, enrollment['enrollment_token']):
            self.assertNotIn(secret, body.decode())

    def test_latest_fault_invalidates_historical_bulk_validation(self):
        task=self.app.store.new_enrollment('Yorke','ecd-test')
        a=self.app.store.enroll(task['enrollment_token'],'machine-test','YORKE','100.64.0.10')
        def cycle():
            lease=self.app.store.acquire(a['agent_id'])
            self.app.store.poll(a['agent_token'],{'vh_status':'attached','bluetooth_status':'ready','applied_action':'use','applied_generation':lease['generation']})
            self.app.store.release()
            generation=self.app.store.snapshot()['lease']['generation']
            self.app.store.poll(a['agent_token'],{'vh_status':'released','bluetooth_status':'absent','applied_action':'release','applied_generation':generation})
        with patch.object(self.app,'source_ready',return_value=True),patch('controller.MARKER') as marker:
            marker.read_text.return_value='{"installed_at":0}'
            cycle();self.assertTrue(self.app.bulk_ready())
            self.app.cfg['bulk_validation_hold']=True
            self.assertFalse(self.app.bulk_ready())
            self.app.cfg['bulk_validation_hold']=False
            with self.app.store.connect() as db:self.app.store.event(db,'fault_release_requested',agent_id=a['agent_id'])
            self.assertFalse(self.app.bulk_ready())
            cycle();self.assertTrue(self.app.bulk_ready())
            with self.app.store.connect() as db:self.app.store.event(db,'source_reset_failed',agent_id=a['agent_id'])
            self.assertFalse(self.app.bulk_ready())

    def test_watchdog_enrollment_requires_admin_origin_and_bound_network_peer(self):
        data=json.dumps({'peer':'100.64.0.20'})
        self.assertEqual(self.request('POST','/api/watchdog/enrollment',body=data)[0],401)
        headers={'X-Admin-Token':self.app.admin_token,'Content-Type':'application/json'}
        self.assertEqual(self.request('POST','/api/watchdog/enrollment',headers,data)[0],403)
        headers['Origin']=f'http://127.0.0.1:{self.server.server_port}'
        code,_,body=self.request('POST','/api/watchdog/enrollment',headers,data)
        self.assertEqual(code,200)
        token=json.loads(body)['enrollment_token']
        self.server.role='agent'
        code,_,_=self.request('POST','/agent/watchdog/enroll',{'Authorization':'Bearer '+token},'{}')
        self.assertEqual(code,401)  # Loopback cannot impersonate the bound Tailscale peer.
