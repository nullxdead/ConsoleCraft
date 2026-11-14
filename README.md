# ConsoleLauncher 4.4.0

Терминальный лаунчер Minecraft Java Edition для Windows, macOS и Linux. **Creators: 0xdead, Frosty, Qwertyks**

[Релизы](https://github.com/nullxdead/ConsoleLauncher/releases) · [Discord](https://discord.gg/FtqBFt58w) · [Telegram](https://t.me/CCLauncher)

## Быстрая установка

Windows (PowerShell):

```powershell
irm https://raw.githubusercontent.com/nullxdead/ConsoleLauncher/v4.4.0/scripts/install.ps1 | iex
```

macOS Apple Silicon и Linux x86_64:

```sh
curl -fsSL https://raw.githubusercontent.com/nullxdead/ConsoleLauncher/v4.4.0/scripts/install.sh | sh
```

Установщик скачивает готовую сборку, проверяет SHA-256 и устанавливает команду в пользовательский каталог. Потребуются интернет, а на macOS/Linux — `curl`, `unzip` и `shasum` или `sha256sum`. На других архитектурах используйте исходники и Python 3.11+. После установки откройте новый терминал и запустите:

```sh
consolelauncher
```

Меню управляется стрелками ↑/↓ и клавишей Enter. В каждом подменю есть пункт «На главный экран»; при вводе текста и подтверждениях нажмите Ctrl+C. Во время загрузки Ctrl+C отменяет операцию; завершение текущих соединений может занять время до сетевого таймаута. Ввод имени, адреса сервера и других значений выполняется с клавиатуры. Консольные команды также доступны напрямую: `consolelauncher --help`, `consolelauncher play`, `consolelauncher profiles`, `consolelauncher mods`. При перенаправленном вводе меню принимает номера — это обеспечивает работу скриптов.

Лаунчер использует фон, настроенный в терминале, и не меняет его цвет.

Раздел «Каталог Modrinth» позволяет найти и установить моды, модпаки, шейдеры и ресурс-паки. Модпаки устанавливаются в отдельные профили и выбираются автоматически; Minecraft и загрузчик при необходимости скачаются при нажатии «Играть». Необязательные компоненты модпаков пропускаются. Шейдеры и ресурс-паки нужно включить в настройках игры; для шейдеров может понадобиться Iris или OptiFine.

Вход Microsoft открывает системный браузер и возвращает вас в лаунчер после подтверждения. При первой настройке потребуется идентификатор зарегистрированного приложения; для него должны быть разрешены личные аккаунты Microsoft и адрес возврата `http://localhost`. Подробнее: [вход Microsoft](docs/microsoft-auth.md).

Discord Rich Presence работает для всех профилей, когда открыт настольный Discord и настроен идентификатор приложения через «Настройки» → «Discord Rich Presence: Application ID». Он показывает лаунчер или версию Minecraft, но не имя аккаунта или адрес сервера. Пустое значение отключает статус.

Для удаления в macOS/Linux:

```sh
curl -fsSL https://raw.githubusercontent.com/nullxdead/ConsoleLauncher/v4.4.0/scripts/install-cli.sh | sh -s -- --uninstall
```

Для удаления в Windows PowerShell:

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/nullxdead/ConsoleLauncher/v4.4.0/scripts/install-cli.ps1))) -Uninstall
```

При удалении команды настройки, аккаунты, профили, моды и игровые миры сохраняются. Если имя команды занято другой программой, установщик не будет её перезаписывать.

## Установка из исходников

Нужен Python 3.11+. Выполните `python -m pip install -e .` из папки проекта; затем запустите `consolelauncher`. Также доступны `start.bat` и `sh start.sh`, которые создают виртуальное окружение и устанавливают зависимости автоматически.

Сохранены Microsoft OAuth, офлайн-аккаунты, поддержка скинов Ely.by, Fabric, Forge, NeoForge, Quilt, Modrinth, профили, серверы, Java и настройки памяти. Игровые файлы и существующий каталог данных ConsoleCraft не перемещаются. Сборки не подписаны сертификатом разработчика.

## Лицензия

Новые изменения распространяются по условиям [LICENSE](LICENSE): исходный код доступен для просмотра. Права на версии до 4.1.0 включительно, ранее предоставленные по MIT, сохраняются; их условия находятся в [LICENSE-MIT-4.1.0](LICENSE-MIT-4.1.0). У сторонних компонентов собственные лицензии. GitHub позволяет делать форки публичных репозиториев в соответствии со своими условиями.
