# DataAgent Provider 对接设计方案

## Context

当前项目已经有 workspace 级业务数据连接体系，`business_data_search` 的运行链路是：

```text
business_data_search
  -> resolve_business_data_connection(workspace_id, ontology_id)
  -> query_business_data_stream(request, connection)
  -> provider_registry.get_provider_spec(connection.provider)
  -> spec.factory({"ontology_search": {provider_name: connection.config}})
  -> provider.search_stream(query=query, entity=entity)
```

关键现状：

- 当前项目已有 `nl2sql_http`、`nl2sql_template`、`mindsdb`、`neo4j`、`hugegraph` provider。
- 当前项目尚未注册 `dataagent` provider。
- DataAgent 对外提供 A2A 1.0 REST 服务，业务查询入口为 `POST /a2a/rest/message:send`，服务发现入口为 `GET /.well-known/agent-card.json`。

这个对接不应该新增 Agent 工具面。Agent 仍然只调用 `business_data_search(ontology_id, entity, query)`，DataAgent 只是底层业务数据 provider。

## Decision

在当前项目新增一个 workspace 数据连接 provider：

```text
provider id: dataagent
transport: DataAgent A2A 1.0 RESTful HTTP
primary endpoint: POST {url}/a2a/rest/message:send
discovery endpoint: GET {url}/.well-known/agent-card.json
```

首版明确采用 RESTful 调用方式：

```text
DataAgentProvider.search_stream()
  -> services.dataagent.call_query(server, token, question, timeout)
  -> POST {server}/a2a/rest/message:send
  -> parse task.status.message.parts[*].text
  -> format_output(show_sql=False)
```

不采用进程内 SDK 调用、直接导入 DataAgent Python 包、JSON-RPC 或自定义 HTTP API。原因是 RESTful 方式保持了清晰的服务化边界，部署上只需要 DataAgent 服务地址和 token，不把两个系统的 Python 依赖、运行时配置、workspace 目录和模型配置耦合在同一进程里。

选择 `dataagent` 而不是 `dataagent_a2a` 的原因：

- 当前 `provider_registry.py` 中 provider id 是产品/后端类型语义，如 `mindsdb`、`neo4j`、`hugegraph`，不是传输协议语义。
- DataAgent 当前对接形态是 A2A REST；如果未来 DataAgent 增加其他协议，可在 config 中加 `rest_path` / `agent_card_path`，不必提前拆 provider id。

## Architecture

```mermaid
flowchart LR
  User["用户问题"] --> Agent["onto-agent"]
  Agent --> Tool["business_data_search"]
  Tool --> Resolver["workspace + ontology binding resolver"]
  Resolver --> Registry["provider_registry: dataagent"]
  Registry --> Provider["DataAgentProvider"]
  Provider --> Client["DataAgent A2A REST client"]
  Client --> DataAgent["DataAgent A2A REST service"]
  DataAgent --> Source["DataAgent YAML 中配置的数据源 / NL2SQL 能力"]
```

本体包和 workspace 连接的职责分离：

| 层 | 职责 |
| --- | --- |
| 本体包 `mappings/object-data-mappings.json` | 说明 object 对应哪个 provider alias、resource、字段口径 |
| workspace data binding | 把 ontology 绑定到具体 connection |
| data connection | 保存 `provider=dataagent` 和 DataAgent 服务地址、token、超时等配置 |
| DataAgentProvider | 把 `business_data_search` 请求转换成 A2A REST message |
| DataAgent 服务 | 根据自身 YAML / NL2SQL / 数据源配置执行数据分析 |

## Provider Schema

首版 schema 只要求 RESTful 调用必需的字段：

```python
"dataagent": BusinessDataProviderSpec(
    name="dataagent",
    label="DataAgent",
    description="通过 DataAgent A2A REST 服务进行自然语言数据查询与分析。",
    factory=DataAgentProvider,
    fields=[
        ProviderField("url", "DataAgent 服务地址", required=True),
        ProviderField("token", "访问 Token", secret=True),
        ProviderField("timeout_seconds", "超时时间", type="int", default=600),
        ProviderField("max_retries", "最大重试次数", type="int", default=3),
    ],
)
```

字段约束：

- `url` 归一化时去掉尾部 `/`。
- `token` 必须 `secret=True`，列表和详情接口只返回 `has_token`。
- 不在代码或文档中写真实 token；需要鉴权时由用户在连接配置中填写，或通过后续 secret 管理能力注入。

`rest_path` 和 `agent_card_path` 不建议放进首版公开 schema。它们应该先作为 client 内部常量：

```text
_REST_PATH = "/a2a/rest/message:send"
_AGENT_CARD_PATH = "/.well-known/agent-card.json"
```

只有当生产环境确实存在网关改写路径、多版本 DataAgent 并存，或需要兼容非标准部署时，再把路径配置开放到 provider schema。首版保持固定路径可以降低配置面和测试复杂度。

## Request Mapping

`DataAgentProvider.search_stream(query, entity)` 首版不改变 tool 入参。最小实现可以直接把 `query` 作为 DataAgent message text：

```python
result = call_query(self.base_url, self.token, query, timeout=self.timeout)
```

如果当前项目希望更充分利用本体对象映射，可以在不改变 RESTful 接口的前提下，把 `entity` 作为文本上下文拼进 question。这个增强不影响 DataAgent provider 的传输方式，仍然是 `POST /a2a/rest/message:send`。

增强版 message text：

```text
请基于业务数据回答以下查询。

目标业务对象：{entity}
用户查询：{query}

要求：
1. 优先返回与目标业务对象直接相关的数据。
2. 如果生成或执行了 SQL，请在最终结果中保留 SQL、列、行数和结果摘要。
3. 如果缺少字段或无法查询，请明确返回失败原因，不要编造表名、字段或数据。
```

A2A REST 请求体：

```json
{
  "message": {
    "role": "ROLE_USER",
    "parts": [
      {
        "text": "..."
      }
    ],
    "messageId": "m-<uuid>",
    "contextId": "c-<uuid>"
  },
  "configuration": {
    "returnImmediately": false
  }
}
```

HTTP headers：

```text
Content-Type: application/json
A2A-Version: 1.0
Authorization: Bearer <token>   # token 配置存在时才发送
```

## Response Normalization

DataAgent A2A 响应可能有两类：

1. 结构化 NL2SQL state 文本，里面包含 `sql`、`columns`、`rows_preview`、`confidence`、`error` 等字段。
2. 普通自然语言回答，只有 message/artifact text。

Provider 对外统一产出 `OntologySearchEvent`：

```text
start -> intermediate* -> final -> done
```

`final.content` 使用 JSON 字符串，建议结构：

```json
{
  "success": true,
  "provider": "dataagent",
  "data": {
    "columns": ["..."],
    "rows": [["..."]]
  },
  "row_count": 1,
  "confidence": 0.92,
  "has_sql": true,
  "message": "查询成功，返回 1 行数据",
  "raw_task_id": "task-abc123"
}
```

失败时：

```json
{
  "success": false,
  "provider": "dataagent",
  "data": null,
  "row_count": 0,
  "error": "HTTP 500: ...",
  "message": "查询失败: HTTP 500: ..."
}
```

SQL 默认不放入 `final.content`，只保留 `has_sql=true`。后续如果要审计 SQL，可通过受控 trace/evidence 存储，而不是直接暴露给 Agent。

## Connectivity Test

当前项目已有 `backend/agentic/business_data/connection_tester.py`，需要增加 `dataagent` 分支。

推荐测试策略：

1. 优先 `GET {url}{agent_card_path}`。
2. 校验响应是 JSON object，且至少包含 `name`。
3. 可选校验 `supportedInterfaces` 中存在 `HTTP+JSON` 或 URL 指向 `{rest_path}`。
4. 如果遇到 404，可兼容尝试 `/.well-known/agent.json`，用于支持少量历史或非标准部署。

连接测试不建议直接发业务查询，因为 DataAgent 的 YAML、模型和数据源可能较重，管理界面测试按钮应该快速验证服务和鉴权可用。

## Implementation Files

首版实现建议只改这些文件：

| 文件 | 变更 |
| --- | --- |
| `backend/agentic/ontology_search/providers/dataagent_provider.py` | 新增 provider adapter |
| `backend/agentic/services/dataagent.py` | 新增 DataAgent A2A REST client、响应解析、输出归一化 |
| `backend/agentic/business_data/provider_registry.py` | 注册 `dataagent` schema |
| `backend/agentic/business_data/connection_tester.py` | 增加 DataAgent AgentCard 探测 |
| `backend/tests/test_dataagent_provider.py` | 覆盖 provider schema、请求构造、响应解析、输出归一化 |
| `backend/tests/test_business_data_connections.py` | 覆盖连接创建、secret 脱敏、测试连接 |

通常不需要改 `frontend/src/components/DataConnectionManagement.jsx`。前端表单由后端 provider schema 动态渲染，`url`、`token` 字段也已经会按整行字段处理。

## Implementation Notes

- 当前项目已有 graph providers，注册时不能覆盖或回退已有 `neo4j` / `hugegraph`。
- 当前项目 `connection_tester.py` 已有 `_sanitize_secret_text()` 等安全辅助，DataAgent 失败信息也应做 token 脱敏。
- 连接测试应以 `/.well-known/agent-card.json` 为主，兼容 `/.well-known/agent.json`。
- 当前项目已有 `requests` 和 retry/session 风格；DataAgent client 应使用 `requests.Session` 实现 RESTful 协议。
- 首版可以继续“一次 business_data_search 创建一个 provider”，但中期应对齐 `docs/本体消费关键设计.md` 里的 provider client 复用方向。

不建议采用的方式：

| 方式 | 不作为首版原因 |
| --- | --- |
| 直接导入 DataAgent SDK | 会把 DataAgent 的 Python 依赖、配置、workspace、模型环境耦合进当前后端进程 |
| JSON-RPC | RESTful `message:send` 已满足首版同步查询，协议面更小 |
| 自定义 `/query` HTTP API | A2A REST 已提供标准消息入口，另造协议会增加两边维护成本 |
| 把 DataAgent 做成新 Agent 工具 | 会绕开现有 `business_data_search`、ontology binding 和 workspace connection 治理 |

## Error Handling

推荐错误码语义：

| 场景 | 返回 |
| --- | --- |
| url 未配置 | `success=false`, `error="DataAgent 服务地址未配置"` |
| HTTP 401/403 | `success=false`, `error="DataAgent 鉴权失败或无权限"` |
| HTTP 404 message endpoint | `success=false`, `error="DataAgent A2A REST 路径不可用"` |
| 非 JSON 响应 | `success=false`, `error="Invalid JSON response"` |
| A2A task 非 completed | `success=false`, `error=<status.error 或 message text>` |
| completed 但 state 内有 error | `success=false`, `error=<inner error>` |
| completed 且只有自然语言 | `success=true`, `data=null`, `answer=<text>` |

错误信息中必须避免回显 token、Authorization header、真实密码或完整请求头。

## Test Plan

最快相关检查：

```bash
cd backend
python -m pytest tests/test_dataagent_provider.py tests/test_business_data_connections.py -q
```

如果实现改动影响 provider registry 或 business data search 解析，再补跑：

```bash
cd backend
python -m pytest tests/test_business_data_search_resolution.py tests/test_ontology_search_providers.py -q
```

如果同步改了前端布局，再补跑：

```bash
cd frontend
npm run lint
npm run build
```

最终检查：

```bash
git diff --check
```

## Rollout Plan

1. 后端先新增 DataAgent client 和 provider，并注册 schema。
2. 增加连接测试分支，确保 DataAgent 服务地址、鉴权、AgentCard 可达。
3. 补齐 provider 单测和连接 API 单测。
4. 用一个 workspace 创建 `provider=dataagent` 的数据连接，并绑定到目标 ontology。
5. 使用现有 `business_data_search` 触发真实查询，观察 final JSON 是否稳定。
6. 再考虑 provider client 复用、并发限制、Runtime Facts/evidence 记录等生产化增强。

## Open Questions

- DataAgent 返回的 NL2SQL state 是否会长期保持 Python dict repr 字符串格式，还是会改成标准 JSON artifact？如果能由 DataAgent 输出标准 JSON，当前解析逻辑可以大幅简化。
- DataAgent 是否需要接收 `entity` / `resource` / `fields` 的结构化上下文？首版可以拼进 message text，中期建议扩展到 metadata/configuration。
- 生产环境是否使用统一 secret store？当前项目连接配置只有脱敏存储和回填行为，后续如引入 secret ref，需要同步调整 provider schema。
- 是否需要展示 SQL？首版不展示 SQL 给 Agent；如果调试或审计要看 SQL，应走受控日志或 evidence，不建议直接扩大工具输出。
