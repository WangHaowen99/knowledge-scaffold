"""users.json 存储：从 consumer AuthManager 平移存储/凭据部分（不含 token 逻辑）。

文件格式与 consumer 完全兼容：
{"users": [{username, password_hash, role, must_change_password,
            password_changed_at, password_version}]}
在此之上新增：
- token_version 字段（缺省视为 1），用于 refresh token 全量吊销；
- email 字段（可选，存量用户没有），开放注册用户必填且全局唯一。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from .policy import validate_password_policy

PASSWORD_HASH_ITERATIONS = 600_000


def normalize_email(email: Any) -> str:
    return str(email or "").strip().lower()


@dataclass(frozen=True)
class UserView:
    username: str
    role: str
    password_change_required: bool = False
    password_version: int = 1
    token_version: int = 1
    email: Optional[str] = None

    def to_public_dict(self) -> Dict[str, Any]:
        # 现有契约形状，不暴露 email；admin list 用 to_admin_dict
        return {
            "username": self.username,
            "role": self.role,
            "password_change_required": self.password_change_required,
        }

    def to_admin_dict(self) -> Dict[str, Any]:
        result = self.to_public_dict()
        if self.email:
            result["email"] = self.email
        return result


class UserStore:
    def __init__(self, users_file: str | Path, user_max_count: int = 10) -> None:
        self.users_file = Path(users_file)
        self._user_max_count = max(1, int(user_max_count))
        self._lock = threading.Lock()
        self._users_cache: Optional[Dict[str, Dict[str, Any]]] = None
        self._users_mtime: Optional[float] = None
        self.ensure_users_file()

    # ------------------------------------------------------------------ 哈希

    @staticmethod
    def hash_password(password: str, iterations: int = PASSWORD_HASH_ITERATIONS) -> str:
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        salt_b64 = base64.b64encode(salt).decode("ascii")
        digest_b64 = base64.b64encode(digest).decode("ascii")
        return f"pbkdf2_sha256${iterations}${salt_b64}${digest_b64}"

    @staticmethod
    def _verify_hash(password: str, password_hash: str) -> bool:
        try:
            algo, iterations_str, salt_b64, digest_b64 = password_hash.split("$", 3)
        except ValueError:
            return False
        if algo != "pbkdf2_sha256":
            return False
        try:
            iterations = int(iterations_str)
        except ValueError:
            return False
        salt = base64.b64decode(salt_b64.encode("ascii"))
        expected = base64.b64decode(digest_b64.encode("ascii"))
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return hmac.compare_digest(actual, expected)

    @staticmethod
    def _hash_iterations(password_hash: str) -> int:
        try:
            algo, iterations_str, _salt_b64, _digest_b64 = password_hash.split("$", 3)
        except ValueError:
            return 0
        if algo != "pbkdf2_sha256":
            return 0
        try:
            return int(iterations_str)
        except ValueError:
            return 0

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    # ---------------------------------------------------------------- 记录字段

    @staticmethod
    def _password_version(record: Dict[str, Any]) -> int:
        try:
            return max(1, int(record.get("password_version") or 1))
        except (TypeError, ValueError):
            return 1

    @staticmethod
    def _token_version(record: Dict[str, Any]) -> int:
        try:
            return max(1, int(record.get("token_version") or 1))
        except (TypeError, ValueError):
            return 1

    @classmethod
    def _password_change_required(cls, record: Dict[str, Any]) -> bool:
        if "must_change_password" in record:
            return bool(record.get("must_change_password"))
        return not bool(str(record.get("password_changed_at") or "").strip())

    def _user_view_from_record(self, username: str, record: Dict[str, Any]) -> UserView:
        return UserView(
            username=username,
            role=record.get("role", "user"),
            password_change_required=self._password_change_required(record),
            password_version=self._password_version(record),
            token_version=self._token_version(record),
            email=normalize_email(record.get("email")) or None,
        )

    # ------------------------------------------------------------------ 存取

    def ensure_users_file(self) -> None:
        if self.users_file.exists():
            return
        default_users = {
            "admin": {
                "username": "admin",
                "password_hash": self.hash_password(os.environ["AUTH_BOOTSTRAP_ADMIN_PASSWORD"]),
                "role": "admin",
                "must_change_password": True,
                "password_changed_at": "",
                "password_version": 1,
                "token_version": 1,
            },
        }
        self._save_users(default_users)

    def _load_users(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            try:
                mtime = self.users_file.stat().st_mtime
            except FileNotFoundError:
                pass
            else:
                if self._users_cache is not None and self._users_mtime == mtime:
                    return self._users_cache

        try:
            with open(self.users_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            mtime = self.users_file.stat().st_mtime
        except FileNotFoundError:
            self.ensure_users_file()
            with open(self.users_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            mtime = self.users_file.stat().st_mtime

        users: Dict[str, Dict[str, Any]] = {}
        for entry in data.get("users", []):
            username = str(entry.get("username", "")).strip()
            if not username:
                continue
            record = dict(entry)
            record["role"] = record.get("role") or "user"
            users[username] = record

        with self._lock:
            self._users_cache = users
            self._users_mtime = mtime
        return users

    def _save_users(self, users: Dict[str, Dict[str, Any]]) -> None:
        records = []
        for record in users.values():
            entry: Dict[str, Any] = {"username": record["username"]}
            if "password_hash" in record:
                entry["password_hash"] = record["password_hash"]
            else:
                entry["password_hash"] = self.hash_password(record["password"])
            entry["role"] = record.get("role", "user")
            entry["must_change_password"] = self._password_change_required(record)
            entry["password_changed_at"] = str(record.get("password_changed_at") or "")
            entry["password_version"] = self._password_version(record)
            entry["token_version"] = self._token_version(record)
            if normalize_email(record.get("email")):
                entry["email"] = normalize_email(record.get("email"))
            records.append(entry)
        payload = json.dumps({"users": records}, ensure_ascii=False, indent=2)
        with self._lock:
            self.users_file.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.users_file.with_name(
                ".{}.{}.tmp".format(self.users_file.name, uuid.uuid4().hex)
            )
            temp_path.write_text(payload, encoding="utf-8")
            try:
                temp_path.chmod(0o600)
            except OSError:
                pass
            os.replace(temp_path, self.users_file)
            self._users_cache = None
            self._users_mtime = None

    # ------------------------------------------------------------------ 查询

    def get_user(self, username: str) -> Optional[UserView]:
        users = self._load_users()
        record = users.get(username)
        if not record:
            return None
        return self._user_view_from_record(username, record)

    def get_user_by_email(self, email: str) -> Optional[UserView]:
        email = normalize_email(email)
        if not email:
            return None
        for username, record in self._load_users().items():
            if normalize_email(record.get("email")) == email:
                return self._user_view_from_record(username, record)
        return None

    def list_users(self) -> list:
        users = self._load_users()
        result = []
        for record in users.values():
            result.append(
                {
                    "username": record["username"],
                    "role": record.get("role", "user"),
                    "password_change_required": self._password_change_required(record),
                }
            )
        return result

    def list_users_admin(self) -> list:
        """admin list 形状：现有契约字段 + email（仅在存在时附带，存量用户形状不变）。"""
        users = self._load_users()
        return [
            self._user_view_from_record(username, record).to_admin_dict()
            for username, record in users.items()
        ]

    # ------------------------------------------------------------------ 认证

    def authenticate(self, username: str, password: str) -> Optional[UserView]:
        if not username or not password:
            return None
        users = self._load_users()
        record = users.get(username)
        if not record:
            return None
        changed = False
        if "password_hash" in record:
            if not self._verify_hash(password, record.get("password_hash", "")):
                return None
            if self._hash_iterations(record.get("password_hash", "")) < PASSWORD_HASH_ITERATIONS:
                record["password_hash"] = self.hash_password(password)
                changed = True
        else:
            # 遗留明文 password 字段：首登迁移为哈希并强制改密
            if record.get("password") != password:
                return None
            record["password_hash"] = self.hash_password(password)
            record.pop("password", None)
            record["must_change_password"] = True
            changed = True
        if "must_change_password" not in record:
            record["must_change_password"] = not bool(
                str(record.get("password_changed_at") or "").strip()
            )
            changed = True
        if "password_changed_at" not in record:
            record["password_changed_at"] = ""
            changed = True
        if "password_version" not in record:
            record["password_version"] = 1
            changed = True
        if "token_version" not in record:
            record["token_version"] = 1
            changed = True
        if changed:
            users[username] = record
            self._save_users(users)
        return self._user_view_from_record(username, record)

    # ------------------------------------------------------------------ 变更

    def create_user(self, username: str, password: str, role: str = "user") -> UserView:
        username = username.strip()
        if not username or not password:
            raise ValueError("用户名和密码不能为空")
        if role not in ("admin", "user"):
            raise ValueError("角色必须是 admin 或 user")
        validate_password_policy(username, password)
        users = self._load_users()
        if username in users:
            raise ValueError(f"用户 {username} 已存在")
        if len(users) >= self._user_max_count:
            raise ValueError("用户数量已达上限: {}".format(self._user_max_count))
        users[username] = {
            "username": username,
            "password_hash": self.hash_password(password),
            "role": role,
            "must_change_password": True,
            "password_changed_at": "",
            "password_version": 1,
            "token_version": 1,
        }
        self._save_users(users)
        return self._user_view_from_record(username, users[username])

    def create_registered_user(self, username: str, email: str, password_hash: str) -> UserView:
        """开放注册创建用户：role=user；密码是本人刚设置的，不强制改密。

        密码策略在注册提交时已校验；用户名/邮箱唯一性与数量上限在此再兜底一次。
        """
        username = str(username or "").strip()
        email = normalize_email(email)
        if not username or not password_hash:
            raise ValueError("用户名和密码不能为空")
        if not email:
            raise ValueError("邮箱格式不合法")
        users = self._load_users()
        if username in users:
            raise ValueError(f"用户 {username} 已存在")
        if any(normalize_email(r.get("email")) == email for r in users.values()):
            raise ValueError("邮箱已被注册")
        if len(users) >= self._user_max_count:
            raise ValueError("用户数量已达上限: {}".format(self._user_max_count))
        users[username] = {
            "username": username,
            "password_hash": password_hash,
            "role": "user",
            "must_change_password": False,
            "password_changed_at": self._now_iso(),
            "password_version": 1,
            "token_version": 1,
            "email": email,
        }
        self._save_users(users)
        return self._user_view_from_record(username, users[username])

    def update_user(
        self,
        username: str,
        *,
        password: Optional[str] = None,
        role: Optional[str] = None,
    ) -> UserView:
        username = username.strip()
        users = self._load_users()
        record = users.get(username)
        if not record:
            raise ValueError(f"用户 {username} 不存在")
        if password is not None:
            validate_password_policy(username, password)
            record["password_hash"] = self.hash_password(password)
            record.pop("password", None)
            record["must_change_password"] = True
            record["password_changed_at"] = ""
            record["password_version"] = self._password_version(record) + 1
            record["token_version"] = self._token_version(record) + 1
        if role is not None:
            if role not in ("admin", "user"):
                raise ValueError("角色必须是 admin 或 user")
            record["role"] = role
        users[username] = record
        self._save_users(users)
        return self._user_view_from_record(username, record)

    def delete_user(self, username: str) -> None:
        username = username.strip()
        users = self._load_users()
        if username not in users:
            raise ValueError(f"用户 {username} 不存在")
        del users[username]
        self._save_users(users)

    def change_password(self, username: str, old_password: str, new_password: str) -> UserView:
        username = username.strip()
        user = self.authenticate(username, old_password)
        if not user:
            raise ValueError("原密码错误")
        validate_password_policy(username, new_password, old_password=old_password)
        users = self._load_users()
        record = users.get(username)
        if not record:
            raise ValueError(f"用户 {username} 不存在")
        record["password_hash"] = self.hash_password(new_password)
        record.pop("password", None)
        record["must_change_password"] = False
        record["password_changed_at"] = self._now_iso()
        record["password_version"] = self._password_version(record) + 1
        record["token_version"] = self._token_version(record) + 1
        users[username] = record
        self._save_users(users)
        return self._user_view_from_record(username, record)

    def revoke_user_tokens(self, username: str) -> bool:
        """token_version+1，等价于吊销该用户全部已发 refresh/access token。"""
        username = username.strip()
        users = self._load_users()
        record = users.get(username)
        if not record:
            return False
        record["token_version"] = self._token_version(record) + 1
        users[username] = record
        self._save_users(users)
        return True
