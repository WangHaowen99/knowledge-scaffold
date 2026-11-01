Trace interactive walkthrough mode. Loaded by `.claude/commands/ontology-trace.md` after Step 0.

页面「交互走查」发来的消息带参数 `scenario`（场景编号，形如 `<逻辑名>--<场景序号>`）：直接对这个场景所在的逻辑、从这个场景开始走查。推演核对通过后同样执行 `python3 .claude/scripts/trace_index.py record` 更新场景索引。

## Mode Inputs

- `TRACE_MODE=interactive`
- `INTERACTIVE_TARGET`: remaining text after the trace entrypoint removed the trigger token (`交互` / `交互走查` / `对话走查` / `走查`). It may be empty, a operation path, or a scenario description.

## <a id="interactive-walkthrough-mode"></a>Interactive Walkthrough Mode

交互走查用于 **Operation 语义还不够确定、需要人逐个分支定方向** 的场景。它不替代普通 trace；它先帮助用户构造可复用 testcase / 正常 mock 数据，再回到普通 trace 执行推演。

### 适用与触发

显性告知用户：

```
已进入交互走查模式。

用法：
- `/ontology-trace 交互`：先列出逻辑，让你选择入口
- `/ontology-trace 交互 ontology/operation/创建订单.md`：直接走查指定逻辑
- 也可以直接描述业务场景，例如：`交互 1 个订单，库存不足，优惠券已过期`

规则：
- 每个分支点由你选择方向；我不会替你默认选分支
- 我根据你给的业务特征补具体输入、testcase 和必要的正常 mock 数据
- 边界/异常场景写入 testcase 的自定义输入，不写入 data-mock 的边界/异常分区

💡 可选参考：交互走查默认围绕分支选择展开；若本次 Operation 包含规则引擎、红线拦截、连续阈值、多判定器组合或审计链，可先对照 Operation 范式。
可参阅 `.claude/reference/OPERATION_PARADIGM_GUIDE.md` §三 / §四核对范式；若判定为断言驱动型或混合型，再参阅 `.claude/reference/ASSERTION_DRIVEN_TESTING_GUIDE.md` §三 / §八，补充规则覆盖、约束边界、退化逻辑和跳过审计的负面 Case。无需中断当前流程。
```

上述参考提醒按 `.claude/reference/OPERATION_PARADIGM_GUIDE.md` §七与 `.claude/reference/ASSERTION_DRIVEN_TESTING_GUIDE.md` §十一执行；若用户本会话已表达"不需要 / 知道了 / 已判别 / 已按断言型补充"，本次不再提示。本命令本次执行只触发一次。

### I1: Choose Entry

- If the optional target is a operation file path, set `OPERATION_PATH` to it.
- If the optional target is a scenario description, keep it as `USER_SCENARIO_HINT` and list candidate operation files from `ontology/operation/` for the user to choose.
- If no target is provided, list operation files from `ontology/operation/` and ask the user which one to walk through.
- If no operation files exist, tell user "未找到任何逻辑文档，请先运行 `/ontology-extract-operation`。" Then `python3 .claude/scripts/state_helper.py release trace` and stop.

After `OPERATION_PATH` is selected, read it once to derive:
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

### I2: Expand Branch Points

Read `OPERATION_PATH` and its direct sub-operation calls. For the current operation:
- Extract `# 逻辑描述`, `# 输入`, `# 与object的关联`, decision points, branches, and sub-operation calls.
- Show each decision point as a compact table:
  - 判断点 / 步骤
  - 可选分支
  - 走该分支需要哪些输入或对象字段
  - 会进入哪个子流程（如有）
- Ask the user to choose one branch or provide a business assumption.

Never treat "继续" as choosing a default branch. If the next branch is not explicit, ask again with the branch options.

### I3: Build Scenario And Data

For each user-selected branch:
- Convert the user's business assumption into concrete scenario inputs.
- Use existing `ontology/data-mock/*.md` normal rows when possible.
- If required object rows are missing, generate only the necessary normal rows in `ontology/data-mock/{对象名称}.md`; follow `.claude/templates/mock-data-template.md` and `.claude/agents/mock-data-agent.md`.
- Keep cross-object values consistent: if a scenario references one target row, copied same-meaning attributes must reuse that row's values.
- Non-critical unknown attributes may be blank/`无` only when the object definition allows it. Primary keys, foreign keys, and branch-dependent fields must not be blank.
- Record the source for each generated value: existing mock row, user assumption, or AI-filled concrete value.

Show the constructed scenario and any mock row changes, then ask:

```
请确认这个走查场景：
- 回复 **确认** → 保存场景并继续
- 回复 **修改 ...** → 调整输入或数据
- 回复 **换分支** → 回到当前判断点重新选择
- 回复 **停止** → 汇总已构造内容
```

### I4: Recurse Through Sub-Operation

- If the selected branch calls a sub-operation, enter that sub-operation and repeat I2/I3.
- Do not skip intermediate operation layers, even if the sub-operation looks simple.
- When a branch reaches an end/termination point, ask whether to backtrack and cover another branch.

### I5: Handle Semantic Defects

If a semantic defect is found, stop immediately and discuss it before continuing. Do not silently collect it for later.

Show:
- defect location (`operation + step`)
- why it blocks or weakens the walkthrough
- suggested fix
- impact on generated scenario/data

Then ask:

```
- 回复 **记录** → 只记录缺陷，继续或结束走查
- 回复 **修复** → 暂停 trace，转入现有修复流程（如 /ontology-validate 或 /ontology-update）
- 回复 **跳过** → 保留缺陷并选择其他分支
```

Do not directly edit operation files without user confirmation.

### I6: Save Outputs

After the user stops or all desired branches are covered:
- Save confirmed scenarios to `ontology/testcase/{OPERATION_NAME}.md` using `.claude/templates/testcase-template.md`.
- Save any confirmed normal mock rows under `ontology/data-mock/`.
- Write a walkthrough report to `reports/{YYYYMMDD}-{HHMMSS}-walkthrough-{OPERATION_NAME}.md` using `Asia/Shanghai` time, with:
  - branch coverage checklist
  - generated/updated mock data files
  - testcase path
  - semantic defect list
  - unresolved assumptions
- Commit testcase files separately with `git commit -m "trace: {OPERATION_NAME} interactive testcases"` after user confirms.
- If mock data files changed, commit them separately with `git commit -m "mock: {OPERATION_NAME} walkthrough data"` after user confirms.

Final prompt:

```
交互走查已完成。
- 测试场景：`ontology/testcase/{OPERATION_NAME}.md`
- 测试数据：{data-mock files or "未改动"}
- 走查报告：`reports/{filename}`

下一步建议：
- 回复 **执行追踪** → 用刚保存的 testcase 运行 `/ontology-trace {OPERATION_PATH}`
- 回复 **继续走查** → 回到分支选择，补更多场景
- 回复 **修复缺陷** → 处理语义缺陷
```

If user replies **执行追踪**, continue with normal single-file mode using `OPERATION_PATH`; saved testcases should be reused at Step 2.5.
