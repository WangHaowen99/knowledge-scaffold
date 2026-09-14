"""Authentication helpers for the agentic API."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, Iterable, Optional, Protocol

from agent.runtime.offload import OffloadPoolClosed, OffloadPoolSaturated
from api.exceptions import APIError
from fastapi import Request as StarletteRequest


@dataclass(frozen=True)
class AuthUser:
    username: str
    role: str
    password_change_required: bool = False
    password_version: int = 1

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "username": self.username,
            "role": self.role,
            "password_change_required": self.password_change_required,
        }


@dataclass(frozen=True)
class LoginResult:
    """Atomic login result returned without shared, username-keyed state."""

    user: AuthUser
    access_token: str
    refresh_token: Optional[str] = None
    expires_in: Optional[int] = None


@dataclass(frozen=True)
class AuthFailure:
    status_code: int
    content: Dict[str, Any]


class PasswordPolicyError(ValueError):
    def __init__(self, violations: list[str]) -> None:
        self.violations = violations
        super().__init__("密码复杂度不满足要求")


class AuthBackend(Protocol):
    """Authentication contract shared by the remote backend and test backend."""

    def login(self, username: str, password: str) -> Optional[LoginResult]:
        return ...

    def verify_token(self, token: str) -> Optional[AuthUser]:
        return ...

    def get_user(self, username: str) -> Optional[AuthUser]:
        return ...

    def list_users(self) -> list:
        return ...

    def create_user(self, username: str, password: str, role: str = "user") -> AuthUser:
        return ...

    def update_user(
        self,
        username: str,
        *,
        password: Optional[str] = None,
        role: Optional[str] = None,
    ) -> AuthUser:
        return ...

    def delete_user(self, username: str) -> None:
        return ...

    def change_password(
        self,
        username: str,
        old_password: str,
        new_password: str,
    ) -> AuthUser:
        return ...


class AuthManager:
    """File-backed test backend; production app assembly uses RemoteAuthBackend."""

    PASSWORD_HASH_ITERATIONS = 600_000
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

    def __init__(
        self,
        users_file: str,
        secret: Optional[str],
        token_ttl_seconds: int = 12 * 3600,
        bootstrap_admin_password: Optional[str] = None,
    ) -> None:
        self.users_file = Path(users_file)
        self._secret = (secret or "dev-secret-change").encode("utf-8")
        self._token_ttl_seconds = max(60, int(token_ttl_seconds))
        self._lock = threading.Lock()
        self._users_cache: Optional[Dict[str, Dict[str, Any]]] = None
        self._users_mtime: Optional[float] = None
        self._bootstrap_admin_password = bootstrap_admin_password or os.environ.get("AUTH_BOOTSTRAP_ADMIN_PASSWORD")
        self.ensure_users_file()

    def ensure_users_file(self) -> None:
        if self.users_file.exists():
            return
        bootstrap_password = self._bootstrap_admin_password
        if not bootstrap_password:
            raise RuntimeError("首次启动必须配置 AUTH_BOOTSTRAP_ADMIN_PASSWORD；已有 users.json 的部署无需配置")
        self.validate_password_policy("admin", bootstrap_password)
        self.users_file.parent.mkdir(parents=True, exist_ok=True)
        default_users = [
            {
                "username": "admin",
                "password_hash": self.hash_password(bootstrap_password),
                "role": "admin",
                "must_change_password": False,
                "password_changed_at": self._now_iso(),
                "password_version": 1,
            },
        ]
        with open(self.users_file, "w", encoding="utf-8") as f:
            json.dump({"users": default_users}, f, ensure_ascii=False, indent=2)
        self._bootstrap_admin_password = None

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

    @classmethod
    def validate_password_policy(
        cls,
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
        if value.lower() in cls._WEAK_PASSWORDS:
            violations.append("common_password")
        if old_password is not None and value == old_password:
            violations.append("same_as_old")
        if violations:
            raise PasswordPolicyError(violations)

    @staticmethod
    def _password_version(record: Dict[str, Any]) -> int:
        try:
            return max(1, int(record.get("password_version") or 1))
        except (TypeError, ValueError):
            return 1

    @staticmethod
    def _password_change_required(_record: Dict[str, Any]) -> bool:
        # 保留响应字段以兼容既有客户端，但不再要求任何账号首次登录后改密。
        return False

    def _auth_user_from_record(self, username: str, record: Dict[str, Any]) -> AuthUser:
        return AuthUser(
            username=username,
            role=record.get("role", "user"),
            password_change_required=self._password_change_required(record),
            password_version=self._password_version(record),
        )

    def _load_users(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            try:
                mtime = self.users_file.stat().st_mtime
            except FileNotFoundError:
                self.ensure_users_file()
                mtime = self.users_file.stat().st_mtime

            if self._users_cache is not None and self._users_mtime == mtime:
                return self._users_cache

            with open(self.users_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            users: Dict[str, Dict[str, Any]] = {}
            for entry in data.get("users", []):
                username = str(entry.get("username", "")).strip()
                if not username:
                    continue
                record = dict(entry)
                record["role"] = record.get("role") or "user"
                users[username] = record

            self._users_cache = users
            self._users_mtime = mtime
            return users

    def load_users_for_test(self) -> Dict[str, Dict[str, Any]]:
        """Load user records through the production cache path for tests."""
        return self._load_users()

    def get_user(self, username: str) -> Optional[AuthUser]:
        users = self._load_users()
        record = users.get(username)
        if not record:
            return None
        return self._auth_user_from_record(username, record)

    def authenticate(self, username: str, password: str) -> Optional[AuthUser]:
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
            if self._hash_iterations(record.get("password_hash", "")) < self.PASSWORD_HASH_ITERATIONS:
                record["password_hash"] = self.hash_password(password)
                changed = True
        else:
            if record.get("password") != password:
                return None
            record["password_hash"] = self.hash_password(password)
            record.pop("password", None)
            record["must_change_password"] = False
            record["password_changed_at"] = self._now_iso()
            changed = True
        if "must_change_password" not in record:
            record["must_change_password"] = False
            changed = True
        if "password_changed_at" not in record:
            record["password_changed_at"] = self._now_iso()
            changed = True
        if "password_version" not in record:
            record["password_version"] = 1
            changed = True
        if changed:
            users[username] = record
            self._save_users(users)
        return self._auth_user_from_record(username, record)

    def login(self, username: str, password: str) -> Optional[LoginResult]:
        user = self.authenticate(username, password)
        if user is None:
            return None
        return LoginResult(user=user, access_token=self.issue_token(user))

    def _save_users(self, users: Dict[str, Dict[str, Any]]) -> None:
        records = []
        for record in users.values():
            entry = {"username": record["username"]}
            if "password_hash" in record:
                entry["password_hash"] = record["password_hash"]
            else:
                entry["password_hash"] = self.hash_password(record["password"])
            entry["role"] = record.get("role", "user")
            entry["must_change_password"] = self._password_change_required(record)
            entry["password_changed_at"] = str(record.get("password_changed_at") or self._now_iso())
            entry["password_version"] = self._password_version(record)
            records.append(entry)
        with self._lock:
            with open(self.users_file, "w", encoding="utf-8") as f:
                json.dump({"users": records}, f, ensure_ascii=False, indent=2)
            self._users_cache = None
            self._users_mtime = None

    def save_users_for_test(self, users: Dict[str, Dict[str, Any]]) -> None:
        """Persist synthetic user records through the production path."""
        self._save_users(users)

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

    def create_user(self, username: str, password: str, role: str = "user") -> AuthUser:
        username = username.strip()
        if not username or not password:
            raise ValueError("用户名和密码不能为空")
        if role not in ("admin", "user"):
            raise ValueError("角色必须是 admin 或 user")
        self.validate_password_policy(username, password)
        users = self._load_users()
        if username in users:
            raise ValueError(f"用户 {username} 已存在")
        users[username] = {
            "username": username,
            "password_hash": self.hash_password(password),
            "role": role,
            "must_change_password": False,
            "password_changed_at": self._now_iso(),
            "password_version": 1,
        }
        self._save_users(users)
        return self._auth_user_from_record(username, users[username])

    def update_user(self, username: str, *, password: Optional[str] = None, role: Optional[str] = None) -> AuthUser:
        username = username.strip()
        users = self._load_users()
        record = users.get(username)
        if not record:
            raise ValueError(f"用户 {username} 不存在")
        if password is not None:
            self.validate_password_policy(username, password)
            record["password_hash"] = self.hash_password(password)
            record.pop("password", None)
            record["must_change_password"] = False
            record["password_changed_at"] = self._now_iso()
            record["password_version"] = self._password_version(record) + 1
        if role is not None:
            if role not in ("admin", "user"):
                raise ValueError("角色必须是 admin 或 user")
            record["role"] = role
        users[username] = record
        self._save_users(users)
        return self._auth_user_from_record(username, record)

    def delete_user(self, username: str) -> None:
        username = username.strip()
        users = self._load_users()
        if username not in users:
            raise ValueError(f"用户 {username} 不存在")
        del users[username]
        self._save_users(users)

    def change_password(self, username: str, old_password: str, new_password: str) -> AuthUser:
        username = username.strip()
        user = self.authenticate(username, old_password)
        if not user:
            raise ValueError("原密码错误")
        self.validate_password_policy(username, new_password, old_password=old_password)
        users = self._load_users()
        record = users.get(username)
        if not record:
            raise ValueError(f"用户 {username} 不存在")
        record["password_hash"] = self.hash_password(new_password)
        record.pop("password", None)
        record["must_change_password"] = False
        record["password_changed_at"] = self._now_iso()
        record["password_version"] = self._password_version(record) + 1
        users[username] = record
        self._save_users(users)
        return self._auth_user_from_record(username, record)

    def issue_token(self, user: AuthUser) -> str:
        now = int(time.time())
        payload = {
            "sub": user.username,
            "role": user.role,
            "pwd": user.password_version,
            "iat": now,
            "exp": now + self._token_ttl_seconds,
        }
        payload_json = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        payload_b64 = _b64url_encode(payload_json)
        sig = self._sign(payload_b64.encode("ascii"))
        return f"{payload_b64}.{sig}"

    def verify_token(self, token: str) -> Optional[AuthUser]:
        if not token:
            return None
        parts = token.split(".")
        if len(parts) != 2:
            return None
        payload_b64, sig = parts
        expected_sig = self._sign(payload_b64.encode("ascii"))
        if not hmac.compare_digest(expected_sig, sig):
            return None
        try:
            payload_bytes = _b64url_decode(payload_b64)
            payload = json.loads(payload_bytes.decode("utf-8"))
        except ValueError:
            return None
        exp = payload.get("exp")
        if not isinstance(exp, int) or exp < int(time.time()):
            return None
        username = payload.get("sub")
        if not username:
            return None
        user = self.get_user(username)
        if not user:
            return None
        if payload.get("pwd") != user.password_version:
            return None
        return user

    def _sign(self, data: bytes) -> str:
        digest = hmac.new(self._secret, data, hashlib.sha256).digest()
        return _b64url_encode(digest)


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


_current_user: ContextVar[Optional[AuthUser]] = ContextVar(
    "consumer_current_user",
    default=None,
)


def get_auth_manager(request: StarletteRequest) -> Optional[AuthBackend]:
    services = getattr(request.app.state, "services", {})
    return services.get("AUTH_MANAGER")


def get_current_user() -> Optional[AuthUser]:
    return _current_user.get()


@contextmanager
def current_user_scope(user: Optional[AuthUser]):
    """Bind an authenticated principal to the current request/task scope."""
    token = _current_user.set(user)
    try:
        yield user
    finally:
        _current_user.reset(token)


def _authorization_error(status_code: int, message: str, *, code: str = "") -> AuthFailure:
    content = {"error": message}
    if code:
        content["code"] = code
    return AuthFailure(status_code=status_code, content=content)


def _authorize_request(
    request: StarletteRequest,
    *,
    roles: Optional[set[str]] = None,
) -> tuple[Optional[AuthUser], Optional[AuthFailure]]:
    auth_manager = get_auth_manager(request)
    if not auth_manager:
        return None, _authorization_error(500, "Auth not configured")

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None, _authorization_error(401, "Unauthorized")

    token = auth_header.split(" ", 1)[1].strip()
    user = auth_manager.verify_token(token)
    if not user:
        return None, _authorization_error(401, "Unauthorized")

    if roles and user.role not in roles:
        return None, _authorization_error(403, "Forbidden")

    return user, None


def authenticated_user_dependency(
    roles: Optional[Iterable[str]] = None,
) -> Callable[[StarletteRequest], AsyncIterator[AuthUser]]:
    """Build a native FastAPI dependency that scopes the authenticated user."""
    roles_set = set(roles) if roles else None

    async def dependency(request: StarletteRequest):
        user = await authenticate_user(request, roles=roles_set)
        token = _current_user.set(user)
        try:
            yield user
        finally:
            _current_user.reset(token)

    return dependency


async def authenticate_user(
    request: StarletteRequest,
    *,
    roles: Optional[Iterable[str]] = None,
) -> AuthUser:
    """Authenticate one native ASGI request without consuming its body."""
    runtime = getattr(request.app.state, "runtime_container", None)
    app_type = type(request.app)
    is_isolated_test = app_type.__module__ == "api.testing" and app_type.__name__ == "TestApplication"
    if runtime is None and is_isolated_test:
        runtime = getattr(request.app.state, "agent_execution_runtime", None)
    pool = getattr(runtime, "external_io", None) if runtime is not None else None
    if pool is None:
        if not is_isolated_test:
            raise APIError(503, {"error": "Runtime is not available"})
        user, error = _authorize_request(
            request,
            roles=set(roles) if roles else None,
        )
    else:
        try:
            user, error = await pool.run(
                _authorize_request,
                request,
                roles=set(roles) if roles else None,
            )
        except OffloadPoolSaturated as exc:
            raise APIError(
                429,
                {"error": "Runtime offload capacity is exhausted"},
            ) from exc
        except OffloadPoolClosed as exc:
            raise APIError(503, {"error": "Runtime is draining"}) from exc
    if error is not None:
        raise APIError(error.status_code, error.content)
    if user is None:
        failure = _authorization_error(401, "Unauthorized")
        raise APIError(failure.status_code, failure.content)
    return user


require_authenticated_user = authenticated_user_dependency()
require_admin_user = authenticated_user_dependency(["admin"])
