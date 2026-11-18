Create, initialize, or refresh business process flowcharts in ontology/diagrams/. Domain or refresh request (if any): $ARGUMENTS

## Step 0: 前置检查

遵循 `.claude/STEP0_TEMPLATE.md`，COMMAND_NAME = `flowchart`。简述：

1. `python3 .claude/scripts/state_helper.py check-lock flowchart`
   - `locked:...` → `python3 .claude/scripts/state_helper.py recover flowchart` 由平台定（不问用户）：`resume:{phase}` 跳到对应阶段接着做，`restart` 已清理、重新开始；工作过程里说一句业务话
2. `python3 .claude/scripts/state_helper.py check-prereq flowchart`（无硬前置，若已有 `research/` 则会在下方 Mode A 自动使用）
3. `python3 .claude/scripts/state_helper.py acquire flowchart start`
4. 加载已沉淀业务知识（仅 global 范围；流程图阶段对象尚未提取）：
   ```bash
   KNOWLEDGE_BLOCK=$(python3 .claude/scripts/knowledge_helper.py load --scope global)
   ```
   若 `KNOWLEDGE_BLOCK` 等于 `（暂无相关业务知识）`，置为空字符串以跳过注入。

命令结束前 `release flowchart` 并 `update-progress`。

## 业务知识识别 & 应用（贯穿整个命令）

整个 flowchart 命令期间始终遵循 `.claude/BUSINESS_KNOWLEDGE_SNIPPET.md`。

本命令以 main agent 直接执行为主（无 sub-agent dispatch）。Mode A 在绘制节点/边、给流程命名时，**必须**先扫描 Step 0 加载的 `KNOWLEDGE_BLOCK`：已沉淀的术语/口径/枚举优先使用，避免生成与既有业务知识冲突的命名。

## Refresh from resource（优先分支）

如果 `$ARGUMENTS` 包含 `刷新`、`全量刷新`、`刷新流程图`、`刷新Excel`、`刷新 xlsx` 等含义，优先进入本分支，**不要执行后续 Mode detection**。

本分支的前提：
- `resource/` 是用户编辑的源材料；`ontology/diagrams/`、`ontology/excel/`、`ontology/diagrams-summary/` 都是派生产物
- 刷新只负责从 `resource/` 重建派生产物，不做结构/语义校验
- 刷新完成后**不要自动运行** `/ontology-validate-diagrams`，只提示用户下一步运行校验
- 刷新完成后**不要自动提交**，待用户校验确认后再提交

### 用户友好的指令形式

支持以下说法，并按规则翻译成脚本参数：

| 用户说法 | 执行命令 |
|---|---|
| `/ontology-flowchart 全量刷新` | `python3 .claude/scripts/init_flowchart_resources.py --refresh` |
| `/ontology-flowchart 刷新 A、B` | `python3 .claude/scripts/init_flowchart_resources.py --names A B` |
| `/ontology-flowchart 刷新流程图 A、B` | `python3 .claude/scripts/init_flowchart_resources.py --pages A B` |
| `/ontology-flowchart 刷新Excel A、B` | `python3 .claude/scripts/init_flowchart_resources.py --sheets A B` |
| `/ontology-flowchart 刷新 xlsx A、B` | `python3 .claude/scripts/init_flowchart_resources.py --sheets A B` |

如果用户指定了源文件，例如 `resource/diagrams/总流程.drawio 中的 A、B` 或 `resource/excel/补充.xlsx 中的 A、B`：

- 流程图页签：追加 `--diagram resource/diagrams/总流程.drawio --pages A B`
- Excel sheet：追加 `--excel resource/excel/补充.xlsx --sheets A B`
- 同时刷新两类同名页签/sheet：使用 `--names A B`

多个页签/sheet 名可以用顿号、中文逗号、英文逗号或空格分隔；执行前把它们转换成独立参数。若脚本报告重名页签/sheet，提示用户补充具体源文件路径后重试。

### 刷新后的反馈

执行脚本后读取命令输出，先 `python3 .claude/scripts/state_helper.py release flowchart`，再向用户说明刷新了几个流程图、几张 Excel 表、几份流程图摘要，并说一句还没做结构和语义校验；不列下一步菜单（界面的建议条会给出）。

## Step 1: Assess current state

Check these locations to determine mode:
- `ontology/diagrams/` — for existing initialized flowcharts
- `ontology/diagrams-summary/` — for generated summary markdown used for review and downstream extraction only
- `ontology/excel/` — for initialized Excel supplements
- `resource/diagrams/` — for source diagrams not yet initialized
- `resource/excel/` — for Excel supplements paired with source diagrams
- `research/` — for research reports that can inform flowchart creation

`ontology/diagrams-summary/` is a derived artifact. Do not treat a newer summary as source truth, and do not regenerate `.drawio` files from edited summaries.

**Mode detection:**
1. `$ARGUMENTS` 含「补充」「新增」「追加」，且 `ontology/diagrams/` 已有 .drawio 文件 → Mode D (add missing processes)
2. `ontology/diagrams/` has .drawio files → Mode C (already exists)
3. `ontology/diagrams/` empty BUT `resource/diagrams/` has .drawio files → Mode B (init from source)
4. Both empty → Mode A (create from scratch)

用一句业务话告诉用户接下来做什么（如「从资料区的流程图初始化」），不说 Mode 名。

## Step 2: Execute mode

### Mode A — Create from scratch

1. Check for research reports in `research/`. If found, read them to identify key business processes.
2. If no research reports, `python3 .claude/scripts/state_helper.py acquire flowchart confirm` 后 ask user（回复后 `acquire flowchart start`）: "未找到资料分析报告或现有流程图。请选择：
   - 描述你的业务流程，我来生成流程图
   - 先运行 `/ontology-research <领域>` 进行资料分析"
3. Once business processes are identified (from research or user description), `acquire flowchart confirm`，list them and ask user to confirm which ones to create flowcharts for（回复后 `acquire flowchart start`）〔自主构建跳过：不问，全部画〕.
4. For each confirmed process, generate a .drawio XML file:
   - Read the legend `.claude/reference/图示说明定义.drawio` (older projects may have their own copy at `resource/diagrams/图示说明定义.drawio`, which takes precedence) to understand node type conventions
   - Generate well-formed draw.io XML with proper node types (开始/结束, 业务活动, 判断, 子流程, 子流程判断, 动作, 说明, 递归)
   - Do not generate semantic flow nodes from legend entries marked "虚拟项"; `offPageConnector` is only a passthrough view helper
   - Include start/end nodes, clear input/output annotations
   - Save to `ontology/diagrams/{process_name}.drawio`
5. Generate summaries:
   ```bash
   python3 .claude/scripts/batch_summarize.py ontology/diagrams
   ```
6. Read summaries from `ontology/diagrams-summary/` and present to user〔自主构建跳过〕.
7. Proceed to Step 3 (commit).

> 生成后复用确定性结构检查；深度语义校验仍由 `/ontology-validate-diagrams` 负责，不新增模型复核。

### Mode B — Initialize from source

1. Initialize source materials from `resource/` and generate summaries:
   - `resource/diagrams/*.drawio` → `ontology/diagrams/*.drawio`（多页签会拆成单图）
   - `resource/excel/*.xlsx` → `ontology/excel/{sheet名}.xlsx`（多 sheet 会拆成单表）
   - `ontology/diagrams/*.drawio` → `ontology/diagrams-summary/{图名}.md`
   ```bash
   python3 .claude/scripts/init_flowchart_resources.py
   ```
2. Read summaries and present to user.
3. Proceed to Step 3 (commit).

### Mode D — 补充新流程

用户在流程图编辑器里点「AI 推荐新流程」时进入本分支：在已有流程图之外补画还没有覆盖的业务流程，已有流程图一律不动。

1. 读 `research/` 下的资料分析报告与 `ontology/diagrams-summary/` 下已有流程图的摘要，找出报告里有、但还没有流程图的业务流程。
2. 先 `python3 .claude/scripts/state_helper.py acquire flowchart confirm`，用业务语言列出候选（每条一句话说明它是什么流程、为什么值得单独画），请用户确认要补画哪些（回复后 `acquire flowchart start`）；没有候选时说明「资料分析报告里的业务流程都已有流程图」，并询问用户是否要描述新的流程。
3. 对确认的流程按 Mode A 第 4～6 步生成 `.drawio` 与摘要，只写新文件。
4. Proceed to Step 3 (commit).

### Mode C — Already exists

1. Count .drawio files in `ontology/diagrams/`.
2. Display: "已有 {N} 个流程图，无需重新创建。"
3. Skip to Step 4 (suggest next step), without committing.

## Step 3: Commit

保存前运行 `python3 .claude/scripts/verify_artifacts.py diagrams --files {本轮应生成的文件路径，逐项加引号}`，
清单取已确认流程计划，不能只统计已落盘文件。退出码 0 才可报告结构核验通过并提交；
1 为结构错误，2 为缺少产物、执行失败或检查期间文件变化，均不能宣布完成。只修本批问题，不改源资料；
有派生子任务时先等实际终态，不以主 Agent 回答结束或子任务摘要代替完成。提交 hook 会复用同修订回执。

Determine TOPIC:
- If $ARGUMENTS was provided: use $ARGUMENTS
- Else if research reports exist: infer from research report filename
- Else: ask user for a topic name（问之前 `acquire flowchart confirm`，回复后切回 `start`）

```bash
git add ontology/diagrams/ ontology/diagrams-summary/ ontology/excel/
git commit -m "flowchart: {TOPIC} initial diagrams"
```

## Step 4: Suggest next step

先 `python3 .claude/scripts/state_helper.py release flowchart`，再用两三句话小结：画了或初始化了哪些流程图、还有什么要用户定；不列下一步菜单（界面的建议条会给出）。用户之后在对话里说「校验」「提取对象」，按下面的分支做。

If user replies **校验**: read `.claude/commands/ontology-validate-diagrams.md` and follow its instructions.
If user replies **提取对象**: read `.claude/commands/ontology-extract-object.md` and follow its instructions.
