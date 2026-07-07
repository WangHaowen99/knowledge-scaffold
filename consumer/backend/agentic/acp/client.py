"""ACP Client SDK."""

from __future__ import annotations

import json
from typing import Any, Generator

import requests


class ACPClient:
    """ACP 协议客户端"""

    def __init__(self, base_url: str = "http://localhost:8200", timeout: float = 300.0, headers: dict[str, str] | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        if headers:
            self.session.headers.update(headers)
        self.session.headers.setdefault("Content-Type", "application/json")

    def close(self):
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def get_agent(self) -> dict[str, Any]:
        resp = self.session.get(f"{self.base_url}/acp/agent", timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def create_run(
        self,
        message: str,
        context: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        lang: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "messages": [
                {
                    "role": "user",
                    "parts": [{"type": "text", "content": message}],
                }
            ],
            "context": context or {},
            "metadata": metadata or {},
        }
        if lang:
            payload["lang"] = lang
        resp = self.session.post(f"{self.base_url}/acp/runs", json=payload, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def get_run(self, run_id: str) -> dict[str, Any]:
        resp = self.session.get(f"{self.base_url}/acp/runs/{run_id}", timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def execute_run(self, run_id: str) -> dict[str, Any]:
        resp = self.session.post(f"{self.base_url}/acp/runs/{run_id}/execute", timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def stream_run(self, run_id: str) -> Generator[dict[str, Any], None, None]:
        resp = self.session.post(
            f"{self.base_url}/acp/runs/{run_id}/stream",
            stream=True,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        for line in resp.iter_lines(decode_unicode=True):
            if not line:
                continue
            if line.startswith("data:"):
                data_str = line[5:].strip()
                if not data_str:
                    continue
                try:
                    yield json.loads(data_str)
                except json.JSONDecodeError:
                    continue

    def execute_stream(
        self,
        message: str,
        context: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        lang: str | None = None,
    ) -> Generator[dict[str, Any], None, None]:
        payload: dict[str, Any] = {
            "messages": [
                {
                    "role": "user",
                    "parts": [{"type": "text", "content": message}],
                }
            ],
            "context": context or {},
            "metadata": metadata or {},
        }
        if lang:
            payload["lang"] = lang
        resp = self.session.post(
            f"{self.base_url}/acp/runs/execute_stream",
            json=payload,
            stream=True,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        current_event = None
        for line in resp.iter_lines(decode_unicode=True):
            stripped = line.strip() if line else ""
            if not stripped:
                current_event = None
                continue
            if stripped.startswith("event: "):
                current_event = stripped[7:].strip()
            elif stripped.startswith("data: "):
                data_str = stripped[6:]
                try:
                    payload_data = json.loads(data_str)
                except json.JSONDecodeError:
                    payload_data = {"raw": data_str}
                yield {
                    "event": current_event or "message",
                    "data": payload_data,
                }

    def respond_to_await(
        self,
        run_id: str,
        await_id: str,
        response: str,
    ) -> dict[str, Any]:
        payload = {
            "run_id": run_id,
            "await_id": await_id,
            "response": {
                "role": "user",
                "parts": [{"type": "text", "content": response}],
            },
        }
        resp = self.session.post(f"{self.base_url}/acp/runs/{run_id}/await", json=payload, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def interact_stream(
        self,
        run_id: str,
        await_id: str,
        response_text: str,
    ) -> Generator[dict[str, Any], None, None]:
        payload = {
            "run_id": run_id,
            "await_id": await_id,
            "response": {
                "role": "user",
                "parts": [{"type": "text", "content": response_text}],
            },
        }
        resp = self.session.post(
            f"{self.base_url}/acp/runs/{run_id}/interact_stream",
            json=payload,
            stream=True,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        current_event = None
        for line in resp.iter_lines(decode_unicode=True):
            stripped = line.strip() if line else ""
            if not stripped:
                current_event = None
                continue
            if stripped.startswith("event: "):
                current_event = stripped[7:].strip()
            elif stripped.startswith("data: "):
                data_str = stripped[6:]
                try:
                    payload_data = json.loads(data_str)
                except json.JSONDecodeError:
                    payload_data = {"raw": data_str}
                yield {
                    "event": current_event or "message",
                    "data": payload_data,
                }

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        resp = self.session.delete(f"{self.base_url}/acp/runs/{run_id}", timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def list_runs(self) -> list[dict[str, Any]]:
        resp = self.session.get(f"{self.base_url}/acp/runs", timeout=self.timeout)
        resp.raise_for_status()
        return resp.json().get("runs", [])

    def run_sync(
        self,
        message: str,
        context: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        lang: str | None = None,
    ) -> dict[str, Any]:
        run_info = self.create_run(message, context=context, metadata=metadata, lang=lang)
        return self.execute_run(run_info["run_id"])