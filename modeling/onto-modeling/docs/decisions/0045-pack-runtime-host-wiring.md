# 0045 合并 B4c：插件运行时接到会话宿主与项目流，登记集中在一个接线模块

## Context

B4a（会话宿主、三条事件流，ADR-0034～0038）与 B4c（扩展项、插件产物、拆可选包，ADR-0040～0044）并行开发，
两边各自只留了登记点：

- B4a 的 S-20 认 `action` 要 `routers/sessions.register_action_resolver`，`workspace.changed.artifacts` 要
  `services/project_events.register_artifact_mapper`，`extensions.updated.changed` 要 `register_extension_differ`；
  登记前分别是一律 409、空列表、全部七类。宿主注入的业务知识信号是 `injection.py` 里写死的两张词表，对所有项目都提示。
- B4c 给了 `packs/registry.resolve_action`、`extension_differ`、`prompt_hints` 与 `kernel/artifacts/service.after_commit`，
  事件经 `kernel/project_events.emit` 发出，没有接收方；切换能力包后 `packs/service.emit_switch_events` 自己再发一份
  `assembly.updated`、`extensions.updated`。
- 合并时唯一的文本冲突是 `kernel_guards/knowledge_signal_probe.sh`：主线删了（ADR-0034），B4c 给它加了「没启用知识
  沉淀包不出声」。

要定的是：登记放在哪、两个同名的 `project_events` 怎么分、重复的事件留哪一份、知识信号怎么只在启用了知识沉淀包的
项目里出现。

## Decision

1. **冲突**：接受删除，同时删掉只测这个 hook 的 B4c 用例。「没启用知识沉淀包不提示」由第 6 条的提示规则注入实现，
   两个引擎都管用（hook 原本只有 Claude 执行）。
2. **登记集中在 `app/wiring.py`**：`install()` 一次登记全部五项；`app/main.py` 在建应用时（`v1.install(app)` 之后）调，
   不放 lifespan——登记的都是进程内的函数引用，不做 I/O、可以重复调，不开 lifespan 的 TestClient 与 uvicorn 起的
   服务看到同一套接线。工作区管理器一律调用时再取 `deps.workspaces`。Jiuwen worker 是另一个进程（ADR-0038），
   宿主注入在 worker 里算，`jiuwen_worker.main` 调 `install_worker(数据目录, 资产目录)` 登记自己那份提示规则。
3. **五个衔接点**：

   | 衔接点 | 登记到 | 接线函数 |
   |---|---|---|
   | S-20 `action` → 命令 | `routers/sessions.register_action_resolver` | `wiring._page_action_command`：`registry.resolve_action(...)` 的 `command`，认不出给 None |
   | 提交改到的产物 | `services/project_events.register_artifact_mapper` | `wiring._artifact_refs`：`after_commit(deps.workspaces, root.name, paths)`（项目 ID 就是工作区目录名，`project_root` = `workspaces/<项目 ID>`） |
   | 扩展项比较 | `register_extension_differ` | `registry.extension_differ()`（同一个函数对象，重复调不会登记两份） |
   | 内核事件 → 项目流 | `kernel/event_port.register_sink` | `wiring._publish_kernel_event`：`streams.publish_project`，信封同契约第 4 章 |
   | 包提示规则 → 宿主注入 | `session_host/injection.register_prompt_hint_provider` | `wiring._prompt_hints`：`registry.prompt_hints(deps.workspaces, root, 原文)` |

4. **两个 `project_events` 不合并，内核那个改名 `kernel/event_port.py`**。分层是内核不依赖会话宿主一侧的流
   （`services/project_events`、`services/streams`）；合成一个，要么内核反过来依赖 services 的流，要么把流搬进内核。
   改名后的职责：`event_port` 是内核发项目事件的出口，和写入事务的 `kernel/writepath/events` 同一个做法——内核只管发，
   上层登记接收方；现在只有内核的产物索引（`artifacts.updated`）经它发。内核之外的发送方直接调
   `services/project_events`：知识接口改调新加的 `knowledge_changed`（`knowledge.updated`）。
5. **装配与扩展项事件只留项目流这一处**：锁文件提交时由提交事件发「待生效」，切换装配时发「已生效」，每次一条，
   各配一条 `extensions.updated`。`emit_switch_events` 删掉，换成 `announce_immediate_switch`：只动纯数据包（H0）时
   `_switch` 当场就把装配目录切了，之后不会再有「消息前切换」，已生效这一次由它经 `project_events.assembly_switched`
   补发（和 `fingerprint.prepare` 同一个出口，前后哈希相同时不发）。`registry.remember_baseline` 保留：改锁文件之前
   记下比较基准，提交事件里的比较函数才能只报变了的类。
6. **知识信号走包的提示规则**：`injection.py` 去掉写死的词表与 `knowledge_signal_hint`，改为按登记的数据源取
   生效中的装配里各包声明的 `prompts`（`InjectionResult.prompt_hint`）；没登记、没命中、算出错都不带，不挡消息。
   命中的消息与退役的 hook 一致（知识沉淀包清单里是同一组词），提示文本换成包里的那段（指向沉淀守则
   `KNOWLEDGE_SEDIMENTATION.md`），不再逐个列出命中的词。启用知识沉淀包后，等装配在下一条消息前切过去才开始提示。
7. **网页写产物不重复发 `artifacts.updated`**：产物映射挂在全部主线提交上（网页、agent、插件都走），AR-05/06/07 的提交
   事件也会触发 `after_commit`。这三处自己发 created / updated / deleted，提交时用一个上下文变量标出「这些路径由调用方
   报告」，`after_commit` 照样把它们放进 `workspace.changed.artifacts`，但不再补一条 updated；过期状态翻转照常发。

## Alternatives considered

- **在 lifespan 里登记**：大部分接口测试不开 lifespan，接线就时有时无；登记本身没有副作用，不需要等启动恢复。
- **各模块导入时自己登记**（像 `routers/events.py` 导入时装写入事件订阅）：五项散在五个文件里，查「谁接了谁」要全仓搜。
- **H0 切换也只发一条**（把提交时的「待生效」压掉）：要在提交事件里认出「这次提交随后会当场切换」，得给写入事务
  加上下文；两条紧挨着的事件（待生效 → 已生效）对前端无害，和跟随通道升级（`prepare_for_message`）的现状一致。
- **worker 经协议回调内核算提示规则**：多一次跨进程往返，还要把同步的注入改成等回调；提示规则只读数据目录里的
  装配与包仓库，worker 自己读结果一样。

## Consequences

- 每次主线提交都同步跑一遍产物索引刷新（按启用的产物类型，文件签名没变不重新解析），提交接口多一点耗时。
- agent 删掉产物文件时只有 `workspace.changed.paths`，没有 `artifacts.updated` 的 deleted：`after_commit` 只看提交后
  还在的行（见 `_plan/gaps.md`「B4 合并」）。
- 知识信号的提示文本与 ADR-0022 的逐字对照不再适用，测试改为对照「哪些消息命中」；两张词表只剩包清单里一份。
- 用到 `extensions.updated.changed` 的地方现在拿到的是真实的比较结果；进程里还没有基准的项目第一次仍给全部七类。
