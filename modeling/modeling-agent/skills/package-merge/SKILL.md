---
name: package-merge
description: 一次性融合多个知识包或选择性导入单一知识包，并保持来源可追踪
---

# 知识包融合

- 每轮开始或从检查点恢复后，先 `list_package_fusion_plans`，再 `list_package_sources`。来源指纹集合、选择范围和输出设置未改变时，必须复用最新的 `ready` 或 `awaiting_decisions` 计划并从现有账本继续；只有来源、范围、输出已改变，或用户明确要求重建时才调用 `create_package_fusion_plan`。不得按上传顺序反复执行单包 merge；输入顺序不能改变输出。
- 用户明确只需要部分内容时，用 `source_selections` 按来源选择 objects/operations/rules/metrics/scripts/diagrams；不得留下被保留元素对已排除范围的引用，core 阻断后应主动给出一起保留或一起排除的业务选项。
- 计划返回的 ledger 是逐元素去向事实。用 `focus(kind='conflict')` 展示未裁决项；可选独立保留、经业务依据归并到另一来源的同类型目标，或说明原因后排除。不要因同名自行认定等价、合并或排除。
- 可确定保留的元素主动用 `update_package_fusion_plan` 登记，不逐文件转录。跨包概念也通过同一计划按 id 增量登记；读取定义和证据后按 common/parameterized 分组，只问用户可理解的业务含义，不要求用户提供工具名、来源 ID 或裁决编码。
- `unresolvedCount` 必须为 0 才能调用 `compile_package_fusion_plan`。编译只运行一次并生成一个原子候选，随后直接 `request_confirmation`；确认前当前知识包不变。批量融合不依赖资料建模阶段，不要为了编译去补文档或跳转目标、流程、知识等普通建模阶段。
- 用户在评测界面准备好真实场景用例和数据源后，对每个融合来源分别运行来源回归，再运行正式融合包评测。对话中只说来源包名和通过情况，不要让用户提供 `source_id` 或工具调用参数；来源回归失败不得覆盖正式融合包的发布结果。
- 只有一个来源且用户明确要求选择部分目录时，才用 `analyze_package_merge` 缩小范围、检测冲突，再用 `stage_package_merge` 生成候选。
- key、别名和引用冲突用确定性集合事实；同名是否同义由你分析并说明理由。
- 来源包只读，所有融合只写候选修订并记录 provenance。
- 必须人工裁决互斥规则、别名撞车和业务口径改变；其余按已确认策略处理。
- 融合后若引用断裂，阻止确认并指出由哪项范围选择或裁决造成。
- 用户放弃时只丢候选，当前已确认包不受影响。
