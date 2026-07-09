# 解耦用户管理鉴权系统：架构决策

> 状态：提议（待评审）· 2026-07-09 · 分支 `feat/decoupled-auth`
> 结论依据：对现有认证实现的代码调研（`agentic/services/auth.py` 459 行、13 个后端文件 80+ 处鉴权装饰、前端 21 处 authFetch + 8 处 useAuthContext、SDK 双语言、agent_eval 登录适配器）。

## Context

现有认证是 consumer 内嵌的 `AuthManager`：users.json 存储 + PBKDF2 哈希 + 自制两段式 HMAC token（12h TTL），
"改密/删用户后旧 token 立即失效"靠每次请求回查 users.json 的 `password_version`。目标是解耦为
「consumer 内通用 auth client + 独立 auth server」，先做预置实现，后续可替换为企业 IdP。

## Decision

### D1 协议：client↔server 采用标准 OAuth2/OIDC 子集，不自定义 REST

- v1 预置 server 只实现明确子集：密码模式 token 端点 + refresh token + 吊销（RFC 7009）+ JWKS + discovery；
  基于 Authlib 等成熟库，不手写 OIDC。
- 理由：可替换性是硬需求。标准协议下"替换"=换 issuer/discovery URL；自定义协议下"替换"=每家 IdP 一套适配器 + 安全评审。

### D2 Token：JWT + JWKS 本地验签；access TTL 从 12h 缩短到 10~15 分钟 + refresh token

- **吊销语义显式放宽**：从"立即失效"（逐请求回查）放宽为"≤ access TTL 内失效"；改密/管理员重置/删用户时
  吊销该用户全部 refresh token（等价现有 `password_version+1`）。
- 不采用逐请求 introspection：auth server 会成为全站 API 的同步单点，且企业 IdP 对 introspection 支持参差。
- ⚠️ TTL 配置必须在实现中锁死短值；沿用 12h 即安全回归。

### D3 认证/授权切分：authN 外移，authZ 留 consumer

- auth server 只管：身份（sub）、凭据（密码哈希/策略/强制改密数据）、token 签发与生命周期。
- admin/user 角色、workspace 隔离、前端 `can()` 权限全部留在 consumer；consumer 侧新增按 sub 键控的
  角色存储（v1 小 JSON 文件）；token 中 role claim 只作提示不作准（延续现有"不信 token 里的 role"语义）。
- 用户 CRUD 与密码策略移入 auth server；consumer 的 `/auth/users*`、`/auth/change-password` 保留为薄代理，
  透传中文错误消息与 `PASSWORD_POLICY_FAILED` violations 代码。

### D4 嵌入形态：BFF + 接缝置换，对外契约全部冻结

- HTTP 门面冻结：`POST /auth/login {username,password}→{token, user:{username,role,password_change_required}}`、
  `/auth/me`、`/auth/users*` CRUD、401/403 语义、`PASSWORD_CHANGE_REQUIRED`/`PASSWORD_POLICY_FAILED` 错误码、
  Bearer 头、`X-Workspace-Id`。注意：登录响应字段名是 `token` 而非 `access_token`——agent_eval 默认配置与
  外部脚本依赖它，不得"顺手标准化"。
- Python 接缝：`require_auth(roles=)`/`require_role`/`get_current_user`/`get_auth_manager`/
  `app.config["AUTH_MANAGER"]` 同名同签名；内部抽 `AuthBackend` 协议——现有 AuthManager 即
  `LocalAuthBackend`，新增 `RemoteAuthBackend`；`AUTH_MODE=local|remote` 切换，**local 长期保留为默认**
  （存在 backend 单容器部署形态）。
- 预置 server 落位：新顶层目录 `auth-server/`（与 consumer 平级），独立容器、不对外暴露端口；
  **禁止与 consumer 之间任何 Python import**，唯一接口是 HTTP。

### D5 开放注册 + 邮箱验证（2026-07-09 用户拍板，从"不做清单"移入范围）

- auth server 增加自助注册：`/register`（用户名+邮箱+密码）→ 6 位邮箱验证码（15 分钟有效、
  60 秒重发冷却、5 次尝试上限）→ 验证通过创建账号，默认角色 `user`、不强制首登改密。
- 用户模型新增 `email` 字段（注册用户必填唯一，存量用户可空）——同时作为将来切换企业 IdP
  时的账号关联键（解决"sub 映射"遗留问题）。
- 注册功能默认关闭（`AUTH_ENABLE_REGISTRATION=false`，opt-in）；邮件经 SMTP（env 配置），
  开发/测试用 console 模式。防滥用：按 IP 与邮箱限流、验证码只存哈希。
- 前端注册入口与 BFF 代理端点随施工步骤 4（consumer 接通 auth server）一并交付，
  local 模式无注册功能。

## Alternatives considered

- 自定义 client-server REST 协议：实现快，但替换成本转嫁到每次 IdP 对接，否决。
- 逐请求 introspection：保住即时吊销，但引入全站同步单点，否决。
- auth server 做成 consumer 同进程 Flask 蓝图：共享 secret/config 会诱使直调捷径，"可替换"名存实亡，否决。
- 直接采用 Keycloak/Ory 作预置 server：未否决——若团队接受较重镜像应认真评估；自研的价值只剩轻量与
  users.json 无缝迁移。实现 v1 前需拍板。

## Consequences

- 迁移：users.json 的 PBKDF2 哈希串自包含，原样导入 auth server，用户无需重设密码；切 remote 后 consumer
  停止直写该文件。上线需双 token 验证链（新 JWT 优先、旧 HMAC 回落一个 TTL 周期），否则全体在线用户被踢出。
- 编排：三份 compose（根/backend/deploy.release）+ deploy/ 下 7 个脚本（backup/upgrade/migrate/rollback/preflight 等）
  都要覆盖 auth-server，backup 不纳入 auth 用户数据则回滚丢用户。
- 账号关联：consumer 角色存储按 sub 键控，预置 server 与企业 IdP 的 sub 几乎必然不一致，需在切换前定
  关联策略（如按 username/email 映射），否则换 IdP 时角色分配全部丢失。
- 强制改密跨边界：`password_change_required` 数据在 auth server（claim 下发），403 拦截与白名单
  （`/auth/me`、`/auth/change-password`）留在 consumer 的 require_auth wrapper。

## D6 Step 4 远程模式前端会话形态：方案 a（2026-07-09 用户拍板）

D2 的短 TTL access token（10~15 分钟）与"前端零改动"存在冲突：前端 useAuth 目前
拿到 `token` 后长期使用（本地 12h TTL），没有任何续期逻辑。remote 模式三选一：

- **方案 a（已采纳）**：`token` = auth server 的 access JWT；BFF 新增 `POST /auth/refresh`；
  前端 authFetch 加一层"401 时静默调 refresh 重试一次"（前端小改，约几十行，一次性）。
  语义最正，token 生命周期完全由 auth server 掌控。
- 方案 b：BFF 登录时拿 access+refresh，自己签发 12h 的本地会话 token 给前端（沿用现有
  HMAC 机制），auth server 只做凭据校验与用户管理。前端零改动，但 12h 吊销窗口的
  安全回归重新出现，且偏离 D2。
- 方案 c：`token` 直接用 access JWT 且不做 refresh——前端每 15 分钟被登出，不可接受。

## 施工顺序（每步独立合入、全量测试通过）

1. 纯重构接缝：定义 `AuthBackend` 协议、AuthManager 声明为本地实现、app.py 内联的 7 个 `/auth/*` 路由抽 blueprint（零行为变化）。
2. 契约固化：把对外契约写成契约测试（登录响应形状、错误码、token 失效语义、装饰器 HTTP 语义）；verify 改可插拔 verifier 链 + `AUTH_MODE` 配置（仅 local）。
3. 新建 `auth-server/`：搬移核心逻辑，暴露 token/JWKS/discovery/用户 CRUD 端点，自带测试（consumer 不接入即可合入）。
4. `RemoteAuthBackend`：HTTP client + JWT 验签 + 吊销补偿 + 双 token 验证链，`AUTH_MODE=remote` 集成测试。
5. 编排与部署：三份 compose + deploy 脚本 + users.json 首启导入 + backup 覆盖（remote 仍 opt-in）。
6. 跑一个发布周期后按需切默认、删除旧 HMAC fallback、更新 `.env.example` 与文档。
