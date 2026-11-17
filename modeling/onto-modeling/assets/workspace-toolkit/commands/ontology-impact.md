Analyze downstream impact of modifying an ontology document. Target (file path or name): $ARGUMENTS

## Step 0: 前置检查

这是**只读分析命令**，不需要锁。按 `.claude/STEP0_TEMPLATE.md` 的约定只做第 2 步（阶段前置）：

```bash
python3 .claude/scripts/state_helper.py check-prereq impact
```

- 输出 `ok` → 继续。
- 输出 `missing:对象已提取,...` → 提示"当前缺少 {阶段列表}，影响分析需要先有对象文档。回复 **先做 对象提取**（/ontology-extract-object）/ **继续**（强行分析，可能结果为空）/ **取消**。"

若最终仍是空仓库，可回退提示："当前无本体数据，无法做影响分析。回复 **资料分析** 开始建模。"并停止。

## Step 1: Parse $ARGUMENTS

- **If empty**: Ask "请输入要分析的对象/逻辑名称（如 `订单`），或完整路径（如 `ontology/object/订单.md`）："
- Otherwise accept as `TARGET`.

## Step 2: Run impact analysis

```bash
python3 .claude/scripts/impact_analysis.py "{TARGET}"
```

The script auto-detects whether TARGET is a bare stem or a path, and which category (object/operation/mock) it belongs to.

## Step 3: Present result

Directly show the script's markdown output. It already contains:
- 下游逻辑文档
- 调用此子流程的父流程（若目标是逻辑）
- 引用此目标的对象
- 相关测试数据
- 流程图及摘要（只读候选，需另行复核并同步）
- 提示（advisory notes — 基于目标类型给出的修改建议）

If the script returns a non-empty `diagram_scan.next_offset` in JSON or a pagination hint in markdown, continue with `--diagram-offset {next_offset} --diagram-limit {limit}` until all diagram candidates have been presented. Do not present only the first page as the full list.

List every diagram/summary candidate with its complete `file` path and its own `reason`. Long lists may be split into sections, but never replace entries with filename ranges or abbreviated numeric lists. Calculate each category count from the de-duplicated entries; distinguish literal formulas, name/reference candidates and unreadable resources. Do not describe all candidates as containing an old formula.

If the script reports no static references, explain the static scan boundary and review business semantics before drawing a conclusion. Never turn an empty result into a guarantee that modifying the document cannot affect other resources. Unreadable/unsupported diagrams must remain explicit pending-review reminders.

## Step 4: Suggest next step

```
**下一步建议：**
- 回复 **校验** → 修改后跑一次 /ontology-validate 确认无残留问题
- 回复 **追踪** → 若改的是逻辑/对象，用 /ontology-trace 重走场景
上述选项只是参考，你也可以直接说明想修改、补充、校验或调整的内容，不必局限于这些建议。
```

Handle the reply by reading the corresponding command file and following it.
