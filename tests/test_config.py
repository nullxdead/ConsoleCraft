from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from consolecraft.config import ConfigError, ConfigStore, Settings, DEFAULT_CLIENT_ID


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / "данные с пробелами"
        self.store = ConfigStore(self.home)
        credentials = {}
        self.backend = Mock()
        self.backend.get_password.side_effect = lambda service, key: credentials.get((service, key))
        self.backend.set_password.side_effect = lambda service, key, value: credentials.__setitem__((service, key), value)
        self.backend.delete_password.side_effect = lambda service, key: credentials.pop((service, key), None)
        self.backend_patch = patch.object(self.store, '_identifier_backend', return_value=self.backend)
        self.backend_patch.start()
        self.addCleanup(self.backend_patch.stop)

    def test_reading_defaults_does_not_create_files(self):
        self.assertEqual(self.store.load().version, "latest")
        self.assertEqual(self.store.load().client_id, DEFAULT_CLIENT_ID)
        self.assertEqual(self.store.load().schema_version, 2)
        self.assertFalse(self.home.exists())

    def test_round_trip_unicode_path_and_public_account_only(self):
        settings = Settings(memory_mb=4096, java_path="/папка/java", account={
            "username": "Steve", "uuid": "a" * 32, "kind": "microsoft",
            "access_token": "secret-access", "refresh_token": "secret-refresh",
        })
        self.store.save(settings)
        restored = self.store.load()
        self.assertEqual(restored.memory_mb, 4096)
        self.assertEqual(restored.java_path, "/папка/java")
        self.assertEqual(set(restored.account), {"username", "uuid", "kind"})
        self.assertNotIn("secret", self.store.path.read_text(encoding="utf-8"))
        self.assertEqual(list(self.home.glob(".config-*.tmp")), [])

    def test_invalid_edit_preserves_existing_file(self):
        self.store.save(Settings(memory_mb=3072))
        before = self.store.path.read_bytes()
        for memory in [True, "2048", 0, -1, 511, 65537]:
            with self.subTest(memory=memory), self.assertRaises(ConfigError):
                self.store.save(Settings(memory_mb=memory))
            self.assertEqual(self.store.path.read_bytes(), before)

    def test_corrupt_file_is_not_overwritten(self):
        self.home.mkdir()
        self.store.path.write_text('{"memory_mb":', encoding="utf-8")
        with self.assertRaisesRegex(ConfigError, "Повреждён"):
            self.store.load()
        self.assertEqual(self.store.path.read_text(encoding="utf-8"), '{"memory_mb":')

    def test_future_schema_rejected_unknown_fields_ignored(self):
        self.home.mkdir()
        self.store.path.write_text(json.dumps({"future_field": 42, "username": "Steve"}), encoding="utf-8")
        self.assertEqual(self.store.load().username, "Steve")
        self.store.path.write_text('{"schema_version": 999}', encoding="utf-8")
        with self.assertRaisesRegex(ConfigError, "Обновите"):
            self.store.load()

    def test_invalid_names_and_client_ids(self):
        for name in ["a", "русский", "bad name", "a" * 17, "../Player"]:
            with self.subTest(name=name), self.assertRaises(ConfigError):
                Settings(username=name).validate()
        with self.assertRaises(ConfigError):
            Settings(client_id="not-an-app-id").validate()
        for version in ["..", "../escape", "bad\x00id"]:
            with self.subTest(version=version), self.assertRaises(ConfigError):
                Settings(version=version).validate()

    def test_legacy_migration_keeps_custom_client_and_creates_one_backup(self):
        self.home.mkdir()
        custom = "12345678-1234-1234-1234-123456789abc"
        self.store.path.write_text(json.dumps({"schema_version": 1, "client_id": custom, "memory_mb": 4096}), encoding="utf-8")
        settings = self.store.load()
        self.assertEqual(settings.schema_version, 2)
        self.assertEqual(settings.client_id, custom)
        self.assertEqual(settings.memory_mb, 4096)
        backup = self.home / "config.v1.backup.json"
        before = backup.read_bytes()
        self.store.load()
        self.assertEqual(before, backup.read_bytes())

    def test_download_settings_validate_and_round_trip(self):
        settings = Settings(download_timeout=60, download_workers=4, auto_check_updates=False)
        self.store.save(settings)
        self.assertEqual(self.store.load().download_timeout, 60)
        self.assertFalse(self.store.load().auto_check_updates)
        for values in ({"download_timeout": 1}, {"download_workers": 0}, {"auto_check_updates": "true"}, {"default_profile": "bad"}):
            with self.subTest(values=values), self.assertRaises(ConfigError):
                Settings(**values).validate()

    def test_identifiers_are_not_written_to_configuration_or_backup(self):
        microsoft = "12345678-1234-1234-1234-123456789abc"
        discord = "123456789012345678"
        self.home.mkdir()
        self.store.path.write_text(json.dumps({"schema_version": 1, "client_id": microsoft, "discord_app_id": discord}), encoding='utf-8')
        settings = self.store.load()
        self.assertEqual((settings.client_id, settings.discord_app_id), (microsoft, discord))
        for path in (self.store.path, self.home / 'config.v1.backup.json'):
            self.assertNotIn(microsoft, path.read_text(encoding='utf-8'))
            self.assertNotIn(discord, path.read_text(encoding='utf-8'))
        another = ConfigStore(self.home)
        with patch.object(another, '_identifier_backend', return_value=self.backend):
            restored = another.load()
        self.assertEqual((restored.client_id, restored.discord_app_id), (microsoft, discord))

    def test_existing_backup_keeps_settings_but_removes_identifiers(self):
        self.home.mkdir()
        original = {"schema_version": 2, "memory_mb": 4096, "client_id": "12345678-1234-1234-1234-123456789abc"}
        backup = self.home / 'config.v1.backup.json'
        backup.write_text(json.dumps(original), encoding='utf-8')
        self.store.path.write_text(json.dumps({"schema_version": 2}), encoding='utf-8')
        self.store.load()
        self.assertEqual(json.loads(backup.read_text(encoding='utf-8')), {"schema_version": 2, "memory_mb": 4096})

    def test_unavailable_keyring_keeps_identifier_only_in_memory(self):
        client_id = "12345678-1234-1234-1234-123456789abc"
        with patch.object(self.store, '_identifier_backend', return_value=None):
            self.store.save(Settings(client_id=client_id))
            self.assertEqual(self.store.load().client_id, client_id)
            self.assertTrue(self.store.identifiers_temporary)
            self.assertNotIn(client_id, self.store.path.read_text(encoding='utf-8'))


if __name__ == "__main__":
    unittest.main()
