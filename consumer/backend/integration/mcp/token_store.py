"""File-backed MCP token registry without plaintext credential persistence."""

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

from .config import load_mcp_runtime_config
from .token_crypto import hash_token, mask_token


_LOCK = threading.Lock()
_DOCUMENT_VERSION = 2


class MCPTokenStoreError(RuntimeError):
    """Raised when the durable token registry is malformed or unavailable."""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class MCPTokenStore:
    def __init__(self, path: Path | None = None) -> None:
        resolved_path = path
        if resolved_path is None:
            resolved_path = load_mcp_runtime_config().token_store_path
        self.path = Path(resolved_path).resolve()
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.backup_path = self.path.with_suffix(self.path.suffix + ".pre-plaintext.bak")
        self.last_migration_report: dict[str, Any] = {
            "migrated": False,
            "removed_plaintext_fields": 0,
            "backup_path": "",
        }

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
        durable_item = dict(item)
        durable_item.pop("token", None)
        durable_item.pop("token_value", None)
        with self._locked_document(mutating=True) as document:
            document["tokens"].append(durable_item)
        return self._public_item(durable_item)

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

    def revoke_user_tokens(self, *, username: str) -> int:
        normalized_username = str(username or "").strip()
        if not normalized_username:
            raise ValueError("username 不能为空")
        now = utc_now_iso()
        revoked_count = 0
        with self._locked_document(mutating=True) as document:
            for item in document["tokens"]:
                if (
                    item.get("username") == normalized_username
                    and item.get("status") != "revoked"
                    and not item.get("revoked_at")
                ):
                    item["status"] = "revoked"
                    item["revoked_at"] = now
                    revoked_count += 1
        return revoked_count

    def verify_active_token(
        self,
        *,
        jti: str,
        token_hash: str,
        update_last_used: bool = True,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        now = utc_now_iso()
        with self._locked_document(mutating=True) as document:
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

    def migrate_legacy_plaintext_tokens(self) -> dict[str, Any]:
        with self._locked_document():
            pass
        return dict(self.last_migration_report)

    @contextmanager
    def _locked_document(self, *, mutating: bool = False) -> Iterator[dict[str, Any]]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            with self.lock_path.open("a+", encoding="utf-8") as lock_file:
                try:
                    os.chmod(self.lock_path, 0o600)
                except OSError:
                    pass
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    raw_bytes = self.path.read_bytes() if self.path.exists() else b""
                    document = self._read_document()
                    migration = self._remove_plaintext_fields(document)
                    if migration["migrated"]:
                        if raw_bytes and not self.backup_path.exists():
                            self._write_bytes_atomic(self.backup_path, raw_bytes)
                        migration["backup_path"] = str(self.backup_path) if raw_bytes else ""
                        self._write_document(document)
                    self.last_migration_report = migration
                    yield document
                    if mutating:
                        self._write_document(document)
                finally:
                    if fcntl is not None:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _read_document(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": _DOCUMENT_VERSION, "tokens": []}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MCPTokenStoreError("MCP token registry 无法读取或格式无效") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("tokens"), list):
            raise MCPTokenStoreError("MCP token registry 必须包含 tokens 数组")
        if any(not isinstance(item, dict) for item in payload["tokens"]):
            raise MCPTokenStoreError("MCP token registry 包含无效记录")
        return {
            "version": int(payload.get("version") or 1),
            "tokens": [dict(item) for item in payload["tokens"]],
        }

    def _remove_plaintext_fields(self, document: dict[str, Any]) -> dict[str, Any]:
        removed = 0
        for item in document["tokens"]:
            plaintext = ""
            for field in ("token_value", "token"):
                if field not in item:
                    continue
                value = str(item.pop(field) or "").strip()
                if value and not plaintext:
                    plaintext = value
                removed += 1
            if plaintext:
                item.setdefault("token_hash", hash_token(plaintext))
                item.setdefault("token_masked", mask_token(plaintext))
        version_changed = int(document.get("version") or 1) != _DOCUMENT_VERSION
        document["version"] = _DOCUMENT_VERSION
        return {
            "migrated": bool(removed or version_changed),
            "removed_plaintext_fields": removed,
            "backup_path": "",
        }

    def _write_document(self, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self._write_bytes_atomic(self.path, encoded)

    @staticmethod
    def _write_bytes_atomic(path: Path, payload: bytes) -> None:
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_bytes(payload)
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        os.replace(tmp_path, path)

    @staticmethod
    def _public_item(item: dict[str, Any]) -> dict[str, Any]:
        status = str(item.get("status") or "active")
        expires_at = str(item.get("expires_at") or "").strip()
        revoked_at = item.get("revoked_at")
        if revoked_at:
            status = "revoked"
        elif expires_at and _is_expired(expires_at):
            status = "expired"
        ontology_id = str(item.get("ontology_id") or "").strip()
        return {
            "token_id": str(item.get("token_id") or ""),
            "name": str(item.get("name") or ""),
            "token_masked": str(item.get("token_masked") or ""),
            "username": str(item.get("username") or ""),
            "workspace_id": str(item.get("workspace_id") or ""),
            "ontology_id": ontology_id,
            "binding_mode": "ontology" if ontology_id else "workspace",
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
