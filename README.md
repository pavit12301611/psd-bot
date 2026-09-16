# psd-bot

The project lives in the **`psd.ai/`** folder.

## Run it on Windows

Double-click **`run.bat`** — that's it. It will:

1. find Python 3.11+
2. create a virtual environment & install dependencies (first run only)
3. run first-time setup — you'll be asked for an admin username & password
4. **download and run a hardware-fit group of 3 to 5 local models** (first run only)
5. open <http://localhost:7000> in your browser
6. start the server

After that, re-running `run.bat` just starts the app. Press `Ctrl+C` in its window to stop.

## The local AI model group

Step 4 opens a **second window** titled *psd.ai - local model group*. On the
first run it downloads three to five hardware-fit GGUF models concurrently and
runs one `llama-server` per model on ports starting at `8080`. Later launches
reuse everything cached under `psd.ai/runtime/` (git-ignored), so they start in
a few seconds.

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
| run `python scripts\local_llama.py --print` | show the hardware-fit group, no downloads |
| run `python scripts\local_llama.py --model llama-3.2-3b` | prefer a model while filling the group |
| run `python scripts\local_llama.py --single-model` | use the legacy one-model mode |

If the download or the GPU start fails, psd.ai still opens — the failure is
reported in that window and you can add a model under **Settings → Models**.

## Branding

The app is presented as **psd.ai** everywhere it is visible in a browser: the tab
title, the login page, the sidebar, the welcome screen, and the PWA install name.

Full manual instructions (Linux/macOS/Docker) are in [`psd.ai/README.md`](psd.ai/README.md) and [`psd.ai/website/setup.md`](psd.ai/website/setup.md).
