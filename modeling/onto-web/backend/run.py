import os

import uvicorn

from app import config

if __name__ == "__main__":
    # 开发默认热重载；容器/生产环境设 ONTO_WEB_RELOAD=0 关闭
    reload_enabled = os.environ.get("ONTO_WEB_RELOAD", "1") == "1"
    uvicorn.run(
        "app.main:app", host=config.HOST, port=config.PORT, reload=reload_enabled
    )
