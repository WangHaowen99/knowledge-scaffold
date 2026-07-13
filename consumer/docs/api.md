# Backend API Reference

Base URL: `http://localhost:5055`

本文档以当前代码实现为准，覆盖以下路由前缀：

- `/auth/*`：认证与用户管理接口
- `/agentic/*`：主业务接口（runs / sessions / skills / ontologies / workspaces / data connections / operations / performance）
- `/api/*`：系统配置与本地观测接口
- `/.well-known/a2a/*` 与 `/a2a/*`：A2A 适配层接口
- `/acp/*`：ACP 适配层接口

---

## 1. 全局约定

### 1.1 认证

以下端点为**公开接口**：

- `POST /auth/login`
- `POST /auth/register`（仅开启注册时）
- `POST /auth/refresh`
- `POST /auth/logout`
- `GET /health`
- `GET /.well-known/a2a/agent-card.json`
- `GET /acp/agent`

其余接口默认要求：

```http
Authorization: Bearer <token>
```

ACP 除 `GET /acp/agent` 外也默认使用同一 JWT 认证；只有当 `.onto-runtime/acp_config.json` 中 `acp.authentication.type` 显式配置为 `none` 时才会跳过认证。

权限规则：

- 普通已认证用户可访问绝大多数读接口与运行接口
- `admin` 才能访问：
  - `POST /agentic/skills/upload`
  - `DELETE /agentic/skills/{skill_name}`
  - `DELETE /agentic/ontologies/{ontology_id}`
  - `DELETE /agentic/ontologies/{ontology_id}/versions/{version}`
  - `PUT /agentic/ontologies/{ontology_id}/file`
  - `POST /agentic/ontologies/{ontology_id}/upload`
  - `POST /agentic/ontologies/import-zip`
  - `POST /agentic/workspaces`
  - `PATCH /agentic/workspaces/{workspace_id}`（全局 admin 可转移 owner；workspace owner 可改名称和描述）
  - `PUT /agentic/workspaces/{workspace_id}/members`（全局 admin 或 workspace owner）
  - `DELETE /agentic/workspaces/{workspace_id}`
  - `POST /agentic/workspaces/{workspace_id}/default`
  - `PATCH /agentic/workspaces/{workspace_id}/runtime-config`
  - `POST /agentic/data-connections`
  - `PATCH /agentic/data-connections/{connection_id}`
  - `DELETE /agentic/data-connections/{connection_id}`
  - `POST /agentic/data-connections/{connection_id}/default`
  - `POST /agentic/data-connections/{connection_id}/test`
  - `PUT /agentic/ontologies/{ontology_id}/data-binding`
  - `DELETE /agentic/ontologies/{ontology_id}/data-binding`
  - `POST /agentic/operations/{ontology_id}/{operation_key}/staged/apply`
  - `POST /agentic/operations/{ontology_id}/{operation_key}/staged/reject`
  - `GET /auth/users`
  - `POST /auth/users`
  - `PUT /auth/users/{username}`
  - `DELETE /auth/users/{username}`
  - `GET /api/local-observability/overview`
  - `GET /api/local-observability/runs`
  - `GET /api/system-config/template`
  - `PATCH /api/system-config`
  - `POST /api/system-config/import`
  - `POST /api/system-config/test`

认证失败时通常返回：

```json
{ "error": "Unauthorized" }
```

权限不足时通常返回：

```json
{ "error": "Forbidden" }
```

如果用户必须先修改密码，除 `/auth/me` 和 `/auth/change-password` 外的认证接口会返回：

```json
{
  "error": "Password change required",
  "code": "PASSWORD_CHANGE_REQUIRED"
}
```

> 备注：错误返回字段在不同历史路由中并不完全统一，既有 `error`，也有 `detail`。

### 1.2 工作空间头 `X-Workspace-Id`

工作空间相关接口（尤其是 `/agentic/*`、`/a2a/*` 和 `/acp/*`）支持通过请求头指定工作空间：

```http
X-Workspace-Id: my-workspace
```

解析规则：

1. 如果请求头存在，则优先使用该 workspace
2. 如果请求头不存在，则使用默认工作空间
3. 如果没有默认工作空间，但系统中恰好只有一个工作空间，则自动选中它
4. 如果存在多个工作空间且未指定 `X-Workspace-Id`，请求会失败

工作空间 ID 规则：

- 必须以英文小写字母开头
- 只能包含英文小写字母、数字、下划线 `_`、中划线 `-`
- 最长 64 个字符

### 1.3 SSE 响应约定

流式接口使用 `text/event-stream`，并关闭代理缓冲：

```http
Cache-Control: no-cache
X-Accel-Buffering: no
```

项目当前有三类 SSE 事件：

- 主流协议：`event: agent.v2`
- A2A 投影协议：`event: task.update`
- ACP 投影协议：`event: answer` / `event: progress` / `event: run.completed` 等

### 1.4 常见错误码

- `400`：请求参数非法 / 缺少必填字段
- `401`：未认证或 token 无效
- `403`：角色权限不足
- `404`：资源不存在
- `409`：状态冲突
- `429`：运行请求 TPS 超限
- `503`：运行请求排队超时
- `500`：服务内部异常

---

## 2. 认证与健康检查

### 2.1 `POST /auth/login`

使用用户名密码换取 RS256 access/refresh token 对。

请求示例：

```json
{
  "username": "admin",
  "password": "<REDACTED_WEAK_PASSWORD>"
}
```

响应 `200`：

```json
{
  "token": "<signed-token>",
  "refresh_token": "<refresh-token>",
  "expires_in": 900,
  "user": {
    "username": "admin",
    "role": "admin",
    "password_change_required": true
  }
}
```

响应 `401`：

```json
{ "error": "Invalid credentials" }
```

说明：

- `token` 是短期 access token，用于 `Authorization: Bearer`。
- `refresh_token` 仅提交给 `/auth/refresh` 或 `/auth/logout`。浏览器应在受保护存储中保存并在 401 时做单次刷新，禁止写入日志或 URL。
- 通用 Python/TypeScript SDK 只接收 access token，不持有 refresh token。
- 首次空用户库会生成开发账号 `admin / <REDACTED_INITIAL_PASSWORD>`，该账号必须立即改密。

### 2.1.1 `POST /auth/refresh`

用 refresh token 轮换一对新 token。成功后必须丢弃旧 refresh token。

```json
{ "refresh_token": "<refresh-token>" }
```

响应 `200`：

```json
{ "token": "<new-access-token>", "refresh_token": "<new-refresh-token>", "expires_in": 900 }
```

过期、吊销或用户 `token_version` 已变化时返回 `401`。客户端应清除登录态并重新登录。

### 2.1.2 `POST /auth/logout`

吊销 refresh token，并使该用户此前签发的 token 版本失效：

```json
{ "refresh_token": "<refresh-token>" }
```

响应 `200`：`{ "success": true }`。

### 2.2 `GET /auth/me`

返回当前 token 对应用户。

响应 `200`：

```json
{
  "user": {
    "username": "admin",
    "role": "admin",
    "password_change_required": true
  }
}
```

### 2.3 `GET /auth/users` (`admin`)

列出全部用户。

响应 `200`：

```json
{
  "users": [
    { "username": "admin", "role": "admin", "password_change_required": true },
    { "username": "user1", "role": "user", "password_change_required": false }
  ]
}
```

### 2.4 `POST /auth/users` (`admin`)

创建用户。

请求体：

```json
{
  "username": "newuser",
  "password": "NewStrongPass123!"
}
```

字段说明：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `username` | string | 是 | 用户名，不能为空 |
| `password` | string | 是 | 临时密码，必须满足复杂度要求 |

说明：

- 新创建的用户角色固定为 `user`，不支持通过接口创建管理员
- 新用户首次登录后必须修改密码

响应 `201`：

```json
{
  "user": {
    "username": "newuser",
    "role": "user",
    "password_change_required": true
  }
}
```

错误返回：

- `400`：`{ "error": "用户名和密码不能为空" }`
- `400`：`{ "error": "用户 {username} 已存在" }`
- `400`：`{ "error": "密码复杂度不满足要求", "code": "PASSWORD_POLICY_FAILED", "violations": ["min_length"] }`

### 2.5 `PUT /auth/users/<username>` (`admin`)

管理员重置指定用户的临时密码。

请求体：

```json
{
  "password": "ResetStrongPass123!"
}
```

字段说明：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `password` | string | 否 | 新临时密码，必须满足复杂度要求 |

说明：

- 不支持通过接口修改用户角色
- 被重置密码的用户下次登录后必须修改密码
- 不能通过该接口重置当前登录用户自己的密码；当前用户修改密码必须使用 `/auth/change-password`

响应 `200`：

```json
{
  "user": {
    "username": "newuser",
    "role": "admin",
    "password_change_required": true
  }
}
```

错误返回：

- `400`：`{ "error": "用户 {username} 不存在" }`
- `400`：`{ "error": "不能通过用户管理重置当前登录用户密码，请使用修改密码入口" }`
- `400`：`{ "error": "密码复杂度不满足要求", "code": "PASSWORD_POLICY_FAILED", "violations": ["character_classes"] }`

### 2.6 `DELETE /auth/users/<username>` (`admin`)

删除用户。

约束：

- 不能删除自己
- 不能删除 `admin` 管理员

响应 `200`：

```json
{ "success": true }
```

错误返回：

- `400`：`{ "error": "不能删除自己" }`
- `400`：`{ "error": "不能删除 admin 管理员" }`

### 2.7 `PUT /auth/change-password`

修改当前用户密码。

请求体：

```json
{
  "old_password": "oldpass",
  "new_password": "NewStrongPass123!"
}
```

响应 `200`：

```json
{
  "success": true,
  "requires_relogin": true
}
```

说明：密码修改成功后旧 token 会失效，前端应清除本地登录态并要求用户使用新密码重新登录。

错误返回：

- `400`：`{ "error": "原密码和新密码不能为空" }`
- `400`：`{ "error": "原密码错误" }`
- `400`：`{ "error": "密码复杂度不满足要求", "code": "PASSWORD_POLICY_FAILED", "violations": ["same_as_old"] }`

### 2.8 `GET /health`

健康检查。无需认证。

响应 `200`：

```json
{ "status": "ok" }
```

---

## 3. Runs

### 3.1 `POST /agentic/runs/stream`

启动一次 agent 执行，并返回 `agent.v2` SSE 流。

请求体：

```json
{
  "session_id": "session-123",
  "request": "请分析这个问题",
  "run_id": "optional-run-id",
  "context": {
    "agent_mode": "quick",
    "llm_request_mode": "stream"
  }
}
```

字段说明：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `session_id` | string | 是 | 会话 ID；若会话不存在，运行时可自动创建 |
| `request` | string | 是 | 用户输入 |
| `run_id` | string | 否 | 希望使用的 run ID |
| `context` | object | 否 | 运行期上下文参数 |

常见 `context` 字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `selected_ontology_id` | string | 指定当前轮锁定的 ontology |
| `agent_mode` | string | Agent 执行模式；支持 `quick`、`thinking`、`multi_agent` |
| `llm_request_mode` | string | 上游 LLM 请求模式，支持 `stream` 或 `sync` |

`agent_mode` 取值：

| 值 | 行为 |
| --- | --- |
| `quick` | 默认值。关闭 LLM reasoning；不向本轮主 Agent 暴露 `spawn_sub_agent` 编排工具 |
| `thinking` | 启用 LLM reasoning；不向本轮主 Agent 暴露 `spawn_sub_agent` 编排工具 |
| `multi_agent` | 启用 LLM reasoning；向本轮主 Agent 暴露 `spawn_sub_agent` 并注入子 Agent 编排提示 |

SSE 格式：

```text
event: agent.v2
data: { ...agent-stream.v2 envelope... }
```

关键行为：

- 普通用户最多 **1** 个活跃 run
- 管理员最多 **100** 个活跃 run
- 启动前必须已有可用模型配置；如果选定 ontology 需要查询业务数据，还必须先为该 ontology 绑定工作区数据连接
- 如果提供的 `run_id` 已存在，系统**不会报错**，而是自动回退为一个新的 UUID
- 未传 `agent_mode`、传空值或传未知值时，默认为 `quick`
- 默认模式下 `llm_request_mode = stream`
- 若并发槽已满，返回 `202 application/json`，包含 `run_id`、排队位置和 `status/events/cancel` 链接；客户端先轮询 status，再连接 events。

响应 `400`：

- 缺少 `request`
- 缺少 `session_id`
- 模型配置不存在或未启用
- 已选 ontology 未绑定业务数据连接时，运行中的 `business_data_search` 会返回工具错误

响应 `429`：

```json
{
  "error": "请求过于频繁，请稍后重试",
  "limit": 50
}
```

响应 `503`：

```json
{
  "error": "排队超时，请稍后重试",
  "limit": 1
}
```

### 3.2 `GET /agentic/runs/{run_id}/events`

连接或重连一个已进入执行态的 run。游标可通过任一形式传入；同时存在时优先使用 `Last-Event-ID`：

```http
GET /agentic/runs/run-123/events?after_seq=42
Last-Event-ID: 42
```

响应为 `agent.v2` SSE，只回放 `seq > 42` 的事件，并继续推送后续事件。客户端必须在消费成功后保存 envelope 的 `seq`。

- run 仍在队列中：`409`，响应包含 queue 和 links。
- run 不属于当前用户/workspace：`404`。
- 运行时内存回放窗口已释放：`410`；最终消息仍可从 session 读取。

### 3.3 `GET /agentic/runs/{run_id}/status`

查询 run 状态。

响应 `200`：

```json
{
  "run_id": "run-123",
  "status": "running",
  "created_at": "2026-04-08T08:00:00Z",
  "updated_at": "2026-04-08T08:00:03Z",
  "completed_at": null,
  "result": null,
  "error": null
}
```

响应 `404`：

```json
{ "error": "Run not found" }
```

说明：

- 仅 run 所属用户本人可查询

### 3.4 `POST /agentic/runs/{run_id}/cancel`

取消一个运行中的 run。

请求体：

```json
{
  "reason": "user_stop"
}
```

说明：

- `reason` 可选
- 空字符串或非法值会被规范化为 `"unspecified"`

响应 `200`：

```json
{
  "success": true,
  "cancelled": true
}
```

响应 `404`：

```json
{ "error": "Run not found" }
```

### 3.5 `POST /agentic/runs/sync`

同步（非流式）执行 agent 工作流，等待全部迭代完成后返回最终结果。

请求体：

```json
{
  "request": "请分析这个问题",
  "session_id": "optional-session-id",
  "context": {
    "agent_mode": "quick"
  }
}
```

字段说明：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `request` | string | 是 | 用户输入 |
| `session_id` | string | 否 | 会话 ID；未传时自动生成 `sync_{uuid}` |
| `context` | object | 否 | 运行期上下文参数，同 `/runs/stream` 的 `context` |

并发限制：

- 普通用户最多 **1** 个活跃 run
- 管理员最多 **100** 个活跃 run（含流式 + 同步）

响应 `200`：

```json
{
  "status": "success",
  "final_answer": "分析结果文本",
  "session_id": "sync_a1b2c3d4",
  "run_id": "4c132188-ecdd-46db-b94f-c24e3bd3e0b0"
}
```

字段说明：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `status` | string | `success` / `error` / `cancelled` |
| `final_answer` | string | Agent 最终输出；类型取决于 `final_output`（可能为对象） |
| `session_id` | string | 实际使用的会话 ID |
| `run_id` | string | 运行 ID |

错误响应 `400`：

```json
{ "error": "Request is required" }
```

其他 `400` 场景：

- 模型配置不存在或未启用
- 已选 ontology 未绑定业务数据连接时，运行中的 `business_data_search` 会返回工具错误

TPS 超限 `429`：

```json
{
  "error": "请求过于频繁，请稍后重试",
  "limit": 50
}
```

排队超时 `503`：

```json
{
  "error": "排队超时，请稍后重试",
  "limit": 1
}
```

服务端异常 `500`：

```json
{
  "status": "error",
  "error": "错误描述",
  "final_answer": "",
  "session_id": "sync_a1b2c3d4",
  "run_id": null
}
```

说明：

- 该接口**没有请求级别的总超时**，实际耗时取决于 Agent Loop 迭代次数和 LLM 调用耗时
- 每次底层 LLM HTTP 调用的超时默认为 600 秒（可通过系统配置调整）
- 不支持 `run_id` 参数，每次调用都会创建新的 run
- 适用于不需要实时流式输出的批处理 / API 集成场景

---

## 4. Sessions

### 4.1 `GET /agentic/sessions`

列出当前用户、当前工作空间下的全部会话。

响应 `200`：

```json
{
  "sessions": [
    {
      "id": "session-123",
      "title": "Example",
      "created_at": "2026-04-08T08:00:00Z",
      "updated_at": "2026-04-08T08:05:00Z",
      "message_count": 4,
      "metadata": {
        "llm_request_mode": "stream"
      }
    }
  ]
}
```

### 4.2 `POST /agentic/sessions`

创建会话。

请求体：

```json
{
  "title": "可选标题",
  "llm_request_mode": "stream"
}
```

字段说明：

- `title`：可选
- `llm_request_mode`：支持 `stream` / `sync`；未传时默认 `stream`

响应 `201`：

```json
{
  "id": "session-123",
  "title": "可选标题",
  "created_at": "2026-04-08T08:00:00Z",
  "updated_at": "2026-04-08T08:00:00Z",
  "message_count": 0,
  "metadata": {
    "llm_request_mode": "stream"
  }
}
```

### 4.3 `GET /agentic/sessions/{session_id}`

返回会话详情、消息列表与原始事件。

响应 `200`：

```json
{
  "id": "session-123",
  "title": "Example",
  "created_at": "2026-04-08T08:00:00Z",
  "updated_at": "2026-04-08T08:05:00Z",
  "message_count": 4,
  "metadata": {
    "llm_request_mode": "stream"
  },
  "messages": [
    {
      "id": "session-123:0",
      "session_id": "session-123",
      "role": "user",
      "content": "Hello",
      "timestamp": "2026-04-08T08:00:01",
      "metadata": {}
    }
  ],
  "events": [
    {
      "event": "chat_message",
      "data": { "role": "user", "content": "Hello" },
      "ts": 1700000000000
    }
  ]
}
```

响应 `404`：

```json
{ "error": "Session not found" }
```

### 4.4 `DELETE /agentic/sessions/{session_id}`

删除会话，同时清理执行记录与相关 memory session。

响应 `200`：

```json
{ "success": true }
```

响应 `404`：

```json
{ "error": "Session not found" }
```

### 4.5 `GET /agentic/sessions/{session_id}/messages`

获取某会话消息历史。

查询参数：

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `limit` | integer | 否 | 仅返回最近 N 条消息 |

响应 `200`：

```json
{
  "messages": [
    {
      "id": "session-123:0",
      "session_id": "session-123",
      "role": "user",
      "content": "Hello",
      "timestamp": "2026-04-08T08:00:01",
      "metadata": {}
    }
  ]
}
```

响应 `404`：

```json
{ "error": "Session not found" }
```

### 4.6 `POST /agentic/sessions/{session_id}/generate-title`

根据用户消息自动生成中文标题。

请求体：

```json
{
  "message": "请帮我分析这次设计变更的影响范围"
}
```

响应 `200`：

```json
{
  "title": "设计变更影响分析",
  "session_id": "session-123"
}
```

当 LLM 失败时，会回退到基于原消息截断的标题，并返回：

```json
{
  "title": "请帮我分析这次设计变...",
  "session_id": "session-123",
  "fallback": true
}
```

响应 `400`：

```json
{ "error": "Message is required" }
```

### 4.7 本地性能分析

本组接口只读取当前机器上由 `observability.mode=local` 写出的新 local perf JSONL schema。默认扫描最近 24 小时，最大扫描窗口为 7 天；当本地观测关闭、当前模式为 Langfuse、日志过期或日志文件被删除时，返回结果可能为空。

底层 Trace 记录使用以下稳定 schema；`span_key`/`parent_key` 表达 run 内层级关系，回放和 local/Langfuse exporter 都保留该关系：

```json
{
  "schema_version": 1,
  "kind": "tool_call",
  "event": "tool.finished",
  "ts_ms": 1700000000000,
  "run_id": "run-123",
  "session_id": "session-123",
  "turn_id": 1,
  "span_key": "tool:call-1",
  "parent_key": "tools:main:1",
  "status": "success",
  "caller": "main",
  "iteration": 1,
  "metrics": { "elapsed_ms": 35 },
  "labels": { "tool_name": "business_data_search", "tool_call_id": "call-1" }
}
```

请求可携带 W3C `traceparent: 00-<32 hex trace id>-<16 hex parent span id>-<flags>`。非法值返回 `400 INVALID_TRACEPARENT`；合法 trace id 用于关联外部 Trace，内部 span 仍以 `span_key`/`parent_key` 建树。

#### `GET /agentic/sessions/{session_id}/performance`

返回当前已认证用户可见会话的性能摘要、轮次趋势、run 列表和读取诊断。

查询参数：

| 参数 | 说明 |
| --- | --- |
| `from` | 起始 epoch ms 或 ISO 时间，选填 |
| `to` | 结束 epoch ms 或 ISO 时间，选填 |
| `limit_runs` | run 列表上限，最大 `200` |

#### `GET /agentic/performance/runs/{run_id}`

返回单次运行的耗时拆分、iteration、LLM、工具、runtime phase 和事件时间线。

- 普通用户必须同时传 `session_id`，且该 session 的执行记录必须能关联该 `run_id`。
- 管理员可从全局本地观测页直接按 run 下钻。
- 支持与会话分析相同的 `from` / `to` 窗口参数。

#### `GET /api/local-observability/overview` (`admin`)

返回管理员全局总览，包括窗口内运行数、成功率、P50/P90/P99、按小时趋势、耗时分布、慢运行、工具排行、runtime phase 排行和失败摘要。

#### `GET /api/local-observability/runs` (`admin`)

返回管理员全局 run summary 列表。

管理员接口支持以下查询参数：

| 参数 | 说明 |
| --- | --- |
| `from`, `to` | 扫描窗口 |
| `workspace_id` | 工作空间过滤 |
| `status` | run 状态过滤 |
| `model_provider` | 模型供应商过滤 |
| `agent_mode` | Agent 模式过滤 |
| `limit`, `offset` | 仅 run 列表接口使用；`limit` 最大 `200` |

---

## 5. Skills

Skills 是**按工作空间隔离**的，接口会结合 `X-Workspace-Id` 读取或写入当前 workspace 的技能目录。

### 5.1 `GET /agentic/skills`

列出当前工作空间可用技能。

响应 `200`：

```json
{
  "skills": [
    {
      "name": "example-skill",
      "description": "...",
      "file_path": "/abs/path/.../SKILL.md",
      "allowed_tools": ["ontology_context"]
    }
  ]
}
```

### 5.2 `GET /agentic/skills/{skill_name}`

获取单个 skill 详情。

响应 `200`：

```json
{
  "name": "example-skill",
  "description": "...",
  "content": "---\nname: example-skill\n...",
  "file_path": "/abs/path/.../SKILL.md",
  "allowed_tools": ["ontology_context"]
}
```

响应 `404`：

```json
{ "error": "Skill not found" }
```

### 5.3 `POST /agentic/skills/upload` (`admin`)

上传一个技能压缩包。

请求类型：`multipart/form-data`

表单字段：

- `file`: `.zip` / `.tar` / `.tar.gz` / `.tgz`

归档要求：

- 压缩包根目录必须**只有一个目录**
- 该目录名就是 skill 名
- 目录内必须包含合法的 `SKILL.md`
- `SKILL.md` 的 frontmatter 中 `name` 必须与目录名一致
- 不允许危险路径和 symlink

响应 `201`：

```json
{
  "success": true,
  "skill": {
    "name": "example-skill",
    "description": "...",
    "file_path": "/abs/path/.../SKILL.md"
  }
}
```

常见错误：

- `No file uploaded`
- `Unsupported file format. Use .zip, .tar, .tar.gz, or .tgz`
- `Archive must contain exactly one root folder`
- `Missing SKILL.md file in skill folder`

### 5.4 `DELETE /agentic/skills/{skill_name}` (`admin`)

删除技能。

响应 `200`：

```json
{ "success": true }
```

响应 `404`：

```json
{ "error": "Skill not found" }
```

---

## 6. Ontologies

Ontologies 也按工作空间隔离。

### 6.1 `GET /agentic/ontologies`

列出当前工作空间下的 ontology。

响应 `200`：

```json
{
  "ontologies": [
    {
      "id": "example-domain-operations-manual",
      "name": "示例业务执行手册",
      "description": "...",
      "version": "1.0.0",
      "language": "zh-CN",
      "manifest_path": "/abs/path/.../manifest.yaml",
      "ontology_md_path": "/abs/path/.../ONTOLOGY.md",
      "business_data": {
        "connection_id": "example-nl2sql",
        "connection_name": "示例制造 NL2SQL",
        "provider": "nl2sql_http",
        "ready": true
      }
    }
  ]
}
```

### 6.2 `DELETE /agentic/ontologies/{ontology_id}` (`admin`)

删除一个 ontology 目录。

响应 `200`：

```json
{
  "success": true,
  "ontology_id": "example-domain-operations-manual"
}
```

响应 `404`：

```json
{ "error": "Ontology not found" }
```

### 6.2.1 `DELETE /agentic/ontologies/{ontology_id}/versions/{version}` (`admin`)

删除指定 ontology 的一个非激活版本，只移除 `versions/{version}` 并更新根 `manifest.yaml` 的 `versions` 列表；不会删除其他版本，也不会清理 ontology 级业务数据绑定。

响应 `200`：

```json
{
  "success": true,
  "ontology_id": "example-domain-operations-manual",
  "deleted_version": "2.0.0",
  "active_version": "1.0.0",
  "versions": ["1.0.0"]
}
```

删除当前激活版本时返回 `409`：

```json
{
  "error": "不能删除当前激活版本，请先切换激活版本后再删除。",
  "conflict_type": "active_version",
  "ontology_id": "example-domain-operations-manual",
  "version": "1.0.0",
  "active_version": "1.0.0",
  "versions": ["1.0.0", "2.0.0"]
}
```

### 6.3 `GET /agentic/ontologies/{ontology_id}/files?folder=operations|objects`

列出 ontology 下某个目录中的 Markdown 文件。

查询参数：

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `folder` | string | 是 | 只能是 `operations` 或 `objects` |

响应 `200`：

```json
{
  "folder": "operations",
  "label": "逻辑",
  "files": [
    {
      "name": "sample.md",
      "size": 1024,
      "updated_at": 1700000000
    }
  ]
}
```

说明：

- 当目录不存在时，返回空数组，不报错

### 6.4 `GET /agentic/ontologies/{ontology_id}/file?folder=operations|objects&name=sample.md`

读取单个 Markdown 文件内容。

响应 `200`：

```json
{
  "folder": "objects",
  "label": "实体",
  "name": "sample.md",
  "content": "# Markdown content"
}
```

常见错误：

- `Invalid ontology or folder. Use 'operations' or 'objects'.`
- `Invalid file name. Only .md files are allowed.`
- `File not found`
- `File encoding must be UTF-8`

### 6.5 `PUT /agentic/ontologies/{ontology_id}/file` (`admin`)

更新已有 Markdown 文件内容。

请求体：

```json
{
  "folder": "operations",
  "name": "sample.md",
  "content": "# Updated markdown"
}
```

响应 `200`：

```json
{
  "success": true,
  "folder": "operations",
  "label": "逻辑",
  "name": "sample.md"
}
```

说明：

- 仅允许 `.md` 文件
- 文件必须已存在
- 文件内容有大小与行数限制

### 6.6 `POST /agentic/ontologies/{ontology_id}/upload` (`admin`)

上传单个 Markdown 文件到 `operations` 或 `objects` 目录。

请求类型：`multipart/form-data`

表单字段：

- `folder=operations|objects`
- `file=<markdown file>`

响应 `201`：

```json
{
  "success": true,
  "folder": "objects",
  "label": "实体",
  "name": "sample.md",
  "overwritten": false
}
```

### 6.7 `GET /agentic/ontologies/{ontology_id}/download`

下载 ontology 指定版本的 ZIP 包。

查询参数：

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `version` | string | 否 | 要下载的版本；不传时下载当前激活版本 |

行为说明：

- 普通已认证用户可下载
- ZIP 内包含一个顶层目录 `{ontology_id}/`
- 只递归打包目标版本目录下的普通文件，包括 `objects/`、`operations/`、`scripts/`、`assets/` 等
- ZIP 内的 `manifest.yaml` 会被写成当前下载版本，并将 `versions` 收敛为仅包含该版本
- 下载文件名为 `{ontology_id}-{version}.zip`
- 当前接口不下载全部版本

响应 `200`：

```http
Content-Type: application/zip
Content-Disposition: attachment; filename="{ontology_id}-{version}.zip"
```

响应 `404`：

```json
{ "error": "Ontology not found" }
```

### 6.8 `POST /agentic/ontologies/import-zip` (`admin`)

上传 `.zip` 包并导入为 ontology；必要时自动创建或覆盖目标 ontology 目录。

请求类型：`multipart/form-data`

表单字段：

- `file=<zip>`
- `overwrite=true|false`（可选）

行为说明：

- 仅支持 `.zip`
- 支持两类上传包：
  - 源格式包：需能识别出有效源目录（包含 `object` 与 `logic`），服务端会自动转换成 ontology 格式
  - 已转换 ontology 包：需包含有效 `manifest.yaml`，并带有 `objects/`、`operations/`、`objects-list.md`、`operations-list.md` 中的任意部分
- 源格式包会优先从 `SKILL.md` frontmatter 读取 `id` / `name` / `description`
- 源格式包如果额外包含 `action/` 目录，会复制其中 Python 脚本和 `.env` 文件到目标 `scripts/` 目录，并根据 Python 脚本生成 `scripts-list.md`；脚本顶部可用 `# 工具流程: <操作名>` 声明对应的 `logic/<操作名>.md`，未声明时退回使用脚本名作为 key；脚本参数优先读取顶部 `parameters` / `参数` 注释，未声明时静态解析 `argparse.add_argument(...)`；下划线开头的 helper 和 `.env` 只复制不注册
- 已转换 ontology 包会直接使用 `manifest.yaml` 中的元数据
- 如果目标 ontology 已存在且未传 `overwrite=true`，会返回确认型冲突
- 传 `overwrite=true` 时，不论是源格式包还是已转换包，都会以新导入结果整体替换目标 ontology 目录，不保留旧残留文件

冲突响应 `409`：

```json
{
  "error": "本体 example-domain-operations-manual 已存在，是否覆盖？",
  "requires_confirmation": true,
  "ontology_id": "example-domain-operations-manual",
  "ontology_name": "示例业务执行手册"
}
```

成功响应 `201`：

```json
{
  "success": true,
  "created": true,
  "overwritten": false,
  "import_mode": "source",
  "ontology_id": "example-domain-operations-manual",
  "ontology_name": "示例业务执行手册",
  "source_root": "/tmp/.../unzipped/project",
  "objects_count": 12,
  "operations_count": 18,
  "objects_list_path": "/abs/path/.../objects-list.md",
  "operations_list_path": "/abs/path/.../operations-list.md",
  "business_data_binding": {
    "ontology_id": "example-domain-operations-manual",
    "connection_id": "example-nl2sql",
    "updated_at": "2026-05-10T10:00:00+00:00"
  },
  "warnings": {
    "objects": [],
    "objects_list": [],
    "operations": []
  }
}
```

说明：

- `import_mode=source`：表示上传的是源格式包，服务端已执行格式转换
- `import_mode=converted`：表示上传的是已转换完成的 ontology 包，服务端直接导入目录内容
- `business_data_binding` 仅在新导入 ontology 且当前 workspace 已设置默认数据连接时返回，否则为 `null`

---

## 7. Workspaces

### 7.1 Workspace 对象结构

响应中的 workspace 一般形如：

```json
{
  "id": "demo-workspace",
  "name": "Demo Workspace",
  "description": "示例工作空间",
  "created_at": "2026-04-16T04:00:00+00:00",
  "updated_at": "2026-04-16T04:00:00+00:00",
  "is_default": true,
  "owner": "admin",
  "members": ["analyst"]
}
```

### 7.2 `GET /agentic/workspaces`

列出全部工作空间。

响应 `200`：

```json
{
  "workspaces": [
    {
      "id": "demo-workspace",
      "name": "Demo Workspace",
      "description": "示例工作空间",
      "created_at": "2026-04-16T04:00:00+00:00",
      "updated_at": "2026-04-16T04:00:00+00:00",
      "is_default": true
    }
  ],
  "total": 1,
  "default_workspace_id": "demo-workspace"
}
```

### 7.3 `POST /agentic/workspaces` (`admin`)

创建工作空间。

请求体：

```json
{
  "id": "demo-workspace",
  "name": "Demo Workspace",
  "description": "示例工作空间",
  "set_as_default": true
}
```

字段说明：

- `id` 可选；不传时会根据 `name` 自动 slugify，并在冲突时追加随机后缀
- `name` 必填
- `description` 可选
- `set_as_default` 可选

响应 `201`：返回创建后的 workspace 对象。

错误返回：

```json
{ "detail": "工作空间 ID 'demo-workspace' 已存在" }
```

### 7.4 `GET /agentic/workspaces/{workspace_id}`

获取单个工作空间。

响应 `200`：返回 workspace 对象。

响应 `404`：

```json
{ "detail": "工作空间 'demo-workspace' 不存在" }
```

### 7.5 `PATCH /agentic/workspaces/{workspace_id}`

全局 admin 或该 workspace owner 可更新名称和描述；只有全局 admin 可通过 `owner` 转移所有权。

请求体：

```json
{
  "name": "新的名称",
  "description": "新的描述"
}
```

响应 `200`：返回更新后的 workspace 对象。

#### `PUT /agentic/workspaces/{workspace_id}/members`

全量替换成员列表。全局 admin 或 workspace owner 可调用；成员用户名必须已存在且不得重复。

```json
{ "members": ["analyst", "operator"] }
```

无 owner 的存量 workspace 保持公共可见，服务不会在迁移时自动私有化；管理员应根据审计报告显式设置 owner 和 members。

### 7.6 `DELETE /agentic/workspaces/{workspace_id}` (`admin`)

删除工作空间。

响应 `200`：

```json
{
  "success": true,
  "id": "demo-workspace"
}
```

说明：

- 默认工作空间不可删除，必须先切换默认 workspace

### 7.7 `POST /agentic/workspaces/{workspace_id}/default` (`admin`)

切换默认工作空间。

响应 `200`：

```json
{
  "workspace": {
    "id": "demo-workspace",
    "name": "Demo Workspace",
    "description": "示例工作空间",
    "created_at": "2026-04-16T04:00:00+00:00",
    "updated_at": "2026-04-16T04:00:00+00:00",
    "is_default": true
  },
  "default_workspace_id": "demo-workspace"
}
```

### 7.8 工作区运行配置

工作区运行配置保存在 `.onto-runtime/workspaces/{workspace_id}/runtime_config.json`，用于控制与具体工作区绑定的运行行为。

#### `GET /agentic/workspaces/{workspace_id}/runtime-config`

读取指定工作区运行配置。

响应 `200`：

```json
{
  "lang": {
    "enabled": false,
    "default": "zh"
  },
  "acp_suppress_script_patterns": ""
}
```

#### `PATCH /agentic/workspaces/{workspace_id}/runtime-config` (`admin`)

增量更新指定工作区运行配置。

请求示例：

```json
{
  "lang": {
    "enabled": true,
    "default": "zh"
  },
  "acp_suppress_script_patterns": "internal_operation_key"
}
```

#### `GET /agentic/workspaces/current/runtime-config`

按当前请求的 `X-Workspace-Id` 或默认工作区读取运行配置。

### 7.9 工作区业务数据连接与本体绑定

业务数据连接按 workspace 隔离。运行时不会从全局配置隐式继承 provider；`business_data_search` 会解析当前 workspace 下当前 ontology 的显式绑定。

Graph provider 配置：

| Provider | 必填配置 | 可选配置 | 隔离要求 |
| --- | --- | --- | --- |
| `neo4j` | `uri`、`username`、`password` | `database=neo4j`、超时/重试/结果上限 | `workspace_property=d2o_workspace_id`，建议显式配置 `ontology_property=d2o_ontology_id` |
| `hugegraph` | `url` | `graph=hugegraph`、`graph_space`、`username/password/token`、超时/重试/结果上限 | 同样配置 workspace/ontology 属性 |

密码和 token 加密落盘，对外读取只返回 `has_password` / `has_token`。创建连接后先调用 test，再通过 data-binding 绑定 ontology；MCP search 只接受已绑定的 `neo4j` 或 `hugegraph`。

新增业务数据 provider 的开发步骤见 [`docs/business-data-provider-extension.md`](business-data-provider-extension.md)。

#### `GET /agentic/business-data/provider-schemas`

返回当前支持的数据连接 provider 表单 schema。所有已认证用户可访问。

响应 `200`：

```json
{
  "providers": [
    {
      "name": "nl2sql_http",
      "label": "NL2SQL HTTP",
      "description": "通过 NL2SQL HTTP 查询服务检索本体业务数据。",
      "fields": [
        { "name": "url", "label": "服务地址", "type": "str", "required": true, "secret": false, "default": "" },
        { "name": "token", "label": "访问 Token", "type": "str", "required": false, "secret": true, "default": "" }
      ]
    }
  ]
}
```

#### `GET /agentic/data-connections`

列出当前 workspace 的业务数据连接。secret 字段只返回 `has_*` 标记。

响应 `200`：

```json
{
  "default_connection_id": "example-nl2sql",
  "connections": [
    {
      "id": "example-nl2sql",
      "name": "示例制造 NL2SQL",
      "provider": "nl2sql_http",
      "config": {
        "url": "https://nl2sql.example.com",
        "has_token": true,
        "scene": "example",
        "timeout_seconds": 600,
        "max_retries": 3
      },
      "created_at": "2026-05-10T10:00:00+00:00",
      "updated_at": "2026-05-10T10:00:00+00:00"
    }
  ]
}
```

#### `POST /agentic/data-connections` (`admin`)

创建当前 workspace 的业务数据连接。

请求体：

```json
{
  "id": "example-nl2sql",
  "name": "示例制造 NL2SQL",
  "provider": "nl2sql_http",
  "set_as_default": true,
  "config": {
    "url": "https://nl2sql.example.com",
    "token": "secret-token",
    "scene": "example"
  }
}
```

响应 `201`：

```json
{
  "connection": { "...": "同 GET /agentic/data-connections 中的单个 connection" },
  "default_connection_id": "example-nl2sql"
}
```

#### `PATCH /agentic/data-connections/{connection_id}` (`admin`)

更新连接名称、provider 或配置。secret 字段留空时保留原值。

#### `DELETE /agentic/data-connections/{connection_id}` (`admin`)

删除连接。若仍有 ontology 绑定该连接，返回 `409`。

```json
{
  "detail": "该连接仍被本体绑定，不能删除",
  "bound_ontologies": ["example-domain-operations-manual"]
}
```

#### `POST /agentic/data-connections/{connection_id}/default` (`admin`)

设置当前 workspace 新导入 ontology 的默认连接。导入新 ontology 时如果尚无绑定，会自动创建绑定；运行时仍以 ontology 显式绑定为准。

#### `POST /agentic/data-connections/{connection_id}/test` (`admin`)

测试连接服务可达性。MindsDB 会执行轻量 SQL 探测；启用鉴权时会先登录再带 token 探测查询接口。NL2SQL HTTP / NL2SQL Template 会请求 `/api/v1/query`，携带轻量 `question` 与配置的 `scene`，验证服务地址、网络和鉴权头是否可用。

```json
{
  "success": true,
  "message": "NL2SQL 服务连接正常",
  "tested_at": "2026-05-10T10:00:00+00:00",
  "connection": {
    "id": "example-nl2sql",
    "name": "示例制造 NL2SQL",
    "provider": "nl2sql_http",
    "config": {
      "url": "https://nl2sql.example.com",
      "has_token": true,
      "scene": "example",
      "timeout_seconds": 600,
      "max_retries": 3
    }
  }
}
```

#### `GET /agentic/ontologies/{ontology_id}/data-binding`

读取某个 ontology 的业务数据连接绑定。

```json
{
  "ontology_id": "example-domain-operations-manual",
  "binding": {
    "connection_id": "example-nl2sql",
    "updated_at": "2026-05-10T10:00:00+00:00"
  }
}
```

#### `PUT /agentic/ontologies/{ontology_id}/data-binding` (`admin`)

绑定或切换 ontology 的业务数据连接。

```json
{
  "connection_id": "example-nl2sql"
}
```

#### `DELETE /agentic/ontologies/{ontology_id}/data-binding` (`admin`)

删除 ontology 的业务数据连接绑定。

### 7.10 Operation Evolution API

Operation evolution API 按当前 workspace 读取和审核由 `memory` 工具/运行时积累的 operation 改进建议，并在管理员确认后把 staged patch 应用到 ontology 的 operation Markdown 文件。

| 端点 | 权限 | 说明 |
| --- | --- | --- |
| `GET /agentic/operations/proposals` | 已认证 | 当前 workspace 下按 operation 汇总 proposal |
| `GET /agentic/operations/proposals/export` | 已认证 | 导出当前 workspace 的 proposal 聚合与明细 |
| `GET /agentic/operations/{ontology_id}/{operation_key}/proposals` | 已认证 | 查看单个 operation 的 proposal，可带 `include_records=true` |
| `POST /agentic/operations/{ontology_id}/{operation_key}/proposals/{fingerprint}/endorse` | 已认证 | 支持一条 proposal |
| `POST /agentic/operations/{ontology_id}/{operation_key}/proposals/{fingerprint}/reject` | 已认证 | 拒绝一条 proposal |
| `PATCH /agentic/operations/{ontology_id}/{operation_key}/proposals/{fingerprint}` | 已认证 | 编辑 proposal 内容 |
| `POST /agentic/operations/{ontology_id}/{operation_key}/rescan` | 已认证 | 请求 patcher 重新评估该 operation |
| `GET /agentic/operations/pending` | 已认证 | 查看等待管理员审核的 staged patch |
| `GET /agentic/operations/{ontology_id}/{operation_key}/staged` | 已认证 | 获取原文、staged 内容与元数据，用于 diff |
| `POST /agentic/operations/{ontology_id}/{operation_key}/staged/apply` | admin | 将 staged patch 应用到 operation 文件并归档原文 |
| `POST /agentic/operations/{ontology_id}/{operation_key}/staged/reject` | admin | 丢弃 staged patch，可同时拒绝关联 proposals |

---

## 8. System Config

系统配置路由位于 **`/api`** 前缀，而不是 `/agentic`。

配置优先级：

```text
先读已保存的 system_config.json；某个字段没有保存值时再读环境变量；最后用内置默认值
```

说明：

- 模型属于外部运行必需能力，不提供可运行的内置默认值。
- 业务数据连接不属于全局系统配置，按 workspace 存储并由 ontology 绑定选择。
- 内置默认值主要用于超时、重试、temperature、存储路径等运行参数。
- 可通过 `SYSTEM_CONFIG_PATH` 指定其他保存位置。

支持的 section：

- `llm`
- `llm_profiles`
- `task_llm`
- `storage`
- `auth`
- `agent`
- `memory`
- `observability`
- `web_search`

说明：观测配置只从 `.onto-runtime/config/system_config.json` / 系统设置页读取，不再读取旧的 `LANGFUSE_*` 或 `OBSERVABILITY_*` 环境变量。

### 8.1 `GET /api/system-config`

返回对前端可见的系统配置。所有已认证用户都可查看。

注意：secret 字段不会直接返回明文，而是返回 `has_*` 标记。

注意：

- 当前版本后端不再内建默认 LLM 地址与模型 ID
- 若系统尚未配置模型，则 `llm.base_url` / `llm.model` 以及 `llm_profiles.profiles` 可能为空
- 业务数据连接请使用 `/agentic/data-connections` 与 `/agentic/ontologies/{ontology_id}/data-binding`

响应 `200`：

```json
{
  "llm": {
    "base_url": "",
    "model": "",
    "has_api_key": false,
    "timeout": 600,
    "max_tokens": 4096,
    "temperature": 0.7
  },
  "llm_profiles": {
    "default_profile_id": "",
    "profiles": []
  },
  "task_llm": {
    "base_url": "",
    "model": "",
    "has_api_key": false,
    "timeout": 600
  },
  "storage": {
    "project_root": ".../.onto-runtime"
  },
  "auth": {
    "auth_server_url": "http://auth-server:5056",
    "has_auth_server_admin_token": true,
    "auth_server_audience": "meta-wise",
    "auth_server_issuer": ""
  },
  "agent": {
    "llm_connection_max_retries": 3,
    "main_agent_max_iterations": 50,
    "sub_agent_max_iterations": 20,
    "sub_agent_max_concurrency": 6,
    "sub_agent_max_depth": 10,
    "parallel_default_max_workers": 10,
    "context_max_rounds": 10,
    "context_prune_tools": ""
  },
  "observability": {
    "enabled": true,
    "mode": "local",
    "local": {
      "retention_days": 30
    },
    "langfuse": {
      "public_key": "",
      "has_secret_key": false,
      "base_url": "",
      "debug": false
    }
  },
  "web_search": {
    "enabled": false,
    "provider": "tavily",
    "base_url": "https://api.tavily.com",
    "has_api_key": false,
    "timeout_seconds": 20,
    "default_max_results": 5,
    "max_results_limit": 10,
    "search_depth": "basic",
    "allowed_domains": "",
    "blocked_domains": ""
  }
}
```

### 8.2 `GET /api/system-config/readiness`

返回系统运行前置配置是否就绪。所有已认证用户都可查看。

响应 `200`：

```json
{
  "ready": false,
  "model": {
    "ready": false,
    "message": "请先配置可用模型",
    "missing_fields": ["llm_profiles.profiles"],
    "default_profile_id": "",
    "profile_count": 0
  }
}
```

字段说明：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `ready` | bool | 模型配置是否满足运行前置条件 |
| `model.ready` | bool | 是否存在可用模型 profile |
| `model.missing_fields` | array | 模型侧缺失字段 |

### 8.3 `GET /api/system-config/template` (`admin`)

下载系统配置模板文件。

响应：

- `Content-Type: application/json`
- `Content-Disposition: attachment; filename="system_config.template.json"`

### 8.4 `PATCH /api/system-config` (`admin`)

更新单个配置 section。

请求体：

```json
{
  "section": "llm",
  "data": {
    "base_url": "https://example.com/openai/v1",
    "model": "gpt-4.1",
    "api_key": "sk-xxx"
  }
}
```

响应 `200`：

```json
{
  "section": "llm",
  "config": {
    "base_url": "https://example.com/openai/v1",
    "model": "gpt-4.1",
    "has_api_key": true,
    "timeout": 600,
    "max_tokens": 4096,
    "temperature": 0.7
  }
}
```

说明：

- `section` 仅支持：`llm | llm_profiles | task_llm | storage | auth | agent | memory | observability | web_search`
- `data` 必须为对象
- secret 清空不能直接传空字符串，需使用对应清除字段：
  - `llm.clear_api_key`
  - `task_llm.clear_api_key`
  - `observability.clear_langfuse_secret_key`
  - `web_search.clear_api_key`
- 配置保存后会尝试刷新运行时依赖；若保存成功但刷新失败，会返回 `500`
- `llm_profiles` 为多模型配置 section，对话时实际优先使用它，而不是旧版单模型 `llm`

### 8.5 `POST /api/system-config/import` (`admin`)

批量导入配置。

请求体：

```json
{
  "config": {
    "llm": {
      "base_url": "https://example.com/openai/v1",
      "model": "gpt-4.1",
      "api_key": "sk-xxx"
    },
    "agent": {
      "main_agent_max_iterations": 60
    }
  }
}
```

响应 `200`：

```json
{
  "success": true,
  "message": "配置导入成功",
  "imported_sections": ["llm", "agent"],
  "config": { "...": "同 GET /api/system-config" }
}
```

说明：

- 至少需要包含一个合法 section
- 不支持未知 section
- 导入时可包含旧版 `llm` section；运行时会内部兼容迁移，但不会对外暴露兼容模型概念

### 8.6 `POST /api/system-config/test` (`admin`)

测试某个配置 section 的有效性或连接性。

请求体：

```json
{
  "section": "llm",
  "data": {
    "base_url": "https://example.com/openai/v1",
    "model": "gpt-4.1",
    "api_key": "sk-xxx"
  }
}
```

响应 `200`：

```json
{
  "section": "llm",
  "success": true,
  "message": "LLM 连接成功，模型可用：gpt-4.1",
  "tested_at": "2026-04-16T04:00:00+00:00"
}
```

测试行为：

- `llm` / `task_llm`：测试 OpenAI 兼容 `chat/completions`
- `llm_profiles`：测试指定模型 profile（若请求里带 `profile_id`）或默认模型 profile
- `observability`：`enabled=false` 返回 `观测已关闭`，启用后 `mode=local` 返回本地性能日志已启用，`mode=langfuse` 时校验 `public_key` / `secret_key` / `base_url` 是否已配置
- `storage` / `auth` / `agent`：仅做配置校验，返回 `配置校验通过`

### 8.7 配置字段清单

#### `llm`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `base_url` | string | 主模型 API 基地址 |
| `model` | string | 主模型名称 |
| `api_key` | secret | 主模型密钥 |
| `timeout` | int | 请求超时秒数 |
| `max_tokens` | int | 默认最大 tokens |
| `temperature` | float | 采样温度 |

#### `llm_profiles`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `default_profile_id` | string | 默认模型 profile id；为空表示尚未配置默认模型 |
| `profiles` | array | 多模型配置列表 |

`profiles[]` 字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | profile 唯一标识 |
| `name` | string | 显示名称 |
| `provider` | string | 当前支持 `glm \| deepseek \| openai_compatible` |
| `base_url` | string | 模型 API 基地址 |
| `model` | string | 模型 ID |
| `api_key` | secret | 模型密钥 |
| `timeout` | int | 请求超时秒数 |
| `max_tokens` | int | 默认最大 tokens |
| `temperature` | float | 采样温度 |
| `enabled` | bool | 是否启用 |

#### `task_llm`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `base_url` | string | 轻量任务模型 API 基地址 |
| `model` | string | 轻量任务模型名称 |
| `api_key` | secret | 密钥 |
| `timeout` | int | 超时秒数 |

#### `storage`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `project_root` | string | `.onto-runtime` 根目录 |

#### `auth`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `auth_server_url` | string | 独立 auth-server 地址（必填），如 `http://auth-server:5056` |
| `auth_server_admin_token` | secret | auth-server Admin API 共享管理令牌（对外仅返回 `has_auth_server_admin_token` 布尔） |
| `auth_server_audience` | string | JWT 本地验签 audience，默认 `meta-wise` |
| `auth_server_issuer` | string | JWT issuer，留空 = 使用 `auth_server_url` |

#### `agent`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `llm_connection_max_retries` | int | LLM 连接重试次数 |
| `main_agent_max_iterations` | int | 主 agent 最大迭代数 |
| `sub_agent_max_iterations` | int | 子 agent 最大迭代数 |
| `sub_agent_max_concurrency` | int | 子 agent 最大并发数 |
| `sub_agent_max_depth` | int | 子 agent 最大深度 |
| `parallel_default_max_workers` | int | 默认并行 worker 数 |
| `context_max_rounds` | int | 上下文保留轮数 |
| `context_prune_tools` | string | 上下文裁剪工具列表 |

#### `observability`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `enabled` | bool | 是否启用运行时观测输出 |
| `mode` | string | 启用后使用的观测 exporter，支持 `local \| langfuse` |
| `local.retention_days` | int | 本地性能日志保留天数 |
| `langfuse.public_key` | string | Langfuse public key |
| `langfuse.secret_key` | secret | Langfuse secret key |
| `langfuse.base_url` | string | Langfuse 服务地址 |
| `langfuse.debug` | bool | 是否启用 Langfuse SDK debug |

#### `web_search`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `enabled` | bool | 是否向模型暴露联网搜索工具 |
| `provider` | string | 搜索 provider，当前支持 `tavily` |
| `base_url` | string | 搜索服务地址 |
| `api_key` | secret | 搜索服务 API Key |
| `timeout_seconds` | int | 搜索请求超时 |
| `default_max_results` | int | 工具未指定结果数时的默认值 |
| `max_results_limit` | int | 工具可请求的最大结果数上限 |
| `search_depth` | string | Tavily 搜索深度：`basic \| fast \| ultra-fast \| advanced` |
| `allowed_domains` | string | 逗号分隔允许域名；留空表示不限制 |
| `blocked_domains` | string | 逗号分隔屏蔽域名 |

#### `memory`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `mode` | string | 记忆后端模式 |
| `recall_topk` | int | 召回条数 |
| `recall_threshold` | string | 可选召回阈值 |
| `workspace_root` | string | 文件系统记忆工作区根目录 |

### 8.8 MCP 管理与独立服务

MCP 管理接口走 backend `5055`，要求 consumer access token 和 `X-Workspace-Id`；MCP 工具调用走独立服务 `7488`，要求专用 MCP token。

#### `GET /agentic/mcp/runtime/status`

返回独立进程的连通状态、transport、外部地址和 search/chat 服务卡片。外部地址来自 `MCP_PUBLIC_BASE_URL`，部署时应设置为客户端实际可达的域名或 IP。

#### `GET /agentic/mcp/tokens`

列出当前用户、当前 workspace 的 token 元数据。响应不包含明文 token。

#### `POST /agentic/mcp/tokens`

```json
{
  "name": "automation",
  "scopes": ["mcp:search"],
  "expires_in": 86400
}
```

允许的 scope：

| scope | 允许的服务 |
| --- | --- |
| `mcp:search` | `/search/mcp`、`/search/sse`，只执行原子图查询，不调用 LLM |
| `mcp:chat` | `/chat/mcp`、`/chat/sse`，进入标准 workflow 和 run admission queue |

未传 scopes 时默认同时授予两项。响应 `201` 只在创建时返回一次 `token` 明文；token 固定绑定 `sub` 和 `workspace_id`，不能用请求头切换到其他 workspace。

#### `DELETE /agentic/mcp/tokens/{token_id}`

吊销当前用户、当前 workspace 下的 token。MCP token 注册表默认位于 `.onto-runtime/mcp-tokens.json`，备份和恢复必须与 workspace 数据同步完成。

独立服务默认地址：

- Streamable HTTP：`http://<host>:7488/search/mcp`、`http://<host>:7488/chat/mcp`
- SSE：`http://<host>:7488/search/sse`、`http://<host>:7488/chat/sse`
- 健康检查：`http://<host>:7488/health`

search 服务要求 ontology 已绑定 `neo4j` 或 `hugegraph` 数据连接。所有 MCP 调用应传 `Authorization: Bearer <mcp_token>`；consumer access token 不可替代 MCP token。

---

## 9. A2A Protocol

A2A 适配层以现有 `agent-stream.v2` 运行为内核，对外暴露 A2A 风格的任务接口。

### 9.1 Agent Card `GET /.well-known/a2a/agent-card.json`

公开发现接口，无需认证。

响应 `200`：

```json
{
  "name": "onto-agent",
  "description": "A2A adapter over agent-stream.v2 runtime",
  "version": "0.1.0",
  "url": "http://localhost:5055",
  "skills": [
    "ontology_context",
    "skill",
    "business_data_search",
    "execute_script",
    "spawn_sub_agent",
    "memory"
  ],
  "capabilities": {
    "streaming": true,
    "cancellation": true
  },
  "endpoints": {
    "create_task": "/a2a/tasks",
    "get_task": "/a2a/tasks/{task_id}",
    "cancel_task": "/a2a/tasks/{task_id}",
    "task_events": "/a2a/tasks/{task_id}/events",
    "stream_create": "/a2a/tasks/stream"
  },
  "authentication": {
    "type": "bearer",
    "notes": "Same JWT auth as /agentic/* endpoints"
  }
}
```

### 9.2 Task 状态

A2A task 使用以下状态：

- `pending`
- `in_progress`
- `completed`
- `failed`
- `canceled`
- `not_found`

映射规则：

- 内部 `RunStatus.RUNNING` -> `in_progress`
- 内部 `RunStatus.FAILED` -> `failed`
- 内部 `RunStatus.CANCELLED` -> `canceled`
- 内部 `RunStatus.PENDING` -> `pending`
- 内部 `RunStatus.COMPLETED` -> `completed`

### 9.3 Task Snapshot 结构

A2A 响应中的 `task` 一般形如：

```json
{
  "id": "task-123",
  "session_id": "session-123",
  "state": "pending",
  "created_at": null,
  "updated_at": null,
  "completed_at": null,
  "error": null,
  "result": null,
  "metadata": {
    "mode": "background"
  }
}
```

字段说明：

| 字段 | 说明 |
| --- | --- |
| `id` | task ID，对应内部 run ID |
| `session_id` | 归属会话 |
| `state` | A2A 侧状态 |
| `created_at` / `updated_at` / `completed_at` | 时间戳，取自内部 run |
| `error` | 失败错误信息 |
| `result` | 内部 run 的结果对象 |
| `metadata` | 附加元数据，例如 `mode=background` |

### 9.4 `POST /a2a/tasks`

后台创建任务，立即返回 task snapshot。

请求体：

```json
{
  "message": "请帮我分析这个项目",
  "session_id": "optional-session-id",
  "task_id": "optional-task-id",
  "context": {
    "agent_mode": "quick"
  }
}
```

兼容输入形状：

```json
{
  "input": {
    "parts": [
      { "text": "请帮我分析这个项目" }
    ]
  }
}
```

字段说明：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `message` | string | 条件必填 | 用户消息；若未提供，可用 `input.parts[0].text` |
| `session_id` | string | 否 | 不传时自动生成 |
| `task_id` | string | 否 | 指定 task ID；若已存在则返回 400 |
| `context` | object | 否 | 透传给内部 orchestrator；支持与 `/agentic/runs/stream` 相同的 `agent_mode`、`selected_ontology_id`、`llm_request_mode` 等字段 |

响应 `202`：

```json
{
  "task": {
    "id": "task-123",
    "session_id": "session-123",
    "state": "pending",
    "created_at": null,
    "updated_at": null,
    "completed_at": null,
    "error": null,
    "result": null,
    "metadata": {
      "mode": "background"
    }
  }
}
```

### 9.5 `GET /a2a/tasks/{task_id}`

查询单个 task 快照。

可选查询参数：

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `session_id` | string | 否 | 可显式提供；不传时服务会尝试从 run metadata 回推 |

响应 `200`：

```json
{
  "task": {
    "id": "task-123",
    "session_id": "session-123",
    "state": "completed",
    "created_at": "2026-04-16T04:00:00+00:00",
    "updated_at": "2026-04-16T04:00:05+00:00",
    "completed_at": "2026-04-16T04:00:05+00:00",
    "error": null,
    "result": { "output": "..." },
    "metadata": {
      "session_id": "session-123",
      "user_id": "admin",
      "workspace_id": "demo-workspace"
    }
  }
}
```

不存在时返回 `404`，但仍带 task 结构：

```json
{
  "task": {
    "id": "task-404",
    "session_id": null,
    "state": "not_found",
    "created_at": null,
    "updated_at": null,
    "completed_at": null,
    "error": "Task not found",
    "result": null,
    "metadata": {}
  }
}
```

### 9.6 `DELETE /a2a/tasks/{task_id}`

取消任务。

响应 `200`：

```json
{
  "success": true,
  "task": {
    "id": "task-123",
    "session_id": "session-123",
    "state": "canceled",
    "created_at": "2026-04-16T04:00:00+00:00",
    "updated_at": "2026-04-16T04:00:01+00:00",
    "completed_at": "2026-04-16T04:00:01+00:00",
    "error": null,
    "result": null,
    "metadata": { "...": "..." }
  }
}
```

响应 `404`：

```json
{ "error": "Task not found" }
```

### 9.7 `GET /a2a/tasks/{task_id}/events`

回放一个 task 已持久化的事件，并将内部 `agent.v2` 事件投影为 A2A 风格 `task.update`。

查询参数：

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `session_id` | string | 否 | 不传时服务会先根据 `task_id` 回推 |

返回格式：

```text
event: task.update
data: { ... }
```

单条 `task.update` 数据结构：

```json
{
  "task_id": "task-123",
  "session_id": "session-123",
  "event_type": "tool.started",
  "timestamp": 1700000000000,
  "source": {
    "node": "agent_loop",
    "component": "tool"
  },
  "payload": {
    "tool_name": "ontology_context"
  }
}
```

说明：

- 这是**重放接口**，不是实时订阅接口
- 仅会输出当前 `task_id` 对应的持久化 `agent.v2` 事件

### 9.8 `POST /a2a/tasks/stream`

一边创建任务，一边直接以 A2A 风格流式返回 `task.update`。

请求体：

```json
{
  "message": "请分析这个项目",
  "session_id": "session-123",
  "task_id": "optional-task-id",
  "context": {
    "agent_mode": "quick"
  }
}
```

说明：

- `session_id` 必填
- `task_id` 可选
- `context` 会透传给内部 `orchestrator.run(...)`，字段含义与 `/agentic/runs/stream` 相同
- 输出会把每个 `agent.v2` 事件即时映射为 `task.update`

SSE 格式：

```text
event: task.update
data: { ... }
```

异常时会输出：

```json
{
  "task_id": "task-123",
  "session_id": "session-123",
  "event_type": "task.error",
  "timestamp": 1700000000000,
  "source": {
    "node": "a2a",
    "component": "routes"
  },
  "payload": {
    "error": "..."
  }
}
```

---

## 10. ACP Protocol

ACP 适配层复用现有 `AgenticOrchestrator` 和 `agent-stream.v2` 内核，对外暴露 ACP 风格的 run 接口。ACP run 会写入同一个 RunStore，并通过 `X-Workspace-Id` 解析工作空间。

### 10.1 Agent Detail `GET /acp/agent`

公开发现接口，无需认证。响应包含 agent 名称、版本、能力、认证方式、ACP 端点和当前解析到的 `workspace_id`。

响应 `200`：

```json
{
  "name": "Onto-Agent",
  "version": "1.0.0",
  "description": "基于 Jiuwen ReAct 的本体查询智能体",
  "capabilities": {
    "supported_content_types": ["text/plain", "application/json"],
    "supported_modes": ["sync", "async", "stream"],
    "features": [
      "ontology_query",
      "entity_search",
      "relation_search",
      "multi_turn_conversation"
    ]
  },
  "authentication": {
    "type": "jwt",
    "scheme": "bearer"
  },
  "endpoints": {
    "agent": "http://localhost:5055/acp/agent",
    "runs": "http://localhost:5055/acp/runs",
    "health": "/health"
  },
  "metadata": {
    "workspace_id": "default",
    "protocol_version": "1.0.0"
  }
}
```

### 10.2 Run 请求结构

ACP run 从 `messages` 中抽取 `role=user` 且 `parts[].type=text` 的内容作为用户输入；非文本 part 当前不会进入主运行输入。

```json
{
  "messages": [
    {
      "role": "user",
      "parts": [
        {
          "type": "text",
          "content": "请分析这个本体",
          "content_type": "text/plain"
        }
      ]
    }
  ],
  "context": {
    "session_id": "optional-session-id",
    "selected_ontology_id": "optional-ontology-id"
  },
  "metadata": {},
  "lang": "zh"
}
```

### 10.3 Run 端点

| 端点 | 说明 |
| --- | --- |
| `POST /acp/runs` | 创建 pending run，后续再执行 |
| `GET /acp/runs` | 列出当前用户的 ACP run |
| `GET /acp/runs/{run_id}` | 查询 run 状态 |
| `POST /acp/runs/{run_id}/execute` | 同步执行已创建 run，收集完成后返回 JSON |
| `POST /acp/runs/{run_id}/stream` | 对已创建 run 执行并返回 ACP SSE |
| `POST /acp/runs/execute_stream` | 创建 run 并立即流式执行 |
| `POST /acp/runs/{run_id}/await` | 对 awaiting run 提交用户响应并同步继续 |
| `POST /acp/runs/{run_id}/interact_stream` | 对 awaiting run 提交用户响应并流式继续 |
| `DELETE /acp/runs/{run_id}` | 取消 run |

### 10.4 ACP SSE 事件

ACP 流式端点返回 `text/event-stream`，并把内部 `agent.v2` 事件投影为 ACP 事件：

| 事件 | 来源 |
| --- | --- |
| `run.started` | `turn.started` |
| `message_delta` | content `token.delta` |
| `thinking` | reasoning `token.delta`、工具启动和工具进度 |
| `result` | `tool.finished` |
| `answer` | 成功 `turn.finished` 的 `final_output` |
| `progress` | `dag_performance` |
| `run.awaiting` | `request_user_interaction` / `turn.finished(status=awaiting_input)` |
| `run.failed` | 失败 `turn.finished` 或执行异常 |
| `run.completed` | 流式执行正常结束 |

说明：

- `ontology_context` 是内部上下文工具，ACP 事件映射会隐藏它的工具事件。
- `workspace runtime_config.acp_suppress_script_patterns` 可按逗号配置需要在 ACP 流里隐藏的工具名或 `execute_script.operation_key`。
- ACP 当前复用主 agent loop；工具是否允许 ACP 调用由 `ToolSpec.allowed_callers` 声明，但实际运行仍进入同一个编排器和主循环。

---

## 11. agent-stream.v2 协议

### 11.1 SSE 事件外层

主运行流使用：

```text
event: agent.v2
data: { ... }
```

### 11.2 Envelope 结构

```json
{
  "protocol": "agent-stream.v2",
  "event_id": "run-123:1",
  "seq": 1,
  "ts": 1700000000000,
  "session_id": "session-123",
  "run_id": "run-123",
  "turn_id": 1,
  "type": "token.delta",
  "source": {
    "node": "agent_loop",
    "component": "llm"
  },
  "payload": {
    "channel": "content",
    "text": "hello"
  }
}
```

字段说明：

| 字段 | 说明 |
| --- | --- |
| `protocol` | 固定为 `agent-stream.v2` |
| `event_id` | 事件唯一 ID，格式通常为 `{run_id}:{seq}` |
| `seq` | 同一 run 内递增序号 |
| `ts` | 毫秒级时间戳 |
| `session_id` | 会话 ID |
| `run_id` | 运行 ID |
| `turn_id` | 当前 turn 编号 |
| `type` | 事件类型 |
| `source` | 事件来源节点与组件 |
| `payload` | 事件具体内容 |

### 11.3 事件类型总览

当前完整事件集合：

- `turn.started`
- `turn.finished`
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

### 11.4 Turn 事件

#### `turn.started`

payload 字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `user_preview` | string | 用户输入预览 |

#### `turn.finished`

payload 字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `status` | string | `success` / `error` / `cancelled` |
| `final_output` | any | 最终输出 |
| `error` | string | 错误信息 |
| `cancel_reason` | string | 取消原因 |
| `usage` | object | token 使用等统计 |

### 11.5 Token 与推理事件

#### `token.delta`

payload 字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `channel` | string | `content` 或 `reasoning` |
| `text` | string | 增量文本 |
| `token_index` | int | token 序号 |
| `reasoning_block_id` | string | 推理块 ID |

#### `reasoning.summary`

payload 字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `channel` | string | 固定为 `reasoning` |
| `summary` | string | 推理摘要文本 |
| `reasoning_block_id` | string | 对应的推理块 ID |

说明：

- `token.delta` 可持续输出细粒度推理文本
- `reasoning.summary` 用于输出归纳后的推理摘要，更适合 UI 展示

### 11.6 Tool 事件

#### `tool.started`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `tool_call_id` | string | 工具调用 ID |
| `tool_name` | string | 工具名称 |
| `input` | object | 工具输入 |
| `message` | string | 启动提示文本 |

#### `tool.progress`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `tool_call_id` | string | 工具调用 ID |
| `tool_name` | string | 工具名称 |
| `status` | string | 阶段状态 |
| `message` | string | 进度文本 |
| `progress` | object | 附加进度信息 |

#### `tool.finished`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `tool_call_id` | string | 工具调用 ID |
| `tool_name` | string | 工具名称 |
| `status` | string | `completed` / `error` |
| `output_preview` | string | 输出预览 |
| `output_truncated` | boolean | 预览是否被截断 |
| `error` | string | 错误信息 |
| `elapsed_ms` | int | 耗时毫秒 |

### 11.7 Subagent 事件

#### `subagent.started`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `subagent_id` | string | 子 agent ID |
| `agent_type` | string | 子 agent 类型 |
| `task_id` | string | 任务 ID |
| `task` | string | 任务描述 |
| `task_index` | int | 任务索引 |
| `group_id` | string | 所属 group ID |
| `parent_subagent_id` | string | 父子 agent ID |
| `depth` | int | 嵌套深度 |
| `root_tool_call_id` | string | 根工具调用 ID |
| `parent_tool_call_id` | string | 父工具调用 ID |

#### `subagent.delta`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `subagent_id` | string | 子 agent ID |
| `agent_type` | string | 子 agent 类型 |
| `task_id` | string | 任务 ID |
| `channel` | string | `content` / `reasoning` / `log` |
| `text` | string | 增量文本 |
| `index` | int | 片段索引 |
| `group_id` | string | 所属 group ID |
| `parent_subagent_id` | string | 父子 agent ID |
| `depth` | int | 嵌套深度 |
| `root_tool_call_id` | string | 根工具调用 ID |
| `parent_tool_call_id` | string | 父工具调用 ID |

#### `subagent.progress`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `subagent_id` | string | 子 agent ID |
| `agent_type` | string | 子 agent 类型 |
| `task_id` | string | 任务 ID |
| `status` | string | 进度状态 |
| `step` | string | 当前步骤 |
| `meta` | object | 扩展元数据 |
| `group_id` | string | 所属 group ID |
| `parent_subagent_id` | string | 父子 agent ID |
| `depth` | int | 嵌套深度 |
| `root_tool_call_id` | string | 根工具调用 ID |
| `parent_tool_call_id` | string | 父工具调用 ID |

#### `subagent.finished`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `subagent_id` | string | 子 agent ID |
| `agent_type` | string | 子 agent 类型 |
| `task_id` | string | 任务 ID |
| `status` | string | `completed` / `error` |
| `result` | any | 子 agent 结果 |
| `error` | string | 错误信息 |
| `group_id` | string | 所属 group ID |
| `parent_subagent_id` | string | 父子 agent ID |
| `depth` | int | 嵌套深度 |
| `root_tool_call_id` | string | 根工具调用 ID |
| `parent_tool_call_id` | string | 父工具调用 ID |

### 11.8 Subagent Group 事件

#### `subagent_group.started`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `group_id` | string | 任务组 ID |
| `depth` | int | 嵌套深度 |
| `parent_subagent_id` | string | 父子 agent ID |
| `root_tool_call_id` | string | 根工具调用 ID |
| `parent_tool_call_id` | string | 父工具调用 ID |
| `task_count` | int | 任务总数 |
| `tasks` | list | 任务摘要列表，元素常含 `task_id` / `task` / `task_index` |

#### `subagent_group.finished`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `group_id` | string | 任务组 ID |
| `depth` | int | 嵌套深度 |
| `status` | string | `completed` / `error` |
| `task_count` | int | 任务总数 |
| `completed_count` | int | 完成数 |
| `error_count` | int | 失败数 |

---

## 12. A2A `task.update` 投影格式

A2A 流式接口与事件回放接口都会把内部 `agent.v2` 事件映射为简化版 `task.update`：

```json
{
  "task_id": "run-123",
  "session_id": "session-123",
  "event_type": "tool.started",
  "timestamp": 1700000000000,
  "source": {
    "node": "agent_loop",
    "component": "tool"
  },
  "payload": {
    "tool_call_id": "call_1",
    "tool_name": "ontology_context"
  }
}
```

映射规则：

- `task_id <- agent.v2.data.run_id`
- `session_id <- agent.v2.data.session_id`
- `event_type <- agent.v2.data.type`
- `timestamp <- agent.v2.data.ts`
- `source <- agent.v2.data.source`
- `payload <- agent.v2.data.payload`

---

## 13. 实践建议

- 新前端/客户端若直接消费主协议，优先使用 `POST /agentic/runs/stream`
- 如果你需要更标准的 task 视图或 background task 管理，优先使用 `/a2a/*`
- 如果你接入的是 ACP client 或需要 ACP 事件形状，使用 `/acp/*`
- 使用多工作空间时，建议所有 `/agentic/*`、`/a2a/*` 与 `/acp/*` 请求都显式带上 `X-Workspace-Id`
