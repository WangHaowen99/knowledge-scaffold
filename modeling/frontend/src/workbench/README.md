# 建模工作台前端（正式版）

按 [`docs/redesign/01-frontend-mockup.html`](../../docs/redesign/01-frontend-mockup.html) 实现的新工作台。
与现有应用**并存**：现有应用仍在 `index.html`，本工作台在 `workbench.html`，共用 Vite 工程与依赖。

```bash
npm run dev      # http://localhost:6700/workbench.html
npm run build    # 双入口构建
npm run smoke    # SSR 渲染每个场景，抓渲染期异常
```

`#/create` 是全屏创建上传页，其余走工作台。

## 结构

```
src/workbench/
  tokens.css  base.css  app.css     设计令牌 / 原语 / 布局，三层单向依赖
  App.jsx                           外壳 + hash 路由
  lib/store.js                      useSyncExternalStore 微容器
  lib/appStore.js                   共享状态与 actions
  lib/api.js                       Agent 状态、消息流、资料与知识包 API
  lib/mock/                        无 project 参数时的设计评审场景
  shell/  TopBar · Grip · ProcessDrawer
  left/   LeftPane（知识包树 / 影响热图 / 变更范围 / 资料列表）
  center/ CenterPane 按 focus 分发 → Flow · Diff · Impact · Conflict · Gate · Doc · File
  right/  RightPane（对话流）· ActivityLine · Composer · blocks/
  pages/  CreatePage
```

## 设计约束

来自 [`docs/redesign/02-feature-map.md`](../../docs/redesign/02-feature-map.md) §8.2，实现时不可退让：

1. **助手控制中栏焦点。** `focus` 决定中栏显示什么，用户可「取消跟随」。缺了它三栏就是三个互不相干的窗口。
2. **输入框永不锁死。** 有阻断性问题时问题卡与自由输入并存。
3. **没有 diff 就没有确认。**

另外三条实现层的：

- **只有一条流。** 状态、计划、问题、确认、策略都是右栏消息，没有第二个待办面板。
- **活动行三级递进。** 常驻一行 → 展开最近几条 → 过程抽屉。文案用工具返回的 `display` 字段，不解析工具名。
- **确认卡按 `tier` 分诊。** 口径展开 / 结构折叠 / 索引隐藏；批量模式含抽检，复核方式要留痕。

## 性能

| 首屏 | gzip |
| --- | --- |
| `react` chunk（可长期缓存） | 60.3 KB |
| `workbench` 应用代码 | 18.2 KB |
| `workbench` 样式 | 5.2 KB |
| **合计** | **≈ 84 KB** |

- **不引 webfont。** 中文字体是首屏最大的性能风险，系统字体栈零成本。
- **不引图标库、状态库、路由库。** 12 个图标手写内联 SVG；状态是 `useSyncExternalStore` 微容器；两个页面用 hash 判断。
- **`react-markdown` 链路（gzip 47 KB）单独成块且不预加载**，只在打开 `.md` 文件时按需拉取。
- **三栏各自 `contain: layout paint`**，一栏重绘不波及其他两栏。
- **对话流消息用 `content-visibility: auto`**，长会话滚动不掉帧。
- **拖拽分栏走 DOM style + rAF**，拖动过程零 React 重渲染，松手才写回状态。
- 选择器订阅：`useStore(store, sel)` 的 `sel` 必须返回稳定引用，需要多个字段就分别订阅，别在 selector 里现造对象。

## 真实运行

- `workbench.html?project=<project-id>` 进入真实项目，`#/create` 创建并上传资料。
- 真实模式通过同源代理订阅 `modeling-agent` 的持久化 SSE；断线后从最后序号续传。
- 不带 `project` 参数时保留 mock 场景，仅用于设计评审、截图和 SSR smoke。
- 工具返回值的 `display` 字段直接进入活动行，不在浏览器里解析内部工具名。
