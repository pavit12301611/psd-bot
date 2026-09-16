@echo off
setlocal EnableExtensions EnableDelayedExpansion
title psd.ai - Setup and Launch

REM ==================================================================
REM  psd.ai - one-click launcher for Windows
REM
REM  Double-click this file and it will:
REM    1. find Python 3.11+
REM    2. create a virtual environment      (first run only)
REM    3. install all dependencies          (first run only)
REM    4. run setup - creates admin account (first run only)
REM    5. open http://localhost:7000 in your browser
REM    6. start the server
REM
REM  Safe to re-run - steps already done are skipped, so later
REM  launches start straight away. Keep this next to the psd.ai
REM  folder. Press Ctrl+C in this window to stop the server.
REM ==================================================================

set "APP_DIR=%~dp0psd.ai"
set "PORT=7000"

if not exist "%APP_DIR%\app.py" (
    echo.
    echo  [ERROR] Could not find app.py inside:
    echo          %APP_DIR%
    echo.
    echo  Keep run.bat in the same folder as the psd.ai folder.
    echo.
    pause
    exit /b 1
)

cd /d "%APP_DIR%"

echo.
echo  ============================================================
echo    psd.ai  -  one-click setup + launch
echo  ============================================================
echo.

REM ----------------------------------------------------------------
REM  1. Find Python 3.11+ (py launcher first, then plain python)
REM ----------------------------------------------------------------
echo  ==^> Looking for Python 3.11+...

set "PYCMD="
py -3.13 -c "import sys" >nul 2>&1 && set "PYCMD=py -3.13"
if not defined PYCMD py -3.12 -c "import sys" >nul 2>&1 && set "PYCMD=py -3.12"
if not defined PYCMD py -3.11 -c "import sys" >nul 2>&1 && set "PYCMD=py -3.11"
if not defined PYCMD (
    python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1 && set "PYCMD=python"
)

if not defined PYCMD (
    echo.
    echo  [ERROR] Python 3.11+ was not found on this PC.
    echo.
    echo  Install it from https://www.python.org/downloads/
    echo  ^(tick "Add python.exe to PATH" during setup^), then
    echo  double-click run.bat again.
    echo.
    pause
    exit /b 1
)

set "PYVER="
for /f "delims=" %%i in ('%PYCMD% -c "import platform; print(platform.python_version())"') do set "PYVER=%%i"
echo       Using Python %PYVER% ^(%PYCMD%^)

REM ----------------------------------------------------------------
REM  2. Create the virtual environment (first run only)
REM ----------------------------------------------------------------
set "VENVPY=%APP_DIR%\venv\Scripts\python.exe"

if not exist "%VENVPY%" (
    echo  ==^> Creating virtual environment ^(venv^)...
    %PYCMD% -m venv venv
    if errorlevel 1 (
        echo.
        echo  [ERROR] Failed to create the virtual environment.
        echo.
        pause
        exit /b 1
    )
) else (
    echo  ==^> Virtual environment already exists - skipping.
)

REM ----------------------------------------------------------------
REM  3. Install dependencies (first run only - keeps restarts fast)
REM ----------------------------------------------------------------
if not exist "venv\.deps_ok" (
    echo  ==^> Installing dependencies... first run can take a few minutes.
    "%VENVPY%" -m pip install --upgrade pip --quiet
    "%VENVPY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo  [ERROR] Dependency install failed - scroll up for the pip error.
        echo          Fix it, then double-click run.bat again.
        echo.
        pause
        exit /b 1
    )
    echo ok> "venv\.deps_ok"
) else (
    echo  ==^> Dependencies already installed - skipping.
    echo       ^(Delete the venv folder to force a reinstall.^)
)

REM ----------------------------------------------------------------
REM  4. First-time setup (creates data folders, database and .env,
REM     and asks you for an admin username + password on first run)
REM ----------------------------------------------------------------
echo  ==^> Running setup...
"%VENVPY%" setup.py
if errorlevel 1 (
    echo.
    echo  [ERROR] setup.py failed - scroll up for details.
    echo.
    pause
    exit /b 1
)

REM ----------------------------------------------------------------
REM  5. Open the app in the browser a few seconds after the server
REM     starts, then launch the server in this window
REM ----------------------------------------------------------------
echo.
echo  ==^> Starting psd.ai at http://localhost:%PORT%
echo      The page will open automatically - press Ctrl+C here to stop.
echo.

set "APP_PORT=%PORT%"
start "" /min cmd /c "timeout /t 5 /nobreak >nul & start http://localhost:%PORT%"

"%VENVPY%" -m uvicorn app:app --host 127.0.0.1 --port %PORT%

echo.
echo  psd.ai has stopped.
pause
endlocal
