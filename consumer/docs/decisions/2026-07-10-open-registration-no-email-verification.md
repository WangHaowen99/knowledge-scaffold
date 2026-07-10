# 开放注册去除邮箱验证码，一步直接建号

> 状态：已实施 · 2026-07-10 · 分支 feat/register-no-verify
> 修订：2026-07-09-decoupled-auth-architecture.md 决策 D5（开放注册 + 邮箱验证码）

## Context

D5 引入的开放注册是两步：`POST /register`（用户名+邮箱+密码）→ 生成 6 位验证码发邮件 → `POST /register/verify` 提交验证码后才创建正式用户，另有 `/register/resend` 重发。验证码链路依赖 auth-server 的 mailer（SMTP 或 console 模式），部署时需配置 `AUTH_SMTP_*` / `AUTH_MAIL_*`，且 `AUTH_MAIL_MODE=smtp` 但 SMTP 未配好时注册端点整体 503。

实践中这套邮箱验证对当前使用场景（内部工具 / 受控环境）成本大于收益：多一步 UX、多一套 SMTP 配置负担、mailer 是验证码的唯一消费者。决定去掉验证码步骤，注册一步直接建号。

## Decision

1. **注册一步建号**：`POST /register {username, email, password}` 校验通过后**直接创建正式用户**（role=user，不强制改密），返回 `{success: true, user: {username, role, password_change_required}}`（即原 `/register/verify` 的响应形状）。保留邮箱字段（仍必填、格式校验、全局唯一——email 仍是未来切企业 IdP 的账号关联键，见 D5），只是不再验证邮箱归属。
2. **删除验证码链路**：移除 `POST /register/verify`、`POST /register/resend` 端点，以及 pending 存储（`pending_registrations.json`）、验证码生成/哈希/校验、重发冷却等全部相关代码。
3. **保留防滥用**：进程内固定窗口限流（按 IP 与按 email 各每小时 5 次）保留——开放注册无验证码后更需要限流兜底。用户名/邮箱格式校验、唯一性、密码策略保留。
4. **移除邮件发送能力**：去验证码后 mailer 无任何消费者，删除 `mailer.py` 模块、`create_mailer` 装配，以及配置项 `AUTH_MAIL_MODE` / `AUTH_SMTP_HOST` / `AUTH_SMTP_PORT` / `AUTH_SMTP_USERNAME` / `AUTH_SMTP_PASSWORD` / `AUTH_SMTP_USE_TLS` / `AUTH_MAIL_FROM` 及三份 compose 对它们的透传。`AUTH_ENABLE_REGISTRATION` 保留（注册总开关，默认 false）。
5. **前端两侧单步**：consumer 与 modeling 的注册界面从"填资料 → 验证码"两步简化为一步；注册成功回登录视图（不自动登录，保持原样）。两侧 BFF/代理删除 verify/resend 转发。

## Alternatives considered

- **保留验证码，仅默认关闭注册**：被否。用户诉求是保留注册但去掉验证码这一步，不是关闭注册。
- **去验证码但保留 mailer 模块以备未来（密码重置等）**：被否。当前 mailer 无消费者，保留即死代码/未接入插件（违反 CLAUDE.md §3）。未来若需邮件（密码重置、通知）再按需重新引入，届时可参考本次删除的实现。
- **注册连邮箱也去掉（仅用户名+密码）**：被否。email 是 D5 预留的 IdP 关联键，保留字段成本极低，去掉反而丢失关联能力且改动更大。

## Consequences

- 注册不再依赖 SMTP：部署无需再配 `AUTH_SMTP_*` / `AUTH_MAIL_*`，也不会因 SMTP 未配而 503。此前关于 163/Brevo SMTP 的配置负担消除。
- 邮箱不再被验证归属：用户可用任意格式合法但非本人的邮箱注册（唯一性仍保证不重复）。在受控/内部场景可接受；若未来面向公开互联网，需重新评估是否恢复邮箱验证或加人机验证。
- 防滥用仅剩限流：无验证码后，注册端点的滥用门槛降低，靠每 IP / 每 email 每小时 5 次的固定窗口限流兜底。多副本部署时限流为进程内、每 worker 独立（既有形态）。
- 对外契约变更：`POST /register` 响应从 `{message}` 变为 `{success, user}`；`/register/verify`、`/register/resend` 端点移除——所有调用方（consumer BFF、modeling 代理、两个前端）已同步更新。
- 已删除 pending_registrations.json 机制：升级后遗留的该文件（若有）不再被读写，可安全清理。
