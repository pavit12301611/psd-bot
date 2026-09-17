"""GET /api/hwfit/models — the `access` license filter, end-to-end.

Drives the real route handler (same pattern as
test_hwfit_gpu_count_nonnumeric.py) with hardware detection replaced by a
fixed 24 GB CUDA box so assertions are deterministic on any machine.

Pins the API contract the Cookbook frontend relies on:
- access=unrestricted / access=restricted return only rows of that class,
  each carrying access + license metadata;
- an unknown access value degrades to no filter (never a 400/500);
- access composes with use_case=coding;
- every row (fit or too-tight) carries the access/license keys.
"""
import pytest

from routes.hwfit_routes import setup_hwfit_routes


def _fake_system(**overrides):
    base = {
        "has_gpu": True,
        "backend": "cuda",
        "gpu_name": "NVIDIA GeForce RTX 4090",
        "gpu_vram_gb": 24.0,
        "gpu_count": 1,
        "gpus": [{"name": "NVIDIA GeForce RTX 4090", "vram_gb": 24.0}],
        "gpu_groups": [{"name": "NVIDIA GeForce RTX 4090", "vram_each": 24.0, "count": 1}],
        "available_ram_gb": 64.0,
        "total_ram_gb": 64.0,
        "cpu_name": "Test CPU",
        "platform": "linux",
        "error": None,
    }
    base.update(overrides)
    return base


@pytest.fixture
def models_handler(monkeypatch):
    import services.hwfit.hardware as hw

    monkeypatch.setattr(hw, "detect_system", lambda **kwargs: _fake_system())
    router = setup_hwfit_routes()
    for route in router.routes:
        if getattr(route, "path", "").endswith("/models") and "GET" in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError("hwfit /models route not found")


def test_access_unrestricted_returns_only_open_license_rows(models_handler):
    result = models_handler(access="unrestricted", limit=100)
    assert not result.get("error")
    models = result["models"]
    assert models, "expected unrestricted models on a 24 GB box"
    for row in models:
        assert row["access"] == "unrestricted", row["name"]
        assert row.get("license"), f"{row['name']} missing license label"


def test_access_restricted_returns_only_conditional_rows(models_handler):
    result = models_handler(access="restricted", limit=100)
    assert not result.get("error")
    models = result["models"]
    assert models, "expected restricted models on a 24 GB box"
    for row in models:
        assert row["access"] == "restricted", row["name"]


def test_access_partition_never_overlaps(models_handler):
    open_rows = models_handler(access="unrestricted", limit=100)["models"]
    restricted_rows = models_handler(access="restricted", limit=100)["models"]
    assert not ({r["name"] for r in open_rows} & {r["name"] for r in restricted_rows})


def test_unknown_access_value_degrades_to_no_filter(models_handler):
    everything = models_handler(access="nonsense", limit=100)["models"]
    baseline = models_handler(limit=100)["models"]
    assert [r["name"] for r in everything] == [r["name"] for r in baseline]


def test_access_composes_with_coding_use_case(models_handler):
    result = models_handler(use_case="coding", access="unrestricted", limit=100)
    models = result["models"]
    assert models, "expected unrestricted coding models"
    for row in models:
        assert row["use_case"] == "coding", row["name"]
        assert row["access"] == "unrestricted", row["name"]
    names = {r["name"] for r in models}
    assert "Qwen/Qwen2.5-Coder-14B-Instruct" in names


def test_every_row_carries_access_keys(models_handler):
    models = models_handler(limit=100)["models"]
    assert models
    for row in models:
        assert "access" in row and "license" in row, row["name"]
        assert row["access"] in ("unrestricted", "restricted", ""), row["name"]


def test_limit_still_respected_with_access(models_handler):
    result = models_handler(access="unrestricted", limit=5)
    assert len(result["models"]) <= 5
