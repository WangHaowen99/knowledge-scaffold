"""服务令牌：外部服务调用三份契约接口的凭据（详细设计第 11、14 章，API 契约 1.3、X-06，ADR-0020）。

和 MCP token 分开签发、分开存储：MCP token 归项目所有者、代表一个人读写本体；服务令牌由
管理员签发，代表一个外部服务（如评测服务），只能调用三份契约的接口，读不到项目内容。

- 明文只在签发时返回一次，盘上只存 SHA-256 摘要（数据目录 service-tokens.json，0600）；
- 权限范围三选多：发布读取 releases.read、验证回执 receipts.write、反馈 feedback.write；
  可限定项目（project_ids），不限时为 null；可设有效期（expires_in_s，null 表示不过期）；
- 吊销后记录保留在库里供追溯，令牌立即失效；
- 令牌带固定前缀 onto_svc_，鉴权中间件据此区分登录 JWT 与服务令牌。
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

from .. import config
from .credential_store import SecretStore

TOKEN_PREFIX = "onto_svc_"
SCOPE_RELEASES_READ = "releases.read"
SCOPE_RECEIPTS_WRITE = "receipts.write"
SCOPE_FEEDBACK_WRITE = "feedback.write"
SCOPES = {
    SCOPE_RELEASES_READ: "读取发布列表、发布记录与包清单，按版本下载知识包",
    SCOPE_RECEIPTS_WRITE: "下载求解验证候选包、写入求解验证回执",
    SCOPE_FEEDBACK_WRITE: "写入外部反馈（失分用例、线上反馈）",
}
MAX_NAME_LENGTH = 80
MAX_PROJECTS = 200
MAX_TOKENS = 200
MIN_TTL_SECONDS = 300
MAX_TTL_SECONDS = 10 * 365 * 86400
# 最近使用时间不必每次请求都落盘：同一令牌一分钟内只记一次
LAST_USED_RESOLUTION_SECONDS = 60
_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

CODE_UNAUTHENTICATED = "unauthenticated"
CODE_SCOPE = "scope_forbidden"
CODE_INVALID = "invalid_argument"
CODE_NOT_FOUND = "not_found"


class ServiceTokenError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = CODE_INVALID, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.details = details


def _store() -> SecretStore:
    return SecretStore(lambda: Path(config.DATA_DIR) / "service-tokens.json")


def store_path() -> Path:
    return _store().path


def _digest(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def rfc3339(epoch: float | None) -> str | None:
    """API 契约 1.9：RFC 3339 UTC，毫秒精度，以 Z 结尾。"""
    if epoch is None:
        return None
    moment = datetime.fromtimestamp(epoch, timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def summary(record: dict) -> dict:
    """对外视图（X-06）：不含摘要，更不含明文。"""
    return {
        "id": record["id"],
        "name": record.get("name", ""),
        "scopes": list(record.get("scopes") or []),
        "project_ids": list(record["project_ids"]) if record.get("project_ids") else None,
        "created_at": rfc3339(record.get("created_at")),
        "created_by": record.get("created_by", ""),
        "expires_at": rfc3339(record.get("expires_at")),
        "last_used_at": rfc3339(record.get("last_used_at")),
    }


def _invalid(field: str, reason: str) -> ServiceTokenError:
    return ServiceTokenError(reason, 400, CODE_INVALID, {"fields": {field: reason}})


def _normalize(body: dict) -> dict:
    name = str(body.get("name") or "").strip()
    if not name or len(name) > MAX_NAME_LENGTH:
        raise _invalid("name", f"名称不能为空，且不超过 {MAX_NAME_LENGTH} 个字符")
    scopes = body.get("scopes")
    if not isinstance(scopes, list) or not scopes or any(scope not in SCOPES for scope in scopes):
        raise _invalid("scopes", f"必须是 {'、'.join(SCOPES)} 的非空子集")
    project_ids = body.get("project_ids")
    if project_ids is not None:
        if not isinstance(project_ids, list) or len(project_ids) > MAX_PROJECTS:
            raise _invalid("project_ids", f"必须是项目 ID 列表（最多 {MAX_PROJECTS} 个），不限项目时不传或传 null")
        project_ids = [str(item).strip() for item in project_ids]
        if any(not _PROJECT_ID_RE.match(item) for item in project_ids):
            raise _invalid("project_ids", "有无效的项目 ID")
    ttl = body.get("expires_in_s")
    if ttl is not None and (
        isinstance(ttl, bool) or not isinstance(ttl, int) or not MIN_TTL_SECONDS <= ttl <= MAX_TTL_SECONDS
    ):
        raise _invalid("expires_in_s", f"必须是 {MIN_TTL_SECONDS} 至 {MAX_TTL_SECONDS} 的整数秒，不过期时传 null")
    return {
        "name": name,
        "scopes": sorted(set(scopes)),
        "project_ids": sorted(set(project_ids)) if project_ids else None,
        "ttl": ttl,
    }


def issue(body: dict, issued_by: str = "") -> dict:
    """签发：返回 {token: 明文（只此一次）, summary}。"""
    fields = _normalize(body or {})
    raw = TOKEN_PREFIX + secrets.token_urlsafe(36)
    now = time.time()
    record = {
        "id": "svc_" + secrets.token_hex(8),
        "name": fields["name"],
        "digest": _digest(raw),
        "scopes": fields["scopes"],
        "project_ids": fields["project_ids"],
        "created_at": now,
        "created_by": issued_by,
        "expires_at": now + fields["ttl"] if fields["ttl"] else None,
        "last_used_at": None,
        "revoked_at": None,
    }

    def add(data: dict) -> None:
        tokens = data.setdefault("tokens", [])
        if sum(1 for item in tokens if not item.get("revoked_at")) >= MAX_TOKENS:
            raise ServiceTokenError(f"有效的服务令牌最多 {MAX_TOKENS} 个，请先吊销不用的", 409, "quota_exceeded")
        tokens.append(record)

    _store().update(add)
    return {"token": raw, "summary": summary(record)}


def listing() -> list[dict]:
    """有效（未吊销）的令牌，新的在前；已吊销的留在库里供追溯，不列出。"""
    tokens = _store().load().get("tokens") or []
    rows = [item for item in tokens if isinstance(item, dict) and not item.get("revoked_at")]
    rows.sort(key=lambda item: item.get("created_at") or 0, reverse=True)
    return [summary(item) for item in rows]


def revoke(token_id: str, revoked_by: str = "") -> dict:
    def mark(data: dict) -> dict | None:
        for item in data.get("tokens") or []:
            if item.get("id") == token_id and not item.get("revoked_at"):
                item["revoked_at"] = time.time()
                item["revoked_by"] = revoked_by
                return item
        return None

    record = _store().update(mark)
    if record is None:
        raise ServiceTokenError("服务令牌不存在或已吊销", 404, CODE_NOT_FOUND)
    return summary(record)


def looks_like_service_token(raw: str) -> bool:
    return bool(raw) and raw.startswith(TOKEN_PREFIX)


def verify(raw: str, scope: str, project_id: str | None = None) -> dict:
    """校验令牌、范围与项目；通过时返回摘要视图，否则抛 ServiceTokenError（401 / 403）。"""
    if not looks_like_service_token(raw):
        raise ServiceTokenError("缺少服务令牌或令牌无效", 401, CODE_UNAUTHENTICATED)
    digest = _digest(raw)
    now = time.time()
    record = None
    for item in _store().load().get("tokens") or []:
        # 逐条定长比较，不因匹配位置不同而泄露耗时差异
        if isinstance(item, dict) and hmac.compare_digest(str(item.get("digest") or ""), digest):
            record = item
    if record is None or record.get("revoked_at"):
        raise ServiceTokenError("服务令牌无效或已吊销", 401, CODE_UNAUTHENTICATED)
    if record.get("expires_at") is not None and record["expires_at"] <= now:
        raise ServiceTokenError("服务令牌已过期", 401, CODE_UNAUTHENTICATED)
    if scope not in (record.get("scopes") or []):
        raise ServiceTokenError("服务令牌的权限范围不够", 403, CODE_SCOPE, {"required_scope": scope})
    if project_id is not None and not allows_project(record, project_id):
        raise ServiceTokenError(
            "服务令牌不能访问该项目", 403, CODE_SCOPE, {"required_scope": scope, "project_id": project_id}
        )
    if now - (record.get("last_used_at") or 0) >= LAST_USED_RESOLUTION_SECONDS:
        _touch(record["id"], now)
    return summary(record)


def allows_project(token: dict, project_id: str) -> bool:
    projects = token.get("project_ids")
    return not projects or project_id in projects


def _touch(token_id: str, now: float) -> None:
    def mark(data: dict) -> None:
        for item in data.get("tokens") or []:
            if item.get("id") == token_id:
                item["last_used_at"] = now

    try:
        _store().update(mark)
    except OSError:
        pass  # 只是使用时间，写不进去不影响本次鉴权
