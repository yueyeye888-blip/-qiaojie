import copy
import unittest
from unittest.mock import patch

from source_cycle_acceptance import Acceptance, PILOTS, privileged_test


class Api:
    def __init__(self, fail=False, ambiguous=False):
        self.target = None
        self.fail, self.ambiguous = fail, ambiguous
        self.calls = []

    def __call__(self, path, body=None):
        if path == '/api/lease/acquire':
            self.calls.append(('acquire', body['agent_id']))
            self.target = body['agent_id']
            if self.ambiguous:
                raise OSError('response lost')
        elif path == '/api/lease/release':
            self.calls.append(('release', self.target))
            self.target = None
        return copy.deepcopy({'lease': {'state': 'active' if self.target else 'idle', 'target_id': self.target},
            'agents': [{'id': i, 'desktop_id': PILOTS[i], 'online': True, 'last_report': {
                'error': 'PnP absent' if self.fail and i == self.target else '',
                'vh_status': 'attached' if i == self.target else 'released',
                'bluetooth_status': 'ready' if i == self.target else 'absent'}} for i in (1, 2)]})


class SourceCycleTests(unittest.TestCase):
    def runner(self, api):
        events, resets, clock = [], [], [0]
        def sleep(seconds):
            clock[0] += seconds
        runner = Acceptance(api, lambda: resets.append('reset'),
                            lambda event, **fields: events.append((event, fields)), sleep, lambda: clock[0])
        return runner, events, resets

    def test_resets_before_each_claim_and_releases_every_pilot(self):
        api = Api(); runner, events, resets = self.runner(api)
        runner.run()
        self.assertEqual(len(resets), 3)
        self.assertEqual(api.calls, [('acquire', 2), ('release', 2), ('acquire', 1), ('release', 1), ('acquire', 2), ('release', 2)])
        self.assertEqual(events[-1][0], 'restart_workaround_passed')
        self.assertEqual([e[0] for e in events[:4]], ['reset_start', 'acquire_start', 'attached_ready', 'released_confirmed'])

    def test_existing_session_is_left_untouched(self):
        api = Api(); api.target = 1
        runner, events, resets = self.runner(api)
        with self.assertRaisesRegex(RuntimeError, 'ExistingSessionMustBeReleasedFirst'):
            runner.run()
        self.assertEqual(api.calls, [])
        self.assertEqual(resets, [])

    def test_short_offline_period_after_admin_prompt_waits_for_fresh_heartbeat(self):
        api = Api(); polls = [0]
        def delayed(path, body=None):
            state = api(path, body)
            if path == '/api/status':
                polls[0] += 1
                if polls[0] <= 3:
                    state['agents'][1]['online'] = False
            return state
        runner, events, resets = self.runner(delayed)
        runner.run()
        self.assertEqual(events[0][0], 'preflight_wait')
        self.assertFalse(events[0][1]['state']['pilots'][1]['online'])
        self.assertEqual(len(resets), 3)
        self.assertEqual(events[-1][0], 'restart_workaround_passed')

    def test_preflight_timeout_records_missing_readiness_without_reset_or_claim(self):
        api = Api()
        def unavailable(path, body=None):
            state = api(path, body)
            state['agents'][1]['last_report']['vh_status'] = 'unknown'
            return state
        runner, events, resets = self.runner(unavailable)
        with self.assertRaisesRegex(RuntimeError, 'ConfirmationTimeout'):
            runner.run()
        self.assertEqual(resets, [])
        self.assertEqual(api.calls, [])
        self.assertEqual(events[-1][0], 'confirmation_timeout')
        self.assertEqual(events[-1][1]['state']['pilots'][1]['vh'], 'unknown')

    def test_wrong_pilot_identity_is_rejected_before_reset(self):
        api = Api()
        def different(path, body=None):
            state = api(path, body)
            state['agents'][1]['desktop_id'] = 'ecd-other'
            return state
        runner, events, resets = self.runner(different)
        with self.assertRaisesRegex(RuntimeError, 'PilotIdentityMismatch'):
            runner.run()
        self.assertEqual(resets, [])
        self.assertEqual(api.calls, [])

    def test_attachment_fault_releases_and_stops_without_second_reset(self):
        api = Api(fail=True); runner, events, resets = self.runner(api)
        with self.assertRaisesRegex(RuntimeError, 'AgentAttachmentFailed'):
            runner.run()
        self.assertEqual(api.calls, [('acquire', 2), ('release', 2)])
        self.assertEqual(len(resets), 1)
        self.assertEqual(events[-1][0], 'failure_release_confirmed')

    def test_ambiguous_acquire_also_requests_release(self):
        api = Api(ambiguous=True); runner, events, resets = self.runner(api)
        with self.assertRaisesRegex(OSError, 'response lost'):
            runner.run()
        self.assertEqual(api.calls, [('acquire', 2), ('release', 2)])

    def test_unselected_target_cannot_trigger_a_root_reset(self):
        api = Api(); runner, events, resets = self.runner(api)
        with self.assertRaisesRegex(ValueError, 'OnlySelectedPilotsAllowed'):
            runner.run((3,))
        self.assertEqual(resets, [])
        self.assertEqual(api.calls, [])

    def test_privileged_entry_rejects_non_root_before_touching_files(self):
        with patch('source_cycle_acceptance.os.geteuid', return_value=501), patch.dict('os.environ', {'SUDO_UID': '501'}):
            with self.assertRaisesRegex(RuntimeError, 'UserInvokedSudoEntry'):
                privileged_test()
