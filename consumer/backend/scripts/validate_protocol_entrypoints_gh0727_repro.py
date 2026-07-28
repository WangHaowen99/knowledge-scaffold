#!/usr/bin/env python3
"""Validate ACP/MCP/A2A entrypoints against the imported gh0727-repro workspace.

Run this inside the backend runtime container:

    PROJECT_ROOT=/app/.onto-runtime PYTHONPATH=/tmp/backend-current \
      python3 scripts/validate_protocol_entrypoints_gh0727_repro.py
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib import error, request


WORKSPACE_ID = os.environ.get("VALIDATE_WORKSPACE_ID", "gh0727-repro")
ONTOLOGY_ID = os.environ.get("VALIDATE_ONTOLOGY_ID", "gh0727-agent")
AUTH_SERVER_URL = os.environ.get("AUTH_SERVER_URL", "").rstrip("/")
AUTH_SERVER_ADMIN_TOKEN = os.environ.get("AUTH_SERVER_ADMIN_TOKEN", "").strip()
REQUEST_TIMEOUT = float(os.environ.get("VALIDATE_HTTP_TIMEOUT", "300"))
MCP_VALIDATION_SECRET = os.environ.get(
    "VALIDATE_MCP_TOKEN_SECRET",
    "gh0727-repro-validation-secret-0123456789abcdef",
)

TREASURY_KEYWORDS = (
    "财资管理",
    "资金池",
    "多银行现金管理",
    "银企互联",
    "多银行账户",
)
BAD_PATTERNS = (
    "[object Object]",
    "awaiting_input",
    "希望如何处理",
    "请选择处理方式",
)


def _read_real_request() -> str:
    execution_path = (
        Path(os.environ.get("PROJECT_ROOT", ""))
        / "workspaces"
        / WORKSPACE_ID
        / "runtime"
        / "users"
        / "admin"
        / "executions"
        / "mw-eval-27c87825"
        / "execution.json"
    )
    data = json.loads(execution_path.read_text(encoding="utf-8"))
    return str((data.get("turns") or [])[-1].get("user_request") or "").strip()


def _http_json(
    method: str,
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, Any]:
    body = None
    merged_headers = {"Accept": "application/json"}
    if headers:
        merged_headers.update(headers)
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        merged_headers["Content-Type"] = "application/json"
    req = request.Request(url, data=body, headers=merged_headers, method=method)
    try:
        with request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw


def _http_text(
    method: str,
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, str]:
    body = None
    merged_headers = {"Accept": "*/*"}
    if headers:
        merged_headers.update(headers)
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        merged_headers["Content-Type"] = "application/json"
    req = request.Request(url, data=body, headers=merged_headers, method=method)
    try:
        with request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def _http_sse_until_terminal(
    method: str,
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    terminal_events: tuple[str, ...] = ("run.completed", "run.failed", "run.awaiting"),
) -> tuple[int, list[tuple[str, Any]], str]:
    body = None
    merged_headers = {"Accept": "text/event-stream"}
    if headers:
        merged_headers.update(headers)
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        merged_headers["Content-Type"] = "application/json"
    req = request.Request(url, data=body, headers=merged_headers, method=method)
    with request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        current_event = ""
        events: list[tuple[str, Any]] = []
        raw_lines: list[str] = []
        while True:
            line = resp.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip("\r\n")
            raw_lines.append(text)
            if not text:
                current_event = ""
                continue
            if text.startswith("event:"):
                current_event = text.split(":", 1)[1].strip()
                continue
            if not text.startswith("data:"):
                continue
            payload_text = text.split(":", 1)[1].lstrip()
            try:
                parsed = json.loads(payload_text)
            except json.JSONDecodeError:
                parsed = payload_text
            events.append((current_event, parsed))
            if current_event in terminal_events:
                break
        return resp.status, events, "\n".join(raw_lines)


def _parse_sse_events(raw: str) -> list[tuple[str, Any]]:
    events: list[tuple[str, Any]] = []
    current_event = ""
    for line in str(raw or "").splitlines():
        text = line.rstrip("\r")
        if not text:
            current_event = ""
            continue
        if text.startswith("event:"):
            current_event = text.split(":", 1)[1].strip()
            continue
        if not text.startswith("data:"):
            continue
        payload_text = text.split(":", 1)[1].lstrip()
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            payload = payload_text
        events.append((current_event, payload))
    return events


def _build_test_client(repo_root: Path):
    os.environ.setdefault("MCP_TOKEN_SECRET", MCP_VALIDATION_SECRET)
    os.environ.setdefault("MCP_TOKEN_ALGORITHM", "HS256")
    os.environ.setdefault("MCP_TOKEN_ISSUER", "meta-wise")
    os.environ.setdefault("MCP_TOKEN_AUDIENCE", "meta-wise-mcp")
    os.environ.setdefault(
        "MCP_TOKEN_STORE_PATH",
        str(repo_root / ".pytest-cache" / "mcp-validation-tokens.json"),
    )
    sys.path.insert(0, str(repo_root))
    import app as app_module

    app_module.run_startup_migrations = lambda: {}
    app = app_module.create_app()
    return app.test_client()


def _create_temp_user() -> tuple[str, str]:
    if not AUTH_SERVER_URL or not AUTH_SERVER_ADMIN_TOKEN:
        raise RuntimeError("AUTH_SERVER_URL / AUTH_SERVER_ADMIN_TOKEN must be available inside the container")
    username = f"proto_validate_{uuid.uuid4().hex[:8]}"
    initial_password = f"ProtoInit#{uuid.uuid4().hex[:10]}A!"
    final_password = f"ProtoFinal#{uuid.uuid4().hex[:10]}B!"
    status, payload = _http_json(
        "POST",
        f"{AUTH_SERVER_URL}/admin/users",
        payload={"username": username, "password": initial_password, "role": "admin"},
        headers={"X-Admin-Token": AUTH_SERVER_ADMIN_TOKEN},
    )
    if status != 201:
        raise RuntimeError(f"failed to create temp user: status={status}, payload={payload}")
    status, payload = _http_json(
        "PUT",
        f"{AUTH_SERVER_URL}/admin/change-password",
        payload={
            "username": username,
            "old_password": initial_password,
            "new_password": final_password,
        },
        headers={"X-Admin-Token": AUTH_SERVER_ADMIN_TOKEN},
    )
    if status != 200:
        raise RuntimeError(f"failed to finalize temp user password: status={status}, payload={payload}")
    return username, final_password


def _delete_temp_user(username: str) -> None:
    if not username:
        return
    _http_json(
        "DELETE",
        f"{AUTH_SERVER_URL}/admin/users/{username}",
        headers={"X-Admin-Token": AUTH_SERVER_ADMIN_TOKEN},
    )


def _login(client, username: str, password: str) -> str:
    response = client.post("/auth/login", json={"username": username, "password": password})
    payload = response.get_json(silent=True) or {}
    if response.status_code != 200 or not isinstance(payload, dict) or not payload.get("token"):
        raise RuntimeError(f"login failed: status={response.status_code}, payload={payload}")
    return str(payload["token"])


def _create_mcp_token_for_owner() -> tuple[str, str]:
    from integration.mcp.token_service import create_mcp_token

    result = create_mcp_token(
        username="admin",
        workspace_id=WORKSPACE_ID,
        name="protocol-validate",
        scopes=["mcp:chat"],
        expires_in=3600,
    )
    return str(result["token"]), str((result.get("item") or {}).get("token_id") or "")


def _revoke_mcp_token(token_id: str) -> None:
    if not token_id:
        return
    from integration.mcp.token_service import revoke_mcp_token

    revoke_mcp_token(username="admin", workspace_id=WORKSPACE_ID, token_id=token_id)


def _assert_answer(protocol: str, text: str) -> None:
    normalized = str(text or "")
    if not normalized.strip():
        raise AssertionError(f"{protocol}: empty answer")
    for pattern in BAD_PATTERNS:
        if pattern in normalized:
            raise AssertionError(f"{protocol}: bad pattern found: {pattern}")
    if not any(keyword in normalized for keyword in TREASURY_KEYWORDS):
        raise AssertionError(f"{protocol}: expected treasury keywords not found")


def _validate_acp(client, jwt_token: str, user_request: str) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {jwt_token}",
        "X-Workspace-Id": WORKSPACE_ID,
    }
    response = client.post(
        "/acp/runs/execute_stream",
        json={
            "messages": [{"role": "user", "parts": [{"type": "text", "content": user_request}]}],
            "context": {
                "session_id": f"proto-acp-{uuid.uuid4().hex[:8]}",
                "selected_ontology_id": ONTOLOGY_ID,
                "agent_mode": "quick",
            },
        },
        headers=headers,
        buffered=True,
    )
    raw = response.get_data(as_text=True)
    if response.status_code != 200:
        raise RuntimeError(f"ACP execute_stream failed: status={response.status_code}, body={raw[:1200]}")
    events = _parse_sse_events(raw)
    event_names = [name for name, _payload in events]
    if "run.awaiting" in event_names:
        raise AssertionError("ACP: unexpectedly entered run.awaiting")
    if "run.failed" in event_names:
        raise AssertionError(f"ACP: run.failed received: {events}")
    if "run.completed" not in event_names:
        raise AssertionError(f"ACP: run.completed not found: {event_names}")
    answer_chunks = [
        str(payload.get("text") or "")
        for name, payload in events
        if name in {"answer", "message_delta"} and isinstance(payload, dict)
    ]
    answer = "".join(answer_chunks)
    _assert_answer("ACP", answer)
    return {"status": "completed", "answer_preview": answer[:600]}


def _validate_mcp(client, jwt_token: str, user_request: str) -> dict[str, Any]:
    del jwt_token
    mcp_token, token_id = _create_mcp_token_for_owner()
    try:
        response = client.post(
            "/agentic/mcp/internal/chat",
            json={
                "request": user_request,
                "session_id": f"proto-mcp-{uuid.uuid4().hex[:8]}",
                "context": {
                    "selected_ontology_id": ONTOLOGY_ID,
                    "agent_mode": "quick",
                },
            },
            headers={
                "Authorization": f"Bearer {mcp_token}",
                "X-Workspace-Id": WORKSPACE_ID,
            },
        )
        payload = response.get_json(silent=True) or {}
        if response.status_code != 200:
            raise RuntimeError(f"MCP chat failed: status={response.status_code}, payload={payload}")
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise AssertionError(f"MCP: expected ok=true, got {payload}")
        answer = str(((payload.get("result") or {}).get("content")) or "")
        _assert_answer("MCP", answer)
        return {
            "status": (payload.get("result") or {}).get("status"),
            "answer_preview": answer[:600],
        }
    finally:
        _revoke_mcp_token(token_id)


def _validate_a2a(client, jwt_token: str, user_request: str) -> dict[str, Any]:
    response = client.post(
        "/a2a/tasks/stream",
        json={
            "message": user_request,
            "session_id": f"proto-a2a-{uuid.uuid4().hex[:8]}",
            "context": {
                "selected_ontology_id": ONTOLOGY_ID,
                "agent_mode": "quick",
            },
        },
        headers={
            "Authorization": f"Bearer {jwt_token}",
            "X-Workspace-Id": WORKSPACE_ID,
        },
        buffered=True,
    )
    body = response.get_data(as_text=True)
    if response.status_code != 200:
        raise RuntimeError(f"A2A stream failed: status={response.status_code}, body={body[:800]}")
    if "event: task.update" not in body:
        raise AssertionError("A2A: expected task.update events")
    if any(pattern in body for pattern in BAD_PATTERNS):
        raise AssertionError("A2A: bad pattern found in stream body")
    if not any(keyword in body for keyword in TREASURY_KEYWORDS):
        raise AssertionError("A2A: expected treasury keywords not found in stream body")
    return {"status": "completed", "body_preview": body[:800]}


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    user_request = _read_real_request()
    temp_user = ""
    temp_password = ""
    try:
        client = _build_test_client(repo_root)
        temp_user, temp_password = _create_temp_user()
        jwt_token = _login(client, temp_user, temp_password)
        summary = {
            "workspace_id": WORKSPACE_ID,
            "ontology_id": ONTOLOGY_ID,
            "request_preview": user_request[:160],
            "acp": _validate_acp(client, jwt_token, user_request),
            "mcp": _validate_mcp(client, jwt_token, user_request),
            "a2a": _validate_a2a(client, jwt_token, user_request),
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    finally:
        if temp_user:
            _delete_temp_user(temp_user)


if __name__ == "__main__":
    raise SystemExit(main())
