#!/usr/bin/env python3
"""psd.ai — local model bootstrap (Linux / Fedora).

One command that makes the app work offline out of the box:

  1. detect this machine's hardware (RAM / CPU / GPU / session)
  2. choose the strongest group of 3–6 models that fits together
  3. download llama.cpp (prebuilt llama-server for Linux) — first run only
  4. download the GGUF weights concurrently (resumable) — first run only
  5. start one llama-server per model on consecutive local ports
  6. register every endpoint in the app and make the first one the default

``run.sh`` calls this before launching the app, so a fresh Fedora install ends
up with a working local model instead of an empty model picker.

Design notes
------------
* Linux is the only supported platform. The prebuilt ``llama-server`` comes
  from the llama.cpp ``ubuntu-*`` release assets (``.tar.gz``), which run on
  any glibc ≥ 2.35 distribution — Fedora included — and a CUDA / ROCm /
  SYCL / Vulkan build is picked when this machine has a GPU that can use it.
* Selection is a pure function (``plan_model``) over the detected hardware and
  the file list Hugging Face reports, so it is unit-testable with no network.
* Multi-part GGUFs (``*-00001-of-00004.gguf``) are first-class: every part is
  downloaded, which is what makes the 100B+ MoE models in the catalogue usable
  at all — the big quantisations are never shipped as one file.
* Mixture-of-experts models are planned with their *active* parameter count for
  KV-cache maths and can keep attention on the GPU while the expert weights
  stay in RAM (``--n-cpu-moe``), so a 35B MoE is a realistic pick on a laptop.
* Downloads use only the stdlib (urllib + Range requests), so this runs on a
  bare venv with nothing but the app's own requirements installed.
* Every network/subprocess step is optional: if anything fails the caller is
  told, and run.sh still starts the app.
"""

from __future__ import annotations

import argparse
import atexit
import importlib
import importlib.util
import json
import math
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

APP_NAME = "psd.ai"
DEFAULT_PORT = int(os.getenv("PSD_LLAMA_PORT", "8080") or "8080")

# psd.ai is a Linux-only application: every launcher, every OS integration and
# every prebuilt binary it fetches is Linux. Failing loudly here beats the
# alternative — silently planning a machine we cannot drive (no Wayland/X11
# control, no llama.cpp asset, no venv layout).
IS_LINUX = sys.platform.startswith("linux")


def _require_linux() -> None:
    if IS_LINUX:
        return
    raise SystemExit(
        f"psd.ai runs on Linux only (this interpreter reports '{sys.platform}').\n"
        "On Fedora: ./run.sh\n"
        "See README.md for the Linux install steps."
    )


def _default_runtime_dir() -> Path:
    """Device-level home for llama.cpp + model weights.

    Lives OUTSIDE the project folder so re-downloading / re-extracting the code
    never throws away multi-GB models:

      Linux : $XDG_DATA_HOME/psd.ai/runtime  (~/.local/share/psd.ai/runtime)

    Override with PSD_AI_RUNTIME_DIR (e.g. a second NVMe or a btrfs subvolume).
    A legacy ``psd.ai/runtime`` folder is moved here automatically the first
    time.
    """
    override = os.getenv("PSD_AI_RUNTIME_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path(os.getenv("XDG_DATA_HOME") or (Path.home() / ".local" / "share")) / "psd.ai" / "runtime"


def _migrate_legacy_runtime(target: Path) -> None:
    """One-time move of the old in-repo ``runtime/`` into the device folder."""
    legacy = BASE_DIR / "runtime"
    try:
        if not legacy.is_dir() or legacy.resolve() == target.resolve():
            return
        if not any(legacy.iterdir()):
            return
        target.mkdir(parents=True, exist_ok=True)
        for item in legacy.iterdir():
            dest = target / item.name
            if dest.exists():
                # keep whatever is already in the device folder, drop the copy
                if item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    item.unlink(missing_ok=True)
                continue
            shutil.move(str(item), str(dest))
        try:
            legacy.rmdir()
        except OSError:
            pass
        print(f"  [ok] moved existing models from {legacy} to {target}")
    except Exception as exc:  # never block startup on a housekeeping move
        print(f"  [warn] could not migrate legacy runtime folder: {exc}")


RUNTIME_DIR = _default_runtime_dir()
_migrate_legacy_runtime(RUNTIME_DIR)
MODELS_DIR = RUNTIME_DIR / "models"
LLAMA_DIR = RUNTIME_DIR / "llama.cpp"
LOG_FILE = RUNTIME_DIR / "llama-server.log"
STATE_FILE = RUNTIME_DIR / "local_model.json"
FAIL_FILE = RUNTIME_DIR / "local_model_failed.txt"

HF_API = os.getenv("PSD_HF_API", "https://huggingface.co/api")
# Weight downloads must come from the same origin as the metadata, otherwise a
# mirror (hf-mirror.com, a corporate proxy, a test double) would list files the
# downloader then fetches from huggingface.co anyway.
HF_ORIGIN = os.getenv("PSD_HF_ORIGIN", "") or HF_API.rstrip("/").removesuffix("/api")
GH_API = os.getenv("PSD_GH_API", "https://api.github.com")
USER_AGENT = "psd.ai-local-llama/1.0"

# ── Model catalogue ──────────────────────────────────────────────────────────
# Newest/strongest first. Each entry lists the HF GGUF mirrors to try, in
# order; the first one that resolves wins (an unreachable or renamed mirror is
# a warning, never a failure — the planner just uses the next one).
# ``max_context`` is the model's trained context, not what we will run with.
#
# ``params_b`` is the TOTAL parameter count and drives the quality score;
# ``active_params_b`` is what runs per token on a mixture-of-experts model and
# drives both the KV-cache estimate and the speed expectation. That split is
# the whole reason a 2026 catalogue can be dramatically stronger than the old
# one on the same laptop: a 35B-A3B MoE has the knowledge of a 35B model and
# the token cost of a 3B one, so it is a realistic *default* rather than a
# machine-melting luxury.
@dataclass(frozen=True)
class ModelSpec:
    id: str
    label: str
    family: str
    params_b: float
    repos: Tuple[str, ...]
    max_context: int
    active_params_b: float = 0.0     # 0 → dense model (active == total)
    moe: bool = False                # experts can be pinned to RAM (--n-cpu-moe)
    vision: bool = False             # repo ships an mmproj we can attach
    thinking: bool = False           # emits reasoning we should surface
    tier: str = "general"            # general | coding | reasoning

    @property
    def kv_params_b(self) -> float:
        """Parameter count the KV-cache cost actually scales with."""
        return self.active_params_b or self.params_b

    @property
    def alias(self) -> str:
        """Endpoint alias — carries `-vision` when the model can see images."""
        base = f"psd-{self.id}"
        return f"{base}-vision" if self.vision else base


MODEL_SPECS: Tuple[ModelSpec, ...] = (
    # ── Frontier / workstation (64 GB+ RAM or 24 GB+ VRAM) ───────────────────
    ModelSpec(
        id="qwen3.5-122b-a10b",
        label="Qwen3.5 122B A10B",
        family="qwen3.5",
        params_b=122.0,
        active_params_b=10.0,
        moe=True,
        thinking=True,
        repos=(
            "unsloth/Qwen3.5-122B-A10B-GGUF",
            "bartowski/Qwen3.5-122B-A10B-GGUF",
        ),
        max_context=262144,
    ),
    ModelSpec(
        id="kimi-k3",
        label="Kimi K3",
        family="kimi-k3",
        params_b=1000.0,
        active_params_b=32.0,
        moe=True,
        thinking=True,
        repos=(
            "unsloth/Kimi-K3-GGUF",
            "bartowski/moonshotai_Kimi-K3-GGUF",
        ),
        max_context=262144,
    ),
    ModelSpec(
        id="gpt-oss-120b",
        label="gpt-oss 120B",
        family="gpt-oss",
        params_b=117.0,
        active_params_b=5.1,
        moe=True,
        thinking=True,
        repos=(
            "unsloth/gpt-oss-120b-GGUF",
            "ggml-org/gpt-oss-120b-GGUF",
            "bartowski/openai_gpt-oss-120b-GGUF",
        ),
        max_context=131072,
    ),
    ModelSpec(
        id="qwen3-coder-next",
        label="Qwen3-Coder-Next",
        family="qwen3-coder",
        params_b=80.0,
        active_params_b=3.0,
        moe=True,
        tier="coding",
        repos=(
            "unsloth/Qwen3-Coder-Next-GGUF",
            "Qwen/Qwen3-Coder-Next-GGUF",
            "bartowski/Qwen3-Coder-Next-GGUF",
        ),
        max_context=262144,
    ),
    ModelSpec(
        id="qwen3-next-80b-a3b",
        label="Qwen3-Next 80B A3B Instruct",
        family="qwen3-next",
        params_b=80.0,
        active_params_b=3.0,
        moe=True,
        repos=(
            "unsloth/Qwen3-Next-80B-A3B-Instruct-GGUF",
            "Qwen/Qwen3-Next-80B-A3B-Instruct-GGUF",
            "bartowski/Qwen3-Next-80B-A3B-Instruct-GGUF",
        ),
        max_context=262144,
    ),
    ModelSpec(
        id="glm-5.3-flash",
        label="GLM 5.3 Flash",
        family="glm-5.3",
        params_b=106.0,
        active_params_b=12.0,
        moe=True,
        thinking=True,
        tier="coding",
        repos=(
            "unsloth/GLM-5.3-Flash-GGUF",
            "zai-org/GLM-5.3-Flash-GGUF",
            "bartowski/zai-org_GLM-5.3-Flash-GGUF",
        ),
        max_context=202752,
    ),
    # ── 32–48 GB workstations and 24 GB GPUs ────────────────────────────────
    ModelSpec(
        id="qwen3.6-35b-a3b",
        label="Qwen3.6 35B A3B",
        family="qwen3.6",
        params_b=35.0,
        active_params_b=3.0,
        moe=True,
        repos=(
            "unsloth/Qwen3.6-35B-A3B-GGUF",
            "Qwen/Qwen3.6-35B-A3B-GGUF",
            "AtomicChat/Qwen3.6-35B-A3B-GGUF",
            "bartowski/Qwen3.6-35B-A3B-GGUF",
        ),
        max_context=262144,
    ),
    ModelSpec(
        id="gemma-4-31b",
        label="Gemma 4 31B",
        family="gemma-4",
        params_b=33.0,
        vision=True,
        repos=(
            "unsloth/gemma-4-31B-it-GGUF",
            "google/gemma-4-31B-it-GGUF",
            "bartowski/gemma-4-31B-it-GGUF",
        ),
        max_context=131072,
    ),
    ModelSpec(
        id="glm-4.7-flash",
        label="GLM 4.7 Flash 30B A3B",
        family="glm-4.7",
        params_b=30.0,
        active_params_b=3.0,
        moe=True,
        thinking=True,
        tier="coding",
        repos=(
            "unsloth/GLM-4.7-Flash-GGUF",
            "zai-org/GLM-4.7-Flash-GGUF",
            "bartowski/zai-org_GLM-4.7-Flash-GGUF",
        ),
        max_context=202752,
    ),
    ModelSpec(
        id="gemma-4-26b-a4b",
        label="Gemma 4 26B A4B",
        family="gemma-4",
        params_b=27.0,
        active_params_b=4.0,
        moe=True,
        vision=True,
        repos=(
            "unsloth/gemma-4-26B-A4B-it-GGUF",
            "google/gemma-4-26B-A4B-it-GGUF",
            "bartowski/gemma-4-26B-A4B-it-GGUF",
        ),
        max_context=131072,
    ),
    ModelSpec(
        id="qwen3.6-27b",
        label="Qwen3.6 27B",
        family="qwen3.6",
        params_b=27.0,
        thinking=True,
        repos=(
            "unsloth/Qwen3.6-27B-GGUF",
            "Qwen/Qwen3.6-27B-GGUF",
            "bartowski/Qwen3.6-27B-GGUF",
        ),
        max_context=262144,
    ),
    ModelSpec(
        id="qwen3.5-27b",
        label="Qwen3.5 27B",
        family="qwen3.5",
        params_b=27.0,
        thinking=True,
        repos=(
            "unsloth/Qwen3.5-27B-GGUF",
            "Qwen/Qwen3.5-27B-GGUF",
            "bartowski/Qwen3.5-27B-GGUF",
        ),
        max_context=262144,
    ),
    ModelSpec(
        id="devstral-small-2-24b",
        label="Devstral Small 2 24B",
        family="devstral",
        params_b=24.0,
        tier="coding",
        repos=(
            "unsloth/Devstral-Small-2-24B-Instruct-GGUF",
            "mistralai/Devstral-Small-2-24B-Instruct-GGUF",
            "bartowski/mistralai_Devstral-Small-2-24B-Instruct-GGUF",
        ),
        max_context=131072,
    ),
    # ── 16–32 GB: the sweet spot for a Fedora laptop ────────────────────────
    ModelSpec(
        id="gpt-oss-20b",
        label="gpt-oss 20B",
        family="gpt-oss",
        params_b=21.0,
        active_params_b=3.6,
        moe=True,
        thinking=True,
        repos=(
            "unsloth/gpt-oss-20b-GGUF",
            "ggml-org/gpt-oss-20b-GGUF",
            "bartowski/openai_gpt-oss-20b-GGUF",
        ),
        max_context=131072,
    ),
    ModelSpec(
        id="gemma-4-12b",
        label="Gemma 4 12B",
        family="gemma-4",
        params_b=12.0,
        vision=True,
        repos=(
            "unsloth/gemma-4-12B-it-GGUF",
            "google/gemma-4-12B-it-GGUF",
            "bartowski/gemma-4-12B-it-GGUF",
        ),
        max_context=131072,
    ),
    ModelSpec(
        id="qwen3.5-9b",
        label="Qwen3.5 9B",
        family="qwen3.5",
        params_b=9.0,
        thinking=True,
        repos=(
            "unsloth/Qwen3.5-9B-GGUF",
            "Qwen/Qwen3.5-9B-GGUF",
            "bartowski/Qwen3.5-9B-GGUF",
        ),
        max_context=262144,
    ),
    # ── 8–16 GB ─────────────────────────────────────────────────────────────
    ModelSpec(
        id="gemma-4-e4b",
        label="Gemma 4 E4B",
        family="gemma-4",
        params_b=8.0,
        active_params_b=4.0,
        vision=True,
        repos=(
            "unsloth/gemma-4-E4B-it-GGUF",
            "google/gemma-4-E4B-it-GGUF",
            "bartowski/gemma-4-E4B-it-GGUF",
        ),
        max_context=131072,
    ),
    ModelSpec(
        id="qwen3.5-4b",
        label="Qwen3.5 4B",
        family="qwen3.5",
        params_b=4.0,
        thinking=True,
        repos=(
            "unsloth/Qwen3.5-4B-GGUF",
            "Qwen/Qwen3.5-4B-GGUF",
            "bartowski/Qwen3.5-4B-GGUF",
        ),
        max_context=262144,
    ),
    ModelSpec(
        id="smollm3-3b",
        label="SmolLM3 3B",
        family="smollm3",
        params_b=3.0,
        repos=(
            "HuggingFaceTB/SmolLM3-3B-GGUF",
            "bartowski/SmolLM3-3B-GGUF",
        ),
        max_context=8192,
    ),
    ModelSpec(
        id="gemma-4-e2b",
        label="Gemma 4 E2B",
        family="gemma-4",
        params_b=5.0,
        active_params_b=2.0,
        vision=True,
        repos=(
            "unsloth/gemma-4-E2B-it-GGUF",
            "google/gemma-4-E2B-it-GGUF",
            "bartowski/gemma-4-E2B-it-GGUF",
        ),
        max_context=131072,
    ),
    # ── Tiny fallbacks (4–8 GB RAM, always-downloadable floor) ──────────────
    ModelSpec(
        id="qwen3-1.7b",
        label="Qwen3 1.7B",
        family="qwen3",
        params_b=1.7,
        repos=(
            "unsloth/Qwen3-1.7B-GGUF",
            "Qwen/Qwen3-1.7B-GGUF",
            "bartowski/Qwen3-1.7B-GGUF",
        ),
        max_context=32768,
    ),
    ModelSpec(
        id="qwen3-0.6b",
        label="Qwen3 0.6B",
        family="qwen3",
        params_b=0.6,
        repos=(
            "unsloth/Qwen3-0.6B-GGUF",
            "Qwen/Qwen3-0.6B-GGUF",
            "bartowski/Qwen3-0.6B-GGUF",
        ),
        max_context=32768,
    ),
)

# How much a mixture-of-experts model is worth relative to a dense one of the
# same total size. An MoE knows more than a dense model with its *active*
# count but is not quite a dense model of its *total* count, so the score sits
# between the two: total params weighted with a slice of the active count.
MOE_QUALITY_WEIGHT = 0.8


# The first (strongest) member of a group becomes the default chat model, so it
# matters far more than whether a fourth or fifth endpoint exists. Weighting it
# is what stops a 16 GB laptop from being handed four small models instead of
# the best 9B it can hold plus two fast helpers.
PRIMARY_MODEL_WEIGHT = 3.0


def group_quality(plans: Sequence["Plan"]) -> float:
    """Score a resident group; ``plans[0]`` is the primary model."""
    total = 0.0
    for index, plan in enumerate(plans):
        weight = PRIMARY_MODEL_WEIGHT if index == 0 else 1.0
        total += weight * spec_quality(plan.spec) * 100.0 + plan.context / 1024.0
    return total


def spec_quality(spec: ModelSpec) -> float:
    """Comparable "how smart is this" score for a catalogue entry.

    Dense models score on parameters. MoE models score on a blend of total and
    active parameters, which keeps a 35B-A3B ahead of a 9B dense (it is) while
    stopping a 1T-A32B from outranking a 122B-A10B purely on paper weight.
    """
    if spec.moe or spec.active_params_b:
        return MOE_QUALITY_WEIGHT * spec.params_b + (1.0 - MOE_QUALITY_WEIGHT) * spec.kv_params_b
    return spec.params_b

# A fresh install intentionally starts a small local model group instead of a
# single endpoint. The planner may choose fewer than six when the hardware
# cannot hold them, but it never deliberately provisions fewer than three.
MIN_GROUP_MODELS = 3
MAX_GROUP_MODELS = 6
GROUP_SERVER_HEADROOM_GB = 0.15
GROUP_STATE_FILE = RUNTIME_DIR / "local_model_group.json"

# The catalogue is far larger than the group, and the group search is
# combinatorial over the specs that can fit at all. Shortlisting to the
# strongest N affordable candidates keeps that search bounded (C(9,5)=126
# instead of C(22,5)=26 334) without ever hiding the best model: anything
# outside the top N is weaker than everything inside it.
MAX_GROUP_CANDIDATES = 9
# Per-spec cap for the knapsack search — see _group_variants.
MAX_VARIANTS_PER_SPEC = 8
# Beam width for the group search. 192 states over 3–6 specs is enough to keep
# the cheap-but-strong combinations alive while staying well under a second.
GROUP_BEAM_WIDTH = 192

# ── Power profiles ───────────────────────────────────────────────────────────
# "How much of this machine may the models have?" The default keeps a
# comfortable margin so the desktop stays responsive; `power` and `max` trade
# that margin for a bigger model and a longer context window.
#   balanced : 3–6 resident models, ≤62 % of RAM (default, responsive desktop)
#   power    : 3–6 resident models, ≤78 % of RAM, ≥8k context, stronger picks
#   max      : ONE model, ≤90 % of RAM/VRAM, the longest context that fits
@dataclass(frozen=True)
class PowerProfile:
    name: str
    ram_fraction: float
    available_fraction: float
    group: bool
    min_context: int
    allow_low_quant: bool
    solo_fraction: float = 0.60


POWER_PROFILES: Dict[str, PowerProfile] = {
    "balanced": PowerProfile("balanced", 0.62, 0.88, True, 4096, True, 0.60),
    "power": PowerProfile("power", 0.78, 0.94, True, 8192, True, 0.75),
    "max": PowerProfile("max", 0.90, 0.97, False, 8192, False, 0.90),
}
DEFAULT_PROFILE = os.getenv("PSD_MODEL_PROFILE", "balanced").strip().lower()


def active_profile(name: str = "") -> PowerProfile:
    """Resolve a profile name (env var or CLI) to its settings."""
    key = (name or DEFAULT_PROFILE or "balanced").strip().lower()
    if key in ("1", "true", "yes", "on"):
        key = "power"
    return POWER_PROFILES.get(key) or POWER_PROFILES["balanced"]


# Quantisation quality, best first. Used to refuse "best model, awful quant"
# picks: a 70B at IQ1 is worse in practice than an 8B at Q4_K_M.
#
# MXFP4 / NVFP4 / FP8 are the native 4- and 8-bit float formats modern MoE
# releases ship in (gpt-oss is MXFP4 out of the box). They rank next to the
# K-quants of the same width: an MoE in MXFP4 is a good deal, not a compromise.
QUANT_RANK: Tuple[str, ...] = (
    "F16", "BF16", "Q8_0", "FP8", "Q6_K", "Q6_K_L", "Q5_K_M", "Q5_K_S", "Q5_K_L",
    "UD-Q5_K_XL", "AD-Q5_K_XL", "Q4_K_M", "Q4_K_S", "Q4_K_L", "Q4_K_XL",
    "UD-Q4_K_XL", "AD-Q4_K_XL", "MXFP4", "MXFP4_MOE", "NVFP4", "FP4",
    "IQ4_XS", "IQ4_NL", "AD-IQ4_XS", "AD-IQ4_NL",
    "Q4_0", "Q4_1", "Q3_K_M", "Q3_K_S", "Q3_K_L", "Q3_K_XL", "UD-Q3_K_XL", "AD-Q3_K_XL",
    "Q2_K", "Q2_K_L", "UD-Q2_K_XL", "AD-Q2_K_XL", "IQ3_M", "IQ3_XXS", "UD-IQ3_XXS", "AD-IQ3_XXS",
    "IQ2_M", "IQ2_XXS", "UD-IQ2_M", "UD-IQ2_XXS", "AD-IQ2_M", "IQ1_M", "IQ1_S", "UD-IQ1_M", "UD-IQ1_S",
)
_QUANT_RANK_INDEX = {q: i for i, q in enumerate(QUANT_RANK)}
# Quality window for what we will actually pick.
#   floor (Q3_K_M)  - below this the model gets noticeably dumb, so we would
#                     rather drop to a smaller model at a decent quant.
#   ceiling (Q8_0)  - Q8_0 is effectively lossless for chat. F16/BF16 double
#                     the download and halve the speed for no usable gain, so
#                     "best" stops here instead of meaning "biggest file".
MIN_QUANT_RANK = _QUANT_RANK_INDEX["Q3_K_M"]
BEST_QUANT = "Q8_0"
BEST_QUANT_RANK = _QUANT_RANK_INDEX[BEST_QUANT]

GB = 1024 ** 3


def log(msg: str = "") -> None:
    print(msg, flush=True)


# ── Hardware ─────────────────────────────────────────────────────────────────
def _load_hardware_module():
    """Import services.hwfit.hardware without dragging in the whole services
    package (it pulls fastapi/bs4/... through services/__init__.py)."""
    try:
        return importlib.import_module("services.hwfit.hardware")
    except Exception:
        path = BASE_DIR / "services" / "hwfit" / "hardware.py"
        spec = importlib.util.spec_from_file_location("psd_hwfit_hardware", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        return module


def detect_hardware() -> Dict[str, Any]:
    """Detect RAM/CPU/GPU using the app's own Cookbook hardware probe."""
    hw = _load_hardware_module()
    try:
        system = hw.detect_system(fresh=True)
    except Exception:
        system = {}
    if not isinstance(system, dict):
        system = {}
    return system


def memory_budget_gb(
    system: Dict[str, Any],
    profile: Optional[PowerProfile] = None,
    moe: bool = False,
) -> float:
    """How much memory (GB) we may spend on ONE model's weights + KV cache.

    GPU path: VRAM minus headroom for the runtime/KV cache. When the GPU is a
    unified-memory part (NVIDIA GB10 / DGX Spark, AMD Strix Halo) or the VRAM
    figure is missing we fall back to the system-RAM budget instead, which is
    the same pool the GPU uses.

    The power profile decides how much of the machine we are willing to take:
    ``balanced`` leaves the desktop room to breathe, ``max`` spends nearly all
    of it on one very large model.
    """
    prof = profile or active_profile()
    try:
        ram = float(system.get("available_ram_gb") or system.get("total_ram_gb") or 0.0)
    except (TypeError, ValueError):
        ram = 0.0
    unified = bool(system.get("unified_memory"))
    try:
        vram = float(system.get("gpu_vram_gb") or 0.0)
    except (TypeError, ValueError):
        vram = 0.0

    # Reserve less VRAM headroom the more aggressive the profile is: the
    # compute buffers need ~0.5 GB and the KV cache is planned separately.
    vram_headroom = {"balanced": 1.0, "power": 0.8, "max": 0.6}.get(prof.name, 1.0)
    ram_budget = max(0.6, ram * prof.solo_fraction)
    if moe:
        # A mixture-of-experts model does not have to fit in VRAM: its experts
        # can stay in RAM (--n-cpu-moe) while attention runs on the GPU, and
        # decoding only touches the active slice. That makes a 122B MoE a
        # realistic pick on a 24 GB card in a 128 GB workstation, which is the
        # single biggest difference between this planner and a VRAM-only one.
        if system.get("has_gpu") and vram >= 4:
            return max(ram_budget, vram - vram_headroom)
        return ram_budget
    if system.get("has_gpu") and not unified and vram >= 4:
        return max(1.0, vram - vram_headroom)
    if unified and vram >= 4:
        return max(1.0, min(vram, ram * 0.65) - vram_headroom)
    # CPU-only: leave the OS + the app itself room to breathe.
    return ram_budget


# ── File selection (pure) ────────────────────────────────────────────────────
@dataclass(frozen=True)
class GGUFFile:
    """One downloadable model choice.

    ``path`` is what gets handed to ``llama-server --model``. For a split
    release that is the FIRST shard; ``parts`` then carries every shard that
    has to sit next to it (``size`` is the sum of all of them). Single-file
    releases leave ``parts`` empty.
    """

    repo: str
    path: str
    size: int
    parts: Tuple[str, ...] = ()
    part_sizes: Tuple[int, ...] = ()

    @property
    def filename(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    @property
    def size_gb(self) -> float:
        return self.size / GB

    @property
    def all_paths(self) -> Tuple[str, ...]:
        return self.parts or (self.path,)

    @property
    def is_split(self) -> bool:
        return len(self.all_paths) > 1

    @property
    def key(self) -> str:
        """Stable identity across HF mirrors (same file listed by two repos)."""
        return f"{self.repo}:{self.path}"


# A split GGUF shard: "<name>-<QUANT>-00001-of-00004.gguf".
_SHARD_RE = re.compile(r"-(?P<idx>\d{5})-of-(?P<total>\d{5})\.gguf$", re.IGNORECASE)


def _is_split_part(filename: str) -> bool:
    """True for one shard of a multi-part GGUF ('...-00001-of-00003.gguf')."""
    return bool(_SHARD_RE.search(filename))


def _strip_shard_suffix(filename: str) -> str:
    """'<name>-Q8_0-00002-of-00008.gguf' -> '<name>-Q8_0.gguf'."""
    return _SHARD_RE.sub(".gguf", filename)


def quant_of(filename: str) -> Optional[str]:
    """Extract the quant tag from a GGUF filename ('...-Q4_K_M.gguf' -> 'Q4_K_M').

    Shard suffixes are ignored first, so a part of a split release reports the
    same quant as the whole model ('...-Q8_0-00001-of-00002.gguf' -> 'Q8_0').
    """
    stem = _strip_shard_suffix(filename)
    stem = stem[:-5] if stem.lower().endswith(".gguf") else stem
    m = re.search(
        r"[-.]((?:UD-|AD-)?(?:IQ|Q)[0-9][A-Z0-9_]*|MXFP4(?:_MOE)?|NVFP4|FP8|FP4|F16|BF16)$",
        stem,
        re.IGNORECASE,
    )
    if not m:
        return None
    return m.group(1).upper()


# Files that live next to the real weights inside a GGUF repo but are not the
# model itself:
#
#   mmproj-*   the vision projector, attached with --mmproj (find_mmproj owns it)
#   eagle3-*   an EAGLE3 speculative-decoding draft
#   mtp-*      a multi-token-prediction draft (Gemma 4 and friends ship these)
#   *draft*    generic draft releases
#
# A draft is a perfectly valid GGUF with an attractive quant tag, so without
# this filter it can OUTRANK the real weights (eagle3-gpt-oss-20b-Q8_0 beats
# gpt-oss-20b-MXFP4 on quant rank) and then die at context creation with
# "eagle3 requires ctx_other to be set" -- which is exactly how a first run
# ends up with three dead llama-servers and "2 of 3 models ready".
_AUX_GGUF_MARKERS = ("mmproj", "eagle", "mtp-", "-mtp", "draft", "specul", "dflash")


def _is_auxiliary_gguf(name: str) -> bool:
    """True for vision projectors and speculative-decoding drafts."""
    low = name.lower()
    return any(marker in low for marker in _AUX_GGUF_MARKERS)


def candidate_files(repo: str, files: Iterable[Dict[str, Any]]) -> List[GGUFFile]:
    """Known-quant GGUFs from a HF tree listing, split releases reassembled.

    Every *complete* shard set becomes one candidate whose size is the sum of
    its parts — that is what makes the 100B+ models in the catalogue usable at
    all, since their good quantisations are never published as a single file.
    An incomplete set (a mirror that only lists some parts) is skipped rather
    than half-downloaded.
    """
    out: List[GGUFFile] = []
    shards: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for entry in files:
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("path") or "")
        if not path.lower().endswith(".gguf"):
            continue
        name = path.rsplit("/", 1)[-1]
        if _is_auxiliary_gguf(name):
            continue  # projector or draft: never a main model
        quant = quant_of(name)
        if quant is None or quant not in _QUANT_RANK_INDEX:
            continue
        try:
            size = int(entry.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        if size <= 0:
            continue
        match = _SHARD_RE.search(name)
        if not match:
            out.append(GGUFFile(repo=repo, path=path, size=size))
            continue
        directory = path[: -len(name)]
        group_key = (directory, _strip_shard_suffix(name))
        bucket = shards.setdefault(
            group_key, {"total": int(match.group("total")), "parts": {}}
        )
        bucket["parts"][int(match.group("idx"))] = (path, size)

    for (_directory, _base), bucket in shards.items():
        total = int(bucket["total"])
        indexes = sorted(bucket["parts"])
        if total < 2 or indexes != list(range(1, total + 1)):
            continue  # incomplete mirror listing — never start a partial model
        paths = tuple(bucket["parts"][i][0] for i in indexes)
        sizes = tuple(bucket["parts"][i][1] for i in indexes)
        out.append(GGUFFile(
            repo=repo,
            path=paths[0],
            size=sum(sizes),
            parts=paths,
            part_sizes=sizes,
        ))
    return out


# ── Vision projectors ────────────────────────────────────────────────────────
def find_mmproj(repo: str, files: Iterable[Dict[str, Any]]) -> Optional[GGUFFile]:
    """The multimodal projector that belongs to a vision-capable GGUF repo.

    llama.cpp keeps vision weights in a separate ``mmproj-*.gguf``; attaching
    one with ``--mmproj`` is what lets a local model actually *see* an image —
    for psd.ai that means the ``computer_screen`` tool can hand a screenshot to
    a local model instead of describing it in text.
    """
    best: Optional[Tuple[Tuple[int, int], GGUFFile]] = None
    for entry in files:
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("path") or "")
        name = path.rsplit("/", 1)[-1]
        if not name.lower().endswith(".gguf") or "mmproj" not in name.lower():
            continue
        if _is_split_part(name):
            continue
        try:
            size = int(entry.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        if size <= 0:
            continue
        candidate = GGUFFile(repo=repo, path=path, size=size)
        # An F16/BF16 projector is the reference one; a quantised mmproj saves
        # a few hundred MB but degrades OCR-style detail, so prefer full precision.
        rank = 0 if quant_of(name) in ("F16", "BF16") else 1
        key = (rank, size)
        if best is None or key < best[0]:
            best = (key, candidate)
    return best[1] if best else None


# ── Disk space ───────────────────────────────────────────────────────────────
def free_disk_gb(path: Path) -> float:
    """Free GB on the filesystem that would hold ``path`` (0.0 if unknown)."""
    probe = Path(path)
    for candidate in (probe, *probe.parents):
        try:
            if candidate.exists():
                return shutil.disk_usage(str(candidate)).free / GB
        except OSError:
            continue
    return 0.0


def disk_shortfall_gb(plans: Sequence[Plan], models_dir: Path, reserve_gb: float = 4.0) -> float:
    """GB missing to hold every weight file plus a working reserve.

    Models are downloaded to the device runtime folder (by default
    ``~/.local/share/psd.ai/runtime``), which on a Fedora install is often a
    small root partition while the big storage lives elsewhere — so this is
    checked before a multi-GB download starts, not after it fills the disk.
    """
    needed = sum(p.file.size_gb for p in plans)
    already = 0.0
    for plan in plans:
        target = models_dir / plan.file.repo.replace("/", "__")
        for part in plan.file.all_paths:
            existing = target / Path(part).name
            try:
                if existing.exists():
                    already += existing.stat().st_size / GB
            except OSError:
                pass
    free = free_disk_gb(models_dir)
    if free <= 0:
        return 0.0  # unknown — do not block on a guess
    return max(0.0, (needed - already + reserve_gb) - free)


# KV cache grows with layer count and KV-head count, not with parameter count,
# so a flat per-8k figure badly misjudged both the small and the 70B models.
# MB of f16 KV per 1k tokens, banded by model size. Callers pass a model's
# ACTIVE parameter count, which is what attention cost tracks on an MoE.
KV_MB_PER_1K_TOKENS: Tuple[Tuple[float, float], ...] = (
    (3.0, 60.0), (8.0, 125.0), (14.0, 200.0), (34.0, 250.0), (10_000.0, 320.0),
)
# We request a q8_0 KV cache below, which is roughly half the f16 footprint.
KV_Q8_FACTOR = 0.55


def plan_context_gb(context: int, params_b: float = 8.0, quantized_kv: bool = True) -> float:
    """Estimated KV-cache footprint for a context window (weights are separate).

    ``params_b`` should be the model's *active* parameter count — see
    :attr:`ModelSpec.kv_params_b` — because the KV cache is written by the
    attention layers only. On an MoE that is a small fraction of the total.
    """
    mb_per_1k = KV_MB_PER_1K_TOKENS[-1][1]
    for limit, value in KV_MB_PER_1K_TOKENS:
        if params_b <= limit:
            mb_per_1k = value
            break
    gb = context / 1000.0 * mb_per_1k / 1024.0
    if quantized_kv:
        gb *= KV_Q8_FACTOR
    return max(0.1, gb)


@dataclass
class Plan:
    spec: ModelSpec
    file: GGUFFile
    quant: str
    context: int
    budget_gb: float
    fits: bool
    note: str = ""


@dataclass
class GroupPlan:
    """A set of local servers that can stay resident at the same time."""

    plans: Tuple[Plan, ...]
    budget_gb: float
    fits: bool
    note: str = ""
    vram_budget_gb: float = float("inf")

    @property
    def total_gb(self) -> float:
        """RAM the whole group keeps resident."""
        return sum(plan_ram_cost(p) for p in self.plans)

    @property
    def vram_total_gb(self) -> float:
        """VRAM the whole group keeps resident (MoE experts excluded)."""
        return sum(plan_vram_cost(p) for p in self.plans)


# Longest window first. The planner walks down until the KV cache stops
# eating the budget, and never asks for more than the model was trained on
# (``spec.max_context``) or than the power profile allows.
CONTEXT_LADDER: Tuple[int, ...] = (131072, 65536, 32768, 16384, 8192, 4096, 2048)
# A 2048-token window truncates most real conversations. If a model can only
# run that small, a smaller model at a proper context is the better answer.
MIN_USABLE_CONTEXT = 4096

# Per-profile ceiling on the context window. A 128k window is only worth its
# KV cache when the machine has room to spare; on `balanced` the same memory is
# better spent on a stronger model or another resident group member.
PROFILE_MAX_CONTEXT: Dict[str, int] = {"balanced": 32768, "power": 65536, "max": 262144}


def context_ladder(spec: ModelSpec, profile: Optional[PowerProfile] = None) -> Tuple[int, ...]:
    """Context windows worth trying for one model under one profile."""
    prof = profile or active_profile()
    ceiling = min(spec.max_context, PROFILE_MAX_CONTEXT.get(prof.name, 32768))
    ladder = tuple(c for c in CONTEXT_LADDER if c <= ceiling)
    return ladder or (CONTEXT_LADDER[-1],)


def _context_for(
    spec: ModelSpec,
    weights_gb: float,
    budget_gb: float,
    profile: Optional[PowerProfile] = None,
) -> int:
    """Largest context whose KV cache still leaves room for the weights."""
    ladder = context_ladder(spec, profile)
    for ctx in ladder:
        if weights_gb + plan_context_gb(ctx, spec.kv_params_b) <= budget_gb:
            return ctx
    return ladder[-1]


# Relaxation ladder for the quant floor. Q4_K_M is the sweet spot: above it the
# gains are small, below it models start losing instructions and tool-call
# reliability — and since context is ranked first, a Q3 floor would happily buy
# 32k of window with a noticeably dumber model. So the first pass refuses to go
# below Q4_K_M and only relaxes (Q3, Q2, IQ2, IQ1) when nothing else fits at
# all: a weak model that answers beats a strong one that does not load.
QUANT_FLOORS: Tuple[str, ...] = ("Q4_K_M", "Q3_K_M", "Q2_K", "IQ2_M", "IQ1_M")

# A quant floor only makes sense relative to the model. Q3 of a 122B MoE is a
# far better assistant than Q8 of a 9B — capacity dominates once the quant is
# past the point where instructions and tool schemas start breaking — while Q3
# of a 4B model is just a worse 4B. So big models are allowed to reach lower
# down the ladder, and small ones are not.
SIZE_QUANT_ALLOWANCE: Tuple[Tuple[float, str], ...] = (
    (60.0, "Q2_K"),     # 60B+ (and every 100B-class MoE)
    (20.0, "Q3_K_M"),   # 20B+
)


def quant_floor_rank_for(spec: "ModelSpec", floor_quant: str, relax_for_size: bool = True) -> int:
    """Quant-rank floor for one model: the requested floor, size-relaxed.

    Returns an index into :data:`QUANT_RANK` (higher = worse is allowed). An
    explicit ``--min-quant`` is never relaxed: that is the user telling us what
    they consider acceptable.
    """
    rank = _QUANT_RANK_INDEX.get(floor_quant.upper(), MIN_QUANT_RANK)
    if not relax_for_size:
        return rank
    for min_params, allowed in SIZE_QUANT_ALLOWANCE:
        if spec.params_b >= min_params:
            return max(rank, _QUANT_RANK_INDEX[allowed])
    return rank


def select_model_file(
    files: Sequence[GGUFFile],
    budget_gb: float,
    spec: ModelSpec,
    floor_quant: str,
    profile: Optional[PowerProfile] = None,
    relax_for_size: bool = True,
) -> Optional[Tuple[GGUFFile, str, int]]:
    """Largest GGUF of ``spec`` that fits ``budget_gb``.

    Split releases count as one candidate (their parts are summed), so a model
    published only as ``*-00001-of-00004.gguf`` competes on equal terms with a
    single-file one.

    Returns (file, quant, context) or None when nothing clears the quant floor
    *and* the memory budget together.
    """
    floor_rank = quant_floor_rank_for(spec, floor_quant, relax_for_size)
    usable = [
        f for f in files
        if BEST_QUANT_RANK
        <= _QUANT_RANK_INDEX.get(quant_of(f.filename) or "", len(QUANT_RANK))
        <= floor_rank
    ]
    if not usable:
        return None
    # The largest context window that still fits wins; quant quality is only
    # the tie-breaker. On a RAM-bound laptop a smaller model serving 32k beats
    # a marginally sharper one stuck at 4k, and the smaller file also decodes
    # faster because token generation is memory-bandwidth bound.
    best: Optional[Tuple[Tuple[int, int], GGUFFile, str, int]] = None
    for f in usable:
        ctx = _context_for(spec, f.size_gb, budget_gb, profile)
        if f.size_gb + plan_context_gb(ctx, spec.kv_params_b) > budget_gb:
            continue
        key = (ctx, -_QUANT_RANK_INDEX.get(quant_of(f.filename) or "", len(QUANT_RANK)))
        if best is None or key > best[0]:
            best = (key, f, quant_of(f.filename) or "", ctx)
    if best is None:
        return None
    return best[1], best[2], best[3]


def catalogue_by_strength() -> Tuple[ModelSpec, ...]:
    """The catalogue ordered strongest-first, MoE blends included.

    The declared order in ``MODEL_SPECS`` is a rough size ladder; this is the
    order the planner actually walks, so a 35B-A3B MoE correctly outranks a
    27B dense model of similar speed instead of losing on a raw size compare.
    """
    return tuple(sorted(MODEL_SPECS, key=spec_quality, reverse=True))


def plan_model(
    system: Dict[str, Any],
    files_by_repo: Dict[str, List[GGUFFile]],
    prefer_id: str = "",
    min_quant: str = "",
    profile: Optional[PowerProfile] = None,
) -> Optional[Plan]:
    """Pick the best local model (model + quant + context) for this hardware.

    Walks the catalogue strongest-first and, for each model, takes the largest
    GGUF that fits the memory budget at a quant no worse than the current
    floor. If no model fits at Q3_K_M the floor is relaxed (Q2, IQ2, IQ1) and
    the walk repeats, so a small machine still lands on the biggest model it
    can actually run instead of a giant one it cannot.
    """
    prof = profile or active_profile()
    specs = catalogue_by_strength()
    if prefer_id:
        wanted = [s for s in specs if s.id == prefer_id.lower()]
        if wanted:
            specs = tuple(wanted)

    def pool_for(spec: ModelSpec) -> List[GGUFFile]:
        out: List[GGUFFile] = []
        for repo in spec.repos:
            out.extend(files_by_repo.get(repo, []))
        return out

    def budget_for(spec: ModelSpec) -> float:
        return memory_budget_gb(system, prof, moe=bool(spec.moe or spec.active_params_b))

    floors = [min_quant.upper()] if min_quant else list(QUANT_FLOORS)
    relax_for_size = not min_quant
    if not prof.allow_low_quant:
        # `max` spends the machine on one model, so it refuses the sub-2-bit
        # quants: a 122B at IQ1 that crawls is worse than a 35B at Q4 that flies.
        floors = [f for f in floors if _QUANT_RANK_INDEX[f] <= _QUANT_RANK_INDEX["IQ2_M"]] or floors[:1]
    # Pass 1: only models that get a usable context window. Pass 2: allow the
    # 2048-token fallback rather than returning nothing at all.
    for min_context in (prof.min_context, CONTEXT_LADDER[-1]):
        for floor in floors:
            for spec in specs:
                budget = budget_for(spec)
                picked = select_model_file(
                    pool_for(spec), budget, spec, floor, prof, relax_for_size=relax_for_size
                )
                if picked is None:
                    continue
                f, quant, ctx = picked
                if ctx < min_context:
                    continue
                return Plan(
                    spec=spec, file=f, quant=quant, context=ctx,
                    budget_gb=round(budget, 2), fits=True,
                )

    # Nothing fits at any quant: take the smallest file of the smallest model
    # we know about, so the user still gets a local model (slow, but working).
    for spec in reversed(specs):
        pool = [
            f for f in pool_for(spec)
            if _QUANT_RANK_INDEX.get(quant_of(f.filename) or "", len(QUANT_RANK)) >= BEST_QUANT_RANK
        ] or pool_for(spec)
        if not pool:
            continue
        smallest = min(pool, key=lambda f: f.size)
        return Plan(
            spec=spec, file=smallest, quant=quant_of(smallest.filename) or "",
            context=CONTEXT_LADDER[-1], budget_gb=round(budget_for(spec), 2), fits=False,
            note="nothing fit the memory budget - using the smallest model available, expect slow replies",
        )
    return None


def group_memory_budget_gb(
    system: Dict[str, Any], profile: Optional[PowerProfile] = None
) -> float:
    """Total memory reserved for a resident model group.

    ``memory_budget_gb`` is intentionally conservative for one large model.
    A group has several models, so use the available-memory reading and leave
    the OS/application a safety margin decided by the power profile. GPU memory
    remains the hard limit for discrete cards; unified-memory parts (NVIDIA
    GB10, AMD Strix Halo) use the shared pool.
    """
    prof = profile or active_profile()
    unified = bool(system.get("unified_memory"))
    if unified and has_usable_gpu(system):
        # One pool shared by CPU and GPU: plan against RAM, cap it at the
        # GPU-visible figure so the estimate stays honest.
        try:
            vram = float(system.get("gpu_vram_gb") or 0.0)
        except (TypeError, ValueError):
            vram = 0.0
        try:
            ram = float(system.get("available_ram_gb") or system.get("total_ram_gb") or 0.0)
        except (TypeError, ValueError):
            ram = 0.0
        return max(1.5, min(vram, ram * 0.65) - 1.0)

    try:
        total = float(system.get("total_ram_gb") or 0.0)
    except (TypeError, ValueError):
        total = 0.0
    try:
        available = float(system.get("available_ram_gb") or 0.0)
    except (TypeError, ValueError):
        available = 0.0
    if total <= 0:
        return max(1.5, memory_budget_gb(system, prof))
    # Never consume more than the profile's share of physical RAM or of the
    # currently free RAM. The lower value wins, which behaves well both after
    # boot and on a machine where other applications are already open.
    physical_cap = total * prof.ram_fraction
    available_cap = available * prof.available_fraction if available > 0 else physical_cap
    return max(1.5, min(physical_cap, available_cap))


def group_vram_budget_gb(
    system: Dict[str, Any], profile: Optional[PowerProfile] = None
) -> float:
    """VRAM the group may occupy, or +inf when nothing offloads.

    Two pools are planned separately on purpose. A dense model has to be
    resident in VRAM to be worth running, so dense members are charged here;
    an MoE only needs its attention + shared weights + KV cache in VRAM and
    can leave the experts in the RAM pool (``--n-cpu-moe``). Charging both to
    one number is what used to cap a 64 GB / 24 GB workstation at 22 GB of
    models.
    """
    prof = profile or active_profile()
    if not has_usable_gpu(system) or bool(system.get("unified_memory")):
        return float("inf")
    try:
        vram = float(system.get("gpu_vram_gb") or 0.0)
    except (TypeError, ValueError):
        vram = 0.0
    if vram < MIN_USABLE_VRAM_GB:
        return float("inf")
    # Every server needs its own compute buffers, so the reserve grows a little
    # with the group; `power`/`max` tighten it.
    reserve = {"balanced": 1.25, "power": 1.0, "max": 0.75}.get(prof.name, 1.25)
    return max(1.0, vram - reserve)


def plan_ram_cost(plan: Plan) -> float:
    """RAM a resident model costs: weights + KV cache + server overhead."""
    return (
        plan.file.size_gb
        + plan_context_gb(plan.context, plan.spec.kv_params_b)
        + GROUP_SERVER_HEADROOM_GB
    )


def plan_vram_cost(plan: Plan) -> float:
    """VRAM a resident model needs, given how its layers will be placed."""
    kv = plan_context_gb(plan.context, plan.spec.kv_params_b)
    if plan.spec.moe:
        # Experts may live in RAM; everything else must be on the GPU.
        return plan.file.size_gb * MOE_NON_EXPERT_FRACTION + kv + 0.6
    return plan.file.size_gb + kv + GROUP_SERVER_HEADROOM_GB


def _group_variants(
    spec: ModelSpec,
    files: Sequence[GGUFFile],
    budget_gb: float,
    floor_quant: str,
    profile: Optional[PowerProfile] = None,
    vram_budget_gb: float = float("inf"),
    relax_for_size: bool = True,
) -> List[Plan]:
    """Affordable file/context choices for one member of a group, best first.

    Capped at :data:`MAX_VARIANTS_PER_SPEC` entries so the group search stays
    fast even when a mirror publishes thirty quantisations: the dropped ones
    are strictly weaker than the kept ones (same or smaller context at a worse
    quant), so the cap never changes the answer on real hardware.
    """
    prof = profile or active_profile()
    floor_rank = quant_floor_rank_for(spec, floor_quant, relax_for_size)
    variants: List[Plan] = []
    for f in files:
        quant = quant_of(f.filename) or ""
        rank = _QUANT_RANK_INDEX.get(quant, len(QUANT_RANK))
        if not (BEST_QUANT_RANK <= rank <= floor_rank):
            continue
        for context in context_ladder(spec, prof):
            probe = Plan(
                spec=spec, file=f, quant=quant, context=context,
                budget_gb=budget_gb, fits=True,
            )
            if plan_ram_cost(probe) > budget_gb:
                continue
            if plan_vram_cost(probe) > vram_budget_gb:
                continue
            variants.append(Plan(
                spec=spec,
                file=f,
                quant=quant,
                context=context,
                budget_gb=budget_gb,
                fits=True,
            ))

    # A file can occur in multiple HF mirrors: keep the cheapest copy of each
    # (path, context) pair, then rank strongest-first for the bounded search.
    unique: Dict[Tuple[str, int], Plan] = {}
    for plan in variants:
        key = (plan.file.path, plan.context)
        previous = unique.get(key)
        if previous is None or plan.file.size < previous.file.size:
            unique[key] = plan

    return sorted(unique.values(), key=_variant_score, reverse=True)[:MAX_VARIANTS_PER_SPEC]


def _variant_score(plan: Plan) -> Tuple[float, float, int]:
    """Rank one (model, quant, context) choice: strength, then window, then quant."""
    rank = _QUANT_RANK_INDEX.get(plan.quant, len(QUANT_RANK))
    return (
        spec_quality(plan.spec) * 100.0,
        plan.context / 1024.0,
        -rank,
    )


def _best_group_variants(
    specs: Sequence[ModelSpec],
    variants_by_spec: Dict[str, List[Plan]],
    budget_gb: float,
    vram_budget_gb: float = float("inf"),
) -> Optional[Tuple[Plan, ...]]:
    """Bounded knapsack search for the strongest fitting choice per spec.

    Two pools are enforced at once: total RAM (weights + KV cache for every
    resident server) and VRAM (what has to be on the card). An MoE is cheap in
    the VRAM pool because its experts can stay in RAM, which is exactly the
    trade that lets a mid-range GPU host a much stronger group than a
    single-pool planner would allow.
    """
    # State: (RAM used, VRAM used, quality, selected plans). A few hundred
    # states is plenty for a shortlisted catalogue and avoids a combinatorial
    # explosion when a repo exposes dozens of quantisations.
    states: List[Tuple[float, float, float, Tuple[Plan, ...]]] = [(0.0, 0.0, 0.0, ())]
    for spec in specs:
        variants = variants_by_spec.get(spec.id) or []
        if not variants:
            return None
        expanded: List[Tuple[float, float, float, Tuple[Plan, ...]]] = []
        for ram_used, vram_used, quality, selected in states:
            for plan in variants:
                new_ram = ram_used + plan_ram_cost(plan)
                if new_ram > budget_gb + 1e-6:
                    continue
                new_vram = vram_used + plan_vram_cost(plan)
                if new_vram > vram_budget_gb + 1e-6:
                    continue
                rank = _QUANT_RANK_INDEX.get(plan.quant, len(QUANT_RANK))
                # specs are walked strongest-first, so index 0 is the primary.
                weight = PRIMARY_MODEL_WEIGHT if len(selected) == 0 else 1.0
                new_quality = (
                    quality
                    + weight * spec_quality(plan.spec) * 100.0
                    + plan.context / 1024.0
                    - rank * 0.01
                )
                expanded.append((new_ram, new_vram, new_quality, selected + (plan,)))
        if not expanded:
            return None
        # Keep the best state for each decile of memory usage, then cap the
        # beam. This preserves cheap combinations needed to fit later models.
        buckets: Dict[int, Tuple[float, float, float, Tuple[Plan, ...]]] = {}
        for state in expanded:
            bucket = int(state[0] * 10)
            previous = buckets.get(bucket)
            if previous is None or state[2] > previous[2]:
                buckets[bucket] = state
        states = sorted(
            buckets.values(), key=lambda item: (item[2], -item[0]), reverse=True
        )[:GROUP_BEAM_WIDTH]
    return max(states, key=lambda item: item[2])[3] if states else None


def _variant_cost(plan: Plan) -> float:
    """Resident RAM cost of one choice (see :func:`plan_ram_cost`)."""
    return plan_ram_cost(plan)


def _group_shortlist(
    specs: Sequence[ModelSpec],
    variants_by_spec: Dict[str, List[Plan]],
    *,
    limit: int = MAX_GROUP_CANDIDATES,
    prefer_id: str = "",
) -> List[ModelSpec]:
    """Trim the affordable catalogue to a searchable set without losing quality.

    The knapsack search is combinatorial in the number of specs, so with a
    twenty-model catalogue it has to be bounded. Keeping the strongest
    ``limit`` candidates cannot hide a better answer — everything dropped is
    weaker than everything kept — but a group also needs *cheap* members to
    reach three to six models inside a tight budget, so the smallest
    affordable specs are kept alongside the strongest ones.
    """
    affordable = [spec for spec in specs if variants_by_spec.get(spec.id)]
    if len(affordable) <= limit:
        return affordable

    ranked = sorted(
        affordable, key=lambda s: (spec_quality(s), s.params_b), reverse=True
    )
    shortlist = ranked[:limit]

    def cheapest(spec: ModelSpec) -> float:
        return min(_variant_cost(p) for p in variants_by_spec.get(spec.id) or [])

    for spec in sorted(affordable, key=cheapest)[:3]:
        if spec not in shortlist:
            shortlist.append(spec)
    if prefer_id:
        for spec in affordable:
            if spec.id == prefer_id.lower() and spec not in shortlist:
                shortlist.append(spec)
    return shortlist


def plan_model_group(
    system: Dict[str, Any],
    files_by_repo: Dict[str, List[GGUFFile]],
    prefer_id: str = "",
    min_quant: str = "",
    min_models: int = MIN_GROUP_MODELS,
    max_models: int = MAX_GROUP_MODELS,
    profile: Optional[PowerProfile] = None,
    prefer_tier: str = "",
) -> Optional[GroupPlan]:
    """Choose three to six models that fit in memory together.

    Every feasible group size is scored and the best one wins, rather than
    taking the largest group that happens to fit: five small models make a
    worse assistant than three where the primary is the strongest thing the
    machine can hold. Each returned plan has its own port/server, and
    ``plans[0]`` is the primary model (the default the app talks to).

    ``prefer_tier`` ("coding" / "reasoning") requires the *primary* member to
    come from that specialism; if nothing from it is affordable the constraint
    is dropped so the group still ships at least three models.
    """
    prof = profile or active_profile()
    min_models = max(MIN_GROUP_MODELS, min(MAX_GROUP_MODELS, int(min_models)))
    max_models = max(min_models, min(MAX_GROUP_MODELS, int(max_models)))
    budget = group_memory_budget_gb(system, prof)
    vram_budget = group_vram_budget_gb(system, prof)
    specs = catalogue_by_strength()
    if prefer_tier:
        wanted_tier = prefer_tier.strip().lower()
        tiered = [s for s in specs if s.tier == wanted_tier]
        if tiered:
            specs = tuple(tiered + [s for s in specs if s not in tiered])

    def pool_for(spec: ModelSpec) -> List[GGUFFile]:
        files: List[GGUFFile] = []
        for repo in spec.repos:
            files.extend(files_by_repo.get(repo, []))
        return files

    floors = [min_quant.upper()] if min_quant else list(QUANT_FLOORS)
    relax_for_size = not min_quant
    required_tier = prefer_tier.strip().lower() if prefer_tier else ""
    for floor in floors:
        variants_by_spec = {
            spec.id: _group_variants(
                spec, pool_for(spec), budget, floor, prof,
                vram_budget_gb=vram_budget, relax_for_size=relax_for_size,
            )
            for spec in specs
        }
        available = [spec for spec in specs if variants_by_spec.get(spec.id)]
        if len(available) < min_models:
            continue
        candidates = _group_shortlist(specs, variants_by_spec, prefer_id=prefer_id)
        if len(candidates) < min_models:
            continue

        # Try the tier constraint first, then fall back to an unconstrained
        # group rather than shipping nothing.
        for tier_required in ((True, False) if required_tier else (False,)):
            best: Optional[Tuple[float, Tuple[Plan, ...]]] = None
            for count in range(min(max_models, len(candidates)), min_models - 1, -1):
                for selected_specs in combinations(candidates, count):
                    if prefer_id and selected_specs[0].id != prefer_id.lower():
                        continue
                    # candidates are strength-sorted, so [0] is the primary.
                    if tier_required and selected_specs[0].tier != required_tier:
                        continue
                    selected = _best_group_variants(
                        selected_specs, variants_by_spec, budget, vram_budget
                    )
                    if selected is None:
                        continue
                    quality = group_quality(selected)
                    if best is None or quality > best[0]:
                        best = (quality, selected)
            if best is not None:
                return GroupPlan(
                    plans=best[1],
                    budget_gb=round(budget, 2),
                    fits=True,
                    note=(
                        f"{len(best[1])} resident models selected for this machine "
                        f"({prof.name} profile)"
                        + (f", primary is {required_tier}-tuned" if tier_required else "")
                    ),
                    vram_budget_gb=vram_budget,
                )

    # Last-resort path: still return three downloadable models, using the
    # smallest 2048-context variants if that is the only possible group. This
    # keeps the user-facing contract at three; the warning makes an unusual
    # low-memory setup explicit rather than failing after one download.
    floor = floors[-1]
    variants_by_spec = {
        spec.id: _group_variants(
            spec, pool_for(spec), float("inf"), floor, prof, relax_for_size=relax_for_size
        )
        for spec in specs
    }
    available = [spec for spec in specs if variants_by_spec.get(spec.id)]
    if len(available) < min_models:
        return None
    cheapest: List[Plan] = []
    for spec in sorted(available, key=lambda item: item.params_b):
        choices = variants_by_spec[spec.id]
        cheapest.append(min(choices, key=_variant_cost))
        if len(cheapest) >= min_models:
            break
    return GroupPlan(
        plans=tuple(cheapest[:max_models]),
        budget_gb=round(budget, 2),
        fits=False,
        note="three smallest models selected; total memory is above the conservative budget",
    )


# ── GPU / launch flags (pure) ────────────────────────────────────────────────
# Integrated graphics report themselves as a GPU with a token amount of
# dedicated VRAM (Intel UHD: 128 MB). Nothing useful offloads to that, and the
# Vulkan/ROCm builds are a bigger download that can only fail to initialise, so
# below this line we treat the machine as CPU-only.
MIN_USABLE_VRAM_GB = 1.5


def gpu_vram_gb(system: Dict[str, Any]) -> float:
    try:
        return float(system.get("gpu_vram_gb") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def has_usable_gpu(system: Dict[str, Any]) -> bool:
    """True only when there is a GPU with enough VRAM to hold real layers."""
    return bool(system.get("has_gpu")) and gpu_vram_gb(system) >= MIN_USABLE_VRAM_GB


def plan_gpu_layers(system: Dict[str, Any], plan: Plan) -> int:
    """How many layers to offload to the GPU. -1 = all of them.

    Mixture-of-experts models get a different answer from dense ones, and this
    is where most of the speed on a mid-range GPU comes from: an MoE's expert
    weights are huge but touched sparsely, so the winning layout is *every*
    layer on the GPU with the experts spilled to RAM (``--n-cpu-moe``, see
    :func:`plan_cpu_moe`) rather than a partial layer offload that leaves
    attention on the CPU.
    """
    vram = gpu_vram_gb(system)
    if not has_usable_gpu(system):
        return 0
    kv = plan_context_gb(plan.context, plan.spec.kv_params_b)
    need = plan.file.size_gb + kv
    if need <= vram - 0.6:
        return -1
    if plan.spec.moe:
        # Attention + shared weights + KV must fit; the experts may live in RAM.
        resident = plan.file.size_gb * MOE_NON_EXPERT_FRACTION + kv
        if resident <= vram - 0.6:
            return -1
    # Partial offload: keep ~0.5 GB of VRAM for the KV cache/compute buffers.
    room = max(0.0, vram - 0.5)
    if room <= 0.2:
        return 0
    frac = min(0.95, room / max(0.1, plan.file.size_gb))
    return max(1, int(frac * _estimate_layers(plan.spec)))


# Share of an MoE's weight bytes that is NOT expert FFN (attention, shared
# experts, embeddings, norms). Spilling the rest to RAM is what makes a 35B
# MoE usable on a 12 GB card.
MOE_NON_EXPERT_FRACTION = 0.18

# Transformer block counts for the catalogue models where the number matters
# enough to state (n-cpu-moe / partial -ngl maths). Anything unlisted falls
# back to a size band, which is accurate to within a few layers.
KNOWN_LAYER_COUNTS: Dict[str, int] = {
    "qwen3.5-122b-a10b": 62,
    "kimi-k3": 61,
    "gpt-oss-120b": 36,
    "gpt-oss-20b": 24,
    "qwen3-coder-next": 48,
    "qwen3-next-80b-a3b": 48,
    "qwen3.6-35b-a3b": 48,
    "glm-4.7-flash": 48,
    "glm-5.3-flash": 61,
    "gemma-4-26b-a4b": 42,
    "gemma-4-31b": 48,
    "gemma-4-12b": 36,
    "qwen3.6-27b": 48,
    "qwen3.5-27b": 48,
    "devstral-small-2-24b": 40,
    "qwen3.5-9b": 36,
    "qwen3.5-4b": 32,
    "gemma-4-e4b": 30,
    "gemma-4-e2b": 26,
    "smollm3-3b": 32,
    "qwen3-1.7b": 28,
    "qwen3-0.6b": 28,
}


def _estimate_layers(spec: ModelSpec) -> int:
    """Transformer block count for a model, known value or size-band estimate."""
    known = KNOWN_LAYER_COUNTS.get(spec.id)
    if known:
        return known
    total = spec.params_b
    if total <= 2:
        return 28
    if total <= 5:
        return 32
    if total <= 12:
        return 36
    if total <= 35:
        return 48
    if total <= 100:
        return 62
    return 64


def plan_cpu_moe(system: Dict[str, Any], plan: Plan, gpu_layers: int) -> int:
    """How many MoE layers to keep in RAM while the rest runs on the GPU.

    Returns 0 for dense models and for MoE models that fit entirely in VRAM.
    Otherwise it is the smallest number of expert layers that has to move to
    RAM for weights + KV cache + compute buffers to fit, which is exactly what
    ``llama-server --n-cpu-moe`` expects.
    """
    # gpu_layers == -1 means "all of them", so only a genuine 0 rules out a
    # GPU-resident MoE.
    if not plan.spec.moe or gpu_layers == 0:
        return 0
    vram = gpu_vram_gb(system)
    kv = plan_context_gb(plan.context, plan.spec.kv_params_b)
    total_need = plan.file.size_gb + kv + 0.6  # compute buffers
    if total_need <= vram:
        return 0
    layers = max(8, _estimate_layers(plan.spec))
    expert_gb = plan.file.size_gb * (1.0 - MOE_NON_EXPERT_FRACTION)
    per_layer = expert_gb / layers
    if per_layer <= 0.05:
        return 0
    spill_gb = total_need - vram
    needed = int(math.ceil(spill_gb / per_layer))
    return max(1, min(layers - 1, needed))


def _cores(system: Dict[str, Any]) -> Tuple[int, int]:
    """(logical, physical) processor counts, either of which may be 0."""
    def _int(key):
        try:
            return int(system.get(key) or 0)
        except (TypeError, ValueError):
            return 0
    return _int("cpu_cores"), _int("cpu_physical_cores")


def plan_threads(system: Dict[str, Any]) -> int:
    """Generation thread count.

    Token generation is memory-bandwidth bound and does NOT scale past the
    physical performance cores. On hybrid Intel (12th gen and later) it is
    actively harmful to include the E-cores: every worker waits on the slowest
    one, and reported losses are 20-30% or worse.

    P-cores are SMT (2 threads each) and E-cores are not (1 thread each), so
    with P + E = physical and 2P + E = logical, the P-core count falls straight
    out as `logical - physical`. On a non-hybrid SMT CPU that same expression
    equals the physical core count, which is also the right answer, so one
    formula covers both.
    """
    logical, physical = _cores(system)
    if logical > physical > 0:
        p_cores = logical - physical
        if 1 <= p_cores <= physical:
            return p_cores
    if physical > 0:
        return physical
    return max(1, logical)


def cpu_arch(system: Dict[str, Any]) -> str:
    """llama.cpp release arch tag for this machine ('x64' or 'arm64')."""
    arch = str(system.get("cpu_arch") or platform.machine() or "").lower()
    if arch in ("arm64", "aarch64"):
        return "arm64"
    return "x64"


# Intel GPUs are worth a SYCL build even though they are not "usable VRAM" in
# the offload sense: an Arc card or a Core Ultra iGPU runs llama.cpp through
# oneAPI, which is the only accelerated path Fedora offers for that hardware.
_INTEL_GPU_MARKERS = ("arc", "iris xe", "battlemage", "intel(r) core(tm) ultra", "data center gpu")


def _is_intel_gpu(system: Dict[str, Any]) -> bool:
    name = str(system.get("gpu_name") or "").lower()
    backend = str(system.get("backend") or "").lower()
    if backend in ("sycl", "intel", "oneapi"):
        return True
    return bool(name) and any(marker in name for marker in _INTEL_GPU_MARKERS)


def linux_backend_candidates(system: Dict[str, Any]) -> List[str]:
    """llama.cpp Linux asset kinds to try, best first.

    The CPU build is always last: it is tiny and works everywhere, so it is the
    guaranteed fallback when a GPU build refuses to start (missing driver, no
    ``/dev/dri`` permission, ROCm version mismatch — all common on a fresh
    Fedora install).
    """
    backend = str(system.get("backend") or "").lower()
    gpu_name = str(system.get("gpu_name") or "").lower()
    arch = cpu_arch(system)
    out: List[str] = []
    if has_usable_gpu(system):
        if backend == "cuda" or "nvidia" in gpu_name or "geforce" in gpu_name or "rtx" in gpu_name:
            out.append("cuda")
        if backend == "rocm" or "radeon" in gpu_name or "instinct" in gpu_name:
            out.append("rocm")
        if _is_intel_gpu(system) and arch == "x64":
            out.append("sycl")
        if arch == "x64":
            out.append("vulkan")
        elif backend != "cuda":
            out.append("vulkan")
    elif _is_intel_gpu(system) and arch == "x64":
        # iGPU with no dedicated VRAM: SYCL is still a real speed-up over CPU.
        out.append("sycl")
    out.append("cpu")
    seen: List[str] = []
    for item in out:
        if item not in seen:
            seen.append(item)
    return seen


# Release asset naming for Linux. llama.cpp publishes the binaries as Ubuntu
# builds (``llama-bNNNN-bin-ubuntu-*``); they are plain glibc builds and run on
# any distribution with glibc >= 2.35 — Fedora 40+ ships 2.39+, so these are
# the right artefacts here.
_ASSET_PATTERNS: Dict[str, str] = {
    "cpu": r"^llama-{tag}-bin-ubuntu-{arch}\.tar\.gz$",
    "vulkan": r"^llama-{tag}-bin-ubuntu-vulkan-{arch}\.tar\.gz$",
    "cuda": r"^llama-{tag}-bin-ubuntu-cuda-(?P<ver>[\d.]+)-{arch}\.tar\.gz$",
    "rocm": r"^llama-{tag}-bin-ubuntu-rocm-(?P<ver>[\d.]+)-{arch}\.tar\.gz$",
    "sycl": r"^llama-{tag}-bin-ubuntu-sycl-(?P<ver>fp16|fp32)-{arch}\.tar\.gz$",
    "openvino": r"^llama-{tag}-bin-ubuntu-openvino-(?P<ver>[\d.]+)-{arch}\.tar\.gz$",
}
# CUDA runtime bundle published next to the CUDA build; without it llama-server
# fails at dlopen on a machine that has the driver but not the CUDA toolkit.
_CUDART_PATTERN = r"^cudart-llama-{tag}-bin-ubuntu-cuda-(?P<ver>[\d.]+)-{arch}\.tar\.gz$"


def _asset_version_key(name: str) -> Tuple[int, ...]:
    """Numeric version inside an asset name, for sorting CUDA/ROCm builds."""
    m = re.search(r"-(\d+(?:\.\d+)+)-", name)
    if not m:
        return (0,)
    return tuple(int(part) for part in m.group(1).split("."))


def match_release_asset(
    assets: Iterable[Dict[str, Any]], tag: str, kind: str, arch: str
) -> Optional[Dict[str, Any]]:
    """Find the llama.cpp Linux release asset for (tag, backend kind, arch).

    When a backend ships several variants (CUDA 12.8 / 13.3, SYCL fp16 / fp32)
    the *most compatible* one wins rather than the newest: a CUDA 12.x build
    runs on any driver >= 525 through minor-version compatibility, while 13.x
    needs a current driver that a stable Fedora install may not have.
    """
    pattern = _ASSET_PATTERNS.get(kind)
    if not pattern:
        return None
    rx = re.compile(pattern.format(tag=re.escape(tag), arch=re.escape(arch)))
    matches: List[Dict[str, Any]] = []
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "")
        if not rx.match(name) or not asset.get("browser_download_url"):
            continue
        matches.append(asset)
    if not matches:
        return None
    if kind == "cuda":
        # Lowest CUDA major first (12.x before 13.x) — widest driver support.
        matches.sort(key=lambda a: (_asset_version_key(str(a.get("name")))[0], str(a.get("name"))))
    elif kind == "sycl":
        # fp16 is the faster build and works on every supported Intel GPU.
        matches.sort(key=lambda a: (0 if "fp16" in str(a.get("name")) else 1, str(a.get("name"))))
    else:
        matches.sort(key=lambda a: str(a.get("name")))
    return matches[0]


def match_cudart_asset(
    assets: Iterable[Dict[str, Any]], tag: str, arch: str, cuda_version: str
) -> Optional[Dict[str, Any]]:
    """The CUDA runtime bundle that belongs to a specific CUDA build."""
    rx = re.compile(_CUDART_PATTERN.format(tag=re.escape(tag), arch=re.escape(arch)))
    wanted = f"cuda-{cuda_version}-{arch}"
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "")
        if rx.match(name) and wanted in name and asset.get("browser_download_url"):
            return asset
    return None


def cuda_version_of(asset: Dict[str, Any]) -> str:
    """'12.8' out of 'llama-b11064-bin-ubuntu-cuda-12.8-x64.tar.gz'."""
    m = re.search(r"-cuda-(\d+(?:\.\d+)+)-", str(asset.get("name") or ""))
    return m.group(1) if m else ""


def pick_llama_release(releases: Iterable[Dict[str, Any]], arch: str = "x64") -> Optional[Dict[str, Any]]:
    """Newest llama.cpp release that actually carries Linux binaries.

    llama.cpp publishes rolling ``bNNNN`` prereleases with the binaries; the
    semver tags (v0.4.x) only carry ``nightly-tag.txt``, so "latest release"
    is not usable here.
    """
    best_tag: Optional[str] = None
    best_release: Optional[Dict[str, Any]] = None
    for release in releases or []:
        if not isinstance(release, dict):
            continue
        assets = release.get("assets") or []
        if not any(
            isinstance(a, dict) and re.match(r"^llama-b\d+-bin-ubuntu-", str(a.get("name") or ""))
            for a in assets
        ):
            continue
        tag = str(release.get("tag_name") or "")
        m = re.match(r"^b(\d+)$", tag)
        if not m:
            continue
        if best_tag is None or int(m.group(1)) > int(re.match(r"^b(\d+)$", best_tag).group(1)):  # type: ignore[arg-type]
            best_tag, best_release = tag, release
    if best_release is None:
        return None
    return {"tag": best_tag, "assets": best_release.get("assets") or []}


# ggml backend shared objects, as the Linux builds name them.
_BACKEND_LIBS: Tuple[Tuple[str, str], ...] = (
    ("vulkan", "ggml-vulkan"),
    ("cuda", "ggml-cuda"),
    ("cuda", "ggml-hip"),
    ("cuda", "ggml-hipblas"),
    ("sycl", "ggml-sycl"),
    ("openvino", "ggml-openvino"),
    ("cpu", "ggml-cpu"),
)


def detect_llama_backends(exe: Path) -> List[str]:
    """Which ggml backends a llama-server binary was built with.

    Linux builds load their backend as a sibling shared object
    (``libggml-vulkan.so``, ``libggml-cuda.so``), so listing the directory the
    binary lives in is enough — no need to dlopen anything. The binary's own
    bytes are scanned as a fallback for statically linked builds.
    """
    found: List[str] = []

    def _add(name: str) -> None:
        if name not in found:
            found.append(name)

    for directory in (exe.parent, exe.parent / "lib", exe.parent.parent / "lib"):
        try:
            entries = [p.name.lower() for p in directory.iterdir()]
        except OSError:
            continue
        for backend, marker in _BACKEND_LIBS:
            if any(marker in entry for entry in entries if entry.endswith(".so")):
                _add(backend)
    try:
        blob = exe.read_bytes()[: 32 * 1024 * 1024]
    except OSError:
        blob = b""
    lower = blob.lower()
    for backend, marker in _BACKEND_LIBS:
        if marker.encode() in lower:
            _add(backend)
    _add("cpu")
    return found


# --help output is identical for every call, so probe each flag at most once.
_HELP_CACHE: Dict[str, str] = {}


def server_help_text(exe: Path) -> str:
    """Cached ``llama-server --help`` output (empty string when unavailable)."""
    key = str(exe)
    if key in _HELP_CACHE:
        return _HELP_CACHE[key]
    text = ""
    try:
        proc = subprocess.run(
            [str(exe), "--help"], capture_output=True, text=True, timeout=20
        )
        text = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    except Exception:
        text = ""
    _HELP_CACHE[key] = text
    return text


# Backends that can actually hold model layers (Metal is gone with macOS
# support; SYCL and OpenVINO are the Intel paths on a Fedora box).
GPU_BACKENDS: Tuple[str, ...] = ("cuda", "vulkan", "sycl", "openvino", "hip")


def has_gpu_backend(backends: Sequence[str]) -> bool:
    """True when this llama-server build can offload layers to a GPU."""
    return any(b in GPU_BACKENDS for b in (backends or []))


def server_supports_flag(exe: Path, flag: str) -> bool:
    """Whether this llama-server build knows a flag.

    llama.cpp renames and adds flags often (``--jinja``, ``--reasoning-format``,
    ``--n-cpu-moe``, ``--flash-attn`` all changed shape over a year), and an
    unknown flag makes the server exit immediately. Probing ``--help`` once and
    caching keeps a stale binary from breaking the whole model group.
    """
    return flag in server_help_text(exe)


def build_server_command(
    exe: Path, model_path: Path, alias: str, port: int,
    context: int, gpu_layers: int, threads: int, host: str = "127.0.0.1",
    parallel: int = 1, quantized_kv: bool = True,
    cpu_moe: int = 0,
    mmproj: Optional[Path] = None,
    jinja: bool = True,
    reasoning: bool = False,
    probe_flags: bool = True,
) -> List[str]:
    """llama-server argv for one model of the group.

    Two flags here are worth more than any amount of quantisation tuning:

    * ``--jinja`` uses the model's own chat template instead of llama.cpp's
      generic one. On 2025+ models that is the difference between tool calls
      parsing correctly and the agent silently losing its tools — and psd.ai's
      agent is tool-driven, so this is not optional in practice.
    * ``--n-cpu-moe`` keeps an MoE's expert weights in RAM while its attention
      runs on the GPU, which is how a 35B-class model becomes usable on a
      12–16 GB card instead of impossible.

    Both are probed against ``--help`` first: an unknown flag makes
    llama-server exit immediately, and a distro-packaged or older binary must
    not take the whole model group down with it.
    """
    cmd = [
        str(exe),
        "--model", str(model_path),
        "--alias", alias,
        "--host", host,
        "--port", str(port),
        "--ctx-size", str(context),
        "--n-gpu-layers", str(gpu_layers),
        "--threads", str(max(1, threads)),
        "--parallel", str(max(1, parallel)),
    ]
    # Flash attention plus a q8_0 KV cache roughly halves KV memory - on a
    # 16 GB laptop that is the difference between a 4k and a 32k window - at a
    # quality cost too small to measure.
    if quantized_kv:
        cmd += ["--flash-attn", "on", "--cache-type-k", "q8_0", "--cache-type-v", "q8_0"]

    def _supported(flag: str) -> bool:
        return (not probe_flags) or server_supports_flag(exe, flag)

    if jinja and _supported("--jinja"):
        cmd += ["--jinja"]
    if cpu_moe > 0 and _supported("--n-cpu-moe"):
        cmd += ["--n-cpu-moe", str(int(cpu_moe))]
    if reasoning and _supported("--reasoning-format"):
        # Surface a thinking model's reasoning as `reasoning_content` so the
        # app's reasoning panel works with local models too.
        cmd += ["--reasoning-format", "auto"]
    if mmproj is not None and Path(mmproj).exists():
        cmd += ["--mmproj", str(mmproj)]
    cmd += ["--no-webui"]
    return cmd


def plan_parallel(plan: Plan, system: Dict[str, Any]) -> int:
    """One person talks to this server, so do not split the context window
    across slots: a 4-way split turns 32k into four 8k windows AND multiplies
    the KV cache four times over. Keep two slots only where memory is not the
    binding constraint."""
    if plan.file.size_gb > 14.0 or not has_usable_gpu(system):
        return 1
    return 2


# ── Network helpers ──────────────────────────────────────────────────────────
def _http_get_json(url: str, timeout: float = 30.0) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def list_repo_files(repo: str, revision: str = "main") -> List[Dict[str, Any]]:
    """File listing (path + size) for a Hugging Face repo."""
    url = f"{HF_API}/models/{repo}/tree/{revision}?recursive=false"
    data = _http_get_json(url, timeout=45.0)
    if not isinstance(data, list):
        return []
    return [e for e in data if isinstance(e, dict) and e.get("type") == "file"]


# A transfer that trickles slower than this for _STALL_WINDOW seconds is
# effectively dead. HF's CDN/xet connections sometimes degrade to a few KB/s
# instead of dropping, so the socket timeout never fires and a first-run
# download crawls for minutes on end - which used to end with the whole model
# group giving up. Reconnecting fixes it almost every time: the .part file
# stays on disk and the next attempt resumes it with a Range header.
_STALL_WINDOW = 30.0        # seconds of measurement
_STALL_MIN_RATE = 8 * 1024  # bytes/second


class _DownloadStalled(IOError):
    """Internal signal: this connection is trickling, open a fresh one."""


def _download_once(url: str, dest: Path, tmp: Path, expected_size: int,
                   label: str) -> Path:
    """One connection attempt; resumes from whatever .part bytes exist."""
    have = tmp.stat().st_size if tmp.exists() else 0
    if dest.exists() and (not expected_size or dest.stat().st_size == expected_size):
        return dest
    if expected_size and have >= expected_size:
        tmp.replace(dest)
        return dest

    headers = {"User-Agent": USER_AGENT}
    if have:
        headers["Range"] = f"bytes={have}-"
    req = urllib.request.Request(url, headers=headers)
    total = expected_size or 0
    last = 0.0
    with urllib.request.urlopen(req, timeout=120) as resp:
        if resp.status == 200:
            # Server ignored Range → start over.
            have = 0
            mode = "wb"
        else:
            mode = "ab"
        if not total:
            try:
                total = int(resp.headers.get("Content-Length") or 0) + have
            except ValueError:
                total = 0
        chunk = 1024 * 1024
        last_check = time.time()
        last_have = have
        with open(tmp, mode) as fh:
            while True:
                buf = resp.read(chunk)
                if not buf:
                    break
                fh.write(buf)
                have += len(buf)
                now = time.time()
                if now - last >= 2.0 and total:
                    pct = have * 100.0 / total
                    log(f"      {label or dest.name}: {pct:5.1f}%  {have / GB:.2f}/{total / GB:.2f} GB")
                    last = now
                elapsed = now - last_check
                if elapsed >= _STALL_WINDOW and elapsed > 0:
                    rate = (have - last_have) / elapsed
                    if rate < _STALL_MIN_RATE:
                        raise _DownloadStalled(
                            f"transfer stalled at {rate / 1024:.1f} KB/s "
                            f"({have / GB:.2f}/{(total or have) / GB:.2f} GB)")
                    last_check, last_have = now, have
    if total and have < total:
        raise IOError(f"download of {dest.name} stopped at {have} of {total} bytes")
    tmp.replace(dest)
    log(f"      {label or dest.name}: done ({have / GB:.2f} GB)")
    return dest


def download_file(url: str, dest: Path, expected_size: int = 0, label: str = "",
                  attempts: int = 5) -> Path:
    """Resumable download with a coarse progress line and automatic retries.

    A dropped, truncated or trickling connection is not fatal: the .part file
    survives, so every retry resumes where the previous attempt stopped on a
    fresh connection. Only when `attempts` connections all fail does this
    raise - one dead CDN socket no longer kills the whole first-run group.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    name = label or dest.name
    attempts = max(1, attempts)
    for attempt in range(1, attempts + 1):
        try:
            return _download_once(url, dest, tmp, expected_size, label)
        except OSError as exc:
            # OSError covers URLError/HTTPError, socket timeouts, the
            # truncated-transfer IOError and _DownloadStalled alike.
            if attempt >= attempts:
                raise
            have = tmp.stat().st_size if tmp.exists() else 0
            log(f"      [warn] {name}: {exc}")
            log(f"      [warn] {name}: reconnecting ({attempt + 1}/{attempts}), "
                f"resuming from {have / GB:.2f} GB already on disk...")
            time.sleep(min(30.0, 2.0 * attempt))
    raise IOError(f"download of {dest.name} failed after {attempts} attempts")


# ── llama.cpp install ────────────────────────────────────────────────────────
def find_llama_server() -> Optional[Path]:
    """The llama-server binary in the device runtime folder, if one is there."""
    if not LLAMA_DIR.exists():
        return None
    hits = sorted(LLAMA_DIR.rglob("llama-server"))
    executable = [p for p in hits if os.access(p, os.X_OK)]
    return (executable or hits)[0] if hits else None


def _safe_extract_tar(archive: Path, dest: Path) -> None:
    """Untar into ``dest``, refusing members that would escape it.

    Release archives are downloaded over HTTPS from GitHub, but a path like
    ``../../.bashrc`` inside one would still be honoured by a naive
    ``extractall`` — so every member is checked before it is written.
    """
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    with tarfile.open(archive, "r:*") as tar:
        members = []
        for member in tar.getmembers():
            target = (dest / member.name).resolve()
            if target != root and root not in target.parents:
                raise RuntimeError(f"refusing to extract '{member.name}' outside {dest}")
            if member.issym() or member.islnk():
                link = Path(os.path.normpath(os.path.join(dest, member.linkname))).resolve()
                if link != root and root not in link.parents:
                    raise RuntimeError(f"refusing to extract link '{member.name}' outside {dest}")
            members.append(member)
        tar.extractall(dest, members=members)


def _make_executables_runnable(directory: Path) -> None:
    """chmod +x every binary in a freshly unpacked llama.cpp tree.

    Some archives lose the executable bit through the packaging step, and a
    llama-server without +x fails with a confusing PermissionError later.
    """
    for path in directory.rglob("*"):
        if not path.is_file() or path.suffix in (".so", ".a", ".txt", ".json", ".h"):
            continue
        if path.name.startswith("llama-") or path.name.startswith("ggml"):
            try:
                mode = path.stat().st_mode
                path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            except OSError:
                pass


def install_llama_cpp(system: Dict[str, Any], prefer: Sequence[str] = ()) -> Tuple[Path, List[str]]:
    """Download + unpack a prebuilt Linux llama.cpp, return (exe, backends)."""
    _require_linux()
    existing = find_llama_server()
    if existing is not None:
        return existing, detect_llama_backends(existing)

    # A distro-built llama-server (``dnf install llama.cpp`` from COPR, or a
    # hand-compiled one) is preferred over a download: it is already linked
    # against this machine's CUDA/ROCm/Vulkan stack.
    on_path = shutil.which("llama-server")
    if on_path:
        return Path(on_path), detect_llama_backends(Path(on_path))

    arch = cpu_arch(system)
    releases = _http_get_json(f"{GH_API}/repos/ggml-org/llama.cpp/releases?per_page=15", timeout=45.0)
    picked = pick_llama_release(releases if isinstance(releases, list) else [], arch=arch)
    if not picked:
        raise RuntimeError("could not find a llama.cpp release with Linux binaries")
    tag = str(picked["tag"])

    kinds = list(prefer) or linux_backend_candidates(system)
    errors: List[str] = []
    for kind in kinds:
        asset = match_release_asset(picked["assets"], tag, kind, arch)
        if not asset:
            continue
        url = str(asset.get("browser_download_url") or "")
        if not url:
            continue
        name = str(asset.get("name") or f"llama-{tag}-{kind}")
        size_gb = int(asset.get("size") or 0) / GB
        log(f"  ==> Downloading llama.cpp {tag} ({kind}, {arch}, {size_gb:.2f} GB)...")
        archive = RUNTIME_DIR / f"{name}"
        try:
            download_file(url, archive, int(asset.get("size") or 0), label=f"llama.cpp {kind}")
            LLAMA_DIR.mkdir(parents=True, exist_ok=True)
            _safe_extract_tar(archive, LLAMA_DIR)
            # The CUDA build dlopens libcudart at startup. Fedora machines with
            # only the driver (no CUDA toolkit) need the companion bundle, so
            # fetch it into the same tree — otherwise the server dies with
            # "libcudart.so.12: cannot open shared object file".
            if kind == "cuda":
                version = cuda_version_of(asset)
                cudart = match_cudart_asset(picked["assets"], tag, arch, version) if version else None
                if cudart:
                    cudart_name = str(cudart.get("name") or "cudart.tar.gz")
                    cudart_size = int(cudart.get("size") or 0)
                    log(f"  ==> Downloading CUDA runtime bundle ({cudart_size / GB:.2f} GB)...")
                    cudart_archive = RUNTIME_DIR / cudart_name
                    try:
                        download_file(
                            str(cudart.get("browser_download_url") or ""),
                            cudart_archive, cudart_size, label="cudart",
                        )
                        _safe_extract_tar(cudart_archive, LLAMA_DIR)
                        cudart_archive.unlink(missing_ok=True)
                    except Exception as exc:
                        log(f"  [warn] CUDA runtime bundle failed ({exc}) — the GPU build may not start.")
            archive.unlink(missing_ok=True)
            _make_executables_runnable(LLAMA_DIR)
            exe = find_llama_server()
            if exe is None:
                raise RuntimeError("llama-server missing from the downloaded archive")
            backends = detect_llama_backends(exe)
            log(f"  ==> llama.cpp {tag} ready at {exe} (backends: {', '.join(backends)})")
            return exe, backends
        except Exception as exc:  # try the next backend build
            errors.append(f"{kind}: {exc}")
            log(f"  [warn] llama.cpp {kind} build failed ({exc}) — trying the next one.")
    raise RuntimeError(
        "could not install llama.cpp: " + "; ".join(errors)
        + " — or install it yourself (Fedora: sudo dnf install llama.cpp) and re-run."
    )


# ── Server lifecycle ─────────────────────────────────────────────────────────
def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex((host, port)) == 0


def start_server(cmd: List[str]) -> Tuple[subprocess.Popen, Any]:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log_fh = open(LOG_FILE, "ab", buffering=0)
    kwargs: Dict[str, Any] = {"stdout": log_fh, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL}
    # Own process group (setsid): Ctrl+C in the launcher terminal must not kill
    # the model server before we get a chance to shut it down cleanly, and a
    # systemd user unit stopping the launcher must not orphan-kill it either.
    kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **kwargs)

    def _cleanup() -> None:
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    atexit.register(_cleanup)
    return proc, log_fh


def _health_and_model(base: str, timeout: float = 10.0) -> Optional[str]:
    """Return the served model id once /health says ok."""
    try:
        with urllib.request.urlopen(f"{base}/health", timeout=timeout) as resp:
            if getattr(resp, "status", 200) != 200:
                return None
    except Exception:
        return None
    try:
        with urllib.request.urlopen(f"{base}/v1/models", timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return "psd-llama"
    models = data.get("data") if isinstance(data, dict) else None
    if isinstance(models, list) and models and isinstance(models[0], dict):
        return str(models[0].get("id") or "psd-llama")
    return "psd-llama"


def wait_until_healthy(base: str, proc: subprocess.Popen, timeout: float = 900.0) -> str:
    """Block until llama-server answers /health. Returns the model id."""
    deadline = time.time() + timeout
    next_notice = time.time() + 15
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"llama-server exited with code {proc.returncode} — see {LOG_FILE}"
            )
        model_id = _health_and_model(base)
        if model_id:
            return model_id
        if time.time() >= next_notice:
            log("      loading model into memory...")
            next_notice = time.time() + 30
        time.sleep(1.0)
    raise RuntimeError(f"llama-server did not become healthy in {timeout:.0f}s — see {LOG_FILE}")


def stop_server(proc: Optional[subprocess.Popen]) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def stop_servers(processes: Iterable[Optional[subprocess.Popen]]) -> None:
    """Stop every member of a local model group, best-effort."""
    for proc in reversed(list(processes)):
        stop_server(proc)


# ── App registration ─────────────────────────────────────────────────────────
ENDPOINT_NAME = "psd.ai Local Llama"


def register_endpoint(base_url: str, model_id: str, plan: Plan) -> str:
    """Insert/refresh the local endpoint row and return its id."""
    from core.database import Base, ModelEndpoint, SessionLocal, engine

    Base.metadata.create_all(bind=engine)  # idempotent; covers a first run
    endpoint_id = f"local-llama-{plan.spec.id}"
    db = SessionLocal()
    try:
        row = db.query(ModelEndpoint).filter(ModelEndpoint.id == endpoint_id).first()
        if row is None:
            row = ModelEndpoint(id=endpoint_id)
            db.add(row)
        row.name = f"{ENDPOINT_NAME} · {plan.spec.label}"
        row.base_url = base_url
        row.is_enabled = True
        row.model_type = "llm"
        row.endpoint_kind = "local"
        row.supports_tools = True
        row.cached_models = json.dumps([model_id])
        row.hidden_models = json.dumps([])
        db.commit()
        return endpoint_id
    finally:
        db.close()


def set_default_model(endpoint_id: str, model_id: str, force: bool = False) -> bool:
    """Point the app's default chat model at the local Llama.

    Only fills an empty default unless ``force`` is set, so a user who already
    chose OpenRouter & co. keeps their pick.
    """
    from src.settings import load_settings, save_settings

    settings = load_settings()
    if not force and (settings.get("default_endpoint_id") or "").strip():
        return False
    settings["default_endpoint_id"] = endpoint_id
    settings["default_model"] = model_id
    if not (settings.get("task_endpoint_id") or "").strip():
        settings["task_endpoint_id"] = endpoint_id
        settings["task_model"] = model_id
    if not (settings.get("utility_endpoint_id") or "").strip():
        settings["utility_endpoint_id"] = endpoint_id
        settings["utility_model"] = model_id
    # Without this a non-admin user's composer never resolves the global
    # default (routes/model_routes.py::get_default_chat), which would leave a
    # fresh install with an empty model picker.
    if not settings.get("share_defaults_with_users", False):
        settings["share_defaults_with_users"] = True
    save_settings(settings)
    return True


def write_state(payload: Dict[str, Any]) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


# ── Orchestration ────────────────────────────────────────────────────────────
def model_repo_paths(plan: Plan, part: str = "") -> Path:
    """On-disk location of one model file (or one shard of a split release)."""
    directory = MODELS_DIR / plan.file.repo.replace("/", "__")
    return directory / Path(part or plan.file.path).name


def collect_repo_files(
    specs: Sequence[ModelSpec],
    fetch_files=list_repo_files,
    skip_download: bool = False,
) -> Tuple[Dict[str, List[GGUFFile]], Dict[str, List[Dict[str, Any]]]]:
    """List every catalogue mirror once: (candidates, raw tree entries).

    Candidates drive the planner; the raw listing is kept because a vision
    projector (``mmproj-*.gguf``) is deliberately not a model candidate and can
    only be found in the unfiltered tree. A mirror that 404s (a renamed or
    gated repo) is a warning and an empty list — the next mirror takes over.
    """
    if skip_download:
        raw = _raw_from_disk()
        return _files_from_disk(), raw

    candidates: Dict[str, List[GGUFFile]] = {}
    raw: Dict[str, List[Dict[str, Any]]] = {}
    for spec in specs:
        for repo in spec.repos:
            if repo in candidates:
                continue
            try:
                entries = fetch_files(repo)
            except Exception as exc:
                log(f"  [warn] could not list {repo}: {exc}")
                candidates[repo] = []
                raw[repo] = []
                continue
            entries = [e for e in (entries or []) if isinstance(e, dict)]
            raw[repo] = entries
            candidates[repo] = candidate_files(repo, entries)
    return candidates, raw


def download_plan_files(
    plan: Plan,
    downloader=download_file,
    skip_download: bool = False,
) -> Path:
    """Fetch every file a plan needs and return the path to hand llama-server.

    Split releases download all of their shards (resumable, in order) so a
    partial model can never be started.
    """
    primary = model_repo_paths(plan)
    missing = [
        part for part in plan.file.all_paths
        if not _part_on_disk(plan, part)
    ]
    if not missing:
        return primary
    if skip_download:
        raise RuntimeError(f"model file missing and downloads are disabled: {primary}")
    for part in plan.file.all_paths:
        target = model_repo_paths(plan, part)
        if not any(part == m for m in missing):
            continue
        label = f"{plan.file.filename}" if len(plan.file.all_paths) == 1 else Path(part).name
        log(f"  ==> Downloading {label} ...")
        downloader(
            f"{HF_ORIGIN}/{plan.file.repo}/resolve/main/{part}",
            target,
            _part_size(plan, part),
            label=label,
        )
    return primary


def _part_size(plan: Plan, part: str) -> int:
    """Expected byte size of one shard (the whole file for single-file models)."""
    paths = plan.file.all_paths
    if len(paths) == 1:
        return int(plan.file.size)
    try:
        index = paths.index(part)
    except ValueError:
        return 0
    sizes = plan.file.part_sizes
    return int(sizes[index]) if index < len(sizes) else 0


def _part_on_disk(plan: Plan, part: str) -> bool:
    """True when a shard is present AND complete (size matches the listing)."""
    target = model_repo_paths(plan, part)
    expected = _part_size(plan, part)
    try:
        if not target.exists():
            return False
        actual = target.stat().st_size
    except OSError:
        return False
    return actual > 0 and (expected == 0 or actual == expected)


def download_mmproj(
    plan: Plan,
    raw_by_repo: Dict[str, List[Dict[str, Any]]],
    downloader=download_file,
    skip_download: bool = False,
) -> Optional[Path]:
    """Attach the vision projector for a multimodal model, if the repo has one.

    Returns the local path, or None when the model is text-only or the
    projector could not be fetched (a missing projector only costs vision —
    the model itself still serves text, so this never raises).
    """
    if not plan.spec.vision:
        return None
    projector: Optional[GGUFFile] = None
    for repo in plan.spec.repos:
        projector = find_mmproj(repo, raw_by_repo.get(repo) or [])
        if projector is not None:
            break
    if projector is None:
        return None
    target = MODELS_DIR / plan.file.repo.replace("/", "__") / Path(projector.path).name
    try:
        if target.exists() and target.stat().st_size == projector.size:
            return target
        if skip_download:
            return target if target.exists() else None
        log(f"  ==> Downloading vision projector ({projector.size_gb:.2f} GB)...")
        downloader(
            f"{HF_ORIGIN}/{projector.repo}/resolve/main/{projector.path}",
            target, projector.size, label="mmproj",
        )
        return target
    except Exception as exc:
        log(f"  [warn] vision projector unavailable ({exc}) — running text-only.")
        return None


def prepare_local_llama(
    port: int = DEFAULT_PORT,
    prefer_id: str = "",
    host: str = "127.0.0.1",
    skip_download: bool = False,
    threads: int = 0,
    fetch_files=list_repo_files,
    downloader=download_file,
    installer=install_llama_cpp,
    health_timeout: float = 900.0,
    set_default: bool = True,
    system: Optional[Dict[str, Any]] = None,
    profile: str = "",
) -> Dict[str, Any]:
    """Download + start the best local model. Returns a status dict."""
    _require_linux()
    prof = active_profile(profile)
    system = system if system is not None else detect_hardware()
    log("")
    log(f"  Hardware: {system.get('cpu_name') or 'unknown CPU'}, "
        f"{system.get('total_ram_gb') or '?'} GB RAM, "
        f"{system.get('gpu_name') or 'no GPU'}")
    budget = memory_budget_gb(system, prof)
    log(f"  Memory budget for the model: {budget:.1f} GB (profile: {prof.name})")

    files_by_repo, raw_by_repo = collect_repo_files(MODEL_SPECS, fetch_files, skip_download)

    plan = plan_model(system, files_by_repo, prefer_id=prefer_id, profile=prof)
    if plan is None:
        raise RuntimeError("no downloadable Llama GGUF found — check the network connection")

    log(f"  Chosen model : {plan.spec.label} ({plan.quant}, {plan.file.size_gb:.2f} GB)")
    log(f"  Source repo  : {plan.file.repo}")
    if plan.file.is_split:
        log(f"  Split release: {len(plan.file.all_paths)} parts")
    if plan.note:
        log(f"  Note         : {plan.note}")

    exe, backends = installer(system)
    gpu_layers = plan_gpu_layers(system, plan)
    if gpu_layers and not has_gpu_backend(backends):
        log("  [info] this llama-server build is CPU-only — running on the CPU.")
        gpu_layers = 0
    cpu_moe = plan_cpu_moe(system, plan, gpu_layers)

    shortfall = disk_shortfall_gb([plan], MODELS_DIR)
    if shortfall > 0:
        log(f"  [warn] only {free_disk_gb(MODELS_DIR):.0f} GB free — this download needs "
            f"about {shortfall:.0f} GB more. Free some space, or point "
            f"PSD_AI_RUNTIME_DIR at a larger filesystem.")

    model_path = download_plan_files(plan, downloader, skip_download)
    if not model_path.exists():
        raise RuntimeError(f"model file missing and downloads are disabled: {model_path}")
    mmproj = download_mmproj(plan, raw_by_repo, downloader, skip_download)

    alias = plan.spec.alias
    base_url = f"http://{host}:{port}/v1"
    threads = threads or plan_threads(system)

    proc: Optional[subprocess.Popen] = None
    log_fh = None
    model_id = alias

    existing = _health_and_model(f"http://{host}:{port}", timeout=3.0)
    if existing:
        log(f"  ==> llama-server already running on port {port} — reusing it.")
        model_id = existing
        endpoint_id = register_endpoint(base_url, model_id, plan)
        became_default = set_default_model(endpoint_id, model_id) if set_default else False
        state = {
            "model_id": model_id, "alias": alias, "spec_id": plan.spec.id,
            "label": plan.spec.label, "quant": plan.quant, "repo": plan.file.repo,
            "file": str(model_path), "file_size_gb": round(plan.file.size_gb, 2),
            "endpoint_id": endpoint_id, "base_url": base_url, "port": port,
            "context": plan.context, "gpu_layers": gpu_layers, "backends": backends,
            "cpu_moe": cpu_moe, "vision": bool(mmproj), "mmproj": str(mmproj or ""),
            "profile": prof.name, "pid": None, "reused": True,
        }
        write_state(state)
        log(f"  ==> Local model ready: {model_id} at {base_url}")
        if became_default:
            log("  ==> Set as the default chat model for psd.ai")
        return {"ok": True, "proc": None, **state}

    # Port 8080 is a popular default. If something that is NOT our llama-server
    # already holds it, starting would fail with an opaque bind error, so say
    # plainly what happened and how to move off the port.
    if port_in_use(port, host):
        raise RuntimeError(
            f"port {port} is already in use by another program "
            f"(on Fedora: `ss -ltnp | grep {port}`). Close it, or set LLAMA_PORT "
            f"to a free port before running ./run.sh."
        )

    try:
        attempts: List[Tuple[int, str]] = [(gpu_layers, "gpu")]
        if gpu_layers:
            attempts.append((0, "cpu"))
        last_error: Optional[Exception] = None
        for layers, mode in attempts:
            cmd = build_server_command(
                exe, model_path, alias, port, plan.context, layers, threads, host=host,
                parallel=plan_parallel(plan, system),
                cpu_moe=cpu_moe if layers else 0,
                mmproj=mmproj,
                reasoning=plan.spec.thinking,
            )
            extras = []
            if layers and cpu_moe:
                extras.append(f"cpu-moe {cpu_moe}")
            if mmproj:
                extras.append("vision")
            log(f"  ==> Starting llama-server ({mode}, ctx {plan.context}, ngl {layers}"
                f"{', ' + ', '.join(extras) if extras else ''}) on port {port}...")
            proc, log_fh = start_server(cmd)
            try:
                model_id = wait_until_healthy(f"http://{host}:{port}", proc, timeout=health_timeout)
                # Record what is ACTUALLY serving: a GPU start that died and
                # fell back to CPU must not be reported as ngl=-1.
                gpu_layers = layers
                break
            except Exception as exc:
                last_error = exc
                stop_server(proc)
                proc = None
                log(f"  [warn] {mode} start failed: {exc}")
        else:
            raise RuntimeError(f"llama-server would not start: {last_error}")
    except Exception:
        stop_server(proc)
        if log_fh:
            log_fh.close()
        raise

    if log_fh:
        log_fh.close()

    endpoint_id = register_endpoint(base_url, model_id, plan)
    became_default = set_default_model(endpoint_id, model_id) if set_default else False
    state = {
        "model_id": model_id,
        "alias": alias,
        "spec_id": plan.spec.id,
        "label": plan.spec.label,
        "quant": plan.quant,
        "repo": plan.file.repo,
        "file": str(model_path),
        "file_size_gb": round(plan.file.size_gb, 2),
        "endpoint_id": endpoint_id,
        "base_url": base_url,
        "port": port,
        "context": plan.context,
        "gpu_layers": gpu_layers,
        "backends": backends,
        "cpu_moe": cpu_moe,
        "vision": bool(mmproj),
        "mmproj": str(mmproj or ""),
        "profile": prof.name,
        "pid": proc.pid if proc else None,
    }
    write_state(state)
    log(f"  ==> Local model ready: {model_id} at {base_url}")
    if became_default:
        log("  ==> Set as the default chat model for psd.ai")
    return {"ok": True, "proc": proc, **state}


def prepare_local_llama_group(
    port: int = DEFAULT_PORT,
    prefer_id: str = "",
    host: str = "127.0.0.1",
    skip_download: bool = False,
    threads: int = 0,
    fetch_files=list_repo_files,
    downloader=download_file,
    installer=install_llama_cpp,
    health_timeout: float = 900.0,
    set_default: bool = True,
    system: Optional[Dict[str, Any]] = None,
    min_models: int = MIN_GROUP_MODELS,
    max_models: int = MAX_GROUP_MODELS,
    profile: str = "",
    prefer_tier: str = "",
) -> Dict[str, Any]:
    """Download and run a hardware-fit group of three to six models.

    llama-server is intentionally launched once per model on consecutive ports
    (the base port is the first model). Weight downloads run concurrently, so a
    first install does not wait for five multi-GB files one after another.
    """
    _require_linux()
    prof = active_profile(profile)
    min_models = max(MIN_GROUP_MODELS, min(MAX_GROUP_MODELS, int(min_models)))
    max_models = max(min_models, min(MAX_GROUP_MODELS, int(max_models)))
    system = system if system is not None else detect_hardware()
    log("")
    log(f"  Hardware: {system.get('cpu_name') or 'unknown CPU'}, "
        f"{system.get('total_ram_gb') or '?'} GB RAM, "
        f"{system.get('gpu_name') or 'no GPU'}")
    budget = group_memory_budget_gb(system, prof)
    log(f"  Memory budget for the model group: {budget:.1f} GB (profile: {prof.name})")

    files_by_repo, raw_by_repo = collect_repo_files(MODEL_SPECS, fetch_files, skip_download)

    group = plan_model_group(
        system,
        files_by_repo,
        prefer_id=prefer_id,
        min_models=min_models,
        max_models=max_models,
        profile=prof,
        prefer_tier=prefer_tier,
    )
    if group is None or len(group.plans) < min_models:
        raise RuntimeError(
            f"could not find {min_models} downloadable models for this machine "
            f"({group_memory_budget_gb(system, prof):.1f} GB budget)"
        )

    log(f"  Model group  : {len(group.plans)} models, {group.total_gb:.1f} GB resident")
    for index, plan in enumerate(group.plans, 1):
        flags = []
        if plan.spec.moe:
            flags.append(f"MoE {plan.spec.active_params_b:g}B active")
        if plan.spec.vision:
            flags.append("vision")
        if plan.file.is_split:
            flags.append(f"{len(plan.file.all_paths)} parts")
        suffix = f" [{', '.join(flags)}]" if flags else ""
        log(f"    {index}. {plan.spec.label} ({plan.quant}, {plan.file.size_gb:.2f} GB, "
            f"ctx {plan.context}){suffix}")
    if group.note:
        log(f"  Group note   : {group.note}")

    exe, backends = installer(system)
    threads = threads or plan_threads(system)

    shortfall = disk_shortfall_gb(group.plans, MODELS_DIR)
    if shortfall > 0:
        log(f"  [warn] only {free_disk_gb(MODELS_DIR):.0f} GB free where the models live — "
            f"the group needs about {shortfall:.0f} GB more. Free some space, or set "
            f"PSD_AI_RUNTIME_DIR to a larger filesystem (see ./run.sh --doctor).")

    entries: List[Tuple[Plan, Path]] = [(plan, model_repo_paths(plan)) for plan in group.plans]

    def _download_one(item: Tuple[Plan, Path]) -> Tuple[Plan, Path, Optional[Path]]:
        plan, _model_path = item
        log(f"  ==> Downloading {plan.spec.label} ({plan.file.size_gb:.2f} GB)...")
        path = download_plan_files(plan, downloader, skip_download)
        projector = download_mmproj(plan, raw_by_repo, downloader, skip_download)
        return plan, path, projector

    # Downloading independent GGUF files concurrently makes a first run much
    # faster on a connection that can sustain multiple streams. Limit workers
    # to three so the launcher does not overwhelm a laptop or an HF mirror.
    mmproj_by_spec: Dict[str, Optional[Path]] = {}
    with ThreadPoolExecutor(max_workers=min(3, len(entries))) as pool:
        futures = {pool.submit(_download_one, item): item for item in entries}
        for future in as_completed(futures):
            plan, _path, projector = future.result()
            mmproj_by_spec[plan.spec.id] = projector

    processes: List[subprocess.Popen] = []
    states: List[Dict[str, Any]] = []
    pending: List[Tuple[Plan, Path, int, int, int, Optional[Path], subprocess.Popen, Any]] = []

    for index, (plan, model_path) in enumerate(entries):
        model_port = port + index
        base_url = f"http://{host}:{model_port}/v1"
        alias = plan.spec.alias
        mmproj = mmproj_by_spec.get(plan.spec.id)
        gpu_layers = plan_gpu_layers(system, plan)
        if gpu_layers and not has_gpu_backend(backends):
            gpu_layers = 0
        cpu_moe = plan_cpu_moe(system, plan, gpu_layers)
        existing = _health_and_model(f"http://{host}:{model_port}", timeout=3.0)
        if existing:
            log(f"  ==> {plan.spec.label} already running on port {model_port} — reusing it.")
            endpoint_id = register_endpoint(base_url, existing, plan)
            became_default = set_default_model(endpoint_id, existing) if set_default and not states else False
            states.append({
                "model_id": existing, "alias": alias, "spec_id": plan.spec.id,
                "label": plan.spec.label, "quant": plan.quant, "repo": plan.file.repo,
                "file": str(model_path), "file_size_gb": round(plan.file.size_gb, 2),
                "endpoint_id": endpoint_id, "base_url": base_url, "port": model_port,
                "context": plan.context, "gpu_layers": gpu_layers, "backends": backends,
                "cpu_moe": cpu_moe, "vision": bool(mmproj), "mmproj": str(mmproj or ""),
                "profile": prof.name,
                "pid": None, "reused": True, "became_default": became_default,
            })
            continue
        if port_in_use(model_port, host):
            log(f"  [warn] port {model_port} is busy; skipping {plan.spec.label}")
            continue
        cmd = build_server_command(
            exe, model_path, alias, model_port, plan.context, gpu_layers, threads,
            host=host, parallel=1, cpu_moe=cpu_moe, mmproj=mmproj,
            reasoning=plan.spec.thinking,
        )
        log(f"  ==> Starting {plan.spec.label} on port {model_port} "
            f"(ngl {gpu_layers}{f', cpu-moe {cpu_moe}' if cpu_moe else ''})...")
        proc, log_fh = start_server(cmd)
        processes.append(proc)
        pending.append((plan, model_path, model_port, gpu_layers, cpu_moe, mmproj, proc, log_fh))

    for plan, model_path, model_port, gpu_layers, cpu_moe, mmproj, proc, log_fh in pending:
        base_url = f"http://{host}:{model_port}"
        try:
            model_id = wait_until_healthy(base_url, proc, timeout=health_timeout)
            endpoint_id = register_endpoint(f"{base_url}/v1", model_id, plan)
            became_default = set_default_model(endpoint_id, model_id) if set_default and not states else False
            states.append({
                "model_id": model_id, "alias": plan.spec.alias, "spec_id": plan.spec.id,
                "label": plan.spec.label, "quant": plan.quant, "repo": plan.file.repo,
                "file": str(model_path), "file_size_gb": round(plan.file.size_gb, 2),
                "endpoint_id": endpoint_id, "base_url": f"{base_url}/v1", "port": model_port,
                "context": plan.context, "gpu_layers": gpu_layers, "backends": backends,
                "cpu_moe": cpu_moe, "vision": bool(mmproj), "mmproj": str(mmproj or ""),
                "profile": prof.name,
                "pid": proc.pid, "became_default": became_default,
            })
            log(f"  ==> {plan.spec.label} ready at {base_url}/v1")
        except Exception as exc:
            log(f"  [warn] {plan.spec.label} failed to start: {exc}")
            stop_server(proc)
        finally:
            if log_fh:
                log_fh.close()

    if len(states) < min_models:
        stop_servers(processes)
        raise RuntimeError(
            f"only {len(states)} of the required {min_models} local models became ready "
            f"(see {LOG_FILE})"
        )

    payload = {
        "mode": "group",
        "count": len(states),
        "requested_min": min_models,
        "requested_max": max_models,
        "budget_gb": group.budget_gb,
        "resident_gb": round(group.total_gb, 2),
        "fits": group.fits,
        "profile": prof.name,
        "models": states,
        "ports": [state["port"] for state in states],
        "primary_model_id": states[0]["model_id"],
        "primary_endpoint_id": states[0]["endpoint_id"],
    }
    write_state(payload)
    GROUP_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    GROUP_STATE_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    log(f"  ==> Local model group ready: {len(states)} models on ports {', '.join(map(str, payload['ports']))}")
    return {"ok": True, "proc": processes[0] if processes else None, "procs": processes, **payload}


def _files_from_disk() -> Dict[str, List[GGUFFile]]:
    """Build a file map from what is already on disk (offline re-runs)."""
    out: Dict[str, List[GGUFFile]] = {}
    for repo, entries in _raw_from_disk().items():
        out[repo] = candidate_files(repo, entries)
    return out


def _raw_from_disk() -> Dict[str, List[Dict[str, Any]]]:
    """Unfiltered on-disk tree per repo, shaped like the HF listing API."""
    out: Dict[str, List[Dict[str, Any]]] = {}
    if not MODELS_DIR.exists():
        return out
    for repo_dir in MODELS_DIR.iterdir():
        if not repo_dir.is_dir():
            continue
        repo = repo_dir.name.replace("__", "/")
        entries: List[Dict[str, Any]] = []
        for path in sorted(repo_dir.rglob("*.gguf")):
            try:
                size = path.stat().st_size
            except OSError:
                continue
            entries.append({
                "path": str(path.relative_to(repo_dir)),
                "size": size,
            })
        out[repo] = entries
    return out


def _model_log_tail() -> str:
    """Last line of run.sh's model log, so the wait message names the work.

    The bootstrap writes its download/serve progress to
    ``<repo root>/logs/local-model.log``; quoting its tail turns forty
    identical "still waiting" lines into one line that says which weight file
    is moving. Empty string when the log is not there (headless callers).
    """
    path = Path(__file__).resolve().parents[2] / "logs" / "local-model.log"
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 4096))
            chunk = fh.read().decode("utf-8", "replace")
    except OSError:
        return ""
    lines = [ln.strip() for ln in chunk.splitlines() if ln.strip()]
    return lines[-1][:110] if lines else ""


def wait_for_ready(timeout: float, state_file: Path = STATE_FILE, fail_file: Path = FAIL_FILE,
                   poll: float = 2.0) -> int:
    """Block until a sibling bootstrap finishes (or gives up).

    ``run.sh`` uses this so the app window only opens once the local model is
    registered and the picker can select it. Returns 0 when ready, 3 otherwise
    — either way the caller should still start the app.
    """
    deadline = time.time() + timeout
    started = time.time()
    next_beat = time.time() + 20
    beats = 0
    while time.time() < deadline:
        if state_file.exists():
            log("  ==> Local model is ready.")
            return 0
        if fail_file.exists():
            try:
                reason = fail_file.read_text(encoding="utf-8").strip()
            except OSError:
                reason = ""
            log(f"  [warn] Local model setup gave up{': ' + reason if reason else '.'}")
            log("         Starting psd.ai anyway - you can add a model under Settings > Models.")
            return 3
        if time.time() >= next_beat:
            mins = int((time.time() - started) // 60)
            detail = _model_log_tail()
            if detail:
                log(f"      [{mins:>3}m] still waiting for the local model: {detail}")
            else:
                log(f"      [{mins:>3}m] still waiting for the local model "
                    "(first run downloads a few GB)...")
            beats += 1
            # Frequent at first, then quiet: a 40-line wall of identical
            # messages says less than one line that names the download.
            next_beat = time.time() + (20 if beats < 3 else 60)
        time.sleep(poll)
    log("  [warn] Timed out waiting for the local model - starting psd.ai anyway.")
    return 3


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Download + run a hardware-fit local model group for this PC.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="first model port; additional models use the next ports")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--model", default=os.getenv("PSD_LOCAL_MODEL_ID", ""),
                        help="prefer a model in the group (for example qwen3.6-35b-a3b or gemma-4-12b)")
    parser.add_argument("--profile", default=os.getenv("PSD_MODEL_PROFILE", "balanced"),
                        choices=sorted(POWER_PROFILES),
                        help="how much of this machine the models may use: "
                             "balanced (default, responsive desktop), power (bigger models, "
                             "longer context), max (one model, everything else waits)")
    parser.add_argument("--tier", default=os.getenv("PSD_MODEL_TIER", ""),
                        choices=["", "general", "coding", "reasoning"],
                        help="bias the strongest model in the group towards a specialism")
    parser.add_argument("--min-quant", default=os.getenv("PSD_MODEL_MIN_QUANT", ""),
                        help="refuse quantisations worse than this (default: walk the "
                             "built-in ladder Q4_K_M -> Q3_K_M -> Q2_K -> IQ2_M -> IQ1_M)")
    parser.add_argument("--min-models", type=int, default=MIN_GROUP_MODELS,
                        help="minimum resident models (default: 3)")
    parser.add_argument("--max-models", type=int, default=MAX_GROUP_MODELS,
                        help="maximum resident models (default: 5)")
    parser.add_argument("--single-model", action="store_true",
                        help="legacy mode: download and run only one model")
    parser.add_argument("--threads", type=int, default=0)
    parser.add_argument("--skip-download", action="store_true",
                        help="use only models already downloaded under the device runtime/models folder")
    parser.add_argument("--no-default", action="store_true", help="do not change the app's default model")
    parser.add_argument("--list-models", action="store_true",
                        help="print the model catalogue and exit")
    parser.add_argument("--print", dest="print_only", action="store_true",
                        help="print the hardware-fit model group and exit (no downloads)")
    parser.add_argument("--foreground", action="store_true",
                        help="keep this process attached to all model servers (Ctrl+C stops them)")
    parser.add_argument("--wait-ready", type=int, default=0, metavar="SECONDS",
                        help="block until another instance finishes setup, then exit "
                             "(0 = ready, 3 = timed out or failed)")
    args = parser.parse_args(argv)

    if args.list_models:
        for spec in catalogue_by_strength():
            kind = "MoE" if spec.moe else "dense"
            active = f", {spec.active_params_b:g}B active" if spec.active_params_b else ""
            extras = " ".join(
                tag for tag, on in (("[vision]", spec.vision),
                                    ("[thinking]", spec.thinking),
                                    (f"[{spec.tier}]", spec.tier != "general"))
                if on
            )
            log(f"  {spec.id:<24} {spec.label:<32} {spec.params_b:>6g}B {kind}{active} "
                f"ctx {spec.max_context // 1024}k {extras}".rstrip())
        return 0

    if args.wait_ready:
        return wait_for_ready(args.wait_ready)

    _require_linux()
    prof = active_profile(args.profile)

    log("")
    log("  ============================================================")
    log(f"    {APP_NAME} - local model setup (Linux, profile: {prof.name})")
    log("  ============================================================")

    # A stale success marker would make run.sh open the UI before a new group
    # has finished. Existing servers can still be reused by the planner below.
    FAIL_FILE.unlink(missing_ok=True)
    STATE_FILE.unlink(missing_ok=True)
    GROUP_STATE_FILE.unlink(missing_ok=True)

    try:
        if args.print_only:
            system = detect_hardware()
            files_by_repo, _raw = collect_repo_files(MODEL_SPECS)
            if args.single_model or not prof.group:
                plan = plan_model(
                    system, files_by_repo, prefer_id=args.model,
                    min_quant=args.min_quant, profile=prof,
                )
                if plan is None:
                    log("  No local GGUF reachable.")
                    return 1
                layers = plan_gpu_layers(system, plan)
                log(json.dumps({
                    "mode": "single",
                    "model": plan.spec.label, "spec_id": plan.spec.id, "quant": plan.quant,
                    "size_gb": round(plan.file.size_gb, 2), "repo": plan.file.repo,
                    "file": plan.file.filename, "context": plan.context,
                    "parts": list(plan.file.all_paths),
                    "budget_gb": plan.budget_gb, "gpu_layers": layers,
                    "cpu_moe": plan_cpu_moe(system, plan, layers),
                    "moe": plan.spec.moe, "vision": plan.spec.vision,
                    "profile": prof.name, "fits": plan.fits,
                }, indent=2))
                return 0
            group = plan_model_group(
                system, files_by_repo, prefer_id=args.model, min_quant=args.min_quant,
                min_models=args.min_models, max_models=args.max_models,
                profile=prof, prefer_tier=args.tier,
            )
            if group is None:
                log("  No three-model group is reachable for this machine.")
                return 1
            log(json.dumps({
                "mode": "group", "count": len(group.plans), "budget_gb": group.budget_gb,
                "total_gb": round(group.total_gb, 2), "fits": group.fits,
                "profile": prof.name,
                "models": [
                    {"model": p.spec.label, "spec_id": p.spec.id, "quant": p.quant,
                     "size_gb": round(p.file.size_gb, 2), "repo": p.file.repo,
                     "file": p.file.filename, "context": p.context,
                     "parts": list(p.file.all_paths), "moe": p.spec.moe,
                     "vision": p.spec.vision}
                    for p in group.plans
                ],
            }, indent=2))
            return 0

        if args.single_model or not prof.group:
            if not args.single_model:
                log("  [info] profile 'max' runs one model instead of a group.")
            result = prepare_local_llama(
                port=args.port,
                prefer_id=args.model,
                host=args.host,
                skip_download=args.skip_download,
                threads=args.threads,
                set_default=not args.no_default,
                profile=prof.name,
            )
        else:
            result = prepare_local_llama_group(
                port=args.port,
                prefer_id=args.model,
                host=args.host,
                skip_download=args.skip_download,
                threads=args.threads,
                set_default=not args.no_default,
                min_models=args.min_models,
                max_models=args.max_models,
                profile=prof.name,
                prefer_tier=args.tier,
            )
    except KeyboardInterrupt:
        log("\n  Interrupted.")
        return 130
    except Exception as exc:
        log("")
        log(f"  [warn] Local model setup failed: {exc}")
        log("         psd.ai will still start - add a model under Settings > Models,")
        log("         or re-run this script once the network is back.")
        try:
            RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
            FAIL_FILE.write_text(f"{exc}\n", encoding="utf-8")
        except OSError:
            pass
        return 0

    if args.foreground:
        processes = result.get("procs") or [result.get("proc")]
        log(f"  Press Ctrl+C to stop the {len([p for p in processes if p is not None])}-model group.")
        try:
            while any(proc is not None and proc.poll() is None for proc in processes):
                time.sleep(1.0)
        except KeyboardInterrupt:
            pass
        finally:
            stop_servers(processes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
