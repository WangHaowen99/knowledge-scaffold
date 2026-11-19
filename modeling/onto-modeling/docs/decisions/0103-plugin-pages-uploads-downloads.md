# 0103 插件自己的页面、上传位与下载：页面复用上传包编辑器的隔离 iframe

## Context

**目标**：上传安装的插件（插件中心上传，不随平台发版）能带一个自己的页面，在页面上传输入文件、发命令让助手处理、看结果、
下载产物。第一个用户是公共本体插件：上传多个知识包，抽出公共部分（父类），看实类、术语表、规则模板、继承映射，下载结果。
成功标准：不改平台代码就能装上这样的插件并跑通「上传 → 抽取 → 看结果 → 下载」。

**基本约束**

1. 上传包不可信：前端登录令牌在 `localStorage`，包代码不能在平台源下运行（ADR-0055）。
2. 页面只发命令、不跑助手（ADR-0040）；要做语义判断的活由助手在对话里做。
3. 写主线都走写入事务（ADR-0046）；助手的写入不可信，它要读的输入不能被它改掉。
4. 发布只读 `ontology/` 的本体目录和资料表格（`publish/layout.py`），`ext/` 不进知识包。
5. 平台已有一套知识包上传校验（PC-01：子进程、限时、限大小、路径安全、manifest 合规）。
6. 原来的内置页（scenarios、scripts）、上传包编辑器、窄接口 1.0 行为不变。

## Decision

1. **页面就是「不绑产物的编辑器」**（用户 11-19 定用 iframe，不用平台按声明画）。清单 `contributes.pages[]` 里 id 不是
   内置页的，要带 `ui: <编辑器 id>`，指向本包 `contributes.editors` 里声明的编辑器；入口、票据、静态资源、隔离头、
   导航走了就拆掉，全部沿用 ADR-0055。左栏与路由的 id 是 `<包名>:<页面 id>`（`#/p/<项目>/<包名>:<页面 id>`），不会和
   内核页、内置页、别的包撞；页面整块是 iframe，标题和按钮由插件自己画。官方包（随平台编译）暂不支持自带页面，静态检查
   拦下。`PAGE_API` 升到 1.1。
2. **窄接口 1.1**（`UI_API` 1.0 → 1.1，1.0 的方法不变）。页面模式下 `context.get` 带 `page` 与 `uploads`、`artifact`
   为空，新增方法：
   - `artifact.list {type, filter?, q?, cursor?, limit?}` → AR-02，只限本包登记的类型，`limit` ≤200；
   - `upload.list {slot}`、`upload.put {slot, name, data: ArrayBuffer}`（≤50 MB，经端口转交）、`upload.delete {slot, id}`
     → PL-29、PL-28、PL-30，只限本包声明的上传位；
   - `file.download {path}` → PL-31，只认插件目录 `ext/<包名>/` 下的相对路径，平台在父窗口触发下载，2 秒一次；
   - `studio.open {kind, key}`：平台跳到 Studio 的条目；
   - `command.tasks`：本页发出的、还在跑的命令（只给 `task_id、state、title、percent`）。
   新推送：`upload.changed {slot}`、`command.progress`。上传位与本包产物类型由 PL-20 一并返回。
   页面模式的 `readonly` 恒为 false：上传、删除由服务端按写入范围锁判，锁着时如实回 423。
3. **上传位**：清单 `contributes.uploads: [{id, title, kind, max}]`，`kind` 目前只有 `knowledge-package`，`max` 1～20。
   文件落在主线工作区 `ext/<包名>/uploads/<上传位>/<id>/`：`id` 由知识包 manifest 的 `id` 算出（同一个包重传就替换），
   只留 `manifest.yaml` 与 `objects/ operations/ rules/ metrics/ scripts/` 的文本（各目录的 `index.md` 不留，合计
   ≤20 MB），另写 `upload.json`。校验直接复用 PC-01（`package_compare.index_archive`）。网页上传、删除经 `ChangeTx`
   （来源 web）提交到主线；只有项目所有者能操作。
4. **助手只读上传区**：变更集把 `ext/*/uploads/` 当受保护路径（和资料区同一处，`changesets/worktree.is_protected`），
   副本里的改动不进变更集、合入时取主线的版本。两个引擎同一口径，不靠守卫。
5. **视图 `card: false`**：插件的清单、汇总类产物一轮会改很多份，这类视图写 `card: false`，对话里不逐份出卡
   （公共本体插件只留「状态」一张）。
6. **下载**：PL-31 `GET /projects/{id}/packs/{name}/files?path=`（所有者，可用 `?token=`）只读主线已提交的内容：
   文件原样，目录用 `git archive` 打成 `<包名>-<目录名>.zip`；路径不能越出插件目录，不能有隐藏段。

## Alternatives considered

- **平台按声明画页面**（上传块、按钮块、产物列表块、下载块）：界面统一、插件不带前端代码；但每加一种块都要平台发版。
  用户选了 iframe：插件自己决定页面长什么样。
- **上传文件放工作区外、抽取时再挂进沙箱**：要给两个引擎各做一次挂载，助手读不到原样文件时还得加读取工具；放进
  `ext/` 后助手直接读，保护靠变更集的受保护路径，现有机制。
- **iframe 自己 fetch 上传与下载**：要给 iframe 开 `connect-src` 和 `allow-downloads`，等于给不可信代码一条直通平台
  接口的路；现在请求都经窄接口、由平台带上登录态代办，越权的在平台和服务端两道拒绝。
- **上传后保留原 ZIP**：git 里放二进制、体积大；助手只需要本体文本。

## Consequences

- 契约新增 PL-28～PL-31、`contributes.pages[].ui`、`contributes.uploads`、`ManifestSummary.contributes.uploads`、
  PL-12 `pages[]` 的 `page`、`ui`，PL-20 响应的 `types`、`uploads`，窄接口 1.1（`docs/plugin-architecture/contract-changes.md`）。
- 插件页面能看到的只有本包登记的产物、本包上传位里的文件清单，读不到 `ontology/` 的条目；要在页面上列本体条目，
  插件得自己写一份清单产物（公共本体插件的 `ext/common-ontology/real.md`）。
- 插件页面的样式要插件自己对齐平台（`theme.get` 给配色变量）。
- 输入知识包的文本进项目的 git 历史；删掉之后历史里仍在（和资料区一样）。
