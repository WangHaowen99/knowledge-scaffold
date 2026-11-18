Trace all operation files in batch mode. Loaded by `.claude/commands/ontology-trace.md` after Step 0.

## Mode Inputs

- `TRACE_MODE=batch`
- 场景模拟页「场景模拟」按钮发来的 `scope=all` 就是全量批量推演（同不带参数）。

## <a id="batch-mode"></a>Batch Mode

### Step B1: Collect all operation files

List `.md` files in `ontology/operation/`. Let `OPERATION_FILES` = this list.

If empty: "未找到任何逻辑文档。" 然后 `python3 .claude/scripts/state_helper.py release trace`，Stop.

不问用户（点了「场景模拟」就是要做），说一句: "对 {N} 个逻辑做批量追踪：{逻辑名}。

💡 可选参考：现有批量追踪默认更适合 SOP 流程型；若本批 Operation 包含规则引擎、红线拦截、连续阈值、多判定器组合或审计链，可先对照 Operation 范式。
可参阅 `.claude/reference/OPERATION_PARADIGM_GUIDE.md` §三 / §四核对范式；若判定为断言驱动型或混合型，再参阅 `.claude/reference/ASSERTION_DRIVEN_TESTING_GUIDE.md` §三 / §八，补充规则覆盖、约束边界、退化逻辑和跳过审计的负面 Case。无需中断当前流程。"

上述参考提醒按 `.claude/reference/OPERATION_PARADIGM_GUIDE.md` §七与 `.claude/reference/ASSERTION_DRIVEN_TESTING_GUIDE.md` §十一执行；若用户本会话已表达"不需要 / 知道了 / 已判别 / 已按断言型补充"，本次不再提示。本命令本次执行只触发一次。

然后直接进入 Step B2。

### Step B2: Pre-check mock data (once)

Collect all objects referenced by any operation in `OPERATION_FILES` (from each doc's `与object的关联` section). Let `ALL_REF_OBJECTS` = union of these.

Load business knowledge for batch mode after `OPERATION_FILES` and `ALL_REF_OBJECTS` are known:

```bash
SCOPE_ARGS=(--scope global)
for operation_path in $OPERATION_FILES; do
    operation_name=$(basename "$operation_path" .md)
    SCOPE_ARGS+=(--scope "operation:$operation_name")
done
for obj in $ALL_REF_OBJECTS; do
    SCOPE_ARGS+=(--scope "object:$obj")
done
KNOWLEDGE_BLOCK=$(python3 .claude/scripts/knowledge_helper.py load "${SCOPE_ARGS[@]}")
```

If `KNOWLEDGE_BLOCK` equals `（暂无相关业务知识）`, set it to an empty string before dispatching agents.

List `ontology/data-mock/`. Compute `MISSING_MOCK` = objects in `ALL_REF_OBJECTS` without corresponding mock files.

If `MISSING_MOCK` not empty: 不问用户，说一句「{list} 还没有测试数据，先补上」，按 `.claude/commands/ontology-mock.md` 的「由追踪带起」方式只给这些对象生成，
然后继续。用户在 `$ARGUMENTS` 或这条消息里说了不要测试数据时才跳过：set `MOCK_DATA_DIR = empty`。

### Step B2.5: Ask about saved testcase reuse

检查 `ontology/testcase/` 下与 `OPERATION_FILES` 对应的已保存场景文件。设 `WITH_SAVED` 为已有保存文件的逻辑集合，`WITHOUT_SAVED` 为其余。

If `WITH_SAVED` 非空，`acquire trace confirm` 后询问用户（回复后 `acquire trace start`）：

```
以下 {k} 个逻辑已有保存的追踪场景：
{list}

- 回复 **复用** → 已有保存场景的逻辑使用保存场景，其他的自动生成
- 回复 **全部重新生成** → 所有逻辑重新生成场景（覆盖已有保存）
```

- **复用**（默认推荐）: `BATCH_REUSE = true`
- **全部重新生成**: `BATCH_REUSE = false`

如果 `WITH_SAVED` 为空，直接设 `BATCH_REUSE = false`（没东西可复用）并继续。

### Step B3: Build JOBS list and one-shot dispatch

固定本次 batch 的时间戳 `BATCH_TS = {YYYYMMDD}-{HHMMSS}`，使用 `Asia/Shanghai` 时间；每个 per-operation 报告与汇总报告共用同一个 `BATCH_TS`，便于关联回溯。

#### B3.1 组装 OPERATION_JOBS

只在主命令做轻量目录扫描，**不要 Read 任何 operation 文件**（agent 会自己 Read）：

For each `OPERATION_PATH` in `OPERATION_FILES`:
- `OPERATION_NAME` = basename without `.md`
- `report_path` = `reports/{BATCH_TS}-trace-{OPERATION_NAME}.md`
- `testcase_path` = `ontology/testcase/{OPERATION_NAME}.md`
- Job entry：
  - 若 `BATCH_REUSE = true` 且 `testcase_path` 存在 → `{ operation_path, report_path, testcase_path }`
  - 否则 → `{ operation_path, report_path }`（agent 会自动生成场景并把 testcase 写到 `testcase_path`）

Append all entries into `OPERATION_JOBS`。

告诉用户："准备批量追踪 {N} 个逻辑（其中 {k} 个复用已保存场景，{N-k} 个自动生成场景），请稍候。"

#### B3.2 单次 dispatch

Read `.claude/agents/trace-agent.md`. Dispatch 一次，prompt 形如：

```
{若 KNOWLEDGE_BLOCK 非空：原样粘贴 KNOWLEDGE_BLOCK 在此处，紧跟一行分隔符 ---}

OPERATION_JOBS = {上面组装好的 JSON 数组}
OBJECTS_DIR = ontology/object/
OPERATION_DIR = ontology/operation/
MOCK_DATA_DIR = {Step B2 决议出的值，可能为空}

---

请阅读并遵循 `.claude/BUSINESS_KNOWLEDGE_SNIPPET.md` 的「业务知识识别守则」。
```

Wait for it to return a JSON array `JOB_SUMMARIES`（按 OPERATION_JOBS 顺序排列；每项含 `operation_name / status / report_path / branch_metrics / scenario_metrics / issues_by_type / issues / generated_testcase`）。**不要再 Read 详细报告文件**——主上下文只保留 summary。

汇总前，逐个已执行 job 使用该 job 的确认稿路径及 summary 返回的 inputs_path / results_path 运行
`python3 .claude/scripts/trace_inputs.py "{testcase_path}" --actual "{inputs_path}" --results "{results_path}"`。
回执缺失或非零退出，将该 job 标为 failed，并保留原因；不得把子任务自报通过数直接汇总。

#### B3.3 进度回放

agent 返回后，按 `JOB_SUMMARIES` 顺序打印一次性进度块（每行对应一个 job）：

```
[1/N] {operation_name} → {pass}/{total} 通过，{issue_count} 个问题  ({来源: 复用/自动生成})
[2/N] {operation_name} → 已跳过（缺逻辑描述）
...
```

进度块只为告知用户，不影响后续逻辑。

### Step B4: Save combined summary

每个 job 的结果核对通过后（`tmp/trace-results-{job序号}.json`），执行 `python3 .claude/scripts/trace_index.py record "{该 job 的 TESTCASE_PATH}" --results tmp/trace-results-{job序号}.json` 更新场景索引；核对失败的 job 不写索引。

页面「补齐过期场景」发来的消息带参数 `keys`（场景编号，形如 `<逻辑名>--<场景序号>`）：只推演这些场景所在的逻辑。

每个 per-operation 详细报告已由 trace-agent 在 Step B3 写入 `reports/{BATCH_TS}-trace-{OPERATION_NAME}.md`，本步**不再**由主命令 Write 这些详细报告——只用各 operation 的 summary JSON 写一份 batch 汇总。

B3 中由 agent 新写入 `ontology/testcase/*.md` 的文件集中 commit 一次（来源：`JOB_SUMMARIES` 中 `generated_testcase: true` 的条目）：

```bash
git add ontology/testcase/ ext/trace/index/
git commit -m "trace: batch testcases ({N} operations)"
```

（`{N}` = `JOB_SUMMARIES` 中 `generated_testcase: true` 的数量。若全为 false，跳过本次 commit。）

Also write a combined summary to `reports/{BATCH_TS}-trace-batch.md`：

从 JOB_SUMMARIES 取具体缺失分支与推演结果。branch_metrics 仅用于内部缺项检查，不对用户展示覆盖率、分母或百分比；推演通过率保留。

```markdown
# 批量追踪汇总（{timestamp} Asia/Shanghai）

## 总体统计
- 逻辑文件数：{N}
- 场景总数：{∑TOTAL_SCENARIOS}
- 平均推演通过率：{avg_pct}% — 衡量场景实际推演是否通过
- 发现问题总数：{total_issues}

> 注：未发现缺项不代表逻辑正确，仍需检查实际推演结果。

## 按逻辑文件

### {operation_name_1}
- 待补场景：{具体缺失分支或本次未发现缺项}
- 推演通过率：{p}/{n}（{pct}%）
- 问题概览：{分支缺失:x, 死路径:y, 隐式兜底:z, 规则定义与执行脱节:w, ...}
- 详细报告：`reports/{detail_filename}`

### {operation_name_2}
...

## 问题热点（按类型聚合）
- 分支缺失：{count}（分布在 {k} 个逻辑）
- 死路径：{count}（分布在 {k} 个逻辑）
- 隐式兜底：{count}（分布在 {k} 个逻辑）
- 规则定义与执行脱节：{count}（分布在 {k} 个逻辑）
- ...

## 建议优先处理
- {operation_name}：{问题数} 个问题（尤其是 {主要问题类型}）
```

### Step B5: Present summary

先 `python3 .claude/scripts/state_helper.py release trace` 与 `update-progress`，再 show only the combined summary in terminal:

"批量追踪完成。
- 已追踪：{N} 个逻辑，{scenario_total} 个场景
- 平均推演通过率：{pct}% — 衡量场景实际推演是否通过
- 发现问题：{issue_total} 个

⚠️ 未发现缺项不代表逻辑正确；推演通过率低意味着逻辑或数据有缺陷。

逐个逻辑的详细结果见批量追踪汇总报告。
新保存了 {M} 个逻辑的测试场景，下次追踪自动复用。（{M} = JOB_SUMMARIES 中 `generated_testcase: true` 的数量，若为 0 则省略本行）"

不列下一步菜单（界面的建议条会给出）。用户之后在对话里说「修复 {逻辑名}」「再追踪 {逻辑名}」「扫描脚本候选」「生成脚本」（后两个和界面按钮同名），按下面的分支做。

**If user replies 完成:** `python3 .claude/scripts/state_helper.py release trace`，Stop.
（**修复** / **再追踪** 仍在本命令内继续：先 `acquire trace start`，处理完再 release 并小结）

**If user replies 扫描脚本候选:** `python3 .claude/scripts/state_helper.py release trace`，然后 read `.claude/commands/ontology-codegen-scan.md` and follow its instructions.

**If user replies 生成脚本:** `python3 .claude/scripts/state_helper.py release trace`，然后 read `.claude/commands/ontology-codegen.md` and follow its instructions.
