Extract business operation from available source material and confirmed object documents. Optional mode/input: $ARGUMENTS

This command is the non-object half of extraction. It requires existing `ontology/object/*.md`, reads flowchart summaries, same-name Excel supplements, and/or research reports, and writes each candidate to the directory its 分类 declares: `操作` → `ontology/operation/*.md`, `规则` → `ontology/rule/*.md`, `指标` → `ontology/metric/*.md`. It then checks coverage and commits the phase.

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
- `research/` 下的 `.md` 文件（资料分析报告）→ `REPORTS`

**资料分析报告只有通过内容验收才作为来源。** `REPORTS` 非空时先确认验收状态：

```bash
python3 .claude/scripts/research_checker.py status
```

退出码 0 = 已验收，按下面的模式继续。非 0（待核验或未通过）时，这份报告的计数、ID、
字段与分类还没对过账——Step 1.6 的分流正是按它的分类列做的：

- `SUMMARIES` 非空 → 置 `REPORTS = empty`，按模式 B 只用流程图继续，并告诉用户：
  "资料分析报告（`research/{文件名列表}`）尚未通过内容验收，本次只基于流程图提取。
  用 `python3 .claude/scripts/research_checker.py check` 核验通过后，可再以增量模式
  补充报告里的内容。"
- `SUMMARIES` 为空（报告是唯一来源）→ 按 `.claude/STEP0_TEMPLATE.md` 的 **跳步守卫**
  提示："资料分析报告的计数、ID、引用字段或分类尚未对账，直接提取会把错误内容带进逻辑、
  规则与指标，并可能分流到错误目录。" 选项 **先做 核验** / **继续** / **取消**。
  用户选 **继续** 才保留 `REPORTS`，并在本轮摘要里标注"来源报告未通过内容验收"；
  选 **取消** 时先 `python3 .claude/scripts/state_helper.py release extract-operation` 再 stop。

根据存在情况分四种模式：

**模式 A — 两者都为空：**
Tell user:
"未找到流程图摘要，也未找到资料分析报告。逻辑提取需要至少一个来源，请先：
- 回复 **资料分析**（/ontology-research）→ 做资料分析
- 回复 **流程图**（/ontology-flowchart）→ 直接画流程图
"
任一选择都会离开本命令，因此在等待用户回复前先释放锁：
```bash
python3 .claude/scripts/state_helper.py release extract-operation
```
然后 stop。

**模式 B — 仅有流程图摘要（`SUMMARIES` 非空，`REPORTS` 空）：**
Set `SUMMARY_PATHS = SUMMARIES`, `REPORT_PATHS = empty`。Proceed to Step 2。

**模式 C — 仅有资料分析报告（`SUMMARIES` 空，`REPORTS` 非空）：**
Tell user:
"未找到流程图摘要，将仅基于资料分析报告提取逻辑（`research/{文件名列表}`）。流程图是可选步骤，概念明确时可跳过。
- 回复 **继续** → 仅基于资料分析报告提取逻辑
- 回复 **先做流程图** → 转入 /ontology-flowchart 创建流程图后再回来
"
- **继续**: set `SUMMARY_PATHS = empty`, `REPORT_PATHS = REPORTS`，进入 Step 2。
- **先做流程图**: 先 `python3 .claude/scripts/state_helper.py release extract-operation`；再读取 `.claude/commands/ontology-flowchart.md` 并执行；完成后 Claude 重新运行本命令。
- **取消**: `python3 .claude/scripts/state_helper.py release extract-operation`，stop。

**模式 D — 两者都有：**
Tell user:
"已发现流程图摘要 {N} 份和资料分析报告 {M} 份。
- 回复 **两者结合**（推荐）→ 报告里的操作、规则、指标全部生成，流程图提供步骤与数据流；报告没收录的流程图作为补充单独成文，提取后对账标明来源
- 回复 **跳过资料分析** → 只用流程图摘要
- 回复 **跳过流程图** → 只用资料分析报告（适合流程图过时或不完整时）
"
- **两者结合**（或默认）: `SUMMARY_PATHS = SUMMARIES`, `REPORT_PATHS = REPORTS`。
- **跳过资料分析**: `SUMMARY_PATHS = SUMMARIES`, `REPORT_PATHS = empty`。
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

> 传给 agent 时：若 `SUMMARY_PATHS` 为空，在 prompt 中写 `SUMMARY_PATHS: (无，仅基于资料分析报告)`；若 `REPORT_PATHS` 为空，省略该行；若 `PAIRED_EXCEL_PATHS` 为空，写 `PAIRED_EXCEL_PATHS: (无同名 Excel 补充表)`。

## Step 1.6: 按分类拆分候选，比对流程图

实体类型由所在目录决定：规则留在 `ontology/operation/` 会被发布包当成 operations
打包、studio 图谱也按操作建节点，知识包五元组整条链路跟着错。所以分流只认报告里的
确定性分类，不按名字猜。

仅当 `REPORT_PATHS` 非空时执行。拆分与比对交给脚本，不要自己读表：

```bash
python3 .claude/scripts/report_reconcile.py plan --json --reports {REPORT_PATHS，空格分隔} --summaries {SUMMARY_PATHS，空格分隔；为空时只写 --summaries}
```

- 退出码 2：报告清单没有「分类」列或分类取值非法，说明报告还是旧契约或没过验收。把脚本给出的
  原因告诉用户，提示先跑 `python3 .claude/scripts/research_checker.py` 核验、补齐分类后再回到
  本命令，**不要自行推断分类**；先 `python3 .claude/scripts/state_helper.py release extract-operation`
  再 stop。
- 其余情况按 JSON 取值：
  - `OPERATION_CANDIDATES` = `candidates.operation`；`RULE_CANDIDATES` = `candidates.rule`；
    `METRIC_CANDIDATES` = `candidates.metric`（`candidates.object` 归对象提取，本命令不处理）
  - `DOCUMENT_NAMES` = `document_names`：每项提供原名 `name`、实际路径 `path`、`exists` 和需要写入的 `frontmatter`；`operation` 同时包含流程图补充项（`source=flowchart`），统一检查跨目录实体文件名冲突。原名与路径分开传递，禁止自行替换标点或解码路径。
  - `flowchart_matches`：与报告里的操作同名的流程图，两边合成一份文档
  - `FLOWCHART_SUPPLEMENTS` = `flowchart_supplements`：报告没收录的流程图，允许作为流程图补充
    生成操作，提取后在对账里标明来源
  - `FLOWCHART_SKIPPED` 初始为空，`ALIASES` 初始为空

- 退出码 1：有需要用户决定的冲突，提取前逐条处理：
  - `naming_conflicts`：安全文件名被其他名称占用、原名映射无效或重复。展示冲突，先 release 再 stop；禁止覆盖文件或凭标点相似度猜测归属。
  - `category_conflicts`：多份报告给同一名称不同的分类。告诉用户统一报告里的分类并重新核验后
    再提取；先 release 再 stop。
  - `flowchart_conflicts`：流程图名称在报告里是规则、指标或对象，流程图把它当操作、报告不是。
    逐条问用户：

    "流程图「{name}」在资料分析报告里是{对应类型}，不能直接按操作生成。
    - 回复 **改报告** → 把报告里这一条的分类改成「操作」并重新核验后再提取
    - 回复 **改名生成** → 按流程图生成操作，换一个名字（默认「{name}流程」，也可以直接给名字）
    - 回复 **不单独成文** → 这份流程图不生成操作文档"

    - **改报告**：release 后 stop。
    - **改名生成**：在 `FLOWCHART_SUPPLEMENTS` 里加 `{新名字}={name}`，在 `ALIASES` 里记一条 `{新名字}={name}`。
    - **不单独成文**：把 `{name}` 加进 `FLOWCHART_SKIPPED`。

`REPORT_PATHS` 为空（纯流程图来源）时不跑脚本、三组候选均为空：本命令只做操作提取，行为与
之前一致；规则与指标仍可事后用 `/ontology-extract-rule-metric` 从已落盘文档补抽。

## Step 2: Determine operation mode

List `ontology/operation/*.md`、`ontology/rule/*.md` 与 `ontology/metric/*.md`（目录索引 `index.md` 不算文档）。
本命令同时产出这三类文档。增量对三个目录一视同仁，已存在的一律不重新生成——否则会把
校验阶段修过的规则再生成一遍。重做只清空本轮生成得回来的目录：`ontology/operation/` 总是
清空；`ontology/rule/` 与 `ontology/metric/` 只在本轮用了资料分析报告（`REPORT_PATHS` 非空，
Step 1.6 已按分类拆出候选）时才清空，这时规则与指标以报告为准，清掉才不会留下报告里已经
没有的旧规则。本轮没用报告时，这两个目录里的文档本轮生成不回来（可能是从知识包导入的，
也可能是 /ontology-extract-rule-metric 补抽的），一律保留。

If any of the three directories already has documents and MODE is not pre-set:

```
已发现逻辑文档 {K} 个、规则 {R} 条、指标 {P} 个。

**下一步建议：**
- 回复 **增量** → 只补充缺失的逻辑、规则与指标，不覆盖已有文档
- 回复 **重做** → 清空 {REPORT_PATHS 非空：`ontology/operation/`、`ontology/rule/`、`ontology/metric/`；REPORT_PATHS 为空：`ontology/operation/`} 后重新提取
{若 REPORT_PATHS 为空且 R+P > 0，追加一行：
"注意：本轮没有使用资料分析报告，重做只清空 `ontology/operation/`，现有的 {R} 条规则与 {P} 个指标保留不动。"}
上述选项只是参考，你也可以直接说明想修改、补充、校验或调整的内容，不必局限于这些建议。
```

Handle user choice:
- **增量**: set `MODE = incremental`。报告模式使用目录处理后重新生成的 `DOCUMENT_NAMES` 判断已有项；仅无报告模式收集 `EXISTING_FILES`（当前 `ontology/operation/*.md` stems）。
- **重做**: clear `ontology/operation/*.md`；`REPORT_PATHS` 非空时同时清空 `ontology/rule/*.md` 与 `ontology/metric/*.md`，为空时这两个目录不动; set `MODE = full`.
- **完成**: `python3 .claude/scripts/state_helper.py release extract-operation`，Stop.

If none of the three directories has documents, set `MODE = full`.

When called from another command with `MODE = incremental` pre-set, skip this prompt；报告模式仍须重新生成 `DOCUMENT_NAMES`，仅无报告模式收集 `EXISTING_FILES`（当前 `ontology/operation/*.md` stems）。

选择增量或重做、完成目录处理后，报告模式必须重跑 plan，刷新 `DOCUMENT_NAMES`（尤其是重做删除文件后的 `exists` 和历史路径）：沿用 Step 1.6 的报告参数，`--summaries` 排除 `FLOWCHART_SKIPPED`，并为 `ALIASES` 每项传入 `--alias 文档名=流程图名`。
流程图补充项的原名、用户指定的新名与报告候选共同参与路径规划和跨目录 stem 检查。用户修改补充项名称后也要重新规划，存在 `naming_conflicts` 时不得派发或覆盖文件。
报告模式中，是否已存在仅以最新 `DOCUMENT_NAMES` 内对应分类、业务原名 `name` 的 `exists` 为准；不得用 `EXISTING_FILES` 或其他文件 stem 列表匹配业务原名。每次重做、补提或修改文件后重新规划，丢弃旧计划。
既有文件被人工改名但没有原名记录时，不自动合并：核实报告来源后可在该文档头部补充
`report_name: "原始业务名称"`（JSON 字符串，放在文件开头的一对 `---` 内；已有文档头时合并字段），再重跑 plan/check，不批量重命名。

## Step 3: Execute operation extraction

```bash
python3 .claude/scripts/state_helper.py acquire extract-operation operation
```

Tell user: "**逻辑提取**\n正在从{source_label}中识别和提取业务逻辑..."

> `{source_label}` 取值：`SUMMARY_PATHS` 非空且 `PAIRED_EXCEL_PATHS` 非空且 `REPORT_PATHS` 非空 → "流程图、Excel 补充表和资料分析报告"；`SUMMARY_PATHS` 非空且 `PAIRED_EXCEL_PATHS` 非空 → "流程图和 Excel 补充表"；`SUMMARY_PATHS` 非空且 `REPORT_PATHS` 非空 → "流程图和资料分析报告"；仅 `SUMMARY_PATHS` → "流程图摘要"；仅 `REPORT_PATHS` → "资料分析报告"。

Read `.claude/agents/extract-operation-agent.md`. 派发前运行 `python3 .claude/scripts/field_semantics.py joins`，
stdout 记为 `JOIN_KEYS`（已声明的关联字段对与各对象的标识字段）；输出里 `truncated` 为 true 时，改用
`--objects {本轮来源涉及的对象名}` 收窄后重跑。Dispatch it with:

```
{若 KNOWLEDGE_BLOCK 非空：原样粘贴 KNOWLEDGE_BLOCK 在此处，紧跟一行分隔符 ---}

SUMMARY_PATHS: {摘要文件路径}
PAIRED_EXCEL_PATHS: {逗号分隔的同名 Excel 路径；无则写 (无同名 Excel 补充表)}
EXCEL_CONTEXT:
{EXCEL_CONTEXT}
JOIN_KEYS:
{JOIN_KEYS}

REPORT_PATHS: {资料分析报告路径，无则省略}
DOCUMENT_NAMES: {最新 plan 的 document_names.operation，包含报告操作及流程图补充项，保留 JSON 结构；无报告时省略}
OPERATION_CANDIDATES: {Step 1.6 的操作组，逗号分隔；无报告时写 (无报告分类，按来源材料识别)}
FLOWCHART_SUPPLEMENTS: {Step 1.6 的流程图补充，逗号分隔；无则写 无}
FLOWCHART_SKIPPED: {用户决定不单独成文的流程图，逗号分隔；无则写 无}
OBJECTS_DIR: ontology/object/
OPERATION_DIR: ontology/operation/
TEMPLATES_DIR: .claude/templates/
{仅无报告模式且 MODE=incremental: EXISTING_FILES: {现有文件名}}

请按照你的指令完整提取所有业务逻辑，写入 OPERATION_DIR。规则与指标不归你处理。

---

请阅读并遵循 `.claude/BUSINESS_KNOWLEDGE_SNIPPET.md` 的「业务知识识别守则」。
```

Wait for completion. Display summary.

**规则与指标提取：** 仅当 `RULE_CANDIDATES` 或 `METRIC_CANDIDATES` 非空时执行。

Tell user: "**规则与指标提取**\n正在按报告分类生成规则 {R} 条、指标 {M} 个..."

派发规则/指标前按 Step 2 的参数重跑 plan，更新 `DOCUMENT_NAMES`；只使用这次最新计划，发现冲突时先处理，不回退到 Step 1.6 的旧计划。

Read `.claude/agents/rule-metric-extraction-agent.md`. Dispatch it with:

```
{若 KNOWLEDGE_BLOCK 非空：原样粘贴 KNOWLEDGE_BLOCK 在此处，紧跟一行分隔符 ---}

SOURCE_MODE=report
REPORT_PATHS={资料分析报告路径}
DOCUMENT_NAMES={最新 plan 的 document_names.rule 与 document_names.metric，保留分类与 JSON 结构}
RULE_CANDIDATES={Step 1.6 的规则组，逗号分隔；无则写 无}
METRIC_CANDIDATES={Step 1.6 的指标组，逗号分隔；无则写 无}
OBJECT_DIR=ontology/object/
OPERATION_DIR=ontology/operation/
RULE_DIR=ontology/rule/
METRIC_DIR=ontology/metric/
RULE_TEMPLATE=.claude/templates/rule-template.md
METRIC_TEMPLATE=.claude/templates/metric-template.md
```

Wait for the JSON summary `{"rules": [...], "metrics": [...], "backlinks_added": N, "skipped": [...]}`。
派发失败或返回项少于候选数时，把缺的名称逐条告诉用户，不要把缺口留在静默里
（因最新 `DOCUMENT_NAMES` 中对应项 `exists=true` 跳过的不算缺口；`exists=false` 的候选不可因原名碰巧等于其他文件 stem 而跳过）。

**Coverage check:**
Dispatch `coverage-check-agent` with:
```
{若 KNOWLEDGE_BLOCK 非空：原样粘贴 KNOWLEDGE_BLOCK 在此处，紧跟一行分隔符 ---}

SOURCE_PATHS: {SUMMARY_PATHS、PAIRED_EXCEL_PATHS 与 REPORT_PATHS 的并集，逗号分隔；至少有一个}
EXCEL_CONTEXT:
{EXCEL_CONTEXT}

OPERATION_DIR: ontology/operation/
RULE_DIR: ontology/rule/
METRIC_DIR: ontology/metric/
CHECK_TYPE: {本轮有规则或指标候选时写 operation,rule,metric；否则写 operation}
```

Handle gaps（全部补充 / 选择性补充 / 跳过），with one-retry limit.

**对账（资料分析报告 ↔ 本轮产物）：** 仅当 `REPORT_PATHS` 非空时执行。覆盖率检查是模型判断，
报告条目有没有落地以这里的确定性对账为准：

```bash
python3 .claude/scripts/report_reconcile.py check --reports {REPORT_PATHS，空格分隔} --summaries {SUMMARY_PATHS，空格分隔；为空时只写 --summaries} {ALIASES 里每一条写一个 --alias 文档名=流程图名}
```

把脚本输出原样展示给用户，作为提取摘要里的「对账」一节。退出码 0 = 报告条目都已落在正确目录，
多出来的都有来源（流程图补充，或本轮没动的已有文档）。退出码 1 时逐类处理，处理完重跑本步骤：

- **名称映射冲突**：先修正重复或无效的 `report_name` 记录并重跑；禁止以覆盖文档消除冲突。
- **报告里有、没生成**：默认回到 Step 3 以增量模式补提这些名称（`补充提取目标` = 缺的名称）；
  用户明确说不生成的，逐条记下原因。
- **生成在错误目录**（报告里是规则或指标，却写成了操作）：按报告分类和对应模板重写到
  `ontology/rule/` 或 `ontology/metric/`，删掉 operation 目录里的那份。
- **[需说明] 报告与流程图都没有、本轮新增或改动的文档**：问用户是删除，还是保留并说明来源。
- **没有同名操作文档的流程图**：应能在逻辑提取 agent 返回的「合并」一栏里找到（并进了报告里
  的某个操作），或在 `FLOWCHART_SKIPPED` 里。两处都没有的，补提或请用户说明。

每一处差异要么处理掉，要么由用户逐条确认原因；**未处理、也未确认的差异，不进入提交**。
已确认的原因记为 `RECONCILE_NOTES`，提交时写进提交说明。

**BFO 参考提醒（可选）**：按 `.claude/reference/BFO_MODELING_GUIDE.md` §10 评估：
- 用 §10.2 的逻辑关键词列表（含"状态机型 / 计划规范"等）对本批次逻辑文件名做子串匹配。
- 命中 → 按 §10.3「带匹配项的形式」输出提示；本次执行只触发一次。
- 未命中 → 不输出。

收尾前确认所有提取及覆盖子任务均已实际结束；中断、仍在运行或返回不完整时不得宣布完成。
运行 `python3 .claude/scripts/verify_artifacts.py operations --files {本轮应生成的文件路径，逐项加引号}`，
清单取提取计划和已有对账/覆盖结果，不仅罗列已落盘文件；退出码 0 才能进入确认，
1 为确定性错误，2 为产物缺失、执行失败或核验期间文件变化。修改后重验，提交复用同修订回执。
只做本批产物和引用的确定性检查，不重复发起语义 Agent；上方对账/覆盖检查仍保留。
核验结果的 `by_category` 里「字段关联」类分开展示：错误是把不同名、非别名、未声明关联的两个字段当作同一个值
核对或关联（如「无法交叉核对则报资料冲突」），资料没有依据时删掉这句或改为注意事项里的「待确认」，
不得为了通过核验改写对象的关联声明；警告是已标「待确认」的等价关系，逐条列给用户确认。
覆盖检查只按名称比对来源，展示时写明「覆盖（按名称）」，不代表逻辑条件与字段关联正确；没有运行的检查不得写成通过，
脚本退出码 2 时写「无法核验：{原因}」。

Ask: "逻辑产物已生成：操作 {N} 个、规则 {R} 条、指标 {M} 个。
覆盖（按名称）：{结果}；轻量核验：{status}；字段关联：错误 {E} 条、待确认 {W} 条。
{REPORT_PATHS 非空时附上对账结果；有流程图补充或合并时逐条列出}
如需修改请直接说明；确认无误请回复 **确认**。"

**修改已生成或已确认的逻辑文档（候选修订，不直接覆盖）：**
用户要求修改 operation / rule / metric 文档时，不直接编辑 `ontology/` 下的文件：
1. `python3 .claude/scripts/candidate_revision.py start --plan {要改的文档路径，逐项加引号} --note "{修正说明}"`，只修改输出里的 `candidate` 路径。
2. `python3 .claude/scripts/candidate_revision.py diff`，把每个文件的增删行数与关键改动展示给用户。
3. `python3 .claude/scripts/candidate_revision.py check`：叠加候选后跑整计划规则检查（含字段关联）。退出码 0 通过；1 候选引入新错误（展示 `new_errors`，继续改候选后重新 check）或确认版本已被改动（`stale`，reject 后重新 start）；2 无法核验（展示 reason，不得接受）。
4. 用户同意后 `python3 .claude/scripts/candidate_revision.py accept` 写回；不同意时 `python3 .claude/scripts/candidate_revision.py reject --reason "{原因}"`，工作区文件不动。
5. accept 后重新运行上面的 `verify_artifacts.py`，不复用旧回执。

When user confirms（回复 **确认** 或等价的肯定词，如"好的"、"可以"、"没问题"、"继续"、"是"）:

Determine TOPIC from flowchart summary filenames, research report filenames, or ask user.

```bash
git add ontology/operation/ ontology/rule/ ontology/metric/
git commit -m "extract: {TOPIC} operation{本轮产出规则或指标时追加 /rule/metric}" {REPORT_PATHS 非空时追加 -m "对账：{对账结果的逐类计数，一行}"；有 RECONCILE_NOTES 时再追加 -m "差异说明：{逐条原因}"}
```

**Operation 范式参考提醒（可选）**：按 `.claude/reference/OPERATION_PARADIGM_GUIDE.md` §七「逻辑提取完成后」输出提示；若用户本会话已表达"不需要 / 知道了 / 已判别"，本次不再提示。该提醒与 `.claude/reference/ASSERTION_DRIVEN_TESTING_GUIDE.md` §十一的逻辑提取完成后提醒合并输出。

"**提取完成。** 对象：{M} 个，操作：{K} 个，规则：{R} 条，指标：{P} 个。
{若 K 为 0 且 R+P 大于 0，追加一行："本轮没有识别出操作，阶段仍停在「对象已提取」——校验、追踪与代码生成都依赖 operation 文档。"}

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

If the user asks to modify a document at any point before committing, use the candidate revision flow above (start → 改候选 → diff → check → accept / reject) instead of editing the file in place. Accepted changes are included in the final phase-end commit; the base version of each revision stays under `.ontology-state/revisions/`.
