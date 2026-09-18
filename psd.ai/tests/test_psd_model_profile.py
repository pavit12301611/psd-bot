"""Regression coverage for the PSD identity and local launcher preference."""

from scripts.local_llama import GGUFFile, MODEL_SPECS, plan_model, plan_model_group
from src.psd_model import PSD_MODEL_ID, is_psd_model, is_psd_spec


def _catalogue_with_one_q4_file_per_spec():
    files = {}
    for spec in MODEL_SPECS:
        files[spec.repos[0]] = [
            GGUFFile(
                repo=spec.repos[0],
                path=f"{spec.id}-Q4_K_M.gguf",
                size=max(1, int(spec.params_b * 0.65 * 1024**3)),
            )
        ]
    return files


def test_psd_aliases_are_stable():
    assert is_psd_model(PSD_MODEL_ID)
    assert is_psd_model("local/psd-coder-7b")
    assert not is_psd_model("llama-3.2-3b")
    assert is_psd_spec("psd-coder-0.5b")


def test_single_launcher_prefers_psd_when_available():
    plan = plan_model(
        {"total_ram_gb": 8, "available_ram_gb": 8, "has_gpu": False},
        _catalogue_with_one_q4_file_per_spec(),
    )
    assert plan is not None
    assert is_psd_spec(plan.spec.id)


def test_group_keeps_psd_first_on_small_hardware():
    group = plan_model_group(
        {"total_ram_gb": 4, "available_ram_gb": 4, "has_gpu": False},
        _catalogue_with_one_q4_file_per_spec(),
    )
    assert group.plans
    assert group.plans[0].spec.id == "psd-coder-0.5b"
