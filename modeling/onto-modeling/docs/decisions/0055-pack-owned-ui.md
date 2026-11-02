# 0055 包自带界面：卡片按声明画，编辑器可以随包发布（修订详设决策 17）

## Context

详设决策 17 与 5.9.1、5.9.5 规定「包不能带前端代码」：页面、卡片、编辑器都由前端内置，包只能在清单里声明选哪个。
这样做的代价是包的编辑体验要等平台发版，第一个例子就是流程图编辑器——它只服务图示包，却长在平台的
`frontend/src/components/flowchart/`、`src/pages/flowchart/` 里，图示包停用了代码照样打进主包。

用户 2026-11-02 定：对话卡片保持声明式；编辑器和弹窗可以由包自带渲染代码，按来源分两档。约束有两条：

- 前端的登录令牌在 `localStorage`，同源加载的包代码能直接读到，再以用户（含管理员）身份调任意接口；
- 写入路径与「运行中只读」必须守住，包的代码不能绕过。

## Decision

1. **卡片仍按声明画。** 视图 `views/<类型>.yaml` 的 `card` 认 `title`、`subtitle`（字段名）、`chips`、`fields`
   （各最多 4 个字段）与 `component`（平台的专用卡片组件，如 `flowchart`）。一轮结束时宿主给每个新增或修改的
   插件产物出一张 `card_type: artifact` 的卡，服务端按声明取好值（`kernel/artifacts/cards.py`）；平台认得
   `component` 就用专用组件画，不认得按通用卡画。原来的 `card_type: flowchart` 保留兼容，图示包改成
   `card: {component: flowchart}` 选用同一个平台组件。卡片生成登记在 `app/wiring.py`，会话宿主在发完
   「这一轮改了什么」之后调 `project_events.round_cards`。
2. **双击打开什么由 `detail` 决定**：包自带编辑器（`detail.editor`）> 平台专用组件（`detail.component`）> 通用详情
   （`detail.layout`）。前端的选择在 `src/app/packUi.js` 的 `openModeFor`，包没启用（AR-01 里没有这个类型）时只说
   「插件已停用」，一行包代码都不加载。
3. **编辑器怎么声明**：清单 `contributes.editors: [{id, title, ui_api?}]`，视图 `detail.editor: <id>` 引用。入口按约定：
   - 官方包（入库来源 `builtin`，随镜像发布）：`ui/<id>/index.jsx`，默认导出 React 组件，接 `{artifact, onClose}`；
   - 上传包：`ui/<id>/index.html`，只是构建好的静态资源。
   AR-01 把 `detail.editor` 解析成 `{id, title, pack, version, mode: bundled|iframe, ui_api, entry}`。官方身份只认入库来源，
   清单里写 official 不算（同 ADR-0045 审查第 10 条）。
4. **官方包的编辑器随平台编译**：前端用 `import.meta.glob('@packs/*/ui/*/index.jsx')` 收进来，每个编辑器单独一个
   懒加载块，只有打开时才下载。包代码用 `@onto/` 引平台模块，裸模块名按平台前端的依赖解析（`vite.config.js` 的
   `packUi` 插件、单测的 `tests/pack-resolve-hooks.mjs` 同一条规则）。镜像的前端构建阶段把 `assets/packs` 一并拷进去。
   第一个样例：流程图编辑器整体迁到 `assets/packs/diagram/ui/flowchart/`，图示包升到 0.6.0；旧版图示包的
   `detail.component: flowchart` 由平台映射到同一个编辑器。
5. **上传包的编辑器只在不同源的 iframe 里跑**：
   - iframe `sandbox="allow-scripts"`，不给 `allow-same-origin`，文档是不透明源，读不到平台的 `localStorage`；
   - 静态资源由后端从包仓库按版本发（`GET /api/v1/pack-ui/<票据>/<ui/ 下的路径>`，PL-21），票据由
     `POST /api/v1/projects/{id}/packs/{name}/ui-sessions`（PL-20，所有者）签发，2 小时有效、只授权读这个包这个版本的
     `ui/`；每次读都按项目锁文件重查，包停用或换了版本立即 404；
   - 响应头 `Content-Security-Policy: sandbox allow-scripts; default-src 'none'; script-src 'self'; style-src 'self'
     'unsafe-inline'; img-src 'self' data: blob:; font-src 'self'; connect-src 'none'; form-action 'none'; base-uri 'none';
     frame-ancestors 'self'`，加 `nosniff`、`no-referrer`、`no-store`：直接打开也是沙箱；发不了任何请求，脚本只能是
     包里的外部文件；
   - 和平台只经 postMessage 窄接口通信（界面接口 `UI_API = 1.0`）：`context.get`、`artifact.read`、`artifact.save`、
     `chat.command`、`theme.get`。读写只限本包登记的产物类型，保存就是 AR-05（走写入事务，命中写入范围时如实回 423），
     命令只能是本包的（`/<包名>-…`），主题只给设计系统的几样变量。认不出的方法、越权的类型与命令一律拒绝。
6. **静态检查**：包里的前端代码只能放在 `ui/` 下（`frontend` 检查）；新增 `ui` 检查——编辑器入口要在、视图引用的编辑器
   要声明、`ui_api` 要满足；上传包的 `ui/` 只能是 html、js、css、json、map、txt、图片与字体，拒绝要编译的源码
   （jsx、ts、vue……）、`package.json`、`node_modules`、点文件与服务端可执行内容；符号链接与 `..` 照旧在解压与
   `symlinks` 检查拦下。凭据扫描加上 js、css。
7. **`ui/` 不进装配目录**：agent 用不到，两个包的 `ui/` 也不会互相冲突。

## Alternatives considered

- **全部包代码都进 iframe**（官方包也一样）：流程图编辑器要用平台的设计系统、写入范围锁、项目流、drawio 宿主，全都得
  经窄接口重做一遍，而官方包本来就随镜像一起审过、一起发，没有同源风险。
- **上传包代码同源加载、靠代码审核**：令牌在 `localStorage`，一行脚本就能拿走；审核挡不住混淆过的构建产物。
- **票据放查询串或 Cookie**：查询串里的票据，iframe 里引用的相对路径带不上；Cookie 会把平台的身份带进 iframe。
  放在路径里，相对引用自动带上，票据本身也只授权读这个包的静态文件。
- **编辑器声明只放在视图里**（`detail.editor: {entry: …}`）：一个编辑器服务多种产物时要写多份；放在清单里，
  静态检查和插件详情也能直接列出包带了哪些界面。

## Consequences

- 契约新增 PL-20、PL-21、`card_type: artifact`、`ArtifactType.view.detail.editor`、`ManifestSummary.contributes.editors`、
  窄接口协议与 `UI_API`（`_plan/contract-changes.md` B5a 节）。
- 上传包能读写的只有自己登记的产物，跨包读写、调助手、拿登录态都做不到；它仍能在 iframe 里显示任意内容，
  拿到的产物内容也能显示出来——这是给它开编辑器的本意。`img-src` 等只放同源，减少把内容带到外面的通道。
- 票据存在后端进程内存里，后端重启后 iframe 里后续的静态请求会 404，重新打开编辑器即可。
- 卡片的出卡点在会话宿主「一轮结束」那一处；变更集（B4b）改了一轮结束的流程后，这一处调用要跟着挪。
