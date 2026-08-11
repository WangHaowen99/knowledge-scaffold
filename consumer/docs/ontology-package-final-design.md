# 本体包消费目标设计（未完全实现）

> 同步说明（2026-07-15）：本文从旧 `meta-wise` 项目迁入统一仓库，作为知识包消费契约的设计基线。文中保留历史术语“本体包”；在本仓库面向产品的统一口径中，它对应“知识包”。

> 实现状态：本文描述目标形态和迁移基线，不是当前版本的 API 契约。当前 `ontology_context` 仅支持 `ontology_id/resource/key`，`business_data_search` 仅支持 `ontology_id/entity/query`，`execute_script` 支持 `ontology_id/operation_key/arguments`。`ontology_evidence_search`、`section/max_chars`、`fields/filters`、`business_data_search(operation_key/query_plan)` 和证据闭环尚未实现；集成时必须以当前工具注册表和 schema 为准。

> 示例数据说明：文中的名称、编号、表名和字段仅用于说明格式，均为虚构的通用工程变更示例，不对应任何客户、项目或生产系统。真实项目资料进入共享仓库前必须完成授权确认和脱敏审查。

版本：v0.2
状态：目标设计（部分未实现）
最后更新：2026-06-24

## 1. 首版目录结构

首版本体包只定义 Agent 消费本体必须依赖的结构。其他能力后续按需要增加，不在第一版里一次性做大。

```text
<ontology_id>/
  manifest.yaml                  # 必选
  index.md                       # 必选

  objects/                       # 必选
    index.md
    <object_key>.md

  operations/                    # 必选
    index.md
    <operation_key>.md

  rules/                         # 可选
    index.md
    <rule_key>.md

  metrics/                       # 可选
    index.md
    <metric_key>.md

  scripts/                       # 可选
    index.md
    <script_key>.md
    <script_file>.py

  mappings/                      # 条件必选：仅当启用 business_data_search 时需要
    object-data-mappings.json

  assets/                        # 可选
    <asset_key>
```

首版把 `objects/` 和 `operations/` 作为必选消费资源，把 `rules/`、`metrics/`、`scripts/`、`assets/` 作为可选能力资源。没有 rules、metrics、scripts 的本体包，仍然可以用于基础对象理解和操作说明。`assets/` 只用于共享文本、配置样例或少量辅助材料，不替代对象、操作、规则、指标和 mapping。暂不把 `references/`、`graph/`、`imports/`、`evals/` 纳入必备目录。

## 2. 目录结构必选和可选说明

### 2.1 基本概念边界

| 概念 | 首版定义 |
| --- | --- |
| 本体包 | 一个业务域的语义消费单元 |
| object | 业务对象，不等于数据库表 |
| operation | 用户问题可映射到的一类业务任务 |
| rule | 可复用的业务判断条件 |
| metric | 有明确口径、公式和边界的业务指标 |
| script | 已注册、可受控执行的确定性脚本 |
| field | 业务字段，不等于真实数据字段 |
| mapping | object 到真实数据资源的映射，不包含连接密钥 |
| provider alias | 本体包中的逻辑数据源别名，由 workspace 绑定到真实连接 |

硬边界：

```text
本体包不保存真实业务数据；
本体包不保存 URL、账号、token；
Agent 不读取任意文件路径，只通过 resource + key 消费；
真实查询必须先确定 object，再走 mapping 和 provider。
```

### 2.2 必选文件

这些文件缺失时，本体包不能被 Agent 稳定消费。

| 路径 | 职责 |
| --- | --- |
| `manifest.yaml` | 本体包身份、名称、描述、版本和语言 |
| `index.md` | 根目录压缩索引，回答“这个本体能解决什么问题” |
| `objects/index.md` | 对象目录，列出可加载的业务对象 |
| `objects/<object_key>.md` | 对象详情，定义字段、关系、查询边界 |
| `operations/index.md` | 操作目录，列出可执行或可分析的业务任务 |
| `operations/<operation_key>.md` | 操作详情，定义输入、输出、流程和涉及对象 |

### 2.3 可选能力文件

这些目录按业务复杂度增加。一旦启用目录，就应提供对应 `index.md`，目录索引声明的详情文件必须存在。本体包导入时做强校验：`index.md` 中声明但找不到详情文件、以及详情文件未进入目录索引都会导致导入失败，避免 root 阶段导航目录与真实资源脱节。运行时若检测到这两类不一致（例如包被绕过导入直接修改），降级为 warning 并记录，不阻断已加载本体的消费。

| 路径 | 启用条件 | 职责 |
| --- | --- | --- |
| `rules/index.md` | 需要显式规则目录 | 规则目录，列出可复用的业务判断条件 |
| `rules/<rule_key>.md` | 声明具体规则 | 规则详情，定义判断条件、适用范围和例外 |
| `metrics/index.md` | 需要显式指标目录 | 指标目录，列出有明确业务口径的指标 |
| `metrics/<metric_key>.md` | 声明具体指标 | 指标详情，定义口径、公式、维度和空值策略 |
| `scripts/index.md` | 需要受控脚本能力 | 脚本目录，列出可受控执行的脚本 |
| `scripts/<script_key>.md` | 声明具体脚本 | 脚本说明，定义绑定操作、参数、输出和执行边界 |
| `scripts/<script_file>.py` | 声明可执行脚本 | 脚本实现，由后端受控执行，默认不进入模型上下文 |
| `assets/<asset_key>` | 需要共享辅助文本 | 可按 key 读取的辅助材料；不保存密钥，不作为语义主干 |

### 2.4 条件必选文件

只有启用真实业务数据查询时，才需要 mapping。

| 路径 | 启用条件 | 职责 |
| --- | --- | --- |
| `mappings/object-data-mappings.json` | 启用 `business_data_search` | 把 object key，以及可选 metric/rule key，映射到 provider alias、真实资源和字段 |

如果本体包只用于语义问答、对象理解、操作说明，不查真实业务数据，可以没有 `mappings/`。

### 2.5 首版暂不定义

这些能力有价值，但不是第一版消费必需。

| 能力 | 暂缓原因 | 后续如何增加 |
| --- | --- | --- |
| `references/` 目录 | 引用资料可以先放在正文“依据”段 | 当需要证据检索和来源审计时增加 |
| `graph/` 目录 | 图谱可由 frontmatter 和链接派生 | 当需要可视化或图谱查询时生成 |
| `imports/` 目录 | 属于导入和治理过程，不是消费必需 | 当导入器和迁移审计成熟后增加 |
| `evals/` 目录 | 属于测试治理，不影响首版消费 | 当需要回归评测时增加 |
| `provider-hints.json` | 首版先用 object mapping 和自然语言 query | 当 provider 差异明显时增加 |

## 3. 首版消费目标

第一版只解决一件事：让 Agent 能稳定地按业务语义消费本体。

消费链路：

```text
用户问题
  -> ontology_context(root)
  -> ontology_context(operation / object / rule / metric)
  -> 必要时 execute_script
  -> 必要时 business_data_search
  -> 输出回答和证据摘要
```

首版不追求：

| 不追求 | 原因 |
| --- | --- |
| 完整本体治理平台 | 先把消费链路跑通 |
| 复杂图谱协议 | 对象和操作关系先用正文和简单 frontmatter 表达 |
| 复杂规则引擎/指标引擎 | 首版只定义规则和指标口径，不做自动推理引擎 |
| 复杂脚本运行时治理 | 首版只定义脚本注册、参数和边界，不展开完整证据闭环 |
| 导入溯源全链路 | 不影响 Agent 消费本体 |

### 3.1 目标工具前置约束（部分尚未实现）

消费工具在运行时维护 read ledger。`ontology_context` 每次成功读取资源后，会记录 `resource/key/source_path/content_hash/read_count` 等元数据；后续工具据此判断是否满足前置条件。

下表描述目标约束，不代表当前工具 schema 已全部支持：

| 工具调用 | 前置条件 | 不满足时 |
| --- | --- | --- |
| `ontology_context(resource!=root)` | 已通过 `ontology_context(resource="root")` 加载本体根目录；Agent 只能使用 `resource + key` 以及可选 `section/max_chars`，不能传入文件路径 | `ROOT_REQUIRED` / `INVALID_ARGUMENTS` |
| `ontology_evidence_search(query=...)` | 已通过 `ontology_context(resource="root")` 加载本体根目录；Agent 只能传自然语言 query 和可选整数 limit，不能传文件路径 | `ROOT_REQUIRED` / `INVALID_ARGUMENTS` |
| `business_data_search(entity=<object_key>)` | 已通过 `ontology_context(resource="root")` 加载本体根目录，并已加载目标对象详情；可选 `fields` 和 `filters` 必须只使用 mapping 白名单中的业务字段，且每个 filter 只能包含 `field/op/value` | `ROOT_REQUIRED` / `OBJECT_CONTEXT_REQUIRED` / `INVALID_ARGUMENTS` |
| `business_data_search(operation_key=<operation_key>)` | 已通过 `ontology_context(resource="root")` 加载本体根目录，并已加载目标操作详情；Agent 不能传入手写 `query_plan` | `ROOT_REQUIRED` / `OPERATION_CONTEXT_REQUIRED` / `INVALID_ARGUMENTS` |
| `execute_script(operation_key=<operation_key>)` | 已通过 `ontology_context(resource="root")` 加载本体根目录，并已加载目标操作详情；脚本必须已注册；Agent 不能传入脚本路径、源码、SQL 或连接信息 | `ROOT_REQUIRED` / `OPERATION_CONTEXT_REQUIRED` / `INVALID_ARGUMENTS` |

目录、root 摘要和 evidence 命中只用于定位候选资源，不等于详情已加载。

当 `business_data_search(operation_key=...)` 触发后端 `query_plan` 时，返回值必须包含 `plan`、`steps`、`result_summary` 和 query_plan 级 `evidence_id`。`result_summary` 按 operation 的 `output_contract` 记录输出目标、step evidence id、行数和执行状态，只做证据汇总，不替代最终自然语言回答。

单对象 `business_data_search(entity=...)` 返回真实业务数据时，结果末尾保留 `证据: <evidence_id>`，并可附带最小 `证据摘要`，例如 provider、resource、row_count 和 request_id。完整证据仍通过 `/agentic/evidence/<evidence_id>` 查回，Runtime Facts 不是数据缓存。

### 3.2 目标证据索引范围（`ontology_evidence_search` 尚未实现）

`ontology_evidence_search` 只索引静态本体证据候选，不索引运行态数据、连接配置、mapping 原文和脚本源码。

| 范围 | 处理 |
| --- | --- |
| `index.md`、`objects/*.md`、`operations/*.md`、`rules/*.md`、`metrics/*.md` | 作为本体语义证据 |
| `scripts/index.md`、`scripts/*.md` | 只索引脚本用途、参数和绑定说明 |
| `assets/` | 可选索引；只索引允许格式且可 UTF-8 读取的共享文本资产，命中后仍需通过 `ontology_context(resource="asset", key=...)` 按 key 读取 |
| `scripts/*.py`、`mappings/*.json`、workspace 连接配置 | 不进入静态本体证据索引 |

Markdown 命中会返回 `section` 和携带 `section` 的 `next_action`，用于从 evidence 片段回到 `ontology_context` 精确加载同一章节。`asset_text` 命中仍按 asset key 读取，不支持章节切片。

### 3.3 目标上下文预算与分段加载（`section/max_chars` 尚未实现）

`ontology_context` 支持可选参数 `max_chars`，用于限制单次返回给模型的上下文字符数。

预算规则：

| 规则 | 说明 |
| --- | --- |
| 不传 `max_chars` | 返回完整紧凑上下文 |
| 传入正整数 | 返回不超过预算上限的前缀，并在首行 `context_meta.budget` 标明是否截断 |
| 最大上限 | 后端会把过大的预算限制在 `50000` 字符 |
| 截断提示 | 正文末尾追加 `context_truncated` 标记，提示加载更小资源或放大预算 |

`max_chars` 是消费运行参数，不写入本体包。

`ontology_context` 也支持可选参数 `section`。当 resource 是 `operation`、`object`、`rule`、`metric` 或 `script` 时，`section` 可以是 `frontmatter` 或 Markdown 标题名，返回内容只包含该片段，并在 `context_meta.section` 标明本次加载范围。找不到章节时返回明确错误，不退回整篇文档。

## 4. Key 规范

本节定义本体包录结构中所有可消费资源的 key 规范，作用范围包括（范围内的这些文件如果涉及key，都必须符合本节的规范）：

| 资源类型 | 角色 | 对应 frontmatter `resource` | 路径 |
| --- | --- | --- | --- |
| 根目录索引 | 目录 | `root` | `index.md`，`key` 固定为 `root` |
| 对象 | 目录 | `object_index` | `objects/index.md` |
| 对象 | 详情 | `object` | `objects/<object_key>.md` |
| 操作 | 目录 | `operation_index` | `operations/index.md` |
| 操作 | 详情 | `operation` | `operations/<operation_key>.md` |
| 规则 | 目录 | `rule_index` | `rules/index.md` |
| 规则 | 详情 | `rule` | `rules/<rule_key>.md` |
| 指标 | 目录 | `metric_index` | `metrics/index.md` |
| 指标 | 详情 | `metric` | `metrics/<metric_key>.md` |
| 脚本 | 目录 | `script_index` | `scripts/index.md` |
| 脚本 | 详情 | `script` | `scripts/<script_key>.md` |
| 辅助资产 | 详情 | `asset` | `assets/<asset_key>` |

`manifest.yaml` 自身的 `id` 属于本体包身份标识，不走本节的资源 key 规范；`mappings/` 是数据映射文件，不使用资源 key，按 object/metric/rule 的 key 索引。

这些 key 是第 3 节消费链路里 Agent 唯一可用的稳定引用：`ontology_context`、`execute_script`、`business_data_search` 只接受 `resource + key`，不接收文件路径。`uses.objects`、`bound_operation`、mapping 等跨资源引用也都以 key 为准。因此 key 的稳定性、合法字符和文件名规则必须统一约束，不能由各目录各自约定。

首版要求上述可消费资源都有稳定 key，但不能假设业务团队一定先提供英文 key。

中文场景下，很多业务对象、操作、规则、指标和脚本的中文术语本身就是稳定名称。首版允许中文 key，同时推荐在条件成熟时补充 ASCII slug，方便跨系统集成、URL、日志和代码引用。

### 4.1 Key 分层

| 字段 | 是否必需 | 说明 | 示例 |
| --- | --- | --- | --- |
| `key` | 必需 | 本体包内稳定引用，可以是中文或 ASCII | `CHANGE_ORDER受影响对象`、`change_order_affected_object` |
| `title` | 必需 | 展示名称，面向人和 Agent | `CHANGE_ORDER受影响对象` |
| `aliases` | 推荐 | 用户可能使用的自然语言说法 | `设计变更影响对象`、`CHANGE_ORDER影响对象` |
| `canonical_key` | 推荐 | ASCII 稳定 key，用于系统集成和后续迁移 | `change_order_affected_object` |

首版工具应以 `key` 为主，同时允许通过 `title`、`aliases`、`canonical_key` 做解析和召回。包校验中的 operation 引用和 mapping key 也按同一组等价标识判断，避免中文主 key 与 ASCII `canonical_key` 并存时误报资源缺失。

### 4.2 合法 key 字符

为了兼顾中文可读性和路径安全，`key` 允许：

| 字符类型 | 示例 |
| --- | --- |
| 中文字符 | `受影响对象` |
| 英文字母 | `change_order` |
| 数字 | `001` |
| 下划线 | `change_order_affected_object` |
| 短横线 | `change-impact` |

`key` 不允许：

| 禁止内容 | 原因 |
| --- | --- |
| `/`、`\` | 会破坏路径边界 |
| `..` | 路径穿越风险 |
| `#`、`?`、`&` | URL 和引用歧义 |
| 首尾空格 | 不稳定且难排查 |
| 换行、制表符 | 不适合作为资源标识 |

建议长度不超过 80 个字符。导入时应做 Unicode 规范化，避免视觉相同但编码不同的 key。

### 4.3 文件名规则

首版推荐文件名与 `key` 一致：

```text
objects/CHANGE_ORDER受影响对象.md
objects/change_order_affected_object.md
```

如果使用中文 key，文件名可以使用中文。后端读取时必须按 UTF-8 处理，并禁止路径穿越。

如果团队希望文件名全部 ASCII，也可以用 `canonical_key` 作为文件名，但 frontmatter 必须保留真实 `key`：

```markdown
---
resource: object
key: CHANGE_ORDER受影响对象
canonical_key: change_order_affected_object
title: CHANGE_ORDER受影响对象
aliases:
  - 设计变更影响对象
---
```

此时导入器需要建立 `key -> 文件路径` 的索引，不能只靠文件名推断 key。

### 4.4 推荐策略

| 场景 | 推荐 key | 说明 |
| --- | --- | --- |
| 业务术语稳定且主要中文使用 | 中文 key | 例如 `CHANGE_ORDER受影响对象`，降低业务维护成本 |
| 已有系统字段或团队已有英文命名 | ASCII key | 例如 `change_order_affected_object` |
| 需要频繁跨 API、日志、代码引用 | ASCII `canonical_key` | 中文 `key` 仍可保留为业务引用 |
| 旧包迁移 | 先保留中文 key | 后续渐进补 `canonical_key`，避免一次性翻译失真 |

关键原则：

```text
key 必须稳定，不一定必须英文；
title 可以变化，key 不应随展示名频繁变化；
aliases 用于自然语言匹配，不作为唯一标识；
canonical_key 是工程友好补充，不应替代业务 key 的语义。
```

## 5. Manifest

`manifest.yaml` 是系统识别本体包的入口。

首版只保留必要字段：

```yaml
kind: ontology-package
schema_version: 0.1
id: example-change-impact
name: 示例工程变更影响分析本体
description: 面向设计变更、物料影响和库存成本分析的业务本体。
version: 0.1.0
language: zh-CN
status: draft
```

字段说明：

| 字段 | 必需 | 说明 |
| --- | --- | --- |
| `kind` | 是 | 固定为 `ontology-package` |
| `schema_version` | 是 | 包结构版本，首版为 `0.1` |
| `id` | 是 | 本体稳定 id |
| `name` | 是 | 展示名称 |
| `description` | 是 | 本体覆盖范围 |
| `version` | 是 | 本体包版本 |
| `language` | 推荐 | 主要语言 |
| `status` | 推荐 | `draft`、`review`、`released`、`deprecated` |

## 6. Root Index

本体包根目录层的`index.md` 是 本体包的入口，包含本体包的元数据、领域范围、典型问题、目录结构等。

它只做压缩索引，不承载长篇业务细节。

模板：

```markdown
---
resource: root
key: root
title: 示例工程变更影响分析本体
---

# 领域范围

本体覆盖 CHANGE_ORDER 设计变更影响分析，关注受影响对象、设计件版本、制造物料和库存成本。

# 典型问题

| 问题 | 推荐入口 |
| --- | --- |
| CHANGE_ORDER 影响了哪些物料 | operation:change_order_change_impact_analysis |
| 变更前后的 SourceItem 版本是什么 | object:change_order_affected_object |

# 目录

- 对象目录：objects/index.md
- 操作目录：operations/index.md
- 规则目录：rules/index.md（可选）
- 指标目录：metrics/index.md（可选）
- 脚本目录：scripts/index.md（可选）

# 消费边界

查询真实数据前必须先加载相关 object；判断类问题优先加载相关 rule，没有规则目录时回到 operation 的业务说明；计算类问题优先加载相关 metric，没有指标目录时不得编造指标口径。
```

## 7. Object 目录

`objects/index.md` 列出可被 Agent 加载的业务对象。

模板：

```markdown
---
resource: object_index
key: objects
title: 对象目录
---

# 对象目录

| object key | 标题 | 别名 | 说明 |
| --- | --- | --- | --- |
| change_order_change_order | CHANGE_ORDER变更单 | 工程变更单、变更指令 | 工程变更单主对象 |
| change_order_affected_object | CHANGE_ORDER受影响对象 | CHANGE_ORDER影响对象、设计变更影响对象 | 记录 CHANGE_ORDER 影响到的对象和版本变化 |
| source_item_version | SourceItem版本 | 设计件版本、SourceItem版次 | 设计件版本 |
```

别名列用于对象路由，多个别名使用 `[,，、;/；]` 任一分隔（英文逗号、中文逗号、顿号、英文分号、斜杠、中文分号）；没有别名时填 `-`。运行时惰性构建并缓存对象候选映射，覆盖 `key`、`canonical_key`、`title`、`alias`、文件名五种标识；当用户问题命中别名时，先定位对象 `key`，再加载对象详情。查找阶段为 O(1) 字典查找，不遍历详情文件；建索引时会遍历所有 `objects/*.md` 读取 frontmatter（`key`、`title`、`aliases`、`canonical_key`）作为权威来源，`objects/index.md` 的别名列只是路由投影，仅靠目录无法完成路由。

目录中的别名是对象详情 frontmatter `aliases` 的路由投影，两处应保持一致；对象详情仍是完整定义的权威来源。导入时应报告目录与详情之间的别名缺失、多余或指向冲突，运行时不再校验一致性。别名不是唯一标识；同一别名命中多个对象时，必须保留全部候选并返回给 Agent，由 Agent 结合问题上下文消歧，不得任意选择一个。

目录只放对象路由所需的摘要和别名，不放字段全文。目录命中只表示找到候选对象，不等于已加载对象详情。

## 8. Object 详情

每个业务对象一个 Markdown 文件。

路径：

```text
objects/<object_key>.md
```

模板：

```markdown
---
resource: object
key: change_order_affected_object
title: CHANGE_ORDER受影响对象
description: 记录一个 CHANGE_ORDER 变更影响到的设计对象、变更前后版本和处理意见。
aliases:
  - CHANGE_ORDER影响对象
  - 设计变更影响对象
mapping: change_order_affected_object
---

# 业务定义

CHANGE_ORDER受影响对象表示某个 CHANGE_ORDER 变更涉及的设计对象及其变更前后版本。

# 字段

| 字段 key | 中文名 | 类型 | 必填 | 业务说明 | 别名 |
| --- | --- | --- | --- | --- | --- |
| change_order_id | CHANGE_ORDER编号 | string | 是 | 工程变更单编号 | 变更单号 |
| affected_object_id | 受影响对象ID | string | 是 | 被变更影响的对象标识 | 影响对象 |
| before_source_item_version_id | 变更前SourceItem版本 | string | 否 | 变更前设计件版本 | 旧版本 |
| after_source_item_version_id | 变更后SourceItem版本 | string | 否 | 变更后设计件版本 | 新版本 |

# 关系

| 关系 | 目标对象 | 说明 |
| --- | --- | --- |
| belongs_to | change_order_change_order | 受影响对象归属于一个 CHANGE_ORDER |
| references | source_item_version | 通过变更前后版本关联 SourceItem 版本 |

# 查询边界

- 查询具体 CHANGE_ORDER 的受影响对象前，必须提供 CHANGE_ORDER 编号。
- 不得将缺失版本信息解释为无影响。
```

首版 object 详情必须有：

| 内容 | 是否必需 |
| --- | --- |
| frontmatter `resource/key/title` | 必需 |
| frontmatter `description` | 推荐 |
| frontmatter `aliases` | 推荐 |
| frontmatter `canonical_key` | 推荐 |
| frontmatter `mapping` | 推荐 |
| 业务定义 | 必需 |
| 字段表 | 必需 |
| 查询边界 | 推荐 |
| 关系表 | 推荐 |

## 9. Operation 目录

`operations/index.md` 列出用户问题可映射到的业务任务。

模板：

```markdown
---
resource: operation_index
key: operations
title: 操作目录
---

# 操作目录

| operation key | 标题 | 输入 | 输出 |
| --- | --- | --- | --- |
| change_order_change_impact_analysis | CHANGE_ORDER变更影响分析 | change_order_id | 受影响物料、版本变化和风险摘要 |
```

## 10. Operation 详情

每个业务操作一个 Markdown 文件。

路径：

```text
operations/<operation_key>.md
```

模板：

```markdown
---
resource: operation
key: change_order_change_impact_analysis
title: CHANGE_ORDER变更影响分析
description: 分析 CHANGE_ORDER 对 SourceItem、TargetItem 和库存成本的影响。
intent_examples:
  - CHANGE_ORDER-001 会影响哪些物料
  - 设计变更前后的 TargetItem 编码是什么
uses:
  objects:
    - change_order_change_order
    - change_order_affected_object
    - source_item_version
  rules:
    - change_effectivity_rule
  metrics:
    - inventory_handling_cost
script:
  key: change_order_change_impact_script
  required: false
---

# 业务目标

根据 CHANGE_ORDER 编号识别设计变更影响到的 SourceItem、TargetItem 和库存风险。

# 输入

| 参数 key | 中文名 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| change_order_id | CHANGE_ORDER编号 | string | 是 | 工程变更单编号 |

# 输出

| 输出 key | 中文名 | 类型 | 说明 |
| --- | --- | --- | --- |
| affected_items | 受影响对象 | array | 受影响 SourceItem / TargetItem 明细 |
| risk_summary | 风险摘要 | object | 库存、批次、成本等风险摘要 |

# 流程

1. 加载 object:change_order_change_order，确认 CHANGE_ORDER 编号和基础状态。
2. 加载 object:change_order_affected_object，查询 CHANGE_ORDER 对应的受影响对象。
3. 加载 object:source_item_version，识别变更前后版本。
4. 必要时查询相关库存对象，形成风险摘要。
5. 当用户提供完整输入且需要确定性汇总时，可以调用 script:change_order_change_impact_script。

# 规则、指标和注意事项

- 缺少 CHANGE_ORDER 编号时必须追问。
- 查不到数据不能直接解释为没有影响。
- 成本类结论必须说明数量和单价来源。
```

Operation 详情必须有：

| 内容 | 是否必需 | 说明 |
| --- | --- | --- |
| frontmatter `resource/key/title` | 必需 | `resource` 固定为 `operation`，`key` 是稳定引用 |
| frontmatter `description` | 推荐 | 概述 Operation 覆盖范围 |
| frontmatter `canonical_key` | 推荐 | ASCII 稳定 key，用于系统集成 |
| frontmatter `intent_examples` | 推荐 | 用户问题示例，辅助意图路由 |
| frontmatter `uses.objects` | 必需 | 声明 Operation 直接依赖的业务对象，必须是数组 |
| frontmatter `uses.rules` / `uses.metrics` | 按需必需 | 涉及可复用规则或指标时必须声明 |
| frontmatter `script` | 按需必需 | 绑定受控脚本时必须声明 |
| 业务目标 | 必需 | 说明 Operation 要解决的业务问题 |
| 输入 | 必需 | 定义输入参数和必填约束；无输入时应显式说明 |
| 输出 | 必需 | 定义输出结果及其语义 |
| 流程 | 必需 | 说明对象加载、查询、判断或计算步骤 |

`uses.objects` 用于在执行 Operation 前确定应加载的对象详情。只要 Operation 涉及业务对象、对象字段或真实业务数据查询，`uses.objects` 就必须包含至少一个对象。不依赖任何业务对象的纯说明或纯流程控制 Operation，也必须显式声明：

```yaml
uses:
  objects: []
```

`uses.objects` 中的引用必须使用对象的稳定 `key`，不得使用 `title` 或 `aliases` 代替。包导入或校验时，应报告 `uses.objects` 缺失、类型错误、重复引用或引用不存在对象；对于正文或查询流程明确使用、但未进入 `uses.objects` 的对象，应给出依赖不完整警告。

Operation 可以引用 rule 和 metric，但不应把可复用规则和指标口径长期埋在 operation 正文里。

## 11. Rule 目录

`rules/index.md` 列出可被 Agent 加载的业务规则。

模板：

```markdown
---
resource: rule_index
key: rules
title: 规则目录
---

# 规则目录

| rule key | 标题 | 说明 |
| --- | --- | --- |
| change_effectivity_rule | 变更生效批次判断规则 | 判断设计变更从哪个批次开始生效 |
```

## 12. Rule 详情

每个业务规则一个 Markdown 文件。

路径：

```text
rules/<rule_key>.md
```

模板：

```markdown
---
resource: rule
key: change_effectivity_rule
title: 变更生效批次判断规则
description: 判断设计变更从哪个批次开始生效。
applies_to:
  objects:
    - change_order_change_order
    - change_order_affected_object
---

# 规则内容

当 CHANGE_ORDER 明确指定生效批次时，以 CHANGE_ORDER 生效批次为准；当未指定时，必须结合在制状态、库存状态和计划投产时间判断。

# 判断条件

| 条件 | 结论 |
| --- | --- |
| CHANGE_ORDER 指定生效批次 | 使用指定批次作为候选生效点 |
| CHANGE_ORDER 未指定生效批次且物料已投产 | 返回风险提示，进入人工确认 |
| CHANGE_ORDER 未指定生效批次且无投产记录 | 使用计划投产批次作为候选，不得直接定论 |

# 例外

- 紧急适航指令或人工审批结论可以覆盖常规切换规则，但必须说明依据。
```

首版 rule 详情必须有：

| 内容 | 是否必需 |
| --- | --- |
| frontmatter `resource/key/title` | 必需 |
| frontmatter `description` | 推荐 |
| frontmatter `canonical_key` | 推荐 |
| frontmatter `applies_to` | 推荐 |
| 规则内容 | 必需 |
| 判断条件 | 推荐 |
| 例外 | 推荐 |

## 13. Metric 目录

`metrics/index.md` 列出可被 Agent 加载的业务指标。

模板：

```markdown
---
resource: metric_index
key: metrics
title: 指标目录
---

# 指标目录

| metric key | 标题 | 说明 |
| --- | --- | --- |
| inventory_handling_cost | 库存处理成本 | 因设计变更导致旧物料不可用时的库存处理成本 |
```

## 14. Metric 详情

每个业务指标一个 Markdown 文件。

路径：

```text
metrics/<metric_key>.md
```

模板：

```markdown
---
resource: metric
key: inventory_handling_cost
title: 库存处理成本
description: 因设计变更导致旧物料不可用时的库存处理成本。
unit: CNY
grain: material + warehouse + batch
uses:
  objects:
    - material_inventory
---

# 业务口径

库存处理成本 = 待处理数量 * 库存单价。

# 计算边界

- 只统计被 CHANGE_ORDER 影响且不能改制、不能替代、不能继续消耗的旧物料。
- 在途采购不计入库存处理成本，应进入采购风险。

# 空值策略

| 字段 | 处理 |
| --- | --- |
| 库存单价为空 | 不得按 0 计算，必须返回缺失证据 |
| 待处理数量为空 | 不得计算总成本 |

# 维度

| 维度 key | 说明 |
| --- | --- |
| material_id | 物料 |
| warehouse_id | 仓库 |
| batch_no | 批次 |
```

首版 metric 详情必须有：

| 内容 | 是否必需 |
| --- | --- |
| frontmatter `resource/key/title` | 必需 |
| frontmatter `description` | 推荐 |
| frontmatter `unit` | 推荐 |
| frontmatter `grain` | 推荐 |
| frontmatter `uses.objects` | 按需必需 |
| 业务口径 | 必需 |
| 计算边界 | 必需 |
| 空值策略 | 推荐 |
| 维度 | 推荐 |

## 15. Script 目录

`scripts/index.md` 列出可被 Agent 识别、由后端受控执行的脚本。

脚本目录只提供摘要，不暴露脚本源码。

模板：

```markdown
---
resource: script_index
key: scripts
title: 脚本目录
---

# 脚本目录

| script key | 标题 | 绑定操作 | 参数摘要 |
| --- | --- | --- | --- |
| change_order_change_impact_script | CHANGE_ORDER变更影响分析脚本 | operation:change_order_change_impact_analysis | change_order_id |
```

如果本体包暂时没有脚本，`scripts/index.md` 可以保留空目录：

```markdown
# 脚本目录

当前本体包暂无已注册脚本。
```

## 16. Script 详情

每个可执行脚本一个 Markdown 说明文件，脚本实现文件由后端执行，不直接进入 Agent 上下文。

路径：

```text
scripts/<script_key>.md
scripts/<script_file>.py
```

模板：

```markdown
---
resource: script
key: change_order_change_impact_script
title: CHANGE_ORDER变更影响分析脚本
description: 根据 CHANGE_ORDER 编号输出设计变更影响分析的结构化摘要。
bound_operation: change_order_change_impact_analysis
runtime: python
entrypoint: change_order_change_impact.py
timeout_seconds: 300
---

# 脚本用途

当用户已经提供 CHANGE_ORDER 编号，且需要稳定生成变更影响分析摘要时，可以调用该脚本。

# 输入参数

| 参数 key | CLI 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| change_order_id | --change-order-id | string | 是 | CHANGE_ORDER 编号 |

# 输出

| 输出 key | 类型 | 说明 |
| --- | --- | --- |
| change_order_id | string | CHANGE_ORDER 编号 |
| affected_items | array | 受影响对象摘要 |
| risk_summary | object | 风险摘要 |

# 执行边界

- 只能通过 `execute_script` 调用。
- 不允许 Agent 直接读取或改写脚本源码。
- 脚本路径必须位于当前本体包的 `scripts/` 目录内。
- 参数必须来自脚本说明中的输入参数表。
```

脚本实现示例：

```python
#!/usr/bin/env python3
import argparse
import json

parser = argparse.ArgumentParser()
parser.add_argument("--change-order-id", required=True)
args = parser.parse_args()

print(json.dumps({
    "change_order_id": args.change_order_id,
    "affected_items": [],
    "risk_summary": {"status": "requires_provider_data"}
}, ensure_ascii=False))
```

首版 script 详情必须有：

| 内容 | 是否必需 |
| --- | --- |
| frontmatter `resource/key/title` | 必需 |
| frontmatter `description` | 推荐 |
| frontmatter `bound_operation` | 推荐 |
| frontmatter `runtime` | 必需 |
| frontmatter `entrypoint` | 必需 |
| frontmatter `timeout_seconds` | 推荐 |
| 输入参数表 | 必需 |
| 输出说明 | 推荐 |
| 执行边界 | 必需 |

首版脚本边界：

| 边界 | 说明 |
| --- | --- |
| 脚本必须注册 | 未出现在 `scripts/index.md` 和 `scripts/<script_key>.md` 的脚本不能执行 |
| 路径必须受限 | `entrypoint` 只能指向 `scripts/` 内文件，禁止 `..` |
| 参数必须声明 | Agent 不能自由构造未声明参数 |
| 密钥不在本体包内 | 需要数据连接时走 workspace connection |
| 源码不进上下文 | Agent 只看脚本说明，不看脚本实现 |
| 审批策略受控 | 当 `policy.requires_approval: true` 时，`execute_script` 必须提供 `approval_ref`，并把批准引用写入 Runtime Facts |

## 17. Mapping

只有当本体包需要支持 `business_data_search` 时，才需要 mapping。

路径：

```text
mappings/object-data-mappings.json
```

首版 mapping 只做四件事：

| 事项 | 说明 |
| --- | --- |
| resource key 到 provider alias | 指明 object、metric 或 rule 应该走哪个逻辑数据源 |
| resource key 到 resource | 指明 provider 内部资源名 |
| 业务字段到真实字段 | 指明本体字段如何映射到真实字段；模型只能看到业务字段 key |
| 读取策略 | 声明 `max_rows`、`require_user_filter` 等消费边界 |

模板：

```json
{
  "schema_version": "0.1",
  "default_provider_alias": "business",
  "objects": {
    "change_order_affected_object": {
      "provider_alias": "business",
      "resource": "CHANGE_ORDER_AFFECTED_OBJECT",
      "primary_key": ["change_order_id", "affected_object_id"],
      "fields": {
        "change_order_id": {
          "source": "CHANGE_ORDER_ID",
          "type": "string",
          "required": true
        },
        "affected_object_id": {
          "source": "AFFECTED_OBJECT_CODE",
          "type": "string",
          "required": true
        },
        "before_source_item_version_id": {
          "source": "SOURCE_ITEM_VERSION_ID_BEFORE",
          "type": "string"
        },
        "after_source_item_version_id": {
          "source": "SOURCE_ITEM_VERSION_ID_AFTER",
          "type": "string"
        }
      },
      "policy": {
        "read_only": true,
        "max_rows": 200,
        "require_user_filter": ["change_order_id"]
      }
    }
  },
  "metrics": {
    "handling_cost": {
      "provider_alias": "business",
      "resource": "HANDLING_COST_VIEW",
      "fields": {
        "change_order_id": {"source": "CHANGE_ORDER_ID", "type": "string"},
        "amount": {"source": "HANDLING_AMOUNT", "type": "number"}
      },
      "policy": {
        "read_only": true,
        "max_rows": 100
      }
    }
  },
  "rules": {
    "effectivity_rule": {
      "provider_alias": "business",
      "resource": "EFFECTIVITY_RULE",
      "fields": {
        "rule_id": {"source": "RULE_ID", "type": "string"},
        "batch_no": {"source": "BATCH_NO", "type": "string"}
      },
      "policy": {
        "read_only": true,
        "max_rows": 100
      }
    }
  }
}
```

字段说明：

| 字段路径 | 类型 | 是否必需 | 说明 |
| --- | --- | --- | --- |
| `schema_version` | string | 必需 | 当前 mapping 文件的结构版本，与知识包版本相互独立；首版固定为 `0.1` |
| `default_provider_alias` | string | 推荐 | 默认逻辑数据源别名；资源映射未声明 `provider_alias` 时继承该值 |
| `objects` | object | 条件必选 | object key 到对象数据映射的字典；当业务对象需要查询真实业务数据时，必须提供相应映射 |
| `metrics` | object | 可选 | metric key 到物化指标数据映射的字典；仅用于具有独立数据资源或物化结果的指标 |
| `rules` | object | 可选 | rule key 到规则事实、规则参数或物化判定结果数据映射的字典；规则定义本身不通过此字段映射 |
| `<category>.<resource_key>.provider_alias` | string | 条件必选 | 当前资源使用的逻辑数据源别名；未声明时必须能够继承 `default_provider_alias` |
| `<category>.<resource_key>.resource` | string | 必需 | 该资源在 Provider 中的标识，例如数据库表、视图、索引、图节点 label、图关系 type 或 API 资源名；具体语义由 Provider 定义 |
| `<category>.<resource_key>.primary_key` | array<string> | 推荐 | 唯一标识一条记录的业务字段 key 有序列表；复合主键包含多个字段，字段必须在当前映射的 `fields` 中声明 |
| `<category>.<resource_key>.fields` | object | 必需 | 业务字段到 Provider 字段的映射字典；key 为知识包资源中声明的业务字段 key |
| `<category>.<resource_key>.fields.<field_key>.source` | string | 必需 | 该业务字段在 Provider 数据资源中对应的字段标识，例如数据库列名、图属性名、JSON 路径或 API 字段名 |
| `<category>.<resource_key>.fields.<field_key>.type` | string | 必需 | 字段的统一业务逻辑类型，用于参数校验、值转换和结果序列化，例如 `string`、`number`、`boolean`、`date`、`datetime` |
| `<category>.<resource_key>.fields.<field_key>.required` | boolean | 可选 | 该业务字段在数据记录中是否必须存在且非空，默认 `false`；不表示查询时必须使用该字段作为过滤条件 |
| `<category>.<resource_key>.policy` | object | 推荐 | 当前数据资源的访问方式、查询范围和数据安全策略 |
| `<category>.<resource_key>.policy.read_only` | boolean | 推荐 | 是否禁止通过该映射执行写操作；首版只允许设置为 `true` |
| `<category>.<resource_key>.policy.max_rows` | integer | 推荐 | 单次查询允许返回的最大记录数，是该资源不可被调用参数放大的返回上限 |
| `<category>.<resource_key>.policy.require_user_filter` | array<string> | 可选 | 用户查询必须显式提供的过滤字段候选集合；查询条件必须至少命中其中一个业务字段 key |
| `<category>.<resource_key>.policy.row_level_security` | object | 可选 | 行级可见性策略，定义可信用户或会话属性与资源业务字段之间的绑定关系，详见下文 |

其中：

- `<category>` 可以是 `objects`、`metrics` 或 `rules`。
- `<resource_key>` 是对应 Object、Metric 或 Rule 的稳定资源 key。
- `<field_key>` 是知识包资源中声明的业务字段 key。
- `fields.<field_key>` 必须能在对应资源定义中找到。
- `primary_key`、`require_user_filter` 和行级安全策略引用的都是业务字段 key，不得填写 Provider 物理字段名。
- `source` 是 Provider 侧字段标识，不要求与业务字段 key 同名。

对于不同资源类型：

- `objects` 的 `<field_key>` 对应 Object 详情中的字段 key。
- `metrics` 的 `<field_key>` 对应 Metric 声明的维度、输入字段或物化结果字段。
- `rules` 的 `<field_key>` 对应 Rule 声明的输入事实、规则参数或物化判定结果字段。
- 纯公式 Metric 和纯语义 Rule 不需要数据映射。

`policy.require_user_filter` 是运行时硬约束。只要 mapping 声明了该字段，`business_data_search` 必须在结构化 `filters` 中提供至少一个命中的业务字段；完全不传 filters 或只传其他字段都会被拒绝。该约束用于防止 Agent 对大表做无边界扫描。

`policy.row_level_security` 用于声明行级可见性边界。首版支持 `mode=mandatory_filter`：`resource_field` 必须是 `fields` 中声明的业务字段 key，`subject_attribute` 指向可信认证上下文中的主体属性；运行时必须把 `resource_field` 约束为该主体属性的值，缺少对应过滤条件时返回 `RLS_FILTER_REQUIRED`，无法解析主体属性时返回 `RLS_SUBJECT_UNRESOLVED`。`mode=session_attribute` 预留给后续 Provider 会话变量注入。

`ontology_context(resource="object"|"metric"|"rule")` 加载详情时，可以附带安全的 `data_mapping_summary`，只暴露 `provider_alias`、`resource`、`primary_key`、`field_keys` 和 `policy`。真实字段 `source`、mapping 文件路径、workspace connection、账号和密钥不会进入模型上下文。

本体包校验会返回 `mapping_coverage`，用于批量治理 object、metric、rule 与 mapping 的一致性。摘要只包含 `enabled`、`status`，以及各资源类型的 `total`、`mapped`、`unmapped`、`stale`、`field_count`、`unmapped_keys`、`stale_keys`；它帮助维护者发现未映射资源和废弃 mapping key，但不把 mapping 原文、真实字段 source、连接配置或密钥暴露给 Agent。

首版 mapping 不包含：

| 不包含 | 原因 |
| --- | --- |
| URL、host、port | 属于 workspace connection |
| username、password、token | 属于密钥 |
| provider 连接配置 | 由 workspace 管理 |
| 大段 SQL | 后续如需要再引入受控 query template |
| 跨资源或复杂 metric / rule 映射 | 首版 metric/rule 只支持和 object 一样的受控字段映射；复杂 SQL、跨表计算和模板后续扩展 |

## 18. Workspace Binding

本体包只保存 provider alias，不保存真实连接。

运行时由 workspace 绑定 provider alias 到 connection。

建议结构：

```json
{
  "ontology_id": "example-change-impact",
  "default_connection_id": "conn_example",
  "provider_bindings": {
    "business": "conn_example"
  }
}
```

首版解析链路：

```text
business_data_search(entity=change_order_affected_object)
  -> object-data-mappings.json 找 provider_alias/resource/fields
  -> workspace binding 找 connection_id
  -> data connection 找 provider type 和密钥配置
  -> provider 执行查询
  -> 返回结果摘要
```
