# 建模阶段轨：把步骤显性化为门禁投影

## Context

后端一直有完整的阶段模型：`PHASE_LABELS` 七个线性阶段外加 `increment` 再入口、`PHASE_GATES` 六道入口门禁、`PHASE_EDGES` 允许的跳转，`agent_project_workflow_state` 甚至已经算好了每个目标阶段的 `ready` 与 `blockers`。

但这一整套到前端只剩顶栏一枚 `wb-phase-pill`，显示当前阶段名。用户能看见「知识建模」四个字，看不见这一步交付什么、离下一步还差什么、卡住时谁该动手。

旧的 `WorkflowTimeline`（9 步导航）在 `docs/redesign/02-feature-map.md` §8.1 被明确退休，理由成立——阶段推进由 agent 决定，不该有一条用户可点着走的流水线。但退休时把「可点着走」和「看得见」一起扔了。

同时，门禁事实在浏览器侧完全拿不到：确认记录在 `agent_confirmations` 表、沙箱结果在 `deterministicCapabilityState`、评测结论在 `latestEffectiveEvaluation`，`/api/projects/:id/agent/*` 下没有任何一条 GET 暴露它们。`gate_status` 只能经 `/internal/agent/tools/:tool` 带 run 凭据调用，而用户盯着进度看的时候，助手多半正好空闲。

## Decision

**步骤是节点，门禁是节点之间的闸。** `knowledge_confirmed` 是*进入*「确定性验证」的条件，不是「知识建模」的属性。画成节点会让人以为门禁是第 8 个步骤，画成边则一眼看出卡在哪一段。

**轨是投影，不是驱动器。** 阶段来自 `state` 事件，门禁来自确定性核心，产出计数来自文件清单。轨自己不持有任何状态、不推进任何东西——这是它与被退休的 `WorkflowTimeline` 唯一但根本的区别。

**一条轨上只亮一道闸。** 从当前步往后数第一道没通过的才带标记，其余画细虚线。同时亮六个红叉等于没亮。

**轨上没有确认按钮。** 阻断项只做导航，跳回对话流的确认卡或中栏 diff——「没有 diff 就没有确认」不可退让。

具体实现：

- `gateStatus` 拆成「挑一道」与 `computeGates`「一次算全部」。它本来就是算完全部再挑一个返回，抽出来即可复用。同时删掉 `releasedContent` 的懒算分支：相对 overlay 和评测查询那点开销可以忽略，而留着分支会让 `computeGates` 返回一个条件性错误的字段。
- 新增只读 `GET /api/projects/:id/agent/progress`，用 `this.workspace(projectId)` 而非 `readableWorkspace`——门禁是项目级事实，换个人看不该得出不同结论。助手尚未认领工作区时如实返回 `available: false`。
- `modeling_agent/service.py` 的 `_phase_projection()` 随 `/state` 一起下发阶段顺序、标签和入口门禁。阶段表继续单点存放在 `PHASE_LABELS`，前端不抄第二份。
- 前端三级递进，与活动行同构：常驻轨（L1）→ 阶段卡 / 闸卡（L2）→ 中栏 `progress` 焦点的进度全景（L3）。
- 门禁按需拉取；`state` 与 `change_set` 事件到达时把缓存置空，下次打开重算。

## Alternatives considered

- **纯前端实现**：骨架可以，闸不行。六道闸里只有 `materials_ready` 的输入已经在 snapshot 里，其余五道依赖确认记录、沙箱结果和评测结论，浏览器一条都拿不到。先上带假闸的轨比不上轨更糟——常驻元素的错误会被反复当成事实消费。
- **让 agent 通过 `focus(kind='gates', data=…)` 填充**：`data` 是 `additionalProperties: true` 的自由 JSON，等于让模型给自己的进度打分，踩在 `02-feature-map.md` 分界线的错误一侧。
- **把阶段顺序和标签硬编码进前端**：`PHASE_LABELS` 一改就得改两处，与活动行坚持用工具返回的 `display` 字段、不解析工具名是同一条理由。
- **随 `state` 事件推送全部门禁**：`computeGates` 要 overlay 整个候选工作区、读 base revision、查评测轮次，跟着事件流反复算不划算。
- **恢复 `WorkflowTimeline` 式的可点击流程**：与「阶段推进由 agent 决定」冲突，是当初退休它的原因。

## Consequences

- 顶栏 `wb-phase-pill` 移除，阶段名不再出现两次；轨占用 46px 垂直空间。
- 轨的显著程度与状态挂钩：平时白底克制，`execution === 'waiting'` 时整条换琥珀底并把出口按钮转实心——这是唯一该打断用户的时刻。不用红色，卡住不是出错，是在等人做决定。
- 门禁结论可能短暂过期（事件到达与重新拉取之间）。选择置空而非沿用旧值：宁可多拉一次，也不要把过期结论继续摆在轨上。
- `computeGates` 每次调用多算一次 `equivalentReleasedRevision`。未发布项目为零成本，已发布项目是有界的若干次 git diff。
- 阶段轨只画门禁顺序，不画助手实际走过的路。返工、回头、跳步仍然只在过程抽屉里可见。
