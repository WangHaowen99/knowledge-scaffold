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


def masked() -> dict:
    data = load()
    key = data.get("api_key", "")
    return {
        "base_url": data.get("base_url", ""),
        "model": data.get("model", ""),
        "api_key_masked": (key[:6] + "…" + key[-4:]) if len(key) > 12 else ("已设置" if key else ""),
        "configured": bool(key or data.get("base_url")),
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


def session_env() -> dict:
    """转成会话子进程的环境变量；空值不注入。"""
    data = load()
    env = {}
    if data.get("base_url"):
        env["ANTHROPIC_BASE_URL"] = data["base_url"]
    if data.get("api_key"):
        env["ANTHROPIC_API_KEY"] = data["api_key"]
    if data.get("model"):
        env["ANTHROPIC_MODEL"] = data["model"]
    return env
