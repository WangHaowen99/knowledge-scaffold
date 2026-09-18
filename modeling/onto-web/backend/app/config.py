import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent  # backend/
PACKAGE_ROOT = BASE_DIR.parent  # onto-web/

DATA_DIR = Path(os.environ.get("ONTO_WEB_DATA_DIR", str(BASE_DIR / "data")))
ASSETS_DIR = Path(os.environ.get("ONTO_WEB_ASSETS_DIR", str(PACKAGE_ROOT / "assets")))
HOST = os.environ.get("ONTO_WEB_HOST", "127.0.0.1")
PORT = int(os.environ.get("ONTO_WEB_PORT", "8100"))

# canUseTool 确认卡等待前端答复的超时；超时按拒绝处理，避免会话永久挂起
PERMISSION_TIMEOUT_SECONDS = int(os.environ.get("ONTO_WEB_PERMISSION_TIMEOUT", "600"))

# 会话权限模式：默认全放行（bypassPermissions）；设为 default 时启用逐操作确认卡
PERMISSION_MODE = os.environ.get("ONTO_WEB_PERMISSION_MODE", "bypassPermissions")

# 私有网关兼容：多轮回放空 thinking 块会被部分网关 400 拒绝，设 0 关闭扩展思考
MAX_THINKING_TOKENS = os.environ.get("ONTO_WEB_MAX_THINKING_TOKENS", "")

# auth-server 管理 API 的令牌（用户管理页需要；未配置时用户管理功能不可用）
AUTH_ADMIN_TOKEN = os.environ.get("ONTO_WEB_AUTH_ADMIN_TOKEN", "")
MAX_FILE_PREVIEW_BYTES = 512 * 1024
