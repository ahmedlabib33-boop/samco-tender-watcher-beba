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
echo  - checks run automatically every hour (Windows task "SAMCO Watcher hourly")
echo  - dashboard:     http://127.0.0.1:4207/  (opens automatically)
echo  This window only shows the dashboard; Ctrl+C here to close it.
echo ============================================================

echo Starting dashboard server on http://127.0.0.1:4207/ ...
python "server.py" --host 127.0.0.1 --port 4207 --open

echo.
echo Dashboard server stopped. The hourly checks keep running in the background.
pause
