Generate a structured overview of the entire ontology: object relationships, operation call chains, and coverage stats. Optional scope (topic keyword): $ARGUMENTS

## Step 1: Check ontology exists

List files in `ontology/object/` and `ontology/operation/`.

If both are empty: "未找到本体文档，请先运行 `/ontology-extract-object`。" Stop.

## Step 2: Build indexes

Scan all ontology documents and build the following in-memory indexes:

1. **OBJECTS_IDX**: `{对象名 → {别名, 关联对象列表}}`
   - From each `ontology/object/*.md`:
     - Parse `# 对象别名` line for aliases
     - Parse `# 关联对象` section for edges like `当前对象 → 关系 → [[对象X]]` or `[[对象Y]] → 关系 → 当前对象`；关系名后的 `（源字段 → 目标字段）` 括注是关联字段声明，取关系名时去掉括注（括注内的箭头不是条目分隔符）
2. **OPERATION_IDX**: `{逻辑名 → {输入对象, 输出对象, 支撑对象, 调用operation, 被operation调用}}`
   - From each `ontology/operation/*.md`:
     - Parse `# 与object的关联` for 输入/输出/支撑 对象的 `[[...]]`
     - Parse `# 与operation的关联` for "调用operation / 被operation调用" 行中的 `[[...]]`
     - Parse `# 逻辑描述` for 调用子流程 (`调用[[X]]逻辑文档`)
3. **SCRIPTS_IDX**: `{脚本文件名 → {size_kb, 关联逻辑（数组）, 角色}}`
   - List `*.py` files under `ontology/scripts/`（忽略 `__pycache__` 与 `_` 开头的私有模块）
   - 关联逻辑：扫描每个 `ontology/operation/*.md` 的「调用指令」/「脚本位置」section，匹配 `ontology/scripts/<name>.py` 字样；可能 1 个脚本对应 1 个 operation，也可能对应多个，未被引用则记为「工具脚本」
   - 角色：被 operation 引用 → 「主流程脚本」；未被引用 → 「工具脚本」（一般是被其他脚本 import 的辅助）

## Step 3: Compute derived views

From the indexes, compute:

- **孤立对象**: in OBJECTS_IDX but NOT referenced by any operation (not in any operation's 输入/输出/支撑)
- **缺失引用**: `[[X]]` links whose target file does not exist (broken links)
- **顶层逻辑**: operation with no "被operation调用" entry (entry points)
- **子流程**: operation referenced by others via "调用[[...]]"
- **对象度数 topN**: objects referenced by most operation (business hubs)

If $ARGUMENTS provided as a keyword: filter all views to only include items whose name contains the keyword (substring match, case-insensitive for latin; exact char match for Chinese).

## Step 4: Generate HTML overview

读两个模板：

- `BASE_TEMPLATE` = `.claude/templates/html-report-base.html`（页面外框 + CSS 主题）
- `BODY_TEMPLATE` = `.claude/templates/overview-body.html`（overview 特有的 stats / 7 个 tab / vis-network init JS）

输出路径：`reports/{YYYYMMDD}-{HHMMSS}-overview.html`，日期时间使用 `Asia/Shanghai`。

### 4.1 Prepare graph datasets

从索引构建两组 JSON：

**object_data**（vis-network 节点/边）：
- 节点：每个对象一项，字段为 `{id: "对象名", label: "对象名", value: <被引用次数>, color: <见下>, title: "<aliases + 描述首行>"}`
  - 颜色规则：孤立对象 → `{background:'#ffd8a8', border:'#bf8700'}`；高度数（top 10%）→ `{background:'#0969da', border:'#0550ae', highlight:{background:'#1f6feb'}}`，文字 `font:{color:'#fff'}`；其他 → `{background:'#9ec5fe', border:'#0969da'}`
  - `value` 用于 vis-network 的 size 缩放（启用 `scaling`）
- 边：每条 `# 关联对象` 条目一项，字段 `{from, to, label: "<关系名>", arrows: 'to'}`（`label` 只取关系名，不含关联字段括注）

**operation_data**（vis-network 节点/边）：
- 节点：每个逻辑一项，`{id, label, color}`
  - 顶层逻辑 → `{background:'#ffd8a8', border:'#bf8700'}`
  - 工具流程（被 3 个以上 operation 调用）→ `{background:'#c7eed8', border:'#1a7f37'}`
  - 其他 → `{background:'#a7f0ba', border:'#1a7f37'}`
- 边：每个 `调用[[X]]` 一项，`{from: 调用方, to: 被调用方, arrows: 'to'}`

**object_meta**（侧栏详情，JS 对象 `{对象名: {...}}`）：
- `name`, `aliases`（数组）, `description`（业务描述首行）, `used_by`（数组：使用该对象的逻辑名）, `related`（数组：`"对象X（关系）"` 字符串）

**operation_meta**（侧栏详情，JS 对象 `{逻辑名: {...}}`）：
- `name`, `description`, `is_top`（boolean）, `inputs`/`outputs`/`supports`（对象数组）, `calls`（调用的子流程数组）, `called_by`（被哪些 operation 使用）

> **节点过多兜底**：若对象总数 > 200 或逻辑总数 > 150，graph 数据集只保留度数 top 50 的节点 + 与其相连的边，并在图上方加 callout：「节点过多，仅展示度数 top 50；完整关系见下方矩阵」。

### 4.2 Compute placeholder values for BODY_TEMPLATE

`BODY_TEMPLATE` 顶部注释列出了所有占位符。按下面规则算出每个：

**STATS 段**

- `{{N_OBJECTS}}` / `{{N_OPERATIONS}}` / `{{N_SCRIPTS}}` — 整数
- `{{STATS_WARN_BADGES}}` — 把下列条件块拼成一段（全部为零时为空字符串）：
  - 若 `n_orphan_obj > 0` → `<div class="stat warn"><span class="num">{n}</span><span class="label">孤立对象</span></div>`
  - 若 `n_broken > 0` → `<div class="stat danger">…</div>`，label "缺失引用"

**BODY 段**

- `{{N_PROBLEMS_BADGE}}` — `n_problems > 0` 时 `<span class="badge danger">{n}</span>`，否则空串
- `{{TOP_OPERATION_LIST}}` — 每个顶层 operation 一条 `<li><strong>逻辑名</strong> — 业务描述</li>`（描述文本转义）
- `{{SCALE_SUMMARY}}` — 两三行自然语言摘要，例如"本体包含 X 个对象、Y 个逻辑，其中 Z 个顶层流程；存在 N 个待整改问题。"
- `{{MATRIX_ROWS}}` — 每个 operation 一条 `<tr><td>逻辑名</td><td>对象A, 对象B</td><td>…</td><td>…</td></tr>`（对象用 `, ` 分隔，全部转义）
- `{{HOTSPOT_ROWS}}` — top-20 对象，每条 `<tr><td>对象名</td><td><span class="bar" style="width:{count*8}px"></span>{count}</td><td>逻辑列表</td></tr>`
- `{{SCRIPTS_PANEL_CONTENT}}`：
  - 若 `n_scripts == 0` → `<p style="color:var(--text-muted);">未发现脚本（<code>ontology/scripts/</code> 为空）。</p>`
  - 否则 → 整段 `<table><thead><tr><th>脚本</th><th>大小</th><th>角色</th><th>关联逻辑</th></tr></thead><tbody>{ROWS}</tbody></table>`。每行：`<tr><td><code>filename</code></td><td>{size_kb} KB</td><td>{角色 badge}</td><td>{关联 operation 列表或 —}</td></tr>`。脚本按角色分组（主流程脚本 → 工具脚本），组内按文件名。角色 badge：主流程 `<span class="badge accent">主流程</span>`，工具 `<span class="badge">工具</span>`
- `{{PROBLEMS_PANEL_CONTENT}}` — 按下列条件拼接（全部为 0 时退化为单张 `<div class="card">未发现问题。</div>`）：
  - 孤立对象 > 0 → `<div class="card problem-section warn"><h3>孤立对象（n）</h3><p>这些对象未被任何逻辑使用：</p><ul>{对象列表}</ul></div>`
  - 缺失引用 > 0 → `<div class="card problem-section danger"><h3>缺失引用（n）</h3><ul>{引用方 → 缺失目标}</ul></div>`

**SCRIPTS 段（JSON 数据集）**

- `{{OBJECT_NODES_JSON}}` / `{{OBJECT_EDGES_JSON}}` / `{{OBJECT_META_JSON}}` / `{{OPERATION_NODES_JSON}}` / `{{OPERATION_EDGES_JSON}}` / `{{OPERATION_META_JSON}}` — **必须用真正的 JSON 序列化器产出**（如 `python3 -c 'import json,sys;json.dump(obj,sys.stdout,ensure_ascii=False)'`），中文按字面输出、不要 `\uXXXX`。
  - ⚠️ **不要手写 JS 对象字面量**。业务描述里经常嵌半角双引号（如 `触发"提交订单"后`、`实现"两段式预占"`），手写极易漏转义，产出 `description: "…触发"提交订单"…"` 会让整段 `<script>` 在该行断裂报 `SyntaxError`，**对象关系图与逻辑调用图双双空白（只剩静态图例）**。序列化器会自动把值内 `"` 转义为 `\"`、换行转 `\n`、反斜杠转 `\\`；坚持手写就必须自己完成同样的转义。这是该报告最常见的炸点。

### 4.3 Substitute and assemble

1. 在 `BODY_TEMPLATE` 上执行上述 13 个占位符替换 → 得到 `FILLED_BODY`。
2. 把 `FILLED_BODY` 按三个分隔符切成三段（顶部的文档注释块整体丢弃）：
   - `<!-- ===STATS=== -->` 之后到 `<!-- ===BODY=== -->` 之前 → `STATS_HTML`
   - `<!-- ===BODY=== -->` 之后到 `<!-- ===SCRIPTS=== -->` 之前 → `BODY_HTML`
   - `<!-- ===SCRIPTS=== -->` 之后到末尾 → `SCRIPTS_HTML`
3. 在 `BASE_TEMPLATE` 上替换：
   - `{{TITLE}}` → `本体建模整体视图`
   - `{{TIMESTAMP}}` → `YYYY-MM-DD HH:MM Asia/Shanghai`
   - `{{SCOPE_HINT}}` → 若有 `$ARGUMENTS` 关键词："　|　范围：包含关键词「{ARGUMENTS}」"；否则空串
   - `{{HEAD_EXTRA}}` → 空字符串（vis-network CDN 已在 `SCRIPTS_HTML` 顶部）
   - `{{HEADER_EXTRA}}` → `STATS_HTML`
   - `{{BODY_CONTENT}}` → `BODY_HTML`
   - `{{BODY_SCRIPTS}}` → `SCRIPTS_HTML`
   - `{{FOOTER_NOTE}}` → `由 /ontology-overview 生成`
4. 写入 `reports/{YYYYMMDD}-{HHMMSS}-overview.html`（日期时间使用 `Asia/Shanghai`）。
5. **产出后语法校验（必做，先于 Step 5）**：报告里嵌入的 6 个数据集只要有一处字符串没转义好，整段 `<script>` 就会解析失败、两张图全白。写完文件后跑下面这段扫描（纯 Python、无需 node），抓「`key: "值"` 标量行里值内出现未转义 `"`」这个主炸点：
   ```bash
   python3 - "reports/{filename}" <<'PY'
   import re, sys
   js = re.findall(r'<script>(.*?)</script>', open(sys.argv[1], encoding='utf-8').read(), re.S)[-1]
   bad = [(i, ln.strip()) for i, ln in enumerate(js.split('\n'), 1)
          if (m := re.match(r'\s*\w+:\s*"(.*)"\s*,?\s*$', ln)) and re.search(r'(?<!\\)"', m.group(1))]
   if bad:
       print("❌ 数据集存在未转义内层引号，会导致脚本断裂、图表空白：")
       [print(f"  script行{i}: {l[:90]}") for i, l in bad]
       sys.exit(1)
   print("✅ 数据集字符串转义检查通过")
   PY
   ```
   报 `❌` 时定位并修正这些行（内层 `"` → `\"`）后重写报告并重新校验，通过前不要进入 Step 5。若本机装有 node，可额外 `node --check`（抽出最后一段 `<script>` 写临时文件再 check）做全量语法兜底。

> ⚠️ **HTML 转义**：所有插入到 HTML 文本节点（`<li>` / `<td>` / `<h3>` 等）的用户内容必须做 `<`/`>`/`&`/`"`/`'` 转义；嵌入到 `<script>` 的 6 个 `*_JSON` 数据集必须是**真正序列化出来的合法 JSON**——只有这样值内的 `"`、换行才会被转成 `\"`、`\n`。**切勿手写 JS 字面量后直接粘贴描述文本**（值内半角引号未转义是最常见的炸点，会让整段脚本失效、两张图空白）。文档名/对象名一般不含特殊字符可按字面写入，但描述文本若含 `<` 等需转义。

## Step 5: Present summary

Show in terminal:

"整体视图已生成：`reports/{filename}`（HTML 报告，含交互式对象关系图与逻辑调用图）

**规模：** 对象 {M} / 逻辑 {K}
**问题：** 孤立对象 {x} / 缺失引用 {z}
**顶层流程：** {n} 个入口

{if 问题 > 0: '建议运行 /ontology-validate 处理缺失引用与孤立问题。'}

**BFO 参考提醒（可选）**：按 `.claude/reference/BFO_MODELING_GUIDE.md` §10 评估：
- 用 §10.2 的对象/逻辑关键词列表对当前所有对象与逻辑文件名做子串匹配，得到 `matched_keywords`（去重，最多取 5 个）。
- 命中 → 按 §10.3「带匹配项的形式」输出提示，作为"下一步建议"之前的独立一行。本命令本次执行只触发一次。
- 未命中 → 不输出。

**下一步建议：**
- 回复 **打开** → 在浏览器打开报告
- 回复 **修复**（/ontology-validate）→ 进入校验处理问题
- 回复 **问题视图** → 生成业务用户可提问清单 + 能力边界（独立报告，业务用户视角）
- 回复 **积木视图** → 生成规则积木与可组合性分析（独立报告，实现视角）
上述选项只是参考，你也可以直接说明想修改、补充、校验或调整的内容，不必局限于这些建议。"

On **打开**: 执行 `open reports/{filename}` （macOS 会用默认浏览器打开 HTML）。

On **修复**: read `.claude/commands/ontology-validate.md` and follow it.

On **问题视图**:
- 加载已沉淀业务知识（全量已存在的 operation / object scope；问题视图需要看到全部规则）：
  ```bash
  SCOPE_ARGS=(--scope global)
  for stem in $(ls ontology/operation/ 2>/dev/null | sed 's/\.md$//'); do SCOPE_ARGS+=(--scope "operation:$stem"); done
  for stem in $(ls ontology/object/ 2>/dev/null | sed 's/\.md$//'); do SCOPE_ARGS+=(--scope "object:$stem"); done
  KNOWLEDGE_BLOCK=$(python3 .claude/scripts/knowledge_helper.py load "${SCOPE_ARGS[@]}")
  ```
  若 `KNOWLEDGE_BLOCK` 等于 `（暂无相关业务知识）`，置为空字符串以跳过注入。
- Read `.claude/agents/question-extraction-agent.md` and dispatch it as a subagent.
- Dispatch prompt（顶部先注入 KNOWLEDGE_BLOCK 若非空，紧跟 `---` 分隔符）：
  ```
  {若 KNOWLEDGE_BLOCK 非空：原样粘贴 KNOWLEDGE_BLOCK 在此处，紧跟一行分隔符 ---}

  OPERATION_DIR=ontology/operation/
  OBJECT_DIR=ontology/object/
  REPORT_PATH=reports/{YYYYMMDD}-{HHMMSS}-questions.html
  ```
  （`REPORT_PATH` 与本次 overview 同前缀，便于 iframe 引用；日期时间使用 `Asia/Shanghai`。）
- Wait for the agent to complete (it writes the report itself; no return value needed beyond confirmation).
- **嵌入到 overview**：编辑本次的 overview HTML，在 tab nav 的 `问题清单` 按钮之后追加 `<button class="tab-btn" data-tab="questions">可响应问题</button>`（如已存在则跳过），并在 `</main>` 之前追加：
  ```html
  <section id="questions" class="tab-panel">
    <iframe src="{YYYYMMDD}-{HHMMSS}-questions.html" style="width:100%; height:calc(100vh - 240px); min-height:600px; border:1px solid var(--border); border-radius:var(--radius-md); background:var(--surface);" title="可响应业务问题清单"></iframe>
  </section>
  ```
- Tell user: "业务问题视图已生成：`reports/{filename}`，并嵌入到 overview 的「可响应问题」tab。回复 **打开问题视图** 单独打开，或 **打开** 回到 overview。"
- If user replies **打开问题视图**: 执行 `open reports/{filename}`。

On **积木视图**:
- 加载已沉淀业务知识（与「问题视图」同一加载逻辑；若本次 overview 已加载过 KNOWLEDGE_BLOCK 可直接复用）：
  ```bash
  SCOPE_ARGS=(--scope global)
  for stem in $(ls ontology/operation/ 2>/dev/null | sed 's/\.md$//'); do SCOPE_ARGS+=(--scope "operation:$stem"); done
  for stem in $(ls ontology/object/ 2>/dev/null | sed 's/\.md$//'); do SCOPE_ARGS+=(--scope "object:$stem"); done
  KNOWLEDGE_BLOCK=$(python3 .claude/scripts/knowledge_helper.py load "${SCOPE_ARGS[@]}")
  ```
  若 `KNOWLEDGE_BLOCK` 等于 `（暂无相关业务知识）`，置为空字符串以跳过注入。
- Read `.claude/agents/composition-analysis-agent.md` and dispatch it as a subagent.
- Dispatch prompt（顶部先注入 KNOWLEDGE_BLOCK 若非空，紧跟 `---` 分隔符）：
  ```
  {若 KNOWLEDGE_BLOCK 非空：原样粘贴 KNOWLEDGE_BLOCK 在此处，紧跟一行分隔符 ---}

  OPERATION_DIR=ontology/operation/
  OBJECT_DIR=ontology/object/
  REPORT_PATH=reports/{YYYYMMDD}-{HHMMSS}-composition.html
  ```
  （`REPORT_PATH` 与本次 overview 同前缀；日期时间使用 `Asia/Shanghai`。）
- Wait for the agent to complete.
- **嵌入到 overview**：编辑本次的 overview HTML，在 tab nav 末尾追加 `<button class="tab-btn" data-tab="composition">积木分析</button>`（如已存在则跳过），并在 `</main>` 之前追加：
  ```html
  <section id="composition" class="tab-panel">
    <iframe src="{YYYYMMDD}-{HHMMSS}-composition.html" style="width:100%; height:calc(100vh - 240px); min-height:600px; border:1px solid var(--border); border-radius:var(--radius-md); background:var(--surface);" title="规则积木与可组合性分析"></iframe>
  </section>
  ```
- Tell user: "积木视图已生成：`reports/{filename}`，并嵌入到 overview 的「积木分析」tab。回复 **打开积木视图** 单独打开，或 **打开** 回到 overview。"
- If user replies **打开积木视图**: 执行 `open reports/{filename}`。

## Note

本命令不修改任何文档，不做 commit。仅生成只读报告。
