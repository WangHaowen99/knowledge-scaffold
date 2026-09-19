# 0004 建模范式对齐 modeling 五元组标准

## Context

onto-web 的提示词资产沿用了早期"对象 + 逻辑（logic）"两元模型，而 modeling 知识包标准已经演进为五元组一等实体：object / operation / rule / metric / script。两侧术语与目录不一致导致：

- onto-web 产出的知识包无法与 modeling 的 manifest / resource 元信息对齐；
- Studio、导出、MCP 工具等出口各自用 "logic" 命名，与包标准的 "operation" 冲突；
- 规则与指标没有独立实体位，被迫混入操作文档正文，无法结构化校验与反查。

## Decision

以 modeling 为标准做整体范式升级：

1. **术语与目录**：`ontology/logic` → `ontology/operation`；提示词资产做词边界机械替换（`logic(s)` → `operation(s)`，保护 `logical` 等误伤词），命令/模板/代理文件相应重命名（如 `/ontology-extract-logic` → `/ontology-extract-operation`）。
2. **新增实体**：`ontology/rule`、`ontology/metric` 目录与模板（规则：规则描述/适用对象/约束表达等；指标：指标描述/计算公式/数据来源等），新增 `/ontology-extract-rule-metric` 命令与 `rule-metric-extraction-agent`，`ontology_checker.py` 注册对应结构与链接校验。
3. **旧目录兼容（只读别名）**：所有读取路径（checker、query、state_helper、impact、Studio KIND_DIRS、进度探针、导出、MCP list_elements）同时接受 `ontology/logic`，同名 stem 去重；新写入一律进 `ontology/operation`。旧章节名「与logic的关联」读取时归一为「与operation的关联」。
4. **导出五元组**：`export_package_zip` 按 modeling 包布局输出 `objects/ operations/ rules/ metrics/ scripts/` 分组，规则与指标同操作一样注入 `uses.objects`（wikilink 推导）；脚本导出 `.py` 并生成伴生 `.md`（`resource: script`）。
5. **API 契约键名**：项目进度与导入摘要 `logics` → `operations`；codegen 候选字段 `logic` → `operation`；前端（LeftPane 阶段、CenterPane 概览/评审、StudioView 类型、ChatPane 快捷项）同步。
6. **阶段链不扩**：规则与指标不进入主阶段链（避免历史项目进度回退），入口放在左栏附加操作与 `/ontology` 状态建议中。
7. **action 实体保留现状**：modeling 无对应物，不迁移不删除，待后续单独决策。

## Alternatives considered

- **双轨并存（logic 与 operation 各自为一等实体）**：出口与校验都要写两套分支，包标准仍不兼容，放弃。
- **一次性迁移旧项目目录（改名落盘）**：需要写迁移脚本并动用户 git 历史，风险大于收益；只读别名可让旧项目零操作继续工作，新增内容自然落新目录。
- **规则/指标进主阶段链**：会让所有已完成主链的历史项目进度条回退，且两者本质是可选补充产物，放弃。

## Consequences

- 新导出的知识包与 modeling 标准逐字段对齐，可直接被 modeling 侧消费。
- 旧项目（`ontology/logic` 目录）继续可读、可校验、可导出，进度与统计合并计数。
- `logics` API 键名属破坏性变更，前后端同版本发布；无第三方消费者。
- 提示词资产中的兼容读取逻辑（双目录、章节别名）构成少量长期维护成本，待旧项目自然消亡后可移除。
