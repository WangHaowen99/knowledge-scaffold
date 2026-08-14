#!/usr/bin/env python3
"""Validate deployment config, deferring persisted state only before migrations."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union
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


def _bootstrap_password_is_strong(value: str) -> bool:
    if value != value.strip() or not 8 <= len(value) <= 128:
        return False
    if "admin" in value.lower():
        return False
    classes = (
        any(ch.islower() for ch in value),
        any(ch.isupper() for ch in value),
        any(ch.isdigit() for ch in value),
        any(not ch.isalnum() for ch in value),
    )
    return sum(classes) >= 3 and value.lower() not in {
        "123456",
        "12345678",
        "password",
        "password123",
        "qwerty",
        "qwerty123",
    }


def _read_json_document(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return raw if isinstance(raw, dict) else {}


ComposeEnvFiles = Union[Path, Sequence[Path]]
ComposeConfigLoader = Callable[
    [Path, ComposeEnvFiles],
    Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]],
]


def _compose_command_candidates() -> tuple[tuple[str, ...], ...]:
    candidates: list[tuple[str, ...]] = []
    preferred = os.environ.get("DOCKER_COMPOSE_CMD", "").strip()
    if preferred == "docker compose":
        candidates.append(("docker", "compose"))
    elif preferred == "docker-compose":
        candidates.append(("docker-compose",))

    for candidate in (("docker", "compose"), ("docker-compose",)):
        if candidate not in candidates:
            candidates.append(candidate)
    return tuple(candidates)


def _compose_project_directory(compose_file: Path, env_file: Path) -> Path:
    if env_file.parent.name == "backend":
        return env_file.parent.parent
    return compose_file.parent


def _merged_compose_env_values(env_files: Sequence[Path]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for env_file in env_files:
        merged.update(read_env(env_file))
    return merged


def _service_block(compose_text: str, service_name: str) -> str | None:
    match = re.search(
        rf"(?ms)^  {re.escape(service_name)}:\s*(?:#.*)?\n(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:\s*(?:#.*)?$|^\S|\Z)",
        compose_text,
    )
    return match.group("body") if match is not None else None


def _strip_optional_quotes(value: str) -> str:
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {"'", '"'}:
        return stripped[1:-1]
    return stripped


def _resolve_port_token(token: str, env_values: Mapping[str, str]) -> int | None:
    stripped = _strip_optional_quotes(token)
    if stripped.isdigit():
        return int(stripped)

    match = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-|-)([0-9]+)\}", stripped)
    if match is None:
        return None

    name, default = match.groups()
    resolved = env_values.get(name, default).strip()
    return int(resolved) if resolved.isdigit() else None


def _resolve_service_published_port(
    compose_text: str,
    service_name: str,
    target_port: int,
    env_values: Mapping[str, str],
) -> int | None:
    body = _service_block(compose_text, service_name)
    if body is None:
        return None

    in_ports = False
    ports_indent = 0
    for raw_line in body.splitlines():
        if not raw_line.strip():
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        stripped = raw_line.strip()
        if not in_ports:
            if stripped == "ports:":
                in_ports = True
                ports_indent = indent
            continue
        if indent <= ports_indent:
            break
        if not stripped.startswith("- "):
            continue
        entry = _strip_optional_quotes(stripped[2:].strip())
        match = re.fullmatch(r"(.+):([0-9]+)(?:/(?:tcp|udp))?", entry)
        if match is None:
            continue
        published_token, target_token = match.groups()
        if target_token != str(target_port):
            continue
        return _resolve_port_token(published_token, env_values)
    return None


def _fallback_release_port_config(
    compose_file: Path,
    env_files: Sequence[Path],
) -> tuple[dict[str, Any] | None, str | None]:
    if not compose_file.exists():
        return None, "compose file not found"

    compose_text = compose_file.read_text(encoding="utf-8")
    env_values = _merged_compose_env_values(env_files)
    services: dict[str, dict[str, list[dict[str, str]]]] = {}

    for service_name, target_port in (("backend", 5055), ("mcp", 7488), ("frontend", 80)):
        published_port = _resolve_service_published_port(
            compose_text,
            service_name,
            target_port,
            env_values,
        )
        if published_port is None:
            return None, f"release compose fallback could not resolve {service_name} host port"
        services[service_name] = {
            "ports": [{"target": target_port, "published": str(published_port)}],
        }

    return {"services": services}, None


def _load_compose_config_json(
    compose_file: Path,
    env_files: ComposeEnvFiles,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    resolved_env_files = (
        (env_files.resolve(),)
        if isinstance(env_files, Path)
        else tuple(path.resolve() for path in env_files)
    )
    if not resolved_env_files:
        return None, [{"type": "compose_config_unavailable", "reason": "no env file"}]
    project_dir = _compose_project_directory(compose_file.resolve(), resolved_env_files[0])
    process_env = os.environ.copy()
    process_env.setdefault("BACKEND_IMAGE", "preflight-backend:latest")
    process_env.setdefault("FRONTEND_IMAGE", "preflight-frontend:latest")
    process_env.setdefault("AUTH_SERVER_IMAGE", "preflight-auth-server:latest")
    process_env.setdefault("JIUWENBOX_IMAGE", "preflight-jiuwenbox:latest")

    last_failure: str | None = None
    for base_cmd in _compose_command_candidates():
        try:
            version_result = subprocess.run(
                [*base_cmd, "version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except OSError:
            continue
        if version_result.returncode != 0:
            last_failure = "compose version check failed"
            continue

        compose_args = [*base_cmd]
        for env_file in resolved_env_files:
            compose_args.extend(("--env-file", str(env_file)))
        compose_args.extend((
            "-f",
            str(compose_file.resolve()),
            "--project-directory",
            str(project_dir),
        ))
        try:
            result = subprocess.run(
                [*compose_args, "config", "--format", "json"],
                cwd=str(project_dir),
                env=process_env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except OSError:
            continue
        if result.returncode != 0:
            if base_cmd == ("docker-compose",):
                try:
                    legacy_result = subprocess.run(
                        [*compose_args, "config"],
                        cwd=str(project_dir),
                        env=process_env,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        check=False,
                    )
                except OSError:
                    legacy_result = None
                if legacy_result is not None and legacy_result.returncode == 0:
                    payload, parse_failure = _fallback_release_port_config(
                        compose_file,
                        resolved_env_files,
                    )
                    if payload is not None:
                        return payload, []
                    last_failure = parse_failure or "compose config fallback parse failed"
                    continue
            last_failure = (
                "compose config failed"
                if base_cmd == ("docker-compose",)
                else "compose config json failed"
            )
            continue
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            last_failure = "compose config returned invalid json"
            continue
        if not isinstance(payload, dict):
            last_failure = "compose config returned a non-object document"
            continue

        return payload, []

    return None, [{
        "type": "compose_config_unavailable",
        "detail": last_failure or "docker compose config --format json is unavailable",
    }]


def _published_host_port(
    compose_config: Mapping[str, Any] | None,
    service_name: str,
    target_port: int,
) -> int | None:
    services = compose_config.get("services") if isinstance(compose_config, Mapping) else None
    service = services.get(service_name) if isinstance(services, Mapping) else None
    ports = service.get("ports") if isinstance(service, Mapping) else None
    if not isinstance(ports, list):
        return None

    published_ports: set[int] = set()
    for port in ports:
        if not isinstance(port, Mapping):
            continue
        target = _positive_int(str(port.get("target") or ""))
        published = _positive_int(str(port.get("published") or ""))
        if target == target_port and published is not None and published <= 65535:
            published_ports.add(published)
    if len(published_ports) != 1:
        return None
    return next(iter(published_ports))


def _release_host_ports(
    compose_config: Mapping[str, Any] | None,
) -> dict[str, int | None]:
    return {
        "backend": _published_host_port(compose_config, "backend", 5055),
        "mcp": _published_host_port(compose_config, "mcp", 7488),
        "frontend": _published_host_port(compose_config, "frontend", 80),
    }


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
                    "reason": "missing" if "ontology_property" not in config else "blank",
                    "effect": "ontology_scope_disabled",
                })
    return errors, warnings, count


def validate_config(
    env_file: Path,
    runtime_root: Path,
    *,
    compose_file: Path,
    pre_migration: bool = False,
    compose_config_loader: ComposeConfigLoader | None = None,
    compose_env_files: Sequence[Path] | None = None,
) -> dict[str, Any]:
    env = read_env(env_file)
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    loader = compose_config_loader or _load_compose_config_json
    compose_config, compose_config_errors = loader(
        compose_file,
        tuple(compose_env_files or (env_file,)),
    )
    errors.extend(compose_config_errors)
    host_ports = _release_host_ports(compose_config)

    auth_url = env.get("AUTH_SERVER_URL", "").rstrip("/")
    issuer = env.get("AUTH_SERVER_ISSUER", "").rstrip("/")
    audience = env.get("AUTH_SERVER_AUDIENCE", "")
    admin_token = env.get("AUTH_SERVER_ADMIN_TOKEN", "")
    bootstrap_admin_password = env.get("AUTH_BOOTSTRAP_ADMIN_PASSWORD", "")
    bootstrap_user_store = env_file.parent.parent / ".onto-auth" / "users.json"
    if not _valid_http_url(auth_url):
        errors.append({"type": "invalid_auth_server_url"})
    # Keep issuer/audience optional for compatibility with the original
    # develop auth-server contract. RemoteAuthBackend falls back to the
    # auth-server URL and the default audience when these are unset.
    if issuer and not _valid_http_url(issuer):
        errors.append({"type": "invalid_auth_server_issuer"})
    if len(admin_token) < 16:
        errors.append({"type": "weak_or_missing_auth_admin_token", "minimum_length": 16})
    if not bootstrap_admin_password and not bootstrap_user_store.is_file():
        errors.append({"type": "weak_or_missing_bootstrap_admin_password"})
    elif bootstrap_admin_password and not _bootstrap_password_is_strong(
        bootstrap_admin_password
    ):
        errors.append({"type": "weak_or_missing_bootstrap_admin_password"})
    if bootstrap_admin_password and bootstrap_admin_password == admin_token:
        errors.append({"type": "bootstrap_password_reuses_auth_admin_token"})

    mcp_secret = env.get("MCP_TOKEN_SECRET", "")
    if len(mcp_secret) < 32:
        errors.append({"type": "weak_or_missing_mcp_token_secret", "minimum_length": 32})
    elif mcp_secret == admin_token:
        errors.append({"type": "mcp_secret_reuses_auth_admin_token"})
    if bootstrap_admin_password and bootstrap_admin_password == mcp_secret:
        errors.append({"type": "bootstrap_password_reuses_mcp_token_secret"})
    mcp_port = _positive_int(env.get("MCP_SERVER_PORT", "7488"))
    if mcp_port is None or mcp_port > 65535:
        errors.append({"type": "invalid_mcp_server_port"})
    elif mcp_port != 7488:
        errors.append({"type": "mcp_container_port_must_be_7488", "port": mcp_port})

    # User-count limits are retired. Keep the report shape for consumers of
    # the preflight artifact, but ignore legacy env values entirely.
    user_limit = 0
    auth_user_limit = 0

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
        "host_ports": host_ports,
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
    parser.add_argument("--compose-env-file", type=Path, action="append")
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
        compose_env_files=args.compose_env_file,
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
