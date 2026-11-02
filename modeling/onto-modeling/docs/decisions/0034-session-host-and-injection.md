# 0034 会话宿主：对话流按契约逐类给载荷，两个引擎都由宿主注入，三条事件流

## Context

详设 4.2（会话宿主）、6.10（统一下一步）与后端改造方案 B3、B4、B7、B11 要求：

- 阶段提示和下一步由宿主在两轮之间注入，两个引擎都能拿到；ADR-0022 先给 Jiuwen 接上，Claude 仍靠
  `session_start_hook.sh`、`knowledge_signal_probe.sh` 两个 hook；
- 对话流事件按 API 契约第 4.2 节逐类给载荷（B2 只换了信封和事件名，载荷还是会话层的旧字段），前端的
  对话状态机已按契约写好；项目流 P-15、用户流 A-12 还没有，`flow.updated`、`assembly.updated`、
  `extensions.updated`、`workspace.changed` 没有地方发；
- 模型配置归属固定为项目所有者（B3），去掉「谁发消息用谁的 Key、换人 409」；会话身份进 agent 的
  命令环境（B7）。

## Decision

### 1. 对话时间线：引擎事件 → 宿主记录 → 对话流事件（`session_host/timeline.py`）

- JSONL 历史一行一条宿主记录，行号是记录的 seq；**对外事件 id = seq + 1**，从 1 开始。`after=n` 补发 id
  大于 n 的持久事件，不带 `after` 从头回放；瞬时事件（`message.delta`、`context.usage`）不带 id、不回放。
  旧历史的行号不变，前端拿到的 id 只是整体加一。
- **一条记录至多投影成一条事件**。引擎的一条助手消息在宿主这里先拆开：正文一条（`message.assistant`），
  每个工具调用一条（`tool.activity`，internal），「工作过程」组一条（`progress` / `progress.updated`）。
  只给内部用的记录（逐次用量、子代理的内部消息）投影为空，对外 id 留空号。
- 「工作过程」组按轮：工具调用写成业务话（只给文件名，不给命令原文），子代理、执行阶段变化各一步，
  最多留 30 步，轮结束收起；标题是这条命令的业务名（执行记录的节点标题）。
- AskUserQuestion 改成决策卡（`question.asked`，kind `decision`，一题一张，末尾一项「其他，我来说」），
  S-11 按题回答，一个提问的题都答完才把答案交给引擎；其余确认卡补上标题、业务话说明、技术细节、超时。
  等待回答期间界面状态是 `waiting_user`（只发给界面，不改会话池调度用的状态）。
- 出错卡带错误编号（与运维错误留档 O-50 的 `[err-xxxxxx]` 前缀同一个）、重试次数；中断标记带执行 ID、
  「你在什么阶段打断的」、已保存的内容；一轮结束、打断之后发 `suggestions.updated`（与 FL-02 同一份；
  打断后主推「从打断处继续」）。
- 旧版历史照样能投影：缺的字段给默认值，旧的中断原因（stale、ops_disconnect）映射到契约的 `by`。
- S-06 给卡片、工作过程组的最新内容（放在第一次出现的位置），问题带上回答后的状态。

### 2. 两个引擎都由宿主注入（`session_host/injection.py`）

- 宿主给每个引擎实例建一个 `Injector`，经 `EngineContext.injector` 交给引擎；内容（阶段、统一下一步、
  业务知识信号）与去重状态在宿主这一份，引擎只决定「消息送到模型的那一刻」：
  - Claude：SDK 的 UserPromptSubmit 回调（在后端进程里跑），返回 additionalContext。不用消息前缀——
    前缀会让斜杠命令不再以 `/` 开头，CLI 不展开；
  - Jiuwen：轮内积压的消息开始跑时拼进 query（沿用 ADR-0022）。被打断时 `forget()`，下一条重新带上。
- 两个 hook 退役：从内核守卫与 hooks 基线里删掉，装配目录不再带。逐字一致由原 hook 跑出的期望文本守着。
- 统一「下一步」的输入：`register_next_step_provider` 是数据源登记点，B4b 的 `state_helper suggest`
  接这里；登记前用 FL-02 的建议。下一步计入去重摘要，变了才再带。
- Jiuwen 跑在独立 worker 里时（ADR-0038），Injector 在 worker 里建，代码与内容同一份。

### 3. 三条事件流（`services/streams.py`、`services/project_events.py`）

- 对话流的持久事件在 JSONL 历史里；项目流、用户流不落盘，每个项目 / 用户一个频道，id 是 `<epoch>.<n>`，
  留最近 2000 条或 30 分钟作回放缓冲。epoch 不同、被挤出缓冲、订阅者处理太慢撑满队列时先发
  `stream.reset`（带要重拉的快照接口）；15 秒心跳，令牌到期发 `auth.expired` 后关流；每个用户同时最多 8 条
  （429 `too_many_streams`）。发布方可以在任意线程调用，推给订阅者经它的事件循环。
- 接上的事件：写入事务的 `workspace.changed`（agent 的提交由宿主每轮结束补发，来源 assistant）；
  工作区变了、一轮结束、装配切换后重算流程，只发变了的节点（`flow.updated`）；锁文件提交与消息前切换装配
  后的 `assembly.updated`、`extensions.updated`；执行记录 `exec.updated`、页面命令 `command.progress`；
  对话、项目、维护状态；用户流的项目卡片与项目移除。
- 留给以后的登记点：写入范围锁的数据源（`register_scope_provider`，B4b；登记前一律「没有锁」，
  一轮开始、结束时发 `scope.updated`）、产物映射与扩展项比较（B4c）。

### 4. owner 固定与会话身份

- 模型配置归属 = 项目所有者在该引擎下的有效配置（有个人 Key 用个人的，否则管理员配置），谁发消息都一样，
  Studio 复核等后端内部发的消息也一样；归属计入会话指纹，所有者改了 Key、项目移交，下一条消息前重连。
- 引擎实例 ID 由宿主生成，写进 `.onto/run/engine.json`（加 `owner`），Claude 的 CLI 环境带
  `ONTO_RUN_PROJECT / CONVERSATION / ENGINE`，经 shell 前缀进 agent 的每条 bash；Jiuwen 的命令与守卫按
  工作区读引擎标记补上同样的变量。git 提交钩子据此认出 agent 的提交（ADR-0037）。

## Alternatives considered

- **按契约形状重写 JSONL 历史**：内部读历史的地方（用量统计、Jiuwen 回放、标题、幽灵态自愈）都要改，
  存量历史还得迁移；选了「记录不变、投影成契约形状」，只在宿主产出的新记录上加字段。
- **一条记录投影成多条事件**：id 就不再是行号，回放与去重要另记映射；改为写入时拆开。
- **Claude 用消息前缀注入**：斜杠命令会失效（见上）。
- **项目流复用对话流**（方案 D14 原文）：Studio、插件页不开对话也要收事件，契约已定三条流（8.1#11）。

## Consequences

- 前端对话状态机的 `lastSeq` 从 0 起算，id 从 1 开始后第一条事件不再被当成重复丢掉。
- 历史里多了工具调用、工作过程组的记录（原来工具调用藏在助手消息的 blocks 里），体积相近。
- 项目流、用户流重启即丢，前端按 `stream.reset` 重拉快照；`flow.updated` 的流程哈希变了（装配换了节点）
  前端要整份重拉 FL-01。
- 下一步的计算、写入范围锁、变更集确认（`open_changeset`、round_changes 卡的待确认态）在 B4b 接上。

## 合并后的衔接（B4c，2026-11-02）

见 ADR-0045。第 2 节的业务知识信号不再写死在 `injection.py`：改按登记的数据源取生效中的装配里各包声明的提示规则
（`register_prompt_hint_provider`），只有启用了知识沉淀包的项目会提示；Jiuwen worker 进程自己登记一份。第 3 节留给
B4c 的两个登记点（产物映射、扩展项比较）与 S-20 的 `action` 解析都在 `app/wiring.py` 接上；插件运行时只动纯数据包时
当场切换装配，已生效的 `assembly.updated` 也从 `assembly_switched` 发。
