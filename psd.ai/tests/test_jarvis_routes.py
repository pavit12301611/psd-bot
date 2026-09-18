"""Jarvis route surface: gating, validation and the action round-trip.

The real ComputerService is never constructed here — a stub records what the
route asked for, which keeps the suite safe to run on a developer's own
desktop.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.jarvis_routes as routes_mod
from routes.jarvis_routes import setup_jarvis_routes


class FakeComputer:
    def __init__(self, result=None):
        self.result = result or {"ok": True, "action": "open", "risk": "write", "opened": "notepad"}
        self.calls = []

    async def act(self, action, params=None, confirm_risky=None):
        self.calls.append((action, dict(params or {}), confirm_risky))
        return dict(self.result)

    async def screenshot(self, monitor=0):
        return b"\x89PNG\r\n\x1a\n" + b"0" * 32


class FakeJarvis:
    def __init__(self):
        self.calls = []

    def status(self):
        return {"enabled": True, "model": "", "computer": {"enabled": True}, "actions": ["open"]}

    async def transcribe(self, audio):
        return "open notepad"

    async def turn(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "transcript": kwargs.get("text") or "open notepad",
            "reply": "Opening it, sir.",
            "audio": None,
            "speak_in_browser": True,
            "actions": [],
            "model": "",
            "planner": "heuristic",
            "took_ms": 3,
        }

    def clear_history(self, key=None):
        self.cleared = key
        return None


@pytest.fixture
def current_user():
    return {"name": "owner"}


@pytest.fixture
def app(monkeypatch, current_user):
    monkeypatch.setattr(routes_mod, "owner_is_admin_or_single_user", lambda owner: owner == "owner")
    jarvis = FakeJarvis()
    computer = FakeComputer()
    application = FastAPI()

    @application.middleware("http")
    async def _user_mw(request, call_next):
        request.state.current_user = current_user["name"]
        return await call_next(request)

    application.include_router(setup_jarvis_routes(jarvis, computer))
    application.state.jarvis = jarvis
    application.state.computer = computer
    return application


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------


def test_status_is_public_and_reports_capabilities(client):
    r = client.get("/api/jarvis/status")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert "open" in body["actions"]


def test_act_runs_an_allowed_action(client, app):
    r = client.post("/api/jarvis/act", json={"action": "open", "params": {"target": "notepad"}})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert app.state.computer.calls[0][0] == "open"
    assert app.state.computer.calls[0][1]["target"] == "notepad"


def test_confirm_flag_is_forwarded(client, app):
    client.post("/api/jarvis/act", json={"action": "kill", "params": {"name": "notepad.exe"}, "confirm": True})
    assert app.state.computer.calls[0][1].get("confirm") is True


def test_act_rejects_anonymous_callers(client, current_user):
    current_user["name"] = None
    r = client.post("/api/jarvis/act", json={"action": "open", "params": {}})
    assert r.status_code == 403


def test_act_rejects_api_tokens(client, current_user):
    current_user["name"] = "api"
    r = client.post("/api/jarvis/act", json={"action": "open", "params": {}})
    assert r.status_code == 403


def test_act_rejects_non_admin_users(client, current_user):
    current_user["name"] = "guest"
    r = client.post("/api/jarvis/act", json={"action": "open", "params": {}})
    assert r.status_code == 403


def test_act_rejects_cross_site_navigation(client):
    r = client.post("/api/jarvis/act", json={"action": "open", "params": {}}, headers={"sec-fetch-site": "cross-site"})
    assert r.status_code == 403


def test_act_rejects_an_empty_action(client):
    r = client.post("/api/jarvis/act", json={"action": "", "params": {}})
    assert r.status_code == 422


def test_turn_with_text_returns_the_spoken_reply(client, app):
    r = client.post("/api/jarvis/turn", data={"text": "open notepad"})
    assert r.status_code == 200
    body = r.json()
    assert body["reply"] == "Opening it, sir."
    assert body["speak_in_browser"] is True
    assert app.state.jarvis.calls[0]["text"] == "open notepad"


def test_turn_with_nothing_to_say_is_a_400(client):
    r = client.post("/api/jarvis/turn", data={"text": "   "})
    assert r.status_code == 400


def test_turn_can_disable_actions(client, app):
    client.post("/api/jarvis/turn", data={"text": "hi", "allow_actions": "0"})
    assert app.state.jarvis.calls[0]["allow_actions"] is False


def test_turn_rejects_cross_site(client):
    r = client.post("/api/jarvis/turn", data={"text": "hi"}, headers={"sec-fetch-site": "cross-site"})
    assert r.status_code == 403


def test_screen_returns_a_png(client):
    r = client.get("/api/jarvis/screen")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content.startswith(b"\x89PNG")


def test_reset_clears_history(client, app):
    r = client.post("/api/jarvis/reset", json={"session_id": "abc"})
    assert r.status_code == 200
    assert app.state.jarvis.cleared == "abc"


def test_transcribe_is_owner_only(client, current_user):
    current_user["name"] = "guest"
    r = client.post("/api/jarvis/transcribe", files={"file": ("a.webm", b"12345", "audio/webm")})
    assert r.status_code == 403


def test_transcribe_returns_the_text(client):
    r = client.post("/api/jarvis/transcribe", files={"file": ("a.webm", b"12345", "audio/webm")})
    assert r.status_code == 200
    assert r.json()["text"] == "open notepad"


def test_a_blocked_action_is_reported_not_swallowed(monkeypatch, current_user):
    """When the policy refuses, the route must surface the refusal."""

    blocked = FakeComputer(result={"ok": False, "action": "kill", "risk": "risky",
                                   "blocked": True, "error": "Refused: protected process"})
    monkeypatch.setattr(routes_mod, "owner_is_admin_or_single_user", lambda owner: True)
    application = FastAPI()

    @application.middleware("http")
    async def _user_mw(request, call_next):
        request.state.current_user = "owner"
        return await call_next(request)

    application.include_router(setup_jarvis_routes(FakeJarvis(), blocked))
    with TestClient(application) as c:
        r = c.post("/api/jarvis/act", json={"action": "kill", "params": {"name": "python.exe"}})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["blocked"] is True
    assert "Refused" in body["error"]
