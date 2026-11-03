"""证据源提供方 SPI（详设 5.8.3；契约 EV-10 的提供方清单）。本期只有协议，没有实现（ADR-0056）。

提供方在连接器网关里以子进程、旁路容器或 MCP 方式运行，内核进程不加载它们；凭据由网关经 stdin 交给
提供方子进程（5.8.6），这里的 `conn` 只是网关解析好的连接对象，内核侧看不到明文。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Literal, Protocol, runtime_checkable

ProviderKind = Literal["file", "db", "code", "api", "rea", "ui", "cli", "log"]
Capability = Literal["discover", "snapshot", "sample", "incremental"]
Runtime = Literal["subprocess", "sidecar", "mcp"]


@dataclass(frozen=True)
class CredentialField:
    """登记数据源时要填的连接参数；secret=True 的只交给网关，不进内核（5.8.6）。"""

    name: str
    label: str
    secret: bool = False


@dataclass(frozen=True)
class ProviderInfo:
    """提供方清单要声明的内容（5.8.3「提供方清单要声明的内容」）。"""

    id: str                                   # 如 db-mysql
    version: str
    label: str
    kind: ProviderKind
    evidence_types: tuple[str, ...]           # 能产出的标准记录类型（evidence.RECORD_TYPES 的子集）
    capabilities: tuple[Capability, ...]
    cred_fields: tuple[CredentialField, ...]
    readonly_method: str                      # 怎么保证只读，如「只读账号 + 只读会话 + 语句白名单」
    anchor_rule: str                          # 锚点 ID 的生成规则与稳定性承诺（5.8.5 规则 1）
    runtime: Runtime
    output_format: str = "evidence-snapshot/1"
    license_note: str = ""                    # 驱动、反编译工具的授权说明


@dataclass(frozen=True)
class TestCheck:
    name: Literal["connectivity", "readonly_account", "statement_allowlist"]
    ok: bool
    detail: str = ""
    latency_ms: int | None = None


@dataclass(frozen=True)
class TestResult:
    """测试连接，并自检账号确实只读（契约 EV-08 的 checks）。"""

    ok: bool
    checks: tuple[TestCheck, ...]


@dataclass(frozen=True)
class InventoryItem:
    object: str                               # 锚点路径部分，如 crm.customer
    kind: str                                 # entity / view / proc / endpoint / page …
    estimate_rows: int | None = None
    estimate_bytes: int | None = None


@dataclass(frozen=True)
class Inventory:
    """范围内可采集的对象与规模估算（discover 的产出，快照的 inventory.json 由它而来）。"""

    items: tuple[InventoryItem, ...]
    skipped: tuple[tuple[str, str], ...] = ()  # (对象, 没采的原因)


@dataclass(frozen=True)
class Budget:
    """单次任务的上限（契约 EV-02 budget）；网关强制执行，提供方绕不过去。"""

    max_rows: int
    max_bytes: int
    max_duration_s: int


@dataclass
class Stats:
    """一次采集的结果统计（快照 manifest.json 的计数与覆盖率来自它）。"""

    records: dict[str, int] = field(default_factory=dict)
    bytes: int = 0
    complete: bool = True
    coverage_pct: float = 100.0
    checkpoint: dict[str, Any] | None = None  # 按实体记的断点，续采用


@runtime_checkable
class RecordSink(Protocol):
    """提供方逐条写标准记录的出口（5.8.4 的统一记录格式，一行一条）。"""

    def write(self, record: dict) -> None: ...

    def blob(self, sha256: str, size: int, media_type: str) -> None: ...


@runtime_checkable
class EvidenceProvider(Protocol):
    """详设 5.8.3 的提供方接口。conn 是网关解析好的连接（含凭据），scope 是范围白名单内的对象。"""

    def describe(self) -> ProviderInfo: ...

    def test(self, conn: Any) -> TestResult: ...

    def discover(self, conn: Any, scope: Iterable[str]) -> Inventory: ...

    def snapshot(self, conn: Any, scope: Iterable[str], budget: Budget, sink: RecordSink) -> Stats: ...

    def sample(self, conn: Any, selector: str, limit: int) -> list[dict]: ...
