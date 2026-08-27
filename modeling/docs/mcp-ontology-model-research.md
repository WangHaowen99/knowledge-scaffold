# 通过 MCP 接口支持业务 Agent 查询、校验、修改对象模型元属性、字段定义与对象关系调研

更新时间：2026-08-27

## 0. 当前已落地的 V1 实现

截至 2026-08-27，建模侧 MCP 已完成一版可用落地，范围如下：

### 0.1 已实现

1. 建模侧独立 MCP transport

- 启动入口：`modeling/src/mcp-server.ts`
- transport 路由：`modeling/src/mcp/transport.ts`
- 当前已支持：
  - `POST /{mcp_endpoint_id}/read/mcp`
  - `POST /{mcp_endpoint_id}/change/mcp`

2. 项目级 MCP 管理 API

- 管理路由：`modeling/src/mcp/routes.ts`
- 主服务内已接入：
  - `GET /api/projects/:id/mcp/runtime/status`
  - `GET /api/projects/:id/mcp/tokens`
  - `POST /api/projects/:id/mcp/tokens`
  - `DELETE /api/projects/:id/mcp/tokens/:tokenId`
- status 返回中已包含：
  - `server_name`
  - `server_version`
  - `scope_catalog`
  - `token_policy`

3. 项目级 endpoint 与 token 治理

- 持久化与鉴权：`modeling/src/mcp/service.ts`
- 已实现：
  - `project` 级稳定 `mcp_endpoint_id`
  - Bearer token 签发
  - token 吊销
  - scope 校验
  - endpoint 与 project 绑定校验
  - 用户项目权限复检

4. 已对外暴露的对象模型工具

- 工具定义：`modeling/src/mcp/toolset.ts`
- `read` toolset：
  - `list_ontology_objects`
  - `get_ontology_object`
  - `get_ontology_object_review_status`
  - `list_ontology_object_audit_records`
- `change` toolset：
  - `validate_ontology_object_patch`
  - `apply_ontology_object_patch`
  - `submit_ontology_object_review`
  - `prepare_ontology_object_confirmation_groups`
  - `preflight_ontology_object_confirmation`
  - `confirm_ontology_object_review`
  - `reject_ontology_object_review`

5. 与现有建模底座的集成

- 仍复用 `AgentCoreService`、`StudioWriteService`、`StudioReviewService`
- 对象模型内部能力没有重写，只是在其外层补了标准 MCP 暴露层

6. 前端配置中心 MCP 入口

- 前端位置：`modeling/frontend/src/SettingsModal.jsx`
- 当前已接入：
  - 项目级 `MCP 接入` tab
  - runtime 状态展示
  - read/change URL 展示
  - token 创建
  - token 吊销
  - 已创建 token 单次明文回显
  - 回环地址提示（提醒部署 `MODELING_MCP_PUBLIC_BASE_URL`）

### 0.2 当前仍未实现

1. 第二层字段定义虽然已能通过对象模型 patch 写入，但前端专用字段编辑器还没单独补齐
2. 还没有补项目级 review 总览、跨对象审计聚合和独立审计检索页

### 0.3 当前 V1 的实际定位

当前实现已经足够支持：

- 业务 Agent 通过标准 MCP 读取对象模型
- 通过 `Streamable HTTP` 或 `SSE` 连接建模侧 MCP transport
- 对对象元属性、字段定义、对象关系做 validate-only
- 写入草稿 candidate
- 提交 review
- 查询对象当前 draft / review_requested / awaiting_confirmation / accepted 等状态
- 按对象查询候选、确认、退回等审计记录
- 读取确认分组
- 做确认前复检
- 对 review 结果执行最终确认或退回

但它还不是“全部治理能力都齐全的最终版”，后续仍建议继续补：

- 项目级 review 总览与队列视图
- 更细粒度审计筛选、聚合与导出能力

## 1. 需求范围确认

基于最新确认，当前需求范围已经扩展为同时覆盖三层：

- 第一层：对象自己的模型元属性
- 第二层：对象有哪些字段属性
- 第三层：对象与对象之间的关系

对应到当前工程中的具体结构：

- 第一层：`SpecObject.title`、`SpecObject.description`、`SpecObject.aliases`、`SpecObject.queryBoundary`
- 第二层：`SpecObject.fields[]`
- 第三层：`SpecObject.relations[]`

当前不纳入本次需求的内容：

- 具体业务数据值：例如某个客户的 `customer_level = VIP`

一句话概括：

- 本次需求要改的是“这个对象是什么”
- “这个对象由哪些字段组成”
- “这个对象和哪些对象存在什么关系”
- 不改“某条业务记录当前的具体值”

## 2. 概念边界

为了避免后续讨论继续混淆，建议固定按下面方式理解：

| 层次 | 当前工程里的对应位置 | 是否属于本次需求 |
| --- | --- | --- |
| 第一层：对象自己的模型元属性 | `SpecObject.title/description/aliases/queryBoundary` | 是 |
| 第二层：对象字段定义 | `SpecObject.fields[]` | 是 |
| 第三层：对象关系 | `SpecObject.relations[]` | 是 |
| 具体业务数据值 | 数据表/样例数据/业务库 | 否 |

用“客户对象”举例：

第一层：

- `title = 客户`
- `description = 购买商品或服务的主体`
- `aliases = [会员]`
- `queryBoundary = 仅包含注册客户，不含游客`

第二层：

- `fields[]` 中定义 `customer_id`
- `fields[]` 中定义 `customer_level`
- `fields[]` 中定义 `register_time`

第三层：

- `relations[]` 中定义客户关联订单
- `relations[]` 中定义客户关联会员等级

不属于本次需求的例子：

- 某条数据里 `customer_level = VIP`

## 3. 代码实现位置

## 3.1 核心模型定义

对象模型定义位于：

- `modeling/src/types.ts`

本次需求直接相关的结构有三类：

第一层元属性：

- `SpecObject.title`
- `SpecObject.description`
- `SpecObject.aliases`
- `SpecObject.queryBoundary`

第二层字段定义：

- `SpecObject.fields[]`
- `SpecField.key`
- `SpecField.cnName`
- `SpecField.type`
- `SpecField.required`
- `SpecField.note`
- `SpecField.alias`
- `SpecField.sourceColumn`
- `SpecField.valueBindingRef`

第三层对象关系：

- `SpecObject.relations[]`
- `SpecRelation.relation`
- `SpecRelation.target`
- `SpecRelation.note`
- `SpecRelation.evidence`

## 3.2 知识包落盘形式

当前建模数据底层不是独立图数据库，而是知识包文件。

对象最终会落到：

- `objects/<object_key>.md`

相关代码：

- 渲染：`modeling/src/workflow/package-render.ts`
- 解析：`modeling/src/workflow/package-import.ts`

本次需求中的三层能力最终都会落回对象知识文件：

- 第一层元属性会影响对象标题、业务定义、别名、查询边界
- 第二层字段定义会影响 `# 字段`
- 第三层对象关系会影响 `# 关系`

因此，MCP 对外虽然是工具协议，本质上改的仍然是知识包中的对象定义。

## 3.3 当前结构化读写底座

当前工程已经在知识包文件之上抽象出一层 `Studio` 结构化读写能力。

关键文件：

- 路由：`modeling/src/agent/core-routes.ts`
- 读服务：`modeling/src/agent/studio-read-service.ts`
- 写服务：`modeling/src/agent/studio-write-service.ts`
- 复核服务：`modeling/src/agent/studio-review-service.ts`

后续做 MCP，最合理的做法不是直接绕过这层去改文件，而是复用这层结构化读写底座。

## 4. 页面呈现位置

当前前端入口位于 Modeling Workbench 的 `Studio` 区域。

相关文件：

- 入口：`modeling/frontend/src/workbench/center/CenterPane.jsx`
- API 封装：`modeling/frontend/src/workbench/studio/studio-api.js`
- 主页面：`modeling/frontend/src/workbench/studio/StudioView.jsx`

对象详情页当前会展示：

- `title`
- `description`
- `aliases`
- `fields`
- `queryBoundary`
- `sources`
- `relations`

其中：

- 第一层元属性当前可编辑
- 第二层 `fields[]` 当前可展示但不可编辑
- 第三层对象关系当前可编辑，但页面交互偏粗粒度

也就是说，页面已经具备“第一层编辑 + 第二层只读展示 + 第三层基础编辑”的现状。

## 4.1 当前配置中心弹窗位置

除 Studio 详情页本身外，当前建模前端还有一个更适合承载 MCP 接入配置的统一入口：

- 页面右上角的“配置中心”按钮
- 打开后是 `SettingsModal` 弹窗

当前已有 tab：

- `大模型（LLM）`
- `consumer 服务`
- `评测数据`
- `MCP 接入`（仅项目内显示）

这说明前端已经存在一个“集中管理外部接入与运行配置”的标准位置。

## 4.2 建议的 MCP 页面入口位置

本次需求已按这个方向实现：MCP 接入入口放在“配置中心”弹窗里，而不是散落在 Studio 页面内部。

推荐做法：

- 在配置中心弹窗中新增一个 `MCP` 或 `Agent 接入` tab

建议优先命名为：

- `MCP 接入`

原因是：

- 与现有 `consumer 服务`、`评测数据` 这类配置项风格一致
- 能直接表达“这是给外部 Agent/工具接入用的”
- 不会和 Studio 内部编辑能力混在一起

## 4.3 为什么建议放在配置中心弹窗里

这个位置比放在 Studio 详情页更合适，原因有三点：

1. MCP 本质上是接入配置，不是知识内容本身

- 它属于 endpoint、token、scope、toolset、连接方式管理
- 不属于对象内容编辑表单的一部分

2. 现有用户心智一致

- 用户已经知道模型、consumer、评测等系统级接入配置放在配置中心
- MCP 放在同一处更符合直觉

3. 后续扩展空间更大

- 未来加 token 管理、scope 管理、连接示例、调用审计入口时，不会挤占 Studio 内容区

## 4.4 建议的显示条件

虽然入口放在配置中心弹窗里，但内容不建议做成纯全局配置。

建议规则是：

- 当用户还在项目列表页、尚未进入具体项目时，不显示 `MCP 接入` tab
- 只有进入某个具体建模项目后，才显示该 tab

原因是：

- 建模 MCP 后续建议按 `project` 绑定 `mcp_endpoint_id`
- token、scope、working package、review 也都是项目级语义
- 因此这是“项目内配置”，不是“平台全局配置”

## 4.5 建议的 tab 内容

`MCP 接入` tab 建议至少展示以下内容：

1. 运行状态

- MCP 是否已启用
- 当前 transport
- 当前 toolset

2. 接入地址

- `read` toolset 的 MCP URL
- `change` toolset 的 MCP URL
- 如保留 SSE，则同时展示 SSE URL

3. token 管理

- 新建 token
- 查看 token 名称、作用域、失效时间、最后使用时间
- 吊销 token

4. 权限说明

- 当前 token 是只读还是可变更
- 可访问哪些 toolset

5. 调用说明

- 连接示例
- 鉴权方式说明
- 参数说明入口或文档链接

## 4.6 建议的交互边界

这个 tab 建议只负责“接入治理”，不直接承担对象编辑。

也就是说：

- 在这里查看 URL、token、scope、toolset、状态
- 不在这里直接编辑对象元属性、字段定义、对象关系

对象内容的编辑仍然通过：

- Studio 页面
- 或未来外部 Agent 通过 MCP tools 调用

## 5. 当前工程已实现的内部能力

## 5.1 已有查询能力

当前已有的结构化查询接口：

- `GET /api/projects/:id/studio/schema`
- `GET /api/projects/:id/studio/elements`
- `GET /api/projects/:id/studio/elements/:elementId`
- `GET /api/projects/:id/studio/graph`

这些接口已经足够支撑：

- 查询对象列表
- 查询对象详情
- 查询第一层元属性
- 查询第二层字段定义
- 查询第三层对象关系
- 查询哪些字段可编辑
- 查询对象关系图

## 5.2 第一层当前已具备修改能力

当前已有的结构化写接口：

- `PATCH /api/projects/:id/studio/elements/:elementId`
- `POST /api/projects/:id/studio/review`

对于第一层，当前对象 schema 中：

- `title` 可编辑
- `description` 可编辑
- `aliases` 可编辑
- `queryBoundary` 可编辑

因此第一层已经有现成底座。

可直接复用的写入语义如下：

```json
{
  "expectedRevision": "rev_xxx",
  "clientMutationId": "mcp-update-object-meta-001",
  "changes": {
    "title": "客户",
    "description": "用于表示客户主体及其基础画像",
    "aliases": ["会员", "客户主体"],
    "queryBoundary": "仅覆盖注册客户，不含潜在线索"
  }
}
```

## 5.3 第二层当前只有读取能力，没有结构化写入能力

当前对象 schema 中：

- `fields` 存在
- 但 `fields` 被标记为 `readOnly`

这意味着第二层现在可以读，但不能通过现有 Studio 结构化接口直接改。

当前限制是三层同时存在的：

1. schema 层限制

- `fields` 被定义为 `readOnly`

2. 前端编辑草稿限制

- `editableDraft()` 只会把非只读字段放入草稿

3. 后端写服务限制

- `normalizeFieldValue()` 对结构化字段只支持 `inputs/outputs/conditions/nullPolicy/dimensions`
- 对 `field_table` 会直接返回“该结构字段暂不支持编辑”

所以第二层不是“前端还没做”，而是当前 Studio 底座本身就没有打通。

## 5.4 第三层当前已具备结构化修改能力

第三层对象关系当前已经有现成底座，且成熟度明显高于第二层。

现状如下：

1. schema 层已开放关系类型

- 对象 kind 当前声明了 `legalRelations: [{ kind: 'object_relation', targetKinds: ['object'] }]`

2. 读服务已能把 `SpecObject.relations[]` 投影成结构化 `StudioRelation[]`

- 读服务会把 `relation`、`target`、`note` 映射为关系类型、目标 ID、说明

3. 写服务已能接收 `changes.relations`

- `normalizeValues()` 会单独识别 `relations`
- `normalizeRelations()` 会校验类型、目标 ID、目标类型、可选 `label/description`
- `applyRelations()` 会把结构化关系写回 `SpecObject.relations[]`

4. 写服务已具备关系目标存在性校验

- `validateRelations()` 会校验目标对象是否真实存在

5. 前端已提供基础关系编辑器

- 当前支持新增关系目标
- 当前支持删除关系目标
- 当前编辑草稿里也会带上 `relations`

因此第三层不是待建设能力，而是已经具备内部结构化写入能力。

## 5.5 第三层当前的限制

虽然第三层已有底座，但仍有边界：

- 页面关系编辑器更偏“增删目标对象”
- 不是完整的关系事实编辑器
- 前端默认关系标签是预设值，例如 `关联对象`
- 关系 `description` / `note` 的页面编辑能力比较弱

所以如果对外做 MCP，不应以当前页面交互能力为边界，而应以后台结构化写服务能力为边界。

## 5.6 已有并发控制与写入约束

当前 `StudioWriteService` 已实现以下能力：

- `expectedRevision` 乐观锁
- `clientMutationId` 幂等
- 编辑租约/写锁
- 先形成 candidate，再进入 review/confirmation

这些能力对 MCP 写接口非常重要，因为无论改第一层、第二层还是第三层，都不应该绕过版本控制与复核流程。

## 6. 代码现状结论

当前工程对本次需求的支撑情况不是平均一致的，而是分层成熟度不同：

- 第一层：已有完整内部底座，可较快 MCP 化
- 第二层：只有读能力，写能力需要先补 Studio 底座
- 第三层：已有结构化读写底座，可较快 MCP 化

所以这个需求不能简单理解成“把现有 Studio 包一层 MCP 就完了”。

更准确地说：

- 第一层可以直接做 MCP 封装
- 第二层必须先补建模侧结构化写入能力，再做 MCP 封装
- 第三层可以直接做 MCP 封装，但建议绕过前端粗粒度语义，直接复用后台关系写服务

进一步收口后的修改语义建议是：

- 第一层继续保持现有顶层 patch 语义不变
- 第二层采用字段级 `add / update / remove`
- 第三层采用关系级 `add / update / remove`

这样做的主要原因是并发安全和接入稳定性：

- 第一层是顶层字段，天然适合局部 patch
- 第二层和第三层底层都是数组结构
- 如果直接对外暴露整数组覆盖，外部 Agent 很容易互相覆盖修改
- 因此更建议 MCP 层接收“子项级变更意图”，由服务端内部执行 `read -> merge -> patch`

## 7. MCP 协议规范现状

仓库中现成的、已经对外运行的标准 MCP 实现主要在 `consumer/backend`。

关键位置：

- `consumer/backend/README.md`
- `consumer/backend/api/protocols/mcp/server.py`

consumer 侧已经体现出一套成熟模式：

- 基于 `FastMCP`
- 支持 `Streamable HTTP`
- 支持 `SSE`
- 有独立 endpoint
- 有专用 token 鉴权
- 有 scope 化的工具权限控制

但这里要先把一个边界说清楚：

- `consumer` 侧现有 MCP 主要面向运行态知识查询与问答
- 本次需求要操作的是建模态对象模型草稿、candidate、review、revision
- 因此“协议方案可以复用 consumer”，但“能力承载侧应落在 modeling”

也就是说：

- 不是直接把对象模型写操作塞进 `consumer` 现有 `search/chat` MCP 服务
- 而是要在 `modeling` 侧建设自己的 MCP server / toolset / token / runtime 状态
- 只是在协议实现和治理模式上尽量与 `consumer` 保持同构

## 7.0 结论先行：应接到 Modeling 侧 MCP

对于“通过 MCP 接口支持业务 Agent 查询、校验、修改对象模型元属性、字段定义与对象关系”这个需求，推荐结论非常明确：

1. 权威读写面在 `modeling`

- 对象模型读写、candidate、review、revision 都在 `modeling` 侧
- 真正的落盘内容是对象知识文件，而不是 `consumer` 运行态图查询结果

2. `consumer` 侧现有 MCP 不应直接承接这类建模写操作

- `consumer` 侧当前 MCP toolset 重点是：
  - `search`
  - `chat`
- 它绑定的是：
  - `workspace`
  - `ontology active_version`
- 它适合“查已生效知识包”和“基于运行态知识问答”
- 不适合直接承担“改建模草稿、走 review、受 revision 约束”的职责

3. 最合理的方式是：

- `modeling` 自建一套 MCP 服务
- 路由到 `modeling` 自己的 Studio / workflow / review 底座
- 协议、transport、endpoint、token、scope、status API 设计尽量复用 `consumer` 已验证的模式

一句话收口：

- 本次需求应接到“建模侧自己的 MCP”
- 不是直接接到“consumer 侧现有 MCP 服务”

## 7.1 consumer 侧 MCP 方案整理

当前 `consumer` 侧不是把 MCP 混在主后端进程里，而是采用一套独立部署、统一治理的方案。

核心设计如下：

1. 独立 MCP 进程

- MCP 服务由独立 `FastMCP` 进程承载
- Docker Compose 下也是单独 `mcp` 容器
- 主后端只负责管理 API、鉴权前置能力和运行时信息暴露

2. 管理面与数据面分离

- 管理 API 走主后端的 `/agentic/mcp/*`
- MCP 真正对外通信走独立 FastMCP 服务

这意味着：

- 管理配置、发 token、查状态，不直接暴露在 MCP tool 路由里
- MCP tool 路由只承担协议交互和工具执行

3. 入口按 workspace 绑定

- 每个 workspace 都有一个持久化随机 `mcp_endpoint_id`
- MCP URL 不是固定公共路由，而是带 `mcp_endpoint_id`

典型形式：

- `/{mcp_endpoint_id}/search/mcp`
- `/{mcp_endpoint_id}/chat/mcp`
- `/{mcp_endpoint_id}/search/sse`
- `/{mcp_endpoint_id}/chat/sse`

4. 工具按 toolset 分组

- `search` 和 `chat` 是两套独立 toolset
- 不同 toolset 可以有不同入口、不同权限和不同执行语义

5. Token 支持两级绑定

- 默认可绑定到 `workspace`
- 也可以进一步绑定到某个 `ontology`

也就是说，consumer 侧不是简单“一个 token 到处通用”，而是：

- 先用 endpoint 把入口收口到 workspace
- 再用 token scope / ontology binding 把数据访问范围继续收紧

6. 运行时状态可查询

- 完整 MCP URL 不是靠调用方自己拼
- 而是通过 `GET /agentic/mcp/runtime/status` 获取

这能避免：

- 网关前缀变化
- 外网域名变化
- transport 配置变化

7. transport 可配置

- 支持 `Streamable HTTP`
- 支持 `SSE`
- 支持 `both`

其中：

- `Streamable HTTP` 适合作为主通道
- `SSE` 更多是兼容旧客户端

8. 额外治理能力

- 校验 MCP token
- 校验 workspace ACL
- 校验请求 URL 与 endpoint 绑定关系
- 记录观测埋点
- 管理 session 生命周期

这套方案的特点是：

- 协议实现和业务工具解耦
- 入口隔离、权限隔离、工具分组都比较清楚
- 适合多租户、多项目的企业部署

## 7.2 对 consumer 方案的判断

从工程架构角度，这套方案可以视为业界主流的企业落地方式。

它的核心优点是：

- 不是只做最小 demo，而是把鉴权、隔离、状态管理、URL 管理、toolset 分组一起做了
- 既支持标准 MCP，又考虑了多租户和生产治理

需要注意的是：

- 它带有明显的项目化增强
- 不只是最小官方示例
- 同时保留 `SSE`，属于兼容型生产实现

但对当前这个仓库的产品形态来说，这不是问题，反而是优点。

## 7.3 建模侧应自建同构 MCP，而不是直接挂到 consumer 现有 MCP 服务

对于 Modeling，本次需求建议不要重新设计另一套完全不同的 MCP 体系，但也不应把建模写操作直接挂到 `consumer` 现有 MCP 服务里。

更准确的说法是：

- `consumer` 提供“可复用的 MCP 治理范式”
- `modeling` 提供“真正的对象模型读写与变更流程”
- 最终应建设一套“建模侧自有、但与 consumer 同构”的 MCP

原因有三点：

1. 数据与写入权威在 modeling

- 对象模型结构定义在 `modeling/src/types.ts`
- 对象模型校验和变更预览在：
  - `modeling/src/workflow/ontology-model-validation.ts`
  - `modeling/src/workflow/ontology-object-model-mutation.ts`
- 对象模型真正写回 candidate 的能力在：
  - `modeling/src/agent/studio-write-service.ts`
- review 与 confirm 生命周期也在 `modeling` 侧

2. 并发控制语义是 project / candidate / revision，不是 consumer 运行态查询语义

- 本次写操作依赖 `expectedRevision`
- 后续 review / apply 还会依赖 candidate 校验收据
- 这些都属于建模态流程控制，而不是运行态知识检索流程控制

3. 运行态 MCP 和建模态 MCP 的职责不同

- `consumer` MCP 更偏“读运行态知识、做问答和图检索”
- `modeling` MCP 更偏“读草稿、校验变更、提交草稿、提 review、查 review 状态”

因此，这里推荐的是：

- 复用 consumer 的 `FastMCP + transport + endpoint + token + scope + status API` 思路
- 但在 `modeling` 中建设独立 MCP 进程、独立 toolset 和独立管理面

建议如下：

1. 仍然采用独立 MCP 进程或独立容器

- 不把建模 MCP 直接塞进主应用请求线程
- 主应用负责管理 API
- MCP 进程负责 tool 调用

2. 仍然采用“管理面 / 工具面分离”

建议建模侧也保留两类入口：

- 管理入口：项目内查看状态、发 token、回收 token、查看 toolset
- MCP 入口：真正给外部 Agent 连接的协议地址

3. 绑定粒度从 workspace 映射为 project

consumer 侧按 `workspace` 绑定入口。

Modeling 侧更合理的映射是按 `project` 绑定入口，即：

- 每个建模项目持久化一个 `mcp_endpoint_id`
- 每个项目暴露自己的 MCP 连接地址

原因是：

- 本体建模、Studio 草稿、review、working revision 都是项目级隔离
- 当前工程的权限边界本身也是按 project 做的

4. 不建议把 endpoint 直接绑到 working revision

原因是：

- working revision 是并发控制字段，不是租户边界字段
- 版本一致性应继续通过 `expectedRevision` 解决
- endpoint/token 更适合绑定稳定的 project 维度

5. toolset 也建议分组

consumer 侧是 `search` / `chat`。

Modeling 侧建议分成至少两组：

- `read`：只读查询类
- `change`：校验、修改、提 review 类

这样做的好处是：

- 只读 Agent 不必拿写权限
- 写操作可以单独做更严格的 scope 和审计
- 对外部业务 Agent 来说，权限边界更清楚

6. transport 策略建议保持一致

- 主推 `Streamable HTTP`
- 保留 `SSE` 作为兼容模式
- 运行时通过 status API 返回完整 URL

## 7.3.1 当前 Modeling 已有的内部工具现状

当前 `modeling` 并不是完全没有这批能力。

已经存在的内部工具入口：

- 路由：`modeling/src/agent/core-routes.ts`
- 内部接口：`POST /internal/agent/tools/:tool`

当前已经接入的对象模型相关内部工具包括：

- `list_ontology_objects`
- `get_ontology_object`
- `validate_ontology_object_patch`
- `apply_ontology_object_patch`
- `submit_ontology_object_review`

对应实现位于：

- `modeling/src/agent/core-service.ts`

这说明当前真实状态不是“功能完全没有”，而是：

- `modeling` 侧对象模型工具能力已经有内部实现
- 但它还是内部 agent tool 入口
- 还没有独立的标准 MCP server / transport / token / status 管理

所以后续落地时，不应从零重写对象模型工具本身，而应：

- 保留现有 `core-service` 的对象工具实现
- 在其外层补建模侧标准 MCP 暴露层
- 再补 token、scope、endpoint、runtime status 与前端接入管理

## 7.3.2 consumer 侧在这件事里的合理角色

`consumer` 在这个需求里仍然有价值，但角色应限制在“参考或复用协议治理实现”，而不是“承载建模写操作”。

更合理的角色边界是：

1. 可复用的部分

- `FastMCP` 选型
- `Streamable HTTP + SSE` transport 组织方式
- `mcp_endpoint_id` 持久化思路
- token store / token scope / runtime status 的治理模式
- toolset 分组方式

2. 不建议直接复用为承载面的部分

- 现有 `consumer` 的 `search/chat` toolset
- 现有按 `workspace + ontology active_version` 绑定的运行态语义
- 现有对运行态图数据和问答编排的执行桥接

如果后续希望平台上“看起来是一套统一 MCP 方案”，推荐做法也应是：

- `consumer` MCP 继续负责运行态能力
- `modeling` MCP 新增负责建模态能力
- 两侧在接入方式、token 风格、status API 风格、transport 风格上保持一致
- 而不是把两类职责硬塞进一个服务里

## 7.4 建模侧推荐的 MCP 路径设计

如果建模侧照 consumer 方案落地，建议形成如下路径结构。

管理 API：

- `GET /api/projects/:id/mcp/runtime/status`
- `GET /api/projects/:id/mcp/tokens`
- `POST /api/projects/:id/mcp/tokens`
- `DELETE /api/projects/:id/mcp/tokens/:tokenId`

MCP 工具入口：

- `/{mcp_endpoint_id}/read/mcp`
- `/{mcp_endpoint_id}/change/mcp`
- `/{mcp_endpoint_id}/read/sse`
- `/{mcp_endpoint_id}/change/sse`

其中：

- `read` toolset 对应查询对象元属性、字段定义、对象关系
- `change` toolset 对应 validate、update、submit review

## 7.5 建模侧推荐的 token 绑定方式

建模侧建议也采用两级绑定思想，但绑定对象要换成更符合 Modeling 语义的粒度。

建议优先级如下：

1. 默认按 `project` 绑定

- 适合作为建模 MCP 的默认 token
- 能覆盖一个项目下的完整本体建模空间

2. 如需进一步收紧，可按“项目内目标范围”绑定

第一版不建议直接绑定到 working revision，但可以为后续预留更细粒度限制，例如：

- 指定只读
- 指定只允许某类 toolset
- 指定只允许对象模型相关修改

当前更务实的做法是：

- token 先绑定 project
- 权限细化靠 scope
- 并发一致性继续靠 `expectedRevision`

## 7.6 建模侧为什么要跟 consumer 保持同一套方案

建议保持同一套总体方案，原因有三点：

1. 降低认知和维护成本

- 平台只维护一套 MCP 治理模型
- 使用方理解成本更低

2. 复用现成经验

- `mcp_endpoint_id`
- runtime status
- token store
- scope 校验
- toolset 分组

这些 consumer 侧已经证明可用，没必要在 Modeling 侧再造一套不同的轮子

3. 后续更容易统一运营

- 文档体系可以统一
- 接入方式可以统一
- 监控、审计、运维方式可以统一

因此，如果 Modeling 侧补 MCP，建议沿用这一套模式，而不是重新发明另一种协议封装。

当前 Modeling 侧缺的是：

- MCP server
- MCP token / scope / runtime 管理
- 与 Studio 服务的工具封装层

## 8. 需要实现哪些接口

基于最新范围，建议把接口拆成三组。

## 8.1 第一层接口：对象模型元属性

查询类：

- `list_objects`
- `get_object_metadata`
- `get_object_metadata_schema`

校验类：

- `validate_object_metadata_patch`

修改类：

- `update_object_metadata`
- `submit_object_metadata_review`

## 8.2 第二层接口：对象字段定义

查询类：

- `get_object_fields`
- `get_object_fields_schema`

校验类：

- `validate_object_field_add`
- `validate_object_field_update`
- `validate_object_field_remove`

修改类：

- `add_object_field`
- `update_object_field`
- `remove_object_field`
- `submit_object_fields_review`

## 8.3 第三层接口：对象关系

查询类：

- `get_object_relations`
- `get_object_relation_schema`
- `get_object_relation_graph`

校验类：

- `validate_object_relation_add`
- `validate_object_relation_update`
- `validate_object_relation_remove`

修改类：

- `add_object_relation`
- `update_object_relation`
- `remove_object_relation`
- `submit_object_relations_review`

如果后续想减少工具数量，也可以把三层合并为统一工具：

- `get_object_model`
- `validate_object_model_patch`
- `update_object_model`

但从当前工程现状看，分开更清楚，因为三层的成熟度并不一致。

其中修改语义建议明确为：

- 第一层：顶层 patch
- 第二层：字段级 `add / update / remove`
- 第三层：关系级 `add / update / remove`

## 9. 哪些内部接口已实现，哪些还需要补足

## 9.1 已有可直接复用的内部接口

可直接复用：

- `GET /studio/schema`
- `GET /studio/elements`
- `GET /studio/elements/:elementId`
- `GET /studio/graph`
- `PATCH /studio/elements/:elementId`
- `POST /studio/review`

这些接口已经足够支撑：

- 第一层查询
- 第二层查询
- 第三层查询
- 第一层修改
- 第三层修改
- 第一层提 review
- 第三层提 review

## 9.2 当前还缺的内部能力

### 对第二层字段定义写入的缺口

至少需要补：

1. schema 开放

- 将对象 `fields` 从只读改为可写，或新增专用字段编辑接口

2. 字段结构输入协议

- 定义字段级 `add/update/remove` 的标准输入格式
- 明确字段唯一定位方式，例如按 `field.key`

3. 字段级校验器

至少要校验：

- `key` 唯一性
- `key` 命名合法性
- `cnName`/`type`/`required`/`note`/`alias` 格式
- `sourceColumn` 合法性
- `valueBindingRef` 兼容性
- 主键字段兼容性

4. 写回逻辑

- 把字段级变更在服务端合并后写回 `SpecObject.fields[]`

5. 渲染/解析链路回归

- 确保 `package-render` 与 `package-import` round-trip 一致

6. 前端编辑器

- Studio 页面提供字段表编辑器
- 或 MCP-only 先行，前端后补

### 对第二层、第三层的并发合并策略

第二层和第三层都属于数组型结构，因此 MCP 层不建议简单把外部输入直接透传为整数组覆盖。

建议统一采用：

- MCP 工具表达单次意图
- 服务端内部 `read -> merge -> patch`

具体含义是：

1. 先读取当前最新对象结构
2. 基于 `expectedRevision` 校验版本一致性
3. 将本次字段级或关系级变更合并到当前最新结构
4. 生成新的 `fields[]` 或 `relations[]`
5. 再调用底层写服务提交最终 patch

这样做的好处是：

- 降低外部 Agent 因自己拼整数组而覆盖他人修改的概率
- 审计信息更清楚，能明确记录本次到底是 add、update 还是 remove
- 与现有乐观锁和幂等机制更容易配合

## 9.3 当前还缺的 MCP 层能力

无论第一层、第二层还是第三层，都还缺：

- 建模侧独立 MCP server
- 建模侧 project 级 `mcp_endpoint_id`
- 建模侧 runtime status API
- 建模侧 MCP token 管理
- 建模侧 MCP scope 管理
- 建模侧 MCP runtime 状态管理
- 将内部对象模型工具暴露为标准 MCP toolset 的封装层
- 独立的 `read/change` transport 入口

这里再强调一次：

- 当前不是“项目没有 MCP 概念”
- 而是“项目还没有 modeling 侧独立 MCP 暴露层”

当前已存在的是：

- `consumer` 侧对外 MCP
- `modeling` 侧内部 agent tool

当前缺的是把后者提升为：

- 标准 MCP tool
- 标准 MCP transport
- 标准 MCP token / scope / status 治理

## 9.4 建议将校验模块独立成可复用领域模块

这次需求里的“校验”不建议散落在：

- MCP tool handler
- 前端表单
- `StudioWriteService` 的分支逻辑里

更合理的做法是单独沉淀成一个可复用的领域校验模块，由不同入口统一调用。

### 9.4.1 建议放置位置

建议放在 `modeling/src/workflow` 下，而不是放在 `agent` 目录下。

推荐新增类似文件：

- `modeling/src/workflow/ontology-model-validation.ts`

原因是：

- `agent` 更偏 transport / route / orchestration
- `workflow` 更偏知识包与本体领域逻辑
- 后续不仅 MCP 会复用，Studio 写入、批量导入、review 前预检也都可能复用

### 9.4.2 这个模块应该负责什么

建议把校验拆成三层：

1. 输入格式与变更语义校验

- 请求结构是否合法
- `field_key` / `relation_key` 是否完整
- `add / update / remove` 语义是否冲突

2. 对象模型领域规则校验

- 第一层元属性格式是否合法
- 第二层字段定义是否重复、命名是否合法、类型是否合法
- 第三层对象关系是否存在非法目标、非法关系类型、重复关系

3. 跨对象 / 跨文件一致性校验

- 修改后对象引用是否仍然完整
- 渲染回知识包后是否还能通过 `spec-validation`
- 是否破坏现有知识包结构约束

### 9.4.3 这个模块不应该负责什么

这个模块不建议直接负责：

- HTTP 路由处理
- MCP 协议适配
- token 鉴权
- 写锁与租约控制
- candidate 持久化
- review / apply 状态推进

这些都应该由上层服务处理。

校验模块本身应该尽量保持“纯领域逻辑 + 可重复调用”。

### 9.4.4 建议输入输出形式

建议它接收的不是前端表单态，也不是 MCP 原始请求，而是统一后的领域变更描述。

例如统一成：

- `projectSnapshot`
- `elementSnapshot`
- `mutationIntent`
- `validationOptions`

返回统一结构：

- `ok`
- `errors[]`
- `warnings[]`
- `normalizedMutation`
- `affectedPaths[]`

这样做的好处是：

- 前端、Studio、MCP、批量任务都能共用
- 错误码和错误定位能保持一致
- 后续如果要加“仅校验不落库”接口，也能直接复用

### 9.4.5 推荐的复用入口

后续至少建议被下面几类入口复用：

1. Studio 页面保存
2. MCP `validate_*` 工具
3. MCP `patch_*` / `apply_*` 前置校验
4. review 前预检
5. 批量导入或脚本化改包

也就是说：

- 校验逻辑只维护一份
- 不同入口只做参数适配和流程编排

### 9.4.6 与当前工程现有校验能力的关系

当前工程里已经有多类校验能力，例如：

- `modeling/src/workflow/spec-validation.ts`
- `modeling/src/workflow/knowledge-markdown-contract.ts`
- `modeling/src/agent/studio-write-service.ts` 内部的字段与关系校验

因此这次不建议从零再造一套。

更合理的方向是：

- 把对象元属性、字段定义、对象关系相关校验从写服务流程里抽出来
- 复用已有 `spec-validation` 作为最终一致性兜底
- 形成“局部变更校验 + 全量规格校验”两段式结构

### 9.4.7 对本次需求的直接影响

本次如果要做 MCP 化，建议同步把校验模块抽出来。

否则很容易出现三套规则：

- Studio 一套
- MCP 一套
- review 前预检再一套

这会导致：

- 同一份修改在不同入口表现不一致
- 错误提示不一致
- 后续维护成本明显变高

所以这里建议把“独立校验模块”视为本次需求的基础建设项，而不是后补优化项。

### 9.4.8 V1 建议先收口为单文件入口

考虑到你当前希望“把校验模块单独写在一个位置，后续可复用”，V1 建议先不要拆太散。

建议先统一收口到：

- `modeling/src/workflow/ontology-model-validation.ts`

这个文件作为唯一公开入口。

V1 先这样做有两个好处：

- 便于检查所有校验规则是否真的只维护了一份
- 便于后续 Studio、MCP、review 统一接入

如果后续体量明显变大，再往下拆成目录，例如：

- `ontology-model-validation/meta.ts`
- `ontology-model-validation/fields.ts`
- `ontology-model-validation/relations.ts`
- `ontology-model-validation/cross-spec.ts`

但对外仍保持一个稳定入口文件，避免调用方跟着频繁改 import。

### 9.4.9 建议的公共类型

建议校验模块内部统一围绕“快照 + 变更意图”工作，而不是直接吃前端表单或 MCP 原始 payload。

建议至少定义下面几类公共类型：

```ts
export type OntologyMutationLayer =
  | 'object_meta'
  | 'object_fields'
  | 'object_relations';

export type OntologyValidationMode =
  | 'validate_only'
  | 'save_draft'
  | 'apply_preflight';

export interface OntologyProjectSnapshot {
  projectId: string;
  revisionOid: string;
  spec: PackageSpec;
}

export interface OntologyElementSnapshot {
  elementId: string;
  objectKey: string;
  object: SpecObject;
}

export interface ValidationIssue {
  code: string;
  level: 'error' | 'warning';
  path: string;
  message: string;
  suggestion?: string;
}

export interface OntologyValidationResult<TIntent = unknown> {
  ok: boolean;
  errors: ValidationIssue[];
  warnings: ValidationIssue[];
  normalizedMutation: TIntent | null;
  nextObject: SpecObject | null;
  affectedPaths: string[];
}
```

其中：

- `projectSnapshot` 用来做跨对象和全量 spec 校验
- `elementSnapshot` 用来做单对象局部校验
- `normalizedMutation` 表示已经过 trim、去重、默认值补齐后的标准变更
- `nextObject` 表示本次修改后的对象预览态，供 preview、review、apply 复用

### 9.4.10 建议的变更意图类型

建议把三层修改统一抽象成显式 mutation intent。

第一层元属性：

```ts
export interface ObjectMetaPatchIntent {
  layer: 'object_meta';
  changes: Partial<Pick<SpecObject, 'title' | 'description' | 'aliases' | 'queryBoundary'>>;
}
```

第二层字段定义：

```ts
export type ObjectFieldIntent =
  | { layer: 'object_fields'; op: 'add'; field: SpecField; }
  | { layer: 'object_fields'; op: 'update'; fieldKey: string; changes: Partial<SpecField>; }
  | { layer: 'object_fields'; op: 'remove'; fieldKey: string; };
```

第三层对象关系：

```ts
export type ObjectRelationIntent =
  | { layer: 'object_relations'; op: 'add'; relation: { kind: string; targetId: string; label?: string; description?: string; }; }
  | { layer: 'object_relations'; op: 'update'; relationKey: { kind: string; targetId: string; }; changes: { label?: string; description?: string; }; }
  | { layer: 'object_relations'; op: 'remove'; relationKey: { kind: string; targetId: string; }; };
```

这样做的目的不是为了抽象好看，而是为了让：

- MCP
- Studio
- review 预检
- 后续脚本改包

都围绕同一套领域语义工作。

### 9.4.11 建议的公开函数清单

建议公开函数保持少而稳，V1 控制在 3 到 5 个。

建议至少有：

1. `validateOntologyMutation(input)`

- 核心总入口
- 输入 `projectSnapshot + elementSnapshot + mutationIntent + mode`
- 返回统一 `OntologyValidationResult`

2. `validateObjectMetaMutation(input)`

- 第一层专用校验
- 校验元属性格式、空值、别名重复、queryBoundary 基本规范

3. `validateObjectFieldMutation(input)`

- 第二层专用校验
- 校验字段 key、类型、重复、主键兼容性、sourceColumn / valueBindingRef 合法性

4. `validateObjectRelationMutation(input)`

- 第三层专用校验
- 校验关系目标是否存在、关系类型是否合法、是否重复、是否自环

5. `validatePatchedSpecIntegrity(input)`

- 用于最终一致性兜底
- 对合并后的 `nextObject` 回写到 spec 后，再跑一次全量规格校验

建议调用顺序固定为：

1. normalize mutation
2. 局部语义校验
3. 生成 `nextObject`
4. 回填到临时 spec
5. 执行全量一致性校验
6. 输出 `errors / warnings / nextObject`

### 9.4.12 建议的内部私有函数清单

为避免公开 API 过多，下面这些函数建议只在模块内部使用：

- `normalizeMetaPatch(...)`
- `normalizeFieldIntent(...)`
- `normalizeRelationIntent(...)`
- `buildNextObjectFromMutation(...)`
- `findFieldByKey(...)`
- `findRelationByKey(...)`
- `dedupeAliases(...)`
- `dedupeRelations(...)`
- `assertFieldKeyValid(...)`
- `assertRelationTargetExists(...)`
- `collectAffectedPaths(...)`

其中最关键的是：

- `buildNextObjectFromMutation(...)`

因为很多后续能力都依赖“本次修改后的对象预览态”，包括：

- validate-only
- review 展示 diff
- apply 前复检
- 前端预览

所以这个能力虽然服务于校验，但本身应尽量做成纯函数。

### 9.4.13 建议复用现有代码，而不是重写

当前仓库里已有不少可复用能力，建议直接吸收：

- `modeling/src/workflow/keys.ts`
  - 复用 `key` 合法性校验
- `modeling/src/workflow/spec-validation.ts`
  - 复用全量规格一致性校验
- `modeling/src/workflow/package-render.ts`
  - 复用渲染前最终检查
- `modeling/src/agent/studio-write-service.ts`
  - 抽取现有对象关系校验、局部字段归一化思路

这一步的重点是“抽出来”，不是“复制一份”。

否则 MCP 校验和 Studio 校验很快又会漂移。

### 9.4.14 建议的错误码分层

建议错误码不要混成一类字符串，至少按 4 层分：

1. 请求语义错误

- `INVALID_MUTATION`
- `EMPTY_CHANGES`
- `UNSUPPORTED_LAYER`
- `UNSUPPORTED_OPERATION`

2. 第一层元属性错误

- `OBJECT_TITLE_EMPTY`
- `OBJECT_ALIAS_DUPLICATE`
- `OBJECT_QUERY_BOUNDARY_INVALID`

3. 第二层字段定义错误

- `FIELD_NOT_FOUND`
- `FIELD_KEY_INVALID`
- `FIELD_KEY_DUPLICATE`
- `FIELD_TYPE_INVALID`
- `FIELD_SOURCE_COLUMN_INVALID`
- `FIELD_VALUE_BINDING_INVALID`
- `FIELD_PRIMARY_KEY_CONFLICT`

4. 第三层对象关系错误

- `RELATION_NOT_FOUND`
- `RELATION_KIND_INVALID`
- `RELATION_TARGET_NOT_FOUND`
- `RELATION_DUPLICATE`
- `RELATION_SELF_LOOP_FORBIDDEN`

5. 全量规格错误

- `SPEC_INTEGRITY_BROKEN`
- `PACKAGE_RENDER_FAILED`
- `KNOWLEDGE_CONTRACT_BROKEN`

这样后续：

- MCP 返回结构化错误
- 前端高亮定位
- review 页面展示原因

都更容易统一。

### 9.4.15 建议的返回结构细节

建议 `errors[]` 和 `warnings[]` 都携带足够定位信息。

例如：

```json
{
  "ok": false,
  "errors": [
    {
      "code": "FIELD_KEY_DUPLICATE",
      "level": "error",
      "path": "fields.customer_level.key",
      "message": "字段 key 重复",
      "suggestion": "请改为未被占用的字段 key"
    }
  ],
  "warnings": [],
  "normalizedMutation": {
    "layer": "object_fields",
    "op": "add",
    "field": {
      "key": "customer_level",
      "cnName": "客户等级",
      "type": "string",
      "required": false,
      "note": "客户分层标签"
    }
  },
  "nextObject": null,
  "affectedPaths": [
    "objects/customer.md"
  ]
}
```

这里建议：

- 有 error 时，`nextObject` 可以为空
- 只有 warning 时，允许返回 `nextObject`
- `affectedPaths` 即便失败也可以尽量返回，方便日志和审计

### 9.4.16 建议的调用时机

这个模块建议在 3 个关键时机调用：

1. 草稿保存前

- Studio 保存对象
- MCP `patch_*`

2. 显式校验时

- MCP `validate_*`
- review 前预检

3. apply 前

- `confirm/apply` 前再跑一次
- 防止候选生成后到正式生效前，校验规则已变化或候选已失真

这里的原则是：

- 不是只在最后 apply 才校验
- 也不是只在前端先校验一遍就结束
- 而是“保存前校验 + review 前校验 + apply 前复检”

### 9.4.17 与 review / apply 的关系

建议把这个模块放在 review / apply 链路之前，作为统一前置能力。

推荐流程：

1. 外部 Agent 或前端发起 mutation
2. 上层服务读取当前项目与对象快照
3. 调用 `validateOntologyMutation(...)`
4. 若失败：
   - 不进入 review
   - 不进入 apply
   - 返回结构化错误
   - 保留原草稿
5. 若通过：
   - 允许写入 candidate
   - 允许发起 review
6. apply 前再次调用校验模块做复检
7. 复检通过后再进入真正的 `confirm / acceptCandidateRevision`

也就是说：

- review 不代替结构化校验
- apply 也不应绕过结构化校验

### 9.4.18 对“校验失败后怎么处理”的建议

建议固定以下行为：

1. 当前正式 revision 不变
2. 当前 mutation 不直接 apply
3. 已有 draft / candidate 不自动清空
4. 返回结构化错误列表
5. 调用方可基于错误继续修正再提交

这点很重要。

如果失败就把 candidate 清掉，外部 Agent 很难做自动修复闭环。

更合理的是：

- 校验失败时保留草稿
- review 拒绝时保留草稿
- 只有显式 discard 时才丢弃草稿

### 9.4.19 建议的实施顺序

如果按落地优先级排，建议顺序是：

1. 先抽出第三层对象关系校验
   - 因为当前已有底座，收益最快
2. 再抽第一层元属性校验
   - 改造风险低
3. 最后补第二层字段定义校验
   - 因为它还涉及 Studio 写入底座补齐

这样做的原因是：

- 能尽快让第一层和第三层先复用到 MCP
- 第二层本来就不是只抽校验就能上线，还要补写入能力

## 10. 建议的输入输出语义

## 10.1 第一层：更新对象元属性

建议输入：

- `project_id`
- `element_id`
- `expected_revision`
- `client_mutation_id`
- `changes`

其中 `changes` 允许：

- `title`
- `description`
- `aliases`
- `queryBoundary`

## 10.2 第二层：更新对象字段定义

第二层改为采用字段级 `add / update / remove`，不直接对外暴露整数组覆盖。

### 10.2.1 新增字段

建议输入：

- `project_id`
- `element_id`
- `expected_revision`
- `client_mutation_id`
- `field`

其中 `field` 结构可包含：

- `key`
- `cnName`
- `type`
- `required`
- `note`
- `alias`
- `sourceColumn`
- `valueBindingRef`

示例：

```json
{
  "project_id": "p1",
  "element_id": "object:customer",
  "expected_revision": "rev_xxx",
  "client_mutation_id": "mcp-add-object-field-001",
  "field": {
    "key": "customer_level",
    "cnName": "客户等级",
    "type": "string",
    "required": false,
    "note": "客户分层标签"
  }
}
```

### 10.2.2 更新字段

建议输入：

- `project_id`
- `element_id`
- `expected_revision`
- `client_mutation_id`
- `field_key`
- `changes`

示例：

```json
{
  "project_id": "p1",
  "element_id": "object:customer",
  "expected_revision": "rev_xxx",
  "client_mutation_id": "mcp-update-object-field-001",
  "field_key": "customer_level",
  "changes": {
    "cnName": "客户等级",
    "note": "客户价值分层标签"
  }
}
```

### 10.2.3 删除字段

建议输入：

- `project_id`
- `element_id`
- `expected_revision`
- `client_mutation_id`
- `field_key`

示例：

```json
{
  "project_id": "p1",
  "element_id": "object:customer",
  "expected_revision": "rev_xxx",
  "client_mutation_id": "mcp-remove-object-field-001",
  "field_key": "customer_level"
}
```

服务端内部应执行：

- 读取当前最新 `fields[]`
- 按 `field_key` 或新增字段内容合并变更
- 生成新的完整 `fields[]`
- 再通过底层写服务写回

## 10.3 第三层：更新对象关系

第三层也改为采用关系级 `add / update / remove`，不直接对外暴露整数组覆盖。

### 10.3.1 新增关系

建议输入：

- `project_id`
- `element_id`
- `expected_revision`
- `client_mutation_id`
- `relation`

其中 `relation` 可包含：

- `kind`
- `targetId`
- `label`
- `description`

示例：

```json
{
  "project_id": "p1",
  "element_id": "object:customer",
  "expected_revision": "rev_xxx",
  "client_mutation_id": "mcp-add-object-relation-001",
  "relation": {
    "kind": "object_relation",
    "targetId": "object:order",
    "label": "关联对象",
    "description": "客户与订单之间的关联"
  }
}
```

### 10.3.2 更新关系

建议输入：

- `project_id`
- `element_id`
- `expected_revision`
- `client_mutation_id`
- `relation_key`
- `changes`

其中 `relation_key` 建议由 MCP 层定义为稳定定位键，例如：

- `kind + targetId`

示例：

```json
{
  "project_id": "p1",
  "element_id": "object:customer",
  "expected_revision": "rev_xxx",
  "client_mutation_id": "mcp-update-object-relation-001",
  "relation_key": {
    "kind": "object_relation",
    "targetId": "object:order"
  },
  "changes": {
    "label": "客户关联订单",
    "description": "客户到订单的业务关联"
  }
}
```

### 10.3.3 删除关系

建议输入：

- `project_id`
- `element_id`
- `expected_revision`
- `client_mutation_id`
- `relation_key`

示例：

```json
{
  "project_id": "p1",
  "element_id": "object:customer",
  "expected_revision": "rev_xxx",
  "client_mutation_id": "mcp-remove-object-relation-001",
  "relation_key": {
    "kind": "object_relation",
    "targetId": "object:order"
  }
}
```

服务端内部应执行：

- 读取当前最新 `relations[]`
- 按 `relation_key` 合并变更
- 生成新的完整 `relations[]`
- 再通过底层写服务写回

## 11. 还需要补充的关键设计点

这一章用于把前面讨论过、但如果不单独收口就容易遗漏的设计点一次性写清楚。

## 11.1 第三层关系修改的联动影响

第三层关系修改和“删除对象本身”不是一回事，建议分开设计。

当前工程对“删除对象”已经有一套较成熟的预览机制：

- `POST /api/projects/:id/studio/deletions/preview`
- `POST /api/projects/:id/studio/deletions/apply`

它的核心是：

- 先计算 `incomingReferences`
- 再提示调用方会影响哪些条目
- 最后决定是否真正删除

对于“删除一条对象关系”，V1 不建议直接复刻一套同样重的 `preview/apply` 双接口。

原因是：

- 删除关系通常只影响单个对象文件
- 它不是删除知识条目本身
- 当前底层关系并不是独立资源，而是 `SpecObject.relations[]` 的一个数组项

因此，V1 更合适的做法是：

- 不单独新增“关系删除预览接口”
- 把影响分析直接纳入 `validate-only` 返回结果

建议至少给出以下影响信息：

- `affectedPaths`
- `affectedObjects`
- `relationDelta`
- `graphImpactSummary`
- `reviewHints`

其中：

- `graphImpactSummary` 用于描述对象图边数变化、某对象是否变成孤立节点
- `reviewHints` 用于提示“建议人工复核”的场景

典型需要提示的场景包括：

- 删除后对象失去最后一条对外关系
- 删除后目标对象不再被任何对象引用
- 删除的是高频访问对象之间的关键关系

建议结论：

- 删除对象：继续保留独立 `preview/apply`
- 删除关系：V1 通过 `validate-only` 返回影响摘要
- 如果后续出现“批量关系清理”场景，再考虑给关系删除补独立 preview

## 11.2 validate-only 的返回内容

`validate-only` 不应只返回“通过 / 不通过”，而应返回一份可复用、可展示、可审计的结构化结果。

建议返回结构包含：

- `ok`
- `errors[]`
- `fieldErrors`
- `warnings[]`
- `impactSummary`
- `normalizedMutation`
- `nextObject`
- `reviewRecommendation`

其中各字段的建议含义如下。

### `errors[]`

阻断型错误，供：

- MCP 调用方自动修复
- 前端高亮定位
- review 前预检拦截

每条至少包含：

- `code`
- `path`
- `message`
- `suggestion`

### `fieldErrors`

给页面表单直接消费的扁平结构。

建议它不是单独维护的一套规则，而是由 `errors[]` 派生生成。

示例：

- `title`
- `queryBoundary`
- `field.key`
- `field.type`
- `relation.targetId`

### `warnings[]`

非阻断问题，但应提示调用方注意。

例如：

- 删除关系后对象图会变稀疏
- 别名存在重复，系统已自动去重
- 历史存量问题仍存在，但本次未继续恶化

### `impactSummary`

这是本次建议新增的重点。

用于告诉调用方：

- 这次修改会影响哪些对象
- 会改动哪些知识包文件
- 会不会改变对象图结构
- 是否建议进入人工 review

建议至少包含：

- `affectedPaths`
- `affectedObjects`
- `affectedRelations`
- `relationDelta`
- `potentiallyImpactedRules`
- `potentiallyImpactedOperations`

其中后两项在 V1 可以先做“基础摘要”，不要求一开始就做到完全精确。

### `normalizedMutation`

返回服务端标准化后的变更意图。

目的有两个：

- 让外部 Agent 知道服务端实际理解成了什么
- 便于做幂等重试和调试

### `nextObject`

校验通过时，建议返回本次修改后的对象预览态。

这样可以直接支撑：

- MCP 调用方预览
- 前端变更确认
- review 差异展示

### `reviewRecommendation`

建议补充一个轻量字段，明确告诉调用方当前建议怎么走：

- `no_review_needed`
- `review_recommended`
- `review_required`

这样可以把“纯结构校验”和“治理策略建议”拆开表达。

## 11.3 review 之后的完整闭环

本次需求如果要做完整，不能只写到 `submit review`，还需要把谁确认、怎么确认、确认后如何 apply 写清楚。

### 当前工程现状

当前建模链路里已经存在比较明确的 review / confirm 机制：

- `POST /api/projects/:id/studio/review`
  - 负责发起 review，请求返回 `202`
- `confirm()`
  - 在 review 已批准后真正接收 candidate revision
- `rejectConfirmation()`
  - 负责退回候选修改

也就是说，当前工程里：

- `review` 不等于正式生效
- 真正的 `apply` 发生在 `confirm -> acceptCandidateRevision(...)`

### 建议的完整状态流转

建议统一成下面这条状态链：

- `draft`
- `validation_failed`
- `review_pending`
- `changes_requested`
- `approved`
- `revalidation_required`
- `applied`
- `discarded`

各状态含义建议如下：

- `draft`：已经形成草稿/候选，但尚未送审
- `validation_failed`：结构校验未通过，不能送审
- `review_pending`：已送审，等待人工确认
- `changes_requested`：被退回修改
- `approved`：review 已通过，具备 apply 前提
- `revalidation_required`：review 后因 candidate/revision/validator 变化，需要复检
- `applied`：已进入正式 revision
- `discarded`：草稿被显式放弃

### 谁来确认

V1 建议保持人工确认。

建议可确认角色包括：

- 项目管理员
- 本体负责人
- 领域专家
- 知识工程师

不建议让普通业务 Agent 在默认权限下直接完成最终 apply。

### MCP 是否允许发起确认发布

V1 建议边界如下：

- MCP 可以：
  - 查询
  - 提交草稿修改
  - 做 `validate-only`
  - 发起 review
  - 查询 review 状态

- MCP 不默认开放：
  - 最终 `apply`
  - 直接确认发布

理由是：

- `apply` 不只是技术动作，而是治理动作
- 它意味着正式 revision 变化、责任归属变化和审计落盘

### V2 的可选扩展

如果后续确实需要“高权限 Agent 自动闭环”，可以在 V2 再开放：

- `apply_reviewed_change`
- `reject_reviewed_change`

但必须额外要求：

- 更细 scope
- 更强审计
- 带 `expectedRevision`
- 带 `expectedCandidateRevision`
- 带 `expectedCandidateHash`
- 带 `expectedValidatorVersion`

## 11.4 review 后再次校验失败时怎么处理

这点很关键，因为 review 通过并不代表 candidate 在最终 apply 时仍然有效。

例如下面几种情况都可能发生：

- review 通过后，底层校验规则升级了
- candidate 内容变化了
- base revision 已变化
- 期间其他修改导致原确认卡失效

当前工程里已经有这类防线：

- apply 前会校验 candidate validation receipt
- 不匹配时要求重新执行完整预检

因此本次需求建议明确规定：

1. review 通过后，如果 apply 前复检失败：

- 不 apply
- 当前正式 revision 不变
- candidate 不自动清空
- 返回 `revalidation_required`

2. 返回结果里建议带：

- 当前 `candidateRevision`
- 当前 `candidateHash`
- 当前 `validatorVersion`
- 失效原因
- 建议下一步动作

3. 不建议自动回滚到空白状态

因为这样会破坏外部 Agent 的修复闭环。

更合理的是：

- 保留 draft / candidate
- 让调用方基于最新错误继续修正

## 11.5 权限与 scope 模型

本次需求如果做成 MCP，权限模型需要单独写清楚。

建议最少拆成下面几类 scope：

- `ontology.read`
- `ontology.change.draft`
- `ontology.change.validate`
- `ontology.change.review`
- `ontology.change.apply`
- `ontology.audit.read`

对应建议如下：

- `read` toolset 只要求 `ontology.read`
- `change` toolset 根据工具再细分 scope
- `apply` 单独做最高权限，不与普通写权限混用

这样做的目的是：

- 避免“能改草稿”就天然“能正式发布”
- 让只读 Agent 和写 Agent 权限边界清楚
- 审计时能明确区分操作等级

## 11.6 幂等、重试与并发控制

这部分虽然前文零散提过，但建议在需求文档里单独写成明确约束。

建议规则如下：

1. 所有写操作必须带：

- `expectedRevision`
- `clientMutationId`

2. 第二层和第三层必须采用：

- 服务端 `read -> merge -> patch`

而不是让外部 Agent 直接传整数组覆盖。

3. review / apply 必须继续依赖：

- `expectedCandidateRevision`
- `expectedCandidateHash`
- `expectedValidatorVersion`

4. 发生并发冲突时：

- 返回明确冲突错误
- 不静默覆盖
- 不自动重试 apply

建议把这部分视为 MCP 写接口的强约束，而不是实现细节。

## 11.7 草稿与 candidate 生命周期

建议把草稿生命周期也写清楚，否则调用方不知道失败后数据会不会丢。

建议规则如下：

1. 修改进入草稿后：

- 可多次 patch
- 可多次 validate
- 可多次 request review

2. 以下场景保留草稿：

- validate 失败
- review 被退回
- apply 前复检失败

3. 以下场景清空草稿：

- apply 成功
- 调用方显式 `discard`

4. 建议补一个明确接口：

- `discard_object_model_draft`

这样外部 Agent 才能显式结束一轮修改会话。

## 11.8 审计与追溯

本次需求既然涉及 Agent 自动修改本体，审计字段必须明确。

建议每次写操作、review、apply 至少记录：

- 调用方系统
- token / endpoint
- 用户名
- projectId
- expectedRevision
- candidateRevision
- mutationIntent 摘要
- 影响对象
- review 结果
- apply 结果
- 时间戳

最好还能保留：

- 结构化错误列表
- warning 摘要
- 受影响文件路径

这样后续排查“是谁通过什么 Agent 改了哪条关系”才有据可查。

## 11.9 字段与关系的稳定定位键

为了让 MCP 能可靠执行 `update/remove`，需要明确稳定定位键。

建议：

- 第二层字段以 `field.key` 作为唯一定位键
- 第三层关系在 V1 先以 `kind + targetId` 作为定位键

但这里要补一句边界：

如果未来业务允许同一对象对同一目标对象存在多条同类关系，那么：

- `kind + targetId` 就不再足够
- 需要给关系补独立 `relationId`

所以 V1 文档里应明确：

- 当前默认一对 `kind + targetId` 唯一
- 若后续语义放宽，要同步升级关系标识设计

## 11.10 页面与 MCP 的协同边界

前端和 MCP 不是两套平行系统，建议在文档里写清楚协同边界。

建议如下：

- 配置中心 `MCP 接入` tab 负责：
  - URL
  - token
  - scope
  - toolset
  - runtime 状态

- Studio 页面负责：
  - 对象内容查看
  - 人工编辑
  - review 结果查看
  - 变更差异确认

这样分工更稳定：

- 配置治理在配置中心
- 知识内容在 Studio

## 11.11 协议与兼容性策略

既然建模侧建议复用 consumer 方案，这里也建议把兼容策略写清楚。

建议：

- 主通道使用 `Streamable HTTP`
- 保留 `SSE` 兼容旧客户端
- 运行时通过 status API 返回完整 MCP URL

同时建议在 status API 里返回：

- `transport`
- `toolsets`
- `scopes`
- `endpointId`
- `serverVersion`

这样外部 Agent 可以先查询运行态，再决定如何连接。

## 11.12 测试与验收建议

为了避免这个需求只停留在接口设计，建议把最小测试范围也写进文档。

至少包括：

1. 校验模块单测

- 第一层元属性校验
- 第二层字段增删改校验
- 第三层关系增删改校验
- review 前复检

2. Studio 集成测试

- 草稿保存
- validate-only
- review 送审
- 人工退回
- apply 成功

3. MCP 集成测试

- token/scope 校验
- `read` toolset
- `change` toolset
- 冲突与幂等

4. 手工验收

- 从读取对象
- 到修改字段/关系
- 到 review
- 到人工确认发布

完整走通一遍

## 12. 实现优先级建议

建议按下面顺序推进。

### 第一阶段

- 建 Modeling MCP server
- 建 project 级 `mcp_endpoint_id`
- 建 `/api/projects/:id/mcp/runtime/status`
- 建 token 管理与 scope 管理
- 开放第一层只读工具
- 开放第二层只读工具
- 开放第三层只读工具

### 第二阶段

- 开放第一层校验与写入工具
- 开放第三层校验与写入工具
- 打通第一层与第三层 review 流程

### 第三阶段

- 补第二层字段定义的 Studio 写入底座
- 开放第二层校验与写入工具
- 打通第二层 review 流程

### 第四阶段

- 补权限治理、审计增强、接入文档

## 13. 最终结论

基于当前代码现状，这个需求现在应当这样定性：

- 第一层对象模型元属性：内部能力已具备，适合直接做 MCP 化
- 第二层对象字段定义：内部读取能力已具备，但写入能力未具备，需要先补底座
- 第三层对象关系：内部读写能力已具备，适合直接做 MCP 化

所以完整结论不是“当前工程已经整体支持这个需求”，而是：

- 第一层和第三层已经有底座
- 第二层需要先做建模侧结构化写能力建设
- `modeling` 侧已经有内部对象模型工具入口，可作为后续 MCP 暴露层的直接底座
- 标准 MCP 方案应建设在 `modeling` 侧，而不是把建模写操作直接挂到 `consumer` 现有 MCP 服务
- 协议与治理模式建议复用 `consumer` 侧已验证的总体方案
- 然后再统一通过建模侧 MCP 暴露给业务 Agent

如果后续还要扩展到：

- 具体业务数据值修改

建议继续作为独立需求单列，不要再并进本次对象模型三层需求。
