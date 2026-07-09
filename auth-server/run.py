"""开发启动入口：默认 127.0.0.1:5056，可用 AUTH_SERVER_HOST/PORT 覆盖。"""

from __future__ import annotations

from auth_server import create_app
from auth_server.config import AuthServerConfig


def main() -> None:
    config = AuthServerConfig.from_env()
    app = create_app(config)
    app.run(host=config.host, port=config.port)


if __name__ == "__main__":
    main()
