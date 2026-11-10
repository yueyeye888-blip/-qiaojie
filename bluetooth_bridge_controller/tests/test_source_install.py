import unittest
from types import SimpleNamespace
from unittest.mock import patch

import install_source_isolation as source


def result(code=0):
    return SimpleNamespace(returncode=code,stderr='',stdout='')


class SourceLifecycleTests(unittest.TestCase):
    def test_waits_for_launchd_removal_before_loading_replacement(self):
        calls=[]
        # Initial lookup, bootout, still stopping twice, finally absent, bootstrap.
        replies=iter([result(),result(),result(),result(),result(1),result()])
        def fake(action,*args,**kwargs):
            calls.append(action)
            return next(replies)
        with patch.object(source,'launch',side_effect=fake),patch.object(source.time,'sleep'):
            source.stop_service()
            source.bootstrap_service()
        self.assertEqual(calls,['print','bootout','print','print','print','bootstrap'])

    def test_stuck_old_service_prevents_replacement(self):
        with patch.object(source,'launch',return_value=result()) as launch,patch.object(source.time,'sleep'):
            with self.assertRaisesRegex(RuntimeError,'尚未退出'):
                source.stop_service()
            self.assertNotIn('bootstrap',[call.args[0] for call in launch.call_args_list])
