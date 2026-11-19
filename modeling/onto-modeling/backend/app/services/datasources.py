"""数据源（ADR-0103；design/data-sources.md 第 4、9 节）：数据源资料、凭据、采集，以及多源连接器的信任与网关对账。

- 数据源是一份资料 `resource/excel/<名称>.datasource`，只有连接配置和凭据引用；采集结果是它的文本版，走资料转换的队列
  （`material_conversion` 里资料类型 `source`），「重新转换」就是重新采集。
- 凭据只进连接器网关：内核不存、不记日志，只在这一次请求里转交。
- 上传的多源连接器要管理员信任后，内核把那个版本打成 tar 交给网关；网关插件卷丢了的话，每次刷新网关清单时对账补上。
"""

from __future__ import annotations

import io
import logging
import tarfile
import threading
import time
import uuid
from pathlib import Path

from ..api.errors import ApiError
from ..evidence import sources as spec
from ..evidence.tickets import sha256_hex
from .material_conversion import gateway
from .workspace.layout import resource_text_path

_log = logging.getLogger(__name__)

GROUP = "excel"
TRUST_FIELD = "trusted"
TEST_TIMEOUT_S = 40
COLLECT_TIMEOUT_S = 660          # 网关采集最长 600 秒，加上排队与传输
SECRET_TIMEOUT_S = 15
PLUGIN_TIMEOUT_S = 60
NAME_MAX = 60


def _manager():
    from ..api import deps

    return deps.workspaces


def _unavailable() -> ApiError:
    return ApiError("gateway_unavailable", "连接器网关不可用", 503)


# ---------------------------------------------------------------------------
# DS-01 连接器
# ---------------------------------------------------------------------------


def connectors(force: bool = False) -> dict:
    status = gateway.health(force)["status"]
    data = gateway.catalog(force) if status == "ok" else None
    items = []
    for item in (data or {}).get("sources") or []:
        info = spec.info_from_dict(item)
        if info is not None:
            items.append(info.to_dict())
    return {"gateway": {"status": status if data is not None or status != "ok" else "down"}, "items": items}


def _catalog_if_up() -> dict | None:
    """列资料、列插件时附带的网关清单：先看缓存的健康状态，网关挂了不逐份去连（每份资料都等超时）。"""
    try:
        if gateway.health()["status"] != "ok":
            return None
        return gateway.catalog()
    except Exception:  # noqa: BLE001 - 网关清单只用来补字段与可用性
        return None


def connector_info(connector: str) -> spec.SourceInfo | None:
    """网关里实际加载的那一版连接器；网关不可用抛 503。"""
    data = gateway.catalog()
    if data is None:
        raise _unavailable()
    item = next((item for item in data.get("sources") or [] if item.get("id") == connector), None)
    return spec.info_from_dict(item) if item else None


def _require_connector(connector: str) -> spec.SourceInfo:
    if not isinstance(connector, str) or not connector:
        raise ApiError("invalid_argument", "要选一个连接器", 400, {"fields": {"connector": "required"}})
    info = connector_info(connector)
    if info is None:
        raise ApiError("connector_not_found", "连接器不可用", 404, {"connector": connector})
    return info


def _fields_error(errors: dict) -> ApiError:
    return ApiError("invalid_argument", "连接信息不完整或不正确", 400, {"fields": dict(sorted(errors.items()))})


# ---------------------------------------------------------------------------
# 数据源资料
# ---------------------------------------------------------------------------


def path_for(name: str) -> str:
    return f"resource/{GROUP}/{name}{spec.DATASOURCE_SUFFIX}"


def _check_name(name) -> str:
    name = str(name or "").strip()
    if not name or len(name) > NAME_MAX or name.startswith(".") or any(ch in name for ch in '/\\:*?"<>|') \
            or any(ord(ch) < 32 for ch in name):
        raise ApiError("invalid_argument", "名称不合法", 400, {"fields": {"name": "invalid"}})
    return name


def read_document(root: Path, rel: str) -> dict:
    if not spec.is_datasource(rel):
        raise ApiError("not_a_source", "这份资料不是数据源", 409, {"path": rel})
    path = Path(root) / rel
    if not path.is_file() or path.is_symlink():
        raise ApiError("file_not_found", "文件不存在", 404, {"path": rel})
    document = spec.load_document(path.read_bytes())
    if document is None:
        raise ApiError("not_a_source", "数据源资料格式不对", 409, {"path": rel})
    return document


def material_source(root: Path, rel: str) -> dict | None:
    """契约 Material.source：连接配置（不含凭据）与字段；不是数据源资料时 None。"""
    if not spec.is_datasource(rel):
        return None
    try:
        document = read_document(root, rel)
    except ApiError:
        return None
    data = _catalog_if_up()
    item = next((item for item in (data or {}).get("sources") or [] if item.get("id") == document["connector"]), None)
    info = spec.info_from_dict(item) if item else None
    return {"connector": {"id": document["connector"], "label": info.label if info else document["connector"],
                          "version": info.version if info else None, "available": info is not None},
            "config": document["config"], "fields": [field.to_dict() for field in info.fields] if info else [],
            "secret_fields": document["secret_fields"]}


def _new_ref() -> str:
    return f"ds-{uuid.uuid4().hex[:12]}"


def _put_secrets(project_id: str, ref: str, fields: dict) -> list[str]:
    import json

    body = json.dumps({"fields": fields}, ensure_ascii=False).encode("utf-8")
    status, payload = gateway.call("PUT", f"/v1/secrets/{project_id}/{ref}", body, operation="secret", scope=body,
                                   claims={"project": project_id}, timeout=SECRET_TIMEOUT_S)
    if status != 200:
        _log.error("网关拒绝写凭据：%s %s", status, payload.get("error"))
        raise ApiError("gateway_error", "连接器网关没有存下凭据", 502)
    return list(payload.get("secret_fields") or [])


def delete_secrets(project_id: str, rel: str, data: bytes) -> None:
    """删数据源资料时顺带删网关里的凭据（尽力而为）。"""
    document = spec.load_document(data) if spec.is_datasource(rel) else None
    if not document or not document["secret"]:
        return
    ref = document["secret"]
    scope = f"delete:{project_id}/{ref}".encode("utf-8")
    try:
        gateway.call("DELETE", f"/v1/secrets/{project_id}/{ref}", None, operation="secret", scope=scope,
                     claims={"project": project_id}, timeout=SECRET_TIMEOUT_S)
    except gateway.GatewayUnavailable:
        _log.warning("删数据源 %s 时网关不可用，凭据没删", rel)


def _write(manager, project_id: str, rel: str, data: bytes, author: str, *, created: bool) -> None:
    manager.save_resource(project_id, GROUP, Path(rel).name, data, "schema" if created else "keep",
                          author=author, datasource=True)


def create(project_id: str, *, name, connector, config, secrets, author: str) -> str:
    """DS-02：返回资料路径。凭据先写进网关，成功后才写资料；写完排队采集。"""
    manager = _manager()
    root = manager.project_root(project_id)
    rel = path_for(_check_name(name))
    if (root / rel).exists():
        raise ApiError("file_exists", "已有同名资料", 409, {"fields": {"name": "exists"}, "path": rel})
    info = _require_connector(connector)
    clean, given, errors = spec.coerce_config(info, config, secrets)
    if errors:
        raise _fields_error(errors)
    ref = _new_ref()
    stored = _put_secrets(project_id, ref, given) if given else []
    # 写入事件会排队采集（material_conversion.on_write），这里不再另排，免得同一份资料连采两遍
    _write(manager, project_id, rel, spec.dump_document(info.id, clean, ref, stored), author, created=True)
    return rel


def update(project_id: str, *, path, config=None, secrets=None, author: str) -> str:
    """DS-03：config 整份替换，secrets 给了的覆盖。改完重新采集。"""
    manager = _manager()
    root = manager.project_root(project_id)
    rel = str(path or "")
    document = read_document(root, rel)
    info = _require_connector(document["connector"])
    ref = document["secret"] or _new_ref()
    stored_fields = tuple(document["secret_fields"]) if document["secret"] else ()
    clean, given, errors = spec.coerce_config(info, document["config"] if config is None else config, secrets,
                                              stored_secrets=stored_fields)
    if errors:
        raise _fields_error(errors)
    stored = _put_secrets(project_id, ref, given) if given else list(stored_fields)
    data = spec.dump_document(info.id, clean, ref, stored)
    if (root / rel).read_bytes() == data:
        # 只换了凭据：资料内容没变、没有写入事件，强制重采一次
        _enqueue(project_id, rel, force=True)
    else:
        _write(manager, project_id, rel, data, author, created=False)
    return rel


def test(project_id: str, *, connector=None, config=None, secrets=None, path=None) -> dict:
    """DS-04：同步测试连接。带 path 时用这份数据源已存的凭据，secrets 里给了的临时覆盖。"""
    import json

    manager = _manager()
    root = manager.project_root(project_id)
    ref, stored = "", ()
    if path:
        document = read_document(root, str(path))
        connector = connector or document["connector"]
        if connector != document["connector"]:
            raise ApiError("invalid_argument", "连接器不能换", 400, {"fields": {"connector": "invalid"}})
        ref, stored = document["secret"], tuple(document["secret_fields"])
        if config is None:
            config = document["config"]
    info = _require_connector(connector)
    clean, given, errors = spec.coerce_config(info, config, secrets, stored_secrets=stored if ref else ())
    if errors:
        raise _fields_error(errors)
    body = {"project": project_id, "connector": info.id, "config": clean}
    if ref:
        body["secret"] = ref
    if given:
        body["secrets"] = given
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    try:
        status, payload = gateway.call("POST", "/v1/sources/test", data, operation="source_test", scope=data,
                                       claims={"project": project_id}, timeout=TEST_TIMEOUT_S)
    except gateway.GatewayUnavailable:
        raise _unavailable() from None
    if status == 404:
        raise ApiError("connector_not_found", "连接器不可用", 404, {"connector": info.id})
    if status != 200:
        return {"ok": False, "checks": [], "error": {"code": str(payload.get("error") or "gateway_error"),
                                                       "message": str(payload.get("message") or "")}}
    error = None
    if payload.get("error_code"):
        error = {"code": str(payload["error_code"]), "message": str(payload.get("error_message") or "")}
    return {"ok": bool(payload.get("ok")), "checks": list(payload.get("checks") or []), "error": error}


def _enqueue(project_id: str, rel: str, *, force: bool) -> None:
    from . import material_conversion

    material_conversion.enqueue(project_id, rel, force=force, requested="datasource")


# ---------------------------------------------------------------------------
# 采集（资料转换队列调）
# ---------------------------------------------------------------------------


def collect(project_id: str, data: bytes) -> dict:
    """POST /v1/sources/collect；返回网关的结果（state 为 converted / failed，另有 connector）。
    网关不可用、连接器还没加载抛 GatewayUnavailable（资料记「等待采集」，恢复后补）。"""
    import json

    document = spec.load_document(data)
    if document is None:
        return {"state": "failed", "error_code": "config_invalid"}
    loaded = gateway.catalog()
    if loaded is None:
        raise gateway.GatewayUnavailable("down")
    if not any(item.get("id") == document["connector"] for item in loaded.get("sources") or []):
        return {"state": "failed", "error_code": "connector_not_found",
                "connector": {"id": document["connector"], "version": ""}}
    body = json.dumps({"project": project_id, "connector": document["connector"], "config": document["config"],
                       "secret": document["secret"]}, ensure_ascii=False).encode("utf-8")
    status, payload = gateway.call("POST", "/v1/sources/collect", body, operation="source_collect", scope=body,
                                   claims={"project": project_id}, timeout=COLLECT_TIMEOUT_S)
    if status == 200:
        return payload
    code = str(payload.get("error") or "crashed")
    return {"state": "failed", "error_code": code, "error_message": str(payload.get("message") or ""),
            "connector": {"id": document["connector"], "version": ""}}


FAILURE_TEXT = {
    "connector_not_found": "连接器不可用", "secret_missing": "凭据没有存，要修改连接重新填写",
    "secret_undecryptable": "凭据解不开，要修改连接重新填写", "config_invalid": "连接信息不正确",
    "egress_forbidden": "连接地址不能是平台内部服务", "connect_failed": "连不上数据库", "auth_failed": "账号或密码不对",
    "account_not_readonly": "账号有写权限，换成只读账号", "timeout": "采集超时", "egress_denied": "连接器试图连别的地址",
    "exec_denied": "连接器试图启动别的程序", "memory": "采集超出内存上限", "output_too_large": "采集结果超过上限",
    "statement_blocked": "连接器发了不允许的语句", "invalid_event": "连接器输出不对", "crashed": "采集失败",
}


def failure_text(code: str, message: str = "") -> str:
    return FAILURE_TEXT.get(code) or (message[:120] if message else FAILURE_TEXT["crashed"])


# ---------------------------------------------------------------------------
# 多源连接器的信任（PL-28、PL-29）与网关对账
# ---------------------------------------------------------------------------


def pack_tar(directory: Path) -> bytes:
    """版本目录 → 确定性的 tar：只放普通文件，按路径排序，mtime=0、属主 0，不压缩。"""
    directory = Path(directory)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:", format=tarfile.PAX_FORMAT) as archive:
        for path in sorted(p for p in directory.rglob("*") if p.is_file() and not p.is_symlink()):
            rel = path.relative_to(directory).as_posix()
            if any(part.startswith(".") for part in rel.split("/")) or "__pycache__" in rel.split("/"):
                continue
            data = path.read_bytes()
            member = tarfile.TarInfo(rel)
            member.size = len(data)
            member.mtime = 0
            member.mode = 0o644
            member.uid = member.gid = 0
            member.uname = member.gname = ""
            archive.addfile(member, io.BytesIO(data))
    return buffer.getvalue()


def _is_connector(manifest) -> bool:
    return manifest.kind == "provider" and (isinstance(manifest.raw.get("converter"), dict)
                                            or isinstance(manifest.raw.get("source"), dict))


def trusted(repository, name: str) -> dict:
    entry = repository.channel_entry(name) or {}
    value = entry.get(TRUST_FIELD)
    return dict(value) if isinstance(value, dict) else {}


def trust_state(repository, name: str, version: str) -> dict | None:
    """契约 PackCard.trust；不是多源连接器时 None。"""
    manifest = repository.manifest(name, version)
    if not _is_connector(manifest):
        return None
    data = _catalog_if_up()
    clean = version.split("+", 1)[0]
    installed = None
    if data is not None:
        installed = any(item.get("name") == name and item.get("version") == clean for item in data.get("plugins") or [])
    if repository.is_builtin(name, version):
        return {"state": "builtin", "installed": installed}
    return {"state": "trusted" if version in trusted(repository, name) else "untrusted", "installed": installed}


def set_trust(manager, name: str, version: str, *, on: bool, by: str) -> None:
    repository = manager.packs.repository
    if not repository.has(name, version):
        raise ApiError("pack_not_found", "包不存在", 404, {"name": name, "version": version})
    manifest = repository.manifest(name, version)
    if not _is_connector(manifest) or repository.is_builtin(name, version):
        raise ApiError("invalid_argument", "只有上传的多源连接器需要信任", 400)
    records = trusted(repository, name)
    if on:
        records[version] = {"sha256": sha256_hex(pack_tar(repository.version_dir(name, version))), "by": by,
                            "at": int(time.time())}
    else:
        records.pop(version, None)
    repository.set_field(name, TRUST_FIELD, records)
    try:
        if on:
            _install(repository, name, version)
        else:
            _remove(name, version)
        gateway.catalog(force=True)
    except gateway.GatewayUnavailable:
        _log.warning("信任状态已记下，网关不可用，恢复后对账：%s@%s", name, version)


def _plugin_version(version: str) -> str:
    return version.split("+", 1)[0]


def _install(repository, name: str, version: str) -> None:
    data = pack_tar(repository.version_dir(name, version))
    expected = (trusted(repository, name).get(version) or {}).get("sha256")
    if expected and expected != sha256_hex(data):
        _log.error("包 %s@%s 的内容和信任时不一致，不交付", name, version)
        return
    plain = _plugin_version(version)
    status, payload = gateway.call("PUT", f"/v1/plugins/{name}/{plain}", data, operation="plugin_install", scope=data,
                                   claims={"plugin": f"{name}@{plain}"}, timeout=PLUGIN_TIMEOUT_S)
    if status != 200:
        _log.error("网关没装上 %s@%s：%s %s", name, plain, status, payload.get("error"))


def _remove(name: str, version: str) -> None:
    plain = _plugin_version(version)
    scope = f"{name}@{plain}".encode("utf-8")
    gateway.call("DELETE", f"/v1/plugins/{name}/{plain}", None, operation="plugin_remove", scope=scope, claims={},
                 timeout=PLUGIN_TIMEOUT_S)


_sync_lock = threading.Lock()


def reconcile(catalog: dict) -> None:
    """网关清单刷新后对账：已信任的版本网关里没有就交付，网关里装着但已不信任的就删除。后台线程里做。"""
    if not _sync_lock.acquire(blocking=False):
        return
    threading.Thread(target=_reconcile, args=(catalog,), daemon=True, name="connector-sync").start()


def _reconcile(catalog: dict) -> None:
    try:
        manager = _manager()
        repository = manager.packs.repository
        installed = {(item.get("name"), item.get("version"), item.get("sha256"))
                     for item in catalog.get("plugins") or [] if item.get("origin") == "installed"}
        wanted = set()
        # 私有包也要算：用户上传、管理员信任的连接器多半是私有包
        for name in repository.names(private=True):
            for version, record in trusted(repository, name).items():
                if not repository.has(name, version):
                    continue
                wanted.add((name, _plugin_version(version)))
                if (name, _plugin_version(version), record.get("sha256")) not in installed:
                    _install(repository, name, version)
        for name, version, _ in installed:
            if (name, version) not in wanted:
                _remove(name, version)
    except gateway.GatewayUnavailable:
        pass
    except Exception:  # noqa: BLE001 - 对账失败只记日志，下一轮再试
        _log.exception("多源连接器对账失败")
    finally:
        _sync_lock.release()


def install() -> None:
    gateway.on_catalog(reconcile)


def text_path(rel: str) -> str:
    return resource_text_path(rel)
