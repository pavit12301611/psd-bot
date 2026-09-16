@echo off
setlocal EnableExtensions EnableDelayedExpansion
title psd.ai - Terminal Interface

REM ==================================================================
REM  psd.ai - terminal interface (replaces the localhost website)
REM
REM  Double-click this file and it will:
REM    1. find Python 3.11+
REM    2. create a virtual environment (first run only)
REM    3. install dependencies incl. the `textual` TUI library
REM    4. run first-time setup inside the terminal (admin username +
REM       password, database initialisation) - first run only
REM    5. open the full-screen psd.ai terminal interface
REM
REM  No web server is started and no browser or port is used: psd.ai
REM  runs in-process and is drawn in this terminal window.
REM  Close this window (or press q inside the app) to stop.
REM ==================================================================

set "APP_DIR=%~dp0psd.ai"

if not exist "%APP_DIR%\psd_tui.py" (
    echo.
    echo  [ERROR] Could not find psd_tui.py inside:
    echo          %APP_DIR%
    echo.
    echo  Keep tui.bat in the same folder as the psd.ai folder.
    echo.
    pause
    exit /b 1
)

cd /d "%APP_DIR%"

echo.
echo  ============================================================
echo    psd.ai - terminal interface (no browser, no localhost)
echo  ============================================================
echo.

REM ----------------------------------------------------------------
REM  1. Find Python 3.11+
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
    echo  double-click tui.bat again.
    echo.
    pause
    exit /b 1
)

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
if not exist "venv\.tui_deps_ok" (
    echo  ==^> Installing dependencies... first run can take a few minutes.
    "%VENVPY%" -m pip install --upgrade pip --quiet
    "%VENVPY%" -m pip install -r requirements.txt "textual" "httpx<0.28"
    if errorlevel 1 (
        echo.
        echo  [ERROR] Dependency install failed - scroll up for the pip error.
        echo          Fix it, then double-click tui.bat again.
        echo.
        pause
        exit /b 1
    )
    echo ok> "venv\.tui_deps_ok"
) else (
    echo  ==^> Dependencies already installed - skipping.
    echo       ^(Delete the venv folder to force a reinstall.^)
)

REM ----------------------------------------------------------------
REM  4. Launch the terminal interface.
REM     psd_tui.py handles first-time setup (data folders, database
REM     and admin account) inside the terminal on the first run.
REM ----------------------------------------------------------------
echo.
echo  ==^> Starting the psd.ai terminal interface...
echo      Press q inside the app (or Ctrl+C) to stop.
echo.

"%VENVPY%" psd_tui.py --embedded

echo.
echo  psd.ai terminal interface has stopped.
pause
endlocal
