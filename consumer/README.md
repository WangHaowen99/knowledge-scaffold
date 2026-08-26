该项目是一个面向企业本体与运行时工作区的通用 Agent 系统，提供 React 前端、FastAPI 后端、工具调用、子 Agent 编排、长期记忆和本体检索能力。

本文档描述 `consumer/` 应用。除非特别说明，下面的命令都默认在 `consumer/` 目录执行。

## 项目定位

Onto Agent 的核心目标是把“企业本体”作为 Agent 的运行上下文：本体不只是数据文件，也包含业务对象、操作逻辑、工具约束和工作区资源。系统通过统一的 Agent Loop 消费这些上下文，完成查询、分析、计划分解、并行执行和结果汇总。

主要能力：

- **通用 Agent Loop**：纯 Python ReAct 循环，负责 LLM 推理、工具调用、结果写回和终止判断。
- **工具注册与调度**：统一工具注册表，支持工具访问控制、并发安全工具批处理和分阶段执行。
- **本体与技能工具**：通过 `ontology_context` 读取本体定义，通过 `skill` 按需加载技能说明，通过 `business_data_search` 查询业务数据，通过 `execute_script` 执行已登记脚本流程。
- **子 Agent 编排**：通过 `spawn_sub_agent(tasks=[...])` 分解独立子任务，支持并发 fan-out/fan-in 和嵌套深度限制。
- **长期记忆**：运行前会注入用户级记忆上下文；主 Agent 可通过统一 `memory` 工具显式沉淀用户提供的长期信息。
- **工作区化运行时**：运行资源默认放在 `consumer/` 应用目录的 `.onto-runtime/` 下，按 `system/` 与 `workspaces/` 组织。
- **外部协议适配**：提供 A2A 与 ACP 适配层，复用同一套 `AgenticOrchestrator`、`agent-stream.v2` 事件和工具执行能力。

## 技术栈

- **后端**：Python、FastAPI、OpenAI-compatible LLM API、Server-Sent Events
- **前端**：React 19、Vite、lucide-react、react-markdown
- **存储**：本地 JSON 文件存储会话、执行记录和用户数据
- **部署**：Docker Compose、nginx 静态前端服务、单 worker Uvicorn 后端服务

## 快速开始

### 1. 启动 auth-server

认证统一由独立 auth-server 提供（consumer 与 modeling 共用账号体系），本地开发需先启动它（在仓库根目录执行）：

```bash
cd auth-server
pip install -r requirements.txt
AUTH_SERVER_PORT=5056 python run.py
```

说明：

- `backend/.env` 必须配置 `AUTH_SERVER_URL`（本地开发如 `http://localhost:5056`，容器内为 `http://auth-server:5056`），否则后端启动直接报错。
- auth-server 首次启动使用 `AUTH_BOOTSTRAP_ADMIN_PASSWORD` 创建管理员账号 `admin`，未配置或密码不符合策略时拒绝启动；管理员可直接使用该密码登录。
- 使用 Docker Compose 部署时无需单独启动：auth-server 已是 compose 必备服务，会随 `docker compose up` 一并拉起。

### 2. 准备后端

```bash
cd backend
python -m venv venv
```

激活虚拟环境：

```bash
# Linux / macOS
source venv/bin/activate
# Windows
.\venv\Scripts\activate.bat
```

安装依赖并启动：

```bash
pip install -r requirements.txt
cp .env.example .env
python run.py
```

> **Windows 用户**：如果 `pip install` 报路径过长的错误，需先以管理员身份在 PowerShell 执行以下命令启用长路径支持，然后重新打开终端：
>
> ```powershell
> New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" `
>   -Name "LongPathsEnabled" -Value 1 -PropertyType DWORD -Force
> ```
>
> 如果默认 pip 源不可达，可使用国内镜像：`pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple`

默认后端地址：

```text
http://localhost:5055
```

启动前需要显式配置模型。后端不再内建可运行的模型地址或模型 ID，也不会把示例占位值当成真实默认配置：

```bash
LLM_BASE_URL=<openai-compatible-base-url>
LLM_MODEL=<model-id>
LLM_API_KEY=<api-key>
```

业务数据检索不再使用全局本体检索 provider。启动后在知识包的「数据源」页直接选择 MindsDB、NL2SQL HTTP 等 provider，并填写当前 ontology 专属的配置。运行时 `business_data_search` 只使用当前 workspace + ontology 对应的数据源，不共享连接，也不需要额外绑定。

### 3. 准备前端

```bash
cd frontend
npm install
npm run dev
```

本地开发时 Vite 会自动代理后端 API，无需额外配置。如需自定义后端地址或日志级别，可创建 `.env`：

```bash
# 可选
VITE_API_BASE_URL=http://localhost:5055
VITE_LOG_LEVEL=info
```

默认前端开发地址：

```text
http://localhost:6600
```

### 4. 使用 Docker Compose

应用目录提供完整前后端编排：

```bash
cp backend/.env.example backend/.env
cp frontend/.env.example frontend/.env
docker compose up --build
```

默认端口：

| 服务 | 地址 |
| --- | --- |
| 后端 | `http://localhost:5055` |
| 前端 | `http://localhost:8888` |

也可以使用脚本启动指定镜像标签：

```bash
./run_docker.sh latest all
./run_docker.sh latest backend
./run_docker.sh latest frontend
```

## 运行时目录

默认运行时根目录是 `consumer/` 应用目录下的 `.onto-runtime/`：

```text
.onto-runtime/
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

后端通过 `PROJECT_ROOT` 指向运行时根目录。默认值会优先使用 `consumer/.onto-runtime`，容器中映射到 `/app/.onto-runtime`。
系统配置只从 `.onto-runtime/config/system_config.json` 读取；文件不存在时使用环境变量和内置默认值，首次保存配置时创建该文件。运维规格配置默认读取 `.onto-runtime/config/ops_config.json`，也可通过 `OPS_CONFIG_PATH` 指定。

## 用户本体上传格式

本体 ZIP 上传入口只接受 `.zip` 文件。推荐用户按“源格式包”组织材料，系统会自动转换为运行时本体结构：

```text
my-ontology.zip
└── my-ontology/
    ├── SKILL.md
    ├── object/
    │   └── 实体A.md
    ├── logic/
    │   └── 操作A.md
    └── action/                 # 可选
        └── script_a.py
```

核心要求：

- `SKILL.md` 需要包含 `id`、`name`、`description` frontmatter。
- `object/` 放实体对象定义，每个实体一个 `.md` 文件。
- `logic/` 放业务操作逻辑，每个操作一个 `.md` 文件。
- `action/` 可选，放 Python 脚本及脚本运行所需 `.env` 文件；导入后会复制到 `scripts/`，为可执行脚本生成 `scripts/<key>.md` 描述文件，并更新 `scripts/index.md`。脚本顶部可用 `# 工具流程: <操作名>` 绑定对应 `logic/<操作名>.md`；脚本参数优先读取顶部 `parameters` / `参数` 注释，未声明时静态解析 `argparse.add_argument(...)`；下划线开头的 helper 和 `.env` 只复制不注册。

- 已转换本体包使用 `kind: ontology-package`、`schema_version: 0.1`，目录清单位于各目录的 `index.md`。迁移期仍接受旧包，导入边界会一次性转换；安装目录不会保留 `ONTOLOGY.md` 或 `*-list.md`。

完整模板和字段说明见 [`docs/ontology-upload-template.md`](docs/ontology-upload-template.md)。

## 架构概览

普通消息和 HITL 回答都从 `POST /agentic/runs` 进入。HITL 回答携带
`interaction_response`，创建新的 Run 和 Turn；客户端随后通过 Run 的事件端点
消费 SSE：

```text
POST /agentic/runs
        │
        ▼
ExecutionStore checkpoint + session 单飞准入
        │
        ▼
RuntimeContainer / AsyncRunManager / RunTaskSupervisor
        │
        ▼
AsyncExecutionService → JiuwenAsyncAgentRuntime
        │
        ▼
JiuwenReactLoopEngine.arun()（ASGI lifespan 所有的 Runner）
        │
        ▼
EventHub 持久化后发布 → GET /agentic/runs/{run_id}/events
```

核心模块：

| 模块 | 说明 |
| --- | --- |
| `backend/agent/runtime/runtime.py` | ASGI lifespan 内的 Runner、Supervisor、EventHub 与 bounded offload 所有权 |
| `backend/agent/runtime/manager.py` | Run 单次执行、session 单飞、取消和终态仲裁 |
| `backend/agent/runtime/service.py` | Runtime 事件到持久化 PublicEvent 的执行边界 |
| `backend/agent/runtime/jiuwen.py` | Consumer 契约与 Jiuwen 引擎之间的 native async adapter |
| `backend/agent/core/jiuwen_engine.py` | 主 Agent ReAct 循环和 Jiuwen 工具桥接 |
| `backend/api/routes/workflows.py` | 统一 Run 准入、查询、事件 SSE 与取消 API |
| `backend/agent/memory/` | 长期记忆后端与上下文构造 |
| `backend/agent/tools/business_data/` | ontology 数据源、provider schema 与配置加密 |
| `backend/migrations/` | 部署与本地启动共用的集中入口，以及可独立删除的一次性兼容迁移 |
| `backend/agent/direct_execution/` | 可选直执行分支与脚本运行封装 |
| `backend/agent/evolution/` | operation evolution 缓冲、补丁生成与审批 |
| `backend/api/protocols/` | A2A、MCP 与 ACP 外部协议适配层 |
| `backend/integration/observability/` | 运行时观测导出与本地性能分析 |
| `backend/persistence/` | 会话、Turn checkpoint 和执行记录存储 |
| `frontend/src/` | React 前端，包含聊天、配置中心、本体管理、运算进化、本地观测与任务过程侧栏 |

## 工具系统

工具通过 `ToolSpec` 注册，并按调用方限制访问。当前主要工具分组：

| 类型 | 工具 | 调用方 |
| --- | --- | --- |
| 常规工具 | `ontology_context`, `skill`, `business_data_search`, `web_search`, `execute_script` | `main`, `subagent`, `acp` |
| 编排工具 | `spawn_sub_agent` | `main`, `subagent`, `acp` |
| 记忆工具 | `memory` | `main` |

> A2A、MCP 和 ACP 是同一 `AsyncRunManager` 的协议 adapter，不创建独立执行引擎。

## API 与事件流

常用接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/auth/login` | 登录并获取 JWT |
| `GET` | `/auth/me` | 获取当前用户 |
| `GET` | `/health` | 健康检查 |
| `POST` | `/agentic/runs` | 提交普通消息或 HITL 回答，返回 Run 准入结果 |
| `POST` | `/agentic/runs/sync` | 等待 Run 终态并返回同步兼容 JSON |
| `GET` | `/agentic/runs/<run_id>` | 查询运行状态 |
| `GET` | `/agentic/runs/<run_id>/events` | 按事件序号回放并继续消费 SSE |
| `POST` | `/agentic/runs/<run_id>/cancel` | 取消运行 |
| `GET` / `POST` | `/agentic/sessions` | 查询或创建会话 |
| `GET` / `DELETE` | `/agentic/sessions/<session_id>` | 获取或删除会话 |
| `GET` | `/agentic/sessions/<session_id>/performance` | 查看当前会话本地性能分析 |
| `GET` | `/api/local-observability/overview` | 管理员查看本机本地观测总览 |
| `GET` / `PATCH` / `POST` | `/agentic/workspaces/<workspace_id>/observability-config...` | 读取、保存或测试工作空间观测配置 |
| `GET` / `PUT` / `DELETE` | `/agentic/ontologies/<ontology_id>/data-source` | 本体私有业务数据源配置 |
| `POST` | `/agentic/ontologies/<ontology_id>/data-source/test` | 测试已保存或候选数据源配置 |
| `GET` | `/agentic/business-data/provider-schemas` | 数据源 Provider 表单元数据 |
| `GET` / `POST` / `PATCH` | `/agentic/operations...` | operation evolution 提案与 staged patch 审核 |
| `GET` / `POST` / `DELETE` | `/agentic/skills...` | 技能管理 |
| `GET` / `PUT` / `POST` | `/agentic/ontologies...` | 本体资源管理 |
| `GET` | `/.well-known/a2a/agent-card.json` | A2A 发现接口 |
| `GET` / `POST` / `DELETE` | `/a2a/tasks...` | A2A 协议任务接口 |
| `GET` | `/acp/agent` | ACP 发现接口 |
| `GET` / `POST` / `DELETE` | `/acp/runs...` | ACP run 接口 |

完整接口示例见 [`docs/api.md`](docs/api.md)。

SSE 的 `event` 字段直接使用强类型公共事件名，`id` 是 Run 内单调递增序号：

```text
id: 1
event: token.delta
data: {"schema_version":"1.0","seq":1,"event_type":"token.delta",...}
```

`data` 是持久化后的 `PublicEvent`，不再嵌套兼容 envelope。常见事件类型包括：

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

## 配置说明

后端配置来源优先级：

```text
先读已保存的 system_config.json；某个字段没有保存值时再读环境变量；最后用内置默认值
```

消费智能体规格限制和运行时日志级别使用独立运维配置，读取优先级为：

```text
先读 ops_config.json；某个字段没有保存值时再读环境变量；最后用内置默认值
```

运行时日志级别写在 `ops_config.json` 的 `logging.level`：

```json
{
  "logging": {
    "level": "DEBUG"
  }
}
```

默认级别为 `INFO`。修改文件后，后端和 MCP 进程会在下一次日志级别检查时分别触发重读，无需重启；无效 JSON 或无效级别会保留上一次有效值。建议通过“写临时文件后原子替换”的方式更新该文件。

常用后端变量：

| 变量 | 说明 |
| --- | --- |
| `LLM_BASE_URL` | OpenAI-compatible 主模型地址 |
| `LLM_MODEL` | 主模型 ID |
| `LLM_API_KEY` | 主模型密钥 |
| `TASK_LLM_BASE_URL` / `TASK_LLM_MODEL` / `TASK_LLM_API_KEY` | 可选轻量模型，用于标题、摘要等任务 |
| `WEB_SEARCH_ENABLED` / `WEB_SEARCH_PROVIDER` / `WEB_SEARCH_API_KEY` | 可选联网搜索工具配置，默认关闭 |
| `SYSTEM_CONFIG_PATH` | 可选系统配置文件路径，默认 `.onto-runtime/config/system_config.json` |
| `OPS_CONFIG_PATH` | 可选运维规格配置文件路径，默认 `.onto-runtime/config/ops_config.json` |
| `PROJECT_ROOT` | 运行时根目录，默认 `.onto-runtime` |
| `CHAT_STORAGE_DIR` | 会话 JSON 存储目录 |
| `EXECUTION_STORAGE_DIR` | 执行记录存储目录 |
| `AUTH_SERVER_URL` | 独立 auth-server 地址【必填】，如 `http://auth-server:5056`，未配置后端启动报错 |
| `AUTH_SERVER_ADMIN_TOKEN` | 与 auth-server 共享的管理令牌，用户管理必填 |
| `AUTH_BOOTSTRAP_ADMIN_PASSWORD` | 首次创建 auth-server 用户文件时必填的管理员初始强密码；初始化后可移除 |
| `AUTH_SERVER_AUDIENCE` | JWT 本地验签 audience，默认 `meta-wise` |
| `AUTH_SERVER_ISSUER` | JWT issuer，留空 = 使用 `AUTH_SERVER_URL` |
| `SECRET_ENCRYPTION_KEY` | 可选的 32 字节 base64 加密根密钥，用于加密落盘 API key 和业务数据源 secret |
| `MEMORY_MODE` | 记忆后端模式，默认 `filesystem` |
| `MEMORY_WORKSPACE_ROOT` | 文件系统记忆工作区根目录 |
| `AGENT_MAIN_LOOP_MAX_ITERATIONS` | 主 Agent 最大迭代次数 |
| `AGENT_SUB_AGENT_MAX_ITERATIONS` | 子 Agent 最大迭代次数 |
| `AGENT_SUB_AGENT_MAX_CONCURRENCY` | 每次运行最大并发子 Agent 数 |
| `AGENT_SUB_AGENT_MAX_DEPTH` | 子 Agent 嵌套最大深度 |
| `AGENT_RUN_TPS` | 单进程全局运行请求 TPS，默认 `50` |
| `WORKSPACE_MAX_COUNT` | 最大工作空间数量，默认 `50` |
| `ONTOLOGY_MAX_COUNT_PER_WORKSPACE` | 每个 workspace 最大本体数量，默认 `10` |
| `ONTOLOGY_OBJECT_MAX_COUNT` | 单本体最大实体数量，默认 `200` |
| `ONTOLOGY_OPERATION_MAX_COUNT` | 单本体最大流程数量，默认 `100` |
| `ENTITY_ATTRIBUTE_MAX_COUNT` | 单实体最大属性数量，默认 `100` |
| `DATA_CONNECTION_CONFIGS_MAX_PER_WORKSPACE` | 旧版数据连接数量上限兼容字段；本体私有数据源不使用该限制 |
| `MODEL_PROFILE_MAX_COUNT` | 最大模型 profile 数量，默认 `10` |
| `MODEL_INPUT_TOKEN_LIMIT` | 平台输入 token 硬上限，默认 `50000` |
| `MODEL_OUTPUT_TOKEN_LIMIT` | 平台输出 token 硬上限，默认 `100000` |
| `SESSION_MAX_PER_USER_WORKSPACE` | 每个 workspace 下每个用户会话保留数量，默认 `100` |
| `ONTOLOGY_OPERATION_MAX_CHARS` | 单个最终 operation markdown 字符上限，默认 `15000` |
| `AGENT_LOG_RETENTION_DAYS` | 普通 agent 日志保留天数，默认 `180` |
| `AGENT_LOG_COMPRESS_OLD` | 是否压缩历史普通日志，默认 `true` |

说明：

- 当前版本后端不再内建默认 LLM 地址和模型 ID。首次启动后如果尚未配置模型，聊天页会提示先完成模型配置。
- 业务数据源按 `workspace_id + ontology_id` 存储在运行时目录中；系统配置页不再提供全局本体检索 provider。
- 观测输出按工作空间配置，通过配置中心的“空间观测”或 `/agentic/workspaces/<workspace_id>/observability-config` 管理；每个工作空间可独立关闭观测，或选择本地性能日志和独立 Langfuse 项目。全局系统配置不再提供 `observability` section，也不再读取旧的 `LANGFUSE_*` 或 `OBSERVABILITY_*` 环境变量。
- 工作空间观测配置为 `mode=local` 时，聊天页的当前会话可打开“性能分析”；管理员可在用户菜单打开“本地观测分析”，跨工作空间聚合当前机器保留窗口内的整体趋势，并按工作空间筛选。该页面是聚合分析视图，不是全局观测配置；它只读取新的 workspace local perf JSONL，不读取 Langfuse 或旧 direct perf logger 数据。
- 工作空间观测配置是 Speedscope 的唯一控制入口。`enabled=false` 时不写本地 perf、Langfuse 或 Speedscope；开启后，`mode=local` 和 `mode=langfuse` 都会同时生成当前会话的 evented `.speedscope.json`。文件按工作空间和用户隔离，并由 `speedscope.retention_days` 控制保留时间；当前会话可通过 `/agentic/sessions/{session_id}/performance/speedscope` 下载。
- 消费智能体规格限制是部署级策略，通过 `.onto-runtime/config/ops_config.json` 或 `OPS_CONFIG_PATH` 管理，不在系统设置页展示或开放给用户编辑；输入/输出 token 硬上限同样由 ops_config 管理；普通 `agent.log` 使用 180 天保留与旧日志 gzip 压缩，各工作空间的 `agent_perf` 由其观测模式和 `local.retention_days` 独立控制。
- 内置默认值只用于超时、重试、temperature、存储路径等运行参数；模型和业务数据源这类外部能力必须显式配置。
- 如果接入 GLM，推荐地址示例为 `https://open.bigmodel.cn/api/paas/v4`。

前端配置：

| 变量 | 说明 |
| --- | --- |
| `VITE_API_BASE_URL` | 后端 API 地址 |

## 目录结构

```text
.
├── backend/                  # FastAPI 后端与 native async runtime
│   ├── agent/                # Agent 引擎、runtime、工具、记忆与子 Agent
│   ├── api/                  # FastAPI 路由、鉴权与协议 adapter
│   ├── tests/                # 后端测试
│   ├── .env.example          # 后端环境变量模板
│   └── run.py                # 后端入口
├── frontend/                 # React + Vite 前端
│   ├── src/                  # 前端源码
│   ├── .env.example          # 前端环境变量模板
│   └── package.json
├── docs/                     # API 与设计文档
├── sdk/                      # Python / TypeScript SDK 相关内容
├── deploy/                   # 发布、升级、回滚和预检脚本
├── tools/                    # 辅助工具
├── .onto-runtime/            # 本体、技能和工作区运行时资源
├── docker-compose.yml        # 前后端完整 Compose
├── build.sh                  # 发布构建入口
└── run_docker.sh             # Docker 运行入口
```

## 开发验证

后端测试：

```bash
cd backend
PYTHONPATH=. venv/bin/pytest tests
```

也可以从 `consumer/` 目录运行：

```bash
PYTHONPATH=backend backend/venv/bin/python -m pytest backend/tests
```

前端验证：

```bash
cd frontend
npm run lint
npm run build
```

## 构建与部署

发布构建入口：

```bash
./build.sh <tag> [amd|arm] [all|backend|frontend]
```

Docker 运行入口：

```bash
./run_docker.sh <tag> [all|backend|frontend]
```

更完整的生产部署、升级、备份和回滚流程见 [`deploy/README.md`](deploy/README.md)。

## 相关文档

- [`backend/README.md`](backend/README.md)：后端运行时与接口细节
- [`frontend/README.md`](frontend/README.md)：前端结构与运行方式
- [`docs/api.md`](docs/api.md)：API 请求与响应示例
- [`docs/consumer-agent-specs.md`](docs/consumer-agent-specs.md)：消费智能体规格与限制说明
- [`docs/ontology-upload-template.md`](docs/ontology-upload-template.md)：用户本体 ZIP 上传 Markdown 模板
- [`sdk/README.md`](sdk/README.md)：SDK 说明书
