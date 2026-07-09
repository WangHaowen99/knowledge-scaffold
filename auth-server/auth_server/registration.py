"""开放注册流程：pending 存储（重启不丢）+ 邮箱验证码 + 进程内固定窗口限流。

验证码存哈希不存明文（salted SHA-256；码空间 10^6 且尝试上限 5 次，无需慢哈希）。
密码在注册提交时即做 PBKDF2 哈希落 pending，验证通过后原样写入正式用户记录。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from .mailer import Mailer
from .policy import validate_password_policy
from .users import UserStore, UserView, normalize_email

logger = logging.getLogger(__name__)

CODE_TTL_SECONDS = 15 * 60
RESEND_COOLDOWN_SECONDS = 60
MAX_VERIFY_ATTEMPTS = 5
RATE_LIMIT_WINDOW_SECONDS = 3600
RATE_LIMIT_MAX_REQUESTS = 5

PENDING_FILENAME = "pending_registrations.json"

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_EMAIL_MAX_LENGTH = 254


class RegistrationRateLimited(Exception):
    """请求超过固定窗口限额或重发冷却，路由层映射为 429。"""


def _new_code() -> str:
    return "{:06d}".format(secrets.randbelow(1_000_000))


def _hash_code(code: str) -> str:
    salt = secrets.token_hex(8)
    digest = hashlib.sha256("{}:{}".format(salt, code).encode("utf-8")).hexdigest()
    return "sha256${}${}".format(salt, digest)


def _verify_code(code: str, code_hash: str) -> bool:
    try:
        algo, salt, digest = str(code_hash or "").split("$", 2)
    except ValueError:
        return False
    if algo != "sha256":
        return False
    actual = hashlib.sha256("{}:{}".format(salt, code).encode("utf-8")).hexdigest()
    return hmac.compare_digest(actual, digest)


class _FixedWindowRateLimiter:
    """进程内固定窗口计数器；register 与 resend 共享同一份按 IP / 按 email 的额度。"""

    _PRUNE_THRESHOLD = 4096

    def __init__(self, max_requests: int, window_seconds: int) -> None:
        self._max = max_requests
        self._window = window_seconds
        self._counts: Dict[str, tuple[int, int]] = {}
        self._lock = threading.Lock()

    def check(self, *keys: str) -> bool:
        """对每个 key 计一次数；任一 key 已达上限则返回 False。"""
        window = int(time.time()) // self._window
        with self._lock:
            if len(self._counts) > self._PRUNE_THRESHOLD:
                self._counts = {k: v for k, v in self._counts.items() if v[0] == window}
            allowed = True
            for key in keys:
                start, count = self._counts.get(key, (window, 0))
                if start != window:
                    start, count = window, 0
                if count >= self._max:
                    allowed = False
                self._counts[key] = (start, count + 1)
            return allowed


class PendingStore:
    """pending_registrations.json：每次操作直读直写（0600 原子写），读写时清理过期项。"""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()

    def _read(self) -> tuple[Dict[str, Dict[str, Any]], bool]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}, False
        except (ValueError, OSError):
            logger.warning("pending 注册文件损坏，已忽略: %s", self._path)
            return {}, True
        now = int(time.time())
        pending: Dict[str, Dict[str, Any]] = {}
        cleaned = False
        for entry in data.get("pending", []):
            email = normalize_email(entry.get("email"))
            try:
                expires_at = int(entry.get("expires_at") or 0)
            except (TypeError, ValueError):
                expires_at = 0
            if not email or expires_at <= now:
                cleaned = True
                continue
            pending[email] = dict(entry)
        return pending, cleaned

    def _write(self, pending: Dict[str, Dict[str, Any]]) -> None:
        payload = json.dumps(
            {"pending": list(pending.values())}, ensure_ascii=False, indent=2
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._path.with_name(
            ".{}.{}.tmp".format(self._path.name, uuid.uuid4().hex)
        )
        temp_path.write_text(payload, encoding="utf-8")
        try:
            temp_path.chmod(0o600)
        except OSError:
            pass
        os.replace(temp_path, self._path)

    def get(self, email: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            pending, cleaned = self._read()
            if cleaned:
                self._write(pending)
            record = pending.get(normalize_email(email))
            return dict(record) if record else None

    def put(self, email: str, record: Dict[str, Any]) -> None:
        with self._lock:
            pending, _ = self._read()
            pending[normalize_email(email)] = dict(record)
            self._write(pending)

    def delete(self, email: str) -> None:
        with self._lock:
            pending, cleaned = self._read()
            removed = pending.pop(normalize_email(email), None)
            if removed is not None or cleaned:
                self._write(pending)

    def has_username(self, username: str) -> bool:
        with self._lock:
            pending, _ = self._read()
            return any(
                str(record.get("username") or "") == username
                for record in pending.values()
            )


class RegistrationService:
    def __init__(self, user_store: UserStore, mailer: Mailer, data_dir: Path) -> None:
        self._store = user_store
        self._mailer = mailer
        self._pending = PendingStore(Path(data_dir) / PENDING_FILENAME)
        self._limiter = _FixedWindowRateLimiter(
            RATE_LIMIT_MAX_REQUESTS, RATE_LIMIT_WINDOW_SECONDS
        )

    # ------------------------------------------------------------------ 步骤

    def register(self, username: str, email: str, password: str, client_ip: str) -> None:
        email = normalize_email(email)
        self._check_rate(client_ip, email)

        username = str(username or "").strip()
        if not _USERNAME_RE.match(username):
            raise ValueError("用户名格式不合法：3-32 位字母、数字、下划线、点或连字符")
        if not email or len(email) > _EMAIL_MAX_LENGTH or not _EMAIL_RE.match(email):
            raise ValueError("邮箱格式不合法")
        if self._store.get_user(username):
            raise ValueError(f"用户 {username} 已存在")
        if self._store.get_user_by_email(email):
            raise ValueError("邮箱已被注册")
        if self._pending.get(email):
            raise ValueError("该邮箱已提交注册，请查收验证码邮件或稍后重试")
        if self._pending.has_username(username):
            raise ValueError(f"用户 {username} 已存在")
        validate_password_policy(username, password)

        code = _new_code()
        now = int(time.time())
        self._pending.put(
            email,
            {
                "username": username,
                "email": email,
                "password_hash": UserStore.hash_password(str(password)),
                "code_hash": _hash_code(code),
                "expires_at": now + CODE_TTL_SECONDS,
                "attempts": 0,
                "last_sent_at": now,
            },
        )
        self._send_code(email, code)

    def verify(self, email: str, code: str) -> UserView:
        email = normalize_email(email)
        record = self._pending.get(email)
        if not record:
            raise ValueError("验证码无效或已过期")
        attempts = int(record.get("attempts") or 0)
        if attempts >= MAX_VERIFY_ATTEMPTS:
            raise ValueError("验证码错误次数过多，请重新注册")
        if not _verify_code(str(code or "").strip(), str(record.get("code_hash") or "")):
            record["attempts"] = attempts + 1
            self._pending.put(email, record)
            raise ValueError("验证码错误")
        user = self._store.create_registered_user(
            str(record.get("username") or ""),
            email,
            str(record.get("password_hash") or ""),
        )
        self._pending.delete(email)
        return user

    def resend(self, email: str, client_ip: str) -> None:
        email = normalize_email(email)
        self._check_rate(client_ip, email)
        record = self._pending.get(email)
        if not record:
            raise ValueError("未找到待验证的注册，请重新注册")
        now = int(time.time())
        try:
            last_sent_at = int(record.get("last_sent_at") or 0)
        except (TypeError, ValueError):
            last_sent_at = 0
        if now - last_sent_at < RESEND_COOLDOWN_SECONDS:
            raise RegistrationRateLimited()
        code = _new_code()
        record.update(
            code_hash=_hash_code(code),
            expires_at=now + CODE_TTL_SECONDS,
            attempts=0,
            last_sent_at=now,
        )
        self._pending.put(email, record)
        self._send_code(email, code)

    # ------------------------------------------------------------------ 内部

    def _check_rate(self, client_ip: str, email: str) -> None:
        keys = ["ip:" + str(client_ip or "unknown")]
        if email:
            keys.append("email:" + email)
        if not self._limiter.check(*keys):
            raise RegistrationRateLimited()

    def _send_code(self, email: str, code: str) -> None:
        self._mailer.send(
            email,
            "meta-wise 注册验证码",
            "您的注册验证码是：{}，{} 分钟内有效。如非本人操作请忽略本邮件。".format(
                code, CODE_TTL_SECONDS // 60
            ),
        )
