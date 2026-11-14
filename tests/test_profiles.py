"""Profile isolation, migration, archive and world-preservation boundaries."""

from dataclasses import asdict
import json
from pathlib import Path
import stat
import tempfile
import unittest
import zipfile

from consolecraft.config import ConfigStore, DEFAULT_CLIENT_ID
from consolecraft.profiles import ProfileError, ProfileStore


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.config = ConfigStore(self.home)
        self.profiles = ProfileStore(self.config)

    def jar(self, profile, name="example.jar", content=b"test mod"):
        target = Path(profile.game_directory) / "mods" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return target

    def archive(self, members):
        destination = self.home / "input.zip"
        with zipfile.ZipFile(destination, "w") as archive:
            archive.writestr("profile.json", json.dumps({"schema_version": 1, "profile": {"name": "Imported"}}))
            for key, value in members.items():
                info = zipfile.ZipInfo("placeholder")
                info.filename = key
                archive.writestr(info, value)
        return destination

    def test_default_migrates_without_touching_legacy_worlds_or_mods(self):
        legacy = self.config.game_directory / "saves" / "World" / "level.dat"
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(b"do not delete")
        self.config.path.write_text(json.dumps({"schema_version": 1, "version": "1.21.1", "memory_mb": 4096, "client_id": ""}), encoding="utf-8")
        settings = self.config.load()
        self.assertEqual(settings.schema_version, 2)
        self.assertEqual(settings.client_id, DEFAULT_CLIENT_ID)
        self.assertTrue((self.home / "config.v1.backup.json").exists())
        profile = self.profiles.get()
        self.assertTrue(profile.legacy)
        self.assertEqual(profile.minecraft_version, "1.21.1")
        self.assertEqual(profile.memory_mb, 4096)
        self.assertEqual(Path(profile.game_directory), self.config.game_directory)
        self.assertEqual(legacy.read_bytes(), b"do not delete")
        self.assertEqual(self.config.load().default_profile, profile.id)
        self.assertEqual(self.profiles.get().id, profile.id)

    def test_profiles_have_independent_paths_mods_and_default(self):
        first = self.profiles.create("Fabric", loader="fabric", loader_version="0.16.0")
        second = self.profiles.create("Quilt", loader="quilt")
        self.jar(first)
        self.assertNotEqual(first.id, second.id)
        self.assertFalse((Path(second.game_directory) / "mods" / "example.jar").exists())
        self.profiles.set_default(second.id)
        self.assertEqual(self.profiles.get().id, second.id)
        self.assertEqual(self.profiles.get("fabric").id, first.id)

    def test_overlap_duplicate_names_and_identity_changes_rejected(self):
        first = self.profiles.create("One")
        for kwargs in ({"game_directory": first.game_directory}, {"game_directory": str(Path(first.game_directory) / "nested")}):
            with self.assertRaises(ProfileError):
                self.profiles.create("Two", **kwargs)
        with self.assertRaises(ProfileError):
            self.profiles.create("one")
        with self.assertRaises(ProfileError):
            self.profiles.update(first.id, id="bad")
        with self.assertRaises(ProfileError):
            self.profiles.update(first.id, game_directory=str(self.home / "elsewhere"))

    def test_edit_and_rename_preserve_created_and_reset_install_on_version_change(self):
        first = self.profiles.create("One", installed_version="1.21.1")
        self.profiles.rename(first.id, "Renamed")
        updated = self.profiles.update(first.id, minecraft_version="1.21.2", memory_mb=4096)
        self.assertEqual(updated.created_at, first.created_at)
        self.assertEqual(updated.name, "Renamed")
        self.assertEqual(updated.installed_version, "")
        self.assertTrue(self.profiles.mark_launched(first.id).last_launch)

    def test_copy_copies_mods_but_never_worlds_or_tokens(self):
        first = self.profiles.create("One")
        self.jar(first)
        root = Path(first.game_directory)
        (root / "saves").mkdir()
        (root / "saves" / "private-world").write_bytes(b"world")
        (root / "config.json").write_text('{"refresh_token":"secret"}')
        clone = self.profiles.copy(first.id, "Copy")
        self.assertEqual((Path(clone.game_directory) / "mods" / "example.jar").read_bytes(), b"test mod")
        self.assertFalse((Path(clone.game_directory) / "saves").exists())
        self.assertFalse((Path(clone.game_directory) / "config.json").exists())

    def test_delete_requires_confirmation_and_preserves_worlds_by_default(self):
        profile = self.profiles.create("One")
        world = Path(profile.game_directory) / "saves" / "World"
        world.mkdir(parents=True)
        with self.assertRaises(ProfileError):
            self.profiles.delete(profile.id)
        self.assertEqual(len(self.profiles.list()), 1)
        self.profiles.delete(profile.id, confirmed=True)
        self.assertTrue(world.exists())
        self.assertEqual(self.profiles.list(), [])

    def test_world_deletion_needs_second_confirmation_and_managed_directory(self):
        profile = self.profiles.create("One")
        self.jar(profile)
        with self.assertRaises(ProfileError):
            self.profiles.delete(profile.id, confirmed=True, delete_worlds=True)
        self.profiles.delete(profile.id, confirmed=True, delete_worlds=True, confirm_worlds=True)
        self.assertFalse(Path(profile.game_directory).exists())
        legacy = self.profiles.get()
        with self.assertRaises(ProfileError):
            self.profiles.delete(legacy.id, confirmed=True, delete_worlds=True, confirm_worlds=True)

    def test_export_import_round_trip_uses_new_identity_and_no_host_paths(self):
        first = self.profiles.create("One", loader="fabric", java_path="/private/java", jvm_args=["-Dexample=private"])
        self.jar(first)
        archive_path = self.profiles.export_profile(first.id, self.home / "export.zip")
        with zipfile.ZipFile(archive_path) as archive:
            descriptor = json.loads(archive.read("profile.json"))["profile"]
            self.assertNotIn("game_directory", descriptor)
            self.assertNotIn("java_path", descriptor)
            self.assertEqual(descriptor["jvm_args"], [])
        imported = self.profiles.import_profile(archive_path, name="Imported")
        self.assertNotEqual(imported.id, first.id)
        self.assertEqual(imported.loader, "fabric")
        self.assertEqual((Path(imported.game_directory) / "mods" / "example.jar").read_bytes(), b"test mod")

    def test_archive_path_traversal_absolute_backslash_and_unexpected_files_rejected(self):
        for name in ("../escape", "/tmp/escape", "game/mods/../../escape", "game\\mods\\bad.jar", "C:/escape", "game/saves/World/level.dat", "credentials.json"):
            with self.subTest(name=name), self.assertRaises(ProfileError):
                self.profiles.import_profile(self.archive({name: b"bad"}))
            self.assertEqual(self.profiles.list(), [])

    def test_corrupt_and_symlink_archive_rejected_before_any_profile_created(self):
        source = self.home / "corrupt.zip"
        source.write_bytes(b"not zip")
        with self.assertRaises(ProfileError):
            self.profiles.import_profile(source)
        source = self.archive({})
        with zipfile.ZipFile(source, "a") as archive:
            info = zipfile.ZipInfo("game/mods/link.jar")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "/private")
        with self.assertRaises(ProfileError):
            self.profiles.import_profile(source)
        self.assertEqual(self.profiles.list(), [])

    def test_import_rejects_launch_argument_and_host_path_injection(self):
        for injection in ({"game_directory": "/private"}, {"jvm_args": ["-javaagent:evil.jar"]}, {"access_token": "SECRET"}):
            source = self.home / "unsafe.zip"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("profile.json", json.dumps({"schema_version": 1, "profile": {"name": "Bad", **injection}}))
            with self.assertRaises(ProfileError):
                self.profiles.import_profile(source)
        self.assertEqual(self.profiles.list(), [])

    def test_profile_args_cannot_replace_auth_or_inject_java_agents(self):
        for kwargs in ({"jvm_args": ["-javaagent:/bad.jar"]}, {"game_args": ["--accessToken", "secret"]}, {"game_args": ["--gameDir=/private"]}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ProfileError):
                self.profiles.create("Bad", **kwargs)

    def test_archive_size_limit_checked_without_extracting(self):
        source = self.archive({"game/mods/a.jar": b"large"})
        self.profiles.MAX_FILE_BYTES = 3
        with self.assertRaises(ProfileError):
            self.profiles.import_profile(source)
        self.assertEqual(self.profiles.list(), [])

    def test_export_excludes_common_secret_files_but_keeps_mod_settings(self):
        profile = self.profiles.create("One")
        config = Path(profile.game_directory) / "config"
        config.mkdir()
        for name in (".env.production", "credentials.json", "server.key", "private.pem", "tokens.json"):
            (config / name).write_text("SECRET", encoding="utf-8")
        (config / "mod-settings.json").write_text('{"quality":"high"}', encoding="utf-8")
        output = self.profiles.export_profile(profile.id, self.home / "public.zip")
        with zipfile.ZipFile(output) as archive:
            self.assertEqual(set(archive.namelist()), {"profile.json", "game/config/mod-settings.json"})

    def test_import_rejects_known_secret_file_names(self):
        for name in ("game/config/.env.production", "game/config/credentials.json", "game/config/server.key"):
            with self.subTest(name=name), self.assertRaises(ProfileError):
                self.profiles.import_profile(self.archive({name: b"SECRET"}))
        self.assertEqual(self.profiles.list(), [])


if __name__ == "__main__":
    unittest.main()
