from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..services import auth, mcp_settings, model_settings
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


@router.get("/model")
def get_model_settings(request: Request):
    _require_admin(request)
    return model_settings.masked()


@router.put("/model")
def put_model_settings(body: ModelBody, request: Request):
    _require_admin(request)
    try:
        return model_settings.save(body.base_url, body.api_key, body.model)
    except model_settings.ModelConfigError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})


@router.put("/model/jiuwen")
def put_jiuwen_model_settings(body: ModelBody, request: Request):
    """Jiuwen 引擎的 OpenAI 兼容模型配置（网关地址 / API Key / 模型名）。"""
    _require_admin(request)
    try:
        return model_settings.save_jiuwen(body.base_url, body.api_key, body.model)
    except model_settings.ModelConfigError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})


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
