@echo off
setlocal EnableExtensions
title SAMCO Watcher - local 4207
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python 3 is not on PATH. Install Python 3, then run this again.
  pause
  exit /b 1
)

set "PYTHONIOENCODING=utf-8"
if not exist "logs" mkdir "logs"

echo ============================================================
echo  SAMCO Construction Opportunity Watcher - local mode
echo  - engine window: real HTTP checks of every registered site
echo  - dashboard:     http://127.0.0.1:4207/  (opens automatically)
echo  Close the engine window (or Ctrl+C here) to stop.
echo ============================================================

echo Starting watcher engine loop (real checks + headless-Chrome render pass)...
start "SAMCO Watcher engine" cmd /k python "Watcher.py" --loop --render --interval 60

echo Starting dashboard server on http://127.0.0.1:4207/ ...
python "server.py" --host 127.0.0.1 --port 4207 --open

echo.
echo Dashboard server stopped. The engine window may still be watching - close it to stop the loop.
pause
