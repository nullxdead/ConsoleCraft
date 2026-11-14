from __future__ import annotations
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

import requests

from consolecraft.modrinth import ModrinthClient, ModrinthError
from consolecraft.mods import ModManager


def response(data, status=200):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(data).encode()
    return result


def mod_bytes(mod_id="example"):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("fabric.mod.json", json.dumps({"id": mod_id, "version": "1.0"}))
    return output.getvalue()


def version(identifier="version1", project="project1", content=None, dependencies=None):
    content = content or mod_bytes()
    return {"id": identifier, "project_id": project, "version_number": "1.0", "version_type": "release", "loaders": ["fabric"], "game_versions": ["1.20.1"], "dependencies": dependencies or [], "files": [{"filename": project + ".jar", "url": "https://cdn.modrinth.com/data/file.jar", "size": len(content), "hashes": {"sha512": hashlib.sha512(content).hexdigest()}, "primary": True}]}


class ModrinthTests(unittest.TestCase):
    def setUp(self):
        self.http = Mock()
        self.http.headers = {}
        self.client = ModrinthClient(self.http)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def download_response(self, content):
        result = Mock(status_code=200, headers={"Content-Length": str(len(content))})
        result.iter_content.return_value = [content[:10], content[10:]]
        self.http.get.return_value = result
        return result

    def test_search_encodes_filters_and_user_agent_with_timeout(self):
        self.http.request.return_value = response({"hits": [{"slug": "sodium"}]})
        self.assertEqual(self.client.search("sodium", "1.20.1", "fabric")[0]["slug"], "sodium")
        args, kwargs = self.http.request.call_args
        self.assertEqual(args[1], "https://api.modrinth.com/v2/search")
        self.assertIn(["categories:fabric"], json.loads(kwargs["params"]["facets"]))
        self.assertEqual(kwargs["timeout"], (10, 40))
        self.assertFalse(kwargs["allow_redirects"])
        self.assertIn("ConsoleLauncher", self.http.headers["User-Agent"])

    def test_description_authors_and_version_filters(self):
        self.http.request.side_effect = [response({"id": "project1", "team": "team1", "body": "description"}), response([{"user": {"username": "Author"}}]), response([version()])]
        self.assertEqual(self.client.project("project1")["authors"], ["Author"])
        self.assertEqual(len(self.client.versions("project1", "1.20.1", "fabric")), 1)
        self.assertEqual(json.loads(self.http.request.call_args.kwargs["params"]["loaders"]), ["fabric"])

    def test_rate_limit_has_retry_information(self):
        result = response({}, 429)
        result.headers["Retry-After"] = "30"
        self.http.request.return_value = result
        with self.assertRaisesRegex(ModrinthError, "30 секунд"):
            self.client.search("x")

    def test_timeout_and_invalid_json_report_safe_errors(self):
        self.http.request.side_effect = requests.Timeout("secret-token")
        with self.assertRaises(ModrinthError) as error:
            self.client.search("x")
        self.assertNotIn("secret-token", str(error.exception))
        self.http.request.side_effect = None
        bad = response({})
        bad._content = b"not JSON"
        self.http.request.return_value = bad
        with self.assertRaisesRegex(ModrinthError, "JSON"):
            self.client.search("x")

    def test_successful_real_bytes_install_and_hash_verification(self):
        content = mod_bytes()
        self.http.request.return_value = response([version(content=content)])
        download = self.download_response(content)
        events = []
        mods = self.client.install("project1", self.root / "profile", "1.20.1", "fabric", callback=lambda current, total: events.append((current, total)))
        self.assertEqual(mods[0].mod_ids, ["example"])
        self.assertTrue((self.root / "profile/mods/project1.jar").exists())
        self.assertEqual(events[-1], (len(content), len(content)))
        download.close.assert_called_once()

    def test_hash_failure_leaves_profile_unmodified(self):
        content = mod_bytes()
        self.http.request.return_value = response([version(content=content)])
        self.download_response(content[:-1] + b"X")
        with self.assertRaisesRegex(ModrinthError, "Контрольная"):
            self.client.install("project1", self.root / "profile", "1.20.1", "fabric")
        self.assertFalse((self.root / "profile/mods").exists())

    def test_staged_fabric_api_rejects_old_loader_before_profile_mutation(self):
        content = self.fabric_api_bytes()
        candidate = version(content=content)
        candidate["game_versions"] = ["26.3"]
        self.http.request.return_value = response([candidate])
        self.download_response(content)
        with self.assertRaisesRegex(ModrinthError, "fabric.*0.18.4.*0.16.0"):
            self.client.install("project1", self.root / "profile", "26.3", "fabric", loader_version="0.16.0")
        self.assertFalse((self.root / "profile/mods").exists())

    def test_staged_fabric_api_accepts_compatible_loader(self):
        content = self.fabric_api_bytes()
        candidate = version(content=content)
        candidate["game_versions"] = ["26.3"]
        self.http.request.return_value = response([candidate])
        self.download_response(content)
        result = self.client.install("project1", self.root / "profile", "26.3", "fabric", loader_version="0.20.1")
        self.assertEqual(result[0].mod_ids, ["fabric-api"])
        self.assertTrue((self.root / "profile/mods/project1.jar").exists())

    def test_update_with_incompatible_loader_restores_original_bytes(self):
        profile = self.root / "profile"
        original = self.root / "old.jar"
        original.write_bytes(mod_bytes("fabric-api"))
        ModManager(profile).install_local(original)
        content = self.fabric_api_bytes()
        candidate = version(content=content)
        candidate["game_versions"] = ["26.3"]
        self.http.request.return_value = response(candidate)
        self.download_response(content)
        updates = [{"filename": "old.jar", "project_id": "project1", "version_id": "version1"}]
        with patch.object(self.client, "check_updates", return_value=updates):
            with self.assertRaisesRegex(ModrinthError, "fabric"):
                self.client.update(profile, "26.3", "fabric", confirmed=True, loader_version="0.16.0")
        self.assertEqual((profile / "mods/old.jar").read_bytes(), original.read_bytes())
        self.assertEqual([path.name for path in (profile / "mods").iterdir()], ["old.jar"])

    @staticmethod
    def fabric_api_bytes():
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("fabric.mod.json", json.dumps({"id": "fabric-api", "name": "Fabric API", "version": "0.166.0+26.3", "depends": {"minecraft": "~26.3-", "fabricloader": ">=0.18.4"}}))
        return output.getvalue()

    def test_untrusted_hosts_traversal_and_missing_hash_refused(self):
        for mutate in (lambda item: item.update(url="https://evil.example/mod.jar"), lambda item: item.update(url="http://cdn.modrinth.com/mod.jar"), lambda item: item.update(filename="../escape.jar"), lambda item: item.update(hashes={}), lambda item: item.update(size=600 * 1024 * 1024)):
            candidate = version()
            mutate(candidate["files"][0])
            self.http.request.return_value = response([candidate])
            with self.subTest(candidate=candidate), self.assertRaises(Exception):
                self.client.install("project1", self.root / "profile", "1.20.1", "fabric")
        self.http.get.assert_not_called()

    def test_required_dependencies_download_before_root(self):
        dep_content, root_content = mod_bytes("dependency"), mod_bytes("root")
        root = version("rootv", "root", root_content, [{"dependency_type": "required", "project_id": "dep", "version_id": "depv"}])
        dep = version("depv", "dep", dep_content)
        self.http.request.side_effect = [response([root]), response(dep)]
        dep_download = Mock(status_code=200, headers={})
        dep_download.iter_content.return_value = [dep_content]
        root_download = Mock(status_code=200, headers={})
        root_download.iter_content.return_value = [root_content]
        self.http.get.side_effect = [dep_download, root_download]
        mods = self.client.install("root", self.root / "profile", "1.20.1", "fabric")
        self.assertEqual([mod.mod_ids[0] for mod in mods], ["dependency", "root"])

    def test_dependency_conflicting_versions_refused_before_download(self):
        root = version(dependencies=[{"dependency_type": "required", "project_id": "dep", "version_id": "one"}, {"dependency_type": "required", "project_id": "dep", "version_id": "two"}])
        self.http.request.side_effect = [response([root]), response(version("one", "dep")), response(version("two", "dep"))]
        with self.assertRaisesRegex(ModrinthError, "конфликтующие"):
            self.client.install("project1", self.root, "1.20.1", "fabric")
        self.http.get.assert_not_called()

    def test_incompatible_version_refused(self):
        self.http.request.return_value = response([version()])
        with self.assertRaisesRegex(ModrinthError, "несовместима"):
            self.client.install("project1", self.root, "1.21", "fabric")

    def test_update_check_uses_documented_hash_endpoint(self):
        source = self.root / "old.jar"
        source.write_bytes(mod_bytes())
        ModManager(self.root / "profile").install_local(source)
        update = version("updated", content=mod_bytes("changed"))
        self.http.request.return_value = response(update)
        result = self.client.check_updates(self.root / "profile", "1.20.1", "fabric")
        self.assertEqual(result[0]["version_id"], "updated")
        args, kwargs = self.http.request.call_args
        self.assertEqual(args[0], "POST")
        self.assertTrue(args[1].endswith("/update"))
        self.assertEqual(kwargs["params"], {"algorithm": "sha512"})
        with self.assertRaisesRegex(ModrinthError, "подтверждения"):
            self.client.update(self.root / "profile", "1.20.1", "fabric")

    def test_failed_update_restores_original_mod(self):
        source = self.root / "old.jar"
        content = mod_bytes()
        source.write_bytes(content)
        manager = ModManager(self.root / "profile")
        manager.install_local(source)
        with patch.object(self.client, "check_updates", return_value=[{"filename": "old.jar", "project_id": "project1", "version_id": "new"}]), patch.object(self.client, "install", side_effect=ModrinthError("network failed")):
            with self.assertRaises(ModrinthError):
                self.client.update(self.root / "profile", "1.20.1", "fabric", confirmed=True)
        self.assertEqual((manager.directory / "old.jar").read_bytes(), content)

    def test_installed_incompatible_project_rejected_before_download_without_jar_breaks(self):
        source = self.root / "existing.jar"
        source.write_bytes(mod_bytes("installed_mod_id"))
        profile = self.root / "profile"
        manager = ModManager(profile)
        manager.install_local(source)
        incoming = version(dependencies=[{"dependency_type": "incompatible", "project_id": "installedproject", "version_id": None}])
        self.http.request.side_effect = [response([incoming]), response({"id": "installedversion", "project_id": "installedproject"})]
        with self.assertRaisesRegex(ModrinthError, "existing.jar"):
            self.client.install("project1", profile, "1.20.1", "fabric")
        self.http.get.assert_not_called()
        self.assertEqual((manager.directory / "existing.jar").read_bytes(), source.read_bytes())
        self.assertEqual(len(manager.list_mods()), 1)
        lookup = self.http.request.call_args
        self.assertEqual(lookup.args[0], "GET")
        self.assertTrue(lookup.args[1].endswith("/version_file/" + manager.list_mods()[0].sha512))
        self.assertEqual(lookup.kwargs["params"], {"algorithm": "sha512"})

    def test_conflict_lookup_network_failure_never_silently_installs(self):
        source = self.root / "existing.jar"
        source.write_bytes(mod_bytes("installed"))
        profile = self.root / "profile"
        ModManager(profile).install_local(source)
        incoming = version(dependencies=[{"dependency_type": "incompatible", "version_id": "badversion"}])
        self.http.request.side_effect = [response([incoming]), requests.Timeout("secret")]
        with self.assertRaisesRegex(ModrinthError, "связаться"):
            self.client.install("project1", profile, "1.20.1", "fabric")
        self.http.get.assert_not_called()

    def test_index_missing_404_is_ignored_and_exact_incompatible_version_is_specific(self):
        source = self.root / "existing.jar"
        source.write_bytes(mod_bytes("installed"))
        manager = ModManager(self.root / "profile")
        manager.install_local(source)
        incoming = version(dependencies=[{"dependency_type": "incompatible", "version_id": "badversion", "project_id": "installedproject"}])
        # A different version from the same project does not match an exact exclusion.
        for lookup in (response({}, 404), response({"id": "goodversion", "project_id": "installedproject"})):
            self.http.request.return_value = lookup
            self.client._check_installed_conflicts([incoming], manager)
        self.http.request.return_value = response({"id": "badversion", "project_id": "installedproject"})
        with self.assertRaisesRegex(ModrinthError, "несовместимость"):
            self.client._check_installed_conflicts([incoming], manager)

    def test_no_incompatible_declarations_never_looks_up_installed_hashes(self):
        source = self.root / "existing.jar"
        source.write_bytes(mod_bytes("installed"))
        manager = ModManager(self.root / "profile")
        manager.install_local(source)
        self.client._check_installed_conflicts([version()], manager)
        self.http.request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
