@echo off
chcp 65001 >nul 2>&1
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"
title pingmon - A/B test

if not exist ".venv\Scripts\python.exe" (
  echo.
  echo   Setup missing. Run 1-VALIDATE.bat first.
  echo.
  pause
  exit /b 1
)
echo.
echo   ================================================================
echo    Before you start:
echo      - close ExitLag / Windscribe / Hotspot Shield NOW
echo      - stay near the PC: it asks you to switch services every 60s
echo      - takes about 25-30 minutes
echo   ================================================================
echo.
pause

.venv\Scripts\python.exe -m pingmon run
echo.
pause
