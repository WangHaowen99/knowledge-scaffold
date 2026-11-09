"""Process-wide gateway state and FastAPI dependencies."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from .auth import KeyRing, Principal, extract_token
from .registry import Registry


@dataclass
class State:
    registry: Registry
    keys: KeyRing


_state: State | None = None


def set_state(state: State | None) -> None:
    global _state
    _state = state


def state() -> State:
    if _state is None:
        raise RuntimeError("kb-gateway is not started")
    return _state


def registry() -> Registry:
    return state().registry


def caller(request: Request, capability: str) -> Principal:
    principal = state().keys.authenticate(extract_token(request.headers))
    principal.require(capability)
    return principal


def need(capability: str):
    def dependency(request: Request) -> Principal:
        return caller(request, capability)

    return dependency
