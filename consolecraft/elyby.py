"""Opt-in Ely.by public skins for local profiles; never handles account passwords."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import tempfile
from urllib.parse import urlparse, quote
from uuid import UUID
import zipfile

import requests
from . import __version__

ARTIFACT_API = "https://authlib-injector.yushi.moe/artifact/latest.json"
ELY_AUTH_ROOT = "https://authserver.ely.by/"
MAX_AGENT_SIZE = 32 * 1024 * 1024


class ElyByError(RuntimeError):
    pass


class ElyByService:
    def __init__(self, cache_directory: Path | str, timeout: int = 30):
        self.directory = Path(cache_directory).expanduser().resolve()
        self.timeout = timeout
        self._http = requests.Session()
        self._http.headers.update({"User-Agent": f"ConsoleCraft/{__version__} (https://github.com/nullxdead/ConsoleLauncher)"})

    def _get(self, url: str, **kwargs):
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in {"authlib-injector.yushi.moe", "skinsystem.ely.by"} or parsed.username or parsed.password:
            raise ElyByError("Небезопасный адрес компонента Ely.by.")
        try:
            response = self._http.get(url, timeout=(10, self.timeout), allow_redirects=False, **kwargs)
            if 300 <= response.status_code < 400:
                raise ElyByError("Сервис Ely.by вернул перенаправление. Повторите позднее.")
            if response.status_code == 429:
                raise ElyByError("Слишком много запросов Ely.by. Повторите позднее.")
            if response.status_code not in {200, 204, 404}:
                raise ElyByError("Сервис Ely.by временно недоступен.")
            return response
        except requests.RequestException:
            raise ElyByError("Не удалось соединиться с Ely.by или сервером authlib-injector.") from None

    def profile(self, username: str) -> dict:
        if not isinstance(username, str) or re.fullmatch(r"[A-Za-z0-9_]{3,16}", username) is None:
            raise ElyByError("Укажите корректный ник для поиска скина Ely.by.")
        response = self._get("https://skinsystem.ely.by/profile/" + quote(username, safe=""))
        if response.status_code in {204, 404}:
            raise ElyByError("Скин Ely.by для этого ника не найден. Используйте другой ник или отключите Ely.by.")
        try:
            data = response.json()
            account_id = UUID(data["id"]).hex
            name = data["name"]
            if not isinstance(name, str) or re.fullmatch(r"[A-Za-z0-9_]{1,16}", name) is None:
                raise ValueError
            return {"uuid": account_id, "id": account_id, "username": name, "name": name, "properties": data.get("properties", [])}
        except (ValueError, TypeError, KeyError):
            raise ElyByError("Ely.by вернул некорректный профиль скина.") from None

    @staticmethod
    def _verify_jar(path: Path, expected: str) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ElyByError("Отсутствует корректная SHA-256 компонента Ely.by.")
        if not path.is_file() or not 0 < path.stat().st_size <= MAX_AGENT_SIZE:
            raise ElyByError("Некорректный размер authlib-injector.")
        with path.open("rb") as handle:
            if hashlib.file_digest(handle, "sha256").hexdigest() != expected:
                raise ElyByError("Контрольная сумма authlib-injector не совпала.")
        try:
            with zipfile.ZipFile(path) as archive:
                entries = archive.infolist()
                if len(entries) > 10000 or sum(item.file_size for item in entries) > 128 * 1024 * 1024:
                    raise ElyByError("Архив authlib-injector превышает безопасные ограничения.")
                for item in entries:
                    if item.filename.startswith(("/", "\\")) or ".." in item.filename.replace("\\", "/").split("/"):
                        raise ElyByError("Небезопасный путь в архиве authlib-injector.")
                if "META-INF/MANIFEST.MF" not in archive.namelist():
                    raise ElyByError("Загруженный файл не является Java-agent.")
        except (zipfile.BadZipFile, OSError):
            raise ElyByError("Повреждён архив authlib-injector.") from None

    def prepare(self, callback: dict | None = None) -> Path:
        response = self._get(ARTIFACT_API)
        try:
            data = response.json()
            digest = data["checksums"]["sha256"]
            url = data["download_url"]
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError
            parsed = urlparse(url)
            if parsed.scheme != "https" or parsed.hostname != "authlib-injector.yushi.moe" or not parsed.path.startswith("/artifact/"):
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise ElyByError("Сервер authlib-injector вернул некорректные данные загрузки.") from None
        self.directory.mkdir(parents=True, exist_ok=True)
        destination = self.directory / f"authlib-injector-{digest[:16]}.jar"
        if destination.is_file():
            try:
                self._verify_jar(destination, digest)
                destination.with_suffix(".sha256").write_text(digest, encoding="ascii")
                return destination
            except ElyByError:
                pass  # A corrupt managed cache is replaced only after a verified download.
        callbacks = callback or {}
        callbacks.get("setStatus", lambda *_: None)("Загрузка authlib-injector")
        streamed = self._get(url, stream=True)
        if streamed.status_code != 200:
            raise ElyByError("Компонент authlib-injector недоступен.")
        temporary = None
        try:
            declared = int(streamed.headers.get("Content-Length", "0"))
            if declared > MAX_AGENT_SIZE:
                raise ElyByError("Слишком большой компонент authlib-injector.")
            callbacks.get("setMax", lambda *_: None)(declared or MAX_AGENT_SIZE)
            with tempfile.NamedTemporaryFile(dir=self.directory, suffix=".part", delete=False) as handle:
                temporary = Path(handle.name)
                total = 0
                for chunk in streamed.iter_content(chunk_size=65536):
                    total += len(chunk)
                    if total > MAX_AGENT_SIZE:
                        raise ElyByError("Превышен максимальный размер authlib-injector.")
                    handle.write(chunk)
                    callbacks.get("setProgress", lambda *_: None)(total)
            self._verify_jar(temporary, digest)
            temporary.replace(destination)
            # Persist only public integrity metadata, never tokens.
            destination.with_suffix(".sha256").write_text(digest, encoding="ascii")
            return destination
        except (requests.RequestException, ValueError):
            raise ElyByError("Загрузка authlib-injector прервана. Повторите попытку.") from None
        finally:
            streamed.close()
            if temporary:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def arguments_for_verified(path: Path) -> list[str]:
        path = path.expanduser().resolve()
        try:
            digest = path.with_suffix(".sha256").read_text(encoding="ascii").strip()
        except OSError:
            raise ElyByError("Отсутствует контрольная сумма authlib-injector. Повторите загрузку.") from None
        ElyByService._verify_jar(path, digest)
        return [f"-javaagent:{path}={ELY_AUTH_ROOT}"]

    def jvm_arguments(self, auth_mode: str, enabled: bool, explicit_consent: bool = False,
                      callback: dict | None = None) -> list[str]:
        if not enabled:
            return []
        if auth_mode not in {"offline", "demo"}:
            raise ElyByError("Ely.by доступен для локального профиля. Microsoft-сеанс использует стандартные скины Minecraft.")
        return self.arguments_for_verified(self.prepare(callback=callback))
