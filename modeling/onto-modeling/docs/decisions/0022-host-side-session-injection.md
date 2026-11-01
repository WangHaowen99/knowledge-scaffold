# 0022 阶段提示与业务知识信号改由宿主注入，Jiuwen 先接

## Context

工具箱里有两个给模型补上下文的 hook：

- SessionStart 的 `session_start_hook.sh`：调 `state_helper.py detect-stage` 读阶段，再列出进行中的命令
  （`.ontology-state/lock-*.json`）和最近一份报告的 `- [ ]` 待处理数；
- UserPromptSubmit 的 `knowledge_signal_probe.sh`：在用户消息里找纠错信号词和业务常识/规范信号词，
  命中就提醒模型按 `BUSINESS_KNOWLEDGE_SNIPPET.md` 评估要不要沉淀为业务知识。

这两个 hook 在 Claude 引擎下都生效（输出的是 additionalContext）。Jiuwen 引擎只执行 PreToolUse 守卫
（`jiuwen_assets.ClaudeHooksRail`），所以 Jiuwen 会话既不知道项目阶段，也识别不了业务知识信号。
此外，两个引擎都有同一个问题：阶段提示只在会话开始时给一次，会话常驻期间阶段变了不会刷新
（后端改造方案第 1 章对 6.1、12#4 的更正）。

插件化详细设计 4.2 节第 4 条和后端改造方案 B4 要求这类提示改由宿主在两轮之间注入，不再依赖 hook；
P0 先补 Jiuwen，P2 两个引擎都走宿主注入（引擎协议 `send(text, context)`，见 B15），之后两个 hook 退役（B16、D16）。

## Decision

### 1. 注入逻辑放在宿主侧的独立模块

新增 `backend/app/services/session_host/injection.py`（方案第 3 章的目标布局），不依赖任何引擎，也不依赖会话层：

```python
build_context(project_root, *, user_text: str, session_start: bool, last_injected: str | None) -> InjectionResult
compose_query(user_text: str, context: str) -> str
```

- `InjectionResult` 有 `text`（拼好的注入文本，空串表示本条不注入）、`digest`（模型已看到的阶段提示摘要，
  下一条消息作为 `last_injected` 传回）、`stage_hint`、`knowledge_hint`；
- 阶段提示：与 hook 输出逐字一致。阶段用子进程跑**资产目录**里的 `state_helper.py detect-stage`
  （不跑工作区 `.claude/` 下可被改写的那份），环境用 `script_env.minimal_env()`，超时 15 秒；
  锁和报告直接读文件。`session_start` 为真时必带；之后只在内容变了时再带，阶段在会话常驻期间变了也会刷新；
- 业务知识信号：探测规则移植成纯 Python 函数，不再依赖 shell。每条消息单独判断，探测的是用户原文，
  不是斜杠命令展开后的全文（命令文档里本来就有「必须」「通常」）；
- `compose_query` 把注入文本包在 `<system-reminder>` 里放到消息前面，注明不是用户的原话。

### 2. Jiuwen 在消息真正送达模型时调用

- `JiuwenEngine.send` 只记用户原文；轮内追加的消息照旧积压，轮结束后逐条接续；
- 每条消息送进 `Runner.run_agent_streaming` 之前（`_model_query`）才展开斜杠命令、调用 `build_context`、拼 query。
  积压的消息拿到的是送达那一刻的阶段，而不是发出那一刻的；
- 引擎实例的第一条消息（新建、回收后重连、续接）算会话开始；被打断的一轮未必进了存档，
  下一条消息重新带阶段提示；
- 注入只影响发给模型的 query：会话层按原文记录 `user_message`，历史回放也只用原文；
- 注入出错只记运维留档（kind `injection`），消息照常发送，不阻断建模。

### 3. Claude 暂时不动

Claude 引擎继续靠两个 hook，hook 脚本的行为不改。P2 Claude 改走同一个入口（SDK 的 UserPromptSubmit
回调或消息前缀），之后两个 hook 的提示逻辑退役；`state_helper.py` 里没人用的 `STAGE_NEXT_COMMAND` 随 B16 删除。

## Alternatives considered

- **让 Jiuwen 也执行 SessionStart / UserPromptSubmit hook**：要在框架里找对应的回调点；SessionStart 只在
  会话开始时执行一次，「会话常驻期间阶段变了不刷新」的问题仍然在；P2 本来就要退役这两个 hook，
  不值得再给 Jiuwen 接一遍。
- **把逻辑直接写在 Jiuwen 引擎里**：P2 Claude 要接时还得再搬一次，两个引擎的提示也容易分叉。
- **在会话层 `_deliver` 时计算后传给引擎**：积压在引擎里的消息要等当前一轮结束才送达，提前算的阶段可能
  已经过时；`send(text, context)` 协议也要到 P1 引擎协议（B15）才定。P2 接入时由宿主在消息送达时提供上下文
  （送达时机仍由引擎决定），入口函数不变。
- **阶段提示每条消息都带**：上下文重复，摘要比对后只在变化时带。

## Consequences

- Jiuwen 会话开始时知道项目阶段、进行中的命令和最近报告，用户发消息时能识别业务知识信号
  （详细设计第 13 章 P0 验收：Jiuwen 下能识别业务知识信号）；阶段变了下一条消息会带上新阶段，这一点比 Claude 现状更及时。
- Jiuwen 每条消息送达前多起一次 `python3` 子进程读阶段（与 Claude 下 hook 的开销同量级）。
- 和 hook 版的差异：
  - hook 先用 `git rev-parse --show-toplevel` 定位工作区，不在 git 仓库里不输出；宿主直接用会话的工作区目录，
    只检查 `.claude/scripts` 是否存在；
  - hook 被 Claude 判超时时整条不输出；宿主读阶段超时（15 秒）时本条不带阶段提示，知识信号照常；
  - 阶段读取跑资产目录里的 `state_helper.py`，不跑工作区副本；
  - 不再有 SessionStart 与 UserPromptSubmit 两条消息之分，两段拼在同一个 `<system-reminder>` 里。
- 两份信号词表要同步改：`knowledge_signal_probe.sh` 与 `injection.py` 的 `CORRECTION_SIGNALS`、
  `CONVENTION_SIGNALS`；`tests/test_session_injection.py` 用 hook 脚本本体做对照，改漏了会失败。
- 方案 B4 里的「下一步」「未结束的执行」「自主构建指令」还没有（hook 本身也不输出下一步）：分别要等流程运行时的
  `suggest`（D16）、执行记录（B5）和自主构建的运行控制（详细设计 8.5），到时加进 `build_context`。
