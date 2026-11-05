"""Recoverable, project-bound package publication for Onto MCP."""
import base64
from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import hmac
import json
import secrets
import time

from .. import hub_packages, knowledge_hub
from ..workspace import WorkspaceError
from . import tokens
from .errors import McpError

CONFIRM_TTL = 300
MAX_ACTIVE_JOBS = 32
MAX_ACTIVE_JOBS_PER_TOKEN = 2
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="onto-mcp-publish")


def _secret(manager):
    path = manager.workspaces_dir.parent / "onto-mcp-confirmation.key"
    path.parent.mkdir(parents=True, exist_ok=True)
    with (path.parent / "onto-mcp-confirmation.lock").open("a") as lock_file:
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
        if payload.get("exp", 0) < time.time():
            raise McpError("PUBLISH_CONFIRMATION_EXPIRED", "发布确认已过期，请重新预览", 409)
        return payload
    except McpError:
        raise
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        raise McpError("PUBLISH_CONFIRMATION_INVALID", "发布确认无效，请重新预览", 400) from None


def _jobs(manager):
    # Keep durable jobs beside the workspace manager they belong to. Production has one
    # manager, while isolated managers (tests and maintenance tools) must not share queues.
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


def preview(manager, principal, args):
    request_id = args.get("request_id") or secrets.token_hex(16)
    allow_unbound = args.get("allow_unbound", False)
    if not principal.get("hub_issuer") or not principal.get("hub_sub"):
        raise McpError("MCP_HUB_IDENTITY_REQUIRED", "发布委托缺少 Hub 用户身份", 401)
    if not knowledge_hub.configured():
        raise McpError("HUB_UNAVAILABLE", "知识包 Hub 未配置", 503)
    try:
        manager._ensure_solver_publishable(principal["project_id"])
        _, digest = knowledge_hub.source_state(manager, principal["project_id"], allow_unbound)
    except WorkspaceError as exc:
        return {"ready": False, "request_id": request_id,
                "blockers": [{"code": exc.code, "message": str(exc)}],
                "display": {"text": "当前项目尚不能发布知识包版本"}}
    base = hub_packages.association(principal["project_id"], json.dumps(
        [principal["hub_issuer"], principal["hub_sub"]], ensure_ascii=False))
    try:
        hub_packages.check_delegation(principal["token_id"], principal["project_id"],
                                      principal["hub_issuer"], principal["hub_sub"])
    except WorkspaceError as exc:
        return {"ready": False, "request_id": request_id,
                "blockers": [{"code": exc.code, "message": str(exc)}],
                "display": {"text": "当前发布授权不可用"}}
    issued = time.time()
    payload = {"v": 1, "project_id": principal["project_id"], "token_id": principal["token_id"],
               "request_id": request_id, "allow_unbound": allow_unbound, "input_digest": digest,
               "base_release_id": (base or {}).get("release_id"),
               "base_digest": (base or {}).get("package_digest"), "iat": issued, "exp": issued + CONFIRM_TTL}
    return {"ready": True, "request_id": request_id, "source_digest": digest,
            "ontology_id": hub_packages.ontology_id(principal["project_id"]),
            "first_publish": not bool(base), "base_release_id": payload["base_release_id"],
            "base_digest": payload["base_digest"], "change_type": "modeling_content",
            "confirmation_token": _encode(manager, payload), "expires_at": payload["exp"],
            "display": {"text": "发布检查通过，请确认后发布知识包新版本"}}


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
            if any(existing.get("preview", {}).get(key) != payload.get(key) for key in intent_fields):
                raise McpError("PUBLISH_REQUEST_CONFLICT", "相同 request_id 的发布内容发生变化，请使用新的 request_id", 409)
            if existing.get("status") != "failed":
                return public(existing)
        active = []
        for job_path in _jobs(manager).glob("*.json"):
            try:
                active.append(knowledge_hub.load(job_path))
            except (OSError, ValueError):
                continue
        active = [item for item in active if item and item.get("status") in ("queued", "running")
                  and (not existing or item.get("job_id") != existing.get("job_id"))]
        if len(active) >= MAX_ACTIVE_JOBS or sum(
                item.get("token_id") == principal["token_id"] for item in active) >= MAX_ACTIVE_JOBS_PER_TOKEN:
            raise McpError("PUBLISH_QUEUE_LIMIT", "知识包发布任务较多，请等待已有任务完成后重试", 429)
        job = existing or {"job_id": job_id, "project_id": principal["project_id"],
                           "token_id": principal["token_id"], "request_id": args["request_id"],
                           "created_at": time.time()}
        job.update(status="queued", updated_at=time.time(), preview=payload)
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
            preview = job["preview"]
            subject = json.dumps([principal["hub_issuer"], principal["hub_sub"]], ensure_ascii=False)
            def recheck():
                tokens.verify_id(manager, job["project_id"], job["token_id"], "package.publish")
            receipt = hub_packages.publish(
                manager, job["project_id"], job["request_id"], "", subject,
                preview["allow_unbound"], delegated={"token_id": job["token_id"],
                                                       "issuer": principal["hub_issuer"],
                                                       "sub": principal["hub_sub"]},
                expected_input_digest=preview["input_digest"],
                expected_base=(preview.get("base_release_id"), preview.get("base_digest")),
                before_external=recheck)
            job.update(status="committed", receipt=receipt, updated_at=time.time())
        except (McpError, WorkspaceError) as exc:
            job.update(status="failed", error={"code": exc.code, "message": str(exc),
                                               "retryable": exc.status in (409, 503)}, updated_at=time.time())
        except Exception:
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
    """Resume durable queued/running jobs after a process restart."""
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
    """Expire old terminal job metadata; Hub receipts and packages remain authoritative."""
    cutoff = time.time() - 30 * 86400
    for path in _jobs(manager).glob("*.json"):
        try:
            job = knowledge_hub.load(path)
            if job.get("status") in ("committed", "failed") and job.get("updated_at", 0) < cutoff:
                path.unlink(missing_ok=True)
        except (OSError, ValueError):
            continue
