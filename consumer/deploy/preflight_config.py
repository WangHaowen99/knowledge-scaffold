#!/usr/bin/env python3
"""Validate deployment config, deferring persisted state only before migrations."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping
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


ComposeConfigLoader = Callable[[Path, Path], tuple[dict[str, Any] | None, list[dict[str, Any]]]]
_RELEASE_SANDBOX_EXPECTED = {
    "SANDBOX_ENABLED": "true",
    "SANDBOX_FALLBACK_LOCAL": "false",
    "SANDBOX_SECRET_ALLOW_PLAIN_HTTP": "false",
}


def _compose_command_candidates() -> tuple[tuple[str, ...], ...]:
    return (("docker", "compose"),)


def _compose_project_directory(compose_file: Path, env_file: Path) -> Path:
    if env_file.parent.name == "backend":
        return env_file.parent.parent
    return compose_file.parent


def _load_compose_config_json(
    compose_file: Path,
    env_file: Path,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    project_dir = _compose_project_directory(compose_file.resolve(), env_file.resolve())
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

        compose_args = [
            *base_cmd,
            "--env-file",
            str(env_file.resolve()),
            "-f",
            str(compose_file.resolve()),
            "--project-directory",
            str(project_dir),
        ]
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
            last_failure = "compose config json failed"
            continue
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            last_failure = "compose config returned invalid json"
            continue
        if not isinstance(payload, dict):
            last_failure = "compose config returned a non-object document"
            continue

        try:
            source_result = subprocess.run(
                [
                    *compose_args,
                    "config",
                    "--no-interpolate",
                    "--no-env-resolution",
                    "--format",
                    "json",
                ],
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
        if source_result.returncode != 0:
            last_failure = "compose config source inspection failed"
            continue
        try:
            source_payload = json.loads(source_result.stdout or "{}")
        except json.JSONDecodeError:
            last_failure = "compose config source inspection returned invalid json"
            continue
        if not isinstance(source_payload, dict):
            last_failure = "compose config source inspection returned a non-object document"
            continue
        return payload, _release_sandbox_structured_source_errors(source_payload)

    return None, [{
        "type": "compose_config_unavailable",
        "detail": last_failure or "docker compose config --format json is unavailable",
    }]


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


def _normalize_bool_token(value: str | None) -> str | None:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return None
    if normalized in {"1", "true", "yes", "y", "on"}:
        return "true"
    if normalized in {"0", "false", "no", "n", "off"}:
        return "false"
    return None


def _duplicate_release_sandbox_source_errors(compose_text: str) -> list[dict[str, Any]]:
    backend_match = re.search(
        r"(?ms)^(?P<indent>[ ]+)backend:\s*(?:#.*)?\n(?P<body>.*?)(?=^(?P=indent)\S[^\n]*:\s*(?:#.*)?$|\Z)",
        compose_text,
    )
    if backend_match is None:
        return []
    backend_body = "\n".join(
        line.split("#", 1)[0] for line in backend_match.group("body").splitlines()
    )
    errors: list[dict[str, Any]] = []
    for setting, expected_value in _RELEASE_SANDBOX_EXPECTED.items():
        if len(re.findall(rf"\b{re.escape(setting)}\b", backend_body)) <= 1:
            continue
        errors.append({
            "type": "duplicate_release_sandbox_setting",
            "setting": setting,
            "expected": expected_value,
            "source": "compose",
        })
    return errors


def _compose_backend_environment(
    compose_config: Mapping[str, Any] | None,
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    if not isinstance(compose_config, Mapping):
        return {}, [{"type": "compose_config_unavailable"}]

    services = compose_config.get("services")
    if not isinstance(services, Mapping):
        return {}, [{"type": "missing_compose_services"}]

    backend = services.get("backend")
    if not isinstance(backend, Mapping):
        return {}, [{"type": "missing_compose_backend_service"}]

    environment = backend.get("environment")
    if environment is None:
        return {}, []

    errors: list[dict[str, Any]] = []
    values: dict[str, str] = {}
    if isinstance(environment, Mapping):
        for key, value in environment.items():
            name = str(key).strip()
            if not name:
                continue
            values[name] = "" if value is None else str(value)
        return values, errors

    if isinstance(environment, list):
        for item in environment:
            if isinstance(item, str):
                if "=" not in item:
                    errors.append({
                        "type": "unsupported_release_sandbox_setting_source",
                        "source": "compose_config",
                        "detail": "bare environment variable reference is not allowed",
                    })
                    continue
                key, value = item.split("=", 1)
                name = key.strip()
                if name:
                    values[name] = value
                continue
            if isinstance(item, Mapping):
                for key, value in item.items():
                    name = str(key).strip()
                    if name:
                        values[name] = "" if value is None else str(value)
                continue
            errors.append({
                "type": "unsupported_release_sandbox_environment",
                "source": "compose_config",
            })
        return values, errors

    return {}, [{
        "type": "unsupported_release_sandbox_environment",
        "source": "compose_config",
    }]


def _release_sandbox_structured_source_errors(
    compose_config: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    source_env, errors = _compose_backend_environment(compose_config)
    for setting, expected_value in _RELEASE_SANDBOX_EXPECTED.items():
        raw_value = source_env.get(setting)
        stripped = str(raw_value or "").strip()
        if not stripped or "${" in stripped:
            errors.append({
                "type": "unsupported_release_sandbox_setting_source",
                "setting": setting,
                "expected": expected_value,
                "source": "compose_config_no_interpolate",
                "detail": "sandbox settings must be literal backend environment values",
            })
    return errors


def _validate_release_sandbox_env(
    *,
    compose_config: Mapping[str, Any] | None,
    compose_config_errors: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, str | None]]:
    errors: list[dict[str, Any]] = []
    effective: dict[str, str | None] = {
        setting: None for setting in _RELEASE_SANDBOX_EXPECTED
    }
    if compose_config_errors:
        errors.extend(compose_config_errors)
        return errors, effective

    backend_env, backend_env_errors = _compose_backend_environment(compose_config)
    errors.extend(backend_env_errors)
    for setting, expected_value in _RELEASE_SANDBOX_EXPECTED.items():
        raw_value = backend_env.get(setting)
        normalized = _normalize_bool_token(raw_value)
        effective[setting] = normalized
        if raw_value is None or not str(raw_value).strip():
            errors.append({
                "type": "missing_release_sandbox_setting",
                "setting": setting,
                "expected": expected_value,
                "source": "compose_config",
            })
            continue
        if normalized is None:
            errors.append({
                "type": "invalid_release_sandbox_setting",
                "setting": setting,
                "expected": expected_value,
                "actual": str(raw_value),
                "source": "compose_config",
            })
            continue
        if normalized != expected_value:
            errors.append({
                "type": "invalid_release_sandbox_setting",
                "setting": setting,
                "expected": expected_value,
                "actual": normalized,
                "source": "compose_config",
            })
    return errors, effective


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


_SANDBOX_SUPPORTED_SCRIPT_PROVIDERS = {"mindsdb", "nl2sql_http"}
_FRONTMATTER_BOUND_OPERATION = re.compile(
    r"^\s*(?:bound_operation|operation_key)\s*:\s*(?![\"']?\s*[\"']?\s*$).+\s*$",
    re.MULTILINE,
)
_FRONTMATTER_PYTHON_ENTRYPOINT = re.compile(
    r"^\s*(?:entrypoint|path)\s*:\s*[\"']?[^\n#\"']+\.py[\"']?\s*(?:#.*)?$",
    re.MULTILINE | re.IGNORECASE,
)


def _ontology_has_bound_python_script(ontology_root: Path) -> bool:
    script_dirs = [ontology_root / "scripts"]
    versions_dir = ontology_root / "versions"
    if versions_dir.is_dir():
        script_dirs.extend(path / "scripts" for path in versions_dir.iterdir() if path.is_dir())
    for scripts_dir in script_dirs:
        if not scripts_dir.is_dir():
            continue
        for descriptor in scripts_dir.glob("*.md"):
            try:
                descriptor_text = descriptor.read_text(encoding="utf-8")
            except OSError:
                continue
            if (
                _FRONTMATTER_BOUND_OPERATION.search(descriptor_text)
                and _FRONTMATTER_PYTHON_ENTRYPOINT.search(descriptor_text)
            ):
                return True
    return False


def _sandbox_script_provider_findings(runtime_root: Path) -> tuple[list[dict[str, Any]], int]:
    errors: list[dict[str, Any]] = []
    count = 0
    workspaces = runtime_root / "workspaces"
    if not workspaces.exists():
        return errors, count
    for path in sorted(workspaces.glob("*/ontology-data-sources.json")):
        workspace_id = path.parent.name
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        sources = payload.get("sources") if isinstance(payload, dict) else None
        if not isinstance(sources, dict):
            continue
        for ontology_id, source in sources.items():
            if not isinstance(source, dict):
                continue
            mode = str(source.get("mode") or "direct_provider").strip()
            provider = str(source.get("provider") or "").strip().lower()
            if mode != "direct_provider" or not provider:
                continue
            ontology_root = path.parent / "ontologies" / str(ontology_id)
            if provider in _SANDBOX_SUPPORTED_SCRIPT_PROVIDERS:
                continue
            if not _ontology_has_bound_python_script(ontology_root):
                continue
            count += 1
            errors.append({
                "workspace_id": workspace_id,
                "ontology_id": str(ontology_id),
                "provider": provider,
                "type": "unsupported_sandbox_script_provider",
                "supported_providers": sorted(_SANDBOX_SUPPORTED_SCRIPT_PROVIDERS),
                "detail": "migrate the ontology script provider before enabling release sandbox execution",
            })
    return errors, count


def validate_config(
    env_file: Path,
    runtime_root: Path,
    *,
    compose_file: Path,
    pre_migration: bool = False,
    compose_config_loader: ComposeConfigLoader | None = None,
) -> dict[str, Any]:
    env = read_env(env_file)
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    compose_text = compose_file.read_text(encoding="utf-8") if compose_file.exists() else ""
    errors.extend(_duplicate_release_sandbox_source_errors(compose_text))
    loader = compose_config_loader or _load_compose_config_json
    compose_config, compose_config_errors = loader(compose_file, env_file)
    sandbox_errors, sandbox_effective = _validate_release_sandbox_env(
        compose_config=compose_config,
        compose_config_errors=compose_config_errors,
    )
    errors.extend(sandbox_errors)

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
    sandbox_provider_errors, sandbox_incompatible_count = _sandbox_script_provider_findings(
        runtime_root
    )
    errors.extend(sandbox_provider_errors)
    return {
        "schema_version": 1,
        "ok": not errors,
        "phase": "pre_migration" if pre_migration else "strict",
        "persisted_data_validation": "deferred" if pre_migration else "strict",
        "sandbox": {
            "enabled": sandbox_effective.get("SANDBOX_ENABLED"),
            "fallback_local": sandbox_effective.get("SANDBOX_FALLBACK_LOCAL"),
            "secret_allow_plain_http": sandbox_effective.get(
                "SANDBOX_SECRET_ALLOW_PLAIN_HTTP"
            ),
        },
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
        "sandbox_incompatible_script_provider_count": sandbox_incompatible_count,
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
