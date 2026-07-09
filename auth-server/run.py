"""开发启动入口：默认 127.0.0.1:5056，可用 AUTH_SERVER_HOST/PORT 覆盖。"""

from __future__ import annotations

import logging

from auth_server import create_app
from auth_server.config import AuthServerConfig


def main() -> None:
    # INFO 级：console 邮件模式的验证码、TTL 钳制告警等以 INFO/WARNING 输出，
    # 不配置的话根 logger 默认 WARNING 会吞掉验证码日志
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    config = AuthServerConfig.from_env()
    app = create_app(config)
    app.run(host=config.host, port=config.port)


if __name__ == "__main__":
    main()
