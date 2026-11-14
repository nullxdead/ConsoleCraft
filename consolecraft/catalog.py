"""Install Modrinth resource packs, shaders and isolated .mrpack profiles."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
from urllib.parse import urljoin, urlparse
import zipfile

import requests

from .cancellation import check_cancelled
from .modrinth import ModrinthError, _id

KINDS = {"mod": "Моды", "modpack": "Модпаки", "shader": "Шейдеры", "resourcepack": "Ресурс-паки"}
HOSTS = {"cdn.modrinth.com", "github.com", "raw.githubusercontent.com", "gitlab.com",
         "release-assets.githubusercontent.com", "objects.githubusercontent.com"}
MAX_FILE = 512 * 1024 * 1024
MAX_TOTAL = 4 * 1024 * 1024 * 1024
MAX_FILES = 4096


def _safe_path(value: str) -> Path:
    if not isinstance(value, str) or not value or "\\" in value or value.startswith("/"):
        raise ModrinthError("В модпаке указан небезопасный путь.")
    pieces = value.split("/")
    if any(not p or p in {".", ".."} or p[-1] in ". " or any(ord(c) < 32 or c in ':*?"<>|' for c in p)
           or re.match(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", p, re.I) for p in pieces):
        raise ModrinthError("В модпаке указан небезопасный путь.")
    return Path(*PurePosixPath(value).parts)


def _chosen_version(client, project: str, kind: str, minecraft: str = "", loader: str = ""):
    data = client._request("GET", "/project/" + _id(project))
    if not isinstance(data, dict) or data.get("project_type") != kind:
        raise ModrinthError("Тип проекта не совпадает с выбранным разделом.")
    versions = client.versions(project, minecraft, loader)
    if not versions:
        raise ModrinthError("Для этого профиля не найдена подходящая версия.")
    return data, next((v for v in versions if v.get("version_type") == "release"), versions[0])


def _archive_file(version: dict, extension: str) -> dict:
    files = [f for f in version.get("files", []) if isinstance(f, dict)
             and str(f.get("filename", "")).lower().endswith(extension)]
    if not files:
        raise ModrinthError("У проекта нет подходящего файла " + extension)
    return next((f for f in files if f.get("primary")), files[0])


def install_pack(client, kind: str, project: str, profile, callback=None) -> Path:
    """Install a shader/resource ZIP without replacing existing user files."""
    if kind not in {"shader", "resourcepack"}:
        raise ModrinthError("Неизвестный тип ресурс-пака или шейдера.")
    minecraft = "" if profile.minecraft_version in {"latest", "release", "snapshot", "latest-snapshot"} else profile.minecraft_version
    _, version = _chosen_version(client, project, kind, minecraft)
    with tempfile.TemporaryDirectory(prefix="consolelauncher-addon-") as temporary:
        path = client._download(_archive_file(version, ".zip"), Path(temporary), callback, inspect_mod=False)
        if not zipfile.is_zipfile(path):
            raise ModrinthError("Скачанный файл не является ZIP-архивом.")
        directory = Path(profile.game_directory) / ("shaderpacks" if kind == "shader" else "resourcepacks")
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / path.name
        created = False
        try:
            with target.open("xb") as output:
                created = True
                with path.open("rb") as source:
                    while chunk := source.read(65536):
                        check_cancelled()
                        output.write(chunk)
        except FileExistsError:
            raise ModrinthError("Файл уже существует: " + target.name) from None
        except BaseException:
            if created:
                target.unlink(missing_ok=True)
            raise
        return target


def _download_index_file(client, entry: dict, target: Path, callback=None):
    size = entry.get("fileSize")
    hashes = entry.get("hashes")
    digest_expected = hashes.get("sha512", "") if isinstance(hashes, dict) else ""
    urls = entry.get("downloads")
    if type(size) is not int or not 0 < size <= MAX_FILE or not re.fullmatch(r"[a-fA-F0-9]{128}", str(digest_expected)) or not isinstance(urls, list) or not urls:
        raise ModrinthError("В модпаке отсутствуют корректные размер, хеш или адрес файла.")
    if callback:
        callback["setStatus"]("Загрузка " + target.name)
        callback["setMax"](size)
    last_error = None
    for address in urls:
        response = None
        try:
            url = address
            for _ in range(6):
                check_cancelled()
                parsed = urlparse(url)
                if parsed.scheme != "https" or parsed.hostname not in HOSTS or parsed.username or parsed.password or parsed.port not in {None, 443}:
                    raise ModrinthError("Модпак содержит недопустимый адрес загрузки.")
                response = client.http.get(url, stream=True, timeout=client.timeout, allow_redirects=False)
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    response.close()
                    response = None
                    if not location:
                        raise ModrinthError("Некорректное перенаправление загрузки.")
                    url = urljoin(url, location)
                    continue
                if response.status_code != 200:
                    raise ModrinthError(f"Ошибка загрузки файла модпака: HTTP {response.status_code}.")
                break
            else:
                raise ModrinthError("Слишком много перенаправлений файла модпака.")
            target.parent.mkdir(parents=True, exist_ok=True)
            digest, downloaded = hashlib.sha512(), 0
            with target.open("wb") as output:
                for chunk in response.iter_content(chunk_size=65536):
                    check_cancelled()
                    downloaded += len(chunk)
                    if downloaded > size:
                        raise ModrinthError("Файл модпака превышает заявленный размер.")
                    digest.update(chunk)
                    output.write(chunk)
                    if callback:
                        callback["setProgress"](downloaded)
            if downloaded != size or digest.hexdigest() != digest_expected.lower():
                raise ModrinthError("Размер или контрольная сумма файла модпака не совпадает.")
            return
        except (requests.RequestException, ModrinthError, ValueError) as exc:
            last_error = exc
            target.unlink(missing_ok=True)
        finally:
            if response is not None:
                response.close()
    raise ModrinthError("Не удалось скачать файл модпака: " + target.name) from last_error


def install_modpack(client, project: str, profiles, callback=None):
    """Stage the complete pack, then create a new, isolated game profile."""
    data, version = _chosen_version(client, project, "modpack")
    with tempfile.TemporaryDirectory(prefix="consolelauncher-modpack-") as temporary:
        root = Path(temporary)
        archive_path = client._download(_archive_file(version, ".mrpack"), root, callback, inspect_mod=False)
        stage = root / "game"
        stage.mkdir()
        try:
            with zipfile.ZipFile(archive_path) as archive:
                infos = archive.infolist()
                if len(infos) > MAX_FILES or sum(i.file_size for i in infos) > MAX_TOTAL:
                    raise ModrinthError("Модпак слишком большой.")
                index_info = archive.getinfo("modrinth.index.json")
                if index_info.file_size > 2 * 1024 * 1024:
                    raise ModrinthError("Описание модпака слишком большое.")
                index = json.loads(archive.read(index_info))
                if not isinstance(index, dict) or index.get("formatVersion") != 1 or index.get("game") != "minecraft":
                    raise ModrinthError("Неподдерживаемый формат модпака.")
                deps, entries = index.get("dependencies"), index.get("files")
                if not isinstance(deps, dict) or not isinstance(entries, list) or len(entries) > MAX_FILES:
                    raise ModrinthError("Некорректное описание модпака.")
                loaders = {"fabric-loader": "fabric", "quilt-loader": "quilt", "forge": "forge", "neoforge": "neoforge"}
                selected_loaders = [(loaders[k], v) for k, v in deps.items() if k in loaders]
                if len(selected_loaders) > 1 or set(deps) - {"minecraft", *loaders}:
                    raise ModrinthError("Модпак использует неподдерживаемые зависимости.")
                minecraft = deps.get("minecraft")
                if not isinstance(minecraft, str) or not minecraft:
                    raise ModrinthError("Модпак не указал версию Minecraft.")
                loader, loader_version = selected_loaders[0] if selected_loaders else ("vanilla", "")
                if not isinstance(loader_version, str):
                    raise ModrinthError("Некорректная версия загрузчика модпака.")
                seen, total = set(), 0
                for entry in entries:
                    if not isinstance(entry, dict):
                        raise ModrinthError("Некорректный файл в модпаке.")
                    env = entry.get("env", {})
                    if not isinstance(env, dict) or env.get("client", "required") not in {"required", "optional", "unsupported"}:
                        raise ModrinthError("Некорректные условия файла модпака.")
                    if env.get("client", "required") != "required":
                        continue
                    relative = _safe_path(entry.get("path"))
                    key = relative.as_posix().casefold()
                    if key in seen:
                        raise ModrinthError("Повторяющийся файл модпака.")
                    seen.add(key)
                    size = entry.get("fileSize")
                    if type(size) is not int or size < 0:
                        raise ModrinthError("Некорректный размер файла модпака.")
                    total += size
                    if total > MAX_TOTAL:
                        raise ModrinthError("Файлы модпака превышают допустимый размер.")
                    _download_index_file(client, entry, stage / relative, callback)
                for prefix in ("overrides/", "client-overrides/"):
                    override_seen = set()
                    for info in infos:
                        check_cancelled()
                        if not info.filename.startswith(prefix) or info.is_dir():
                            continue
                        relative = _safe_path(info.filename[len(prefix):])
                        key = relative.as_posix().casefold()
                        if key in override_seen or stat.S_ISLNK(info.external_attr >> 16) or info.file_size > MAX_FILE:
                            raise ModrinthError("Недопустимый файл в архиве модпака.")
                        override_seen.add(key)
                        target = stage / relative
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with archive.open(info) as source, target.open("wb") as output:
                            while chunk := source.read(65536):
                                check_cancelled()
                                output.write(chunk)
        except (zipfile.BadZipFile, KeyError, json.JSONDecodeError, UnicodeError):
            raise ModrinthError("Модпак повреждён или не содержит правильное описание.") from None
        name = str(data.get("title") or index.get("name") or project).strip()[:70] or "Модпак"
        existing = {p.name.casefold() for p in profiles.list()}
        candidate, suffix = name, 2
        while candidate.casefold() in existing:
            candidate, suffix = f"{name} ({suffix})", suffix + 1
        profile = profiles.create(candidate, minecraft_version=minecraft, loader=loader, loader_version=loader_version)
        try:
            shutil.copytree(stage, profile.game_directory, dirs_exist_ok=True)
            check_cancelled()
            profiles.set_default(profile.id)
        except BaseException:
            profiles.delete(profile.id, confirmed=True, delete_worlds=True, confirm_worlds=True)
            raise
        return profile
