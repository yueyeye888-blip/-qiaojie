import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from controller import Store


def cfg():
    return {
        "agent_bind": "100.64.0.1",
        "agent_port": 8767,
        "virtualhere_hub": "100.64.0.1:7575",
        "usb_vendor_id": "10d7",
        "usb_product_id": "b012",
        "usb_name": "general adapter",
        "lease_seconds": 900,
        "renew_interval_seconds": 10,
        "agent_offline_seconds": 35,
    }


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "state.db", cfg())

    def tearDown(self):
        self.tmp.cleanup()

    def enroll_agent(self, name, desktop, machine):
        task = self.store.new_enrollment(name, desktop)
        return self.store.enroll(task["enrollment_token"], machine, name.lower(), "100.64.0.10")

    def test_single_use_enrollment(self):
        task = self.store.new_enrollment("Yorke", "d-yorke")
        self.store.enroll(task["enrollment_token"], "machine-a", "YORKE", "100.64.0.10")
        with self.assertRaises(PermissionError):
            self.store.enroll(task["enrollment_token"], "machine-b", "OTHER", "100.64.0.11")

    def test_lease_becomes_active_after_agent_confirmation(self):
        enrolled = self.enroll_agent("Yorke", "d-yorke", "machine-a")
        token = enrolled["agent_token"]
        self.store.acquire(enrolled["agent_id"])
        desired = self.store.poll(token, {"vh_status": "released", "applied_generation": 0, "applied_action": "release"})
        self.assertEqual(desired["action"], "use")
        self.store.poll(token, {"vh_status": "attached", "applied_generation": desired["generation"], "applied_action": "use", "bluetooth_status": "ready"})
        self.assertEqual(self.store.snapshot()["lease"]["state"], "active")

    def test_switch_waits_for_release(self):
        a = self.enroll_agent("Yorke", "d-a", "machine-a")
        b = self.enroll_agent("Nova", "d-b", "machine-b")
        self.store.acquire(a["agent_id"])
        desired = self.store.poll(a["agent_token"], {"vh_status": "released", "applied_generation": 0})
        self.store.poll(a["agent_token"], {"vh_status": "attached", "applied_generation": desired["generation"], "applied_action": "use", "bluetooth_status": "ready"})
        self.store.acquire(b["agent_id"])
        lease = self.store.snapshot()["lease"]
        self.assertEqual(lease["state"], "releasing")
        release = self.store.poll(a["agent_token"], {"vh_status": "attached", "applied_generation": desired["generation"], "applied_action": "use"})
        self.assertEqual(release["action"], "release")
        self.store.poll(a["agent_token"], {"vh_status": "released", "applied_generation": release["generation"], "applied_action": "release", "bluetooth_status": "absent"})
        lease = self.store.snapshot()["lease"]
        self.assertEqual(lease["target_id"], b["agent_id"])
        self.assertEqual(lease["state"], "authorizing")

    def test_error_or_unready_device_cannot_confirm_attachment(self):
        a = self.enroll_agent("Yorke", "d-a", "machine-a")
        lease = self.store.acquire(a["agent_id"])
        for status, error in (("absent", ""), ("ready", "USE failed")):
            self.store.poll(a["agent_token"], {"vh_status": "attached", "applied_generation": lease["generation"], "applied_action": "use", "bluetooth_status": status, "error": error})
            self.assertEqual(self.store.snapshot()["lease"]["state"], "authorizing")

    def test_release_does_not_handoff_while_usb_present(self):
        a = self.enroll_agent("Yorke", "d-a", "machine-a")
        b = self.enroll_agent("Other", "d-b", "machine-b")
        self.store.acquire(a["agent_id"])
        lease = self.store.acquire(b["agent_id"])
        self.store.poll(a["agent_token"], {"vh_status": "released", "applied_generation": lease["generation"], "applied_action": "release", "bluetooth_status": "ready"})
        self.assertEqual(self.store.snapshot()["lease"]["target_id"], a["agent_id"])
        with self.assertRaises(ValueError):
            self.store.acquire(a["agent_id"])

    def test_stale_or_expired_session_cannot_renew(self):
        a = self.enroll_agent("Yorke", "d-a", "machine-a")
        lease = self.store.acquire(a["agent_id"])
        with self.assertRaises(ValueError):
            self.store.renew(a["agent_id"], "old-session", lease["generation"])
        with patch("controller.now", return_value=lease["expires_at"] + 1):
            with self.assertRaises(ValueError):
                self.store.renew(a["agent_id"], lease["session_id"], lease["generation"])

    def test_lost_enrollment_response_retry_preserves_same_identity_and_token(self):
        task=self.store.new_enrollment('Yorke','ecd-yorke')
        first=self.store.enroll(task['enrollment_token'],'machine-a','YORKE','100.64.0.10')
        second=self.store.enroll(task['enrollment_token'],'machine-a','YORKE','100.64.0.10')
        self.assertEqual(first,second)
        with self.assertRaises(PermissionError):
            self.store.enroll(task['enrollment_token'],'machine-a','YORKE','100.64.0.11')

    def test_registered_desktop_cannot_be_silently_reinitialized(self):
        a=self.enroll_agent('Yorke','ecd-yorke','machine-a')
        with self.assertRaises(ValueError):self.store.new_enrollment('New name','ecd-yorke')
        self.assertEqual(self.store.authenticate_agent(a['agent_token'])['machine_id'],'machine-a')

    def test_preparation_heartbeat_never_confirms_attachment_or_release(self):
        a=self.enroll_agent('Yorke','ecd-yorke','machine-a')
        self.store.acquire(a['agent_id'])
        report={'vh_status':'preparing','bluetooth_status':'pnp-Error','applied_generation':-1,'applied_action':'unknown'}
        desired=self.store.poll(a['agent_token'],report)
        self.assertEqual(desired['action'],'use')
        self.assertEqual(self.store.snapshot()['lease']['state'],'authorizing')
        self.assertIsNotNone(self.store.usb_authorization('100.64.0.10'))
        self.store.release()
        desired=self.store.poll(a['agent_token'],report)
        self.assertEqual(desired['action'],'release')
        self.assertEqual(self.store.snapshot()['lease']['state'],'releasing')
        self.assertIsNone(self.store.usb_authorization('100.64.0.10'))

    def test_repeated_release_preserves_ack_generation(self):
        a=self.enroll_agent('Yorke','ecd-yorke','machine-a')
        self.store.acquire(a['agent_id']);self.store.release()
        generation=self.store.snapshot()['lease']['generation']
        self.store.release();self.store.release()
        self.assertEqual(self.store.snapshot()['lease']['generation'],generation)
        self.store.poll(a['agent_token'],{'vh_status':'released','bluetooth_status':'absent','applied_action':'release','applied_generation':generation})
        self.assertEqual(self.store.snapshot()['lease']['state'],'idle')

    def test_normal_release_drains_only_original_connection_until_ack(self):
        a=self.enroll_agent('Yorke','ecd-yorke','machine-a')
        self.store.acquire(a['agent_id'])
        identity=self.store.usb_authorization('100.64.0.10')
        self.store.release()
        self.assertIsNone(self.store.usb_authorization('100.64.0.10'))
        self.assertTrue(self.store.usb_connection_authorized('100.64.0.10',identity))
        self.assertFalse(self.store.usb_connection_authorized('100.64.0.11',identity))
        self.assertFalse(self.store.usb_connection_authorized('100.64.0.10',('other-session',identity[1],identity[2])))
        generation=self.store.snapshot()['lease']['generation']
        self.store.poll(a['agent_token'],{'vh_status':'released','bluetooth_status':'absent','applied_action':'release','applied_generation':generation})
        self.assertFalse(self.store.usb_connection_authorized('100.64.0.10',identity))

    def test_drain_is_bounded_and_hardware_fault_closes_it(self):
        a=self.enroll_agent('Yorke','ecd-yorke','machine-a')
        self.store.acquire(a['agent_id']);identity=self.store.usb_authorization('100.64.0.10')
        self.store.release();deadline=self.store.snapshot()['lease']['release_grace_until']
        self.store.source_ready=False
        self.assertFalse(self.store.usb_connection_authorized('100.64.0.10',identity))
        self.store.source_ready=True
        with patch('controller.now',return_value=deadline):
            self.assertFalse(self.store.usb_connection_authorized('100.64.0.10',identity))

    def test_agent_fault_revokes_drain_without_revival_on_error_clear(self):
        a=self.enroll_agent('Yorke','ecd-yorke','machine-a')
        self.store.acquire(a['agent_id']);identity=self.store.usb_authorization('100.64.0.10')
        self.store.release()
        self.store.poll(a['agent_token'],{'error':'connection failed'})
        self.assertFalse(self.store.usb_connection_authorized('100.64.0.10',identity))
        self.store.poll(a['agent_token'],{'vh_status':'released','bluetooth_status':'absent','applied_generation':-1,'applied_action':'release'})
        self.assertFalse(self.store.usb_connection_authorized('100.64.0.10',identity))

    def test_switch_drain_cannot_outlive_original_lease_expiry(self):
        a=self.enroll_agent('Yorke','ecd-yorke','machine-a')
        b=self.enroll_agent('Other','ecd-other','machine-b')
        lease=self.store.acquire(a['agent_id']);identity=self.store.usb_authorization('100.64.0.10')
        with patch('controller.now',return_value=lease['expires_at']-5):
            self.store.poll(b['agent_token'],{'vh_status':'released','bluetooth_status':'absent'})
            self.store.acquire(b['agent_id'])
        self.assertEqual(self.store.snapshot()['lease']['release_grace_until'],lease['expires_at'])
        with patch('controller.now',return_value=lease['expires_at']):
            self.assertFalse(self.store.usb_connection_authorized('100.64.0.10',identity))

    def test_restart_revokes_normal_release_drain(self):
        a=self.enroll_agent('Yorke','ecd-yorke','machine-a')
        self.store.acquire(a['agent_id']);identity=self.store.usb_authorization('100.64.0.10')
        self.store.release();self.store.recover_after_restart()
        self.assertEqual(self.store.snapshot()['lease']['release_grace_until'],0)
        self.assertFalse(self.store.usb_connection_authorized('100.64.0.10',identity))

    def test_end_sharing_cancels_handoff_without_invalidating_release(self):
        a=self.enroll_agent('Yorke','ecd-yorke','machine-a')
        b=self.enroll_agent('Other','ecd-other','machine-b')
        self.store.acquire(a['agent_id'])
        lease=self.store.acquire(b['agent_id'])
        self.store.release()
        self.assertEqual(self.store.snapshot()['lease']['generation'],lease['generation'])
        self.store.poll(a['agent_token'],{'vh_status':'released','bluetooth_status':'absent','applied_action':'release','applied_generation':lease['generation']})
        self.assertEqual(self.store.snapshot()['lease']['state'],'idle')
        self.assertIsNone(self.store.snapshot()['lease']['pending_target_id'])

    def test_network_peer_cannot_impersonate_another_registered_agent(self):
        a=self.enroll_agent('Yorke','ecd-yorke','machine-a')
        with self.assertRaises(PermissionError):self.store.poll(a['agent_token'],{},peer_ip='100.64.0.11')

    def test_usb_authorization_checks_target_expiry_and_hardware(self):
        a=self.enroll_agent('Yorke','ecd-yorke','machine-a')
        lease=self.store.acquire(a['agent_id'])
        self.assertIsNotNone(self.store.usb_authorization('100.64.0.10'))
        self.assertIsNone(self.store.usb_authorization('100.64.0.11'))
        self.store.source_ready=False
        self.assertIsNone(self.store.usb_authorization('100.64.0.10'))
        self.store.source_ready=True
        with patch('controller.now',return_value=lease['expires_at']+1):
            self.assertIsNone(self.store.usb_authorization('100.64.0.10'))

    def test_controller_restart_only_releases_and_never_hands_off(self):
        a=self.enroll_agent('Yorke','ecd-a','machine-a')
        b=self.enroll_agent('Other','ecd-b','machine-b')
        self.store.acquire(a['agent_id']);self.store.acquire(b['agent_id'])
        self.store.recover_after_restart()
        lease=self.store.snapshot()['lease']
        self.assertEqual(lease['state'],'releasing')
        self.assertIsNone(lease['pending_target_id'])
        self.assertIsNone(self.store.usb_authorization('100.64.0.10'))


if __name__ == "__main__":
    unittest.main()
