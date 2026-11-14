# Console Launcher 4.0.1 — исправление установщика Windows

Исправлены запуск установщика через `irm | iex` в Windows PowerShell 5.1 и определение 64-битной системы. Терминальное меню со стрелками и установщики macOS/Linux сохранены.

Инструкция установки и удаления — в README. Windows x64, macOS Apple Silicon и Linux x64; для других архитектур доступны исходники.

Установка в Windows PowerShell:

```powershell
irm https://raw.githubusercontent.com/nullxdead/ConsoleLauncher/v4.0.1/scripts/install.ps1 | iex
```

Creators: 0xdead, Frosty, Qwertyks · [Discord](https://discord.gg/FtqBFt58w)
