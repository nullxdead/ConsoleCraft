# История изменений

## 4.4.1

- Если Microsoft явно отклоняет адрес возврата, лаунчер сообщает о настройке приложения вместо ожидания входа.

## 4.4.0

- Вход Microsoft через браузер с автоматическим возвратом в лаунчер.
- Обновлено хранение настроек приложений и устранено отображение идентификаторов в настройках.
- Сохранены игровой профиль, существующий вход, каталог дополнений и Discord Rich Presence.

## 4.3.1

- Убраны изменение цвета фона терминала и анимация; используется фон терминала пользователя.

## 4.3.0

- Фиолетовый фон со сменой оттенков и поддержкой классической консоли Windows.
- Каталог Modrinth: моды, модпаки, шейдеры и ресурс-паки с выбором стрелками.
- Модпаки устанавливаются в отдельные профили с проверкой загружаемых файлов; необязательные компоненты пропускаются.
- Глобальный Discord Rich Presence показывает лаунчер и запущенную версию Minecraft.

## 4.2.0

- Фиолетовое оформление в поддерживаемых терминалах с восстановлением цветов при выходе.
- Исправлены адрес проверки обновлений и выход лаунчера при неверном вводе настройки.
- Проверка обновлений доступна в главном меню; настройки получили понятные названия.
- Обновлены условия лицензии для новых изменений и имена создателей.

## 4.1.0

- Ctrl+C отменяет загрузку и возвращает в меню.
- Во всех подменю доступен возврат на главный экран.
- Незавершённые загрузки не заменяют готовые файлы.

## 4.0.1

- Исправлен запуск установщика в Windows PowerShell 5.1.

## 4.0.0

- Терминальный интерфейс и навигация стрелками.
- Автоматическая установка для Windows, macOS и Linux с проверкой SHA-256.
- Удалены Qt и графическая оболочка; данные и Minecraft backend сохранены.

# Changelog

## 3.0.0 — 2026-11-11

- ConsoleCraft переименован в **Console Launcher**; GitHub-адрес и каталоги пользовательских данных сохранены.
- Основной интерфейс заменён настоящим Qt Widgets GUI: зелёная тема, панорама, пиксельные элементы, профили, моды, аккаунты, серверы, загрузки и настройки.
- Браузерная оболочка и HTTP-панель удалены. Обычный запуск открывает GUI, консольное меню доступно через `consolelauncher cli`.
- Добавлены пользовательские PATH-установщики для команды `consolelauncher` и новые имена готовых пакетов.
- Системные иконки создаются из обновлённого пользовательского `logo.png`. Существующий backend Minecraft и сохранённые игровые данные используются без переноса.

## 2.2.0 — 2026-11-11

- Отдельное окно ConsoleCraft (`gui`, пункт 14); браузерный режим сохранён (`web`, пункт 16).
- Доступ с телефона по IP компьютера в LAN, показ полных ссылок; `--local-only` ограничивает доступ компьютером.
- Закрытие интерфейса с возвратом в CLI, heartbeat нескольких устройств и автоматическая остановка после ухода последнего клиента. Активные операции не прерываются.
- Светлая, тёмная и системная темы, компактные элементы подключения и закрытия.
- Официальный Discord: https://discord.gg/FtqBFt58w — интерфейс, меню, сведения о программе и документация.

## 2.1.1 — 2026-11-11

- macOS `.app` и Linux-ярлык запускают CLI; браузер и HTTP-сервер включаются явно.
- Реальные иконки Fabric/Quilt/Forge/NeoForge и Modrinth с безопасным кэшем.
- Компактный нейтральный интерфейс без декоративного hero и маркетинговых блоков.
- Спокойные короткие переходы, адаптивная компоновка и reduced motion.

## 2.1.0 — 2026-11-10

- Локальный тёмно-зелёный веб-интерфейс с настоящим запуском Minecraft, профилями, модами и настройками.
- Сохраняемые офлайн-аккаунты и стандартное подключение к допускающим их серверам.
- Исправлены Fabric-предикаты, включая `~26.3-`, и проверка версии загрузчика.
- Защита локальных операций ключом, Origin/Host, ограничение загрузок и безопасная обработка JAR.
- Фирменный logo.png, favicon, Windows ICO, macOS ICNS/.app и Linux desktop entry.
- Консольное управление, Microsoft и Ely.by сохранены.

## 2.0.0 — 2026-11-10

- Added the ConsoleCraft public Microsoft Client ID by default, advanced overrides and distinct OAuth, Xbox, Minecraft, entitlement and application-approval errors. Secure refresh-token storage and automatic session renewal remain in place.
- Added Fabric, Forge, NeoForge and Quilt catalogues, compatible version selection, official installation, integrity checks and managed-file repair.
- Added isolated profiles with their own loader/version, game folder, RAM, Java, arguments, authentication mode, Ely.by preference and timestamps; CRUD, copy, defaults and bounded portable import/export.
- Added configuration schema 2, safe schema 1 migration, a public-settings backup and preservation of existing worlds/mods in the legacy default profile.
- Added a JAR mod manager with search, enable/disable, bulk actions, safe metadata inspection, dependency/compatibility diagnostics and confirmed removal.
- Added Modrinth API v2 search, authors/descriptions/versions, checked downloads, supported required dependencies and confirmed hash-based updates.
- Added opt-in experimental Ely.by public profiles and verified authlib-injector for offline profiles, without replacing Microsoft authentication or license checks.
- Expanded the Russian Rich menu and CLI, download/network settings, service diagnostics and launcher-update checks.
- Added official Telegram https://t.me/CCLauncher to the menu, about, metadata and documentation.
- Added security tests and documentation for archives, paths, checksums, credentials and world-preserving operations.
- Added native build/smoke jobs for Windows, macOS and Linux alongside wheel, sdist and clean source ZIP packaging. Executable availability depends on successful platform builds.

Automated tests do not imply a live Microsoft login, full graphical launch with every loader or in-game Ely.by verification. Minecraft API approval and modpack compatibility remain external constraints.

## 1.0.0 — 2026-11-10

- Initial Minecraft Java terminal launcher with Russian Rich menu and CLI.
- Official version catalogue, vanilla installation, Mojang Java runtime support and RAM/Java settings.
- Microsoft device-code sign-in using a configured application ID, OS-backed credentials, refresh and logout.
- Offline/demo modes, game logs, source packaging and cross-platform bootstrap scripts.
- Windows checks included Minecraft 26.3/Java 25 installation and classpath validation; no real game-window or Microsoft account sign-in verification was claimed.

**Creators: 0xdead, Frosty, Qwertyks**

Official Telegram: https://t.me/CCLauncher

Official Discord: https://discord.gg/FtqBFt58w
