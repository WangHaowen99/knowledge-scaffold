from __future__ import annotations

import json
import logging
from importlib import resources

import asyncpg

from .config import get_settings

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


async def _init_connection(conn: asyncpg.Connection) -> None:
    for typename in ("json", "jsonb"):
        await conn.set_type_codec(
            typename, encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
        )


async def connect(dsn: str, *, min_size: int | None = None, max_size: int | None = None) -> asyncpg.Pool:
    """Open the process-wide pool.

    Without ``max_size`` (the API) the pool holds ``db_pool_size`` connections,
    opened at start and kept while idle: a burst of searches then neither
    waits for new connections nor pays a fresh backend's first-query cost.
    PostgreSQL parallel query is set to ``db_parallel_workers`` per session.
    """

    global _pool
    if _pool is None:
        settings = get_settings()
        if max_size is None:
            max_size = settings.db_pool_size
            min_size = max_size if min_size is None else min_size
        _pool = await asyncpg.create_pool(
            dsn,
            min_size=min(min_size or 1, max_size),
            max_size=max_size,
            max_inactive_connection_lifetime=0,
            init=_init_connection,
            server_settings={"max_parallel_workers_per_gather": str(max(0, settings.db_parallel_workers))},
        )
    return _pool


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("database pool is not initialized")
    return _pool


async def close() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def migrate(dsn: str) -> list[str]:
    """Apply bundled SQL migrations in name order; each runs once."""

    conn = await asyncpg.connect(dsn)
    applied: list[str] = []
    try:
        # Serialize concurrent starters (api and worker boot together).
        await conn.execute("SELECT pg_advisory_lock(728134)")
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        done = {r["name"] for r in await conn.fetch("SELECT name FROM schema_migrations")}
        files = sorted(
            p for p in resources.files("kb_service.migrations").iterdir() if p.name.endswith(".sql")
        )
        for path in files:
            if path.name in done:
                continue
            async with conn.transaction():
                await conn.execute(path.read_text(encoding="utf-8"))
                await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", path.name)
            applied.append(path.name)
            logger.info("applied migration %s", path.name)
    finally:
        try:
            await conn.execute("SELECT pg_advisory_unlock(728134)")
        finally:
            await conn.close()
    return applied
