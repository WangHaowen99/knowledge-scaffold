# Onto Modeling — 本体建模工具 Web 版

基于 Claude Agent SDK 的本体建模 Web 工作台。设计方案见
`/mnt/jianguoyun/meta/design/本体建模工具Web版-方案设计-20260918.md`。

## 结构

```
onto-modeling/
  backend/    FastAPI 服务：项目/工作区管理、Agent 会话编排（会话池 + SSE）、文件与版本 API
  frontend/   React 19 + Vite 7 + antd + @ant-design/x 三栏工作台
  assets/     官方包：workspace-toolkit 即 core 包（manifest.yaml、flow.yaml），packs/ 下是可选能力包与行业包，
              启动时导入包仓库；packs/<包>/ui/ 是包自带的编辑器，前端构建时一起编译
  deploy/     离线交付包构建、预检与部署脚本
  docs/       架构决策记录（decisions/）、API v1 契约（api-v1.md）、插件化改造的设计与交接文档（plugin-architecture/）
```

插件化改造的现状、分支与接手方式见 [docs/plugin-architecture/handoff.md](docs/plugin-architecture/handoff.md)。

## 启动（开发）

后端（默认 127.0.0.1:8100）：

```bash
cd backend
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
./venv/bin/python run.py
```

前端（默认 127.0.0.1:5180，/api 代理到后端）：

```bash
cd frontend
npm install
npm run dev
```

不连后端时可以用前端自带的 mock 服务端（按契约路径响应，状态在内存里）：

```bash
cd frontend
VITE_API_MOCK=1 npm run dev     # 或把 VITE_API_MOCK=1 写进 frontend/.env.local
```

- 只在开发、测试构建里生效；`npm run build`（生产模式）恒为关，`src/api/mock/` 不进产物（ADR-0069）。
- 演示状态用查询串 `mock` 切换（写在 `#` 之前），如 `http://127.0.0.1:5180/?mock=projects:empty#/projects`、
  `?mock=stage:materials,evidence:off`；可用的键见 `src/api/mock/index.js` 文件头。也可以存在浏览器 `localStorage`
  的 `onto.mock` 里。
- 「对话卡片与状态」说明页（项目内页面 `chat-states`）只在开发与 mock 构建里有。

前端的路由、事件流读法与 mock 的打包约定见 ADR-0069。

本机起带登录的完整服务（不用容器；三步各开一个终端，都从本目录出发）：

```bash
# 1. auth-server（仓库根 auth-server/，127.0.0.1:5056）
cd ../../auth-server && python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
AUTH_SERVER_DATA_DIR=/tmp/onto-auth AUTH_SERVER_ISSUER=http://127.0.0.1:5056 \
  AUTH_BOOTSTRAP_ADMIN_PASSWORD='<初始密码>' AUTH_SERVER_ADMIN_TOKEN='<管理令牌>' ./venv/bin/python run.py
# 2. 前端构建产物由后端直接发（frontend/dist）：构建时给 js、css 各写一份 .br / .gz，后端按 Accept-Encoding 发；
#    assets/ 下带哈希的文件缓存一年，index.html 每次向服务端确认。前面再加反向代理时不要重复压缩、别改这两种缓存头
cd frontend && npm ci && npm run build
# 3. 后端：登录、用户管理、运维中心经业务端口
cd backend && ONTO_WEB_AUTH_URL=http://127.0.0.1:5056 ONTO_WEB_AUTH_ISSUER=http://127.0.0.1:5056 \
  ONTO_WEB_AUTH_ADMIN_TOKEN='<管理令牌>' ONTO_WEB_OPS_ON_MAIN=true ONTO_WEB_RELOAD=0 ./venv/bin/python run.py
```

- 打开 `http://127.0.0.1:8100`，用 `admin` 和初始密码登录；模型网关在「系统设置 → 模型接入」或 `.env` 里配。
- 脚本验证与脚本节点要沙箱网关（`ONTO_WEB_SANDBOX_GATEWAY_URL`），本机没有时这两项报沙箱不可用，其余不受影响；
  要全套（含网关）用 `docker compose up --build`。
- PDF、Word、Excel 转文本要连接器网关（`ONTO_WEB_CONNECTOR_GATEWAY_URL`，ADR-0073），本机没有时这些资料停在「等待转换」；
  本机单独起网关：`cd backend && pip install -r connector-requirements.txt && ONTO_CONNECTOR_CONVERTERS_DIR=../assets/packs
  ONTO_CONNECTOR_ISSUER_KEY_URL=http://127.0.0.1:8100/api/v1/connector/issuer-key python -m app.connector_gateway`，
  再给后端设 `ONTO_WEB_CONNECTOR_GATEWAY_URL=http://127.0.0.1:8120`。

模型接入通过环境变量注入（见 `.env.example`），不落盘、不进仓库。

模型上下文约束按 配置中心「模型接入 → 上下文与自动压缩」> `.env` 中的 `ONTO_WEB_CONTEXT_*`
（见 `.env.example`，变量名与会话引擎无关）> 内置默认 逐项生效。配置页只填模型真实窗口和最大输出，
内置默认分别为 200000 和 32000；压缩窗口与触发百分比只能在 `.env` 配置，两者都没配时服务端按生效的
窗口与最大输出自动推算，使压缩前的请求仍给模型窗口留至少 10% 余量（压缩窗口向下取 10000 的倍数，
低于 CLI 下限 100000 时改推触发百分比，向下取 5 的倍数），配了任一项就完全按 `.env`。稳定引擎把
生效值转成 CLI 的环境变量注入会话（Claude 官方模型名不读窗口变量，仍按 CLI 内置窗口）。接入窗口
小于生效窗口的模型时必须配置真实窗口，否则自动压缩来不及触发，请求会因超出模型窗口被网关拒绝；
窗口更大的模型可配置以用满窗口。配置页按输入框里的值实时提示自动压缩的触发点。`.env` 改动需重建
容器（`docker compose up -d`）才生效，非法值会被忽略并在服务日志中告警。

内测引擎（Jiuwen）的窗口与最大输出在配置页另存一组，留空时同样回落 `ONTO_WEB_CONTEXT_*`，都没配时
最大输出按 `ONTO_WEB_JIUWEN_MAX_TOKENS`（默认 32000，与稳定引擎一致；输出上限更小的模型须填写，
否则请求会被网关拒绝）。引擎内置的压缩器在上下文达到「窗口 × 触发比例」
时压缩，比例按同样的 10% 余量推算，限定在 0.3–0.8（向下取到 0.01）；`ONTO_WEB_CONTEXT_COMPACT_*`
只作用于稳定引擎。两个引擎在会话里显示同样的上下文用量与「上下文已自动压缩」提示。内测引擎会话
重建（服务重启、改配置、切换会话）时优先从框架存档续接，存档里就是压缩后的上下文；存档不可用时
退回历史回放，从会话历史里最近一次压缩摘要接续，不再只靠截断的原始历史。

配置页填写模型名后会查模型上下文推荐表：命中时填入窗口和最大输出，可手改，与网关配置一起保存；
没命中时清空这两项（原值属于之前的模型）；改回已保存的模型时恢复已保存的值。推荐表只做预填，
不参与上面的生效顺序，打开配置页也不会自动改已保存的值。匹配规则：不区分大小写，忽略 `provider/`
前缀、`.` 与 `_` 写法差异和网关追加的 6 或 8 位日期后缀，其余须与 id 或表中别名完全一致。

推荐表分两层：内置表（`backend/app/services/model_context_presets.json`，随版本发布，数值取自厂商
官方文档，最大输出取官方默认值，思考与非思考默认值不同时取思考模式的）和数据目录下的自定义表
`model-context-presets.json`（同 id 整条替换内置条目，新 id 追加）。管理员可在配置页「查看或编辑
推荐表」中覆盖内置条目、恢复内置、新增或删除模型；运维也可参照数据目录下的
`model-context-presets.example.json`（每次启动按内置表重写，本身不生效）直接编辑自定义表。
自定义表改动在下次查表时生效，无需重启；文件有误时整份不生效、沿用上一次合法的结果，页面显示错误且
不能编辑。页面保存带上读取时的版本号，文件已被改过时拒绝保存，每次保存前把上一版存为同目录的
`model-context-presets.json.prev`。「停用自定义表」把文件改名为 `model-context-presets.json.bak-日期-时间`
备份，改回原名即重新启用。
聊天面板顶部的上下文环显示当前会话最近一次模型调用的已用量、
模型窗口、剩余量和距自动压缩的余量（新会话发出第一条消息前显示空环）；发生压缩时对话里插入「上下文已自动压缩」提示。
环旁边的 token 数是项目累计用量，含全部会话和已删除会话。内测引擎每次模型调用返回后就计入，
长命令运行中也会增长，被打断或服务重启时只丢正在进行的那次调用；稳定引擎每轮结束时计入。

启用登录鉴权后，用户可在「设置 → 我的模型配置」填写自己的 API Key（可选再填网关地址、
模型，留空沿用管理员配置）。个人 Key 只用于本人发送的消息：同一项目换人发消息（如管理员换了
所有者）且配置来源不同时，会话断开后用 resume 续接；本轮未结束时拒绝换人（409）。没有个人 Key 的用户、
未启用鉴权的部署，以及管理员关闭个人 Key 后，行为都与管理员统一配置时一致。个人配置不含
上下文参数，两个引擎都沿用管理员配置页的窗口与最大输出；个人模型的窗口与之不同时，以管理员配置为准。

## 会话池（方案 4.1.1）

每个活跃建模会话 = 一个 CLI 子进程（均值约 400MB）。会话池按
`ONTO_WEB_MAX_SESSIONS`（默认 12，对应 8G 内存）限制并发：超限先驱逐最久
空闲的会话，全部在跑则排队并在前端显示位次；空闲超过
`ONTO_WEB_SESSION_IDLE_TTL`（默认 600 秒）的会话被回收，下次发消息用
resume 续接模型上下文，界面无感。

每个项目可有多条会话（聊天面板顶部下拉：新建、切换续聊、重命名、删除），各自保存历史与模型
上下文，但同一时刻只有一个在线引擎。往另一条会话发消息时，原会话空闲就换绑，忙则返回 409。
存储与取舍见 `docs/decisions/0013-multi-conversation-sessions.md`。

管理员改了模型配置后，空闲会话在下一条消息时断开重连（resume 续接）使新配置生效；正在跑的
一轮不会被切断：这期间发来的消息仍交给原引擎，等引擎里的轮次都结束后再断开，之后的消息按新
配置重连。

服务停机时先给没跑完的轮在会话历史里写中断标记，再并发断开全部引擎，总时长控制在 25 秒内
（uvicorn 等连接关闭最多 3 秒，会话回收最多 20 秒；docker 停机宽限期 30 秒）。

引擎每次连接时在工作区写 `.onto/run/engine.json`（实例标识与后端进程号）。工具箱
`state_helper.py` 据此把命令执行锁绑定到引擎实例：同一实例的多次调用可续写锁，实例换了
（回收、重启、改配置、切引擎）旧锁即判过期，与后端是不是容器里的 1 号进程无关。容器以
`init: true` 运行，镜像也以 tini 作入口兜底，后端不再是 1 号进程。

后端直接运行工作区里的工具箱脚本（反查 `query.py`、示例注入 `seed_example.py`、Jiuwen
引擎的守卫钩子）时只给最小环境（`PATH`、`HOME`、语言区域与 Python 编码相关变量），不带
模型 Key、JWT 与各类 `ONTO_WEB_*` 凭据。

阶段提示、统一下一步与包的提示规则由后端在两轮之间注入，两个引擎同一份：每条消息送到模型前，把当前
阶段、进行中的命令、最近报告的待处理数、与界面建议条同一份的下一步建议，以及用户原话命中的包提示规则附上，
只发给模型，会话历史里仍是用户原话。业务知识信号（纠错、惯例）就是知识沉淀包的一条提示规则，没启用这个包的
项目不提示。稳定引擎经 SDK 的 UserPromptSubmit 回调，内测引擎拼在发给模型的消息前面；原来的两个提示钩子
（`session_start_hook.sh`、`knowledge_signal_probe.sh`）已退役。阶段与下一步在会话开始时必带，之后内容变了
才再带。见 `docs/decisions/0034-session-host-and-injection.md`、`0045-pack-runtime-host-wiring.md`。

每条命令（`/ontology-*`）有一份执行记录（数据目录 `exec/<项目>/`）：一轮结束命令还没做完记为等待，
打断、引擎异常结束、会话回收、停机或重启时记为在某阶段中断，对话里出现续跑卡（从打断处继续、或放弃）；
输入的资料或上游产物被改过、这一步的方法升级了时不能续跑。插件页、资料页发的命令返回任务对象，页面
显示进度条（ADR-0035）。

内测引擎的会话默认放在独立的 worker 子进程里（`ONTO_WEB_JIUWEN_WORKER`，默认 `true`）：它出错不拖垮
后端，同时在线的内测引擎会话一起结束、下一条消息重拉；设 `false` 退回在后端进程内运行（ADR-0038）。
一轮里超过 `ONTO_WEB_JIUWEN_STALL_SECONDS`（默认 900 秒，0 关闭）没有任何动静（流事件、模型调用、子任务进展；
等确认卡不算）时按出错结束这一轮，执行记为中断，建议条主推「从打断处继续」。

agent 自己的 `git commit` 经内核发布的 git 钩子（数据目录 `githooks/`，各项目 `core.hooksPath` 指向它）：
提交那一刻检查真实暂存区（`git add -A && git commit` 写在一行也查得到），提交说明带 `Onto-Source: agent`、
`Onto-Author`、`Onto-Packs`、`Onto-Node`；`--no-verify` 与改钩子路径会被拦下（ADR-0037）。

## 接口版本与旧路径兼容

后端接口统一在 `/api/v1` 下（契约见 `docs/api-v1.md`），错误体统一为
`{code, message, details, trace_id}`，`code` 取契约第 2 章的错误码，`trace_id` 也在响应头
`X-Trace-Id` 里。旧路径 `/api/...` 在兼容期（到 2027-05-01）经兼容中间件改写到对应的 v1 接口：
请求和响应都是 v1 的形状，响应多带 `Deprecation`、`Sunset`、`Link` 头，错误体额外带 `error`
（值同 `message`）；已删除的旧接口（评测、Studio 租约、成员、发布用的 `GET .../export?scope=package`
等）返回 `410 endpoint_removed`，`details.successor` 给出替代接口。`/api/health` 与 MCP 旧地址
永久保留。见 `docs/decisions/0021-api-v1-and-legacy-compat.md`。

## 启动自检

服务启动时、任何会话开始之前按启动恢复注册表跑一遍各模块的恢复步骤（ADR-0036）：清掉各项目 `.git`
下残留的锁，补提交上次停机时写了一半的写入（写入事务留下的意图，提交说明以 `recover:` 开头），列出各项目
剩下没提交的改动（保留原样，等所有者处理），存量数据迁移，核对装配目录，装 agent 提交钩子，把停在运行中的
对话历史补上中断标记、没回复的确认卡补失效，把没结束的执行改判为中断并放续跑卡；外部依赖探测与清扫删到
一半的项目目录放后台，做完补进报告。结果写进服务日志并留存到数据目录 `startup-report.json`，运维接口
`GET /api/v1/ops/startup-report` 可以取到，并记下上次是否正常退出。前台步骤做完之前 `/api/health/ready` 返回 503。

## 事件流

三条 SSE 流（前端用 fetch 读，令牌放请求头；SSE 与下载接口也接受 `?token=`）：对话流
`/api/v1/projects/{id}/conversations/{cid}/events`（事件 id 从 1 开始，重连带 `?after=<最后 id>`）、项目流
`/api/v1/projects/{id}/events` 与用户流 `/api/v1/me/events`（id 是 `<进程标识>.<序号>`，重连带
`Last-Event-ID`，补发不了时先发 `stream.reset` 让前端重拉快照）。每 15 秒一次心跳，令牌到期发
`auth.expired` 后关流，每个用户同时最多 8 条流。

## 元模型与写入事务

实体类型、目录、章节结构、`[[链接]]` 语法与文件名编码只在工具箱的 `onto_metamodel.py` 里定义一份，
后端与工具箱脚本共用，`GET /api/v1/metamodel` 给前端（ADR-0030）。链接里的 `#` 是锚点，解析时先按
「名称#锚点」全名找文件，兼容文件名里带 `#` 的存量条目。

网页保存、Studio 提交、MCP 写入、资料与知识条目、求解契约保存、整树回退、示例注入都经同一个写入
事务（`app/kernel/writepath`，ADR-0031）：拿项目锁、校验版本号、原子写入、刷新派生的 `index.md`，
只提交本次路径，提交说明末尾带 `Onto-Source`、`Onto-Author` 等 trailer；agent 还没提交的改动不会被
带进去。

## 助手变更集、写入范围与待处理

见 `docs/decisions/0046-assistant-changesets.md`～`0049-inbox-and-change-lists.md`、`0072-auto-merge-each-turn.md`。

- **助手在副本里改**：每个项目一个 git worktree 副本，固定在数据目录 `changesets/<项目>/assistant`（`resource/`
  是指向主线工作区的链接，`.claude` 链到主线的 `.claude`）。助手在跑时提交只在副本里；**一轮结束（成功、出错、
  被打断）自动合入成一个版本**（叠到最新主线、按实体检测冲突、在主线写一个提交），对话里一轮一张「这一轮改了什么」卡，
  只说改了什么、存成的版本和检查提示，可以**撤回这一轮**（`POST .../changesets/{cs}/discard`：写反向提交、回到上一个
  版本，之后又改过同一条时不撤并说明是哪几条）。没有「确认生效」（ADR-0072）。和你在 Studio 的修改改到同一条时这一轮
  不合入、进待处理选保留哪边。下一轮开始前、启动时补合上一轮没走到收尾的修改（升级前留下的待确认变更集启动时自动合入）。
  启动时核对副本（清残留锁、重建坏了的副本）；建不了副本时这一轮退回主线工作区。
- **检查只提示**：合入时的规则检查、包声明的提交门禁只在卡片上提示（「3 处指向第 2 段待建的内容」「1 处链接找不到
  对象」），指向蓝图里已规划、还没建的内容的链接记「待建」；Studio 保存只拦解析不了的结构，MCP 写入的引用问题只给
  `warnings`；发布时由发布门禁链拦。
- **写入范围只读**：助手在跑时它正在执行的那一步声明的范围（流程节点的 `produces` / `writes`），以及这一轮已经改过的
  条目，在这一轮结束之前（合入冲突时到选好保留哪边）所有编辑器里只读，写入一律 `423 write_scope_locked`（`GET .../scope` 查当前范围）；范围外
  照常编辑。Studio 的 30 分钟租约取消。守卫只放行这一步声明的本体目录。
- **流程与下一步**：节点状态由内核按执行记录、变更集、产物指纹算（进行中、待确认、已确认、过期、需复核……），进度只数
  主线上的；界面建议条、助手回复末尾的「下一步建议」（`state_helper.py suggest`）、两轮之间的提示是同一份。
- **待处理**：`GET .../inbox` 汇总决策（含人工节点的决策卡）、续跑、合入冲突、过期、需复核、报告待处理、
  外部反馈、变更点清单；不另存状态，处理完来源一变就消失。

## 建模蓝图与分段建模

见 `docs/decisions/0050-modeling-blueprint.md`～`0052-slice-write-scope.md`。

- **主流程**（core 0.3.0，详设 6.8）：资料分析（含分段规划）→ 建模（按段：对象、操作、规则、写入即查，启用场景走查
  再加「场景」）→ 校验（与走查）→ 全局一致性（`/ontology-consistency`）→ 发布；指标、流程图是旁支，增量更新按需。
  存量项目跟随通道升级到 0.3.0 后按全量模式显示（一段「全部」），原来对象提取、操作提取上的已确认状态映射过来。
- **建模蓝图**：`research/blueprint.json`，只由 core 的 `blueprint.py` 写入与校验（资料分析时助手写草稿、脚本编号落盘；
  网页调整与确认由后端调同一份）。`GET/PUT .../blueprint`、`POST .../blueprint/confirm`（BP-01～03）；资料分析那一轮结束
  就合入，确认蓝图只标已确认。资料分析到蓝图为止：任何构建方式都停下来等你确认蓝图，「接下来怎么建」不由助手提问、
  也不由自主构建的策略代答。`next=L4`（按蓝图一路建完）在自主构建登记前回
  `409 autobuild_not_available`；自主构建登记后（B5b）返回开始前预览，蓝图卡底部多一个「确认，一路建完…」。
- **按段**：`GET .../flow` 的 `slices`、`slice_stages` 给每段每步的状态，建模节点给「n/m 段」；建模命令带
  `slice=<分段>`（不带时取这一步还没做完的第一段），写入范围只锁这一段的条目，守卫不让助手改别的段已有的条目。
  按段建模时全局一致性是发布前的流程关卡（`GET .../publish/check` 的 `gates` 里 `source.kind=flow`）。

## 自主构建

见 `docs/decisions/0060-autobuild-runner.md`～`0063-autobuild-ledger-and-review.md`。

- **开始**：项目设置里的策略（AB-01、AB-02：范围、预算、决定策略、资料优先级）→ 开始前预览（AB-03，10 分钟内有效；
  也可以在确认蓝图时 `next=L4`）→ 开始（AB-04）。需要已确认的蓝图；有没确认的修改时不能开始。运行在自己的对话里
  （「自主构建 · 运行 #n」），同时运行数超过 `ONTO_WEB_AUTOBUILD_MAX_RUNS`（默认 2，系统设置里可改）的排队。
- **对话里切换**：输入框旁的模式胶囊点开选「陪伴建模」或「自主构建」。切到自主构建：蓝图没确认的先弹出确认，
  确认过的直接到开始前预览；还没有蓝图时只改默认方式（确认蓝图时按自主构建开始）。运行中切回陪伴建模走「转为陪伴」
  （AB-08），并把默认方式改成陪伴建模。不在运行中时等同于在项目设置里改构建方式（P-13）。
- **运行**：按蓝图逐段执行，每段新开上下文、一个变更集（来源 autobuild），每一步命令带 `slice=`；助手的确认卡、决策卡由
  策略立即作答并记进决策台账（达到阈值取推荐项，低于阈值按策略取保守项记为假设或挂起）；检查通过自动生效，产出标
  「需复核」；门禁不过让助手修，修不好挂起这一段。每段生效之后是安全点：暂停（这一步做完）、停在安全点、转为陪伴、
  预算用完都在这里停。运行中别的对话发消息、发命令回 `409 autobuild_running`，运行对话里的消息是补充说明，下一步带给助手。
  命令模板自带的产物确认（「确认无误请回复确认」）按确认往下做，不进台账；策略放行的确认不发「需要你确认」通知。
- **开始前**：构建方式是自主构建、还没有运行时，资料分析（含分段规划）里的提问同样由策略代答（`autobuild/prerun.py`）：
  达到阈值取推荐项，否则取保守项记为假设，不挂起；对话里只留一行「已自动决定」，不出卡片、不进待处理，不进台账。
  确认建模蓝图与开始前预览仍由用户操作。
- **预算**：token 按运行对话的用量事件累加，费用 = token × 模型单价表（系统设置 SY-18，没配时用引擎报的费用，都没有记 0
  并标估算），时长按墙钟；用到 80% 提醒，100% 停在最近的安全点。预算只能设在平台上限（SY-17）与剩余配额之内。
- **复核**：待处理的「自主构建复核」里低于阈值的决定一条一项、高置信的合成一项、挂起的问题一个一项；接受、改判（往运行
  对话发重做命令，运行没结束就排到结束后）、回答挂起问题、按段撤回（反向提交，只动这一段）。台账与运行报告写进
  `reports/autobuild/<运行号>/`。
- **重启**：在跑的运行停在检查点，启动后按执行记录自动续跑（AB-05 `recovered_from_checkpoint`）。

## 治理平面

- **配置**：每个环境变量在配置注册表里声明来源优先级、校验与生效时机（`app/governance/config_registry.py`），
  不合法的值告警后回落；管理员可在 `GET /api/v1/admin/config-registry` 查看（密钥只显示引用）。
- **日志、指标、链路**：日志是 JSON（带项目、会话、执行、变更集、包版本、节点、trace id 等公共标签，
  令牌与口令脱敏），另写数据目录 `logs/`，按日轮转保留 14 天；指标与链路在进程内，运维面
  `/api/v1/ops/metrics`、`/traces`、`/logs` 查看，Prometheus 抓运维监听上的 `/metrics`。
- **审计**：每个写请求在响应之后记一条（操作者、类型、状态码、trace id、来源 IP），另有 agent 提交、
  登录、设置、运维操作等专项记录；不记文件正文与对话内容。按日轮转在数据目录 `audit/`。
- **配额**（`app/governance/quota.py`，ADR-0043）：按用户限并发会话、每日 token、每数据源每小时采集次数，按项目限每月
  token；发起一轮之前检查（发消息、页面命令、重试、续跑、Studio 转助手复核都经会话池），超限返回 `429 quota_exceeded`。用量从会话历史记账（数据目录
  `governance/quota.sqlite`），记在项目所有者名下；限额与单独设置经 `GET/PUT /api/v1/admin/quotas`、
  `POST /api/v1/admin/quotas/overrides` 管理（数据目录 `governance/quotas.json`，0 表示不限）。配额自身出错一律放行。
- **健康检查**：`/api/health/live` 只看进程（容器 HEALTHCHECK 用它）；`/api/health/ready` 探测模型网关、
  auth-server（JWKS）、沙箱网关、磁盘、git 等，关键依赖失败返回 503；`/api/health` 不变。
- **角色**：`user`、`admin`、`ops` 三种（auth-server 签发 `role` claim）。admin 管系统设置、用户、
  指定所有者、服务令牌；ops 管运维面的写操作；运维面只读接口 admin 也能看。admin、ops 都只能看
  别人项目的元数据。

## 运维管理面

运维接口 `/api/v1/ops/*` 在同一进程的第二个监听上（`ONTO_WEB_OPS_HOST`、`ONTO_WEB_OPS_PORT`，默认
`127.0.0.1:8101`，端口 0 关闭），由 `python run.py` 起。独立端口挡不住同容器的 agent，所以运维面照样
校验登录令牌与 ops 角色。`ONTO_WEB_OPS_ON_MAIN=true` 时业务面也处理运维接口：两份 compose 默认打开，
没有反代的单端口部署才用得上运维中心；有反代时在 `.env` 设 `false`（业务面上返回 404），把
`/api/v1/ops/` 转到运维监听，或者设 `ONTO_WEB_OPS_BASE_URL` 让前端直接访问。不经 compose 直接运行后端时默认关。

维护模式（`POST /api/v1/ops/maintenance`）分四步：限制（拒绝新会话，或只读：一切写请求返回 503，
运维面与登录除外）→ 排空（在线会话跑完当前这一轮后回收，超时强制断开并标中断）→ 升级 → 恢复。
另有会话池在线调整、强制断开、排空、熔断、诊断包（脱敏）、版本清单、审计导出等。

插件运维（ADR-0067）：`GET /api/v1/ops/packs` 列出各包在通道上的、项目在用的、禁用的版本；回滚通道
（`POST /packs/{名}/rollback`）只改指针，跟随通道的项目下一条消息换过去；禁用版本后新项目不能再启用它，通道指着它时
改取最新的可用版本（core 的默认版本不能禁用）；灰度只做按项目（beta 通道带比例与名单，跟随 stable 的项目按项目编号
稳定分桶），按租户回 501。熔断的包版本，它的节点暂时跳过、挡住的关卡解除。告警规则（`/api/v1/ops/alerts/rules`）
本期只登记、不评估。

## 密钥与 agent 身份

- 管理员模型 Key、个人 Key 密文与自动生成的主密钥、MCP token 库、服务令牌库都在密钥目录
  （`ONTO_WEB_SECRETS_DIR`，默认数据目录下的 `secrets/`，0700，文件 0600）；老位置的文件在启动时搬过来。
- agent 及其工具子进程以独立的非 root 用户运行（镜像里是 `onto-agent`，`ONTO_WEB_AGENT_USER`）：
  后端仍是 root，起 Claude CLI、执行 Jiuwen 的命令时切到这个用户，只保留它自己的组。工作区、CLI 的
  `HOME`、Jiuwen 工件区归这个组可写；密钥目录与数据目录其余部分它读不到。
- 两个引擎里 agent 执行的 bash 只给最小环境：后端的 `ONTO_WEB_*`、JWT、管理令牌、模型 Key 都不在里面
  （Claude CLI 自己仍拿着当前会话用的模型网关配置）。开发机不配 `ONTO_WEB_AGENT_USER` 时只做环境最小化。
- 必须配置 `ONTO_WEB_MODEL_SECRET_KEY`（至少 32 字节），没配时启动告警。备份只带密文；自动生成的主密钥
  不进备份，配置 `ONTO_WEB_KEY_BACKUP_DIR` 时每次备份单独拷一份到那里（与密文分开放）。

## 能力包、装配目录与主流程

见 `docs/decisions/0024-pack-repository-and-assembly.md`、`0025-flow-definition.md`、`0026-guard-baseline.md`。

- **包仓库**：数据目录 `packs/<名>/<版本>/`，每个版本不可修改、附 `checksums.sha256`；`packs/channels.json`
  记 stable / beta 通道指向的版本。镜像里的 `assets/workspace-toolkit` 是 core 包，启动时入库，版本号是
  清单版本加内容摘要（如 `0.1.0+3f9a1c2d`）。管理员经 `POST /api/v1/admin/packs/uploads` 上传插件包，
  静态检查通过后发布到通道；`ui/` 之外带前端代码、带凭据、名字冲突、兼容范围不符的包检查不通过。旧的
  `POST /api/settings/assets/upload` 改写到这里：没有清单的资产包当作 core 新版本，检查通过直接发布到 stable。
- **锁文件**：每个项目一份 `.onto/packs.lock`（进项目 git），记启用的包、版本与摘要；`.onto/flow.lock` 记它组装出的流程节点
  （来源包与版本、挂法、锚点、完成判定签名），两份在同一个带 `Onto-Source: plugin` 的提交里。项目设置里启用、升级、回滚、
  停用都改它，每次切换是一个已合入的插件变更集（带 `Onto-Changeset`，可用变更集的反向提交撤回）；助手的修改还没确认时，
  会改流程节点的切换回 423（ADR-0065）。
- **装配目录**：数据目录 `assembled/<哈希>/`，内核守卫 + 已启用的包按锁文件组装，锁文件相同的项目共用；
  项目的 `.claude` 是指向它的相对链接，`CLAUDE.md`、`README.md` 指向 `.claude/` 里的同名文件。
  切换用原子替换软链，装配失败项目保持原版本。
- **生效时机**：纯数据包（行业知识）马上生效；能力包从下一条消息开始生效，正在进行的一轮跑完旧版本；
  跟随 stable 的项目在下一条消息前升级到通道版本。`GET /api/v1/projects/{id}/assembly` 看是否有待生效的切换。
- **启动核对**：服务启动、会话开始之前逐个项目核对 `.claude` 指向的装配目录在不在、内容对不对、和锁文件
  一致不一致，不对就重新装配；没有锁文件的存量项目写一份并提交一次。
- **守卫基线**：写保护守卫与 hooks 基线在 `backend/app/kernel_guards/`，随镜像发布、装配时注入，包删不掉；
  守卫出错一律拒绝。
- **可选能力包**（ADR-0042）：场景走查 `trace`、知识沉淀 `knowledge`、代码生成与求解 `codegen`、图示 `diagram`、
  反馈演进 `evo`、联网调研 `websearch`、迁移复用 `migrate` 从 core 拆出，放在 `assets/packs/`，命令名不变。新建项目缺省启用
  前四个（清单 `default_on`），向导里可以改；项目设置里按项目启用、停用，停用后已有产物保留。可选包用自己的 `flow.yaml`
  往主流程上挂节点，`/ontology` 只推荐本项目启用了的命令。锁文件格式升到 2：存量项目在 core 升到 0.2.0 时一次性补上
  拆出去的全部包（能力不变）。联网调研没启用时，项目不能切到联网调研模式。
- **行业包**（ADR-0044）：`assets/packs/industry-retail/`（零售连锁，合成内容）是第一个行业包，新建项目时在向导里选。
  它的知识条目作为只读的行业层加载（项目里同名条目覆盖），`ontology_checker` 按它的声明式规则给警告；在装配目录的
  `industry/<包名>/` 下。知识沉淀包启用时，项目知识可以提名进行业包（自动脱敏后等管理员审核）。
- **扩展项与插件产物**（ADR-0040、0041）：`GET /api/v1/projects/{id}/extensions` 给左栏（内核 4 项 + 已启用包声明的页面）、
  资料子页、入口、产物类型与门禁；左栏跟锁文件走（启用马上出现、停用马上消失），页面按钮等新装配生效。包登记的
  产物（场景、示例数据、流程图、扫描报告、求解契约、业务知识）经 `/api/v1/projects/{id}/artifacts/...` 通用接口读写，
  内核按登记的格式解析、在工作区 `.onto/run/artifacts.sqlite` 建索引，不跑包的代码，也不进知识包。页面按钮经
  `POST /api/v1/projects/{id}/commands` 的 `action` 换成包里的命令；产物变化、左栏变化经项目流
  （`artifacts.updated`、`workspace.changed.artifacts`、`extensions.updated`）推给前端（ADR-0045，接线在 `backend/app/wiring.py`）。
- **包自带界面**（ADR-0055）：对话里的产物卡按包的 `views/<类型>.yaml` 里 `card` 的声明画（`title`、`subtitle`、
  `chips`、`fields`，`component` 选平台的专用卡片组件如 `flowchart`），包不带卡片代码。编辑器可以随包发布：清单
  `contributes.editors: [{id, title, ui_api}]`，视图 `detail.editor: <id>` 引用，双击产物时打开它（没有编辑器时用平台专用
  组件或通用详情）。
  - 官方包（镜像内置）：入口 `ui/<id>/index.jsx`，默认导出接 `{artifact, onClose}` 的 React 组件，随平台一起编译、
    按包懒加载，包没启用时不加载；怎么引平台模块与第三方库见下面「包作者：官方包界面代码」。流程图编辑器就在
    `assets/packs/diagram/ui/flowchart/`。
  - 上传包：`ui/` 里只能放构建好的静态资源（html、js、css、json、图片、字体），入口 `ui/<id>/index.html`，脚本写成
    外部文件。编辑器在不同源的 iframe 里跑，拿不到登录态、自己发不了请求，只能经窄接口（界面接口 1.0）：先往父窗口发
    `{onto_ui: "1.0", connect: true}` 握手，平台转交一个 MessagePort，之后请求 `{onto_ui, id, method, params}`、回应
    `{onto_ui, id, ok, result | error}` 都走它。方法有 `context.get`、`artifact.read`、`artifact.save`（就是 AR-05，助手正在改时
    如实回 423）、`chat.command`（只能发本包清单里的命令，参数是短的单行值，5 秒一条）、`theme.get`。编辑器把自己导航到别的
    页面，平台就关掉它。编辑器的只读状态随写入范围变（`context.get` 给初值，之后推 `readonly.changed`）。包名不能叫
    `ontology` 这类保留名，命令不能和已入库的包重名。合成样例在 `backend/tests/fixtures/packs/sticky-notes/`。
  - `ui/` 不进装配目录。
  - 助手这一轮改到的产物，在「这一轮改了什么」之后各出一张卡，卡片内容按这一轮的树读（ADR-0046、0072）。
- **节点包**（ADR-0065、0066）：清单 `kind: node`，插件目录里归进能力包；只带节点声明（`flow.yaml` 或 `nodes/*.yaml`）和节点要用的
  命令、子代理、脚本、模板，上传时多一项 `node_pack` 检查。节点挂法只能是串入 `step`、旁支 `branch`、关卡 `gate`、按需
  `on_demand`，类型有四种：agent（助手执行入口命令）、script（`script: {name, inputs, output}`，内核经脚本验证网关在
  jiuwenbox 沙箱里跑，不在本机回退，标准输出写进 `output`）、human（`decision: {title, options, record}`，只出一张决策卡，
  结论写进 `record`）、external（`provider: {kind}`，本期没有证据源提供方，启用时回 `409 pack_unavailable`）。启用后从下一条
  消息开始出现在流程里；项目已经越过插入位置时显示「可补做」、不计入进度；停用后节点标「已停用」、它挡住的关卡解除；
  升级后完成判定变了，之前已确认的标「需复核」。合成样例见 `backend/tests/pack_helpers.make_node_pack`。
- **扩展项补齐**（ADR-0068）：清单还能声明界面入口 `entries`（快捷项、附加操作）、门禁 `gates`（`on: save|commit|publish`，
  commit 在写入事务提交与合入时查，publish 进发布门禁链）、视图选择 `view_choices`、配置项 `config`（进配置注册表，键
  `<包名>.<key>`）、指标 `metrics` 与告警规则 `alerts`（进 /metrics 与 O-08，规则本期只登记不评估）、只读 MCP 工具 `mcp_tools`
  （按本包产物类型列表或取一项）；包的 flow.yaml 可以写节点钩子 `hooks`（挂在已有节点前后，进节点动作与两轮之间的提示）。
- **证据源**（ADR-0056）：本期只留接口。`backend/app/evidence/` 只有提供方、网关客户端、数据源登记的协议；
  `/api/v1/evidence/…`、项目下的 `/evidence/…`、`/anchors/resolve` 与提供方安装接口一律回 `501 evidence_not_available`；
  助手没有 `evidence.*` 工具；界面上连接业务系统的入口不出现。
- **主流程**：阶段、完成判定、命令前置与 `/ontology` 的下一步建议只在 core 包的 `flow.yaml` 里定义一次，
  后端进度、`state_helper.py`、`GET /api/v1/projects/{id}/flow` 都读它。「质量检查完成」= 有校验报告且
  `reports/` 下没有未处理的 `[ ]`。包可以用 `slice_steps` 往按段的节点里加步骤、用 `node_titles` 改主线节点的显示名；core 的 `aliases`
  让还没升级的包写的旧节点编号照样认。

### 包作者：官方包界面代码

官方包的 `ui/<id>/` 随平台前端一起编译，但目录在 `assets/packs/<包>/ui/`，不在前端工程里（ADR-0055 补充第 1 条）：

- 平台模块一律写 `@onto/<src 下的路径>`（如 `@onto/app/context.js`、`@onto/styles/flowchart.css`），不写指向
  `frontend/src` 的相对路径。`@onto/` 在开发服务器、单测（`frontend/tests/pack-resolve-hooks.mjs`）和镜像构建里指同一处，
  拿到的上下文、项目流、写入范围锁才和应用是同一个实例。
- 第三方库只用平台前端 `package.json` 里已有的依赖，写裸模块名（`react`、`@xyflow/react`），按平台前端解析；包里不放
  `package.json`、`node_modules`。要用新库，先加进平台前端的依赖、随平台发版；平台没有的库构建直接失败。
- 包内文件之间用相对路径，不引别的包的 `ui/`。
- 只读按写入范围锁判断（项目上下文的 `scope`、`@onto/state/readonly.js`），保存以 AR-05 的 423 为准。

上传包不适用：上传包的 `ui/` 只能是构建好的静态资源，在 iframe 里经窄接口和平台通信（见上面「包自带界面」）。

## 项目所有权

一个项目只归一个人。启用登录鉴权后，项目内容（文件、资料、会话、Studio、版本、导出发布、
MCP Token、改名/归档/删除）只对所有者开放，没有成员与只读评审。管理员不是所有者时只在项目
列表和运维中心看到元数据（名称、所有者、阶段、归档状态、创建时间、占用），可以用
`PUT /api/v1/admin/projects/{id}/owner` 指定所有者（列表卡片上的「指定所有者」）；改成自己即可
打开项目，每次指定都记入审计日志（原所有者、新所有者、操作人）。所有者为空的历史项目普通
用户看不到、谁都打不开，管理员指定所有者后才可用；服务启动时日志报告这类项目的数量与 id，
并删除各项目元数据里旧的成员表（原值先记入审计）。MCP Token 只能由所有者为自己签发，使用时
校验签发人仍是当前所有者，换所有者后旧 Token 自动失效。未启用鉴权时不区分用户，全部放开。

## 版本历史与项目元数据

项目元数据 `.onto-web.json`（所有者、项目名等）、工具状态 `.ontology-state/` 与运行期
文件 `.onto/run/` 不进项目 git：回退版本不会回滚所有者与项目名。存量项目在服务启动时
迁移一次：补齐 `.gitignore`、把这几个路径从 git 中摘除，单独提交为
「chore: 停止跟踪项目元数据与工具状态」，工作区里其他已暂存、未暂存的改动不受影响；已迁移的
项目启动时什么都不做。回退、克隆、导入（含旧版迁移包）同样保证这几个路径不被重新跟踪，
导出迁移包仍带 `.onto-web.json`（不带 `.ontology-state/`、`.onto/run/`）。

服务启动时先清掉各项目 `.git` 下上次停机残留的锁（`index.lock`、`HEAD.lock`、`packed-refs.lock`、
`refs/**/*.lock`），删除的文件记入服务日志。运行期间 git 因锁文件失败时，锁已存在超过 120 秒
才视为残留，删除后重试一次，否则照原样报错。

网页上传、替换、删除资料和改资料用途，与 Web 保存文件一样当即提交，提交里只含本次涉及的文件
和 `resource/materials.json`，agent 已暂存或未暂存的其他改动不会被带进去；删除知识条目同样只
提交该文件。旧版上传不提交，服务启动时会把资料区里还没进 git 的上传补提交一次（资料区只有网页
能写，不会带进 agent 的改动）。主线版本只计合入主线的提交（`git rev-list --first-parent`），版本号
n 从 1 开始（建项目的基线提交是 v0.1），界面显示 `v0.{n}`；版本列表分页返回全部主线版本，来源
（助手、网页、MCP、导入、回退等）优先取提交 trailer `Onto-Source`，存量提交按提交说明的前缀推断。
回退是整树恢复：目标版本之后新增的文件一并删除，生成一个新版本，历史保留。

发布用 `POST /api/v1/projects/{id}/releases`（`{commit?, degrade?}`，缺省主线最新），按提交打包：
在项目锁内确定提交，用 `git archive` 把发布要读的内容（对象、操作、规则、指标、脚本、求解契约目录，
以及资料区的 `materials.json` 和表格文件）导出成只读树，门禁与渲染都只读这棵树，工作区里没提交的
改动不进包。发布检查 `GET /api/v1/projects/{id}/publish/check` 同口径，给出门禁链（数据绑定、求解
契约回执、知识包结构；降级发布只放宽数据绑定）并列出这些路径里未提交的改动（资料区的 PDF、图片等
发布不读，不算在内）。求解验证回执在 `.ontology-state/`，不进 git，仍从工作区读取。

版本号为 `0.{n}.0`，n 是发布提交的主线版本号（与界面上的 v0.{n} 对应）；该提交已发布过就沿用原
版本（同一提交重复发布，包逐字节相同）；`0.{n}.0` 不大于项目已有的全部发布标签（含旧版的 `0.1.N`）
时，取已有最大版本的补丁号 + 1，保证单调递增。每次发布打注释标签 `release/<版本>`（注明发布人与
是否降级发布），在版本列表里标出、不能删除；包清单写入 `source_commit`；包原样留存在数据目录
`releases/<项目>/<版本>.zip`，按版本下载读的就是这一份（见下文「对接评测服务：三份契约」）。
渲染由发布适配器完成（`backend/app/services/publish/`，见 ADR-0023），重构前后同一提交、同一版本
参数渲染出的包逐字节相同，由 `tests/test_publish_render_regression.py` 守住。

## 数据备份

运维中心可手动备份；`ONTO_WEB_BACKUP_INTERVAL_HOURS`（默认 24，0 关闭）控制定时备份，启动时
距上次备份已超过间隔就先补一份，再按间隔循环。备份写到 `ONTO_WEB_BACKUP_DIR`（默认数据目录下
的 `backups/`，与数据同卷；建议挂独立卷并设置该变量），保留最近 3 份，文件权限 0600。

一份备份是一个 `onto-modeling-backup-<时间>.tar.gz`，`data/` 与数据目录一一对应、`home/` 对应
`HOME`：各项目工作区（含 git 历史，按项目持锁打包）、会话历史与会话索引、Jiuwen 存档、Studio
草稿、密钥目录里的密文（管理员模型配置、个人 Key 密文、MCP token 库、服务令牌库）、包仓库
`packs/`、发布留存的知识包、变更集记录与节点状态（`changeset-state/`、`flow-state/`、`change-lists/`）、
各类设置、审计与错误留档，以及存在时的 CLI transcript（`~/.claude/projects`）。助手副本（`changesets/`）不备，
恢复后按副本分支重建，没提交的改动会丢。SQLite 库（Jiuwen 存档、MCP token 库）用 SQLite 的 backup API 拷贝，
不直接打包正在使用的库文件。恢复时停服务，把 `data/` 解到 `ONTO_WEB_DATA_DIR`、`home/` 解到
`HOME`。

个人 Key 的主密钥不在备份里：未设 `ONTO_WEB_MODEL_SECRET_KEY` 时它是密钥目录下自动生成的
`.model-secret-key`。主密钥与加密后的个人 Key 放在同一个包里等于没加密，所以要单独保管该文件
（配置 `ONTO_WEB_KEY_BACKUP_DIR` 时每次备份自动单独拷一份），或改用环境变量提供；异机恢复时缺了
同一把主密钥，已保存的个人 Key 解不开，用户需重新填写。

## 场景与来源核验

- 场景走查由 `trace_inputs.py` 统一读取已保存确认稿，保留字段类型与尾注，派遣前及报告前核对源文件修订和初始输入回执。解析或对账失败不计通过。
- 来源核验使用 `source_extract.py` 抽取 UTF-8 文本、DOCX 正文/表格和 XLSX 单元格，返回原文件摘要与定位锚点；无法读取或定位时明确标记“无法核验”，不以新增 ID 声明或历史基线免检。
- 页面及新报告不再展示覆盖率统计，仍展示具体缺项与推演通过率；旧报告原文保留。

升级时需匹配工作区工具箱版本；旧回执保留但须重新核验。实现与兼容边界见
[`modeling-review-integrity.md`](../../docs/decisions/modeling-review-integrity.md)。

## 对接评测服务：三份契约

评测已移出 onto-modeling（[ADR-0020](docs/decisions/0020-eval-removal-and-contracts.md)）：
「数据评测」阶段、评测数据源、评测集、评测编排、求解端到端验证编排和 Modeling 项目关联都已删除，
阶段从 11 个变为 10 个。onto 不主动调用评测服务，评测服务持**服务令牌**调用下面三份契约的接口。

**服务令牌**（API 契约 1.3、X-06）：管理员在 `/api/v1/admin/service-tokens` 签发（`POST`，请求
`{name, scopes, project_ids?, expires_in_s?}`，明文只在响应里出现一次）、列出（`GET`）、吊销（`DELETE /{id}`）。
令牌形如 `onto_svc_…`，只存 SHA-256 摘要（数据目录 `service-tokens.json`，0600），和项目的 MCP token 分开签发、
分开存储。权限范围 `releases.read`、`receipts.write`、`feedback.write`；`project_ids` 限定项目（不传为全部），
`expires_in_s` 设有效期（不传为不过期）。服务令牌只能调用下面的接口，用在别处一律 401。

1. **包出**（`releases.read`）
   - `GET /api/v1/contract/releases?since=&project_id=&cursor=&limit=`（X-01）：令牌授权范围内全部项目的发布，
     按发布时间从新到旧，游标分页（默认 100、上限 200）；
   - `GET /api/v1/projects/{id}/releases/{version}`（PB-05）：发布记录（版本、标签、提交、格式、发布时间、
     发布人、是否降级、包摘要、拉取次数）；
   - `GET /api/v1/projects/{id}/releases/{version}/manifest`（PB-06）：包清单（`manifest.yaml` 内容、包摘要、
     文件清单）；
   - `GET /api/v1/projects/{id}/releases/{version}/download?variant=standard|graph`（PB-07）：按版本下载。读发布时
     留存的包，同一版本同一变体永远是同一份字节；响应头 `X-Package-Version`、`X-Package-Commit`、
     `X-Package-Format`、`X-Package-Variant`、`X-Package-Sha256`。`graph` 是图模式变体（给评测服务导入图库），
     第一次下载时按同一标签提交渲染并留存。标准包留存缺失（数据目录丢失、留存功能之前的旧标签）时按标签提交
     重新渲染一次并补存。服务令牌下载时在发布记录上记「已拉取」。
   - 以上三个接口也接受项目所有者的登录 token（下载可用 `?token=`）；`GET /api/v1/projects/{id}/releases`
     （PB-04，发布记录列表）只给所有者。
2. **验证回执入**（`receipts.write`）
   - `GET /api/v1/projects/{id}/solver/candidate[?allow_unbound=true]`：求解验证候选包（API 契约之外的补充，
     见 ADR）。按 HEAD 提交渲染、manifest 标 `status: draft`，不打标签；响应头带 `X-Solver-Revision`
     （求解契约修订号）、`X-Solver-Contract-Digest` 与发布时会得到的 `X-Package-Version`；
   - `POST /api/v1/projects/{id}/solver/receipts`（X-04）：必填 `revision`、`solver_keys`（必须正好是该修订启用的
     求解操作）、`status`（`passed` / `failed`）、`authoritative`、`contract_digest`、`request_digest`、
     `result_digest`、`completed_at`、`receipt_id`（幂等键），可选 `release_version`、`candidate_count`、
     `evidence`（不超过 64 KB，只放引用）、`error_code`、`message`。修订号必须是当前修订（HEAD 提交或工作区的），
     否则 `422 receipt_revision_unknown`；同一 `receipt_id` 只生效一次。发布门禁照旧只认当前修订的「权威通过」
     回执，求解模型面板经 `GET /api/v1/projects/{id}/solver/receipts`（SV-04）读取。
3. **反馈入**（`feedback.write`）
   - `POST /api/v1/projects/{id}/feedback`（X-05）：一批反馈，请求 `{feedback_id（幂等键）, source: {service, run},
     release_version?, items: [{case_id, question, expected, actual, loss_reason?, suspected_artifacts?}], received_at?}`，
     最多 200 条、2 MB；另收两项补充字段：`summary`（score、total、pass、partial、fail、error）与条目的
     `verdict`（fail / partial / error，缺省 fail）、`tool_failures`（工具失败行）。不合法返回
     `422 feedback_invalid`（`details.errors`）。
   - 落到工作区 `feedback/<接收时间>-<feedback_id>.json` 并单独提交（只提交这个文件，不进知识包），文件加
     `format: onto-feedback/v1`、`received_at`、`received_by`，评测侧给的 `received_at` 记为 `reported_at`。
   - `/ontology-evolution` 与 `check_eval_generalization.py`（反馈演进包，项目启用后才有）默认读最新一批反馈。

**升级迁移**（启动时一次性、幂等）：各项目 `.ontology-state/eval-data/` 里评测上传的数据表，凡有 Sheet
被对象的数据绑定用到（声明或推断），复制进 `resource/excel/` 并在 `materials.json` 标为 `schema`，
单独提交（只提交这些路径）；随后删除 `.ontology-state/` 下的评测数据（上传表、`eval.json`、求解验证用例）
与项目元数据里的 Modeling 关联字段，最后删除数据目录下的评测数据源配置（`eval-data-sources/`，含连接密码）
与 Modeling 清理队列。单个项目迁移失败只记运维错误日志，下次启动重试；数据源配置不等它，照删。

升级后注意：

- **资料分析验收回执会整体失效一次**：验收指纹含评测上传的数据表（以及 `materials.json`），上传过
  这类表的项目迁移后指纹对不上，验收状态回到「待核验」（原因：源资料已改动），需要重新跑一次资料分析
  验收（`research_checker.py`）。对象已提取的项目只显示待核验提醒，不挡后续阶段；没上传过评测数据表的
  项目不受影响。
- 工具箱的表登记脚本更名为 `table_profile.py`（`--headers`、`--profile`、三方对账），旧名
  `check_eval_data.py` 保留一个版本转发；`mock_to_eval_seed.py` 已删除。
- 不再读取的环境变量：`ONTO_WEB_MODELING_URL`、`ONTO_WEB_MODELING_TOKEN`、
  `ONTO_WEB_MODELING_ASSOCIATION_URL`、`ONTO_WEB_MODELING_ASSOCIATION_TOKEN`、
  `ONTO_WEB_MODELING_CLEANUP_TOKEN`、`ONTO_WEB_MODELING_CLEANUP_INTERVAL`、
  `ONTO_WEB_EVAL_MAX_CONCURRENT_ROUNDS`。

## 测试

```bash
cd backend && ./venv/bin/python -m pytest tests/
cd frontend && npm test && npm run build
```

转换器与连接器网关的测试要转换库（`backend/connector-requirements.txt`，只装进网关镜像）：没装时 `test_f4a_converters.py` 和网关测试里用真转换器的那条跳过，
Linux 全量用装了它的测试镜像。

## 构建与部署

```bash
./build.sh <version> [amd|arm]     # 产出 dist/onto-modeling-<version>-linux-<arch>.tar.gz
docker compose up --build          # 本地从源码起全栈（onto-modeling + auth-server + 两个网关）验证
```

离线包里有三个镜像：onto-modeling、auth-server、连接器网关（`onto-modeling-connector-gateway`，同一个 Dockerfile 的
`connector-gateway` 目标，只装 `backend/connector-requirements.txt` 里的转换库，见 ADR-0073）。

依赖复用（需 BuildKit，Docker Desktop / buildx 默认开启）：

- pip 索引源默认华为云 PyPI 镜像，可用 `ONTO_MODELING_PIP_INDEX_URL`（或 `PIP_INDEX_URL`）覆盖，
  例如 `ONTO_MODELING_PIP_INDEX_URL=https://pypi.example.com/simple ./build.sh <version>`；
- 宿主 pip 运行在 Python 3.11 及以上时，构建脚本先把目标平台 wheel 供应到 `vendor/wheels/`
  （含约 100MB 的 claude-agent-sdk，命中宿主缓存则零下载），镜像内经 bind mount 作为 find-links，
  索引不可达时兜底；宿主 Python 更低时环境标记无法按 3.11 求值、供应不完整，脚本跳过这一步，
  避免大包在宿主和镜像内各下一遍；
- 运行镜像基于 `python:3.11-slim`、前端构建用 `node:22-slim`，与 auth-server /
  consumer / modeling 同基础镜像，复用已拉取的层；
- npm 与 pip 均挂 BuildKit 缓存，同机重复构建不再重新下载。

目标机部署流程与容量规划（含 AVX 预检、降级兼容包）见 `deploy/README.md`。

### 构建进度提醒与 HTTPS

构建跑完一步、agent 提问或请求确认、构建出错时，如果用户不在工作台页面，
前端会弹出系统通知，并让标签页标题和图标按提醒色闪烁。浏览器只允许 HTTPS 或 localhost 页面弹系统通知：
用 HTTP 加 IP 访问时只有标题和图标闪烁，设置页“通知”里会提示原因。需要系统通知时请为工作台配置 HTTPS。
提醒只覆盖标签页当前显示的会话；同时跟进多个项目时，每个项目各开一个标签页。

## 项目级本地 MCP

在项目设置的 MCP 页面创建 Token 并复制 Read / Change 地址。MCP 直接读写当前
Onto 工作区，新建、导入和克隆项目无需创建 Modeling 关联。旧 Modeling MCP
地址及 Token 不会自动迁移，请为本地端点重新创建 Token。

- HTTP：`/api/v1/onto-mcp/{project_id}/{read|change}/mcp`。
- SSE：`/api/v1/onto-mcp/{project_id}/{read|change}/sse`。
- 已经配好的旧地址 `/api/onto-mcp/...` 永久保留，行为相同。
- Token 通过 `Authorization: Bearer <token>` 传递，按项目隔离；读、校验、保存
  分别要求 `ontology.read`、`ontology.change.validate`、`ontology.change.apply`。
- 写操作必须提供最新 `expected_revision`；修改保存到本项目的 Git 工作区。
- 默认按访问工作台的地址生成 MCP 地址。反向代理部署可选用
  `ONTO_WEB_MCP_PUBLIC_BASE_URL` 指定公开地址，例如 `https://example.com`。

设计与兼容边界见 [本地 MCP 设计说明](docs/decisions/onto-local-mcp.md)。

## 当前状态

已有：项目生命周期（创建/导入向导（已发布知识包与工作区内容格式自动识别，识别不出
或解析为空时拒绝导入）/导出（含按提交发布知识包与发布标签）/克隆/归档/永久删除，项目只归
所有者，管理员只看元数据并指定所有者；新建/导入/重命名/克隆时校验任务名称，重名给出候选名）、
资料上传（分类/替换/变更提醒）、Agent 会话编排（会话池 +
排队 + 空闲回收 + resume 续接、流式消息、确认卡、打断）、hash 路由与深链、文件树
（树内搜索）/ markdown（语法高亮/代码复制/mermaid 主题联动）、drawio 预览与
在线编辑（官方 drawio 离线 webapp）、Excel 预览、HTML 沙箱预览、`[[链接]]`跳转、
文件编辑乐观并发（Web 保存即版本，冲突走合并对比）、版本时间线/回退/标记、
Studio 三视图（图/表/条目 + **结构化编辑：schema 表单、草稿区、助手写入范围只读、
确定性检查分流**）、助手变更集（每轮自动存成版本 / 撤回这一轮）与待处理汇总、报告逐条评审（编号勾选/逐条修复）、走查分支卡、业务知识
沉淀（对话一键入库 + 知识库管理页）、六种反查面板、挂卡重复计数与取消出口、
单轮成本/缓存命中展示、回收站恢复（仅旧版软删除留下的存量条目）、登录鉴权与账号改密、设置中心独立页
（模型/MCP 测试连接与按项目启用/权限模式/jiuwenbox 沙箱连接状态）、能力包按项目启用/升级/回滚、
代码生成脚本必做运行验证（经脚本验证网关在 jiuwenbox 沙箱中执行，不可用时在容器内执行，未验证的脚本不能提交）、
运维中心（用量/审计/
备份定时调度/用户管理/会话池/健康页/回收站）、离线交付包构建与部署链、
consumer 同源色系。

未做（后续分段）：pixi.js 大图渲染完整路径（现为 xyflow + 大图降级模式）、
代码生成链评审页的专属候选格式。
