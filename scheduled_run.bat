@echo off
rem Hourly job started by Windows Task Scheduler (task "SAMCO Watcher hourly").
rem One real check cycle, then publish the fresh snapshot to the Vercel site using
rem the Vercel login already on this PC. Output goes to logs\scheduled.log.
setlocal EnableExtensions
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
if not exist "logs" mkdir "logs"
set "LOG=%~dp0logs\scheduled.log"

echo ===== %date% %time% start >> "%LOG%"
"C:\Python314\python.exe" "Watcher.py" --once --render --scheduled --interval 60 >> "%LOG%" 2>&1
if errorlevel 1 (
  echo ===== engine failed - nothing published >> "%LOG%"
  exit /b 1
)

cd vercel
call "C:\Program Files\nodejs\npx.cmd" --yes vercel@latest deploy --prod --yes >> "%LOG%" 2>&1
if errorlevel 1 (
  echo ===== publish to Vercel failed >> "%LOG%"
  exit /b 1
)
echo ===== %date% %time% done - site updated >> "%LOG%"
