"""Postgres-backed job queue (SELECT ... FOR UPDATE SKIP LOCKED)."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from dataclasses import dataclass
from typing import Awaitable, Callable

import asyncpg

from . import db
from .config import get_settings

logger = logging.getLogger(__name__)

NOTIFY_CHANNEL = "kbs_jobs"

KIND_INGEST = "ingest"
KIND_SUMMARY = "summary"
KIND_DELETE = "delete"
KIND_QUESTIONS = "questions"
KIND_WIKI_INGEST = "wiki_ingest"
KIND_WIKI_RETRACT = "wiki_retract"
KIND_WIKI_FINALIZE = "wiki_finalize"
KIND_PROFILE = "kb_profile"


class RetryLater(Exception):
    """Raise from a handler to retry with backoff (counts as an attempt)."""


class Abandon(Exception):
    """Raise from a handler when the job is obsolete; marks it done."""


class Defer(Exception):
    """Raise when the job cannot run yet (e.g. a lock is busy); requeued without using an attempt."""

    def __init__(self, delay_s: float = 15.0) -> None:
        super().__init__(f"deferred {delay_s}s")
        self.delay_s = delay_s


@dataclass
class Job:
    id: int
    kind: str
    document_id: str | None
    payload: dict
    attempts: int
    max_attempts: int
    kb_id: str | None = None


async def enqueue(
    conn: asyncpg.Connection,
    kind: str,
    *,
    document_id: str | None = None,
    payload: dict | None = None,
    max_attempts: int = 3,
    delay_s: float = 0.0,
    kb_id: str | None = None,
) -> int:
    job_id = await conn.fetchval(
        "INSERT INTO jobs (kind, document_id, payload, max_attempts, run_after, kb_id)"
        " VALUES ($1, $2, $3, $4, now() + make_interval(secs => $5), $6) RETURNING id",
        kind,
        document_id,
        payload or {},
        max_attempts,
        float(delay_s),
        kb_id,
    )
    await conn.execute(f"NOTIFY {NOTIFY_CHANNEL}")
    return job_id


async def enqueue_kb_once(conn: asyncpg.Connection, kind: str, kb_id: str, *, delay_s: float) -> None:
    """Queue a knowledge-base level job unless one is already waiting.

    A waiting job is pushed back to ``now() + delay`` instead, so a burst of
    document events runs the job once, after the burst settles.
    """

    updated = await conn.fetchval(
        "UPDATE jobs SET run_after = greatest(run_after, now() + make_interval(secs => $3)), updated_at = now()"
        " WHERE kind = $1 AND kb_id = $2 AND status = 'queued' RETURNING id",
        kind,
        kb_id,
        float(delay_s),
    )
    if updated is None:
        await enqueue(conn, kind, kb_id=kb_id, delay_s=delay_s)


Handler = Callable[[Job], Awaitable[None]]


class Worker:
    def __init__(self, handlers: dict[str, Handler], *, concurrency: int | None = None) -> None:
        settings = get_settings()
        self.handlers = handlers
        self.concurrency = concurrency or settings.worker_concurrency
        self.name = f"{socket.gethostname()}:{os.getpid()}"
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()

    async def _claim(self) -> Job | None:
        row = await db.pool().fetchrow(
            "UPDATE jobs SET status = 'running', locked_by = $1, locked_at = now(),"
            " attempts = attempts + 1, updated_at = now()"
            " WHERE id = (SELECT id FROM jobs WHERE status = 'queued' AND run_after <= now()"
            "   AND kind = ANY($2::text[]) ORDER BY run_after, id FOR UPDATE SKIP LOCKED LIMIT 1)"
            " RETURNING id, kind, document_id, payload, attempts, max_attempts, kb_id",
            self.name,
            list(self.handlers),
        )
        return Job(**dict(row)) if row else None

    async def _finish(self, job: Job, *, error: str | None, retry: bool) -> None:
        if error is None:
            await db.pool().execute(
                "UPDATE jobs SET status = 'done', locked_by = NULL, updated_at = now() WHERE id = $1",
                job.id,
            )
            return
        if retry and job.attempts < job.max_attempts:
            backoff = min(300, 10 * (2 ** (job.attempts - 1)))
            await db.pool().execute(
                "UPDATE jobs SET status = 'queued', locked_by = NULL, last_error = $2,"
                " run_after = now() + make_interval(secs => $3), updated_at = now() WHERE id = $1",
                job.id,
                error[:2000],
                float(backoff),
            )
        else:
            await db.pool().execute(
                "UPDATE jobs SET status = 'failed', locked_by = NULL, last_error = $2,"
                " updated_at = now() WHERE id = $1",
                job.id,
                error[:2000],
            )

    async def _run_one(self, job: Job) -> None:
        handler = self.handlers[job.kind]
        try:
            await handler(job)
        except Abandon as exc:
            logger.info("job %s abandoned: %s", job.id, exc)
            await self._finish(job, error=None, retry=False)
        except Defer as exc:
            await db.pool().execute(
                "UPDATE jobs SET status = 'queued', locked_by = NULL, attempts = greatest(attempts - 1, 0),"
                " run_after = now() + make_interval(secs => $2), updated_at = now() WHERE id = $1",
                job.id,
                float(exc.delay_s),
            )
        except RetryLater as exc:
            await self._finish(job, error=str(exc), retry=True)
        except Exception as exc:  # noqa: BLE001 - one bad job must not kill the loop
            logger.exception("job %s (%s) failed", job.id, job.kind)
            await self._finish(job, error=f"{type(exc).__name__}: {exc}", retry=False)
        else:
            await self._finish(job, error=None, retry=False)

    async def _loop(self) -> None:
        poll = get_settings().job_poll_s
        while not self._stop.is_set():
            job = await self._claim()
            if job is None:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=poll)
                except asyncio.TimeoutError:
                    pass
                continue
            await self._run_one(job)

    async def _listen(self) -> None:
        conn = await asyncpg.connect(get_settings().database_url)
        try:
            await conn.add_listener(NOTIFY_CHANNEL, lambda *_: self._wake.set())
            await self._stop.wait()
        finally:
            await conn.close()

    async def _reap(self) -> None:
        """Requeue jobs whose worker died mid-run (lease expired)."""

        lease = get_settings().job_lease_s
        while not self._stop.is_set():
            await db.pool().execute(
                "UPDATE jobs SET status = CASE WHEN attempts < max_attempts THEN 'queued' ELSE 'failed' END,"
                " locked_by = NULL, last_error = 'lease expired', updated_at = now()"
                " WHERE status = 'running' AND locked_at < now() - make_interval(secs => $1)",
                float(lease),
            )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass

    async def run(self) -> None:
        tasks = [asyncio.create_task(self._loop()) for _ in range(self.concurrency)]
        tasks.append(asyncio.create_task(self._listen()))
        tasks.append(asyncio.create_task(self._reap()))
        try:
            await self._stop.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
