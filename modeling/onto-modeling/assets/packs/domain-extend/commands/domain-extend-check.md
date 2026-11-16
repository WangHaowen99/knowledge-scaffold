---
description: 核对底座：领域本体包条目是否和导入时逐字一致，新增条目有没有和底座重名
---
核对底座：领域本体包的条目是否和导入时逐字一致、底座锁定是否生效、新增条目有没有和底座重名。没通过不能发布。补充说明（可选）：$ARGUMENTS

## Step 0: 前置检查

遵循 `.claude/STEP0_TEMPLATE.md`，COMMAND_NAME = `domain-extend-check`。简述：

1. `python3 .claude/scripts/state_helper.py check-lock domain-extend-check`
2. `python3 .claude/scripts/state_helper.py check-prereq domain-extend-check`
3. `python3 .claude/scripts/state_helper.py acquire domain-extend-check start`

要问用户时先 `acquire domain-extend-check confirm`，用户回复后 `acquire domain-extend-check start`。

## Step 1: 跑核对

```bash
python3 .claude/scripts/domain_extend.py check
```

核对整个工作区，网页、Studio、MCP 上人工改的也算。报告在 `ext/domain-extend/check.md`（每节最多 50 条），完整清单在 `check.json`。

## Step 2: 处理待处理项

报告里每个 `[ ]` 都要处理完，报告才算通过：

- **底座改动**：先看改了什么（`git diff <导入提交> -- <条目路径>`，导入提交在报告第一行），向用户转述要点，不贴全文；说明底座不能改，
  想要的变化可以做成新增条目（/domain-extend-add）。用确认卡问：
  - **撤回（推荐）**：`python3 .claude/scripts/domain_extend.py restore` 把被改、被删的底座条目写回导入时的内容，再重跑 `check`；
  - **先不处理**：保持待处理，发布一直被挡。
  搬到别处的底座条目，撤回后原位置恢复，搬过去的那份会列成「和底座重名」，问用户删掉还是改名后当新增条目。
- **和底座重名的新增条目**：新增条目改个名字（链接到它的地方一起改），重跑 `check`。
- **底座没有锁定**：告诉用户运行 /domain-extend-bind（发布之后锁定会自动解除）。
- 「指不到的链接」只是提醒，不挡发布，转述给用户。

## Step 3: 收尾

`python3 .claude/scripts/state_helper.py release domain-extend-check`。结束语：底座是否和导入时一致、新增了多少条目；通过了就可以校验、发布。
