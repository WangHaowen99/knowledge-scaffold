# Ontology Project Rules

These rules apply to all work in this repository.

## Required reading（必读约定）

进入任何 `/ontology-*` 命令前，Claude 都应将下列约定文件视作活动 context：

- `.claude/USER_VOCABULARY.md` — 用户可见文案词汇表（替换技术术语 → 业务用语）
- `.claude/INTERACTION_CONVENTIONS.md` — 命令/agent 面向用户的交互约定（确认词、选择列表、错误恢复话术等）
- `.claude/VALIDATION_BOUNDARIES.md` — 规则检查 vs 语义检查归口，含「权威严重性矩阵」
- `.claude/BUSINESS_KNOWLEDGE_SNIPPET.md` — 业务知识识别守则与应用规则
- `.claude/STEP0_TEMPLATE.md` — 所有命令的通用前置流程

每个 `.claude/commands/*.md` 与 `.claude/agents/*.md` 都假设上述四份约定在 context 中；若你正在新增/修改 prompt 而需要明确引用条目，**优先 cite 文件路径而不是把约定原文复制进 prompt**，以避免漂移。

## Optional Capabilities（可选能力）

基础建模能力之外的命令、子代理、技能由项目启用的能力包提供（项目设置 → 能力与插件）：

| 能力 | 命令 / 技能 |
|---|---|
| 场景模拟 | `/ontology-trace`、`/ontology-trace-single`、`/ontology-trace-batch`、`/ontology-trace-interactive`、`/ontology-mock` |
| 代码生成与求解 | `/ontology-codegen-scan`、`/ontology-codegen`、`/ontology-solver-evaluator` |
| 图示 | `/ontology-flowchart`、`/ontology-validate-diagrams`、mermaid-flowchart 技能 |
| 知识沉淀 | `/knowledge-record`、`/knowledge-review`、`/knowledge-nominate`，`.claude/KNOWLEDGE_SEDIMENTATION.md` |
| 反馈演进 | `/ontology-evolution` |
| 联网调研 | deep-research 技能 |
| 迁移复用 | extract-migrated-objects 技能 |

- 命令文档、下一步建议、菜单里提到这些命令时，先看 `.claude/commands/<命令名>.md`（技能看 `.claude/skills/<名>/SKILL.md`）在不在：在才提供这个选项、才去读它；不在就不要列出，用户问起时说明本项目没有启用对应能力，可以在项目设置里启用。
- 下一步建议统一用 `python3 .claude/scripts/state_helper.py next-step` 取，它只列出本项目已启用的命令。

## Industry Packs（行业包）

项目启用的行业包在 `.claude/industry/<包名>/`（只读；`pack.json` 里有显示名与版本）。目录不存在就跳过本节：

- 资料分析：对照 `standards.json` 的规范类型找资料里对应的内容，缺的记进待确认问题；用 `concepts.json` 的概念与别名辅助识别资料用途和术语。
- 抽取对象、操作、规则与指标：先看 `reference/` 里 `decision_point` 对应本步骤的写法提示；`library/` 是参考对象与操作，**只作候选提示**，按项目资料写、经专家确认，不直接复制。
- 行业知识由 `knowledge_helper.py load` 自动分层加载（项目里同名条目优先），不用另读 `knowledge/`。
- 产物用到了行业知识或参考库时，在「数据来源」或提交说明里注明来源，如 `industry-retail@1.0.0 / 安全库存测算`。
- `ontology_checker` 会按行业包的声明式规则给警告（分类「行业规则」）；项目口径确实不同就保留，并沉淀一条项目知识说明原因。

## Subagent Dispatch（子任务派发）

命令里写「Read `.claude/agents/X.md`. Dispatch it …」时，agent 文件就是子任务的完整指令：

- 派发提示第一行固定写：`请先完整阅读 .claude/agents/X.md，严格按其中的规则与输出格式执行。`，之后再接命令要求的输入（如 TARGET_FILES、CHANGE_DIFF）。
- 不要把 agent 文件改写成摘要或几句话代替，也不要在派发提示里另加 agent 文件没有的检查范围或任务。
- 子任务不得再派发子任务；需要别的检查时，结束并把结果交回主对话。

## Formatting Rules

1. **Wiki 链接** — `[[文件名]]` must exactly match the target filename with no `.md` extension and no path prefix. Example: `[[订单]]` links to `ontology/object/订单.md`. Links resolve across `object/`, `operation/`, `rule/` and `metric/` directories (legacy `logic/` remains readable).

2. **Sub-logic references** — Any sub-logic called in a 逻辑描述 section (format: `调用[[子流程名称]]逻辑文档`) must also appear as `[[子流程名称]]` in the 与logic的关联 section of the same document (in the "使用logic" line).

3. **File locations** — Ontology documents may only be written to:
   - `ontology/object/` — business object docs
   - `ontology/operation/` — business operation/process docs（`ontology/logic/` 是旧目录名，只读兼容）
   - `ontology/rule/` — 规则实体文档：判定、约束、门禁、阈值拦截
   - `ontology/metric/` — 指标实体文档：评分、比率、汇总等可度量口径
   - `ontology/data-mock/` — mock test data
   - `ontology/scripts/` — codegen 产出的逻辑脚本（headers 按 `.claude/templates/script-template.py`，CLI 入参用 argparse）；按完整流程一个 operation 一个脚本，没标「可单独调用 > 是」的子操作并入主操作脚本；新增或修改的脚本必须用 `python3 .claude/scripts/script_verify.py run` 跑通（优先在 jiuwenbox 沙箱，不可用时在容器内），没有当前版本的通过记录不能提交；脚本只做纯计算，禁用写法与「关联operation」要求见 `.claude/commands/ontology-codegen.md`「脚本安全」与头部注释说明。每个对外脚本旁有同名 `.md` 描述符（consumer 调用契约），由 `python3 .claude/scripts/script_descriptor.py render ontology` 按脚本生成，不要手改
   - `ontology/diagrams/` — flowchart .drawio files
   - `ontology/diagrams-summary/` — flowchart summaries
   - `ontology/excel/` — Excel supplements split from `resource/excel/*.xlsx` by sheet
   - `ontology/testcase/` — reusable trace scenarios (one `.md` per logic)
   - `ontology/knowledge/` — business knowledge entries (corrections, conventions, terminology, domain rules)
   Never create ontology files elsewhere.

3.1 **实体类型由目录决定** — 知识包按目录分 objects / operations / rules / metrics，
   studio 图谱同理。规则写进 `ontology/operation/` 不是"只是乱放"：发布出去就变成了
   操作。提取阶段按资料分析报告「业务项清单」的分类列分流，`ontology_checker.py` 的
   「目录归属」检查会把放错的文档判为错误并阻止提交。要调整归属就移动文件并按
   `.claude/templates/{rule,metric}-template.md` 重写，不要改检查。
   同一检查给出「疑似」警告时（报告里有「XX规则」，operation 目录下有「XX」），先核对
   它是那条规则还是另一个同名操作，再决定是否迁移——警告不拦提交。
   生成结果与报告是否对得上，用 `python3 .claude/scripts/report_reconcile.py check` 查：
   它按名称与分类列出报告里有、没生成的，生成在错误目录的，以及报告里没有的（并标明
   是否来自流程图）。不要凭印象说"已与报告一致"。

3.2 **规则的「原始资料来源」只指向用户上传的原始资料** — 规则文档的「原始资料来源」节
   逐条写 `resource/` 下的原始资料路径与锚点，取自资料分析报告的「来源位置」和「详细信息」，
   锚点逐字沿用
   （如 `resource/others/规则.docx#p:2`、`resource/excel/表.xlsx#sheet:规则!B3`），每条用
   行内代码包住，工作台据此打开原文并定位。不写 `research/`、`reports/` 等过程文件；
   没有对应上传资料时写「无」并在括号里照抄来源位置的文字，如 `无（领域知识整理）`。建模依据（报告路径、`[[链接]]`）仍写在
   「来源」节，两节互不替代。指标、对象、操作不加这一节。

## Validation Rules

4. **No external LLM APIs** — Semantic validation is performed by Claude itself. Never call external LLM APIs (Gemini, OpenAI, etc.) for validation tasks in this project.

4.1 **"已保存"不等于"已验收"** — 资料分析报告（`research/*.md`）写进工作区后必须通过
   `python3 .claude/scripts/research_checker.py check` 的确定性检查，才算验收通过。
   脚本对账计数、ID 唯一与溯源（每个规范 ID 必须逐字出现在它声明的来源文件或 `resource/`
   的文本资料里，本轮新造的 ID 走「ID变更对照」声明）、引用字段是否真实、来源位置能否定位、
   「业务项清单」的分类列与四段候选是否逐项一致（提取按这一列分流，见 3.1），
   结果写入 `.ontology-state/research-review.json`；阶段状态（CLI 前置、项目进度、左栏展示）
   只认这份回执。报告或源资料改动后回执自动失效，需重新核验。
   验收管的是进入对象提取之前：对象已经提取的项目，回执缺失或失效只在界面上留一个
   待核验的提醒，不再挡后续阶段，也不要因此建议用户重做资料分析。
   任何时候都不要用自己的总结代替这份检查结论，也不要在报告里写"检查无问题"；
   需要人工判断的条目列进「缺口与待决」，由用户确认后勾选。

## Git Rules

5. **Phase-end commits only** — Do not commit ontology changes mid-phase. Commit only at the end of each completed phase.

6. **Pre-commit rule check** — `.claude/scripts/pre_commit_check.sh` 会对 `ontology/` 下的待提交文档跑一次规则检查；发现 ERROR 级问题时会阻止提交。遇到阻止时先跑 `/ontology-validate` 处理错误，不要绕过 hook。暂存 `research/` 下的报告时同一 hook 会跑 `research_checker.py`，未通过内容验收同样阻止提交。

## Tool State Directory

7. **`.ontology-state/` is tool-managed** — 该目录下的锁文件（`lock-*.json`）、进度文件（`progress.json`）、资料分析验收回执（`research-review.json`）、只读来源快照（`research-inventory.json`）与已验收 ID 基线（`research-baseline.json`）由命令脚本（`state_helper.py`、`research_checker.py` 等）维护，用于跨命令协调。不要手工编辑；遇到异常残留锁时用 `python3 .claude/scripts/state_helper.py force-release <cmd>` 清理。手工改写回执等于伪造验收结论。

## Protected Source Material

8. **`resource/` is user-managed and read-only for models** — 模型可以读取 `resource/`，也可以基于 `resource/` 刷新或生成 `ontology/` 下的派生产物；但任何时候都不得创建、修改、移动或删除任意 `resource/` 目录下的文件。若源材料需要调整，必须提示用户手工修改 `resource/`。

8.04 **子代理派遣必须同步等待完成** — 用 Task/Agent 工具派遣子代理时必须在本轮内
   同步等待其结果后再继续（工具若有 `run_in_background` 参数，必须显式传 `false`）。
   **严禁后台派遣后结束回合"等通知"**：平台会回收空闲会话进程，后台子代理会随进程
   丢失且不会有任何通知送达，任务将静默中断。材料再大也走同步派遣（本轮等待）。

8.05 **临时/生成用脚本一律写到 `tmp/`** — 需要写一次性辅助脚本（批量生成、转换、检查等）时，
   写到工作区 `tmp/` 目录（已 gitignore），用完删除；正式的代码产物走 codegen 流程进
   `ontology/scripts/`。`.claude/` 是只读工具箱（资产源，受守卫保护），任何时候不要往里写文件。

8.06 **`ontology/solver/` 是平台受管的求解契约目录** — 每个 `<solver_key>/` 子目录由
   `binding.yaml` 与 `model.yaml`、`input.schema.json`、`result.schema.json`、
   `fact-snapshot.schema.json`、`presentation.yaml` 成组组成，只能由用户在「求解模型」面板保存或经
   知识包导入写入；守卫会拒绝模型对该目录的任何写入。模型只读取和解释其中内容，需要调整时提示用户
   在求解模型面板中修改。`binding.yaml` 的 `operation` 指向已绑定的 Operation：不要删除或改名这些
   Operation。契约所用的评估器（`ontology/scripts/`）分两类：旧式评估器自带计算代码，须继续导出
   `SOLVER_CONTEXT` 等契约符号，不要改写其接口；声明式评估器只含字面量声明，由
   `/ontology-solver-evaluator` 生成和修改，改完必须运行该命令的自检；本对象缺的取数列用一跳 lookup
   从关联对象取得，不要为凑列给对象文档补造属性。修改绑定 Operation 的正文、评估器，或声明式评估器
   `TABLE_SOURCES` 引用的对象文档（含 lookup 关联对象），都会让已有的求解验证过期，发布前需用户
   重新发起验证。

8.1 **资料用途标签（`resource/materials.json`）** — 用户上传时不再选择用途与读取模式：平台按表格结构识别数据 Schema 与术语表并预填用途，其余资料由你按内容判断；用户可在左侧资料区查看并修改用途。读取任何资料前先查该清单（不存在或无条目视为 `auto`）：

   | purpose | 语义 | 建模约束 |
   |---|---|---|
   | `auto` | 由助手判断 | 按文件名/类型/内容自行判断用途；说明建模场景、目标或范围的文档（如场景说明、需求说明）按 `goal` 对待 |
   | `goal` | 建模目标依据 | 目标确认（Step 0 / 资料分析范围）必须优先引用该资料 |
   | `schema` | 数据 Schema | Sheet 范围内的 Sheet 是对象与属性的**事实来源**：对象属性清单的英文名称/类型必须来自真实表头列，逐 Sheet 覆盖、不得虚构或合并；范围外的 Sheet 只作参考；类型与主键只能标注为推断，不得据样例行编造业务含义 |
   | `term` | 术语表 | 对象/操作/规则/指标的命名与别名必须与术语表对齐；冲突时以术语表为准并在文档标注 |
   | `evidence` | 通用业务依据 | 作为佐证材料按需引用，引用时注明出处文件 |
   - **数据表**只认 `schema` 资料 Sheet 范围内的 Sheet（一个 Sheet = 一张表，首行 = 列名；范围由用户在资料页确认，`--headers` 已按范围过滤）：对象「# 数据来源」写它的 Sheet 名，从库里读取的属性英文名称逐字取自该 Sheet 的表头（`python3 .claude/scripts/table_profile.py --headers` 把全部表头写到 `.ontology-state/table-headers.json`，stdout 给摘要；资料区的 Excel 只读它的文本版，还没有文本版的列在 `missing_text`，按未读处理，不要去读原件）。样例行**不是业务语义来源**——不得据样例行编造业务含义、枚举或规则。文档形式的数据字典与真实表头冲突时以表头为准，并在资料分析报告或抽取结果里列出差异。旧项目 `materials.json` 里残留的已停用用途标签读取时按 `auto` 处理；原先在数据源里上传、已被对象引用的数据表，平台升级时已迁进 `resource/excel/` 并标为 `schema`。

   `reading_mode`：由平台按资料类型决定，用户不再选择。条目显式写了 `full` / `on_demand`（存量项目）时照旧执行；没有写时：文档类资料（docx、doc、pdf、md、txt）全文读取后再使用，篇幅过长（如超过约 3 万字或 50 页）时可按需检索，但要在报告「信息来源」里注明哪些部分未通读；表格、图片、流程图按需检索（数据 Schema 与术语表的表头、术语由脚本读取）。该文件由平台维护，模型同样**不得修改**。

   条目带 `auto_detected: true` 表示平台上传时按表格结构识别为数据表或术语表、预填了 `schema` / `term`，与人工标注同等对待。`--headers` 报 `text_missing` 时资料已经标为 schema、只是还没转成文本，**不要再请用户标**；平台在建模命令开始前会等转换完成，仍没有文本版的会在本轮上下文里点名，按未读处理。只有确实没有 `schema` 资料（`no_schema`）、但 `resource/` 里有像数据库表的表格（首行是英文列名）时，才请用户在资料页点该表格的用途、选「数据 Schema」；**不要让用户编辑 `materials.json`**，改完后重新读取清单再继续。

## Business Knowledge

9. **业务知识** —
   已沉淀的业务知识（项目知识与已启用行业包的知识）由 `python3 .claude/scripts/knowledge_helper.py load` 加载，各命令照常应用。
   把对话里的纠错与惯例沉淀成新条目属于「知识沉淀」能力：`.claude/KNOWLEDGE_SEDIMENTATION.md` 存在时，
   专家说出 `应该是 / 其实 / 我们这边 / 我们公司 / 规则是 / 通常 / 必须 / 不对 / 你弄错了 / 实际上 / 行业里 / 标准做法` 这类信号词，
   按它的守则询问是否记入（用户主动说「记一下：…」时直接保存）；不存在时只应用、不新增。
   所有规则细节见 `.claude/BUSINESS_KNOWLEDGE_SNIPPET.md`。

## Git Commit Conventions

| Phase | Message format | Notes |
|-------|---------------|-------|
| Research complete | `research: <topic>` | |
| Research appended | `research: append <direction>` | |
| Flowchart created | `flowchart: <topic> initial diagrams` | |
| Flowchart polished | `flowchart: polish <topic>` | |
| Objects extracted | `extract: <topic> objects` | |
| Logic extracted | `extract: <topic> logic` | |
| Rule check fixes | `fix: address rule check issues` | |
| Semantic check fixes | `fix: address semantic check issues` | |
| Trace fixes | `fix: address trace issues` | |
| Trace testcases saved | `trace: <logic_name> testcases` | |
| Mock data generated | `mock: <topic> test data` | |
| Code generated | `codegen: <topic>` | |
| Knowledge added | `knowledge: <topic>` | |
| Knowledge revised | `knowledge: revise <slug>` | |
| Knowledge removed | `knowledge: remove <slug>` | |
| Update applied | `update: apply reference changes` | /ontology-update 的首次落盘；若改动已被 fix commit 吃掉则跳过 |

## 对用户说话

10. **只说业务上的进展和结论** — 对话里写给用户的话按 `.claude/USER_VOCABULARY.md`：
   - 不说命令文档的步骤编号与步骤名（「Step 2.5：固化只读来源快照」「执行 Step 0 前置检查」）、脚本与工具名、
     文件路径与变量（`REPORT_PATHS = …`）、命令锁（「锁检查通过」「释放 validate 锁」）、退出码。这些只出现在
     工具调用里；工具调用之间不写过程旁白，要说就一句业务话（「读完了 4 份资料，开始整理业务对象」）。
   - 工具调用的说明（Bash、Task 的 description）界面上会作为过程步骤给用户看：用中文业务话写（「读取补货资料」
     「整理业务对象」），加锁、释放锁、检查前置这类内部动作写「准备」「收尾」；正文里也不提数据字段名、环境变量、
     命令参数（如 `read: null`、`WEB_RESEARCH_MODE`）。
   - 提交只是把这一轮的修改记下来，这一轮结束平台自动存成一个版本：不说「已保存版本 N」，不让用户回复「回退」「撤回」。
     这一轮改了什么、撤回这一轮，界面上有卡片和按钮；回退旧版本在版本页做。
   - 命令做完先释放锁，再写两三句小结：做了什么、还有什么要用户定。不列「下一步建议 / 回复 X →」菜单，
     下一步由界面的建议条给出；要用户定的事用确认卡问（AskUserQuestion / ask_user）。
11. **只问业务问题** — 只问含义、范围、冲突、取舍这类业务上的事。编号、文件名、目录、格式、读哪些资料、
   用什么工具、要不要联网、接下来做什么，一律自己定，不问用户（规则见 `.claude/INTERACTION_CONVENTIONS.md` 核心原则 5）。
   要解决的问题以平台注入的「建模目标」为准，不改写；资料和目标对不上时，在建模蓝图里提目标问题。
