# 0040 扩展项注册表与插件页面：页面只在平台内置的三个里选，左栏跟锁文件走

## Context

方案 D17、详设 5.6、5.9.1～5.9.3、5.9.6～5.9.8，契约 PL-12、S-20、EP-07。P2 要让包往界面上加东西：左栏页面、
资料页的子页、对话里的快捷入口、产物类型与视图、声明式门禁、配置项、提示规则。前端第五版只做了三个插件页面
（场景走查、知识沉淀、代码生成与求解）和资料页的三个子页，页面代码在平台里，包不能带前端代码（PL-15 的
`frontend` 检查）。验收要求：停用带页面的包后，左栏页面消失、产物保留。

还要定两件事：包启用、停用后界面什么时候变（能力包要等新装配目录在下一条消息生效，界面却不能等）；
页面上的按钮怎么变成对话里的命令（详设 5.9.3「页面只发命令、不跑助手」）。

## Decision

1. **注册表按项目现算**（`services/packs/registry.py`）：读锁文件里启用的包在包仓库里不可修改的版本目录，
   汇总清单 `contributes` 的 `pages`、`subpages`、`entries`、`artifacts`、`views`、`gates`、`config`、`prompts`。
   只读 YAML，不跑包的代码；不缓存（锁文件一变结果就变）。
2. **左栏 = 内核 4 项 + 已启用包声明的页面 ∩ 平台内置页面**。页面 id 只认 `scenarios`、`knowledge`、`scripts`，
   子页只认 `sources`、`snapshots`、`schema`，页面接口版本（`page_api`）不满足的不出；同一页面只取第一个声明它的包；
   插件页按声明的 `order` 排，超过 5 个的进「更多」。
3. **生效口径分两层**：左栏、子页、产物类型、视图跟锁文件走——启用后马上出现，能看已有产物；停用后马上消失，
   工作区里的产物文件原样保留，停用的包的页面列进 `disabled_pages`（旧链接显示「插件已停用」）。页面上的按钮要等
   新的装配目录生效（下一条消息），在那之前 `pages[].pending=true`。
4. **按钮只发命令**：清单里的 `actions[]` 是 `{id, label, command, hint}`；S-20 的 `action: {pack, page, action_id}`
   由 `registry.resolve_action` 换成命令，只认锁文件里启用的包；命令在不在生效中的装配里仍由 S-20 判断。
5. **提示规则**（`prompts`）：包声明「正则 → 一段提示」，会话宿主在两轮之间按用户消息匹配后注入
   （`registry.prompt_hints`），两个引擎都能拿到；知识沉淀包的纠错信号就是一条（取代只有 Claude 执行的
   UserPromptSubmit hook，见 0042）。
6. **事件只定发送口**（`kernel/project_events.py`）：`extensions.updated`、`assembly.updated`、`artifacts.updated`、
   `knowledge.updated` 都经 `project_events.emit` 发出，没有接收方时只留进程内的最近记录；项目流（会话宿主一侧）
   落地后用 `register_sink` 接上，发送方不改。`extensions.updated.changed` 由 `registry.changed_parts` 比较前后两份
   注册表得出。

## Alternatives considered

- **包自带页面代码（iframe 或远程组件）**：详设 5.9.1 明确页面在平台里、包只给数据和命令；带前端代码就要
  前端沙箱和审查，P2 不做。
- **左栏等装配生效再变**：停用后页面还在、点进去按钮却不能用，用户分不清；设计稿要求启用后马上能看已有产物。
- **注册表缓存进装配目录**：装配目录是 agent 侧的东西，界面口径跟锁文件，不跟装配；现算的开销是读几份
  YAML，可以接受。

## Consequences

- 新的插件页面要平台发版（加进 `BUILTIN_PAGES` 与前端），包只能选用已有页面；这是有意的约束。
- S-20 的 `action` 解析、`prompt_hints` 注入、事件接收方都要会话宿主（B4a）在合并时接上：解析函数登记为
  `registry.resolve_action`，接收方 `project_events.register_sink`。合并前事件只在进程内。
- 角标数据（`register_badge_provider`）等待处理汇总（B4b）登记，之前恒为 0。

## 合并后的衔接（2026-11-02）

见 ADR-0045。第 6 条的发送口改名 `kernel/event_port.py`，只给内核的产物索引用，接收方在 `app/wiring.py` 登记到项目流；
`extensions.updated`、`assembly.updated` 改由项目流在锁文件提交与装配切换时发（`registry.extension_differ` 登记成比较函数），
`packs/service.emit_switch_events` 删掉，只在纯数据包当场切换时补发已生效；`knowledge.updated` 由知识接口直接调
`services/project_events.knowledge_changed`。S-20 的 `action` 解析与提示规则注入也在 `app/wiring.py` 接上。
