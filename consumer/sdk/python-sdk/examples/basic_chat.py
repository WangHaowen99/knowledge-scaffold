import logging
import sys
from typing import TextIO

from onto_agent_sdk import OntoAgentClient

logger = logging.getLogger(__name__)


def _configure_cli_logging(stream: TextIO) -> None:
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def main() -> None:
    _configure_cli_logging(sys.stdout)
    client = OntoAgentClient(
        base_url="http://localhost:5055",
        token="YOUR_TOKEN",
        workspace_id="demo-workspace",
    )

    with client.chat.send(
        session_id="session-123",
        message="请分析这个项目",
    ) as stream:
        for event in stream:
            logger.info("%s %s", event.kind, event.payload)


if __name__ == "__main__":
    main()
