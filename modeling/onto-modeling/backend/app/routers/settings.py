"""系统设置（API 契约 3.24 SY-01～SY-13，admin）：模型接入、默认引擎、个人 Key 策略、上下文推荐表、
会话与权限、脚本验证沙箱、全局 MCP 服务器。

资产包上传（POST /api/settings/assets/upload）不在兼容表里：它的去向 PL-13 属于包仓库（B3），
过渡期照原路径、原行为保留。
"""

from __future__ import annotations

import re
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ..api import deps
from ..api.errors import ApiError
from ..api.views import ENGINE_CHANNELS, ENGINE_LABELS
from ..services import mcp_settings, model_context_presets, model_settings, runtime_settings

router = APIRouter(tags=["settings"])
BASE = "/api/v1/admin/settings"


def _admin(request: Request) -> None:
    deps.require_role(request, "admin")


def _invalid(message: str, fields: dict | None = None) -> ApiError:
    return ApiError("invalid_argument", message, 400, {"fields": fields} if fields else None)


def _package_version(name: str) -> str | None:
    try:
        from importlib.metadata import version as pkg_version

        return pkg_version(name)
    except Exception:
        return None


# ---- 上下文参数：页面字段 window / max_output，存储字段 max_context_tokens / max_output_tokens ----


def claude_compact_at_pct() -> int | None:
    """稳定引擎实际的自动压缩触发点占窗口的百分比（按 CLI 规则从生效参数推算）。"""
    effective = model_settings.effective_context()
    env = {
        model_settings._CLI_CONTEXT_ENV[name]: str(value)
        for name, value in effective.items()
        if value is not None
    }
    window, threshold = model_settings.cli_context_limits(env)
    return round(threshold * 100 / window) if window and threshold else None


def jiuwen_compact_at_pct() -> int:
    return round(model_settings.jiuwen_effective_context()["trigger_ratio"] * 100)


def compact_at_pct(engine: str) -> int | None:
    return claude_compact_at_pct() if engine == "claude" else jiuwen_compact_at_pct()


def effective_context(engine: str) -> dict:
    """实际生效的上下文参数（配置页 > .env > 内置默认），给个人模型配置页显示。"""
    if engine == "claude":
        values = model_settings.effective_context()
    else:
        values = model_settings.jiuwen_effective_context()
    return {
        "window": values["max_context_tokens"],
        "max_output": values["max_output_tokens"],
        "compact_at_pct": compact_at_pct(engine),
    }


def _context_defaults(engine: str, defaults: dict) -> dict:
    """留空时生效的值与来源；压缩触发点只能 .env 配，没配时按窗口与最大输出推算（source=derived）。"""
    env_compaction = engine == "claude" and any(
        (defaults.get(name) or {}).get("source") == "env" for name in ("auto_compact_window", "autocompact_pct")
    )
    return {
        "window": defaults["max_context_tokens"],
        "max_output": defaults["max_output_tokens"],
        "compact_at_pct": {"value": compact_at_pct(engine), "source": "env" if env_compaction else "derived"},
    }


def engine_view(engine: str, masked: dict | None = None) -> dict:
    """SY-01 里一个引擎的配置；Key 只给掩码。"""
    from ..services.engines import available_engines

    masked = masked or model_settings.masked()
    section = masked if engine == "claude" else masked["jiuwen"]
    saved = section.get("context") or {}
    return {
        "label": ENGINE_LABELS[engine],
        "channel": ENGINE_CHANNELS[engine],
        "installed": bool(available_engines().get(engine)),
        "runtime_version": _package_version("claude-agent-sdk" if engine == "claude" else "openjiuwen"),
        "base_url": section.get("base_url", ""),
        "api_key_masked": section.get("api_key_masked", ""),
        "api_key_set": bool(section.get("api_key_masked")),
        "model": section.get("model", ""),
        "configured": bool(section.get("configured")),
        "context": {
            "window": saved.get("max_context_tokens"),
            "max_output": saved.get("max_output_tokens"),
            "compact_at_pct": compact_at_pct(engine),
        },
        "context_defaults": _context_defaults(engine, section.get("context_defaults") or {}),
    }


def model_settings_view() -> dict:
    from ..services.engines import ENGINE_NAMES

    masked = model_settings.masked()
    return {
        "engines": {engine: engine_view(engine, masked) for engine in ENGINE_NAMES},
        "default_engine": masked["default_engine"],
        "personal_key_allowed": bool(masked.get("allow_personal")),
    }


def admin_summary(engine: str) -> dict:
    """管理员配置的摘要（个人模型配置页显示），只给网关主机名和模型，不含 Key。"""
    masked = model_settings.masked()
    section = masked if engine == "claude" else masked["jiuwen"]
    try:
        host = urlsplit(section.get("base_url") or "").hostname
    except ValueError:
        host = None
    return {"gateway_host": host or None, "model": section.get("model") or None, "configured": bool(section.get("configured"))}


# ---- SY-01～SY-04 模型接入 ----


@router.get(f"{BASE}/model")
def get_model_settings(request: Request):
    """SY-01：模型接入。"""
    _admin(request)
    return model_settings_view()


class ContextUpdate(BaseModel):
    window: int | None = None
    max_output: int | None = None
    # 压缩触发点由服务端按窗口与最大输出推算（或 .env），页面传来的值暂不保存，见 contract-changes
    compact_at_pct: int | None = Field(default=None, ge=50, le=95)


class ModelUpdateBody(BaseModel):
    base_url: str | None = None  # 不传表示不改，传空串表示清空
    api_key: str | None = None  # 留空表示不改
    model: str | None = None
    context: ContextUpdate | None = None  # 不传表示不改上下文参数；字段传 null 表示留空


@router.put(f"{BASE}/model/{{engine}}")
def put_engine_model(engine: str, body: ModelUpdateBody, request: Request):
    """SY-02：保存一个引擎的网关与上下文参数，先全部校验再写，不会只存一半。新会话生效。"""
    _admin(request)
    from ..services.engines import ENGINE_NAMES

    if engine not in ENGINE_NAMES:
        raise _invalid("未知引擎", {"engine": "enum"})
    masked = model_settings.masked()
    section = masked if engine == "claude" else masked["jiuwen"]
    base_url = section.get("base_url", "") if body.base_url is None else body.base_url
    model = section.get("model", "") if body.model is None else body.model
    context = None
    if body.context is not None:
        context = dict(section.get("context") or {})
        given = body.context.model_fields_set
        if "window" in given:
            context["max_context_tokens"] = body.context.window
        if "max_output" in given:
            context["max_output_tokens"] = body.context.max_output
    save = model_settings.save if engine == "claude" else model_settings.save_jiuwen
    try:
        save(base_url, body.api_key or "", model, context)
    except model_settings.ModelConfigError as exc:
        raise _invalid(str(exc)) from exc
    return engine_view(engine)


class DefaultEngineBody(BaseModel):
    engine: str


@router.put(f"{BASE}/default-engine")
def put_default_engine(body: DefaultEngineBody, request: Request):
    """SY-03：默认引擎，此后新建或导入的项目用它，已有项目不变。"""
    _admin(request)
    from ..services.engines import ENGINE_NAMES

    if body.engine not in ENGINE_NAMES:
        raise _invalid("未知引擎", {"engine": "enum"})
    try:
        saved = model_settings.save_default_engine(body.engine)
    except model_settings.ModelConfigError as exc:
        raise ApiError("engine_unavailable", str(exc), 409, {"engine": body.engine}) from exc
    return {"default_engine": saved["default_engine"]}


class PersonalPolicyBody(BaseModel):
    allow: bool


@router.put(f"{BASE}/personal-key-policy")
def put_personal_policy(body: PersonalPolicyBody, request: Request):
    """SY-04：是否允许个人 Key 覆盖管理员配置，从每个用户的下一条消息生效。"""
    _admin(request)
    saved = model_settings.save_personal_policy(body.allow)
    return {"allow": bool(saved.get("allow_personal"))}


# ---- SY-05～SY-08 上下文推荐表 ----

_ROW_IN_ERROR = re.compile(r"presets 第 (\d+) 条")
_LINE_IN_ERROR = re.compile(r"第 (\d+) 行第 (\d+) 列")


def _preset_error(error: str | None) -> dict | None:
    if not error:
        return None
    row = _ROW_IN_ERROR.search(error)
    if row:
        return {"row": int(row.group(1)), "column": None, "message": error}
    line = _LINE_IN_ERROR.search(error)
    if line:
        return {"row": int(line.group(1)), "column": int(line.group(2)), "message": error}
    return {"row": None, "column": None, "message": error}


def _preset_origin(preset: dict) -> str:
    if preset.get("origin") == "custom":
        return "override" if preset.get("overrides_builtin") else "custom"
    return "builtin"


def _preset_row(preset: dict) -> dict:
    origin = _preset_origin(preset)
    builtin = None
    if origin == "override":
        values = preset.get("builtin_values") or preset
        builtin = {"window": values["max_context_tokens"], "max_output": values["max_output_tokens"]}
    return {
        "model": preset["id"],
        "vendor": preset.get("vendor", ""),
        "window": preset["max_context_tokens"],
        "max_output": preset["max_output_tokens"],
        "note": preset.get("note", ""),
        "aliases": preset.get("aliases", []),
        "sources": preset.get("sources", []),
        "origin": origin,
        "builtin": builtin,
    }


def presets_view() -> dict:
    """SY-05 形状：合并后的推荐表与自定义表状态（不回传服务端文件路径）。"""
    data = model_context_presets.presets()
    custom = data["custom"]
    rows = [_preset_row(preset) for preset in data["presets"]]
    status = "invalid" if custom.get("error") else "ok" if custom.get("exists") else "disabled"
    return {
        "rows": rows,
        "builtin_verified_at": data.get("verified_at") or None,
        "custom": {
            "version": custom.get("version") or "",
            "status": status,
            "error": _preset_error(custom.get("error")),
            "overrides": sum(1 for row in rows if row["origin"] == "override"),
            "added": sum(1 for row in rows if row["origin"] == "custom"),
            "verified_at": custom.get("verified_at") or None,
            "loaded_at": deps.iso(custom.get("loaded_at")),
            "backup_available": bool(custom.get("backup_path")),
        },
    }


@router.get(f"{BASE}/context-presets")
def get_context_presets(request: Request):
    """SY-05：上下文推荐表（只做推荐，不参与运行时取值）。"""
    _admin(request)
    return presets_view()


class PresetRow(BaseModel):
    model: str
    vendor: str = ""
    window: int
    max_output: int
    note: str = ""
    aliases: list[str] | None = None  # 不传时沿用已存的同名条目
    sources: list[str] | None = None


class CustomPresetsBody(BaseModel):
    version: str  # 读取推荐表时的 custom.version；文件已被改过时拒绝保存
    presets: list[PresetRow]  # 整份自定义条目
    verified_at: str | None = None  # 不传表示不改


def _custom_entries(rows: list[PresetRow]) -> list[dict]:
    """页面行 → 自定义表条目；别名与来源链接页面不编辑时沿用已存的同名条目。"""
    normalize = model_context_presets.normalize_model_name
    existing = {normalize(entry["id"]): entry for entry in model_context_presets.presets()["custom"]["entries"]}
    entries = []
    for row in rows:
        previous = existing.get(normalize(row.model), {})
        entries.append({
            "id": row.model,
            "vendor": row.vendor,
            "max_context_tokens": row.window,
            "max_output_tokens": row.max_output,
            "note": row.note,
            "aliases": row.aliases if row.aliases is not None else previous.get("aliases", []),
            "sources": row.sources if row.sources is not None else previous.get("sources", []),
        })
    return entries


@router.put(f"{BASE}/context-presets/custom")
def put_custom_context_presets(body: CustomPresetsBody, request: Request):
    """SY-06：整份替换自定义推荐表；校验与手改文件相同，上一版存为 .prev，已保存的上下文参数不受影响。"""
    _admin(request)
    try:
        model_context_presets.save_custom(
            _custom_entries(body.presets), body.version, body.verified_at, deps.actor(request)
        )
    except model_context_presets.PresetConflictError as exc:
        current = model_context_presets.presets()["custom"].get("version") or ""
        raise ApiError(
            "revision_conflict", str(exc), 409,
            {"target": "context-presets", "expected": body.version, "actual": current},
        ) from exc
    except model_context_presets.PresetFileError as exc:
        raise ApiError("invalid_argument", str(exc), 400, {"error": _preset_error(str(exc))}) from exc
    except OSError as exc:
        raise ApiError("internal_error", f"保存失败：{exc.strerror or exc}", 500) from exc
    return presets_view()


@router.post(f"{BASE}/context-presets/custom/disable")
def disable_custom_context_presets(request: Request):
    """SY-07：停用自定义推荐表（改名备份），推荐表恢复为内置表。"""
    _admin(request)
    try:
        model_context_presets.disable_custom()
    except OSError as exc:
        raise ApiError("internal_error", f"停用失败：{exc.strerror or exc}", 500) from exc
    return presets_view()


@router.get(f"{BASE}/context-presets/match")
def match_context_preset(request: Request, model: str = ""):
    """SY-08：按模型名查推荐，查不到时 preset 为 null。"""
    _admin(request)
    preset = model_context_presets.match(model)
    if preset is None:
        return {"model": model, "preset": None}
    return {
        "model": model,
        "preset": {
            "window": preset["max_context_tokens"],
            "max_output": preset["max_output_tokens"],
            "origin": _preset_origin(preset),
        },
    }


# ---- SY-09 会话与权限 ----


def session_settings_view() -> dict:
    from .. import config

    return {
        "permission_mode": runtime_settings.permission_mode(),
        "allowed": list(runtime_settings.ALLOWED_PERMISSION_MODES),
        "pool": {
            "max_sessions": config.MAX_ACTIVE_SESSIONS,
            "idle_ttl_s": config.SESSION_IDLE_TTL_SECONDS,
            "permission_timeout_s": config.PERMISSION_TIMEOUT_SECONDS,
            "source": "env",
        },
    }


@router.get(f"{BASE}/session")
def get_session_settings(request: Request):
    """SY-09：会话与权限；会话池参数只读，来自环境变量。"""
    _admin(request)
    return session_settings_view()


class SessionSettingsBody(BaseModel):
    permission_mode: str


@router.put(f"{BASE}/session")
def put_session_settings(body: SessionSettingsBody, request: Request):
    _admin(request)
    try:
        runtime_settings.save(body.permission_mode)
    except ValueError as exc:
        raise _invalid(str(exc), {"permission_mode": "enum"}) from exc
    return session_settings_view()


# ---- SY-10、SY-11 脚本验证沙箱 ----


def _memory_mb(value) -> int | None:
    text = str(value or "").strip().upper()
    match = re.fullmatch(r"(\d+)([KMG]?)", text)
    if not match:
        return None
    number, unit = int(match.group(1)), match.group(2)
    return {"K": number // 1024, "M": number, "G": number * 1024, "": number // (1024 * 1024)}[unit]


def sandbox_isolation(client) -> dict:
    """沙箱隔离参数，取自实际下发给 jiuwenbox 的策略。"""
    policy = client.sandbox_policy()
    process = policy.get("process") or {}
    files = policy.get("filesystem_policy") or {}
    cgroup = policy.get("cgroup") or {}
    egress = ((policy.get("network") or {}).get("egress") or {})
    return {
        "non_root": process.get("run_as_user") not in (None, "", "root"),
        "readonly_root": set(files.get("read_write") or []) <= {"/tmp"},
        "memory_mb": _memory_mb(cgroup.get("memory_max")),
        "max_procs": cgroup.get("pids_max"),
        "network": "denied" if egress.get("default") == "deny" else "allowed",
    }


def sandbox_view() -> dict:
    from ..services import sandbox_status

    status = sandbox_status.status()
    return {
        "connected": bool(status.get("connected")),
        "gateway": status.get("gateway") or "",
        "detail": status.get("detail"),
        "isolation": sandbox_isolation(sandbox_status.client),
        "python": status.get("python"),
        "checked_at": deps.iso(status.get("checked_at")),
        "packages": [
            {"name": item.get("name"), "version": item.get("version"), "purpose": None}
            for item in status.get("packages") or []
        ],
    }


@router.get(f"{BASE}/sandbox")
def get_sandbox_status(request: Request):
    """SY-10：经脚本验证网关查 jiuwenbox；连不上时脚本运行验证在 onto-modeling 容器内执行。"""
    _admin(request)
    return sandbox_view()


@router.post(f"{BASE}/sandbox/probe")
def probe_sandbox(request: Request):
    """SY-11：重新检测（状态接口本身每次都实时探测，不缓存）。"""
    _admin(request)
    return sandbox_view()


# ---- SY-12、SY-13 全局 MCP 服务器 ----


def mcp_server_view(entry: dict) -> dict:
    """SY-12 的一项。连接状态与工具清单不落盘，测试连接（SY-13）时才拿到。"""
    return {
        "name": entry["name"],
        "transport": entry["transport"],
        "url": entry.get("url"),
        "command": entry.get("command"),
        "args": entry.get("args") or [],
        "enabled": entry.get("enabled", True),
        "auth": {"kind": "none"},
        "status": "unknown",
        "status_detail": None,
        "tools": [],
        "last_test_at": None,
        "latency_ms": None,
    }


def _reject_auth(server: dict) -> None:
    kind = ((server.get("auth") or {}) if isinstance(server.get("auth"), dict) else {}).get("kind") or "none"
    if kind != "none":
        raise _invalid("暂不支持为 MCP 服务器配置鉴权密钥", {"auth.kind": "unsupported"})


@router.get(f"{BASE}/mcp-servers")
def get_mcp_servers(request: Request):
    """SY-12：全局 MCP 服务器。"""
    _admin(request)
    return {"items": [mcp_server_view(entry) for entry in mcp_settings.load()]}


class McpServersBody(BaseModel):
    items: list[dict]


@router.put(f"{BASE}/mcp-servers")
def put_mcp_servers(body: McpServersBody, request: Request):
    """整份替换；只影响新建的会话。stdio 配置可指定任意命令，所以只有管理员可改。"""
    _admin(request)
    for item in body.items:
        _reject_auth(item)
    try:
        saved = mcp_settings.save(body.items)
    except mcp_settings.McpConfigError as exc:
        raise _invalid(str(exc)) from exc
    return {"items": [mcp_server_view(entry) for entry in saved]}


class McpTestBody(BaseModel):
    server: dict


@router.post(f"{BASE}/mcp-servers/test")
def test_mcp_server(body: McpTestBody, request: Request):
    """SY-13：连通性测试，不做完整 MCP 握手，拿不到工具清单（tools 为空）。"""
    _admin(request)
    _reject_auth(body.server)
    started = time.monotonic()
    try:
        result = mcp_settings.test_server(body.server)
    except mcp_settings.McpConfigError as exc:
        raise _invalid(str(exc)) from exc
    ok = bool(result.get("ok"))
    return {
        "ok": ok,
        "latency_ms": int((time.monotonic() - started) * 1000),
        "tools": [],
        "detail": result.get("detail"),
        "error": None if ok else result.get("detail"),
    }
