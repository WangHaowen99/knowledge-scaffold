"""三份契约接口（详细设计第 11 章「和评测服务之间的三份契约」，API 契约 3.20、3.21，ADR-0020）。

外部服务（评测服务）持服务令牌调用，不经登录；onto 不主动调用评测服务。

- 包出：X-01 跨项目发布列表；求解验证候选包（契约补充，见 _plan/contract-changes.md）；
  PB-05 发布记录、PB-06 包清单、PB-07 下载也接受服务令牌，在 routers/publish.py；
- 验证回执入：X-04；
- 反馈入：X-05。
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import FileResponse, JSONResponse

from ..services import audit, feedback_inbox, publish, service_tokens, solver_validation
from ..services.service_tokens import (
    SCOPE_FEEDBACK_WRITE,
    SCOPE_RECEIPTS_WRITE,
    SCOPE_RELEASES_READ,
    rfc3339,
)
from ..services.workspace import WorkspaceError
from ..api import deps

router = APIRouter(prefix="/api/v1", tags=["contracts"])

_PROJECT = r"/api/v1/projects/[^/]+"
# 只收服务令牌的接口：没带服务令牌也放行到接口，由接口回 401
_SERVICE_ONLY = (
    ("GET", re.compile(r"^/api/v1/contract/releases$")),
    ("GET", re.compile(rf"^{_PROJECT}/solver/candidate$")),
    ("POST", re.compile(rf"^{_PROJECT}/solver/receipts$")),
    ("POST", re.compile(rf"^{_PROJECT}/feedback$")),
)
# 所有者或服务令牌都能读：带服务令牌时不走登录校验，带登录 token 时照常校验
_OWNER_OR_SERVICE = (
    ("GET", re.compile(rf"^{_PROJECT}/releases/[^/]+(?:/manifest|/download)?$")),
)
RECEIPT_MAX_BYTES = 256 * 1024
CONTRACT_PAGE_DEFAULT, CONTRACT_PAGE_MAX = 100, 200
OWNER_PAGE_DEFAULT, OWNER_PAGE_MAX = 50, 200


def bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    return header[7:].strip() if header.lower().startswith("bearer ") else ""


def skips_login(method: str, path: str, token: str) -> bool:
    """鉴权中间件用：契约接口由接口自己验服务令牌，不做登录校验（API 契约 1.2 第 6 条）。"""
    if any(method == verb and pattern.match(path) for verb, pattern in _SERVICE_ONLY):
        return True
    return service_tokens.looks_like_service_token(token) and any(
        method == verb and pattern.match(path) for verb, pattern in _OWNER_OR_SERVICE
    )


def _service(request: Request, scope: str, project_id: str | None = None) -> dict:
    token = service_tokens.verify(bearer(request), scope, project_id)
    request.state.service_token = token
    return token


def _actor(token: dict) -> str:
    return f"service:{token['name']}"


async def _json_body(request: Request, limit: int) -> object:
    raw = await request.body()
    if len(raw) > limit:
        raise WorkspaceError(f"请求体不超过 {limit // 1024} KB", 413, "payload_too_large")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise WorkspaceError("请求体必须是 UTF-8 JSON", 400, "invalid_argument") from None


def _page(items: list, cursor: str, limit: int, default: int, maximum: int, binding: dict) -> dict:
    """游标分页（API 契约 1.6）：游标绑定筛选条件，条件变了再用旧游标返回 invalid_cursor。"""
    limit = max(1, min(int(limit or default), maximum))
    key = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()[:16]
    offset = 0
    if cursor:
        try:
            data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
            offset = int(data["o"])
            valid = data.get("b") == key and offset >= 0
        except (ValueError, KeyError, TypeError):
            valid = False
        if not valid:
            raise WorkspaceError("游标无法解析，或筛选条件变了", 400, "invalid_cursor")
    end = offset + limit
    next_cursor = None
    if end < len(items):
        next_cursor = base64.urlsafe_b64encode(json.dumps({"o": end, "b": key}).encode()).decode().rstrip("=")
    return {"items": items[offset:end], "next_cursor": next_cursor, "total": len(items)}


def _urls(project_id: str, version: str) -> dict:
    base = f"/api/v1/projects/{project_id}/releases/{version}"
    return {"manifest_url": f"{base}/manifest", "download_url": f"{base}/download"}


# ---- 包出 ----


@router.get("/contract/releases")
def list_contract_releases(
    request: Request, since: str = "", project_id: str = "", cursor: str = "", limit: int = 0,
):
    """X-01：令牌授权范围内全部项目的发布，按发布时间从新到旧。"""
    token = _service(request, SCOPE_RELEASES_READ, project_id or None)
    threshold = None
    if since:
        try:
            threshold = datetime.fromisoformat(since.replace("Z", "+00:00")).timestamp()
        except ValueError:
            raise WorkspaceError("since 必须是 RFC 3339 时间", 400, "invalid_argument") from None
    manager = deps.workspaces
    items = []
    # 只读元数据，不算项目进度：项目多时 list_projects 要逐个核对资料分析验收
    for root in manager._project_dirs():
        if not service_tokens.allows_project(token, root.name) or (project_id and root.name != project_id):
            continue
        name = manager._read_meta(root).get("name", "")
        for entry in publish.releases(manager, root.name):
            if threshold is not None and (entry["released_at"] or 0) <= threshold:
                continue
            items.append({
                "project_id": root.name,
                "package_name": name,
                "version": entry["version"],
                "commit": entry["commit"],
                "format": entry["format"],
                "released_at": entry["released_at"],
                "degraded": entry["degraded"],
                **_urls(root.name, entry["version"]),
            })
    items.sort(key=lambda item: (item["released_at"] or 0, item["project_id"], item["version"]), reverse=True)
    for item in items:
        item["released_at"] = rfc3339(item["released_at"])
    page = _page(
        items, cursor, limit, CONTRACT_PAGE_DEFAULT, CONTRACT_PAGE_MAX, {"since": since, "project_id": project_id},
    )
    return {"items": page["items"], "next_cursor": page["next_cursor"]}


@router.get("/projects/{project_id}/solver/candidate")
def download_solver_candidate(
    project_id: str, request: Request, background: BackgroundTasks, allow_unbound: bool = False,
):
    """求解验证候选包（契约补充）：HEAD 提交、manifest 标 draft；验证后经 X-04 回写结论。"""
    _service(request, SCOPE_RECEIPTS_WRITE, project_id)
    workdir = Path(tempfile.mkdtemp(prefix="onto-solver-candidate-"))
    background.add_task(shutil.rmtree, workdir, True)
    dest = workdir / "candidate.zip"
    try:
        info = publish.solver_candidate_package(deps.workspaces, project_id, dest, allow_unbound=allow_unbound)
    except Exception:
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    headers = {
        "X-Package-Version": info["version"],
        "X-Package-Commit": info["commit"],
        "X-Solver-Revision": info["revision"],
        "X-Solver-Contract-Digest": info["contract_digest"],
    }
    return FileResponse(
        dest, filename=f"{project_id}-{info['version']}-candidate.zip", media_type="application/zip",
        headers=headers, background=background,
    )


# ---- 验证回执入 ----


@router.post("/projects/{project_id}/solver/receipts")
async def post_solver_receipt(project_id: str, request: Request):
    """X-04：写入求解契约端到端验证回执，绑定求解契约修订号，供发布门禁检查。"""
    token = _service(request, SCOPE_RECEIPTS_WRITE, project_id)
    deps.workspaces.project_root(project_id)
    payload = await _json_body(request, RECEIPT_MAX_BYTES)
    record, created = solver_validation.receive_receipt(deps.workspaces, project_id, payload, _actor(token))
    if created:
        audit.log(_actor(token), "POST", request.url.path, {"contract.receipt": record["receipt_id"]})
    return JSONResponse(status_code=201 if created else 200, content=solver_validation.receipt_view(record))


# ---- 反馈入 ----


@router.post("/projects/{project_id}/feedback")
async def post_feedback(project_id: str, request: Request):
    """X-05：接收一批外部反馈，落到工作区 feedback/ 并单独提交，供反馈演进读取。"""
    token = _service(request, SCOPE_FEEDBACK_WRITE, project_id)
    deps.workspaces.project_root(project_id)
    payload = await _json_body(request, feedback_inbox.MAX_BODY_BYTES)
    result, created = feedback_inbox.receive(deps.workspaces, project_id, payload, _actor(token))
    if created:
        audit.log(_actor(token), "POST", request.url.path, {"contract.feedback": result["feedback_id"]})
    return JSONResponse(status_code=201 if created else 200, content=result)
