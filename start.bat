@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" py -3 -m venv .venv
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -c "import consolecraft, minecraft_launcher_lib, rich, questionary, platformdirs, requests, keyring" >nul 2>nul
if errorlevel 1 ".venv\Scripts\python.exe" -m pip install -e .
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m consolecraft %*
