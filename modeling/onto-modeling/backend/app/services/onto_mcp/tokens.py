"""Local MCP credentials, stored outside project exports, with revocation and ACL checks."""
import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from .definitions import SCOPES, TOOLS
from .errors import McpError

SERVER = {"name": "knowledge-scaffold-onto-mcp", "version": "0.1.0"}
VERSIONS = ["2026-07-28", "2025-11-25", "2025-06-18"]


@contextmanager
def database(manager):
    path = manager.workspaces_dir.parent / "onto-mcp.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS tokens (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL, digest TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL, subject TEXT NOT NULL, issued_by TEXT NOT NULL,
        scopes TEXT NOT NULL, created REAL NOT NULL, expires REAL, used REAL, revoked REAL)""")
    path.chmod(0o600)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else None


def summary(row):
    return {"id": row["id"], "name": row["name"], "scopes": json.loads(row["scopes"]),
            "subjectUsername": row["subject"], "issuedBy": row["issued_by"],
            "createdAt": iso(row["created"]), "expiresAt": iso(row["expires"]),
            "lastUsedAt": iso(row["used"])}


def issue(manager, meta, body, user):
    name = str(body.get("name", "")).strip()
    scopes = body.get("scopes", [SCOPES[0]])
    ttl = body.get("expires_in_seconds", 86400)
    if not name or len(name) > 120:
        raise McpError("INVALID_INPUT", "Token 名称或 scope 无效")
    if not isinstance(scopes, list) or not scopes or any(s not in SCOPES for s in scopes):
        raise McpError("INVALID_INPUT", "Token 名称或 scope 无效")
    if ttl is not None:
        if isinstance(ttl, bool) or not isinstance(ttl, int) or not 300 <= ttl <= 2592000:
            raise McpError("INVALID_INPUT", "Token 有效期必须在 300 至 2592000 秒之间")
    # An administrator delegates to this project's owner. Tokens cannot carry a global admin role.
    subject = meta.get("owner") if user.get("role") == "admin" and meta.get("owner") else user.get("sub", "")
    raw, tid, now = secrets.token_urlsafe(48), secrets.token_hex(16), time.time()
    with database(manager) as conn:
        conn.execute("INSERT INTO tokens VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)",
                     (tid, meta["id"], hashlib.sha256(raw.encode()).hexdigest(), name, subject,
                      user.get("sub", ""), json.dumps(sorted(set(scopes))), now, now + ttl if ttl else None))
        row = conn.execute("SELECT * FROM tokens WHERE id=?", (tid,)).fetchone()
    return {"endpoint_id": meta["id"], "token": raw, "token_summary": summary(row)}


def listing(manager, project_id):
    with database(manager) as conn:
        rows = conn.execute("SELECT * FROM tokens WHERE project_id=? AND revoked IS NULL ORDER BY created DESC", (project_id,)).fetchall()
    return {"tokens": [summary(row) for row in rows]}


def revoke(manager, project_id, token_id):
    with database(manager) as conn:
        changed = conn.execute("UPDATE tokens SET revoked=? WHERE project_id=? AND id=? AND revoked IS NULL",
                               (time.time(), project_id, token_id)).rowcount
    if not changed:
        raise McpError("MCP_TOKEN_NOT_FOUND", "Token 不存在或已吊销", 404)
    return {"ok": True}


def verify(manager, project_id, token, toolset):
    if toolset not in ("read", "change"):
        raise McpError("INVALID_INPUT", "未知工具集", 404)
    if not token:
        raise McpError("MCP_AUTH_REQUIRED", "缺少 MCP Token", 401)
    with database(manager) as conn:
        row = conn.execute("SELECT * FROM tokens WHERE project_id=? AND digest=? AND revoked IS NULL",
                           (project_id, hashlib.sha256(token.encode()).hexdigest())).fetchone()
        if row is None or (row["expires"] is not None and row["expires"] <= time.time()):
            raise McpError("MCP_AUTH_INVALID", "MCP Token 无效或已过期", 401)
        meta = manager.get_project(project_id)
        allowed = manager.can_write(meta, row["subject"], False) if toolset == "change" else manager.can_access(meta, row["subject"], False)
        if not allowed or meta.get("archived") and toolset == "change":
            raise McpError("MCP_PROJECT_FORBIDDEN", "Token 所属用户已无权执行此项目操作", 403)
        scopes = json.loads(row["scopes"])
        if not set(scopes).intersection(SCOPES[:1] if toolset == "read" else SCOPES[1:]):
            raise McpError("MCP_SCOPE_FORBIDDEN", "Token 不具备当前工具集权限", 403)
        conn.execute("UPDATE tokens SET used=? WHERE id=?", (time.time(), row["id"]))
    return {"token_id": row["id"], "username": row["subject"], "scopes": scopes, "project_id": project_id}


def status(project_id, base):
    base = base.rstrip("/")
    return {"configured": True, "enabled": True, "running": True, "detail": "Onto 本地 MCP 服务运行中",
            "project_id": project_id, "endpoint_id": project_id, "transport": "both",
            "server_name": SERVER["name"], "server_version": SERVER["version"], "protocol_versions": VERSIONS,
            "protocol_compatibility": True, "base_url": base,
            "scope_catalog": [{"scope": scope, "description": label} for scope, label in zip(SCOPES, ("读取本体", "校验修改", "保存修改"))],
            "token_policy": {"min_ttl_seconds": 300, "default_ttl_seconds": 86400, "max_ttl_seconds": 2592000, "supports_non_expiring": True},
            "urls": [{"toolset": group, "transport": transport, "url": f"{base}/api/onto-mcp/{project_id}/{group}/{suffix}"}
                     for group in ("read", "change") for transport, suffix in (("streamable-http", "mcp"), ("sse", "sse"))],
            "toolsets": [{"name": group, "scope_requirement": "all" if group == "read" else "any",
                          "accepted_scopes": list(SCOPES[:1] if group == "read" else SCOPES[1:]),
                          "required_scopes": list(SCOPES[:1] if group == "read" else SCOPES[1:]),
                          "tools": [name for name, tool in TOOLS.items() if tool["toolset"] == group]} for group in ("read", "change")]}
