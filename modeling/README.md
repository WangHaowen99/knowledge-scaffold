# 知识包建模工具

`modeling/` 下的独立 Web 服务——基于 [pi](https://github.com/earendil-works/pi) agent 的动态 workflow 交互式建模，把领域资料结构化为知识包并经 consumer 实测、优化器迭代。

- 设计文档：[docs/design.md](docs/design.md)
- pi 集成验证：[docs/spike-notes.md](docs/spike-notes.md)

## 状态

**M1（服务骨架 + 认证 + 项目/上传/解析 + workflow 引擎与恢复）已完成**，验收「传入样例资料走到节点 3」通过。当前为后端；前端与后续里程碑（M2 访谈/生成、M3 评测闭环、M4 优化循环）见设计文档。

已实现节点：① 项目创建 · ② 资料上传与解析（Excel→数据字典、md/txt/word/pdf→正文）· ③ 初步分析（pi agent run 产出领域概览/候选对象/开放问题，快照可序列化恢复）。

## 技术栈

Fastify 5 + `node:sqlite`（零原生编译）+ pi-agent-core/pi-ai + xlsx/mammoth/pdf-parse。**需 Node ≥ 22**（`node:sqlite`）。

## 本地开发

```bash
cp .env.example .env      # 填 MODELING_LLM_* 与 AUTH_ISSUER_URL
npm install
# Node 24+ 直接启动
npx tsx --env-file=.env src/server.ts

# Node 22 的 node:sqlite 需实验开关，改用：
NODE_OPTIONS=--experimental-sqlite npx tsx --env-file=.env src/server.ts
```

服务默认 `http://localhost:5100`。

## 认证与注册

直连独立 auth-server（详见 `.env.example` 与 docs/design.md 决策 3）：`AUTH_ISSUER_URL` 指向 auth-server，登录/续期走 OAuth2 `/token`（password/refresh 两种 grant），校验用 JWKS 本地 RS256 验签（不逐请求回查），注册转发 auth-server 的 `/register`（一步建号，无邮箱验证码）。

```bash
# auth-server 需开启注册：AUTH_ENABLE_REGISTRATION=true。
# AUTH_JWT_ISSUER 必须等于 auth-server 侧 AUTH_SERVER_ISSUER 的取值（默认 http://auth-server:5056），
# 不一致时登录报 503「认证服务暂不可用」并在日志提示验签配置错误
AUTH_ISSUER_URL=http://localhost:5056 \
AUTH_JWT_ISSUER=http://auth-server:5056 \
NODE_OPTIONS=--experimental-sqlite npx tsx --env-file=.env src/server.ts
```

前端登录页带「注册新账号」入口（用户名+邮箱+密码，一步提交即建正式账号，成功后回登录页并预填用户名）；
access token 过期后前端对 401 自动静默续期（单飞 `/auth/refresh`，上游明确拒绝才清凭据，刷新页面后回登录页；上游暂不可用则保留凭据待重试）。

评测闭环访问的 consumer 业务 API（`CONSUMER_API_URL`）是另一个服务；consumer 须信任同一 auth-server（remote 模式），本工具透传的专家 token 才能被其接受。

已知限制：

- 强制改密用户（如 auth-server 首启的 `admin/<REDACTED_INITIAL_PASSWORD>`、管理员重置后的临时密码）会被本服务拒绝登录（403 `PASSWORD_CHANGE_REQUIRED`）——modeling 无改密入口，请先在管理端完成改密。
- auth-server 在 compose 里默认不发布宿主端口，modeling 需与它同网络部署，本机联调时再暴露 5056。

## 验证

```bash
# M1 冒烟：用 faux LLM（无需真实 key）跑通节点 1→3
DATA_DIR=./data/smoke MODELING_LLM_BASE_URL=x MODELING_LLM_MODEL=x npx tsx scripts/smoke-nodes.ts

npm run typecheck
npm run build
```

## 主要接口（M1）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/auth/login` | auth-server `/token` 登录（加发 `refresh_token`/`expires_in`）|
| POST | `/auth/refresh` | 刷新 token（`{refresh_token}` → 新 token 对；401=上游拒绝，503=上游不可达）|
| POST | `/auth/register` | 注册（一步建号），转发 auth-server（未开启时 503）|
| GET | `/api/me` | 当前用户 |
| GET/POST | `/api/projects` | 项目列表 / 新建（节点 1）|
| GET | `/api/projects/:id` | 项目详情（含文档、workflow 节点）|
| POST | `/api/projects/:id/documents` | 上传并解析资料（节点 2，multipart）|
| POST | `/api/projects/:id/analyze` | 触发初步分析（节点 3）|

除 `/health` 与 `/auth/*` 公开端点外均需 `Authorization: Bearer <token>`（经 auth-server JWKS 本地 RS256 验签）。
