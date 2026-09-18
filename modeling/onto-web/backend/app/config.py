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
MAX_FILE_PREVIEW_BYTES = 512 * 1024
