# 受控修复的里程碑归属与建模完成拦截的可操作出口

## Context

两个相关问题在同一条路径上暴露：

**1. 知识结构确认后无法再发起受控修复。**
`AiConstructionOrchestrator` 里六个受控修复方法（`startKnowledgeRepair`、`waitForKnowledgeRepair`、
`completeKnowledgeRepair`、`deferKnowledgeErrors`、`stopKnowledgeRepair`、`supersedeKnowledgeRepair`）
都按 `milestoneId === 'knowledge_confirmed'` 查找修复任务，`completeKnowledgeRepair` 还把重新校验任务
硬编码规划到该里程碑。而任务图要求里程碑处于 `active` 才能规划或推进任务
（`plan_task` 的 `只能为当前活动里程碑规划任务`、状态迁移的 `milestone.status !== 'active'`）。

知识结构确认后该里程碑变为 `confirmed`，此后知识包仍会出阻断错误（人工改包、确定性阶段校验）。
体检面板的「自动修复错误」按钮只看错误数、不看里程碑，于是：点击 → `plan_task` 抛
`INVALID_TASK_PLAN` → 400 + 原始内部文案「只能为当前活动里程碑规划任务」。
业务专家看到一句看不懂的话，且没有其他出口。

**2. 「确认完成 AI 辅助构建」五条拦截里只有一条能就地处理。**
`PACKAGE_WARNINGS_UNREVIEWED` 会把 `warningReview` 一起返回，前端据此展开复核区；
其余四条只返回一句 `error` 文本，前端渲染成无按钮的气泡。其中
`KNOWLEDGE_EVIDENCE_REQUIRED` 后端已算出 `problems` 明细却被前端丢弃；
`PENDING_INTERACTIONS` 用无过滤的 `pendingInteractions.length` 计数，
而助手栏只渲染通过 `isCurrentConstructionInteraction` 的待办——
修订变化后失效的受控修复待办会造成「被拦住但界面上找不到要处理的事项」。

## Decision

**受控修复跟随任务本身的里程碑，而不是假定 `knowledge_confirmed`。**

- 新增 `findRepairTask(graph, statuses?)`：未结束的受控修复任务全局只有一条，按 kind 查找，
  不再按里程碑筛。五个消费方改用它，消息块的 `milestoneId` 取自任务。
- 新增 `ensureRepairMilestone(projectId, graph)`：创建修复任务时解析承载里程碑——
  有活动里程碑就用它；没有（建模已完成）则重做最近一次已完成的 `package_validation`
  把该阶段重新打开。
- `completeKnowledgeRepair` 里的重新校验任务也规划到修复任务所在里程碑。

**被拦下时由后端声明可执行的下一步。**

- 四条拦截各自返回 `nextActions`（与消息里的 `next_actions` 同构），
  `KNOWLEDGE_EVIDENCE_REQUIRED` 附带 `problems`，`PENDING_INTERACTIONS` 附带
  带 `location` 的待办清单。
- 后端计数改用 `blockingPendingInteractions`，与助手栏的渲染规则一致：
  修订已变的受控修复待办不再阻塞。
- 前端 `completionBlockerMessage()` 把 409 转成一条本地消息，交给现有
  `AiConstructionMessageCard` 渲染；不为每个 code 写特判。

## Alternatives considered

- **把错误文案翻译成人话**：止血不治本。用户仍然无路可走——「自动修复错误」这个唯一入口本身是坏的。
- **禁用非 `knowledge_confirmed` 阶段的修复入口**：知识包在确定性阶段和人工改包后确实会出错误，
  堵死入口等于让用户只能手动改文件。
- **前端为四条拦截各写一个特判**：与「动作的语义由后端声明」相悖；每加一条拦截都要改前端，
  且容易再出现前端处理了、后端没发的孤儿动作。

## Consequences

- 知识结构确认后仍可修复；建模已完成时会重新打开校验阶段（`redo` 会把下游里程碑标记为 stale、
  清掉确认记录）。这是有意的：校验之后发现阻断错误意味着当时的确认不再成立。
- 四条拦截都给出可点的下一步；缺证据的具体条目和待办所在位置对专家可见。
- 新增 `run_package_check`、`open_scenario_review` 两个动作 id，后者复用既有 `onNavigate` 通道。
- 仍未处理：`view_validation` 等动作仍是「往输入框预填一句话」的形态，点击后不直接产生结果。
