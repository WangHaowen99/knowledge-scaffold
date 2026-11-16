---
description: 在领域本体包底座上新增功能：只新建条目，底座条目不改
argument-hint: 要增加的功能
---
在领域本体包（底座）上新增功能：只新建对象、操作、规则、指标、脚本，用链接引用底座条目，底座条目一律不改。要增加的功能（可选，留空按对话里说的或新上传的资料）：$ARGUMENTS

## Step 0: 前置检查

遵循 `.claude/STEP0_TEMPLATE.md`，COMMAND_NAME = `domain-extend-add`。简述：

1. `python3 .claude/scripts/state_helper.py check-lock domain-extend-add`
2. `python3 .claude/scripts/state_helper.py check-prereq domain-extend-add`
3. `python3 .claude/scripts/state_helper.py acquire domain-extend-add start`
4. `python3 .claude/scripts/domain_extend.py status`：底座锁定不是「已锁定」→ 告诉用户先锁定底座（/domain-extend-bind），`release domain-extend-add` 后结束。
5. 业务知识按涉及的对象加载：`python3 .claude/scripts/knowledge_helper.py load --scope global --scope object:<对象名> ...`，
   遵循 `.claude/BUSINESS_KNOWLEDGE_SNIPPET.md`。

要问用户时先 `acquire domain-extend-add confirm`，用户回复后 `acquire domain-extend-add start`。

## Step 1: 弄清要什么、底座有什么

- 要增加的功能看 `$ARGUMENTS` 和对话；新上传的资料转好的文本在 `resource/text/` 下（如 `resource/others/需求.docx` → `resource/text/others/需求.docx.md`）。
  说不清要什么就先问，不要猜。
- 找相关底座条目：`python3 .claude/scripts/domain_extend.py catalog --q <关键字>`（或 `--kind 对象`），读它们的原文；底座全貌在 `ext/domain-extend/base.md`。

## Step 2: 规划，等确认

写 `ext/domain-extend/plan.md`：

- 目标一句话；
- 要新增的条目：类型、名称、作用、引用哪些底座条目（`[[名称]]`），关系写在哪个新条目上；
- 原本得改底座的地方怎么换成新增：
  - 底座对象要多存信息 → 新建扩展对象（如「订单配送扩展」），用底座对象的主键关联它，关系声明在扩展对象的「关联对象」里；
  - 底座操作要多几步 → 新建操作，先「调用[[底座操作]]逻辑文档」再做新增的步骤，或读底座操作的输出对象接着处理；
  - 底座规则、指标的口径要变 → 新建规则、指标，写清和底座那条的差别、适用场景，原条目不动；
  - 只有改底座才做得到的，列进「做不到的」，说明要去改领域本体包、发布新版本。
- 新增条目不能和底座条目同名。

要点写在正文里，用确认卡问（`.claude/INTERACTION_CONVENTIONS.md` 第 2 条），选项「确认」「调整」：确认 → Step 3；调整 → 按意见改 `plan.md` 再问。

## Step 3: 新建条目

- 按 `.claude/templates/` 的模板新建，资料没给的写 `TODO: 待补充{字段名}`，不编造；用 `[[链接]]` 引用底座条目。
- 新建操作里写了「调用[[X]]逻辑文档」，在「与operation的关联」里列 `[[X]]`；新增条目同步所在目录的 `index.md`。
- 新增脚本：`python3 .claude/scripts/script_verify.py run <脚本> --cases <用例文件>`，再
  `python3 .claude/scripts/script_descriptor.py render ontology`。
- 只建不改：写底座条目被拒，说明这处规划越界了，回 Step 2 改成新增的做法；不要换写法重试，也不要去「重新圈定」。

每建完一个条目向用户说一句（条目 + 作用），不贴全文。

## Step 4: 自检

- `python3 .claude/scripts/ontology_checker.py`：这次新增引入的硬错误清零；底座自身的问题不修，列给用户。
- `python3 .claude/scripts/domain_extend.py added`：看新增清单和各自引用的底座条目；提示「和底座条目重名」的新增条目改名（链接一起改），
  提示底座被改动的，告诉用户发布前要在 /domain-extend-check 里撤回。

## Step 5: 收尾

`python3 .claude/scripts/state_helper.py release domain-extend-add`。结束语：新增了几个条目、各引用了哪些底座条目，有没有「做不到的」；下一步 /domain-extend-check。
