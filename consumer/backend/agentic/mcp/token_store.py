"""File-backed MCP token registry."""

from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None  # type: ignore[assignment]

from .config import MCPRuntimeConfig, load_mcp_runtime_config


_LOCK = threading.Lock()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class MCPTokenStore:
    def __init__(self, path: Path | None = None) -> None:
        cfg = load_mcp_runtime_config()
        self.path = Path(path or cfg.token_store_path).resolve()
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    def list_tokens(self, *, username: str, workspace_id: str) -> list[dict[str, Any]]:
        # Write the sanitized document back so legacy plaintext token fields are
        # removed as soon as the registry is accessed after an upgrade.
        with self._locked_document(mutating=True) as document:
            items = [
                self._public_item(item)
                for item in document["tokens"]
                if item.get("username") == username and item.get("workspace_id") == workspace_id
            ]
        return sorted(items, key=lambda item: str(item.get("created_at") or ""), reverse=True)

    def create_token(self, item: dict[str, Any]) -> dict[str, Any]:
        with self._locked_document(mutating=True) as document:
            document["tokens"].append(dict(item))
        return self._public_item(item)

    def revoke_token(self, *, username: str, workspace_id: str, token_id: str) -> bool:
        now = utc_now_iso()
        revoked = False
        with self._locked_document(mutating=True) as document:
            for item in document["tokens"]:
                if (
                    item.get("token_id") == token_id
                    and item.get("username") == username
                    and item.get("workspace_id") == workspace_id
                    and item.get("status") != "revoked"
                ):
                    item["status"] = "revoked"
                    item["revoked_at"] = now
                    revoked = True
                    break
        return revoked

    def verify_active_token(self, *, jti: str, token_hash: str, update_last_used: bool = True) -> tuple[bool, str, dict[str, Any] | None]:
        now = utc_now_iso()
        with self._locked_document(mutating=update_last_used) as document:
            for item in document["tokens"]:
                if str(item.get("jti") or "") != str(jti or ""):
                    continue
                if str(item.get("token_hash") or "") != str(token_hash or ""):
                    return False, "token_hash_mismatch", None
                status = str(item.get("status") or "active")
                if status != "active":
                    return False, "token_{}".format(status), None
                if item.get("revoked_at"):
                    return False, "token_revoked", None
                expires_at = str(item.get("expires_at") or "").strip()
                if expires_at and _is_expired(expires_at):
                    item["status"] = "expired"
                    return False, "token_expired", None
                if update_last_used:
                    item["last_used_at"] = now
                return True, "ok", self._public_item(item)
        return False, "token_not_registered", None

    @contextmanager
    def _locked_document(self, *, mutating: bool = False) -> Iterator[dict[str, Any]]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            with self.lock_path.open("a+", encoding="utf-8") as lock_file:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    document = self._read_document()
                    yield document
                    if mutating:
                        self._write_document(document)
                finally:
                    if fcntl is not None:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _read_document(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "tokens": []}
        try:
            with self.path.open("r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception:
            return {"version": 1, "tokens": []}
        if not isinstance(payload, dict):
            return {"version": 1, "tokens": []}
        tokens = payload.get("tokens")
        if not isinstance(tokens, list):
            tokens = []
        sanitized_tokens = []
        for item in tokens:
            if not isinstance(item, dict):
                continue
            sanitized = dict(item)
            sanitized.pop("token", None)
            sanitized.pop("token_value", None)
            sanitized_tokens.append(sanitized)
        return {"version": 1, "tokens": sanitized_tokens}

    def _write_document(self, payload: dict[str, Any]) -> None:
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, self.path)

    @staticmethod
    def _public_item(item: dict[str, Any]) -> dict[str, Any]:
        status = str(item.get("status") or "active")
        expires_at = str(item.get("expires_at") or "").strip()
        revoked_at = item.get("revoked_at")
        if revoked_at:
            status = "revoked"
        elif expires_at and _is_expired(expires_at):
            status = "expired"

        return {
            "token_id": str(item.get("token_id") or ""),
            "name": str(item.get("name") or ""),
            "token_masked": str(item.get("token_masked") or ""),
            "username": str(item.get("username") or ""),
            "workspace_id": str(item.get("workspace_id") or ""),
            "scopes": list(item.get("scopes") or []),
            "status": status,
            "created_at": item.get("created_at"),
            "expires_at": item.get("expires_at"),
            "revoked_at": item.get("revoked_at"),
            "last_used_at": item.get("last_used_at"),
        }


def _is_expired(expires_at: str) -> bool:
    try:
        value = expires_at.replace("Z", "+00:00")
        expires = datetime.fromisoformat(value)
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
    except Exception:
        return False
    return expires <= datetime.now(timezone.utc)
