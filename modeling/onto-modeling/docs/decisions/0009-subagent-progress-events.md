# 子代理实时执行过程事件

## Context

聊天区原来只根据主 Agent 的 `Task`/`Agent` 工具调用和最终 `tool_result` 渲染子代理卡片，用户在子任务完成前只看到静态的"运行中"。第一版改造改成了「阶段 + 累计工具调用次数 + 每 2 秒心跳耗时」，实测三个问题：

- 卡片长期停在"正在启动、0 次工具调用"。根因不在展示层：openjiuwen 的
  `add_general_purpose_agent=True` 注入的 general-purpose 子代理 spec 会把**父 agent 的
  workspace 原样带上**，父子共用同一个 workspace 根时，子代理在首个模型调用之前就永久挂住
  （父 agent 停在 `task_tool` 的子流迭代上，子代理一个 Rail 回调都发不出），子任务从来没真正跑起来。
  卡死的落点是子代理 `init_workspace` 写脚手架文件：openjiuwen 按"目标路径哈希"建 SQLite
  锁库，而租约释放后进程内的锁不会立刻放掉，于是写同名路径的一方耗尽 `write_file` 的
  300 秒 `lock_timeout` 后失败（日志特征：`method_exec_time_ms: 300001`）。**凡是两个 agent
  在同进程写同一路径都会触发**，所以不只父子要错开，两次派发之间也要错开。
- 心跳让秒数一跳一跳，且每条心跳都写进事件 JSONL 并参与历史回放。
- 计数（工具次数 / tokens）不是用户要的进度；用户要的是"能展开看到这个子代理在干什么"。

## Decision

**修根因**：Jiuwen 引擎显式提供同名 general-purpose 子代理 spec（抢在 SDK 注入之前），
workspace 落在 `DATA_DIR/jiuwen/<pid>/sub_agents` 下；再在 `create_subagent`（所有派发路径的
唯一汇聚点，SDK 自己的 observability 也挂这里）之前把 spec 的 workspace 换成按 `subsession_id`
命名的子目录，做到**每次派发一个独立工件区**——否则第二次派发会撞上第一次留下的锁。spec 的
`sys_operation` 只在 `workspace` 非空时才被采纳，两者必须一起给，否则子代理会自建 LOCAL sysop、
丢掉沙箱三根。

**进度语义从"计数"改成"有界步骤流"**，两个引擎归一到同一组平台事件：

- `subagent_started`：`task_id` / `tool_use_id` / `description` / `agent_type`；
- `subagent_activity`：一步一条，`step_id` + `kind: tool | text`，同 `step_id` 后到的事件只更新
  `status`；工具参数只保留摘要键（file_path/path/pattern/command/description/url/query，各自截断），
  单个子任务步骤数封顶 60，超出只补一条省略提示；
- `subagent_finished`：终态 + `usage.duration_ms`（耗时只在结束时上报一次，不再有心跳）。

来源分别是：Jiuwen 用 Rail 回调（父 `task_tool` 的 before/after 开卡收卡，子代理的
`before_tool_call` / `after_tool_call` / `after_model_call` 产出步骤与文字），Claude 用本来就
实时流过来的子代理消息——`assistant_message` 补 `parent_tool_use_id` 即可归卡，`task_progress`
心跳不再入历史。

**前端**：子任务卡默认折叠，右侧折叠开关展开后是该子代理自己的步骤流（工具行复用主对话的
`ToolLine`，文字步骤单独一行，终态附结果摘录）；卡片只在终态显示一次静态耗时，不展示工具次数与
tokens。归并规则收口在 `frontend/src/lib/subagentEvents.js`（纯函数，`node --test` 可直接跑），
按派遣调用 id 分流，派发多个子代理时各卡独立；卡片被历史截断时子代理消息退回主工具组呈现。

## Alternatives considered

- 沿用 SDK 注入的 general-purpose spec：会共用父 workspace 根，子代理必然挂死，所以不可行。
- 只换一个固定的 `sub_agents` 根（不按派发轮换）：第一次派发正常，第二次起会撞上前一个子代理
  留下的锁，卡片停在"已启动，等待子代理反馈"直到 300 秒超时失败。
- 让 SDK 自己派生子目录（spec 不给 workspace）：`create_subagent` 只在 `spec.workspace` 非空时
  采纳 `spec.sys_operation`，子代理会自建 LOCAL sysop 丢掉沙箱三根，所以仍自带 spec 并自行轮换。
- 继续用「阶段 + 计数 + 心跳」：不反映真实动作，秒级心跳还会刷爆事件历史。
- 转发子代理全部文本和思考增量：信息量大、容易把内部上下文混进主对话。
- 前端定时轮询会话或新增进度接口：会重复读取已有事件，增加连接和一致性问题。
- 只桥接 OpenJiuwen 的子代理 chunk：同步 `task_tool` 会在隐藏子会话内消费 chunk，主会话收不到。

## Consequences

子任务卡能实时反映子代理的每一步动作，展开即可追过程，历史回放也能复原。事件量与单个子任务的
步骤数成正比且有上限，不再随时间线性增长。两个内核的卡片呈现一致，但文字步骤只有 Claude 内核与
Jiuwen 的子代理模型回答能提供；不支持 Rail 回调或旧 SDK 的运行时仍回退到"运行中 + 结果摘录"的静态卡。
Jiuwen 子代理的工件区从此按派发落在 `sub_agents/<subsession_id>/` 下，和主 agent 以及彼此都不混用，
代价是每次派发多出一份脚手架（与 SDK 默认的 per-subsession 行为一致）。

上游的锁生命周期缺陷没有修（租约释放后进程内 SQLite 锁不放，`write_file` 的 300 秒
`lock_timeout` 也只能按调用传参，`LocalWorkConfig` 没有对应字段）。这里只消除了必然重复的路径；
子代理若去写主 agent 刚写过的**项目文件**，同类停顿仍可能出现，识别特征是日志里
`write_file` 失败且 `method_exec_time_ms` 接近 300000。

## 补充：Claude 引擎子代理一律同步执行

Claude Code CLI 2.x 默认把子代理放到后台运行，而平台以主轮 `result` 判定会话空闲、以派遣调用的
`tool_result` 判定子任务完成。后台派遣时，子任务卡提前显示完成、会话提前置为空闲，后台子代理还可能
随空闲回收被静默终止（Issue #377）。工作区规则 8.04 要求模型显式传 `run_in_background: false`，
但只能靠模型自觉，实测仍会漏传。

因此 Claude 引擎在会话环境中注入 `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`：CLI 从工具定义中移除
`run_in_background`，并关闭子代理、超时 Shell 命令与长时间 MCP 调用的自动转后台。规则 8.04 保留，
作为降级 CLI 与未来 CLI 版本的第二道防线。

代价：命令超过本次调用的超时（默认 2 分钟，模型可以延长，最长 10 分钟）直接失败，不再转入后台；
子代理运行期间主轮持续占用会话，其间的新消息排队到本轮结束。

前端子任务卡不再把任何派遣回执都当作完成（`frontend/src/lib/subagentEvents.js` 的 `resolveSubagentState`）：
有生命周期终态，或已有回执且不是后台启动回执（同步派遣返回的结果），即判定结束，失败、停止按终态与回执显示；
同步结果不等终态事件，避免生命周期事件缺失或匹配不上时卡片一直转圈。回执是后台启动回执时显示"后台运行中"；
既无终态也无同步结果、而会话已被打断、关闭（含空闲回收）或出错时显示"已中断"。后台启动回执是 CLI 内部信息，
不在卡片详情中展示。

会话层同样不再把"主轮结束"当作"任务结束"：Claude 引擎注入 `CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS=1`
（并显式设 `CLAUDE_CODE_BG_TASKS_REPORT_RUNNING=1`，2.1.269 之前的 CLI 默认每轮结束都报 idle），CLI 以
`session_state_changed` 上报自身状态，引擎归一为不入历史的内部事件 `_cli_state`。主轮 `result` 到达时，
CLI 从未上报（旧版 CLI、九问引擎）或已报 idle 才置空闲；CLI 仍报 running（后台子代理未结束、接续轮次
未处理）则保持运行中，等它报 idle。用户点"停止"后的 `result` 直接置空闲；CLI 随后自行开始接续轮次时
恢复运行中。这样开关失效、子代理走后台时，顶部状态、构建通知仍与真实进度一致，运行中的会话也不会被
空闲回收、槽位挤占或换绑会话断开。没有采用平台自行跟踪在途子任务或计时兜底：是否还有后续轮次由 CLI
判断最准。

## 补充：一轮被截断、出错或长时间无响应时的收尾（Issue #387）

子任务卡一直显示"运行中"的另一类成因，是这一轮已经结束或被取消，派遣调用的 `tool_result` 却再也不会到达：

- Jiuwen 引擎开启了 SDK 的 task loop，框架默认一轮最多等 600 秒（`completion_timeout`），比 `task_tool`
  自己的 720 秒超时还短。到点取消时 SDK 跳过 `after_tool_call`，工具结果发不出来。现在显式设
  `completion_timeout=None`，一轮的长短由最大迭代数、用户停止和下面的无响应判定兜底。
- SDK 把失败收成一条 `answer`，流照常结束、不抛异常。原先这条消息被忽略，这一轮记成成功、没有任何提示。
  现在按字段区分：`error` 有值（模型调用失败、轮次超时）走出错收尾，用户看到错误提示，在飞的子任务卡收成
  失败；`error` 为 `aborted` 是用户停止，由取消路径收尾；`result_type` 为 `error` 而没有 `error` 字段是步数、
  外层轮数用完或连续没有进展，属于正常收口，只提示用户可以发"继续"，不报模型故障、不拆引擎。
- Claude 引擎的消息流正常结束（CLI 以退出码 0 退出）时发 `_engine_end`（`reason: exited`）：这一轮还在跑
  时按出错处理，空闲时静默关闭。原先会话停在原状态，下一条消息才报错。

会话层增加无响应判定：运行中、没有待确认卡、连续 `ONTO_WEB_SESSION_STALL_TIMEOUT`（默认 1800 秒，0 关闭）
没有任何引擎事件时，硬终止引擎、释放槽位，广播带 `reason: stall_timeout` 的 `session_error`，下一条消息
重新连接。计时从接上引擎开始，排队与连接的时间不计入；不转发的运行时消息（状态、任务进度、后台任务心跳）
由引擎产出内部事件 `_activity`，同样算作有活动。子代理一次模型调用的流式输出两个引擎都不转给会话层
（实测 Claude CLI 期间不发任何消息），所以单次调用沉默超过阈值也会被判超时；单次调用自身有超时
（Jiuwen 300 秒，Claude CLI 默认 10 分钟），默认阈值下不会发生。Jiuwen 硬终止前先 `abort()`，否则 task loop 在 SDK 自己的
后台任务里还会接着跑。一轮出错或超时收尾时，这一轮进行中追加的消息随引擎拆除丢失，错误提示里注明条数，
请用户重发。

这与上文"没有采用计时兜底"并不冲突：那里判断的是后台子代理是否结束，仍以 CLI 上报为准；这里只处理整整
30 分钟没有任何事件的情形。阈值取 30 分钟，是因为模型调用失败重试期间同样没有事件，Jiuwen 单次调用最长
300 秒，实测连续重试可超过 25 分钟。代价是真正沉默超过 30 分钟的工作会被终止；两个引擎的工具自身都有更短
的上限（Jiuwen 300 秒，Claude Shell 最长 10 分钟），实测不受影响。

前端相应补两条判定（`resolveSubagentState`、`endedByStall`）：同步派遣的子代理活不过它所在的一轮，一轮已经
收尾（会话回到空闲）却仍没有结果，就不会再有了，显示"已中断"，升级前遗留的历史也能据此恢复；后台派遣例外。
派遣之后最先发生的收尾是无响应判定时，显示"执行超时"。
