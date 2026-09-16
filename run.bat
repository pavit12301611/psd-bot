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
REM    4. run setup - creates the admin account (first run only)
REM    5. download + run a hardware-fit group of 3-5 local models
REM       (first run only - a few GB per model, in a second window)
REM    6. open the psd.ai desktop app (GUI window)
REM
REM  The desktop app runs psd.ai IN-PROCESS: no web server, no browser,
REM  no localhost page. Chats, history, memory and settings all live in
REM  the same data folder as before, so nothing is lost.
REM
REM  Safe to re-run - steps already done are skipped, so later
REM  launches start straight away. Keep this next to the psd.ai
REM  folder. Close the app window to stop.
REM
REM  Prefer the old localhost server instead? Set PSD_GUI_SERVE=1 and it
REM  will run `python -m uvicorn app:app --host 127.0.0.1 --port 7000`
REM  (matching the previous behaviour) instead of embedding the app.
REM ==================================================================

set "APP_DIR=%~dp0psd.ai"
set "PORT=7000"
REM Base port for the local model group, and how long to wait for a
REM first-run model download before opening the app anyway. Override by
REM setting these in the environment before double-clicking run.bat.
if not defined LLAMA_PORT set "LLAMA_PORT=8080"
if not defined MODEL_WAIT_SECONDS set "MODEL_WAIT_SECONDS=2400"

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
REM  3. Install dependencies (first run only - keeps restarts fast).
REM     Adds PySide6 for the desktop (GUI) window.
REM ----------------------------------------------------------------
REM A venv created by an older run.bat predates PySide6 (its .deps_ok marker
REM exists but the GUI binding is missing). Verify the binding actually
REM imports before trusting the marker, otherwise reinstall.
set "NEED_DEPS="
if not exist "venv\.deps_ok" set "NEED_DEPS=1"
if not defined NEED_DEPS (
    "%VENVPY%" -c "import PySide6" >nul 2>&1
    if errorlevel 1 set "NEED_DEPS=1"
)
if defined NEED_DEPS (
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
REM  5. Local AI model group (first run downloads 3-5 fit models)
REM
REM     Picks a 3-5 model group for THIS PC (RAM / GPU), downloads the
REM     llama.cpp server + model weights, serves one model per port
REM     starting at %LLAMA_PORT%, and registers the group in the app.
REM
REM     Set PSD_NO_LOCAL_MODEL=1 to skip this and bring your own model.
REM ----------------------------------------------------------------
if not defined PSD_NO_LOCAL_MODEL (
    echo.
    echo  ==^> Starting the local model group in a second window...
    echo      First run downloads llama.cpp + 3-5 fit model weights ^(a few GB each^).
    echo      Keep that window open while you use psd.ai - closing it stops the model group.
    if exist "runtime\local_model_failed.txt" del /q "runtime\local_model_failed.txt"
    start "psd.ai - local model group" cmd /k ""%VENVPY%" scripts\local_llama.py --port %LLAMA_PORT% --foreground"
    "%VENVPY%" scripts\local_llama.py --wait-ready %MODEL_WAIT_SECONDS%
) else (
    echo.
    echo  ==^> PSD_NO_LOCAL_MODEL is set - skipping the local model download.
)

REM ----------------------------------------------------------------
REM  6. Launch the psd.ai desktop app (GUI window).
REM
REM     Default: embedded - psd.ai runs inside this window (no web
REM     server, no browser, no localhost page).
REM
REM     Set PSD_GUI_SERVE=1 to run the previous localhost server mode
REM     (uvicorn on 127.0.0.1:%PORT%) instead.
REM ----------------------------------------------------------------
echo.
if defined PSD_GUI_SERVE (
    echo  ==^> Starting psd.ai server at http://localhost:%PORT% ...
    echo      Close this window (Ctrl+C) to stop.
    echo.
    set "APP_PORT=%PORT%"
    "%VENVPY%" -m uvicorn app:app --host 127.0.0.1 --port %PORT%
) else (
    echo  ==^> Starting the psd.ai desktop app...
    echo      No browser or localhost page - psd.ai is this window.
    echo.
    REM Belt-and-braces: if a pre-existing venv is missing the GUI binding,
    REM install it right here instead of failing silently.
    "%VENVPY%" -c "import PySide6" >nul 2>&1
    if errorlevel 1 (
        echo  [info] PySide6 ^(GUI^) not found - installing it now...
        "%VENVPY%" -m pip install PySide6
        if errorlevel 1 (
            echo.
            echo  [ERROR] Could not install PySide6 - check your internet connection.
            echo          Then double-click run.bat again.
            echo.
            pause
            exit /b 1
        )
        echo ok> "venv\.deps_ok"
    )
    "%VENVPY%" psd_gui.py
    if errorlevel 1 (
        echo.
        echo  [ERROR] psd.ai GUI closed with an error - see the messages above.
    )
)

echo.
echo  psd.ai has stopped.
pause
endlocal
