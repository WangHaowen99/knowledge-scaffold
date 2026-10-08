Extract business operation from available source material and confirmed object documents. Optional mode/input: $ARGUMENTS

This command is the operation-only half of extraction. It requires existing `ontology/object/*.md`, reads flowchart summaries, same-name Excel supplements, and/or research reports, writes `ontology/operation/*.md`, checks coverage, then commits the operation phase.

## Step 0: 前置检查

遵循 `.claude/STEP0_TEMPLATE.md`，COMMAND_NAME = `extract-operation`。简述：

1. `python3 .claude/scripts/state_helper.py check-lock extract-operation`
   - `locked:{ts}:{phase}` → 按 **错误恢复类** 提示；phase 可能是 `source` / `operation`，**续执**时跳到对应阶段。
2. `python3 .claude/scripts/state_helper.py check-prereq extract-operation`
   - `missing:对象已提取` → 按 **跳步守卫** 提示："当前没有已确认对象，逻辑中的对象引用无从对齐。" 选项 **先做 对象提取**（/ontology-extract-object）。
3. `python3 .claude/scripts/state_helper.py acquire extract-operation source`
4. 加载已沉淀业务知识。SCOPE_HINTS 取已存在的对象/逻辑名：
   ```bash
   SCOPE_ARGS=(--scope global)
   for f in ontology/object/*.md ontology/operation/*.md; do
       [ -f "$f" ] || continue
       stem=$(basename "$f" .md)
       case "$f" in
           */object/*) SCOPE_ARGS+=(--scope "object:$stem") ;;
           */operation/*)   SCOPE_ARGS+=(--scope "operation:$stem") ;;
       esac
   done
   KNOWLEDGE_BLOCK=$(python3 .claude/scripts/knowledge_helper.py load "${SCOPE_ARGS[@]}")
   ```
   若 `KNOWLEDGE_BLOCK` 等于 `（暂无相关业务知识）`，置为空字符串以跳过注入。

进入逻辑提取阶段时再调用：
```bash
python3 .claude/scripts/state_helper.py acquire extract-operation operation
```

命令结束前 `release extract-operation`。

## 业务知识识别（贯穿整个命令）

整个命令期间始终遵循 `.claude/BUSINESS_KNOWLEDGE_SNIPPET.md`。
agent prompt 需 transclude 该文件全文。

## Step 1: Load source material

检查两个可能的提取来源：
- `ontology/diagrams-summary/` 下的 `.md` 文件（流程图摘要）→ `SUMMARIES`
- `ontology/excel/` 下与流程图摘要同名的 `.xlsx` 文件（Excel 补充表）→ 后续绑定到对应流程图
- `research/` 下的 `.md` 文件（调研报告）→ `REPORTS`

根据存在情况分四种模式：

**模式 A — 两者都为空：**
Tell user:
"未找到流程图摘要，也未找到调研报告。逻辑提取需要至少一个来源，请先：
- 回复 **调研**（/ontology-research）→ 做领域调研
- 回复 **流程图**（/ontology-flowchart）→ 直接画流程图
"
任一选择都会离开本命令，因此在等待用户回复前先释放锁：
```bash
python3 .claude/scripts/state_helper.py release extract-operation
```
然后 stop。

**模式 B — 仅有流程图摘要（`SUMMARIES` 非空，`REPORTS` 空）：**
Set `SUMMARY_PATHS = SUMMARIES`, `REPORT_PATHS = empty`。Proceed to Step 2。

**模式 C — 仅有调研报告（`SUMMARIES` 空，`REPORTS` 非空）：**
Tell user:
"未找到流程图摘要，将仅基于调研报告提取逻辑（`research/{文件名列表}`）。流程图是可选步骤，概念明确时可跳过。
- 回复 **继续** → 仅基于调研报告提取逻辑
- 回复 **先做流程图** → 转入 /ontology-flowchart 创建流程图后再回来
"
- **继续**: set `SUMMARY_PATHS = empty`, `REPORT_PATHS = REPORTS`，进入 Step 2。
- **先做流程图**: 先 `python3 .claude/scripts/state_helper.py release extract-operation`；再读取 `.claude/commands/ontology-flowchart.md` 并执行；完成后 Claude 重新运行本命令。
- **取消**: `python3 .claude/scripts/state_helper.py release extract-operation`，stop。

**模式 D — 两者都有：**
Tell user:
"已发现流程图摘要 {N} 份和调研报告 {M} 份。
- 回复 **两者结合**（推荐）→ 流程图为主，调研报告补充细节
- 回复 **跳过调研** → 只用流程图摘要
- 回复 **跳过流程图** → 只用调研报告（适合流程图过时或不完整时）
"
- **两者结合**（或默认）: `SUMMARY_PATHS = SUMMARIES`, `REPORT_PATHS = REPORTS`。
- **跳过调研**: `SUMMARY_PATHS = SUMMARIES`, `REPORT_PATHS = empty`。
- **跳过流程图**: `SUMMARY_PATHS = empty`, `REPORT_PATHS = REPORTS`。
- **取消**: `python3 .claude/scripts/state_helper.py release extract-operation`，stop。

## Step 1.5: Bind Excel supplements to selected flowcharts

仅当 `SUMMARY_PATHS` 非空时执行。Excel 不是独立提取来源，必须按同名规则绑定到流程图摘要：

- `PAIRED_EXCEL_PATHS`: 对每个 `ontology/diagrams-summary/{流程名}.md`，若存在 `ontology/excel/{流程名}.xlsx`，加入列表。
- `ORPHAN_EXCEL_PATHS`: `ontology/excel/*.xlsx` 中 stem 不在 `SUMMARY_PATHS` stem 集合内的文件。
- `MISSING_EXCEL_STEMS`: `SUMMARY_PATHS` 中没有同名 Excel 的流程名。

用脚本把匹配到的 Excel 导出为 Markdown 上下文：

```bash
python3 .claude/scripts/excel_context.py --summary {逐个 SUMMARY_PATHS} --excel-dir ontology/excel
```

Set `EXCEL_CONTEXT` = 脚本 stdout。若 stdout 为空，置为 `（无同名 Excel 补充表）`。

若 `ORPHAN_EXCEL_PATHS` 非空，提取前提示用户：

"发现未匹配流程图摘要的 Excel：{ORPHAN_EXCEL_PATHS}。孤立 Excel 不作为全局提取来源，避免把其他流程 sheet 的细节串入本次提取。
- 回复 **继续** → 忽略这些孤立 Excel，继续提取
- 回复 **校验流程图** → 先检查流程图与 Excel 是否对齐
- 回复 **刷新** → 从 resource 重新刷新拆分后的流程图/Excel/摘要
"

- **继续**（或默认）: 忽略 `ORPHAN_EXCEL_PATHS`，继续 Step 2。
- **校验流程图**: `python3 .claude/scripts/state_helper.py release extract-operation`；read `.claude/commands/ontology-validate-diagrams.md` and follow it；Stop this command。
- **刷新**: `python3 .claude/scripts/state_helper.py release extract-operation`；read `.claude/commands/ontology-flowchart.md` and follow refresh branch；Stop this command。

若 `SUMMARY_PATHS` 为空：`PAIRED_EXCEL_PATHS = empty`，`ORPHAN_EXCEL_PATHS = empty`，`EXCEL_CONTEXT = （无流程图摘要，Excel 不参与本次提取）`。

> 传给 agent 时：若 `SUMMARY_PATHS` 为空，在 prompt 中写 `SUMMARY_PATHS: (无，仅基于调研报告)`；若 `REPORT_PATHS` 为空，省略该行；若 `PAIRED_EXCEL_PATHS` 为空，写 `PAIRED_EXCEL_PATHS: (无同名 Excel 补充表)`。

## Step 2: Determine operation mode

List `ontology/operation/*.md`.

If operation files already exist and MODE is not pre-set:

```
已发现逻辑文档 {K} 个。

**下一步建议：**
- 回复 **增量** → 只补充缺失逻辑，不覆盖已有逻辑
- 回复 **重做** → 清空 `ontology/operation/` 后重新提取逻辑
上述选项只是参考，你也可以直接说明想修改、补充、校验或调整的内容，不必局限于这些建议。
```

Handle user choice:
- **增量**: set `MODE = incremental`; collect `EXISTING_FILES` from current `ontology/operation/*.md` stems.
- **重做**: clear `ontology/operation/*.md`; set `MODE = full`.
- **完成**: `python3 .claude/scripts/state_helper.py release extract-operation`，Stop.

If operation files do not exist, set `MODE = full`.

When called from another command with `MODE = incremental` pre-set, skip this prompt and collect `EXISTING_FILES` from current `ontology/operation/*.md`.

## Step 3: Execute operation extraction

```bash
python3 .claude/scripts/state_helper.py acquire extract-operation operation
```

Tell user: "**逻辑提取**\n正在从{source_label}中识别和提取业务逻辑..."

> `{source_label}` 取值：`SUMMARY_PATHS` 非空且 `PAIRED_EXCEL_PATHS` 非空且 `REPORT_PATHS` 非空 → "流程图、Excel 补充表和调研报告"；`SUMMARY_PATHS` 非空且 `PAIRED_EXCEL_PATHS` 非空 → "流程图和 Excel 补充表"；`SUMMARY_PATHS` 非空且 `REPORT_PATHS` 非空 → "流程图和调研报告"；仅 `SUMMARY_PATHS` → "流程图摘要"；仅 `REPORT_PATHS` → "调研报告"。

Read `.claude/agents/extract-operation-agent.md`. Dispatch it with:

```
{若 KNOWLEDGE_BLOCK 非空：原样粘贴 KNOWLEDGE_BLOCK 在此处，紧跟一行分隔符 ---}

SUMMARY_PATHS: {摘要文件路径}
PAIRED_EXCEL_PATHS: {逗号分隔的同名 Excel 路径；无则写 (无同名 Excel 补充表)}
EXCEL_CONTEXT:
{EXCEL_CONTEXT}

REPORT_PATHS: {调研报告路径，无则省略}
OBJECTS_DIR: ontology/object/
OPERATION_DIR: ontology/operation/
TEMPLATES_DIR: .claude/templates/
{如 MODE=incremental: EXISTING_FILES: {现有文件名}}

请按照你的指令完整提取所有业务逻辑，写入对应目录。

---

请阅读并遵循 `.claude/BUSINESS_KNOWLEDGE_SNIPPET.md` 的「业务知识识别守则」。
```

Wait for completion. Display summary.

**Coverage check:**
Dispatch `coverage-check-agent` with:
```
{若 KNOWLEDGE_BLOCK 非空：原样粘贴 KNOWLEDGE_BLOCK 在此处，紧跟一行分隔符 ---}

SOURCE_PATHS: {SUMMARY_PATHS、PAIRED_EXCEL_PATHS 与 REPORT_PATHS 的并集，逗号分隔；至少有一个}
EXCEL_CONTEXT:
{EXCEL_CONTEXT}

OPERATION_DIR: ontology/operation/
CHECK_TYPE: operation
```

Handle gaps（全部补充 / 选择性补充 / 跳过），with one-retry limit.

**BFO 参考提醒（可选）**：按 `.claude/reference/BFO_MODELING_GUIDE.md` §10 评估：
- 用 §10.2 的逻辑关键词列表（含"状态机型 / 计划规范"等）对本批次逻辑文件名做子串匹配。
- 命中 → 按 §10.3「带匹配项的形式」输出提示；本次执行只触发一次。
- 未命中 → 不输出。

Ask: "逻辑提取完成，共 {N} 个。如需修改请直接说明；确认无误请回复 **确认**。"

Handle modifications if any.

When user confirms（回复 **确认** 或等价的肯定词，如"好的"、"可以"、"没问题"、"继续"、"是"）:

Determine TOPIC from flowchart summary filenames, research report filenames, or ask user.

```bash
git add ontology/operation/
git commit -m "extract: {TOPIC} operation"
```

**Operation 范式参考提醒（可选）**：按 `.claude/reference/OPERATION_PARADIGM_GUIDE.md` §七「逻辑提取完成后」输出提示；若用户本会话已表达"不需要 / 知道了 / 已判别"，本次不再提示。该提醒与 `.claude/reference/ASSERTION_DRIVEN_TESTING_GUIDE.md` §十一的逻辑提取完成后提醒合并输出。

"**提取完成。** 对象：{M} 个，逻辑：{K} 个。

💡 可选参考：逻辑提取已完成，进入校验/追踪前可先判别 Operation 范式。
可参阅 `.claude/reference/OPERATION_PARADIGM_GUIDE.md` §三 / §四，判断当前业务更适合 SOP 流程型、断言驱动型或混合型验证策略。若判定为断言驱动型或混合型，再参阅 `.claude/reference/ASSERTION_DRIVEN_TESTING_GUIDE.md` §三补充测试场景。无需中断当前流程。

**下一步建议：**
- 回复 **校验**（/ontology-validate）→ 立即开始校验
- 回复 **整体视图**（/ontology-overview）→ 查看对象/逻辑全局关系
上述选项只是参考，你也可以直接说明想修改、补充、校验或调整的内容，不必局限于这些建议。"

If user replies **校验**: `python3 .claude/scripts/state_helper.py release extract-operation`，read `.claude/commands/ontology-validate.md` and follow it. Stop this command.
If user replies **整体视图**: `python3 .claude/scripts/state_helper.py release extract-operation`，read `.claude/commands/ontology-overview.md` and follow it. Stop this command.
If user replies **完成**: `python3 .claude/scripts/state_helper.py release extract-operation`，Stop.

## Note on mid-process edits

If the user asks to modify a document at any point before committing, edit the file immediately. All changes are included in the final phase-end commit.
