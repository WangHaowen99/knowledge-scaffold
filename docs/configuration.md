# Configuration

本文档是 Meta Wise 的配置参考，覆盖模型、运行时目录、数据连接、观测和常用环境变量。

## Configuration Sources

后端配置来源优先级：

```text
已保存的 system_config.json
  -> 环境变量
  -> 内置默认值
```

运维规格限制使用独立配置：

```text
ops_config.json
  -> 环境变量
  -> 内置默认值
```

模型地址、模型 ID、业务数据源等外部能力不应该依赖内置默认值，需要显式配置。

## Runtime Directory

默认运行时目录：

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

容器部署时，仓库根目录的 `.onto-runtime` 会挂载到 `/app/.onto-runtime`。

## Model Configuration

最小模型配置：

```bash
LLM_BASE_URL=<openai-compatible-base-url>
LLM_MODEL=<model-id>
LLM_API_KEY=<api-key>
```

可选轻量模型用于标题、摘要等任务：

```bash
TASK_LLM_BASE_URL=<openai-compatible-base-url>
TASK_LLM_MODEL=<model-id>
TASK_LLM_API_KEY=<api-key>
```

如果使用 GLM，地址示例：

```text
https://open.bigmodel.cn/api/paas/v4
```

## Backend Environment Variables

| 变量 | 说明 |
| --- | --- |
| `LLM_BASE_URL` | OpenAI-compatible 主模型地址 |
| `LLM_MODEL` | 主模型 ID |
| `LLM_API_KEY` | 主模型密钥 |
| `TASK_LLM_BASE_URL` / `TASK_LLM_MODEL` / `TASK_LLM_API_KEY` | 可选轻量模型，用于标题、摘要等任务 |
| `WEB_SEARCH_ENABLED` / `WEB_SEARCH_PROVIDER` / `WEB_SEARCH_API_KEY` | 可选联网搜索工具配置，默认关闭 |
| `SYSTEM_CONFIG_PATH` | 可选系统配置文件路径，默认 `.onto-runtime/config/system_config.json` |
| `OPS_CONFIG_PATH` | 可选运维规格配置路径，默认 `.onto-runtime/config/ops_config.json` |
| `PROJECT_ROOT` | 运行时根目录，默认 `.onto-runtime` |
| `CHAT_STORAGE_DIR` | 会话 JSON 存储目录 |
| `EXECUTION_STORAGE_DIR` | 执行记录存储目录 |
| `AUTH_USERS_FILE` | 用户文件路径 |
| `AUTH_SECRET` | JWT 签名密钥 |
| `SECRET_ENCRYPTION_KEY` | 可选 32 字节 base64 加密根密钥，用于加密落盘 API key 和业务数据连接 secret |
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

## Frontend Environment Variables

本地开发通常不需要前端 `.env`，Vite 会代理到默认后端地址。

| 变量 | 说明 |
| --- | --- |
| `VITE_API_BASE_URL` | 后端 API 地址 |
| `VITE_LOG_LEVEL` | 前端日志级别 |

示例：

```bash
VITE_API_BASE_URL=http://localhost:5055
VITE_LOG_LEVEL=info
```

## Business Data Connections

业务数据检索不使用全局 provider。正确流程是：

1. 在当前 workspace 创建数据连接。
2. 选择 provider 类型，例如 MindsDB、NL2SQL HTTP、NL2SQL Template、Neo4j 或 HugeGraph。
3. 在本体绑定页面把 ontology 显式绑定到该连接。
4. Agent 运行时通过 `business_data_search` 使用该绑定。

如果当前 ontology 没有绑定数据连接，`business_data_search` 会返回工具错误，提示先完成绑定。

## Observability

观测配置通过系统设置页或 `.onto-runtime/config/system_config.json` 管理。

| 字段 | 说明 |
| --- | --- |
| `observability.enabled` | 是否启用观测 |
| `observability.mode` | `local` 或 `langfuse` |
| `observability.local.retention_days` | 本地性能日志保留天数 |

说明：

- `observability.mode=local` 时，聊天页可打开当前会话性能分析。
- 管理员可在用户菜单打开本地观测分析，查看当前机器保留窗口内的整体趋势、慢运行、慢工具和慢运行阶段。
- 新配置不再读取旧的 `LANGFUSE_*` 或 `OBSERVABILITY_*` 环境变量。

## Runtime Limits

消费智能体规格限制是部署级策略，通过 `.onto-runtime/config/ops_config.json` 或 `OPS_CONFIG_PATH` 管理，不在系统设置页展示，也不开放给普通用户编辑。

常见限制包括：

- 用户数量
- workspace 数量
- 每个 workspace 的 ontology 数量
- 单本体对象数量
- 单本体 operation 数量
- 数据连接数量
- 模型 profile 数量
- 输入和输出 token 硬上限

## Secrets

生产环境建议设置：

```bash
AUTH_SECRET=<strong-random-secret>
SECRET_ENCRYPTION_KEY=<32-byte-base64-key>
```

不要把真实 API key、数据库密码或业务数据连接密钥提交到 Git。
