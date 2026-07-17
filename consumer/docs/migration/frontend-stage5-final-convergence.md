# Consumer 前端阶段 5：最终物理收敛验收记录

## 1. 范围

- 逻辑交付单元：`frontend-final-convergence`
- 基线：阶段 4 `e506d8a`
- 日期：2026-07-16
- 目标：完成唯一入口、正式目录、样式、依赖边界、资源和开发文档的最终收口，并关闭最终验收发现的认证竞态、Workspace fail-closed 与移动端可访问性风险。
- 上一阶段：[阶段 4：配置、管理与权限](./frontend-stage4-settings-admin.md)
- 总体方案：[Consumer 前端收敛与能力加强计划](./frontend-convergence-and-enhancement-plan.md#阶段-5最终物理收敛)

## 2. 唯一入口与正式目录

`src/main.jsx` 只挂载 `app/App.jsx`，仓库内只有这一处 `createRoot`。旧入口回退注释、环境开关和第二套 App 均不存在。

本阶段完成以下物理迁移：

| 原路径 | 正式路径 |
|---|---|
| `redesign/RedesignApp.jsx` | `app/App.jsx` |
| `redesign/AppShell.jsx` | `app/AppShell.jsx` |
| `redesign/OnboardingWizard.jsx`、`wizardState.js`、`utils/onboardingLayout.js` | `app/onboarding/**` |
| `redesign/AuthContext.jsx`、`LoginPage.jsx`、`permissions.js`、`useAuthContext.js` | `features/auth/**` |
| `redesign/ChatWorkspace.jsx`、`ChatHistorySidebar.jsx`、`chatUtils.js` | `features/chat/**` |
| `redesign/KnowledgeWorkbench.jsx`、`KnowledgeDataSources.jsx`、`KbSidebarTree.jsx`、`useKbBrowser.js`、`OntologyGraphPanel.jsx` | `features/knowledge/**` |
| `redesign/EvolutionWorkbench.jsx`、`features/knowledge/evolution/**` | `features/evolution/**` |
| `redesign/ConfigCenter.jsx` | `features/settings/ConfigCenter.jsx` |
| `redesign/Markdown.jsx`、`ui.jsx`、`UiFeedback.jsx` | `shared/ui/**` |
| `redesign/theme.js` | `shared/styles/theme.js` |
| `utils/requestErrors.js` | `shared/api/errors.js` |
| `redesign/version.js` | `app/appMeta.js` |

最终生产结构只有 `app / features / shared` 三层。旧 `components`、`hooks`、`utils`、`redesign` 目录均已移除。

## 3. 样式收敛

旧 `src/index.css`（3819 行）和 `redesign/redesign.css`（1015 行）已删除。保留的有效规则按所有权拆分到：

- `shared/styles/base.css`：全局重置、设计变量、主题、深色与 reduced-motion。
- `shared/ui/ui.css`：按钮、表格、Modal、Markdown、Skeleton、Toast、Dialog。
- `app/appShell.css`、`app/onboarding/onboarding.css`：应用壳和向导。
- `features/auth/login.css`：登录与认证响应式布局。
- `features/chat/chat.css`：会话、消息、Trace 和聊天响应式布局。
- `features/knowledge/knowledge.css`、`features/knowledge/components/kbSidebarTree.css`：知识包工作台、数据源和入口侧栏目录树。
- `features/evolution/workbench.css`：Evolution 工作台基础布局。
- `features/settings/settings.css`：三级配置布局。
- 各阶段已建立的 graph、import、observability、model、system、admin、MCP 和 auth 专属样式继续由对应 feature 直接引入。

清理了已被正式实现替代的旧 MCP、静态本体图、旧历史会话、旧消息步骤、旧配置页头和旧强制改密 selector。新增 CSS class/compound selector 门禁扫描 19 份样式表：只从 JSX `className` 及相关表达式收集引用，运行时动态类和组合显式登记；潜在孤儿 class 或同元素从未共同发出的组合会使测试失败。

响应式规则也归入所属 feature：应用壳不再承担登录、聊天、知识或设置的具体样式。

## 4. 架构门禁

新增 `scripts/check-import-boundaries.mjs`：

- feature 不能反向依赖 app。
- shared 不能依赖 app 或具体 feature。
- 跨 feature 只能经目标 feature 的公开 `index`。
- `shared/lib` 禁止 React 依赖。
- `shared/ui` 禁止业务 API 调用或依赖 `shared/api`。
- `src` 全部文件与 `public` 全部资源参与历史目录检查；`src` 禁止根级生产文件。
- 仅 `main.jsx` 可引入 `react-dom/client` 或创建 React root。
- 强制唯一 `app/App.jsx`，并限制 `main.jsx` 的本地 import。
- JS、JSX、CSS 均参与路径检查；CSS 相对 `@import` 必须可解析、留在 `src` 内并遵守分层，跨 feature CSS 依赖被禁止。

入口扫描已扩展到 CSS；JS、JSX、CSS 都必须从 `main.jsx` 可达。批准孤儿列表保持为空。

`npm test` 现在执行全部 Node 测试后串行执行 import/CSS 边界、潜在孤儿 class 与 compound selector、入口可达性检查。仓库根目录 `.gitcode/workflows/consumer-frontend-ci.yml` 在 `develop`、`frontend-unify`、`codex/frontend-*` 分支 push 和目标为 `develop` 的 PR 上执行安装、lint、上述测试和 production build。

## 5. 删除范围

除迁移后的旧源文件外，本阶段同步删除：

- `src/index.css`
- `src/redesign/**`
- 空的 `src/components`、`src/hooks`、`src/utils`
- `prototype/NOTES.md`
- `prototype/console-redesign-prototype.html`
- 无引用的 `public/icon.svg`、`public/icon-option-agent.svg`

当前品牌资源 `public/brand-mark.png` 保留，并继续由 favicon、登录、应用壳和聊天空状态使用。

### 5.1 最终验收修复

- 认证凭据收敛为单一 versioned envelope；旧双 key 在启动时原子迁移并删除。
- 登录、刷新和登出在 Web Locks 可用时跨标签串行；无 Web Locks 时同一页面仍使用 Promise 队列，所有写入均以 session/version 比较防止迟到请求覆盖新身份。
- 受保护响应正文和流式 reader 绑定认证身份；换号后禁止继续读取旧账号数据。
- Workspace 列表增加 loaded/loading/error/epoch 状态，聊天、历史侧栏、知识树、会话观测和 Workspace 配置统一 fail-closed。
- 零工作空间提供管理员创建入口，普通用户显示联系管理员说明；加载失败保持可重试且不显示旧数据。
- 移动导航和操作向导补齐焦点圈、Tab/Shift+Tab、Escape、焦点恢复与背景 inert。
- 会话列表、创建和读取路由将缺失或歧义 Workspace 的输入错误稳定映射为 400，同时保留 404/403 契约。

## 6. 自动验收

### 6.1 前端

```bash
cd consumer/frontend
npm run lint
npm test
npm run build
npm run check:architecture
git diff --check
```

结果：

- ESLint：通过，0 error / 0 warning。
- Node tests：231 / 231 通过。
- Import/CSS 边界：130 个代码模块和 19 份样式表通过。
- CSS class/compound selector：19 份样式表通过。
- 入口可达性：110 / 110 个生产 JS、JSX、CSS 模块可达；批准孤儿和未批准孤儿均为 0。
- Vite production build：通过，2050 个模块完成转换。
- `git diff --check`：通过。
- 生产历史目录、第二入口和旧资源残留扫描：通过。

### 6.2 本地启动与浏览器验收

在已授权的系统网络环境中启动 Vite `6600`、认证服务 `5056` 和 Consumer backend `5055`，三个健康检查均返回 200。使用真实 Chromium 和确定性 API 响应完成以下验收：

- 单一认证 envelope 的旧 key 启动迁移、旧 key 清理和唯一 session id。
- `alpha 登录 → 退出 → beta 登录` 的真实 UI 流程；最终 token、用户和 UI 一致。
- Web Locks 下登出吊销与新登录串行化；登录超时释放锁；绕锁的新 session 不被迟到登出清除。
- 换号后旧响应正文不可读，尚未收到数据块的 SSE reader 立即以 `AuthStateChangedError` 结束。
- 工作空间加载、零工作空间、失败重试和账号/Workspace 切换均 fail-closed；旧会话、知识树和会话观测内容不会回显。
- 会话搜索、置顶、删除，配置中心保存/测试写路径，零工作空间创建入口，以及两个标签页的 Workspace 隔离。
- 桌面、窄屏和移动布局切换；移动导航与操作向导的 Tab、Shift+Tab、Escape、焦点恢复和背景 inert。

最终干净会话确认用户 `beta`、工作空间 `Workspace A`、旧 `onto_agent_token` 不存在，浏览器控制台为 0 error / 0 warning。

上述写路径使用本地真实页面和确定性 API 响应，验证前端请求、状态和交互闭环。它不等价于真实外部模型、业务数据库或 Evolution 任务成功链路；这些边界在第 9 节单列。

### 6.3 后端全量回归

```bash
cd consumer
PYTHONPATH=backend backend/venv/bin/python -m pytest backend/tests
```

在已授权的系统环境中共收集 1134 项：1132 passed、2 skipped、4 warnings，耗时 125.20 秒。最终后端改动完成后另执行：

- `test_session_routes.py + test_remote_auth.py`：39 passed、1 warning。
- `test_workspace_missing_contract.py + test_session_routes.py`：21 passed。

沙箱内首次运行仍会因禁止绑定 `127.0.0.1` 随机端口而失败；同一命令在系统环境中通过，确认属于执行环境限制。会话路由对缺失/歧义 Workspace 的 `ValueError` 返回 400，显式不存在 Workspace 仍保持 `WORKSPACE_NOT_FOUND` 404，ACL 403 契约未改变。

### 6.4 最终风险回归

- 认证与访问定向测试：27 / 27 通过。
- 后续只读代码复审：阻断项 0。
- `git diff --check`：通过。

## 7. Bundle 对比

| 资源 | 阶段 4 | 阶段 5 | 变化 |
|---|---:|---:|---:|
| 入口 JS | 525.53 kB | 540.57 kB | +2.86% |
| 入口 JS gzip | 165.28 kB | 170.13 kB | +2.93% |
| 入口 CSS | 140.22 kB | 59.76 kB | -57.38% |
| 全部构建 CSS | 182.84 kB | 111.16 kB | -39.20% |
| Knowledge lazy CSS | 10.71 kB | 18.04 kB | 正式工作台样式归入按需 chunk |
| Observability lazy CSS | 17.62 kB | 17.62 kB | 不变 |
| Config lazy CSS | 分散在全局 CSS | 1.45 kB | 改为按需加载 |
| Knowledge lazy JS | 72.09 kB / gzip 23.52 kB | 72.09 kB / gzip 23.46 kB | 无回归 |
| Observability lazy JS | 40.97 kB / gzip 12.90 kB | 40.97 kB / gzip 12.90 kB | 不变 |

入口 JS 增长 2.86%、gzip 增长 2.93%，低于单阶段 10% 门槛。阶段 4 全部 CSS 口径为全局 140.22 kB 加 6 个 lazy CSS chunk，按构建产物字节汇总为 182.84 kB；当前汇总为 111.16 kB，总 CSS 减少 39.20%，入口首载 CSS 减少 57.38%。知识、观测和设置继续按需加载。Vite 仍提示入口 JS 超过默认 500 kB，该提示不影响构建通过。

### 7.1 页面加载性能

同一台本机、同一 Vite 服务下连续导航三次：

| 指标 | 当前三次结果（ms） | 当前中位数 | 阶段 0 中位数 | 变化 |
|---|---|---:|---:|---:|
| `responseStart` | 40.6 / 43.8 / 41.9 | 41.9 | 42.4 | -1.2% |
| `DOMContentLoaded` | 89.7 / 102.4 / 88.0 | 89.7 | 105.9 | -15.3% |
| `load` | 90.6 / 103.3 / 88.9 | 90.6 | 107.8 | -16.0% |

三个指标均未出现超过 10% 的退化。

## 8. 文档与台账

- README 已更新为唯一入口、正式目录、运行边界和架构命令。
- `CLAUDE.md` 已移除“冻结旧 App”说明，改为最终分层和门禁约束。
- GitCode workflow 已接入 `develop`、`frontend-unify`、`codex/frontend-*` 分支 push 与目标为 `develop` 的 PR，执行 lint、测试、架构门禁和构建；首次远端运行需在分支推送后确认。
- 阶段 0 台账保留历史快照，并追加最终闭环链接。
- 回退只依赖 Git 提交、发布版本和部署回滚，不依赖仓库内第二套 UI。

## 9. 阶段结论

阶段 5 的代码与本地自动验收项已完成：唯一生产入口和正式目录已建立，`redesign` 与历史目录已删除，样式按所有权拆分并通过潜在孤儿 class/组合检查，跨层和跨 feature 依赖已接入仓库 CI workflow，全部生产 JS、JSX、CSS 均由入口可达。

至此，阶段 0—5 的实现、本地自动门禁、受控真实浏览器功能/UI 与写路径、页面加载性能复测均已完成；最终复审没有代码阻断项。

仍需外部联调环境提供结果的边界是：真实模型对话及多会话运行、NL2SQL/真实业务数据库查询、Operation/Skill Evolution 的真实生成/审批/落盘成功链路，以及分支推送后的远端 workflow。它们依赖本仓库之外的凭据、模型、数据库或运行服务，不作为本次本地代码收敛验收的完成声明。
