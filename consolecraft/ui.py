"""Small terminal presentation layer, usable with redirected output too."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn, TimeElapsedColumn
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from . import CREDIT, DISCORD_URL, __version__


console = Console(theme=Theme({"brand": "bold bright_magenta", "muted": "dim", "good": "bright_green", "bad": "bold bright_red"}))


def banner() -> None:
    title = Text()
    title.append("    ______\n", style="brand")
    title.append("   /_____/|    ", style="brand")
    title.append("C O N S O L E  L A U N C H E R\n", style="bold bright_magenta")
    title.append("  |     | |    ", style="brand")
    title.append(f"MINECRAFT JAVA / TERMINAL  v{__version__}\n", style="bold")
    title.append("  |_____|/\n", style="brand")
    title.append(f"\n  {CREDIT}", style="cyan")
    console.print(Panel(title, border_style="bright_magenta", padding=(1, 1)))
    console.print(Text("Discord: " + DISCORD_URL, style="muted"))


def select(question, choices, default=None):
    """Return a choice using arrow keys in a terminal; allow pipes and tests."""
    import sys
    if sys.stdin and sys.stdout and sys.stdin.isatty() and sys.stdout.isatty():
        import questionary
        options = [questionary.Choice(title, value=value) for title, value in choices]
        answer = questionary.select(question, choices=options, default=default,
                                   instruction="↑/↓ — выбрать · Enter — открыть · Ctrl+C — назад",
                                   style=questionary.Style([
                                       ("pointer", "fg:#d9b3ff bold"),
                                       ("highlighted", "fg:#d9b3ff bold"),
                                       ("selected", "fg:#d9b3ff"),
                                   ])).ask()
        if answer is None:
            raise KeyboardInterrupt
        return answer
    from rich.prompt import Prompt
    for title, value in choices:
        console.print(f"{value}. {title}")
    return Prompt.ask(question, choices=[str(value) for _, value in choices],
                      default=str(default) if default is not None else None, console=console)


def prompt_choice(question, choices, default=None, console=None):
    names = {"memory": "Оперативная память (МБ)", "java": "Путь к Java", "username": "Никнейм",
             "version": "Версия Minecraft", "client-id": "Microsoft Client ID", "profile": "Профиль по умолчанию",
             "download-timeout": "Таймаут загрузки (секунды)", "download-workers": "Количество загрузок",
             "auto-updates": "Проверка обновлений (on/off)",
             "discord-app-id": "Discord Rich Presence: Application ID",
             "mod": "Моды", "modpack": "Модпаки", "shader": "Шейдеры", "resourcepack": "Ресурс-паки",
             "back": "На главный экран", "list": "Список", "show": "Подробнее", "add": "Добавить",
             "play": "Играть", "remove": "Удалить", "delete": "Удалить", "enable": "Включить", "disable": "Выключить",
             "check": "Проверить совместимость", "updates": "Обновления", "create": "Создать",
             "edit": "Изменить", "default": "Выбрать по умолчанию", "select": "Выбрать",
             "copy": "Копировать", "rename": "Переименовать", "export": "Экспортировать",
             "import": "Импортировать", "status": "Состояние", "prepare": "Подготовить",
             "offline": "Офлайн", "demo": "Демо", "microsoft": "Microsoft", "logout": "Выйти из аккаунта"}
    options = [(names.get(str(value), str(value)), str(value)) for value in choices]
    extra_back = 'back' not in choices
    if extra_back:
        options.append(('На главный экран', 'back'))
    result = select(question, options, default)
    if extra_back and result == 'back':
        raise MenuBack()
    return result


class MenuBack(Exception):
    """Return from any submenu to the main menu."""


def input_text(question, *args, **kwargs):
    from rich.prompt import Prompt
    return Prompt.ask(f"{question} (Ctrl+C — главное меню)", *args, **kwargs)


def confirm(question, *args, **kwargs):
    from rich.prompt import Confirm
    return Confirm.ask(f"{question} (Ctrl+C — главное меню)", *args, **kwargs)


def success(message: str) -> None:
    console.print(Text("✓ " + message, style="good"))


def error(message: str) -> None:
    console.print(Text("Ошибка: " + message, style="bad"))


def info(message: str) -> None:
    console.print(Text(message))


def versions_table(versions: list[dict], selected: str = "") -> None:
    table = Table(title="Minecraft Java Edition", border_style="bright_magenta")
    table.add_column("Версия", style="bold", no_wrap=True)
    table.add_column("Тип")
    table.add_column("Установлена")
    table.add_column("Выбрана")
    for version in versions:
        version_id = str(version.get("id", "?"))
        table.add_row(
            Text(version_id),
            Text(str(version.get("type", "unknown"))),
            "да" if version.get("installed", False) else "—",
            "●" if version_id == selected else "",
        )
    console.print(table)


def settings_table(rows: list[tuple[str, str]], title: str = "Настройки") -> None:
    table = Table(title=title, border_style="green", show_header=False)
    table.add_column("Параметр", style="cyan")
    table.add_column("Значение", overflow="fold")
    for label, value in rows:
        table.add_row(Text(label), Text(str(value)))
    console.print(table)


@contextmanager
def download_progress() -> Iterator[dict]:
    from .cancellation import cancellable_download, check_cancelled
    if console.is_terminal:
        info("Ctrl+C — отменить загрузку и вернуться в меню.")
    with cancellable_download(lambda: info("\nОтменяем загрузку, завершаем соединения…")), Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=False,
        disable=not console.is_terminal,
    ) as progress:
        task = progress.add_task("Подготовка загрузки…", total=None)

        def status(value: str) -> None:
            check_cancelled()
            progress.update(task, description=Text(str(value)))

        def maximum(value: int) -> None:
            check_cancelled()
            progress.update(task, total=max(1, int(value)), completed=0)

        def advance(value: int) -> None:
            check_cancelled()
            progress.update(task, completed=int(value))

        yield {"setStatus": status, "setMax": maximum, "setProgress": advance}
