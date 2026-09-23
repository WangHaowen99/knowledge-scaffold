"""评测数据源配置库：按项目保存可复用的数据源配置，评测时直接选用。

数据源形态对齐 modeling 的项目数据源（evaluation/data-source.ts）并保留
onto-modeling 自有的数据集通道：

- ``none``：不使用数据源，纯知识包问答；
- ``dataset``：eval_data 资料表 / mock 种子 → modeling quick-import 建评测库；
- ``connection``：外部数据源，provider 为 ``nl2sql_template`` / ``nl2sql_http`` /
  ``neo4j``，字段与校验规则同 modeling（Scene、评测工作空间 ID、隔离条件）。
  Neo4j 可附带要导入的数据（resources / include_mock），每轮评测前一键导入图库。

每轮评测都会在 modeling 新建评测项目，连接凭据必须由 onto-modeling 保存才能
复用：配置落数据目录（0600，不进工作区 git、不随导出/克隆外带），读取接口一律
只回 has_token / has_password 标记。
"""

from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path

from .. import config

MODES = ("none", "dataset", "connection")
PROVIDERS = ("nl2sql_template", "nl2sql_http", "neo4j")
PROVIDER_LABELS = {
    "nl2sql_template": "NL2SQL Template",
    "nl2sql_http": "NL2SQL HTTP",
    "neo4j": "Neo4j",
}
MAX_SOURCES = 30
MAX_NAME_LENGTH = 60

_WORKSPACE_ID = re.compile(r"^[a-z][a-z0-9_-]*$")
_WORKSPACE_ID_MAX_LENGTH = 64
_NL2SQL_SCENE = re.compile(r"^[a-zA-Z0-9_]{1,60}$")
_GRAPH_IDENTIFIER = re.compile(r"^[A-Za-z_一-鿿][A-Za-z0-9_一-鿿]*$")
_PROJECT_ID = re.compile(r"^[A-Za-z0-9_-]+$")


class DataSourceError(ValueError):
    """配置输入不合法（接口层映射为 400）。"""


def _text(value) -> str:
    return "" if value is None else str(value).strip()


# ---- 校验（与 modeling normalizeDataSourceInput 同一口径）----


def _workspace_id(value, label: str = "评测工作空间 ID") -> str:
    workspace_id = _text(value)
    if not workspace_id:
        raise DataSourceError(f"{label}不能为空")
    if not _WORKSPACE_ID.match(workspace_id) or len(workspace_id) > _WORKSPACE_ID_MAX_LENGTH:
        raise DataSourceError(
            f"{label}格式非法：必须以英文小写字母开头，只能包含小写字母、数字、"
            "下划线或中划线，且最长 64 个字符"
        )
    return workspace_id


def _scene(value) -> str:
    scene = _text(value)
    if not scene:
        raise DataSourceError("Scene 不能为空")
    if not _NL2SQL_SCENE.match(scene):
        raise DataSourceError("Scene 格式非法：只能包含英文字母、数字或下划线，且最长 60 个字符")
    return scene


def _graph_identifier(value: str, label: str) -> str:
    if not _GRAPH_IDENTIFIER.match(value):
        raise DataSourceError(f"{label}格式非法：{value}")
    return value


def _http_url(value, label: str, schemes: tuple[str, ...]) -> str:
    url = _text(value).rstrip("/")
    if not url:
        raise DataSourceError(f"{label}不能为空")
    if not url.startswith(schemes):
        raise DataSourceError(f"{label}须以 {' / '.join(schemes)} 开头")
    return url


def _normalize_connection(provider: str, raw: dict, previous: dict | None) -> dict:
    """provider 字段规整；密钥留空 = 沿用已保存的值（同 provider 才沿用）。"""
    previous = previous or {}
    kept = (previous.get("config") or {}) if previous.get("provider") == provider else {}
    if provider == "neo4j":
        password = _text(raw.get("password")) or _text(kept.get("password"))
        if not password:
            raise DataSourceError("Neo4j Password 不能为空")
        out = {
            "uri": _text(raw.get("uri")),
            "username": _text(raw.get("username")),
            "password": password,
            "database": _text(raw.get("database")) or "neo4j",
            # Workspace 对应值同时作为 consumer 评测工作空间 ID（modeling 同一约束）
            "workspace_property": _graph_identifier(
                _text(raw.get("workspace_property")) or "d2o_workspace_id", "Workspace 隔离字段"
            ),
            "workspace_value": _workspace_id(raw.get("workspace_value"), "Workspace 对应值"),
        }
        if not out["uri"]:
            raise DataSourceError("Neo4j URL 不能为空")
        if not out["username"]:
            raise DataSourceError("Neo4j User 不能为空")
        # Ontology 隔离值必须等于知识包 ID（modeling 强校验），发起评测时自动填写，
        # 这里只保存隔离字段名；留空表示不按知识包隔离
        ontology_property = _text(raw.get("ontology_property"))
        if ontology_property:
            out["ontology_property"] = _graph_identifier(ontology_property, "Ontology 隔离字段")
        return out
    out = {
        "url": _http_url(raw.get("url"), "数据服务 URL", ("http://", "https://")),
        "scene": _scene(raw.get("scene")),
        "evaluation_workspace_id": _workspace_id(raw.get("evaluation_workspace_id")),
    }
    token = _text(raw.get("token")) or _text(kept.get("token"))
    if token:
        out["token"] = token
    return out


def _import_selection(body: dict) -> dict:
    resources = body.get("resources") or []
    if not isinstance(resources, list):
        raise DataSourceError("resources 必须是数组")
    return {
        "resources": [_text(r) for r in resources if _text(r)][:20],
        "include_mock": bool(body.get("include_mock")),
    }


def normalize(body: dict, previous: dict | None = None) -> dict:
    """前端提交的配置 → 存储结构（不含 id / 时间戳）。"""
    body = body or {}
    mode = _text(body.get("mode")) or "none"
    if mode not in MODES:
        raise DataSourceError("数据源模式仅支持 none、dataset 或 connection")
    source: dict = {"mode": mode}
    if mode == "dataset":
        source.update(_import_selection(body))
        if not source["resources"] and not source["include_mock"]:
            raise DataSourceError("数据集模式需要至少一份评测数据表或示例数据种子")
    elif mode == "connection":
        provider = _text(body.get("provider")).lower()
        if provider not in PROVIDERS:
            raise DataSourceError("provider 仅支持 nl2sql_template、nl2sql_http 或 neo4j")
        source["provider"] = provider
        source["config"] = _normalize_connection(provider, body.get("config") or {}, previous)
        selection = _import_selection(body) if provider == "neo4j" else {}
        if selection.get("resources") or selection.get("include_mock"):
            # modeling 图导入要求 Workspace 与 Ontology 两组隔离条件齐全
            if not source["config"].get("ontology_property"):
                raise DataSourceError("导入图数据需要填写 Ontology 隔离字段（按知识包隔离）")
            source.update(selection)
    name = _text(body.get("name"))[:MAX_NAME_LENGTH]
    source["name"] = name or default_name(source)
    return source


def default_name(source: dict) -> str:
    mode = source.get("mode")
    if mode == "dataset":
        count = len(source.get("resources") or [])
        if source.get("include_mock") and not count:
            return "示例数据种子"
        label = f"业务数据表（{count} 份）"
        return label + " + 示例数据" if source.get("include_mock") else label
    if mode == "connection":
        cfg = source.get("config") or {}
        provider = source.get("provider", "")
        target = cfg.get("workspace_value") if provider == "neo4j" else cfg.get("scene")
        name = f"{PROVIDER_LABELS.get(provider, provider)} · {target}"
        if source.get("include_mock"):
            return name + "（导入示例数据）"
        if source.get("resources"):
            return name + f"（导入 {len(source['resources'])} 份数据）"
        return name
    return "不使用数据源"


def public_view(source: dict) -> dict:
    """对外视图：去掉密钥，只留是否已保存的标记。"""
    view = {k: v for k, v in source.items() if k != "config"}
    if source.get("mode") == "connection":
        cfg = dict(source.get("config") or {})
        has_password = bool(cfg.pop("password", ""))
        has_token = bool(cfg.pop("token", ""))
        if source.get("provider") == "neo4j":
            cfg["has_password"] = has_password
        else:
            cfg["has_token"] = has_token
        view["config"] = cfg
    return view


# ---- 发起评测：选用的配置 → run 载荷 ----


def modeling_payload(source: dict, ontology_id: str) -> dict:
    """connection 配置 → modeling ``PUT /data-source`` 请求体（含密钥）。"""
    cfg = dict(source.get("config") or {})
    if source.get("provider") == "neo4j" and cfg.get("ontology_property"):
        cfg["ontology_value"] = ontology_id
    return {"mode": "connection", "provider": source["provider"], "config": cfg}


# ---- 存储 ----


def _file(project_id: str) -> Path:
    if not _PROJECT_ID.match(project_id or ""):
        raise DataSourceError("项目 ID 不合法")
    return Path(config.DATA_DIR) / "eval-data-sources" / f"{project_id}.json"


def _load(project_id: str) -> list[dict]:
    path = _file(project_id)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [s for s in data.get("sources") or [] if isinstance(s, dict) and s.get("id")]


def _save(project_id: str, sources: list[dict]) -> None:
    path = _file(project_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"sources": sources}, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)


def list_sources(project_id: str) -> list[dict]:
    """最近使用/更新的排在前面。"""
    sources = _load(project_id)
    sources.sort(key=lambda s: max(s.get("used_at", 0), s.get("updated_at", 0)), reverse=True)
    return sources


def get_source(project_id: str, source_id: str) -> dict | None:
    return next((s for s in _load(project_id) if s["id"] == source_id), None)


def create_source(project_id: str, body: dict) -> dict:
    sources = _load(project_id)
    if len(sources) >= MAX_SOURCES:
        raise DataSourceError(f"每个项目最多保存 {MAX_SOURCES} 个数据源配置，请先删除不用的")
    now = int(time.time())
    source = {"id": uuid.uuid4().hex[:12], **normalize(body), "created_at": now, "updated_at": now}
    sources.append(source)
    _save(project_id, sources)
    return source


def update_source(project_id: str, source_id: str, body: dict) -> dict:
    sources = _load(project_id)
    for index, current in enumerate(sources):
        if current["id"] != source_id:
            continue
        updated = {
            "id": source_id,
            **normalize(body, previous=current),
            "created_at": current.get("created_at", int(time.time())),
            "updated_at": int(time.time()),
        }
        if current.get("used_at"):
            updated["used_at"] = current["used_at"]
        sources[index] = updated
        _save(project_id, sources)
        return updated
    raise KeyError(source_id)


def delete_source(project_id: str, source_id: str) -> None:
    sources = _load(project_id)
    remaining = [s for s in sources if s["id"] != source_id]
    if len(remaining) == len(sources):
        raise KeyError(source_id)
    _save(project_id, remaining)


def mark_used(project_id: str, source_id: str) -> None:
    sources = _load(project_id)
    for source in sources:
        if source["id"] == source_id:
            source["used_at"] = int(time.time())
            _save(project_id, sources)
            return
