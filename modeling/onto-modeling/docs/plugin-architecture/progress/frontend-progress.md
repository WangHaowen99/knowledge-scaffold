# 前端进度

前端 worktree：`<worktrees>/onto-modeling-plugin-frontend`，分支 `feat/onto-modeling-plugin-frontend`。

## F1（2026-11-01）提交 a2aca4ee..a2153a82

| 块 | 状态 | 位置 |
|---|---|---|
| 设计系统与组件库 | 完成 | `src/styles/base.css`（设计稿原样，去掉原型控件）、`src/ui/` |
| 数据层 | 完成 | `src/api/client.js`（错误体、刷新、分页、409/423/428）、`src/api/sse.js`（三条流 + 日志流共用）、`src/api/endpoints.js`（F1 用到的分组）、`src/state/`（对话状态机、只读判定、流程视图） |
| mock | 完成 F1 范围 | `src/api/mock/`，`VITE_API_MOCK=1 npm run dev` 打开；状态切换见 `index.js` 顶部注释 |
| 外壳 | 完成 | `src/shell/`、`src/app/`：顶栏（全局 / 项目）、左栏（内核 4 项 + 插件页面，超过 5 个进「更多」）、子导航、⌘K、通知抽屉、维护横幅、角色与本地模式、项目流 |
| login | 完成 | `src/pages/login/` |
| projects | 完成 | `src/pages/projects/` |
| import | 完成 | `src/pages/import/` |
| create | 完成 | `src/pages/create/` |
| wb-l2 | 完成 | `src/pages/workbench/` |
| chat-states | 完成 | `src/pages/chat-states/`；第 1～20 项的组件都在 `src/pages/workbench/` |

测试：`npm test` 85 项（客户端、SSE、对话状态机、只读判定、Markdown 标记、页面纯函数，另有保留的求解契约与 MCP 配置测试）；`npm run build` 通过。

## F2（2026-11-01）提交 4c0369b1..8c936dca

| 块 | 状态 | 位置 |
|---|---|---|
| 示例场景清理 | 完成 | 设计稿示例全部换成合成场景「门店补货单编制」等，`git grep` 零命中；新旧对照只在 `/tmp`，见 decisions-log「F2 主控」 |
| F2 骨架 | 完成 | `src/api/endpoints.js`（ST、AR、RP、IN、AB、R、BP、V-02/V-04、F-02/03/04、EV-26）；`src/api/mock/routes-*.js`（每组一个文件）、`scenario.js`（合成场景的权威清单）；`src/app/App.jsx` 按路由懒加载；工作台扩展点 `extraCards.js`、`autobuild.jsx`、流程图弹窗宿主 |
| flowchart | 完成 | `src/pages/flowchart/`、`src/components/flowchart/`（React Flow 画布，图形按设计稿自绘）、`DrawioView.jsx`、`src/api/mock/routes-artifacts.js` |
| studio、studio-table | 完成 | `src/pages/studio/`、`src/components/studio/`（原版 WebGL 图谱改成受控组件，只画画布）、`src/api/mock/routes-studio.js` |
| reports | 完成 | `src/pages/reports/`、`src/api/mock/routes-reports.js` |
| inbox、l4-run | 完成 | `src/pages/inbox/`、`src/pages/workbench/autobuild.jsx`、`cards-autobuild.jsx`、`src/api/mock/routes-inbox.js`、`routes-autobuild.js`、`run3.js`、`inbox-data.js` |
| wb-materials、wb-blueprint | 完成 | `src/pages/workbench/cards-materials.jsx`、`materials/`（资料地图、核验、蓝图、确认后怎么建）、`src/api/mock/routes-materials.js` |

mock 状态（地址参数 `?mock=`，逗号分隔）：
- 工作台阶段：`stage:l2`（缺省，wb-l2）、`stage:materials`（读资料 5/7）、`stage:materials-empty`（还没传资料）、`stage:blueprint`（蓝图草案）、`stage:flowchart`（流程图弹窗背后的对话）、`stage:l4`（自主构建运行 #3 进行中），`stage:l4,run:ended`（运行结束）。
- 写入范围锁两态：`scope:running`、`scope:pending`。
- Studio：`submit:fail`、`submit:review`、`raw:big`、`raw:changed`、`draft:none`。
- 流程图：`fc:conflict`（保存 409）、`fc:compressed`（压缩格式，自动回退 drawio）。
- 另外两个项目有默认阶段：共享单车故障诊断 → `l4`，物业报修工单处理 → 还没传资料。

测试：`npm test` 158 项（F1 85 项 + F2 73 项：Studio、报告、流程图模型与 mock、待处理、自主构建、资料分析、蓝图）；`npm run build` 通过，每个提交单独导出后测试、构建也都通过。

视觉对照（1440×900，数据文字不同属预期）：flowchart 2.5%～3.6%；studio 去掉 WebGL 画布区后 6%～6.6%；studio-table 6.2%～8.6%（设计稿演示切换那一行去掉，下面上移 52px）；reports 2.1%～2.7%；l4-run 2.2%～6.5%；inbox 2.2%～8.3%；wb-materials 2.2%～7.3%；wb-blueprint 2.7%～3.7%，「确认后怎么建」卡 0%。wb-l2、projects 回归对照 0%。

## 已保留、等后续阶段接线的代码

| 代码 | 接到哪 | 阶段 |
|---|---|---|
| ~~`src/components/ExcelView.jsx`、`HtmlPreview.jsx`~~ | F3 没用：资料区按设计稿另写了预览（`ExcelView` 调的 `api.excelPreview` 本来就不存在） | F4 已删除 |
| `src/components/DrawioPreview.jsx` | 资料区 drawio 预览（压缩格式兜底） | F3 已接 |
| ~~`src/components/cm/MergeDiff.jsx`~~ | F3 没用：版本对比按设计稿 `.mr-cmp` 另写 | F4 已删除 |
| `src/lib/solver-contract-model.js` | 代码生成与求解页的求解契约表单（复用通用函数；两个旧场景参数不显示，待用户定） | F3 已接 |
| `src/lib/mcpClientConfig.js` | 项目设置「对外 MCP 与 Token」 | F3 已接 |

F2 已接线、删掉的旧代码：流程图多图侧栏（`FlowchartView`、`AddFlowchartCard`、`flowchart-editor.css`）、旧 Studio 接口转接（`studio-api.js`、`components/api.js`）、旧操作流程面板（改由页面用 ST-05 实现）。

## 下一步（F3 起，已完成，见下面 F3 一节）

1. 插件与项目页：知识、场景走查、代码生成、增量更新、版本、发布、资料与证据源、快照、schema、项目设置（含自主构建策略 AB-01/AB-02，设计稿 `L4_SET_*`）。
2. F2 留下的跳转目标都在 F3：versions（查看改动、刷新后对比）、resources / schema（查看原文）、proj-settings（调整自主构建策略）。
3. 新页面照 F2 的写法：路由在 `App.jsx` 懒加载登记；接口补进 `endpoints.js`；mock 每组一个 `routes-*.js`，名称数据取 `scenario.js`；示例只用合成场景，交付前跑安全 grep。
4. 视觉对照脚本：`/tmp/ks-f2/shotx.mjs`（设计稿）、`shotapp.mjs`（本地 mock）、`diff.mjs`（像素差）。

## 后端 B3a 带来的前端改动（2026-11-01 记）

- **前端阶段定义改读 `/flow`**（F3 已完成，提交 1d7c5bea）：主流程只在 core 包的 `flow.yaml` 定义一次（ADR 0025）。前端的 `LeftPane` 的 `STAGES`、`stageHits`、`stageStatuses`、`stageForCommand` 与 `ChatPane.buildSuggestions` 不再自带阶段表，改读 `GET /api/v1/projects/{id}/flow`：
  - 进度条与建模步骤：`nodes[]`（标题、`optional`、`state`、`actions`、`panel`），分组用 `sections`，进度数用 `progress`；
  - 运行中的命令属于哪个节点：`commands`（命令名 → 节点 ID，命令名不带斜杠与 `ontology-` 前缀）；
  - 输入框上方建议条：`suggestions`（P1 只给主线下一步，打断、过期等优先级 P2 补）。
  - 「质量检查完成」口径变了（有校验报告且没有 `[ ]`），前端不要再按 `reports/` 有没有文件判断。
  - 字段细节见 `contract-changes.md`「B3a」第 1、2 条。
- **插件页**（plugins、plugin-detail、plugin-upload、proj-settings 的能力与插件）：PL-01～PL-03、PL-05～PL-11、PL-13～PL-18 已可联调；PL-04、PL-12、PL-19 未做（见 `gaps.md` B3a 第 1 条）。时间字段都是 RFC 3339。
- **chat-states 第 14 项**（能力包升级提示）：对话流已发 `session.config_pending`、`session.reconnected`（契约 EC-06、EC-07，字段见 contract-changes B3a 第 15 条）；项目流的 `assembly.updated` 要等 P-15。
- **ops-ctl 的 core 默认版本**：O-30～O-34 已改由包仓库承接，版本号是真实的 core 版本（如 `0.1.0+3f9a1c2d`），不再是 `current` / `dotclaude-*`。

## F3（2026-11-01）提交 1d7c5bea..77f819e1

主控先把前端阶段定义改读 `/flow`、搭骨架，再拆 7 个并行子任务（A 知识沉淀与场景走查、B 代码生成、C 增量更新、D 版本与发布、E 资料区、F 数据源 / 快照 / 证据浏览器、G 项目设置），由主控核对后按页提交。

| 块 | 状态 | 位置 |
|---|---|---|
| 阶段定义改读 `/flow`（B3a 留的那条） | 完成 | `src/state/flow.js`（`normalizeFlow` 收 P1 / P2 两种形状、`sectionGroups`、`nodeActionKind`、`commandNode`、`overlayRunning`）；工作台进度条与建模步骤抽屉、自主构建运行详情不再自带阶段表 |
| F3 骨架与共用件 | 完成 | `src/api/endpoints.js`（K、V、CS-01、S-19、INC、PL-02～11、SC、SV、EV、PB、M、P-12、P-14、F-01/05/06、R-04/05/11/12）；`src/pages/plugin/parts.jsx`（插件标识、发命令按钮、进度条、写入范围只读条、插件没启用的空状态）、`src/state/commandTask.js`；`src/styles/more.css`、`evidence.css`（设计稿样式原样移植）；mock 分组与同一路径多处理者 |
| knowledge | 完成 | `src/pages/knowledge/`、`routes-knowledge.js`、`knowledge-data.js`；对话里「沉淀为业务知识」可选作用范围（`ScopePicker`） |
| scenarios | 完成 | `src/pages/scenarios/`、`routes-scenarios.js`、`scenarios-data.js` |
| scripts | 完成 | `src/pages/scripts/`、`routes-scripts.js`、`scripts-data.js` |
| incremental | 完成 | `src/pages/workbench/cards-incremental.jsx`、`incremental/`、`routes-incremental.js`、`incremental-data.js`（工作台对话里走完） |
| versions | 完成 | `src/pages/versions/`、`routes-versions.js`、`versions-data.js`；接住「查看改动」`?v=&run=&slice=`、「刷新后对比」`?path=&from=&to=WORKTREE` |
| publish | 完成 | `src/pages/publish/`、`routes-publish.js`、`publish-data.js` |
| resources | 完成 | `src/pages/resources/`、`routes-resources.js`、`resources-data.js`（R-01/02/04/05 从 `routes-materials.js` 搬来，资料状态经 `ctx.materials` 共用）；接住「查看原文」`?anchor=`、「上传文本版」`?path=`、外部反馈 `?group=feedback&feedback=` |
| sources、snapshots、schema | 完成 | `src/pages/evidence/`、`routes-evidence.js`、`evidence-data.js`；schema 接住数据库锚点的「查看原文」`?anchor=db:…` |
| proj-settings | 完成 | `src/pages/settings/`、`routes-settings.js`、`settings-data.js`；「开始自主构建」入口（AB-02 → AB-03 预览 → AB-04 → l4-run），⌘K 的开始入口改走这里；分区参数 `?section=` |

mock 状态（地址参数 `?mock=`，逗号分隔）：
- 流程：`flow:p1`（`/flow` 按 P1 后端的形状返回，核对归一处理）。
- 页面命令进行中：`task:knowledge`、`task:scenarios`、`task:scripts`、`task:resources`、`task:snapshots`（该页一打开就有一个进行中的任务，静止不动）。
- 插件停用：`off:<包名>[+<包名>]`，如 `off:reverse`、`off:trace+knowledge`（左栏、资料子页跟着消失，直接打开地址看到「本项目没有启用」）。
- 知识沉淀：`knowledge:empty`。场景走查：`trace:batch`（与 `scope:pending` 一起用，批量推演跑完待确认）。
- 代码生成：`sandbox:off`、`scripts:empty`、`sv:conflict`；页签 `?tab=sv` 直接打开求解模型。
- 增量更新：`stage:incremental`（等确认清单），加 `inc:executed`（执行后待确认）、`inc:merged`（已确认生效）；其他阶段直接打开 incremental 路由也能到这条对话。
- 发布：`publish:degrade`、`publish:ready`、`publish:empty`。
- 数据源：`role:user`（普通用户视角，只能提交申请）。项目设置：`assembly:failed`、`engine:missing`。
- 写入范围锁两态 `scope:running`、`scope:pending` 在各插件页都生效（场景、求解契约、知识条目经各分组的 `lockScope` 并进锁）。

测试：`npm test` 299 项（F2 158 项 + F3 141 项：流程归一、页面命令任务、知识、场景、代码生成、增量更新、版本、发布、资料区、证据源、项目设置）；`npm run build` 通过，每个提交单独导出后测试、构建也都通过。主入口包 695 KB（gzip 221 KB），F3 页面都按路由拆包。

视觉对照（1440×900，数据文字不同属预期；「补位」指设计稿那一行原型演示切换在产品里不留，对照时补回高度再比）：
- knowledge：默认 14.09%（设计稿页头里的演示切换把说明挤成两行，产品一行，下面上移 19px），对齐后 6.74%；还没有条目 1.17%（对齐后）；新增 / 删除 / 提名 / 冲突明细弹窗 0.96% / 1.59% / 4.39% / 8.99%。
- scenarios：助手运行中 2.29%；修改待确认 8.05%；空闲 3.48%（对齐后，空闲时没有进度条那一行）；示例数据页签 2.23%。
- scripts：脚本页签 2.89%（补位后）；求解模型 1.39%（补位后），滚到下面 9.47%（附属文件名长，折两行）；助手运行中 2.02%；修改待确认 1.40%；转为手动编辑弹窗 1.43%。
- incremental：停在清单卡 4.29%；顶部 2.88%；往下 5.86% / 5.19%；执行后 4.95%；确认生效后 5.32%；清单弹窗头部 8.09%、底部 3.38%；建模步骤抽屉 4.27%（清单 5 条，设计稿 4 条）。
- versions：默认 8.39%（左栏 5.80%；示例条目的 Markdown 更长，对比卡变高）；标记 / 回退 / 删除标记弹窗本身 0.45% / 5.79% / 5.20%。
- publish：默认 2.89%；滚动后 3.14%；降级发布弹窗本身 0.37%。
- resources：默认 4.21%；表格 / CSV / 流程图 / HTML 预览 2.26%～4.77%；被引用 1.54%；删除弹窗 5.16%；往下 6.41% / 6.32%（去掉了设计稿预览卡里那排原型类型切换，下面上移 36px）。
- sources：默认 5.28%（登记 2 个数据源，设计稿 4 个）；登记抽屉 7.1%～8.6%；申请抽屉 6.35%。snapshots：首屏 3.96%、往下 5.23% / 9.62%；生产采集确认弹窗 1.99%。schema：默认 5.21%、带锚点进入 5.34%。
- proj-settings：首屏 0.77%，往下 0.50% / 2.70% / 2.50%（对齐后）/ 6.59%；选 L4 后的策略 0.47%；新建 Token 弹窗 2.09%；删除弹窗 3.83%。
- 回归：wb-l2（P2 形状的流程）0%；`flow:p1` 下进度条显示 6 个必经步骤、抽屉按 sections 分组并收起多余命令。

## 下一步（F4 起）

1. 运维与管理：运维中心、可观测、管控、用户、插件目录 / 详情 / 上传、个人设置、系统设置。插件详情的跳转参数是 `plugin-detail?pack=<包名>`（F3 的插件标识、项目设置的能力表都这样跳）；个人设置 `me` 由项目设置「改我的模型配置」跳过去。
2. 新页面照 F2、F3 的写法：路由在 `App.jsx` 懒加载登记；接口补进 `endpoints.js`；mock 每组一个 `routes-*.js`，同一路径可以有几个处理者（返回 `undefined` 交给下一个）；示例只用合成场景，交付前跑安全 grep。
3. 视觉对照脚本：`/tmp/ks-f3/shotx.mjs`（设计稿，自动隐藏原型导航条）、`shotapp.mjs`（本地 mock）、`diff.mjs`（像素差）。

## F4（2026-11-02）提交 29890e75..9f8ba691

主控先搭骨架（29890e75 运维面与管理接口分组、角色判定、全局页路由拆包、二级导航与共用件、mock 分组；11bb5b6e 平台视角的账号与项目元数据），再拆 7 个并行子任务（A 运维总览与可观测、B 运维管控与健康、C 用户审计与回收站、D 插件中心与详情、E 上传与审核、F 个人设置、G 系统设置）。D 被网络中断打断，由接手的主控补完；收尾按页提交。

| 块 | 状态 | 提交 | 位置 |
|---|---|---|---|
| F4 骨架 | 完成 | 29890e75、11bb5b6e | `src/api/endpoints.js`（O-01～O-54 走 `ops_base_url`、AD、SY、X-06、PL-13～PL-19、A-06/08/10/11）、`src/state/roles.js`、`src/shell/SideNav.jsx`、`src/pages/ops/parts.jsx`（`OpsShell`）、`src/pages/plugins/parts.jsx`（`PluginsNav`、`usePluginProject`）、`src/api/mock/platform-data.js` |
| ops、ops-obs | 完成 | 28dc5d9d | `src/pages/ops/OpsOverviewPage.jsx`、`OpsObsPage.jsx`、`overviewModel.js`、`obsModel.js`、`charts.jsx`、`obs*.jsx`；`routes-ops-obs.js`、`ops-obs-data.js` |
| ops-ctl | 完成 | ff11c4b0 | `src/pages/ops/OpsCtlPage.jsx`、`ctl*.{js,jsx}`；`routes-ops-ctl.js`、`ops-ctl-data.js`；mock 只读维护放行运维面写请求（`index.js`） |
| ops-users | 完成 | 6790c9d5 | `src/pages/ops/OpsUsersPage.jsx`、`users*.{js,jsx}`；`routes-admin.js`、`admin-data.js` |
| plugin-upload | 完成 | 02a29d6d | `src/pages/plugins/PluginUploadPage.jsx`、`upload*.{js,jsx}`；`routes-pack-uploads.js`（另导出 `addAdminRequest`）、`pack-uploads-data.js` |
| plugins、plugin-detail | 完成 | 5f697ff0 | `src/pages/plugins/PluginsPage.jsx`、`PluginDetailPage.jsx`、`catalog*.{js,jsx}`、`detail*.{js,jsx}`；`routes-plugins.js`、`plugins-data.js`；`routes-settings.js` 加两处挂钩，插件中心与项目设置能力表同一状态 |
| me | 完成 | ce6cec71 | `src/pages/me/`、`routes-me.js` |
| sys | 完成 | 23de2829 | `src/pages/sys/`、`routes-system.js`、`system-data.js` |
| 收尾 | 完成 | 6b89d5c6、e815a277、9f8ba691 | 个人设置两处入口带分区；删除旧组件；主入口拆包（ops-ctl 测试的包名断言改为对照合成目录，已并进 ff11c4b0，历史里不留设计稿的包名关键词） |

mock 状态（地址参数 `?mock=`，逗号分隔；同一组的多个标记用 `+` 连写）：
- 角色与模式：`role:ops`、`role:user`、`role:admin`（缺省 zhangsan 是管理员）、`auth:local`、`maint:readonly`、`maint:reject`。
- 运维总览、可观测：`ops:p1`（O-01 按 B2 口径）、`obs:p1`（按 B3b 口径，O-08～O-12 返回 404）；`ops-obs?trace=<id>`、`?section=rules`。
- 运维管控：`ctl:ready`、`ctl:notready`、`ctl:nosess`；`ops-ctl?section=startup|health|sessions|maintenance|packs|core|backups|tasks|versions|retention|diagnostics`。
- 用户、审计与回收站：`users:unavailable`、`trash:empty`、`trash:legacy`；`ops-users?tab=users|audit|errors|trash&error=<编号>`。
- 插件中心：`plugins:norec`（推荐为空）、`plugins:held`（core 1.5 还在 beta，场景走查 0.10.0 更新被挡，对应设计稿那一刻）、`plugins:ind`（零售连锁 1.1.0 可更新）；`plugins?cat=<分类>|inst|upd`，`plugin-detail?pack=<包名>[&project=<项目>]`。
- 上传与审核：`upload:checking`、`upload:empty`、`upload:exists`；文件名以 `demo-` 开头按租户包判 4 项失败，含 `nomanifest` 返回 400；`plugin-upload?upload=<id>`。
- 个人设置：`me:nokey`、`me:personal`、`me:noprefs`、`me:rate`、`me:noadmin`；当前密码填 `wrong` 返回「旧密码不正确」；`me?section=acct|look|noti|model`。
- 系统设置：`sys:p1`（按 B2/B3b 现状，SY-15～SY-18、X-06 返回 404）、`sys:ctxbad`、`sys:ctxoff`、`sys:boxdown`，另认 `engine:missing`、`sandbox:off`；`sys?section=`。

测试：`npm test` 473 项（F3 299 项 + 骨架 6 项 + F4 页面 168 项：可观测 27、管控 30、用户 19、插件中心 19、上传 20、个人 16、系统 37）；`npm run build` 通过。每个提交单独导出后测试、构建都通过。

包体积：主入口包 F3 结束 695 KB → 加完 F4 页面 705 KB（gzip 223 KB）→ 拆包后 **321 KB（gzip 103 KB）**。
- 拆出去的：登录、我的项目、新建、导入、工作台（含对话卡片）、状态说明页，改为按路由懒加载。Markdown 渲染进了共享块（167 KB，gzip 51 KB，工作台、插件页、可观测的处置手册用），工作台本身 100 KB。
- 原来随工作台进主入口的 6 份样式（autobuild、more、incremental、materials、knowledge、workbench-extra）改在 `App.jsx` 里按原顺序引入，`index.css` 与拆包前逐字节相同；14 页拆包前后截图 0%～0.14%（我的项目的 0.14% 是自主构建卡的流光动画）。
- 主入口里剩下的：React DOM（约占一半）、外壳、数据层、组件库、⌘K，没有再拆的空间。
- F4 各页：系统设置 57 KB、运维管控 55 KB、用户 27 KB、上传 23 KB、个人 23 KB、可观测 20 KB、插件详情 17 KB、运维总览 8 KB、插件中心 3 KB（卡片与弹窗在和详情共用的块里）。

已删除的旧组件：`src/components/ExcelView.jsx`、`src/components/HtmlPreview.jsx`、`src/components/cm/MergeDiff.jsx`（grep 无引用）。`src/components/api.js` F2 已删。删完后 `package.json` 有 9 个依赖没有代码引用了，见 gaps F4。

视觉对照（1440×900，数据文字不同属预期；收尾时在合并后的代码上重拍了 9 页默认态）：
- ops：默认 0.86%（运维视角），滚到底 3.12%，管理员视角 0.97%。
- ops-obs：首屏 1.60%，往下 1.84% / 2.41%；导出器弹窗 2.01%；管理员视角 1.81%～2.83%。公共标签过滤默认是空的（设计稿预填了 7 个演示标签）。
- ops-ctl：首屏 4.77%（会话池头去掉演示切换，下面上移 9px；保存没改动时置灰）；往下对齐后 6.29% / 6.39% / 7.61% / 4.10%（版本号、人名；启动自检、版本清单各多 1 行）；弹窗 0.26%～2.71%，进入维护弹窗 4.82%（加了「升级目标」）。
- ops-users：默认 4.48%（23 个账号对设计稿 6 行，统计卡挤到首屏以下；页头 1.31%）；不可用 0.57%；重置密码 3.90%；删除用户弹窗 3.24%～4.89%；审计 4.58%；错误留档 3.67%；回收站 1.56%。
- plugins：整屏 7.02%，顶部 390px 0.82%：卡片说明用新建向导同一份包目录，比设计稿短，卡片矮 18px，顺序也按包目录；启用弹窗区域 0.83%；更新弹窗区域 2.72%；往下 9.28% / 10.67%（提供方与证据源页同一批：Oracle、达梦代替设计稿的界面录制、CLI-Anything）；没有匹配 4.56%。
- plugin-detail：默认 0.05%；「已启用到本项目」菜单 0.03%；暂时不能更新 0.03%；包含什么 0.02%；更新记录 2.01%。
- plugin-upload：默认 2.83%；往下 7.15% / 6.61%（后端固定 14 项检查，设计稿画了 11 行）；失败包 4.56%。
- me：账号（三格按设计稿填）0.81%；外观 0.00%；通知 0.13%；我的模型配置（存过个人 Key）3.65%；管理员关闭个人 Key 1.89%。
- sys：模型接入 0.77%；推荐表 3.03%、编辑卡 2.46%、有误 3.14%；会话与权限 0.15%（保存后）；沙箱 1.01%；MCP 2.01%；配置注册表 2.06%；配额 2.72% / 4.26%。

### 设计稿没画入口的 5 个组件（写好、不挂载，等用户决定）

组件、mock、单测都在；页面里没有挂载点、注释或开关。挂载只改下面几行：

| 能力 | 组件 | mock / 测试 | 挂载时改哪里 |
|---|---|---|---|
| 链路检索 O-05 | `src/pages/ops/TraceSearch.jsx` | `routes-ops-obs.js` 的 `GET /ops/traces`；`tests/opsObs.test.js` | `OpsObsPage.jsx`：加 `import TraceSearch from './TraceSearch.jsx'`；在 `<TraceCard …/>` 上一行加 `<TraceSearch range={range} labels={labels} onOpen={openTrace} />` |
| 无主项目与指定所有者 AD-06/07 | `src/pages/ops/UnownedProjects.jsx` | `routes-admin.js`（AD-06 两个合成项目、AD-07 校验并写审计）；`tests/opsUsers.test.js` | `usersAccounts.jsx` 的 `AccountsPane`：import `UnownedProjects`、`Seg`；加 `const [view, setView] = useState('all')`；角色说明行 `<span className="sp" />` 后放 `<Seg items={[['all','全部账号'],['unowned','无主项目']]} value={view} onChange={setView} />`（可只对 admin 显示）；`view === 'unowned'` 时渲染 `<UnownedProjects users={items} />` 代替账号表与统计卡 |
| 管理员审核申请 AD-08～10 | `src/pages/plugins/AdminRequests.jsx` | `routes-pack-uploads.js`（另导出 `addAdminRequest`，PL-04 已用它推申请）；`tests/pluginUpload.test.js` | `PluginUploadPage.jsx` 的 `UploadReview`：import `AdminRequests`、`Tabs`；加 `const [tab, setTab] = useState(route.query?.tab === 'req' ? 'req' : 'up')`；`PageHead` 下放 `<Tabs items={[['up','上传',null,'upload'],['req','申请',null,'inbox']]} value={tab} onChange={setTab} />`；`tab === 'req'` 时把 `.plu-grid` 换成 `<AdminRequests />`；通知 `request_pending` 跳 `plugin-upload?tab=req` |
| 模型单价 SY-18 | `src/pages/sys/PricingCard.jsx` | `routes-system.js`；`tests/sys.test.js` | `ModelSection.jsx`：加 import；在末尾「默认引擎 / 个人 API Key」那个 `grid2` 之后加 `<PricingCard className="mt16" />` |
| 服务令牌 X-06 | `src/pages/sys/ServiceTokensSection.jsx` | `routes-system.js`（明文是 `onto_svc_mock-placeholder-0001` 这类占位）；`tests/sys.test.js` | `SysPage.jsx`：import 并在 `PANES` 加 `contract: ServiceTokensSection`；`sysModel.js` 的 `SYS_SECTIONS` 末尾加 `{ key: 'contract', title: '对外契约', icon: 'key' }` |

各组件文件头也写了同样的挂载方法。子任务都临时挂上截图核对过，已还原。

## 下一步（I1 起）

1. 与真实后端联调：F1～F4 的接口都只对 mock 跑通。联调要对齐的契约补充见 `contract-changes.md` F1～F4 各节，后端没做的接口见 `gaps.md`。
2. 5 个没挂载的组件等用户决定，挂载方法见上表。
3. 外壳收到用户流通知时读 A-08 偏好、弹系统通知与闪标题（`meModel.js` 已有判断函数），深色主题样式与启动时切主题（gaps F4-F）。
4. `package.json` 里没人用的依赖清理（要重新生成 `package-lock.json`）。

## 联调前要对齐的后端改动（B4a、B4c，2026-11-02 记）

I1 时处理，F4 之前的前端代码还没改：

- 场景走查两个按钮的命令名：`/ontology-trace-refresh` → `/ontology-trace-batch {keys}`，`/ontology-trace-walk` → `/ontology-trace-interactive {scenario}`（`contract-changes.md` B4c 节的映射表）。
- 节点编号带包前缀（如 `trace.trace`、`codegen.codegen`），FL-01 的契约变更（B4c 第 10 条）。
- 场景产物仍在 `ontology/testcase/<逻辑>.md`，不是 F3 第 8 条写的 `ext/trace/scenarios/`（B4c 第 1 条）。
- 对话流事件 id 从 1 开始（B4a 第 1 条），前端状态机 `lastSeq` 从 0 起算正好对上；`session.queued` 改为持久事件（B4a 第 20 条）。
