复核业务知识的口径冲突、重复与失效引用，给出处理建议并按用户选择修改。条目编号：$ARGUMENTS

# 复核业务知识

整个命令按 `.claude/KNOWLEDGE_SEDIMENTATION.md` 执行，用户可见的话遵守 `.claude/USER_VOCABULARY.md`。

## Step 1：确定复核范围

- 知识沉淀页「在对话里复核」发来的消息带参数 `project_entry`（项目条目编号）与 `industry_entry`（行业条目编号，可能为空）：只复核这一对；
- `$ARGUMENTS` 给了条目编号：复核这一条与所有可能冲突的条目；
- 都没有：用 `python3 .claude/scripts/knowledge_helper.py list` 列出全部项目条目，逐条复核。

行业条目不在项目目录里，用 `python3 .claude/scripts/knowledge_helper.py load --scope global` 的输出查看（来源写着行业包名）；行业条目只读，要改口径就在项目层写一条同名条目覆盖它。

## Step 2：逐对判断（需要语义判断的部分在这里做）

对每一对候选，派出 `.claude/agents/knowledge-conflict-agent.md` 判断关系（重复 / 冲突 / 相关 / 无关），并对照资料里的原文核实哪一方有依据。

## Step 3：给出建议并等用户选择

每一对给出一句结论和建议：保留项目口径（覆盖行业条目）、改用行业口径（删掉项目条目）、合并成一条、两条都保留并互相关联。用户选定后按守则用 `knowledge_helper.py revise` / `remove` / `add` 落盘，最后汇总改了哪些条目。
