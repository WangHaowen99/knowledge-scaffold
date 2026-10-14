"""模型接入配置：网关地址与 API Key，存数据目录（0600），注入会话环境。

未配置时回落到部署机的 ~/.claude 配置（现状行为）。
Key 只在保存时接收明文，读取接口一律返回掩码。
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from pathlib import Path

from .. import config

_log = logging.getLogger(__name__)


class ModelConfigError(Exception):
    pass


def _file() -> Path:
    return Path(config.DATA_DIR) / "model-settings.json"


def load() -> dict:
    path = _file()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _mask(key: str) -> str:
    return (key[:6] + "…" + key[-4:]) if len(key) > 12 else ("已设置" if key else "")


def masked() -> dict:
    from .engines import available_engines

    data = load()
    key = data.get("api_key", "")
    jiuwen = data.get("jiuwen") or {}
    return {
        "base_url": data.get("base_url", ""),
        "model": data.get("model", ""),
        "api_key_masked": _mask(key),
        "configured": bool(key or data.get("base_url")),
        "context": context_config(),
        # 上下文参数在配置页留空时实际生效的值及来源，供配置页提示
        "context_defaults": context_defaults(),
        # 新建/导入项目使用的默认引擎，以及各引擎在本部署的可用性
        "default_engine": default_engine(),
        "engines": available_engines(),
        # Jiuwen 引擎（OpenAI 兼容）配置：与 Anthropic 网关配置并列的第二组
        "jiuwen": {
            "base_url": jiuwen.get("base_url", ""),
            "model": jiuwen.get("model", ""),
            "api_key_masked": _mask(jiuwen.get("api_key", "")),
            "configured": bool(jiuwen.get("api_key") or jiuwen.get("base_url")),
        },
    }


def save(base_url: str, api_key: str, model: str, context: dict | None = None) -> dict:
    """保存 Anthropic 网关配置；带 context 时上下文参数一并保存，先全部校验再写，不会只存一半。"""
    base_url = (base_url or "").strip()
    model = (model or "").strip()
    api_key = (api_key or "").strip()
    if base_url and not base_url.startswith(("http://", "https://")):
        raise ModelConfigError("网关地址必须以 http(s):// 开头")
    context_section = _context_section(context) if context is not None else None
    data = load()
    data["base_url"] = base_url
    data["model"] = model
    if api_key:  # 留空表示不修改既有 Key
        data["api_key"] = api_key
    if context_section is not None:
        data.pop("claude_context", None)
        data["context"] = context_section
    path = _file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    path.chmod(0o600)
    return masked()


def clear_key() -> dict:
    data = load()
    data.pop("api_key", None)
    _file().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return masked()


def save_jiuwen(base_url: str, api_key: str, model: str) -> dict:
    """Jiuwen 引擎的 OpenAI 兼容模型配置（与 Anthropic 网关配置同文件存储）。"""
    base_url = (base_url or "").strip()
    model = (model or "").strip()
    api_key = (api_key or "").strip()
    if base_url and not base_url.startswith(("http://", "https://")):
        raise ModelConfigError("网关地址必须以 http(s):// 开头")
    data = load()
    section = data.setdefault("jiuwen", {})
    section["base_url"] = base_url
    section["model"] = model
    if api_key:  # 留空表示不修改既有 Key
        section["api_key"] = api_key
    path = _file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    path.chmod(0o600)
    return masked()


def default_engine() -> str:
    """新建/导入项目使用的默认引擎。

    未设置时回落内置默认；设过但当前部署已不可用（如镜像未装 openjiuwen）
    同样回落，避免建出来的项目一上手就起不了会话。
    """
    from .engines import DEFAULT_ENGINE, ENGINE_NAMES, available_engines

    name = (load().get("default_engine") or "").strip()
    if name not in ENGINE_NAMES or not available_engines().get(name):
        return DEFAULT_ENGINE
    return name


def save_default_engine(name: str) -> dict:
    """设置默认引擎；只接受已知且当前可用的引擎名。"""
    from .engines import ENGINE_NAMES, available_engines

    name = (name or "").strip()
    if name not in ENGINE_NAMES:
        raise ModelConfigError("未知引擎")
    if not available_engines().get(name):
        raise ModelConfigError("该引擎在当前部署不可用")
    data = load()
    data["default_engine"] = name
    path = _file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    path.chmod(0o600)
    return masked()


# 上下文与自动压缩参数：字段 → (部署级环境变量, 下限, 上限)。
# 环境变量名与会话引擎无关，由各引擎自行映射到内部参数。
# 网关后接的模型窗口与引擎内置假定不一致时，引擎会按假定窗口计算压缩阈值，
# 输入加输出预留先超过真实窗口，请求被网关以 400 拒绝而压缩始终不触发。
# 压缩窗口的 100k–1M 区间是稳定引擎 CLI 自身接受的范围。
CONTEXT_FIELDS = {
    "max_context_tokens": ("ONTO_WEB_CONTEXT_WINDOW", 1_000, 10_000_000),
    "auto_compact_window": ("ONTO_WEB_CONTEXT_COMPACT_WINDOW", 100_000, 1_000_000),
    "max_output_tokens": ("ONTO_WEB_CONTEXT_MAX_OUTPUT_TOKENS", 1_000, 1_000_000),
    "autocompact_pct": ("ONTO_WEB_CONTEXT_COMPACT_PCT", 1, 100),
}

# 配置页只填模型真实窗口与最大输出；压缩窗口与触发百分比只留 .env 给运维，
# 两者都没配时按前两项自动推算（见 derived_compaction）
PAGE_CONTEXT_FIELDS = ("max_context_tokens", "max_output_tokens")

# 稳定引擎 CLI 读取的对应环境变量
_CLI_CONTEXT_ENV = {
    "max_context_tokens": "CLAUDE_CODE_MAX_CONTEXT_TOKENS",
    "auto_compact_window": "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
    "max_output_tokens": "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
    "autocompact_pct": "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE",
}

# 配置页与 .env 都没给模型真实窗口时注入的默认值，与 CLI 对不认识的模型名的
# 假定一致。窗口更小的模型必须填写，否则压缩点落在真实窗口之外；窗口更大的
# 模型可填写以用满窗口。CLI 只对不认识的模型名读取该变量，
# Claude 官方模型名仍按 CLI 内置窗口。
DEFAULT_MAX_CONTEXT_TOKENS = 200_000

# 配置页与 .env 都没给最大输出时注入的默认值，与 CLI 对不认识的模型名的默认最大输出一致
# （CLI 对这类模型默认 32000、上限 128000，设得更大会被截到 128000）。
# 有了它才能按实际输出推算压缩参数；与窗口变量不同，CLI 对 Claude 官方模型名也读取该变量。
DEFAULT_MAX_OUTPUT_TOKENS = 32_000

_BUILTIN_CONTEXT_DEFAULTS = {
    "max_context_tokens": DEFAULT_MAX_CONTEXT_TOKENS,
    "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
}

# 稳定引擎 CLI 的自动压缩规则：有效窗口 = min(压缩窗口, 模型窗口) − min(最大输出, 20000)，
# 触发阈值 = 有效窗口 − 13000；设了触发百分比时取 min(有效窗口 × 百分比, 上式)
_CLI_OUTPUT_RESERVE_CAP = 20_000
_CLI_AUTOCOMPACT_BUFFER = 13_000
# 自动推算压缩参数的目标：压缩前的请求仍给模型真实窗口留至少 10% 余量；
# 压缩窗口向下取 10000 的倍数，触发百分比向下取 5 的倍数
_COMPACT_MARGIN_RATIO = 0.1
_COMPACT_WINDOW_STEP = 10_000
_COMPACT_PCT_STEP = 5


@lru_cache(maxsize=None)
def _parse_env_int(env_name: str, raw: str, low: int, high: int) -> int | None:
    # 按原始值缓存：同一个非法值只告警一次，不会每次组装会话环境都刷日志
    try:
        value = int(raw)
    except ValueError:
        value = None
    if value is None or not low <= value <= high:
        _log.warning("忽略 %s=%r：须为 %d–%d 之间的整数", env_name, raw, low, high)
        return None
    return value


def context_config() -> dict:
    """配置页保存的上下文参数；None 表示留空。

    旧版本保存的压缩窗口与触发百分比不再读取：界面已不展示，留着会悄悄覆盖自动推算。
    """
    data = load()
    # 旧版本存在 claude_context 下：读时兼容，下次保存迁移到 context
    section = data["context"] if "context" in data else data.get("claude_context")
    section = section or {}
    return {name: section.get(name) for name in PAGE_CONTEXT_FIELDS}


def context_defaults() -> dict:
    """配置页留空时生效的值与来源：.env（env）> 内置默认（default，模型真实窗口与最大输出有）。

    压缩窗口与触发百分比都没有时 value 与 source 为 None，按窗口与最大输出自动推算。
    非法的 .env 值忽略并告警。
    """
    result = {}
    for name, (env_name, low, high) in CONTEXT_FIELDS.items():
        raw = os.environ.get(env_name, "").strip()
        value = _parse_env_int(env_name, raw, low, high) if raw else None
        source = "env" if value is not None else None
        if value is None and name in _BUILTIN_CONTEXT_DEFAULTS:
            value, source = _BUILTIN_CONTEXT_DEFAULTS[name], "default"
        result[name] = {"value": value, "source": source, "env": env_name}
    return result


def effective_context() -> dict:
    """实际生效的上下文参数：配置页 > .env > 内置默认；None 表示交由引擎默认。

    压缩窗口与触发百分比只有 .env 一层，.env 都没配时按窗口与最大输出自动推算；
    配了任一项就完全按 .env，不再推算。
    """
    saved = context_config()
    result = {
        name: saved.get(name) if saved.get(name) is not None else fallback["value"]
        for name, fallback in context_defaults().items()
    }
    if result["auto_compact_window"] is None and result["autocompact_pct"] is None:
        result.update(derived_compaction(result["max_context_tokens"], result["max_output_tokens"]))
    return result


def derived_compaction(real_window: int, max_output: int | None) -> dict:
    """按模型真实窗口与最大输出反算压缩参数，使压缩前的请求仍留足余量。

    优先给压缩窗口；所需值低于 CLI 下限时改给触发百分比；引擎默认阈值已够、
    最大输出未知或大到留不出余量时都留空，交由引擎默认。
    """
    result = {"auto_compact_window": None, "autocompact_pct": None}
    if not max_output:
        return result
    threshold_cap = int(real_window * (1 - _COMPACT_MARGIN_RATIO)) - max_output
    reserve = min(max_output, _CLI_OUTPUT_RESERVE_CAP)
    window = (
        (threshold_cap + reserve + _CLI_AUTOCOMPACT_BUFFER)
        // _COMPACT_WINDOW_STEP
        * _COMPACT_WINDOW_STEP
    )
    if window >= real_window:
        return result
    _, window_min, window_max = CONTEXT_FIELDS["auto_compact_window"]
    if window >= window_min:
        result["auto_compact_window"] = min(window, window_max)
        return result
    effective = real_window - reserve
    if effective <= 0:
        return result
    pct = threshold_cap * 100 // effective // _COMPACT_PCT_STEP * _COMPACT_PCT_STEP
    if pct >= 1 and effective * pct // 100 < effective - _CLI_AUTOCOMPACT_BUFFER:
        result["autocompact_pct"] = pct
    return result


def _context_section(values: dict) -> dict:
    """校验配置页的模型真实窗口与最大输出，返回要存的 context 段；None 表示留空，按 .env 或默认值生效。"""
    section = {}
    for name in PAGE_CONTEXT_FIELDS:
        _, low, high = CONTEXT_FIELDS[name]
        value = values.get(name)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ModelConfigError(f"{name} 须为 {low}–{high} 之间的整数")
        section[name] = value
    # 留空项会取 .env 或默认值，校验按合并后的实际生效值
    merged = {name: item["value"] for name, item in context_defaults().items()} | section
    output = merged["max_output_tokens"]
    if (output or 0) >= merged["max_context_tokens"]:
        if "max_output_tokens" in section:
            raise ModelConfigError("最大输出必须小于上下文窗口")
        raise ModelConfigError(f"最大输出留空时按 {output} 生效，须小于上下文窗口，请填写最大输出")
    return section


def save_context(values: dict) -> dict:
    """只保存配置页的模型真实窗口与最大输出。"""
    section = _context_section(values)
    data = load()
    data.pop("claude_context", None)
    data["context"] = section
    path = _file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    path.chmod(0o600)
    return masked()


def jiuwen_config() -> dict:
    """Jiuwen 引擎读取用的明文配置（仅服务端内部使用，不经接口外发）。"""
    section = load().get("jiuwen") or {}
    return {
        "base_url": section.get("base_url", ""),
        "api_key": section.get("api_key", ""),
        "model": section.get("model", ""),
    }


def session_env() -> dict:
    """转成稳定引擎会话子进程的环境变量；空值不注入，模型真实窗口与最大输出总有值（见 effective_context）。

    key 同时覆盖 ANTHROPIC_API_KEY 与 ANTHROPIC_AUTH_TOKEN：CLI 优先用
    AUTH_TOKEN 做 Bearer 鉴权，若只覆盖 API_KEY，部署机 .env 里残留的
    旧令牌会继续生效，导致新网关 401（实测踩坑）。
    """
    data = load()
    env = {}
    if data.get("base_url"):
        env["ANTHROPIC_BASE_URL"] = data["base_url"]
    if data.get("api_key"):
        env["ANTHROPIC_API_KEY"] = data["api_key"]
        env["ANTHROPIC_AUTH_TOKEN"] = data["api_key"]
    if data.get("model"):
        env["ANTHROPIC_MODEL"] = data["model"]
    for name, value in effective_context().items():
        if value is not None:
            env[_CLI_CONTEXT_ENV[name]] = str(value)
    return env
