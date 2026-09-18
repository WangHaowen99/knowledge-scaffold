from fastapi import APIRouter
from pydantic import BaseModel

from ..services import auth

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginBody(BaseModel):
    username: str
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


@router.post("/refresh")
def refresh(body: RefreshBody):
    return auth.token_request(
        {"grant_type": "refresh_token", "refresh_token": body.refresh_token}
    )
