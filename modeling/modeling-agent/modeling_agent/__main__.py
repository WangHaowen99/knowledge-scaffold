from __future__ import annotations

import logging
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from .settings import Settings


def configure_logging(log_dir: Path) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for logger_name in ("openjiuwen", "openai", "httpx", "httpcore", "alembic"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)

    from openjiuwen.core.common.logging.log_config import configure_log_config

    configure_log_config({
        "backend": "default",
        "level": "WARNING",
        "log_path": str(log_dir),
        "output": ["console", "file"],
        "interface_output": ["console", "file"],
        "performance_output": ["file"],
        "propagate": False,
        "loggers": {},
    })


def main() -> None:
    load_dotenv()
    settings = Settings.from_env()
    configure_logging(settings.data_dir / "logs")

    from .app import create_app
    from .service import AgentService

    uvicorn.run(
        create_app(AgentService(settings)),
        host=settings.host,
        port=settings.port,
        server_header=False,
        access_log=False,
    )


if __name__ == "__main__":
    main()
