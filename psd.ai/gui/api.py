"""Typed client for the psd.ai API surface.

Every screen talks to the backend exclusively through this facade, so the
endpoint paths and payload shapes live in exactly one place. All calls are
synchronous and safe to invoke from worker threads
(:mod:`gui.workers`); they drive the in-process backend, never a socket.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from gui.backend import BackendError, InProcessBackend, SseStream, get_backend, timezone_headers


@dataclass
class ChatTurn:
    """One user message plus its options, ready for ``/api/chat_stream``."""

    message: str
    session_id: str = ""
    mode: str = "chat"                       # chat | agent
    model: str = ""
    endpoint_id: str = ""
    endpoint_url: str = ""
    attachments: Sequence[str] = field(default_factory=list)
    use_web: bool = False
    allow_web_search: bool = False
    allow_bash: bool = False
    use_rag: bool = False
    use_research: bool = False
    incognito: bool = False
    plan_mode: bool = False
    approved_plan: str = ""
    workspace: str = ""
    preset_id: str = ""
    active_doc_id: str = ""
    active_email_uid: str = ""
    active_email_folder: str = ""
    active_email_account: str = ""
    tool_approval_id: str = ""
    tool_approval_decision: str = ""

    def form(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "message": self.message,
            "session": self.session_id,
            "mode": self.mode,
            "plan_mode": "true" if self.plan_mode else "false",
            "use_web": "true" if self.use_web else "false",
            "allow_web_search": "true" if self.allow_web_search else "false",
            "allow_bash": "true" if self.allow_bash else "false",
            "use_rag": "true" if self.use_rag else "false",
            "use_research": "true" if self.use_research else "false",
            "incognito": "true" if self.incognito else "false",
        }
        if self.model:
            data["selected_model"] = self.model
        if self.endpoint_url:
            data["selected_endpoint_url"] = self.endpoint_url
        if self.endpoint_id:
            data["selected_endpoint_id"] = self.endpoint_id
        if self.attachments:
            data["attachments"] = json.dumps(list(self.attachments))
        if self.approved_plan:
            data["approved_plan"] = self.approved_plan[:8192]
        if self.workspace:
            data["workspace"] = self.workspace
        if self.preset_id:
            data["preset_id"] = self.preset_id
        if self.active_doc_id:
            data["active_doc_id"] = self.active_doc_id
        if self.active_email_uid:
            data["active_email_uid"] = self.active_email_uid
            data["active_email_folder"] = self.active_email_folder or "INBOX"
            if self.active_email_account:
                data["active_email_account"] = self.active_email_account
        if self.tool_approval_id:
            data["tool_approval_id"] = self.tool_approval_id
            data["tool_approval_decision"] = self.tool_approval_decision
        return data


class Api:
    """Facade over the in-process backend."""

    def __init__(self, backend: Optional[InProcessBackend] = None) -> None:
        self.be = backend or get_backend()

    # ------------------------------------------------------------------ #
    # raw
    # ------------------------------------------------------------------ #
    def get(self, path: str, **kw: Any) -> Any:
        return self.be.get(path, **kw)

    def post(self, path: str, **kw: Any) -> Any:
        return self.be.post(path, **kw)

    def put(self, path: str, **kw: Any) -> Any:
        return self.be.put(path, **kw)

    def patch(self, path: str, **kw: Any) -> Any:
        return self.be.patch(path, **kw)

    def delete(self, path: str, **kw: Any) -> Any:
        return self.be.delete(path, **kw)

    # ------------------------------------------------------------------ #
    # auth / account
    # ------------------------------------------------------------------ #
    def auth_status(self) -> Dict[str, Any]:
        return self.get("/api/auth/status")

    def login(self, username: str, password: str, remember: bool = True,
              totp_code: str = "") -> Dict[str, Any]:
        return self.post("/api/auth/login", json_body={
            "username": username, "password": password,
            "remember": remember, "totp_code": totp_code or None,
        })

    def logout(self) -> Dict[str, Any]:
        return self.post("/api/auth/logout")

    def first_run_setup(self, username: str, password: str) -> Dict[str, Any]:
        return self.post("/api/auth/setup", json_body={"username": username, "password": password})

    def change_password(self, current: str, new: str) -> Dict[str, Any]:
        return self.post("/api/auth/change-password", json_body={
            "current_password": current, "new_password": new})

    def users(self) -> List[Dict[str, Any]]:
        return self.get("/api/auth/users").get("users", [])

    def create_user(self, username: str, password: str, is_admin: bool = False) -> Dict[str, Any]:
        return self.post("/api/auth/users", json_body={
            "username": username, "password": password, "is_admin": is_admin})

    def delete_user(self, username: str) -> Dict[str, Any]:
        return self.delete("/api/auth/users", params={"username": username})

    def set_admin(self, username: str, is_admin: bool) -> Dict[str, Any]:
        return self.put(f"/api/auth/users/{username}/admin", json_body={"is_admin": is_admin})

    def set_privileges(self, username: str, privileges: Dict[str, Any]) -> Dict[str, Any]:
        return self.put(f"/api/auth/users/{username}/privileges", json_body=privileges)

    def features(self) -> Dict[str, Any]:
        return self.get("/api/auth/features")

    def auth_settings(self) -> Dict[str, Any]:
        return self.get("/api/auth/settings")

    def save_auth_settings(self, settings: Dict[str, Any]) -> Dict[str, Any]:
        return self.post("/api/auth/settings", json_body=settings)

    def two_fa_status(self) -> Dict[str, Any]:
        return self.get("/api/auth/2fa/status")

    def heartbeat(self) -> None:
        """Mirror the web UI's activity heartbeat (background-task gate)."""
        try:
            self.post("/api/activity/heartbeat")
        except BackendError:
            pass

    # ------------------------------------------------------------------ #
    # sessions / history
    # ------------------------------------------------------------------ #
    def sessions(self) -> List[Dict[str, Any]]:
        return self.get("/api/sessions") or []

    def archived_sessions(self) -> List[Dict[str, Any]]:
        return self.get("/api/sessions/archived") or []

    def create_session(self, name: str = "", model: str = "", endpoint_id: str = "",
                       endpoint_url: str = "") -> Dict[str, Any]:
        data: Dict[str, Any] = {"name": name, "skip_validation": "true"}
        if model:
            data["model"] = model
        if endpoint_id:
            data["endpoint_id"] = endpoint_id
        if endpoint_url:
            data["endpoint_url"] = endpoint_url
        return self.post("/api/session", data=data)

    def rename_session(self, sid: str, name: str) -> Dict[str, Any]:
        return self.patch(f"/api/session/{sid}", data={"name": name})

    def update_session(self, sid: str, **fields: Any) -> Dict[str, Any]:
        return self.patch(f"/api/session/{sid}", data={k: v for k, v in fields.items() if v is not None})

    def delete_session(self, sid: str) -> Any:
        return self.post(f"/api/session/{sid}/delete")

    def bulk_delete_sessions(self, sids: Sequence[str]) -> Any:
        return self.post("/api/sessions/bulk-delete", json_body={"session_ids": list(sids)})

    def delete_all_sessions(self) -> Any:
        return self.delete("/api/sessions/all")

    def archive_session(self, sid: str) -> Any:
        return self.post(f"/api/session/{sid}/archive")

    def unarchive_session(self, sid: str) -> Any:
        return self.post(f"/api/session/{sid}/unarchive")

    def export_session(self, sid: str) -> Any:
        return self.get(f"/api/session/{sid}/export")

    def history(self, sid: str) -> Dict[str, Any]:
        return self.get(f"/api/history/{sid}")

    def context_info(self, sid: str) -> Dict[str, Any]:
        return self.get(f"/api/session/{sid}/context_info")

    def compact_session(self, sid: str) -> Any:
        return self.post(f"/api/session/{sid}/compact")

    def mark_important(self, sid: str, important: bool) -> Any:
        return self.post(f"/api/session/{sid}/important", json_body={"important": important})

    def conversation_topics(self) -> Dict[str, Any]:
        return self.get("/api/conversations/topics")

    # ------------------------------------------------------------------ #
    # chat
    # ------------------------------------------------------------------ #
    def stream_chat(self, turn: ChatTurn) -> SseStream:
        headers = dict(timezone_headers())
        headers["Accept"] = "text/event-stream"
        return self.be.stream("POST", "/api/chat_stream", data=turn.form(), headers=headers)

    def stop_chat(self, sid: str) -> Any:
        return self.post(f"/api/chat/stop/{sid}")

    def chat_stream_status(self, sid: str) -> Dict[str, Any]:
        return self.get(f"/api/chat/stream_status/{sid}")

    def resume_chat(self, sid: str) -> Any:
        return self.get(f"/api/chat/resume/{sid}")

    # ------------------------------------------------------------------ #
    # uploads / attachments
    # ------------------------------------------------------------------ #
    def upload_files(self, paths: Sequence[str], session_id: str = "") -> Dict[str, Any]:
        files: List[tuple] = []
        handles = []
        try:
            for path in paths:
                handle = open(path, "rb")
                handles.append(handle)
                files.append(("files", (os.path.basename(path), handle, "application/octet-stream")))
            data = {"session_id": session_id} if session_id else None
            return self.post("/api/upload", data=data, files=files)
        finally:
            for handle in handles:
                handle.close()

    def upload_stats(self) -> Dict[str, Any]:
        return self.get("/api/upload/stats")

    # ------------------------------------------------------------------ #
    # models / endpoints
    # ------------------------------------------------------------------ #
    def models(self, refresh: bool = False) -> Dict[str, Any]:
        params = {"background": "false"}
        if refresh:
            params["refresh"] = "true"
        return self.get("/api/models", params=params)

    def endpoints(self) -> List[Dict[str, Any]]:
        return self.get("/api/model-endpoints") or []

    def add_endpoint(self, name: str, base_url: str, api_key: str = "",
                     model_type: str = "llm", endpoint_kind: str = "auto",
                     pinned_models: str = "", supports_tools: str = "",
                     shared: bool = True, skip_probe: bool = True) -> Dict[str, Any]:
        return self.post("/api/model-endpoints", data={
            "name": name, "base_url": base_url, "api_key": api_key,
            "model_type": model_type, "endpoint_kind": endpoint_kind,
            "pinned_models": pinned_models, "supports_tools": supports_tools,
            "shared": "true" if shared else "false",
            "skip_probe": "true" if skip_probe else "false",
        })

    def update_endpoint(self, ep_id: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        return self.patch(f"/api/model-endpoints/{ep_id}", json_body=fields)

    def toggle_endpoint(self, ep_id: str) -> Any:
        return self.patch(f"/api/model-endpoints/{ep_id}")

    def delete_endpoint(self, ep_id: str) -> Any:
        return self.delete(f"/api/model-endpoints/{ep_id}")

    def test_endpoint(self, base_url: str, api_key: str = "") -> Dict[str, Any]:
        return self.post("/api/model-endpoints/test", data={
            "base_url": base_url, "api_key": api_key})

    def probe_endpoint(self, ep_id: str) -> Dict[str, Any]:
        return self.get(f"/api/model-endpoints/{ep_id}/probe")

    def endpoint_models(self, ep_id: str) -> Dict[str, Any]:
        return self.get(f"/api/model-endpoints/{ep_id}/models")

    def default_chat(self) -> Dict[str, Any]:
        return self.get("/api/default-chat")

    def set_default_chat(self, endpoint_id: str, model: str) -> None:
        """Default model = per-user prefs, exactly like the web picker."""
        self.put("/api/prefs/default_endpoint_id", json_body={"value": endpoint_id})
        self.put("/api/prefs/default_model", json_body={"value": model})

    def providers(self) -> Any:
        return self.get("/api/providers")

    def discover_models(self) -> Any:
        return self.get("/api/discover")

    # ------------------------------------------------------------------ #
    # documents
    # ------------------------------------------------------------------ #
    def documents(self) -> Dict[str, Any]:
        return self.get("/api/documents/library")

    def document(self, doc_id: str) -> Dict[str, Any]:
        return self.get(f"/api/document/{doc_id}")

    def create_document(self, title: str = "Untitled", content: str = "",
                        language: str = "", session_id: str = "") -> Dict[str, Any]:
        return self.post("/api/document", json_body={
            "title": title, "content": content, "language": language or None,
            "session_id": session_id or None})

    def save_document(self, doc_id: str, content: str) -> Dict[str, Any]:
        return self.put(f"/api/document/{doc_id}", json_body={"content": content})

    def patch_document(self, doc_id: str, title: Optional[str] = None,
                       language: Optional[str] = None) -> Dict[str, Any]:
        body: Dict[str, Any] = {}
        if title is not None:
            body["title"] = title
        if language is not None:
            body["language"] = language
        return self.patch(f"/api/document/{doc_id}", json_body=body)

    def delete_document(self, doc_id: str) -> Any:
        return self.delete(f"/api/document/{doc_id}")

    def document_versions(self, doc_id: str) -> Any:
        return self.get(f"/api/document/{doc_id}/versions")

    def restore_document_version(self, doc_id: str, version: int) -> Any:
        return self.post(f"/api/document/{doc_id}/restore/{version}")

    def export_document_pdf(self, doc_id: str) -> bytes:
        return self.be.get(f"/api/document/{doc_id}/export-pdf", expect="bytes")

    def tidy_documents(self) -> Any:
        return self.post("/api/documents/tidy")

    # ------------------------------------------------------------------ #
    # notes
    # ------------------------------------------------------------------ #
    def notes(self, archived: Optional[bool] = None) -> List[Dict[str, Any]]:
        params = {}
        if archived is not None:
            params["archived"] = "true" if archived else "false"
        return (self.get("/api/notes", params=params) or {}).get("notes", [])

    def note(self, note_id: str) -> Dict[str, Any]:
        return self.get(f"/api/notes/{note_id}")

    def create_note(self, **fields: Any) -> Dict[str, Any]:
        return self.post("/api/notes", json_body=fields)

    def update_note(self, note_id: str, **fields: Any) -> Dict[str, Any]:
        return self.put(f"/api/notes/{note_id}", json_body=fields)

    def delete_note(self, note_id: str) -> Any:
        return self.delete(f"/api/notes/{note_id}")

    def pin_note(self, note_id: str, pinned: bool) -> Any:
        return self.post(f"/api/notes/{note_id}/pin", json_body={"pinned": pinned})

    def archive_note(self, note_id: str, archived: bool) -> Any:
        return self.post(f"/api/notes/{note_id}/archive", json_body={"archived": archived})

    def toggle_note_item(self, note_id: str, index: int) -> Any:
        return self.post(f"/api/notes/{note_id}/items/{index}/toggle")

    def reorder_notes(self, order: Sequence[str]) -> Any:
        return self.post("/api/notes/reorder", json_body={"order": list(order)})

    # ------------------------------------------------------------------ #
    # tasks / scheduler
    # ------------------------------------------------------------------ #
    def tasks(self, include_last_run: bool = True) -> List[Dict[str, Any]]:
        return (self.get("/api/tasks", params={
            "include_last_run": "true" if include_last_run else "false"}) or {}).get("tasks", [])

    def task(self, task_id: str) -> Dict[str, Any]:
        return self.get(f"/api/tasks/{task_id}")

    def create_task(self, **fields: Any) -> Dict[str, Any]:
        return self.post("/api/tasks", json_body=fields)

    def update_task(self, task_id: str, **fields: Any) -> Dict[str, Any]:
        return self.put(f"/api/tasks/{task_id}", json_body=fields)

    def delete_task(self, task_id: str) -> Any:
        return self.delete(f"/api/tasks/{task_id}")

    def run_task(self, task_id: str) -> Any:
        return self.post(f"/api/tasks/{task_id}/run")

    def stop_task(self, task_id: str) -> Any:
        return self.post(f"/api/tasks/{task_id}/stop")

    def pause_task(self, task_id: str) -> Any:
        return self.post(f"/api/tasks/{task_id}/pause")

    def resume_task(self, task_id: str) -> Any:
        return self.post(f"/api/tasks/{task_id}/resume")

    def task_runs(self, task_id: str) -> Any:
        return self.get(f"/api/tasks/{task_id}/runs")

    def recent_task_runs(self) -> Any:
        return self.get("/api/tasks/runs/recent")

    def task_notifications(self) -> Any:
        return self.get("/api/tasks/notifications")

    def task_output_targets(self) -> Any:
        return self.get("/api/tasks/meta/output-targets")

    # ------------------------------------------------------------------ #
    # calendar
    # ------------------------------------------------------------------ #
    def calendar_events(self, start: str, end: str, calendar: str = "") -> List[Dict[str, Any]]:
        params = {"start": start, "end": end}
        if calendar:
            params["calendar"] = calendar
        return (self.get("/api/calendar/events", params=params) or {}).get("events", [])

    def calendars(self) -> List[Dict[str, Any]]:
        return self.get("/api/calendar/calendars") or []

    def create_event(self, summary: str, dtstart: str, dtend: Optional[str] = None,
                     all_day: bool = False, description: str = "", location: str = "",
                     **extra: Any) -> Dict[str, Any]:
        body = {"summary": summary, "dtstart": dtstart, "dtend": dtend,
                "all_day": all_day, "description": description, "location": location}
        body.update({k: v for k, v in extra.items() if v is not None})
        return self.post("/api/calendar/events", json_body=body)

    def update_event(self, uid: str, **fields: Any) -> Dict[str, Any]:
        return self.put(f"/api/calendar/events/{uid}", json_body=fields)

    def delete_event(self, uid: str) -> Any:
        return self.delete(f"/api/calendar/events/{uid}")

    def calendar_config(self) -> Dict[str, Any]:
        return self.get("/api/calendar/config")

    def save_calendar_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        return self.post("/api/calendar/config", json_body=config)

    def sync_calendar(self) -> Any:
        return self.post("/api/calendar/sync")

    def quick_parse_calendar(self, text: str) -> Any:
        return self.post("/api/calendar/quick-parse", json_body={"text": text})

    # ------------------------------------------------------------------ #
    # email
    # ------------------------------------------------------------------ #
    def email_accounts(self) -> List[Dict[str, Any]]:
        return (self.get("/api/email/accounts") or {}).get("accounts", [])

    def add_email_account(self, account: Dict[str, Any]) -> Dict[str, Any]:
        return self.post("/api/email/accounts", json_body=account)

    def update_email_account(self, account_id: str, account: Dict[str, Any]) -> Dict[str, Any]:
        return self.post("/api/email/accounts", params={"account_id": account_id}, json_body=account)

    def delete_email_account(self, account_id: str) -> Any:
        return self.delete(f"/api/email/accounts/{account_id}")

    def email_folders(self, account_id: str = "") -> List[Dict[str, Any]]:
        params = {"account_id": account_id} if account_id else None
        return (self.get("/api/email/folders", params=params) or {}).get("folders", [])

    def email_list(self, folder: str = "INBOX", limit: int = 50, offset: int = 0,
                   filter: str = "all", account_id: str = "") -> Dict[str, Any]:
        params: Dict[str, Any] = {"folder": folder, "limit": limit, "offset": offset, "filter": filter}
        if account_id:
            params["account_id"] = account_id
        return self.get("/api/email/list", params=params) or {}

    def email_read(self, uid: str, folder: str = "INBOX", account_id: str = "") -> Dict[str, Any]:
        params: Dict[str, Any] = {"folder": folder}
        if account_id:
            params["account_id"] = account_id
        return self.get(f"/api/email/read/{uid}", params=params) or {}

    def email_send(self, to: str, subject: str, body: str, cc: str = "", bcc: str = "",
                   account_id: str = "", body_html: str = "", attachments: Any = None) -> Dict[str, Any]:
        params = {"account_id": account_id} if account_id else None
        payload = {
            "to": to, "subject": subject, "body": body,
            "cc": cc or None, "bcc": bcc or None,
            "body_html": body_html or None,
            "attachments": attachments,
            "account_id": account_id or None,
        }
        return self.post("/api/email/send", params=params, json_body=payload)

    def email_search(self, query: str, folder: str = "INBOX", account_id: str = "") -> Any:
        params: Dict[str, Any] = {"q": query, "folder": folder}
        if account_id:
            params["account_id"] = account_id
        return self.get("/api/email/search", params=params)

    def email_mark_read(self, uid: str, folder: str = "INBOX", account_id: str = "") -> Any:
        return self.post("/api/email/mark-read", params=self._acct(account_id),
                         json_body={"uid": uid, "folder": folder})

    def email_mark_unread(self, uid: str, folder: str = "INBOX", account_id: str = "") -> Any:
        return self.post("/api/email/mark-unread", params=self._acct(account_id),
                         json_body={"uid": uid, "folder": folder})

    def email_delete(self, uid: str, folder: str = "INBOX", account_id: str = "") -> Any:
        return self.post("/api/email/delete", params=self._acct(account_id),
                         json_body={"uid": uid, "folder": folder})

    def email_move(self, uid: str, to_folder: str, from_folder: str = "INBOX",
                   account_id: str = "") -> Any:
        return self.post("/api/email/move", params=self._acct(account_id),
                         json_body={"uid": uid, "from_folder": from_folder, "to_folder": to_folder})

    def email_flag(self, uid: str, folder: str = "INBOX", account_id: str = "") -> Any:
        return self.post("/api/email/flag", params=self._acct(account_id),
                         json_body={"uid": uid, "folder": folder})

    def email_summarize(self, uid: str, folder: str = "INBOX", account_id: str = "") -> Any:
        return self.post("/api/email/summarize", params=self._acct(account_id),
                         json_body={"uid": uid, "folder": folder})

    @staticmethod
    def _acct(account_id: str) -> Optional[Dict[str, str]]:
        return {"account_id": account_id} if account_id else None

    # ------------------------------------------------------------------ #
    # gallery
    # ------------------------------------------------------------------ #
    def gallery_library(self, tag: str = "", album: str = "", search: str = "",
                        limit: int = 200, offset: int = 0) -> Dict[str, Any]:
        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        if tag:
            params["tag"] = tag
        if album:
            params["album"] = album
        if search:
            params["search"] = search
        return self.get("/api/gallery/library", params=params) or {}

    def gallery_stats(self) -> Dict[str, Any]:
        return self.get("/api/gallery/stats") or {}

    def gallery_tags(self) -> Any:
        return self.get("/api/gallery/tags")

    def gallery_albums(self) -> Any:
        return self.get("/api/gallery/albums")

    def gallery_item(self, image_id: str) -> Dict[str, Any]:
        return self.get(f"/api/gallery/{image_id}")

    def gallery_rename(self, image_id: str, name: str) -> Any:
        return self.post(f"/api/gallery/{image_id}/rename", data={"name": name})

    def gallery_rotate(self, image_id: str, degrees: int = 90) -> Any:
        return self.post(f"/api/gallery/{image_id}/rotate", data={"degrees": str(degrees)})

    def gallery_delete(self, image_id: str) -> Any:
        return self.delete(f"/api/gallery/{image_id}")

    def gallery_upload(self, paths: Sequence[str]) -> Any:
        files, handles = [], []
        try:
            for path in paths:
                handle = open(path, "rb")
                handles.append(handle)
                files.append(("files", (os.path.basename(path), handle, "application/octet-stream")))
            return self.post("/api/gallery/upload", files=files)
        finally:
            for handle in handles:
                handle.close()

    def generated_image_bytes(self, filename: str) -> bytes:
        return self.be.get(f"/api/generated-image/{filename}", expect="bytes")

    def gallery_file_bytes(self, path: str) -> bytes:
        return self.be.get(path, expect="bytes")

    # ------------------------------------------------------------------ #
    # research
    # ------------------------------------------------------------------ #
    def research_start(self, query: str, max_rounds: int = 0, model: str = "",
                       endpoint_id: str = "", max_time: int = 300) -> Dict[str, Any]:
        return self.post("/api/research/start", json_body={
            "query": query, "max_rounds": max_rounds or None,
            "model": model or None, "endpoint_id": endpoint_id or None,
            "max_time": max_time})

    def research_status(self, session_id: str) -> Dict[str, Any]:
        return self.get(f"/api/research/status/{session_id}")

    def research_stream(self, session_id: str) -> SseStream:
        return self.be.stream("GET", f"/api/research/stream/{session_id}",
                              headers={"Accept": "text/event-stream"})

    def research_report(self, session_id: str) -> Any:
        return self.get(f"/api/research/report/{session_id}")

    def research_result(self, session_id: str) -> Any:
        return self.post(f"/api/research/result/{session_id}")

    def research_cancel(self, session_id: str) -> Any:
        return self.post(f"/api/research/cancel/{session_id}")

    def research_library(self) -> Dict[str, Any]:
        return self.get("/api/research/library") or {}

    def research_active(self) -> Any:
        return self.get("/api/research/active")

    # ------------------------------------------------------------------ #
    # local models / cookbook / hardware fit
    # ------------------------------------------------------------------ #
    def cookbook_state(self) -> Dict[str, Any]:
        return self.get("/api/cookbook/state") or {}

    def hwfit_system(self) -> Dict[str, Any]:
        return self.get("/api/hwfit/system") or {}

    def hwfit_models(self) -> Dict[str, Any]:
        return self.get("/api/hwfit/models") or {}

    def hwfit_profiles(self, model: str = "") -> Dict[str, Any]:
        params = {"model": model} if model else None
        return self.get("/api/hwfit/profiles", params=params) or {}

    def cached_models(self) -> Any:
        return self.get("/api/model/cached")

    def download_model(self, repo_id: str, include: str = "", hf_token: str = "") -> Any:
        return self.post("/api/model/download", json_body={
            "repo_id": repo_id, "include": include or None, "hf_token": hf_token or None})

    def serve_model(self, repo_id: str, cmd: str) -> Any:
        return self.post("/api/model/serve", json_body={"repo_id": repo_id, "cmd": cmd})

    def kill_pid(self, pid: int) -> Any:
        return self.post("/api/cookbook/kill-pid", json_body={"pid": pid})

    def local_llama_group(self) -> Dict[str, Any]:
        """Hardware-fit group the launcher would start (no downloads)."""
        return self.get("/api/hwfit/models") or {}

    # ------------------------------------------------------------------ #
    # memory / skills / mcp / personal docs
    # ------------------------------------------------------------------ #
    def memories(self) -> List[Dict[str, Any]]:
        return (self.get("/api/memory") or {}).get("memory", [])

    def add_memory(self, content: str, category: str = "fact") -> Any:
        return self.post("/api/memory/add", json_body={"content": content, "category": category})

    def search_memory(self, query: str) -> Any:
        return self.get("/api/memory/search", params={"q": query})

    def delete_memory(self, memory_id: str) -> Any:
        return self.delete(f"/api/memory/{memory_id}")

    def skills(self) -> Dict[str, Any]:
        return self.get("/api/skills") or {}

    def skill(self, skill_id: str) -> Any:
        return self.get(f"/api/skills/{skill_id}")

    def delete_skill(self, skill_id: str) -> Any:
        return self.delete(f"/api/skills/{skill_id}")

    def toggle_skill(self, skill_id: str, enabled: bool) -> Any:
        return self.patch(f"/api/skills/{skill_id}", json_body={"enabled": enabled})

    def mcp_servers(self) -> List[Dict[str, Any]]:
        return self.get("/api/mcp/servers") or []

    def mcp_tools(self) -> Any:
        return self.get("/api/mcp/tools")

    def add_mcp_server(self, server: Dict[str, Any]) -> Any:
        return self.post("/api/mcp/servers", json_body=server)

    def delete_mcp_server(self, server_id: str) -> Any:
        return self.delete(f"/api/mcp/servers/{server_id}")

    def personal_docs(self) -> Dict[str, Any]:
        return self.get("/api/personal") or {}

    def add_personal_directory(self, path: str) -> Any:
        return self.post("/api/personal/add_directory", json_body={"path": path})

    def remove_personal_directory(self, path: str) -> Any:
        return self.delete("/api/personal/remove_directory", params={"path": path})

    # ------------------------------------------------------------------ #
    # settings / prefs / integrations / misc
    # ------------------------------------------------------------------ #
    def pref(self, key: str) -> Any:
        data = self.get(f"/api/prefs/{key}") or {}
        return data.get("value") if isinstance(data, dict) else data

    def set_pref(self, key: str, value: Any) -> Any:
        return self.put(f"/api/prefs/{key}", json_body={"value": value})

    def presets(self) -> Dict[str, Any]:
        return self.get("/api/presets") or {}

    def search_config(self) -> Dict[str, Any]:
        return self.get("/api/search/config") or {}

    def search_query(self, query: str) -> Any:
        return self.get("/api/search/query", params={"q": query})

    def diagnostics_services(self) -> Dict[str, Any]:
        return self.get("/api/diagnostics/services") or {}

    def diagnostics_logs(self, limit: int = 300) -> Any:
        return self.be.get("/api/diagnostics/logs", params={"limit": limit}, expect="text")

    def db_stats(self) -> Dict[str, Any]:
        return self.get("/api/db/stats") or {}

    def version(self) -> str:
        try:
            return str((self.get("/api/version") or {}).get("version", ""))
        except BackendError:
            return ""

    def api_tokens(self) -> List[Dict[str, Any]]:
        return self.get("/api/tokens") or []

    def create_api_token(self, name: str, scopes: Sequence[str]) -> Any:
        return self.post("/api/tokens", json_body={"name": name, "scopes": list(scopes)})

    def delete_api_token(self, token_id: str) -> Any:
        return self.delete(f"/api/tokens/{token_id}")

    def webhooks(self) -> List[Dict[str, Any]]:
        return self.get("/api/webhooks") or []

    def workspace_browse(self, path: str = "") -> Dict[str, Any]:
        params = {"path": path} if path else None
        return self.get("/api/workspace/browse", params=params) or {}


_API: Optional[Api] = None


def get_api() -> Api:
    global _API
    if _API is None:
        _API = Api()
    return _API
