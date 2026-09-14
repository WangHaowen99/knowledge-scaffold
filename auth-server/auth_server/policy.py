# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""密码策略：从 consumer AuthManager.validate_password_policy 平移，违规代码一字不差。"""

from __future__ import annotations

from typing import Optional

_WEAK_PASSWORDS = {
    "123456",
    "12345678",
    "admin",
    "admin123",
    "password",
    "password123",
    "qwerty",
    "qwerty123",
}


class PasswordPolicyError(ValueError):
    def __init__(self, violations: list[str]) -> None:
        self.violations = violations
        super().__init__("密码复杂度不满足要求")


def validate_password_policy(
    username: str,
    password: str,
    *,
    old_password: Optional[str] = None,
) -> None:
    value = str(password or "")
    violations: list[str] = []
    if value != value.strip():
        violations.append("surrounding_whitespace")
    if len(value) < 8:
        violations.append("min_length")
    if len(value) > 128:
        violations.append("max_length")
    normalized_username = str(username or "").strip().lower()
    if normalized_username and normalized_username in value.lower():
        violations.append("contains_username")
    classes = 0
    if any(ch.islower() for ch in value):
        classes += 1
    if any(ch.isupper() for ch in value):
        classes += 1
    if any(ch.isdigit() for ch in value):
        classes += 1
    if any(not ch.isalnum() for ch in value):
        classes += 1
    if classes < 3:
        violations.append("character_classes")
    if value.lower() in _WEAK_PASSWORDS:
        violations.append("common_password")
    if old_password is not None and value == old_password:
        violations.append("same_as_old")
    if violations:
        raise PasswordPolicyError(violations)
