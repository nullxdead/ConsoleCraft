"""Independent game profiles and bounded, portable profile archives.

Worlds are never included in exports or copies and are preserved by default
when removing profile metadata. Imported archives never choose host paths.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
from uuid import UUID, uuid4
import zipfile

from .config import ConfigStore


class ProfileError(ValueError):
    """Profile data or a requested operation is unsafe or invalid."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uuid(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ProfileError("Некорректный ID игрового профиля.") from exc


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=".profiles-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _safe_id(value: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 180 or value in {".", ".."}:
        raise ProfileError("Укажите корректный ID версии Minecraft/модлоадера.")
    if any(ord(c) < 32 or c in '/\\:*?"<>|' for c in value) or value[-1] in ". ":
        raise ProfileError("ID версии не должен содержать путь или управляющие символы.")


def _args(value: list[str], kind: str) -> None:
    if not isinstance(value, list) or len(value) > 64 or any(not isinstance(v, str) or len(v) > 4096 or any(ord(c) < 32 for c in v) for v in value):
        raise ProfileError(f"Аргументы {kind} должны быть списком строк без управляющих символов.")
    # Profile configuration must not smuggle credentials or replace the selected
    # account / filesystem roots. Advanced Java agents are handled by Ely.by only.
    forbidden = ("-javaagent", "-agentpath", "-agentlib", "-xbootclasspath", "-cp", "-classpath", "--class-path", "--module-path", "--accessToken", "--username", "--uuid", "--gameDir", "--assetsDir", "--clientId", "--xuid")
    for argument in value:
        lowered = argument.lower()
        if any(lowered == flag.lower() or lowered.startswith(flag.lower() + "=") or (flag.startswith("-agent") or flag == "-javaagent") and lowered.startswith(flag.lower() + ":") for flag in forbidden):
            raise ProfileError("Аргументы не могут подменять авторизацию, игровые каталоги или внедрять Java-агенты.")


def _secret_path(relative: Path | PurePosixPath) -> bool:
    """Exclude known launcher credentials and common secret-file conventions."""
    names = {"credentials", "credentials.json", "credentials.toml", "credentials.yml", "credentials.yaml", "tokens", "tokens.json", "token.json", "secrets", "secrets.json", "secrets.toml", "secrets.yml", "secrets.yaml", "config.json", "profiles.json", "id_rsa", "id_ed25519"}
    for part in relative.parts:
        lowered = part.casefold()
        if lowered == ".env" or lowered.startswith(".env.") or lowered in names or Path(lowered).suffix in {".pem", ".key", ".p12", ".pfx", ".keystore", ".jks"}:
            return True
    return False


@dataclass
class GameProfile:
    id: str
    name: str
    minecraft_version: str = "latest"
    loader: str = "vanilla"
    loader_version: str = ""
    game_directory: str = ""
    memory_mb: int = 2048
    java_path: str = ""
    jvm_args: list[str] = field(default_factory=list)
    game_args: list[str] = field(default_factory=list)
    auth_mode: str = "microsoft"
    elyby_enabled: bool = False
    created_at: str = field(default_factory=_now)
    last_launch: str = ""
    installed_version: str = ""
    legacy: bool = False

    def validate(self) -> None:
        self.id = _uuid(self.id)
        if not isinstance(self.name, str) or not self.name.strip() or len(self.name) > 80 or any(ord(c) < 32 for c in self.name):
            raise ProfileError("Имя профиля должно содержать 1–80 видимых символов.")
        self.name = self.name.strip()
        _safe_id(self.minecraft_version)
        if not isinstance(self.loader, str) or self.loader not in {"vanilla", "fabric", "forge", "neoforge", "quilt"}:
            raise ProfileError("Модлоадер должен быть vanilla, fabric, forge, neoforge или quilt.")
        if self.loader_version:
            _safe_id(self.loader_version)
        if self.installed_version:
            _safe_id(self.installed_version)
        if not isinstance(self.game_directory, str) or any(ord(c) < 32 for c in self.game_directory):
            raise ProfileError("Некорректный игровой каталог.")
        if not self.game_directory or not Path(self.game_directory).is_absolute():
            raise ProfileError("Игровой каталог должен быть абсолютным путём.")
        if type(self.memory_mb) is not int or not 512 <= self.memory_mb <= 65536:
            raise ProfileError("RAM профиля должна быть от 512 до 65536 МБ.")
        if not isinstance(self.java_path, str) or any(ord(c) < 32 for c in self.java_path):
            raise ProfileError("Некорректный путь к Java.")
        _args(self.jvm_args, "Java")
        _args(self.game_args, "Minecraft")
        if not isinstance(self.auth_mode, str) or self.auth_mode not in {"microsoft", "offline", "demo"}:
            raise ProfileError("Режим входа должен быть microsoft, offline или demo.")
        if type(self.elyby_enabled) is not bool or type(self.legacy) is not bool:
            raise ProfileError("Некорректные логические настройки профиля.")
        for value in (self.created_at, self.last_launch):
            if not isinstance(value, str):
                raise ProfileError("Некорректная дата профиля.")
            if value:
                try:
                    datetime.fromisoformat(value)
                except ValueError as exc:
                    raise ProfileError("Некорректная дата профиля.") from exc


class ProfileStore:
    MAX_FILES = 4096
    MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
    MAX_FILE_BYTES = 256 * 1024 * 1024
    PORTABLE_DIRECTORIES = {"mods", "config", "resourcepacks", "shaderpacks"}
    PORTABLE_FILES = {"options.txt", "optionsof.txt", ".consolecraft-mods.json"}

    def __init__(self, store: ConfigStore):
        self.store = store
        self.root = store.home / "profiles"
        self.path = store.home / "profiles.json"

    def _read(self) -> list[GameProfile]:
        if not self.path.exists():
            return []
        try:
            if self.path.stat().st_size > 2 * 1024 * 1024:
                raise ValueError("Oversized registry")
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get("profiles"), list):
                raise ValueError("Invalid registry")
            allowed = {item.name for item in fields(GameProfile)}
            profiles = []
            for entry in data["profiles"]:
                profile = GameProfile(**{k: v for k, v in entry.items() if k in allowed})
                profile.validate()
                profiles.append(profile)
            if len({p.id for p in profiles}) != len(profiles) or len({p.name.casefold() for p in profiles}) != len(profiles):
                raise ValueError("Duplicate profiles")
            directories = [str(Path(p.game_directory).resolve()).casefold() for p in profiles]
            if len(set(directories)) != len(directories):
                raise ValueError("Shared profile directories")
            return profiles
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise ProfileError("Файл profiles.json повреждён. Восстановите резервную копию; игровые файлы сохранены.") from exc

    def _write(self, profiles: list[GameProfile]) -> None:
        for profile in profiles:
            profile.validate()
        _atomic_json(self.path, {"schema_version": 1, "profiles": [asdict(p) for p in profiles]})

    def list(self) -> list[GameProfile]:
        return self._read()

    def ensure_default(self) -> GameProfile:
        profiles = self._read()
        settings = self.store.load()
        if not profiles:
            profile = GameProfile(
                id=str(uuid4()), name="Default", minecraft_version=settings.version,
                game_directory=str(self.store.game_directory), memory_mb=settings.memory_mb,
                java_path=settings.java_path, legacy=True,
            )
            self._write([profile])
            settings.default_profile = profile.id
            self.store.save(settings)
            return profile
        selected = next((p for p in profiles if p.id == settings.default_profile), profiles[0])
        if settings.default_profile != selected.id:
            settings.default_profile = selected.id
            self.store.save(settings)
        return selected

    def get(self, identifier: str | None = None) -> GameProfile:
        if identifier is None:
            return self.ensure_default()
        matches = [p for p in self._read() if p.id == identifier or p.name.casefold() == identifier.casefold()]
        if len(matches) != 1:
            raise ProfileError("Игровой профиль не найден; используйте profiles list.")
        return matches[0]

    def _check_unique(self, candidate: GameProfile, profiles: list[GameProfile]) -> None:
        destination = Path(candidate.game_directory).resolve()
        for profile in profiles:
            if profile.id == candidate.id:
                continue
            other = Path(profile.game_directory).resolve()
            if profile.name.casefold() == candidate.name.casefold():
                raise ProfileError("Профиль с таким именем уже существует.")
            if destination == other or destination in other.parents or other in destination.parents:
                raise ProfileError("Каталоги игровых профилей не должны пересекаться.")

    def create(self, name: str, **settings) -> GameProfile:
        profiles = self._read()
        if set(settings) - {item.name for item in fields(GameProfile)} or any(k in settings for k in {"id", "name", "legacy", "created_at", "last_launch"}):
            raise ProfileError("Недопустимые поля нового профиля.")
        identity = str(uuid4())
        settings.setdefault("game_directory", str(self.root / identity / "game"))
        if not settings["game_directory"]:
            settings["game_directory"] = str(self.root / identity / "game")
        profile = GameProfile(identity, name, **settings)
        profile.validate()
        self._check_unique(profile, profiles)
        Path(profile.game_directory).mkdir(parents=True, exist_ok=True)
        self._write([*profiles, profile])
        return profile

    def update(self, identifier: str, **changes) -> GameProfile:
        profiles = self._read()
        current = self.get(identifier)
        allowed = {item.name for item in fields(GameProfile)} - {"id", "created_at", "legacy"}
        if set(changes) - allowed:
            raise ProfileError("Недопустимые поля профиля.")
        if "game_directory" in changes and changes["game_directory"] != current.game_directory:
            # Moving a profile must never silently strand worlds or share another
            # profile's files. External directories are chosen at creation/import.
            raise ProfileError("Для другого игрового каталога создайте или импортируйте отдельный профиль.")
        candidate = replace(current, **changes)
        candidate.validate()
        self._check_unique(candidate, profiles)
        if any(k in changes for k in {"minecraft_version", "loader", "loader_version"}) and "installed_version" not in changes:
            candidate.installed_version = ""
        self._write([candidate if p.id == current.id else p for p in profiles])
        return candidate

    def rename(self, identifier: str, name: str) -> GameProfile:
        return self.update(identifier, name=name)

    def relocate(self, identifier: str, destination: str | Path) -> GameProfile:
        """Copy game data into an empty folder; keep the original files intact."""
        profile = self.get(identifier)
        source = Path(profile.game_directory).resolve()
        target = Path(destination).expanduser()
        if not target.is_absolute() or target.is_symlink():
            raise ProfileError("Выберите абсолютный путь к новой пустой игровой папке.")
        target = target.resolve()
        if target == source:
            return profile
        if source in target.parents or target in source.parents:
            raise ProfileError("Новая папка не должна пересекаться с прежней.")
        candidate = replace(profile, game_directory=str(target), legacy=False)
        candidate.validate()
        self._check_unique(candidate, self._read())
        if target.exists() and (not target.is_dir() or any(target.iterdir())):
            raise ProfileError("Новая игровая папка должна быть пустой.")
        files = []
        for path in source.rglob("*") if source.exists() else []:
            relative = path.relative_to(source)
            if profile.legacy and relative.parts[0] in {"assets", "libraries", "versions", "runtime"}:
                continue  # The shared installation cache stays where it is.
            if path.is_symlink() or not path.resolve().is_relative_to(source):
                raise ProfileError("Папка содержит символическую ссылку; перенос отменён.")
            if path.is_file(): files.append((path, relative))
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=target.parent, prefix=".consolecraft-move-") as temporary:
            staged = Path(temporary) / "game"
            staged.mkdir()
            for path, relative in files:
                output = staged / relative
                output.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, output)
            if target.exists(): target.rmdir()  # Only an empty directory can be replaced.
            staged.replace(target)
        self._write([candidate if item.id == profile.id else item for item in self._read()])
        return candidate

    def _portable_files(self, profile: GameProfile):
        root = Path(profile.game_directory).resolve()
        count = total = 0
        if not root.exists():
            return
        for path in root.rglob("*"):
            relative = path.relative_to(root)
            if relative.parts[0] not in self.PORTABLE_DIRECTORIES and relative.as_posix() not in self.PORTABLE_FILES:
                continue
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise ProfileError("Профиль содержит символическую ссылку; перенос отменён.")
            if not path.is_file():
                continue
            count += 1
            total += path.stat().st_size
            if count > self.MAX_FILES or total > self.MAX_ARCHIVE_BYTES or path.stat().st_size > self.MAX_FILE_BYTES:
                raise ProfileError("Превышен допустимый размер переносимого профиля.")
            if _secret_path(relative):
                # Game mod configs are exportable; launcher auth/config files are not.
                continue
            yield path, relative

    def copy(self, identifier: str, name: str) -> GameProfile:
        source = self.get(identifier)
        portable = list(self._portable_files(source))
        data = asdict(source)
        for key in ("id", "name", "game_directory", "created_at", "last_launch", "legacy"):
            data.pop(key)
        destination = self.create(name, **data)
        for source_path, relative in portable:
            target = Path(destination.game_directory) / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, target)
        return destination

    def set_default(self, identifier: str) -> GameProfile:
        profile = self.get(identifier)
        settings = self.store.load()
        settings.default_profile = profile.id
        self.store.save(settings)
        return profile

    def mark_launched(self, identifier: str) -> GameProfile:
        return self.update(identifier, last_launch=_now())

    def delete(self, identifier: str, *, confirmed: bool = False, delete_worlds: bool = False, confirm_worlds: bool = False) -> None:
        profile = self.get(identifier)
        if not confirmed:
            raise ProfileError("Подтвердите удаление профиля. По умолчанию все игровые файлы и миры сохраняются.")
        if delete_worlds and not confirm_worlds:
            raise ProfileError("Удаление игровых файлов и миров требует отдельного подтверждения.")
        if delete_worlds:
            managed = (self.root / profile.id / "game").resolve()
            target = Path(profile.game_directory).resolve()
            if profile.legacy or target != managed or not managed.is_relative_to(self.root.resolve()):
                raise ProfileError("Старый или внешний игровой каталог автоматически не удаляется.")
            if target.exists():
                shutil.rmtree(target)
        remaining = [p for p in self._read() if p.id != profile.id]
        self._write(remaining)
        settings = self.store.load()
        if settings.default_profile == profile.id:
            settings.default_profile = remaining[0].id if remaining else ""
            self.store.save(settings)

    def export_profile(self, identifier: str, destination: str | Path, *, include_mods: bool = True) -> Path:
        profile = self.get(identifier)
        destination = Path(destination).expanduser().resolve()
        if destination.exists():
            raise ProfileError("Файл экспорта уже существует; выберите другое имя.")
        files_to_export = [(p, r) for p, r in self._portable_files(profile) if include_mods or r.parts[0] != "mods"]
        portable = asdict(profile)
        for key in ("id", "game_directory", "java_path", "created_at", "last_launch", "legacy", "installed_version"):
            portable.pop(key)
        # Runtime arguments may contain local paths or user-supplied properties;
        # they are intentionally excluded from archives shared with other users.
        portable["jvm_args"] = []
        portable["game_args"] = []
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".tmp-" + uuid4().hex)
        try:
            with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("profile.json", json.dumps({"schema_version": 1, "profile": portable}, ensure_ascii=False))
                for path, relative in files_to_export:
                    archive.write(path, "game/" + relative.as_posix())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    def import_profile(self, source: str | Path, *, name: str | None = None) -> GameProfile:
        source = Path(source).expanduser()
        try:
            if source.stat().st_size > self.MAX_ARCHIVE_BYTES:
                raise ProfileError("Архив профиля слишком велик.")
            with zipfile.ZipFile(source) as archive:
                infos = archive.infolist()
                if len(infos) > self.MAX_FILES + 1 or sum(i.file_size for i in infos) > self.MAX_ARCHIVE_BYTES:
                    raise ProfileError("Превышен размер распакованного профиля.")
                seen = set()
                for item in infos:
                    path = PurePosixPath(item.filename)
                    if ("\\" in item.orig_filename or path.is_absolute() or ".." in path.parts or ":" in item.filename
                        or any(ord(c) < 32 for c in item.filename) or not path.parts
                        or item.filename.casefold() in seen or stat.S_ISLNK(item.external_attr >> 16)
                        or item.flag_bits & 1 or item.file_size > self.MAX_FILE_BYTES
                        or item.file_size > max(item.compress_size, 1) * 1000):
                        raise ProfileError("Архив содержит небезопасный путь, ссылку или чрезмерно сжатый файл.")
                    seen.add(item.filename.casefold())
                    if item.filename != "profile.json" and _secret_path(path):
                        raise ProfileError("Архив содержит потенциально секретный файл; импорт отменён.")
                    if item.filename == "profile.json":
                        if item.file_size > 64 * 1024:
                            raise ProfileError("Описание профиля слишком велико.")
                    elif len(path.parts) < 3 or path.parts[0] != "game" or path.parts[1] not in self.PORTABLE_DIRECTORIES:
                        if item.filename not in {"game/" + p for p in self.PORTABLE_FILES} and not item.is_dir():
                            raise ProfileError("Архив содержит файл вне переносимых данных профиля.")
                data = json.loads(archive.read("profile.json"))
                if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get("profile"), dict):
                    raise ProfileError("Некорректный формат экспорта профиля.")
                settings = data["profile"]
                allowed = {"name", "minecraft_version", "loader", "loader_version", "memory_mb", "auth_mode", "elyby_enabled", "jvm_args", "game_args"}
                if set(settings) - allowed:
                    raise ProfileError("Архив пытается задать локальные пути или недопустимые параметры.")
                if settings.get("jvm_args") or settings.get("game_args"):
                    raise ProfileError("Аргументы запуска из чужого профиля нужно добавить вручную после проверки.")
                profile_name = name or settings.pop("name", "Imported")
                settings.pop("name", None)
                # Stream and CRC-validate all members in a staging directory before
                # creating a profile. Archive size limits never turn into 1GB RAM use.
                self.store.home.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(prefix=".profile-import-", dir=self.store.home) as staging:
                    staged_game = Path(staging) / "game"
                    staged_game.mkdir()
                    for item in infos:
                        if item.filename == "profile.json" or item.is_dir():
                            continue
                        target = staged_game.joinpath(*PurePosixPath(item.filename).parts[1:])
                        if not target.resolve().is_relative_to(staged_game):
                            raise ProfileError("Импорт выходит за пределы каталога профиля.")
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with archive.open(item) as reader, target.open("xb") as writer:
                            shutil.copyfileobj(reader, writer, length=65536)
                    profile = self.create(profile_name, **settings)
                    destination = Path(profile.game_directory).resolve()
                    try:
                        destination.rmdir()  # freshly created managed directory only
                        staged_game.replace(destination)
                    except OSError:
                        self.delete(profile.id, confirmed=True)
                        raise
                    return profile
        except (zipfile.BadZipFile, KeyError, json.JSONDecodeError, UnicodeError, RuntimeError, TypeError) as exc:
            raise ProfileError("Архив профиля повреждён или не поддерживается; данные не импортированы.") from exc
