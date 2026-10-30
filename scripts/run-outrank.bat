@echo off
rem ScoreDeck - start the read-only Outrank archive site.
rem Archives live in ..\data\archives (gitignored); seed it with archive.py.
setlocal
chcp 65001 >nul
cd /d "%~dp0.."
set "PYTHONPATH=%CD%\liveboard;%CD%\core;%CD%\app"

if not exist data\archives mkdir data\archives

rem Bind 127.0.0.1 only; put nginx (or another reverse proxy) in front for HTTPS.
rem For a quick local test you can pass --host 0.0.0.0 and --port 8080 instead.
python outrank\outrank.py --host 127.0.0.1 --port 8099 ^
    --archives data\archives --web-root liveboard\web
if errorlevel 1 pause
