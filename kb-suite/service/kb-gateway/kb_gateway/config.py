"""Gateway settings and the static backend list (``backends.yaml``)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

# ids are routed by prefix and embedded in DA page ids ("{docId}:L1") and in
# the comma separated X-Kb-Scope header. The default backend's ids are UUIDs,
# which never contain "_", so a prefix ending in "_" cannot capture them.
_PREFIX_RE = re.compile(r"^[A-Za-z0-9]+_$")
_ID_RE = re.compile(r"^[a-z][a-z0-9-]*$")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KBGW_")

    backends_file: str = "/etc/kb-gateway/backends.yaml"
    # "name=ENV_VAR:cap,cap;name=ENV_VAR:cap" -- key values come from the named env vars
    keys: str = ""
    tenant: str = "default"
    refresh_interval_s: float = 60.0
    backend_timeout_s: float = 120.0
    search_timeout_s: float = 30.0


@lru_cache
def get_settings() -> Settings:
    return Settings()


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class BackendConfig:
    id: str
    label: str
    base_url: str
    credential_env: str
    tools_credential_env: str
    id_prefix: str
    default: bool
    readonly: bool
    # not deployed is normal: hidden until it has answered once
    optional: bool = False

    def credential(self, *, tools: bool = False) -> str:
        name = self.tools_credential_env if tools and self.tools_credential_env else self.credential_env
        return os.environ.get(name, "") if name else ""


def _backend(raw: dict) -> BackendConfig:
    try:
        backend = BackendConfig(
            id=str(raw["id"]),
            label=str(raw.get("label") or raw["id"]),
            base_url=str(raw["base_url"]).rstrip("/"),
            credential_env=str(raw.get("credential_env") or ""),
            tools_credential_env=str(raw.get("tools_credential_env") or ""),
            id_prefix=str(raw.get("id_prefix") or ""),
            default=bool(raw.get("default", False)),
            readonly=bool(raw.get("readonly", False)),
            optional=bool(raw.get("optional", False)),
        )
    except KeyError as exc:
        raise ConfigError(f"backend entry is missing {exc.args[0]!r}: {raw!r}") from exc
    if not _ID_RE.match(backend.id):
        raise ConfigError(f"backend id {backend.id!r} must match {_ID_RE.pattern}")
    if backend.id_prefix and not _PREFIX_RE.match(backend.id_prefix):
        raise ConfigError(f"id_prefix {backend.id_prefix!r} of backend {backend.id} must be letters/digits ending in '_'")
    if backend.default and backend.optional:
        raise ConfigError(f"default backend {backend.id} cannot be optional")
    return backend


def parse_backends(data: dict) -> list[BackendConfig]:
    entries = (data or {}).get("backends") or []
    backends = [_backend(raw) for raw in entries]
    if not backends:
        raise ConfigError("no backends configured")
    ids = [b.id for b in backends]
    if len(set(ids)) != len(ids):
        raise ConfigError(f"duplicate backend ids: {ids}")
    # "default" is where a knowledge base created without a backend goes; the backend
    # without an id_prefix owns every id that carries no known prefix (kb-service's
    # UUIDs). They need not be the same backend, so the default can change without
    # renaming any existing id.
    defaults = [b for b in backends if b.default]
    if len(defaults) != 1:
        raise ConfigError("exactly one backend must be marked default: true")
    unprefixed = [b.id for b in backends if not b.id_prefix]
    if len(unprefixed) > 1:
        raise ConfigError(f"at most one backend may have an empty id_prefix (unprefixed: {unprefixed})")
    prefixes = [b.id_prefix for b in backends if b.id_prefix]
    if len(set(prefixes)) != len(prefixes):
        raise ConfigError(f"duplicate id prefixes: {prefixes}")
    return backends


def load_backends(path: str | None = None) -> list[BackendConfig]:
    file = Path(path or get_settings().backends_file)
    try:
        data = yaml.safe_load(file.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read {file}: {exc}") from exc
    return parse_backends(data)
