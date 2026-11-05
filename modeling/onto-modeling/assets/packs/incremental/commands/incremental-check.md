核对本轮增量改动的传播：从基线到现在改了哪些条目，传播链上谁还没确认，有没有范围外的改动。补充说明（可选）：$ARGUMENTS

## Step 0: 前置检查

遵循 `.claude/STEP0_TEMPLATE.md`，COMMAND_NAME = `incremental-check`。简述：

1. `python3 .claude/scripts/state_helper.py check-lock incremental-check`
2. `python3 .claude/scripts/state_helper.py check-prereq incremental-check`
3. `python3 .claude/scripts/state_helper.py acquire incremental-check start`

要问用户时先 `acquire incremental-check confirm`，用户回复后 `acquire incremental-check start`。

## Step 1: 跑核对

```bash
python3 .claude/scripts/incremental_check.py run
```

核对整个工作区，网页上人工改的条目也算。输出改动统计、范围外改动、待确认项（最多列 20 条），
报告写在 `incremental/check.md`（每节最多 50 条），完整清单在 `incremental/check.json`。

## Step 2: 处理待确认项

「待确认」里每个 `[ ]` 是改动条目传播链上、自己没改的条目。每个都先读它（和路径上的改动条目）再判断，不能没读就标：

- 要改：范围内的直接改（写法同 /incremental-apply Step 1）；标了「范围外」的告诉用户要先重新圈定（/incremental-scope）。
- 不受影响：`python3 .claude/scripts/incremental_check.py mark <条目> [<条目> ...] --reason "<为什么不受影响>"`，
  理由要具体，如「只引用订单编号，没用到改动的配送时效」。

`mark` 会重算报告；改了条目要重跑 `run`。处理完一批，报告会列出下一批，直到没有 `[ ]`。

## Step 3: 报告

- 范围外改动（含网页上人工改的）也是 `[ ]`，不能用 `mark` 清：原样告诉用户，问是并进范围（/incremental-scope 重新圈定）还是由建模人员在网页上撤回，处理后重跑 `run`；
- 改动占比超过提醒线时转述提醒，和用户确认要不要拆成几轮。

`python3 .claude/scripts/state_helper.py release incremental-check`。结束语：核对通过后可以校验、发布；要结束这一轮，说「结束本轮」。
