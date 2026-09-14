#!/usr/bin/env python3
"""Migrate retired settings and historical LLM output-budget environment values."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any


LEGACY_JSON_FIELD = "context_prune_tools"
LEGACY_ENV_NAME = "AGENT_CONTEXT_PRUNE_TOOLS"
LEGACY_LLM_MAX_TOKENS = frozenset({"4096", "8192"})
CURRENT_LLM_MAX_TOKENS = "32768"
LLM_MAX_TOKENS_BACKUP_SUFFIX = "pre-model-profile-output-budget-v1.bak"
_LLM_ASSIGNMENT_RE = re.compile(
    r"^(?P<prefix>\s*(?:export\s+)?LLM_MAX_TOKENS\s*=\s*)"
    r"(?P<value>[^\r\n]*)(?P<newline>\r\n|\n|\r)?$"
)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"System config must be a JSON object: {path}")
    return payload


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            prefix=f".{path.name}.",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    """Flush a containing directory where the platform exposes directory fsync."""

    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_regular_bytes(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise OSError(f"environment file must be a regular file: {path}")
    return path.read_bytes()


def _ensure_private_file(
    path: Path,
    *,
    owner: tuple[int, int] | None = None,
) -> None:
    """Require a regular file and restrict its mode before exposing secrets."""

    if path.is_symlink() or not path.is_file():
        raise OSError(f"private file must be a regular file: {path}")
    path.chmod(0o600)
    metadata = path.stat(follow_symlinks=False)
    if os.name != "nt":
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise OSError(f"private file mode verification failed: {path}")
        if owner is not None and (metadata.st_uid, metadata.st_gid) != owner:
            raise OSError(f"private file owner verification failed: {path}")


def _atomic_write_bytes(
    path: Path,
    payload: bytes,
    *,
    mode: int = 0o600,
    owner: tuple[int, int] | None = None,
) -> None:
    """Write bytes through a same-directory fsynced replacement."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise OSError(f"environment file must not be a symbolic link: {path}")
    temporary: Path | None = None
    descriptor: int | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            if os.name != "nt":
                os.fchmod(stream.fileno(), mode)
                temporary_metadata = os.fstat(stream.fileno())
                if owner is not None and (
                    temporary_metadata.st_uid,
                    temporary_metadata.st_gid,
                ) != owner:
                    os.fchown(stream.fileno(), *owner)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _ensure_private_file(path, owner=owner)
        _fsync_directory(path.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _backup_base(path: Path, backup_root: Path | None = None) -> Path:
    directory = path.parent if backup_root is None else backup_root
    return directory / f"{path.name}.{LLM_MAX_TOKENS_BACKUP_SUFFIX}"


def _candidate_backup_path(base_path: Path, index: int) -> Path:
    return base_path if index == 0 else base_path.with_name(f"{base_path.name}.{index}")


def _create_verified_backup(
    source_path: Path,
    source_bytes: bytes,
    base_path: Path,
) -> Path:
    for index in range(1000):
        candidate = _candidate_backup_path(base_path, index)
        if candidate.exists() or candidate.is_symlink():
            if candidate.is_symlink() or not candidate.is_file():
                raise OSError(f"backup target is not a regular file: {candidate}")
            if _read_regular_bytes(candidate) == source_bytes:
                if _read_regular_bytes(source_path) != source_bytes:
                    raise OSError("environment file changed while verifying backup")
                _ensure_private_file(candidate)
                return candidate
            continue
        _atomic_write_bytes(candidate, source_bytes)
        if _read_regular_bytes(candidate) != source_bytes:
            raise OSError(f"backup verification failed: {candidate}")
        if _read_regular_bytes(source_path) != source_bytes:
            raise OSError("environment file changed while creating backup")
        return candidate
    raise OSError(f"too many conflicting backup files: {base_path}")


def _clean_env_lines(content: str) -> tuple[str, int]:
    removed = 0
    kept: list[str] = []
    for line in content.splitlines(keepends=True):
        candidate = line.strip()
        if candidate and not candidate.startswith("#") and candidate.split("=", 1)[0].strip() == LEGACY_ENV_NAME:
            removed += 1
            continue
        kept.append(line)
    return "".join(kept), removed


def _env_value_span(raw_value: str) -> tuple[str, int, int] | None:
    """Return the scalar token and its offsets within an assignment value."""

    leading = len(raw_value) - len(raw_value.lstrip())
    body = raw_value[leading:]
    if not body:
        return None

    if body[0] in "'\"":
        quote = body[0]
        closing = body.find(quote, 1)
        if closing < 0:
            return None
        suffix = body[closing + 1 :]
        if suffix.strip() and not suffix.lstrip().startswith("#"):
            return None
        token = body[1:closing]
        if not token:
            return None
        start = leading + 1
        return token, start, start + len(token)

    comment = body.find("#")
    value_part = body if comment < 0 else body[:comment]
    token = value_part.strip()
    if not token:
        return None
    start = leading + len(value_part) - len(value_part.lstrip())
    return token, start, start + len(token)


def _migrate_llm_max_tokens_lines(content: str) -> tuple[str, list[int]]:
    migrated_values: list[int] = []
    rendered: list[str] = []
    for line in content.splitlines(keepends=True):
        match = _LLM_ASSIGNMENT_RE.match(line)
        if match is None:
            rendered.append(line)
            continue
        span = _env_value_span(match.group("value"))
        if span is None or span[0] not in LEGACY_LLM_MAX_TOKENS:
            rendered.append(line)
            continue
        token, start, end = span
        raw_value = match.group("value")
        replacement = raw_value[:start] + CURRENT_LLM_MAX_TOKENS + raw_value[end:]
        rendered.append(
            match.group("prefix")
            + replacement
            + (match.group("newline") or "")
        )
        migrated_values.append(int(token))
    return "".join(rendered), migrated_values


def migrate(
    root: Path,
    *,
    dry_run: bool,
    backup_root: Path | None = None,
) -> dict[str, Any]:
    root = root.resolve()
    config_path = root / ".onto-runtime" / "config" / "system_config.json"
    env_path = root / "backend" / ".env"
    resolved_backup_root = (
        None
        if backup_root is None
        else Path(backup_root).expanduser().resolve()
    )
    report: dict[str, Any] = {
        "schema_version": 1,
        "mode": "dry-run" if dry_run else "apply",
        "status": "ok",
        "changed": False,
        "system_config": {"path": str(config_path), "exists": config_path.exists(), "removed": False},
        "backend_env": {
            "path": str(env_path),
            "exists": env_path.exists(),
            "removed_count": 0,
            "max_tokens_migrated_count": 0,
            "max_tokens_migrated_values": [],
            "max_tokens_from": None,
            "max_tokens_to": None,
            "max_tokens_backup_path": None,
            "max_tokens_planned_backup_path": None,
            "backup_root": str(resolved_backup_root) if resolved_backup_root else None,
        },
    }

    config_payload: dict[str, Any] | None = None
    if config_path.exists():
        config_payload = _read_json(config_path)
        agent_section = config_payload.get("agent")
        if isinstance(agent_section, dict) and LEGACY_JSON_FIELD in agent_section:
            report["system_config"]["removed"] = True
            report["changed"] = True
            if not dry_run:
                del agent_section[LEGACY_JSON_FIELD]
                _atomic_write_text(
                    config_path,
                    json.dumps(config_payload, ensure_ascii=False, indent=2) + "\n",
                )

    if env_path.exists():
        original_env_bytes = _read_regular_bytes(env_path)
        original_env_metadata = env_path.stat(follow_symlinks=False)
        original_env_owner = (
            original_env_metadata.st_uid,
            original_env_metadata.st_gid,
        )
        original_env = original_env_bytes.decode("utf-8")
        cleaned_env, removed_count = _clean_env_lines(original_env)
        migrated_env, migrated_values = _migrate_llm_max_tokens_lines(cleaned_env)
        report["backend_env"]["removed_count"] = removed_count
        report["backend_env"]["max_tokens_migrated_count"] = len(migrated_values)
        report["backend_env"]["max_tokens_migrated_values"] = migrated_values
        report["backend_env"]["max_tokens_from"] = (
            migrated_values[0] if migrated_values else None
        )
        report["backend_env"]["max_tokens_to"] = (
            int(CURRENT_LLM_MAX_TOKENS) if migrated_values else None
        )
        env_changed = migrated_env != original_env
        report["changed"] = bool(report["changed"] or env_changed)
        if migrated_values:
            planned_backup = _backup_base(env_path, resolved_backup_root)
            report["backend_env"]["max_tokens_planned_backup_path"] = str(
                planned_backup
            )
        if env_changed and not dry_run:
            backup_path: Path | None = None
            if migrated_values:
                backup_path = _create_verified_backup(
                    env_path,
                    original_env_bytes,
                    _backup_base(env_path, resolved_backup_root),
                )
                report["backend_env"]["max_tokens_backup_path"] = str(backup_path)
            migrated_bytes = migrated_env.encode("utf-8")
            if _read_regular_bytes(env_path) != original_env_bytes:
                raise OSError("environment file changed before atomic replacement")
            current_env_metadata = env_path.stat(follow_symlinks=False)
            if (
                current_env_metadata.st_uid,
                current_env_metadata.st_gid,
            ) != original_env_owner:
                raise OSError("environment file owner changed before atomic replacement")
            _atomic_write_bytes(
                env_path,
                migrated_bytes,
                owner=original_env_owner,
            )
            if _read_regular_bytes(env_path) != migrated_bytes:
                raise OSError("environment file verification failed")

    return report


def _write_report(path: Path, report: dict[str, Any]) -> None:
    _atomic_write_text(path, json.dumps(report, ensure_ascii=False, indent=2) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--backup-root", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        report = migrate(
            args.root,
            dry_run=args.dry_run,
            backup_root=args.backup_root,
        )
        _write_report(args.report, report)
        return 0
    except (OSError, ValueError) as exc:
        report = {
            "schema_version": 1,
            "mode": "dry-run" if args.dry_run else "apply",
            "status": "failed",
            "error": str(exc),
        }
        _write_report(args.report, report)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
