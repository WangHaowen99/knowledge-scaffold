Master entry point for domain knowledge modeling. Domain (if any): $ARGUMENTS

## Step 1: Detect current phase

First, try `python3 .claude/scripts/state_helper.py show-progress` to read the persisted progress snapshot. Parse its JSON output for context (`last_command`, `stage`, `validate_last_run`, etc). If missing or empty, continue.

Then compute the authoritative current stage:

```bash
python3 .claude/scripts/state_helper.py detect-stage
```

Output is one of: `未开始 / 资料分析待核验 / 资料分析完成 / 流程图就绪 / 对象已提取 / 逻辑已提取 / 校验待审阅 / 完成`。

Also run `python3 .claude/scripts/state_helper.py check-lock <command>`（替换 `<command>` 为待诊断的具体命令名，如 `extract-object` / `extract-operation` / `validate` / `codegen`）for any command you suspect is stuck (optional; only when diagnosing "is there a stuck task?") — skipped on happy path.

Before showing the status block, also look for an 引导 trigger: if $ARGUMENTS equals `示例`（或 `例子` / `demo`），跳到 **Step 3.5: 示例模式**。

## Step 2: If $ARGUMENTS provided

If the user passed a domain name (e.g. `/ontology 电商`), treat it as starting fresh research regardless of current state. Read `.claude/commands/ontology-research.md` and follow it, using $ARGUMENTS as the topic. Stop after research completes.

## Step 3: Act based on detected state

每个状态块里的 `{next-step}` 来自主流程定义（`.claude/flow.yaml`），不要自己编：用 Step 1
检测到的阶段名运行

```bash
python3 .claude/scripts/state_helper.py next-step <阶段名>
```

把输出逐行原样放在 `{next-step}` 的位置，不增不减、不改措辞（输出为空时整行省略）。

**未开始:**
```
欢迎使用 Ontology Toolset。

**开始方式：**
{next-step}
```

After showing, listen for the user's next reply:
- **资料分析** → read `.claude/commands/ontology-research.md` and follow it (ask for TOPIC if not given)
- **流程图** → read `.claude/commands/ontology-flowchart.md`（图示能力；文件不在说明本项目没启用，告诉用户去项目设置启用） and follow it
- other → treat as normal input

**资料分析待核验:**

`research/` 下有报告，但没有通过内容验收的回执（新报告未核验、报告或源资料改动后
回执失效、或旧项目本来就没有回执）。先跑检查拿到具体结论，再决定怎么说：

```bash
python3 .claude/scripts/research_checker.py check
```

```
当前状态：资料分析待核验 ⏳
报告：research/{latest_filename}
{退出码 0：确定性检查已通过，阶段随即变为「资料分析完成」}
{退出码 1：逐条列出错误项与待人工确认条目}

**下一步建议：**
{next-step}
```

Handle reply:
- **修正** / **人工确认** → 按 `.claude/commands/ontology-research.md` 的 Step 4.5 处理
- **重新做资料分析** → read `.claude/commands/ontology-research.md` and follow it
- **提取对象** → 用户坚持时按 `.claude/STEP0_TEMPLATE.md` 的 **跳步守卫** 先提示后果，
  再由用户决定；不要主动推荐这条路。

**资料分析完成:**

Find the latest file in `research/`. Show:
```
当前状态：资料分析完成 ✅
报告：research/{latest_filename}

**下一步建议：**
{next-step}
上述选项只是参考，你也可以直接说明想修改、补充、校验或调整的内容，不必局限于这些建议。
```

Handle reply:
- **流程图** → read `.claude/commands/ontology-flowchart.md`（图示能力；文件不在说明本项目没启用，告诉用户去项目设置启用） and follow it
- **提取对象** → read `.claude/commands/ontology-extract-object.md` and follow it（Step 1 会识别到只有研究报告，进入模式 C）
- **追加资料分析** → read `.claude/commands/ontology-research.md` (it will detect existing report and prompt for append mode)
- **完成** → stop

**流程图就绪:**

Count .drawio files in `ontology/diagrams/`. Show:
```
当前状态：流程图就绪 ✅
流程图：{N} 个（ontology/diagrams/）

**下一步建议：**
{next-step}
上述选项只是参考，你也可以直接说明想修改、补充、校验或调整的内容，不必局限于这些建议。
```

Handle reply:
- **提取对象** → read `.claude/commands/ontology-extract-object.md` and follow it
- **校验流程图** → read `.claude/commands/ontology-validate-diagrams.md`（图示能力；文件不在说明本项目没启用，告诉用户去项目设置启用） and follow it
- **完成** → stop

**对象已提取:**

Count .md files in `ontology/object/`. Show:
```
当前状态：对象已提取 ✅
对象：{M} 个（ontology/object/）

**下一步建议：**
{next-step}
上述选项只是参考，你也可以直接说明想修改、补充、校验或调整的内容，不必局限于这些建议。
```

Handle reply:
- **提取逻辑** → read `.claude/commands/ontology-extract-operation.md` and follow it
- **完成** → stop

**逻辑已提取:**

Count .md files in `ontology/object/`、`ontology/operation/`、`ontology/rule/` 与 `ontology/metric/`（跳过 index.md）. Show:
```
当前状态：提取完成 ✅
对象：{M} 个  |  逻辑：{K} 个  |  规则：{R} 条  |  指标：{P} 个

**下一步建议：**
{next-step}
上述选项只是参考，你也可以直接说明想修改、补充、校验或调整的内容，不必局限于这些建议。
```

Handle reply:
- **校验** → read `.claude/commands/ontology-validate.md` and follow it
- **规则与指标** → read `.claude/commands/ontology-extract-rule-metric.md` and follow it
- **整体视图** → read `.claude/commands/ontology-overview.md` and follow it
- **更新** → read `.claude/commands/ontology-update.md` and follow it
- **完成** → stop

**校验待审阅:**

Find the latest file in `reports/`. Count remaining `[ ]` items. Show:
```
当前状态：校验待审阅 ⏳
问题清单：reports/{latest_filename}（{N} 个问题待处理）

**下一步建议：**
{next-step}
上述选项只是参考，你也可以直接说明想修改、补充、校验或调整的内容，不必局限于这些建议。
```

Handle reply:
- **继续校验** → read `.claude/commands/ontology-validate.md` and follow it
- **完成** → stop

**完成:**

Count all docs. Get last git log line. Show:
```
当前建模状态：完成 ✅

  对象：{M} 个（ontology/object/）
  逻辑：{K} 个（ontology/operation/）
  规则：{R} 条（ontology/rule/）  指标：{P} 个（ontology/metric/）
  上次提交：{git log --oneline -1}

**可选操作：**
{next-step}
上述选项只是参考，你也可以直接说明想修改、补充、校验或调整的内容，不必局限于这些建议。
```

Handle reply by reading the corresponding command file and following it.

---

## Step 3.5: 示例模式（Demo Mode）

当 $ARGUMENTS 等于 `示例` / `例子` / `demo` 时进入本步骤。

目标：让从未用过本工具的新用户，**零投入** 看到完整流程：资料分析 → 流程图 → 对象提取 → 逻辑提取 → 校验 → 追踪 → 代码生成。

### 准备环境

先检查 `research/` / `ontology/diagrams/` / `ontology/object/` 是否已有内容：

- **若任一已非空**：警告用户"当前项目已有数据，示例模式会在临时位置写入，不影响你的内容"。询问是否继续。若继续，设置 `DEMO_ROOT = ontology-demo/`（整棵示例树放在此处，不污染主目录）。
- **若全部为空**：直接使用主目录作为示例，`DEMO_ROOT = ""`（空字符串，代表主目录）。

### 注入示例内容

运行：

```bash
python3 .claude/scripts/seed_example.py {DEMO_ROOT}
```

该脚本会创建：
- `{DEMO_ROOT}research/20260417-外卖订单.md` — 一份 500 字左右的迷你资料分析报告
- `{DEMO_ROOT}ontology/diagrams/下单流程.drawio` — 一张简化的下单流程图
- `{DEMO_ROOT}ontology/diagrams/配送流程.drawio` — 一张配送流程图
- `{DEMO_ROOT}ontology/diagrams-summary/下单流程.md`、`配送流程.md` — 自动生成的摘要

完成后告知用户：

```
示例已就绪。迷你领域：**外卖订单**。

接下来你可以：
- 回复 **提取对象**（/ontology-extract-object）→ 从示例流程图提取对象
- 回复 **完整走一遍** → 自动依次跑 提取 → 校验 → 测试数据 → 追踪 → 代码生成（每步之间暂停让你看结果）
- 回复 **交互走查** → 用 `/ontology-trace 交互` 逐个分支构造 testcase 和正常 mock 数据
- 回复 **清理示例** → 删除示例数据恢复空项目（仅删除 seed_example.py 写入的文件）
```

### 处理回复

- **提取对象**：读取并执行 `/ontology-extract-object` 指令（示例已备好流程图摘要，可直接开始）。
- **完整走一遍**：依次运行
  1. `/ontology-extract-object`
  2. `/ontology-extract-operation`
  3. `/ontology-validate`
  4. `/ontology-mock`
  5. `/ontology-trace 全部`
  6. `/ontology-codegen`
  每步完成后 pause 告知"已完成 N/6 步"并让用户决定是否继续下一步。
- **清理示例**：运行 `python3 .claude/scripts/seed_example.py --clean {DEMO_ROOT}` 并告知已清理。
