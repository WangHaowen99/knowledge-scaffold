"""旧路径兼容中间件（API 契约 1.2、第 6 章；方案 A14）。

位于鉴权中间件之前：把旧路径改写成 v1 路径（必要时连方法一起改）后交给 v1 处理器，
响应加 Deprecation、Sunset、Link 头；错误体额外带 error（值同 message）。只改路径，不做
请求与响应的形状兼容。已删除的旧接口直接回 410 endpoint_removed。

永久保留、不进兼容期的旧路径（/api/health、/api/onto-mcp/...、/drawio/*）不经这里改写。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Callable
from urllib.parse import parse_qs, quote

from ..kernel import metamodel

SUNSET = "Sat, 01 May 2027 00:00:00 GMT"
_P = r"(?P<id>[^/]+)"
_BODY_LIMIT = 4 * 1024 * 1024  # 需要看请求体才能改写的几个旧接口，请求体都很小

# 没指定对话时的占位：v1 会话接口把它解析成引擎当前绑定的对话（没有就取最近更新的一条）
CURRENT_CONVERSATION = "-"


def entry_id_for_path(path: str, project_id: str | None = None) -> str:
    """旧接口按文件路径定位的条目 → v1 的 entry_id（<kind>:<key>）；认不出时原样返回路径。

    给了项目时按本项目锁定版本的元模型认目录（B3 合并遗留），项目不存在时用镜像版本。
    """
    path = (path or "").strip().lstrip("/")
    return _model_for(project_id).entry_key(path) or path


def _model_for(project_id: str | None):
    if not project_id:
        return metamodel.current()
    try:
        from . import deps

        root = deps.workspaces.project_root(project_id)
    except Exception:  # noqa: BLE001 - 项目不存在（改写后由 v1 处理器回 404）
        return metamodel.current()
    return metamodel.for_project(root) if root.is_dir() else metamodel.current()


@dataclass(frozen=True)
class Rule:
    method: str
    pattern: re.Pattern
    # 返回 (新方法, 新路径)；返回 None 表示该旧接口已删除（410）
    target: Callable[[dict, dict, dict], tuple[str, str] | None]
    needs_body: bool = False
    successor: Callable[[dict, dict], str | None] | None = None  # 410 时给出的替代接口


def _same(method: str, template: str):
    return lambda groups, query, body: (method, template.format(**groups))


def _removed(successor: str | None = None):
    def target(groups, query, body):
        return None

    def succ(groups, query):
        return successor.format(**groups) if successor else None

    return target, succ


def _q(query: dict, name: str) -> str:
    values = query.get(name) or [""]
    return values[0]


def _conversation(query: dict, body: dict | None = None) -> str:
    cid = (body or {}).get("conversation_id") if isinstance(body, dict) else None
    return quote(str(cid or _q(query, "conversation") or CURRENT_CONVERSATION), safe="")


def _export(groups, query, body):
    scope = _q(query, "scope") or "ontology"
    if scope == "workspace":
        return "GET", f"/api/v1/projects/{groups['id']}/transfer-package"
    return None


def _export_successor(groups, query):
    if (_q(query, "scope") or "ontology") == "package":
        return f"/api/v1/projects/{groups['id']}/releases"
    return None


def _entry(suffix: str, method: str | None = None):
    def target(groups, query, body):
        path = _q(query, "path") or ((body or {}).get("path") if isinstance(body, dict) else "") or ""
        entry = quote(entry_id_for_path(str(path), groups.get("id")), safe=":")
        return method or groups["_method"], f"/api/v1/projects/{groups['id']}/studio/entries/{entry}{suffix}"

    return target


def _report_items(groups, query, body):
    path = _q(query, "path")
    report_id = quote(path.removeprefix("reports/").removesuffix(".md"), safe="")
    return "GET", f"/api/v1/projects/{groups['id']}/reports/{report_id}/items"


def _session(suffix: str, method: str):
    def target(groups, query, body):
        return method, f"/api/v1/projects/{groups['id']}/conversations/{_conversation(query, body)}{suffix}"

    return target


def _trash_restore(groups, query, body):
    name = quote(str((body or {}).get("name") or ""), safe="") if isinstance(body, dict) else ""
    return "POST", f"/api/v1/ops/trash/{name or '-'}/restore"


def _admin_user_update(groups, query, body):
    if isinstance(body, dict) and body.get("password") is not None:
        return "POST", f"/api/v1/admin/users/{groups['user']}/reset-password"
    return "PATCH", f"/api/v1/admin/users/{groups['user']}"


def _r(method: str, pattern: str, target, needs_body: bool = False, successor=None) -> Rule:
    return Rule(method, re.compile(f"^{pattern}$"), target, needs_body, successor)


def _gone(method: str, pattern: str, successor: str | None = None) -> Rule:
    target, succ = _removed(successor)
    return _r(method, pattern, target, successor=succ)


_PROJ = f"/api/projects/{_P}"
_V1 = "/api/v1/projects/{id}"

RULES: tuple[Rule, ...] = (
    # ---- 6.1 routers/projects.py ----
    _r("GET", "/api/projects", _same("GET", "/api/v1/projects")),
    _r("POST", "/api/projects", _same("POST", "/api/v1/projects")),
    _r("POST", "/api/projects/import", _same("POST", "/api/v1/projects/import")),
    _r("POST", "/api/projects/transfer-import", _same("POST", "/api/v1/projects/import")),
    _gone("GET", f"{_PROJ}/members"),
    _gone("PUT", f"{_PROJ}/members"),
    _r("GET", _PROJ, _same("GET", _V1)),
    _r("PATCH", _PROJ, _same("PATCH", _V1)),
    _r("DELETE", _PROJ, _same("DELETE", _V1)),
    _r("POST", f"{_PROJ}/archive", _same("POST", f"{_V1}/archive")),
    _r("POST", f"{_PROJ}/clone", _same("POST", f"{_V1}/clone")),
    _r("GET", f"{_PROJ}/export", _export, successor=_export_successor),
    _r("GET", f"{_PROJ}/package/check", _same("GET", f"{_V1}/publish/check")),
    _r("GET", f"{_PROJ}/package/solver-contract", _same("GET", f"{_V1}/solver/contract")),
    _r("PUT", f"{_PROJ}/package/solver-contract", _same("PUT", f"{_V1}/solver/contract")),
    _r("POST", f"{_PROJ}/package/solver-contract/check", _same("POST", f"{_V1}/solver/contract/check")),
    _gone("GET", f"{_PROJ}/package/solver-validations/latest"),
    _gone("GET", f"{_PROJ}/package/solver-validations/cases"),
    _gone("PUT", f"{_PROJ}/package/solver-validations/cases"),
    _gone("POST", f"{_PROJ}/package/solver-validations"),
    _r("POST", f"{_PROJ}/objects/(?P<key>[^/]+)/data-source", _same("PUT", f"{_V1}/objects/{{key}}/data-source")),
    _r("GET", f"{_PROJ}/files", _same("GET", f"{_V1}/files/tree")),
    _r("GET", f"{_PROJ}/file", _same("GET", f"{_V1}/files/content")),
    _r("PUT", f"{_PROJ}/file", _same("PUT", f"{_V1}/files/content")),
    _r("GET", f"{_PROJ}/excel", _same("GET", f"{_V1}/files/table-preview")),
    _r("GET", f"{_PROJ}/file-at", _same("GET", f"{_V1}/files/at")),
    _r("GET", f"{_PROJ}/raw", _same("GET", f"{_V1}/files/raw")),
    _r("GET", f"{_PROJ}/search", _same("GET", f"{_V1}/search")),
    _r("GET", f"{_PROJ}/metrics", _same("GET", f"{_V1}/studio/stats")),
    _r("POST", f"{_PROJ}/resources", _same("POST", f"{_V1}/resources")),
    _r("GET", f"{_PROJ}/resource-meta", _same("GET", f"{_V1}/resources")),
    _r("PATCH", f"{_PROJ}/resource-meta", _same("PATCH", f"{_V1}/resources/purpose")),
    _r("DELETE", f"{_PROJ}/resources", _same("DELETE", f"{_V1}/resources")),
    _r("GET", f"{_PROJ}/mcp", _same("GET", f"{_V1}/mcp-servers")),
    _r("PUT", f"{_PROJ}/mcp", _same("PUT", f"{_V1}/mcp-servers")),
    # 旧版评测记录只读（ADR-0092）：读的旧接口指向 ES-09，写的没有替代
    _gone("GET", f"{_PROJ}/eval(?:/.*)?", f"{_V1}/legacy-evaluations"),
    *(_gone(method, f"{_PROJ}/eval(?:/.*)?") for method in ("POST", "PUT", "DELETE")),
    _r("GET", f"{_PROJ}/engine", _same("GET", f"{_V1}/engine")),
    _r("PUT", f"{_PROJ}/engine", _same("PUT", f"{_V1}/engine")),
    _r("GET", f"{_PROJ}/studio/graph", _same("GET", f"{_V1}/studio/graph")),
    _r("GET", f"{_PROJ}/studio/elements/(?P<eid>[^/]+)/flow", _same("GET", f"{_V1}/studio/entries/{{eid}}/flow")),
    _r("GET", f"{_PROJ}/studio/element", _entry("", "GET")),
    _gone("POST", f"{_PROJ}/studio/lease", f"{_V1}/scope"),
    _gone("DELETE", f"{_PROJ}/studio/lease", f"{_V1}/scope"),
    _r("GET", f"{_PROJ}/studio/draft", _entry("/draft", "GET")),
    _r("PUT", f"{_PROJ}/studio/draft", _entry("/draft", "PUT"), needs_body=True),
    _r("DELETE", f"{_PROJ}/studio/draft", _entry("/draft", "DELETE")),
    _r("POST", f"{_PROJ}/studio/submit", _entry("/submit", "POST"), needs_body=True),
    _r("GET", f"{_PROJ}/report-items", _report_items),
    _r("GET", f"{_PROJ}/query", _same("GET", f"{_V1}/query")),
    _r("GET", f"{_PROJ}/knowledge", _same("GET", f"{_V1}/knowledge")),
    # 知识条目的网页写入随知识沉淀包删除（ADR-0101），没有替代
    _gone("POST", f"{_PROJ}/knowledge"),
    _gone("DELETE", f"{_PROJ}/knowledge"),
    _r("GET", f"{_PROJ}/versions", _same("GET", f"{_V1}/versions")),
    _r("POST", f"{_PROJ}/versions/revert", _same("POST", f"{_V1}/versions/revert")),
    _r("POST", f"{_PROJ}/versions/tag", _same("POST", f"{_V1}/versions/tags")),
    _r("DELETE", f"{_PROJ}/versions/tag/(?P<tag>[^/]+)", _same("DELETE", f"{_V1}/versions/tags/{{tag}}")),
    # ---- 6.2 routers/sessions.py ----
    _r("GET", f"{_PROJ}/session/usage", _same("GET", f"{_V1}/usage")),
    _r("GET", f"{_PROJ}/session", _session("", "GET")),
    _r("GET", f"{_PROJ}/session/conversations", _same("GET", f"{_V1}/conversations")),
    _r("POST", f"{_PROJ}/session/conversations", _same("POST", f"{_V1}/conversations")),
    _r("PATCH", f"{_PROJ}/session/conversations/(?P<cid>[^/]+)", _same("PATCH", f"{_V1}/conversations/{{cid}}")),
    _r("DELETE", f"{_PROJ}/session/conversations/(?P<cid>[^/]+)", _same("DELETE", f"{_V1}/conversations/{{cid}}")),
    _r("POST", f"{_PROJ}/session/messages", _session("/messages", "POST"), needs_body=True),
    _r(
        "POST", f"{_PROJ}/session/permissions/(?P<rid>[^/]+)",
        lambda g, q, b: ("POST", f"/api/v1/projects/{g['id']}/conversations/{CURRENT_CONVERSATION}/permissions/{g['rid']}"),
    ),
    _r("POST", f"{_PROJ}/session/close", _session("/restart", "POST")),
    _r("POST", f"{_PROJ}/session/interrupt", _session("/interrupt", "POST")),
    _r("GET", f"{_PROJ}/session/stream", _session("/events", "GET")),
    # ---- 6.6 routers/onto_mcp.py（管理接口；传输接口永久保留，不改写） ----
    _r("GET", f"{_PROJ}/modeling-mcp", _same("GET", f"{_V1}/mcp")),
    _r("GET", f"{_PROJ}/modeling-mcp/tokens", _same("GET", f"{_V1}/mcp/tokens")),
    _r("POST", f"{_PROJ}/modeling-mcp/tokens", _same("POST", f"{_V1}/mcp/tokens")),
    _r("DELETE", f"{_PROJ}/modeling-mcp/tokens/(?P<tid>[^/]+)", _same("DELETE", f"{_V1}/mcp/tokens/{{tid}}")),
    # ---- 6.3 routers/auth_routes.py ----
    _r("GET", "/api/auth/status", _same("GET", "/api/v1/client-config")),
    _r("POST", "/api/auth/login", _same("POST", "/api/v1/auth/login")),
    _r("POST", "/api/auth/register", _same("POST", "/api/v1/auth/register")),
    _r("POST", "/api/auth/refresh", _same("POST", "/api/v1/auth/refresh")),
    _r("POST", "/api/auth/password", _same("PUT", "/api/v1/me/password")),
    # ---- 6.4 routers/settings.py ----
    _r("GET", "/api/settings/model", _same("GET", "/api/v1/admin/settings/model")),
    _r("PUT", "/api/settings/model", _same("PUT", "/api/v1/admin/settings/model/claude")),
    _r("PUT", "/api/settings/model/jiuwen", _same("PUT", "/api/v1/admin/settings/model/jiuwen")),
    _r("PUT", "/api/settings/model/context", _same("PUT", "/api/v1/admin/settings/model/claude")),
    _r("GET", "/api/settings/model/context-presets", _same("GET", "/api/v1/admin/settings/context-presets")),
    _r("PUT", "/api/settings/model/context-presets/custom", _same("PUT", "/api/v1/admin/settings/context-presets/custom")),
    _r(
        "POST", "/api/settings/model/context-presets/custom/disable",
        _same("POST", "/api/v1/admin/settings/context-presets/custom/disable"),
    ),
    _r("GET", "/api/settings/model/context-presets/match", _same("GET", "/api/v1/admin/settings/context-presets/match")),
    _r("PUT", "/api/settings/model/default-engine", _same("PUT", "/api/v1/admin/settings/default-engine")),
    _r("PUT", "/api/settings/model/personal-policy", _same("PUT", "/api/v1/admin/settings/personal-key-policy")),
    _r("GET", "/api/settings/model/me", _same("GET", "/api/v1/me/model-settings")),
    _r("PUT", "/api/settings/model/me/(?P<engine>[^/]+)", _same("PUT", "/api/v1/me/model-settings/{engine}")),
    _r("DELETE", "/api/settings/model/me/(?P<engine>[^/]+)", _same("DELETE", "/api/v1/me/model-settings/{engine}")),
    _r("GET", "/api/settings/runtime", _same("GET", "/api/v1/admin/settings/session")),
    _r("PUT", "/api/settings/runtime", _same("PUT", "/api/v1/admin/settings/session")),
    _r("GET", "/api/settings/sandbox", _same("GET", "/api/v1/admin/settings/sandbox")),
    _gone("GET", "/api/settings/assets", "/api/v1/ops/core-default"),
    _r("GET", "/api/settings/assets/backups", _same("GET", "/api/v1/ops/pack-repo/backups")),
    _r("POST", "/api/settings/assets/rollback", _same("POST", "/api/v1/ops/core-default/rollback")),
    # 资产包上传当作 core 新版本走包仓库的静态检查，通过后发布到 stable（契约第 6 章、方案 2.3 第 4 步）
    _r("POST", "/api/settings/assets/upload", _same("POST", "/api/v1/admin/packs/uploads")),
    _r("GET", "/api/settings/mcp", _same("GET", "/api/v1/admin/settings/mcp-servers")),
    _r("PUT", "/api/settings/mcp", _same("PUT", "/api/v1/admin/settings/mcp-servers")),
    _r("POST", "/api/settings/mcp/test", _same("POST", "/api/v1/admin/settings/mcp-servers/test")),
    # ---- 6.5 routers/admin.py ----
    _r("GET", "/api/admin/overview", _same("GET", "/api/v1/ops/overview")),
    _r("POST", "/api/admin/backup", _same("POST", "/api/v1/ops/backups")),
    _r("POST", f"/api/admin/sessions/{_P}/recycle", _same("POST", "/api/v1/ops/sessions/{id}/recycle")),
    _r("GET", "/api/admin/health", _same("GET", "/api/v1/ops/health")),
    _r("GET", "/api/admin/trash", _same("GET", "/api/v1/ops/trash")),
    _r("POST", "/api/admin/trash/restore", _trash_restore, needs_body=True),
    _r("GET", "/api/admin/audit", _same("GET", "/api/v1/ops/audit")),
    _r("PUT", f"/api/admin/projects/{_P}/owner", _same("PUT", "/api/v1/admin/projects/{id}/owner")),
    _r("GET", "/api/admin/users", _same("GET", "/api/v1/admin/users")),
    _r("PUT", "/api/admin/users/(?P<user>[^/]+)", _admin_user_update, needs_body=True),
    _r("DELETE", "/api/admin/users/(?P<user>[^/]+)", _same("DELETE", "/api/v1/admin/users/{user}")),
    _gone("GET", "/api/admin/modeling-cleanups"),
    _gone("POST", "/api/admin/modeling-cleanups/(?P<cid>[^/]+)/retry"),
)

# 永久保留、不改写的旧路径前缀；资产包上传等过渡期接口不在兼容表里，照原路径处理
PERMANENT_PREFIXES = ("/api/health", "/api/onto-mcp/", "/api/v1/")


def match(method: str, path: str) -> tuple[Rule, dict] | None:
    if not path.startswith("/api/") or path.startswith(PERMANENT_PREFIXES):
        return None
    for rule in RULES:
        if rule.method != method:
            continue
        found = rule.pattern.match(path)
        if found:
            return rule, {**found.groupdict(), "_method": method}
    return None


def resolve(method: str, path: str, query_string: str, body: dict | None = None):
    """旧路径 → ("rewrite", 新方法, 新路径) / ("removed", 替代接口) / None（不是兼容期旧路径）。"""
    found = match(method, path)
    if found is None:
        return None
    rule, groups = found
    query = parse_qs(query_string, keep_blank_values=True)
    target = rule.target(groups, query, body)
    if target is None:
        successor = rule.successor(groups, query) if rule.successor else None
        return ("removed", successor)
    return ("rewrite", target[0], target[1])


def _encode_path(path: str) -> bytes:
    return quote(path, safe="/:@!$&'()*+,;=-._~%").encode("ascii")


async def _read_body(receive) -> tuple[bytes, list[dict]]:
    chunks, messages = [], []
    more = True
    size = 0
    while more:
        message = await receive()
        messages.append(message)
        if message["type"] != "http.request":
            break
        chunk = message.get("body", b"")
        size += len(chunk)
        if size <= _BODY_LIMIT:
            chunks.append(chunk)
        more = message.get("more_body", False)
    return b"".join(chunks), messages


def _replay(messages: list[dict], receive):
    pending = list(messages)

    async def replayed():
        if pending:
            return pending.pop(0)
        return await receive()

    return replayed


class LegacyPathMiddleware:
    """纯 ASGI 中间件：SSE 与文件下载的流式响应原样透传，只在错误体上补 error 字段。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        method, path = scope["method"], scope["path"]
        found = match(method, path)
        if found is None:
            return await self.app(scope, receive, send)
        rule, _groups = found
        body = None
        if rule.needs_body:
            raw, messages = await _read_body(receive)
            receive = _replay(messages, receive)
            try:
                body = json.loads(raw.decode("utf-8")) if raw else None
            except ValueError:  # 含 UnicodeDecodeError、JSONDecodeError
                body = None
        query_string = scope.get("query_string", b"").decode("latin-1")
        outcome = resolve(method, path, query_string, body)
        if outcome[0] == "removed":
            return await _send_removed(send, outcome[1])
        _, new_method, new_path = outcome
        scope = dict(scope)
        scope["method"] = new_method
        scope["path"] = new_path
        scope["raw_path"] = _encode_path(new_path)
        scope.setdefault("state", {})
        scope["state"] = {**scope["state"], "legacy_path": path}
        link = f"<{new_path}>; rel=\"successor-version\""
        await self.app(scope, receive, _legacy_send(send, link))


async def _send_removed(send, successor: str | None) -> None:
    from .errors import body as error_body

    content = error_body("endpoint_removed", "该接口已删除", {"successor": successor})
    content["error"] = content["message"]
    payload = json.dumps(content, ensure_ascii=False).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(payload)).encode()),
        (b"deprecation", b"true"),
        (b"sunset", SUNSET.encode()),
    ]
    if successor:
        headers.append((b"link", f"<{successor}>; rel=\"successor-version\"".encode()))
    await send({"type": "http.response.start", "status": 410, "headers": headers})
    await send({"type": "http.response.body", "body": payload})


def _legacy_send(send, link: str):
    """给旧路径响应加弃用头；错误体（JSON）补 error 字段，需要缓冲，其余响应流式透传。"""
    state = {"buffer": None, "start": None}

    async def wrapped(message):
        kind = message.get("type")
        if kind == "http.response.start":
            headers = [
                (k, v) for k, v in message.get("headers", [])
                if k.lower() not in (b"deprecation", b"sunset", b"link")
            ]
            headers += [
                (b"deprecation", b"true"),
                (b"sunset", SUNSET.encode()),
                (b"link", link.encode("utf-8")),
            ]
            message = {**message, "headers": headers}
            content_type = dict((k.lower(), v) for k, v in headers).get(b"content-type", b"")
            # ASGI 的 response.start 必带 status；万一缺了不缓冲，原样交给服务器去报错
            if message.get("status", 0) >= 400 and content_type.startswith(b"application/json"):
                state["start"] = message
                state["buffer"] = bytearray()
                return
            return await send(message)
        buffer = state.get("buffer")
        if kind == "http.response.body" and buffer is not None:
            buffer.extend(message.get("body", b""))
            if message.get("more_body"):
                return
            payload = bytes(buffer)
            try:
                content = json.loads(payload)
                if isinstance(content, dict) and "message" in content and "error" not in content:
                    content["error"] = content["message"]
                    payload = json.dumps(content, ensure_ascii=False).encode("utf-8")
            except ValueError:
                pass
            start = state["start"]
            headers = [(k, v) for k, v in start["headers"] if k.lower() != b"content-length"]
            headers.append((b"content-length", str(len(payload)).encode()))
            await send({**start, "headers": headers})
            return await send({"type": "http.response.body", "body": payload})
        return await send(message)

    return wrapped
