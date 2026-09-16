# psd-bot

The project lives in the **`psd.ai/`** folder.

psd.ai is a **native desktop app** now: one window with chat, documents,
notes, tasks, calendar, email, gallery, research, models and settings. There
is no browser page and no localhost server window — the whole workspace engine
runs in-process inside the app.

## Run it on Windows

Double-click **`run.bat`** — that's it. It will:

1. find Python 3.11+
2. create a virtual environment & install dependencies, including Qt (first run only)
3. run first-time setup — data folders, database, `.env`
4. open the **psd.ai desktop window**

On the very first start the window shows a *"Create your admin account"*
screen — pick a username and password there. Later launches sign straight in
on that machine. Close the window (or the tray icon) to stop everything.

Hinglish step-by-step guide: [`GUI_KAISE_CHALAYE.md`](GUI_KAISE_CHALAYE.md).

### Inside the app

| Area | What it does |
| --- | --- |
| **Chat** | streaming answers, agent tools, approvals, attachments, sessions |
| **Documents / Notes / Tasks / Calendar / Email / Gallery** | the full workspace, editable |
| **Research** | deep-research runs and the report library |
| **Models** | API endpoints (OpenAI-style, local, custom) + default chat model |
| **Local Models** | hardware-fit GGUF picks, downloads with progress, llama.cpp server group start/stop — all in-app |
| **Memory / Skills / MCP Servers** | what the agent remembers and can use |
| **Settings** | themes, fonts, account & 2FA, assistant providers, data export/import |
| **Diagnostics** | service health + the live application log (no console window needed) |

## The local AI model group

**Local Models** in the sidebar measures your RAM, GPU and CPU, then lists the
strongest GGUF models that can stay resident together. *Download selected*
fetches the weights with live progress, *Start model group* serves one
`llama-server` per model (ports from 8080) and registers each as an endpoint —
and its output stays in the app's log pane instead of a second console window.
*Stop* shuts every server down. Everything caches under `psd.ai/runtime/`
(git-ignored), so later starts take seconds.

The group never has fewer than three models and never exceeds five:

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
- F16/BF16 is avoided in favour of faster Q8/Q6/Q5/Q4 quantisations, and the
  first model in the group becomes the default chat model (unless you already
  picked one).
- Servers launch with `--flash-attn on` and a `q8_0` KV cache, which roughly
  halves the memory the conversation history costs — that is what buys the 16k
  window on a 16 GB laptop.

### Hybrid Intel CPUs (12th gen and later)

Chips like the i7-13620H mix fast **P-cores** with slow **E-cores**. Token
generation is memory-bound and does *not* scale past the P-cores, while the
E-cores drag every worker down to their speed — measured losses of 20–30%. So
the model group counts the P-cores and uses exactly that many threads (6 on a
13620H), rather than the 16 that Windows reports.

### Integrated graphics

An Intel UHD or Iris Xe adapter reports itself as a GPU with ~128 MB of
dedicated VRAM. Nothing useful offloads to that, so the app treats the machine
as CPU-only and downloads the smaller CPU build instead of the Vulkan one.

### Options

| Do this | To |
| --- | --- |
| skip *Local Models* entirely | just don't press *Start model group* — bring your own endpoint under **Models** |
| run `python scripts\local_llama.py --print` | show the hardware-fit group in a terminal, no downloads |
| run `python scripts\local_llama.py --model llama-3.2-3b` | prefer a model while filling the group |
| run `python scripts\local_llama.py --single-model` | use the legacy one-model mode |

If a download or a GPU start fails, the rest of the app keeps working — the
failure is shown in the Local Models log pane and you can add any model under
**Models**.

## Branding

The app is presented as **psd.ai** everywhere it is visible: the desktop
window title, tray icon, first-run screen, sidebar, and the legacy web assets
(tab title, login page, PWA install name).

Full manual instructions (Linux/macOS/Docker, plus the legacy web front end)
are in [`psd.ai/README.md`](psd.ai/README.md) and
[`psd.ai/website/setup.md`](psd.ai/website/setup.md).
