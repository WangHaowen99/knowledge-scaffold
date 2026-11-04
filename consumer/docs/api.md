# Backend API Reference

Base URL: `http://localhost:5055`

本文档以当前代码实现为准，覆盖以下路由前缀：

- `/auth/*`：认证与用户管理接口
- `/agentic/*`：主业务接口（runs / sessions / skills / ontologies / workspaces / ontology data sources / operations / performance）
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
- 普通已认证用户可调用 `POST /agentic/workspaces` 创建工作空间；服务端会强制将 owner 设为当前用户，且普通用户不能主动替换已有全局默认工作空间
- workspace member 只能读取、使用资源和执行评测运行，不能修改 workspace 资源
- 全局 `admin` 或当前 workspace owner 可执行：
  - `POST /agentic/ontologies/import-zip`
  - `PUT /agentic/ontologies/{ontology_id}/active-version`
  - `PUT /agentic/ontologies/{ontology_id}/data-source`
  - `DELETE /agentic/ontologies/{ontology_id}/data-source`
  - `PUT /agentic/ontologies/{ontology_id}/status`
  - `DELETE /agentic/ontologies/{ontology_id}`
  - `DELETE /agentic/ontologies/{ontology_id}/versions/{version}`
  - `PUT /agentic/ontologies/{ontology_id}/file`
  - `POST /agentic/ontologies/{ontology_id}/upload`
  - `POST /agentic/ontologies/{ontology_id}/data-source/test`
  - `PATCH /agentic/workspaces/{workspace_id}`（仅全局 admin 可转移 owner）
  - `POST /agentic/workspaces/{workspace_id}/members/validate`
  - `PUT /agentic/workspaces/{workspace_id}/members`
  - `DELETE /agentic/workspaces/{workspace_id}`（workspace owner 只能删除自己的空间）
- `admin` 才能访问：
  - `POST /agentic/skills/upload`
  - `DELETE /agentic/skills/{skill_name}`
  - `POST /agentic/workspaces/{workspace_id}/default`
  - `PATCH /agentic/workspaces/{workspace_id}/runtime-config`
  - `PATCH /agentic/workspaces/{workspace_id}/observability-config`
  - `POST /agentic/workspaces/{workspace_id}/observability-config/test`
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
  "password": "<管理员当前密码>"
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
    "password_change_required": false
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
- 首次空用户库使用 `AUTH_BOOTSTRAP_ADMIN_PASSWORD` 创建管理员账号 `admin`；未配置或不符合密码策略时 auth-server 拒绝启动。该账号可直接使用引导密码登录。

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
    "password_change_required": false
  }
}
```

### 2.3 `GET /auth/users` (`admin`)

列出全部用户。

响应 `200`：

```json
{
  "users": [
    { "username": "admin", "role": "admin", "password_change_required": false },
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
| `password` | string | 是 | 登录密码，必须满足复杂度要求 |

说明：

- 新创建的用户角色固定为 `user`，不支持通过接口创建管理员
- 新用户可直接使用管理员设置的密码登录

响应 `201`：

```json
{
  "user": {
    "username": "newuser",
    "role": "user",
    "password_change_required": false
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
- 被重置密码的用户可直接使用新密码登录
- 不能通过该接口重置当前登录用户自己的密码；当前用户修改密码必须使用 `/auth/change-password`

响应 `200`：

```json
{
  "user": {
    "username": "newuser",
    "role": "admin",
    "password_change_required": false
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

### 3.1 `POST /agentic/runs`

创建并启动一个 Run。该接口只返回 JSON admission；客户端随后使用响应中的 `links.events` 读取 SSE。请求必须携带唯一的 `Idempotency-Key`，`run_id` 和 `turn_id` 始终由服务端分配。

```http
POST /agentic/runs
Idempotency-Key: request-demo-0001
Content-Type: application/json
```

```json
{
  "session_id": "session_demo",
  "request": "分析一条合成输入",
  "agent_mode": "quick",
  "llm_request_mode": "stream",
  "model_profile_id": "",
  "arguments": null,
  "include_trace": false
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `session_id` | string | 是 | 会话 ID；同一 session 同时只允许一个非终态 Run |
| `request` | string | 是 | 当前 Turn 的用户输入 |
| `agent_mode` | string | 否 | `quick`、`thinking` 或 `multi_agent`，默认 `quick` |
| `llm_request_mode` | string | 否 | `stream` 或 `sync`；仅影响上游模型调用方式，不改变 HTTP create-then-stream 契约 |
| `model_profile_id` | string | 否 | 模型配置 ID；空值使用工作空间默认配置 |
| `arguments` | object/null | 否 | 本次运行的结构化参数 |
| `include_trace` | boolean | 否 | 是否在允许的终态事件中返回脱敏 trace |
| `interaction_response` | object/null | 否 | 回答上一 Turn 的 HITL checkpoint，见 3.4 |

首次创建响应 `202`：

```json
{
  "created": true,
  "run_id": "agentic-run-demo",
  "session_id": "session_demo",
  "turn_id": 1,
  "status": "queued",
  "last_seq": 0,
  "links": {
    "self": "/agentic/runs/agentic-run-demo",
    "events": "/agentic/runs/agentic-run-demo/events",
    "cancel": "/agentic/runs/agentic-run-demo/cancel"
  }
}
```

相同 key 和相同请求的精确重试返回 `200`、`created=false`，且不会启动第二个执行任务。相同 key 搭配不同请求返回 `409 IDEMPOTENCY_CONFLICT`；同 session 已有活跃 Run 时返回 `409 SESSION_BUSY`。容量耗尽返回 `429 RUNTIME_CAPACITY_EXCEEDED`，服务停止准入时返回 `503 RUNTIME_DRAINING`。

### 3.2 `GET /agentic/runs/{run_id}`

读取 live snapshot 或已持久化的 Turn 状态。状态为 `queued`、`running`、`awaiting_input`、`completed`、`failed` 或 `cancelled`。

```http
GET /agentic/runs/agentic-run-demo
```

响应包含 `run_id`、`session_id`、`turn_id`、`status`、`last_seq`、时间戳、`result`、`error`、`pending_interaction` 和 links。Run 不存在或不属于当前用户/工作空间时返回 `404 RUN_NOT_FOUND`。

### 3.3 `GET /agentic/runs/{run_id}/events`

连接或重连一个 Run 的 SSE。首次读取使用：

```http
GET /agentic/runs/agentic-run-demo/events?after_seq=0
```

后续重连可以递增 `after_seq`，也可以发送 `Last-Event-ID`；两者同时存在时必须相等。服务只回放 `seq` 大于游标的事件，并推送到唯一的 `turn.finished` 终态事件。事件格式：

```text
id: 1
event: turn.started
data: {"schema_version":"1.0","event_id":"...","run_id":"agentic-run-demo","seq":1,"event_type":"turn.started","status":"running","payload":{}}
```

回放窗口在服务重启后不可用时返回 `410 EVENT_REPLAY_UNAVAILABLE`；最终 Turn 仍可通过 3.2 和 session 历史读取。订阅容量耗尽返回 `429 RUNTIME_CAPACITY_EXCEEDED`。

### 3.4 HITL 回答创建新 Run/Turn

当事件流包含 `interaction.requested`，且 `turn.finished.status=awaiting_input` 时，客户端必须再次调用 `POST /agentic/runs`。回答沿用同一 `session_id`，使用新的 `Idempotency-Key`，并携带 checkpoint 的 `interaction_id`：

```json
{
  "session_id": "session_demo",
  "request": "选择合成选项 A",
  "interaction_response": {
    "interaction_id": "interaction_demo",
    "type": "single_choice",
    "option_id": "option_a",
    "value": "value_a"
  }
}
```

服务端消费旧 checkpoint，并为回答创建新的 `run_id` 和递增的 `turn_id`。精确重试复用已创建的回答 Run；不同回答竞争同一 checkpoint 时返回 `409 INTERACTION_ALREADY_CONSUMED`。普通消息不能绕过未消费的 checkpoint。

### 3.5 `POST /agentic/runs/{run_id}/cancel`

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

响应 `200` 返回取消后的 Run snapshot。Run 不存在时返回 `404 RUN_NOT_FOUND`。终态竞争由服务端统一仲裁；取消成功后事件流仍以唯一的 `turn.finished` 收口。

### 3.6 `POST /agentic/runs/sync`

同步兼容入口。服务端通过 canonical Run 平台创建并执行 Run，等待终态后一次性返回 JSON；该接口不要求 `Idempotency-Key`，也不支持 HITL。需要断点续传、客户端重连或交互式运行时，使用 3.1 至 3.3 的 create-then-stream 契约。

请求示例：

```json
{
  "session_id": "session_demo",
  "request": "分析一条合成输入",
  "context": {
    "agent_mode": "thinking",
    "llm_request_mode": "stream"
  },
  "arguments": null,
  "include_trace": true
}
```

`session_id` 可省略，服务端会生成 `sync_<随机值>`。`context` 兼容读取 `agent_mode`、`llm_request_mode` 和 `model_profile_id`；这三个字段也可以放在请求顶层。同步入口不设置服务端整段 Run 超时，会持续等待终态；请求任务被客户端取消时，服务端会取消仍在运行的 Run。

成功终态响应 `200`：

```json
{
  "status": "success",
  "final_answer": "合成回答",
  "session_id": "session_demo",
  "run_id": "agentic-run-demo",
  "execution_trace": {
    "tool_calls": ["synthetic_tool"]
  }
}
```

`status` 可能为 `success`、`error` 或 `cancelled`。同步入口关闭 HITL；Runtime 发起用户交互时会按不支持交互处理为 `error`。Runtime 业务失败仍返回 HTTP `200`，并通过顶层字符串 `error` 表达公开错误；参数错误、容量限制和服务停止准入使用对应的 `4xx/5xx`。`include_trace=true` 时，`execution_trace` 只汇总已经过公共事件适配器脱敏的工具名称与失败信息。

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

本组接口只读取当前机器上由工作空间观测配置 `mode=local` 写出的新 local perf JSONL schema。默认扫描最近 24 小时，最大扫描窗口为 7 天；当对应工作空间的本地观测关闭、当前模式为 Langfuse、日志过期或日志文件被删除时，返回结果可能为空。

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

#### `GET /agentic/sessions/{session_id}/performance/speedscope`

Downloads the current session's combined `.speedscope.json` trace. Regular users may download only their own sessions; workspace administrators may download any session in the workspace. The trace does not contain prompts, answers, or tool output. When observability is disabled or the trace is absent, the endpoint returns `404 SPEEDSCOPE_NOT_FOUND`.

#### `GET /agentic/performance/runs/{run_id}`

返回单次运行的耗时拆分、iteration、LLM、工具、runtime phase 和事件时间线。

- 普通用户必须同时传 `session_id`，且该 session 的执行记录必须能关联该 `run_id`。
- 管理员可从跨工作空间聚合的本地观测页直接按 run 下钻；该页面是分析视图，不是全局观测配置。
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
      "index_path": "/abs/path/.../index.md",
      "business_data": {
        "configured": true,
        "ready": true,
        "mode": "direct_provider",
        "provider": "nl2sql_http",
        "updated_at": "2026-07-15T10:00:00+00:00"
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

### 6.7.1 `PUT /agentic/ontologies/{ontology_id}/status` (`admin`)

启用或停用 ontology。

请求体（JSON）：

```json
{ "enabled": false }
```

行为说明：

- `enabled` 为必填布尔值；缺失返回 `400`
- 停用后 ontology 不再参与 Agent 检索上下文

请求体校验失败响应 `400`：`{ "error": "\`enabled\` is required" }` 或 `{ "error": "\`enabled\` must be a boolean" }`

响应 `200`：

```json
{
  "success": true,
  "ontology_id": "example-domain-operations-manual",
  "enabled": false
}
```

响应 `404`：`{ "error": "Ontology not found" }`

### 6.7.2 `GET|PUT /agentic/ontologies/{ontology_id}/capabilities/business-checkpoint`

读取或修改知识包的业务阶段 CheckPoint 开关。开关默认关闭；GET 对已认证工作空间成员开放，PUT 仅允许工作空间 owner 或平台管理员。

PUT 请求体：

```json
{
  "enabled": true,
  "expected_revision": "<revision returned by GET or ontology list>"
}
```

开启前，当前激活版本必须包含可读取的 `assets/business-checkpoint-template.md`，否则返回 `409`。revision 已变化时同样返回 `409`。关闭后，新构建的 LLM 工具列表不再包含 `report_business_checkpoint`；已缓存的工具调用也会在执行时被拒绝。

成功响应包含 `enabled`、新 `revision` 和 `updated_at`。知识包导入后该开关重置为关闭，删除知识包时同步清理配置。

### 6.7.3 `PUT /agentic/ontologies/{ontology_id}/active-version` (`admin`)

切换 ontology 的激活版本。

请求体（JSON）：

```json
{ "version": "2.0.0" }
```

行为说明：

- `version` 必填且必须匹配 `[A-Za-z0-9][A-Za-z0-9._-]*`；非法版本返回 `400`
- 目标版本不存在时返回 `400`（`ValueError` 消息）

响应 `200`：

```json
{
  "success": true,
  "ontology_id": "example-domain-operations-manual",
  "active_version": "2.0.0",
  "version": "2.0.0",
  "versions": ["1.0.0", "2.0.0"]
}
```

响应 `404`：`{ "error": "Ontology not found" }`

### 6.7.4 `GET /agentic/ontologies/{ontology_id}/graph`

返回 ontology 指定版本的节点与边图谱快照。

查询参数：

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `version` | string | 否 | 目标版本；不传时使用当前激活版本 |

行为说明：

- 节点来源：`graph/nodes`（存在时）合并 `operations/*.md` 与 `objects/*.md` 自动生成的节点
- 边来源：`graph/edges`（存在时）合并自动生成的流程-对象依赖边

响应 `200`：

```json
{
  "ontology_id": "example-domain-operations-manual",
  "name": "示例业务执行手册",
  "description": "...",
  "version": "2.0.0",
  "nodes": [
    {
      "id": "op-query-part",
      "type": "operation",
      "label": "查询零件",
      "source": "operations/QueryPart.md"
    }
  ],
  "edges": [
    {
      "source": "op-query-part",
      "target": "obj-part",
      "type": "uses"
    }
  ],
  "stats": {
    "node_count": 12,
    "edge_count": 18,
    "operation_count": 8,
    "object_count": 4
  }
}
```

响应 `404`：`{ "error": "Ontology not found" }` 或 `{ "error": "Ontology version not found" }`

### 6.7.5 `GET /agentic/ontologies/{ontology_id}/tree`

返回指定版本目录的目录树，供编辑器/浏览器展示。

查询参数：

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `version` | string | 否 | 目标版本；不传时使用当前激活版本 |

行为说明：

- 每个条目包含 `path`、`type`（`dir`/`file`）、`name` 与 `children`（目录递归展开）
- 与编辑器文件列表共用同一套过滤：排除 `origin_data`、`dataset(s)`、`__pycache__`、隐藏目录等私有/运行时目录，以及任何符号链接
- `excluded` 列出当前生效的排除目录名清单

响应 `200`：

```json
{
  "ontology_id": "example-domain-operations-manual",
  "version": "2.0.0",
  "excluded": ["datasets", "dataset", "origin-data", "origin_data"],
  "entries": [
    {
      "path": "operations",
      "name": "operations",
      "type": "dir",
      "children": [
        {
          "path": "operations/QueryPart.md",
          "name": "QueryPart.md",
          "type": "file"
        }
      ]
    }
  ]
}
```

安全错误响应 `400`：`{ "error": "Invalid file path" }`

### 6.7.6 `GET /agentic/ontologies/{ontology_id}/raw-file`

读取指定版本内的单个文本文件内容。

查询参数：

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `path` | string | 是 | 版本目录内相对路径，如 `operations/QueryPart.md` |
| `version` | string | 否 | 目标版本；不传时使用当前激活版本 |

行为说明：

- 只允许读取版本目录内的普通文件；绝对路径、目录穿越（`..`）、含 `.`/空段的非规范路径、符号链接（文件或祖先目录）均拒绝
- 只返回 UTF-8 文本；二进制文件、非 UTF-8、超过在线阅读上限的大文件拒绝
- 版本目录本身不允许是符号链接

响应 `200`：

```json
{
  "ontology_id": "example-domain-operations-manual",
  "version": "2.0.0",
  "path": "operations/QueryPart.md",
  "name": "QueryPart.md",
  "size": 512,
  "content": "# Query part\n"
}
```

安全错误响应：

| 状态码 | `error` |
| --- | --- |
| `400` | `Invalid file path` |
| `400` | `Binary file is not viewable` |
| `400` | `File too large to view online` |
| `400` | `File encoding must be UTF-8` |
| `404` | `File not found` |
| `500` | `Failed to read file` |

### 6.8 `POST /agentic/ontologies/import-zip` (`admin`)

上传 `.zip` 包并导入为 ontology；必要时自动创建或覆盖目标 ontology 目录。

请求类型：`multipart/form-data`

表单字段：

- `file=<zip>`
- `overwrite=true|false`（可选）

行为说明：

- 仅支持 `.zip`
- 支持 v0.1 ontology 包：`manifest.yaml` 声明 `kind: ontology-package`、`schema_version: 0.1`，目录清单使用各目录的 `index.md`
- 服务端不再接受源格式包（`object`/`logic` 结构）。旧格式包需先用离线转换脚本转为 v0.1 格式后上传，见《ontology 包转换指南》；安装目录不生成或保留 `ONTOLOGY.md`、`objects-list.md`、`operations-list.md`、`scripts-list.md`
- 直接使用 `manifest.yaml` 中的元数据
- 如果目标 ontology 已存在且未传 `overwrite=true`，会返回确认型冲突
- 传 `overwrite=true` 时，会以新导入结果整体替换目标 ontology 目录，不保留旧残留文件

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
  "ontology_id": "example-domain-operations-manual",
  "ontology_name": "示例业务执行手册",
  "version": "1.0.0",
  "active_version": "1.0.0",
  "source_root": "/tmp/.../unzipped/project",
  "objects_count": 12,
  "operations_count": 18,
  "objects_index_path": "/abs/path/.../objects/index.md",
  "operations_index_path": "/abs/path/.../operations/index.md",
  "business_data_configured": false,
  "warnings": {
    "objects": [],
    "objects_index": [],
    "operations": [],
    "object_aliases": [],
    "field_aliases": []
  },
  "normalization_warnings": [],
  "preview": {
    "source": {
      "files": [
        {
          "path": "objects/index.md",
          "size": 128,
          "content": "...",
          "truncated": false,
          "binary": false
        }
      ],
      "total_files": 12,
      "truncated": false
    },
    "result": {
      "files": [],
      "total_files": 0,
      "truncated": false
    }
  }
}
```

说明：

- 新导入 ontology 不继承默认连接，`business_data_configured` 初始为 `false`；需要在导入后显式配置该 ontology 的数据源
- `warnings.object_aliases`：对象别名中的重复、空值等非阻断问题；不会阻止导入
- `warnings.field_aliases`：字段别名中的重复、空值等非阻断问题；不会阻止导入
- `normalization_warnings`：v0.1 目录规范化（目录整理、引用调整等）过程中产生的非阻断提示；空列表表示没有这类提示
- `preview`：导入前后文件清单，`source` 为上传播包临时品目录，`result` 为规范化后的版本目录；`files` 仅包含前若干项，`truncated` 表示因长度被截断，`binary` 表示非文本文件（不返回 `content`）
- 对象或字段名称存在歧义、冲突等 error 时，接口返回 `400` 并在安装版本前终止导入，不会进入上述 warning 列表

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

列出当前用户在工作台中可选择的工作空间。普通用户列表仅包含自己拥有或作为成员加入的空间；该列表过滤不改变其他接口已有的授权规则。普通用户首次调用时，如果还没有自己拥有的工作空间，服务会创建名称为“默认工作空间”的个人空间；全局管理员不会触发该自动创建流程。

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
      "is_default": true,
      "owner": "admin",
      "members": [],
      "role": "admin"
    }
  ],
  "total": 1,
  "default_workspace_id": "demo-workspace",
  "preferred_workspace_id": "demo-workspace"
}
```

`default_workspace_id` 是平台管理员设置的全局默认空间；`preferred_workspace_id` 是当前用户进入平台时应优先选择的空间。普通用户返回其个人空间，管理员返回全局默认空间。前端仍会优先恢复当前账号已保存且仍可访问的选择。

普通用户首次自动创建个人空间时，认证目录不可用返回 `503 AUTH_DIRECTORY_UNAVAILABLE`，身份删除状态不可用返回 `503 USER_DELETION_STATE_UNAVAILABLE`，身份正在删除返回 `409 USER_DELETION_RECONCILIATION_PENDING`；无法分配个人空间 ID 时返回 `409 PERSONAL_WORKSPACE_PROVISIONING_FAILED`。

### 7.3 `POST /agentic/workspaces`（已认证用户；`admin` 可指定 owner）

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

- `id` 可选；不传时会根据 `name` 自动 slugify，并在冲突时追加随机后缀。匹配 `home-<20 位小写十六进制>` 或其 `-<8 位小写十六进制>` 冲突后缀形式的 ID 由系统个人空间保留，用户请求返回 `400`
- `name` 必填
- `description` 可选
- 创建或转入同一 owner 的工作空间数量受 `WORKSPACE_MAX_COUNT` 限制，默认每个 owner 50 个；`owner=None` 的公共空间不占用任何用户配额；建模影子专用接口可显式跳过本次配额校验
- `set_as_default` 可选
- 全局 admin 显式指定的非空 `owner` 必须已存在；普通用户的 owner 固定为当前登录用户，并在创建时重新校验认证目录

响应 `201`：返回创建后的 workspace 对象。

错误返回：

```json
{ "detail": "工作空间 ID 'demo-workspace' 已存在" }
```

身份引用相关错误与 owner 转移一致：负责人不存在时返回 `400 WORKSPACE_OWNER_NOT_FOUND`，正在删除时返回 `409 USER_DELETION_RECONCILIATION_PENDING`，用户删除状态不可读取时返回 `503 USER_DELETION_STATE_UNAVAILABLE`，认证目录不可用时返回 `503 AUTH_DIRECTORY_UNAVAILABLE`。

### 7.4 `GET /agentic/workspaces/{workspace_id}`

获取单个工作空间。

响应 `200`：返回 workspace 对象。

响应 `404`：

```json
{ "detail": "工作空间 'demo-workspace' 不存在" }
```

### 7.5 `PATCH /agentic/workspaces/{workspace_id}`

全局 admin 或该 workspace owner 可更新名称和描述；只有全局 admin 可通过 `owner` 转移所有权。非空 `owner` 必须是认证目录中已存在的用户名；认证目录不可用时拒绝写入。

请求体：

```json
{
  "name": "新的名称",
  "description": "新的描述"
}
```

响应 `200`：返回更新后的 workspace 对象。

错误响应：

- `400 WORKSPACE_OWNER_NOT_FOUND`：目标负责人不存在
- `409 USER_DELETION_RECONCILIATION_PENDING`：目标用户正在删除或等待清理
- `503 USER_DELETION_STATE_UNAVAILABLE`：用户删除状态文件损坏或暂时无法读取
- `503 AUTH_DIRECTORY_UNAVAILABLE`：认证目录暂不可用，无法完成最终校验
- `400`：目标 owner 已达到 `WORKSPACE_MAX_COUNT` 工作空间上限，所有权不会改变

#### `POST /agentic/workspaces/{workspace_id}/members/validate`

在前端把用户名加入成员草稿前即时校验。全局 admin 或 workspace owner 可调用；该接口只校验，不修改 `workspace.json`。保存时仍会由 PUT 接口重新做最终校验，因此不能用即时校验结果绕过用户删除并发。

请求体必须是 JSON object；数组、字符串或数字返回 `400`。

```json
{ "username": "analyst" }
```

成功响应 `200`：

```json
{ "valid": true, "username": "analyst" }
```

#### `PUT /agentic/workspaces/{workspace_id}/members`

全量替换成员列表。全局 admin 或 workspace owner 可调用；成员用户名必须已存在且不得重复。服务端在持有身份生命周期锁期间重新读取认证目录并完成最终写入，防止用户删除与 ACL 保存并发制造孤儿成员。

请求体必须是 JSON object；数组、字符串或数字返回 `400`。

```json
{ "members": ["analyst", "operator"] }
```

错误响应：

- `400 WORKSPACE_MEMBER_NOT_FOUND`：一个成员不存在
- `400 WORKSPACE_MEMBERS_NOT_FOUND`：多个成员不存在，`usernames` 返回缺失用户名
- `409 USER_DELETION_RECONCILIATION_PENDING`：成员正在删除或等待清理
- `503 USER_DELETION_STATE_UNAVAILABLE`：用户删除状态文件损坏或暂时无法读取
- `503 AUTH_DIRECTORY_UNAVAILABLE`：认证目录暂不可用；非空成员列表不会写入

为便于修复存量 ACL，提交空数组 `{ "members": [] }` 不依赖认证目录，可在认证服务异常时清空成员引用。

无 owner 的存量 workspace 保持公共可见，服务不会在迁移时自动私有化；管理员应根据审计报告显式设置 owner 和 members。

### 7.6 `DELETE /agentic/workspaces/{workspace_id}`（全局 `admin` 或 workspace owner）

删除工作空间。workspace owner 只能删除自己拥有的空间；普通 member 不能删除。

响应 `200`：

```json
{
  "success": true,
  "id": "demo-workspace"
}
```

说明：

- 全局 admin 删除默认 workspace 前仍必须先切换默认 workspace
- workspace owner 可删除自己拥有的默认 workspace；删除后系统显式保持“无全局默认”状态，不会自动把其他私有 workspace 提升为默认

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

### 7.9 本体业务数据源

每个 ontology 在当前 workspace 下拥有一份私有业务数据源。运行时不会从全局配置、默认连接或其他 ontology 隐式继承 provider；`business_data_search` 只解析当前 `workspace_id + ontology_id` 对应的数据源。

Graph provider 配置：

| Provider | 必填配置 | 可选配置 | 隔离要求 |
| --- | --- | --- | --- |
| `neo4j` | `uri`、`username`、`password` | `database=neo4j`、超时/重试/结果上限 | `workspace_property=d2o_workspace_id`；`ontology_property` 默认为空，留空时不执行 Ontology 隔离 |
| `hugegraph` | `url` | `graph=hugegraph`、`graph_space`、`username/password/token`、超时/重试/结果上限 | 同样强制 Workspace 隔离；仅在 `ontology_property` 非空时执行 Ontology 隔离 |

密码和 token 加密落盘，对外读取只返回 `has_password` / `has_token`。如需按本体隔离，应显式配置 `ontology_property`，例如 `d2o_ontology_id`；其过滤值始终为当前 `ontology_id`。可先用候选配置调用 test，确认可用后再保存；MCP search 只接受已配置的 `neo4j` 或 `hugegraph` 数据源。

新增业务数据 provider 的开发步骤见 [`docs/business-data-provider-extension.md`](business-data-provider-extension.md)。

#### `GET /agentic/business-data/provider-schemas`

返回当前支持的数据源 provider 表单 schema。所有已认证用户可访问。

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
        { "name": "token", "label": "访问 Token", "type": "str", "required": true, "secret": true, "default": "" },
        { "name": "scene", "label": "场景 Scene", "type": "str", "required": true, "secret": false, "default": "" }
      ]
    }
  ]
}
```

#### `GET /agentic/ontologies/{ontology_id}/data-source`

读取当前 workspace 下某个 ontology 的私有数据源。secret 字段只返回 `has_*` 标记。

响应 `200`：

```json
{
  "ontology_id": "example-domain-operations-manual",
  "configured": true,
  "source": {
    "mode": "direct_provider",
    "provider": "nl2sql_http",
    "config": {
      "url": "https://nl2sql.example.com",
      "has_token": true,
      "scene": "example",
      "timeout_seconds": 600,
      "max_retries": 3
    },
    "created_at": "2026-05-10T10:00:00+00:00",
    "updated_at": "2026-07-15T10:00:00+00:00"
  }
}
```

尚未配置时返回 `configured=false`、`source=null`。

#### `PUT /agentic/ontologies/{ontology_id}/data-source` (`admin`)

创建或覆盖 ontology 的私有数据源。当前只接受 `mode=direct_provider`；同一 provider 更新时，secret 字段留空或省略会保留原值。

```json
{
  "mode": "direct_provider",
  "provider": "nl2sql_http",
  "config": {
    "url": "https://nl2sql.example.com",
    "token": "secret-token",
    "scene": "example"
  }
}
```

响应结构与 GET 相同，`configured=true`。

#### `DELETE /agentic/ontologies/{ontology_id}/data-source` (`admin`)

清除 ontology 的私有数据源。重复删除仍返回 `200`：

```json
{
  "success": true,
  "ontology_id": "example-domain-operations-manual",
  "configured": false
}
```

#### `POST /agentic/ontologies/{ontology_id}/data-source/test` (`admin`)

测试数据源服务可达性。请求体可传入与 PUT 相同的候选配置；请求体为空时测试已保存配置。测试不会保存候选配置。MindsDB 会请求 Project 范围内的 Agent 列表，并校验 `project_name + default_agent_name`；NL2SQL HTTP / NL2SQL Template 会携带 Scene 与 Bearer Token 请求 `/api/v1/sql`，执行 `SELECT 1`，并要求响应包含 `success=true`。

```json
{
  "success": true,
  "message": "NL2SQL 服务连接正常",
  "tested_at": "2026-07-15T10:00:00+00:00"
}
```

### 7.10 工作区观测配置

本地性能日志和 Langfuse 连接按 workspace 隔离，配置保存在：

```text
.onto-runtime/workspaces/{workspace_id}/observability_config.json
```

全局 `/api/system-config` 不提供 `observability` section。管理员跨工作空间查看本地观测数据时使用的是聚合分析接口，不会产生或修改一份全局观测配置。

#### `GET /agentic/workspaces/{workspace_id}/observability-config`

读取指定工作空间的观测配置。所有已认证且可访问该工作空间的用户均可读取；secret 不返回明文，只返回 `has_secret_key`。

响应 `200`：

```json
{
  "enabled": true,
  "mode": "local",
  "local": {
    "retention_days": 30
  },
  "speedscope": {
    "retention_days": 30
  },
  "langfuse": {
    "public_key": "",
    "has_secret_key": false,
    "base_url": "",
    "debug": false
  }
}
```

如果持久化文件仍是旧结构、JSON 损坏、字段不完整或 secret 无法解密，读取路径不会改写文件或回退到旧格式，而是返回 `409`：

```json
{
  "code": "WORKSPACE_OBSERVABILITY_MIGRATION_REQUIRED",
  "detail": "工作空间 'demo' 的观测配置不是规范格式，需要先完成迁移或人工修复",
  "workspace_id": "demo"
}
```

#### `PATCH /agentic/workspaces/{workspace_id}/observability-config` (`admin`)

增量更新指定工作空间的观测配置。保存后只影响后续运行；本地模式写入该工作空间的 runtime 目录，Langfuse 模式使用该工作空间自己的项目凭据。

请求示例：

```json
{
  "enabled": true,
  "mode": "langfuse",
  "local": {
    "retention_days": 30
  },
  "speedscope": {
    "retention_days": 30
  },
  "langfuse": {
    "public_key": "pk-example",
    "secret_key": "sk-example",
    "base_url": "https://langfuse.example.com",
    "debug": false
  }
}
```

更新时省略 `langfuse.secret_key` 会保留已保存的密钥；显式传空字符串会清空密钥。

#### `POST /agentic/workspaces/{workspace_id}/observability-config/test` (`admin`)

使用请求中的增量配置和已保存配置测试当前工作空间的观测目标，不写入配置：

- `enabled=false`：返回工作空间观测已关闭。
- `mode=local`：校验本地观测配置可用。
- `mode=langfuse`：校验 `public_key`、`secret_key`、`base_url` 并请求 Langfuse 项目接口。

### 7.11 Operation Evolution API

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
- 业务数据源不属于全局系统配置，按 `workspace_id + ontology_id` 独立存储。
- 观测不属于全局系统配置，按 workspace 通过 `/agentic/workspaces/{workspace_id}/observability-config` 管理。
- 内置默认值主要用于超时、重试、temperature、存储路径等运行参数。
- 可通过 `SYSTEM_CONFIG_PATH` 指定其他保存位置。

支持的 section：

- `llm`
- `llm_profiles`
- `storage`
- `auth`
- `agent`
- `memory`
- `web_search`

说明：旧全局 `observability` 只作为启动兼容迁移的输入；迁移后由各工作空间独立保存和使用。旧的 `LANGFUSE_*` 或 `OBSERVABILITY_*` 环境变量不再作为运行时配置入口。

### 8.1 `GET /api/system-config`

返回对前端可见的系统配置。所有已认证用户都可查看。

注意：secret 字段不会直接返回明文，而是返回 `has_*` 标记。

注意：

- 当前版本后端不再内建默认 LLM 地址与模型 ID
- 若系统尚未配置模型，则 `llm.base_url` / `llm.model` 以及 `llm_profiles.profiles` 可能为空
- 业务数据源请使用 `/agentic/ontologies/{ontology_id}/data-source`

响应 `200`：

```json
{
  "llm": {
    "base_url": "",
    "model": "",
    "has_api_key": false,
    "timeout": 600,
    "max_tokens": 32768,
    "temperature": 0.7
  },
  "llm_profiles": {
    "default_main_profile_ref": null,
    "default_task_profile_ref": null,
    "profiles": []
  },
  "storage": {
    "project_root": ".../.onto-runtime"
  },
  "auth": {
    "auth_server_url": "http://auth-server:5056",
    "has_auth_server_admin_token": true,
    "auth_server_audience": "knowledge-scaffold",
    "auth_server_issuer": ""
  },
  "agent": {
    "llm_connection_max_retries": 3,
    "main_agent_max_iterations": 50,
    "sub_agent_max_iterations": 20,
    "sub_agent_max_concurrency": 6,
    "sub_agent_max_depth": 10,
    "parallel_default_max_workers": 10,
    "context_max_rounds": 10
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

若持久化配置仍包含旧模型字段、明文 secret，或密文无法使用当前密钥和 context 解密，读取不会改写文件或回退到其他配置，而是返回 `409`：

```json
{
  "code": "SYSTEM_CONFIG_SECRET_MIGRATION_REQUIRED",
  "detail": "系统配置包含明文 secret，需要先完成安全迁移"
}
```

`code` 可能为：

- `SYSTEM_CONFIG_MODEL_MIGRATION_REQUIRED`
- `SYSTEM_CONFIG_SECRET_MIGRATION_REQUIRED`
- `SYSTEM_CONFIG_SECRET_UNSAFE`
- `SYSTEM_CONFIG_INVALID`

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

持久化配置不可安全读取时，本接口返回与 `GET /api/system-config` 相同的 `409` 错误契约。

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
    "max_tokens": 32768,
    "temperature": 0.7
  }
}
```

说明：

- `section` 仅支持：`llm | llm_profiles | storage | auth | agent | memory | web_search | direct_execution | skill_evolution`
- `data` 必须为对象
- secret 清空不能直接传空字符串，需使用对应清除字段：
  - `llm.clear_api_key`
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
- 导入时可包含 `llm` section；`task_llm` 与 `llm_profiles.default_profile_id` 不属于可写入格式

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

- `llm`：测试 OpenAI 兼容 `chat/completions`
- `llm_profiles`：测试指定模型 profile（若请求里带 `profile_id`）或默认模型 profile
- `storage` / `auth` / `agent`：仅做配置校验，返回 `配置校验通过`

工作空间观测连接测试使用 `POST /agentic/workspaces/{workspace_id}/observability-config/test`，不通过本接口处理。

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
| `default_main_profile_ref` | object \| null | 默认主模型引用，格式为 `{ "scope": "global", "id": "..." }` |
| `default_task_profile_ref` | object \| null | 默认轻量模型引用；为空时沿用主模型 |
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

#### `storage`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `project_root` | string | `.onto-runtime` 根目录 |

#### `auth`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `auth_server_url` | string | 独立 auth-server 地址（必填），如 `http://auth-server:5056` |
| `auth_server_admin_token` | secret | auth-server Admin API 共享管理令牌（对外仅返回 `has_auth_server_admin_token` 布尔） |
| `auth_server_audience` | string | JWT 本地验签 audience，默认 `knowledge-scaffold` |
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

返回当前工作空间的 `mcp_endpoint_id`、独立进程连通状态、transport、外部地址和 search/chat 服务卡片。`services[].active_url` 是客户端应直接使用的完整 URL。外部基础地址来自 `MCP_PUBLIC_BASE_URL`，该变量只配置协议、域名、端口和可选网关前缀。

```json
{
  "workspace_id": "corporate",
  "mcp_endpoint_id": "cdd181f5b6cb47a9",
  "running": true,
  "transport": "both",
  "services": [
    {
      "toolset": "chat",
      "active_url": "https://mcp.example.com/cdd181f5b6cb47a9/chat/mcp",
      "streamable_http_url": "https://mcp.example.com/cdd181f5b6cb47a9/chat/mcp",
      "sse_url": "https://mcp.example.com/cdd181f5b6cb47a9/chat/sse"
    }
  ]
}
```

#### `GET /agentic/mcp/runtime/concurrency`

读取当前工作空间普通用户 MCP Progress 流式 Chat 的并发上限。已认证用户可读取；响应始终返回服务端生效值及可配置范围。

```json
{
  "stream_user_workspace_concurrency": 5,
  "default": 5,
  "min": 1,
  "max": 32
}
```

#### `PATCH /agentic/mcp/runtime/concurrency` (`admin`)

仅平台管理员可更新当前工作空间的普通用户 MCP Progress 流式 Chat 并发上限；值必须是 `1` 至 `32` 的整数，默认值为 `5`。配置保存后对后续流式 Chat 请求生效；管理员 MCP 流式 Chat 仍使用平台运行上限，普通 JSON Chat 不受此项影响。

```json
{
  "stream_user_workspace_concurrency": 8
}
```

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
| `mcp:search` | `/{mcp_endpoint_id}/search/mcp`、`/{mcp_endpoint_id}/search/sse`，只执行原子图查询，不调用 LLM |
| `mcp:chat` | `/{mcp_endpoint_id}/chat/mcp`、`/{mcp_endpoint_id}/chat/sse`，进入标准 workflow 和 run admission queue |

未传 scopes 时默认同时授予两项。响应 `201` 只在创建时返回一次 `token` 明文；token 固定绑定 `sub` 和 `workspace_id`，不能用请求头切换到其他 workspace。

#### `DELETE /agentic/mcp/tokens/{token_id}`

吊销当前用户、当前 workspace 下的 token。MCP token 注册表默认位于 `.onto-runtime/mcp-tokens.json`，备份和恢复必须与 workspace 数据同步完成。

独立服务地址格式：

- Streamable HTTP：`http://<host>:7488/{mcp_endpoint_id}/search/mcp`、`http://<host>:7488/{mcp_endpoint_id}/chat/mcp`
- SSE：`http://<host>:7488/{mcp_endpoint_id}/search/sse`、`http://<host>:7488/{mcp_endpoint_id}/chat/sse`
- 健康检查：`http://<host>:7488/health`

`mcp_endpoint_id` 是稳定的公开路由标识，不是凭证。URL 中的 Endpoint ID 必须与 MCP Token 绑定的 workspace 一致；不一致时工具返回 `MCP_WORKSPACE_ENDPOINT_MISMATCH`。每个 MCP transport session 同时绑定到首次使用的 Endpoint，`Mcp-Session-Id` 或 SSE `session_id` 不能跨工作空间 URL 复用；跨 Endpoint 使用按未知会话返回 HTTP 404 `Session not found`。Streamable HTTP Session 默认空闲 30 分钟后失效，Endpoint 绑定在额外 60 秒安全宽限后清理；SSE 连接断开时立即清理。每个服务 transport 固定最多保留 10000 个活动 Session，达到上限返回 HTTP 503 `Server session capacity reached`，不会淘汰活动 Session。MCP 服务重启后原 transport session 失效，客户端需要重新 initialize。search 服务要求 ontology 已配置 `neo4j` 或 `hugegraph` 数据源。所有 MCP 调用应传 `Authorization: Bearer <mcp_token>`；consumer access token 不可替代 MCP token。旧 `/search/mcp`、`/chat/mcp`、`/search/sse`、`/chat/sse` 路由均不可用。

MCP `chat` 工具通过请求参数 `include_trace` 控制是否返回脱敏后的 `trace`，默认 `false`。传入 `stream=true` 且客户端提供 MCP Progress Token 时，服务会返回脱敏后的粗粒度 Progress；不支持 Progress 的客户端自动退化为普通 Chat。普通用户的 Progress 流式 Chat 按 `用户 + workspace` 使用上述可配置并发上限。当前公共 Trace schema 为 `5.0`：同一逻辑迭代只保留最后一份累计快照，工具调用嵌入对应 iteration，Agent 输出位于 `agent_output`，不返回模型原始思维链或 `reasoning_summaries`。

`selected_ontology_id` 为可选参数。已存在的 session 会复用其锁定 ontology；新 session 未显式指定时，工作空间只有一个已启用 ontology 则自动选中，存在多个时返回 `MCP_ONTOLOGY_SELECTION_REQUIRED`，不会猜测。求解类操作成功后，`result.authoritative_solver_result` 返回经过脱敏的权威结果摘要和工件引用；其中 `summary.candidate_id` 是稳定内部标识，`summary.combination_no` 是面向业务展示的组合号，完整候选列表由 `candidate_results_artifact_ref` 标识。

```json
{
  "query": "查询业务数据",
  "session_id": "optional-session-id",
  "selected_ontology_id": "optional-ontology-id",
  "include_trace": true,
  "stream": true
}
```

`MCP_CHAT_TRACE_MAX_EVENTS`、`MCP_CHAT_TRACE_MAX_BYTES` 和 `MCP_CHAT_TRACE_MAX_TEXT_CHARS` 仅限制开启 Trace 后的响应规模，不会自动开启 Trace。`MCP_CHAT_PROGRESS_MAX_EVENTS`、`MCP_CHAT_PROGRESS_MAX_MESSAGE_BYTES`、`MCP_CHAT_PROGRESS_QUEUE_SIZE` 和 `MCP_CHAT_PROGRESS_HEARTBEAT_SECONDS` 控制流式 Progress 的数量、大小、缓冲与心跳；拥塞时非终态进度可能被裁剪，最终结果仍会返回。

```json
{
  "trace": {
    "schema_version": "5.0",
    "level": "summary",
    "iterations": [
      {
        "iteration": 1,
        "status": "continue",
        "timing": {
          "total_elapsed_ms": 1000,
          "model_elapsed_ms": 700,
          "tools_elapsed_ms": 250,
          "setup_elapsed_ms": 50
        },
        "agent_output": {
          "summary": "我需要查询业务实体状态。",
          "source": "agent_content",
          "truncated": false
        },
        "tools": {
          "count": 1,
          "succeeded": 1,
          "failed": 0,
          "calls": [
            {
              "name": "business_data_search",
              "status": "completed",
              "elapsed_ms": 250,
              "record_count": 1,
              "executed_steps": [
                {
                  "tool": "property_filter",
                  "summary": "执行内部图工具 property_filter，返回 1 条"
                }
              ]
            }
          ]
        }
      }
    ],
    "totals": {
      "overall_elapsed_ms": 1005,
      "iterations": 1,
      "tool_calls": 1,
      "tool_calls_succeeded": 1,
      "tool_calls_failed": 0,
      "tool_execution_elapsed_ms": 250,
      "retries": 0,
      "warnings": 0
    }
  }
}
```

`agent_output.summary` 优先取该迭代 `token.delta(channel=content)` 在前端运行轨迹中展示的“Agent 输出”；最终迭代以 `turn.finished.final_output` 为准。`source` 标明摘要来源，可能为 `agent_content`、`final_output`、`error`、`cancel_reason`、`iteration_event`、`tool_query_summary` 或 `tool_name`；`truncated` 表示摘要是否受 `MCP_CHAT_TRACE_MAX_TEXT_CHARS` 限制而被截断。原始 reasoning token 和 `reasoning.summary` 事件均不会进入公共 Trace。

`omitted.text_fields_truncated` 统计 Trace 文本字段触发截断的次数；同一迭代的流式 `agent_content` 被终态 `final_output` 覆盖时，按最终保留的字段去重计数。

公共工具事件和 MCP Trace 共用结构化、文本双层脱敏，会处理 `access_token`、`client_secret`、`db_password`、`private_key`、JSON 字符串、Bearer/JWT、URI 凭据和 PEM 私钥等常见格式。脱敏在 `tool.started` / `tool.finished` 事件边界执行，不依赖 `include_trace` 是否开启。

`ontology_context` 调用会在 `tools.calls[]` 中附带输入中的 `resource` 和可选 `key`。`business_data_search` 调用会附带最多 8 条脱敏、限长的 `executed_steps`，同时保留 `record_count`、查询摘要和数据库 Provider 等原有字段；schema `5.0` 不输出 `evidence_ids` 和 `reasoning_summaries`，但仍保留 `query_digest`。

`timing.tools_elapsed_ms` 是该迭代工具阶段的墙钟时间；`totals.tool_execution_elapsed_ms` 是每次工具调用耗时之和，并行执行时后者可能更大。`totals.overall_elapsed_ms` 从 MCP workflow bridge 开始处理请求计时，到生成 Trace 快照为止，包含校验、排队、Agent 执行和结果组装。

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
| `context` | object | 否 | A2A 上下文；运行语义与 `POST /agentic/runs` 的 `agent_mode`、`llm_request_mode` 和 `arguments` 对齐 |

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
- `context` 会映射为原生异步 Runtime 上下文，字段含义与 `POST /agentic/runs` 的对应字段对齐
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

ACP 适配层复用现有 `AsyncRunManager` 和 `agent-stream.v2` 内核，对外暴露 ACP 风格的 run 接口。ACP run 会写入同一个 ExecutionStore，并通过 `X-Workspace-Id` 解析工作空间。

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

- 新前端/客户端若直接消费主协议，先 `POST /agentic/runs`，再使用响应中的 events link 从 `after_seq=0` 读取 SSE
- 如果你需要更标准的 task 视图或 background task 管理，优先使用 `/a2a/*`
- 如果你接入的是 ACP client 或需要 ACP 事件形状，使用 `/acp/*`
- 使用多工作空间时，建议所有 `/agentic/*`、`/a2a/*` 与 `/acp/*` 请求都显式带上 `X-Workspace-Id`
