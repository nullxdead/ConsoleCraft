"""Validated, atomic settings storage. Credentials never belong in this file."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
import json
import hashlib
import os
from pathlib import Path
import re
import tempfile
from uuid import UUID

from platformdirs import user_data_path

DEFAULT_CLIENT_ID = ""
DEFAULT_DISCORD_APP_ID = ""

class ConfigError(ValueError):
    """A readable, actionable configuration error."""


def validate_username(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_]{3,16}", value):
        raise ConfigError("Ник должен содержать 3–16 латинских букв, цифр или знаков _.")
    return value


def validate_client_id(value: str) -> str:
    if not isinstance(value, str):
        raise ConfigError("Client ID должен быть строкой UUID.")
    if not value:
        return ""
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise ConfigError("Microsoft Client ID должен быть UUID приложения из Azure.") from exc


@dataclass
class Settings:
    version: str = "latest"
    username: str = "Player"
    memory_mb: int = 2048
    java_path: str = ""
    client_id: str = DEFAULT_CLIENT_ID
    account: dict = field(default_factory=dict)
    schema_version: int = 2
    default_profile: str = ""
    download_timeout: int = 30
    download_workers: int = 8
    auto_check_updates: bool = True
    discord_app_id: str = DEFAULT_DISCORD_APP_ID
    browser_auto_open: bool = True
    after_launch: str = "status"

    def validate(self) -> None:
        validate_username(self.username)
        if type(self.memory_mb) is not int or not 512 <= self.memory_mb <= 65536:
            raise ConfigError("Объём RAM должен быть целым числом от 512 до 65536 МБ.")
        if not isinstance(self.version, str) or not self.version.strip() or len(self.version) > 120:
            raise ConfigError("Укажите непустой ID версии Minecraft (например, latest).")
        if self.version in {".", ".."} or any(c in self.version for c in '/\\\x00\r\n'):
            raise ConfigError("ID версии не должен содержать путь или управляющие символы.")
        if not isinstance(self.java_path, str) or any(c in self.java_path for c in '\x00\r\n'):
            raise ConfigError("Путь к Java должен быть строкой без управляющих символов.")
        self.client_id = validate_client_id(self.client_id)
        if type(self.schema_version) is not int or self.schema_version != 2:
            raise ConfigError("Эта версия конфигурации не поддерживается. Обновите лаунчер.")
        if not isinstance(self.default_profile, str):
            raise ConfigError("ID профиля должен быть строкой.")
        if self.default_profile:
            try:
                self.default_profile = str(UUID(self.default_profile))
            except ValueError as exc:
                raise ConfigError("Некорректный ID профиля по умолчанию.") from exc
        if type(self.download_timeout) is not int or not 5 <= self.download_timeout <= 300:
            raise ConfigError("Таймаут загрузок должен быть от 5 до 300 секунд.")
        if type(self.download_workers) is not int or not 1 <= self.download_workers <= 32:
            raise ConfigError("Число потоков загрузки должно быть от 1 до 32.")
        if type(self.auto_check_updates) is not bool:
            raise ConfigError("Проверка обновлений должна быть включена или отключена.")
        if not isinstance(self.discord_app_id, str) or (self.discord_app_id and not re.fullmatch(r"[0-9]{15,22}", self.discord_app_id)):
            raise ConfigError("Discord Application ID должен содержать от 15 до 22 цифр; пустое значение отключает RPC.")
        if type(self.browser_auto_open) is not bool or not isinstance(self.after_launch, str) or self.after_launch not in {"status", "keep"}:
            raise ConfigError("Некорректные настройки браузера или поведения после запуска.")
        if not isinstance(self.account, dict):
            raise ConfigError("Некорректное поле account в конфигурации.")
        # Public metadata only. Even a caller supplying token fields cannot persist them.
        self.account = {
            key: self.account[key]
            for key in ("username", "uuid", "kind")
            if key in self.account and isinstance(self.account[key], str)
        }


class ConfigStore:
    def __init__(self, home: str | Path | None = None):
        self.home = (
            Path(home).expanduser().resolve()
            if home is not None
            else Path(user_data_path("ConsoleCraft", appauthor=False))
        )
        self.path = self.home / "config.json"
        self._identifiers: dict[str, str] = {}
        self.identifiers_temporary = False
        self.identifier_backup_warning = False
        self._identifier_service = "ConsoleLauncher-identifiers:" + hashlib.sha256(str(self.home).encode("utf-8")).hexdigest()[:24]

    def _identifier_backend(self):
        from .auth import _secure_backend
        return _secure_backend()

    def _get_identifier(self, name: str) -> str:
        if name in self._identifiers:
            return self._identifiers[name]
        backend = self._identifier_backend()
        if backend is not None:
            try:
                value = backend.get_password(self._identifier_service, name) or ""
                self._identifiers[name] = value
                return value
            except Exception:
                self.identifiers_temporary = True
        return ""

    def _set_identifier(self, name: str, value: str) -> None:
        self._identifiers[name] = value
        backend = self._identifier_backend()
        if backend is None:
            if value:
                self.identifiers_temporary = True
            return
        try:
            if value:
                backend.set_password(self._identifier_service, name, value)
            elif backend.get_password(self._identifier_service, name) is not None:
                backend.delete_password(self._identifier_service, name)
        except Exception:
            self.identifiers_temporary = True

    def _sanitize_identifier_backup(self) -> None:
        backup = self.home / "config.v1.backup.json"
        if not backup.exists():
            return
        try:
            data = json.loads(backup.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not any(k in data for k in ("client_id", "discord_app_id")):
                return
            data.pop("client_id", None)
            data.pop("discord_app_id", None)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.home, prefix=".backup-", suffix=".tmp", delete=False) as handle:
                    temporary = Path(handle.name)
                    json.dump(data, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, backup)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        except (OSError, UnicodeError, ValueError):
            self.identifier_backup_warning = True

    @property
    def game_directory(self) -> Path:
        return self.home / "minecraft"

    @property
    def logs_directory(self) -> Path:
        return self.home / "logs"

    def load(self) -> Settings:
        if not self.path.exists():
            return Settings(client_id=self._get_identifier("client_id"), discord_app_id=self._get_identifier("discord_app_id"))
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ConfigError(
                f"Повреждён файл {self.path}. Исправьте JSON или переименуйте файл для сброса настроек."
            ) from exc
        if not isinstance(data, dict):
            raise ConfigError(f"В {self.path} должен находиться JSON-объект.")
        schema = data.get("schema_version", 1)
        if type(schema) is not int or schema not in {1, 2}:
            raise ConfigError("Эта версия конфигурации не поддерживается. Обновите лаунчер.")
        migrating = schema == 1
        old_identifiers = {key: data.pop(key) for key in ("client_id", "discord_app_id") if key in data}
        for value in old_identifiers.values():
            if not isinstance(value, str):
                raise ConfigError("Некорректный идентификатор в настройках.")
        Settings(client_id=old_identifiers.get("client_id", ""),
                 discord_app_id=old_identifiers.get("discord_app_id", "")).validate()
        for key, value in old_identifiers.items():
            self._set_identifier(key, value)
        data["client_id"] = self._get_identifier("client_id")
        data["discord_app_id"] = self._get_identifier("discord_app_id")
        data["schema_version"] = 2
        allowed = {item.name for item in fields(Settings)}
        settings = Settings(**{k: v for k, v in data.items() if k in allowed})
        settings.validate()
        if migrating:
            backup = self.home / "config.v1.backup.json"
            if not backup.exists():
                # Only known public settings are preserved; accidental token fields
                # from hand-edited configuration are not copied into a new file.
                public_backup = asdict(settings)
                public_backup.pop("client_id", None)
                public_backup.pop("discord_app_id", None)
                public_backup["schema_version"] = 1
                with backup.open("x", encoding="utf-8") as handle:
                    json.dump(public_backup, handle, ensure_ascii=False, indent=2)
            self.save(settings)
        elif old_identifiers:
            self.save(settings)
        self._sanitize_identifier_backup()
        return settings

    def save(self, settings: Settings) -> None:
        settings.validate()
        self._set_identifier("client_id", settings.client_id)
        self._set_identifier("discord_app_id", settings.discord_app_id)
        self.home.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.home, prefix=".config-", suffix=".tmp", delete=False
            ) as handle:
                temporary = Path(handle.name)
                public_settings = asdict(settings)
                public_settings.pop("client_id", None)
                public_settings.pop("discord_app_id", None)
                json.dump(public_settings, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
