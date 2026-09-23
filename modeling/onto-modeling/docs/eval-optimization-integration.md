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

## 3.7 真实栈端到端验证记录（2026-09-21，方案 A 共享账号中心）

本机 onto-modeling 切换为外接 consumer 栈账号中心（`ONTO_MODELING_EXTERNAL_AUTH_NETWORK`
一个变量 + 重跑 deploy.sh），注册/登录/评测 token 透传全链打通。真实 modeling 上
逐步验证结果：

- ✅ 包导入（`package-workspaces/import` 自动建项目）
- ✅ 数据源配置（实测发现 modeling 要求显式配置，`mode=none` 步骤已补进编排）
- ✅ 用例注入（trace 转换的用例 batch 成功）
- ✅ evaluate 启动 + 轮次创建 + 轮询 + 失败日志透出
  （failed 轮不再误报 0 分报告，状态带 modeling 侧原因）
- ⛔ 轮次执行：本机 modeling 栈缺 `MODELING_SERVICE_TOKEN`（≥32 字符跨服务令牌，
  影子工作区 provisioning 依赖）——**部署配置项**，代码链路已验证到此深度；
  正式栈还需 consumer 真实 LLM（本机 faux 模式）与 `EVAL_DB_URL`（MySQL，数据导入）。

结论：onto-modeling 侧代码完备；剩余全部为 modeling/consumer 栈的部署配置
（SERVICE_TOKEN / EVAL_DB_URL / LLM），随根一键部署整改一并解决。

### 3.8 数据源对接落地（2026-09-22）

- 评测支持 `none` / `dataset` 两种数据源模式：dataset 走 modeling quick-import
  （建项目评测库 + Excel 导入 + 自动绑定 NL2SQL），导入 done 后才 evaluate；
- 数据来源：eval_data 标签资料（多选）或 mock 种子（mock_to_eval_seed，每对象一
  Sheet，与知识包"来源Sheet"默认映射一致）；UI 为两步向导（数据源选择卡 → 用例复核）；
- 部署形态：modeling compose `eval` profile = mw-eval-mysql + mw-modeling-nl2sql
  （同 mw-modeling 镜像跑 dist/nl2sql/server.js），`EVAL_DB_URL` + `MODELING_EVAL_BINDING_URL`
  两个 env 接线；评测库未就绪时 quick-import 503 被翻译为带部署指引的失败信息；
- 端到端实测（本机真实栈）：mock 种子 → MySQL 建库导入 → NL2SQL 绑定 →
  consumer 查库作答（含聚合统计）→ 真实 judge 判分，2/2 pass 得分 100。

### 3.9 停止评测与中断恢复（2026-09-22）

编排只活在 onto-modeling 进程内（`_running` / `_live_tokens` 是内存态），而状态落在
`.ontology-state/eval.json`。进程重启（如容器重新部署）会让编排线程消失、状态永远
停在 `running`，前端"运行评测"按钮据此置灰，形成不可恢复的死锁——即使 modeling
侧轮次已经跑完。本次按三层收敛：

- **停止评测**：`POST /api/projects/:id/eval/stop` → 调 modeling
  `POST /api/projects/:mp/evaluate/cancel`（带 `roundId`；404/409 视为目标已达成），
  状态落新终态 `stopped`；进程内有编排时同时置协作取消标记，轮询下一拍抛
  `EvalStopped` 退出，编排在"发起 evaluate 前"的取消点停下即不产生模型开销。
  已完成用例的判分结果保留在 modeling 侧，轮次坐标保留在状态里。
- **中断判定**：进程启动时 `sweep_interrupted()` 扫描各工作区，`running` 一律改判
  `interrupted`（新进程里必属孤儿）；`/eval/status` 用 `status_view()` 补一道运行期
  判定——内存无编排 + `ts` 停滞超过 `STALE_AFTER_SECONDS`（5 个轮询周期）即改判中断。
  两者都保留 `modeling_project` / `round_id`，并在响应里补 `resumable` 标记。
- **续接**：`POST /api/projects/:id/eval/resume` 复用 `_try_resume`，只 GET 已有轮次
  （running → 继续轮询；done → 直接落报告并 git 提交），**绝不重新发起评测**；
  轮次已失败或不存在时回 409 / 落中断提示，引导重跑。前端不再仅凭 `status === 'running'`
  置灰：运行中显示「停止评测」，中断/失败且可续接时显示「续接上一轮」。

### 3.10 对象↔数据表绑定与发布包数据映射（2026-09-22）

数据源接通不等于数据通道接通。此前发布包里三处都没有真实表名：`objects/index.md`
的「来源Sheet」列回填的是对象 key、对象文档没有 modeling 契约要求的 `# 字段` 段、
包里根本没有 `mappings/object-data-mappings.json`。modeling 导入后对象的 `fields`
全为空、`sourceRefs` 指向一个并不存在的表，nl2sql 无表无列可用，于是每个用例都只
读了知识包上下文、一次库都没查——评测照跑，还给出一个看起来不错的分数。

- **单一实现**：`app/services/package_mapping.py` 解析对象文档与 eval_data/schema
  表格资料，产出对象→表、属性→列的绑定，导出、评测门禁、资料区对账三处共用，
  不各自再实现一遍（工具箱 `check_eval_data.py` 仍只做列级对账，不重复表级绑定）。
- **绑定来源**：对象文档新增 `# 数据来源` 段显式声明真实表名（模板已加，优先级最高）；
  未声明时按「属性英文名 ∩ Sheet 列头」的重合度推断，要求命中 ≥2 列且候选唯一，
  达不到就留空（来源Sheet 写 `—`）而不是猜一个——错绑比不绑更难被发现。
- **导出产物**：`来源Sheet` 写真实表名；对象文档的「对象属性清单」按契约改写为
  `# 字段`（前六列按位解析，`必填` 由主键列推导，作者填写指引不进包）；绑定成功的
  对象补 `mapping` / `sources` frontmatter，并生成 `mappings/object-data-mappings.json`
  （`sources[].fields[].source_column` 恒写，modeling 靠它回填 `sourceColumn`）。
  库里没有对应列的属性仍留在 `# 字段` 里（业务语义不丢），但不进 mapping——
  写进去只会让 nl2sql 生成查不到的列。
- **前置门禁**：数据集模式发起评测前跑 `assert_data_binding()`，声明的表不存在、或
  没有任何对象绑定到表时直接判失败并给出整改指引，不再静默跑出一个没查过库的分数。
- **对账合流**：`/eval-data/alignment` 在列级对账之外返回 `binding`（每对象的表、
  匹配方式、命中列数、缺列）与 `problems` / `warnings`。
- **两端验证**：生成包经 modeling 契约（`parseObjectIndex` / `parseObject` /
  `mappingTableSources`）与 consumer 校验器（`validate_object_data_mappings`、
  `validate_field_aliases`、`validate_object_aliases`、`inspect_importable_package`）
  双向核对通过。

### 3.11 数据源 provider、评测集与异常轮演进（2026-09-23）

- **外部数据源（对齐 modeling 的三种 provider）**：数据源新增 `connection` 模式，
  provider 为 `nl2sql_template` / `nl2sql_http` / `neo4j`，字段与校验口径同 modeling
  （`evaluation/data-source.ts`：Scene、评测工作空间 ID、Neo4j 隔离条件）。编排在包导入后
  `PUT /data-source`，由 modeling 测试连接并同步到 consumer；Neo4j 的 Ontology 隔离值
  必须等于知识包 ID，发起时按本次发布的包 ID 自动填写。连接失败落 `failed`，
  状态里写 modeling 给出的原因。
- **数据源配置按项目保存**（`services/eval_data_sources.py`）：`none` / `dataset` /
  `connection` 都存成可复用的配置。每轮评测都会在 modeling 新建评测项目，凭据必须由
  onto-modeling 保存才能复用，所以配置落数据目录 `eval-data-sources/<项目>.json`
  （0600），不进工作区 git，也不随导出、克隆带走。读接口只返回 `has_token` /
  `has_password`，编辑时密钥留空表示沿用。发起评测传 `data_source_id`，
  外部连接不接受内联提交。
- **评测向导**：项目从未配置过数据源时，「运行评测」直接进配置表单（必须先配置）。
  配置过的项目弹出「选择数据源」窗口，可新建、编辑、删除。
- **评测集**（`services/eval_sets.py`，落 `.ontology-state/eval-sets.json`）：
  导入 JSON 用例时默认同时保存为评测集（名称取文件名），之后的评测默认选中
  最近使用的评测集，也可以切换到场景验证转换或手工录入，并支持「另存为评测集」。
  运行时对所选评测集的修改会回写到评测集。
- **异常轮也能触发 `/ontology-evolution`**：modeling 在连续环境异常时会熔断整轮（round
  `failed`），而旧编排只要遇到 `failed` 就不出报告，异常用例因此进不了演进。现在的处理：
  有部分结果时照样出报告，状态为 `failed`，未执行的用例计为异常（E 类·运行异常）；
  没有任何结果时仍按编排失败处理。是否提示演进统一由 `modeling_eval.evolution_hint()`
  判定：得分低于 90、存在异常用例，或者轮次异常终止但已出报告，三者满足其一即提示。
  判定结果经项目 meta `eval_evolution` 和 `/eval/status` 的 `evolution` 下发，
  聊天区建议和左栏「评测优化」按钮共用这一个结果。`report_round` 记录报告出自哪一轮，
  新一轮发起时清空，避免拿旧报告提示。另外，前端监测到评测进入终态后会刷新项目 meta
  和文件树。此前刷新缺失，评测结束后建议不会及时出现。

### 3.12 一键导入 Excel / Neo4j，mapping 与导入库同源（2026-09-23）

- **导入工作簿同源**（`services/eval_import.py`）：dataset 模式和「评测前导入图数据」
  的 Neo4j 配置，都先把选中的数据（eval_data 资料表 / mock 种子）整理成一份规范化
  工作簿 `eval-data.xlsx`。对象绑定按这份工作簿重新计算（`resolve_object_tables`
  传入导入表），发布包 mapping 从这个绑定导出。这样一来，导入库建什么表、什么列，
  和 mapping 引用什么表、什么列，出自同一份表结构；导出后 `verify_mapping` 逐项核对
  每个 Sheet 和 `source_column`，不一致就阻断。此前 mapping 取自全部 eval_data/schema
  资料的表头，schema 资料比实际数据多列、只勾选部分文件、Sheet 只有表头、mock 用中文
  表头时，mapping 会引用库里不存在的表或列。
- **整理规则**：
  - 表头去掉首尾空白；空表头列丢弃；大小写不敏感的重名列、超过 64 字符的列名直接阻断；
  - 多份文件出现同名 Sheet 时阻断；没有数据行的 Sheet 不导入，绑定到它的对象不进 mapping；
  - 次行注释的判定与 modeling 解析同一口径；
  - mock 种子的中文表头按对象文档换成属性英文名，Sheet 名换成对象声明的来源表名；
  - 以 `=` 开头的文本写成文本单元格，不会被当作公式；`.xlsm` 也统一转成 `.xlsx`
    （modeling 只收 xlsx/xls）。
- **日期**：
  - modeling 解析层只按前缀 `^\d{4}[-/]\d{1,2}` 判定日期列，于是年月（2026-01）、带文字
    后缀、非法日期、带时区的 ISO 串都会被建成 DATETIME，原样写入后，MySQL 严格模式整批
    拒绝（真实 MySQL 8 复现：`Incorrect datetime value: '2026-01'`）；
  - 两侧都做了处理：onto 侧只有整列都是完整、合法的日期或日期时间时，才统一写成
    `YYYY-MM-DD` / `YYYY-MM-DD HH:MM:SS`（兼容 Excel 日期单元格、`2026/1/5`、
    `2026年1月5日`），否则保留文本原样；modeling `eval-db.ts` 新增
    `withStorableColumnTypes`，推断为日期但有值无法无损写入的列降级为 TEXT，
    DATETIME 值统一为补零的标准格式。
- **Neo4j 一键导入**：
  - Neo4j 配置可以选择评测前导入示例数据或业务数据表，这时必须填写 Ontology 隔离字段
    （modeling 图导入要求两组隔离条件齐全）；
  - 编排在 `PUT /data-source` 之后，依次调用 modeling `graph-import/uploads` → `validate`
    → `execute`（`replaceExisting: true`，只清空「工作空间 + 知识包 ID」隔离范围内的数据）
    → 轮询 `graph-imports`；
  - 关系关联由 `graph_relation_mapping` 推断：同一 Sheet 的两类对象按同行关联；跨 Sheet 时
    找「一端主键在另一端有同名属性」的外键约定，按字段值关联；推断不出的关系跳过，
    并在执行详情里给出说明。
- **图模式导出**：
  - modeling 对 Neo4j 包的约定是 mapping 的 `resource` 为对象 key（即 Label），`fields`
    覆盖对象的全部字段，Sheet 与列由 `sources[]` 声明；
  - 对象文档的「关联对象」转为 `# 关系` 表，只收两端都有数据表、关系名为合法图标识符的关系
    （modeling 会校验包内全部关系，不合规的一条就会让整包失败）；
  - 属性英文名不是合法图标识符时，在发起前阻断。
- **向导**：「导入预览」列出本轮要导入的表、日期列的规范化结果、对象到表的对齐情况
  （即 mapping 内容）、阻断问题和提示；选中已保存配置时同样展示。执行详情显示图数据导入的
  节点数、关系数和提示。
- **验证**：用 onto 导出的包和工作簿，直接调用 modeling 的契约代码：`parsePackageArchive`
  + `runFormatChecks` 零错误；`extractGraphImportSchema` 和 `validateGraphImportMapping`
  通过；`writeGraphImport` 写入真实 Neo4j 5，关系建成、日期为 `DATE` 类型；
  `importEvaluationDatabaseTables` 写入真实 MySQL 8，mapping 引用的列在库里全部存在。

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
