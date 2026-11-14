import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

from consolecraft.catalog import install_modpack, install_pack
from consolecraft.config import ConfigStore
from consolecraft.modrinth import ModrinthClient, ModrinthError
from consolecraft.profiles import ProfileStore


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.http = Mock()
        self.http.headers = {}
        self.client = ModrinthClient(self.http)
        self.profiles = ProfileStore(ConfigStore(self.root / 'data'))
        self.profiles.ensure_default()

    def pack(self, path='mods/example.jar', bad_hash=False):
        payload = b'mod file'
        index = {'formatVersion': 1, 'game': 'minecraft', 'name': 'Example',
                 'dependencies': {'minecraft': '1.20.1', 'fabric-loader': '0.16.0'},
                 'files': [{'path': path, 'fileSize': len(payload),
                            'hashes': {'sha512': '0' * 128 if bad_hash else hashlib.sha512(payload).hexdigest()},
                            'downloads': ['https://cdn.modrinth.com/data/example.jar']}]}
        package = self.root / 'example.mrpack'
        with zipfile.ZipFile(package, 'w') as archive:
            archive.writestr('modrinth.index.json', json.dumps(index))
            archive.writestr('overrides/config/example.toml', 'works=true')
        response = Mock(status_code=200, headers={})
        response.iter_content.return_value = [payload]
        self.http.get.return_value = response
        return package

    def prepare(self, package, kind='modpack'):
        project = {'project_type': kind, 'title': 'Example'}
        version = {'version_type': 'release', 'files': [{'filename': package.name, 'primary': True}]}
        return patch.multiple(self.client, _request=Mock(return_value=project), versions=Mock(return_value=[version]),
                              _download=Mock(return_value=package))

    def test_modpack_creates_isolated_profile_and_selects_it(self):
        package = self.pack()
        with self.prepare(package):
            profile = install_modpack(self.client, 'example', self.profiles)
        self.assertEqual((profile.minecraft_version, profile.loader, profile.loader_version), ('1.20.1', 'fabric', '0.16.0'))
        self.assertEqual((Path(profile.game_directory) / 'mods/example.jar').read_bytes(), b'mod file')
        self.assertEqual((Path(profile.game_directory) / 'config/example.toml').read_text(), 'works=true')
        self.assertEqual(self.profiles.get().id, profile.id)

    def test_bad_hash_and_path_do_not_create_profile(self):
        for path, bad_hash in [('mods/example.jar', True), ('../escape.jar', False)]:
            with self.subTest(path=path, bad_hash=bad_hash):
                package = self.pack(path, bad_hash)
                with self.prepare(package), self.assertRaises(ModrinthError):
                    install_modpack(self.client, 'example', self.profiles)
                self.assertEqual(len(self.profiles.list()), 1)
                self.assertFalse((self.root / 'escape.jar').exists())

    def test_shader_zip_is_installed_without_overwriting(self):
        package = self.root / 'shader.zip'
        with zipfile.ZipFile(package, 'w') as archive:
            archive.writestr('shaders/test.glsl', 'test')
        profile = self.profiles.get()
        with self.prepare(package, 'shader'):
            target = install_pack(self.client, 'shader', 'example', profile)
            self.assertEqual(target.read_bytes(), package.read_bytes())
            with self.assertRaisesRegex(ModrinthError, 'существует'):
                install_pack(self.client, 'shader', 'example', profile)

    def test_search_uses_selected_category(self):
        result = Mock(status_code=200, content=b'{}')
        result.json.return_value = {'hits': []}
        self.http.request.return_value = result
        self.client.search('', project_type='modpack')
        facets = json.loads(self.http.request.call_args.kwargs['params']['facets'])
        self.assertIn(['project_type:modpack'], facets)

