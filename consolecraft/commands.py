"""Profile-aware commands and terminal workflows for Console Launcher 2."""
from __future__ import annotations

import argparse
from contextlib import nullcontext
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
import sys
import webbrowser
from uuid import uuid4

from rich.prompt import Confirm, IntPrompt, Prompt

from . import TELEGRAM_URL, DISCORD_URL, __version__
from . import ui
from .auth import Account, offline_account
from .accounts import AccountStore, AccountError
from .config import ConfigError
from .profiles import ProfileStore, ProfileError
from .loaders import LoaderService, LoaderError
from .elyby import ElyByService, ElyByError
from .mods import ModManager, ModError
from .modrinth import ModrinthClient, ModrinthError

LOADERS = ("vanilla", "fabric", "forge", "neoforge", "quilt")
ERRORS = (ProfileError, LoaderError, ElyByError, ModError, ModrinthError, AccountError)


def add_commands(commands, play, install, config, doctor):
    for command in (play, install):
        command.add_argument("--profile", help="ID или название игрового профиля")
    play.add_argument("--account", help="ID офлайн-аккаунта или microsoft")
    play.add_argument("--server", help="адрес совместимого сервера")
    play.add_argument("--port", type=int, default=25565)
    config.add_argument("--profile", help="выбрать профиль по умолчанию")
    config.add_argument("--download-timeout", type=int, help="таймаут сети в секундах")
    config.add_argument("--download-workers", type=int, help="параллельные загрузки Minecraft")
    config.add_argument("--auto-updates", choices=["on", "off"], help="проверять обновления лаунчера при открытии меню")
    config.add_argument("--discord-app-id", help="Discord Application ID; пустая строка отключает Rich Presence")
    doctor.add_argument("--services", action="store_true", help="проверить доступность сервисов по сети")
    commands.add_parser("telegram", help="открыть Telegram-канал Console Launcher")
    commands.add_parser("discord", help="открыть Discord-сервер Console Launcher")
    commands.add_parser("updates", help="проверить обновления лаунчера")
    servers = commands.add_parser("servers", help="сохранённые серверы")
    server_actions = servers.add_subparsers(dest="action")
    server_actions.add_parser("list")
    add_server = server_actions.add_parser("add")
    add_server.add_argument("name"); add_server.add_argument("host"); add_server.add_argument("--port", type=int, default=25565)
    for name in ("remove", "play"):
        item = server_actions.add_parser(name); item.add_argument("id")
    accounts = commands.add_parser("accounts", help="Microsoft и локальные аккаунты")
    account_actions = accounts.add_subparsers(dest="action", required=True)
    account_actions.add_parser("list")
    action = account_actions.add_parser("create"); action.add_argument("username")
    action = account_actions.add_parser("select"); action.add_argument("account")
    action = account_actions.add_parser("delete"); action.add_argument("account"); action.add_argument("--yes", action="store_true")

    profiles = commands.add_parser("profiles", help="независимые игровые профили")
    actions = profiles.add_subparsers(dest="action")
    profiles.set_defaults(action="list")
    actions.add_parser("list")
    show = actions.add_parser("show"); show.add_argument("profile", nargs="?")
    for name in ("create", "edit"):
        action = actions.add_parser(name)
        action.add_argument("name" if name == "create" else "profile")
        action.add_argument("--minecraft", dest="minecraft_version")
        action.add_argument("--loader", choices=LOADERS)
        action.add_argument("--loader-version")
        action.add_argument("--memory", dest="memory_mb", type=int)
        action.add_argument("--java", dest="java_path")
        if name == "create": action.add_argument("--game-directory")
        action.add_argument("--jvm-arg", dest="jvm_args", action="append")
        action.add_argument("--game-arg", dest="game_args", action="append")
        action.add_argument("--auth", dest="auth_mode", choices=["microsoft", "offline", "demo"])
        action.add_argument("--elyby", choices=["on", "off"])
    for name in ("copy", "rename"):
        action = actions.add_parser(name); action.add_argument("profile"); action.add_argument("name")
    action = actions.add_parser("delete"); action.add_argument("profile")
    action.add_argument("--yes", action="store_true", help="подтвердить удаление профиля")
    action.add_argument("--delete-worlds", action="store_true")
    action.add_argument("--confirm-worlds", action="store_true", help="отдельно подтвердить удаление миров")
    action = actions.add_parser("export"); action.add_argument("profile"); action.add_argument("destination", type=Path)
    action = actions.add_parser("import"); action.add_argument("source", type=Path); action.add_argument("--name")
    action = actions.add_parser("default"); action.add_argument("profile")

    loaders = commands.add_parser("loaders", help="Fabric / Forge / NeoForge / Quilt")
    actions = loaders.add_subparsers(dest="action", required=True)
    actions.add_parser("list")
    for name in ("minecraft", "versions", "install", "repair"):
        action = actions.add_parser(name)
        action.add_argument("loader", choices=LOADERS[1:])
        if name != "minecraft": action.add_argument("minecraft_version")
        if name in {"install", "repair"}:
            action.add_argument("--loader-version"); action.add_argument("--profile")
    action = actions.add_parser("verify"); action.add_argument("--profile")

    mods = commands.add_parser("mods", help="моды выбранного профиля")
    mods.add_argument("--profile")
    actions = mods.add_subparsers(dest="action")
    mods.set_defaults(action="list", query="")
    action = actions.add_parser("list"); action.add_argument("--query", default="")
    action = actions.add_parser("show"); action.add_argument("filename")
    action = actions.add_parser("add"); action.add_argument("source", type=Path)
    action.add_argument("--allow-incompatible", action="store_true")
    action = actions.add_parser("remove"); action.add_argument("filename"); action.add_argument("--yes", action="store_true")
    for name in ("enable", "disable"):
        action = actions.add_parser(name); action.add_argument("filename", nargs="?"); action.add_argument("--all", action="store_true")
    actions.add_parser("check")
    actions.add_parser("updates")

    modrinth = commands.add_parser("modrinth", help="поиск, установка и обновления Modrinth")
    modrinth.add_argument("--profile")
    actions = modrinth.add_subparsers(dest="action", required=True)
    action = actions.add_parser("search"); action.add_argument("query"); action.add_argument("--limit", type=int, default=20)
    for name in ("show", "versions", "install"):
        action = actions.add_parser(name); action.add_argument("project")
        if name == "install": action.add_argument("--version-id")
    action = actions.add_parser("update"); action.add_argument("--yes", action="store_true")
    skins = commands.add_parser("elyby", help="настройки скинов отдельно для профиля")
    skins.add_argument("action", choices=["status", "enable", "disable", "prepare"])
    skins.add_argument("--profile")


def profile_store(app):
    if not hasattr(app, "profiles"):
        app.profiles = ProfileStore(app.store)
        app.profiles.get()  # Migrate the legacy game before any new profile is created.
    return app.profiles


def current(app, selector=None):
    profile = profile_store(app).get(selector)
    app.settings = app.store.load()
    return profile


def confirm(message, supplied=False):
    if supplied:
        return True
    if not sys.stdin.isatty():
        raise ConfigError(message + " Повторите с явным подтверждением --yes.")
    return ui.confirm(message, default=False, console=ui.console)


def table(title, columns, rows):
    from rich.table import Table
    from rich.text import Text
    view = Table(title=title, border_style="bright_magenta")
    for column in columns: view.add_column(column, overflow="fold")
    for row in rows: view.add_row(*(Text(str(value)) for value in row))
    ui.console.print(view)


def show_profile(profile):
    ui.settings_table([(key, str(value)) for key, value in asdict(profile).items()], title=profile.name)


def loader_service(app):
    return LoaderService(app.store.game_directory, timeout=app.settings.download_timeout,
                         max_workers=app.settings.download_workers)


def install_profile(app, profile, version=None, repair=False, callback=None):
    selected = version or profile.minecraft_version
    with (nullcontext(callback) if callback is not None else ui.download_progress()) as callback:
        if profile.loader == "vanilla":
            installed = app.minecraft.install(selected, callback=callback, java_path=profile.java_path or None)
            resolved = installed
        else:
            resolved = app.minecraft.resolve_version(selected)
            service = loader_service(app)
            loader_version = profile.loader_version
            if not loader_version:
                versions = service.loader_versions(profile.loader, resolved, stable_only=True)
                if not versions: versions = service.loader_versions(profile.loader, resolved)
                if not versions: raise LoaderError("Нет совместимых версий модлоадера.")
                loader_version = versions[0]
            method = service.repair if repair else service.install
            installed = method(profile.loader, resolved, loader_version,
                               java_path=profile.java_path or None, callback=callback)
    fields = dict(minecraft_version=resolved, installed_version=installed)
    if profile.loader != "vanilla": fields["loader_version"] = loader_version
    profile = profile_store(app).update(profile.id, **fields)
    ui.success(f"Профиль {profile.name}: установлена версия {installed}.")
    return profile


def launch_profile(app, version=None, *, offline=False, demo=False, username=None, no_install=False, profile=None,
                   account=None, server=None, port=25565, progress_callback=None, on_output=None):
    selected_profile = current(app, profile)
    if account == "microsoft" and (offline or demo):
        raise ConfigError("Выберите Microsoft или явный offline/demo-режим, а не оба одновременно.")
    accounts = AccountStore(app.store)
    selection = accounts.select(account) if account else accounts.active()
    if selection.get("kind") == "microsoft" and accounts.path.exists() and not (offline or demo):
        selected_profile.auth_mode = "microsoft"
    if selection.get("kind") == "offline" and not demo:
        offline = True
        username = username or selection["username"]
    if account == "microsoft":
        offline = demo = False
        selected_profile.auth_mode = "microsoft"
    selected = version or selected_profile.minecraft_version
    if username and not (offline or demo or selected_profile.auth_mode in {"offline", "demo"}):
        raise ConfigError("Параметр --username используется вместе с --offline или --demo.")
    if no_install and selected in {"latest", "release", "snapshot", "latest-snapshot"}:
        raise ConfigError("Для --no-install укажите точный ID установленной версии Minecraft.")
    if no_install and selected_profile.loader != "vanilla" and selected != selected_profile.minecraft_version:
        raise ConfigError("Версия Minecraft не совпадает с установленным модлоадером профиля. Выполните установку.")
    mode = "demo" if demo else "offline" if offline else selected_profile.auth_mode
    if mode == "microsoft":
        account = app.auth.refresh()
        app.settings.account = account.public_dict()
    else:
        account = offline_account(username or app.settings.username)
    if selected_profile.elyby_enabled and mode != "offline":
        raise ConfigError("Ely.by разрешён только для явно выбранного offline-профиля. Microsoft-авторизация не подменяется.")
    if not no_install:
        selected_profile = install_profile(app, selected_profile, selected, callback=progress_callback)
    installed = selected_profile.installed_version if selected_profile.loader != "vanilla" else selected
    if not no_install: installed = selected_profile.installed_version
    if not installed:
        raise ConfigError("Сначала установите модлоадер для этого профиля.")
    extra_jvm = list(selected_profile.jvm_args)
    if selected_profile.elyby_enabled:
        ely = ElyByService(app.store.home / "elyby")
        public = ely.profile(account.username)
        account = Account(public.get("name", account.username), public["id"].replace("-", ""), "0", "offline")
        with ui.download_progress() as callback:
            extra_jvm.extend(ely.jvm_arguments(mode, True, callback=callback))
        ui.info("Скины Ely.by: видимость зависит от режима, версии Minecraft и поддержки сервера.")
    for warning in ModManager(Path(selected_profile.game_directory)).diagnostics(selected_profile.minecraft_version, selected_profile.loader, loader_version=selected_profile.loader_version):
        ui.info("Моды: " + str(warning))
    app.store.save(app.settings)
    app.store.logs_directory.mkdir(parents=True, exist_ok=True)
    log = app.store.logs_directory / f"game-{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:6]}.log"
    ui.info(f"Запуск {selected_profile.name} · {installed} · {account.username} · {mode}")
    profile_store(app).mark_launched(selected_profile.id)
    app.presence.set("Играет в Minecraft", f"Версия {installed}")
    try:
        result = app.minecraft.launch(
            installed, account.username, account.uuid, account.access_token,
            java_path=selected_profile.java_path or None, memory_mb=selected_profile.memory_mb,
            demo=mode == "demo", offline=mode != "microsoft", download_java=not no_install,
            client_id=app.auth.client_id if mode == "microsoft" else "", xuid=account.xuid,
            game_directory=Path(selected_profile.game_directory), jvm_args=extra_jvm,
            game_args=selected_profile.game_args, log_file=log,
            server=server, port=port,
            on_output=on_output or (lambda line: ui.console.print(line.rstrip("\r\n"), markup=False, highlight=False)),
        )
    finally:
        app.presence.set("В лаунчере")
    if result == 0: ui.success("Игра завершена.")
    else: ui.error(f"Игра завершилась с кодом {result}. Лог: {log}")
    return 0 if result == 0 else 1


def execute(app, args):
    command, action = args.command, getattr(args, "action", None)
    if command == "servers":
        from .servers import ServerStore
        store = ServerStore(app.store.home)
        if action in (None, "list"):
            table("Серверы", ["ID", "Название", "Адрес", "Порт"], [(s["id"], s["name"], s["host"], s["port"]) for s in store.list()])
        elif action == "add":
            store.save({"name": args.name, "host": args.host, "port": args.port}); ui.success("Сервер сохранён.")
        else:
            matches = [s for s in store.list() if s["id"] == args.id or s["name"].casefold() == args.id.casefold()]
            if len(matches) != 1: raise ConfigError("Сервер не найден или название неоднозначно.")
            if action == "remove": store.delete(matches[0]["id"]); ui.success("Сервер удалён.")
            else: return app.play(server=matches[0]["host"], port=matches[0]["port"])
        return 0
    if command == "accounts":
        store = AccountStore(app.store)
        if action == "list":
            table("Аккаунты", ["ID", "Ник", "Тип"], [("microsoft", app.settings.account.get("username", "Microsoft"), "Microsoft")] + [(a["id"], a["username"], "Офлайн") for a in store.list()])
            ui.info("Выбран: " + str(store.active().get("username", "Microsoft")))
        elif action == "create":
            value = store.create(args.username); store.select(value["id"]); ui.success("Офлайн-аккаунт создан и выбран: " + value["username"])
        elif action == "select": store.select(args.account); ui.success("Аккаунт выбран.")
        elif action == "delete" and confirm("Удалить офлайн-аккаунт? Миры и профили сохранятся.", args.yes):
            store.remove(args.account, confirmed=True); ui.success("Аккаунт удалён.")
        return 0
    if command == "telegram":
        if not webbrowser.open(TELEGRAM_URL): ui.info("Откройте " + TELEGRAM_URL)
        return 0
    if command == "discord":
        if not webbrowser.open(DISCORD_URL): ui.info("Откройте " + DISCORD_URL)
        return 0
    if command == "updates":
        check_updates(app); return 0
    if command == "profiles":
        store = profile_store(app)
        if action == "list":
            table("Игровые профили", ["ID", "Название", "Minecraft", "Загрузчик", "Режим"],
                  [(p.id, p.name, p.minecraft_version, p.loader, p.auth_mode) for p in store.list()])
        elif action == "show": show_profile(store.get(args.profile))
        elif action in {"create", "edit"}:
            fields = {name: getattr(args, name) for name in ("minecraft_version", "loader", "loader_version", "memory_mb", "java_path", "jvm_args", "game_args", "auth_mode") if getattr(args, name) is not None}
            if getattr(args, "game_directory", None): fields["game_directory"] = str(Path(args.game_directory).expanduser().resolve())
            if action == "edit" and any(key in fields for key in ("minecraft_version", "loader", "loader_version")):
                fields["installed_version"] = ""
            if args.elyby is not None: fields["elyby_enabled"] = args.elyby == "on"
            value = store.create(args.name, **fields) if action == "create" else store.update(args.profile, **fields)
            show_profile(value)
        elif action == "copy": show_profile(store.copy(args.profile, args.name))
        elif action == "rename": show_profile(store.rename(args.profile, args.name))
        elif action == "default":
            store.set_default(args.profile); app.settings = app.store.load(); ui.success("Профиль по умолчанию выбран.")
        elif action == "delete":
            if confirm("Удалить запись профиля? Игровые файлы и миры сохранятся; их удаление требует отдельного подтверждения.", args.yes):
                worlds_confirmed = args.confirm_worlds
                if args.delete_worlds and not worlds_confirmed:
                    worlds_confirmed = confirm("Отдельно подтверждаете удаление миров?", False)
                store.delete(args.profile, confirmed=True, delete_worlds=args.delete_worlds, confirm_worlds=worlds_confirmed)
                app.settings = app.store.load(); ui.success("Профиль удалён.")
        elif action == "export":
            result = store.export_profile(args.profile, args.destination); ui.success(f"Профиль экспортирован: {result}")
        elif action == "import": show_profile(store.import_profile(args.source, name=args.name))
        return 0
    if command == "loaders":
        service = loader_service(app)
        if action == "list": ui.info("Fabric · Forge · NeoForge · Quilt")
        elif action in {"minecraft", "versions"}:
            values = service.minecraft_versions(args.loader) if action == "minecraft" else service.loader_versions(args.loader, args.minecraft_version)
            table(args.loader, ["Версия"], [(value,) for value in values])
        elif action == "verify":
            p = current(app, args.profile)
            issues = service.verify(p.installed_version or p.minecraft_version)
            for issue in issues: ui.info(str(issue))
            if not issues: ui.success("Целостность установки подтверждена.")
            return 1 if issues else 0
        else:
            p = current(app, args.profile)
            p = profile_store(app).update(p.id, loader=args.loader, minecraft_version=args.minecraft_version, loader_version=args.loader_version or "", installed_version="")
            install_profile(app, p, repair=action == "repair")
        return 0
    if command in {"mods", "modrinth", "elyby"}:
        p = current(app, args.profile)
        manager = ModManager(Path(p.game_directory))
        client = ModrinthClient(timeout=app.settings.download_timeout)
        catalog_mc = "" if p.minecraft_version in {"latest", "release", "snapshot", "latest-snapshot"} else p.minecraft_version
        catalog_loader = "" if p.loader == "vanilla" else p.loader
        if command == "elyby":
            if action in {"enable", "disable"}:
                if action == "enable" and p.auth_mode != "offline":
                    raise ConfigError("Для Ely.by сначала выберите offline: profiles edit PROFILE --auth offline")
                p = profile_store(app).update(p.id, elyby_enabled=action == "enable")
            if action == "prepare":
                with ui.download_progress() as callback: ElyByService(app.store.home / "elyby").prepare(callback=callback)
                ui.success("Компонент Ely.by проверен и установлен.")
            else: ui.settings_table([("Профиль", p.name), ("Ely.by", "включён" if p.elyby_enabled else "выключен"), ("Режим", p.auth_mode)])
            return 0
        if command == "mods":
            if action == "show":
                found = [m for m in manager.list_mods() if m.filename == args.filename]
                if not found: raise ModError("Мод не найден в выбранном профиле.")
                ui.settings_table([(key, str(value)) for key, value in found[0].public_dict().items()], title="Метаданные мода")
            elif action == "list":
                table("Моды: " + p.name, ["Файл", "Название", "Версия", "Загрузчик", "Включён", "Ошибки"],
                      [(m.filename, m.name, m.version, ", ".join(m.loaders), m.enabled, m.error or "") for m in manager.list_mods(args.query)])
            elif action == "add":
                ui.info("Моды выполняют произвольный код при запуске игры. Используйте доверенные источники.")
                result = manager.install_local(args.source, minecraft_version=p.minecraft_version, loader=p.loader,
                                               loader_version=p.loader_version, allow_incompatible=args.allow_incompatible)
                ui.success(f"Установлен {result.filename}")
            elif action == "remove":
                if confirm(f"Удалить мод {args.filename}?", args.yes): manager.remove(args.filename, confirmed=True); ui.success("Мод удалён.")
            elif action in {"enable", "disable"}:
                if args.all: manager.set_all_enabled(action == "enable")
                elif args.filename: manager.set_enabled(args.filename, action == "enable")
                else: raise ConfigError("Укажите файл мода или --all.")
                ui.success("Состояние модов обновлено.")
            elif action == "check":
                issues = manager.diagnostics(p.minecraft_version, p.loader)
                for issue in issues: ui.info(str(issue))
                if not issues: ui.success("По доступным метаданным проблем не найдено.")
                return 1 if issues else 0
            elif action == "updates": show_mod_updates(client, p)
        elif action == "search":
            results = client.search(args.query, minecraft_version=catalog_mc, loader=catalog_loader, limit=args.limit)
            if isinstance(results, dict): results = results.get("hits", [])
            table("Modrinth", ["ID", "Название", "Автор", "Описание"], [(m.get("slug", m.get("project_id")), m.get("title"), m.get("author", ""), m.get("description", "")) for m in results])
        elif action == "show":
            value = client.project(args.project)
            ui.settings_table([(key, str(value.get(key, ""))) for key in ("title", "description", "authors", "license", "source_url", "body")], title="Modrinth")
        elif action == "versions":
            versions = client.versions(args.project, minecraft_version=catalog_mc, loader=catalog_loader)
            table("Версии мода", ["ID", "Название", "Номер", "Тип"], [(v["id"], v["name"], v["version_number"], v["version_type"]) for v in versions])
        elif action == "install":
            if p.loader == "vanilla":
                raise ConfigError("Для установки модов выберите профиль Fabric, Forge, NeoForge или Quilt в разделе «Игровые профили».")
            if not catalog_mc:
                p = profile_store(app).update(p.id, minecraft_version=app.minecraft.resolve_version(p.minecraft_version))
            ui.info("Моды выполняют произвольный код при запуске игры. Используйте доверенные источники.")
            with ui.download_progress() as callback:
                installed = client.install(args.project, Path(p.game_directory), p.minecraft_version, p.loader,
                                           version_id=args.version_id, callback=callback, loader_version=p.loader_version)
            for mod in installed: ui.success("Установлен " + mod.filename)
        elif action == "update":
            if confirm("Обновить моды и удалить заменяемые файлы старых версий?", args.yes):
                with ui.download_progress() as callback:
                    result = client.update(Path(p.game_directory), p.minecraft_version, p.loader, confirmed=True,
                                           callback=callback, loader_version=p.loader_version)
                ui.success(f"Обновление завершено: {len(result)}")
        return 0
    return None


def show_mod_updates(client, profile):
    updates = client.check_updates(Path(profile.game_directory), profile.minecraft_version, profile.loader)
    ui.settings_table([(str(index + 1), str(value)) for index, value in enumerate(updates)], title="Обновления модов")


def check_updates(app, quiet=False):
    import requests
    try:
        timeout = (3, min(5, app.settings.download_timeout)) if quiet else (10, app.settings.download_timeout)
        response = requests.get("https://api.github.com/repos/nullxdead/ConsoleLauncher/releases/latest", timeout=timeout, headers={"User-Agent": "ConsoleLauncher/" + __version__})
        response.raise_for_status()
        release = response.json()
        latest = str(release.get("tag_name", "")).lstrip("v")
        if tuple(int(x) for x in latest.split(".")) > tuple(int(x) for x in __version__.split(".")):
            ui.info(f"Доступен Console Launcher {latest}: https://github.com/nullxdead/ConsoleLauncher/releases/latest")
        elif not quiet: ui.info("Установлена актуальная версия Console Launcher.")
    except (requests.RequestException, ValueError, TypeError, AttributeError):
        if not quiet: raise ConfigError("Не удалось проверить обновления Console Launcher.") from None


def service_status(app):
    import requests
    urls = {"Microsoft OAuth": "https://login.microsoftonline.com/consumers/v2.0/.well-known/openid-configuration", "Minecraft": "https://launchermeta.mojang.com/mc/game/version_manifest_v2.json", "Modrinth": "https://api.modrinth.com/v2/", "Ely.by": "https://authserver.ely.by/"}
    rows = []
    for name, url in urls.items():
        try:
            result = requests.get(url, timeout=(5, app.settings.download_timeout), headers={"User-Agent": "Console Launcher/" + __version__})
            rows.append((name, f"HTTP {result.status_code}"))
        except requests.RequestException: rows.append((name, "нет соединения"))
    ui.settings_table(rows, title="Доступность сервисов (не проверка разрешений аккаунта)")


def catalog_menu(app):
    from .catalog import KINDS, install_modpack, install_pack
    kind = ui.prompt_choice("Каталог Modrinth", choices=list(KINDS), console=ui.console)
    profile = current(app)
    query = ui.input_text("Название (пусто — популярные проекты)", default="", console=ui.console)
    client = ModrinthClient(timeout=app.settings.download_timeout)
    mc = "" if profile.minecraft_version in {"latest", "release", "snapshot", "latest-snapshot"} else profile.minecraft_version
    loader = profile.loader if kind == "mod" and profile.loader != "vanilla" else ""
    results = client.search(query, minecraft_version=mc if kind != "modpack" else "", loader=loader, project_type=kind)
    choices = [(f"{item.get('title', item.get('slug', 'Проект'))} — {item.get('author', '')}", item.get("slug") or item.get("project_id"))
               for item in results if item.get("slug") or item.get("project_id")]
    if not choices:
        ui.info("Проекты не найдены. Попробуйте другой запрос или профиль.")
        return
    choices.append(("На главный экран", "back"))
    project = ui.select(KINDS[kind], choices)
    if project == "back":
        return
    if kind == "mod":
        from .cli import make_parser
        return app.run_command(make_parser().parse_args(["modrinth", "install", project]))
    with ui.download_progress() as callback:
        if kind == "modpack":
            ui.info("Модпак устанавливается в отдельный профиль. Необязательные компоненты пропускаются.")
            installed = install_modpack(client, project, profile_store(app), callback)
        else:
            installed = install_pack(client, kind, project, profile, callback)
    if kind == "modpack":
        ui.success(f"Создан и выбран профиль {installed.name}. Нажмите «Играть», чтобы установить Minecraft и загрузчик.")
    else:
        ui.success(f"Установлен {installed.name} в профиль {profile.name}.")
        if kind == "shader":
            ui.info("Включите шейдер в игре; для него может понадобиться Iris или OptiFine.")
        else:
            ui.info("Включите ресурс-пак в настройках Minecraft.")


def menu_action(app, choice):
    def run(*parts):
        from .cli import make_parser
        return app.run_command(make_parser().parse_args(list(parts)))
    if choice == "1": return app.play()
    if choice == "2": return app.login()
    if choice == "3": return app.versions(snapshots=ui.confirm("Показать снапшоты?", default=False, console=ui.console))
    if choice == "4":
        p = current(app); return install_profile(app, p, ui.input_text("Minecraft", default=p.minecraft_version, console=ui.console))
    if choice == "5":
        loader = ui.prompt_choice("Модлоадер", choices=list(LOADERS[1:]), console=ui.console)
        mc = ui.input_text("Версия Minecraft", default=current(app).minecraft_version, console=ui.console)
        values = loader_service(app).loader_versions(loader, mc)
        table(loader, ["Версия"], [(v,) for v in values[:50]])
        version = ui.input_text("Версия модлоадера", default=values[0] if values else "", console=ui.console)
        return run("loaders", "install", loader, mc, "--loader-version", version)
    if choice == "6":
        action = ui.prompt_choice("Моды", choices=["list", "show", "add", "remove", "enable", "disable", "check", "updates"], default="list", console=ui.console)
        return run("mods", action, *([ui.input_text("Путь к JAR" if action == "add" else "Имя файла", console=ui.console)] if action in {"show", "add", "remove", "enable", "disable"} else []))
    if choice == "7":
        return catalog_menu(app)
    if choice == "8":
        run("profiles", "list")
        action = ui.prompt_choice("Профили", choices=["create", "show", "edit", "default", "copy", "rename", "delete", "export", "import", "back"], default="back", console=ui.console)
        if action == "back": return
        target = ui.input_text("Название нового профиля" if action == "create" else "Путь к ZIP" if action == "import" else "ID/название профиля", console=ui.console)
        if action == "create":
            mc = ui.input_text("Minecraft", default="latest", console=ui.console)
            mode = ui.prompt_choice("Режим", choices=["microsoft", "offline", "demo"], default="microsoft", console=ui.console)
            return run("profiles", action, target, "--minecraft", mc, "--auth", mode)
        if action == "edit":
            field = ui.prompt_choice("Поле", choices=["minecraft", "loader", "loader-version", "memory", "java", "auth", "elyby", "jvm-arg", "game-arg"], console=ui.console)
            value = ui.input_text("Значение", console=ui.console)
            return run("profiles", "edit", target, f"--{field}={value}")
        tail = [ui.input_text("Новое имя" if action in {"copy", "rename"} else "Путь к ZIP", console=ui.console)] if action in {"copy", "rename", "export"} else []
        return run("profiles", action, target, *tail)
    if choice == "9": return run("elyby", ui.prompt_choice("Ely.by", choices=["status", "enable", "disable", "prepare"], default="status", console=ui.console))
    if choice == "10": return app.edit_settings()
    if choice == "11":
        app.doctor()
        if ui.confirm("Проверить доступность сервисов?", default=False, console=ui.console): service_status(app)
    if choice == "12": return run("telegram")
    if choice == "13": return run("about")
    if choice == "14":
        run("servers", "list")
        action = ui.prompt_choice("Серверы", choices=["add", "play", "remove", "back"], default="back")
        if action == "back": return
        name = ui.input_text("Название сервера", console=ui.console)
        if action == "add":
            host = ui.input_text("IP или домен", console=ui.console)
            port = IntPrompt.ask("Порт (Ctrl+C — главное меню)", default=25565, console=ui.console)
            return run("servers", "add", name, host, "--port", str(port))
        if action == "remove" and not ui.confirm("Удалить сервер из списка?", default=False, console=ui.console): return
        return run("servers", action, name)
    if choice == "16": return run("discord")
    if choice == "17": return run("updates")
    if choice == "15":
        run("accounts", "list")
        action = ui.prompt_choice("Аккаунты", choices=["create", "select", "delete", "back"], default="back", console=ui.console)
        if action != "back":
            value = ui.input_text("Никнейм" if action == "create" else "ID аккаунта (или microsoft)", console=ui.console)
            return run("accounts", action, value)
