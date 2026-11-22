# 0108 知识包数据飞轮接入插件化结构（develop !630）

## Context

**目标**：develop !630（合并 c6ebbb3ed，提交 51ffae45d）上线了知识包数据飞轮：评测服务给出建议后，建模助手按开关自动修订、检查、
发布 Hub 正式版本，再评测新版本，用户关掉页面后由委托在后台继续。设计取舍见 develop 的
[ADR](../../../docs/decisions/2026-11-05-knowledge-package-data-flywheel.md)。这里把它的后端行为搬到 PR555 的插件化结构上，
前端另行接入，所以接口子路径和 JSON 字段沿用 develop。

**与 develop 的结构差异**（11-22 核对，基线 `chore/pr555-merge-develop-r4`）：

1. 接口在 `/api/v1` 下，错误体是 `{code, message, details, trace_id}`，请求校验失败是 `400 invalid_argument`（develop 是 FastAPI 的 422 与 `detail`）。
2. 项目只有一个所有者（`ownership.owns`），没有成员与只读角色；归档项目只读，写接口 `409 project_archived`。
3. 反馈演进包连同 `/ontology-evolution`、`check_eval_generalization.py` 已按 ADR-0101 删除，评测优化（ES-07、ES-08、ES-12）
   与外部反馈批次一起去掉。飞轮离不开这条命令。
4. 发布按提交导出只读树（`publish.tree.release_tree`，默认主线最新提交），工作区里没提交的改动不会发出去；发布前检查由 ADR-0103 的
   发布就绪（readiness）和数据绑定、求解契约组成。
5. 助手在变更集副本里改，平台合入主线后这一轮才算结束；页面命令经会话宿主（S-20）发起，带重复提交与自主构建两道守卫。

## Decision

按 develop 的行为移植，结构服从 PR555：

1. **接口**：`/api/v1/projects/{id}/flywheel`，FW-01～FW-14（API 契约 3.21b），子路径、查询参数、请求体和成功响应的字段与 develop 一致；
   错误码走 v1 错误体，校验失败 `400 invalid_argument`（`details.fields`）。不进旧路径兼容层：develop 的 `/api/projects/{id}/flywheel…`
   在本分支从未出现过。
2. **只给所有者用**：读写都要求项目所有者（`deps.require_owner`），全局管理员也不例外；写接口另要求项目未归档（`deps.require_writable`）。
   委托只替「未归档项目的所有者」登记（`grants.acts_for`），取代 develop 的「凭自己的项目角色能编辑的成员」。
   项目转移、归档或删除后，后台一轮（`background`）撤销旧所有者的委托；页面打开时只重试撤销，不登记。
   develop 里「另一位成员的飞轮在优化或发布时不动工作区」的互斥照搬：飞轮状态仍按（项目，用户）存，转移前后两人的状态可能并存。
3. **评测优化命令回到基础建模能力**：`/ontology-evolution`（含 `--flywheel`、`--flywheel-recheck`）与 `check_eval_generalization.py`
   恢复进 `workspace-toolkit`，版本升到 0.6.2；数据表对账用已有的 `table_profile.py`，不恢复 `check_eval_data.py` 和写表头 CSV。
   只有飞轮调用这条命令，工作台没有「评测优化」入口，ES-07、ES-08、ES-12 仍按 ADR-0101 删除。工作区没装配这条命令时，
   开始优化回 `409 pack_not_enabled`（`details.command`）。
4. **优化经会话宿主发起**：`flywheel/assistant.py` 用 `SessionManager.send` 发命令，`meta` 与页面命令一致（显示文本、来源
   `page=flywheel`、命令名）。重复提交（`message_duplicate`）和自主构建运行中（`autobuild_running`）都转成
   `409 flywheel_assistant_busy`，按「助手正忙」等下一轮；助手是否在忙看 `workspace_busy`（对话、排队、命令链、未合入的变更集）
   加自主构建。变更集合入之前这一轮仍算运行中，所以之后的检查看到的是修订后的主线。
5. **检查加发布就绪**：飞轮的检查在数据绑定、求解契约之外加 `readiness` 一项（ADR-0103 `readiness.mainline_findings`），
   检查没能完成时按未通过算。发布复核又遇到 `PUBLISH_CHECKS` 里的码（如 `script_output_unverified`）时停在「发布条件复核未通过」。
6. **发布读主线提交**：快照、优化结果、回退和中止都提交到主线（`chore: 数据飞轮 …`、`revert: 数据飞轮 …`），发布时
   `hub_packages.publish(expected_revision=…)` 比对导出树的内容 revision，不一致回 `409 revision_conflict`，飞轮记为「候选内容已变化」。
   `details` 与契约 2.4 同形：`{target: "ontology", expected, actual}`。发布输入摘要（`hub_packages.workspace_inputs`）也按主线最新提交的只读树算。
7. **回写只跟飞轮的优化走**：飞轮开始的优化记在项目里（`package_evaluations.remember`），Hub 关联记下的第一个算数版本钉住
   （`pin_release`，任何发布渠道都会经过），用户下次打开飞轮时用其本人的登录回写（`settle`）。不恢复 develop 网页发布后的
   `after_publish` 立即回写、ES-08 标记处理失败和 ES-02 的 `writeback` 字段。
8. **评测服务调用的身份**：`eval_client` 默认只用用户自己的登录；登记、续期、撤销用户本人的委托，或后台凭委托读取时，才同时带
   建模的服务凭据（`X-Modeling-Service-Token`）。补记在 ADR-0092。
9. **后台调度**：`main.py` 的 lifespan 启动 `flywheel.background.scheduler_loop`，停机时与其他后台任务一起取消。不新增部署变量。

## Alternatives considered

- **恢复反馈演进包、由包提供命令**：不取。ADR-0101 删掉六个包是为了少一道「先启用插件」的门；飞轮对所有发布过 Hub 的项目都可用，
  再挂一个包开关等于把这道门装回来。
- **保留 develop 的成员与只读角色**：PR555 只有单一所有者，没有成员角色可以对应；共享在知识包这一层，不碰项目权限。
- **发布工作区当前内容（develop 的做法）**：不取。PR555 发布的是提交，飞轮自己提交修订，检查和发布看到的是同一个提交。
- **恢复 ES-07 手动评测优化**：不在本次范围，ADR-0101 的取舍不变；需要时另开决策。
- **给飞轮另开旧路径兼容**：不取，前端直接用 v1 路径，旧路径兼容只服务 PR555 之前已有的接口。

## Consequences

- 工作区的基础建模能力升到 0.6.2 后才有 `/ontology-evolution`；还没重新装配的项目开始优化时回 `pack_not_enabled`，提示重新装配。
- 主线历史里多出飞轮的快照与回退提交，以及评测产物导入提交（`feedback: 评测优化导入 …`，只提交 `reports/` 下的报告、完整回答与建议）。
- 发布就绪与求解契约的问题可能在检查结果里各列一次。
- 助手的变更集合入冲突、什么都没合进主线时，这一轮按「没有修改」处理（`no_changes`），不单独提示冲突。
- 「另一位成员」的文案和互斥逻辑保留，单一所有者下只在转移前后出现。
- 前端要按 v1 读错误（`message`、`code`），校验失败看 400；非所有者 403 `project_owner_only`，归档项目写操作 409 `project_archived`。
