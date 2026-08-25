# 知识包建模工具

`modeling/` 下的独立 Web 服务。系统以 Pi 为唯一建模运行路径，把领域资料整理为知识包，并通过检查、评测和优化持续完善。

- 设计文档：[docs/design.md](docs/design.md)
- pi 集成验证：[docs/spike-notes.md](docs/spike-notes.md)
- 统一任务与版本存储：[docs/decisions/2026-07-21-unified-authoring-and-version-store.md](docs/decisions/2026-07-21-unified-authoring-and-version-store.md)

## 状态

工作台展示唯一的 9 步流程：项目创建、建模资料上传、AI 辅助构建、场景推演、数据源绑定、评测、失败诊断、优化更新和发布交付。建模目标确认、资料分析、专家访谈、流程图确认、知识提取和确定性脚本生成都由 AI 辅助构建任务图统一编排；知识包的编辑、检查、版本与迭代在同一个知识包工作区完成。

新项目先用自由文本说明建模目标，目标确认后才围绕该目标分析资料、提出候选流程并进入专家访谈。流程图由 AI 对目标、资料和访谈结论加工后草拟，Mermaid 生成过程在中间栏与建模助手同步流式展示；生成完成后以结构化画布为事实源，可直接增删改节点和边、编辑边文字与完整业务条件、拖动布局并显式保存。用户可以继续追加资料候选、AI 推荐或空白流程，最后从建模助手提交全部已保存流程图。

流程图只对重复 ID、悬空连线和非法类型等结构损坏做阻断；缺少开始/结束、未连接节点、循环或判断分支缺少文字属于非阻断提示，用户检查并明确接受后仍可提交。流程图确认正确后才允许生成知识包。脚本需求在 AI 辅助构建中经过候选选择、契约确认、生成和保存前校验，不再提供另一套独立代码生成入口。正式交付时，流程图确认、评测和内容检查为发布门禁。

Pi 主路径已吸收以下通用能力：

- 任务进度、八类待确认事项和断线续接事件。
- 修改前确认、内容搜索和关联影响分析。
- 文件级修改、差异查看、历史恢复和并发覆盖保护。
- 表格来源、文档证据和混合来源建模。
- 流程图绘制与人工确认、场景确认、受控脚本方案和正式发布门禁。
- 多流程结构化草稿、不可变草稿修订、节点/边直接编辑和 Mermaid 派生资源。

同一项目同时只允许一个写操作。服务用 SQLite 租约协调多个 Web 进程；操作完成、失败或超时后均可继续，不依赖单个进程的内存状态。

知识包修缮确认后，多个脚本共享同一汇总 operation 时按“每个脚本单独生成、operation 最后生成”的顺序执行；全部批次成功后才原子合并并保存新修订，任一批次失败都不会写入部分结果。单批模型调用只使用绝对总时限，可通过 `MODELING_PACKAGE_APPLY_TIMEOUT_MS` 配置毫秒值；默认 `600000`（10 分钟），有效范围 `30000`～`1800000`（30 秒～30 分钟）。达到超时后立即失败，不限制模型输出预算、不使用空闲超时，也不会自动重试。

## 领域分析、自动补交与过程记录

领域分析以已确认建模目标为范围，先识别支撑目标的候选流程、领域对象和知识缺口，再逐份判断上传材料中的知识片段适合生成哪类资源：

- 数据实体、字段和映射归为对象。
- 适用条件、判断门槛和例外归为规则。
- 查询、推荐和办理步骤归为操作。
- 公式和统计口径归为指标。

一份文档可以同时产出多种知识资源，不会把整份文档强制归到单一类型。材料分类每批最多处理 4 个来源，每批都会校验材料和知识候选是否完整；如果模型漏交，系统会自动要求重新提交当前批次，最多补交 2 次。两次后仍不完整时，分析节点会记录为失败，保留明确原因并允许重试，不会把残缺结果当作成功。

分析中的工具调用、校验提醒、自动补交和完成/失败状态会持久化保存。前端在运行中自动刷新，刷新页面或重新进入项目后仍能查看。页面展示可审计事件、结构化参数和模型公开摘要，不保存或展示模型私有思维链、密钥、密码或完整文档正文。

知识包生成页面也使用同一套持久化过程记录，按顺序展示业务对象、规则/指标、业务操作和包完整性校验。如果重新生成失败，页面会保留当次失败原因和过程，同时继续展示上一个成功版本。

## 技术栈

Fastify 5 + `node:sqlite` + pi-agent-core/pi-ai + isomorphic-git + React 19。文档解析使用 xlsx、mammoth 和 pdf-parse。**需 Node >= 22**（`node:sqlite`）。

版本功能不调用 Git CLI，本机和 Web 容器都不需要安装 `git`。每个项目的版本文件保存在 `DATA_DIR/artifacts/`，SQLite 索引保存在 `DATA_DIR/modeling.db`。备份和恢复时必须同时处理这两部分。

## 本地开发

```bash
cp .env.example .env      # 填 MODELING_LLM_* 与 AUTH_ISSUER_URL
npm install
# Node 24+ 直接启动
npx tsx --env-file=.env src/server.ts

# Node 22 的 node:sqlite 需实验开关，改用：
NODE_OPTIONS=--experimental-sqlite npx tsx --env-file=.env src/server.ts
```

`MODELING_LLM_*` 和 `MODELING_TASK_LLM_*` 仅作为可选部署兜底。admin 可以在配置中心维护
Modeling 平台主模型与后台任务模型统一默认；保存后立即成为所有没有个人覆盖账号的运行时
默认，core 与独立 modeling-agent 在新任务开始时读取同一份配置，无需重启容器。

每个用户仍可在配置中心分别维护主模型和后台任务模型，个人端点覆盖优先于平台默认。点击
“使用平台默认配置”会清除当前用途的个人覆盖；后台任务默认未设置时跟随平台主模型。
配置接口不会向浏览器回显 API Key。

平台模板及用户快照中的 API Key 加密后存入 `modeling.db`。显式设置 `MODELING_LLM_CONFIG_ENCRYPTION_KEY`
时要求至少 32 字节；该变量为空时，Modeling 会在 `DATA_DIR/.llm-config-encryption-key`
首次生成并复用持久化随机密钥。生产环境建议通过 secret manager 注入显式密钥；自动密钥文件
必须与数据库一同持久化和备份/恢复。平台后台任务模型未设置时直接跟随平台主模型；用户
设置个人后台任务模型时仍优先使用个人配置。
平台默认和个人配置都将
`maxTokens`（单次输出上限）与 `contextWindow`（输入+输出总上下文窗口）分开保存；
上下文窗口应按所选模型 API 的实际上限填写。

服务默认 `http://localhost:5100`。

前端开发服务：

```bash
cd frontend
npm install
npm run dev
```

页面默认打开 `http://localhost:6700`，API 会转发到 `http://localhost:5100`。

## 认证与注册

直连独立 auth-server（详见 `.env.example` 与 docs/design.md 决策 3）：`AUTH_ISSUER_URL` 指向 auth-server，登录/续期走 OAuth2 `/token`（password/refresh 两种 grant），校验用 JWKS 本地 RS256 验签（不逐请求回查），注册转发 auth-server 的 `/register`（一步建号，无邮箱验证码）。

```bash
# auth-server 默认开启注册；如需关闭，设置 AUTH_ENABLE_REGISTRATION=false。
# AUTH_JWT_ISSUER 必须等于 auth-server 侧 AUTH_SERVER_ISSUER 的取值（默认 http://auth-server:5056），
# 不一致时登录报 503「认证服务暂不可用」并在日志提示验签配置错误
AUTH_ISSUER_URL=http://localhost:5056 \
AUTH_JWT_ISSUER=http://auth-server:5056 \
NODE_OPTIONS=--experimental-sqlite npx tsx --env-file=.env src/server.ts
```

前端登录页带「注册新账号」入口（用户名+邮箱+密码，一步提交即建正式账号，成功后回登录页并预填用户名）；
access token 过期后前端对 401 自动静默续期（单飞 `/auth/refresh`，上游明确拒绝才清凭据，刷新页面后回登录页；上游暂不可用则保留凭据待重试）。

评测闭环访问的 consumer 业务 API（`CONSUMER_API_URL`）是另一个服务；consumer 须信任同一 auth-server（remote 模式），本工具透传的专家 token 才能被其接受。

`MODELING_SERVICE_TOKEN` 是 Modeling、modeling-agent 与 consumer 共用的服务端令牌，至少 32 个随机字符，一键部署时自动生成。它保护 Agent 运行时模型配置读取、评测影子空间 provisioning 和 Trace 读取，不等同于全局管理员 Token。知识包导入、数据连接绑定和用例运行仍使用当前用户 Token；服务令牌只发送到部署期固定的内部服务地址，不会发送到页面覆盖的数据服务地址。

已知限制：

- 强制改密用户（如 auth-server 首启创建的 `admin`、管理员重置后的临时密码）会被本服务拒绝登录（403 `PASSWORD_CHANGE_REQUIRED`）——modeling 无改密入口，请先在管理端完成改密。
- auth-server 在 compose 里默认不发布宿主端口，modeling 需与它同网络部署，本机联调时再暴露 5056。

## 验证

```bash
# M1 冒烟：用 faux LLM（无需真实 key）跑通节点 1→3
DATA_DIR=./data/smoke MODELING_LLM_BASE_URL=x MODELING_LLM_MODEL=x npx tsx scripts/smoke-nodes.ts

npm run typecheck
npm test
npm run build

cd frontend
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
| GET | `/api/projects/:id/nodes/:nodeId/trace` | 读取已持久化的分析过程（仅项目 owner）|
| POST | `/api/projects/:id/documents` | 上传并解析资料（节点 2，multipart）|
| POST | `/api/projects/:id/analyze` | 触发初步分析（节点 3）|

除 `/health` 与 `/auth/*` 公开端点外均需 `Authorization: Bearer <token>`（经 auth-server JWKS 本地 RS256 验签）。
