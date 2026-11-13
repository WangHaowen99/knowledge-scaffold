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
# 临时缓解长步骤期间的空闲回收；不改变池满驱逐或会话换绑策略。
SESSION_IDLE_TTL_SECONDS = int(os.environ.get("ONTO_WEB_SESSION_IDLE_TTL", "600"))
SESSION_REAPER_INTERVAL = int(os.environ.get("ONTO_WEB_SESSION_REAPER_INTERVAL", "30"))
# 一轮在跑却这么久（秒）没有任何引擎事件，判定执行超时并终止引擎；0 = 不判定。
# 模型调用失败重试期间也没有事件：Jiuwen 单次调用最长 300 秒，实测连续重试可超过 25 分钟。
# 到 30 分钟仍没有任何进展就按超时收尾，用户可以直接发消息继续
SESSION_STALL_TIMEOUT_SECONDS = int(os.environ.get("ONTO_WEB_SESSION_STALL_TIMEOUT", "1800"))

# 降级兼容包（无 AVX 环境的最后手段）：指向外部 JS 版 CLI 的路径，空 = 用捆绑 CLI
CLI_PATH = os.environ.get("ONTO_WEB_CLI_PATH", "")

# Jiuwen 引擎（openjiuwen，进程内，OpenAI 兼容模型直连）：env 为部署级兜底，
# 配置中心「模型接入」的 UI 配置优先（docs/openjiuwen-engine-migration.md §3.3）
JIUWEN_BASE_URL = os.environ.get("ONTO_WEB_JIUWEN_BASE_URL", "")
JIUWEN_API_KEY = os.environ.get("ONTO_WEB_JIUWEN_API_KEY", "")
JIUWEN_MODEL = os.environ.get("ONTO_WEB_JIUWEN_MODEL", "")
JIUWEN_MAX_TOKENS = int(os.environ.get("ONTO_WEB_JIUWEN_MAX_TOKENS", "32000"))
JIUWEN_MAX_ITERATIONS = int(os.environ.get("ONTO_WEB_JIUWEN_MAX_ITERATIONS", "60"))

# modeling 评测服务（数据评测阶段，方案 docs/eval-optimization-integration.md）：
# 未配置 URL 时「数据评测」阶段自动隐藏；TOKEN 为无共享 auth 部署的兜底令牌
# （共享账号中心/方案 A 下透传当前用户 token，无需配置）
ONTO_MCP_PUBLIC_BASE_URL = os.environ.get("ONTO_WEB_MCP_PUBLIC_BASE_URL", "").rstrip("/")
# 评测服务在浏览器中的入口地址，由用户浏览器访问（不是容器网络地址）。留空时浏览器按建模页面同主机的 5180 端口打开；
# 评测在其他主机、端口或经 HTTPS 时填写完整地址；不接受相对路径
EVAL_PUBLIC_URL = os.environ.get("ONTO_WEB_EVAL_PUBLIC_URL", "").strip()
# 评测服务在容器网络中的地址（如 http://knowledge-evaluation:5180），供后端以当前用户登录读取评测结果与产物；
# 留空时已发布版本不显示评测报告与优化建议
EVAL_URL = os.environ.get("ONTO_WEB_EVAL_URL", "").rstrip("/")
MODELING_URL = os.environ.get("ONTO_WEB_MODELING_URL", "").rstrip("/")
MODELING_TOKEN = os.environ.get("ONTO_WEB_MODELING_TOKEN", "")
# 独立的导入/MCP 补关联通道；不影响新建项目的全局 Modeling 配置
MODELING_ASSOCIATION_URL = os.environ.get("ONTO_WEB_MODELING_ASSOCIATION_URL", "").rstrip("/")
MODELING_ASSOCIATION_TOKEN = os.environ.get("ONTO_WEB_MODELING_ASSOCIATION_TOKEN", "")
# 仅用于清理 Onto 创建失败后遗留的空 Modeling 项目，不是用户 MCP Token。
MODELING_CLEANUP_TOKEN = os.environ.get("ONTO_WEB_MODELING_CLEANUP_TOKEN", "")
MODELING_CLEANUP_INTERVAL_SECONDS = int(
    os.environ.get("ONTO_WEB_MODELING_CLEANUP_INTERVAL", "30")
)
# 同时执行的评测轮数上限（评测专用线程池容量）；超出的轮次排队，状态里显示位次。
# 每轮在 modeling 侧按用例并发调用 consumer，上限同时约束下游负载
EVAL_MAX_CONCURRENT_ROUNDS = max(1, int(os.environ.get("ONTO_WEB_EVAL_MAX_CONCURRENT_ROUNDS", "16")))

# workspace 定期备份间隔（小时，0 关闭）；保留份数固定为最近 3 份
BACKUP_INTERVAL_HOURS = int(os.environ.get("ONTO_WEB_BACKUP_INTERVAL_HOURS", "24"))

# auth-server 管理 API 的令牌（用户管理页需要；未配置时用户管理功能不可用）
AUTH_ADMIN_TOKEN = os.environ.get("ONTO_WEB_AUTH_ADMIN_TOKEN", "")
MAX_FILE_PREVIEW_BYTES = 512 * 1024
