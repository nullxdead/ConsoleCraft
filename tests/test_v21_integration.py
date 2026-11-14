"""Account selection, profile relocation and browser settings integration."""
from __future__ import annotations

from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from rich.console import Console

from consolecraft import commands
from consolecraft.accounts import AccountStore
from consolecraft.auth import Account, offline_account
from consolecraft.cli import Application
from consolecraft.config import ConfigError, ConfigStore, DEFAULT_CLIENT_ID, Settings
from consolecraft.profiles import ProfileError


class Integration21Tests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = ConfigStore(self.root / "launcher")
        self.store.save(Settings(version="1.21.1"))
        console = patch("consolecraft.ui.console", Console(file=io.StringIO(), force_terminal=False))
        console.start(); self.addCleanup(console.stop)
        environment = patch.dict("os.environ", {"CONSOLECRAFT_CLIENT_ID": ""})
        environment.start(); self.addCleanup(environment.stop)
        self.app = Application(self.store)
        self.profiles = commands.profile_store(self.app)
        self.app.minecraft = Mock()
        self.app.minecraft.launch.return_value = 0

    def profile(self, name="Game", **fields):
        return self.profiles.create(name, minecraft_version="1.21.1", **fields)

    def test_selected_offline_account_overrides_microsoft_profile_without_auth(self):
        p = self.profile(auth_mode="microsoft")
        selected = AccountStore(self.store).create("OfflineUser")
        with patch("consolecraft.cli.AuthManager", side_effect=AssertionError("Microsoft must not be used")):
            self.assertEqual(self.app.play(profile=p.id, no_install=True, server="example.org", port=25566), 0)
        args, options = self.app.minecraft.launch.call_args
        self.assertEqual(args[1:4], ("OfflineUser", offline_account("OfflineUser").uuid, "0"))
        self.assertTrue(options["offline"])
        self.assertEqual(options["client_id"], "")
        self.assertEqual((options["server"], options["port"]), ("example.org", 25566))
        self.assertEqual(AccountStore(self.store).active()["id"], selected["id"])
        self.assertEqual(self.profiles.get(p.id).auth_mode, "microsoft")

    def test_explicit_microsoft_uses_real_refreshed_identity_on_offline_profile(self):
        p = self.profile(auth_mode="offline")
        AccountStore(self.store).create("LocalUser")
        real = Account("LicensedUser", "a" * 32, "synthetic-secret", xuid="123456")
        manager = Mock(client_id=DEFAULT_CLIENT_ID)
        manager.refresh.return_value = real
        self.app._auth = manager; self.app._auth_client_id = DEFAULT_CLIENT_ID
        self.assertEqual(self.app.play(profile=p.id, no_install=True, account="microsoft"), 0)
        manager.refresh.assert_called_once()
        args, options = self.app.minecraft.launch.call_args
        self.assertEqual(args[1:4], (real.username, real.uuid, real.access_token))
        self.assertFalse(options["offline"])
        self.assertEqual(options["xuid"], "123456")
        self.assertEqual(AccountStore(self.store).active(), {"kind": "microsoft"})
        self.assertEqual(self.profiles.get(p.id).auth_mode, "offline")
        self.assertNotIn(real.access_token, self.store.path.read_text(encoding="utf-8"))

    def test_legacy_offline_profile_and_explicit_demo_still_work(self):
        p = self.profile(auth_mode="offline")
        self.assertEqual(self.app.play(profile=p.id, no_install=True), 0)
        self.assertTrue(self.app.minecraft.launch.call_args.kwargs["offline"])
        AccountStore(self.store).create("OfflineUser")
        self.assertEqual(self.app.play(profile=p.id, no_install=True, demo=True), 0)
        self.assertTrue(self.app.minecraft.launch.call_args.kwargs["demo"])

    def test_selected_loader_version_is_checked_before_modded_launch(self):
        p = self.profile(loader="fabric", loader_version="0.20.1", installed_version="fabric-test", auth_mode="offline")
        with patch("consolecraft.commands.ModManager") as manager:
            manager.return_value.diagnostics.return_value = []
            self.assertEqual(self.app.play(profile=p.id, no_install=True), 0)
        manager.return_value.diagnostics.assert_called_once_with("1.21.1", "fabric", loader_version="0.20.1")
        self.assertEqual(self.app.minecraft.launch.call_args.args[0], "fabric-test")

    def test_browser_settings_round_trip_and_invalid_types_preserve_config(self):
        settings = replace(self.store.load(), browser_auto_open=False, after_launch="keep")
        self.store.save(settings)
        loaded = self.store.load()
        self.assertEqual((loaded.browser_auto_open, loaded.after_launch), (False, "keep"))
        original = self.store.path.read_bytes()
        for values in ({"browser_auto_open": "false"}, {"browser_auto_open": 0}, {"after_launch": "close-random"}, {"after_launch": []}):
            with self.subTest(values=values), self.assertRaises(ConfigError):
                self.store.save(replace(loaded, **values))
            self.assertEqual(self.store.path.read_bytes(), original)
        bad = json.loads(original)
        bad["browser_auto_open"] = "false"
        self.store.path.write_text(json.dumps(bad), encoding="utf-8")
        with self.assertRaises(ConfigError): self.store.load()

    def test_relocating_copies_worlds_mods_and_preserves_originals(self):
        p = self.profile(auth_mode="offline")
        source = Path(p.game_directory)
        (source / "saves" / "World").mkdir(parents=True)
        (source / "saves" / "World" / "level.dat").write_bytes(b"precious-world")
        (source / "mods").mkdir()
        (source / "mods" / "local.jar").write_bytes(b"local-mod-data")
        target = self.root / "new game directory"
        target.mkdir()
        changed = self.profiles.relocate(p.id, target)
        self.assertEqual(changed.id, p.id)
        self.assertEqual(Path(changed.game_directory), target.resolve())
        for relative in ("saves/World/level.dat", "mods/local.jar"):
            self.assertEqual((source / relative).read_bytes(), (target / relative).read_bytes())
        self.assertEqual(self.profiles.get(p.id).game_directory, str(target.resolve()))

    def test_relocating_rejects_nonempty_and_overlapping_targets(self):
        p = self.profile()
        source = Path(p.game_directory)
        target = self.root / "occupied"; target.mkdir()
        (target / "user.txt").write_text("preserve", encoding="utf-8")
        other = self.profile("Other")
        for destination in (target, source / "nested", source.parent, Path(other.game_directory)):
            with self.subTest(destination=destination), self.assertRaises(ProfileError):
                self.profiles.relocate(p.id, destination)
            self.assertEqual(self.profiles.get(p.id).game_directory, p.game_directory)
        self.assertEqual((target / "user.txt").read_text(), "preserve")

    def test_relocating_legacy_profile_keeps_shared_cache_in_original_location(self):
        p = self.profiles.get()
        self.assertTrue(p.legacy)
        source = Path(p.game_directory)
        for directory in ("assets", "libraries", "versions", "runtime", "saves"):
            path = source / directory; path.mkdir(parents=True, exist_ok=True)
            (path / "important.bin").write_bytes(directory.encode())
        target = self.root / "legacy relocated"
        moved = self.profiles.relocate(p.id, target)
        self.assertFalse(moved.legacy)
        self.assertTrue((target / "saves/important.bin").is_file())
        for directory in ("assets", "libraries", "versions", "runtime"):
            self.assertTrue((source / directory / "important.bin").is_file())
            self.assertFalse((target / directory).exists())

    def test_relocating_rejects_symlink_targets_and_source_files(self):
        p = self.profile()
        source = Path(p.game_directory)
        actual = self.root / "actual"; actual.mkdir()
        target_link = self.root / "target-link"
        try:
            target_link.symlink_to(actual, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("This host does not permit symlink creation")
        with self.assertRaises(ProfileError): self.profiles.relocate(p.id, target_link)
        secret = self.root / "secret.txt"; secret.write_text("outside", encoding="utf-8")
        (source / "linked.txt").symlink_to(secret)
        with self.assertRaises(ProfileError): self.profiles.relocate(p.id, self.root / "safe target")
        self.assertEqual(secret.read_text(), "outside")


if __name__ == "__main__": unittest.main()
