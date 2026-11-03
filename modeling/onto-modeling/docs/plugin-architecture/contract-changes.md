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
| 资料区 · 修订资料上传后（R-02 `is_revision`，服务端发起） | `/ontology-update` | `{reference}` |
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
15. **内置包版本**：codegen 0.9.3、evo 0.3.3（只改了清单说明，见 gaps F3 第 4 条）。

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
