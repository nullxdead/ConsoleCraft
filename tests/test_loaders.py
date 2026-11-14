import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from consolecraft.java import JavaRuntime
from consolecraft.loaders import LoaderError, LoaderService, LOADERS
from consolecraft.minecraft import MinecraftService, library_limits


class LoaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.service = LoaderService(self.root)
        self.loader = Mock()
        self.loader.get_loader_versions.return_value = ["1.2.3", "1.2.2"]
        self.loader.get_minecraft_versions.return_value = ["1.21.1"]
        self.loader.get_installed_version.return_value = "fabric-loader-1.2.3-1.21.1"
        self.loader.install.return_value = "fabric-loader-1.2.3-1.21.1"
        self.loader_patch = patch("consolecraft.loaders.mod_loader.get_mod_loader", return_value=self.loader)
        self.loader_patch.start()
        self.addCleanup(self.loader_patch.stop)

    def test_all_four_loaders_use_public_api(self):
        for name in LOADERS:
            self.assertEqual(self.service.minecraft_versions(name), ["1.21.1"])
            self.assertEqual(self.service.loader_versions(name, "1.21.1"), ["1.2.3", "1.2.2"])
        with self.assertRaises(LoaderError):
            self.service.minecraft_versions("../../loader")

    def test_network_failure_is_actionable(self):
        self.loader.get_minecraft_versions.side_effect = requests.Timeout("private-data")
        with self.assertRaisesRegex(LoaderError, "сеть") as result:
            self.service.minecraft_versions("fabric")
        self.assertNotIn("private-data", str(result.exception))

    def test_incompatible_version_does_not_start_installer(self):
        with patch.object(self.service.minecraft, "resolve_version", return_value="1.21.1"), self.assertRaisesRegex(LoaderError, "не совместима"):
            self.service.install("fabric", "1.21.1", "9.9")
        self.loader.install.assert_not_called()

    def test_install_checks_result_before_success(self):
        with patch.object(self.service.minecraft, "resolve_version", return_value="1.21.1"), patch.object(
            self.service.minecraft, "install"
        ), patch.object(self.service.minecraft, "metadata", return_value={}), patch(
            "consolecraft.loaders.ensure_java", return_value=JavaRuntime("java21", 21)
        ), patch.object(self.service, "verify", return_value=[]) as verify:
            self.assertEqual(self.service.install("fabric", "1.21.1"), "fabric-loader-1.2.3-1.21.1")
            verify.assert_called_once_with("fabric-loader-1.2.3-1.21.1")
            self.assertEqual(self.loader.install.call_args.kwargs["java"], "java21")
            verify.return_value = ["missing library"]
            with self.assertRaisesRegex(LoaderError, "проверку"):
                self.service.install("fabric", "1.21.1")
            self.loader.install.return_value = "unexpected"
            with self.assertRaisesRegex(LoaderError, "неожиданное"):
                self.service.install("fabric", "1.21.1")

    def write_version(self, name, **fields):
        folder = self.root / "versions" / name
        folder.mkdir(parents=True, exist_ok=True)
        data = {"id": name, "libraries": [], "mainClass": "Main", "type": "release", "arguments": {"jvm": ["-cp", "${classpath}"], "game": []}, **fields}
        (folder / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")
        return folder

    def test_inherited_profile_checks_installer_created_client_jar(self):
        base = self.write_version("1.21.1", javaVersion={"majorVersion": 21})
        (base / "1.21.1.jar").write_bytes(b"client")
        child = self.write_version("fabric-test", inheritsFrom="1.21.1")
        (child / "fabric-test.jar").write_bytes(b"client")
        self.assertIn("fabric-test", [x["id"] for x in self.service.minecraft.installed_versions()])
        self.assertEqual(self.service.verify("fabric-test"), [])

    def test_integrity_detects_corrupt_classpath_and_assets(self):
        digest = hashlib.sha1(b"client").hexdigest()
        base = self.write_version("1.21.1", downloads={"client": {"sha1": digest}}, assetIndex={"id": "test"})
        (base / "1.21.1.jar").write_bytes(b"corrupt-client")
        asset_digest = hashlib.sha1(b"asset").hexdigest()
        indexes = self.root / "assets" / "indexes"
        indexes.mkdir(parents=True)
        (indexes / "test.json").write_text(json.dumps({"objects": {"test": {"hash": asset_digest}}}))
        problems = self.service.verify("1.21.1")
        self.assertTrue(any("Повреждён" in value for value in problems))
        self.assertTrue(any("assets" in value for value in problems))

    def test_metadata_paths_cannot_escape_installation(self):
        base = self.write_version("1.21.1", libraries=[{"name": "a:b:1", "downloads": {"artifact": {"path": "../../evil.jar"}}}])
        (base / "1.21.1.jar").write_bytes(b"client")
        with self.assertRaises(LoaderError):
            self.service.verify("1.21.1")

    def test_request_and_installer_timeouts_are_scoped(self):
        original_request = requests.sessions.Session.request
        with patch("consolecraft.minecraft.requests.sessions.Session.request", return_value=Mock()) as request:
            with library_limits(17):
                requests.get("https://example.com/")
                self.assertEqual(request.call_args.kwargs["timeout"], (10, 17))
            self.assertIs(requests.sessions.Session.request, request)
        self.assertIs(requests.sessions.Session.request, original_request)

    def test_official_installer_stream_hash_checked_before_execution(self):
        content = b"official installer bytes"
        response = requests.Response()
        response.status_code = 200
        response.raw = io.BytesIO(content)
        checksum = Mock(status_code=200, text=hashlib.sha1(content).hexdigest())
        with patch("consolecraft.minecraft.requests.sessions.Session.request", side_effect=[response, checksum]):
            with library_limits():
                downloaded = requests.get("https://maven.fabricmc.net/fabric-installer-1.jar", stream=True)
                self.assertEqual(b"".join(downloaded.iter_content(8192)), content)
        response = requests.Response()
        response.status_code = 200
        response.raw = io.BytesIO(b"tampered")
        with patch("consolecraft.minecraft.requests.sessions.Session.request", side_effect=[response, checksum]):
            with library_limits(), self.assertRaisesRegex(Exception, "сумма"):
                downloaded = requests.get("https://maven.fabricmc.net/fabric-installer-1.jar", stream=True)
                b"".join(downloaded.iter_content(8192))
        response = requests.Response()
        response.status_code = 200
        response.raw = io.BytesIO(b"tampered-raw")
        with patch("consolecraft.minecraft.requests.sessions.Session.request", side_effect=[response, checksum]):
            with library_limits(), self.assertRaisesRegex(Exception, "сумма"):
                downloaded = requests.get("https://maven.fabricmc.net/fabric-installer-1.jar", stream=True)
                import shutil
                shutil.copyfileobj(downloaded.raw, io.BytesIO())

    def test_repair_quarantines_managed_corruption_preserving_worlds(self):
        base = self.write_version("1.21.1")
        (base / "1.21.1.jar").write_bytes(b"client")
        child = self.write_version("fabric-loader-1.2.3-1.21.1", inheritsFrom="1.21.1")
        broken = child / "fabric-loader-1.2.3-1.21.1.jar"
        broken.touch()
        world = self.root / "saves" / "world" / "level.dat"
        world.parent.mkdir(parents=True)
        world.write_bytes(b"precious world")
        with patch.object(self.service, "install", return_value=child.name):
            self.assertEqual(self.service.repair("fabric", "1.21.1", "1.2.3"), child.name)
        self.assertFalse(broken.exists())
        self.assertEqual(world.read_bytes(), b"precious world")
        self.assertEqual(len(list((self.root / ".consolecraft-quarantine").rglob("*.jar"))), 1)

    def test_corrupt_loader_metadata_is_preserved_for_recovery(self):
        base = self.write_version("1.21.1")
        (base / "1.21.1.jar").write_bytes(b"client")
        child = self.write_version("fabric-loader-1.2.3-1.21.1")
        original = child / f"{child.name}.json"
        original.write_text("broken", encoding="utf-8")
        with patch.object(self.service, "install", return_value=child.name):
            self.service.repair("fabric", "1.21.1", "1.2.3")
        backups = list((self.root / ".consolecraft-quarantine").rglob("*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), "broken")

    def test_repair_can_recover_corrupt_vanilla_metadata(self):
        base = self.write_version("1.21.1")
        original = base / "1.21.1.json"
        original.write_text("broken-base", encoding="utf-8")
        with patch.object(self.service, "install", return_value="fabric-test"):
            self.service.repair("fabric", "1.21.1", "1.2.3")
        backups = list((self.root / ".consolecraft-quarantine").rglob("*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), "broken-base")

    def test_profile_arguments_and_directory_are_isolated(self):
        self.write_version("1.21.1", javaVersion={"majorVersion": 21})
        game = self.root / "separate profile"
        with patch("consolecraft.minecraft.ensure_java", return_value=JavaRuntime("java21", 21)):
            args = self.service.minecraft.build_command("1.21.1", "Player", "id", "0", offline=True,
                game_directory=game, jvm_args=["-Dexample=true"], game_args=["--width", "1024"])
        self.assertIn("-Dexample=true", args)
        self.assertEqual(args[-2:], ["--width", "1024"])
        with patch("consolecraft.minecraft.ensure_java", return_value=JavaRuntime("java21", 21)), self.assertRaisesRegex(Exception, "аккаунт"):
            self.service.minecraft.build_command("1.21.1", "Player", "id", "0", game_args=["--accessToken=bad"])
        with self.assertRaisesRegex(Exception, "Java-agent"):
            self.service.minecraft.build_command("1.21.1", "Player", "id", "token", jvm_args=["-javaagent:evil.jar"])


if __name__ == "__main__":
    unittest.main()
