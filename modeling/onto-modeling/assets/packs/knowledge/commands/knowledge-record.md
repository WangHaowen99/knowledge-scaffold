把对话里的一条纠错或业务惯例记成业务知识，入库前查重、查冲突。要记下的内容：$ARGUMENTS

# 记录业务知识

整个命令按 `.claude/KNOWLEDGE_SEDIMENTATION.md` 执行，用户可见的话遵守 `.claude/USER_VOCABULARY.md`。

## Step 1：确定要记什么

- `$ARGUMENTS` 不为空：把它当作要记下的内容；
- 为空：取最近几轮对话里用户纠正过你、或主动说明的业务口径，列出候选让用户选一条（最多 3 条）；
- 知识沉淀页发来的消息可能带参数（如 `source`：来源对话与消息），按参数定位那条消息。

把内容整理成一条候选：标题（一句话，≤60 字）、类型（纠错 / 惯例 / 术语 / 领域规则）、适用范围（全局，或具体的对象、操作、规则）、规则正文、背景（可选）、置信度。

## Step 2：查重与冲突（必做）

按守则「Add 前置：冲突检查」派出 `.claude/agents/knowledge-conflict-agent.md`，拿到结论后按守则给用户选择：覆盖、合并、仍新增、取消。

`python3 .claude/scripts/knowledge_helper.py load --scope global` 里带「行业」来源的条目来自行业包：新条目和它口径不同时，在正文里写明「本项目以此为准」，项目条目会覆盖同名的行业条目。

## Step 3：落盘

用户确认后按守则的「标准 add 调用」写入（`--after-conflict-check` 填 Step 2 的结论），然后告诉用户：已记入业务知识，之后的建模命令会自动加载。
