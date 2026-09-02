# 建模服务重启后的 Run / 计划恢复一致性修复方案

## 状态

本文针对平台建模服务重新部署后出现的「幽灵计划」流程死锁问题，记录根因分析、修复设计与实现要点。
对应现象：页面仍展示建模计划「已批准」，继续执行时后端找不到计划；重新规划被「当前运行已有进行中计划」阻断；
知识包工作修订冲突同时出现，旧计划不可执行、新计划不可创建。

本文只讨论建模服务重启后的 Run、计划、Checkpoint、任务回执与知识包工作修订恢复一致性，
不讨论模型生成的业务规则质量，也不放宽知识包修订并发保护。

相关背景：

- [Agent 受控修改、计划生成与浏览器验收方案](agent-controlled-change-set-problems-and-browser-e2e.md)
- [Agent 领域工具收敛与候选修复闭环方案](agent-domain-tool-convergence-and-repair-plan.md)

## 1. 问题概述

### 1.1 现象

单用户、单页面、前台业务专家路径下（无并发提交、无刷新、无脚本修改知识包）：

1. 原计划包含 6 项任务并已执行候选生成，随后追加 3 项规则修订，页面展示为 9 项计划且状态为「已批准」；
2. 建模服务重新部署后继续执行，依次出现：
   - 「建模计划不存在或不属于当前运行」（`CHANGE_PLAN_NOT_FOUND`）；
   - 「知识包工作修订已经变化，请刷新后重试」（`WORKING_REVISION_CHANGED`）；
   - 「当前运行已经有一份进行中的建模计划」（`CHANGE_PLAN_ALREADY_ACTIVE`）；
   - 任务门禁仍统计 9 项必选任务未完成，已执行任务的回执未被恢复。

### 1.2 影响

- 已批准计划无法继续，用户不能完成 AI 辅助建模；
- 旧计划持续占用当前运行，不能通过重新规划自恢复；
- 页面状态与后端实际状态互相矛盾，容易导致用户重复操作；
- 若直接清理旧状态，可能丢失已完成任务的候选内容和审计链；
- 周版本端到端测试被阻塞。

### 1.3 非目标

- 不将服务重新部署本身视为缺陷；缺陷是重新部署后未能安全恢复或明确失败；
- 不讨论模型生成的业务规则质量；
- 与资源 ID 契约类问题相互独立。

## 2. 根因分析

### 2.1 Python 侧：`recover_interrupted()` 只转状态、不对账事实

`modeling/modeling-agent/modeling_agent/store.py` 的 `recover_interrupted()` 在服务重启时
只把 `running/stopping` 状态转为 `suspended`，存在以下缺口：

- 不校验 `agent_runs` 记录是否仍然存在。若运行记录缺失，`project_agent_state.run_id` 仍被保留，
  而 `suspended ∈ ACTIVE_RUN_STATUSES`，导致 `active_run()` / `create_run()` 持续返回 `RUN_ACTIVE`，
  形成「幽灵占用」；
- 不校验 `checkpoints.sqlite` 中对应 session 是否存在。若检查点数据卷被更换或缺失，
  OpenJiuwen 会从空白继续，而 `agent_tool_receipts` 仍留在 `runtime.sqlite`，
  已完成任务可能被重新统计或重算；
- 自动恢复失败只发布事件，不落可诊断状态，页面与后端状态继续矛盾。

### 2.2 两端 run_id 错位：计划挂在旧 run 上

- Python 侧每次 `start_run()` 生成新的 `run_{uuid}`；
- Node 侧 `claimCandidateWorkspace()` 会把候选工作区与 active plan 转移到新 run（`transferActiveRun`）；
- 但 `resetWorkspaceToCurrent()`（知识包修订变化触发的重建路径）与 `ensureWorkspace()` 的新 run 分支
  只更新工作区与候选内容，不转移 active plan；
- `getChangePlan()` / `snapshot()` 走 `requirePlan(project_id, run_id, plan_id)` 严格校验 run_id，
  计划仍挂在旧 run 时直接返回 `CHANGE_PLAN_NOT_FOUND`（现象 1）；
- 新 run 上无 plan 时 `createPlan()` 本可创建，但 Python 侧状态仍被旧 run 占用（现象 3），
  形成「旧计划不可执行、新计划不可创建」的双重死锁。

### 2.3 页面「已批准」来自前端缓存

页面计划状态来自事件流中缓存的 `block.plan`（`modeling/frontend/src/workbench/right/blocks/Blocks.jsx`），
与后端计划 / Run 状态不同步；`ProjectAgentState.public()` 此前没有恢复诊断与修复入口字段。

### 2.4 工作修订冲突只提示刷新

`WORKING_REVISION_CHANGED` 已携带 `expectedRevision` / `actualRevision`，但提示文案仅要求刷新，
没有引导「基于当前修订显式重新基线化」，也没有说明 expected/actual 差异。

## 3. 修复方案设计

### 3.1 Python 侧：恢复一致性对账与 stale 标记

#### 3.1.1 状态数据模型

`project_agent_state` 新增 `recovery_status` 列（`TEXT NOT NULL DEFAULT ''`），
取值：

| 值 | 含义 |
| --- | --- |
| `''` | 无待处理恢复事项 |
| `stale_run` | 状态引用的运行记录已丢失（幽灵占用） |
| `checkpoint_missing` | 检查点数据库缺失或不可读 |
| `resume_failed` | 自动恢复失败，运行停在最近检查点 |
| `volume_mismatch` | 持久化自检未通过（两个数据库不同卷或半恢复） |
| `plan_unreadable` | 计划记录存在但当前 run 不可读（Node 侧对账结果） |

`ProjectAgentState.public()` 在 `recovery_status` 非空时输出 `recovery: { status }`，
`project_state()` 聚合后补充 `missing` 与 `nextSteps`。

#### 3.1.2 `recover_interrupted()` 对账

对每个处于 active 执行态（`running/stopping/suspended/queued`）的项目状态：

- run 记录存在且无待答交互：run 转为 `suspended`，状态转为 `suspended`，进入可自动恢复列表；
- run 记录存在且有待答交互（`waiting`）：保留原状态，不自动恢复，等用户回答；
- run 记录缺失：标记 `recovery_status = stale_run`，保持占用但不自动恢复，
  写审计 `recovery_stale_run`，不进入自动恢复列表。

#### 3.1.3 `create_run()` 的 stale 识别

当执行态属于 `ACTIVE_RUN_STATUSES` 时：

- run 记录存在 → 维持 `RUN_ACTIVE`；
- run 记录缺失 → 返回 `RUN_STALE_OCCUPIED`（409），携带
  `recovery: { status, missing: ["run"] }` 与 `nextSteps: ["安全终止失效计划后重新规划"]`。

不再「同时拒绝旧计划与新计划」：用户可先安全终止失效占用，再重新规划。

#### 3.1.4 安全终止失效占用

新增 `terminate_stale_occupancy(project_id)`：

- 仅在「执行态为 active 且 run 记录缺失」时生效；
- 状态迁移为 `{phase}:cancelled`，清空 `run_id` / `interaction_id` / `checkpoint_id` / `recovery_status`；
- 写审计 `stale_occupancy_terminated`，保留 `agent_tool_receipts`、Node 候选内容与审计链；
- run 记录仍存在时拒绝（必须走正常取消入口）。

新增 `mark_recovery_status(project_id, status)`：在锁内仅更新恢复诊断，不改变状态文本，
供自动恢复成功/失败与存储自检写入诊断。

### 3.2 Python 侧：持久化自检与自动恢复门禁

#### 3.2.1 存储自检（`service.py`）

`startup()` 在 `Runner.start()` 后执行 `_storage_diagnostic()`：

- 校验 `runtime.sqlite` 与 `checkpoints.sqlite` 位于同一目录（持久卷）；
- 记录数据卷身份（`data_dir/.modeling_agent_identity` 中的 `volumeId`）与新建标记；
- 统计 Run 数量与 Checkpoint 数量（只读打开两个 SQLite，不对 Checkpoint schema 做假设）；
- 判定规则：两个数据库不同卷、`runtime.sqlite` 缺失、或「存在运行记录但 `checkpoints.sqlite` 缺失」时，
  标记 `healthy = false`；
- 诊断结果缓存在 `health()` / `project_state()` 中暴露。

#### 3.2.2 自动恢复门禁

- 存储自检不通过 → 对所有已恢复 run 标记 `volume_mismatch`，阻止自动续跑；
- `_auto_resume_recovered()` 增加 `_checkpoint_available()` 校验：
  检查点数据库文件缺失或不可读 → 标记 `checkpoint_missing` 并停止；
- 自动恢复失败（非 `CORE_UNAVAILABLE` 或重试耗尽）→ 标记 `resume_failed`；
- 自动恢复成功 → 清除 `recovery_status`，发布「恢复完成，正在从原进度继续」。

### 3.3 Python 侧：诊断暴露与用户入口

- `project_state()` 聚合 `state.recovery`：`{ status, missing, nextSteps }`；
- `health()` 暴露 `storage`（自检明细）与 `recovery`（卷身份、Run/Checkpoint 计数、检查时间）；
- `cancel()` 检测到 stale 占用时自动走安全终止路径，返回 `terminatedStale: true`；
- 新增路由 `POST /v1/projects/{project_id}/recovery/terminate-stale`，
  调用 `service.safe_terminate_stale()`，终止后返回可重新规划的状态。

### 3.4 Node 侧：计划迁移与 stale 诊断

#### 3.4.1 工作区接续时迁移 active plan

- `ensureWorkspace()` 的新 run 分支：`existing.run_id !== context.runId` 时，
  在事务内先调用 `transferActiveRun(project_id, existing.run_id, context.runId)`；
- `resetWorkspaceToCurrent()`（知识包修订变化的重建路径）：同样先迁移 active plan，
  避免计划悬挂在旧 run 上形成「幽灵计划」；
- 迁移后，新 run 上 `getChangePlan()` 可读到计划；若工作修订已变化，
  后续任务以 `WORKING_REVISION_CHANGED` 显式提示重新基线化，而不是找不到计划。

#### 3.4.2 `requirePlan()` / `snapshot()` 的 stale 诊断

- `requirePlan()` 先按 `plan_id + project_id` 查计划；若计划存在但 `run_id` 不匹配：
  - 计划仍为 `active` → 返回 `CHANGE_PLAN_STALE_RUN`（409），
    携带 `plan_id`、`expected_run_id`、`current_run_id` 与下一步指引；
  - 计划已结束 → 维持 `CHANGE_PLAN_NOT_FOUND`（404）。
- `snapshot()` 未指定 `plan_id` 时，当前 run 无 active plan 但项目存在其他 run 的 active plan →
  返回 `CHANGE_PLAN_STALE_RUN`，而不是裸 404。

#### 3.4.3 安全终止失效计划

新增 `safeTerminatePlan(project_id, plan_id, actor)`（`change-control.ts`）：

- 仅允许终止 `active` 计划（含挂在其他 run 的失效计划）；
- 计划置 `cancelled`、未结算任务置 `cancelled`、未结算 Change Set 置 `cancelled`；
- 不删除候选内容、回执与审计，写入 `plan.safe_terminated` 事件；
- 终止后 `activePlan()` 不再占用，可重新 `createPlan()`。

`core-service.ts` 新增 `safeTerminateChangePlan()`；`core-routes.ts` 新增路由
`POST /api/projects/:id/agent/change-plans/:planId/safe-terminate`。

### 3.5 工作修订冲突处理

`WORKING_REVISION_CHANGED`（`studio-read-service.ts`）提示改为
「知识包工作修订已经变化，请基于当前修订重新基线化计划后再继续」，
保留 `expectedRevision` / `actualRevision` 并在 `next` 中引导查看差异后重新规划；
不绕过人工批准自动换 revision 重试。

## 4. 接口与行为变更清单

### 4.1 错误码

| 错误码 | 触发条件 | 状态码 |
| --- | --- | --- |
| `RUN_STALE_OCCUPIED` | 状态占用但运行记录丢失时尝试创建新运行 | 409 |
| `CHANGE_PLAN_STALE_RUN` | 计划存在但挂在其他 run（半恢复） | 409 |

### 4.2 路由

- `POST /v1/projects/{project_id}/recovery/terminate-stale`（Python 建模服务）；
- `POST /api/projects/:id/agent/change-plans/:planId/safe-terminate`（Node Core）。

### 4.3 状态字段

- `project_agent_state.recovery_status`（新增列，含旧库迁移）；
- `ProjectAgentState.public().recovery`（诊断对象）；
- `health().storage` / `health().recovery`（自检与卷信息）。

## 5. 测试方案

### 5.1 Python（store 层离线可测）

`modeling/modeling-agent/tests/test_store_and_safety.py` 新增：

- stale-run 诊断：run 记录丢失 → `recover_interrupted()` 不返回、状态标记 `stale_run`、
  `create_run()` 抛 `RUN_STALE_OCCUPIED`、`terminate_stale_occupancy()` 后可重新规划，
  审计保留（`stale_occupancy_terminated` → `run_started`）；
- waiting 交互保留：重启后 `intake:waiting` 与待答交互不变，不进入自动恢复。

### 5.2 Node（需 node_modules，CI 运行）

`modeling/tests/agent-recovery-consistency.test.ts` 新增：

- 工作区接续把 active plan 转移到新 run，旧 run 访问返回 `CHANGE_PLAN_STALE_RUN`；
- 计划挂在旧 run 时返回可恢复诊断（409）而非裸 404；
- `safe-terminate` 后计划置 `cancelled`，同一 run 可重新创建计划。

### 5.3 验收对照

| 验收项 | 对应改动 |
| --- | --- |
| 计划批准后/执行中/回执落盘前后重启均可从同一 Run 与最近 Checkpoint 恢复 | 3.1.2 + 3.2.2 + Node 3.4.1 |
| 已完成任务不重算、回执不丢不重复 | 保留 `agent_tool_receipts`，恢复同 run 续跑 |
| 任一数据库缺失/不匹配时返回明确诊断，不形成幽灵计划 | 3.2.1 + 3.1.2 |
| 自动恢复期间 core 短时不可用可重试，最终失败进入可操作 suspended | 保留 `RECOVERY_RETRY_DELAYS` 重试 + `resume_failed` |
| 页面计划/当前 Run/active-plan 占用/任务门禁统计一致 | `state.recovery` + stale 诊断 + 安全终止入口 |
| 安全终止失效计划后可重新规划，候选与审计可查询 | 3.1.4 + 3.4.3 |
| 覆盖正常重启、非正常退出、数据卷更换、Checkpoint 缺失、工作修订变化 | 5.1 + 5.2 |

## 6. 部署与运维注意事项

- 两个 SQLite（`runtime.sqlite`、`checkpoints.sqlite`）必须挂载在同一个持久卷
  （`docker-compose.release.yml` 中 `modeling-agent-data:/data`）；
- 首次启动会在数据目录写入 `.modeling_agent_identity` 记录卷身份；
  更换数据卷后重启会因「存在运行记录但检查点缺失」被拦截并给出诊断；
- 需要人工修复时：先核对数据卷与两个数据库文件是否完整；
  确认旧 run 无法恢复后，调用安全终止入口（`cancel` 或 `recovery/terminate-stale`），
  候选内容与审计记录保留，之后可重新规划；
- 本次改动不引入新的 CI 门禁；恢复诊断结果可通过 `GET /health` 的 `storage` / `recovery` 字段观测。

## 7. 后续建议

- 前端可基于 `state.recovery` 展示「已暂停 / 恢复失败」横幅与「安全终止失效计划后重新规划」按钮
  （可复用 `cancel` 或新路由）；
- 前端计划卡增加服务端 `get_change_plan` 同步，避免缓存「已批准」与后端不一致；
- 将 `volumeId` 对账纳入发布流程检查项，防止数据卷漂移再次造成半恢复。
