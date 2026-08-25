# auth-server

meta-wise 解耦鉴权系统的**预置 auth server v1**。设计依据见
`consumer/docs/decisions/2026-07-09-decoupled-auth-architecture.md`。

## 定位

- 只管身份（sub）、凭据（PBKDF2 密码哈希 / 密码策略）、token 签发与生命周期（决策 D3）。
- 对 consumer 暴露标准 OAuth2/OIDC 子集（密码模式 + refresh + RFC 7009 吊销 + JWKS + discovery，决策 D1），
  后续可整体替换为企业 IdP：替换 = 换 issuer/discovery URL。
- 独立容器部署、不对外暴露端口；与 consumer 之间**禁止任何 Python import**，唯一接口是 HTTP（决策 D4）。
- users.json 格式与 consumer 现有实现完全兼容（新增 `token_version` 字段，缺省视为 1）：
  现有文件放进 `<DATA_DIR>/users.json` 即可直接登录，用户无需重设密码。
- 吊销语义：改密 / 管理员重置 / /revoke 时 `token_version+1`，全量吊销该用户 refresh token；
  已签发的 access token 在 ≤ TTL 内自然失效（决策 D2 的显式放宽）。
- 首启无用户文件时使用环境变量 `AUTH_BOOTSTRAP_ADMIN_PASSWORD` 创建 `admin`（role=admin）；未配置或不符合密码策略时拒绝启动。

## 端点清单

### 标准协议侧（无需鉴权）

| 端点 | 说明 |
| --- | --- |
| `POST /token` | form 或 json。`grant_type=password`（username/password）或 `grant_type=refresh_token`。成功返回 `{access_token, refresh_token, token_type: "Bearer", expires_in}`；凭据/refresh 无效返回 400 `invalid_grant`；其他 grant_type 返回 400 `unsupported_grant_type`。兼容字段 `pwd_cr` 固定为 `false` |
| `POST /revoke` | RFC 7009 风格，`{token}`。合法 refresh token 触发该用户 `token_version+1` 全量吊销；无论成功与否一律 200 `{}` |
| `GET /.well-known/openid-configuration` | discovery：issuer、token/revocation/userinfo 端点、jwks_uri、`grant_types_supported=["password","refresh_token"]` |
| `GET /jwks.json` | JWKS 公钥集（含 kid，支持多把 key 以备轮换） |
| `GET /userinfo` | Bearer access token，通过校验返回 `{sub, role, password_change_required}`（以存储为准），否则 401 |
| `GET /health` | `{"status": "ok"}` |

### 自助注册（默认开启，可用 `AUTH_ENABLE_REGISTRATION=false` 关闭）

注册功能默认开启；显式关闭后端点一律返回 503 `{"error": "注册功能未开启"}`。

| 端点 | 说明 |
| --- | --- |
| `POST /register` | `{username, email, password}`。校验用户名格式（3-32 位字母/数字/`_`/`.`/`-`）与唯一性、邮箱格式与全局唯一、密码策略（同 Admin API，违规透传 `PASSWORD_POLICY_FAILED` violations）→ 一步直接创建正式用户（role=user、不强制改密——密码是本人刚设置的）→ 200 `{"success": true, "user": {username, role, password_change_required}}` |

防滥用：`/register` 走进程内固定窗口限流（按 IP 和按 email 各计，每小时 5 次，
超限 429 `{"error": "请求过于频繁，请稍后再试"}`）。
防枚举权衡：用户名/邮箱被占用会返回明确中文错误（而非模糊响应），可枚举性靠限流兜底——
这是内部工具的务实取舍，公网部署前应重新评估。

### Admin API（供 consumer BFF 转发）

鉴权：请求头 `X-Admin-Token` 必须等于环境变量 `AUTH_SERVER_ADMIN_TOKEN`；
该变量未配置时一律 503 `{"error": "Admin API disabled"}`。
中文错误消息与 `PASSWORD_POLICY_FAILED` violations 结构与 consumer 现有 `/auth/users*` 响应一致，BFF 直接透传。

| 端点 | 说明 |
| --- | --- |
| `GET /admin/users` | `{"users": [{username, role, password_change_required[, email]}]}`（email 仅注册用户有，存量用户形状不变） |
| `POST /admin/users` | `{username, password[, role]}` → 201 `{"user": {...}}`；策略违规 400 `{error, code: "PASSWORD_POLICY_FAILED", violations}` |
| `PUT /admin/users/<username>` | `{password}` 管理员重置：写 `password_changed_at`，`password_version+1` 且 `token_version+1`（可选 `{role}` 改角色） |
| `DELETE /admin/users/<username>` | `{"success": true}`；用户不存在 400 中文错误 |
| `PUT /admin/change-password` | `{username, old_password, new_password}` 本人改密语义：写 `password_changed_at`、双 version+1 → `{"success": true}` |

注意："依赖当前登录用户"的守卫（不能删除自己、不能删除 admin 管理员、不能通过用户管理重置当前登录
用户密码）留在 consumer BFF——本服务不感知调用者身份。

## Token 细节

- RS256 非对称签名；私钥首启自动生成（RSA 2048），存 `<DATA_DIR>/keys/private.pem`，0600 原子写；
  kid = 公钥 SHA-256 指纹前 12 位。轮换：换新 `private.pem`，把旧公钥（PEM）留在 `keys/` 目录即可继续验旧 token。
- access claims：`iss, aud, sub, role, pwd_cr, ver, typ="access", iat, exp`（role/pwd_cr 只作提示，consumer 以 /userinfo 或自身存储为准）。
- refresh claims：`iss, aud, sub, ver, typ="refresh", jti, iat, exp`。
- TTL：access 默认 900s，**硬上限 3600s（超过钳制并打日志警告，决策 D2）**；refresh 默认 259200s（72h）。

## 配置（全部环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AUTH_SERVER_DATA_DIR` | `./data` | users.json 与 keys/ 所在目录 |
| `AUTH_SERVER_ISSUER` | `http://auth-server:5056` | JWT iss |
| `AUTH_SERVER_AUDIENCE` | `meta-wise` | JWT aud |
| `AUTH_ACCESS_TTL_SECONDS` | `900` | access TTL，硬上限 3600 |
| `AUTH_REFRESH_TTL_SECONDS` | `259200` | refresh TTL |
| `AUTH_SERVER_ADMIN_TOKEN` | 未设置 | Admin API 共享密钥；未设置则 /admin/* 全部 503 |
| `AUTH_BOOTSTRAP_ADMIN_PASSWORD` | 未设置 | 首次创建 users.json 时必填的管理员初始强密码；已有用户文件时无需配置 |
| `AUTH_SERVER_HOST` / `AUTH_SERVER_PORT` | `127.0.0.1` / `5056` | 监听地址与端口 |
| `AUTH_ENABLE_REGISTRATION` | `true` | 自助注册开关；设为 `false` 可关闭 |

限流窗口（每小时 5 次）为 `registration.py` 内常量，不做 env。

## 运行

```bash
# 开发（初次启动须在本地环境中提供独立强密码）
cd auth-server
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
AUTH_BOOTSTRAP_ADMIN_PASSWORD='<独立强密码>' .venv/bin/python run.py

# 测试
.venv/bin/python -m pytest tests -q

# 容器（数据挂 /data；不要 publish 端口到宿主机，走内部网络）
docker build -t meta-wise-auth-server .
docker run -v auth-data:/data \
  -e AUTH_SERVER_ADMIN_TOKEN='<独立管理令牌>' \
  -e AUTH_BOOTSTRAP_ADMIN_PASSWORD='<独立强密码>' \
  meta-wise-auth-server
```

## 明确不做（v1）

- 不做 authorization code flow / PKCE（无浏览器重定向场景）
- 不做 SSO / 企业 IdP 联邦（替换路径是整体换 issuer，不是在本服务内桥接）
- 不做 MFA
- 不做多租户
- 不做逐请求 introspection（决策 D2 已否决）
- 不做授权（authZ）：角色裁决、workspace 隔离、前端权限全部留在 consumer（决策 D3）

自助注册（`/register`）默认开启，防滥用措施见"自助注册"一节；不允许自行建号的部署应显式设置
`AUTH_ENABLE_REGISTRATION=false`。现有防护包括：
限流 + 防枚举权衡（占用类错误返回明确中文消息、靠限流兜底）。
