---
description: 锁定领域本体包底座：底座条目只读，新增条目不受限
---
锁定领域本体包底座：之后助手改不动、删不掉底座条目，新增条目不受限。发布之后锁定自动解除，要再锁一次。（不需要参数）$ARGUMENTS

## Step 0: 前置检查

遵循 `.claude/STEP0_TEMPLATE.md`，COMMAND_NAME = `domain-extend-bind`。简述：

1. `python3 .claude/scripts/state_helper.py check-lock domain-extend-bind`
2. `python3 .claude/scripts/state_helper.py check-prereq domain-extend-bind`
   - `missing:...` → 跳步守卫："这个项目还没有本体条目，领域包扩展要以导入的领域本体包为底座"。选项 **导入领域本体包**（在项目列表从知识包 Hub 导入，或上传发布包）/ **取消**。
3. `python3 .claude/scripts/state_helper.py acquire domain-extend-bind start`

要问用户时先 `acquire domain-extend-bind confirm`，用户回复后 `acquire domain-extend-bind start`。

## Step 1: 锁定

```bash
python3 .claude/scripts/domain_extend.py bind
```

- 报「不是从已发布的领域本体包导入的」：原样告诉用户，领域包扩展只能用在从领域本体包导入的项目上；`release domain-extend-bind` 后结束。
- 其他报错原样转述，不要手写 `ext/domain-extend/` 下的文件。

## Step 2: 收尾

`python3 .claude/scripts/state_helper.py release domain-extend-bind`

结束语两三句：底座是哪个领域本体包、多少条目（按类型），锁定已生效（这一轮里已经改不了底座，之后一直有效，发布后要重新锁定）；
新功能下一步用 /domain-extend-add 说要增加什么。

## 说明

- 这条命令不会放开任何底座条目：底座一直只读，再运行一次只是刷新锁定（比如发布之后）。
- `ext/domain-extend/` 下的文件一律经脚本写；范围文件只有本命令运行时能写，直接改它也放不开底座。
