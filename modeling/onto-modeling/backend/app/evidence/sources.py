"""数据源登记与申请（契约 EV-01～EV-05、EV-03、EV-30；`design/evidence-collection.md` §6「自助登记」）。

登记存 `DATA_DIR/evidence/sources.json`（平台级，不在工作区）。只存凭据引用和不敏感的连接参数。

- 管理员可以登记任何已安装的提供方；
- 提供方 `self_service=true`（不要凭据、不连业务网络，如 REA）时登录用户也能登记，`owner` 固定为本人，登记只有本人和
  管理员看得到，本人才能授权给自己的项目；
- 别人登记的自助类数据源不出现在列表里，也不能授权。
"""

from __future__ import annotations

import re
import threading
import time
import urllib.parse
import uuid
from datetime import datetime

from ..api.errors import ApiError
from . import grants, storage
from .registry import SOURCE_ID_PATTERN

_lock = threading.Lock()
SOURCE_ID = re.compile(SOURCE_ID_PATTERN)
NAME_MAX = 80
NOTE_MAX = 2000
SECRET_MAX = 8192
MASK = "«已遮蔽»"
# 网关拒绝写凭据时原样带回的状态；其余（鉴权、5xx）算上游不可用
PASS_THROUGH = (400, 404, 409, 413, 422)
ENVIRONMENTS = ("production", "test")
# 自助类（REA）的缺省：任务说明里的预算按 spec §4 的例子
SELF_SERVICE_BUDGET = {"max_rows": 200000, "max_bytes": 268435456, "max_duration_s": 1800}
SELF_SERVICE_RATE = {"concurrency": 1, "qps": 1, "query_timeout_s": 1800}
ARTIFACT_KEYS = ("artifact_sha256", "artifact_filename", "artifact_size")
# 登记后不能改的字段
FIXED = ("id", "provider", "owner", "self_service", "registered_by_admin", "created_at")


def _path():
    return storage.root() / "sources.json"


def _load() -> list[dict]:
    data = storage.read_json(_path(), {}) or {}
    return [dict(item) for item in data.get("items") or [] if isinstance(item, dict) and item.get("id")]


def _save(items: list[dict]) -> None:
    storage.write_json(_path(), {"items": items})


def all_sources() -> list[dict]:
    return _load()


def get(source_id: str) -> dict | None:
    return next((item for item in _load() if item["id"] == source_id), None)


def visible(registration: dict, user: str, admin: bool) -> bool:
    return admin or registration.get("registered_by_admin", True) or registration.get("owner") == user


def can_manage(registration: dict, user: str, admin: bool) -> bool:
    """EV-04、EV-05：管理员，或自助类数据源的登记人。"""
    return admin or bool(registration.get("self_service") and registration.get("owner") == user)


def can_grant(registration: dict, user: str, admin: bool) -> bool:
    """授权给自己的项目：管理员登记的谁都能授权（项目所有者自己决定），自助登记的只有登记人。"""
    return bool(registration.get("registered_by_admin", True) or registration.get("owner") == user)


def require_visible(source_id: str, user: str, admin: bool) -> dict:
    registration = get(source_id)
    if registration is None or not visible(registration, user, admin):
        raise ApiError("source_not_found", "数据源不存在", 404)
    return registration


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


def _invalid(field: str, reason: str, message: str = "") -> ApiError:
    return ApiError("invalid_argument", message or f"{field} 不合法", 400, {"fields": {field: reason}})


def _positive_ints(value, field: str, keys: tuple[str, ...]) -> dict:
    if not isinstance(value, dict):
        raise _invalid(field, "object")
    out = {}
    for key in keys:
        item = value.get(key)
        if isinstance(item, bool) or not isinstance(item, (int, float)) or item < 0:
            raise _invalid(f"{field}.{key}", "non_negative_number")
        out[key] = int(item) if isinstance(item, int) or float(item).is_integer() else float(item)
    return out


def _strings(value, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise _invalid(field, "string_list")
    return [item.strip() for item in value][:500]


def _egress(value) -> list[dict]:
    if not isinstance(value, list):
        raise _invalid("egress", "list")
    out = []
    for item in value:
        port = item.get("port") if isinstance(item, dict) else None
        if not isinstance(item, dict) or not str(item.get("host") or "").strip() or isinstance(port, bool) \
                or not isinstance(port, int) or not 0 < port < 65536:
            raise _invalid("egress", "host_port")
        out.append({"host": str(item["host"]).strip(), "port": port})
    return out


def _window(value) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise _invalid("access_window", "object")
    clock = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
    days = value.get("days", "daily")
    if not (days == "daily" or isinstance(days, list) and all(isinstance(day, str) for day in days)):
        raise _invalid("access_window.days", "daily_or_list")
    if not clock.match(str(value.get("start") or "")) or not clock.match(str(value.get("end") or "")):
        raise _invalid("access_window", "hh_mm")
    return {"days": days, "start": value["start"], "end": value["end"], "tz": str(value.get("tz") or "UTC")}


def _artifact_connection(value) -> dict:
    if not isinstance(value, dict):
        raise _invalid("connection", "object")
    sha256 = str(value.get("artifact_sha256") or "").lower()
    size = storage.blob_size(sha256)
    if size is None:
        raise ApiError("artifact_not_found", "目标程序还没上传完", 422)
    filename = str(value.get("artifact_filename") or "").strip()[:200] or sha256[:12]
    return {"artifact_sha256": sha256, "artifact_filename": filename, "artifact_size": size}


def _connection(value) -> dict:
    if not isinstance(value, dict):
        raise _invalid("connection", "object")
    out = {}
    for key, item in value.items():
        if isinstance(item, bool) or not isinstance(item, (str, int, float)):
            raise _invalid(f"connection.{key}", "scalar")
        out[str(key)] = item
    return out


# ---------------------------------------------------------------------------
# 连接型提供方（数据库、代码仓库；spec §8～§10）
# ---------------------------------------------------------------------------


def masking_policies(info: dict) -> list[str]:
    """提供方支持的脱敏策略，第一个是缺省：提供方声明了 `masking_policies` 就用它；否则数据库是 `mask-v1`，其他（REA、代码）是 `none`。"""
    declared = [str(item) for item in info.get("masking_policies") or [] if str(item).strip()]
    if declared:
        return declared
    return ["mask-v1"] if info.get("kind") == "db" else ["none"]


def connection_type(info: dict) -> bool:
    """要凭据、按连接参数采集的提供方（不是自助类、不收目标程序）。"""
    return info.get("input") == "connection" and not info.get("self_service")


def _cred_fields(info: dict) -> list[dict]:
    return [field for field in info.get("cred_fields") or [] if isinstance(field, dict) and field.get("name")]


def secret_names(info: dict) -> list[str]:
    return [str(field["name"]) for field in _cred_fields(info) if field.get("secret")]


def _typed_connection(value, info: dict) -> dict:
    """`connection` 只收提供方 `cred_fields` 里的非密钥字段；密钥字段放进来直接拒绝（不回显值）。"""
    if not isinstance(value, dict):
        raise _invalid("connection", "object")
    plain = [field for field in _cred_fields(info) if not field.get("secret")]
    allowed = {str(field["name"]) for field in plain}
    secret = set(secret_names(info))
    out: dict = {}
    for key, item in value.items():
        key = str(key)
        if key in secret:
            raise _invalid(f"connection.{key}", "secret_field", "密钥不能写进连接参数")
        if key not in allowed:
            raise _invalid(f"connection.{key}", "unknown")
        if isinstance(item, bool) or not isinstance(item, (str, int)):
            raise _invalid(f"connection.{key}", "scalar")
        item = item.strip()[:500] if isinstance(item, str) else item
        if item != "":
            out[key] = item
    for field in plain:
        if field.get("required", True) and str(field["name"]) not in out:
            raise _invalid(f"connection.{field['name']}", "required")
    if "port" in out:
        port = out["port"]
        port = int(port) if isinstance(port, str) and port.isdigit() else port
        if not isinstance(port, int) or not 0 < port < 65536:
            raise _invalid("connection.port", "port")
        out["port"] = port
    if "repo_url" in out:
        _repo_endpoint(str(out["repo_url"]))
    return out


def _repo_endpoint(url: str) -> tuple[str, int]:
    """仓库地址只认 https，不能带用户名和令牌（令牌走凭据）。"""
    parsed = urllib.parse.urlsplit(url)
    try:
        port = parsed.port
    except ValueError:
        raise _invalid("connection.repo_url", "url") from None
    if parsed.scheme != "https" or not parsed.hostname:
        raise _invalid("connection.repo_url", "https", "仓库地址要用 https")
    if parsed.username or parsed.password:
        raise _invalid("connection.repo_url", "userinfo", "仓库地址里不能带用户名和令牌")
    return parsed.hostname.lower(), port or 443


def connection_endpoint(kind: str, connection: dict) -> tuple[str, int] | None:
    """采集要连的主机和端口：数据库是 host:port，代码仓库是仓库主机的 443（地址里写了端口就用它）。"""
    if kind == "db" and connection.get("host") and isinstance(connection.get("port"), int):
        return str(connection["host"]).lower(), int(connection["port"])
    if kind == "code" and connection.get("repo_url"):
        return _repo_endpoint(str(connection["repo_url"]))
    return None


def scope_pattern(item: str) -> bool:
    """范围写法 `crm.*`、`crm.customer`、`erp.store*`：`*` 只能在末尾。"""
    return bool(item) and "*" not in item[:-1] and not any(char.isspace() for char in item)


def scope_within(item: str, patterns: list[str]) -> bool:
    """范围项（或一个对象名）落在某条白名单写法里：`crm.customer`、`crm.c*` 都在 `crm.*` 里。"""
    for pattern in patterns:
        if pattern.endswith("*"):
            if item.startswith(pattern[:-1]):
                return True
        elif item == pattern:
            return True
    return False


def _check_connection_type(registration: dict, kind: str) -> None:
    """登记（新建或合并修改后）的整体约束：出口白名单要覆盖连接目标，数据库要写范围白名单，预算和限流都是正数。"""
    egress = registration.get("egress") or []
    if not egress:
        raise _invalid("egress", "required", "出口白名单必填")
    endpoint = connection_endpoint(kind, registration.get("connection") or {})
    if endpoint and endpoint not in {(str(item["host"]).lower(), int(item["port"])) for item in egress}:
        raise _invalid("egress", "missing_target", f"出口白名单里要有 {endpoint[0]}:{endpoint[1]}")
    scope = registration.get("scope_allowlist") or []
    if kind == "db" and not scope:
        raise _invalid("scope_allowlist", "required", "数据库要写范围白名单")
    for item in scope:
        if not scope_pattern(item):
            raise _invalid("scope_allowlist", "pattern", "范围写法如 crm.*、crm.customer，* 只能在末尾")
    for key in ("budget", "rate_limit"):
        if any(value <= 0 for value in (registration.get(key) or {}).values()):
            raise _invalid(key, "positive")


def secret_fields(value, info: dict) -> dict[str, str]:
    """EV-06 的 `fields`、EV-02/04 的 `secret`：只收提供方声明的密钥字段，值是非空文字。报错不带值。"""
    if not isinstance(value, dict) or not value:
        raise _invalid("fields", "object")
    allowed = set(secret_names(info))
    out = {}
    for key, item in value.items():
        if str(key) not in allowed:
            raise _invalid(f"fields.{key}", "unknown")
        if not isinstance(item, str) or not item or len(item) > SECRET_MAX:
            raise _invalid(f"fields.{key}", "non_empty_string")
        out[str(key)] = item
    return out


def _gateway_failure(exc, secrets) -> ApiError:
    """网关拒绝写凭据：把网关的错误码带回去；消息里万一回显了明文也遮掉。鉴权类和 5xx 一律算上游不可用。"""
    def scrub(text: str) -> str:
        for value in secrets:
            if value:
                text = text.replace(value, MASK)
        return text

    code, message = scrub(exc.code), scrub(exc.message)
    if exc.status in PASS_THROUGH:
        return ApiError(code, message, exc.status)
    return ApiError("upstream_unavailable", "连接器网关拒绝了请求", 502, {"gateway_error": code,
                                                                     "gateway_status": exc.status})


def _store(source_id: str, **changes) -> dict:
    with _lock:
        items = _load()
        for index, item in enumerate(items):
            if item["id"] == source_id:
                items[index] = {**item, **changes, "updated_at": time.time()}
                _save(items)
                return items[index]
    raise ApiError("source_not_found", "数据源不存在", 404)


def write_secret(source_id: str, fields: dict[str, str]) -> dict:
    """转交网关 `PUT /v1/secrets/{源}`，登记里只存新的 `cred_ref` 和这一版各字段的写入时间。"""
    from . import collector

    try:
        result = collector.put_secret(source_id, fields)
    except collector.GatewayUnavailable:
        raise ApiError("gateway_unavailable", "连接器网关不可达", 503) from None
    except collector.GatewayError as exc:
        raise _gateway_failure(exc, list(fields.values())) from None
    cred_ref = str(result.get("cred_ref") or "")
    if not cred_ref.startswith(f"cred://{source_id}/"):
        raise ApiError("upstream_unavailable", "连接器网关回的凭据引用不对", 502)
    try:
        written = datetime.fromisoformat(str(result.get("written_at") or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        written = time.time()
    # 新版本只含这次写的字段：没写的字段在新版本里就没有了
    return _store(source_id, cred_ref=cred_ref, secret_written_at={name: written for name in fields})


def delete_secret(source_id: str) -> dict:
    """网关 `DELETE /v1/secrets/{源}`（删所有版本），`cred_ref` 置空。网关里本来就没有也算删掉了。"""
    from . import collector

    try:
        collector.delete_secret(source_id)
    except collector.GatewayUnavailable:
        raise ApiError("gateway_unavailable", "连接器网关不可达", 503) from None
    except collector.GatewayError as exc:
        if exc.status != 404:
            raise _gateway_failure(exc, ()) from None
    return _store(source_id, cred_ref=None, secret_written_at={})


def _clean(body: dict, info: dict, *, creating: bool, current: dict | None = None) -> dict:
    """按提供方校验登记字段；更新时只校验带上的字段。"""
    self_service = bool(info.get("self_service"))
    artifact_input = info.get("input") == "artifact"
    out: dict = {}

    def given(key: str) -> bool:
        return key in body and (creating or body[key] is not None or key == "access_window")

    if creating or given("display_name"):
        name = str(body.get("display_name") or "").strip()
        if not name or len(name) > NAME_MAX:
            raise _invalid("display_name", "required", f"显示名必填，最多 {NAME_MAX} 字")
        out["display_name"] = name
    if creating or given("environment"):
        environment = body.get("environment") or ("test" if self_service and creating else None)
        if environment not in ENVIRONMENTS:
            raise _invalid("environment", "enum")
        out["environment"] = environment
    if creating or given("connection"):
        raw = body.get("connection") if body.get("connection") is not None else {}
        if artifact_input:
            out["connection"] = _artifact_connection(body.get("connection"))
        elif connection_type(info) and "cred_fields" in info:
            out["connection"] = _typed_connection(raw, info)
        else:
            out["connection"] = _connection(raw)
    for key, parse, default in (
        ("scope_allowlist", lambda value: _strings(value, "scope_allowlist"), []),
        ("egress", _egress, []),
        ("budget", lambda value: _positive_ints(value, "budget", ("max_rows", "max_bytes", "max_duration_s")),
         dict(SELF_SERVICE_BUDGET)),
        ("rate_limit", lambda value: _positive_ints(value, "rate_limit", ("concurrency", "qps", "query_timeout_s")),
         dict(SELF_SERVICE_RATE)),
    ):
        if given(key) and body.get(key) is not None:
            out[key] = parse(body[key])
        elif creating:
            if not self_service:
                raise _invalid(key, "required")
            out[key] = default
    if given("access_window"):
        out["access_window"] = _window(body.get("access_window"))
    elif creating:
        out["access_window"] = None
    if given("masking_policy") or creating:
        allowed = masking_policies(info)
        policy = str(body.get("masking_policy") or "").strip() or allowed[0]
        if policy not in allowed:
            raise _invalid("masking_policy", "unsupported", f"脱敏策略只能是 {'、'.join(allowed)}")
        out["masking_policy"] = policy
    if given("authorization_note") or creating:
        note = str(body.get("authorization_note") or "").strip()[:NOTE_MAX] or None
        out["authorization_note"] = note
    note = out.get("authorization_note", (current or {}).get("authorization_note"))
    if info.get("kind") == "rea" and not note:
        raise _invalid("authorization_note", "required", "逆向类数据源要写软件授权说明")
    return out


def _provider_info(provider_id: str) -> dict:
    from . import collector

    providers = collector.catalog()
    if providers is None:
        raise ApiError("gateway_unavailable", "连接器网关不可达", 503)
    info = next((item for item in providers if item.get("id") == provider_id), None)
    if info is None or not info.get("installed", True):
        raise ApiError("provider_not_installed", "提供方没有安装", 409, {"provider": provider_id})
    return info


# ---------------------------------------------------------------------------
# EV-02、EV-04、EV-05
# ---------------------------------------------------------------------------


def register(body: dict, *, user: str, admin: bool) -> dict:
    source_id = str(body.get("id") or "")
    if not SOURCE_ID.match(source_id):
        raise _invalid("id", "pattern", "ID 只能用小写字母、数字和连字符，2～41 位")
    provider_id = str(body.get("provider") or "").strip()
    if not provider_id:
        raise _invalid("provider", "required")
    info = _provider_info(provider_id)
    self_service = bool(info.get("self_service"))
    if not admin and not self_service:
        raise ApiError("forbidden", "这类数据源要管理员登记", 403, {"required_role": "admin"})
    secret = None
    if body.get("secret"):
        if not connection_type(info):
            raise _invalid("secret", "not_supported", "这类数据源不用凭据")
        secret = secret_fields(body["secret"], info)
    fields = _clean(body, info, creating=True)
    if connection_type(info):
        _check_connection_type(fields, str(info.get("kind") or ""))
    owner = user if self_service or not admin else str(body.get("owner") or "").strip() or user
    now = time.time()
    registration = {
        "id": source_id, "provider": provider_id, "provider_version": str(info.get("version") or ""),
        "provider_label": str(info.get("label") or provider_id), "kind": str(info.get("kind") or ""),
        "input": "artifact" if info.get("input") == "artifact" else "connection",
        **fields, "cred_ref": None, "owner": owner, "self_service": self_service,
        "registered_by_admin": admin and not self_service, "created_at": now, "updated_at": now,
        "secret_written_at": {},
    }
    with _lock:
        items = _load()
        if any(item["id"] == source_id for item in items):
            raise ApiError("source_id_taken", "数据源 ID 已被占用", 409)
        items.append(registration)
        _save(items)
    if secret:
        # 先登记、再写凭据；写不进去就撤掉登记，回网关的错误
        try:
            return write_secret(source_id, secret)
        except ApiError:
            with _lock:
                _save([item for item in _load() if item["id"] != source_id])
            raise
    return registration


def _input_of(registration: dict) -> str:
    if registration.get("input") in ("artifact", "connection"):
        return registration["input"]
    return "artifact" if "artifact_sha256" in (registration.get("connection") or {}) else "connection"


def provider_of(registration: dict) -> dict:
    """登记对应的提供方信息：网关通就取网关的（含 `cred_fields`），不通就用登记里记下的几项。"""
    from . import collector

    base = {"self_service": registration.get("self_service"), "kind": registration.get("kind"),
            "input": _input_of(registration)}
    info = collector.provider(registration["provider"]) if base["input"] == "connection" else None
    return {**info, **base} if info else base


def update(source_id: str, body: dict, *, user: str, admin: bool) -> dict:
    current = require_visible(source_id, user, admin)
    if not can_manage(current, user, admin):
        raise ApiError("forbidden", "只有登记人和管理员能改", 403, {"required_role": "admin"})
    info = provider_of(current)
    if connection_type(info) and body.get("connection") is not None and "cred_fields" not in info:
        raise ApiError("gateway_unavailable", "连接器网关不可达，改不了连接参数", 503)
    secret = None
    if body.get("secret"):
        if not connection_type(info) or not admin:
            raise _invalid("secret", "not_supported", "这类数据源不用凭据")
        if "cred_fields" not in info:
            raise ApiError("gateway_unavailable", "连接器网关不可达", 503)
        secret = secret_fields(body["secret"], info)
    changes = _clean({key: value for key, value in body.items() if key not in FIXED}, info, creating=False,
                     current=current)
    if admin and not current.get("self_service") and str(body.get("owner") or "").strip():
        changes["owner"] = str(body["owner"]).strip()
    if connection_type(info):
        _check_connection_type({**current, **changes}, str(current.get("kind") or ""))
    updated = _store(source_id, **changes)
    if secret:
        try:
            return write_secret(source_id, secret)
        except ApiError:
            _store(source_id, **{key: current.get(key) for key in changes})
            raise
    return updated


def delete(source_id: str, *, user: str, admin: bool) -> dict:
    current = require_visible(source_id, user, admin)
    if not can_manage(current, user, admin):
        raise ApiError("forbidden", "只有登记人和管理员能删", 403, {"required_role": "admin"})
    from . import jobs

    active = jobs.active_for_source(source_id)
    if active:
        raise ApiError("job_state_invalid", "这个数据源有正在跑的采集任务", 409, {"state": active[0]["state"]})
    if current.get("cred_ref"):
        delete_secret(source_id)  # 登记删了凭据不能留在网关里；网关不通就先不删登记
    with _lock:
        _save([item for item in _load() if item["id"] != source_id])
    grants.drop_source(source_id)
    return current


# ---------------------------------------------------------------------------
# 视图（EvidenceSource）
# ---------------------------------------------------------------------------


def ref(registration: dict) -> dict:
    return {"id": registration["id"], "display_name": registration.get("display_name") or registration["id"],
            "environment": registration.get("environment") or "test"}


def scope_summary(registration: dict) -> str:
    connection = registration.get("connection") or {}
    if "artifact_filename" in connection:
        return str(connection.get("artifact_filename") or "")
    scope = registration.get("scope_allowlist") or []
    return "、".join(scope[:3]) + (f" 等 {len(scope)} 项" if len(scope) > 3 else "") if scope else "全部"


def view(registration: dict, *, user: str, admin: bool, project_id: str | None = None,
         provider_info: dict | None = None, last_job: dict | None = None) -> dict:
    """EvidenceSource。普通用户看别人登记的数据源时，连接参数、出口、凭据引用都不给。"""
    from ..api import deps

    detailed = admin or registration.get("owner") == user
    info = provider_info or {}
    secret_fields = [
        {"name": field["name"], "set": field["name"] in (registration.get("secret_written_at") or {}),
         "written_at": deps.iso((registration.get("secret_written_at") or {}).get(field["name"]))}
        for field in info.get("cred_fields") or [] if isinstance(field, dict) and field.get("secret")
    ]
    authorized = None
    if project_id:
        authorized = registration["id"] in grants.authorized_ids(project_id)
    return {
        **ref(registration),
        "provider": {"id": registration["provider"], "version": str(info.get("version") or
                                                                      registration.get("provider_version") or ""),
                     "label": str(info.get("label") or registration.get("provider_label") or registration["provider"])},
        "connection": dict(registration.get("connection") or {}) if detailed else {},
        "cred_ref": registration.get("cred_ref") if detailed else None,
        "secret_fields": secret_fields,
        "scope_allowlist": list(registration.get("scope_allowlist") or []),
        "scope_summary": scope_summary(registration),
        "egress": list(registration.get("egress") or []) if detailed else [],
        "access_window": registration.get("access_window"),
        "budget": dict(registration.get("budget") or {}),
        "rate_limit": dict(registration.get("rate_limit") or {}),
        "masking_policy": registration.get("masking_policy") or "none",
        "authorization_note": registration.get("authorization_note"),
        "owner": registration.get("owner") or "",
        "self_service": bool(registration.get("self_service")),
        "last_job": {"at": deps.iso(last_job.get("ended_at") or last_job.get("created_at")),
                     "state": last_job["state"]} if last_job else None,
        "authorized_for_project": authorized,
    }


# ---------------------------------------------------------------------------
# EV-03、EV-30：申请与通知管理员
# ---------------------------------------------------------------------------


REQUEST_ID = re.compile(r"^esr-[0-9a-f]{12}$")
_requests_lock = threading.Lock()


def _request_path(request_id: str):
    return storage.root() / "requests" / f"{request_id}.json"


def save_request(kind: str, by: str, payload: dict) -> str:
    request_id = f"esr-{uuid.uuid4().hex[:12]}"
    storage.write_json(_request_path(request_id),
                       {"id": request_id, "kind": kind, "by": by, "at": time.time(), "state": "pending", **payload})
    return request_id


def get_request(request_id: str) -> dict | None:
    if not REQUEST_ID.match(str(request_id or "")):
        return None
    record = storage.read_json(_request_path(request_id))
    return record if isinstance(record, dict) else None


def all_requests(kind: str | None = None) -> list[dict]:
    """存下的申请（EV-03 登记申请 kind=register、EV-30 通知管理员 kind=notify_admin），按提交时间倒序。"""
    directory = storage.root() / "requests"
    out = []
    for path in sorted(directory.glob("esr-*.json")) if directory.is_dir() else []:
        record = storage.read_json(path)
        if isinstance(record, dict) and (kind is None or record.get("kind") == kind):
            out.append(record)
    return sorted(out, key=lambda item: item.get("at") or 0, reverse=True)


def decide_request(request_id: str, state: str, by: str, note: str | None, **extra) -> dict:
    """把待处理的申请记成通过或退回；已经处理过的回 409 request_state（AD-09、AD-10）。"""
    with _requests_lock:
        record = get_request(request_id)
        if record is None:
            raise ApiError("request_not_found", "申请不存在", 404)
        if record.get("state") != "pending":
            raise ApiError("request_state", "这条申请已经处理过了", 409, {"state": record.get("state")})
        record.update(state=state, reviewed_by=by, reviewed_at=time.time(), note=note or None, **extra)
        storage.write_json(_request_path(request_id), record)
        return record


# 管理员的「有新申请」通知点进插件安装页的「申请」页签（契约 8.2）
REVIEW_ACTION = {"label": "去处理", "page": "plugin-upload", "params": {"tab": "req"}}


def notify_admins(title: str, body: str, *, project_id: str | None = None, action: dict | None = None) -> int:
    """给管理员发 `request_pending` 通知，返回发出的条数。本地模式发给本地用户；取不到账号列表时只存不发。"""
    from ..services import auth, notifications

    if not auth.ENABLED:
        return 1 if notifications.notify("local", "request_pending", title, body, project_id=project_id,
                                         action=action) else 0
    try:
        from ..routers.admin import auth_admin_available, list_auth_users

        admins = [user["username"] for user in list_auth_users() if user.get("role") == "admin"] \
            if auth_admin_available() else []
    except Exception:  # noqa: BLE001 - 认证服务不通：申请已经存在 requests/ 下，只是不发通知
        admins = []
    sent = 0
    for name in admins:
        if notifications.notify(name, "request_pending", title, body, project_id=project_id, action=action):
            sent += 1
    return sent
