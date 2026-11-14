import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

import requests
from consolecraft.elyby import ElyByError, ElyByService


def jar_bytes():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("META-INF/MANIFEST.MF", "Premain-Class: example.Agent\n")
    return output.getvalue()


class ElyByTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = ElyByService(Path(self.temp.name))
        self.http = Mock()
        self.service._http = self.http
        self.jar = jar_bytes()
        self.digest = hashlib.sha256(self.jar).hexdigest()

    def responses(self, content=None, digest=None):
        metadata = Mock(status_code=200)
        metadata.json.return_value = {"checksums": {"sha256": digest or self.digest}, "download_url": "https://authlib-injector.yushi.moe/artifact/56/test.jar"}
        download = Mock(status_code=200, headers={"Content-Length": str(len(content or self.jar))})
        download.iter_content.return_value = [content or self.jar]
        self.http.get.side_effect = [metadata, download]

    def test_download_is_verified_and_cached(self):
        self.responses()
        path = self.service.prepare()
        self.assertEqual(path.read_bytes(), self.jar)
        args = self.service.arguments_for_verified(path)
        self.assertTrue(args[0].endswith("=https://authserver.ely.by/"))
        self.assertEqual(self.http.get.call_args.kwargs["timeout"], (10, 30))
        metadata = Mock(status_code=200)
        metadata.json.return_value = {"checksums": {"sha256": self.digest}, "download_url": "https://authlib-injector.yushi.moe/artifact/56/test.jar"}
        self.http.get.side_effect = [metadata]
        self.assertEqual(self.service.prepare(), path)

    def test_corrupt_download_never_becomes_active(self):
        self.responses(content=b"invalid", digest=self.digest)
        with self.assertRaisesRegex(ElyByError, "сумма"):
            self.service.prepare()
        self.assertEqual(list(self.service.directory.glob("*.jar")), [])
        self.assertEqual(list(self.service.directory.glob("*.part")), [])

    def test_tampered_cached_agent_is_rejected_before_launch(self):
        self.responses()
        path = self.service.prepare()
        path.write_bytes(b"tampered")
        with self.assertRaises(ElyByError):
            self.service.arguments_for_verified(path)

    def test_bad_zip_and_traversal_are_rejected_without_extraction(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("../outside", "bad")
            archive.writestr("META-INF/MANIFEST.MF", "Agent")
        for data in (b"not-a-jar", output.getvalue()):
            self.responses(content=data, digest=hashlib.sha256(data).hexdigest())
            with self.assertRaises(ElyByError):
                self.service.prepare()

    def test_microsoft_identity_can_never_be_sent_to_ely(self):
        for consent in (True, False):
            with self.assertRaisesRegex(ElyByError, "Microsoft"):
                self.service.jvm_arguments("microsoft", enabled=True, explicit_consent=consent)
        self.http.get.assert_not_called()
        self.assertEqual(self.service.jvm_arguments("microsoft", enabled=False), [])

    def test_offline_uses_verified_agent_only_after_opt_in(self):
        self.responses()
        self.assertEqual(self.service.jvm_arguments("offline", False), [])
        args = self.service.jvm_arguments("offline", True)
        self.assertEqual(len(args), 1)
        self.assertIn("-javaagent:", args[0])

    def test_public_skin_lookup_returns_valid_uuid_without_credentials(self):
        response = Mock(status_code=200)
        response.json.return_value = {"id": "a" * 32, "name": "Player", "properties": []}
        self.http.get.return_value = response
        result = self.service.profile("Player")
        self.assertEqual(result["uuid"], "a" * 32)
        self.assertNotIn("Authorization", self.http.get.call_args.kwargs)
        with self.assertRaises(ElyByError):
            self.service.profile("../escape")

    def test_missing_skin_and_network_errors_are_actionable(self):
        self.http.get.return_value = Mock(status_code=204)
        with self.assertRaisesRegex(ElyByError, "не найден"):
            self.service.profile("Player")
        self.http.get.side_effect = requests.Timeout("secret")
        with self.assertRaisesRegex(ElyByError, "соединиться") as result:
            self.service.prepare()
        self.assertNotIn("secret", str(result.exception))

    def test_redirects_untrusted_hosts_and_missing_hashes_rejected(self):
        for url in ("http://authlib-injector.yushi.moe/x", "https://evil.example/x", "https://name:secret@skinsystem.ely.by/x"):
            with self.assertRaises(ElyByError):
                self.service._get(url)
        self.http.get.return_value = Mock(status_code=302)
        with self.assertRaises(ElyByError):
            self.service.prepare()
        self.http.get.return_value = Mock(status_code=200)
        self.http.get.return_value.json.return_value = {"download_url": "https://evil.example/x"}
        with self.assertRaises(ElyByError):
            self.service.prepare()


if __name__ == "__main__":
    unittest.main()
