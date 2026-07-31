# 自助注册默认开启

> 状态：已实施 · 2026-07-31
> 修订：`2026-07-09-decoupled-auth-architecture.md` D5 与 `2026-07-10-open-registration-no-email-verification.md` 的默认开关策略

## Context

auth-server 已提供一步自助注册、用户名与邮箱唯一性校验、密码策略、用户数量上限，以及按 IP
和邮箱执行的固定窗口限流。Consumer 会通过能力接口决定是否展示注册入口，Modeling 会将注册
请求转发给同一个 auth-server。

此前代码、示例配置和三套 Compose 均以关闭注册为默认值。新部署若未额外设置环境变量，登录页
无法直接完成首次普通用户建号，管理员还需要理解并配置一个额外的 opt-in 开关。

## Decision

1. `AuthServerConfig` 在未设置环境变量时默认启用注册。
2. 根目录、backend 开发编排和 release 编排的 `AUTH_ENABLE_REGISTRATION` 兜底统一为 `true`。
3. 保留 `AUTH_ENABLE_REGISTRATION=false` 作为显式关闭方式；关闭后能力接口返回 false，注册端点返回 503。
4. 不改变注册协议：仍为用户名、邮箱、密码一步建号，不增加自动登录或邮箱验证码。

## Alternatives considered

- 保持默认关闭：会继续要求每个新部署额外发现并配置开关，不符合开箱可注册的目标。
- 删除开关并永久开放：受控环境无法恢复仅管理员建号，否决。
- 只修改 auth-server 代码默认值：三套 Compose 会继续显式注入 false，容器行为与裸机行为不一致，否决。

## Consequences

- 新部署无需额外配置即可注册普通用户，Consumer 与 Modeling 共用同一账号。
- 受控环境可显式设置 `AUTH_ENABLE_REGISTRATION=false`，恢复仅管理员建号的策略。
- 默认开放会扩大账号创建入口。现有每 IP、每邮箱每小时 5 次限流、密码策略、唯一性校验和
  `AUTH_USER_MAX_COUNT` 继续生效；面向公网部署时仍应结合网关限流、人机验证或企业 IdP 重新评估。
