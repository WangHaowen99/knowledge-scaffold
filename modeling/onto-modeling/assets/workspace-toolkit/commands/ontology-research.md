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

5. **只做分段规划**：`$ARGUMENTS` 里有 `phase=blueprint`（或 `phase=slices`）时，不重做资料分析：先跑一次
   Step 4.5 的内容验收（只核验，不改报告），通过后直接到 Step 6；没通过按 Step 4.5 的未通过话术处理并停下。

## 业务知识识别（贯穿整个命令）

整个 research 命令期间始终遵循 `.claude/BUSINESS_KNOWLEDGE_SNIPPET.md`。
research-agent 的 dispatch prompt 顶部必须注入 `KNOWLEDGE_BLOCK`（若非空）。

## Step 1: Check for existing report

List files in `research/`. Check if a report for this domain already exists (filename contains the topic keywords).

**If a report exists:** 按 `$ARGUMENTS` 判断，不问用户（接下来做什么由你定，只问业务问题）：
- 含「追加」或给了补充方向 → Go to Step 1b；
- 含「重做」「重新」→ Proceed as new research (treat as no existing report). Skip to Step 1.5；
- 其余 → 先跑一次 Step 4.5 的内容验收（旧项目的报告没有回执，默认是「资料分析待核验」）。
  通过 → 有蓝图就 `release research` 后用两三句话总结要点，没有蓝图就到 Step 6；未通过 → 按 Step 4.5 修正。
  不重新做资料分析。

**If no report exists:** skip to Step 1.5.

## Step 1b: Suggest append directions (append mode only)

Analyze the existing report. Identify 3-5 specific areas that lack detail or are not yet covered. Present:

```
建议补充资料分析以下方向：
a) [specific gap or topic]
b) [specific gap or topic]
c) [specific gap or topic]
d) 自定义方向...
```

先 `python3 .claude/scripts/state_helper.py acquire research confirm`，再 ask the user to select one or more directions (or type a custom direction)；回复后 `acquire research start`。Record selected directions as FOCUS_DIRECTIONS.

## Step 1.4: 资料用途清单

读取 `resource/materials.json`（不存在则跳过）。本步骤只建立处理计划，不逐个精读：

- `purpose=goal` 的资料 → 在 Step 2 确认分析主题时必须先引用其内容；没有 `goal` 标注时，从文档类资料里找出说明建模场景、目标或范围的文档（如场景说明、需求说明）按同样方式优先引用，并在报告「信息来源」里注明；
- 需要全文读取的资料（`reading_mode=full`，或没有写 `reading_mode` 的文档类资料，规则见 CLAUDE.md 8.1）→ 在 Step 1.5 中必须全文读取，不得只扫开头；
- `purpose=schema` / `term` 的表格 → 资料分析报告中列入"数据与术语来源"清单，供后续对象提取直接引用；
- 其余按 `auto` 处理。

## Step 1.5: 资料与联网方式（不问用户）

资料就是资料区里的全部资料，不问用户还有没有别的文档、在哪个路径：

```bash
python3 .claude/scripts/source_extract.py --sources
```

只读文本：每份资料读它的 `read`（md、txt、csv 是原文件，PDF、Word、Excel 是数据接入转出的文本版
`resource/text/…`）；`read` 为空的资料还没有文本版（转换中、暂不支持或转换失败），记为未读，不要自己解析原件。
一份可读的文本都没有时停下（`acquire research confirm`），告诉用户先在资料区上传资料或等资料转成文本。

联网方式按 `$ARGUMENTS` 和项目能力定，也不问：含「仅基于本地」或项目没启用联网调研
（`.claude/skills/deep-research/SKILL.md` 不存在）→ WEB_RESEARCH_MODE = skip；否则 supplement。

**Note for append mode:** materials are read by the research agent alongside the focused research directions. They do not affect the gap analysis in Step 1b, which is based solely on the existing report.

---

## Step 2: Confirm topic

要解决的问题以平台注入的「建模目标」为准（建项目时用户写的），不要改写，也不要从资料里另找一份当目标。
TOPIC 取建模目标里的业务领域（几个字，用于报告文件名），不问用户。`$ARGUMENTS` 里的「仅基于本地文档」「追加」
这类说明不是领域名。

资料区「把资料读完」发来的是 `path=<资料路径> reading_mode=full`：沿用已有资料分析的 TOPIC，只把这份资料按全文读取补进分析（Step 1.5 的全文规则），不重做其他资料。

## Step 2.5: 固化只读来源快照

派遣之前把本轮的事实依据固化下来，派遣与回传核验引用同一份快照：

```bash
INVENTORY_BLOCK=$(python3 .claude/scripts/research_checker.py capture)
```

它会把**真实表头**（schema 资料的 Sheet 名与列名）和**既有规范清单**
（上一次完整验收通过时的业务项 ID、规则 ID 及来源报告；从未通过过则为空）写入
`.ontology-state/research-inventory.json`，并输出可直接注入 prompt 的区块。
没通过验收的报告不进清单：它里面的 ID 尚未核实，当成基线会让编造的 ID 在本轮放行。

该快照是本轮字段名与 ID 的唯一依据：派遣时原文注入，回传后由同一份快照对账。
不要把表头或清单改写成自然语言描述后再传给子代理。

## Step 3: Dispatch research subagent

Read `.claude/agents/research-agent.md` to get the subagent prompt.

Dispatch a general-purpose subagent (Agent tool) with:
- If `KNOWLEDGE_BLOCK` is non-empty: prepend it (followed by a blank line and `---` separator) **before** the research-agent.md content
- `INVENTORY_BLOCK` 紧随其后（同样用空行 + `---` 分隔）**prepended before** the research-agent.md content
- The full contents of `.claude/agents/research-agent.md` as the base prompt
- Append: `\n\nResearch topic: {TOPIC}`
- If append mode, also append: `\n\nAppend focus directions: {FOCUS_DIRECTIONS}\n\nExisting report summary (first 400 words):\n{SUMMARY}`
- Always append: `\n\n建模目标：{建模目标}\nWeb research mode: {WEB_RESEARCH_MODE}`

Wait for the subagent to complete and return the full structured report.

## Step 4: Save report（候选稿）

**New research:** Write the returned report to `research/YYYYMMDD-{TOPIC}.md` (use today's `Asia/Shanghai` date, replace spaces with hyphens in filename).

**Append mode:** Write the returned report to a new file: `research/YYYYMMDD-{TOPIC}-补充-{SHORT_DIRECTION}.md` (use today's `Asia/Shanghai` date), where SHORT_DIRECTION is a brief slug (1-4 Chinese characters) summarizing the focus directions.

落盘只代表"已保存"，不代表"已验收"。先别向用户宣布资料分析完成，继续 Step 4.5。

## Step 4.5: 内容验收（确定性检查，不可跳过）

```bash
python3 .claude/scripts/research_checker.py check
```

检查计数自洽、ID 唯一与稳定、引用字段是否真实、来源位置能否定位；通过后写验收
回执 `.ontology-state/research-review.json`，对象提取的阶段前置只认这份回执。

退出码 0 = 已验收；1 = 有确定性错误或人工待决未完成；2 = 没有报告可核验。

**退出码为 1 时**：不要改写检查结论，也不要跳到 Step 5。把脚本列出的错误项逐条
展示给用户（含来源位置），然后：

- **确定性错误**（计数不符、ID 静默变化、字段不在表头、来源定位不到）→ 按错误项
  修正报告正文，修正后重跑本步骤。失败草稿保留，不删除报告、不重建项目。
- **待人工确认条目**（输出里标 `[待人工确认]`）→ 条目原文是工作记录，**不要原样转给用户**。
  逐条按 `INTERACTION_CONVENTIONS.md`「请专家确认卡类」处理：有明确默认做法的（如输出类对象
  数据来源为「无」、资料用途可按内容判断）直接按默认处理并在报告里写明结论；只有需要业务判断的
  才改写成业务问题问用户（问之前 `acquire research confirm`，回复后 `acquire research start`）。处理完后在报告「缺口与待决」里把对应条目改成 `- [x]` 并补上结论，
  再重跑本步骤。
- 修正两轮仍不通过时停下来（先 `acquire research confirm`），向用户报告剩余错误项与建议，不要反复重写报告。

**脚本自身报错或缺失**（输出不是上述三种退出码的正常结果）：向用户说明"无法核验"，
保留报告，不要据此宣布通过，也不要继续对象提取。

## Step 5: Commit

```bash
git add research/
git commit -m "research: {TOPIC}"
```

For append mode use: `git commit -m "research: append {FOCUS_DIRECTIONS}"`

commit 前置 hook 会再跑一次同一检查；未通过时提交会被阻止，这是预期行为。

**Step 4.5 已验收（退出码 0）时：** 直接进 Step 6 做分段规划。


**Step 4.5 修正两轮仍未通过时**（不要提议提取对象）：先 `python3 .claude/scripts/state_helper.py acquire research confirm`，
用业务话说明报告还差什么（哪些出处对不上、哪些字段在表头里找不到），只把需要用户给业务结论的条目问出来，
不列「修正 / 人工确认」这类菜单——怎么改报告是你的事。用户回复后 `acquire research start`，回到 Step 4.5，通过后进 Step 6。

## Step 6: 分段规划（建模蓝图）

资料分析含分段规划（详设第 9 章建议 2）：把业务拆成几段，之后按段建模，每段单独走完「对象 → 操作 → 规则 → 写入即查」，
每一轮结束自动存成版本。蓝图文件 `research/blueprint.json` **只由脚本写**：你先写一份草稿 JSON，再让脚本校验、编号后落盘。

1. 读已验收的资料分析报告（业务项清单、候选段、缺口与待决）和资料用途清单，按下面的规则分段：
   - 一段是一条能单独讲清楚、单独验证的业务链路：通常 1 个主操作、3～8 个对象、几条规则；
   - 被几段共用的对象（如基础档案）放在最先做的那一段，别的段依赖它、只引用不改；
   - 按依赖排顺序：被依赖的在前；
   - 资料少、业务链路只有一条时只划一段（全量模式），不要硬拆。
2. 写草稿 `.ontology-state/blueprint-draft.json`（字段名照写，名称用资料里的业务叫法，不编造）：

   ```json
   {"slices": [{"name": "段名（≤20 字）", "core_flow": "核心链路（≤30 字）", "depends_on": ["依赖的段名"],
                "objects": ["对象名"], "operations": ["操作名"], "rules": ["规则名"],
                "sources": [{"anchor": "出处锚点", "label": "出处短标签"}]}],
    "terms": {"aligned": ["已经统一的叫法"], "ambiguous": [{"variants": ["叫法一", "叫法二"]}]},
    "questions": [{"title": "需要用户回答的业务问题", "background": "为什么要问（一句）", "options": ["选项一", "选项二"]}]}
   ```

   对象、操作、规则的名字就是之后条目的文件名，不能带 `/ \ # ? & | [ ]`。资料里没有规则的段，`rules` 留空。
   要解决的问题就是平台注入的「建模目标」，草稿里不写目标；读不了的资料由资料区显示，草稿里也不写「不在范围」。
   **资料和建模目标对不上、或目标太笼统**（比如目标说补货单，资料讲的是盘点）时，在 `questions` 里加一个目标问题，
   不要用 AskUserQuestion 问（自主构建时那会被策略代答）：
   `{"kind": "goal", "title": "资料讲的是…，和目标里的…对不上，以哪个为准？", "background": "一句依据",
   "suggested_goal": "按资料改写的一句目标（必写：用户选它，项目目标就改成这句）"}`。选项由脚本生成；用户在蓝图上回答，选了改写或自己改了目标才改项目目标。
   这类业务取舍（以哪个为准、要不要建）放进 `questions`；编号、文件名、格式这些你自己定，不问。
3. 落盘：

   ```bash
   python3 .claude/scripts/blueprint.py write .ontology-state/blueprint-draft.json
   ```

   输出 `ok:…` 才算写好；输出 `error:…` 时按提示改草稿再写，不要手工改 `research/blueprint.json`。
   已有蓝图时（重新划分）草案号会加 1，同名的段沿用原来的编号。
4. 提交：`git add research/blueprint.json && git commit -m "research: 建模蓝图"`。

界面会出建模蓝图卡和「确认后怎么建」，回复里不要重复整份蓝图。先 `python3 .claude/scripts/state_helper.py release research`，
再用一两句说明划了几段、哪一段推荐先做、有几个问题等用户回答；不列下一步菜单（界面的建议条会给出），
也**不要用提问（AskUserQuestion / ask_user）问「接下来怎么建」「先做哪一段」**——说完就 Stop，不开始建模。
蓝图在界面上确认，用户在对话里说「确认」时提醒他点「确认蓝图」。

用户要调整分段（合并、排除、改名、调顺序）时，请他在建模蓝图里直接改；要「把某一段按章节拆分」时，重读那一段的
资料，按上面的规则重写草稿并再跑一次 `blueprint.py write`。
