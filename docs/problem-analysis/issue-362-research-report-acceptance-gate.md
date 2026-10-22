# Issue #362 分析：资料分析报告内容不一致仍显示阶段完成

- 分析分支：`fix/research-report-acceptance-gate`（基线 `origin/0.26.9.0` @ `9aece304`）
- 结论：问题存在，可在当前基线复现（门禁部分已用合成数据验证）
- 范围：`modeling/onto-modeling/`（资料分析编排 + 阶段状态前后端）

## 1. 结论速览

问题由两件独立的事叠加造成：

1. **报告内容没有输出契约** —— 资料分析子代理的提示词在 `## Step 4: Return structured report in Chinese` 这一行就结束了，没有任何报告模板、计数规则、ID 稳定性要求或字段保真要求。同时资料分析阶段也**没有把真实表头传给子代理**（只有后续的对象提取阶段传了）。所以"总数与清单不符、规则 ID 被改名、出现源表不存在的字段"属于缺少约束的必然结果，而不是偶发模型抖动。
2. **阶段完成只看文件是否存在** —— 三处阶段判断（CLI、后端进度、前端左栏）都只探测 `research/` 目录下有无非 `index.md` 的 `.md` 文件。报告保存成功 = 阶段完成 = 放行对象提取，内容对不对完全不参与判断。

两者合起来就是用户看到的现象：报告错了，但系统宣布"资料分析完成"并建议进入下一步。

## 2. 根因一：资料分析缺少输出契约

**提示词被截断。** `assets/workspace-toolkit/agents/research-agent.md` 共 55 行，最后一行就是
`## Step 4: Return structured report in Chinese`，正文为空。该文件自首次提交（`03ac1adc`）起历次重命名都是 55 行，说明模板从一开始就没写进去，不是某次重构弄丢的。

后果：
- 子代理第 30 行被要求"在报告的 `信息来源` 段列出本地文档"，但 `信息来源` 这个段落从未被定义；
- 没有"总数必须等于清单逐项计数"的要求 → 26/28 不一致；
- 没有"沿用既有规范 ID、改名须给对照"的要求 → ID 静默变更；
- 没有"schema 字段逐字取自真实表头"的要求 → 出现不存在的字段。

**真实表头没有注入资料分析阶段。** 仓库里已经有权威表头来源 `check_eval_data.table_headers(root)`，并且：

| 阶段 | 是否注入真实表头 | 是否有确定性列名对账 |
|---|---|---|
| 对象提取 `ontology-extract-object.md:128` | 是（`TABLE_HEADERS` 为唯一依据） | 有（`ontology_checker.check_object_columns`，错误阻止提交） |
| 资料分析 `ontology-research.md` | **否**（只传 `LOCAL_DOCS_PATHS`） | **无** |

`assets/workspace-rules.md` 第 8.1 节早已规定"英文属性名必须来自真实表头、不得虚构"，但这条规则只在对象提取链路落地，资料分析链路既没传依据也没有检查。

## 3. 根因二：阶段完成是"目录探针"，不是"内容验收"

三处实现各自独立，判断依据都是"目录里有文件"：

| 位置 | 实现 |
|---|---|
| `assets/workspace-toolkit/scripts/state_helper.py:325`（`_compute_stage`）、`:288`（`COMMAND_PREREQS`） | `_has_md_files(Path('research'))` → 阶段 `调研完成`；`extract-object` 前置 `['调研完成']` 随之放行 |
| `backend/app/services/workspace.py:186`（`STAGES`）、`:212`（`_project_progress`） | `_dir_has_entries(root/'research')` 计入 `done` |
| `frontend/src/components/LeftPane.jsx:50`（`probe`）、`:133`（`stageStatuses`） | `hasEntry(paths,'research/')` → `finish`，下一阶段变 `process` |

`ontology-research.md` Step 4/5 把子代理返回的报告直接落盘并 `git commit`，中间没有任何检查步骤。
`pre_commit_check.sh:110` 的本体检查只筛 `ontology/(object|operation|action|data-mock|knowledge)/*.md`，研究报告不在范围内，仅暂存 `research/` 时直接 `exit 0`。
`backend/scripts/eval_baseline.py:234`（`research_done`）、`:252`（`produced_report`）的研究阶段评分只看报告字符数 > 1500、锁已释放、有提交，不看内容。

仓库里目前**不存在**任何研究报告内容校验模块（`grep research_valid` 无命中）。

### 合成复现（已执行）

空临时目录 → 放入一份"声称 3 项实际 4 项、ID 被改名"的合成报告，运行当前基线脚本：

| 命令 | 空目录 | 放入错误报告后 |
|---|---|---|
| `state_helper.py detect-stage` | `未开始` | `调研完成` |
| `state_helper.py check-prereq extract-object` | `missing:调研完成` | `ok` |

未运行模型、未上传任何样例、未修改实现代码。该复现只证明"文件存在即放行"，不证明模型每次都会写错报告。

## 4. 推荐修复方案

按性价比排序，前两项是必须项。

### 修复 1：补全资料分析的输出契约（治"为什么写错"）

- 补全 `research-agent.md` 的 Step 4 报告模板，固定段落：`业务项清单`（逐项一行，含业务项 ID / 规则 ID / 引用字段 / 来源位置）、`分类小计`、`合计`、`数据与术语来源`、`信息来源`、`缺口与待决`。
- 明确三条硬约束：**合计 = 清单唯一项计数**（分类小计之和亦须相等）；**规范 ID 一律沿用，新增/合并/改名必须在 `ID 变更对照` 段给出旧→新与依据**；**schema 字段逐字取自真实表头，对不上就写入"缺口与待决"，不得改写或编造**。
- 在 `ontology-research.md` Step 3 派遣前注入 `TABLE_HEADERS`（复用 `python3 .claude/scripts/check_eval_data.py --headers`）和已确认的规范清单，并声明"表头是字段名的唯一依据"——与 `ontology-extract-object.md:128` 同口径。

### 修复 2：新增确定性报告检查 + 验收回执（治"为什么放行"）

- 新增 `assets/workspace-toolkit/scripts/research_checker.py`（复用 `check_eval_data.table_headers`，与 `ontology_checker.check_object_columns` 同口径），检查：计数自洽、ID 唯一且与规范清单一致、引用字段存在于真实表头、来源位置可定位。输出错误项 + 来源位置。
- `ontology-research.md` 保存报告后立即运行该检查：通过 → 写回执 `.ontology-state/research-accept.json`（含报告内容摘要 `_content_revision`、表头摘要、清单版本、检查器版本、结果）；不通过 → 保留草稿并列出错误项，不写回执。`.ontology-state/` 已 gitignore 且在 `WRITE_FORBIDDEN_TOP` 里，与现有 `eval.json` 做法一致。
- `pre_commit_check.sh` 增加 `^research/.*\.md$` 分支，调用同一检查器。

### 修复 3：三处阶段判断统一读回执

- `state_helper._compute_stage`：`research/` 有报告但无有效回执 → 新增 `调研待核验`（排序低于 `调研完成`），`extract-object` 前置因此不放行并给出修复提示。
- `workspace._project_progress`：同一判断，研究阶段未验收不计入 `done`。
- `LeftPane.jsx`：research 阶段状态改为三态——无报告 `wait/process`、有报告无回执 `已生成，待核验`、检查失败 `未通过`。
- 回执随内容失效：`write_file`（`workspace.py:1175`）已有 `_content_revision`，保存 `research/` 下文件或源资料、规范清单变更时即判回执失效。
- **旧项目兼容**：无回执一律显示"待核验"，保留既有报告与已生成资源，不自动删除或重建。检查器异常/缺失时显示"无法核验"，不默认放行。

### 修复 4：测试与评分

- 单元测试（全合成数据）：计数不自洽、ID 改名、字段不存在、正向正确样例各一例。
- `state_helper` 与 `_project_progress` 增加"错误报告已存在但未通过"用例；`LeftPane` 增加三态一致性用例。
- `eval_baseline.py` 研究阶段评分加入"回执存在且通过"与"错误项断言"，替换当前仅看字符数的评分。

### 不要这样做

- 只改前端文案或只加一句"请认真检查"的提示词：后端与 CLI 前置仍放行，错误报告照样被下游使用。
- 只在 git hook 里拦：Web 保存路径不走 hook，换执行引擎即失效。确定性结论必须由受控检查器产出。

## 5. 实施情况

修复已在本分支实施，设计取舍见
`modeling/onto-modeling/docs/decisions/0011-research-report-acceptance-gate.md`。

| 方案 | 落地位置 |
|---|---|
| 补全输出契约 | `agents/research-agent.md` 的 Step 4 模板与 7 条硬约束；`commands/ontology-research.md` Step 2.5 固化并注入只读来源快照 |
| 生产/消费段落契约合一 | 模板沿用消费侧既有的 `核心业务对象候选` / `核心业务逻辑候选` / `详细信息` 段与 `- **名称**：` 加粗格式，并由检查器判定（原先这套契约只写在 `coverage-check-agent` / `extract-objects-agent` / `extract-operation-agent` 里） |
| 确定性检查器 | 新增 `scripts/research_checker.py`（复用 `check_eval_data.table_headers` 与 `ontology_checker` 的表格解析） |
| 验收回执与失效 | 新增 `scripts/research_review.py`；回执记录全部输入指纹，任一输入改动即退回「待核验」 |
| 三处阶段统一 | `state_helper.py` 新增 `调研待核验`；`backend/app/services/workspace.py` 的 `_project_progress` 改读回执并返回 `research_review`；`LeftPane.jsx` / `ChatPane.jsx` 按该状态显示与建议 |
| 提交与评分 | `pre_commit_check.sh` 增加 `research/` 分支；`backend/scripts/eval_baseline.py` 研究阶段拆出 10 分内容验收 |
| 测试 | `backend/tests/test_research_review.py`（24 例，全合成数据） |

三个现象对应的检查项：

| issue 现象 | 检查项 | 判定 |
|---|---|---|
| 报告写"合计26项"，实列28个业务项编号 | `_check_counts`：合计 == 清单唯一业务项ID数，分类小计之和 == 同一个数 | 错误 |
| 部分原有规则ID被换名、未提供变更对照 | `_trace_ids` ID 溯源（首轮）+ `check_id_stability` 跨轮次基线对比 | 错误 |
| 源表字段写成表头中不存在的名称 | `_check_rows` 字段对账（对 `table_headers`） | 错误 |
| 「仍显示阶段完成」 | 回执 + 三处阶段统一 | `调研待核验` / 「未通过」，`extract-object` 不放行 |

**ID 溯源是补洞补上的**：issue 现场是**首轮**调研，规范清单来自用户上传的 21 份业务
依据文档而非前一版报告，所以只做"跨轮次基线对比"捉不到那次改名。现在每个 ID 还要能
逐字溯源到：自己声明的来源文件 → `resource/` 下的文本资料 → 快照里已验收的 ID →
「ID变更对照」声明的新增 ID；四处都没有即判错。溯源语料不含任何报告正文（报告会被
本轮覆盖，读它等于自证）。

已验证：`test_research_review.py` 24 例通过；全量后端测试的失败集与基线 `9aece304`
完全一致（107 项，均为 Windows 符号链接权限与缺失可选依赖导致的环境问题）；
前端 `npm run build` 通过。前端状态映射未补单测：`stageStatuses` 在 `LeftPane.jsx`
里（含 JSX 与 antd import），`node --test` 无法直接 import，需先把纯逻辑抽到
`src/lib/`（同 `subagentEvents.js` 的做法）才能补。
临时工作区复现 issue 首轮场景（规范清单在资料里、报告把 `RULE-02` 写成 `RULE-02-NEW`）：
`[错误][ID溯源] 1 个 ID 在资料与既有报告里都找不到：ITEM-02:规则 ID RULE-02-NEW`，
`detect-stage=调研待核验`；改回规范 ID 后 `已验收` / `调研完成` / 前置 `ok`。

### 超出本 issue 的相邻范围（本轮未做）

- 报告与下游知识资源没有逐项对账。源字段一致是因为报告侧与对象侧
  （`ontology_checker.check_object_columns`）各自对同一个 `table_headers` 对账。
- 规则数量与规范规则 ID 没有对到 `ontology/rule/`：`/ontology-extract-rule-metric`
  不读调研报告，`rule-template.md` 也没有承载规范 ID 的字段。issue #362 现场尚无任何
  正式对象/规则，不涉及这一段；要闭环需改规则文档结构（ADR-0004）。
- 「缺失不隐式补 1 或 0」只做成提示词硬约束，无法确定性判定。
- 规范清单若放在 `.xlsx` 里，ID 溯源读不到（语料只含文本后缀），需声明或另存为文本。
