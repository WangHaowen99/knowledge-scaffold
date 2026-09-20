Trace operation with concrete scenarios and mock data to find operation gaps. The operation file path, "全部"/"all", or "交互 [逻辑文件]" is: $ARGUMENTS

## Step 0: 前置检查

遵循 `.claude/STEP0_TEMPLATE.md`，COMMAND_NAME = `trace`。简述：

1. `python3 .claude/scripts/state_helper.py check-lock trace`
2. `python3 .claude/scripts/state_helper.py check-prereq trace`
   - `missing:逻辑已提取` → **跳步守卫**："继续会导致没有逻辑可供追踪"。选项 **继续** / **先做 逻辑提取**（/ontology-extract-operation）。
3. `python3 .claude/scripts/state_helper.py acquire trace start`
4. 本入口只做路由，不加载 scoped knowledge。各模式文件在确定 operation/object scope 后自行加载业务知识。

命令结束前 `release trace` 并 `update-progress`。

## 业务知识识别（贯穿整个命令）

整个 trace 命令期间始终遵循 `.claude/BUSINESS_KNOWLEDGE_SNIPPET.md`。
trace 主代理 prompt 与可能的 mock 子代理 prompt 都需 transclude 该文件全文。

## Step 0.5: Determine mode

- If $ARGUMENTS first token is `全部` / `all` / `batch`:
  - Set `TRACE_MODE=batch`.
  - Read `.claude/commands/ontology-trace-batch.md` and follow it.
- If $ARGUMENTS first token is `交互` / `交互走查` / `对话走查` / `走查`:
  - Set `TRACE_MODE=interactive`.
  - Set `INTERACTIVE_TARGET=<remaining text after 去掉触发词>`（may be empty, a operation path, or a scenario description）。
  - Read `.claude/commands/ontology-trace-interactive.md` and follow it.
- If $ARGUMENTS is empty, ask:
  "请输入要追踪的逻辑文件路径（如 `ontology/operation/创建订单.md`），或选择：
  - 回复 **全部** → 批量追踪所有逻辑
  - 回复 **交互** → 进入交互走查：每个分支由你选方向，AI 辅助构造场景和正常测试数据
  - 回复 **交互 ontology/operation/创建订单.md** → 对指定逻辑做交互走查"
  Then parse the user reply the same way:
  - first token `全部` / `all` / `batch` → set `TRACE_MODE=batch`; read `.claude/commands/ontology-trace-batch.md` and follow it.
  - first token `交互` / `交互走查` / `对话走查` / `走查` → set `TRACE_MODE=interactive`; set `INTERACTIVE_TARGET=<remaining text after 去掉触发词>`; read `.claude/commands/ontology-trace-interactive.md` and follow it.
  - otherwise → set `TRACE_MODE=single`; set `OPERATION_PATH` to the reply; read `.claude/commands/ontology-trace-single.md` and follow it.
- Otherwise:
  - Set `TRACE_MODE=single`.
  - Set `OPERATION_PATH=$ARGUMENTS`.
  - Read `.claude/commands/ontology-trace-single.md` and follow it.
