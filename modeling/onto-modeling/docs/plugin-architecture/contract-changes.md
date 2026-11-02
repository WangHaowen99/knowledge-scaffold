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

接口编号见 `api-v1.md`。没写的字段与契约一致；契约里依赖以后阶段能力的字段（切片、自主构建、变更集、写入范围锁、执行记录、命令任务、证据、包）给 null、0 或空列表。

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
   - 节点就是现有 10 个阶段加按需的「增量更新」（`core.research`、`core.flowchart`、`core.object`、`core.operation`、`core.rule_metric`、`core.validate`、`core.mock`、`core.trace`、`core.codegen`、`core.solver`、`core.update`）；按切片的建模节点、全局一致性、发布节点在 P2 按详设 6.8 重排。
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
   - 原因：设计稿把自动决定和挂起按时间画在切片块里，只给 id 排不出顺序。
10. **`AutobuildRun`、`AutobuildReport` 加 `base_version_label`（运行前版本）**，报告的 `slices[]` 加 `reverted`。用于「回退到运行前 · v0.12」和已回退的行。
11. **IN-02 的 `review` 详情加 `base_version_label`、`slice_version_label`。**
12. **高置信合并项**：同一次运行里还没复核、置信度 ≥ 阈值的自动决定合成一项。
    - id 为 `review:<run_id>:high`，带 `high_confidence: true`；
    - `detail` 为 `{group: "high_confidence", run_id, threshold, entries: LedgerEntry[]}`；
    - 对它发 IN-03 `accept` 等价于 AB-13；单条改判走 AB-11 / AB-12。
13. **`LedgerEntry` 加 `summary`**：切片简称加决定，一句话。
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

19. `Version.run` 与 V-02 的 `run` 统一成 `{run_id, run_no, slice_id, slice_name}`。原因：7.6 只有 `slice_name`，V-02 只有 `slice_id`；「查看改动」要按切片 id 对上，界面要显示切片名。
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
