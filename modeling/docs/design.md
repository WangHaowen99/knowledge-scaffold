# 知识包建模工具 · 设计文档

> 状态：设计定稿（经 12 项决策逐项评审确认）· 2026-07-08
> 范围：`modeling/` 下的独立 Web 服务——基于 pi agent 的动态 workflow 交互式建模，覆盖「资料摄取 → 访谈建模 → 知识包生成 → 评测 → 优化」全闭环。

## 0. 定位


## 1. 决策记录

| # | 决策点 | 结论 |
| --- | --- | --- |
| 1 | Agent 底座 | [pi](https://github.com/earendil-works/pi)（`@earendil-works/pi-agent-core`，TS，MIT）。pi 是 harness 非 workflow 引擎：**动态 workflow 由本服务在 pi 之上自建**（节点编排器 + 会话状态机），人机交互建模为 `pending_interaction → 挂起 → 前端应答 → resume` |
| 2 | 技术栈 | modeling = 仓库首个 **Node 全栈服务**：Fastify + pi-agent-core（同进程）+ React 前端；与 consumer（Python）仅经 HTTP 协作 |
| 3 | 认证 | **直连独立 auth-server**（`AUTH_ISSUER_URL` 指向 auth-server；2026-07-10 起移除 introspect 模式，单一路径避免部署歧义）：登录/续期走 OAuth2 `/token`（password/refresh grant），JWKS 本地 RS256 验签（`AUTH_JWT_ISSUER`/`AUTH_JWT_AUDIENCE` 配期望 claims，锁死 alg、强制 `typ=access`）；注册经本服务一步转发 auth-server 的 `/register`（一步建正式用户、无邮箱验证码，带 X-Forwarded-For 供限流）；强制改密用户（pwd_cr）拒绝进入（403 PASSWORD_CHANGE_REQUIRED，modeling 无改密入口）。前端 401 单飞静默续期，对齐 consumer ADR D6 方案 a。评测闭环的 consumer 业务 API（`CONSUMER_API_URL`）为另一服务，须信任同一 auth-server 才能接受透传的专家 token |
| 4 | 与 consumer 闭环 | **全自动 API 直连**（专家 token 透传）：配置时 `import-zip` → `PUT ontology data-source`，评测时 `import-zip` → 复用 ontology data-source → 逐用例 `runs/sync`；评测跑在**建模影子工作区**（普通 workspace，靠 X-Workspace-Id 隔离，专用命名 + 用后可清理）；导出 zip 仅作兜底 |
| 5 | Excel 语义 | 一个 xlsx=一个库；**sheet=表**（sheet 名=表名），首行=字段名，次行可选注释，余行=数据。三段用途：① 入项目数据字典（驱动访谈与映射）② 推导 objects 字段与 `object-data-mappings.json`；每个 object 必须绑定真实 Sheet、字段必须存在于该 Sheet，同一 Sheet 可映射多个 object，`objects/index.md` 的 `来源Sheet` 是 Excel 图导入选表依据 ③ 在数据源配置步骤全量、异步写入项目连接的 MySQL 或 PostgreSQL 评测库；可用具备建库权限的账号幂等创建固定名称 `mv_shadow_<project_id>` |
| 6 | 访谈交互 | 三种题型（单选/多选/选项+自由文本），每题带「其他」「跳过」；**按节点批次出题（1~5 题/批）**；聚焦四类决策点（对象划分/字段含义/关系/口径），场景背景在建项目时一次性填写；**支持回溯修改**，改后从该节点重新规划（仍有效的后续答案保留预填） |
| 7 | 胜任力问题 | **建模↔评测之桥**：候选 10~20 条 → 专家勾选/编辑/补充（至少 5 条方可进入生成）；双重落地——operations 必须覆盖选中问题（生成后覆盖检查，未覆盖标红）+ 自动预填评测用例 `question` |
| 8 | 评测与优化 | **LLM Judge** 判分（通过/部分/失败+理由，专家可改判）→ 优化器输出**结构化建议**（精确到文件段落、预期修复的用例）→ 逐条采纳后 agent 改包、**版本递增、自动重评**；首版内置轻量评测，不接 `evaluation/agent_eval` |
| 9 | 持久化 | **SQLite**（项目/workflow 状态/访谈/用例/评测轮次/建议）+ **文件系统**（原始文档、解析产物、包各版本工作副本），compose 挂卷；会话断点恢复；项目仅 owner 可见；与评测 MySQL 严格分离 |
| 10 | LLM 配置 | modeling 独立 `.env`（`MODELING_LLM_BASE_URL/MODEL/API_KEY` + 可选轻量模型），OpenAI-compatible 直喂 pi；**不**从 consumer 取密钥（不破坏「密钥永不出全局层」） |
| 11 | workflow 节点 | 固定主干 10 节点（下文），1~6 顺行+可回溯、7~10 自由往返；**动态节点限 4 类**（追问访谈/深读文档/数据探查/冲突消解），每次插入产出可见节点卡片说明理由 |
| 12 | 前端 | 复用控制台 `.mwr` 设计体系（首版拷贝 tokens）；四页面：项目列表 / 建模工作台（三栏）/ 包评审（可编辑）/ 评测中心；部署默认：compose 服务名 `modeling`，后端 5100、前端 dev 6700；NL2SQL 评测库按项目连接 MySQL 或 PostgreSQL |

## 2. 系统架构

```mermaid
flowchart LR
  Expert["领域专家"] --> FE["建模前端（React · .mwr 设计体系）"]
  FE --> BE["modeling 后端（Fastify + pi-agent-core）"]
  BE --> WF["动态 workflow 引擎（节点编排 + 会话状态机）"]
  WF --> PI["pi agent（LLM 统一层 · 工具调用 · 状态管理）"]
  BE --> DB[("SQLite 项目库")]
  BE --> FS[("文件卷：文档 / 解析产物 / 包版本")]
  BE --> EVALDB[("评测 MySQL（每项目一 schema）")]
  BE -- "AuthClient(introspect)" --> Consumer["consumer API"]
  BE -- "import-zip / data-source / runs·sync" --> Consumer
  Consumer --> Shadow["影子工作区"]
  Shadow --> EVALDB
```

- **workflow 引擎**（自建，~核心模块）：节点图定义、节点状态机（`pending / running / waiting_user / done / stale`）、动态节点插入、回溯重规划（节点答案变更 → 其下游节点标 `stale` → agent 重新规划）。
- **pi 集成层**：每个「agent 型节点」对应一次 pi agent run，注入节点专属 system prompt 与工具集（读数据字典、读文档切片、写包文件、SQL 探查等自定义 tool）；`waiting_user` 时序列化 agent 状态，应答后恢复继续。
- **consumer 客户端**：封装 import-zip（409 覆盖确认）、data-source、runs/sync（含 429 限流退避）、影子工作区建删。

## 3. 固定主干与动态节点

```
1 项目创建        场景背景一次性表单（领域、目标用户、术语偏好）
2 资料上传与解析   md/txt/pdf/word/excel；excel → 数据字典
3 初步分析        领域概览 + 候选对象清单 + 「拿不准点」清单
4 建模访谈        多批次出题（动态插入追问）
5 胜任力问题      候选 → 勾选/编辑/补充
6 知识包生成      objects/operations/mappings/index/manifest + 覆盖检查
7 包评审          目录树浏览 + 任意文件手工编辑
8 评测用例录入    question 预填 + ground_truth 补全
9 评测运行        影子工作区：导入 → 复用 ontology data-source → runs/sync → LLM Judge
10 优化建议与应用  采纳 → 新版本 → 回到 9；专家满意 → 交付/导出
```

动态节点（agent 在 3~6 间按需插入，类型封闭）：

| 类型 | 触发场景 | 产出 |
| --- | --- | --- |
| `追问访谈` | 某决策点置信度不足 | 追加一批题 |
| `深读文档` | 初析发现关键段落需精读 | 结构化提取结果 |
| `数据探查` | 需验证唯一键/口径/分布 | 对评测库跑聚合 SQL 的结论 |
| `冲突消解` | 文档与数据矛盾 | 出题请专家裁决 |

## 4. 数据模型（SQLite 主表）

`projects`（owner/领域背景/阶段）· `documents`（原始文件+解析状态）· `data_dict`（表/字段/注释/样例统计）· `wf_nodes`（节点实例：类型/状态/产物引用/插入理由）· `interview_batches` + `interview_answers` · `competency_questions`（候选/勾选/覆盖的 operation）· `package_versions`（v1..vN 目录快照+变更说明）· `testcases`（question/ground_truth/来源）· `eval_rounds` + `eval_results`（final_answer/judge 结论/人工改判）· `optimization_suggestions`（目标文件段落/建议内容/预期修复用例/采纳状态）。

文件布局：`modeling-data/{projects/<id>/{uploads/, parsed/, packages/v<N>/, exports/}}`。

## 5. 评测闭环协议

1. 生成包 zip → `POST /agentic/ontologies/import-zip`（影子工作区头；已存在则 `action=overwrite_active` 或版本参数递增）；
2. `PUT /agentic/ontologies/<id>/data-source`（以 `direct_provider` 覆盖影子知识包本轮评测数据源）；
3. 逐用例 `POST /agentic/runs/sync`（`session_id` 每用例独立；429/503 退避重试）；
4. Judge（轻量模型可选）：`(question, ground_truth, final_answer) → {verdict, reason}`；
5. 优化器（pi agent run）：输入失败用例 + 包全文 + judge 理由 → 建议清单；采纳项由 agent 应用 → `packages/v<N+1>` → 回到 1。

## 6. 前端信息架构

- **项目列表**：卡片墙（名称/领域/阶段徽章/最近通过率）+ 新建；
- **建模工作台**（三栏）：左 = workflow 时间线（节点卡片，含动态节点及插入理由，完成态可回溯）；中 = 当前节点交互区（上传器/访谈答题卡/胜任力勾选/生成进度流）；右 = 上下文面板（数据字典/文档列表/包目录树，可切换）；
- **包评审**：目录树 + Markdown 渲染/源码编辑（沿用控制台知识包工作台交互 + 保存）；
- **评测中心**：用例表 → 轮次视图（通过率大数字 + 逐用例 judge 卡 + 改判）→ 建议列表（采纳/拒绝）→ 版本时间线（各版本通过率走势）。

访谈答题卡：选项为可点卡片，带「其他____」与「跳过」，批次内进度点。

## 7. 配置与部署

```bash
# modeling/.env.example
MODELING_LLM_BASE_URL=   MODELING_LLM_MODEL=   MODELING_LLM_API_KEY=
MODELING_TASK_LLM_*=                # 可选轻量模型（judge/摘要）
AUTH_ISSUER_URL=http://auth-server:5056   # 直连独立 auth-server（登录/验签/注册）
# AUTH_JWT_ISSUER=http://auth-server:5056   # 验签期望 iss（=auth-server 的 AUTH_SERVER_ISSUER）
# AUTH_JWT_AUDIENCE=meta-wise               # 验签期望 aud
CONSUMER_API_URL=http://consumer:5055     # 评测业务 API（另一服务，须信任同一 auth-server）
# 评测库连接不使用全局环境变量，在项目“数据源配置”中保存公开字段并测试连接
```

compose：`modeling`（Node，5100，挂 `modeling-data` 卷）；前端 dev 6700，生产由 modeling 服务托管静态产物。NL2SQL 项目可连接外部 MySQL 或 PostgreSQL 评测库。

评测库导入由独立 Worker Thread 执行 Excel 解析和数据库写入，单实例同时只运行一个任务，避免阻塞 HTTP 事件循环并限制大文件并发占用。导入不设置 2000 行截断；用户必须显式确认同名表替换。PostgreSQL 在事务中替换，MySQL 先完整写入临时表，再用单条原子 `RENAME TABLE` 切换，失败时保留原表。运行中的导入会阻止启动评测。

## 8. 里程碑

| 阶段 | 内容 | 验收 |
| --- | --- | --- |
| **M1 ✅** | 服务骨架 + AuthClient + 项目/上传/解析（含 excel→数据字典）+ workflow 引擎与恢复 + **前端骨架** | 传入样例资料可走到节点 3 —— **已达成**。后端：`scripts/smoke-nodes.ts` 通过（节点 1→3 端到端、Excel 数据字典、快照序列化恢复），HTTP 服务启动、认证门控、build/typecheck 通过。前端：登录 → 项目列表 → 建模工作台三栏（时间线 / 上传解析 / 数据字典）端到端联调通过（puppeteer 截图验证），build 通过 |
| **M2 ✅** | 访谈（批次/挂起恢复）+ 胜任力（候选/勾选/覆盖）+ 包生成（spec→标准包渲染）+ 导出 zip + **前端四面板** | 产出的包可被 consumer 成功导入 —— **已达成**。后端：`scripts/smoke-m2.ts` 通过（访谈跨节点挂起/恢复、胜任力 5/5 覆盖、包渲染），并用 consumer 的 `_has_converted_ontology_structure` 真实校验生成的 zip。前端：访谈答题卡（三题型/topic/其他/跳过/进度点）、胜任力勾选（≥5 门控）、生成（覆盖明细）、包评审（文件树 + Markdown 渲染/源码 + 导出 zip）——puppeteer 全新项目端到端（真实上传 xlsx → 生成 → 评审）截图验证。新增 `MODELING_LLM_BASE_URL=faux` 演示模式（脚本化假建模者，无 key 可全流程试玩）。遗留：动态节点 deepread/probe/conflict、包评审编辑 |
| **M3 ✅** | 评测闭环（评测工作空间/评测库/runs·sync/Judge/结果页） | 端到端出分 —— **已达成**（stub consumer 层面）。后端：`scripts/smoke-m3.ts` 通过（工作空间创建、import-zip 409→overwrite_version 重试、本体级评测数据源配置与复用、runs/sync 429 退避、faux Judge 判分、轮次统计、人工改判同步 pass_count）。NL2SQL 数据源保存时同步测试服务与 MySQL/PostgreSQL 评测库，随后在数据源步骤上传 Excel 并由后台任务全量写入；评测运行仅复用已准备的数据。前端：用例录入（胜任力预填/增删改）+ 数据源配置与 Excel 后台导入状态轮询 + 评测运行（数据源摘要、进度条轮询、日志、结果卡改判、轮次历史）。遗留：真实 consumer + example 13 条用例端到端验收（依赖真实环境） |
| **M4 ✅** | 优化器 + 版本迭代 + 评测中心完整 UI + 导出交付 | 两轮优化后通过率可见提升 —— **已达成**。后端：`scripts/smoke-m4.ts` 通过（内容感知 stub consumer：回答质量取决于导入包内容），两轮优化通过率 1/3 → 2/3 → 3/3；优化器 `evaluation/optimizer.ts`（propose：未通过用例 → 结构化建议 submit_suggestions；apply：采纳建议 → 修订规格 submit_revised_spec → 版本 1.N.0 递增 + 覆盖重算）；建议状态机 pending/applied/rejected（`workflow/m4-repo.ts`）；package 路由支持 `?version=` 历史版本查看/导出；`/versions` 聚合各版本评测通过率。前端：节点 10 优化面板（建议卡勾选采纳/忽略、应用生成新版本、去评测验证）+ 版本历史与交付表（通过率趋势 ↑提升 标记、逐版本导出 zip）——puppeteer 端到端（建议生成 → 应用 v1.1.0 → 复评 2/2 → 版本表提升标记）截图验证。遗留：真实 consumer + example 样例端到端验收（依赖真实环境） |

## 8.5 真实场景验收记录

| 场景 | 输入 | 结果 | 迭代 |
| --- | --- | --- | --- |
| 示例业务 DOMAIN（CHANGE_ORDER 变更成本/切换分析，计算推理型） | 16 张核心表 xlsx + 领域规则文档 ×2 | **13 例满分**：基线 3 → 10 → 11 → 13（激活修复揭示盲改冲突 → 回退标注清障 → 口径下沉查询条件） | 8 版本 / 12 轮 |
| 企业对公营销（场景→产品方案推荐型） | 10 张营销数据表 xlsx + 20 万字符产品手册 docx | **8 例满分**：基线 5(+3 partial) → 7 → 8 | 3 版本 / 3 轮 |

企业银行满分的方法论前提（均为通用管线能力，无案例特化）：长文档三件套
（大纲/分页/检索）、导入后显式激活确认、修订落实校验、协议恢复轻推、
评测库布尔映射与按表隔离、优化器执行痕迹分诊 + 回退标注。

## 9. 风险与开放问题

- **pi API 细节**：✅ 已 spike 验证（见 [spike-notes.md](spike-notes.md)）——自定义工具、状态序列化/恢复、`terminate` 挂起模式、OpenAI-compatible 接入四项全部成立；「waiting_user 序列化对象=`agent.state.messages` 数组」得到确认。✅ 真实端点冒烟已完成：`npm run smoke:real`（`.env` 配真实 OpenAI-compatible 端点）覆盖全部 6 个 agent 角色——初析（4 候选对象/8 开放问题）、访谈（收敛提示后 2 批内主动结束）、胜任力（15 候选）、包生成（4 对象/5 操作，胜任力覆盖 5/5）、Judge（正/负例判定正确）、优化器（建议→修订保留既有内容→版本迭代）；连通性快检 `npm run check:llm`；
- Node 侧文档解析选型：xlsx（SheetJS）/ mammoth（docx）/ pdf-parse，扫描版 PDF 首版不支持（明确报「不可解析」）；
- 长会话 token 过期：AuthClient 需处理评测长跑中的 401 → 前端引导重登后续跑；
- consumer 普通用户并发 run=1 的限流：评测串行执行 + 退避即可，无需改 consumer；
- 影子工作区清理策略：项目删除时级联清理其影子工作区与评测 schema。
