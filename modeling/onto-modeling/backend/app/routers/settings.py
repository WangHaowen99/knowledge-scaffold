from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..services import (
    auth,
    mcp_settings,
    model_context_presets,
    model_settings,
    personal_model_settings,
)
from ..services.auth import AuthError

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _require_admin(request: Request) -> None:
    if not auth.ENABLED:
        return
    user = getattr(request.state, "user", None)
    if not user or user.get("role") != "admin":
        raise AuthError("需要管理员权限", 403)


class ModelBody(BaseModel):
    base_url: str = ""
    api_key: str = ""  # 留空表示不修改
    model: str = ""


class ContextBody(BaseModel):
    max_context_tokens: int | None = None
    max_output_tokens: int | None = None


class ClaudeModelBody(ModelBody):
    context: ContextBody | None = None  # 不传表示不修改上下文参数


class JiuwenModelBody(ModelBody):
    context: ContextBody | None = None  # 不传表示不修改上下文参数


@router.get("/model")
def get_model_settings(request: Request):
    _require_admin(request)
    return model_settings.masked()


@router.put("/model")
def put_model_settings(body: ClaudeModelBody, request: Request):
    """稳定引擎的网关配置；配置页带上 context，与上下文参数一起校验、一起保存。"""
    _require_admin(request)
    context = body.context.model_dump() if body.context is not None else None
    try:
        return model_settings.save(body.base_url, body.api_key, body.model, context)
    except model_settings.ModelConfigError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})


@router.put("/model/jiuwen")
def put_jiuwen_model_settings(body: JiuwenModelBody, request: Request):
    """Jiuwen 引擎的 OpenAI 兼容模型配置（网关地址 / API Key / 模型名），可带上下文参数一起保存。"""
    _require_admin(request)
    context = body.context.model_dump() if body.context is not None else None
    try:
        return model_settings.save_jiuwen(body.base_url, body.api_key, body.model, context)
    except model_settings.ModelConfigError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})


@router.put("/model/context")
def put_model_context_settings(body: ContextBody, request: Request):
    """稳定引擎的上下文窗口与最大输出；压缩参数由服务端按 .env 或自动推算，一并注入 CLI 会话环境。"""
    _require_admin(request)
    try:
        return model_settings.save_context(body.model_dump())
    except model_settings.ModelConfigError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})


@router.get("/model/context-presets")
def get_model_context_presets(request: Request):
    """模型上下文推荐表全文（内置表 + 数据目录下的自定义表，只做推荐，不参与运行时取值）。"""
    _require_admin(request)
    return model_context_presets.presets()


class CustomPresetsBody(BaseModel):
    version: str  # 读取推荐表时的 custom.version；文件已被改过时拒绝保存
    presets: list[dict]  # 整份自定义条目
    verified_at: str | None = None  # 自定义表的核对日期；不传表示不改


@router.put("/model/context-presets/custom")
def put_custom_model_context_presets(body: CustomPresetsBody, request: Request):
    """页面保存自定义推荐表：校验与手改文件相同，原子写入，上一版存为 .prev；已保存的上下文参数不受影响。"""
    _require_admin(request)
    user = (getattr(request.state, "user", None) or {}).get("sub", "")
    try:
        return model_context_presets.save_custom(body.presets, body.version, body.verified_at, user)
    except model_context_presets.PresetConflictError as exc:
        return JSONResponse(status_code=409, content={"error": str(exc)})
    except model_context_presets.PresetFileError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    except OSError as exc:
        return JSONResponse(status_code=500, content={"error": f"保存失败：{exc.strerror or exc}"})


@router.post("/model/context-presets/custom/disable")
def disable_custom_model_context_presets(request: Request):
    """停用数据目录下的自定义推荐表：改名备份，推荐表恢复为内置表；已保存的上下文参数不受影响。"""
    _require_admin(request)
    try:
        return model_context_presets.disable_custom()
    except OSError as exc:
        return JSONResponse(status_code=500, content={"error": f"停用失败：{exc.strerror or exc}"})


@router.get("/model/context-presets/match")
def match_model_context_preset(request: Request, model: str = ""):
    """按模型名查推荐表；查不到时 preset 为 null。"""
    _require_admin(request)
    return {"model": model, "preset": model_context_presets.match(model)}


class DefaultEngineBody(BaseModel):
    engine: str


@router.put("/model/default-engine")
def put_default_engine(body: DefaultEngineBody, request: Request):
    """默认引擎：此后新建或导入的项目都用它，已有项目不受影响。"""
    _require_admin(request)
    try:
        return model_settings.save_default_engine(body.engine)
    except model_settings.ModelConfigError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})


class PersonalPolicyBody(BaseModel):
    allow: bool


@router.put("/model/personal-policy")
def put_personal_policy(body: PersonalPolicyBody, request: Request):
    """是否允许用户用个人 Key 覆盖管理员配置；关闭后所有会话回到管理员配置。"""
    _require_admin(request)
    return model_settings.save_personal_policy(body.allow)


# ---- 个人模型配置：登录用户自助，只读写自己的那份 ----


def _username(request: Request) -> str:
    user = getattr(request.state, "user", None) or {}
    return user.get("sub", "")


def _personal_error(exc: personal_model_settings.PersonalConfigError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"error": str(exc)})


@router.get("/model/me")
def get_my_model_settings(request: Request):
    return personal_model_settings.masked(_username(request))


@router.put("/model/me/{engine}")
def put_my_model_settings(engine: str, body: ModelBody, request: Request):
    try:
        return personal_model_settings.save(
            _username(request), engine, body.base_url, body.api_key, body.model
        )
    except personal_model_settings.PersonalConfigError as exc:
        return _personal_error(exc)


@router.delete("/model/me/{engine}")
def delete_my_model_settings(engine: str, request: Request):
    try:
        return personal_model_settings.clear(_username(request), engine)
    except personal_model_settings.PersonalConfigError as exc:
        return _personal_error(exc)


class McpBody(BaseModel):
    servers: list[dict]


class RuntimeBody(BaseModel):
    permission_mode: str


@router.get("/runtime")
def get_runtime(request: Request):
    _require_admin(request)
    from ..services import runtime_settings

    return {
        "permission_mode": runtime_settings.permission_mode(),
        "allowed": list(runtime_settings.ALLOWED_PERMISSION_MODES),
    }


@router.put("/runtime")
def put_runtime(body: RuntimeBody, request: Request):
    _require_admin(request)
    from ..services import runtime_settings

    try:
        runtime_settings.save(body.permission_mode)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    return {"permission_mode": runtime_settings.permission_mode()}


@router.get("/sandbox")
def get_sandbox_status(request: Request):
    """是否连上 onto-agent-jiuwenbox；连不上时脚本运行验证在 onto-modeling 容器内执行。"""
    _require_admin(request)
    from ..services import sandbox_status

    return sandbox_status.status()


@router.get("/assets")
def get_assets(request: Request):
    _require_admin(request)
    from ..services import assets_admin

    return assets_admin.info()


@router.post("/assets/upload")
async def upload_assets(request: Request, file: UploadFile = File(...)):
    _require_admin(request)
    import tempfile
    from pathlib import Path

    from ..services import assets_admin

    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)
    try:
        return assets_admin.upload(tmp_path)
    except assets_admin.AssetError as exc:
        return JSONResponse(status_code=exc.status, content={"error": str(exc)})
    finally:
        tmp_path.unlink(missing_ok=True)


@router.get("/mcp")
def get_mcp(request: Request):
    _require_admin(request)
    return {"servers": mcp_settings.load()}


@router.put("/mcp")
def put_mcp(body: McpBody, request: Request):
    # stdio 配置可指定任意命令，仅管理员可改
    _require_admin(request)
    try:
        return {"servers": mcp_settings.save(body.servers)}
    except mcp_settings.McpConfigError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})


class McpTestBody(BaseModel):
    server: dict


@router.post("/mcp/test")
def test_mcp(body: McpTestBody, request: Request):
    _require_admin(request)
    try:
        return mcp_settings.test_server(body.server)
    except mcp_settings.McpConfigError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})


@router.get("/assets/backups")
def asset_backups(request: Request):
    _require_admin(request)
    from ..services import assets_admin

    return {"backups": assets_admin.list_backups()}


class RollbackBody(BaseModel):
    name: str


@router.post("/assets/rollback")
def asset_rollback(body: RollbackBody, request: Request):
    _require_admin(request)
    from ..services import assets_admin

    try:
        return assets_admin.rollback(body.name)
    except assets_admin.AssetError as exc:
        return JSONResponse(status_code=exc.status, content={"error": str(exc)})
