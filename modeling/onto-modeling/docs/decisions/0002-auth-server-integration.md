# 0002 — 登录认证接入仓内 auth-server

## Context

onto-web 部署后公网可访问且无鉴权，任何人可消耗模型网关配额。仓库已有独立的
auth-server（OAuth2 密码模式子集：/token + refresh + RFC 7009 吊销 + JWKS +
discovery，RS256，独立容器/进程部署，可整体替换企业 IdP）。

## Decision

onto-web 按标准资源服务器对接 auth-server：

- 后端 `services/auth.py`：JWKS 验签（PyJWT）+ iss/aud/typ 校验；HTTP 中间件
  保护全部 /api 路由（白名单：/api/health、/api/auth/*、静态资源）；token 接受
  Authorization 头或 `?token=` 查询参数（EventSource 与下载链接无法带头）
- 登录/刷新经后端代理（/api/auth/login、/api/auth/refresh），前端不直连 auth-server
- 前端 localStorage 存 token，401 时静默刷新一次，失败回登录页
- `ONTO_WEB_AUTH_URL` 未配置时鉴权整体关闭（本地开发默认）
- auth-server 仅绑定 127.0.0.1，注册功能关闭，管理员由首启引导创建

## Alternatives considered

- 单管理员密码写死：无法长成多用户，弃
- 前端直连 auth-server：跨域与令牌暴露面更大，代理更可控
- 企业 IdP 直连：保留为后续替换路径（换 issuer/discovery 即可），当前无需求

## Consequences

- 公网裸奔问题解决；多用户/角色能力有了地基（claims 已带 role）
- 新增运行时依赖 pyjwt[crypto]；部署多一个 systemd 服务
- 用户管理 UI 尚缺（注册已关闭），需要后续对接 auth-server Admin API
