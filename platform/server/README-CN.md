# Agentic Backend 中文说明

这是 Onto Agent 的 Flask 后端，负责认证接口、Agent 运行流式接口、聊天/执行状态持久化、本体与技能资源管理，以及带工具调用能力的通用 Agent Loop。后端通过 `agent.v2` SSE 协议向前端持续输出模型 token、工具状态、子 Agent 事件和轮次终态。

运行时本体、技能和工作区资源默认放在仓库根目录的 `.onto-runtime/` 下：

```text
<repo>/.onto-runtime/
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
├── users.json
└── workspace-settings.json
```

## 当前能力

- 主 ReAct Loop 通过 `agentic/loop/jiuwen_adapter.py` 固定使用 Jiuwen 适配层
- 统一工具注册与调度，工具定义集中在 `agentic/tools/registry.py`
- 支持并发安全工具批处理，以及 `context -> search -> execute` 的阶段化工具执行
- 支持流式 LLM 响应中的工具调用组装与执行
- 支持 `read_documents` 结构化账本，用于约束本体检索前置阅读条件
- 支持 `ontology_context` 本体定义读取、`skill` 技能说明读取、`business_data_search` 业务数据检索、可选 `web_search` 联网搜索和 `execute_script` 脚本流程执行
- 支持统一 `memory` 工具，用于显式记忆沉淀，并在后台路由到普通记忆或 operation evolution
- 支持 `spawn_sub_agent` 批量子任务分解、并发执行和嵌套深度限制
- 支持技能 CRUD、本体文件管理、本体导入、工作区管理、工作区运行配置和系统配置管理
- 支持 workspace 级业务数据连接、provider schema 查询，以及本体与数据连接绑定
- 提供 A2A 和 ACP 适配接口，便于外部系统按协议调用同一个编排器和运行时
- 支持本地性能分析，以及通过系统配置启用的观测导出

## 项目结构

策略 A 保持 Python 包名仍为 `agentic`，源码目录先按领域拆开。`platform/server/agentic/__init__.py` 会把 runtime 与 platform 下的 `agentic` 片段加入包搜索路径。

```text
platform/server/
├── agentic/                  # Flask app factory 与 HTTP routes 包锚点
├── run.py                    # 本地 Flask 启动入口
├── wsgi.py                   # gunicorn/生产入口
├── server_config.py          # host、port、debug、请求限制
├── requirements.txt
└── Dockerfile

runtime/
├── agent/agentic/            # Orchestrator、Jiuwen loop、prompts、services、sub_agents
├── tools/agentic/            # 工具注册、工具执行、工具事件
├── data_search/agentic/      # business_data_search、MindsDB、NL2SQL、图检索 provider
├── direct_execution/agentic/ # 脚本型 operation 直执行
├── memory/agentic/           # memory 与 operation evolution 运行期能力
└── web_search/agentic/       # 外部网页检索 provider

platform/
├── config/agentic/           # system_config、ops_config
├── workspace/agentic/        # workspace 与 .onto-runtime 路径管理
├── auth/agentic/             # 用户、认证、密钥处理
├── stores/agentic/           # ChatStore、RunStore、ExecutionStore
├── protocols/agentic/        # ACP、A2A 适配
└── observability/agentic/    # RuntimeEvent、exporter、本地性能分析

modeling/skills/skill_parser/ # 本体技能文件解析
tests/backend/                # 后端 pytest 回归测试
```

## 本地运行

在 `platform/server/` 目录下运行：

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

说明：

- `AUTH_USERS_FILE` 默认是 `<repo>/.onto-runtime/users.json`。如果文件不存在，后端会自动初始化默认用户文件。
- `PROJECT_ROOT` 默认指向仓库根目录的 `.onto-runtime`，这是运行时根目录。
- `system_config.json` 默认保存在 `<repo>/.onto-runtime/config/system_config.json`；旧的 `<repo>/backend/config/system_config.json` 会在新文件不存在时自动迁移。
- 运维规格配置默认读取 `<repo>/.onto-runtime/config/ops_config.json`，也可通过 `OPS_CONFIG_PATH` 指定。
- 本体资源优先按工作区解析：`<repo>/.onto-runtime/workspaces/<workspace_id>/ontologies`。
- 技能资源优先按工作区解析：`<repo>/.onto-runtime/workspaces/<workspace_id>/skills`，然后回退到 `<repo>/.onto-runtime/system/skills`。
- 服务器 host、port 和 debug 配置在 [`server_config.py`](server_config.py)。

## 测试

推荐在 `platform/server/` 目录下执行：

```bash
PYTHONPATH=.:../../modeling/skills venv/bin/pytest ../../tests/backend
```

也可以使用 Python 模块方式：

```bash
PYTHONPATH=.:../../modeling/skills venv/bin/python -m pytest ../../tests/backend
```

如果从仓库根目录运行，可使用：

```bash
PYTHONPATH=platform/server:modeling/skills ./venv/bin/python -m pytest tests/backend
```

## 配置

复制 [`platform/server/.env.example`](.env.example) 为 `.env` 后再按环境修改。

后端配置来源优先级：

```text
先读已保存的 system_config.json；某个字段没有保存值时再读环境变量；最后用内置默认值
```

消费智能体规格限制使用独立运维配置，读取优先级为：

```text
先读 ops_config.json；某个字段没有保存值时再读环境变量；最后用内置默认值
```

重要变量：

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `LLM_BASE_URL` | 主 OpenAI-compatible 模型接口地址 | 空 |
| `LLM_MODEL` | 主模型 ID | 空 |
| `LLM_API_KEY` | 主模型 API key | 无 |
| `LLM_TIMEOUT` | 主模型超时时间，单位秒 | `600` |
| `LLM_MAX_TOKENS` | 主模型最大输出 token | `4096` |
| `LLM_TEMPERATURE` | 主模型 temperature | `0.7` |
| `TASK_LLM_BASE_URL` | 可选轻量任务模型地址，用于标题和推理摘要 | 回退到 `LLM_BASE_URL` |
| `TASK_LLM_MODEL` | 可选轻量任务模型 ID | 回退到 `LLM_MODEL` |
| `TASK_LLM_API_KEY` | 可选轻量任务模型 key | 回退到 `LLM_API_KEY` |
| `TASK_LLM_TIMEOUT` | 轻量任务模型超时时间，单位秒 | `600` |
| `SYSTEM_CONFIG_PATH` | 可选系统配置文件路径 | `<repo>/.onto-runtime/config/system_config.json` |
| `OPS_CONFIG_PATH` | 可选运维规格配置文件路径 | `<repo>/.onto-runtime/config/ops_config.json` |
| `PROJECT_ROOT` | 包含 `system/` 和 `workspaces/` 的运行时根目录 | `<repo>/.onto-runtime` |
| `AUTH_USERS_FILE` | 用户凭据文件 | `<repo>/.onto-runtime/users.json` |
| `AUTH_SECRET` | JWT 签名密钥 | 未设置时使用开发默认值 |
| `AUTH_TOKEN_TTL` | JWT 有效期，单位秒 | `43200` |
| `SECRET_ENCRYPTION_KEY` | API key / 业务数据连接 secret 落盘加密根密钥，base64 编码 32 字节 | 未设置时本地生成 `<PROJECT_ROOT>/config/secret.key` |
| `AGENT_MAIN_LOOP_MAX_ITERATIONS` | 主 Agent 最大迭代次数 | `50` |
| `AGENT_SUB_AGENT_MAX_ITERATIONS` | 子 Agent 最大迭代次数 | `20` |
| `AGENT_SUB_AGENT_MAX_CONCURRENCY` | 单次运行最大并发子 Agent 数 | `6` |
| `AGENT_SUB_AGENT_MAX_DEPTH` | 子 Agent 最大嵌套深度 | `10` |
| `AGENT_PARALLEL_DEFAULT_MAX_WORKERS` | 普通并发工具 worker 数 | `10` |
| `AGENT_CONTEXT_MAX_ROUNDS` | 滑动上下文保留轮数 | `10` |
| `AGENT_CONTEXT_PRUNE_TOOLS` | 更早裁剪的工具名列表，逗号分隔 | 空 |
| `AGENT_LLM_CONNECTION_MAX_RETRIES` | LLM 连接类失败重试次数 | `3` |
| `AGENT_RUN_TPS` | 单进程全局运行请求 TPS | `50` |
| `USER_MAX_COUNT` | 最大用户数量 | `10` |
| `WORKSPACE_MAX_COUNT` | 最大工作空间数量 | `5` |
| `ONTOLOGY_MAX_COUNT_PER_WORKSPACE` | 每个 workspace 最大本体数量 | `10` |
| `ONTOLOGY_OBJECT_MAX_COUNT` | 单本体最大实体数量 | `200` |
| `ONTOLOGY_OPERATION_MAX_COUNT` | 单本体最大流程数量 | `100` |
| `ENTITY_ATTRIBUTE_MAX_COUNT` | 单实体最大属性数量 | `100` |
| `DATA_CONNECTION_CONFIGS_MAX_PER_WORKSPACE` | 每个 workspace 最大数据连接配置数量 | `10` |
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
| `LOG_LEVEL` | 运行日志级别 | 应用默认 |
| `LOG_CONSOLE` | 是否启用控制台日志 | 关闭 |

说明：

- 后端已不再内建默认 LLM 地址和模型 ID。要让对话可用，需要显式配置 `LLM_BASE_URL` 与 `LLM_MODEL`，或先在系统配置中保存至少一个启用的模型。
- 主 ReAct 循环运行在 openJiuwen 上。后端依赖统一通过 `platform/server/requirements.txt` 安装。
- `business_data_search` 使用 workspace 本地的数据连接与 ontology 绑定。请通过 `/agentic/data-connections` 创建 MindsDB 或 NL2SQL HTTP 连接，再通过 `/agentic/ontologies/{ontology_id}/data-binding` 为本体绑定连接。
- `web_search` 是可选联网搜索工具，通过系统设置 UI 或 `web_search` 配置 section 开启；默认关闭，未开启时不会暴露给模型。
- 观测输出通过 `/api/system-config` 或系统设置 UI 配置。`observability.enabled` 控制是否导出事件，`observability.mode` 选择本地性能 JSONL 或 Langfuse；旧的 `LANGFUSE_*` 和 `OBSERVABILITY_*` 环境变量不再作为运行时配置入口读取。
- `observability.mode=local` 时，前端会话性能页和管理员本地观测页只读取当前机器保留的新 schema 本地 perf JSONL，不读取 Langfuse 数据，也不读取旧 direct perf logger 记录。
- 消费智能体规格限制是部署级策略，通过 `.onto-runtime/config/ops_config.json` 或 `OPS_CONFIG_PATH` 管理，不在系统设置页展示或开放给用户编辑；输入/输出 token 硬上限同样由 ops_config 管理；普通 `agent.log` 使用 180 天保留与旧日志 gzip 压缩，`agent_perf` 仍由 `observability.local.retention_days` 独立控制。
- 如果直接接入 GLM，推荐地址示例为 `https://open.bigmodel.cn/api/paas/v4`。

## HTTP 接口

主要路由组：

- `POST /auth/login`
- `GET /auth/me`
- `GET /health`
- `POST /agentic/runs/stream`
- `GET /agentic/runs/<run_id>/status`
- `POST /agentic/runs/<run_id>/cancel`
- `GET/POST/DELETE /agentic/sessions...`
- `GET /agentic/sessions/<session_id>/performance`
- `GET /agentic/performance/runs/<run_id>`
- `GET /api/local-observability/overview`（`admin`）
- `GET /api/local-observability/runs`（`admin`）
- `GET/POST/DELETE /agentic/skills...`
- `GET/PUT/POST/DELETE /agentic/ontologies...`
- `GET/PUT/DELETE /agentic/ontologies/<ontology_id>/data-binding`
- `GET/POST/PATCH/DELETE /agentic/data-connections...`
- `GET/POST/PATCH /agentic/operations...`
- `GET/POST/PATCH/DELETE /agentic/workspaces...`
- `GET/PATCH /agentic/workspaces/<workspace_id>/runtime-config`
- `GET/PATCH/POST /api/system-config...`
- `GET/POST/DELETE /a2a/tasks...`
- `GET /.well-known/a2a/agent-card.json`
- `GET /acp/agent`
- `GET/POST/DELETE /acp/runs...`

完整请求与响应示例见 [`docs/api.md`](../../docs/api.md)。

## 流式协议

Agent 工作流使用 SSE：

```text
event: agent.v2
data: { ... }
```

JSON envelope 使用：

```json
{
  "protocol": "agent-stream.v2",
  "event_id": "<run_id>:<seq>",
  "seq": 1,
  "run_id": "...",
  "session_id": "...",
  "turn_id": 1,
  "type": "<event_type>",
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
docker compose -f ../../docker-compose.yml up --build
```

仅后端：

```bash
docker compose -f docker-compose.yml up --build
```

后端容器内部监听端口为 `5055`。

相关文件：

- 完整前后端 Compose：[`../../docker-compose.yml`](../../docker-compose.yml)
- 后端 Compose：[`docker-compose.yml`](docker-compose.yml)
