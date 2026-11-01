"""配置门面：同名属性保持不变，取值经配置注册表（governance/config_registry，方案 E5）。

每一项的来源优先级、校验与生效时机在注册表里声明；这里只取环境变量与默认值，数据目录里的覆盖
（设置页保存的模型接入、权限模式、运维调整的会话池）由各设置模块在运行时读。
"""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent  # backend/
PACKAGE_ROOT = BASE_DIR.parent  # onto-modeling/

try:  # noqa: E402  注册表的默认值依赖上面两个路径
    from .governance.config_registry import env_value as _env
except ImportError:  # 按文件单独加载（没有包上下文）时
    from app.governance.config_registry import env_value as _env

DATA_DIR = Path(_env("ONTO_WEB_DATA_DIR"))
ASSETS_DIR = Path(_env("ONTO_WEB_ASSETS_DIR"))
HOST = _env("ONTO_WEB_HOST")
PORT = _env("ONTO_WEB_PORT")

# canUseTool 确认卡等待前端答复的超时；超时按拒绝处理，避免会话永久挂起
PERMISSION_TIMEOUT_SECONDS = _env("ONTO_WEB_PERMISSION_TIMEOUT")

# 会话权限模式：默认全放行（bypassPermissions）；设为 default 时启用逐操作确认卡
PERMISSION_MODE = _env("ONTO_WEB_PERMISSION_MODE")

# 私有网关兼容：多轮回放空 thinking 块会被部分网关 400 拒绝，设 0 关闭扩展思考
MAX_THINKING_TOKENS = _env("ONTO_WEB_MAX_THINKING_TOKENS")

# 会话池（方案 4.1.1）：活跃子进程上限；8G 内存建议 12，超限先驱逐空闲会话再排队。
# 运维面（O-16）可在线调整，覆盖值存数据目录，启动时由 ops_state 套用到这两个属性上
MAX_ACTIVE_SESSIONS = _env("ONTO_WEB_MAX_SESSIONS")
# 空闲回收 TTL：连续对话期间进程常驻（轮间零延迟），停顿超过该时长即回收
# 子进程释放内存（~400MB/会话），下次消息经 resume 续接（多付 2~5s 冷启动，
# 且该场景通常已过 API prompt cache 的 5 分钟 TTL，感知很弱）。0 关闭
SESSION_IDLE_TTL_SECONDS = _env("ONTO_WEB_SESSION_IDLE_TTL")
SESSION_REAPER_INTERVAL = _env("ONTO_WEB_SESSION_REAPER_INTERVAL")
# 池满时的处理：先驱逐空闲会话再排队（evict_idle_then_queue），或只排队（queue_only）
SESSION_OVERFLOW_POLICY = "evict_idle_then_queue"

# 降级兼容包（无 AVX 环境的最后手段）：指向外部 JS 版 CLI 的路径，空 = 用捆绑 CLI
CLI_PATH = _env("ONTO_WEB_CLI_PATH")

# Jiuwen 引擎（openjiuwen，进程内，OpenAI 兼容模型直连）：env 为部署级兜底，
# 配置中心「模型接入」的 UI 配置优先（docs/openjiuwen-engine-migration.md §3.3）
JIUWEN_BASE_URL = _env("ONTO_WEB_JIUWEN_BASE_URL")
JIUWEN_API_KEY = _env("ONTO_WEB_JIUWEN_API_KEY")
JIUWEN_MODEL = _env("ONTO_WEB_JIUWEN_MODEL")
JIUWEN_MAX_TOKENS = _env("ONTO_WEB_JIUWEN_MAX_TOKENS")
JIUWEN_MAX_ITERATIONS = _env("ONTO_WEB_JIUWEN_MAX_ITERATIONS")

# 项目级本地 MCP 的公开地址（反向代理部署时用）；空 = 按访问工作台的地址生成
ONTO_MCP_PUBLIC_BASE_URL = _env("ONTO_WEB_MCP_PUBLIC_BASE_URL").rstrip("/")

# 定期备份间隔（小时，0 关闭）；保留份数固定为最近 3 份。启动时已超过间隔就先补一份
BACKUP_INTERVAL_HOURS = _env("ONTO_WEB_BACKUP_INTERVAL_HOURS")
# 备份目录；空 = 数据目录下的 backups/（与数据同卷，建议另挂独立卷）
BACKUP_DIR = _env("ONTO_WEB_BACKUP_DIR")
# 主密钥单独备份的目录（与密文分开放，方案第 6 章第 5 项）；空 = 不单独备份
KEY_BACKUP_DIR = _env("ONTO_WEB_KEY_BACKUP_DIR")

# auth-server 管理 API 的令牌（用户管理页需要；未配置时用户管理功能不可用）
AUTH_ADMIN_TOKEN = _env("ONTO_WEB_AUTH_ADMIN_TOKEN")
MAX_FILE_PREVIEW_BYTES = 512 * 1024

# 运维管理面（契约 1.1、方案 E8）：同进程第二个监听，默认只在本机；业务面默认不处理 /api/v1/ops
OPS_HOST = _env("ONTO_WEB_OPS_HOST")
OPS_PORT = _env("ONTO_WEB_OPS_PORT")
OPS_ON_MAIN = _env("ONTO_WEB_OPS_ON_MAIN")
OPS_BASE_URL = _env("ONTO_WEB_OPS_BASE_URL").rstrip("/")

# 密钥与 agent 身份（方案 E10）：密钥目录 agent 读不到；agent 及其工具子进程用独立的非 root 用户
# 密钥目录：空 = 数据目录下的 secrets/（取路径用 governance.secrets.secrets_dir()，跟随 DATA_DIR）
SECRETS_DIR = _env("ONTO_WEB_SECRETS_DIR")
AGENT_USER = _env("ONTO_WEB_AGENT_USER")
LOG_LEVEL = _env("ONTO_WEB_LOG_LEVEL")
