"""Interactive terminal menu and scriptable command-line interface."""

from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import getpass
from importlib.metadata import version as package_version
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

import requests
from rich.prompt import Prompt

from . import APP_NAME, CREDIT, TELEGRAM_URL, DISCORD_URL, __version__
from .auth import AuthError, AuthManager, DeviceCode
from .config import ConfigError, ConfigStore, DEFAULT_CLIENT_ID, validate_client_id
from .java import JavaError
from .minecraft import MinecraftError, MinecraftService
from .discord_rpc import DiscordPresence
from . import ui
from . import commands as advanced


def _limit(value: str) -> int:
    try:
        count = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("ожидается целое число") from exc
    if count < 0:
        raise argparse.ArgumentTypeError("число должно быть >= 0 (0 = все)")
    return count


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="consolelauncher", description="Console Launcher — терминальный лаунчер Minecraft Java Edition.",
        epilog=CREDIT,
    )
    parser.add_argument("--version", "-V", action="version", version=f"{APP_NAME} {__version__} | {CREDIT}")
    parser.add_argument("--home", type=Path, help="отдельная папка настроек, игры и логов")
    commands = parser.add_subparsers(dest="command", title="команды")
    commands.add_parser("cli", aliases=["menu"], help="открыть консольное меню в текущем терминале")
    versions = commands.add_parser("versions", help="список версий Minecraft")
    versions.add_argument("--snapshots", action="store_true", help="включая снапшоты и старые тестовые версии")
    versions.add_argument("--installed", action="store_true", help="только локальные версии, без сети")
    versions.add_argument("--limit", type=_limit, default=30, help="количество строк; 0 = все (по умолчанию 30)")
    install = commands.add_parser("install", help="скачать или восстановить версию и Java")
    install.add_argument("version", nargs="?", default="latest", help="ID версии или latest / snapshot")
    play = commands.add_parser("play", help="установить при необходимости и запустить игру")
    play.add_argument("version", nargs="?", help="ID версии; по умолчанию выбранная в настройках")
    modes = play.add_mutually_exclusive_group()
    modes.add_argument("--offline", action="store_true", help="локальная одиночная игра без Microsoft")
    modes.add_argument("--demo", action="store_true", help="официальный ограниченный деморежим")
    play.add_argument("--username", help="ник для offline/demo (3–16: A–Z, a–z, 0–9, _)")
    play.add_argument("--no-install", action="store_true", help="не проверять загрузки; требуется точный ID установленной версии")
    login = commands.add_parser("login", help="войти в Microsoft через браузер")
    login.add_argument("--client-id", help="идентификатор приложения Microsoft (лучше ввести в настройках без отображения)")
    login.add_argument("--device-code", action="store_true", help="запасной способ входа по коду устройства")
    commands.add_parser("logout", help="удалить сохранённые данные входа")
    config = commands.add_parser("config", help="просмотреть или изменить настройки")
    config.add_argument("--memory", type=int, metavar="MB", help="RAM для Java, 512–65536 МБ")
    config.add_argument("--java", metavar="PATH", help="путь к java/java.exe; пустая строка = автоматически")
    config.add_argument("--username", help="ник для локальной игры")
    config.add_argument("--version", dest="game_version", help="выбранная версия Minecraft")
    config.add_argument("--client-id", help="идентификатор Microsoft; пустая строка = сброс")
    doctor = commands.add_parser("doctor", help="проверить среду и показать расположение файлов")
    commands.add_parser("about", help="авторы и сведения о проекте")
    advanced.add_commands(commands, play, install, config, doctor)
    return parser


class Application:
    def __init__(self, store: ConfigStore):
        self.store = store
        self.settings = store.load()
        if store.identifier_backup_warning:
            ui.info("Не удалось обновить старую резервную копию настроек; проверьте доступ к файлам.")
        if store.identifiers_temporary:
            ui.info("Системное хранилище недоступно: настройки приложений сохранятся только до закрытия лаунчера.")
        self.minecraft = MinecraftService(store.game_directory, timeout=self.settings.download_timeout,
                                          max_workers=self.settings.download_workers)
        self._auth: AuthManager | None = None
        self._auth_client_id: str | None = None
        self.presence = DiscordPresence(self.settings.discord_app_id)

    @property
    def auth(self) -> AuthManager:
        client_id = validate_client_id(os.environ.get("CONSOLECRAFT_CLIENT_ID") or self.settings.client_id or DEFAULT_CLIENT_ID)
        if self._auth is None or client_id != self._auth_client_id:
            namespace = hashlib.sha256(str(self.store.home).encode("utf-8")).hexdigest()[:24]
            self._auth = AuthManager(client_id, credential_namespace=namespace)
            self._auth_client_id = client_id
        return self._auth

    def show_config(self) -> None:
        profile = advanced.current(self)
        account_name = self.settings.account.get("username", "не выполнен")
        ui.settings_table([
            ("Профиль", f"{profile.name} ({profile.id})"),
            ("Minecraft / загрузчик профиля", f"{profile.minecraft_version} / {profile.loader} {profile.loader_version}"),
            ("RAM / Java профиля", f"{profile.memory_mb} МБ / {profile.java_path or 'автоматически'}"),
            ("Игровой каталог профиля", profile.game_directory),
            ("Загрузки", f"таймаут {self.settings.download_timeout} с · потоков {self.settings.download_workers}"),
            ("Проверка обновлений", str(self.settings.auto_check_updates)),
            ("Discord Rich Presence", "включён" if self.settings.discord_app_id else "выключен (нужен Application ID)"),
            ("Версия", self.settings.version),
            ("Ник offline", self.settings.username),
            ("RAM", f"{self.settings.memory_mb} МБ"),
            ("Java", self.settings.java_path or "автоматически (runtime Mojang / JAVA_HOME / PATH)"),
            ("Microsoft", account_name),
            ("Microsoft приложение", "задано" if self.settings.client_id else "не задано"),
            ("Конфигурация", str(self.store.path)),
            ("Файлы Minecraft", str(self.store.game_directory)),
        ])

    def configure(self, args: argparse.Namespace) -> int:
        if getattr(args, "profile", None):
            advanced.profile_store(self).set_default(args.profile)
            self.settings = self.store.load()
        self.settings = replace(self.settings)
        changed = False
        for argument, field in (
            ("memory", "memory_mb"), ("java", "java_path"), ("username", "username"),
            ("game_version", "version"), ("client_id", "client_id"),
            ("download_timeout", "download_timeout"), ("download_workers", "download_workers"),
            ("discord_app_id", "discord_app_id"),
        ):
            value = getattr(args, argument, None)
            if value is not None:
                setattr(self.settings, field, value)
                changed = True
        if getattr(args, "auto_updates", None) is not None:
            self.settings.auto_check_updates = args.auto_updates == "on"
            changed = True
        if changed:
            self.store.save(self.settings)
            if self.store.identifiers_temporary:
                ui.info("Системное хранилище недоступно: настройки приложений сохранятся только до закрытия лаунчера.")
            if getattr(args, "discord_app_id", None) is not None:
                self.presence.close()
                self.presence = DiscordPresence(self.settings.discord_app_id)
                self.presence.set("В лаунчере")
            self.minecraft.timeout = self.settings.download_timeout
            self.minecraft.max_workers = self.settings.download_workers
            profile = advanced.current(self)
            updates = {field: getattr(args, arg) for arg, field in (("memory", "memory_mb"), ("java", "java_path"), ("game_version", "minecraft_version")) if getattr(args, arg, None) is not None}
            if updates: advanced.profile_store(self).update(profile.id, **updates)
            ui.success("Настройки сохранены.")
        self.show_config()
        return 0

    def versions(self, *, snapshots: bool = False, installed: bool = False, limit: int = 30) -> int:
        available = self.minecraft.list_versions(include_snapshots=snapshots, installed_only=installed)
        installed_ids = {row["id"] for row in self.minecraft.installed_versions()}
        rows = [{**row, "installed": row["id"] in installed_ids} for row in available]
        ui.versions_table(rows[:limit] if limit else rows, self.settings.version)
        if self.minecraft.manifest_cached and not installed:
            ui.info("Сеть недоступна: показан сохранённый каталог версий.")
        if not rows:
            ui.info("Версий пока нет. Установите игру: consolelauncher install latest")
        elif limit and len(rows) > limit:
            ui.info(f"Показано {limit} из {len(rows)}. Все версии: versions --limit 0")
        return 0

    def install(self, version: str) -> str:
        ui.info(f"Установка Minecraft {version}. При первом запуске загрузка может занять несколько минут.")
        with ui.download_progress() as callback:
            resolved = self.minecraft.install(version, callback=callback, java_path=self.settings.java_path or None)
        self.settings.version = resolved
        self.store.save(self.settings)
        ui.success(f"Minecraft {resolved} установлен и выбран.")
        return resolved

    def _device_code(self, device: DeviceCode) -> None:
        ui.settings_table([
            ("Откройте в браузере", device.verification_uri),
            ("Код входа", device.user_code),
            ("Срок действия", f"{device.expires_in // 60} мин."),
        ], title="Вход Microsoft")
        ui.info("Введите код на странице Microsoft. Ожидание подтверждения… Ctrl+C — отмена.")

    def login(self, client_id: str | None = None, *, device_code: bool = False) -> int:
        if client_id is not None:
            self.settings.client_id = validate_client_id(client_id)
            self.store.save(self.settings)
        if not (os.environ.get("CONSOLECRAFT_CLIENT_ID") or self.settings.client_id):
            if not sys.stdin.isatty():
                raise AuthError("Для Microsoft-входа сначала задайте идентификатор приложения в настройках.")
            value = getpass.getpass("Идентификатор приложения Microsoft (ввод скрыт): ")
            self.settings.client_id = validate_client_id(value.strip())
            self.store.save(self.settings)
            if self.store.identifiers_temporary:
                ui.info("Системное хранилище недоступно: настройка сохранится только до закрытия лаунчера.")
        if device_code:
            account = self.auth.login(self._device_code)
        else:
            ui.info("Открывается Microsoft в браузере. Войдите в аккаунт и вернитесь в лаунчер. Ctrl+C — отмена.")
            account = self.auth.login_browser()
        from .accounts import AccountStore
        AccountStore(self.store).select("microsoft")
        self.settings.account = account.public_dict()
        self.store.save(self.settings)
        ui.success(f"Выполнен вход: {account.username}.")
        if self.auth.storage_warning:
            ui.info(self.auth.storage_warning)
        return 0

    def logout(self) -> int:
        self.auth.logout()
        self.settings.account = {}
        self.store.save(self.settings)
        ui.success("Данные входа Microsoft удалены.")
        return 0

    def play(self, version: str | None = None, *, offline: bool = False, demo: bool = False,
             username: str | None = None, no_install: bool = False, profile: str | None = None,
             account: str | None = None, server: str | None = None, port: int = 25565,
             progress_callback=None, on_output=None) -> int:
        return advanced.launch_profile(self, version, offline=offline, demo=demo, username=username,
                                       no_install=no_install, profile=profile, account=account, server=server, port=port,
                                       progress_callback=progress_callback, on_output=on_output)

    def doctor(self) -> int:
        java_path = self.settings.java_path or (
            str(Path(os.environ["JAVA_HOME"]) / "bin" / ("java.exe" if os.name == "nt" else "java"))
            if os.environ.get("JAVA_HOME") else shutil.which("java")
        )
        java_status = "не найдена в системе; будет загружена при установке, если runtime доступен для платформы"
        healthy = True
        if java_path:
            try:
                result = subprocess.run([java_path, "-version"], capture_output=True, text=True,
                                        errors="replace", timeout=10, check=False)
                output = (result.stderr or result.stdout).strip().splitlines()
                java_status = f"{java_path}\n" + (output[0] if output else f"код {result.returncode}")
                healthy = result.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                java_status = f"не удалось запустить: {java_path}"
                healthy = False
        status = self.auth.account_status()
        rows = [
            ("ОС", f"{platform.system()} {platform.release()} ({platform.machine()})"),
            ("Python", f"{platform.python_version()} · {sys.executable}"),
            ("minecraft-launcher-lib", package_version("minecraft-launcher-lib")),
            ("Java в системе", java_status),
            ("Microsoft", status.get("username") or "нет активного аккаунта"),
            ("Хранилище учётных данных", str(status.get("storage", "не доступно"))),
            ("Установлено версий", str(len(self.minecraft.installed_versions()))),
            ("Папка данных", str(self.store.home)),
            ("Логи игры", str(self.store.logs_directory)),
        ]
        ui.settings_table(rows, title="Диагностика Console Launcher")
        if os.environ.get("CONSOLECRAFT_CLIENT_ID"):
            ui.info("Client ID берётся из переменной CONSOLECRAFT_CLIENT_ID.")
        return 0 if healthy else 1

    def run_command(self, args: argparse.Namespace) -> int:
        result = advanced.execute(self, args)
        if result is not None: return result
        match args.command:
            case "versions":
                return self.versions(snapshots=args.snapshots, installed=args.installed, limit=args.limit)
            case "install":
                advanced.install_profile(self, advanced.current(self, args.profile), args.version)
                return 0
            case "play":
                return self.play(args.version, offline=args.offline, demo=args.demo,
                                 username=args.username, no_install=args.no_install, profile=args.profile,
                                 account=args.account, server=args.server, port=args.port)
            case "login":
                return self.login(args.client_id, device_code=args.device_code)
            case "logout":
                return self.logout()
            case "config":
                return self.configure(args)
            case "doctor":
                result = self.doctor()
                if args.services: advanced.service_status(self)
                return result
            case "about":
                ui.banner()
                ui.info("Windows · Linux · macOS | Python 3.11+ | Minecraft Java Edition")
                ui.info("Telegram-канал Console Launcher: " + TELEGRAM_URL)
                ui.info("Discord Console Launcher: " + DISCORD_URL)
                ui.info("Независимый проект. Не является официальным продуктом Mojang или Microsoft.")
                return 0
        return self.menu()

    def menu(self) -> int:
        self.presence.set("В лаунчере")
        ui.banner()
        profile = advanced.current(self)
        if self.settings.auto_check_updates:
            advanced.check_updates(self, quiet=True)
        labels = [("Играть", "1"), ("Войти через Microsoft", "2"), ("Версии Minecraft", "3"),
                  ("Установить Minecraft", "4"), ("Модлоадеры", "5"), ("Менеджер модов", "6"),
                  ("Каталог Modrinth: моды, модпаки, шейдеры", "7"), ("Игровые профили", "8"), ("Скины и Ely.by", "9"),
                  ("Настройки", "10"), ("Диагностика", "11"), ("Telegram", "12"), ("О программе", "13"),
                  ("Серверы", "14"), ("Аккаунты", "15"), ("Discord", "16"),
                  ("Проверить обновления", "17"), ("Выход", "0")]
        while True:
            profile = advanced.current(self)
            ui.info(f"\nПрофиль: {profile.name} · {profile.minecraft_version} · {profile.loader} · {profile.auth_mode}")
            try:
                choice = ui.select("Главное меню", labels, default="1")
                if choice == "0": return 0
                advanced.menu_action(self, choice)
            except ui.MenuBack:
                continue
            except SystemExit as exc:
                if exc.code not in (0, None):
                    ui.error("Некорректное значение. Проверьте ввод и попробуйте ещё раз.")
            except (ConfigError, AuthError, MinecraftError, JavaError) + advanced.ERRORS as exc:
                ui.error(str(exc))
            except requests.RequestException:
                ui.error("Ошибка соединения. Проверьте интернет и повторите действие.")
            except KeyboardInterrupt:
                ui.info("\nДействие отменено. Возврат в меню.")
            except EOFError:
                return 0

    def edit_settings(self) -> None:
        self.show_config()
        field = ui.prompt_choice("Настройка", choices=["memory", "java", "username", "version", "client-id", "profile", "download-timeout", "download-workers", "auto-updates", "discord-app-id", "logout", "back"], default="back", console=ui.console)
        if field == "back": return
        if field == "logout": self.logout(); return
        if field in {"client-id", "discord-app-id"}:
            value = getpass.getpass("Идентификатор приложения (ввод скрыт, пусто — отключить): ")
        else:
            value = ui.input_text("Значение", default="", console=ui.console)
        self.configure(make_parser().parse_args(["config", f"--{field}={value}"]))


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = make_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        args.command = "cli"
    try:
        app = Application(ConfigStore(args.home))
        try:
            return app.run_command(args)
        finally:
            app.presence.close()
    except (ConfigError, AuthError, MinecraftError, JavaError) + advanced.ERRORS as exc:
        ui.error(str(exc))
        return 1
    except requests.RequestException:
        ui.error("Не удалось соединиться с сервером. Проверьте интернет и повторите команду.")
        return 1
    except OSError as exc:
        ui.error(f"Ошибка доступа к файлу или запуска программы: {exc.strerror or type(exc).__name__}.")
        return 1
    except (KeyboardInterrupt, EOFError):
        ui.info("\nДействие отменено.")
        return 130
    except Exception as exc:
        # Do not dump tracebacks: third-party HTTP exceptions may contain credentials.
        ui.error(f"Не удалось выполнить действие ({type(exc).__name__}). Проверьте настройки и выполните doctor.")
        return 1
