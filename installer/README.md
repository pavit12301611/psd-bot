# psd.ai graphical installer

A **native installer window** for Fedora Linux - the thing that shows a real
progress bar while psd.ai sets itself up, like a proper desktop installer.
No browser, no localhost page: a Tauri v2 window that owns the whole
install, then launches the psd.ai desktop app and supervises it.

```
installer/
  src-tauri/          Rust backend (the installer engine)
    src/main.rs       steps, progress parsing, fault cards, supervisor
    tauri.conf.json   window config (1060x740, CSP 'self', no bundling)
    capabilities/     core:default only (invoke + listen)
    icons/            window icons (copied from desktop/)
  ui/                 frontend - plain HTML/CSS/JS, no npm, no build step
    index.html        layout: banner, steps, models card, faults, console
    app.js            renders the state snapshot, sends actions
    style.css         dark theme matching the psd.ai brand
```

## How it is used

`./run.sh` is still the entry point. On a desktop session it:

1. installs the system packages incl. the Rust toolchain (dnf, first run),
2. **builds this installer once** (`cargo build --release`, a few minutes
   the first time, seconds after - the binary is cached in
   `src-tauri/target/release/psd-ai-installer`),
3. **opens the installer window** and hands over; run.sh reports the
   installer's exit code and exits.

The installer is only used when there is actually work to do (missing
venv, missing deps stamp, missing app binary, `--repair`). An already
installed psd.ai skips straight to the app, as before. It is also skipped
when there is no display, no cargo, or when `--no-gui` is passed - then
the same steps run in the terminal with the browser dashboard
(`psd.ai/scripts/install_gui.py`) as fallback progress view.

## What the installer runs

| step    | command                                                       | fatal |
|---------|---------------------------------------------------------------|-------|
| venv    | `python -m venv venv` (only when missing)                     | yes   |
| deps    | `venv/bin/python -m pip install -r requirements.txt`          | yes   |
| extras  | `... -r requirements-jarvis.txt` (voice + PC control)         | no    |
| stt     | `... install faster-whisper` (local speech-to-text)           | no    |
| setup   | `venv/bin/python setup.py` (data dirs, admin-account hint off)| yes   |
| build   | `npm install` + `npm run tauri build -- --no-bundle`          | no*   |
| models  | `venv/bin/python scripts/local_llama.py --foreground`         | no    |
| launch  | start the built desktop app (or a headless engine fallback)   | -     |

\* if the desktop app cannot be built, the installer warns and falls back
to running the Python engine headless (`uvicorn`) and opening the browser
dashboard URL - the install still finishes.

Skipped steps (already done) are shown as skipped, so re-running is fast.
The model group starts **after** deps are installed (the bootstrap imports
the app's database module) and downloads run with their own progress bar,
resume-after-stall retries and per-model cards.

## Faults

Every failure becomes a **fault card**: step name, exit code, message, a
fix hint, and the suspicious lines from the failing command's output
(expandable). Fatal faults pause the install and offer
**Retry step** / **Skip step** / **Quit**; non-fatal faults are listed as
warnings and the install continues. Models that fail or time out get a
Retry on their card.

## UI contract (for anyone touching ui/ or main.rs)

- backend command `get_state` -> full state snapshot (JSON)
- backend command `send_action` with `{action, step}`;
  actions: `retry` | `skip` | `launch` | `stop` | `quit`
- backend event `"state"` pushes a fresh snapshot (throttled >= 150 ms)
- phases: `installing` -> `waiting_models` -> `ready` -> `running_app`
  -> `closed` (relaunchable); `failed` on a fatal fault
- the frontend must stay offline-safe: CSP is `default-src 'self'`, no
  CDN fonts/scripts, no bundler (`withGlobalTauri` provides
  `window.__TAURI__`)

## Environment (set by run.sh)

| variable                  | meaning                              | default            |
|---------------------------|--------------------------------------|--------------------|
| `PSD_INSTALL_APP_DIR`     | `psd.ai/` working dir                | derived from exe   |
| `PSD_INSTALL_DESKTOP_DIR` | `desktop/` dir                       | `<repo>/desktop`   |
| `PSD_INSTALL_LOG_DIR`     | where installer.log is written       | `<repo>/logs`      |
| `PSD_INSTALL_PYCMD`       | system python used to create the venv| `python3`          |
| `PSD_INSTALL_LLAMA_PORT`  | first port of the model group        | `8080`             |
| `PSD_INSTALL_MODEL_WAIT`  | seconds to wait for first download   | `2400`             |
| `PSD_INSTALL_NO_VOICE`    | `1` skips the jarvis extras          | `0`                |
| `PSD_INSTALL_NO_MODELS`   | `1` skips the local model group      | `0`                |
| `PSD_INSTALL_NO_STT`      | `1` skips faster-whisper             | `0`                |
| `APP_BIND` / `APP_PORT`   | headless engine fallback bind        | `127.0.0.1` / `7000` |

Without the variables the installer walks up from its own binary path to
find the repo root (a dir containing `run.sh` + `psd.ai/`), so running it
by hand from the repo works too.

## Building / running by hand

```bash
cd installer/src-tauri
cargo build --release                      # needs rust + webkit2gtk4.1-devel
PSD_INSTALL_LOG_DIR=../../logs \
  ./target/release/psd-ai-installer        # opens the window
```

## Supervision semantics

- every spawned command runs in its own **process group**
  (`process_group(0)`); quitting the installer sends TERM (then KILL) to
  the whole group, so no orphan pip/npm/llama-server survives.
- closing the window = quit everything (installer, models, app).
- when the launched app exits, the installer stops the model group and
  offers **Launch psd.ai again**; quitting from the footer exits.
- all output is mirrored to `logs/installer.log`; the model track is
  additionally mirrored to `logs/local-model.log` (same file the desktop
  app uses), so both UIs read the same truth.
