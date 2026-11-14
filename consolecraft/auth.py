"""Microsoft browser login and OS-backed credential storage.

Tokens are never written to the launcher configuration or included in repr().
Only Windows Credential Manager, macOS Keychain, and Linux Secret Service /
libsecret backends are accepted. Without one, sign-in lasts for this process.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import base64
import hmac
import json
import re
import secrets
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
from uuid import UUID

import requests

from .config import DEFAULT_CLIENT_ID
from .auth_page import render_auth_page
from . import __version__


_OAUTH_BASE = "https://login.microsoftonline.com/consumers/oauth2/v2.0"
_SCOPE = "XboxLive.signin offline_access"
_HTTP_TIMEOUT = (10, 30)
_MICROSOFT_LOGIN_HOSTS = {"login.microsoftonline.com", "login.live.com"}
_SECURE_BACKENDS = {
    "keyring.backends.Windows",
    "keyring.backends.macOS",
    "keyring.backends.SecretService",
    "keyring.backends.libsecret",
}


class AuthError(RuntimeError):
    """A safe, user-facing authentication error without remote response data."""


class OAuthError(AuthError):
    """Microsoft OAuth rejected or could not complete authentication."""


class XboxError(AuthError):
    """Xbox Live / XSTS account or service failure."""


class MinecraftServiceError(AuthError):
    """Minecraft Services failure distinct from OAuth and Xbox failures."""


class AppNotPermittedError(MinecraftServiceError):
    """Minecraft API access is not approved or has been denied."""


class NoEntitlementError(MinecraftServiceError):
    """No Minecraft Java license or applicable Game Pass entitlement."""


@dataclass(frozen=True)
class Account:
    username: str
    uuid: str
    access_token: str = field(repr=False)
    kind: str = "microsoft"
    expires_at: float = 0
    xuid: str = ""

    def public_dict(self) -> dict[str, str]:
        """The only account fields permitted in normal configuration files."""
        return {"username": self.username, "uuid": self.uuid, "kind": self.kind}


@dataclass(frozen=True)
class DeviceCode:
    verification_uri: str
    user_code: str
    expires_in: int


def offline_account(username: str) -> Account:
    """Create the same name UUID used by Java offline-mode servers."""
    if not isinstance(username, str) or re.fullmatch(r"[A-Za-z0-9_]{3,16}", username) is None:
        raise AuthError("Ник должен содержать 3–16 латинских букв, цифр или символов _. ")
    digest = hashlib.md5(("OfflinePlayer:" + username).encode("utf-8"), usedforsecurity=False).digest()
    return Account(username, UUID(bytes=digest, version=3).hex, "0", "offline")


def _secure_backend() -> Any | None:
    try:
        import keyring

        backend = keyring.get_keyring()
        candidates = getattr(backend, "backends", [backend])
        for candidate in candidates:
            if type(candidate).__module__ in _SECURE_BACKENDS and candidate.priority > 0:
                return candidate
    except Exception:
        # An absent, locked or unsupported credential service must not result in
        # an unencrypted fallback (including third-party keyrings.alt plugins).
        pass
    return None


class AuthManager:
    """One account per client ID and launcher-data namespace."""

    def __init__(self, client_id: str = DEFAULT_CLIENT_ID, credential_namespace: str = "default") -> None:
        self.client_id = client_id.strip()
        namespace = hashlib.sha256(credential_namespace.encode("utf-8")).hexdigest()[:24]
        self._service = f"ConsoleCraft:{namespace}"
        self._key = hashlib.sha256(self.client_id.encode("utf-8")).hexdigest() if self.client_id else "unconfigured"
        self._legacy_key = self.client_id
        self._backend = _secure_backend()
        self._loaded = False
        self._persisted = False
        self._refresh_token: str | None = None
        self._account: Account | None = None
        self._username: str | None = None
        self.storage_warning: str | None = None
        self._http = requests.Session()
        self._http.headers.update({"User-Agent": f"ConsoleCraft/{__version__}", "Accept": "application/json"})

    def _require_client(self) -> None:
        if not self.client_id:
            raise AuthError(
                "Для Microsoft-входа укажите свой Client ID: config --client-id UUID "
                "или переменную CONSOLECRAFT_CLIENT_ID. Инструкция находится в README.md."
            )
        try:
            UUID(self.client_id)
        except ValueError:
            raise AuthError("Microsoft Client ID должен быть UUID приложения Azure / Entra.") from None

    def _load_credentials(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if self._backend is None:
            self.storage_warning = "Системное хранилище недоступно: вход сохранится только до закрытия лаунчера."
            return
        try:
            raw = self._backend.get_password(self._service, self._key)
            if not raw and self._legacy_key:
                raw = self._backend.get_password(self._service, self._legacy_key)
                if raw:
                    self._backend.set_password(self._service, self._key, raw)
                    self._backend.delete_password(self._service, self._legacy_key)
            if raw:
                saved = json.loads(raw)
                token = saved.get("refresh_token")
                if isinstance(token, str) and token:
                    self._refresh_token = token
                    username = saved.get("username")
                    if isinstance(username, str) and re.fullmatch(r"[A-Za-z0-9_]{1,16}", username):
                        self._username = username
                    self._persisted = True
        except Exception:
            self.storage_warning = "Не удалось открыть системное хранилище. Выполните Microsoft-вход заново."

    def _save_credentials(self) -> None:
        self._loaded = True
        if self._backend is None or not self._refresh_token:
            self._persisted = False
            self.storage_warning = "Системное хранилище недоступно: вход сохранится только до закрытия лаунчера."
            return
        try:
            self._backend.set_password(
                self._service,
                self._key,
                json.dumps({"refresh_token": self._refresh_token, "username": self._username}),
            )
            self._persisted = True
            self.storage_warning = None
        except Exception:
            self._persisted = False
            self.storage_warning = "Не удалось сохранить вход в системном хранилище. Сеанс доступен до закрытия лаунчера."

    def account_status(self) -> dict[str, Any]:
        """Report presence of credentials, without making network requests."""
        self._load_credentials()
        return {
            "enabled": bool(self.client_id),
            "available": bool(self._refresh_token or self._account),
            "session": bool(self._account and self._account.expires_at > time.time() + 60),
            "persisted": self._persisted,
            "username": self._username,
            "storage": "system" if self._backend is not None else "memory",
        }

    def _request(self, method: str, url: str, *, allow_error: bool = False, **kwargs: Any) -> dict[str, Any]:
        error_type = self._service_error_type(url)
        try:
            response = self._http.request(method, url, timeout=_HTTP_TIMEOUT, allow_redirects=False, **kwargs)
        except requests.RequestException:
            raise error_type("Сервис входа недоступен. Проверьте подключение к интернету и повторите попытку.") from None
        try:
            result = response.json()
        except ValueError:
            raise error_type("Сервис входа вернул некорректный ответ. Повторите попытку позже.") from None
        if not isinstance(result, dict):
            raise error_type("Сервис входа вернул неожиданный ответ.")
        if not response.ok and not allow_error:
            self._raise_remote_error(result, response.status_code, url)
        if 300 <= response.status_code < 400:
            raise error_type("Сервис входа неожиданно перенаправил запрос.")
        return result

    @staticmethod
    def _service_error_type(url: str) -> type[AuthError]:
        host = urlparse(url).hostname or ""
        if host.endswith("xboxlive.com"):
            return XboxError
        if host == "api.minecraftservices.com":
            return MinecraftServiceError
        return OAuthError

    @staticmethod
    def _raise_remote_error(result: dict[str, Any], status: int, url: str) -> None:
        # Never include server response bodies, error_description or requests
        # exceptions: those can contain bearer tokens or account information.
        xerr = result.get("XErr")
        if xerr == 2148916233:
            raise XboxError("Для этого Microsoft-аккаунта сначала создайте профиль Xbox на xbox.com.")
        if xerr in (2148916236, 2148916237, 2148916238):
            raise XboxError("Xbox требует подтверждения возраста или настройки семейного доступа на xbox.com.")
        if xerr == 2148916235:
            raise XboxError("Xbox Live недоступен для региона этого аккаунта.")
        if status == 403 and "api.minecraftservices.com" in url:
            raise AppNotPermittedError(
                "Minecraft Services отклонил доступ приложения. Одобрение ConsoleCraft для Minecraft Java API "
                "может ещё находиться на рассмотрении. Повторите позже или задайте разрешённый Client ID "
                "в расширенных настройках; обход авторизации не выполняется."
            )
        if status == 404 and url.endswith("/minecraft/profile"):
            raise NoEntitlementError("Профиль Minecraft Java не найден. Проверьте лицензию/Game Pass и создайте профиль в официальном лаунчере.")
        if result.get("error") in {"invalid_client", "unauthorized_client", "invalid_scope"}:
            raise OAuthError("Microsoft отклонил параметры приложения. Проверьте настройки приложения и адрес возврата http://localhost.")
        if result.get("error") in {"invalid_grant", "interaction_required", "login_required"}:
            raise OAuthError("Сохранённый вход истёк или отозван. Выполните login ещё раз.")
        if status == 429:
            raise AuthManager._service_error_type(url)("Слишком много запросов входа. Подождите несколько минут.")
        raise AuthManager._service_error_type(url)(f"Ошибка сервиса авторизации (HTTP {status}). Повторите вход позже.")

    def login(self, on_device_code: Callable[[DeviceCode], None], timeout: int = 900) -> Account:
        self._require_client()
        if timeout <= 0:
            raise AuthError("Время ожидания входа должно быть больше нуля.")
        data = self._request("POST", _OAUTH_BASE + "/devicecode", data={"client_id": self.client_id, "scope": _SCOPE})
        try:
            device_code = str(data["device_code"])
            user_code = str(data["user_code"])
            verification_uri = str(data["verification_uri"])
            expires_in = min(int(data["expires_in"]), timeout)
            interval = max(1, int(data.get("interval", 5)))
        except (KeyError, TypeError, ValueError):
            raise AuthError("Microsoft вернул неполный код авторизации.") from None
        parsed = urlparse(verification_uri)
        if parsed.scheme != "https" or parsed.hostname not in {
            "microsoft.com", "www.microsoft.com", "login.microsoftonline.com", "login.live.com",
        }:
            raise AuthError("Microsoft вернул неизвестный адрес для входа.")
        on_device_code(DeviceCode(verification_uri, user_code, expires_in))
        deadline = time.monotonic() + expires_in
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining < interval:
                break
            time.sleep(interval)
            token = self._request("POST", _OAUTH_BASE + "/token", allow_error=True, data={
                "client_id": self.client_id,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device_code,
            })
            error = token.get("error")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5
                continue
            if error in {"authorization_declined", "access_denied"}:
                raise AuthError("Microsoft-вход отменён пользователем.")
            if error in {"expired_token", "bad_verification_code"}:
                raise AuthError("Код входа истёк. Запустите login ещё раз.")
            if error:
                self._raise_remote_error(token, 400, _OAUTH_BASE)
            return self._finish(token)
        raise AuthError("Время ожидания Microsoft-входа истекло. Запустите login ещё раз.")

    @staticmethod
    def _redirect_registration_rejected(authorization_url: str) -> bool:
        """Detect Microsoft's explicit redirect mismatch without logging the URL."""
        url = authorization_url
        try:
            for _ in range(5):
                parsed = urlparse(url)
                if (parsed.scheme != "https" or parsed.hostname not in _MICROSOFT_LOGIN_HOSTS
                        or parsed.username or parsed.password or parsed.port not in (None, 443)):
                    return False
                response = requests.get(url, timeout=(3, 5), allow_redirects=False, stream=True)
                try:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if not location:
                            return False
                        url = urljoin(url, location)
                        continue
                    if response.status_code != 200:
                        return False
                    body = bytearray()
                    for chunk in response.iter_content(chunk_size=16384):
                        body.extend(chunk)
                        if len(body) > 1024 * 1024:
                            return False
                    text = body.decode("utf-8", errors="replace")
                    if "AADSTS50011" in text:
                        return True
                    error_code = re.search(r'<[^>]*\bid\s*=\s*["\']errorCode["\'][^>]*>\s*invalid_request\b', text, re.I)
                    error_description = re.search(r'<[^>]*\bid\s*=\s*["\']errorDescription["\'][^>]*>[^<]*redirect_uri', text, re.I)
                    return bool(error_code and error_description)
                finally:
                    response.close()
        except (requests.RequestException, OSError, ValueError):
            return False
        return False

    def login_browser(self, timeout: int = 300) -> Account:
        """Use Microsoft's system-browser authorization code flow with PKCE."""
        self._require_client()
        if timeout <= 0:
            raise AuthError("Время ожидания входа должно быть больше нуля.")
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
        state = secrets.token_urlsafe(32)
        result: dict[str, Any] = {}
        manager = self

        class Callback(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path != "/" or len(parsed.query) > 16000:
                    self.send_error(404)
                    return
                try:
                    values = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=20)
                except ValueError:
                    self.send_error(400)
                    return
                states = values.get("state", [])
                if len(states) != 1 or not hmac.compare_digest(states[0], state):
                    self.send_error(400)
                    return
                codes, errors = values.get("code", []), values.get("error", [])
                if len(codes) == 1 and codes[0] and not errors:
                    try:
                        token = manager._request("POST", _OAUTH_BASE + "/token", data={
                            "client_id": manager.client_id, "grant_type": "authorization_code", "code": codes[0],
                            "redirect_uri": redirect, "code_verifier": verifier, "scope": _SCOPE,
                        })
                        result["account"] = manager._finish(token)
                    except AuthError as exc:
                        result["failure"] = exc
                    except Exception:
                        result["failure"] = AuthError("Не удалось завершить вход. Повторите попытку.")
                elif len(errors) == 1 and errors[0] and not codes:
                    result["failure"] = AuthError("Microsoft-вход отменён или отклонён. Повторите вход.")
                else:
                    self.send_error(400)
                    return
                body = render_auth_page("account" in result)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        try:
            with HTTPServer(("127.0.0.1", 0), Callback) as server:
                server.timeout = 0.5
                redirect = f"http://localhost:{server.server_port}"
                authorization_url = _OAUTH_BASE + "/authorize?" + urlencode({
                    "client_id": self.client_id, "response_type": "code", "redirect_uri": redirect,
                    "response_mode": "query", "scope": _SCOPE, "state": state,
                    "code_challenge": challenge, "code_challenge_method": "S256",
                })
                if self._redirect_registration_rejected(authorization_url):
                    raise AuthError(
                        "Microsoft отклонил адрес возврата. В регистрации приложения Microsoft Entra "
                        "добавьте http://localhost для мобильных и классических приложений. "
                        "Альтернативный вход: consolelauncher login --device-code."
                    )
                if not webbrowser.open(authorization_url):
                    raise AuthError("Не удалось открыть браузер. Проверьте браузер по умолчанию и повторите вход.")
                deadline = time.monotonic() + timeout
                while not result and time.monotonic() < deadline:
                    server.handle_request()
        except OSError:
            raise AuthError("Не удалось открыть локальный адрес для Microsoft-входа.") from None
        if "failure" in result:
            raise result["failure"]
        if "account" not in result:
            raise AuthError("Время ожидания Microsoft-входа истекло. Запустите вход ещё раз.")
        return result["account"]

    def refresh(self) -> Account:
        self._require_client()
        if self._account and self._account.expires_at > time.time() + 60:
            return self._account
        self._load_credentials()
        if not self._refresh_token:
            raise AuthError("Microsoft-аккаунт не подключён. Сначала выполните login.")
        token = self._request("POST", _OAUTH_BASE + "/token", data={
            "client_id": self.client_id,
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
            "scope": _SCOPE,
        })
        return self._finish(token)

    def _finish(self, oauth: dict[str, Any]) -> Account:
        stage: type[AuthError] = OAuthError
        try:
            microsoft_token = oauth["access_token"]
            if not isinstance(microsoft_token, str) or not microsoft_token:
                raise ValueError
            refresh_token = oauth.get("refresh_token", self._refresh_token)
            if isinstance(refresh_token, str) and refresh_token:
                # Preserve rotated credentials even when a later Xbox/Minecraft
                # request fails. No Minecraft access tokens are persisted.
                self._refresh_token = refresh_token
                self._save_credentials()
            stage = XboxError
            xbl = self._request("POST", "https://user.auth.xboxlive.com/user/authenticate", json={
                "Properties": {"AuthMethod": "RPS", "SiteName": "user.auth.xboxlive.com", "RpsTicket": "d=" + microsoft_token},
                "RelyingParty": "http://auth.xboxlive.com",
                "TokenType": "JWT",
            })
            xsts = self._request("POST", "https://xsts.auth.xboxlive.com/xsts/authorize", json={
                "Properties": {"SandboxId": "RETAIL", "UserTokens": [xbl["Token"]]},
                "RelyingParty": "rp://api.minecraftservices.com/",
                "TokenType": "JWT",
            })
            userhash = xsts["DisplayClaims"]["xui"][0]["uhs"]
            xuid = xsts["DisplayClaims"]["xui"][0].get("xid", "")
            if not isinstance(xuid, str) or not xuid.isascii() or not xuid.isdecimal():
                xuid = ""
            stage = MinecraftServiceError
            minecraft = self._request("POST", "https://api.minecraftservices.com/authentication/login_with_xbox", json={
                "identityToken": f"XBL3.0 x={userhash};{xsts['Token']}",
            })
            access_token = minecraft["access_token"]
            if not isinstance(access_token, str) or not access_token:
                raise ValueError
            headers = {"Authorization": "Bearer " + access_token}
            entitlements = self._request("GET", "https://api.minecraftservices.com/entitlements/mcstore", headers=headers)
            if not isinstance(entitlements.get("items"), list):
                raise ValueError
            if not any(
                isinstance(item, dict) and item.get("name") in {"game_minecraft", "product_minecraft"}
                for item in entitlements["items"]
            ):
                raise NoEntitlementError("У аккаунта не найдена лицензия Minecraft Java Edition. Проверьте покупку или подписку Game Pass.")
            # A Java entitlement and profile are both required. Bedrock-only or
            # unrelated store products must not be treated as Java ownership.
            profile = self._request("GET", "https://api.minecraftservices.com/minecraft/profile", headers=headers)
            username = profile["name"]
            if not isinstance(username, str) or re.fullmatch(r"[A-Za-z0-9_]{1,16}", username) is None:
                raise ValueError
            account_uuid = UUID(profile["id"]).hex
            lifetime = int(minecraft.get("expires_in", 3600))
            if lifetime <= 0:
                raise ValueError
        except (KeyError, IndexError, TypeError, ValueError):
            raise stage("Сервис входа вернул неполные данные аккаунта. Повторите login.") from None
        account = Account(username, account_uuid, access_token, "microsoft", time.time() + lifetime, xuid)
        self._account = account
        self._username = username
        self._save_credentials()
        return account

    def logout(self) -> None:
        """Remove local credentials; this does not revoke Microsoft's consent."""
        self._load_credentials()
        self._refresh_token = None
        self._account = None
        self._username = None
        self._loaded = True
        if self._backend is not None:
            try:
                if self._backend.get_password(self._service, self._key) is not None:
                    self._backend.delete_password(self._service, self._key)
            except Exception:
                raise AuthError(
                    "Сеанс закрыт, но удалить сохранённый вход не удалось. "
                    "Удалите запись ConsoleCraft из системного хранилища паролей."
                ) from None
        self._persisted = False

