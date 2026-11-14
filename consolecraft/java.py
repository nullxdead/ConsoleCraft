"""Find and verify the Java runtime requested by a Minecraft version."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
from typing import Any, Callable

from minecraft_launcher_lib import runtime


class JavaError(RuntimeError):
    """A suitable Java runtime could not be used."""


@dataclass(frozen=True)
class JavaRuntime:
    path: str
    major: int


def required_java_major(metadata: dict[str, Any]) -> int:
    """Prefer Mojang's requirement; support old manifests without javaVersion."""
    specified = metadata.get("javaVersion", {}).get("majorVersion")
    if isinstance(specified, int) and specified > 0:
        return specified
    match = re.match(r"^1\.(\d+)(?:\.(\d+))?(?:$|[-_])", str(metadata.get("id", "")))
    if match:
        minor, patch = int(match[1]), int(match[2] or 0)
        if minor > 20 or (minor == 20 and patch >= 5):
            return 21
        if minor >= 18:
            return 17
        if minor == 17:
            return 16
    return 8


def java_major(executable: str | Path) -> int | None:
    """Read java -version without invoking a shell or opening a console window."""
    options: dict[str, Any] = {}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run(
            [str(executable), "-version"], capture_output=True, text=True,
            errors="replace", timeout=10, check=False, **options,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    output = result.stderr + "\n" + result.stdout
    match = re.search(r'(?:openjdk|java)\s+(?:version\s+)?["\']?(\d+)(?:\.(\d+))?', output, re.IGNORECASE)
    if match is None:
        return None
    return int(match[2]) if match[1] == "1" and match[2] else int(match[1])


def _candidates(game_directory: Path, metadata: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    component = metadata.get("javaVersion", {}).get("component")
    components = [component] if isinstance(component, str) else []
    if required_java_major(metadata) == 8:
        components.append("jre-legacy")
    for name in components:
        executable = runtime.get_executable_path(name, game_directory)
        if executable:
            candidates.append(executable)
    binary = "java.exe" if os.name == "nt" else "java"
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        candidates.append(str(Path(java_home) / "bin" / binary))
    executable = shutil.which("java")
    if executable:
        candidates.append(executable)
    if platform.system() == "Darwin":
        try:
            result = subprocess.run(
                ["/usr/libexec/java_home", "-v", str(required_java_major(metadata))],
                capture_output=True, text=True, timeout=10, check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                candidates.append(str(Path(result.stdout.strip()) / "bin" / "java"))
        except (OSError, subprocess.TimeoutExpired):
            pass
    if platform.system() == "Linux":
        for base in (Path("/usr/lib/jvm"), Path("/opt/java")):
            if base.is_dir():
                candidates.extend(str(path) for path in sorted(base.glob("*/bin/java")))
    return list(dict.fromkeys(candidates))


def find_java(
    game_directory: Path, metadata: dict[str, Any], configured: str | Path | None = None,
) -> JavaRuntime:
    """Choose the matching major, avoiding incompatible modern Java for old games."""
    required = required_java_major(metadata)
    if configured:
        path = os.path.expandvars(str(configured))
        path = str(Path(path).expanduser())
        executable = shutil.which(path) or path
        major = java_major(executable)
        if major is None:
            raise JavaError("Не удалось запустить указанную Java. Проверьте путь к исполняемому файлу.")
        if major != required:
            raise JavaError(f"Для этой версии нужна Java {required}; по указанному пути найдена Java {major}.")
        return JavaRuntime(executable, major)
    for executable in _candidates(Path(game_directory), metadata):
        major = java_major(executable)
        if major == required:
            return JavaRuntime(executable, major)
    raise JavaError(
        f"Не найдена Java {required}. Установите версию игры с интернетом "
        "или укажите путь к подходящей Java в настройках."
    )


def ensure_java(
    game_directory: Path, metadata: dict[str, Any], configured: str | Path | None = None,
    callback: dict[str, Callable[..., None]] | None = None, download: bool = True,
) -> JavaRuntime:
    """Use local Java first, optionally install Mojang's runtime for this version."""
    try:
        return find_java(game_directory, metadata, configured)
    except JavaError:
        if configured or not download:
            raise
    component = metadata.get("javaVersion", {}).get("component")
    if not component and required_java_major(metadata) == 8:
        component = "jre-legacy"
    if not isinstance(component, str):
        raise JavaError(f"Установите Java {required_java_major(metadata)} и укажите путь в настройках.")
    if any(character in component for character in "/\\:") or component in {".", ".."}:
        raise JavaError("Некорректное имя Java runtime в описании версии.")
    try:
        runtime.install_jvm_runtime(component, game_directory, callback=callback or {})
    except Exception as error:
        raise JavaError(
            f"Не удалось загрузить Java {required_java_major(metadata)}. "
            "Проверьте соединение или установите подходящую Java самостоятельно."
        ) from error
    return find_java(game_directory, metadata)
