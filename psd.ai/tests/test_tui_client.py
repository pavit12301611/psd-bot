"""Tests for the terminal-interface API client (tui/api_client.py).

The client is a thin wrapper over httpx that talks to the psd.ai backend over
loopback HTTP or in-process ASGI. These tests exercise it against a minimal
FastAPI app so they stay fast and never import the full psd.ai application.
"""

import json

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from tui.api_client import TuiApiClient, ApiError


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
        return {"ok": True, "username": body.get("username", "tester")}

    @app.post("/api/chat_stream")
    async def chat_stream(request: Request):
        async def gen():
            for delta in ["Hello ", "from ", "the client"]:
                yield f"data: {json.dumps({'delta': delta})}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    return app


async def test_health_through_asgi(mini_app):
    client = TuiApiClient("http://tui.invalid", app=mini_app)
    try:
        result = await client.health()
        assert result["status"] == "healthy"
    finally:
        await client.aclose()


async def test_login_ok_and_error(mini_app):
    client = TuiApiClient("http://tui.invalid", app=mini_app)
    try:
        user = await client.login("tester", "right")
        assert user == "tester"

        with pytest.raises(ApiError) as info:
            await client.login("tester", "wrong")
        assert info.value.status == 401
    finally:
        await client.aclose()


async def test_chat_stream_parses_deltas(mini_app):
    client = TuiApiClient("http://tui.invalid", app=mini_app)
    try:
        chunks = [c async for c in client.chat_stream("hi", "s1", mode="chat")]
        deltas = "".join(c.get("delta", "") for c in chunks if "delta" in c)
        assert deltas == "Hello from the client"
        assert any("_done" in c for c in chunks)
    finally:
        await client.aclose()


def test_ensure_2xx_surface_detail(mini_app, monkeypatch):
    """A non-2xx JSON response surfaces `detail` as the ApiError message."""

    async def run():
        client = TuiApiClient("http://tui.invalid", app=mini_app)
        try:
            resp = await client._request("GET", "/api/nope")
            with pytest.raises(ApiError) as info:
                client._ensure_2xx(resp)
            assert info.value.status == 404
        finally:
            await client.aclose()

    import asyncio

    asyncio.run(run())
