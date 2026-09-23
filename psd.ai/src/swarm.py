# src/swarm.py
"""psd.ai Swarm — every local model working together under one manager.

The local model group already runs 3–6 llama-server processes side by side.
Until now the app used exactly one of them at a time. The Swarm turns the
whole group into a team:

* the **strongest** model becomes the MANAGER. It never answers alone; it
  breaks the request into specialist steps and writes the final answer from
  the reports.
* every other model is a WORKER, and each worker only gets work it is
  actually good at — the coding-tuned model writes code, the
  reasoning-tuned ("thinking") model analyses, the smallest model does the
  quick lookups, the vision model handles images. That is the whole point:
  ``jo model jis cheez mein acha hai, usi pe kaam karta hai``.
* routing scores every worker per step on SPECIALTY MATCH first, then POWER
  (total params) and SPEED (measured tok/s, falling back to an
  active-params estimate) — so picks are both quick and strong.
* independent steps run in PARALLEL (asyncio), so a 6-model group answers
  like one big model without the latency of six sequential calls.
* FULL INTERNET ACCESS: research steps search the live web through the
  app's own search service and fetch real pages; the manager cites them.
* the swarm LEARNS: durable facts from every swarm turn (or any URL you
  hand it) are stored in a small local knowledge file and injected into
  future swarm prompts.

Everything here is local-first: the swarm talks to the same
OpenAI-compatible ``/v1`` endpoints the app already registered, needs no
extra service, and degrades gracefully — if a worker dies the step is
reported as failed and the manager still answers from the rest.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Storage ──────────────────────────────────────────────────────────────────
# Knowledge + settings live under the app's single DATA_DIR (src/constants.py
# is the only place PSD_AI_DATA_DIR is read, per the runtime-path contract).

def _data_dir() -> str:
    from src.constants import DATA_DIR
    return DATA_DIR


def _knowledge_file() -> Path:
    return Path(_data_dir()) / "swarm_knowledge.json"


def _settings_file() -> Path:
    return Path(_data_dir()) / "swarm_settings.json"


def _speed_file() -> Path:
    return Path(_data_dir()) / "swarm_speed.json"


# ── Defaults ─────────────────────────────────────────────────────────────────
DEFAULT_SETTINGS: Dict[str, Any] = {
    "internet": True,       # full web access for research steps
    "parallel": True,       # run independent worker steps concurrently
    "auto_learn": True,     # distil durable facts after each swarm turn
    "verify": True,         # a critic pass checks the draft before it ships
    "cloud_workers": False,  # let configured REMOTE endpoints join the team
    "max_steps": 4,         # manager plan budget
    "disabled_workers": [],  # spec_ids the user switched off
}

MAX_KNOWLEDGE_ENTRIES = 300
KNOWLEDGE_INJECT_LIMIT = 6
WORKER_TIMEOUT_S = 150.0
MANAGER_TIMEOUT_S = 240.0


def load_swarm_settings() -> Dict[str, Any]:
    settings = dict(DEFAULT_SETTINGS)
    try:
        raw = json.loads(_settings_file().read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for key in DEFAULT_SETTINGS:
                if key in raw:
                    settings[key] = raw[key]
    except (OSError, ValueError):
        pass
    return settings


def save_swarm_settings(update: Dict[str, Any]) -> Dict[str, Any]:
    settings = load_swarm_settings()
    for key in DEFAULT_SETTINGS:
        if key in update:
            settings[key] = update[key]
    settings["max_steps"] = max(1, min(6, int(settings.get("max_steps") or 4)))
    path = _settings_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    return settings


# ── Speed memory (measured tok/s per spec, EWMA) ─────────────────────────────
def _load_speeds() -> Dict[str, float]:
    try:
        raw = json.loads(_speed_file().read_text(encoding="utf-8"))
        return {k: float(v) for k, v in raw.items() if isinstance(v, (int, float)) and v > 0}
    except (OSError, ValueError):
        return {}


def _save_speeds(speeds: Dict[str, float]) -> None:
    try:
        path = _speed_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(speeds, indent=2), encoding="utf-8")
    except OSError:
        pass


def record_speed(spec_id: str, tokens: int, elapsed_s: float) -> Optional[float]:
    """Blend a fresh tok/s measurement into the per-spec EWMA."""
    if not spec_id or tokens <= 0 or elapsed_s <= 0.05:
        return None
    # Clamp: one absurd sample (clock jitter, a trivial reply) must not
    # dominate the EWMA the router depends on.
    tps = min(tokens / elapsed_s, 400.0)
    speeds = _load_speeds()
    old = speeds.get(spec_id)
    speeds[spec_id] = round(old * 0.7 + tps * 0.3, 2) if old else round(tps, 2)
    _save_speeds(speeds)
    return speeds[spec_id]


# ── Knowledge store ──────────────────────────────────────────────────────────
def _norm_text(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", (text or "").lower()).split()


def load_knowledge(owner: str = "") -> List[Dict[str, Any]]:
    try:
        raw = json.loads(_knowledge_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    if not owner:
        return raw
    # Per-user view: your own entries plus legacy shared ones (no owner).
    return [e for e in raw if isinstance(e, dict) and (not e.get("owner") or e.get("owner") == owner)]


def add_knowledge(text: str, owner: str = "", source: str = "swarm", url: str = "") -> Optional[Dict[str, Any]]:
    text = (text or "").strip()
    if not text:
        return None
    entries = load_knowledge()
    words = set(_norm_text(text))
    # Dedupe: an entry whose words are a superset of the new text already knows it.
    for entry in entries:
        if words and words.issubset(set(_norm_text(entry.get("text", "")))):
            return None
    item = {
        "id": str(uuid.uuid4()),
        "text": text[:600],
        "source": source or "swarm",
        "url": (url or "")[:400],
        "ts": int(time.time()),
    }
    if owner:
        item["owner"] = owner
    entries.append(item)
    if len(entries) > MAX_KNOWLEDGE_ENTRIES:
        entries = entries[-MAX_KNOWLEDGE_ENTRIES:]
    path = _knowledge_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")
    return item


def delete_knowledge(entry_id: str, owner: str = "") -> bool:
    entries = load_knowledge()
    kept = [e for e in entries if e.get("id") != entry_id]
    if len(kept) == len(entries):
        return False
    path = _knowledge_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(kept, indent=2, ensure_ascii=False), encoding="utf-8")
    return True


def clear_knowledge(owner: str = "") -> int:
    entries = load_knowledge()
    if not owner:
        kept: List[Dict[str, Any]] = []
    else:
        kept = [e for e in entries if e.get("owner") and e.get("owner") != owner]
    path = _knowledge_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(kept, indent=2, ensure_ascii=False), encoding="utf-8")
    return len(entries) - len(kept)


def search_knowledge(query: str, owner: str = "", limit: int = KNOWLEDGE_INJECT_LIMIT) -> List[Dict[str, Any]]:
    """Cheapest useful rank: word overlap, newest first on ties."""
    qwords = set(_norm_text(query))
    if not qwords:
        return []
    scored = []
    for entry in load_knowledge(owner):
        ewords = set(_norm_text(entry.get("text", "")))
        overlap = len(qwords & ewords)
        if overlap:
            scored.append((overlap, entry.get("ts", 0), entry))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [e for _, _, e in scored[:limit]]


# ── Worker roster ────────────────────────────────────────────────────────────
@dataclass
class SwarmWorker:
    """One model endpoint (local llama-server or an allowed remote) with its
    specialty attached."""
    spec_id: str
    label: str
    model_id: str
    base_url: str
    endpoint_id: str
    tier: str = "general"        # general | coding | reasoning
    thinking: bool = False       # emits reasoning — the analyst kind
    vision: bool = False         # can see images
    params_b: float = 0.0        # total parameters → POWER
    active_params_b: float = 0.0  # per-token parameters → SPEED
    moe: bool = False
    context: int = 8192
    quant: str = ""
    port: int = 0
    role: str = "Utility"        # friendly specialty label for the GUI
    measured_tps: float = 0.0    # live tok/s EWMA, 0 = not measured yet
    enabled: bool = True
    is_manager: bool = False
    remote: bool = False         # True → a cloud/remote endpoint (cloud assist)
    api_key: str = ""            # NEVER serialized (see to_dict)

    @property
    def est_tps(self) -> float:
        """Estimated tok/s on a mid laptop CPU — the prior before measuring."""
        active = self.active_params_b or self.params_b or 4.0
        return max(2.0, min(80.0, 14.0 / max(active, 0.4)))

    @property
    def speed_tps(self) -> float:
        return self.measured_tps or self.est_tps

    @property
    def power(self) -> float:
        """Comparable 'how smart' score (mirrors local_llama.spec_quality)."""
        if self.moe or self.active_params_b:
            return 0.8 * self.params_b + 0.2 * (self.active_params_b or self.params_b)
        return self.params_b

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("api_key", None)  # credentials never leave the engine
        d["speed_tps"] = round(self.speed_tps, 1)
        d["est_tps"] = round(self.est_tps, 1)
        d["power"] = round(self.power, 1)
        return d


ROLE_BY_TIER = {"coding": "Coder", "reasoning": "Analyst"}


def _group_state() -> Dict[str, Any]:
    """The running local model group's state file (written by scripts/local_llama.py)."""
    try:
        from scripts.local_llama import GROUP_STATE_FILE
        return json.loads(Path(GROUP_STATE_FILE).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _spec_by_id(spec_id: str):
    try:
        from scripts.local_llama import MODEL_SPECS
        return next((s for s in MODEL_SPECS if s.id == spec_id), None)
    except Exception:
        return None


def _params_from_name(name: str) -> float:
    """Best-effort parameter count from a model id ('qwen3-235b-a22b' → 235)."""
    text = (name or "").lower()
    mixture = re.search(r"(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)b", text)
    if mixture:
        try:
            return float(mixture.group(1)) * float(mixture.group(2))
        except ValueError:
            return 0.0
    plain = re.search(r"(\d+(?:\.\d+)?)b", text)
    if plain:
        try:
            return float(plain.group(1))
        except ValueError:
            return 0.0
    return 0.0


def _cloud_workers_from_db(owner: str, speeds: Dict[str, float], disabled: set) -> List[SwarmWorker]:
    """Remote LLM endpoints as optional workers (cloud assist, opt-in).

    Tier is guessed from the model id; power from the name where possible, so
    a huge remote ranks as a specialist, never as the MANAGER while any local
    model is alive (see build_roster — local-first is deliberate).
    """
    try:
        from core.database import ModelEndpoint, SessionLocal
        from src.auth_helpers import owner_filter
        db = SessionLocal()
        try:
            q = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)  # noqa: E712
            rows = owner_filter(q, ModelEndpoint, owner).all()
        finally:
            db.close()
    except Exception as exc:
        logger.warning("swarm cloud roster unavailable: %s", exc)
        return []

    workers: List[SwarmWorker] = []
    for row in rows:
        row_id = getattr(row, "id", "") or ""
        if row_id.startswith("local-llama-"):
            continue  # already covered by the group state / local fallback
        if (getattr(row, "model_type", "llm") or "llm") != "llm":
            continue
        base_url = getattr(row, "base_url", "") or ""
        if not base_url:
            continue
        try:
            hidden = set(json.loads(getattr(row, "hidden_models", None) or "[]"))
        except (ValueError, TypeError):
            hidden = set()
        try:
            models = [m for m in json.loads(getattr(row, "cached_models", None) or "[]") if isinstance(m, str)]
        except (ValueError, TypeError):
            models = []
        models = [m for m in models if m not in hidden][:3]
        if not models:
            continue
        for model in models:
            spec_id = f"remote:{row_id}:{model}"
            lower = model.lower()
            tier = "general"
            if "cod" in lower:
                tier = "coding"
            elif any(tag in lower for tag in ("think", "reason", "r1", "o1", "o3")):
                tier = "reasoning"
            workers.append(SwarmWorker(
                spec_id=spec_id,
                label=model.split("/")[-1][:48],
                model_id=model,
                base_url=base_url,
                endpoint_id=row_id,
                tier=tier,
                thinking=tier == "reasoning",
                vision=any(tag in lower for tag in ("vl", "vision")),
                params_b=_params_from_name(model),
                moe="x" in lower and "b" in lower,
                role="Remote",
                measured_tps=float(speeds.get(spec_id) or 0.0),
                enabled=spec_id not in disabled,
                remote=True,
                api_key=getattr(row, "api_key", "") or "",
            ))
    return workers


def build_roster(owner: str = "") -> List[SwarmWorker]:
    """Everyone on the team right now — local models first, then (opt-in)
    remote specialists. The strongest LOCAL model manages; a remote model only
    leads when there is no local model at all."""
    settings = load_swarm_settings()
    state = _group_state()
    raw_models = state.get("models") if isinstance(state, dict) else None
    workers: List[SwarmWorker] = []
    speeds = _load_speeds()
    disabled = set(settings.get("disabled_workers") or [])

    if isinstance(raw_models, list) and raw_models:
        for entry in raw_models:
            if not isinstance(entry, dict):
                continue
            spec = _spec_by_id(entry.get("spec_id") or "")
            spec_id = entry.get("spec_id") or (spec.id if spec else "unknown")
            workers.append(SwarmWorker(
                spec_id=spec_id,
                label=entry.get("label") or (spec.label if spec else spec_id),
                model_id=entry.get("model_id") or (spec.alias if spec else spec_id),
                base_url=entry.get("base_url") or "",
                endpoint_id=entry.get("endpoint_id") or "",
                tier=(spec.tier if spec else "general") or "general",
                thinking=bool(spec.thinking) if spec else False,
                vision=bool(entry.get("vision")),
                params_b=float(spec.params_b) if spec else 0.0,
                active_params_b=float(spec.active_params_b or 0.0) if spec else 0.0,
                moe=bool(spec.moe) if spec else False,
                context=int(entry.get("context") or (spec.max_context if spec else 8192)),
                quant=entry.get("quant") or "",
                port=int(entry.get("port") or 0),
                role=ROLE_BY_TIER.get((spec.tier if spec else ""), "Utility"),
                measured_tps=float(speeds.get(spec_id) or 0.0),
                enabled=spec_id not in disabled,
            ))
    else:
        # Fallback: registered local endpoints (single-model setups).
        try:
            from core.database import ModelEndpoint, SessionLocal
            from src.auth_helpers import owner_filter
            db = SessionLocal()
            try:
                q = db.query(ModelEndpoint).filter(ModelEndpoint.id.like("local-llama-%"))
                for row in owner_filter(q, ModelEndpoint, owner).all():
                    if not (getattr(row, "id", "") or "").startswith("local-llama-"):
                        continue  # remote rows are cloud-assist material, not locals
                    spec = _spec_by_id(row.id.removeprefix("local-llama-"))
                    spec_id = spec.id if spec else row.id
                    models = []
                    try:
                        models = json.loads(row.cached_models or "[]")
                    except ValueError:
                        pass
                    workers.append(SwarmWorker(
                        spec_id=spec_id,
                        label=(row.name or spec_id).split("·")[-1].strip(),
                        model_id=models[0] if models else (spec.alias if spec else spec_id),
                        base_url=row.base_url or "",
                        endpoint_id=row.id,
                        tier=(spec.tier if spec else "general") or "general",
                        thinking=bool(spec.thinking) if spec else False,
                        vision=bool(spec.vision) if spec else False,
                        params_b=float(spec.params_b) if spec else 0.0,
                        active_params_b=float(spec.active_params_b or 0.0) if spec else 0.0,
                        moe=bool(spec.moe) if spec else False,
                        context=spec.max_context if spec else 8192,
                        role=ROLE_BY_TIER.get((spec.tier if spec else ""), "Utility"),
                        measured_tps=float(speeds.get(spec_id) or 0.0),
                        enabled=spec_id not in disabled,
                    ))
            finally:
                db.close()
        except Exception as exc:
            logger.warning("swarm roster fallback failed: %s", exc)

    # Cloud assist (opt-in): remote specialists join AFTER the locals, so the
    # manager is always the strongest LOCAL model while one exists.
    if settings.get("cloud_workers"):
        workers.extend(_cloud_workers_from_db(owner, speeds, disabled))

    ordered = sorted(workers, key=lambda w: (w.remote, -w.power))
    if ordered:
        ordered[0].is_manager = True
        ordered[0].role = "Manager"
    return ordered


# ── Specialist routing ───────────────────────────────────────────────────────
STEP_KINDS = ("coding", "reasoning", "research", "vision", "fast", "general")

KIND_LABEL = {
    "coding": "Coding",
    "reasoning": "Reasoning",
    "research": "Web research",
    "vision": "Vision",
    "fast": "Quick lookup",
    "general": "General",
}


def _match_score(worker: SwarmWorker, kind: str) -> float:
    """0..1 — how much this worker's SPECIALTY fits the step kind."""
    if kind == "coding":
        if worker.tier == "coding":
            return 1.0
        if worker.thinking:
            return 0.55
        return 0.15
    if kind == "reasoning":
        if worker.tier == "reasoning" and worker.thinking:
            return 1.0
        if worker.thinking:
            return 0.85
        return 0.2
    if kind == "vision":
        return 1.0 if worker.vision else 0.0
    if kind == "fast":
        # Small, quick models shine; the giant manager is wasted here.
        active = worker.active_params_b or worker.params_b or 4.0
        return 1.0 if active <= 2.0 else (0.6 if active <= 5.0 else 0.25)
    if kind == "research":
        return 0.8 if (worker.active_params_b or worker.params_b) <= 5.0 else 0.5
    return 0.6  # general


def route_worker(workers: List[SwarmWorker], kind: str) -> Optional[SwarmWorker]:
    """The right specialist for a step: match first, then power × speed.

    ``power × speed`` is deliberate — a brilliant-but-crawling model loses to
    a strong-and-quick one for step work, because the MANAGER (who is the
    strongest anyway) does the heavy final thinking. This is what keeps the
    swarm both fast and powerful.
    """
    pool = [w for w in workers if w.enabled and w.base_url and not w.is_manager]
    if not pool:
        pool = [w for w in workers if w.enabled and w.base_url]
    if not pool:
        return None
    if kind == "vision":
        pool = [w for w in pool if w.vision] or pool

    top_power = max((w.power for w in pool), default=1.0) or 1.0
    top_speed = max((w.speed_tps for w in pool), default=1.0) or 1.0

    def score(w: SwarmWorker) -> float:
        return (
            2.2 * _match_score(w, kind)
            + 0.6 * (w.power / top_power)
            + 0.5 * (w.speed_tps / top_speed)
        )

    return max(pool, key=score)


# ── Planning ─────────────────────────────────────────────────────────────────
_WEB_INTENT = re.compile(
    r"\b(search|look\s*up|lookup|google|latest|current|today|news|weather|"
    r"price|rate|score|match|release|who\s+is|what\s+happened)\b",
    re.IGNORECASE,
)


def parse_plan(raw: str, max_steps: int = 4) -> List[Dict[str, str]]:
    """Extract the manager's step list. Small local models fence JSON, pad it,
    or drop it entirely — every failure falls back to a sane one-step plan."""
    if not raw:
        return []
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidates = [fence.group(1)] if fence else []
    brace = re.search(r"\{.*\}", text, re.DOTALL)
    if brace:
        candidates.append(brace.group(0))
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        steps = data.get("steps") if isinstance(data, dict) else data
        if isinstance(steps, list):
            clean: List[Dict[str, str]] = []
            for step in steps[:max_steps]:
                if not isinstance(step, dict):
                    continue
                task = str(step.get("task") or "").strip()
                kind = str(step.get("kind") or "general").strip().lower()
                if not task:
                    continue
                if kind not in STEP_KINDS:
                    kind = "general"
                clean.append({"kind": kind, "task": task[:400]})
            if clean:
                return clean
    return []


def plan_prompt(message: str, workers: List[SwarmWorker], knowledge: List[Dict[str, Any]],
                internet: bool, max_steps: int = 4) -> str:
    team = ", ".join(
        f"{w.role} ({w.label})"
        for w in workers if w.enabled and not w.is_manager
    ) or "no workers"
    facts = "\n".join(f"- {e.get('text', '')}" for e in knowledge) or "- none yet"
    return (
        "You are the manager of an AI team. Split the user request into at most "
        f"{max_steps} short steps for the best specialist. Reply with ONLY JSON:\n"
        '{{"steps":[{"kind":"coding|reasoning|research|fast|general","task":"..."}]}}\n'
        f"Team: {team}\n"
        f"Internet search available: {'yes' if internet else 'no'} "
        '(use kind "research" only when fresh facts from the web are needed).\n'
        f"Remembered facts:\n{facts}\n"
        f"User request: {message[:1200]}\nJSON:"
    )


def _best_kind_for(worker: SwarmWorker) -> str:
    if worker.tier == "coding":
        return "coding"
    if worker.thinking:
        return "reasoning"
    if (worker.active_params_b or worker.params_b) <= 2.0:
        return "fast"
    return "general"


def fallback_plan(message: str) -> List[Dict[str, str]]:
    steps: List[Dict[str, str]] = []
    if _WEB_INTENT.search(message or ""):
        steps.append({"kind": "research", "task": f"Find fresh web facts about: {message[:300]}"})
    steps.append({"kind": "general", "task": message[:400]})
    return steps


# ── LLM calls (OpenAI-compatible, local llama-server endpoints) ──────────────
def _auth_headers(api_key: str = "") -> Dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


async def _chat(url: str, model: str, messages: List[Dict[str, str]], timeout: float,
                temperature: float = 0.4, api_key: str = "") -> Tuple[str, Dict[str, Any]]:
    """Non-streaming completion → (text, usage)."""
    import httpx
    async with httpx.AsyncClient(timeout=timeout, headers=_auth_headers(api_key)) as client:
        resp = await client.post(url, json={
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        })
        resp.raise_for_status()
        data = resp.json()
    choice = (data.get("choices") or [{}])[0]
    text = ((choice.get("message") or {}).get("content") or "").strip()
    return text, (data.get("usage") or {})


async def _chat_stream(url: str, model: str, messages: List[Dict[str, str]], timeout: float,
                       temperature: float = 0.5, api_key: str = ""):
    """Streaming completion — yields content deltas."""
    import httpx
    async with httpx.AsyncClient(timeout=timeout, headers=_auth_headers(api_key)) as client:
        async with client.stream("POST", url, json={
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    if payload == "[DONE]":
                        break
                    continue
                try:
                    chunk = json.loads(payload)
                except ValueError:
                    continue
                delta = ((chunk.get("choices") or [{}])[0].get("delta") or {})
                piece = delta.get("content")
                if piece:
                    yield piece


# ── Web research (full internet access) ──────────────────────────────────────
async def web_research(query: str, max_pages: int = 8) -> Tuple[str, List[Dict[str, str]]]:
    """Live web search + page fetch through the app's own search service.

    Returns (digest, sources) — the digest is the service's extracted context,
    the sources are the real URLs behind it for citation.
    """
    try:
        from services.search import comprehensive_web_search
    except Exception as exc:
        logger.warning("swarm research unavailable: %s", exc)
        return "", []
    try:
        out = await asyncio.to_thread(
            comprehensive_web_search, query, max_pages=max_pages, return_sources=True,
        )
    except Exception as exc:
        logger.warning("swarm research failed: %s", exc)
        return "", []
    if isinstance(out, tuple) and len(out) == 2:
        context, raw_sources = out
        sources = []
        for s in raw_sources or []:
            if isinstance(s, dict) and s.get("url"):
                sources.append({"url": str(s.get("url", ""))[:300], "title": str(s.get("title", ""))[:200]})
        return str(context or "")[:8000], sources[:8]
    return str(out or "")[:8000], []


# ── Orchestration ────────────────────────────────────────────────────────────
@dataclass
class StepResult:
    index: int
    kind: str
    task: str
    worker: str            # worker label
    worker_spec: str
    ok: bool
    text: str = ""
    elapsed_s: float = 0.0
    tps: float = 0.0
    error: str = ""


def candidate_workers(workers: List[SwarmWorker], kind: str, limit: int = 2) -> List[SwarmWorker]:
    """Best specialist first, then the next-best — the failover order."""
    picks: List[SwarmWorker] = []
    pool = list(workers)
    while len(picks) < limit:
        pick = route_worker(pool, kind)
        if pick is None or any(pick is w for w in picks):
            break
        picks.append(pick)
        pool = [w for w in pool if w is not pick]
    return picks


def pick_critic(workers: List[SwarmWorker], manager: SwarmWorker) -> SwarmWorker:
    """The sharpest checker: the strongest thinking model that ISN'T the
    manager (fresh eyes), falling back to the manager itself."""
    candidates = [w for w in workers if w.enabled and w.base_url and not w.is_manager]
    thinkers = [w for w in candidates if w.thinking] or candidates
    return max(thinkers, key=lambda w: w.power) if thinkers else manager


async def _run_step(step: Dict[str, str], index: int, workers: List[SwarmWorker],
                    knowledge: List[Dict[str, Any]], internet: bool):
    """Run one specialist step as an async GENERATOR of events:

    step_delta (live text) → [step_retry on failure] → step_done
    If the first-choice specialist fails, the next-best one takes over
    automatically (auto-failover) before the step is declared failed.
    """
    kind = step.get("kind", "general")
    task = step.get("task", "")
    started = time.monotonic()

    web_sources: Optional[List[Dict[str, str]]] = None
    context_blocks: List[str] = []

    if kind == "research":
        digest, sources = ("", [])
        if internet:
            digest, sources = await web_research(task)
        if sources:
            web_sources = sources
            pinned = "\n".join(f"[{i+1}] {src.get('title', '')[:80]} — {src['url']}"
                               for i, src in enumerate(sources))
            context_blocks.append("Live web findings:\n" + (digest[:4000] or "(no results)"))
            context_blocks.append(
                "FOUND SOURCES (cite ONLY these URLs, exactly as written — never invent one):\n" + pinned)
        elif internet:
            context_blocks.append("Web search returned nothing useful — rely on your own knowledge and say clearly that you have no live source.")
        else:
            context_blocks.append("(internet disabled — answer from knowledge)")

    attempts = candidate_workers(workers, kind)
    if not attempts:
        yield {"type": "step_done", **asdict(StepResult(index, kind, task, "nobody", "", False,
                                                        error="no worker available")),
               "web_sources": web_sources}
        return

    known = "\n".join(f"- {e.get('text', '')}" for e in knowledge[:KNOWLEDGE_INJECT_LIMIT])
    system = (
        f"You are the {KIND_LABEL.get(kind, 'General')} specialist of the psd.ai swarm. "
        f"Work ONLY on this step; be precise and compact (max ~180 words). "
        + ("If you write code, give the exact code in a fenced block. " if kind == "coding" else "")
        + "Do not greet, do not restate the whole request."
    )
    user_parts = [f"Step ({KIND_LABEL.get(kind, 'general')}): {task}"]
    if known:
        user_parts.append(f"Team memory:\n{known}")
    user_parts.extend(context_blocks)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]

    last_error = ""
    for attempt, worker in enumerate(attempts[:2]):
        if attempt:
            yield {"type": "step_retry", "index": index, "worker": worker.label, "reason": last_error[:160]}
        text = ""
        url = worker.base_url.rstrip("/") + "/chat/completions"
        try:
            async for piece in _chat_stream(url, worker.model_id, messages,
                                            timeout=WORKER_TIMEOUT_S, temperature=0.3,
                                            api_key=worker.api_key):
                text += piece
                yield {"type": "step_delta", "index": index, "delta": piece}
        except Exception as exc:
            last_error = str(exc)[:300]
            logger.warning("swarm step %s failed on %s: %s", index, worker.label, last_error)
            continue
        elapsed = time.monotonic() - started
        if not text.strip():
            last_error = "empty reply"
            continue
        tokens = max(1, len(text) // 4)
        tps = record_speed(worker.spec_id, tokens, elapsed) or (tokens / elapsed if elapsed > 0.05 else 0.0)
        yield {"type": "step_done", **asdict(StepResult(
            index, kind, task, worker.label, worker.spec_id, True,
            text=text[:2400], elapsed_s=round(elapsed, 2), tps=round(tps or 0.0, 1))),
            "web_sources": web_sources}
        return

    yield {"type": "step_done", **asdict(StepResult(
        index, kind, task, attempts[0].label, attempts[0].spec_id, False,
        elapsed_s=round(time.monotonic() - started, 2), error=last_error or "failed")),
        "web_sources": web_sources}



async def _drive_steps(queue: "asyncio.Queue", gen):
    """Forward every event of one step generator into the shared queue."""
    try:
        async for event in gen:
            await queue.put(event)
    except Exception as exc:  # a crashed step must not sink the turn
        logger.warning("swarm step crashed: %s", exc)
        await queue.put({"type": "step_done", "index": -1, "kind": "?", "task": "",
                         "worker": "?", "worker_spec": "", "ok": False,
                         "text": "", "elapsed_s": 0.0, "tps": 0.0,
                         "error": str(exc)[:200], "web_sources": None})
    finally:
        await queue.put(None)


def _chunks(text: str, size: int = 80):
    for i in range(0, len(text), size):
        yield text[i:i + size]


# English averages ~4 chars/token; the synthesis prompt may spend at most
# ~45% of the manager's context window so the reply + chat template still fit.
SYNTH_CONTEXT_FRACTION = 0.45
SYNTH_MAX_CHARS = 12000
SYNTH_MIN_CHARS = 2000


def synthesis_budget(manager: "SwarmWorker") -> int:
    """Chars the synthesis prompt may use for a given manager's context window.

    A 4k-context model gets ~7k chars (~1.8k tokens), a 32k+ model the full
    12k. This is exactly what stops a small-machine manager from overflowing
    and killing the whole turn.
    """
    chars = manager.context * 4.0 * SYNTH_CONTEXT_FRACTION
    return int(max(SYNTH_MIN_CHARS, min(SYNTH_MAX_CHARS, chars)))


def build_synth_messages(message: str, reports: List[str], known: str, recent: str,
                         sources: List[Dict[str, str]], budget: int,
                         compact: bool = False) -> List[Dict[str, str]]:
    """Synthesis prompt that ALWAYS fits ``budget`` chars.

    compact=True shrinks everything hard (short report summaries) — the retry
    ladder uses it when the full prompt already failed once.
    """
    reserved = 1400 if compact else 3400  # request + system + slack
    per_report = max(350 if compact else 900, (budget - reserved) // max(1, len(reports) or 1))
    body = "Specialist reports:\n" + ("\n\n".join(r[:per_report] for r in reports) or "(none)")
    if sources:
        body += "\n\nWeb sources to cite:\n" + "\n".join(
            f"[{i+1}] {src['url']}" for i, src in enumerate(sources[:8]))
    if compact:
        user = (
            f"User request: {message[:800]}\n\n{body[:max(800, budget - 1200)]}\n\n"
            "Write the final answer to the user NOW, briefly and clearly. No preamble."
        )
    else:
        user = (
            (f"Recent conversation:\n{recent}\n" if recent else "")
            + f"User request: {message[:1500]}\n\n"
            + (f"Remembered facts:\n{known[:800]}\n\n" if known else "")
            + body
            + "\n\nWrite the final answer to the user yourself. Combine the reports, "
              "drop failures quietly (or note them in one short line), keep it clear and complete. "
              "Structure like a top-tier assistant: short intro, sections or bullets when they "
              "help, code in fenced blocks, plain about anything uncertain. Cite web sources as "
              "[1], [2] — only the URLs given above."
        )
    system = (
        "You are the MANAGER of the psd.ai swarm. Combine the specialist reports into ONE "
        "clear, correct, well-structured final answer."
        if compact else
        "You are the MANAGER of the psd.ai swarm — the strongest model on this machine. "
        "Specialist workers did the sub-tasks; now give the user ONE clear, correct, "
        "well-structured final answer. Cite web sources as [1], [2] when used."
    )
    return [
        {"role": "system", "content": system[:600]},
        {"role": "user", "content": user[:max(600, budget - 600)]},
    ]


async def orchestrate(message: str, owner: str = "", history: Optional[List[Dict[str, str]]] = None):
    """The full swarm turn. An async GENERATOR of event dicts:

    phase(plan) → plan → step_delta* → [step_retry] → step_done*
    → phase(synthesize) → [phase(verify)] → synth_delta* → final → error?
    """
    settings = load_swarm_settings()
    workers = build_roster(owner)
    live = [w for w in workers if w.enabled and w.base_url]
    if not live:
        yield {"type": "error", "error": "No local models are running. Start the local model group first."}
        return

    manager = next((w for w in live if w.is_manager), live[0])
    knowledge = search_knowledge(message, owner)
    max_steps = int(settings.get("max_steps") or 4)

    # 1 ── the manager plans
    yield {"type": "phase", "phase": "plan", "manager": manager.to_dict()}
    plan_text = ""
    try:
        plan_text, _ = await _chat(
            manager.base_url.rstrip("/") + "/chat/completions",
            manager.model_id,
            [{"role": "user", "content": plan_prompt(
                message, workers, knowledge, bool(settings.get("internet")), max_steps=max_steps)}],
            timeout=60.0,
            temperature=0.2,
            api_key=manager.api_key,
        )
    except Exception as exc:
        logger.warning("swarm manager plan failed: %s", exc)
    steps = parse_plan(plan_text, max_steps=max_steps) or fallback_plan(message)

    # 2 ── delegate to specialists (parallel when the steps are independent)
    def _assignee(step: Dict[str, str]) -> str:
        w = route_worker(workers, step["kind"])
        return w.label if w else "nobody"

    yield {"type": "plan", "steps": [
        {**step, "worker": _assignee(step), "kind_label": KIND_LABEL.get(step["kind"], step["kind"])}
        for step in steps
    ]}

    results: List[StepResult] = []
    all_sources: List[Dict[str, str]] = []

    async def _absorb(event: dict):
        """Collect step results + web sources from a step event."""
        if event.get("type") != "step_done":
            return
        sources = event.pop("web_sources", None)
        if isinstance(sources, list):
            for src in sources:
                if src.get("url") and src not in all_sources:
                    all_sources.append(src)
        data = {k: v for k, v in event.items() if k != "type"}
        try:
            results.append(StepResult(**data))
        except TypeError:
            logger.warning("swarm: malformed step_done event skipped")

    parallel = bool(settings.get("parallel")) and 1 < len(steps) <= 4
    if parallel:
        queue: asyncio.Queue = asyncio.Queue()
        drivers = [
            asyncio.create_task(_drive_steps(
                queue, _run_step(step, i, workers, knowledge, bool(settings.get("internet")))))
            for i, step in enumerate(steps)
        ]
        finished = 0
        while finished < len(drivers):
            event = await queue.get()
            if event is None:
                finished += 1
                continue
            await _absorb(event)
            yield event
        await asyncio.gather(*drivers, return_exceptions=True)
    else:
        for i, step in enumerate(steps):
            async for event in _run_step(step, i, workers, knowledge, bool(settings.get("internet"))):
                await _absorb(event)
                yield event

    results.sort(key=lambda r: r.index)

    # 3 ── the manager writes the answer; the critic verifies it before it ships
    yield {"type": "phase", "phase": "synthesize", "manager": manager.to_dict()}
    reports = []
    for r in results:
        label = KIND_LABEL.get(r.kind, r.kind)
        if r.ok and r.text:
            reports.append(f"[{r.worker} · {label}] {r.text}")
        elif not r.ok:
            reports.append(f"[{r.worker} · {label}] FAILED: {r.error}")

    known = "\n".join(f"- {e.get('text', '')}" for e in knowledge)
    recent = ""
    if history:
        for h in history[-6:]:
            role = "User" if h.get("role") == "user" else "Assistant"
            recent += f"{role}: {str(h.get('content', ''))[:400]}\n"

    budget = synthesis_budget(manager)
    synth_messages = build_synth_messages(message, reports, known, recent, all_sources, budget)
    synth_url = manager.base_url.rstrip("/") + "/chat/completions"
    verify = bool(settings.get("verify"))

    final_text = ""
    degraded = False
    started = time.monotonic()
    try:
        if verify:
            # Draft quietly, let the critic attack it, only then show the user
            # one polished answer. (Streaming a draft and then replacing it
            # would feel broken — so the polish happens before the first token.)
            draft, _ = await _chat(synth_url, manager.model_id, synth_messages,
                                   timeout=MANAGER_TIMEOUT_S, api_key=manager.api_key)
            final_text = draft
            if draft.strip():
                critic = pick_critic(live, manager)
                yield {"type": "phase", "phase": "verify", "manager": manager.to_dict(),
                       "critic": critic.label}
                critic_reply = ""
                try:
                    critic_reply, _ = await _chat(
                        critic.base_url.rstrip("/") + "/chat/completions",
                        critic.model_id,
                        [{"role": "system", "content": (
                            "You are the swarm's quality CRITIC. Compare the DRAFT answer against "
                            "the specialist reports. Fix factual errors, remove invented details, "
                            "add anything important that is missing, keep the structure. "
                            "If the draft is already correct and complete, reply with exactly: KEEP. "
                            "Otherwise reply with the corrected full answer only."
                        )},
                         {"role": "user", "content": (
                             f"User request: {message[:800]}\n\nSpecialist reports:\n"
                             + ("\n\n".join(reports) or "(none)")[:6000] + "\n\n"
                             + ("Web sources:\n" + "\n".join(
                                 f"[{i+1}] {src['url']}" for i, src in enumerate(all_sources)
                             ) + "\n" if all_sources else "")
                             + f"\nDRAFT ANSWER:\n{draft[:6000]}"
                         )}],
                        timeout=MANAGER_TIMEOUT_S,
                        temperature=0.2,
                        api_key=critic.api_key,
                    )
                except Exception as exc:
                    logger.warning("swarm critic unavailable, shipping the draft: %s", exc)
                verdict = (critic_reply or "").strip()
                if verdict and not verdict.upper().startswith("KEEP") and len(verdict) >= 30:
                    final_text = verdict
        else:
            async for piece in _chat_stream(synth_url, manager.model_id, synth_messages,
                                            timeout=MANAGER_TIMEOUT_S, api_key=manager.api_key):
                final_text += piece
                yield {"type": "synth_delta", "delta": piece}
    except Exception as exc:
        # ── Synthesis ladder ────────────────────────────────────────────────
        # Attempt 1 failed (usually a small-context manager overflowing on a
        # big turn). Retry compact on the manager, then on ANY other live
        # worker, before falling back to raw reports. The user must get the
        # best answer this hardware can produce, not an error.
        logger.warning("swarm synthesis attempt 1 failed: %s", exc)
        degraded = True
        compact_msgs = build_synth_messages(
            message, reports, known, "", all_sources, budget, compact=True)
        ladder = [manager] + [w for w in live if w.base_url and not w.is_manager]
        for worker in ladder:
            yield {"type": "step_retry", "index": -1,
                   "worker": f"{worker.label} (answer)", "reason": "synthesis retry"}
            url = worker.base_url.rstrip("/") + "/chat/completions"
            try:
                final_text = ""
                async for piece in _chat_stream(url, worker.model_id, compact_msgs,
                                                timeout=MANAGER_TIMEOUT_S,
                                                api_key=worker.api_key):
                    final_text += piece
                    yield {"type": "synth_delta", "delta": piece}
                if final_text.strip():
                    break
            except Exception as exc2:
                logger.warning("swarm synthesis retry on %s failed: %s", worker.label, exc2)
                final_text = ""

    if not final_text.strip():
        ok_reports = [r for r in results if r.ok and r.text]
        if ok_reports:
            logger.warning("swarm: all synthesis attempts failed — shipping raw reports")
            yield {"type": "error",
                   "error": "Answer synthesis failed — showing the specialist reports raw. "
                            "A stronger primary model helps: PSD_MODEL_PROFILE=power ./run.sh"}
            final_text = "\n\n".join(r.text for r in ok_reports)
        else:
            yield {"type": "error", "error": "No worker could produce an answer for this request."}
            return

    # Ship the (possibly critic-corrected) answer as one streamed reply.
    if verify and not degraded:
        for piece in _chunks(final_text):
            yield {"type": "synth_delta", "delta": piece}
    elapsed = time.monotonic() - started
    record_speed(manager.spec_id, max(1, len(final_text) // 4), elapsed)

    yield {"type": "final", "text": final_text, "sources": all_sources,
           "steps": [asdict(r) for r in results], "degraded": degraded}

    # 4 ── learn (best-effort, never blocks the answer)
    if settings.get("auto_learn") and final_text:
        try:
            learn_text, _ = await _chat(
                synth_url,
                manager.model_id,
                [{"role": "user", "content": (
                    "From this exchange, list up to 2 durable facts worth remembering for "
                    "future requests (names, preferences, project facts, stable numbers). "
                    "One per line, no dashes, or reply NONE.\n\n"
                    f"Request: {message[:600]}\nAnswer: {final_text[:1500]}"
                )}],
                timeout=45.0,
                temperature=0.1,
                api_key=manager.api_key,
            )
            for line in (learn_text or "").splitlines():
                line = line.strip().lstrip("-•* ").strip()
                if not line or line.upper().startswith("NONE") or len(line) < 8:
                    continue
                add_knowledge(line, owner=owner, source="swarm")
        except Exception as exc:
            logger.debug("swarm learn skipped: %s", exc)



def status(owner: str = "") -> Dict[str, Any]:
    """Everything the Swarm GUI needs in one call."""
    settings = load_swarm_settings()
    workers = build_roster(owner)
    knowledge = load_knowledge(owner)
    state = _group_state()
    return {
        "available": any(w.enabled and w.base_url for w in workers),
        "workers": [w.to_dict() for w in workers],
        "manager": next((w.to_dict() for w in workers if w.is_manager), None),
        "settings": settings,
        "knowledge_count": len(knowledge),
        "group": {
            "count": state.get("count", len(workers)),
            "resident_gb": state.get("resident_gb"),
            "budget_gb": state.get("budget_gb"),
            "profile": state.get("profile"),
            "fits": state.get("fits"),
        } if isinstance(state, dict) else None,
    }
