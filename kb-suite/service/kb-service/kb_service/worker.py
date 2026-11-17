"""Worker process: runs ingestion, enrichment (summary, questions, wiki, profile) and delete jobs."""

from __future__ import annotations

import asyncio
import logging
import signal

from . import db, log_policy, models_client
from .config import get_settings
from .jobs import Worker
from .parsing import docreader_client
from .pipeline import handlers


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")  # NOSONAR(S4792)
    log_policy.quiet_http_clients()
    settings = get_settings()
    await db.migrate(settings.database_url)
    await db.connect(settings.database_url, max_size=settings.worker_concurrency + settings.llm_concurrency + 4)
    worker = Worker(handlers())
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, worker.stop)
    logging.getLogger(__name__).info("worker %s started, concurrency=%s", worker.name, worker.concurrency)
    try:
        await worker.run()
    finally:
        await models_client.aclose()
        await docreader_client.aclose()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
