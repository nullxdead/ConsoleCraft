"""Authentication behavior and credential boundaries; all HTTP calls are mocked."""

from __future__ import annotations

import json
import base64
import hashlib
import threading
import time
import traceback
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

import requests

from consolecraft.auth import Account, AuthError, AuthManager, _secure_backend, offline_account, OAuthError, XboxError, MinecraftServiceError, AppNotPermittedError, NoEntitlementError
from consolecraft.config import DEFAULT_CLIENT_ID


CLIENT_ID = "12345678-1234-1234-1234-123456789abc"
PROFILE_UUID = "12345678123412341234123456789abc"


class MemoryCredentialStore:
    """Emulate OS storage without touching the developer's real keychain."""

    def __init__(self):
        self.data = {}

    def get_password(self, service, key):
        return self.data.get((service, key))

    def set_password(self, service, key, value):
        self.data[service, key] = value

    def delete_password(self, service, key):
        del self.data[service, key]


def response(payload, status=200):
    result = Mock(spec=requests.Response)
    result.status_code = status
    result.ok = status < 400
    result.json.return_value = payload
    return result


class OfflineAccountTests(unittest.TestCase):
    def test_uuid_matches_java_offline_mode_and_is_case_sensitive(self):
        self.assertEqual(offline_account("Notch").uuid, "b50ad385829d3141a2167e7d7539ba7f")
        self.assertNotEqual(offline_account("Notch").uuid, offline_account("notch").uuid)
        self.assertEqual(offline_account("0xdead").kind, "offline")

    def test_invalid_player_names_are_rejected(self):
        for name in ("ab", "a" * 17, "../Steve", "two words", "игрок", "abc\n", None):
            with self.subTest(name=name), self.assertRaises(AuthError):
                offline_account(name)

    def test_public_metadata_and_repr_omit_tokens(self):
        account = Account("Tester", PROFILE_UUID, "SECRET_ACCESS")
        self.assertNotIn("SECRET_ACCESS", repr(account))
        self.assertEqual(account.public_dict(), {"username": "Tester", "uuid": PROFILE_UUID, "kind": "microsoft"})


class SecureBackendTests(unittest.TestCase):
    def test_plaintext_and_encrypted_file_plugins_are_not_accepted(self):
        # No custom file backend is used as a substitute for OS protection.
        for module in ("keyrings.alt.file", "keyring.backends.fail", "custom.encrypted_file"):
            backend = type("FakeBackend", (), {"__module__": module, "priority": 10})()
            with self.subTest(module=module), patch("keyring.get_keyring", return_value=backend):
                self.assertIsNone(_secure_backend())

    def test_chainer_selects_os_backend_and_skips_plaintext(self):
        plaintext = type("PlaintextKeyring", (), {"__module__": "keyrings.alt.file", "priority": 10})()
        secure = type("Keyring", (), {"__module__": "keyring.backends.SecretService", "priority": 5})()
        chainer = Mock(backends=[plaintext, secure])
        with patch("keyring.get_keyring", return_value=chainer):
            self.assertIs(_secure_backend(), secure)

    def test_backend_discovery_failure_becomes_memory_only(self):
        with patch("keyring.get_keyring", side_effect=RuntimeError("locked keychain")):
            self.assertIsNone(_secure_backend())


class AuthManagerTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryCredentialStore()
        self.backend_patch = patch("consolecraft.auth._secure_backend", return_value=self.store)
        self.backend_patch.start()
        self.addCleanup(self.backend_patch.stop)
        self.manager = AuthManager(CLIENT_ID, "test-profile")
        self.manager._http.request = Mock(side_effect=self.service_response)
        self.entitlements = [{"name": "game_minecraft"}]
        self.refresh_count = 0
        self.xuid = "2533274790000001"

    def service_response(self, method, url, **kwargs):
        if url.endswith("/devicecode"):
            return response({"device_code": "PRIVATE_DEVICE", "user_code": "ABCD-EFGH", "verification_uri": "https://microsoft.com/devicelogin", "expires_in": 900, "interval": 1})
        if url.endswith("/token"):
            self.refresh_count += 1
            return response({"access_token": "MS_SECRET", "refresh_token": f"REFRESH_{self.refresh_count}"})
        if url.endswith("/user/authenticate"):
            return response({"Token": "XBL_SECRET"})
        if url.endswith("/xsts/authorize"):
            return response({"Token": "XSTS_SECRET", "DisplayClaims": {"xui": [{"uhs": "USER_HASH", "xid": self.xuid}]}})
        if url.endswith("/login_with_xbox"):
            return response({"access_token": "MINECRAFT_SECRET", "expires_in": 3600})
        if url.endswith("/mcstore"):
            return response({"items": self.entitlements})
        if url.endswith("/minecraft/profile"):
            return response({"name": "Tester", "id": PROFILE_UUID})
        raise AssertionError(f"Unexpected HTTP call: {method} {url}")

    def sign_in(self):
        return self.manager._finish({"access_token": "MS_SECRET", "refresh_token": "REFRESH_INITIAL"})

    def test_full_authentication_chain_and_http_boundaries(self):
        account = self.sign_in()
        self.assertEqual(account.username, "Tester")
        self.assertEqual(account.uuid, PROFILE_UUID)
        self.assertEqual(account.access_token, "MINECRAFT_SECRET")
        self.assertEqual(account.xuid, "2533274790000001")
        self.assertNotIn("xuid", account.public_dict())
        self.assertGreater(account.expires_at, time.time() + 3500)
        calls = self.manager._http.request.call_args_list
        self.assertEqual(len(calls), 5)
        self.assertEqual(calls[0].kwargs["json"]["Properties"]["RpsTicket"], "d=MS_SECRET")
        self.assertEqual(calls[2].kwargs["json"]["identityToken"], "XBL3.0 x=USER_HASH;XSTS_SECRET")
        for call in calls:
            self.assertEqual(call.kwargs["timeout"], (10, 30))
            self.assertFalse(call.kwargs["allow_redirects"])
        saved = next(iter(self.store.data.values()))
        self.assertIn("REFRESH_INITIAL", saved)
        for secret in ("MINECRAFT_SECRET", "MS_SECRET", "XBL_SECRET", "XSTS_SECRET"):
            self.assertNotIn(secret, saved)

    def test_refresh_reuses_valid_session_then_rotates_expired_credentials(self):
        account = self.sign_in()
        self.manager._http.request.reset_mock()
        self.assertIs(self.manager.refresh(), account)
        self.manager._http.request.assert_not_called()
        self.manager._account = Account(account.username, account.uuid, "OLD_ACCESS", expires_at=time.time() - 1)
        self.assertEqual(self.manager.refresh().username, "Tester")
        saved = json.loads(next(iter(self.store.data.values())))
        self.assertEqual(saved["refresh_token"], "REFRESH_1")
        self.assertNotIn("OLD_ACCESS", json.dumps(saved))

    def test_xuid_is_optional_and_requires_ascii_decimal_string(self):
        for value in ("", None, 2533274790000001, "123abc", "１２３", "123\n", "-123"):
            with self.subTest(value=value):
                self.xuid = value
                self.assertEqual(self.sign_in().xuid, "")
        self.xuid = "1234567890123456"
        self.assertEqual(self.sign_in().xuid, self.xuid)

    def test_missing_xuid_claim_becomes_empty_string(self):
        original = self.service_response
        def without_xid(method, url, **kwargs):
            if url.endswith("/xsts/authorize"):
                return response({"Token": "XSTS_SECRET", "DisplayClaims": {"xui": [{"uhs": "USER_HASH"}]}})
            return original(method, url, **kwargs)
        self.manager._http.request.side_effect = without_xid
        self.assertEqual(self.sign_in().xuid, "")

    def test_rotated_refresh_survives_later_minecraft_failure(self):
        self.sign_in()
        self.manager._account = None
        original = self.service_response
        def fail_after_refresh(method, url, **kwargs):
            if url.endswith("/login_with_xbox"):
                return response({"error": "Forbidden", "error_description": "PRIVATE_DETAIL"}, 403)
            return original(method, url, **kwargs)
        self.manager._http.request.side_effect = fail_after_refresh
        with self.assertRaises(AuthError) as error:
            self.manager.refresh()
        self.assertNotIn("PRIVATE_DETAIL", str(error.exception))
        self.assertEqual(json.loads(next(iter(self.store.data.values())))["refresh_token"], "REFRESH_1")

    def test_java_entitlement_is_required_not_just_a_nonempty_list(self):
        for items in ([], [{"name": "game_minecraft_bedrock"}], [{"name": "unrelated_product"}], ["game_minecraft"], [{"name": "game_minecraft_fake"}]):
            with self.subTest(items=items):
                self.entitlements = items
                with self.assertRaises(AuthError):
                    self.sign_in()
                self.assertIsNone(self.manager._account)

    def test_both_known_java_entitlements_are_accepted(self):
        for name in ("game_minecraft", "product_minecraft"):
            with self.subTest(name=name):
                self.entitlements = [{"name": "game_minecraft_bedrock"}, {"name": name}]
                self.assertEqual(self.sign_in().username, "Tester")

    def test_profiles_and_client_ids_use_isolated_credential_records(self):
        self.sign_in()
        same = AuthManager(CLIENT_ID, "test-profile")
        self.assertEqual(same.account_status()["username"], "Tester")
        self.assertFalse(AuthManager(CLIENT_ID, "different-profile").account_status()["available"])
        self.assertFalse(AuthManager("00000000-0000-0000-0000-000000000000", "test-profile").account_status()["available"])
        self.manager.logout()
        self.assertFalse(self.store.data)
        self.assertFalse(self.manager.account_status()["available"])

    def test_existing_credentials_are_migrated_from_old_record_name(self):
        self.store.set_password(self.manager._service, CLIENT_ID, json.dumps({"refresh_token": "OLD_REFRESH", "username": "Tester"}))
        self.assertTrue(self.manager.account_status()["available"])
        self.assertIsNone(self.store.get_password(self.manager._service, CLIENT_ID))
        self.assertIn("OLD_REFRESH", self.store.get_password(self.manager._service, self.manager._key))

    def test_unavailable_keychain_keeps_refresh_only_in_memory(self):
        with patch("consolecraft.auth._secure_backend", return_value=None):
            manager = AuthManager(CLIENT_ID)
            manager._http.request = Mock(side_effect=self.service_response)
            self.assertEqual(manager._finish({"access_token": "MS_SECRET", "refresh_token": "MEMORY_SECRET"}).username, "Tester")
            self.assertFalse(manager.account_status()["persisted"])
            self.assertEqual(manager.account_status()["storage"], "memory")
            self.assertTrue(manager.storage_warning)
            self.assertFalse(self.store.data)

    def test_network_timeout_has_safe_message_and_no_exception_leak(self):
        self.manager._http.request.side_effect = requests.Timeout("SECRET_BEARER /private?token=SECRET")
        with self.assertRaises(AuthError) as error:
            self.sign_in()
        formatted = "".join(traceback.format_exception(error.exception))
        self.assertNotIn("SECRET", formatted)
        self.assertNotIn("/private", formatted)
        self.assertIsNone(self.manager._account)

    def test_missing_client_or_missing_credentials_has_actionable_error(self):
        for client in ("", "not-a-uuid"):
            with self.subTest(client=client), self.assertRaises(AuthError):
                AuthManager(client).refresh()
        with self.assertRaises(AuthError):
            self.manager.refresh()
        self.manager._http.request.assert_not_called()

    def test_no_client_id_is_bundled_without_configuration(self):
        self.assertEqual(AuthManager().client_id, DEFAULT_CLIENT_ID)
        self.assertEqual(DEFAULT_CLIENT_ID, "")

    def test_browser_login_uses_pkce_and_rejects_wrong_state(self):
        observed = {}
        threads = []
        def open_browser(url):
            query = parse_qs(urlparse(url).query)
            observed["query"] = query
            callback = query["redirect_uri"][0].replace("localhost", "127.0.0.1")
            def visit():
                wrong = requests.get(callback, params={"code": "wrong", "state": "invalid"}, timeout=3)
                observed["wrong"] = wrong.status_code
                accepted = requests.get(callback, params={"code": "synthetic-code", "state": query["state"][0]}, timeout=3)
                observed["accepted"] = accepted.status_code
            thread = threading.Thread(target=visit)
            threads.append(thread)
            thread.start()
            return True
        with patch.object(self.manager, "_redirect_registration_rejected", return_value=False), patch("consolecraft.auth.webbrowser.open", side_effect=open_browser):
            account = self.manager.login_browser(timeout=5)
        for thread in threads:
            thread.join(timeout=3)
        self.assertEqual(account.username, "Tester")
        self.assertEqual((observed["wrong"], observed["accepted"]), (400, 200))
        token_call = next(c for c in self.manager._http.request.call_args_list if c.args[1].endswith('/token'))
        data = token_call.kwargs['data']
        digest = base64.urlsafe_b64encode(hashlib.sha256(data['code_verifier'].encode('ascii')).digest()).rstrip(b'=').decode('ascii')
        self.assertEqual(digest, observed['query']['code_challenge'][0])
        self.assertEqual(data['grant_type'], 'authorization_code')
        self.assertEqual(data['code'], 'synthetic-code')

    def test_microsoft_redirect_mismatch_is_reported_without_waiting(self):
        redirect = Mock(status_code=302, headers={"Location": "https://login.live.com/authorize"})
        error = Mock(status_code=200, headers={})
        error.iter_content.return_value = [b'<span id="errorCode">invalid_request: </span><span id="errorDescription">Bad redirect_uri</span>']
        with patch('consolecraft.auth.requests.get', side_effect=[redirect, error]), \
                patch('consolecraft.auth.webbrowser.open') as browser:
            with self.assertRaisesRegex(AuthError, 'http://localhost'):
                self.manager.login_browser(timeout=5)
        browser.assert_not_called()
        self.manager._http.request.assert_not_called()
        redirect.close.assert_called_once()
        error.close.assert_called_once()

    def test_preflight_does_not_follow_untrusted_redirects(self):
        redirect = Mock(status_code=302, headers={"Location": "https://example.invalid/"})
        with patch('consolecraft.auth.requests.get', return_value=redirect) as get:
            self.assertFalse(self.manager._redirect_registration_rejected('https://login.microsoftonline.com/authorize'))
        self.assertEqual(get.call_count, 1)
        redirect.close.assert_called_once()

    def test_service_failures_have_distinct_types_and_never_echo_response(self):
        cases = [
            ({"error": "invalid_client"}, 400, "https://login.microsoftonline.com/consumers/oauth2/v2.0/token", OAuthError),
            ({"XErr": 2148916233}, 401, "https://xsts.auth.xboxlive.com/xsts/authorize", XboxError),
            ({"error": "Forbidden"}, 403, "https://api.minecraftservices.com/authentication/login_with_xbox", AppNotPermittedError),
            ({}, 404, "https://api.minecraftservices.com/minecraft/profile", NoEntitlementError),
            ({}, 503, "https://api.minecraftservices.com/entitlements/mcstore", MinecraftServiceError),
        ]
        for payload, status, url, error_type in cases:
            with self.subTest(error_type=error_type), self.assertRaises(error_type) as error:
                self.manager._raise_remote_error({**payload, "error_description": "SECRET_TOKEN"}, status, url)
            self.assertNotIn("SECRET_TOKEN", str(error.exception))

    def test_device_flow_obeys_pending_and_slow_down(self):
        responses = iter([
            response({"error": "authorization_pending"}, 400),
            response({"error": "slow_down"}, 400),
            response({"access_token": "MS_SECRET", "refresh_token": "DEVICE_REFRESH"}),
        ])
        def device_response(method, url, **kwargs):
            if url.endswith("/token"):
                return next(responses)
            return self.service_response(method, url, **kwargs)
        self.manager._http.request.side_effect = device_response
        shown = []
        with patch("consolecraft.auth.time.sleep") as sleep:
            account = self.manager.login(shown.append)
        self.assertEqual(account.username, "Tester")
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 1, 6])
        self.assertEqual(shown[0].user_code, "ABCD-EFGH")
        self.assertNotIn("PRIVATE_DEVICE", repr(shown))

    def test_device_flow_rejects_unexpected_verification_site(self):
        self.manager._http.request.return_value = response({
            "device_code": "PRIVATE_DEVICE", "user_code": "CODE", "verification_uri": "https://evil.example", "expires_in": 900,
        })
        self.manager._http.request.side_effect = None
        callback = Mock()
        with self.assertRaises(AuthError):
            self.manager.login(callback)
        callback.assert_not_called()

    def test_user_declining_device_login_stops_without_an_account(self):
        self.manager._http.request.side_effect = [
            response({"device_code": "PRIVATE_DEVICE", "user_code": "CODE", "verification_uri": "https://microsoft.com/devicelogin", "expires_in": 900, "interval": 1}),
            response({"error": "authorization_declined"}, 400),
        ]
        with patch("consolecraft.auth.time.sleep"), self.assertRaises(AuthError):
            self.manager.login(Mock())
        self.assertIsNone(self.manager._account)
        self.assertFalse(self.store.data)


if __name__ == "__main__":
    unittest.main()
