import time
import unittest
from unittest.mock import patch

from integrations import discover_telegram_chat


CODE='bridge-'+'a'*24


def message(chat_id=42,kind='private',code=CODE,age=0):
    return {'message':{'text':'/start '+code,'date':int(time.time())-age,
                       'chat':{'id':chat_id,'type':kind},'from':{'id':chat_id,'is_bot':False}}}


class TelegramPairingTests(unittest.TestCase):
    def test_only_current_private_challenge_is_selected(self):
        updates=[message(99,code='other'),message(-1,kind='group'),message(12,age=900),message()]
        with patch('integrations.telegram_call',return_value=updates) as call:
            self.assertEqual(discover_telegram_chat('test-token',CODE),'42')
        self.assertEqual(call.call_args.args[1],'getUpdates')
        self.assertNotIn('offset',call.call_args.args[2])

    def test_ambiguous_pairing_does_not_guess_recipient(self):
        with patch('integrations.telegram_call',return_value=[message(42),message(43)]):
            with self.assertRaises(ValueError):discover_telegram_chat('test-token',CODE)

    def test_missing_pairing_requires_user_message(self):
        with patch('integrations.telegram_call',return_value=[message(age=900)]):
            with self.assertRaises(ValueError):discover_telegram_chat('test-token',CODE)
