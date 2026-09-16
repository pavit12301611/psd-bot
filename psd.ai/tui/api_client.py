"""tui/api_client.py — loopback HTTP client for the psd.ai backend.

The terminal interface never re-implements any business logic: it talks to
the exact same FastAPI server that the browser front-end uses, over
http://127.0.0.1:<port>. Auth, sessions, history, model endpoints, streaming,
research — everything stays authoritative on the server. This module is just
a thin, deliberately dependency-free wrapper over :mod:`httpx`.

It supports the two launch modes of psd_tui.py:

* **attached** (default) — the server is already running; the client logs in
  with ``POST /api/auth/login`` and carries the session cookie.
* **embedded** — psd_tui.py imported and started the ``app`` object in-process
  (no TCP at all): the token is minted directly and every request is an ASGI
  call inside the same loop. The ``psd_ai_session`` cookie is still set on
  each request so the server's middleware sees a normal browser session.
"""

from __future__ import annotations

import json
from typing import Any, AsyncGenerator, Dict, List, Optional

import httpx


class ApiError(Exception):
    """Raised for any non-2xx / transport-level problem talking to the API."""

    def __init__(self, message: str, status: int = 0, detail: Any = None):
        super().__init__(message)
        self.status = status
        self.detail = detail


class TuiApiClient:
    """Async client for the psd.ai API, usable with httpx or in-process ASGI."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str = "",
        app: Any = None,
        transport: Any = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._app = app
        self._transport = transport
        # Only install a transport when talking to an in-process ASGI app.
        if app is not None and transport is None:
            self._transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            transport=self._transport,
            timeout=httpx.Timeout(connect=10.0, read=1200.0, write=1200.0, pool=10.0),
            follow_redirects=False,
        )
        if app is not None and token:
            self._client.cookies.set("psd_ai_session", token)

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ #
    # Low-level helpers
    # ------------------------------------------------------------------ #

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers: Dict[str, str] = {}
        if self._token:
            headers.setdefault("Authorization", f"Bearer {self._token}")
        if extra:
            headers.update(extra)
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        data: Any = None,
        params: Any = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> httpx.Response:
        try:
            resp = await self._client.request(
                method,
                path,
                json=json_body,
                data=data,
                params=params,
                headers=self._headers(headers),
            )
        except httpx.HTTPError as exc:
            raise ApiError(f"connection failed: {exc}") from exc
        return resp

    @staticmethod
    def _ensure_2xx(resp: httpx.Response) -> httpx.Response:
        if resp.is_success:
            return resp
        detail: Any = None
        try:
            body = resp.json()
        except Exception:
            body = None
        if isinstance(body, dict):
            detail = body.get("detail") or body.get("error") or body
        kind = f"HTTP {resp.status_code}"
        if isinstance(detail, str):
            raise ApiError(f"{kind}: {detail}", resp.status_code, detail)
        raise ApiError(kind, resp.status_code, detail)

    # ------------------------------------------------------------------ #
    # Auth
    # ------------------------------------------------------------------ #

    async def login(self, username: str, password: str, totp: str = "") -> str:
        body: Dict[str, Any] = {"username": username, "password": password, "remember": True}
        if totp:
            body["totp_code"] = totp
        resp = await self._request("POST", "/api/auth/login", json_body=body)
        party = resp.json() or {}
        if resp.status_code == 200 and party.get("ok"):
            return str(party.get("username") or "")
        if resp.status_code == 200 and party.get("requires_totp"):
            raise ApiError("2FA_REQUIRED", 200, party)
        body_ = party.get("detail")
        raise ApiError(f"login failed: {body_ or f'HTTP {resp.status_code}'}", resp.status_code, body_)

    async def status(self) -> Dict[str, Any]:
        resp = self._ensure_2xx(await self._request("GET", "/api/auth/status"))
        return resp.json() or {}

    async def health(self) -> Dict[str, Any]:
        resp = self._ensure_2xx(await self._request("GET", "/api/health"))
        return resp.json() or {}

    async def logout(self) -> None:
        try:
            await self._request("POST", "/api/auth/logout")
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Models & endpoints
    # ------------------------------------------------------------------ #

    async def models(self, refresh: bool = False) -> Dict[str, Any]:
        resp = self._ensure_2xx(
            await self._request("GET", "/api/models", params={"refresh": refresh})
        )
        return resp.json() or {}

    async def probe_local(self) -> Dict[str, Any]:
        resp = self._ensure_2xx(await self._request("GET", "/api/model-endpoints/probe-local"))
        return resp.json() or {}

    async def default_chat(self) -> Dict[str, Any]:
        try:
            resp = self._ensure_2xx(await self._request("GET", "/api/default-chat"))
            return resp.json() or {}
        except ApiError:
            return {}

    # ------------------------------------------------------------------ #
    # Sessions
    # ------------------------------------------------------------------ #

    async def sessions(self) -> List[Dict[str, Any]]:
        resp = self._ensure_2xx(await self._request("GET", "/api/sessions"))
        data = resp.json()
        if isinstance(data, list):
            return data
        return []

    async def create_session(
        self,
        name: str = "",
        model: str = "",
        endpoint_url: str = "",
        endpoint_id: str = "",
    ) -> Dict[str, Any]:
        form: Dict[str, str] = {
            "name": name,
            "model": model,
            "endpoint_url": endpoint_url,
            "endpoint_id": endpoint_id,
        }
        resp = await self._request("POST", "/api/session", data=form)
        if resp.status_code == 200:
            return resp.json() or {}
        raise ApiError(f"create session failed: {resp.status_code}", resp.status_code,
                       self._detail(resp))

    def _detail(self, resp: httpx.Response) -> Any:
        try:
            return (resp.json() or {}).get("detail")
        except Exception:
            return None

    async def delete_session(self, sid: str) -> None:
        self._ensure_2xx(await self._request("DELETE", f"/api/session/{sid}"))

    async def rename_session(self, sid: str, name: str) -> None:
        self._ensure_2xx(await self._request(
            "PATCH", f"/api/session/{sid}", data={"name": name}
        ))

    async def patch_session(
        self,
        sid: str,
        *,
        model: str = "",
        endpoint_url: str = "",
        endpoint_id: str = "",
    ) -> None:
        data: Dict[str, str] = {}
        if model:
            data["model"] = model
        if endpoint_url:
            data["endpoint_url"] = endpoint_url
        if endpoint_id:
            data["endpoint_id"] = endpoint_id
        self._ensure_2xx(await self._request("PATCH", f"/api/session/{sid}", data=data))

    # ------------------------------------------------------------------ #
    # History
    # ------------------------------------------------------------------ #

    async def history(self, sid: str, limit: int = 100) -> Dict[str, Any]:
        resp = self._ensure_2xx(
            await self._request("GET", f"/api/history/{sid}", params={"limit": limit})
        )
        return resp.json() or {}

    # ------------------------------------------------------------------ #
    # Streaming chat
    # ------------------------------------------------------------------ #

    def chat_stream(
        self,
        message: str,
        session: str,
        *,
        mode: str = "chat",
        endpoint_id: str = "",
        use_web: bool = False,
        allow_bash: bool = False,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        async def _gen() -> AsyncGenerator[Dict[str, Any], None]:
            form: Dict[str, Any] = {
                "message": message,
                "session": session,
                "mode": mode,
                "allow_bash": "true" if allow_bash else "false",
                "allow_web_search": "true" if use_web else "false",
            }
            if endpoint_id:
                form["selected_endpoint_id"] = endpoint_id
            try:
                async with self._client.stream(
                    "POST",
                    "/api/chat_stream",
                    data=form,
                    headers=self._headers(),
                    timeout=httpx.Timeout(
                        connect=10.0, read=1800.0, write=60.0, pool=10.0
                    ),
                ) as resp:
                    if resp.status_code != 200:
                        body = await resp.aread()
                        try:
                            detail = json.loads(body).get("detail")
                        except Exception:
                            detail = body.decode("utf-8", "replace")[:300]
                        yield {"_error": f"HTTP {resp.status_code}: {detail}"}
                        return
                    async for raw in resp.aiter_lines():
                        line = raw.strip()
                        if not line or not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload in ("[DONE]", ""):
                            yield {"_done": True}
                            continue
                        try:
                            yield json.loads(payload)
                        except json.JSONDecodeError:
                            continue
            except httpx.HTTPError as exc:
                yield {"_error": f"connection lost: {exc}"}

        return _gen()

    async def stop_stream(self, session: str) -> None:
        try:
            await self._request("POST", f"/api/chat/stop/{session}", json_body={})
        except Exception:
            pass
