"""Catalogue recovery, local launches and child process handling."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

import requests

from consolecraft.minecraft import MinecraftError, MinecraftService, validate_version_id, server_address
from consolecraft.java import JavaRuntime


MANIFEST = {
    "latest": {"release": "1.21.1", "snapshot": "test-snapshot"},
    "versions": [
        {"id": "test-snapshot", "type": "snapshot", "releaseTime": "2025-02-01"},
        {"id": "1.21.1", "type": "release", "releaseTime": "2025-01-01"},
    ],
}


class MinecraftTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "game with spaces"
        self.service = MinecraftService(self.directory)

    def write_version(self, version="1.21.1", **overrides):
        directory = self.directory / "versions" / version
        directory.mkdir(parents=True, exist_ok=True)
        data = {
            "id": version, "type": "release", "releaseTime": "2025-01-01", "libraries": [],
            "javaVersion": {"component": "java-runtime-delta", "majorVersion": 21},
            "mainClass": "net.minecraft.client.main.Main", "assets": "test-assets",
            "arguments": {"jvm": ["-cp", "${classpath}"], "game": [
                "--username", "${auth_player_name}", "--accessToken", "${auth_access_token}",
                "--gameDir", "${game_directory}",
            ]},
            **overrides,
        }
        (directory / f"{version}.json").write_text(json.dumps(data), encoding="utf-8")
        (directory / f"{version}.jar").touch()

    def test_official_unusual_ids_and_malicious_paths(self):
        for value in ["1.21.1", "3D Shareware v1.34", "20w14∞"]:
            self.assertEqual(validate_version_id(value), value)
        for value in ["", "..", "../outside", "foo\\bar", "C:escape", "CON", "NUL.json", "trailing.", "bad\nname"]:
            with self.subTest(value=value), self.assertRaises(MinecraftError):
                validate_version_id(value)

    def test_manifest_filtering_marks_locally_installed_version(self):
        self.write_version()
        response = Mock()
        response.json.return_value = MANIFEST
        with patch("consolecraft.minecraft.requests.get", return_value=response) as get:
            versions = self.service.list_versions()
            self.assertEqual([(item["id"], item["installed"]) for item in versions], [("1.21.1", True)])
            self.assertEqual(self.service.resolve_version("snapshot"), "test-snapshot")
            self.assertEqual(len(self.service.list_versions(include_snapshots=True)), 2)
            get.assert_called_once()
            self.assertEqual(get.call_args.kwargs["timeout"], (10, 30))

    def test_manifest_cache_survives_connection_failure(self):
        response = Mock()
        response.json.return_value = MANIFEST
        with patch("consolecraft.minecraft.requests.get", return_value=response):
            self.assertEqual(self.service.resolve_version("latest"), "1.21.1")
        offline_service = MinecraftService(self.directory)
        with patch("consolecraft.minecraft.requests.get", side_effect=requests.ConnectionError()):
            self.assertEqual(offline_service.resolve_version("latest"), "1.21.1")
            self.assertTrue(offline_service.manifest_cached)

    def test_connection_failure_without_cache_is_actionable(self):
        with patch("consolecraft.minecraft.requests.get", side_effect=requests.ConnectionError()):
            with self.assertRaisesRegex(MinecraftError, "список версий"):
                self.service.list_versions()

    def test_installed_only_is_local_and_skips_incomplete_version(self):
        self.write_version()
        self.write_version("partial")
        (self.directory / "versions" / "partial" / "partial.jar").unlink()
        with patch("consolecraft.minecraft.requests.get", side_effect=AssertionError("network forbidden")):
            self.assertEqual([item["id"] for item in self.service.list_versions(installed_only=True)], ["1.21.1"])
            self.assertEqual(self.service.resolve_version("1.21.1"), "1.21.1")

    def test_inheritance_cycles_and_traversal_are_rejected(self):
        self.write_version("cycle", inheritsFrom="cycle")
        with self.assertRaisesRegex(MinecraftError, "Циклическое"):
            self.service.metadata("cycle")
        self.write_version("unsafe", inheritsFrom="../outside")
        with self.assertRaises(MinecraftError):
            self.service.metadata("unsafe")

    def test_offline_command_uses_local_java_and_preserves_paths_as_single_arguments(self):
        self.write_version()
        with patch("consolecraft.java._candidates", return_value=["local-java"]), patch(
            "consolecraft.java.java_major", return_value=21,
        ), patch("consolecraft.java.runtime.install_jvm_runtime", side_effect=AssertionError("download forbidden")), patch(
            "consolecraft.minecraft.requests.get", side_effect=AssertionError("network forbidden"),
        ):
            arguments = self.service.build_command(
                "1.21.1", "Player", "test-uuid", "secret-token", memory_mb=3072,
                offline=True, download_java=False,
            )
        self.assertEqual(arguments[0], "local-java")
        self.assertIn("-Xmx3072M", arguments)
        self.assertNotIn("--disableMultiplayer", arguments)
        self.assertIn("secret-token", arguments)
        self.assertEqual(Path(arguments[arguments.index("--gameDir") + 1]).resolve(), self.directory.resolve())

    def test_manual_java_survives_unavailable_mojang_runtime(self):
        self.write_version()

        def install_without_runtime(version, directory, callback):
            callback["setStatus"]("Install java runtime")
            raise KeyError("unsupported-platform")

        with patch("consolecraft.minecraft.install.install_minecraft_version", side_effect=install_without_runtime), patch(
            "consolecraft.minecraft.ensure_java", return_value=JavaRuntime("custom-java", 21),
        ) as ensure:
            self.assertEqual(self.service.install("1.21.1", java_path="custom-java"), "1.21.1")
            self.assertEqual(ensure.call_args.kwargs["configured"], "custom-java")

    def test_offline_server_connection_uses_standard_legacy_client_options(self):
        self.write_version()
        with patch("consolecraft.minecraft.ensure_java", return_value=JavaRuntime("java", 21)):
            command = self.service.build_command("1.21.1", "Steve", "offline-uuid", "0", offline=True,
                                                 server="localhost:25566", download_java=False)
        self.assertNotIn("--disableMultiplayer", command)
        self.assertEqual(command[command.index("--server") + 1], "localhost")
        self.assertEqual(command[command.index("--port") + 1], "25566")

    def test_modern_server_connection_uses_manifest_quick_play(self):
        self.write_version(arguments={"jvm": ["-cp", "${classpath}"], "game": [
            {"rules": [{"action": "allow", "features": {"is_quick_play_multiplayer": True}}],
             "value": ["--quickPlayMultiplayer", "${quickPlayMultiplayer}"]}]})
        with patch("consolecraft.minecraft.ensure_java", return_value=JavaRuntime("java", 21)):
            command = self.service.build_command("1.21.1", "Steve", "offline-uuid", "0", offline=True,
                                                 server="[::1]", port=25567, download_java=False)
            self.assertEqual(command[command.index("--quickPlayMultiplayer") + 1], "[::1]:25567")
            self.assertNotIn("--server", command)
            with self.assertRaises(MinecraftError):
                self.service.build_command("1.21.1", "Steve", "offline-uuid", "0", demo=True, server="localhost")

    def test_server_addresses_are_validated_without_network(self):
        self.assertEqual(server_address("example.org"), ("example.org", 25565))
        self.assertEqual(server_address("127.0.0.1:25566"), ("127.0.0.1", 25566))
        self.assertEqual(server_address("::1", None), ("::1", 25565))
        for server, port in [("https://example.org", 25565), ("bad host", 25565), ("-flag", 25565),
                             ("example.org", 0), ("example.org", True), ("example.org:99999", 25565),
                             ("[::1", 25565), ("example.org/path", 25565), ("example.org:25566", 25567)]:
            with self.subTest(server=server, port=port), self.assertRaises(MinecraftError): server_address(server, port)

    def test_modern_client_id_and_xuid_placeholders_are_substituted(self):
        self.write_version()
        raw = ["java", "--clientId", "${clientid}", "--xuid", "${auth_xuid}", "-Dclient=${clientid}"]
        with patch("consolecraft.minecraft.ensure_java", return_value=JavaRuntime("java", 21)), patch(
            "consolecraft.minecraft.command.get_minecraft_command", return_value=raw,
        ):
            arguments = self.service.build_command(
                "1.21.1", "Player", "id", "token", client_id="application-id", xuid="123456789",
            )
        self.assertEqual(arguments, ["java", "--clientId", "application-id", "--xuid", "123456789", "-Dclient=application-id"])

    def test_missing_offline_credentials_preserve_empty_argument_positions(self):
        self.write_version()
        with patch("consolecraft.minecraft.ensure_java", return_value=JavaRuntime("java", 21)), patch(
            "consolecraft.minecraft.command.get_minecraft_command",
            return_value=["java", "--clientId", "${clientid}", "--xuid", "${auth_xuid}", "--demo"],
        ):
            arguments = self.service.build_command("1.21.1", "Player", "id", "0", offline=True)
        self.assertEqual(arguments, ["java", "--clientId", "", "--xuid", "", "--demo"])

    def test_future_unknown_manifest_arguments_require_launcher_update(self):
        self.write_version()
        with patch("consolecraft.minecraft.ensure_java", return_value=JavaRuntime("java", 21)), patch(
            "consolecraft.minecraft.command.get_minecraft_command", return_value=["java", "${future_argument}"],
        ), self.assertRaisesRegex(MinecraftError, "Обновите лаунчер"):
            self.service.build_command("1.21.1", "Player", "id", "token")

    def test_manual_java_does_not_hide_failed_game_downloads(self):
        self.write_version()
        with patch("consolecraft.minecraft.install.install_minecraft_version", side_effect=OSError("disk full")), patch(
            "consolecraft.minecraft.ensure_java",
        ) as ensure:
            with self.assertRaises(MinecraftError):
                self.service.install("1.21.1", java_path="custom-java")
            ensure.assert_not_called()

    def test_installer_cannot_report_success_without_game_jar(self):
        self.write_version()
        (self.directory / "versions" / "1.21.1" / "1.21.1.jar").unlink()
        with patch("consolecraft.minecraft.install.install_minecraft_version"), patch(
            "consolecraft.minecraft.ensure_java",
        ) as ensure:
            with self.assertRaisesRegex(MinecraftError, "Файл игры"):
                self.service.install("1.21.1")
            ensure.assert_not_called()

    def test_process_output_and_log_redact_access_token_and_keep_exit_code(self):
        secret = "secret-game-token"
        log = Path(self.temporary.name) / "logs" / "game.log"
        lines = []
        arguments = [sys.executable, "-c", "print('secret-game-token'); raise SystemExit(3)"]
        self.directory.mkdir()
        with patch.object(self.service, "build_command", return_value=arguments) as build:
            code = self.service.launch(
                "1.21.1", "Player", "id", secret, log_file=log, on_output=lines.append,
                client_id="application-id", xuid="123456789",
            )
        self.assertEqual(build.call_args.args[-2:], ("application-id", "123456789"))
        self.assertEqual(code, 3)
        self.assertEqual(lines, ["[REDACTED]"])
        self.assertNotIn(secret, log.read_text(encoding="utf-8"))
        self.assertIn("[REDACTED]", log.read_text(encoding="utf-8"))

    def test_offline_sentinel_does_not_redact_every_zero(self):
        self.directory.mkdir()
        lines = []
        with patch.object(self.service, "build_command", return_value=[sys.executable, "-c", "print('RAM 2048; code 0')"]):
            self.service.launch("1.21.1", "Player", "id", "0", offline=True, on_output=lines.append)
        self.assertEqual(lines, ["RAM 2048; code 0"])

    def test_interrupt_terminates_then_kills_unresponsive_game(self):
        def interrupted_output():
            yield "Starting game\n"
            raise KeyboardInterrupt

        process = MagicMock()
        process.__enter__.return_value = process
        process.stdout = interrupted_output()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("java", 5), 0]
        with patch.object(self.service, "build_command", return_value=["java"]), patch(
            "consolecraft.minecraft.subprocess.Popen", return_value=process,
        ), self.assertRaises(KeyboardInterrupt):
            self.service.launch("1.21.1", "Player", "id", "token", on_output=lambda line: None)
        process.terminate.assert_called_once()
        process.kill.assert_called_once()


if __name__ == "__main__":
    unittest.main()
