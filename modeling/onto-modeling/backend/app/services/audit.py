"""审计（详设 4.10.1、方案 E4、契约 1.12、7.16 AuditEntry）。

- 业务面与运维面的每个写请求在**响应之后**记一条（带状态码、trace id、来源 IP），由治理中间件写；
- 不经 HTTP 的专项事件（agent 提交、插件切换、熔断、登录……）由各模块调用 `record()`；
- 不记项目内容：只记路径、条目 key、动作和结果，不记正文、对话、字段值；
- 鉴权关闭时照常记，操作者为 local；
- 按日轮转：`DATA_DIR/audit/audit-YYYY-MM-DD.jsonl`；旧版单文件 `audit.jsonl` 保留为最早的一段，只读。
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

from .. import config

MAX_PATH_LENGTH = 200
MAX_SUMMARY_LENGTH = 300
TYPES = (
    "write", "mcp", "commit", "plugin", "evidence", "ops", "auth", "publish", "changeset",
    "autobuild", "user", "contract",
)
ACTOR_KINDS = ("user", "agent", "mcp", "service", "system")
_lock = threading.Lock()


def audit_dir() -> Path:
    return Path(config.DATA_DIR) / "audit"


def _legacy_file() -> Path:
    return Path(config.DATA_DIR) / "audit.jsonl"


def _file() -> Path:
    """当天的审计文件（UTC 日期）。"""
    return audit_dir() / f"audit-{time.strftime('%Y-%m-%d', time.gmtime())}.jsonl"


def files() -> list[Path]:
    """全部审计文件，从旧到新：旧版单文件在最前，其后按日期。"""
    result = [_legacy_file()] if _legacy_file().is_file() else []
    if audit_dir().is_dir():
        result += sorted(audit_dir().glob("audit-*.jsonl"))
    return result


def _summary(detail: dict | None) -> str:
    """摘要只取短的标量值（键、版本、用户名、计数），不带正文。"""
    parts = []
    for key, value in (detail or {}).items():
        if key == "action" or isinstance(value, (dict, list)):
            continue
        text = "" if value is None else str(value)
        if len(text) <= 80:
            parts.append(f"{key}={text}")
    return "，".join(parts)[:MAX_SUMMARY_LENGTH]


def record(
    *,
    type: str,  # noqa: A002  契约字段名
    action: str,
    actor: str,
    actor_kind: str = "user",
    method: str | None = None,
    path: str | None = None,
    status: int | None = None,
    project_id: str | None = None,
    summary: str = "",
    trailer: dict | None = None,
    ip: str | None = None,
    trace_id: str | None = None,
    detail: dict | None = None,
) -> None:
    """写一条审计（AuditEntry 形状）；落盘失败不影响业务。"""
    from ..governance import context

    entry = {
        "ts": time.time(),
        "actor": actor or "anonymous",
        "actor_kind": actor_kind if actor_kind in ACTOR_KINDS else "user",
        "type": type if type in TYPES else "write",
        "action": action,
        "method": method,
        "path": path[:MAX_PATH_LENGTH] if path else None,
        "status": status,
        "project_id": project_id or context.get("project") or None,
        "summary": (summary or _summary(detail))[:MAX_SUMMARY_LENGTH],
        "trailer": trailer or None,
        "ip": ip,
        "trace_id": trace_id or context.get("trace_id") or None,
    }
    if detail:
        entry["detail"] = detail
    try:
        target = _file()
        with _lock:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


# 写请求按路径归类审计类型（契约 7.16 AuditEntry.type）
TYPE_BY_PATH = (
    (re.compile(r"^/api/v1/ops/|^/api/admin/(?:backup|sessions|trash)"), "ops"),
    (re.compile(r"^/api(?:/v1)?/auth/|^/api/v1/me/password"), "auth"),
    (re.compile(r"^/api(?:/v1)?/admin/users|^/api/v1/admin/projects/[^/]+/owner|^/api/admin/projects/[^/]+/owner"), "user"),
    (re.compile(r"/(?:releases|publish)(?:/|$)"), "publish"),
    (re.compile(r"/changesets(?:/|$)"), "changeset"),
    (re.compile(r"/autobuild(?:/|$)"), "autobuild"),
    (re.compile(r"/(?:packs|plugins)(?:/|$)"), "plugin"),
    (re.compile(r"/evidence(?:/|$)"), "evidence"),
    (re.compile(r"^/api(?:/v1)?/onto-mcp/|/mcp(?:/|$)"), "mcp"),
)


def classify_path(path: str) -> str:
    """写请求按路径归类：运维、认证、用户管理、发布……；认不出是 write。"""
    return next((name for pattern, name in TYPE_BY_PATH if pattern.search(path or "")), "write")


_SERVICE_ACTOR = re.compile(r"^service:")


def log(user: str, method: str, path: str, detail: dict | None = None) -> None:
    """旧入口（专项审计）：detail 里的 action（或形如 `publish.release` 的键）就是动作名。

    这些调用都是请求处理器里额外写的专项记录；请求本身的默认记录由治理中间件在响应后写。
    """
    detail = dict(detail or {})
    action = detail.get("action") or next((key for key in detail if "." in key), None)
    actor = user or "anonymous"
    if _SERVICE_ACTOR.match(actor):
        actor_kind, kind = "service", "contract"
    elif method == "MCP":
        actor_kind, kind = "mcp", "mcp"
    elif actor == "system" or method == "MIGRATE":
        actor_kind, kind = "system", "write"
    else:
        actor_kind, kind = "user", "write"
    prefix = (action or "").split(".", 1)[0]
    if prefix in TYPES and kind not in ("contract", "mcp"):
        kind = prefix
    elif action == "project.set_owner":
        kind = "user"
    elif kind == "write" and path:
        kind = classify_path(path)
    project = detail.get("project") if isinstance(detail.get("project"), str) else None
    record(
        type=kind, action=action or (f"http.{method.lower()}" if method else "unknown"), actor=actor,
        actor_kind=actor_kind, method=method, path=path, project_id=project, detail=detail or None,
    )


def record_agent_commits(project_id: str, cwd, base: str, head: str, changes: dict) -> None:
    """一轮里 agent 自己的提交（详设 4.10.1「agent 提交」）：提交数、文件数与最新提交的 trailer，不记内容。"""
    import subprocess

    try:
        count = subprocess.run(["git", "rev-list", "--count", f"{base}..{head}"], cwd=cwd, capture_output=True,
                               text=True, timeout=15).stdout.strip()
        body = subprocess.run(["git", "log", "-1", "--format=%B", head], cwd=cwd, capture_output=True, text=True,
                              timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        count, body = "", ""
    trailer = {key: value.strip() for key, _sep, value in
               (line.partition(":") for line in body.splitlines() if line.startswith("Onto-"))}
    files = sum(len(changes.get(kind) or []) for kind in ("added", "modified", "deleted"))
    record(type="commit", action="agent.commit", actor="agent", actor_kind="agent", project_id=project_id,
           summary=f"commits={count or '?'}，files={files}，head={head[:12]}", trailer=trailer or None)


def read_all() -> list[tuple[int, dict]]:
    """全部审计记录：(全局序号, 原始记录)，新的在前。序号按文件从旧到新累计行号，只追加写所以稳定。"""
    entries: list[tuple[int, dict]] = []
    offset = 0
    for path in files():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for index, line in enumerate(lines):
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(raw, dict):
                entries.append((offset + index, raw))
        offset += len(lines)
    entries.reverse()
    return entries


def read(limit: int = 200) -> list[dict]:
    return [raw for _index, raw in read_all()[:limit]]
