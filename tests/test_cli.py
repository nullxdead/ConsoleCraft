from __future__ import annotations

from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from rich.console import Console

from consolecraft.auth import Account, AuthError, offline_account
from consolecraft.cli import Application, main, make_parser
from consolecraft.config import ConfigError, ConfigStore, DEFAULT_CLIENT_ID


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = ConfigStore(Path(self.temporary.name))
        self.output = io.StringIO()
        self.console_patch = patch("consolecraft.ui.console", Console(file=self.output, force_terminal=False, width=120))
        self.console_patch.start()
        self.addCleanup(self.console_patch.stop)
        self.env_patch = patch.dict("os.environ", {"CONSOLECRAFT_CLIENT_ID": ""})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def test_offline_launch_never_authenticates_and_no_install_stays_local(self):
        app = Application(self.store)
        app.minecraft = Mock()
        app.minecraft.launch.return_value = 0
        with patch("consolecraft.cli.AuthManager", side_effect=AssertionError("unexpected auth")):
            result = app.play("1.21.1", offline=True, username="Steve", no_install=True)
        self.assertEqual(result, 0)
        app.minecraft.install.assert_not_called()
        app.minecraft.launch.assert_called_once()
        args, kwargs = app.minecraft.launch.call_args
        self.assertEqual(args[:4], ("1.21.1", "Steve", offline_account("Steve").uuid, "0"))
        self.assertTrue(kwargs["offline"])
        self.assertFalse(kwargs["download_java"])
        self.assertEqual(kwargs["client_id"], "")
        self.assertEqual(kwargs["xuid"], "")

    def test_microsoft_failure_never_falls_back_to_offline_or_downloads_game(self):
        app = Application(self.store)
        app.minecraft = Mock()
        app._auth = Mock()
        app._auth_client_id = DEFAULT_CLIENT_ID
        app._auth.refresh.side_effect = AuthError("Сеанс истёк")
        with self.assertRaisesRegex(AuthError, "истёк"):
            app.play("latest")
        app.minecraft.install.assert_not_called()
        app.minecraft.launch.assert_not_called()

    def test_microsoft_identity_reaches_game_without_persisting_tokens(self):
        app = Application(self.store)
        app.minecraft = Mock()
        app.minecraft.launch.return_value = 0
        client_id = "12345678-1234-1234-1234-123456789abc"
        app._auth = Mock(client_id=client_id)
        app._auth_client_id = client_id
        app._auth.refresh.return_value = Account(
            "Alex", "a" * 32, "secret-access-token", expires_at=9999999999, xuid="12345"
        )
        with patch.dict("os.environ", {"CONSOLECRAFT_CLIENT_ID": client_id}):
            self.assertEqual(app.play("1.21.1", no_install=True), 0)
        args, kwargs = app.minecraft.launch.call_args
        self.assertEqual(args[3], "secret-access-token")
        self.assertEqual(kwargs["client_id"], client_id)
        self.assertEqual(kwargs["xuid"], "12345")
        self.assertFalse(kwargs["offline"])
        self.assertNotIn("secret-access-token", self.store.path.read_text(encoding="utf-8"))
        self.assertNotIn("secret-access-token", self.output.getvalue())

    def test_demo_passes_real_demo_flag(self):
        app = Application(self.store)
        app.minecraft = Mock()
        app.minecraft.launch.return_value = 0
        app.play("1.21.1", demo=True, no_install=True)
        self.assertTrue(app.minecraft.launch.call_args.kwargs["demo"])
        self.assertTrue(app.minecraft.launch.call_args.kwargs["offline"])

    def test_install_preserves_configured_java_and_saves_resolved_id(self):
        app = Application(self.store)
        app.settings.java_path = "/custom/java"
        app.minecraft = Mock()
        app.minecraft.install.return_value = "1.21.1"
        app.install("latest")
        self.assertEqual(app.minecraft.install.call_args.kwargs["java_path"], "/custom/java")
        self.assertEqual(self.store.load().version, "1.21.1")

    def test_no_install_alias_requires_explicit_id(self):
        app = Application(self.store)
        for alias in ("latest", "release", "snapshot", "latest-snapshot"):
            with self.subTest(alias=alias), self.assertRaises(ConfigError):
                app.play(alias, offline=True, no_install=True)

    def test_cli_mutually_exclusive_modes(self):
        with redirect_stdout(io.StringIO()), patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit) as error:
            make_parser().parse_args(["play", "--offline", "--demo"])
        self.assertEqual(error.exception.code, 2)

    def test_main_expected_failure_has_no_traceback_or_secret(self):
        with patch("consolecraft.cli.Application", side_effect=RuntimeError("secret-token")):
            code = main(["--home", str(self.store.home), "about"])
        self.assertEqual(code, 1)
        self.assertNotIn("secret-token", self.output.getvalue())
        self.assertNotIn("Traceback", self.output.getvalue())

    def test_game_failure_returns_nonzero_cli_status(self):
        app = Application(self.store)
        app.minecraft = Mock()
        app.minecraft.launch.return_value = -1073741819
        self.assertEqual(app.play("1.21.1", offline=True, no_install=True), 1)

    def test_default_entry_uses_cli(self):
        with patch("consolecraft.cli.Application") as application:
            application.return_value.run_command.return_value = 0
            self.assertEqual(main([]), 0)
            self.assertEqual(application.return_value.run_command.call_args.args[0].command, "cli")


if __name__ == "__main__":
    unittest.main()
