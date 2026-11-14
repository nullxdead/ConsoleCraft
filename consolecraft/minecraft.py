"""Official version catalogue, installation, command generation and process handling."""

from __future__ import annotations

from contextlib import nullcontext
import json
import hashlib
import inspect
import ipaddress
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import tempfile
import time
from urllib.parse import urlparse
from contextlib import contextmanager
from typing import Any, Callable

from minecraft_launcher_lib import command, install, runtime, fabric, forge, quilt, _helper
import requests

from .java import ensure_java
from . import __version__
from .cancellation import DownloadCancelled, current_event, check_cancelled

_NETWORK_LOCK = threading.RLock()


@contextmanager
def library_limits(timeout: int = 30, max_workers: int = 8):
    """Bound library 8's otherwise unbounded HTTP and installer calls.

    This CLI serializes installers. Defaults are scoped to the operation and
    apply to its downloader threads; explicit caller timeouts remain intact.
    """
    with _NETWORK_LOCK:
        request = requests.sessions.Session.request
        run = subprocess.run
        executor = install.ThreadPoolExecutor
        runtime_executor = runtime.ThreadPoolExecutor
        event = current_event()
        modules = (install, runtime, fabric, forge, quilt)
        downloads = {module: module.download_file for module in modules}
        download = _helper.download_file
        download_signature = inspect.signature(download)
        def timed_request(session, method, url, **kwargs):
            check_cancelled(event)
            kwargs.setdefault("timeout", (10, timeout))
            response = request(session, method, url, **kwargs)
            try:
                check_cancelled(event)
            except BaseException:
                response.close()
                raise
            parsed = urlparse(url)
            installer_hosts = {"maven.fabricmc.net", "maven.minecraftforge.net", "maven.neoforged.net", "maven.quiltmc.org"}
            if method.upper() == "GET" and parsed.scheme == "https" and parsed.hostname in installer_hosts and "installer" in parsed.path and parsed.path.endswith(".jar") and response.status_code == 200:
                checksum = request(session, "GET", url + ".sha1", timeout=(10, timeout), allow_redirects=False)
                digest = checksum.text.strip().split()[0] if checksum.text.strip() else ""
                if checksum.status_code != 200 or re.fullmatch(r"[0-9a-fA-F]{40}", digest) is None:
                    response.close()
                    raise MinecraftError("Официальный установщик не предоставил корректную контрольную сумму SHA-1.")
                class VerifiedRaw:
                    # Library 8 copies response.raw rather than using iter_content.
                    # Guard both interfaces so no consumer can skip verification.
                    def __init__(self, raw):
                        self.raw = raw
                        self.current = hashlib.sha1()
                        self.size = 0
                    def __getattr__(self, name):
                        return getattr(self.raw, name)
                    @property
                    def decode_content(self):
                        return getattr(self.raw, "decode_content", False)
                    @decode_content.setter
                    def decode_content(self, value):
                        self.raw.decode_content = value
                    def read(self, amt=None, *args, **raw_kwargs):
                        chunk = self.raw.read(amt, *args, **raw_kwargs)
                        self.size += len(chunk)
                        if self.size > 128 * 1024 * 1024:
                            raise MinecraftError("Размер официального установщика превышает допустимый предел.")
                        self.current.update(chunk)
                        if not chunk or amt is None or amt < 0:
                            if self.current.hexdigest() != digest.lower():
                                raise MinecraftError("Контрольная сумма официального установщика не совпала; запуск отменён.")
                        return chunk
                    def stream(self, amt=65536, decode_content=None):
                        if decode_content is not None:
                            self.decode_content = decode_content
                        while True:
                            chunk = self.read(amt)
                            if not chunk:
                                break
                            yield chunk
                response.raw = VerifiedRaw(response.raw)
            if event is not None and response.raw is not None:
                class CancellableRaw:
                    def __init__(self, raw): self.raw = raw
                    def __getattr__(self, name): return getattr(self.raw, name)
                    @property
                    def decode_content(self): return self.raw.decode_content
                    @decode_content.setter
                    def decode_content(self, value): self.raw.decode_content = value
                    def read(self, *args, **kwargs):
                        try:
                            check_cancelled(event)
                            chunk = self.raw.read(*args, **kwargs)
                            check_cancelled(event)
                            return chunk
                        except DownloadCancelled:
                            try: self.raw.close()
                            except Exception: pass
                            raise
                    def stream(self, *args, **kwargs):
                        try:
                            check_cancelled(event)
                            for chunk in self.raw.stream(*args, **kwargs):
                                check_cancelled(event)
                                yield chunk
                        except DownloadCancelled:
                            try: self.raw.close()
                            except Exception: pass
                            raise
                response.raw = CancellableRaw(response.raw)
            return response
        def atomic_download(*args, **kwargs):
            check_cancelled(event)
            bound = download_signature.bind(*args, **kwargs)
            bound.apply_defaults()
            destination = Path(bound.arguments['path'])
            directory = bound.arguments['minecraft_directory']
            if directory is not None:
                _helper.check_path_inside_minecraft_directory(directory, str(destination))
            if destination.is_file() and not bound.arguments['overwrite']:
                digest = bound.arguments['sha1']
                if digest is None or _helper.get_sha1_hash(str(destination)) == digest:
                    return False
            destination.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix='.consolelauncher-', suffix='.part', dir=destination.parent)
            os.close(fd)
            temporary = Path(name)
            try:
                callbacks = dict(bound.arguments['callback'] or {})
                status = callbacks.get('setStatus')
                if status:
                    callbacks['setStatus'] = lambda text: status(str(text).replace(temporary.name, destination.name))
                bound.arguments.update(path=str(temporary), callback=callbacks, overwrite=True)
                downloaded = download(*bound.args, **bound.kwargs)
                check_cancelled(event)
                if downloaded:
                    temporary.replace(destination)
                return downloaded
            finally:
                temporary.unlink(missing_ok=True)
        def timed_run(*args, **kwargs):
            kwargs.setdefault("timeout", 900)
            if event is not None:
                check_cancelled(event)
                duration = kwargs.pop('timeout')
                check = kwargs.pop('check', False)
                input_data = kwargs.pop('input', None)
                if input_data is not None:
                    if kwargs.get('stdin') is not None: raise ValueError('stdin and input are mutually exclusive')
                    kwargs['stdin'] = subprocess.PIPE
                if kwargs.pop('capture_output', False):
                    if kwargs.get('stdout') is not None or kwargs.get('stderr') is not None: raise ValueError('capture_output conflicts with stdout/stderr')
                    kwargs['stdout'] = kwargs['stderr'] = subprocess.PIPE
                if os.name == 'nt':
                    kwargs['creationflags'] = kwargs.get('creationflags', 0) | subprocess.CREATE_NEW_PROCESS_GROUP
                else:
                    kwargs['start_new_session'] = True
                with subprocess.Popen(*args, **kwargs) as process:
                    try:
                        deadline = None if duration is None else time.monotonic() + duration
                        while True:
                            check_cancelled(event)
                            wait = 0.25 if deadline is None else max(0, min(0.25, deadline - time.monotonic()))
                            try:
                                stdout, stderr = process.communicate(input=input_data, timeout=wait)
                                break
                            except subprocess.TimeoutExpired:
                                input_data = None
                                if deadline is not None and time.monotonic() >= deadline:
                                    raise subprocess.TimeoutExpired(process.args, duration)
                        check_cancelled(event)
                    except BaseException:
                        if process.poll() is None:
                            if os.name == 'nt':
                                try:
                                    taskkill = str(Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'taskkill.exe')
                                    killer = subprocess.Popen([taskkill, '/PID', str(process.pid), '/T', '/F'], stdout=subprocess.DEVNULL,
                                                              stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
                                    killer.wait(timeout=5)
                                except (OSError, subprocess.TimeoutExpired):
                                    pass
                            else:
                                try: os.killpg(process.pid, signal.SIGKILL)
                                except OSError: pass
                            if process.poll() is None: process.kill()
                        process.communicate()
                        raise
                    if check and process.returncode:
                        raise subprocess.CalledProcessError(process.returncode, process.args, output=stdout, stderr=stderr)
                    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
            return run(*args, **kwargs)
        def limited_executor(max_workers=None, **kwargs):
            if event is not None:
                class CancelAwareExecutor(executor):
                    def __exit__(self, exc_type, exc_value, traceback):
                        self.shutdown(wait=True, cancel_futures=event.is_set())
                        return False
                return CancelAwareExecutor(max_workers=max_workers or worker_limit, **kwargs)
            return executor(max_workers=max_workers or worker_limit, **kwargs)
        worker_limit = max_workers
        requests.sessions.Session.request = timed_request
        subprocess.run = timed_run
        install.ThreadPoolExecutor = limited_executor
        runtime.ThreadPoolExecutor = limited_executor
        if event is not None:
            for module in modules: module.download_file = atomic_download
        try:
            yield
        finally:
            requests.sessions.Session.request = request
            subprocess.run = run
            install.ThreadPoolExecutor = executor
            runtime.ThreadPoolExecutor = runtime_executor
            for module, original in downloads.items(): module.download_file = original


MANIFEST_URL = "https://launchermeta.mojang.com/mc/game/version_manifest_v2.json"
Callback = dict[str, Callable[..., None]]


class MinecraftError(RuntimeError):
    """An installation, catalogue or launch problem suitable for display to users."""


def server_address(server: str, port: int = 25565) -> tuple[str, int]:
    """Validate a standard Minecraft host/port without contacting the server."""
    if port is None:
        port = 25565
    if not isinstance(server, str) or not server or len(server) > 300 or any(c.isspace() or ord(c) < 32 for c in server):
        raise MinecraftError("Укажите IP-адрес или домен сервера без пробелов.")
    if type(port) is not int or not 1 <= port <= 65535:
        raise MinecraftError("Порт сервера должен быть от 1 до 65535.")
    host, supplied = server, None
    if server.startswith("["):
        match = re.fullmatch(r"\[([^\]]+)\](?::(\d+))?", server)
        if not match:
            raise MinecraftError("Некорректный IPv6-адрес сервера.")
        host, supplied = match[1], match[2]
    elif server.count(":") == 1:
        host, supplied = server.rsplit(":", 1)
        if not supplied.isdigit():
            raise MinecraftError("Порт сервера должен быть числом.")
    if supplied is not None:
        embedded = int(supplied)
        if not 1 <= embedded <= 65535 or port != 25565 and port != embedded:
            raise MinecraftError("Некорректный или противоречивый порт сервера.")
        port = embedded
    try:
        host = str(ipaddress.ip_address(host))
    except ValueError:
        try:
            host = host.encode("idna").decode("ascii").rstrip(".")
        except UnicodeError:
            raise MinecraftError("Некорректный домен сервера.") from None
        if not host or len(host) > 253 or any(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) is None for label in host.split(".")):
            raise MinecraftError("Некорректный IP-адрес или домен сервера.")
    return host, port


def validate_version_id(version: str) -> str:
    """Keep official unusual IDs, while rejecting traversal and Windows device names."""
    if (
        not isinstance(version, str) or not version or len(version) > 180
        or version in {".", ".."} or version[-1:] in {".", " "}
        or any(ord(character) < 32 or character in '/\\:*?"<>|' for character in version)
        or re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", version, re.IGNORECASE)
    ):
        raise MinecraftError("Недопустимое имя версии Minecraft.")
    return version


class MinecraftService:
    def __init__(self, game_directory: Path | str, timeout: int = 30, max_workers: int = 8):
        self.game_directory = Path(game_directory).expanduser().resolve()
        self.minecraft_directory = self.game_directory
        self.manifest_cached = False
        self._manifest: dict[str, Any] | None = None
        self.timeout = timeout
        self.max_workers = max_workers

    def _version_directory(self, version: str) -> Path:
        validate_version_id(version)
        versions = self.game_directory / "versions"
        result = (versions / version).resolve()
        if not result.is_relative_to(versions.resolve()):
            raise MinecraftError("Каталог версии находится за пределами каталога Minecraft.")
        return result

    def _get_manifest(self) -> dict[str, Any]:
        if self._manifest is not None:
            return self._manifest
        cache = self.game_directory / ".consolecraft-version-manifest.json"
        try:
            response = requests.get(
                MANIFEST_URL, timeout=(10, self.timeout), headers={"User-Agent": f"ConsoleCraft/{__version__}"},
            )
            response.raise_for_status()
            manifest = response.json()
            if not isinstance(manifest.get("versions"), list) or not isinstance(manifest.get("latest"), dict):
                raise ValueError("Invalid manifest")
            self._manifest = manifest
            try:
                self.game_directory.mkdir(parents=True, exist_ok=True)
                temporary = cache.with_suffix(".tmp")
                temporary.write_text(json.dumps(manifest), encoding="utf-8")
                temporary.replace(cache)
            except OSError:
                pass  # Catalogue remains useful in a read-only directory.
            return manifest
        except (requests.RequestException, ValueError, AttributeError):
            try:
                manifest = json.loads(cache.read_text(encoding="utf-8"))
                if not isinstance(manifest.get("versions"), list) or not isinstance(manifest.get("latest"), dict):
                    raise ValueError("Invalid cache")
                self._manifest = manifest
                self.manifest_cached = True
                return manifest
            except (OSError, ValueError, AttributeError) as error:
                raise MinecraftError(
                    "Не удалось получить список версий. Проверьте интернет; "
                    "уже установленные версии доступны без загрузки каталога."
                ) from error

    def installed_versions(self) -> list[dict[str, Any]]:
        directory = self.game_directory / "versions"
        if not directory.is_dir():
            return []
        versions = []
        for path in directory.iterdir():
            if not path.is_dir():
                continue
            try:
                data = self.metadata(path.name)
                jar_id = validate_version_id(str(data.get("jar", path.name)))
                jar = self._version_directory(jar_id) / f"{jar_id}.jar"
                if not jar.is_file():
                    continue
                versions.append({
                    "id": path.name, "type": data.get("type", "custom"),
                    "releaseTime": data.get("releaseTime", ""), "installed": True,
                })
            except (MinecraftError, OSError):
                continue
        return sorted(versions, key=lambda entry: str(entry["releaseTime"]), reverse=True)

    def list_versions(
        self, include_snapshots: bool = False, installed_only: bool = False,
    ) -> list[dict[str, Any]]:
        local = self.installed_versions()
        if installed_only:
            return local
        installed_ids = {entry["id"] for entry in local}
        versions = []
        seen: set[str] = set()
        for item in self._get_manifest()["versions"]:
            if not include_snapshots and item.get("type") != "release":
                continue
            version = item.get("id")
            try:
                validate_version_id(version)
            except MinecraftError:
                continue
            versions.append({**item, "installed": version in installed_ids})
            seen.add(version)
        versions.extend(
            entry for entry in local
            if entry["id"] not in seen
            and (include_snapshots or entry["type"] not in {"snapshot", "old_alpha", "old_beta"})
        )
        return versions

    def resolve_version(self, version: str = "latest") -> str:
        if version in {"latest", "release", "snapshot", "latest-snapshot"}:
            kind = "snapshot" if "snapshot" in version else "release"
            return validate_version_id(self._get_manifest()["latest"][kind])
        validate_version_id(version)
        if (self._version_directory(version) / f"{version}.json").is_file():
            return version
        if any(item.get("id") == version for item in self._get_manifest()["versions"]):
            return version
        raise MinecraftError(f"Версия {version} не найдена в официальном каталоге.")

    def metadata(self, version: str) -> dict[str, Any]:
        visited: set[str] = set()

        def read(current: str) -> dict[str, Any]:
            if current in visited or len(visited) > 12:
                raise MinecraftError("Циклическое наследование версий Minecraft.")
            visited.add(current)
            path = self._version_directory(current) / f"{current}.json"
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("Expected an object")
                validate_version_id(data.get("id", current))
                java_version = data.get("javaVersion")
                if java_version is not None and not isinstance(java_version, dict):
                    raise ValueError("Invalid Java version metadata")
                if java_version and "component" in java_version:
                    validate_version_id(java_version["component"])
                if "jar" in data:
                    validate_version_id(data["jar"])
                if "inheritsFrom" in data:
                    parent = read(validate_version_id(data["inheritsFrom"]))
                    data = {**parent, **data}
                return data
            except (OSError, ValueError, TypeError) as error:
                raise MinecraftError(
                    f"Описание версии {current} отсутствует или повреждено. Переустановите версию."
                ) from error

        return read(validate_version_id(version))

    def install(
        self, version: str, callback: Callback | None = None, java_path: str | Path | None = None,
    ) -> str:
        resolved = self.resolve_version(version)
        self._version_directory(resolved)
        if (self.game_directory / "versions" / resolved / f"{resolved}.json").is_file():
            self.metadata(resolved)  # Validate local inheritance before invoking the library.
        self.game_directory.mkdir(parents=True, exist_ok=True)
        java_phase = False
        callbacks = dict(callback or {})
        original_status = callbacks.get("setStatus")

        def set_status(status: str) -> None:
            nonlocal java_phase
            if status == "Install java runtime":
                java_phase = True
            elif status in {"Download Libraries", "Download Assets"}:
                java_phase = False
            if original_status:
                original_status(status)

        callbacks["setStatus"] = set_status
        try:
            with library_limits(self.timeout, self.max_workers):
                install.install_minecraft_version(resolved, self.game_directory, callback=callbacks)
        except Exception as error:
            # The library installs the game first, then its JVM. A manually configured
            # JVM remains usable when Mojang has no runtime for this platform.
            if not (java_phase and java_path and not self.metadata(resolved).get("inheritsFrom")):
                raise MinecraftError(
                    f"Не удалось установить Minecraft {resolved}. Проверьте интернет, "
                    "свободное место и права на каталог; повторная установка продолжит загрузку."
                ) from error
        metadata = self.metadata(resolved)
        jar_id = validate_version_id(str(metadata.get("jar", resolved)))
        if not (self._version_directory(jar_id) / f"{jar_id}.jar").is_file():
            raise MinecraftError("Файл игры не был загружен. Повторите установку и проверьте соединение.")
        with library_limits(self.timeout, self.max_workers):
            ensure_java(self.game_directory, metadata, configured=java_path, callback=callback)
        return resolved

    def build_command(
        self, version: str, username: str, uuid: str, token: str,
        java_path: str | Path | None = None, memory_mb: int = 2048,
        demo: bool = False, offline: bool = False, download_java: bool = True,
        client_id: str = "", xuid: str = "",
        game_directory: Path | str | None = None, jvm_args: list[str] | None = None,
        game_args: list[str] | None = None, injector_path: Path | str | None = None,
        server: str | None = None, port: int = 25565,
    ) -> list[str]:
        validate_version_id(version)
        if isinstance(memory_mb, bool) or not isinstance(memory_mb, int) or not 512 <= memory_mb <= 65536:
            raise MinecraftError("Объём памяти должен быть от 512 до 65536 МБ.")
        metadata = self.metadata(version)
        extra_java = list(jvm_args or [])
        extra_game = list(game_args or [])
        if any(not isinstance(arg, str) or "\x00" in arg or "\n" in arg for arg in extra_java + extra_game):
            raise MinecraftError("Аргументы должны быть строками без управляющих символов.")
        if injector_path:
            if not offline:
                raise MinecraftError("Ely.by нельзя подключать к сеансу Microsoft.")
            from .elyby import ElyByService
            extra_java.extend(ElyByService.arguments_for_verified(Path(injector_path)))
        if not offline and any(arg.startswith("-javaagent:") for arg in extra_java):
            raise MinecraftError("Java-agent сторонней авторизации запрещён для Microsoft-сеанса.")
        with library_limits(self.timeout, self.max_workers):
            java = ensure_java(self.game_directory, metadata, configured=java_path, download=download_java)
        options = {
            "username": username, "uuid": uuid, "token": token,
            "executablePath": java.path,
            "jvmArguments": ["-Xms256M", f"-Xmx{memory_mb}M", "-Dlog4j2.formatMsgNoLookups=true", *extra_java],
            "launcherName": "ConsoleCraft", "launcherVersion": __version__,
            "gameDirectory": str(Path(game_directory).expanduser().resolve() if game_directory else self.game_directory), "demo": demo,
            "enableLoggingConfig": True, "disableMultiplayer": demo,
        }
        if server is not None:
            if demo:
                raise MinecraftError("Деморежим не поддерживает подключение к серверу.")
            host, server_port = server_address(server, port)
            features = json.dumps(metadata.get("arguments", {}).get("game", []))
            if "is_quick_play_multiplayer" in features:
                address = f"[{host}]" if ":" in host else host
                options["quickPlayMultiplayer"] = f"{address}:{server_port}"
            else:
                options["server"] = host
                options["port"] = str(server_port)
        try:
            arguments = command.get_minecraft_command(version, self.game_directory, options)
        except Exception as error:
            raise MinecraftError("Не удалось подготовить запуск. Переустановите выбранную версию.") from error
        # minecraft-launcher-lib 8 does not yet substitute these modern manifest fields.
        arguments = [
            argument.replace("${clientid}", client_id).replace("${auth_xuid}", xuid)
            for argument in arguments
        ]
        if any(re.search(r"\$\{[^{}]+\}", argument) for argument in arguments):
            raise MinecraftError("Эта версия использует неподдерживаемые параметры запуска. Обновите лаунчер.")
        protected = {"--accessToken", "--uuid", "--username", "--gameDir", "--assetsDir", "--assetIndex", "--clientId", "--xuid"}
        if any(arg.split("=", 1)[0] in protected for arg in extra_game):
            raise MinecraftError("Дополнительные аргументы не могут заменять аккаунт или каталоги игры.")
        return arguments + extra_game

    def launch(
        self, version: str, username: str, uuid: str, token: str,
        java_path: str | Path | None = None, memory_mb: int = 2048,
        demo: bool = False, offline: bool = False, log_file: Path | None = None,
        on_output: Callable[[str], None] | None = None, download_java: bool = True,
        client_id: str = "", xuid: str = "",
        game_directory: Path | str | None = None, jvm_args: list[str] | None = None,
        game_args: list[str] | None = None, injector_path: Path | str | None = None,
        server: str | None = None, port: int = 25565,
    ) -> int:
        arguments = self.build_command(
            version, username, uuid, token, java_path, memory_mb, demo, offline, download_java,
            client_id, xuid,
            game_directory=game_directory, jvm_args=jvm_args, game_args=game_args, injector_path=injector_path,
            server=server, port=port,
        )
        if log_file is not None:
            log_file = Path(log_file)
            log_file.parent.mkdir(parents=True, exist_ok=True)
        output = on_output or (lambda line: print(line, flush=True))
        log_context = log_file.open("w", encoding="utf-8") if log_file else nullcontext(None)
        launch_directory = Path(game_directory).expanduser().resolve() if game_directory else self.game_directory
        launch_directory.mkdir(parents=True, exist_ok=True)
        try:
            with log_context as log:
                with subprocess.Popen(
                    arguments, cwd=launch_directory, shell=False,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", bufsize=1,
                ) as process:
                    try:
                        assert process.stdout is not None
                        for line in process.stdout:
                            safe_line = line.replace(token, "[REDACTED]") if len(token) > 1 else line
                            if log:
                                log.write(safe_line)
                                log.flush()
                            output(safe_line.rstrip("\r\n"))
                        return process.wait()
                    except BaseException:
                        if process.poll() is None:
                            process.terminate()
                            try:
                                process.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                process.kill()
                                process.wait()
                        raise
        except OSError as error:
            raise MinecraftError("Не удалось запустить Java или открыть журнал игры. Проверьте настройки и права.") from error
