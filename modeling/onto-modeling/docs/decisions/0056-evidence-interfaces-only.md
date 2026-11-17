# 0056 证据源本期只留接口

## Context

详设 5.8 的证据源层（连接器网关、提供方、快照、锚点、检索）与系统逆向建模包排在 P3（方案 E11～E16）。
用户 2026-11-02 定：本期不实现，只留接口。要定的是「留」到什么程度：契约 3.19 的 30 个接口、PL-04 与 PL-19
（提供方由管理员装到平台）、助手侧的 `evidence.*` 工具、前端里指向证据源的入口、配额与自主构建策略里的相关字段。

## Decision

1. **内核 SPI 只有协议**（`backend/app/evidence/`）：
   - `spi.py`：提供方 `EvidenceProvider`（describe / test / discover / snapshot / sample）与提供方清单 `ProviderInfo`
     （证据类型、能力、凭据字段与是否密钥、只读方式、锚点规则、运行方式、许可证说明），按详设 5.8.3；
   - `gateway_client.py`：连接器网关客户端 `ConnectorGatewayClient`（一个方法对网关一个接口）与一次性票据 `Ticket`
     （jti、任务、数据源、操作、范围哈希、预算、10 分钟过期、用过即作废），按 5.8.2；
   - `registry.py`：数据源登记 `SourceRegistration`（只有凭据引用，没有放明文的字段）与项目授权 `Grant` 的仓储
     `SourceRegistry`，按 5.8.7 与方案 E11。
   没有实现类，也没有代码导入它们去做事。
2. **路由全部注册，一律 `501 evidence_not_available`**（`routers/evidence.py`）：契约 3.19 每个接口（EV-01～EV-30，
   EV-11、EV-17、EV-29 各有多条）加 PL-04、PL-19；错误信封按契约 1.5，`details.api` 带接口编号。固定段先于
   路径参数登记（`snapshots/diff` 不会被当成快照 ID）。
3. **不给助手注册 `evidence.*` 工具**：MCP（`onto_mcp/definitions.py`）和命令行（工作区脚本）都不加。一律失败的工具
   只会让助手白耗轮次。**以后接入的落点**：
   - MCP：在 `services/onto_mcp/definitions.py` 加 `evidence_search`、`evidence_show`、`evidence_where_used`、
     `evidence_lineage`、`evidence_diff`，权限范围 `evidence.read`，处理函数调 `app/evidence/` 的检索实现；
     项目级的只读服务器在 P-14 列出（`routers/project_settings.py` 已留说明）；
   - 命令行：在 core 包加 `scripts/evidence.py`（方案 E13，沿用 `source_extract` 的分页协议），两个引擎都能用；
   - 两边都要等系统逆向建模包启用、项目有授权的数据源时才出现，和流程节点 `rev.evidence` 一起装配。
4. **前端入口不出现，也不写「暂未开放」**：
   - 对话里的欢迎与上传卡：缺省动作不含 `connect_system`，只有服务端给了这个动作、且本项目有数据源子页时才出现；
   - 新建向导：EV-01 读不到时不列「连接业务系统」；项目设置：读不到数据源登记时不出「生产库采集授权」；
   - Studio 证据卡：数据库锚点的「查看原文」和「被谁引用」只在证据浏览器子页存在时出现；
   - 待处理的「证据冲突」筛选、插件中心的「证据源提供方」分类：没有事项、没有包时不出现；
   - 资料页的三个子页本来就只随系统逆向建模包出现，命令面板的入口也按子页判断。
5. **保留字段，标明不生效**：配额的 `collections_per_source_per_hour`、`snapshot_git_mb` 照常能存，SY-15 响应带
   `not_enforced` 列出这两项，系统设置里不显示它们；`QuotaService.check_collection` 留着但没有调用方。自主构建策略
   （AB-01、AB-02，B5b 实现）里的生产库采集授权与 `platform_limits.allow_production_collection` 保留，本期不生效。

## Alternatives considered

- **路由先不注册**：前端拿到 404 分不清「接口不存在」与「功能没开」，以后实现时还要再对一遍路径；501 + 专用错误码
  给了明确的落点。
- **注册工具但回「未开放」**：助手会反复尝试，每次一轮；而且工具描述会占提示词预算。
- **前端显示「暂未开放」**：用户看到的是一个点不动的入口，多一句没有用的话；入口随包出现，包不存在就不出现。

## Consequences

- 契约 2.8 加错误码 `evidence_not_available`（501）；SY-15 加 `not_enforced`（`_plan/contract-changes.md` B5a 节）。
- 前端 mock 仍按设计稿模拟系统逆向建模包启用后的样子（证据页面、数据源、快照），只用于设计对照。
- 证据源实现时：把 `routers/evidence.py` 的条目逐条换成处理函数，`app/evidence/` 补实现，按上面第 3 条注册工具，
  并把 SY-15 的 `not_enforced` 去掉对应项。

## 补充（2026-11-17，ADR-0100）

证据源接口随系统逆向建模包一起删除（数据源、凭据、采集、快照、证据浏览器）。资料锚点解析 EV-29 保留，只解析 `file:` 锚点。本 ADR 作废。
