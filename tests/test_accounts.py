from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import json
import tempfile
import unittest

from consolecraft.accounts import AccountError, AccountStore
from consolecraft.auth import offline_account
from consolecraft.config import ConfigStore


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.store = AccountStore(ConfigStore(self.home))

    def test_create_switch_rename_remove_persisted_without_credentials(self):
        self.assertEqual(self.store.active(), {"kind": "microsoft"})
        first = self.store.create("Steve")
        second = self.store.create("Alex")
        self.assertEqual(AccountStore(self.home).active(), second)
        self.store.select(first["id"])
        self.assertEqual(self.store.active()["uuid"], offline_account("Steve").uuid)
        changed = self.store.update(first["id"], "Notch")
        self.assertEqual(changed["id"], first["id"])
        self.assertEqual(changed["uuid"], offline_account("Notch").uuid)
        self.assertEqual(self.store.active()["username"], "Notch")
        with self.assertRaises(AccountError): self.store.remove(changed["id"])
        self.store.remove(changed["id"], confirmed=True)
        self.assertEqual(self.store.active(), {"kind": "microsoft"})
        self.assertEqual(self.store.list(), [second])
        self.assertFalse(any("token" in key for account in self.store.list() for key in account))
        self.assertEqual(list(self.home.glob(".accounts-*.tmp")), [])

    def test_microsoft_selection_does_not_create_any_token(self):
        self.store.create("Steve")
        self.store.select("microsoft")
        saved = self.store.path.read_text(encoding="utf-8")
        self.assertNotIn("access_token", saved)
        self.assertNotIn("refresh_token", saved)
        self.assertEqual(self.store.active(), {"kind": "microsoft"})

    def test_input_validation_duplicates_and_unknown_ids(self):
        for name in ("a", "a" * 17, "bad name", "кириллица", "../outside"):
            with self.subTest(name=name), self.assertRaises(AccountError): self.store.create(name)
        self.store.create("Steve")
        with self.assertRaises(AccountError): self.store.create("steve")
        self.store.create("Alex")
        with self.assertRaises(AccountError): self.store.update("Alex", "STEVE")
        with self.assertRaises(AccountError): self.store.select("missing")
        self.assertEqual(len(self.store.list()), 2)

    def test_untrusted_uuid_or_token_fields_never_used(self):
        account = self.store.create("Steve")
        data = json.loads(self.store.path.read_text(encoding="utf-8"))
        data["accounts"][0].update(uuid="someone-elses-uuid", access_token="secret")
        self.store.path.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(self.store.active()["uuid"], offline_account("Steve").uuid)
        self.assertNotIn("access_token", self.store.active())
        self.store.select(account["id"])
        self.assertNotIn("secret", self.store.path.read_text(encoding="utf-8"))

    def test_corrupted_json_kept_and_reported(self):
        self.store.path.write_text('{"broken":', encoding="utf-8")
        with self.assertRaises(AccountError): self.store.list()
        with self.assertRaises(AccountError): self.store.create("Steve")
        self.assertEqual(self.store.path.read_text(encoding="utf-8"), '{"broken":')

    def test_concurrent_store_instances_keep_all_local_accounts(self):
        def create(index):
            return AccountStore(self.home).create(f"Player_{index}")
        with ThreadPoolExecutor(max_workers=4) as pool:
            created = list(pool.map(create, range(16)))
        self.assertEqual({a["id"] for a in self.store.list()}, {a["id"] for a in created})
        self.assertIn(self.store.active()["id"], {a["id"] for a in created})


if __name__ == "__main__": unittest.main()
