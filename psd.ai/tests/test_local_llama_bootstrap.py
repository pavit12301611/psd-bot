"""scripts/local_llama.py — model selection, launch flags and setup handshake.

run.sh calls this to download and serve the strongest local models this machine
can hold, so the picker is not empty on a fresh Fedora install. The selection
rules are what make "strongest" mean "strongest that actually runs here": a
122B at IQ1 on a laptop is worse than a 35B MoE at Q4, and a model that only
fits at a 2048 context is worse than a smaller one at 32768.
"""

import json
import pathlib

import pytest

from scripts import local_llama as ll


def _files(repo, quants):
    """Build a GGUF file list shaped like the Hugging Face tree API response."""
    name = repo.rsplit("/", 1)[-1].removesuffix("-GGUF")
    return ll.candidate_files(
        repo,
        [{"type": "file", "path": f"{name}-{q}.gguf", "size": size} for q, size in quants.items()],
    )


GB = 1024 ** 3

# Sizes mirror the real unsloth / bartowski / official GGUF repositories.
Q_QWEN35_9B = {
    "BF16": int(18.0 * GB), "Q8_0": int(9.6 * GB), "Q6_K": int(7.6 * GB),
    "Q5_K_M": int(6.6 * GB), "Q4_K_M": int(5.6 * GB), "Q3_K_M": int(4.6 * GB),
}
Q_QWEN35_4B = {
    "Q8_0": int(4.3 * GB), "Q5_K_M": int(2.9 * GB), "Q4_K_M": int(2.5 * GB),
}
Q_QWEN36_35B_A3B = {  # MoE: 35B total, 3B active
    "Q8_0": int(37.0 * GB), "Q6_K": int(28.0 * GB), "Q5_K_M": int(24.7 * GB),
    "Q4_K_M": int(21.2 * GB), "IQ2_M": int(12.0 * GB),
}
Q_GEMMA4_E4B = {"Q8_0": int(8.19 * GB), "Q5_K_M": int(5.48 * GB), "Q4_K_M": int(4.98 * GB)}
Q_GEMMA4_12B = {"Q8_0": int(13.0 * GB), "Q6_K": int(10.2 * GB), "Q5_K_M": int(8.8 * GB),
                "Q4_K_M": int(7.5 * GB)}
Q_GEMMA4_26B_A4B = {"Q8_0": int(26.9 * GB), "Q5_K_M": int(21.2 * GB), "Q4_K_M": int(16.9 * GB)}
Q_GPT_OSS_20B = {"MXFP4": int(12.0 * GB), "Q8_0": int(22.0 * GB)}
Q_QWEN3_17B = {"Q8_0": int(1.9 * GB), "Q4_K_M": int(1.1 * GB)}
Q_QWEN3_06B = {"Q8_0": int(0.7 * GB), "Q4_K_M": int(0.45 * GB)}
Q_SMOLLM3_3B = {"Q8_0": int(3.3 * GB), "Q4_K_M": int(1.8 * GB)}
Q_DEVSTRAL_24B = {"Q8_0": int(25.1 * GB), "Q6_K": int(19.4 * GB), "Q4_K_M": int(14.0 * GB)}
Q_QWEN35_122B_A10B = {"Q2_K": int(45.0 * GB), "Q3_K_M": int(56.0 * GB)}

REPOS = {
    "unsloth/Qwen3.5-9B-GGUF": Q_QWEN35_9B,
    "unsloth/Qwen3.5-4B-GGUF": Q_QWEN35_4B,
    "unsloth/Qwen3.6-35B-A3B-GGUF": Q_QWEN36_35B_A3B,
    "unsloth/gemma-4-E4B-it-GGUF": Q_GEMMA4_E4B,
    "unsloth/gemma-4-12B-it-GGUF": Q_GEMMA4_12B,
    "unsloth/gemma-4-26B-A4B-it-GGUF": Q_GEMMA4_26B_A4B,
    "unsloth/gpt-oss-20b-GGUF": Q_GPT_OSS_20B,
    "unsloth/Qwen3-1.7B-GGUF": Q_QWEN3_17B,
    "unsloth/Qwen3-0.6B-GGUF": Q_QWEN3_06B,
    "HuggingFaceTB/SmolLM3-3B-GGUF": Q_SMOLLM3_3B,
    "unsloth/Devstral-Small-2-24B-Instruct-GGUF": Q_DEVSTRAL_24B,
    "unsloth/Qwen3.5-122B-A10B-GGUF": Q_QWEN35_122B_A10B,
}
FILES = {repo: _files(repo, quants) for repo, quants in REPOS.items()}


def _system(ram=32.0, avail=None, gpu=None, vram=0.0, backend="cpu_x86", unified=False,
            arch="x86_64", cores=16, phys=8):
    return {
        "has_gpu": gpu is not None, "gpu_name": gpu, "gpu_vram_gb": vram,
        "gpu_count": 1 if gpu else 0, "backend": backend, "unified_memory": unified,
        "available_ram_gb": avail if avail is not None else ram * 0.75,
        "total_ram_gb": ram, "cpu_cores": cores, "cpu_physical_cores": phys,
        "cpu_arch": arch,
    }


def _spec(spec_id):
    return next(s for s in ll.MODEL_SPECS if s.id == spec_id)


def _file_of(spec_id, quant_suffix):
    spec = _spec(spec_id)
    for repo in spec.repos:
        for candidate in FILES.get(repo, []):
            if candidate.filename.endswith(quant_suffix):
                return candidate
    raise AssertionError(f"no {quant_suffix} file for {spec_id} in the fake listing")


# ── shared fakes for prepare_local_llama tests ───────────────────────────────
def _setup_paths(monkeypatch, tmp_path):
    for name, sub in [
        ("RUNTIME_DIR", "runtime"), ("MODELS_DIR", "runtime/models"),
        ("LLAMA_DIR", "runtime/llama.cpp"),
    ]:
        monkeypatch.setattr(ll, name, tmp_path / sub)
    monkeypatch.setattr(ll, "STATE_FILE", tmp_path / "runtime" / "local_model.json")
    monkeypatch.setattr(ll, "FAIL_FILE", tmp_path / "runtime" / "local_model_failed.txt")
    monkeypatch.setattr(ll, "GROUP_STATE_FILE", tmp_path / "runtime" / "local_model_group.json")
    monkeypatch.setattr(ll, "LOG_FILE", tmp_path / "runtime" / "llama-server.log")


def _fetch_one_repo(repo, revision="main"):
    if repo == "unsloth/Qwen3.5-9B-GGUF":
        return [{"type": "file", "path": "Qwen3.5-9B-Q4_K_M.gguf", "size": int(5.6 * GB)}]
    raise KeyError(repo)


def _fake_installer(system, prefer=()):
    return pathlib.Path("/usr/bin/true"), ["cpu"]


def _fake_downloader(url, dest, expected_size=0, label=""):
    """Stand in for the multi-GB weight download (covered separately)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"GGUF" + b"\x00" * 4092)
    return dest


# ── platform ─────────────────────────────────────────────────────────────────
def test_non_linux_is_refused_with_instructions(monkeypatch):
    """psd.ai is Linux-only: no silent half-working install elsewhere."""
    monkeypatch.setattr(ll, "IS_LINUX", False)
    with pytest.raises(SystemExit) as excinfo:
        ll._require_linux()
    assert "Linux only" in str(excinfo.value)
    monkeypatch.setattr(ll, "IS_LINUX", True)
    assert ll._require_linux() is None


def test_runtime_dir_follows_xdg(monkeypatch, tmp_path):
    monkeypatch.delenv("PSD_AI_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert ll._default_runtime_dir() == tmp_path / "data" / "psd.ai" / "runtime"
    monkeypatch.setenv("PSD_AI_RUNTIME_DIR", str(tmp_path / "elsewhere"))
    assert ll._default_runtime_dir() == tmp_path / "elsewhere"


# ── selection ────────────────────────────────────────────────────────────────
def test_8gb_laptop_gets_a_modern_small_model():
    plan = ll.plan_model(_system(ram=8, avail=5.5), FILES)
    assert plan.spec.id in {"qwen3.5-4b", "gemma-4-e4b", "smollm3-3b"}
    assert plan.fits is True
    # Never a 2024-era fallback when a current model fits.
    assert plan.spec.params_b >= 3.0


def test_16gb_laptop_gets_9b_at_a_real_context_window():
    hw = _system(ram=16, avail=12)
    plan = ll.plan_model(hw, FILES)
    assert plan.spec.id == "qwen3.5-9b"
    # Q6_K is the sharper file but it cannot hold a real window inside the
    # balanced budget; context wins and quant quality breaks the tie.
    assert plan.quant == "Q4_K_M"
    assert plan.context == 8192
    assert plan.file.size_gb + ll.plan_context_gb(plan.context, plan.spec.kv_params_b) \
        <= ll.memory_budget_gb(hw)
    # Q3 would buy a 32k window, and the planner refuses that trade: below
    # Q4_K_M models start dropping instructions and tool schemas.
    assert ll.QUANT_FLOORS[0] == "Q4_K_M"
    # A more aggressive profile spends more RAM, and never picks a weaker
    # model or a smaller window than balanced did.
    power = ll.plan_model(hw, FILES, profile=ll.POWER_PROFILES["power"])
    assert power.budget_gb > plan.budget_gb
    assert power.spec.params_b >= plan.spec.params_b
    assert power.context >= plan.context


def test_24gb_gpu_gets_a_35b_moe_not_a_12b_dense():
    """The point of the 2026 catalogue: MoE makes big models laptop-sized."""
    hw = _system(ram=64, avail=50, gpu="RTX 4090", vram=24.0, backend="cuda")
    plan = ll.plan_model(hw, FILES)
    assert plan.spec.id == "qwen3.6-35b-a3b"
    assert plan.spec.moe is True
    assert ll.plan_gpu_layers(hw, plan) == -1


def test_96gb_workstation_gets_the_122b_moe():
    hw = _system(ram=128, avail=110, gpu="RTX 5090", vram=32.0, backend="cuda")
    plan = ll.plan_model(hw, FILES)
    assert plan.spec.id == "qwen3.5-122b-a10b"
    assert plan.spec.moe is True


def test_f16_is_never_picked_over_q8_0():
    """BF16 doubles the download and halves the speed for no usable gain."""
    plan = ll.plan_model(
        _system(ram=32, avail=26, gpu="RTX 4070", vram=12.0, backend="cuda"), FILES
    )
    assert plan.quant != "BF16"
    assert ll._QUANT_RANK_INDEX[plan.quant] >= ll.BEST_QUANT_RANK


def test_model_that_only_fits_at_a_tiny_context_is_rejected():
    """A 12 GB card cannot hold the 26B MoE at a usable window, so 12B wins."""
    hw = _system(ram=32, avail=24, gpu="RTX 4070", vram=12.0, backend="cuda")
    plan = ll.plan_model(hw, FILES)
    assert plan.spec.id != "gemma-4-26b-a4b"
    assert plan.context >= ll.MIN_USABLE_CONTEXT


def test_unified_memory_gpu_uses_the_shared_pool():
    """NVIDIA GB10 / AMD Strix Halo report VRAM that is really system RAM."""
    hw = _system(ram=128, avail=100, gpu="NVIDIA GB10", vram=120.0,
                 backend="cuda", unified=True, arch="aarch64")
    budget = ll.memory_budget_gb(hw)
    assert budget < 100.0  # capped by the RAM share, not the raw VRAM figure
    plan = ll.plan_model(hw, FILES)
    assert plan is not None and plan.fits is True


def test_tiny_machine_still_gets_a_local_model():
    plan = ll.plan_model(_system(ram=4, avail=2.2), FILES)
    assert plan.spec.id in {"qwen3-0.6b", "qwen3-1.7b"}
    assert plan.fits is True


def test_nothing_available_returns_none():
    assert ll.plan_model(_system(), {}) is None


def test_prefer_id_overrides_the_ranking():
    plan = ll.plan_model(_system(ram=16, avail=12), FILES, prefer_id="qwen3-1.7b")
    assert plan.spec.id == "qwen3-1.7b"


def test_catalogue_is_ordered_by_quality_not_by_raw_size():
    """A 35B MoE outranks a 33B dense model; a 1.7B never outranks a 9B."""
    order = [spec.id for spec in ll.catalogue_by_strength()]
    assert order.index("qwen3.5-122b-a10b") < order.index("qwen3.6-35b-a3b")
    assert order.index("qwen3.6-35b-a3b") < order.index("qwen3.5-9b")
    assert order.index("qwen3.5-9b") < order.index("qwen3-1.7b")


def test_every_catalogue_entry_has_a_mirror_and_a_real_context():
    """A single-repo entry is one rename away from being undownloadable."""
    for spec in ll.MODEL_SPECS:
        assert len(spec.repos) >= 2, spec.id
        assert spec.max_context >= 8192, spec.id
        assert spec.params_b > 0, spec.id
        assert 0 <= spec.active_params_b <= spec.params_b, spec.id
        assert ll.spec_quality(spec) > 0, spec.id


# ── power profiles ───────────────────────────────────────────────────────────
def test_power_profile_spends_more_of_the_machine():
    hw = _system(ram=32, avail=28)
    balanced = ll.group_memory_budget_gb(hw, ll.active_profile("balanced"))
    power = ll.group_memory_budget_gb(hw, ll.active_profile("power"))
    assert power > balanced


def test_max_profile_runs_one_model_instead_of_a_group():
    assert ll.active_profile("max").group is False
    assert ll.active_profile("balanced").group is True
    assert ll.active_profile("nonsense").name == "balanced"


def test_profile_environment_variable_is_honoured(monkeypatch):
    monkeypatch.setenv("PSD_MODEL_PROFILE", "power")
    monkeypatch.setattr(ll, "DEFAULT_PROFILE", "power")
    assert ll.active_profile().name == "power"
    assert ll.active_profile("balanced").name == "balanced"


def test_higher_profile_reaches_a_longer_context_or_a_bigger_model():
    hw = _system(ram=64, avail=56, gpu="RTX 4090", vram=24.0, backend="cuda")
    balanced = ll.plan_model(hw, FILES, profile=ll.active_profile("balanced"))
    power = ll.plan_model(hw, FILES, profile=ll.active_profile("power"))
    assert (ll.spec_quality(power.spec), power.context) >= \
           (ll.spec_quality(balanced.spec), balanced.context)


# ── quant parsing ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name,expected", [
    ("Qwen3.5-9B-Q4_K_M.gguf", "Q4_K_M"),
    ("Qwen3.5-9B-Q8_0.gguf", "Q8_0"),
    ("Qwen3.5-9B-UD-Q4_K_XL.gguf", "UD-Q4_K_XL"),
    ("Qwen3.5-9B-AD-IQ4_XS.gguf", "AD-IQ4_XS"),
    ("Qwen3.5-9B-IQ4_XS.gguf", "IQ4_XS"),
    ("Qwen3.5-9B-BF16.gguf", "BF16"),
    ("gpt-oss-20b-mxfp4.gguf", "MXFP4"),
    ("Qwen3.5-9B-Q8_0-00002-of-00004.gguf", "Q8_0"),
    ("Qwen3.5-9B.imatrix", None),
    ("README.md", None),
])
def test_quant_of(name, expected):
    assert ll.quant_of(name) == expected


# ── split releases ───────────────────────────────────────────────────────────
def test_complete_shard_set_becomes_one_candidate():
    """Multi-part GGUFs are what make the 100B+ models downloadable at all."""
    files = ll.candidate_files("r/x-GGUF", [
        {"type": "file", "path": "x-Q8_0-00001-of-00003.gguf", "size": 1000},
        {"type": "file", "path": "x-Q8_0-00002-of-00003.gguf", "size": 2000},
        {"type": "file", "path": "x-Q8_0-00003-of-00003.gguf", "size": 500},
        {"type": "file", "path": "x-Q4_K_M.gguf", "size": 900},
    ])
    by_name = {f.filename: f for f in files}
    assert set(by_name) == {"x-Q8_0-00001-of-00003.gguf", "x-Q4_K_M.gguf"}
    split = by_name["x-Q8_0-00001-of-00003.gguf"]
    assert split.is_split is True
    assert split.size == 3500
    assert split.all_paths[0] == "x-Q8_0-00001-of-00003.gguf"
    assert by_name["x-Q4_K_M.gguf"].is_split is False


def test_incomplete_shard_set_is_never_offered():
    """Half a model would download, then fail at load — worse than skipping."""
    files = ll.candidate_files("r/x-GGUF", [
        {"type": "file", "path": "x-Q8_0-00001-of-00003.gguf", "size": 1000},
        {"type": "file", "path": "x-Q8_0-00003-of-00003.gguf", "size": 500},
    ])
    assert files == []


def test_shards_in_a_subfolder_group_by_folder():
    """unsloth dynamic quants live in per-quant subfolders."""
    files = ll.candidate_files("r/x-GGUF", [
        {"type": "file", "path": "UD-Q4_K_XL/x-UD-Q4_K_XL-00001-of-00002.gguf", "size": 1000},
        {"type": "file", "path": "UD-Q4_K_XL/x-UD-Q4_K_XL-00002-of-00002.gguf", "size": 1000},
        {"type": "file", "path": "Q2_K/x-Q2_K-00001-of-00002.gguf", "size": 400},
        {"type": "file", "path": "Q2_K/x-Q2_K-00002-of-00002.gguf", "size": 400},
    ])
    assert len(files) == 2
    assert sorted(ll.quant_of(f.filename) for f in files) == ["Q2_K", "UD-Q4_K_XL"]
    # Both parts of each set are remembered, in order.
    assert all(f.is_split and len(f.all_paths) == 2 for f in files)


# ── vision ───────────────────────────────────────────────────────────────────
def test_vision_projector_is_found_preferring_full_precision():
    listing = [
        {"type": "file", "path": "gemma-4-12B-it-Q4_K_M.gguf", "size": 1000},
        {"type": "file", "path": "mmproj-BF16.gguf", "size": 900},
        {"type": "file", "path": "mmproj-Q8_0.gguf", "size": 500},
    ]
    projector = ll.find_mmproj("unsloth/gemma-4-12B-it-GGUF", listing)
    assert projector is not None
    assert projector.filename == "mmproj-BF16.gguf"
    # A text-only repo has no projector, and that is not an error.
    assert ll.find_mmproj("r/x-GGUF", [listing[0]]) is None


def test_vision_models_are_aliased_so_the_app_enables_images():
    """`is_vision_model()` is keyword based; the alias has to carry the signal."""
    for spec in ll.MODEL_SPECS:
        if spec.vision:
            assert spec.alias.endswith("-vision"), spec.id
        else:
            assert not spec.alias.endswith("-vision"), spec.id


# ── launch flags ─────────────────────────────────────────────────────────────
def test_cpu_only_machine_gets_no_offload():
    hw = _system(ram=16, avail=12)
    plan = ll.plan_model(hw, FILES)
    assert ll.plan_gpu_layers(hw, plan) == 0
    assert ll.plan_cpu_moe(hw, plan, 0) == 0


def test_dense_model_bigger_than_vram_gets_partial_offload():
    # 13 GB of weights on a 12 GB card: spill some layers, keep the rest.
    plan = ll.Plan(
        spec=_spec("gemma-4-12b"), file=_file_of("gemma-4-12b", "Q8_0.gguf"),
        quant="Q8_0", context=4096, budget_gb=40, fits=True,
    )
    layers = ll.plan_gpu_layers(_system(ram=64, gpu="RTX 4070", vram=12.0, backend="cuda"), plan)
    assert 0 < layers < 99


def test_moe_keeps_every_layer_on_the_gpu_and_spills_experts_to_ram():
    """--n-cpu-moe is how a 21 GB MoE runs on a 16 GB card."""
    hw = _system(ram=64, avail=50, gpu="RTX 4080", vram=16.0, backend="cuda")
    plan = ll.Plan(
        spec=_spec("qwen3.6-35b-a3b"), file=_file_of("qwen3.6-35b-a3b", "Q4_K_M.gguf"),
        quant="Q4_K_M", context=16384, budget_gb=40, fits=True,
    )
    layers = ll.plan_gpu_layers(hw, plan)
    assert layers == -1
    cpu_moe = ll.plan_cpu_moe(hw, plan, layers)
    assert 0 < cpu_moe < ll._estimate_layers(plan.spec)
    # A dense model never gets --n-cpu-moe: it has no experts to move.
    dense = ll.Plan(
        spec=_spec("qwen3.5-9b"), file=_file_of("qwen3.5-9b", "Q4_K_M.gguf"),
        quant="Q4_K_M", context=16384, budget_gb=40, fits=True,
    )
    assert ll.plan_cpu_moe(hw, dense, -1) == 0


def test_moe_that_fits_entirely_in_vram_needs_no_cpu_experts():
    hw = _system(ram=64, avail=50, gpu="RTX 5090", vram=32.0, backend="cuda")
    plan = ll.Plan(
        spec=_spec("qwen3.6-35b-a3b"), file=_file_of("qwen3.6-35b-a3b", "Q4_K_M.gguf"),
        quant="Q4_K_M", context=8192, budget_gb=40, fits=True,
    )
    assert ll.plan_gpu_layers(hw, plan) == -1
    assert ll.plan_cpu_moe(hw, plan, -1) == 0


def test_server_command_carries_model_alias_and_context():
    cmd = ll.build_server_command(
        pathlib.Path("/usr/bin/llama-server"), pathlib.Path("/m/model.gguf"),
        "psd-qwen3.5-9b", 8080, 8192, -1, 16, probe_flags=False,
    )
    assert cmd[1:3] == ["--model", "/m/model.gguf"]
    assert "--alias" in cmd and "psd-qwen3.5-9b" in cmd
    assert cmd[cmd.index("--ctx-size") + 1] == "8192"
    assert cmd[cmd.index("--n-gpu-layers") + 1] == "-1"
    assert "--no-webui" in cmd


def test_server_command_quantizes_the_kv_cache():
    cmd = ll.build_server_command(
        pathlib.Path("/usr/bin/llama-server"), pathlib.Path("/m/model.gguf"),
        "psd-qwen3.5-9b", 8080, 16384, 0, 6, probe_flags=False,
    )
    assert cmd[cmd.index("--flash-attn") + 1] == "on"
    assert cmd[cmd.index("--cache-type-k") + 1] == "q8_0"
    assert cmd[cmd.index("--cache-type-v") + 1] == "q8_0"
    # Opting out drops all four flags rather than passing empty values.
    plain = ll.build_server_command(
        pathlib.Path("/usr/bin/llama-server"), pathlib.Path("/m/model.gguf"),
        "psd-qwen3.5-9b", 8080, 16384, 0, 6, quantized_kv=False, probe_flags=False,
    )
    assert "--flash-attn" not in plain and "--cache-type-k" not in plain


def test_jinja_is_passed_because_the_agent_needs_real_tool_calls(monkeypatch):
    monkeypatch.setattr(ll, "server_help_text", lambda exe: "--jinja  use chat template")
    cmd = ll.build_server_command(
        pathlib.Path("/usr/bin/llama-server"), pathlib.Path("/m/model.gguf"),
        "psd", 8080, 8192, 0, 8,
    )
    assert "--jinja" in cmd


def test_unknown_flags_are_never_passed_to_an_older_binary(monkeypatch):
    """An unrecognised flag makes llama-server exit immediately."""
    monkeypatch.setattr(ll, "server_help_text", lambda exe: "--model  path")
    cmd = ll.build_server_command(
        pathlib.Path("/usr/bin/llama-server"), pathlib.Path("/m/model.gguf"),
        "psd", 8080, 8192, -1, 8, cpu_moe=12, reasoning=True, jinja=True,
    )
    assert "--jinja" not in cmd
    assert "--n-cpu-moe" not in cmd
    assert "--reasoning-format" not in cmd


def test_reasoning_format_is_requested_for_thinking_models(monkeypatch):
    monkeypatch.setattr(ll, "server_help_text", lambda exe: "--jinja --reasoning-format")
    cmd = ll.build_server_command(
        pathlib.Path("/usr/bin/llama-server"), pathlib.Path("/m/model.gguf"),
        "psd", 8080, 8192, 0, 8, reasoning=True,
    )
    assert cmd[cmd.index("--reasoning-format") + 1] == "auto"


def test_cpu_moe_and_mmproj_reach_the_command_line(monkeypatch, tmp_path):
    monkeypatch.setattr(ll, "server_help_text", lambda exe: "--jinja --n-cpu-moe")
    projector = tmp_path / "mmproj-BF16.gguf"
    projector.write_bytes(b"GGUF")
    cmd = ll.build_server_command(
        pathlib.Path("/usr/bin/llama-server"), pathlib.Path("/m/model.gguf"),
        "psd", 8080, 8192, -1, 8, cpu_moe=18, mmproj=projector,
    )
    assert cmd[cmd.index("--n-cpu-moe") + 1] == "18"
    assert cmd[cmd.index("--mmproj") + 1] == str(projector)


def test_backend_order_prefers_detected_gpu_then_always_ends_on_cpu():
    assert ll.linux_backend_candidates(_system(gpu="RTX 4060", vram=8, backend="cuda")) \
        == ["cuda", "vulkan", "cpu"]
    assert ll.linux_backend_candidates(_system(gpu="RX 7900 XTX", vram=24, backend="rocm")) \
        == ["rocm", "vulkan", "cpu"]
    # Intel Arc: SYCL is the native path, Vulkan the portable one.
    assert ll.linux_backend_candidates(_system(gpu="Intel Arc A770", vram=16, backend="cpu_x86")) \
        == ["sycl", "vulkan", "cpu"]
    # Integrated graphics have no usable VRAM, but SYCL still beats pure CPU.
    assert ll.linux_backend_candidates(_system(gpu="Intel(R) Iris Xe", vram=0.1, backend="cpu_x86")) \
        == ["sycl", "cpu"]
    assert ll.linux_backend_candidates(_system()) == ["cpu"]
    assert ll.linux_backend_candidates(_system(arch="aarch64")) == ["cpu"]


def test_has_gpu_backend_understands_the_linux_builds():
    assert ll.has_gpu_backend(["vulkan", "cpu"]) is True
    assert ll.has_gpu_backend(["sycl", "cpu"]) is True
    assert ll.has_gpu_backend(["cuda", "cpu"]) is True
    assert ll.has_gpu_backend(["cpu"]) is False


# ── hybrid CPU thread planning ───────────────────────────────────────────────
# Token generation is memory-bandwidth bound: it stops scaling at the physical
# performance cores, and on hybrid Intel the E-cores actively drag every worker
# down to their speed. Reported losses are 20-30%, so getting this wrong is the
# single most expensive mistake available on a modern laptop.
def test_hybrid_cpu_pins_generation_to_the_p_cores():
    # i7-13620H: 6 P-cores (12 threads) + 4 E-cores (4 threads) = 10c / 16t.
    assert ll.plan_threads({"cpu_cores": 16, "cpu_physical_cores": 10}) == 6
    # i9-13900K: 8P + 16E = 24c / 32t.
    assert ll.plan_threads({"cpu_cores": 32, "cpu_physical_cores": 24}) == 8
    # i5-12450H: 4P + 4E = 8c / 12t.
    assert ll.plan_threads({"cpu_cores": 12, "cpu_physical_cores": 8}) == 4


def test_non_hybrid_cpu_uses_its_physical_cores():
    # Ryzen 7 5800H: 8 cores, SMT, no E-cores. The same formula yields 8, which
    # is the right answer anyway - one code path covers both shapes of CPU.
    assert ll.plan_threads({"cpu_cores": 16, "cpu_physical_cores": 8}) == 8
    # A CPU without SMT at all.
    assert ll.plan_threads({"cpu_cores": 8, "cpu_physical_cores": 8}) == 8


def test_thread_count_falls_back_when_physical_cores_are_unknown():
    # An older machine, or a container that hides the topology: fall back to the
    # logical count rather than to something arbitrary.
    assert ll.plan_threads({"cpu_cores": 16, "cpu_physical_cores": 0}) == 16
    assert ll.plan_threads({"cpu_cores": 16}) == 16
    assert ll.plan_threads({}) == 1


# ── integrated graphics are not a GPU ────────────────────────────────────────
def test_integrated_graphics_are_treated_as_no_gpu():
    hw = _system(ram=16, avail=11, gpu="Intel(R) UHD Graphics", vram=0.1, backend="cpu_x86")
    assert ll.has_usable_gpu(hw) is False
    plan = ll.plan_model(hw, FILES)
    assert ll.plan_gpu_layers(hw, plan) == 0
    # A real GPU still counts.
    assert ll.has_usable_gpu(_system(gpu="RTX 4060", vram=8, backend="cuda")) is True


# ── context window ───────────────────────────────────────────────────────────
def test_context_wins_over_quant_quality():
    """A smaller file running at 16k beats a sharper one stuck at 2k."""
    hw = _system(ram=16, avail=13)
    plan = ll.plan_model(hw, FILES)
    assert (plan.spec.id, plan.quant, plan.context) == ("qwen3.5-9b", "Q4_K_M", 16384)
    spec9b = _spec("qwen3.5-9b")
    q6 = _file_of("qwen3.5-9b", "Q6_K.gguf")
    assert q6.size_gb > plan.file.size_gb
    budget = ll.memory_budget_gb(hw)
    assert ll._context_for(spec9b, q6.size_gb, budget) < ll._context_for(
        spec9b, plan.file.size_gb, budget
    )


def test_context_ladder_respects_the_model_and_the_profile():
    spec = _spec("qwen3.5-9b")          # trained to 256k
    tiny = _spec("smollm3-3b")          # trained to 8k
    balanced = ll.context_ladder(spec, ll.active_profile("balanced"))
    assert balanced[0] == 32768
    assert ll.context_ladder(spec, ll.active_profile("max"))[0] == 131072
    assert max(ll.context_ladder(tiny, ll.active_profile("max"))) <= tiny.max_context


def test_kv_estimate_scales_with_model_size():
    """The old flat 0.7 GB/8k figure misjudged both ends of the model range."""
    assert ll.plan_context_gb(8192, 8.0) < ll.plan_context_gb(8192, 70.0)
    # A 9B model is 36 layers x 8 KV heads x 128 dims x 2 bytes x 2 (K and V)
    # = 128 KB/token, i.e. ~1.0 GB per 8k of f16 KV cache.
    assert 0.9 < ll.plan_context_gb(8192, 8.0, quantized_kv=False) < 1.1
    # ...and the q8_0 cache we actually request is about half of that, which is
    # what buys the jump from a 4k to a 32k window on a 16 GB laptop.
    assert ll.plan_context_gb(8192, 8.0) < 0.6


def test_moe_kv_cost_uses_the_active_parameter_count():
    """A 35B-A3B MoE writes KV like a small model, not like a 35B dense one."""
    moe = _spec("qwen3.6-35b-a3b")
    dense = _spec("qwen3.6-27b")
    assert ll.plan_context_gb(32768, moe.kv_params_b) < ll.plan_context_gb(32768, dense.kv_params_b)


def test_one_user_means_one_slot():
    """Splitting the window across slots would multiply the KV cache for nobody."""
    hw = _system(ram=16, avail=11)
    plan = ll.plan_model(hw, FILES)
    assert ll.plan_parallel(plan, hw) == 1
    # Plenty of VRAM and a small model: two slots is affordable.
    rich = _system(ram=64, avail=50, gpu="RTX 4090", vram=24.0, backend="cuda")
    small = ll.plan_model(rich, FILES, prefer_id="qwen3.5-9b")
    assert ll.plan_parallel(small, rich) == 2
    # ...but a 30 GB MoE is memory-bound whatever the card, so it stays at one.
    big = ll.plan_model(rich, FILES)
    assert big.file.size_gb > 14.0
    assert ll.plan_parallel(big, rich) == 1
    # No GPU at all: never split.
    assert ll.plan_parallel(small, _system(ram=64, avail=50)) == 1


def test_fedora_laptop_gets_the_tuned_plan():
    """End-to-end golden check for the machine run.sh actually ships to."""
    hw = {
        "cpu_cores": 16, "cpu_physical_cores": 10, "cpu_arch": "x86_64",
        "gpu_name": "Intel(R) Iris Xe", "gpu_vram_gb": 0.1, "has_gpu": True,
        "backend": "cpu_x86", "total_ram_gb": 15.7, "available_ram_gb": 11.0,
    }
    plan = ll.plan_model(hw, FILES)
    assert plan.spec.id == "qwen3.5-9b"
    assert plan.context == 8192
    cmd = ll.build_server_command(
        pathlib.Path("/usr/bin/llama-server"), pathlib.Path("m.gguf"),
        "psd", 8080, plan.context, ll.plan_gpu_layers(hw, plan), ll.plan_threads(hw),
        parallel=ll.plan_parallel(plan, hw), probe_flags=False,
    )
    assert cmd[cmd.index("--threads") + 1] == "6"      # not 16 - no E-cores
    assert cmd[cmd.index("--ctx-size") + 1] == "8192"  # not 4096
    assert cmd[cmd.index("--n-gpu-layers") + 1] == "0"  # Iris Xe is not a GPU here
    assert cmd[cmd.index("--parallel") + 1] == "1"
    assert cmd[cmd.index("--flash-attn") + 1] == "on"

    # PSD_MODEL_PROFILE=power is the knob for a longer window on this box.
    power = ll.plan_model(hw, FILES, profile=ll.POWER_PROFILES["power"])
    assert (power.spec.id, power.quant, power.context) == ("qwen3.5-9b", "Q4_K_M", 16384)
    # ...and `max` spends the whole machine on the strongest single model.
    top = ll.plan_model(hw, FILES, profile=ll.POWER_PROFILES["max"])
    assert top.spec.params_b >= 12.0 and top.context == 16384


# ── model group ──────────────────────────────────────────────────────────────
def test_group_never_returns_fewer_than_three_models():
    for hw in (_system(ram=8, avail=5.5), _system(ram=16, avail=12), _system(ram=64, avail=50)):
        group = ll.plan_model_group(hw, FILES)
        assert group is not None
        assert ll.MIN_GROUP_MODELS <= len(group.plans) <= ll.MAX_GROUP_MODELS
        assert group.total_gb <= group.budget_gb + 0.01


def test_group_members_are_distinct_models():
    group = ll.plan_model_group(_system(ram=32, avail=26), FILES)
    ids = [plan.spec.id for plan in group.plans]
    assert len(ids) == len(set(ids))


def test_group_budget_is_respected_on_a_big_machine():
    hw = _system(ram=64, avail=56, gpu="RTX 4090", vram=24.0, backend="cuda")
    group = ll.plan_model_group(hw, FILES)
    assert group is not None
    assert group.total_gb <= group.budget_gb + 0.01
    assert group.plans[0].spec.params_b >= 9.0


def test_group_shortlist_keeps_cheap_members_so_the_group_can_be_filled():
    """Without the smallest specs a tight budget could never reach 3 models."""
    hw = _system(ram=8, avail=6.0)
    budget = ll.group_memory_budget_gb(hw)
    variants = {
        spec.id: ll._group_variants(
            spec, [f for repo in spec.repos for f in FILES.get(repo, [])], budget, "Q3_K_M"
        )
        for spec in ll.MODEL_SPECS
    }
    shortlist = ll._group_shortlist(list(ll.MODEL_SPECS), variants)
    assert len(shortlist) <= ll.MAX_GROUP_CANDIDATES + 3
    cheapest = min(
        (spec for spec in shortlist if variants.get(spec.id)),
        key=lambda s: min(ll._variant_cost(p) for p in variants[s.id]),
    )
    assert cheapest.params_b <= 2.0


def test_prefer_tier_biases_the_group_towards_coding_models():
    hw = _system(ram=64, avail=56, gpu="RTX 4090", vram=24.0, backend="cuda")
    group = ll.plan_model_group(hw, FILES, prefer_tier="coding")
    assert group is not None
    assert any(plan.spec.tier == "coding" for plan in group.plans)


def test_group_planning_stays_fast_with_a_big_catalogue():
    """The search is bounded: 22 specs must not mean C(22,5) combinations."""
    import time

    hw = _system(ram=48, avail=40, gpu="RTX 4090", vram=24.0, backend="cuda")
    started = time.monotonic()
    group = ll.plan_model_group(hw, FILES)
    elapsed = time.monotonic() - started
    assert group is not None
    assert elapsed < 5.0, f"group planning took {elapsed:.1f}s"


# ── disk space ───────────────────────────────────────────────────────────────
def test_disk_shortfall_is_reported_before_a_multi_gb_download(monkeypatch, tmp_path):
    plan = ll.Plan(
        spec=_spec("qwen3.6-35b-a3b"), file=_file_of("qwen3.6-35b-a3b", "Q4_K_M.gguf"),
        quant="Q4_K_M", context=8192, budget_gb=40, fits=True,
    )
    monkeypatch.setattr(ll, "free_disk_gb", lambda path: 10.0)
    assert ll.disk_shortfall_gb([plan], tmp_path) > 10.0
    monkeypatch.setattr(ll, "free_disk_gb", lambda path: 500.0)
    assert ll.disk_shortfall_gb([plan], tmp_path) == 0.0
    # An unreadable free-space figure must never block a launch.
    monkeypatch.setattr(ll, "free_disk_gb", lambda path: 0.0)
    assert ll.disk_shortfall_gb([plan], tmp_path) == 0.0


def test_files_already_on_disk_are_not_counted_again(monkeypatch, tmp_path):
    plan = ll.Plan(
        spec=_spec("qwen3.5-9b"), file=_file_of("qwen3.5-9b", "Q4_K_M.gguf"),
        quant="Q4_K_M", context=8192, budget_gb=40, fits=True,
    )
    target = tmp_path / plan.file.repo.replace("/", "__") / plan.file.filename
    target.parent.mkdir(parents=True)
    monkeypatch.setattr(ll, "free_disk_gb", lambda path: 3.0)
    without = ll.disk_shortfall_gb([plan], tmp_path)
    # A sparse file reports its full size without occupying the disk, which is
    # all the accounting here looks at.
    with open(target, "wb") as fh:
        fh.truncate(plan.file.size)
    with_file = ll.disk_shortfall_gb([plan], tmp_path)
    assert without == pytest.approx(plan.file.size_gb + 1.0, abs=0.01)
    assert with_file == pytest.approx(1.0, abs=0.01)  # only the reserve is left


# ── release picking (real ggml-org Linux asset names) ────────────────────────
RELEASES = [
    # The semver "latest release" only carries a pointer file — no binaries.
    {"tag_name": "v0.4.1", "prerelease": False,
     "assets": [{"name": "nightly-tag.txt", "browser_download_url": "u0"}]},
    # A Windows-only build must be skipped: psd.ai no longer runs there.
    {"tag_name": "b11063", "assets": [
        {"name": "llama-b11063-bin-win-cpu-x64.zip", "browser_download_url": "w1"}]},
    {"tag_name": "b11064", "assets": [
        {"name": "llama-b11064-bin-ubuntu-x64.tar.gz", "browser_download_url": "u2"},
        {"name": "llama-b11064-bin-ubuntu-arm64.tar.gz", "browser_download_url": "u8"},
        {"name": "llama-b11064-bin-ubuntu-vulkan-x64.tar.gz", "browser_download_url": "u3"},
        {"name": "llama-b11064-bin-ubuntu-vulkan-arm64.tar.gz", "browser_download_url": "u9"},
        {"name": "llama-b11064-bin-ubuntu-cuda-13.3-x64.tar.gz", "browser_download_url": "u4b"},
        {"name": "llama-b11064-bin-ubuntu-cuda-12.8-x64.tar.gz", "browser_download_url": "u4"},
        {"name": "llama-b11064-bin-ubuntu-rocm-10.0-x64.tar.gz", "browser_download_url": "u5"},
        {"name": "llama-b11064-bin-ubuntu-sycl-fp16-x64.tar.gz", "browser_download_url": "u6"},
        {"name": "llama-b11064-bin-ubuntu-sycl-fp32-x64.tar.gz", "browser_download_url": "u6b"},
        {"name": "cudart-llama-b11064-bin-ubuntu-cuda-12.8-x64.tar.gz",
         "browser_download_url": "u7"},
        {"name": "llama-b11064-bin-win-cpu-x64.zip", "browser_download_url": "w2"},
    ]},
]


def test_picks_newest_build_with_linux_binaries():
    assert ll.pick_llama_release(RELEASES)["tag"] == "b11064"


def test_semver_and_windows_only_releases_are_ignored():
    assert ll.pick_llama_release(RELEASES[:1]) is None
    assert ll.pick_llama_release(RELEASES[:2]) is None


@pytest.mark.parametrize("kind,url", [
    ("cpu", "u2"), ("vulkan", "u3"), ("cuda", "u4"), ("rocm", "u5"), ("sycl", "u6"),
])
def test_match_release_asset(kind, url):
    picked = ll.pick_llama_release(RELEASES)
    asset = ll.match_release_asset(picked["assets"], picked["tag"], kind, "x64")
    assert asset["browser_download_url"] == url


def test_cuda_prefers_the_widely_compatible_toolkit():
    """CUDA 12.x runs on any driver >= 525; 13.x needs a very current one."""
    picked = ll.pick_llama_release(RELEASES)
    asset = ll.match_release_asset(picked["assets"], picked["tag"], "cuda", "x64")
    assert ll.cuda_version_of(asset) == "12.8"


def test_arm64_gets_its_own_builds():
    picked = ll.pick_llama_release(RELEASES)
    assert ll.match_release_asset(picked["assets"], picked["tag"], "cpu", "arm64")[
        "browser_download_url"] == "u8"
    assert ll.match_release_asset(picked["assets"], picked["tag"], "vulkan", "arm64")[
        "browser_download_url"] == "u9"


def test_match_release_asset_never_grabs_the_cudart_bundle():
    """cudart-*.tar.gz is the CUDA runtime, not a server build."""
    picked = ll.pick_llama_release(RELEASES)
    asset = ll.match_release_asset(picked["assets"], picked["tag"], "cuda", "x64")
    assert not asset["name"].startswith("cudart-")


def test_cudart_bundle_is_matched_to_its_cuda_build():
    """Without it a driver-only Fedora box cannot dlopen libcudart."""
    picked = ll.pick_llama_release(RELEASES)
    bundle = ll.match_cudart_asset(picked["assets"], picked["tag"], "x64", "12.8")
    assert bundle["browser_download_url"] == "u7"
    assert ll.match_cudart_asset(picked["assets"], picked["tag"], "x64", "13.3") is None


def test_backend_detection_reads_the_shared_objects(tmp_path):
    """Linux builds ship ggml backends as sibling .so files."""
    bindir = tmp_path / "build" / "bin"
    bindir.mkdir(parents=True)
    exe = bindir / "llama-server"
    exe.write_bytes(b"\x7fELF" + b"\x00" * 64)
    (bindir / "libggml-vulkan.so").write_bytes(b"")
    (bindir / "libggml-cuda.so").write_bytes(b"")
    assert ll.detect_llama_backends(exe) == ["vulkan", "cuda", "cpu"]
    # No siblings: fall back to a byte scan of the binary itself.
    exe.write_bytes(b"\x7fELF" + b"libggml-hip.so\x00")
    for sibling in bindir.glob("*.so"):
        sibling.unlink()
    assert ll.detect_llama_backends(exe) == ["cuda", "cpu"]
    assert ll.detect_llama_backends(tmp_path / "does-not-exist") == ["cpu"]


def test_safe_extract_refuses_to_escape_the_destination(tmp_path):
    """A malicious archive must not be able to write outside the runtime dir."""
    import tarfile

    victim = tmp_path / "evil.tar.gz"
    payload = tmp_path / "payload"
    payload.write_text("pwned", encoding="utf-8")
    with tarfile.open(victim, "w:gz") as tar:
        info = tarfile.TarInfo("../../escaped.txt")
        info.size = payload.stat().st_size
        with open(payload, "rb") as fh:
            tar.addfile(info, fh)
    dest = tmp_path / "runtime" / "llama.cpp"
    with pytest.raises(RuntimeError, match="outside"):
        ll._safe_extract_tar(victim, dest)
    assert not (tmp_path / "escaped.txt").exists()


def test_safe_extract_unpacks_a_normal_archive(tmp_path):
    import tarfile

    archive = tmp_path / "ok.tar.gz"
    staging = tmp_path / "staging" / "build" / "bin"
    staging.mkdir(parents=True)
    binary = staging / "llama-server"
    binary.write_bytes(b"\x7fELF")
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(tmp_path / "staging", arcname=".")
    dest = tmp_path / "runtime" / "llama.cpp"
    ll._safe_extract_tar(archive, dest)
    assert (dest / "build" / "bin" / "llama-server").exists()


# ── setup handshake used by run.sh ───────────────────────────────────────────
def test_wait_for_ready_returns_immediately_when_state_exists(tmp_path):
    state = tmp_path / "local_model.json"
    state.write_text("{}", encoding="utf-8")
    assert ll.wait_for_ready(30, state_file=state, fail_file=tmp_path / "no.txt", poll=0.05) == 0


def test_wait_for_ready_bails_on_failure_marker_instead_of_blocking(tmp_path):
    fail = tmp_path / "local_model_failed.txt"
    fail.write_text("network unreachable", encoding="utf-8")
    rc = ll.wait_for_ready(600, state_file=tmp_path / "never.json", fail_file=fail, poll=0.05)
    assert rc == 3


def test_wait_for_ready_times_out_with_code_3(tmp_path):
    rc = ll.wait_for_ready(1, state_file=tmp_path / "never.json", fail_file=tmp_path / "no.txt", poll=0.05)
    assert rc == 3


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    """Point src.settings at a throwaway file.

    Without this the tests read/write the repo's real data/settings.json, so a
    default written by one test (or one run) makes the next test's
    "became default" assertion fail.
    """
    import src.settings as settings_mod

    target = tmp_path / "settings.json"
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", str(target))
    settings_mod._invalidate_caches()
    yield settings_mod
    settings_mod._invalidate_caches()


def test_endpoint_and_default_are_written_to_the_app(isolated_settings):
    """The whole point: after setup the app's picker has a working default."""
    plan = ll.plan_model(_system(ram=16, avail=12), FILES)
    endpoint_id = ll.register_endpoint("http://127.0.0.1:8080/v1", plan.spec.alias, plan)

    from core.database import ModelEndpoint, SessionLocal
    db = SessionLocal()
    try:
        row = db.query(ModelEndpoint).filter(ModelEndpoint.id == endpoint_id).first()
        assert row is not None
        assert row.base_url == "http://127.0.0.1:8080/v1"
        assert row.is_enabled is True
        assert row.endpoint_kind == "local"
        assert row.supports_tools is True
        assert json.loads(row.cached_models) == [plan.spec.alias]
    finally:
        db.close()

    assert ll.set_default_model(endpoint_id, plan.spec.alias) is True
    settings = isolated_settings.load_settings()
    assert settings["default_endpoint_id"] == endpoint_id
    assert settings["default_model"] == plan.spec.alias
    # Without this a non-admin's composer never resolves the global default.
    assert settings["share_defaults_with_users"] is True


def test_existing_default_is_not_clobbered(isolated_settings):
    isolated_settings.save_settings({
        **isolated_settings.load_settings(),
        "default_endpoint_id": "openrouter", "default_model": "gpt-x",
    })
    plan = ll.plan_model(_system(ram=16, avail=12), FILES)
    endpoint_id = ll.register_endpoint("http://127.0.0.1:8080/v1", plan.spec.alias, plan)
    assert ll.set_default_model(endpoint_id, plan.spec.alias) is False
    assert isolated_settings.load_settings()["default_model"] == "gpt-x"


# ── port handling ────────────────────────────────────────────────────────────
def test_occupied_port_reports_a_clear_error(tmp_path, monkeypatch):
    """Port 8080 is a popular default; say what to do instead of a bind error."""
    import socket

    _setup_paths(monkeypatch, tmp_path)
    squatter = socket.socket()
    squatter.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    squatter.bind(("127.0.0.1", 0))
    squatter.listen(1)
    port = squatter.getsockname()[1]
    try:
        with pytest.raises(RuntimeError, match="already in use"):
            ll.prepare_local_llama(
                port=port, system=_system(ram=16, avail=12),
                fetch_files=_fetch_one_repo, installer=_fake_installer,
                downloader=_fake_downloader, health_timeout=5,
            )
    finally:
        squatter.close()


def test_healthy_server_on_the_port_is_reused(tmp_path, monkeypatch):
    """A llama-server from an earlier run must not be started twice."""
    import http.server, socketserver, threading

    _setup_paths(monkeypatch, tmp_path)
    alias = _spec("qwen3.5-9b").alias

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            if self.path == "/health":
                self.send_response(200); self.send_header("Content-Length", "2")
                self.end_headers(); self.wfile.write(b"{}")
            elif self.path == "/v1/models":
                body = json.dumps({"data": [{"id": alias}]}).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(body)))
                self.end_headers(); self.wfile.write(body)
            else:
                self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers()

    class S(socketserver.TCPServer):
        allow_reuse_address = True

    started = []
    def installer_that_must_not_start_a_server(system, prefer=()):
        started.append(1)
        return _fake_installer(system, prefer)

    with S(("127.0.0.1", 0), H) as httpd:
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        result = ll.prepare_local_llama(
            port=port, system=_system(ram=16, avail=12),
            fetch_files=_fetch_one_repo, installer=installer_that_must_not_start_a_server,
            downloader=_fake_downloader, health_timeout=5,
        )
        httpd.shutdown()

    assert result["model_id"] == alias
    assert result.get("reused") is True
    assert result["proc"] is None
    assert result["profile"] == "balanced"
    assert started, "installer still runs (it resolves the binary), but no server is spawned"


def test_split_release_downloads_every_part(tmp_path, monkeypatch):
    """Starting a model with a missing shard fails at load, not at download."""
    _setup_paths(monkeypatch, tmp_path)
    repo = "unsloth/Qwen3.5-122B-A10B-GGUF"
    listing = [
        {"type": "file", "path": f"Qwen3.5-122B-A10B-Q8_0-0000{i}-of-00004.gguf", "size": 1024}
        for i in range(1, 5)
    ]
    candidates = ll.candidate_files(repo, listing)
    assert len(candidates) == 1 and candidates[0].is_split
    plan = ll.Plan(
        spec=_spec("qwen3.5-122b-a10b"), file=candidates[0], quant="Q8_0",
        context=8192, budget_gb=200, fits=True,
    )

    fetched = []

    def downloader(url, dest, expected_size=0, label=""):
        fetched.append(url.rsplit("/", 1)[-1])
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as fh:
            fh.truncate(expected_size or 1024)   # sparse: exact size, no bytes
        return dest

    path = ll.download_plan_files(plan, downloader)
    assert fetched == [f"Qwen3.5-122B-A10B-Q8_0-0000{i}-of-00004.gguf" for i in range(1, 5)]
    assert path.name == "Qwen3.5-122B-A10B-Q8_0-00001-of-00004.gguf"
    # A complete set on disk is never re-downloaded.
    fetched.clear()
    ll.download_plan_files(plan, downloader)
    assert fetched == []


def test_offline_rerun_reads_the_model_cache(tmp_path, monkeypatch):
    """--skip-download must see split releases already on disk."""
    _setup_paths(monkeypatch, tmp_path)
    repo_dir = ll.MODELS_DIR / "unsloth__Qwen3.5-9B-GGUF"
    repo_dir.mkdir(parents=True)
    (repo_dir / "Qwen3.5-9B-Q4_K_M.gguf").write_bytes(b"\x00" * 4096)
    (repo_dir / "mmproj-BF16.gguf").write_bytes(b"\x00" * 1024)

    candidates, raw = ll.collect_repo_files(ll.MODEL_SPECS, skip_download=True)
    assert any(f.filename == "Qwen3.5-9B-Q4_K_M.gguf" for f in candidates[
        "unsloth/Qwen3.5-9B-GGUF"])
    plan = ll.plan_model(_system(ram=16, avail=12), candidates, prefer_id="qwen3.5-9b")
    assert plan is not None
    # The projector is not a model candidate, but it is still findable.
    assert ll.find_mmproj("unsloth/Qwen3.5-9B-GGUF", raw["unsloth/Qwen3.5-9B-GGUF"]) is not None


# ── auxiliary GGUFs: drafts and projectors are not models ────────────────────
def test_candidate_files_skips_drafts_and_projectors():
    """eagle3/MTP drafts and mmproj projectors must never become main weights.

    A draft carries an attractive quant tag (eagle3-gpt-oss-20b-Q8_0 outranks
    gpt-oss-20b-MXFP4), loads fine, then dies at context creation with
    "eagle3 requires ctx_other to be set" - the exact failure a real Fedora
    first run hit with three dead servers.
    """
    repo = "ggml-org/gpt-oss-20b-GGUF"
    files = ll.candidate_files(repo, [
        {"type": "file", "path": "eagle3-gpt-oss-20b-Q8_0.gguf", "size": int(22.0 * GB)},
        {"type": "file", "path": "gpt-oss-20b-MXFP4.gguf", "size": int(12.0 * GB)},
        {"type": "file", "path": "mmproj-F16.gguf", "size": int(1.0 * GB)},
    ])
    assert [f.filename for f in files] == ["gpt-oss-20b-MXFP4.gguf"]

    gemma = ll.candidate_files("bartowski/gemma-4-12B-it-GGUF", [
        {"type": "file", "path": "mtp-gemma-4-12B-it-Q8_0.gguf", "size": int(2.0 * GB)},
        {"type": "file", "path": "gemma-4-12B-it-Q8_0.gguf", "size": int(13.0 * GB)},
        {"type": "file", "path": "gemma-4-12B-it-draft-Q4_K_M.gguf", "size": int(4.0 * GB)},
    ])
    assert [f.filename for f in gemma] == ["gemma-4-12B-it-Q8_0.gguf"]


def test_projector_still_found_by_find_mmproj():
    """Excluding mmproj from candidates must not blind the vision path."""
    repo = "unsloth/gemma-4-E2B-it-GGUF"
    entries = [
        {"type": "file", "path": "mmproj-F16.gguf", "size": int(1.0 * GB)},
        {"type": "file", "path": "gemma-4-E2B-it-Q4_K_M.gguf", "size": int(3.0 * GB)},
    ]
    assert ll.candidate_files(repo, entries)[0].filename == "gemma-4-E2B-it-Q4_K_M.gguf"
    projector = ll.find_mmproj(repo, entries)
    assert projector is not None and projector.filename == "mmproj-F16.gguf"


def test_group_plan_never_serves_a_speculative_draft():
    listing = {
        repo: files for repo, files in FILES.items()
        if repo != "unsloth/gpt-oss-20b-GGUF"
    }
    listing["ggml-org/gpt-oss-20b-GGUF"] = ll.candidate_files(
        "ggml-org/gpt-oss-20b-GGUF",
        [
            {"type": "file", "path": "eagle3-gpt-oss-20b-Q8_0.gguf", "size": int(22.0 * GB)},
            {"type": "file", "path": "gpt-oss-20b-MXFP4.gguf", "size": int(12.0 * GB)},
        ],
    )
    plan = ll.plan_model_group(_system(ram=24.0), listing)
    assert plan is not None
    for member in plan.plans:
        low = member.file.filename.lower()
        assert not low.startswith(("eagle", "mtp-", "mmproj")), member.file.filename
    assert plan.plans[0].file.filename == "gpt-oss-20b-MXFP4.gguf"


def test_wait_message_quotes_the_model_log(monkeypatch, tmp_path):
    """The wait loop must say what is moving instead of repeating itself."""
    monkeypatch.setattr(
        ll, "_model_log_tail",
        lambda: "[dl] Qwen3.5-9B-Q4_K_M.gguf 42% (5.6 GB)",
    )
    seen = []
    monkeypatch.setattr(ll, "log", lambda msg: seen.append(msg))
    monkeypatch.setattr(ll.time, "sleep", lambda _s: None)

    clock = {"now": 1000.0}

    def fake_time():
        clock["now"] += 1.0
        return clock["now"]

    monkeypatch.setattr(ll.time, "time", fake_time)
    rc = ll.wait_for_ready(
        40, state_file=tmp_path / "state.json", fail_file=tmp_path / "fail.txt",
        poll=0.0,
    )
    assert rc == 3  # nothing ever became ready: the timeout path
    beats = [m for m in seen if "still waiting" in m]
    assert beats, seen
    assert "Qwen3.5-9B-Q4_K_M.gguf 42%" in beats[0]
    assert beats[0].strip().startswith("[")
    # Quiet after the first few beats: 40 s of waiting is at most two lines,
    # not the forty-identical-line wall a first run used to print.
    assert len(beats) <= 2, beats


# ── download resilience: one dead CDN socket must not kill the whole group ──
class _FakeResponse:
    """Minimal urlopen() result: sliced body, dict headers, context manager."""

    def __init__(self, body: bytes, start: int, stop_after=None):
        self.status = 206 if start else 200
        self.headers = {"Content-Length": str(len(body) - start)}
        self._data = body[start:] if stop_after is None else body[start:start + stop_after]
        self._pos = 0

    def read(self, n):
        buf = self._data[self._pos:self._pos + n]
        self._pos += len(buf)
        return buf

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _range_start(req):
    rng = req.get_header("Range")
    return int(rng.split("=", 1)[1].rstrip("-")) if rng else 0


def test_download_file_resumes_after_a_dropped_connection(tmp_path, monkeypatch):
    """A truncated transfer retries with a Range header instead of raising."""
    payload = b"A" * 400 + b"B" * 600
    dest = tmp_path / "model.gguf"
    starts = []

    def fake_urlopen(req, timeout=None):
        start = _range_start(req)
        starts.append(start)
        # First connection dies after 400 bytes; the retry serves the rest.
        return _FakeResponse(payload, start, stop_after=400 if start == 0 else None)

    monkeypatch.setattr(ll.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(ll.time, "sleep", lambda _s: None)
    out = ll.download_file("http://fake/model.gguf", dest, expected_size=len(payload))
    assert out == dest
    assert dest.read_bytes() == payload
    assert starts == [0, 400]  # second attempt resumed exactly where it stopped
    assert not dest.with_suffix(".gguf.part").exists()


def test_download_file_reconnects_when_the_transfer_stalls(tmp_path, monkeypatch):
    """A connection that trickles below the floor is abandoned and resumed."""
    payload = b"x" * 100
    dest = tmp_path / "stall.gguf"
    starts = []
    seen_logs = []
    monkeypatch.setattr(ll, "log", lambda msg="": seen_logs.append(msg))
    monkeypatch.setattr(ll, "_STALL_WINDOW", 0.0)   # check on every iteration
    monkeypatch.setattr(ll, "_STALL_MIN_RATE", 8 * 1024)

    class StallingResponse(_FakeResponse):
        def __init__(self, body, start):
            super().__init__(body, start)
            self._first = start == 0

        def read(self, n):
            if not self._first:
                ll._STALL_MIN_RATE = 0.0  # the fresh connection never stalls
                return super().read(n)
            if self._pos == 0:
                # First connection: hand over 40 bytes, then make sure no
                # rate can ever pass the floor again -> stall detector fires.
                ll._STALL_MIN_RATE = float("inf")
                self._pos = 40
                return payload[:40]
            return b"x"  # trickle forever, never EOF: a zombie CDN socket

    def fake_urlopen(req, timeout=None):
        start = _range_start(req)
        starts.append(start)
        return StallingResponse(payload, start)

    monkeypatch.setattr(ll.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(ll.time, "sleep", lambda _s: None)
    out = ll.download_file("http://fake/stall.gguf", dest, expected_size=len(payload))
    assert out.read_bytes() == payload
    assert starts == [0, 40]
    assert any("stalled" in m for m in seen_logs), seen_logs
    assert any("resuming from 0.00 GB" in m for m in seen_logs), seen_logs


def test_download_file_gives_up_after_all_attempts(tmp_path, monkeypatch):
    """Retries are bounded: a permanently broken URL still fails the plan."""
    dest = tmp_path / "broken.gguf"
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(1)
        raise ll.urllib.error.URLError("network unreachable")

    monkeypatch.setattr(ll.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(ll.time, "sleep", lambda _s: None)
    with pytest.raises(OSError):
        ll.download_file("http://fake/broken.gguf", dest, expected_size=10, attempts=3)
    assert len(calls) == 3
