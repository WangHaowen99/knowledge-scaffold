Validate the ontology documents in two sequential phases: rule check first, then semantic check. Files to check (if any): $ARGUMENTS

## Step 0: 前置检查

遵循 `.claude/STEP0_TEMPLATE.md`，COMMAND_NAME = `validate`。简述：

1. `python3 .claude/scripts/state_helper.py check-lock validate`
2. 若 `$ARGUMENTS` 非空，先按 Step 0.5 预解析目标文件与 `SCOPE`。
   - 若 `SCOPE = objects` 且 `TARGET_FILES` 非空：这是**对象增量校验**，跳过 `check-prereq validate`；对象提取完成后允许先检验对象，不要求逻辑已提取。
   - 其他情况继续执行下一条前置检查。
3. `python3 .claude/scripts/state_helper.py check-prereq validate`
   - `missing:...` → **跳步守卫**："继续会导致校验范围不完整"。选项 **继续** / **先做 逻辑提取**（/ontology-extract-operation）。
   - 若上一步已识别为对象增量校验，本条不执行。
4. `python3 .claude/scripts/state_helper.py acquire validate start`
5. 加载已沉淀业务知识（全量，validate 需要看到所有规则）：
   ```bash
   SCOPE_ARGS=(--scope global)
   while IFS= read -r f; do
       [ -f "$f" ] || continue
       stem=$(basename "$f" .md)
       case "$f" in
           */object/*) SCOPE_ARGS+=(--scope "object:$stem") ;;
           */operation/*)   SCOPE_ARGS+=(--scope "operation:$stem") ;;
       esac
   done < <(find ontology/object ontology/operation -type f -name '*.md' 2>/dev/null)
   KNOWLEDGE_BLOCK=$(python3 .claude/scripts/knowledge_helper.py load "${SCOPE_ARGS[@]}")
   ```
   若 `KNOWLEDGE_BLOCK` 等于 `（暂无相关业务知识）`，置为空字符串以跳过注入。

命令结束前 `release validate` 并 `update-progress validate_last_run {timestamp}`。

## Step 0.5: Determine validation scope

If $ARGUMENTS is not empty, enter **incremental mode**:

1. Parse $ARGUMENTS as space-separated bare names (e.g. `订单 创建订单 支付`). If $ARGUMENTS contains `--since <rev>`, take `<rev>` out as `CHANGE_BASE` before parsing names (see 6 below).
2. For each name, recursively search the four packaged entry types — `ontology/object/**/{name}.md`, `ontology/operation/**/{name}.md`, `ontology/rule/**/{name}.md`, `ontology/metric/**/{name}.md`. If none exists, warn: "⚠️ 未找到文件：{name}，已跳过。"
3. The resolved list is TARGET_FILES. If all names were invalid, fall back to full mode.
4. Compute RELATED_FILES: for each target file, read it and collect all `[[link]]` stems. Also scan all other ontology files and include any whose `[[link]]` targets include a TARGET_FILE stem. Remove TARGET_FILES from this set.
5. Display:
   ```
   增量校验模式：检查 {TARGET_FILES joined by 、}
   相关文件未包含在本次校验中：{RELATED_FILES joined by 、}
   ```
6. **只查改动（change-only）**：若取到了 `CHANGE_BASE`（/ontology-update 传入改动前的版本；校验过期后点「刷新校验」时，平台传入上次确认校验时的版本和之后改过的条目名），计算本次改动。`git diff` 报错（版本不存在）时不进入只查改动模式，按普通增量模式校验：

   ```bash
   git diff {CHANGE_BASE} -- {TARGET_FILES 的路径，空格分隔}
   ```

   - 结果记为 `CHANGE_DIFF`（含已暂存与未暂存的改动）。没有任何改动的目标文件从 TARGET_FILES 中去掉；全部为空则跳过 Phase B 的语义检查（规则检查照常）。
   - `RELATED_FILES` 改为只取 `CHANGE_DIFF` 新增、删除行里出现的 `[[链接]]`，不再收集全部引用方；引用方由语义检查子任务在改动涉及输入、输出时按需读取。
   - `CHANGE_DIFF` 超过 800 行时不进入只查改动模式，按普通增量模式校验。
   - 第 5 步的提示改为：`本次只检查改动部分：{TARGET_FILES joined by 、}`。

If $ARGUMENTS is empty, proceed in **full mode** (no message needed).

**Determine SCOPE**（用于报告文件命名）：
- 全量模式：`SCOPE = all`。
- 增量模式：按 `TARGET_FILES` 所属目录计算：
  - 若全部目标都属于 `ontology/object/` → `SCOPE = objects`
  - 若全部目标都属于 `ontology/operation/` → `SCOPE = operation`
  - 若全部目标都属于 `ontology/rule/` → `SCOPE = rule`
  - 若全部目标都属于 `ontology/metric/` → `SCOPE = metric`
  - 跨多个分类 → `SCOPE = all`
- 后续报告文件名统一使用 `reports/{YYYYMMDD}-{HHMMSS}-{SCOPE}-rule.md` 与 `reports/{YYYYMMDD}-{HHMMSS}-{SCOPE}-semantic.md`，其中日期时间使用 `Asia/Shanghai`。

---

## 业务知识识别（贯穿整个命令）

整个 validate 命令期间始终遵循 `.claude/BUSINESS_KNOWLEDGE_SNIPPET.md`。
rule-check-agent 与 semantic-check-agent 的 prompt 都需 transclude 该文件全文。

---

## Step 1: Check ontology exists

List markdown files recursively in `ontology/object/` and `ontology/operation/`. If both directories have no markdown files, tell user: "未找到本体文档，请先运行 `/ontology-extract-object`。" 然后：

```bash
python3 .claude/scripts/state_helper.py release validate
```

Stop.

---

## Phase A: 规则检查

### A1: Dispatch rule-check agent

Read `.claude/agents/rule-check-agent.md`. Dispatch it as a subagent.

在 dispatch prompt 顶部（在任何输入变量之前）插入：

```
{若 KNOWLEDGE_BLOCK 非空：原样粘贴 KNOWLEDGE_BLOCK 在此处，紧跟一行分隔符 ---}
```

If incremental mode: include in the dispatch prompt: "TARGET_FILES: {space-separated stems from TARGET_FILES}"

在 dispatch prompt 底部（在已有的收尾指令之后）追加：

```

---

请阅读并遵循 `.claude/BUSINESS_KNOWLEDGE_SNIPPET.md` 的「业务知识识别守则」。
```

Wait for it to return a JSON array of issues.

### A2: Generate rule check report

Generate filename: `reports/{YYYYMMDD}-{HHMMSS}-{SCOPE}-rule.md`（`SCOPE` 已在 Step 0.5 计算；日期时间使用 `Asia/Shanghai`）

Parse the JSON array. Group issues by `file`. Within each file, sort by severity: 错误 → 警告 → 建议. Remove exact duplicates (same `file` + same `message`).

Count totals: E = error count, W = warning count, S = suggestion count.

Write the report:

```markdown
## 规则检查结果 {YYYY-MM-DD HH:MM} Asia/Shanghai
（共 {N} 个问题：{S_COUNT} 自动修复 / {C_COUNT} 需业务判断）

### {filename1}
- 🔧 ⚠️ [术语不一致] {message} → 已自动修复
- [ ] ❌ [逻辑缺陷] {message}

### {filename2}
- 🔧 ⚠️ [链接失效] {message} → 已自动修复
- [ ] ⚠️ [参数未使用] {message}
```

Simple issues (auto-fixable) use `🔧` prefix. Complex issues (need judgment) use `[ ]` prefix.

If no issues (N=0): write "（无问题，本体文档通过全部规则检查。）" — skip to Phase B.

### A3: Auto-classify issues

Classify each issue from the A1 JSON array automatically — no user interaction in this step.

**Simple** (auto-fix, no judgment needed):
- `链接失效` — `[[Link]]` has no matching file
- `结构缺失` — required section heading missing
- `表格格式` — required table column missing
- `术语不一致` — only when message contains a clear correction target (contains "该字段定义在" or "应通过" or similar phrasing that identifies the correct location)
- `列名对账` — only severity `错误` (message contains "英文名称改为"): the real column name is already known
- `字段名称冲突` — when the issue includes the conflicting fields and enough semantic context exists; rename the conflicting alias to an accurate unique alias, never delete it

**Complex** (requires expert judgment):
- All other categories: 逻辑缺陷 / 参数未使用 / 输出不可达 / 子流程不匹配 / 业务不合理 / 描述歧义 / 关联不合理
- `术语不一致` where the message does NOT contain a clear correction target
- `列名对账` with severity `警告` (declared table missing, or attributes without a same-name column — needs to decide rename / derived / 非库字段)

### A4: Auto-fix simple issues + show summary

If no simple issues, skip to A5.

#### A4a: Bulk-apply via rule-fix-agent

Read `.claude/agents/rule-fix-agent.md`. Dispatch it as a subagent with:

```
{若 KNOWLEDGE_BLOCK 非空：原样粘贴 KNOWLEDGE_BLOCK 在此处，紧跟一行分隔符 ---}

RULE_ISSUES = {A3 分类后 simple 列表的 JSON 数组，按列表顺序为每条赋 issue_id = 1..N（仅本 A4 内部使用，不进规则报告）}
ONTOLOGY_ROOT = ontology/
```

Wait for it to return a JSON array `APPLY_RESULTS`，按 `issue_id` 索引。

> ⚠️ agent 已经把 Edit 落盘了——主命令不要再重复 Read+Edit。后续走 `APPLY_RESULTS` 摘要、`git diff`、`git checkout` 三条路径。
>
> **Scope guardrail（保留）**：自动修复**只能**修改 `ontology/` 路径下的文件——rule-fix-agent prompt 已经内置了这条栅栏。若 `APPLY_RESULTS` 中出现 `reason: scope_out_of_ontology` 的条目，把它在 A5 当成复杂问题展示。曾出过 fix 步骤误清空 `.claude/settings.json` env 块的事故。

**完整性兜底**（防 agent 漏报致 issue 静默丢失——HARD CONTRACT 失效时的最后一道防线）：

读到 `APPLY_RESULTS` 后，对每个出现在 `RULE_ISSUES` 但未出现在 `APPLY_RESULTS` 的 `issue_id`，补一条 `{issue_id, applied: false, reason: "agent_truncated_output", proposed_change: "（agent 未返回该条结果，请人工接续）"}`。这些条目走原有 `applied:false` → A5 的人工接续路径，summary 表显示为 `⚠ agent 漏报`。

> 不在这里 normalize `status:"fixed"` → `applied:true`：HARD CONTRACT 已明确禁字段名漂移，悄悄兜底等于把违规当合规，下次会复发。schema 违规 → 当作 missing 补 false 即可。

#### A4b: Show summary table

```
已批量应用 {APPLIED_COUNT} 条结构性修复，{SKIPPED_COUNT} 条未应用（需人工判断）：

| # | 编号 | 文件 | 问题 | 处理结果 |
|---|------|------|------|----------|
| 1 | 1    | operation/X.md | [[Y]] 无对应文件 | ✅ 已应用：{change_summary} |
| 2 | 5    | operation/Z.md | 字段「A」定义不一致 | ⏸ 未应用（{reason}）：{proposed_change} |

字段名称冲突的已应用修复直接保留并进入回归检查；修复代理只能改写冲突别名，不能删除别名、修改字段 key 或修改无冲突字段。
其他已应用的机械修复也直接保留；未应用条目自动并入 A5 的复杂问题列表。
```

#### A4c: 处理结果（不等回复）

- `applied: true` 条目直接保留，报告里保留 🔧 标记，进入 A5.5 回归检查。不问「确认 / 逐条复审 / 全部撤销」：每轮结束自动存成版本，
  用户要撤销可以「撤回这一轮」或在对话里说要撤哪条。
- `applied: false` 条目：合并到 A5 的复杂问题列表（带 `proposed_change` 作为起点）。

### A5: Handle complex issues via terminal

If no complex issues, skip to A5.5.

先 `python3 .claude/scripts/state_helper.py acquire validate confirm`，再 show a numbered list in the terminal — no file-based checkbox interaction（用户回复后 `acquire validate start`；逐条处理时每次等回复同样先切 `confirm`）:

```
还有 {M} 个需要业务判断的问题：

1. ❌ [逻辑缺陷] operation/X.md → {message}
2. ⚠️ [参数未使用] operation/Y.md → {message}
3. 💡 [关联不合理] operation/Z.md → {message}
```

然后用确认卡问「要处理哪些？」，选项 **全部** / **跳过**（只处理其中几条让用户在对话里说编号）；用户在对话里直接打「全部」「1,3」「跳过」也照样推进。

- **全部**: handle each one sequentially with fix proposals:
  ```
  [修改 {N}/{TOTAL}]
  文件：{file}
  问题：{message}

  修改方案：
  {specific proposed change}

  [确认修改] [我来提供方案] [跳过]
  ```
- **输入编号**: handle only the selected items, same per-item flow as above；没选中的留 `[ ]`，以后还能处理或跳过。
- **跳过**: skip all complex issues listed this round（A6 记 `⏭️`，不再算待处理）, proceed to A5.5.

### A5.5: 回归规则检查

Re-dispatch the rule-check-agent (same as A1, with the same TARGET_FILES if in incremental mode). Wait for it to return results.

Filter the new results: remove any issue whose `file` + `message` combination was already present in the A1 results. Only issues not previously seen count as regressions introduced by the fixes.

回归检查只做这一轮：查出的新问题写进报告留待处理，这一轮不再修、不再复查。

**If regressions found:**

Append them to `reports/{rule-filename}` under a new section:

```markdown
### 回归检查发现的新问题（{YYYY-MM-DD HH:MM} Asia/Shanghai）
- [ ] ❌ [{category}] {message}
- [ ] ⚠️ [{category}] {message}
```

Tell the user:
"修复后发现 {N} 个新问题（可能由本次修改引入），已追加至报告，留待处理。"

不问用户、不进 A3→A5，直接进入 A6（这些行保持 `[ ]`，算待处理）。

**If no regressions:** Tell user "回归检查通过，未发现新问题。" and proceed to A6.

---

### A6: Update rule report and commit

Update `reports/{rule-filename}`:
- `🔧` items (auto-fixed simple): keep as-is (already marked as auto-fixed)
- items that were fixed → `✅`
- items the user explicitly skipped（选「跳过」那一批里没修的、逐条处理时选「跳过」的）→ `⏭️`
- items not selected when the user picked only some numbers → keep as `[ ]`

写法是把行首的 `[ ]` 换掉，如 `- [ ] ❌ [逻辑缺陷] …` → `- ⏭️ ❌ [逻辑缺陷] …`。只有 `[ ]` 算待处理（挡住校验完成和发布）；`⏭️` 是用户明确不处理，不挡。

```bash
git add ontology/ reports/
git commit -m "fix: address rule check issues"
```

不问用户，直接进入 Phase B（工作过程里说一句「规则检查做完了，接着做语义检查」）。

---

## Phase B: 语义检查

### B1: Dispatch semantic-check agents

语义检查的子任务边查边记：问题逐项记进问题目录，回复只有一句话小结，不再返回 JSON。

1. 建问题目录（`MODE` 取 `full` / `incremental` / `change`，对应全量、增量、只查改动）：

   ```bash
   python3 .claude/scripts/semantic_issues.py init --mode {MODE}
   ```

   输出的路径记为 `ISSUES_DIR`。

2. 定要派的部分 `PARTS`：
   - 全量模式：`operation`、`object`、`cross` 三个；
   - 增量模式（有 TARGET_FILES、没有 CHANGE_DIFF）：TARGET_FILES 里有操作（`ontology/operation/`）才派 `operation`，有对象（`ontology/object/`）才派 `object`，`cross` 总是派；
   - 只查改动模式（有 CHANGE_DIFF）：不拆，只派一个 `all`。

3. Read `.claude/agents/semantic-check-agent.md`. 在**同一条消息里**一起派出 `PARTS` 的每个子任务（每部分一个），不要等前一个回来再派下一个。每个派发提示依次是：
   - 第一行按工作区规则「子任务派发」写阅读 agent 文件那句；
   - 紧接两行 `PART: {部分}`、`ISSUES_DIR: {ISSUES_DIR}`（写在知识区块前面：子任务卡片按派发提示前段取名）；
   - {若 KNOWLEDGE_BLOCK 非空：原样粘贴 KNOWLEDGE_BLOCK 在此处，紧跟一行分隔符 ---}
   - If incremental mode: "TARGET_FILES: {space-separated stems from TARGET_FILES}" 与 "RELATED_FILES: {space-separated stems from RELATED_FILES}"
   - If change-only mode (Step 0.5 第 6 条): additionally include `CHANGE_DIFF_CMD: git diff {CHANGE_BASE} -- {TARGET_FILES 的路径，空格分隔}`（子任务自己运行它取 CHANGE_DIFF；不要把 diff 原文抄进派发提示）
   - 底部（在已有的收尾指令之后）追加：

     ```

     ---

     请阅读并遵循 `.claude/BUSINESS_KNOWLEDGE_SNIPPET.md` 的「业务知识识别守则」。
     ```

4. 子任务都回来后（出错、超时的也算回来）查进度：

   ```bash
   python3 .claude/scripts/semantic_issues.py status --dir {ISSUES_DIR} --parts {PARTS，逗号分隔}
   ```

   - 有缺项的部分（子任务出错、超时或漏标）**补派一次**：派发提示同第 3 步，另在 `ISSUES_DIR` 那行后加一行 `ONLY_CHECKS: {status 列出的缺项，顿号分隔}`；`all` 没标完成的原样补派一次。已记的问题保留，不要清空目录；几个部分都要补派时同样在同一条消息里一起派。
   - 补派的都回来后再 `status` 一次；还缺的检查项记为 `UNFINISHED_CHECKS`（按部分列，如「对象类：关联合理性、业务合理性」），不再补派。

5. 合并：

   ```bash
   python3 .claude/scripts/semantic_issues.py merge --dir {ISSUES_DIR}
   ```

   输出的 JSON 数组就是本轮语义检查的问题列表（已按文件、置信度、严重程度排好并去重），进入 B2。

### B2: Generate semantic check report

Generate filename: `reports/{YYYYMMDD}-{HHMMSS}-{SCOPE}-semantic.md`（`SCOPE` 沿用 Step 0.5 的取值；日期时间使用 `Asia/Shanghai`）

Parse the JSON array merged in B1 第 5 步. Group issues by `file`. Within each file, sort by **`confidence` first (high → medium → low)**, then severity (错误 → 警告 → 建议). Remove exact duplicates.

Count totals: E = error count, W = warning count, S = suggestion count.
Also count by confidence: H = high, M = medium, L = low.

Maintain a session-local counter `NEXT_SEMANTIC_ISSUE_ID`, initialized to `1` for the first B2 report in this validation run. Assign every semantic issue a globally unique numeric ID as you write the report. If B5 later appends regression issues, continue numbering from the current counter value; do not reset.

**Confidence icon map**: `high` → 🔴、`medium` → 🟡、`low` → 🔵（附在 severity icon 之前）。

Write the report:

```markdown
## 语义检查结果 {YYYY-MM-DD HH:MM} Asia/Shanghai
（共 {N} 个问题：{E} 错误 / {W} 警告 / {S} 建议；置信度 {H} 高 / {M} 中 / {L} 低）

---
处理说明：在对话里选要处理的问题（全部、高置信、编号或跳过）。
高置信 = 只处理 🔴 high 档问题（规则性强的明确问题）；低置信 🔵 通常需人工判断，默认折叠。
---

### {filename1}
1. 🔴 ❌ [{category}] {message}
   依据：{evidence}
2. 🟡 ⚠️ [{category}] {message}
   依据：{evidence}

### {filename2}
3. 🔵 💡 [{category}] {message}
   依据：{evidence}
```

If the agent didn't provide `confidence`, default to `medium` and render 🟡. If no `evidence`, omit the 依据 line.

`UNFINISHED_CHECKS`（B1 第 4 步）非空时，在标题行下面加一行 `未完成的检查项：{UNFINISHED_CHECKS，部分之间用「；」分隔}`。

**只查改动模式**：先写 `origin = change` 的问题（按上面的文件、置信度规则排列），再写一节原有问题，编号接着往下排：

```markdown
### 原有问题（与本次改动无关，不影响本次结论）
{id}. 🟡 ⚠️ [{category}] {file} → {message}
```

没有 `origin` 的问题按 `change` 处理。标题行的计数只算 `change` 问题，另起一行写「另有 {K} 个原有问题，见文末」。

If no issues (N=0): write "（无问题，本体文档通过全部语义检查。）"；`UNFINISHED_CHECKS` 非空时改写「（已做的检查项没有发现问题。）」，并告诉用户哪些检查项没做完 — skip to B5.

### B3: Prompt user for review

先 `python3 .claude/scripts/state_helper.py acquire validate confirm`，再 show a numbered summary list in the terminal, **grouped by confidence tier**（用户选好后 `acquire validate start`）. The numbering here must exactly match the report.

Example (low-confidence group is default-collapsed — show only header + count, expand only on request):

```
语义检查完成，共 {N} 个问题（🔴{H} 高 / 🟡{M} 中 / 🔵{L} 低）：

🔴 高置信（{H}）
1. ❌ [逻辑缺陷] operation/X.md → {message}

🟡 中置信（{M}）
2. ⚠️ [描述歧义] objects/Y.md → {message}

🔵 低置信（{L}）— 默认折叠，回复 **展开低置信** 查看
（已保存至报告文件）
```

`UNFINISHED_CHECKS` 非空时，在列表后加一行「未完成的检查项：{UNFINISHED_CHECKS}，这几项这次没查」。

Then ask with a confirm card（INTERACTION_CONVENTIONS 第 2 条）「语义检查的 {N} 个问题要处理哪些？」，选项 **全部** / **高置信** / **跳过**；
展开低置信、只处理几条编号让用户在对话里说。用户在对话里直接打这些关键词也照样推进。

**只查改动模式**：终端只列 `change` 问题（仍按置信度分组）；原有问题只写一行「另有 {K} 个原有问题与本次改动无关，已记入报告；需要一并处理请回复编号」。

Parse the user's selection:
- **全部**: set `B4_SELECTED` = all semantic issue IDs in this round（只查改动模式下只含 `change` 问题；原有问题需回复编号才处理）
- **高置信**: set `B4_SELECTED` = IDs whose `confidence == "high"`; if none, tell user "本轮无高置信问题，请改用其他选项。"
- **展开低置信**: expand and re-print the low-confidence block, then ask again for a selection
- **输入编号如 1,3**: parse comma-separated IDs; ignore spaces; if any ID does not exist, warn the user and only keep valid IDs
- **跳过**: set `B4_SELECTED` = empty, add the issue IDs listed this round to `B4_SKIPPED`, and proceed to B6

Initialize session-local state for Phase B:
- `B4_MODIFIED`: stems of files actually modified during semantic fixes; start empty when Phase B begins and append on each actual file change
- `B4_FIXED`: issue IDs successfully fixed in this report; start empty when Phase B begins
- `B4_SKIPPED`: issue IDs explicitly skipped after review; start empty when Phase B begins

Per selection round before entering B4:
- `B4_SELECTED`: the selected issue IDs for this round

### B4: Apply fixes

If `B4_SELECTED` is empty, do not enter the fix loop; proceed directly to B6.

每轮修复开始前先 `git add ontology/` 暂存当前内容（不提交），让 B5 能单独取出本轮修复的改动。

#### B4a: Bulk-apply via semantic-fix-agent（默认路径）

Read `.claude/agents/semantic-fix-agent.md`. Dispatch it as a subagent with:

```
{若 KNOWLEDGE_BLOCK 非空：原样粘贴 KNOWLEDGE_BLOCK 在此处，紧跟一行分隔符 ---}

SEMANTIC_ISSUES = {把 B1 合并出的 issue 列表过滤为 B4_SELECTED 子集后的 JSON 数组，每条带上 B2 分配的 issue_id}
ONTOLOGY_ROOT = ontology/
```

Wait for it to return a JSON array of apply results。把返回结果按 `issue_id` 索引为 `APPLY_RESULTS`。

> ⚠️ agent 已经把 Edit 落盘了——主命令不要再重复 Read+Edit 同一文件。后续操作只走 `APPLY_RESULTS` 摘要、`git diff`、`git checkout` 三条路径。

**完整性兜底**（防 agent 漏报致 issue 静默丢失——HARD CONTRACT 失效时的最后一道防线）：

读到 `APPLY_RESULTS` 后，对每个出现在 `B4_SELECTED` 但未出现在 `APPLY_RESULTS` 的 `issue_id`，补一条 `{issue_id, applied: false, reason: "agent_truncated_output", proposed_change: "（agent 未返回该条结果，请人工接续）"}`。这些条目走原有 B4c 的 `applied:false` → B4d 人工接续路径（"确认"回复也会把它们转入 B4d，不会直接进 B5），summary 表显示为 `⚠ agent 漏报`。

> 不在这里 normalize `status:"fixed"` → `applied:true`：HARD CONTRACT 已明确禁字段名漂移，悄悄兜底等于把违规当合规，下次会复发。schema 违规 → 当作 missing 补 false 即可。

#### B4b: Show summary table

```
已批量应用 {APPLIED_COUNT} 条语义修复，{SKIPPED_COUNT} 条未应用（需人工判断）：

| # | 编号 | 文件 | 问题 | 处理结果 |
|---|------|------|------|----------|
| 1 | 3    | operation/创建订单.md | {message 摘要} | ✅ 已应用：{change_summary} |
| 2 | 7    | operation/支付流程.md | {message 摘要} | ⏸ 未应用（{reason}）：{proposed_change} |
| 3 | ...  | ... | ... | ... |

```

展示这张表后不等回复，按 B4c 往下走（用户已经选过要修哪些；每轮结束自动存成版本，要撤销可以「撤回这一轮」，或在对话里说要撤哪条）。

#### B4c: 处理结果

- 默认（不等回复）:
  - 对 `APPLY_RESULTS` 中 `applied: true` 的每条，把 `issue_id` 加入 `B4_FIXED`，把 `files_changed` 的 stems 加入 `B4_MODIFIED`。
  - 对 `applied: false` 的条目（agent 主动放弃的）→ 进入 B4d 逐条人工处理。
- 用户之后在对话里主动要求 **逐条复审**:
  - 对每条 `applied: true` 走子流程：
    ```
    [复审 {n}/{APPLIED_COUNT}]
    编号：{issue_id}  文件：{file}
    问题：{message}
    实际改动：{change_summary}
    ```
    展示 `git diff -- {file}` 的关键片段，让用户回 **保留** / **撤销** / **我来重写**（等回复前 `acquire validate confirm`，回复后切回 `start`）。
    - 保留: `issue_id` 进 `B4_FIXED`，files 进 `B4_MODIFIED`。
    - 撤销: 用 `git checkout -- {files_changed}` 还原；`issue_id` 进 `B4_SKIPPED`。
    - 我来重写: 撤销 agent 的改动，进入 B4d 让用户为该条提供方案。
  - 复审结束后，未 applied 的条目同样进入 B4d。
- 用户主动要求 **全部撤销**:
  - 收集所有 `applied: true` 条目的 `files_changed`，`git checkout -- {files...}` 还原。
  - 把 `APPLY_RESULTS` 标记为全部未处理，转入 B4d 让用户对全部 `B4_SELECTED` 逐条人工处理。

#### B4d: 逐条人工处理（fallback）

对需要逐条处理的 issue（来自 B4c 的 "我来重写" / `applied:false` / "全部撤销"），按原流程一条条交互（每条等回复前 `acquire validate confirm`，回复后切回 `start`）：

```
[语义修改 {n}/{TOTAL}]
编号：{issue_id}
文件：{file}
问题：{message}

修改方案：
{specific proposed change}  ← 若 APPLY_RESULTS 给了 proposed_change，优先用它做起点

[确认修改] [我来提供方案] [跳过]
```

Per issue:
- **确认修改**: 主命令直接 Read+Edit 应用方案，`issue_id` 进 `B4_FIXED`，files 进 `B4_MODIFIED`。
- **我来提供方案**: 按用户描述 Edit 应用，`issue_id` 进 `B4_FIXED`，files 进 `B4_MODIFIED`。
- **跳过**: `issue_id` 进 `B4_SKIPPED`。

B4d 处理完毕后进入 B5。

### B5: 回归语义检查

If B4_MODIFIED is empty (no files were modified), skip this step and proceed to B6.

Re-dispatch the semantic-check-agent in **regression mode**（全量、增量、只查改动三种模式通用；不拆部分）。先另建一个问题目录：

```bash
python3 .claude/scripts/semantic_issues.py init --mode regression
```

输出的路径记为本轮的 `ISSUES_DIR`。派发提示按工作区规则「子任务派发」第一行要求完整阅读 agent 文件，之后附：

```
PART: all
ISSUES_DIR: {本轮 ISSUES_DIR}
TARGET_FILES: {B4_MODIFIED 的 stem，空格分隔}
REGRESSION_ISSUES: |
{本轮修复的问题，每条一行：issue_id、file、message 原文}
CHANGE_DIFF_CMD: git diff -U3 -- {B4_MODIFIED 的路径，空格分隔}
```

`CHANGE_DIFF_CMD` 由子任务自己运行，拿到的是未暂存部分（B4 开始前已暂存，即本轮修复的改动）的原文；不要自己运行后把输出抄进派发提示。不传 RELATED_FILES，也不要在派发提示里另加检查范围。子任务回来后 `python3 .claude/scripts/semantic_issues.py merge --dir {本轮 ISSUES_DIR}`，输出的 JSON 数组就是回归结果。

Filter the new results: remove any issue whose `file` + `message` combination was already present in the B1 results. Only issues not previously seen count as regressions introduced by the fixes.

回归检查只做这一轮：查出的新问题写进报告留待处理，这一轮不再修、不再复查，也不问用户。

**If regressions found:**

Append them to `reports/{semantic-filename}` under a new section:

```markdown
### 回归检查发现的新问题（{YYYY-MM-DD HH:MM} Asia/Shanghai）
{next_id}. ❌ [{category}] {message}
{next_id+1}. ⚠️ [{category}] {message}
```

Tell the user "语义修复后发现 {N} 个新问题（可能由本次修改引入），已追加至报告，留待处理。"，用和 B3 相同的编号格式列出这些问题（编号与报告里追加的一致），然后进入 B6（这些编号行保持原样，算待处理）。

**If no regressions:** Tell user "回归检查通过，未发现新问题。" and proceed to B6.

---

### B6: Update semantic report and commit

Update `reports/{semantic-filename}`:
- Issue IDs in `B4_FIXED` → replace the numbered prefix with `✅`
- Issue IDs in `B4_SKIPPED` → replace the numbered prefix with `⏭️`
- Issue IDs never selected for processing → keep their original numbered lines unchanged

Example:

```markdown
✅ ❌ [逻辑缺陷] {message}
⏭️ ⚠️ [描述歧义] {message}
3. 💡 [关联不合理] {message}
```

```bash
git add ontology/ reports/
git commit -m "fix: address semantic check issues"
```

**BFO 参考提醒（可选）**：按 `.claude/reference/BFO_MODELING_GUIDE.md` §10 评估：
- 扫描本次 semantic 报告中所有 issue 的 `category`，与 §10.2「校验报告 issue category 关键词」做子串匹配，得到 `matched_categories`（去重，最多取 5 个）。
- 命中 → 在 "**校验全部完成**" 文案前**单独一行**按 §10.3「校验报告触发形式」输出提示。本命令本次执行只触发一次。
- 未命中 → 不输出。

提交后先 `python3 .claude/scripts/state_helper.py release validate` 与 `update-progress validate_last_run {timestamp}`，再写收尾小结。

**Operation 范式 / 断言驱动测试参考提醒（可选）**：按 `.claude/reference/OPERATION_PARADIGM_GUIDE.md` §七「校验完成或 trace 场景确认前」与 `.claude/reference/ASSERTION_DRIVEN_TESTING_GUIDE.md` §十一「校验完成后」输出提示；若用户本会话已表达"不需要 / 知道了 / 已判别 / 已按断言型补充"，本次不再提示。本命令本次执行只触发一次。

Tell user:
"💡 可选参考：现有校验/追踪默认更适合 SOP 流程型；若本次 Operation 包含规则引擎、红线拦截、连续阈值、多判定器组合或审计链，可先对照 Operation 范式。
可参阅 `.claude/reference/OPERATION_PARADIGM_GUIDE.md` §三 / §四核对范式；若判定为断言驱动型或混合型，再参阅 `.claude/reference/ASSERTION_DRIVEN_TESTING_GUIDE.md` §六 / §七，人工核对审计链贯通、规则不冲突、写入器前置审计与边界值口径。无需中断当前流程。

**校验全部完成。**{两三句话：修了哪些、跳过和没处理的还剩几条要用户定}"

不列下一步菜单（界面的建议条会给出）。用户之后在对话里说「生成示例数据」「场景模拟」「交互模拟」「整体视图」「扫描脚本候选」「生成脚本」「提取对象」「提取操作」「追加资料分析」「校验」（和界面按钮同名），按下面的分支做；「校验」从本命令 Step 0 重新开始。

> 锁已在小结前释放；下面分支里的 release 再调一次无害。

**If user replies 生成示例数据:** release validate，然后 read `.claude/commands/ontology-mock.md`（场景模拟能力；文件不在说明本项目没启用，告诉用户去项目设置启用） and follow its instructions.

**If user replies 场景模拟:** ask "要追踪哪个逻辑（如「创建订单」）？或回复 **全部** 追踪所有逻辑：". Then release validate 并 read `.claude/commands/ontology-trace.md`（场景模拟能力；文件不在说明本项目没启用，告诉用户去项目设置启用） and follow its instructions with `ontology/operation/{用户说的逻辑}.md`（用户回复 **全部** 时传 `全部`）.

**If user replies 交互模拟:** ask "要交互模拟哪个逻辑（如「创建订单」）？或回复 **选择** 让我列出所有逻辑：". Then release validate 并 read `.claude/commands/ontology-trace.md`（场景模拟能力；文件不在说明本项目没启用，告诉用户去项目设置启用） and follow it with `交互 ontology/operation/{用户说的逻辑}.md`（如果用户回复 **选择**，传 `交互`）。

**If user replies 整体视图:** release validate，然后 read `.claude/commands/ontology-overview.md` and follow its instructions.

**If user replies 扫描脚本候选:** release validate，然后 read `.claude/commands/ontology-codegen-scan.md`（代码生成与求解能力；文件不在说明本项目没启用，告诉用户去项目设置启用） and follow its instructions.

**If user replies 生成脚本:** release validate，然后 read `.claude/commands/ontology-codegen.md`（代码生成与求解能力；文件不在说明本项目没启用，告诉用户去项目设置启用） and follow its instructions.

**If user replies 提取对象:** release validate，然后 Read `.claude/commands/ontology-extract-object.md` and follow its instructions with these overrides: MODE = incremental; collect EXISTING_FILES from current `ontology/object/` contents before dispatching the agent. After the agent returns its summary, say "对象已增量提取，要重新校验可以说「校验」。" — 用户说校验时 restart from Step 1 of this command.

**If user replies 提取操作:** release validate，然后 Read `.claude/commands/ontology-extract-operation.md` and follow its instructions with these overrides: MODE = incremental; collect EXISTING_FILES from current `ontology/operation/` contents before dispatching the agent. After the agent returns its summary, say "操作已增量提取，要重新校验可以说「校验」。" — 用户说校验时 restart from Step 1 of this command.

**If user replies 追加资料分析:** Ask: "请描述要补充的分析方向（例如：「XX子流程的逻辑细节」「YY对象的状态转换」「某个引用断裂的缺失实体」）：". Then release validate 并 read `.claude/commands/ontology-research.md` and follow it using **追加资料分析（append）mode** with the user's direction as FOCUS_DIRECTIONS; **skip research's Step 5 next-step prompt** (do not ask about running flowchart). After research completes, ask: "资料分析完成。要增量提取哪一类？回复 **对象**、**逻辑** 或 **否**。" — if object, follow `/ontology-extract-object` incremental; if operation, follow `/ontology-extract-operation` incremental; then say "增量提取完成，要重新校验可以说「校验」。" — 用户说校验时 restart from Step 1.

**If user replies 完成:** `python3 .claude/scripts/state_helper.py release validate`，Stop.
