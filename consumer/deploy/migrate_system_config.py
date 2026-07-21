#!/usr/bin/env python3
"""Remove the retired context_prune_tools setting from persisted config."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any


LEGACY_JSON_FIELD = "context_prune_tools"
LEGACY_ENV_NAME = "AGENT_CONTEXT_PRUNE_TOOLS"


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


def migrate(root: Path, *, dry_run: bool) -> dict[str, Any]:
    root = root.resolve()
    config_path = root / ".onto-runtime" / "config" / "system_config.json"
    env_path = root / "backend" / ".env"
    report: dict[str, Any] = {
        "schema_version": 1,
        "mode": "dry-run" if dry_run else "apply",
        "status": "ok",
        "changed": False,
        "system_config": {"path": str(config_path), "exists": config_path.exists(), "removed": False},
        "backend_env": {"path": str(env_path), "exists": env_path.exists(), "removed_count": 0},
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
        original_env = env_path.read_text(encoding="utf-8")
        cleaned_env, removed_count = _clean_env_lines(original_env)
        report["backend_env"]["removed_count"] = removed_count
        report["changed"] = bool(report["changed"] or removed_count)
        if removed_count and not dry_run:
            _atomic_write_text(env_path, cleaned_env)

    return report


def _write_report(path: Path, report: dict[str, Any]) -> None:
    _atomic_write_text(path, json.dumps(report, ensure_ascii=False, indent=2) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        report = migrate(args.root, dry_run=args.dry_run)
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
