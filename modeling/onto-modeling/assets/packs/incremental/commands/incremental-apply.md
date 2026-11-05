在本轮增量范围内修改：按 `incremental/scope.json` 的种子新增、修改、删除条目，传播链上要跟着改的一起改。补充说明（可选）：$ARGUMENTS

## Step 0: 前置检查

遵循 `.claude/STEP0_TEMPLATE.md`，COMMAND_NAME = `incremental-apply`。简述：

1. `python3 .claude/scripts/state_helper.py check-lock incremental-apply`
2. `python3 .claude/scripts/state_helper.py check-prereq incremental-apply`
3. `python3 .claude/scripts/state_helper.py acquire incremental-apply start`
4. 读 `incremental/scope.json`：没有或 `status` 不是 `active` → 告诉用户先圈定范围（/incremental-scope），`release incremental-apply` 后结束。
5. 业务知识按种子涉及的对象加载：`python3 .claude/scripts/knowledge_helper.py load --scope global --scope object:<对象名> ...`，
   遵循 `.claude/BUSINESS_KNOWLEDGE_SNIPPET.md`。

要问用户时先 `acquire incremental-apply confirm`，用户回复后 `acquire incremental-apply start`。

## Step 1: 改种子

逐个处理 `seeds`，写法和 /ontology-update 落盘一致（`.claude/agents/update-apply-agent.md` 的规则）：

- 新建用 `.claude/templates/` 的模板，资料没给的写 `TODO: 待补充{字段名}`，不编造；
- 修改只动要改的部分，不顺手整理别处；
- 删除前查谁还链接它，链接方在范围里的一起改掉；
- 新增、删除条目时同步 `index.md` 和 `[[链接]]`；操作里加了「调用[[X]]逻辑文档」，在「与operation的关联」里列 `[[X]]`；
- 改了 `ontology/scripts/*.py`：`python3 .claude/scripts/script_verify.py run <脚本> --cases <用例文件>`，再
  `python3 .claude/scripts/script_descriptor.py render ontology`。

每改完一个文件向用户说一句（文件 + 改了什么），不贴全文。

## Step 2: 跟着改传播链

`chain` 里 `include` 为 true 的是范围内的传播链，`trail` 是它和种子的关系。逐条判断要不要跟着改，要改的直接改。
写入被挡（「本轮增量范围外的条目只读」）说明那个条目不在范围里：不要换写法绕过，告诉用户要改它得重新圈定（/incremental-scope）。

## Step 3: 自检

`python3 .claude/scripts/ontology_checker.py`：硬错误清零，这次引入的警告修掉或向用户说明。

## Step 4: 收尾

```bash
python3 .claude/scripts/incremental_scope.py applied [--skip <种子> "<不改的理由>"]
python3 .claude/scripts/state_helper.py release incremental-apply
```

`applied` 列出没处理的种子时：处理掉，或和用户确认后用 `--skip` 写明理由，再重跑。
结束语：改了几个条目，下一步 /incremental-check 核对传播。
