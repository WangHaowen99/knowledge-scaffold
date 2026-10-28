"""Admin CLI: bootstrap a tenant and service keys.

    python -m kb_service.cli bootstrap --tenant default --out /secrets/keys.env

Keys are printed once into the output file (mode 600) and stored hashed.
Re-running keeps existing keys unless --rotate is given.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import uuid
from pathlib import Path

from . import auth, db
from .config import get_settings

DEFAULT_KEYS = (
    # (name, env var, capabilities, can_assert_tenant)
    ("swarm-plugin", "KBS_PLUGIN_KEY", ["manage", "ingest", "retrieve"], False),
    ("swarm-mcp", "KBS_MCP_KEY", ["retrieve"], False),
    ("da", "KBS_DA_KEY", ["retrieve", "reprocess"], True),
)


async def bootstrap(tenant: str, out: Path, rotate: bool) -> None:
    settings = get_settings()
    await db.migrate(settings.database_url)
    pool = await db.connect(settings.database_url)
    await pool.execute(
        "INSERT INTO tenants (id, name) VALUES ($1, $1) ON CONFLICT (id) DO NOTHING", tenant
    )
    existing = {}
    if out.exists() and not rotate:
        for line in out.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                existing[k.strip()] = v.strip()
    lines = [f"# kb-service keys for tenant {tenant}. Keep out of version control."]
    for name, env, caps, assert_tenant in DEFAULT_KEYS:
        raw = existing.get(env)
        if raw and await pool.fetchval("SELECT 1 FROM service_keys WHERE key_hash = $1", auth.hash_key(raw)):
            lines.append(f"{env}={raw}")
            continue
        raw = auth.new_key()
        await pool.execute("UPDATE service_keys SET disabled = true WHERE name = $1 AND tenant_id = $2", name, tenant)
        await pool.execute(
            "INSERT INTO service_keys (id, tenant_id, name, key_hash, capabilities, can_assert_tenant)"
            " VALUES ($1, $2, $3, $4, $5, $6)",
            str(uuid.uuid4()),
            tenant,
            name,
            auth.hash_key(raw),
            caps,
            assert_tenant,
        )
        lines.append(f"{env}={raw}")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n")
    os.chmod(out, 0o600)
    await db.close()
    print(f"wrote {len(DEFAULT_KEYS)} keys for tenant {tenant} to {out}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="kb-service")
    sub = parser.add_subparsers(dest="cmd", required=True)
    boot = sub.add_parser("bootstrap")
    boot.add_argument("--tenant", default="default")
    boot.add_argument("--out", required=True, type=Path)
    boot.add_argument("--rotate", action="store_true")
    args = parser.parse_args()
    if args.cmd == "bootstrap":
        asyncio.run(bootstrap(args.tenant, args.out, args.rotate))


if __name__ == "__main__":
    main()
