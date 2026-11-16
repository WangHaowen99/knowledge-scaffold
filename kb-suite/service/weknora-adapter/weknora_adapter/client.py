"""Thin client for WeKnora's HTTP API: one API key, envelope unwrapping, error mapping."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import httpx
from pydantic_settings import BaseSettings, SettingsConfigDict

from kb_provider_kit import ProviderError


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WEKNORA_")

    # base of the WeKnora REST API: the service address followed by /api/v1 (set by compose)
    api_base: str = ""
    api_key: str = ""
    embedding_model_id: str = ""
    chat_model_id: str = ""
    max_file_mb: int = 50
    timeout_s: float = 120.0
    # hand .json files to WeKnora as plain text: its JSON reader refuses malformed JSON and
    # rewrites the rest (arrays become objects keyed "0", "1", ...); false keeps that reader
    json_as_text: bool = True
    # adapter side
    adapter_key: str = ""
    id_prefix: str = "wk_"
    label: str = "WeKnora"
    upload_dir: str = "/tmp/weknora-adapter-uploads"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def _message(response: httpx.Response) -> tuple[str, str]:
    try:
        body = response.json()
    except ValueError:
        return "backend_error", response.text[:300]
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        return str(err.get("code") or "backend_error"), str(err.get("message") or "")
    if isinstance(err, str):
        return str(body.get("code") or "backend_error"), err
    return str(body.get("code") or "backend_error"), str(body.get("message") or response.text[:300])


def _status(code: int) -> int:
    # the adapter's own key being refused is a deployment fault, not the caller's
    if code in (401, 403):
        return 502
    if code >= 500:
        return 502
    return code


class WeKnora:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    @classmethod
    def from_settings(cls, transport: httpx.AsyncBaseTransport | None = None) -> "WeKnora":
        s = get_settings()
        client = httpx.AsyncClient(
            base_url=s.api_base.rstrip("/"), headers={"X-API-Key": s.api_key}, timeout=s.timeout_s,
            transport=transport, trust_env=False,
        )
        return cls(client)

    async def send(self, method: str, path: str, *, stream: bool = False, **kwargs: Any) -> httpx.Response:
        request = self.client.build_request(method, path, **kwargs)
        try:
            response = await self.client.send(request, stream=stream)
        except httpx.HTTPError as exc:
            raise ProviderError(502, "backend_unavailable", f"WeKnora 不可用：{exc}") from exc
        if response.status_code >= 400:
            if stream:
                await response.aread()
                await response.aclose()
            code, message = _message(response)
            if response.status_code == 409:
                raise ProviderError(409, "duplicate", message or "文档已存在")
            raise ProviderError(_status(response.status_code), code, f"WeKnora：{message}")
        return response

    async def call(self, method: str, path: str, **kwargs: Any) -> Any:
        """The parsed body; for ``{success, data}`` envelopes the whole envelope (callers read data/total)."""

        response = await self.send(method, path, **kwargs)
        if not response.content:
            return None
        return response.json()

    async def data(self, method: str, path: str, **kwargs: Any) -> Any:
        body = await self.call(method, path, **kwargs)
        return body.get("data") if isinstance(body, dict) and "success" in body else body

    async def health(self) -> bool:
        root = str(self.client.base_url).rstrip("/").removesuffix("/api/v1")
        try:
            response = await self.client.get(f"{root}/health", timeout=5)
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    async def aclose(self) -> None:
        await self.client.aclose()
