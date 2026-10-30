@echo off
rem ScoreDeck - start the live scoreboard service (no GUI).
rem Requires Python 3.9+ on PATH.
setlocal
chcp 65001 >nul
cd /d "%~dp0..\liveboard"
set "PYTHONPATH=%CD%;%CD%\..\app;%CD%\..\core"

rem First run: seed config.json from the sanitized example.
if not exist config.json (
    copy /y "..\examples\live-config.example.json" config.json >nul
    echo [i] created liveboard\config.json from examples\live-config.example.json
    echo [i] edit problem_set_id before starting the poller.
)

if not exist cookie.txt (
    echo [i] missing liveboard\cookie.txt - put one line with JSESSIONID=...; PTASession=...
)

rem Port / interval examples (edit config.json, or run the modules directly):
rem   python server.py --port 8791
rem   python poller.py --interval 5 --full-interval 120
rem Default address: http://127.0.0.1:8791/board
echo [i] starting live scoreboard: http://127.0.0.1:8791/board
python launcher.py
if errorlevel 1 pause
