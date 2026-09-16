#!/usr/bin/env python3
"""psd.ai — local Llama bootstrap.

One command that makes the app work offline out of the box:

  1. detect this PC's hardware (RAM / CPU / GPU)
  2. pick the best Llama that actually fits on it
  3. download llama.cpp (prebuilt llama-server) — first run only
  4. download the GGUF weights (resumable) — first run only
  5. start llama-server on 127.0.0.1:<port> and wait until it answers
  6. register it in the app database and make it the default chat model

run.bat calls this before launching the server, so a fresh Windows install
ends up with a working local model instead of an empty model picker.

Design notes
------------
* Selection is a pure function (``plan_model``) over the detected hardware and
  the file list Hugging Face reports, so it is unit-testable with no network.
* Downloads use only the stdlib (urllib + Range requests), so this runs on a
  bare venv with nothing but the app's own requirements installed.
* Every network/subprocess step is optional: if anything fails the caller is
  told, and run.bat still starts the app.
"""

from __future__ import annotations

import argparse
import atexit
import importlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

APP_NAME = "psd.ai"
DEFAULT_PORT = int(os.getenv("PSD_LLAMA_PORT", "8080") or "8080")
RUNTIME_DIR = BASE_DIR / "runtime"
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
# order; the first one that resolves wins. ``max_context`` is the model's
# trained context, not what we will actually run with.
@dataclass(frozen=True)
class ModelSpec:
    id: str
    label: str
    family: str
    params_b: float
    repos: Tuple[str, ...]
    max_context: int


MODEL_SPECS: Tuple[ModelSpec, ...] = (
    ModelSpec(
        id="llama-3.3-70b",
        label="Llama 3.3 70B Instruct",
        family="3.3",
        params_b=70.0,
        repos=("unsloth/Llama-3.3-70B-Instruct-GGUF",),
        max_context=131072,
    ),
    ModelSpec(
        id="llama-3.1-8b",
        label="Llama 3.1 8B Instruct",
        family="3.1",
        params_b=8.0,
        repos=("unsloth/Llama-3.1-8B-Instruct-GGUF", "bartowski/Meta-Llama-3.1-8B-Instruct-GGUF"),
        max_context=131072,
    ),
    ModelSpec(
        id="llama-3.2-3b",
        label="Llama 3.2 3B Instruct",
        family="3.2",
        params_b=3.0,
        repos=("bartowski/Llama-3.2-3B-Instruct-GGUF", "unsloth/Llama-3.2-3B-Instruct-GGUF"),
        max_context=131072,
    ),
    ModelSpec(
        id="llama-3.2-1b",
        label="Llama 3.2 1B Instruct",
        family="3.2",
        params_b=1.0,
        repos=("unsloth/Llama-3.2-1B-Instruct-GGUF", "bartowski/Llama-3.2-1B-Instruct-GGUF"),
        max_context=131072,
    ),
)

# Quantisation quality, best first. Used to refuse "best model, awful quant"
# picks: a 70B at IQ1 is worse in practice than an 8B at Q4_K_M.
QUANT_RANK: Tuple[str, ...] = (
    "F16", "BF16", "Q8_0", "Q6_K", "Q6_K_L", "Q5_K_M", "Q5_K_S", "Q5_K_L",
    "UD-Q5_K_XL", "Q4_K_M", "Q4_K_S", "Q4_K_L", "UD-Q4_K_XL", "IQ4_XS", "IQ4_NL",
    "Q4_0", "Q4_1", "Q3_K_M", "Q3_K_S", "Q3_K_L", "Q3_K_XL", "UD-Q3_K_XL",
    "Q2_K", "Q2_K_L", "UD-Q2_K_XL", "IQ3_M", "IQ3_XXS", "UD-IQ3_XXS",
    "IQ2_M", "IQ2_XXS", "UD-IQ2_M", "UD-IQ2_XXS", "IQ1_M", "IQ1_S", "UD-IQ1_M", "UD-IQ1_S",
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


def memory_budget_gb(system: Dict[str, Any]) -> float:
    """How much memory (GB) we may spend on weights + KV cache.

    GPU path: VRAM minus headroom for the runtime/KV cache. When the GPU is
    Apple Silicon (unified memory) or the VRAM figure is missing we fall back
    to the system-RAM budget instead, which is the same pool the GPU uses.
    """
    try:
        ram = float(system.get("available_ram_gb") or system.get("total_ram_gb") or 0.0)
    except (TypeError, ValueError):
        ram = 0.0
    unified = bool(system.get("unified_memory"))
    try:
        vram = float(system.get("gpu_vram_gb") or 0.0)
    except (TypeError, ValueError):
        vram = 0.0

    if system.get("has_gpu") and not unified and vram >= 4:
        return max(1.0, vram - 1.0)
    if unified and vram >= 4:
        return max(1.0, min(vram, ram * 0.65) - 1.0)
    # CPU-only: leave the OS + the app itself room to breathe.
    return max(0.6, ram * 0.6)


# ── File selection (pure) ────────────────────────────────────────────────────
@dataclass(frozen=True)
class GGUFFile:
    repo: str
    path: str
    size: int

    @property
    def filename(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    @property
    def size_gb(self) -> float:
        return self.size / GB


def quant_of(filename: str) -> Optional[str]:
    """Extract the quant tag from a GGUF filename ('...-Q4_K_M.gguf' -> 'Q4_K_M')."""
    stem = filename[:-5] if filename.lower().endswith(".gguf") else filename
    m = re.search(r"[-.]((?:UD-)?(?:IQ|Q)[0-9][A-Z0-9_]*|F16|BF16)$", stem, re.IGNORECASE)
    if not m:
        return None
    return m.group(1).upper()


def _is_split_part(filename: str) -> bool:
    """Split GGUF shards ('model-00001-of-00003.gguf') need every part."""
    return bool(re.search(r"\d{5}-of-\d{5}", filename))


def candidate_files(repo: str, files: Iterable[Dict[str, Any]]) -> List[GGUFFile]:
    """Single-file, known-quant GGUFs from a HF tree listing."""
    out: List[GGUFFile] = []
    for entry in files:
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("path") or "")
        if not path.lower().endswith(".gguf"):
            continue
        name = path.rsplit("/", 1)[-1]
        if _is_split_part(name):
            continue
        quant = quant_of(name)
        if quant is None or quant not in _QUANT_RANK_INDEX:
            continue
        try:
            size = int(entry.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        if size <= 0:
            continue
        out.append(GGUFFile(repo=repo, path=path, size=size))
    return out


# KV cache grows with layer count and KV-head count, not with parameter count,
# so a flat per-8k figure badly misjudged both the small and the 70B models.
# MB of f16 KV per 1k tokens, banded by model size.
KV_MB_PER_1K_TOKENS: Tuple[Tuple[float, float], ...] = (
    (3.0, 60.0), (8.0, 125.0), (14.0, 200.0), (34.0, 250.0), (10_000.0, 320.0),
)
# We request a q8_0 KV cache below, which is roughly half the f16 footprint.
KV_Q8_FACTOR = 0.55


def plan_context_gb(context: int, params_b: float = 8.0, quantized_kv: bool = True) -> float:
    """Estimated KV-cache footprint for a context window (weights are separate)."""
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


CONTEXT_LADDER: Tuple[int, ...] = (16384, 8192, 4096, 2048)
# A 2048-token window truncates most real conversations. If a model can only
# run that small, a smaller model at a proper context is the better answer.
MIN_USABLE_CONTEXT = 4096


def _context_for(spec: ModelSpec, weights_gb: float, budget_gb: float) -> int:
    """Largest context whose KV cache still leaves room for the weights."""
    chosen = CONTEXT_LADDER[-1]
    for ctx in CONTEXT_LADDER:
        if ctx > spec.max_context:
            continue
        if weights_gb + plan_context_gb(ctx, spec.params_b) <= budget_gb:
            return ctx
    return chosen


# Relaxation ladder for the quant floor. When a 16 GB box cannot hold any 70B
# quant we would rather drop to a low quant of the *next* model than hand back
# a 70B IQ1 that crawls.
QUANT_FLOORS: Tuple[str, ...] = ("Q3_K_M", "Q2_K", "IQ2_M", "IQ1_M")


def select_model_file(
    files: Sequence[GGUFFile], budget_gb: float, spec: ModelSpec, floor_quant: str
) -> Optional[Tuple[GGUFFile, str, int]]:
    """Largest single-file GGUF of ``spec`` that fits ``budget_gb``.

    Returns (file, quant, context) or None when nothing clears the quant floor
    *and* the memory budget together.
    """
    floor_rank = _QUANT_RANK_INDEX.get(floor_quant.upper(), MIN_QUANT_RANK)
    usable = [
        f for f in files
        if BEST_QUANT_RANK
        <= _QUANT_RANK_INDEX.get(quant_of(f.filename) or "", len(QUANT_RANK))
        <= floor_rank
    ]
    if not usable:
        return None
    # The largest context window that still fits wins; quant quality is only
    # the tie-breaker. On a RAM-bound laptop a smaller model serving 16k beats
    # a marginally sharper one stuck at 4k, and the smaller file also decodes
    # faster because token generation is memory-bandwidth bound.
    best: Optional[Tuple[Tuple[int, int], GGUFFile, str, int]] = None
    for f in usable:
        ctx = _context_for(spec, f.size_gb, budget_gb)
        if f.size_gb + plan_context_gb(ctx, spec.params_b) > budget_gb:
            continue
        key = (ctx, -_QUANT_RANK_INDEX.get(quant_of(f.filename) or "", len(QUANT_RANK)))
        if best is None or key > best[0]:
            best = (key, f, quant_of(f.filename) or "", ctx)
    if best is None:
        return None
    return best[1], best[2], best[3]


def plan_model(
    system: Dict[str, Any],
    files_by_repo: Dict[str, List[GGUFFile]],
    prefer_id: str = "",
    min_quant: str = "",
) -> Optional[Plan]:
    """Pick the best Llama (model + quant + context) for this hardware.

    Walks the catalogue strongest-first and, for each model, takes the largest
    single-file GGUF that fits the memory budget at a quant no worse than the
    current floor. If no model fits at Q3_K_M the floor is relaxed (Q2, IQ2,
    IQ1) and the walk repeats, so a small machine still lands on the biggest
    model it can actually run instead of a giant one it cannot.
    """
    budget = memory_budget_gb(system)
    specs = MODEL_SPECS
    if prefer_id:
        wanted = [s for s in specs if s.id == prefer_id.lower()]
        if wanted:
            specs = tuple(wanted)

    def pool_for(spec: ModelSpec) -> List[GGUFFile]:
        out: List[GGUFFile] = []
        for repo in spec.repos:
            out.extend(files_by_repo.get(repo, []))
        return out

    floors = [min_quant.upper()] if min_quant else list(QUANT_FLOORS)
    # Pass 1: only models that get a usable context window. Pass 2: allow the
    # 2048-token fallback rather than returning nothing at all.
    for min_context in (MIN_USABLE_CONTEXT, CONTEXT_LADDER[-1]):
        for floor in floors:
            for spec in specs:
                picked = select_model_file(pool_for(spec), budget, spec, floor)
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
            context=CONTEXT_LADDER[-1], budget_gb=round(budget, 2), fits=False,
            note="nothing fit the memory budget - using the smallest Llama available, expect slow replies",
        )
    return None


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
    """How many layers to offload to the GPU. -1 = all of them."""
    vram = gpu_vram_gb(system)
    if not has_usable_gpu(system):
        return 0
    need = plan.file.size_gb + plan_context_gb(plan.context, plan.spec.params_b)
    if need <= vram - 0.6:
        return -1
    # Partial offload: keep ~0.5 GB of VRAM for the KV cache/compute buffers.
    room = max(0.0, vram - 0.5)
    if room <= 0.2:
        return 0
    frac = min(0.95, room / max(0.1, plan.file.size_gb))
    return max(1, int(frac * 40))  # Llama dense blocks: 32 (8B) / 80 (70B)


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


def windows_backend_candidates(system: Dict[str, Any]) -> List[str]:
    """llama.cpp Windows asset kinds to try, best first.

    The CPU build is always last: it is tiny and works everywhere, so it is
    the guaranteed fallback when a GPU build refuses to start.
    """
    backend = str(system.get("backend") or "").lower()
    arch = "arm64" if str(system.get("cpu_arch") or "").lower() in ("arm64", "aarch64") else "x64"
    out: List[str] = []
    if has_usable_gpu(system):
        if backend == "cuda" or "nvidia" in str(system.get("gpu_name") or "").lower():
            out.append("cuda")
        if backend == "rocm":
            out.append("rocm")
        if arch == "x64":
            out.append("vulkan")
    out.append("cpu")
    seen: List[str] = []
    for item in out:
        if item not in seen:
            seen.append(item)
    return seen


def match_release_asset(assets: Iterable[Dict[str, Any]], tag: str, kind: str, arch: str) -> Optional[Dict[str, Any]]:
    """Find the llama.cpp release asset for (tag, backend kind, arch)."""
    if kind == "cpu":
        pattern = rf"^llama-{re.escape(tag)}-bin-win-cpu-{arch}\.zip$"
    elif kind == "vulkan":
        pattern = rf"^llama-{re.escape(tag)}-bin-win-vulkan-{arch}\.zip$"
    elif kind == "cuda":
        pattern = rf"^llama-{re.escape(tag)}-bin-win-cuda-[\d.]+-{arch}\.zip$"
    elif kind == "rocm":
        pattern = rf"^llama-{re.escape(tag)}-bin-win-rocm-[\d.]+-{arch}\.zip$"
    else:
        return None
    rx = re.compile(pattern)
    best: Optional[Dict[str, Any]] = None
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "")
        if not rx.match(name):
            continue
        if asset.get("browser_download_url") and (best is None or name < str(best.get("name"))):
            best = asset
    return best


def pick_llama_release(releases: Iterable[Dict[str, Any]], arch: str = "x64") -> Optional[Dict[str, Any]]:
    """Newest llama.cpp release that actually carries Windows binaries.

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
            isinstance(a, dict) and re.match(r"^llama-b\d+-bin-win-(cpu|vulkan|cuda|rocm)-", str(a.get("name") or ""))
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


def detect_llama_backends(exe: Path) -> List[str]:
    """Which ggml backends a llama-server binary was built with.

    Windows binaries load their backend as a sibling DLL (ggml-vulkan.dll,
    ggml-cuda.dll), and the DLL name sits in the exe's import table — so a
    cheap byte scan tells us whether -ngl is worth passing at all.
    """
    found: List[str] = []
    try:
        blob = exe.read_bytes()[: 32 * 1024 * 1024]
    except OSError:
        return ["cpu"]
    lower = blob.lower()
    if b"ggml-vulkan" in lower:
        found.append("vulkan")
    if b"ggml-cuda" in lower or b"ggml-hip" in lower:
        found.append("cuda")
    if b"ggml-metal" in lower:
        found.append("metal")
    found.append("cpu")
    return found


def build_server_command(
    exe: Path, model_path: Path, alias: str, port: int,
    context: int, gpu_layers: int, threads: int, host: str = "127.0.0.1",
    parallel: int = 1, quantized_kv: bool = True,
) -> List[str]:
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
    # 16 GB laptop that is the difference between a 4k and a 16k window - at a
    # quality cost too small to measure.
    if quantized_kv:
        cmd += ["--flash-attn", "on", "--cache-type-k", "q8_0", "--cache-type-v", "q8_0"]
    cmd += ["--no-webui"]
    return cmd


def plan_parallel(plan: Plan, system: Dict[str, Any]) -> int:
    """One person talks to this server, so do not split the context window
    across slots: a 4-way split turns 16k into four 4k windows AND multiplies
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


def download_file(url: str, dest: Path, expected_size: int = 0, label: str = "") -> Path:
    """Resumable download with a coarse progress line."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
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
    if total and have < total:
        raise IOError(f"download of {dest.name} stopped at {have} of {total} bytes")
    tmp.replace(dest)
    log(f"      {label or dest.name}: done ({have / GB:.2f} GB)")
    return dest


# ── llama.cpp install ────────────────────────────────────────────────────────
def find_llama_server() -> Optional[Path]:
    if not LLAMA_DIR.exists():
        return None
    hits = sorted(LLAMA_DIR.rglob("llama-server.exe" if os.name == "nt" else "llama-server"))
    return hits[0] if hits else None


def install_llama_cpp(system: Dict[str, Any], prefer: Sequence[str] = ()) -> Tuple[Path, List[str]]:
    """Download + unpack a prebuilt llama.cpp, return (exe, backends)."""
    existing = find_llama_server()
    if existing is not None:
        return existing, detect_llama_backends(existing)

    if os.name != "nt":
        on_path = shutil.which("llama-server")
        if on_path:
            return Path(on_path), detect_llama_backends(Path(on_path))
        raise RuntimeError(
            "llama-server not found. Install llama.cpp (e.g. `brew install llama.cpp`) "
            "or run this script on Windows, where run.bat fetches a prebuilt binary."
        )

    arch = "arm64" if str(system.get("cpu_arch") or "").lower() in ("arm64", "aarch64") else "x64"
    releases = _http_get_json(f"{GH_API}/repos/ggml-org/llama.cpp/releases?per_page=15", timeout=45.0)
    picked = pick_llama_release(releases if isinstance(releases, list) else [], arch=arch)
    if not picked:
        raise RuntimeError("could not find a llama.cpp release with Windows binaries")
    tag = str(picked["tag"])

    kinds = list(prefer) or windows_backend_candidates(system)
    errors: List[str] = []
    for kind in kinds:
        asset = match_release_asset(picked["assets"], tag, kind, arch)
        if not asset:
            continue
        url = str(asset.get("browser_download_url") or "")
        if not url:
            continue
        log(f"  ==> Downloading llama.cpp {tag} ({kind}, {arch})...")
        zip_path = RUNTIME_DIR / f"llama-{tag}-{kind}.zip"
        try:
            download_file(url, zip_path, int(asset.get("size") or 0), label=f"llama.cpp {kind}")
            LLAMA_DIR.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(LLAMA_DIR)
            zip_path.unlink(missing_ok=True)
            exe = find_llama_server()
            if exe is None:
                raise RuntimeError("llama-server.exe missing from the downloaded archive")
            backends = detect_llama_backends(exe)
            log(f"  ==> llama.cpp {tag} ready at {exe} (backends: {', '.join(backends)})")
            return exe, backends
        except Exception as exc:  # try the next backend build
            errors.append(f"{kind}: {exc}")
            log(f"  [warn] llama.cpp {kind} build failed ({exc}) — trying the next one.")
    raise RuntimeError("could not install llama.cpp: " + "; ".join(errors))


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
    if os.name == "nt":
        # Own process group: Ctrl+C in the launcher window must not kill the
        # model server before we get a chance to shut it down cleanly.
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
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
        row.name = ENDPOINT_NAME
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
) -> Dict[str, Any]:
    """Download + start the best local Llama. Returns a status dict."""
    system = system if system is not None else detect_hardware()
    log("")
    log(f"  Hardware: {system.get('cpu_name') or 'unknown CPU'}, "
        f"{system.get('total_ram_gb') or '?'} GB RAM, "
        f"{system.get('gpu_name') or 'no GPU'}")
    budget = memory_budget_gb(system)
    log(f"  Memory budget for the model: {budget:.1f} GB")

    if skip_download:
        files_by_repo = _files_from_disk()
    else:
        files_by_repo = {}
        for spec in MODEL_SPECS:
            for repo in spec.repos:
                if repo in files_by_repo:
                    continue
                try:
                    files_by_repo[repo] = candidate_files(repo, fetch_files(repo))
                except Exception as exc:
                    log(f"  [warn] could not list {repo}: {exc}")
                    files_by_repo[repo] = []

    plan = plan_model(system, files_by_repo, prefer_id=prefer_id)
    if plan is None:
        raise RuntimeError("no downloadable Llama GGUF found — check the network connection")

    log(f"  Chosen model : {plan.spec.label} ({plan.quant}, {plan.file.size_gb:.2f} GB)")
    log(f"  Source repo  : {plan.file.repo}")
    if plan.note:
        log(f"  Note         : {plan.note}")

    exe, backends = installer(system)
    gpu_layers = plan_gpu_layers(system, plan)
    if gpu_layers and not any(b in backends for b in ("vulkan", "cuda", "metal")):
        log("  [info] this llama-server build is CPU-only — running on the CPU.")
        gpu_layers = 0

    model_path = MODELS_DIR / plan.file.repo.replace("/", "__") / plan.file.filename
    if not skip_download and not (model_path.exists() and model_path.stat().st_size == plan.file.size):
        log(f"  ==> Downloading {plan.file.filename} ({plan.file.size_gb:.2f} GB) — this happens once.")
        url = f"{HF_ORIGIN}/{plan.file.repo}/resolve/main/{plan.file.path}"
        downloader(url, model_path, plan.file.size, label=plan.file.filename)
    elif not model_path.exists():
        raise RuntimeError(f"model file missing and downloads are disabled: {model_path}")

    alias = f"psd-{plan.spec.id}"
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
            "pid": None, "reused": True,
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
            f"port {port} is already in use by another program. "
            f"Close it, or set LLAMA_PORT to a free port before running run.bat."
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
            )
            log(f"  ==> Starting llama-server ({mode}, ctx {plan.context}, ngl {layers}) on port {port}...")
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
        "pid": proc.pid if proc else None,
    }
    write_state(state)
    log(f"  ==> Local model ready: {model_id} at {base_url}")
    if became_default:
        log("  ==> Set as the default chat model for psd.ai")
    return {"ok": True, "proc": proc, **state}


def _files_from_disk() -> Dict[str, List[GGUFFile]]:
    """Build a file map from what is already on disk (offline re-runs)."""
    out: Dict[str, List[GGUFFile]] = {}
    if not MODELS_DIR.exists():
        return out
    for repo_dir in MODELS_DIR.iterdir():
        if not repo_dir.is_dir():
            continue
        repo = repo_dir.name.replace("__", "/")
        entries = [
            {"path": p.name, "size": p.stat().st_size}
            for p in repo_dir.glob("*.gguf")
        ]
        out[repo] = candidate_files(repo, entries)
    return out


def wait_for_ready(timeout: float, state_file: Path = STATE_FILE, fail_file: Path = FAIL_FILE,
                   poll: float = 2.0) -> int:
    """Block until a sibling bootstrap finishes (or gives up).

    run.bat uses this so the browser only opens once the local model is
    registered and the app can select it. Returns 0 when ready, 3 otherwise —
    either way the caller should still start the app.
    """
    deadline = time.time() + timeout
    next_beat = time.time() + 20
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
            log("      still waiting for the local model (first run downloads a few GB)...")
            next_beat = time.time() + 20
        time.sleep(poll)
    log("  [warn] Timed out waiting for the local model - starting psd.ai anyway.")
    return 3


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Download + run the best local Llama for this PC.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--model", default=os.getenv("PSD_LOCAL_MODEL_ID", ""),
                        help="force a model id (llama-3.3-70b, llama-3.1-8b, llama-3.2-3b, llama-3.2-1b)")
    parser.add_argument("--min-quant", default="Q3_K_M")
    parser.add_argument("--threads", type=int, default=0)
    parser.add_argument("--skip-download", action="store_true",
                        help="use only models already downloaded under runtime/models")
    parser.add_argument("--no-default", action="store_true", help="do not change the app's default model")
    parser.add_argument("--print", dest="print_only", action="store_true",
                        help="print the model this PC would get and exit (no downloads)")
    parser.add_argument("--foreground", action="store_true",
                        help="keep this process attached to llama-server (Ctrl+C stops it)")
    parser.add_argument("--wait-ready", type=int, default=0, metavar="SECONDS",
                        help="block until another instance finishes setup, then exit "
                             "(0 = ready, 3 = timed out or failed)")
    args = parser.parse_args(argv)

    if args.wait_ready:
        return wait_for_ready(args.wait_ready)

    log("")
    log("  ============================================================")
    log(f"    {APP_NAME} - local Llama setup")
    log("  ============================================================")

    FAIL_FILE.unlink(missing_ok=True)

    try:
        if args.print_only:
            system = detect_hardware()
            files_by_repo: Dict[str, List[GGUFFile]] = {}
            for spec in MODEL_SPECS:
                for repo in spec.repos:
                    if repo in files_by_repo:
                        continue
                    try:
                        files_by_repo[repo] = candidate_files(repo, list_repo_files(repo))
                    except Exception as exc:
                        log(f"  [warn] {repo}: {exc}")
            plan = plan_model(system, files_by_repo, prefer_id=args.model, min_quant=args.min_quant)
            if plan is None:
                log("  No Llama GGUF reachable.")
                return 1
            log(json.dumps({
                "model": plan.spec.label, "spec_id": plan.spec.id, "quant": plan.quant,
                "size_gb": round(plan.file.size_gb, 2), "repo": plan.file.repo,
                "file": plan.file.filename, "context": plan.context,
                "budget_gb": plan.budget_gb, "gpu_layers": plan_gpu_layers(system, plan),
                "fits": plan.fits,
            }, indent=2))
            return 0

        result = prepare_local_llama(
            port=args.port,
            prefer_id=args.model,
            host=args.host,
            skip_download=args.skip_download,
            threads=args.threads,
            set_default=not args.no_default,
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
        proc = result.get("proc")
        log("  Press Ctrl+C to stop the local model.")
        try:
            while proc is not None and proc.poll() is None:
                time.sleep(1.0)
        except KeyboardInterrupt:
            pass
        finally:
            stop_server(proc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
