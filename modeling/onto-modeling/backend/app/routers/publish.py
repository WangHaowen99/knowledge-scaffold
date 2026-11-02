"""发布（API 契约 3.20）：PB-01 发布检查、PB-02 设置对象数据来源、PB-03 发布、PB-04 发布记录、
PB-05 记录详情、PB-06 包清单、PB-07 下载。PB-05～PB-07 也接受服务令牌 releases.read（X-02、X-03）。

发布从主线提交出（缺省最新），变更集永远不发布；格式自动判定（启用求解契约即求解知识包）；
版本号 0.{n}.0，n 是主线版本号；同一提交重复发布得到逐字节相同的包。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from ..api import deps, views
from ..api.errors import ApiError
from ..governance import telemetry
from ..services import audit, feedback_inbox, mainline, ownership, publish, release_store, service_tokens
from ..services.service_tokens import SCOPE_RELEASES_READ
from ..services.workspace import WorkspaceError

router = APIRouter(prefix="/api/v1/projects/{project_id}", tags=["publish"])

FORMAT_LABELS = {"ontology": "普通知识包", "solver": "求解知识包"}


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    return header[7:].strip() if header.lower().startswith("bearer ") else ""


def _reader(request: Request, project_id: str) -> dict | None:
    """所有者或服务令牌（releases.read）；返回服务令牌摘要，所有者时为 None。"""
    if service_tokens.looks_like_service_token(_bearer(request)):
        token = service_tokens.verify(_bearer(request), SCOPE_RELEASES_READ, project_id)
        request.state.service_token = token
        deps.workspaces.project_root(project_id)
        return token
    ownership.require_owner(deps.workspaces, project_id, request)
    return None


def urls(project_id: str, version: str) -> dict:
    base = f"/api/v1/projects/{project_id}/releases/{version}"
    return {"manifest_url": f"{base}/manifest", "download_url": f"{base}/download"}


def _feedback_counts(root) -> dict[str, int]:
    folder = root / feedback_inbox.FEEDBACK_DIR
    counts: dict[str, int] = {}
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            version = json.loads(path.read_text(encoding="utf-8")).get("release_version")
        except (OSError, ValueError):
            continue
        if version:
            counts[version] = counts.get(version, 0) + 1
    return counts


def record_view(project_id: str, entry: dict, root, feedback: dict[str, int] | None = None) -> dict:
    """ReleaseRecord（契约 7.14），另带 project_id 与清单、下载地址（三份契约的下游直接用）。"""
    package = entry.get("package") or {}
    fmt = entry.get("format")
    return {
        "project_id": project_id,
        "version": entry["version"],
        "version_label": mainline.label(mainline.count(root, entry["commit"])),
        "commit": entry["commit"],
        "tag": entry["tag"],
        "at": deps.iso(entry.get("released_at")),
        "by": entry.get("released_by") or "",
        "format": fmt,
        "format_label": FORMAT_LABELS.get(fmt) if fmt else None,
        "gates": {
            "all_passed": not entry.get("degraded"),
            "degraded": bool(entry.get("degraded")),
            "unbound_objects": entry.get("unbound_objects") or [],
        },
        "manifest_attached": bool(package),
        "pulls": [
            {"at": deps.iso(pull.get("at")), "by_token": pull.get("by", ""), "variant": pull.get("variant", "standard")}
            for pull in entry.get("pulls") or []
        ],
        "feedback_count": (feedback or {}).get(entry["version"], 0),
        "size": package.get("size"),
        "sha256": package.get("sha256"),
        **urls(project_id, entry["version"]),
    }


def find_release(project_id: str, version: str) -> dict:
    for entry in publish.releases(deps.workspaces, project_id):
        if entry["version"] == version:
            return entry
    raise ApiError("release_not_found", f"版本 {version} 不存在", 404)


# ---- PB-01 发布检查、PB-02 数据来源 ----


def _issue_view(item: dict) -> dict:
    """BindingIssue（契约 7.14）。"""
    fixes = ["set_table", "set_none"] if item["kind"] != "declared_unverifiable" else ["mark_schema_material", "set_none"]
    return {
        "object": {"kind": "object", "id": f"object:{item['object']}", "key": item["object"], "name": item["object"]},
        "declared": item.get("declared") or None,
        "kind": item["kind"],
        "message": item["message"],
        "detail": None,
        "candidates": item.get("candidates") or [],
        "fixes": fixes,
        "revision": item.get("revision", ""),
        "blocking": item["blocking"],
    }


def _gate_view(gate: dict) -> dict:
    return {**gate, "findings": [views.finding(item) for item in gate["findings"]]}


def check_view(project_id: str, commit: str | None = None) -> dict:
    root = deps.workspaces.project_root(project_id)
    data = publish.package_binding_check(deps.workspaces, project_id, commit)
    gates = [_gate_view(gate) for gate in data["gates"]]
    # 流程关卡（挂在发布节点前面的：全局一致性、包声明的关卡，详设 6.8、B4d）排在平台门禁后面
    from ..services.flow import view as flow_view

    gates += [{**gate, "order": len(gates) + index + 1} for index, gate in enumerate(flow_view.publish_gates(root))]
    blocking = [gate for gate in gates if gate["blocking"]]
    last = data.get("last_publish")
    meta = deps.workspaces.get_project(project_id)
    return {
        "commit": data["commit"],
        "version_label": mainline.label(mainline.count(root, data["commit"])),
        "package_name": meta.get("package_name") or project_id,
        "format": data["format"],
        "gates": gates,
        "blocking_count": len(blocking),
        "binding": {
            "issues": [_issue_view(item) for item in data["issues"]],
            "warnings": data["warnings"],
            "tables": data["tables"],
            "table_count": data["table_count"],
            "objects": data["objects"],
            "bound_objects": data["bound_objects"],
            "table_resources": data["table_resources"],
            "truncated": data["truncated"],
        },
        "degradable": bool(blocking) and all(gate["degradable"] for gate in blocking),
        "uncommitted": {"count": data["uncommitted_count"], "paths": data["uncommitted"]},
        "last_release": _last_release(project_id, root, last),
        "checked_at": deps.iso(data["checked_at"]),
        "duration_ms": data["duration_ms"],
    }


def _last_release(project_id: str, root, last: dict | None) -> dict | None:
    if not last:
        return None
    try:
        return record_view(project_id, find_release(project_id, last["version"]), root)
    except ApiError:
        return None


@router.get("/publish/check")
def publish_check(project_id: str, request: Request, commit: str = "", refresh: bool = False):
    deps.require_owner(project_id, request)
    return check_view(project_id, commit or None)


@router.put("/objects/{key}/data-source")
async def set_data_source(project_id: str, key: str, request: Request):
    """发布前修复：对象「# 数据来源」改成现有数据表之一，或「无」；保存为一个版本。"""
    root = deps.require_writable(project_id, request)
    body = await deps.json_body(request)
    kind = body.get("kind")
    if kind == "anchor":
        # 数据库锚点要证据快照（E11～E14，P3）解析表名列名
        raise ApiError("anchor_not_found", "还没有可以解析锚点的证据快照", 404)
    if kind not in ("table", "none"):
        raise ApiError("invalid_argument", "kind 取值不合法", 400, {"fields": {"kind": "enum"}})
    table = str(body.get("value") or "") if kind == "table" else ""
    if kind == "table" and not table:
        raise ApiError("invalid_argument", "要指定数据表名", 400, {"fields": {"value": "required"}})
    saved = publish.set_object_data_source(
        deps.workspaces, project_id, key, table,
        base_revision=str(body.get("base_revision") or ""), author=deps.username(request),
    )
    state = mainline.state(root)
    return {
        "entry": {"kind": "object", "id": f"object:{key}", "key": key, "name": key},
        "revision": saved["revision"],
        "commit": saved["commit"] or state["head"],
        "version_label": state["version_label"],
        "binding": None,
    }


# ---- PB-03 发布 ----


@router.post("/releases")
async def create_release(project_id: str, request: Request):
    """门禁 → 渲染 → 留存 → 打 release/0.{n}.0 标签 → 写发布记录。降级发布只放宽数据绑定。"""
    root = deps.require_writable(project_id, request)
    body = await deps.json_body(request)
    commit = body.get("commit") or None
    degrade = bool(body.get("degrade"))
    check = check_view(project_id, commit)
    failing = [gate for gate in check["gates"] if gate["blocking"] and not (degrade and gate["degradable"])]
    if failing:
        telemetry.count("onto_check_failures_total", check="publish_gate")
        raise ApiError("gate_blocked", "门禁链有未通过项，不能发布", 422, {"gates": failing})
    try:
        record = publish.publish_package(
            deps.workspaces, project_id, allow_unbound=degrade, author=deps.actor(request), commit=commit,
        )
    except WorkspaceError as exc:
        if exc.code in ("invalid_commit",):
            raise
        # 门禁之后渲染才发现的问题（与门禁同口径，正常不会走到这里）
        raise ApiError("gate_blocked", str(exc), 422, {"gates": [], "reason": exc.code}) from exc
    audit.log(deps.actor(request), "POST", request.url.path, {"publish.release": record["version"], "commit": record["commit"]})
    return JSONResponse(status_code=201, content=record_view(project_id, find_release(project_id, record["version"]), root))


# ---- PB-04～PB-07 ----


@router.get("/releases")
def list_releases(project_id: str, request: Request, cursor: str = "", limit: int = 0):
    """PB-04：发布记录（只给所有者；服务令牌用 X-01）。"""
    root = deps.require_owner(project_id, request)
    feedback = _feedback_counts(root)
    items = [record_view(project_id, entry, root, feedback) for entry in publish.releases(deps.workspaces, project_id)]
    page = deps.page(items, cursor, limit, {"project_id": project_id})
    return {"items": page["items"], "next_cursor": page["next_cursor"]}


@router.get("/releases/{version}")
def get_release(project_id: str, version: str, request: Request):
    _reader(request, project_id)
    root = deps.workspaces.project_root(project_id)
    return record_view(project_id, find_release(project_id, version), root, _feedback_counts(root))


@router.get("/releases/{version}/manifest")
def get_release_manifest(project_id: str, version: str, request: Request):
    """PB-06：包清单——manifest.yaml、来源提交、包摘要与文件清单。"""
    _reader(request, project_id)
    path, record = publish.release_package(deps.workspaces, project_id, version)
    files, truncated = release_store.list_files(path)
    manifest = release_store.read_manifest(path) or {}
    return {
        "package_name": manifest.get("id") or project_id,
        "version": version,
        "schema_version": str(manifest.get("schema_version") or "0.1"),
        "source_commit": record.get("commit", ""),
        "released_at": deps.iso(record.get("released_at")),
        "packs": [],
        "flow_hash": "",
        "solver": None,
        "degraded": {"unbound_objects": record.get("unbound_objects") or []} if record.get("degraded") else None,
        "files": files,
        "truncated": truncated,
        "format": record.get("format"),
        "sha256": record.get("sha256", ""),
        "size": record.get("size"),
        "manifest": manifest,
    }


@router.get("/releases/{version}/download")
def download_release(project_id: str, version: str, request: Request, variant: str = "standard"):
    """PB-07：读发布时留存的包，同一版本同一变体永远是同一份字节；graph 是图模式变体。"""
    token = _reader(request, project_id)
    path, record = publish.release_package(deps.workspaces, project_id, version, variant)
    if token:
        actor = f"service:{token['name']}"
        release_store.record_pull(deps.workspaces.data_dir, project_id, version, actor, variant)
    else:
        actor = deps.actor(request)
    audit.log(actor, "GET", request.url.path, {"publish.download": version, "variant": variant})
    meta = deps.workspaces.get_project(project_id)
    suffix = "" if variant == "standard" else f"-{variant}"
    headers = {
        "X-Package-Version": version,
        "X-Package-Commit": record.get("commit", ""),
        "X-Package-Format": record.get("format") or "",
        "X-Package-Variant": variant,
        "X-Package-Sha256": record.get("sha256", ""),
    }
    return FileResponse(path, filename=f"{meta['name']}-{version}{suffix}.zip", media_type="application/zip", headers=headers)

