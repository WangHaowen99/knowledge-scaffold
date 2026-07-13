"""Audit workspace owner/member references against the auth-server user store.

The command is intentionally read-only.  It is suitable for migration preflight
and can be repeated without changing either source file.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable


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
        username = str(record.get("username") or "").strip()
        if not username:
            raise ValueError(f"Auth user entry {index} has no username")
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
        if not isinstance(owner, str) or not owner.strip():
            findings.append(
                _finding(workspace_id, "invalid_owner", detail="owner must be null or a non-empty string")
            )
        elif owner.strip() not in auth_usernames:
            findings.append(_finding(workspace_id, "unknown_owner", username=owner.strip()))

    members = metadata.get("members", [])
    if not isinstance(members, list):
        findings.append(_finding(workspace_id, "invalid_members", detail="members must be an array"))
        return findings

    seen: set[str] = set()
    for index, member in enumerate(members):
        if not isinstance(member, str) or not member.strip():
            findings.append(
                _finding(
                    workspace_id,
                    "invalid_member",
                    detail=f"members[{index}] must be a non-empty string",
                )
            )
            continue
        username = member.strip()
        if username in seen:
            findings.append(_finding(workspace_id, "duplicate_member", username=username))
            continue
        seen.add(username)
        if username not in auth_usernames:
            findings.append(_finding(workspace_id, "unknown_member", username=username))

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

    reconciliation_findings = _audit_pending_reconciliations(workspaces_root)
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
        "pending_reconciliation_count": len(reconciliation_findings),
        "finding_count": len(findings),
        "findings": findings,
    }


def _audit_pending_reconciliations(
    workspaces_root: Path,
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
    except ValueError as exc:
        return [
            _finding(
                "<system>",
                "invalid_user_deletion_reconciliation",
                detail=str(exc),
            )
        ]
    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        return [
            _finding(
                "<system>",
                "invalid_user_deletion_reconciliation",
                detail="records must be an array",
            )
        ]
    findings: list[dict[str, str]] = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            findings.append(
                _finding(
                    "<system>",
                    "invalid_user_deletion_reconciliation",
                    detail=f"records[{index}] must be an object",
                )
            )
            continue
        if str(record.get("status") or "pending") != "pending":
            continue
        username = str(record.get("username") or "").strip()
        record_id = str(record.get("id") or "").strip()
        findings.append(
            _finding(
                "<system>",
                "pending_user_deletion_reconciliation",
                username=username or None,
                detail="reconciliation_id={}".format(record_id or "<missing>"),
            )
        )
    return findings


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit workspace owner/member references against auth-server users.json"
    )
    parser.add_argument("--auth-users-file", required=True, type=Path)
    parser.add_argument("--workspaces-root", required=True, type=Path)
    parser.add_argument("--output", type=Path, help="Optional path for the JSON report")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        report = audit_workspace_acl(args.auth_users_file, args.workspaces_root)
    except ValueError as exc:
        print(json.dumps({"clean": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1

    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["clean"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
