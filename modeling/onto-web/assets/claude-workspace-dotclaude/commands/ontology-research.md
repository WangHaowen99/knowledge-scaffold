Conduct domain research for an ontology modeling project. The domain is: $ARGUMENTS

## Step 0: 前置检查

遵循 `.claude/STEP0_TEMPLATE.md`，COMMAND_NAME = `research`。简述：

1. `python3 .claude/scripts/state_helper.py check-lock research`
   - `ok` → 继续
   - `locked:{ts}:{phase}` → 按 **错误恢复类** 话术，让用户选 **续执** / **清理重来** / **取消**
2. `python3 .claude/scripts/state_helper.py check-prereq research`（research 无前置，应始终返回 ok）
3. `python3 .claude/scripts/state_helper.py acquire research start`

4. 加载已沉淀业务知识（仅 global 范围；research 是首阶段，对象/逻辑尚未提取）：
   ```bash
   KNOWLEDGE_BLOCK=$(python3 .claude/scripts/knowledge_helper.py load --scope global)
   ```
   若 `KNOWLEDGE_BLOCK` 等于 `（暂无相关业务知识）`，置为空字符串以跳过注入。

命令结束前执行 `release research` 与 `update-progress`。

## 业务知识识别（贯穿整个命令）

整个 research 命令期间始终遵循 `.claude/BUSINESS_KNOWLEDGE_SNIPPET.md`。
research-agent 的 dispatch prompt 顶部必须注入 `KNOWLEDGE_BLOCK`（若非空）。

## Step 1: Check for existing report

List files in `research/`. Check if a report for this domain already exists (filename contains the topic keywords).

**If a report exists:**
- Read the first 400 words to summarize it
- Tell the user: "已有调研报告（research/xxx.md），请选择：[1] 继续使用现有报告 [2] 追加调研 [3] 重新调研"
- Wait for user response:
  - **[1]**: Summarize key findings and suggest running `/ontology-extract-object` next. Stop.
  - **[3]**: Proceed as new research (treat as no existing report). Skip to Step 1.5.
  - **[2]**: Go to Step 1b.

**If no report exists:** skip to Step 1.5.

## Step 1b: Suggest append directions (append mode only)

Analyze the existing report. Identify 3-5 specific areas that lack detail or are not yet covered. Present:

```
建议补充调研以下方向：
a) [specific gap or topic]
b) [specific gap or topic]
c) [specific gap or topic]
d) 自定义方向...
```

Ask the user to select one or more directions (or type a custom direction). Record selected directions as FOCUS_DIRECTIONS.

## Step 1.5: Check for local domain documents

Ask the user:
"是否有本地领域文档（如业务说明书、流程规范、数据字典等 Markdown 文件）可作为调研素材？
- 如有，请提供文件路径，多个路径用逗号分隔（如 `docs/业务说明.md, docs/流程规范.md`）
- 如无，直接回复 **无**"

Wait for response.

**If user provides paths:**
- Set LOCAL_DOCS_PATHS = list of provided paths
- Ask: "是否同时进行网络调研以补充本地文档未覆盖的内容？[是] [否，仅基于本地文档]"
  - **是**: set WEB_RESEARCH_MODE = supplement
  - **否**: set WEB_RESEARCH_MODE = skip

**If user replies "无" or empty:** set LOCAL_DOCS_PATHS = empty (do not pass WEB_RESEARCH_MODE to agent — agent proceeds normally by default).

**Note for append mode:** local documents are read by the research agent alongside the focused research directions. They do not affect the gap analysis in Step 1b, which is based solely on the existing report.

---

## Step 2: Confirm topic

If $ARGUMENTS is empty, ask: "请输入要调研的领域名称：" and wait for response. Use it as TOPIC.
Otherwise TOPIC = $ARGUMENTS.

## Step 3: Dispatch research subagent

Read `.claude/agents/research-agent.md` to get the subagent prompt.

Dispatch a general-purpose subagent (Agent tool) with:
- If `KNOWLEDGE_BLOCK` is non-empty: prepend it (followed by a blank line and `---` separator) **before** the research-agent.md content
- The full contents of `.claude/agents/research-agent.md` as the base prompt
- Append: `\n\nResearch topic: {TOPIC}`
- If append mode, also append: `\n\nAppend focus directions: {FOCUS_DIRECTIONS}\n\nExisting report summary (first 400 words):\n{SUMMARY}`
- If LOCAL_DOCS_PATHS is non-empty, also append: `\n\nLocal document paths: {LOCAL_DOCS_PATHS}\nWeb research mode: {WEB_RESEARCH_MODE}`

Wait for the subagent to complete and return the full structured report.

## Step 4: Save report

**New research:** Write the returned report to `research/YYYYMMDD-{TOPIC}.md` (use today's `Asia/Shanghai` date, replace spaces with hyphens in filename).

**Append mode:** Write the returned report to a new file: `research/YYYYMMDD-{TOPIC}-补充-{SHORT_DIRECTION}.md` (use today's `Asia/Shanghai` date), where SHORT_DIRECTION is a brief slug (1-4 Chinese characters) summarizing the focus directions.

## Step 5: Commit

```bash
git add research/
git commit -m "research: {TOPIC}"
```

For append mode use: `git commit -m "research: append {FOCUS_DIRECTIONS}"`

Show the user a brief summary of the report (5-10 bullet points covering key objects and operations found), then ask:

"调研报告已保存至 `research/{filename}`。

**下一步建议：**
- 回复 **流程图**（/ontology-flowchart）→ 创建业务流程图（流程复杂、多子流程交叉时推荐）
- 回复 **提取对象**（/ontology-extract-object）→ 跳过流程图，直接从本份调研报告提取对象
- 回复 **追加调研** → 补充其它方向
上述选项只是参考，你也可以直接说明想修改、补充、校验或调整的内容，不必局限于这些建议。"

If user replies **流程图** (or any affirmative like 好/开始/继续/是): read `.claude/commands/ontology-flowchart.md` and follow its instructions immediately.
If user replies **提取对象**: read `.claude/commands/ontology-extract-object.md` and follow it（由于没有流程图摘要，Step 1 会进入"仅基于调研报告"模式）。
If user replies **追加调研**: restart this command from Step 1.
If user wants other changes first: handle them, then ask again.
