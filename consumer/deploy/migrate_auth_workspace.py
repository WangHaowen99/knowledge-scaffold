#!/usr/bin/env python3
"""Safely reconcile legacy consumer users with the standalone auth server.

The command is self-contained because release bundles do not ship the backend
Python package. It validates both stores, snapshots migration inputs before any
comparison or mutation, and audits workspace ACL references.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import tarfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class UserStore:
    path: Path
    payload: dict[str, Any]
    users: dict[str, dict[str, Any]]
    canonical_users: dict[str, dict[str, Any]]
    token_version_defaults: int


class MigrationIssue(ValueError):
    def __init__(self, code: str, detail: str, *, findings: list[dict[str, Any]] | None = None) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.findings = findings or []


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MigrationIssue("FILE_NOT_FOUND", f"File does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise MigrationIssue("INVALID_JSON", f"Invalid JSON in {path}: {exc.msg}") from exc


def _validate_password_hash(value: Any, *, label: str, username: str) -> str:
    password_hash = str(value or "").strip()
    parts = password_hash.split("$")
    if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
        raise MigrationIssue(
            "INVALID_USER_STORE",
            f"{label} user {username!r} has an invalid password_hash format",
        )
    try:
        if int(parts[1]) <= 0:
            raise ValueError
        for encoded in parts[2:]:
            base64.b64decode(encoded.encode("ascii"), altchars=b"-_", validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise MigrationIssue(
            "INVALID_USER_STORE",
            f"{label} user {username!r} has an invalid password_hash payload",
        ) from exc
    return password_hash


def _normalized_token_version(value: Any, *, label: str, username: str) -> int:
    if value is None:
        return 1
    if isinstance(value, bool):
        raise MigrationIssue("INVALID_USER_STORE", f"{label} user {username!r} has invalid token_version")
    try:
        version = int(value)
    except (TypeError, ValueError) as exc:
        raise MigrationIssue(
            "INVALID_USER_STORE",
            f"{label} user {username!r} has invalid token_version",
        ) from exc
    if version < 1:
        raise MigrationIssue("INVALID_USER_STORE", f"{label} user {username!r} has invalid token_version")
    return version


def _normalized_must_change(record: dict[str, Any], *, label: str, username: str) -> bool:
    if "must_change_password" in record:
        value = record["must_change_password"]
        if not isinstance(value, bool):
            raise MigrationIssue(
                "INVALID_USER_STORE",
                f"{label} user {username!r} has non-boolean must_change_password",
            )
        return value
    changed_at = record.get("password_changed_at")
    if changed_at is not None and not isinstance(changed_at, str):
        raise MigrationIssue(
            "INVALID_USER_STORE",
            f"{label} user {username!r} has invalid password_changed_at",
        )
    return not bool(str(changed_at or "").strip())


def load_user_store(path: Path, *, label: str) -> UserStore:
    payload = _read_json(path)
    records = payload.get("users") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        raise MigrationIssue("INVALID_USER_STORE", f"{label} must contain a users array")

    users: dict[str, dict[str, Any]] = {}
    canonical_users: dict[str, dict[str, Any]] = {}
    token_version_defaults = 0
    for index, raw in enumerate(records):
        if not isinstance(raw, dict):
            raise MigrationIssue("INVALID_USER_STORE", f"{label} users[{index}] must be an object")
        username = str(raw.get("username") or "").strip()
        if not username or username != raw.get("username"):
            raise MigrationIssue("INVALID_USER_STORE", f"{label} users[{index}] has an invalid username")
        if username in users:
            raise MigrationIssue("INVALID_USER_STORE", f"{label} contains duplicate username {username!r}")
        role = str(raw.get("role") or "user").strip()
        if role not in {"admin", "user"}:
            raise MigrationIssue("INVALID_USER_STORE", f"{label} user {username!r} has invalid role {role!r}")
        password_hash = _validate_password_hash(raw.get("password_hash"), label=label, username=username)
        token_version = _normalized_token_version(raw.get("token_version"), label=label, username=username)
        if "token_version" not in raw:
            token_version_defaults += 1
        must_change = _normalized_must_change(raw, label=label, username=username)

        record = dict(raw)
        users[username] = record
        canonical = dict(record)
        canonical.update({
            "username": username,
            "role": role,
            "password_hash": password_hash,
            "must_change_password": must_change,
            "token_version": token_version,
        })
        canonical_users[username] = canonical

    return UserStore(
        path=path,
        payload=payload,
        users=users,
        canonical_users=canonical_users,
        token_version_defaults=token_version_defaults,
    )


def _safe_diff_value(field: str, value: Any) -> Any:
    lowered = field.lower()
    if "password_hash" in lowered:
        digest = hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]
        return f"sha256:{digest}"
    if "password" in lowered or "secret" in lowered or (
        "token" in lowered and field != "token_version"
    ):
        return "<redacted>" if value is not None else None
    return value


def user_store_differences(local: UserStore, auth: UserStore) -> list[dict[str, Any]]:
    differences: list[dict[str, Any]] = []
    for username in sorted(set(local.canonical_users) | set(auth.canonical_users)):
        local_record = local.canonical_users.get(username)
        auth_record = auth.canonical_users.get(username)
        if local_record is None or auth_record is None:
            differences.append({
                "username": username,
                "field": "<record>",
                "local": "missing" if local_record is None else "present",
                "auth": "missing" if auth_record is None else "present",
            })
            continue
        for field in sorted(set(local_record) | set(auth_record)):
            local_value = local_record.get(field)
            auth_value = auth_record.get(field)
            if local_value != auth_value:
                differences.append({
                    "username": username,
                    "field": field,
                    "local": _safe_diff_value(field, local_value),
                    "auth": _safe_diff_value(field, auth_value),
                })

    local_top = {key: value for key, value in local.payload.items() if key != "users"}
    auth_top = {key: value for key, value in auth.payload.items() if key != "users"}
    for field in sorted(set(local_top) | set(auth_top)):
        if local_top.get(field) != auth_top.get(field):
            differences.append({
                "username": "<document>",
                "field": field,
                "local": _safe_diff_value(field, local_top.get(field)),
                "auth": _safe_diff_value(field, auth_top.get(field)),
            })
    return differences


def audit_workspace_acl(runtime_root: Path, usernames: set[str]) -> dict[str, Any]:
    workspaces_root = runtime_root / "workspaces"
    findings: list[dict[str, Any]] = []
    public_workspaces: list[str] = []
    workspace_count = 0
    if workspaces_root.exists():
        if not workspaces_root.is_dir():
            findings.append({"workspace_id": "<root>", "type": "invalid_workspaces_root"})
        else:
            for directory in sorted(workspaces_root.iterdir(), key=lambda item: item.name):
                metadata_path = directory / "workspace.json"
                if not directory.is_dir() or not metadata_path.exists():
                    continue
                workspace_count += 1
                try:
                    metadata = _read_json(metadata_path)
                except MigrationIssue as exc:
                    findings.append({
                        "workspace_id": directory.name,
                        "type": "invalid_metadata",
                        "detail": exc.detail,
                    })
                    continue
                if not isinstance(metadata, dict):
                    findings.append({"workspace_id": directory.name, "type": "invalid_metadata"})
                    continue
                metadata_id = str(metadata.get("id") or "").strip()
                if metadata_id != directory.name:
                    findings.append({
                        "workspace_id": directory.name,
                        "type": "workspace_id_mismatch",
                        "detail": f"workspace.json id is {metadata_id!r}",
                    })
                owner = metadata.get("owner")
                if owner is None or owner == "":
                    public_workspaces.append(directory.name)
                elif not isinstance(owner, str) or not owner.strip():
                    findings.append({"workspace_id": directory.name, "type": "invalid_owner"})
                elif owner.strip() not in usernames:
                    findings.append({
                        "workspace_id": directory.name,
                        "type": "unknown_owner",
                        "username": owner.strip(),
                    })
                members = metadata.get("members", [])
                if not isinstance(members, list):
                    findings.append({"workspace_id": directory.name, "type": "invalid_members"})
                    continue
                seen: set[str] = set()
                for member in members:
                    username = str(member or "").strip() if isinstance(member, str) else ""
                    if not username:
                        findings.append({"workspace_id": directory.name, "type": "invalid_member"})
                    elif username in seen:
                        findings.append({
                            "workspace_id": directory.name,
                            "type": "duplicate_member",
                            "username": username,
                        })
                    elif username not in usernames:
                        findings.append({
                            "workspace_id": directory.name,
                            "type": "unknown_member",
                            "username": username,
                        })
                    seen.add(username)

    reconciliation_path = runtime_root / "system" / "user-deletion-reconciliation.json"
    if reconciliation_path.exists():
        try:
            payload = _read_json(reconciliation_path)
            records = payload.get("records") if isinstance(payload, dict) else None
            if not isinstance(records, list):
                raise MigrationIssue("INVALID_RECONCILIATION", "records must be an array")
            for index, record in enumerate(records):
                if not isinstance(record, dict):
                    findings.append({
                        "workspace_id": "<system>",
                        "type": "invalid_user_deletion_reconciliation",
                        "detail": f"records[{index}] must be an object",
                    })
                    continue
                username = str(record.get("username") or "").strip()
                findings.append({
                    "workspace_id": "<system>",
                    "type": "pending_user_deletion_reconciliation",
                    "username": username,
                    "detail": f"reconciliation_id={record.get('id') or '<missing>'}",
                })
                if username and username in usernames:
                    findings.append({
                        "workspace_id": "<system>",
                        "type": "same_name_account_inheritance_risk",
                        "username": username,
                    })
        except MigrationIssue as exc:
            findings.append({
                "workspace_id": "<system>",
                "type": "invalid_user_deletion_reconciliation",
                "detail": exc.detail,
            })

    findings.sort(key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True))
    return {
        "clean": not findings,
        "workspace_count": workspace_count,
        "public_workspaces": sorted(public_workspaces),
        "visibility_changed": False,
        "finding_count": len(findings),
        "findings": findings,
    }


def _snapshot(root: Path, backup_root: Path) -> Path | None:
    candidates = [
        root / ".onto-runtime" / "users.json",
        root / ".onto-runtime" / "workspaces",
        root / ".onto-auth" / "users.json",
        root / ".onto-auth" / "keys",
    ]
    existing = [path for path in candidates if path.exists()]
    if not existing:
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    backup_root.mkdir(parents=True, exist_ok=True)
    destination = backup_root / f"migration-snapshot-{stamp}.tar.gz"
    with tarfile.open(destination, "w:gz") as archive:
        for path in existing:
            archive.add(path, arcname=path.relative_to(root))
    destination.chmod(0o600)
    return destination


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.migration-{os.getpid()}")
    try:
        shutil.copyfile(source, temporary)
        temporary.chmod(0o600)
        os.replace(temporary, destination)
        destination.chmod(0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def execute_migration(
    root: Path,
    *,
    dry_run: bool,
    writes_stopped: bool,
    backup_root: Path,
    from_version: str = "",
    to_version: str = "",
) -> tuple[int, dict[str, Any]]:
    root = root.resolve()
    runtime_users = root / ".onto-runtime" / "users.json"
    auth_users = root / ".onto-auth" / "users.json"
    report: dict[str, Any] = {
        "schema_version": 1,
        "mode": "dry-run" if dry_run else "apply",
        "from_version": from_version,
        "to_version": to_version,
        "writes_stopped_confirmed": bool(writes_stopped),
        "status": "failed",
        "backup": None,
        "users": {
            "action": "pending",
            "runtime_exists": runtime_users.exists(),
            "auth_exists": auth_users.exists(),
            "field_differences": [],
        },
    }
    if not dry_run and not writes_stopped:
        report["error"] = {
            "code": "WRITES_NOT_STOPPED",
            "detail": "Apply mode requires --confirm-writes-stopped",
        }
        return 2, report

    if not dry_run:
        snapshot = _snapshot(root, backup_root)
        report["backup"] = str(snapshot) if snapshot else None

    try:
        local_store = load_user_store(runtime_users, label="runtime users") if runtime_users.exists() else None
        auth_store = load_user_store(auth_users, label="auth users") if auth_users.exists() else None
        selected_store = auth_store or local_store

        if local_store and auth_store:
            differences = user_store_differences(local_store, auth_store)
            report["users"]["field_differences"] = differences
            if differences:
                raise MigrationIssue(
                    "USER_STORES_DIVERGED",
                    "Runtime and auth user stores differ; no file was overwritten",
                    findings=differences,
                )
            report["users"]["action"] = "in_sync"
        elif local_store:
            report["users"]["action"] = "would_copy_runtime_to_auth" if dry_run else "copied_runtime_to_auth"
        elif auth_store:
            report["users"]["action"] = "use_auth_only"
        else:
            report["users"]["action"] = "initialize_auth_on_start"

        usernames = set(selected_store.users) if selected_store else set()
        acl = audit_workspace_acl(root / ".onto-runtime", usernames)
        report["workspace_acl"] = acl
        if not acl["clean"]:
            raise MigrationIssue(
                "WORKSPACE_ACL_RECONCILIATION_REQUIRED",
                "Workspace owner/member reconciliation is required before startup",
                findings=acl["findings"],
            )

        if local_store and not auth_store and not dry_run:
            _atomic_copy(runtime_users, auth_users)
            auth_store = load_user_store(auth_users, label="copied auth users")
            if _sha256(runtime_users) != _sha256(auth_users):
                raise MigrationIssue("COPY_VERIFICATION_FAILED", "Copied auth users checksum does not match source")
            selected_store = auth_store

        report["users"].update({
            "user_count": len(selected_store.users) if selected_store else 0,
            "token_version_defaulted_to_one": (
                selected_store.token_version_defaults if selected_store else 0
            ),
            "password_hashes_preserved": bool(selected_store),
            "destination_mode": (
                oct(auth_users.stat().st_mode & 0o777) if auth_users.exists() else None
            ),
        })
        report["status"] = "ok"
        return 0, report
    except MigrationIssue as exc:
        report["error"] = {"code": exc.code, "detail": exc.detail}
        if exc.findings and not report["users"].get("field_differences"):
            report["error"]["findings"] = exc.findings
        return 2, report


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)
    path.chmod(0o600)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--backup-root", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--from-version", default="")
    parser.add_argument("--to-version", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--confirm-writes-stopped", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    backup_root = args.backup_root or args.root / "deploy" / "backups"
    code, report = execute_migration(
        args.root,
        dry_run=args.dry_run,
        writes_stopped=args.confirm_writes_stopped,
        backup_root=backup_root,
        from_version=args.from_version,
        to_version=args.to_version,
    )
    write_report(args.report, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
