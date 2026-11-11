# 0011 资料分析验收回执：报告"已保存"与"已验收"分离

## Context

资料分析（`/ontology-research`）把子代理返回的报告直接写入 `research/` 并提交，
之后三处阶段判断都只探测"目录里有没有非 index 的 `.md`"：

- `assets/workspace-toolkit/scripts/state_helper.py` 的 `_compute_stage` → `调研完成`，
  `extract-object` 的前置随即放行；
- `backend/app/services/workspace.py` 的 `STAGES` / `_project_progress` → 计入 `stages_done`；
- `frontend/src/components/LeftPane.jsx` 的 `probe` → `finish`，下一阶段变 `process`。

`pre_commit_check.sh` 只筛 `ontology/` 下的文档，研究报告不在范围内；
`backend/scripts/eval_baseline.py` 的研究阶段评分只看报告字符数、锁是否释放、有没有提交。
结果是报告内容错了（issue #362：声称 26 项实列 28 项、规范规则 ID 被静默改名、
出现源表不存在的字段），系统仍然宣布资料分析完成并建议进入对象提取。

同时 `assets/workspace-toolkit/agents/research-agent.md` 的 `## Step 4` 只有标题、
没有报告模板，也没有把真实表头传给子代理——对象提取阶段早就把 `TABLE_HEADERS`
作为字段名的唯一依据并由 `ontology_checker.check_object_columns` 逐列对账，
资料分析阶段既没传依据也没有检查。

报告的段落契约同样只写在消费侧：`coverage-check-agent.md`、`extract-objects-agent.md`、
`extract-operation-agent.md` 都按 `核心业务对象候选` / `核心业务逻辑候选` / `详细信息`
段里的加粗名称取候选，生产侧（research-agent）从未声明过这些段落。

## Decision

把"已保存"和"已验收"拆成两件事，验收结论由受控脚本产出。

- **输出契约前移**：`research-agent.md` 的 Step 4 给出固定报告模板
  （核心业务对象候选 / 核心业务逻辑候选 / 详细信息 / 业务项清单 / 分类小计 / 合计 /
  ID变更对照 / 数据与术语来源 / 信息来源 / 缺口与待决），
  并写明计数自洽、ID 沿用、字段逐字取自表头三条硬约束。
  前三段沿用消费侧既有的段落名与 `- **名称**：说明` 加粗格式，
  并由检查器判定段落与加粗候选是否存在——生产侧与消费侧的契约从此写在同一处。
  `ontology-research.md` 在派遣前执行 `research_checker.py capture`，
  把真实表头与既有规范清单固化成只读快照 `.ontology-state/research-inventory.json`，
  派遣与回传核验引用同一份快照——与 `ontology-extract-object.md` 的 `TABLE_HEADERS` 同口径。
  既有规范清单取自 `.ontology-state/research-baseline.json`：只在报告完整验收通过时写入、
  核验失败不清空。不直接读当前报告——未通过报告里编造的 ID 若进了基线，下一轮
  ID 溯源会把它当作合法出处放行。
- **确定性检查器**：新增 `assets/workspace-toolkit/scripts/research_checker.py`，
  复用 `check_eval_data.table_headers` 与 `ontology_checker` 的 markdown 解析，
  检查结构、计数、ID 唯一与稳定、字段保真、来源可定位；
  语义判断不自动确认，「缺口与待决」里未勾选的条目转人工待决。
- **ID 溯源补住首轮**：规范 ID 来自用户上传的资料，首轮调研没有报告基线，
  跨轮次的 ID 稳定性检查无从比对（issue #362 的现场正是首轮，所以单靠基线对比
  捉不到那次改名）。因此每个 ID 还要能逐字溯源：自己声明的来源文件 →
  `resource/` 下的文本资料 → 快照里已验收的 ID → 「ID变更对照」声明的新增 ID；
  四处都没有即判错。语料**不含任何报告正文**——报告会被本轮覆盖，读它等于自证。
- **验收回执**：检查结果写入 `.ontology-state/research-review.json`（gitignore，派生状态），
  记录结论、检查器版本与本次读过的全部输入指纹（报告 + schema/term 表格 + 评测数据表 +
  `materials.json` + 快照 + ID 溯源时实际读过的来源文档）。回执判定集中在
  `assets/workspace-toolkit/scripts/research_review.py`，对外只有四种状态
  `none / pending / failed / passed`。
- **失效由指纹决定，不靠失效钩子**：任一输入指纹不符即退回 `pending`，
  所以编辑报告、改源表、重跑 capture 都会让旧回执立即失效；
  失效方向永远是"退回待核验"，不会把未知状态当成已验收。
  超过 8 MiB 的源表格以文件大小代替内容哈希，避免项目列表刷新时全量哈希。
- **三处阶段判断读同一份回执**：
  `state_helper` 新增 `调研待核验` 阶段（排序低于 `调研完成`），`extract-object` 因此不放行；
  `_project_progress` 的资料分析阶段改读回执，并返回 `research_review`（状态、原因、
  是否仍挡着后续阶段、错误与待决条数、下一步提示）；`LeftPane` 按该状态显示
  「已生成，待核验」/「未通过」，`ChatPane` 的下一步建议同步不再主推对象提取。
  后端通过 `_load_research_review()` 直接加载工具箱脚本（与既有
  `_load_script_contract()` 同一手法），判定逻辑只有一份实现。
- **验收只管进入对象提取之前**：报告未验收、对象也还没提取时，资料分析才算未完成并
  挡住后续阶段（`research_review.blocking`）。对象一旦提取，资料分析按已越过处理——
  进度照常往后计、导出入口照常出现，只留「待核验」/「未通过」角标作提醒。这与
  `state_helper._compute_stage` 的"已到达的最高阶段优先"同口径；挡不挡由后端判定，
  前端只读 `blocking`、不自行推断（`frontend/src/lib/researchGate.js`）。
  报告挡着的时候，聊天区把「核验报告」排在建议的第一个，而不是先推重新调研。
- **提交与评分同口径**：`pre_commit_check.sh` 暂存 `research/*.md` 时跑同一检查器，
  未通过阻止提交；`eval_baseline.py` 的研究阶段把原先 20 分的"产出报告"
  拆成 10 分产出 + 10 分内容验收通过，总分不变。

## Alternatives considered

- **只补提示词**：把报告模板和"请认真核对"写进 prompt，不加脚本检查。
  后端与 CLI 前置仍然放行，错误报告照样被下游当事实来源；模型自检不是确定性结论。
- **只在 git hook 拦**：Web 端保存报告不走 hook，换执行引擎 hook 也可能不生效，
  阻断范围覆盖不全。
- **回执只存结论，不记输入指纹**：需要在资料上传、删除、`materials.json` 更新、
  `write_file` 等六处分别挂失效钩子，漏一处就会把旧结论继续当成已验收。
- **后端另写一份回执判定**：与工具箱脚本两份实现必然漂移，正是本次问题的成因。
- **未验收就一直挡住后续阶段**（不看对象是否已提取）：最初就是这么做的。结果是升级后
  凡有调研报告的存量项目进度整体归零、失去导出入口，而自由格式的旧报告过不了结构
  检查，只能逐个重做调研；回执因打标签、换评测表而失效时也会这样。界面还与命令行的
  阶段判定相反。
- **只豁免从未核验过的旧项目**：回执后来失效的项目照样归零，只解决一半。
- **把字段保真检查合并进 `ontology_checker`**：该检查器的输入是本体文档
  （object/operation/rule/metric），研究报告结构不同；合并会让它的目标目录判断复杂化。

## Consequences

- 旧项目只有报告、没有回执时显示「待核验」，不自动算完成；
  既有报告与已生成资源一律保留，不自动删除或重建。
  已经提取过对象的旧项目只多一个角标，进度与导出入口不受影响；还没提取对象的
  旧项目会停在资料分析，需要核验或重做调研。
- 用户在跳步守卫里坚持用未通过的报告提取对象之后，资料分析同样不再挡后续阶段，
  只剩角标提醒。这是用户明确做出的选择，本轮摘要里已标注"来源报告未通过内容验收"。
- 检查器缺失或异常时拿不到通过回执，阶段停在「调研待核验」——失败方向是不放行。
- 报告必须按模板写，自由格式的旧报告重新核验时会判结构缺失；
  需要用户或助手按模板补齐清单、小计与合计后才能通过。
- 流程图来源分支保留原边界：只画流程图、不做调研仍可进入对象提取，
  研究门禁没有扩大为所有入口强制研究。
- 阶段前置只看得到"有流程图"，所以流程图与未验收报告并存时前置照样通过。这一格由
  提取命令自己把关：`ontology-extract-object.md` / `ontology-extract-operation.md` 的
  Step 1 先跑 `research_checker.py status`，未验收的报告不进来源、只用流程图继续；
  报告是唯一来源时走跳步守卫，由用户明确选择后才使用并在摘要里标注。
  没有改成"有未验收报告就整体拦住"，那会让一份坏报告堵死本来可用的流程图路线。
- `.ontology-state/` 新增三个工具托管文件（回执、快照、ID 基线），`workspace-rules.md` 已声明不得手工编辑。
- `LeftPane` / `ChatPane` 的状态映射只做了极薄的一层（完成态取后端
  `research_review.blocking`），权威判定与测试都在后端。前端这一层的纯逻辑抽到了
  `src/lib/researchGate.js` 并有 `node --test` 用例；`stageStatuses` 与 `STAGES` 仍住在
  `LeftPane.jsx` 里（含 JSX 与 antd import），没有单测。

## 补充：「详细信息」出处一并核对

- **Context**：规则文档新增「原始资料来源」节，内容取自报告「业务项清单」的来源位置和「详细信息」
  同名小节里的出处。原检查器只核对清单的来源位置，「详细信息」写错路径、锚点或省略路径时照样放行，
  错误会被抄进规则，页面上打不开或定位不到。
- **Decision**：`research_checker.py` 截取「详细信息」整段（`extract_sections` 会把 `##` 小节拆开，
  所以按一级标题截取），逐处核对 `resource/` 出处：文件不存在判「来源缺失」，锚点在原文里找不到判
  「来源无法核验」，只写锚点、省略文件路径判「来源缺失」，三者都不放行。`research-agent.md` 要求
  每处出处都写完整的 `路径#锚点`。出处按 ADR-0074 写文本版的行号（`resource/text/…#L12`、`#L12-L15`），
  抽取器的 `located_text` 认行号范围（清单的来源位置同样受益）。
- **Alternatives considered**：只在规则提取时校验出处——错误已经落进规则，返工更大；
  只取清单的来源位置——同一规则出自多份资料时会丢出处。
- **Consequences**：新报告的「详细信息」出处有一处写错就过不了验收，需要改正后重新核验；
  已验收的旧报告重新核验时同样按新规则检查。

## 本轮不覆盖的范围

issue #362 的"不一致"指报告内部自相矛盾（声称 26 项实列 28 项）以及报告与既有规范
清单、真实表头不一致——这三项本 ADR 都已覆盖。以下是**超出该 issue 的**相邻范围，
本轮没做：

- **报告与对象文档之间没有逐项对账。** 源字段之所以一致，是因为报告侧
  （`research_checker` 的字段对账）与对象侧（`ontology_checker.check_object_columns`）
  各自对 `check_eval_data.table_headers` 对账，两边都不会出现虚构字段；并没有
  "报告 ↔ 对象文档"的逐项比对。操作、规则、指标在逻辑提取时有按名称与分类的对账
  （ADR-0012，`report_reconcile.py`），对象提取没有接入。
- **规则 ID 没有对到 `ontology/rule/`。** 逻辑提取按报告分类生成规则时会把清单里的
  `规则ID` 写进规则文档的「来源」段（ADR-0012），但 `templates/rule-template.md` 没有
  承载规范 ID 的结构化字段（规则身份是文件名 + `# 来源` 回链），对账也只比名称、不比 ID。
  要闭环需改规则文档结构（ADR-0004 五元组），属独立决策。
  注：issue #362 的现场尚无任何正式对象/规则，不涉及这一段。
- **「缺失不隐式补 1 或 0」只写进了提示词硬约束**，没有也无法做成确定性脚本检查。
- 报告正文仍由模型按模板书写，不是从同一结构化结果渲染。清单、分类小计与合计的
  一致性靠检查器事后对账保证，而不是靠渲染时天然一致。
- **Excel 里的规范清单无法参与 ID 溯源**：溯源语料只含
  `.md/.txt/.csv/.json/.yaml/.yml`。规范清单放在 `.xlsx` 里时，ID 溯源会判错，
  需要按「ID变更对照」声明或把清单另存为文本资料。
- **回执只跟踪报告自己声明的来源文档。** ID 不在声明的来源里、而是在 `resource/`
  别处找到时（检查器会给一条警告），那份资料不在指纹里，它改动后回执不会失效。
- **回执失效仍然偏敏感**：给任意资料打用途标签会改写 `materials.json`，替换评测数据表
  也算输入变化，都会让状态退回待核验。对象提取之后这只影响角标，之前则会挡住流程；
  没有做成只对 schema/term 相关的条目算指纹。
