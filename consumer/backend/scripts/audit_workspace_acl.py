"""Audit workspace owner/member references against the auth-server user store.

The command is intentionally read-only.  It is suitable for migration preflight
and can be repeated without changing either source file.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any, Iterable, TextIO


_RECONCILIATION_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
logger = logging.getLogger(__name__)
result_logger = logging.getLogger(f"{__name__}.result")


def _configure_stream_logger(
    target: logging.Logger,
    *,
    stream: TextIO,
    level: int,
) -> None:
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    target.handlers.clear()
    target.addHandler(handler)
    target.setLevel(level)
    target.propagate = False


def _configure_cli_logging() -> None:
    _configure_stream_logger(logger, stream=sys.stderr, level=logging.ERROR)
    _configure_stream_logger(result_logger, stream=sys.stdout, level=logging.INFO)


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"File does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc.msg}") from exc


def load_auth_usernames(users_file: Path) -> set[str]:
    """Return all valid usernames from the auth-server ``users.json`` file."""
    payload = _load_json(users_file)
    records = payload.get("users") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        raise ValueError(f"Auth user file must contain a users array: {users_file}")

    usernames: set[str] = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValueError(f"Auth user entry {index} must be an object")
        username = record.get("username")
        if not isinstance(username, str) or not username:
            raise ValueError(f"Auth user entry {index} has no username")
        if username != username.strip():
            raise ValueError(f"Auth user entry {index} has an invalid username")
        if username in usernames:
            raise ValueError(f"Auth user file contains duplicate username {username!r}")
        usernames.add(username)
    return usernames


def _finding(
    workspace_id: str,
    finding_type: str,
    *,
    username: str | None = None,
    detail: str | None = None,
) -> dict[str, str]:
    item = {"workspace_id": workspace_id, "type": finding_type}
    if username is not None:
        item["username"] = username
    if detail is not None:
        item["detail"] = detail
    return item


def _audit_workspace_metadata(
    workspace_id: str,
    metadata: Any,
    auth_usernames: set[str],
) -> list[dict[str, str]]:
    if not isinstance(metadata, dict):
        return [_finding(workspace_id, "invalid_metadata", detail="workspace.json must be an object")]

    findings: list[dict[str, str]] = []
    owner = metadata.get("owner")
    if owner is not None:
        if (
            not isinstance(owner, str)
            or not owner
            or owner != owner.strip()
        ):
            findings.append(
                _finding(
                    workspace_id,
                    "invalid_owner",
                    detail="owner must be null or a non-empty string without surrounding whitespace",
                )
            )
        elif owner not in auth_usernames:
            findings.append(_finding(workspace_id, "unknown_owner", username=owner))

    members = metadata.get("members", [])
    if not isinstance(members, list):
        findings.append(_finding(workspace_id, "invalid_members", detail="members must be an array"))
        return findings

    seen: set[str] = set()
    for index, member in enumerate(members):
        if (
            not isinstance(member, str)
            or not member
            or member != member.strip()
        ):
            findings.append(
                _finding(
                    workspace_id,
                    "invalid_member",
                    detail=(
                        f"members[{index}] must be a non-empty string "
                        "without surrounding whitespace"
                    ),
                )
            )
            continue
        if member in seen:
            findings.append(_finding(workspace_id, "duplicate_member", username=member))
            continue
        seen.add(member)
        if member not in auth_usernames:
            findings.append(_finding(workspace_id, "unknown_member", username=member))

    return findings


def _workspace_metadata_paths(workspaces_root: Path) -> Iterable[tuple[str, Path]]:
    if not workspaces_root.exists():
        raise ValueError(f"Workspace root does not exist: {workspaces_root}")
    if not workspaces_root.is_dir():
        raise ValueError(f"Workspace root is not a directory: {workspaces_root}")
    for entry in sorted(workspaces_root.iterdir(), key=lambda item: item.name):
        if entry.is_dir() and (entry / "workspace.json").exists():
            yield entry.name, entry / "workspace.json"


def audit_workspace_acl(users_file: Path, workspaces_root: Path) -> dict[str, Any]:
    """Build a deterministic, JSON-serializable ACL audit report."""
    auth_usernames = load_auth_usernames(users_file)
    findings: list[dict[str, str]] = []
    workspace_count = 0

    for directory_id, metadata_path in _workspace_metadata_paths(workspaces_root):
        workspace_count += 1
        try:
            metadata = _load_json(metadata_path)
        except ValueError as exc:
            findings.append(_finding(directory_id, "invalid_metadata", detail=str(exc)))
            continue
        workspace_id = directory_id
        if isinstance(metadata, dict):
            metadata_id = str(metadata.get("id") or "").strip()
            if not metadata_id:
                findings.append(
                    _finding(directory_id, "invalid_workspace_id", detail="workspace.json has no id")
                )
            elif metadata_id != directory_id:
                findings.append(
                    _finding(
                        directory_id,
                        "workspace_id_mismatch",
                        detail=f"workspace.json id is {metadata_id!r}",
                    )
                )
        findings.extend(_audit_workspace_metadata(workspace_id, metadata, auth_usernames))

    reconciliation_findings = _audit_pending_reconciliations(
        workspaces_root,
        auth_usernames,
    )
    findings.extend(reconciliation_findings)

    findings.sort(
        key=lambda item: (
            item["workspace_id"],
            item["type"],
            item.get("username", ""),
            item.get("detail", ""),
        )
    )
    return {
        "clean": not findings,
        "auth_user_count": len(auth_usernames),
        "workspace_count": workspace_count,
        "pending_reconciliation_count": sum(
            item["type"] == "pending_user_deletion_reconciliation"
            for item in reconciliation_findings
        ),
        "finding_count": len(findings),
        "findings": findings,
    }


def _audit_pending_reconciliations(
    workspaces_root: Path,
    auth_usernames: set[str],
) -> list[dict[str, str]]:
    path = (
        workspaces_root.parent
        / "system"
        / "user-deletion-reconciliation.json"
    )
    if not path.exists():
        return []
    try:
        payload = _load_json(path)
        records = _validate_reconciliation_document(payload)
    except ValueError as exc:
        return [
            _finding(
                "<system>",
                "invalid_user_deletion_reconciliation",
                detail=str(exc),
            )
        ]
    findings: list[dict[str, str]] = []
    for record in records:
        # Completed records are removed from this file.  Every record that is
        # still present reserves its username, regardless of the current
        # deletion phase (intent, unconfirmed identity delete, or cleanup).
        username = record["username"]
        record_id = record["id"]
        findings.append(
            _finding(
                "<system>",
                "pending_user_deletion_reconciliation",
                username=username,
                detail="reconciliation_id={}".format(record_id),
            )
        )
        if username and username in auth_usernames:
            findings.append(
                _finding(
                    "<system>",
                    "same_name_account_inheritance_risk",
                    username=username,
                )
            )
    return findings


def _validate_reconciliation_document(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise ValueError("reconciliation document must be an object")
    if payload.get("version") != 1:
        raise ValueError("reconciliation version must be 1")
    records = payload.get("records")
    if not isinstance(records, list):
        raise ValueError("reconciliation records must be an array")

    seen_ids: set[str] = set()
    seen_usernames: set[str] = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValueError(f"records[{index}] must be an object")
        record_id = record.get("id")
        username = record.get("username")
        if not isinstance(record_id, str) or not _RECONCILIATION_ID_PATTERN.fullmatch(record_id):
            raise ValueError(f"records[{index}].id must be 32 lowercase hex characters")
        if record_id in seen_ids:
            raise ValueError(f"records[{index}].id is duplicated")
        if (
            not isinstance(username, str)
            or not username
            or username != username.strip()
        ):
            raise ValueError(f"records[{index}].username must be a canonical non-empty string")
        if username in seen_usernames:
            raise ValueError(f"records[{index}].username is duplicated")
        if not isinstance(record.get("status"), str) or not record.get("status"):
            raise ValueError(f"records[{index}].status must be a non-empty string")
        if not isinstance(record.get("identity_deleted"), bool):
            raise ValueError(f"records[{index}].identity_deleted must be boolean")
        attempts = record.get("attempts")
        if not isinstance(attempts, int) or attempts < 1:
            raise ValueError(f"records[{index}].attempts must be a positive integer")
        if not isinstance(record.get("completed_steps"), list):
            raise ValueError(f"records[{index}].completed_steps must be an array")
        if not isinstance(record.get("errors"), dict):
            raise ValueError(f"records[{index}].errors must be an object")
        seen_ids.add(record_id)
        seen_usernames.add(username)
    return records


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit workspace owner/member references against auth-server users.json"
    )
    parser.add_argument("--auth-users-file", required=True, type=Path)
    parser.add_argument("--workspaces-root", required=True, type=Path)
    parser.add_argument("--output", type=Path, help="Optional path for the JSON report")
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_cli_logging()
    args = _build_parser().parse_args(argv)
    try:
        report = audit_workspace_acl(args.auth_users_file, args.workspaces_root)
    except ValueError as exc:
        rendered_error = json.dumps(
            {"clean": False, "error": str(exc)},
            ensure_ascii=False,
        )
        logger.error(rendered_error)
        return 1

    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    result_logger.info(rendered.rstrip("\n"))
    return 0 if report["clean"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
