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


def jiuwen_config() -> dict:
    """Jiuwen 引擎读取用的明文配置（仅服务端内部使用，不经接口外发）。"""
    section = load().get("jiuwen") or {}
    return {
        "base_url": section.get("base_url", ""),
        "api_key": section.get("api_key", ""),
        "model": section.get("model", ""),
    }


def session_env() -> dict:
    """转成会话子进程的环境变量；空值不注入。

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
    return env
