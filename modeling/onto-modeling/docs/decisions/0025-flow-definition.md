# 0025 主流程写成 core 包的 flow.yaml，阶段定义只剩一处

## Context

阶段定义散在 5 处、口径不一致（详设 6.1）：后端 `workspace.STAGES`（项目列表 x/N）、前端 `LeftPane.STAGES`
与 `ChatPane.buildSuggestions`、`state_helper.py` 的 `STAGE_ORDER` / `COMMAND_PREREQS`（还有没人用的
`STAGE_NEXT_COMMAND`）、`session_start_hook.sh`、`/ontology` 命令里按阶段写死的「下一步建议」。「质量
检查完成」有三种判法：前后端看 `reports/` 有没有文件，`state_helper` 看 `validate_last_run`。加一个阶段要改
5 处。方案 D12、D14 要求 core 包提供流程定义、组装时做锚点 / 环 / 输出冲突检查、各处改读它，并提供
`GET /flow`；P1 只做定义与读取，节点状态计算属于 P2（D13）。

## Decision

1. **一份定义**：`assets/workspace-toolkit/flow.yaml`（core 包，清单 `contributes.flow`）。内容：
   - `probes`：命名探针（目录里有没有符合 glob 的文件、报告里有没有 `[ ]`、资料分析验收结论）；
   - `nodes`：现有 10 个阶段加按需的增量更新，带标题、挂法、分组、锚点、完成判定（探针组合，`!` 取反）、
     入口与快捷操作；
   - `stages`：state_helper 的 8 个线性阶段（名字不变，命令模板按字面量分支），每个阶段的成立条件；
   - `commands`：命令 → 所属节点与前置阶段（check-prereq）；
   - `next_steps`：`/ontology` 各阶段的「下一步建议」条目。
2. **一份算法**：`scripts/flow_state.py`（只用标准库）解释探针、算节点完成、旧版阶段、命令前置与下一步条目。
   state_helper 在工作区里跑它；后端经共享契约库按项目锁定的版本加载它。
3. **组装**（`services/flow/assemble.py`）：节点编号带包前缀且唯一、锚点存在、没有环、同一路径不被两个节点
   声明为输出、命令映射指向存在的节点；不通过则装配失败。组装结果写进装配目录的 `flow.yaml`——JSON 写法
   （也是合法 YAML），工作区脚本只用标准库就能读；哈希进 MANIFEST，计入会话指纹。
4. **各处改读它**：后端进度、state_helper（detect-stage / check-prereq / 新增 next-step）、会话开始的阶段提示
   （经 detect-stage）、`/ontology` 的下一步（经 `state_helper.py next-step <阶段>` 原样输出）、`GET /flow`。
   前端 `LeftPane`、`ChatPane` 不归后端，改读 `GET /flow` 记在前端线。
5. **统一「质量检查完成」**：有校验报告（`reports/*-rule.md` 或 `*-semantic.md`，示例数据与流程图的检查报告
   不算），且 `reports/` 下没有未处理的 `[ ]`。state_helper 的「完成」、后端的「质量检查」、`/flow` 的节点
   都按它。
6. **`GET /api/v1/projects/{id}/flow`**（FL-01）与 `/flow/suggestions`（FL-02）：节点、连线、分组、旧版阶段、
   进度；节点状态 P1 只粗算已确认 / 进行中（命令锁）/ 可开始 / 未就绪；建议只给主线下一步。

## Alternatives considered

- **每个节点一个 `nodes/*.yaml`**（详设 6.3 的节点包写法）：主流程的节点、阶段、前置、下一步彼此引用，拆成
  多个文件反而要再拼；扩展节点（P3 节点包）按 6.3 写单独的节点声明，组装时并进来，`assemble` 已按多包设计。
- **装配目录里同时放 YAML 源与编译后的 JSON**：两份文件就是两份定义；只写组装结果，且它本身是合法 YAML。
- **state_helper 调后端接口取阶段**：工作区脚本要能离线、终端里直接跑（方案 D15 也要求走文件不调 HTTP）。

## Consequences

- 统一口径后，只有场景验证或示例数据报告、没有校验报告的项目，「质量检查」不再显示完成；`validate_last_run`
  不再参与阶段判定（命令仍会写它，无害）。流程图阶段只认 `.drawio`。
- 加一个阶段只改 flow.yaml（和对应命令）；`test_flow_definition.py` 逐处核对后端、state_helper、会话提示、
  `/ontology` 下一步、`/flow` 与 flow.yaml 一致。
- 节点状态的完整计算、按切片的建模节点、跳过 / 恢复 / 开始节点、`flow.updated` 事件与 MCP `flow_get`
  在 P2 做；`post_commit_hook.sh` 按提交前缀给的提示仍在，P2 随宿主注入退役。
