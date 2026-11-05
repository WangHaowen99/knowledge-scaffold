"""证据源（详设 5.8；方案 E11～E16；ADR-0082）。

协议与格式（网关镜像也带，不能导入后端其他模块）：
- `spi`：证据源提供方的协议（5.8.3），提供方跑在连接器网关或旁路容器里，内核不加载提供方代码；
- `gateway_client`、`tickets`、`spec`：内核与网关之间的票据、任务说明、锚点与记录哈希；
- `registry`：数据源登记与项目授权的数据结构（5.8.7、方案 E11）。

内核这一侧（`design/evidence-collection.md` §6）：`sources` 登记、`grants` 项目授权、`uploads` 分块上传、
`collector` 调网关采集、`jobs` 采集任务、`ingest` 快照入库、`index` 检索索引、`query` 检索与锚点解析。
路由在 `routers/evidence.py`。助手侧不注册 `evidence.*` MCP 工具，工作区脚本由系统逆向建模包带。
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
    """平台已安装、可用的证据源提供方类型（外部节点启用前查，详设 6.5）：网关里 installed 的提供方 kind；
    网关没配或不通时为空集。网关镜像也带这个包，所以内核模块在函数里才导入。"""
    from .collector import catalog

    return {str(item.get("kind")) for item in catalog() or [] if item.get("installed", True) and item.get("kind")}
