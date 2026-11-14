"""Official Modrinth v2 catalogue, checked downloads and dependency installation."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Callable
from urllib.parse import urlparse

import requests
from . import __version__

from .mods import MAX_JAR_SIZE, ModError, ModInfo, ModManager, file_hash, inspect_jar, matches_version, safe_filename

API_URL = "https://api.modrinth.com/v2"
TIMEOUT = (10, 40)
USER_AGENT = f"nullxdead/ConsoleLauncher/{__version__} (https://github.com/nullxdead/ConsoleLauncher)"


class ModrinthError(ModError):
    pass


def _id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
        raise ModrinthError("Некорректный ID или slug Modrinth.")
    return value


class ModrinthClient:
    def __init__(self, session: requests.Session | None = None, timeout: int = 40, max_download_mb: int = 512):
        self.http = session or requests.Session()
        self.http.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        self.timeout = (10, max(5, min(timeout, 180)))
        self.max_download_size = min(MAX_JAR_SIZE, max(1, max_download_mb) * 1024 * 1024)

    def _request(self, method: str, route: str, *, missing_ok: bool = False, **kwargs):
        try:
            response = self.http.request(method, API_URL + route, timeout=self.timeout, allow_redirects=False, **kwargs)
            if response.status_code == 404 and missing_ok:
                return None
            if response.status_code == 429:
                retry = response.headers.get("Retry-After", response.headers.get("X-Ratelimit-Reset", ""))
                delay = f" через {retry} секунд" if str(retry).isdigit() else " позже"
                raise ModrinthError("Превышен лимит Modrinth API. Повторите" + delay + ".")
            if response.status_code == 404:
                raise ModrinthError("Проект или версия не найдены в Modrinth.")
            if response.status_code != 200:
                raise ModrinthError(f"Modrinth API вернул HTTP {response.status_code}.")
            if len(response.content) > 20 * 1024 * 1024:
                raise ModrinthError("Ответ Modrinth превышает допустимый размер.")
            return response.json()
        except requests.exceptions.JSONDecodeError:
            raise ModrinthError("Modrinth вернул повреждённый JSON.") from None
        except requests.RequestException:
            raise ModrinthError("Не удалось связаться с Modrinth. Проверьте сеть и повторите запрос.") from None
        except (ValueError, TypeError):
            raise ModrinthError("Modrinth вернул повреждённый JSON.") from None

    def search(self, query: str, minecraft_version: str = "", loader: str = "", limit: int = 20, project_type: str = "mod", **aliases) -> list[dict]:
        minecraft_version = aliases.get("mc", minecraft_version)
        if project_type not in {"mod", "modpack", "resourcepack", "shader"}:
            raise ModrinthError("Неизвестный тип дополнения.")
        facets = [["project_type:" + project_type]]
        if minecraft_version:
            facets.append(["versions:" + minecraft_version])
        if loader:
            facets.append(["categories:" + loader])
        data = self._request("GET", "/search", params={"query": query[:300], "facets": json.dumps(facets), "limit": min(max(limit, 1), 100)})
        if not isinstance(data, dict) or not isinstance(data.get("hits"), list):
            raise ModrinthError("Некорректный результат поиска Modrinth.")
        return data["hits"]

    def project(self, project: str) -> dict:
        data = self._request("GET", "/project/" + _id(project))
        if not isinstance(data, dict):
            raise ModrinthError("Некорректное описание проекта.")
        if data.get("project_type") not in {None, "mod"}:
            raise ModrinthError("Этот проект не является Minecraft-модом.")
        if data.get("team"):
            members = self._request("GET", "/team/" + _id(data["team"]) + "/members")
            data["authors"] = [m.get("user", {}).get("username", "unknown") for m in members if isinstance(m, dict)]
        else:
            data["authors"] = []
        return data

    def versions(self, project: str, minecraft_version: str = "", loader: str = "", **aliases) -> list[dict]:
        minecraft_version = aliases.get("mc", minecraft_version)
        params = {"include_changelog": "false"}
        if minecraft_version:
            params["game_versions"] = json.dumps([minecraft_version])
        if loader:
            params["loaders"] = json.dumps([loader])
        result = self._request("GET", "/project/" + _id(project) + "/version", params=params)
        if not isinstance(result, list):
            raise ModrinthError("Некорректный список версий Modrinth.")
        return result

    def version(self, version_id: str) -> dict:
        result = self._request("GET", "/version/" + _id(version_id))
        if not isinstance(result, dict):
            raise ModrinthError("Некорректная версия Modrinth.")
        return result

    @staticmethod
    def _compatible(version: dict, minecraft_version: str, loader: str) -> None:
        if minecraft_version not in version.get("game_versions", []) or loader not in version.get("loaders", []):
            raise ModrinthError(f"Версия мода несовместима с Minecraft {minecraft_version} / {loader}.")

    @staticmethod
    def _file(version: dict) -> dict:
        files = [item for item in version.get("files", []) if isinstance(item, dict) and str(item.get("filename", "")).endswith(".jar")]
        if not files:
            raise ModrinthError("Версия Modrinth не содержит JAR-файла.")
        return next((item for item in files if item.get("primary")), files[0])

    def _download(self, item: dict, directory: Path, callback: dict | Callable | None = None, *, inspect_mod: bool = True) -> Path:
        filename = safe_filename(item.get("filename", ""))
        url = item.get("url", "")
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != "cdn.modrinth.com" or parsed.username or parsed.password or parsed.port not in {None, 443}:
            raise ModrinthError("Загрузка разрешена только с официального HTTPS CDN Modrinth.")
        size = item.get("size")
        if type(size) is not int or not 0 < size <= self.max_download_size:
            raise ModrinthError("Размер мода отсутствует или превышает лимит загрузки.")
        hashes = item.get("hashes", {})
        algorithm = "sha512" if hashes.get("sha512") else "sha1"
        expected = hashes.get(algorithm, "")
        if not isinstance(expected, str) or not re.fullmatch(r"[a-fA-F0-9]{" + str(128 if algorithm == "sha512" else 40) + "}", expected):
            raise ModrinthError("В Modrinth отсутствует корректная контрольная сумма файла.")
        target = directory / filename

        def progress(name: str, value) -> None:
            if isinstance(callback, dict) and callback.get(name):
                callback[name](value)
            elif callable(callback) and name == "setProgress":
                callback(value, size)

        progress("setStatus", "Загрузка " + filename)
        progress("setMax", size)
        response = None
        try:
            response = self.http.get(url, stream=True, timeout=self.timeout, allow_redirects=False)
            if response.status_code != 200:
                raise ModrinthError(f"CDN Modrinth вернул HTTP {response.status_code}.")
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) != size:
                raise ModrinthError("Размер ответа CDN не совпадает с метаданными.")
            digest = hashlib.new(algorithm)
            downloaded = 0
            with target.open("xb") as handle:
                for chunk in response.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    downloaded += len(chunk)
                    if downloaded > size:
                        raise ModrinthError("Загрузка превысила заявленный размер.")
                    digest.update(chunk)
                    handle.write(chunk)
                    progress("setProgress", downloaded)
                handle.flush()
                os.fsync(handle.fileno())
            if downloaded != size or digest.hexdigest().lower() != expected.lower():
                raise ModrinthError("Контрольная сумма или размер мода не совпадают; файл отклонён.")
            if inspect_mod:
                inspect_jar(target)
            return target
        except requests.RequestException:
            raise ModrinthError("Ошибка загрузки мода. Профиль не изменён.") from None
        except ValueError:
            raise ModrinthError("CDN вернул некорректный размер файла.") from None
        finally:
            if response is not None:
                response.close()

    def _plan(self, project: str, minecraft_version: str, loader: str, version_id: str | None) -> list[dict]:
        result, selected, visiting = [], {}, set()

        def resolve(project_id: str | None = None, explicit: str | None = None) -> None:
            if len(selected) > 64:
                raise ModrinthError("Слишком много обязательных зависимостей (лимит 64).")
            if explicit:
                version = self.version(explicit)
            else:
                versions = self.versions(project_id, minecraft_version, loader)
                if not versions:
                    raise ModrinthError("Не найдена совместимая версия мода или зависимости: " + str(project_id))
                version = next((v for v in versions if v.get("version_type") == "release"), versions[0])
            self._compatible(version, minecraft_version, loader)
            pid, vid = _id(version.get("project_id", "")), _id(version.get("id", ""))
            if project_id and project_id == project and version_id and project_id != pid:
                # Slugs may differ from IDs, so validate explicit root via project API below.
                expected = self.project(project)["id"]
                if pid != expected:
                    raise ModrinthError("Выбранная версия принадлежит другому проекту.")
            if pid in selected:
                if selected[pid] != vid:
                    raise ModrinthError("Обязательные зависимости требуют конфликтующие версии проекта " + pid)
                return
            if vid in visiting:
                return
            visiting.add(vid)
            selected[pid] = vid
            for dep in version.get("dependencies", []):
                if not isinstance(dep, dict) or dep.get("dependency_type") != "required":
                    continue
                if not dep.get("version_id") and not dep.get("project_id"):
                    raise ModrinthError("Обязательная внешняя зависимость не поддерживается: " + str(dep.get("file_name", "unknown")))
                resolve(dep.get("project_id"), dep.get("version_id"))
            visiting.remove(vid)
            result.append(version)

        resolve(project, version_id)
        selected_pids = set(selected)
        for version in result:
            for dependency in version.get("dependencies", []):
                conflicts = (dependency.get("version_id") in selected.values() if dependency.get("version_id")
                             else dependency.get("project_id") in selected_pids)
                if dependency.get("dependency_type") == "incompatible" and conflicts:
                    raise ModrinthError("Конфликт несовместимых зависимостей Modrinth.")
        return result

    def _check_installed_conflicts(self, plan: list[dict], manager: ModManager) -> None:
        incompatible = [dependency for version in plan for dependency in version.get("dependencies", [])
                        if isinstance(dependency, dict) and dependency.get("dependency_type") == "incompatible"
                        and (dependency.get("project_id") or dependency.get("version_id"))]
        if not incompatible:
            return
        for mod in manager.list_mods():
            if not mod.enabled or not mod.sha512:
                continue
            # A local filename/mod ID is not a Modrinth project ID. Resolve the
            # actual file identity through the documented SHA-512 lookup.
            existing = self._request("GET", "/version_file/" + mod.sha512, missing_ok=True,
                                     params={"algorithm": "sha512"})
            if existing is None:  # A legitimate local mod need not be indexed.
                continue
            if not isinstance(existing, dict) or not isinstance(existing.get("id"), str) or not isinstance(existing.get("project_id"), str):
                raise ModrinthError("Modrinth вернул некорректные данные установленного мода.")
            for dependency in incompatible:
                conflicts = (existing["id"] == dependency["version_id"] if dependency.get("version_id")
                             else existing["project_id"] == dependency.get("project_id"))
                if conflicts:
                    raise ModrinthError("Modrinth объявляет несовместимость с установленным модом " + mod.filename + ". Отключите его перед установкой.")

    def install(self, project: str, game_directory: Path | str, minecraft_version: str, loader: str,
                version_id: str | None = None, callback=None, loader_version: str = "") -> list[ModInfo]:
        if loader not in {"fabric", "forge", "neoforge", "quilt"}:
            raise ModrinthError("Для установки модов выберите профиль с Fabric/Forge/NeoForge/Quilt.")
        manager = ModManager(game_directory)
        plan = self._plan(_id(project), minecraft_version, loader, _id(version_id) if version_id else None)
        self._check_installed_conflicts(plan, manager)
        # Stage and inspect every component before mutating the user's mods folder.
        with tempfile.TemporaryDirectory(prefix="consolecraft-modrinth-") as temporary:
            staged = []
            filenames = set()
            for version in plan:
                item = self._file(version)
                if item.get("filename") in filenames:
                    raise ModrinthError("Разные проекты используют одинаковое имя JAR-файла.")
                filenames.add(item.get("filename"))
                path = self._download(item, Path(temporary), callback)
                info = inspect_jar(path)
                self._check_loader_requirement(info, loader, loader_version)
                same_mod = None
                for existing in manager.list_mods():
                    if (existing.filename == path.name or set(existing.mod_ids) & set(info.mod_ids)) and existing.sha512 != info.sha512:
                        raise ModrinthError("Уже установлена другая версия мода: " + existing.filename + ". Используйте подтверждённое обновление.")
                    if existing.sha512 == info.sha512:
                        same_mod = existing
                staged.append((path, same_mod))
            created, installed = [], []
            try:
                for path, same_mod in staged:
                    if same_mod is not None:
                        installed.append(same_mod)
                        continue
                    already_exists = (manager.directory / path.name).exists()
                    installed.append(manager.install_local(path, allow_incompatible=True))
                    if not already_exists:
                        created.append(manager.directory / path.name)
            except (OSError, ModError):
                for path in created:
                    path.unlink(missing_ok=True)
                raise
        return installed

    @staticmethod
    def _check_loader_requirement(info: ModInfo, loader: str, loader_version: str) -> None:
        """Catalogue Minecraft filters cannot establish the installed loader version."""
        if not loader_version:
            return  # Preserve catalogue-based behavior for existing callers.
        dependency = {"fabric": "fabricloader", "quilt": "quilt_loader", "forge": "forge", "neoforge": "neoforge"}.get(loader)
        constraint = info.dependencies.get(dependency)
        if constraint is None:
            return
        compatible = matches_version(loader_version, constraint)
        if compatible is False:
            raise ModrinthError(f"Моду {info.name or info.filename} нужен {loader} {constraint}; выбран {loader_version}.")
        if compatible is None:
            raise ModrinthError(f"Не удалось проверить ограничение {loader} {constraint} для мода {info.name or info.filename}.")

    def check_updates(self, game_directory: Path | str, minecraft_version: str, loader: str) -> list[dict]:
        result = []
        for mod in ModManager(game_directory).list_mods():
            if mod.error or not mod.sha512:
                continue
            update = self._request("POST", "/version_file/" + mod.sha512 + "/update", missing_ok=True,
                                   params={"algorithm": "sha512"}, json={"loaders": [loader], "game_versions": [minecraft_version], "version_types": ["release"]})
            if update is None:
                continue
            if not isinstance(update, dict):
                raise ModrinthError("Некорректный ответ проверки обновлений.")
            self._compatible(update, minecraft_version, loader)
            item = self._file(update)
            if item.get("hashes", {}).get("sha512", "").lower() != mod.sha512:
                result.append({"filename": mod.filename, "current_version": mod.version, "version": update,
                               "version_id": update.get("id"), "name": mod.name, "project_id": update.get("project_id")})
        return result

    def update(self, game_directory: Path | str, minecraft_version: str, loader: str,
               filename: str | None = None, confirmed: bool = False, callback=None, loader_version: str = "") -> list[ModInfo]:
        if not confirmed:
            raise ModrinthError("Обновление заменяет пользовательские моды и требует подтверждения.")
        manager = ModManager(game_directory)
        updates = [item for item in self.check_updates(game_directory, minecraft_version, loader) if filename is None or item["filename"] == filename]
        installed = []
        for update in updates:
            old = manager._path(update["filename"])
            was_disabled = old.name.endswith(".disabled")
            # Preserve the original in a rollback location outside mods; never lose it on error.
            with tempfile.TemporaryDirectory(prefix="consolecraft-update-") as temporary:
                backup = Path(temporary) / old.name
                import shutil
                shutil.copy2(old, backup)
                old.unlink()
                try:
                    mods = self.install(update["project_id"], game_directory, minecraft_version, loader, update["version_id"], callback,
                                        loader_version=loader_version)
                    if was_disabled and mods:
                        mods[-1] = manager.set_enabled(mods[-1].filename, False)
                    installed.extend(mods)
                except BaseException:
                    if not old.exists():
                        shutil.copy2(backup, old)
                    raise
        return installed
