# 指令寿命分层与上下文分层治理

## Context

线上实测到两类插话失败：一次性动作（如「把某项内容移出建模范围」）被 turn_goal 的「不要自行改换目标」禁令挡掉；长期约束（如「某字段不许写死」）只在恰好留在上下文窗口里时被遵守，属于侥幸生效。根因是系统只有一种指令表示形式——会沉底的聊天文本——而指令有不同的寿命。

同时，上下文管理存在结构性缺陷：贵重信息（门禁清单、专家指令）被一事一议地手工搬进 system 段避难（turn_goal 是补丁一号），system 段自身却没有准入标准；契约 JSON 全阶段常驻，阶段方法靠模型自觉调 `list_skill` 加载且加载结果作为普通消息可被驱逐。

## Decision

**指令寿命分层**（`modeling-agent`）：

1. **插话即目标**：`_turn_goal` 的判据从「是不是续跑」改为「有没有待处理指令」。未处置的专家插话（`steering_messages` 新增 `origin/disposition` 生命周期列）优先于门禁项成为本轮唯一目标；插话转出的待办排第二；都清空后回到门禁目标。「一轮一个目标」的防空转纪律完整保留，变的只是目标来源。
2. **强制表态**：每条插话在本轮结束前必须经 `dispose_interjection` 处置——登记为约束 / 转待办 / 已满足（强制证据锚点，堵合理化逃生口）/ 拒绝（强制理由）。未处置时在预算内强制续跑；预算耗尽发 `interjection_undisposed` 可见事件，沉默成为错误状态而非默认状态。
3. **约束登记表**：新表 `project_constraints`（active/revoked/superseded，第一天支持覆盖与撤销），每轮重算注入 system 段「专家约束」槽位，撤销即时生效。页面可见：`GET/POST /v1/projects/{id}/constraints[...]` + TS 代理透传 + 工作台左栏「规矩」页签（专家改判兜底通道）。
4. **不变量冲突走结构化升级**：不在准入时做语义判断（代码算不出来），探测点后移到执行层的确定性守卫；撞墙时模型发 `ask(kind='conflict_decision')`，服务端在 `_settle` 确定性地把未处置插话绑定为「已升级待决」；裁决卡被回答后插话重新开放，按专家裁决收尾。升级不是终态，错误升级最多延迟处置一次。

**上下文分层治理**（除代价分级 D 项外）：

- **A 卸载链加固**：`task_tool`（子代理派遣）加入默认保护名单——子代理报告是「可昂贵重取」档，在按档分级落地前先按不可重获对待。卸载链开关本身是部署配置（compose 透传已就绪），不改默认值。
- **B 落盘纪律**：主手册「工作方式」新增第 10 条——每完成一个分析单元先落盘再继续，判据「重做代价超过一次工具调用就不许只存在于对话里」。软纪律先行，升级梯子（边界提醒 rail、轮末核对）见失败证据再上。
- **C1 契约跟工具走**：`CONTRACT_GOVERNED_TOOLS`（领域写入 + 脚本 + 融合暂存）与阶段工具过滤同一开关；本阶段暴露管辖工具才注入契约 JSON，否则留一行占位。intake/goal/process 卸下数 KB 常驻负重，机制上不可能出现「用得上却不在场」。
- **C2 阶段方法服务端注入**：`PHASE_SKILLS` 映射四个阶段形 skill（material-analysis→intake/goal、process-modeling→process、knowledge-extraction→knowledge、incremental-intake→increment），按 `context.phase` 注入 system 段「本阶段方法」节；情形形（package-merge、failure-repair）保留 `list_skill` 事件触发。主手册 Skill 路由节相应改写。

system 段自此按寿命分四槽位：契约（项目寿命，随管辖工具出现）→ 主手册（跨阶段通则）→ 本阶段方法（阶段寿命）→ 专家约束（撤销前有效）→ 本轮目标（一轮寿命）。

## Alternatives considered

- **插话塞进 system 段与门禁并列**：两条同级指令没有定序规则，仍由模型仲裁，同类 bug 更难发现。优先级必须算出来。
- **逐轮重发插话原文**：没有完成状态，已满足的指令被永远重复注入，退化成噪音。处置位（consumed）与「插话即目标」连体不可拆。
- **一次性动作挂 TS 侧 `ai_construction_tasks`**：语义最合，但隔着服务边界，写回链路成本高；改用 steering 行内的 task 状态（open/done + 证据），单表完成生命周期。
- **约束并入 `package_contract`**：它是每轮从纯函数重算的静态字面量，无运行时增删改入口；专家的规矩需要独立的可变存储。
- **准入时语义判断插话是否碰不变量**：自然语言理解代码算不出；改为宽进 + 确定性守卫探测 + 结构化升级，模型没有仲裁角色。
- **「进阶段直接执行 skill」替代目标合成器**：手册（怎么干）替代不了派工单（现在干哪件），会重开空转口子；两者按 C2 与 turn_goal 分工并存。

## Consequences

- 「储罐」类失败由插话即目标修复，「单价」类失败由约束登记表修复；两类修复都有回归测试（`test_interjection_lifecycle.py`）。
- 契约与阶段方法注入随阶段变化会破坏跨轮 prompt 缓存——turn_goal 已每轮变化，边际影响有限；净效果是 system 段瘦身。
- 专家插话处置在预算耗尽时仍可能未完成，但必然可见（事件 + 下一轮重新成为目标），不再有静默丢弃路径。
- 已知边界：交互卡内的插话回答（interject 通道）不经 steering 表，暂无处置生命周期；首轮原始 query 无 turn_goal 保护（既有弱点，与本决策正交）。
- 本地测试环境（PyPI 版 openjiuwen）存在 7 个与本改动无关的既有失败，提交前后失败集合一致，已逐一比对基线确认。

## 补充（2026-09-17）：资料档案查询面与工具返回形态规约

**Context**：跨阶段重复全文读取原始资料成本高；核实发现全文精读的分块提取结果早已三层落库（`material_analysis_chunks` 分块事实带原文 offset、`material_analysis_document_summaries` 文档摘要、`material_analysis_runs.result_json` 项目汇总），且按修订指纹可复用，但消费方只有进度条——细粒度事实没有查询出口，后续阶段只能回原文重读。同时，大工具返回值直接进上下文只有逐工具的手工约定（search_documents 限 8 条命中、校验报告分页、全文精读只回摘要），无成文规约。

**Decision**：(1) 新增只读工具 `get_material_context(document/topic/kind)`（core-service 从三层落库结果组装，条数与字符上限，返回带 filename+offset 锚点，按当前修订指纹标注 stale），并加入 material_analyst / knowledge_extractor / merge_analyst 子代理工具集；(2) process-modeling、knowledge-extraction、incremental-intake 三个阶段手册写死工作序「先查档案 → search 定位锚点 → 按锚点核对原文 → 仅点名或 stale 时全文精读」，并强调档案是缓存、写入知识包必须经锚点核对；(3) 「结论+锚点+句柄」返回形态规约写入根 CLAUDE.md 第 6 节，作为新增/修改 agent 工具的评审判据。

**Alternatives considered**：扩展提取 schema 重跑分析（不必要——细粒度事实已在 chunk payload 里，缺的只是读取面）；上 FTS/向量检索（现有子串检索加档案层可能已够，不见失败证据不上贵机制）；把档案做成常驻 system 段（体积不可控，且档案是按需查询型信息，不满足特权区「当下相关」准入）。

**Consequences**：后期阶段的原文全文重读预期显著下降（验收看活动日志中 `read_documents_fully` 调用次数对比基线）；档案摘要有丢承重细节的固有风险，由「写入前锚点核对原文」纪律兜底而非摘要质量兜底；卸载链开关仍是部署侧动作，本补充不改默认值。

