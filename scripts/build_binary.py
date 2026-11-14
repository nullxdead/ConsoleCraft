"""Build the terminal-only executable on each supported operating system."""
from pathlib import Path
import os, platform, subprocess, sys, tempfile, tomllib, zipfile
ROOT = Path(__file__).resolve().parents[1]

def run(args, **kwargs):
    return subprocess.run(args, cwd=ROOT, check=True, timeout=300, **kwargs)

def main():
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    output = ROOT / "binary-dist" / "cli"
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--console",
         "--name", "consolelauncher", "--distpath", str(output), "--workpath", str(ROOT / "build" / "cli"),
         "--specpath", str(ROOT / "build"), "--collect-all", "minecraft_launcher_lib",
         "--collect-submodules", "keyring.backends", "--copy-metadata", "console-launcher",
         "--copy-metadata", "minecraft-launcher-lib", "--copy-metadata", "keyring",
         "--exclude-module", "PySide6", "--exclude-module", "PyQt6", "--exclude-module", "webview", str(ROOT / "launcher.py")])
    executable = output / ("consolelauncher.exe" if os.name == "nt" else "consolelauncher")
    with tempfile.TemporaryDirectory(prefix="consolelauncher-cli-") as temporary:
        base=[str(executable), "--home", str(Path(temporary) / "data")]
        run([str(executable), "--version"])
        run(base + ["config", "--auto-updates", "off"])
        result=run(base + ["cli"], input="0\n", text=True, encoding="utf-8", capture_output=True)
        assert "Главное меню" in result.stdout
    destination = ROOT / "dist" / f"ConsoleLauncher-{version}-{platform.system()}-{platform.machine()}.zip"
    destination.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(executable, executable.name)
        archive.write(ROOT / "scripts" / ("install-cli.ps1" if os.name == "nt" else "install-cli.sh"), "install-cli.ps1" if os.name == "nt" else "install-cli.sh")
        for name in ("README.md", "LICENSE", "LICENSE-MIT-4.1.0", "CHANGELOG.md"):
            archive.write(ROOT / name, name)
    print(f"Built: {destination}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
