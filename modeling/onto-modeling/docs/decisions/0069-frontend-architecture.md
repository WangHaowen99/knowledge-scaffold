# 0069 前端架构：哈希路由、用 fetch 读 SSE、mock 不进生产包

## Context

新前端（`frontend/`，设计稿第五版）在 F1 定下三件和后端、部署都有关的事，当时只写在代码注释里：
页面地址怎么组织、四条 SSE 事件流（契约 1.11：对话流、项目流、用户流、日志跟随流）怎么读、开发用的 mock 服务端
怎么保证不进生产包。I1 联调前补成 ADR，免得后来的人在反向代理、鉴权或打包上改出问题。

## Decision

1. **哈希路由**（`src/app/router.js`）：地址形如 `#/login`、`#/projects`、`#/p/<项目>/<页面>?参数`、`#/<全局页>`。
   - 后端用 `StaticFiles(html=True)` 直接发 `frontend/dist`，没有「找不到就回 index.html」的回退；哈希后面的部分不发给服务端，
     刷新、深链接、书签都不需要服务端改写规则，挂在反向代理的子路径下也一样。
   - 页面地址和 `/api/v1/…`、`/drawio`、上传包界面 `/api/v1/pack-ui/…` 不会撞路径。
   - 页面 id 与设计稿一致（`wb-l2`、`studio`、`ops-ctl` …），流程节点动作的 `target`、建议条的 `navigate`、通知的跳转都直接用它。
2. **SSE 用 fetch 读，不用 `EventSource`**（`src/api/sse.js`）：
   - 要带 `Authorization: Bearer` 头（令牌在 `localStorage`，过期时先走 A-04 刷新再重连）；`EventSource` 不能设请求头，
     只能把令牌放进查询串，会进访问日志和代理日志。
   - 续传按流不同：对话流的 id 是整数，重连带 `after=<id>`；项目流、用户流的 id 是 `<epoch>.<n>`，重连带 `last_event_id`，
     断档时服务端先发 `stream.reset`，前端按 `resync` 重拉快照。`EventSource` 自动重连只会带 `Last-Event-ID` 头，
     也不暴露重连时机，做不了按流的续传与重拉。
   - 心跳超时（45 秒没有任何帧，含注释心跳）主动断开重连；退避 3 秒起、最长 30 秒，服务端给 `retry` 时按它。
   - 错误响应（401、403、404、503 维护）能读到契约 1.5 的错误体，按同一套 `ApiError` 处理；`EventSource` 只有一个
     不带状态码的 `error` 事件。
   - mock 服务端是一个 `fetch` 实现（`StreamHub` 推流），fetch 读法在 mock 与真实后端下走同一段代码。
3. **mock 不进生产包**：
   - 开关是构建期常量 `__API_MOCK__`（`vite.config.js` 的 `define`）：只有 `mode !== 'production'` 且 `VITE_API_MOCK=1`
     时为 `true`，`npm run build`（生产模式）恒为 `false`。
   - 唯一的入口是 `src/api/transport.js` 里 `if (__API_MOCK__) await import('./mock/index.js')`；常量为 `false` 时这段
     死代码被摇掉，`src/api/mock/` 整个目录不进产物。别处不得静态导入 `api/mock/`（单测除外）。
   - 只给设计评审用的「对话卡片与状态」页同样按 `import.meta.env.DEV || __API_MOCK__` 注册，生产构建里没有。
   - 演示状态用查询串 `?mock=键:值,…` 切换（也可存在 `localStorage` 的 `onto.mock`），只在 mock 构建里有意义。

## Alternatives considered

- **history 路由（`/p/<项目>/<页面>`）**：地址更好看，但后端静态挂载要加 SPA 回退、反向代理子路径要改写，
  而且页面路径和 `/api`、`/drawio` 共用一个命名空间；没有换来功能上的好处。
- **`EventSource` + 查询串里的令牌**：少写一个解析器，但令牌进日志、续传与重拉做不了、错误体读不到。
- **mock 走独立的 dev server 或 MSW**：要多一个依赖或进程；构建期常量加动态 import 已经能保证生产包里没有 mock。

## Consequences

- 部署不用为前端配路由回退；反向代理只要转发 `/api`、`/drawio` 与静态文件。
- 新增事件流照 `sse.js` 的写法，不要引入 `EventSource`；要改续传参数时前后端一起改契约 1.11。
- 新增 mock 接口放 `src/api/mock/routes-*.js`，由 `mock/index.js` 汇总；检查产物里没有 mock 可以在 `dist/` 里搜
  mock 专有的夹具文字（端到端脚本对「对话卡片与状态」页做过同样的检查）。
- 哈希路由下页面地址不进服务端日志，排查时要靠前端带的 `origin.page` 与链路 ID。
