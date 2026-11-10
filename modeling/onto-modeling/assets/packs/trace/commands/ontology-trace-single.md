Trace one operation file with concrete scenarios and mock data. Loaded by `.claude/commands/ontology-trace.md` after Step 0.

## Mode Inputs

- `TRACE_MODE=single`
- `OPERATION_PATH`: the normalized operation file path from the trace entrypoint.

## Step 1: Read operation document

Read the operation document. Extract:
- `# 逻辑描述` section (the operation steps)
- `# 输入` table (required inputs)
- `# 与object的关联` section (referenced objects)
- All decision points (判断/若) and their branches
- All sub-operation calls (调用[[X]]逻辑文档)

If `# 逻辑描述` is empty or missing, tell user，然后 `python3 .claude/scripts/state_helper.py release trace`，stop.

After reading the operation, derive:
- `OPERATION_NAME` = basename without `.md`
- `REFERENCED_OBJECTS` = object names from `# 与object的关联`

Load business knowledge for this mode:

```bash
SCOPE_ARGS=(--scope global --scope "operation:$OPERATION_NAME")
for obj in $REFERENCED_OBJECTS; do
    SCOPE_ARGS+=(--scope "object:$obj")
done
KNOWLEDGE_BLOCK=$(python3 .claude/scripts/knowledge_helper.py load "${SCOPE_ARGS[@]}")
```

If `KNOWLEDGE_BLOCK` equals `（暂无相关业务知识）`, set it to an empty string before dispatching agents.

## Step 2: Check for mock data

List `ontology/data-mock/`. Extract the object names referenced by this operation from the `与object的关联` section.

**If mock data is missing for any referenced object:**
不问用户，说一句「{对象} 还没有测试数据，先补上」，按 `.claude/commands/ontology-mock.md` 的「由追踪带起」方式只给缺的对象生成，完成后进入 Step 3。
用户在 `$ARGUMENTS` 或这条消息里说了不要测试数据时才跳过：set MOCK_DATA_DIR = empty, proceed to Step 3.

**If all referenced objects have mock data:** set MOCK_DATA_DIR = `ontology/data-mock/`

## Step 2.5: Check for saved testcases

Derive `OPERATION_NAME` from `OPERATION_PATH`（取 basename 去掉 `.md`）。设 `TESTCASE_PATH = ontology/testcase/{OPERATION_NAME}.md`。

**If `TESTCASE_PATH` exists:**

使用 `python3 .claude/scripts/trace_inputs.py "{TESTCASE_PATH}" --output "tmp/trace-inputs.json"` 解析，禁止另写临时正则转换器。非零退出立即记录输入错误，不进入推演。解析规则（与 `.claude/templates/testcase-template.md` 对齐）：
- H1 首行 `# 测试场景：{OPERATION_NAME}` 验证逻辑名一致（不一致时先 `acquire trace confirm`，提示用户并让其确认是否继续；回复后切回 `start`）。
- 每个 `## 场景 N：{name}` 段落提取 `输入`、`预期路径`、`数据来源` 三项，构造 `SAVED_SCENARIOS` 列表。

`python3 .claude/scripts/state_helper.py acquire trace confirm`，then show user（回复后 `acquire trace start`）:

```
找到已保存的 {N} 个测试场景（{YYYY-MM-DD} 更新）：
{列出每个场景名称}

- 回复 **复用** → 直接使用保存的场景，跳过自动生成
- 回复 **补充** → 保留已有场景，自动补充未覆盖的判断分支
- 回复 **重新生成** → 丢弃已保存场景，完全重新生成
```

Handle the reply:
- **复用**: set `SCENARIOS = SAVED_SCENARIOS`; set `REUSE_MODE = true`; **jump to Step 4**（跳过 Step 3 的自动生成）。
- **补充**: set `EXISTING_SCENARIOS = SAVED_SCENARIOS`; proceed to Step 3 in supplement mode（只为未被已有场景覆盖的分支生成新场景；最终 `SCENARIOS = EXISTING_SCENARIOS + 新增场景`）。
- **重新生成**: 忽略 `SAVED_SCENARIOS`，正常进入 Step 3。

**If `TESTCASE_PATH` does not exist:** 正常进入 Step 3。

## Step 3: Auto-generate scenarios

Based on the operation, generate test scenarios. Each scenario is a set of concrete input values that exercises a specific path:

**Must include:**
- **正常路径**: All conditions satisfied, reaches successful end
- **Each decision branch**: At least one scenario per branch of every decision node
- **异常路径**: Inputs that trigger error/termination nodes (if any)
- **边界情况**: Edge values (e.g., quantity=0, empty string, null-like states)

**If mock data is available:** Use actual values from the mock data table when constructing scenario inputs. Boundary or exception scenario values should be custom scenario inputs, not mock-data sections.

Each scenario format:

```
场景 N：{场景名称}
输入：
  - {参数1} = {具体值}
  - {参数2} = {具体值}
预期路径：{正常完成 / 异常终止于步骤N / 走分支X}
数据来源：{正常数据第N行 / 自定义}
```

Aim for 3–6 scenarios depending on operation complexity.

**Supplement mode（仅当 Step 2.5 选 补充）**：比对 `EXISTING_SCENARIOS` 已经覆盖的分支，仅针对未覆盖的判断节点或边界补充新场景。最终 `SCENARIOS = EXISTING_SCENARIOS + 新增场景`。若已有场景已覆盖所有分支，先 `acquire trace confirm`，直接告知用户"已有场景已覆盖全部分支，无需补充"，并让用户在"直接执行追踪"或"补一个自定义场景"中选择（回复后切回 `start`）。

## Step 4: 设计阶段确认（设计与执行的硬隔离）

> 设计阶段核对具体缺失分支，不展示覆盖率数字或百分比；执行阶段才汇报推演结果。本步是设计阶段的硬门——未确认不进入执行。

Step 1 已解析逻辑中所有判断节点的分支列表，记总分支数为 `TOTAL_BRANCHES`；从 `SCENARIOS` 的「预期路径」字段汇总每个场景命中的分支，记去重后的覆盖分支数为 `COVERED_BRANCHES`。

计算：
- 内部保留分支集合用于缺项检查，不展示覆盖率统计。
- 未覆盖分支清单 = `TOTAL_BRANCHES - COVERED_BRANCHES` 中具体未被命中的 (判断节点, 分支) 对

**Operation 范式 / 断言驱动测试参考提醒（可选）**：在展示场景并请求确认前，按 `.claude/reference/OPERATION_PARADIGM_GUIDE.md` §七「校验完成或 trace 场景确认前」与 `.claude/reference/ASSERTION_DRIVEN_TESTING_GUIDE.md` §十一「trace 场景确认前」输出提示；若用户本会话已表达"不需要 / 知道了 / 已判别 / 已按断言型补充"，本次不再提示。本命令本次执行只触发一次。

`python3 .claude/scripts/state_helper.py acquire trace confirm`，then show all generated scenarios and ask:

"以上是 {N} 个测试场景。

💡 可选参考：现有校验/追踪默认更适合 SOP 流程型；若本次 Operation 包含规则引擎、红线拦截、连续阈值、多判定器组合或审计链，可先对照 Operation 范式。
可参阅 `.claude/reference/OPERATION_PARADIGM_GUIDE.md` §三 / §四核对范式；若判定为断言驱动型或混合型，再参阅 `.claude/reference/ASSERTION_DRIVEN_TESTING_GUIDE.md` §三 / §八，补充规则覆盖、约束边界、退化逻辑和跳过审计的负面 Case。无需中断当前流程。

**待补场景：**
{有缺项时列出具体未覆盖分支，否则说明本次检查未发现缺项：}
未覆盖：
- {判断节点 A} 的「{分支 X}」
- {判断节点 B} 的「{分支 Y}」

⚠️ 未发现缺项不代表逻辑正确，仍需逐步骤推演。

- 回复 **确认** → 进入执行阶段（推演）
- 回复 **添加 {场景描述}** → 补充场景（建议优先补未覆盖分支）
- 回复 **删除 N** → 删除第 N 个场景
- 回复 **修改 N {内容}** → 修改第 N 个场景"

Repeat until user confirms（确认后 `acquire trace start`）。**未确认不得 dispatch trace-agent**。

## Step 4.5: Save testcases for reuse

User 确认后（无论是 复用、补充 还是 重新生成 路径汇合到这里），把最终的 `SCENARIOS` 写入 `TESTCASE_PATH`：

1. 确保 `ontology/testcase/` 目录存在（用 `mkdir -p` 对应的文件系统操作）。
2. 读取 `.claude/templates/testcase-template.md` 作为格式参考，按如下 shape 生成内容：

   ```markdown
   # 测试场景：{OPERATION_NAME}

   > 由 /ontology-trace 生成并由用户确认。再次运行 trace 会优先读取本文件以复用场景。
   > 对应逻辑：[[{OPERATION_NAME}]]
   > 最近一次更新：{YYYY-MM-DD}

   ## 场景 1：{name}

   - **输入**：
     - `{参数}` = `{值}`
   - **预期路径**：{path}
   - **数据来源**：{source}

   ## 场景 2：...
   ```

3. Write to `TESTCASE_PATH`（覆盖既有文件）。
4. Commit 这份场景文件（独立一次提交，方便回溯 "下次追踪用什么数据"）：

   ```bash
   git add ontology/testcase/{OPERATION_NAME}.md
   git commit -m "trace: {OPERATION_NAME} testcases"
   ```

5. Tell user: "场景已保存，下次追踪 **{OPERATION_NAME}** 会自动复用。"

> 特例：若 `REUSE_MODE = true` 且文件内容未发生变化（即用户选 **复用** 后没做任何修改），跳过写入与 commit。

## Step 5: Dispatch trace-agent

保存确认稿后重新运行统一转换器生成 `tmp/trace-inputs.json`，随后执行：
`python3 .claude/scripts/trace_inputs.py "{TESTCASE_PATH}" --actual tmp/trace-inputs.json`。
只有退出 0 才能派遣。交接传路径，不重新手抄/筛选 JSON 字段；记录 source_sha256。

Read `.claude/agents/trace-agent.md`. Dispatch it as a subagent with:

{若 KNOWLEDGE_BLOCK 非空：原样粘贴 KNOWLEDGE_BLOCK 在此处，紧跟一行分隔符 ---}

- OPERATION_PATH = the operation file path
- TESTCASE_PATH = confirmed testcase file
- INPUTS_PATH = `tmp/trace-inputs.json`（包含修订与完整 scenarios，不得转抄）
- OBJECTS_DIR = `ontology/object/`
- OPERATION_DIR = `ontology/operation/`
- MOCK_DATA_DIR = `ontology/data-mock/` (or empty if skipped)

---

请阅读并遵循 `.claude/BUSINESS_KNOWLEDGE_SNIPPET.md` 的「业务知识识别守则」。

Wait for it to return a JSON array of trace results. 原样保存到 `tmp/trace-results.json`，报告前执行
`python3 .claude/scripts/trace_inputs.py "{TESTCASE_PATH}" --actual tmp/trace-inputs.json --results tmp/trace-results.json`。
对场景 ID、实际初始输入、类型和修订全部核对，失败不得提交通过报告。
核对通过后执行 `python3 .claude/scripts/trace_index.py record "{TESTCASE_PATH}" --results tmp/trace-results.json`，
把每个场景的推演结论写进场景索引（`ext/trace/index/scenarios.jsonl`，「场景模拟」页读它），与报告一起提交。

## Step 6: Save report

Generate filename: `reports/{YYYYMMDD}-{HHMMSS}-trace.md` using `Asia/Shanghai` time.

汇总指标：
- 保留 Step 4 的具体缺失分支列表，不显示覆盖率数字或统计。
- **推演通过率** = `PASS_COUNT / TOTAL_SCENARIOS`，其中 `PASS_COUNT` 仅统计 `reached_end == true` 且 `issues == []` 的场景

Write the full trace results to the report file。报告**必须**包含以下两个独立小节，不可只写一个综合数字：

```markdown
# 追踪报告（{timestamp} Asia/Shanghai）

## 场景缺项检查
- 未覆盖分支：{若有则列出，否则填"无"}

## 推演通过率
- 场景总数：{TOTAL_SCENARIOS}
- 推演通过数：{PASS_COUNT}
- 推演通过率：{pct}%
- 待人工确认数：{halted 且未归入通过/失败的场景数，若无则填 0}

## 问题分类汇总
{按 issue type 计数}

## 各场景详细步骤
{逐场景的 step 表 + issues}
```

## Step 7: Present summary

先 `python3 .claude/scripts/state_helper.py release trace` 与 `update-progress`，再 show only the summary in terminal:

"**追踪总结：**

待补场景：{具体缺失分支或本次未发现缺项}
推演通过率：{PASS_COUNT}/{TOTAL_SCENARIOS}（{pct}%）— 衡量场景实际推演是否通过

⚠️ 未发现缺项不代表逻辑正确；推演通过率低意味着逻辑或数据有缺陷。

- 发现问题：{issue_count}
  - 分支缺失：{n}
  - 死路径：{n}
  - 状态矛盾：{n}
  - 回滚缺失：{n}
  - 子流程输入不满足：{n}
  - 逻辑模糊：{n}
  - 引用断裂：{n}
  - 隐式兜底：{n}
  - 规则定义与执行脱节：{n}

详细步骤见追踪报告；测试场景已保存，下次追踪自动复用。
{有问题时补一句：哪些问题需要用户定}"

不列下一步菜单（界面的建议条会给出）。用户之后在对话里说「修复」（逐条提出修改方案，同校验流程）、「再追踪」（改场景后重新执行）或「补充资料分析」，按下面的分支做。

**If user replies 补充资料分析:** Ask: "请描述要补充的分析方向（例如：「XX子流程的详细逻辑」「YY业务规则的边界条件」）：". Then `python3 .claude/scripts/state_helper.py release trace`，再 read `.claude/commands/ontology-research.md` and follow it using **追加资料分析（append）mode** with the user's direction as FOCUS_DIRECTIONS; **skip research's Step 5 next-step prompt**. After research completes, ask: "资料分析完成。要增量提取哪一类？回复 **对象**、**逻辑** 或 **否**。" — if object, read `.claude/commands/ontology-extract-object.md` and follow it using **incremental mode**; if operation, read `.claude/commands/ontology-extract-operation.md` and follow it using **incremental mode**. After extract summary, ask: "是否重新追踪？回复 **是** 或 **否**。" — if yes, restart this command from Step 1（restart 时新一轮 Step 0 会重新 acquire）.

**If user replies 完成:** `python3 .claude/scripts/state_helper.py release trace`，Stop.

**If user replies 修复 / 再追踪:** 仍在本命令内继续：先 `acquire trace start`，处理完再按 Step 7 release 并小结。
