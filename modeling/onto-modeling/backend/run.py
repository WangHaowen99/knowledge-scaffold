import os

import uvicorn

from app import config

if __name__ == "__main__":
    # 开发默认热重载；容器/生产环境设 ONTO_WEB_RELOAD=0 关闭
    reload_enabled = os.environ.get("ONTO_WEB_RELOAD", "1") == "1"
    uvicorn.run(
        "app.main:app",
        host=config.HOST,
        port=config.PORT,
        reload=reload_enabled,
        # SSE 事件流不会自己结束：不设上限时 uvicorn 一直等连接关闭，停机收尾（写中断标记、
        # 回收引擎）根本轮不到，就被 docker 宽限期后的 SIGKILL 打断。预算见 agent_sessions
        timeout_graceful_shutdown=3,
    )
