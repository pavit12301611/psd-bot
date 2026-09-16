"""Tests for the desktop-interface API client (gui/api_client.py).

The client is a thin wrapper over httpx that talks to the psd.ai backend over
loopback HTTP or in-process ASGI. These tests exercise it against a minimal
FastAPI app so they stay fast and never import the full psd.ai application.
"""

import json

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from gui.api_client import GuiApiClient, ApiError


@pytest.fixture
def mini_app():
    app = FastAPI()

    @app.get("/api/health")
    async def health():
        return {"status": "healthy"}

    @app.get("/api/auth/status")
    async def status(request: Request):
        return {"configured": True, "authenticated": True, "username": "tester"}

    @app.post("/api/auth/login")
    async def login(request: Request):
        body = await request.json()
        if body.get("password") == "wrong":
            return JSONResponse({"detail": "Invalid credentials"}, status_code=401)
        if body.get("username") == "totp":
            return {"ok": False, "requires_totp": True, "username": "totp"}
        resp = JSONResponse({"ok": True, "username": body.get("username", "tester")})
        resp.set_cookie("psd_ai_session", "cookie-value")
        return resp

    @app.post("/api/auth/setup")
    async def setup(request: Request):
        body = await request.json()
        return {"ok": True, "message": f"created {body.get('username')}"}

    @app.get("/api/models")
    async def models():
        return {
            "items": [
                {
                    "endpoint_id": "ep1",
                    "endpoint_name": "local",
                    "url": "http://127.0.0.1:8080/v1",
                    "models": ["model-a", "model-b"],
                    "models_extra": [],
                }
            ]
        }

    @app.get("/api/default-chat")
    async def default_chat():
        return {"endpoint_id": "ep1", "endpoint_url": "http://127.0.0.1:8080/v1", "model": "model-a"}

    @app.get("/api/sessions")
    async def sessions():
        return [{"id": "s1", "name": "Hello", "model": "model-a"}]

    @app.post("/api/session")
    async def create_session(request: Request):
        form = await request.form()
        return {"id": "s2", "name": form.get("name", ""), "model": form.get("model", "")}

    @app.patch("/api/session/{sid}")
    async def patch_session(sid: str, request: Request):
        form = await request.form()
        return {"ok": True, "id": sid, "name": form.get("name", "")}

    @app.delete("/api/session/{sid}")
    async def delete_session(sid: str):
        return {"ok": True, "id": sid}

    @app.get("/api/history/{sid}")
    async def history(sid: str):
        return {
            "history": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}],
            "name": "Hello",
            "model": "model-a",
        }

    @app.post("/api/chat_stream")
    async def chat_stream(request: Request):
        async def gen():
            for delta in ["Hello ", "from ", "the client"]:
                yield f"data: {json.dumps({'delta': delta})}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.post("/api/chat/stop/{sid}")
    async def stop(sid: str):
        return {"ok": True}

    return app


async def test_health_through_asgi(mini_app):
    client = GuiApiClient("http://gui.invalid", app=mini_app)
    try:
        result = await client.health()
        assert result["status"] == "healthy"
    finally:
        await client.aclose()


async def test_login_ok_and_error(mini_app):
    client = GuiApiClient("http://gui.invalid", app=mini_app)
    try:
        result = await client.login("tester", "right")
        assert result["ok"] is True
        assert result["session"] == "cookie-value"
        with pytest.raises(ApiError):
            await client.login("tester", "wrong")
    finally:
        await client.aclose()


async def test_login_requires_totp(mini_app):
    client = GuiApiClient("http://gui.invalid", app=mini_app)
    try:
        result = await client.login("totp", "right")
        assert result["ok"] is False
        assert result["requires_totp"] is True
    finally:
        await client.aclose()


async def test_sessions_default_and_history(mini_app):
    client = GuiApiClient("http://gui.invalid", app=mini_app)
    try:
        sessions = await client.sessions()
        assert sessions[0]["id"] == "s1"
        dflt = await client.default_chat()
        assert dflt["model"] == "model-a"
        hist = await client.history("s1")
        assert len(hist["history"]) == 2
        created = await client.create_session("New chat", model="model-a", endpoint_url="u")
        assert created["id"] == "s2"
        await client.rename_session("s2", "Renamed")
        await client.patch_session("s2", model="model-b")
        await client.delete_session("s2")
    finally:
        await client.aclose()


async def test_models_flattened(mini_app):
    client = GuiApiClient("http://gui.invalid", app=mini_app)
    try:
        data = await client.models()
        items = data["items"]
        assert items[0]["endpoint_id"] == "ep1"
        assert items[0]["models"] == ["model-a", "model-b"]
    finally:
        await client.aclose()


async def test_chat_stream_deltas(mini_app):
    client = GuiApiClient("http://gui.invalid", app=mini_app)
    try:
        chunks = []
        async for chunk in client.chat_stream("hi", "s1", mode="chat"):
            chunks.append(chunk)
        joined = "".join(c.get("delta", "") for c in chunks if "delta" in c)
        assert joined == "Hello from the client"
        assert any(c.get("_done") for c in chunks)
    finally:
        await client.aclose()
