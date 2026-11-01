# Onto Modeling — 本体建模工具 Web 版

基于 Claude Agent SDK 的本体建模 Web 工作台。设计方案见
`/mnt/jianguoyun/meta/design/本体建模工具Web版-方案设计-20260918.md`。

## 结构

```
onto-modeling/
  backend/    FastAPI 服务：项目/工作区管理、Agent 会话编排（会话池 + SSE）、文件与版本 API
  frontend/   React 19 + Vite 7 + antd + @ant-design/x 三栏工作台
  assets/     官方能力包：workspace-toolkit 即 core 包（manifest.yaml、flow.yaml），启动时导入包仓库
  deploy/     离线交付包构建、预检与部署脚本
  docs/       架构决策记录
```

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

Jiuwen 引擎只执行工具箱的守卫钩子，会话开始和用户发消息时的两个钩子（阶段提示、业务知识信号）
由后端补上：每条消息送达模型前，把当前阶段、进行中的命令、最近报告的待处理数和命中的信号词
提示附在消息前面，只发给模型，会话历史里仍是用户原话。阶段提示在会话开始时必带，之后内容变了
才再带；阶段用项目装配目录里哈希核对过的 `state_helper.py` 读取，同样只给最小环境。稳定引擎仍靠这两个钩子。
见 `docs/decisions/0022-host-side-session-injection.md`。

## 接口版本与旧路径兼容

后端接口统一在 `/api/v1` 下（契约见 `docs/api-v1.md`），错误体统一为
`{code, message, details, trace_id}`，`code` 取契约第 2 章的错误码，`trace_id` 也在响应头
`X-Trace-Id` 里。旧路径 `/api/...` 在兼容期（到 2027-05-01）经兼容中间件改写到对应的 v1 接口：
请求和响应都是 v1 的形状，响应多带 `Deprecation`、`Sunset`、`Link` 头，错误体额外带 `error`
（值同 `message`）；已删除的旧接口（评测、Studio 租约、成员、发布用的 `GET .../export?scope=package`
等）返回 `410 endpoint_removed`，`details.successor` 给出替代接口。`/api/health` 与 MCP 旧地址
永久保留。见 `docs/decisions/0021-api-v1-and-legacy-compat.md`。

## 启动自检

服务启动时、任何会话开始之前做一次自检：清掉各项目 `.git` 下残留的锁，列出各项目没提交的改动
（保留原样，等所有者处理），结果写进服务日志并留存到数据目录 `startup-report.json`，运维接口
`GET /api/v1/ops/startup-report` 可以取到（运维总览的「启动自检」摘要也读它）。

## 能力包、装配目录与主流程

见 `docs/decisions/0024-pack-repository-and-assembly.md`、`0025-flow-definition.md`、`0026-guard-baseline.md`。

- **包仓库**：数据目录 `packs/<名>/<版本>/`，每个版本不可修改、附 `checksums.sha256`；`packs/channels.json`
  记 stable / beta 通道指向的版本。镜像里的 `assets/workspace-toolkit` 是 core 包，启动时入库，版本号是
  清单版本加内容摘要（如 `0.1.0+3f9a1c2d`）。管理员经 `POST /api/v1/admin/packs/uploads` 上传插件包，
  静态检查通过后发布到通道；带前端代码、带凭据、名字冲突、兼容范围不符的包检查不通过。旧的
  `POST /api/settings/assets/upload` 改写到这里：没有清单的资产包当作 core 新版本，检查通过直接发布到 stable。
- **锁文件**：每个项目一份 `.onto/packs.lock`（进项目 git），记启用的包、版本与摘要；改它是一次带
  `Onto-Source: plugin` 的提交。项目设置里启用、升级、回滚、停用都改它。
- **装配目录**：数据目录 `assembled/<哈希>/`，内核守卫 + 已启用的包按锁文件组装，锁文件相同的项目共用；
  项目的 `.claude` 是指向它的相对链接，`CLAUDE.md`、`README.md` 指向 `.claude/` 里的同名文件。
  切换用原子替换软链，装配失败项目保持原版本。
- **生效时机**：纯数据包（行业知识）马上生效；能力包从下一条消息开始生效，正在进行的一轮跑完旧版本；
  跟随 stable 的项目在下一条消息前升级到通道版本。`GET /api/v1/projects/{id}/assembly` 看是否有待生效的切换。
- **启动核对**：服务启动、会话开始之前逐个项目核对 `.claude` 指向的装配目录在不在、内容对不对、和锁文件
  一致不一致，不对就重新装配；没有锁文件的存量项目写一份并提交一次。
- **守卫基线**：写保护守卫与 hooks 基线在 `backend/app/kernel_guards/`，随镜像发布、装配时注入，包删不掉；
  守卫出错一律拒绝。
- **主流程**：阶段、完成判定、命令前置与 `/ontology` 的下一步建议只在 core 包的 `flow.yaml` 里定义一次，
  后端进度、`state_helper.py`、`GET /api/v1/projects/{id}/flow` 都读它。「质量检查完成」= 有校验报告且
  `reports/` 下没有未处理的 `[ ]`。

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
草稿、MCP token 库、服务令牌库（只有摘要）、发布留存的知识包、各类设置、审计与错误留档，以及存在时的 CLI transcript
（`~/.claude/projects`）。SQLite 库（Jiuwen 存档、MCP token 库）用 SQLite 的 backup API 拷贝，
不直接打包正在使用的库文件。恢复时停服务，把 `data/` 解到 `ONTO_WEB_DATA_DIR`、`home/` 解到
`HOME`。

个人 Key 的主密钥不在备份里：未设 `ONTO_WEB_MODEL_SECRET_KEY` 时它是数据目录下自动生成的
`.model-secret-key`。主密钥与加密后的个人 Key 放在同一个包里等于没加密，所以要单独保管该文件，
或改用环境变量提供；异机恢复时缺了同一把主密钥，已保存的个人 Key 解不开，用户需重新填写。
每次备份都会在服务日志里提醒这一点。

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
   - `/ontology-evolution` 与 `check_eval_generalization.py` 默认读最新一批反馈。

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

## 构建与部署

```bash
./build.sh <version> [amd|arm]     # 产出 dist/onto-modeling-<version>-linux-<arch>.tar.gz
docker compose up --build          # 本地从源码起全栈（onto-modeling + auth-server）验证
```

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
Studio 三视图（图/表/条目 + **结构化编辑：schema 表单、草稿区、30 分钟租约、
确定性检查分流**）、报告逐条评审（编号勾选/逐条修复）、走查分支卡、业务知识
沉淀（对话一键入库 + 知识库管理页）、六种反查面板、挂卡重复计数与取消出口、
单轮成本/缓存命中展示、回收站恢复（仅旧版软删除留下的存量条目）、登录鉴权与账号改密、设置中心独立页
（模型/MCP 测试连接与按项目启用/权限模式/jiuwenbox 沙箱连接状态）、能力包按项目启用/升级/回滚、
代码生成脚本必做运行验证（经脚本验证网关在 jiuwenbox 沙箱中执行，不可用时在容器内执行，未验证的脚本不能提交）、
运维中心（用量/审计/
备份定时调度/用户管理/会话池/健康页/回收站）、离线交付包构建与部署链、
consumer 同源色系。

未做（后续切片）：pixi.js 大图渲染完整路径（现为 xyflow + 大图降级模式）、
代码生成链评审页的专属候选格式。
