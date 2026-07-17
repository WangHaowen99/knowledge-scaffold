# Consumer 前端阶段 1：聊天运行基础验收记录

## 1. 范围

- 分支：`codex/frontend-chat-runtime`
- 基线：阶段 0 `frontend-unify@c225710`
- 日期：2026-07-15
- 目标：收敛认证、会话、SSE、活动流与 Workspace 运行状态所有权。

## 2. 正式目录迁移

| 原路径 | 正式路径 | 状态 |
|---|---|---|
| `src/hooks/useAuth.js` | `src/features/auth/useAuth.js` | 已迁移，原文件删除 |
| `src/hooks/useAgenticStream.js` | `src/features/chat/hooks/useAgenticStream.js` | 已迁移，原文件删除 |
| `src/hooks/useChatSessions.js` | `src/features/chat/hooks/useChatSessions.js` | 已迁移，原文件删除 |
| `src/hooks/useStreamManager.js` | `src/features/chat/hooks/useStreamManager.js` | 已迁移，原文件删除 |
| `src/utils/auth.js` | `src/shared/api/auth.js` | 已迁移，原文件删除 |
| `src/utils/logger.js` | `src/shared/lib/logger.js` | 已迁移，原文件删除 |
| `src/utils/messageBuilder.js` | `src/features/chat/lib/messageBuilder.js` | 已迁移，原文件删除 |
| `src/utils/sessionModel.js` | `src/features/chat/lib/sessionModel.js` | 已迁移，原文件删除 |
| `src/utils/streamHelpers.js` | `src/features/chat/lib/streamHelpers.js` | 已迁移，原文件删除 |
| `src/utils/streamLifecycle.js` | `src/features/chat/lib/streamLifecycle.js` | 已迁移，原文件删除 |
| `src/utils/streamResume.js` | `src/features/chat/lib/streamResume.js` | 已迁移，原文件删除 |
| `src/utils/workspacePreferences.js` | `src/features/chat/lib/workspacePreferences.js` | 已迁移，原文件删除 |
| `src/utils/workspaceRuntime.js` | `src/app/providers/workspaceRuntime.js` | 已迁移，原文件删除 |

冻结入口中的引用同步指向同一套正式 runtime，不再保留重复的 session/stream 实现。

## 3. 状态所有权与一致性

- 服务端 session metadata 固化 `agent_mode`、模型 profile 与创建时选定的知识包。
- 创建会话时，后端在请求 Workspace 作用域内验证知识包存在且启用；无选择时不写 metadata。
- StreamManager 独占活动连接、run cursor、去重集合、队列状态和逐会话消息缓存。
- 页面仅持有当前选择、输入草稿和展示状态。
- auth 请求在首次发送时固定 Workspace；401 刷新后的重试不会改用切换后的 Workspace。
- 退出登录或切换 Workspace 会静默断开本地连接并清空运行缓存，不会把 AbortError 写成用户取消消息。
- 登录凭据变化使用 epoch 防止并发 refresh 在退出后恢复旧 token。
- 会话请求同时绑定 Workspace scope epoch 和 selection epoch；旧 Workspace 响应、慢返回的旧选择均不能覆盖当前页面。
- 刷新后会恢复列表中全部 queued/running 会话。每个会话先读取持久事件，再从 `last_seq` 续传，避免后台会话只保留半段内容。
- SSE 解析保留 `id`、多行 `data` 与无空行 EOF 尾帧；重连继续按 sequence/event id 去重。
- `/events` 返回 410 且 run 已终态时正常收敛，不进入无效重连循环。
- 用户停止必须取得后端取消成功；失败时保留连接并显示 `cancel_error`。删除会话也只在停止和 DELETE 均成功后提交本地删除。

## 4. 自动验收

### 前端

```bash
cd consumer/frontend
npm run lint
npm test
npm run build
```

- ESLint：通过，0 error / 0 warning。
- Node tests：63 项通过。
- 入口检查：79 个生产模块，45 个可达，34 个阶段 2—5 待迁移白名单，0 个未批准孤儿模块。
- Vite build：通过。

新增回归覆盖：认证 refresh/退出竞态、请求 Workspace 固定、session scope/选择竞态、全量活动会话恢复、SSE id/多行/EOF、静默 scope abort、终态 run 判断和知识包锁定。

### 后端

```bash
cd consumer/backend
PYTHONPATH=. venv/bin/python -m pytest tests/test_session_routes.py tests/test_auth_security.py -q
```

- 22 项通过。
- 会话创建的知识包校验覆盖：有效、空值省略、不存在、禁用和 Workspace 作用域。

## 5. UI 验收

使用当前分支独立 Vite 服务 `127.0.0.1:6601` 与本地后端完成：

- 登录成功，历史会话列表与当前会话恢复成功。
- 连续切换历史会话后，标题、消息、锁定配置与 Trace 同步更新。
- 会话锁定的模型/知识包/Agent 模式保持只读。
- 1440、1024、768 三档宽度可用；768 下 Trace 进入遮罩抽屉。
- 浏览器控制台 error：0。

截图：

- [1440 × 1000](../../frontend/local-browser-artifacts/frontend-stage1/chat-1440x1000.png)
- [1024 × 1000](../../frontend/local-browser-artifacts/frontend-stage1/chat-1024x1000.png)
- [768 × 1000](../../frontend/local-browser-artifacts/frontend-stage1/chat-768x1000.png)

## 6. 性能对比

| 资源 | 阶段 0 | 阶段 1 | 变化 |
|---|---:|---:|---:|
| 入口 JS | 475.31 kB | 483.38 kB | +1.70% |
| 入口 JS gzip | 149.99 kB | 152.45 kB | +1.64% |
| 全局 CSS | 283.28 kB | 283.28 kB | 0 |
| Knowledge lazy chunk | 28.31 kB | 28.31 kB | 0 |
| Config lazy chunk | 61.84 kB | 61.84 kB | 0 |

增量来自 session scope、SSE 边界和全量运行恢复逻辑，低于 10% 门槛。

## 7. 阶段结论

阶段 1 退出条件已满足。当前生产入口只使用一套 auth/session/stream runtime；阶段 2 可以在该状态模型上实施消息、Trace 和观测能力。

下一阶段：[阶段 2：聊天、Trace 与观测](./frontend-stage2-chat-observability.md)。
