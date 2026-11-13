# Evaluation API

前缀 `/evaluation/api`。除 health、client-config、账号登录/刷新及受 scene 凭据保护的 NL2SQL 外，均要求 `Authorization: Bearer <access-token>`；`/internal/feedback-signals` 另须消费服务凭据。身份在线验证并按 issuer/subject 隔离；不接受建模项目归属或请求体 owner。

| 接口 | 行为 |
| --- | --- |
| `GET /health` | 活性，不代表依赖就绪；允许任意来源读取 |
| `GET /client-config` | 可向评测页面交接登录的建模来源列表 |
| `GET /session` | 当前主体键、user（从已验证 userinfo 取得的 username / role）、当前用户生效的非敏感 Judge 配置（含 source：personal / settings / deployment / disabled）、独立的 judge_configured 标记，以及是否开启消费反馈 feedback_enabled；用户名仅供当前会话展示，不写入任务归属或存储 |
| `GET /judge-settings` | 默认 Judge 配置的来源（页面配置或部署配置）、模型、配置版本及 enabled（默认配置是否对用户启用）；管理员额外可见网关地址和是否已配置 Key，从不返回 Key |
| `PUT /judge-settings/enabled` | 仅管理员；`{"enabled": bool}` 启用或停用默认配置，不改变配置版本。停用后没有个人配置的用户生效来源为 `disabled`，新建轮次返回 judge_default_disabled（409），其排队及进行中的轮次停止判分；重新启用后可恢复。NL2SQL 不受影响 |
| `PUT /judge-settings` | 仅管理员；base_url、model、可选 api_key。默认配置由管理员维护，网关可以是内网地址。保存后替换部署配置，Key 加密存储；网关不变时 api_key 留空沿用原 Key，更换网关须重填。值不变时保留配置版本，否则生成新版本，排队及进行中的轮次随之停止判分 |
| `DELETE /judge-settings` | 仅管理员；删除页面配置，恢复部署配置 |
| `POST /judge-settings/test` | 仅管理员；用提交的值执行与轮次预检相同的结构化判分检查，不保存。网关认证、权限、接口、限流、超时、协议错误返回对应安全提示；模型 Key 的 401/403 映射为 422，避免误触发用户登录失效 |
| `GET /judge-settings/me` | 任意登录用户；返回 `personal`（本人个人配置的网关、模型、配置版本、是否已配置 Key，未配置为 null）与 `active`（本人新轮次将使用的配置来源、模型、版本、是否完整），从不返回 Key |
| `PUT /judge-settings/me` | 保存本人个人配置，覆盖默认配置，只影响本人新创建的轮次；api_key 留空仅沿用本人同一网关的已存 Key，不借用默认配置的 Key；值不变时保留配置版本。网关指向本机或内网地址时返回 judge_gateway_internal（422），`EVAL_JUDGE_ALLOWED_HOSTS` 中的主机和部署配置的判分主机除外；测试和每次调用前同样校验 |
| `DELETE /judge-settings/me` | 删除本人个人配置，恢复使用默认配置 |
| `POST /judge-settings/me/test` | 任意登录用户；测试提交的个人配置，不保存，错误映射同上 |
| `POST /auth/login`、`/auth/refresh` | 直连统一账号，响应不缓存；不保存用户令牌 |
| `GET /packages`、`/packages/{id}/releases` | 当前用户 Hub 包及正式版本列表 |
| `GET /packages/{id}/releases/{rid}` | 精确版本回执，用于跳转到历史版本 |
| `GET /packages/{id}/releases/{rid}/solver-operations` | 该版本启用的求解操作：solver_key、title、decision_model、profile_id、input_schema（不超过 16 KiB，超出时只给 input_schema_truncated）；最多 64 项，供编辑求解验证集 |
| `GET/POST /eval-sets`、`/data-sources`、`/solver-sets` | 用户定义列表和创建 |
| `GET/PUT/DELETE /eval-sets/{id}`、`/data-sources/{id}`、`/solver-sets/{id}` | 读取、带 revision 更新、删除定义；不改历史快照。数据源读取额外返回 connection_view、secret_configured 和 inputs 文件元数据，不返回凭据值 |
| `POST /data-source-inspections` | provider、connection、可选 data_source_id；从 Scene 对应数据库读取表字段，返回 inspection_id、schema、tables、read_at。固定只读元数据查询，不接受客户端 SQL |
| `POST /data-source-previews` | data_source_id、package_ref；只读预览对象绑定、实体实例与关系数量、缺失列、警告和截断标志；不发布或写入外部数据 |
| `POST /graph-import-checks` | data_source_id、package_ref、可选 preparation_id；只读检查 Workspace 与 Ontology 同时匹配的节点、范围内关系和跨范围关系，返回 id、scope、计数、expires_at、import_state；启动用检查必须绑定 preparation_id |
| `POST /inputs` | multipart file，CSV/XLSX，返回受控句柄和摘要 |
| `GET /inputs/{id}` | 当前主体上传文件的元数据，不返回内容 |
| `POST /package-preparations` | request_id、package_ref、可选 data_source_id；冻结输入、生成 mapping、必要时发布快照。同一基础版本加相同 mapping 复用已提交的评测快照，不再占用新版本号 |
| `GET /package-preparations/{id}` | 状态、固定包引用及有界映射预览 |
| `POST /package-publications` | request_id、preparation_id；创建正式 mapping 派生版本，返回 Hub 回执 |
| `GET /package-publications` | 分页查询本地发布任务及 Hub 回执 |
| `GET /package-publications/{id}` | 本地持久发布任务状态；任务 ID 可由发布查询列表获得 |
| `POST /runs` | request_id、preparation_id、可选 run_type（`data` 默认，或 `solver` 求解验证）。数据评测需 eval_set_id、可选 eval_set_revision；求解验证需 solver_set_id、可选 solver_set_revision，见下文“求解验证”。Neo4j 导入还需 graph_check_id，存在旧数据时必须显式传 replace_existing_graph=true。可选 concurrency（同时发送给 consumer 的问题数；数据评测 1 到 16、默认 4，求解验证 1 到 4、默认 1）与 model_profile_id（`global:<id>` 或 `user:<id>`，consumer 全局模型池或当前用户个人模型池中已启用的模型，留空用 consumer 默认模型）。活动任务冲突、版本冲突和缺少确认在创建前拒绝，持久化后返回 202；所选模型在执行时已停用则轮次中断并提示 model_unavailable |
| `GET /consumer-models` | consumer 全局模型池和当前用户个人模型池中已启用的模型（合计最多 100 个），字段 id、name、model、provider、is_default、scope（`global` 或 `user`）；consumer 不可用时返回 `available: false`，新轮次使用 consumer 默认模型 |
| `GET /runs`、`/runs/{id}` | 摘要、进度和统计；`summary.score` 为报告口径的得分（通过 1、部分通过 0.5，只算有效判分的用例，没有有效用例或反馈轮次为 null），`summary.strict_pass_rate` 为通过 ÷ 全部用例（页面称“通过率”），`judged_pass_rate` 保留兼容、页面不再显示；列表按 offset 分页，可选 limit（1 到 20，默认 20），可选 ontology_id 只列该知识包的执行记录；`origin=consumer_feedback` 加 ontology_id 列出该包的消费反馈轮次（须在 Hub 上能看到该包）。每项带 `origin`（evaluation / consumer_feedback）和 `optimization`（建模处理进展，见下文）；反馈轮次另带 `feedback`（统计、问题簇、用户建议）。新任务保留 eval_set_id、eval_set_name、data_source_name、created_at、started_at、updated_at、finished_at。`base_ref` 为用户选择的发布版本，`package_ref` 为实际运行的包（mapping 变化时为评测快照）；本字段之前创建的任务由其准备记录补出 `base_ref`，仅用于返回，不改写已存记录 |
| `GET /modeling-projects` | 重复的 ontology_id，每次 1 到 20 个；以当前用户登录向建模服务查询关联项目名称，仅供展示、不落盘。返回 `available` 与 `items[{ontology_id,projects,total}]`，每包最多 5 个名称；建模不可用或拒绝时返回 `available: false`，不报错 |
| `GET /runs/{id}/results` | 有界分页用例摘要；执行异常、判分异常的用例带 `error`（code、message，message 截断到 256 字符）。执行异常的原因取自 consumer 运行状态与错误码，如 `consumer 执行失败（错误码）：说明`、等待用户补充信息、完成但未返回回答；`SESSION_PREPARE_FAILED` 会换新会话重试，最终仍失败时说明末尾注明“已换新会话重试 N 次” |
| `GET /runs/{id}/results/{case_id}/answer` | 单用例完整答案、判分、改判历史及未获得答案时的完整原因（不超过 1000 字符） |
| `GET /runs/{id}/results/{case_id}/evidence` | 从 consumer 按需读取有界执行事件索引，不复制完整工具报文；反馈轮次没有 consumer 运行，返回 unavailable |
| `GET /runs/{id}/report` | 可重建的统计投影、包与模型引用 |
| `POST /runs/{id}/cancel`、`/resume`、`/credentials` | 取消意图、对账恢复或更新内存授权；反馈轮次返回 not_applicable（409）。恢复后立即返回准备中的轮次。与 `POST /runs` 一样可带请求头 `X-Refresh-Token`（不超过 8 KiB），执行引擎只在内存中保存，用于 access token 到期前自动续期；登录失效时轮次以 `login_expired`（401）进入待授权 |
| `POST /runs/{id}/results/{case_id}/override` | verdict、reason、revision；保留审计，不允许无答案或活动任务改判；求解验证返回 override_unsupported（409）。改判只重建报告，优化建议标 `suggestions_stale`，用户点“重新生成”时才重跑。反馈轮次在待负责人确认时可改判，可带 reference（≤4000 字）补标准答案，判为 pass 即“用户误判”，不进入优化建议；改判后该轮优化建议立即重新生成；已提交交付且建议正在生成时返回 invalid_state（409） |
| `DELETE /runs/{id}` | 删除评测记录并清理自有资源，清理任务可重试。反馈轮次封板后可删除，其问题簇回到就绪并进入下一个反馈轮次。已结束（completed / cancelled / failed）可删除；interrupted / waiting_auth 在没有 executing / unknown 用例时可删除；执行中或仍有未对账提交返回 run_active（409），须先停止评测 |
| `GET /package-tasks` | 按知识包合并的任务列表：每个知识包的最新一次执行、执行次数及是否有执行中的评测，按 offset 分页；`origin=consumer_feedback` 列出调用者在 Hub 上能看到、且有反馈轮次的知识包，每项带 `awaiting_review`（待负责人确认的轮次数）、`awaiting_cases` / `awaiting_suggestions`（这些轮次中的问题数和用户建议数）、`awaiting_since`（其中最早的创建时间）、`collecting_clusters`（正在收集的问题簇数）、`handed` / `handed_since`（已交付、建模还没处理且没有被后续交付带上的交付次数及最早时间）、`status`（pending / delivering / notified / failed / collecting / done / idle）、`suggestions`（待确认部分的优化建议：generating / ready / failed）、`latest_result`（最近一次建模回写，不含随其他交付一起记录的）、`total_cases` / `total_suggestions`、`last_received`。请求带 `X-Refresh-Token` 时，对待负责人确认、还没有优化建议的反馈轮次，用调用者的登录在后台生成（判分模型为默认配置） |
| `GET /package-evaluations` | ontology_id；每个发布版本最近一次已生成报告的数据评测 `releases` 与求解验证 `solver_releases`（各最多 100 个版本，条目带 run_type，求解验证另带 solver_summary）及该知识包执行中的评测（active 带 run_type），供建模服务对账；`owner` 为调用者的所有者标识，与完成通知中的 owner 相同，建模服务据此按用户存放评测状态。每条带 `origin`、`optimization`；`origin=all` 时 `releases` 另含每个版本最近一个已确认的消费反馈轮次 |
| `GET /runs/{id}/artifacts`、`/runs/{id}/artifacts/{name}` | 报告与优化建议的状态、建模通知投递状态；name 为 report.md、answers.json、context.json、suggestions.md，只返回当前修订，响应头带 `X-Artifact-Revision` 与 `X-Artifact-Stem`。状态中的 `carryover` 为本轮文件带上的历史轮次（见下文“建议拼接”），没有时为 null；拼接前的本轮建议只在内部保存，不能下载 |
| `GET /runs/{id}/carryover` | 本轮文件带上的历史轮次（`preview=false`）；反馈轮次待负责人确认时，返回现在确认会带上的轮次（`preview=true`）。`carryover` 含 `rounds[{run_id,version,handed_at,cases,items,legacy}]` 与 `slid_out[{run_id,version}]`，没有时为 null |
| `POST /runs/{id}/artifacts/regenerate` | 已停止且有用例结果的任务重新生成报告与优化建议，修订号加一；执行中返回 run_active（409）。可带 `X-Refresh-Token`，排队中的优化建议到期前自动续期。反馈轮次收集中时先封板；用调用者登录读取知识包生成建议，判分模型为默认配置；版本未发布到 Hub 时返回 release_unpublished（409） |
| `POST /runs/{id}/notifications/retry` | 立即重发该任务最新一条完成通知（含已被拒收和已失败的），重新计数；已送达的只返回状态；未配置通知凭据返回 notification_disabled（409）。反馈轮次待负责人确认时，请求体 `{"decision":"confirm"}` 确认并通知建模（向 Hub 查出该版本的 release_id），`{"decision":"dismiss","reason":"…"}` 驳回。确认时若优化建议还没生成或已过期，先重新生成并在 `optimization.handover` 记下交付请求，返回的状态仍为 `awaiting_review`；同一知识包所有提交了交付的轮次都生成完后，按收到先后自动交付。交付时若有同一知识包已交付、建模还未处理的反馈轮次，报告、回答、上下文和建议拼接后以新修订重写，再通知建模 |
| `POST /runs/{id}/modeling-result` | 建模回写处理结果：request_id、outcome（done / failed）、done 时 release_id、可选 note（≤2000 字）。done 时用调用者登录向 Hub 核对版本存在且不是被评测的版本（release_not_found / same_release，422）；轮次须已交给建模（invalid_state，409）；同一 request_id 重试返回原结果。本轮推送带上的、仍待处理的历史轮次记为同样的结果（`result.via` 为本轮 ID，note 注明“随某轮一起处理”），重试时补齐未完成的部分。只记录，不触发评测 |
| `GET /internal/feedback-signals/health` | 消费侧连通性检查：令牌要求同 `/internal/feedback-signals`；先校验服务令牌（不符 403 `service_forbidden`，此时不调用账号服务），再校验用户令牌（无效 401 `invalid_identity`，需先改密码 403 `account_action_required`，账号服务不可用 503 `dependency_unavailable`）；反馈接收关闭时也返回 200，`feedback_enabled=false`；返回 `status`、`feedback_enabled`、认得的信号格式 `accepted_schemas` 和 `server_time`（UTC）；不加跨域头 |
| `POST /internal/feedback-signals` | 消费推送反馈：`Authorization` 为提交者的用户令牌，`X-Eval-Service-Token` 为消费服务凭据（`EVAL_FLYWHEEL_SERVICE_TOKEN`，错误返回 service_forbidden 403）。请求体 `{"schema":"consumer-feedback-signal-batch.v1","signals":[…]}`，每批 1–50 条 FeedbackSignal v1（未知字段忽略，单条 ≤256 KB）。返回 202 与逐条 `accepted` / `duplicate` / `rejected`（schema_invalid、actor_mismatch、signal_too_large）/ `conflict`（payload_mismatch）。`EVAL_FEEDBACK_ENABLED` 未开启时 feedback_disabled（503）；用户令牌无效 invalid_identity（401） |

评测停止、人工改判或重新生成后，评测服务按修订号生成报告与优化建议（只保留最新修订）：报告、回答和上下文立即生成，优化建议在单独的队列排队（`artifacts.queue` 为前面还有几个），不会挡住其他轮次的报告。完成后向建模服务 `POST /api/internal/evaluation/notifications` 发送完成通知，服务凭据放在 `X-Evaluation-Service-Token` 头。通知先写入本地发件箱再投递，失败按指数退避重试（最长 1 小时），400/413/422 视为拒收不再自动重试；401/403/404 重试 3 次、其他错误累计 `EVAL_NOTIFICATION_MAX_ATTEMPTS`（默认 30）次后置为 failed，页面提示检查建模地址和通知凭据；建模服务按 run_id 与 sequence 去重。通知只含版本、状态、得分汇总、`origin` 与 `optimization`，不含报告正文，建模服务查看时以用户自己的登录读取。通知带 run_type（data / solver）；求解验证另带 solver_summary（total、passed、failed、first_failure_code、first_failure_message），只生成确定性的修复指引，不排队生成优化建议。反馈轮次在负责人确认后才通知，`owner` 为确认人，且须开启 `EVAL_NOTIFY_FEEDBACK_RUNS`。

`optimization.status`：`awaiting_review`（反馈轮次待负责人确认）、`notified`（已交给建模，评测轮次生成报告后即为此状态）、`modeling_done` / `modeling_failed`（建模回写结果，再次回写时上一次进入 `history`，最多 10 条）、`dismissed`（负责人驳回）。待处理的轮次被较新一轮带上时记 `carried_into`（run_id、at），因文件超过大小上限没有带上时记 `slid_out`，以最近一次推送为准。

建议拼接：建模对每个版本和来源只保留最新一轮。一轮交给建模时（评测轮次生成报告时，反馈轮次负责人确认时），评测把同一来源、同一知识包版本（比较 `base_ref.release_id`，没有时比较版本号）中仍为 `notified` 的较早轮次带上：评测来源为同一用户，反馈来源为已交付的反馈轮次；两种来源、不同版本都不互拼；`modeling_failed` 的轮次不带。不限轮数；从新到旧以最新结论为准，本轮或较新轮次已判定的问题、只归因为运行异常的问题不带。历史问题接在 report.md「逐用例明细」之后并顺延编号，answers.json 同步追加；历史建议放在 suggestions.md「历史未处理建议」一节，不重新调用判分模型。总览、得分和通知 `summary` 只算本轮。任一文件超过大小上限时从最早交给建模的一轮起少带，少带的保持待处理并记 `slid_out`。没有待处理的历史时，四个文件与不拼接时逐字节相同。通知与 `package-evaluations` 只多出 `artifacts.carryover`、`optimization.carried_into` / `slid_out` / `result.via` 等新字段，建模按未知字段忽略。

映射冲突（mapping_conflict，422）的错误带 `details.issues`：最多 20 个与数据表不一致的对象，每项 object、kind（declared_missing / no_columns）、table、candidates、message。

调用 consumer 提问时遇到 429、503、连接超时或失败，按 Retry-After（没有则 5 秒起翻倍，最长 60 秒）重发同一幂等键，最多 6 次；仍失败的用例保留原状态和原因，连续 3 条同类失败才停止提交（dependency_circuit_open）。单题在 consumer 侧 15 分钟未结束时不再等待，结束时仍有未完成用例则轮次中断（cases_unsettled），恢复只处理这些用例。提问和取消请求带 `X-Evaluation-Service-Token`，标记为评测流量。

Hub 查询默认每页 20、最多 100；本地列表每页 20，按 next_offset 翻页。用例列表截短问题/理由，完整答案单独读取。映射预览最多 20 个对象，有 truncated 标志。consumer 单次响应最多 2 MiB、事件索引源最多 1 MiB/输出最多 100 项；超限明确失败，不伪装成完整结果。

新表单的 NL2SQL 数据源保存传 inspection_id，结构由服务端读取结果填入；句柄须属于当前主体且与 Provider / 连接指纹一致，不能把旧库结构套用到新连接。每次读取最多 100 张表、每表 256 字段，每批响应最多 1 MiB；空结构、权限失败、重复分页或超限明确报错。外部服务地址和凭据不进入错误正文。旧客户端的 connection / tables 请求仍按原契约处理。

Neo4j 查询模式允许无 tables；导入模式需要文件和明确的 import_data。表单支持 workspace_property / workspace_value / ontology_property，ontology_value 在执行时由固定知识包补入。新建表单默认 `d2o_workspace_id` / `d2o_ontology_id`，与 consumer 图数据的范围字段一致；保存时 workspace_property 留空按 `d2o_workspace_id` 处理。图导入预览最多 100 个对象、100 条关系与 20 条提示，truncated 标记不完整结果。节点实例按主键去重；关系按源 / 目标主键与关联字段统计，预览不提交任何图写入。

生成 mapping 时，对象主键以发布包自带的 mapping 为准（主键列都能对上本次数据时采用，否则按对象文档推断），复合主键按全部主键列导入；主键列有空值时返回 graph_key_missing（422）。

导入完成后在图中的回执节点记录本次数据与映射的指纹及导入后的节点、关系快照（含标签、关系类型与属性的摘要，属性只参与计算，不保存也不返回）。`import_state` 对比准备结果与回执：`empty` 范围为空；`unchanged` 数据、映射与图快照均一致，启动时不删除也不导入；`data_changed` 上传文件变化；`mapping_changed` 文件未变而对象映射或关系变化；`graph_changed` 导入后图数据被增删或属性被修改、出现跨范围关系，或无法确认未被修改；`unrecorded` 范围内数据没有导入回执；`unknown` 检查未绑定准备结果。除 `empty`、`unchanged` 外须确认替换（`replace_existing_graph`），不能沿用图中现有数据。

Neo4j 导入使用配置的 Workspace 值与知识包 Ontology 值同时匹配。`graph-import-checks` 使用固定只读查询，不接受客户端 Cypher，返回值只有范围、计数和受控检查句柄，不返回节点属性或凭据。检查最多 10 万个节点、20 万条关系，有效期 10 分钟；句柄绑定当前用户、准备结果和冻结数据源。存在跨范围关系时拒绝替换，未确认时不删除也不导入。执行前核对图范围指纹，变化时返回 graph_scope_changed 并中断；清理范围内关系、删除节点和导入在同一事务中完成。导入回执与数据同事务保存，恢复先查回执，避免因提交响应丢失再次删除新数据。旧的 eval_<run_id> 图范围保持原样，不纳入配置范围的替换。缺检查、未确认、过期、范围冲突分别返回 graph_check_required、graph_confirmation_required、graph_check_expired、graph_boundary_conflict（409）。

同一图数据范围的活动导入评测互斥，冲突返回 graph_scope_busy（409），提示不暴露其他账号或任务身份。恢复已完成图导入的任务时还会核对当前范围的导入回执；被后续任务替换时要求重新创建评测，避免旧任务继续读取新一轮的数据。

普通请求体最多 1 MiB，数据上传最多 50 MiB，实际接收大小也检查。Hub ZIP 最大 256 MiB、展开最大 1 GiB、成员最多 10000，拒绝路径穿越、符号链接、重复路径及过高压缩比。

错误格式 `code,message,phase,retryable`；依赖原始错误、连接地址和秘密不回显。鉴权失败 401/403，不存在或无权访问本地记录 404，并发或幂等冲突 409，过期 410，超限 413，格式错误 422，依赖不可用 503。依赖的拒绝只按其错误码细分，不转述正文：消费服务因评测工作空间中已有同名但本体 ID 不同的知识包而拒绝安装时，任务以 `package_name_conflict`（409，阶段 `package_install`）中断，提示清理旧知识包或更换评测工作空间后恢复；评测服务不删除用户配置的工作空间中的内容。

准备/发布/启动使用稳定 request_id，同键不同输入冲突；超时沿用原请求。Hub 发布与快照命名空间分开，服务不生成本地成功回执。数据库删除保留请求墓碑，不能通过重复请求重新创建已删轮次。

可选 NL2SQL：`POST /nl2sql/api/v1/query`、`/sql`。请求使用专属 scene HMAC 凭据，不使用用户登录；只允许本轮 eval 库的受限 SELECT、最多 1000 行。连接配置仅作为 consumer 运行设置，不进入 Hub 包。

## 求解验证

求解验证检查固定版本的求解能力能否在 consumer 中跑通，不经 Judge，也不需要判分模型配置。

- 求解验证集（`/solver-sets`）：name 与 1 到 20 条用例。用例字段 id（`^[A-Za-z0-9_-]{1,64}$`，集内唯一）、name（1 到 80 字）、solver_key（`^[a-z][a-z0-9_]{0,31}$`）、arguments（JSON 对象，规范化后不超过 64 KiB）、可选 question（不超过 4000 字）、可选 expected_candidate_count（0 到 100000）；其他字段返回 case_invalid（422）。
- 启动前校验：数据源必须是 Neo4j（solver_data_source_unsupported）；版本没有启用的求解操作返回 solver_validation_not_required；用例引用不存在或未启用的操作返回 solver_cases_invalid；有启用操作没有用例返回 solver_cases_incomplete；只连接不导入且图范围内没有节点返回 solver_graph_data_empty（均 422）。
- 用例在轮次中按位置编号（id 为 "0"、"1"…），集内标识保存在 case_id。question 留空时按求解操作与入参生成；expected_candidate_count 留空时，profile 为 `ordered_binary_route.v1` 且 arguments.items 为 1 到 64 项则取 2×项目数，否则不核对候选数。
- 执行：提问时固定本轮激活的知识包版本；安装时 consumer 报告不可用的求解操作，用例直接判为未通过而不提问。每条用例读取一次运行事件（最多 4 MiB），按工具卡名识别求解调用，同一操作多次调用以最后一次为准；入参比对读取 `tool.started` 的入参预览，预览被截断或脱敏时判为 solver_arguments_unverifiable。权威结果且有候选结果引用时读取 consumer `/api/solver/history/{job_id}/candidate-results` 的候选数。
- 断言顺序与失败码：solver_tool_not_called → solver_arguments_unverifiable / solver_arguments_mismatch → 求解未完成（consumer 返回的码、awaiting_input、solver_timeout）→ solver_authoritative_event_missing / solver_authoritative_event_mismatch → solver_result_not_authoritative → solver_evidence_incomplete → solver_candidate_count_mismatch（或候选结果读取错误码）。不可行结果没有候选时候选数断言视为通过。
- 结果：用例 judgement 为 pass / fail，带 code；`solver` 字段为公开证据与断言列表，不含令牌与连接信息；`/results` 列表不返回 arguments。报告与修复指引按失败码确定性生成，`suggestions_model` 为空。

### 交付内容与待办展示

- `GET /runs/{id}/delivery?scope=all|own|history&offset=0&limit=20`：沿用轮次权限；返回当前报告修订的交付问题及来源 `source_run_id`，不改写原判分。每页最多 20 条，问题与理由各最多 1000 字符、回答预览最多 2000 字符；完整内容见报告附件。反馈待确认时使用后端拼接、去重和大小限制计算预览，`preview/provisional=true`，其他批次交付或内容更新后可能变化。正式交付内容从已保存附件读取。
- 轮次增加 `delivery`：`state` 为 `none`（无需优化）、`actionable`（有待处理内容）、`diagnose`（需诊断）、`pending`（产物未就绪）、`unknown`（历史证据不足）或 `solver`；`own/history` 分列本轮及历史数量。此字段仅用于展示，不改变建模回写协议；通知状态仍单独读取 `notification`。
- `GET /package-tasks` 增加 `pending_deliveries`、`historical_failures` 和 `history_limited`，汇总最近最多 1000 次评测，已并入其他交付的记录不重复计待办。
- 按知识包查询 `GET /runs` 可指定 `run_type=all|data|solver`，按类型筛选后分页。
- 用户反馈知识包汇总增加 `transport`：分别统计已送达、待同步、同步失败、无需优化的独立交付；`status` 原协议保持兼容，页面结合两者显示。

### 关系结构检查（Issue #367）

`POST /package-preparations`、使用旧准备结果创建 `POST /runs`、包拉取及映射生成均检查原始对象关系。关系名保持读取兼容，自关联允许；重复身份由源对象、关系名、目标对象及两端关联字段组成。结构错误返回 HTTP 422，`code=EVALUATION_PACKAGE_INVALID`、`phase=package_validation`；`details.problems` 含对象文档路径、行号及原因（最多 100 项），`details.total` 为总数。准备结果中的 `relation_validation` 包含 `rule_version` 和 `package_digest`。该记录仅由服务端生成，不接受客户端声明已校验。
