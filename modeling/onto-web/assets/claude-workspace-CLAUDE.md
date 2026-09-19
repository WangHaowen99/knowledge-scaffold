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

1. **Obsidian links** — `[[文件名]]` must exactly match the target filename with no `.md` extension and no path prefix. Example: `[[订单]]` links to `ontology/object/订单.md`. Links resolve across `object/` and `logic/` directories.

2. **Sub-logic references** — Any sub-logic called in a 逻辑描述 section (format: `调用[[子流程名称]]逻辑文档`) must also appear as `[[子流程名称]]` in the 与logic的关联 section of the same document (in the "使用logic" line).

3. **File locations** — Ontology documents may only be written to:
   - `ontology/object/` — business object docs
   - `ontology/logic/` — business logic/process docs
   - `ontology/data-mock/` — mock test data
   - `ontology/scripts/` — codegen 产出的逻辑脚本（headers 按 `.claude/templates/script-template.py`，CLI 入参用 argparse）
   - `ontology/diagrams/` — flowchart .drawio files
   - `ontology/diagrams-summary/` — flowchart summaries
   - `ontology/excel/` — Excel supplements split from `resource/excel/*.xlsx` by sheet
   - `ontology/testcase/` — reusable trace scenarios (one `.md` per logic)
   - `ontology/knowledge/` — business knowledge entries (corrections, conventions, terminology, domain rules)
   Never create ontology files elsewhere.

## Validation Rules

4. **No external LLM APIs** — Semantic validation is performed by Claude itself. Never call external LLM APIs (Gemini, OpenAI, etc.) for validation tasks in this project.

## Git Rules

5. **Phase-end commits only** — Do not commit ontology changes mid-phase. Commit only at the end of each completed phase.

6. **Pre-commit rule check** — `.claude/scripts/pre_commit_check.sh` 会对 `ontology/` 下的待提交文档跑一次规则检查；发现 ERROR 级问题时会阻止提交。遇到阻止时先跑 `/ontology-validate` 处理错误，不要绕过 hook。

## Tool State Directory

7. **`.ontology-state/` is tool-managed** — 该目录下的锁文件（`lock-*.json`）、进度文件（`progress.json`）由命令脚本（`state_helper.py` 等）维护，用于跨命令协调。不要手工编辑；遇到异常残留锁时用 `python3 .claude/scripts/state_helper.py force-release <cmd>` 清理。

## Protected Source Material

8. **`resource/` is user-managed and read-only for models** — 模型可以读取 `resource/`，也可以基于 `resource/` 刷新或生成 `ontology/` 下的派生产物；但任何时候都不得创建、修改、移动或删除任意 `resource/` 目录下的文件。若源材料需要调整，必须提示用户手工修改 `resource/`。

8.1 **资料用途标签（`resource/materials.json`）** — 用户可为每份资料指定用途与读取模式（对齐 modeling 的资料用途体系）。读取任何资料前先查该清单（不存在或无条目视为 `auto`）：

   | purpose | 语义 | 建模约束 |
   |---|---|---|
   | `auto` | 由助手判断 | 按文件名/类型/内容自行判断用途 |
   | `goal` | 建模目标依据 | 目标确认（Step 0 / 调研范围）必须优先引用该资料 |
   | `schema` | 数据 Schema | 该表格是对象与属性的**事实来源**：对象属性清单的英文名称/类型必须来自真实表头列，逐 Sheet 覆盖、不得虚构或合并；类型与主键只能标注为推断，不得据样例行编造业务含义 |
   | `term` | 术语表 | 对象/操作/规则/指标的命名与别名必须与术语表对齐；冲突时以术语表为准并在文档标注 |
   | `evidence` | 通用业务依据 | 作为佐证材料按需引用，引用时注明出处文件 |

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
