import json
import io
import os
from pathlib import Path
import socket
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from controller import Store
import source_reset_helper as helper
import install_source_reset_helper as installer
from source_reset import Coordinator
from test_controller import cfg


class ResetLeaseTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.directory.name)/'db',dict(cfg(),source_reset_enabled=True))
        self.agents=[]
        for i in (1,2):
            task=self.store.new_enrollment('Pilot'+str(i),'ecd-pilot'+str(i))
            a=self.store.enroll(task['enrollment_token'],'machine'+str(i),'PILOT'+str(i),'100.64.0.'+str(i))
            self.store.poll(a['agent_token'],{'vh_status':'released','bluetooth_status':'absent'})
            self.agents.append(a)

    def tearDown(self): self.directory.cleanup()

    def begin(self): return self.store.acquire(self.agents[0]['agent_id'])

    def finish(self,nonce): return self.store.finish_source_reset(nonce,{'old_pid':100,'new_pid':101})

    def test_initial_claim_waits_for_source_reset_before_any_usb_authorization(self):
        lease=self.begin()
        self.assertEqual(lease['state'],'resetting');self.assertIsNone(lease['target_id'])
        self.assertIsNone(self.store.usb_authorization('100.64.0.1'))
        self.assertTrue(self.finish(lease['reset_nonce']))
        self.assertEqual(self.store.snapshot()['lease']['state'],'authorizing')
        self.assertIsNotNone(self.store.usb_authorization('100.64.0.1'))

    def test_switch_resets_only_after_old_pnp_absence_ack(self):
        first=self.begin();self.finish(first['reset_nonce'])
        lease=self.store.snapshot()['lease']
        self.store.poll(self.agents[0]['agent_token'],{'vh_status':'attached','bluetooth_status':'ready','applied_action':'use','applied_generation':lease['generation']})
        release=self.store.acquire(self.agents[1]['agent_id'])
        report={'vh_status':'released','applied_action':'release','applied_generation':release['generation'],'bluetooth_status':'ready'}
        self.store.poll(self.agents[0]['agent_token'],report)
        self.assertEqual(self.store.snapshot()['lease']['state'],'releasing')
        report['bluetooth_status']='absent';self.store.poll(self.agents[0]['agent_token'],report)
        queued=self.store.snapshot()['lease']
        self.assertEqual(queued['state'],'resetting');self.assertIsNone(queued['target_id'])
        self.assertEqual(queued['pending_target_id'],self.agents[1]['agent_id'])
        self.assertIsNone(self.store.usb_authorization('100.64.0.1'))
        self.assertIsNone(self.store.usb_authorization('100.64.0.2'))

    def test_cancelled_reset_cannot_be_activated_by_a_late_response(self):
        lease=self.begin();self.store.release()
        self.assertFalse(self.finish(lease['reset_nonce']))
        self.assertEqual(self.store.snapshot()['lease']['state'],'idle')

    def test_restart_does_not_blindly_repeat_a_pending_privileged_action(self):
        lease=self.begin();self.store.recover_after_restart()
        state=self.store.snapshot()['lease']
        self.assertEqual(state['state'],'idle');self.assertEqual(state['reset_error'],'ControllerRestartDuringReset')
        self.assertFalse(self.finish(lease['reset_nonce']))

    def test_expired_reset_never_allocates(self):
        lease=self.begin()
        with patch('controller.now',return_value=lease['expires_at']+1):
            self.assertFalse(self.finish(lease['reset_nonce']))
        self.assertEqual(self.store.snapshot()['lease']['reset_error'],'SourceResetExpired')

    def test_lost_target_after_reset_leaves_idle_and_does_not_use_usb(self):
        lease=self.begin()
        with self.store.connect() as db: db.execute('UPDATE agents SET last_seen=1 WHERE id=?',(self.agents[0]['agent_id'],))
        self.assertFalse(self.finish(lease['reset_nonce']))
        self.assertEqual(self.store.snapshot()['lease']['state'],'idle')

    def app(self):
        proxy=SimpleNamespace(probe_at=10,ready=lambda:True)
        return SimpleNamespace(store=self.store,cfg=self.store.cfg,proxy=proxy,
                               management=SimpleNamespace(probe_source=lambda:None),source_maintenance=False)

    def test_coordinator_failure_stops_without_retrying_the_reset(self):
        lease=self.begin();app=self.app();calls=[]
        def rpc(op,nonce=None):
            calls.append(op)
            if op=='reset': raise OSError('helper unavailable')
            return {'ok':True}
        coordinator=Coordinator(app,rpc);coordinator.tick();coordinator.tick()
        self.assertEqual(calls.count('reset'),1)
        self.assertEqual(self.store.snapshot()['lease']['state'],'idle')
        self.assertFalse(app.source_maintenance)

    def test_coordinator_cancel_during_reset_does_not_allocate(self):
        self.begin();app=self.app()
        def rpc(op,nonce=None):
            self.store.release()
            return {'ok':True,'nonce':nonce,'old_pid':100,'new_pid':101}
        Coordinator(app,rpc).tick()
        self.assertEqual(self.store.snapshot()['lease']['state'],'idle')
        self.assertFalse(app.source_maintenance)


class HelperBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.receipt=Path(self.directory.name)/'receipt.json'
        self.patch=patch.object(helper,'RECEIPT',self.receipt);self.patch.start()
        self.service=helper.ResetService({'uid':501})
        self.nonce='a'*32;self.request={'op':'reset','nonce':self.nonce}

    def tearDown(self): self.patch.stop();self.directory.cleanup()

    def state(self):
        return {'lease':{'state':'resetting','target_id':None,'pending_target_id':1,'reset_nonce':self.nonce,'expires_at':int(time.time())+60},'management':{'usb_active_tunnels':0}}

    def test_only_installed_user_can_request_a_reset(self):
        with patch.object(helper,'launch') as launch:
            for uid in (0,502):
                with self.assertRaises(PermissionError):self.service.handle(self.request,uid)
            launch.assert_not_called()

    def test_no_caller_controlled_service_labels_paths_or_commands(self):
        with patch.object(helper,'launch') as launch:
            for request in ({'op':'run','command':'anything'},{'op':'reset','nonce':self.nonce,'label':'another'}, {'op':'reset','nonce':'bad'}):
                with self.assertRaises(ValueError):self.service.handle(request,501)
            launch.assert_not_called()

    def test_active_lease_or_open_tunnel_never_restarts_source(self):
        for field in ('state','tunnel','nonce'):
            state=self.state()
            if field=='state':state['lease']['state']='active'
            elif field=='tunnel':state['management']['usb_active_tunnels']=1
            else:state['lease']['reset_nonce']='b'*32
            with patch.object(helper,'validate_service'),patch.object(helper,'controller_status',return_value=state),patch.object(helper.time,'monotonic',side_effect=[100,104]),patch.object(helper,'launch') as launch:
                with self.assertRaisesRegex(RuntimeError,'SafeReset'):self.service.handle(self.request,501)
                launch.assert_not_called()

    def test_known_safe_request_restarts_exact_fixed_label_once(self):
        with patch.object(helper,'validate_service'),patch.object(helper,'controller_status',return_value=self.state()),patch.object(helper,'source_pid',side_effect=[100,101]),patch.object(helper,'launch') as launch,patch.object(helper.socket,'create_connection'):
            result=self.service.handle(self.request,501)
            self.assertEqual(result['new_pid'],101)
            launch.assert_called_once_with('kickstart','-k','system/com.virtualhere.vhusbd')
        self.assertTrue(json.loads(self.receipt.read_text())['ok'])

    def test_successful_duplicate_nonce_returns_receipt_without_restarting(self):
        self.receipt.write_text(json.dumps({'ok':True,'nonce':self.nonce,'old_pid':100,'new_pid':101}))
        with patch.object(helper,'protected'),patch.object(helper,'source_pid',return_value=101),patch.object(helper,'launch') as launch:
            self.assertTrue(self.service.handle(self.request,501)['ok'])
            launch.assert_not_called()

    def test_unknown_previous_result_is_not_blindly_repeated(self):
        self.receipt.write_text(json.dumps({'ok':False,'nonce':self.nonce,'stage':'started'}))
        with patch.object(helper,'protected'),patch.object(helper,'launch') as launch:
            with self.assertRaisesRegex(RuntimeError,'OutcomeUnknown'):self.service.handle(self.request,501)
            launch.assert_not_called()

    def test_native_peer_identity_matches_local_socket_creator(self):
        left,right=socket.socketpair()
        try:self.assertEqual(helper.peer_uid(left),os.getuid())
        finally:left.close();right.close()

    def test_oversized_local_request_is_rejected(self):
        left,right=socket.socketpair()
        try:
            right.sendall(b'x'*1100+b'\n')
            with self.assertRaisesRegex(ValueError,'TooLarge'):helper.receive(left)
        finally:left.close();right.close()

    def test_privileged_token_reader_rejects_symlinks_fifos_and_public_files(self):
        path=Path(self.directory.name)/'token';manifest={'uid':os.getuid(),'admin_token_path':str(path)}
        with patch.object(helper.urllib.request,'urlopen') as request:
            path.write_text('private-token');path.chmod(0o644)
            with self.assertRaisesRegex(RuntimeError,'TokenFile'):helper.controller_status(manifest)
            path.unlink();os.mkfifo(path,0o600)
            with self.assertRaisesRegex(RuntimeError,'TokenFile'):helper.controller_status(manifest)
            path.unlink();other=path.with_name('secret');other.write_text('different-secret');path.symlink_to(other)
            with self.assertRaises(OSError):helper.controller_status(manifest)
            request.assert_not_called()

    def test_token_path_replacement_cannot_redirect_the_privileged_file_read(self):
        path=Path(self.directory.name)/'token';path.write_text('original-token');path.chmod(0o600)
        secret=path.with_name('secret');secret.write_text('must-not-be-read')
        manifest={'uid':os.getuid(),'admin_token_path':str(path)}
        original_fstat=os.fstat
        def replace_path(fd):
            info=original_fstat(fd)
            path.unlink();path.symlink_to(secret)
            return info
        response=MagicMock();response.__enter__.return_value=io.BytesIO(b'{"lease":{}}')
        with patch.object(helper.os,'fstat',side_effect=replace_path),patch.object(helper.urllib.request,'urlopen',return_value=response) as request:
            helper.controller_status(manifest)
            sent=request.call_args.args[0]
            self.assertEqual(sent.get_header('X-admin-token'),'original-token')

    def test_installer_waits_for_old_launchd_job_to_disappear(self):
        replies=iter([SimpleNamespace(returncode=0),SimpleNamespace(returncode=0),SimpleNamespace(returncode=0),SimpleNamespace(returncode=1)])
        with patch.object(installer,'launch',side_effect=lambda *args,**kwargs:next(replies)) as launch,patch.object(installer.time,'sleep'):
            installer.stop('system/com.virtualhere.vhusbd')
            self.assertEqual([call.args[0] for call in launch.call_args_list],['print','bootout','print','print'])
