# 移除 AUTH_MODE=local，remote 成为唯一认证路径

> 状态：已实施 · 2026-07-10 · 分支 refactor/remote-auth-only
> 反转：2026-07-09-decoupled-auth-architecture.md 决策 D4「local 长期保留为默认」

## Context

解耦鉴权 ADR 的 D4 把 `AUTH_MODE=local`（内置 `AuthManager` + PBKDF2 + 自签 HMAC token）保留为默认，理由是"单容器部署形态"的便利；`remote`（独立 auth-server + JWKS 验签）为可选。切换期还引入了双 token 验证链——remote 模式下若 `users.json` 仍在，用本地 `AuthManager` 作 fallback 回落验证旧 HMAC token 一个 TTL 周期（施工步骤 6 计划最终删除）。

实践中两条认证路径带来部署歧义：`AUTH_MODE`、`AUTH_SECRET`、`AUTH_USERS_FILE`、`AUTH_TOKEN_TTL` 与 `AUTH_SERVER_URL`、`AUTH_SERVER_ADMIN_TOKEN` 等两组配置并存，部署者容易配错模式或混填；compose 的 `--profile auth` opt-in 条件逻辑散落在 8 个部署脚本里；modeling 工具也已同步收敛为直连 auth-server 的单一路径（feat/modeling-login-auth）。为消除歧义，本决策把 remote 定为唯一生产路径。

## Decision

1. **移除生产 local 路径**：删除 `build_runtime_services` 的 `AUTH_MODE` 三分支（local / remote / 未知），收敛为无条件构造 `RemoteAuthBackend` + 校验 `AUTH_SERVER_URL`（缺失即 `RuntimeError` 启动失败）。删除 `auth_mode`、`auth_secret`、`auth_users_file`、`auth_token_ttl_seconds` 配置项。
2. **连同迁移 fallback 一起删除**：fallback 的唯一来源是 local 模式签发的 HMAC token；移除 local 生产路径后它在生产中是死代码，且是 `AUTH_SECRET`/`AUTH_USERS_FILE` 仍被读取的唯一原因。删除 `RemoteAuthBackend` 的 `fallback` 参数与 `verify_token` 的回落分支，收敛为"仅本地 RS256 JWKS 验签，失败即 None"。
3. **`AuthManager` 类保留为纯测试后端**：15+ 个测试文件直接实例化它并注入 `app.config["AUTH_MANAGER"]`（绕过 `AUTH_MODE` 装配），契约测试依赖它验证对外冻结契约（任何后端都须满足）。只删它在 `app.py` 的生产装配，类本体与 `AuthBackend` Protocol 保留。
4. **auth-server 从 opt-in 变常备组件**：三份 compose 删除 `profiles: [auth]`，backend 增加 `depends_on: {auth-server: {condition: service_healthy}}`；部署脚本删除 `auth_profile_enabled()`/`compose_profile_args()` 及所有 `if auth_profile_enabled` 守卫，auth 镜像加载/健康等待/`.onto-auth` 目录变为无条件。
5. **配置收敛**：`AUTH_SERVER_URL`、`AUTH_SERVER_ADMIN_TOKEN` 标注为必需；`AUTH_SERVER_AUDIENCE`（默认 meta-wise）、`AUTH_SERVER_ISSUER`（空=回落 URL）保持可选。

## Alternatives considered

- **保留 local 作为默认、仅文档劝退**：被否。两组配置并存的歧义是问题根源，文档无法消除误配。
- **删 local 但保留 remote 的迁移 fallback**：被否。保留 fallback 就保留了一条 HMAC 验证路径和 `AUTH_SECRET`/`AUTH_USERS_FILE` 配置，与"单一路径、无歧义"目标矛盾；且移除 local 后已无 HMAC token 来源，fallback 是死代码。
- **删除 `AuthManager` 类**：被否。15+ 测试文件把它当离线测试后端，契约测试依赖它；删除会连带重写整个测试基建，改动面失控且损失契约保护。
- **保留 `AUTH_MODE` env 但只留 remote 分支**：被否。留一个只有单值的开关仍是歧义源；彻底删除该配置项更干净。

## Consequences

- 部署只有一条路径：必须提供 `AUTH_SERVER_URL` 指向 auth-server，缺失则 backend 启动即失败（快速失败优于运行期半残）。auth-server 成为 compose 必备服务。
- **升级注意（一次性成本）**：现有 `AUTH_MODE=local` 部署升级到本版本前，必须先跑 `migrate.sh` 把 `users.json` 导入 auth-server 的 `.onto-auth/`（PBKDF2 哈希串自包含可直接搬移，用户无需重设密码）；且因删除了 HMAC fallback，升级时持旧 HMAC token 的在线用户会被强制重新登录一次（不再有一个 TTL 周期的平滑过渡）。部署文档已提示。
- 本地开发也需要一个 auth-server 实例（可用 `AUTH_MAIL_MODE=console` + 内存数据目录快速起）；不再能零依赖单容器起 consumer。
- `AuthManager` 类成为"仅测试使用"的代码：保留但生产不再装配，需在类注释标注避免误用为生产后端。
- 对外 HTTP 契约不变（登录 `{token,user}`、401/403 语义、`/auth/refresh`、注册代理等）——切换的是内部装配，契约测试继续全绿。
