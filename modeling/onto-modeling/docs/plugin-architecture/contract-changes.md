# 契约变更记录

- 2026-11-01（主控）：PB 发布版本号 `0.{n}.0` 必须严格大于该项目已有的全部 `release/*` 标签（按 semver 比较，含旧的 `0.1.N`）。不满足时（例如 n=1 而旧版发过 0.1.57），改用 `0.{已有最大 minor}.{该 minor 下最大 patch + 1}`；同一提交重复发布仍沿用原标签。发布记录里 `version` 与 `version_label` 分开存，界面上的主线版本 `v0.{n}` 不变。

## 2026-11-01 · B1（评测移出）

1. **新增：求解验证候选包** `GET /api/v1/projects/{id}/solver/candidate[?allow_unbound=true]` · 服务令牌 `receipts.write` · 审计：否。
   - 原因：X-04 要求回执绑定求解契约修订号，但求解包在拿到「权威通过」回执之前发布不了（门禁），评测服务又只能从 PB-07 拿已发布的包——没有这一步就是死循环。移出前由 onto 自己导出未发布的包交给 modeling 验证。
   - 行为：按 HEAD 提交渲染，manifest 标 `status: draft`，不打标签、不写发布记录；只跳过验证门禁，静态检查与数据绑定照发布同口径拦截。响应 `application/zip`，响应头 `X-Package-Version`（发布时会得到的版本）、`X-Package-Commit`、`X-Solver-Revision`、`X-Solver-Contract-Digest`。
   - 错误：`solver_validation_not_required`（400，当前提交没有启用的求解契约）、`solver_contract_invalid`（400，静态检查不通过）、`binding_problems`（400，数据绑定不通过，可带 `allow_unbound=true`）。
   - 建议在 3.21 补一条 X-07。
2. **X-05 请求补充字段**：顶层可选 `summary: {score, total, pass, partial, fail, error}`；`items[]` 可选 `verdict`（`fail|partial|error`，缺省 `fail`）与 `tool_failures: string[]`。
   - 原因：反馈演进（`/ontology-evolution`）按「运行异常 / 答错」分别归因，运行异常要看执行轨迹里的工具失败行；契约里的 `loss_reason` 是自由文本，区分不了。字段都是可选的，不传时行为与契约一致。
3. **X-04 / X-05 的幂等语义**：同一 `receipt_id` / `feedback_id` 重复提交返回第一次的结果，状态码 `200`（首次 `201`）。契约只写了「幂等键」，没写重复提交的状态码。
4. **PB-07 补充响应头** `X-Package-Sha256`（留存包的摘要，下游可校验）。
5. **对象形状**（第 7 章还没有定义，B1 先这样返回，第 7 章定稿后按它对齐）：
   - `ReleaseRecord`：`{project_id, version, tag, commit, format: "ontology"|"solver"|null, released_at, released_by, degraded, package: {sha256, size}|null, pulls: {count, last_at}, manifest_url, download_url}`；`format`、`package` 为 null 表示该版本还没有留存包（本功能之前打的标签，第一次下载后补上）。
   - `ReleaseManifest`：`{project_id, version, commit, format, sha256, size, manifest: <manifest.yaml 内容>, files: [{path, size}]（≤2000）, truncated}`。
   - `Receipt`：验证记录原样（`id, receipt_id, revision, solver_keys, status, authoritative, contract_digest, request_digest, result_digest, release_version, candidate_count, error_code, message, evidence, by, created_at, completed_at`），时间为 RFC 3339。
   - `Feedback`：`{feedback_id, path, source, release_version, item_count, verdicts: {fail, partial, error}, received_at, received_by, commit}`。
6. **版本号仍是 `0.1.N`**：契约 3.20 的 `0.{n}.0` 属于 A9（B2），B1 不改；PB-05/06/07 按 `\d+.\d+.\d+` 校验版本，B2 改版本号后不用动。

## 2026-11-01 · F1（前端：设计系统、外壳、数据层、入口页、工作台对话）

1. **出处角标的写法**：`message.assistant.text_md`、`Question.background_md`、`step_output.steps[].text_md` 里的角标统一写成 Markdown 脚注引用 `[^n]`，`n` 对应同一对象 `anchors[].n`；条目链接仍是 `[[名称]]`。
   - 原因：契约只写了「出处角标按 anchors 编号」，没定正文里怎么标位置，前后端要用同一个记号。
2. **P-01 的 `active`（界面「进行中」）改为：未归档，且不处于「可发布」**。`counts.active` 同口径。
   - 原因：设计稿筛选是「全部 5 · 进行中 4 · 可发布 1」，可发布的项目不算进行中；契约原写「未归档」会和「全部」一样。
3. **`PackCard` 加 `default_on: bool`**：新建向导里默认勾选（行业包、系统知识包、能力包）。不带 `project_id` 时，离线部署不可用的包 `project_status` 仍返回 `offline`（其余为 null）。
   - 原因：P-02 写了「capabilities 缺省取推荐值」，但目录里没有字段说明哪些是推荐默认开的；`badges: recommended` 在设计稿里只给「场景走查」挂「推荐」标，和默认开启的 5 个能力不是一回事。新建向导没有项目 id，离线禁用也要能显示。
4. **`Option` 加 `dismiss?: bool`**：请专家确认卡（`Question.kind = expert`）里「不需要问这个」这类选项置 true，界面放成右侧的小按钮。
   - 原因：4.6 写了「含 dismiss 型选项」，但 `Option` 没有字段能认出它。
5. **`Question.kind = walkthrough` 加 `operation?: EntryRef`**：交互走查卡标题下第一个标签（设计稿 chat-states 第 20 项）。

## 2026-11-01 · B2 · 非项目级路由

接口编号见 `api-v1.md`。没写的字段与契约一致。

1. **金额币种**：模型单价表（方案 B14）落地前，费用只有引擎 SDK 报告的美元数。`C-01 billing.currency` 返回 `"USD"`，运维面的 `Money.currency` 同为 `"USD"`；B14 落地后改回平台配置（默认 CNY）。
2. **`UserProfile` 加 `roles: string[]`**（生效的全部角色）。本地模式 `role` 取 `"admin"`、`roles` 为 `["user","admin","ops"]`。原因：契约 1.3 说本地模式同时拥有三种角色，`role` 单值表达不了。
3. **认证接口在本地模式下**：A-01、A-04 返回 `404 not_found`（与 A-02 在鉴权开启时 404 对称）；A-03 返回 `403 registration_disabled`；A-06 返回 `403 forbidden`，`details.reason="local_mode"`。
4. **A-05、A-06 的吊销范围**：auth-server 的 `/revoke` 与改密都是按用户整体作废（`token_version+1`），当前设备的令牌也失效，前端要回登录页。契约写的是「其他设备的刷新令牌全部作废」。
5. **A-07 `last_login`**：取本服务记下的最近一次成功登录（内存，重启后为 null）；auth-server 不记登录时间。
6. **A-09 `PersonalEngineConfig`**：`context` 给运行时实际生效的值（个人 Key 只换网关、Key 与模型，上下文参数仍按管理员配置，推荐表不参与运行时取值）；`admin_summary` 是 `{gateway_host, model}`，未配置时为 null。
7. **A-10 `source=admin|deployment` 等于清除个人配置**：现有存储没有「存着但不用」的状态。`base_url`、`model` 不传表示沿用已存值。
8. **SY-01 引擎项**加 `configured`；`context` 是配置页保存的值（null 表示留空），其中 `compact_at_pct` 总是实际生效值；`context_defaults` 是 `{window: {value, source, env}, max_output: {...}, compact_at_pct: {value, source: "env"|"derived"}}`。
9. **SY-02**：字段不传表示不改；`context` 里的字段传 null 表示留空。`compact_at_pct` 会校验 50–95，但不保存：压缩触发点由服务端按窗口与最大输出推算（或 `.env`），见 gaps。
10. **SY-05～SY-07**：`rows[]` 加 `aliases`、`sources`；顶层加 `builtin_verified_at`；`custom` 加 `loaded_at`、`backup_available`，没有自定义表时 `status="disabled"`。SY-06 的行可带 `aliases`、`sources`，不带时沿用已存的同名条目；校验失败 `400 invalid_argument`，`details.error={row, column, message}`。SY-07 响应同 SY-05。
11. **SY-10/11** 加 `detail`（未连接原因）；`packages[].purpose` 为 null（沙箱只报包名和版本）。
12. **SY-12/13**：项加 `args`、`enabled`；`transport` 还保留现有的 `"sse"`；`auth.kind` 只支持 `none`，传 `bearer` 返回 `400 invalid_argument`；`status` 恒为 `unknown`、`tools` 为空（不落盘、不做 MCP 握手）。SY-13 加 `detail`，`tools` 恒为空。
13. **AD-01** 响应带 `total`（分页统一形状）；`stats.new_7d`、`stats.locked` 为 null（auth-server 不记创建时间、没有锁定）。
14. **AD-03 `require_change` 忽略**（auth-server 不支持）。
15. **AD-04**：用户名下没有项目时可以不传 `projects`；`projects="trash"` 返回 `400 invalid_argument`，`details.fields.projects="trash_unsupported"`（删除已是永久删除，没有回收站去向）。响应 `trashed` 恒为 0。
16. **O-01**：`users[].calls_today` 为 null（用量事件不带发起人）；`stats.autobuild.limit` 为 null；没配管理令牌时 `stats.accounts` 为 null；`autobuild_today.avg_cost.amount` 为 null。
17. **O-13**：`dependencies[].name` 现有 `model_gateway`（只查是否配置）、`auth_server`、`sandbox_gateway`、`disk`、`git`；结果缓存 30 秒（按需刷新，不是后台定时）；`runtime.last_exit` 为 null。
18. **O-15** 项加 `conversation_id`、`connected`。
19. **O-30/O-32/O-33（过渡，B3 包仓库前）**：core 只有一份提示词资产。`version` 取 `"current"`（当前资产）或资产备份名 `dotclaude-<epoch>`（channel `retired`）；O-32 请求 `{version}` 填备份名；O-33 `keep` 为 null（资产备份现在不自动清理）。O-30 一并按这个口径实现，供 O-32 返回同一形状、410 的 successor 可达。
20. **O-36**：`schedule` 是 `{daily_at: null, interval_hours, next_due_at, keep, dir_volume}`（现状按间隔定时，没有固定时刻）；`scope` 多 `service_tokens`、`releases`。
21. **O-37**：备份同步打完再回 `202 {task_id, name}`；`task_id` 暂时查不到（任务中心 O-40 属于以后阶段）。
22. **O-48 `AuditEntry` 投影**：`status`、`ip`、`trailer` 为 null，`trace_id` 只有新记录有（`audit.jsonl` 不记这些）；`action` 取记录里的动作名，没有时为 `http.<method>`；`id` 是 `aud-<摘要>`；响应带 `total`。游标按日志行号，翻页期间新写入不会错位。
23. **O-50 `ErrorRecord`** 加 `project_id`、`source`；`pack`、`node` 为 null；`id` 优先取原文里的 `err-xxxxxx`，旧记录按行号摘要生成。
24. **O-52**：`retention_days`、`purge_at` 为 null：回收站只剩旧版软删除的存量条目，不自动清理。
25. **M-01**：`endpoints[].url` 用 v1 路径；`client_config_templates.full` 两个工具集分别用 `<READ_TOKEN>`、`<CHANGE_TOKEN>`；客户端配置里的服务器名沿用 `modeling-read`、`modeling-change`。**M-03**：token 的权限范围覆盖不到的工具集，`client_config` 对应项为空串。

## 2026-11-01 · B2 · 项目级接口、发布与会话

接口编号见 `api-v1.md`。没写的字段与契约一致；契约里依赖以后阶段能力的字段（分段、自主构建、变更集、写入范围锁、执行记录、命令任务、证据、包）给 null、0 或空列表。

1. **ReleaseRecord 按第 7.14 节对齐**（B1 第 5 条的临时形状作废）：`{version, version_label, commit, tag, at, by, format, format_label, gates: {all_passed, degraded, unbound_objects}, manifest_attached, pulls: [{at, by_token, variant}], feedback_count, size, sha256}`，另带 `project_id`、`manifest_url`、`download_url`（三份契约的下游直接用）。没有留存包的旧标签 `manifest_attached=false`、`format`/`size`/`sha256` 为 null。PB-04 响应只有 `{items, next_cursor}`。
2. **ReleaseManifest（PB-06）**：7.14 的字段之外保留 B1 的 `format`、`sha256`、`size`、`manifest`（manifest.yaml 全文）、`truncated`；`files[]` 只有 `{path, size}`（没有逐文件 sha256）；`packs`、`flow_hash`、`solver` 在插件运行时（P1）前为空。
3. **PB-01 发布检查**：`gates` 现在是平台三道——`data_binding`（可降级）、`solver_receipt`（启用求解契约时才有）、`package_structure`（脚本绑定、入参形态、文档元数据、发布树安全，按降级方式试渲染一次得出）；`binding.issues[]` 在 BindingIssue 之外带 `blocking`；`detail` 为 null。`refresh` 参数接受但不缓存（每次现算）。
4. **PB-02**：`kind=anchor` 在证据快照（P3）前返回 `404 anchor_not_found`；`binding` 恒为 null。
5. **PB-03**：门禁不通过 `422 gate_blocked`，`details.gates` 只列不通过的项；门禁之后渲染才发现的问题同样 `gate_blocked`，`details.reason` 带内部码。
6. **SV-01**：`operations[]` 另带 `binding`（全量）与 `template_params`（还原的模板参数），面板编辑后原样回传 SV-02；`evaluator.kind` 恒为 `script`；`problem_families[].label` 暂取 id。`revision_no` 是受管目录在主线上被提交改动的次数。SV-02 结构被拒返回 `422 solver_contract_rejected`（`details.findings`），`contract.operations` 不是列表返回 `400 solver_contract_invalid`。门禁 finding 码按 2.9 改名为 `solver_receipt_required/stale/failed`（只在接口层改名，内部记录不变）。SV-03 的 `evaluator` 为 null。
7. **SV-04**：`items[]`、`latest` 是 Receipt（B1 第 5 条形状）加 `revision_no`。
8. **P-01**：`counts.autobuild`、`counts.publishable` 恒为 0，卡片 `publishable` 为 null——门禁结论要逐项目渲染，列表不现算（流程运行时与待处理汇总落地后由缓存提供）。`status=autobuild|publishable` 因此返回空列表。卡片的 `landing_page` 恒为 `wb-l2`。
9. **P-02**：`industry_packs`、`system_packs` 非空返回 `404 pack_not_found`（包仓库在 B3）；`capabilities`、`starting_points` 接受但不生效；`example` 只认内置示例（任何 `id` 都注入同一个示例领域）。`package_name` 等于项目 ID。
10. **P-05**：`research_mode=web` 在联网调研包启用前返回 `409 pack_not_enabled`（缺省 `local`）。
11. **P-08**：`include_history` 缺省 true 时复制 git 历史，但不带 `release/*` 标签（那是原项目的发布记录）；`include_snapshots` 接受但没有内容可复制；`include_conversations=true` 时复制对话记录、不复制续接点。
12. **P-10**：`blockers[].code` 现在只区分 `unknown_format`、`no_objects`、`solver_contract_invalid`（`symlink`、`path_traversal` 等成员在导入时直接跳过，不作为阻断项）；跳过的顶层条目放在 `warnings`（`skipped_entries`）；`after_import.capabilities`、`evidence_sources` 为空。迁移包不再是阻断项（P-11 自动识别）。
13. **P-11**：迁移包重名时候选名以包里记录的原名为基础编号（与旧导入接口一致）。导入不作为变更集（变更集在 P2）。
14. **P-12** GET 的 `model_source`：当前用户有个人 Key 为 `personal`，管理员配置了网关或 Key 为 `admin`，否则 `deployment`。**P-14**：内置的只读服务器（evidence、flow）还没有；`status` 按全局开关给 `running|stopped`，`tools` 为空。
15. **P-16**：`cost.currency` 为 `USD`（引擎回报的美元金额，单价表 B14 之前不换算）。
16. **F-02**：`lock` 恒为 null。**F-03**：`resource/` 下只允许写 `.drawio`（资料页编辑 drawio 后保存，契约 F-03 页面），其余资料返回 `403 managed_path`；`check_rejected` 在写入路径（C1）前不会出现。`revision_conflict.details` 的 `changed_by`：文件与最近提交不同（未提交的改动）记 `assistant`，否则取最近提交的来源。
17. **F-07**：资料组只搜文本类文件（docx、pdf 要等文件提供方 E14）；`artifacts` 组为空；`term_summary.referenced_by` 是条目组里命中的条目数。**F-08** 跑内核资产里的 `query.py`（不跑工作区副本）。
18. **R-01**：`Material.referenced` 为 0、`coverage` 为 null、`uploaded_at` 取文件修改时间；`feedback_group.enabled=false`。**R-02**：`is_revision`、`batch_id` 接受但 `command_task` 恒为 null（页面命令 S-20 在以后阶段）。**R-03** `reread_task`、**R-04** `broken_references` 同理为 null、0。
19. **RP-02、RP-03**（旧 report-items 的去向）：`report_id` 是 `reports/` 下的相对路径去掉 `.md`；报告条目沿用现有启发式解析，`ReportItem` 为 `{id, group, text_md, level, status: "pending"}`；`Report` 给 `{id, path, title, type, summary, last_change, items, item_total, repair, arbitration, extras}`。
20. **ST-02**：节点 `status` 恒为 `confirmed`，`clusters` 为空；`chains` 是规则 / 指标节点及其相邻节点。边 `kind`：`references` 关系为 `mention`，其余为 `declared`。**ST-03**：`status` 只有 `confirmed`、`editing`（本人有草稿）。**ST-04**：`fields[].evidence` 都是缺失；`backlinks[].relation` 恒为 `references`；`data_source` 读「# 数据来源」首行。**ST-05**：`business_flow` 为空结构。**ST-07** PUT 只收 `content`（结构化表单 `form` 要等元模型 C3），草稿 `form` 为 null。**ST-08** `needs_review` 的 `command_task` 为 null（复核结果在对话里）；`rejected` 的 `errors/warnings` 是 Finding（码 `check_rejected`、`needs_review`）。
21. **K-***：知识条目存放在 core 的 `ontology/knowledge/<type>-<slug>.md`（工具箱 knowledge_helper 管理，助手加载的就是它），`knowledge_id` 是文件名去扩展名；类型名与工具箱互相换算（`term`↔`terminology`、`domain_rule`↔`domain-rule`）。旧版网页写在 `knowledge/<名称>.md` 的条目照常列出、可删除，修改返回 `403 artifact_readonly`（`details.reason="legacy"`）。K-04 不能改类型。`conflicts` 恒为空、`review_task` 为 null（冲突检查 K-06 随知识沉淀包）。没有行业层，`layers` 只有项目层。
22. **V-01**：`Version.run` 为 null、`needs_review` 为 0；`source` 优先取 trailer `Onto-Source`，存量提交按提交说明前缀推断（`web:`/`knowledge:`→web，`revert:`→revert，`import:`/`clone:`→import，`mcp:`→mcp，`init:`/`chore:`/`feedback:`→plugin，其余→assistant）；`tags` 只含用户标记（发布标签放在 `release`）。**V-06** 是整树恢复（方案 C1 的口径，之后新增的文件也删掉）。
23. **S-01～S-13（会话）**：对话流与历史的信封按 1.11，事件名按 6.2 改成点号形式；**载荷沿用会话层现有字段**（放在 `data` 里），第 4 章的逐类载荷随会话宿主改造（P2）收敛。`model_usage` 不再单独发出（并入 `turn.completed`，用量看 P-16）；`subagent.updated` 的 `data.phase` 为 `started|finished`；`card` 只有 `card_type=round_changes`（来自旧的 workspace_changes）。S-05 的 `turn_started_at`、`active_exec`、`open_changeset`、`open_questions` 为空值，`pending_permission.expires_at` 为 null。S-07：附件按上传落到资料区（各自一个版本）并在消息前注明，引用的条目以一句话附在消息后；`client_message_id` 的幂等只在进程内记最近 512 条；`message_id` 为 `m-<seq>`。S-11：`question_id` 就是确认请求 ID（AskUserQuestion），选项 ID 为「问题序号.选项序号」（如 `0.1`），响应 `question` 为 `{id, state, answers}`。路径里的对话 ID 可写 `-` 表示引擎当前绑定的对话（兼容期旧接口没带对话时用）。
24. **U-01**：`purpose=material` 的 `detected` 恒有对象（识别不出时 `purpose` 为 null）。上传者在本地模式下记为 `local`。

## 2026-11-01 · B3a · 插件运行时与流程运行时

接口编号见 `api-v1.md`。没写的字段与契约一致；依赖 P2 以后能力的字段给 null、0 或空列表。

1. **FL-01 `GET /flow`（P1 只读）**：
   - 节点就是现有 10 个阶段加按需的「增量更新」（`core.research`、`core.flowchart`、`core.object`、`core.operation`、`core.rule_metric`、`core.validate`、`core.mock`、`core.trace`、`core.codegen`、`core.solver`、`core.update`）；按段的建模节点、全局一致性、发布节点在 P2 按详设 6.8 重排。
   - `state` 只有 `confirmed`（完成判定满足）、`running`（有这个节点命令的执行锁）、`ready`、`not_ready`；`flags` 全 false，`progress` 为 null，`slices`、`slice_stages`、`notices` 为空。
   - 额外字段：`stage`（state_helper 的旧版阶段名）、`progress: {done, total}`（项目列表的 x/N 同一口径）、`commands: {命令名: 节点 ID}`（前端「运行中的命令属于哪个阶段」用，替代 `stageForCommand`）；节点多 `optional`、`panel`（入口在面板、不走对话）、`counts_in_progress`；`actions[]` 是 `{kind: "start", label, target: 命令文本, verify?}`；`sections[]` 是 `{id, title, nodes: 节点 ID[]}`。
   - `version` 是 `core@<锁定版本>`；没有装配目录的旧工作区是 `core@image`。
   - `suggestions` 只按主线给（第一个没完成的必经步骤的快捷操作；资料分析待核验时核验排第一；校验待审阅时「继续校验」；都完成时「整体视图」+「扫描脚本候选」），`mode` 恒为 `normal`；`conversation_id` 接受但不参与（打断、过期、报告错误的优先级在 P2）。
   - 「质量检查完成」的口径改为：有校验报告（`reports/*-rule.md` 或 `*-semantic.md`，示例数据与流程图的检查报告不算）且 `reports/` 下没有未处理的 `[ ]`。P-01 / P-04 的 `stages_done` 同口径。
2. **FL-02** 与 FL-01 的 `suggestions` 是同一份。FL-03～FL-05 未做（P2）。
3. **PL-01**：带 `project_id` 时要求是项目所有者；`recommendations` 恒为空；`project_status` 只出 `required`、`enabled`、`update`、`pending`、`available`（可替换实现为 `platform_installed`），不出 `offline`、`not_applicable`、`platform_available`；`usage_note` 为 null；`batch`、`badges`、`default_on` 读包清单的同名字段（`default_on` 缺省时必装包为 true）；`counts.enabled` 含待生效的。
4. **PL-02**：多 `hot_level`（H0/H1/H2）、`effective`、`released_by`、`files`（文件数）、`size`（字节）；`project_stats` 为 null；`flow_insertions[]` 是 `{id, title}`（title 暂取 ID）；`changelog[].notes` 取包清单的 `changelog` 字段，没有时为入库备注；`config` 原样给清单的 `contributes.config`。
5. **PL-03**：`checks[].id` 六项齐全；`names`、`flow` 两项靠试装配一遍得出（不改锁文件、不留装配目录）；升级 core 时，已启用的其他包对 core 的依赖范围也算进 `depends`。`impact` 恒为 0（节点完成判定变化导致的「需复核」随 P2 节点状态）；`brings.nodes[].title` 暂取 ID。`effective` 只有 `immediate`（纯数据包）与 `next_message`。
6. **PL-05**：`ProjectPack` 多 `follow`（是否跟随通道）；停用过的包保留在锁文件（`enabled=false`），`status="disabled"`、`actions=["enable"]`，重新启用缺省回到原锁定版本；`pending_switches` 是锁文件与生效中装配的差异条数。
7. **PL-06～PL-09**：锁文件改动直接作为一次主线提交合入（提交说明带 `Onto-Source: plugin`、`Onto-Author`），不是变更集（P2）；先装好新装配目录再提交，装配不了返回 `pack_incompatible`（`details.checks`）、`flow_assembly_failed` 或 `assembly_failed`，锁文件不动。`effective`：本次切换已立即生效时为 `immediate`（只动了纯数据包），否则 `next_message`。`extensions_pending_hash` 暂给待生效的装配哈希（扩展项注册表 PL-12 在 P2）。PL-07 的 `effective=next_autobuild` 在自主构建（P3）前按下一条消息生效处理；升级后旧「已确认」标「需复核」未做（P2）。升级到通道最新版恢复跟随通道，回滚则锁定版本、不再跟随。
8. **PL-10** 响应 `{pack: ProjectPack}`；把锁文件里这个包改回生效中装配目录记录的版本（或去掉）。
9. **PL-11**：`effective` 取 `immediate` 或 `next_message`；`last_error` 只在 `state="failed"` 时给。
10. **PL-13**：响应带 `name`、`version`（检查前为 null）；静态检查在响应之后跑。旧路径 `POST /api/settings/assets/upload` 改写到这里（带弃用头）：没有清单、带 `commands/` 与 `agents/` 的资产包当作 core 新版本（去掉现在归内核的守卫与 `settings.json`，从镜像补工作区规则、说明与流程定义，版本号为 stable 的基础版本加内容摘要），检查通过直接发布到 stable。
11. **PL-15**：`checks[].id` 取 `manifest`、`checksums`、`symlinks`、`declared`、`compat`、`names`、`entity_types`、`write_scope`、`node_entry`、`hooks`、`frontend`、`secrets`、`syntax`、`prompt_budget`（租户上传 core 时另有 `tenant_core`）；官方包的 HTML 模板带脚本只给警告，租户包判失败。
12. **PL-17**：`rollout`、`auto_breaker` 原样存进通道记录，灰度放量与自动熔断（O-26、O-29）还没执行。新错误码 **`pack_version_exists`（409）**：同名同版本已在包仓库里且内容不同（版本目录不可修改）。
13. **PL-18**：不通知上传者（通知中心 A-13 以后做）。PL-04、PL-12、PL-19 未做。
14. **O-30～O-34 改由包仓库承接**：O-30 的 `versions` 是包仓库里 core 的全部版本，`released_at` 为 RFC 3339；O-31 推广到 stable 已实现；O-32 先打一份包仓库备份再改 stable 指针；O-33 列包仓库备份（`packs/` 的 tar.gz，不含上传区），`keep=3`；O-34 同步打完再回 `202 {task_id, name}`。B2 第 19 条的过渡口径（`current`、`dotclaude-<epoch>`）作废。
15. **EC-06 `session.config_pending`**：运行中发现配置指纹变了时发一次，`data` 为 `{changes: [{kind, summary}], effective: "next_message", detail_link: null}`；`kind` 另有 `owner`（模型配置归属变了），没有 `engine`（换引擎走新会话）。**EC-07 `session.reconnected`**：因配置变化重连后发，`data` 为 `{reason: "config_changed", changes: kind[], context_kept, duration_ms}`。

## 2026-11-01 · F2（前端：Studio、报告、流程图、待处理、自主构建、资料分析、建模蓝图）

### Studio 与报告（F2-A）

1. **`EntryForm.relations` 只放本条目自己声明的关系**，等于 `Entry.relations.declared` 去掉 `backlinks` 里的来源；`declared` 本身含双向。
   - 原因：设计稿「关系声明」列表里有「被读取」这种反向关系，但编辑器只管本条目文件里写的。
2. **`Report.repair.rounds` 里 `n > limit` 的那一项表示「现在」还剩几项。**
   - 原因：设计稿的柱状图在「第 1 轮」「第 2 轮」之后还有一根「现在」。
3. **报告类型专属内容 `extras` 的形状：**
   - 链接检查：`{orphans, unused, dangling}`，都是整数计数；具体问题在 `ReportItem` 里，`code` 用 `LINK-ORPHAN` / `LINK-UNUSED` / `LINK-DANGLING` 区分。
   - 资料分析：`{total, read_pct, found: {items, terms, rule_candidates}, candidates: {objects, rules}, open_questions, contradictions: {total, resolved}, purposes: [{value, count}], items: MaterialMapItem[], problems, verification}`。
   - 原因：契约只写了「见 MaterialMap 的摘要」，前后端要按同一个形状。

### 流程图编辑弹窗（F2-B）

4. **`FlowchartModel` 节点的 `x`、`y` 是节点中心坐标**；节点尺寸由前端按类型和文字长度算，不存进模型。Studio 的流程图预览也按中心坐标画。
5. **节点加可选字段 `pending_note: string|null`**：待定口径的几种候选说法，弹窗的提示里会用到。
6. **AR-05 保存流程图的两种提交：**
   - 标准编辑器提交 `FlowchartModel`，`drawio_xml` 为 null，由图示包按模型重写 drawio 文件；
   - drawio 高级编辑提交时带 `drawio_xml`：压缩存储原样写回；明文时由图示包解析出节点和连线，同 id 的业务说明、关联条目、待定标记保留。
7. **AR-08 的 `Finding.field` 写成 `nodes/<id>` 或 `edges/<id>`**，界面靠它定位到节点或连线。
8. **AR-02 里 `diagram.flowchart` 的 `ArtifactRow.fields` 为 `{operation, summary, state, source_label, updated_by}`**，Studio 表格也读它。

### 待处理与自主构建（F2-C）

9. **`slice_progress.timeline` 里可以穿插 `{type: "auto_decision"|"suspension", card: <同名卡片结构>}`**，`decision_ids`、`suspension_ids` 保留。
   - 原因：设计稿把自动决定和挂起按时间画在分段块里，只给 id 排不出顺序。
10. **`AutobuildRun`、`AutobuildReport` 加 `base_version_label`（运行前版本）**，报告的 `slices[]` 加 `reverted`。用于「回退到运行前 · v0.12」和已回退的行。
11. **IN-02 的 `review` 详情加 `base_version_label`、`slice_version_label`。**
12. **高置信合并项**：同一次运行里还没复核、置信度 ≥ 阈值的自动决定合成一项。
    - id 为 `review:<run_id>:high`，带 `high_confidence: true`；
    - `detail` 为 `{group: "high_confidence", run_id, threshold, entries: LedgerEntry[]}`；
    - 对它发 IN-03 `accept` 等价于 AB-13；单条改判走 AB-11 / AB-12。
13. **`LedgerEntry` 加 `summary`**：分段简称加决定，一句话。
14. **IN-01 的 `autobuild_summary` 加 `high_confidence_pending`**，「一键接受高置信 N 项」用它。
15. **`ConflictDetail.sides[]` 加可选的 `highlight`（原文里标黄的片段）和 `anchor_label`（「出处：…」）。**
16. **IN-02 的 `feedback` 详情加 `items: [{question, expected, actual, loss_reason}]`。**
17. **`Suspension` 在 `kind=unreadable_material` 时加 `material: {path, name}`**，「上传文本版」靠它跳到资料区的那个文件。

### 资料分析与建模蓝图（F2-D）

18. **`SuggestionAction` 加两种类型**：`{type: "interrupt"}`（调 S-08）和 `{type: "confirm_blueprint", next?: "L2"|"L4"}`（BP-03 加前端的确认后流程）；`navigate.params.dialog` 可以是 `material_map|verification|blueprint|build`，到目标页直接打开弹窗。
    - 原因：现有动作类型表达不了「打断」和「确认蓝图」。
19. **`MaterialMap.problems[].anchors` 由 `string[]` 改为 `{n, anchor, label}[]`**；`text_md` 用 `[^n]` 标出处，允许 `**粗体**`。
    - 原因：出处 chip 要显示「规则 §9.2」这样的短标签，和 `Question.anchors`、本步产出卡的 `sources` 一致。
20. **`Blueprint` 和 `blueprint` 卡加 `goal_anchor_label`**，原因同上。
21. **`ProgressStep.state` 加 `"dot"`**：表示「接下来」的一步，灰点、不带用时。`parts.jsx` 已能渲染。

## 2026-11-01 · B3b（元模型、写入事务、治理平面、密钥隔离）

接口编号见 `api-v1.md`。没写的字段与契约一致。

1. **C-05 `Metamodel` 形状**（第 7 章没定义）：`{version, entity_types: [{kind, label, directory, readonly_aliases[], suffix, recursive, sections: [{title, kind: "text"|"table", required, columns?}]}], section_aliases, link_syntax: {pattern, anchor_separator: "#", alias_separator: "|", escaped_alias_separator: "\\|", path_prefix: "last_segment", resolution: ["full_name", "name", "encoded_full_name", "encoded_name"], empty_targets[]}, relation_syntax: {section, arrows[], current_object}, filename_encoding: {scheme: "percent", escaped[], max_key_length}, index_file}`。属性表的「证据」列随证据源（P3）加。
   - 原因：前端要按它生成表单与解析链接，链接里 `#` 是锚点、全名优先匹配的规则必须给前端。
2. **O-35 `items[].id` 加 `write_intents`**（写入事务留下的意图，`result` 取 `committed|ok|failed`），排在 `index_lock` 之后、`uncommitted` 之前；`last_exit` 现在有值：`{at, reason: "unclean"|"shutdown", code: null|0}`。
   - 原因：详设 8.3 措施 2「启动时把没提交的意图补提交」要在自检里看得到。
3. **SY-14 `ConfigItem` 加 `description`、`precedence`（该项自己的来源优先级）**，来自插件时带 `pack`。顶层 `precedence` 是默认顺序 `env > data_dir > pack > default`；模型接入、权限模式、会话池这几项是设置页优先（`data_dir > env`）。调用方：admin 与 ops。
   - 原因：方案 E5「每项声明来源优先级」，各项不一样，单一顶层顺序表达不了。
4. **C-04**：带有效令牌（或本地模式）请求、启动恢复还没做完时，`checks` 多一项 `{name: "startup_recovery", status: "fail"}`；`connector_gateway` 在 P3 前恒为 `ok`（detail「未部署」），`assembly` 由插件运行时注册，注册前恒为 `ok`。
5. **O-13 `runtime` 加 `restarts`**（累计重启次数）；`dependencies[].status` 的 `ready` 对应就绪检查的 `ok`，与 C-04 共用一份探测（30 秒缓存）。**O-14** 响应就是 `dependencies` 数组。
6. **O-02**：数值来自进程内指标，重启清零；`merges`、`collections` 在 P2、P3 前恒为 0（`note` 写明）；`pool.series` 是当前值的平铺。
7. **O-03**：只统计带 `pack` / `node` 标签的样本（插件运行时接入后才有数据）；`reruns`、`first_accept_pct` 为 null。
8. **O-06**：`level` 取 `DEBUG|INFO|WARN|ERROR`；`fields` 里带 `message`（脱敏后的日志正文）。**O-07** 事件信封 `{type: "log", seq, data}`，`data` 同 O-06 的一项。
9. **O-16** 响应 `{config}`；校验失败 `400 invalid_argument`，`details.fields` 指出字段。`overflow_policy=queue_only` 时池满不驱逐空闲会话，只排队。
10. **O-18** `reason` 必填（空返回 `400 invalid_argument`）。**O-19** `max_wait_s` 取 1–86400。
11. **`MaintenanceState` 加 `until`**（恒为 null，错误体 `details.until` 同源）。**O-21** `autobuild` 接受但不生效（L4 在 P3），`target.kind` 只认 `core_upgrade`。**O-22** 在第 4 步「恢复」再推进返回 `409 maintenance_step_blocked`（`details.step="resume"`），退出用 O-24。
12. **维护模式下的「新会话」**：项目没有已连上的引擎时发消息（S-07）返回 `503 maintenance_no_new_sessions`；已连上的会话照常收消息，排空时跑完当前这一轮再回收。只读模式放行的写请求：`/api/v1/ops/*`、`/api/v1/auth/*`（含旧路径 `/api/auth/*`）。
13. **O-29** 包与版本的存在性由插件运行时登记的查询函数判断（`ops_control.register_pack_lookup`），没登记或查不到返回 `404 pack_version_not_found`；响应是查询函数给的 O-25 一项加上 `breaker`。
14. **O-36 `scope` 加 `packs`**（包仓库 `data/packs/`）；密钥目录里的密文在 `data/secrets/` 下，主密钥不进备份。
15. **O-42**：内核、数据格式、引擎几类由内核给；包与锁文件格式由插件运行时经 `register_version_provider` 登记。
16. **O-45** `include` 为空表示全部；响应 `201 {id, name, size, created_at, created_by, expires_at}`。**O-47**、**O-49** 各记一条专项审计（`ops.diagnostics_download`、`ops.audit_export`）。
17. **O-51** `context.exec_id`、`changeset_id` 为 null（执行记录、变更集在 P2）；`stack` 是脱敏后的原文。**O-54** 响应 `202 {purged: entry}`。
18. **AuditEntry**：默认记录的 `action` 是 `http.<method>`，`type` 按路径归类（运维面上一律 `ops`，服务令牌是 `contract`）；agent 提交是 `type=commit`、`action=agent.commit`、`actor=agent`，`trailer` 取这一轮最新提交的 `Onto-*`；MCP 传输接口不记默认记录（工具写入各记一条 `mcp`）。
19. **AD-02** 可以设 `ops`（auth-server 已支持；B2 第 2 条「由 auth-server 拒绝」作废）；**P-04** 对 ops 也返回 `ProjectMetaView`（B2 只给 admin）。
20. **写入路径错误码**：ChangeTx 带 `expected_head` 时主线已前进返回 `409 head_moved`（P1 还没有接口传它）；真实 git 索引被别的 git 操作占着时不提交，`500 internal_error`。

## 2026-11-01 · F3（前端：插件页与项目页）

接口编号见 `api-v1.md`。没写的字段与契约一致。

### 主控

1. **EC-10 `message.user` 加可选 `command: string|null`**：S-20 发出的消息带命令名（不显示）。前端按 FL-01 的 `commands` 判断对话正在跑的命令属于哪个节点（替代旧 `stageForCommand`）。原因：S-20 的 `text` 是自然语言，认不出节点。
2. **FL-01 前端两种形状都认**（不改契约，记一下口径）：节点缺 `section` 时按顶层 `sections` 归组；`actions[].target` 是页面 id 时跳页，否则当命令文本经 S-20 发出；缺 `display_state` 时按 `state` 补。

### F3-A 知识沉淀与场景走查

3. 知识条目在写入范围锁里写成 `ScopeLock.artifacts` 的 `{type: "core.knowledge", key: <knowledge_id>, title}`；`KnowledgeEntry` 可带可选的 `locked: bool`（同 `ArtifactRow.locked`）。
4. `KnowledgeEntry.source.kind` 加 `"manual"`（知识沉淀页手动新增，label「手动新增」）。
5. K-06 的 `conflicts[]` 定为 `{id, kind, project_entry: {id, title}, industry_entry: {id, title}|null, note, effective}`；从没检查过时 `checked_at` 为 null。
6. K-02 的 `overrides` 加 `entry_id`；`note` 可以为 null。
7. K-01 的 `layers[]`：行业层的 `label` 是行业包显示名（如「零售连锁」）；两层的 `overridden` 都是「覆盖了行业条目的项目条目数」。
8. `trace.scenario` 的字段：`title, slice, operation, source(anchor), source_label, input_kind(custom|mock), last_run_at, result(pass|fail|stale|not_run), expected, actual, fail_reason, stale_source`；分节「输入 / 预期结论 / 推演记录」；路径 `ext/trace/scenarios/{key}.md`。
9. `trace.mock` 的字段：`object(EntryRef), scenarios, rows, mode(by_scenario|manual)`；AR-03 给 `table`；AR-02 响应多 `skipped: [{entry: EntryRef, reason}]`（不落库、跳过生成的对象）。
10. 批量推演那把写入范围锁的 `node.id` 是 `trace.regression`，前端靠它决定待确认时只读条的文字。
11. AR-09 接受 `trend=true`（推演通过率的趋势线）。

### F3-B 代码生成与求解

12. 写入范围锁里求解契约的 `ArtifactRef` 是 `{type: "codegen.solver_contract", key: solver_key}`。
13. EP-06 用到的产物类型：`codegen.script`、`codegen.scan_report`、`codegen.solver_contract`，页面收到 `codegen.*` 就刷新；`workspace.changed` 里带脚本条目或 `ontology/solver/` 路径时也刷新。
14. `ScriptRow` 不带路径，预览先用 ST-04 拿 `Entry.path` 再用 F-02 读原文；SV-03 的 `rendered[i]` 按 role 作键。SV-01 没带 `binding`、`template_params` 时，前端按 7.12 的字段拼草稿。

### F3-C 增量更新

15. **`ChangeList` 加 `conversation_id: string|null`**：清单所在的对话。原因：进入 incremental 时要切到那条对话。
16. **`round_changes`、`regression` 卡加 `change_list_id: string|null`**：带它的「这一轮」按增量更新的样子画；回归卡的「交给助手修」也带上清单。
17. **`SuggestionAction` 加 `{type: "execute_change_list", change_list_id}`**；`navigate.params.dialog` 加 `change_list`（同时带 `change_list_id`）。前端另有一个只在前端合成、不由服务端下发的 `{type: "start_incremental", reference}`。
18. **`external_feedback` 卡的 `items[]` 加 `case_id`**，「让助手分析」只发没对上变更点的用例。

### F3-D 版本与发布

19. `Version.run` 与 V-02 的 `run` 统一成 `{run_id, run_no, slice_id, slice_name}`。原因：7.6 只有 `slice_name`，V-02 只有 `slice_id`；「查看改动」要按段 id 对上，界面要显示分段名。
20. V-04：文件在某一侧不存在时那一侧给 `''`；WORKTREE 一侧文件不存在时 `right_revision` 为 null，前端据此把右侧设为只读。
21. `BindingIssue.message` 放在状态标下面那行解释；状态标文字由前端按 `kind` 决定。

### F3-E 资料区

22. `Feedback.summary` 用「 · 」分开标题和说明，资料区按第一个「 · 」拆成两行。
23. R-05 的 `location` 可以是空字符串（场景引用时不写位置）。

### F3-F 数据源、采集与快照、证据浏览器

24. EV-17 的 `retry` 请求体为 `{confirmation_token?}`；生产库缺 token 返回 428，`details` 同 EV-13 的响应（带新 token）。
25. EV-13 `summary`：`window` 是字符串（如「22:00 – 06:00」），`rate_limit` 与 EvidenceSource 的同名字段一样是对象。
26. EV-01 里「普通用户看不到的敏感字段」定为 `egress`、`authorization_note`。
27. CollectionJob 的 `error.code` 用 `credential_expired`，「联系管理员」按它映射成 EV-30 的 `reason`。
28. EV-26 接受 `prefix=true`（按整张表查被谁引用）。

### F3-G 项目设置

29. PL-05 的 `ProjectPack.locked_version` 在有待生效切换时取生效中的版本（等于 `pending.from`），切换后的版本看 `pending.to`；停用时 `pending.to` 为 null，启用时 `pending.from` 为 null。原因：设计稿「锁定版本」列在待生效时显示旧版本，chip 上写新版本。
30. PL-12 的 `pages[].pending` 只在「待生效的启用」时为 true；`disabled_pages` 列出停用的页面包。原因：待生效的升级或回滚期间页面一直可用。
31. M-01 的 `endpoints[0]` 作为界面上显示和复制的主地址。

### 页面命令（S-20）的命令名与参数（前端定的，后端实现 S-20 时对齐）

命令名按 core 的 `flow.yaml`（增量更新是 `/ontology-update`，反馈演进是 `/ontology-evolution`）；mock 的命令目录 S-18 同步改成 `/ontology-update`。

| 页面 · 按钮 | `command` | `args` |
|---|---|---|
| 场景走查 · 批量验证 | `/ontology-trace-batch` | `{scope: "all"}` |
| 场景走查 · 补齐过期场景 | `/ontology-trace-refresh` | `{keys}` |
| 场景走查 · 交互走查、行内 ▶ | `/ontology-trace-walk` | `{scenario}` |
| 场景走查 · 按场景补生成 | `/ontology-mock` | `{by: "scenario"}` |
| 知识沉淀 · 在对话里复核 | `/knowledge-review` | `{project_entry, industry_entry}` |
| 代码生成 · 扫描 | `/ontology-codegen-scan` | — |
| 代码生成 · 生成脚本 | `/ontology-codegen` | `{operations: string[]}` |
| 代码生成 · 修复 / 重新生成描述符 / 重新生成脚本 | `/ontology-codegen` | `{script, mode: "fix"\|"descriptor"\|"regenerate"}` |
| 代码生成 · 保存后生成评估器 | `/ontology-solver-evaluator` | `{solver_key}` |
| 代码生成 · 按模板重新生成 | `/ontology-solver-evaluator` | `{solver_key, mode: "template"}` |
| 增量更新 · 开始增量更新 | `/ontology-update` | `{reference}` |
| 增量更新 · 交给助手修 | `/ontology-update` | `{change_list_id, fix_scenarios: string[]}` |
| 增量更新 · 外部反馈让助手分析 | `/ontology-evolution` | `{feedback_id, case_ids}` |
| 资料区 · 外部反馈在对话里处理 | `/ontology-evolution` | `{feedback_id}` |
| 资料区 · 把资料读完 | `/ontology-research` | `{path, reading_mode: "full"}` |
| 采集与快照 · 在对话里分析影响 | `/ontology-evidence-impact` | `{from, to}` |

## 2026-11-01 · B4a（会话宿主、执行记录、崩溃恢复、项目流）

接口编号见 `api-v1.md`。没写的字段与契约一致；依赖 B4b（变更集、写入范围锁、节点状态）的字段给 null 或空。

### 事件流

1. **对话流事件 id 从 1 开始**：id = 历史行号 + 1；`?after=n` 补发 id 大于 n 的持久事件，不带 `after` 从头回放；`Last-Event-ID` 同义。S-05 `last_seq`、S-06 `first_seq / last_seq / before_seq`、S-07 响应的 `seq` 都是这个对外 id；`message_id` 为 `m-<id>`。原因：前端状态机的 `lastSeq` 从 0 起算，id 为 0 的第一条事件会被当成重复丢掉。B2 的 0 起编号作废（只改对外编号，历史文件不动）。
2. **对外 id 可以有空号**：只给内部用的记录（逐次用量 `model_usage`、子代理的内部消息、旧版的 `tool_results`）占行号、不发出。
3. **项目流与用户流**：频道缓冲 2000 条或 30 分钟；`stream.reset.reason` 为 `epoch_changed`（id 不是本进程的，含格式不对）、`gap`（已被挤出缓冲）、`overflow`（订阅者处理太慢、队列撑满，之后接着推）。首次打开（不带 id）不回放。用户流的 `resync` 是 `["P-01","A-13"]`。本地模式的用户流就是空用户名那个频道。
4. **`flow.updated`**：`nodes[]` 只含变了的节点，每项是 `{id, state, flags, progress, display_state}`；第一次（服务刚启动、没有基线）时含全部节点。`flow_hash` 变了表示装配换了节点集合，前端要整份重拉 FL-01（`mergeFlow` 不会新增节点）。
5. **`assembly.updated`** 在锁文件提交后（待生效）与消息前切换装配后（已生效）各发一次；同时发 `extensions.updated`，`changed` 在扩展项注册表（PL-12，B4c）登记比较函数之前一律给全部七类。
6. **`scope.updated`** 由登记的写入范围数据源给（B4b）；登记前一律是 `state="none"` 的空锁，一轮开始、结束时各发一次。
7. **`workspace.changed`** 的 `source` 里 `agent` 写成 `assistant`（与 VersionSource 一致）；agent 的提交不经写入事务，由宿主在每轮结束时按这一轮的提交补发一条（`commit` 是这一轮最后的提交，`by` 是项目所有者）。`artifacts` 在产物登记（B4c）接上之前为空。
8. **`conversation.updated`** 在新建、改名、删除对话与在线对话状态变化时发；删除时 `conversation` 是删除前的样子。
9. **用户流**：`project.card.updated` 在流程进度变了、项目改名 / 改目标 / 归档后发；`project.removed` 在删除（`deleted`）与用户删除时移交（`transferred`）后发。`notification`（EU-01）在通知中心（A-13）之前没有。

### 对话流载荷

10. **`message.user` 带 `command`**（F3 补充）：S-20 是命令名；S-07 消息以斜杠命令开头时也给。`text` 是用户原话（附件说明、引用条目只进发给模型的消息，不进气泡）。
11. **`message.assistant`** 只放正文；`anchors`、`entry_links` 在证据快照（P3）前为空，`continued` 表示这一轮里前面已有助手正文。思考内容只走瞬时的 `message.delta`（`kind="thinking"`），不持久。
12. **`progress`**：一轮一个组，`group_id` 为 `g-<turn_id>`，标题是这条命令的业务名（执行记录的节点标题），没有命令时是「工作过程」；工具调用写成业务话（只给文件名），子代理、执行阶段变化各一步，最多留 30 步（`desc` 计全部步数）；一轮结束 `collapsed=true`。步骤 `state` 另有 `info`（阶段变化）。
13. **`subagent.updated`** 的 `title` 是派出时的说明；Jiuwen 的子代理每一步发一次 `status="running"`，`progress_text` 是这一步的业务话。
14. **`tool.activity.tool`** 是工具原名（如 `Read`），`summary` 是业务话；`state` 取 `started|done|failed`。
15. **决策卡**：AskUserQuestion 一题一张 `question.asked`（kind `decision`）；只有一题时 `question_id` 就是确认请求 ID（与 B2 一致），多题时是 `<请求 ID>-<序号>`，题都答完才交给引擎。选项 ID 仍是「题序号.选项序号」，末尾多一项 `"<序号>.custom"`（「其他，我来说」，`custom=true`）；选项标签里带「推荐」的 `recommended=true`；`background_md` 放题目的短标题（header）；`inbox_item_id` 为 `decision:<question_id>`；`affected` 为空。超时、拒绝发 `question.withdrawn`；旧接口 S-10 带 answers 一次答完时发 `summary="已回答"` 的 `question.answered`。
16. **`permission.requested`**：`description` 按工具写成业务话；`details_text` 是工具名加参数 JSON（截 2000 字）。`permission.resolved.by` 另有 `timeout`（含服务重启后失效的旧确认卡，`behavior="deny"`）。
17. **`session.state`** 另有 `waiting_user`（有待回答的确认卡或决策卡，回答后回到 `running`）；`reason` 在 `error` 时是出错提示。`session.started.engine_label` 取引擎的显示名，`context_kept` 同 `resumed`。
18. **`session.error`**：`error_id` 与运维错误留档（O-50）同一个编号；`retryable` 在知道出错那一轮的消息时为 true，`actions` 为 `["retry","redirect"]`；`retries` 是 S-14 连续重试的次数。
19. **`session.interrupted`**：`by` 里 `restart` 也用于旧版历史的「上一轮未正常结束」；`position_label` 形如「你在「对象提取」的「写入对象」阶段打断了」；没有执行记录时 `exec_id`、`saved` 为 null。
20. **`session.queued` 改为持久事件**，`progress_pct` 是排队进度（排第一为 100）。
21. **`turn.completed.cost`** 的币种是 `USD`（同 B2 第 15 条），Jiuwen 没有单价时 `amount` 为 null。
22. **`context.compacted.kept`** 现在固定 `{decisions: 0, artifacts: true, todos: false}`（引擎不报告压缩保留了什么，产物在磁盘上总是在）。
23. **`day.marker`** 只在跨天时放（同一天接着聊、对话的第一条前面都不放），`label` 形如「11月1日」。
24. **`suggestions.updated`** 在一轮结束（同 FL-02；有待回答的问题时 `mode="waiting_decision"`、主推禁用）与你打断之后（`mode="interrupted"`：有执行记录时主推 `resume_exec`，次选「继续」「换个方向」）发；这一轮随后出的 result 不覆盖打断后的建议。
25. **卡片**：`round_changes` 现在 `state` 恒为 `merged`（变更集在 B4b，agent 的提交直接进主线）、`changeset_id` 为 null、`checks` 为 0（提交已过提交检查）、`lock_note` 为空，多 `from`、`to`、`change_list_id`；不是本体条目的路径归到「其他文件」组，`target` 是 `{type: "file", key: 路径, title: 文件名}`。`resume` 卡的 `card_id` 为 `resume-<exec_id>`，`status.key` 取 `pending`（可续跑）/ `stale`（不能续跑）/ 续跑后 `running` / 放弃后 `skipped`，`not_resumable_reason` 是业务话，另带 `state`。
26. **`notice`** 现在只在放弃执行时出（`kind="info"`，居中）。

### 接口

27. **S-05**：`turn_started_at` 有值；`active_exec` 是这条对话里开着（运行中或等待）的执行；`open_questions` 是还没答的决策卡题目 ID；`pending_permission.expires_at` 有值；`config_pending.changes` 按实际变化项给；`open_changeset`、`autobuild_run_id` 为 null。
28. **S-08** 响应的 `exec` 是被打断的执行（没有时 null）。你自己打断的不放续跑卡，回收、引擎异常结束、停机、重启时放。
29. **S-14**：只在引擎绑着这条对话且处于 `error` 时可重试，重发出错那一轮的消息；气泡显示「（重试）原话」。
30. **S-15**：`state` 可逗号分隔或重复给；游标分页同 1.6，响应只有 `{items, next_cursor}`。
31. **S-16**：只有 `state="interrupted"` 的能续跑，其余（运行中、等待中、已完成、已放弃）一律 `409 exec_not_resumable`（运行中、等待中的 `reason` 也是 `already_finished`——契约枚举里没有「还在跑」）。续跑往对话发一条带原命令的续跑指令，气泡显示「从打断处继续「…」」，接着原来那份记录。
32. **S-17**：运行中的执行先要打断（`409 session_busy`）；已完成、已放弃的返回 `409 exec_not_resumable`（`already_finished`）。
33. **S-18**：只列当前装配里的命令（`.claude/commands/*.md`），按流程节点所在分组（建模 / 发布前检查 / 可选能力 / 有需要时 / 其他）；`label` 优先取流程里按钮的叫法，`description` 是节点标题；`args_schema` 为 null。
34. **S-19**：`state` 逗号分隔，默认 `queued,running,waiting_user`；最多 50 条，新的在前。
35. **S-20**：命令文本按 `命令 键=值 …` 拼（非字符串的值写成 JSON），`text` 另起一段；命令不在当前装配里返回 `409 pack_not_enabled`（`details.command`，装配有待生效切换时 `effective="next_message"`）；`action` 由扩展项注册表登记的解析函数换成命令（B4c），没人认得返回 `409 pack_not_enabled`（`details.pack`）。另一条对话在跑时 `409 session_busy`。任务标题为「助手正在执行「<命令业务名>」」；`percent` 只在完成时为 100，`step_label` 是执行阶段。
36. **O-35** 的项按契约补全：`assembly`（`reassembled`/`failed`/`ok`）、`dependencies`（后台，做完才补进报告，带 `checks`）、`interrupted_execs`（有改判时 `awaiting_user`）、`history_repair`（`cleared`）、`purge`（后台，`cleared`）；`agent_commit_hooks` 只在有项目装不上提交钩子时出现。各项可带 `projects: [{project_id, count, paths}]`。

## 2026-11-02 · F4（前端：运维中心、插件中心、个人与系统设置）

接口编号见 `api-v1.md`。没写的字段与契约一致。标「建议」的是前端按现有口径已经能用、希望后端补的。

### F4-A 运维总览与可观测

1. **O-05 的 `labels` 加 `user`（发起人）**：链路检索按用户筛要用（建议）。
2. O-01 `startup.items[].result` 沿用 O-35 的取值（`cleared`、`committed`、`ok`、`reassembled`、`awaiting_user`、`failed`），页面按它映射标签和图标。

### F4-B 运维管控与健康

3. **O-25 每项加 `rollback_to: string|null`**：该通道的上一个版本，「回滚」按钮用它填 O-27 的 `to_version`，为 null 时按钮置灰。原因：O-25 没给可以回滚到哪个版本。
4. **O-28 禁用之后**，O-25 里该版本显示为 `channel="retired"`、灰度 0（或另加 `disabled` 字段）。原因：契约没写禁用后列表里怎么体现。

### F4-C 用户、审计与回收站

5. 前端把 O-52 的 `retention_days === null` 当作「删除用户时不能移入回收站」，AD-04 返回 `trash_unsupported` 时同样处理。建议契约补一个明确的能力字段。
6. 页面地址参数 `ops-users?tab=users|audit|errors|trash&error=<ErrorRecord.id>`：chat-states 第 15 项的错误编号可以直接链过来。

### F4-D 插件中心与插件详情

7. PL-01 不带 `project_id` 时 `counts.enabled`、`counts.updatable` 为 0（没有项目视角）。
8. PL-01 的 `usage_note`：`not_applicable` 时写适用条件（如「适用于物业服务项目」），`platform_installed` 的提供方写「<数据源 id> 在用」「未登记数据源」或「内置」。
9. **PL-02 `flow_insertions[]` 前端认 `{id, title, at?, note?, short?, kind?}`**：`at` 是插在主线哪一段（`research|model|validate|consistency|publish`），没有时按 ID 里的词猜、猜不出算建模；`note` 是图上胶囊的小字，`short` 是统计卡里的短名，`kind` 是「节点钩子 / 串入节点 / 旁支节点 / 按需节点」；另认顶层 `flow_note`（图下那句说明）。B3a 只给 `{id, title}` 时照样能画。
10. **PL-02 `project_stats` 定为 `{produced: {value, unit, note}, metric: {label, value, note}} | null`**：两张统计卡分别是「在本项目产出」（提供方是「在平台上」）和一项运行指标（如「推演通过率」）；null 时显示「—」并说明原因。
11. **PL-02 `contents[]` 定为 `{group, items: (string | {title})[], note?, total?}`**，`group` 用界面叫法（命令、助手角色、模板、检查规则、报告格式……），`total` 用于条目多的组（如「术语 312」）；没有「加进流程」「界面入口」「可调设置」组时前端用 `flow_insertions`、`ui`、`config` 补。
12. PL-02 带 `enabled_at`（本项目启用日期，英雄区与菜单弹窗「本项目在用 x · 启用于 y」用）、`released_at`、`requires: {depends: {包名: 范围}, kernel_api, page_api}`。
13. **PL-04 的申请进管理员审核（AD-08）**：`kind="pack_install"`，`payload: {pack: {name, label}, version, note}`，`summary` 为「<包显示名> <版本>」，`project_id` 为 null。
14. PL-03 依赖不满足时 `checks[id=depends].detail` 写给用户看的一句话（如「基础建模能力的新版本正在小范围试用，管理员推广后就可以一起更新。」），「暂时不能更新」弹窗直接显示；`blocked_reason` 用于弹窗红条「暂时不能更新：<原因>」。

### F4-E 插件上传与审核（都是建议）

15. `ManifestSummary` 加可选的 `category`：knowledge 包分不出是行业包还是系统知识包。
16. PL-15 对已发布的上传加 `channel`、`rollout`、`auto_breaker`：后端记录里有，但响应没给。
17. AD-08 的申请项加 `project_name`：管理员要显示别人项目的名字，mock 暂放在 `payload.project_name`。
18. AD-09 / AD-10 加错误码 `request_not_found`（404）和 `request_state`（409，带 `details.state`），mock 已按此实现。

### F4-F 个人设置

19. A-10 的 `base_url`、`model` 传空串表示清掉已存的值，不传表示沿用（契约只写了不传的情况）。

### F4-G 系统设置

无。

## 2026-11-02 · B4c（扩展项、插件产物、拆可选包、配额、第一个行业包）

接口编号见 `api-v1.md`。没写的字段与契约一致。分支 `feat/onto-modeling-plugin-p2-packs`，ADR 0040～0044。

### 页面命令（S-20）命令名映射：前端 F3 → 后端（以后端为准，前端要改的标 ★）

官方包保留现有命令名（方案第 6 章第 7 项）。S-20 把 `args` 拼成 `命令 键=值 …`，各命令文档已写明怎么认这些参数。

| 页面 · 按钮 | 前端 F3 的 `command` | 后端命令（包） | `args` |
|---|---|---|---|
| 场景走查 · 批量验证 | `/ontology-trace-batch` | 同名（trace） | `{scope: "all"}` |
| 场景走查 · 补齐过期场景 | ★ `/ontology-trace-refresh` | `/ontology-trace-batch`（trace） | `{keys}`，场景编号形如 `<逻辑名>--<场景序号>` |
| 场景走查 · 交互走查、行内 ▶ | ★ `/ontology-trace-walk` | `/ontology-trace-interactive`（trace） | `{scenario}` |
| 场景走查 · 按场景补生成 | `/ontology-mock` | 同名（trace） | `{by: "scenario"}` |
| 知识沉淀 · 在对话里复核 | `/knowledge-review` | 同名（knowledge，本次新增） | `{project_entry, industry_entry}` |
| 代码生成 · 扫描 | `/ontology-codegen-scan` | 同名（codegen） | — |
| 代码生成 · 生成 / 修复 / 重新生成 | `/ontology-codegen` | 同名（codegen） | `{operations}` 或 `{script, mode}` |
| 代码生成 · 生成评估器 / 按模板 | `/ontology-solver-evaluator` | 同名（codegen） | `{solver_key[, mode: "template"]}` |
| 增量更新 · 开始 / 交给助手修 | `/ontology-update` | 同名（core） | `{reference}`；`{change_list_id, fix_scenarios}` 要等变更清单（B4b） |
| 增量更新、资料区 · 外部反馈 | `/ontology-evolution` | 同名（evo，缺省不启用） | `{feedback_id[, case_ids]}` |
| 资料区 · 把资料读完 | `/ontology-research` | 同名（core） | `{path, reading_mode: "full"}` |
| 采集与快照 · 分析影响 | `/ontology-evidence-impact` | 没有（系统逆向建模包在 P3） | 现在返回 `409 pack_not_enabled` |

另外，页面上的按钮也可以按 S-20 的 `action: {pack, page, action_id}` 发，官方页面的 `action_id`：scenarios 为 `batch`、`walk`、`refresh`、`mock`；knowledge 为 `record`、`review`、`nominate`；scripts 为 `scan`、`generate`、`evaluator`。新增的知识沉淀命令还有 `/knowledge-record`（去对话里沉淀）、`/knowledge-nominate`（提名进行业包）。

### 流程节点（FL-01）

1. **拆包后节点编号带包前缀**（详设 6.8）：`core.flowchart` → `diagram.flowchart`、`core.mock` → `trace.mock`、`core.trace` → `trace.trace`、`core.codegen` → `codegen.codegen`、`core.solver` → `codegen.solver`；新增按需节点 `evo.evolution`（反馈演进）、`migrate.reuse`（迁移复用）。没启用的包的节点不出现；`commands` 映射跟着改。代码生成挂在场景验证之后，没启用场景走查时挂在质量检查之后。
2. `/ontology` 的下一步建议只列本项目启用了的命令（组装时去掉指向没启用的包的命令）。

### 扩展项（PL-12）、包目录（PL-01）与新建项目（P-02）

3. `Extensions.nav[]`、`subpages[]` 加 `pack_label`；`disabled_pages[]` 为 `{id, pack, pack_label, title}`。页面 id 只认 `scenarios`、`knowledge`、`scripts`，子页只认 `sources`、`snapshots`、`schema`。`badge` 只在页面声明了 `badge: inbox` 时给，`count` 在待处理汇总（B4b）登记前恒为 0。
4. `extensions.updated.changed` 由注册表比较前后两份得出（B4a 第 5 条「一律给全部七类」在合并后改用 `registry.changed_parts`）。
5. **P-02**：`industry_packs`、`system_packs` 按名字启用，不存在或类别不对 `404 pack_not_found`；`capabilities: {包名: bool}` 没写到的取清单 `default_on`（场景走查、知识沉淀、代码生成与求解、图示），包仓库里没有的名字忽略（前端目录可能比本部署多）；三项都没传时按缺省能力包。行业包不算「缺省启用」，向导里选了才写进锁文件。`capabilities` 不是对象、`industry_packs` 不是列表返回 `400 invalid_argument`。
6. **PL-01**：官方行业包 `industry-retail`（label「零售连锁」，`category: industry`，`default_on: true`——向导里默认勾选）；可选能力包 `default_on` 见上；`badges: ["recommended"]` 只有场景走查。
7. **P-05**：`research_mode=web` 在项目启用联网调研包（`websearch`）后才能设，否则仍是 `409 pack_not_enabled`。
8. **锁文件 `lock_version` = 2**：存量项目（版本 1）在 core 升到拆分后的版本（≥0.2.0）时一次性补上全部拆出去的包（都启用），之后用户停用的不再补回。这一步是一次锁文件提交（`Onto-Source: plugin`），主线版本 +1。

### 插件产物（AR-*）

9. `ArtifactType.format` 多 `md-sections`（一个二级标题一项），字段 `type` 多 `list`；`ArtifactType` 另带 `readonly`（只读原因，可写的为 null）。`ArtifactRow` 加 `path`（工作区相对路径）。
10. **官方产物沿用已有目录，不在 `ext/<包名>/` 下**：`trace.scenario` 是 `ontology/testcase/<逻辑>.md` 里的每个「## 场景 N」（键 `<逻辑名>--<序号>`，推演结论在 `ext/trace/index/scenarios.jsonl`）；`trace.mock` 是 `ontology/data-mock/*.md`；`diagram.flowchart` 是 `ontology/diagrams/*.drawio`；`codegen.scan_report` 是 `reports/*-codegen-candidates.md`；`codegen.solver_contract` 是 `ontology/solver/*/binding.yaml`；`core.knowledge` 是 `ontology/knowledge/*.md`。F3 第 8 条写的 `ext/trace/scenarios/{key}.md` 作废；新包的产物仍只能在 `ext/<包名>/` 下。
11. 只读类型：`codegen.scan_report`（`reason=report`）、`codegen.solver_contract`（`managed`，在求解页编辑）、`core.knowledge`（`knowledge_api`，走 K-*）；AR-05/06/07 返回 `403 artifact_readonly`。
12. MCP `artifact_list` 一页最多 50 行、不带分段正文；`artifact_get` 分段每段最多 4000 字、表格最多 20 行，流程图只给节点与连线摘要，超了 `truncated=true`。

### 知识（K-*）

13. K-01 的行业层条目 `layer="industry"`、`readonly=true`、带 `pack`；`source.kind` 为 `pack`。`layers[]` 的行业层另带 `packs`（行业包名列表）。行业条目的 `knowledge_id` 是 `<包名>:<文件名去扩展名>`（如 `industry-retail:domain-rule-safety-stock-formula`），不会和项目条目重名；`source.label` 是「包显示名 版本」。
14. K-04/K-05 对行业条目返回 `403 artifact_readonly`，`details.reason="industry"`。K-03/K-04 响应的 `conflicts[]` 是 `{kind, with, note}`，来自确定性查重。
15. K-06 `conflicts[].kind` 取 `duplicate`（同名重复）、`scope_conflict`（覆盖了口径不同的行业条目）、`broken_ref`（适用范围引用了不存在的条目）；POST 立即跑一次，GET 给最近一次（没跑过 `checked_at` 为 null）。
16. K-07/K-08 要同时启用知识沉淀包与 `industry_pack` 指定的行业包，否则 `409 pack_not_enabled`（`details.pack`）。K-07 响应 `{checks: [{item, found, action}], sanitized_text, highlights, pack: {name, label}}`，`item` 取 `secret_or_pii`、`customer_name`、`internal_id`、`amount`；K-08 响应 `201 {nomination_id, state: "pending_review"}`。

### 配额（SY-15、SY-16）

17. SY-15 GET 也允许 ops 读（写只有 admin）。`quota_exceeded` 只在发消息（S-07）、发命令（S-20）前检查（P-02、上传、引擎启动不查）；`details.scope` 是 `user|project`。配额自身出错一律放行。`snapshot_git_mb` 能设但在证据快照（P3）前不检查。
18. `budget_exceeds_quota` 的 `details` 另带 `quota`（卡住的那项配额）、`requested`、`remaining`。

### 求解（SV-01、SV-03）

19. B2 第 6 条的三处空值已修：`option_labels`、`result_title`、`result_texts` 从模板参数还原（都空为 null）；`problem_families[].label` 是界面名（如「候选方案二选一」），另带 `aliases`；`evaluator.kind` 按评估器源码判断 `declarative|script`。SV-03 的 `evaluator` 是 `{kind, constraints}`，草稿找不到评估器时为 null。

### 脚本页（SC-01～SC-03）

20. SC-01 的 `ScriptRow` 为 `{script: EntryRef, operation: EntryRef|null, contract: {status, findings}, descriptor: "synced"|"stale"|"none", sandbox: {status, duration_ms, ran_at}, actions: ("fix"|"regenerate_descriptor"|"regenerate")[]}`；`candidates.scanned_at` 取扫描报告的文件时间。沙箱验证只有 `passed|not_run`：`script_verify` 只留通过的记录（源码哈希对不上算没验证），`duration_ms` 为 null；SC-02 的 `cases[]` 取通过记录里的用例（`args` 是用例名、`output` 是期望）。SC-03 `python` 取沙箱报告的版本。

## 2026-11-02 · B4 合并（B4c 合进交付分支后的衔接与审查修复）

接口编号见 `api-v1.md`。ADR-0045。

1. **EP-05 `workspace.changed.artifacts`** 有值了：网页、agent、插件的主线提交都给改到的插件产物（ArtifactRef，最多 200 个）。
2. **EP-07 `extensions.updated.changed`** 由扩展项注册表比较得出，只含变了的类；进程里还没有比较基准的项目第一次仍给全部七类。
3. **EP-08 `assembly.updated`** 每次锁文件提交一条（待生效）、每次切换装配一条（已生效）；只动纯数据包（H0）时两条紧挨着发。B4c 分支里插件运行时自己再发的那份去掉。
4. **EP-06 `artifacts.updated`、EP-23 `knowledge.updated`** 进项目流（契约第 4 章信封）。网页新建、保存、删除产物只发一条（created / updated / deleted）。
5. **S-20 `action`** 按锁文件里启用的包换成命令；包没启用 `409 pack_not_enabled`（`details.pack`），启用了但装配待生效 `409 pack_not_enabled`（`details.command`、`effective="next_message"`）。
6. **K-08** 新增 `422 nomination_not_sanitized`：服务端按 K-07 同一套规则重查 `sanitized_text`，还有命中项时拒收，`details.checks` 为 `[{item, found, action}]`（只列有命中的类别，不回显原文）。
7. **S-14 重试、S-16 续跑、ST-08 提交条目（转助手复核那一路）** 也可能回 `429 quota_exceeded`（配额关口在会话池，所有发起一轮的入口都查）。
8. **K-03** 新建条目的标题与某条行业条目相同时，条目 ID 沿用行业条目的 slug（`<类型>-<行业 slug>`），`overrides_industry=true`。
9. **PL-05** `rollback_to` 只给换上后本项目还能装配的版本，一个都没有时为 null、`actions` 不含 `rollback`。
10. **PL-01** `publisher.kind` 只有镜像内置的包是 `official`；上传的包清单里写了 official 也给 `tenant`（`label` 为空）。
11. **AR-05 流程图**：标准编辑器与高级编辑器保存都只改第一页里的节点、连线与流程元数据，其他单元格与其他页保留；第一页是压缩格式时写回成明文。

## 2026-11-02 · B5a（包的界面、证据源只留接口）

接口编号见 `api-v1.md`。没写的字段与契约一致。分支 `feat/onto-modeling-plugin-p3-ext`，ADR 0055、0056。

### 包的界面（ADR-0055，修订详设决策 17 与 5.9.1、5.9.5）

1. **1.1 加「界面接口版本」`UI_API = "1.0"`**：上传包 iframe 编辑器与平台之间 postMessage 协议的版本，与页面接口 `page_api` 并列。
   编辑器在清单里声明需要的范围 `ui_api`（如 `">=1.0 <2.0"`），不满足时平台不打开它（退回专用组件或通用详情）。
2. **1.2 免鉴权前缀加 `/api/v1/pack-ui/`**（PL-21：路径里的短期票据即授权）。
3. **新增 PL-20 打开上传包编辑器** `POST /api/v1/projects/{id}/packs/{name}/ui-sessions` · 所有者 · 审计：写请求通用审计。
   - 请求：`{editor: string}`（清单 `contributes.editors[].id`）。
   - 响应 `201`：`{url, expires_at, ui_api: "1.0", pack: {name, version, label}, editor: {id, title}}`；`url` 形如
     `/api/v1/pack-ui/<票据>/<editor>/index.html`，前端直接作 iframe 的 `src`。
   - 错误：`pack_not_enabled`（409，`details.pack`，本项目没启用或已停用）、`not_found`（404，没有这个编辑器或缺入口文件）、
     `invalid_argument`（400，`details.mode="bundled"`：官方包的编辑器随平台加载，不用申请）、`page_api_unsupported`
     （409，`details: {required, platform}`，编辑器要的界面接口版本平台不满足）。
4. **新增 PL-21 上传包界面的静态资源** `GET /api/v1/pack-ui/{ticket}/{path}` · 免登录（票据即授权） · 审计：否。
   - 只发包仓库里这个版本 `ui/` 下的文件，后缀限静态资源（同静态检查 `ui` 项的白名单）；票据 2 小时有效、存在后端进程内存，
     重启后作废。每次读都按项目锁文件重查：包停用、换了版本回 `404 pack_not_enabled`；票据不对、过期、路径越界回 `404 not_found`。
   - 响应头：`Content-Security-Policy: sandbox allow-scripts; default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline';
     img-src 'self' data: blob:; font-src 'self'; connect-src 'none'; form-action 'none'; base-uri 'none'; frame-ancestors 'self'`，
     `X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer`、`Cache-Control: no-store`。
5. **窄接口协议（新增一节，界面接口 1.0）**：iframe `sandbox="allow-scripts"`（不给 `allow-same-origin`），只经 postMessage 和平台通信。
   - 请求 `{onto_ui: "1.0", id: string(≤64), method, params: object}`；回应 `{onto_ui, id, ok: true, result}` 或
     `{onto_ui, id, ok: false, error: {code, message, status?, details?}}`；平台主动推 `{onto_ui, event, data}`，
     `event` 取 `artifact.changed`（`data: {type, key}`，助手或别处改了这份产物）、`readonly.changed`（`data: {readonly}`）。
   - 方法：`context.get` → `{ui_api, pack: {name, version, label}, editor: {id, title}, artifact: ArtifactRef, readonly}`；
     `artifact.read {type?, key?}` → `Artifact`（AR-03）；`artifact.save {type?, key?, base_revision, fields?, sections?, content?, summary?}`
     → AR-05 的响应；`chat.command {command, args?, text?}` → S-20 的响应（`origin.page="artifact"`）；`theme.get` →
     `{mode: "light", vars: {"--ink": …}}`（只给设计系统的颜色、圆角、字体变量）。`type`、`key` 缺省是打开的那份。
   - 限制：读写只限本包登记的产物类型（类型前缀等于包名）；命令只能是 `/<包名>-…`；`args` 只能是简单键值；保存只认上面的字段。
   - 拒绝时的 `error.code`：`invalid_message`（没有 id）、`ui_api_unsupported`（主版本不同）、`method_unknown`、`forbidden`
     （越权的类型、命令）、`invalid_argument`；接口出错原样带回 `code`、`message`、`status`、`details`（如写入范围锁的
     `423 write_scope_locked`、`409 revision_conflict`）。不是窄接口的消息（没有 `onto_ui` 字段）忽略不回。
6. **清单 `contributes.editors: [{id, title, ui_api?}]`**：`id` 小写字母开头，字母、数字、连字符，包内唯一。入口按约定：
   官方包（入库来源 builtin）`ui/<id>/index.jsx`（随平台编译），上传包 `ui/<id>/index.html`（iframe）。
7. **视图声明**：`views/<类型>.yaml` 的 `detail.editor: <编辑器 id>`；`card` 认 `title`、`subtitle`（字段名）、`chips`、`fields`
   （各最多 4 个字段名）与 `component`（平台卡片组件）。
8. **`ArtifactType.view`（AR-01）**：`card: {title, chips, subtitle?, fields?, component?}`（不认得的键去掉）；`detail` 加
   `editor?: {id, title, pack, version, mode: "bundled" | "iframe", ui_api, entry}`（引用的编辑器包里没声明时没有这个键）。
   旧版图示包的 `detail.component: "flowchart"` 保留兼容，前端映射到同一个编辑器。
9. **`ManifestSummary.contributes` 加 `editors: string[]`**（编辑器 id）。
10. **PL-15 静态检查**：`frontend` 项改为「页面只选用平台内置，前端代码只在 ui/ 下」（`ui/` 之外的前端代码仍判失败）；
    新增 `ui` 项「包自带界面：上传包只能是静态资源、经隔离 iframe 加载」——编辑器入口要在、视图引用的编辑器要声明、
    `ui_api` 要满足；上传包 `ui/` 只能是 html、htm、js、mjs、css、json、map、txt、png、jpg、jpeg、gif、svg、webp、ico、
    woff、woff2、ttf，拒绝要编译的源码、`package.json`、`node_modules`、点文件与服务端可执行内容；有 `ui/` 没有编辑器给警告。
    凭据扫描加上 js、mjs、jsx、css。
11. **4.7 新增 `card_type: artifact`（通用产物卡）**：一轮结束时宿主给每个新增或修改的插件产物出一张（一轮最多 8 张，新增在前；
    一个文件多项的格式只出真变了的那几项；删除的不出卡）。特有字段：
    `{artifact: ArtifactRef, change: "add"|"mod", component: string|null, chips: [{field, label, value, text, tone}],
    fields: [{field, label, text}], detail: {kind: "generic", layout} | {kind: "component", component} | {kind: "editor", editor},
    locked: bool}`；共有字段 `eyebrow` 是「<类型标题> · 本轮新增|本轮修改」，`plugin` 是 `{name, label}`，
    `open` 是 `{kind: "dialog", target: "artifact", ref: ArtifactRef}`，`status` 为 null（状态在 chips 里）。
    `component: "flowchart"` 时另带 `flowchart` 卡的 `state`、`summary`、`thumbnail`（节点最多 40 个，数据对象不画）。
    `card_type: flowchart` 保留兼容，前端两种都认。
12. **图示包 0.6.0**：流程图编辑器在 `ui/flowchart/`；视图 `detail.editor: flowchart`、`card.component: flowchart`。

### 证据源只留接口（ADR-0056）

13. **2.8 加错误码 `evidence_not_available`（501）**，`details: {api: "EV-14"}`（接口编号）。
14. **3.19 全部接口（EV-01～EV-30，含 EV-11 的 GET/PUT、EV-17 的 cancel/resume/retry、EV-29 的 GET/POST）与 PL-04、PL-19
    一律回 `501 evidence_not_available`**，不校验权限、不读请求体。EV-29 的 `file` 锚点同样 501（见 gaps B5a）。
15. **SY-15 响应加 `not_enforced: string[]`**：本期不生效的配额项（`collections_per_source_per_hour`、`snapshot_git_mb`），能存、不检查；
    前端不显示这两项。
16. **`upload_prompt` 卡**：`actions` 不含 `connect_system`、`sources_available` 为 0（服务端现在不出这张卡，以后出时照此）。
17. **AB-01/AB-02**：策略里的生产库采集授权与 `platform_limits.allow_production_collection` 保留，本期不生效（B5b 实现时照此）。
18. **M-08**：工具目录没有 `evidence_*`；P-14 的项目级只读服务器不列 evidence。

## 2026-11-02 · B4b（变更集、确认生效与撤回、写入范围只读、节点状态、统一下一步、待处理）

接口编号见 `api-v1.md`。没写的字段与契约一致。主线 2fee09d8..0a009a5c，ADR-0046～0049。

### 变更集（CS-01～CS-07、EP-04、4.7 round_changes、S-05）

1. **Changeset**：`source` 现在只有 `assistant`（自主构建 `autobuild` 在 P3）；`build_mode` 助手为 `L2`；`purpose` 是
   「助手执行「<命令业务名>」的修改」或「助手的修改」；`state` 的 `open` 表示助手还在跑、`pending_confirm` 表示这一轮结束、
   改动等你确认。Studio、MCP、导入、插件变更「接口上不可见」，不出现在 CS-01。
2. **CS-02** `groups[].kind` 另有 `other`（不是条目、也不是登记过的插件产物的文件，`target` 是
   `{type: "file", key: 路径, title: 文件名}`，与 B4a 第 25 条同一口径）；`checks` 是相对变更集基准**新增**的问题
   （一轮结束时在副本的树上跑规则检查算的）；`items[].detail` 是实体级差异的一句话（「改了「对象属性清单」：新增 1 行」）。
3. **CS-03** `target` 认 `EntryId`、`<artifact_type>/<key>` 或文件路径；`sections[].title` 为空串表示整份文本（不是条目的文件）。
4. **CS-04** 另有 `503 service_unavailable`（`details.dependency="gate"`）：门禁脚本跑不起来时不合入。`gate_failed` 的
   `findings` 只列新增的错误。确认生效后对话里这个变更集的全部卡片发 `card.updated`（`state=merged`），再加一条
   `notice`（`kind=merged`、`text`「这一轮的修改已确认生效 · 版本 v0.n」）。撤回同理（`kind=discarded`）。
5. **CS-05** 撤回时副本里没提交的文件一并丢掉。**CS-07** `choices[].target` 认冲突里给的 `entities[].id`（条目 ID；
   不是条目的文件是路径），没选全仍是 `409 merge_conflict`。
6. **round_changes 卡**：一轮有改动就放一张新卡，`card_id` 为 `rc-<变更集>-<序号>`，同一变更集之前的卡跟着发
   `card.updated`（内容都是变更集的全部改动，按钮作用于同一个变更集）；`status.key` 取 `pending` / `confirmed` /
   `skipped`（撤回）；`subtitle` 是变更集的 `purpose`；`remaining_failures` 是新增错误数；增量更新这一轮带
   `change_list_id`。旧历史里的 `workspace_changes` 记录仍按 B4a 的口径投影成 `state=merged`。
7. **EP-05**：助手在副本里的提交不再发 `workspace.changed`（主线没变），确认生效时由写入事务发一条
   （`source=assistant`、带 `changeset_id`）。
8. **S-05 `open_changeset`**：这条对话里（或不属于任何对话的）没确认的变更集。

### 写入范围（CS-06、EP-03、2.4 write_scope_locked）

9. **ScopeLock**：`paths` 是 fnmatch 模式，含正在执行与执行过的节点声明的 `produces`、`writes`（`**` 写成 `*`），也含
   变更集改动的文件路径（`[`、`]`、`*`、`?` 转义成字面量）；`slice` 取执行记录的分段（分段规划前为 null）；
   `node` 只在助手在跑时给；`run_id` 在 P3 前为 null。助手在跑但没执行有声明的节点、也还没改动时，`state=assistant_running`
   且范围为空。
10. **CS-06 `?target=`** 认 `EntryId` 与 `<artifact_type>/<key>`；锁住时给整份锁，没锁给空锁。
11. **V-06 回退**：有没确认的助手变更集、或助手正在跑时一律 `423 write_scope_locked`（整树恢复会碰到范围里的条目）。
12. **MCP `PROJECT_EDITING_BUSY`** 的 `details`（与 issue）另带 `entities`（范围里的条目与插件产物）、`scope_state`、
    `changeset_id`；「助手在跑就整项目忙」「条目有 Studio 租约」两条取消，「条目有你没提交的 Studio 草稿」保留。
13. **ST-07 存草稿不再占 30 分钟租约**（旧接口 `studio/lease` 早已 410）；ST-02 节点、ST-03 行的 `locked`，ST-04、F-02 的
    `lock`，SV-01 的 `lock`（范围里有求解契约时）按写入范围锁给。
14. **节点声明多一个可选字段 `writes`**：本节点运行期间还会改、但不归它产出的路径（修复型节点、跨类改动），与 `produces`
    合起来是写入范围；组装时只查 `produces` 不重叠。`.onto/run/active.json`（state_helper 写）为
    `{commands: {命令: {node, allow, phase, since}}}`。

### 流程（FL-01～FL-05、EP-01）

15. **FlowNode.state** 按 8 种状态给（P1 的四种作废），`flags.review_source` 取 `upgrade` / `import`（`autobuild` 在 P3）；
    `display_state`：已确认 + 需复核为 `needs_review`、自主构建的为 `auto`。`actions` 里过期节点第一项 `kind=refresh`，
    可选节点多一项 `skip` 或 `restore`（`target` 省略）。
16. **progress（x/N）** 仍只数主线上满足完成判定的节点（已确认与过期都算做过，待确认不算）；P-01/P-04 同口径。
17. **notices** 现在只有 `pending_changeset`（`action` 是 `show_changes`）。`slices`、`slice_stages` 在分段规划（建模蓝图）
    前为空。
18. **Suggestions**：`mode=pending_confirm` 时主推 `{type: "merge_changeset"}`「确认生效」，次选是主线下一步与
    `{type: "discard_changeset"}`「撤回这一轮」；`waiting_decision` 时主推是禁用的「先做上面的决定」
    （`action` 为 `{type: "navigate", page: "wb-l2"}`）；过期刷新的 `id` 为 `refresh:<节点>`、优先级 2。
    `state_helper.py suggest` 输出同一结构（JSON）。
19. **FL-03** 只有可选节点能跳过（必经节点 `node_not_skippable`）；`reason` 截 200 字。**FL-05** 用节点的
    `entry.command`（没有时取第一个斜杠命令动作），`slice_id` 作为命令参数 `slice=` 带上，`origin` 缺省
    `{page: "flow", label: 节点标题}`；没有对话入口（面板节点）`400 invalid_argument`。

### 待处理（IN-01～IN-04、EP-02）与变更点清单（INC-01～INC-05）

20. **IN-01** `autobuild_summary` 恒为 null（P3）；`today.eta_minutes` 按每项 2 分钟估；`pack` 只有插件节点的过期 /
    需复核、插件产物的格式迁移、外部反馈（`evo`）有值。
21. **InboxItem**：续跑项 `source` 在服务重启中断时为 `restart`；需复核项的 `run` 在 P3 前为 null；`impact` 只有修改待确认
    （「涉及」）与决策（「改动预览」）给。决策、修改待确认、合入冲突、变更清单 `batchable=false`。
22. **IN-02 `detail`**：`review` 为 `{source, at, note, node: {id, title}, options: [{id: "accept"|"rejudge", label}]}`；
    `report` 为 `{path, pending}`；`format_migration` 为 `{type, key, from, to}`；`changeset_conflict` 为
    `{changeset: Changeset, entities}`；`stale` 的 `upstream_diff.lines` 为空（只给说明）。
23. **IN-03**：`keep_stale` 等同忽略；`rejudge` 重做这一步（FL-05）；`send_to_assistant` 以 `/ontology-validate` 发出、
    带上报告路径；`migrate` 在包提供迁移命令之前 `409 pack_not_enabled`；`revert_slice`、`use_generic`、`arbitrate` 在
    P3 的事项出现之前不会出现在 `actions` 里。
24. **IN-04** `refresh_stale` 一次只刷新最上游的那一个（同一项目同一时刻一条命令），其余进 `skipped`；
    `accept_high_confidence` 在 P3 前恒为 0。
25. **ChangePoint** 由助手的影响分析条目映射：`condition` 取理由、`value` 取变更说明、`scope` 取文件、`anchor` 为空、
    `confidence` 按改动类型（修改 high、新建 medium、删除 low）；`impact` 是这条与需要跟着改的级联条目。
    **ChangeList** 多 `conversation_id`（F3 第 15 条）；`impact_summary.scenarios` 与 **Impact** 的 `regression` 在场景索引
    接上之前为 0 / 空。**INC-04** 响应的 `changeset_id` 是当前没确认的变更集（还没开始改时为 null）；之前的变更集还开着时
    发命令照常（同一对话里接着改）。

### 其他

26. **O-35** 多一项 `changeset_copies`（助手副本：清残留锁、补登记、重建、补链接），只在修过或修不了时出现。
27. **O-36** 备份范围加 `changeset-state`、`flow-state`、`change-lists`（助手副本按分支重建，不备）。

### B5a 审查修复（同日，修订上面几条）

19. **PL-20 响应加 `commands: string[]`**：本包清单 `contributes.commands` 里的命令（带 `/`）。窄接口 `chat.command` 只认这些，不再按
    「/包名-」前缀判断（第 5 条的限制作废）。PL-20 新错误 `429 rate_limited`（`details.limit`）：同一用户同时有效的票据最多 50 张、全平台 5000 张，
    满了拒绝新申请，不淘汰别人的；同一个人重开同一个编辑器沿用原票据、顺延有效期。
20. **窄接口握手改走 MessagePort**（第 5 条修订）：iframe 先往父窗口发 `{onto_ui: "1.0", connect: true}`，平台回
    `{onto_ui, event: "connected", data: {ui_api}}` 并转交一个端口；之后的请求、回应、推送都走端口，窗口级的请求不理。每个 iframe 只握手一次；
    iframe 第二次 load（导航离开）时平台拆掉桥接、卸掉 iframe。
21. **`chat.command` 的参数**：`args` 最多 10 个键（`^[A-Za-z_][A-Za-z0-9_-]{0,39}$`），值是不超过 200 字、不带换行与控制字符的文字，或数字、布尔；
    `text` 不超过 500 字；同一个编辑器 5 秒最多一条（`rate_limited`）。
22. **S-20 `origin.pack`**：包界面发来的命令，前端在 `origin` 里带包名；服务端据此再校验——包要启用中（否则 `409 pack_not_enabled`），命令要是
    本包清单里的、不带别的参数文字（否则 `403 forbidden`，`details.reason="not_pack_command"`），参数限制同第 21 条（否则 `400 invalid_argument`，
    `details.fields`），同一项目同一包 3 秒一条（否则 `429 rate_limited`，`details.retry_after_s`）。
23. **PL-21 响应头加 `Allow-CSP-From: *`**；平台的 iframe 带 `csp` 属性（来源写平台完整地址的那份 CSP）。
24. **PL-15**：新增 `views` 检查项「视图声明格式正确」（每份视图能解析、是映射、`artifact` 是本包登记的类型、三段是映射、各键是字段名或字段名列表、
    `detail.layout` 合法）；`names` 检查加上保留包名（`ontology`、`onto`、`kernel`、`platform`；租户另不能用 `core`）和与包仓库里别的包（任何版本）的
    命令重名；检查本身出错时上传记为 `failed`，检查项 `internal`。
25. **`card_type: artifact` 去掉 `locked`**（第 11 条修订）：卡片在对话历史里，只读与否由前端按实时的写入范围（CS-06）判断；
    `flowchart` 卡的 `locked` 前端也不再看。
26. **AR-01 的视图**：运行时形状不对的段、键丢掉回到缺省，坏的产物登记只跳过它自己，一个包读不出来不影响别的包。

## 2026-11-02 · B5 合并（B5a 合进交付分支后的衔接）

1. **4.7 `card_type: artifact`**（B5a 第 11 条补充）：助手在变更集副本里改时，产物卡在这一轮的 `round_changes` 卡之后出；
   「这一轮的改动」是这一轮开始时副本的树到结束时的树（含没提交的文件，不含上一轮改的、跟上主线时并进副本的网页保存），
   卡片内容按副本里的文件；`card_id` 的种子是这一轮 `round_changes` 卡的编号。确认生效前双击打开仍读主线（AR-03，见 gaps「B5 合并」第 1 条）。
2. **窄接口 `context.get` 的 `readonly` 与 `readonly.changed`**（B5a 第 5 条补充）：按写入范围锁判断——锁里有这份产物，或产物所在文件
   落在锁的 `paths` 里（助手正在跑的节点声明的范围也算），和 AR-05 回 `423 write_scope_locked` 同一口径；锁随 EP-03 `scope.updated` 变。

## 2026-11-02 · B4b 审查修复

1. **CS-04 / CS-07**：合成结果和主线一样（冲突全选「保留主线」、两边改成一样）时不写提交，响应 `commit` 为 null，
   `version_label` 是当前主线版本；Changeset `merged.commit` 同样为 null。
2. **INC-04**：清单已确认（已发出执行）或已执行时 `409 changeset_closed`（`details.state`）。清单随所属变更集：确认生效后
   `state=executed`，撤回后回到 `editing`、`changeset_id` 为 null；只做了影响分析的一轮也会收成清单（`changeset_id` 先为 null）。
3. **IN-01**：全部通过的外部反馈批次不列；`review` 事项只有自主构建的段带 `revert_slice`；忽略过的事项在来源变了之后重新出现。
4. **O-35 `changeset_copies`** 的 `detail` 另有「补记了 n 个已合入的变更集」（崩溃后启动核对）。

## 2026-11-02 · B5c（节点包、扩展项补齐、插件运维登记点）

接口编号见 `api-v1.md`。没写的字段与契约一致。ADR 0065～0068。

### 插件与节点包

1. **PL-01**：节点包（清单 `kind: node`）的 `category` 一律是 `capability`（清单写 `category: node` 也一样），`counts.node` 恒为 0。用户 11-02 定：节点包是能力包的特例，不单列分类。
2. **PL-06～PL-09** 响应多 `changeset_id: string|null`：这次切换记成的插件变更集（已合入，`source="plugin"`）；助手的修改还没确认、这次切换又不改流程节点时不记变更集，为 null。
3. **PL-06、PL-07** 的 `write_scope_locked`（423，details 是 `ScopeLock`）只在「助手的变更集没关闭」且「这次切换会改流程节点（flow.lock 的节点变了）」时出现。
4. **PL-06、PL-07** 包里有外部节点、需要的证据源提供方不可用（本期恒不可用）时回 `409 pack_unavailable`，`details: {reason: "not_installed", providers: string[]}`；**PL-03** 的 `depends` 检查同时不通过，`detail` 写缺哪个提供方。
5. **PL-03** `impact.needs_review_after_upgrade` 给真实值：换成这一版后完成判定签名变了的已确认节点个数（与节点状态同一判据）。
6. **PL-10** 撤销的是第一次启用时（锁文件里原来没有这个包），`pack` 给一项 `status="disabled"`、`actions=["enable"]` 的 ProjectPack（锁文件里已经没有它）。
7. **PL-12** 的 `entries` 只有 `quick_action`、`extra_action`，`command` 一定有且是本包的命令；`source_wizard` 随证据源留空不出现。`gates[]` 多 `on: ("save"|"commit"|"publish")[]`。`config[].key` 写成 `<包名>.<key>`（与配置注册表同一个键）。新增 `view_choices: [{id: "er_view"|"schema_browser"|"evidence_sidebar", pack}]`，依赖证据源的两种本期不出现。`extensions.updated.changed` 可能含 `view_choices`。
8. **PL-15** 检查项：`kind: node` 的包多 `node_pack`；声明了 entries / gates / view_choices / config / metrics / alerts / mcp_tools 的包多 `extensions`。

### 流程

9. **FL-01**：
   - `flags.backfill`、`flags.disabled` 给真实值。可补做的节点不计入 `progress.total`（`counts_in_progress=false`），P-01 的 x/N 同一口径；
   - 停用了的包的节点也列在 `nodes` 里：`state="skipped"`、`display_state="disabled"`、`actions=[]`、`counts_in_progress=false`，没有连线；
   - 所属包版本被熔断（O-29）的节点 `state="skipped"`、`actions=[]`，它的 `blocks` 不生效；
   - 节点多 `hooks: [{id, title, when: "before"|"after", command, pack}]`（节点钩子），`actions[]` 里钩子那一项带 `hook: <钩子编号>`；
   - `notices` 多一种 `{kind: "assembly_pending", count, text: "插件变更下一条消息生效", action: {type: "navigate", page: "proj-settings"}}`：锁文件改了、装配还没切换时给。
10. **FL-05** 按节点类型：
    - script：同步跑完回 `200 {node, flow_hash, run: {state: "passed", output, commit, version_label, duration_ms, runtime: "jiuwenbox"}}`；错误 `node_run_failed`（422，新错误码，`details: {exit_code, timed_out, stderr}`）、`service_unavailable`（503，`dependency: "sandbox_gateway"`，不在本机回退）、`payload_too_large`（413）、`upstream_unavailable`（502）；
    - human：回 `200 {node, flow_hash, question: Question}`，对话里出一张 `question.asked`（kind `decision`，不带「其他，我来说」）；同一节点有没回答的卡时沿用它；
    - external：`409 pack_unavailable`（`reason: "not_installed"`、`providers`）；
    - 节点所属包版本被熔断：`409 pack_unavailable`（`reason: "breaker"`）。
11. **S-11** 也回答人工节点的决策卡：结论写进节点声明的记录文件；没选或选项不存在回 `answer_invalid`，回答过再答回 410 `question_gone`。

### 写入路径与发布

12. 所有写入（网页、Studio、MCP、变更集合入）可能回 `422 gate_failed`，`details.findings: Finding[]`：包声明的门禁 `on` 含 `commit` 时在写入事务提交前查。回退、撤回、插件变更、导入不查。
13. **PB-01** `gates[]` 在平台三道之后接包声明的门禁（`on` 含 `publish`）：`id` 为 `<包名>.<门禁>`，`source: {kind: "pack", label, pack}`，`level: warning` 的门禁 `status="warn"`、不阻断。

### 运维

14. **O-25** 已实现：每个包的 stable、beta、项目在用的、禁用的版本各一行，带 F4 第 3 条的 `rollback_to`；禁用的版本 `channel="retired"`、`rollout_pct=0`（F4 第 4 条）。`error_rate_pct` 恒为 null。
15. **O-26** 只做 `by: "project"`，只能调 beta 通道上的版本（调 stable 的回 `400 invalid_argument`）；`by: "tenant"` 回 **`501 rollout_not_available`**（新错误码）。跟随 stable 的项目按项目编号稳定分桶，落在比例里或在 `targets` 里就跟 beta。
16. **O-27** 响应是 `to_version` 那一项；core 的 stable 同 O-32 先备份包仓库。
17. **O-28** `reason` 必填（空的回 400）；core 的默认版本不能禁用（`409 pack_required`）；禁用后通道指着它时改取最新的可用版本。「已锁定它的项目进待处理」未做。
18. **O-42** 多 `{category: "lockfile", component: "flow.lock", version: "1", note: "组装后的流程节点"}`。
19. **O-08～O-10** 已实现：运维新建的规则 `source="kernel"`；包声明的规则 `id` 为 `<包名>.<规则>`、`source` 为 `<包名>@<版本>`，只能 `PUT {enabled}`，改别的字段或删除回 `403 forbidden`。规则本期只登记不评估：`state` 恒为 `ok`，`firing` 恒为 0。

### MCP

20. 读工具集多出本项目生效中的包声明的只读工具，名字 `<包名>_<工具名>`（连字符换成下划线），输入同 `artifact_list`（list）或 `{key}`（get），类型固定为声明的那一种。

## 2026-11-02 · B5c 合并（B5c 合进交付分支后的衔接）

1. **PL-06～PL-09 `changeset_id`**（修订 B5c 第 2 条）：切换写了提交就有值，助手的修改没确认时也一样（插件变更集不占
   「没关闭的变更集」位置）；只有锁文件没变、没写提交时为 null。会改流程节点的切换在助手变更集没关闭时仍回 423（B5c 第 3 条不变）。
   这期间记下的插件变更集要等助手那组修改确认或撤回后才能撤回，PL-10 在这之前按生效中的版本改回。
2. **IN-01 `decision`** 多一个来源：人工节点（FL-05）出的、没回答的决策卡。`source="plugin"`，`pack` 是节点所属的包（算进
   插件页角标），`meta`「n 个选项」，`batchable=false`，`actions=["answer","dismiss"]`；IN-02 `detail` 就是那张 `Question`；
   IN-03 `answer` 与 S-11 同一个退路（要带 `option_id`，`custom_text` 记成补充说明），回答后这一项消失。包停用了、节点不在当前
   流程里的卡不列。FL-05 出卡后推 `inbox.updated`。
3. **IN-01 `stale`、`review`** 不列所属包版本被熔断（O-29）的节点（流程里算跳过、FL-05 回 409）；IN-04 `refresh_stale` 也就不会选到它。

## 2026-11-02 · B4d（建模蓝图、分段、按详设 6.8 重排主流程）

接口编号见 `api-v1.md`。没写的字段与契约一致。分支 `feat/onto-modeling-plugin-p2-blueprint`，ADR-0050～0052。

### 建模蓝图（BP-01～BP-03、4.7 卡片）

1. **Blueprint** 多 `mode`（`full` | `sliced`：没排除的段多于一段才是按段）；`research.report_id` 恒为 null（资料分析报告在
   `research/`，RP-02 只读 `reports/`）；`evidence_hint` 恒为 null（证据源只留接口）；`revision` 是文件内容 sha256 前 16 位。
   `goal_anchor_label` 见 F2 第 20 条。`order_label` 由位置算（第一段「推荐先做」、依赖多段的最后一段「建议最后做」、其余
   「第 n 个」、只有一段或排除的为空）；`scale` 是这一段规划的对象数、规则数。
2. **BP-02**：
   - 新段与合并出来的段（前端给临时编号）一律换成 `s<n>`，响应里是新编号；别的段对它和对被合并段的依赖跟着换。
   - `slice_invalid.details.reason` 取 `empty`、`missing`（原有的段不在列表里也没被合并：排除要用 `excluded`）、`duplicate_id`、
     `duplicate_name`、`name_required`、`name_too_long`、`dependency_order`、`dependency_excluded`、`dependency_cycle`（依赖自己）、
     `unknown_dependency`、`merge_too_few`、`merge_unknown`、`merge_invalid`（被合并的段还留在列表里、或给已有的段加 merged_from），
     另带 `slice_id`。
   - `mode` 缺失或不对、`term_choices`（不存在的术语、空的叫法）、`answers`（不存在的问题或选项）回 `400 invalid_argument`
     （`details.fields`）。`base_revision` 必填，空串表示不检查。
   - 蓝图还在没确认的变更集里（资料分析那一轮）时写进副本，是这一轮修改的一部分；只有助手在跑时 `423 write_scope_locked`。
     蓝图在主线上时经写入事务提交，范围锁照常（资料分析的变更集没确认时 `research/*` 在范围里，也是 423）。
   - 保存后对话里登记过的蓝图卡全部 `card.updated`，流程重算。
3. **BP-03**：
   - 请求多可选 `conversation_id`（L2 发命令的对话，缺省当前对话）。`next` 缺省取 P-13 的构建方式。
   - 蓝图还在没确认的变更集里时，先把这一轮确认生效（同 CS-04），再在主线上标已确认：因此另有 CS-04 的错误
     `422 gate_failed`、`409 merge_conflict`、`409 changeset_in_progress`、`503 service_unavailable`。已确认的再调不重复写。
   - `next=L2`：往对话发 `/ontology-extract-object slice=<第一段>`（全量模式不带分段），`command_task` 同 S-20 的 `task`。
   - `next=L4`：自主构建（B5b）登记开始前预览之前回 **`409 autobuild_not_available`（新错误码）**，在改任何东西之前回，蓝图不动。
4. **卡片**：一轮写出或改了蓝图时，在「这一轮改了什么」之后出：
   - `research_summary`（这一轮也改了资料分析报告时）：`files` 是验收回执里读过的资料数，`coverage_pct` 是其中读得出文字的比例，
     `verified_anchors` 是核对过的出处数，`problems_carried` 是人工待决与错误项数；`report_id` 恒为 null、`open` 为 null；
   - `blueprint`：`subtitle` 为空串，`card_id` 为 `<这一轮的卡片种子>-blueprint`；
   - `build_choice`：**只在自主构建可用时出**（只能一段一段来时，确认蓝图就是一段一段来，不出这张卡）。
   - 「这一轮改了什么」里 `research/blueprint.json` 的 `target.title` 是「建模蓝图」。
5. **待处理**：蓝图里没回答的问题是 `decision` 项，`id` 为 `decision:bq-<问题>`，`detail` 是 Question 的形状（`question_id`
   为 `bq-<问题>`、`allow_custom: true`）；IN-03 `answer` 写回蓝图（同 BP-02 只带 answers），错误为 `400 answer_invalid`。

### 流程（FL-01～FL-05、EP-01、P-01）

6. **节点**（core 0.3.0，详设 6.8）：`core.research`、`core.model`（建模，`per_slice: true`）、`core.metric`（指标，旁支）、
   `core.validate`（校验；启用场景走查是「校验与走查」）、`core.consistency`（全局一致性）、`core.publish`（发布）、`core.update`；
   场景走查 0.10.0 只有 `trace.trace`（全量回归，串在校验之后，`placement: step`），`trace.mock` 去掉。`core.object`、
   `core.operation`、`core.rule_metric` 不再出现。`commands` 里 `extract-object`、`extract-operation` 归 `core.model`，
   `extract-rule-metric` 归 `core.metric`（带分段的命令按步骤归建模）。
7. **`slice_stages`**：`[{id, label, plugin?}]`，core 的是 object 对象、operation 操作、rule 规则、check 写入即查，场景走查加
   `{id: "scenario", label: "场景", plugin: "trace"}`。
8. **`slices[]`**：`{id, name, core_flow, group, stages, note, no_rules}`。没有蓝图或只有一段时是一段 `{id: "all", name: "全部"}`
   （全量模式）。`stages` 取 NodeState 与 `draft`（这一段没确认的修改有新错误，只出现在 check）、`auto`；`group` 取 now（在跑或
   做了一部分）/ wait（待确认、过期）/ done / todo；`note` 只写「正在做「X」」「待确认」「检查没通过」「n 步过期」「停在「X」」或空串
   （空串时前端显示 core_flow）。`no_rules`：按段模式下这一段没规划规则，规则一步为 skipped。
9. **`core.model`**：`progress` 只在按段模式给「n/m 段」；状态由各段汇总（全部段确认 → confirmed；任一段过期 → stale；在跑 →
   running；待确认 → pending_confirm；做了一部分 → running）。按段模式下进度 x/N（含 P-01、P-04）里建模要全部段做完才算。
10. **`core.research`**：蓝图是草案时 `state=pending_confirm`，`actions` 第一项 `{kind: "confirm", label: "去确认", target: "wb-blueprint"}`。
11. **`core.publish`**：`kind: human`，`actions` 是 `{kind: "start", label: "去发布", target: "publish"}`（target 是页面 id）；完成判定是
   有 `release/*` 标签，发布之后改了内容显示过期。按段建模时 `core.consistency` 挡住发布（`state=blocked`）。
12. **FL-03 / FL-04**：对 `core.model` 带 `slice_id` 时跳过 / 恢复这一段的可选步骤，请求多可选 `step`（缺省 `rule`）；不是可选
   步骤 `409 node_not_skippable`。不带 `slice_id` 仍按节点（`core.model` 不能跳过）。
13. **FL-05**：对 `core.model`：请求多可选 `step`；带 `slice_id` 取这一段还没做完的第一个必经步骤的命令；都不带取主线下一步
   （过期的先刷新），命令带 `slice=`（全量模式不带）。
14. **Suggestions**：
   - 蓝图草案：主推 `{id: "confirm_blueprint", action: {type: "confirm_blueprint", next}}`「确认蓝图」，次选「调整分段」
     （`navigate` wb-blueprint、`params.dialog=blueprint`）、「重新划分」（`send` `/ontology-research phase=blueprint`）；蓝图就在这一轮的
     修改里时 `mode=pending_confirm`，次选换成「撤回这一轮」，不单列「确认生效」。
   - 建模的下一步：`id` 为 `core.model:<段>:<步骤>`，`action` 为 `{type: "send", text: "", command, args: {slice}}`（全量模式没有 args），
     `label` 是「提取对象 / 提取操作 / 提取规则 / 场景走查」。建模过期时 `refresh:core.model` 的 `label` 是「刷新「<段名>」」、动作是过期的那一步。
   - 没有蓝图、资料分析做完：次选多一个 `plan_slices`「分段规划」（`send` `/ontology-research phase=blueprint`）。
15. **P-01 `ProjectCard.slices`**：按段模式时 `{total, confirmed, cells}`，每段一格：done → `confirmed`、now → `running`、wait →
   `pending`（有过期步骤时 `stale`）、todo → `ready`；全量模式为 null。
16. **ExecRecord.slice** 现在有值：建模命令带 `slice=` 时是那一段（不存在或已排除时 null），不带时是这一步还没做完的第一段，
   全量模式是 `{id: "all", name: "全部"}`；`command_label` 取这一步的业务名（对象提取、操作提取、规则提取、场景走查）。
17. **Changeset.slice_id**：陪伴建模下是这个变更集做的第一段。

### 写入范围（CS-06、EP-03、2.4）

18. **ScopeLock**：建模节点在按段模式下 `paths` 是这几段包含的条目路径（转义成字面量；操作含 `ontology/logic/` 同名路径，
   另加这几段操作的场景文件 `ontology/testcase/<操作>.md`），
   `entries` 含这几段规划的条目（还没建的也在里面），不再是整类模式；`slice` 给 `{id, name}`（全量模式为 null），助手在跑时
   `reason_text` 是「助手正在改「X」这一段，确认生效或撤回之前这些内容只读」。全量模式仍按节点声明的整类。

### 发布（PB-01、PB-03）

19. **PB-01 `gates`** 在平台门禁（与 B5c 的包门禁）之后加流程关卡：`blocks` 指向发布节点的节点（按段建模时的全局一致性、包的
   关卡节点）：`{id: "flow:<节点>", title: 节点名, source: {kind: "flow", label: "主流程"|包名, pack?}, status: pass|fail（过期）|
   not_run, summary: ""|"过期"|"还没运行", blocking, degradable: false, findings: [], action: {type: "start_node", node_id}|null}`。
   PB-03 同样按它挡（`422 gate_blocked`）。

### 命令

20. 新命令 `/ontology-consistency`（core）；`/ontology-research` 认 `phase=blueprint`（`phase=slices` 同义）只做分段规划；
   `/ontology-extract-object`、`/ontology-extract-operation`、`/ontology-extract-rule-metric`、`/ontology-trace` 认 `slice=<段>`。

## 2026-11-02 · B5c 审查修复

1. **FL-05** 脚本节点：输出路径或输入路径不在节点声明的范围里（含 `..`、受保护目录、`.git`）回 `403 path_forbidden`（`details.path`）；脚本输出超过执行器上限回 `422 node_run_failed`，`details: {truncated: true, limit_chars}`，不写产出。人工节点的记录路径同样，出卡时就回 403。
2. **所有写入**：路径落在 `.git/` 下的回 `403 path_forbidden`（写入事务兜底）。
3. **S-11** 回答人工节点的卡：项目已归档回 `409 project_archived`。
4. **MCP** 包声明的只读工具名改成 `<包名>__<工具名>`（原 B5c 第 20 条的 `<包名>_<工具名>` 作废）；**/metrics** 包指标全名改成 `onto_pack_<包名，连字符换下划线>__<指标名>`，每个指标名只出一组 HELP、TYPE。
5. **PL-15** 的 `extensions` 检查：工具名、指标名里不许写 `__`，和内置工具、包仓库里别的包重名时不通过；`write_scope` 检查对所有类型的包核对脚本输出、决策记录在不在写入范围里；清单的 `contributes.nodes` 只能写节点编号或包内相对路径（`manifest` 检查）。
6. **O-25** `rollback_to` 取通道历史里上一个还在、没禁用的版本；**O-28** 禁用 stable 指着的版本后，跟随通道的项目退回这个通道上一个可用版本（不取只在 beta 上的预发布）。**O-27**、O-31/O-32、发布到 stable：stable 追上或超过 beta、或往回退时，beta 的 `rollout_pct` 归零、名单清空。

## 2026-11-02 · UI 文案（前端不再显示的下发字段）

分支 `feat/onto-modeling-plugin-ui-copy`（起点 0c5c0eaa）。用户定的口径 ⑤：后端下发的说明文字契约里不再下发；前端已经不显示，后端在 I1 去掉。
证据源相关的数据（口径「证据源本期不开放」）前端已按 `state/evidence.js` 统一过滤，后端同样不该下发，清单在 gaps「UI 文案」第 2 条。
下面列的是「前端已不显示、I1 停止下发」的字段；没列的字段与契约一致。

### 对话流卡片（契约 4.7）与工作台

1. 卡片 `subtitle`：`upload_prompt`、`material_map`、`blueprint`、`build_choice` 四种卡，以及增量更新那一轮（带 `change_list_id`）的 `round_changes` 卡。
2. `run_report` 卡的 `note`；`round_changes` 卡的 `lock_note`（锁定说明由前端按写入范围算，`roundLockNote`）。
3. **R3**：`build_choice` 卡前端不再单独画，「确认，一路建完…」并进蓝图卡底部。建议蓝图卡带 `build_options: ("L2"|"L4")[]`（自主构建登记了才有 L4），之后不再发 `build_choice` 卡；前端现在两种都认（有 `build_choice` 卡就当 L4 可选）。
4. AB-05 运行的 `scope_lock_text`；增量影响面卡的 `semantic_scope_text`。
5. FL-01 `nodes[].note`（建模步骤抽屉不再显示节点说明，含带状态的那几句）。

### 待处理、Studio、报告

6. IN-01 / IN-02 事项的 `meta`（如「4 个选项 · 推荐 A」、人工节点卡的「n 个选项」）。
7. RP-01 `items[].type_label`：列表不显示，只在前端不认识这个类型时给「这份报告」卡的「检查类型」兜底；RP-02 `progress.note`。
8. ST-06 `snapshot_basis`（证据页签顶部「以及某数据源最近一次采集」，证据源相关）。

### 资料、发布、版本、插件页

9. R-01 `purposes[]` 里「由助手判断」的 `description`；`files[].is_template`（不再显示「资源模板」）。
10. PB-01 `gates[].source.label`（门禁卡的来源行）。
11. PB-04 发布记录的 `tag`、V-01 发布标签的 `release/…`、PB-06 包清单的 `source_commit`、`flow_hash`、`sha256` 不再显示（manifest.yaml 原文里仍有）；「源版本」现在取 PB-04 的 `version_label`，建议 PB-06 加 `source_version_label`。
12. K-01 `loading_rule`、`layers[].overridden`（R9 分层条去掉）；K-06 `conflicts[].note`。
13. AR-02（trace.mock）`skipped[].reason`。

### 项目设置、插件中心

14. AB-01 `boundary_text`（自主构建边界说明）。
15. M-01 `recommended_order`、`limits`（对外 MCP 的连接顺序与协议细节整块删了）。
16. PL-01 `items[].meta`（插件卡元信息，如「22 命令 · 18 子代理」）；`items[].ui.note` 新建向导不再显示（只给带页面的标「带页面：xx」），插件中心、插件详情只作界面标记的悬停。
17. PL-02 `flow_insertions[].note`（插入点下的小字）、`flow_insertions[].short`（只给删掉的统计卡用）。

### 运维中心

18. O-02 `cards[].note`（如「变更集在 P2」「证据源在 P3」）。`merges` 卡在没有计数来源之前带 `unavailable: true` 或不返回（前端两种都不画）；`collections` 卡本期不返回。
19. O-03 `reruns`、`first_accept_pct` 没有数据来源时可以不返回（整列为空前端不出这一列）。

### 前端新认的字段（建议 I1 补）

20. FL-01 `nodes[]` 里 `kind="script"` 的加 `last_run: {state: "passed"|"failed", summary: string, at: string} | null`（R24：建模步骤抽屉展开脚本节点看最近一次结果；没有时只用本次会话里 FL-05 的同步返回，刷新后就没了）。
21. AR-01 `view.detail.editor` 加包的显示名 `pack_label`（iframe 编辑器头部的插件标记悬停「由「xx」插件提供」，现在只能显示包名）。
22. PL-12 `entries[].where`：`quick_action` 写 `"composer"`（输入框「全部命令」旁，R22），`extra_action` 写页面 id（`scenarios`、`knowledge`、`scripts`，放在该插件页页头）。`view_choices` 里有 `er_view` 时 Studio 视图切换多「ER 视图」（R23，页面 id `studio-er`，前端按条目画，每个对象一次 ST-04；对象多时可以加批量接口，非必须）。

## 2026-11-02 · B4d 合并（B4d 合进交付分支后的衔接）

merge 4e6ed10f，衔接 ee03c022。修订 B4d 节的几条：

1. **`build_choice` 卡不再发**（设计调整 R3，用户已批准）：`blueprint` 卡多 `confirm_options: [{mode: "L2", label: "确认，一段一段来"},
   {mode: "L4", label: "确认，一路建完…"}]`，草案时给，L4 只在自主构建（B5b）登记后出现，已确认时为空；按钮调 BP-03 `next=mode`。
   契约 4.7 的 `build_choice` 作废，B4d 第 4 条里「`build_choice` 只在自主构建可用时出」一并作废。
2. **FL-01 多 `blueprint: {state: "none" | "draft" | "confirmed"}`**：没有蓝图（`none`）时，建模步骤抽屉的「调整分段」改为用 S-20 发
   `/ontology-research phase=blueprint`（按钮文字不变）。
3. **FL-05 对 `core.publish`**（在发布页里做的节点）回 `400 invalid_argument`，`details` 为 `{fields: {node_id: "no_entry"}, page: "publish"}`；
   B5c 的人工节点（声明了 `decision` 的）照常出决策卡。
4. **`trace.trace` 的 `placement` 是 `spine`**（B4d 第 6 条写的是 step）：随 core 拆出、本来就在主线上，不算新插进来的可补做节点（B5c 口径）。
5. **IN-01 的 `decision`** 同时含会话里的决策卡、人工节点的卡（`source: plugin`，B5c 合并）与蓝图的待答问题（`decision:bq-<问题>`，`source: assistant`）。
6. **PB-01 `gates`** 顺序：平台门禁 → 包声明的门禁（`source.kind=pack`，B5c）→ 流程关卡（`source.kind=flow`，B4d 第 19 条），`order` 连续。
7. **`hooks` 的宿主节点**认 core 的 `aliases`（挂在 `core.object` 上的钩子挂到 `core.model`）；`after` 与 `before` 映射到同一节点时去掉 `before`。

## 2026-11-02 · B5b（自主构建：运行器、会话池、预算、决策台账与复核）

分支 `feat/onto-modeling-plugin-p3-autobuild`，ADR-0060～0063。接口编号见 `api-v1.md`，没写的字段与契约一致。

### 策略、平台限制、单价（AB-01、AB-02、SY-17、SY-18）

1. **AB-01** 不带 `boundary_text`（UI 文案第 14 条）；多 `not_enforced: ["production_collection"]`（生产库授权原样存取、本期不生效）；
   `quota_remaining.tokens` 不限时为 null，`quota_remaining.cost.amount` 恒为 null（配额没有费用维度）；`last_run.duration_ms` 是跑着的时间。
2. **AB-02**：给了哪组改哪组（`scope`、`budget`、`advanced`、`production_collection`），组内给了哪项改哪项；整份策略再校验：
   `400 invalid_argument`（`details.fields`：`scope.mode`、`scope.slice_ids`（`unknown`/`required`）、`scope.until_node`（`unknown`，发布、资料分析、
   增量更新不能作终点）、`budget.*`（`positive`）、`advanced.confidence_threshold`（`out_of_range`，0.5～1.0）、`advanced.below_threshold`、
   `advanced.max_fix_rounds`（1～3）、`advanced.must_suspend_kinds` / `source_priority`（`list`，各 ≤20 项、每项 ≤60 字））；
   `422 budget_exceeds_quota {dimension: duration|tokens|cost, max}`，token 的 `max` 是平台上限与剩余配额的较小者；
   `revision_conflict` 的 `details` 是 `{target: "autobuild_policy", expected, actual}`。`production_collection` 不对照授权，
   不回 `source_not_authorized`（证据源只留接口）。
3. **SY-17** 多 `not_enforced: ["allow_production_collection", "night_priority"]`；`max_cost.currency` 跟 SY-18 的币种；
   `max_concurrent_runs` 取 1～16，**不允许 0**（主控定）：收到 0 或负数回 `422 invalid_argument`（`details: {fields: {max_concurrent_runs: "min"}, min: 1}`），
   超过 16 仍是 `400 invalid_argument`；环境变量 `ONTO_WEB_AUTOBUILD_MAX_RUNS=0` 报配置错误（启动日志告警、SY-14 问题清单）并回落；ops 能读。同时运行数也是配置项 `ONTO_WEB_AUTOBUILD_MAX_RUNS`（SY-14 可见）。
4. **SY-18** 默认 `{currency: "USD", items: []}`；PUT 校验币种（USD|CNY）、引擎、模型（≤120 字、同一引擎不重复）、单价（≥0，
   `cache_read_per_mtok` 可为 null），不合法 `400 invalid_argument`（`fields: {"items.<n>": ...}`）；`model="*"` 是这个引擎的兜底单价；ops 能读。

### 运行（AB-03～AB-09、AB-18）

5. **AB-03**：不带 `production`（UI 文案 gaps 第 2 条）；`slices[].current` 取「已确认 / 未开始 / 做了一部分 / 待确认 / 有过期」；
   范围里没有要做的段 `400 invalid_argument {fields: {scope: "empty"}}`；`warnings` 现在只有「这一轮的修改还没确认生效或撤回」；
   `queue_position` = 排队中的运行数 + 1。蓝图没有或是草案 `409 blueprint_not_confirmed`。
6. **AB-04**：多错误 **`409 changeset_in_progress {changeset_id, conversation_id}`**（有没确认的助手修改时不能开始）；
   预览只认开始前 10 分钟内、本项目的（否则 `410 preview_expired`；服务重启后预览失效）。新对话 `kind: "autobuild"`、`run_id`、
   标题「自主构建 · 运行 #n」，`run_id` 为 `run-<n>`。
7. **AB-05**：`scenarios` 恒为 `{passed: 0, total: 0}`（场景推演通过数没有数据来源）；`scope_lock_text` 恒为空串（UI 文案第 4 条）；
   `tail_text` 为「跑完全部分段后：校验 → 全量回归 → 全局一致性」这样（按这次要做的几步，发布不在里面）；`matrix` 含运行前就做完的段
   （`result` 为「运行前已完成」），`cells` 取 `confirmed|auto|running|queued|skipped`（没生效的部分完成为 `skipped`），`result` 取
   「进行中 / 排队 / 完成 / 部分完成 / 完成 · 挂起 n 项 / 检查没通过 / 未开始 / 已回退到运行前 · v0.x」；`current.step_label` 是步骤的动作名
   （「提取操作」）；多 `base_version_label`（F2 第 10 条）。
8. **AB-06～AB-09**：暂停在当前这一步做完后生效；停在安全点、转为陪伴在这一段生效后生效；还在排队、或已经停在安全点
   （暂停在两段之间）时直接结束；从暂停直接停在安全点、转为陪伴会先把这一段做完（返回 `stopping` / `converting`）；
   续跑返回 `queued` 或 `running`（有名额才开始）。排队中的运行也可以暂停（直接 `paused`）。
9. **AB-18**：`state=running` 取没结束、还在跑的（`queued`、`running`、`pausing`、`stopping`、`converting`），其余取值按状态精确匹配。
10. **EP-15**：预算用完时 `next` 为 `stopped`；**EP-17** 在运行报告进 git、对话里的报告卡放好之后才发。

### 台账、挂起、报告、撤回（AB-10～AB-17）

11. **AB-10**：`below_threshold` 取 `true|false`；撤回了的段上的决定不列；`LedgerEntry` 带 `summary`（F2 第 13 条）、`reason_text`
    取「达到阈值，采用推荐选项」或「低于阈值，采用保守选项并记为假设」；`options` 里采用的那一项 `recommended: true`。
12. **AB-12**：202；运行没结束时改判排到结束后（`command_task.state=queued`），之后往运行对话发「这一段这一步的命令 + 改判说明」；
    任务标题「改判：<summary>」。
13. **AB-15**：`action` 只认 `use_generic`、`defer_to_next_run`；回答后往运行对话发这一段这一步的命令补上（任务标题「补上：<问题>」）；
    有运行没结束时任务 `queued`，排到它结束后发（同 AB-12）；门禁没过挂起的那一段（`title`「「段名」检查没通过」、`options: []`）同样可以回答。
    AB-12 在有别的运行没结束时同样排队。
14. **AB-16**：运行没结束 `409 autobuild_running`；`note` 恒为空串（UI 文案第 2 条）；`slices` 只含分段（不含分段之后的几步），
    `scenarios` 恒为 0/0，多 `reverted`（F2 第 10 条）与 `base_version_label`。
15. **AB-17**：`409 slice_not_revertible` 的 `reason` 取 `not_in_run`、`already_reverted`、`not_merged`、`later_changes`（另带 `entities`）；
    有任何运行没结束时 `409 autobuild_running`（不只本次运行）。撤回时蓝图不跟着撤。

### 会话、对话、写入范围、流程

16. **S-01 / EP-21 Conversation** 的 `kind`、`run_id` 有值；**S-05** `autobuild_run_id` 是这条对话所属的运行（不论是否结束）。
17. **S-07**：运行对话里发消息回 202，`delivery: "deferred"`、`state: "running"`、`queue_position: null`（不交给引擎，下一步开始前带给助手）；
    别的对话回 `409 autobuild_running {run_id}`。**S-14、S-16、S-20**（含经 S-20 的 FL-05、BP-03 `next=L2`、RP-05/06、IN-03 的发命令动作）
    在运行没结束时一律 409 `autobuild_running`；**S-08、S-09、S-04** 对运行对话 409 `autobuild_running`。运行没结束包括暂停、中断。
18. **运行器发的步骤消息**不进对话流（S-06、S-13 都没有对应的 `message.user`）；运行对话里只有开始卡、分段进展块、报告卡、
    提示行、建议条与助手自己的输出。**EC-31** `permission.resolved.by=autobuild_policy`（工具确认由策略放行）；自动决定不出 `question.asked`。
19. **ScopeLock**：自主构建这一段的变更集没生效之前 `state="autobuild_slice"`、`run_id` 有值，`reason_text` 为「自主构建正在做「段名」这一段」
    （分段之后的几步为「自主构建正在做这一步」）。
20. **CS-04、CS-05** 对运行中那一段的变更集回 `409 autobuild_running`。
21. **FL-01 / FL-02 / EC-53**：运行在跑（含排队、正在去安全点）时 `mode="autobuild_controls"`，主推「暂停」、次选「在安全点停下」「转为陪伴」；
    暂停、中断时 `primary: null`、`secondary` 只有「运行详情」（`{type: "autobuild", op: "details"}`）；运行对话里结束时的建议条主推「去复核」
    （`navigate` inbox `filter=autobuild_review`），其余按流程。FL-01 没有单独的 `autobuild` 字段（契约 3.12 的说明由 `suggestions` 承担）。
22. **ExecRecord.run_id** 对自主构建的步骤有值；这些执行重启时不放续跑卡、待处理不列「续跑」。

### 待处理（IN-01～IN-04）

23. **IN-01**：自主构建复核按台账出——低于阈值的一条一项（`id` 为 `review:<条目>`，`title` 是 `summary`，`high_confidence: false`，
    运行结束且这一段生效过时 `actions` 含 `revert_slice`）；同一次运行里高置信、还没复核的合成一项（F2 第 12 条，`actions` 只有 `accept`）；
    挂起的问题一个一项（`suspension:<问题>`；读不出的资料只有 `use_generic`）。节点级的「需复核（自主构建）」不再出（节点升级、导入的照旧）。
    运行中那一段的变更集不列「修改待确认」。`autobuild_summary` 带 `high_confidence_pending`（F2 第 14 条），`run_id` 参数可以指定哪一次。
24. **IN-02**：`review`（台账条目）的 `detail` 是 LedgerEntry 加 `base_version_label`、`slice_version_label`；合并项是
    `{group: "high_confidence", run_id, threshold, entries}`；`suspension` 是 Suspension。
25. **IN-03**：台账条目 `accept` = AB-11、`rejudge` = AB-12（结果 `{entry, command_task}`）、`revert_slice` = AB-17；合并项 `accept` = AB-13；
    挂起 `answer`（只认 `option_id`、`custom_text`）、`use_generic` = AB-15。**IN-04** `accept_high_confidence` 可带 `run_id`（不存在 `404 run_not_found`），
    不带时接受全部运行的高置信决定。

### 项目、运维、可观测

26. **P-01 ProjectCard**：没结束的运行，或结束了还有要复核的那次，给 `autobuild`；`state` 按卡片的 6 个取值归并（`stopping`、`converting`
    → `running`，`interrupted` → `paused`，`budget_exhausted`、`failed` → `stopped`，`converted` → `completed`）；`status` 加 `autobuild`（没结束）或
    `autobuild_done`（结束了、还有要复核的）；没结束时 `landing_page` 为 `l4-run`；`counts.autobuild` 有值。
27. **P-06、P-07（归档）、P-12、V-06** 在运行没结束时 `409 autobuild_running`；**P-13** GET 的 `autobuild_running`、`run_id` 有值。
28. **O-01** `stats.autobuild = {running, limit}`、`autobuild_runs`（排队与在跑的）、`autobuild_today`（UTC 当天结束的）有值；
    **O-15** `items[].autobuild` 有值；**O-21** `autobuild=pause_at_safe_point` 生效；**O-35** 停机时有运行在跑才多一项 `autobuild_runs`。
29. **/metrics**：`onto_autobuild_runs_total{state}`、`onto_autobuild_run_seconds`、`onto_autobuild_cost_total{currency}`、
    `onto_autobuild_decisions_total`、`onto_autobuild_reviews_total{outcome}`，即时值 `onto_autobuild_running`、`onto_autobuild_rejudge_ratio`。
30. **EU-01** 推 `notification`（`autobuild_finished`、`autobuild_suspension`、`autobuild_stopped`），不落盘（A-13 没做）。
31. **V-01** 自主构建生效的段 `run = {run_id, run_no, slice_id, slice_name}`（F3 第 19 条）、`needs_review` 是这一段还没复核的决定数；
    报告那一次提交（`autobuild: 自主构建运行 #n 报告`）`run` 为 null。
32. **ST-03** `needs_review`、**ST-04** `needs_review: {source: "autobuild", run_id}`：还标着「需复核」的段生效时改到的条目。

## 2026-11-02 · I1（前后端联调）

分支 `feat/onto-modeling-plugin-i1`（起点 c76c236d）。接口编号见 `api-v1.md`，没写的字段与契约一致。核对方法与结果见 `i1-checklist.md` 第 1 节。

### 契约增补

1. **C-01 `features.evidence: bool`**：证据源本期为 false。前端据此不再调 EV-01 探测（501 会在浏览器控制台留错误），`evidenceSources` 直接按 `501 evidence_not_available` 失败，调用处照旧走失败分支；后端不给这个键时照常请求。
2. **4.2 `message.assistant.anchors`**：助手正文末尾的出处定义行 `[^n]: <锚点>`（锚点带 scheme 或以 `resource/` 开头）由宿主收成 `[{n, anchor}]` 并从 `text_md` 里去掉，正文里的 `[^n]` 留给前端画角标；不是锚点的脚注原样保留。工作区约定（`INTERACTION_CONVENTIONS.md` 呈现约定）加一行教助手这么写。
3. **EP-19 `release.updated`**：PB-03 发布后、评测服务经 PB-07 拉取后各发一条（`{release: ReleaseRecord}`）。原先不发。
4. **EC-53 `suggestions.updated`**：CS-04 确认生效、CS-05 撤回（含 CS-07、待处理里的确认）之后，对这个变更集所在的对话再发一条（与 FL-02 同一份）。原先只在一轮结束与打断后发，确认后建议条停在「确认生效 / 撤回这一轮」。
5. **AR-03 查询参数 `changeset`**（变更集 id）：这个变更集还没关闭（开着、待确认、合入冲突）且副本里有这份产物时读副本里这一轮的版本（新增的也读得到）；其余情况读当前版本。响应多 `from_changeset: string|null`（读的是哪个变更集）。`card_type: artifact`、`flowchart` 卡（以及同处广播的蓝图卡）带 `changeset_id`（B5 合并第 1 条）。
6. **ST-07 PUT `form`**（按契约补实现）：服务端以当前文件为底，只改表单里变了的部分（名称、描述、属性表、数据来源、关联、章节），没动的部分逐字节保留；GET 草稿、ST-04 的 `draft.form` 带回存下的表单（原文方式存的草稿仍为 null）。新错误（都是 `400 invalid_argument`）：改 key `details.fields.key="rename_unsupported"`；「# 文档名 + ## 章节」写法的对象改数据来源 `data_source="unsupported"`；表达不了的关联（如关联脚本）`relations="unsupported"`；字段类型不对 `<字段>="invalid"`。表单缺的键按当前版本补。
7. **V-05 `packs_lock_changes[]`** 为 `{pack, label, from, to}`：`label` 是能力的显示名；`from` 是当前锁定版本、`to` 是目标版本，没启用的一侧为 null。
8. **PL-02 `flow_insertions[]`** 为 `{id, title, at?}`：`title` 取包的流程声明里的节点标题，`at` 按 `after` / `before` 指向的 core 节点算段（research / model / validate / consistency / publish，指标归 model）。`note`、`short`、`kind`、`flow_note` 不下发（UI 文案第 17 条）。
9. **O-11 每项多 `required: bool`**（不能关，如本地日志文件）；内置项（`implementation="builtin"`）地址与信号固定。只列本部署真有的 `prometheus`、`file`，OTel 与外部平台不出现。**O-12** `effective` 是 `"immediate"`（契约写 `rolling_restart`；现在改完立即生效），关 `file` 回 `400`（`details.fields.file="required"`）。
10. **流程声明的动作可带 `pack`**：要某个包才能做的动作（如 core 资料分析的「联网资料分析」要联网调研包 `websearch`），这次装配里没有这个包时组装时去掉（FL-01 节点动作、FL-02 建议同时少这一项）。
11. **元模型（core `onto_metamodel.py`）对象章节**加「数据来源」「查询边界」两节（可选）：条目模板本来就有，Studio 保存时不再算模板外章节。

### 资料、出处、报告（R-*、EV-29、ST-06、RP-*）

12. **EV-29** 不再整体 501：`file:` 锚点（及不带 scheme 的 `resource/…#片段`）按上传资料解析，返回前后各一段原文与高亮区间；其余 scheme 每项 `resolved=false`。`AnchorResolution.reason` 多三个取值：`not_found`（片段定位不到）、`unreadable`（抽取器读不了这种格式）、`not_available`（业务系统锚点本期不开放）。
13. **R-06**：pdf、doc 等 core 抽取器读不了的格式返回空块（不回 `preview_unavailable`）；`coverage.read_sections` 恒为空串。md / txt 的 `Material.preview` 是 `text`（契约本有，前端补认）。
14. **R-01**：`PurposeOption` 不再带 `description`；`Material.coverage.label` 取「已读」「未读」；`feedback_group.count` 按真实数量。
15. **R-07**：`read_pct` 只有 0 与 100，`status` 只有 `done` / `queued`；`position`、`queue_reason`、`blocked` 恒为 null，`counts.terms` 恒为 0；资源模板（图示说明定义等）不进资料地图。「读过」= 资料分析报告引用过它，资料分析验收通过之后才算读全。
16. **R-08**：对象、规则取资料分析报告的候选段，术语取蓝图里已统一的叫法，出处取业务项清单里同名的那一行。项目流 `materials.candidate` 不发（没有实时来源）。
17. **R-10**：项目还没有蓝图时 `404 blueprint_not_found`；`ask_me` 与 `blueprint_question` 都生成同一个蓝图待答问题（因此都进待处理的「决策」，编号 `decision:bq-qN`），区别只在是否返回 `inbox_item_id`。
18. **R-11**：`summary` 写「失分用例 n 条」，`related` 恒为 null；`state=handled` 的筛选也包括已忽略的。处理状态存 `.onto/run/feedback-state.json`（不进 git）。待处理只列 `open` 的反馈；「交给反馈演进」同时把这批标成已处理。
19. **`materials.updated`**：资料或 `research/` 的改动生效后发，只带变了的条目；助手在变更集副本里跑的那一轮不发（确认生效后发）。
20. **ST-06**：只给条目正文里 `file:` 锚点的出处，属性表行带 `used_for`；`snapshot_basis` 恒为空列表。ST-04 属性的 `evidence` 仍为 null。
21. **报告编号**：`reports/x.md` → `x`，`research/x.md` → `research:x`，内核算的链接检查 → `link`（结果记 `.onto/run/link-report.json`）。RP-01 不列各目录的占位索引 `index.md`。`ReportItem.status` 只有 `pending` / `fixed`，`status_note` 为空；`Report` 的 `scope_text` 为空，`duration_ms`、`previous`（链接检查有）、`repair`、`arbitration` 为 null。RP-05、RP-06 走 S-20：rule、semantic、coverage 发 `/ontology-validate`，diagram 发 `/ontology-validate-diagrams`；RP-06 的 regression 只在命令里附「只查改动」。助手这一轮写的报告还没确认生效时从副本读。
22. **`research_summary` 卡** `report_id` 有值，`open` 是 `{kind: "page", target: "reports", ref: {report}}`；BP-01 `research.report_id` 有值（B4d 第 1、4 条里的「恒为 null」作废）。

### 版本、条目、个人、运维（V-*、ST-09/10、A-08/13/14、O-*）

23. **V-02**：和主线上一个版本比，条目与产物的认法同变更集明细；`review`、`run` 恒为 null（B5b 前）；`.onto/` 下的系统文件不列。**V-03** 缺省 `prefix=ontology/`，`download=1` 打成 zip（「项目名-版本-ontology.zip」）。**V-04** `to` 缺省 `WORKTREE`（这时带 `right_revision`），文本截 512 KB。**V-05** `files` 不含 `.onto/`，能力锁变化只在 `packs_lock_changes`。
24. **ST-09**：正文用项目锁定的 core 条目模板，走写入事务（`Onto-Source: studio`）；`key` 不填用名称，`key` 与名称不同时名称写进文档头 `report_name`；重名跨所有类型判（`409 revision_conflict`，`details.kind="name"`）；`flowchart` 走 AR-06，图示包没启用 `409 pack_not_enabled`（`details.pack="diagram"`）。**ST-10** 被别的条目引用时 `409 entry_referenced`（脚本不算），流程图走 AR-07。
25. **A-13 通知来源**：一轮跑完（`step_done`）、要回答或确认（`needs_input`）、会话出错（`session_error`）发给项目所有者，同一对话里没读的同类只留最新一条；PL-18 退回时通知上传者（自己退回自己的不通知）。`autobuild_*`、`nomination_reviewed`、`request_pending`、`evidence_conflict`、`publish_done` 还没有来源。
26. **O-38 / O-39**：从备份里只解出抽检的项目到临时目录，检查元数据、版本历史与 `git fsck`；O-36 `last_drill` 有值。**O-40** 记备份（手动、定时，定时的发起人 `system`）、包仓库备份、恢复演练三类，任务号带类型前缀；服务重启时还在跑的改判失败。**O-41** `cancel` 只对还在跑的演练，`retry` 对失败的备份或演练、开一个新任务并返回新任务，`resume` 一律 `409 task_state_invalid`。**O-43** 六项：结构化日志、备份、诊断包、通知可改；审计日志（365 天，只是声明）、回收站（不自动清，与 O-52 `retention_days=null` 一致）不可改；证据快照、会话历史不返回。备份范围加 `data/me/`、`data/ops/` 下的设置与记录。

### UI 文案的后端部分（contract-changes「UI 文案」第 1～19 条的落实）

27. 说明槽位是**删键**，不是给空值；卡片共有字段 `subtitle` 保留为空串。删掉的：round_changes（增量那一轮）`subtitle`、`lock_note`；INC-05 `semantic_scope_text`；FL-01 `nodes[].note`；IN-01 / IN-02 `meta`；RP-02 `progress.note`；R-01 `purposes[].description`；K-01 `loading_rule`；K-06、K-03 / K-04 `conflicts[].note`；M-01 `recommended_order`、`limits`；PL-01 `items[].meta`；O-02 `cards[].note`；O-03 `reruns`、`first_accept_pct`。
28. **数据字段保留**（口径 ⑤ 针对说明句；API 客户端与 agent 会用）：PB-04 `tag`、V-01 发布标签、PB-06 `source_commit` / `flow_hash` / `sha256`、R-01 `is_template`、RP-01 `type_label`、K-01 `layers[].overridden`、PB-01 `gates[].source.label`（来源名）、PL-01 `ui.note`（插件中心悬停在用）。AR-02 本来就不下发 `skipped`。
29. 知识冲突用数据代替说明句：同名冲突带 `duplicate_of: {id, title}`，失效引用带 `missing: string[]`；K-03 / K-04 的 `conflicts[]` 为 `{kind, with}`。
30. **O-02 `merges` 卡**给真实计数（每次确认生效记 `onto_merges_total`），不带 `unavailable`；`collections` 卡不返回。
31. **证据源数据不下发**：IN-01 计数没有 `evidence_conflict`（筛选参数照常接受）；PL-01 `counts` 没有 `provider`、目录里不列提供方包（`?category=provider` 返回空）；FL-01、O-25、O-42 不列外部节点与提供方包。FL-02、BP-01、P-10、P-14、M-01、PB-01 核对后本来就不下发。
32. **新字段**：FL-01 `kind=script` 节点 `last_run: {state: "passed"|"failed", summary, at}|null`（只记真跑起来的结果，存 `.onto/run/script-runs.json`）；AR-01 `view.detail.editor.pack_label`（产物卡里的编辑器引用同样带）；PL-12 `entries[].where`（快捷项 `"composer"`；附加操作取声明的插件页 id，没写或不是插件页时取本包第一个插件页，包没有插件页时不下发这个入口）；PB-06 `source_version_label`；SV-02 / SV-03 缺 `decision_model` 时先取 Decision Model 文档里的 `id`，没有才补 `<solver_key>_model`（文档 id 与绑定不一致会被拒，不能一律补默认值）。
33. **口径 ③**：项目内的错误提示、撤回通知（「这一轮的修改已撤回」）、待处理的「合入冲突」（改「冲突」）、建议条 reason（「主线下一步」改「下一步」，在 core `flow_state.py`）不再说「合入」「主线」「提交」；运维中心（O-02 合入卡、O-20、O-35）、包静态检查、MCP issue 按口径保留。

## 2026-11-02 · B5b 审查修复

交付分支 c7afa68d..0dab01f0，ADR-0060～0063「审查修复」一节。只列对外可见的变化，其余见 decisions-log 同名节。

1. **2.10 运行中拦截补全**：ST-08 转助手复核、S-12 取消运行对话的排队、S-17 放弃执行在有运行没结束时回 `409 autobuild_running {run_id}`；
   会话池里发起一轮的入口都按这个口径（只放行运行器自己发的步骤）。
2. **自主构建合入删除已确认内容**：内部 `merge(source="autobuild")` 回 `422 delete_forbidden {paths}`；运行器把这一段撤回、记一个挂起问题
   （`title`「「段名」要删除已确认的内容」、`options: []`、`handled_as`「这一段没生效，先做下一段」（分段之后的几步为「这一步没生效，接着做下一步」）），
   AB-05 `matrix[].result`、AB-16 `slices[].result_text` 为「要删除已确认的内容」。
3. **EC-31**：运行中删除类、打 `release/*` 标签的工具确认由策略拒绝（`permission.resolved` 的结果是拒绝，`by=autobuild_policy`），助手收到
   「自主构建不能删除内容，也不能打发布标签」。
4. **助手不能打发布标签**（陪伴建模同样）：守卫拒绝 `git tag|push|update-ref` 带 `release/`、`git push --tags`；发布只走 PB-*。
5. **并发会话配额**：运行会话不计入用户的并发会话（配额里数在线会话时也不数它），运行不会再因为并发会话配额以 `budget_exhausted` 结束。
6. **AB-06～AB-09**：运行正在收尾时回 `409 autobuild_not_running`；没有驱动任务的运行（进程里的任务没了）「停止」直接结束。
   AB-04 同一项目同时两次开始，只有一次成功，另一次 `409 autobuild_running`。
7. **AB-11、AB-12、AB-15** 新错误 **`409 slice_reverted {slice_id}`**：这一段已经撤回。排到运行结束后的改判、补上，发之前这一段撤回了就不发，
   页面命令任务记为 `cancelled`（`result.summary`「这一段已经撤回」）。
8. **AB-15 `defer_to_next_run`** 可以同时带 `option_id` 或 `custom_text`（不带也行）：回答记下，`Suspension.state="deferred"`，
   `command_task: null`；`deferred` 的问题 IN-01 照样列（`suspension:<问题>`，动作同 `open`），AB-15 还能再回答（改成现在补上）；
   下一次自主构建做到这一段时补做，做完变 `answered`。AB-03 预览里这样的段 `included: true`（范围是选定时要选了它）。
   AB-05 `suspended`、AB-16 `metrics.suspended` 含 `deferred`。
9. **AB-17**：撤回时蓝图里这一段这次新记进去的条目一起去掉（同一个版本），取代 B5b 第 15 条「撤回时蓝图不跟着撤」；这一段的格子回到
   运行前（原来过期的仍是过期）。上次撤回写完提交就出错的，再调一次返回那次撤回的 `commit`、`version_label`（不再生成新版本）；
   停机的由启动恢复补完，O-35 / 启动报告 `autobuild_runs` 一项的 `detail` 可能是「补完 n 段撤回」。

## 2026-11-02 · I1 审查修复

1. **EU-01 `notification`** 多 `replaces: string[]`：这一条替换掉的同组未读通知编号（前端从列表拿掉、未读数不重复算）。A-13 列表不变。
2. **V-03 `download=1`** 只接受 `prefix` 在 `ontology/` 下（其余 `400 invalid_argument`，`details.fields.prefix="scope"`）；未压缩超过 256 MB `413 payload_too_large`（`details.limit_bytes`）；读不出文件 `503 service_unavailable`（不再返回缺文件的 zip）。
3. **ST-09**：没给 key 时名称不能以「.」开头（`400`，`fields.name="pattern"`）；名称（或 key）超过 UTF-8 200 字节 `400`（`fields.<name|key>="too_long"`）。
4. **F-02 / F-04 / F-05 / V-04**：路径含 `..` 一律 `403 path_forbidden`（先去掉 `.` 段再判受保护目录）；解析后落进受保护目录（根内的符号链接指到 `.git`、`.onto` 等）同样 403；路径含 NUL `400 invalid_argument`。R-05 / R-06 路径含 NUL 也是 400。
5. **EV-29**：锚点含 NUL（含 `%00`）这一项 `resolved=false`、`reason="invalid"`；批量里某一项出错只判这一项。
6. **R-02**：坏的 xlsx 照常上传成功，`detected` 为 null；R-06 / EV-29 / R-07 / R-09 对坏文件按不可核验处理。
7. 报告、资料分析报告、知识条目、Studio 条目、资料列表、求解契约、蓝图、外部反馈：路径上有符号链接的不列、不读（当不存在）。

## 2026-11-02 · I1 合并（I1 合进交付分支后的衔接）

1. **EU-01 / A-13**：自主构建的三类通知（`autobuild_finished`、`autobuild_suspension`、`autobuild_stopped`）进通知中心（落盘，A-13 能列、能标已读）；「新增挂起问题」按运行成组，同一次运行没读的旧一条被替换（事件带 `replaces`），`body` 是这次运行还没回答的挂起总数。取代 B5b 第 30 条「不落盘」。
2. **EC-53 `suggestions.updated`**：另在两处发——蓝图卡更新时（BP-02 保存、BP-03 确认）发给蓝图卡所在的对话；自主构建暂停、续跑后发给运行对话（暂停时 `mode="autobuild_controls"`、`primary: null`、`secondary` 只有「运行详情」，在跑时是三个控制，与 FL-02 同一份）。
3. **ST-09**：新建的条目只有章节骨架（标题行；`from_template` 时表格章节留表头），不带条目模板里的 `{待填写…}` 与填写指引。

## 2026-11-03 · V 阶段（栈 c）

1. **SY-01** 每个引擎多 `deploy: {configured, model}`：界面没配、只靠部署环境变量接模型时，模型接入页原来两种引擎都显示「未配置 · 用部署默认值」、Key 框写「还没有设置 Key」，管理员看不出内测引擎其实已经接上、稳定引擎才是真没配。现在没配界面时按 `deploy.configured` 显示「用部署配置」或「未配置」，Key 框占位「用部署配置里的 Key」，模型框占位部署的模型名。只给有没有和模型名，网关地址、Key 不出接口。

## 2026-11-03 · F 第一批

1. **flow.yaml 命令前置多一个可选字段 `lock_waits: [<阶段>…]`**（包作者契约）：声明这条命令在等用户回复时把锁切到这些阶段（约定用 `confirm`），收尾先释放锁。声明了的命令，一轮正常结束、这次保存过、副本里没有没提交的改动、锁却不在这些阶段时，平台代为释放（ADR-0070）。没声明的命令行为不变。接口字段没有变化；执行记录内部多记一个 `released_by`，不进 ExecRecord。
2. **EC-53 建议条**：引擎出错、模型流超时把执行记为中断时，也推一份 `mode=interrupted` 的建议条（主推 `resume_exec`，`reason`「出错中断」），与用户打断时同形。
3. **FL-01 节点 `plugin.label`、PB-01 流程关卡 `source.label`**：取包清单里的 `label`，原来是包名；字段不变。
4. **PL-01 / PL-02 `channel`**：只发布到 beta 的包为 `beta`（原来 stable 通道没设时退回「最新的正式版本」并标 `stable`）。

## 2026-11-03 · F2-chat（对话页进展）

1. **EC-22 `subagent.updated`** 多 `step: {id, kind: "tool"|"text", text: string|null, state: "running"|"done"|"failed"} | null`：子代理自己的一步（读了哪份资料、跑了什么、说了什么）。同一 `id` 后到的只改状态，`text` 为 null 时沿用先到的（F2 之前写进历史的收尾事件没有说法）。`progress_text` 改为最近一步的说法（原来收尾事件会退成「读取资料」「运行脚本」）。
2. **EC-20/21 工作过程按时间分段**：助手每说一段成段的话，之前做完的步骤收成一组（done、折叠），之后另起一组（`group_id` 形如 `g-<轮>-<段号>`）。形状不变，一轮可以有多组。
3. **过程话不发 `message.assistant`**：和工具调用同在一条回复里的短话（≤160 字，没有清单、表格、标题、代码、出处角标，且不是提问前的话）作为 `state=info` 的一步进组；它的 `message.delta` 片段照发，前端在组到达时去掉。宿主历史里原文保留（`narration: true`），续接回放照用。

## 2026-11-03 · F3（去掉确认生效、检查只提示、资料分析停在蓝图、叫法改成「段」）

用户 11-03 定，推翻详设 v1.1 8.2「检查通过后用户点确认生效」（ADR-0072）。`docs/api-v1.md` 与本目录 `api-v1.md` 已同步改。

1. **变更集的生命周期**：陪伴建模一个变更集就是助手的一轮。一轮结束（成功、出错、被打断）自动合入成一个版本；下一轮开始前、服务启动时补合上一轮没走到收尾的；引擎里还排着下一条消息时并到下一轮。`pending_confirm` 只在合入前的一瞬与升级前遗留的数据里出现。实体冲突照旧记 `conflict` 进待处理。
2. **CS-04** 界面不再调，保留给遗留的待确认变更集；不再回 `gate_failed`（检查只记进 `checks`）。
3. **CS-05 撤回这一轮**：合入过的写反向提交，响应 `{state: "reverted", commit, version_label}`；之后又改过同一条时 `409 merge_conflict`，`message` 写出是哪几条、`details.entities` 列出；冲突中还没合入的照旧 `{state: "discarded", commit: null, version_label: null}`。审计 `changeset.revert`。
4. **round_changes 卡**：去掉 `lock_note`、`next_version_label`、`remaining_failures`；`title` 只写「改了 N 处」；`state` 取 `merged|reverted|conflict|pending`；新增 `hints: [{tone: info|warn, text}]`、`checks.pending`、`reverted_version_label`、`inbox_item_id`。一个变更集一张卡（`rc-<cs>-1`），之后只发 `card.updated`。
5. **合入检查**（CS-02 `checks`、卡片 `checks`）：按段建模时链接失效的目标在蓝图里已规划就记进 `pending_links: [{target, path, slice_id, slice_name, slice_no}]`（一处一条），不计进 `errors`、`warnings`；包声明的提交门禁在助手合入里只提示，问题以 `{code: 门禁 id, level, label, message, path}` 进 `findings`。
6. **IN-01** 没有 `pending_changeset`；`changeset_conflict` 的动作是 `resolve`、`discard`（撤回这一轮）。IN-03 不再接受 `merge`。
7. **FL-01 / FL-02**：建议条没有 `pending_confirm` 模式；合入冲突时主推 `{id: "resolve", label: "处理冲突", action: {type: "navigate", page: "inbox"}}`。`notices` 的 `pending_changeset` 换成 `changeset_conflict`（`action` 去待处理）。
8. **CS-06 ScopeLock**：`pending_confirm` 只表示合入冲突（`reason_text`「这一轮和你的修改有冲突，在待处理里选好保留哪边后才能编辑」）；助手在跑时的 `reason_text` 改成「……这一轮结束后才能编辑」。
9. **BP-03**：不再先合入资料分析那一轮；遗留的待确认变更集先合入（检查不拦）；蓝图所在那一轮冲突时 `409 merge_conflict`（`details.inbox_item_id`），助手还在改时 `409 changeset_in_progress`。
10. **ST-08** `committed` 多 `warnings: Finding[]`（`code=check_hint`）：链接指向不存在的文档、没有主键只提示，不再转助手复核。
11. **MCP 写工具**：`REFERENCE_NOT_FOUND`、`REFERENCE_TYPE_INVALID`、`RELATION_TARGET_INVALID`、`FIELD_REFERENCED` 进 `warnings`（`level: "warning"`），不挡写入；`RESOURCE_REFERENCED`（删除仍被引用的）照旧拒绝。
12. **AB-03 / AB-04**：开始前先合入遗留的一轮；只有合入冲突时 AB-04 回 `409 changeset_in_progress`（「有一轮的修改和你的修改冲突……」），AB-03 的 `warnings` 同一句。
13. **策略代答**：资料分析里（任何构建方式、没有运行）决定流程走向的问题（问题里说「接下来 / 下一步……建、做、提取」，或至少两个选项是流程动作）不出卡、不代答，从提问里拿掉，回给引擎的答案是「停在蓝图确认」；同一次提问里的业务问题照常。
14. **叫法**：契约 1.13 新增「叫法」一节：`slice` 等字段界面叫「段」，变更集界面不出现。原 1.13 维护模式改为 1.14。
15. **内置包版本**：F3 曾把 codegen、evo 手工升到 0.9.3、0.3.3（只改了清单说明会撞版本）；F3b 改成摘要算清单，两者退回 0.9.2、0.3.2，所有内置包按新规则换一次版本号（见 F3b 第 6 条）。

## 2026-11-03 · F3b（插件门禁一律只提示、内置包摘要算清单）

主控定（F3 回报的待定项）。

1. **写入事务的提交门禁一律只提示**：包声明 `on: commit` 的门禁不再让写入回 `422 gate_failed`；没过的规则随写入结果带回（形状 `{code: "pack_gate", level, gate, gate_label, pack, message, target, path}`），照常提交。回退（`Onto-Source: revert`）照旧不查。CS-02 与 round_changes 卡的 `checks.findings` 里，包门禁问题是 `{code: 门禁 id, level, label, message, path}`（`label` 取门禁的 label）。
2. **AR-05 保存 / AR-06 新建**：字段定义不合法（解析不了、取值不在枚举里）照旧 `422 artifact_schema_invalid`；包声明的保存门禁（`on: save`）与提交门禁都只提示，响应多 `warnings: [{code: "pack_gate", level: "warning", message, field, gate, gate_label}]`，不再回 `422 gate_failed`。
3. **AR-08 校验**：门禁问题从 `errors` 挪到 `warnings`（`code: "pack_gate"`），`ok` 只看字段定义与格式自带的检查。
4. **FL-05 开始脚本节点**：输出照常写进版本，响应 `run` 多 `warnings: [{code: "pack_gate", level: "warning", message, gate, path}]`；不再因门禁回 422。
5. **`gate_failed` 只剩发布门禁链**（PB-01 / PB-02 的 `GateResult` 与发布被挡）：写入事务、产物编辑器、脚本节点都不再回它。
6. **内置包版本号**：清单版本 + 内容摘要，摘要算清单（去掉版本号）加全部文件；只改清单也是一个新版本，镜像启动时入库、把 stable 指过去，跟随 stable 的项目下一条消息前升级。这一次所有内置包都换一个版本号。

## 2026-11-03 · F4a（连接器网关第一版：文档转换、资料的文本版、「数据接入」）

ADR-0073。契约改动已写进 `docs/api-v1.md` 与 `_plan/api-v1.md`：

1. **叫法**：「怎么读」与 3.16、3.19 加「界面叫法：数据接入」；接口路径、字段、错误码里的 `evidence`、`provider` 不变。
2. **3.6 资料**：文本版在 `resource/text/<分组>/<原文件名>.md`，不在资料列表里、不能单独删除或改用途（R-03、R-04 回 `path_forbidden`）；替换、删除原文件时同一个提交里删掉旧文本版。R-02 上传立即返回，`material.text.state` 为 `converting`（网关没部署时 `waiting`）。
3. **7.4 Material** 加 `text: MaterialText | null`（流程图为 null）；新增 `MaterialText`：`state`（native / converting / converted / unsupported / failed / waiting）、`reason`、`note`、`converter`、`updated_at`、`has_text`。
4. **新增 R-13 查看文本**（`GET …/resources/text?path=`，回 `{path, text, markdown, truncated}`，一次最多 30 万字）、**R-14 重新转换**（`POST …/resources/convert {path}`，202，失败后的重试也是它）。
5. **新增 PL-20 转换器与默认**（`GET /api/v1/converters`）、**PL-21 设默认转换器**（`PUT /api/v1/admin/converters/defaults {type, converter}`，admin，只能在已有的里选）。PL-04、PL-19 仍回 501（第三方转换器的安装方式待定）。
6. **PL-01**：数据接入（`category=provider`）只列文档转换器；`PackCard` 加 `converter: {types, default_for, license} | null`；转换器的 `project_status` 不论带不带项目都是 `platform_installed`；`counts` 带 `provider`。
7. **错误码** `not_convertible`（409，`details.path`）。
8. **事件 EP-25** `materials.text.updated`：`{path, text: MaterialText}`，转换中、等待、失败这些不出提交的状态也推。
9. **运维**：O-13 / 就绪检查的 `connector_gateway` 从占位改为真实探测（`已加载 N 个转换器`；没配网关地址为 warn「未部署」，不可达为 fail，不挡就绪）；O-35 启动报告多一步 `material_text`「资料文本版」（后台补转）；O-42 版本清单列出转换器（`category=provider`、`converter: true`）。
10. **网关接口**（内核与网关之间，不在 `/api/v1`）：`GET /health`、`GET /v1/providers`、`POST /v1/convert`（请求体原文件，`Authorization: Bearer <Ed25519 JWT>`）；内核 `GET /api/v1/connector/issuer-key` 给网关取公钥，免鉴权。

## 2026-11-03 · F4b-research（蓝图目标只存一份、去掉不在范围、资料分析只读文本、编号不问人）

ADR-0074。

1. **BP-01 / `Blueprint`**：`goal_md` 取项目信息里的建模目标（P-05 `goal`），`goal_anchor`、`goal_anchor_label` 恒为 null；`out_of_scope` 不再返回。`questions[]` 多 `kind?: "goal"`、`suggested_goal?`，最多一个目标问题，选项固定为「改成：…」「保持原目标」。
2. **BP-02**：`out_of_scope` 取消，带了也忽略。回答目标问题选「改成：…」或写 `custom_text` 时同时改项目目标并推 `project.updated`。待处理里回答（IN-03）同样处理。
3. **BP-03**：目标问题没答时 409 `blueprint_question_pending`，`details.question_id`。错误码表新增这一项。
4. **蓝图卡**：`goal_md` 为项目目标，去掉 `out_of_scope`，新增 `goal_question: {id, title, answered} | null`。
5. **工作区脚本**：`source_extract.py --sources` 输出 `{materials: [{material, read, kind: native|converted|missing}]}`；命令行给原件时退出码 2，输出 `{status: "use_text_version", text}` 或 `{status: "no_text_version", reason}`。`research_checker.py` 不再产生「新增ID确认」待人工条目；自编编号在回执的 `source_evidence` 里记为 `kind: "assigned"`；出处指向有文本版的原件时多一条「出处」警告。

## 2026-11-03 · F4b-paths（界面上不出工作区路径和文件名）

ADR-0075。契约改动已写进 `docs/api-v1.md`：

1. **7.1 `EntryRef` / `ArtifactRef` 加 `label`**：后端按元模型、产物登记算好的业务名（对象「门店」、流程图「订单流程」、建模蓝图、资料分析报告、检查报告、资料「合同」）。变更里认不出的文件 `type="file"`、`title`、`label` 为空串，路径只在 `key`。CS-02、CS-03、V-02、冲突与撤回挡住时的 `entities` 都带它。
2. **4.7 `round_changes` 卡**：行文只写业务名；认不出的文件合成 `other` 组最后一行 `{change: null, target: null, text: "另有 N 处改动", count: N}`，前端不出动词标签。`title`「改了 N 处」仍按路径数算。
3. **7.1 `AnchorResolution`**：`source_label` 改为资料名（去扩展名；文本版 `resource/text/<分组>/<原文件名>.md` 映射回原资料）；新增 `material: {name, type, path}`；行号锚点 `L12`、`L12-L15` 的 `location_label` 按前面最近的位置标记（「第 3 页」「工作表 客户」）或小节标题算，都没有时为空，不再写「第 12 行」；`L起-L止` 区间整段高亮。`p:`、`table:`、`sheet:` 旧锚点照旧。
4. **7.4 `Material` 加 `title`、`type_label`**；资料页不再按 `group` 分组（`group` 字段保留）。上传目录本来就由后端按扩展名定，界面不选目录。
5. **S-20 加 `display_text`**：对话里只显示「命令 + 说明」，不带 `args`，说明里的工作区路径换成业务名；发给助手的不变。报告页「发给助手」、待处理「交给助手处理」、Studio 复核都给了 `display_text`。
6. **其他下发文字**：版本说明（V-01 `summary`）去掉 `web:` 等来源前缀并把路径换成业务名；待处理报告事项标题用报告的一级标题；变更清单来源名、增量那一轮的对话标题用资料名；过期事项「上游（资料分析、对象）」；自主构建台账出处「资料名 §片段」（不写行号）、删除被挡的挂起原因按业务名；报告条目的位置标签按业务名；「工作过程」步骤「读取对象「门店」」「读取资料分析报告」。

## 2026-11-03 · F4c（主容器里不解析资料原件、锁恢复不问人、资料分析不露路径）

ADR-0076。

1. **内核守卫**：新增 `resource_read_guard.sh`（`_guard_common.py resource_read`），settings 基线 `Read|NotebookRead` 与 `Bash` 两处挂上；内测引擎 `read_file` 映射为 `Read`。拒绝理由里给文本版路径或「没有文本版，按未读处理」。
2. **工作区脚本**：`table_profile.py` 读资料区 Excel 的文本版（`resource_text`、`text_sheets`），没有文本版时严格读取抛「还没有文本版」；`research_checker.py` 出处指向原件时按文本版整篇核对，语料只读文本；`state_helper.py recover <命令>` 输出 `resume:{phase}` / `restart` / `ok`。
3. **后端**：`app/evidence/convert.md_tables`（Excel 文本版 → 工作表行）；`package_mapping.read_table_headers` 资料区 Excel 读文本版。
4. **R-07 / RP-02 extras**：`MaterialMapItem.label`；`problems[].display_md`；R-09、R-08 的 `anchor_label` 改为「资料名 · 位置」。
5. **BP-01、蓝图卡**：`questions[].title`、`background` 里的资料路径换成「资料名 · 位置」（蓝图文件原文不变）。

## 2026-11-03 · F4d（Studio 填证据：先选资料、再选位置）

1. **新增 R-15 `GET /resources/positions`**：一份资料里能引用的位置，逐行 `{anchor, location, text}`，只读文本（md、txt、csv 读原文件，PDF、Word、Excel 读文本版）；没有文本版时 `readable=false`。`anchor` 原样写进 `evidence_anchor`，EV-29 解析得回同一行、同一个位置说法。
2. **Studio 属性表证据列**：去掉手写锚点输入框；「补出处或专家确认」打开一个菜单：先选资料（R-01 里有文本的资料，正在转换的列在后面、点不了；暂不支持、转换失败、流程图不列），再选位置（R-15）。ST-07 草稿表单的 `evidence_anchor` 写法不变。

## 2026-11-03 · 合入 develop（目标上限、CLI 后台任务与空闲判定）

1. **P-01 / P-03**：`goal` 上限从 500 改为 2000 字符，与 modeling 创建页一致。超长时报 400，不截断；`background` 存全文，不再截到 200。
2. **Claude 引擎会话环境**：注入 `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`、`CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS=1`、`CLAUDE_CODE_BG_TASKS_REPORT_RUNNING=1`。CLI 上报的 `session_state_changed` 只给会话层判定空闲用，不进历史，也不推前端。主轮出 result 时如果 CLI 仍报 running，会话保持运行中，等 CLI 报 idle 再置空闲；用户点停止后的 result 直接置空闲（ADR-0009）。
3. **不移植**：develop 上旧 onto 前端的建模目标确认卡（`PUT /api/projects/{id}/goal`、`goal_confirmation_required`），由蓝图的目标问题（BP-03）替代，目标每条消息都由会话注入（F4b）。评测判分模型与模型池选择（`eval/model-profiles`、`eval_judge_config`）随评测移出 onto（ADR-0020），只保留 modeling 服务侧的实现。

## 2026-11-03 · F5（对话页回到旧版样式、开始自主构建可接管、截断不再当成完成）

用户 11-03 定，推翻 F2-chat 第 2、3 条里「过程话收进工作过程」「子代理挂进工作过程组」两处。

1. **过程话照常发 `message.assistant`**：工具调用之间的话不再作为 `state=info` 的一步进组，前端也不再去掉在流的片段。前面做完的步骤照旧收成一段（EC-20/21 形状不变）。F2 写进历史的 `narration: true` 记录回放时仍不出正文（那一步已经在组里）。
2. **子任务单独成卡**：派出子任务（Task / Agent）不在工作过程里占一步，派出时前面做完的步骤收成一段；`subagent.updated` 照发（形状不变），前端按 `task_id` 在时间线上单独成卡，排在第一次出现的位置，不再按 `group_id` 挂进组。
3. **AB-03 `busy`、AB-04 `take_over`**：预览时别的对话正占着助手，返回 `busy: {conversation_id, waiting}`；开始时带 `take_over: true` 就先撤回没回答的问题（EC-34，`reason`「已交给自主构建，按「拿不准时」的策略处理」）、打断那一轮，再开始。AB-04 的 `session_busy` 多带 `details.waiting`，`message` 分「在等你回答」与「在另一条对话里干活」两种。
4. **「拿不准时」的说法**：`below_threshold_text` 改为「先按最稳妥的做法往下做，做完列出来给你复核」/「先跳过这一项，留给你来定」；自动决定的 `reason_text` 改为「没有把握，先按最稳妥的做法往下做，等你复核」/「有把握，按推荐的做法」。取值（`conservative_assumption`、`suspend`）不变。
5. **工作区不显示金额**：自主构建开始卡的「预算」写时长与 token（原来是金额与时长）；运行条、运行详情、运行报告卡、开始前预览、待处理里的运行摘要不再显示费用。接口里的 `cost` 字段照旧返回，费用上限仍在项目设置里配置、仍然生效。
6. **内测引擎输出截断**：一次模型调用因长度截断（`finish_reason=length`）、没有工具调用、正文几乎为空时，带一句提示续一次；连续第二次截断就结束——子代理以失败返回原因（`subagent.updated` 为 `failed`），主助手给出「这一步模型的输出被截断（思考过长），没有产出结果。可以发「继续」让助手接着做。」。原来这种情况被当成正常完成，子代理返回一个字的残片。


## 2026-11-04 · F6（自主构建不按预算判停、结束后确认回到陪伴建模、Studio 表格搜索词跟着地址走）

用户 11-04 定：自主构建只在做完、叫停、出错时结束，预算这套设置整套拿掉；运行结束后点「确认」回到陪伴建模。推翻 B5b「预算」与 F5 第 5 条里「费用上限仍然生效」。

1. **策略没有预算**（AB-01、AB-02、AB-03）：AB-01 不再返回 `budget`、`quota_remaining`，`platform_limits` 只剩 `{allow_production_collection}`；AB-02、AB-03 的 `policy_overrides` 带了 `budget` 忽略，不再回 422 `budget_exceeds_quota`（错误码删除）。AB-03 预览不再返回 `budget`。运行记录里的 `policy` 不再有 `budget`。
2. **运行不按用量判停**：删除 EP-16 `autobuild.budget_warning`；运行不再进入 `budget_exhausted`（F6 之前的老运行仍可能是这个状态，前端照旧显示）。用量照常记：`AutobuildRunSummary.tokens` 只有 `{used}`，`cost` 只有 `{used}`；AB-16 报告的 `metrics` 去掉 `budget_duration_ms`、`budget`；开始卡 `autobuild_start` 的 chips 去掉「预算」。`autobuild.safe_point.next` 不再出现由预算换来的 `stopped`。
3. **配额用完暂停**：发一步时撞上配额（`quota_exceeded`），运行变为 `paused`，`AutobuildRunSummary` 新增 `pause_reason: "quota"|null`（只在 `paused` 时有值），不记失败原因；AB-09 续跑照常，续跑后 `pause_reason` 清空。花费的硬上限只剩配额（SY-15、SY-16）。
4. **新增 AB-19 确认结束的运行** `POST /api/v1/projects/{id}/autobuild/runs/{run_id}/acknowledge` · 所有者 · 审计：是（`autobuild.acknowledge`）。
   - 只对已结束的运行；还没结束或正在收尾回 409 `autobuild_not_running`，运行不存在 404 `run_not_found`。可以重复调。
   - 记下确认；项目没有别的进行中运行时把构建方式改回 `L2`；不论改没改都发一条 EP-20 `project.updated`（`fields: ["build_mode"]`），各标签页以它为准。
   - 响应 `AutobuildRun`；`AutobuildRunSummary` 新增 `acknowledged: bool`，运行对话里已确认的结束运行不再显示运行条。
5. **SY-17 去掉单次上限**：不再有 `max_duration_min`、`max_tokens`、`max_cost`；PUT 带了忽略，旧文件里的这几项下次保存时清掉。剩 `max_concurrent_runs`、`allow_production_collection`、`night_priority`、`running`、`not_enforced`。
6. **项目卡、运维总览**：P-01 `ProjectCard.autobuild` 去掉 `budget_used_pct`；O-01 `autobuild_runs[]` 去掉 `budget_used_pct`、`budget`（`cost` 保留），`autobuild_today` 去掉 `stopped_by_budget`。
7. **Studio 表格的搜索词跟着地址走**（前端）：对话里的条目链接带 `q` 进表格时按它筛；点「表格与条目」页签回到不带 `q` 的地址时清掉；点一行时地址带着原来的 `q`。原来换地址不重建这一页、搜索词只在有 `q` 时才设，从链接进来后再点页签仍只显示那一条，新出的条目看不到，要先切到图谱再切回来。

## 2026-11-04 · F7（对话工作流：平台排队与插话、发送与停止合一、子任务进度、下一步建议按规则表）

起因：5195 上运行中发的命令进了 Jiuwen 引擎自己的缓冲，平台看到第一个结果就判空闲，执行记录挂错命令、引擎在跑界面显示空闲；
打断后建议条不重算，部署改了能力包指纹后「从打断处继续」必然 409。Claude CLI 实测同类问题：运行中的文字被插进当前一轮，
平台却按「一条消息一个结果」等结果，这一轮永远收不到；打断后 CLI 会接着跑排着的消息。

1. **运行中发的消息由平台排队**（S-07、S-20）：`delivery` 新增 `steer`（文字，插进这一轮，主助手下次调用模型前看到）和
   `next`（命令，平台代存一条，这一轮结束后在同一步里开始，中间不报空闲、不推建议条；`message_id` 为 null，开始时才写用户气泡）。
   已有一条时 `next_occupied`，带 `replace: true` 替换。原来的 `deferred` 只留给自主构建运行对话的补充说明。
   新事件 EC-15 `message.delivery`、EC-16 `next.updated`；S-05 带 `next`；新接口 S-22 取消、S-23 现在开始。
2. **空闲判定**：只有引擎没在跑、且没有已交给引擎还没被看到的插话时才报空闲。两个引擎给会话层同一套信号：插话、送达（seen /
   new_turn）、结果里这一轮消耗了哪些消息、打断时撤回了哪些。Claude 读原始消息流（`command_lifecycle`、`user_message_uuids`），
   打断带 `cancel_queued`；拿不到这些字段的旧 CLI 退回「一次发送一个结果」。Jiuwen 去掉引擎内缓冲，插话只注入主 agent。
3. **打断**（S-08）：响应带 `returned`，没被看到的插话撤回（引擎侧也撤回），界面放回输入框；代存的命令保留、`paused`，不自动开始。
4. **子任务进度**：EC-22 加 `step_count`、`brief`、`thinking`；名称依次取描述、任务说明第一行、子代理类型；新接口 S-21 分页取步骤；
   Jiuwen 子任务步骤上限 60 → 200。
5. **下一步建议**（FL-01/FL-02、EC-53）：按 10 条规则从上往下判；`Suggestions` 加 `mode` 取值 `running`、`failed`，加 `note`、`rev`；
   动作加 `retry`、`abandon_exec`、`focus_question`，去掉打断后的「换个方向」，「先做上面的决定」换成可点的「去回答」。
   能否执行的检查生成时过滤、接口收到 `from_suggestion` 请求时校验，不能执行回 409 `suggestion_stale` 并带重算后的一份。
   项目锁定的旧版 `flow_state` 不支持新规则时用镜像版本。
6. **续跑**（S-16）：能力升级、输入改过时不再回 `exec_not_resumable`，按新版重新执行（`mode="rerun"`，原记录放弃、`replaced_by`
   指向新记录）；`exec_not_resumable` 只剩 `already_finished`。S-17 请求加 `conversation_id`、`from_suggestion`。
7. **界面**：输入框右下角发送与停止合成一个按钮（Esc 照旧）；时间线末尾不再放「思考中」和打断，改为输入框上方的运行状态行
   （在做哪一步 · 在等什么 · 跑了多久）；`session_busy` 的文案不再说「另一条对话」。

影响：Studio 复核、`autobuild/review.py` 在助手运行中发的消息分别变成插话、代存命令（后者已有一条时失败；正常只在运行结束后发）。
Claude 引擎依赖 SDK 的私有接口（`_query`、`_send_control_request`、`message_parser`），SDK 升级失效时退回旧行为（插话变成下一轮）。

## 2026-11-04 · F8（建模不分段、资料分析之后不停、平台代存的改动进待处理、零售包默认关、数据 schema 自动处理）

用户 11-04 定，推翻 B4d 的分段建模（ADR-0050～0052），见 ADR-0078。起因：同一模型下 3 段项目从对象提取到校验 10 轮、
107 分钟，插件化之前同样三步 3 轮、43～46 分钟；分段还让前段对象写不进跨段关系的另一半。

1. **BP-01 / `Blueprint`**：`mode` 恒为 `full`，`slices` 只有一段 `all`「全部」（老蓝图里的多段照样返回，界面按全量显示）；
   `questions`、`terms` 不变。BP-02 改分段照常校验，但不再影响建模。BP-03 保留，界面不再调。
2. **蓝图卡** `confirm_options` 恒为 `[]`；`goal_question` 照旧，没答不再挡任何操作。
3. **FL-01 / FL-02**：资料分析做完不再显示「等你确认」；建议条去掉「确认蓝图」「调整分段」「重新规划分段」「规划分段」；
   目标问题没答时主推 `answer_goal`「回答目标问题」，次选是主线下一步（一般是「提取对象」）。
   建模节点的「规则」一步可选，没过期不推荐；`core.consistency` 改为旁支（`placement: branch`、`optional`），不挡发布。
4. **AB-03 / AB-04**：不再回 `409 blueprint_not_confirmed`；单元只有一段 `all`，可选的「规则」一步只在过期或
   `include_optional` 时排；场景走查不再是建模里的一步。AB-02 的 `scope.slice_ids` 里对不上的编号忽略，`mode: selected`
   没剩下的按 `all_pending` 处理，不回 400。
5. **宿主注入**：建模目标后面加一行统一叫法（`统一叫法：X（不用 Y）…`，最多 30 条），只进发给模型的消息。
6. **变更集**：记录多 `collected: bool`（平台在一轮结束时代为提交了助手没提交的改动）；`round_changes` 卡在
   `collected` 且有错误时 `hints` 首条为 `{tone: "warn", text: "助手没提交完，平台代存了这一轮，检查没过的在待处理里"}`。
7. **IN-01** 新类型 `check_findings`「检查未过」：最近一个合入的变更集 `collected` 且检查有错误时出一条，`impact` 列出错误
   （最多 20 条、每条 ≤200 字），动作 `send_to_assistant`（发 `/ontology-validate` 修正这些问题，发出后这条算处理过）、`dismiss`。
8. **能力包**：`industry-retail` 的 `default_on` 改为 `false`（新建项目默认不启用；已启用的项目不变）。
9. **工作区资产**：删 `SLICE_MODE.md`；三个建模命令去掉「分段建模」一节；资料分析 Step 6 只写统一叫法与待定问题；
   `blueprint.py brief` 全量模式带统一叫法；`ontology_checker` 关系对端的端点按元模型解析（`[[X#锚点]]` 不再误报两端不一致）。
10. **数据 schema 自动处理**（用户 11-04 要求「数据 schema 自动识别，自动处理」）：
   - 建模命令（资料分析、对象 / 操作 / 规则提取、校验）交给引擎前，项目里有资料在转文本就先等，最多 10 分钟：EC-01
     `session.state` 为 `running`、`reason: "waiting_materials"`，运行状态行显示「等资料转成文本」。等完仍没有文本版的
     （没等到、转换失败、暂不支持）在这一轮的宿主注入里点名，按未读处理。打断（S-08）、删项目会撤回还在等的命令。
   - `table_profile.py --headers` 把全部表头写到 `.ontology-state/table-headers.json`，stdout 改为摘要（`status`：`ok` /
     `no_schema` / `text_missing`，`file`，表名 → 列数，`missing_text`）；`--profile` 与 `field_semantics.py types` 同样分出
     `text_missing`，不再当成「没有表」去请用户标 Schema。对象提取的子代理按路径读表头（`TABLE_HEADERS_FILE`）。
   - Excel 转换（convert-xlsx 1.1.0）：表头保留全部工作表、全部列；每表数据最多 2000 行、10 万格；全文上限不变。
     `ConvertLimits` 去掉 `max_columns`、`max_sheets`，加 `max_sheet_cells`。
   - 规则检查新增警告「数据来源候选」：「# 数据来源」为「无」或没写表的对象，存储属性英文名与某张 schema 表有 ≥2 列同名
     （含全部主键）时列出候选表；只提示，不要求每个对象都绑表。
   - 对象提取不再问来源怎么用（默认流程图与资料分析报告结合）、不再逐条问待确认的类型（列进本轮摘要）。

## 2026-11-05 · F9（普通用户装自己的插件包、运行报告的检查数）

用户 11-05 要求「普通用户也可以安装插件」，同时修 F8 对照实验里发现的三个问题。决策见 ADR-0079。

1. **新增 PL-22～PL-26**（`/api/v1/me/pack-uploads`，登录即可）：普通用户上传、看检查结果、下载报告、安装到自己名下。PL-13～PL-18 不变。
2. **`PackUpload`**：加 `scope`（`private` | null），`state` 加 `installed`。`PackCard.publisher.kind` 加 `private`。
3. **私有包只归上传者**：别人的私有包在 PL-01、PL-02、PL-03、PL-06、新建向导（P-02）里按不存在处理；已经锁在项目里的不受影响。O-25 列出私有包，管理员能禁用。
4. **PL-17**：普通用户的上传也能发布，发布后变成公开的包；包名是别人的私有包时回 `pack_name_taken`。
5. **C-01 `limits`** 加 `pack_private_upload_bytes`（20 MiB）。错误码加 `pack_name_taken`、`pack_quota_exceeded`，`pack_upload_invalid.reason` 加 `too_large`。
6. **运行报告的检查数**：`AutobuildReport.metrics.checks`、运行详情 `checks` 改为最终版本相对运行开始时的结果（运行记录新增 `final_checks`）。原来把各单元合入时的数字相加，校验里修掉的提醒还算在内。老运行没有这个字段，照旧相加。
7. **不分段时的措辞**：`autobuild_start` 卡片的「范围」写「未完成的步骤」；待处理里自主构建决定的影响范围写「步骤」，「全部」写成「建模」；启动报告「补完 N 段撤回」改为「补完 N 次撤回」。老运行按段的照旧。
8. **场景推演**：建模里已经没有这一步。报告指标、运行详情、结果表没有场景数据时不占位；项目设置去掉「生成场景」开关（`generate_scenarios` 字段保留，老策略读得出来）。

## 2026-11-05 · F8 补充（「场景走查」改名「场景模拟」、删掉全局一致性）

用户 11-05 定。原来页面叫「场景走查」、节点叫「全量回归」、校验节点显示成「校验与走查」，三个名字说的是同一件事，看流程找不到对应的页面。
全局一致性是为分段加的，F8 不分段后跨段检查不再触发，剩下的检查不再保留，也不并进校验。

1. **trace 包**：`label`、左栏页面（`pages[].nav.title`）改为「场景模拟」；节点 `trace.trace` 的 `title` 由「全量回归」改为「场景模拟」；
   界面和命令文档里的「走查」都改成「模拟」（「交互模拟」「模拟报告」「模拟分支选择」）。编号（`trace`、`trace.trace`、页面 `scenarios`、
   问题类型 `walkthrough`）和命令名不变；`/ontology-trace 交互走查`、`走查` 这两个旧触发词照样认。
2. **FL-01**：trace 包不再声明 `node_titles`，`core.validate` 的 `title` 恒为「校验」。`node_titles` 机制保留，没有包在用。
   主线：资料分析 → 建模 → 校验 → 场景模拟 → 发布。
3. **删 `core.consistency`**：节点、探针、`consistency` 命令、`/ontology-consistency`、`consistency_check.py`、建议条的「全局一致性」都去掉；
   `core.publish` 的 `after` 改为 `[trace.trace, core.validate]`。
   - 老包写的 `core.consistency` 锚点：`aliases` 映射到 `core.validate`；PL-02 `flow_insertions[].at` 不再有 `consistency`，归 `validate`。
   - AB-02 老策略的 `scope.until_node` 是 `core.consistency` 时按 `null`（跑完主线）处理，不回 400。
   - 已有的 `reports/consistency/` 报告留着，报告列表照常显示，不再更新。
4. 官方包版本号带内容摘要，改动后是 core、trace 的新版本；跟随通道的项目在下一条消息前升级。已有执行记录、对话里的旧名字不改写。

## 2026-11-05 · F10（用户和管理员导入插件的权限一致）

用户 11-05 定，决策见 ADR-0080。

1. **PL-22**：去掉 `private_scope` 检查项，能带的内容和管理员上传一样（脚本、节点、hooks、界面，各种 `kind`），静态检查按租户包的严格程度查，
   多一项 `private_name`。PL-23～PL-26 不变。
2. 可见范围、包名规则、资源上限不变；发布到全平台（PL-17）还是只有管理员。

## 2026-11-05 · F11（插件中心下载开发 Skill）

决策见 ADR-0081。

1. **PL-27（新增）**：`GET /api/v1/packs/dev-skills/{category}`，登录即可，可用 `?token=`。`capability`、`industry`、`system`、`provider` 给 zip，
   `exporter` 和不认识的分类回 `404 not_found`。其他接口不变。
2. **插件中心**：分类视图的页头多一个「下载开发 Skill」按钮，在「安装插件」左边；全部、已启用、可更新视图没有这个按钮。
3. **检查器进 zip**：`services/packs/` 的 `manifest.py`、`repository.py`、`checks.py`、`extensions.py` 和 `kernel/artifacts/registry.py` 原样打进去。
   改这几个文件时注意它们也在离线跑：不能新增对后端其他模块的依赖，要依赖就同步 `dev_skill.py`，`test_dev_skill.py` 会查。

## 2026-11-05 · F12（「数据接入」改名「多源连接器」）

用户 11-05 定。插件分类、界面和给助手看的文档都改叫「多源连接器」，数据源页的列名和申请详情里叫「连接器」。`category: provider`、`kind: provider`、接口路径都不变。

## 2026-11-05 · F13（REA 逆向连接器与证据层公共底座）

用户 11-05 定：多源连接器先用 REA 打通公共底座，再加数据库和代码连接器。决策见 ADR-0082，三方接口与格式见 `design/evidence-collection.md`。

1. **EV-31～EV-33（新增）**：分块上传目标程序，存数据目录的 blob 仓，不进工作区。
2. **自助类提供方**：EV-10 多 `self_service`、`input`、`max_artifact_bytes`。`self_service=true` 的提供方（REA）登录用户可以调 EV-02 自助登记，
   EV-04、EV-05 登记人也能调；这类登记只有本人和管理员看得到。要凭据的提供方仍然只有管理员登记。
3. **错误码**：2.8 加 `artifact_not_found`、`upload_not_found`、`upload_incomplete`、`upload_hash_mismatch`、`upload_too_large`。
4. 契约 3.19 其余接口按原文实现；EV-06、EV-07（凭据）本轮仍回 `501 evidence_not_available`，等数据库连接器。EV-28 ER 视图只给数据库类，其他回空表。
5. **非数据库快照的检索形状（前端子任务补）**：证据浏览器左栏要按模块分组、右栏要显示位置和成员，原契约只写了数据库表的形状。
   EV-23 对非数据库快照按 `attrs.module` 分组、`entities` 多 `type`；EV-25 的 `fields` 列类的成员；EV-23/24/25/27 带 `untrusted_content`
   （`EvidenceRecord` 加可选字段）；EV-02 自助登记的 `budget`、`masking_policy` 也可省略（登记抽屉不出现这两项）。

6. **内核实现时补的（F13 内核；第 5 条在前端分支）**：
   - EV-08 对旁路容器类（REA）不签票据，`ticket` 为 `null`；自助类数据源的登记人不带 `project_id` 也能测。
   - EV-25、EV-29 多可选参数 `path`（引用锚点的产物）：按它 frontmatter 的 `evidence_snapshots` 解析；EV-29 的 `bin:` 锚点走证据索引。
   - EV-26 每项多 `anchor`（命中的锚点），`location` 是 `<文件>:L<行号>`。
   - 快照「被引用」只算 `ontology/` 里的已确认产物（EV-18/19 的 `referenced`、EV-20 的 `snapshot_referenced`、EV-21 的
     `touching_confirmed`）；`reports/` 里是候选和检查报告，不挡清理。
   - EV-22 先做最小可用：每份受影响的已确认产物写一份 `reports/evidence-revise-*.md`（每个变了的锚点一条 `- [ ]`），
     `inbox_item_ids` 是待处理里对应的 `report:<文件名>`。
   - EV-01 普通用户看别人登记的数据源时，`connection`、`egress`、`cred_ref` 给空；`EvidenceSource` 多 `self_service`。
   - EV-02、EV-04 带 `secret` 回 `501 evidence_not_available`（凭据接口这一轮不开放）。
   - `CollectionJob.result` 多 `commit`；任务错误码另有 `backend_restarted`、`gateway_restarted`、`gateway_unavailable`、`bundle_invalid`、
     `bundle_incomplete`、`snapshot_too_large`、`timeout`、`artifact_not_found`、`source_not_found`，其余透传网关的码。
   - EV-09 的 `statement_digest` 对上传类是 `artifact:<sha256 前 12 位>`，`rows`、`bytes` 取快照清单的 `budget_used`。
   - 新增 **EV-34** `DELETE /api/v1/evidence/uploads/{upload_id}`（上传人）：取消没完成的上传、释放每人 2 个的名额；已完成的回 `upload_not_found`。

## 2026-11-05 · F14（导入后从实际进度开始）

用户 11-05 定，决策见 ADR-0084。

1. **FL-01 节点状态**：spine 节点的产出已经被后面完成的节点用上（对方 `stale_on` 里有它产出的类别）时标 `flags.backfill`，
   `display_state` 为 `backfill`，不计入进度分母，下一步建议不推它。导入已发布知识包后的资料分析就是这样。插件串入节点的规则不变。
2. **P-10**：`after_import.start_node` 按包里的交付物推算，不再固定为 `core.research`；迁移包为 `null`。新增 `start_title`。
3. **导入页**：「导入后」一栏按 `start_title` 显示从哪一步开始。

## 2026-11-05 · F15（增量构建包与范围限定 confine，ADR-0085）

决策见 ADR-0085。

1. **新官方能力包 `incremental`（增量构建）**：默认不开，插件中心能力包分类里多一张卡片。启用后主线是资料分析 → 建模 → 圈定范围 → 范围内修改
   → 传播核对 → 校验 → 场景模拟 → 发布；传播核对没完成时 PB-01 多一条 `flow:incremental.check` 门禁挡发布。产物在工作区 `incremental/` 下，不进 `reports/`。范围外改动在核对报告里是待处理项，不能标记跳过。
2. **flow.yaml 顶层 `confine: {scope_file}`（新增）**：只能有一个包声明；`scope_file` 是工作区里的具体文件，不在 `ontology/` 和受保护目录下，
   在本包节点的 `produces` 里，不满足时组装失败。组装出的 flow.yaml 里是 `confine: {scope_file, pack, node, entry}`。
   范围文件（主线上的，或副本里刚确认的；放宽只认主线）`status` 为 `active` 时，`baseline.commit` 里有、`editable` 里没有的本体条目对助手只读（写、删都拒），新增条目不受限；
   范围文件只在声明它的节点的命令运行时可写。网页编辑不受影响；包停用后限制一起去掉。
3. **节点 `autobuild: false`（新增）**：自主构建排计划时跳过这个节点。
4. **PL-15**：流程声明了 `confine` 时，静态检查多一项 `confine`（范围限定声明正确）；没声明时没有这一项。开发 skill 的协议文档和离线检查同步。
5. **core 0.4.0**：表示内核支持 `confine`；用到它的包声明 `depends: {core: ">=0.4"}`。
6. 其他接口不变，FL-01 的响应里没有 `confine`。

## 2026-11-05 · F16（数据库与代码仓库连接器）

用户 11-05 定：REA 合进 PR 555 并部署后，接着做数据库和代码仓库连接器。决策见 ADR-0086，接口与格式见 `design/evidence-collection.md` §8～§10。

1. **EV-06、EV-07 开放**：内核把凭据立即转交网关 `PUT /v1/secrets/{源}`，只存 `cred_ref` 和字段的写入时间；EV-02、EV-04 带 `secret` 时同样转交。
2. **提供方**：`db-mysql`、`db-postgres`（discover、snapshot、sample）、`code-repo`（snapshot）。都要管理员登记（`self_service=false`）。
3. EV-08 对这几类走网关 `POST /v1/sources/{源}/test`，回连通、只读账号、语句白名单三项；EV-09 来自快照包清单的 `access_log`。
4. **内核实现时补的（F16 内核）**：
   - EV-02：连接型的 `connection` 按提供方 `cred_fields` 的非密钥字段校验（都必填、密钥字段放进来拒绝）；`egress` 必填且要含连接目标
     （数据库 `host:port`、代码仓库主机 443）；数据库要写 `scope_allowlist`；`budget`、`rate_limit` 各项是正数。带 `secret` 时先登记再写
     凭据，写不进去撤回登记。EV-04 按合并后的登记整体再核一遍，带 `secret` 写不进去时改动撤回。
   - EV-05 删有凭据的登记时先删网关里的凭据，网关不通就不删登记（503）——登记没了凭据不能留在网关里。
   - EV-06 的错误：网关回 400、404、409、413、422 时原样带回它的错误码（消息里回显的明文遮掉）；鉴权失败、5xx 一律 502
     `upstream_unavailable`（`details.gateway_error`），免得前端把网关的 401 当成登录过期。EV-07 网关回 404 也算删掉。
   - EV-02/04 的 `masking_policy` 改成可省略、只收提供方支持的（数据库 `mask-v1`，REA、代码仓库 `none`），缺省按提供方；契约例子
     `mask-v3` 改成 `mask-v1`（前端子任务对照契约提出）。
   - EV-08 连接型 `ok`、`checks` 原样转网关的（代码仓库不一定三项都有）；还没写凭据时不调网关，回 `ok=false`、`ticket=null`；网关拒绝回
     422 `connection_test_failed`。
   - EV-09 连接型按快照包清单的 `access_log` 一条一行；`at` 没给时取任务开始时间。
   - EV-13/14：`scope` 按写法核对是否落在白名单里（原来是逐字相等）；`sample.selector` 是单个对象名 `库.表`，要落在白名单和本次范围里，
     `limit` 1～20、缺省 20；`summary.tables`（只算表）、`estimate` 来自最近一次 discover 的清单；`summary.window` 是 AccessWindow 或 null；
     `masking.sample_rows` 数据库是 20（sample 时是 `limit`），其他 0。任务错误码多 `credential_missing`（登记没写凭据）。
     `CollectionJob.result` 多 `inventory`（discover）；discover 不提交快照；sample 是补充快照，网关成功就是 `succeeded`（不是 `partial`）。
   - EV-23 数据库快照按 `<库>` 分组，每项多 `kind`（`table|view`）。
   - EV-25：`fields[].type` 取记录顶层的 `data_type`（§9 列记录和代码字段都放顶层），多可选 `pk`、`fk`、`nullable`；视图按 `attrs.columns`
     列字段；代码记录的下级把 `rule` 也列进来。`samples` 多 `masked_columns`、`snapshot_id`；`profile` 单位是百分比（记录里的整数比例按
     `pct_scale` 换算，`top_k` 的 `{value, count}` 按画像行数算），每项多 `null_pct`、`distinct`；列记录顶层多 `masked`、`data_type`、`nullable`。
   - EV-27 每项多 `relation`、`columns`；推断关系的 `basis` 用 `overlap_permille`（「取值重合 98%」）或 `basis=naming`（「命名推断」）；
     关系名 `references` 显示「引用」。EV-28 多 `total`、`truncated`，关系多 `anchor`、`columns`，只列表不列视图，`focus` 可以是列锚点。
   - 引用扫描（EV-18/19 的 `referenced`、EV-20、EV-21 的 `touching_confirmed`、EV-23 的 `mapped_to`、EV-26）认 `db:<源>/<库>.<表>[.<列>]`
     （原来的正则要求源后面跟 `<类型>:`，数据库锚点一个也认不出）。
   - EV-29：`db:`、`code:` 锚点在工作区有这个源的快照时走证据索引（原来一律 `not_available`）。
   - 对象文档「# 数据来源」首行可以写 `db:<源>/<库>.<表>`（可带反引号）：按这个源最新的完整快照绑定，发布包的来源Sheet 和 mapping 的
     `resource` 只写表名（不带库名；下游 modeling 把它当一个标识符加引号，`库.表` 会被当成一个名字）；快照里没有这张表是
     `declared_missing`，列一个都对不上是 `no_columns`。没有快照时照旧当表名处理。

## 2026-11-09 · F17（工作台插件开发模式，ADR-0087）

决策见 ADR-0087。路径都在 `/api/v1/projects/{project_id}` 下，都要项目所有者；写操作要项目可写（`require_writable`）。

1. **对话（S-01～S-04 扩展）**：对话视图已有的 `kind`（`chat` | `autobuild`）多一个值 `plugin`；多一个字段 `plugin`（绑定的包名，还没写出草稿时为 null）。
   S-02 `POST /conversations` 收 `{kind: "plugin"}`，标题缺省「新插件」。插件对话第一次写出 `plugins/<包名>/` 时绑定这个包名，没手工改过标题的
   改成草稿的 `label`（没有 label 用包名）。一条对话只绑一个包。
2. **PD-01** `GET /plugin-drafts` → `{items: DraftView[]}`：主线上 `plugins/*/manifest.yaml` 能读出的草稿（读不出清单的也列，`manifest_ok=false`），
   按最近修改排。**PD-02** `GET /plugin-drafts/{name}` → `DraftView`（带 `files`、`precheck.items` 全量）。
3. `DraftView`：

   ```
   {
     name, label, description, kind, category, version,            // 来自清单；读不出时只有 name
     manifest_ok: bool,
     file_count, files: [{path, size}],                            // path 相对草稿目录；PD-01 不带 files
     updated_at,                                                   // 主线上最后一次改这个目录的提交时间（ISO）
     conversation_id,                                              // 绑定这份草稿的插件对话，没有为 null
     contents: {
       nodes: [{id, title, kind, placement, section, after, after_title, before, before_title, blocks_publish: bool}],
       commands: [{name, label}], agents: [name], skills: [name], scripts: [name],
       knowledge: {concepts, checks, standards, files} | null      // 行业包、系统知识包的数据条数
     },
     precheck: {state: "passed"|"failed", summary: {pass, warn, fail}, items: [{id, label, result, detail}]},
                                                                    // PD-01 只带 state、summary 和失败项
     install: {
       state: "not_installed"|"installed"|"outdated",              // outdated：草稿内容和项目里启用的版本不同
       enabled_version,                                             // 本项目锁定的版本，没启用为 null
       installed_versions: [..],                                    // 包仓库里本人这个包的版本
       next_version,                                                // 现在点安装会用的版本号（已在仓库里就升补丁号）
       changes: [{path, status: "added"|"modified"|"deleted"}],    // outdated 时：草稿相对启用版本
       diff: {path, lines: [{op: " "|"-"|"+", text}]} | null        // outdated 时：第一个改动的文本文件，最多 40 行
     }
   }
   ```

   `after_title`、`before_title` 是锚点节点在本项目流程里的标题（找不到时为 null）；`blocks_publish` 是节点 `blocks` 含 `core.publish`。
   预检：主线上的草稿复制到临时目录、写校验和，跑上传同一份 `checks.run(tenant=True)`，加 `private_name`（包名没被别人占），加 `dir_name`
   （目录名和包名一致）。不建上传记录。
4. **PD-03** `GET /plugin-drafts/{name}/files?path=<相对路径>` → `{path, content, size, binary: bool}`；文本最多 256 KiB，超出或二进制时 `content` 为 null。
5. **PD-04** `PUT /plugin-drafts/{name}/files` 收 `{path, content}`：网页改草稿里的一个文本文件，作为一次用户提交合入主线（同 Studio 保存，可以撤回）。
   插件对话正在跑时回 409 `session_busy`。路径越界、改 `checksums.sha256` 回 400。成功回 `{path, size, draft: DraftView}`。
6. **PD-05** `POST /plugin-drafts/{name}/install` → `{draft: DraftView, version, upload_id, enabled: {version, effective}}`：
   从主线取草稿；版本号在包仓库里已有时升补丁号并写回草稿（平台提交）；然后私有上传、静态检查、安装（PL-22、PL-26 同一份逻辑），
   本项目没启用就启用（PL-06），启用了别的版本就升级（PL-07）。静态检查没过回 422 `static_check_failed`（`details.items` 失败项的完整对象
   `{id, label, result, detail}`，同时给 api-v1 错误表里同一错误码的 `details.failed`：失败项的 label 列表），
   包名被占回 409 `pack_name_taken`，版本到上限回 409 `pack_quota_exceeded`。安装成功在绑定的插件对话里留一条通知
   （`{"type": "notice", "payload": {"kind": "plugin_installed", "text": "「<label>」<版本> 已装好，启用到本项目"}}`），
   再给本项目最近更新的一条建模对话（`kind=chat`）留一条（`{"type": "notice", "payload": {"kind": "plugin_enabled",
   "text": "插件「<label>」<版本> 已启用"}}`，「切回建模试用」进的就是这条）。
   审计 `pack.upload`、`pack.private_install`、`pack.enable`。停用用 PL-09。
7. **项目流事件**：`plugin_drafts.changed`（`data: {names}`）：草稿在主线上变了（一轮合入、PD-04、PD-05 写回版本号）、装了、停用了。
8. **对话卡片**：插件对话一轮结束、这一轮改到了 `plugins/<包名>/` 时，出卡 `{"type": "card", "payload": {"card_id", "card_type": "plugin_draft",
   "kind": "plugin_draft", "name", "version", "changed_files": [..], "changeset_id"}}`（卡片约定同 EC-40：`card_id` 每轮唯一，
   `pd-<changeset_id>-<包名>`；`kind` 留作兼容），不出通用的「这一轮改了什么」卡；卡片的实时状态按 PD-02 取。
   卡上的「撤回这一轮」按 `changeset_id` 用 CS-02 查（合入后 `state=merged`），撤回走 CS-05。
9. **插件对话里不同的地方**：FL-02 建议条为空（`primary=null`、`secondary=[]`、`all_commands=false`）；命令目录不适用；注入只有草稿状态；
   会话指纹不含 `assembly`、`flow`，改含 `plugin_dev`（插件开发说明更新了）。

**实现补记（2026-11-09，后端）**：

- S-02 的 `kind` 只收 `chat`、`plugin`，别的回 400 `invalid_argument`；建插件对话时自主构建在跑回 409 `autobuild_running`。
- DraftView：清单读不出时 `label`、`description`、`kind`、`category`、`version` 都是 null（字段都在）。`contents.commands[].label` 取命令文件的第一个
  `#` 标题，没有用 frontmatter 的 `description`，再没有用命令名。`files` 只在 PD-02。
- 预检多一项 `limits`（文件数或大小超私有包上限时才有）。预检时清单的版本号先换成 `install.next_version` 再查，所以装过的版本不会报
  「已在包仓库里」。
- PD-03：草稿里没有这个文件回 404 `not_found`；符号链接按 `binary=true`、`content=null` 返回。
- PD-02 草稿不存在回 404 `not_found`。`install.next_version` 清单读得出就一定有（没装过就是清单里的版本）。
- `install.state` 按本项目锁文件算：停用（PL-09）之后锁文件里这一条 `enabled=false`，马上算 `not_installed`、`enabled_version: null`，
  不等下一条消息生效。
- PD-04：`content` 不是字符串或超过 256 KiB 回 400；路径含 `.git` 也回 400。
- PD-05：插件对话正在跑时同样回 409 `session_busy`（安装会写回版本号，和助手这一轮的修改会撞）；平台预检没过时直接回 422，不建上传记录；
  `enabled.effective` 同 PL-06 的 `effective`。走升级那条路时审计记 `pack.upgrade`（不是 `pack.enable`），和插件中心的升级一致。
  两条通知的 payload 另带 `name`、`version`、`align: "center"`。没有建模对话时不留第二条。
- FL-01 带插件对话的 `conversation_id` 时 `suggestions` 同样为空；空建议的 `mode` 用 `normal`（契约的取值里没有单独的值）。
- `plugin_drafts.changed` 跟着 `workspace.changed` 发（提交改到 `plugins/<包名>/` 就带上这些包名），另外 PD-05 装好、PL-06～PL-09 改了
  同名草稿的启用状态时也发。
- 会话指纹的部分（EC-07 `changes[].kind`）多一个值 `plugin_dev`，摘要「插件开发说明有更新」。

**端到端验证后的修正（2026-11-10，真实模型）**：

- DraftView 的 `install` 多 `reuse`（bool）：草稿和本人包仓库里同版本号的那份内容一样（停用后再装、撤回到装过的版本）时为 true，
  `next_version` 就是那个版本。PD-05 这时不上传、不建上传记录，直接启用（没启用时）、升级或回滚（草稿版本比启用的低时）到那个版本，
  `upload_id` 为 null，审计只记 `pack.enable` / `pack.upgrade` / `pack.rollback`；插件对话的通知写「「<label>」<版本> 已启用到本项目」。
  装好且没改时 `next_version` 等于启用的版本（原来是下一个补丁号）。
- 装配换过版本后续接的建模对话：每条对话记下上次连引擎时的装配与流程版本（续接点旁的 `.assembly` 文件），续接时版本变了，
  第一条消息的注入多一句提醒（旧版命令说明作废、执行前重读 `.claude/commands/`，`plugins/` 下是草稿）。两个引擎都带，内测引擎经 worker 的
  connect 参数 `assembly_changed` 带进去。不只插件开发，插件中心升级、启用也同样生效。
- 内核守卫解析 Bash 时，紧挨重定向符的数字（`2>/dev/null`、`2>&1`）按文件描述符处理，`>&`、`<&` 后面是数字或 `-` 时不算写文件。

## 2026-11-10 · 端到端验证修复

真实后端、真实模型下从新建项目走到数据接入时发现的偏离，前端此前只对 mock 跑通。

1. **C-01 `features.evidence`**：不再固定为 false，部署了连接器网关（`ONTO_WEB_CONNECTOR_GATEWAY_URL` 有值）时为 true。证据层（F13、F14）已经落地，
   原来的 false 让前端在真实部署里隐藏系统逆向建模包、「连接业务系统」起点和数据源页，数据接入没有入口。没部署网关时仍为 false。
2. **PL-01 `PackCard.required`**：新增，必装包（core）为 true。不带项目时 `project_status` 为空，新建向导只认 `required` 状态锁开关，
   原来「基础建模能力」能被关掉。
3. **EV-02 代码仓库**：出口白名单要含的连接目标是仓库地址的主机和端口（没写端口是 443），前端缺省值原来固定写 443；登记时前端不再发
   `masking_policy`（表单里没有这一项），原来带上数据库的 `mask-v1` 被内核拒绝，界面上登记不了代码仓库。mock 的登记校验对齐内核：
   只有数据库要写范围白名单，脱敏策略按提供方。
4. **EV-13 的排队提示**：任务 `state=queued` 且 `queue_reason` 为空时是刚提交、马上开始，不再提示「等访问时间窗口」。
5. **FL-02 建议条第 7 条（过期刷新）**：范围限定生效（增量构建一轮进行中，ADR-0085）时不出。原来范围内修改改了对象后，建模的操作一步过期，
   主推「刷新『操作』」（整段重新提取）压过了「传播核对」；整段重新提取会写范围外的条目、被守卫拦下，导入的项目也没有资料分析报告可依据。
   flow_state 的 `SUGGEST_RULES` 升到 3，锁定旧 core 的项目按镜像版本判。
6. **工作台顶栏轨迹**：`needs_review`、`auto`、`pass` 显示为已完成，`backfill` 淡显。原来只认 `confirmed`，导入知识包后建模（需复核）显示成没做、
   资料分析（可补做）显示成待办。
7. **增量构建 `/incremental-scope` 确认范围用确认卡**（选项「确认」「调整」，去掉传播链上的条目在对话里说），原来列一串「回复 xx」的文字。
8. **S-11 多选题**：决策卡按 `multi` 做多选，答复传 `option_ids`（契约原本就有，前端只做了单选：助手问「可多选」时用户只能选一项）。
9. **FL-02 等你在对话里回复**：命令停下来等用户打字回复（锁在命令声明的等回复阶段、没有确认卡）时，建议条不出别的命令，`note` 写
   「『<命令>』在等你回复上面的问题」；`note` 不再只跟主推一起出现。原来这时按流程推「刷新『操作』」等，点了会把命令丢在半路。
10. **CS-02 / 产物卡的业务名**：插件产物用文件里的标题（知识条目的 `name`），原来是文件键；`index.md`、知识库的 `INDEX.md`
    是目录索引，不算产物。

## 2026-11-10 · 端到端验证待定事项：配额与审核申请

1. **SY-15 配额的计数口径与默认值**（ADR-0088）：计入配额的 token 改为「输入 + 缓存写 + 输出 + 缓存读 // 10」，`429 quota_exceeded` 的
   `details.used` 同口径；默认每人每天 10,000,000、每项目每月 100,000,000。项目用量（P-16）、运维用量图里的 token 仍是原始数。
   自主构建运行的 `tokens.used` 和配额同口径。
2. **AD-08～AD-10 落地**（原来后端没有、界面没挂）：列表合并数据源登记申请（EV-03）和知识提名（K-08）；插件安装申请（PL-04）还没有。
   视图按契约，另带 `note`（备注或退回原因）、`reviewed_by`、`reviewed_at`、`source_id`（数据源申请通过后登记的 ID）；`payload` 不含凭据。
   分页用 `limit`（最多 200）和 `cursor`（下一页起点）。AD-09 多一个 `secret`：数据源申请通过时由管理员写凭据，按申请走 EV-02 的校验登记、
   授权给申请人的项目；登记没通过原样回错、申请仍待处理。知识提名通过只记结论、通知提名人，进行业包下一版仍由维护人做。
3. **EV-03 带 `project_id`**（申请人的项目，要是所有者）；**EV-01 带 `project_id` 时多 `my_requests`**：本人在这个项目提交、还没通过的申请
   （待处理、已退回带原因），提交后马上看得到。
4. **通知**：管理员的 `request_pending` 带 `action: {label: "去处理", page: "plugin-upload", params: {tab: "req"}}`，前端跳转带上 `params`；
   新增类型 `request_reviewed`（数据源申请通过或退回，`action` 指向申请人项目的数据源页）。知识提名的结果仍用 `nomination_reviewed`。

## 2026-11-10 · 端到端验证待定事项：插件中心的连接器

用户 11-10 定：插件中心「多源连接器」把数据库、代码仓库、REA 这些采集类连接器也列出来（原来只有文档转换器，用户不知道平台能接什么）。

1. **PL-01**：部署了连接器网关时，在包仓库的卡片之外合成网关里的采集类连接器卡片（取网关提供方清单，内核已缓存 30 秒）：`category="provider"`、
   `converter=null`、新字段 `collector={kind, evidence_types, input, self_service, installed, readonly_method, license_note}`，`description`
   是「采集实体、字段、……」。装好的 `project_status="platform_installed"`，没装的（如没部署 REA 旁路容器）是 `platform_available`；平台安装（PL-19）
   仍不开放，前端对采集类不出安装按钮，写「需要管理员部署」。和包仓库里同名的以包为准。没部署网关、网关不通时不列，目录照常出。
   包仓库里的包卡片 `collector=null`。
2. **PL-02**：采集类连接器的名字也能打开详情，`contents`、`flow_insertions`、`changelog` 为空，`released_at` 为 null，`requires` 为空；前端详情页
   只列能采什么、只读方式、许可、登记方式（自助登记或要管理员），不出「包含什么」「更新记录」页签和「启用到本项目」。

## 2026-11-10 · 端到端验证待定事项：会话与对话展示

1. **空闲回收不打断等你回复的那一步**（ADR-0089）：回收原因是回收（空闲、池满驱逐、切换对话、运维手动回收）、执行记录是「等待」时不记中断、
   不放续跑卡，下一条消息照常接着做；在跑时被回收、删除对话照旧中断（EC-05、续跑卡不变）。
2. **S-20 对话里显示业务名**：用户气泡和自动标题是「节点标题 · 按钮叫法」（叫法里已有节点标题时只写叫法，如「资料分析 · 仅本地资料」
   「传播核对」），原来是命令原文（`/ontology-research 仅基于本地文档`）。请求多一个可选字段 `display_label`（按钮叫法）；给了时 `text`
   是动作自带的参数，不在对话里显示。命令面板、快捷项改发 `display_label`，不再把按钮叫法当参数发给助手。用户在输入框里自己打的
   `/命令` 照原样显示。
3. **工作过程不露内部动作**：平台内部的状态维护（`state_helper.py` 的命令锁、进度、阶段，读写 `.ontology-state/`、`.onto/run/`）和助手读自己的
   说明（`.claude/` 下）不进工作过程的步骤（progress、子任务的 `step`），`tool.activity` 照旧（本来就是 `visibility: internal`）。命令行一步按
   工作区脚本给中文说法（如「核验资料分析报告」「加载业务知识」），模型写的中文描述照用，英文描述改成「运行脚本」。
4. **子任务名称**（EC-22 `title`）：中文的简短描述照用；模型写的英文描述不用，按派发说明里点名的子代理给中文名（如「资料分析」「覆盖检查」），
   认不出时叫「子任务」。子任务面板去掉「任务说明」（那是平台派给子代理的内部提示词）；`brief` 字段保留。

## 2026-11-10 · 端到端验证待定事项：流程引导

1. **RP-02 / RP-03 报告条目**：`ReportItem.status` 多 `skipped`（报告里用户明确跳过的条目，记 ⏭️）；`✅`、`🔧` 开头的条目算 `fixed`
   （原来只认 `[x]`，校验命令收尾写的标记都显示成待处理）；摘要和 `progress.handled` 把 `skipped` 算已处理；RP-03 的 `counts` 多 `skipped`。
   流程探针、待处理、发布关卡照旧只认 `[ ]`。校验命令里用户选「跳过」的那批问题记 ⏭️，只选了部分编号时没选中的仍是 `[ ]`。
2. **FL-02 第 7 条（过期刷新）**：节点或步骤过期、后面有已确认的必经节点能核对并修复同一批改动（`stale_on` 盖住、`produces`/`writes`
   盖住）时不出上游刷新（ADR-0090）。`SUGGEST_RULES` 升到 4。
3. **flow.yaml 动作的条件 `when`**：动作可写 `when: [探针名]`（`!` 取反），不成立时 FL-01 节点动作和 FL-02 建议条都不出；内核探针在工作区里
   算不了时不过滤。装配时检查用到的探针已声明。FL-02 的上下文多 `probes`。
4. **core 资料分析动作**：「仅本地资料」改叫「资料分析」；「追加分析」要已有资料分析报告（`when: [research_report]`）。
5. **系统逆向建模包 0.2.1**：`rev.evidence` 完成要有完整快照且 `reports/evidence/` 下有取证报告；动作按条件出：没快照「采集快照」、
   有快照「整理取证报告」、有报告后「三方对账」。
6. **命令里的提问**：流程走向不再问（逻辑提取没流程图时、规则检查做完进语义检查、批量追踪开始、缺测试数据时补生成），用户选过修订方向
   或要修哪些后不再二次确认（候选修订直接写回，语义修复不再问「确认 / 逐条复审 / 全部撤销」）；校验选要处理哪些、增量更新的清单改用确认卡。

## 2026-11-10 · 实测：规则与指标提取之后建模往回退、建议条推「刷新」

「规则与指标提取」（记在指标旁支）顺手在操作里补了回链，建模里用同一条命令的「规则」一步因此过期，整个建模显示过期，
建议条主推「刷新『规则』」——就是刚跑完的那条命令；校验、场景模拟过期后在顶栏画成空圈，看着像流程往回退。

1. **合入确认**：全量模式下，合入的节点的入口命令也是建模某一步的命令时，那一步在「全部」这一段一并记为确认过（`slices.steps_done_by`）。
2. **FL-01 建模节点状态**：只有可选步骤（规则补抽）过期时不算这一段过期，节点仍是已确认；`slice_stale` 照常列出那一步。
3. **FL-02 第 7 条**：可选步骤过期时，后面的核对节点（ADR-0090 的条件）只要已确认、过期、能开始、在跑或待确认，就交给它，不推补抽；
   必经步骤仍只在核对节点已确认时才不推。`SUGGEST_RULES` 升到 5。
4. **工作台顶栏轨迹**：`stale` 单独一种样式（琥珀色、刷新图标），和没做过的空圈分开。

## 2026-11-11 · 过期按钮写哪类变了；可选步骤不插队

「刷新『校验』」看不出为什么要刷新；模拟发现绕道跑规则与指标提取后，建议条会在「刷新操作」「刷新指标」之间来回推（ADR-0090 补充）。

1. **FL-02 第 7 条**：按钮改成「对象有变化，刷新操作」——前半句是确认之后真变了的那几类（多于两类写「对象、操作等」，老记录说不清时
   只写「刷新X」），按段时后半句是「段名 · 步骤名」；`reason` 改为「按改动后的内容重新做这一步」，不再出说明行 `note`。
   必经的排在可选的前面；可选节点和可选步骤在后面的核对节点还会跑时不出。`SUGGEST_RULES` 升到 6。
2. **FL-01 节点动作**：过期节点的刷新动作同一个说法。
3. **节点与步骤的确认记录**多存按类别的指纹 `kinds`；没有它的老记录按确认时刻（`at`）主线上的提交比到现在、看哪几类目录有改动，还说不清时按钮只写「刷新X」。

## 2026-11-11 · P5b：规则原始资料出处、Studio 最后更新时间（吸收 develop !589）

develop 新增的 `GET /api/projects/{pid}/studio/updates`、`GET /api/projects/{pid}/source-preview` 不进本分支（本分支没有调用方，不补旧路径映射），
功能并进现有契约（ADR-0093）：

1. **ST-03**：`EntryRow` 加 `updated_at: time | null`（最后更新时间：提交时间，未提交的取文件修改时间，纯改名不算，git 不可用为 null）；
   `sort=updated` 实现为新的在前、取不到的排最后。
2. **R-06**：`around` 认行号范围 `L12-L15`（按第一行定位）；`path` 可以是文本版 `resource/text/…`，此时文件头不出块，页、工作表标记
   记在后面每一块的可选字段 `location` 上。主容器仍不解析原件（ADR-0076）。
3. **元模型**：规则多一节「原始资料来源」（选填，在「来源」之后）；ST-04 的 `sections`、模板、提取说明随之变化。
4. **工具箱**：`source_extract` 抽取器版本 1 → 2（稳定快照、Office 解压上限 256 MiB、单个正文 32 MiB、原文件 20 MiB），`located_text` 认行号范围；
   `research_checker` 核对「详细信息」里的出处（文件不存在、锚点找不到、省略路径都不放行）。core 包内容摘要随之变化，跟随通道的项目下一条消息前升级。

## 2026-11-11 · P5a 资料上传与表格（吸收 develop !567 资料部分、!589 上传提示；ADR-0096）

1. **上传白名单与上限**：类型清单与 20 MiB 上限不变（仍按 1.8，没收 develop 新加的 `.webp`，见 gaps）；资料进资料区的所有入口在服务层（`save_resource`）
   再挡一次，前端改用 C-01 的 `material_types`、`limits.material_upload_bytes`，不再各页写一份（资料区原先不收图片、新建页不校验）。
   上传 `.doc`、`.xls` 照收，传完提示另存为 `.docx`、`.xlsx`。
2. **R-01 `Material.schema_scope`**：数据 Schema 表格的 Sheet 范围摘要 `{mode, pending, included_sheets, reference_sheets, error}`，别的资料为 `null`。
3. **R-03 请求体加 `schema_selection`**：`{mode: "auto"}` 或 `{mode: "manual", included_sheets, revision}`；不带时沿用原来的人工范围，
   对不上当前文件报 `409 schema_scope_stale`。新错误码 `schema_scope_stale`、`material_text_pending`。
4. **新增 R-16 `GET …/resources/schema-preview?path=`**：逐 Sheet 的表头与问题、建议范围，只读。develop 是 `POST …/resource-meta/schema-preview`
   （要写权限），这里读接口用 GET、所有者可用；develop 的 `force` 参数不要了（文本版按修订缓存，换了就重读）。
5. **范围的存法**：`materials.json` 只存人工范围 `{version: 1, mode: "manual", included_sheets, revision}`（`revision` 是 sha256 前 16 位）；
   develop 连现算的范围也存、字段名是 `file_sha256`，这里不存现算的，也不认 develop 存的 `mode: "auto"` 列表（按现算处理）。

## 2026-11-11 · 吸收 develop !568：判定执行超时（ADR-0097）

运行中长时间没有引擎事件时，会话层判定执行超时、按出错收尾，子任务卡不再一直转圈。

1. **EC-04 `session.error`** 多一个字段 `reason`：`"stall_timeout"` 表示判定执行超时，其余出错为 `null`。
2. **子任务卡**：这一轮收尾时还没终态的子任务显示「已中断」，`reason` 为 `stall_timeout` 时显示「执行超时」；下一轮开始后不再转圈。

## 2026-11-11 · 版本回退只恢复业务内容、加并发保护；版本列表带发布记录（吸收 develop !567、!570，ADR-0094）

1. **V-05**：响应加 `expected_head`、`ready`、`blockers: [{code, message, details}]`；`files` 只列业务内容（不含 `.gitignore` 等仓库配置、助手装配、目录 `index.md`）；不在当前版本历史里的提交回 `invalid_commit`。
2. **V-06**：请求加可选的 `expected_head`，不符回 `409 head_moved`；新增错误 `session_busy`、`studio_draft_pending`、`workspace_dirty`（与 V-05 的 blockers 同一组判断，取第一条）。主线有没提交的业务改动时由「不带进提交、照常回退」改为拒绝。回退之后助手会话回收、全部对话续接点作废。资料随回退恢复（不变）。
3. **V-01**：响应加 `release_error`；`Version.release` 改取发布记录（版本号来自 Hub），读不到时退回本地标签，版本照列。
4. **错误码**：新增 `studio_draft_pending`、`workspace_dirty`（第 2 章）。
5. 为什么：develop 的回退语义（只恢复业务内容、有人在改时挡住、预览后主线前进不执行）落到本分支已有的 V-05/V-06；develop 的 `/history` 合并时间线不另起，发布标在版本行上（设计稿第五版）。
6. **MCP（M-03、M-08）**：新增 scope `project.version.revert`（read、change 两个工具集都认）与三个版本工具 `list_project_versions`、`preview_project_version_revert`、`revert_project_version`（先预览拿 `preview_id`，再 `confirm: true` 执行；凭据只在进程内存）；`revert_project_version` 在 `tools/list` 里标 `destructiveHint`、`idempotentHint`。输入输出与错误码沿用 develop 的同名工具。

## 2026-11-11 · 正式版本以知识包 Hub 为准，求解验证移到评测服务（ADR-0098）

吸收 develop 的 Hub 发布与求解验证迁移（develop-sync P1）。

1. **PB-03 发布**：要统一账号登录（本地模式 401）；请求加 `request_id`（幂等键）与 `solver_unverified_acknowledged`；版本号由 Hub
   分配（`0.1.N`），不再按主线计数；包根目录与 manifest `id` 改为 Hub 包身份；响应多 `hub`。Hub 不可用 `503 hub_unavailable`。
   同一提交再发布是新版本（同一 `request_id` 重试才是同一版本）。
2. **PB-01**：门禁「求解契约回执」改为「求解契约」（只看静态检查，`requires_acknowledgement`）；知识包结构门禁加 consumer 导入规则；
   响应多 `solver_revision`、`structure`。**SV-01** 去掉 `gate`、`receipt`，加 `requires_validation_after_publish`。
3. **PB-04/05** 的 `ReleaseRecord` 多 `hub`；**PB-07** 留存缺失时带登录令牌从 Hub 下回缓存。
4. **退役**：X-01、求解验证候选包、X-04、SV-04、服务令牌权限 `receipts.write`。
5. **新增** HB-01～HB-09（`/api/v1/hub/...`、`/api/v1/projects/{id}/hub/...`）；外部路径 `GET /api/hub/package-projects` 原样提供。
6. **MCP token** 权限加 `project.version.revert`、`package.publish`；含发布权限的 token 签发时在 Hub 登记委托。

## 2026-11-11 · 评测对接改为独立评测服务（ADR-0092）

develop 的评测服务（eval-service）独立部署后，评测结果以用户登录读回，评测优化要接到反馈演进。

1. **新增 3.21a 独立评测 ES-00～ES-11**：`ES-00` 完成通知沿用评测服务调用的外部路径 `POST /api/internal/evaluation/notifications`（免登录，
   只认请求头 `X-Evaluation-Service-Token`）；`ES-01` 评测入口 `GET /api/v1/evaluation/entry`；`ES-02`～`ES-08` 在
   `/api/v1/projects/{id}/evaluations` 下（状态与对账、报告、优化建议、完整回答、重新生成、评测优化、标记处理失败）；
   `ES-09`～`ES-11` 旧版评测记录只读（`/api/v1/projects/{id}/legacy-evaluations`）。develop 的 `/api/projects/{id}/package/evaluations*`
   与 `/api/settings/evaluation/entry` 只有 develop 旧前端在调，不进兼容表。
2. **X-05 退役**：`POST /api/v1/projects/{id}/feedback` 删除（没有调用方）。反馈批次改由 `ES-07` 写入，格式仍是 `onto-feedback/v1`，
   新增可选字段 `attachments: {report, answers, suggestions, tables}`（工作区相对路径，只认 `reports/` 与 `.ontology-state/eval-service/`），
   评测优化写的批次可以没有条目（全部通过）。
3. **旧评测接口**：`GET /api/projects/{id}/eval/*` 仍是 410，`details.successor` 指向 `ES-09`。
4. **错误码**：2.9 节新增 `login_required`、`eval_*`、`artifacts_not_ready`、`artifacts_changed`、`version_mismatch`、
   `optimization_not_supported`、`answer_not_found`、`note_required`。

## 2026-11-11 · 知识包版本对比（PC 组，吸收 develop !585）

develop 新增了知识包版本对比，发布侧按 Hub 的 `release_id` 引用、每次读结果回 Hub 核对读权限。本分支的发布记录按项目版本号，改为
按版本号引用（ADR-0095）。

1. **新增 PC-01～PC-08**：`/api/v1/projects/{id}/package-comparisons` 下的上传输入、取消上传、删除输入、发起对比、结果、差异文件、
   单文件差异、删除对比；3.0 分组表加 `PC-`，1.8 上传上限加一行，2.9 加 `compare_*` 错误码。
2. **与 develop 的差别**：
   - 发布来源 `{kind: "release", version}`（develop 是 `{ontology_id?, release_id}`）；鉴权只看项目所有者，不再回 Hub 核对；
   - 文件列表游标分页（develop 是 `offset`），单文件差异的参数叫 `path`（develop 是 `file_id`）；
   - 删除返回 `204`；时间是 RFC 3339；
   - 403、409、410 补上具体错误码；非工作区错误不把原文给界面（`error_code="compare_failed"`）。
3. 旧路径 `/api/projects/{id}/package-comparisons/*` 不做兼容映射，调用方只有 develop 的旧前端。

## 2026-11-11 · 集成：发布检查的求解验证豁免、发布后回写评测

1. **PB-01**：求解契约关的 `requires_acknowledgement` 按评测服务的求解验证结果判定（同 PB-03 的豁免口径）：当前修订最近一次验证通过时为 false，带 `verified_revision`；没登录统一账号不判定。此前只有 PB-03 免确认，界面仍要求勾选。
2. **PB-03**：发布成功后调评测对接的回写（ADR-0092），响应可带 `evaluation_writeback`。


## 2026-11-11 · F18（增量构建按上传资料触发，ADR-0091）

用户 11-05 定，决策见 ADR-0091（原定 F16、ADR-0086，号被数据库与代码仓库连接器占用后改号）。

1. **flow.yaml 顶层 `activation: {event, requires, session}`（新增）**：`event` 现在只有 `materials_added`；`requires` 只能写本包声明的探针；`session` 在本包节点的 `produces` 里。组装结果多 `activations`（包 → 声明）。
2. **节点 `visible_if: activated`（新增条件）**：包的触发发生过且没处理，或这一轮开着时才出现。**节点 `preempt`（新增）**：节点出现且没做完时，下一步建议先推它。
3. **探针 `{kernel: session_open, session: <路径>}`（新增）**：这一轮开着。
4. **`confine.lock_on_activation`（新增）**：触发后、范围确认前，已有本体条目对助手全部只读。
5. **PL-15**：声明了 `activation` 或用了 `visible_if: activated` 时，静态检查多一项 `activation`。
6. **增量构建包**：启用后没传新资料时节点不出现，不挡发布，也不限范围；已有条目时上传资料才触发。这一轮发布后自动结束；有改动没核对干净时「结束本轮」被拒。
7. 触发记录在主线工作区的 `.onto/run/activation.json`，不进 git。FL-01 的响应形状不变，节点集合变化时 `flow_hash` 跟着变。
8. **core 0.5.0、增量构建 0.2.0**：core 0.5.0 表示内核支持上面 1～4；用到它们的包声明 `depends: {core: ">=0.5"}`。建议条规则版本 `SUGGEST_RULES` 升到 7（多一条 6.5「触发后抢先的步骤」）。

## 2026-11-12 · 发布行为与界面对齐 develop（ADR-0098 第二轮）

1. **发布前必须完成四个建模阶段**（与 develop 同规则）：资料分析、对象提取、操作提取（含旧 logic 目录）、质量检查，阶段目录里有可见文件才算，
   index.md、隐藏文件、符号链接不算；资料分析验收没通过、对象也没提取时不算。PB-03 先回 `409 modeling_stages_incomplete`
   「建模未完成，请先完成：…」（`details.missing_stages`）；Hub 发布、MCP 预览（`blockers[0].missing_stages`）同一规则。
2. **PB-01** 加 develop 发布检查的同名字段：`solver`（含已验证免确认 `verified_release`）、`structure`、`stages`、`last_publish`；
   门禁链在缺阶段时第一关出 `modeling_stages`。
3. 前端：发布页的发布检查卡、「已发布版本（知识包评测）」弹窗（HB-05，发布成功后自动打开）、项目列表「导入已有知识包 ▾」与
   「导入已发布知识包」弹窗、求解面板「求解验证」卡，元素与文字照 develop。

## 2026-11-12 · 评测对接与 develop 对齐（ADR-0092 第二轮）

用户要求除评测优化外行为和界面元素与 develop 一致。

1. **未登录统一账号**：`ES-02`～`ES-08` 改回 develop 的 401「个人 Hub 包需要统一账号登录」（码 `login_required`）；`ES-02` 不再返回 `configured=false`。
2. **`ES-09` 改为旧版评测的执行详情**：响应 `{state, evolution, evolution_enabled}`（develop `GET /eval/status` 的 `state`、`evolution`），不再列旧报告；
   启动迁移（eval_retirement）不再删 `.ontology-state/eval.json`。`ES-10` 响应多 `path`。
3. **新增 `ES-12`** `POST /api/v1/projects/{id}/legacy-evaluations/{id}/optimization`：旧报告的评测优化，与 `ES-07` 一样成一批外部反馈。


## 2026-11-12 · 版本历史与回退对齐 develop（develop-sync 第二轮 P3，ADR-0094）

1. **V-09 新增** `GET /api/v1/projects/{id}/history`：develop 的历史时间线，迭代与 Hub 发布版本合并分页（`version_history.py` 原样移植）。版本页左栏改用它。
2. **V-05** 加 `changes: {added, modified, deleted, total}`（develop 确认框的计数）；`blockers` 的 `message` 改为 develop 原文。
3. **V-06** 响应加 `changes`、`warning`；回退后删掉 `.ontology-state` 派生状态（第一轮按指纹失效、不删，改回 develop）；暂存了元数据也拒绝；提交说明改为「revert: 回退到版本 N（短提交号）」。
4. 错误提示文字照 develop（「项目已产生新版本，请重新预览后再回退」「当前业务内容与该版本一致，无需回退」「只能回退到当前版本历史中的提交」等），错误码沿用 v1。
5. 会话池加 `workspace_busy`（develop 同名），回退的「助手在跑」按它判断。

## 2026-11-12 · 知识包版本对比改回 develop 的语义（PC 组，ADR-0095 修订）

第二轮对齐 develop：除路径挂在 `/api/v1` 下、错误体带契约码外，PC 组的参数、分页、响应与鉴权都照 develop。

1. **来源**：发布版本改回 Hub 引用 `{ontology_id?|hub_package_id?, release_id}`，能选当前用户在 Hub 上可见的版本；`{version}` 留作本项目简写。
2. **鉴权**：要统一账号（Hub 身份），本地模式 401；读结果、文件列表、正文差异、重放时回 Hub 复核读权限；比较数据按 Hub 主体隔离。
3. **参数与响应**：PC-06 改回 `offset/limit`，响应 `{items, total, filtered_total, overall_total, offset, limit}`；PC-07 参数改回 `file_id`；
   PC-02 返回 `{cancelled: true}`，PC-03、PC-08 返回 `{deleted: true}`；时间字段改回 epoch 秒；任务失败的 `error` 是异常原文。
4. 跨包的任务失败不再带 `error_code`（develop 没有），前端照 develop 在发起前比较两侧包标识、确认后再带 `allow_cross_package`。


## 2026-11-12 · 对齐 develop !547：drawio 高级编辑拿合进原文的 XML（ADR-0097）

1. **新增 AR-10** `POST /api/v1/projects/{id}/artifacts/{type}/{key}/drawio`：返回交给 drawio 高级编辑器的 XML，不落盘。不带 `content` 是文件原文；
   带 `content`（标准编辑器里还没保存的模型）时把模型合进原文，和 AR-05 标准编辑器保存写出的一样，尺寸、样式、泳道、批注、其他页都保留。
   流程图弹窗切到高级编辑时调它，原来的「有未保存修改时按模型重新生成 XML」只在取不到时兜底。
2. **流程图编辑会话**：同一修订、或已经越过的旧修订的刷新不覆盖编辑内容；载入按代次丢掉过期结果。

## 2026-11-12 · P5a 第二轮：资料上传与 Sheet 范围对齐 develop（ADR-0096 改写）

1. **上传白名单加 `.webp`**（同 develop），图片转换同 png；拒收消息改成 develop 的「不支持文件“X”的类型」「文件“X”超过 20 MB，无法上传」，码仍按 1.8。
2. **R-01**：`Material.schema_scope` 从摘要改为 develop resource-meta 的完整范围（含逐 Sheet 的表头与问题、`file_sha256`、`warnings`、`error`），
   Excel 还没文本版时是 `{pending: true, …}`；新增 `Material.schema_candidate`（疑似数据表）。
3. **R-03 `schema_selection`** 改为 develop 的形状 `{mode, included_sheets, file_sha256}`（第一轮是 `revision`），存下的范围同 develop
   （含现算的 `auto` 范围）；错误码 `schema_scope_stale` 改为 develop 的 `schema_selection_invalid`；新增 `resource_source_modified`（资料有没提交的改动）。
4. **R-16** 加 `force`，权限改为能写（同 develop 的 write），响应字段同 develop（`file_sha256`、`warnings`，去掉 `revision`、`pending`），
   错误文字同 develop；Excel 还在转文本版时 `409 material_text_pending`。

## 2026-11-12 · P5b 第二轮：规则出处与来源预览对齐 develop（ADR-0093 修订）

1. **出处写法改回 develop**：资料分析报告的来源位置、「详细信息」出处与规则「原始资料来源」写资料原件路径加锚点
   （`#L12`、`#p:3`、`#table:1/row:2/cell:3`、`#sheet:表!B3`；本分支另有 PDF 的 `#page:3`），不再写文本版行号。
   早先写文本版行号的出处照旧能核对、能定位。
2. **文本版带原件锚点**：convert-docx 1.1.0、convert-xlsx 1.2.0 的输出文末加一段 `<!-- onto:anchors 1 … -->` 注释
   （`app.evidence.convert.anchor_index` / `parse_anchor_index`，工作区 `source_extract.py` 同口径）：Word 段落 `p:<n>`
   （带标题、列表记号长度）、表格行 `table:<t>/row:<r>`；Excel `sheet:<名>`、`row:<Excel 行号>`、`merge <名> A1:C1 …`、
   `cols <名> <n>`（整张表有值的最右一列，扫全部行）。
   老的 Word、Excel 文本版启动迁移时重转一次补上。
3. **source_extract**：资料区里的 Word、Excel、PDF 读文本版、按锚点注释给出原件的段与锚点，不解析原件；CLI 读文本版时
   回 `cite`（原件路径）。抽取结果多 `text_version`、`merges`（Excel）。
4. **R-17**（新）：`GET /resources/source-preview`，照 develop `/source-preview`。
5. **R-06**：docx 的块改由带锚点的文本版给出（锚点不变）。
6. **ST-03**：`updated_at` 与 `sort=updated` 不变（第一轮已加）；前端列宽、排序循环（降序 → 升序 → 取消）照 develop。

## 2026-11-12 · 知识包共享授权与删除项目时一并删除 Hub 包（develop !590，ADR-0098）

1. **知识包共享**（HB-11、HB-12，新增）：develop 把「所有者加建模者」同步成 Hub 授权用户；本分支项目只有所有者，
   改成「所有者加知识包的共享用户」，共享在知识包这一层管理、不碰项目权限。首次发布时初始化，之后增量同步，失败按用户重试。
   PB-03 响应多 `hub_members`。管理员换所有者（AD-07、删用户移交）时给新所有者排授权，旧所有者留作共享用户。
2. **P-06** 请求体加 `delete_hub_package`、`hub_usage_confirmed`；新增 **HB-10** 删除预览。删除处理中或已删除时 Hub 发布与共享修改回 409。
3. 新增 `auth.resolve_hub_member`：授权前在统一账号目录核实账号，要 `ONTO_WEB_AUTH_ADMIN_TOKEN`（已有配置项，不新增变量）。

## 2026-11-12 · 集成：知识包删除中的错误码与共享状态

1. Hub 知识包删除中或已删除时，PB-03、HB 共享的写接口返回 `409 hub_package_deleting`（此前 PB-03 被归成 `422 gate_blocked`、共享接口被归成 `revision_conflict`）；MCP `preview_package_publish` 同样以这个错误码挡住，发布任务失败时不标可重试。没发布却要求删除知识包时返回 `409 hub_package_not_published`。
2. `GET …/hub/sharing` 的 `hub_sync` 带 `status`（`not_published` / `failed` / `synced`），与保存、重试同一结构。

## 2026-11-16 · 吸收 develop !591～!600（评测端口、重复提交、关系导出、项目加载、任务名长度）

1. **评测服务默认端口 8090 → 5180**（!600）：`ONTO_WEB_EVAL_URL` 的 compose 默认值、浏览器入口的同主机默认端口
   （ES-01 `url` 为空时）、配置说明与 ADR-0092 一并改。
2. **连续重复提交**（!596）：S-07、S-20 在这一轮没结束时（在跑、排队、连接中、有积压、命令链要接着跑）又提交和上一条
   相同的内容，回 `409 message_duplicate`；S-20 这条任务记为 `cancelled`。本分支的差异：代存的命令在上一轮结束后接着
   开始（`_chain`）、自主构建发的每一步不受这条限制；打断（S-08）、取消代存的命令（next/cancel）之后同一条可以马上
   重发（撤回的插话放回输入框后重发、「打断并发送」）。连接失败时清空积压消息，重试不重放旧命令。
   前端：发消息、发命令、建议条、建模步骤共用一个提交中拦截，请求回来前再点只提示「消息正在提交，请勿重复操作」；
   这一轮没停下又发相同文字时不先画本地气泡。
3. **发布导出的对象关系**（!594）：标准、图两种变体都用严格的关系检查，结构错误在渲染时以
   `422 relation_structure_invalid` 拒绝，发布检查经渲染门禁列出同码 Finding；图变体的边与标准变体同一份（保留没绑定物理表的语义关系）。
   本分支没有评测导入，develop 的 `EVALUATION_PACKAGE_INVALID` 只在评测服务里出现。
4. **项目列表加载**（!593）：「我的项目」首屏显示加载中；加载失败给错误和「重试」，不再当成「还没有项目」；切筛选、搜索时
   迟到的响应不覆盖新结果。旧前端「深链项目等列表加载」在新前端由各页自己取项目，不需要移植。
5. **任务名称长度**（!597）：新建、重命名、克隆、导入、设置页的项目名称按 Unicode 码点截到 200 个字符（与后端同一口径），
   输入框里显示「n / 200」，到上限时提示「已达到 200 个字符上限」。

## 2026-11-16 · 吸收 develop !607、!608（drawio 文字标注、Studio 过期草稿与 Wiki 链接）

1. **Studio 草稿对正式条目**（!608）：ST-04、ST-07 GET 读草稿前先对一下条目当前内容——草稿内容已和条目一样就删掉；
   基于的修订变了就标 `Draft.state = "stale"`（内容保留），`status` 不算 `editing`，ST-03 的行状态同口径。
   ST-07 PUT 用旧 `base_revision` 存、ST-08 提交 stale 草稿都回 `409 revision_conflict`，`details.draft_state = "stale"`，
   提交在确定性检查和转助手复核之前就拒。版本回退的「有未提交草稿」（ADR-0094）、MCP 写入的「条目有草稿」都不算 stale 草稿。
   本分支的差异：草稿不占租约（develop 的租约申请、释放步骤没有对应），读写草稿与版本回退、MCP 写入同一把项目锁。
   前端：结构化编辑与编辑原文都不自动载入 stale 草稿，提示「上次草稿基于旧版本，未自动载入；请放弃旧草稿或基于当前版本重新编辑」，
   可放弃旧草稿；条目摘要标「旧版本草稿已失效」；自动暂存按顺序排队（排到时才读最新内容，失败的留到下次再存），
   放弃草稿、重读条目先等排着的暂存落地。develop「进入编辑前重读条目」「复核结果到了刷新详情」在本分支已有：选中条目即打开编辑器、
   挂载时读 ST-04，`workspace.changed` 时没有本地改动就重读。
2. **确定性检查只报改动过的模板外章节**（!608）：基线里已有、内容没变的模板外章节不再转复核，内容改了报「修改了模板外的章节」。
3. **对话里的条目链接**（!608）：`[[名称]]` 改在 Markdown 解析之后转成 `#wl:` 链接（`remarkWikiLinks`），代码块、行内代码里的不转，
   历史消息里被空白拆开的 `[文字] (#wl:…)`／`(#wiki:…)` 修复成链接。`[^n]` 角标照旧在解析前改写。
4. **drawio 文字标注**（!607）：本分支流程图由内核解析（`kernel/artifacts/flowchart.py`），在内核实现——样式 `text`、没有 `onto_type`
   的独立文字格子是文字标注，不当节点；带 `data-flowchart-annotation-edge` 的归到那条连线，其余在起点出度大于 1、还没有文字和条件的
   连线里找最近的（折线距离 ≤ 60 且比第二近的近 20 以上），归过去的连线文字与条件取标注文字、类型记为 `cond`。写回（AR-05 保存、
   AR-10）时标注文字跟着连线文字改、带上归属属性，连线本身不再写文字；连线删了或文字清空，标注一起去掉。
   本分支的差异：归不到连线的标注当普通批注原样保留，不像 develop 那样退回 drawio 编辑器（内核写回只改认得的单元格，不会丢）。
   前端 `drawioToModel`（mock 与降级预览）同口径。

## 2026-11-16 · 新官方能力包「领域包扩展」（ADR-0099）

1. **新官方能力包 `domain-extend`（领域包扩展）**：默认不开，插件中心能力包分类里多一张卡片。只用在从已发布知识包导入的项目上
   （HB-04、P-11）：启用后主线是建模 → 锁定底座 → 新增功能 → 底座核对 → 校验 → …… → 发布；底座核对没通过时 PB-01 多一条
   `flow:domain-extend.check` 门禁挡发布。产物在工作区 `ext/domain-extend/` 下。
2. 底座 = 导入提交里 `ontology/` 下的条目，助手写入经内核 `confine` 拒绝；人工改动在核对报告里是待处理项，不能标记跳过，只能撤回。
   发布后底座锁定自动解除（ADR-0091），「锁定底座」变回待做，重新锁定前发布被挡。
3. 和 `incremental` 都声明 `confine`，两个包不能同时启用：启用第二个时 PL 启用接口返回 `422 flow_assembly_failed`。

## 2026-11-17 · 陪伴建模切自主构建的交接（实测修复）

1. **接管打断**（EC-05）：`by` 多一个 `"autobuild"`，`position_label`「开始了自主构建，「对象提取」交给它接着做」，`saved` 为 null
   （这一轮没做完的随后才合入，原来写的「已保存到」是合入前的旧版本）；不推「从打断处继续」的建议条。
2. **撤回的问题有去向**（AB-04）：接管时没回答的决策卡按「拿不准时」的策略定下来，记进运行台账（`reason_text` 以
   「开始自主构建时没回答的问题：」开头、待复核）或挂起；运行每一步的注入带着它（挂起的写明跳过、不要再问）。
3. **没做完的执行交给运行收尾**（AB-04、IN-01 `resumable`）：别的对话里没做完的执行，运行要做或开始时已经做完的记为放弃、
   清掉命令锁，待处理不再列续跑，建议条不再推「继续」，那条对话里留一行交给了哪次运行；范围外又没做完的照旧能续跑。
   被接管时停在干活中途（不是在等回答）的建模那一步，运行照做、接着做完（写了一半的条目按「条目在」会算做完）；
   执行记录的阶段按打断那一刻的命令锁记。
4. **交接说明**（宿主注入）：建在某次自主构建之前的对话，注入里带「运行 #n（从 vX 开始）改过主线：新增…；修改…；之前说到的
   条目、数量和状态以现在的主线为准」，和阶段提示一起按摘要去重。运行自己的对话、之后才建的对话不带。
5. **预览提示蓝图里没回答的问题**（AB-03 `warnings`）：蓝图的问题按 BP-03 不代答、留在待处理，开始前说清楚。
6. **待处理不列已交给核对节点的过期**（IN-01 `stale`）：和建议条第 7 条用同一判定（`flow_state.handed_down`，ADR-0090），
   校验已确认、它的修复让建模或指标过期时不列；校验没跑过、跳过时照列。节点上的「过期」不变。
7. **运行顺带写出的步骤、节点记需复核**：自主构建合入时，改到了别的步骤的条目（操作提取一起抽的规则）、别的节点的产物
   （按 `produces`，如一起抽的指标）且已满足完成判定的，也记为自主完成、待复核，不再按「内容在、没有记录」记成已确认。
8. **报告待处理只认列表项开头的 `[ ]`**（流程探针 `pending`）：原来在报告全文里找 `[ ]`，处理过的条目说明里写着「补 `[[ ]]` 包裹」
   也算没处理，校验一直不算做完，建模的过期交不出去（实测：自主构建跑完，建议条推「刷新操作」）。和待处理计数、注入、报告解析
   同一口径；内核按平台这份算，锁定旧版本的项目也一样。

## 2026-11-17 · 发布页、版本历史页改版

用户实测：发布页逻辑乱、大量空白、报错难懂、页头露出项目 ID，版本历史页说明里有内部标记、选中版本和右侧操作对不上。

1. **发布不做重复关系检查**（用户定）：对象模板（develop 同一份）要求同一条关系在两端对象里各写一次，develop !594 起发布导出用严格模式，
   把两端声明判为重复并拒绝，按模板建的模型都发不出去。改为与默认口径一致：两端写法相同（同方向、同关系名、同一对字段）的归并成一条，
   同一端写两遍也归并；缺端点、显式关联字段不在属性清单里照旧拒绝。与 develop 不一致，需同步给 develop。
2. **关系结构问题逐条给出**：`relation_structure_invalid` 原来是一整段拼接的消息（最多 20 处、带路径和行号）；发布检查改为每处一条
   Finding（`path`、`line`、`target` 对象，`message` 不含位置），知识包结构门禁的 summary 改为「N 处对象关系写法有误」或单处时
   「「对象」问题」。发布接口 422 的消息不变，`details.problems` 带同样的结构化条目（最多 50 条）。
3. **数据绑定不再说「都已绑定」**：没有问题、但有对象既没绑表也没声明不落库时，门禁 `status=warn`，summary 如实写个数（不挡发布）。
   `binding.objects` 内部条目加 `declared_none`。
4. **版本说明（V-01、V-09 的 summary）**：再去掉 `assistant:`、`plugin:` 前缀、末尾的变更集编号（`（cs-N）`）与「by 用户」，
   「转换资料 资料「X」」不再重复类别。提交信息本身不变。
5. 前端：发布页改成「发布卡（结论 + 发布 + 检查项）→ 需要处理（每个没通过或有提醒的检查项一张卡）→ 已发布版本（HB-05 与 PB-04 合一张表，
   表头带共享与版本对比）」，导出迁移包、旧版评测记录、重新检查收进「⋯」；「发布」按钮以门禁 blocking 为准（原来渲染时发现的结构问题
   不计入，按钮可点、点了才 422）。版本历史页左栏每行只留版本号、来源、时间、一句说明，标记、回退、查看文件收进「⋯」；右栏是选中版本的
   详情与「改了什么」，对比默认看这一版改了什么（上一版 → 这一版），可切到「这一版 → 当前」。

## 2026-11-17 · 子任务结果换成说法

用户实测：规则检查子任务的卡片、面板「过程」和「结果」都显示 `[]`。检查、修复、影响分析等子代理按约定只交回 JSON 给主助手解析，
平台原样当成了小结，最后一句又记成了一步。

1. **小结（EC-22、S-21 `summary`）**：子代理交回的是 JSON 时换成说法，第一行是结论，其后每行「· 」开头一条明细（最多 5 条，
   多的写「…另有 N 条」，路径换成业务名）。按条目字段认：带 `severity` 的是问题（「发现 3 个问题：错误 1 · 警告 2」，`[]` 为
   「没有发现问题」）、带 `applied` 的是修复（「改好 2 条，1 条没改」）、`operation` 为 create/modify/delete 的是影响分析、
   带 `verdict` 的是脚本候选、带 `scenario` 的是场景推演、带 `rules`/`metrics` 的对象是规则指标抽取；认不出、截断了解析不了的为 null
   （界面用「已完成，结果交回给助手」）。引擎拿到完整结果时换（截断前），读历史时再换一次，已存的旧记录同样生效；不是 JSON 的不变。
2. **步骤（EC-22 `step`、S-21 `items`）**：子代理交回的 JSON 原文不再记成说的话，旧历史里的读出时跳过。
3. 前端：子任务卡第二行只放结论，面板「结果」下面逐条列明细。

## 2026-11-17 · 语义检查拆成三部分逐项记录，校验过期后刷新只查改动（ADR-0100）

用户实测：一个 9 份文档的小项目，语义检查子任务一次思考用满 32000 输出 token 被截断，又撞上 720 秒超时，12 分钟没有交回
任何结果；这一轮刷新校验只因为改了 9 个文件，就把全部文档重查了一遍。

1. **语义检查（工作区资产）**：全量模式拆成操作类、对象类、跨文档三个子任务，在同一条消息里派出（Claude 并行，Jiuwen
   依次执行）；增量模式按目标里有无操作、对象决定派哪几部分；只查改动和回归模式不拆。子任务每查完一项就用
   `.claude/scripts/semantic_issues.py` 记一次（`init`／`add`／`done`／`status`／`merge`，问题存在 `.ontology-state/semantic/`），
   只回一句话，不再返回 JSON。主命令按 `status` 补派缺项一次，还缺的写进报告「未完成的检查项」并在小结里说明。
   规范仍是 `semantic-check-agent.md` 一份，重排成公共部分加三节，检查细则不变；类别取值补上「业务知识冲突」。
2. **子任务卡片（EC-22 `title`、`step`）**：语义检查三部分的标题为「语义检查 · 操作」「语义检查 · 对象」「语义检查 · 跨文档」；
   子任务记录问题、标做完的那一步显示为「记录「X」的问题」「查完「X」」，主命令的 `init`／`status`／`merge` 不显示。
3. **流程定义**：节点新字段 `refresh_scope: changed_entries`（目前只有 `core.validate` 声明）；取值不认识，或者按段算的节点
   声明它，装配会拒绝。
4. **节点状态记录**：确认时多记 `rev`（主线提交号）；老记录没有时按确认时刻沿主线找。
5. **刷新命令（FL-01 `suggestions`、`nodes[].actions` 的 refresh 动作、FL-05、IN 的 `refresh`）**：声明了 `refresh_scope` 的节点
   过期、且算得出范围时，命令为 `<入口命令> <条目名…> --since <确认时提交前 12 位>`，走只查改动模式；`flow-state.json` 的
   上下文多一个 `stale_scope`。算不出就照旧发整条入口命令：有删除、改到条目目录以外、只改了 index.md、找不到起点、条目名会被
   当成参数、改动超过 100 个、节点升级后待复核。「运行质量检查」等原有动作不变（全量）；待处理里的「重做这一步」（`rejudge`）
   整步重做。
6. 发布前不加全量校验门禁（用户定）。


## 2026-11-17 · 去掉六个官方能力包与建模步骤抽屉（ADR-0101）

用户定：删除系统逆向建模（rev）、反馈演进（evo）、知识沉淀（knowledge）、联网调研（websearch）、迁移复用（migrate）、增量构建（incremental）
六个官方能力包，工作台去掉「建模步骤」抽屉。`api-v1.md` 已同步：删除的接口标「（已删除，ADR-0101）」、正文压成一句，3.19、7.13 只留组标题和说明。
删除的 v1 路径回 `404 not_found`（同一路径还有别的方法时回 405）；旧路径兼容里原来改写到 K-03、K-05 的两条改为回 `410 endpoint_removed`（第 6 条）。

**删除的接口**

1. **EV-01～EV-28、EV-30～EV-34**（3.19）：数据源、凭据、测试连接、访问审计、提供方、项目授权、采集任务、快照、证据检索、ER 视图、
   通知管理员、目标程序分块上传，随系统逆向建模包删除；连接器网关的 `/v1/jobs*`、`/v1/secrets/*`、`/v1/sources/*/test` 一并删除。
   **EV-29 保留**：资料出处悬停看原文要用，只解析 `file:` 锚点，其余 scheme 回 `resolved=false`、`reason=not_available`，不再记专项审计。
2. **AD-08～AD-10**（管理员申请审核）：只服务数据源登记申请和知识提名，两者都删了。
3. **PL-04、PL-19**（申请平台安装、平台安装提供方）：一直只回 501，连接型提供方删除后没有要装的东西。
4. **ES-07、ES-08、ES-12**（评测优化、标记处理失败、旧报告评测优化）：产出只交给反馈演进，随反馈演进包删除；发布后不再回写评测服务。
   评测入口、完成通知、报告查看（ES-00～ES-06、ES-09～ES-11）保留。
5. **R-11、R-12**（外部反馈列表、标记状态）：随反馈演进包删除；工作区已有的 `feedback/*.json` 不迁移、不删。
6. **K-03～K-08**（新增、修改、删除、冲突检查、提名预览、提交提名）：随知识沉淀包删除。**K-01、K-02 保留**、只读：
   知识条目的存放、加载和行业层在 core，条目由助手经工具箱 `knowledge_helper` 写入。旧路径 `POST`、`DELETE /api/projects/{id}/knowledge`
   回 `410 endpoint_removed`，`successor` 为 null（6.1）。
7. **IN-03 的 `send_to_evolution`、`arbitrate`**：去向（反馈演进命令、证据冲突裁决）都没有了。
8. **MCP evidence 工具与 scope `evidence.read`**（M-03、M-08，P-14 内置 `evidence` 服务器）：依赖证据检索，契约里一并删除。
9. **迁移复用（migrate）**：只删包资产，格式迁移的 `migrate` 待处理动作与它无关，保留。
10. **增量构建（incremental）**：只删包资产，内核的激活与范围限定保留；内核增量更新（`core.update`、INC-01～INC-05）不变。

**删除的字段与事件**

11. **联网调研**：P-05 请求、`ProjectMeta` 的 `research_mode`，`MaterialMap` 的 `analysis_mode`、`web_available`，R-07 的分析范围；
    资料分析只读本地资料。P-05 带 `research_mode` 时忽略，不再回 `pack_not_enabled`。
12. **反馈演进**：R-01 `feedback_group`；ES-02 `writeback`、`evolution`（`reconcile=1` 不再补写回写）；ES-09 `evolution`、`evolution_enabled`；
    PB-03 `evaluation_writeback`；`ReleaseRecord.feedback_count`；待处理 kind 与筛选 `feedback`（IN-01 `counts.by_filter`、IN-02 detail、
    `InboxItem.source` 的 `external`）；类型 `Feedback`；卡片 `external_feedback`。
13. **知识沉淀**：EP-23 `knowledge.updated`；内置页面 `knowledge`（左栏内核导航不再有「知识沉淀」）；卡片 `knowledge_saved`；类型 `ConflictFinding`。
14. **证据采集**：C-01 `features.evidence`（契约里原本没写）；EP-13 `evidence.job.updated`；Question kind `collection`；`upload_prompt` 卡片的
    `sources_available` 与动作 `connect_system`，P-02 `starting_points` 的 `connect_system`；`PackCard.collector`，PL-01 `project_status`
    的 `platform_available`；PL-12 `entries` 的 `source_wizard`，`view_choices` 只剩 `er_view`；`FlowNode.kind` 的 `external`，流程条件
    `evidence_sources`、`evidence_snapshot`（不认识的条件一律不成立）；P-06、P-08、P-09 里的证据快照、`include_snapshots`、数据源授权与登记；
    P-10 `evidence_sources`；ST-04 的 `evidence_snapshots`、`counts.evidence`（每个属性的 `evidence` 占位保留）；
    ST-06 `snapshot_basis` 恒为空、只给 `file:` 锚点、不记审计；`AnchorResolution` 的 `reason` 改为
    `invalid|file_missing|not_found|unreadable|not_available`，`page` 不再有 `schema`，`snapshot_id` 恒为 null；7.13 的全部类型。
15. **证据冲突**：待处理 kind 与筛选 `evidence_conflict`、动作 `arbitrate`、类型 `ConflictDetail`；IN-01 带 `filter=evidence_conflict` 回
    `400 invalid_argument`（`fields.filter=enum`）。来源（三方对账）随证据采集没有了。
16. **子页**：内核内置子页 `BUILTIN_SUBPAGES` 清空（原来是资料页的 sources、snapshots、schema），包声明子页时静态检查不通过；子页机制保留。
17. **自主构建**：AB-01、AB-02 的 `production_collection`，SY-17 与 AB-01 `platform_limits` 的 `allow_production_collection`，AB-03 预览的
    生产库授权，`Suspension.kind` 的 `unauthorized_collection`。请求里带了照样忽略，老文件里的不再读、下次保存时清掉；AB-01
    `platform_limits` 为 `{}`、`not_enforced` 为 `[]`。
18. **通知 kind**：`evidence_conflict`、`nomination_reviewed`、`request_pending`、`request_reviewed`。
19. **错误码**：2.8 除 `anchor_not_found`（PB-02 设数据库锚点时一律返回）外全部删除；2.7 `knowledge_duplicate`、`nomination_not_found`；
    2.9 `feedback_invalid`、`artifacts_changed`、`version_mismatch`、`optimization_not_supported`、`note_required`；AB-02 不再列 `source_not_authorized`。
20. **配额**：SY-15 的 `collections_per_source_per_hour`、`snapshot_git_mb`，PUT 带了回 `400 invalid_argument`（`fields` 记 `unknown`）；
    SY-16 单独设置的 `item` 取这两项回 `400`（`fields.item=enum`）；`not_enforced` 为 `[]`。老配额文件里的这两项不读，下次保存时去掉。
21. **配置与运维**：配置项 `ONTO_WEB_EVIDENCE_MAX_ARTIFACT_MB`、`ONTO_WEB_EVIDENCE_SNAPSHOT_GIT_MB`；O-40 任务类型 `collection`（带了回 400）；
    O-41 的 `resume` 操作（回 `404 not_found`）；O-48、O-49 审计类型 `evidence`（老记录照原样显示，按它筛选回 400）；O-50 错误来源
    `collection`；O-02 `collections` 指标、O-42 `evidence-snapshot`、O-43 `snapshots` 保留项（代码里原本没有）。

**新增（下架状态）**

22. **启动时下架镜像里已移除的官方包**（`builtin.retire_missing`）：全部版本都由镜像入库、没有 owner、不是 core，这次 `assets/packs` 里
    没有它，就在通道记录上记 `retired: {at, by, reason}`；`assets/packs` 整个不存在时不下架。原因：老环境的包仓库和锁文件里还有这些包，
    不下架的话插件中心照样列出，启用过的项目装配时缺资产。
23. **下架的效果**：PL-01、PL-02、新建项目的缺省启用、跟随通道、O-25 插件运维都不列；PL-06 启用、试装配回 `pack_not_found`；
    `runtime.normalize` 把它从项目锁文件去掉，作为一次系统提交，项目照常装配。版本目录不删。
24. **取消下架**：换回带这个包的镜像入库时，或上传发布同名新版本时；通道指针不变，已去掉的锁条目不加回来，要用的项目重新启用。
25. 下架是包级的通道记录，不出现在接口字段里；和 O-25 版本行的 `channel="retired"`（这个版本不在任何通道上）不是一回事。

**前端**

26. **建模步骤抽屉**：顶栏按钮和抽屉删除，进度条只展示、不能点开。FL-03、FL-04 后端保留、前端不再调用；FL-01 `sections` 照常返回、
    前端不再用；FL-05 留给建议条、发布页「去运行」、待处理「刷新」。S-20 `display_label` 不再有「建模步骤」来源。
27. **随包删除的页面与入口**：资料子页 sources、snapshots、schema，知识沉淀页 knowledge；新建向导、上传卡、⌘K 的「连接业务系统」；
    资料区外部反馈分组；资料分析的联网 / 仅本地切换；发布页评测优化与标记处理失败；待处理的外部反馈、证据冲突筛选；对话里的采集确认、沉淀入库卡片。


## 2026-11-17 · 建模拆成提取对象、提取操作，去掉分段（节点模型重构 1）

用户定：建模拆成两个节点，分段遗留（按段的状态、步骤、写入范围、建议）整个去掉，「规则」一步删掉（和指标旁支是同一条命令）。
这一批不动 `placement`、`section`、`optional` 等旧字段，不改名、不改菜单和推荐规则；自主构建的改写在下一批（1B），这一批留了
兼容层让它照常跑。

**节点编号与别名**

1. core 的建模节点 `core.model` 拆成 `core.object`「提取对象」（`after: core.research`，`done: [object]`，`stale_on: [research]`，
   `produces: ontology/object/*`）和 `core.operation`「提取操作」（`after: core.object`，`done: [operation]`，`stale_on: [object]`，
   `produces: ontology/operation/*、ontology/rule/*`，`writes: ontology/logic/*、ontology/metric/*、ontology/object/*`）。
   指标、校验、增量更新的锚点改成 `core.operation`，增量更新排到提取操作后面。
2. 别名倒过来：`core.model → core.operation`（原来是 `core.object`、`core.operation → core.model`），`core.rule_metric → core.metric`、
   `core.consistency → core.validate` 不变。没升级的包写的 `core.model` 锚点、钩子宿主、命令归属照样认；官方包（代码生成的求解适配、
   领域包扩展的锁定底座）的锚点改成 `core.operation`。
3. 命令归属：`extract-object → core.object`、`extract-operation → core.operation`、`extract-rule-metric → core.metric`。
4. 写入范围随节点收窄：提取对象只能写 `ontology/object/*`；提取操作能写操作、规则、逻辑、指标、对象。
5. 节点字段 `per_slice`、`steps` 去掉；包的 `slice_steps`、`node_titles` 和节点上的 `per_slice`、`steps` 装配时忽略（老版本的包、
   锁定旧 core 的项目照样能装配，不报错）。`refresh_scope` 不再因为 `per_slice` 拒绝。`blocks_in: sliced` 的关卡一律不挡。

**FL-01 等接口（只加不删）**

6. FL-01 `slice_stages`、`slices` 恒为 `[]`；`nodes[].per_slice` 恒为 `false`、`progress` 恒为 null。EP-01 `flow.updated` 的 `slices`
   恒为 `[]`。P-01 `ProjectCard.slices` 恒为 null。CS-06／EP-03 `ScopeLock.slice` 恒为 null，原因文案不再说「这一段」。
7. FL-03、FL-04、FL-05：请求里的 `slice_id`、`step` 忽略，不报错；`node_id` 是旧编号时按别名认（`core.model` 认成提取操作，
   必经，不能跳过；开始发 `/ontology-extract-operation`，不带分段）。FL-03 记的跳过不再带 `slice_id`。
8. 建议条：建模不再按段推，提取对象、提取操作各自按节点推（主推是节点的动作，「提取对象」「提取操作」）；过期按钮写节点名
   （「对象有变化，刷新提取操作」）。`flow_state.SUGGEST_RULES` 升到 8，`flow-state.json` 的上下文去掉 `slice_next`、`slice_stale`；
   锁定旧 core 的项目（`SUGGEST_RULES` < 8）建议条和待处理的「已交给核对节点」判定都用平台镜像的 flow_state。

**兼容规则**

9. **节点状态记录迁移**（`state.migrate_split`，第一次按拆开的流程算状态或记确认时做，只做一次；流程里还有 `core.model` 的不做），
   顺序不能乱：① 删掉 `nodes` 里残留的 0.2 时代 `core.object`、`core.operation` 记录；② 提取对象取 `slices.all.object`，没有就取
   `core.model`；③ 提取操作取 `slices.all.operation`；④ 需复核标记、按类指纹、`rev` 一起带过来，判定签名不带旧的、按新节点记；
   只有需复核没有基线的不搬；⑤ `slice_skips.all.rule` 转成跳过指标（指标已经跳过的保留原记录）；⑥ 删掉 `core.model`、`slices`、
   `slice_skips`、`legacy_migrated`，记 `split_migrated: true`。
10. **执行记录**：新增 `exec_records.node_of`。老记录的 `core.model` 按 `step` 认（对象 → 提取对象，操作 → 提取操作，规则 → 指标），
    没有 `step` 时按命令名，再认不出按别名算提取操作；流程里还有 `core.model` 时原样。进行中、续跑前的检查都按它。`slice=` 参数
    不再起作用；续跑（按新版重做）的命令不再拼 `slice=`；工作区 `.onto/run/exec.json` 不再写 `slice`、`step`。
11. **变更集**：不再记 `slice_steps`，一轮结束不再把新条目记进蓝图的段；CS-05 撤回不再改蓝图、不再清分段记录。合入时节点列表里的
    `core.model` 展开成新节点：有 `slice_steps` 时按做过的步骤（同第 10 条），没有时提取对象、提取操作都算。写入范围锁按别名认
    `core.model`（提取操作的范围盖住原来建模的）。
12. **守卫**：`.onto/run/active.json` 里分段时代记的 `protect`（段外已有的条目）不再看；`state_helper acquire` 不再记 `slice`、`protect`。
13. **助手副本**：`state_helper suggest` 读到的 `flow-state.json` 里缺的节点（core 刚升级、后端还没按新流程算过）按副本里的完成判定
    兜底：做完了算已确认，没做完时上游做完了算可开始，不会把已经有对象的项目推回「提取对象」。
14. **自主构建（批次 1B 删除的兼容层）**：`flow/slices.py` 只剩自主构建还在调的接口，把两个节点包成一段「全部」× 两步（对象、操作），
    每一步的状态、确认记录落在各自的节点上；`blueprint.run_slices`、`drop_added_members` 保留；提取对象、提取操作的执行记录照旧记
    `slice`（「全部」）与 `step`。AB-01 等自主构建接口不变，策略的 `until_node` 要写 `core.operation`（这一批还不认别名）。

## 2026-11-17 · 自主构建按节点排（节点模型重构 1B）

用户定：自主构建不再按「段 × 步骤」排单元。这一批把 1A 留下的分段兼容层删掉；哪些节点能自主做沿用原来的规则（批次 3 再改成按
`need` 判）。契约里 `slice`、`slice_id`、`slices` 这些字段名、AB-17 的路径、`slice_progress` 卡片类型、`slice_not_revertible` 等错误码
都不变，装的是单元：新运行是节点编号与节点名，按段跑的老运行照旧是段。

**单元与执行**

1. 一个节点一个单元、一步：资料分析之后、到策略的 `until_node` 为止（含）的节点，按流程顺序，跑节点的主命令（`entry.command`）。
   能做的节点：主线（`placement: spine`）、不是人工或面板节点、没写 `autobuild: false`、有主命令；可选的只在 `include_optional` 时做。
   已确认、已跳过的不做；过期的按 `refresh_stale_first`（原来主线上的几步过期了总是重做，现在和建模一样听这个开关）。做没做完按
   确认记录与完成判定算，不叠副本里的命令锁。
2. 每个单元新开上下文、先占一个变更集、合入后是安全点：提取对象、提取操作各合入一次（原来「全部」一段合入一次）。暂停、停在
   安全点、转为陪伴都在当前这一步合入之后生效（原来暂停停在段内一步之后，变更集开着、写入范围锁着）。合入提交说明是
   `autobuild: 自主构建运行 #n「提取对象」（cs-…）`。
3. 注入：开头是「自主构建 · 运行 #n · <节点名>（第几步/共几步）」；去掉段说明；「只改这一段的内容」改成「只做这一步」；合入前
   修检查问题的提示写「只改「<节点名>」这一步写的内容」。
4. 锁定旧 core 的项目（流程里还是 `core.model`，主命令只提取对象）：这个单元照原来做两步（提取对象、提取操作）。

**接口**

5. AB-01、AB-02：`scope.until_node` 写旧节点编号时按 core 的别名认，读出来、保存后都是现在的编号（`core.model` → `core.operation`；
   原来开始运行回 400）；`core.consistency` 照旧当成跑完主线。`scope.slice_ids` 原样保存、不再起作用（原来只留现存的段）；
   `mode: "selected"` 存成 `all_pending`；`generate_scenarios` 保存、不再起作用。
6. AB-03 `slices`（键名不变）：一个节点一行 `{id: 节点, name: 节点名, current, included}`，`current` 是「已确认」「有过期」「已跳过」
   「进行中」「待确认」「未开始」之一；原来只有「全部」一行。
7. `AutobuildRun`（AB-04～AB-09）与 `AutobuildRunSummary`（AB-18、EP-14、P-01 `autobuild.slice_done/slice_total`、O-01）：
   - `slices: {done, total}` 按全部单元算（原来只数段，校验等节点不算）；
   - `current.slice_id/slice_name` 是节点编号、节点名，`step_label` 也是节点名；
   - `matrix` 每个单元一行 `{slice_id, slice_name, state, cells, result}`，新加 `state`（`queued|running|done|partial|skipped`），
     节点单元的 `cells` 为 `{}`；
   - 新加 `stages: [{id, label}]`：按段的老运行的步骤列（对象、操作……），新运行为 `[]`；
   - `tail_text` 恒为 `""`（节点单元都在 `matrix` 里，原来写「跑完全部分段后：校验 → …」）。
8. AB-16 `AutobuildReport`：`slices` 每个单元一行（原来只有段），`metrics.slices`、`metrics.produced` 按全部单元算；节点单元合入过
   就能撤回（`revertible`）。
9. AB-17（路径 `/runs/{run_id}/slices/{单元}/revert` 不变）：撤回一个单元，节点单元也行（原来只有段）；这一步的节点的确认记录放回
   运行前的；建模蓝图不跟着撤。错误码、专项审计 `autobuild.revert_slice` 不变。必须倒序撤回（用户定）：同一次运行里更靠后、用到了
   这一步产出（它的节点的 `stale_on` 盖住这一步节点的产出类别，同流程过期的口径）、还没撤回的单元在，回 `409 slice_not_revertible`，
   `message`「先撤回「提取操作」」，`details: {reason: "revert_later_first", slice_id, slice_name}` 是最靠后的那个；它撤回后这一步就能撤。
   没用到的不挡（撤校验不看后面的场景模拟）。老运行按段的单元整体撤回，不受这条限制。
10. AB-10～AB-15：`LedgerEntry.slice`、`Suspension.slice` 是单元 `{id, name}`；复核项 `summary` 以节点名开头（「提取操作：三级」）。
    待处理里自主构建复核项的影响写「步骤：<节点名>」。改判、补上发那一步的命令，不再带 `slice=` 参数。
11. 卡片：`autobuild_start` 的「范围」写「N 步」；`slice_progress` 每个单元一张；EP-15 `autobuild.safe_point` 每个单元合入后发一次。
12. 「需复核」：一个单元里的自动决定都复核过（没有决定的单元，要整次运行都复核过）就去掉它合入时标在节点上的；整次运行都复核完时
    去掉这次运行标的全部。原来按段清段内记录，节点上的标记要等整次运行复核完。
13. 执行记录：不再记 `slice`、`step`；ExecRecord 的 `slice` 新记录为 null，老记录照读。
14. 变更集检查：按段的「待建链接」（`pending_links`、`pending`）去掉，链接找不到一律算问题；`round_changes` 卡的 `checks.pending` 恒为 0，
    提示不再有「N 处指向第 X 段待建的内容」（老变更集记着的也不再提示）。

**老运行兼容**（运行记录不迁移，按记录里的单元原样认；运行记录里各单元的进度仍存在 `slices` 键下，回退到上一版也能读新运行）

15. 详情（AB-05）：`stages` 从按段单元的步骤和开始时记下的各步状态得出；按段的一行按步骤填格子，跟在后面的节点单元一行、没有
    格子；开始时就做完的段照旧一行「运行前已完成」。
16. 报告、台账、挂起、待处理照旧按 `slice` 认单元；复核项的「全部」写「建模」，按好几段跑的才说「分段」「N 段」。
17. 撤回（`slices/all/revert`）：按步骤记的运行前确认记录（`object`、`operation`）落到提取对象、提取操作上（同节点状态迁移：带指纹、
    需复核标记，判定签名按现在的节点记）；「规则」一步的记录不放回。原来会去掉这一段合入时记进建模蓝图的条目，现在蓝图不动。
18. 暂停中的老运行续跑：接着做这一段没做完的步骤（不重做做完的），再做后面的节点单元。
19. 留到下一次的挂起问题按那一步落到节点（对象 → 提取对象，操作 → 提取操作，不知道哪一步时按这一段的第一步），下一次运行在那个
    节点上补上。
20. 策略里的 `slice_ids` 只保存不使用（第 5 条）。

**删除**

21. `flow/slices.py` 整个删除；`blueprint.run_slices`、`drop_added_members`，`revert_merged` 的 `rewrite` 参数删除；变更集检查的
    待建链接（`gates._planned`、`link_target`）删除；`exec_records` 不再写 `slice`、`step`。

## 2026-11-17 · 节点挂法收成 need 一个字段（节点模型重构 2a）

用户定：节点什么时候要做只用一个字段 `need` 表达。这一批换声明方式，界面行为基本不变：改名、全部命令菜单、推荐五级、跳过入口、
前端交互在 2b；增量更新的事件条件、自主构建按 `need` 判资格在批次 3。

**声明**

1. `need` 三种取值：`required`（上导航栏，计入进度，没做时推荐，自主构建做）、`optional`（不上导航栏，挂在它前面最近的必需节点下面，
   可以跳过）、`{when: <内核条件>}`（条件成立时出现）。缺省 `required`。取值不对装配失败（「节点 X 的 need 只能是……」）。
2. 旧写法装配时折算成 `need`，自上而下取第一条命中的：

   | 旧写法 | 折算成 |
   |---|---|
   | `visible_if: X` | `{when: X}`，`preempt` 不带过来 |
   | `placement` 或 `section` 是 `on_demand` | `optional`，没写 `autobuild` 时加 `autobuild: false` |
   | `optional: true`、`section: optional`、`placement: branch`、`progress: false` | `optional` |
   | `gate`、`pre_publish` | `required` |
   | 其余（`spine`、`step`、什么都没写） | `required` |

3. 同时写了 `need` 和旧字段时以 `need` 为准。旧字段不删，装配时按 `need` 补齐（锁定旧 core 的项目、助手副本里的旧脚本还读）：
   - `required`：`placement` 是 spine、step、gate 的保留，否则 spine；`section` 是 required、pre_publish 的保留，否则 required；
     去掉 `optional`、`progress`、`visible_if`、`preempt`；
   - `optional`：`placement` 是 branch、on_demand 的保留，否则 branch；`section` 是 optional、on_demand 的保留，否则 optional；
     `optional: true`、`progress: false`，去掉 `visible_if`、`preempt`；
   - `{when: X}`：`visible_if: X`、`progress: false`，`preempt` 没写时补 `true`（旧脚本按它先推），去掉 `optional`；
     `placement`、`section` 不合法时补 spine、required。

   整理是幂等的：装配目录里的流程再整理一遍不变。
4. 分段遗留的 `per_slice`、`steps`、`slice_steps`、`node_titles`、`blocks_in` 照旧不起作用（`blocks_in: sliced` 的关卡不挡），静态检查给警告。
5. **主命令**：`actions` 里第一个带 `command` 或 `page` 的动作。`entry` 只做兼容：没写时装配补成主命令；只写了 `entry`、没有这样的动作的
   老节点，装配时把 `entry` 补成第一个动作（叫法取节点标题）。内核不再读 `entry`：开始节点（FL-05）、范围限定的
   `confine.entry`、自主构建、建议条和待处理的刷新、S-18 命令归组、节点钩子提示、静态检查都改读主命令。和原来不同的两处：
   资料分析发 `/ontology-research 仅基于本地文档`（原来 `/ontology-research`，命令说明里这句话不改变做法）；场景模拟发
   `/ontology-trace 全部`（原来 `/ontology-trace`，会先问做哪种；节点卡片上的刷新原来就发这一条）。
6. **第三方包不进自主构建**：不是镜像内置入库、也不是 core 的包，节点没写 `autobuild` 时装配成 `autobuild: false`（自主构建无人值守，
   第三方包的输入不可信）；要进写 `autobuild: true`，上传检查列出来。可选节点同样处理（批次 3 打开「包括可选步骤」时也不做）。
7. **`display_after` 保留**：求解适配改成 `after: codegen.codegen` 的话，没做代码生成时要等场景模拟做完才能开始（原来等提取操作）；
   底座核对改成挂在新增功能后面，没新增时核对不了。两处「可开始」都会变，所以这两个节点继续用 `display_after` 只管显示顺序，
   锁定底座、新增功能的 `display_after` 和 `after` 相同，去掉。
8. **触发**：`activation` 的节点写 `need: {when: activated}`（旧写法 `visible_if: activated` 照认）；`preempt` 不再校验、不再起作用。
   条件节点出现且没做完时，建议条先推它（原来要写 `preempt`）；说明行不再取 `preempt` 的字符串，为空。
9. 装配格式 `ASSEMBLY_FORMAT` 升到 2：装配哈希跟着变，所有项目启动核对时重新装配。读装配目录的流程时再整理一遍（还没重新装配的
   老格式也有 `need`）。`flow.lock` 的节点只记 `need`，去掉 `placement`、`section`、`optional`（下次提交锁文件时改写一次）。

**资产**

10. core 升到 0.6.0，主流程节点改用 `need`，去掉 `placement`、`section`、`optional`、`progress`、`entry`：资料分析、提取对象、提取操作、
    校验、发布是 `required`，指标是 `optional`；增量更新是 `{when: materials_added}`，这个条件内核还没有，按不成立算，**节点不出现**：
    FL-01 里没有增量更新，`/ontology-update` 在 S-18 里没有节点、归到「其他」，手动输入照样能发。
11. 官方包改用 `need`，`depends` 改成 `core: ">=0.6"`：trace 0.11.0（场景模拟 `required`）、diagram 0.7.0（流程图 `optional`）、
    codegen 0.10.0（代码生成、求解适配 `optional`，代码生成加 `autobuild: false`）、domain-extend 0.2.0（三个节点 `required`）。

**接口（只加不删）**

12. FL-01 节点加 `need`、`nav`（必需节点为 true，已停用的为 false）、`under`（可选、条件节点挂在哪个必需节点下面：after 链上它前面
    最近的必需节点，链上没有时取流程顺序里它前面最近的；必需节点为 null）。`section` 按 `need` 算（必需 required、可选 optional、
    条件 on_demand，`pre_publish` 不再出现）；`optional` 按 `need`；`placement` 是补齐后的旧字段。
13. **进度只算必需节点**（可补做的做完之前不算）：FL-01 `progress` 和 P-01 项目卡片的 x/N 由内核按 `need` 同一口径算，锁定旧 core
    的项目也一样。默认几个包启用时分母从 10 变成 6（指标、流程图、代码生成、求解适配不再算）。
14. FL-03 只能跳过 `need: optional` 的节点，必需、条件节点回 `409 node_not_skippable`（「只有可选节点能跳过」）。
15. PL-15 等上传检查：`node_entry` 改查主命令是本包的命令；新增 `node_need`（`need` 写错失败，分段遗留字段警告，没问题时不出现）、
    `autobuild`（上传的包有节点时出现：写了 `autobuild: true` 的节点列在说明里、结果为警告，没有时通过）；节点包的 `node_pack`
    不再限制 `placement`（第三方包也能声明必需节点），锚点仍要写（旧写法的按需节点除外），agent 节点的主命令要是本包命令；
    `activation` 不再查 `preempt`。插件开发 skill 的离线检查器带上 `services/flow/need.py`。
16. 工作台插件开发的草稿内容（`contents.nodes`）加 `need`。
17. S-18 命令按节点的 `need` 归组（必需「建模」、可选「可选能力」、条件「有需要时」），节点包的命令按主命令认节点。

**助手副本（flow_state、state_helper）**

18. `flow_state` 加 `need_of`、`need_kind`、`primary_command`（映射表和内核同一份，测试对齐）；`progress_nodes`、`_required_on_mainline`、
    已交给核对节点的判定、过期刷新、主线下一步、`state_helper` 补缺的节点状态改读 `need`；过期刷新发主命令。`SUGGEST_RULES` 不变。

## 2026-11-17 · 名字统一、全部命令按节点分组、推荐五级（节点模型重构 2b）

这一批只改后端和资产；导航栏下拉、跳过入口、全部命令菜单和 ⌘K 的界面等交互方案确认后再做（见下面「2b 前端」一节）。前端只跟着新的响应形状：菜单和 ⌘K
发条目的 `target`，增量更新页不再自己顶一个「开始增量更新」。

**改名对照（节点名 = 主命令名，变体名单独拿出来也看得懂）**

| 节点 | 原节点名 | 原主命令按钮 | 现在（节点名 / 主命令名） | 变体（原 → 现在） |
|---|---|---|---|---|
| `core.research` | 资料分析 | 资料分析 | 资料分析 | 追加分析 → 追加资料分析；核验报告 → 核验资料分析报告 |
| `core.object` | 提取对象 | 提取对象 | 提取对象（不变） | |
| `core.operation` | 提取操作 | 提取操作 | 提取操作（不变） | |
| `core.update` | 增量更新 | 增量更新 | 增量更新（不变） | |
| `core.metric` | 指标 | 提取规则与指标 | 提取规则与指标 | |
| `core.validate` | 校验 | 运行质量检查 | 校验 | |
| `core.publish` | 发布 | 去发布 | 发布（页面按钮） | |
| `trace.trace` | 场景模拟 | 批量验证 | 场景模拟（`/ontology-trace 全部`） | 交互模拟、生成示例数据（不变） |
| `diagram.flowchart` | 流程图 | 创建/打磨流程图 | 绘制流程图 | 全量刷新 → 全量刷新流程图；校验流程图（不变） |
| `codegen.codegen` | 代码生成 | 扫描候选（排第一） | 生成脚本（主命令改成 `/ontology-codegen`） | 扫描候选 → 扫描脚本候选 |

场景模拟页和 trace 清单里的「批量验证」也改叫「场景模拟」。过期的按钮原来叫「对象有变化，刷新提取操作」，现在叫节点名「提取操作」，
「对象有变化」写在条目的 `reason`（建议条的说明行）。「全量刷新流程图」「校验流程图」加 `when: [flowchart]`，已有流程图时才出现
（全部命令、建议条、节点动作同样）。版本：core 0.6.1、trace 0.11.1、diagram 0.7.1、codegen 0.10.1。

**名字表（`flow/names.py`）**

1. `name_for(flow, text)` 看 text 第一行，按顺序取名：动作文本完全匹配（后面带参数也算，取最长的）→ 某个节点的主命令（节点名）→
   变体（变体名）→ 命令表里的节点（节点名）→ 调用方给的叫法 → 命令名。用条件过滤之前的流程（`model.assembled_for`），
   没出现的增量更新手打也叫「增量更新」。
2. 装配时主命令的 `label` 和节点 `title` 不一致的以 `title` 为准（`need.normalize_node`）。
3. 接上名字表的地方：S-20（任务标题、对话气泡、排队条、执行记录）、手打的斜杠命令（排队条、执行记录）、执行记录 `command_label`
   （发消息时取好的名字优先）、自主构建复核发的命令。`command_display_name`（「节点 · 按钮叫法」）删掉；页面传来的
   `display_label` 只在名字表认不出这条命令时用。
4. trace 的命令表补上 `trace-batch`、`trace-single`、`trace-interactive`，都指向 `trace.trace`：场景模拟页直接发的推演也算这一步
   （有执行记录，节点显示进行中），名字叫「场景模拟」。

**S-18 全部命令**

5. 每个导航栏节点一组（`id` 节点编号、`label` 节点名），按导航顺序；组内先列本节点发到对话的按钮（主命令在前，动作的 `when` 照样
   生效，节点钩子在后），再列挂在它下面的可选、条件节点发到对话的按钮。跳页面的按钮（发布）不列。**不设「其他」组**：整体视图、
   影响分析、查询、版本历史、`/ontology`、场景模拟的子文档不进菜单。原来按 need 分的「建模 / 可选能力 / 有需要时 / 其他」四组去掉。
6. 条目加 `target`（完整命令文本）、`primary`（是不是节点的主命令）；`node_id` 不再为 null；`label` 是按钮名；`description` 恒为空串；
   `id` 是 `<节点>#<序号>`（同一条命令可能有几个按钮）。
7. S-20 改认当前装配里装了的命令（`installed_commands`，`.claude/commands/<名>.md` 在）：不在菜单里的命令手动输入照样能发。

**推荐五级（`flow_state.suggest`，`SUGGEST_RULES` 9）**

8. 从上往下判，命中就停：1 对话状态（等回答含目标问题、出错、被打断、没收尾、合入冲突、等你打字回复、自主构建在跑）；2 条件已成立
   没处理完的节点、过期且没交给下游核对的必需节点，取流程里最靠前的一个；3 第一个没做完的必需节点（在跑或待确认时为空）；4 必需都
   做完时，没做也没跳过的可选节点、过期的可选节点、可补做的必需节点，取最靠前的一个；5 不显示。第 2～4 级一次只推一个节点：主推是
   节点主命令，次选是同一节点的变体，最多 2 个。
9. 去掉：写死的「查看整体视图」「扫描脚本候选」「继续校验」，关卡规则，`preempt`，目标问题后面跟主线下一步、合入冲突后面补主线下一步、
   好几个过期节点一起推。「回答目标问题」并进第 1 级。
10. 节点已有产出（`produces` 下有文件，上下文新增 `produced`）、还没完成时，`verify: true` 的动作排第一（资料分析报告待核验时先核验）；
    核验动作加 `when: [research_report]`。`done` 为空的节点第 3、4 级不推；条件节点做没做完由条件说了算，照样推。
11. 能执行的检查：第 1 级照旧整条不能执行时往下判；第 2～4 级选中的节点没有能执行的条目时建议条为空，不越过它推后面的。
12. 说明行 `note`：第 2 级和第 4 级的过期项写哪类变了；条件节点出现的原因（「上传了新资料」）写在条目的 `reason`，不出说明行，
    旧写法 `preempt` 的说法不再用；`priority` 是所在的级。
13. 后端 `_suggest_module` 只用 `SUGGEST_RULES >= 9` 的 flow_state，锁定旧 core 的项目用镜像版本。后端、助手副本、注入三个出口同一份，
    测试按几个典型状态核对。
14. 前端 `incremental/model.js` 的 `withStartSuggestion`（用「开始增量更新」顶掉主推）和 `start_incremental` 动作删掉，统一交给第 2 级
    （批次 3 接上 `materials_added` 之后推「增量更新」）。

**给模型的说明**

15. flow.yaml 的 `next_steps` 和命令文档里列的回复词换成统一的名字（validate、trace-batch、trace-single、codegen、/ontology）。
16. 注入的「下一步建议」改成「仅供判断，不要在回复里列出」，和 INTERACTION_CONVENTIONS「不列下一步」一致；提到某一步时用界面上的名字。
17. `/ontology-codegen` 不带目标时先扫描候选，再用确认卡让用户挑（原来让用户先选「扫描」或「选择」）。
18. 插件开发指南：节点名就是主命令名，变体的按钮名单独拿出来也要看得懂。

**行为变化**

- 必需节点都做完（发布过）之后，建议条推可选节点（绘制流程图、提取规则与指标、生成脚本），没有可推的时候不显示；原来推「查看整体视图」。
  可选节点要能「跳过」才不会一直被推，跳过入口要和这一批一起上线（批次 2b 前端）。
- 目标问题没答时只推「回答目标问题」，不再跟着推主线下一步；合入冲突时只有「处理冲突」。
- 菜单里同一条命令可能出现几次（资料分析、追加资料分析），前端按 `target` 发。

## 2026-11-18 · 增量更新响应新资料、自主构建按 need（节点模型重构 3）

内核的增量更新 `core.update`（`need: {when: materials_added}`）接上条件：已经建了一部分又上传新资料时出现、建议条推它，
这一轮处理完就隐藏。自主构建做哪些节点改按 `need` 判，「包括可选步骤」的说法按启用的可选节点生成。

**内核条件 materials_added**

1. 上传资料（R-02，`save_resource`）时主线上对象和操作都有了（`ontology/object` 下有条目，`ontology/operation` 或 `ontology/logic`
   下也有），就在主线工作区的 `.onto/run/activation.json` 记下 `kernel:materials_added`：`{event, commit, at, files}`。和增量更新
   命令的前置 `[逻辑已提取, 对象已提取]` 一致：只有对象时上传不记，免得建议条推一条跑不了的命令、把「提取操作」挤下去。不要求有包
   声明这个事件；包级的 `activation` 照旧（它的前提是包自己的探针），同一次上传各记一份。
2. 这一轮处理完（满足一条就算），节点隐藏：
   - 上传之后**开始**的一次增量更新正常做完：执行记录 `done`、`started_at` 晚于上传时间，而且命令锁走到过执行更新（`phases_seen`
     有 `apply` 或 `validate`）。在确认卡上选「取消」的那次也是 `done`，但只到过 `analyze`、`confirm`，不算；前置不满足就停下的
     （锁都没拿）不算；被打断（`interrupted`）、放弃（`abandoned`）的不算；上传时已经在跑的那次做完也不算。出错时命令锁还在，
     记录停在等待或中断，同样不算；
   - 触发那次提交之后发布过（最新的 `release/*` 标签包含触发时的提交，和 `round_open` 同一口径）；
   - 用户选了「这一轮不处理」（FL-03）。
   处理完时在记录上写 `handled: {by: update|release|skip, at, reason?}`，之后读流程不再翻执行记录、不起 git 进程（执行记录只留
   最近 200 条，不能靠它记住这一轮结束过）。没有记录时也不起 git 进程。
3. 这一轮没处理完时再上传：文件累加（最多 50 个），`commit`、`at` 换成这一次的（这次的资料也要有一次增量更新看过）；处理完之后
   再上传开始新的一轮，文件从这次算起。
4. 触发是附带动作：算不出来记日志，上传照常成功。内核和包分开算，一边出错不影响另一边。
5. 隐藏时 `flow-state.json` 里记成 `{state: skipped, flags: {hidden: true}}`，助手副本的 `state_helper suggest` 不推它，和后端同一份。

**接口**

6. **FL-01**：条件成立时增量更新在 `nodes` 里（`need: {when: "materials_added"}`、`nav: false`、`under: "core.operation"`、
   不计入进度），建议条第 2 级推「增量更新」（`reason`「上传了新资料」），S-18「提取操作」组里有它的按钮；处理完就不在了。
   示例里的 `notices` 新资料提示去掉（后端没有这条提示，新资料由节点出现表达）。
7. **FL-03** 对条件节点是「这一轮不处理」：结束这一轮、节点隐藏，不记跳过，下一轮照常出现；响应 `node` 为 null。只有内核条件
   能这样结束（`conditions.ROUND_ENDERS`）；按插件触发的节点（`activated`）这一轮由插件的状态文件管，照旧回 `409 node_not_skippable`。
   复用 FL-03 而不新开接口：界面上都是节点下拉里的「不做这一步」，语义差别（可选节点能恢复、条件节点下一轮自己回来）由节点的
   `need` 决定，前端不用多认一个接口。前端按钮这一批不做（见「2b 前端」一节）。
8. **FL-04** 只恢复跳过的可选节点；条件节点结束这一轮后不在流程里（`node_not_found`）。
9. **R-02** `is_revision`、`batch_id` 收下不处理，`command_task` 恒为 null。原设计是整批发一条增量更新命令，和条件节点重复
   （同一批资料会推一次、又自己跑一次）；现在一律由条件节点出现、用户点了才发。
10. **AB-01** 加只读的 `optional_nodes: [{id, name, under, order}]`（启用的、资料分析之后、自主构建能做的可选节点，按流程顺序；
    `under` 挂在哪个必需节点下，`order` 在流程里排第几）和 `until_order`（`until_node` 排第几，没设为 null）。AB-02 的响应同 AB-01。

**自主构建**

11. 做哪些节点按 `need` 判（`autobuild/plan.eligible`）：必需节点做；可选节点只在「包括可选步骤」时做；条件节点、`autobuild: false`、
    人工、面板、没有主命令的不做；没整理过的流程里旧写法的按需节点也不做。去掉写死的「发布、增量更新不做」（发布是人工节点、
    增量更新是条件节点）。第三方包的节点没写 `autobuild: true` 时装配已补成 `autobuild: false`（2a），这里不再另判。
12. 行为变化：原来按 `placement: spine` 判，旁支（`branch`）的可选节点打开开关也不做，开关实际不起作用；现在打开后加上绘制流程图、
    提取规则与指标，生成脚本（`autobuild: false`）、求解适配（面板）不加。旧写法 `step`、`gate` 的节点现在按必需节点做（官方包里没有这种写法，第三方包的默认不做）。
13. `until_node` 之后的可选节点不做：自主构建按流程顺序走到 `until_node`（含）就停，原来就是这样，没有改。注意按的是流程顺序，
    不是挂在哪个节点下：做到「提取操作」时，挂在它下面、排在它后面的「提取规则与指标」不做。

**前端**

14. 项目设置「范围」的开关文案按 `optional_nodes` 生成：「包括可选步骤：」后面列节点名，用「、」连接，只列 `order <= until_order`
    的（做到哪一步之后的不列，和后端同一口径）；一个都不剩（或老后端没有这个字段）时不显示开关。做到哪一步在这个页面上不能改，
    按已保存的策略算。mock 的 AB-01、AB-02 带上 `optional_nodes`、`until_order`。

**资产与插件开发指南**

15. core `flow.yaml` 增量更新节点的注释改成现在的出现、隐藏规则（内容摘要变，core 版本号的 `+摘要` 随之变）；插件开发指南里
    `need.when` 的说明补上 `materials_added`。

## 2026-11-18 · 导航栏节点下拉、全部命令菜单、建议条说明行（节点模型重构 2b 前端）

2b 后端留下的界面：导航栏节点能点开，可选步骤、跳过 / 恢复、「这一轮不处理」都在下拉里；输入框「/」菜单和 ⌘K 用同一份 S-18；
建议条按钮只写名字，原因放说明行。跳过入口和建议条第 4 级（必需节点都做完后推可选节点）一起上线。

**后端**

1. **FL-01**：条件节点出现在流程里（条件成立）时 `nav: true`（批次 3 是 false），导航栏把它排在 `under` 后面：增量更新排在提取操作
   后面，这一轮处理完就从 `nodes` 里去掉，导航栏上也没有了。
2. **名字表**（`flow/names.py`）第 4 条：调用方给了 `display_label`（页面按钮的叫法）时不再按命令表退回节点名，直接用它。场景页的
   「交互模拟」「补齐过期场景」发的是场景模拟的子文档命令，原来都退回成「场景模拟」。第 1～3 条（动作完全匹配、节点主命令、变体）照旧优先。

**导航栏（`NavTrack.jsx`；下拉里有什么由 `flow.navMenu` 算）**

3. 只放 `nav` 为 true 的节点，按 FL-01 的顺序，条件节点排在它挂的节点后面。默认包：资料分析 → 提取对象 → 提取操作 →（增量更新）→
   校验 → 场景模拟 → 发布。
4. 点节点名展开下拉，展开时重取 FL-01。从上到下：
   - 这个节点发到对话的按钮：FL-01 `actions` 里的 `start`，主命令在前、变体在后；过期时叫节点名的 `refresh` 顶替主命令，`reason`
     放在悬停提示里。斜杠命令走 S-20（带 `display_label`），核验报告那句当普通消息发，页面编号跳页；
   - 分隔线；
   - 挂在它下面（`under`）的可选节点，每行「名字 + 状态 + 一个小按钮」。点名字开始：有斜杠命令的发主命令（过期发刷新那条），脚本、
     人工节点调 FL-05。小按钮没跳过时是「跳过」（FL-03），已跳过时是「恢复」（FL-04）；不另放「开始」按钮。面板节点（求解适配）
     没有小按钮，点名字去声明了它的插件页（PL-12 `pages[].data.nodes`，代码生成页带 `?node=codegen.solver` 打开求解契约页签），
     没有这样的页时只显示状态；
   - 条件节点多一项「这一轮不处理」（对它调 FL-03）。
   跳过、恢复、「这一轮不处理」之后重取 FL-01。
5. 只有一个跳页面按钮、下面没有可选节点的节点（发布）不做下拉，点节点名直接去；什么都没有的节点不能点。项目已归档、平台只读维护
   （输入框不能用）时只留跳页面的节点，别的不展开。

**全部命令菜单（输入框「/」）与 ⌘K（`state/commandMenu.js`）**

6. 输入框打「/」或点「全部命令」打开，每次打开重取 S-18（原来进工作台取一次）。分组、条目和 S-18 一致，组名是节点名；去掉右侧
   灰字（`description`）和「共 N 条」；`available: false` 的条目不列。
7. 斜杠后面接着打字按名字筛（不分大小写），没有条目的组不显示；↑↓ 选，回车执行，Esc 只关菜单（不当成打断）；没有匹配时菜单收起，
   回车按原文发送（S-20 认装了的全部命令）。从按钮打开、框里是别的字时列全部，只能点选，回车照常发框里的字。按键怎么处理由
   `commandMenu.menuKey` 算，输入框只照做。
8. 执行发条目的 `target`（`flow.catalogCommand` 拆成 `command`、`text`，带 `display_label`）；从「/」打开的执行后清空输入框。
9. ⌘K 的命令用同一份 S-18、同一套筛法，组名、条目名和菜单一样；有输入时才列，最多 6 条。

**建议条**

10. 按钮是后端给的节点名或变体名，不拼原因；说明行是 `note`（过期项的「对象有变化」）。会话状态页（chat-states）的示例跟着改。

**其他页面**

11. 资料页「追加分析」改名「追加资料分析」，发 `/ontology-research 追加`，和资料分析节点的变体同一条命令文本、同一个名字。
12. 场景页「场景模拟」「补齐过期场景」「交互模拟」发命令时带 `display_label`（按钮名），对话里叫按钮名。
13. 工作台顶栏的「N 份新资料」去掉：后端从批次 3 起不再给 `new_materials` 提示，新资料由导航栏上出现的「增量更新」表达。
    「全部命令」菜单只剩按钮名，宽度从 440px 收到 260px。
14. mock：FL-01 节点带 `need`、`nav`、`under`（增量更新 `nav: true`），可选节点的动作最后是「跳过」或「恢复」；FL-03、FL-04 按
    `need` 处理（条件节点从流程里拿掉、返回 `node: null`，必需节点回 `node_not_skippable`）；S-18 按导航栏节点分组，跟着当前项目的
    流程算；AB-03 预览按 `need` 算；`routes-resources` 里 `is_revision` 造出来的 `revision_task` 删掉；FL-01 不再带 `new_materials` 提示。

## 2026-11-18 · 重新提取只盯来源和条目增减（ADR-0102）

端到端实测校验确认后建模仍显示过期、自主构建会重跑提取，用户 11-18 定修过期逻辑。

1. **产物类别（`stale_on`、`requires` 可写）新增 `object_set`、`operation_set`**：和 `object`、`operation` 同目录，只看条目清单
   （新增、删除、改名），内容修改不算。PL-15 静态检查认这两个；开发 Skill 的类别列表自动带出。
2. **core 的声明**：建模「操作」一步 `stale_on: [research, object_set]`（原来 `[object]`），「规则」一步 `[object_set, operation_set]`
   （原来 `[object, operation]`），指标节点 `[object_set, operation_set]`（原来 `[object, operation]`）。对象一步、校验、发布不变。
3. **FL-01 节点与 `slices` 状态**：对象、操作的内容修改（校验修复、工作台编辑）只让校验过期；建模、指标只在资料分析变了或条目有增减时
   过期。响应形状不变；过期按钮说法多了「对象清单有变化，刷新操作」「操作清单有变化，……」。
4. **建议条第 7 条（`flow_state.handed_down`）**：判断下游核对节点能否接手时，`object` 盖住 `object_set`、`operation` 盖住
   `operation_set`。`SUGGEST_RULES` 不变（判定与 flow.yaml 同在 core 包里一起升级）。
5. **升级迁移**：节点或步骤的确认记录按另一套 `stale_on` 记的（分类指纹的类别不同；没存分类指纹的老记录按 ADR-0102 之前的定义认），
   第一次计算时按确认那一刻到现在主线上新上游有没有改动来定：没改或找不到那一刻的提交，按新定义重记基线；改了判过期、记下变了的类别。
   结果写回 `flow-state/<项目>.json`，不另做迁移脚本。

**合进节点模型重构后（同日）**：上面第 2、3、5 条说的建模步骤、`slices` 状态随分段一起去掉了，对应落到节点上：
`core.operation`「提取操作」`stale_on: [research, object_set]`（重构时是 `[object]`），`core.metric` 仍是 `[object_set, operation_set]`，
「规则」一步已并进指标。老记录的旧定义（`STALE_ON_BEFORE_0102`）按节点认：提取操作 `[object]`，指标 `[object, operation]`；
拆分迁移（`migrate_split`）带过来的按段记录存着分类指纹，同样走第 5 条。提取操作盯着资料分析，校验接不了手，过期了校验确认过也照推
（建议条第 2 级、待处理同一判定）；对象只改内容时它不过期。

## 2026-11-18 · 去掉资料区「修订资料」和 ⌘K「发起增量更新…」（节点模型重构 2b 前端补）

批次 3 起 R-02 的 `is_revision` 收下不处理、资料列表里恒为 false，新资料由流程里出现的增量更新节点表达。下面这些界面没有后端支撑，
真实后端截图时查出，用户 11-18 定删掉：

1. 资料区：上传框下的「作为修订资料上传（发起增量更新）」勾选、上传提示里的「；已发起增量更新」、修订资料横幅、行内「修订资料」标签和
   「去对话里确认 →」。上传不再带 `is_revision`、`batch_id`；R-02 照旧接受这两个字段，不改契约。
2. ⌘K：写死的「建模 · 发起增量更新…」去掉（只是跳回工作台，而且不管有没有新资料都显示）；增量更新节点出现时，⌘K 按节点分组的命令
   里本来就有它。`incremental` 页面路由照旧，工作台卡片还在用。
3. 上表「页面命令（S-20）」里「资料区 · 修订资料上传后」一行删掉。mock 的「09-修订通知」不再标修订资料，和后端一致。

## 2026-11-18 · agent 提交写不进对象库（按组共享版本库）

实测自主构建时，agent 每步收尾的 `git add` / `git commit` 一再失败，它转而改 HOME、复制对象库去绕，三步里白费约 6 分钟。
原因：root 以默认 umask 022 跑过的 git（运维 `docker exec` 等）在 `.git/objects/` 下建出 0755 的前缀目录，agent 用户（方案 E10）
写不进，之后哈希落进这些目录的对象都提交失败。用户 11-18 定：保留 agent 自己提交（方案 C11），让它对版本库可写。

1. **按组共享**：装 agent 提交钩子时（启动恢复「agent 提交钩子」、每次会话开始）把项目仓库设成 `core.sharedRepository=group`，
   之后不论谁、以什么 umask 跑 git，新建的目录与文件都对 agent 组可写。只在切用户时做（root + 配置了 `ONTO_WEB_AGENT_USER`）。
2. **修存量**：同一时机把 `.git` 里 agent 组写不进的目录补回组读写与 setgid、其余文件（索引、引用、日志）补组读写；对象文件本来只读，不动。
3. 接口与响应形状不变；启动报告「agent 提交钩子」一项照旧。

## 2026-11-18 · 自主构建提速：并行派发、机械型子任务不思考、回归只做一轮

实测一次自主构建（流程图 → 对象 → 操作 → 规则与指标 → 校验）跑了 85 分钟以上，耗时几乎全是模型生成：输出里 85%～95% 是思考，
校验一步 47 分钟还没做完。逐项拆开后改了下面几处（第 2、3 条用户 11-18 定）：

1. **同一条消息里的工具调用并行执行**（Jiuwen 引擎 `parallel_tool_calls=True`）：命令文档早就要求语义检查的三个部分在同一条消息里
   一起派，但引擎声明了串行，模型每次只能发一个调用，三部分实际一个接一个跑（这次 15.7 分钟，最长的一部分 9.8 分钟）。桥接层按原生
   调用 id 配对，进度按子任务 id 分开记。
2. **机械型子任务关闭思考**：agent 文件第一行写 `<!-- onto: thinking=off -->` 的，派发时子代理换用关闭思考的模型实例，请求带上
   `ONTO_WEB_JIUWEN_QUIET_EXTRA_BODY`（默认 `{"enable_thinking": false}`，留空 = 不关）。core 里只标了规则检查、覆盖检查；提取、语义检查、
   语义修复和主代理照常思考。用 HTML 注释不用 frontmatter：`.claude/agents/*.md` 带 frontmatter 会被稳定引擎当成自定义子代理。
3. **校验的回归检查只做一轮**（规则 A5.5、语义 B5）：回归查出的新问题写进报告留待处理（行首 `[ ]` / 编号行），不再修、不再复查，
   也不问用户。代价：这些问题挡着校验完成和发布，要人处理或下次校验再查（这次跑了三轮回归，约 12 分钟）。
4. **diff 交给子任务自己取**：回归和只查改动模式的派发提示改给 `CHANGE_DIFF_CMD`，子任务自己运行；主代理不再把 diff 原文抄进
   派发提示（抄写也是模型输出）。工作区规则「子任务派发」补一句：已在文件里或能用命令取到的内容给路径或命令。
5. **agent 的命令用 bash 执行**：命令资产按 bash 写（数组、`< <(…)`），镜像里 `/bin/sh` 是 dash，加载业务知识等步骤每次先报语法错、
   模型再改写重试。有 `/bin/bash` 就用它。

没改的：语义修复的问题清单仍由主代理抄进派发提示（要改 B2 的编号流程，收益约 1 分钟）；单轮迭代上限仍是 60。

## 2026-11-18 · 自主构建提速第二轮：悬空链接不拦、思考封顶、可选步骤做过就跳

提速后再跑一次自主构建（60 分钟），又查出三处白费的时间。用户 11-18 定前三条：

1. **指向还没提取的文档的链接不拦**：对象先于操作提取，对象里 `[[办理会员]]` 这类链接在提取操作之前必然悬空；增量更新时新对象引用
   新操作也一样。原先本批核验（`verify_artifacts.py`）和提交检查都把它当错误，agent 只好把链接改成纯文本（约 3.5 分钟），校验时又报
   「没用链接格式」。现在 `链接失效`、`引用失效` 在本批核验和提交时降为警告、不拦（核验回执版本升到 2）；校验时全量检查照样按错误报。
2. **每次模型调用给思考封顶**：一次调用思考到输出上限（32000 token、9 分钟），子任务随后超时被判失败、主代理重做，共约 15 分钟。
   新配置 `ONTO_WEB_JIUWEN_EXTRA_BODY`（默认 `{"thinking_budget": 16000}`，留空 = 不附带）随每次请求发出；关思考的子任务两样都带，
   关思考的写法优先。
3. **可选节点走到时已确认就跳过**：提取操作连规则、指标一起抽，紧接着的可选步骤「提取规则与指标」在首次构建里只剩补链接。自主构建
   走到可选节点时它已是「已确认」（上游顺带做完、没过期）就不做，单元记为做完，结果写「上一步已做完」；过期的、接过来做到一半的、
   带着上次挂起问题的照做。开始时就已确认的节点本来就不进运行。
4. **检查器默认报告写进 `tmp/`**：不带 `--json` / `--output` 跑 `ontology_checker.py` 时报告原先落在工作区根目录，会被下一次
   `git add -A` 带进提交；现在写到 `tmp/ontology-check/`（工作区忽略）。提交检查的提示命令也补上 `--json`。

## 2026-11-18 · 模型默认不思考；自主构建跳过给人看的步骤

第四次实测全程关思考：同一份资料的自主构建从 50 分钟降到 16.6 分钟（流程图 3、对象 2、操作 5、校验 6.7），对象、规则、指标的名字
与前一次一致，资料里 16 条业务事实都覆盖到，收尾规则检查 0 错误 0 警告；校验查出的语义问题从 8 个降到 4 个。用户 11-18 定：

1. **思考默认关，设置里可打开**（新增 `SY-19`，`SY-01` 多 `thinking`）：系统设置 → 模型接入 →「思考」，两个引擎都管。
   关时稳定引擎 `MAX_THINKING_TOKENS=0`，内测引擎每次请求带 `ONTO_WEB_JIUWEN_QUIET_EXTRA_BODY`；开时沿用原配置（内测引擎带
   `ONTO_WEB_JIUWEN_EXTRA_BODY`，默认思考封顶 16000）。计入会话配置指纹，在线会话下一条消息前重连；个人模型配置不单设。
2. **自主构建跳过给人看的步骤**：命令文档里标了〔自主构建跳过〕的步骤不做——BFO 提醒、确认前验收展示表、确认锁与「回复确认」、
   对账原样展示、校验 A4b/B4b 汇总表、流程图挑选与摘要展示；检查脚本照跑、没过照样修。自主构建每轮的说明里同时要求：互不依赖的
   调用一次发出，要派给子任务读的资料主对话不预读，写完的文档不回读展示，收尾一句话。陪伴模式不变。

## 2026-11-19 · 会挡发布的问题在建模中修掉，数据绑定发布时自动降级（ADR-0103）

用户 11-19 定：除「对象和数据表对不上」外，发布会被拦的问题都在建模过程中发现并修掉；最后校验时按发布规则查一遍，反馈给模型修。

1. **发布就绪检查**：校验之后平台用发布检查同一份代码（PB-01 与 `readiness` 共用 `content_checks`）查降级也绕不过的门禁。
   自主构建：校验这一步的变更集在一轮结束的检查里带 `checks.publish`（内部字段，变更集视图不输出），合入前的修复循环一起交给助手。
   陪伴模式：校验命令做完后平台发修复消息，`EC-10` 的 `origin` 为 `{kind: "report", page: "publish", label: "发布检查"}`，最多 2 轮。
2. **待处理清单**：修不掉的写进 `reports/publish-check.md`（每条 `- [ ] ❌ [发布检查] …`，提交来源 `plugin`），校验节点因此不算完成；
   查干净后平台删掉。
3. **校验过期**：core 的 `core.validate` 的 `stale_on` 加 `script`、`solver`。
4. **检查器**：按发布包口径判错误——属性英文名称重复、发布包里有中文名或别名却没填英文名称（导出会丢掉英文名称为空或占位的行，
   只有一行都没填时原表照发），容量上限（对象实际最多 100 个、操作目录与单个操作含文件头）；合入检查带 `--include-scripts`。
5. **发布页**：只有数据绑定没过时主「发布」按钮直接带 `degrade: true` 发布，去掉「忽略并降级发布」按钮和确认框。接口不变。

## 2026-11-19 · 数据源热加载：连接器插件 + 数据源资料（ADR-0104）

用户 11-19 要求内核搭好框架，以后接新的数据源只需上传多源连接器插件（需要时加能力包），不改内核、不重建镜像、不重启。
用户定了四件事：信任方式是插件中心点「信任」；采集结果当作一份资料；项目成员自助接入；第一批交付 MySQL、PostgreSQL。

1. **新增 3.19a 数据源**：DS-01 `GET /api/v1/source-connectors`、DS-02 `POST /api/v1/projects/{id}/sources`、
   DS-03 `PATCH /api/v1/projects/{id}/sources`、DS-04 `POST /api/v1/projects/{id}/sources/test`。
   - 数据源是资料 `resource/excel/<名称>.datasource`（`Material.kind="source"`、`type_label="数据源"`），新增字段 `Material.source`。
   - 表数放在 `Material.sheets`，新增类型 `SourceField`、`SourceConnector`。
   - 采集走资料转换的队列和状态（R-13、R-14、EP-25 不变，「重新转换」就是重新采集）。
   - 用途预填「数据 Schema」，Sheet 范围和 Excel 同口径（ADR-0096）。
2. **新增 PL-28、PL-29**：管理员信任、撤销信任上传的多源连接器，按版本记，存在通道记录的 `trusted` 上。
   - `PackCard` 多 `source`（数据源连接器的字段数、许可证）和 `trust`（`builtin` / `trusted` / `untrusted`，网关里装上了没有）。
   - 多源连接器分类里同时列转换器和数据源连接器。
3. **连接器网关的新接口**（`design/data-sources.md` 第 5 节）：
   - `PUT/DELETE /v1/plugins/{名}/{版本}`、`PUT/DELETE /v1/secrets/{项目}/{引用}`、`POST /v1/sources/test|collect`；
   - `/v1/providers` 多 `sources`、`plugins`，`/health` 多 `sources`；
   - 票据操作多 `plugin_install`、`plugin_remove`、`secret`、`source_test`、`source_collect`；
   - 3.19 里「`/v1/secrets/*`、`/v1/sources/*/test` 一并删除」指的是证据采集那一版的接口，新接口路径和语义都不同。
4. **内核条件** 多 `data_sources`（项目里有数据源资料），能力包、节点包的 `visible_if` / `need: {when}` 可用。
5. **工具箱**：
   - `table_sources` 把 `.datasource` 当表格资料、读文本版；
   - `table_profile --headers` 的文件多 `foreign_keys`（库里声明的 `declared`、按命名推断的 `inferred`），摘要多 `foreign_keys` 条数；
   - `source_extract` 认数据源的文本版，锚点同 Excel；
   - 对象抽取守则：`declared` 的外键直接写「关联对象」的关联字段。
6. **静态检查**：`kind: provider` 的包多一项 `provider`，要求 `converter`、`source` 二选一、写法合法、入口文件在。

## 2026-11-19 · 插件自己的页面、上传位与下载（ADR-0105）

公共本体插件要一个自己的页面：上传多个知识包、发抽取命令、看结果、下载。用户 11-19 定页面用插件自带的 iframe（不用平台按声明画）。

1. **1.1 版本**：`PAGE_API` 1.0 → 1.1，`UI_API` 1.0 → 1.1（1.0 的方法与消息不变；协议只比主版本，1.0 的编辑器照常握手）。
2. **清单**：`contributes.pages[]` 的 id 不是内置页时要带 `ui: <编辑器 id>`（本包 `contributes.editors` 里声明的），官方包暂不支持；
   新增 `contributes.uploads: [{id, title, kind: knowledge-package, max: 1～20}]`。`ManifestSummary.contributes` 加 `uploads`。
   PL-15 `frontend` 检查改为「页面是平台内置或本包的隔离页面，前端代码只在 ui/ 下」。
3. **PL-12**：包自己的页面进 `nav`、`pages`、`disabled_pages`，`id` 是 `<包名>:<页面 id>`；`pages[]` 另带 `page`、`ui`。
4. **PL-20（打开上传包编辑器）响应**加 `types`（本包登记的产物类型）、`uploads`（本包的上传位）。
5. **新增 PL-30～PL-33**：往上传位放知识包、列表、删除、下载插件目录里已提交的文件（目录打 zip）；PL-33 可用 `?token=`。
6. **窄接口 1.1**：页面模式 `context.get` 带 `page`、`uploads`，`artifact` 为空；新方法 `artifact.list`、`upload.list`、`upload.put`、
   `upload.delete`、`file.download`、`studio.open`、`command.tasks`；新推送 `upload.changed`、`command.progress`。页面发的命令
   `origin.page` 是 `<包名>:<页面 id>`。
7. **变更集**：`ext/*/uploads/` 是受保护路径（同资料区），助手副本里的改动不进变更集。
8. **前端路由**：`#/p/<项目>/<包名>:<页面 id>` 打开插件页（`pages/plugin/PluginFramePage.jsx`）。
9. **视图 `card: false`**：这类产物不在对话里出卡（AR-01 视图的 `card.hidden = true`）。`artifact.list` 收 `cursor`、`limit`（≤200）。

## 2026-11-22 · Studio 修复与 3D 图谱（develop !611、!624）

develop 在 !611（#407、#415、#416）和 !624 改了 Studio 的表格、关系图和图谱展示，这里移植到插件化结构，页面和接口路径不变。

1. **元模型**：规则加两个可选章节「责任部门」「维护人」，C-05 随之返回；可选章节不升版本。规则模板和规则提取守则已带这两节。
2. **ST-02**：对象之间的关系按发布导出的身份去重（源对象、关系名、目标对象、两端关联字段），同名不同字段的关系 `label` 注明字段；
   声明的对象自关联返回 `source` 等于 `target` 的边，其他自引用不成边；导出不认的声明退回逐行扫描，同方向已有声明时不再返回。字段不变。
3. **ST-03**：规则行多 `departments`、`maintainers`（字符串数组，没填为空数组），其他类型不带。develop 放在图接口的规则节点上，
   这里放到条目表，因为表格页只请求 ST-03。
4. **Studio 表格**（`pages/studio/StudioTablePage.jsx`、`StudioTableFilters.jsx`、`studioTable.js`）：类型选「规则」时多两列，表头可多选
   （含「未填写」）；「最后更新时间」按日期范围筛选。表格在选中条目时不卸载，筛选和排序原样保留，不需要 develop 的返回视图状态。
5. **3D 图谱**：`LegacyStudioGraphHost` 的 props 和页面调用不变；`components/studio/legacy/` 换成 develop 的 3D 运行时、主题和语义图标，
   节点下方只显示类型图标。页面原来的静态图例去掉，改用图谱自带的图例（显示/隐藏、悬停预览）；「展示方式」面板加「再扩一跳」「收起扩展」。
   旧图谱的颜色变量在 `.legacy-studio-host` 上映射到本项目的变量。没有新增依赖（`three` 已在依赖里）。

## 2026-11-22 · 流程图结构修复（develop !613）

没有接口、清单变更。图示包界面（`assets/packs/diagram/ui/flowchart/`）新增 `drawioRepair.js`、`DrawioRepairPanel.jsx`：

1. 流程图弹窗打开时、drawio 保存后、收到 `workspace.changed` 时，多调一次 AR-10 取原文做结构检查（`drawioStructureError`，规则同内核
   FLOW-ID、FLOW-EDGE-DANGLING）。能逐项修的（节点同号、连线端点悬空）在标准编辑器里出修复面板，修不了的只能进 drawio 高级编辑。
2. 修好的原文走 AR-05 的 `content.drawio_xml` 保存，`base_revision` 用 AR-10 返回的 `revision`，期间有人改过会提示冲突。

## 2026-11-22 · 脚本保存校验与克隆带建模目标（develop !616、!612）

1. **F-03 写脚本**：写 `ontology/scripts/*.py` 前按项目锁定版本的契约库只编译检查，不执行源码。语法错误、头部参数与 argparse
   不一致、能静态确定的输出字段与声明不一致，回 `422 check_rejected`，`details` 是 `{errors: Finding[], warnings: []}`，
   Finding 的 `code` 是 `script_contract`，带 `path`，语法错误另带 `line`、`column`。develop 的码是 `script_validation_failed`，
   这里用 F-03 已列的 `check_rejected`（2026-11-03 第 16 条「写入路径前不会出现」到此为止）。检查在事务里、版本号校验之后做，
   拒绝时脚本、描述符、索引和历史都不动。
2. **F-03 响应**加 `warnings: Finding[]`（TODO/FIXME 的文件与行列、输出未验证，最多 50 条）；非脚本文件恒为空数组。
   `Finding` 加 `line?`、`column?`。
3. **发布**：对外脚本的输出静态确认不了时，要有与本次源码（连同同目录依赖）一致的 `script_verify.py run` 通过记录，否则
   `422 script_output_unverified`（`details.path`）；PB-01 的脚本一项同码 Finding 带 `path`。源码哈希按本次渲染的树算：
   Hub 发布冻结（`hub_packages.freeze`）把验证记录一并复制进冻结树，之后工作区再改也不影响这次发布；按提交导出的只读树
   不带记录，读工作区的。记录不算进发布输入摘要、不进知识包。项目锁定的旧契约库没有输出检查，不受影响。
4. **界面**：编辑原文、版本对比保存时，被拒列出每条错误（修改留在编辑器里），有告警列出位置（最多 5 条，再多只给数量）。
   develop 的 `lib/fileSaveFeedback.js` 移到 `api/fileSaveFeedback.js`，显示由 `components/SaveNote.jsx` 做。Studio 提交脚本走
   助手复核（ST-08 `needs_review`），不直接写文件，不受影响。
5. **P-08 克隆**：除 `goal`、`background`、建模模式、引擎外，`meta.goal_status`（develop 上确认过或待确认的建模目标）也带过去；
   原项目没有这个字段时新项目也没有。插件化版本里目标的确认记在 `research/blueprint.json`，本来就随工作区复制。
   项目卡片上只显示两行的目标，悬停看全文。

## 2026-11-22 · 追问作答与扫描防重（develop !606、!623、!629）

1. **扫描防重**（!629）：S-07、S-20 新增错误 `scan_running`（409）。同一条对话里已经有一次扫描可代码化的操作
   （`/ontology-codegen-scan`，带不带说明都算）在跑、连接中、排队或代存着，再发一次就拦下；S-20 的任务记为 `cancelled`。
   - 先于 `message_duplicate` 检查，连点两次扫描回的是 `scan_running`。
   - 助手在跑别的任务时发来的扫描照旧由平台代存（`delivery="next"`）。develop 没有代存，这时回 409「当前任务正在运行」。
   - 打断后暂停的代存扫描不会自己开始，不算占着；自主构建的步骤不受这条限制。
2. **追问作答**（!606）：
   - S-11 从严：选项重复、类型不对、单选题选了多项或选项加文字都回 `answer_invalid`，问题留着可以改了再交。
   - S-10 对决策卡的请求 `allow` 回 `answer_invalid`（直接放行，助手拿不到答案）；`deny` 照旧取消整次提问。
   - 出不了卡的提问不发 `question.asked`，平台把原因退给助手重新提问。develop 出一张异常卡，由用户点「让助手重新提问」。
   - 「Other」「占位示例」这类选项不进 `options`，卡片自带「其他，我来说」。
   - 提交回 410 或收到 EC-34 时，决策卡上填过的内容只读留着并说明没有提交；草稿只在页面内存里，刷新后不保留。
3. **答过的决策卡**（!623）：按 EC-33 的 `answer` 还原勾选和文字，整张只读；策略代答和没有结构化 `answer` 的旧记录仍收成一行。
4. **不搬**：develop 在旧接口 `POST .../permissions/{rid}` 上加的 `answer_details`、`permission_resolved.answers`。
   本线逐题走 S-11，EC-33 的 `answer` 已带选项 id 和文字。`docs/question-answer-contract.md` 并入 `api-v1.md` 的
   S-10、S-11、EC-33、EC-34、4.6，原文删除。

## 2026-11-22 · 独立部署 Auth 与首登改密门禁（develop !638）

develop !638 把 auth-server 从建模离线包里拆出去单独交付，并加了首登改密门禁。这里按 v1 结构吸收（ADR-0107）。

1. **1.3 鉴权**：
   - 验签通过后每个请求都向 auth-server 的 `/userinfo` 核对账号，不缓存；角色以 `/userinfo` 为准，令牌里的 `role` 只作提示。
   - `/userinfo` 拒绝、`sub` 不一致、角色不合法时返回 `401 unauthenticated`。
   - 查不到账号状态（连不上、超时、5xx）时返回 `503 service_unavailable`，`details.dependency = "auth_server"`。
   - 本地模式不核对账号。
2. **新增错误码 2.2 `password_change_required`（403）**：账号待改初始密码时只放行 `A-06`、`A-07` 和免鉴权接口。
   - develop 放行的是整个 `/api/auth/*`；v1 里免鉴权的 `/api/v1/auth/*` 照旧放行，另放行 `A-06` 改密和 `A-07` 我的资料。
   - 旧路径按改写后的 v1 路径判断。
3. **`UserProfile` 加 `password_change_required: boolean`**：
   - `A-07` 返回实时核对的值；
   - `A-01`、`A-03`、`A-04` 的 `user` 取新令牌里的 `pwd_cr`；
   - 本地模式恒为 `false`。
4. **`A-06`**：门禁下可以调用，成功后这个账号的全部令牌作废（auth-server 递增令牌版本），要用新密码重新登录。
5. **不加的兼容口**：develop 新增的 `/api/auth/me` 只给旧前端用，旧前端已删，v1 用 `A-07`，不在第 6 章登记。
6. **配置**：新增 `ONTO_AUTH_STARTUP_WAIT_ENABLED`（默认 `true`），配了 `ONTO_WEB_AUTH_URL` 时启动先等 auth-server 就绪；
   `ONTO_WEB_AUTH_ADMIN_TOKEN` 部署时不再自动生成，要和独立 Auth 的管理令牌相同。

## 2026-11-22 · 知识包数据飞轮（develop !630，ADR-0108）

develop 的数据飞轮后端移植到插件化结构。新增 API 契约 3.21b `FW-01`～`FW-14` 与错误码表 2.9a。子路径、查询参数、请求体和成功响应的字段
与 develop 一致，前端照用；下面是与 develop 形状不同的地方。

1. **路径**：`/api/projects/{id}/flywheel…` 改为 `/api/v1/projects/{id}/flywheel…`，不进旧路径兼容层。
2. **错误体**：v1 的 `{code, message, details, trace_id}`，前端读 `message`（develop 是 `detail`）。请求校验失败由 422 改为
   `400 invalid_argument`：`max_rounds`、`delegation_days` 不在可选值里时 `details.fields` 为 `"one_of"`；`limit` 超过 50、`filter` 不认识、
   `run_id` 不合格、`scope` 不认识也是 400。本地模式（没有统一账号）下 `FW-01` 照旧返回 `{available: false, message}`，其他接口回
   `401 login_required`（与 ES-02～ES-06 同码，前端不当成登录过期去刷新）。
3. **权限**：只有项目所有者能用，非所有者（含全局管理员）`403 project_owner_only`；归档项目写接口 `409 project_archived`，读照常。
   develop 的「成员凭项目角色可编辑」「只读成员只能看」不再有；委托只替未归档项目的所有者登记，项目转移、归档或删除后由后台一轮撤销
   旧所有者的委托（打开页面只重试撤销）。`delegation.<服务>.message` 的「不代执行」说明随之改为「后台只代项目所有者执行」。
4. **新错误码**：`pack_not_enabled`（409，`details.command`：工作区没装配 `/ontology-evolution`）；重复提交同一命令或自主构建运行中，
   开始优化回 `409 flywheel_assistant_busy`（与助手正忙同码，自动执行等下一轮）。
5. **检查**：检查结果多一项 `readiness`（ADR-0103 发布就绪，归在数据绑定一组），检查没能完成按未通过；发布复核遇到发布门禁的码
   （`script_output_unverified`、`package_structure_invalid` 等）停在 `checks_failed`。
6. **发布**：按主线最新提交导出发布，`expected_revision` 比对导出树，不一致时运行停在 `content_changed`；`hub_packages.revision_conflict`
   的 `details` 改为契约 2.4 的 `{target: "ontology", expected, actual}`（develop 是 `{expected_revision, actual_revision}`，只在飞轮内部用）。
   飞轮的提交信息前缀是 `chore:`、`revert:`。
7. **回写**：ES-02 不带 `writeback`，没有 ES-08 标记处理失败，网页发布不调 `after_publish`；评测优化的处理结果只在用户打开飞轮时回写。
8. **能力包**：`/ontology-evolution` 与 `check_eval_generalization.py` 回到基础建模能力 0.6.2（ADR-0101 删除的反馈演进包不恢复）；
   命令里的数据表对账用 `table_profile.py`，不写评测数据源的表头 CSV。
9. **后台调度**：服务启动时开始每 15 秒一轮的飞轮后台任务。不新增部署变量。

## 2026-11-28 · 合入 develop !639～!661

1. **容量上限（!639）**：知识包容量上限跟 develop：对象 1000、操作 500、单个对象属性 300、单个操作文档 30000 字符，规则检查报
   `警告 / 容量上限`，按条数计（不再按发布包目录计）。发布前结构自检仍用 consumer 导入规则副本，按同名环境变量或这组默认值判定。
2. **旧求解验证用例（!640）**：新增 `SV-05 GET /api/v1/projects/{id}/solver/validation-cases`，只读导出；旧路径
   `GET /api/projects/{id}/package/solver-validations/cases` 由 410 改为改写到 `SV-05`。启动迁移不再删用例文件。
3. **旧版评测记录（!640）**：`ES-09`～`ES-11` 删除，发布页「⋯」里的入口一并删除；旧接口 `/api/projects/{id}/eval/*` 的 GET
   仍是 410，`details.successor` 改为 `null`。
4. **界面（!647、!659）**：导入已发布知识包的包列表列名改为「最新版本」，版本列表的「摘要」换成「创建时间」；版本历史条目带
   `can_tag`（项目只有所有者能看历史，恒为 true），为 false 时不显示标记入口。

## 2026-11-28 · 插件开发体验评审：运行中切换

1. **只在等回答的对话不再占住引擎**：`S-07`、`S-20` 在引擎绑着的另一条对话只是在等你回答（决策卡、确认卡）时不回
   `session_busy`：那条的问题按 `reason`「已转到别的对话」撤回（`question.withdrawn`），这一轮停下、记为打断，再开始这条。
   `S-02` 新建对话这时也不再 409。另一条在真跑时照旧 `session_busy`。原因：插件对话里一张没答的提问卡会让整个项目的助手
   都动不了，而在别的对话里找不到能停下它的按钮。
2. **插件对话的标题**：建时的「新插件」是占位，没绑草稿前首条消息换掉它，绑草稿后是插件名（`S-02`）。

