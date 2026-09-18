# psd-bot

The backend lives in the **`psd.ai/`** folder; the desktop application lives in
**`desktop/`** (Tauri 2 + React/Vite + Tailwind).

psd.ai is a **desktop app**. There is no browser tab and no `localhost` URL to
open — the app window starts the Python engine privately inside itself and
talks to it over local IPC (see [`desktop/README.md`](desktop/README.md)).

## Talk to psd.ai (voice + PC control)

psd.ai ships a **Jarvis-style voice mode**. Open it with **Ctrl+Shift+T**, from
the microphone button in the left rail, or from the command palette.

* **Speak in any language — it always answers in English, out loud.** Hindi,
  Hinglish, anything: the reply is pinned to English and written for a
  text-to-speech engine (no markdown, no emoji, no bullet lists).
* **It actually does the work.** "open notepad", "turn the volume up",
  "press ctrl+s", "type hello", "what's my battery", "close chrome" — the
  request is understood, executed on your desktop, and reported back in one
  short sentence.
* **Hold Space** (push to talk) or switch on **Continuous** for a hands-free
  back-and-forth. Silence ends the turn.
* Voice → text happens locally with **faster-whisper** when it's installed; the
  browser's speech API is the fallback. Replies use the engine's TTS when you
  configure one, otherwise the browser speaks them.

The same PC control is available in typed chat: the agent gets
`computer_control` / `computer_screen` tools, so "open the calculator and type
1234" works without saying a word.

### What it can do on your PC

| Group | Actions |
| --- | --- |
| See | `screenshot`, `screen_size`, `windows`, `info` (CPU/RAM/disk/battery), `processes`, `clipboard_get` |
| Drive | `click`, `move`, `drag`, `scroll`, `type`, `key` (shortcuts like `ctrl+s`) |
| Apps | `open` (app, file or URL), `focus`, `close_window`, `kill` |
| System | `volume` (level / up / down / mute), `notify`, `clipboard_set`, `wait` |

### Safety rails

You asked for full access, so full access is the default — with a short list of
things psd.ai will refuse **no matter what you say**:

* no formatting or wiping a drive, no deleting system directories
* no boot/firmware changes, no shutting the PC down
* it can never end its own process, the desktop shell, or core Windows services

Everything else runs. If you want a prompt before anything risky (ending a
process), switch **Autonomy → Ask first** in **Settings → Voice & PC**. Turn
**PC control** off there to refuse every mouse, keyboard and window action while
keeping chat and voice intact.

## Run it on Windows

Double-click **`run.bat`** — that's it. It will:

1. find Python 3.11+
2. create a virtual environment & install dependencies (first run only)
3. install the **Jarvis extras** — offline speech-to-text plus the PC-control
   packages (`psd.ai/requirements-jarvis.txt`, first run only, warnings only
   if it fails)
4. run first-time setup (data folders, database, `.env`)
5. **download and run a hardware-fit group of 3 to 5 local models** (first run only)
6. **open the psd.ai desktop app** — on the very first launch it asks you to
   create your admin account right in the window

### run.bat options

| Command | What it does |
| --- | --- |
| `run.bat --help` | list the options |
| `run.bat --doctor` | check this PC: Python, venv, microphone, disk space, built app, PC-control status |
| `run.bat --repair` | delete the venv and reinstall everything from scratch |
| `run.bat --update` | `git pull` then refresh dependencies |
| `run.bat --rebuild` | throw away the built app and rebuild it |
| `run.bat --no-voice` | skip the Jarvis extras |
| `run.bat --no-models` | skip the local model group for this run |
| `run.bat --no-app` | set everything up, then stop without opening the window |

`run.bat` also keeps a timestamped log in **`logs/run.log`**, warns you when
there isn't enough disk space for the model group, and retries a failed `pip`
install once before giving up.

`run.bat` uses a prebuilt app if one is present (`desktop\psd.ai.exe` or
`desktop\src-tauri\target\release\psd-ai-desktop.exe`). Otherwise it **installs
the build toolchain itself** — portable Node.js (into `.tools\`), Rust (via
rustup), and the Microsoft C++ Build Tools (one UAC prompt) — and builds the
app once. After that, re-running `run.bat` just opens the app. Close the
window to stop.

## The local AI model group

Step 4 opens a **second window** titled *psd.ai - local model group*. On the
first run it downloads three to five hardware-fit GGUF models concurrently and
runs one `llama-server` per model on ports starting at `8080`. Everything is
cached **on the device, outside the project folder** — `%LOCALAPPDATA%\psd.ai\runtime`
on Windows (`~/Library/Application Support/psd.ai/runtime` on macOS,
`~/.local/share/psd.ai/runtime` on Linux; override with `PSD_AI_RUNTIME_DIR`).
So you can delete or re-download the code as often as you like and later
launches still start in a few seconds. An old `psd.ai/runtime/` folder is
moved there automatically.

It measures your RAM, GPU and CPU, then selects the strongest group that can
stay resident together. The group never has fewer than three models and never
exceeds five:

| Your PC | Typical resident group |
| --- | --- |
| 4 GB RAM | 3 tiny models: SmolLM2 135M, Qwen 0.5B, and a lightweight SmolLM2/Qwen build |
| 8 GB RAM | 4–5 models: Llama 3.2 3B plus Qwen, Llama 1B, and tiny fallbacks |
| 16 GB RAM | 5 models: Llama 3.1 8B, Llama 3.2 3B, Qwen 1.5B, Llama 1B, and a tiny fallback |
| 32 GB + 8 GB GPU | Up to 5 models with GPU offload where the VRAM budget allows |
| 64 GB + 24 GB GPU | Up to 5 models, including the largest model that fits |

Selection is based on the **combined** weights, KV cache, GPU/CPU memory, and
server headroom—not just whether each model fits individually:

- **Context window first, quality second.** A smaller quant with a usable context
  beats a sharper model that would starve the other group members.
- Weight downloads run in parallel, while each server gets its own local port
  and endpoint in **Settings → Models**.
- F16/BF16 is avoided in favour of faster Q8/Q6/Q5/Q4 quantisations, and the
  first model in the group becomes the default chat model.

The server itself is launched with `--flash-attn on` and a `q8_0` KV cache,
which roughly halves the memory the conversation history costs — that is what
buys the 16k window on a 16 GB laptop.

### Hybrid Intel CPUs (12th gen and later)

Chips like the i7-13620H mix fast **P-cores** with slow **E-cores**. Token
generation is memory-bound and does *not* scale past the P-cores, while the
E-cores drag every worker down to their speed — measured losses of 20–30%. So
the launcher counts the P-cores and uses exactly that many threads (6 on a
13620H), rather than the 16 that Windows reports.

### Integrated graphics

An Intel UHD or Iris Xe adapter reports itself as a GPU with ~128 MB of
dedicated VRAM. Nothing useful offloads to that, so the launcher treats the
machine as CPU-only and downloads the smaller CPU build instead of the Vulkan
one.

Every model is registered as a separate **psd.ai Local Llama** endpoint. The
strongest selected model becomes the default chat model — but only if you have
not already chosen one, so an existing setup is never overwritten.

**Keep that second window open** while you use psd.ai; closing it stops the whole group.

### Options

| Do this | To |
| --- | --- |
| set `PSD_NO_LOCAL_MODEL=1` | skip the local model group entirely and bring your own |
| set `LLAMA_PORT=9090` | use 9090 as the first model port (the group uses the next ports too) |
| set `PSD_AI_RUNTIME_DIR=D:\ai-models` | keep llama.cpp + model weights somewhere else (e.g. a bigger drive) |
| run `python scripts\local_llama.py --print` | show the hardware-fit group, no downloads |
| run `python scripts\local_llama.py --model llama-3.2-3b` | prefer a model while filling the group |
| run `python scripts\local_llama.py --single-model` | use the legacy one-model mode |
| set `PSD_NO_LOCAL_STT=1` | skip the local Whisper download (voice input uses the browser) |
| set `PSD_AI_COMPUTER_CONTROL=0` | refuse every mouse / keyboard / window action |

If the download or the GPU start fails, psd.ai still opens — the failure is
reported in that window and you can add a model under **Settings → Models**
inside the app.

## Desktop app (all platforms)

```bash
cd desktop
npm install
npm run tauri dev      # run
npm run tauri build    # installers (.exe/.msi, .dmg, .deb/.AppImage)
```

Requires Node.js 18+ and Rust; see [`desktop/README.md`](desktop/README.md).

## Branding

The app is presented as **psd.ai** everywhere: the window title, the setup and
login screens, the sidebar and the welcome screen.

Headless / server / Docker instructions for the backend alone are in
[`psd.ai/README.md`](psd.ai/README.md) and [`psd.ai/website/setup.md`](psd.ai/website/setup.md).
