# 知识包建模工具 · 设计文档

> 状态：统一运行时实现基线 · 2026-07-31
> 范围：`modeling/` 下的独立 Web 服务——基于 pi agent 的可恢复 AI 辅助构建，覆盖「目标确认 → 资料建模 → 知识包治理 → 评测优化 → 发布」全闭环。

## 0. 定位

设计期造模型。领域专家通过访谈式交互把领域资料（文档 + Excel 数据表）结构化为**知识包**（`manifest.yaml + index.md + objects/ + operations/ + mappings/ + scripts/`），经 consumer 实测、优化器迭代，最终沉淀为可交付的领域知识包。

## 1. 决策记录

| # | 决策点 | 结论 |
| --- | --- | --- |
| 1 | Agent 底座 | [pi](https://github.com/earendil-works/pi)（`@earendil-works/pi-agent-core`，TS，MIT）。pi 是 harness 非 workflow 引擎：**动态 workflow 由本服务在 pi 之上自建**（节点编排器 + 会话状态机），人机交互建模为 `pending_interaction → 挂起 → 前端应答 → resume` |
| 2 | 技术栈 | modeling = 仓库首个 **Node 全栈服务**：Fastify + pi-agent-core（同进程）+ React 前端；与 consumer（Python）仅经 HTTP 协作 |
| 3 | 认证 | **直连独立 auth-server**（`AUTH_ISSUER_URL` 指向 auth-server；2026-07-10 起移除 introspect 模式，单一路径避免部署歧义）：登录/续期走 OAuth2 `/token`（password/refresh grant），JWKS 本地 RS256 验签（`AUTH_JWT_ISSUER`/`AUTH_JWT_AUDIENCE` 配期望 claims，锁死 alg、强制 `typ=access`）；注册经本服务一步转发 auth-server 的 `/register`（一步建正式用户、无邮箱验证码，带 X-Forwarded-For 供限流）；强制改密用户（pwd_cr）拒绝进入（403 PASSWORD_CHANGE_REQUIRED，modeling 无改密入口）。前端 401 单飞静默续期，对齐 consumer ADR D6 方案 a。评测闭环的 consumer 业务 API（`CONSUMER_API_URL`）为另一服务，须信任同一 auth-server 才能接受透传的专家 token |
| 4 | 与 consumer 闭环 | **全自动 API 直连**（专家 token 透传）：配置时 `import-zip` → `PUT ontology data-source`，评测时 `import-zip` → 复用 ontology data-source → 逐用例 `runs/sync`；评测跑在**建模影子工作区**（普通 workspace，靠 X-Workspace-Id 隔离，专用命名 + 用后可清理）；导出 zip 仅作兜底 |
| 5 | Excel 语义 | 一个 xlsx=一个库；**sheet=表**（sheet 名=表名），首行=字段名，次行可选注释，余行=数据。三段用途：① 入项目数据字典（驱动访谈与映射）② 推导 objects 字段与 `object-data-mappings.json`；每个 object 必须绑定真实 Sheet、字段必须存在于该 Sheet，同一 Sheet 可映射多个 object，`objects/index.md` 的 `来源Sheet` 是 Excel 图导入选表依据 ③ 在数据源配置步骤全量、异步写入以 Scene 命名的 MySQL 数据库；建库和导入前确认脱敏的 `EVAL_DB_URL`，可为本次项目操作临时指定其他 URL，但不持久化、不修改环境变量；可幂等创建并回填固定名称 `mv_shadow_<project_id>` |
| 6 | 访谈交互 | 建模目标先以自由文本确认；业务流程访谈按核心信息与流程细节两段进行，答案和候选都绑定任务图中的当前任务与工件 revision，刷新后可继续 |
| 7 | 流程事实源 | 多流程只保存一份结构化 `diagrams/processes.json`；画布可增删改流程、节点和连线，Mermaid 文件只由结构化文档单向派生，人工确认后才允许知识生成 |
| 8 | 评测与优化 | **LLM Judge** 判分（通过/部分/失败+理由，专家可改判）→ 优化器输出**结构化建议**（精确到文件段落、预期修复的用例）→ 逐条采纳后 agent 改包、**版本递增、自动重评**；使用 Modeling 内置轻量评测 |
| 9 | 持久化 | **SQLite** 保存项目、任务图、交互、索引和评测状态；`isomorphic-git` 工件仓库保存知识包文件及不可变 revision。人工草稿、候选、检查、评测、图导入和发布都绑定 revision；两者随同一持久卷备份 |
| 10 | LLM 配置 | admin 平台默认是统一运行时兜底，`.env` 仅提供可选部署兜底；认证用户可分别覆盖主模型与后台任务模型的 Base URL、模型、API Key、输出上限和上下文窗口。后台任务模型为空时使用平台或个人主模型，modeling-agent 通过受服务令牌保护的内部接口按发起用户读取同一配置。OpenAI-compatible 直喂运行时，**不**从 consumer 取密钥 |
| 11 | 工作流 | 用户可见流程固定为 9 步；建模阶段只有一份可编辑、可恢复的 `AiConstructionTaskGraph`，允许在封闭任务类型内新增、拆分、合并、重排、跳过和重做。场景、数据源、评测、诊断、优化和发布状态直接来自各自事实记录 |
| 12 | 前端 | 项目只按 `kind` 路由：`modeling` 进入 `AiConstructionWorkbench`，导入知识包的 `package` 项目进入 `PackageWorkspace`。建模工作台内嵌同一个知识包工作区，不保留旧 Workbench 或版本化 UI 实现 |

## 2. 系统架构

```mermaid
flowchart LR
  Expert["领域专家"] --> FE["建模前端（React · .mwr 设计体系）"]
  FE --> BE["modeling 后端（Fastify + pi-agent-core）"]
  BE --> WF["AI 辅助构建任务图 + 可恢复交互状态"]
  WF --> PI["pi agent（LLM 统一层 · 工具调用 · 状态管理）"]
  BE --> DB[("SQLite 项目库")]
  BE --> FS[("文件卷：文档 / 解析产物 / 包版本")]
  BE --> EVALDB[("评测 MySQL（每项目一个 Scene 数据库）")]
  BE -- "AuthClient(introspect)" --> Consumer["consumer API"]
  BE -- "import-zip / data-source / runs·sync" --> Consumer
  Consumer --> Shadow["影子工作区"]
  Shadow --> EVALDB
```

- **AI 辅助构建任务图**：持久化里程碑、任务依赖、确认记录和 revision；任务状态变化使用乐观并发，重做上游任务会明确标记受影响任务过时。
- **pi 集成层**：每个长任务对应可恢复的 authoring run，待专家输入时保存受控 interaction；应答通过专用端点幂等续跑，并由项目事件流更新页面。
- **consumer 客户端**：封装 import-zip（409 覆盖确认）、data-source、runs/sync（含 429 限流退避）、影子工作区建删。

## 3. 唯一工作流与建模任务图

```
1 项目创建        项目类型、领域、目标用户、资料级别和交付要求
2 建模资料上传    md/txt/pdf/word/excel；同一来源保存不可变修订并传播影响
3 AI 辅助构建     目标确认 → 资料分析 → 流程访谈/绘制/确认 → 知识提取 → 脚本决策/生成 → 完成确认
4 场景推演        从当前 Git 工件生成场景并逐项人工确认
5 数据源绑定      配置查询提供者、导入 Excel 或将数据导入 Neo4j
6 评测            当前 revision → 隔离工作区 → runs/sync → LLM Judge
7 失败诊断        基于执行轨迹和当前 revision 形成可复核诊断
8 优化更新        建议 → 候选 revision → 专家接受/退回/拒绝 → 重新评测
9 发布交付        当前检查、警告复核、评测证据和候选状态全部通过后发布
```

AI 辅助构建中的动态任务限定在受控类型内：

| 类型 | 触发场景 | 产出 |
| --- | --- | --- |
| `追问访谈` | 某决策点置信度不足 | 追加一批题 |
| `深读文档` | 初析发现关键段落需精读 | 结构化提取结果 |
| `数据探查` | 需验证唯一键/口径/分布 | 对评测库跑聚合 SQL 的结论 |
| `冲突消解` | 文档与数据矛盾 | 出题请专家裁决 |

## 4. 数据模型（SQLite 主表）

`projects`（owner/类型/领域背景）· `documents`（不可变资料修订+解析状态）· `data_dict`（表/字段/注释/样例统计）· `ai_construction_graphs` + `ai_construction_tasks`（唯一建模任务图及节点状态）· `authoring_runs` + `authoring_interactions`（可恢复执行及专家交互）· `artifact_revisions` + `package_versions`（Git revision 与语义版本索引）· `package_drafts`（绑定基线 revision 的人工草稿）· `testcases` · `eval_rounds` + `eval_results`（绑定 revision 的评测证据）· `failure_diagnoses` + `optimization_suggestions`（诊断、建议和候选关联）。

文件布局：上传资料保存在 `DATA_DIR/projects/<id>/`，每个项目的知识包 Git 仓库保存在 `DATA_DIR/artifacts/<id>/`；数据库和工件目录必须一起备份。

## 5. 评测闭环协议

1. 生成包 zip → `POST /agentic/ontologies/import-zip`（影子工作区头；已存在则 `action=overwrite_active` 或版本参数递增）；
2. `PUT /agentic/ontologies/<id>/data-source`（以 `direct_provider` 覆盖影子知识包本轮评测数据源）；
3. 逐用例 `POST /agentic/runs/sync`（`session_id` 每用例独立；429/503 退避重试）；
4. Judge（轻量模型可选）：`(question, ground_truth, final_answer) → {verdict, reason}`；
5. 失败诊断复用本轮评测工作空间的数据源进行只读核对；上传资料中的表格只作为 schema/样例证据，不能用样例行数推断运行时记录是否存在；
6. 优化器（pi agent run）：输入失败用例 + 包全文 + judge 理由 → 建议清单；采纳项由 agent 应用 → `packages/v<N+1>` → 回到 1。

## 6. 前端信息架构

- **项目列表**：卡片墙展示名称、领域以及任务图的当前任务和真实进度；项目查询不创建或推进任务图。
- **建模工作台**：左侧 9 步时间线，中间为建模助手与待办，右侧嵌入准备面板或唯一知识包工作区；任务图卡片展示依赖、状态、ETA 和受控调整动作。
- **知识包工作区**：目录树、Markdown/源码编辑、流程图画布、场景、数据源、评测优化和发布共用同一当前 revision。
- **评测闭环**：用例与轮次 → 失败诊断 → 优化建议 → 候选预览与专家决策 → 当前 revision 重新评测 → RC/正式发布。

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
EVAL_DB_URL=mysql://user:password@mysql:3306  # NL2SQL 查询与 modeling 建库/导入共用；项目以 Scene 区分数据库
```

compose：`modeling`（Node，5100，挂 `modeling-data` 卷）；前端 dev 6700，生产由 modeling 服务托管静态产物。NL2SQL 服务与 modeling 使用同一 `EVAL_DB_URL`，各项目以 Scene 连接不同的 MySQL 数据库。

评测库导入由独立 Worker Thread 执行 Excel 解析和数据库写入，并限制单实例并发，避免阻塞 HTTP 事件循环和大文件占用失控。导入不设置 2000 行截断；用户必须显式确认同名表替换和本次使用的数据库 URL。服务端只返回隐藏密码后的 `EVAL_DB_URL` 预览；自定义 URL 仅随本次请求传入 Worker，不持久化。MySQL 先完整写入临时表，再用单条原子 `RENAME TABLE` 切换，失败时保留原表。运行中的导入会阻止启动评测。

## 8. 里程碑

| 阶段 | 内容 | 验收 |
| --- | --- | --- |
| **M1 ✅** | 服务骨架 + AuthClient + 项目/上传/解析（含 excel→数据字典）+ workflow 引擎与恢复 + **前端骨架** | 传入样例资料可走到节点 3 —— **已达成**。后端：`scripts/smoke-nodes.ts` 通过（节点 1→3 端到端、Excel 数据字典、快照序列化恢复），HTTP 服务启动、认证门控、build/typecheck 通过。前端：登录 → 项目列表 → 建模工作台三栏（时间线 / 上传解析 / 数据字典）端到端联调通过（puppeteer 截图验证），build 通过 |
| **M2 ✅** | 已确认业务流程 + 包生成（spec→标准包渲染）+ 导出 zip + **统一建模工作台** | 产出的包可被 consumer 成功导入 —— **已达成**。后端：`scripts/smoke-m2.ts` 显式构造 5 条业务流程，验证分阶段包生成、流程 5/5 覆盖、标准包渲染与 zip 解压。前端由统一建模工作台完成流程确认、知识生成、包评审与导出，不再依赖旧访谈和胜任力第二套运行时。新增 `MODELING_LLM_BASE_URL=faux` 演示模式（脚本化假建模者，无 key 可验证生成链）。遗留：动态节点 deepread/probe/conflict、包评审编辑 |
| **M3 ✅** | 评测闭环（评测工作空间/评测库/runs·sync/Judge/结果页） | 端到端出分 —— **已达成**（stub consumer 层面）。后端：`scripts/smoke-m3.ts` 通过（工作空间创建、import-zip 冲突处理、本体级评测数据源配置与复用、runs/sync 退避、faux Judge 判分、轮次统计、人工改判）。NL2SQL 数据源保存时测试服务及 Scene；建库和 Excel 后台导入默认通过共享 `EVAL_DB_URL` 操作 Scene 数据库。前端支持从知识包业务流程预填并维护真实用例、查看执行进度与轨迹、复核判分和轮次历史。遗留：真实 consumer + example 13 条用例端到端验收（依赖真实环境） |
| **M4 ✅** | 优化器 + 候选修订 + 版本生命周期 + 导出交付 | 两轮优化后通过率可见提升 —— **已达成**。`scripts/smoke-m4.ts` 以当前 Git revision 启动评测，完成失败诊断、建议生成、候选创建与接受，再将当前修订固化为 `1.1.0-rc.1`、`1.1.0-rc.2`；三轮结果为 1/3 → 2/3 → 3/3。建议、诊断、评测和过期判断均以 revision 为准，版本页只把评测关联到精确的正式 revision，未版本化当前修订单独展示。遗留：真实 consumer + example 样例端到端验收（依赖真实环境） |

## 8.5 真实场景验收记录

| 场景 | 输入 | 结果 | 迭代 |
| --- | --- | --- | --- |
| 示例工程变更（CHANGE_ORDER 变更成本/切换分析，计算推理型） | 16 张核心表 xlsx + 领域规则文档 ×2 | **13 例满分**：基线 3 → 10 → 11 → 13（激活修复揭示盲改冲突 → 回退标注清障 → 口径下沉查询条件） | 8 版本 / 12 轮 |

## 9. 风险与开放问题

- **pi API 细节**：✅ 已 spike 验证（见 [spike-notes.md](spike-notes.md)）——自定义工具、状态序列化/恢复、`terminate` 挂起模式、OpenAI-compatible 接入四项全部成立；「waiting_user 序列化对象=`agent.state.messages` 数组」得到确认。✅ 真实端点冒烟已完成：`npm run smoke:real`（`.env` 配真实 OpenAI-compatible 端点）覆盖初析、已确认流程驱动的分阶段包生成、流程覆盖校验、Judge 正负例和优化器建议/修订，并按统一 RC 生命周期推进验证版本；连通性快检 `npm run check:llm`；
- Node 侧文档解析选型：xlsx（SheetJS）/ mammoth（docx）/ pdf-parse，扫描版 PDF 首版不支持（明确报「不可解析」）；
- 长会话 token 过期：AuthClient 需处理评测长跑中的 401 → 前端引导重登后续跑；
- consumer 普通用户并发 run=1 的限流：评测串行执行 + 退避即可，无需改 consumer；
- 影子工作区清理策略：项目删除时级联清理其影子工作区与 Scene 数据库。
