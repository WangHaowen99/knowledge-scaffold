# 评测优化闭环并入 onto-modeling 进度流程（方案）

状态：方案待拍板（2026-09-21 起草）。拍板后补 ADR。

## 0. 背景与目标

modeling 工作台有一套完整的**发布后评测优化闭环**（其 9 步流程的"数据源绑定 → 评测 →
失败诊断 → 优化更新"段），onto-modeling 目前只有建模主链（资料分析 → … → 场景验证 →
codegen），发布知识包后无质量闭环。目标：把评测优化**有机并入 onto-modeling 的进度
流程**，并让"评测数据导入"与已有的**资料用途标签体系（数据 Schema 模式）**结合，
使 schema 成为从"提取事实源"到"评测数据契约"的同一根锚。

## 1. modeling 评测闭环现状盘点（源码核实）

组成（`modeling/src/evaluation/`，27 个模块）：

| 环节 | 机制 |
|---|---|
| 测试用例 | `(question, ground_truth)` 对，按项目存库；支持手录 / 批量 / **prefill 预填**（API：`/api/projects/:id/testcases*`） |
| 数据源绑定 | 项目绑定评测数据源：`nl2sql_template` / `nl2sql_http` / `neo4j`（`/data-source`） |
| 评测数据库导入 | 用户上传 Excel 数据表 → worker 灌入 nl2sql 目标库（`/evaluation-database/import`、`quick-import`） |
| 影子工作区 | 知识包 zip 发布到 consumer 的隔离评测工作区（shadow-workspace，经 `CONSUMER_API_URL` + `MODELING_SERVICE_TOKEN`） |
| 执行引擎 | 逐用例把 question 交给"挂着知识包 + 数据源"的 consumer 智能体，取 final_answer（engine + case-runner，并发限流） |
| Judge | LLM 判分 pass/partial/fail + 理由（含口径对齐、数值容差等精细规则） |
| 失败诊断 + 优化 | 失败用例 → 诊断 → 生成针对 spec（objects/operations/rules/metrics/scripts）的优化候选 → 应用回知识包（optimizer / optimization-apply） |

关键认识：**这套闭环评的是"知识包在 consumer 里回答业务问题的能力"**，被测对象是
发布物 + 数据，与建模引擎无关——因此它对 Claude/Jiuwen 双引擎天然中立。

## 2. 结合总体设计

### 2.1 进度链新增第 10 阶段「数据评测」（可选，发布门禁语义）

```
… → 质量检查 → 示例数据 → 场景验证 → codegen(可选) → 【数据评测(可选)】→ 发布知识包
```

- 左栏进度加「数据评测」阶段（探针：`reports/eval-*.md` 或 `.ontology-state/eval.json`）；
- 与 modeling 的发布门禁语义对齐：**导出/发布知识包入口旁显示最近评测通过率**，
  未评测或分数低给黄条提示（不硬阻断，onto-modeling 定位是引导式）。

### 2.2 三个数据结合点（本方案的核心，回应"与数据 schema 模式结合"）

1. **schema 标签资料 = 评测数据契约**。`materials.json` 里 `purpose=schema` 的表既是
   对象属性的提取事实源，也定义了评测数据表的结构。评测数据导入时做**三方对账**
   （确定性检查，不经模型）：
   `导入数据表的列 ↔ schema 资料的字段 ↔ 对象属性清单的英文名`，
   缺列/多列/类型不符逐条列出后再入库——数据问题在导入时暴露，而不是评测跑完才发现。
2. **mock 阶段产物 = 默认评测种子**。没有真实数据时，`ontology/mock/` 的示例数据
   （本就按对象属性生成）直接转为评测库种子，零额外导入即可跑通评测链；
   有真实数据时走 3 的导入通道覆盖。
3. **trace 场景 = 评测用例来源**。`ontology/testcase/` 的场景验证产物（场景 + 判断
   分支 + 预期结论）自动转换为 `(question, ground_truth)` 用例集批量注入
   （modeling 的 `testcases/batch` + `prefill` 现成），支持在界面上手动补充修订。

新增资料用途标签：`eval_data`（评测数据）——用户上传真实业务数据表时打此标签，
平台触发三方对账 + 导入评测库；与 `schema`（结构契约）形成"一个管结构、一个管数据"
的成对语义。

### 2.3 架构：复用 modeling 评测引擎，优化环节由建模会话承担

**方案 I（推荐）：onto-modeling 作为 modeling 评测 API 的编排方。**

```
onto-modeling 后端                     modeling 服务                consumer
────────────────                      ─────────────                ────────
发布知识包 zip ────────────────────▶ 影子工作区 provisioning ───▶ 评测工作区
eval_data 资料（三方对账后）───────▶ 评测数据库导入 worker
testcase/ 场景 → 用例集 ───────────▶ testcases/batch
触发评测 ──────────────────────────▶ evaluate（engine+judge）───▶ 逐用例问答
拉取结果 ◀───────────────────────── 判分 + 失败明细
落 reports/eval-*.md（git 版本化）
```

- 复用理由：影子工作区、数据导入 worker、engine/judge/口径规则是重资产且已生产验证；
  根一键部署本就同栈装 modeling + consumer + onto-modeling，`MODELING_SERVICE_TOKEN`
  跨服务令牌机制现成，auth 同源（方案 A）；
- **优化环节不复用 modeling 的 optimizer 模块**：失败用例与诊断落进
  `reports/eval-*.md` 后，走 onto-modeling **既有的报告评审 UI**（编号勾选 →
  "让助手修复选中项"）——优化建议与应用由建模会话完成，改动直接进工作区 git，
  修完重新发布重评。这是"有机结合"的关键：modeling 里独立的 optimizer LLM 模块，
  在 onto-modeling 里天然由建模 agent 承担，且复用已有交互，不新造界面。

**方案 II（不推荐）**：把 27 个 TS 评测模块移植进 onto-modeling 后端。工作量数周，
且影子工作区/数据导入的 consumer 侧对接要重写，短期收益为零；仅当未来要下线
modeling 服务时再评估。

## 3. 实施分期

### P1：数据通道（onto-modeling 侧独立可交付，约 1 人周）

- `materials.json` 用途枚举加 `eval_data`；上传/打标 UI 复用现有用途下拉；
- **三方对账检查器**（确定性脚本，进 workspace-toolkit/scripts）：数据表列 ↔ schema
  字段 ↔ 对象属性对账，产出对账报告；前端资料区在 eval_data 资料上显示对账状态；
- mock → 评测种子转换器（`ontology/mock/` 表格 → 评测导入格式）。

### P2：评测执行（对接 modeling API，约 1.5 人周）

- 配置：`ONTO_WEB_MODELING_URL` + service token（部署链默认同栈直连，与
  `CONSUMER_API_URL` 同模式）；
- 后端编排服务：发布 zip → 影子空间 → 数据导入 → 用例注入（trace 转换 + prefill）→
  evaluate → 轮询结果 → 报告落 `reports/eval-<时间戳>.md`（含逐用例判分与失败理由）；
- 左栏「数据评测」阶段 + 阶段按钮（绑定数据 / 运行评测）；概览加评测通过率磁贴。

### P3：优化闭环与门禁（约 0.5 人周）

- eval 报告接入报告评审 UI（编号勾选 → 让助手修复 → 重评按钮）；
- 导出/发布入口显示最近评测分与"未评测"黄条；
- 项目卡与进度显示评测状态。

## 3.5 两个已澄清的问题（2026-09-21 答疑）

- **UI 是否新增页面**：基本不新增。上传/打标走现有资料区（用途下拉加「评测数据」，
  对账状态为行内徽标）；运行走左栏阶段按钮；结果查看与修复走既有报告评审 UI
  （当前隐藏的「报告」视图届时重新露出）。唯一新增是 trace 转用例后的复核列表，
  做成阶段按钮弹出的 Modal，不做独立页面。
- **modeling 容器是否需在线**：评测执行期间需要 modeling + consumer 同时在线
  （引擎在 modeling，答题在 consumer）。非评测期间 modeling 可停：onto-modeling
  探测 `ONTO_WEB_MODELING_URL` 不可达时「数据评测」阶段自动隐藏/置灰并给出提示，
  不阻塞主链；独立部署时该阶段整体不出现。若未来决定下线 modeling 服务，
  再评估方案 II（引擎移植）。

## 3.6 落地记录

- **P1 完成（2026-09-21，v0.12.0）**：eval_data 用途标签（前后端 + agent 规则语义）、
  三方对账检查器 `check_eval_data.py`（工具箱脚本单一实现 + `/eval-data/alignment`
  端点 + 资料区行内徽标）、`mock_to_eval_seed.py` 种子转换器。
- **P2 完成（2026-09-21，v0.12.1）**：`services/modeling_eval.py` 编排服务——
  发布包 zip → `POST /api/package-workspaces/import`（modeling 一步建项目+导入包）→
  `testcases/batch`（trace TC 段落自动转换 question/groundTruth）→ `evaluate` →
  轮询 `rounds/:id` → 报告落 `reports/eval-<ts>.md`（git 提交）+ 状态落
  `.ontology-state/eval.json`；左栏「数据评测」阶段（前后端探针 `reports/eval-*.md`，
  可选语义）+ 运行按钮（未配置隐藏提示 / 不可达置灰）+ 用例复核 Modal
  （可改可删可手工补）+ 最近得分 Tag（点击打开报告）。
  鉴权：透传当前用户 token，`ONTO_WEB_MODELING_TOKEN` 兜底。
  **编排协议经 stub modeling 服务全链验证**（import→batch→evaluate→poll→报告/提交
  顺序与载荷断言）；81 后端测试全绿。
- **部署级确认结果**：nl2sql 评测库 = modeling 侧 `EVAL_DB_URL`（仅 MySQL）；
  本机与 tc 的 modeling 实例均未配置——数据导入环节待部署决策后接通
  （编排里预留 data_source/import 步骤位）。真实栈端到端验证需同栈网络 +
  共享账号中心（方案 A），属部署拓扑变更，待用户批准后执行。

## 4. 依赖与开放问题（拍板时定）

1. **部署形态**：评测要求 modeling + consumer 与 onto-modeling 共栈（根一键部署已满足）；
   独立部署 onto-modeling 时「数据评测」阶段自动隐藏（探测 `ONTO_WEB_MODELING_URL`）；
2. **评测库目标**：沿用 modeling 的 nl2sql_template 路径作为默认（最小形态），
   neo4j / nl2sql_http 作为高级选项透传——最小部署是否需要额外数据库实例，
   需在 P2 前对 modeling 的 evaluation-database-target 做一次部署级确认；
3. **trace → ground_truth 质量**：场景预期是文字结论，judge 的口径对齐规则可判，
   但预填后应引导用户在界面上复核修订（P2 的用例管理页用 modeling 现成 UI 还是
   在 onto-modeling 内嵌一个轻量列表，倾向后者，工作量已含在 P2）；
4. **双引擎中立性**：评测对象是知识包 + 数据，不区分建模引擎；Phase 3 的引擎评测
   基线（eval_baseline.py，评"建模过程质量"）与本方案（评"发布物业务问答质量"）
   互补，不合并。
