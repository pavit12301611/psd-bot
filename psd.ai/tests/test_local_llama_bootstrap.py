"""scripts/local_llama.py — model selection, launch flags and setup handshake.

run.bat calls this to download and serve the best local Llama for the machine,
so the picker is not empty on a fresh install. The selection rules are what
make "best" mean "best that actually runs here": a 70B at IQ1 on a laptop is
worse than an 8B at Q6_K, and a model that only fits at a 2048 context is
worse than a smaller one at 8192.
"""

import json

import pytest

from scripts import local_llama as ll


def _files(repo, quants):
    """Build a GGUF file list shaped like the Hugging Face tree API response."""
    name = repo.rsplit("/", 1)[-1].removesuffix("-GGUF")
    return ll.candidate_files(
        repo,
        [{"type": "file", "path": f"{name}-{q}.gguf", "size": size} for q, size in quants.items()],
    )


# Sizes mirror the real unsloth / bartowski Llama GGUF repos.
Q8B = {
    "BF16": 16_068_895_872, "Q2_K": 3_179_136_384, "Q3_K_M": 4_018_922_880,
    "Q4_K_M": 4_920_739_200, "Q5_K_M": 5_732_992_384, "Q6_K": 6_596_011_392,
    "Q8_0": 8_540_000_000,
}
Q70B = {
    "Q2_K": 26_000_000_000, "Q3_K_M": 33_000_000_000, "Q4_K_M": 42_500_000_000,
    "Q5_K_M": 50_000_000_000, "Q8_0": 75_000_000_000,
}
Q3B = {"Q3_K_M": 1_815_347_744, "Q4_K_M": 2_019_377_696, "Q6_K": 2_643_853_856, "Q8_0": 3_421_899_296}
Q1B = {"Q3_K_M": 690_843_680, "Q4_K_M": 807_694_368, "Q6_K": 1_021_800_480, "Q8_0": 1_321_082_528}

REPOS = {
    "unsloth/Llama-3.3-70B-Instruct-GGUF": Q70B,
    "unsloth/Llama-3.1-8B-Instruct-GGUF": Q8B,
    "bartowski/Llama-3.2-3B-Instruct-GGUF": Q3B,
    "unsloth/Llama-3.2-1B-Instruct-GGUF": Q1B,
}
FILES = {repo: _files(repo, quants) for repo, quants in REPOS.items()}


def _system(ram=32.0, avail=None, gpu=None, vram=0.0, backend="cpu_x86", unified=False,
            arch="x86_64", cores=16, phys=8):
    return {
        "has_gpu": gpu is not None, "gpu_name": gpu, "gpu_vram_gb": vram,
        "gpu_count": 1 if gpu else 0, "backend": backend, "unified_memory": unified,
        "available_ram_gb": avail if avail is not None else ram * 0.75,
        "total_ram_gb": ram, "cpu_cores": 16, "cpu_physical_cores": phys,
        "cpu_arch": arch,
    }


# ── shared fakes for prepare_local_llama tests ───────────────────────────────
def _setup_paths(monkeypatch, tmp_path):
    for name, sub in [
        ("RUNTIME_DIR", "runtime"), ("MODELS_DIR", "runtime/models"),
        ("LLAMA_DIR", "runtime/llama.cpp"),
    ]:
        monkeypatch.setattr(ll, name, tmp_path / sub)
    monkeypatch.setattr(ll, "STATE_FILE", tmp_path / "runtime" / "local_model.json")
    monkeypatch.setattr(ll, "FAIL_FILE", tmp_path / "runtime" / "local_model_failed.txt")
    monkeypatch.setattr(ll, "LOG_FILE", tmp_path / "runtime" / "llama-server.log")


def _fetch_one_repo(repo, revision="main"):
    if repo == "unsloth/Llama-3.1-8B-Instruct-GGUF":
        return [{"type": "file", "path": "Llama-3.1-8B-Instruct-Q8_0.gguf", "size": 4096}]
    raise KeyError(repo)


def _fake_installer(system, prefer=()):
    import pathlib as _pl, sys as _sys
    return _pl.Path(_sys.executable), ["cpu"]


def _fake_downloader(url, dest, expected_size=0, label=""):
    """Stand in for the multi-GB weight download (covered separately)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"GGUF" + b"\x00" * 4092)
    return dest


# ── selection ────────────────────────────────────────────────────────────────
def test_small_ram_lands_on_the_biggest_model_that_fits():
    plan = ll.plan_model(_system(ram=8, avail=5.5), FILES)
    assert plan.spec.id == "llama-3.2-3b"
    assert plan.quant == "Q6_K"
    assert plan.fits is True


def test_16gb_cpu_machine_gets_8b_not_a_crippled_70b():
    plan = ll.plan_model(_system(ram=16, avail=12), FILES)
    assert plan.spec.id == "llama-3.1-8b"
    # Q6_K is the sharper file, but only at an 8k window; Q5_K_M buys 16k.
    # Context wins, quality breaks the tie.
    assert plan.quant == "Q5_K_M"
    assert plan.context == 16384


def test_big_gpu_gets_the_strongest_model():
    plan = ll.plan_model(
        _system(ram=96, avail=80, gpu="RTX 3090 x2", vram=48.0, backend="cuda"), FILES
    )
    assert plan.spec.id == "llama-3.3-70b"
    # 47 GB of VRAM cannot hold Q5_K_M (50 GB) plus its KV cache, and the KV
    # cache is now estimated per-model instead of as a flat 0.7 GB, so the
    # 70B fits one quant lower at four times the context.
    assert plan.quant == "Q4_K_M"
    assert plan.context == 16384


def test_f16_is_never_picked_over_q8_0():
    """BF16 doubles the download and halves the speed for no usable gain."""
    plan = ll.plan_model(
        _system(ram=64, avail=50, gpu="RTX 4090", vram=24.0, backend="cuda"), FILES
    )
    assert plan.quant == "Q8_0"
    assert plan.file.size_gb < 10


def test_model_that_only_fits_at_2048_context_is_rejected():
    """A 32 GB card cannot hold 70B at a usable context, so 8B@Q8 wins."""
    plan = ll.plan_model(
        _system(ram=128, avail=110, gpu="RTX 5090", vram=32.0, backend="cuda"), FILES
    )
    assert plan.spec.id == "llama-3.1-8b"
    assert plan.context >= ll.MIN_USABLE_CONTEXT


def test_apple_silicon_uses_unified_memory_budget():
    plan = ll.plan_model(
        _system(ram=16, avail=11, gpu="Apple M3", vram=10.7, backend="metal",
                unified=True, arch="arm64"),
        FILES,
    )
    assert plan.spec.id == "llama-3.1-8b"
    assert ll.plan_gpu_layers(
        _system(ram=16, avail=11, gpu="Apple M3", vram=10.7, backend="metal", unified=True),
        plan,
    ) == -1


def test_tiny_machine_still_gets_a_local_model():
    plan = ll.plan_model(_system(ram=4, avail=2.2), FILES)
    assert plan.spec.id == "llama-3.2-1b"
    assert plan.fits is True


def test_nothing_available_returns_none():
    assert ll.plan_model(_system(), {}) is None


def test_prefer_id_overrides_the_ranking():
    plan = ll.plan_model(_system(ram=16, avail=12), FILES, prefer_id="llama-3.2-1b")
    assert plan.spec.id == "llama-3.2-1b"


# ── quant parsing ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name,expected", [
    ("Llama-3.1-8B-Instruct-Q4_K_M.gguf", "Q4_K_M"),
    ("Llama-3.1-8B-Instruct-Q8_0.gguf", "Q8_0"),
    ("Llama-3.1-8B-Instruct-UD-Q4_K_XL.gguf", "UD-Q4_K_XL"),
    ("Llama-3.1-8B-Instruct-IQ4_XS.gguf", "IQ4_XS"),
    ("Llama-3.1-8B-Instruct-BF16.gguf", "BF16"),
    ("Llama-3.1-8B-Instruct.imatrix", None),
    ("README.md", None),
])
def test_quant_of(name, expected):
    assert ll.quant_of(name) == expected


def test_split_shards_are_skipped():
    """Multi-part GGUFs need every shard; a single-file download would 404."""
    files = ll.candidate_files("r/x-GGUF", [
        {"type": "file", "path": "x-Q8_0-00001-of-00003.gguf", "size": 1000},
        {"type": "file", "path": "x-Q4_K_M.gguf", "size": 2000},
    ])
    assert [f.filename for f in files] == ["x-Q4_K_M.gguf"]


# ── launch flags ─────────────────────────────────────────────────────────────
def test_cpu_only_machine_gets_no_offload():
    plan = ll.plan_model(_system(ram=16, avail=12), FILES)
    assert ll.plan_gpu_layers(_system(ram=16, avail=12), plan) == 0


def test_model_bigger_than_vram_gets_partial_offload():
    spec = next(s for s in ll.MODEL_SPECS if s.id == "llama-3.3-70b")
    f = next(f for f in FILES["unsloth/Llama-3.3-70B-Instruct-GGUF"] if f.filename.endswith("Q4_K_M.gguf"))
    plan = ll.Plan(spec=spec, file=f, quant="Q4_K_M", context=4096, budget_gb=40, fits=True)
    layers = ll.plan_gpu_layers(_system(ram=64, gpu="RTX 4090", vram=24.0, backend="cuda"), plan)
    assert 0 < layers < 40


def test_server_command_carries_model_alias_and_context():
    cmd = ll.build_server_command(
        __import__("pathlib").Path("/x/llama-server.exe"),
        __import__("pathlib").Path("/m/model.gguf"),
        "psd-llama-3.1-8b", 8080, 8192, -1, 16,
    )
    assert cmd[1:3] == ["--model", "/m/model.gguf"]
    assert "--alias" in cmd and "psd-llama-3.1-8b" in cmd
    assert cmd[cmd.index("--ctx-size") + 1] == "8192"
    assert cmd[cmd.index("--n-gpu-layers") + 1] == "-1"
    assert "--no-webui" in cmd


def test_backend_order_prefers_detected_gpu_then_always_ends_on_cpu():
    assert ll.windows_backend_candidates(_system(gpu="RTX 4060", vram=8, backend="cuda")) == ["cuda", "vulkan", "cpu"]
    # A capable GPU the CUDA/ROCm builds do not cover (Intel Arc, say) still
    # gets Vulkan...
    assert ll.windows_backend_candidates(_system(gpu="Intel Arc A770", vram=16, backend="cpu_x86")) == ["vulkan", "cpu"]
    # ...but integrated graphics do not: nothing offloads to 128 MB, and the
    # Vulkan build is a bigger download that can only fail to initialise.
    assert ll.windows_backend_candidates(_system()) == ["cpu"]
    assert ll.windows_backend_candidates(
        _system(gpu="Intel(R) UHD Graphics", vram=0.1, backend="cpu_x86")
    ) == ["cpu"]
    assert ll.windows_backend_candidates(
        _system(gpu="RX 7900 XTX", vram=24, backend="rocm")
    ) == ["rocm", "vulkan", "cpu"]
    assert ll.windows_backend_candidates(_system(arch="arm64")) == ["cpu"]


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
    # An older machine, or one where the WMI probe could not run: fall back to
    # the logical count rather than to something arbitrary.
    assert ll.plan_threads({"cpu_cores": 16, "cpu_physical_cores": 0}) == 16
    assert ll.plan_threads({"cpu_cores": 16}) == 16
    assert ll.plan_threads({}) == 1


# ── integrated graphics are not a GPU ────────────────────────────────────────
def test_integrated_graphics_are_treated_as_no_gpu():
    hw = _system(ram=16, avail=11, gpu="Intel(R) UHD Graphics", vram=0.1, backend="cpu_x86")
    assert ll.has_usable_gpu(hw) is False
    plan = ll.plan_model(hw, FILES)
    assert ll.plan_gpu_layers(hw, plan) == 0
    assert ll.windows_backend_candidates(hw) == ["cpu"]
    # A real GPU still counts.
    assert ll.has_usable_gpu(_system(gpu="RTX 4060", vram=8, backend="cuda")) is True


# ── context window ───────────────────────────────────────────────────────────
def test_context_wins_over_quant_quality():
    """A smaller file running at 16k beats a sharper one stuck at 4k."""
    hw = _system(ram=16, avail=11)
    plan = ll.plan_model(hw, FILES)
    assert plan.context == 16384
    # Q6_K is available and sharper, but it is 0.8 GB heavier, so the largest
    # window it can afford here is 4k - four times smaller than what Q5_K_M
    # reaches. That trade is the whole point of ranking context first.
    spec8b = next(s for s in ll.MODEL_SPECS if s.id == "llama-3.1-8b")
    q6 = next(f for f in FILES["unsloth/Llama-3.1-8B-Instruct-GGUF"] if f.filename.endswith("Q6_K.gguf"))
    assert q6.size_gb > plan.file.size_gb
    assert ll._context_for(spec8b, q6.size_gb, ll.memory_budget_gb(hw)) == 4096
    assert ll._context_for(spec8b, plan.file.size_gb, ll.memory_budget_gb(hw)) == 16384


def test_kv_estimate_scales_with_model_size():
    """The old flat 0.7 GB/8k figure misjudged both ends of the model range."""
    assert ll.plan_context_gb(8192, 8.0) < ll.plan_context_gb(8192, 70.0)
    # Llama-3.1-8B is 32 layers x 8 KV heads x 128 dims x 2 bytes x 2 (K and V)
    # = 128 KB/token, i.e. 1.0 GB per 8k of f16 KV cache - not the flat 0.7 GB
    # the picker used to assume for every model.
    assert 0.9 < ll.plan_context_gb(8192, 8.0, quantized_kv=False) < 1.1
    # ...and the q8_0 cache we actually request is about half of that, which is
    # what buys the jump from a 4k to a 16k window on a 16 GB laptop.
    assert ll.plan_context_gb(8192, 8.0) < 0.6


def test_server_command_quantizes_the_kv_cache():
    cmd = ll.build_server_command(
        __import__("pathlib").Path("/x/llama-server.exe"),
        __import__("pathlib").Path("/m/model.gguf"),
        "psd-llama-3.1-8b", 8080, 16384, 0, 6,
    )
    assert cmd[cmd.index("--flash-attn") + 1] == "on"
    assert cmd[cmd.index("--cache-type-k") + 1] == "q8_0"
    assert cmd[cmd.index("--cache-type-v") + 1] == "q8_0"
    # Opting out drops all four flags rather than passing empty values.
    plain = ll.build_server_command(
        __import__("pathlib").Path("/x/llama-server.exe"),
        __import__("pathlib").Path("/m/model.gguf"),
        "psd-llama-3.1-8b", 8080, 16384, 0, 6, quantized_kv=False,
    )
    assert "--flash-attn" not in plain and "--cache-type-k" not in plain


def test_one_user_means_one_slot():
    """Splitting the window across slots would multiply the KV cache for nobody."""
    hw = _system(ram=16, avail=11)
    plan = ll.plan_model(hw, FILES)
    assert ll.plan_parallel(plan, hw) == 1
    # Plenty of VRAM and a comfortable fit: two slots is affordable.
    rich = _system(ram=64, avail=50, gpu="RTX 4090", vram=24.0, backend="cuda")
    assert ll.plan_parallel(ll.plan_model(rich, FILES), rich) == 2


def test_i7_13620h_16gb_laptop_gets_the_tuned_plan():
    """End-to-end golden check for the machine run.bat actually ships to."""
    hw = {
        "cpu_cores": 16, "cpu_physical_cores": 10, "cpu_arch": "x86_64",
        "gpu_name": "Intel(R) UHD Graphics", "gpu_vram_gb": 0.1, "has_gpu": True,
        "backend": "cpu_x86", "total_ram_gb": 15.7, "available_ram_gb": 11.0,
    }
    plan = ll.plan_model(hw, FILES)
    assert (plan.spec.id, plan.quant, plan.context) == ("llama-3.1-8b", "Q5_K_M", 16384)
    assert plan.file.size_gb + ll.plan_context_gb(plan.context, plan.spec.params_b) <= ll.memory_budget_gb(hw)
    cmd = ll.build_server_command(
        __import__("pathlib").Path("llama-server.exe"), __import__("pathlib").Path("m.gguf"),
        "psd", 8080, plan.context, ll.plan_gpu_layers(hw, plan), ll.plan_threads(hw),
        parallel=ll.plan_parallel(plan, hw),
    )
    assert cmd[cmd.index("--threads") + 1] == "6"       # not 16 - no E-cores
    assert cmd[cmd.index("--ctx-size") + 1] == "16384"  # not 4096
    assert cmd[cmd.index("--n-gpu-layers") + 1] == "0"
    assert cmd[cmd.index("--parallel") + 1] == "1"


# ── release picking (real ggml-org asset names) ──────────────────────────────
RELEASES = [
    # The semver "latest release" only carries a pointer file — no binaries.
    {"tag_name": "v0.4.1", "prerelease": False,
     "assets": [{"name": "nightly-tag.txt", "browser_download_url": "u0"}]},
    {"tag_name": "b10992", "assets": [
        {"name": "llama-b10992-bin-win-cpu-x64.zip", "browser_download_url": "u1"}]},
    {"tag_name": "b10993", "assets": [
        {"name": "llama-b10993-bin-win-cpu-x64.zip", "browser_download_url": "u2"},
        {"name": "llama-b10993-bin-win-vulkan-x64.zip", "browser_download_url": "u3"},
        {"name": "llama-b10993-bin-win-cuda-12.4-x64.zip", "browser_download_url": "u4"},
        {"name": "llama-b10993-bin-win-rocm-10.0-x64.zip", "browser_download_url": "u5"},
        {"name": "cudart-llama-bin-win-cuda-12.4-x64.zip", "browser_download_url": "u6"},
        {"name": "llama-b10993-bin-ubuntu-x64.tar.gz", "browser_download_url": "u7"},
    ]},
]


def test_picks_newest_build_and_skips_binary_less_release():
    assert ll.pick_llama_release(RELEASES)["tag"] == "b10993"


def test_semver_only_releases_are_ignored():
    assert ll.pick_llama_release(RELEASES[:1]) is None


@pytest.mark.parametrize("kind,url", [
    ("cpu", "u2"), ("vulkan", "u3"), ("cuda", "u4"), ("rocm", "u5"),
])
def test_match_release_asset(kind, url):
    picked = ll.pick_llama_release(RELEASES)
    asset = ll.match_release_asset(picked["assets"], picked["tag"], kind, "x64")
    assert asset["browser_download_url"] == url


def test_match_release_asset_never_grabs_the_cudart_bundle():
    """cudart-*.zip is the CUDA runtime, not a server build."""
    picked = ll.pick_llama_release(RELEASES)
    asset = ll.match_release_asset(picked["assets"], picked["tag"], "cuda", "x64")
    assert not asset["name"].startswith("cudart-")


def test_backend_detection_reads_the_import_table():
    exe = __import__("pathlib").Path("/tmp/_psd_fake_llama_server.exe")
    exe.write_bytes(b"MZ\x90\x00" + b"\x00" * 64 + b"ggml-vulkan.dll\x00ggml-cuda.dll")
    try:
        assert ll.detect_llama_backends(exe) == ["vulkan", "cuda", "cpu"]
    finally:
        exe.unlink(missing_ok=True)
    assert ll.detect_llama_backends(__import__("pathlib").Path("/tmp/does-not-exist")) == ["cpu"]


# ── setup handshake used by run.bat ──────────────────────────────────────────
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
    endpoint_id = ll.register_endpoint("http://127.0.0.1:8080/v1", "psd-llama-3.1-8b", plan)

    from core.database import ModelEndpoint, SessionLocal
    db = SessionLocal()
    try:
        row = db.query(ModelEndpoint).filter(ModelEndpoint.id == endpoint_id).first()
        assert row is not None
        assert row.base_url == "http://127.0.0.1:8080/v1"
        assert row.is_enabled is True
        assert row.endpoint_kind == "local"
        assert json.loads(row.cached_models) == ["psd-llama-3.1-8b"]
    finally:
        db.close()

    assert ll.set_default_model(endpoint_id, "psd-llama-3.1-8b") is True
    settings = isolated_settings.load_settings()
    assert settings["default_endpoint_id"] == endpoint_id
    assert settings["default_model"] == "psd-llama-3.1-8b"
    # Without this a non-admin's composer never resolves the global default.
    assert settings["share_defaults_with_users"] is True


def test_existing_default_is_not_clobbered(isolated_settings):
    isolated_settings.save_settings({
        **isolated_settings.load_settings(),
        "default_endpoint_id": "openrouter", "default_model": "gpt-x",
    })
    plan = ll.plan_model(_system(ram=16, avail=12), FILES)
    endpoint_id = ll.register_endpoint("http://127.0.0.1:8080/v1", "psd-llama-3.1-8b", plan)
    assert ll.set_default_model(endpoint_id, "psd-llama-3.1-8b") is False
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

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            if self.path == "/health":
                self.send_response(200); self.send_header("Content-Length", "2")
                self.end_headers(); self.wfile.write(b"{}")
            elif self.path == "/v1/models":
                body = b'{"data":[{"id":"psd-llama-3.1-8b"}]}'
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

    assert result["model_id"] == "psd-llama-3.1-8b"
    assert result.get("reused") is True
    assert result["proc"] is None
    assert started, "installer still runs (it resolves the binary), but no server is spawned"
