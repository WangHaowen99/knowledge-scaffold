"""HTTP/UDS client for the JiuwenBox sandbox API."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from .errors import SandboxCreateError, SandboxExecError, SandboxUnavailableError


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str


class JiuwenboxClient:
    """Thin wrapper around jiuwenbox REST endpoints."""

    def __init__(self, base_url: str, *, timeout: float = 30.0) -> None:
        self._timeout = timeout
        self._client = self._build_client(base_url, timeout=timeout)

    @staticmethod
    def _build_client(base_url: str, *, timeout: float) -> httpx.Client:
        normalized = base_url.strip()
        if normalized.startswith("unix://"):
            socket_path = normalized[len("unix://"):]
            transport = httpx.HTTPTransport(uds=socket_path)
            return httpx.Client(
                transport=transport,
                base_url="http://jiuwenbox",
                timeout=timeout,
                trust_env=False,
            )
        if "://" not in normalized:
            normalized = f"http://{normalized}"
        return httpx.Client(
            base_url=normalized.rstrip("/"),
            timeout=timeout,
            trust_env=False,
        )

    def create_sandbox(
        self,
        sandbox_id: str,
        policy: dict[str, Any],
        *,
        policy_mode: str = "override",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "sandbox_id": sandbox_id,
            "policy": policy,
            "policy_mode": policy_mode,
        }
        try:
            response = self._client.post("/api/v1/sandboxes", json=payload)
            if response.status_code >= 400:
                detail = _response_detail(response)
                raise SandboxCreateError(
                    f"create sandbox {sandbox_id} failed ({response.status_code}): {detail}"
                )
            return response.json()
        except httpx.HTTPError as exc:
            raise SandboxCreateError(f"create sandbox request failed: {exc}") from exc

    def exec(
        self,
        sandbox_id: str,
        command: list[str],
        *,
        workdir: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: int | None = None,
    ) -> ExecResult:
        payload: dict[str, Any] = {"command": list(command)}
        if workdir:
            payload["workdir"] = workdir
        if env:
            payload["env"] = env
        if timeout_seconds is not None:
            payload["timeout_seconds"] = int(timeout_seconds)

        request_timeout = float(timeout_seconds or self._timeout) + 15.0
        try:
            response = self._client.post(
                f"/api/v1/sandboxes/{sandbox_id}/exec",
                json=payload,
                timeout=request_timeout,
            )
            if response.status_code >= 400:
                detail = _response_detail(response)
                raise SandboxExecError(
                    f"exec in sandbox {sandbox_id} failed ({response.status_code}): {detail}"
                )
            data = response.json()
            return ExecResult(
                exit_code=int(data.get("exit_code", 1)),
                stdout=str(data.get("stdout") or ""),
                stderr=str(data.get("stderr") or ""),
            )
        except httpx.TimeoutException as exc:
            raise SandboxExecError(f"exec timed out in sandbox {sandbox_id}") from exc
        except httpx.HTTPError as exc:
            raise SandboxExecError(f"exec request failed: {exc}") from exc

    def delete_sandbox(self, sandbox_id: str) -> None:
        try:
            response = self._client.delete(f"/api/v1/sandboxes/{sandbox_id}")
            if response.status_code in {204, 404}:
                return
            if response.status_code >= 400:
                detail = _response_detail(response)
                raise SandboxExecError(
                    f"delete sandbox {sandbox_id} failed ({response.status_code}): {detail}"
                )
        except httpx.HTTPError as exc:
            raise SandboxExecError(f"delete sandbox request failed: {exc}") from exc

    def create_proxy(
        self,
        *,
        path_prefix: str,
        target_endpoint: str,
        api_key: str = "",
        skip_cert_verify: bool = False,
    ) -> dict[str, Any]:
        payload = {
            "path_prefix": path_prefix,
            "target_endpoint": target_endpoint,
            "api_key": api_key,
            "skip_cert_verify": bool(skip_cert_verify),
        }
        try:
            response = self._client.post("/api/v1/proxies", json=payload)
            if response.status_code >= 400:
                detail = _response_detail(response)
                raise SandboxUnavailableError(
                    f"create proxy {path_prefix} failed ({response.status_code}): {detail}"
                )
            data = response.json()
            return data if isinstance(data, dict) else {}
        except httpx.HTTPError as exc:
            raise SandboxUnavailableError(f"create proxy request failed: {exc}") from exc

    def start_proxy(self, proxy_name: str) -> dict[str, Any]:
        try:
            response = self._client.post(f"/api/v1/proxies/{proxy_name}/start")
            if response.status_code >= 400:
                detail = _response_detail(response)
                raise SandboxUnavailableError(
                    f"start proxy {proxy_name} failed ({response.status_code}): {detail}"
                )
            payload = response.json()
            return payload if isinstance(payload, dict) else {}
        except httpx.HTTPError as exc:
            raise SandboxUnavailableError(f"start proxy request failed: {exc}") from exc

    def delete_proxy(self, proxy_name: str) -> None:
        try:
            response = self._client.delete(f"/api/v1/proxies/{proxy_name}")
            if response.status_code in {200, 204, 404}:
                return
            if response.status_code >= 400:
                detail = _response_detail(response)
                raise SandboxUnavailableError(
                    f"delete proxy {proxy_name} failed ({response.status_code}): {detail}"
                )
        except httpx.HTTPError as exc:
            raise SandboxUnavailableError(f"delete proxy request failed: {exc}") from exc


def _response_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except json.JSONDecodeError:
        return response.text or response.reason_phrase
    if isinstance(payload, dict):
        return str(payload.get("error") or payload.get("detail") or payload)
    return str(payload)
