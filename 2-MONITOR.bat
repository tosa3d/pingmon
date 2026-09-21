@echo off
chcp 65001 >nul 2>&1
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"
title pingmon - live monitor

if not exist ".venv\Scripts\python.exe" (
  echo.
  echo   Setup missing. Run 1-VALIDATE.bat first.
  echo.
  pause
  exit /b 1
)

.venv\Scripts\python.exe -m pingmon monitor
echo.
pause
