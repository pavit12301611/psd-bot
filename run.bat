@echo off
setlocal EnableExtensions EnableDelayedExpansion
title psd.ai - Desktop

REM ==================================================================
REM  psd.ai - one-click DESKTOP launcher for Windows
REM
REM  Double-click this file and it will:
REM    1. find Python 3.11+
REM    2. create a virtual environment        (first run only)
REM    3. install all dependencies, incl. Qt  (first run only)
REM    4. run setup - data folders + database (first run only)
REM    5. open the psd.ai desktop window
REM
REM  There is NO browser page and NO localhost server window: the app
REM  runs fully inside the desktop window (the workspace engine runs
REM  in-process). On the very first start the window asks you to create
REM  your admin account; local models are managed later from
REM  "Local Models" inside the app (download, progress, start/stop).
REM
REM  Safe to re-run - steps already done are skipped, so later
REM  launches open straight away. Keep this file next to the psd.ai
REM  folder. Close the psd.ai window (or its tray icon) to stop.
REM
REM  Debugging: set PSD_GUI_CONSOLE=1 before double-clicking to run the
REM  app inside this console instead of a detached window.
REM ==================================================================

set "APP_DIR=%~dp0psd.ai"

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
echo    psd.ai  -  desktop app setup + launch
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
set "VENVPYW=%APP_DIR%\venv\Scripts\pythonw.exe"

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
REM     requirements.txt includes PySide6 (the Qt desktop toolkit)
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
REM  4. First-time setup (data folders, database, .env).
REM     Admin account creation is skipped here on purpose: the desktop
REM     window shows a proper "Create your admin account" screen on its
REM     very first start.
REM ----------------------------------------------------------------
echo  ==^> Running setup...
set "PSD_AI_SKIP_ADMIN_CREATION=1"
"%VENVPY%" setup.py
if errorlevel 1 (
    echo.
    echo  [ERROR] setup.py failed - scroll up for details.
    echo.
    pause
    exit /b 1
)

REM ----------------------------------------------------------------
REM  5. Sanity-check the desktop toolkit, then open the app window.
REM     pythonw.exe = no console window at all; this launcher window
REM     closes itself right after. No second window, no localhost.
REM ----------------------------------------------------------------
echo  ==^> Checking the desktop toolkit ^(PySide6^)...
"%VENVPY%" -c "import PySide6.QtWidgets" >nul 2>&1
if errorlevel 1 (
    echo.
    echo  [ERROR] PySide6 (Qt) is missing from the virtual environment.
    echo          Re-running the dependency install now...
    "%VENVPY%" -m pip install -r requirements.txt
    "%VENVPY%" -c "import PySide6.QtWidgets" >nul 2>&1
    if errorlevel 1 (
        echo.
        echo  [ERROR] Still missing. Scroll up for the pip error, fix it,
        echo          then double-click run.bat again.
        echo.
        pause
        exit /b 1
    )
)

echo.
echo  ==^> Opening the psd.ai desktop window...
echo      First start: create your admin account in the window.
echo      Local models: app -^> "Local Models" ^(download / start / stop^).
echo      Close the psd.ai window or tray icon to stop everything.
echo.

if defined PSD_GUI_CONSOLE (
    "%VENVPY%" psd_gui.py
) else if exist "%VENVPYW%" (
    start "" "%VENVPYW%" "%APP_DIR%\psd_gui.py"
    goto :launched
) else (
    start "psd.ai" /min "%VENVPY%" psd_gui.py
)
goto :eof

:launched
REM Give the app a moment to claim its single-instance lock, then let
REM this launcher window disappear.
timeout /t 2 /nobreak >nul
endlocal
exit /b 0
