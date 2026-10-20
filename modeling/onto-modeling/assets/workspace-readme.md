# Ontology Toolset

基于 Claude Code 的领域知识建模工具集（Onto Modeling 平台内置版）。通过流程化的交互引导，帮助非技术用户对任意业务领域进行系统的资料分析、（可选）创建业务流程图、分步提取业务对象/操作/规则/指标、生成结构化文档，并进行自动校验和场景验证。

---

## 工具集概览

```
资料分析 → [可选：流程图] → 提取对象 → 提取操作 → [可选：规则与指标] → 质量检查 → 示例数据 → 场景验证 → [可选：代码生成]
   ↑              ↑              ↑           ↑                              │             │            │
   └──────────────┴──────────────┴───────────┴──────────────────────────────┴─────────────┴────────────┘
                        检查/验证发现盲区时，可定向补充分析或增量提取
```

> **流程图为可选步骤**：概念清晰、流程线性的领域可以直接从分析报告提取；流程复杂或多子流程交叉的领域则建议先画流程图。

| 阶段 | 命令 | 说明 |
|------|------|------|
| 入口 | `/ontology [领域]` | 检测当前进度，引导到合适的下一步 |
| 资料分析 | `/ontology-research <领域>` | 对领域资料做系统分析，生成分析报告（写入 `research/`） |
| 流程图（可选）| `/ontology-flowchart [领域]` | 创建/初始化/刷新业务流程图；流程简单时可跳过 |
| 对象提取 | `/ontology-extract-object` | 从流程图摘要或分析报告提取业务对象 |
| 操作提取 | `/ontology-extract-operation` | 基于已确认对象提取业务操作 |
| 规则与指标（可选） | `/ontology-extract-rule-metric` | 从资料与已有实体中提取业务规则与指标 |
| 质量检查 | `/ontology-validate [文件名...]` | 分两阶段校验：规则检查 → 语义检查（报告写入 `reports/`） |
| 示例数据 | `/ontology-mock` | 为业务对象生成 mock 测试数据，并保存 mock 规则校验报告 |
| 场景验证 | `/ontology-trace <操作文件>` | 用具体场景和 mock 数据模拟执行操作；`/ontology-trace 交互` 可逐分支构造场景 |
| 代码生成扫描 | `/ontology-codegen-scan` | 扫全量操作，判定哪些适合转脚本，写带编号的候选报告供人工评审 |
| 代码生成 | `/ontology-codegen [操作名 [操作名:步骤区间]...]` | 从验证过的操作生成 Python 脚本；**禁止一键全部**，必须显式指定 |
| 求解评估器 | `/ontology-solver-evaluator [solver_key]` | 为求解契约生成或修正声明式评估器：只声明取数映射与参数，不写计算代码，并运行自检 |
| 整体视图 | `/ontology-overview` | 生成交互式 HTML 报告：对象关系图 + 操作调用图 + 依赖矩阵 + 脚本清单 + 问题清单 |
| 影响分析 | `/ontology-impact <文件>` | 查看修改该文件会波及的下游文档 |
| 增量更新 | `/ontology-update <参考>` | 按参考内容增量更新已有 ontology，含影响分析 + 自动校验 |
| 反查 | `/ontology-query <关键词>` | 引用/搜索/孤立/未用对象/失效链接/知识 六种只读查询 |
| 流程图校验 | `/ontology-validate-diagrams` | 只读校验流程图与 Excel 补充表对齐，再进行结构 + 语义校验 |
| 版本管理 | `/version [子命令]` | 查看历史/回退/对比/标记版本 |

**阶段之间自动衔接**：每个阶段完成后展示结果摘要并询问是否继续，用户回复"好"即可推进。

## 输出格式（知识包五元组）

```
ontology/
  object/           ← 业务对象文档
  operation/        ← 业务操作文档（旧项目的 logic/ 目录兼容读取）
  rule/             ← 业务规则文档
  metric/           ← 业务指标文档
  scripts/          ← 生成的 Python 脚本（/ontology-codegen 产出）
  data-mock/        ← Mock 测试数据
  diagrams/         ← 业务流程图（.drawio）
  diagrams-summary/ ← 流程图摘要
  mermaid/          ← Mermaid 流程图
  excel/            ← 流程图配套 Excel 补充表（按 sheet 拆分）
  testcase/         ← 每个操作对应一份验证场景（可复用）
  knowledge/        ← 业务知识沉淀（专家纠错/规范/术语/常识）
research/           ← 资料分析报告
reports/            ← 校验/验证/评审报告
resource/           ← 用户上传的领域资料（模型只读；用途标签见 resource/materials.json）
```

文档之间通过 Wiki 链接格式互相引用：`[[文件名]]`（无 `.md` 后缀）。每个目录下的 `index.md` 是平台维护的目录索引，不是实体文档。

## 工作区说明

本工作区由 Onto Modeling 平台创建与管理：

- `.claude/`、`CLAUDE.md`、`README.md` 是平台挂载的只读工具集资源，升级对所有任务自动生效；不要修改或提交它们。
- 工作区有独立的 git，`/ontology-*` 命令产生的提交构成任务的版本历史（平台"版本"页展示）。
- `resource/` 由用户在界面上传维护，模型只读；资料用途与精读要求见 `resource/materials.json`（详见 CLAUDE.md 8.1）。
- `.ontology-state/` 是命令间协调用的工具状态目录，由脚本维护，不进 git。
