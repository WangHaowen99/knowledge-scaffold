# onto-modeling 插件化全量开发 · 总计划与进度

用户要求（2026-11-01）：
- 在一个分支上完成全量开发，完全按前端设计稿第五版和详细设计 v1.1；
- 前端与设计稿完全一致，Studio 的 WebGL 渲染部分除外；
- 后端与详细设计完全一致。

- 交付分支：`feat/onto-modeling-plugin-architecture`（从 2ad2ad7b 拉，已含 P0 第一批止血和第二批单人所有权、按提交发布）。
- 前端线：本地分支 `feat/onto-modeling-plugin-frontend`，只改 frontend/，阶段性合进交付分支。
- 共用规则：`RULES.md`。

## 阶段

| 线 | 阶段 | 内容（方案编号） | 状态 |
|---|---|---|---|
| 契约 | C0 | API v1 契约：详设第 4.1、7 章 + 设计稿 34 页用到的全部接口 | 完成（321 接口、59 事件；待 B1 后提交进仓库） |
| 后端 | B1 | P0 剩余：A1–A7 评测移出（先抽凭据存储和后台任务公共件）、A2 验证回执入口、A7 三份契约 | 完成 ac098d7d..a7a7ea62 |
| 后端 | B2 | P0 剩余：A14 `/api/v1` 与兼容中间件、A15 拆 projects.py、A8 发布适配器 SPI 与 workspace.py 拆分、B4 Jiuwen 阶段提示与知识信号 | 完成 2e6bfc6e..2f5c4ecf |
| 后端 | B3 | P1：D1–D6 包仓库/锁文件/装配器/启动核对/契约库，D7 前半与 D8，D9–D10 守卫，D12/D14 flow.yaml 与 /flow，C1–C6 元模型与统一变更事务，B2/B15 指纹与引擎协议，A10/A16，E3–E8、E10 | B3a 插件与流程运行时完成（主线到 07dc734b）；B3b 元模型/写入事务/治理/密钥完成（分支 feat/onto-modeling-plugin-p1-governance，ac1468d4..6d065ccc）；已合回主线（merge 8cd1d9af，衔接到 075ed5b8） |
| 后端 | B4 | P2：B3–B7、B11，C7–C15，D11、D13、D15–D17，D7 后半拆可选包，E9 配额，第一个行业包 | B4a 完成（主线 075ed5b8..10cc138e）；B4c 完成（分支 feat/onto-modeling-plugin-p2-packs 075ed5b8..cee5dcdb），已合回主线（merge 4b9aa91c，衔接与审查修复到 266b6cc8）；B4b 待开始（用户要求这一波后先停） |
| 后端 | B5 | P3：D18 节点包，B12–B14 自主构建，E11–E16 证据源与网络隔离，扩展项 | 待开始 |
| 后端 | B6 | P4：版本归因与灰度、评测反馈接入、更多提供方、系统知识包、按项目隔离 agent | 待开始 |
| 前端 | F1 | 设计系统、外壳、路由、API 客户端（含 mock）、登录、项目列表、新建与导入向导、工作台对话 | 完成（8 提交，前端分支 a2aca4ee..a2153a82） |
| 前端 | F2 | 工作台各页：Studio（表格、图谱除 WebGL）、报告、流程图、对话卡片与状态、待处理、自主构建运行、资料、蓝图 | 完成 4c0369b1..8c936dca |
| 前端 | F3 | 插件与项目页：知识、场景走查、代码生成、增量、版本、发布、资料与证据源、快照、schema、项目设置 | 完成 1d7c5bea..77f819e1 |
| 前端 | F4 | 运维与管理：运维中心、可观测、管控、用户、插件目录/详情/上传、个人设置、系统设置 | 完成 29890e75..9f8ba691 |
| 集成 | I1 | 前端接真实后端、端到端联调、逐页对照设计稿截图、合并前端线 | 待开始 |

## 进度记录

（每个阶段完成后追加：日期、提交范围、测试结果、遗留）

- 2026-11-01：C0 完成，`api-v1.md` 5534 行；契约里 5 组接口设计稿没画入口（服务令牌、模型单价、指定所有者、管理员审核、链路检索），按「前端与设计完全一致」不加界面，待用户决定。
- 2026-11-01：B1 完成（5 提交 ac098d7d..a7a7ea62，已推远端）；Linux 全量 1490 过 / 1 既有失败（删 205 个评测测试、新增 91 个）。遗留见 gaps.md；仓库根部署脚本仍写 ONTO_WEB_MODELING_URL（无害，未动）。B2 开工。
- 2026-11-01 · F1 前端：`feat/onto-modeling-plugin-frontend` a2aca4ee..a2153a82（8 个提交）。设计系统、外壳、数据层（含 mock）、login / projects / import / create / wb-l2 / chat-states 完成；`npm test` 85 通过、`npm run build` 通过；与设计稿逐页像素对照只差在原型演示控件（见 `gaps.md` F1）。遗留：未与真实后端联调、AD-06/07 无入口、README 待补 `VITE_API_MOCK`。详见 `frontend-progress.md`。
- 2026-11-01：F1 完成，npm test 85 过、build 过；wb-l2/chat-states 与设计稿像素差 0%～0.13%。发现设计稿部分示例场景可能可识别客户，前端改合成场景（F2 第一件事），契约 3 处已改。I1 时要做：README 补 VITE_API_MOCK 说明、前端架构 ADR（哈希路由、fetch 读 SSE、mock 不进生产包）。
- 2026-11-01：B2 完成（7 提交 2e6bfc6e..2f5c4ecf，已推远端）；Linux 全量 1748 过 / 1 既有失败。147 个 v1 路由；旧前端在新前端替换前部分请求会失败（契约不做形状兼容）。Claude 引擎注入仍靠 hook，排进 B4。
- 2026-11-01：B3a 完成（插件运行时与流程运行时，9 提交 2711cc0a..07dc734b，未推远端）：包仓库与 packs.lock、装配器与启动核对、内核守卫与绕过补洞、core 包、共享契约库按锁定版本加载、flow.yaml 单一定义与 `/flow`、会话指纹与引擎协议、插件接口（PL-01～03、05～11、13～18）、O-30～O-34 改由包仓库承接，ADR 0024～0026。Linux 全量见本次报告；遗留见 gaps.md B3a（PL-04/12/19、项目事件流、备份范围加 packs/ 等），前端改读 `/flow` 记在 frontend-progress.md。B3b 合回时注意 ops.py 的 O-30～O-34 段、ADR 从 0027 起。
- 2026-11-01：B3a 完成（9 提交 2711cc0a..07dc734b，已推远端）；Linux 全量 1942 过 / 0 失败（benchmark 在无 .git 快照里跳过）。待合并时处理：backup 加 data/packs（已告知 B3b）、ChangeTx 改用 contracts.for_project、B3b 新脚本登记进 core manifest.yaml、ops.py O-30～34 段合并；存量项目首次启动多一次锁文件提交（主线版本 +1）。B4 等 B3b 合回后开工。
- 2026-11-01 · F2 前端：`feat/onto-modeling-plugin-frontend` 4c0369b1..8c936dca（9 个提交）。示例场景换成合成场景（安全 grep 零命中）；studio、studio-table、reports、flowchart、inbox、l4-run、wb-materials、wb-blueprint 完成；`npm test` 158 通过、`npm run build` 通过；与设计稿对照只差数据文字和原型控件（详见 `frontend-progress.md`）。遗留：契约 4 处旧示例、求解契约表单的行业专用字段待主控定；开始自主构建无单独界面；真实 drawio 未验证；未与真实后端联调。新增契约补充 21 条（`contract-changes.md` F2）。
- 2026-11-01：F2 完成（9 提交，npm test 158 过、build 过），示例场景已换合成场景；契约剩余旧示例主控已改。遗留：solver-contract-model.js 的行业专用字段（仓库原有、与 consumer 共用，未改，待用户定）；主入口包 647KB 警告。F3 开工。
- 2026-11-01：B3b 完成（分支 `feat/onto-modeling-plugin-p1-governance`，起点 2f5c4ecf）：C3～C5 元模型单一定义（ADR-0030）、C1/C2/C6 统一变更事务 ChangeTx（ADR-0031）、E3～E8 治理平面（配置注册表、可观测、响应后审计、存活与就绪、三种角色含 auth-server `ops`、运维监听与维护模式，ADR-0032）、E10 密钥移出与 agent 独立用户与最小环境（ADR-0033）。合并要点：`main.py`、`config.py`、`api/v1.py`、`run.py`、`Dockerfile` 两边都改过；`onto_metamodel.py` 要登记进 `manifest.yaml` 并由契约库加载；ChangeTx 的脚本描述符同步只在 `kernel/writepath/tx.py::_sync_script_descriptors` 一处，换成 `contracts.for_project(root)`。9 个提交 ac1468d4..6d065ccc；Linux 全量 1832 过 / 1 既有失败（起点 1748 / 1），auth-server 44 过。装配器建目录后要调 `agent_identity.share_read_only`（gaps B3b 第 13 条）。遗留见 gaps.md B3b。

- 2026-11-01：B3b 完成（分支 feat/onto-modeling-plugin-p1-governance，9 提交）；Linux 全量 1832 过 / 1 既有失败；残余风险：CLI 进程持模型 Key（等 P3 网关代理）、所有项目 agent 同一系统用户（P4）。合并子任务进行中：merge --no-ff + 衔接（装配目录给 agent 只读、ChangeTx 走契约库、onto_metamodel 登记 manifest、锁文件走 ChangeTx、release compose 默认 ONTO_WEB_OPS_ON_MAIN=true）。
- P2 计划拆分：B4a 执行记录/续跑卡/两引擎宿主注入/Jiuwen worker/owner 固定/崩溃恢复/agent 提交收口/项目事件流 P-15；B4b 变更集/确认生效与撤回/实体差异/写入范围只读守卫/节点与切片状态/suggest/待处理；B4c 扩展项注册表与页面/插件产物登记与通用视图/拆可选包（含知识沉淀、场景走查、代码生成、图示、反馈演进）/配额/第一个行业包。B4a ∥ B4c，之后 B4b。
- 2026-11-01：B3b 合回主线（merge 8cd1d9af，`--no-ff`，冲突 3 个文件：`api/v1.py`、`agent_sessions.py`、`workspace/core.py`）；衔接 7 个提交 5131388c..075ed5b8：新装配目录给 agent 只读（Linux root 测试）、写入事务描述符走契约库、`onto_metamodel` 登记 core 清单并由契约库按项目 / stable 版本加载、锁文件走 ChangeTx（修掉合并后 `plugin:` 提交被记成 web、trailer 重复）、备份含包仓库的断言、两份 compose 默认 `ONTO_WEB_OPS_ON_MAIN=true`、ADR 0024/0030/0032 更新。Linux 全量 2034 过 / 1 既有失败（agent 隔离 11 个用例以 root 跑过）。未接的登记点见 gaps.md「B3 合并」。B4 可开工。
- 2026-11-01：B3b 合并完成（merge 8cd1d9af + 7 衔接提交，主线 075ed5b8 已推）；Linux 全量 2034 过 / 1 既有失败。4 个插件登记点（就绪 assembly 项、O-29、O-42、Onto-Packs trailer）排进 B4c；元模型其余调用点改按项目锁定版本排进 B4b。B4a、B4c 开工。
- 2026-11-01 · F3 前端：`feat/onto-modeling-plugin-frontend` 1d7c5bea..77f819e1（16 个提交，未推远端）。前端阶段定义改读 `/flow`（P1、P2 两种形状归一，节点命令按 `commands` 归属）；knowledge、scenarios、scripts、incremental、versions、publish、resources、sources、snapshots、schema、proj-settings 完成，F2 留下的跳转（查看改动、刷新后对比、查看原文、上传文本版、调整自主构建策略）都已接住，「开始自主构建」入口在项目设置；插件页只发命令（S-19/S-20 进度条）、写入范围只读两态。`npm test` 299 通过、`npm run build` 通过，每个提交单独导出后也通过；安全 grep 零真实命中。与设计稿对照只差数据文字和原型控件（详见 `frontend-progress.md`）。遗留：S-20 的命令名和参数是前端定的，后端实现时对齐（`contract-changes.md` F3 末尾的表）；`ExcelView`、`HtmlPreview`、`MergeDiff` 没人用，删不删待定；主入口包 695 KB；未与真实后端联调。新增契约补充 31 条（`contract-changes.md` F3）。
- 2026-11-01：**用户要求：这一波（F4、B4a、B4c）开发完先停。** 收尾只做：审查、B4c 合回主线、Linux 全量、推交付分支、汇报现状；不启动 B4b 及之后阶段，前端分支暂不合入主线，等用户说继续。
- 2026-11-02：上一轮会话 11-01 晚因代理断线中断，B4a、B4c、F4 都停在半路；重开后 B4a 由主控收尾，B4c、F4 各起一个子任务接着做。
- 2026-11-02：B4a 完成（主线 075ed5b8..10cc138e，12 个提交，已推远端并核对）。项目流与用户流、会话宿主（对话流载荷按契约、执行记录与续跑卡、页面命令任务、两引擎宿主注入、owner 固定）、启动恢复注册表、agent 提交钩子、Jiuwen worker，ADR 0034～0038。收尾修了三处 Linux 下才暴露的竞态，第三处补了确定性用例；重载与停机用例改成按条件等。Linux 全量 2106 过 / 1 既有失败（起点 2034 / 1）；会话相关测试在容器里连跑 15 遍 0 失败。遗留见 gaps.md B4a（留给 B4b 的下一步输入、写入范围数据源、变更集钩子；合并 B4c 时要接的产物、扩展项比较函数、S-20 action 解析）。
- 2026-11-02：F4 完成（前端分支 29890e75..9f8ba691，12 个提交，未推远端）：ops、ops-obs、ops-ctl、ops-users、plugins、plugin-detail、plugin-upload、me、sys 九页，删除 ExcelView、HtmlPreview、MergeDiff，主入口按路由拆包（705 KB → 321 KB）。`npm test` 473 过、build 过，每个提交单独导出也过；和设计稿对照差 0.05%～7%（数据文字与条数）。子任务的一条测试断言把设计稿包名关键词写进了历史，主控把修正并进原提交后改写了这段未推送的历史，新历史零命中。5 个设计稿没画入口的组件（链路检索、指定所有者、管理员审核、模型单价、服务令牌）已写好、未挂载，挂载方法见 frontend-progress.md F4 节，等用户决定。契约补充 19 条见 contract-changes.md F4 节。
- 2026-11-02：B4c 完成（分支 feat/onto-modeling-plugin-p2-packs 075ed5b8..cee5dcdb，13 个提交，未推远端）：扩展项注册表与左栏页面、插件产物登记与通用视图、拆出 7 个可选包（锁文件升到版本 2，存量项目一次性补齐）、配额、行业包「零售连锁」（合成内容）与知识分层接口、4 个登记点、脚本页、SV 修正，ADR 0040～0044。Linux 全量 2148 过 / 1 既有失败（起点 2034 / 1）。遗留见 gaps.md B4c（PL-04/19 等 P3、行业示例与提名审核、配额只拦发消息和发命令）。合并子任务与独立审查并行进行中。
- 2026-11-02：B4c 合回交付分支（merge 4b9aa91c），衔接接在 `app/wiring.py`（ADR-0045）：页面按钮解析成命令、提交后的产物通知、扩展项比较、产物与知识事件走项目流、知识信号只在启用知识沉淀包时注入。独立只读审查确认的 12 条缺陷（流程图与场景保存丢内容、配额可绕过、提名不脱敏、目录链接越界、core 回滚后装配失败、官方身份自声明等）全部修复，用例在 `tests/test_pack_review_fixes.py`。主线到 266b6cc8，Linux 全量 2239 过 / 1 既有失败。设计文档、契约、交接文档的脱敏副本进仓库 `modeling/onto-modeling/docs/plugin-architecture/` 与 `docs/api-v1.md`。按用户要求在此暂停，B4b 未启动。
