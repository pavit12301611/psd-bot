# psd.ai desktop app

A native desktop application for psd.ai — **Tauri 2 + React + Vite + Tailwind**.
There is no browser tab and no `localhost` URL to open: the app window *is* the
product.

```
┌──────────────────────────────────────────────────────────────┐
│  psd.ai window (Tauri WebView)                               │
│    React UI  ──invoke("api_request"/"api_stream")──►  Rust   │
│                                                        │     │
│                          private loopback HTTP (random port) │
│                                                        ▼     │
│              psd.ai/desktop_server.py  (Python sidecar)      │
└──────────────────────────────────────────────────────────────┘
```

* **Front-end → Python via local IPC.** The WebView never fetches a URL.
  Every call goes through Tauri `invoke()` to Rust, which owns the connection
  to the Python sidecar (`src-tauri/src/lib.rs`). Server-sent events are
  forwarded as Tauri events, so chat streams token-by-token.
* **Sidecar.** Rust spawns `psd.ai/desktop_server.py`, which binds to a
  *random* loopback port, prints `PSD_AI_READY port=NNNN` once, and exits
  when the app closes its stdin pipe. Nothing is bound to `0.0.0.0` and no
  fixed port is used.
* **UI.** Frameless window with custom title bar, animated boot / setup /
  login screens, sidebar with grouped chats, model picker, streaming
  markdown with code highlighting, reasoning & tool-call disclosure panels,
  agent approval cards, attachments, settings (endpoints, account, engine
  log), light/dark theme. All animation is Motion + Tailwind.

## Run it

**On Fedora:** `./run.sh` from the repo root. It installs the build
prerequisites with `dnf`, creates `psd.ai/venv`, downloads and starts the local
model group, then launches this app (building it the first time).

```bash
./run.sh                 # everything: deps, models, server, app window
./run.sh --doctor        # report what is missing, with the dnf lines to fix it
./run.sh --no-desktop    # headless server only, no app window
```

**Developers:**

```bash
# prerequisites, Fedora:
sudo dnf install nodejs npm rust cargo patchelf \
                 webkit2gtk4.1-devel gtk3-devel glib2-devel libsoup3-devel \
                 javascriptcoregtk4.1-devel alsa-lib-devel librsvg2-devel
cd desktop
npm install
npm run tauri dev        # hot-reloading app window
npm run tauri build      # bundle/rpm/*.rpm + bundle/appimage/*.AppImage
```

Or install the packaged build: `packaging/psd-ai.spec` produces an RPM that
puts the venv in `/usr/lib/psd.ai` and the window in your application menu
(`packaging/psd-ai.desktop`).

The Rust side finds the Python project via `PSD_AI_APP_DIR` (set by `run.sh`),
or by walking up from the executable / cwd looking for
`psd.ai/desktop_server.py`. For the interpreter it prefers, in order:
`PSD_AI_PYTHON`, `psd.ai/venv/bin/python`, `.venv/bin/python`, the packaged
`/usr/lib/psd.ai/venv/bin/python`, then `/usr/bin/python3`. Each candidate has
to be an executable file, so a venv left half-built by an interrupted run is
skipped rather than trusted.

The sidecar is spawned in its own process group and the whole group is
terminated when the last window closes, so a `llama-server` it started cannot
outlive the app and hold on to the GPU.

## UI-only development (no Rust toolchain)

You can iterate on the React UI in a normal browser; the IPC bridge falls
back to same-origin `fetch` and Vite proxies `/api` to a manually started
sidecar:

```bash
cd psd.ai && PSD_AI_DESKTOP_STANDALONE=1 PSD_AI_DESKTOP_PORT=7311 python desktop_server.py
cd desktop && PSD_AI_DEV_BACKEND=7311 npm run dev
```

This mode exists for development only; the shipped app always uses IPC.

## Layout

```
desktop/
├─ src/
│  ├─ lib/ipc.ts        # the ONLY module that talks to the backend
│  ├─ lib/api.ts        # typed wrappers for psd.ai routes
│  ├─ store/app.ts      # zustand state + streaming reducer
│  ├─ components/       # TitleBar, Boot, Auth, Sidebar, Chat, Message,
│  │                    # Composer, ModelPicker, Settings
│  └─ index.css         # Tailwind v4 theme tokens + component classes
├─ src-tauri/
│  ├─ src/lib.rs        # sidecar lifecycle + api_request / api_stream IPC
│  ├─ tauri.conf.json   # frameless window, CSP, bundle targets
│  └─ capabilities/     # least-privilege window permissions
└─ vite.config.ts
```
