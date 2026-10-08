import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent  # backend/
PACKAGE_ROOT = BASE_DIR.parent  # onto-modeling/

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

# 会话池（方案 4.1.1）：活跃子进程上限；8G 内存建议 12，超限先驱逐空闲会话再排队
MAX_ACTIVE_SESSIONS = int(os.environ.get("ONTO_WEB_MAX_SESSIONS", "12"))
# 空闲会话回收阈值（秒，0 关闭）；回收后靠 resume 续接模型上下文，界面无感
# 空闲回收 TTL：连续对话期间进程常驻（轮间零延迟），停顿超过该时长即回收
# 子进程释放内存（~400MB/会话），下次消息经 resume 续接（多付 2~5s 冷启动，
# 且该场景通常已过 API prompt cache 的 5 分钟 TTL，感知很弱）。
SESSION_IDLE_TTL_SECONDS = int(os.environ.get("ONTO_WEB_SESSION_IDLE_TTL", "120"))
SESSION_REAPER_INTERVAL = int(os.environ.get("ONTO_WEB_SESSION_REAPER_INTERVAL", "30"))

# 降级兼容包（无 AVX 环境的最后手段）：指向外部 JS 版 CLI 的路径，空 = 用捆绑 CLI
CLI_PATH = os.environ.get("ONTO_WEB_CLI_PATH", "")

# Jiuwen 引擎（openjiuwen，进程内，OpenAI 兼容模型直连）：env 为部署级兜底，
# 配置中心「模型接入」的 UI 配置优先（docs/openjiuwen-engine-migration.md §3.3）
JIUWEN_BASE_URL = os.environ.get("ONTO_WEB_JIUWEN_BASE_URL", "")
JIUWEN_API_KEY = os.environ.get("ONTO_WEB_JIUWEN_API_KEY", "")
JIUWEN_MODEL = os.environ.get("ONTO_WEB_JIUWEN_MODEL", "")
JIUWEN_MAX_TOKENS = int(os.environ.get("ONTO_WEB_JIUWEN_MAX_TOKENS", "8192"))
JIUWEN_MAX_ITERATIONS = int(os.environ.get("ONTO_WEB_JIUWEN_MAX_ITERATIONS", "60"))

# modeling 评测服务（数据评测阶段，方案 docs/eval-optimization-integration.md）：
# 未配置 URL 时「数据评测」阶段自动隐藏；TOKEN 为无共享 auth 部署的兜底令牌
# （共享账号中心/方案 A 下透传当前用户 token，无需配置）
MODELING_URL = os.environ.get("ONTO_WEB_MODELING_URL", "").rstrip("/")
MODELING_TOKEN = os.environ.get("ONTO_WEB_MODELING_TOKEN", "")
# 仅用于清理 Onto 创建失败后遗留的空 Modeling 项目，不是用户 MCP Token。
MODELING_CLEANUP_TOKEN = os.environ.get("ONTO_WEB_MODELING_CLEANUP_TOKEN", "")
MODELING_CLEANUP_INTERVAL_SECONDS = int(
    os.environ.get("ONTO_WEB_MODELING_CLEANUP_INTERVAL", "30")
)

# workspace 定期备份间隔（小时，0 关闭）；保留份数固定为最近 3 份
BACKUP_INTERVAL_HOURS = int(os.environ.get("ONTO_WEB_BACKUP_INTERVAL_HOURS", "24"))

# auth-server 管理 API 的令牌（用户管理页需要；未配置时用户管理功能不可用）
AUTH_ADMIN_TOKEN = os.environ.get("ONTO_WEB_AUTH_ADMIN_TOKEN", "")
MAX_FILE_PREVIEW_BYTES = 512 * 1024
