"""配置注册表（详设 4.10.1、方案 E5、契约 SY-14）：每项配置声明来源优先级、校验、生效时机、是否密钥。

- `app/config.py` 保留原来的同名属性当门面，取值都经这里（环境变量解析失败时告警并用默认值，
  不再让 `int("abc")` 把启动打断）；
- 数据目录里的配置（管理员在设置页保存的模型接入、权限模式、运维改的会话池……）以 `data_dir` 来源
  登记读取函数，注册表据此报告「实际生效值从哪来」；
- 插件在清单里声明的配置项经 `register()` 登记（来源 `pack`，插件运行时 B3a 接入）；
- 密钥项只报告引用（`cred://env/<变量名>`、`cred://data_dir/<文件>`），不回显值。

生效时机：`immediate` 立即、`next_message` 下一条消息（指纹变化后重连）、`new_session` 新会话、
`restart` 重启。
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

_log = logging.getLogger(__name__)

EFFECTS = ("immediate", "next_message", "new_session", "restart")
SOURCES = ("env", "data_dir", "pack", "default")
DEFAULT_PRECEDENCE = ("env", "data_dir", "pack", "default")
# 设置页保存的值盖过 .env 的项（模型接入、权限模式、会话池）
UI_FIRST = ("data_dir", "env", "pack", "default")


@dataclass(frozen=True)
class ConfigItem:
    key: str
    default: Any = ""
    kind: str = "str"  # str | int | bool | path | url | enum
    effect: str = "restart"
    description: str = ""
    secret: bool = False
    edit_page: str | None = None
    minimum: int | None = None
    maximum: int | None = None
    choices: tuple[str, ...] = ()
    allow_zero: bool = False  # 0 表示关闭（备份间隔、空闲回收、运维监听）
    min_bytes: int | None = None  # 密钥最小长度
    precedence: tuple[str, ...] = DEFAULT_PRECEDENCE
    data_dir: Callable[[], Any] | None = field(default=None, compare=False)
    pack: str | None = None
    pack_value: Any = None

    @property
    def validation(self) -> str:
        if self.kind == "int":
            low = "0 或 " if self.allow_zero and self.minimum not in (None, 0) else ""
            if self.minimum is not None and self.maximum is not None:
                return f"整数 {low}{self.minimum}–{self.maximum}"
            if self.minimum is not None:
                return f"整数 ≥ {self.minimum}"
            return "整数"
        if self.kind == "bool":
            return "true / false"
        if self.kind == "enum":
            return " / ".join(self.choices)
        if self.kind == "url":
            return "http(s):// 地址，可留空"
        if self.kind == "path":
            return "路径"
        if self.min_bytes:
            return f"至少 {self.min_bytes} 字节"
        return "字符串"


class ConfigError(ValueError):
    pass


def _parse(item: ConfigItem, raw: str) -> Any:
    raw = raw.strip() if isinstance(raw, str) else raw
    if item.kind == "int":
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise ConfigError(f"{item.key} 必须是整数") from None
        if item.allow_zero and value == 0:
            return value
        if item.minimum is not None and value < item.minimum:
            raise ConfigError(f"{item.key} 不能小于 {item.minimum}")
        if item.maximum is not None and value > item.maximum:
            raise ConfigError(f"{item.key} 不能大于 {item.maximum}")
        return value
    if item.kind == "bool":
        lowered = str(raw).lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off", ""):
            return False
        raise ConfigError(f"{item.key} 必须是 true 或 false")
    if item.kind == "enum" and raw not in item.choices:
        raise ConfigError(f"{item.key} 必须是 {'、'.join(item.choices)} 之一")
    if item.kind == "url" and raw and not _is_web_url(str(raw)):
        raise ConfigError(f"{item.key} 必须以 http(s):// 开头")
    if item.min_bytes and raw and len(str(raw).encode("utf-8")) < item.min_bytes:
        raise ConfigError(f"{item.key} 至少 {item.min_bytes} 字节")
    return raw


def _default(item: ConfigItem) -> Any:
    return item.default() if callable(item.default) else item.default


_items: dict[str, ConfigItem] = {}
_problems: dict[str, str] = {}
_lock = threading.Lock()


def declare(*items: ConfigItem) -> None:
    with _lock:
        for item in items:
            if item.effect not in EFFECTS:
                raise ValueError(f"{item.key}：未知的生效时机 {item.effect}")
            _items[item.key] = item


def register(item: ConfigItem, pack: str) -> None:
    """插件声明的配置项（来源 pack）：键必须带包名前缀，不能覆盖内核项。"""
    if item.key in _items and _items[item.key].pack != pack:
        raise ValueError(f"配置项 {item.key} 已由内核或其他包声明")
    declare(ConfigItem(**{**item.__dict__, "pack": pack}))


def unregister_pack(pack: str) -> None:
    with _lock:
        for key in [key for key, item in _items.items() if item.pack == pack]:
            del _items[key]


def item(key: str) -> ConfigItem:
    return _items[key]


def _is_web_url(text: str) -> bool:
    return text.startswith(("http://", "https://"))  # NOSONAR(S5332) 只校验前缀：内网服务地址允许 http


def _env_given(entry: ConfigItem, raw: str | None) -> bool:
    """环境变量算不算给了值：全是空白只对 str、path 类型算数，空串不算。"""
    if raw is None:
        return False
    return bool(raw.strip()) or (entry.kind in ("str", "path") and raw != "")


def _candidates(entry: ConfigItem):
    for source in entry.precedence:
        if source == "env":
            raw = os.environ.get(entry.key)
            if _env_given(entry, raw):
                yield "env", raw
        elif source == "data_dir" and entry.data_dir is not None:
            try:
                stored = entry.data_dir()
            except Exception:
                stored = None
            if stored not in (None, ""):
                yield "data_dir", stored
        elif source == "pack" and entry.pack and entry.pack_value not in (None, ""):
            yield "pack", entry.pack_value
        elif source == "default":
            yield "default", _default(entry)


def resolve(key: str) -> tuple[Any, str]:
    """(实际生效值, 来源)。某个来源的值不合法时告警并跳过它，继续按优先级往下找。"""
    entry = _items[key]
    for source, raw in _candidates(entry):
        if source == "default":
            return raw, source
        try:
            value = _parse(entry, raw) if isinstance(raw, str) else raw
        except ConfigError as exc:
            with _lock:
                if _problems.get(key) != str(exc):
                    _log.warning("配置项不合法，已忽略：%s（来源 %s）", exc, source)
                _problems[key] = str(exc)
            continue
        return value, source
    return _default(entry), "default"


def value(key: str) -> Any:
    return resolve(key)[0]


def env_value(key: str) -> Any:
    """只看环境变量与默认值：`app/config.py` 的门面属性用它（数据目录里的覆盖由各设置模块在运行时读）。"""
    entry = _items[key]
    raw = os.environ.get(key)
    if _env_given(entry, raw):
        try:
            return _parse(entry, raw)
        except ConfigError as exc:
            with _lock:
                _problems[key] = str(exc)
            _log.warning("配置项不合法，已用默认值：%s", exc)
    default = _default(entry)
    return _parse(entry, default) if isinstance(default, str) and default != "" and entry.kind in ("int", "bool") else default


def problems() -> dict[str, str]:
    """启动以来发现的不合法配置（键 → 原因），运维诊断用。"""
    with _lock:
        return dict(_problems)


def _display(entry: ConfigItem, current: Any, source: str) -> str:
    if entry.secret:
        if source == "default" or current in (None, ""):
            return ""
        return f"cred://{source}/{entry.key}"
    return "" if current is None else str(current)


def items(effect: str | None = None, q: str | None = None) -> list[dict]:
    """SY-14 的 ConfigItem 列表，按键名排序；密钥只给引用。"""
    needle = (q or "").strip().lower()
    result = []
    for key in sorted(_items):
        entry = _items[key]
        if effect and entry.effect != effect:
            continue
        if needle and needle not in key.lower() and needle not in entry.description.lower():
            continue
        current, source = resolve(key)
        result.append({
            "key": key,
            "value": _display(entry, current, source),
            "source": source,
            **({"pack": entry.pack} if entry.pack else {}),
            "validation": entry.validation,
            "effect": entry.effect,
            "secret": entry.secret,
            "edit_page": entry.edit_page,
            "description": entry.description,
            "precedence": list(entry.precedence),
        })
    return result


def validate_all() -> dict[str, str]:
    """启动时把每一项都解析一遍：不合法的记日志（值已回落），返回问题清单。"""
    for key in list(_items):
        resolve(key)
    return problems()


# ---------------------------------------------------------------------------
# 内核配置项
# ---------------------------------------------------------------------------


def _config():
    from .. import config

    return config


def _model_settings_value(*path: str):
    def read():
        from ..services import model_settings

        data = model_settings.load()
        for part in path:
            data = data.get(part) if isinstance(data, dict) else None
        return data

    return read


def _runtime_value(name: str):
    def read():
        from ..services import runtime_settings

        return runtime_settings.load().get(name)

    return read


def _pool_value(name: str):
    def read():
        from . import ops_state

        return ops_state.pool_config().get(name)

    return read


def _context_value(name: str):
    def read():
        from ..services import model_settings

        return model_settings.context_config().get(name)

    return read


def _autobuild_value(name: str):
    def read():
        from ..services.autobuild import limits

        return limits.stored().get(name)

    return read


C = ConfigItem
declare(
    # 部署与路径
    C("ONTO_WEB_DATA_DIR", lambda: str(_config().BASE_DIR / "data"), "path", description="数据目录"),
    C("ONTO_WEB_ASSETS_DIR", lambda: str(_config().PACKAGE_ROOT / "assets"), "path", description="内核资产目录"),
    C("ONTO_WEB_STATIC_DIR", lambda: str(_config().PACKAGE_ROOT / "frontend" / "dist"), "path", description="前端构建产物"),
    C("ONTO_WEB_DRAWIO_DIR", lambda: str(_config().PACKAGE_ROOT / "vendor" / "drawio" / "webapp"), "path",
      description="drawio 静态页"),
    C("ONTO_WEB_HOST", "127.0.0.1", description="业务面监听地址"),
    C("ONTO_WEB_PORT", "8100", "int", minimum=1, maximum=65535, description="业务面监听端口"),
    C("ONTO_WEB_RELOAD", "1", "bool", description="开发热重载（容器内为 0）"),
    C("ONTO_WEB_LOG_LEVEL", "INFO", "enum", choices=("DEBUG", "INFO", "WARNING", "ERROR"), description="日志级别"),
    # 运维管理面（E8）
    C("ONTO_WEB_OPS_HOST", "127.0.0.1", description="运维监听地址（同进程第二个监听）"),
    C("ONTO_WEB_OPS_PORT", "8101", "int", minimum=1, maximum=65535, allow_zero=True,
      description="运维监听端口，0 不起运维监听"),
    C("ONTO_WEB_OPS_ON_MAIN", "false", "bool", description="业务面也处理 /api/v1/ops（默认 404）"),
    C("ONTO_WEB_OPS_BASE_URL", "", description="前端访问运维面的地址（C-01 ops_base_url），空表示同源反代"),
    # 鉴权
    C("ONTO_WEB_AUTH_URL", "", "url", description="auth-server 地址，空为本地模式"),
    C("ONTO_WEB_AUTH_ISSUER", lambda: os.environ.get("ONTO_WEB_AUTH_URL", "").rstrip("/"), description="JWT 签发方"),
    C("ONTO_WEB_AUTH_AUDIENCE", "knowledge-scaffold", description="JWT 受众"),
    C("ONTO_WEB_AUTH_ADMIN_TOKEN", "", secret=True, description="auth-server 管理令牌（用户管理）"),
    # 会话与权限
    C("ONTO_WEB_PERMISSION_TIMEOUT", "600", "int", minimum=30, maximum=86400, effect="new_session",
      description="确认卡等待超时（秒）"),
    C("ONTO_WEB_PERMISSION_MODE", "bypassPermissions", "enum",
      choices=("bypassPermissions", "default", "acceptEdits", "plan"), effect="new_session", edit_page="sys",
      precedence=UI_FIRST, data_dir=_runtime_value("permission_mode"), description="会话权限模式"),
    C("ONTO_WEB_MAX_THINKING_TOKENS", "", effect="new_session", description="扩展思考 token 上限，0 关闭"),
    C("ONTO_WEB_MAX_SESSIONS", "12", "int", minimum=1, maximum=64, effect="new_session", edit_page="ops-ctl",
      precedence=UI_FIRST, data_dir=_pool_value("max_sessions"), description="活跃会话上限"),
    C("ONTO_WEB_SESSION_IDLE_TTL", "600", "int", minimum=60, maximum=86400, allow_zero=True, effect="immediate",
      edit_page="ops-ctl", precedence=UI_FIRST, data_dir=_pool_value("idle_ttl_s"), description="空闲会话回收阈值（秒）"),
    C("ONTO_WEB_SESSION_REAPER_INTERVAL", "30", "int", minimum=5, maximum=3600, description="空闲回收巡检间隔（秒）"),
    C("ONTO_WEB_SESSION_STALL_TIMEOUT", "1800", "int", effect="immediate",
      description="运行中多久没有引擎事件判定执行超时（秒，0 或负数关闭）"),
    C("ONTO_WEB_CLI_PATH", "", "path", effect="new_session", description="降级兼容包的外部 CLI 路径"),
    # 自主构建（方案 B13）：平台同时运行的上限（1–16），超出的排队；0 不合法（启动时报配置错误、回落默认值）
    C("ONTO_WEB_AUTOBUILD_MAX_RUNS", "2", "int", minimum=1, maximum=16, effect="immediate", edit_page="sys",
      precedence=UI_FIRST, data_dir=_autobuild_value("max_concurrent_runs"), description="自主构建同时运行上限"),
    # 稳定引擎（Claude）部署机兜底
    C("ANTHROPIC_BASE_URL", "", "url", effect="next_message", edit_page="sys",
      precedence=UI_FIRST, data_dir=_model_settings_value("base_url"), description="稳定引擎模型网关"),
    C("ANTHROPIC_API_KEY", "", secret=True, effect="next_message", edit_page="sys",
      precedence=UI_FIRST, data_dir=_model_settings_value("api_key"), description="稳定引擎模型 Key"),
    # 内测引擎（Jiuwen）
    C("ONTO_WEB_JIUWEN_BASE_URL", "", "url", effect="next_message", edit_page="sys", precedence=UI_FIRST,
      data_dir=_model_settings_value("jiuwen", "base_url"), description="内测引擎模型网关"),
    C("ONTO_WEB_JIUWEN_API_KEY", "", secret=True, effect="next_message", edit_page="sys", precedence=UI_FIRST,
      data_dir=_model_settings_value("jiuwen", "api_key"), description="内测引擎模型 Key"),
    C("ONTO_WEB_JIUWEN_MODEL", "", effect="next_message", edit_page="sys", precedence=UI_FIRST,
      data_dir=_model_settings_value("jiuwen", "model"), description="内测引擎模型"),
    C("ONTO_WEB_JIUWEN_MAX_TOKENS", "32000", "int", minimum=256, maximum=1_000_000, effect="new_session",
      description="内测引擎单次输出上限"),
    C("ONTO_WEB_JIUWEN_MAX_ITERATIONS", "60", "int", minimum=1, maximum=1000, effect="new_session",
      description="内测引擎单轮最大迭代"),
    C("ONTO_WEB_JIUWEN_EXTRA_BODY", '{"thinking_budget": 16000}', effect="new_session",
      description="内测引擎每次调用附带的请求参数（JSON；默认给思考封顶 16000 token，空 = 不附带）"),
    C("ONTO_WEB_JIUWEN_QUIET_EXTRA_BODY", '{"enable_thinking": false}', effect="new_session",
      description="内测引擎关闭思考时附带的请求参数（JSON；agent 文件标了不思考的子任务用，空 = 不关）"),
    C("ONTO_WEB_JIUWEN_STALL_SECONDS", "900", "int", minimum=0, maximum=86_400, effect="new_session",
      description="内测引擎一轮里多久没有任何动静按出错收尾（0 关闭）"),
    C("ONTO_WEB_JIUWEN_WORKER", "true", "bool", effect="restart",
      description="内测引擎会话放在独立 worker 进程（出错不拖垮内核，方案 B11）；false 退回进程内"),
    # 上下文参数（配置页 > .env > 内置默认）
    C("ONTO_WEB_CONTEXT_WINDOW", "", "int", minimum=1_000, maximum=10_000_000, effect="next_message", edit_page="sys",
      precedence=UI_FIRST, data_dir=_context_value("max_context_tokens"), description="上下文窗口"),
    C("ONTO_WEB_CONTEXT_MAX_OUTPUT_TOKENS", "", "int", minimum=1_000, maximum=1_000_000, effect="next_message",
      edit_page="sys", precedence=UI_FIRST, data_dir=_context_value("max_output_tokens"), description="最大输出"),
    C("ONTO_WEB_CONTEXT_COMPACT_WINDOW", "", "int", minimum=100_000, maximum=1_000_000, effect="next_message",
      description="自动压缩窗口"),
    C("ONTO_WEB_CONTEXT_COMPACT_PCT", "", "int", minimum=1, maximum=100, effect="next_message",
      description="自动压缩触发百分比"),
    # MCP、沙箱
    C("ONTO_WEB_MCP_PUBLIC_BASE_URL", "", "url", effect="immediate", description="项目级 MCP 的公开地址"),
    C("ONTO_WEB_SANDBOX_GATEWAY_URL", "", "url", effect="immediate", description="脚本验证沙箱网关"),
    C("ONTO_WEB_SANDBOX_GATEWAY_PORT", "8110", "int", minimum=1, maximum=65535, description="沙箱网关监听端口"),
    C("ONTO_WEB_CONNECTOR_GATEWAY_URL", "", "url", effect="immediate",
      description="连接器网关（资料转文本，ADR-0073）；没配时资料停在等待转换"),
    # 备份
    C("ONTO_WEB_BACKUP_INTERVAL_HOURS", "24", "int", minimum=1, maximum=720, allow_zero=True,
      description="定期备份间隔（小时），0 关闭"),
    C("ONTO_WEB_BACKUP_DIR", "", "path", description="备份目录（建议独立卷）"),
    C("ONTO_WEB_KEY_BACKUP_DIR", "", "path", description="主密钥单独备份的目录（与密文分开放）"),
    # 密钥与 agent 身份（E10）
    C("ONTO_WEB_MODEL_SECRET_KEY", "", secret=True, min_bytes=32, description="个人 Key 主密钥（必须配置）"),
    C("ONTO_WEB_SECRETS_DIR", "", "path", description="密钥目录（agent 读不到），空为数据目录下的 secrets/"),
    C("ONTO_WEB_AGENT_USER", "", description="agent 及其工具子进程使用的系统用户，空为不切换（开发机）"),
)

# 独立评测服务（ADR-0092）：评测在评测服务里跑，onto 只给入口、收完成通知、以当前用户登录读回结果
declare(
    C("ONTO_WEB_EVAL_PUBLIC_URL", "", "url", effect="immediate",
      description="评测服务的浏览器入口，空为建模页面同主机的 5180 端口"),
    C("ONTO_WEB_EVAL_URL", "", "url", effect="immediate",
      description="评测服务的容器网络地址，空时已发布版本不显示评测报告与优化建议"),
    C("ONTO_EVALUATION_SERVICE_TOKEN", "", secret=True, effect="immediate",
      description="评测服务发完成通知用的服务凭据，空时拒收通知"),
)
