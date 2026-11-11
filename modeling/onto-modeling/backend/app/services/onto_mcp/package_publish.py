"""MCP 发布知识包版本（ADR-0098）：预览给出签名的确认令牌，确认后后台按 Hub 委托发布，可查状态、重启后续跑。

与网页发布同口径：门禁读主线最新提交导出的只读树，求解能力未验证要明确确认；版本号由 Hub 分配。
确认令牌绑定项目、MCP token、request_id、输入摘要与基础版本，5 分钟内有效；后台每一步对外动作前复核 token。
"""
import base64
import fcntl
import hashlib
import hmac
import json
import secrets
import time
from concurrent.futures import ThreadPoolExecutor

from .. import hub_packages, knowledge_hub
from ..publish import stages as publish_stages
from ..workspace import WorkspaceError
from . import tokens
from .errors import McpError

CONFIRM_TTL = 300
MAX_ACTIVE_JOBS = 32
MAX_ACTIVE_JOBS_PER_TOKEN = 2
CONFIRMATION_KEY = "onto-mcp-confirmation.key"
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="onto-mcp-publish")


def _secret(manager):
    """确认令牌的签名密钥：放密钥目录（方案 E10），第一次用时生成；丢了只让未确认的预览失效。"""
    from ...governance import secrets as secret_store

    path = secret_store.path(CONFIRMATION_KEY, manager.workspaces_dir.parent)
    with (path.parent / (CONFIRMATION_KEY + ".lock")).open("a") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            value = path.read_bytes() if path.exists() else b""
            if not value:
                manager._atomic_write(path, secrets.token_bytes(32))
                path.chmod(0o600)
                value = path.read_bytes()
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
    if len(value) != 32:
        raise McpError("PUBLISH_CONFIRMATION_UNAVAILABLE", "发布确认密钥不可用", 503)
    return value


def _encode(manager, payload):
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(_secret(manager), encoded.encode(), hashlib.sha256).hexdigest()
    return encoded + "." + signature


def _decode(manager, value):
    try:
        encoded, supplied = value.split(".", 1)
        expected = hmac.new(_secret(manager), encoded.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, supplied):
            raise ValueError
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        if not isinstance(payload, dict):
            raise ValueError
    except (ValueError, TypeError, KeyError):
        raise McpError("PUBLISH_CONFIRMATION_INVALID", "发布确认无效，请重新预览", 400) from None
    if payload.get("exp", 0) < time.time():
        raise McpError("PUBLISH_CONFIRMATION_EXPIRED", "发布确认已过期，请重新预览", 409)
    return payload


def _jobs(manager):
    # 任务跟着所属的工作区管理器放：生产只有一个，测试与维护工具各自隔离
    root = manager.workspaces_dir.parent / "hub-tasks" / "mcp-publish-jobs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _job_path(manager, job_id):
    return _jobs(manager) / (knowledge_hub.identifier(job_id) + ".json")


def _save_job(manager, job):
    knowledge_hub.save(_job_path(manager, job["job_id"]), job)


def _load_job(manager, job_id, principal):
    job = knowledge_hub.load(_job_path(manager, job_id))
    if not job or job.get("project_id") != principal["project_id"] or job.get("token_id") != principal["token_id"]:
        raise McpError("PUBLISH_JOB_NOT_FOUND", "发布任务不存在", 404)
    return job


def _verified_release(project_id, subject, revision):
    """同一求解契约修订在早先版本通过了评测服务的求解验证（评测对接提供）；没有这项能力时为 None。"""
    try:
        from .. import package_evaluations
    except ImportError:
        return None
    verified = getattr(package_evaluations, "verified_solver_release", None)
    return verified(project_id, subject, revision) if verified else None


def _blockers(manager, project_id, allow_unbound):
    """发布检查（PB-01 同口径）里挡发布的门禁；降级只放宽数据绑定。"""
    from .. import publish

    check = publish.package_binding_check(manager, project_id)
    failing = publish.gates.blocking(check["gates"], degrade=allow_unbound)
    return check, [{"code": gate["id"], "message": gate["summary"]} for gate in failing]


def preview(manager, principal, args):
    request_id = args.get("request_id") or secrets.token_hex(16)
    allow_unbound = args.get("allow_unbound", False)
    project_id = principal["project_id"]
    if not principal.get("hub_issuer") or not principal.get("hub_sub"):
        raise McpError("MCP_HUB_IDENTITY_REQUIRED", "发布委托缺少 Hub 用户身份", 401)
    if not knowledge_hub.configured():
        raise McpError("HUB_UNAVAILABLE", "知识包 Hub 未配置", 503)
    # 与网页同一规则：四个必经建模阶段没做完不能发布，告诉调用方缺哪几个
    readiness = publish_stages.readiness(manager, project_id)
    if not readiness["ready"]:
        message = publish_stages.message(readiness["missing_stages"])
        return {"ready": False, "request_id": request_id,
                "blockers": [{"code": publish_stages.CODE, "message": message,
                              "missing_stages": readiness["missing_stages"]}],
                "display": {"text": message}}
    try:
        check, blockers = _blockers(manager, project_id, allow_unbound)
        if not blockers:
            _, digest = knowledge_hub.source_state(manager, project_id, allow_unbound, commit=check["commit"])
    except WorkspaceError as exc:
        blockers = [{"code": exc.code, "message": str(exc)}]
    if blockers:
        return {"ready": False, "request_id": request_id, "blockers": blockers,
                "display": {"text": "当前项目尚不能发布知识包版本"}}
    subject = json.dumps([principal["hub_issuer"], principal["hub_sub"]], ensure_ascii=False)
    base = hub_packages.association(project_id, subject)
    solver = _solver_gate(project_id, subject, check.get("solver") or {})
    try:
        hub_packages.check_delegation(principal["token_id"], project_id, principal["hub_issuer"], principal["hub_sub"])
    except WorkspaceError as exc:
        return {"ready": False, "request_id": request_id,
                "blockers": [{"code": exc.code, "message": str(exc)}],
                "display": {"text": "当前发布授权不可用"}}
    issued = time.time()
    payload = {"v": 1, "project_id": project_id, "token_id": principal["token_id"],
               "request_id": request_id, "allow_unbound": allow_unbound, "input_digest": digest,
               "commit": check["commit"],
               "base_release_id": (base or {}).get("release_id"),
               "base_digest": (base or {}).get("package_digest"), "iat": issued, "exp": issued + CONFIRM_TTL,
               "solver_revision": solver["revision"] if solver else None,
               "solver_verified": bool(solver and solver["verified_release"])}
    text = "发布检查通过，请确认后发布知识包新版本"
    if solver and solver["requires_acknowledgement"]:
        text = ("发布检查通过。求解能力尚未经端到端验证：确认发布时须传 solver_unverified_acknowledged=true，"
                "发布后在评测服务中对新版本发起求解验证")
    elif solver:
        text = f"发布检查通过，求解契约与已通过求解验证的 {solver['verified_release']['version']} 一致，请确认后发布知识包新版本"
    return {"ready": True, "request_id": request_id, "source_digest": digest, "commit": check["commit"],
            "ontology_id": hub_packages.ontology_id(project_id),
            "first_publish": not bool(base), "base_release_id": payload["base_release_id"],
            "base_digest": payload["base_digest"], "change_type": "modeling_content",
            "confirmation_token": _encode(manager, payload), "expires_at": payload["exp"],
            **({"solver": solver} if solver else {}), "display": {"text": text}}


def _solver_gate(project_id, subject, solver):
    """发布要不要确认求解能力未验证：与网页发布检查同一规则。"""
    if not solver.get("requires_validation_after_publish"):
        return None
    verified = _verified_release(project_id, subject, solver["revision"])
    return {"revision": solver["revision"], "requires_acknowledgement": not verified, "verified_release": verified}


def start(manager, principal, args):
    payload = _decode(manager, args["confirmation_token"])
    expected = (principal["project_id"], principal["token_id"], args["request_id"])
    actual = (payload.get("project_id"), payload.get("token_id"), payload.get("request_id"))
    if actual != expected:
        raise McpError("PUBLISH_CONFIRMATION_MISMATCH", "发布确认与当前项目、Token 或请求不匹配", 403)
    job_id = hashlib.sha256(json.dumps([*expected], ensure_ascii=False).encode()).hexdigest()[:32]
    with knowledge_hub.lock("mcp-publish-queue"):
        path = _job_path(manager, job_id)
        existing = knowledge_hub.load(path)
        if existing:
            intent_fields = ("project_id", "token_id", "request_id", "allow_unbound", "input_digest",
                             "base_release_id", "base_digest")
            previewed = existing.get("preview", {})
            if any(previewed.get(key) != payload.get(key) for key in intent_fields):
                raise McpError("PUBLISH_REQUEST_CONFLICT", "相同 request_id 的发布内容发生变化，请使用新的 request_id", 409)
            if existing.get("status") != "failed":
                return public(existing)
        # 与网页发布同一规则：求解能力未验证时须明确确认；重查已有任务不必重复确认
        acknowledged = args.get("solver_unverified_acknowledged") is True
        if payload.get("solver_revision") and not payload.get("solver_verified") and not acknowledged:
            raise McpError("SOLVER_VALIDATION_ACKNOWLEDGEMENT_REQUIRED",
                           "求解能力尚未经端到端验证：请传 solver_unverified_acknowledged=true 确认后再发布，"
                           "发布后在评测服务中发起求解验证", 409)
        active = []
        for job_path in _jobs(manager).glob("*.json"):
            try:
                item = knowledge_hub.load(job_path)
            except (OSError, ValueError):
                continue
            if item and item.get("status") in ("queued", "running") and not (
                    existing and item.get("job_id") == existing.get("job_id")):
                active.append(item)
        if len(active) >= MAX_ACTIVE_JOBS or sum(
                item.get("token_id") == principal["token_id"] for item in active) >= MAX_ACTIVE_JOBS_PER_TOKEN:
            raise McpError("PUBLISH_QUEUE_LIMIT", "知识包发布任务较多，请等待已有任务完成后重试", 429)
        job = existing or {"job_id": job_id, "project_id": principal["project_id"],
                           "token_id": principal["token_id"], "request_id": args["request_id"],
                           "created_at": time.time()}
        job.update(status="queued", updated_at=time.time(), preview=payload, solver_acknowledged=acknowledged)
        job.pop("error", None)
        _save_job(manager, job)
    _executor.submit(_run, manager, job_id)
    return public(job)


def _run(manager, job_id):
    with knowledge_hub.lock("mcp-publish-job-" + job_id):
        path = _job_path(manager, job_id)
        job = knowledge_hub.load(path)
        if not job or job.get("status") not in ("queued", "running"):
            return
        try:
            principal = tokens.verify_id(manager, job["project_id"], job["token_id"], "package.publish")
            if not principal["hub_issuer"] or not principal["hub_sub"]:
                raise McpError("MCP_HUB_IDENTITY_REQUIRED", "发布委托缺少 Hub 用户身份", 401)
            job.update(status="running", updated_at=time.time())
            _save_job(manager, job)
            previewed = job["preview"]
            subject = json.dumps([principal["hub_issuer"], principal["hub_sub"]], ensure_ascii=False)

            def recheck():
                tokens.verify_id(manager, job["project_id"], job["token_id"], "package.publish")

            acknowledged = job.get("solver_acknowledged") is True
            author = principal.get("username") or principal["hub_sub"]
            receipt = hub_packages.publish(
                manager, job["project_id"], job["request_id"], "", subject,
                previewed["allow_unbound"], delegated={"token_id": job["token_id"],
                                                       "issuer": principal["hub_issuer"],
                                                       "sub": principal["hub_sub"]},
                expected_input_digest=previewed["input_digest"],
                expected_base=(previewed.get("base_release_id"), previewed.get("base_digest")),
                before_external=recheck, commit=previewed.get("commit"), author=author,
                acknowledged_by=author if acknowledged else "",
                verified_revision=(previewed.get("solver_revision") or "") if previewed.get("solver_verified") else "")
            job.update(status="committed", receipt=receipt, updated_at=time.time())
        except (McpError, WorkspaceError) as exc:
            job.update(status="failed", error={"code": exc.code, "message": str(exc),
                                               "retryable": exc.status in (409, 503)}, updated_at=time.time())
        except Exception:  # noqa: BLE001 - 后台任务的意外错误落到任务状态里，不丢
            job.update(status="failed", error={"code": "PUBLISH_INTERNAL_ERROR",
                                               "message": "知识包版本发布失败", "retryable": False},
                       updated_at=time.time())
        _save_job(manager, job)


def status(manager, principal, args):
    return public(_load_job(manager, args["job_id"], principal))


def public(job):
    result = {key: job[key] for key in ("job_id", "request_id", "status", "created_at", "updated_at") if key in job}
    if "receipt" in job:
        result["receipt"] = job["receipt"]
    if "error" in job:
        result["error"] = job["error"]
    result["display"] = {"text": {"queued": "发布任务已排队", "running": "正在发布知识包版本",
                                  "committed": "知识包新版本发布成功", "failed": "知识包版本发布失败"}.get(
                                      job.get("status"), "发布任务状态未知")}
    return result


def execute(manager, principal, name, args):
    if name == "preview_package_publish":
        return preview(manager, principal, args)
    if name == "publish_package_version":
        return start(manager, principal, args)
    if name == "get_package_publish_status":
        return status(manager, principal, args)
    raise McpError("MCP_TOOL_NOT_FOUND", "未知知识包发布工具", 404)


def recover(manager):
    """进程重启后续跑排队中、运行中的任务。"""
    for path in _jobs(manager).glob("*.json"):
        try:
            job = knowledge_hub.load(path)
            if job.get("status") in ("queued", "running"):
                job.update(status="queued", updated_at=time.time())
                _save_job(manager, job)
                _executor.submit(_run, manager, job["job_id"])
        except (OSError, ValueError, KeyError):
            continue


def maintain(manager):
    """清掉 30 天前结束的任务记录；Hub 上的版本与回执才是权威。"""
    cutoff = time.time() - 30 * 86400
    for path in _jobs(manager).glob("*.json"):
        try:
            job = knowledge_hub.load(path)
            if job.get("status") in ("committed", "failed") and job.get("updated_at", 0) < cutoff:
                path.unlink(missing_ok=True)
        except (OSError, ValueError):
            continue
