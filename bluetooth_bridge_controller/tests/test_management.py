import json
import hashlib
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from controller import Store, ROOT
from integrations import CloudError, rpc_parameters, telegram_call
from management import Management
from test_controller import cfg


class FakeCloud:
    region='ap-southeast-1'
    def __init__(self):
        self.rows=[{'DesktopId':'ecd-test','DesktopName':'测试电脑','DesktopStatus':'Running'}]
        self.calls=[]
        self.output={}
        self.failure=None
    def desktops(self):return self.rows
    def call(self,action,**params):
        self.calls.append((action,params))
        if self.failure:raise self.failure
        return {}
    def run(self,desktop,script):
        self.calls.append(('RunCommand',{'desktop':desktop,'script':script}))
        if self.failure:raise self.failure
        return 't-test-'+str(len(self.calls))
    def invocation(self,invoke_id):return self.output


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.tmp.name)/'state.db',cfg())
        self.cloud=FakeCloud()
        self.app=SimpleNamespace(store=self.store,cfg=cfg(),repair_script=lambda:'test repair')
        with patch.object(Management,'_has',return_value=False):
            self.manager=Management(self.app,ROOT,cloud=self.cloud,sender=lambda message:None)
        self.manager.cloud_ready=True
        self.manager.sync()
    def tearDown(self):self.tmp.cleanup()
    def enroll(self):
        e=self.store.new_enrollment('测试电脑','ecd-test')
        return self.store.enroll(e['enrollment_token'],'machine-test','test','100.64.0.10')
    def jobs(self):
        with self.store.connect() as db:return [dict(r) for r in db.execute('SELECT * FROM jobs')]
    def success(self,job,output=''):
        self.cloud.output={'Invocations':[{'InvokeId':job['invoke_id'],'InvokeDesktops':[{'DesktopId':job['desktop_id'],'InvocationStatus':'Success','ExitCode':0,'Output':output}]}]}

    def test_monitor_outage_is_deduplicated_and_recovery_notified(self):
        current=int(time.time())
        state={'peer':'100.64.0.20','enrolled_at':current-200,'last_seen':current-100}
        self.app.watchdog=SimpleNamespace(status=lambda:state)
        self.manager.telegram_ready=True
        self.manager.evaluate_health(self.store.snapshot())
        self.manager.evaluate_health(self.store.snapshot())
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM notification_outbox').fetchone()[0],1)
            self.assertIsNone(db.execute("SELECT resolved_at FROM incidents WHERE incident_key='monitor:offline'").fetchone()[0])
        state['last_seen']=current
        self.manager.evaluate_health(self.store.snapshot())
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM notification_outbox').fetchone()[0],2)
            self.assertIsNotNone(db.execute("SELECT resolved_at FROM incidents WHERE incident_key='monitor:offline'").fetchone()[0])

    def test_repeated_request_is_one_batch_and_conflicting_payload_rejected(self):
        ids=self.manager.create_batch(['ecd-test'],'diagnose','batch-request-12345')
        self.assertEqual(ids,self.manager.create_batch(['ecd-test'],'diagnose','batch-request-12345'))
        self.assertEqual(len(self.jobs()),1)
        with self.assertRaises(ValueError):self.manager.create_batch(['ecd-test'],'install','batch-request-12345')

    def test_hibernation_uses_wakeup_once_not_start(self):
        self.cloud.rows[0]['DesktopStatus']='Hibernated'
        self.manager.create_batch(['ecd-test'],'diagnose','batch-request-12345')
        self.manager.tick_jobs();self.manager.tick_jobs()
        self.assertEqual([x[0] for x in self.cloud.calls],['WakeupDesktops'])
        self.cloud.rows[0]['DesktopStatus']='Running'
        self.manager.tick_jobs()
        self.assertEqual(self.jobs()[0]['state'],'preflight')

    def test_ambiguous_submission_is_not_automatically_retried(self):
        self.manager.create_batch(['ecd-test'],'diagnose','batch-request-12345')
        self.cloud.failure=CloudError('TransportUnknown',uncertain=True)
        self.manager.tick_jobs();self.manager.tick_jobs()
        self.assertEqual(self.jobs()[0]['state'],'unknown')
        self.assertEqual(sum(x[0]=='RunCommand' for x in self.cloud.calls),1)
        with self.assertRaises(ValueError):self.manager.create_batch(['ecd-test'],'diagnose','another-request-123')

    def test_command_success_waits_for_matching_agent_version(self):
        a=self.enroll()
        self.manager.create_batch(['ecd-test'],'repair','batch-request-12345')
        self.manager.tick_jobs()
        job=self.jobs()[0]
        probe={'supported':True,'configured':True,'machine_hash':'machine-test','controller':'http://100.64.0.1:8767/agent/poll'}
        self.success(job,'BRIDGE-DIAG:'+json.dumps(probe))
        self.manager.tick_jobs()
        job=self.jobs()[0];self.assertEqual(job['state'],'running')
        self.success(job)
        self.manager.tick_jobs();self.manager.tick_jobs()
        self.assertEqual(self.jobs()[0]['state'],'waiting_heartbeat')
        digest=hashlib.sha256((ROOT/'windows/agent.ps1').read_bytes()).hexdigest()
        self.store.poll(a['agent_token'],{'vh_status':'released','bluetooth_status':'absent','applied_action':'release','agent_version':digest})
        self.manager.tick_jobs()
        self.assertEqual(self.jobs()[0]['state'],'succeeded')

    def test_explicit_cloudassistant_boot_rejection_waits_then_resumes(self):
        self.manager.create_batch(['ecd-test'],'diagnose','batch-request-12345')
        self.cloud.failure=CloudError('CloudAssistant.NotReady')
        self.manager.tick_jobs()
        self.assertEqual(self.jobs()[0]['state'],'waking')
        self.assertEqual(self.jobs()[0]['stage'],'cloudassistant_wait')
        self.assertIsNone(self.jobs()[0]['invoke_id'])
        self.cloud.failure=None
        self.manager.tick_jobs()
        self.assertEqual(self.jobs()[0]['state'],'preflight')
        self.assertTrue(self.jobs()[0]['invoke_id'])

    def test_cloudassistant_boot_wait_is_bounded(self):
        self.manager.create_batch(['ecd-test'],'diagnose','batch-request-12345')
        self.cloud.failure=CloudError('CloudAssistant.NotReady')
        self.manager.tick_jobs()
        with self.store.connect() as db:
            db.execute('UPDATE jobs SET deadline=?',(int(time.time())+1400,))
        self.manager.tick_jobs()
        self.assertEqual(self.jobs()[0]['state'],'failed')
        self.assertEqual(self.jobs()[0]['error_code'],'CloudAssistant.NotReady')

    def test_pending_install_stops_if_source_validation_was_lost(self):
        self.app.bulk_ready=lambda:False
        self.manager.create_batch(['ecd-test'],'install','batch-request-12345')
        self.manager.tick_jobs()
        self.assertEqual(self.jobs()[0]['state'],'failed')
        self.assertEqual(self.jobs()[0]['error_code'],'SourceValidationRequired')
        self.assertFalse(self.cloud.calls)

    def test_false_remote_desktop_result_cannot_complete_job(self):
        self.manager.create_batch(['ecd-test'],'diagnose','batch-request-12345')
        self.manager.tick_jobs();job=self.jobs()[0]
        self.cloud.output={'Invocations':[{'InvokeId':job['invoke_id'],'InvokeDesktops':[{'DesktopId':'ecd-other','InvocationStatus':'Success','ExitCode':0,'Output':'BRIDGE-DIAG:{}'}]}]}
        self.manager.tick_jobs()
        self.assertEqual(self.jobs()[0]['state'],'preflight')

    def test_offline_alert_is_deduplicated_and_sleep_not_an_error(self):
        a=self.enroll();self.manager.telegram_ready=True
        with self.store.connect() as db:db.execute('UPDATE agents SET last_seen=?',(int(time.time())-120,))
        self.manager.evaluate_health(self.store.snapshot());self.manager.evaluate_health(self.store.snapshot())
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM notification_outbox').fetchone()[0],1)
        self.cloud.rows[0]['DesktopStatus']='Hibernated';self.manager.sync()
        self.manager.evaluate_health(self.store.snapshot())
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM incidents WHERE resolved_at IS NULL').fetchone()[0],0)
        self.store.poll(a['agent_token'],{'vh_status':'released','bluetooth_status':'absent'})

    def test_active_offline_target_revokes_authorization(self):
        a=self.enroll();self.store.acquire(a['agent_id'])
        self.assertIsNotNone(self.store.usb_authorization('100.64.0.10'))
        with self.store.connect() as db:db.execute('UPDATE agents SET last_seen=?',(int(time.time())-120,))
        self.manager.evaluate_health(self.store.snapshot())
        self.assertEqual(self.store.snapshot()['lease']['state'],'releasing')
        self.assertIsNone(self.store.usb_authorization('100.64.0.10'))

    def test_notifications_stop_retrying_after_three_failures(self):
        self.manager.telegram_ready=True
        self.manager.sender=lambda message: (_ for _ in ()).throw(ValueError('sensitive exception must not be stored'))
        with self.store.connect() as db:db.execute("INSERT INTO notification_outbox(message,due_at) VALUES('test',0)")
        for _ in range(3):
            self.manager.deliver_one()
            with self.store.connect() as db:db.execute('UPDATE notification_outbox SET due_at=0')
        with self.store.connect() as db:
            row=dict(db.execute('SELECT * FROM notification_outbox').fetchone())
        self.assertEqual((row['state'],row['attempts'],row['error_code']),('failed',3,'TelegramDeliveryFailed'))


class CredentialTests(unittest.TestCase):
    def test_rpc_arrays_and_sensitive_command_remain_post_body_parameters(self):
        values=rpc_parameters('RunCommand','ap-southeast-1','test-id','test-secret',{'DesktopId':['ecd-a','ecd-b'],'CommandContent':'test command'})
        self.assertEqual(values['DesktopId.1'],'ecd-a')
        self.assertEqual(values['DesktopId.2'],'ecd-b')
        self.assertNotIn('test-secret',json.dumps(values))
        self.assertTrue(values['Signature'])

    def test_telegram_error_does_not_expose_url_token(self):
        with patch('urllib.request.urlopen',side_effect=OSError('https://api.telegram.org/bot123:SECRET_TOKEN/sendMessage')):
            with self.assertRaises(ValueError) as error:telegram_call('123:SECRET_TOKEN','sendMessage',{'chat_id':'123','text':'test'})
        self.assertNotIn('SECRET_TOKEN',str(error.exception))


if __name__=='__main__':unittest.main()
