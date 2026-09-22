"""src/swarm.py — the whole local model group working as one team.

The swarm only exists if the ROUTING is right: the strongest model manages,
every step lands on the worker whose specialty fits, and a step never waits
on a brilliant-but-slow model when a strong-and-quick specialist exists.
These tests keep that contract honest with no network and no database.
"""

import json
import types

import pytest

from src import swarm


@pytest.fixture(autouse=True)
def _isolate_files(tmp_path, monkeypatch):
    """Point every swarm store at a throwaway dir."""
    monkeypatch.setattr(swarm, "_knowledge_file", lambda: tmp_path / "swarm_knowledge.json")
    monkeypatch.setattr(swarm, "_settings_file", lambda: tmp_path / "swarm_settings.json")
    monkeypatch.setattr(swarm, "_speed_file", lambda: tmp_path / "swarm_speed.json")
    yield


def _worker(spec_id, *, tier="general", thinking=False, vision=False, params=8.0,
            active=0.0, role=None, measured=0.0, manager=False, base="http://127.0.0.1:9000/v1"):
    w = swarm.SwarmWorker(
        spec_id=spec_id, label=spec_id, model_id=f"psd-{spec_id}", base_url=base,
        endpoint_id=f"local-llama-{spec_id}", tier=tier, thinking=thinking, vision=vision,
        params_b=params, active_params_b=active, moe=active > 0, context=32768,
        role=role or ("Manager" if manager else swarm.ROLE_BY_TIER.get(tier, "Utility")),
        measured_tps=measured, is_manager=manager,
    )
    return w


def _team():
    """A realistic 6-model resident group, strongest first."""
    manager = _worker("gpt-oss-120b", params=117.0, active=5.1, manager=True)
    coder = _worker("qwen3-coder-next", tier="coding", params=80.0, active=3.0)
    analyst = _worker("qwen3.5-27b", tier="reasoning", thinking=True, params=27.0)
    fast = _worker("qwen3.5-4b", params=4.0, measured=30.0)
    tiny = _worker("qwen3-1.7b", params=1.7, measured=45.0)
    eye = _worker("gemma-4-12b", vision=True, params=12.0)
    return [manager, coder, analyst, fast, tiny, eye]


# ── roster ───────────────────────────────────────────────────────────────────
def test_manager_is_the_strongest_model(monkeypatch):
    monkeypatch.setattr(swarm, "_group_state", lambda: {})
    team = _team()
    assert team[0].is_manager
    assert team[0].spec_id == "gpt-oss-120b"
    assert team[0].role == "Manager"
    assert not any(w.is_manager for w in team[1:])


def test_roster_reads_the_group_state_and_attaches_specialties(monkeypatch):
    specs = {
        "qwen3-coder-next": types.SimpleNamespace(
            id="qwen3-coder-next", label="Qwen3-Coder-Next", alias="psd-qwen3-coder-next",
            tier="coding", thinking=False, vision=False, params_b=80.0,
            active_params_b=3.0, moe=True, max_context=262144),
        "qwen3-1.7b": types.SimpleNamespace(
            id="qwen3-1.7b", label="Qwen3 1.7B", alias="psd-qwen3-1.7b",
            tier="general", thinking=False, vision=False, params_b=1.7,
            active_params_b=0.0, moe=False, max_context=32768),
    }
    monkeypatch.setattr(swarm, "_spec_by_id", lambda sid: specs.get(sid))
    monkeypatch.setattr(swarm, "_group_state", lambda: {
        "count": 2,
        "models": [
            {"spec_id": "qwen3-coder-next", "label": "Qwen3-Coder-Next", "model_id": "psd-qwen3-coder-next",
             "base_url": "http://127.0.0.1:8080/v1", "endpoint_id": "local-llama-qwen3-coder-next",
             "context": 16384, "quant": "Q4_K_M", "port": 8080, "vision": False},
            {"spec_id": "qwen3-1.7b", "label": "Qwen3 1.7B", "model_id": "psd-qwen3-1.7b",
             "base_url": "http://127.0.0.1:8081/v1", "endpoint_id": "local-llama-qwen3-1.7b",
             "context": 32768, "quant": "Q8_0", "port": 8081, "vision": False},
        ],
    })
    roster = swarm.build_roster()
    assert [w.spec_id for w in roster] == ["qwen3-coder-next", "qwen3-1.7b"]
    assert roster[0].is_manager and roster[0].role == "Manager"
    assert roster[1].role == "Utility"
    assert roster[1].model_id == "psd-qwen3-1.7b"
    assert roster[0].moe and roster[1].moe is False


# ── specialist routing ───────────────────────────────────────────────────────
def test_coding_steps_go_to_the_coding_specialist():
    pick = swarm.route_worker(_team(), "coding")
    assert pick is not None and pick.tier == "coding"


def test_reasoning_steps_go_to_the_thinking_specialist():
    pick = swarm.route_worker(_team(), "reasoning")
    assert pick is not None and pick.thinking


def test_quick_steps_go_to_the_smallest_fastest_worker():
    pick = swarm.route_worker(_team(), "fast")
    assert pick is not None and pick.spec_id == "qwen3-1.7b"


def test_speed_can_outrank_power_for_step_work():
    # A measured-fast 9B beats a measured-crawling 27B for a general step.
    team = [
        _worker("big", params=27.0, manager=True),
        _worker("slow27", params=27.0, measured=3.0),
        _worker("quick9", params=9.0, measured=25.0),
    ]
    pick = swarm.route_worker(team, "general")
    assert pick is not None and pick.spec_id == "quick9"


def test_vision_steps_require_a_vision_worker():
    team = [w for w in _team() if w.spec_id != "gemma-4-12b"]
    pick = swarm.route_worker(team, "vision")
    # No vision worker left → it degrades to the best available, never None.
    assert pick is not None
    pick2 = swarm.route_worker(_team(), "vision")
    assert pick2 is not None and pick2.vision


def test_manager_never_takes_step_work_when_workers_exist():
    for kind in swarm.STEP_KINDS:
        pick = swarm.route_worker(_team(), kind)
        assert pick is not None and not pick.is_manager, kind


def test_disabled_workers_are_skipped():
    team = _team()
    for w in team:
        if w.tier == "coding":
            w.enabled = False
    pick = swarm.route_worker(team, "coding")
    assert pick is not None and pick.tier != "coding"


# ── planning ─────────────────────────────────────────────────────────────────
def test_parse_plan_handles_fenced_json():
    raw = 'Sure!\n```json\n{"steps":[{"kind":"coding","task":"write the parser"},{"kind":"fast","task":"list names"}]}\n```\nDone.'
    steps = swarm.parse_plan(raw)
    assert [s["kind"] for s in steps] == ["coding", "fast"]
    assert steps[0]["task"] == "write the parser"


def test_parse_plan_handles_bare_json_and_unknown_kinds():
    raw = '{"steps":[{"kind":"welding","task":"x"},{"kind":"research","task":"y"}]}'
    steps = swarm.parse_plan(raw)
    assert [s["kind"] for s in steps] == ["general", "research"]


def test_parse_plan_garbage_returns_empty_so_fallback_kicks_in():
    assert swarm.parse_plan("I will just do it myself, no JSON today.") == []


def test_parse_plan_caps_steps():
    raw = json.dumps({"steps": [{"kind": "general", "task": f"step {i}"} for i in range(10)]})
    assert len(swarm.parse_plan(raw, max_steps=4)) == 4


def test_fallback_plan_adds_research_for_web_intents():
    plain = swarm.fallback_plan("refactor my notes function")
    assert [s["kind"] for s in plain] == ["general"]
    web = swarm.fallback_plan("what is the latest Fedora release?")
    assert web[0]["kind"] == "research"
    assert web[-1]["kind"] == "general"


# ── speed memory ─────────────────────────────────────────────────────────────
def test_record_speed_blends_an_ewma():
    assert swarm.record_speed("m", 100, 10.0) == pytest.approx(10.0, abs=0.1)
    second = swarm.record_speed("m", 30, 1.0)  # 30 tok/s measured
    assert second == pytest.approx(0.7 * 10.0 + 0.3 * 30.0, abs=0.1)
    assert swarm.record_speed("m", 0, 5.0) is None  # no tokens → no sample


def test_worker_speed_prefers_measurement_over_estimate():
    w = _worker("m", params=27.0, measured=0.0)
    est = w.speed_tps
    w.measured_tps = 11.5
    assert w.speed_tps == 11.5
    assert est > 0


# ── knowledge ────────────────────────────────────────────────────────────────
def test_knowledge_add_search_and_dedupe():
    assert swarm.add_knowledge("The user's project is called psd.ai and runs on Fedora.")
    dup = swarm.add_knowledge("psd.ai")
    assert dup is None  # subset of what is already known
    hits = swarm.search_knowledge("what is the user's project called?")
    assert hits and "psd.ai" in hits[0]["text"]


def test_knowledge_is_owner_scoped():
    swarm.add_knowledge("Aarav prefers dark themes.", owner="aarav")
    assert len(swarm.load_knowledge("aarav")) == 1
    assert len(swarm.load_knowledge("someone-else")) == 0
    assert len(swarm.load_knowledge()) == 1  # admin/global view sees it


def test_knowledge_cap_keeps_the_store_bounded():
    for i in range(swarm.MAX_KNOWLEDGE_ENTRIES + 40):
        swarm.add_knowledge(f"unique fact number {i} about topic {i % 7}")
    assert len(swarm.load_knowledge()) == swarm.MAX_KNOWLEDGE_ENTRIES


def test_knowledge_delete_and_clear():
    item = swarm.add_knowledge("Stable fact worth keeping around.")
    assert swarm.delete_knowledge(item["id"])
    assert not swarm.delete_knowledge(item["id"])
    swarm.add_knowledge("Another fact entirely.")
    assert swarm.clear_knowledge() == 1


# ── settings ─────────────────────────────────────────────────────────────────
def test_settings_round_trip_and_clamping():
    updated = swarm.save_swarm_settings({"internet": False, "max_steps": 99})
    assert updated["internet"] is False
    assert updated["max_steps"] == 6  # clamped to the plan budget
    assert swarm.load_swarm_settings()["internet"] is False


# ── orchestration events ─────────────────────────────────────────────────────
class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.text = json.dumps(payload)

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


@pytest.mark.anyio
async def test_orchestrate_runs_steps_and_synthesizes(monkeypatch):
    """Full turn over fakes: plan → parallel specialist steps → streamed answer."""
    roster = _team()
    monkeypatch.setattr(swarm, "build_roster", lambda owner="": roster)
    monkeypatch.setattr(swarm, "search_knowledge", lambda q, owner="", limit=6: [])

    async def fake_chat(url, model, messages, timeout, temperature=0.4):
        if "steps" in messages[-1]["content"]:
            return ('{"steps":[{"kind":"coding","task":"write it"},{"kind":"reasoning","task":"check it"}]}', {})
        return ("ok", {"completion_tokens": 50})

    async def fake_stream(url, model, messages, timeout, temperature=0.5):
        for piece in ["The ", "final ", "answer"]:
            yield piece

    async def fake_run_step(step, index, workers, knowledge, internet):
        w = swarm.route_worker(workers, step["kind"])
        return swarm.StepResult(index, step["kind"], step["task"], w.label, w.spec_id,
                                True, text=f"{w.label} report"), None

    monkeypatch.setattr(swarm, "_chat", fake_chat)
    monkeypatch.setattr(swarm, "_chat_stream", fake_stream)
    monkeypatch.setattr(swarm, "_run_step", fake_run_step)

    events = [e async for e in swarm.orchestrate("build a parser and verify it")]
    kinds = [e["type"] for e in events]
    assert kinds[0] == "phase" and "plan" in kinds and "synth_delta" in kinds
    final = next(e for e in events if e["type"] == "final")
    assert final["text"] == "The final answer"
    plan = next(e for e in events if e["type"] == "plan")
    assert plan["steps"][0]["kind"] == "coding"
    # both specialists actually ran (labels in the fixture are the spec ids)
    done = [e for e in events if e["type"] == "step_done"]
    assert {e["worker"] for e in done} == {"qwen3-coder-next", "qwen3.5-27b"}


def test_status_reports_the_team(monkeypatch):
    monkeypatch.setattr(swarm, "_group_state", lambda: {"count": 6, "resident_gb": 20.0, "profile": "balanced"})
    monkeypatch.setattr(swarm, "build_roster", lambda owner="": _team())
    st = swarm.status()
    assert st["available"] is True
    assert st["manager"]["spec_id"] == "gpt-oss-120b"
    assert len(st["workers"]) == 6
    assert st["group"]["count"] == 6
