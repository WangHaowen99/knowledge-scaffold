"""证据源（详设 5.8；方案 E11～E16）。本期只留接口（ADR-0056）：

- `spi`：证据源提供方的协议（5.8.3），提供方跑在连接器网关里，内核不加载提供方代码；
- `gateway_client`：内核调连接器网关的客户端协议与一次性票据字段（5.8.2）；
- `registry`：数据源登记与项目授权的仓储协议（5.8.7、方案 E11）。

这里只有协议与数据结构，没有实现。契约 3.19 的路由在 `routers/evidence.py` 一律回
`501 evidence_not_available`；助手侧的 `evidence.*` 工具（MCP 与命令行）不注册，接入时的落点见 ADR-0056。
"""

# 证据快照格式版本（5.8.4 manifest.json 的 format）
SNAPSHOT_FORMAT = "evidence-snapshot/1"
# 标准记录的类型（5.8.4 records/*.jsonl，每类一个文件）
RECORD_TYPES = ("entity", "field", "relation", "constraint", "operation", "rule", "flow", "profile", "sample", "term")
# 锚点 scheme（5.8.5）
ANCHOR_SCHEMES = ("file", "db", "code", "bin", "ui", "api", "cli", "log")
# 记录的置信度（5.8.4）
CONFIDENCE = ("declared", "observed", "inferred")


def available_providers() -> set[str]:
    """平台已安装、可用的证据源提供方类型（外部节点启用前查，详设 6.5）。本期证据源只留接口，没有提供方。"""
    return set()
