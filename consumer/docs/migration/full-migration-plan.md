# Meta-Wise Consumer 全量能力保全迁移方案

> 状态：执行中
> 编制日期：2026-07-13
> 目标仓库：`https://gitcode.com/how2win/meta-wise.git`
> 目标分支基线：本地 `develop@1d07c1ee4905b89843b5158e5bbedd91fa3b9537`
> 本地源码基线：`/Users/example/vscodePo/onto-agent`，`main@31fba9db`
> 目标结果：`meta-wise/consumer` 使用本地 `main` 最新目录结构和后端能力，保留远端全部新增能力；前端采用远端视觉体系，并将本地新增前端能力重做为同一风格。

## 1. 执行目标与硬约束

### 1.1 最终结果

迁移完成后必须同时满足：

1. `consumer/backend` 使用本地 `main` 的分层结构：
   `agent/`、`api/`、`llm/`、`config/`、`integration/`、`persistence/`、`ontology/`、`workspace/`、`utils/`。
2. 本地 `main` 当前所有通用后端能力和安全修复进入 `consumer`。
3. 远端新增的 Jiuwen、Graph、MCP、Trace、独立认证、workspace owner/member、DataAgent、知识包工作台等能力全部保留。
4. `auth-server/` 保持独立服务，consumer 与其只通过 HTTP/JWKS 通信，禁止 Python import。
5. `consumer/frontend/src/main.jsx` 继续进入远端 `RedesignApp`；本地新增页面和交互使用远端设计语言重做。
6. 最终只存在一个主 Agent loop：Jiuwen。不得长期保留 native/Jiuwen 双基座或静默 fallback。
7. 最终只存在一套运行时配置、workspace、store、route 和 tool registry。
8. 远端能力可以迁移到新路径、重构实现，但不得因采用本地文件而丢失行为。

### 1.2 明确禁止

- 禁止执行 `git merge --allow-unrelated-histories`、整批 cherry-pick 或普通 `git merge` 后批量解决冲突；两边没有共同 Git 祖先。
- 禁止用本地 `backend/`、`frontend/`、`requirements.txt`、compose 文件整目录覆盖远端。
- 禁止删除 `auth-server/`、MCP、Neo4j/HugeGraph、workspace 成员权限或远端 Trace 字段。
- 禁止独立 MCP 进程自建临时 orchestrator 或绕过 consumer backend；MCP chat 必须进入同一 workflow/run API，复用本地最新的限流、排队、模型和 workspace 契约。
- 禁止让 Graph/MCP 读取未经过 workspace/ontology scope 强制的数据。
- 禁止把 `AUTH_SERVER_ADMIN_TOKEN`、MCP secret、JWT 私钥写入代码或提交的配置文件。
- 禁止照搬 Jiuwen 适配器中的 `verify_ssl=False`。
- 禁止为了兼容旧目录保留 `consumer/backend/agentic` 运行时。
- 禁止在能力保全测试未通过时合入 `develop`。

### 1.3 不改动范围

以下顶层组件不得被本地 consumer 文件覆盖：

- `modeling/`、`evaluation/`、`common/`、`examples/` 保留当前远端内容。
- `auth-server/` 保留既有职责、API 和数据；只执行 M2 明确列出的并发安全、生产服务和部署加固。

本方案中的“本地新增前端能力”指本地 `frontend/`。`business-frontend/` 是独立业务场景应用，不并入远端知识工程控制台；其后端公共契约不得因迁移受损。

## 2. 基线和执行前门禁

### 2.1 唯一目标工作仓

本次迁移的唯一目标工作仓固定为：

```text
/Users/example/vscodePo/meta-wise
```

执行规则：

- 该目录是从 `https://gitcode.com/how2win/meta-wise.git` 的 `develop` 分支独立克隆得到的完整 monorepo。
- 迁移分支固定为 `codex/consumer-main-full-migration`，分支起点是本地 `develop@1d07c1ee...`。
- 所有迁移代码、测试、文档、commit 和后续 PR 都在这个仓库和分支上完成。
- `/Users/example/vscodePo/onto-agent` 只作为本地 `main` 源码来源；禁止在该仓为本次迁移提交实现代码。
- “合并”表示按能力和测试移植到目标分支，禁止执行无共同祖先的 `git merge --allow-unrelated-histories`。
- 自 2026-07-13 用户冻结基线起，禁止再次 fetch/pull `origin/develop`；即使远端跟踪分支出现新提交，也不纳入本次迁移。

当前已完成：

```bash
git clone --branch develop --single-branch \
  https://gitcode.com/how2win/meta-wise.git \
  /Users/example/vscodePo/meta-wise
git -C /Users/example/vscodePo/meta-wise \
  switch -c codex/consumer-main-full-migration
```

### 2.2 每次执行前检查

执行 Agent 每次开始工作前必须完成：

```bash
git -C /Users/example/vscodePo/onto-agent rev-parse main
git -C /Users/example/vscodePo/meta-wise rev-parse develop
git -C /Users/example/vscodePo/meta-wise status --short --branch
git -C /Users/example/vscodePo/meta-wise rev-parse HEAD
git -C /Users/example/vscodePo/meta-wise \
  merge-base --is-ancestor develop HEAD
```

GitCode 凭据只通过 credential helper 或临时环境注入，禁止把账号或令牌写进 remote URL、脚本、日志和迁移文档。

本仓库全部迁移提交固定使用 `FighterInTheSun <sunchengwu@example-brand.com>`；每次提交前后都校验 author 和 committer。

预期：

- 本地 `main` 为 `31fba9db...`。
- 本地 `develop` 为 `1d07c1ee...`，且是迁移分支 HEAD 的祖先。
- `onto-agent` 与 `meta-wise` 是独立历史的能力移植，不能按普通冲突合并估算。
- 目标仓当前分支为 `codex/consumer-main-full-migration`，且工作树干净。

远端 consumer 能力分析起点为 `011752d3...`；到冻结的本地 `develop@1d07c1ee...`，后续两次提交只修改 `evaluation/agent_eval`，`consumer/` 与 `auth-server/` 无差异。Graph Provider 约 25 文件/8466 行，Jiuwen loop 约 17 文件/3693 行，MCP 核心约 12 文件/1990 行（MCP 相关提交整体约 48 文件/4791 行）。远端跟踪分支在冻结后出现的提交明确排除，不再刷新或纳入工作量。

若本地 `develop` 或固定源码快照发生变化，停止代码迁移并等待用户明确指示；不得通过刷新远端分支自行改变基线。

本地源码使用隔离、detached 的本地克隆，只读使用，避免把 `/Users/example/vscodePo/onto-agent` 当前未提交文件带入迁移。当前执行环境将快照放在 `/tmp/onto-agent-main-source-31fba9db`：

```bash
git clone --no-hardlinks --single-branch --branch main --no-checkout \
  /Users/example/vscodePo/onto-agent \
  /tmp/onto-agent-main-source-31fba9db
git -C /tmp/onto-agent-main-source-31fba9db \
  checkout --detach 31fba9db4bd7103f4d3ab54da8f3031e50cef4d2
```

目标仓 `consumer/docs/migration/baseline.md` 必须记录两个完整 SHA；不再创建或推送额外的 reference 分支。

## 3. 决策优先级

| 冲突领域 | 采用来源 | 处理原则 |
|---|---|---|
| 文件和包结构 | 本地 `main` | 远端能力迁移到本地分层路径 |
| orchestrator、store、run transport | 本地 `main` | 保留 queued、SSE cursor、checkpoint、安全边界 |
| 主/子 Agent loop | 远端 | Jiuwen 成为唯一实现，适配本地 engine 接口 |
| 身份、登录、Token、用户 CRUD | 远端 | `auth-server` 为唯一身份源 |
| 全局角色和 workspace 授权 | 两边并集 | auth-server 提供身份/全局角色；consumer 强制 workspace owner/member 和本地最新存储安全 |
| MCP、Neo4j/HugeGraph | 远端 | 迁入新分层结构，接本地 workspace/data source/orchestrator |
| DataAgent/MindsDB/NL2SQL | 本地实现为主 | 保留两边支持的 provider 与远端 UI 能力 |
| 本体数据源 | 两边并集 | 一个模型支持 `direct_provider` 与 `connection_ref` |
| Trace/W3C | 两边并集 | 本地 observability 为主体，补远端 span 父子关系和 `traceparent` |
| 前端视觉和信息架构 | 远端 | `RedesignApp`、设计 token、三层配置中心为主体 |
| 前端运行逻辑 | 本地 `main` | 使用最新 hooks、会话恢复、交互 checkpoint、session pin |
| 依赖、SDK、部署 | 两边并集 | 本地依赖为底，追加远端必需依赖和 auth-server 生命周期 |

### 3.1 远端旧路径到目标路径

| 远端路径 | 目标路径 |
|---|---|
| `agentic/orchestrator.py` | `agent/core/orchestrator.py` |
| `agentic/loop/jiuwen_adapter.py` | `agent/core/jiuwen_engine.py`，通过 `agent/core/engine.py` 暴露统一门面 |
| `agentic/loop/jiuwen/*` | `agent/core/jiuwen/*` |
| `agentic/models/state.py`、`events.py` | 增量合入本地 `agent/state.py`、`agent/events.py` |
| `agentic/tools/*` | 增量合入本地 `agent/tools/*` |
| `agentic/business_data/*` | `agent/tools/business_data/*` |
| `agentic/routes/*` | `api/routes/*` |
| `agentic/a2a`、`agentic/acp` | 增量合入本地 `api/protocols/a2a`、`api/protocols/acp` |
| `agentic/services/llm/*` | 增量合入本地 `llm/*` |
| `agentic/services/auth.py`、`remote_auth.py` | `api/auth/auth.py`、`api/auth/remote.py`，BFF 路由进 `api/routes/auth.py` |
| `agentic/mcp/*` | 协议进 `api/protocols/mcp/*`；Token/目录进 `integration/mcp/*` |
| `agentic/observability/*` | 增量合入本地 `integration/observability/*` |
| `agentic/stores/*` | 按职责合入 `persistence/*`、`workspace/user_store_manager.py` |
| `agentic/perf_analysis/*` | `utils/perf_analysis/*` |
| `agentic/workspaces.py` | 增量合入本地 `workspace/workspaces.py` |

“增量合入”要求以目标文件当前实现为底，提取远端行为和测试；禁止把远端文件整份覆盖到新路径。

## 4. 目标架构

```text
meta-wise/
├── auth-server/                         # 保留，身份与 Token 独立服务
│   ├── auth_server/
│   └── tests/
├── consumer/
│   ├── backend/
│   │   ├── agent/
│   │   │   ├── core/
│   │   │   │   ├── engine.py           # 对 orchestrator 的唯一 engine 接口
│   │   │   │   ├── jiuwen_engine.py    # Jiuwen concrete engine
│   │   │   │   ├── warmup.py           # Jiuwen startup warm-up
│   │   │   │   └── jiuwen/             # 远端 Jiuwen adapter/tools/trace/runtime
│   │   │   ├── tools/
│   │   │   │   └── business_data/
│   │   │   │       └── providers/graph/# Neo4j/HugeGraph + Jiuwen Graph Agent
│   │   │   ├── sub_agents/
│   │   │   ├── memory/
│   │   │   ├── evolution/
│   │   │   └── skill_evolution/
│   │   ├── api/
│   │   │   ├── auth/                    # AuthBackend、RemoteAuthBackend、装饰器
│   │   │   ├── protocols/
│   │   │   │   ├── a2a/
│   │   │   │   ├── acp/
│   │   │   │   └── mcp/                 # 独立 MCP 进程 server/adapters/contracts
│   │   │   └── routes/                  # 包含 auth.py BFF routes
│   │   ├── integration/
│   │   │   ├── observability/
│   │   │   ├── platform/
│   │   │   └── mcp/                     # MCP token/store/catalog/config
│   │   ├── persistence/
│   │   ├── ontology/
│   │   ├── workspace/
│   │   ├── llm/
│   │   ├── config/
│   │   └── utils/
│   ├── frontend/
│   │   └── src/
│   │       ├── redesign/                 # 唯一活动外壳与设计系统
│   │       ├── components/               # 本地能力重做为远端风格后的页面组件
│   │       ├── hooks/                    # 本地最新运行/恢复 hooks
│   │       └── utils/
│   ├── sdk/
│   ├── deploy/
│   └── tools/
├── modeling/
├── evaluation/
└── deploy/
```

### 4.1 认证和授权边界

`auth-server` 负责：

- 用户凭据和 PBKDF2 密码策略。
- RS256 access/refresh JWT、JWKS、discovery、refresh、revoke。
- 注册和用户 CRUD。
- `sub`、全局 `role`、强制改密状态。

consumer 负责：

- `/auth/*` BFF 和对外冻结契约。
- access JWT 本地 JWKS 验签。
- `require_auth`、`require_role`、强制改密白名单。
- workspace owner/member、路由访问裁决。
- user/workspace/session/run 存储隔离。
- MCP token 的 workspace scope 强制。

身份和权限数据的唯一所有者固定如下：

| 数据或决策 | 唯一所有者 |
|---|---|
| `sub`、密码、email、强制改密 | `auth-server` |
| 全局 `admin/user` 角色 | `auth-server` 存储，consumer 按 access claim 执行 |
| access/refresh token、`token_version`、RSA 私钥/JWKS | `auth-server` |
| workspace owner/members | consumer workspace store |
| workspace、session、run、MCP 访问裁决 | consumer backend |
| `AUTH_SERVER_ADMIN_TOKEN` | 部署 secret，仅 backend 与 auth-server 可见 |

当前远端 access token 默认 15 分钟。改密、删除用户或角色降级后，旧 refresh token 立即失效，已签发 access token 最长仍可使用一个 access TTL；迁移测试必须锁定该边界。当前 refresh token 可重复使用到过期或 `token_version` 变化，迁移不得在未同步前端和测试时私自改变此契约。

auth-server 能力冻结清单：

- 标准端点：`POST /token`（password、refresh）、`POST /revoke`、discovery、`/jwks.json`、`/userinfo`、`/health`。
- 注册端点：`POST /register`，保留开关、用户名/email 唯一性、密码策略和进程内限流。
- Admin 端点：用户列表、创建、更新、删除和管理员改密；consumer BFF 继续负责“不能删除/重置自己”等操作者约束。
- access claims：`iss/aud/sub/role/pwd_cr/ver/typ/iat/exp`；refresh 另含 `jti`，且必须严格区分 `typ`。
- RS256/RSA 2048、`kid`、旧公钥验签和 JWKS 未知 `kid` 主动刷新能力全部保留。

### 4.2 数据连接统一模型

目标只保留一份本体数据源真相：`ontology-data-sources.json`。

```json
{
  "mode": "direct_provider | connection_ref",
  "provider": "dataagent | mindsdb | nl2sql_http | nl2sql_template | neo4j | hugegraph",
  "config": {},
  "connection_id": "可选，仅 connection_ref 使用"
}
```

- `direct_provider` 保留本地最新的本体直连能力。
- `connection_ref` 保留远端共享数据连接、默认连接和知识绑定能力。
- 远端 `/data-connections*` 继续作为共享连接管理 API。
- `/ontologies/<id>/data-binding` 写入 `connection_ref`，不再维护第二份运行时 binding 真相。
- 旧 `ontology-bindings.json` 只作为幂等迁移输入；迁移完成后不得继续写入。

## 5. 能力保全矩阵

| ID | 必须保留的能力 | 来源 | 目标模块 | 最低验收 |
|---|---|---|---|---|
| C01 | Jiuwen 主 Agent loop | 远端 | `agent/core/jiuwen` | quick/thinking/multi_agent 全通过 |
| C02 | Jiuwen 子 Agent | 远端 | `agent/sub_agents` + Jiuwen engine | workspace/profile/trace 正确传播 |
| C03 | Jiuwen 常驻 event loop 和 warm-up | 远端 | `agent/core/jiuwen`、`app.py` | 并发无跨 loop 锁错误；预热不污染工具注册 |
| C04 | queued、run replay、SSE cursor | 本地 | `api/routes/workflows.py`、`persistence` | 断线后 `Last-Event-ID` 无重复恢复 |
| C05 | durable interaction checkpoint | 本地 | orchestrator/store/frontend | input/select/approval 可暂停、恢复、只消费一次 |
| C06 | 严格工具 schema 和安全并发工具 | 本地 | `agent/tools` + Jiuwen adapter | 类型校验、barrier、terminal batch 通过 |
| C07 | 独立 auth-server | 远端 | 根 `auth-server` + consumer BFF | login/refresh/revoke/JWKS/CRUD 全链路 |
| C08 | workspace owner/member | 远端 | `workspace`、workspace routes | 水平越权全部 403 |
| C09 | session/run 存储边界 | 本地 | workspace/store/routes | 不能跨用户、跨 workspace 读取或续跑 |
| C10 | Neo4j/HugeGraph Graph Provider | 远端 | business_data graph provider | 14 类图工具、scope、sanitize 通过 |
| C11 | Jiuwen GraphQueryAgent | 远端 | graph provider | 私有图工具、重试、去重、超时通过 |
| C12 | DataAgent/MindsDB/NL2SQL | 两边 | business_data providers | 四类 provider 及远端 DataAgent 重试/结果归一化通过 |
| C13 | 共享连接 + 本体直连 | 两边 | unified data source | 两种模式可保存、测试、解析、迁移 |
| C14 | 独立 MCP search/chat 服务 | 远端 | protocols/mcp + integration/mcp | 7488 health、两类 transport、14 search tools、chat 通过 |
| C15 | MCP Token 与管理 UI | 远端 | integration/mcp + Redesign L2 | scope、创建时显示一次、列表、last-used、过期、吊销 |
| C16 | Trace 树父子关系 | 远端 | SSE + observability + frontend | `span_key/parent_key` 历史/实时一致 |
| C17 | W3C traceparent | 远端 | workflows/observability | 外部 trace 与 LLM/工具 span 同树 |
| C18 | 知识包树、预览、导入、启停、版本、下载 | 远端 | ontology API + redesign | 全生命周期 E2E 通过 |
| C19 | v0.1 知识包读取 | 远端 | ontology import normalization | v0.1 导入后统一为当前内部模型执行 |
| C20 | 用户记忆、operation/skill evolution | 本地 | agent/api/frontend | CRUD、diff、审批、版本门禁通过 |
| C21 | 本体结构图 | 本地 | ontology graph API/frontend | 全屏和数据加载通过 |
| C22 | role-bound/workspace 私有模型 | 本地 | config/llm/frontend | main/task 继承和私有 profile 通过 |
| C23 | workspace observability、性能分析 | 本地 | integration/frontend | 配置、测试连接、运行详情通过 |
| C24 | 远端控制台视觉与三层配置 IA | 远端 | `frontend/src/redesign` | 所有新增页面使用同一 token/组件体系 |
| C25 | 会话置顶、恢复、锁定配置 | 本地 | hooks/ChatHistory/ChatWorkspace | 刷新、换页、等待输入、运行中恢复通过 |
| C26 | A2A、ACP 协议适配 | 本地 | `api/protocols`、routes | 发现、创建、流式、取消、续跑复用同一 orchestrator |
| C27 | 本体/技能/脚本工具链 | 本地 | ontology、skill_parser、agent tools | 导入、编辑、assets、read ledger、execute_script 通过 |
| C28 | Web Search 与 onto-platform | 本地 | agent tools、integration/platform | 配置、连接、错误契约和调用链通过 |
| C29 | Ops 配额、日志与存储标识安全 | 本地 | config、utils、persistence | 配额、保留策略、路径/标识防碰撞测试通过 |
| C30 | SDK 与 business-frontend 公共契约 | 本地 | sdk、HTTP/SSE schema | 现有调用方 contract/E2E 无回退 |

执行期间维护本表状态。任何一项未通过，迁移分支不得合入。

## 6. 分阶段执行计划

每个任务完成后必须：

1. 运行该任务列出的验证。
2. 运行 `git diff --check`。
3. 提交一个单一职责 commit。
4. 在本文任务复选框和能力矩阵旁记录结果；失败不得标记完成。

### Phase 0：基线、保护网和契约冻结

#### M0-1 固定源码和目标基线

- [x] 将远端仓克隆到 `/Users/example/vscodePo/meta-wise`。
- [x] 基于本地 `develop@1d07c1ee...` 创建 `codex/consumer-main-full-migration`。
- [x] 为 `onto-agent/origin/main` 建立 detached、隔离源码快照：`/tmp/onto-agent-main-source-31fba9db`。
- [x] 记录本地和远端 commit、Python/Node/Docker 版本，见 `baseline.md`。
- [x] 在目标分支导出两端 tracked 文件、route 和 test 清单到 `consumer/docs/migration/inventories/`。
- [x] 创建 `remote-capability-ledger.md` 和 `local-main-capability-ledger.md`，逐项对应 C01-C30。

完成条件：记录的 commit 可以复现相同源码；所有新增文件和 commit 只出现在 `/Users/example/vscodePo/meta-wise` 的迁移分支。

建议提交：`chore(migration): pin consumer migration baselines`

#### M0-2 先迁移测试清单

- [x] 为远端 auth、Jiuwen、Graph、MCP、workspace authz、Trace 建立测试迁移映射。
- [x] 冻结远端测试场景和测试名；迁移时只修改 import、fixture 和新契约需要的断言。
- [x] 标记外部服务测试与纯单元测试，禁止用 `skip` 隐藏迁移失败。
- [x] 在远端原结构复现当前基线：auth-server `32 passed`；`test_remote_auth.py + test_auth_register_proxy.py + test_workspace_authz.py` 合计 `47 passed`。结果见 `baseline-test-report.md`。

完成条件：所有远端测试都有目标文件、执行阶段和验收命令。

### Phase 1：采用本地最新结构和公共能力

#### M1-1 重建 consumer 后端骨架

来源：本地 `main@31fba9db`。

- [x] 将本地 `backend` 的分层包、测试、SDK 相关契约迁到 `consumer`。
- [x] 保留远端独有代码引用，不在此阶段尝试把 `agentic/` 作为兼容包运行。
- [x] 合并 requirements：本地依赖为底，追加 `openjiuwen==0.1.14`、`neo4j>=5.0.0`、`PyJWT>=2.8.0`、`fastmcp>=2.0.0`、`uvicorn>=0.30.0`、`jsonschema-path>=0.3.4`。
- [x] 保留远端 `auth-server` 的 Authlib/cryptography 独立 requirements。
- [x] 将本地 `.onto-runtime`、workspace、ops/system config 约定作为 consumer 运行时约定。

验证：

```bash
cd consumer
PYTHONPATH=backend ./venv/bin/python -m pytest \
  backend/tests/test_tool_registry.py \
  backend/tests/test_universal_prompt.py \
  backend/tests/test_routes_stream.py \
  backend/tests/test_session_routes.py -q
```

完成条件：本地核心测试在 meta-wise consumer 路径下通过；远端能力测试仍允许处于待迁移状态，但不得删除。

建议提交：`refactor(consumer): adopt latest layered backend structure`

#### M1-2 迁移本地最新运行能力

- [x] 保留 `RunAdmissionQueue`、token bucket、run handle 和 replay buffer。
- [x] 保留 `/agentic/runs/<run_id>/events`、`Last-Event-ID`、status/cancel。
- [x] 保留 strict interaction checkpoint、响应校验和一次性消费。
- [x] 保留 session storage boundary、workspace-private model、role-bound refs。
- [x] 保留当前 tool registry、memory、operation evolution、skill evolution。
- [x] 保留 A2A/ACP、Web Search、onto-platform、ontology/skill/script parser、assets、ops 配额和日志保留策略。

验证：

```bash
cd consumer
PYTHONPATH=backend ./venv/bin/python -m pytest \
  backend/tests/test_run_admission_and_replay.py \
  backend/tests/test_stream_protocol_v2.py \
  backend/tests/test_session_routes.py \
  backend/tests/test_orchestrator_runtime.py \
  backend/tests/test_tool_registry.py \
  backend/tests/test_a2a_routes.py \
  backend/tests/test_acp_regressions.py \
  backend/tests/test_platform_connector.py \
  backend/tests/test_web_search_tool.py \
  backend/tests/test_skill_ontology_parser.py \
  backend/tests/test_storage_identifier_security.py -q
```

### Phase 2：auth-server 与授权体系

#### M2-1 保留并验证 auth-server

- [x] 原样保留 `auth-server/auth_server/{config,keys,policy,registration,routes,tokens,users}.py` 的能力。
- [x] 保留 password/refresh grant、revoke、JWKS、discovery、userinfo、Admin API、开放注册。
- [x] 保留 access TTL 硬上限、token_version 吊销和密钥轮换。
- [x] 保留 PBKDF2-SHA256 600000 次、旧低迭代哈希自动升级、遗留明文首登迁移、文件权限 0600。
- [x] 将 UserStore 的 read-modify-write 放入完整事务锁，补并发创建用户、改密和 revoke 不丢更新测试。
- [x] 生产改用单 worker WSGI server；JSON 用户库阶段禁止部署多个 auth-server 副本。
- [x] 明确生产部署中 auth-server 为必备服务，不再提供 local auth 模式。

验证：

```bash
cd auth-server
.venv/bin/python -m pytest tests -q
```

建议提交：`test(auth): freeze standalone auth server contracts`

#### M2-2 在本地结构中接入 RemoteAuthBackend

目标文件：

- `consumer/backend/api/auth/auth.py`：`AuthUser`、`AuthBackend`、decorators、current user。
- `consumer/backend/api/auth/remote.py`：远端 `RemoteAuthBackend`。
- `consumer/backend/api/routes/auth.py`：login/refresh/logout/register/me/users/change-password BFF。
- `consumer/backend/app.py`：只构造 remote backend，缺 `AUTH_SERVER_URL` 时启动失败。

执行：

- [x] 冻结对外 `/auth/login` 响应 `{token,user}`，加法保留 `refresh_token/expires_in`。
- [x] JWKS 按 `kid` 缓存，未知 `kid` 强制刷新一次。
- [x] access JWT 只接受 RS256、正确 issuer/audience、`typ=access`。
- [x] 强制改密仅放行 `/auth/me`、`/auth/change-password`、必要的静态/健康端点。
- [x] admin token 只存在 consumer 后端和 auth-server 内网之间。
- [x] 将登录改为原子接口 `login(username, password) -> LoginResult`，删除按 username 暂存 token 的三步缓存，避免同名用户并发登录串 token。
- [x] 增加 `/auth/logout`，先代理 `/revoke` 吊销 refresh token，再由前端清理本地 token。
- [x] 删除生产装配中的本地 HMAC token 路径；`AuthManager` 仅作为离线契约测试后端，不提供生产 fallback。

验证：迁移并运行远端的 `test_auth_contract.py`、`test_remote_auth.py`、`test_auth_register_proxy.py`，同时运行本地 `test_auth_security.py`、`test_auth_limits.py`。

建议提交：`feat(auth): make standalone auth server the consumer identity backend`

#### M2-3 合并 workspace owner/member 与本地安全边界

目标文件：

- `consumer/backend/workspace/workspaces.py`
- `consumer/backend/api/routes/workspaces.py`
- 所有直接从 URL、持久化 run/session 读取 workspace id 的入口

执行：

- [x] workspace metadata 增加 `owner`、`members`，新建空间 owner 为创建者。
- [x] 存量无 owner 空间按远端既有语义视为公共。
- [x] `resolve_workspace_id` 调用 `enforce_workspace_access`。
- [x] workspace 列表按用户过滤并返回当前 `role`。
- [x] owner 可修改空间信息和成员；全局 admin 保留全权限。
- [x] 给 runtime config、LLM profiles、observability、memories、ontology、data source、session、run resume、A2A、ACP、MCP 补齐访问强制。
- [x] workspace JSON 写入使用原子替换和锁；不得回退本地最新 session/store 隔离。
- [x] Workspace ACL 必须在选择用户存储和打开 session store 之前执行。
- [x] consumer 删除用户前扫描 workspace；用户仍是 owner 时返回 `409 WORKSPACE_OWNER_TRANSFER_REQUIRED`。
- [x] 用户删除成功后从全部 members 中清除该用户名；失败写入可重试 reconciliation 记录。
- [x] 提供一次性 owner/member 审计脚本，报告 auth-server 中不存在的用户名，防止同名账号重新注册后继承旧权限。

验证：完整迁移 `test_workspace_authz.py`，并增加以下用例：

- 被移出成员后不能恢复旧 run。
- 不能通过 session id、run id、MCP token 绕过 workspace 权限。
- owner 无法获得全局 admin 权限。
- 默认 workspace 的公开语义与远端一致。
- 删除 owner 必须先转移所有权；删除 member 后同名账号重新注册不能继承旧权限。

建议提交：`feat(authz): combine workspace ownership with latest storage isolation`

Phase 2 自动化结果（2026-07-13）：auth-server `34 passed`；consumer auth、workspace、跨入口、session、A2A、memory、MCP 和用户删除合并回归 `124 passed`；用户删除与 ACL 审计增量门禁 `39 passed`。回环 auth-server 测试使用本机随机端口执行，无跳过项。

### Phase 3：Jiuwen 唯一 Agent loop

#### M3-1 迁移 Jiuwen runtime 边界

来源：

- `consumer/backend/agentic/loop/jiuwen_adapter.py`
- `consumer/backend/agentic/loop/jiuwen/*`
- `consumer/backend/agentic/warmup.py`

目标：

- `consumer/backend/agent/core/engine.py`
- `consumer/backend/agent/core/jiuwen_engine.py`
- `consumer/backend/agent/core/warmup.py`
- `consumer/backend/agent/core/jiuwen/*`

执行：

- [x] 保持 orchestrator 看到的 `run(state, caller, event_writer, snapshot_callback, iteration_limit, system_prompt, ...)` 接口。
- [x] Jiuwen LLM 配置适配本地 role-bound profile，并显式传 `workspace_id`。
- [x] Jiuwen ToolCard 从本地最新 `TOOL_SPECS/get_tools_schema` 生成。
- [x] 工具执行只调用本地最新 `execute_tool_batch`。
- [x] 保留本地严格 JSON schema、caller、parallel-safe/barrier、terminal batch 语义。
- [x] 取消信号由 orchestrator/run handle 注入，不恢复旧 `LLMRouter.get_cancel_event()`。
- [x] 使用本地 `agent/context.py` 的上下文窗口策略，不恢复已删除旧模块。
- [x] 删除 `ov_session_id`、OpenViking 和旧 token limit 路径。
- [x] 保留 Jiuwen content/reasoning stream、history、ontology preload、snapshot。
- [x] 保留 `reasoning -> reasoning_content` 兼容补丁；用 `call_index`、`batch_index` 区分同一 iteration 内多次 LLM 调用和多个 tool batch。
- [x] 主 Agent、SubAgent 和工具资源 ID 必须唯一，禁止跨 run 复用 Jiuwen 实例状态。
- [x] 保留常驻 asyncio loop、并发锁修复和 warm-up。
- [x] warm-up 不调用 LLM、不查询业务数据、不产生 token，不注册带假 state 的 runtime delegate；失败记录告警但不阻断服务，真实请求不复用 warm-up 实例。

#### M3-2 主 Agent 和子 Agent 全部切换

- [x] `AgenticOrchestrator` 使用 Jiuwen engine。
- [x] sub-agent 使用相同 Jiuwen runtime，传播 parent workspace/profile/lineage。
- [x] quick/thinking/multi_agent 的工具曝光规则与本地最新一致。
- [x] 达到行为一致后删除 native loop 代码，不保留环境开关 fallback。

验证：

```bash
cd consumer
PYTHONPATH=backend ./venv/bin/python -m pytest \
  backend/tests/test_jiuwen_react_adapter.py \
  backend/tests/test_loop_shared.py \
  backend/tests/test_spawn_sub_agent_orchestration.py \
  backend/tests/test_tool_registry.py \
  backend/tests/test_universal_prompt.py \
  backend/tests/test_orchestrator_cancellation_summary.py -q
PYTHONPATH=backend ./venv/bin/python -m agent.core.jiuwen.smoke
```

补充压力验证：至少 20 个并发 run、持续 500 个短 run，检查 event-loop 错误、Runner resource 增长和工具注册释放。

执行结果见 `jiuwen-runtime-pressure.md`：真实 OpenJiuwen 0.1.14 与离线模型完成 500/500 次运行、20 路并发；结束后 owned tool/resource/callback/metric/checkpoint 全部归零，唯一 Jiuwen loop 无 pending task、ready handle 或 scheduled handle。

建议提交：`feat(agent): port Jiuwen as the only loop on latest runtime contracts`

### Phase 4：业务数据、Graph 和知识包兼容

#### M4-1 合并远端 DataAgent 可靠性能力

目标文件：

- `consumer/backend/agent/tools/business_data/providers/dataagent_provider.py`
- `consumer/backend/agent/tools/business_data/providers/clients/dataagent.py`
- `consumer/backend/agent/tools/business_data/providers/clients/data_provider_client.py`

- [ ] 保留本地 `OntologySearchEvent`、本体级数据源解析和 secret encryption。
- [ ] 合入远端 `requests.Session`、urllib3 retry/backoff、`max_retries` 和 500/502/503/504 自动重试。
- [ ] 保留 A2A 1.0 Header，兼容 `TASK_STATE_COMPLETED` 与 `completed`。
- [ ] status message 为空时从 artifacts 提取结果。
- [ ] 统一归一化非 JSON、HTTP 错误、自然语言答案和结构化 SQL rows；错误和日志不得泄露密钥。

验证：扩展 `test_dataagent_provider.py`，覆盖 retry 次数、artifact fallback、失败状态、自然语言、SQL rows、无效 JSON 和密钥脱敏。

建议提交：`feat(data): merge remote DataAgent reliability into latest provider`

#### M4-2 合并数据连接模型

- [ ] 保留本地 `ontology-data-sources.json` 和 provider secret encryption。
- [ ] 恢复远端 `/data-connections*` CRUD/default/test。
- [ ] 增加 `connection_ref` mode，binding 解析为共享连接。
- [ ] 迁移旧 `ontology-bindings.json`，迁移幂等且先备份。
- [ ] 删除连接时拒绝删除仍被引用的连接，或要求显式解除绑定；不得产生悬空引用。
- [ ] direct/provider 与 shared/reference 都经过 workspace authz。

验证：扩展本地 `test_ontology_data_sources.py`、`test_business_data_connections.py`，覆盖两种模式和迁移。

#### M4-3 迁移 Neo4j/HugeGraph 与 Jiuwen Graph Agent

目标路径：`consumer/backend/agent/tools/business_data/providers/graph/`。

- [ ] 迁移 backends、mapping、scope、models、runtime、GraphToolExecutor 和 14 类图工具。
- [ ] 冻结 14 个工具名：`get_object_types`、`get_relation_types`、`get_nodes_schema`、`get_edges_schema`、`property_filter`、`pattern_search`、`count_search`、`aggregate_search`、`sorted_search`、`get_element_info`、`hop_search`、`shortest_path`、`list_logic_titles`、`get_logic_content`。
- [ ] 在本地 provider registry 注册 `neo4j`、`hugegraph`。
- [ ] 迁移 connection tester 和 provider schema。
- [ ] 保留 `workspace_id + ontology_id` 强制 scope，禁止调用方覆盖内部 scope 字段。
- [ ] 保留标识符校验、通配符限制、行数上限和统一 sanitize，隐藏内部 workspace/scope 字段。
- [ ] 映射解析顺序固定为本体版本 mapping、Graph 扩展 mapping、Schema 精确匹配。
- [ ] 保留 schema 预加载、关系三元组、路径意图、operation/rule/script 业务上下文注入。
- [ ] 迁移 GraphQueryAgent，复用 M3 的 Jiuwen runtime/LLM profile 解析。
- [ ] 保留批量子查询、去重、截断、标准错误码、超时、重试和 path intent。
- [ ] Graph 内部工具只供 Graph Agent 和 MCP Search 使用，禁止注册为主 Agent 公共工具。
- [ ] 保留图工具 trace parent，挂到外层 `business_data_search` span。
- [ ] 本地 `/agentic/ontologies/<id>/graph` 继续表示知识包静态结构；Neo4j/HugeGraph 表示业务实例数据，两项能力分别保留。
- [ ] 迁移图导入、scope migration 工具到 `consumer/tools/`。

验证：迁移并运行远端所有 `test_graph_*`、`test_neo4j_graph_backend.py`、`test_hugegraph_graph_backend.py`。

增加真实服务门禁：

- Neo4j：schema、filter、aggregate、pattern、shortest path。
- HugeGraph：同一能力矩阵。
- 两个 workspace 使用同一个图实例时数据不可串读。

建议提交：`feat(data): port scoped Neo4j and HugeGraph providers`

#### M4-4 保留 v0.1 与当前知识包格式

- [ ] import 层识别远端 v0.1 和当前格式。
- [ ] 统一转换成当前内部 manifest/catalog，运行时不维护两套分支。
- [ ] 保留远端树浏览、raw/Markdown 预览、启停、active version、版本删除和下载能力。
- [ ] 在本地 `ontologies.py` 上增量接入 `ontology/package_browser.py`，禁止整文件覆盖。
- [ ] 保留远端 `GET /agentic/ontologies/<id>/tree` 和 `GET /agentic/ontologies/<id>/raw-file?path=&version=` 契约；它们与本地 `/files`、`/file` 共用一个 package browser，不形成第二套存储。
- [ ] 浏览器排除 `origin_data`、`origin-data`、`datasets`、`dataset`、点目录和 `__pycache__`，拒绝路径穿越、符号链接、二进制文件和超过 2 MB 的文件。
- [ ] 保留 409 覆盖活动版本确认、历史版本只读、目录筛选、面包屑和首个可查看文件自动选择。

建议提交：`feat(ontology): preserve legacy package import in the latest runtime`

### Phase 5：MCP

#### M5-1 迁移 MCP 结构

协议层目标：

- `consumer/backend/api/protocols/mcp/server.py`
- `chat_adapter.py`、`search_adapter.py`、`schemas.py`、`tool_contracts.py`

服务层目标：

- `consumer/backend/integration/mcp/config.py`
- `scope.py`、`service_catalog.py`、`token_crypto.py`、`token_service.py`、`token_store.py`
- `consumer/backend/api/routes/mcp.py`

执行：

- [ ] MCP 保持独立进程，默认端口 7488；提供 `/health`。
- [ ] 保留 Streamable HTTP 的 `/search/mcp`、`/chat/mcp` 和 SSE 的 `/search/sse`、`/chat/sse`。
- [ ] 保留 FastMCP streamable-http/SSE 两种 transport。
- [ ] 保留 search/chat toolset 和服务目录。
- [ ] 保留用户+workspace scope 的 Token 创建、列表、吊销、过期和 last-used。
- [ ] Token 明文只在 POST 创建响应出现一次；磁盘只存 jti/hash、masked token 和元数据，GET 列表永不返回明文。
- [ ] 为存量 `token_value` 提供一次性清理迁移，迁移前备份，完成后扫描磁盘和 API 响应确认无明文。
- [ ] `MCP_TOKEN_SECRET` 必须独立配置；生产禁止开发默认值。
- [ ] 每次 MCP 调用重新检查 token registry 状态和当前 workspace membership。
- [ ] search adapter 复用 M4 Graph runtime，直接暴露 14 个确定性图工具，不调用 LLM。
- [ ] chat adapter 通过 consumer backend 的内部 workflow bridge 进入 canonical Jiuwen orchestrator 和 `RunAdmissionQueue`；独立 MCP 进程不得创建模块级 orchestrator 单例。
- [ ] backend bridge 在服务端重新验证 MCP token、scope、subject 和当前 workspace membership；创建的 run/session owner 必须是 token subject。
- [ ] queued chat 在配置的等待时间内等待 admission；超时返回结构化错误，不绕过队列。
- [ ] awaiting-input 必须返回明确的可恢复状态、run_id 和 session_id，禁止伪装成成功完成。

验证：

```bash
cd consumer
PYTHONPATH=backend ./venv/bin/python -m pytest \
  backend/tests/test_mcp_search_tools.py \
  backend/tests/test_mcp_token_service.py \
  backend/tests/test_mcp_transport_mounting.py -q
```

真实服务门禁：7488 health、四个端点、wrong scope、expired、revoked、跨 workspace token、chat run owner、重启后 token metadata 全部验证。

建议提交：`feat(mcp): rebase search and chat services on the latest runtime`

### Phase 6：Trace 和观测

#### M6-1 合并实时和历史 Trace 契约

目标文件：

- `consumer/backend/utils/streaming.py`
- `consumer/backend/agent/tools/events.py`
- `consumer/backend/integration/observability/*`
- `consumer/backend/api/routes/sessions.py`
- `consumer/frontend/src/utils/streamHelpers.js`

执行：

- [ ] `agent-stream.v2` envelope 增加可选 `span_key`、`parent_key`。
- [ ] tool span 挂到对应 iteration；Graph 内部工具挂到外层 business data span。
- [ ] sub-agent lineage 和 iteration 保持稳定。
- [ ] 同一 iteration 中使用 `call_index`、`batch_index` 稳定区分多次 LLM 和 tool batch。
- [ ] 保留 `GET /agentic/sessions/<session_id>/turns?status=&has_feedback=true` 结构化查询，turn feedback 独立持久化，禁止依附 evolution store。
- [ ] 历史重建和实时流使用同一节点 key 算法。
- [ ] 前端缺失父节点时合成父节点，旧平铺事件仍可展示。
- [ ] 接收并严格校验 W3C `traceparent` 的长度、十六进制格式和非全零值，再传入 LLM、工具、Jiuwen、Graph、MCP chat span。
- [ ] 性能汇总保留 `had_reasoning`、`calls_with_reasoning`、`total_reasoning_chars`、`first_reasoning_ms`。
- [ ] observer shutdown 必须最终 flush/close。
- [ ] 保留本地 workspace observability 和性能分析。

验证：迁移 `test_graph_provider_trace_parent.py`、Trace 相关回归，并增加“断线重放后树不重复、不换父节点”测试。

建议提交：`feat(trace): preserve hierarchical spans across replay and providers`

### Phase 7：远端风格前端融合

#### M7-1 固定远端设计系统

保留并继续使用：

- `frontend/src/redesign/RedesignApp.jsx`
- `AppShell.jsx`
- `theme.js`
- `redesign.css`
- `OnboardingWizard.jsx`
- 三层配置中心 IA

- [ ] 将远端颜色、字体、间距、圆角、边框、状态色提炼为 CSS variables。
- [ ] 新建远端风格通用组件：PageHeader、SectionCard、FieldRow、Table、Modal、EmptyState、StatusTag、ScopeTabs。
- [ ] 所有迁入页面使用这些组件，禁止直接把本地旧 CSS 大块粘入活动页面。

#### M7-2 对话工作区融合

- [ ] 以本地最新 `useStreamManager`、`useChatSessions`、`useAgenticStream` 为运行核心。
- [ ] 保留远端 ChatWorkspace、ChatHistorySidebar、可拖动 Trace 侧栏。
- [ ] 增加 queued/running/awaiting 状态、SSE cursor 恢复、事件去重。
- [ ] 增加 input/select/approval 交互表单。
- [ ] 增加会话 pin、当前会话持久化、模型/本体/Agent 模式锁定。
- [ ] 保留 quick/thinking/multi_agent、反馈、复制和历史 Trace。
- [ ] 保留 `/auth/refresh` 静默续期；一次失败后清理 access/refresh token 并回登录页。
- [ ] 并发 401 只允许一个 refresh 请求；退出时先调用 `/auth/logout`，成功或明确失败后再清理本地 token。

#### M7-3 知识包工作台融合

远端工作台保留，扩展为统一页签：

1. 文件与版本：远端目录树、预览、导入、启停、激活、删除、下载。
2. 数据源：本地直连 + 远端共享连接/绑定。
3. 本体图：本地结构图和全屏。
4. 记忆进化：operation evolution、skill evolution、diff/审批。

- [ ] 所有本地组件按远端卡片、表格、弹窗、状态标签风格重做。
- [ ] 保留权限门控和 workspace 切换刷新。
- [ ] 保留活动/历史版本浏览，支持 `.md/.yaml/.yml/.json/.py/.txt/.csv/.sql` 在线预览和 Markdown 渲染/源码切换。
- [ ] 融合本地编辑、文件上传、Import Preview/免责声明、assets 管理和 Trace 实时高亮；历史版本保持只读。

#### M7-4 三层配置中心融合

L1 全局：

- 模型池和 main/task 默认引用。
- Web Search、平台接入、全局 observability、Agent 参数。
- 用户管理。

L2 工作空间：

- 空间信息、owner/member。
- workspace 私有模型与 role-bound 选择。
- 共享连接、本体数据源、知识绑定。
- workspace observability。
- MCP 管理。

L3 个人：

- 个人偏好、主题。
- 用户长期记忆。
- 账号安全和改密。

- [ ] 删除 `ConfigCenter.jsx` 中已被本地新组件替代的重复表单实现。
- [ ] 更新 `permissions.js`，前端只做 UI 门控，后端始终为强制点。

#### M7-5 前端验证

```bash
cd consumer/frontend
npm ci
npm run build
npx eslint src/redesign src/components src/hooks src/utils
```

关键 E2E：

- 登录、refresh、强制改密、注册。
- workspace 可见性、owner 成员管理、403 自愈。
- 新会话、运行中切页、刷新重连、等待交互恢复、停止。
- 知识包完整生命周期。
- 两种数据源模式、Graph 查询。
- MCP token 生命周期。
- 记忆和进化审批。

视觉验收：至少保存 1440×900 的 Chat、知识包四页签、L1/L2/L3 配置、登录/注册截图；页面必须使用远端设计 token，不能出现本地旧 UI 风格孤岛。

建议拆成提交：

- `feat(frontend): rebase the remote console on resumable session hooks`
- `feat(frontend): restyle latest knowledge capabilities in the remote system`
- `feat(frontend): unify global workspace and personal settings`

### Phase 8：SDK、配置、部署和数据迁移

#### M8-1 SDK 和 API 文档

- [ ] SDK 保留 `X-Workspace-Id`、session/run 续传能力。
- [ ] 增加 refresh token 使用说明，不把 refresh token 暴露给非浏览器 SDK，除非其明确需要。
- [ ] 记录 MCP 服务地址、Token scope、Graph provider 配置。
- [ ] 更新 API 文档中的 auth、workspace、runs、data source、MCP、Trace schema。

#### M8-2 部署

- [ ] consumer compose 中 auth-server 为必备 healthy dependency，不发布宿主端口。
- [ ] 合并本地最新 backend/frontend build 与远端 auth/MCP 端口、镜像和 volume；MCP 作为 7488 独立进程默认启动并共享必需的 runtime volume。
- [ ] 发布包同时包含 backend、frontend、auth-server 镜像。
- [ ] backup/rollback 覆盖 `.onto-runtime`、`.onto-auth`、MCP token store、配置、Graph migration 状态。
- [ ] 首次迁移仅在 `.onto-auth/users.json` 不存在时复制旧 users.json，保留原文件备份并设 0600。
- [ ] 备份 auth-server `keys/`；丢失私钥会使所有 access/refresh token 立即失效。
- [ ] preflight 检查 auth health、issuer/audience、admin token、MCP 7488 health/secret、Neo4j/HugeGraph 配置。
- [ ] 根 `deploy/` 取消 auth-server “可选”表述，与 consumer 当前必备语义对齐。
- [ ] 统一 `AUTH_USER_MAX_COUNT` 与本地 `USER_MAX_COUNT`，或在 compose 中显式映射，禁止静默采用不同默认值。

#### M8-3 认证与 workspace 数据迁移

必须按以下顺序执行，禁止复用远端当前“先复制、后停写”的迁移窗口：

1. [ ] 停止旧 backend 对 users/workspace 的写入。
2. [ ] 同时备份 `.onto-runtime/users.json`、`.onto-runtime/workspaces`、`.onto-auth/users.json` 和 `.onto-auth/keys/`。
3. [ ] 若仅存在本地 users 文件，复制到 `.onto-auth/users.json`；若仅存在远端 auth 文件，直接使用远端文件。
4. [ ] 两份 users 文件同时存在且内容不同时立即失败，生成字段级差异报告，禁止静默覆盖任一侧。
5. [ ] 校验 username、role、密码哈希、强制改密和 `token_version`；缺失 `token_version` 按 1 兼容。
6. [ ] 启动 auth-server，验证 JWKS、issuer、audience，再启动 consumer backend。
7. [ ] 旧 HMAC access token 不再接受，用户重新登录；保持 `sub=username`，本地 session 路径无需搬迁。
8. [ ] 存量无 owner workspace 不自动私有化，输出公共空间审计报告供管理员处理。
9. [ ] 运行 owner/member 孤儿关系审计和 reconciliation，确认无同名账号权限继承风险。

完成条件：原有账号可用原密码登录、私钥重启后保持、workspace 可见性不扩大、迁移脚本 `--dry-run` 与重复执行结果一致。

建议提交：`chore(deploy): ship auth Graph MCP and the latest consumer together`

## 7. Agent 执行批次与串行文件

完整迁移由 Codex 负责。Codex 可以在内部使用子 Agent 并行分析或实现，最终由根 Agent 统一审查、集成、测试和提交，不需要用户组织开发人员。

Phase 1 完成后按以下批次执行：

| 批次 | 范围 | 内部并行 | 集成前依赖 |
|---|---|---|---|
| A 认证授权 | `auth-server`、`api/auth`、`workspace`、workspace routes | 是 | Phase 1 |
| B Jiuwen/Trace | `agent/core/jiuwen`、sub_agents、streaming、observability | 是 | Phase 1 |
| C Data/Graph | business_data、graph、ontology data source | 是 | Phase 1 |
| D 前端设计系统 | `frontend/src/redesign`、纯 UI primitives | 是 | Phase 1 API 契约冻结 |

MCP 依赖 A+B+C；前端功能接线依赖 A、Trace、Data API 契约。部署收口依赖全部批次。根 Agent 每合入一个批次都必须运行该阶段回归，再开始下游接线。

以下冲突热点由根 Agent 串行修改，子 Agent 不直接并发写入：

- `consumer/backend/app.py`
- `consumer/backend/requirements.txt`
- `consumer/backend/api/routes/__init__.py`
- `consumer/backend/agent/core/orchestrator.py`
- `consumer/backend/agent/tools/registry.py`
- `consumer/frontend/src/redesign/AppShell.jsx`
- `consumer/frontend/src/redesign/ConfigCenter.jsx`
- compose 和 `.env.example`

## 8. 最终验收门禁

### 8.1 静态门禁

```bash
! rg -n "from agentic|import agentic" consumer/backend
! rg -n "LocalAuthBackend|AUTH_MODE=local|dev-mcp-secret-change" consumer auth-server
git diff --exit-code "$TARGET_SHA" -- modeling evaluation common examples
git diff --check
```

预期：

- 生产代码不存在旧 `agentic` import。
- 不存在 local auth 运行路径。
- 生产配置不存在 MCP 默认 secret。
- 当前远端 modeling/evaluation/common/examples 内容未被 consumer 迁移覆盖。
- 无冲突标记、空白错误和意外生成物。

### 8.2 自动化测试

```bash
cd auth-server
.venv/bin/python -m pytest tests -q

cd ../consumer
PYTHONPATH=backend ./venv/bin/python -m pytest backend/tests -q

cd frontend
npm run build
npm run lint
```

### 8.3 必须完成的真实链路

1. 浏览器登录 → access 过期 → refresh → 原请求只重试一次并成功。
2. 改密/revoke 后旧 refresh 立即失效，旧 access 在短 TTL 内按设计失效。
3. user A 无法通过 header、URL、session id、run id、ACP、MCP 访问 user B 的 workspace。
4. Jiuwen 主 Agent、子 Agent、交互暂停、取消和 queued replay 全通过。
5. Neo4j/HugeGraph 两套后端执行相同图工具矩阵，workspace scope 不串数据。
6. MCP search 14 工具和 chat 工具全部可调用，吊销 token 立即拒绝。
7. 远端 Trace 树在实时、历史、重连三种场景结构一致。
8. v0.1 和当前知识包均可导入、浏览和执行。
9. 用户记忆、operation/skill evolution、本体图、模型配置和 workspace observability 可从远端风格 UI 访问。
10. 发布、升级、备份、回滚后 auth 用户、签名密钥、MCP token、runtime 数据完整。
11. DataAgent 的重试、artifact fallback、自然语言与 SQL 结果归一化在真实/模拟服务上通过。
12. A2A、ACP 的发现、运行、取消、续跑都进入同一 Jiuwen orchestrator，并执行 workspace 授权。

## 9. 回滚策略

- 合入前保留远端 `develop@1d07c1ee` 可部署制品。
- 每个 Phase 形成独立 commit，不做跨 Phase 混合提交。
- 数据迁移脚本必须支持 `--dry-run`、幂等执行和迁移报告。
- 执行用户、workspace、binding、Graph scope 迁移前分别备份：
  `.onto-runtime`、`.onto-auth`、`ontology-bindings.json`、Graph 数据或 scope 清单。
- 代码回滚时同时使用对应版本的 backend、frontend、auth-server 镜像，禁止只回滚其中一个。
- 如果回滚到旧 consumer，保留迁移前 users.json；不得让旧 consumer 写新 auth-server 数据目录。

## 10. 责任边界与用户介入点

### 10.1 Codex 独立负责

以下工作不需要用户或其他开发人员参与：

- 双端代码和提交差异分析、能力台账、路径映射。
- 后端目录迁移、Jiuwen、auth、Graph、DataAgent、MCP、Trace 的实现与集成。
- 远端风格前端融合和本地新增功能重绘。
- 单元测试、集成测试、前端构建、本地 Docker E2E、性能与安全回归。
- 数据迁移脚本、`--dry-run`、备份/回滚脚本和迁移报告。
- 阶段性 commit、文档和最终交付清单。
- 普通代码冲突、接口适配和测试失败的分析与修复。

### 10.2 仅以下门禁需要用户介入

| 门禁 | 触发时机 | 需要用户提供或确认 | Codex 在此之前的处理 |
|---|---|---|---|
| U1 工作区写权限 | 开始修改 `/Users/example/vscodePo/meta-wise` | 将该目录打开/加入当前 Codex 可写 workspace | 已完成 clone、分支和只读分析；不改目标代码 |
| U2 真实外部服务 | Neo4j、HugeGraph、DataAgent、MindsDB、LLM、Web Search、Langfuse、onto-platform 真实 E2E | 测试环境地址、账号/密钥，以及允许产生费用的确认 | 先用单测、mock 和本地容器完成可验证部分 |
| U3 用户数据冲突 | `.onto-runtime/users.json` 与 `.onto-auth/users.json` 同时存在且内容不同 | 指定账号、角色、密码记录的权威来源或批准字段级合并结果 | 生成脱敏差异报告，禁止自行覆盖 |
| U4 workspace 业务归属 | 存量公共 workspace、无效 owner/member 或 owner 已删除 | 指定新的 owner、成员和是否转为私有 | 生成审计清单；不扩大、不猜测权限 |
| U5 生产切换 | 停写、备份、数据迁移、替换服务和回滚演练 | 批准维护窗口、目标环境和备份位置 | 完成脚本、dry-run、预检和回滚验证 |
| U6 发布 develop | 本地迁移分支全部验收通过 | 授权 push/创建 PR/合入 `develop`，并提供有效 Git 凭据 | 保持本地分支可复核，不自行发布 |

除 U1-U6 外，Codex 不暂停等待人员分工。到达门禁时必须提交证据、风险和一个明确问题；未受该门禁影响的工作继续推进。

## 11. Agent 最终完成定义

执行 Agent 只有在以下条件全部满足时，才能声明迁移完成：

- C01-C30 全部有自动化或明确的真实环境验收证据。
- 所有远端新增能力在新结构中有唯一归属和测试。
- 本地最新 backend/frontend 能力没有功能回退。
- auth-server 是唯一身份源，workspace authz 在 consumer 强制。
- Jiuwen 是唯一主/子 Agent loop。
- 前端所有活动页面使用远端设计系统。
- full backend test、auth-server test、frontend build/lint 和真实服务矩阵通过。
- 部署、升级、备份、回滚演练完成。
- 代码中不存在临时兼容层、TODO fallback、跳过的能力测试或明文 secret。
