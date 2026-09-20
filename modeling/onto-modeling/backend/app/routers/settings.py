from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..services import auth, mcp_expose, mcp_settings, model_settings
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


class McpBody(BaseModel):
    servers: list[dict]


class TokenBody(BaseModel):
    name: str


@router.get("/mcp-service")
def mcp_service_status(request: Request):
    _require_admin(request)
    return {
        "path": "/mcp-service/mcp",
        "transport": "streamable-http",
        "tools": mcp_expose.TOOL_CATALOG,
        "tokens": mcp_expose.list_tokens(),
    }


@router.post("/mcp-service/tokens")
def create_service_token(body: TokenBody, request: Request):
    # 服务令牌可读全部项目产物，签发/吊销必须是管理员
    _require_admin(request)
    user = getattr(request.state, "user", None) or {}
    plaintext, entry = mcp_expose.create_token(body.name, user.get("sub", ""))
    # 明文只在本响应出现一次，服务端仅存哈希
    return {"token": plaintext, "entry": entry}


@router.delete("/mcp-service/tokens/{token_id}")
def revoke_service_token(token_id: str, request: Request):
    _require_admin(request)
    if not mcp_expose.revoke_token(token_id):
        return JSONResponse(status_code=404, content={"error": "令牌不存在"})
    return {"ok": True}


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
