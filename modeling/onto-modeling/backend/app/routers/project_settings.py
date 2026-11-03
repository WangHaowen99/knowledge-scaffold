"""项目设置（API 契约 3.4、3.1）：C-06 可用引擎、P-12 引擎、P-13 构建方式、P-14 助手可用的 MCP 服务器。"""

from __future__ import annotations

from fastapi import APIRouter, Body, Request

from ..api import deps, views
from ..api.errors import ApiError

router = APIRouter(prefix="/api/v1", tags=["project-settings"])

MCP_DISABLED_MAX = 64


def _engines() -> list[dict]:
    from ..services.engines import ENGINE_NAMES, available_engines, get_engine_class

    available = available_engines()
    items = []
    for name in ENGINE_NAMES:
        installed = bool(available.get(name))
        items.append({
            "id": name,
            "channel": views.ENGINE_CHANNELS.get(name, "beta"),
            "label": views.engine_label(name),
            "installed": installed,
            "unavailable_reason": None if installed else getattr(get_engine_class(name), "UNAVAILABLE_MESSAGE", "引擎不可用"),
            # Claude 引擎能按续接点恢复上下文；Jiuwen 回放历史续上
            "resume": "native" if name == "claude" else "replay",
        })
    return items


@router.get("/engines")
def list_engines():
    """C-06：界面只显示 label，不显示引擎 id。"""
    from ..services import model_settings

    return {"items": _engines(), "default": model_settings.default_engine()}


def _model_source(request: Request, engine: str) -> str:
    """项目当前生效的模型配置来源：个人 Key、管理员配置或部署默认（契约 P-12）。"""
    from ..services import model_settings, personal_model_settings

    username = deps.username(request)
    if username and personal_model_settings.model_owner(username, engine):
        return "personal"
    if engine == "jiuwen":
        configured = model_settings.jiuwen_config()
    else:
        configured = model_settings.load()
    return "admin" if (configured or {}).get("api_key") or (configured or {}).get("base_url") else "deployment"


@router.get("/projects/{project_id}/engine")
def get_project_engine(project_id: str, request: Request):
    from ..services.engines import get_engine_class

    deps.require_owner(project_id, request)
    engine = get_engine_class(deps.workspaces.get_project(project_id).get("engine") or "claude").name
    return {"engine": engine, "model_source": _model_source(request, engine)}


@router.put("/projects/{project_id}/engine")
async def put_project_engine(project_id: str, request: Request, body: dict = Body(...)):
    """切换引擎即关闭本项目全部会话：历史保留可回放，下一条消息用新引擎启动。"""
    from ..services.agent_sessions import manager
    from ..services.engines import ENGINE_NAMES, get_engine_class

    from ..services.autobuild import store as autobuild_store

    deps.require_writable(project_id, request)
    autobuild_store.ensure_no_run(project_id)
    name = str(body.get("engine") or "").strip()
    if name not in ENGINE_NAMES or not get_engine_class(name).available():
        raise ApiError("engine_unavailable", "所选引擎在当前部署不可用", 409, {"engine": name})
    deps.workspaces._update_meta(project_id, engine=name)
    # 全部会话的续接点一并作废：换引擎期间产生的轮次不在旧引擎的 transcript 里
    await manager.close(project_id, all_conversations=True)
    return {"engine": name}


def _build_mode(project_id: str) -> dict:
    from ..services.autobuild import store as autobuild_store

    run = autobuild_store.active(project_id)
    return {
        "build_mode": deps.workspaces.get_project(project_id).get("build_mode") or "L2",
        "autobuild_running": run is not None,
        "run_id": run["run_id"] if run else None,
    }


@router.get("/projects/{project_id}/build-mode")
def get_build_mode(project_id: str, request: Request):
    deps.require_owner(project_id, request)
    return _build_mode(project_id)


@router.put("/projects/{project_id}/build-mode")
def put_build_mode(project_id: str, request: Request, body: dict = Body(...)):
    """只改默认构建方式，下一条消息生效；已在跑的运行不受影响。"""
    deps.require_writable(project_id, request)
    mode = body.get("build_mode")
    if mode not in ("L2", "L4"):
        raise ApiError("invalid_argument", "build_mode 取值不合法", 400, {"fields": {"build_mode": "enum"}})
    deps.workspaces._update_meta(project_id, build_mode=mode)
    return _build_mode(project_id)


def _mcp_servers(project_id: str) -> dict:
    from ..services import mcp_settings

    disabled = set(deps.workspaces.get_project(project_id).get("mcp_disabled") or [])
    items = []
    for server in mcp_settings.load():
        enabled_globally = bool(server.get("enabled", True))
        items.append({
            "name": server["name"],
            "transport": "stdio" if server.get("transport") == "stdio" else "http",
            "target": server.get("url") or server.get("command") or "",
            "origin": "global",
            "read_only": False,
            "status": "running" if enabled_globally else "stopped",
            "tools": [],
            "enabled": enabled_globally and server["name"] not in disabled,
            "enabled_globally": enabled_globally,
        })
    return {"items": items}


@router.get("/projects/{project_id}/mcp-servers")
def get_project_mcp_servers(project_id: str, request: Request):
    """P-14：全局 MCP 服务器在本项目是否可用；内核内置的只读服务器（evidence、flow）随证据源与流程运行时加入。"""
    deps.require_owner(project_id, request)
    return _mcp_servers(project_id)


@router.put("/projects/{project_id}/mcp-servers")
def put_project_mcp_servers(project_id: str, request: Request, body: dict = Body(...)):
    """本项目停用的服务器名，最多 64 个；只影响新建的会话。"""
    deps.require_writable(project_id, request)
    disabled = body.get("disabled")
    if not isinstance(disabled, list) or len(disabled) > MCP_DISABLED_MAX:
        raise ApiError("invalid_argument", f"disabled 必须是不超过 {MCP_DISABLED_MAX} 个名称的列表", 400,
                       {"fields": {"disabled": "list"}})
    names = [str(name)[:32] for name in disabled]
    deps.workspaces._update_meta(project_id, mcp_disabled=names)
    return _mcp_servers(project_id)
