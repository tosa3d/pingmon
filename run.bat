@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul 2>&1
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"
title pingmon

echo.
echo  ================================================================
echo   pingmon - ping-reduction service tester
echo  ================================================================
echo.

REM ---------------------------------------------------------------- python
set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY ( python --version >nul 2>&1 && set "PY=python" )
if not defined PY ( python3 --version >nul 2>&1 && set "PY=python3" )

if not defined PY (
  echo  [X] Python not found.
  echo.
  echo      Install Python 3.10+ from https://www.python.org/downloads/
  echo      IMPORTANT: tick "Add python.exe to PATH" during install,
  echo      then run this file again.
  echo.
  pause
  exit /b 1
)

for /f "tokens=2" %%v in ('%PY% --version 2^>^&1') do set "PYVER=%%v"
echo  [1/3] Python %PYVER%  ^(%PY%^)

REM ---------------------------------------------------------------- venv
if not exist ".venv\Scripts\python.exe" (
  echo  [2/3] Creating virtual environment ^(first run only^)...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo  [X] Could not create .venv
    pause
    exit /b 1
  )
) else (
  echo  [2/3] Virtual environment found
)
set "VPY=.venv\Scripts\python.exe"

REM ---------------------------------------------------------------- deps
"%VPY%" -c "import rich, yaml" >nul 2>&1
if errorlevel 1 (
  echo  [3/3] Installing dependencies ^(rich, pyyaml^)...
  "%VPY%" -m pip install --quiet --upgrade pip
  "%VPY%" -m pip install --quiet -r requirements.txt
  if errorlevel 1 (
    echo  [X] Dependency install failed. Check your internet connection.
    pause
    exit /b 1
  )
) else (
  echo  [3/3] Dependencies OK
)

REM ---------------------------------------------------------------- config
if not exist "pingmon.yaml" (
  echo.
  echo  First run - creating pingmon.yaml from the example.
  "%VPY%" -m pingmon init >nul
  echo  Created. Open pingmon.yaml to add your services ^(ExitLag, NoPing, ...^)
)

REM ---------------------------------------------------------------- menu
:menu
echo.
echo  ----------------------------------------------------------------
echo    1  validate    check which targets actually respond
echo    2  monitor     live dashboard ^(watch only, Ctrl+C to stop^)
echo    3  run         full A/B test + HTML report
echo    4  discover    capture a game's real server IP ^(run mid-match^)
echo    5  sdr-refresh get Valve relay IPs for CS2
echo    6  config      open pingmon.yaml in Notepad
echo    0  exit
echo  ----------------------------------------------------------------
echo.
set "CHOICE="
set /p "CHOICE=  Choose: "

if "%CHOICE%"=="1" ( "%VPY%" -m pingmon validate & goto menu )
if "%CHOICE%"=="2" ( "%VPY%" -m pingmon monitor  & goto menu )
if "%CHOICE%"=="4" (
  echo.
  echo  Which game? r6 / cs2 / apex / warzone / fc26  ^(blank = all^)
  set "G="
  set /p "G=  Game: "
  if defined G ( "%VPY%" -m pingmon discover --game !G! --seconds 30 ) else ( "%VPY%" -m pingmon discover --seconds 30 )
  goto menu
)
if "%CHOICE%"=="5" ( "%VPY%" -m pingmon sdr-refresh & goto menu )
if "%CHOICE%"=="6" ( start notepad pingmon.yaml & goto menu )
if "%CHOICE%"=="0" ( exit /b 0 )

if "%CHOICE%"=="3" (
  echo.
  echo  Before starting, make sure:
  echo    - every ping-reduction service is CLOSED right now
  echo    - your game's region is set in pingmon.yaml
  echo    - you can sit near the PC to switch services when prompted
  echo.
  echo  Default: 6 rounds x 60s blocks. Takes roughly 25-30 minutes.
  echo.
  set "R="
  set /p "R=  Rounds [6]: "
  if not defined R set "R=6"
  "%VPY%" -m pingmon run --rounds !R!
  echo.
  echo  Done. The HTML report is in the pingmon-runs folder.
  start "" "pingmon-runs"
  goto menu
)

echo  Unknown choice.
goto menu
