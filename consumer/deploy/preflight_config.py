#!/usr/bin/env python3
"""Validate deployment config, deferring persisted state only before migrations."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = re.sub(r"\s+#.*$", "", value.strip())
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _valid_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _positive_int(value: str) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _read_json_document(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return raw if isinstance(raw, dict) else {}


def _relative_path(value: str) -> bool:
    if not value:
        return False
    expanded = value.strip()
    if expanded.startswith("/"):
        return False
    if re.match(r"^[A-Za-z]:[\\/]", expanded):
        return False
    if expanded.startswith("\\\\"):
        return False
    return True


def _release_storage_findings(
    env: dict[str, str],
    runtime_root: Path,
    compose_file: Path,
    *,
    pre_migration: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    compose_text = compose_file.read_text(encoding="utf-8") if compose_file.exists() else ""
    runtime_container_root = "/app/.onto-runtime"
    canonical_paths = {
        "chat_storage_dir": "/app/.onto-runtime/data/chats",
        "execution_storage_dir": "/app/.onto-runtime/data/executions",
    }
    required_compose_fragments = [
        "./.onto-runtime:/app/.onto-runtime",
        "CHAT_STORAGE_DIR=/app/.onto-runtime/data/chats",
        "EXECUTION_STORAGE_DIR=/app/.onto-runtime/data/executions",
    ]
    for fragment in required_compose_fragments:
        if fragment not in compose_text:
            errors.append({
                "type": "missing_persistent_storage_compose_contract",
                "fragment": fragment,
            })

    storage_config = _read_json_document(runtime_root / "config" / "system_config.json").get("storage")
    if not isinstance(storage_config, dict):
        storage_config = {}

    for field, canonical_path in canonical_paths.items():
        env_key = "CHAT_STORAGE_DIR" if field == "chat_storage_dir" else "EXECUTION_STORAGE_DIR"
        sources = [
            ("env", env.get(env_key)),
            ("system_config", storage_config.get(field)),
        ]
        for source_name, raw_value in sources:
            if raw_value is None:
                continue
            normalized = str(raw_value).strip()
            if not normalized:
                target = warnings if pre_migration else errors
                target.append({"type": "empty_storage_path", "source": source_name, "field": field})
                continue
            if _relative_path(normalized):
                target = warnings if pre_migration else errors
                target.append({
                    "type": "relative_storage_path",
                    "source": source_name,
                    "field": field,
                    "value": normalized,
                    "canonical": canonical_path,
                })
                continue
            if not normalized.startswith(runtime_container_root + "/"):
                target = warnings if pre_migration else errors
                target.append({
                    "type": "storage_path_outside_runtime_root",
                    "source": source_name,
                    "field": field,
                    "value": normalized,
                    "runtime_root": runtime_container_root,
                })

    return errors, warnings, {
        "runtime_root": runtime_container_root,
        "canonical_paths": canonical_paths,
    }


def _graph_data_source_findings(
    runtime_root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    count = 0
    workspaces = runtime_root / "workspaces"
    if not workspaces.exists():
        return errors, warnings, count
    for path in sorted(workspaces.glob("*/ontology-data-sources.json")):
        workspace_id = path.parent.name
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append({
                "workspace_id": workspace_id,
                "type": "invalid_ontology_data_sources",
                "detail": str(exc),
            })
            continue
        sources = payload.get("sources") if isinstance(payload, dict) else None
        if not isinstance(sources, dict):
            errors.append({"workspace_id": workspace_id, "type": "invalid_ontology_data_sources"})
            continue
        for ontology_id, source in sources.items():
            if not isinstance(source, dict):
                continue
            mode = str(source.get("mode") or "direct_provider").strip()
            if mode != "direct_provider":
                errors.append({
                    "workspace_id": workspace_id,
                    "ontology_id": str(ontology_id),
                    "type": "legacy_data_source_mode",
                    "mode": mode,
                })
                continue
            provider = str(source.get("provider") or "").strip().lower()
            if provider not in {"neo4j", "hugegraph"}:
                continue
            count += 1
            resolved_ontology_id = str(ontology_id or "<missing>")
            config = source.get("config")
            if not isinstance(config, dict):
                errors.append({
                    "workspace_id": workspace_id,
                    "ontology_id": resolved_ontology_id,
                    "type": "invalid_graph_config",
                })
                continue
            required = ["uri", "username", "password"] if provider == "neo4j" else ["url"]
            for field in required:
                if not str(config.get(field) or "").strip():
                    errors.append({
                        "workspace_id": workspace_id,
                        "ontology_id": resolved_ontology_id,
                        "provider": provider,
                        "type": "missing_graph_field",
                        "field": field,
                    })
            secret_fields = ["password"] if provider == "neo4j" else ["password", "token"]
            for field in secret_fields:
                secret = str(config.get(field) or "").strip()
                if secret and not secret.startswith("enc:v1:"):
                    errors.append({
                        "workspace_id": workspace_id,
                        "ontology_id": resolved_ontology_id,
                        "provider": provider,
                        "type": "plaintext_graph_secret",
                        "field": field,
                    })
            if not str(config.get("workspace_property") or "").strip():
                errors.append({
                    "workspace_id": workspace_id,
                    "ontology_id": resolved_ontology_id,
                    "provider": provider,
                    "type": "missing_graph_scope_field",
                    "field": "workspace_property",
                })
            if not str(config.get("ontology_property") or "").strip():
                warnings.append({
                    "workspace_id": workspace_id,
                    "ontology_id": resolved_ontology_id,
                    "provider": provider,
                    "type": "missing_optional_graph_scope_field",
                    "field": "ontology_property",
                })
    return errors, warnings, count


def validate_config(
    env_file: Path,
    runtime_root: Path,
    *,
    compose_file: Path,
    pre_migration: bool = False,
) -> dict[str, Any]:
    env = read_env(env_file)
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    auth_url = env.get("AUTH_SERVER_URL", "").rstrip("/")
    issuer = env.get("AUTH_SERVER_ISSUER", "").rstrip("/")
    audience = env.get("AUTH_SERVER_AUDIENCE", "")
    admin_token = env.get("AUTH_SERVER_ADMIN_TOKEN", "")
    if not _valid_http_url(auth_url):
        errors.append({"type": "invalid_auth_server_url"})
    # Keep issuer/audience optional for compatibility with the original
    # develop auth-server contract. RemoteAuthBackend falls back to the
    # auth-server URL and the default audience when these are unset.
    if issuer and not _valid_http_url(issuer):
        errors.append({"type": "invalid_auth_server_issuer"})
    if len(admin_token) < 16:
        errors.append({"type": "weak_or_missing_auth_admin_token", "minimum_length": 16})

    mcp_secret = env.get("MCP_TOKEN_SECRET", "")
    if len(mcp_secret) < 32:
        errors.append({"type": "weak_or_missing_mcp_token_secret", "minimum_length": 32})
    elif mcp_secret == admin_token:
        errors.append({"type": "mcp_secret_reuses_auth_admin_token"})
    mcp_port = _positive_int(env.get("MCP_SERVER_PORT", "7488"))
    if mcp_port is None or mcp_port > 65535:
        errors.append({"type": "invalid_mcp_server_port"})
    elif mcp_port != 7488:
        errors.append({"type": "mcp_container_port_must_be_7488", "port": mcp_port})

    user_limit = _positive_int(env.get("USER_MAX_COUNT", ""))
    auth_user_limit = _positive_int(env.get("AUTH_USER_MAX_COUNT", ""))
    if user_limit is None or auth_user_limit is None:
        errors.append({"type": "user_limits_must_be_explicit"})
    elif user_limit != auth_user_limit:
        errors.append({
            "type": "user_limit_mismatch",
            "user_max_count": user_limit,
            "auth_user_max_count": auth_user_limit,
        })

    storage_errors, storage_warnings, storage_contract = _release_storage_findings(
        env,
        runtime_root,
        compose_file,
        pre_migration=pre_migration,
    )
    errors.extend(storage_errors)
    warnings.extend(storage_warnings)

    if pre_migration:
        graph_errors: list[dict[str, Any]] = []
        graph_warnings: list[dict[str, Any]] = []
        graph_count: int | None = None
    else:
        graph_errors, graph_warnings, graph_count = _graph_data_source_findings(runtime_root)
    errors.extend(graph_errors)
    warnings.extend(graph_warnings)
    return {
        "schema_version": 1,
        "ok": not errors,
        "phase": "pre_migration" if pre_migration else "strict",
        "persisted_data_validation": "deferred" if pre_migration else "strict",
        "auth": {
            "url": auth_url,
            "issuer": issuer,
            "audience": audience,
            "admin_token_configured": bool(admin_token),
        },
        "mcp": {
            "port": mcp_port,
            "token_secret_configured": bool(mcp_secret),
        },
        "limits": {
            "user_max_count": user_limit,
            "auth_user_max_count": auth_user_limit,
        },
        "storage": storage_contract,
        "graph_data_source_count": graph_count,
        "errors": errors,
        "warnings": warnings,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--compose-file", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--pre-migration", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    report = validate_config(
        args.env_file,
        args.runtime_root,
        compose_file=args.compose_file,
        pre_migration=args.pre_migration,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered, encoding="utf-8")
        args.report.chmod(0o600)
    print(rendered, end="")
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
