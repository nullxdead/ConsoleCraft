from __future__ import annotations
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from consolecraft.mods import ModError, ModManager, compatibility, inspect_jar, matches_version, safe_filename


def jar(path, data=None, extra=None):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("fabric.mod.json", json.dumps(data or {"id": "example", "name": "Example", "version": "1.0", "depends": {"minecraft": ">=1.20 <1.21"}}))
        for name, content in (extra or {}).items():
            archive.writestr(name, content)
    return path


class ModTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.manager = ModManager(self.root / "profile")

    def test_safe_metadata_install_enable_disable_remove(self):
        source = jar(self.root / "example.jar")
        info = self.manager.install_local(source, "1.20.1", "fabric")
        self.assertEqual(info.mod_ids, ["example"])
        self.assertEqual(info.name, "Example")
        self.assertEqual(len(info.sha512), 128)
        self.assertEqual(self.manager.list_mods("EXAMP")[0].filename, "example.jar")
        disabled = self.manager.set_enabled("example.jar", False)
        self.assertFalse(disabled.enabled)
        enabled = self.manager.set_enabled(disabled.filename, True)
        self.assertTrue(enabled.enabled)
        with self.assertRaises(ModError):
            self.manager.remove(enabled.filename)
        self.assertTrue((self.manager.directory / enabled.filename).exists())
        self.manager.remove(enabled.filename, confirmed=True)
        self.assertEqual(self.manager.list_mods(), [])

    def test_independent_directories(self):
        source = jar(self.root / "example.jar")
        self.manager.install_local(source)
        other = ModManager(self.root / "second")
        self.assertEqual(other.list_mods(), [])

    def test_incompatible_mod_refused_without_explicit_override(self):
        source = jar(self.root / "example.jar")
        with self.assertRaises(ModError):
            self.manager.install_local(source, "1.21.1", "forge")
        self.assertFalse(self.manager.directory.exists())
        self.assertEqual(self.manager.install_local(source, "1.21.1", "forge", True).name, "Example")

    def test_duplicate_id_and_existing_filename_not_overwritten(self):
        source = jar(self.root / "a.jar")
        self.manager.install_local(source)
        self.manager.install_local(source)  # Idempotent identical installation.
        other = jar(self.root / "b.jar")
        with self.assertRaisesRegex(ModError, "mod ID"):
            self.manager.install_local(other)
        jar(source, {"id": "different", "version": "2"})
        with self.assertRaisesRegex(ModError, "именем"):
            self.manager.install_local(source)

    def test_corrupt_jars_listed_but_not_installed(self):
        path = self.root / "broken.jar"
        path.write_bytes(b"not ZIP")
        with self.assertRaises(ModError):
            self.manager.install_local(path)
        self.manager.directory.mkdir(parents=True)
        (self.manager.directory / "broken.jar").write_bytes(b"broken")
        self.assertIn("повреждён", self.manager.list_mods()[0].error)
        disabled = self.manager.set_enabled("broken.jar", False)
        self.assertFalse(disabled.enabled)
        self.assertTrue(disabled.error)

    def test_traversal_and_absolute_zip_entries_refused(self):
        for entry in ("../outside", "/absolute", "C:/windows/test", "dir\\bad", "dir/../../outside"):
            with self.subTest(entry=entry):
                path = jar(self.root / "unsafe.jar")
                with zipfile.ZipFile(path, "a") as archive:
                    item = zipfile.ZipInfo()
                    item.filename = entry  # Bypass Windows ZipInfo slash normalization.
                    archive.writestr(item, "payload")
                with self.assertRaises(ModError):
                    inspect_jar(path)
        self.assertFalse((self.root.parent / "outside").exists())

    def test_symlink_and_enormous_metadata_entries_refused(self):
        path = self.root / "link.jar"
        with zipfile.ZipFile(path, "w") as archive:
            entry = zipfile.ZipInfo("link")
            entry.external_attr = 0o120777 << 16
            archive.writestr(entry, "../../secret")
        with self.assertRaises(ModError):
            inspect_jar(path)
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("fabric.mod.json", " " * (2 * 1024 * 1024 + 1))
        with self.assertRaisesRegex(ModError, "2 МБ"):
            inspect_jar(path)

    def test_invalid_names_refused(self):
        for name in ("../a.jar", "CON.jar", "NUL", "bad:name.jar", "a.jar ", "a\x00.jar"):
            with self.subTest(name=name), self.assertRaises(ModError):
                safe_filename(name)
        with self.assertRaises(ModError):
            self.manager.remove("../worlds", confirmed=True)

    def test_quilt_metadata_and_dependencies(self):
        path = self.root / "quilt.jar"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("quilt.mod.json", json.dumps({"quilt_loader": {"id": "quiltmod", "version": "1.2", "metadata": {"name": "Quilt Mod"}, "depends": [{"id": "minecraft", "versions": ">=1.20"}, {"id": "qsl", "versions": "*"}], "breaks": [{"id": "badmod", "versions": "*"}]}}))
        mod = inspect_jar(path)
        self.assertEqual(mod.loaders, ["quilt"])
        self.assertEqual(mod.dependencies["qsl"], "*")
        self.assertEqual(mod.conflicts["badmod"], "*")

    def test_forge_and_neoforge_toml_metadata(self):
        for filename, loader in (("META-INF/mods.toml", "forge"), ("META-INF/neoforge.mods.toml", "neoforge")):
            path = self.root / "forge.jar"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(filename, '''modLoader="javafml"
loaderVersion="[47,)"
[[mods]]
modId="example"
version="1.0"
displayName="Forge Example"
[[dependencies.example]]
modId="minecraft"
mandatory=true
versionRange="[1.20,1.21)"
side="BOTH"
''')
            mod = inspect_jar(path)
            self.assertEqual(mod.loaders, [loader])
            self.assertEqual(compatibility(mod, "1.20.1", loader), [])
            self.assertTrue(compatibility(mod, "1.21", loader))

    def test_required_missing_dependency_and_conflict_diagnostics(self):
        first = jar(self.root / "first.jar", {"id": "first", "version": "1", "depends": {"missing": ">=2"}, "breaks": {"second": "*"}})
        second = jar(self.root / "second.jar", {"id": "second", "version": "1"})
        self.manager.install_local(first)
        self.manager.install_local(second)
        issues = self.manager.diagnostics("1.20.1", "fabric")
        self.assertTrue(any("missing" in issue for issue in issues))
        self.assertTrue(any("конфликт" in issue for issue in issues))
        self.manager.set_all_enabled(False)
        self.assertEqual(self.manager.diagnostics("1.20.1", "fabric"), [])
        self.manager.set_all_enabled(True)
        self.assertTrue(all(mod.enabled for mod in self.manager.list_mods()))

    def test_version_constraints_and_unknown_syntax(self):
        for version, constraint, result in (("1.20.1", ">=1.20 <1.21", True), ("1.21", "[1.20,1.21)", False), ("1.20", "[1.20]", True), ("1.20.3", "1.20.x", True), ("1.21", ["1.20", "1.21"], True), ("1.20.1", "^1.20", True), ("2.0", "~1.20", False), ("26.3-snapshot", ">=26.1", True), ("1.20", "nonsense", None)):
            with self.subTest(version=version, constraint=constraint):
                self.assertIs(matches_version(version, constraint), result)

    def test_fabric_api_26_3_empty_prerelease_regression(self):
        for version in ("26.3", "26.3.1", "26.3-pre.1", "26.3-rc.2", "26.3+build.7"):
            with self.subTest(version=version):
                self.assertIs(matches_version(version, "~26.3-"), True)
        for version in ("26.2", "26.4-pre.1", "27.3"):
            with self.subTest(version=version):
                self.assertIs(matches_version(version, "~26.3-"), False)
        source = jar(self.root / "fabric-api-0.166.0+26.3.jar", {
            "id": "fabric-api", "name": "Fabric API", "version": "0.166.0+26.3",
            "depends": {"minecraft": "~26.3-", "fabricloader": ">=0.18.0"},
        })
        self.assertEqual(self.manager.install_local(source, "26.3", "fabric", loader_version="0.20.1").mod_ids, ["fabric-api"])
        self.assertTrue(compatibility(inspect_jar(source), "26.3", "fabric", "0.17.0"))

    def test_extended_fabric_semver_comparison_and_unknown_snapshots(self):
        cases = [("1.2.0", ">1.2-rc.9", True), ("1.2-rc.10", ">1.2-rc.2", True),
                 ("1.2-rc.1", "<1.2", True), ("1.3-alpha", "~1.2-", False),
                 ("0.9", "^0.2", True), ("1.1", "~1", False),
                 ("1.20-pre.1", "1.20.x", True), ("1.2.3+foo", "=1.2.3+bar", True),
                 ("24w14a", ">=1.20", None), ("24w14a", "24w14a", True)]
        for version, constraint, expected in cases:
            with self.subTest(version=version, constraint=constraint):
                self.assertIs(matches_version(version, constraint), expected)


if __name__ == "__main__":
    unittest.main()
