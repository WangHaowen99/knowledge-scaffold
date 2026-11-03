全局一致性检查：各段建完之后，看关系、口径和分段之间的衔接。参数：$ARGUMENTS

## Step 0: 前置检查

遵循 `.claude/STEP0_TEMPLATE.md`，COMMAND_NAME = `consistency`：

1. `python3 .claude/scripts/state_helper.py check-lock consistency`
2. `python3 .claude/scripts/state_helper.py check-prereq consistency`（缺对象或操作时按话术说明先做建模）
3. `python3 .claude/scripts/state_helper.py acquire consistency check`

命令结束前执行 `release consistency`。

## Step 1: 跑检查

```bash
python3 .claude/scripts/consistency_check.py --json
```

脚本写 `reports/consistency/consistency.md`（覆盖上一份，上一份里勾掉的同一条结论保留），输出 JSON：
`pending` 是未处理条数，`findings[]` 每条有 `id`、`code`、`level`、`text`、`target`。退出码 2 表示还没有可检查的条目。

不要自己另写一份报告，也不要改脚本的结论。

## Step 2: 处理

没有未处理条目：告诉用户「全局一致性检查通过」，跳到 Step 4。

有未处理条目时，按 `code` 分别处理，**修改只落在检查点到的条目上**：

- `dangling_link`：链接写错了就改成已有条目的名字；确实缺条目时列出来，问用户要不要补（补的话走对应的建模命令）。
- `orphan_object`：先看资料里它是不是确实没人用；没用就问用户删不删，有用就补上关联。
- `term_variant`：按蓝图里统一的叫法改正文（`[[链接]]` 的显示名也一起改）。
- `undeclared_dependency`：问用户是要在蓝图里加上这层依赖（建模蓝图页「调整分段」），还是改成不引用那一段的对象。
- `unassigned`、`planned_missing`：列出来给用户看，按用户的决定补建、改归属或在蓝图里去掉。

需要用户拍板的，先 `python3 .claude/scripts/state_helper.py acquire consistency confirm`，再按 `INTERACTION_CONVENTIONS.md` 发单选卡，一次一个问题，不要默认选第一个；用户回复后 `acquire consistency check`。
用户决定「就这样」的条目，在报告里把那一行改成 `- [x]` 并在行尾补一句结论。

## Step 3: 复查

改完后重跑 Step 1，直到没有未处理条目，或者剩下的都要用户决定。最多两轮；两轮后还有的，列出来交给用户。

## Step 4: 提交与收尾

```bash
git add ontology/ reports/consistency/
git commit -m "consistency: 全局一致性检查"
python3 .claude/scripts/state_helper.py release consistency
```

释放锁后用两三句话小结：查出几条、改了哪些、还有哪些要用户定；不列下一步菜单（界面的建议条会给出）。
