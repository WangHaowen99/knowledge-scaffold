# 建模助手回显链路与状态机收敛

## Context

知识包建模助手（`modeling/frontend/src/PackageChat.jsx` + `src/ai-construction/orchestrator.ts`）存在三类问题：

1. **回显链路层层加延迟**：SSE 端是 `setInterval(flush, 1000)` 轮询 `project_events` 表而非推送；只有 3 个消息 key 前缀会把完整消息内联进事件，其余都要靠一次完整 `GET /session` 回源；前端同时开两条 SSE 连接，其中 `ProjectWorkbenchRouter` 的 effect 依赖 `session.cursor`，每来一个事件就重连一次；SSE 之上还叠了 2s/4s 轮询。
2. **状态机语义分散**：任务图的合法迁移散落在 7 个 `if` 分支的 `includes([...])` 里；`complete_task` 缺少其他命令都有的里程碑校验；`ready` 和 `waiting_confirmation` 两个状态从未被写入过，却出现在 6 处守卫里。
3. **评测与建模不闭环**：`src/evaluation/**` 对 ai-construction 零引用。评测发现的知识包缺陷回建模只有两条细线——体检面板的 `mw-chat-prefill` 文本预填，和优化候选修订；以及 `ai-construction-workflow.ts` 里一个把构建步骤显示成 completed 的补丁。

## Decision

**回显**

- `AuthoringState.appendEvent` 后经进程内 `EventEmitter` 通知；SSE 端订阅后立即 flush，轮询降级为 2s 兜底（跨进程写入 / 漏通知）。通知只发信号不带内容，订阅方仍按 seq 从表里读，因此漏通知只会退化成轮询。
- 通知在事务提交后发出：`AuthoringState` 缓存待通知的 projectId，用 `setImmediate` 顺延到外层事务提交（最多 5 次）再 emit。
- 消息事件一律内联完整消息（32KB 上限，超限只发信号）。原来的三前缀白名单改为 `progressOnly` 标记：进度类消息前端只贴消息不回源，其余消息贴完仍调度一次刷新以同步任务图。
- 一个项目只开一条事件流（`projectEventStream.jsx` 的 Provider + `useProjectEvents`），从 seq 0 回放以便晚挂载的订阅者重建进行中的流式状态；订阅起点存 ref，不进依赖数组。
- 时间线按「同角色同文本」丢弃已回流的本地乐观消息。

**状态机**

- 任务状态迁移收敛成显式的 `TASK_STATUS_TRANSITIONS` 表（对齐同仓库已有的 `ALLOWED_RUN_TRANSITIONS` 写法），`apply()` 统一校验，所有迁移都要求里程碑 active。
- `GET /session` 改为纯读；对账副作用（补欢迎语、推进停住的编排、恢复中断的知识修复）移到 `POST /session/advance`，由前端在进入工作台时调用一次。
- `POST /package/chat` 改异步：请求只登记用户消息并开跑，结果与失败经消息流返回；忙碌态由 `authoring.activeRuns` 里 `purpose: 'package_chat'` 的运行记录表达，因此刷新页面可恢复。

**评测回建模**

- 新增 `AiConstructionOrchestrator.recordEvaluationDefects()`：把选中的知识包类失败原因登记成
  **`evaluation_repair`** 任务（`waiting_user`），并在助手对话里写一条含缺陷清单和 `next_actions` 的结构化消息。只登记不执行。
- `evaluation_repair` 与 `package_repair` 分开，因为执行引擎和完成判据都不同：

  | | package_repair | evaluation_repair |
  | --- | --- | --- |
  | 输入 | 知识包**校验**发现（`CheckFinding`） | **评测失败用例** + 失败诊断 |
  | 引擎 | 受控修复：按 `automatic / approval_required / expert_decision` 分级，事中问专家 | 优化更新：从用例反推资源改动，事后看整体 diff |
  | 防过拟合 | 不需要 | 强制 `evidence` + `generalizationPlan` + `validationPlan` |
  | 完成判据 | 知识包校验通过 | 重新评测通过 |

  因此 `findActiveRepairTask` 只认 `package_repair`：`settleRepairAfterValidation`、
  `supersedeKnowledgeRepair`、`ensureKnowledgeRepairRecovery` 都不会碰 `evaluation_repair`。
  助手消息的首个动作是 `open_optimization`（跳转优化更新），不是 `repair_knowledge_structure`。
- 新 kind 需要放宽 `ai_construction_tasks.task_kind` 的 CHECK 约束，用
  `ensureEvaluationRepairTaskKind` 原地重建表（沿用 `ensureFlowchartReviewInteraction` 的做法）。
- 建模已完成时没有活动里程碑，复用 `startKnowledgeRepair` 原有的策略——重做最近一次 `package_validation` 把该阶段重新打开（抽成共用的 `ensureRepairMilestone`）。
- 前端在失败诊断面板就近勾选并提交，助手栏（评测阶段一直可见）立即接管，不跳转。

## Alternatives considered

- **SSE 换 WebSocket / Redis pub-sub**：当前是单进程 + 本地 SQLite，进程内 EventEmitter 足够；引入外部消息中间件属于过度设计。
- **事件里不内联消息，只缩短轮询间隔**：治标；回显仍要一次完整 session 回源（3 个 HTTP + 重建整个 workflow）。
- **删掉 `ready` / `waiting_confirmation`**：需要改 DB CHECK 约束做迁移，收益只是删两个标识符。改为在迁移表中让「不可达」变得显式，并用测试锁定；要启用时补一条迁移即可。
- **评测缺陷直接改知识包**：优化更新（`/optimize/apply`）已经覆盖「自动改包→候选修订」。回建模要解决的是另一类——需要专家参与判断的缺陷，因此只登记任务、由专家在助手里发起。
- **点击后跳转到建模步骤**：会丢失评测上下文。助手栏在评测/诊断阶段始终可见，就近触发 + 助手接管不需要跳转。

### 修缮期间知识包被改动时的状态归属

`package_repair` 任务的归属里程碑取决于触发来源（知识结构阶段校验 → `knowledge_confirmed`；评测缺陷回流 → 通常是 `deterministic_confirmed`）。
原先 `supersedeKnowledgeRepair` / `ensureKnowledgeRepairRecovery` 都硬编码 `knowledge_confirmed`，
导致评测修缮任务对这两套保护不可见——修缮进行中被人工改包时旧待办无法作废，任务会永久停在 `running`。

统一改为按**当前活动里程碑**查找（`findActiveRepairTask`）。新修订到达时的归属：

| 修缮任务状态 | 归属 |
| --- | --- |
| `waiting_user`（尚未发起） | 任务保留；发起时 `startKnowledgeRepair` 读最新修订，基线自然跟随 |
| `running` 且有待确认交互 | 作废：任务置 `completed`，助手提示将基于最新修订重新分类问题 |
| `failed` | 进入工作台时由 `ensureKnowledgeRepairRecovery` 给出「重新校验并继续修复 / 暂缓 / 查看校验结果」 |

改包后由 `synchronizePackageCheck` 自动重查（每个修订只自动跑一次）。该驱动原先挂在
`PackageCheck` 面板上，而面板只在「编辑与检查」页签渲染，导致评测和诊断页签下改包不会重查；
现已上移到 `PackageWorkspace`，面板改为受控展示。

校验结果经 `settleRepairAfterValidation` 回写任务图——否则校验跑了、结果存了，
状态机却一无所知，要拖到「确认完成 AI 辅助构建」才被一次性裁决：

| 校验结果 | 修缮任务 | 助手下一步 |
| --- | --- | --- |
| 零阻断错误 | `completed`，输出指向当前修订 | 确认完成 AI 辅助构建 / 再看知识文件 |
| 仍有阻断错误 | 保持 `waiting_user` | 继续逐项修缮 / 查看校验结果 / 暂缓修缮 |
| 任务正在执行（`running`） | 不介入 | 受控修复自己给结论 |

之后经 `confirm_construction` 重新确认 `construction_completed`（该接口本就强制要求
check 修订匹配、零阻断错误、无待处理交互与候选），任务图回到 `completed`。

## Consequences

- 助手消息回显从「1s 轮询 + 200ms 防抖 + 3 个 HTTP」降到「事件到达即上屏」。
- SSE 连接数从 2 降到 1，且不再每事件重连；候选修订面板不再随每个事件重取。
- 长对话不再占住 HTTP 连接，刷新页面能恢复「正在分析」。
- `GET /session` 不再自我触发事件循环；代价是新增一次 `POST /session/advance`，且如果新入口忘记调用，停住的编排要等下次进入工作台才对账。
- 评测缺陷回流会把已确认的建模里程碑重新打开（`redo` 会把下游里程碑标记为 stale、清掉确认记录），这是有意的——校验之后发现缺陷意味着当时的确认不再成立。UI 文案已说明。
- 仍未解决：`ai-construction-workflow.ts` 的 `evaluationRepairActive` 显示层补丁还在。根因是状态模型缺少「建模完成后的修缮」阶段；本次用「重开校验里程碑」绕过，彻底修法是引入独立的评测修缮里程碑。
