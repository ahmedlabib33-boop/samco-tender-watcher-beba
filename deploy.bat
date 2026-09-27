@echo off
setlocal EnableExtensions
title SAMCO Watcher - deploy to Vercel
cd /d "%~dp0"

where node >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Node.js is required for the Vercel CLI. Install Node LTS, then run this again.
  pause
  exit /b 1
)

if not exist "vercel\data\latest.json" (
  echo [ERROR] No snapshot yet. Run run.bat first so the engine writes vercel\data\latest.json.
  pause
  exit /b 1
)

cd vercel

echo Checking Vercel login...
call npx --yes vercel@latest whoami
if errorlevel 1 (
  echo Not logged in. Starting "vercel login" once - finish it in the browser, then re-run this file.
  call npx --yes vercel@latest login
  pause
  exit /b 1
)

echo.
echo Deploying to production. What ships is exactly the snapshot your local watcher wrote
echo (vercel\data\latest.json + index.html). Nothing is generated or altered at deploy time.
echo.
call npx --yes vercel@latest --prod
if errorlevel 1 (
  echo [ERROR] deploy failed - read the output above.
  pause
  exit /b 1
)
echo.
echo Done. Re-run deploy.bat after a fresh watcher cycle to publish updated real data.
pause
