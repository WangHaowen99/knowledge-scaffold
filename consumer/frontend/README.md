# Meta-wise Consumer Frontend

Meta-wise Consumer 的唯一生产前端。应用使用 React 19 + Vite，统一承载认证、聊天运行、知识包、Evolution、配置管理和可观测性。

## 能力范围

- JWT 登录、首次强制改密和账号安全
- 多会话聊天、后台流式运行、SSE 游标续传、等待用户输入和取消
- 专业化消息、完整 Trace、复制与反馈、会话性能和全局观测
- 知识包文件、版本、导入、数据源、本体图运行态
- Operation / Skill Evolution 生命周期、Diff、漂移与审批
- 全局、Workspace、个人三级配置
- 系统配置导入/模板/连接测试、模型引用保护
- Workspace、用户和 MCP 管理
- 浅色、深色、跟随系统和主题色

## 目录

```text
src/
├── main.jsx                 # 唯一 React 挂载入口
├── app/
│   ├── App.jsx              # Provider 与应用入口
│   ├── AppShell.jsx         # 导航、路由编排与 Workspace 外壳
│   ├── onboarding/
│   └── providers/
├── features/
│   ├── auth/
│   ├── chat/
│   ├── evolution/
│   ├── knowledge/
│   ├── observability/
│   └── settings/
└── shared/
    ├── api/
    ├── lib/
    ├── styles/
    └── ui/
```

生产代码不保留第二套入口，也不使用 `redesign`、`legacy`、`old` 或 `v1` 目录。

## 本地运行

```bash
cd consumer/frontend
npm install
npm run dev
```

开发地址为 `http://localhost:6600`。Vite 将同源的 `/auth`、`/agentic`、`/api` 和 `/health` 代理到 `http://127.0.0.1:5055`。

前端本地开发不要求 `.env`。可选设置见 [`.env.example`](.env.example)。

## 验证

```bash
npm run lint
npm test
npm run build
npm run check:architecture
npm run check:entry
```

`npm test` 会执行全部 `node:test` 用例和架构门禁。架构门禁包括：

- feature 不反向依赖 app
- shared 不依赖 app 或具体 feature
- 跨 feature 只能使用目标 feature 的公开 `index`
- `shared/lib` 不引入 React，`shared/ui` 不调用业务 API
- 扫描 `src` 全部文件和 `public` 资源的历史目录，禁止根级生产文件
- 仅 `main.jsx` 可引入 `react-dom/client` 或创建 React root
- 检查 CSS 相对 `@import` 的可解析性、分层边界和跨 feature 引用
- 检查潜在孤儿 CSS class 及同元素 compound class 组合
- 检查 JS、JSX、CSS 全部由 `main.jsx` 可达，批准孤儿列表必须为空

GitCode workflow [consumer-frontend-ci.yml](../../.gitcode/workflows/consumer-frontend-ci.yml) 会在 `develop`、`frontend-unify`、`codex/frontend-*` 分支 push 和目标为 `develop` 的 PR 上执行安装、lint、测试、架构门禁和 production build。

## 运行边界

### 认证

`features/auth` 负责登录态、capability、Workspace 切换和改密。认证请求集中在 `shared/api/auth.js`。

### 聊天

`features/chat` 负责会话状态、SSE 协议、运行恢复、智能滚动、消息展示和 Trace。服务端 session metadata 是会话级模型、知识包和 Agent 模式的权威来源。

### 知识与 Evolution

`features/knowledge` 负责知识包浏览、导入、版本、数据源和本体图；`features/evolution` 通过公开入口向知识工作台提供进化能力。大型页面保持按需加载。

业务数据源按知识包隔离，前端使用 `GET/PUT/DELETE /agentic/ontologies/:id/data-source`、`POST /agentic/ontologies/:id/data-source/test` 和 `GET /agentic/business-data/provider-schemas`，配置入口位于知识包的数据源页签。

### 配置与观测

`features/settings` 负责三级配置、系统/模型/Workspace/用户/MCP 管理；`features/observability` 负责会话性能、运行详情和全局观测。

## Docker

- 前端 Compose：[docker-compose.yml](docker-compose.yml)
- Consumer 全栈 Compose：[../docker-compose.yml](../docker-compose.yml)

生产镜像由 nginx 提供静态资源，并代理 `/auth`、`/agentic`、`/api` 和 `/health`。

迁移阶段和验收证据见 [前端收敛方案](../docs/migration/frontend-convergence-and-enhancement-plan.md)。
