圈定这一轮增量构建的范围：列出要新增、修改、删除的条目（种子），算出传播链，用户确认后范围外的条目这一轮只读。这一轮要改什么（可选，留空按对话里说的；「结束本轮」表示结束当前一轮）：$ARGUMENTS

## Step 0: 前置检查

遵循 `.claude/STEP0_TEMPLATE.md`，COMMAND_NAME = `incremental-scope`。简述：

1. `python3 .claude/scripts/state_helper.py check-lock incremental-scope`
2. `python3 .claude/scripts/state_helper.py check-prereq incremental-scope`
   - `missing:...` → 跳步守卫："还没有建好的本体，增量构建没有可圈定的条目"。选项 **先做建模**（/ontology-extract-object）/ **取消**。
3. `python3 .claude/scripts/state_helper.py acquire incremental-scope start`

**结束本轮**（用户说「结束本轮」，或 `$ARGUMENTS` 是「结束本轮」）：运行
`python3 .claude/scripts/incremental_scope.py close`，转述输出，`release incremental-scope` 后结束，不做下面的步骤。

## Step 1: 列种子

先看 `incremental/scope.md`（有的话）了解当前一轮。按用户的要求定种子，每个种子三样：

- 动作：`add` 新增、`modify` 修改、`delete` 删除；
- 条目：`ontology/` 下 object、operation、logic、rule、metric、action、data-mock、testcase、scripts 里的单个文件；
- 理由：一句话，说清为什么要动它。

只列用户要求直接涉及的条目，跟着要改的交给传播链。拿不准的列进种子，在理由里写明。

## Step 2: 起草

```bash
python3 .claude/scripts/incremental_scope.py draft --goal "<一句话目标>" \
  --seed modify ontology/object/订单.md "增加配送时效字段" \
  --seed add ontology/rule/超时配送.md "新增超时判定规则"
```

- 传播链按引用反向走，默认 2 层；用户要求更大或更小时加 `--depth N`（最多 5）。
- 输出报种子有问题（不存在、动作不对、没写理由）就改了重跑。

## Step 3: 等确认

先 `python3 .claude/scripts/state_helper.py acquire incremental-scope confirm`，再给用户看
`incremental/scope.draft.md` 的要点：种子、传播链条数、链上每一条为什么在（如「订单 → 下单（引用对象）」）。

- 回复 **确认** → 按草稿生效；
- 回复 **去掉 <条目>** → 这些传播链上的条目不放进范围（种子去不掉，要去掉种子回 Step 1 重新起草）；
- 回复 **调整** → 回 Step 1。

草稿不算范围。确认后写出 `incremental/scope.json`：收紧马上生效；放宽（扩大范围、结束本轮）要这一轮对话结束、合入主线后才生效。

## Step 4: 生效

```bash
python3 .claude/scripts/state_helper.py acquire incremental-scope start
python3 .claude/scripts/incremental_scope.py confirm [--exclude <条目> ...]
python3 .claude/scripts/state_helper.py release incremental-scope
```

结束语两三句：范围已生效，范围外的条目只读，新增条目不受限（这次是扩大范围的话，新放开的条目下一轮对话才能改）；下一步 /incremental-apply。

## 说明

- 上一轮没结束时再圈定是扩大范围：基线沿用，种子和可改条目并进来，前面改过的照样要核对。要换一个目标另起一轮，先「结束本轮」。
- 每次确认，上一次的范围、修改记录和核对报告归档到 `incremental/history/<轮次>/`，范围内修改和传播核对要重新做。
- `incremental/` 下的文件一律经脚本写，不直接编辑；范围文件只有本命令运行时能写。
