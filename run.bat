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
REM    4. install the Jarvis extras         (first run only - voice +
REM       PC control, see requirements-jarvis.txt)
REM    5. run setup - creates data folders  (the app asks you to create
REM       your admin account in its own window on first launch)
REM    6. download + run a hardware-fit group of 3-5 local models
REM       (first run only - a few GB per model, in a second window)
REM    7. open the psd.ai DESKTOP APP  (no browser, no localhost URL)
REM
REM  The desktop app is a native window (Tauri + React). It starts the
REM  Python engine privately inside itself and talks to it over IPC.
REM  Nothing is served on a public port and nothing opens in a browser.
REM
REM  Safe to re-run - steps already done are skipped, so later
REM  launches start straight away. Keep this next to the psd.ai
REM  folder. Close the app window to stop everything.
REM
REM  ------------------------------------------------------------------
REM  Options (run from a terminal: run.bat --help)
REM
REM    run.bat              normal launch (what double-clicking does)
REM    run.bat --help       show this help
REM    run.bat --doctor     check this PC and report what is missing
REM    run.bat --repair     rebuild the venv and reinstall everything
REM    run.bat --update     git pull + refresh dependencies
REM    run.bat --rebuild    force-rebuild the desktop app
REM    run.bat --no-voice   skip the Jarvis extras (no local Whisper,
REM                         no PC-control packages)
REM    run.bat --no-models  skip the local model group this run
REM    run.bat --no-app     set everything up, then stop (no window)
REM    run.bat --skip-numpy-check   skip the numpy import check
REM    run.bat --doctor     report what this PC is missing
REM
REM  Environment overrides
REM
REM    PSD_NO_LOCAL_STT=1    install the PC-control extras but not the
REM                          local Whisper package (use the browser for
REM                          speech-to-text instead)
REM    PSD_NO_LOCAL_MODEL=1  never download local models
REM    PSD_AI_COMPUTER_CONTROL=0/1  switch desktop control off/on
REM    LLAMA_PORT=9090       first model port for the group
REM    PSD_AI_RUNTIME_DIR=   where llama.cpp + weights are cached
REM    PSD_AI_SKIP_NUMPY_CHECK=1  skip the numpy import check
REM    NUMPY_IMPORT_BUDGET=90     seconds to allow for "import numpy"
REM  ------------------------------------------------------------------
REM ==================================================================

set "ROOT=%~dp0"
set "APP_DIR=%ROOT%psd.ai"
set "DESKTOP_DIR=%ROOT%desktop"
set "LOG_DIR=%ROOT%logs"
set "LOG_FILE=%LOG_DIR%\run.log"
set "ROOTDRIVE=%ROOT:~0,1%"

REM ----------------------------------------------------------------
REM  Options parsing (double-clicking passes no arguments)
REM ----------------------------------------------------------------
set "DOCTOR=0"
set "REPAIR=0"
set "UPDATE=0"
set "REBUILD=0"
set "NO_VOICE=0"
set "NO_APP=0"
set "SKIP_NUMPY_CHECK=0"
if defined PSD_AI_SKIP_NUMPY_CHECK set "SKIP_NUMPY_CHECK=1"

:parse_args
if "%~1"=="" goto :args_done
if /i "%~1"=="--help"    call :usage & exit /b 0
if /i "%~1"=="-h"        call :usage & exit /b 0
if /i "%~1"=="/?"        call :usage & exit /b 0
if /i "%~1"=="--doctor"  set "DOCTOR=1"
if /i "%~1"=="--repair"  set "REPAIR=1"
if /i "%~1"=="--update"  set "UPDATE=1"
if /i "%~1"=="--rebuild" set "REBUILD=1"
if /i "%~1"=="--no-voice"  set "NO_VOICE=1"
if /i "%~1"=="--no-models" set "PSD_NO_LOCAL_MODEL=1"
if /i "%~1"=="--no-app"    set "NO_APP=1"
if /i "%~1"=="--skip-numpy-check" set "SKIP_NUMPY_CHECK=1"
shift
goto :parse_args
:args_done

REM Keep the console readable on every Windows locale.
chcp 65001 >nul 2>&1

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%" >nul 2>&1
call :log "run.bat started (args: %*)"

REM Models + llama.cpp live OUTSIDE this folder in %LOCALAPPDATA%\psd.ai\runtime
REM (override with PSD_AI_RUNTIME_DIR), so a fresh copy of the code reuses them.
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
    call :log "ERROR: app.py not found in %APP_DIR%"
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
REM  --doctor : report on this PC and stop
REM ----------------------------------------------------------------
if "%DOCTOR%"=="1" (
    call :doctor
    exit /b 0
)

REM ----------------------------------------------------------------
REM  --update : pull the latest code, then continue with setup
REM ----------------------------------------------------------------
if "%UPDATE%"=="1" (
    echo  ==^> Updating psd.ai...
    where git >nul 2>&1
    if errorlevel 1 (
        echo      git is not installed - skipping the update.
        echo      ^(Re-download the project, or install Git for Windows.^)
    ) else (
        cd /d "%ROOT%"
        git pull --ff-only
        if errorlevel 1 (
            echo      [WARN] git pull failed - continuing with the current copy.
        ) else (
            echo      Code updated.
        )
        cd /d "%APP_DIR%"
    )
    echo.
)

REM ----------------------------------------------------------------
REM  1. Find Python 3.11+ (py launcher first, then plain python)
REM ----------------------------------------------------------------
echo  ==^> Looking for Python 3.11+...

REM Prefer 3.12 / 3.13 / 3.11 (mature wheels for numpy, onnxruntime, etc.).
REM 3.14 is accepted as a last resort but some native packages are still
REM settling there, so it is tried after the others.
set "PYCMD="
py -3.12 -c "import sys" >nul 2>&1 && set "PYCMD=py -3.12"
if not defined PYCMD py -3.13 -c "import sys" >nul 2>&1 && set "PYCMD=py -3.13"
if not defined PYCMD py -3.11 -c "import sys" >nul 2>&1 && set "PYCMD=py -3.11"
if not defined PYCMD py -3.14 -c "import sys" >nul 2>&1 && set "PYCMD=py -3.14"
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
    echo  If Windows opens the Microsoft Store when you type "python",
    echo  the Store alias is shadowing the real install - install Python
    echo  from python.org ^(or run:  py -3 --version^) and try again.
    echo.
    call :log "ERROR: no Python 3.11+ found"
    pause
    exit /b 1
)

set "PYVER="
for /f "delims=" %%i in ('%PYCMD% -c "import platform; print(platform.python_version())"') do set "PYVER=%%i"
echo       Using Python %PYVER% ^(%PYCMD%^)
call :log "python=%PYVER% via %PYCMD%"

REM ----------------------------------------------------------------
REM  2. Create the virtual environment (first run only)
REM ----------------------------------------------------------------
set "VENVPY=%APP_DIR%\venv\Scripts\python.exe"

REM numpy's OpenBLAS DLL can deadlock on import on some Windows PCs (AV /
REM loader-lock). Force a single BLAS thread everywhere - this is the known
REM fix and costs nothing for psd.ai's workloads.
set "OPENBLAS_NUM_THREADS=1"
set "OMP_NUM_THREADS=1"
set "MKL_NUM_THREADS=1"
set "OPENBLAS_MAIN_FREE=1"

if "%REPAIR%"=="1" (
    echo.
    echo  ==^> --repair : removing the venv and the dependency stamps...
    if exist "%APP_DIR%\venv" rmdir /s /q "%APP_DIR%\venv"
    call :log "repair: venv removed"
)

REM Sanity check an existing venv: numpy must import within 20s or the venv
REM is rebuilt from scratch.
if exist "%VENVPY%" if exist "venv\.deps_ok" (
    echo  ==^> Checking the virtual environment...
    call :verify_numpy
    if errorlevel 1 (
        echo      numpy in the existing venv hangs or fails - rebuilding the venv.
        call :log "venv numpy check failed - rebuilding"
        rmdir /s /q venv
    ) else (
        echo       venv OK
    )
)

if not exist "%VENVPY%" (
    echo  ==^> Creating virtual environment ^(venv^)...
    %PYCMD% -m venv venv
    if errorlevel 1 (
        echo.
        echo  [ERROR] Failed to create the virtual environment.
        echo.
        call :log "ERROR: venv creation failed"
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
    call :pip_run "%VENVPY%" "-m pip install --upgrade pip --quiet --disable-pip-version-check"
    if errorlevel 1 (
        echo      [WARN] pip upgrade failed - continuing with the bundled pip.
    )
    call :pip_run "%VENVPY%" "-m pip install -r requirements.txt --disable-pip-version-check --no-input"
    if errorlevel 1 (
        echo.
        echo  [ERROR] Dependency install failed - scroll up for the pip error.
        echo          Common causes: no internet, a proxy, or antivirus blocking pip.
        echo          Fix it, then double-click run.bat again.
        echo          ^(Or run:  run.bat --repair^)
        echo.
        call :log "ERROR: pip install requirements.txt failed"
        pause
        exit /b 1
    )
    echo ok> "venv\.deps_ok"
) else (
    echo  ==^> Dependencies already installed - skipping.
    echo       ^(run.bat --repair forces a fresh install.^)
)
if "%UPDATE%"=="1" (
    echo  ==^> --update : refreshing dependencies...
    call :pip_run "%VENVPY%" "-m pip install -r requirements.txt --disable-pip-version-check --no-input"
)

REM Verify numpy actually imports in THIS venv. Two things make a naive check
REM lie on real Windows PCs, and both are handled below:
REM   * the first import after an install pulls in a ~40 MB OpenBLAS DLL that
REM     Windows Defender scans on first touch - slow, not stuck; and
REM   * `cmd /c "..."` with more than two quotes mangles the command, so the
REM     old check could never succeed on ANY PC. See :_numpy_once.
REM The budget is generous, a slow import is retried once (warm by then), and
REM a genuine error is printed instead of guessed at.
if not defined NUMPY_IMPORT_BUDGET set "NUMPY_IMPORT_BUDGET=60"
echo  ==^> Verifying numpy...
if "%SKIP_NUMPY_CHECK%"=="1" (
    echo      skipped ^(--skip-numpy-check^)
) else (
    call :verify_numpy
    if not errorlevel 1 (
        echo       numpy OK ^(!NP_SECONDS!s^)
    ) else (
        echo      numpy did not import in !NP_SECONDS!s - trying the numpy 1.26 line,
        echo      which ships a different OpenBLAS build...
        "%VENVPY%" -m pip install --quiet --disable-pip-version-check "numpy<2" --force-reinstall
        if errorlevel 1 (
            echo      [WARN] the numpy 1.26 install failed - staying on the current build.
        )
        set "NP_RETRY=0"
        call :verify_numpy
        if not errorlevel 1 (
            echo       numpy OK ^(!NP_SECONDS!s^)
        ) else (
            call :numpy_troubleshoot
        )
    )
)

REM ----------------------------------------------------------------
REM  4. Jarvis extras - voice mode ("Talk to psd.ai") + PC control
REM
REM     requirements-jarvis.txt adds:
REM       * faster-whisper  - offline speech-to-text for the microphone
REM       * pyautogui / mss / pyperclip / psutil / pygetwindow / pycaw
REM                         - mouse, keyboard, windows, clipboard, volume
REM     Every one of them is optional in code: without them psd.ai falls
REM     back to built-in OS calls and browser speech, so a failure here is
REM     a warning, never a hard stop.
REM ----------------------------------------------------------------
if "%NO_VOICE%"=="1" (
    echo.
    echo  ==^> --no-voice : skipping the Jarvis extras.
    echo       Voice mode will use the browser for speech, and PC control
    echo       will rely on the built-in Windows fallbacks.
) else (
    if not exist "requirements-jarvis.txt" (
        echo  ==^> requirements-jarvis.txt not found - skipping the Jarvis extras.
    ) else (
        if not exist "venv\.jarvis_ok" (
            echo.
            echo  ==^> Installing the Jarvis extras ^(voice + PC control^)...
            echo      faster-whisper, pyautogui, mss, pyperclip, psutil, pygetwindow
            call :pip_run "%VENVPY%" "-m pip install -r requirements-jarvis.txt --disable-pip-version-check --no-input"
            if errorlevel 1 (
                echo      [WARN] Some Jarvis extras failed to install.
                echo             Voice + PC control still work using the built-in
                echo             fallbacks; re-run later with:
                echo               %VENVPY% -m pip install -r requirements-jarvis.txt
                call :log "WARN: jarvis extras install failed"
            ) else (
                echo ok> "venv\.jarvis_ok"
                echo      Jarvis extras installed.
                call :log "jarvis extras installed"
            )
        ) else (
            echo  ==^> Jarvis extras already installed - skipping.
        )

        REM faster-whisper is the one heavy piece (~1 GB with ctranslate2), so
        REM it gets its own stamp and its own opt-out.
        if not defined PSD_NO_LOCAL_STT (
            if not exist "venv\.stt_ok" (
                echo  ==^> Installing faster-whisper ^(offline speech-to-text^)...
                call :pip_run "%VENVPY%" "-m pip install faster-whisper --disable-pip-version-check --no-input"
                if errorlevel 1 (
                    echo      [WARN] faster-whisper did not install.
                    echo             Set PSD_NO_LOCAL_STT=1 to stop asking, or install
                    echo             it later. Voice mode will use the browser instead.
                    call :log "WARN: faster-whisper install failed"
                ) else (
                    echo ok> "venv\.stt_ok"
                    echo      faster-whisper installed - the microphone now works offline.
                    call :log "faster-whisper installed"
                )
            )
        ) else (
            echo  ==^> PSD_NO_LOCAL_STT is set - skipping faster-whisper.
        )
    )
)

REM ----------------------------------------------------------------
REM  5. First-time setup (creates data folders, database and .env).
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
    call :log "ERROR: setup.py failed"
    pause
    exit /b 1
)

if "%NO_APP%"=="1" (
    echo.
    echo  ==^> --no-app : setup finished. The desktop app was not started.
    echo.
    call :log "setup done (--no-app)"
    exit /b 0
)

REM ----------------------------------------------------------------
REM  6. Local AI model group (first run downloads 3-5 fit models)
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
    if not defined PSD_AI_RUNTIME_DIR set "PSD_AI_RUNTIME_DIR=%LOCALAPPDATA%\psd.ai\runtime"
    echo      Models are stored on this PC at !PSD_AI_RUNTIME_DIR!
    echo      ^(outside the project folder, so re-downloading the code never re-downloads models^).
    call :free_space_gb
    if defined FREE_GB (
        if !FREE_GB! LSS 8 (
            echo.
            echo      [WARN] Only !FREE_GB! GB free on drive %ROOTDRIVE%:. The model group
            echo             needs roughly 10-20 GB. psd.ai will still start, but the
            echo             download may fail part-way. Free some space or set
            echo             PSD_NO_LOCAL_MODEL=1 to skip it.
            call :log "WARN: low disk space (!FREE_GB! GB)"
        )
    )
    if exist "%PSD_AI_RUNTIME_DIR%\local_model_failed.txt" del /q "%PSD_AI_RUNTIME_DIR%\local_model_failed.txt"
    REM Same doubled-quote wrapping as :_numpy_once - see the note there.
    start "psd.ai - local model group" cmd /d /s /k ""%VENVPY%" scripts\local_llama.py --port %LLAMA_PORT% --foreground"
    "%VENVPY%" scripts\local_llama.py --wait-ready %MODEL_WAIT_SECONDS%
) else (
    echo.
    echo  ==^> PSD_NO_LOCAL_MODEL is set - skipping the local model download.
)

REM ----------------------------------------------------------------
REM  7. Launch the desktop app.
REM
REM     Preference order:
REM       a) a built app:      desktop\src-tauri\target\release\psd-ai-desktop.exe
REM       b) a portable copy:  desktop\psd.ai.exe   (drop a release build here)
REM       c) build from source: Node.js + Rust + C++ Build Tools are
REM          installed AUTOMATICALLY if missing, then the app is built
REM          once (later runs reuse the exe)
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
if "%REBUILD%"=="0" (
    if exist "%DESKTOP_DIR%\src-tauri\target\release\psd-ai-desktop.exe" set "APP_EXE=%DESKTOP_DIR%\src-tauri\target\release\psd-ai-desktop.exe"
    if not defined APP_EXE if exist "%DESKTOP_DIR%\psd.ai.exe" set "APP_EXE=%DESKTOP_DIR%\psd.ai.exe"
    if not defined APP_EXE if exist "%ROOT%psd.ai.exe" set "APP_EXE=%ROOT%psd.ai.exe"
)

if defined APP_EXE (
    echo       Using %APP_EXE%
    call :log "launching app: %APP_EXE%"
    "%APP_EXE%"
    goto :done
)

if "%REBUILD%"=="1" if exist "%DESKTOP_DIR%\src-tauri\target\release\psd-ai-desktop.exe" (
    echo  ==^> --rebuild : discarding the previous build.
    del /q "%DESKTOP_DIR%\src-tauri\target\release\psd-ai-desktop.exe" >nul 2>&1
)

REM ---- build from source: auto-install Node.js / Rust / C++ tools ----
REM  Everything portable goes under .tools\ next to this file (Node), or the
REM  usual per-user locations (Rust -> %USERPROFILE%\.cargo). The Microsoft
REM  C++ Build Tools need one UAC "Yes" click. No manual downloads required.
set "TOOLS_DIR=%ROOT%.tools"
if exist "%TOOLS_DIR%\path.txt" (
    for /f "usebackq delims=" %%p in ("%TOOLS_DIR%\path.txt") do set "PATH=%%p;!PATH!"
)
if exist "%USERPROFILE%\.cargo\bin\cargo.exe" set "PATH=%USERPROFILE%\.cargo\bin;!PATH!"

set "NEED_TOOLS="
where node >nul 2>&1 || set "NEED_TOOLS=1"
where cargo >nul 2>&1 || set "NEED_TOOLS=1"
if defined NEED_TOOLS (
    echo.
    echo  ==^> No built app found. Installing the build toolchain automatically
    echo      ^(Node.js, Rust, Microsoft C++ Build Tools^). First time only.
    echo      This downloads a few GB and can take 10-20 minutes.
    echo.
    powershell -NoProfile -ExecutionPolicy Bypass -File "%DESKTOP_DIR%\scripts\ensure-toolchain.ps1" -ToolsDir "%TOOLS_DIR%"
    if errorlevel 1 (
        echo.
        echo  [ERROR] Automatic toolchain install failed - scroll up for details.
        echo          Fix the issue ^(usually network or the UAC prompt was declined^)
        echo          and double-click run.bat again. Already-installed parts are skipped.
        echo.
        call :log "ERROR: toolchain install failed"
        pause
        exit /b 1
    )
    if exist "%TOOLS_DIR%\path.txt" (
        for /f "usebackq delims=" %%p in ("%TOOLS_DIR%\path.txt") do set "PATH=%%p;!PATH!"
    )
    if exist "%USERPROFILE%\.cargo\bin\cargo.exe" set "PATH=%USERPROFILE%\.cargo\bin;!PATH!"
)
where node >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] Node.js is still not available on PATH after install. Reopen this window and retry.
    pause
    exit /b 1
)
where cargo >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] Rust ^(cargo^) is still not available on PATH after install. Reopen this window and retry.
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
        call :log "ERROR: npm install failed"
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
        call :log "ERROR: tauri build failed"
        pause
        exit /b 1
    )
)

echo       Starting desktop\src-tauri\target\release\psd-ai-desktop.exe
call :log "launching freshly built app"
"%DESKTOP_DIR%\src-tauri\target\release\psd-ai-desktop.exe"

:done
echo.
echo  ------------------------------------------------------------
echo   psd.ai has closed.
echo.
echo   Tip: press Ctrl+Shift+T inside the app ^(or click the mic in
echo   the left rail^) to open "Talk to psd.ai" - speak in any
echo   language and it answers in English, and it can drive this PC.
echo  ------------------------------------------------------------
call :log "run.bat finished"
endlocal
exit /b 0

REM =================================================================
REM                         subroutines
REM =================================================================

:usage
echo.
echo  psd.ai - one-click setup and launch ^(Windows^)
echo.
echo    run.bat                 normal launch
echo    run.bat --help          this help
echo    run.bat --doctor        check this PC and report what is missing
echo    run.bat --repair        rebuild the venv and reinstall dependencies
echo    run.bat --update        git pull and refresh dependencies
echo    run.bat --rebuild       force-rebuild the desktop app
echo    run.bat --no-voice      skip the Jarvis extras ^(voice + PC control^)
echo    run.bat --no-models     skip the local model group
echo    run.bat --no-app        set everything up, then stop
echo    run.bat --skip-numpy-check  skip the numpy import check
echo.
echo  Logs are written to  logs\run.log
echo.
exit /b 0

:log
REM Append one timestamped line to logs\run.log (best effort, never fatal).
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%" >nul 2>&1
>>"%LOG_FILE%" echo %date% %time%  %~1
exit /b 0

:pip_run
REM %~1 = python interpreter, %~2 = full argument string.
REM Runs pip and retries once on a transient network failure.
set "_PY=%~1"
set "_ARGS=%~2"
"%_PY%" %_ARGS%
if not errorlevel 1 exit /b 0
echo      pip failed - retrying once ^(transient network errors are common^)...
timeout /t 3 /nobreak >nul
"%_PY%" %_ARGS%
exit /b %errorlevel%

:free_space_gb
REM Sets FREE_GB to the free space (rounded, in GB) on the drive that
REM holds this script. Leaves it undefined if PowerShell is unavailable.
set "FREE_GB="
for /f "usebackq delims=" %%i in (`powershell -NoProfile -Command "[math]::Round((Get-PSDrive -Name '%ROOTDRIVE%').Free/1GB)" 2^>nul`) do set "FREE_GB=%%i"
exit /b 0

:verify_numpy
REM errorlevel 0 = "import numpy" finished, 1 = it did not.
REM A slow FIRST import is retried once: that pass warms both the file cache
REM and Defender's scan cache, so the second pass usually takes seconds.
if not defined NP_RETRY set "NP_RETRY=1"
set "NP_ATTEMPT=0"
:verify_numpy_again
set /a NP_ATTEMPT+=1
call :_numpy_once %NUMPY_IMPORT_BUDGET%
if not errorlevel 1 goto :verify_numpy_ok
if %NP_ATTEMPT% GEQ 2 goto :verify_numpy_bad
if "%NP_RETRY%"=="0" goto :verify_numpy_bad
echo      ...no answer after %NP_SECONDS%s - retrying once before calling it stuck
goto :verify_numpy_again
:verify_numpy_bad
exit /b 1
:verify_numpy_ok
exit /b 0

:_numpy_once
REM Runs "import numpy" once in the background, waits up to %1 seconds, and
REM sets NP_SECONDS to how long it took.
REM
REM The `cmd /d /s /c ""...""` wrapping is the actual bug fix. cmd.exe strips
REM the FIRST and LAST quote from a /c command line unless the whole thing is
REM wrapped in an extra pair, so the old single-quoted form turned
REM     "C:\...\python.exe" -c "import numpy"
REM into    C:\...\python.exe" -c "import numpy
REM which fails instantly - the check reported "numpy hangs" on every PC,
REM no matter how healthy numpy was. /s plus doubled quotes keeps it intact.
set "_np_budget=%~1"
if not defined _np_budget set "_np_budget=60"
set "NP_SECONDS=0"
if exist "venv\.np_ok" del /q "venv\.np_ok"
if exist "venv\.np_fail" del /q "venv\.np_fail"
if exist "venv\.np_err" del /q "venv\.np_err"
start /b "" cmd /d /s /c ""%VENVPY%" -c "import numpy" >"venv\.np_err" 2>&1 && (echo ok>"venv\.np_ok") || (echo fail>"venv\.np_fail")"
set /a _w=0
:_numpy_once_wait
if exist "venv\.np_fail" goto :_numpy_once_failed
if exist "venv\.np_ok" (
    set "NP_SECONDS=!_w!"
    exit /b 0
)
if !_w! GEQ !_np_budget! (
    set "NP_SECONDS=!_w!"
    goto :_numpy_once_failed
)
timeout /t 1 /nobreak >nul
set /a _w+=1
if !_w! EQU 15 echo      ...15s ^(a cold venv scans a large OpenBLAS DLL - normal^)
if !_w! EQU 45 echo      ...45s
goto :_numpy_once_wait
:_numpy_once_failed
set "NP_SECONDS=!_w!"
if exist "venv\.np_err" (
    set "_np_lines=0"
    for /f "usebackq tokens=* delims=" %%L in ("venv\.np_err") do (
        set /a _np_lines+=1
        if !_np_lines! LEQ 6 echo        %%L
    )
)
exit /b 1

:numpy_troubleshoot
echo.
echo  [WARN] "import numpy" did not finish in !NP_SECONDS!s inside this venv.
echo.
echo   On Windows that is nearly always one of two things:
echo     1. Windows Defender scanning the freshly installed OpenBLAS DLL
echo        ^(fastembed pulls in a ~40 MB one^) on its very first load - slow,
echo        not broken. The retry above usually proves this.
echo     2. A genuinely stuck OpenBLAS import ^(DLL loader lock, or a clash
echo        with another BLAS/MKL copy earlier on your PATH^).
echo.
echo   Check it by hand in another window:
echo     "%VENVPY%" -c "import numpy; print(numpy.__version__)"
echo.
set "NP_FIX="
set /p "NP_FIX=   Add a Microsoft Defender exclusion for the venv now? [y/N] "
if /i "!NP_FIX!"=="y" (
    call :defender_exclusion
    echo      re-checking...
    call :verify_numpy
    if not errorlevel 1 (
        echo       numpy OK ^(!NP_SECONDS!s^) - the exclusion fixed it.
        exit /b 0
    )
)
set "NP_CONT="
set /p "NP_CONT=   Continue anyway and start psd.ai? [y/N] "
if /i "!NP_CONT!"=="y" (
    echo      Continuing. If numpy really is broken, RAG and semantic search
    echo      will be degraded - run  run.bat --doctor  to check it again.
    call :log "WARN: continuing with a numpy import that did not finish"
    exit /b 0
)
echo.
echo   Add this folder to your antivirus exclusions and double-click run.bat:
echo     %APP_DIR%\venv
echo.
echo   Or rebuild from scratch with:   run.bat --repair
echo   Or skip this check with:        run.bat --skip-numpy-check
echo.
call :log "ERROR: numpy import did not finish"
pause
exit /b 1

:defender_exclusion
echo.
echo  ==^> Microsoft Defender exclusion for the venv
net session >nul 2>&1
if errorlevel 1 (
    echo      This needs an administrator window. Open PowerShell as
    echo      Administrator and run these two lines:
    echo.
    echo        Add-MpPreference -ExclusionPath "%APP_DIR%\venv"
    echo        Add-MpPreference -ExclusionPath "%APP_DIR%"
    echo.
    echo      Then double-click run.bat again.
    exit /b 1
)
powershell -NoProfile -Command "Add-MpPreference -ExclusionPath '%APP_DIR%\venv'"
if errorlevel 1 (
    echo      [WARN] The exclusion command failed - add it by hand in
    echo             Windows Security ^> Virus ^& threat protection ^> Exclusions.
    exit /b 1
)
echo      Excluded: %APP_DIR%\venv
call :log "defender exclusion added for venv"
exit /b 0

:doctor
echo  ============================================================
echo    psd.ai doctor
echo  ============================================================
echo.
echo   Machine: %COMPUTERNAME% ^( %PROCESSOR_ARCHITECTURE% ^)
for /f "skip=1 delims=" %%v in ('wmic os get caption /value 2^>nul ^| find "="') do set "OSLINE=%%v"
if defined OSLINE for /f "tokens=2 delims==" %%v in ("%OSLINE%") do echo   OS:      %%v
echo.

echo  -- Python ----------------------------------------------------
set "_P="
py -3.12 -c "import sys" >nul 2>&1 && set "_P=py -3.12"
if not defined _P py -3.13 -c "import sys" >nul 2>&1 && set "_P=py -3.13"
if not defined _P py -3.11 -c "import sys" >nul 2>&1 && set "_P=py -3.11"
if not defined _P py -3.14 -c "import sys" >nul 2>&1 && set "_P=py -3.14"
if not defined _P python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1 && set "_P=python"
if not defined _P (
    echo   [FAIL]  No Python 3.11+ found. Install it from https://www.python.org/downloads/
) else (
    for /f "delims=" %%i in ('%_P% -c "import platform,sys; print(platform.python_version(), sys.executable)"') do echo   [ OK ]  Python %%i
)

echo.
echo  -- Virtual environment ----------------------------------------
set "_VENV_PY=%APP_DIR%\venv\Scripts\python.exe"
set "_HAVE_VENV=0"
set "VENVPY=%_VENV_PY%"
if exist "%_VENV_PY%" set "_HAVE_VENV=1"
if "%_HAVE_VENV%"=="0" (
    echo   [FAIL]  no venv yet - run run.bat once
) else (
    echo   [ OK ]  venv present: %APP_DIR%\venv
    set "_STAMP_MISSING=0"
    if not exist "%APP_DIR%\venv\.deps_ok" set "_STAMP_MISSING=1"
    if "%_STAMP_MISSING%"=="1" (
        echo   [FAIL]  dependencies missing - run run.bat
    ) else (
        echo   [ OK ]  dependencies installed
    )
    set "_STAMP_MISSING=0"
    if not exist "%APP_DIR%\venv\.jarvis_ok" set "_STAMP_MISSING=1"
    if "%_STAMP_MISSING%"=="1" (
        echo   [ .. ]  Jarvis extras not installed yet
    ) else (
        echo   [ OK ]  Jarvis extras installed
    )
    set "_STAMP_MISSING=0"
    if not exist "%APP_DIR%\venv\.stt_ok" set "_STAMP_MISSING=1"
    if "%_STAMP_MISSING%"=="1" (
        echo   [ .. ]  faster-whisper not installed ^(voice input will use the browser^)
    ) else (
        echo   [ OK ]  faster-whisper installed
    )
)

echo.
echo  -- Microphone -------------------------------------------------
set "MIC_FOUND="
for /f "usebackq delims=" %%i in (`powershell -NoProfile -Command "(Get-CimInstance Win32_SoundDevice ^| Measure-Object).Count" 2^>nul`) do set "MIC_FOUND=%%i"
if not defined MIC_FOUND (
    echo   [ .. ]  Could not query audio devices ^(PowerShell unavailable^).
) else if "%MIC_FOUND%"=="0" (
    echo   [FAIL]  No audio input device found.
    echo           Voice mode needs a microphone.
) else (
    echo   [ OK ]  %MIC_FOUND% audio device^(s^) present.
    echo           If the mic still will not work, check
    echo           Settings ^> Privacy ^> Microphone ^> "Let desktop apps access your microphone".
)

echo.
echo  -- Disk space ------------------------------------------------
call :free_space_gb
if defined FREE_GB (
    echo   [ OK ]  %FREE_GB% GB free on drive %ROOTDRIVE%:
    if !FREE_GB! LSS 8 echo   [WARN]  The local model group wants 10-20 GB.
) else (
    echo   [ .. ]  Could not read free space.
)

echo.
echo  -- Desktop app -------------------------------------------------
if exist "%DESKTOP_DIR%\src-tauri\target\release\psd-ai-desktop.exe" (
    echo   [ OK ]  built app: %DESKTOP_DIR%\src-tauri\target\release\psd-ai-desktop.exe
) else if exist "%DESKTOP_DIR%\psd.ai.exe" (
    echo   [ OK ]  portable app: %DESKTOP_DIR%\psd.ai.exe
) else (
    echo   [ .. ]  no built app - run.bat will build it ^(needs Node + Rust once^).
)
if not defined PSD_AI_RUNTIME_DIR set "PSD_AI_RUNTIME_DIR=%LOCALAPPDATA%\psd.ai\runtime"
if exist "%PSD_AI_RUNTIME_DIR%" (
    echo   [ OK ]  model cache: %PSD_AI_RUNTIME_DIR%
) else (
    echo   [ .. ]  model cache not created yet: %PSD_AI_RUNTIME_DIR%
)

echo.
echo  -- numpy -------------------------------------------------------
if "%_HAVE_VENV%"=="0" (
    echo   [ .. ]  no venv yet - run run.bat once
) else (
    if not defined NUMPY_IMPORT_BUDGET set "NUMPY_IMPORT_BUDGET=60"
    call :_numpy_once %NUMPY_IMPORT_BUDGET%
    if not errorlevel 1 (
        echo   [ OK ]  "import numpy" finished in !NP_SECONDS!s
    ) else (
        echo   [FAIL]  "import numpy" did not finish in !NP_SECONDS!s
        echo           A cold venv is slow ^(Defender scans the OpenBLAS DLL on
        echo           first load^) - run this again: if the second run is fast,
        echo           there is nothing wrong. If it never finishes, exclude
        echo             %APP_DIR%\venv
        echo           in Windows Security ^> Virus ^& threat protection ^> Exclusions.
    )
)

echo.
echo  -- Jarvis / PC control -----------------------------------------
if "%_HAVE_VENV%"=="1" (
    "%_VENV_PY%" -c "import sys; sys.path.insert(0, '.'); from services.computer import get_computer_service as g; import json; print(json.dumps(g().status(), indent=2)[:1200])" 2>nul
    if errorlevel 1 echo   [ .. ]  Run run.bat once before the PC-control report is available.
    "%_VENV_PY%" -c "import importlib.util as u; print(('[ OK ]  faster-whisper installed' if u.find_spec('faster_whisper') else '[ .. ]  faster-whisper missing - voice input will use the browser'))"
) else (
    echo   [ .. ]  venv not created yet.
)

echo.
echo   Log: %LOG_FILE%
echo.
pause
exit /b 0
