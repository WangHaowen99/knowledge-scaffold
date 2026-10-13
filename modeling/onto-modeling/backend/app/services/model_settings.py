"""模型接入配置：网关地址与 API Key，存数据目录（0600），注入会话环境。

未配置时回落到部署机的 ~/.claude 配置（现状行为）。
Key 只在保存时接收明文，读取接口一律返回掩码。
"""

from __future__ import annotations

import json
from pathlib import Path

from .. import config


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
        # 上下文参数留空时实际生效的默认值，供配置页提示与阈值估算
        "context_defaults": {"max_context_tokens": DEFAULT_MAX_CONTEXT_TOKENS},
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


def save(base_url: str, api_key: str, model: str) -> dict:
    base_url = (base_url or "").strip()
    model = (model or "").strip()
    api_key = (api_key or "").strip()
    if base_url and not base_url.startswith(("http://", "https://")):
        raise ModelConfigError("网关地址必须以 http(s):// 开头")
    data = load()
    data["base_url"] = base_url
    data["model"] = model
    if api_key:  # 留空表示不修改既有 Key
        data["api_key"] = api_key
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


# 稳定引擎的上下文与自动压缩参数：字段 → (CLI 环境变量, 下限, 上限)。
# 网关后接的模型窗口与 CLI 内置假定不一致时，CLI 会按假定窗口计算压缩阈值，
# 输入加输出预留先超过真实窗口，请求被网关以 400 拒绝而压缩始终不触发。
# 压缩窗口的 100k–1M 区间是 CLI 自身接受的范围。
CONTEXT_FIELDS = {
    "max_context_tokens": ("CLAUDE_CODE_MAX_CONTEXT_TOKENS", 1_000, 10_000_000),
    "auto_compact_window": ("CLAUDE_CODE_AUTO_COMPACT_WINDOW", 100_000, 1_000_000),
    "max_output_tokens": ("CLAUDE_CODE_MAX_OUTPUT_TOKENS", 1_000, 1_000_000),
    "autocompact_pct": ("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", 1, 100),
}

# 模型真实窗口未配置时注入的默认值。CLI 对不认识的模型名一律按 200K 计算，
# 接入的模型以 1M 窗口为主，故默认按 1M；窗口更小的模型必须由管理员填写，
# 否则压缩点落在真实窗口之外。CLI 只对不认识的模型名读取该变量，
# Claude 官方模型名仍按 CLI 内置窗口。
DEFAULT_MAX_CONTEXT_TOKENS = 1_000_000


def context_config() -> dict:
    section = load().get("claude_context") or {}
    return {name: section.get(name) for name in CONTEXT_FIELDS}


def save_context(values: dict) -> dict:
    """保存上下文与压缩参数；None 表示不设置：模型真实窗口按默认值，其余交由 CLI 默认。"""
    section = {}
    for name, (_, low, high) in CONTEXT_FIELDS.items():
        value = values.get(name)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ModelConfigError(f"{name} 须为 {low}–{high} 之间的整数")
        section[name] = value
    real_window = section.get("max_context_tokens", DEFAULT_MAX_CONTEXT_TOKENS)
    if section.get("auto_compact_window", 0) > real_window:
        raise ModelConfigError("压缩窗口不能大于模型真实窗口")
    if section.get("max_output_tokens", 0) >= real_window:
        raise ModelConfigError("最大输出必须小于模型真实窗口")
    data = load()
    data["claude_context"] = section
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
    """转成会话子进程的环境变量；空值不注入，模型真实窗口除外（未配置时注入默认值）。

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
    context = context_config()
    if context["max_context_tokens"] is None:
        context["max_context_tokens"] = DEFAULT_MAX_CONTEXT_TOKENS
    for name, value in context.items():
        if value is not None:
            env[CONTEXT_FIELDS[name][0]] = str(value)
    return env
