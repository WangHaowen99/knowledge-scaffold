Generate mock test data for all business objects. Topic (if any): $ARGUMENTS

## Step 0: 前置检查

遵循 `.claude/STEP0_TEMPLATE.md`，COMMAND_NAME = `mock`。简述：

1. `python3 .claude/scripts/state_helper.py check-lock mock`
   - `locked:...` → **错误恢复** 提示。
2. `python3 .claude/scripts/state_helper.py check-prereq mock`
   - `missing:对象已提取` → **跳步守卫**："继续会导致没有对象定义可生成测试数据"。选项 **继续** / **先做 对象提取**（/ontology-extract-object）。
3. `python3 .claude/scripts/state_helper.py acquire mock start`
4. 加载已沉淀业务知识。SCOPE_HINTS = `global` + 每个待 mock 的对象名（在 Step 1 选定 OBJECTS 列表后追加）：
   ```bash
   SCOPE_ARGS=(--scope global)
   for obj in $OBJECTS; do
       SCOPE_ARGS+=(--scope "object:$obj")
   done
   KNOWLEDGE_BLOCK=$(python3 .claude/scripts/knowledge_helper.py load "${SCOPE_ARGS[@]}")
   ```
   若 `KNOWLEDGE_BLOCK` 等于 `（暂无相关业务知识）`，置为空字符串以跳过注入。

   > 注：OBJECTS 在 Step 1 选定后才能确定，此 load 在 Step 1 完成后实际执行。

命令结束前 `release mock` 并 `update-progress`。

## 业务知识识别（贯穿整个命令）

整个 mock 命令期间始终遵循 `.claude/BUSINESS_KNOWLEDGE_SNIPPET.md`。
mock-data-agent 的 prompt 需 transclude 该文件全文。

## Step 1: Check prerequisites

List files in `ontology/object/`. If empty: "未找到业务对象文档，请先运行 `/ontology-extract-object` 提取对象。" Stop.

## Step 2: Check existing mock data

List files in `ontology/data-mock/`.

**If some exist:**
`python3 .claude/scripts/state_helper.py acquire mock confirm`，show counts and ask（回复后 `acquire mock start`）:
"发现 {M} 个对象文档，其中 {E} 个已有测试数据。

- 回复 **全部** → 全部重新生成（覆盖现有数据）
- 回复 **补充** → 只生成缺失的
- 回复 **指定 对象1 对象2** → 只生成指定对象的数据"

Set OBJECT_NAMES and EXISTING_FILES accordingly:
- **全部**: OBJECT_NAMES = all, EXISTING_FILES = empty
- **补充**: OBJECT_NAMES = all, EXISTING_FILES = existing filenames
- **指定**: OBJECT_NAMES = specified names, EXISTING_FILES = empty

**If none exist:** OBJECT_NAMES = all, EXISTING_FILES = empty.

## Step 3: Dispatch mock data agent

Read `.claude/agents/mock-data-agent.md`. Dispatch it as a subagent with:

```
{若 KNOWLEDGE_BLOCK 非空：原样粘贴 KNOWLEDGE_BLOCK 在此处，紧跟一行分隔符 ---}

OBJECTS_DIR: ontology/object/
MOCK_DIR: ontology/data-mock/
{如有指定: OBJECT_NAMES: {逗号分隔的对象名}}
{如有跳过: EXISTING_FILES: {逗号分隔的现有文件名}}

请按照你的指令生成所有测试数据，写入对应目录。

---

请阅读并遵循 `.claude/BUSINESS_KNOWLEDGE_SNIPPET.md` 的「业务知识识别守则」。
```

Wait for completion. Display summary to user.

## Step 3.5: Rule-level integrity check

Generate report filename: `reports/{YYYYMMDD}-{HHMMSS}-mock-rule.md`（命名与 `/ontology-validate` 的 `reports/{YYYYMMDD}-{HHMMSS}-{SCOPE}-rule.md` 对齐；这里 `SCOPE = mock`，日期时间使用 `Asia/Shanghai`）。Ensure `reports/` exists.

Run the ontology checker with `--include-mock` to verify primary-key uniqueness and foreign-key integrity:

```bash
python3 .claude/scripts/ontology_checker.py ontology/ --include-mock --json
```

Parse the JSON. Filter to issues whose `file` starts with `data-mock/`.

Write the mock rule report to `reports/{YYYYMMDD}-{HHMMSS}-mock-rule.md` using only filtered mock issues:

```markdown
## Mock 规则检查结果 {YYYY-MM-DD HH:MM} Asia/Shanghai
（共 {N} 个问题：{E} 错误 / {W} 警告 / {S} 建议）

### {data-mock/X.md}
- ❌ [主键重复] {message}
- ⚠️ [外键缺失] {message}
```

If no mock issues, write "（无问题，测试数据通过 mock 规则检查。）".

If any mock issues, `python3 .claude/scripts/state_helper.py acquire mock confirm` 后展示:

```
⚠️ 测试数据存在引用问题（{N} 条），已记入校验报告：

- {data-mock/X.md} → {message}
...

- 回复 **自动修复** → 让我尝试调整引用值使其对齐
- 回复 **保留** → 保留现状，照样保存
- 回复 **重新生成** → 重新生成一次测试数据
```

Handle reply（先 `acquire mock start`）:
- **自动修复**: for each `外键缺失` issue, locate the row in the mock file and replace the bad reference with a valid PK value from the target's 正常 mock data. Re-run Step 3.5 and regenerate the mock rule report for the latest data.
- **保留**: proceed to Step 4 with a note appended to the commit message.
- **重新生成**: go back to Step 3.

If no mock issues, tell user "测试数据引用完整性已通过检查。" and proceed.

## Step 4: User review

`python3 .claude/scripts/state_helper.py acquire mock confirm`，then:

"测试数据生成完成，共 {N} 个对象的数据。
如需修改请直接说明；确认无误请回复 **确认**。"

Allow user to modify specific mock data files before saving the version. If mock data files are modified, return to Step 3.5 before asking for final confirmation so the report matches the latest data.

## Step 4.5: 记下没有生成示例数据的对象

本轮没有生成示例数据的对象（不落库的概念对象、用户选择跳过的对象），逐个执行 `python3 .claude/scripts/trace_index.py mock-skipped --object <对象名> --reason "<一句原因>"`；重新全量生成时先加 `--clear` 清空旧记录。「场景走查」页的示例数据页签据此列出跳过的对象。

页面「按场景补生成」发来的消息带参数 `by: scenario`：只为场景里用到、还没有示例数据的对象生成。

## Step 5: Commit

When user confirms (回复 **确认** 或等价的肯定词，如"好的"、"可以"、"没问题"、"继续"、"是"): `acquire mock start`，then

Determine TOPIC:
- If $ARGUMENTS provided: use it
- Else: infer from research report filename or ask user

```bash
git add ontology/data-mock/ reports/ ext/trace/index/
git commit -m "mock: {TOPIC} test data"
```

## Step 6: Suggest next step

先 `python3 .claude/scripts/state_helper.py release mock` 与 `update-progress`，再用两三句话小结：生成了哪些对象的测试数据、还有什么要用户定；不列下一步菜单（界面的建议条会给出）。

If user later replies **追踪**: ask "要追踪哪个逻辑（如「创建订单」）？或回复 **全部** 追踪所有逻辑：", then read `.claude/commands/ontology-trace.md` and follow it with `ontology/operation/{用户说的逻辑}.md`（用户回复 **全部** 时传 `全部`）.
