# 0107 Auth 独立部署：启动等 Auth 就绪，每个请求核对账号，首登先改密

## Context

develop !638 把 auth-server 从建模离线包里拆了出去。拆分之后有三点变化：

- Auth 和 Consumer、Hub、评测服务一样单独交付。建模包可以先装，Auth 可能晚到，也可能单独重启。
- 账号由 Auth 负责人管理。首次登录的账号、被管理员重置密码的账号要先改密码，才能使用业务功能（`password_change_required`）。
- 管理员改角色、删用户、用户改密码（令牌版本递增），都要在建模这边及时生效，不能等令牌过期。

PR555 的鉴权在 `/api/v1` 的中间件里，只按 JWKS 验签。角色取令牌里的 claim，没有改密门禁，启动时也不等 Auth。
develop 的做法写在旧路由和旧前端里，需要按 v1 结构重新落位。

- **目标**：Auth 没就绪时不接请求；账号状态以 Auth 当前记录为准；待改密账号只能改密码。
- **约束**：
  - 不改 auth-server 的接口；
  - 本地模式（没配 `ONTO_WEB_AUTH_URL`）行为不变；
  - 错误体沿用 v1 的 `errors.response` 格式。

## Decision

1. **启动等 Auth**：`governance.startup.wait_for_dependencies()` 在恢复运行之前等 auth-server 就绪，就绪判断看 `/health` 和 `/status`。
   配了 `ONTO_WEB_AUTH_URL` 才等，`ONTO_AUTH_STARTUP_WAIT_ENABLED=false` 时不等（配置注册表登记，默认开）。
2. **每个请求核对账号**：`api/v1.auth_middleware` 验签通过后，用同一个令牌调用 Auth 的 `/userinfo`，不缓存。
   - 角色以 `/userinfo` 为准；
   - `/userinfo` 拒绝、`sub` 不一致或角色不合法时返回 `401 unauthenticated`；
   - 查不到账号状态时返回 `503 service_unavailable`（`dependency: auth_server`），不放行。
3. **首登改密门禁**：`password_change_required` 为真时，只放行 `A-06` 改密、`A-07` 我的资料和免鉴权接口（`/api/v1/auth/*` 等），
   其余接口返回 `403 password_change_required`。
   - 旧路径先改写成 v1 路径再判断。
   - 前端 API 客户端统一处理这个错误，转到改密页（`pages/login/PasswordChangePage.jsx`）。改密页和「我的 › 账号」共用 `PasswordForm`。
4. **改密后重新登录**：改密走 Auth 的管理接口（需要 `ONTO_WEB_AUTH_ADMIN_TOKEN`）。Auth 递增令牌版本，这个账号的全部令牌作废，
   前端提示后回到登录页。
5. **离线包不带 Auth 镜像**：部署脚本默认按独立 Auth 的地址、共享网络、issuer、audience 配置，管理令牌不再自动生成。
   源码 compose 仍带一个 auth-server，供本地联调使用。

## Alternatives considered

- **只验签，角色和改密状态读令牌 claim**：不选。令牌有效期内，删用户、改角色、重置密码都不生效；
  待改密账号拿到令牌后可以直接调用业务接口。
- **`/userinfo` 结果按令牌缓存几秒**：暂不选。develop 每个请求实时查。缓存会让「改完密码、删完用户之后仍能访问」重新出现一个窗口，
  而且缓存失效要靠 Auth 推送，现在没有这个通道。如果压测显示 Auth 成了瓶颈，再定缓存时长。
- **门禁放行整个旧 `/api/auth/*`（develop 原样）**：不选。v1 的改密在 `/api/v1/me/password`，资料在 `/api/v1/me`，
  旧前端已删除，按 v1 接口编号列出放行范围更清楚。
- **Auth 不可用时退回只验签放行**：不选。Auth 不可用时账号状态未知，放行会让待改密、已删除的账号暂时可用。

## Consequences

- 删用户、改角色、改密码都在下一个请求生效，待改密账号进不了业务页面。
- **最强的反对理由**：每个登录请求多一次到 Auth 的同步调用，Auth 抖动时整个建模服务返回 503。
  - 缓解：调用在线程池里执行，超时 5 秒；启动时先等 Auth 就绪。
  - 前端启动时查不到账号显示「暂时无法核对账号状态」并提供重试，不清登录态。
- **代价**：
  - 测试里没有 Auth。`tests/conftest.py` 默认把 `auth.current_user` 换成按令牌放行，`test_first_login_gate` 走真实核对路径。
  - 单独部署时，运维要把 `ONTO_WEB_AUTH_ADMIN_TOKEN` 填成和 Auth 相同的值，否则用户自助改密（包括首登改密）返回 `user_admin_unavailable`。
- **迁移风险**：
  - 升级时如果旧的自带 Auth 容器还在，部署脚本会停下来，要求先核对账号归属，旧账号卷保留。
  - Auth 晚于建模上线时，建模进程一直等待，页面暂时打不开。要用 `deploy.sh --verify-dependencies` 确认依赖就绪。
