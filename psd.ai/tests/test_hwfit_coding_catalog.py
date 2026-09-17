"""Offline coding-model catalog: coverage, license metadata, and the
access filter.

The Cookbook's Scan/Download list ranks local (fully offline) models. This
pins the coding-focused additions:

- Curated coding models ship in BOTH license classes — unrestricted
  (permissive Apache/MIT) and restricted (gated/custom licenses) — so the
  License filter always has real rows to show.
- model_access() prefers explicit catalog fields, falls back to a
  permissive-license table, then to a family table, and returns "" for
  unknowns (never guesses a license).
- rank_models' access filter only returns rows of the requested class and
  rows carry access/license through to the API payload.
- infer_use_case honours an explicit "coding" capability.
"""
from services.hwfit.fit import analyze_model, rank_models
from services.hwfit.models import get_models, infer_use_case, model_access


def _24gb_vram_system():
    return {
        "has_gpu": True,
        "backend": "cuda",
        "gpu_name": "NVIDIA GeForce RTX 4090",
        "gpu_vram_gb": 24.0,
        "gpu_count": 1,
        "available_ram_gb": 64.0,
        "total_ram_gb": 64.0,
    }


def _catalog():
    return {m["name"]: m for m in get_models()}


# ── catalog coverage ───────────────────────────────────────────────────

def test_unrestricted_coding_models_in_catalog():
    cat = _catalog()
    for name in (
        "Qwen/Qwen2.5-Coder-14B-Instruct",
        "Qwen/Qwen2.5-Coder-32B-Instruct",
        "Qwen/Qwen3-Coder-30B-A3B-Instruct",
        "mistralai/Devstral-Small-2507",
        "ibm-granite/granite-4.0-h-tiny",
        "microsoft/Phi-3.5-mini-instruct",
    ):
        entry = cat.get(name)
        assert entry, f"{name} missing from catalog"
        assert entry.get("access") == "unrestricted", name
        assert entry.get("license"), f"{name} has no license metadata"
        assert infer_use_case(entry) == "coding", name


def test_restricted_coding_models_in_catalog():
    cat = _catalog()
    for name, license_label in (
        ("codellama/CodeLlama-13b-Instruct-hf", "llama-2-community"),
        ("codellama/CodeLlama-34b-Instruct-hf", "llama-2-community"),
        ("mistralai/Codestral-22B-v0.1", "mnpl"),
        ("google/codegemma-7b-it", "gemma"),
        ("deepseek-ai/deepseek-coder-33b-instruct", "deepseek-license"),
        ("bigcode/starcoder2-15b", "bigcode-openrail-m"),
    ):
        entry = cat.get(name)
        assert entry, f"{name} missing from catalog"
        assert entry.get("access") == "restricted", name
        assert entry.get("license") == license_label, name
        assert infer_use_case(entry) == "coding", name


def test_curated_coding_models_have_offline_sources():
    # Pure-offline workflow: GGUF sources mean the Cookbook can download and
    # serve via llama.cpp/Ollama without any safetensors-only serving path.
    cat = _catalog()
    for name in (
        "Qwen/Qwen2.5-Coder-14B-Instruct",
        "Qwen/Qwen2.5-Coder-32B-Instruct",
        "Qwen/Qwen3-Coder-30B-A3B-Instruct",
        "mistralai/Devstral-Small-2507",
    ):
        assert cat[name].get("gguf_sources"), f"{name} has no GGUF sources"


# ── model_access classification ────────────────────────────────────────

def test_explicit_fields_win():
    m = {"name": "unknown-org/mystery-model", "license": "MIT", "access": "restricted"}
    assert model_access(m) == {"access": "restricted", "license": "MIT"}


def test_license_field_maps_permissive_vs_restricted():
    assert model_access({"name": "x/y", "license": "apache-2.0"})["access"] == "unrestricted"
    assert model_access({"name": "x/y", "license": "mit"})["access"] == "unrestricted"
    assert model_access({"name": "x/y", "license": "gemma"})["access"] == "restricted"


def test_family_fallback_classifies_known_coding_families():
    cases = {
        "Qwen/Qwen2.5-Coder-99B-Instruct": "unrestricted",
        "Qwen/Qwen3-Coder-480B-A35B-Instruct": "unrestricted",
        "unsloth/Devstral-Small-2507-GGUF": "unrestricted",
        "microsoft/phi-4-mini-instruct": "unrestricted",
        "codellama/CodeLlama-70b-Instruct-hf": "restricted",
        "mistralai/Codestral-22B-v0.1": "restricted",
        "google/codegemma-2b": "restricted",
        "bigcode/starcoder2-3b": "restricted",
        "deepseek-ai/deepseek-coder-1.3b-base": "restricted",
    }
    for name, expected in cases.items():
        assert model_access({"name": name})["access"] == expected, name


def test_unknown_models_get_no_access_label():
    # Never guess: unknown models render no badge in the UI.
    assert model_access({"name": "some-org/totally-new-model"})["access"] == ""
    assert model_access({})["access"] == ""


def test_explicit_coding_capability_drives_use_case():
    m = {"name": "microsoft/Phi-3.5-mini-instruct", "use_case": "Lightweight, long context",
         "capabilities": ["coding"]}
    assert infer_use_case(m) == "coding"


# ── rank_models access filter ──────────────────────────────────────────

def test_rank_models_access_filter_partitions_coding_rows():
    system = _24gb_vram_system()
    unrestricted = rank_models(system, use_case="coding", access="unrestricted", limit=100)
    restricted = rank_models(system, use_case="coding", access="restricted", limit=100)
    assert unrestricted, "no unrestricted coding models ranked"
    assert restricted, "no restricted coding models ranked"
    assert all(r["access"] == "unrestricted" for r in unrestricted)
    assert all(r["access"] == "restricted" for r in restricted)
    # The two classes never overlap.
    assert not ({r["name"] for r in unrestricted} & {r["name"] for r in restricted})


def test_rank_models_invalid_access_ignored():
    system = _24gb_vram_system()
    everything = rank_models(system, use_case="coding", access="nonsense-value", limit=100)
    baseline = rank_models(system, use_case="coding", limit=100)
    assert [r["name"] for r in everything] == [r["name"] for r in baseline]


def test_ranked_rows_carry_license_fields():
    system = _24gb_vram_system()
    rows = rank_models(system, use_case="coding", limit=100)
    assert all("access" in r and "license" in r for r in rows)
    names = {r["name"] for r in rows}
    if "Qwen/Qwen2.5-Coder-14B-Instruct" in names:
        row = next(r for r in rows if r["name"] == "Qwen/Qwen2.5-Coder-14B-Instruct")
        assert row["access"] == "unrestricted"
        assert row["license"] == "apache-2.0"


def test_analyze_model_exposes_access_on_tight_fit():
    # The too-tight result shape must carry access too, so oversized rows
    # still show their license badge.
    cat = _catalog()
    entry = cat["Qwen/Qwen2.5-Coder-32B-Instruct"]
    tiny = {
        "has_gpu": True, "backend": "cuda", "gpu_name": "GTX 1050",
        "gpu_vram_gb": 2.0, "gpu_count": 1,
        "available_ram_gb": 4.0, "total_ram_gb": 4.0,
    }
    result = analyze_model(entry, tiny)
    assert result is not None
    assert result["access"] == "unrestricted"
    assert result["license"] == "apache-2.0"
