# 0021 接口统一到 /api/v1，旧路径经兼容中间件改写，错误体统一

## Context

插件化详细设计 4.1 要求对外接口加版本号；后端改造方案 A14、A15 要求把 6 个写死 `/api` 的路由改到
`/api/v1`、拆分约 50 个路由的 `projects.py`。前端正按 API 契约（`docs/api-v1.md`）重写，契约第 3 章
定了请求与响应形状，第 1.5 节与第 2 章定了错误体与错误码，第 6 章逐条给出旧接口的去向。现状：

- 错误体有 `{error}`、`{error, code}`、`{code, message, details, error}` 三种，错误码各模块自取；
- 鉴权中间件按 `/api/auth/`、`/api/health`、`/api/onto-mcp/` 免鉴权，`?token=` 对所有接口生效；
- 旧前端、外部脚本、已经配好的 MCP 客户端都在用旧路径。

## Decision

1. **v1 汇总**：`backend/app/api/v1.py` 挂全部路由与中间件。中间件从外到内：链路 ID（`X-Trace-Id`，
   错误体的 `trace_id`）→ 旧路径兼容 → 鉴权 → 路由。路由按领域拆成 projects、files、resources、studio、
   knowledge、versions、project_settings、sessions、solver、publish、uploads、contracts、onto_mcp、
   auth_routes、me、settings、admin、service_tokens、ops；路由共用的依赖（工作区管理器单例、当前用户、
   所有权、分页、时间格式）在 `api/deps.py`，跨接口复用的对象投影在 `api/views.py`。
2. **兼容中间件**（`api/compat.py`，纯 ASGI）放在鉴权之前：按第 6 章的对照表把旧路径改写成 v1 路径，
   必要时连方法一起改（`POST .../data-source` → `PUT`，`POST /api/auth/password` → `PUT /api/v1/me/password`）；
   少数旧接口要看请求参数或请求体才能定位（Studio 按 `path` 换算成 `entry_id`，会话消息按 `conversation_id`，
   回收站恢复按 `name`，改用户按是否带 `password` 分到改角色或重置密码）。旧接口没带对话 ID 时改写成
   `-`，v1 会话接口把它解析成引擎当前绑定的对话。只改路径，不做请求与响应的形状兼容；响应加
   `Deprecation`、`Sunset: Sat, 01 May 2027`、`Link`，JSON 错误体补 `error`（值同 `message`）。
   已删除的接口直接回 `410 endpoint_removed`，`details.successor` 给替代接口。
3. **永久保留**：`/api/health`（响应不变）、`/api/onto-mcp/...`（与 `/api/v1/onto-mcp/...` 同一处理器）、
   `/drawio/*`；它们不经改写、不加弃用头。资产包上传 `POST /api/settings/assets/upload` 的去向
   PL-13 属于包仓库（B3），过渡期按原路径照旧工作。
4. **错误体**：`api/errors.py` 的 `ApiError(code, message, status, details)` 是新代码的唯一出口；
   各模块原有的异常（WorkspaceError、SessionError、AuthError……）由同一处翻成契约码，没带具体码的按
   状态码归类，旧码按第 2 章改名（`project_metadata_unavailable` → `metadata_unavailable` 等）。
   参数校验失败统一 `400 invalid_argument`，`details.fields` 列出字段；未预期错误回 `500 internal_error`，
   原始信息只进错误留档，响应给 `error_id`。
5. **鉴权**：免鉴权前缀按契约 1.2 第 6 条更新；`?token=` 只对 SSE 与文件下载生效；令牌过期回
   `401 token_expired`，其余回 `401 unauthenticated`；服务令牌只能调三份契约接口。
6. **一致性测试**：`tests/test_api_compat.py` 对兼容表逐条造样例路径，断言改写目标命中已注册的 v1 路由；
   另测一组代表性旧路径的弃用头、错误体 `error` 字段与 410。

## Alternatives considered

- **旧路径保留旧处理器、v1 另写一套**：两套处理器要同步维护半年，行为必然漂移；契约 8.1 #4、#5 已定
  只改写路径。
- **兼容中间件做形状兼容**：旧前端与新后端同版本替换（前端整体重写），形状兼容只服务外部脚本，
  而外部脚本主要用三份契约与 MCP，这两类不变或永久保留。
- **用 FastAPI 的 BaseHTTPMiddleware 写兼容层**：要缓冲整个响应，会卡住 SSE 与大文件下载；纯 ASGI
  中间件只在错误体（JSON、非 2xx）上缓冲。

## Consequences

- 旧路径调用拿到的是 v1 形状，旧前端在新前端替换前部分页面会显示不全；对外脚本有半年改址期。
- 运维管理面 `/api/v1/ops` 本阶段挂在主监听上，独立监听与 `ONTO_WEB_OPS_ON_MAIN` 开关随 E8（P1）。
- 契约里属于以后阶段的接口（插件、流程、变更集、证据源、自主构建、通知等）还没有路由，访问返回
  `404 not_found`；现有能力换了形状的都已迁到 v1，偏离契约之处记在 `_plan/contract-changes.md`。
- 审计仍在鉴权中间件里、处理请求之前记录（不带状态码与链路 ID），改为响应后记录属于 E4（P1）。
