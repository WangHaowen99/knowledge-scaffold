"""内核调连接器网关的客户端协议（详设 5.8.2；方案 E15）。本期只有协议，没有实现（ADR-0056）。

网关是独立容器，只有它能连业务网络、只有它挂凭据卷。内核的每个请求都带一次性票据：写明任务、数据源、
操作、范围哈希、预算，10 分钟过期，用过即作废（jti 防重放由网关记）。凭据只写不读，没有任何读出明文的接口。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Literal, Protocol, runtime_checkable

from .spi import Budget, ProviderInfo, TestResult

Operation = Literal["discover", "snapshot", "sample", "test"]
JobState = Literal["queued", "running", "succeeded", "partial", "failed", "cancelled"]

TICKET_TTL_S = 600


@dataclass(frozen=True)
class Ticket:
    """一次性票据的字段（5.8.2「鉴权」）；由内核签发，网关验签、核对字段后作废。"""

    jti: str                      # 票据 ID，网关记下防重放
    job_id: str
    source_id: str
    operation: Operation
    scope_hash: str               # 范围白名单（或本次范围）的 sha256，网关按登记重新算一遍核对
    budget: Budget
    issued_at: float
    expires_at: float             # issued_at + TICKET_TTL_S
    issuer: str = "onto-modeling"
    single_use: bool = True


@dataclass(frozen=True)
class GatewayHealth:
    status: Literal["ok", "slow", "down"]
    checked_at: float
    providers_loaded: int


@dataclass(frozen=True)
class JobRequest:
    """`POST /v1/jobs` 的请求：票据、操作、范围、预算；sample 另带选择器与行数上限（≤ 20）。"""

    ticket: Ticket
    operation: Operation
    scope: tuple[str, ...]
    budget: Budget
    incremental_base: str | None = None
    sample_selector: str | None = None
    sample_limit: int | None = None


@dataclass(frozen=True)
class JobStatus:
    job_id: str
    state: JobState
    phase: str | None = None
    done: int = 0
    total: int = 0
    unit: str = ""
    error_code: str | None = None
    error_message: str | None = None
    checkpoint: bool = False


@runtime_checkable
class ConnectorGatewayClient(Protocol):
    """详设 5.8.2 的网关接口，一个方法对一个 HTTP 接口。"""

    def health(self) -> GatewayHealth: ...                                    # GET /health

    def providers(self) -> list[ProviderInfo]: ...                            # GET /v1/providers

    def test_source(self, source_id: str, ticket: Ticket) -> TestResult: ...  # POST /v1/sources/{id}/test

    def submit_job(self, request: JobRequest) -> JobStatus: ...               # POST /v1/jobs

    def job(self, job_id: str, ticket: Ticket) -> JobStatus: ...              # GET /v1/jobs/{id}

    def cancel_job(self, job_id: str, ticket: Ticket) -> JobStatus: ...       # POST /v1/jobs/{id}/cancel

    def bundle(self, job_id: str, ticket: Ticket) -> Iterator[bytes]: ...     # GET /v1/jobs/{id}/bundle（流式）

    def put_secret(self, ref: str, fields: dict[str, str]) -> str: ...        # PUT /v1/secrets/{ref}，返回新引用

    def rotate_secret(self, ref: str, fields: dict[str, str]) -> str: ...     # POST /v1/secrets/{ref}/rotate

    def delete_secret(self, ref: str) -> None: ...                            # DELETE /v1/secrets/{ref}
