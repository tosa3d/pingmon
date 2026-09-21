@echo off
chcp 65001 >nul 2>&1
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"
set "LOG=last-run.txt"
title pingmon - validate

echo === pingmon validate ===> "%LOG%"
echo started %DATE% %TIME%>> "%LOG%"
echo.>> "%LOG%"
echo.
echo   Working... everything is logged to last-run.txt
echo.

REM ---- find python -------------------------------------------------
set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY ( python --version >nul 2>&1 && set "PY=python" )
if not defined PY ( python3 --version >nul 2>&1 && set "PY=python3" )
if not defined PY (
  echo [FAIL] python not found>> "%LOG%"
  goto finish
)
echo [OK] python launcher: %PY%>> "%LOG%"
%PY% --version >> "%LOG%" 2>&1

REM ---- build venv (with pip) ---------------------------------------
set "VPY="
if not exist ".venv\Scripts\python.exe" (
  echo [STEP] creating venv>> "%LOG%"
  %PY% -m venv .venv >> "%LOG%" 2>&1
)
if exist ".venv\Scripts\python.exe" (
  set "VPY=.venv\Scripts\python.exe"
  REM venv may exist but lack pip if ensurepip failed - repair it
  ".venv\Scripts\python.exe" -m pip --version >nul 2>&1
  if errorlevel 1 (
    echo [STEP] venv has no pip - running ensurepip>> "%LOG%"
    ".venv\Scripts\python.exe" -m ensurepip --upgrade >> "%LOG%" 2>&1
  )
  ".venv\Scripts\python.exe" -m pip --version >nul 2>&1
  if errorlevel 1 (
    echo [WARN] venv pip unusable - falling back to system python>> "%LOG%"
    set "VPY="
  )
)

REM ---- fallback: system python with --user -------------------------
set "USERFLAG="
if not defined VPY (
  echo [STEP] using system python with --user>> "%LOG%"
  set "VPY=%PY%"
  set "USERFLAG=--user"
)
echo [OK] interpreter: %VPY% %USERFLAG%>> "%LOG%"

REM ---- deps --------------------------------------------------------
%VPY% -c "import rich, yaml" >nul 2>&1
if errorlevel 1 (
  echo [STEP] installing rich + pyyaml>> "%LOG%"
  %VPY% -m pip install --disable-pip-version-check %USERFLAG% rich pyyaml >> "%LOG%" 2>&1
) else (
  echo [OK] deps already present>> "%LOG%"
)

%VPY% -c "import rich, yaml; print('[OK] rich + yaml import fine')" >> "%LOG%" 2>&1
if errorlevel 1 (
  echo [FAIL] dependencies still missing>> "%LOG%"
  goto finish
)

REM ---- config ------------------------------------------------------
if not exist "pingmon.yaml" (
  echo [STEP] creating pingmon.yaml>> "%LOG%"
  %VPY% -m pingmon init >> "%LOG%" 2>&1
) else (
  echo [OK] pingmon.yaml present>> "%LOG%"
)

REM ---- measure -----------------------------------------------------
echo.>> "%LOG%"
echo [STEP] running validate>> "%LOG%"
echo.>> "%LOG%"
%VPY% -m pingmon validate >> "%LOG%" 2>&1

:finish
echo.>> "%LOG%"
echo === FINISHED ===>> "%LOG%"
cls
type "%LOG%"
echo.
echo   Saved to last-run.txt
echo.
pause
