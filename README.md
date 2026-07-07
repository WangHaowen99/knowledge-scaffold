该仓库是 Meta Wise 目录重构工作区。原先位于仓库根目录的 Onto Agent 消费智能体应用已移动到 `consumer/`，应用内完整说明见 [`consumer/README.md`](consumer/README.md)。

`consumer/` 是一个面向企业本体与运行时工作区的通用 Agent 系统，提供 React 前端、Flask 后端、工具调用、子 Agent 编排、长期记忆和本体检索能力。

## 项目定位

Onto Agent 的核心目标是把“企业本体”作为 Agent 的运行上下文：本体不只是数据文件，也包含业务对象、操作逻辑、工具约束和工作区资源。系统通过统一的 Agent Loop 消费这些上下文，完成查询、分析、计划分解、并行执行和结果汇总。

主要能力：

- **通用 Agent Loop**：纯 Python ReAct 循环，负责 LLM 推理、工具调用、结果写回和终止判断。
- **工具注册与调度**：统一工具注册表，支持工具访问控制、并发安全工具批处理和分阶段执行。
- **本体与技能工具**：通过 `ontology_context` 读取本体定义，通过 `skill` 按需加载技能说明，通过 `business_data_search` 查询业务数据，通过 `execute_script` 执行已登记脚本流程。
- **子 Agent 编排**：通过 `spawn_sub_agent(tasks=[...])` 分解独立子任务，支持并发 fan-out/fan-in 和嵌套深度限制。
- **长期记忆**：运行前会注入用户级记忆上下文；主 Agent 可通过统一 `memory` 工具显式沉淀用户提供的长期信息。
- **工作区化运行时**：运行资源默认放在 `consumer/.onto-runtime/` 下，按 `system/` 与 `workspaces/` 组织。
- **外部协议适配**：提供 A2A 与 ACP 适配层，复用同一套 `AgenticOrchestrator`、`agent-stream.v2` 事件和工具执行能力。

## 技术栈

- **后端**：Python、Flask、OpenAI-compatible LLM API、Server-Sent Events
- **前端**：React 19、Vite、lucide-react、react-markdown
- **存储**：本地 JSON 文件存储会话、执行记录和用户数据
- **部署**：Docker Compose、nginx 静态前端服务、后端 Flask 服务

## 快速开始

### 1. 准备后端

```bash
cd consumer/backend
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

业务数据检索不再使用全局本体检索 provider。启动后在「工作区 -> 数据连接」中为当前 workspace 创建 MindsDB 或 NL2SQL HTTP 连接，再在「本体绑定」中为每个 ontology 显式绑定一个连接。运行时 `business_data_search` 只使用该 workspace 下当前 ontology 的绑定连接。

### 2. 准备前端

```bash
cd consumer/frontend
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

### 3. 使用 Docker Compose

`consumer/` 目录提供完整前后端编排：

```bash
cd consumer
cp backend/.env.example backend/.env
cp frontend/.env.example frontend/.env
docker compose up --build
```

默认端口：

| 服务 | 地址 |
| --- | --- |
| 后端 | `http://localhost:5055` |
| 前端 | `http://localhost:8888` |

从仓库根目录也可以使用脚本启动指定镜像标签：

```bash
./consumer/run_docker.sh latest all
./consumer/run_docker.sh latest backend
./consumer/run_docker.sh latest frontend
```

## 运行时目录

默认运行时根目录是 `consumer/.onto-runtime/`：

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
├── users.json
└── workspace-settings.json
```

后端通过 `PROJECT_ROOT` 指向运行时根目录。默认值会优先使用 `consumer/.onto-runtime`，容器中映射到 `/app/.onto-runtime`。
系统配置默认保存在 `consumer/.onto-runtime/config/system_config.json`；旧的 `consumer/backend/config/system_config.json` 会在新文件不存在时自动迁移。运维规格配置默认读取 `consumer/.onto-runtime/config/ops_config.json`，也可通过 `OPS_CONFIG_PATH` 指定。

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
- `action/` 可选，放 Python 脚本及脚本运行所需 `.env` 文件；导入后会复制到 `scripts/` 并根据 Python 脚本生成 `scripts-list.md`。脚本顶部可用 `# 工具流程: <操作名>` 绑定对应 `logic/<操作名>.md`，未声明时退回使用脚本名作为 key；脚本参数优先读取顶部 `parameters` / `参数` 注释，未声明时静态解析 `argparse.add_argument(...)`；下划线开头的 helper 和 `.env` 只复制不注册。

- 也支持上传已转换本体包：包含 `manifest.yaml`，并包含 `objects/`、`operations/`、`objects-list.md`、`operations-list.md` 中至少一类内容。

完整模板和字段说明见 [`consumer/docs/ontology-upload-template.md`](consumer/docs/ontology-upload-template.md)。

## 架构概览

一次 Agent 执行从 `POST /agentic/runs/stream` 开始：

```text
POST /agentic/runs/stream
        │
        ▼
AgenticOrchestrator.run()
  ├─ 解析 session 与 memory context
  ├─ 构造 AgenticState
  └─ 后台线程执行 _stream_agent_loop()
             │
             ▼
    JiuwenReactLoopEngine.run()
      1. 构建/刷新 system prompt 与 Jiuwen agent 配置
      2. 注册当前工具到 openJiuwen
      3. 调用 openJiuwen ReAct agent
      4. 通过 Jiuwen 回调记录 LLM 子轮次与工具调用
      5. 工具调用回到 execute_tool_batch() 执行
      6. 写入 tool result、state_updates 与最终输出
      7. 遇到 terminal tool batch、取消或迭代上限时返回
```

核心模块：

| 模块 | 说明 |
| --- | --- |
| `consumer/backend/agentic/orchestrator.py` | 运行生命周期、会话持久化、SSE 事件、取消与失败处理 |
| `consumer/backend/agentic/loop/jiuwen_adapter.py` | 主 Agent ReAct 循环入口，负责 openJiuwen 配置、调用与状态桥接 |
| `consumer/backend/agentic/loop/jiuwen/` | Jiuwen 工具绑定、提示词、轨迹与结果归一化 |
| `consumer/backend/agentic/tools/registry.py` | 工具注册表、schema 暴露、调用权限 |
| `consumer/backend/agentic/tools/executor.py` | 工具批处理执行、并发/串行阶段调度 |
| `consumer/backend/agentic/sub_agents/` | 子 Agent 执行与并发管理 |
| `consumer/backend/agentic/memory/` | 长期记忆后端与上下文构造 |
| `consumer/backend/agentic/business_data/` | workspace 数据连接、provider schema 与 ontology 绑定 |
| `consumer/backend/agentic/direct_execution/` | 可选直执行分支与脚本运行封装 |
| `consumer/backend/agentic/evolution/` | operation evolution 缓冲、补丁生成与审批 |
| `consumer/backend/agentic/a2a/`, `consumer/backend/agentic/acp/` | 外部协议适配层 |
| `consumer/backend/agentic/observability/`, `consumer/backend/agentic/perf_analysis/` | 运行时观测导出与本地性能分析 |
| `consumer/backend/agentic/stores/` | 会话、执行记录和运行状态存储 |
| `consumer/frontend/src/` | React 前端，包含聊天、配置中心、本体管理、运算进化、本地观测与任务过程侧栏 |

## 工具系统

工具通过 `ToolSpec` 注册，并按调用方限制访问。当前主要工具分组：

| 类型 | 工具 | 调用方 |
| --- | --- | --- |
| 常规工具 | `ontology_context`, `skill`, `business_data_search`, `web_search`, `execute_script` | `main`, `subagent`, `acp` |
| 编排工具 | `spawn_sub_agent` | `main`, `subagent`, `acp` |
| 记忆工具 | `memory` | `main` |

> 当前 ACP server 作为协议 adapter 复用 `AgenticOrchestrator.run()`，实际执行路径仍进入主 Agent loop；上表的 `acp` 表示工具声明层支持该 caller，不代表 ACP 有一套独立执行引擎。

## API 与事件流

常用接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/auth/login` | 登录并获取 JWT |
| `GET` | `/auth/me` | 获取当前用户 |
| `GET` | `/health` | 健康检查 |
| `POST` | `/agentic/runs/stream` | 启动或继续一次 Agent 运行，返回 SSE |
| `GET` | `/agentic/runs/<run_id>/status` | 查询运行状态 |
| `POST` | `/agentic/runs/<run_id>/cancel` | 取消运行 |
| `GET` / `POST` | `/agentic/sessions` | 查询或创建会话 |
| `GET` / `DELETE` | `/agentic/sessions/<session_id>` | 获取或删除会话 |
| `GET` | `/agentic/sessions/<session_id>/performance` | 查看当前会话本地性能分析 |
| `GET` | `/api/local-observability/overview` | 管理员查看本机本地观测总览 |
| `GET` / `POST` / `PATCH` / `DELETE` | `/agentic/data-connections...` | workspace 业务数据连接管理 |
| `GET` / `PUT` / `DELETE` | `/agentic/ontologies/<ontology_id>/data-binding` | 本体与业务数据连接绑定 |
| `GET` / `POST` / `PATCH` | `/agentic/operations...` | operation evolution 提案与 staged patch 审核 |
| `GET` / `POST` / `DELETE` | `/agentic/skills...` | 技能管理 |
| `GET` / `PUT` / `POST` | `/agentic/ontologies...` | 本体资源管理 |
| `GET` | `/.well-known/a2a/agent-card.json` | A2A 发现接口 |
| `GET` / `POST` / `DELETE` | `/a2a/tasks...` | A2A 协议任务接口 |
| `GET` | `/acp/agent` | ACP 发现接口 |
| `GET` / `POST` / `DELETE` | `/acp/runs...` | ACP run 接口 |

完整接口示例见 [`consumer/docs/api.md`](consumer/docs/api.md)。

SSE 使用统一事件名：

```text
event: agent.v2
data: { ... }
```

事件 JSON envelope 使用 `protocol = "agent-stream.v2"`。常见事件类型包括：

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

消费智能体规格限制使用独立运维配置，读取优先级为：

```text
先读 ops_config.json；某个字段没有保存值时再读环境变量；最后用内置默认值
```

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
| `AUTH_USERS_FILE` | 用户文件 |
| `AUTH_SECRET` | JWT 签名密钥 |
| `SECRET_ENCRYPTION_KEY` | 可选的 32 字节 base64 加密根密钥，用于加密落盘 API key 和业务数据连接 secret |
| `MEMORY_MODE` | 记忆后端模式，默认 `filesystem` |
| `MEMORY_WORKSPACE_ROOT` | 文件系统记忆工作区根目录 |
| `AGENT_MAIN_LOOP_MAX_ITERATIONS` | 主 Agent 最大迭代次数 |
| `AGENT_SUB_AGENT_MAX_ITERATIONS` | 子 Agent 最大迭代次数 |
| `AGENT_SUB_AGENT_MAX_CONCURRENCY` | 每次运行最大并发子 Agent 数 |
| `AGENT_SUB_AGENT_MAX_DEPTH` | 子 Agent 嵌套最大深度 |
| `AGENT_RUN_TPS` | 单进程全局运行请求 TPS，默认 `50` |
| `USER_MAX_COUNT` | 最大用户数量，默认 `10` |
| `WORKSPACE_MAX_COUNT` | 最大工作空间数量，默认 `5` |
| `ONTOLOGY_MAX_COUNT_PER_WORKSPACE` | 每个 workspace 最大本体数量，默认 `10` |
| `ONTOLOGY_OBJECT_MAX_COUNT` | 单本体最大实体数量，默认 `200` |
| `ONTOLOGY_OPERATION_MAX_COUNT` | 单本体最大流程数量，默认 `100` |
| `ENTITY_ATTRIBUTE_MAX_COUNT` | 单实体最大属性数量，默认 `100` |
| `DATA_CONNECTION_CONFIGS_MAX_PER_WORKSPACE` | 每个 workspace 最大数据连接配置数量，默认 `10` |
| `MODEL_PROFILE_MAX_COUNT` | 最大模型 profile 数量，默认 `10` |
| `MODEL_INPUT_TOKEN_LIMIT` | 平台输入 token 硬上限，默认 `50000` |
| `MODEL_OUTPUT_TOKEN_LIMIT` | 平台输出 token 硬上限，默认 `100000` |
| `SESSION_MAX_PER_USER_WORKSPACE` | 每个 workspace 下每个用户会话保留数量，默认 `100` |
| `ONTOLOGY_OPERATION_MAX_CHARS` | 单个最终 operation markdown 字符上限，默认 `15000` |
| `AGENT_LOG_RETENTION_DAYS` | 普通 agent 日志保留天数，默认 `180` |
| `AGENT_LOG_COMPRESS_OLD` | 是否压缩历史普通日志，默认 `true` |

说明：

- 当前版本后端不再内建默认 LLM 地址和模型 ID。首次启动后如果尚未配置模型，聊天页会提示先完成模型配置。
- 业务数据检索连接按 workspace 存储在运行时目录中，并通过 ontology 绑定选择；系统配置页不再提供全局本体检索 provider。
- 观测输出通过系统设置页 / `.onto-runtime/config/system_config.json` 管理，`observability.enabled` 控制开关，`observability.mode` 选择本地性能日志或 Langfuse；不再读取旧的 `LANGFUSE_*` 或 `OBSERVABILITY_*` 环境变量。
- `observability.mode=local` 时，聊天页的当前会话可打开“性能分析”；管理员可在用户菜单打开“本地观测分析”查看当前机器保留窗口内的整体趋势、慢运行、慢工具和慢运行阶段。该分析只读取新 local perf JSONL，不读取 Langfuse 或旧 direct perf logger 数据。
- 消费智能体规格限制是部署级策略，通过 `.onto-runtime/config/ops_config.json` 或 `OPS_CONFIG_PATH` 管理，不在系统设置页展示或开放给用户编辑；输入/输出 token 硬上限同样由 ops_config 管理；普通 `agent.log` 使用 180 天保留与旧日志 gzip 压缩，`agent_perf` 仍由 `observability.local.retention_days` 独立控制。
- 内置默认值只用于超时、重试、temperature、存储路径等运行参数；模型和业务数据源这类外部能力必须显式配置。
- 如果接入 GLM，推荐地址示例为 `https://open.bigmodel.cn/api/paas/v4`。

前端配置：

| 变量 | 说明 |
| --- | --- |
| `VITE_API_BASE_URL` | 后端 API 地址 |

## 目录结构

```text
.
├── consumer/                 # Onto Agent 消费智能体应用
│   ├── backend/              # Flask 后端与 agentic runtime
│   ├── frontend/             # React + Vite 前端
│   ├── docs/                 # API 与设计文档
│   ├── sdk/                  # Python / TypeScript SDK 相关内容
│   ├── deploy/               # 发布、升级、回滚和预检脚本
│   ├── tools/                # 辅助工具
│   ├── .onto-runtime/        # 本体、技能和工作区运行时资源
│   ├── docker-compose.yml    # 前后端完整 Compose
│   ├── build.sh              # 发布构建入口
│   └── run_docker.sh         # Docker 运行入口
├── evaluation/               # Agent 评测相关代码与前端
├── modeling/                 # 本体建模相关代码
```

## 开发验证

后端测试：

```bash
cd consumer/backend
PYTHONPATH=. venv/bin/pytest tests
```

也可以从仓库根目录运行：

```bash
PYTHONPATH=consumer/backend consumer/backend/venv/bin/python -m pytest consumer/backend/tests
```

前端验证：

```bash
cd consumer/frontend
npm run lint
npm run build
```

## 构建与部署

发布构建入口：

```bash
./consumer/build.sh <tag> [amd|arm] [all|backend|frontend]
```

Docker 运行入口：

```bash
./consumer/run_docker.sh <tag> [all|backend|frontend]
```

更完整的生产部署、升级、备份和回滚流程见 [`consumer/deploy/README.md`](consumer/deploy/README.md)。

## 相关文档

- [`consumer/README.md`](consumer/README.md)：消费智能体应用总览
- [`consumer/backend/README.md`](consumer/backend/README.md)：后端运行时与接口细节
- [`consumer/frontend/README.md`](consumer/frontend/README.md)：前端结构与运行方式
- [`consumer/docs/api.md`](consumer/docs/api.md)：API 请求与响应示例
- [`consumer/docs/consumer-agent-specs.md`](consumer/docs/consumer-agent-specs.md)：消费智能体规格与限制说明
- [`consumer/docs/ontology-upload-template.md`](consumer/docs/ontology-upload-template.md)：用户本体 ZIP 上传 Markdown 模板
- [`consumer/sdk/README.md`](consumer/sdk/README.md)：SDK 说明书
