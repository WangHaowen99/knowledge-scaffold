"""个人模型配置：用户用自己的 API Key 覆盖管理员配置（按引擎分组）。

- 管理员配置（model_settings）保持原样，是所有未配个人 Key 的用户的默认值；
  个人配置只影响该用户自己发起的会话轮次。
- 生效条件：鉴权已启用、管理员未关闭个人覆盖、该用户该引擎存了个人 Key。
  只填网关地址或模型而不填 Key 不允许保存——否则会把管理员 Key 发往用户自填的地址。
- 网关地址 / 模型留空时沿用管理员配置；Key 永不回落到管理员 Key。
- Key 以 AES-GCM 加密落盘（附加数据绑定用户名 + 引擎，密文不能挪给别人用）。
  主密钥取 ONTO_WEB_MODEL_SECRET_KEY；未配置时在 DATA_DIR 自动生成（0600）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from pathlib import Path

from .. import config
from . import auth

ENGINES = ("claude", "jiuwen")
_CIPHER_PREFIX = "enc:v1:"
_MANAGED_KEY_FILE = ".model-secret-key"
_MIN_MASTER_KEY_BYTES = 32


class PersonalConfigError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ---- 主密钥与加解密 ----


def _master_key() -> bytes:
    configured = os.environ.get("ONTO_WEB_MODEL_SECRET_KEY", "")
    if configured.strip():
        if len(configured.encode("utf-8")) < _MIN_MASTER_KEY_BYTES:
            raise PersonalConfigError("ONTO_WEB_MODEL_SECRET_KEY 至少 32 字节", 503)
        return hashlib.sha256(configured.encode("utf-8")).digest()
    path = Path(config.DATA_DIR) / _MANAGED_KEY_FILE
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # O_EXCL：并发首次写入时只有一方生成，另一方读取对方的结果
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(secrets.token_urlsafe(32))
        except FileExistsError:
            pass
    master = path.read_text(encoding="utf-8").strip()
    if len(master.encode("utf-8")) < _MIN_MASTER_KEY_BYTES:
        raise PersonalConfigError("个人 Key 加密密钥文件已损坏，请联系管理员", 503)
    return hashlib.sha256(master.encode("utf-8")).digest()


def _aad(username: str, engine: str) -> bytes:
    return f"onto-personal-key\0{username}\0{engine}".encode("utf-8")


def _encrypt(plain: str, username: str, engine: str) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = secrets.token_bytes(12)
    sealed = AESGCM(_master_key()).encrypt(
        nonce, plain.encode("utf-8"), _aad(username, engine)
    )
    return _CIPHER_PREFIX + base64.urlsafe_b64encode(nonce + sealed).decode("ascii")


def _decrypt(token: str, username: str, engine: str) -> str:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not token.startswith(_CIPHER_PREFIX):
        raise PersonalConfigError("个人 API Key 存储格式无效，请重新保存", 409)
    try:
        raw = base64.urlsafe_b64decode(token[len(_CIPHER_PREFIX):])
        plain = AESGCM(_master_key()).decrypt(raw[:12], raw[12:], _aad(username, engine))
    except (InvalidTag, ValueError) as exc:
        raise PersonalConfigError(
            "个人 API Key 无法解密（加密密钥可能已更换），请在「我的模型配置」重新保存", 409
        ) from exc
    return plain.decode("utf-8")


# ---- 存储 ----


def _file() -> Path:
    return Path(config.DATA_DIR) / "user-model-settings.json"


def _load() -> dict:
    path = _file()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _write(data: dict) -> None:
    path = _file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    path.chmod(0o600)


def _section(username: str, engine: str) -> dict:
    return ((_load().get("users") or {}).get(username) or {}).get(engine) or {}


def _check_engine(engine: str) -> None:
    if engine not in ENGINES:
        raise PersonalConfigError("未知引擎")


# ---- 生效判定与解析（会话层 / 引擎使用） ----


def allowed() -> bool:
    """个人覆盖是否可用：需启用鉴权（有用户身份）且管理员未关闭。"""
    if not auth.ENABLED:
        return False
    from . import model_settings

    return model_settings.personal_override_allowed()


def model_owner(username: str, engine: str) -> str:
    """该用户在该引擎下实际使用谁的模型配置：自己的用户名，或 ""（管理员配置）。"""
    if not username or engine not in ENGINES or not allowed():
        return ""
    return username if _section(username, engine).get("api_key") else ""


def resolve(owner: str, engine: str) -> dict:
    """个人配置明文（仅服务端内部使用）：base_url / model 可为空表示沿用管理员配置。"""
    _check_engine(engine)
    section = _section(owner, engine)
    token = section.get("api_key") or ""
    if not token:
        raise PersonalConfigError("个人 API Key 已被清除", 409)
    return {
        "base_url": section.get("base_url", ""),
        "model": section.get("model", ""),
        "api_key": _decrypt(token, owner, engine),
    }


# ---- 用户自助接口 ----


def masked(username: str) -> dict:
    from . import model_settings

    users = _load().get("users") or {}
    mine = users.get(username) or {}
    result = {"enabled": allowed(), "auth": auth.ENABLED}
    for engine in ENGINES:
        section = mine.get(engine) or {}
        has_key = bool(section.get("api_key"))
        result[engine] = {
            "base_url": section.get("base_url", ""),
            "model": section.get("model", ""),
            # 掩码不解密：只表明存在，不回显任何字符
            "api_key_masked": "已设置" if has_key else "",
            "active": has_key and result["enabled"],
        }
    # 用户可见的管理员默认值只到“是否已配置”，不暴露地址与 Key
    admin = model_settings.masked()
    result["claude"]["admin_configured"] = bool(admin["configured"])
    result["jiuwen"]["admin_configured"] = bool(admin["jiuwen"]["configured"])
    return result


def save(username: str, engine: str, base_url: str, api_key: str, model: str) -> dict:
    _check_engine(engine)
    if not auth.ENABLED or not username:
        raise PersonalConfigError("当前部署未启用登录鉴权，不支持个人模型配置")
    if not allowed():
        raise PersonalConfigError("管理员已关闭个人模型配置", 403)
    base_url = (base_url or "").strip()
    model = (model or "").strip()
    api_key = (api_key or "").strip()
    if base_url and not base_url.startswith(("http://", "https://")):
        raise PersonalConfigError("网关地址必须以 http(s):// 开头")
    data = _load()
    users = data.setdefault("users", {})
    section = users.setdefault(username, {}).setdefault(engine, {})
    if not api_key and not section.get("api_key"):
        # 管理员 Key 不能跟着用户自填的网关地址走，所以个人配置必须带自己的 Key
        raise PersonalConfigError("个人配置必须填写自己的 API Key")
    section["base_url"] = base_url
    section["model"] = model
    if api_key:  # 留空表示不修改既有 Key
        section["api_key"] = _encrypt(api_key, username, engine)
    _write(data)
    return masked(username)


def clear(username: str, engine: str) -> dict:
    """清除该引擎的个人配置，回落到管理员配置。"""
    _check_engine(engine)
    data = _load()
    mine = (data.get("users") or {}).get(username)
    if mine and engine in mine:
        mine.pop(engine)
        if not mine:
            data["users"].pop(username)
        _write(data)
    return masked(username)


def delete_user(username: str) -> None:
    """用户被删除时清掉其全部个人配置。"""
    data = _load()
    if username in (data.get("users") or {}):
        data["users"].pop(username)
        _write(data)
