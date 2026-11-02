"""数据源登记与项目授权的仓储协议（详设 5.8.7；方案 E11；契约 EV-01～EV-11）。本期只有协议（ADR-0056）。

- 平台级登记（代码里没有租户概念），只存凭据引用（`cred://<源>/<版本>`）和不敏感的连接参数；
- 项目授权存数据目录（`DATA_DIR/evidence/grants/`），不放工作区：agent 能改工作区文件。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from .spi import Budget

Environment = Literal["production", "test"]
# 数据源 ID 是锚点前缀，登记后不能改（契约 EV-02）
SOURCE_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{1,40}$"


@dataclass(frozen=True)
class Egress:
    host: str
    port: int


@dataclass(frozen=True)
class AccessWindow:
    days: str | tuple[str, ...]   # "daily" 或星期列表
    start: str                    # "22:00"
    end: str                      # "06:00"
    tz: str


@dataclass(frozen=True)
class RateLimit:
    concurrency: int
    qps: float
    query_timeout_s: int


@dataclass
class SourceRegistration:
    """一条数据源登记（5.8.7「数据源登记，内容包括」）。"""

    id: str
    provider: str                              # 提供方 id
    display_name: str
    environment: Environment
    connection: dict[str, str | int]           # 不含密钥，字段由提供方的 cred_fields 决定
    cred_ref: str | None                       # cred://<源>/<版本>；明文只在网关
    scope_allowlist: tuple[str, ...]
    egress: tuple[Egress, ...]
    budget: Budget
    rate_limit: RateLimit
    masking_policy: str
    owner: str
    access_window: AccessWindow | None = None
    authorization_note: str | None = None      # REA 类必填
    created_at: float = 0.0
    updated_at: float = 0.0
    secret_written_at: dict[str, float] = field(default_factory=dict)  # 只记哪些密钥字段写过、什么时候


@dataclass(frozen=True)
class Grant:
    """项目对数据源的授权（契约 EV-11）；自主构建的生产库采集授权另在自主构建策略里。"""

    project_id: str
    source_id: str
    authorized: bool
    granted_at: float | None
    granted_by: str | None


@runtime_checkable
class SourceRegistry(Protocol):
    def list(self) -> list[SourceRegistration]: ...

    def get(self, source_id: str) -> SourceRegistration | None: ...

    def register(self, registration: SourceRegistration, *, by: str) -> SourceRegistration: ...

    def update(self, source_id: str, changes: dict, *, by: str) -> SourceRegistration: ...

    def delete(self, source_id: str, *, by: str) -> None: ...

    def set_cred_ref(self, source_id: str, cred_ref: str | None, *, fields: tuple[str, ...], by: str) -> None: ...

    def grants(self, project_id: str) -> list[Grant]: ...

    def set_grant(self, project_id: str, source_id: str, authorized: bool, *, by: str) -> Grant: ...
