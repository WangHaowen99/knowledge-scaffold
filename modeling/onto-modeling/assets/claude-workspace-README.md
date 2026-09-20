# Ontology Toolset

基于 Claude Code 的领域知识建模工具集。通过流程化的交互引导，帮助非技术用户对任意业务领域进行系统调研、（可选）创建业务流程图、分步提取核心业务对象/逻辑、生成结构化文档，并进行自动校验和场景追踪验证。

---

## 工具集概览

```
调研 → [可选：流程图] → 提取对象 → 提取逻辑 → 校验 → 测试数据 → 场景追踪 → 代码生成
 ↑            ↑              ↑           ↑         │            │            │
 └────────────┴──────────────┴───────────┴─────────┴────────────┴────────────┘
                   校验/追踪发现盲区时，可定向补充调研或增量提取
```

> **流程图为可选步骤**：概念清晰、流程线性的领域可以直接从调研报告提取；流程复杂或多子流程交叉的领域则建议先画流程图。用户在 `/ontology-research` 完成或主入口 `/ontology` 的 `调研完成` 状态下都会看到 "流程图 / 直接提取" 两个选项。

| 阶段 | 命令 | 说明 |
|------|------|------|
| 入口 | `/ontology [领域]` | 检测当前进度，引导到合适的下一步 |
| 调研 | `/ontology-research <领域>` | 对领域进行系统调研，生成调研报告 |
| 流程图（可选）| `/ontology-flowchart [领域]` | 创建/初始化/刷新业务流程图；流程简单时可跳过 |
| 对象提取 | `/ontology-extract-object` | 从流程图摘要或调研报告提取业务对象 |
| 逻辑提取 | `/ontology-extract-logic` | 基于已确认对象提取业务逻辑 |
| 校验 | `/ontology-validate [文件名...]` | 分两阶段校验：规则检查 → 语义检查 |
| 测试数据 | `/ontology-mock` | 为业务对象生成 mock 测试数据，并保存 mock 规则校验报告 |
| 场景追踪 | `/ontology-trace <逻辑文件>` | 用具体场景和 mock 数据模拟执行逻辑；可用 `/ontology-trace 交互 [逻辑文件]` 逐分支构造场景 |
| 代码生成扫描 | `/ontology-codegen-scan` | 扫全量逻辑，判定哪些适合转脚本/哪些只能转片段/哪些不适合，写带编号的候选报告供人工评审 |
| 代码生成 | `/ontology-codegen [逻辑名 [逻辑名:步骤区间]...]` | 从验证过的逻辑生成 Python 脚本；支持片段（如 `活动归因:步骤1-3`）；**禁止一键全部**，必须显式指定 |
| 整体视图 | `/ontology-overview` | 生成交互式 HTML 报告：对象关系图（力导向）+ 逻辑调用图（DAG）+ 依赖矩阵 + 脚本清单 + 问题清单；可嵌入「可响应问题」「积木分析」两个子视图 |
| 影响分析 | `/ontology-impact <文件>` | 查看修改该文件会波及的下游文档 |
| 增量更新 | `/ontology-update <参考>` | 按参考内容（文本或文件路径）增量更新已有 ontology，含影响分析+自动校验 |
| 反查 | `/ontology-query <关键词>` | 引用/搜索/孤立/未用对象/失效链接/知识 六种只读查询 |
| 流程图校验 | `/ontology-validate-diagrams` | 只读校验流程图与 Excel 补充表对齐，再进行结构+语义校验 |
| 版本管理 | `/version [子命令]` | 查看历史/回退/对比/标记版本 |

**阶段之间自动衔接**：每个阶段完成后，Claude 会展示结果摘要并询问是否继续下一步，直接回复"好"即可推进。

### 输出格式

```
ontology/
  object/          ← 业务对象文档
  logic/            ← 业务逻辑/流程文档
  data-mock/        ← Mock 测试数据
  scripts/          ← 生成的 Python 脚本（/ontology-codegen 产出）
  diagrams/         ← 业务流程图（.drawio）
  diagrams-summary/ ← 流程图摘要
  mermaid/          ← Mermaid 流程图（从业务逻辑文本或文件生成）
  excel/            ← 流程图配套 Excel 补充表（按 sheet 拆分）
  testcase/         ← 每个逻辑对应一份追踪场景（可复用）
  knowledge/        ← 业务知识沉淀（专家纠错/规范/术语/常识）
```

文档之间通过 Obsidian 链接格式互相引用：`[[文件名]]`（无 `.md` 后缀）。

---

## 快速开始

### 前置要求

- [Claude Code](https://claude.ai/code) CLI
- Python 3.8+
- git

### 开始建模

在工具集仓根目录执行：

```bash
bash init_task.sh 电商订单建模
cd workspace/电商订单建模
claude
> /ontology 电商订单
```

`init_task.sh` 把任务工作区初始化好（独立 .git + 软链工具集），随后 Claude 在任务目录里跑 `/ontology-*` 时所有 commit 自然落到任务自己的 git，不污染工具集仓的历史。详见下文「任务工作区」。

想先跑一遍完整示例熟悉流程？在任务目录里回复 `/ontology 示例` 可一键注入"外卖订单"迷你领域，完整体验调研 → 流程图 → 对象提取 → 逻辑提取 → 校验 → Mock → 追踪 → 代码生成全链路；体验结束后可一键清理。

---

## 任务工作区

工具集的 git 历史里只想看到工具集源码本身的演进，不想被建模产物污染；同时建模过程又需要 git commit 做版本管理——通过 **`workspace/<任务名>/`** 子目录解决：每个建模任务有自己的 .git，commit 计数从 1 开始，与工具集 git 完全隔离。这是建模的**默认工作方式**，不要在工具集仓根直接跑 `/ontology-*`。

### 初始化新任务

在工具集仓根目录执行：

```bash
bash init_task.sh 电商订单建模
```

脚本会在 `workspace/电商订单建模/` 下完成：

1. `mkdir` 任务目录
2. 软链 3 份只读资源（工具集升级自动作用到所有任务）：
   - `.claude → ../../.claude`（命令/agent/scripts）
   - `CLAUDE.md → ../../CLAUDE.md`（项目规则，claude code 在 cwd 自动加载）
   - `README.md → ../../README.md`（命令/使用说明）
3. 复制 2 份任务局部资源（每个任务独立、可独立修改）：
   - `resource/` ← 仓根 `resource/`（源材料起始模板，用户后续放自己的领域材料）
   - `.obsidian/` ← 仓根 `.obsidian/`（Obsidian vault 配置，任务局部状态）
4. 复制 `.claude/templates/task-gitignore.template` 为 `.gitignore`（只排除 `.ontology-state/` 与 3 个 symlink，让建模产物全部进 git）
5. `git init` + 一次 baseline commit 标记任务起点

### 启动建模

```bash
cd workspace/电商订单建模
claude                                    # 在任务目录里启动 Claude Code
> /ontology 电商订单
```

`/ontology-*` 命令的 `git commit` 自然落到任务自己的 git 上（因为 `git rev-parse --show-toplevel` 在 cwd 锚到任务目录）。**「已保存版本 N」从 2 开始数**（版本 1 是 init baseline），与工具集仓的提交计数零交集。

### 目录结构

```
ontology-modeling/                          ← toolset 仓
├── .git/                                    ← 工具集 git
├── .claude/                                 ← 命令/agent/scripts
├── .gitignore                               ← 已包含 workspace/
└── workspace/                               ← gitignored，全部建模任务在此
    ├── 电商订单建模/
    │   ├── .git/                            ← 任务自己的 git
    │   ├── .claude → ../../.claude          ← 软链：工具集命令/agent/scripts
    │   ├── CLAUDE.md → ../../CLAUDE.md      ← 软链：工具集项目规则
    │   ├── README.md → ../../README.md      ← 软链：命令/使用说明
    │   ├── .gitignore                       ← 仅排除 .ontology-state/ 与上述 3 个 symlink
    │   ├── resource/                        ← 复制：源材料起始模板（用户填充自己的领域材料）
    │   ├── .obsidian/                       ← 复制：Obsidian vault 配置（任务局部）
    │   ├── ontology/                        ← 建模产物，由任务 git 跟踪
    │   ├── research/
    │   ├── reports/
    │   └── knowledge/
    └── 医疗HIS建模/
        └── ...
```

### 工具集升级

工具集根目录执行 `git pull`，所有 `workspace/<任务>/` 自动用上新版本（`.claude` / `CLAUDE.md` / `README.md` 都是软链，指向同一份工具集源码）。任务局部资源（`resource/`、`.obsidian/`）是初始化时复制的，升级不会覆盖。

---

## 详细使用流程

### 第一步：调研

```
/ontology-research 电商
```

Claude 派发调研 Agent 系统搜索该领域知识，返回结构化调研报告，保存到 `research/` 目录。

支持本地文档导入、追加调研（分析覆盖盲区后定向补充）、仅基于本地文档（不做网络搜索）等多种模式。

### 第二步（可选）：流程图

```
/ontology-flowchart
```

**何时需要流程图**：流程复杂、多子流程交叉、分支密集时画流程图能显著提升提取准确度。概念清晰、流程线性时可以跳过这一步直接进入提取。

流程图的四种模式：
- **无流程图**：基于调研报告或用户描述，直接生成 .drawio XML 文件
- **有资源图**：从 `resource/diagrams/` 初始化到 `ontology/diagrams/`；若 `resource/excel/` 有配套 `.xlsx`，按 sheet 拆分到 `ontology/excel/{sheet名}.xlsx`，然后打磨
- **已有图**：提示下一步校验或提取
- **由摘要回写**：非技术用户直接编辑 `ontology/diagrams-summary/*.md`，脚本回写成 .drawio（见命令文件 Mode D）

也可以使用 `mermaid-flowchart` skill 直接把业务逻辑转换为 Mermaid `flowchart`：业务逻辑必须由用户提供，可以是一段文字，也可以是一个文件路径。生成的 Mermaid 文件统一保存到 `ontology/mermaid/`；如果输入是文件路径且用户未指定输出名，流程图文件名与原文档 basename 保持一致；如果输入是一段逻辑描述，则自动根据业务主题生成文件名。

如果用户修改的是 `resource/` 中的原始流程图或 Excel，使用刷新模式更新派生产物：

```bash
/ontology-flowchart 全量刷新
/ontology-flowchart 刷新 分析自制件缺料原因、核实BOM信息准确性
/ontology-flowchart 刷新流程图 分析自制件缺料原因、核实BOM信息准确性
/ontology-flowchart 刷新Excel 分析自制件缺料原因、核实BOM信息准确性
```

刷新会更新 `ontology/diagrams/`、`ontology/excel/` 和 `ontology/diagrams-summary/` 中对应产物；不会自动执行完整校验。刷新后建议运行 `/ontology-validate-diagrams`。

### 第三步：渐进式提取

```
/ontology-extract-object
/ontology-extract-logic
```

两个提取命令使用相同的素材来源选择逻辑。素材来源共四种组合：

| 已有素材 | 提取来源 |
|---|---|
| 流程图 + Excel + 调研报告（默认） | 流程图为主，同名 Excel sheet 补充活动细节，调研报告补充背景 |
| 仅流程图 | 流程图摘要；若存在同名 `ontology/excel/{流程名}.xlsx`，同步作为细节补充 |
| 仅调研报告 | 调研报告（跳过流程图场景） |
| 两者都无 | 阻止提取并提示先做调研或流程图 |

两个阶段分开执行：

1. **对象提取**（`/ontology-extract-object`）— 识别业务数据实体 → 校验覆盖度 → 用户确认 → 提交
2. **逻辑提取**（`/ontology-extract-logic`）— 提取业务流程，引用已确认的对象 → 校验 → 确认 → 提交

旧场景对象迁移到新场景时，可以使用 `extract-migrated-objects` skill 做迁移提取业务对象。该 skill 不自动猜路径，必须由用户显式提供：

```text
OLD_OBJECT_PATHS: 旧场景对象目录或 Markdown 文件，可传多个
NEW_SOURCE_PATHS: 新场景调研报告、流程图摘要、Excel、Markdown 或目录，可传多个
```

它会对比旧对象文档与新场景参考资料，判断哪些对象原样复用、哪些需要修改、哪些舍弃、哪些新增，并按当前对象模板写入 `ontology/object/`；写入后会运行对象范围规则检查，并把校验报告保存为 `reports/{YYYYMMDD}-{HHMMSS}-objects-rule.md`。常见触发语包括"迁移提取业务对象"、"迁移object"、"复用提取业务对象"、"迁移业务对象"。

每个阶段完成后独立提交，支持中途暂停和恢复。逻辑完成即视为提取完毕。

### 第四步：校验

```
/ontology-validate
```

分两阶段：
- **规则检查**：自动修复结构性问题，复杂问题逐条确认
- **语义检查**：逻辑质量、参数使用、对象生命周期等深度检查

支持增量校验（只检查指定文件）和回归检查（修复后自动重跑检查新引入的问题）。

### 第五步：Mock 测试数据

```
/ontology-mock
```

为每个业务对象生成 Markdown 表格形式的正常测试数据，保持跨对象引用一致性；生成后会把 mock 规则校验报告保存为 `reports/{YYYYMMDD}-{HHMMSS}-mock-rule.md`。

### 第六步：场景追踪

```
/ontology-trace ontology/logic/创建订单.md
```

使用 mock 数据进行具体场景模拟，发现逻辑漏洞。trace-agent 严格按文档原文推演，不用自身知识补全。

需要人逐个分支定方向时，用交互走查模式：

```bash
/ontology-trace 交互
/ontology-trace 交互 ontology/logic/创建订单.md
```

交互走查会列出判断分支，让业务用户选择要走的方向；Claude 负责把业务特征落成具体 testcase 和必要的正常 mock 数据。边界/异常场景作为 testcase 的自定义输入保存，不写入 `ontology/data-mock/` 的边界/异常分区。

报告分两个独立指标呈现：
- **设计覆盖率** = 场景命中的判断分支 / 全部判断分支（衡量"测试设计够不够全"）
- **推演通过率** = `reached_end=true && issues=[]` 的场景数 / 总场景数（衡量"逻辑/数据有没有问题"）

设计覆盖率高 ≠ 逻辑正确；两个指标必须分开看。问题类型除常见的 `分支缺失` / `死路径` / `状态矛盾` 外，还会标记 `隐式兜底`（AI 想自补 Logic 没写的兜底）和 `规则定义与执行脱节`（其他文件定义了规则但当前 logic 没执行）。

### 第七步：代码生成（可选）

不是所有 logic 都适合脚本化（纯人工决策、政策陈述、跨部门协作流程等不适合）；同一条 logic 也可能只有部分步骤可参数化。推荐先扫一遍候选再生成：

```
/ontology-codegen-scan        # 扫一遍，看哪些逻辑/片段适合（写报告到 reports/）
/ontology-codegen 客群聚类     # 整段
/ontology-codegen 活动归因:步骤1-3 交易对账   # 混合：片段 + 整段
```

**`/ontology-codegen-scan`** 派发 `script-candidate-agent` 按"三问 + 典型类型"判定每条 logic 为 `适合` / `部分适合` / `不适合`，写带编号的 markdown 报告（含片段范围、输入输出概要、判定理由），用户可回 `生成 1,3` / `生成 全部`（会先复述清单等确认）/ `生成 客群聚类` 把选择交给 codegen。

**`/ontology-codegen`** 必须显式指定目标（拒绝 `全部` / `*` 等通配词，避免给不适合脚本化的 logic 生成空壳）。片段语法 `logic:步骤区间`（如 `:步骤1-3` / `:步骤2,4`），文件命名约定：

| 范围 | 文件名 |
|---|---|
| 整段 | `客群聚类.py` |
| 步骤 N | `客群聚类_step3.py` |
| 步骤 N-M | `活动归因_steps1-3.py` |
| 步骤 N,M,... | `风控审批_steps2_4.py` |

片段脚本头部「关联logic」字段加 ` / 步骤X` 后缀（如 `活动归因 / 步骤1-3`），review agent 据此识别。原 logic 文档的对应步骤改写为「调用脚本 ontology/scripts/xxx.py …」+ argparse 调用示例，其他步骤保持原文。

生成后自动进入 **Step 4.5：代码复审**——dispatch `codegen-review-agent` 检查脚本头部模板（7 字段完整性，含 `关联logic` 的片段后缀）/ argparse 入参规范 / logic 文档「逻辑描述」是否已简化（片段脚本只校验 fragment 范围内的步骤）/ 业务知识硬约束遵循（如 `';'` 边界匹配 SQL）。错误级问题由 main agent 自动修复后做一次回归检查；剩余警告/建议按 `/ontology-validate` A5 模式让用户裁决。修复可能同时改写 `ontology/logic/*.md` 的「逻辑描述」段，因此 `git add` 范围覆盖 `ontology/scripts/` + `ontology/logic/`。

---

## 增量更新（/ontology-update）

已经建好的 ontology 需要根据新的输入做局部修改时（专家补充说明、产品迭代、调研追加、用户写的需求纸条），用 `/ontology-update` 一次性完成"影响分析 → 用户确认 → 修改 → 校验"。

```
# 内联参考（直接粘贴提示词）
/ontology-update 客户对象增加手机号字段，必填；新增触达记录对象（渠道、时间、状态）

# 文件参考
/ontology-update notes/2026-05-20-客户字段补充.md
/ontology-update research/最新调研.md
```

流程：

1. **解析参考**：判定是文件路径还是内联文本，加载为 `REFERENCE_CONTENT`。
2. **影响分析**：派发 `update-impact-agent` 比对参考内容 vs 现有 ontology，列出每个待变更文件的 `操作（modify/create/delete）` 和 `变更说明`；再用 `impact_analysis.py` 做静态字面引用兜底，补全级联清单。`ontology/scripts/` 下的 .py 脚本通过头部 docstring 中的「关联logic」字段（或首行 logic 名称）绑定 logic，如果 logic 改动会影响脚本签名/入参/输出，对应脚本会作为 `kind=script` 条目进入清单。
3. **用户确认**：以表格形式展示直接变更 + 级联提醒；用户可回复 **编辑** 逐条调整再 **确认**。
4. **执行更新**：派发 `update-apply-agent` 严格按 PLAN 改文件，禁止越界改动 / 凭空发明字段。脚本只动 PLAN 指明的最小区域（docstring 头部表格、函数签名、`_cli()` 参数、相关常量/映射）；若 `change_summary` 过于宽泛（如"优化算法"），脚本会被跳过并提示用户跑 `/ontology-codegen` 重生成。
5. **自动校验**：调用 `/ontology-validate` 在增量模式下只跑改动的 object/logic 文档，依次完成规则检查和语义检查（脚本 .py 不进 ontology-validate 范围）。
6. **提交**：把 update 的首次落盘提交为 `update: apply reference changes`；如果改动已被 validate 的 fix commit 吃掉则跳过。

---

## 业务知识沉淀（auto-business-knowledge）

建模过程中业务专家随手提供的两类信息会被自动识别并沉淀到 `ontology/knowledge/`：

- **纠错**：触发词如「不对 / 你弄错了 / 应该是 / 其实 / 实际上」。
- **业务常识 / 规范**：触发词如「我们公司 / 我们这边 / 规则是 / 通常 / 必须 / 行业里 / 标准做法」。

识别到候选后会先弹一次确认；用户主动说「记一下：⋯」时跳过确认。条目分四类：`correction`（纠错）/ `convention`（规范）/ `terminology`（术语）/ `domain-rule`（业务常识）。

**所有 add 路径**（确认通路 + 「记一下」快捷通路）落盘前都会先 dispatch `knowledge-conflict-agent` 扫既有条目，识别重复（duplicate）/ 冲突（conflict）/ 相关（related）关系，让用户在 **覆盖 / 合并 / 仍新增 / 取消** 中决策；agent 返回空数组才直接 add。

此外，用户消息中出现「应该是 / 其实 / 我们这边 / 我们公司 / 规则是 / 必须 / 不对 / 你弄错了 / 实际上 / 行业里 / 标准做法」等信号词时，`knowledge_signal_probe.sh` hook 会自动给 Claude 注入沉淀提醒，避免长上下文里漏识别（见下文「工具自身的保障机制」）。

后续命令运行时会自动加载相关条目作为上下文（`global` 全量 + 当前 scope 命中前 10 条），把同一个问题反复纠正的成本降到零。

```
# 反查
/ontology-query 知识 退款
```

底层脚本：`python3 .claude/scripts/knowledge_helper.py {add,revise,remove,list,show,load,sync-index}`。

---

## 评测系统（eval/）

`eval/` 下是这套工具集自身的**自动化评测系统**——用 PTY 同时驱动三个 `claude` CLI 实例（建模 / 业务专家 / 裁判），跑完一个完整建模场景后产出 Judge 评估报告，用于持续发现工具集的功能性和体验性问题。完全基于真实 claude code CLI 驱动，走的是用户订阅配额而非 API 计费。

### 三个角色

| 角色 | 负责 | 默认模型 |
|---|---|---|
| **Builder** | 调用 `/ontology-*` 命令完成建模 | `claude-sonnet-4-6` |
| **Expert** | 扮演领域专家，按 persona 反馈、推进流程；`task_objectives` 全部达成时输出 `[MODELING_DONE]` 终止 run | `claude-opus-4-7` |
| **Judge** | run 结束后通读 builder/expert digest + 全部产出物，写 `report.md` 列出功能性/体验性 findings | `claude-opus-4-7` |

### 怎么跑

```bash
# 单场景
python3 -m eval.tooling.orchestrator run eval/scenarios/ecommerce-order-basic.yaml

# 批量跑一目录的场景，结束后自动聚合
python3 -m eval.tooling.orchestrator batch eval/scenarios --aggregate

# 把已有 runs 聚合成 summary
python3 -m eval.tooling.orchestrator aggregate workspace/eval-runs

# 把某个 run 的 Builder↔Expert 对话还原成可读 Markdown
python3 -m eval.tooling.render_conversation workspace/eval-runs/<run_id>
```

每个 run 落在 `workspace/eval-runs/<timestamp>_<scenario>/`（`timestamp` 使用 `Asia/Shanghai`），与用户的建模任务共用 `workspace/` 顶层；该目录被工具集仓 `.gitignore` 排除，每个 run 内的 `builder/cwd/` 是独立建模工作区（own .git）。

### Run 产出

```
workspace/eval-runs/<run_id>/
  scenario.yaml         ← 拷贝的场景定义
  outcome.json          ← outcome / turn_count / elapsed_minutes / stuck_reason
  report.md             ← Judge 评估报告（功能性 + 体验性 findings）
  conversation.html     ← Builder↔Expert 完整对话还原（默认 HTML；时间显示使用 Asia/Shanghai；可加跑 --format md 另出 Markdown）
  builder/
    session.jsonl       ← Builder 的 claude code session jsonl
    cwd/                ← 独立建模工作区（own .git，软链工具集 + 复制 resource/.obsidian）
      .git/             ← Builder 的 phase commits 落这里
      ontology/ research/ reports/   ← Builder 实际产出的建模物
  expert/session.jsonl  ← Expert 的 claude code session jsonl
  judge/                ← Judge 工作目录 + jsonl
  digest/               ← 蒸馏后的轻量摘要（喂给 Judge）
```

### 场景定义

`eval/scenarios/*.yaml`：

```yaml
name: ecommerce-order-basic
bootstrap:
  - command: "/ontology"
  - text: "我想给一个 B2C 电商平台的订单流程建模..."
expert:
  persona: |
    你是一家中型 B2C 电商平台的产品经理...
  knowledge:                                  # 专家心里"已知"的领域事实
    - 下单后库存预占 15 分钟...
  task_objectives:                            # Expert 的内置 checklist；全部达成才发 [MODELING_DONE]
    - 跑完 /ontology-research...
    - 跑完 /ontology-codegen...
  style:
    avoid_volunteering: true
    ask_clarifying: occasional
termination:
  max_turns: 50                                # 兜底：撞顶就 EXHAUSTED
  success_signal:
    type: regex_in_expert_text
    value: "\\[MODELING_DONE\\]"
  stuck_threshold:
    no_jsonl_activity_minutes: 3
    repeated_assistant_messages: 3
    no_phase_change_turns: 10
```

### 关键设计

- **回合边界靠 jsonl 的 `stop_reason='end_turn'` 标记**——不靠 ANSI scrape 或脆弱的"静默 N 秒"启发
- **悬空 tool_use 跟踪**——Builder 发起 Agent 子代理 dispatch 后父 jsonl 静默数分钟也不会被误判为 turn done
- **Expert 终止信号**：扫 Expert 文本 regex 匹配 `[MODELING_DONE]`，命中即 SUCCESS 退出
- **STUCK 多源检测 + 原因标记**：jsonl 静默 / 回复重复 / phase 长期不变 / Builder/Expert wait timeout / Expert `[GIVE_UP]`，对应 `outcome.json.stuck_reason` 字段
- **Run dir 顶层 symlink**：Builder/Expert 各自 cwd 下的 `ontology/`、`research/`、`reports/` 通过 symlink 抬到 run 目录顶层，Judge 单个 cwd 看全所有 artifacts

### 模块清单

```
eval/tooling/
  orchestrator.py        ← run/batch/aggregate CLI 入口；主 run 循环
  agent.py               ← Agent 封装：PTY + session tailer + turn detector
  pty_driver.py          ← PTY fork/读写 + bracketed-paste 多行输入
  session_tailer.py      ← 增量 tail claude code 的 session jsonl
  turn_detector.py       ← 基于 stop_reason + pending tool_use 判定回合结束
  outcome.py             ← Outcome 四档（success/exhausted/stuck/crashed）+ 检测器
  scenario.py            ← YAML 场景 schema 与加载
  paths.py               ← Run 目录布局 + symlink 初始化
  digest.py              ← 把长 jsonl 蒸馏成给 Judge 看的轻量摘要
  judge.py               ← Judge run 启动 + 双信号完成检测
  judge_prompt.md        ← Judge 提示词
  expert_template.md     ← Expert persona prompt 模板
  report_parser.py       ← 解析 Judge report.md 的 findings JSON 块
  aggregator.py          ← 跨多 run 聚合 summary.md + findings CSV
  render_conversation.py ← Expert jsonl → 可读对话（默认 HTML，--format md 出 Markdown）
```

---

## 可选参考资料

`.claude/reference/` 下放了几份**面向建模人员的可选指南**，工具集本身不强制使用，遇到对应场景时可查阅：

| 文件 | 何时查阅 |
|------|---------|
| `BFO_MODELING_GUIDE.md` | 建模初期判断对象/逻辑命名是否符合本体论惯例；overview 命令会基于此对当前对象/逻辑做关键词匹配提醒 |
| `LOGIC_PARADIGM_GUIDE.md` | 提取完逻辑、进入验证之前，判断业务属于 SOP 流程型 / 断言驱动型 / 混合型，以选合适的场景设计与覆盖率策略 |
| `ASSERTION_DRIVEN_TESTING_GUIDE.md` | 业务被判定为断言驱动型或混合型时，参考其中的「规则覆盖 / 约束边界 / 退化逻辑 / 安全审计链」四类测试方法补充 trace 场景 |
| `LOGIC_STYLE_GUIDE.md` | 编写 logic 的"逻辑描述"段时遵守：关键字小写、用符号代替中文比较词、嵌套 ≤ 3、禁用口语词；多维条件密集时改用决策表（§八） |
| `OBJECT_EXTRACTION_HEURISTICS.md` | 候选对象"拆/合"不确定时（"X 是实体还是属性?"、"X 和 Y 同类还是异类?"），用决策驱动 + 能力差异两问辅助判断 |

> 这些文档仍是可选参考，不是强制规则；部分命令会在合适节点软提示它们，帮助用户在校验、追踪和场景设计前选择合适策略。

---

## 工具自身的保障机制

`.claude/settings.json` 注册的 hook 在日常使用中静默生效，把易忘的规则约束机械化：

| 事件 | 脚本 | 作用 |
|------|------|------|
| `PreToolUse(Bash)` | `pre_commit_check.sh` | 拦截 `git commit`，对 `ontology/` 待提交文档跑规则检查；ERROR 级直接 block |
| `PreToolUse(Edit/Write/MultiEdit)` | `lock_file_guard.sh` | 禁止手工编辑 `.ontology-state/lock-*.json`（工具管理文件，残留锁请用 `state_helper.py force-release`） |
| `UserPromptSubmit` | `knowledge_signal_probe.sh` | 检测业务知识信号词，自动给 Claude 注入沉淀提醒 |
| `PostToolUse(Bash)` | `post_commit_hook.sh` | git commit 完成后打印中文版本号 + 下一步建议 |
| `SessionStart` | `session_start_hook.sh` | 新会话开始时打印当前 ontology 阶段、锁状态、最近报告 |

主流程之外的两个复审 agent 也归在这里（由命令显式 dispatch，不是 hook 触发）：

| Agent | 触发点 | 作用 |
|-------|--------|------|
| `script-candidate-agent` | `/ontology-codegen-scan` Step 3 | 扫全量 logic 判定哪些适合/部分适合/不适合脚本化，写候选报告并返回 JSON 供命令翻译为 codegen 参数 |
| `codegen-review-agent` | `/ontology-codegen` Step 4.5 | 复审生成的 `ontology/scripts/*.py` 是否符合模板/规范/业务知识；片段脚本只校验对应步骤范围 |
| `knowledge-conflict-agent` | 所有 `knowledge_helper.py add` 路径前置 | 检测候选条目与既有 knowledge 的重复/冲突/相关关系 |

---

## 版本管理

面向非技术用户的中文版本管理：

```
/version           查看最近 10 个版本
/version 回退      安全回退到上一版本
/version 对比 5 8  对比两个版本差异
/version 标记 初版  给当前版本打标记
```

每次阶段提交后自动显示："已保存版本 N: 描述。如需回退到上一版本，回复'回退'"

---

## 目录结构

工具集仓与建模产物分离：**工具集仓根**只放工具集源码本身，**建模产物**全部进 `workspace/<任务名>/`（每个任务自己的 .git，仓根的 `.gitignore` 屏蔽整个 `workspace/`）。

### 工具集仓根

```
ontology-modeling/                   # 工具集仓
├── README.md                        # 命令/使用说明（每个任务通过 symlink 引用）
├── CLAUDE.md                        # 项目规则（每个任务通过 symlink 引用）
├── init_task.sh                     # 任务工作区初始化脚本
├── .claude/
│   ├── commands/                    # 命令定义（15 个）
│   ├── agents/                      # Subagent 提示（18 个）
│   ├── templates/                   # 文档模板（8 个：object/logic/testcase/mock-data/script + 2 个 HTML 报告骨架 + task-gitignore）
│   ├── reference/                   # 可选参考资料（BFO 建模 / Logic 范式识别 / 断言驱动测试 / Logic 风格 / 对象抽取启发式）
│   ├── scripts/                     # Python 校验/查询/状态脚本 + hook 入口脚本（pre_commit / post_commit / session_start / lock_file_guard / knowledge_signal_probe）
│   ├── skills/deep-research/        # 调研方法论
│   ├── skills/extract-migrated-objects/ # 旧场景对象迁移/复用提取
│   ├── skills/mermaid-flowchart/    # 从业务逻辑文本或文件生成 Mermaid 流程图
│   ├── worktrees/                   # 给 sub-agent 用的临时 git worktree 缓存
│   ├── BUSINESS_KNOWLEDGE_SNIPPET.md # 业务知识识别 & 应用守则（agent 顶部自动注入参考）
│   ├── INTERACTION_CONVENTIONS.md   # 交互约定
│   ├── STEP0_TEMPLATE.md            # 所有命令的通用前置流程
│   ├── USER_VOCABULARY.md           # 用户词汇表
│   ├── VALIDATION_BOUNDARIES.md     # 规则 vs 语义边界（含权威严重性矩阵）
│   └── settings.json                # Hook 配置
├── resource/                        # 源材料起始模板（init_task.sh 复制到每个任务）
│   ├── diagrams/                    # 原始流程图模板位置
│   └── others/                      # 参考文档模板位置
├── .obsidian/                       # Obsidian vault 配置模板（init_task.sh 复制到每个任务）
├── eval/                            # 工具集自身的评测系统（PTY 多 agent + Judge 报告）
│   ├── tooling/                     # orchestrator / agent / detector / judge / aggregator 等
│   ├── scenarios/                   # 场景 YAML
│   └── tests/                       # 单元测试
├── .gitignore                       # 屏蔽 workspace/ 不让建模产物污染工具集仓
└── workspace/                       # ⚠️ gitignored；全部建模任务和 eval run 都在此
    ├── <任务名>/                    # 用户建模任务（见下）
    └── eval-runs/                   # 评测系统 run 输出
        └── <timestamp>_<scenario>/  # 单次 run（含 outcome.json / report.md / conversation.html / builder/ expert/ judge/ digest/）
```

### 任务工作区（`workspace/<任务名>/`）

```
workspace/电商订单建模/
├── .git/                            # 任务自己的 git，commit 计数从 1 开始
├── .claude → ../../.claude          # 软链：命令/agent/scripts
├── CLAUDE.md → ../../CLAUDE.md      # 软链：项目规则
├── README.md → ../../README.md      # 软链：使用说明
├── .gitignore                       # 仅排除 .ontology-state/ 与上述 3 个 symlink
├── resource/                        # 复制：用户在此放领域材料
├── .obsidian/                       # 复制：任务局部 Obsidian 配置
├── research/                        # 调研报告（/ontology-research 产出；日期使用 Asia/Shanghai）
├── reports/                         # 校验/追踪/codegen-scan 报告（文件名和正文时间使用 Asia/Shanghai）
├── .ontology-state/                 # 命令状态（progress.json / lock-*.json，不进 git）
└── ontology/                        # 建模产出（进任务 git）
    ├── object/                      # 业务对象
    ├── logic/                       # 业务逻辑
    ├── data-mock/                   # Mock 测试数据
    ├── scripts/                     # 生成代码（逻辑脚本；codegen 产出）
    ├── diagrams/                    # 流程图
    ├── diagrams-summary/            # 流程图摘要
    ├── mermaid/                     # Mermaid 流程图
    ├── excel/                       # 流程图配套 Excel 补充表（按 sheet 拆分）
    ├── testcase/                    # 追踪场景（每个逻辑一份，可复用）
    └── knowledge/                   # 业务知识沉淀（INDEX.md + 各类知识条目）
```

---

## Git 提交格式

| 阶段 | 格式 | 备注 |
|------|------|------|
| 调研完成 | `research: <topic>` | |
| 追加调研 | `research: append <direction>` | |
| 流程图创建 | `flowchart: <topic> initial diagrams` | |
| 流程图打磨 | `flowchart: polish <topic>` | |
| 对象提取 | `extract: <topic> objects` | |
| 逻辑提取 | `extract: <topic> logic` | |
| 规则修复 | `fix: address rule check issues` |
| 语义修复 | `fix: address semantic check issues` |
| 追踪修复 | `fix: address trace issues` |
| 追踪场景保存 | `trace: <logic_name> testcases` |
| 交互走查场景保存 | `trace: <logic_name> interactive testcases` |
| 测试数据 | `mock: <topic> test data` |
| 交互走查数据保存 | `mock: <logic_name> walkthrough data` |
| 代码生成 | `codegen: <topic>` |
| 业务知识沉淀 | `knowledge: <topic>` |
| 业务知识修订 | `knowledge: revise <slug>` |
| 业务知识删除 | `knowledge: remove <slug>` |
