# Agentic Backend 中文说明

这是 Onto Agent 的 FastAPI 后端，负责认证接口、Agent 运行流式接口、聊天/执行状态持久化、本体与技能资源管理，以及带工具调用能力的通用 Agent Loop。后端通过强类型 SSE 公共事件向前端持续输出模型 token、工具状态、子 Agent 事件和轮次终态。

运行时本体、技能和工作区资源默认放在 `consumer/` 应用目录的 `.onto-runtime/` 下：

```text
<consumer>/.onto-runtime/
├── config/
│   ├── system_config.json
│   └── ops_config.json
├── system/
│   └── skills/
├── workspaces/
│   └── <workspace_id>/
│       ├── metadata.json
│       ├── runtime_config.json
│       ├── ontologies/
│       ├── skills/
│       ├── files/
│       └── runtime/
└── workspace-settings.json
```

## 当前能力

- 主 ReAct Loop 通过 `agentic/loop/jiuwen_adapter.py` 固定使用 Jiuwen 适配层
- 统一工具注册与调度，工具定义集中在 `agentic/tools/registry.py`
- 支持并发安全工具批处理，以及 `context -> search -> execute` 的阶段化工具执行
- 支持流式 LLM 响应中的工具调用组装与执行
- 支持 `read_documents` 结构化账本，用于约束本体检索前置阅读条件
- 支持 `ontology_context` 本体定义读取、`skill` 技能说明读取、`business_data_search` 业务数据检索、可选 `web_search` 联网搜索和 `execute_script` 脚本流程执行
- 普通 Agent 脚本支持[生成标识声明、当前请求证据校验与结构化失败原因](../../docs/script-argument-evidence.md)
- 支持统一 `memory` 工具，用于显式记忆沉淀，并在后台路由到普通记忆或 operation evolution
- 支持 `spawn_sub_agent` 批量子任务分解、并发执行和嵌套深度限制
- 支持技能 CRUD、本体文件管理、本体导入、工作区管理、工作区运行配置和系统配置管理
- 支持 provider schema 查询，以及每个本体独立的 workspace 本地业务数据源配置
- 提供 A2A 和 ACP 适配接口，便于外部系统按协议调用同一个编排器和运行时
- 支持本地性能分析，以及通过系统配置启用的观测导出

## 项目结构

```text
backend/
├── app.py                    # FastAPI factory 与 ASGI lifespan
├── run.py                    # 单 worker Uvicorn 开发入口
├── agent/
│   ├── core/                 # Jiuwen ReAct 引擎与编排逻辑
│   ├── runtime/              # RuntimeContainer、Manager、EventHub、Supervisor
│   ├── tools/                # 工具注册、执行与业务数据 adapter
│   ├── sub_agents/           # 子 Agent fan-out/fan-in
│   └── memory/               # 记忆后端与上下文构造
├── api/
│   ├── routes/               # FastAPI 业务路由
│   ├── protocols/            # A2A、MCP、ACP adapter
│   └── auth/                 # 认证与授权
├── persistence/              # Chat 与 Turn checkpoint 持久化
├── integration/              # 观测、平台与 sandbox 集成
├── workspace/                # 工作空间解析与 ACL
├── config/                   # 应用、Agent 与系统配置
├── migrations/               # forward-only 启动/部署迁移
└── tests/                    # pytest 回归测试
```

## 本地运行

在 `backend/` 目录下运行：

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
python run.py
```

默认服务地址：

```text
http://localhost:5055
```

### MCP 服务

MCP 服务作为独立 FastMCP 进程运行。安装依赖后在 `consumer/backend` 目录启动：

```bash
PYTHONPATH=. python -m api.protocols.mcp.server --transport both --host 0.0.0.0 --port 7488
```

Docker Compose 会启动独立 `mcp` 容器，复用后端镜像和 `.onto-runtime` 挂载。FastAPI 主服务提供 `/agentic/mcp/*` 管理 API。每个工作空间持久化一个随机 `mcp_endpoint_id`，独立进程暴露 `/{mcp_endpoint_id}/search/mcp`、`/{mcp_endpoint_id}/chat/mcp`、`/{mcp_endpoint_id}/search/sse` 和 `/{mcp_endpoint_id}/chat/sse`。完整 URL 通过 `GET /agentic/mcp/runtime/status` 获取；旧固定路由不再提供。Search 工具必须显式传入 `ontology_id`，只执行图检索原子工具；Chat 会重新校验 MCP Token、工作空间 ACL 和当前请求 URL Endpoint 绑定，再进入统一 Jiuwen workflow 和 admission queue。Chat 可通过 `stream=true` 和 MCP Progress Token 获取脱敏的粗粒度执行进度；普通用户的流式 Chat 并发默认是当前工作空间 5 个、可由平台管理员在 MCP 接入页调整为 1–32，管理员保持平台运行上限。MCP transport session 同时绑定到首次使用的 Endpoint，不能跨工作空间 URL 复用；Streamable HTTP Session 默认空闲 30 分钟后失效，SSE 断开时释放绑定，每个服务 transport 固定最多保留 10000 个活动 Session。服务重启后客户端需要重新 initialize。

说明：

- 认证统一由独立 auth-server 提供：必须配置 `AUTH_SERVER_URL`（Docker 中为 `http://auth-server:5056`，本地运行 auth-server 时如 `http://localhost:5056`），未配置时后端启动直接报错。
- `PROJECT_ROOT` 默认指向 `consumer/.onto-runtime`，这是运行时根目录。
- `system_config.json` 只从 `<consumer>/.onto-runtime/config/system_config.json` 读取；文件不存在时使用环境变量和内置默认值，首次保存配置时创建该文件。
- 运维规格和日志级别配置默认读取 `<consumer>/.onto-runtime/config/ops_config.json`，也可通过 `OPS_CONFIG_PATH` 指定。`logging.level` 缺省时为 `INFO`；运行时修改后，由下一次日志调用触发重读，无需重启。
- 本体资源优先按工作区解析：`<consumer>/.onto-runtime/workspaces/<workspace_id>/ontologies`。
- 技能资源优先按工作区解析：`<consumer>/.onto-runtime/workspaces/<workspace_id>/skills`，然后回退到 `<consumer>/.onto-runtime/system/skills`。
- 服务器 host、port 和 debug 配置在 [`server_config.py`](server_config.py)。

## 测试

完整后端测试还会调用独立认证和 Solver 服务。先在测试虚拟环境执行 `python -m pip install -r requirements-test.txt`，安装已有服务的测试依赖。测试夹具为 MCP 等认证边界提供独立的合成配置，不依赖真实凭据。

建议从干净 Git 检出执行全量测试，虚拟环境放在代码树之外，避免本地 `.env`、运行数据或第三方依赖目录影响测试收集和静态检查。

以下示例在 `backend/` 目录下创建独立测试环境并执行：

```bash
python -m venv /tmp/knowledge-consumer-tests
/tmp/knowledge-consumer-tests/bin/python -m pip install -r requirements-test.txt
PYTHONPATH=. /tmp/knowledge-consumer-tests/bin/python -m pytest tests
```

如果从 `consumer/` 目录运行，可使用：

```bash
PYTHONPATH=backend /tmp/knowledge-consumer-tests/bin/python -m pytest backend/tests
```

## 配置

复制 [`backend/.env.example`](.env.example) 为 `.env` 后再按环境修改。

后端配置来源优先级：

```text
先读已保存的 system_config.json；某个字段没有保存值时再读环境变量；最后用内置默认值
```

消费智能体规格限制和运行时日志级别使用独立运维配置，读取优先级为：

```text
先读 ops_config.json；某个字段没有保存值时再读环境变量；最后用内置默认值
```

日志级别配置示例：

```json
{
  "logging": {
    "level": "DEBUG"
  }
}
```

后端和 MCP 进程会在下一次日志级别检查时分别检查文件是否变化，变化后才重新解析。无效 JSON 或无效级别会保留上一次有效值。建议通过“写临时文件后原子替换”的方式更新该文件。

重要变量：

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `LLM_BASE_URL` | 主 OpenAI-compatible 模型接口地址 | 空 |
| `LLM_MODEL` | 主模型 ID | 空 |
| `LLM_API_KEY` | 主模型 API key | 无 |
| `LLM_TIMEOUT` | 主模型超时时间，单位秒 | `600` |
| `LLM_MAX_TOKENS` | 主模型最大输出 token | `32768` |
| `LLM_TEMPERATURE` | 主模型 temperature | `0.7` |
| `TASK_LLM_BASE_URL` | 可选轻量任务模型地址，用于标题和推理摘要 | 回退到 `LLM_BASE_URL` |
| `TASK_LLM_MODEL` | 可选轻量任务模型 ID | 回退到 `LLM_MODEL` |
| `TASK_LLM_API_KEY` | 可选轻量任务模型 key | 回退到 `LLM_API_KEY` |
| `TASK_LLM_TIMEOUT` | 轻量任务模型超时时间，单位秒 | `600` |
| `SYSTEM_CONFIG_PATH` | 可选系统配置文件路径 | `<consumer>/.onto-runtime/config/system_config.json` |
| `OPS_CONFIG_PATH` | 可选运维规格配置文件路径 | `<consumer>/.onto-runtime/config/ops_config.json` |
| `CHAT_STORAGE_DIR` | 会话 JSON 存储目录；release 默认落到 `<PROJECT_ROOT>/data/chats` | `<PROJECT_ROOT>/data/chats` |
| `EXECUTION_STORAGE_DIR` | 执行记录存储目录；release 默认落到 `<PROJECT_ROOT>/data/executions` | `<PROJECT_ROOT>/data/executions` |
| `PROJECT_ROOT` | 包含 `system/` 和 `workspaces/` 的运行时根目录 | `<consumer>/.onto-runtime` |
| `AUTH_SERVER_URL` | 独立 auth-server 地址（必填，未配置后端启动报错），如 `http://auth-server:5056` | 空 |
| `AUTH_SERVER_ADMIN_TOKEN` | auth-server Admin API 共享管理令牌（用户管理必填） | 空 |
| `AUTH_BOOTSTRAP_ADMIN_PASSWORD` | 首次创建 auth-server 用户文件时使用的管理员初始强密码 | 空 |
| `AUTH_SERVER_AUDIENCE` | JWT 本地验签 audience | `knowledge-scaffold` |
| `AUTH_SERVER_ISSUER` | JWT issuer，留空 = 使用 `AUTH_SERVER_URL` | 空 |
| `AUTH_USERS_FILE` | 用户凭据文件 | `<consumer>/.onto-runtime/users.json` |
| `AUTH_SECRET` | JWT 签名密钥 | 未设置时使用开发默认值 |
| `AUTH_TOKEN_TTL` | JWT 有效期，单位秒 | `43200` |
| `MCP_SERVER_HOST` | 独立 MCP Server 监听地址；Docker Compose 中 Consumer 后端容器会覆盖为 `mcp` 用于内部探测，MCP 容器会覆盖为 `0.0.0.0` 用于监听 | `0.0.0.0` |
| `MCP_SERVER_PORT` | 独立 MCP Server 端口；Docker Compose 默认将宿主机 `${MCP_SERVER_PORT:-7488}` 映射到容器 `7488` | `7488` |
| `MCP_PUBLIC_BASE_URL` | 外部可访问的协议、域名、端口和可选网关前缀；工作空间 Endpoint ID 和服务路径由系统自动拼接 | 根据 host/port 推导 |
| `MCP_TRANSPORT` | `both` 同时暴露 Streamable HTTP 和 SSE，也可显式选择单一 transport | `both` |
| `MCP_SERVICES_CONFIG_FILE` | 可选 YAML 服务目录路径；相对路径按 `consumer/backend` 解析 | 存在时使用 `config/mcp_services.yaml` |
| `MCP_TOKEN_SECRET` | MCP JWT 签名密钥（必填，至少 32 个字符；不要与其他服务共用） | 无默认值 |
| `MCP_TOKEN_ISSUER` | MCP JWT `iss` claim | `meta-wise` |
| `MCP_TOKEN_AUDIENCE` | MCP JWT `aud` claim | `meta-wise-mcp` |
| `MCP_TOKEN_TTL_SECONDS` | MCP token 默认有效期，单位秒 | `86400` |
| `MCP_TOKEN_STORE_PATH` | MCP 可吊销 token 注册表文件 | `<PROJECT_ROOT>/mcp-tokens.json` |
| `SECRET_ENCRYPTION_KEY` | API key / 业务数据源 secret 落盘加密根密钥，base64 编码 32 字节 | 未设置时本地生成 `<PROJECT_ROOT>/config/secret.key` |
| `AGENT_MAIN_LOOP_MAX_ITERATIONS` | 主 Agent 最大迭代次数 | `50` |
| `AGENT_SUB_AGENT_MAX_ITERATIONS` | 子 Agent 最大迭代次数 | `20` |
| `AGENT_SUB_AGENT_MAX_CONCURRENCY` | 单次运行最大并发子 Agent 数 | `6` |
| `AGENT_SUB_AGENT_MAX_DEPTH` | 子 Agent 最大嵌套深度 | `10` |
| `AGENT_PARALLEL_DEFAULT_MAX_WORKERS` | 普通并发工具 worker 数 | `10` |
| `AGENT_CONTEXT_MAX_ROUNDS` | 滑动上下文保留轮数 | `10` |
| `AGENT_LLM_CONNECTION_MAX_RETRIES` | LLM 连接类失败重试次数 | `3` |
| `AGENT_RUN_TPS` | 单进程全局运行请求 TPS | `50` |
| `WORKSPACE_MAX_COUNT` | 单个用户拥有的最大工作空间数量（保留原配置名） | `50` |
| `ONTOLOGY_MAX_COUNT_PER_WORKSPACE` | 每个 workspace 最大本体数量 | `10` |
| `ONTOLOGY_OBJECT_MAX_COUNT` | 单本体最大实体数量 | `200` |
| `ONTOLOGY_OPERATION_MAX_COUNT` | 单本体最大流程数量 | `100` |
| `ENTITY_ATTRIBUTE_MAX_COUNT` | 单实体最大属性数量 | `100` |
| `DATA_CONNECTION_CONFIGS_MAX_PER_WORKSPACE` | 旧版数据连接数量上限兼容字段，本体私有数据源不使用 | `10` |
| `MODEL_PROFILE_MAX_COUNT` | 最大模型 profile 数量 | `10` |
| `MODEL_INPUT_TOKEN_LIMIT` | 平台输入 token 硬上限 | `50000` |
| `MODEL_OUTPUT_TOKEN_LIMIT` | 平台输出 token 硬上限 | `100000` |
| `SESSION_MAX_PER_USER_WORKSPACE` | 每个 workspace 下每个用户会话保留数量 | `100` |
| `ONTOLOGY_OPERATION_MAX_CHARS` | 单个最终 operation markdown 字符上限 | `15000` |
| `AGENT_LOG_RETENTION_DAYS` | 普通 agent 日志保留天数 | `180` |
| `AGENT_LOG_COMPRESS_OLD` | 是否压缩历史普通日志 | `true` |
| `MEMORY_MODE` | 记忆后端模式 | `filesystem` |
| `MEMORY_WORKSPACE_ROOT` | 文件系统记忆工作区根目录 | 空 |
| `AGENT_MEMORY_RECALL_TOPK` | 记忆召回条数 | `3` |
| `AGENT_MEMORY_RECALL_THRESHOLD` | 记忆召回阈值 | 空 |
| `LOG_LEVEL` | 进程启动日志级别；`ops_config.json` 的 `logging.level` 可覆盖 | `INFO` |
| `LOG_CONSOLE` | 是否启用控制台日志 | 关闭 |

说明：

- 后端已不再内建默认 LLM 地址和模型 ID。要让对话可用，需要显式配置 `LLM_BASE_URL` 与 `LLM_MODEL`，或先在系统配置中保存至少一个启用的模型。
- 主 ReAct 循环运行在 openJiuwen 上。后端依赖统一通过 `backend/requirements.txt` 安装。
- `business_data_search` 根据 `workspace_id + ontology_id` 读取本体私有数据源。通过 `/agentic/ontologies/{ontology_id}/data-source` 保存 `provider + config`；配置存于 workspace 运行目录，不进入本体包。
- `web_search` 是可选联网搜索工具，通过系统设置 UI 或 `web_search` 配置 section 开启；默认关闭，未开启时不会暴露给模型。
- 观测输出按工作空间配置，通过 `/agentic/workspaces/<workspace_id>/observability-config` 或配置中心的“空间观测”管理；每个工作空间继续在本地 OpenTelemetry 记录和 Langfuse 之间选择输出目标。全局系统配置不再提供 `observability` section，旧的 `LANGFUSE_*` 和 `OBSERVABILITY_*` 环境变量也不再作为运行时配置入口读取。
- 工作空间使用 `mode=local` 时，前端会话性能页读取当前工作空间的本地 OpenTelemetry 记录；管理员本地观测页可以跨工作空间聚合和筛选，但只是分析视图，不是全局观测配置。两者都不读取 Langfuse 数据或旧 direct perf logger 记录。使用 `mode=langfuse` 时不新增本地记录，产品内页面只保留已有的本地历史数据。
- 工作空间观测配置是 Speedscope 的唯一控制入口。`enabled=false` 时不写本地 OpenTelemetry、Langfuse 或 Speedscope；开启后，`mode=local` 写本地 OpenTelemetry 记录和会话级 evented trace，`mode=langfuse` 写 Langfuse 和同一会话级 evented trace。Speedscope 文件按工作空间和用户隔离，位于 `<PROJECT_ROOT>/workspaces/<workspace_id>/runtime/observability/speedscope/<user>/`，由 `speedscope.retention_days` 控制保留时间（默认 30 天）。
- Speedscope trace 只包含阶段名称、状态和耗时，不包含问题、Prompt、推理、回答、工具参数、SQL 参数或工具输出。当前会话可通过 `GET /agentic/sessions/{session_id}/performance/speedscope` 下载；普通用户只能下载自己的会话，工作空间管理员可以下载空间内任意用户的会话。
- Speedscope 文件只保存阶段/调用名称、状态、时间关系和模型/工具等安全标签，不保存源码文本、用户问题、Prompt、reasoning、回答、工具参数、SQL 参数或工具输出。MCP 请求不携带知识问答会话的 workspace 运行上下文，因此不会写入这些会话 trace。
- 消费智能体规格限制是部署级策略，通过 `.onto-runtime/config/ops_config.json` 或 `OPS_CONFIG_PATH` 管理，不在系统设置页展示或开放给用户编辑；输入/输出 token 硬上限同样由 ops_config 管理；普通 `agent.log` 使用 180 天保留与旧日志 gzip 压缩，`mode=local` 时各工作空间的本地 OpenTelemetry 记录由 `local.retention_days` 独立控制。
- 如果直接接入 GLM，推荐地址示例为 `https://open.bigmodel.cn/api/paas/v4`。

### 认证失败诊断

登录和刷新失败会通过 `auth.diagnostics` 输出一条诊断日志。每次尝试使用服务端生成的 `auth_attempt_id`，跨认证后端和运行时线程池保持一致；该编号只用于日志关联，不来自用户身份或请求头。

- `auth_operation`：`login` 或 `refresh`。
- `auth_stage`：`token_request`、`token_response`、`jwks`、`jwt_validation` 或 `runtime`；未分类异常使用 `authentication`。
- `auth_reason`、`auth_error_type`：代码定义的失败原因和受限异常类别。
- `auth_upstream_status`：可获得的上游 HTTP 状态；网络错误等场景为空。
- `auth_elapsed_ms`：本次认证尝试截至失败的总耗时，包含线程池等待时间。

例如，`jwks / request_failed` 表示公钥拉取失败，`jwks / refresh_throttled` 表示再次拉取受到 5 秒最小间隔限制；它们均可能发生在令牌接口已经成功之后。正常的无效凭据仍返回 401，不产生可用性告警；503 响应保留现有公开提示。

诊断不记录用户名、密码、令牌、Cookie、请求或响应正文、公钥标识、认证服务地址、异常消息或异常堆栈。普通文本和结构化日志均包含上述诊断字段。

## HTTP 接口

主要路由组：

- `POST /auth/login`
- `GET /auth/me`
- `GET /health`
- `POST /agentic/runs`
- `POST /agentic/runs/sync`
- `GET /agentic/runs/<run_id>`
- `GET /agentic/runs/<run_id>/events`
- `POST /agentic/runs/<run_id>/cancel`
- `GET/POST/DELETE /agentic/sessions...`
- `GET /agentic/sessions/<session_id>/performance`
- `GET /agentic/performance/runs/<run_id>`
- `GET /api/local-observability/overview`（`admin`）
- `GET /api/local-observability/runs`（`admin`）
- `GET/POST/DELETE /agentic/skills...`
- `GET/PUT/POST/DELETE /agentic/ontologies...`
- `GET/PUT/DELETE /agentic/ontologies/<ontology_id>/data-source`
- `POST /agentic/ontologies/<ontology_id>/data-source/test`
- `GET /agentic/business-data/provider-schemas`
- `GET/POST/PATCH /agentic/operations...`
- `GET/POST/PATCH/DELETE /agentic/workspaces...`
- `GET/PATCH /agentic/workspaces/<workspace_id>/runtime-config`
- `GET/PATCH/POST /agentic/workspaces/<workspace_id>/observability-config...`
- `GET/PATCH/POST /api/system-config...`
- `GET/POST/DELETE /a2a/tasks...`
- `GET /.well-known/a2a/agent-card.json`
- `GET /acp/agent`
- `GET/POST/DELETE /acp/runs...`

完整请求与响应示例见 [`docs/api.md`](../docs/api.md)。

## 流式协议

每条 SSE 直接使用强类型公共事件名，并以 Run 内序号作为 `id`：

```text
id: 1
event: token.delta
data: {"schema_version":"1.0","seq":1,"event_type":"token.delta",...}
```

`data` 是持久化后的 `PublicEvent`，不再嵌套兼容 envelope：

```json
{
  "schema_version": "1.0",
  "event_id": "<run_id>:<seq>",
  "seq": 1,
  "run_id": "...",
  "session_id": "...",
  "turn_id": 1,
  "event_type": "<event_type>",
  "status": "running",
  "payload": {}
}
```

常见事件类型：

- `turn.started`
- `token.delta`
- `reasoning.summary`
- `tool.started`
- `tool.progress`
- `tool.finished`
- `subagent.started`
- `subagent.delta`
- `subagent.progress`
- `subagent.finished`
- `subagent_group.started`
- `subagent_group.finished`
- `turn.finished`

说明：

- `token.delta` 通过 `payload.channel` 区分 `content` 与 `reasoning`。
- 推理流片段会携带稳定的 `reasoning_block_id`。
- 当上游 LLM 启用流式工具调用时，工具调用会在拿到稳定 `tool_call.id`、合法 JSON 参数，并且 provider 推进到新 `tool_call.index` 或发出 `finish_reason` 后执行。
- assistant message 完整组装后，循环只会补充执行尚未在流式阶段执行过的工具调用。

## Docker

完整前后端：

```bash
docker compose -f ../docker-compose.yml up --build
```

仅后端：

```bash
docker compose -f docker-compose.yml up --build
```

后端容器内部监听端口为 `5055`。

相关文件：

- 完整前后端 Compose：[`../docker-compose.yml`](../docker-compose.yml)
- 后端 Compose：[`docker-compose.yml`](docker-compose.yml)
