"""Create a clean, portable source ZIP using an explicit source allowlist."""

from __future__ import annotations

from pathlib import Path
import re
import tomllib
import zipfile


ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = (
    "README.md",
    "CHANGELOG.md",
    "LICENSE",
    "LICENSE-MIT-4.1.0",
    "pyproject.toml",
    "requirements.txt",
    "MANIFEST.in",
    ".gitignore",
    ".gitattributes",
    "launcher.py",
    "start.bat",
    "start.sh",
    ".github/workflows/ci.yml",
    "scripts/package_source.py",
)
PYTHON_SOURCE_DIRECTORIES = ("consolecraft", "tests")


def main() -> int:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        version = tomllib.load(handle)["project"]["version"]
    if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9.+-]+", version):
        raise ValueError("The project version is not a safe archive name.")

    sources = {ROOT / relative for relative in ROOT_FILES}
    for directory in PYTHON_SOURCE_DIRECTORIES:
        sources.update((ROOT / directory).rglob("*.py"))
    sources.update((ROOT / "docs").rglob("*.md"))
    sources.update((ROOT / "scripts").glob("*.py"))
    sources.update((ROOT / "scripts").glob("*.sh"))
    sources.update((ROOT / "scripts").glob("*.command"))
    sources.update((ROOT / "scripts").glob("*.ps1"))
    sources.update((ROOT / ".github" / "workflows").glob("*.yml"))
    for source in sources:
        if not source.is_file() or source.is_symlink() or ROOT not in source.resolve().parents:
            raise ValueError(f"Missing or unsafe source file: {source.relative_to(ROOT)}")
        if any(part in {"__pycache__", ".venv", "venv", "dist", "build"} for part in source.relative_to(ROOT).parts):
            raise ValueError(f"Generated files cannot be packaged: {source.relative_to(ROOT)}")

    output_directory = ROOT / "dist"
    output_directory.mkdir(exist_ok=True)
    destination = output_directory / f"ConsoleLauncher-{version}-source.zip"
    temporary = destination.with_suffix(".zip.tmp")
    archive_root = f"ConsoleLauncher-{version}"
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for source in sorted(sources, key=lambda path: path.relative_to(ROOT).as_posix()):
                relative = source.relative_to(ROOT).as_posix()
                info = zipfile.ZipInfo(f"{archive_root}/{relative}", date_time=(2026, 11, 10, 0, 0, 0))
                info.create_system = 3
                info.external_attr = (0o100755 if source.suffix in {".sh", ".command"} else 0o100644) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, source.read_bytes())
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"Created {destination} ({len(sources)} source files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
