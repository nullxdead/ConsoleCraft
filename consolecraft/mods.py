"""Bounded, non-executing JAR inspection and per-game-directory mod management."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
import tomllib
import zipfile

MAX_JAR_SIZE = 512 * 1024 * 1024
MAX_METADATA_SIZE = 2 * 1024 * 1024
MAX_ARCHIVE_SIZE = 2 * 1024 * 1024 * 1024
MAX_ENTRIES = 100000
LOADERS = {"fabric", "forge", "neoforge", "quilt"}


class ModError(RuntimeError):
    """A safe and actionable mod management error."""


def safe_filename(name: str) -> str:
    if (not isinstance(name, str) or not name or len(name) > 220 or name in {".", ".."}
            or name[-1] in ". " or any(ord(c) < 32 or c in '/\\:*?"<>|' for c in name)
            or re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", name, re.I)):
        raise ModError("Недопустимое имя файла мода.")
    return name


def file_hash(path: Path, algorithm: str = "sha512") -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class ModInfo:
    filename: str
    name: str = ""
    version: str = "unknown"
    mod_ids: list[str] = field(default_factory=list)
    loaders: list[str] = field(default_factory=list)
    dependencies: dict = field(default_factory=dict)
    conflicts: dict = field(default_factory=dict)
    minecraft: object = "*"
    enabled: bool = True
    sha512: str = ""
    description: str = ""
    authors: list[str] = field(default_factory=list)
    error: str = ""
    icon_path: str = ""

    def public_dict(self) -> dict:
        return asdict(self)


def _text(value: object, limit: int = 4096) -> str:
    return str(value or "")[:limit]


def _icon_path(value: object) -> str:
    if isinstance(value, str):
        return value[:4096]
    if isinstance(value, dict):
        sizes = [(abs(int(size) - 128), -int(size), path) for size, path in value.items()
                 if isinstance(size, str) and size.isdecimal() and len(size) < 6 and int(size) > 0 and isinstance(path, str)]
        if sizes:
            return min(sizes)[2][:4096]
    return ""


def inspect_jar(path: Path | str) -> ModInfo:
    """Inspect JSON/TOML only, never extract files or load Java classes."""
    path = Path(path)
    safe_filename(path.name)
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= MAX_JAR_SIZE:
        raise ModError("Мод должен быть обычным JAR-файлом размером до 512 МБ.")
    info = ModInfo(path.name, path.name, enabled=not path.name.endswith(".disabled"))
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ENTRIES or sum(e.file_size for e in entries) > MAX_ARCHIVE_SIZE:
                raise ModError("Архив мода превышает допустимые ограничения.")
            names = set()
            for entry in entries:
                name = entry.orig_filename  # zipfile normalizes backslashes on Windows.
                parts = PurePosixPath(name).parts
                if (name in names or name.startswith(("/", "\\")) or "\\" in name
                        or ".." in parts or any(":" in part for part in parts)
                        or (entry.external_attr >> 16) & 0o170000 == 0o120000
                        or entry.flag_bits & 1):
                    raise ModError("Архив содержит небезопасные пути, ссылки или шифрование.")
                names.add(name)
                if entry.file_size > 1024 * 1024 and entry.file_size / max(entry.compress_size, 1) > 1000:
                    raise ModError("Подозрительная степень сжатия архива мода.")

            def metadata(name: str, toml: bool = False) -> dict:
                entry = archive.getinfo(name)
                if entry.file_size > MAX_METADATA_SIZE:
                    raise ModError("Метаданные мода превышают 2 МБ.")
                raw = archive.read(entry)
                data = tomllib.loads(raw.decode("utf-8")) if toml else json.loads(raw)
                if not isinstance(data, dict):
                    raise ModError("Метаданные мода должны быть объектом.")
                return data

            if "fabric.mod.json" in names:
                data = metadata("fabric.mod.json")
                info.loaders.append("fabric")
                info.mod_ids = [_text(data.get("id"), 128)] if data.get("id") else []
                info.mod_ids.extend(_text(alias, 128) for alias in data.get("provides", []) if isinstance(alias, str))
                info.name = _text(data.get("name")) or info.name
                info.version = _text(data.get("version")) or "unknown"
                info.dependencies = data.get("depends", {}) if isinstance(data.get("depends", {}), dict) else {}
                info.conflicts = {**data.get("breaks", {}), **data.get("conflicts", {})}
                info.minecraft = info.dependencies.get("minecraft", "*")
                info.description = _text(data.get("description"))
                info.icon_path = _icon_path(data.get("icon")) or info.icon_path
                info.authors = [_text(a.get("name") if isinstance(a, dict) else a) for a in data.get("authors", [])][:100]
            if "quilt.mod.json" in names:
                data = metadata("quilt.mod.json").get("quilt_loader", {})
                info.loaders.append("quilt")
                if data.get("id"):
                    info.mod_ids.append(_text(data["id"], 128))
                info.version = _text(data.get("version")) or info.version
                meta = data.get("metadata", {})
                info.name = _text(meta.get("name")) or info.name
                info.description = _text(meta.get("description")) or info.description
                info.icon_path = _icon_path(meta.get("icon")) or info.icon_path
                if isinstance(meta.get("contributors"), dict):
                    info.authors = list(meta["contributors"])[:100]
                for dependency in data.get("depends", []):
                    if isinstance(dependency, dict) and not dependency.get("optional", False) and dependency.get("id"):
                        info.dependencies[_text(dependency["id"], 128)] = dependency.get("versions", "*")
                for dependency in data.get("breaks", []):
                    if isinstance(dependency, dict) and dependency.get("id"):
                        info.conflicts[_text(dependency["id"], 128)] = dependency.get("versions", "*")
                info.minecraft = info.dependencies.get("minecraft", "*")
            for name, loader in (("META-INF/mods.toml", "forge"), ("META-INF/neoforge.mods.toml", "neoforge")):
                if name not in names:
                    continue
                data = metadata(name, toml=True)
                info.loaders.append(loader)
                for mod in data.get("mods", []):
                    if not isinstance(mod, dict):
                        continue
                    mod_id = _text(mod.get("modId"), 128)
                    if mod_id:
                        info.mod_ids.append(mod_id)
                    info.name = _text(mod.get("displayName")) or info.name
                    info.version = _text(mod.get("version")) or info.version
                    info.description = _text(mod.get("description")) or info.description
                    info.icon_path = _icon_path(mod.get("logoFile", data.get("logoFile"))) or info.icon_path
                    if mod.get("authors"):
                        info.authors = [_text(author.strip()) for author in str(mod["authors"]).split(",")][:100]
                    for dependency in data.get("dependencies", {}).get(mod_id, []):
                        if not isinstance(dependency, dict) or dependency.get("side", "BOTH") == "SERVER":
                            continue
                        dep_id = _text(dependency.get("modId"), 128)
                        constraint = dependency.get("versionRange", "*")
                        if dependency.get("type") == "incompatible":
                            info.conflicts[dep_id] = constraint
                        elif dependency.get("mandatory", dependency.get("type", "required") == "required"):
                            info.dependencies[dep_id] = constraint
                info.minecraft = info.dependencies.get("minecraft", info.minecraft)
            info.mod_ids = list(dict.fromkeys(filter(None, info.mod_ids)))
            info.loaders = list(dict.fromkeys(info.loaders))
            if not info.loaders:
                info.error = "Не распознаны метаданные Fabric/Forge/NeoForge/Quilt; совместимость неизвестна."
    except ModError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError, ValueError, TypeError, KeyError, AttributeError, UnicodeError) as exc:
        raise ModError("JAR повреждён или содержит некорректные метаданные.") from exc
    info.sha512 = file_hash(path)
    return info


def _version(value: str):
    """Fabric extended semantic version: numeric core, optional empty prerelease.

    Build metadata does not affect precedence. Unnormalised Minecraft snapshot IDs
    remain unknown rather than being guessed into a compatible release.
    """
    if not isinstance(value, str) or len(value) > 256:
        return None
    match = re.fullmatch(r"(\d+(?:\.\d+)*)(?:-([-0-9A-Za-z]*(?:\.[-0-9A-Za-z]+)*))?(?:\+[^\s]*)?", value)
    if not match:
        return None
    return tuple(int(p) for p in match[1].split(".")), match[2]


def _compare(a, b):
    width = max(len(a[0]), len(b[0]))
    left, right = a[0] + (0,) * (width-len(a[0])), b[0] + (0,) * (width-len(b[0]))
    if left != right:
        return (left > right) - (left < right)
    if a[1] is None or b[1] is None:
        return (a[1] is None) - (b[1] is None)
    left, right = a[1].split(".") if a[1] else [], b[1].split(".") if b[1] else []
    for first, second in zip(left, right):
        if first == second:
            continue
        numeric_a = first.isdigit() and (first == "0" or not first.startswith("0"))
        numeric_b = second.isdigit() and (second == "0" or not second.startswith("0"))
        if numeric_a and numeric_b:
            return (int(first) > int(second)) - (int(first) < int(second))
        if numeric_a != numeric_b:
            return -1 if numeric_a else 1
        return (first > second) - (first < second)
    return (len(left) > len(right)) - (len(left) < len(right))


def matches_version(version: str, constraint: object) -> bool | None:
    """Fabric extended semver predicates plus existing Quilt/Maven range support.

    Fabric's empty prerelease marker ``26.3-`` is valid and precedes 26.3,
    allowing ``~26.3-`` to cover prereleases and releases with major/minor 26.3.
    Unknown syntax still requires explicit user permission.
    """
    if isinstance(constraint, list):
        results = [matches_version(version, item) for item in constraint]
        return True if True in results else None if None in results else False
    if not isinstance(constraint, str) or len(constraint) > 4096:
        return None
    constraint = constraint.strip()
    if constraint in {"", "*"}:
        return True
    if "||" in constraint:
        return matches_version(version, constraint.split("||"))
    if constraint == version:
        return True
    actual = _version(version)
    if actual is None:
        return None
    maven = re.fullmatch(r"([\[(])([^,]*),([^\])]*)([\])])", constraint)
    if maven:
        lo, hi = _version(maven[2].strip()) if maven[2] else None, _version(maven[3].strip()) if maven[3] else None
        if (maven[2] and lo is None) or (maven[3] and hi is None):
            return None
        return (lo is None or _compare(actual, lo) >= (0 if maven[1] == "[" else 1)) and (hi is None or _compare(actual, hi) <= (0 if maven[4] == "]" else -1))
    if constraint.startswith("[") and constraint.endswith("]"):
        return matches_version(version, constraint[1:-1])
    results = []
    for predicate in constraint.replace(",", " ").split():
        if predicate == "*":
            continue
        wildcard = re.fullmatch(r"=?(\d+(?:\.\d+)*)(?:\.[xX*])+", predicate)
        if wildcard:
            prefix = tuple(int(p) for p in wildcard[1].split("."))
            core = actual[0] + (0,) * max(0, len(prefix)-len(actual[0]))
            results.append(core[:len(prefix)] == prefix)
            continue
        match = re.fullmatch(r"(>=|<=|>|<|=|~|\^)?(.+)", predicate)
        target = _version(match[2]) if match else None
        if target is None:
            return None
        op = match[1] or "="
        comparison = _compare(actual, target)
        a = actual[0] + (0, 0)
        t = target[0] + (0, 0)
        if op == "~":
            results.append(comparison >= 0 and a[:2] == t[:2])
        elif op == "^":
            results.append(comparison >= 0 and a[0] == t[0])
        else:
            results.append({"=": comparison == 0, ">=": comparison >= 0, "<=": comparison <= 0, ">": comparison > 0, "<": comparison < 0}[op])
    return all(results)


class ModManager:
    def __init__(self, game_directory: Path | str):
        self.game_directory = Path(game_directory).expanduser().resolve()
        self.directory = self.game_directory / "mods"
        if self.directory.is_symlink():
            raise ModError("Каталог mods не должен быть символической ссылкой.")

    def _path(self, filename: str) -> Path:
        safe_filename(filename)
        path = self.directory / filename
        if path.is_symlink() or not path.resolve().is_relative_to(self.game_directory):
            raise ModError("Путь мода выходит за пределы игрового профиля.")
        return path

    def list_mods(self, query: str = "") -> list[ModInfo]:
        if not self.directory.exists():
            return []
        result = []
        for path in sorted(self.directory.iterdir()):
            if not path.name.endswith((".jar", ".jar.disabled")):
                continue
            try:
                mod = inspect_jar(self._path(path.name))
            except ModError as exc:
                mod = ModInfo(path.name, path.name, enabled=not path.name.endswith(".disabled"), error=str(exc))
            if query.lower() in (mod.name + " " + mod.filename + " " + " ".join(mod.mod_ids)).lower():
                result.append(mod)
        return result

    def install_local(self, source: Path | str, minecraft_version: str = "", loader: str = "", allow_incompatible: bool = False, loader_version: str = "") -> ModInfo:
        source = Path(source)
        if not source.name.endswith(".jar"):
            raise ModError("Для установки нужен файл с расширением .jar.")
        info = inspect_jar(source)
        issues = compatibility(info, minecraft_version, loader, loader_version)
        if issues and not allow_incompatible:
            raise ModError("; ".join(issues) + " Для неизвестной совместимости используйте явное разрешение.")
        destination = self._path(source.name)
        if destination.exists():
            if file_hash(destination) == info.sha512:
                return inspect_jar(destination)
            raise ModError("Мод с таким именем уже существует. Сначала отключите или удалите старую версию.")
        for existing in self.list_mods():
            if set(existing.mod_ids) & set(info.mod_ids):
                raise ModError("Этот mod ID уже установлен: " + existing.filename)
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.directory, suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                with source.open("rb") as src:
                    shutil.copyfileobj(src, handle)
                handle.flush()
                os.fsync(handle.fileno())
            if file_hash(temporary) != info.sha512:
                raise ModError("Исходный мод изменился во время копирования.")
            os.replace(temporary, destination)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
        return inspect_jar(destination)

    def set_enabled(self, filename: str, enabled: bool) -> ModInfo:
        source = self._path(filename)
        if not source.is_file() or not filename.endswith((".jar", ".jar.disabled")):
            raise ModError("Указанный мод не найден.")
        destination_name = filename.removesuffix(".disabled") if enabled else filename if filename.endswith(".disabled") else filename + ".disabled"
        destination = self._path(destination_name)
        if source != destination:
            if destination.exists():
                raise ModError("Файл назначения уже существует; операция отменена.")
            source.rename(destination)
        try:
            return inspect_jar(destination)
        except ModError as exc:
            # A broken JAR must still be possible to disable safely.
            return ModInfo(destination.name, destination.name, enabled=enabled, error=str(exc))

    def set_all_enabled(self, enabled: bool) -> list[ModInfo]:
        mods = self.list_mods()
        # Check the whole batch before the first rename.
        for mod in mods:
            target = mod.filename.removesuffix(".disabled") if enabled else mod.filename if mod.filename.endswith(".disabled") else mod.filename + ".disabled"
            if target != mod.filename and self._path(target).exists():
                raise ModError("Массовая операция отменена: конфликт имени " + target)
        return [self.set_enabled(mod.filename, enabled) for mod in mods]

    def remove(self, filename: str, confirmed: bool = False) -> None:
        if not confirmed:
            raise ModError("Удаление мода требует подтверждения пользователя.")
        path = self._path(filename)
        if not filename.endswith((".jar", ".jar.disabled")) or not path.is_file():
            raise ModError("Указанный мод не найден.")
        path.unlink()

    def diagnostics(self, minecraft_version: str, loader: str, loader_version: str = "") -> list[str]:
        mods = [mod for mod in self.list_mods() if mod.enabled]
        installed = {mod_id: mod for mod in mods for mod_id in mod.mod_ids}
        issues = []
        for mod in mods:
            issues.extend(f"{mod.filename}: {issue}" for issue in compatibility(mod, minecraft_version, loader, loader_version))
            for mod_id in mod.mod_ids:
                duplicates = [other.filename for other in mods if mod_id in other.mod_ids]
                if len(duplicates) > 1:
                    issues.append(f"Повторяющийся mod ID {mod_id}: {', '.join(duplicates)}")
            for dep, constraint in mod.dependencies.items():
                if dep in {"minecraft", "java", "fabricloader", "quilt_loader", "forge", "neoforge"}:
                    continue
                target = installed.get(dep)
                if not target:
                    issues.append(f"{mod.name}: отсутствует обязательная зависимость {dep} {constraint}")
                elif matches_version(target.version, constraint) is not True:
                    issues.append(f"{mod.name}: зависимость {dep} {target.version} не подтверждена для {constraint}")
            for conflict, constraint in mod.conflicts.items():
                if conflict in installed and matches_version(installed[conflict].version, constraint) is not False:
                    issues.append(f"{mod.name}: вероятный конфликт с {conflict} ({constraint})")
        return list(dict.fromkeys(issues))


def compatibility(mod: ModInfo, minecraft_version: str, loader: str, loader_version: str = "") -> list[str]:
    issues = [mod.error] if mod.error else []
    if loader and loader not in mod.loaders:
        # Quilt's Fabric compatibility varies; never promise it without matching metadata.
        issues.append(f"Совместимость с {loader} не подтверждена (метаданные: {', '.join(mod.loaders) or 'неизвестно'}).")
    if minecraft_version:
        result = matches_version(minecraft_version, mod.minecraft)
        if result is False:
            issues.append(f"Нужна Minecraft {mod.minecraft}; выбрана {minecraft_version}.")
        elif result is None:
            issues.append(f"Не удалось проверить ограничение Minecraft {mod.minecraft}.")
    if loader_version and loader:
        dependency = {"fabric": "fabricloader", "quilt": "quilt_loader", "forge": "forge", "neoforge": "neoforge"}.get(loader)
        constraint = mod.dependencies.get(dependency)
        if constraint is not None:
            result = matches_version(loader_version, constraint)
            if result is False:
                issues.append(f"Нужен {loader} {constraint}; выбран {loader_version}.")
            elif result is None:
                issues.append(f"Не удалось проверить ограничение {loader} {constraint}.")
    return issues
