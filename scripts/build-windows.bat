@echo off
rem ScoreDeck - build a portable Windows folder (PyInstaller --onedir)
rem Run from anywhere: this script resolves paths relative to itself.
setlocal
chcp 65001 >nul
cd /d "%~dp0.."

rem 1) dependencies (PyInstaller is only needed for building, not for running)
python -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto :error

rem 2) build; data files are copied next to the exe so the runtime can find them
rem    app\   : GUI 模块（实时服务的子进程会 import 它们）
rem    core\  : pta_fetch.py / pta_common.py / pta_to_resolver_xml.py
rem    liveboard\ : 实时榜单服务与 web 前端
python -m PyInstaller --noconfirm --clean --windowed --onedir --name ScoreDeck ^
    --paths app ^
    --add-data "app;app" ^
    --add-data "core;core" ^
    --add-data "liveboard;liveboard" ^
    --distpath dist --workpath build --specpath build ^
    app\pta_assistant_gui.py
if errorlevel 1 goto :error

echo.
echo Build OK: dist\ScoreDeck\ScoreDeck.exe
echo Copy the whole dist\ScoreDeck\ folder to the target machine.
exit /b 0

:error
echo.
echo Build failed. Check the messages above.
exit /b 1
