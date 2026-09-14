"""Run-scoped business-data proxy setup for sandbox scripts."""

from __future__ import annotations

import hashlib
import ipaddress
import re
import socket
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlparse

import requests
from shared_capabilities.business_data import AuthSpec, EffectiveConnection
from utils.logging import get_logger

from .client import JiuwenboxClient
from .config import SandboxConfig, load_sandbox_config
from .errors import SandboxUnavailableError, SandboxUnsupportedProviderError

if TYPE_CHECKING:
    from agent.tools.business_data.resolver import ResolvedBusinessDataConnection


logger = get_logger("sandbox.data_proxy")

_manager_lock = threading.Lock()
_manager_instance: "DataConnectionSecretManager | None" = None


def resolve_business_data_connection(*, workspace_id: str, ontology_id: str):
    """Resolve lazily to avoid importing the eager agent.tools package at startup."""
    from agent.tools.business_data.resolver import resolve_business_data_connection as resolve

    return resolve(workspace_id=workspace_id, ontology_id=ontology_id)


@dataclass(frozen=True)
class PreparedDataConnection:
    env: dict[str, str]
    data_proxy_url: str | None


@dataclass(frozen=True)
class _ProxyRoute:
    connection_id: str
    route_name: str
    route_prefix: str
    proxy_url: str
    safe_env: dict[str, str]


class DataConnectionSecretManager:
    """Prepare safe script env and proxy routes without exposing real secrets."""

    def __init__(self, config: SandboxConfig) -> None:
        self._config = config
        self._client = JiuwenboxClient(
            config.jiuwenbox_url,
            timeout=config.client_timeout_seconds,
        )
        self._lock = threading.Lock()
        self._run_scope: dict[str, tuple[str | None, str | None]] = {}
        self._routes_by_run: dict[str, dict[str, _ProxyRoute]] = {}
        self._route_creation_locks: dict[tuple[str, str], threading.Lock] = {}

    def set_client_for_test(self, client: object) -> None:
        self._client = client

    def client_for_test(self) -> object:
        return self._client

    def has_routes_for_run(self, run_id: str) -> bool:
        with self._lock:
            return bool(self._routes_by_run.get(str(run_id or "").strip()))

    def prepare(
        self,
        *,
        run_id: str,
        workspace_id: str | None,
        ontology_id: str | None,
    ) -> PreparedDataConnection:
        run_key = str(run_id or "").strip()
        if not run_key or not workspace_id or not ontology_id:
            return PreparedDataConnection(env={}, data_proxy_url=None)

        scope = (str(workspace_id or "").strip() or None, str(ontology_id or "").strip() or None)
        with self._lock:
            existing_scope = self._run_scope.get(run_key)
            if existing_scope is not None and existing_scope != scope:
                raise SandboxUnavailableError("sandbox run cannot be reused across workspace or ontology")
            self._run_scope[run_key] = scope

        from agent.tools.business_data.resolver import BusinessDataConnectionError

        try:
            connection = resolve_business_data_connection(
                workspace_id=workspace_id,
                ontology_id=ontology_id,
            )
        except BusinessDataConnectionError:
            return PreparedDataConnection(env={}, data_proxy_url=None)

        route_key = (run_key, connection.id)
        with self._lock:
            route = self._routes_by_run.get(run_key, {}).get(connection.id)
            if route is not None:
                return PreparedDataConnection(
                    env=dict(route.safe_env),
                    data_proxy_url=route.proxy_url,
                )
            creation_lock = self._route_creation_locks.setdefault(
                route_key,
                threading.Lock(),
            )

        try:
            with creation_lock:
                with self._lock:
                    route = self._routes_by_run.get(run_key, {}).get(connection.id)
                if route is None:
                    route = self._create_route(run_id=run_key, connection=connection)
                    with self._lock:
                        self._routes_by_run.setdefault(run_key, {})[connection.id] = route
        finally:
            with self._lock:
                if self._route_creation_locks.get(route_key) is creation_lock:
                    self._route_creation_locks.pop(route_key, None)

        return PreparedDataConnection(
            env=dict(route.safe_env),
            data_proxy_url=route.proxy_url,
        )

    def release(self, run_id: str) -> None:
        run_key = str(run_id or "").strip()
        if not run_key:
            return
        with self._lock:
            self._run_scope.pop(run_key, None)
            routes = list(self._routes_by_run.pop(run_key, {}).values())
        for route in routes:
            try:
                self._client.delete_proxy(route.route_name)
                logger.info(
                    "Sandbox data proxy released: run_id=%s route=%s connection_id=%s",
                    run_key,
                    route.route_name,
                    route.connection_id,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to release sandbox data proxy: run_id=%s route=%s error=%s",
                    run_key,
                    route.route_name,
                    exc,
                )

    def _create_route(
        self,
        *,
        run_id: str,
        connection: ResolvedBusinessDataConnection,
    ) -> _ProxyRoute:
        from agent.tools.business_data.script_env import (
            SCRIPT_BUSINESS_DATA_CONNECTION_ENV,
            serialize_script_business_data_connection,
        )

        route_prefix = _route_prefix(run_id, connection.id)
        route_name = _route_name_for_path_prefix(route_prefix)
        proxy_url = sandbox_reachable_url(
            urljoin(
                self._config.data_proxy_base_url.rstrip("/") + "/",
                route_prefix.lstrip("/"),
            )
        )
        target_endpoint, api_key, safe_connection = _safe_provider_config(
            connection,
            proxy_url=proxy_url,
            sandbox_config=self._config,
        )

        self._client.create_proxy(
            path_prefix=route_prefix,
            target_endpoint=target_endpoint,
            api_key=api_key,
            skip_cert_verify=bool(connection.config.get("skip_cert_verify", False)),
        )
        try:
            self._client.start_proxy(route_name)
        except Exception as exc:
            try:
                self._client.delete_proxy(route_name)
                logger.info(
                    "Rolled back sandbox data proxy after start failure: run_id=%s route=%s connection_id=%s",
                    run_id,
                    route_name,
                    connection.id,
                )
            except Exception as cleanup_exc:
                logger.warning(
                    "Failed to roll back sandbox data proxy after start failure: run_id=%s route=%s connection_id=%s"
                    " start_error=%s rollback_error=%s",
                    run_id,
                    route_name,
                    connection.id,
                    exc,
                    cleanup_exc,
                )
            raise

        safe_env = {
            SCRIPT_BUSINESS_DATA_CONNECTION_ENV: serialize_script_business_data_connection(
                connection=safe_connection,
            )
        }
        logger.info(
            "Sandbox data proxy prepared: run_id=%s route=%s provider=%s connection_id=%s",
            run_id,
            route_name,
            connection.provider,
            connection.id,
        )
        return _ProxyRoute(
            connection_id=connection.id,
            route_name=route_name,
            route_prefix=route_prefix,
            proxy_url=proxy_url,
            safe_env=safe_env,
        )


def get_data_connection_secret_manager() -> DataConnectionSecretManager:
    global _manager_instance
    with _manager_lock:
        if _manager_instance is None:
            _manager_instance = DataConnectionSecretManager(load_sandbox_config())
        return _manager_instance


def _route_prefix(run_id: str, connection_id: str) -> str:
    run_part = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:12]
    conn_part = hashlib.sha256(connection_id.encode("utf-8")).hexdigest()[:12]
    # jiuwenbox proxy routes are single-level only (no internal slashes).
    return f"/onto-{run_part}-{conn_part}"


def _route_name_for_path_prefix(path_prefix: str) -> str:
    name = path_prefix.strip("/").replace("/", "-") or "default"
    return re.sub(r"[^a-zA-Z0-9_-]", "-", name)


def sandbox_reachable_url(url: str) -> str:
    """Resolve and pin a sandbox endpoint to one concrete IP address.

    Resolution is deliberately fail-closed: returning the hostname would let
    the sandbox policy resolve it again after the trusted-host check.  When a
    name has both address families, the stable ordering prefers IPv4 and then
    the numerically lowest address; IPv6-only names remain fully supported.
    """
    normalized = str(url or "").strip()
    if not normalized:
        return normalized
    has_explicit_scheme = "://" in normalized
    parsed = urlparse(normalized if has_explicit_scheme else f"//{normalized}")
    host = (parsed.hostname or "").strip()
    if not host:
        return normalized
    try:
        ipaddress.ip_address(host)
        return normalized
    except ValueError:
        pass
    port = parsed.port
    try:
        address_info = socket.getaddrinfo(
            host,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise SandboxUnavailableError("sandbox network endpoint host could not be resolved") from exc
    addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    for family, _type, _protocol, _canonical_name, socket_address in address_info:
        if family not in {socket.AF_INET, socket.AF_INET6} or not socket_address:
            continue
        try:
            addresses.add(ipaddress.ip_address(socket_address[0]))
        except ValueError:
            continue
    if not addresses:
        raise SandboxUnavailableError("sandbox network endpoint host did not resolve to an IP address")
    address = sorted(addresses, key=lambda item: (item.version, item.packed))[0]
    resolved = address.compressed
    address_netloc = "[{}]".format(resolved) if address.version == 6 else resolved
    netloc = f"{address_netloc}:{port}" if port else address_netloc
    resolved_url = parsed._replace(netloc=netloc).geturl()
    return resolved_url if has_explicit_scheme else resolved_url.removeprefix("//")


def _normalize_endpoint(url: Any) -> str:
    endpoint = str(url or "").strip().rstrip("/")
    if not endpoint:
        raise SandboxUnavailableError("business data connection url is not configured")
    return endpoint


def _safe_provider_config(
    connection: ResolvedBusinessDataConnection,
    *,
    proxy_url: str,
    sandbox_config: SandboxConfig,
) -> tuple[str, str, EffectiveConnection]:
    provider = str(connection.provider or "").strip().lower()
    config = dict(connection.config or {})
    target_endpoint = _normalize_endpoint(config.get("url"))

    if provider == "nl2sql_http":
        token = str(config.get("token") or "").strip()
        _validate_secret_transport(
            target_endpoint=target_endpoint,
            secret_value=token,
            connection=connection,
            config=sandbox_config,
        )
        safe_token = _placeholder_token(connection.id, "token") if token else ""
        return (
            target_endpoint,
            token,
            EffectiveConnection(
                version=1,
                provider=provider,
                endpoint=proxy_url,
                auth=AuthSpec(type="bearer", token=safe_token) if safe_token else AuthSpec(type="none"),
                options={
                    "scene": str(config.get("scene") or "").strip(),
                    "timeout_seconds": int(config.get("timeout_seconds") or 600),
                    "max_retries": int(config.get("max_retries") or 3),
                },
            ),
        )

    if provider == "mindsdb":
        requires_auth = bool(config.get("requires_auth", False))
        auth_token = ""
        if requires_auth:
            password = str(config.get("password") or "").strip()
            if not password:
                raise SandboxUnavailableError("MindsDB authentication is enabled but password is not configured")
            _validate_secret_transport(
                target_endpoint=target_endpoint,
                secret_value=password,
                connection=connection,
                config=sandbox_config,
            )
            auth_token = _login_mindsdb(
                target_endpoint=target_endpoint,
                login=str(config.get("login") or "mindsdb").strip() or "mindsdb",
                password=password,
                timeout_seconds=int(config.get("timeout_seconds") or 600),
                skip_cert_verify=bool(config.get("skip_cert_verify", False)),
            )
        safe_token = _placeholder_token(connection.id, "mindsdb_auth_token") if auth_token else ""
        return (
            target_endpoint,
            auth_token,
            EffectiveConnection(
                version=1,
                provider=provider,
                endpoint=proxy_url,
                auth=AuthSpec(type="bearer", token=safe_token) if safe_token else AuthSpec(type="none"),
                options={
                    "default_database": config.get("default_database") or None,
                    "default_agent_name": config.get("default_agent_name") or "my_agent",
                    "project_name": config.get("project_name") or "mindsdb",
                    "timeout_seconds": int(config.get("timeout_seconds") or 600),
                    "max_retries": int(config.get("max_retries") or 3),
                },
            ),
        )

    raise SandboxUnsupportedProviderError(provider)


def _placeholder_token(connection_id: str, field_name: str) -> str:
    digest = hashlib.sha256(f"{connection_id}:{field_name}".encode("utf-8")).hexdigest()
    return f"onto-placeholder-{digest[:24]}"


def _login_mindsdb(
    *,
    target_endpoint: str,
    login: str,
    password: str,
    timeout_seconds: int,
    skip_cert_verify: bool,
) -> str:
    """Exchange MindsDB credentials for a run-scoped proxy token on the host."""
    login_timeout = min(max(1, int(timeout_seconds)), 10)
    response: requests.Response | None = None
    try:
        response = requests.post(
            f"{target_endpoint}/api/login",
            json={"username": login, "password": password},
            timeout=login_timeout,
            verify=not skip_cert_verify,
        )
    except requests.exceptions.RequestException as exc:
        raise SandboxUnavailableError("MindsDB authentication request failed") from exc

    try:
        if response.status_code != 200:
            raise SandboxUnavailableError("MindsDB authentication failed (HTTP {})".format(response.status_code))
        try:
            payload = response.json()
        except ValueError as exc:
            raise SandboxUnavailableError("MindsDB authentication returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise SandboxUnavailableError("MindsDB authentication returned an invalid payload")
        token = str(payload.get("token") or payload.get("access_token") or "").strip()
        if not token:
            raise SandboxUnavailableError("MindsDB authentication succeeded without a token")
        return token
    finally:
        response.close()


def _validate_secret_transport(
    *,
    target_endpoint: str,
    secret_value: str,
    connection: ResolvedBusinessDataConnection,
    config: SandboxConfig,
) -> None:
    if not secret_value:
        return
    parsed = urlparse(target_endpoint if "://" in target_endpoint else f"https://{target_endpoint}")
    if parsed.scheme.lower() != "http":
        return
    if config.secret_allow_plain_http:
        logger.warning(
            "Sandbox data proxy allows plain HTTP secret injection: provider=%s connection_id=%s host=%s",
            connection.provider,
            connection.id,
            parsed.hostname or "",
        )
        return
    logger.warning(
        "Sandbox data proxy blocked plain HTTP secret injection: provider=%s connection_id=%s host=%s",
        connection.provider,
        connection.id,
        parsed.hostname or "",
    )
    raise SandboxUnavailableError("business data secret injection requires TLS unless plain HTTP is enabled globally")
