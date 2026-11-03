# onto-modeling 后端改造方案

| 项 | 内容 |
|---|---|
| 日期 | 2026-11-01 |
| 依据代码 | knowledge-scaffold `develop` 44644e0a（2026-10-31），目录 `modeling/onto-modeling`。当天重新 fetch，develop 没有新提交 |
| 依据设计 | 《onto-modeling 插件化架构详细设计》v1.1（2026-11-01） |
| 方法 | 只读核对，按设计文档的模块分 5 块逐文件对照。影响最大的结论人工复核过，表中标"已复核" |

**阅读说明**

- 行号都取自 origin/develop 44644e0a。路径省略 `modeling/onto-modeling/`，`app/` 指 `backend/app/`，`scr/` 指 `assets/workspace-toolkit/scripts/`。
- 工作量：S 一两天，M 一周左右，L 两周以上，都是单人粗估。
- 阶段沿用设计文档第 13 章的 P0～P4；需要调整的地方在第 4 章说明。

---

## 0 先看这里

**总体判断：** 设计文档的方向和代码对得上。大部分改造是"把散落的逻辑收口、把写死的东西改成声明"，不需要推倒重来。但有 7 个问题比文档列的止血项更急，建议放进 P0 一起做：

| # | 问题 | 后果 | 位置 |
|---|---|---|---|
| 1 | 项目元数据 `.onto-web.json` 和工具状态 `.ontology-state/` 都进了 git（已复核） | 回退版本会把所有者、成员、项目名一起回滚；回到所有者为空的版本时，项目对所有人可写。以后合入变更集，这两处也必然冲突 | `templates/task-gitignore.template` 没排除；`app/services/workspace.py:205` 建项目时 `git add -A`；`revert_to:1794` 用 `checkout <commit> -- .` |
| 2 | 发布读的是工作区当前文件，不打标签；版本号是 `0.1.{版本数}`，而版本列表默认最多 50 条（已复核） | 未提交的改动会被发出去；50 次提交后版本号停在 0.1.50，下游按版本去重会拿到旧包 | `workspace.py:946`、`:1897`、`publish_package:1688` |
| 3 | 配置指纹一变就断开引擎，不管这一轮是否还在跑（已复核） | 正在跑的一轮被中途切断，也不留中断标记。文档说的"跑完旧版本"并不成立 | `app/services/agent_sessions.py:548-552` |
| 4 | 部署没有定义任何网络（已复核） | 证据源层"主容器不接 biz-egress 就没有路由"的前提不成立：默认 bridge 会经宿主出网。沙箱网关现在也能出网 | `deploy/docker-compose.release.yml` |
| 5 | agent 以 root 运行，整个数据目录都能读 | 能读到明文的管理员模型 Key、个人 Key 的主密钥（能解密别人的 Key）、MCP token 库，以后还有票据私钥；还能改工作区里的所有者 | `Dockerfile`、`model_settings.py:84-92`、`personal_model_settings.py:41-91` |
| 6 | 后端用自己的完整环境变量，运行工作区里的 `query.py`、`seed_example.py`（已复核） | 拆包以后，租户包只要替换这两个脚本，就能在后端进程的身份下执行任意代码 | `app/routers/projects.py:1385`、`workspace.py:1407` |
| 7 | 容器里后端是 1 号进程，`state_helper` 用进程号判断锁 | 加上文档要求的 `init: true` 后判断会翻转：锁从"每次都判过期"变成"一直有效"，会出现跨会话冲突。必须先给 state_helper 打补丁，再加 init | `scr/state_helper.py:118` |

**落地顺序要调的地方**（详见第 4 章）：

- "助手运行中写入范围只读"排在 P2，但它依赖的写入范围守卫（㊹）排在 P3，要一起放到 P2。
- 数据目录的密钥移出 agent 可读范围，建议从 P4 提到 P1（问题 5）。
- 删评测之前，先把凭据存储（0600 + `has_*`）和"后台任务 + 启动时标中断"这两套写法抽成公共件，后面证据源层要复用。

---

## 1 设计文档要更正的地方

核对中发现文档对现状的描述有错或不全，下面这些应在 v1.2 里改掉。

| 章节 | 文档写的 | 代码实际 | 依据 |
|---|---|---|---|
| 3.5 | 后端 import 的 4 个工具箱脚本要重启才生效 | 实际 7 个，还有 `research_review`、`solver_evaluator_contract`、`jiuwenbox_client` | `workspace.py:2076`、`solver_contract.py:140`、`sandbox_status.py:27` |
| 3.5 | 上传资产包后，在线会话仍用旧的 CLAUDE.md | CLAUDE.md 来自 `assets/workspace-rules.md`，不在上传范围里；上传的资产在镜像层，重建容器就丢 | `Dockerfile:38,55` |
| 3.5、4.2 | 正在进行的一轮跑完旧版本 | 会被切断（第 0 章问题 3） | `agent_sessions.py:548` |
| 4.1 | onto_mcp 4 个接口 | 管理 4 个 + 传输 4 个 | `routers/onto_mcp.py:38-70,187-223` |
| 4.1 | 求解契约是独立的文件契约 | 和知识包是同一个包，带求解契约时 schema 升到 0.2 | `workspace.py:1075` |
| 4.2 | 指纹在 `_deliver` 里算 | 指纹在引擎建连时算，`_deliver` 只调用 `config_stale()` | `claude.py:138`、`jiuwen.py:122` |
| 4.3 | 写入范围内的条目"这一轮结束后恢复" | 陪伴建模下一个变更集跨好几轮，只读要持续到确认生效或撤回，否则合入必然冲突 | 设计推演 |
| 4.4 | 维基链接正则 7 处以上 | 约 20 处、11 个文件，分两族：一族遇 `#` 截断，一族把 `#` 当名字的一部分；类型和目录表至少 8 份，章节解析 3 份 | 见 2.2 |
| 4.9 | `.onto-web.json`、`.ontology-state/` 不进 git | 都进 git | 第 0 章问题 1 |
| 4.10.1 | 26 个 `ONTO_WEB_*` 环境变量 | 后端实际读 38 个，散在 6 个文件；其中 7 个随评测删除 | `config.py` 等 |
| 4.10.1 | 角色有 admin、owner、editor、viewer | JWT 只有 admin / user，没有运维；admin 现在能读写所有项目内容，还能代所有者发 MCP token | `auth-server users.py:328`、`onto_mcp/tokens.py:59` |
| 4.10.1 | 个人 Key、评测数据源密码 0600 存放 | 个人 Key 是 AES-GCM 加密，但主密钥同在数据目录；真正明文 0600 的是管理员模型 Key 和评测数据源 | `personal_model_settings.py:41-91`、`model_settings.py:84-92` |
| 4.10.2 | 现有基础有指标 | `metrics.py` 是从报告里抽覆盖率，不是可观测指标；全后端只有 4 个模块用 logging | `metrics.py:1-33` |
| 5.3、5.4 | 包归属 | `report_reconcile` 只被 core 用，归 core；`check_eval_data` 是核心的表登记，被 5 个核心脚本 import，不能删；`check_eval_generalization` 被反馈演进命令调用，随反馈演进包保留；deep-research 是 core 资料分析必读的方法论；`check_diagrams`、`script_verify` 留在 core | `ontology_checker.py:32` 等 |
| 5.8.2、5.8.9 | 主容器不接 biz-egress 就没有路由 | 不成立（第 0 章问题 4） | compose |
| 5.8.5 | `file:resource/excel/客户清单.xlsx#…` | 这个例子违反了规则 2 的转义要求 | — |
| 5.9.4 | 报告格式说明就是登记 | `report_items` 是两个正则启发式解析器，没有格式定义、没有索引、只读 | `report_items.py:11-87` |
| 6.1 | 阶段定义散在 5 处 | 还有 `post_commit_hook.sh:59-106` 和 `App.jsx:461-533`（阶段总数默认写成 9）；"最新报告"也有按文件名和按修改时间两种取法；`knowledge_helper load` 是 16 处 | — |
| 6.1、12#4 | Claude 下会话开始提示、提交后提示可能失效 | 会话开始和用户发消息时的 hook 输出的是 additionalContext，Claude 下生效；提交后提示是纯文本，模型看不到是格式问题。真正的缺口是会话常驻期间阶段变了，提示不刷新 | `session_start_hook.sh:55` |
| 8.3 | 停机时先写续接点；compose 配了健康检查；历史自愈是启动动作 | 续接点早已随消息流落盘，停机时正在跑的一轮不会标中断；健康检查来自 Dockerfile；历史自愈是读取时才修 | `agent_sessions.py:764,118`、`Dockerfile:67` |
| 8.4 | Jiuwen 下锁的 owner 一直活着 | 容器里后端是 1 号进程被排除，owner 记成瞬时 shell，Jiuwen 下每次都判过期（推断） | `state_helper.py:118` |
| 11 | 评测门禁在 `package_mapping` 里 | 门禁在 `projects.py:1079-1100`；`package_mapping` 里属于评测的是评测上传表来源和图模式，前者同时给发布门禁提供数据 | — |
| 11 | 三份契约 | 现在是 onto 主动推包给 modeling。移出后要补三个接口：发布列表与下载、验证回执入口、反馈入口；`ontology-evolution` 现在读 `.ontology-state/eval.json`，移出后没有输入 | — |
| ⑱ | 发布读主线标签 | 现在不读标签（第 0 章问题 2），这是目标，不是现状 | — |

---

## 2 改造清单

### 2.1 评测移出、拆分 workspace.py、发布、单人所有权

**评测删除顺序**（箭头指向被 import 的模块）：

```
main → modeling_eval → eval_import → eval_data_files
                     → eval_data_sources → eval_data_files
main → solver_validation_run → modeling_eval, eval_data_sources, solver_validation（保留）
main, admin → modeling_cleanup → modeling_mcp ← modeling_package_sync
核心反向依赖评测：workspace.py:284 → modeling_eval；package_mapping.py:240 → eval_data_*
```

1. 切断两条反向依赖，把评测上传的、已被对象引用的数据表迁到 `resource/excel` 并标为 schema；
2. 上线验证回执入口；
3. 删路由、启动 / 关闭任务、admin 路由；
4. 按序删模块：package_sync → solver_validation_run → modeling_eval → eval_import、eval_sets → eval_data_sources → eval_data_files → modeling_cleanup → modeling_mcp；
5. 删图模式和 `export_package_zip` 里只供评测用的参数（图模式去留待定，见第 6 章）；
6. 清理配置、脚本、前端、文档。

| # | 改什么 | 现在的代码 | 怎么改 | 量 | 阶段 |
|---|---|---|---|---|---|
| A1 | 评测上传表 | `package_mapping:238-272`、`workspace:1683` | 删评测来源；被引用的表一次性迁到 `resource/excel` | M | P0 |
| A2 | 验证回执入口（㉕） | `solver_validation:62-98` 只被 run 调用 | 新增 `POST /api/v1/projects/{id}/solver/receipts`，服务令牌鉴权、校验修订号；保留 gate、latest。必须先于 A3，否则求解包发布不了 | M | P0 |
| A3 | 评测路由与任务 | `projects:735-1163`（20 个路由）、`:486-535`、`:324-363`；`main:24-44`；`admin:369-378` | 删除 | M | P0 |
| A4 | 评测模块与配置 | 9 个模块；`config:42-58` | 按上面的顺序删；评测数据源目录（含密码）一起可靠删除。删之前把凭据存储和后台任务模式抽成公共件 | M | P0 |
| A5 | 评测阶段 | `workspace:226-227,277-290`；前端 `LeftPane` | 前后端同时删，11 个阶段变 10 个 | S | P0 |
| A6 | 工具箱脚本 | `check_eval_data` 等 | `check_eval_data` 拆出表登记（`table_profile`），旧名留一版转发；`check_eval_generalization` 迁到反馈演进包；删 `mock_to_eval_seed`。注意资料分析验收回执的指纹含评测数据，会整体失效一次 | M | P0 |
| A7 | 三份契约 | 现在 onto 主动推包 | 补发布列表与下载（服务鉴权）、回执入口（A2）、反馈入口；反馈演进改读反馈入口 | M | P0 |
| A8 | 发布适配器 | `workspace:856-1243,1420-1460,1640-1717,1995-2056` | 抽成适配器：确定提交 → `git archive` 到临时目录 → 门禁 → 渲染 → 打 `release/<版本>` 标签 → 写发布记录；用逐字节比对做回归 | L | P0 |
| A9 | 版本号 | `:946` | 按标签单调递增；包清单写入来源提交 | S | P0 |
| A10 | 求解契约搬迁 | `:1461-1638`、`solver_contract`、`solver_validation` | 搬到 `services/solver/`；保存的写盘和提交交给写入路径 | M | P1 |
| A11 | 单人所有权 | `workspace:323-350,416-422`；`projects:77-121,403-420`；`tokens.py:59,95` | 新增所有权模块（`require_owner` / `require_meta_view`）；删成员、editor、viewer；管理员不再读内容、不再代发 token | M | P0 |
| A12 | 无主项目 | `:331` 无主项目全员可写 | 新增 `PUT /admin/projects/{id}/owner`，启动时报告无主项目；迁移时删 `members` | S | P0 |
| A13 | gitignore | 模板 | 加 `.onto-web.json`、`.ontology-state/`、`.onto/run/`，存量项目 `git rm --cached`；P1 再把所有者移到数据目录的注册表 | S | P0 |
| A14 | `/api/v1` | 6 个 router 写死 `/api` | 前缀改 `/api/v1`；ASGI 中间件把旧路径改写到 v1 并加 Deprecation 头，必须放在鉴权中间件之前并同步免鉴权前缀；`/api/health` 保留 | M | P0 |
| A15 | 拆 `projects.py` | 删完评测后约 50 个路由 | 拆成 projects、files、resources、studio、knowledge、versions、project_settings、publish、solver | M | P0 末 |
| A16 | 共享契约库 | 加载器 3 处：`toolkit:21`、`solver_contract:127`、`workspace:2058` | 合成一个，按锁定版本加载（见 D5） | M | P1 |

**`workspace.py` 拆分归属：**

- **工作区核心：** 锁、原子写、建项目、元数据读写、文件和资料、改名、归档、克隆、删除与清扫、迁移包导入导出、`seed_example`、版本和标签、回收站。`write_file`、`revert_to`、`delete_*` 交给写入路径（2.2）。
- **项目所有权：** `role_of`、`visible_to`、`can_access`、`can_write`、`WRITE_ROLES`、`set_members`，以及 `projects.py` 的 `ensure_access`、`ensure_owner`。
- **发布适配器：** `export_package_zip`、`package_binding_check`、`set_object_data_source`、`publish_package`、`import_package_zip`、`_ReproducibleZip` 等。
- **求解契约：** `solver_check`、`solver_contract_view`、`save_solver_contract`、`_ensure_solver_publishable` 等。
- 拆之前先解循环依赖：`package_archive.py:20-26` 在顶层 import 了 `workspace` 的 `META_FILE`、`WorkspaceError`、`_strip_frontmatter`。

**发布适配器 SPI：**

```python
class PublishAdapter(Protocol):
    id: str
    formats: tuple[str, ...]
    def check(self, src: ReleaseSource) -> list[Finding]: ...
    def render(self, src: ReleaseSource, out: Path) -> Manifest: ...
    def materialize(self, archive: Path, dest: Path) -> dict: ...   # 反向导入
# ReleaseSource：project_id、commit、只读的树、meta、version、回执目录
```

### 2.2 写入路径、变更集、运行中只读、元模型、插件产物

**维基链接正则的现状（统一解析的依据）：**

- 遇 `#` 截断：`workspace.py:2128`、`studio.py:20`、`script_argument_shape.py:36`；`onto_mcp/documents.py:12` 要求闭合 `]]`，遇到 `[[A#x]]` 整条丢掉。
- 识别锚点和别名：`package_mapping.py:595`、`field_semantics.py:476`。
- 把 `#` 当名字的一部分：`ontology_checker.py:87/1465/1662`、`operation_relations.py:27`；`impact_analysis.py:76/83`、`query.py:69` 连别名都不剥；`ontology_checker.py:1833/1839/1978` 只排除 `/`。
- 另有两份 `_relation_node`（`ontology_checker.py:925`、`package_mapping.py:595`），`studio.py:472` 的反链用子串前缀匹配，前端 `CenterPane.jsx:208` 还有一份。
- 内部测试环境上的断边就出在这里：导入的 `1#货架`，检查器认，Studio 截断。

| # | 改什么 | 现在的代码 | 怎么改 | 量 | 阶段 |
|---|---|---|---|---|---|
| C1 | 统一变更事务 | `write_file:1723`（非原子写 `:1755`；`commit -m` 不带路径 `:1769`，会把助手暂存的文件一起提交）；`revert_to:1786`、`delete_file:1828`、`tag_version:1936` 不拿锁；MCP `persist:137` | 新增 ChangeTx：拿锁 → 记 HEAD → 写意图 → 用临时 index 生成提交（带来源 trailer）→ `update-ref` 比对旧值后更新 → 原子落盘 → 发事件。四处都改为调用它；回退改为整树恢复，能删掉之后新增的文件 | M | P1 |
| C2 | 变更事件 | 只有会话内的变更卡 | 新增项目级 `workspace.changed`，SSE、流程运行时、产物索引都订阅 | S | P1 |
| C3 | 元模型单一定义 | 类型目录表 8 份、章节解析 3 份 | 新增 `scr/onto_metamodel.py`（只用标准库，后端经契约库加载）：实体类型、目录、只读别名、是否递归、章节、key 规则、文件名编码；加 `GET /api/v1/metamodel` 给前端 | M | P1 |
| C4 | 链接解析 | 约 20 处 | 统一为 `parse_links` → Link(target, anchor, alias, span)，`#` 一律当锚点；`resolve` 先按全名匹配文件，兼容存量含 `#` 的文件名。检查结果会变，要回归 | M | P1 |
| C5 | 文件名编码 | `report_reconcile.py:131` 不转义 `#&` | 编码搬进元模型；导入遇到非法 key 进待处理 | S | P1 |
| C6 | 索引副作用 | `_refresh_indexes:502`；GET /files 经 `:537` 无锁提交 | `index.md` 当派生物在 ChangeTx 里刷新；GET 去掉写副作用 | S | P1 |
| C7 | 变更集副本 | agent 工作目录就是项目根（`sessions.py:94` → `agent_sessions.py:542`） | `git worktree` 建副本，路径每个项目固定（`DATA_DIR/changesets/<pid>/assistant`），否则 Claude 的续接记录会丢；`.claude` 单独挂；`resource/` 用 sparse-checkout 排除后挂只读链接 | L | P2 |
| C8 | 确认生效 / 撤回 | `candidate_revision` 的 check:291、accept:356 | 合入：`merge-tree --write-tree` → 实体冲突检测（C9）→ 在合成树上跑门禁（复用 `_minus:179` 只看新增错误）→ ChangeTx 写 squash 提交。撤回：删分支、重置副本。自主构建撤回单段用反向提交 | L | P2（自主构建部分 P3） |
| C9 | 实体级差异 | `documents.public`、`integrity:41` | 新增 entity_diff：把树解析成 {实体: 章节、字段、关系}；主线和变更集改到同一实体即冲突 | M | P2 |
| C10 | 运行中只读 | Studio 租约 `studio.py:606-707`、`projects.py:1255-1307`；MCP 整项目忙 `onto_mcp/workspace.py:68`；网页 PUT /file 不查租约 | 范围 = 声明范围 ∪ 副本里已改的实体 ∪ 决策卡预览列出的实体，持续到确认生效或撤回。ChangeTx 统一检查，命中返回 423；MCP 继续回 `PROJECT_EDITING_BUSY` 并附实体列表。删租约和整项目忙判断；加 `GET /scope` 和 SSE | M | P2 |
| C11 | agent 提交收口（⑩） | `pre_commit_check.sh` 是 Claude 的 PreToolUse hook | 助手在副本里照常提交（有隔离就不需要项目锁）；检查逻辑搬进内核门禁链，脚本只调本机接口，出错即拒；trailer 写在合入提交上 | M | P2 |
| C12 | 产物变更卡 | `agent_sessions.py:545/963` 把人工提交也算进去 | 基点改为变更集基点，在副本里算差异 | S | P2 |
| C13 | 插件产物登记（5.9.4） | `report_items.py`、`projects.py:1350` | 新增登记表、解析器、索引、引用四部分；5 种格式加一个吸收 `report_items` 的 md-report；按提交建 SQLite + FTS5 索引；过期 = 被引实体指纹变了；保存走 ChangeTx 加 jsonschema 校验；REST 和 MCP `artifact.list` / `artifact.get` | L | P2 |
| C14 | 版本编号 | `versions()` 按提交数编号（`:1897`） | 主线用 `--first-parent`，只计合入提交 | S | P2 |
| C15 | Studio 复核草稿 | 写在主线的 `.ontology-state/studio-review`（`studio.py:764`） | 移进副本，否则助手看不到 | S | P2 |

### 2.3 插件运行时、流程运行时、安全守卫

**全局单版本迁到按项目锁定，分 5 步：**

1. 首次启动时，把当前生效的资产导入包仓库，成为 core@0.x 和 contracts；
2. 给每个存量项目写 `packs.lock`，作为一次系统提交；
3. 装配，原子切换链接；
4. 旧的上传接口改成"发布 core 新版本到 stable 通道"，项目在下一条消息前跟随通道升级，效果同现在，但能按项目撤回；
5. 稳定后取消自动跟随，改成在项目设置里手动升级。

| # | 改什么 | 现在的代码 | 怎么改 | 量 | 阶段 |
|---|---|---|---|---|---|
| D1 | 包仓库与锁文件 | `assets_admin` 整个模块（先移走旧目录再移入新目录 `:69-72`、`:109-111`；上传只校验 commands / agents，不带 settings.json 也能过，一次上传就能删光守卫） | 新增 `packs/repository.py`：静态检查（清单、名字唯一、不带前端代码、无符号链接）后存到 `packs/<名>/<版本>` 附 checksums；`.onto/packs.lock` 进 git；`.onto` 加进 `WRITE_FORBIDDEN_TOP`（`workspace.py:1721`） | M | P1 |
| D2 | 装配器 | `_mount_assets:1956` 挂绝对软链，只在建项目和导入时调用 | 新增 `assembler.py`：把内核守卫和已启用的包**复制**进 `assembled/<哈希>`（7 个脚本按 `__file__` 找同目录模块，逐文件软链会解析错），合并 settings.json、CLAUDE.md 片段、flow.yaml、MANIFEST.json；临时软链 + `os.replace` 切换；根目录 CLAUDE.md、README.md 改成相对链接指向 `.claude/` 内 | M | P1 |
| D3 | 指纹与生效 | `claude.py:138`、`jiuwen.py:122`、`_deliver:548` | 指纹计入装配哈希；会话忙时推迟到空闲再切换（同 B1） | S | P1 |
| D4 | 启动核对 | `main._lifespan` | 逐个项目核对链接目标和哈希，不对就重新装配；处理旧的 `/opt/onto-web/…` 绝对链接 | S | P1 |
| D5 | 契约库 | `toolkit.py` 等 4 个加载点、7 个 import 时固定的脚本 | contracts 库包（5 个契约脚本 + `research_review` + `jiuwenbox_client`），按锁定版本、用带版本号的模块名加载 | M | P1 |
| D6 | Jiuwen 读资产 | `jiuwen_assets.py:43-48`、`jiuwen.py:621/717-731` 读全局 ASSETS_DIR | 改读项目的 `.claude`；sandbox_root 换成装配目录；按装配哈希重新注册 | S | P1 |
| D7 | 拆包 | 整个 toolkit | P1 先原样迁成"core 包 + 内核守卫"，P2 再拆可选包（目录见第 3 章）。拆之前：拆出 `table_profile`；知识沉淀段落（`knowledge_helper` BUSINESS_KNOWLEDGE_SNIPPET `:69-75`）移到知识沉淀包；跨包 Read 改成"若已启用 X 能力"（`ontology.md:37/83/103`、`extract-object.md:91/131`、`extract-operation.md:91/131`、`research.md:188`、`validate.md:585-595`、`trace-batch.md:209`）；删 3 个没人引用的脚本 | L | P1 / P2 |
| D8 | 内核守卫注入（4.8 目标 1） | `settings.json:17-92` 在资产包里 | 守卫和 hooks 基线移到 `app/kernel_guards`，随镜像发布，装配时注入；包只能追加拒绝型 PreToolUse；hook 命令改用 `$CLAUDE_PROJECT_DIR` 或绝对路径并加 `|| exit 2`（现在用 `$(git rev-parse …)` 拼路径，脚本缺失时返回 127 被当作放行） | M | P1 |
| D9 | 出错即拒（目标 2） | `resource:11,22`、`toolset:33-45`、`lock:22-29`、`scope:24-27`、`pre_commit:29`；Jiuwen `jiuwen_assets:159-186`（未知工具名直接放行） | 公共 `_guard_common.py`：最外层捕获异常输出 deny，空输入或非法 JSON 也 deny；Jiuwen 超时、非零退出、输出不是 JSON 一律拒绝，会写文件的未知工具默认拒绝 | S | P1 |
| D10 | 补绕过写法（目标 3） | 4 个守卫各自的 `extract_bash_write_targets` | 共享解析：跟踪 `cd X &&`；`python/perl/node -e` 代码里出现受保护路径就拒；`find -delete/-exec`、`git checkout/restore/rm/clean` 带路径拒；路径含变量且出现受保护目录名拒；commit 正则补 `-C`、`-c` 和绝对路径调用；`git add -A && git commit` 的检查挪到提交接口（现在 PreToolUse 时检查的是旧暂存区，等于没查） | M | P1 |
| D11 | 写入范围守卫（㊹） | `ontology_scope_guard.sh:83-105` 已有反馈演进锁的特判 | 泛化：读 `.onto/run/active.json`（由 acquire 写入节点和 produces），只放行 produces；删评测特判。**从 P3 提到 P2**，C10 依赖它 | S | P2 |
| D12 | 流程定义 | 7 处阶段定义 | core 包提供 `nodes/*.yaml` 组装成 flow.yaml；`flow/assemble.py` 做锚点、环、输入输出冲突检查；P1 先保持现有行为，`_project_progress` 和前端 `LeftPane` 改读它；统一"质量检查完成"口径 | M | P1 |
| D13 | 节点状态 | 无 | `flow/state.py`：执行记录 + 变更集是否接受 + 产物指纹 → 8 种状态，建模节点按段算 | L | P2 |
| D14 | `/flow` 接口 | 前端 `buildSuggestions:108-200` | `GET /api/v1/projects/{id}/flow`，SSE `flow.updated` 复用会话事件流；前端只渲染返回的建议 | M | P1 / P2 |
| D15 | state_helper 兼容层（㊸） | `state_helper.py:269-384` | check-prereq、detect-stage 仍输出 8 个旧阶段名和 `ok/missing:`（命令模板按这些字面量分支）；新增 `suggest`（JSON，最多 3 条）；数据走文件（flow.yaml + 后端写的 flow-state.json），不调 HTTP | M | P2 |
| D16 | 下一步统一 | `ontology.md` 6 个建议块、19 个命令文件、两个提示 hook | 命令收尾调 `suggest`；约定文档加"最多 3 条"；两个 hook 退役，提示改由宿主注入（B4） | M | P2 |
| D17 | 扩展项注册表（⑥） | 左栏写死 | `registry.py` 从 MANIFEST.json 汇总；`GET /api/v1/projects/{id}/extensions` 返回 pages、subpages、entries、artifacts、views、gates、config、metrics 和生效 / 待生效哈希（显示"下一条消息生效"）；左栏 = 内核 4 项 + 声明的页面 ∩ 前端内置页面表，并校验 page_api | M | P2 |
| D18 | 节点包 | 无 | 上传静态检查（命令存在、produces 不碰受保护目录、不新增实体类型）；flow.lock 变更作为变更集 | M | P3 |
| D19 | 后端跑工作区脚本 | `projects.py:1385` 跑 `query.py`、`workspace.py:1407` 跑 `seed_example.py`，继承后端环境 | 只允许执行内核或契约库里校验过的脚本；子进程清空环境变量（第 0 章问题 6） | S | P0 |

### 2.4 会话宿主、执行记录、自主构建、崩溃恢复

| # | 改什么 | 现在的代码 | 怎么改 | 量 | 阶段 |
|---|---|---|---|---|---|
| B1 | 运行中不切断 | `agent_sessions.py:548` | 运行中只记"待重载"，等这一轮的 result（`:1011`）之后再切链接、重连 | S | P0 |
| B2 | 指纹模块 | 引擎各算各的 | 新增 `fingerprint.py` 由宿主统一算：模型、owner、装配哈希、flow.lock、MCP、权限模式（后两项只对 Claude）；变化时发事件，说明变了哪项 | S | P1 |
| B3 | owner 固定 | `:300`、`:518-539`（换人发消息 409） | owner 固定为项目所有者并计入指纹，删掉换人逻辑；Studio 复核、自主构建等后端内部发消息也带 owner，否则会掉回管理员 Key 并触发重连 | S | P2 |
| B4 | 两轮之间注入（㊶） | 靠 shell hook | 新增 `injection.py`：阶段与下一步、未结束的执行、业务知识信号（probe 移植成 Python）、自主构建指令。协议改为 `send(text, context)`：Jiuwen 拼进 query（`:781`，积压的消息也要带上）；Claude 用 SDK 的 UserPromptSubmit 回调或前缀。`_deliver:558` 和 `_connect:672` 两条发送路径都走；内容不变不重复注入 | M | P0 先做 Jiuwen，P2 两引擎都做 |
| B5 | 执行记录（8.4） | `state_helper.py:95-234` 进程号锁 | 新增 `exec_records.py`，存 `DATA_DIR/exec/<pid>/`：执行 ID、会话、引擎实例、节点与版本、输入指纹、阶段、状态、心跳、运行号和段；心跳由宿主在 `:991` 更新；state_helper 有会话 token 时走本机接口，否则退回文件锁 | M | P2（最小版提前到 P0，见第 0 章问题 7） |
| B6 | 中断与等待 | 无 | 一轮结束时执行没结束记为"等待"（陪伴建模一个命令常跨几轮问答，不能一律标中断）；打断、引擎结束、停机、启动扫描记为"中断"，发 `exec_interrupted`；续跑卡调 `execs/{id}/resume` 或 `abandon` | M | P2 |
| B7 | 会话身份注入 | `claude.py:151` | Claude 在 env 里加项目、会话和会话 token。Jiuwen 在进程内共用 `os.environ`，不能用环境变量，要靠 rail 给 bash 加前缀，或等 B11 的 worker | S | P2 |
| B8 | 启动恢复注册表 | `main.py:28-36` 只做两个 sweep 和 purge | 新增启动恢复注册表，收编：清理残留目录、扫描历史末尾、把未结束的执行和运行标为中断、清 `index.lock`（超龄才删并记日志）、报告未提交改动；耗时步骤放后台，接就绪探针 | M | P0 / P1 |
| B9 | 停机 | `:764` 串行回收，最长可超 30 秒宽限期 | 先给正在跑的轮写中断，再并发回收，总时长控制在 25 秒内 | S | P0 |
| B10 | init 进程 | compose、`Dockerfile:70` | 加 `init: true`，镜像装 tini 兜底；**先做 B5 最小版或给 state_helper 打补丁** | S | P0 |
| B11 | Jiuwen 独立 worker | `jiuwen.py:132` 全局 Runner | 新增 `jiuwen_worker.py` 和 `jiuwen_remote.py`，stdio JSON-lines 通信；单 worker 承载多会话，崩溃后重拉，并给其上所有会话发结束事件 | L | P2 |
| B12 | 自主构建运行器 | 无 | `autobuild/runner.py`，每段：新开上下文（`:776`，避免长上下文自动压缩丢掉台账）→ 注入段说明和台账摘要 → 带 owner 发送 → 等 result → 查执行记录，没做完也没挂起就说"继续"（限次数）→ 检查 → 合入 → 到安全点检查暂停、转陪伴等标志；台账写 `reports/autobuild/<运行号>/` | L | P3 |
| B13 | 自主构建与会话池 | `:271`、`:584`、`:917` | Session 加 run_id：运行中不驱逐、不回收；确认卡由策略立即作答并记台账；运行中用户发消息返回 409；限制同时运行的自主构建数 | M | P3 |
| B14 | 预算 | `:1016` 每次全量扫历史 | `budget.py`：token 用 counted_usage；费用 = token × 单价表（模型配置里新增，Jiuwen 现在没有费用）；时长用墙钟；80% 提醒，100% 停在安全点 | M | P3 |
| B15 | 引擎协议 | `base.py:92` 漏了 `kill()`，`agent_sessions.py:343` 在用 | 补 `kill`、`available`、`send(context)`、`model_fingerprint`；加 `add_observer()` 作为插件观察会话的入口 | S | P1 |
| B16 | 死代码 | `state_helper.py:323` | 删 `STAGE_NEXT_COMMAND`；B4 上线后退役两个 hook 的提示逻辑 | S | P1 / P2 |

### 2.5 资料与证据、治理平面、部署

| # | 改什么 | 现在的代码 | 怎么改 | 量 | 阶段 |
|---|---|---|---|---|---|
| E1 | `index.lock` | 无人处理；运行期也会残留（`_git` 60 秒超时杀 git `workspace.py:2140`，强制打断硬终止引擎 `agent_sessions.py:903`） | 启动时在 lifespan 最前面清 `.git` 下的各类 `.lock`；`_git` 遇锁且锁超过 120 秒、没有 git 进程时删锁重试一次 | S | P0 |
| E2 | 备份 | `admin.py:161-202`：只打包 workspaces，放在同一数据卷；先 sleep 再备份，重启间隔小于 24 小时就永远不备份 | 范围加上会话历史、Jiuwen 存档、Studio 草稿、MCP token 库、各配置、审计与错误、`/data/home/.claude/projects`（CLI transcript）；SQLite 用 backup API（开了 WAL，直接 tar 可能打出坏库）；启动时补跑过期备份；独立卷 `ONTO_WEB_BACKUP_DIR`；恢复演练。主密钥和密文分开放 | M | P0 |
| E3 | 角色收敛 | `workspace.py:323-422`；`projects.py:58,90-120,199-207,403-420`；`tokens.py:59`；`admin.py:27`、`settings.py:17` 各一份 `_require_admin` | 合入 A11；auth-server 加运维角色；运维页错误原文先脱敏（可能带项目内容） | M | P1（迁移在 P0 末） |
| E4 | 审计 | `audit.py`：鉴权关闭时不记，执行前就记，没有状态码和结果，不轮转 | 改成事件结构，带 trace_id 和状态码，响应后记录；补登录、MCP token、设置与插件变更、用户管理、备份恢复、发布、agent 提交、合入、证据采集与查询、运维操作；按日轮转；不带项目内容 | M | P1 |
| E5 | 配置注册表 | `config.py` 等 6 处，38 个变量 | 每项声明来源、校验、生效时机、是否密钥；`config.py` 保留同名属性当门面；删 7 个评测变量 | M | P1 |
| E6 | 可观测 | 4 个模块用 logging | JSON 日志 + contextvars 公共标签，脱敏 `token=`；`/metrics`；`metrics.py` 改名 coverage_metrics；依赖要进离线 wheels | M | P1 |
| E7 | 健康检查 | `main.py:112` 恒返回 ok | 拆成 `/api/health/live`（给 HEALTHCHECK）和 `/ready`（JWKS、模型网关、两个网关、磁盘、git、装配目录）；ready 不接 HEALTHCHECK | S | P1 |
| E8 | 运维管理面 | admin 12 个路由 | 用户管理、模型和 MCP 配置留在管理员；其余搬到 `routers/ops.py`，在 `run.py` 同一事件循环起第二个 uvicorn（127.0.0.1:8101，会话池在内存里，必须同进程）；新增排空、维护模式、诊断包、版本清单。独立端口挡不住同容器的 agent，仍要验 JWT | L | P1–P2 |
| E9 | 配额 | 无 | 用量账本（SQLite），在 counted_usage 时记账，send 前检查并发、token 和预算；自主构建预算只能设在剩余配额内 | M | P2 |
| E10 | 密钥移出 agent 可读范围 | 管理员 Key 明文、主密钥、MCP token 库都在 DATA_DIR | 主密钥只经环境变量或 secrets 注入；敏感文件移到 agent 进程读不到的位置（配合后续的按项目隔离） | M | **建议 P1**（原 P4） |
| E11 | 数据源登记 | 只有评测数据源的写法 | `evidence/registry`：全局登记（代码里没有租户概念），只存凭据引用；项目授权放 `DATA_DIR/evidence/grants/`，不放工作区文件（agent 能改） | M | P3（抽公共件在 P0） |
| E12 | 采集与快照 | 仿照 `onto_mcp/workspace.py:150-164` | 任务状态机，启动时把没跑完的标中断；Ed25519 一次性票据；快照校验、扫密钥后在项目锁下提交；资料子目录加 `systems`，只给内核用，不进上传和资料列表 | L | P3 |
| E13 | 证据检索 | Jiuwen 不接 MCP | `scr/evidence.py` 命令行形式，沿用 `source_extract` 的分页协议，FTS 索引；onto MCP 加 `evidence_*` 工具和 `evidence.read` 权限 | M | P3 |
| E14 | 文件提供方 | `source_extract.py:29-103`（`excel_context` 读的是产物、没有锚点，不能当起点） | 内核按路径加载同一个 `extract()`，片段映射成段落、单元格、术语记录；片段格式不改；用 resource 的提交号当快照号；补 drawio、pdf | M | P3 |
| E15 | 连接器网关 | `sandbox_gateway.py` | 新包 `connector_gateway`，不 import `app.config`。照搬并发准入、请求白名单与体积上限、读超时、/health、日志隔离、compose 加固；新增票据与 jti 防重放、加密凭据存储、提供方子进程（凭据经 stdin）、SQL 白名单、限流、流式取回快照包。驱动许可证宜做独立镜像 | L | P3 |
| E16 | 网络隔离 | compose 没定义网络 | 方案二选一：用 DOCKER-USER 规则丢弃主容器子网到业务网段的流量；或主容器只接 internal 网络，模型网关走白名单代理。preflight 里验证。`redeploy-app.sh` 沿用旧部署目录的 compose（`:18,65-79`），网络、init、新网关都不会生效，要一起改 | M | P3（沙箱网关的网络在 P0 顺手收紧） |

compose 增量示意：

```yaml
onto-modeling:     {init: true, networks: [default, onto-internal]}
sandbox-gateway:   {init: true, networks: [onto-internal]}
connector-gateway: {init: true, command: [python, -m, connector_gateway], expose: ["8120"],
                    volumes: [connector-secrets:/secrets], secrets: [gw_master_key, ticket_pub],
                    networks: [onto-internal, biz-egress]}
networks: {onto-internal: {internal: true}, biz-egress: {}}
```

镜像里要预建 `/secrets` 并 chown 给 65534，否则非 root 写不进命名卷。

---

## 3 目标代码布局

```
backend/app/
  api/v1.py                    # 汇总 router + 旧路径兼容中间件
  routers/                     # projects files resources studio knowledge versions project_settings
                               # publish solver sessions changesets artifacts metamodel
                               # packs flow(/flow /extensions /inbox) evidence admin ops internal
  ops_app.py                   # 运维管理面（127.0.0.1:8101，同进程）
  kernel/
    writepath/{tx,scope,events}.py
    changesets/{service,worktree,entity_diff}.py
    metamodel.py
    artifacts/{registry,parsers,index,refs}.py
  kernel_guards/               # settings.base.json、_guard_common.py、4 个守卫、pre_commit_check.sh
  services/
    workspace/{core,transfer,files,versions,errors}.py
    publish/{spi,ontology_package,gates,mapping,operations,archive,service}.py
    solver/{contract,validation}.py
    session_host/{manager,fingerprint,injection,exec_records}.py
    engines/                   # 现有 + jiuwen_worker.py、jiuwen_remote.py
    autobuild/{runner,policy,ledger,budget}.py
    packs/{repository,manifest,lockfile,assembler,registry,reconcile,contracts}.py
    flow/{model,assemble,state,suggest,inbox}.py
  evidence/{registry,grants,jobs,tickets,gateway_client,snapshot,index,anchors,materials}.py
  evidence/providers/file.py
  governance/{roles,audit,config_registry,telemetry,health,quota,backup,startup_recovery}.py
backend/connector_gateway/{server,tickets,secrets,policy,runner}.py、providers/{mysql,postgres}.py
```

- 启动恢复只有一个注册表（`governance/startup_recovery.py`），会话宿主、写入路径、证据任务各自注册步骤。
- 所有权和角色合在 `governance/roles.py`。
- `agent_sessions.py` 保留做兼容重导出（`admin.py:82` 直接读 `_sessions`）。

**数据目录与工作区：**

```
/data/onto-web/packs/<名>/<版本>/        包仓库，不可修改
/data/onto-web/assembled/<哈希>/          装配目录，放真文件
/data/onto-web/changesets/<pid>/assistant 助手变更集副本，每个项目固定一个路径
/data/onto-web/exec/<pid>/                执行记录
/data/onto-web/evidence/                  数据源登记、授权、采集任务
<工作区>/.onto/packs.lock、flow.lock        进 git
<工作区>/.onto/run/                        忽略：active.json、runs.jsonl、flow-state.json
<工作区>/ext/<包名>/                        插件产物
```

**9 个包的归属**（修正了设计文档 5.3、5.4 的 4 处归属）：

| 包 | 内容 |
|---|---|
| core | 命令 11 个：ontology、research、extract-object / -operation / -rule-metric、validate、query、overview、update、impact、version；子代理 13 个；脚本：state_helper、knowledge_helper、candidate_revision、ontology_checker、research_checker、field_semantics、verify_artifacts、check_diagrams、report_reconcile、query、impact_analysis、seed_example、source_extract、excel_context、script_verify、table_profile（新）；模板、参考指南 5 篇、约定文档、deep-research 方法论、nodes/、artifacts/knowledge.yaml |
| contracts | script_contract、script_descriptor、script_argument_shape、operation_relations、solver_evaluator_contract、research_review、jiuwenbox_client、script-template.py |
| trace 场景走查 | trace、trace-single / -batch / -interactive、mock；trace-agent、mock-data-agent；trace_inputs；页面 scenarios |
| knowledge 知识沉淀 | knowledge-conflict-agent、纠错信号提示规则、沉淀段落、3 个新命令；页面 knowledge |
| codegen 代码生成与求解 | codegen-scan、codegen、solver-evaluator；codegen-review-agent、script-candidate-agent；页面 codegen |
| diagram 图示 | flowchart、validate-diagrams、mermaid-flowchart；summarize_diagram、batch_summarize、init_flowchart_resources、check_diagram_excel_alignment |
| rev 系统逆向建模 | P3 新建；资料页子页 |
| evo 反馈演进 | ontology-evolution、check_eval_generalization（先改成读反馈入口） |
| websearch 联网调研 | deep-research 的联网部分 |
| migrate 迁移复用 | extract-migrated-objects |
| 删除 | mock_to_eval_seed、generate_overview、split_drawio、summary_to_drawio；check_eval_data 拆出 table_profile 后删 |

官方包保留现有命令名。包前缀只要求新包和租户包，用 `-` 分隔：Jiuwen 的命令展开正则（`jiuwen_assets.py:107`）不认 `:`，前端 `stageForCommand` 只认 `/ontology*`，历史会话里也都是旧名。

---

## 4 落地顺序（修订版）

在设计文档第 13 章的基础上调整：P0 补入第 0 章的 7 个问题；写入范围守卫从 P3 提到 P2；密钥移出提到 P1。

| 阶段 | 内容 | 关键依赖 |
|---|---|---|
| **P0 移出评测与止血** | 1. gitignore 修复与迁移（A13）<br>2. 单人所有权：无主项目指定所有者、删成员、管理员不读内容（A11、A12）<br>3. 发布按提交打包、打标签、版本号单调（A8、A9）<br>4. 先抽出凭据存储和后台任务公共件，再移出评测（A1～A7）<br>5. `/api/v1` 和兼容中间件（A14），拆 `projects.py`（A15）<br>6. 止血：运行中不切断（B1）、停机标中断（B9）、`index.lock`（E1）、备份（E2）、state_helper 补丁或执行记录最小版后再加 init（B5、B10）、后端跑脚本清环境（D19）、Jiuwen 补阶段提示和知识信号（B4 的 Jiuwen 部分） | 第 4 项依赖第 1、3 项 |
| **P1 声明化与插件运行时最小集** | 包仓库、锁文件、装配器、启动核对、契约库（D1～D6）；工具箱原样迁成 core 包 + 内核守卫（D7 前半、D8）；守卫出错即拒、补绕过（D9、D10）；flow.yaml 与 `/flow`（D12、D14）；元模型与链接解析（C3～C6）；统一变更事务（C1、C2）；指纹模块与引擎协议（B2、B15）；求解契约搬迁（A10）；配置注册表、可观测、健康检查、审计、角色（E3～E7）；密钥移出（E10）；运维管理面起步（E8） | 装配器先于指纹；ChangeTx 先于变更集 |
| **P2 内核能力** | 执行记录与续跑卡（B5、B6、B7）；两引擎注入（B4）；变更集副本、确认生效与撤回、实体差异（C7～C9、C12、C14、C15）；运行中只读和写入范围守卫（C10、D11）；agent 提交收口（C11）；节点和分段状态、suggest、待处理（D13、D15、D16）；扩展项注册表与页面（D17）；插件产物登记（C13）；拆可选包（D7 后半）；配额（E9）；Jiuwen worker（B11）；owner 固定（B3） | 变更集依赖 gitignore 修复；节点状态依赖执行记录和变更集 |
| **P3 开放扩展** | 节点包（D18）；自主构建（B12～B14，C8 的按段撤回）；证据源首版与连接器网关、网络隔离（E11～E16） | 自主构建依赖执行记录、变更集、注入、节点状态 |
| **P4 闭环** | 包和节点版本归因、灰度；评测反馈接入；更多提供方；系统知识包；按项目隔离 agent 的进程和读取 | — |

主要依赖链：

```
gitignore 修复 ──→ 变更集副本 ──→ 确认生效 / 撤回 ──┐
执行记录 ──→ 续跑卡、节点状态 ─────────────────────┼──→ 自主构建（P3）
宿主注入 ──────────────────────────────────────────┘
包仓库 → 装配器 → 指纹 → 按会话边界热更新
元模型单一定义 → 插件产物登记 → 插件页面的通用视图
写入范围守卫（㊹）→ 运行中只读
```

---

## 5 测试

- **整体删除：** test_modeling_eval、test_eval_import、test_eval_presets、test_eval_data_files、test_solver_validation_run、test_modeling_mcp、test_modeling_package_sync、test_eval_data_channel。
- **需要修改（约 30 个）：**
  - 写入与版本：test_lifecycle_and_versions、test_workspace（阶段数 11 → 10、软链断言、回退、版本编号、index.lock）
  - Studio 与 MCP：test_studio_write_path、test_studio_and_settings、test_onto_mcp、test_name_conflict
  - 会话与引擎：test_session_pool、test_conversations、test_claude_engine、test_jiuwen_engine、test_jiuwen_assets、test_personal_model_settings
  - 检查与守卫：test_candidate_revision、test_artifact_verification、test_artifact_gate_safety、test_entity_placement、test_script_verify
  - 链接与解析：test_report_reconcile、test_field_semantics、test_checker_relation_name、test_checker_relation_join、test_checker_data_source、test_eval_generalization（随迁移）
  - 发布与导入：test_publish_binding_gate、test_review_integrity、test_package_mapping、test_package_archive_import、test_solver_package_roundtrip、test_solver_contract_check、test_project_delete
  - 约 30 个 test_checker_*、test_script_* 直接拼 `workspace-toolkit/scripts` 路径，建议在 conftest 里统一提供脚本目录的 fixture。
- **现在零覆盖、要新建：** assets_admin、resource / toolset / lock_file 三个守卫、session_start_hook、knowledge_signal_probe、备份、admin 路由、健康检查。
- **新增：** v1 与旧路径兼容、所有权矩阵、发布包逐字节一致回归、test_writepath、test_changesets、test_write_scope、test_metamodel_links、test_artifact_registry、test_pack_repository、test_assembler（含装配中途杀进程后的恢复）、test_flow_runtime、test_flow_api、test_guard_bypass（把 4.8 的实测表做成参数化用例）、test_state_helper_compat、指纹、注入、执行记录、启动恢复、Jiuwen worker、自主构建运行器与预算。

---

## 6 需要拍板的事

| # | 问题 | 建议 |
|---|---|---|
| 1 | 图模式（给评测服务 Neo4j 导入用的导出）随评测服务走，还是留作发布适配器的一个变体 | 留作适配器变体，评测服务按需调用 |
| 2 | 运维角色需要 auth-server 新增 claim，auth-server 和 consumer 共用 | 接受，在 auth-server 加 `ops` 角色 |
| 3 | 网络隔离用哪种方案 | 主容器只接 internal 网络，模型网关走白名单代理；DOCKER-USER 规则依赖宿主配置，交付现场不好保证 |
| 4 | 密钥移出 agent 可读范围是否提前到 P1 | 提前。现在 agent 能读到管理员 Key 和主密钥 |
| 5 | 主密钥和密文是否分开备份 | 分开；同时要求配置 `ONTO_WEB_MODEL_SECRET_KEY`，否则异机恢复后个人 Key 解不开 |
| 6 | 三份契约的三个接口用什么鉴权 | 服务令牌，和 MCP token 分开签发 |
| 7 | 官方包是否保留现有命令名 | 保留，前缀只要求新包和租户包 |
