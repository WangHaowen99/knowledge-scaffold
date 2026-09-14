# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""开放注册流程：一步直接建号 + 进程内固定窗口限流（按 IP / 按 email）。

校验通过（用户名格式 / 邮箱格式 / 密码策略 / 唯一性）后即调
UserStore.create_registered_user 创建正式用户，不再有验证码 / pending / 邮件环节。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path
from typing import Dict

from .policy import validate_password_policy
from .users import UserStore, UserView, normalize_email

logger = logging.getLogger(__name__)

RATE_LIMIT_WINDOW_SECONDS = 3600
RATE_LIMIT_MAX_REQUESTS = 5

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_EMAIL_MAX_LENGTH = 254


class RegistrationRateLimited(Exception):
    """请求超过固定窗口限额，路由层映射为 429。"""


class _FixedWindowRateLimiter:
    """进程内固定窗口计数器；按 IP / 按 email 分别计额。"""

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


class RegistrationService:
    def __init__(self, user_store: UserStore, data_dir: Path) -> None:
        self._store = user_store
        self._data_dir = Path(data_dir)
        self._limiter = _FixedWindowRateLimiter(
            RATE_LIMIT_MAX_REQUESTS, RATE_LIMIT_WINDOW_SECONDS
        )

    def register(
        self, username: str, email: str, password: str, client_ip: str
    ) -> UserView:
        email = normalize_email(email)
        self._check_rate(client_ip, email)

        username = str(username or "").strip()
        if not _USERNAME_RE.match(username):
            raise ValueError("用户名格式不合法：3-32 位字母、数字、下划线、点或连字符")
        if not email or len(email) > _EMAIL_MAX_LENGTH or not _EMAIL_RE.match(email):
            raise ValueError("邮箱格式不合法")
        # 提前查一次给出更明确的中文错误；create_registered_user 内还会再兜底一次唯一性/上限。
        if self._store.get_user(username):
            raise ValueError(f"用户 {username} 已存在")
        if self._store.get_user_by_email(email):
            raise ValueError("邮箱已被注册")
        validate_password_policy(username, password)

        return self._store.create_registered_user(
            username, email, UserStore.hash_password(str(password))
        )

    def _check_rate(self, client_ip: str, email: str) -> None:
        keys = ["ip:" + str(client_ip or "unknown")]
        if email:
            keys.append("email:" + email)
        if not self._limiter.check(*keys):
            raise RegistrationRateLimited()
