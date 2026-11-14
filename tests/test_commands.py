"""End-to-end command dispatch tests with real temporary profile/mod storage."""
from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

from rich.console import Console

from consolecraft import TELEGRAM_URL
from consolecraft.auth import Account
from consolecraft.cli import Application, make_parser
from consolecraft.config import ConfigError, ConfigStore, DEFAULT_CLIENT_ID, Settings
from consolecraft import commands


class CommandWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / "данные"
        self.store = ConfigStore(self.home)
        self.store.save(Settings(version="1.20.1"))
        self.output = io.StringIO()
        self.console_patch = patch("consolecraft.ui.console", Console(file=self.output, force_terminal=False, width=120))
        self.console_patch.start()
        self.addCleanup(self.console_patch.stop)
        self.env_patch = patch.dict("os.environ", {"CONSOLECRAFT_CLIENT_ID": ""})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.app = Application(self.store)

    def run_command(self, *arguments):
        return self.app.run_command(make_parser().parse_args(list(arguments)))

    def create(self, name="Fabric", **overrides):
        commands.current(self.app)
        fields = dict(minecraft_version="1.20.1", loader="fabric", loader_version="0.16.10", auth_mode="offline")
        fields.update(overrides)
        return commands.profile_store(self.app).create(name, **fields)

    def test_first_profile_command_migrates_existing_v1_profile(self):
        self.run_command("profiles", "create", "New", "--minecraft", "1.21.1")
        profiles = commands.profile_store(self.app).list()
        legacy = [profile for profile in profiles if profile.legacy]
        self.assertEqual(len(legacy), 1)
        self.assertEqual(legacy[0].minecraft_version, "1.20.1")
        self.assertEqual(Path(legacy[0].game_directory), self.store.game_directory)
        self.assertEqual(len(profiles), 2)

    def test_profile_cli_create_edit_rename_default_round_trip(self):
        self.run_command("profiles", "create", "Modded", "--minecraft", "1.20.1", "--loader", "fabric", "--loader-version", "0.16.10", "--auth", "offline", "--memory", "4096")
        profile = commands.profile_store(self.app).get("Modded")
        self.assertEqual((profile.loader, profile.memory_mb, profile.auth_mode), ("fabric", 4096, "offline"))
        self.run_command("profiles", "edit", profile.id, "--jvm-arg=-Dconsolecraft.test=true", "--game-arg=--fullscreen")
        self.run_command("profiles", "rename", profile.id, "Renamed")
        self.run_command("profiles", "default", "Renamed")
        saved = commands.profile_store(self.app).get()
        self.assertEqual(saved.id, profile.id)
        self.assertEqual(saved.jvm_args, ["-Dconsolecraft.test=true"])
        self.assertEqual(saved.game_args, ["--fullscreen"])

    def test_config_profile_updates_requested_profile_only(self):
        default = commands.current(self.app)
        other = self.create()
        self.run_command("config", "--profile", other.id, "--memory", "4096")
        self.assertEqual(commands.profile_store(self.app).get(other.id).memory_mb, 4096)
        self.assertEqual(commands.profile_store(self.app).get(default.id).memory_mb, default.memory_mb)
        self.assertEqual(self.store.load().default_profile, other.id)

    def test_copy_export_import_preserve_mods_and_never_copy_worlds(self):
        source = self.create()
        game = Path(source.game_directory)
        (game / "mods").mkdir()
        (game / "mods/example.jar.disabled").write_bytes(b"opaque mod bytes")
        (game / "saves/My World").mkdir(parents=True)
        (game / "saves/My World/level.dat").write_bytes(b"world")
        self.run_command("profiles", "copy", source.id, "Copy")
        copied = commands.profile_store(self.app).get("Copy")
        self.assertEqual((Path(copied.game_directory) / "mods/example.jar.disabled").read_bytes(), b"opaque mod bytes")
        self.assertFalse((Path(copied.game_directory) / "saves").exists())
        exported = Path(self.temporary.name) / "portable.zip"
        self.run_command("profiles", "export", source.id, str(exported))
        with zipfile.ZipFile(exported) as archive:
            self.assertNotIn("game/saves/My World/level.dat", archive.namelist())
        self.run_command("profiles", "import", str(exported), "--name", "Imported")
        imported = commands.profile_store(self.app).get("Imported")
        self.assertNotEqual(imported.id, source.id)
        self.assertNotEqual(imported.game_directory, source.game_directory)
        self.assertTrue((Path(imported.game_directory) / "mods/example.jar.disabled").exists())

    def test_profile_delete_requires_confirmation_and_preserves_worlds(self):
        profile = self.create()
        world = Path(profile.game_directory) / "saves/World/level.dat"
        world.parent.mkdir(parents=True)
        world.write_bytes(b"world")
        with patch("sys.stdin.isatty", return_value=False), self.assertRaises(ConfigError):
            self.run_command("profiles", "delete", profile.id)
        self.assertTrue(world.is_file())
        self.run_command("profiles", "delete", profile.id, "--yes")
        self.assertTrue(world.is_file())
        self.assertFalse(any(p.id == profile.id for p in commands.profile_store(self.app).list()))

    def test_profile_world_deletion_requires_independent_confirmation(self):
        profile = self.create()
        game = Path(profile.game_directory)
        (game / "saves").mkdir()
        with patch("sys.stdin.isatty", return_value=False), self.assertRaises(ConfigError):
            self.run_command("profiles", "delete", profile.id, "--yes", "--delete-worlds")
        self.assertTrue(game.exists())
        self.run_command("profiles", "delete", profile.id, "--yes", "--delete-worlds", "--confirm-worlds")
        self.assertFalse(game.exists())

    def test_loader_dispatch_pins_resolved_minecraft_and_loader_version(self):
        profile = self.create()
        self.app.minecraft = Mock()
        self.app.minecraft.resolve_version.return_value = "1.21.1"
        with patch("consolecraft.commands.LoaderService") as loader_class:
            loader = loader_class.return_value
            loader.loader_versions.return_value = ["0.16.10", "0.16.9"]
            loader.install.return_value = "fabric-loader-0.16.10-1.21.1"
            self.run_command("loaders", "install", "fabric", "1.21.1", "--profile", profile.id)
            loader.install.assert_called_once()
            self.assertEqual(loader.install.call_args.args, ("fabric", "1.21.1", "0.16.10"))
        saved = commands.profile_store(self.app).get(profile.id)
        self.assertEqual(saved.minecraft_version, "1.21.1")
        self.assertEqual(saved.loader_version, "0.16.10")
        self.assertEqual(saved.installed_version, "fabric-loader-0.16.10-1.21.1")

    def test_mods_install_and_confirmed_delete_use_selected_profile_only(self):
        first, second = self.create("One"), self.create("Two")
        source = Path(self.temporary.name) / "example.jar"
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("fabric.mod.json", json.dumps({"id": "example", "version": "1", "depends": {"minecraft": "1.20.1"}}))
        self.run_command("mods", "--profile", first.id, "add", str(source))
        installed = Path(first.game_directory) / "mods/example.jar"
        self.assertTrue(installed.is_file())
        self.assertFalse((Path(second.game_directory) / "mods").exists())
        self.run_command("mods", "--profile", first.id, "disable", "--all")
        self.assertTrue(installed.with_suffix(".jar.disabled").is_file())
        with patch("sys.stdin.isatty", return_value=False), self.assertRaises(ConfigError):
            self.run_command("mods", "--profile", first.id, "remove", "example.jar.disabled")
        self.run_command("mods", "--profile", first.id, "remove", "example.jar.disabled", "--yes")
        self.assertFalse(installed.with_suffix(".jar.disabled").exists())

    def test_modrinth_install_and_update_forward_exact_profile_and_version(self):
        profile = self.create()
        with patch("consolecraft.commands.ModrinthClient") as client_class:
            client = client_class.return_value
            client.install.return_value = []
            client.update.return_value = []
            self.run_command("modrinth", "--profile", profile.id, "install", "sodium", "--version-id", "specificversion")
            self.assertEqual(client.install.call_args.args, ("sodium", Path(profile.game_directory), "1.20.1", "fabric"))
            self.assertEqual(client.install.call_args.kwargs["version_id"], "specificversion")
            with patch("sys.stdin.isatty", return_value=False), self.assertRaises(ConfigError):
                self.run_command("modrinth", "--profile", profile.id, "update")
            client.update.assert_not_called()
            self.run_command("modrinth", "--profile", profile.id, "update", "--yes")
            self.assertTrue(client.update.call_args.kwargs["confirmed"])
            self.assertEqual(client.update.call_args.args[0], Path(profile.game_directory))

    def test_modrinth_browsing_default_vanilla_alias_omits_invalid_filters(self):
        profile = self.create("Browse", minecraft_version="latest", loader="vanilla", loader_version="")
        with patch("consolecraft.commands.ModrinthClient") as client_class:
            client = client_class.return_value
            client.search.return_value = []
            client.versions.return_value = []
            self.run_command("modrinth", "--profile", profile.id, "search", "sodium")
            self.assertEqual(client.search.call_args.kwargs["minecraft_version"], "")
            self.assertEqual(client.search.call_args.kwargs["loader"], "")
            self.run_command("modrinth", "--profile", profile.id, "versions", "sodium")
            self.assertEqual(client.versions.call_args.kwargs, {"minecraft_version": "", "loader": ""})
            with self.assertRaises(ConfigError):
                self.run_command("modrinth", "--profile", profile.id, "install", "sodium")
            client.install.assert_not_called()

    def test_profile_launch_isolates_mods_game_directory_and_arguments(self):
        first = self.create("One", memory_mb=4096, java_path="/custom/java", jvm_args=["-Dprofile=one"], game_args=["--width", "1024"])
        second = self.create("Two")
        commands.profile_store(self.app).update(first.id, installed_version="fabric-loader-0.16.10-1.20.1")
        self.app.minecraft = Mock()
        self.app.minecraft.launch.return_value = 0
        with patch("consolecraft.cli.AuthManager", side_effect=AssertionError("offline launch contacted Microsoft")):
            self.run_command("play", "--profile", first.id, "--no-install")
        self.app.minecraft.install.assert_not_called()
        args, kwargs = self.app.minecraft.launch.call_args
        self.assertEqual(args[0], "fabric-loader-0.16.10-1.20.1")
        self.assertEqual(kwargs["game_directory"], Path(first.game_directory))
        self.assertEqual(kwargs["java_path"], "/custom/java")
        self.assertEqual(kwargs["memory_mb"], 4096)
        self.assertEqual(kwargs["jvm_args"], ["-Dprofile=one"])
        self.assertEqual(kwargs["game_args"], ["--width", "1024"])
        self.assertTrue(kwargs["offline"])
        self.assertFalse(kwargs["download_java"])
        self.assertEqual(kwargs["client_id"], "")
        self.assertTrue(commands.profile_store(self.app).get(first.id).last_launch)
        self.assertEqual(commands.profile_store(self.app).get(second.id).last_launch, "")

    def test_menu_microsoft_login_opens_browser(self):
        client_id = "12345678-1234-1234-1234-123456789abc"
        manager = Mock(client_id=client_id)
        manager.login_browser.return_value = Account("Alex", "a" * 32, "secret-access", expires_at=9999999999)
        manager.storage_warning = None
        self.app.settings.client_id = client_id
        self.app._auth, self.app._auth_client_id = manager, client_id
        with patch("consolecraft.commands.Prompt.ask", side_effect=AssertionError("unexpected Client ID prompt")):
            commands.menu_action(self.app, "2")
        manager.login_browser.assert_called_once()
        self.assertEqual(self.store.load().account["username"], "Alex")
        self.assertNotIn("secret-access", self.store.path.read_text(encoding="utf-8"))

    def test_telegram_opens_only_on_explicit_action(self):
        with patch("consolecraft.commands.webbrowser.open", return_value=True) as browser:
            self.run_command("about")
            browser.assert_not_called()
            commands.current(self.app)
            browser.assert_not_called()
            self.run_command("telegram")
            browser.assert_called_once_with(TELEGRAM_URL)
        self.assertIn(TELEGRAM_URL, self.output.getvalue())

    def test_elyby_never_enables_on_microsoft_profile(self):
        profile = self.create(auth_mode="microsoft")
        with self.assertRaises(ConfigError):
            self.run_command("elyby", "enable", "--profile", profile.id)
        self.assertFalse(commands.profile_store(self.app).get(profile.id).elyby_enabled)


if __name__ == "__main__":
    unittest.main()
