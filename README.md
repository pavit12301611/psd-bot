# psd-bot

The project lives in the **`psd.ai/`** folder.

## Run it on Windows

Double-click **`run.bat`** — that's it. It will:

1. find Python 3.11+
2. create a virtual environment & install dependencies (first run only)
3. run first-time setup — you'll be asked for an admin username & password
4. **download and run the best local Llama for your PC** (first run only)
5. open <http://localhost:7000> in your browser
6. start the server

After that, re-running `run.bat` just starts the app. Press `Ctrl+C` in its window to stop.

## The local AI model

Step 4 opens a **second window** titled *psd.ai - local Llama model*. On the first
run it downloads a few GB and then serves the model; on later runs it starts in a
few seconds because everything is cached under `psd.ai/runtime/` (git-ignored).

It measures your RAM, GPU and CPU, then picks the largest Llama that genuinely
runs well on that hardware:

| Your PC | What it picks |
| --- | --- |
| 4 GB RAM | Llama 3.2 1B @ Q6_K, 16k context |
| 8 GB RAM | Llama 3.2 3B @ Q6_K, 16k context |
| 16 GB RAM, integrated graphics | Llama 3.1 8B @ Q5_K_M, 16k context |
| 32 GB + 8 GB GPU | Llama 3.1 8B @ Q5_K_M, 16k context (full GPU offload) |
| 64 GB + 24 GB GPU | Llama 3.1 8B @ Q8_0, 16k context (full GPU offload) |
| 96 GB + 48 GB GPU | Llama 3.3 70B @ Q4_K_M, 16k context (full GPU offload) |

"Best" means best *usable* answer, not the biggest file:

- **Context window first, quality second.** A slightly smaller quant running at
  16k beats a sharper one stuck at 4k, and the smaller file also replies faster.
- It never picks F16/BF16 over Q8_0 (twice the download, half the speed, no
  real quality gain), and it drops to a smaller model rather than run one at a
  crippled 2048-token context.
- The KV cache is estimated per model, not with a single flat number, so the
  8B and the 70B are both given a realistic window.

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

The model is registered as **psd.ai Local Llama** and set as your default chat
model — but only if you have not already chosen one, so an existing setup is
never overwritten.

**Keep that second window open** while you use psd.ai; closing it stops the model.

### Options

| Do this | To |
| --- | --- |
| set `PSD_NO_LOCAL_MODEL=1` | skip the local model entirely and bring your own |
| set `LLAMA_PORT=9090` | serve the model on a different port (default 8080) |
| run `python scripts\local_llama.py --print` | show what your PC would get, no downloads |
| run `python scripts\local_llama.py --model llama-3.2-3b` | force a specific model |

If the download or the GPU start fails, psd.ai still opens — the failure is
reported in that window and you can add a model under **Settings → Models**.

## Branding

The app is presented as **psd.ai** everywhere it is visible in a browser: the tab
title, the login page, the sidebar, the welcome screen, and the PWA install name.

Full manual instructions (Linux/macOS/Docker) are in [`psd.ai/README.md`](psd.ai/README.md) and [`psd.ai/website/setup.md`](psd.ai/website/setup.md).
