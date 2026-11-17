"""In-memory stand-ins for the asyncpg pool, enough to drive document and API code without PostgreSQL."""

from __future__ import annotations

import asyncpg
import pytest


class FakeConn:
    """Runs every query through ``handler(kind, sql, args)``.

    Transactions follow PostgreSQL: an error inside a transaction block aborts
    it (later queries fail) until the block, or its savepoint, is rolled back.
    """

    def __init__(self, handler) -> None:
        self.handler = handler
        self.depth = 0
        self.aborted = False
        self.queries: list[str] = []

    def transaction(self) -> "_Transaction":
        return _Transaction(self)

    async def _run(self, kind: str, sql: str, args: tuple):
        if self.aborted:
            raise asyncpg.InFailedSQLTransactionError("current transaction is aborted")
        self.queries.append(sql)
        try:
            return self.handler(kind, sql, args)
        except asyncpg.PostgresError:
            if self.depth:
                self.aborted = True
            raise

    async def fetchrow(self, sql: str, *args):
        return await self._run("fetchrow", sql, args)

    async def fetchval(self, sql: str, *args):
        return await self._run("fetchval", sql, args)

    async def fetch(self, sql: str, *args):
        return await self._run("fetch", sql, args)

    async def execute(self, sql: str, *args):
        return await self._run("execute", sql, args)

    async def executemany(self, sql: str, rows):
        return await self._run("executemany", sql, (rows,))


class _Transaction:
    def __init__(self, conn: FakeConn) -> None:
        self.conn = conn

    async def __aenter__(self) -> "_Transaction":
        self.conn.depth += 1
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self.conn.depth -= 1
        if exc_type is not None:
            self.conn.aborted = False  # ROLLBACK (TO SAVEPOINT)
        return False


class _Acquire:
    def __init__(self, conn: FakeConn) -> None:
        self.conn = conn

    async def __aenter__(self) -> FakeConn:
        return self.conn

    async def __aexit__(self, *exc) -> bool:
        return False


class FakePool(FakeConn):
    def acquire(self) -> _Acquire:
        return _Acquire(self)


@pytest.fixture
def fake_pool(monkeypatch):
    """``fake_pool(handler)`` installs a FakePool as ``db.pool()`` and returns it."""

    from kb_service import db

    def install(handler) -> FakePool:
        pool = FakePool(handler)
        monkeypatch.setattr(db, "pool", lambda: pool)
        return pool

    return install


class FakeCaller:
    tenant_id = "tenant_demo"

    async def check_kb(self, kb_id: str) -> None:
        return None

    async def kb_scope(self, kb_ids, *, allow_all_when_empty: bool = True) -> list[str]:
        return list(kb_ids or ["kb_demo"])


@pytest.fixture
def api_client(monkeypatch):
    """TestClient on the real app with authentication replaced by a fixed caller (no lifespan, no database)."""

    from fastapi.testclient import TestClient

    from kb_service.api import app as app_module
    from kb_service.api import common

    async def fake_caller(request, capability):
        return FakeCaller()

    monkeypatch.setattr(common, "caller_from_request", fake_caller)
    return TestClient(app_module.create_app())
