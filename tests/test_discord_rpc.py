import threading
import unittest
from unittest.mock import patch

from consolecraft.config import DEFAULT_DISCORD_APP_ID, Settings
from consolecraft.discord_rpc import DiscordPresence


class DiscordPresenceTests(unittest.TestCase):
    def test_global_default_id_and_disabled_option(self):
        self.assertEqual(Settings().discord_app_id, DEFAULT_DISCORD_APP_ID)
        disabled = DiscordPresence('')
        disabled.set('В лаунчере')
        self.assertIsNone(disabled.thread)

    def test_menu_and_game_states_are_sent(self):
        updated = threading.Event()
        calls = []

        class FakePresence:
            def __init__(self, app_id, **kwargs):
                self.app_id = app_id
            def connect(self):
                pass
            def update(self, **kwargs):
                calls.append(kwargs)
                if len(calls) == 2:
                    updated.set()
            def close(self):
                pass

        with patch('pypresence.Presence', FakePresence):
            rpc = DiscordPresence('123456789012345678')
            rpc.set('В лаунчере')
            rpc.set('Играет в Minecraft', 'Версия 1.21.1')
            self.assertTrue(updated.wait(3))
            rpc.close()
        self.assertEqual([v['details'] for v in calls], ['В лаунчере', 'Играет в Minecraft'])
        self.assertEqual(calls[1]['state'], 'Версия 1.21.1')

