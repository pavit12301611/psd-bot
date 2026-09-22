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
    """Full turn over fakes: plan → parallel specialist steps → critic → answer."""
    roster = _team()
    monkeypatch.setattr(swarm, "build_roster", lambda owner="": roster)
    monkeypatch.setattr(swarm, "search_knowledge", lambda q, owner="", limit=6: [])

    async def fake_chat(url, model, messages, timeout, temperature=0.4, api_key=""):
        prompt = messages[-1]["content"]
        if "ONLY JSON" in prompt:
            return ('{"steps":[{"kind":"coding","task":"write it"},{"kind":"reasoning","task":"check it"}]}', {})
        if "DRAFT ANSWER" in prompt:
            return ("KEEP", {})
        if "durable facts" in prompt:
            return ("NONE", {})
        return ("The final answer", {"completion_tokens": 50})

    async def fake_stream(url, model, messages, timeout, temperature=0.5, api_key=""):
        for piece in ["unused"]:
            yield piece

    async def fake_run_step(step, index, workers, knowledge, internet):
        w = swarm.route_worker(workers, step["kind"])
        yield {"type": "step_delta", "index": index, "delta": "working"}
        yield {"type": "step_done", "index": index, "kind": step["kind"], "task": step["task"],
               "worker": w.label, "worker_spec": w.spec_id, "ok": True,
               "text": f"{w.label} report", "elapsed_s": 0.1, "tps": 99.0, "error": "",
               "web_sources": None}

    monkeypatch.setattr(swarm, "_chat", fake_chat)
    monkeypatch.setattr(swarm, "_chat_stream", fake_stream)
    monkeypatch.setattr(swarm, "_run_step", fake_run_step)

    events = [e async for e in swarm.orchestrate("build a parser and verify it")]
    kinds = [e["type"] for e in events]
    assert kinds[0] == "phase" and "plan" in kinds and "synth_delta" in kinds
    assert "step_delta" in kinds  # worker text streams live
    # verify pass ran: plan → synthesize → verify → chunked answer
    phases = [e["phase"] for e in events if e["type"] == "phase"]
    assert phases == ["plan", "synthesize", "verify"], phases
    final = next(e for e in events if e["type"] == "final")
    assert final["text"] == "The final answer"
    plan = next(e for e in events if e["type"] == "plan")
    assert plan["steps"][0]["kind"] == "coding"
    # both specialists actually ran (labels in the fixture are the spec ids)
    done = [e for e in events if e["type"] == "step_done"]
    assert {e["worker"] for e in done} == {"qwen3-coder-next", "qwen3.5-27b"}


@pytest.mark.anyio
async def test_critic_replaces_a_bad_draft(monkeypatch):
    """verify on + a critic that flags problems → the corrected answer ships."""
    roster = _team()
    monkeypatch.setattr(swarm, "build_roster", lambda owner="": roster)
    monkeypatch.setattr(swarm, "search_knowledge", lambda q, owner="", limit=6: [])

    async def fake_chat(url, model, messages, timeout, temperature=0.4, api_key=""):
        prompt = messages[-1]["content"]
        if "ONLY JSON" in prompt:
            return ('{"steps":[{"kind":"general","task":"do it"}]}', {})
        if "DRAFT ANSWER" in prompt:
            return ("CORRECTED: the parser must handle UTF-8, here is the full fix…", {})
        if "durable facts" in prompt:
            return ("NONE", {})
        return ("draft with a factual error", {})

    async def fake_step(step, index, workers, knowledge, internet):
        yield {"type": "step_done", "index": index, "kind": step["kind"], "task": step["task"],
               "worker": "w", "worker_spec": "w", "ok": True, "text": "report",
               "elapsed_s": 0.1, "tps": 0.0, "error": "", "web_sources": None}

    monkeypatch.setattr(swarm, "_chat", fake_chat)
    monkeypatch.setattr(swarm, "_run_step", fake_step)

    events = [e async for e in swarm.orchestrate("explain x")]
    final = next(e for e in events if e["type"] == "final")
    assert final["text"].startswith("CORRECTED")
    verify_ev = next(e for e in events if e.get("phase") == "verify")
    assert verify_ev["critic"]  # the GUI can name the critic


@pytest.mark.anyio
async def test_step_failover_moves_to_the_next_specialist(monkeypatch):
    """First-choice worker crashes mid-step → step_retry, then the fallback ships."""
    roster = _team()
    monkeypatch.setattr(swarm, "search_knowledge", lambda q, owner="", limit=6: [])

    calls = []

    async def fake_stream(url, model, messages, timeout, temperature=0.5, api_key=""):
        calls.append(model)
        if "qwen3-coder-next" in model:
            raise RuntimeError("connection reset")
        for piece in ["fallback ", "report"]:
            yield piece

    monkeypatch.setattr(swarm, "_chat_stream", fake_stream)

    events = []
    async for ev in swarm._run_step({"kind": "coding", "task": "write it"}, 0, roster, [], False):
        events.append(ev)

    kinds = [e["type"] for e in events]
    assert "step_retry" in kinds
    retry = next(e for e in events if e["type"] == "step_retry")
    assert retry["worker"] == "qwen3.5-27b"  # the next-best coding candidate
    done = next(e for e in events if e["type"] == "step_done")
    assert done["ok"] and done["worker"] == "qwen3.5-27b"


@pytest.mark.anyio
async def test_step_reports_failure_only_after_the_fallback_also_fails(monkeypatch):
    async def failing_stream(url, model, messages, timeout, temperature=0.5, api_key=""):
        raise RuntimeError("down")
        yield  # pragma: no cover

    monkeypatch.setattr(swarm, "_chat_stream", failing_stream)
    events = [e async for e in swarm._run_step({"kind": "coding", "task": "t"}, 0, _team(), [], False)]
    done = next(e for e in events if e["type"] == "step_done")
    assert not done["ok"] and "down" in done["error"]


def test_status_reports_the_team(monkeypatch):
    monkeypatch.setattr(swarm, "_group_state", lambda: {"count": 6, "resident_gb": 20.0, "profile": "balanced"})
    monkeypatch.setattr(swarm, "build_roster", lambda owner="": _team())
    st = swarm.status()
    assert st["available"] is True
    assert st["manager"]["spec_id"] == "gpt-oss-120b"
    assert len(st["workers"]) == 6
    assert st["group"]["count"] == 6


# ── cloud assist (opt-in remote specialists) ─────────────────────────────────
class _FakeRow:
    def __init__(self, row_id, name, base_url, models, api_key="", hidden="[]", model_type="llm"):
        self.id = row_id
        self.name = name
        self.base_url = base_url
        self.cached_models = json.dumps(models)
        self.hidden_models = hidden
        self.model_type = model_type
        self.api_key = api_key
        self.is_enabled = True
        self.owner = None


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *_):
        return self

    def all(self):
        return self._rows


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    def query(self, _model):
        return _FakeQuery(self._rows)

    def close(self):
        pass


@pytest.fixture()
def _fake_endpoint_db(monkeypatch):
    """A stub core.database with one local + one huge remote endpoint."""
    rows = [
        _FakeRow("local-llama-qwen3.5-9b", "psd.ai Local Llama · Qwen3.5 9B",
                 "http://127.0.0.1:8080/v1", ["psd-qwen3.5-9b"]),
        _FakeRow("openrouter", "OpenRouter", "https://openrouter.ai/api/v1",
                 ["qwen/qwen3-235b-a22b", "openai/gpt-5.2-codex"],
                 api_key="sk-SUPER-SECRET"),
    ]

    import sys as _sys
    import types as _types

    class _FakeColumn:
        def like(self, *_):
            return None

        def __eq__(self, _other):
            return True

    stub = _types.ModuleType("core.database")
    stub.ModelEndpoint = type("ModelEndpoint", (), {
        "id": _FakeColumn(), "is_enabled": True, "model_type": "llm", "owner": None,
    })
    stub.SessionLocal = lambda: _FakeDB(rows)
    monkeypatch.setitem(_sys.modules, "core.database", stub)

    import src.auth_helpers as auth_helpers
    monkeypatch.setattr(auth_helpers, "owner_filter", lambda q, m, u: q)
    return rows


def test_cloud_off_by_default_and_never_without_opt_in(monkeypatch, _fake_endpoint_db):
    monkeypatch.setattr(swarm, "_group_state", lambda: {})
    swarm.save_swarm_settings({"cloud_workers": False})
    roster = swarm.build_roster()
    assert [w.remote for w in roster] == [False]  # only the local model


def test_cloud_opt_in_adds_remote_specialists_but_local_manages(monkeypatch, _fake_endpoint_db):
    monkeypatch.setattr(swarm, "_group_state", lambda: {})
    swarm.save_swarm_settings({"cloud_workers": True})
    roster = swarm.build_roster()
    remotes = [w for w in roster if w.remote]
    assert remotes, "remote specialists should join on opt-in"
    # The MANAGER is always the strongest LOCAL model, never the remote giant.
    manager = next(w for w in roster if w.is_manager)
    assert not manager.remote
    assert manager.spec_id == "qwen3.5-9b"
    # Coding tier guessed from the name; power parsed from "235b" (a dense
    # id without a B-suffix, like gpt-5.2-codex, legitimately parses to 0).
    coder = next(w for w in remotes if "codex" in w.model_id)
    assert coder.tier == "coding"
    big = next(w for w in remotes if "235b" in w.model_id)
    assert big.params_b == 235.0
    assert big.power > manager.power  # stronger, yet still only a worker


def test_cloud_api_keys_never_leak_into_status(monkeypatch, _fake_endpoint_db):
    monkeypatch.setattr(swarm, "_group_state", lambda: {})
    swarm.save_swarm_settings({"cloud_workers": True})
    st = swarm.status()
    assert "sk-SUPER-SECRET" not in json.dumps(st)
    for worker in st["workers"]:
        assert "api_key" not in worker


def test_params_from_name_parses_dense_and_moe_ids():
    assert swarm._params_from_name("qwen3-235b-a22b") == 235.0
    assert swarm._params_from_name("mixtral-8x7b-instruct") == 56.0
    assert swarm._params_from_name("gpt-5.2") == 0.0


# ── failover candidates ──────────────────────────────────────────────────────
def test_candidate_workers_orders_specialists_and_never_the_manager():
    picks = swarm.candidate_workers(_team(), "coding", limit=3)
    assert [w.tier for w in picks[:1]] == ["coding"]
    assert all(not w.is_manager for w in picks)
    assert len(picks) == 3


def test_pick_critic_prefers_strongest_thinking_non_manager():
    team = _team()
    critic = swarm.pick_critic(team, team[0])
    assert not critic.is_manager
    assert critic.thinking
    # A team with no thinking worker falls back to the strongest non-manager.
    plain = [_worker("boss", params=30.0, manager=True), _worker("helper", params=8.0)]
    assert swarm.pick_critic(plain, plain[0]) is plain[1]
