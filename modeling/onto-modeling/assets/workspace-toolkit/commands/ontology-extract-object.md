Extract business objects from available source material. Optional mode/input: $ARGUMENTS

This command is the object-only half of extraction. It reads flowchart summaries, same-name Excel supplements, and/or research reports, writes `ontology/object/*.md`, checks coverage, then commits the object phase.

## Step 0: 前置检查

遵循 `.claude/STEP0_TEMPLATE.md`，COMMAND_NAME = `extract-object`。简述：

1. `python3 .claude/scripts/state_helper.py check-lock extract-object`
   - `locked:{ts}:{phase}` → 按 **错误恢复类** 提示；phase 可能是 `source` / `objects`，**续执**时跳到对应阶段。
2. `python3 .claude/scripts/state_helper.py check-prereq extract-object`
   - `missing:调研完成` → 先 `python3 .claude/scripts/state_helper.py detect-stage` 区分两种情况：
     - `未开始` → 按 **跳步守卫** 提示："当前既无调研报告也无流程图，提取无源。" 选项 **先做 调研**（/ontology-research）/ **先做 流程图**（/ontology-flowchart）。
     - `调研待核验` → 报告已存在但内容未通过验收。按 **跳步守卫** 提示："调研报告的计数、ID 或引用字段尚未对账，直接提取会把错误字段和 ID 带进对象、规则与追溯。" 选项 **先做 核验**（`python3 .claude/scripts/research_checker.py check`，按 `/ontology-research` 的 Step 4.5 处理结果）；用户仍选 **继续** 时照常放行，但在本轮摘要里标注"来源报告未通过内容验收"。
   - 若只有研究报告（已验收）或只有流程图，prereq 会通过；Step 1 会依据实际存在的素材决定提取模式。
3. `python3 .claude/scripts/state_helper.py acquire extract-object source`
4. 加载已沉淀业务知识。SCOPE_HINTS 取已存在的对象名（首次提取时仅 `global`）：
   ```bash
   SCOPE_ARGS=(--scope global)
   for f in ontology/object/*.md; do
       [ -f "$f" ] || continue
       stem=$(basename "$f" .md)
       SCOPE_ARGS+=(--scope "object:$stem")
   done
   KNOWLEDGE_BLOCK=$(python3 .claude/scripts/knowledge_helper.py load "${SCOPE_ARGS[@]}")
   ```
   若 `KNOWLEDGE_BLOCK` 等于 `（暂无相关业务知识）`，置为空字符串以跳过注入。

进入对象提取阶段时再调用：
```bash
python3 .claude/scripts/state_helper.py acquire extract-object objects
```

命令结束前 `release extract-object`。

## 业务知识识别（贯穿整个命令）

整个命令期间始终遵循 `.claude/BUSINESS_KNOWLEDGE_SNIPPET.md`。
agent prompt 需 transclude 该文件全文。

## Step 1: Load source material

检查两个可能的提取来源：
- `ontology/diagrams-summary/` 下的 `.md` 文件（流程图摘要）→ `SUMMARIES`
- `ontology/excel/` 下与流程图摘要同名的 `.xlsx` 文件（Excel 补充表）→ 后续绑定到对应流程图
- `research/` 下的 `.md` 文件（调研报告）→ `REPORTS`

**调研报告只有通过内容验收才作为来源。** `REPORTS` 非空时先确认验收状态：

```bash
python3 .claude/scripts/research_checker.py status
```

退出码 0 = 已验收，按下面的模式继续。非 0（待核验或未通过）时，这份报告的计数、ID
与字段还没对过账——有流程图不代表报告可用，阶段前置只看得到"有流程图"：

- `SUMMARIES` 非空 → 置 `REPORTS = empty`，按模式 B 只用流程图继续，并告诉用户：
  "调研报告（`research/{文件名列表}`）尚未通过内容验收，本次只基于流程图提取。
  用 `python3 .claude/scripts/research_checker.py check` 核验通过后，可再以增量模式
  补充报告里的内容。"
- `SUMMARIES` 为空（报告是唯一来源）→ 用户已在 Step 0 的跳步守卫里选过 **继续** 时，
  保留 `REPORTS` 并在本轮摘要里标注"来源报告未通过内容验收"；否则按
  `.claude/STEP0_TEMPLATE.md` 的 **跳步守卫** 给出同样的后果说明与选项
  （**先做 核验** / **继续** / **取消**），用户选 **继续** 才保留 `REPORTS`，
  选 **取消** 时先 `python3 .claude/scripts/state_helper.py release extract-object` 再 stop。

根据存在情况分四种模式：

**模式 A — 两者都为空：**
Tell user:
"未找到流程图摘要，也未找到调研报告。对象提取需要至少一个来源，请先：
- 回复 **调研**（/ontology-research）→ 做领域调研
- 回复 **流程图**（/ontology-flowchart）→ 直接画流程图
"
任一选择都会离开本命令，因此在等待用户回复前先释放锁：
```bash
python3 .claude/scripts/state_helper.py release extract-object
```
然后 stop。

**模式 B — 仅有流程图摘要（`SUMMARIES` 非空，`REPORTS` 空）：**
Set `SUMMARY_PATHS = SUMMARIES`, `REPORT_PATHS = empty`。Proceed to Step 2。

**模式 C — 仅有调研报告（`SUMMARIES` 空，`REPORTS` 非空）：**
Tell user:
"未找到流程图摘要，将仅基于调研报告提取对象（`research/{文件名列表}`）。流程图是可选步骤，概念明确时可跳过。
- 回复 **继续** → 仅基于调研报告提取对象
- 回复 **先做流程图** → 转入 /ontology-flowchart 创建流程图后再回来
"
- **继续**: set `SUMMARY_PATHS = empty`, `REPORT_PATHS = REPORTS`，进入 Step 2。
- **先做流程图**: 先 `python3 .claude/scripts/state_helper.py release extract-object`；再读取 `.claude/commands/ontology-flowchart.md` 并执行；完成后 Claude 重新运行本命令。
- **取消**: `python3 .claude/scripts/state_helper.py release extract-object`，stop。

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
- **取消**: `python3 .claude/scripts/state_helper.py release extract-object`，stop。

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
- **校验流程图**: `python3 .claude/scripts/state_helper.py release extract-object`；read `.claude/commands/ontology-validate-diagrams.md` and follow it；Stop this command。
- **刷新**: `python3 .claude/scripts/state_helper.py release extract-object`；read `.claude/commands/ontology-flowchart.md` and follow refresh branch；Stop this command。

若 `SUMMARY_PATHS` 为空：`PAIRED_EXCEL_PATHS = empty`，`ORPHAN_EXCEL_PATHS = empty`，`EXCEL_CONTEXT = （无流程图摘要，Excel 不参与本次提取）`。

> 传给 agent 时：若 `SUMMARY_PATHS` 为空，在 prompt 中写 `SUMMARY_PATHS: (无，仅基于调研报告)`；若 `REPORT_PATHS` 为空，省略该行；若 `PAIRED_EXCEL_PATHS` 为空，写 `PAIRED_EXCEL_PATHS: (无同名 Excel 补充表)`。

## Step 1.6: 资料用途约束（schema / term / 数据表表头）

读取 `resource/materials.json`（不存在则跳过本步）：

- 存在 `purpose=schema` 的表格时，这些表是对象与属性的**事实来源**：
  - 每个 Sheet 对应一个候选对象，属性英文名称必须逐列取自真实表头，不得虚构、改名或合并列；
  - 类型与主键候选只能标注为"推断"；样例行只用于类型判断，不得据此编造业务含义或必填性；
  - 空表头、重名列、主键歧义 → 用业务化的选项询问用户，不自行决断。
- 存在 `purpose=term` 的术语表时，对象命名、别名、属性中文名必须与术语表对齐；冲突时以术语表为准并在文档"来源"处标注。
- `reading_mode=full` 的资料在提取前必须已全文读取。
- 运行 `python3 .claude/scripts/check_eval_data.py --headers`，stdout（JSON：`{Sheet 名: [表头列]}`，来自 schema 资料与评测数据源上传的数据表）记为 `TABLE_HEADERS`；为空（`{}`）时记为 `（无）`。
  `TABLE_HEADERS` 非空时，它是表名与列名的**唯一依据**：对象「# 数据来源」取其中的 Sheet 名，对不上任何 Sheet 的对象填「无」；从库里读取的属性英文名称逐字取自该 Sheet 的表头；数据字典类文档、调研报告里的字段名与之冲突时以 `TABLE_HEADERS` 为准，冲突清单写进返回摘要的注意事项。提取后的规则检查会按对象逐列对账：声明的表不存在、一列都对不上、或属性别名里才是真实列名，均判为错误并阻止提交（发布时同样会被阻断）。
  `TABLE_HEADERS` 为 `（无）` 时，所有对象的「# 数据来源」填「无」，不得臆造表名；规则检查会对声明了表名的对象给出警告。

## Step 2: Determine object mode

List `ontology/object/*.md`.

If object files already exist and MODE is not pre-set:

```
已发现对象文档 {M} 个。

**下一步建议：**
- 回复 **增量** → 只补充缺失对象，不覆盖已有对象
- 回复 **重做** → 清空 `ontology/object/` 后重新提取对象
上述选项只是参考，你也可以直接说明想修改、补充、校验或调整的内容，不必局限于这些建议。
```

Handle user choice:
- **增量**: set `MODE = incremental`; collect `EXISTING_FILES` from current `ontology/object/*.md` stems.
- **重做**: clear `ontology/object/*.md`; set `MODE = full`.
- **完成**: `python3 .claude/scripts/state_helper.py release extract-object`，Stop.

If object files do not exist, set `MODE = full`.

When called from another command with `MODE = incremental` pre-set, skip this prompt and collect `EXISTING_FILES` from current `ontology/object/*.md`.

## Step 3: Execute object extraction

```bash
python3 .claude/scripts/state_helper.py acquire extract-object objects
```

Tell user: "**对象提取**\n正在从{source_label}中识别和提取业务对象..."

> `{source_label}` 取值：`SUMMARY_PATHS` 非空且 `PAIRED_EXCEL_PATHS` 非空且 `REPORT_PATHS` 非空 → "流程图、Excel 补充表和调研报告"；`SUMMARY_PATHS` 非空且 `PAIRED_EXCEL_PATHS` 非空 → "流程图和 Excel 补充表"；`SUMMARY_PATHS` 非空且 `REPORT_PATHS` 非空 → "流程图和调研报告"；仅 `SUMMARY_PATHS` → "流程图摘要"；仅 `REPORT_PATHS` → "调研报告"。

Read `.claude/agents/extract-objects-agent.md`. Dispatch it as a subagent with:

```
{若 KNOWLEDGE_BLOCK 非空：原样粘贴 KNOWLEDGE_BLOCK 在此处，紧跟一行分隔符 ---}

SUMMARY_PATHS: {逗号分隔的摘要文件路径}
PAIRED_EXCEL_PATHS: {逗号分隔的同名 Excel 路径；无则写 (无同名 Excel 补充表)}
EXCEL_CONTEXT:
{EXCEL_CONTEXT}
TABLE_HEADERS:
{TABLE_HEADERS}

REPORT_PATHS: {调研报告路径，无则省略此行}
OBJECTS_DIR: ontology/object/
TEMPLATES_DIR: .claude/templates/
{如 MODE=incremental: EXISTING_FILES: {逗号分隔的现有文件名}}

请按照你的指令完整提取所有业务对象，写入对应目录。

---

请阅读并遵循 `.claude/BUSINESS_KNOWLEDGE_SNIPPET.md` 的「业务知识识别守则」。
```

Wait for completion. Display summary to user.

**Coverage check:**
Tell user: "正在检查对象覆盖度..."

Dispatch `coverage-check-agent` with:
```
{若 KNOWLEDGE_BLOCK 非空：原样粘贴 KNOWLEDGE_BLOCK 在此处，紧跟一行分隔符 ---}

SOURCE_PATHS: {SUMMARY_PATHS、PAIRED_EXCEL_PATHS 与 REPORT_PATHS 的并集，逗号分隔；至少有一个}
EXCEL_CONTEXT:
{EXCEL_CONTEXT}

OBJECTS_DIR: ontology/object/
CHECK_TYPE: object
```

Display coverage results. Handle gaps（全部补充 / 选择性补充 / 跳过），with one-retry limit.

**BFO 参考提醒（可选）**：按 `.claude/reference/BFO_MODELING_GUIDE.md` §10 评估是否触发：
- 用 §10.2 的对象关键词列表对本批次对象文件名做子串匹配，得到 `matched_keywords`（去重，最多取 5 个）。
- 命中 → 按 §10.3「带匹配项的形式」输出提示；本次执行只触发一次。
- 未命中 → 不输出。

Ask: "对象提取完成，共 {N} 个。如需修改请直接说明；确认无误请回复 **确认**。"

Handle modifications if any.

When user confirms（回复 **确认** 或等价的肯定词，如"好的"、"可以"、"没问题"、"继续"、"是"）:

Determine TOPIC from flowchart summary filenames, research report filenames, or ask user.

```bash
git add ontology/object/
git commit -m "extract: {TOPIC} objects"
```

Display:
"**下一步建议：**
- 回复 **检验对象**（/ontology-validate）→ 先对已确认对象做规则和语义检验
- 回复 **提取逻辑**（/ontology-extract-operation）→ 基于已确认对象提取业务逻辑
上述选项只是参考，你也可以直接说明想修改、补充、校验或调整的内容，不必局限于这些建议。"

内部执行时，`{OBJECT_STEMS}` 为当前 `ontology/object/*.md` 的文件名 stem，空格分隔。不要在用户可见提示中展开这些对象名称。

If user replies **检验对象**: `python3 .claude/scripts/state_helper.py release extract-object`，read `.claude/commands/ontology-validate.md` and follow it with `$ARGUMENTS = OBJECT_STEMS`。Stop this command.
If user replies **提取逻辑**: `python3 .claude/scripts/state_helper.py release extract-object`，read `.claude/commands/ontology-extract-operation.md` and follow it. Stop this command.
If user replies **完成**: `python3 .claude/scripts/state_helper.py release extract-object`，Stop.

## Note on mid-process edits

If the user asks to modify a document at any point before committing, edit the file immediately. All changes are included in the final phase-end commit.
