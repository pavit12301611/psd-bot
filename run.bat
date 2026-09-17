@echo off
setlocal EnableExtensions EnableDelayedExpansion
title psd.ai - Setup and Launch

REM ==================================================================
REM  psd.ai - one-click launcher for Windows (desktop app)
REM
REM  Double-click this file and it will:
REM    1. find Python 3.11+
REM    2. create a virtual environment      (first run only)
REM    3. install all dependencies          (first run only)
REM    4. run setup - creates data folders  (the app asks you to create
REM       your admin account in its own window on first launch)
REM    5. download + run a hardware-fit group of 3-5 local models
REM       (first run only - a few GB per model, in a second window)
REM    6. open the psd.ai DESKTOP APP  (no browser, no localhost URL)
REM
REM  The desktop app is a native window (Tauri + React). It starts the
REM  Python engine privately inside itself and talks to it over IPC.
REM  Nothing is served on a public port and nothing opens in a browser.
REM
REM  Safe to re-run - steps already done are skipped, so later
REM  launches start straight away. Keep this next to the psd.ai
REM  folder. Close the app window to stop everything.
REM ==================================================================

set "ROOT=%~dp0"
set "APP_DIR=%ROOT%psd.ai"
set "DESKTOP_DIR=%ROOT%desktop"
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
echo    psd.ai  -  one-click setup + launch  (desktop app)
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
REM  4. First-time setup (creates data folders, database and .env).
REM     The admin account is created inside the desktop app's own
REM     first-run screen, so the console prompt is skipped here.
REM ----------------------------------------------------------------
echo  ==^> Running setup...
set "PSD_AI_DEFER_ADMIN=1"
set "PSD_AI_SKIP_RUN_HINT=1"
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
REM  6. Launch the desktop app.
REM
REM     Preference order:
REM       a) a built app:      desktop\src-tauri\target\release\psd-ai-desktop.exe
REM       b) a portable copy:  desktop\psd.ai.exe   (drop a release build here)
REM       c) developer mode:   `npm run tauri dev` inside desktop\
REM          (needs Node.js + Rust; builds the app the first time)
REM
REM     The app spawns psd.ai\desktop_server.py itself on a private,
REM     random loopback port and talks to it through IPC. Nothing is
REM     opened in a browser and no fixed port is used.
REM ----------------------------------------------------------------
echo.
echo  ==^> Opening the psd.ai desktop app...
echo      Close the app window to stop psd.ai.
echo.

set "PSD_AI_APP_DIR=%APP_DIR%"
set "PSD_AI_PYTHON=%VENVPY%"

set "APP_EXE="
if exist "%DESKTOP_DIR%\src-tauri\target\release\psd-ai-desktop.exe" set "APP_EXE=%DESKTOP_DIR%\src-tauri\target\release\psd-ai-desktop.exe"
if not defined APP_EXE if exist "%DESKTOP_DIR%\psd.ai.exe" set "APP_EXE=%DESKTOP_DIR%\psd.ai.exe"
if not defined APP_EXE if exist "%ROOT%psd.ai.exe" set "APP_EXE=%ROOT%psd.ai.exe"

if defined APP_EXE (
    echo       Using %APP_EXE%
    "%APP_EXE%"
    goto :done
)

REM ---- developer fallback: build + run the Tauri app from source ----
where node >nul 2>&1
if errorlevel 1 (
    echo.
    echo  [ERROR] No built psd.ai desktop app was found and Node.js is not installed.
    echo.
    echo  Either:
    echo    - place a release build at  desktop\psd.ai.exe   ^(see desktop\README.md^), or
    echo    - install Node.js LTS ^(https://nodejs.org^) and Rust ^(https://rustup.rs^),
    echo      then double-click run.bat again to build the app from source.
    echo.
    pause
    exit /b 1
)
where cargo >nul 2>&1
if errorlevel 1 (
    echo.
    echo  [ERROR] No built psd.ai desktop app was found and Rust ^(cargo^) is not installed.
    echo.
    echo  Install Rust from https://rustup.rs ^(default options^), reopen this window,
    echo  and double-click run.bat again. The first build takes a few minutes.
    echo.
    pause
    exit /b 1
)

cd /d "%DESKTOP_DIR%"
if not exist "node_modules" (
    echo  ==^> Installing desktop UI dependencies ^(first run only^)...
    call npm install --no-audit --no-fund
    if errorlevel 1 (
        echo.
        echo  [ERROR] npm install failed - scroll up for details.
        echo.
        pause
        exit /b 1
    )
)

if not exist "src-tauri\target\release\psd-ai-desktop.exe" (
    echo  ==^> Building the desktop app ^(first run only, a few minutes^)...
    call npm run tauri build -- --no-bundle
    if errorlevel 1 (
        echo.
        echo  [ERROR] Desktop app build failed - scroll up for details.
        echo.
        pause
        exit /b 1
    )
)

echo       Starting desktop\src-tauri\target\release\psd-ai-desktop.exe
"%DESKTOP_DIR%\src-tauri\target\release\psd-ai-desktop.exe"

:done
echo.
echo  psd.ai has closed.
endlocal
