from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..services import auth

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginBody(BaseModel):
    username: str
    password: str


class RegisterBody(BaseModel):
    username: str
    email: str = ""  # 界面不再收集邮箱；auth-server 要求唯一邮箱，由服务端合成
    password: str


class RefreshBody(BaseModel):
    refresh_token: str


@router.get("/status")
def status():
    return {"enabled": auth.ENABLED}


@router.post("/login")
def login(body: LoginBody):
    return auth.token_request(
        {
            "grant_type": "password",
            "username": body.username,
            "password": body.password,
        }
    )


@router.post("/register")
def register(body: RegisterBody):
    payload = body.model_dump()
    if not payload["email"]:
        payload["email"] = f"{body.username}@users.example.com"
    status_code, data = auth.register_request(payload)
    return JSONResponse(status_code=status_code, content=data)


@router.post("/refresh")
def refresh(body: RefreshBody):
    return auth.token_request(
        {"grant_type": "refresh_token", "refresh_token": body.refresh_token}
    )
