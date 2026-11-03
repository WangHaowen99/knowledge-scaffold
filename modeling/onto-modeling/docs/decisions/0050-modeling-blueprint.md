# 0050 建模蓝图：工作区里的 research/blueprint.json，由确定性脚本写入与校验

## Context

详设第 9 章建议 2 把建模从全量瀑布改为按业务分段纵向推进：资料分析时拆出分段，每段单独走完「抽取 → 检查 →
场景 → 确认合入」。6.8 把分段规划并进资料分析（`core.research`）；契约 BP-01～BP-03 给出蓝图的读、调整（草案 /
调整两种保存、`revision` 冲突，合并、排除、改名、调序、新增都表达成整份分段列表）与确认（`next=L2` 往对话发「开始第一个
分段」，`next=L4` 返回自主构建开始前预览）。B4b 之前没有蓝图，FL-01 的 `slices` 为空（gaps B4b 第 1 条）。

## Decision

1. **文件与格式**：`research/blueprint.json`（JSON，进 git；`format: onto-blueprint/1`）。字段：`version_no`、`state`
   （draft | confirmed）、`goal_md`、`goal_anchor(_label)`、`out_of_scope`、`slices[]`（数组顺序即建议顺序；每段 `id`
   为 `s<n>` 或全量模式的 `all`、`name`、`core_flow`、`depends_on`、`excluded`、`merged_from`、这一段包含的
   `objects` / `operations` / `rules`（业务名，即条目文件名解码后的名字）、`sources[{anchor, label}]`）、`terms`
   （`aligned`、`ambiguous[{id, variants, chosen}]`）、`questions[{id, title, background, options[{id, label}], answer}]`、
   `next_ids`（下一个编号）、`updated_at`、`confirmed_at`。固定键序、两格缩进，改一处只动一处。
   - 放在 `research/` 下：它是资料分析的产出，`core.research` 声明的 `produces: research/*` 就覆盖它；资料分析报告的
     验收（只认 `*.md`）与完成判定不受影响；节点上游指纹不算它（调整分段不让对象提取过期）。
2. **只由脚本写**：core 新增 `scripts/blueprint.py`（标准库，登记进契约库）。资料分析的第 6 步让助手先写草稿
   `.ontology-state/blueprint-draft.json`，`blueprint.py write` 校验、编号（同名分段沿用旧编号，草案号加 1）后落盘；
   助手不手工改文件。后端的 BP-02、BP-03、待处理里回答问题、一轮结束时把新条目记进这一段，都经契约库按项目锁定版本
   调同一组函数（`apply_edit`、`confirm`、`assign`），校验口径只有一份。
   - 校验（契约 `slice_invalid.details.reason`）：至少留一段（`empty`）、段名非空且不重复（`name_required`、
     `duplicate_name`）、依赖只能指向排在前面、没排除的段（`dependency_order`、`dependency_excluded`、`dependency_cycle`、
     `unknown_dependency`）、合并至少两段且不能同时保留原段（`merge_too_few`、`merge_invalid`、`merge_unknown`）、原有的段
     不能直接删（`missing`，排除用 `excluded`）、条目名不能带保留字符（`member_invalid`）。
   - 新段（前端给的是临时编号）一律换成下一个 `s<n>`，别的段对它和对被合并段的依赖跟着换。
   - `order_label` 由位置算：第一段「推荐先做」、依赖多段的最后一段「建议最后做」、其余「第 n 个」，只有一段时为空。
3. **放在哪里读写**（`services/flow/blueprint.py`）：资料分析那一轮的蓝图在变更集副本里，确认生效之前主线上没有。
   没确认的变更集改过蓝图时，BP-01 读副本、BP-02 写副本（这一轮修改的一部分；助手在跑时 `423 write_scope_locked`），
   之后看一眼副本更新变更集；蓝图已在主线上时，BP-02 经写入事务提交（范围锁照常检查）。
4. **确认蓝图（BP-03）**：先核对构建方式与版本号（不改东西）；蓝图在没确认的变更集里时先把这一轮确认生效（同 CS-04，
   门禁、冲突照常），再在主线上标已确认（一个 `web: 确认建模蓝图` 提交）；已确认的不再写。
   - `next=L2`：经 S-20 同一个 `dispatch_command` 发 `/ontology-extract-object slice=<第一段>`（全量模式不带分段），
     返回 `{blueprint, command_task}`；
   - `next=L4`：调 `register_autobuild_preview` 登记的提供方算开始前预览（同 AB-03）。**B5b 登记之前回
     `409 autobuild_not_available`，在改任何东西之前就回**，蓝图不动。
   - 缺省取项目的构建方式（P-13）。
5. **对话里的卡片**（契约 4.7）：一轮写出或改了蓝图，在「这一轮改了什么」之后出蓝图卡（`open={dialog, blueprint}`）；
   这一轮也改了资料分析报告时先出资料分析小结卡（数字取验收回执：读过的资料、核对过的出处、带过来的问题）；
   蓝图卡底部带确认按钮 `confirm_options`（设计调整 R3，取代「确认后怎么建」卡）：草案时「确认，一段一段来」（L2），
   自主构建可用时再加「确认，一路建完…」（L4）；已确认时为空。卡片编号登记在数据目录，BP-02、BP-03、回答问题之后对话里
   的蓝图卡全部 `card.updated`。
6. **没答的问题**进待处理（`decision:bq-<问题>`，IN-03 `answer` 写回蓝图）。

### 给 B5b（自主构建）的接口

| 用途 | 位置 | 说明 |
|---|---|---|
| 开始前预览登记点 | `services/flow/blueprint.register_autobuild_preview(fn)` | `fn(manager, project_id, blueprint_view, *, actor) -> AutobuildPreview`；登记后 BP-03 `next=L4` 返回它，蓝图卡底部出现「确认，一路建完…」 |
| 按段执行的分段列表 | `services/flow/blueprint.run_slices(manager, project_id)` | `[{id, name, core_flow, depends_on, brief, group, stages}]`，按建议顺序、没排除的段；全量模式是一段「全部」 |
| 段说明 | `brief`（同上）；工作区里 `python3 .claude/scripts/blueprint.py brief <段>` | 段名、核心链路、依赖、规划的条目、依据、范围外、统一叫法；建模命令注入的是同一份 |
| 段的命令 | `/ontology-extract-object slice=<段>` 等（ADR-0051） | 执行记录自动带分段与步骤，变更集记 `slice_steps`，确认生效时这一段这一步记为确认过 |
| 蓝图有没有确认 | `blueprint.load_doc(root)["state"]` | AB-03 的 `blueprint_not_confirmed` 用它 |

## Alternatives considered

- **YAML 或 Markdown**：YAML 要 PyYAML（工作区脚本只用标准库），Markdown 要另写解析且容易被改坏；JSON 固定键序后在
  版本历史里一样看得清。
- **放在 `.onto/`**：`.onto/` 是受保护路径，副本里的改动合入时一律取主线，助手写不进去。
- **让模型直接写 `research/blueprint.json`**：编号、依赖、草案号会各写各的，校验也没法和网页调整同一口径。
- **确认蓝图和确认生效分成两步**：资料分析那一轮结束后界面上会同时有「确认生效」「确认蓝图」两个确认，用户分不清；
  确认蓝图本来就是认可这一轮资料分析的产出。
- **蓝图在副本里时 BP-02 回 423**：调整分段是蓝图页的主路径，先确认生效再调整要点两次确认。

## Consequences

- 蓝图随项目进 git、跟着版本走；回退、克隆、导出都带着它。
- BP-03 可能回变更集的错误（`gate_failed`、`merge_conflict`、`changeset_in_progress`），见 contract-changes B4d。
- 资料分析小结卡的数字只取验收回执里有的；资料地图（R-07）没做，`report_id` 先为 null（研究报告不在 `reports/` 下）。
- 选 L4 在 B5b 之前不可用，蓝图卡上不出「确认，一路建完…」。
