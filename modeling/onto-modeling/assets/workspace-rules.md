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

## Formatting Rules

1. **Wiki 链接** — `[[文件名]]` must exactly match the target filename with no `.md` extension and no path prefix. Example: `[[订单]]` links to `ontology/object/订单.md`. Links resolve across `object/`, `operation/`, `rule/` and `metric/` directories (legacy `logic/` remains readable).

2. **Sub-logic references** — Any sub-logic called in a 逻辑描述 section (format: `调用[[子流程名称]]逻辑文档`) must also appear as `[[子流程名称]]` in the 与logic的关联 section of the same document (in the "使用logic" line).

3. **File locations** — Ontology documents may only be written to:
   - `ontology/object/` — business object docs
   - `ontology/operation/` — business operation/process docs（`ontology/logic/` 是旧目录名，只读兼容）
   - `ontology/rule/` — 规则实体文档：判定、约束、门禁、阈值拦截
   - `ontology/metric/` — 指标实体文档：评分、比率、汇总等可度量口径
   - `ontology/data-mock/` — mock test data
   - `ontology/scripts/` — codegen 产出的逻辑脚本（headers 按 `.claude/templates/script-template.py`，CLI 入参用 argparse）；新增或修改的脚本必须用 `python3 .claude/scripts/script_verify.py run` 跑通（优先在 jiuwenbox 沙箱，不可用时在容器内），没有当前版本的通过记录不能提交
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

## Validation Rules

4. **No external LLM APIs** — Semantic validation is performed by Claude itself. Never call external LLM APIs (Gemini, OpenAI, etc.) for validation tasks in this project.

4.1 **"已保存"不等于"已验收"** — 调研报告（`research/*.md`）写进工作区后必须通过
   `python3 .claude/scripts/research_checker.py check` 的确定性检查，才算验收通过。
   脚本对账计数、ID 唯一与溯源（每个规范 ID 必须逐字出现在它声明的来源文件或 `resource/`
   的文本资料里，本轮新造的 ID 走「ID变更对照」声明）、引用字段是否真实、来源位置能否定位、
   「业务项清单」的分类列与四段候选是否逐项一致（提取按这一列分流，见 3.1），
   结果写入 `.ontology-state/research-review.json`；阶段状态（CLI 前置、项目进度、左栏展示）
   只认这份回执。报告或源资料改动后回执自动失效，需重新核验。
   验收管的是进入对象提取之前：对象已经提取的项目，回执缺失或失效只在界面上留一个
   待核验的提醒，不再挡后续阶段，也不要因此建议用户重做调研。
   任何时候都不要用自己的总结代替这份检查结论，也不要在报告里写"检查无问题"；
   需要人工判断的条目列进「缺口与待决」，由用户确认后勾选。

## Git Rules

5. **Phase-end commits only** — Do not commit ontology changes mid-phase. Commit only at the end of each completed phase.

6. **Pre-commit rule check** — `.claude/scripts/pre_commit_check.sh` 会对 `ontology/` 下的待提交文档跑一次规则检查；发现 ERROR 级问题时会阻止提交。遇到阻止时先跑 `/ontology-validate` 处理错误，不要绕过 hook。暂存 `research/` 下的报告时同一 hook 会跑 `research_checker.py`，未通过内容验收同样阻止提交。

## Tool State Directory

7. **`.ontology-state/` is tool-managed** — 该目录下的锁文件（`lock-*.json`）、进度文件（`progress.json`）、调研验收回执（`research-review.json`）、只读来源快照（`research-inventory.json`）与已验收 ID 基线（`research-baseline.json`）由命令脚本（`state_helper.py`、`research_checker.py` 等）维护，用于跨命令协调。不要手工编辑；遇到异常残留锁时用 `python3 .claude/scripts/state_helper.py force-release <cmd>` 清理。手工改写回执等于伪造验收结论。

## Protected Source Material

8. **`resource/` is user-managed and read-only for models** — 模型可以读取 `resource/`，也可以基于 `resource/` 刷新或生成 `ontology/` 下的派生产物；但任何时候都不得创建、修改、移动或删除任意 `resource/` 目录下的文件。若源材料需要调整，必须提示用户手工修改 `resource/`。

8.04 **子代理派遣必须同步等待完成** — 用 Task/Agent 工具派遣子代理时必须在本轮内
   同步等待其结果后再继续（工具若有 `run_in_background` 参数，必须显式传 `false`）。
   **严禁后台派遣后结束回合"等通知"**：平台会回收空闲会话进程，后台子代理会随进程
   丢失且不会有任何通知送达，任务将静默中断。材料再大也走同步派遣（本轮等待）。

8.05 **临时/生成用脚本一律写到 `tmp/`** — 需要写一次性辅助脚本（批量生成、转换、检查等）时，
   写到工作区 `tmp/` 目录（已 gitignore），用完删除；正式的代码产物走 codegen 流程进
   `ontology/scripts/`。`.claude/` 是只读工具箱（资产源，受守卫保护），任何时候不要往里写文件。

8.1 **资料用途标签（`resource/materials.json`）** — 用户可为每份资料指定用途与读取模式（对齐 modeling 的资料用途体系）。读取任何资料前先查该清单（不存在或无条目视为 `auto`）：

   | purpose | 语义 | 建模约束 |
   |---|---|---|
   | `auto` | 由助手判断 | 按文件名/类型/内容自行判断用途 |
   | `goal` | 建模目标依据 | 目标确认（Step 0 / 调研范围）必须优先引用该资料 |
   | `schema` | 数据 Schema | 该表格是对象与属性的**事实来源**：对象属性清单的英文名称/类型必须来自真实表头列，逐 Sheet 覆盖、不得虚构或合并；类型与主键只能标注为推断，不得据样例行编造业务含义 |
   | `term` | 术语表 | 对象/操作/规则/指标的命名与别名必须与术语表对齐；冲突时以术语表为准并在文档标注 |
   | `evidence` | 通用业务依据 | 作为佐证材料按需引用，引用时注明出处文件 |
   - **评测数据**（评测库导入用的业务数据表）不走资料区打标，由用户在「数据评测」的数据源里上传，存于 `.ontology-state/eval-data/<文件ID>/`。它**不是业务语义来源**——不得据样例行编造业务含义、枚举或规则；但它的 **Sheet 名与表头是表名、列名的依据**：没有 `schema` 资料时，对象「# 数据来源」写它的 Sheet 名，从库里读取的属性英文名称逐字取自该 Sheet 的表头（`python3 .claude/scripts/check_eval_data.py --headers` 列出全部表头）。文档形式的数据字典与真实表头冲突时以表头为准，并在调研报告或抽取结果里列出差异。其结构应与 `schema` 资料对账一致（`python3 .claude/scripts/check_eval_data.py` 默认检查这些上传文件）。旧项目 `materials.json` 里残留的 `eval_data` 标签同此处理。

   `reading_mode`：`full` = 必须全文精读后再使用；`on_demand` = 按需检索片段即可。该文件由平台维护，模型同样**不得修改**。

## Business Knowledge

9. **业务知识识别守则** —
   建模过程中专家若说出以下信号词，须考虑沉淀为业务知识：
   `应该是 / 其实 / 我们这边 / 我们公司 / 规则是 / 通常 / 必须 / 不对 / 你弄错了 / 实际上 / 行业里 / 标准做法`

   发现候选时按 `.claude/INTERACTION_CONVENTIONS.md` 的「业务知识确认对话」模板询问用户是否记入。
   用户主动说「记一下：…」「记下这条规则」时直接保存，不再二次确认。
   落盘脚本：`python3 .claude/scripts/knowledge_helper.py add ...`。
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

## Version Management

10. **Version display** — After every commit, display a user-friendly version message in Chinese:
   ```
   已保存版本 N: {commit message description}
   如需回退到上一版本，回复"回退"
   ```
   Where N is the total commit count in the repository.
