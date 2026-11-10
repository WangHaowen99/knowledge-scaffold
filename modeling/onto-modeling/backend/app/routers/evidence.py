"""证据源（契约 3.19 EV-01～EV-34；`design/evidence-collection.md`；ADR-0082）。

实现在 `app/evidence/`：登记 `sources`、上传 `uploads`、任务 `jobs`（经网关 `collector`）、入库 `ingest`、索引 `index`、
检索 `query`。这里只做权限、参数和视图。

- 数据源是平台级的；项目侧接口（EV-11～EV-28）要项目所有者，且项目启用了系统逆向建模包（`rev`），否则 409 `pack_not_enabled`；
- 采集、检索只认本项目授权了的数据源（EV-11），没授权 403 `source_not_authorized`；
- EV-06、EV-07（凭据，F16）：明文立即转交网关，内核只存 `cred_ref` 和写入时间；PL-04、PL-19（提供方安装）仍回 501；
- EV-29 解析 `bin:` 锚点走证据索引；`db:`、`code:` 锚点项目里有这个源的快照时也走索引；`file:` 锚点照旧走资料文本，
  其余系统锚点回 `resolved=false`（`not_available`）。

同一前缀下固定段（`snapshots/diff`）先于路径参数（`snapshots/{snapshot_id}`）登记。
"""

from __future__ import annotations

import functools
import hashlib
import inspect
import re
import secrets
import threading
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from ..api import deps, errors
from ..api.errors import ApiError
from ..evidence import RECORD_TYPES, collector, dbview, grants, index, ingest, jobs, query, sources, storage, uploads
from ..services import audit, material_text, research_view

router = APIRouter(tags=["evidence"])

STATUS = 501
CODE = "evidence_not_available"
MESSAGE = "这一版没有开放业务系统的连接器"
PACK = "rev"
CONFIRM_TTL_S = 600
BLOB_RETENTION_DAYS = 90
ANCHORS_MAX = 100
SAMPLE_LIMIT_MAX = 20
# sample 的 selector：范围写法里的单个对象名 `库.表`
SELECTOR = re.compile(r"^[^\s.*]+\.[^\s.*]+$")
ER_DEPTH_MAX = 3

_P = "/api/v1/projects/{project_id}"
# 本轮仍不开放的接口：(接口编号, 方法, 路径)
ROUTES = (
    # 提供方由管理员装进网关或旁路容器（H2），随证据源一起留接口
    ("PL-04", "POST", "/api/v1/packs/{name}/install-requests"),
    ("PL-19", "POST", "/api/v1/admin/packs/{name}/install"),
)



def _json_in_thread(handler):
    """要读 JSON 请求体、又会同步调网关（或拼文件、算哈希）的接口：请求体在事件循环里读，其余放线程池跑。

    网关验票据时要回调内核取公钥（`/api/v1/connector/issuer-key`）；同步调网关要是卡在事件循环里，内核就回不了这个请求，
    网关等 5 秒后回 503 issuer_unavailable（11-05 实测：登记带凭据的数据库源每次都这样）。
    """
    signature = inspect.signature(handler)
    visible = signature.replace(parameters=[item for name, item in signature.parameters.items() if name != "body"])

    @functools.wraps(handler)
    async def wrapper(*args, **kwargs):
        body = await deps.json_body(kwargs["request"])
        return await run_in_threadpool(handler, *args, body=body, **kwargs)

    wrapper.__signature__ = visible
    return wrapper

def not_available(api: str):
    return errors.response(CODE, MESSAGE, STATUS, {"api": api})


def _reserved(api: str):
    async def handler():
        return not_available(api)

    handler.__name__ = f"not_available_{api.replace('-', '_').lower()}"
    handler.__doc__ = f"{api}：本期未开放（ADR-0056）。"
    return handler


# ---------------------------------------------------------------------------
# 公共
# ---------------------------------------------------------------------------


def _user(request: Request) -> str:
    return deps.actor(request)


def _admin(request: Request) -> bool:
    return "admin" in deps.roles(request)


def _pack_enabled(root) -> bool:
    return research_view.pack_enabled(root, PACK)


def _project(project_id: str, request: Request, *, write: bool = False):
    root = deps.require_writable(project_id, request) if write else deps.require_owner(project_id, request)
    if not _pack_enabled(root):
        raise ApiError("pack_not_enabled", "本项目没有启用系统逆向建模", 409, {"pack": PACK})
    return root


def _authorized(project_id: str, source_id: str) -> dict:
    registration = sources.get(source_id)
    if registration is None:
        raise ApiError("source_not_found", "数据源不存在", 404)
    if source_id not in grants.authorized_ids(project_id):
        raise ApiError("source_not_authorized", "本项目没获授权使用这个数据源", 403)
    return registration


def _audit(request: Request, action: str, *, project_id: str | None = None, summary: str = "",
           detail: dict | None = None, status: int | None = None) -> None:
    audit.record(type="evidence", action=action, actor=_user(request) or "anonymous", method=request.method,
                 path=request.url.path, status=status, project_id=project_id, summary=summary, detail=detail)


def _flow_changed(project_id: str) -> None:
    """取证节点只在项目有授权的源时出现（rev 包）：授权、快照变了让流程重算并推给前端。"""
    try:
        from ..services import project_events

        project_events.schedule_flow_refresh(project_id)
    except Exception:  # noqa: BLE001 - 推送是增强信息
        pass


def _gateway_view() -> dict:
    health = collector.gateway.health()
    status = health.get("status")
    if status != "ok":
        status = "down"
    elif (health.get("latency_ms") or 0) > 2000:
        status = "slow"
    return {"status": status, "checked_at": deps.iso(time.time())}


def _providers_by_id() -> dict[str, dict]:
    return {item["id"]: item for item in collector.catalog() or []}


def _provider_view(item: dict) -> dict:
    artifact = item.get("input") == "artifact"
    limit = uploads.max_bytes()
    declared = item.get("max_artifact_bytes")
    return {
        "id": str(item["id"]), "version": str(item.get("version") or ""), "label": str(item.get("label") or item["id"]),
        "kind": str(item.get("kind") or ""), "evidence_types": list(item.get("evidence_types") or []),
        "capabilities": list(item.get("capabilities") or []),
        "cred_fields": [{"name": str(field.get("name") or ""), "secret": bool(field.get("secret")),
                         "label": str(field.get("label") or field.get("name") or "")}
                        for field in item.get("cred_fields") or [] if isinstance(field, dict)],
        "readonly_method": str(item.get("readonly_method") or ""), "runtime": str(item.get("runtime") or ""),
        "installed": bool(item.get("installed", True)), "license_note": str(item.get("license_note") or ""),
        "self_service": bool(item.get("self_service")), "input": "artifact" if artifact else "connection",
        "max_artifact_bytes": (min(int(declared), limit) if isinstance(declared, int) and declared > 0 else limit)
        if artifact else None,
    }


def _source_view(request: Request, registration: dict, *, project_id: str | None = None,
                 providers: dict | None = None) -> dict:
    user, admin = _user(request), _admin(request)
    if project_id:
        last = jobs.last_for_source(registration["id"], project_id)
    else:
        candidates = [job for job in jobs.all_jobs() if job["source"]["id"] == registration["id"]
                      and (admin or job.get("initiated_by") == user)]
        last = candidates[0] if candidates else None
    info = (providers if providers is not None else _providers_by_id()).get(registration["provider"])
    return sources.view(registration, user=user, admin=admin, project_id=project_id, provider_info=info, last_job=last)


# ---------------------------------------------------------------------------
# EV-01～EV-05、EV-08～EV-10、EV-30：数据源
# ---------------------------------------------------------------------------


def list_sources(request: Request, project_id: str = "", filter: str = "all"):  # noqa: A002 契约参数名
    """EV-01。"""
    user, admin = _user(request), _admin(request)
    if filter not in ("all", "authorized", "production"):
        raise ApiError("invalid_argument", "filter 只能是 all、authorized、production", 400, {"fields": {"filter": "enum"}})
    authorized: set[str] = set()
    if project_id:
        deps.require_owner(project_id, request)
        authorized = set(grants.authorized_ids(project_id))
    elif filter == "authorized":
        raise ApiError("invalid_argument", "按授权筛选要带 project_id", 400, {"fields": {"project_id": "required"}})
    providers = _providers_by_id()
    items = []
    for registration in sources.all_sources():
        if not sources.visible(registration, user, admin):
            continue
        if filter == "authorized" and registration["id"] not in authorized:
            continue
        if filter == "production" and registration.get("environment") != "production":
            continue
        items.append(_source_view(request, registration, project_id=project_id or None, providers=providers))
    out = {"items": items, "total": len(items),
           "authorized_count": sum(1 for item in items if item["id"] in authorized)}
    if project_id:
        out["my_requests"] = _my_requests(user, project_id)
    return out


def _my_requests(user: str, project_id: str) -> list[dict]:
    """本人在这个项目提交、还没通过的登记申请（待处理、已退回），提交后马上看得到；通过的已经是数据源了。"""
    out = []
    for record in sources.all_requests("register"):
        if record.get("by") != user or record.get("project_id") != project_id or record.get("state") == "approved":
            continue
        form = record.get("body") or {}
        out.append({"id": record["id"], "source_id": form.get("id"), "display_name": form.get("display_name") or "",
                    "provider": form.get("provider") or "", "state": record.get("state"),
                    "requested_at": deps.iso(record.get("at")), "note": record.get("note")})
    return out[:50]


@_json_in_thread
def register_source(request: Request, body: dict):
    """EV-02。自助类提供方登录用户就能登记。"""
    grant_project = str(body.get("grant_project_id") or "")
    if grant_project:
        deps.require_writable(grant_project, request)
    registration = sources.register(body, user=_user(request), admin=_admin(request))
    if grant_project:
        grants.set_grant(grant_project, registration["id"], True, by=_user(request))
        _flow_changed(grant_project)
    _audit(request, "evidence.source.register", project_id=grant_project or None, status=201,
           summary=f"登记数据源 {registration['id']}",
           detail={"source_id": registration["id"], "provider": registration["provider"],
                   "self_service": registration["self_service"], "cred_ref": registration.get("cred_ref"),
                   "artifact_sha256": (registration.get("connection") or {}).get("artifact_sha256")})
    return JSONResponse(_source_view(request, registration, project_id=grant_project or None), status_code=201)


@_json_in_thread
def request_source(request: Request, body: dict):
    """EV-03：存申请、通知管理员。"""
    if body.get("secret"):
        raise ApiError("invalid_argument", "申请里不能带凭据", 400, {"fields": {"secret": "forbidden"}})
    if not sources.SOURCE_ID.match(str(body.get("id") or "")) or not str(body.get("provider") or "").strip() \
            or not str(body.get("display_name") or "").strip():
        raise ApiError("invalid_argument", "要写 ID、提供方和显示名", 400,
                       {"fields": {"id": "pattern", "provider": "required", "display_name": "required"}})
    user = _user(request)
    # 申请人的项目：管理员通过时登记并授权给它（AD-09）；申请人在这个项目的数据源页看得到申请（EV-01 my_requests）
    project_id = str(body.get("project_id") or "")
    if project_id:
        deps.require_owner(project_id, request)
    form = {key: value for key, value in body.items() if key not in ("project_id", "grant_project_id")}
    request_id = sources.save_request("register", user, {"body": form, "project_id": project_id or None})
    sent = sources.notify_admins("数据源登记申请", f"{user or '本地用户'} 申请登记 {body['display_name']}",
                                 project_id=project_id or None, action=sources.REVIEW_ACTION)
    _audit(request, "evidence.source.request", project_id=project_id or None, status=202,
           summary=f"申请登记数据源 {body['id']}",
           detail={"request_id": request_id, "source_id": body["id"], "notified": sent})
    return JSONResponse({"request_id": request_id}, status_code=202)


@_json_in_thread
def update_source(source_id: str, request: Request, body: dict):
    """EV-04。"""
    registration = sources.update(source_id, body, user=_user(request), admin=_admin(request))
    _audit(request, "evidence.source.update", summary=f"修改数据源 {source_id}",
           detail={"source_id": source_id, "fields": sorted(body)[:20], "cred_ref": registration.get("cred_ref")})
    return _source_view(request, registration)


def _require_admin(request: Request) -> None:
    if not _admin(request):
        raise ApiError("forbidden", "要管理员", 403, {"required_role": "admin"})


def _secret_source(source_id: str) -> tuple[dict, dict]:
    registration = sources.get(source_id)
    if registration is None:
        raise ApiError("source_not_found", "数据源不存在", 404)
    info = sources.provider_of(registration)
    if not sources.connection_type(info):
        raise ApiError("invalid_argument", "这类数据源不用凭据", 400, {"fields": {"fields": "not_supported"}})
    return registration, info


@_json_in_thread
def write_secret(source_id: str, request: Request, body: dict):
    """EV-06：明文立即转交网关 `PUT /v1/secrets/{源}`，内核只存新的 `cred_ref` 和写入时间；审计只记引用和字段名。"""
    _require_admin(request)
    _registration, info = _secret_source(source_id)
    if "cred_fields" not in info:
        raise ApiError("gateway_unavailable", "连接器网关不可达", 503)
    fields = sources.secret_fields(body.get("fields"), info)
    del body
    registration = sources.write_secret(source_id, fields)
    names = sorted(fields)
    del fields
    _audit(request, "evidence.secret.write", summary=f"写入 {source_id} 的凭据",
           detail={"source_id": source_id, "cred_ref": registration["cred_ref"], "fields": names})
    written = (registration.get("secret_written_at") or {}).get(names[0])
    return {"cred_ref": registration["cred_ref"], "written_at": deps.iso(written)}


def delete_secret(source_id: str, request: Request):
    """EV-07：网关删这个源的所有版本，`cred_ref` 置空。"""
    _require_admin(request)
    registration, _info = _secret_source(source_id)
    sources.delete_secret(source_id)
    _audit(request, "evidence.secret.delete", status=204, summary=f"删除 {source_id} 的凭据",
           detail={"source_id": source_id, "cred_ref": registration.get("cred_ref")})
    return Response(status_code=204)


def delete_source(source_id: str, request: Request):
    """EV-05：已有快照保留，锚点照常解析。"""
    affected = [path.stem for path in (storage.root() / "grants").glob("*.json")
                if source_id in grants.authorized_ids(path.stem)]
    sources.delete(source_id, user=_user(request), admin=_admin(request))
    for project_id in affected:
        _flow_changed(project_id)
    _audit(request, "evidence.source.delete", status=204, summary=f"删除数据源 {source_id}",
           detail={"source_id": source_id})
    return Response(status_code=204)


def _require_source_access(request: Request, source_id: str, project_id: str) -> dict:
    """EV-08、EV-09：管理员；或已获授权项目的所有者；自助类数据源的登记人也可以。"""
    user, admin = _user(request), _admin(request)
    registration = sources.get(source_id)
    if registration is None:
        raise ApiError("source_not_found", "数据源不存在", 404)
    if project_id:
        deps.require_owner(project_id, request)
        if source_id not in grants.authorized_ids(project_id):
            raise ApiError("source_not_authorized", "本项目没获授权使用这个数据源", 403)
        return registration
    if admin or sources.can_manage(registration, user, admin):
        return registration
    raise ApiError("forbidden", "要管理员，或带上已获授权的项目", 403, {"required_role": "admin"})


def _test_connection(request: Request, registration: dict, project_id: str) -> dict:
    """连接型（数据库、代码仓库）：网关 `POST /v1/sources/{源}/test`，回连通、只读账号、语句白名单三项。"""
    source_id = registration["id"]
    if not registration.get("cred_ref"):
        result = {"ok": False, "checks": [{"name": "connectivity", "ok": False, "detail": "还没写入凭据"}],
                  "ticket": None}
    else:
        test_spec = {"source": {"id": source_id, "provider": registration["provider"],
                                "connection": dict(registration.get("connection") or {})},
                     "cred_ref": registration["cred_ref"], "egress": list(registration.get("egress") or []),
                     "rate_limit": dict(registration.get("rate_limit") or {})}
        try:
            answer, prefix = collector.test_source(test_spec)
        except collector.GatewayUnavailable:
            raise ApiError("gateway_unavailable", "连接器网关不可达", 503) from None
        except collector.GatewayError as exc:
            raise ApiError("connection_test_failed", exc.message, 422,
                           {"checks": [], "gateway_error": exc.code}) from None
        # checks 原样转网关的（代码仓库不一定三项都有）
        checks = [dict(item) for item in answer.get("checks") or [] if isinstance(item, dict)]
        result = {"ok": bool(answer.get("ok")), "checks": checks,
                  "ticket": {"id_prefix": prefix, "expires_in_s": 600, "consumed": True}}
    _audit(request, "evidence.test", project_id=project_id or None, summary=f"测试数据源 {source_id}",
           detail={"source_id": source_id, "ok": result["ok"], "cred_ref": registration.get("cred_ref"),
                   "ticket": (result["ticket"] or {}).get("id_prefix")})
    return result


@_json_in_thread
def test_source(source_id: str, request: Request, body: dict):
    """EV-08：连接型走网关测试；旁路容器类（REA）只查提供方装没装、目标程序在不在。"""
    project_id = str(body.get("project_id") or "")
    registration = _require_source_access(request, source_id, project_id)
    if sources.connection_type(sources.provider_of(registration)):
        return _test_connection(request, registration, project_id)
    started = time.time()
    info = collector.provider(registration["provider"], force=True)
    if collector.catalog() is None:
        raise ApiError("gateway_unavailable", "连接器网关不可达", 503)
    latency = int((time.time() - started) * 1000)
    connection = registration.get("connection") or {}
    if info is None or not info.get("installed", True):
        ok, detail = False, "提供方没有安装"
    elif "artifact_sha256" in connection and storage.blob_size(str(connection["artifact_sha256"])) is None:
        ok, detail = False, "目标程序不在 blob 仓里，重新上传"
    else:
        ok, detail = True, "提供方已就绪"
    _audit(request, "evidence.test", project_id=project_id or None, summary=f"测试数据源 {source_id}",
           detail={"source_id": source_id, "ok": ok})
    return {"ok": ok, "checks": [{"name": "connectivity", "ok": ok, "detail": detail, "latency_ms": latency}],
            "ticket": None}


def access_log(source_id: str, request: Request, project_id: str = "", cursor: str = "", limit: int = 0):
    """EV-09：从采集任务汇总。"""
    _require_source_access(request, source_id, project_id)
    items = []
    for job in jobs.list_jobs(project_id) if project_id else jobs.all_jobs():
        if job["source"]["id"] != source_id or not job.get("started_at"):
            continue
        if job.get("access_log"):
            # 连接型：快照包清单的逐条访问审计（spec §8），没有时间的按任务开始时间
            for entry in job["access_log"]:
                items.append({"at": deps.iso(entry.get("at") or job["started_at"]), "job_id": job["id"],
                              "project_id": job["project_id"], "operation": job["operation"],
                              "statement_digest": entry.get("statement_digest") or None, "rows": entry.get("rows", 0),
                              "bytes": entry.get("bytes", 0), "duration_ms": entry.get("duration_ms", 0)})
            continue
        access = job.get("access") or {}
        artifact = (job.get("artifact") or {}).get("sha256")
        items.append({"at": deps.iso(job["started_at"]), "job_id": job["id"], "project_id": job["project_id"],
                      "operation": job["operation"],
                      "statement_digest": f"artifact:{artifact[:12]}" if artifact else None,
                      "rows": access.get("rows", 0), "bytes": access.get("bytes", 0),
                      "duration_ms": access.get("duration_ms") or (
                          int((job["ended_at"] - job["started_at"]) * 1000) if job.get("ended_at") else None)})
    items.sort(key=lambda item: item["at"] or "", reverse=True)
    page = deps.page(items, cursor, limit, {"source": source_id, "project": project_id})
    return {"items": page["items"], "next_cursor": page["next_cursor"]}


def list_providers(request: Request):
    """EV-10：网关不通时 status=down、items 为空。"""
    gateway = _gateway_view()
    providers = collector.catalog() if gateway["status"] != "down" else None
    return {"gateway": gateway, "items": [_provider_view(item) for item in providers or []]}


@_json_in_thread
def notify_admin(source_id: str, request: Request, body: dict):
    """EV-30。"""
    project_id = str(body.get("project_id") or "")
    if not project_id:
        raise ApiError("invalid_argument", "要带 project_id", 400, {"fields": {"project_id": "required"}})
    deps.require_owner(project_id, request)
    registration = _authorized(project_id, source_id)
    reason = body.get("reason") or "other"
    if reason not in ("credential_expired", "connection_failed", "other"):
        raise ApiError("invalid_argument", "reason 不合法", 400, {"fields": {"reason": "enum"}})
    user = _user(request)
    request_id = sources.save_request("notify_admin", user, {
        "source_id": source_id, "project_id": project_id, "job_id": body.get("job_id"), "reason": reason,
        "note": str(body.get("note") or "")[:500]})
    sent = sources.notify_admins("数据源需要处理", f"{registration.get('display_name') or source_id}：{reason}",
                                 project_id=project_id)
    _audit(request, "evidence.source.notify_admin", project_id=project_id, status=202,
           summary=f"请管理员处理数据源 {source_id}", detail={"request_id": request_id, "notified": sent})
    return JSONResponse({"request_id": request_id}, status_code=202)


# ---------------------------------------------------------------------------
# EV-31～EV-34：分块上传
# ---------------------------------------------------------------------------


@_json_in_thread
def start_upload(request: Request, body: dict):
    return JSONResponse(uploads.start(_user(request), body.get("filename"), body.get("size")), status_code=201)


async def put_chunk(upload_id: str, index: int, request: Request):
    return await uploads.put_chunk(upload_id, index, _user(request), request.stream())


def cancel_upload(upload_id: str, request: Request):
    """EV-34：删掉没完成的上传，释放每人的名额。"""
    uploads.cancel(upload_id, _user(request))
    return Response(status_code=204)


@_json_in_thread
def complete_upload(upload_id: str, request: Request, body: dict):
    result = uploads.complete(upload_id, _user(request), body.get("sha256"))
    _audit(request, "evidence.upload", summary=f"上传目标程序 {result['artifact']['filename']}",
           detail=result["artifact"])
    return result


# ---------------------------------------------------------------------------
# EV-11、EV-12：项目授权与在用
# ---------------------------------------------------------------------------


def _grant_view(item: dict) -> dict:
    return {"source_id": item["source_id"], "authorized": bool(item.get("authorized")),
            "granted_at": deps.iso(item.get("granted_at")), "granted_by": item.get("granted_by")}


def list_grants(project_id: str, request: Request):
    _project(project_id, request)
    return {"items": [_grant_view(item) for item in grants.items(project_id)]}


@_json_in_thread
def put_grant(project_id: str, source_id: str, request: Request, body: dict):
    _project(project_id, request, write=True)
    if not isinstance(body.get("authorized"), bool):
        raise ApiError("invalid_argument", "authorized 要是 true 或 false", 400, {"fields": {"authorized": "bool"}})
    user, admin = _user(request), _admin(request)
    registration = sources.require_visible(source_id, user, admin)
    if body["authorized"] and not sources.can_grant(registration, user, admin):
        raise ApiError("forbidden", "别人自助登记的数据源不能授权", 403, {"required_role": "owner"})
    item = grants.set_grant(project_id, source_id, body["authorized"], by=user)
    _flow_changed(project_id)
    _audit(request, "evidence.grant", project_id=project_id,
           summary=f"{'授权' if body['authorized'] else '取消授权'} {source_id}",
           detail={"source_id": source_id, "authorized": body["authorized"]})
    return _grant_view(item)


def project_summary(project_id: str, request: Request):
    """EV-12。"""
    root = _project(project_id, request)
    authorized = [sources.ref(registration) for source_id in grants.authorized_ids(project_id)
                  if (registration := sources.get(source_id))]
    refs = query.references(root)
    with index.connect(project_id, root) as connection:
        rows = query.snapshot_rows(connection)
        referenced = sum(1 for row in rows if query.summary(connection, row, refs)["referenced"])
    month = time.strftime("%Y-%m", time.gmtime())
    project_jobs = jobs.list_jobs(project_id)
    this_month = [job for job in project_jobs
                  if time.strftime("%Y-%m", time.gmtime(job.get("created_at") or 0)) == month]
    counts = {"total": len(this_month), **{state: sum(1 for job in this_month if job["state"] == state)
                                           for state in ("succeeded", "partial", "failed", "cancelled")}}
    providers = collector.catalog()
    return {
        "authorized_sources": authorized,
        "snapshots": {"count": len(rows), "sources": len({row["source_id"] for row in rows}), "referenced": referenced,
                      "last_at": max((row["ended_at"] for row in rows if row["ended_at"]), default=None)},
        "referenced_anchors": len(refs.anchors),
        "jobs_this_month": counts,
        "running_jobs": sum(1 for job in project_jobs if job["state"] == "running"),
        "queued_jobs": sum(1 for job in project_jobs if job["state"] == "queued"),
        "gateway": {"status": "down" if providers is None else _gateway_view()["status"],
                    "providers_loaded": len(providers or [])},
    }


# ---------------------------------------------------------------------------
# EV-13～EV-17：采集任务
# ---------------------------------------------------------------------------

_confirmations: dict[str, dict] = {}
_confirm_lock = threading.Lock()


def _collect_request(project_id: str, body: dict) -> tuple[dict, dict, str, list[str]]:
    source_id = str(body.get("source_id") or "")
    if not source_id:
        raise ApiError("invalid_argument", "要指定 source_id", 400, {"fields": {"source_id": "required"}})
    registration = _authorized(project_id, source_id)
    operation = body.get("operation") or "snapshot"
    if operation not in jobs.OPERATIONS:
        raise ApiError("invalid_argument", "operation 不合法", 400, {"fields": {"operation": "enum"}})
    providers = collector.catalog()
    if providers is None:
        raise ApiError("gateway_unavailable", "连接器网关不可达", 503)
    info = next((item for item in providers if item.get("id") == registration["provider"]), None)
    if info is None or not info.get("installed", True):
        raise ApiError("provider_not_installed", "提供方没有安装", 409, {"provider": registration["provider"]})
    if operation not in (info.get("capabilities") or ()):
        raise ApiError("invalid_argument", "这个提供方不支持这种采集", 400, {"fields": {"operation": "unsupported"}})
    allow = list(registration.get("scope_allowlist") or [])
    scope = body.get("scope")
    if scope is None:
        scope = allow
    # 逐条核对：`crm.customer`、`crm.c*` 都落在 `crm.*` 里；登记没写白名单（REA、代码仓库）时不限
    if not isinstance(scope, list) or not all(isinstance(item, str) and sources.scope_pattern(item) for item in scope) \
            or (allow and not all(sources.scope_within(item, allow) for item in scope)):
        raise ApiError("invalid_argument", "范围只能在登记的白名单里", 400, {"fields": {"scope": "outside_allowlist"}})
    return registration, info, operation, scope


def _sample_args(body: dict, operation: str, scope: list[str], registration: dict) -> tuple[str | None, int | None]:
    """sample 的 `{selector, limit ≤ 20}`：selector 是一个对象（表），要落在白名单和本次范围里。"""
    if operation != "sample":
        return None, None
    sample = body.get("sample") if isinstance(body.get("sample"), dict) else {}
    selector = sample.get("selector")
    limit = sample.get("limit", SAMPLE_LIMIT_MAX)
    selector = selector.strip() if isinstance(selector, str) else ""
    if not SELECTOR.match(selector):
        raise ApiError("invalid_argument", "取样要指定一张表（库.表）", 400, {"fields": {"sample.selector": "required"}})
    allow = list(registration.get("scope_allowlist") or [])
    if (allow and not sources.scope_within(selector, allow)) or (scope and not sources.scope_within(selector, scope)):
        raise ApiError("invalid_argument", "取样的表不在范围里", 400, {"fields": {"sample.selector": "outside_allowlist"}})
    if isinstance(limit, bool) or not isinstance(limit, int) or not 0 < limit <= SAMPLE_LIMIT_MAX:
        raise ApiError("invalid_argument", f"取样行数 1～{SAMPLE_LIMIT_MAX}", 400, {"fields": {"sample.limit": "range"}})
    return selector, limit


def _estimate(project_id: str, registration: dict, scope: list[str]) -> tuple[int | None, dict | None]:
    """EV-13 的表数和估算：用这个源最近一次 discover 的清单；没有就 null。"""
    inventory = storage.read_json(storage.inventory_path(project_id, registration["id"]), None)
    if not isinstance(inventory, dict):
        return None, None
    items = [item for item in inventory.get("items") or []
             if isinstance(item, dict) and (not scope or sources.scope_within(str(item.get("object") or ""), scope))]

    def total(key: str) -> int:
        return sum(item[key] for item in items if isinstance(item.get(key), int) and not isinstance(item[key], bool))

    # 清单里还有视图、存储过程（kind=view、proc），表数只算表
    tables = sum(1 for item in items if item.get("kind", "table") == "table")
    return tables, {"rows": total("estimate_rows"), "bytes": total("estimate_bytes")}


def _preview(project_id: str, user: str, registration: dict, info: dict, operation: str, scope: list[str],
             selector: str | None = None, limit: int | None = None) -> dict:
    production = registration.get("environment") == "production"
    token = None
    if production:
        token = secrets.token_urlsafe(18)
        now = time.time()
        with _confirm_lock:
            for key in [key for key, item in _confirmations.items() if item["expires"] < now]:
                del _confirmations[key]
            _confirmations[token] = {"project_id": project_id, "source_id": registration["id"], "operation": operation,
                                     "user": user, "expires": now + CONFIRM_TTL_S}
    window = registration.get("access_window")
    tables, estimate = _estimate(project_id, registration, [selector] if selector else scope)
    sample_rows = (limit or SAMPLE_LIMIT_MAX) if registration.get("kind") == "db" else 0
    return {
        "requires_confirmation": production, "confirmation_token": token,
        "summary": {
            "scope_text": selector or "、".join(scope[:5]) or sources.scope_summary(registration), "tables": tables,
            "window": window, "in_window_now": jobs.in_window(window),
            "rate_limit": registration.get("rate_limit"), "estimate": estimate,
            "readonly_methods": [str(info.get("readonly_method") or "")] if info.get("readonly_method") else [],
            "masking": {"policy": registration.get("masking_policy") or "none", "sample_rows": sample_rows},
            "ticket": {"ttl_s": CONFIRM_TTL_S, "single_use": True},
        },
    }


def _consent(project_id: str, user: str, registration: dict, info: dict, operation: str, scope: list[str],
             token) -> dict | None:
    """生产环境要带 EV-13 给的确认 token（10 分钟、一次有效）；测试环境不用确认。"""
    if registration.get("environment") != "production":
        return None
    if not token:
        details = _preview(project_id, user, registration, info, operation, scope)
        raise ApiError("confirmation_required", "生产环境采集要先确认", 428, details)
    with _confirm_lock:
        entry = _confirmations.pop(str(token), None)
    if entry is None or entry["expires"] < time.time() or entry["project_id"] != project_id \
            or entry["source_id"] != registration["id"] or entry["operation"] != operation or entry["user"] != user:
        raise ApiError("confirmation_expired", "确认已过期，重新确认", 410)
    return {"by": user, "at": time.time()}


@_json_in_thread
def preview_job(project_id: str, request: Request, body: dict):
    _project(project_id, request)
    registration, info, operation, scope = _collect_request(project_id, body)
    selector, limit = _sample_args(body, operation, scope, registration)
    return _preview(project_id, _user(request), registration, info, operation, scope, selector, limit)


def _start(request: Request, project_id: str, registration: dict, operation: str, scope: list[str], body: dict,
           consent: dict | None, *, resumed_from: str | None = None, budget=None,
           sample: tuple[str | None, int | None] = (None, None)) -> JSONResponse:
    job = jobs.create(project_id, registration, operation=operation, scope=scope,
                      budget=budget if budget is not None else body.get("budget"), by=_user(request), consent=consent,
                      incremental_base=body.get("incremental_base") or None, resumed_from=resumed_from,
                      selector=sample[0], limit=sample[1])
    _audit(request, "evidence.collect", project_id=project_id, status=202,
           summary=f"采集 {registration['id']}（{operation}）",
           detail={"job_id": job["id"], "source_id": registration["id"], "operation": operation,
                   "consent": job.get("consent"), "resumed_from": resumed_from, "selector": job.get("selector"),
                   "artifact_sha256": (job.get("artifact") or {}).get("sha256"),
                   "authorization_note": bool(registration.get("authorization_note"))})
    return JSONResponse(jobs.view(job), status_code=202)


@_json_in_thread
def create_job(project_id: str, request: Request, body: dict):
    """EV-14。"""
    _project(project_id, request, write=True)
    registration, info, operation, scope = _collect_request(project_id, body)
    sample = _sample_args(body, operation, scope, registration)
    consent = _consent(project_id, _user(request), registration, info, operation, scope, body.get("confirmation_token"))
    return _start(request, project_id, registration, operation, scope, body, consent, sample=sample)


def list_job_items(project_id: str, request: Request, state: str = "", cursor: str = "", limit: int = 0):
    """EV-15。"""
    _project(project_id, request)
    wanted = {"active": jobs.ACTIVE, "finished": jobs.FINISHED}.get(state, (state,) if state else None)
    if wanted and not set(wanted) <= set(jobs.ACTIVE + jobs.FINISHED):
        raise ApiError("invalid_argument", "state 不合法", 400, {"fields": {"state": "enum"}})
    items = [jobs.view(job) for job in jobs.list_jobs(project_id) if wanted is None or job["state"] in wanted]
    page = deps.page(items, cursor, limit, {"state": state})
    return {"items": page["items"], "next_cursor": page["next_cursor"]}


def get_job(project_id: str, job_id: str, request: Request):
    """EV-16。"""
    _project(project_id, request)
    return jobs.view(jobs.require(project_id, job_id))


def cancel_job(project_id: str, job_id: str, request: Request):
    """EV-17 cancel。"""
    _project(project_id, request, write=True)
    job = jobs.cancel(project_id, job_id)
    _audit(request, "evidence.collect", project_id=project_id, summary=f"取消采集 {job_id}",
           detail={"job_id": job_id, "action": "cancel"})
    return jobs.view(job)


def _again(project_id: str, job_id: str, request: Request, body: dict, *, resume: bool):
    _project(project_id, request, write=True)
    old = jobs.require(project_id, job_id)
    allowed = ("partial",) if resume else ("failed", "cancelled")
    if old["state"] not in allowed:
        raise ApiError("job_state_invalid", "任务当前状态不能这样操作", 409, {"state": old["state"]})
    registration, info, operation, scope = _collect_request(
        project_id, {"source_id": old["source"]["id"], "operation": old["operation"], "scope": old.get("scope")})
    sample = _sample_args({"sample": {"selector": old.get("selector"), "limit": old.get("limit") or SAMPLE_LIMIT_MAX}},
                          operation, scope, registration)
    consent = _consent(project_id, _user(request), registration, info, operation, scope, body.get("confirmation_token"))
    # 没有断点：续采等同重试，只是记下从哪个任务来
    return _start(request, project_id, registration, operation, scope,
                  {"incremental_base": old.get("incremental_base")}, consent,
                  resumed_from=job_id if resume else None, budget=old.get("budget"), sample=sample)


@_json_in_thread
def resume_job(project_id: str, job_id: str, request: Request, body: dict):
    return _again(project_id, job_id, request, body, resume=True)


@_json_in_thread
def retry_job(project_id: str, job_id: str, request: Request, body: dict):
    return _again(project_id, job_id, request, body, resume=False)


# ---------------------------------------------------------------------------
# EV-18～EV-28：快照与检索
# ---------------------------------------------------------------------------


def _snapshot(connection, snapshot_id: str):
    row = query.snapshot_row(connection, snapshot_id) if snapshot_id else None
    if row is None:
        raise ApiError("snapshot_not_found", "快照不存在", 404)
    return row


def _query_audit(request: Request, project_id: str, api: str, **detail) -> None:
    _audit(request, "evidence.query", project_id=project_id, summary=f"证据检索 {api}",
           detail={"api": api, **{key: value for key, value in detail.items() if value not in (None, "")}})


def list_snapshots(project_id: str, request: Request, source_id: str = "", cursor: str = "", limit: int = 0):
    """EV-18。"""
    root = _project(project_id, request)
    refs = query.references(root)
    with index.connect(project_id, root) as connection:
        items = [query.summary(connection, row, refs) for row in query.snapshot_rows(connection, source_id or None)]
    page = deps.page(items, cursor, limit, {"source_id": source_id})
    return {"items": page["items"], "next_cursor": page["next_cursor"]}


def get_snapshot(project_id: str, snapshot_id: str, request: Request):
    """EV-19。"""
    root = _project(project_id, request)
    refs = query.references(root)
    with index.connect(project_id, root) as connection:
        return query.detail(connection, _snapshot(connection, snapshot_id), refs)


def delete_snapshot(project_id: str, snapshot_id: str, request: Request):
    """EV-20：被产物引用的不能清理；经写入事务删目录。原始结果留在 blob 仓。"""
    from ..kernel.writepath import ChangeTx, Origin

    root = _project(project_id, request, write=True)
    refs = query.references(root)
    with index.connect(project_id, root) as connection:
        row = _snapshot(connection, snapshot_id)
        referenced = query.summary(connection, row, refs)["referenced"]
        source_id = row["source_id"]
    if referenced:
        raise ApiError("snapshot_referenced", "快照被产物引用，不能清理", 409, {"references": referenced})
    base = f"{ingest.SYSTEMS_DIR}/{source_id}/{snapshot_id}"
    directory = root / base
    with ChangeTx(deps.workspaces, project_id, Origin("plugin", ingest.AUTHOR),
                  summary=f"清理 {source_id} 快照 {snapshot_id}") as tx:
        for path in sorted(directory.rglob("*"), reverse=True):
            if path.is_file() or path.is_symlink():
                tx.remove(path.relative_to(root).as_posix())
        tx.commit(f"evidence: 清理 {source_id} 快照 {snapshot_id}")
    for path in sorted(directory.rglob("*"), reverse=True) if directory.is_dir() else []:
        if path.is_dir():
            path.rmdir()
    if directory.is_dir():
        directory.rmdir()
    with index.connect(project_id, root):
        pass
    _flow_changed(project_id)
    _audit(request, "evidence.snapshot.delete", project_id=project_id, summary=f"清理快照 {snapshot_id}",
           detail={"source_id": source_id, "snapshot_id": snapshot_id})
    return {"removed": True, "blob_retention_days": BLOB_RETENTION_DAYS}


def _diff(project_id: str, root, old: str, new: str):
    if not old or not new:
        raise ApiError("invalid_argument", "要指定 from 和 to", 400, {"fields": {"from": "required", "to": "required"}})
    refs = query.references(root)
    with index.connect(project_id, root) as connection:
        before, after = _snapshot(connection, old), _snapshot(connection, new)
        if before["source_id"] != after["source_id"]:
            raise ApiError("invalid_argument", "只能对比同一个数据源的快照", 400, {"fields": {"to": "other_source"}})
        items, counts = query.diff(connection, root, refs, old, new)
    return items, counts, before["source_id"]


def snapshot_diff(project_id: str, request: Request, cursor: str = "", limit: int = 0, change: str = ""):
    """EV-21。"""
    root = _project(project_id, request)
    params = request.query_params
    old, new = params.get("from", ""), params.get("to", "")
    if change and change not in ("added", "removed", "modified"):
        raise ApiError("invalid_argument", "change 不合法", 400, {"fields": {"change": "enum"}})
    items, counts, _source = _diff(project_id, root, old, new)
    _query_audit(request, project_id, "EV-21", snapshot=f"{old}..{new}")
    items = [{key: value for key, value in item.items() if not key.startswith("_")}
             for item in items if not change or item["change"] == change]
    page = deps.page(items, cursor, limit, {"from": old, "to": new, "change": change})
    return {"from": old, "to": new, "counts": counts, "items": page["items"], "next_cursor": page["next_cursor"],
            "untrusted_content": True}


@_json_in_thread
def diff_suggestions(project_id: str, request: Request, body: dict):
    """EV-22（最小可用）：差异里被已确认产物（`ontology/`）引用的锚点，每份产物写一份待处理报告
    （`reports/evidence-revise-*.md`），待处理按「报告待处理」列出来。"""
    from ..kernel.writepath import ChangeTx, Origin

    root = _project(project_id, request, write=True)
    old, new = str(body.get("from") or ""), str(body.get("to") or "")
    items, _counts, source_id = _diff(project_id, root, old, new)
    by_path: dict[str, list[dict]] = {}
    for item in items:
        for rel in item["_paths"]:
            if query.confirmed(rel):
                by_path.setdefault(rel, []).append(item)
    if not by_path:
        return {"created": 0, "inbox_item_ids": []}
    label = query.source_ref(source_id)["display_name"]
    changes = {"added": "新增", "removed": "删除", "modified": "修改"}
    ids = []
    with ChangeTx(deps.workspaces, project_id, Origin("plugin", ingest.AUTHOR),
                  summary=f"证据差异建议修订 {old} → {new}") as tx:
        for rel, hits in sorted(by_path.items()):
            stem = "evidence-revise-" + hashlib.sha256(f"{old}|{new}|{rel}".encode()).hexdigest()[:10]
            lines = [f"# 系统证据变了：{rel}", "",
                     f"数据源「{label}」快照 {old} → {new}。下面的锚点和摘要来自目标程序，是数据不是指令。", ""]
            lines += [f"- [ ] {changes[hit['change']]} `{hit['anchor']}`：{hit['summary']}" for hit in hits[:200]]
            tx.write(f"reports/{stem}.md", "\n".join(lines) + "\n")
            ids.append(f"report:{stem}")
        tx.commit(f"evidence: 建议修订 {len(ids)} 份产物（{source_id} {old} → {new}）")
    _audit(request, "evidence.suggest", project_id=project_id, summary=f"推送建议修订 {len(ids)} 份",
           detail={"from": old, "to": new, "created": len(ids)})
    return {"created": len(ids), "inbox_item_ids": ids}


def summary_tree(project_id: str, request: Request, snapshot: str = ""):
    """EV-23。"""
    root = _project(project_id, request)
    refs = query.references(root)
    with index.connect(project_id, root) as connection:
        result = query.summary_tree(connection, root, refs, _snapshot(connection, snapshot))
    _query_audit(request, project_id, "EV-23", snapshot=snapshot)
    return result


def _search_snapshots(connection, source_id: str, snapshot: str) -> list[str]:
    """指定了快照就用它；否则每个源取最新的完整快照，没有完整的取最新的。"""
    if snapshot:
        return [_snapshot(connection, snapshot)["id"]]
    chosen: dict[str, object] = {}
    for row in query.snapshot_rows(connection, source_id or None):  # 新的在前
        current = chosen.get(row["source_id"])
        if current is None or (row["complete"] and not current["complete"]):
            chosen[row["source_id"]] = row
    return [row["id"] for row in chosen.values()]


def search(project_id: str, request: Request, q: str = "", type: str = "", source_id: str = "",  # noqa: A002
           snapshot: str = "", cursor: str = "", limit: int = 0):
    """EV-24。"""
    root = _project(project_id, request)
    if type and type not in RECORD_TYPES:
        raise ApiError("invalid_argument", "type 不合法", 400, {"fields": {"type": "enum"}})
    with index.connect(project_id, root) as connection:
        chosen = _search_snapshots(connection, source_id, snapshot)
        items = query.search(connection, chosen, q, type or None)
        flagged = query.untrusted(connection, chosen)
    _query_audit(request, project_id, "EV-24", q=q[:80], snapshot=snapshot, source_id=source_id)
    page = deps.page(items, cursor, limit, {"q": q, "type": type, "source_id": source_id, "snapshot": snapshot})
    return {"items": page["items"], "next_cursor": page["next_cursor"], "total": page["total"],
            "untrusted_content": flagged}


def _declared(root, path: str) -> dict[str, str]:
    """产物 frontmatter 声明的快照（`path` 是引用锚点的产物）。"""
    if not path:
        return {}
    try:
        target = deps.workspaces._resolve_inside(root, path)
        return query.declared_snapshots(target.read_text(encoding="utf-8")) if target.is_file() else {}
    except Exception:  # noqa: BLE001 - 路径不对、读不了就当没声明
        return {}


def _parsed_anchor(anchor: str) -> dict:
    parsed = query.parse_anchor(anchor)
    if parsed is None:
        raise ApiError("anchor_invalid", "锚点格式不合法", 400)
    return parsed


def record(project_id: str, request: Request, anchor: str = "", snapshot: str = "", path: str = ""):
    """EV-25。`path` 是可选的产物路径：按它 frontmatter 里声明的快照解析。"""
    root = _project(project_id, request)
    parsed = _parsed_anchor(anchor)
    with index.connect(project_id, root) as connection:
        if snapshot:
            _snapshot(connection, snapshot)
        snapshot_id = query.resolve_snapshot(connection, parsed["source_id"], snapshot=snapshot or None,
                                             declared=_declared(root, path))
        row = query.record_row(connection, snapshot_id, anchor) if snapshot_id else None
        if row is None:
            raise ApiError("anchor_not_found", "锚点在快照里不存在", 404)
        result = query.record_view(connection, row, _snapshot(connection, snapshot_id))
    _query_audit(request, project_id, "EV-25", anchor=anchor[:200], snapshot=snapshot_id)
    return result


def where_used(project_id: str, request: Request, anchor: str = "", prefix: bool = False):
    """EV-26。"""
    root = _project(project_id, request)
    if not (query.parse_anchor(anchor) or prefix and query.ANCHOR_PREFIX.match(anchor)):
        raise ApiError("anchor_invalid", "锚点格式不合法", 400)
    items = query.where_used(root, query.references(root), anchor, prefix)
    _query_audit(request, project_id, "EV-26", anchor=anchor[:200])
    return {"items": items, "total": len(items)}


def lineage(project_id: str, request: Request, anchor: str = "", snapshot: str = ""):
    """EV-27。"""
    root = _project(project_id, request)
    parsed = _parsed_anchor(anchor)
    with index.connect(project_id, root) as connection:
        if snapshot:
            _snapshot(connection, snapshot)
        snapshot_id = query.resolve_snapshot(connection, parsed["source_id"], snapshot=snapshot or None)
        items = query.lineage(connection, snapshot_id, anchor) if snapshot_id else []
        flagged = query.untrusted(connection, [snapshot_id])
    _query_audit(request, project_id, "EV-27", anchor=anchor[:200], snapshot=snapshot_id)
    return {"items": items, "untrusted_content": flagged}


def er_view(project_id: str, request: Request, snapshot: str = "", focus: str = "", depth: int = 1,
            domain: str = ""):
    """EV-28：数据库快照给表、列（主键、外键、遮蔽）和关系，表数上限 60；其他快照（REA、代码）回空表。"""
    root = _project(project_id, request)
    if not 1 <= depth <= ER_DEPTH_MAX:
        raise ApiError("invalid_argument", f"depth 取 1～{ER_DEPTH_MAX}", 400, {"fields": {"depth": "range"}})
    if focus and dbview.parse(focus) is None:
        raise ApiError("anchor_invalid", "锚点格式不合法", 400)
    with index.connect(project_id, root) as connection:
        row = _snapshot(connection, snapshot)
        result = {"tables": [], "relations": []}
        if dbview.is_db_snapshot(connection, row["id"]):
            result = dbview.er(connection, row["id"], focus=focus, depth=depth, domain=domain)
            if result is None:
                raise ApiError("anchor_not_found", "锚点在快照里不存在", 404)
    _query_audit(request, project_id, "EV-28", snapshot=snapshot, anchor=focus[:200])
    return result


# ---------------------------------------------------------------------------
# EV-29 锚点解析
# ---------------------------------------------------------------------------


def _resolve(project_id: str, request: Request, root, anchors: list[str], path: str = "") -> list[dict]:
    """`bin:` 走证据索引（spec §6）；`db:`、`code:` 在项目里有这个源的快照时也走索引；其余交给资料文本
    （file 锚点；没有快照的 db、code 等回 not_available）。"""
    collected = {source for source, _directory in index.workspace_snapshots(root).values()}
    system = [anchor for anchor in anchors if str(anchor).startswith("bin:") or (
        str(anchor).startswith(("db:", "code:")) and (query.parse_anchor(anchor) or {}).get("source_id") in collected)]
    resolved: dict[str, dict] = {}
    if system:
        declared = _declared(root, path)
        with index.connect(project_id, root) as connection:
            for anchor in system:
                resolved[anchor] = query.resolution(connection, anchor, declared=declared)
        _query_audit(request, project_id, "EV-29", anchors=len(system))
    others = [anchor for anchor in anchors if anchor not in resolved]
    files = dict(zip(others, material_text.resolve_many(root, others), strict=True))
    return [resolved.get(anchor) or files[anchor] for anchor in anchors]


def resolve_anchor(project_id: str, request: Request, anchor: str = "", path: str = ""):
    """EV-29（GET）：一个锚点。`path`（可选）是引用它的产物，按产物声明的快照解析。"""
    root = deps.require_owner(project_id, request)
    if not anchor.strip():
        raise ApiError("invalid_argument", "要指定 anchor", 400, {"fields": {"anchor": "required"}})
    return {"items": _resolve(project_id, request, root, [anchor], path)}


async def resolve_anchors(project_id: str, request: Request):
    """EV-29（POST）：一批锚点，最多 100 个，按请求顺序返回。"""
    root = deps.require_owner(project_id, request)
    body = await deps.json_body(request)
    anchors = body.get("anchors")
    if not isinstance(anchors, list) or not all(isinstance(item, str) for item in anchors):
        raise ApiError("invalid_argument", "anchors 要是文字列表", 400, {"fields": {"anchors": "list"}})
    if len(anchors) > ANCHORS_MAX:
        raise ApiError("invalid_argument", f"一次最多 {ANCHORS_MAX} 个锚点", 400, {"fields": {"anchors": "too_many"}})
    return {"items": _resolve(project_id, request, root, anchors, str(body.get("path") or ""))}


# ---------------------------------------------------------------------------
# 登记路由
# ---------------------------------------------------------------------------

# (接口编号, 方法, 路径, 处理函数, 成功状态码)；固定段先于路径参数
IMPLEMENTED = (
    ("EV-01", "GET", "/api/v1/evidence/sources", list_sources),
    ("EV-02", "POST", "/api/v1/admin/evidence/sources", register_source),
    ("EV-03", "POST", "/api/v1/evidence/source-requests", request_source),
    ("EV-04", "PATCH", "/api/v1/admin/evidence/sources/{source_id}", update_source),
    ("EV-05", "DELETE", "/api/v1/admin/evidence/sources/{source_id}", delete_source),
    ("EV-06", "PUT", "/api/v1/admin/evidence/sources/{source_id}/secret", write_secret),
    ("EV-07", "DELETE", "/api/v1/admin/evidence/sources/{source_id}/secret", delete_secret),
    ("EV-08", "POST", "/api/v1/evidence/sources/{source_id}/test", test_source),
    ("EV-09", "GET", "/api/v1/evidence/sources/{source_id}/access-log", access_log),
    ("EV-10", "GET", "/api/v1/evidence/providers", list_providers),
    ("EV-11", "GET", f"{_P}/evidence/grants", list_grants),
    ("EV-11", "PUT", f"{_P}/evidence/grants/{{source_id}}", put_grant),
    ("EV-12", "GET", f"{_P}/evidence/summary", project_summary),
    ("EV-13", "POST", f"{_P}/evidence/jobs/preview", preview_job),
    ("EV-14", "POST", f"{_P}/evidence/jobs", create_job),
    ("EV-15", "GET", f"{_P}/evidence/jobs", list_job_items),
    ("EV-16", "GET", f"{_P}/evidence/jobs/{{job_id}}", get_job),
    ("EV-17", "POST", f"{_P}/evidence/jobs/{{job_id}}/cancel", cancel_job),
    ("EV-17", "POST", f"{_P}/evidence/jobs/{{job_id}}/resume", resume_job),
    ("EV-17", "POST", f"{_P}/evidence/jobs/{{job_id}}/retry", retry_job),
    ("EV-18", "GET", f"{_P}/evidence/snapshots", list_snapshots),
    ("EV-21", "GET", f"{_P}/evidence/snapshots/diff", snapshot_diff),
    ("EV-22", "POST", f"{_P}/evidence/snapshots/diff/suggestions", diff_suggestions),
    ("EV-19", "GET", f"{_P}/evidence/snapshots/{{snapshot_id}}", get_snapshot),
    ("EV-20", "DELETE", f"{_P}/evidence/snapshots/{{snapshot_id}}", delete_snapshot),
    ("EV-23", "GET", f"{_P}/evidence/summary-tree", summary_tree),
    ("EV-24", "GET", f"{_P}/evidence/search", search),
    ("EV-25", "GET", f"{_P}/evidence/record", record),
    ("EV-26", "GET", f"{_P}/evidence/where-used", where_used),
    ("EV-27", "GET", f"{_P}/evidence/lineage", lineage),
    ("EV-28", "GET", f"{_P}/evidence/er", er_view),
    ("EV-29", "GET", f"{_P}/anchors/resolve", resolve_anchor),
    ("EV-29", "POST", f"{_P}/anchors/resolve", resolve_anchors),
    ("EV-30", "POST", "/api/v1/evidence/sources/{source_id}/notify-admin", notify_admin),
    ("EV-31", "POST", "/api/v1/evidence/uploads", start_upload),
    ("EV-32", "PUT", "/api/v1/evidence/uploads/{upload_id}/chunks/{index}", put_chunk),
    ("EV-33", "POST", "/api/v1/evidence/uploads/{upload_id}/complete", complete_upload),
    ("EV-34", "DELETE", "/api/v1/evidence/uploads/{upload_id}", cancel_upload),
)

for _api, _method, _path, _handler in IMPLEMENTED:
    router.add_api_route(_path, _handler, methods=[_method], include_in_schema=False)
for _api, _method, _path in ROUTES:
    router.add_api_route(_path, _reserved(_api), methods=[_method], include_in_schema=False)
