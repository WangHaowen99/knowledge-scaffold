"""Set up an empty WeKnora instance for the kb-gateway adapter, over HTTP only.

Registers an owner account (if needed), creates a full-access API key and
registers the chat and embedding models, appending WEKNORA_API_KEY,
WEKNORA_CHAT_MODEL_ID and WEKNORA_EMBEDDING_MODEL_ID to the env file given as
the first argument as soon as each one exists. Already-present values are kept,
so it can be re-run after a partial failure: models are reused by name, and an
API key of ours whose token never reached the env file is revoked before a new
one is made.

Environment: WEKNORA_URL (the WeKnora service address; kbsuite.sh passes the
compose service), WEKNORA_ADMIN_EMAIL, WEKNORA_ADMIN_PASSWORD, KBS_LLM_BASE_URL,
KBS_LLM_API_KEY, and optionally WEKNORA_CHAT_MODEL (qwen-plus),
WEKNORA_EMBED_MODEL (text-embedding-v4), WEKNORA_EMBED_DIM (1024). Nothing
secret is logged.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

import httpx

logger = logging.getLogger("weknora-bootstrap")

KEY_NAME = "kb-gateway"


def _env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    return values


def _append(path: Path, key: str, value: str) -> None:
    # created owner-only, never readable by others even for a moment
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600), "a", encoding="utf-8") as fh:
        fh.write(f"{key}={value}\n")
    path.chmod(0o600)


class BootstrapError(RuntimeError):
    """Setting WeKnora up cannot continue; main() reports it and exits non-zero."""


def _wait(client: httpx.Client) -> None:
    for _ in range(90):
        try:
            if client.get("/health").status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(2)
    raise BootstrapError("WeKnora did not become healthy")


def _token(client: httpx.Client, email: str, password: str) -> tuple[str, int]:
    login = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    if login.status_code == 401:
        reg = client.post("/api/v1/auth/register", json={"username": "kbgw-owner", "email": email, "password": password})
        reg.raise_for_status()
        logger.info("registered owner account")
        login = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    login.raise_for_status()
    body = login.json()
    tenant = (body.get("active_tenant") or {}).get("id") or (body.get("user") or {}).get("tenant_id")
    if tenant is None:
        raise BootstrapError("WeKnora login returned no tenant")
    return body["token"], int(tenant)


def _api_key(client: httpx.Client, headers: dict, tenant: int) -> str:
    """A new full-access key; earlier keys of ours are revoked, their tokens were never stored."""

    base = f"/api/v1/tenants/{tenant}/api-keys"
    listed = client.get(base, headers=headers)
    listed.raise_for_status()
    for key in listed.json().get("data") or []:
        if key.get("name") == KEY_NAME:
            client.delete(f"{base}/{int(key['id'])}", headers=headers).raise_for_status()
            logger.info("revoked unrecorded API key %s", KEY_NAME)
    created = client.post(base, headers=headers, json={"name": KEY_NAME, "full_access": True})
    created.raise_for_status()
    logger.info("created API key %s", KEY_NAME)
    return created.json()["data"]["token"]


def _model(client: httpx.Client, headers: dict, payload: dict) -> str:
    existing = client.get("/api/v1/models", headers=headers).json().get("data") or []
    for model in existing:
        if model.get("name") == payload["name"] and model.get("type") == payload["type"]:
            return model["id"]
    created = client.post("/api/v1/models", headers=headers, json=payload)
    created.raise_for_status()
    logger.info("registered %s model %s", payload["type"], payload["name"])
    return created.json()["data"]["id"]


def run(client: httpx.Client, out: Path, env: dict[str, str]) -> int:
    """Bring WeKnora to the adapter's needs; returns the number of values written to ``out``."""

    have = _env_file(out)
    written = 0

    def keep(key: str, value: str) -> None:
        # written at once, so a later failure cannot strand a created key or model
        nonlocal written
        _append(out, key, value)
        written += 1

    _wait(client)
    token, tenant = _token(client, env["WEKNORA_ADMIN_EMAIL"], env["WEKNORA_ADMIN_PASSWORD"])
    headers = {"Authorization": f"Bearer {token}"}
    if not have.get("WEKNORA_API_KEY"):
        keep("WEKNORA_API_KEY", _api_key(client, headers, tenant))
    params = {"base_url": env.get("KBS_LLM_BASE_URL", "").rstrip("/"), "api_key": env.get("KBS_LLM_API_KEY", ""),
              "provider": "generic"}
    if not have.get("WEKNORA_CHAT_MODEL_ID"):
        keep("WEKNORA_CHAT_MODEL_ID", _model(client, headers, {
            "name": env.get("WEKNORA_CHAT_MODEL", "qwen-plus"), "type": "KnowledgeQA", "source": "remote",
            "parameters": params,
        }))
    if not have.get("WEKNORA_EMBEDDING_MODEL_ID"):
        dim = int(env.get("WEKNORA_EMBED_DIM", "1024"))
        keep("WEKNORA_EMBEDDING_MODEL_ID", _model(client, headers, {
            "name": env.get("WEKNORA_EMBED_MODEL", "text-embedding-v4"), "type": "Embedding",
            "source": "remote", "parameters": {**params, "embedding_parameters": {"dimension": dim}},
        }))
    return written


def main(argv: list[str]) -> None:
    if len(argv) != 2:
        raise BootstrapError("usage: bootstrap.py <env file>")
    missing = [k for k in ("WEKNORA_URL", "WEKNORA_ADMIN_EMAIL", "WEKNORA_ADMIN_PASSWORD") if not os.environ.get(k)]
    if missing:
        raise BootstrapError(f"{', '.join(missing)} must be set")
    with httpx.Client(base_url=os.environ["WEKNORA_URL"], timeout=60, trust_env=False) as client:
        written = run(client, Path(argv[1]), dict(os.environ))
    logger.info("WeKnora ready for the adapter (%d new values written)", written)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")  # NOSONAR(S4792)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        main(sys.argv)
    except BootstrapError as exc:
        sys.exit(str(exc))
