"""Verified installations through minecraft-launcher-lib's documented loader API."""
from __future__ import annotations

import hashlib
import json
import os
from uuid import uuid4
from pathlib import Path
from typing import Callable

from minecraft_launcher_lib import command, mod_loader
from .java import ensure_java
from .minecraft import MinecraftService, MinecraftError, library_limits, validate_version_id

LOADERS = ("fabric", "forge", "neoforge", "quilt")


class LoaderError(MinecraftError):
    pass


class LoaderService:
    def __init__(self, minecraft_directory: Path | str, timeout: int = 30, max_workers: int = 8):
        self.directory = Path(minecraft_directory).expanduser().resolve()
        self.timeout = timeout
        self.max_workers = max_workers
        self.minecraft = MinecraftService(self.directory, timeout=timeout, max_workers=max_workers)
        self._invalid_paths: list[Path] = []

    def _loader(self, loader: str):
        if loader not in LOADERS:
            raise LoaderError("Выберите Fabric, Forge, NeoForge или Quilt.")
        return mod_loader.get_mod_loader(loader)

    def minecraft_versions(self, loader: str, stable_only: bool = True) -> list[str]:
        instance = self._loader(loader)
        try:
            with library_limits(self.timeout, self.max_workers):
                result = instance.get_minecraft_versions(stable_only)
            return [validate_version_id(item) for item in result]
        except Exception as exc:
            raise LoaderError(f"Не удалось получить версии Minecraft для {loader}. Проверьте сеть и доступность сервиса.") from exc

    def loader_versions(self, loader: str, mc_version: str, stable_only: bool = False) -> list[str]:
        instance = self._loader(loader)
        validate_version_id(mc_version)
        try:
            with library_limits(self.timeout, self.max_workers):
                result = instance.get_loader_versions(mc_version, stable_only)
            return [validate_version_id(item) for item in result]
        except Exception as exc:
            raise LoaderError(f"Не удалось получить версии {loader} для Minecraft {mc_version}.") from exc

    def install(self, loader: str, mc_version: str, loader_version: str | None = None,
                java_path: str | Path | None = None, callback: dict[str, Callable] | None = None) -> str:
        instance = self._loader(loader)
        mc_version = self.minecraft.resolve_version(mc_version)
        versions = self.loader_versions(loader, mc_version)
        if not versions:
            raise LoaderError(f"{loader} не поддерживает Minecraft {mc_version}.")
        selected = loader_version or versions[0]
        if selected not in versions:
            raise LoaderError(f"Версия {selected} не совместима с {loader} / Minecraft {mc_version}.")
        self.minecraft.install(mc_version, callback=callback, java_path=java_path)
        java = ensure_java(self.directory, self.minecraft.metadata(mc_version), configured=java_path)
        expected = validate_version_id(instance.get_installed_version(mc_version, selected))
        try:
            with library_limits(self.timeout, self.max_workers):
                installed = instance.install(mc_version, self.directory, loader_version=selected,
                                             java=java.path, callback=callback or {})
        except Exception as exc:
            raise LoaderError(f"Установка {loader} не завершена. Проверьте Java, сеть и журнал установщика; повторите восстановление.") from exc
        if installed != expected:
            raise LoaderError("Установщик вернул неожиданное имя игрового профиля.")
        problems = self.verify(installed)
        if problems:
            raise LoaderError("Установка не прошла проверку: " + "; ".join(problems[:5]))
        return installed

    def _inside(self, relative: str) -> Path:
        path = (self.directory / relative).resolve()
        if not path.is_relative_to(self.directory) or path == self.directory:
            raise LoaderError("Описание установки содержит небезопасный путь.")
        return path

    def verify(self, version_id: str) -> list[str]:
        """Check client JAR, active platform classpath, metadata and declared hashes.

        Installer-created libraries without published hashes are checked for presence
        and nonzero length. Hashes for Mojang assets are checked from their index.
        """
        metadata = self.minecraft.metadata(version_id)
        self._invalid_paths = []
        problems: list[str] = []
        checks: dict[Path, str | None] = {}
        options = {"username": "Player", "uuid": "0" * 32, "token": "0", "executablePath": "java"}
        try:
            args = command.get_minecraft_command(version_id, self.directory, options)
            classpath = args[args.index("-cp") + 1]
            for item in classpath.split(os.pathsep):
                if item:
                    path = Path(item).resolve()
                    if not path.is_relative_to(self.directory):
                        raise LoaderError("Classpath выходит за каталог установки.")
                    checks[path] = None
        except (ValueError, KeyError, OSError) as exc:
            raise LoaderError("Не удалось проверить classpath установки.") from exc
        effective_jar = validate_version_id(str(metadata.get("jar", version_id)))
        effective_client = self._inside(f"versions/{effective_jar}/{effective_jar}.jar")
        client_digest = metadata.get("downloads", {}).get("client", {}).get("sha1")
        if effective_client in checks and client_digest:
            checks[effective_client] = client_digest
        visited = set()
        def read_chain(current):
            validate_version_id(current)
            if current in visited:
                raise LoaderError("Циклическое наследование установки.")
            visited.add(current)
            path = self._inside(f"versions/{current}/{current}.json")
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("inheritsFrom"):
                read_chain(data["inheritsFrom"])
            client = data.get("downloads", {}).get("client", {})
            client_path = self._inside(f"versions/{current}/{current}.jar")
            if client:
                checks[client_path] = client.get("sha1")
            for library in data.get("libraries", []):
                downloads = library.get("downloads", {})
                for artifact in [downloads.get("artifact", {}), *downloads.get("classifiers", {}).values()]:
                    if artifact.get("path"):
                        artifact_path = self._inside("libraries/" + artifact["path"])
                        if artifact_path in checks:
                            checks[artifact_path] = artifact.get("sha1")
        try:
            read_chain(version_id)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            raise LoaderError("Повреждены метаданные установленного модлоадера.") from exc
        index = metadata.get("assetIndex", {})
        if index.get("id"):
            validate_version_id(index["id"])
            index_path = self._inside(f"assets/indexes/{index['id']}.json")
            checks[index_path] = index.get("sha1")
            if index_path.is_file():
                try:
                    assets = json.loads(index_path.read_text(encoding="utf-8"))["objects"]
                    for asset in assets.values():
                        digest = asset["hash"]
                        if not isinstance(digest, str) or len(digest) != 40 or any(c not in "0123456789abcdef" for c in digest):
                            raise ValueError("Invalid asset hash")
                        checks[self._inside(f"assets/objects/{digest[:2]}/{digest}")] = digest
                except (ValueError, KeyError, TypeError) as exc:
                    raise LoaderError("Повреждён индекс ресурсов игры.") from exc
        for path, digest in checks.items():
            label = str(path.relative_to(self.directory))
            if not path.is_file() or path.stat().st_size == 0:
                problems.append("Отсутствует: " + label)
                self._invalid_paths.append(path)
            elif digest:
                with path.open("rb") as handle:
                    actual = hashlib.file_digest(handle, "sha1").hexdigest()
                if actual != digest:
                    problems.append("Повреждён: " + label)
                    self._invalid_paths.append(path)
        return problems

    def repair(self, loader: str, mc_version: str, loader_version: str | None = None,
               java_path: str | Path | None = None, callback: dict | None = None) -> str:
        """Quarantine broken managed artifacts, then rerun the official installer."""
        instance = self._loader(loader)
        resolved = self.minecraft.resolve_version(mc_version)
        selected = loader_version
        if not selected:
            versions = self.loader_versions(loader, resolved)
            if not versions:
                raise LoaderError("Нет совместимых версий модлоадера.")
            selected = versions[0]
        installed = validate_version_id(instance.get_installed_version(resolved, selected))
        profile_json = self._inside(f"versions/{installed}/{installed}.json")
        quarantine = self.directory / ".consolecraft-quarantine" / uuid4().hex
        base_json = self._inside(f"versions/{resolved}/{resolved}.json")
        if base_json.is_file():
            try:
                self.minecraft.metadata(resolved)
            except MinecraftError:
                target = quarantine / base_json.relative_to(self.directory)
                target.parent.mkdir(parents=True, exist_ok=True)
                base_json.replace(target)
        if profile_json.exists():
            try:
                self.verify(installed)
                broken = list(self._invalid_paths)
            except (MinecraftError, ValueError):
                broken = [profile_json]
            for path in broken:
                if path.is_file():
                    target = quarantine / path.relative_to(self.directory)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    path.replace(target)
        return self.install(loader, resolved, selected, java_path=java_path, callback=callback)
