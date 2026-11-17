# onto-modeling API v1 契约

| 项 | 内容 |
|---|---|
| 版本 | v1.0（C0 阶段产出） |
| 日期 | 2026-11-01 |
| 依据 | 详细设计 v1.1（20261101）；后端改造方案（20261101）；高保真设计稿第五版（34 个界面页、`features.js`、`标注.json`）；主线 worktree `feat/onto-modeling-plugin-architecture` @ 2ad2ad7b 的 `backend/app/routers/*.py` |
| 适用 | 前端按设计稿第五版重写、后端按详设 v1.1 改造，都以本文件为准 |
| 修改 | 实现中必须改契约时，在 `_plan/contract-changes.md` 追加一条（改了什么、为什么），不得静默偏离 |

**怎么读**

- 第 1 章通用约定，第 2 章错误码目录，第 3 章按资源分组的接口，第 4 章 SSE 事件目录，第 5 章页面 → 接口对照表，第 6 章旧接口对照，第 7 章数据模型附录，第 8 章本文定下的不确定项，第 9 章详设与设计稿的矛盾。
- 每个接口一个小标题，编号形如 `P-03`。编号前缀对应分组（见 3.0）。
- 接口块里的「依据」写详设章节或接口编号（①～㊹），「页面」写设计稿页面 id。
- 对象类型名（如 `ProjectCard`、`ScopeLock`）在第 7 章定义；接口里只写引用。
- 只写契约，不写实现。实现位置以改造方案第 3 章为准。
- 界面叫法：插件里的「证据源」「证据源提供方」在界面上叫「多源连接器」（F4a）；采回来的仍叫证据（证据快照、证据浏览器）。接口路径、字段、错误码里的 `evidence`、`provider` 不变。

---

## 1 通用约定

### 1.1 前缀、版本与两个监听面

| 面 | 前缀 | 监听 | 说明 |
|---|---|---|---|
| 业务面 | `/api/v1` | 主监听（容器内 8100） | 工作台、Studio、资料、发布、插件、个人与系统设置、三份契约 |
| 运维管理面 | `/api/v1/ops` | 运维监听（默认 `127.0.0.1:8101`，同进程，见方案 E8） | 运维中心 4 页的大部分接口（㊲） |
| 健康检查 | `/api/health`、`/api/health/live`、`/api/health/ready` | 两个监听都提供 | 不带版本号，免鉴权 |
| MCP 传输 | `/api/v1/onto-mcp/...` | 主监听 | 外部协议（②），MCP token 鉴权 |

- 前端从 `C-01 GET /api/v1/client-config` 取 `ops_base_url`，运维管理面接口一律请求 `{ops_base_url}/api/v1/ops/...`。`ops_base_url` 为空串表示与业务面同源（部署在反代上把 `/api/v1/ops/` 转到运维监听）。
- 运维管理面同样校验 JWT（方案 E8：独立端口挡不住同容器的 agent）。
- 业务面上收到 `/api/v1/ops/*` 时：配置 `ONTO_WEB_OPS_ON_MAIN=true` 则照常处理，否则返回 404 `not_found`。

### 1.2 旧路径兼容期

依据方案 A14。

1. 兼容中间件位于鉴权中间件之前，把旧路径改写为 v1 路径后交给 v1 处理器。对照表见第 6 章。
2. 经旧路径进来的响应加三个头：
   - `Deprecation: true`
   - `Sunset: Sat, 01 May 2027 00:00:00 GMT`（v1 上线后 6 个月；之后旧路径一律 410）
   - `Link: </api/v1/...>; rel="successor-version"`
3. 旧路径的响应体就是 v1 的响应体，不做形状兼容。唯一的例外：错误体额外带 `error` 字段（值同 `message`），方便旧脚本读取。
4. 已删除的旧接口（评测、Studio 租约、成员、资产包等，见第 6 章）返回 `410 endpoint_removed`，`details.successor` 给出替代接口或 `null`。
5. 永久保留、不进兼容期的旧路径：
   - `/api/health`：响应不变，`{ok, agent_sdk, drawio}`；
   - `/api/onto-mcp/{project_id}/{toolset}/{mcp|sse|sse/messages}`：外部 MCP 客户端已经配好的地址，行为与 `/api/v1/onto-mcp/...` 完全相同，不加 Sunset 头；
   - `/drawio/*`：drawio 静态页，不属于 API。
6. 免鉴权前缀同步更新为：`/api/v1/auth/`、`/api/v1/client-config`、`/api/health`、`/api/v1/onto-mcp/`、`/api/onto-mcp/`。
   - 评测服务的完成通知 `POST /api/internal/evaluation/notifications`（`ES-00`）也免登录，由处理器校验服务凭据；路径是评测服务调用的外部路径，不进 v1、不改。
   - 带 `onto_svc_` 前缀令牌的请求，鉴权中间件只放行到服务令牌接口：`PB-05`、`PB-06`、`PB-07`，由处理器校验权限范围；用在其他接口返回 `401 unauthenticated`（`X-01`、`X-04` 已随 ADR-0098 退役，`X-05` 随 ADR-0092 退役）。

### 1.3 鉴权

**JWT。**

- auth-server 签发 RS256 access token，后端按 JWKS 验签，校验 `iss`、`aud`、`typ`（③）。
- 携带方式：`Authorization: Bearer <access_token>`。
- 只有下面这些接口允许用查询参数 `?token=` 携带（浏览器的 EventSource 和下载链接带不了头）：
  - 全部 SSE 接口（第 4 章列出的 4 条流）；
  - 文件下载：`F-04`、`P-09`、`PB-07`、`V-03` 的 `download=1`、`PL-16`、`PL-27`、`O-47`、`O-49`。
- 其余接口带 `?token=` 一律忽略。
- 令牌过期返回 `401 token_expired`，无效返回 `401 unauthenticated`。前端用 `A-04` 无感刷新后重试一次。

**角色。** JWT 的 `role` claim 取三个值之一（方案第 6 章 #2：auth-server 新增 `ops`）：

| role | 界面叫法 | 能做什么 |
|---|---|---|
| `user` | 建模者 | 自己的项目全部操作；个人设置；浏览插件目录，把包启用到自己的项目；提交数据源登记申请、插件安装申请 |
| `admin` | 管理员 | `user` 的全部能力，另加：系统设置（3.23）、用户管理、指定项目所有者、插件上传 / 审核 / 发布、平台安装提供方、证据源租户级登记与凭据、知识提名审核、服务令牌 |
| `ops` | 运维 | `user` 的全部能力，另加：运维管理面（3.24 中标 ops 的接口）、审计、错误留档、回收站 |

- admin 也能调用运维管理面的只读接口和回收站（见各接口「调用方」）。
- 任何角色都能创建并拥有自己的项目。管理员和运维**不能**读写别人项目的内容，只能看项目元数据（1.6、详设 1.6）。

**本地模式。** 没有配置 `ONTO_WEB_AUTH_URL` 时鉴权关闭：

- 所有请求视为本机用户 `local`，同时拥有 `user`、`admin`、`ops` 三种角色；
- 所有权检查全部放行；
- 审计照常记录，`actor` 为 `local`；
- `C-01` 返回 `auth.enabled=false`，登录页显示「本地模式」。

**MCP token。** 只用于 `/api/v1/onto-mcp/*` 传输接口（M-05～M-07）。

- 格式 `onto_pat_<urlsafe>`，只存 SHA-256 摘要，明文只在签发响应里出现一次。
- 权限范围 `ontology.read`、`ontology.change.validate`、`ontology.change.apply`，新增 `evidence.read`（方案 E13）、`project.version.revert`（版本列表、回退预览与回退，read 和 change 两个工具集都认，ADR-0094）、`package.publish`（发布知识包版本，ADR-0098）。
- 含 `package.publish` 的 token 要统一账号登录签发，只能给登录用户本人签：签发时在 Hub 登记委托（不保存登录令牌），吊销时一并撤销；Hub 登记失败则不签发（`401 MCP_HUB_IDENTITY_REQUIRED`、`403 MCP_HUB_IDENTITY_MISMATCH`）。
- token 绑定项目和签发人；签发人不再是项目所有者时立即失效。

**服务令牌**（三份契约用，方案第 6 章 #6）。

- 格式 `onto_svc_<urlsafe>`，由管理员签发（X-06），与 MCP token 分开存放。
- 携带方式：`Authorization: Bearer onto_svc_...`。
- 权限范围：`releases.read`（`receipts.write` 随 X-04 退役，`feedback.write` 随 X-05 退役）。可以限定到项目列表，也可以不限。
- 只能调用 `PB-05`、`PB-06`、`PB-07`；用在别处返回 `401 unauthenticated`。

### 1.4 项目所有权

依据方案 A11、`services/ownership.py`。

- 一个项目只有一个所有者（`owner` = 用户名），没有成员和评审人。
- 项目内容接口（工作区文件、条目、对话、产物、报告、版本、变更集、证据快照等）只有所有者能调。
  - 非所有者返回 `403 project_owner_only`；
  - 项目没有所有者时返回 `403 project_unowned`（等管理员用 `AD-07` 指定）。
- 项目元数据接口（`P-01`、`P-04`）对 admin 和 ops 返回 `ProjectMetaView`：只有名称、所有者、阶段、构建方式、占用、最近活动，没有目标、资料、产物。
- 项目 ID 不合法或不存在返回 `404 project_not_found`。为了不泄露存在性，非所有者访问不存在的项目也返回 404。
- 已归档项目只读。写接口返回 `409 project_archived`，取消归档（`P-07`）除外。

### 1.5 错误体

所有非 2xx 响应体都是：

```json
{
  "code": "revision_conflict",
  "message": "文件已被助手修改，请刷新对比后再保存",
  "details": {"path": "ontology/object/配送时限.md", "expected": "9f3c1a…", "actual": "a71e03…"},
  "trace_id": "4f9c2e1d"
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `code` | string | 小写蛇形错误码，全集见第 2 章 |
| `message` | string | 给用户看的中文短句，遵守用户词汇表，不含路径以外的内部术语 |
| `details` | object \| null | 错误码约定的结构化补充 |
| `trace_id` | string | 本次请求的链路 ID，可在可观测（`O-04`）里查 |
| `error` | string | 仅旧路径兼容期返回，值同 `message` |

- FastAPI 参数校验失败统一转成 `400 invalid_argument`，`details.fields` 为 `{字段路径: 原因}`。
- MCP 传输层保留 MCP 自己的错误码和 JSON-RPC 信封（见 `M-05`），不受本节约束。

### 1.6 分页、筛选、排序

- 列表接口统一用游标分页：
  - 请求：`?cursor=<不透明串>&limit=<n>`，`limit` 默认 50，上限 200（详设 4.1，与 MCP 一致）；
  - 响应：`{items: [...], next_cursor: string|null, total: int|null}`。
  - 游标里绑定了筛选、排序条件和数据版本。条件变了再用旧游标返回 `400 invalid_cursor`；数据版本变了返回 `409 cursor_stale`，前端从头拉。
- 少数本身有上限、不分页的列表，接口里写明「不分页，上限 N」。
- 排序参数：`sort=<字段>&order=asc|desc`。
- 筛选参数：各接口逐个列出，多值用逗号分隔，如 `?kind=decision,stale`。

### 1.7 内容版本号与乐观并发

| 对象 | 读到的版本号 | 写入时带回 | 冲突时 |
|---|---|---|---|
| 工作区文件、Studio 条目、插件产物、知识条目 | `revision`：文件内容 `sha256` 的前 16 位 hex | `base_revision` | `409 revision_conflict` |
| 求解契约 | `revision`：契约目录、绑定操作、评估器源码的摘要；`revision_no` 是给人看的整数（界面显示 r8） | `base_revision` | `409 revision_conflict`，`details.kind="solver_contract"` |
| 蓝图、变更点清单、自主构建策略 | `revision` | `base_revision` | `409 revision_conflict` |
| 项目主线 | `head`：主线最新提交；`version_label`：如 `v0.13` | 变更集合入时可选带 `expected_head` | `409 head_moved` |

- `revision_conflict` 的 `details` 是 `{target, expected, actual, changed_by, changed_at, version_label}`。`changed_by` 取 `assistant|web|studio|mcp|autobuild|import|plugin`，前端据此显示「文件已被助手修改（2 分钟前，版本 v0.13）」。
- `base_revision` 为空串或省略时不做冲突检查。只有新建文件，或明确要覆盖时才这样传。
- **写入范围锁**（详设 4.3、方案 C10）：
  - 写到助手写入范围内的条目，所有写接口统一返回 `423 write_scope_locked`，`details` 为 `ScopeLock`；
  - 锁的状态有两态：`assistant_running`（助手运行中，到这一轮结束合入为止）、`pending_confirm`（F3 起只在这一轮和你的修改合入冲突时出现，到在待处理里选好为止；界面叫「有冲突」）。自主构建下是 `autobuild_slice`，持续到这一段自动合入；
  - 范围外照常写，保存即合入主线。
- 网页、Studio、MCP、导入的写入都经内核写入路径（⑤），成功响应带 `commit` 和 `version_label`。

### 1.8 请求体与上传上限

| 类别 | 上限 | 超限 |
|---|---|---|
| JSON 请求体（默认） | 1 MiB | `413 payload_too_large` |
| 文件内容写入（`F-03`、产物保存） | 4 MiB | `413` |
| 资料上传（`R-02`、`U-01` `purpose=material`） | 单个 20 MiB | `413` |
| 对话附件（`U-01` `purpose=attachment`） | 单个 20 MiB，一条消息最多 10 个 | `413` / `400 invalid_argument` |
| 项目导入包（`U-01` `purpose=project_import`） | 200 MiB | `413` |
| 插件离线包（`PL-13`） | 200 MiB | `413` |
| 普通用户的插件包（`PL-22`） | 20 MiB | `413` |
| 知识包对比输入（`PC-01`） | 50 MiB，解压后 200 MiB、2000 个文件 | `413` |
| MCP 请求体 | 1 MiB | `413 REQUEST_TOO_LARGE`（MCP 码） |
| 证据锚点批量解析 | 一次 100 个 | `400 invalid_argument` |

允许的资料类型：`.md .markdown .txt .doc .docx .xls .xlsx .xlsm .csv .pdf .drawio .html .htm .png .jpg .jpeg .webp`。图片只作为对话附件和资料附件。其他类型返回 `415 unsupported_media_type`（消息「不支持文件“X”的类型」），`details.allowed` 列出允许的扩展名；超限是 `413 payload_too_large`（消息「文件“X”超过 20 MB，无法上传」），文字与 develop 一致。资料进资料区的所有入口（U-01、R-02、新建项目、对话附件）在服务层再按同一份白名单与上限挡一次；前端按 C-01 的 `material_types`、`limits.material_upload_bytes` 先在本地挡，规则不另写一份。上传 `.doc`、`.xls` 照收，前端传完提示另存为 `.docx`、`.xlsx`（转不出文本，不能分析和预览）。

### 1.9 时间、数量与金额

- 时间一律 RFC 3339 UTC，毫秒精度，以 `Z` 结尾，如 `2026-11-01T08:52:00.000Z`。界面上的「5 分钟前」由前端换算。
- 时长用整数毫秒，字段名以 `_ms` 结尾；配置项里的秒数以 `_s` 结尾。
- token 数用整数。
- 金额用 `Money`：`{amount: number（两位小数）, currency: "CNY"|"USD"}`。币种取平台配置 `billing.currency`，默认 `CNY`。费用 = token × 单价表（方案 B14），没配单价的模型 `amount=null`。
- 百分比用 0～100 的数，字段名以 `_pct` 结尾；置信度用 0～1 的数。

### 1.10 ID 与引用格式

| 对象 | 格式 | 例子 |
|---|---|---|
| 项目 | `^[a-z0-9][a-z0-9-]{0,63}$` | `retail-restock-3f9a1c2d` |
| 对话 | 12 位 hex | `c7a1e2f03b44` |
| 本体条目 | `<kind>:<key>`，kind 取 `object\|operation\|rule\|metric\|script` | `object:hardness_limit` |
| 插件产物 | 类型 `<包>.<名>` + key | `trace.scenario` / `s-03` |
| 变更集 | `cs-<n>` | `cs-31` |
| 执行记录 | `ex-<n>` | `ex-0412` |
| 自主构建运行 | `run-<n>`，界面显示「运行 #n」 | `run-3` |
| 采集任务 | `job-<n>` | `job-0131` |
| 证据快照 | `YYYYMMDD-HHMMSS-xxxx` | `20261030-221500-9c1d` |
| 证据锚点 | `<scheme>:<源ID>/<路径>[#<片段>]`（详设 5.8.5，名字里的特殊字符百分号编码） | `db:mes-prod/mes.std_limit.limit_max` |
| 流程节点 | `<包>.<名>` | `core.model` |
| 待处理事项 | `<kind>:<来源ID>`，来源不变 ID 就不变 | `decision:q-7f3a` |
| 错误留档 | `err-<4～8 位 hex>` | `err-7f3a` |
| 凭据引用 | `cred://<源ID>/<版本>` | `cred://mes-prod/3` |

- 锚点放在查询参数里传（`?anchor=`），不放路径段，避免 `/` 和 `#` 被路由或浏览器截断。
- 条目和产物引用的统一写法见第 7 章 `EntryRef`、`ArtifactRef`。

### 1.11 SSE 事件流

**四条流。**

| 流 | 接口 | 谁开 | 内容 |
|---|---|---|---|
| 对话流 | `S-13 GET /api/v1/projects/{id}/conversations/{cid}/events` | 工作台对话页 | 消息、卡片、决策、确认、进展、会话状态（4.2） |
| 项目流 | `P-15 GET /api/v1/projects/{id}/events` | 进入任意项目页 | 流程、待处理、写入范围锁、变更集、产物、插件命令进度、采集、自主构建（4.3） |
| 用户流 | `A-12 GET /api/v1/me/events` | 全局页（我的项目、插件中心、个人设置） | 通知、项目卡片更新（4.4） |
| 日志跟随 | `O-07 GET /api/v1/ops/logs/stream` | 可观测页「跟随」 | 结构化日志（4.5） |

同一时刻前端最多开两条：项目页是「项目流 + 对话流」，全局页是「用户流」。每个用户同时最多 8 条连接，超过返回 `429 too_many_streams`。

**帧格式。**

```
retry: 3000

id: 1532
data: {"id":"1532","type":"question.asked","ts":"2026-11-01T07:21:03.120Z","project_id":"retail-restock-3f9a1c2d","conversation_id":"c7a1e2f03b44","data":{...}}

```

- 不写 `event:` 行，前端统一走 `onmessage`，按 `type` 分发。
- 信封字段：
  - `id`：同 SSE 的 id 行；
  - `type`：事件类型，见第 4 章；
  - `ts`：时间；
  - `project_id`；
  - `conversation_id`：只有对话流有；
  - `data`：载荷。
- 流开始时先发一次 `retry: 3000`。

**事件 id 与回放。**

| 流 | id 形式 | 持久化 | 断线重连 |
|---|---|---|---|
| 对话流 | 十进制整数 `seq`，每条对话单调递增，写进 JSONL 历史 | 是（瞬时事件除外） | 带 `Last-Event-ID` 头或 `?after=<seq>`，服务端补发 `seq > after` 的全部持久事件，再接实时推送 |
| 项目流、用户流 | `<epoch>.<n>`：`epoch` 是进程启动标识，`n` 单调递增 | 内存环形缓冲：每个项目（用户）最近 2000 条或 30 分钟 | 带 `Last-Event-ID` 或 `?last_event_id=`。缓冲里还有就补发；epoch 不同或已被挤出时，先发一条 `stream.reset`，前端据此重新拉快照接口 |

- **瞬时事件**不带 `id:` 行，不回放：`message.delta`、`context.usage`、`ping`、`command.progress` 的中间百分比。它们的最终状态由持久事件或快照接口给出。
- 对话流首次打开的推荐顺序：
  1. 先用 `S-06` 分页拉历史（默认最近 200 条）；
  2. 再带 `after=<最后一条 seq>` 打开流。
  - `after` 缺省为 0，表示从头回放全部历史。
- 项目流 `stream.reset` 之后要重拉的快照：`FL-01`、`IN-01`、`CS-06`、`PL-12`、`AB-05`（有运行时），以及当前页自己的列表接口。

**心跳与令牌过期。**

- 每 15 秒发一条 `{"type":"ping"}`（不带 id）。前端 45 秒收不到任何帧，就判定断线并重连。
- 服务端发现 access token 到期，先发 `auth.expired` 再关流。前端刷新令牌后，带 `?token=<新>&last_event_id=<最后 id>` 重连。
- 服务器重启后，对话流按持久 seq 续上；项目流和用户流会收到 `stream.reset`。

### 1.12 审计

- 业务面和运维管理面的每个写请求（POST、PUT、PATCH、DELETE）都在**响应之后**写一条审计（方案 E4）：`AuditEntry{ts, actor, actor_kind, type, method, path, status, project_id, summary, trace_id, ip}`。
- 读请求不记，例外是证据检索（E 组的 `EV-25`～`EV-29`）和下载发布包（`PB-07`、`X-03`），这两类按详设 4.10.1 记录。
- 另有几类不经 HTTP 的专项审计：agent 提交与合入、插件切换、采集访问、熔断等。事件类型见第 7 章 `AuditEntry.type`。
- 审计不带项目内容：只记路径、条目 key、动作和结果，不记文件正文、对话正文、字段值。
- 接口块里的「审计」一栏：
  - `是`：按本节默认规则记；
  - `专项：<type>`：额外写一条该类型的专项审计；
  - `否`：不记。

### 1.13 叫法

接口字段、文件路径、代码标识符沿用英文，界面与助手说的话用下面的叫法（F3，用户 11-03 定）：

| 字段 / 概念 | 界面叫法 |
|---|---|
| `slice`、`slice_id`、`slices`、`per_slice`、`slice_steps`、`slice_stages` | 段（分段、第 N 段、分段规划、分段建模）；不说「片」「切片」 |
| `changeset`（变更集） | 不出现在界面上：助手每一轮结束自动合入成一个版本，界面说「这一轮」；只有合入冲突时在待处理里出现 |

### 1.14 维护模式

运维开启维护模式后（`O-21`）：

| 模式 | 写请求 | 新会话 | 已有会话 |
|---|---|---|---|
| `reject_new_sessions` | 照常 | 发消息返回 `503 maintenance_no_new_sessions`，`details.message` 是运维填的提示 | 跑完当前这一轮 |
| `read_only` | 一律 `503 maintenance_read_only`，运维管理面和登录除外 | 同上 | 同上 |

`C-01` 返回当前的维护状态，前端据此显示横幅。

---

## 2 错误码目录

HTTP 状态码是这个错误码的默认值。少数错误码在不同接口里状态码不同，在接口块里单独写明。

### 2.1 通用

| code | 状态 | 含义 | details |
|---|---|---|---|
| `invalid_argument` | 400 | 参数或请求体不合法 | `fields: {路径: 原因}` |
| `invalid_cursor` | 400 | 游标无法解析，或筛选条件变了 | — |
| `cursor_stale` | 409 | 游标绑定的数据版本已变 | — |
| `unauthenticated` | 401 | 没带令牌或令牌无效 | — |
| `token_expired` | 401 | access token 过期 | — |
| `forbidden` | 403 | 角色不够 | `required_role` |
| `scope_forbidden` | 403 | 服务令牌权限范围不够 | `required_scope` |
| `project_owner_only` | 403 | 不是项目所有者 | — |
| `project_unowned` | 403 | 项目没有所有者，等管理员指定 | — |
| `not_found` | 404 | 路径不存在 | — |
| `project_not_found` | 404 | 项目不存在或 ID 不合法 | — |
| `endpoint_removed` | 410 | 旧接口已删除 | `successor` |
| `payload_too_large` | 413 | 请求体或文件超限 | `limit_bytes` |
| `unsupported_media_type` | 415 | 文件类型不支持 | `allowed[]` |
| `confirmation_required` | 428 | 需要先确认（生产采集、强确认操作） | 各接口自定 |
| `rate_limited` | 429 | 频率限制 | `retry_after_s` |
| `quota_exceeded` | 429 | 超出配额 | `quota, scope(user\|project), limit, used, reset_at` |
| `too_many_streams` | 429 | SSE 连接超限 | `limit` |
| `internal_error` | 500 | 未预期错误，原始错误已留档 | `error_id` |
| `upstream_unavailable` | 502 | auth-server、连接器网关、沙箱网关不可达 | `upstream` |
| `service_unavailable` | 503 | 依赖未就绪 | `dependency` |
| `maintenance_read_only` | 503 | 维护模式：只读 | `message, until` |
| `maintenance_no_new_sessions` | 503 | 维护模式：拒绝新会话 | `message, until` |
| `metadata_unavailable` | 503 | 项目元数据或资料用途清单暂时读不到（旧码 `project_metadata_unavailable`、`resource_metadata_unavailable`） | `which` |

### 2.2 认证与账号

| code | 状态 | 含义 | details |
|---|---|---|---|
| `invalid_credentials` | 401 | 用户名或密码不正确 | `remaining_attempts`（auth-server 提供时） |
| `account_locked` | 429 | 连续失败被锁定 | `retry_after_s` |
| `refresh_invalid` | 401 | 刷新令牌无效或过期 | — |
| `registration_disabled` | 403 | 注册未开启 | — |
| `username_taken` | 409 | 用户名已存在 | — |
| `weak_password` | 400 | 密码不满足规则 | `failed_rules[]`：取 `len, max, classes, contains_username, common, edge_space, same_as_old` |
| `old_password_incorrect` | 400 | 旧密码不正确 | — |
| `user_admin_unavailable` | 503 | 没配置 auth-server 管理令牌 | — |
| `user_not_found` | 404 | 用户不存在 | — |
| `transfer_target_invalid` | 400 | 移交对象不是建模者或不存在 | — |
| `invalid_owner` | 400 | 指定的所有者用户名不合法 | — |

### 2.3 项目、上传、文件

| code | 状态 | 含义 | details |
|---|---|---|---|
| `project_name_required` | 400 | 名称为空 | — |
| `project_name_too_long` | 400 | 名称超过 200 字符 | `max` |
| `project_name_conflict` | 409 | 同一用户下重名（含已归档） | `conflict: {original_name, attempted_name, candidates[≤3]}` |
| `confirm_name_mismatch` | 400 | 删除时输入的名称与项目名不一致 | — |
| `project_archived` | 409 | 已归档项目只读 | — |
| `project_busy` | 409 | 目录被占用，删除没完成 | — |
| `engine_unavailable` | 409 | 引擎在当前部署未安装 | `engine` |
| `archive_invalid` | 400 | 压缩包无法解析，或含符号链接、`..`、绝对路径 | `reason, entry` |
| `unsupported_archive_kind` | 400 | 认不出格式，或有阻断项 | `blockers[]: {code, message}` |
| `upload_not_found` | 404 | 暂存上传不存在 | — |
| `upload_expired` | 410 | 暂存上传已过期（24 小时） | — |
| `upload_purpose_mismatch` | 400 | 暂存上传的用途与接口不符 | `expected, actual` |
| `path_forbidden` | 403 | 路径越界，或是受保护目录（`.git`、`.claude`、`.onto`、`.ontology-state` 等） | `path` |
| `managed_path` | 403 | 受管目录，要用专用接口写（求解契约、脚本描述符、`resource/`） | `path, use` |
| `file_not_found` | 404 | 文件不存在 | `path` |
| `binary_not_previewable` | 415 | 二进制文件不能按文本预览 | — |
| `preview_unavailable` | 501 | 服务端缺预览依赖 | `reason` |
| `not_convertible` | 409 | 这份资料不能转成文本（本身是文本、流程图、没有能转的转换器） | `path` |
| `schema_selection_invalid` | 409 | 数据 Schema 的 Sheet 范围对不上当前文件（换过文件、所选 Sheet 没了或不能作表头、配置无效），或保存时带的 `file_sha256` 不是当前文件，要重新检查后确认（ADR-0096，同 develop） | — |
| `material_text_pending` | 409 | Excel 还没转出文本版，暂时检查不了 Sheet 范围 | `path` |
| `resource_source_modified` | 409 | 资料有还没保存为项目版本的改动，先重新上传或恢复再改设置（同 develop） | — |
| `invalid_filename` | 400 | 文件名不合法（含路径、以 `.` 开头） | — |
| `revision_conflict` | 409 | 版本号不一致 | `target, expected, actual, changed_by, changed_at, version_label, kind` |

### 2.4 写入路径、变更集、版本

| code | 状态 | 含义 | details |
|---|---|---|---|
| `write_scope_locked` | 423 | 目标在助手写入范围内：这一轮结束前只读，合入冲突时到选好保留哪边为止 | `ScopeLock` |
| `check_rejected` | 422 | 确定性检查有硬伤，拒绝保存 | `errors[]: Finding, warnings[]: Finding` |
| `gate_failed` | 422 | 发布门禁链不通过（F3b 起只在发布时出现：写入事务、产物编辑器、脚本节点遇到包声明的门禁都只提示，不回它） | `findings[]: Finding` |
| `changeset_not_found` | 404 | 变更集不存在 | — |
| `changeset_in_progress` | 409 | 助手还在跑，不能确认或撤回 | `conversation_id` |
| `changeset_closed` | 409 | 变更集已经合入或撤回 | `state` |
| `merge_conflict` | 409 | 合入时检测到实体冲突，已进待处理 | `entities[]: EntryRef, inbox_item_id` |
| `head_moved` | 409 | 主线已前进，`expected_head` 不符 | `head` |
| `studio_draft_pending` | 409 | Studio 有暂存草稿，版本回退前要先保存或放弃 | `path, entry_id, count` |
| `workspace_dirty` | 409 | 主线工作区有没提交的业务改动，不能回退 | `paths[]`（最多 20 个） |
| `invalid_commit` | 400 | 版本号不合法，或不在主线上 | — |
| `nothing_to_revert` | 400 | 当前内容与目标版本一致 | — |
| `tag_not_found` | 404 | 标记不存在，或不是用户标记 | — |
| `entry_referenced` | 409 | 条目被其他条目引用，不能删除（流程图除外） | `references: EntryRef[]` |

### 2.5 会话与执行

| code | 状态 | 含义 | details |
|---|---|---|---|
| `conversation_not_found` | 404 | 对话不存在或已删除 | — |
| `session_busy` | 409 | 助手正在运行（这条或另一条对话），或运行中不能切换、新建、删除 | `running_conversation_id`；AB-04 另带 `waiting`（那条对话停在没回答的问题上） |
| `message_empty` | 400 | 消息正文和附件都为空 | — |
| `engine_not_installed` | 503 | 引擎运行时不可用 | `engine` |
| `permission_request_gone` | 410 | 确认请求已处理、超时或会话已重启 | — |
| `question_gone` | 410 | 问题已回答、撤回或失效 | `state` |
| `answer_invalid` | 400 | 选项不存在，或要求自定义回答却为空 | — |
| `not_queued` | 409 | 当前不在排队，无法取消排队 | — |
| `next_occupied` | 409 | 运行中已经排着一条命令（F7，最多一条） | `label`；请求带 `replace: true` 时替换 |
| `message_duplicate` | 409 | 这一轮还没结束（在跑、排队、连接中、代存着命令），又提交了和上一条相同的消息或命令（develop !596） | — |
| `next_not_found` | 404 | 没有排着的命令 | — |
| `subagent_not_found` | 404 | 这条对话里没有这个子任务的记录 | — |
| `suggestion_stale` | 409 | 建议条发起的请求（带 `from_suggestion`）：看到按钮到点下去之间情况变了 | `reason`、`suggestions`（重算后的一份，界面直接换上） |
| `exec_not_found` | 404 | 执行记录不存在 | — |
| `exec_not_resumable` | 409 | 这一步已经做完了（F7 起能力升级、输入改过时 S-16 按新版重新执行，不再拒绝） | `reason`：`already_finished` |
| `nothing_to_retry` | 409 | 没有可重试的出错轮次 | — |
| `title_invalid` | 400 | 对话名称须为 1–60 个字符 | — |

### 2.6 流程与蓝图

| code | 状态 | 含义 | details |
|---|---|---|---|
| `node_not_found` | 404 | 节点不存在或未出现 | — |
| `node_not_skippable` | 409 | 主线或必经节点不能跳过 | — |
| `node_not_skipped` | 409 | 节点没有被跳过，无法恢复 | — |
| `flow_assembly_failed` | 422 | 流程组装不通过（锚点、环、输出冲突、版本） | `problems[]` |
| `blueprint_not_found` | 404 | 还没有建模蓝图 | — |
| `blueprint_not_confirmed` | 409 | 蓝图还没确认（F8 起不再返回：自主构建不要求先确认蓝图） | — |
| `blueprint_question_pending` | 409 | 资料和建模目标对不上的那个问题还没答，不能确认蓝图（F4b） | `question_id` |
| `slice_invalid` | 400 | 分段编辑不合法（合并少于 2 段、依赖成环、名称重复等） | `reason` |
| `low_confidence_batch` | 422 | 把握低的变更点不能批量接受，必须单独确认 | `point_ids` |

### 2.7 插件与产物

| code | 状态 | 含义 | details |
|---|---|---|---|
| `pack_not_found` | 404 | 包不存在 | — |
| `pack_version_not_found` | 404 | 版本不存在 | — |
| `pack_incompatible` | 409 | 兼容检查不通过 | `checks[]: CompatCheck` |
| `pack_required` | 409 | 必装包不能停用 | — |
| `pack_not_enabled` | 409 | 本项目没启用这个包 | — |
| `pack_already_enabled` | 409 | 已启用 | — |
| `pack_unavailable` | 409 | 离线部署不可用、本项目不适用，或平台未安装 | `reason`：`offline\|not_applicable\|not_installed` |
| `page_api_unsupported` | 409 | 平台页面版本不够 | `required, platform` |
| `assembly_failed` | 409 | 装配失败，项目保持原版本 | `error_id, reason` |
| `no_pending_switch` | 409 | 没有待生效的切换可撤销 | — |
| `pack_upload_invalid` | 400 | 不是 zip，或缺清单、校验和 | `reason` |
| `static_check_failed` | 422 | 有失败项，不能发布 | `failed[]` |
| `pack_upload_state` | 409 | 上传已发布或已退回 | `state` |
| `pack_name_taken` | 409 | 包名是公开的包或别人的私有包 | — |
| `pack_quota_exceeded` | 409 | 私有包版本数到上限 | `limit` |
| `artifact_type_unknown` | 404 | 产物类型没有登记，或登记它的包没启用 | — |
| `artifact_not_found` | 404 | 产物不存在 | — |
| `artifact_schema_invalid` | 422 | 不符合登记的格式定义 | `errors[]: {field, message}` |
| `artifact_readonly` | 403 | 只读产物（行业层知识、索引文件等） | `reason` |
| `format_migration_required` | 409 | 产物还是旧格式版本，要先迁移 | `from, to` |
| `knowledge_duplicate` | 409 | 入库查重发现同名或同范围条目 | `conflicts[]` |
| `nomination_not_found` | 404 | 提名不存在 | — |

### 2.8 证据源

| code | 状态 | 含义 | details |
|---|---|---|---|
| `source_not_found` | 404 | 数据源不存在 | — |
| `source_id_taken` | 409 | 数据源 ID 已被占用（ID 是锚点前缀，登记后不能改） | — |
| `source_not_authorized` | 403 | 本项目没获授权使用该数据源 | — |
| `provider_not_installed` | 409 | 提供方未安装 | `provider` |
| `gateway_unavailable` | 503 | 连接器网关不可达 | — |
| `connection_test_failed` | 422 | 测试连接或只读自检不通过 | `checks[]` |
| `outside_access_window` | 409 | 不在访问时间窗口内（任务会排队，只在要求立即执行时报） | `window` |
| `confirmation_expired` | 410 | 生产采集确认已过期（10 分钟） | — |
| `job_not_found` | 404 | 采集任务不存在 | — |
| `job_state_invalid` | 409 | 任务当前状态不允许该操作 | `state` |
| `snapshot_not_found` | 404 | 快照不存在 | — |
| `snapshot_referenced` | 409 | 快照被已确认产物引用，不能清理 | `references: int` |
| `anchor_invalid` | 400 | 锚点格式不合法 | — |
| `anchor_not_found` | 404 | 锚点在所声明的快照或资料里不存在 | — |
| `artifact_not_found` | 422 | 登记或采集引用的目标程序不在 blob 仓里（F13） | — |
| `upload_not_found` | 404 | 上传不存在、已过期或不是本人的（F13） | — |
| `upload_incomplete` | 409 | 还有块没传（F13） | `missing: int[]` |
| `upload_hash_mismatch` | 422 | 拼好的文件和声明的 sha256 不一致（F13） | — |
| `upload_too_large` | 413 | 超过单文件上限（F13） | `max_bytes` |

### 2.9 发布与三份契约

| code | 状态 | 含义 | details |
|---|---|---|---|
| `gate_blocked` | 422 | 门禁链有未通过项，不能发布；降级发布只放宽数据绑定 | `gates[]: GateResult` |
| `solver_contract_rejected` | 422 | 求解契约结构被拒 | `findings[]` |
| `solver_contract_invalid` | 400 | 求解契约语法错误 | `findings[]` |
| `release_not_found` | 404 | 发布记录不存在 | — |
| `receipt_revision_unknown` | 422 | 回执里的契约修订号在本项目不存在 | `revision` |
| `feedback_invalid` | 422 | 反馈载荷不符合反馈契约 | `errors[]` |
| `login_required` | 401 | 评测要统一账号登录（本地模式没有），文案「个人 Hub 包需要统一账号登录」；不是 `unauthenticated`，前端不当成登录过期 | — |
| `eval_unconfigured` | 503 | 没配评测服务地址或地址无效 | — |
| `eval_unauthorized` | 401 | 评测服务没接受当前登录 | — |
| `eval_not_found` | 404 | 评测记录不存在、无权访问，或评测的不是本项目的 Hub 包 | — |
| `eval_unavailable` | 503 | 评测服务连不上或出错 | — |
| `eval_invalid_response`、`eval_response_limit` | 502 | 评测服务响应格式错误或超过上限 | — |
| `eval_conflict` 与评测服务自己的 409 码（如 `run_active`） | 409 | 评测服务拒绝了请求；评测服务给的说明原样作 `message` | — |
| `artifacts_not_ready` | 409 | 评测报告还没生成 | — |
| `artifacts_changed` | 409 | 读产物期间评测服务开始重新生成 | — |
| `version_mismatch` | 409 | 评测的版本不是项目当前版本，不能评测优化 | — |
| `optimization_not_supported` | 409 | 求解验证的轮次不能评测优化 | — |
| `answer_not_found` | 404 | 报告附件里没有这条完整回答 | — |
| `note_required` | 422 | 标记处理失败没写原因 | — |
| `compare_cross_package` | 409 | 两侧是不同的知识包（manifest `id` 不同），没带 `allow_cross_package` | — |
| `compare_not_ready` | 409 | 对比还没完成 | — |
| `compare_input_in_use` | 409 | 对比输入还被对比任务用着 | — |
| `compare_request_conflict` | 409 | 同一 `request_id` 已绑定别的来源 | — |
| `compare_upload_in_progress` | 409 | 同一上传标识还在处理 | — |
| `compare_expired` | 410 | 对比结果、输入或冻结的来源已过期或被删 | — |
| `compare_cancelled` / `compare_upload_cancelled` | 410 | 对比任务、上传已取消 | — |
| `compare_timeout` / `compare_archive_timeout` | 408 | 处理或解析超时 | — |
| `compare_user_busy` / `compare_busy` / `compare_upload_busy` | 429 | 本人进行中的任务、全局队列、同时上传数满 | — |
| `compare_user_storage_full` / `compare_storage_full` | 429 / 507 | 本人、全局临时空间满 | — |
| `compare_service_unavailable` | 503 | 多进程部署时本进程没拿到对比服务锁 | — |

门禁链里各项的 `Finding.code`（不作为 HTTP 错误码出现）：

- 数据绑定：`declared_missing`、`no_columns`、`declared_unverifiable`；
- 求解契约：
  - `solver_contract_invalid`、`solver_contract_version_incompatible`、`solver_contract_reference_missing`、`solver_context_missing`、`solver_runtime_binding_forbidden`；
  - `solver_receipt_required`、`solver_receipt_stale`、`solver_receipt_failed`（旧码 `solver_contract_validation_required/_stale/_failed`）；
- 脚本：`script_binding_conflict`、`script_argument_shape`；
- 对象关系：`relation_structure_invalid`（缺端点、显式关联字段不在两端属性清单里；发布时同码 422）。发布检查每个问题一条
  Finding，带 `path`（对象文档）、`line`、`target`（对象），`message` 不含位置；两端各写一次的同一条关系归并，不算重复；
- 发布树：`release_tree_unsafe`、`release_tree_mismatch`；
- 证据：`anchor_missing`（ERROR）、`property_unevidenced`（WARNING）；
- 流程关卡：`node_blocked`（`details.node_id`）；
- 包声明的门禁：`<包>.<规则ID>`，例如 `trace.pass_rate`。

### 2.10 自主构建

| code | 状态 | 含义 | details |
|---|---|---|---|
| `autobuild_running` | 409 | 有运行在跑（运行中发消息、改引擎、再启动等） | `run_id` |
| `autobuild_not_running` | 409 | 运行不在可控制的状态 | `state` |
| `run_not_found` | 404 | 运行不存在 | — |
| `budget_exceeds_quota` | 422 | 预算超出平台上限或剩余配额 | `dimension, max` |
| `ledger_entry_not_found` | 404 | 台账条目不存在 | — |
| `suspension_not_found` | 404 | 挂起问题不存在或已回答 | — |
| `slice_not_revertible` | 409 | 这一段不能撤回（未合入、已撤回，或后续段依赖它的改动） | `reason` |
| `preview_expired` | 410 | 开始前预览已过期（10 分钟），要重新预览 | — |

### 2.11 运维

| code | 状态 | 含义 | details |
|---|---|---|---|
| `maintenance_active` | 409 | 已在维护模式 | — |
| `maintenance_not_active` | 409 | 不在维护模式 | — |
| `maintenance_step_blocked` | 409 | 当前步骤没完成，不能进入下一步 | `step, remaining` |
| `session_not_found` | 404 | 该项目没有在线会话 | — |
| `session_running` | 409 | 运行中的会话不能回收，只能强制断开 | — |
| `breaker_state_invalid` | 409 | 熔断状态不允许该操作 | `state` |
| `backup_in_progress` | 409 | 已有备份在跑 | — |
| `task_not_found` | 404 | 任务不存在 | — |
| `task_state_invalid` | 409 | 任务状态不允许该操作 | `state` |
| `diagnostics_not_found` | 404 | 诊断包不存在或已过期 | — |
| `trash_entry_not_found` | 404 | 回收站条目不存在 | — |
| `trace_not_found` | 404 | 链路不存在或已过保留期 | — |
| `error_record_not_found` | 404 | 错误留档不存在 | — |

### 2.12 MCP 传输层

MCP 传输接口（`M-05`～`M-07`）保留现有大写错误码和 JSON-RPC 信封，不改：

- 400：`INVALID_INPUT`、`INVALID_REQUEST`、`PARSE_ERROR`、`UNSUPPORTED_PROTOCOL`、`PROTOCOL_MISMATCH`、`CURSOR_CONTEXT_MISMATCH`、`INVALID_CURSOR`；
- 401：`MCP_AUTH_REQUIRED`、`MCP_AUTH_INVALID`；
- 403：`MCP_PROJECT_FORBIDDEN`、`MCP_SCOPE_FORBIDDEN`、`SESSION_PRINCIPAL_MISMATCH`；
- 404：`METHOD_NOT_FOUND`、`MCP_TOOL_NOT_FOUND`、`SESSION_NOT_FOUND`；
- 409：`REVISION_CONFLICT`、`CURSOR_REVISION_MISMATCH`、`RESOURCE_ALREADY_EXISTS`、`RESOURCE_NAME_CONFLICT`、`UNSAFE_PATH`、`PROJECT_EDITING_BUSY`；
- 413：`REQUEST_TOO_LARGE`；
- 422：`RESOURCE_NOT_FOUND`（另有 404 用法）、`RESOURCE_REFERENCED`、`NO_CHANGES`、`SUBMISSION_BLOCKED`、`AMBIGUOUS_SECTION`、`INVALID_METADATA`、`FIELD_DUPLICATE`、`RELATION_TARGET_INVALID`；
- 429：`SESSION_BUSY`、`SESSION_LIMIT`；
- 500：`MCP_INTERNAL_ERROR`。

`PROJECT_EDITING_BUSY` 的触发条件改为：目标条目在写入范围锁内（方案 C10），`details.entities` 列出锁定的条目。整项目忙的判断删除。

---
## 3 接口

### 3.0 分组与编号

| 前缀 | 分组 | 节 |
|---|---|---|
| `C-` | 客户端配置、健康检查、元模型、引擎 | 3.1 |
| `A-` | 认证与个人设置、通知 | 3.2 |
| `U-` | 暂存上传 | 3.3 |
| `P-` | 项目 | 3.4 |
| `F-` | 文件与检索 | 3.5 |
| `R-` | 资料与资料分析 | 3.6 |
| `ST-` | Studio 条目 | 3.7 |
| `K-` | 知识 | 3.8 |
| `V-` | 版本与标签 | 3.9 |
| `CS-` | 变更集与写入范围 | 3.10 |
| `S-` | 会话、对话、执行记录、命令 | 3.11 |
| `FL-`、`BP-` | 流程与统一下一步、建模蓝图 | 3.12 |
| `IN-` | 待处理 | 3.13 |
| `INC-` | 增量更新（变更点清单） | 3.14 |
| `RP-` | 报告 | 3.15 |
| `PL-` | 插件：目录、按项目启用、装配状态、上传审核 | 3.16 |
| `AR-` | 插件产物与通用视图 | 3.17 |
| `SC-`、`SV-` | 脚本、求解契约 | 3.18 |
| `EV-` | 证据源 | 3.19 |
| `PB-` | 发布 | 3.20 |
| `PC-` | 知识包版本对比 | 3.20 |
| `X-` | 三份契约（服务令牌） | 3.21 |
| `AB-` | 自主构建 | 3.22 |
| `M-` | MCP | 3.23 |
| `SY-`、`AD-` | 系统设置、管理员 | 3.24 |
| `O-` | 运维管理面 | 3.25 |

每个接口块的第一行是：方法与路径 · 调用方 · 旧路径 · 审计 · 依据 · 页面。

- 「调用方」里的 **所有者** 指 1.4 的项目所有者检查，**登录** 指任何已登录用户。
- 路径里的 `{id}` 一律指项目 ID。

---

### 3.1 客户端配置、健康检查、元模型、引擎

#### C-01 客户端配置

`GET /api/v1/client-config` · 免鉴权 · 旧：`GET /api/auth/status` · 审计：否 · 依据：①、4.10.1 · 页面：login、全部页面（启动时）

响应 `200`：

| 字段 | 类型 | 说明 |
|---|---|---|
| `api_version` | string | 固定 `"1"` |
| `server_version` | string | 内核版本，如 `2.0.0` |
| `page_api` | string | 前端页面接口版本，如 `1.0`；包声明的 `page_api` 据此校验 |
| `auth.enabled` | bool | false 即本地模式 |
| `auth.registration_enabled` | bool \| null | 不确定时为 null，前端照常显示注册页，由 `A-03` 的 `registration_disabled` 兜底 |
| `auth.local_user` | string \| null | 本地模式下的用户名，固定 `local` |
| `ops_base_url` | string | 运维管理面地址，空串表示同源 |
| `maintenance` | `MaintenanceState` \| null | 维护模式状态，供横幅使用 |
| `limits` | object | `{json_body_bytes, file_write_bytes, material_upload_bytes, attachment_bytes, attachments_per_message, project_import_bytes, pack_upload_bytes, pack_private_upload_bytes, page_size_max}`，数值同 1.8 |
| `material_types` | string[] | 允许的资料扩展名 |
| `features.drawio` | bool | drawio 静态页是否可用 |
| `billing.currency` | string | 金额币种 |

#### C-02 健康检查（旧）

`GET /api/health` · 免鉴权 · 旧：同路径，永久保留 · 审计：否 · 依据：4.10.2 · 页面：—

响应 `200`：`{ok: true, agent_sdk: bool, drawio: bool}`，与现状一致。

#### C-03 存活

`GET /api/health/live` · 免鉴权 · 旧：— · 审计：否 · 依据：4.10.2、方案 E7 · 页面：ops-ctl（显示）

- 响应 `200`：`{status: "ok", uptime_ms, started_at}`。
- 只要进程能响应就返回 200，不探测依赖。容器 HEALTHCHECK 用它。

#### C-04 就绪

`GET /api/health/ready` · 免鉴权 · 旧：— · 审计：否 · 依据：4.10.2、方案 E7 · 页面：ops-ctl（显示）

- 响应 `200`（全部就绪或降级）或 `503`（关键依赖不可用）：`{status: "ready"|"degraded"|"not_ready", checks: [{name, status: "ok"|"slow"|"warn"|"fail", latency_ms, detail}]}`。
- `name` 取 `model_gateway`、`auth_server`、`sandbox_gateway`、`connector_gateway`、`disk`、`git`、`assembly`。
- 匿名请求只返回 `status` 和每项的 `name`、`status`，不返回 `detail`。
- 启动恢复还没完成时返回 `503 not_ready`（方案 B8）。

#### C-05 元模型

`GET /api/v1/metamodel` · 登录 · 旧：— · 审计：否 · 依据：4.4、方案 C3 · 页面：studio、studio-table（表单与链接渲染）

响应 `200`：`Metamodel`，即实体类型、目录、只读别名、章节结构、链接语法、关系语法、文件名编码规则、属性表列定义（含「证据」列）。前端用它生成条目编辑表单、解析 `[[链接]]`。

#### C-06 可用引擎

`GET /api/v1/engines` · 登录 · 旧：`GET /api/projects/{id}/engine` 的 `available` 部分 · 审计：否 · 依据：4.2、5.7 · 页面：create、proj-settings

响应 `200`：`{items: [{id: "claude"|"jiuwen", channel: "stable"|"beta", label: "稳定引擎"|"内测引擎", installed: bool, unavailable_reason: string|null, resume: "native"|"replay"}], default: string}`。

- 界面只显示 `label`，不显示 `id`。
- `resume`：`native` 表示会话中断后能续上上下文；`replay` 表示回放历史续上。

---

### 3.2 认证与个人设置、通知

#### A-01 登录

`POST /api/v1/auth/login` · 免鉴权 · 旧：`POST /api/auth/login` · 审计：专项 `auth.login`（成功和失败都记，不记密码） · 依据：③ · 页面：login

- 请求：`{username: string 1–64, password: string 1–128}`。
- 响应 `200`：`{access_token, refresh_token, token_type: "Bearer", expires_in: int（秒）, user: UserProfile}`。
- 错误：`invalid_credentials`、`account_locked`、`rate_limited`、`upstream_unavailable`。

#### A-02 本地模式进入

`POST /api/v1/auth/local` · 免鉴权，只在 `auth.enabled=false` 时可用，否则 404 · 旧：— · 审计：否 · 依据：4.10.1 · 页面：login（本地模式「进入工作台」）

响应 `200`：`{user: UserProfile}`。前端此后不带令牌请求。

#### A-03 注册

`POST /api/v1/auth/register` · 免鉴权 · 旧：`POST /api/auth/register` · 审计：专项 `auth.register` · 依据：③ · 页面：login

- 请求：`{username: string 3–64，匹配 ^[A-Za-z0-9_.-]+$, password: string 8–128}`。邮箱由服务端合成，界面不收集（沿用现状）。
- 服务端校验密码规则：
  - 8–128 位；
  - 大写、小写、数字、符号至少三类；
  - 不含用户名（不区分大小写）；
  - 不在常见密码表里；
  - 首尾无空格。
- 响应 `201`：同 `A-01`（注册即登录）。
- 错误：`registration_disabled`、`username_taken`、`weak_password`、`rate_limited`。

#### A-04 刷新令牌

`POST /api/v1/auth/refresh` · 免鉴权 · 旧：`POST /api/auth/refresh` · 审计：否 · 依据：③ · 页面：全部（无感刷新）

- 请求：`{refresh_token}`。
- 响应 `200`：同 `A-01`。
- 错误：`refresh_invalid`。

#### A-05 退出登录

`POST /api/v1/auth/logout` · 登录 · 旧：— · 审计：专项 `auth.logout` · 依据：③ · 页面：me

- 请求：`{refresh_token?: string}`。
- 响应 `204`。服务端吊销刷新令牌（auth-server 支持时），并关闭该用户的 SSE 连接。

#### A-06 修改密码

`PUT /api/v1/me/password` · 登录（不能用于本地模式） · 旧：`POST /api/auth/password`（旧接口在请求体里带 `username`，v1 改为取令牌里的用户） · 审计：是 · 依据：③ · 页面：me

- 请求：`{old_password, new_password}`。新密码规则同 `A-03`，另加「不能与旧密码相同」。
- 响应 `204`。其他设备的刷新令牌全部作废。
- 错误：`old_password_incorrect`、`weak_password`、`user_admin_unavailable`、`rate_limited`。

#### A-07 我的资料

`GET /api/v1/me` · 登录 · 旧：— · 审计：否 · 依据：4.10.1 · 页面：me、所有页面顶栏头像

响应 `200`：`UserProfile` 加 `{last_login: {at, ip}|null, project_count, usage_this_month: {output_tokens, input_tokens, cost: Money}}`。

#### A-08 我的偏好

`GET /api/v1/me/preferences`、`PUT /api/v1/me/preferences` · 登录 · 旧：— · 审计：PUT 记 · 依据：—（设计稿 me） · 页面：me

- 读写同一个对象 `Preferences`：
  - `theme`：`light|dark`；
  - `notifications`：`{system: bool, l2_step_done: bool, l2_needs_input: bool, l2_error: bool, l4_finished: bool, l4_new_suspension: bool, l4_error_or_stopped: bool}`。
- 标题闪烁始终开启，不提供开关。
- PUT 是整体替换，响应 `200` 返回保存后的对象。

#### A-09 我的模型配置

`GET /api/v1/me/model-settings` · 登录 · 旧：`GET /api/settings/model/me` · 审计：否 · 依据：⑧、4.2 · 页面：me、create（显示「使用我的模型配置」）

响应 `200`：

| 字段 | 类型 | 说明 |
|---|---|---|
| `personal_allowed` | bool | 管理员是否允许个人 Key；false 时页面锁定 |
| `engines` | object | 键是引擎 id，值是 `PersonalEngineConfig` |
| `PersonalEngineConfig.source` | string | `deployment\|admin\|personal`：当前生效的来源（部署默认、管理员配置、个人 Key） |
| `PersonalEngineConfig.admin_summary` | object | `{gateway_host, model}`：管理员配置的摘要，不含 Key |
| `PersonalEngineConfig.base_url` | string | 个人网关地址，可为空 |
| `PersonalEngineConfig.model` | string | 个人模型，可为空 |
| `PersonalEngineConfig.api_key_set` | bool | 是否已设置个人 Key；Key 不回显 |
| `PersonalEngineConfig.context` | object | `{window, max_output, compact_at_pct}`：按推荐表带出的有效值 |

#### A-10 保存我的模型配置

`PUT /api/v1/me/model-settings/{engine}` · 登录 · 旧：`PUT /api/settings/model/me/{engine}` · 审计：是（只记来源切换，不记 Key） · 依据：⑧ · 页面：me

- 请求：`{source: "deployment"|"admin"|"personal", api_key?: string（留空表示不改）, base_url?: string（http/https）, model?: string}`。
- `source=personal` 而且以前没设过 Key 时，`api_key` 必填。
- 响应 `200`：`PersonalEngineConfig`，从该用户下一条消息生效。
- 错误：`forbidden`（管理员已关闭个人 Key）、`invalid_argument`。

#### A-11 清除我的模型配置

`DELETE /api/v1/me/model-settings/{engine}` · 登录 · 旧：`DELETE /api/settings/model/me/{engine}` · 审计：是 · 依据：⑧ · 页面：me

响应 `200`：`PersonalEngineConfig`（`source` 回到 `admin`）。

#### A-12 用户事件流

`GET /api/v1/me/events` · 登录（可用 `?token=`） · 旧：— · 审计：否 · 依据：①、8.5 · 页面：projects、plugins、plugin-detail、plugin-upload、me、sys、ops*

- SSE 用户流，事件见 4.4。
- 查询参数：`last_event_id?`。

#### A-13 通知列表

`GET /api/v1/me/notifications` · 登录 · 旧：— · 审计：否 · 依据：8.5 · 页面：projects（铃铛抽屉）

- 查询参数：`unread_only?: bool`；分页（1.6），默认 20 条。
- 响应：`{items: Notification[], next_cursor, unread_count}`。
- 通知保留 30 天。

#### A-14 标为已读

`POST /api/v1/me/notifications/read` · 登录 · 旧：— · 审计：否 · 依据：— · 页面：projects

- 请求：`{ids?: string[], all?: bool}`，二者必选其一。
- 响应：`{unread_count}`。

---

### 3.3 暂存上传

创建项目、导入项目、对话附件都要在项目确定之前或消息发出之前先传文件，所以单设暂存区。暂存文件 24 小时后过期。

#### U-01 上传到暂存区

`POST /api/v1/uploads`（multipart） · 登录 · 旧：— · 审计：是 · 依据：㉗ · 页面：create、import、wb-materials、chat-states（附件）、wb-l2

请求字段：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `file` | file | 是 | 单个文件 |
| `purpose` | string | 是 | `material\|attachment\|project_import` |
| `project_id` | string | `attachment` 时必填 | 附件归属的项目，用于所有权检查 |

- 上限：`material`、`attachment` 各 20 MiB，`project_import` 200 MiB。
- 类型：`material`、`attachment` 按 1.8 的白名单；`project_import` 只收 zip。
- 响应 `201`：

  | 字段 | 类型 | 说明 |
  |---|---|---|
  | `upload_id` | string | 暂存 ID |
  | `filename` | string | 原文件名 |
  | `size` | int | 字节数 |
  | `content_type` | string | — |
  | `sha256` | string | — |
  | `expires_at` | time | 过期时间 |
  | `detected` | object \| null | 仅 `material`：`{purpose: "goal"\|"schema"\|"term"\|"evidence"\|null, tables: string[]}`，按表格结构自动识别 |

- 错误：`payload_too_large`、`unsupported_media_type`、`invalid_filename`。

#### U-02 查看暂存上传

`GET /api/v1/uploads/{upload_id}` · 上传者本人 · 旧：— · 审计：否 · 依据：— · 页面：create、import

- 响应：同 `U-01`。
- 错误：`upload_not_found`、`upload_expired`。

#### U-03 删除暂存上传

`DELETE /api/v1/uploads/{upload_id}` · 上传者本人 · 旧：— · 审计：是 · 依据：— · 页面：create（文件行「移除」「全部移除」）、chat-states（附件 ×）

响应 `204`。

---

### 3.4 项目

#### P-01 列出项目

`GET /api/v1/projects` · 登录 · 旧：`GET /api/projects` · 审计：否 · 依据：①、1.6、6.7 · 页面：projects、ops（admin、ops 的元数据视图另见 `O-01`）

查询参数：

| 名 | 类型 | 说明 |
|---|---|---|
| `status` | string | `all`（默认，不含已归档）\|`active`\|`autobuild`\|`publishable`\|`archived` |
| `q` | string | 按名称和目标做子串匹配，不区分大小写 |
| `sort` | string | `recent`（默认，按最近活动）\|`name`\|`created` |
| `cursor`、`limit` | — | 分页，见 1.6 |

响应 `200`：

- `items: ProjectCard[]`：只含自己的项目。admin、ops 看别人的项目用 `O-01`。
- `next_cursor`。
- `counts: {all, active, autobuild, publishable, archived}`：筛选分段上的数字。

各状态的含义：

| status | 条件 |
|---|---|
| `active` | 未归档 |
| `autobuild` | 有运行中的自主构建 |
| `publishable` | 门禁链全部通过，且有未发布的主线版本 |

#### P-02 新建项目

`POST /api/v1/projects` · 登录 · 旧：`POST /api/projects` · 审计：是 · 依据：1.6、5.5、5.8、6.9 · 页面：create、projects（空状态「看示例项目」）

请求体：

| 字段 | 类型 | 必填 | 约束 / 说明 |
|---|---|---|---|
| `name` | string | 是 | 1–200 字符，去首尾空白；同一用户下唯一（含已归档） |
| `goal` | string | 是 | 一句话目标，1–2000 字符 |
| `starting_points` | string[] | 否 | 取 `upload\|connect_system\|industry_pack\|import_package\|example`，只用于欢迎卡的推荐顺序，不改变行为 |
| `materials` | `{upload_id, purpose?}`[] | 否 | 第 2 步上传的资料。`purpose` 取 `auto\|goal\|schema\|term\|evidence`，缺省 `auto` |
| `industry_packs` | `{name, version?}`[] | 否 | 行业包，可多选；不写版本时取 stable 通道 |
| `system_packs` | `{name, version?}`[] | 否 | 系统知识包 |
| `capabilities` | `{[pack_name]: bool}` | 否 | 可选能力包开关。缺省取推荐值；core 必装、忽略；离线部署不可用的包传 true 返回 `pack_unavailable` |
| `engine` | string | 否 | 引擎 id，缺省取系统默认引擎 |
| `build_mode` | string | 否 | `L2\|L4`，缺省 `L2` |
| `example` | `{pack?: string, id: string}` | 否 | 看示例项目：内置示例（`seed_example`）或行业包里的示例项目 |

- 知识包包名由服务端按名称生成（`package_name`），创建后不能改。
- 资料按 `materials` 落到 `resource/` 并提交，提交说明 `import: 新建项目资料`。
- 选中的包写进 `.onto/packs.lock`，作为首个系统提交（`Onto-Source: plugin`）。
- 响应 `201`：`ProjectMeta`，加 `landing_page: "wb-materials"`。
- 错误：
  - `project_name_required`、`project_name_too_long`、`project_name_conflict`；
  - `upload_not_found`、`upload_expired`、`upload_purpose_mismatch`；
  - `pack_not_found`、`pack_incompatible`、`pack_unavailable`；
  - `engine_unavailable`、`quota_exceeded`。

#### P-03 名称可用检查

`GET /api/v1/projects/name-check` · 登录 · 旧：— · 审计：否 · 依据：— · 页面：create、projects（重命名弹窗）、import

- 查询参数：`name`（必填）、`exclude_project_id?`（重命名时排除自己）。
- 响应 `200`：`{available: bool, normalized: string, package_name: string（建议包名）, conflict: {original_name, attempted_name, candidates[≤3]}|null, reason: "empty"|"too_long"|"conflict"|null}`。
- 比较规则：去首尾空白，NFKC 归一，不区分大小写，含已归档项目。

#### P-04 项目详情

`GET /api/v1/projects/{id}` · 所有者得 `ProjectMeta`；admin、ops 得 `ProjectMetaView` · 旧：`GET /api/projects/{id}` · 审计：否 · 依据：1.6、4.9 · 页面：全部项目页（外壳面包屑、知识包版本 chip）、proj-settings

- 响应 `200`：`ProjectMeta` 或 `ProjectMetaView`。
- 错误：`project_not_found`、`project_owner_only`、`project_unowned`、`metadata_unavailable`。

#### P-05 修改项目

`PATCH /api/v1/projects/{id}` · 所有者 · 旧：`PATCH /api/projects/{id}`（旧接口只能改名） · 审计：是 · 依据：4.9 · 页面：proj-settings、projects（重命名）、wb-materials（联网 / 仅本地）

- 请求（字段都可选）：`{name?: string, goal?: string, research_mode?: "web"|"local"}`。
- `research_mode=web` 要求「联网调研」包已启用，否则返回 `pack_not_enabled`。
- 改名不影响包名和版本历史。
- 元数据不进工作区 git（方案 A13），不产生版本。
- 响应 `200`：`ProjectMeta`。
- 错误：`project_name_required`、`project_name_too_long`、`project_name_conflict`、`pack_not_enabled`、`project_archived`。

#### P-06 删除项目

`DELETE /api/v1/projects/{id}` · 所有者 · 旧：`DELETE /api/projects/{id}` · 审计：是 · 依据：ADR 0010 · 页面：projects、proj-settings

- 请求体：`{confirm_name: string, delete_hub_package?: bool, hub_usage_confirmed?: bool}`，`confirm_name` 必须与项目名逐字相同。
  - `delete_hub_package=true`（develop !590，ADR-0098）：先物理删除项目发布的 Hub 包（整个本体的全部正式版本、评测快照与共享资料），
    要 `hub_usage_confirmed=true`（否则 `422 invalid_argument`「请确认消费和评测不再使用这些知识包」）且统一账号登录；
    远端失败项目保留、删除进度留着，再次删除可以重试（`HB-10` 看进度）。
- 效果：
  - 不论当前状态都直接删除；
  - 会话、排队消息、自主构建运行立即强制终止；
  - 工作区、对话、版本历史、证据快照永久清除，不进回收站；
  - 该项目的 MCP token 全部吊销；
  - 数据源登记和凭据不受影响。
- 响应 `202`：`{deleted: id}`。清扫在后台完成，进程中途重启会接着清。
- 错误：`confirm_name_mismatch`、`project_busy`；一并删除 Hub 包时另有 `invalid_argument`（未确认使用情况）、
  Hub 删除失败或结果待确认（503 / 502，项目尚未删除）、`409`（发布状态不明确、项目知识包关联不一致）。

#### P-07 归档 / 恢复

`POST /api/v1/projects/{id}/archive` · 所有者 · 旧：`POST /api/projects/{id}/archive` · 审计：是 · 依据：4.9 · 页面：projects、proj-settings

- 请求：`{archived: bool}`。
- 归档时：
  - 有运行中的自主构建返回 `autobuild_running`；
  - 在线会话关闭；
  - MCP change 工具集拒绝写入。
- 响应 `200`：`ProjectMeta`。

#### P-08 克隆项目

`POST /api/v1/projects/{id}/clone` · 所有者 · 旧：`POST /api/projects/{id}/clone` · 审计：是 · 依据：4.9 · 页面：projects

- 请求：

  | 字段 | 类型 | 说明 |
  |---|---|---|
  | `name` | string? | 缺省为「原名（副本）」 |
  | `include_history` | bool | 默认 true：带 `.git` 历史 |
  | `include_snapshots` | bool | 默认 true：带 `resource/systems/` |
  | `include_conversations` | bool | 默认 false：带对话记录 |

- 新项目只归调用者。
- 启用的能力和锁定版本与原项目一致。
- 数据源授权不复制，凭据不复制。
- 响应 `201`：`ProjectMeta`。
- 错误：`project_name_conflict`、`project_name_too_long`、`quota_exceeded`。

#### P-09 导出迁移包

`GET /api/v1/projects/{id}/transfer-package` · 所有者（可用 `?token=`） · 旧：`GET /api/projects/{id}/export?scope=workspace` · 审计：专项 `project.export` · 依据：4.1 文件契约 · 页面：projects（卡片菜单）、publish（导出迁移包）

- 响应 `200`：`application/zip`，文件名 `{name}-transfer.zip`。
- 包内：
  - 完整工作区，含 `.git` 版本历史；
  - 项目元数据；
  - 数据源的登记信息，不带凭据引用（详设 5.8.6）。
- 不含：`.ontology-state`、`.onto/run`、对话记录。

#### P-10 检查导入包

`POST /api/v1/projects/import/inspect` · 登录 · 旧：`POST /api/projects/import`（`dry_run=true`） · 审计：否 · 依据：4.1、ADR 0008 · 页面：import

- 请求：`{upload_id, name?: string}`。
- 响应 `200`：

  | 字段 | 类型 | 说明 |
  |---|---|---|
  | `kind` | string | `transfer\|published_package\|workspace_archive\|unknown`：迁移包、已发布知识包、工作区压缩包 |
  | `kind_label` | string | 界面叫法 |
  | `root_dir` | string | 压缩包根目录 |
  | `files` | int | 文件数 |
  | `total_size` | int | 字节数 |
  | `counts` | object | `{objects, operations, rules, metrics, scripts, solver_operations}` |
  | `package` | object \| null | `{name, version, schema_version}`（已发布知识包） |
  | `blockers` | `{code, message, entry?}`[] | `code` 取 `symlink\|path_traversal\|absolute_path\|unknown_format\|empty_archive\|too_large\|solver_contract_invalid\|no_objects` |
  | `warnings` | `{code, message}`[] | 不阻断导入的提醒 |
  | `suggested_name` | string | 默认取包里的名称 |
  | `name_check` | object | 同 `P-03` 的响应 |
  | `after_import` | object | `{capabilities: {name, version}[]（按包里记录自动匹配）, start_node: string \| null, start_title: string \| null, mark_needs_review: bool}`；起点按包里的交付物套主流程的完成判定推算，越过的上游节点不算（ADR-0084），迁移包为 `null` |
  | `evidence_sources` | `{id, display_name, needs_reauth: true}`[] | 包里登记的数据源 |

- 错误：`upload_not_found`、`upload_expired`、`upload_purpose_mismatch`、`archive_invalid`。

#### P-11 导入项目

`POST /api/v1/projects/import` · 登录 · 旧：`POST /api/projects/import`（工作区、知识包）、`POST /api/projects/transfer-import`（迁移包） · 审计：是 · 依据：4.1、ADR 0008、6.6（导入产物标需复核） · 页面：import

- 请求：`{upload_id, name: string, name_strategy?: "exact"|"suggested"（默认 exact）, build_mode?: "L2"|"L4"}`。
- 格式自动识别，规则同 `P-10`。
  - 迁移包：原样恢复。
  - 已发布知识包：反向转换，原包完整留底在 `.imported-package/`；导入的产物标「需复核」（来源 `import`）。
  - 工作区压缩包：按原样导入。
- 导入作为一个变更集校验通过后合入，可撤回（详设 8.2）。
- 响应 `201`：`ProjectMeta`，加 `landing_page: "wb-l2"`。
- 错误：
  - `unsupported_archive_kind`（带 `blockers`）、`archive_invalid`；
  - `project_name_conflict`（`name_strategy=exact` 时）、`project_name_too_long`；
  - `upload_expired`、`quota_exceeded`。

#### P-12 引擎

`GET /api/v1/projects/{id}/engine`、`PUT /api/v1/projects/{id}/engine` · 所有者 · 旧：`GET/PUT /api/projects/{id}/engine` · 审计：PUT 记 · 依据：4.2、5.7 · 页面：proj-settings

- GET 响应：`{engine: string, model_source: "personal"|"admin"|"deployment"}`。
- PUT 请求：`{engine}`。
  - 效果：关闭本项目全部会话，下一条消息用新引擎启动。
  - 响应：`{engine}`。
- 错误：`engine_unavailable`、`autobuild_running`。

#### P-13 构建方式

`GET /api/v1/projects/{id}/build-mode`、`PUT /api/v1/projects/{id}/build-mode` · 所有者 · 旧：— · 审计：PUT 记 · 依据：1.6#2、8.5 · 页面：proj-settings、wb-l2（输入框模式胶囊）、create

- GET 响应：`{build_mode: "L2"|"L4", autobuild_running: bool, run_id: string|null}`。
- PUT 请求：`{build_mode}`。
  - 只改默认方式：选 L4 后，确认建模蓝图时直接开始自主构建。
  - 已在跑的运行不受影响。
  - 下一条消息生效。
- 响应：GET 的形状。

#### P-14 助手可用的 MCP 服务器（项目级）

`GET /api/v1/projects/{id}/mcp-servers`、`PUT /api/v1/projects/{id}/mcp-servers` · 所有者 · 旧：`GET/PUT /api/projects/{id}/mcp` · 审计：PUT 记 · 依据：4.2 指纹 · 页面：proj-settings

- GET 响应：`{items: [{name, transport: "stdio"|"http", target: string（地址或命令）, origin: "builtin"|"global"|"pack", read_only: bool, status: "running"|"stopped"|"error", tools: string[], enabled: bool, enabled_globally: bool}]}`。
  - `origin=builtin` 的是内核内置的只读服务器：`evidence`（`evidence.search/show/where-used/lineage/diff`）、`flow`（`flow.get`）。
- PUT 请求：`{disabled: string[]}`，即本项目停用的服务器名，最多 64 个。
- 只影响新建的会话。
- 响应：GET 的形状。

#### P-15 项目事件流

`GET /api/v1/projects/{id}/events` · 所有者（可用 `?token=`） · 旧：— · 审计：否 · 依据：①、4.1（`flow.updated`、`inbox.updated`、`artifacts.updated`）、4.3 · 页面：全部项目页

- SSE 项目流，事件见 4.3。
- 查询参数：`last_event_id?`。

#### P-16 项目用量

`GET /api/v1/projects/{id}/usage` · 所有者 · 旧：`GET /api/projects/{id}/session/usage` · 审计：否 · 依据：4.2 · 页面：chat-states（第 10 项「项目累计」）

响应 `200`：`{calls, input_tokens, output_tokens, cost: Money, last_activity_at}`。统计口径含全部对话和已删除对话留存的用量。

---

### 3.5 文件与检索

工作区文件的通用读写。本体条目优先用 Studio 接口（3.7），资料优先用资料接口（3.6）。

#### F-01 文件树

`GET /api/v1/projects/{id}/files/tree` · 所有者 · 旧：`GET /api/projects/{id}/files` · 审计：否 · 依据：4.9 · 页面：—（设计稿删除了产物树；保留给 MCP 客户端和排障）

- 查询参数：`root?: string`，缺省为工作区根目录。
- 响应：`{tree: FileNode[]}`。
- 跳过 `.git`、`.ontology-state`、`.onto/run`、以 `.` 开头的条目和符号链接。
- 只读，不再有刷新索引、自动提交的副作用（方案 C6）。

#### F-02 读文本文件

`GET /api/v1/projects/{id}/files/content` · 所有者 · 旧：`GET /api/projects/{id}/file` · 审计：否 · 依据：4.9 · 页面：studio-table（编辑原文）、resources（文本预览）、versions

- 查询参数：`path`（必填）。
- 响应：

  | 字段 | 类型 | 说明 |
  |---|---|---|
  | `path` | string | — |
  | `content` | string | 最多 512 KiB |
  | `truncated` | bool | 超过 512 KiB 时为 true；此时前端只读，提示下载原文件 |
  | `size` | int | 全文件字节数 |
  | `revision` | string | 对全文件计算 |
  | `last_change` | object | `{commit, version_label, source, at}` |
  | `lock` | `ScopeLock` \| null | 写入范围锁 |

- 错误：`path_forbidden`、`file_not_found`、`binary_not_previewable`。

#### F-03 写文本文件

`PUT /api/v1/projects/{id}/files/content` · 所有者 · 旧：`PUT /api/projects/{id}/file` · 审计：是 · 依据：⑤、4.3 · 页面：studio-table（编辑原文）、resources（drawio 编辑保存）、versions（对比右侧保存）

- 请求：`{path, content: string（≤4 MiB）, base_revision: string, summary?: string（≤80，写进版本说明）}`。
- 经写入路径提交，trailer `Onto-Source: web`。
- 写 `.py` 脚本时同步脚本描述符。
- 响应 `200`：`{path, size, revision, commit, version_label}`。
- 错误：
  - `path_forbidden`、`managed_path`；
  - `revision_conflict`、`write_scope_locked`；
  - `check_rejected`：确定性检查硬伤；
  - `project_archived`、`payload_too_large`。

#### F-04 下载原文件

`GET /api/v1/projects/{id}/files/raw` · 所有者（可用 `?token=`） · 旧：`GET /api/projects/{id}/raw` · 审计：否 · 依据：4.9 · 页面：resources（下载原文件、HTML 报告预览、drawio）、studio-table（大文件）

- 查询参数：`path`（必填）、`download?: bool`（加 `Content-Disposition: attachment`）。
- 响应：原样字节。
- `.html`、`.htm` 一律带 `Content-Security-Policy: sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'` 和 `X-Content-Type-Options: nosniff`。前端放进 sandbox iframe 预览。
- 错误：`path_forbidden`、`file_not_found`。

#### F-05 表格预览

`GET /api/v1/projects/{id}/files/table-preview` · 所有者 · 旧：`GET /api/projects/{id}/excel` · 审计：否 · 依据：4.5 · 页面：resources

- 查询参数：`path`（必填）、`sheet?: string`（缺省返回全部 sheet，最多 20 个）。
- 支持 `.xlsx`、`.xlsm`、`.csv`。
- 响应：`{format: "xlsx"|"csv", sheets: [{name, rows: string[][], merges: {row, col, rowspan, colspan}[], total_rows, truncated: bool}], sheet_truncated: bool}`。
  - 每个 sheet 最多 500 行 × 60 列。
  - CSV 只有一个 sheet，首行作表头。
- 错误：`file_not_found`、`unsupported_media_type`、`preview_unavailable`。

#### F-06 读历史版本文件

`GET /api/v1/projects/{id}/files/at` · 所有者 · 旧：`GET /api/projects/{id}/file-at` · 审计：否 · 依据：4.9 · 页面：versions

- 查询参数：`commit`（hex 6–40 或 `HEAD~n`）、`path`。
- 响应：`{path, commit, version_label, content, truncated}`，上限 512 KiB。
- 错误：`invalid_commit`、`file_not_found`、`path_forbidden`。

#### F-07 全局搜索

`GET /api/v1/projects/{id}/search` · 所有者 · 旧：`GET /api/projects/{id}/search` · 审计：否 · 依据：5.9.5（插件产物进全局搜索）、features「全文搜索（⌘K）」 · 页面：全部项目页（⌘K 命令面板）

- 查询参数：`q`（必填，1–200）、`groups?: string`（`entries,materials,reports,knowledge,artifacts`，缺省全部）、`limit?`（每组上限，默认 10，最大 50）。
- 响应：`{q, groups: [{group, total, items: SearchHit[]}], term_summary: [{term, by_group: {entries, materials, reports, knowledge, artifacts}, referenced_by: int}]}`。
  - `term_summary` 用于「陈列上限 · 知识包 3 处、资料 2 处、报告 1 处」这类聚合行。
- 搜索范围：
  - 本体条目、资料（经文件提供方抽取的文本）、报告、知识条目、已登记的插件产物；
  - 不区分大小写。

#### F-08 引用查询

`GET /api/v1/projects/{id}/query` · 所有者 · 旧：`GET /api/projects/{id}/query` · 审计：否 · 依据：4.4 · 页面：—（MCP 与排障；Studio 用 `ST-04` 的 `backlinks`，链接检查用 `RP-06`）

- 查询参数：`kind`（`used_by|orphans|search|unused|dangling|knowledge`）、`term?`。
- 响应：`{markdown}`。
- 内核按锁定的契约库版本执行，不再运行工作区里的脚本（方案 D19）。

---

### 3.6 资料与资料分析

资料区对 agent 只读，只能由用户上传、替换、删除，每次都保存版本（详设 4.5、4.8）。`resource/systems/`（证据快照）不出现在资料接口里。

资料的文本版（F4a，ADR-0073）：PDF、Word、Excel 上传后在后台经连接器网关转成 Markdown，放在 `resource/text/<分组>/<原文件名>.md`（第一行记原文件修订与转换器），走写入路径提交（`Onto-Source: plugin`），不在资料列表里出现、不能单独删除或改用途；替换、删除原文件时同一个提交里删掉旧的文本版。状态见 7.4 `MaterialText`，变化经 EP-25 推送；md、txt、csv 本身就是文本。

#### R-01 资料列表

`GET /api/v1/projects/{id}/resources` · 所有者 · 旧：`GET /api/projects/{id}/resource-meta` + `GET /files` · 审计：否 · 依据：4.5、ADR 0017 · 页面：resources、wb-materials

- 响应 `200`：`{groups: [{key: "diagrams"|"excel"|"others", label, items: Material[]}], feedback_group: {enabled: bool, count: int}, purposes: PurposeOption[]}`。
  - `feedback_group` 对应资料区的「外部反馈」分组：反馈演进包启用时 `enabled=true`，条目用 `R-11` 取。
  - `purposes` 是 5 种用途：`goal` 建模目标依据、`schema` 数据 Schema、`term` 术语表、`evidence` 通用业务依据、`auto` 由助手判断，每项带 `{value, label, description}`。
- 不分页，单项目资料上限 500 份。

#### R-02 上传资料

`POST /api/v1/projects/{id}/resources` · 所有者 · 旧：`POST /api/projects/{id}/resources` · 审计：是 · 依据：㉗、4.5 · 页面：resources、wb-materials（上传卡、追加资料）、wb-l2（输入框拖入文件）

- 请求：multipart，或 JSON `{upload_id}`（先经 `U-01` 暂存）。

  | 字段 | 类型 | 必填 | 说明 |
  |---|---|---|---|
  | `file` / `upload_id` | — | 二选一 | 单个文件 ≤20 MiB，类型见 1.8 |
  | `subdir` | string | 否 | `diagrams\|excel\|others`，缺省按扩展名归类 |
  | `purpose` | string | 否 | 5 种用途之一，缺省 `auto` |
  | `reading_mode` | string | 否 | `on_demand\|full`，缺省 `on_demand` |
  | `is_revision` | bool | 否 | 「这批是修订 / 变更资料」 |
  | `batch_id` | string | 否 | 同一批多个文件共用；`is_revision=true` 时整批只发起一次增量更新 |

- 同名文件直接替换（`replaced=true`）。上传即经写入路径提交，`Onto-Source: web`。
- 上传立即返回，`material.text.state` 是 `converting`（网关没部署时 `waiting`）；转换在后台做，结果经 EP-25 推送。
- `is_revision=true` 时，整批上传完成后往工作台对话发一条增量更新命令（`core.update`），响应里带 `command_task`。
- 响应 `201`：`{material: Material, replaced: bool, commit, version_label, detected: {purpose, tables[]}|null, command_task: CommandTask|null}`。
- 错误：`payload_too_large`、`unsupported_media_type`、`invalid_filename`、`project_archived`、`upload_expired`。

#### R-03 改资料用途

`PATCH /api/v1/projects/{id}/resources/purpose` · 所有者 · 旧：`PATCH /api/projects/{id}/resource-meta` · 审计：是 · 依据：ADR 0017 · 页面：resources、wb-materials（资料地图改用途菜单）

- 请求：`{path, purpose, reading_mode?, schema_selection?}`。改用途会覆盖自动识别结果。
  - `schema_selection`（ADR-0096，同 develop）：`{mode: "auto"|"manual", included_sheets: string[], file_sha256}`，`file_sha256` 取自 R-16。
    只给 `purpose=schema` 的 xlsx、xlsm、csv，别的 `400`「仅数据 Schema 表格支持设置 Sheet 范围」；`file_sha256` 不是当前文件 `409`「文件已变化，请刷新 Sheet 范围后重试」；
    所选 Sheet 有表头错误（`severity=error`）时 `400`，消息逐行「Sheet「名」：问题」。
  - 标为 schema 后存下范围 `{version: 1, mode, included_sheets, reference_sheets, file_sha256}`：不带 `schema_selection` 时沿用原来的范围，对不上当前文件 `409 schema_selection_invalid`；
    没有范围时按结构算好存下（`auto`）。Excel 还没转出文本版时先只改用途，范围在文本版转出的那个提交里存下。改成别的用途时范围随之撤掉。
- 响应 `200`：`{material: Material, commit, version_label, reread_task: CommandTask|null}`。
  - 资料分析已经开始过时，服务端自动发一条「重读受影响部分」命令，在 `reread_task` 里返回。
- 错误：`file_not_found`、`path_forbidden`、`invalid_argument`、`schema_selection_invalid`、`resource_source_modified`。

#### R-04 删除资料

`DELETE /api/v1/projects/{id}/resources` · 所有者 · 旧：`DELETE /api/projects/{id}/resources` · 审计：是 · 依据：4.5 · 页面：resources

- 查询参数：`path`。
- 删除保存为新版本，可从版本历史恢复。引用它的出处变为「来源无法核验」，派生产物不自动删除。
- 响应 `200`：`{deleted: path, commit, version_label, broken_references: int}`。
- 错误：`file_not_found`、`path_forbidden`（含 `resource/materials.json`、`resource/systems/`、`resource/text/`）。

#### R-05 资料被谁引用

`GET /api/v1/projects/{id}/resources/references` · 所有者 · 旧：— · 审计：否 · 依据：5.8.5、4.4 · 页面：resources（「被引用」页签、删除弹窗）

- 查询参数：`path`。
- 响应：`{path, total, items: [{target: EntryRef|ArtifactRef, location: string（如「步骤 3」「依据」）, anchors: string[]（片段，如 p:9.2）}]}`，不分页，上限 500。

#### R-06 资料原文预览

`GET /api/v1/projects/{id}/resources/preview` · 所有者 · 旧：— · 审计：否 · 依据：5.8.3（文件提供方）、5.8.5 · 页面：resources（文档预览）、全部页面（锚点悬停的「看原文」）

- 查询参数：`path`、`around?: string`（锚点片段，如 `p:9.2`，定位到该段前后；行号范围 `L12-L15` 按第一行定位）、`limit?: int`（块数，默认 200，最大 1000）、`cursor?`。
- 文档类资料（docx、md、txt）经 `source_extract` 抽取成带锚点的块（PDF 的文字看文本版，R-13），片段格式沿用 `source_extract`：`L12`、`p:3`、`table:1/row:2/cell:3`。
  docx 的块来自多源连接器转出的带原件锚点的文本版（ADR-0093），还没有文本版时没有块。
- `path` 也可以是文本版（`resource/text/<分组>/<原文件名>.md`，规则出处与证据「查看原文」按它定位，ADR-0093）：块按行，锚点 `L<n>` 和 EV-29、R-15 同一套行号；
  第一行文件头不出块，页、工作表标记不单独成块，记在它后面每一块的 `location` 上（「第 3 页」「工作表 客户」）。
- 表格和 drawio 分别用 `F-05` 和 `F-04`。
- 响应：

  | 字段 | 类型 | 说明 |
  |---|---|---|
  | `path` | string | — |
  | `format` | string | — |
  | `coverage` | object | `{read_sections: string（如「第 1–14 章」）, read_pct, complete: bool}`：资料分析读到哪 |
  | `blocks` | `[{anchor, kind: "heading"\|"paragraph"\|"table"\|"list", level?, text, highlights?: [{start, end}], location?: string}]` | 带锚点的块；`location` 只在读文本版时有 |
  | `next_cursor` | string \| null | — |

#### R-07 资料地图

`GET /api/v1/projects/{id}/materials/map` · 所有者 · 旧：— · 审计：否 · 依据：第 9 章建议 5、ADR 0011 · 页面：wb-materials（资料地图卡与弹窗）、chat-states（产物卡）

- 响应 `200`：`MaterialMap`，即
  - 每份资料的用途、读取进度与状态、读到的位置、排队原因、矛盾提示、计数；
  - 总读取率；
  - 问题（资料矛盾、疑似缺失）；
  - 核验摘要；
  - 分析范围（联网 / 仅本地）。
- 增量变化经项目流 `materials.updated` 推送。

#### R-08 资料分析候选

`GET /api/v1/projects/{id}/materials/candidates` · 所有者 · 旧：— · 审计：否 · 依据：第 9 章 · 页面：wb-materials（候选实时流）

- 查询参数：`after?: string`（候选 ID，返回在它之后新增的）、`limit?`（默认 50）。
- 响应：`{total, items: [{id, kind: "object"|"rule"|"term", name, anchor, anchor_label, created_at}]}`。
- 新候选经项目流 `materials.candidate` 推送。

#### R-09 资料分析核验

`GET /api/v1/projects/{id}/materials/verification` · 所有者 · 旧：— · 审计：否 · 依据：ADR 0011（research_checker） · 页面：wb-materials（核验抽屉）、reports（资料分析报告）

- 响应：`{checked_at, total, passed, problems, items: [{subject: string, kind: "rule"|"term"|"object"|"item", anchor, anchor_label, status: "passed"|"unverifiable"|"pending", reason: string|null}]}`。
- F4c：`anchor_label` 是「资料名 · 位置」（页、工作表或小节标题；文本版映射回原资料名，不显示行号和扩展名）。
- 不分页，上限 1000。

#### R-10 把资料问题记为待答问题

`POST /api/v1/projects/{id}/materials/problems/{problem_id}/promote` · 所有者 · 旧：— · 审计：是 · 依据：第 9 章（资料矛盾与缺失转待答问题） · 页面：wb-materials

- 请求：`{as: "blueprint_question"|"ask_me"}`。
  - `blueprint_question`：「记为待答问题」，进建模蓝图的「待你回答」；
  - `ask_me`：「向我确认」，进待处理的决策。
- 响应 `200`：`{question_id, inbox_item_id|null}`。
- 错误：`not_found`、`write_scope_locked`。

#### R-11 外部反馈列表

`GET /api/v1/projects/{id}/feedback` · 所有者 · 旧：— · 审计：否 · 依据：㉔、5.3（反馈演进） · 页面：resources（外部反馈分组）、inbox（外部反馈详情）、incremental（外部反馈卡）

- 查询参数：`state?: "open"|"handled"`；分页。
- 响应：`{items: Feedback[], next_cursor}`。
- 反馈演进包没启用时返回空列表，并带 `plugin_disabled: true`。

#### R-12 标记外部反馈状态

`PATCH /api/v1/projects/{id}/feedback/{feedback_id}` · 所有者 · 旧：— · 审计：是 · 依据：㉔ · 页面：resources、inbox

- 请求：`{state: "open"|"handled"|"dismissed"}`。
- 响应：`Feedback`。
- 「交给反馈演进处理」「在对话里处理」用 `S-20` 发命令，不经这里。

#### R-13 查看文本

`GET /api/v1/projects/{id}/resources/text` · 所有者 · 旧：— · 审计：否 · 依据：ADR-0073 · 页面：resources（PDF 的预览、Word / Excel 的「文本」页签）

- 查询参数：`path`（原文件的路径，不收文本版本身）。
- 响应 `200`：`{path, text: MaterialText, markdown: string | null, truncated: bool}`。
  - `markdown` 是文本版去掉第一行文件头后的正文；位置标记是 HTML 注释 `<!-- 第 3 页 -->`、`<!-- 工作表：客户 -->`；没转好时为 null；
  - 一次最多 30 万字，超出时 `truncated=true`（全文在工作区里）。
- 错误：`file_not_found`、`path_forbidden`、`not_convertible`（流程图）。

#### R-14 重新转换

`POST /api/v1/projects/{id}/resources/convert` · 所有者 · 旧：— · 审计：是 · 依据：ADR-0073 · 页面：resources（「重新转换」「重试」）

- 请求：`{path}`。用当前默认的转换器再转一次（失败后的重试也是它），后台执行，状态经 EP-25 推送；转出来的文本变了就是一次资料变化（资料分析回执里记的文件修订对不上，退回待核验）。
- 响应 `202`：`{material: Material}`。
- 错误：`file_not_found`、`path_forbidden`、`not_convertible`（本身就是文本、流程图、没有能转这种文件的转换器）、`project_archived`。

#### R-15 选出处的位置

`GET /api/v1/projects/{id}/resources/positions` · 所有者 · 旧：— · 审计：否 · 依据：ADR-0076、5.8.5 · 页面：studio-table（属性表证据列「补出处」：先选资料、再选位置）

- 查询参数：`path`（原文件的路径，不收文本版本身）、`q?`（按原文和位置筛，不分大小写）、`limit?`（默认 200，最大 1000）、`cursor?`。
- 只读文本：md、txt、csv 读原文件，PDF、Word、Excel 读当前修订的文本版（R-13），主容器不解析原件。逐行列出，和 `source_extract` 的 `L<n>` 一一对应；文件头、位置标记、小节标题本身和表格分隔行不列。
- 响应 `200`：

  | 字段 | 类型 | 说明 |
  |---|---|---|
  | `path` | string | — |
  | `name` | string | 资料名（去扩展名） |
  | `readable` | bool | 还没有文本版（转换中、暂不支持、转换失败、原文件换了还没重转）时为 false，`items` 为空 |
  | `items` | `[{anchor, location, text}]` | `anchor` 是完整锚点，原样写进 Studio 表单的 `evidence_anchor`（如 `file:resource/text/others/合同.pdf.md#L40`），和助手写的一样；`location` 与 EV-29 对同一锚点给的 `location_label` 一致（「第 3 页」「工作表 客户」、小节标题，没有时空串）；`text` 是这一行的预览（表格行单元格用 ` · ` 隔开，最多 160 字） |
  | `next_cursor` | string \| null | — |

- 错误：`file_not_found`、`path_forbidden`、`not_convertible`（流程图）、`invalid_cursor`。

#### R-16 Sheet 范围预检

`GET /api/v1/projects/{id}/resources/schema-preview?path=&force=` · 所有者（项目未归档） · 旧：`POST /api/projects/{id}/resource-meta/schema-preview`（develop 新增，没有调用方，不映射） · 审计：否 · 依据：ADR 0096 · 页面：resources、wb-materials（改用途为数据 Schema、资料行「查看范围」里的重新检查）

按「标成数据 Schema」检查一份表格的每个 Sheet，不改用途；行为与 develop 的 schema-preview 一致。Excel 读网关转出的文本版与它的原件锚点（行号、合并区），CSV 直接读，不解析 Excel 原件（ADR-0076）。检查结果按文件 sha256 缓存，`force=true` 重扫。

- 响应 `200`：

  | 字段 | 类型 | 说明 |
  |---|---|---|
  | `version` / `mode` | `1` / `"auto"\|"manual"` | 当前范围：现算的，或用户确认过的 |
  | `file_sha256` | string | 文件 sha256，R-03 保存范围时带回 |
  | `included_sheets` / `reference_sheets` | string[] | 建议的数据表 / 只作参考的 Sheet；表头有错误的不进建议 |
  | `sheets` | `[{name, kind, headers, selectable, schema_selectable, issues: [{code, severity, message}]}]` | `kind` 是结构类别（`table`、`maybe`、`dictionary`、`term`、`document`、`meta`、`empty`）；`issues.code`：`empty_header`、`merged_header`、`blank_columns`、`duplicate_columns`（`error`，不能当数据表），`reference_sheet`、`single_column`、`non_identifier_header`（`warning`），消息文字同 develop |
  | `warnings` | string[] | 「没有选中数据 Sheet…」「未选中的 Sheet 仅作参考…」 |
  | `error` | string | 只在人工范围对不上当前文件时出现，此时其余字段是现算的结果 |

- 表头是每个 Sheet 的物理第一行；第一行为空、合并、缺列名、重名都是错误。有内容的最右一列按整张表算。
- 错误（文字同 develop）：`400`「只能检查资料区中存在的文件」「数据 Schema 支持 xlsx、xlsm 和 UTF-8 CSV。…」「文件无法解析为数据表：…」（含 CSV 不是 UTF-8、Excel 转换失败）；
  `409 material_text_pending`（Excel 还在转文本版）；`project_archived`。

#### R-17 原始资料分段预览

`GET /api/v1/projects/{id}/resources/source-preview` · 所有者 · 旧：`GET /api/projects/{id}/source-preview`（develop，无调用方，不改写） · 审计：否 · 依据：ADR-0093 · 页面：resources（资料芯片、「查看原文」带锚点跳来时的来源预览）

- 查询参数：`path`（资料区里的 md、txt、csv、json、yaml、docx）、`anchor?`（`L12`、`p:3`、`table:1/row:2/cell:3`）。
- 分段与锚点同工具箱 `source_extract`；资料区里的 Word 读多源连接器转出的带原件锚点的文本版（ADR-0076、ADR-0093），不解析原件。
- 只返回定位点附近的一个窗口：最多 300 段，定位点前留 60 段；单段正文最多 4000 字。
- 响应 `200`：`{status: "ok", anchor, anchor_found: bool|null（没给锚点时 null）, total, offset, has_more_before, has_more_after,
  segments: [{anchor, text, truncated}]}`；读不了（文件超过抽取上限、坏文件、还没有文本版）时
  `{status: "unavailable", reason, anchor, segments: []}`。
- 错误：`file_not_found`、`path_forbidden`、`unsupported_media_type`（415，xlsx 走 F-05，PDF、图片按原样打开 F-04）。

---

### 3.7 Studio 条目

Studio 管五元组本体条目（对象、操作、规则、指标、脚本），加上图示包启用时的「流程图」（插件产物 `diagram.flowchart`，见 9.2）。30 分钟租约取消，改为写入范围锁（详设 4.3）。

#### ST-01 知识包统计

`GET /api/v1/projects/{id}/studio/stats` · 所有者 · 旧：`GET /api/projects/{id}/metrics`（字段不同） · 审计：否 · 依据：features「知识包统计」 · 页面：studio（顶部统计条）、projects（卡片）

响应 `200`：`{objects, operations, rules, metrics, scripts, relations, flowcharts: int|null, scenarios: {total, passed, pass_rate_pct}|null, version_label, head}`。

- `flowcharts` 只在图示包启用时有值；
- `scenarios` 只在场景模拟包启用时有值，这时前端在统计条上加插件标记。

#### ST-02 关系图

`GET /api/v1/projects/{id}/studio/graph` · 所有者 · 旧：`GET /api/projects/{id}/studio/graph` · 审计：否 · 依据：4.4、R3-9 · 页面：studio

- 查询参数：`include_mentions?: bool`，默认 true。
- 响应：

  | 字段 | 类型 | 说明 |
  |---|---|---|
  | `head` | string | 主线提交 |
  | `nodes` | `[{id: EntryId, kind, key, label, description, status, locked: bool, slice_ids: string[]}]` | — |
  | `edges` | `[{id, source, target, kind: "declared"\|"mention", relation: string, label}]` | `declared` 是一级标题「关联对象」下的关系声明（实线）；`mention` 是正文里的提及（虚线） |
  | `clusters` | `[{id, label, slice_id, node_ids}]` | 按段分组的关系簇 |
  | `chains` | `{rule: EntryId[], metric: EntryId[]}` | 规则链、指标链高亮用 |

- `relation` 沿用现有取值：`object_relation\|calls_operation\|operation_input_object\|operation_output_object\|operation_support_object\|uses_rule\|uses_metric\|applies_to_object\|uses_object\|script_uses_object\|uses_script\|references`。
- 链接解析以统一的 `parse_links` 为准（方案 C4），对象名带 `#` 不再断边。

#### ST-03 条目表

`GET /api/v1/projects/{id}/studio/entries` · 所有者 · 旧：— · 审计：否 · 依据：4.4、5.9.5 · 页面：studio-table

- 查询参数：
  - `kind?`：`object|operation|rule|metric|script|flowchart`，多值逗号分隔；
  - `q?`：名称、Key 子串；
  - `status?`；
  - `sort?`：`name|refs_in|refs_out|updated`（`updated` 新的在前，取不到时间的排最后）；
  - 分页。
- 响应：`{items: EntryRow[], next_cursor, counts: {all, object, operation, rule, metric, script, flowchart|null}, kinds: [{kind, label, plugin: {name, label}|null}]}`。
  - `kinds` 里 `flowchart` 带插件标记（图示）。
  - `EntryRow.updated_at`（最后更新时间）：已提交的条目取最后一次改动它的提交时间，已写入工作区、未提交的取文件修改时间；纯移动或改名不算更新，
    Studio 草稿不算。git 不可用时为 null（ADR-0093）。

#### ST-04 条目详情

`GET /api/v1/projects/{id}/studio/entries/{entry_id}` · 所有者 · 旧：`GET /api/projects/{id}/studio/element?path=` · 审计：否 · 依据：4.4、5.8.5 · 页面：studio（节点侧栏）、studio-table（条目编辑器）

- 响应：`Entry`，即
  - 结构化字段、属性表（含证据列）、关系（声明 / 提及）；
  - 被谁引用、数据来源；
  - `revision`、`lock`、`draft`（本人未提交的草稿）、需复核标记。
  - 读之前先拿草稿对一下正式条目（develop !608）：草稿内容已和条目一样（复核时合进去了）就删掉草稿，`draft` 为 null；
    草稿基于的修订已不是条目当前修订时 `draft.state` 为 `stale`，`status` 不算 `editing`，编辑器不自动载入它。
- 错误：`not_found`、`path_forbidden`。
- 流程图条目返回 `410 endpoint_removed`，`details.successor` 指向 `AR-03`。

#### ST-05 操作流程面板

`GET /api/v1/projects/{id}/studio/entries/{entry_id}/flow` · 所有者 · 旧：`GET /api/projects/{id}/studio/elements/{element_id}/flow` · 审计：否 · 依据：4.4 · 页面：studio（操作流程面板）

- 只对 `operation:` 条目可用，其他返回 `invalid_argument`。
- 响应：`{operation: EntryRef, business_flow: {steps: [{label, owner_label, entry: EntryRef|null, hit: bool}], main_operation: EntryRef|null, sub_operations: int, referenced_by_scenarios: int}, steps: [{index, text_md}], diagrams: ArtifactRef[]}`。

#### ST-06 证据侧栏

`GET /api/v1/projects/{id}/studio/entries/{entry_id}/evidence` · 所有者 · 旧：— · 审计：专项 `evidence.query` · 依据：5.6（证据侧栏）、5.8.5 · 页面：studio（证据页签）、studio-table（属性表证据列悬停）

- 响应：`{snapshot_basis: [{source_id, snapshot_id, collected_at}], items: [{anchor, scheme: "file"|"db"|"code"|"api"|"ui"|"cli"|"log"|"bin", title, confidence: "declared"|"observed"|"inferred"|null, used_for: string[]（属性名）, resolved: AnchorResolution}]}`。
- 前端「查看原文」按 `scheme` 跳转：`db` 去 schema，`file` 去 resources。

#### ST-07 草稿

`GET`、`PUT`、`DELETE /api/v1/projects/{id}/studio/entries/{entry_id}/draft` · 所有者 · 旧：`GET/PUT/DELETE /api/projects/{id}/studio/draft` · 审计：PUT、DELETE 记 · 依据：4.3 · 页面：studio-table

- **GET** 响应：`{draft: Draft|null}`，读前同 ST-04 对一下正式条目。
- **PUT** 请求：`{base_revision, form?: EntryForm, content?: string}`。
  - `form` 是结构化编辑器的内容，由服务端按元模型渲染成 Markdown；`content` 是原文。二者必选其一。
  - 自动暂存时调用，节流由前端控制（建议 2 秒）。
  - 响应：`{saved_at, draft: Draft}`。
  - `base_revision` 已不是条目当前修订时拒存，回 `409 revision_conflict`，`details: {draft_state: "stale"}`（develop !608）：旧底稿不能盖掉新版本。
    基于当前修订再存会替换掉 stale 草稿。
- **DELETE**（放弃草稿）响应 `204`。
- 草稿存数据目录，不进 git；每人每条目一份。
- 写入范围锁住时，PUT 仍允许（「你的草稿会保留」），但 `ST-08` 提交会被拒。

#### ST-08 提交条目

`POST /api/v1/projects/{id}/studio/entries/{entry_id}/submit` · 所有者 · 旧：`POST /api/projects/{id}/studio/submit` · 审计：是 · 依据：4.3（确定性检查三路）、方案 C15 · 页面：studio-table

- 请求：`{summary?: string}`。提交的是当前草稿。
- 响应 `200`，按结果分三种：

  | `result` | 其他字段 | 含义 |
  |---|---|---|
  | `committed` | `entry: Entry, revision, commit, version_label, warnings: Finding[]` | 检查通过，直接保存为新版本（`Onto-Source: studio`），草稿删除；链接指向不存在的文档、没有主键这类质量问题只在 `warnings` 里提示（F3，`code=check_hint`），不再转助手复核 |
  | `rejected` | `errors: Finding[], warnings: Finding[]` | 解析不了的结构（缺必需章节、表头缺列），草稿保留 |
  | `needs_review` | `review_points: string[], conversation_id, command_task: CommandTask` | 超出确定性检查范围。草稿移进助手变更集副本，往工作台对话发一条复核命令，结果在对话里 |

- 错误：`not_found`（没有草稿）、`revision_conflict`（含草稿已 stale：`details.draft_state = "stale"`，检查与复核前就拒，不转助手）、`write_scope_locked`、`project_archived`。

#### ST-09 新建条目

`POST /api/v1/projects/{id}/studio/entries` · 所有者 · 旧：— · 审计：是 · 依据：4.4 · 页面：studio-table（「新建」）

- 请求：`{kind: "object"|"operation"|"rule"|"metric"|"flowchart", name: string, key?: string, from_template?: bool（默认 true）}`。
- `flowchart` 要求图示包已启用，创建的是 `diagram.flowchart` 产物。
- 响应 `201`：`Entry`，流程图返回 `Artifact`。
- 错误：`invalid_argument`、`pack_not_enabled`、`revision_conflict`（重名，`details.kind="name"`）。

#### ST-10 删除条目

`DELETE /api/v1/projects/{id}/studio/entries/{entry_id}` · 所有者 · 旧：— · 审计：是 · 依据：4.3 · 页面：studio-table（删除流程图）

- 请求：`{base_revision}`。
- 删除也保存为一个版本，可以回退。
- 被引用时返回 `409 entry_referenced`，`details.references` 是 `EntryRef[]`。流程图例外：引用它的操作会提示「流程图缺失」，不挡删除。
- 响应 `200`：`{commit, version_label}`。
- 错误：`write_scope_locked`、`revision_conflict`、`entry_referenced`。


---

### 3.8 知识

知识条目的存放和加载在 core（详设 5.3、5.5），知识沉淀页由知识沉淀包带来。没启用知识沉淀包时，读接口照常可用，`K-07`、`K-08` 返回 `pack_not_enabled`。

#### K-01 知识条目列表

`GET /api/v1/projects/{id}/knowledge` · 所有者 · 旧：`GET /api/projects/{id}/knowledge` · 审计：否 · 依据：5.5 · 页面：knowledge

- 查询参数：
  - `layer?`：`all|project|industry`，默认 all；core 通用守则不展示；
  - `type?`：`correction|convention|term|domain_rule`；
  - `q?`；分页。
- 响应：`{items: KnowledgeEntry[], next_cursor, counts: {all, project, industry}, layers: [{layer, label, count, packs?: string[], overridden: int}], loading_rule: string}`。
  - `loading_rule` 是给人看的加载规则说明。

#### K-02 知识条目详情

`GET /api/v1/projects/{id}/knowledge/{knowledge_id}` · 所有者 · 旧：— · 审计：否 · 依据：5.5 · 页面：knowledge

- 响应：`KnowledgeEntry`，加：
  - `background_md`、`rule_md`；
  - `source_quote: {text, conversation_id?, message_id?, at, by}|null`；
  - `overrides: {pack, entry_title, note}|null`；
  - `used_by: (EntryRef|ArtifactRef)[]`。

#### K-03 新增知识条目（确认入库）

`POST /api/v1/projects/{id}/knowledge` · 所有者 · 旧：`POST /api/projects/{id}/knowledge` · 审计：是 · 依据：5.5 · 页面：knowledge（新增弹窗）、chat-states（第 12 项「沉淀为业务知识」）

- 请求：

  | 字段 | 类型 | 必填 | 说明 |
  |---|---|---|---|
  | `title` | string | 是 | 1–60 字符 |
  | `type` | string | 是 | `correction\|convention\|term\|domain_rule` |
  | `scope` | object | 是 | `{kind: "global"}` 或 `{kind: "entries", entries: EntryRef[]}` |
  | `background_md` | string | 否 | — |
  | `rule_md` | string | 是 | — |
  | `source` | object | 否 | `{conversation_id, message_id}`，从消息旁沉淀时带上 |
  | `confidence` | string | 否 | `high\|medium\|low`，缺省 `medium` |
  | `base_revision` | string | 否 | — |

- 入库前做确定性查重：同名、同范围、失效引用。
- 需要语义判断的口径冲突，由知识沉淀包的命令发到对话复核，响应里带 `review_task`。
- 保存为新版本。
- 响应 `201`：`{entry: KnowledgeEntry, commit, version_label, conflicts: ConflictFinding[], review_task: CommandTask|null}`。
- 错误：`knowledge_duplicate`（同名同范围）、`invalid_argument`、`write_scope_locked`。

#### K-04 修改知识条目

`PUT /api/v1/projects/{id}/knowledge/{knowledge_id}` · 所有者 · 旧：— · 审计：是 · 依据：5.5 · 页面：knowledge（「编辑」）

- 请求：同 `K-03`，`base_revision` 必填。
- 行业层条目只读，返回 `artifact_readonly`；要覆盖就在项目层写一条同名条目。
- 响应：同 `K-03`。

#### K-05 删除知识条目

`DELETE /api/v1/projects/{id}/knowledge/{knowledge_id}` · 所有者 · 旧：`DELETE /api/projects/{id}/knowledge?path=` · 审计：是 · 依据：5.5 · 页面：knowledge

- 请求：`{base_revision}`。
- 删除保存为新版本。被覆盖的行业条目重新生效。
- 响应：`{commit, version_label, affected_artifacts: int, restored_industry_entry: string|null}`。

#### K-06 知识冲突检查

`GET /api/v1/projects/{id}/knowledge/conflicts`、`POST /api/v1/projects/{id}/knowledge/conflicts` · 所有者 · 旧：— · 审计：POST 记 · 依据：5.5 · 页面：knowledge（冲突卡、明细弹窗）、chat-states（第 12 项「入库前先查重」）

- GET 返回最近一次结果；POST 立即跑一次确定性检查。
- 响应：`{checked_at, duration_ms, total_entries, conflicts: [{kind: "scope_conflict"|"duplicate"|"broken_ref", project_entry, industry_entry?, note, effective: "project"}], consistent_with_materials: int, unsourced: int, duplicates: int, broken_refs: int}`。
- 「在对话里复核」用 `S-20` 发命令。

#### K-07 提名预览（脱敏检查）

`POST /api/v1/projects/{id}/knowledge/{knowledge_id}/nomination-preview` · 所有者 · 旧：— · 审计：否 · 依据：5.5（回流与脱敏） · 页面：knowledge（提名弹窗）

- 请求：`{industry_pack: string}`。
- 响应：`{sanitized_text, highlights: [{start, end}], checks: [{item: "customer_name"|"internal_id"|"amount"|"secret_or_pii", found: int, action: string}], pack: {name, label}}`。
- 错误：`pack_not_enabled`（知识沉淀包没启用，或项目没启用该行业包）。

#### K-08 提交提名

`POST /api/v1/projects/{id}/knowledge/{knowledge_id}/nominations` · 所有者 · 旧：— · 审计：是 · 依据：5.5 · 页面：knowledge

- 请求：`{industry_pack, sanitized_text, confirm_general: true}`。`confirm_general` 必须为 true。
- 响应 `201`：`{nomination_id, state: "pending_review"}`。
- 审核结果出现在待处理（kind `notification`），并发用户通知。
- 管理员审核接口见 `AD-09`、`AD-10`。

---

### 3.9 版本与标签

主线版本只计合入提交（`--first-parent`，方案 C14）。版本号 `n` 从 1 开始，界面显示 `version_label = "v0.{n}"`。

#### V-01 版本列表

`GET /api/v1/projects/{id}/versions` · 所有者 · 旧：`GET /api/projects/{id}/versions`（旧接口固定 50 条） · 审计：否 · 依据：4.3 trailer、4.9 · 页面：versions、publish（版本下拉）

- 查询参数：`source?`（`assistant|web|studio|mcp|autobuild|import|plugin|revert`，多值）、分页（默认 50）。
- 响应：`{items: Version[], next_cursor, total, head, current_version_label, release_error: string}`。
  - `Version.release` 取发布记录（`publish.releases`，版本号来自知识包 Hub）：同一提交发布过几次取最新的；记录没带提交时按发布时间归到当时的最新版本。没有发布记录时退回本地 `release/*` 标签。
  - 发布记录读不到（Hub 不可用等）时版本照列，`release_error` 是原因，否则为空串。

#### V-02 版本详情

`GET /api/v1/projects/{id}/versions/{commit}` · 所有者 · 旧：— · 审计：否 · 依据：4.3 · 页面：versions（「v0.11 改了什么」）、inbox（查看这一段的改动）、l4-run

- 响应：`Version`，加 `changes: [{target: EntryRef|ArtifactRef, change: "add"|"mod"|"del", detail: string, review: "needs_review"|"accepted"|null}], run: {run_id, run_no, slice_id}|null`。

#### V-03 某版本的文件

`GET /api/v1/projects/{id}/versions/{commit}/files` · 所有者（`download=1` 时可用 `?token=`） · 旧：— · 审计：否 · 依据：4.9 · 页面：versions（查看此版本的文件）

- 查询参数：`prefix?`（默认 `ontology/`）、`download?: bool`。
- `download=1` 时返回该版本 `ontology/` 的 zip，否则返回 `{items: [{path, size}]}`（不分页，上限 5000）。
- 单个文件内容用 `F-06`。

#### V-04 对比

`GET /api/v1/projects/{id}/versions/diff` · 所有者 · 旧：— · 审计：否 · 依据：4.9（MergeDiff） · 页面：versions

- 查询参数：`from`（commit）、`to`（commit 或 `WORKTREE` 表示当前文件）、`path?` 或 `entry?`（二选一）。
- 响应：`{from: {commit, version_label}, to: {commit|null, version_label|null}, path, left: string, right: string, right_revision: string|null, hunks: [{left_start, left_lines, right_start, right_lines}]}`。
- 右侧可编辑时，前端用 `right_revision` 作为 `F-03` 的 `base_revision`，保存后生成新版本。

#### V-05 回退预览

`GET /api/v1/projects/{id}/versions/revert-preview` · 所有者 · 旧：— · 审计：否 · 依据：4.9 · 页面：versions（回退确认弹窗）

- 查询参数：`commit`。
- 响应：`{target: {commit, version_label}, next_version_label, expected_head, changes: {added: string[], modified: string[], deleted: string[], total}, files: [{path, change: "restore"|"delete"}], undone_versions: string[], packs_lock_changes: [{pack, from, to}], ready: bool, blockers: [{code, message, details}]}`。
  - `changes` 是 develop 回退确认框用的计数口径：回退会新增（目标版本里有、现在没有）、修改、删除的业务文件。
  - 回退会把启用的能力和版本也一起恢复到当时状态，所以要列出 `packs_lock_changes`。
  - 只列业务内容（ADR-0094）：仓库配置（`.gitignore` 等）、项目元数据、运行状态、助手装配（`.claude`、`CLAUDE.md`、`README.md`）不随回退；目录 `index.md` 是派生文件，不单独列，回退时重新生成。资料（`resource/`）随回退恢复。
  - `expected_head` 是预览时的主线版本，回退时原样带回 `V-06`。
  - `blockers` 是现在不能回退的原因（`message` 照 develop 原文），`ready=false` 时不打开确认框，直接提示各原因；`code` 取 `write_scope_locked`（`details` 是 ScopeLock）、`session_busy`、`autobuild_running`（`details.run_id`）、`studio_draft_pending`（`details: {path, entry_id, count}`）、`workspace_dirty`（`details.paths`，最多 20 个）。
- 错误：`invalid_commit`（含不在当前版本历史里的提交）、`nothing_to_revert`。

#### V-06 回退

`POST /api/v1/projects/{id}/versions/revert` · 所有者 · 旧：`POST /api/projects/{id}/versions/revert` · 审计：是 · 依据：4.9、方案 C1 · 页面：versions

- 请求：`{commit, expected_head?}`。`expected_head` 取 `V-05` 的同名字段，预览之后主线前进了就 `409 head_moved`。
- 业务内容整份恢复，之后新增的文件也会删掉（方案 C1）；不随回退的内容同 `V-05`。生成新版本，历史保留。
- 能力切换下一条消息生效。回退之后助手会话回收、全部对话的续接点作废（ADR-0094）：对话历史保留，下一条消息全新开始。
- 回退之后删掉 `.ontology-state` 里的派生状态（验收回执、核验记录、进度等）；删不掉时响应带 `warning`。
- 主线工作区有没提交或暂存的业务改动、或暂存了不随回退的路径时拒绝（`workspace_dirty`）。
- 响应：`{commit, version_label, reverted_to: {commit, version_label}, changes: {added, modified, deleted, total}, warning?}`。
- 错误：`invalid_commit`、`nothing_to_revert`、`head_moved`，以及 `V-05` 的各种 `blockers`（取第一条）：`write_scope_locked`（423）、`session_busy`、`autobuild_running`、`studio_draft_pending`、`workspace_dirty`（均 409）。

#### V-07 标记版本

`POST /api/v1/projects/{id}/versions/tags` · 所有者 · 旧：`POST /api/projects/{id}/versions/tag` · 审计：是 · 依据：4.9 · 页面：versions

- 请求：`{commit, label: string 1–120}`。
- 响应 `201`：`{name: "mark-<epoch>-<hex4>", label, commit, version_label}`。

#### V-08 删除标记

`DELETE /api/v1/projects/{id}/versions/tags/{name}` · 所有者 · 旧：`DELETE /api/projects/{id}/versions/tag/{tag_name}` · 审计：是 · 依据：4.9 · 页面：versions

- 只能删用户标记（`mark-*`），发布标签 `release/*` 不能删。
- 响应 `204`。
- 错误：`tag_not_found`。

#### V-09 历史时间线

`GET /api/v1/projects/{id}/history` · 所有者 · 旧：`GET /api/projects/{id}/history`（develop，未映射） · 审计：否 · 依据：ADR-0094 · 页面：versions（左栏）

- 查询参数：`limit`（1–50，默认 50，越界 `400 invalid_argument`）、`cursor`（后端给的原样带回，格式不对 `400 invalid_cursor`）。
- 响应：`{items: HistoryEntry[], next_cursor: string|null, release_error: string}`。迭代与 Hub 发布版本按时间从新到旧合并，时间相同时发布版本在前；一页只覆盖两边都已读到的范围。
  - 迭代：`{kind: "iteration", version, commit, time（epoch 秒）, message, tags, current, can_revert}`，另带 V-01 的 `Version` 字段（`n`、`version_label`、`source`、`summary`、`run` 等）。`version` 就是主线版本号 N，界面写「迭代 N」。
  - 发布版本：`{kind: "release", release_id, ontology_id, version, package_digest, source_service, created_at, time}`，取自 Hub（HB-05 同源）。
- 没有统一账号登录（本地模式）时只列迭代。Hub 读不到时这一页照列迭代，`release_error` 是原因，之后的页不再读 Hub（游标里记 `off`）。

---

### 3.10 变更集与写入范围

依据详设 8.2、4.3、方案 C7～C12；F3（用户 11-03 定）修订：陪伴建模下一个变更集就是助手的一轮修改，这一轮结束（成功、出错、被打断）自动合入成一个版本，没有「确认生效」；下一轮开始前、服务启动时补合上一轮没走到收尾的；升级前留下的待确认变更集启动时自动合入。合入时按实体检测冲突，冲突的进待处理，这是用户唯一会看到变更集的情况。合入时的检查（规则检查、包声明的提交门禁）只记录、在卡片上提示，不挡合入，发布门禁链（PB-01）照拦。自主构建下一段一个变更集，自动合入。Studio、MCP、导入、插件变更各自的「一次保存」也是变更集，校验通过即合入，接口上不可见。

#### CS-01 变更集列表

`GET /api/v1/projects/{id}/changesets` · 所有者 · 旧：— · 审计：否 · 依据：8.2 · 页面：wb-l2（恢复页面时找未确认的变更集）、inbox

- 查询参数：`state?`（`open|pending_confirm|merged|discarded|conflict`，多值）、`source?`；分页。
- 响应：`{items: Changeset[], next_cursor}`。

#### CS-02 变更集详情

`GET /api/v1/projects/{id}/changesets/{cs}` · 所有者 · 旧：— · 审计：否 · 依据：8.2 · 页面：wb-l2、chat-states（第 8 项「本轮改了什么」）、incremental

- 响应：`Changeset`，加：
  - `groups: [{kind: "object"|"operation"|"rule"|"metric"|"script"|"scenario"|"artifact", items: [{target: EntryRef|ArtifactRef, change: "add"|"mod"|"del", detail, checks: "passed"|"warning"|"failed"}]}]`；
  - `checks: {errors, warnings, findings: Finding[]}`（只提示；指向蓝图里已规划、还没建的内容的链接不在这里，记在卡片的 `checks.pending`）。`findings` 里有两种来源：规则检查（`code` 是检查类别，如「链接失效」「结构缺失」）与包声明的提交门禁（`code` 是门禁 id，另带 `label` 门禁名，F3b）；`round_changes` 卡的 `hints` 由它们算出；
  - `next_version_label`。
- 用户在 Studio 或文件里另外保存的改动不算在这里（方案 C12）。

#### CS-03 变更集实体差异

`GET /api/v1/projects/{id}/changesets/{cs}/diff` · 所有者 · 旧：— · 审计：否 · 依据：4.4（实体级差异）、方案 C9 · 页面：wb-l2（「查看改动」）、chat-states（「对比」）、incremental

- 查询参数：`target?`（`EntryId` 或 `<artifact_type>/<key>`）。不传时返回全部。
- 响应：`{items: [{target, change, sections: [{title, lines: DiffLine[]}]}]}`。
  - `DiffLine` 是 `{kind: "header"|"del"|"add"|"ctx"|"note", text}`。
  - 界面按条目分段，段首用 `target.label`；`label` 为空的（认不出的文件）合成最后一段「另有 N 处改动」（F4b-paths）。
  - 渲染时去掉 `[[ ]]` 由前端做。

#### CS-04 合入（F3 起界面不再调）

`POST /api/v1/projects/{id}/changesets/{cs}/merge` · 所有者 · 旧：— · 审计：专项 `changeset.merge` · 依据：8.2、14#7 · 页面：—（F3：一轮结束由会话宿主自动合入；保留给升级前遗留的待确认变更集）

- 请求：`{expected_head?: string}`。
- 合入步骤（自动合入同一套）：
  1. 叠到最新主线上重新校验；
  2. 按实体检测冲突；
  3. 跑检查（规则检查、包声明的提交门禁），只记进变更集的 `checks`，不挡合入（F3）；
  4. 写 squash 提交，trailer 记 `Onto-Changeset`、`Onto-Source`、构建方式、包版本。
- 响应 `200`：`{state: "merged", commit, version_label, merged_at}`。同时：
  - 写入范围锁解除（`scope.updated`）；
  - 节点状态重算（`flow.updated`）；
  - 对话里出一条 `notice`。
- 错误：
  - `changeset_in_progress`：助手还在跑，先打断或等它停；
  - `changeset_closed`；
  - `merge_conflict`：已进待处理，由用户选保留哪一边；
  - `head_moved`。
  - F3 起不再回 `gate_failed`。

#### CS-05 撤回这一轮

`POST /api/v1/projects/{id}/changesets/{cs}/discard` · 所有者 · 旧：— · 审计：专项 `changeset.revert`（合入过的）/ `changeset.discard`（冲突中的） · 依据：8.2；F3 · 页面：wb-l2、chat-states、incremental、inbox（冲突项）

- 请求：`{}`。
- 合入过的（一轮结束已存成版本）：在主线上写这一轮的反向提交，回到这一轮之前的内容，另存一个版本；之后在别处的修改保留。按段建模的一轮记进蓝图这一段的新条目一起去掉，蓝图别的修改不动；这一轮记过的段、步骤作废，按主线现状重算。
- 冲突中还没合入的：丢弃整个变更集，主线不受影响，助手副本重置，写入范围锁解除。
- 响应：合入过的 `{state: "reverted", commit, version_label}`；冲突中的 `{state: "discarded", commit: null, version_label: null}`。对话里这一轮的卡片原地更新为已撤回，加一条 `notice`。
- 错误：
  - `merge_conflict`：这一轮之后又改过同一条，撤回会连同后来的修改一起撤掉，不撤；`message` 写出是哪几条，`details.entities` 列出；
  - `changeset_in_progress`：助手还在跑；
  - `changeset_closed`：已经撤回过。

#### CS-06 写入范围锁

`GET /api/v1/projects/{id}/scope` · 所有者 · 旧：Studio 租约 `POST/DELETE /api/projects/{id}/studio/lease`（删除） · 审计：否 · 依据：4.3、㊹、方案 C10 · 页面：studio、studio-table、flowchart、scenarios、scripts、knowledge、l4-run

- 查询参数：`target?`（单个 `EntryId` 或 `<artifact_type>/<key>`，只问它锁没锁）。
- 响应：`ScopeLock`。没锁时 `state="none"`、`entries=[]`。
- 变化经项目流 `scope.updated` 推送。

#### CS-07 解决合入冲突

`POST /api/v1/projects/{id}/changesets/{cs}/conflicts/resolve` · 所有者 · 旧：— · 审计：是 · 依据：8.2（守卫失效时的冲突兜底） · 页面：inbox（kind `changeset_conflict`）

- 请求：`{choices: [{target: EntryId|string, keep: "mainline"|"changeset"}]}`。
- 全部选完后自动重试合入。
- 响应：同 `CS-04`。

---

### 3.11 会话、对话、执行记录、命令

依据详设 4.2、8.4、方案 B1～B7。

- 一个项目可以有多条对话，但同一时刻只有一个在线引擎（14#14）。
- 对话流事件见 4.2。
- 下面的 `{cid}` 是对话 ID。

#### S-01 对话列表

`GET /api/v1/projects/{id}/conversations` · 所有者 · 旧：`GET /api/projects/{id}/session/conversations` · 审计：否 · 依据：4.2、ADR 0013 · 页面：chat-states（第 1 项会话切换菜单）、wb-l2 等全部工作台页（顶栏会话名）

- 响应：`{items: Conversation[], live: {conversation_id, state, busy: bool}|null, pool: PoolStatus}`。
- 不分页，单项目上限 200 条。
- 按最近更新排序，至少有一条。

#### S-02 新建对话

`POST /api/v1/projects/{id}/conversations` · 所有者 · 旧：`POST /api/projects/{id}/session/conversations` · 审计：是 · 依据：ADR 0013 · 页面：chat-states

- 请求：`{title?: string}`。
- 响应 `201`：`Conversation`。发第一条消息时引擎才换绑过来。
- 错误：`session_busy`（当前有对话在运行）。

#### S-03 重命名对话

`PATCH /api/v1/projects/{id}/conversations/{cid}` · 所有者 · 旧：`PATCH /api/projects/{id}/session/conversations/{cid}` · 审计：是 · 依据：ADR 0013 · 页面：chat-states

- 请求：`{title: string 1–60}`。
- 响应：`Conversation`。
- 错误：`title_invalid`、`conversation_not_found`、`session_busy`。

#### S-04 删除对话

`DELETE /api/v1/projects/{id}/conversations/{cid}` · 所有者 · 旧：`DELETE /api/projects/{id}/session/conversations/{cid}` · 审计：是 · 依据：ADR 0013 · 页面：chat-states

- 对话记录与模型上下文一并删除，不可恢复。累计用量照常计入项目。
- 响应：`{items: Conversation[]}`，即剩余对话。
- 错误：`session_busy`、`conversation_not_found`。

#### S-05 对话状态

`GET /api/v1/projects/{id}/conversations/{cid}` · 所有者 · 旧：`GET /api/projects/{id}/session?conversation=` · 审计：否 · 依据：4.2 · 页面：全部工作台页

- 响应：`ConversationState`，包括：
  - `state`、`queue_position`、`turn_started_at`、`last_seq`；
  - `context: ContextUsage`；
  - `pending_permission`、`open_questions[]`；
  - `active_exec: ExecRecord|null`、`open_changeset: Changeset|null`；
  - `config_pending: ConfigPending|null`；
  - `autobuild_run_id`；
  - `next: NextCommand|null`：运行中发的命令由平台代存，这一轮结束后开始（F7，见 S-07）。
    `NextCommand = {id, label（业务名）, command, text, origin, created_at, paused: bool}`；`paused` 为打断或出错后暂停，不自动开始。

#### S-06 对话历史

`GET /api/v1/projects/{id}/conversations/{cid}/messages` · 所有者 · 旧：`GET /api/projects/{id}/session` + 流回放 · 审计：否 · 依据：4.2 · 页面：全部工作台页

- 查询参数：`before_seq?`（取这条之前的）、`limit?`（默认 200，最大 500）。
- 响应：`{items: StreamEvent[]（按 seq 升序）, has_more: bool, first_seq, last_seq}`。
  - `StreamEvent` 就是对话流里的持久事件信封（1.11），不含瞬时事件。
  - 卡片和问题以最新状态返回：同一 `card_id` 只保留最后一个版本，原位置不变。

#### S-07 发消息

`POST /api/v1/projects/{id}/conversations/{cid}/messages` · 所有者 · 旧：`POST /api/projects/{id}/session/messages` · 审计：是（不记正文） · 依据：④、4.2 · 页面：全部工作台页、studio（问助手）、studio-table（问助手）

请求：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `text` | string | 有附件时可空 | ≤ 32,000 字符 |
| `attachments` | string[] | 否 | `U-01` 的 `upload_id`，最多 10 个。作为资料附件：确认使用时落到 `resource/` |
| `references` | `(EntryRef\|ArtifactRef\|{kind: "anchor", anchor})[]` | 否 | 「问助手」带进来的条目引用，最多 20 个 |
| `client_message_id` | string | 否 | 幂等键，同一键重复提交只生效一次 |
| `replace` | bool | 否 | 运行中发命令、已经排着一条时替换它（F7） |
| `from_suggestion` | bool | 否 | 建议条发起的（F7）：先按建议条同一份检查校验，不能执行时回 `suggestion_stale` |

响应 `202`：`{message_id: string|null, seq, conversation_id, state, delivery: "immediate"|"steer"|"next"|"deferred"|"queued", queue_position: int|null, next: NextCommand|null}`。

- `steer`（F7）：助手正在跑这一轮，发的是文字：插进这一轮，主助手下一次调用模型之前看到。送到哪了看 `message.delivery`（EC-15）。
  主助手在等子任务时插不进去，等子任务结束；这一轮里一直没被看到的，这一轮结束后作为下一轮开始（`new_turn`）。
- `next`（F7）：助手正在跑这一轮，发的是命令（正文以 `/` 开头）：平台代存，这一轮结束后在同一步里开始，中间不报空闲、不推建议条。
  这时还没有用户气泡，`message_id` 为 null，开始时才写 `message.user`（`delivery="immediate"`，带原来的 `origin`）。最多存一条，已有时回 `next_occupied`。
- `deferred`：自主构建运行对话里的补充说明，下一步开始前带给助手（不打断运行）。
- `queued`：会话池满，排队。

错误：

- `message_empty`、`session_busy`（另一条对话正在运行）、`next_occupied`、`message_duplicate`（连续提交同一条，见 2.5）、`suggestion_stale`；
- `engine_not_installed`、`quota_exceeded`；
- `maintenance_no_new_sessions`、`project_archived`；
- `upload_expired`。

#### S-08 打断

`POST /api/v1/projects/{id}/conversations/{cid}/interrupt` · 所有者 · 旧：`POST /api/projects/{id}/session/interrupt` · 审计：是 · 依据：④、8.4 · 页面：chat-states（第 3 项「打断」）、wb-materials（「打断」）、flowchart（锁定条「回到对话里打断它」）

- 响应：`{interrupted: bool, exec: ExecRecord|null, returned: {message_id, text}[]}`。
- 已保存的内容不会丢。执行记录标为「在某阶段中断」，对话流发 `session.interrupted`。
- 还没被助手看到的插话一律撤回（引擎侧也撤回，打断后不会自己再跑起来），列在 `returned` 里，界面放回输入框；
  每条发 `message.delivery`（`state="returned"`）。代存的命令保留、`paused=true`，不自动开始（F7）。
- 自主构建运行中不能用它，要用 `AB-06` 暂停，否则返回 `autobuild_running`。

#### S-09 重启对话

`POST /api/v1/projects/{id}/conversations/{cid}/restart` · 所有者 · 旧：`POST /api/projects/{id}/session/close` · 审计：是 · 依据：4.2 · 页面：chat-states（第 11 项）

- 对话记录保留，丢弃模型上下文。下次发消息按最新配置重连。稳定引擎会清空模型上下文。
- 响应：`{state: "closed"}`。
- 错误：`session_busy`（运行中，先打断）。

#### S-10 回复确认卡

`POST /api/v1/projects/{id}/conversations/{cid}/permissions/{request_id}` · 所有者 · 旧：`POST /api/projects/{id}/session/permissions/{request_id}` · 审计：专项 `permission.reply` · 依据：4.2、4.8#6 · 页面：chat-states（第 6 项）

- 请求：`{behavior: "allow"|"deny", message?: string}`。
- 响应：`{ok: true}`。
- 错误：`permission_request_gone`。
- 只在系统设置为「逐操作确认」时出现；600 秒没回复按拒绝处理。

#### S-11 回答问题（决策卡、分支、专家确认、采集确认）

`POST /api/v1/projects/{id}/conversations/{cid}/questions/{question_id}/answer` · 所有者 · 旧：`POST .../permissions/{request_id}` 的 `answers`（AskUserQuestion） · 审计：是 · 依据：第 9 章建议 4、4.7 · 页面：wb-l2、wb-materials、chat-states（第 7、20 项）、inbox、reports（专家裁决）、snapshots（生产采集确认）

- 请求：`{option_id?: string, option_ids?: string[], custom_text?: string}`。
  - 单选题传 `option_id`，多选题传 `option_ids`。
  - 选「其他，我来说」时传对应 `option_id` 加 `custom_text`。
  - 没选不能确认：三项都空返回 `answer_invalid`。
- 响应：`{question: Question}`（`state="answered"`）。
  - 对话流发 `question.answered`；
  - 助手是陪伴建模的等待态时，随即继续；
  - 待处理里对应事项消失。
- 错误：`question_gone`、`answer_invalid`、`conversation_not_found`。

#### S-12 取消排队

`POST /api/v1/projects/{id}/conversations/{cid}/queue/cancel` · 所有者 · 旧：— · 审计：是 · 依据：4.2 会话池 · 页面：chat-states（第 9 项）

- 响应：`{state: "idle"}`。排队中的消息作废，记录保留并标「已取消」。
- 错误：`not_queued`。

#### S-13 对话事件流

`GET /api/v1/projects/{id}/conversations/{cid}/events` · 所有者（可用 `?token=`） · 旧：`GET /api/projects/{id}/session/stream?after=&conversation=` · 审计：否 · 依据：①、4.2 · 页面：全部工作台页

- SSE 对话流，事件见 4.2。
- 查询参数：`after?: int`（最后收到的 seq）、`last_event_id?`。
- 引擎绑着别的对话时，这条流只回放历史并发心跳。

#### S-14 重试出错的一轮

`POST /api/v1/projects/{id}/conversations/{cid}/retry` · 所有者 · 旧：— · 审计：是 · 依据：4.10.1 错误留档 · 页面：chat-states（第 15 项「重试」）

- 重新发起最近一轮出错的请求。
- 请求：`{from_suggestion?: bool}`（F7，同 S-07）。
- 响应 `202`：同 `S-07`。
- 错误：`nothing_to_retry`、`session_busy`。

#### S-15 执行记录列表

`GET /api/v1/projects/{id}/execs` · 所有者 · 旧：— · 审计：否 · 依据：8.4、方案 B5 · 页面：chat-states（第 5 项续跑卡）、inbox（续跑）、ops-obs（链路里看）

- 查询参数：`state?`（`running|waiting|interrupted|done|abandoned`，多值）、`conversation_id?`；分页。
- 响应：`{items: ExecRecord[], next_cursor}`。

#### S-16 续跑

`POST /api/v1/projects/{id}/execs/{exec_id}/resume` · 所有者 · 旧：— · 审计：是 · 依据：8.4、方案 B6 · 页面：chat-states（第 4、5 项「从打断处继续」「从步骤 3 续跑」）、inbox

- 请求：`{conversation_id?: string, from_suggestion?: bool}`。缺省用执行记录所在的对话。
- 能续跑时往对话发一条续跑指令，接着原来那份执行记录（`mode="resume"`）。
- 能力升级、输入改过时（F7）按新版重新执行这条命令（`mode="rerun"`）：原记录记为放弃（`replaced_by` 指向新记录），
  新开一份，提示词带上「已经保存的修改保留，不要重做」；续跑卡改为「已按新版重新做」。
- 响应 `202`：`{mode: "resume"|"rerun", exec: ExecRecord, replaced?: ExecRecord, message_id, conversation_id}`。
- 错误：`exec_not_found`、`exec_not_resumable`（已经做完了）、`session_busy`、`autobuild_running`、`suggestion_stale`。

#### S-17 放弃执行

`POST /api/v1/projects/{id}/execs/{exec_id}/abandon` · 所有者 · 旧：— · 审计：是 · 依据：8.4 · 页面：chat-states（「放弃，从头再来」）、inbox

- 请求：`{conversation_id?: string, from_suggestion?: bool}`（F7，建议条「放弃这一步」）。
- 响应：`{exec: ExecRecord}`（`state="abandoned"`）。
- 已经保存的版本不回退。

#### S-18 全部命令

`GET /api/v1/projects/{id}/commands/catalog` · 所有者 · 旧：— · 审计：否 · 依据：5.6（界面入口）、6.10（全部命令下拉） · 页面：chat-states（第 2 项）、全部工作台页（「全部命令」、⌘K）

- 响应：`{groups: [{id, label, items: CommandItem[]}], total}`。
- 按业务名分组，只列已启用包的命令。命令名只在 `command` 字段里，界面不显示。

#### S-19 页面命令任务

`GET /api/v1/projects/{id}/commands/tasks` · 所有者 · 旧：— · 审计：否 · 依据：5.9.3 · 页面：scenarios、scripts、knowledge、reports、resources（刷新页面后恢复进度条）

- 查询参数：`origin_page?`、`state?`（默认 `queued,running,waiting_user`）。
- 响应：`{items: CommandTask[]}`，不分页，上限 50。

#### S-20 发命令到对话

`POST /api/v1/projects/{id}/commands` · 所有者 · 旧：— · 审计：是 · 依据：5.9.3「页面只发命令、不跑助手」、6.10 · 页面：scenarios、scripts、knowledge、reports、resources、snapshots、wb-materials、inbox、publish、wb-l2（建模步骤抽屉「开始」）、incremental

请求：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `action` | `{pack, page, action_id}` | 与 `command` 二选一 | 包页面声明里的按钮（详设 5.9.2 `actions`） |
| `command` | string | 与 `action` 二选一 | 内核或包的命令名，如 `/ontology-trace-batch` |
| `args` | object | 否 | 命令参数，如 `{scenario: "s-03"}`、`{report_item: "RI-12"}` |
| `text` | string | 否 | 附带的自然语言说明 |
| `display_label` | string | 否 | 页面上按钮的叫法（建议条、建模步骤、命令面板、快捷项）。给了时 `text` 当作动作自带的参数、不在对话里显示 |
| `display_text` | string | 否 | 对话里显示的说明（F4b-paths）；不给时用 `text`（给了 `display_label` 时不用）。对话里显示「业务名 + 说明」：业务名是命令所属节点的标题加按钮叫法（叫法里已有节点标题时只写叫法），不出命令原文和 `args`，说明里的工作区路径换成业务名；自动标题也用它。发给助手的仍是命令、参数和 `text` 原文 |
| `origin` | `{page, label}` | 是 | 对话里显示「来自「场景模拟」页」用 |
| `conversation_id` | string | 否 | 缺省：引擎当前绑定的对话，没有就取最近更新的一条 |
| `replace` | bool | 否 | 同 S-07（F7） |
| `from_suggestion` | bool | 否 | 同 S-07（F7） |

- 响应 `202`：`{task: CommandTask, conversation_id, message_id: string|null, delivery: "immediate"|"next"|"queued", next: NextCommand|null}`。
  - 助手正在跑时命令由平台代存（`delivery="next"`，同 S-07），这一轮结束后开始；被替换的那条的页面命令任务记为 `cancelled`。
  - 对话里出现一条带 `origin` 的用户消息；
  - 进度经项目流 `command.progress` 推送；
  - 跑完结果回到页面列表（`artifacts.updated`）。
- 错误：
  - `pack_not_enabled`：按钮所属的包没启用，或要等下一条消息才生效；这时 `details.effective="next_message"`；
  - `autobuild_running`、`session_busy`、`invalid_argument`、`next_occupied`、`message_duplicate`（这条任务记为 `cancelled`）、`suggestion_stale`。

#### S-21 子任务的步骤

`GET /api/v1/projects/{id}/conversations/{cid}/subagents/{task_id}/steps` · 所有者 · 旧：— · 审计：否 · 依据：F7 · 页面：全部工作台页（点子任务卡打开的进度面板）

- 查询参数：`cursor?`、`limit?`（默认 50，最大 200）；游标分页（1.6），游标绑定 `task_id`。
- 响应：`{task: {task_id, title, brief: string|null, status, started_at, ended_at, step_count, summary}, items: {id, kind: "tool"|"text", text, state, started_at, ended_at, duration_ms}[], next_cursor}`。
- 数据来自对话历史里这个子任务的记录。一个子任务最多记 200 步，最坏约 100 KB；时间线上的 `subagent.updated` 只带当前一步，面板打开时才拉明细。
- `task.summary` 与 EC-22 同一口径；`items` 不含子代理交回的 JSON 原文。
- 错误：`subagent_not_found`。

#### S-22 取消代存的命令

`POST /api/v1/projects/{id}/conversations/{cid}/next/cancel` · 所有者 · 旧：— · 审计：是 · 依据：F7 · 页面：全部工作台页（输入框上方的排队条）

- 响应：`{next: null}`，对话流发 `next.updated`。这条命令的页面命令任务记为 `cancelled`。没有排着的也返回同样结果（幂等）。

#### S-23 现在开始代存的命令

`POST /api/v1/projects/{id}/conversations/{cid}/next/start` · 所有者 · 旧：— · 审计：是 · 依据：F7 · 页面：全部工作台页（打断后排队条的「现在开始」）

- 响应 `202`：同 S-07（`delivery="immediate"`）。
- 错误：`next_not_found`、`session_busy`（在跑，这一轮结束会自动开始）。

---

### 3.12 流程、统一下一步、建模蓝图

#### FL-01 流程

`GET /api/v1/projects/{id}/flow` · 所有者 · 旧：— · 审计：否 · 依据：㊴、6.6、6.7 · 页面：全部工作台页（主线进度条、建模步骤抽屉）、projects（卡片分段进度，经 `P-01`）

- 查询参数：`conversation_id?`（带上时 `suggestions` 按这条对话的打断状态计算）。
- 响应：`Flow`，即
  - `flow_hash`；
  - `nodes`：`FlowNode[]`，建模节点带 `slices`；
  - `edges`；
  - `sections`：抽屉分组「必经步骤 / 发布前检查 / 可选步骤 / 有需要时」；
  - `slices`：分段 × 步骤；
  - `suggestions`；
  - `notices`：如「1 份新资料」；
  - `autobuild`：运行中时给运行控制。

返回示例：

```json
{"flow_hash": "b41e09…",
 "nodes": [{"id": "core.model", "title": "建模", "kind": "agent", "placement": "spine",
            "state": "running", "flags": {"needs_review": false, "disabled": false, "backfill": false},
            "progress": {"done": 3, "total": 5, "label": "3/5 段"}, "version": "core@1.4.2",
            "section": "required", "actions": [], "per_slice": true}],
 "edges": [{"from": "core.research", "to": "core.model"}],
 "slice_stages": [{"id": "object", "label": "对象"}, {"id": "operation", "label": "操作"},
                  {"id": "rule", "label": "规则"}, {"id": "check", "label": "写入即查"},
                  {"id": "scenario", "label": "场景", "plugin": "trace"}],
 "slices": [{"id": "s5", "name": "超量与审批提示", "group": "now",
             "stages": {"object": "confirmed", "operation": "pending_confirm", "rule": "draft",
                        "check": "running", "scenario": "not_ready"},
             "note": "正在做 · 停在「操作」"}],
 "suggestions": {"mode": "normal", "primary": {...}, "secondary": [...]},
 "notices": [{"kind": "new_materials", "count": 1, "files": ["resource/others/09-修订通知.docx"],
              "action": {"type": "navigate", "page": "incremental"}}]}
```

#### FL-02 统一下一步

`GET /api/v1/projects/{id}/flow/suggestions` · 所有者 · 旧：— · 审计：否 · 依据：6.10、㊸（`state_helper suggest` 同源） · 页面：全部工作台页（建议条）、chat-states（第 18 项）

- 查询参数：`conversation_id?`。
- 响应：`Suggestions`。规则（F7，详设 6.10）：从上往下判，命中第一条就停；1 个主推，最多 2 个次选。
  1. 这条对话在跑：`mode="running"`，不给建议；
  2. 有等你回答的问题：`mode="waiting_decision"`，主推「去回答」（`focus_question`，可点）；
  3. 这一轮出错：`mode="failed"`，主推「重试这一轮」（`retry`）；
  4. 刚被打断且有执行记录：`mode="interrupted"`，主推「继续「命令名」」（`resume_exec`，能力升级时按新版重新做），次选「放弃这一步」（`abandon_exec`）；
     这一轮结束了、命令没收尾时同样两项（主推发「继续」）；
  5. 合入冲突：主推「处理冲突」，其余按 6～10 补齐；
  6. 目标问题没答（F8）：主推「回答目标问题」，次选是主线下一步，不拦着开始建模（蓝图草案不用确认）；
  7. 过期的步骤：「对象有变化，刷新操作」——前半句是确认之后真变了的那几类（多于两类写「对象、操作等」；老记录按主线提交反查，还说不清时只写「刷新X」），后半句是刷新哪一步（按段时写「段名 · 步骤名」）；必经的排在可选的前面，可选的（规则补抽、指标）后面的校验还会跑时交给校验、不单独推；
  8. 报告里有待处理问题、挡住发布的关卡没过：继续校验或关卡的操作；
  9. 主线下一个必经步骤（按段时按钮带段名）；
  10. 必经都做完：查看整体视图，次选扫描脚本候选、补做插件步骤。
  - 自主构建运行中 `mode="autobuild_controls"`，返回运行控制，不推荐下一步。
  - 能显示就能执行：每个条目的动作先过「此刻能不能执行」的检查（与接口收到 `from_suggestion` 请求时同一份），不能的不出现，整条规则没有能执行的就往下一条判。
  - 第 3、4 条带说明行 `note`（主推的 `reason`），其余为 null（第 7 条的原因已经写在按钮上）。
  - 会话变为运行中、回到空闲、出现等你回答的问题、出错、打断时，对话流推新的一份（EC-53）；`rev` 递增，界面取 `rev` 大的那份。

#### FL-03 跳过节点

`POST /api/v1/projects/{id}/flow/nodes/{node_id}/skip` · 所有者 · 旧：— · 审计：是 · 依据：6.9、features「跳过 / 补做 / 恢复」 · 页面：全部工作台页（建模步骤抽屉「跳过」）

- 请求：`{slice_id?: string, reason?: string}`。
- 主线和必经节点不能跳过。
- 响应：`{node: FlowNode, flow_hash}`。
- 错误：`node_not_found`、`node_not_skippable`。

#### FL-04 恢复节点

`POST /api/v1/projects/{id}/flow/nodes/{node_id}/restore` · 所有者 · 旧：— · 审计：是 · 依据：6.9 · 页面：建模步骤抽屉（「恢复」）

- 请求：`{slice_id?}`。
- 响应：同 `FL-03`。
- 错误：`node_not_skipped`。

#### FL-05 开始节点（含补做）

`POST /api/v1/projects/{id}/flow/nodes/{node_id}/start` · 所有者 · 旧：— · 审计：是 · 依据：6.3 `entry`、6.9 · 页面：建模步骤抽屉（「开始」「刷新」）、publish（合规检查「去运行」）、inbox（「刷新这个场景」）

- 请求：`{slice_id?, args?: object, origin: {page, label}}`。
- 等价于用节点声明的 `entry.command` 调 `S-20`。
- 响应 `202`：同 `S-20`。
- 脚本节点（`kind: script`）当场在沙箱里跑完，响应 `200`：节点视图加 `run: {state, output, commit, version_label, duration_ms, runtime, warnings}`。`warnings: [{code: "pack_gate", level: "warning", message, gate, path}]` 是输出没过的包提交门禁：只提示，输出照常写进版本（F3b）。
- 错误：`node_not_found`、`flow_assembly_failed`、`autobuild_running`。

#### BP-01 建模蓝图

`GET /api/v1/projects/{id}/blueprint` · 所有者 · 旧：— · 审计：否 · 依据：第 9 章建议 2、6.8（`core.research` 含分段规划） · 页面：wb-blueprint（蓝图卡、调整弹窗）、chat-states（产物卡）、proj-settings（自主构建范围的分段清单）

- 响应：`Blueprint`，即要解决的问题、分段、术语口径、待答问题、草案版本、状态、`revision`。
- F4b：要解决的问题（`goal_md`）就是项目信息里的建模目标（`P-05` 的 `goal`），蓝图不另存一份；`goal_anchor` 恒为 null。「范围外」已取消，不再返回。
- 错误：`blueprint_not_found`。

#### BP-02 保存蓝图调整

`PUT /api/v1/projects/{id}/blueprint` · 所有者 · 旧：— · 审计：是 · 依据：第 9 章建议 2 · 页面：wb-blueprint

请求：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `base_revision` | string | 是 | — |
| `mode` | string | 是 | `draft`：保存为草案，草案号不变；`adjust`：保存调整，草案号加 1 |
| `slices` | `BlueprintSliceInput[]` | 否 | 完整分段列表，按数组顺序排。合并、排除、改名、调顺序、新增都表达为对这份列表的修改。合并时 `merged_from` 写原分段 ID，`excluded: true` 表示排除 |
| `term_choices` | `{term_id, chosen}[]` | 否 | 统一叫法 |
| `answers` | `{question_id, option_id?, custom_text?}[]` | 否 | 待你回答 |

- 「按章节拆分」需要读资料，不在这里做，前端用 `S-20` 发命令「把分段 X 按章节拆分」。
- 保存后助手按新蓝图继续（下一条消息读取）。
- F4b：`out_of_scope` 已取消，旧前端带着也忽略。回答目标问题（`kind: "goal"`）时选了「改成：…」或写了 `custom_text`，同时改项目信息里的建模目标（推 `project.updated`）；选「保持原目标」不改。
- 响应：`Blueprint`。
- 错误：`revision_conflict`、`slice_invalid`、`write_scope_locked`、`blueprint_not_found`。

#### BP-03 确认蓝图

`POST /api/v1/projects/{id}/blueprint/confirm` · 所有者 · 旧：— · 审计：是 · 依据：1.6#2、8.5 · 页面：wb-blueprint（「确认蓝图，开始第一段」「一段一段来」「按蓝图一路建完」）

- 请求：`{base_revision, next: "L2"|"L4"}`。缺省取项目的构建方式。
- 没答的问题留在待处理。
- F3：资料分析那一轮结束就合入，蓝图在主线上，确认只标已确认，不再先合入变更集；升级前留下的待确认变更集先合入（检查不拦）。
- 响应：
  - `next="L2"`：返回 `{blueprint, command_task}`，往对话发「开始第一段」；
  - `next="L4"`：返回 `{blueprint, autobuild_preview: AutobuildPreview}`，前端展示范围和预算后再调 `AB-04`（详设 8.5：开始前展示、确认后才跑）。
- 错误：`revision_conflict`、`blueprint_not_found`；`merge_conflict`（蓝图所在的那一轮和你的修改冲突，`details.inbox_item_id` 指向待处理）；`changeset_in_progress`（助手还在改，等这一轮结束）；`blueprint_question_pending`（F4b：资料和建模目标对不上的那个问题还没答，`details.question_id`；任何构建方式都不由策略代答）。

---

### 3.13 待处理

依据详设 4.7。待处理不另存一份状态：处理完一项，它的来源就变了，计数随之减少。唯一自存的是「忽略」记录（`.onto/run/` 下，不进 git）。

事项类型 `kind`：

| kind | 界面叫法 | 来源 | 可做的动作（`IN-03` 的 `action`） |
|---|---|---|---|
| `decision` | 决策 | 陪伴建模里没回答的决策卡 | `answer` |
| `evidence_conflict` | 证据冲突 | 三方对账报告 | `arbitrate` |
| `change_list` | 变更清单 | 增量更新的变更点清单 | `accept_points`、`rewrite_point`、`execute` |
| `review` | 需复核 | 自主构建自动决定、节点升级、导入；`source` 分别为 `autobuild\|upgrade\|import` | `accept`、`rejudge`、`revert_slice` |
| `suspension` | 挂起 | 自主构建挂起的问题，包括未授权的采集 | `answer`、`use_generic` |
| `stale` | 过期 | 节点状态；已交给后面已确认的核对节点的过期不列（和建议条第 7 条同一判定 `flow_state.handed_down`，ADR-0090） | `refresh`、`keep_stale` |
| `resumable` | 续跑 | 中断的执行；开始自主构建时交给运行收尾的不列（见 AB-04） | `resume`、`abandon` |
| `feedback` | 外部反馈 | 评测服务回传（㉔） | `send_to_evolution`、`dismiss` |
| `report` | 报告待处理 | 某份报告里还没处理的条目，一份报告合成一项 | `send_to_assistant` |
| `changeset_conflict` | 合入冲突 | 这一轮合入时和你的修改改到同一条（F3 起唯一会出现的变更集事项；没有「修改待确认」） | `resolve`、`discard`（撤回这一轮） |
| `check_findings` | 检查未过 | 助手这一轮没提交完、平台代存了版本，合入检查有错误（F8；最近一个合入的变更集才出，`impact` 列错误，最多 20 条） | `send_to_assistant`（发 `/ontology-validate` 修正，发出后算处理过） |
| `format_migration` | 格式待迁移 | 插件升级改了产物格式（5.9.4） | `migrate` |
| `notification` | 通知 | 提名审核结果等 | `dismiss` |

所有事项都可以 `dismiss`，忽略后不计数。设计稿的筛选分段对应：

| 设计稿筛选 | 对应的 kind |
|---|---|
| 全部 | 全部 kind |
| 自主构建复核 | `review`（`source=autobuild`）和 `suspension` |
| 决策 | `decision` |
| 证据冲突 | `evidence_conflict` |
| 变更清单 | `change_list` |
| 过期 | `stale` |
| 续跑 | `resumable` |
| 外部反馈 | `feedback` |

#### IN-01 待处理列表

`GET /api/v1/projects/{id}/inbox` · 所有者 · 旧：— · 审计：否 · 依据：4.7、㊴ · 页面：inbox、全部项目页（左栏工作台角标、顶栏「待处理 N」）、projects（卡片「待处理 N」，经 `P-01`）

- 查询参数：
  - `filter?`：`all|autobuild_review|decision|evidence_conflict|change_list|stale|resumable|feedback`；
  - `kind?`（多值）、`run_id?`、`pack?`（插件页角标用）；
  - 分页，默认 50。
- 响应：

  | 字段 | 类型 | 说明 |
  |---|---|---|
  | `items` | `InboxItem[]` | — |
  | `next_cursor` | string \| null | — |
  | `counts` | object | `{total, by_filter: {all, autobuild_review, decision, evidence_conflict, change_list, stale, resumable, feedback}, by_pack: {[pack]: int}}` |
  | `today` | object | `{handled, total, eta_minutes, remaining_by_filter: {...}}`：「今天的进度」 |
  | `autobuild_summary` | object \| null | 只在 `filter=autobuild_review` 时有：`{run_id, run_no, started_at, ended_at, state, cost: Money, decisions, high_confidence, needs_look, suspended, assumptions}` |

#### IN-02 事项详情

`GET /api/v1/projects/{id}/inbox/{item_id}` · 所有者 · 旧：— · 审计：否 · 依据：4.7 · 页面：inbox

- 响应：`InboxItem`，加 `detail`。`detail` 按 `kind` 取不同结构（见第 7 章 `InboxDetail`）：

  | kind | `detail` 结构 |
  |---|---|
  | `decision` | `Question` |
  | `evidence_conflict` | `ConflictDetail`：左右两侧来源和取值、选项 |
  | `change_list` | `ChangeList` |
  | `review` | `LedgerEntry` 加选项 |
  | `suspension` | `Suspension` |
  | `stale` | `StaleDetail`：上游差异、对本项的影响 |
  | `resumable` | `ExecRecord` |
  | `feedback` | `Feedback` |
  | `report` | 报告摘要 |

#### IN-03 处理事项

`POST /api/v1/projects/{id}/inbox/{item_id}/actions` · 所有者 · 旧：— · 审计：是 · 依据：4.7 · 页面：inbox（「确认并下一条」、各类按钮、键盘 1–4、E、↵）

请求：`{action: string, option_id?, custom_text?, point_ids?: string[], point_id?, text?, choices?, base_revision?}`。字段按 action 取用：

| action | 用到的字段 | 等价接口 |
|---|---|---|
| `answer` | `option_id`、`custom_text` | `S-11` 或 `AB-15` |
| `arbitrate` | `option_id`、`custom_text` | 写进对应对象的「证据」章节，两处出处都保留，经写入路径 |
| `accept_points` | `point_ids` | `INC-03` |
| `rewrite_point` | `point_id`、`text` | `INC-02` 的单行版 |
| `execute` | — | `INC-04` |
| `accept`、`rejudge` | `option_id`、`custom_text` | `AB-11`、`AB-12` |
| `revert_slice` | — | `AB-17` |
| `use_generic` | — | 挂起问题「先按通用要求」 |
| `refresh` | — | `FL-05` |
| `keep_stale` | — | 忽略并保持过期 |
| `resume`、`abandon` | — | `S-16`、`S-17` |
| `send_to_evolution` | — | 以反馈演进命令调 `S-20` |
| `send_to_assistant` | — | 以报告修复命令调 `S-20` |
| `discard` | — | `CS-05` |
| `resolve` | `choices` | `CS-07` |
| `migrate` | — | 包的迁移命令 |
| `dismiss` | — | 忽略 |

- 响应 `200`：`{item_id, resolved: bool, result: object, next_item_id: string|null}`。
  - 会触发助手工作的动作，`result` 里带 `command_task`。
  - 「改判」后助手重做受影响的部分并保存新版本。
- 错误：
  - `invalid_argument`：动作与 kind 不匹配；
  - `not_found`：事项已不存在，前端刷新列表；
  - 以及等价接口的全部错误码。

#### IN-04 批量处理

`POST /api/v1/projects/{id}/inbox/batch` · 所有者 · 旧：— · 审计：是 · 依据：4.7、8.5 · 页面：inbox（「一键接受高置信 13 项」「全部刷新 3 项」「接受选中的 3 项（高把握）」）

- 请求：`{action: "accept_high_confidence"|"refresh_stale"|"accept_change_points", run_id?, item_ids?: string[], change_list_id?, point_ids?: string[]}`。
- 规则：
  - `accept_high_confidence`：只接受置信度 ≥ 阈值（默认 0.80）的自动决定；
  - `accept_change_points`：只接受「把握高」的变更点，低把握的条目不能批量接受。
- 响应：`{accepted: int, skipped: [{id, reason}], command_task: CommandTask|null}`。
- 错误：`invalid_argument`、`run_not_found`。

---

### 3.14 增量更新（变更点清单）

增量更新是内核能力（core 的按需节点 `core.update`），不单独设页面，在对话里走完（详设 1.6、14#16）。变更点清单是对话里的卡片，双击打开弹窗逐条改。清单在执行前可以编辑，不受写入范围锁约束。

#### INC-01 变更点清单

`GET /api/v1/projects/{id}/change-lists/{change_list_id}` · 所有者 · 旧：— · 审计：否 · 依据：第 9 章建议 6、INC-5、INC-11 · 页面：incremental（清单卡、弹窗）、inbox（变更清单）

- 响应：`ChangeList`，即来源资料、变更点、状态、`revision`、影响面摘要。
- 错误：`not_found`。

#### INC-02 保存清单

`PUT /api/v1/projects/{id}/change-lists/{change_list_id}` · 所有者 · 旧：— · 审计：是 · 依据：第 9 章建议 6 · 页面：incremental（「保存清单」「补充一条」「改写」）、inbox

- 请求：`{base_revision, points: ChangePointInput[]}`。完整列表；新增条目不带 `id`。
- 保存后重算影响面（确定性，秒级），经项目流 `change_list.updated` 推送。
- 响应：`{change_list: ChangeList, impact_ms: int}`。
- 错误：`revision_conflict`、`invalid_argument`、`changeset_closed`（清单已执行）。

#### INC-03 接受变更点

`POST /api/v1/projects/{id}/change-lists/{change_list_id}/accept` · 所有者 · 旧：— · 审计：是 · 依据：INC-11 · 页面：incremental（「接受高把握项 2」「接受」）、inbox

- 请求：`{point_ids?: string[], high_confidence?: bool}`，二者必选其一。
- 低把握的条目不能出现在多于一条的批量里。
- 响应：`ChangeList`。
- 错误：`low_confidence_batch`、`revision_conflict`。


#### INC-04 确认清单并执行

`POST /api/v1/projects/{id}/change-lists/{change_list_id}/execute` · 所有者 · 旧：— · 审计：是 · 依据：第 9 章建议 6 · 页面：incremental（「确认清单并执行」）、inbox

- 请求：`{base_revision}`。
- 要求所有条目已接受或已改写，低把握条目必须单独确认过。
- 往对话发执行命令。之后的流程：只改受影响部分 → 只查改动 → 自动回归 → 回归结果卡 → 本轮改了什么卡 → 确认生效。
- 响应 `202`：`{command_task: CommandTask, changeset_id}`。
- 错误：`revision_conflict`、`invalid_argument`（`details.unconfirmed` 列出没确认的条目）。

#### INC-05 影响面

`GET /api/v1/projects/{id}/change-lists/{change_list_id}/impact` · 所有者 · 旧：— · 审计：否 · 依据：第 9 章建议 6、INC-6 · 页面：incremental（影响面卡）

- 响应：`Impact`，即：
  - 耗时，拆成按变更点定位产物、沿引用算下游、挑回归场景三段；
  - 将标为过期的产物；
  - 回归场景：受影响的旧场景，加守护老路径的场景。

---

### 3.15 报告

报告逐条评审吸收了现有 `report_items`，改由产物登记解析（详设 5.9.4、方案 C13）。报告类型有：

| type | 界面叫法 |
|---|---|
| `rule` | 规则 |
| `link` | 链接 |
| `research` | 资料 |
| `semantic` | 语义 |
| `trace` | 追踪 |
| `candidate` | 候选 |
| `coverage` | 覆盖 |
| `diagram` | 流程图校验 |
| `codegen` | 代码生成 |
| `<包>.<名>` | 包登记的报告格式 |

#### RP-01 报告列表

`GET /api/v1/projects/{id}/reports` · 所有者 · 旧：— · 审计：否 · 依据：5.9.4 · 页面：reports

- 查询参数：`type?`（多值）；分页（默认 50）。
- 响应：`{items: ReportSummary[], next_cursor}`。

#### RP-02 报告详情

`GET /api/v1/projects/{id}/reports/{report_id}` · 所有者 · 旧：`GET /api/projects/{id}/report-items?path=` · 审计：否 · 依据：5.9.4、第 9 章建议 3 · 页面：reports、wb-blueprint（资料分析小结卡）

- 响应：`Report`，包括：
  - 摘要、范围、模式、耗时、上一份；
  - 处理进度；
  - 条目（第一页）；
  - 修复轮次 `repair: {rounds: [{n, count}], limit: 2, exhausted: bool}`；
  - 专家裁决 `arbitration: Question|null`（`conversation_id` 在 `Question` 里，用 `S-11` 回答）；
  - 类型专属内容 `extras`：
    - `link`：`{orphans, unused, dangling}`；
    - `research`：资料读取结果，见 `MaterialMap` 的摘要；
    - `codegen`：候选与不适合的操作。

#### RP-03 报告条目

`GET /api/v1/projects/{id}/reports/{report_id}/items` · 所有者 · 旧：`GET /api/projects/{id}/report-items` · 审计：否 · 依据：5.9.4 · 页面：reports

- 查询参数：`status?`（`expert|fixing|pending|fixed|skipped`）、`level?`；分页（默认 100）。
- 响应：`{items: ReportItem[], next_cursor, counts: {error, warning, fixed, skipped}}`。

#### RP-04 报告原文

`GET /api/v1/projects/{id}/reports/{report_id}/raw` · 所有者 · 旧：`GET /api/projects/{id}/file?path=reports/...` · 审计：否 · 依据：5.9.4 · 页面：reports（「报告原文」）

响应：`{path, content_md, truncated}`。

#### RP-05 发给助手修复

`POST /api/v1/projects/{id}/reports/{report_id}/fix` · 所有者 · 旧：— · 审计：是 · 依据：第 9 章建议 3（修复最多 2 轮） · 页面：reports（「逐条修复（发给助手）」、单条「发给助手」）

- 请求：`{item_ids?: string[]}`，不传表示全部未修复条目。
- 等价于 `S-20`，发报告修复命令，参数里带条目。
- 响应 `202`：同 `S-20`。
- 错误：`invalid_argument`（条目已修复）、`autobuild_running`。

#### RP-06 重新检查

`POST /api/v1/projects/{id}/reports/recheck` · 所有者 · 旧：— · 审计：是 · 依据：4.4、第 9 章建议 3 · 页面：reports（「重新校验」）、publish（「修完重新检查」）

- 请求：`{kind: "rule"|"semantic"|"link"|"coverage"|"diagram", mode?: "regression"|"full"}`。
- `link` 由内核确定性计算，同步返回 `200 {report: Report}`。其余发命令，返回 `202 {command_task}`。

---

### 3.16 插件：目录、按项目启用、装配状态、上传与审核

依据详设 4.6、5.2、5.3、5.6、5.9、8.1、方案 D1～D7、D17。

- 包有 6 类：能力包、行业包、系统知识包、多源连接器（`category=provider`，旧称证据源提供方）、节点包、导出器。
- 多源连接器里列文档转换器（`PackCard.converter` 不为空，ADR-0073）；部署了连接器网关时还列网关里的采集类连接器（数据库、代码仓库、REA，`PackCard.collector` 不为空），它们不是包仓库里的包、不按项目启用，网关不通时不列。
- 包仓库里每个版本的目录不可修改。项目用 `.onto/packs.lock` 锁定版本。
- 切换从下一条消息开始生效；正在进行的一轮按旧版本跑完；自主构建运行中时，在下一个安全点之后生效。

#### PL-01 插件目录

`GET /api/v1/packs` · 登录 · 旧：`GET /api/settings/assets`（全局单版本，删除） · 审计：否 · 依据：5.3、5.7、8.1 · 页面：plugins、create（第 3 步行业包、系统知识包、能力包）

- 列出公开的包和看的人自己的私有包（`publisher.kind="private"`）。别人的私有包在 PL-01、PL-02、PL-03、PL-06 和新建向导里都按不存在处理，已经锁在项目里的除外（项目转给别人后照常能用）。

- 查询参数：
  - `category?`：`capability|industry|system|provider|node|exporter`，多值；
  - `q?`：名称、ID、描述子串；
  - `project_id?`：带上时，每项计算本项目视角的 `project_status`，并返回推荐；
  - `installed_only?`：只看「已启用」；
  - `updatable_only?`：只看「可更新」。
- 不分页，平台包总数上限 500。
- 响应：

  | 字段 | 类型 | 说明 |
  |---|---|---|
  | `items` | `PackCard[]` | — |
  | `counts` | object | `{all, capability, industry, system, provider, node, exporter, enabled, updatable}` |
  | `recommendations` | `[{pack: PackRef, reason: string, action: "enable"\|"upgrade"}]` | 只在带 `project_id` 时有，按资料、已登记的数据源和分段进度推荐，最多 3 条 |

- `PackCard.project_status` 取值：

  | 值 | 卡片显示 | 按钮 |
  |---|---|---|
  | `required` | 必装 · 已启用 | 详情 |
  | `enabled` | 已启用 | 详情 |
  | `update` | 已启用 x.y | 更新到 x.z |
  | `available` | — | 启用到本项目 |
  | `offline` | 离线部署不可用 | 启用（禁用） |
  | `not_applicable` | 本项目不适用 | 详情 |
  | `platform_installed` | 平台已安装（多源连接器；转换器、装好的采集类连接器不论带不带项目都是这个） | 详情 |
  | `platform_available` | 未安装（多源连接器；采集类连接器没装时也是这个，只能管理员部署，不出安装按钮） | 安装到平台（采集类：详情） |
  | `pending` | 已启用 · 下一条消息生效 | 详情 |

#### PL-02 插件详情

`GET /api/v1/packs/{name}` · 登录 · 旧：— · 审计：否 · 依据：5.2、6.9 · 页面：plugin-detail

- 查询参数：`project_id?`、`version?`（缺省取项目锁定的版本，没有则取 stable 最新版）。
- 响应：`PackDetail`，包括：
  - 头部：名称、版本、类别、发布方、标签、描述、大小、发布日期、本项目启用日期；
  - `requires`：依赖范围（「基础建模能力 1.4 以上」）、`kernel_api`、`page_api`；
  - `flow_insertions`：往流程里加了什么；
  - `project_stats`：本项目里的产出统计；
  - `contents`：「包含什么」分组，组名用界面叫法；
  - `ui`：界面入口；
  - `config`：可调设置；
  - `changelog`：更新记录，每条带 `{version, date, notes, tags: ["needs_core_upgrade"|"in_use"]}`。

#### PL-03 兼容检查

`GET /api/v1/packs/{name}/versions/{version}/compatibility` · 登录 · 旧：— · 审计：否 · 依据：4.6、8.1、6.7（组装检查） · 页面：plugins（启用、更新弹窗）、plugin-detail（「更新到 0.10.0」、不能更新弹窗）

- 查询参数：`project_id`（必填）。
- 响应：

  | 字段 | 类型 | 说明 |
  |---|---|---|
  | `ok` | bool | — |
  | `checks` | `CompatCheck[]` | 内核接口 `kernel_api`、会话引擎、依赖、命令 / 子代理 / 脚本名唯一、流程组装、`page_api` |
  | `brings` | object | `{summary_md, commands: int, agents: int, skills: int, nodes: [{id, title, placement}], pages: [{id, title}], subpages: [{parent, id, title}]}` |
  | `ui_effect` | object | `{kind: "page"\|"subpages"\|"none"\|"kernel", text}`，如「启用后左栏出现「场景模拟」」 |
  | `effective` | string | `next_message\|next_safe_point\|immediate`：行业包这类纯数据包是 `immediate` |
  | `impact` | object | `{needs_review_after_upgrade: int, affected_artifacts: int, note}`：节点完成判定变化等 |
  | `blocked_reason` | string \| null | 如「需要先升级基础建模能力」 |

#### PL-04 申请平台安装

`POST /api/v1/packs/{name}/install-requests` · 登录 · 旧：— · 审计：是 · 依据：5.8.3（提供方由管理员安装） · 页面：plugins（非管理员点「安装到平台」）

- 请求：`{version?, note?: string}`。
- 响应 `202`：`{request_id}`。管理员收到通知。

#### PL-05 本项目的能力

`GET /api/v1/projects/{id}/packs` · 所有者 · 旧：— · 审计：否 · 依据：8.1、1.6#4 · 页面：proj-settings（能力与插件）、plugins（「本项目已启用」）

- 响应：`{items: ProjectPack[], pending_switches: int, lock_hash, flow_hash}`。

#### PL-06 启用到本项目

`POST /api/v1/projects/{id}/packs` · 所有者 · 旧：— · 审计：专项 `pack.enable` · 依据：8.1、6.9、14#1 · 页面：plugins（启用弹窗「启用」、推荐「启用」）、proj-settings（「启用」）

- 请求：`{name, version?: string}`，缺省取 stable 最新版。
- 效果：
  - 改 `packs.lock`，作为一个变更集自动合入（`Onto-Source: plugin`），可撤回；
  - 下一条消息前重新装配。
- 响应 `201`：`{pack: ProjectPack, commit, version_label, effective: "next_message"|"next_safe_point"|"immediate", extensions_pending_hash}`。
- 错误：`pack_not_found`、`pack_already_enabled`、`pack_incompatible`、`pack_unavailable`、`page_api_unsupported`、`flow_assembly_failed`、`write_scope_locked`。

#### PL-07 升级

`POST /api/v1/projects/{id}/packs/{name}/upgrade` · 所有者 · 旧：— · 审计：专项 `pack.upgrade` · 依据：8.1、6.9#6 · 页面：plugins（「更新到 x」、更新弹窗）、plugin-detail（「更新到 0.10.0」）、proj-settings（「升级」）

- 请求：`{to_version: string, effective?: "next_message"|"next_autobuild"}`。
  - `next_autobuild` 对应「只在下次自主构建时启用新版本」。
- 节点完成判定变了的话，旧的「已确认」标为「需复核」（6.9）。
- 响应：同 `PL-06`。
- 错误：同 `PL-06`，加 `pack_not_enabled`、`pack_version_not_found`。

#### PL-08 回滚

`POST /api/v1/projects/{id}/packs/{name}/rollback` · 所有者 · 旧：`POST /api/settings/assets/rollback`（全局，删除） · 审计：专项 `pack.rollback` · 依据：8.1#6 · 页面：proj-settings（「回滚」）

- 请求：`{to_version: string}`。旧版本一直保留在包仓库。
- 响应：同 `PL-06`。

#### PL-09 停用

`POST /api/v1/projects/{id}/packs/{name}/disable` · 所有者 · 旧：— · 审计：专项 `pack.disable` · 依据：5.9.6、6.9#5 · 页面：proj-settings（「停用」）、plugin-detail（菜单「停用」）

- 效果：
  - 产物保留；
  - 节点标「已停用」，被它挡住的关卡随之解除；
  - 带页面的包，左栏页面立即消失；
  - 待处理里属于它的事项保留，可以忽略。
- 响应：同 `PL-06`。
- 错误：`pack_required`（core）、`pack_not_enabled`。

#### PL-10 撤销待生效的切换

`POST /api/v1/projects/{id}/packs/{name}/cancel-pending` · 所有者 · 旧：— · 审计：是 · 依据：8.1 · 页面：proj-settings（「撤销切换」）

- 在下一条消息之前把锁文件改回去。
- 响应：`{pack: ProjectPack}`。
- 错误：`no_pending_switch`。

#### PL-11 装配状态

`GET /api/v1/projects/{id}/assembly` · 所有者 · 旧：— · 审计：否 · 依据：8.1、⑭⑮、方案 D2～D4 · 页面：proj-settings（待生效 chip）、chat-states（第 14 项能力包升级提示）

- 响应：`{active_hash, pending_hash|null, lock_hash, flow_hash, state: "applied"|"pending"|"failed", pending_changes: [{pack, from, to, kind: "enable"|"upgrade"|"rollback"|"disable"}], effective: "next_message"|"next_safe_point", last_error: {error_id, message, at}|null}`。

#### PL-12 扩展项注册表

`GET /api/v1/projects/{id}/extensions` · 所有者 · 旧：— · 审计：否 · 依据：⑥、5.6、5.9.2、5.9.7、方案 D17 · 页面：全部项目页（左栏）、resources（子页）、studio-table（条目类型）、chat-states（快捷操作）、scenarios、knowledge、scripts

- 响应：`Extensions`，包括：
  - 左栏 `nav`：内核 4 项，加上插件页面，按声明顺序排，超过 5 个进「更多」；
  - `subpages`；
  - `pages`：页面声明，含 `data`、`actions`、`badge`；
  - `entries`：界面入口，如快捷项、附加操作、数据源连接向导；
  - `artifact_types`、`views`、`gates`、`config`；
  - `active_hash`、`pending_hash`；
  - `disabled_pages`：已停用插件的页面，打开旧链接显示「插件已停用」和重新启用按钮。
- 左栏只列「声明的页面 ∩ 前端内置页面」，且通过 `page_api` 校验。

#### PL-13 上传插件包

`POST /api/v1/admin/packs/uploads`（multipart） · admin · 旧：`POST /api/settings/assets/upload`（改写为上传 core 新版本） · 审计：专项 `pack.upload` · 依据：6.9#1、8.1#1、方案 D1 · 页面：plugin-upload

- 请求：`file`（zip，≤200 MiB）、`tenant_label?: string`（以租户身份上传时填）。
- 上传后自动做静态检查。
- 响应 `202`：`PackUpload`（`state="checking"`）。
- 错误：`pack_upload_invalid`、`payload_too_large`。

#### PL-14 上传列表

`GET /api/v1/admin/packs/uploads` · admin · 旧：— · 审计：否 · 依据：6.9 · 页面：plugin-upload

- 查询参数：`state?`（`checking|passed|failed|published|rejected|held`）；分页。
- 响应：`{items: PackUpload[], next_cursor}`。

#### PL-15 上传详情与静态检查

`GET /api/v1/admin/packs/uploads/{upload_id}` · admin · 旧：— · 审计：否 · 依据：6.9#1、5.9.6（租户包不能带前端代码）、方案 D1 · 页面：plugin-upload

- 响应：`PackUpload`，加：
  - `manifest: ManifestSummary`；
  - `checks: StaticCheck[]`；
  - `timeline: [{at, actor, text, state}]`：审核记录。
- 检查项至少包括：
  - 清单格式与必填字段、校验和一致；
  - 声明的命令都存在；
  - 写入范围不碰受保护目录（`resource/`、`.claude`、`.onto` 等）；
  - 没有新增实体类型、兼容范围对得上、名字不冲突；
  - 不含凭据；
  - hooks 只追加拒绝型；
  - 页面只选用平台内置、没有前端代码；
  - 流程声明了范围限定（`confine`）时：只有一个范围文件，在本包节点的 `produces` 里，不在 `ontology/` 和受保护目录下（检查项 `confine`，ADR-0085）；
  - 提示词体积预算。

#### PL-16 下载检查报告

`GET /api/v1/admin/packs/uploads/{upload_id}/report` · admin（可用 `?token=`） · 旧：— · 审计：否 · 依据：6.9 · 页面：plugin-upload

- 响应：`text/markdown` 附件。

#### PL-17 发布到通道

`POST /api/v1/admin/packs/uploads/{upload_id}/publish` · admin · 旧：— · 审计：专项 `pack.publish` · 依据：8.1#2（灰度通道） · 页面：plugin-upload

- 请求：`{channel: "beta"|"stable", rollout: {by: "project"|"tenant", percent: 0–100, targets?: string[]}, auto_breaker: bool}`。
- 普通用户的上传（`scope="private"`）也能发布，`installed` 状态同样可以：发布后这个包变成公开的。包名是别人的私有包时回 `pack_name_taken`。
  - `auto_breaker`：失败率超过 5% 或连续 5 次失败时，回退到上一版本。
- 包进入只读的包仓库目录。
- 响应：`{pack: PackRef, channel, rollout}`。
- 错误：`static_check_failed`、`pack_upload_state`、`invalid_argument`。

#### PL-18 退回

`POST /api/v1/admin/packs/uploads/{upload_id}/reject` · admin · 旧：— · 审计：是 · 依据：6.9 · 页面：plugin-upload

- 请求：`{reason: string}`。上传者收到通知。
- 响应：`PackUpload`（`state="rejected"`）。
- 「暂不发布」不调接口，上传保持 `passed`。

#### PL-19 平台安装提供方

`POST /api/v1/admin/packs/{name}/install` · admin · 旧：— · 审计：专项 `pack.install` · 依据：5.8.3、H2 · 页面：plugins（管理员点「安装到平台」）

- 请求：`{version}`。连接器和导出器装进网关或旁路容器，H2 级生效。
- 响应 `202`：`{task_id}`，进度在 `O-40` 任务列表。
- 本期 PL-04、PL-19 仍回 `501`：第三方转换器怎么装待定（不能有主容器往网关放代码的通道，ADR-0073 待定项）。

#### PL-20 转换器与默认

`GET /api/v1/converters` · 登录 · 旧：— · 审计：否 · 依据：ADR-0073 · 页面：plugin-detail（转换器详情的默认标记）

- 响应 `200`：`{gateway: {status: "ok"|"down"|"not_configured", detail}, items: [{id, label, version, types: string[], license, official, loaded: bool|null, default_for: string[]}], types: [{type: ".pdf", label: "PDF", default: string|null, chosen_by_admin: bool, converters: string[]}]}`。
  - `loaded`：网关里加载了没有，网关不可用时为 null；
  - 没指定默认时用唯一的那个，有多个时优先官方的。

#### PL-21 设默认转换器

`PUT /api/v1/admin/converters/defaults` · admin · 旧：— · 审计：是 · 依据：ADR-0073 · 页面：plugin-detail（「设为 PDF 默认」，同一种文件有多个转换器时才出现）

- 请求：`{type: ".pdf", converter: "convert-pdf"}`，只能在已有的转换器之间选。换了默认不重转旧资料，资料上用 R-14。
- 响应 `200`：同 PL-20。
- 错误：`invalid_argument`（这种文件没有这个转换器）。

---

#### PL-22 上传自己用的插件包

`POST /api/v1/me/pack-uploads`（multipart） · 登录即可 · 旧：— · 审计：专项 `pack.upload` · 依据：ADR-0079 · 页面：plugin-upload

- 请求：`file`（zip，≤20 MiB，解压后 ≤64 MiB、≤2000 个文件）。
- 能带的内容和管理员上传一样（ADR-0080）：静态检查按 PL-15 租户包的严格程度查，多一项 `private_name`（包名没被占用）。
- 每人保留最近 20 个上传，更早的自动清掉。
- 响应 `202`：`PackUpload`（`state="checking"`，`scope="private"`）。
- 错误：`pack_upload_invalid`（`reason` 多一种 `too_large`）、`payload_too_large`。

#### PL-23 自己的上传

`GET /api/v1/me/pack-uploads` · 登录即可 · 旧：— · 审计：否 · 依据：ADR-0079 · 页面：plugin-upload

- 查询参数、响应同 PL-14，只有本人的上传。

#### PL-24 自己的上传详情

`GET /api/v1/me/pack-uploads/{upload_id}` · 登录即可 · 旧：— · 审计：否 · 依据：ADR-0079 · 页面：plugin-upload

- 响应同 PL-15。别人的上传回 `404`。

#### PL-25 下载自己的检查报告

`GET /api/v1/me/pack-uploads/{upload_id}/report` · 登录即可（可用 `?token=`） · 旧：— · 审计：否 · 依据：ADR-0079 · 页面：plugin-upload

- 响应同 PL-16。

#### PL-26 安装到自己名下

`POST /api/v1/me/pack-uploads/{upload_id}/install` · 登录即可 · 旧：— · 审计：专项 `pack.private_install` · 依据：ADR-0079 · 页面：plugin-upload

- 包入库，占下包名，之后在插件中心启用到自己的项目（PL-06）。重复调用幂等。
- 响应 `200`：`PackUpload`（`state="installed"`）。
- 错误：`static_check_failed`、`pack_upload_state`、`pack_name_taken`、`pack_quota_exceeded`（每人最多 50 个版本）。

#### PL-27 下载开发 Skill

`GET /api/v1/packs/dev-skills/{category}` · 登录即可（可用 `?token=`） · 旧：— · 审计：否 · 依据：ADR-0081 · 页面：plugins

- `category`：`capability`、`industry`、`system`、`provider`。
- 响应 `200`：`application/zip`，`Content-Disposition` 带文件名 `onto-plugin-dev-<category>.zip`。下载时现打包，顶层目录同名，含 `SKILL.md`、`references/`、`platform.json`、`scripts/pack_tool.py`、`scripts/checker/`。`platform.json` 只有公开包。
- 大小：现在约 70 KiB，最坏约 6 MiB（最多记 500 个公开包，每个包 50 个版本、每类 100 个名字，名字按 80 字符算）。
- 错误：`404 not_found`（`exporter` 和不认识的分类）。

### 3.17 插件产物与通用视图

依据详设 5.9.4、5.9.5、方案 C13。

- 包登记的产物类型（如 `trace.scenario`、`trace.mock`、`codegen.solver_contract`、`diagram.flowchart`、`core.knowledge`）都经这组接口读写。
- 内核按登记的格式解析，不跑包的代码；保存走写入路径，先按格式定义校验，再过包声明的门禁规则。
- 插件产物不是本体实体：不建关系边，不进知识包。

#### AR-01 产物类型

`GET /api/v1/projects/{id}/artifact-types` · 所有者 · 旧：— · 审计：否 · 依据：5.9.4 · 页面：scenarios、scripts、knowledge、studio-table（流程图）

- 响应：`{items: ArtifactType[]}`，每项含格式定义（字段与类型）、`refs`、`status` 映射和视图声明（`list`、`detail`、`card`）。
- 只返回已启用包登记的类型。

#### AR-02 产物列表

`GET /api/v1/projects/{id}/artifacts/{type}` · 所有者 · 旧：— · 审计：否 · 依据：5.9.4 · 页面：scenarios（场景、示例数据）、studio-table（流程图行）

- 查询参数：
  - `filter[<field>]=<value>`：只允许视图声明的 `filters` 字段；
  - `q?`：全文；
  - `stale?: bool`；
  - `sort?`、`order?`；分页。
- 响应：`{items: ArtifactRow[], next_cursor, total, facets: {[field]: [{value, label, count}]}}`。
  - 每项带 `status`（经 `status.map` 映射成界面文案）、`stale`、`locked`。
- 错误：`artifact_type_unknown`、`invalid_argument`。

#### AR-03 产物详情

`GET /api/v1/projects/{id}/artifacts/{type}/{key}` · 所有者 · 旧：— · 审计：否 · 依据：5.9.4 · 页面：scenarios、flowchart、studio-table、scripts（求解契约之外的产物）

- 查询参数：`rows_limit?`：表格类产物（csv、jsonl）预览行数，默认 20，最大 500。
- 响应：`Artifact`，包括：
  - `fields`、`sections`（Markdown 按标题切段）；
  - `table`（表格类）、`refs`（解析后的条目引用）；
  - `status`、`stale`、`stale_reason`；
  - `revision`、`lock`、`format_version`。
- `diagram.flowchart` 的 `content` 是 `FlowchartModel`。
- 错误：`artifact_type_unknown`、`artifact_not_found`。

#### AR-04 产物历史

`GET /api/v1/projects/{id}/artifacts/{type}/{key}/history` · 所有者 · 旧：— · 审计：否 · 依据：5.9.4 · 页面：scenarios（详情侧栏 history）

- 分页。
- 响应：`{items: [{commit, version_label, at, source, summary}], next_cursor}`。

#### AR-05 保存产物

`PUT /api/v1/projects/{id}/artifacts/{type}/{key}` · 所有者 · 旧：— · 审计：是 · 依据：5.9.3 规则 3、5.9.4 · 页面：scenarios（编辑场景、示例数据）、flowchart（保存）、scripts（求解契约以外）

- 请求：`{base_revision, fields?: object, sections?: {[title]: string}, content?: any, summary?: string}`。
  - `fields`、`sections` 用于 md-frontmatter；`content` 用于 yaml、json、jsonl、csv、drawio。
- 响应：`{artifact: Artifact, commit, version_label, warnings: Finding[]}`。
  - `warnings` 是包声明的保存门禁（`on: save`）与提交门禁（`on: commit`）没过的规则：`{code: "pack_gate", level: "warning", message, field, gate, gate_label}`。只提示，照常保存（F3b：建模过程中检查只提示，发布时才拦）。
- 错误：
  - `artifact_schema_invalid`：字段定义不合法（解析不了、类型不对、取值不在枚举里），照旧拒绝；
  - `revision_conflict`、`write_scope_locked`；
  - `artifact_readonly`、`format_migration_required`。

#### AR-06 新建产物

`POST /api/v1/projects/{id}/artifacts/{type}` · 所有者 · 旧：— · 审计：是 · 依据：5.9.4 · 页面：scenarios、studio-table（新建流程图，经 `ST-09`）

- 请求：`{key?: string, fields?, sections?, content?}`。路径只能落在 `ext/<包名>/` 下。
- 响应 `201`：同 `AR-05`（含 `warnings`）。

#### AR-07 删除产物

`DELETE /api/v1/projects/{id}/artifacts/{type}/{key}` · 所有者 · 旧：— · 审计：是 · 依据：5.9.4 · 页面：scenarios、studio-table（删除流程图）

- 请求：`{base_revision}`。
- 响应：`{commit, version_label}`。

#### AR-08 校验产物

`POST /api/v1/projects/{id}/artifacts/{type}/{key}/validate` · 所有者 · 旧：— · 审计：否 · 依据：5.9.4 · 页面：flowchart（「校验」）、studio-table（流程图「校验流程图」）

- 请求：`{content?: any}`。不带时校验已保存版本。
- 按格式定义和包声明的确定性规则校验，不调模型。
- 响应：`{ok: bool, errors: Finding[], warnings: Finding[]}`。`ok` 只看字段定义与格式自带的检查；包声明的门禁没过的规则在 `warnings`（`code: "pack_gate"`），不影响 `ok`（F3b）。
- 流程图的全量刷新在对话里提，不在这里。

#### AR-09 产物汇总

`GET /api/v1/projects/{id}/artifacts/{type}/summary` · 所有者 · 旧：— · 审计：否 · 依据：5.9.5（通用视图） · 页面：scenarios（通过率、通过 / 失败 / 过期计数、趋势）

- 查询参数：`group_by?`（默认 `status`）、`trend?: bool`。
- 响应：`{total, by: [{value, label, count}], stale: int, trend: [{version_label, value_pct}]|null, delta_vs_previous_pct: number|null}`。

#### AR-10 交给 drawio 高级编辑器的 XML

`POST /api/v1/projects/{id}/artifacts/{type}/{key}/drawio` · 所有者 · 旧：— · 审计：否 · 依据：5.9.5 · 页面：flowchart（切到「drawio 高级编辑」）

- 请求：`{content?: FlowchartModel}`。只用于 drawio 格式的产物。
- 响应：`{xml: string, revision: string}`，不落盘。
  - 不带 `content`：文件原文。
  - 带 `content`（标准编辑器里还没保存的模型）：把模型合进原文，和 `AR-05` 标准编辑器保存写出的一样——只改第一页里内核认得的节点、连线与流程元数据，尺寸、样式、泳道、批注、其他页原样保留。
- 错误：`artifact_type_unknown`、`artifact_not_found`、`artifact_schema_invalid`（模型不合法）、`invalid_argument`（不是 drawio 格式）。

---

### 3.18 脚本与求解契约

脚本属于五元组，内核本来就能读。求解契约是受管目录，加硬锁（详设 5.9.8），由内核的求解模块和共享契约库处理（方案 A10、A16）。代码生成页的扫描、生成、修复、重新生成都只发命令（`S-20`）。

#### SC-01 脚本与可代码化候选

`GET /api/v1/projects/{id}/scripts` · 所有者 · 旧：— · 审计：否 · 依据：5.4、5.7（共享契约库）、⑳ · 页面：scripts

- 响应：`{sandbox: SandboxSummary, candidates: {scanned_at, items: [{operation: EntryRef, reason, suitable: "yes"|"partial"|"no", selected_default: bool}], unsuitable: [{operation: EntryRef, reason}]}, items: ScriptRow[]}`。
  - `candidates` 来自最近一份代码生成扫描报告。
  - `ScriptRow` 含：脚本条目、对应操作、契约检查（`passed|failed`，带 `findings`）、描述符（`synced|stale|none`）、沙箱验证（`passed|failed|not_run`，带耗时）。

#### SC-02 沙箱验证结果

`GET /api/v1/projects/{id}/scripts/{key}/verification` · 所有者 · 旧：— · 审计：否 · 依据：㉑、5.4 · 页面：scripts（沙箱验证输出卡）

- 响应：`{script: EntryRef, ran_at, status: "passed"|"failed"|"not_run", duration_ms, network_access: false, cases: [{args: string, output: string, passed: bool}]}`。
- 错误：`not_found`。

#### SC-03 沙箱状态（摘要）

`GET /api/v1/sandbox/status` · 登录 · 旧：`GET /api/settings/sandbox`（管理员全量见 `SY-10`） · 审计：否 · 依据：5.7 · 页面：scripts（「脚本验证沙箱可用」）

- 响应：`{available: bool, network: "denied", python: string|null, checked_at}`。

#### SV-01 求解契约

`GET /api/v1/projects/{id}/solver/contract` · 所有者 · 旧：`GET /api/projects/{id}/package/solver-contract` · 审计：否 · 依据：5.9.8、方案 A10、ADR 0009/0010/0011 · 页面：scripts（求解模型页签）

- 响应：`SolverContractView`，包括：
  - `revision`、`revision_no`、`enabled`；
  - `operations[]`：每项是一份「求解契约」，界面上一张卡，含 `solver_key`、`operation`、`problem_family`、`capabilities`、`field_units`、`evaluator`、`option_labels`、`result_title`、`result_texts`、`enabled`、`mode: "template"|"manual"`、`sidecars`、`managed`；
  - `findings`、`gate`；
  - `receipt`：本修订的验证回执状态；
  - 目录：`available_operations`、`evaluators`、`problem_families[{id, label, capabilities}]`、`supported_profiles`、`template`；
  - `lock: ScopeLock|null`。

#### SV-02 保存求解契约

`PUT /api/v1/projects/{id}/solver/contract` · 所有者 · 旧：`PUT /api/projects/{id}/package/solver-contract` · 审计：是 · 依据：5.9.8、方案 A10 · 页面：scripts（「保存求解契约」、「转为手动编辑」后保存）

- 请求：`{base_revision, contract: {operations: [{binding, template?: object, sidecars?: {role: text}}]}, summary?: string（≤80，写进版本记录）}`。
- 经写入路径提交到受管目录 `ontology/solver/`，修订号加 1。旧修订的验证回执对新修订不再有效。
- 保存后由代码生成包的命令生成评估器，不在这里。
- 响应：`SolverContractView`，加 `commit`、`version_label`。
- 错误：`revision_conflict`（`details.kind="solver_contract"`）、`solver_contract_invalid`、`solver_contract_rejected`、`write_scope_locked`。

#### SV-03 检查求解契约（不落盘）

`POST /api/v1/projects/{id}/solver/contract/check` · 所有者 · 旧：`POST /api/projects/{id}/package/solver-contract/check` · 审计：否 · 依据：ADR 0009 · 页面：scripts（检查卡）

- 请求：同 `SV-02`，忽略 `base_revision`。
- 响应：`{rejected: Finding[], findings: Finding[], rendered: [{role: text}|null], evaluator: {kind: "declarative"|"script", constraints: int}|null}`。

#### SV-04 验证回执（已退役）

ADR-0098：端到端验证在发布后的独立评测里做，onto 不再收回执。`GET /api/v1/projects/{id}/solver/receipts` 已删除，
旧路径 `GET /api/projects/{id}/package/solver-validations/latest` 回 `410 endpoint_removed`。`SV-01` 的响应去掉
`gate`、`receipt`，改带 `requires_validation_after_publish: bool`（启用了需要验证的求解契约时为 true，发布时要确认）。

---

### 3.19 证据源

依据详设 5.8、14#4、#5、方案 E11～E15。

- 数据源在平台级登记（代码里没有租户概念，「租户级」即平台全局），项目级授权。
- 凭据只写不读，明文只在连接器网关。
- 采集由内核签发一次性票据，经网关只读执行，全程审计。
- 资料页的 3 个子页（数据源、采集与快照、证据浏览器）由系统逆向建模包带来。包没启用时，项目侧接口返回 `pack_not_enabled`。
- 界面叫法：「证据源」「证据源提供方」叫「多源连接器」，单个插件按它接什么起名（「PDF 转换」「业务数据库」）；本节接口路径不变。

#### EV-01 数据源列表

`GET /api/v1/evidence/sources` · 登录 · 旧：— · 审计：否 · 依据：5.8.7 · 页面：sources、wb-materials（「已登记 2 个数据源可用」）、create（第 2 步）、proj-settings（自主构建生产库授权）

- 查询参数：`project_id?`（带上时返回 `authorized_for_project`）、`filter?`（`all|authorized|production`）。
- 响应：`{items: EvidenceSource[], total, authorized_count, my_requests?}`。带 `project_id` 时多 `my_requests`：本人在这个项目提交、还没通过的登记申请 `[{id, source_id, display_name, provider, state: "pending"|"rejected", requested_at, note}]`（`note` 是退回原因）。
- 普通用户只看到不敏感的登记字段；`secret_fields` 只有是否已设置的标记。

#### EV-02 登记数据源

`POST /api/v1/admin/evidence/sources` · admin；自助类提供方（`self_service=true`，如 REA）登录即可（F13） · 旧：— · 审计：专项 `evidence.source.register` · 依据：5.8.7、5.8.6 · 页面：sources（登记抽屉「保存登记并授权本项目」）

- 请求：

  | 字段 | 类型 | 必填 | 说明 |
  |---|---|---|---|
  | `id` | string | 是 | `^[a-z0-9][a-z0-9-]{1,40}$`，锚点前缀，登记后不能改 |
  | `display_name` | string | 是 | — |
  | `provider` | string | 是 | 已安装的提供方 id |
  | `environment` | string | 是 | `production\|test` |
  | `connection` | object | 是 | 不含密钥的连接参数，字段由提供方的 `cred_fields` 决定，如 `{host, port, database, username}` |
  | `secret` | object | 否 | 如 `{password}`；内核立即转交网关 `PUT /v1/secrets`，不保存，日志只记引用 |
  | `scope_allowlist` | string[] | 是 | 只采这些对象 |
  | `egress` | `{host, port}[]` | 是 | 网关只连白名单里的主机和端口 |
  | `access_window` | object | 否 | `{days: "daily"\|string[], start: "22:00", end: "06:00", tz}` |
  | `budget` | object | 是 | `{max_rows, max_bytes, max_duration_s}` |
  | `rate_limit` | object | 是 | `{concurrency, qps, query_timeout_s}` |
  | `masking_policy` | string | 否 | 只能是提供方支持的：数据库 `mask-v1`，其他（REA、代码仓库）`none`；缺省按提供方，填别的回 400 `fields.masking_policy=unsupported`（F16） |
  | `authorization_note` | string | REA 类必填 | — |
  | `owner` | string | 是 | 负责人用户名 |
  | `grant_project_id` | string | 否 | 登记后立即授权给这个项目 |

- 自助类提供方（F13，ADR-0082）：`owner` 固定为本人；`connection` 是 `{artifact_sha256, artifact_filename, artifact_size}`（先用 `EV-31`～`EV-33`
  上传目标程序）；`scope_allowlist`、`egress`、`access_window`、`rate_limit`、`budget`、`masking_policy` 可省略，按提供方缺省；`environment` 缺省 `test`；登记只有本人和管理员看得到。
- 连接型提供方（`input=connection`，数据库、代码仓库，F16）：只有管理员能登记；`connection` 只收提供方 `cred_fields` 里的非密钥字段，
  都必填，密钥字段放进来回 400（不回显值）；`port` 是 1～65535 的整数；`repo_url` 只认 https，不能带用户名和令牌。`egress` 必填，
  且要含连接目标（数据库的 `host:port`，代码仓库的仓库主机 443 或地址里写的端口），不含回 400 `fields.egress=missing_target`；
  `scope_allowlist` 数据库必填（写法 `crm.*`、`crm.customer`、`erp.store*`，`*` 只在末尾），代码仓库可空；`budget`、`rate_limit` 各项是正数。
- 带 `secret` 时先登记、再转交网关写凭据；网关不通或拒绝时撤回登记，回 `gateway_unavailable` 或网关的错误码（见 `EV-06`）。
- 响应 `201`：`EvidenceSource`（`cred_ref` 如 `cred://mes-prod/1`）。
- 错误：`source_id_taken`、`provider_not_installed`、`gateway_unavailable`、`invalid_argument`、`artifact_not_found`（自助类，程序没上传完）、`forbidden`（非管理员登记要凭据的提供方）。

#### EV-03 提交登记申请

`POST /api/v1/evidence/source-requests` · 登录 · 旧：— · 审计：是 · 依据：5.8.7 · 页面：sources（非管理员「提交给管理员」）

- 请求：同 `EV-02`，但不能带 `secret`；`project_id?` 是申请人的项目（要是所有者），管理员通过时登记并授权给它。
- 响应 `202`：`{request_id}`。管理员收到 `request_pending` 通知（`action` 指向插件安装页的「申请」页签），在 `AD-09` 写入凭据后登记。

#### EV-04 修改登记

`PATCH /api/v1/admin/evidence/sources/{source_id}` · admin；自助类数据源的登记人（F13） · 旧：— · 审计：专项 `evidence.source.update` · 依据：5.8.7 · 页面：sources（⋯「编辑登记」）

- 请求：`EV-02` 里除 `id`、`provider` 外的任意字段。自助类换版本就是把 `connection` 换成新上传的程序，旧快照不动。
- 连接型（F16）按合并后的登记整体再核一遍 `EV-02` 的规则（换了主机，出口白名单要跟着换）；改 `connection` 时网关要通（要按它的
  `cred_fields` 校验），否则 503。带 `secret`（只有管理员）时先改登记再写凭据，写不进去改动撤回。
- 响应：`EvidenceSource`。

#### EV-05 删除登记

`DELETE /api/v1/admin/evidence/sources/{source_id}` · admin；自助类数据源的登记人（F13） · 旧：— · 审计：专项 `evidence.source.delete` · 依据：5.8.7 · 页面：sources（⋯「删除登记」）

- 已有快照保留，锚点照常解析。
- 响应 `204`。
- 错误：`job_state_invalid`（有运行中的任务）。

#### EV-06 写入或轮换凭据

`PUT /api/v1/admin/evidence/sources/{source_id}/secret` · admin · 旧：— · 审计：专项 `evidence.secret.write`（只记引用） · 依据：5.8.6 · 页面：sources（「轮换」「写入新密码」）

- 请求：`{fields: {password?: string, token?: string, ...}}`。字段名只能是提供方 `cred_fields` 里 `secret=true` 的，值是非空文字（最长 8192）。
- 内核把明文立即转交网关 `PUT /v1/secrets/{源}`（票据 op `secret`），自己只存新的 `cred_ref` 和这一版各字段的写入时间；明文不进日志、
  审计、任务文件、错误信息。新版本只含这次写的字段，`EvidenceSource.secret_fields` 按这一版标 `set`。
- 新任务用新版本；旧版本等没有任务在用时由网关删除。
- 响应：`{cred_ref: "cred://mes-prod/4", written_at}`。没有任何能读出明文的接口。
- 错误：`gateway_unavailable`、`upstream_unavailable`（网关鉴权失败或 5xx，`details.gateway_error`）；网关回 400、404、409、413、422 时
  原样带回它的错误码，消息里万一回显了明文换成「«已遮蔽»」；`invalid_argument`（字段不对，或这类数据源不用凭据）；`forbidden`。

#### EV-07 删除凭据

`DELETE /api/v1/admin/evidence/sources/{source_id}/secret` · admin · 旧：— · 审计：专项 `evidence.secret.delete` · 依据：5.8.6 · 页面：sources

- 网关 `DELETE /v1/secrets/{源}` 删所有版本，`cred_ref` 置空；网关里本来就没有（404）也算删掉。`EV-05` 删有凭据的登记时也先删凭据，
  网关不通就不删登记（503）。
- 响应 `204`。

#### EV-08 测试连接

`POST /api/v1/evidence/sources/{source_id}/test` · admin，或已获授权项目的所有者（带 `project_id`） · 旧：— · 审计：专项 `evidence.test` · 依据：5.8.2（测试连接并自检只读） · 页面：sources（「测试已授权的连接」、⋯「测试连接」、抽屉「重新测试」）

- 请求：`{project_id?: string}`。
- 响应：`{ok: bool, checks: [{name: "connectivity"|"readonly_account"|"statement_allowlist", ok, detail, latency_ms?}], ticket: {id_prefix, expires_in_s: 600, consumed: true}}`。
- 错误：`source_not_authorized`、`gateway_unavailable`、`connection_test_failed`（`ok=false` 时也可以直接返回 200，前端按 `ok` 判断；返回 422 只在网关报错时）。
- 旁路容器类（REA，F13）：只查提供方装没装、目标程序在不在 blob 仓，`checks` 只有 `connectivity`，不签票据，`ticket` 为 `null`；自助类数据源的登记人不带 `project_id` 也能测。
- 连接型（F16）：网关 `POST /v1/sources/{源}/test`（票据 op `test`，范围是 `TestSpec` 的哈希），`ok`、`checks` 原样带回（代码仓库不一定
  三项都有，项里可能多 `code` 等字段）。
  `ticket.id_prefix` 是票据 jti 的前 8 位。还没写凭据时不调网关，回 `ok=false`、`checks` 只有 `connectivity`（「还没写入凭据」）、`ticket=null`。
  网关拒绝回 422 `connection_test_failed`（`details.gateway_error`），网关不通 503。

#### EV-09 访问审计

`GET /api/v1/evidence/sources/{source_id}/access-log` · admin，或已获授权项目的所有者（只看本项目的访问） · 旧：— · 审计：否 · 依据：5.8.2（每次访问业务系统都留记录） · 页面：sources（⋯「查看访问审计」）

- 查询参数：`project_id?`；分页。
- 响应：`{items: [{at, job_id, project_id, operation, statement_digest, rows, bytes, duration_ms}], next_cursor}`。
- 连接型（F16）：每条是快照包清单 `access_log` 里的一次访问（`statement_digest` 如 `select:3f2a…`），`at` 没给时取任务开始时间；
  上传类每个任务一条（`statement_digest` 是 `artifact:<sha256 前 12 位>`）。新的在前。

#### EV-10 提供方列表

`GET /api/v1/evidence/providers` · 登录 · 旧：— · 审计：否 · 依据：5.8.3 · 页面：sources（登记抽屉类型格）、snapshots（「已加载 N 个提供方」）

- 响应：`{gateway: {status: "ok"|"slow"|"down", checked_at}, items: [{id, version, label, kind: "file"|"db"|"code"|"api"|"rea"|"ui"|"cli"|"log", evidence_types: string[], capabilities: ("discover"|"snapshot"|"sample"|"incremental")[], cred_fields: [{name, secret: bool, label}], readonly_method, runtime: "subprocess"|"sidecar"|"mcp", installed: bool, license_note, self_service: bool, input: "artifact"|"connection", max_artifact_bytes: int\|null}]}`。
- `self_service`：不要凭据、不连业务网络，登录用户可以自助登记（F13）；`input=artifact` 表示要上传目标程序（`EV-31`～`EV-33`）。

#### EV-11 本项目的授权

`GET /api/v1/projects/{id}/evidence/grants`、`PUT /api/v1/projects/{id}/evidence/grants/{source_id}` · 所有者 · 旧：— · 审计：PUT 专项 `evidence.grant` · 依据：14#4 · 页面：sources（「授权本项目使用」开关、⋯「取消授权」）

- GET 响应：`{items: [{source_id, authorized: bool, granted_at, granted_by}]}`。
- PUT 请求：`{authorized: bool}`。
  - 授权记录存数据目录，不放工作区（agent 能改工作区，方案 E11）。
  - 自主构建的生产库采集授权另在 `AB-02` 设置。
- 响应：GET 列表里的一项。

#### EV-12 本项目在用

`GET /api/v1/projects/{id}/evidence/summary` · 所有者 · 旧：— · 审计：否 · 依据：5.8.7 · 页面：sources（「本项目在用」卡）、snapshots（统计条）

- 响应：`{authorized_sources: EvidenceSourceRef[], snapshots: {count, sources, referenced, last_at}, referenced_anchors: int, jobs_this_month: {total, succeeded, partial, failed, cancelled}, running_jobs: int, queued_jobs: int, gateway: {status, providers_loaded}}`。

#### EV-13 采集预览

`POST /api/v1/projects/{id}/evidence/jobs/preview` · 所有者 · 旧：— · 审计：否 · 依据：5.8.7、14#5 · 页面：snapshots（「发起采集」→ 生产采集确认弹窗）

- 请求：`{source_id, operation: "discover"|"snapshot"|"sample", scope?: string[]（缺省沿用登记白名单）, incremental_base?: string（快照 ID）, budget?: object, sample?: {selector, limit ≤ 20}}`。
- 响应：

  | 字段 | 类型 | 说明 |
  |---|---|---|
  | `requires_confirmation` | bool | 生产环境为 true |
  | `confirmation_token` | string \| null | 10 分钟有效 |
  | `summary` | object | `{scope_text, tables, window, in_window_now, rate_limit, estimate: {rows, bytes}\|null, readonly_methods: string[], masking: {policy, sample_rows}, ticket: {ttl_s: 600, single_use: true}}` |

- `scope` 逐条核对落在登记白名单里（`crm.customer`、`crm.c*` 都在 `crm.*` 里），不在回 400 `fields.scope=outside_allowlist`；登记没写白名单时不限。
- `operation=sample`（F16）：`sample.selector` 是范围写法里的单个对象名 `库.表`（不带 `*`），要落在登记白名单和本次范围里，否则 400；
  `sample.limit` 1～20，缺省 20。
- `summary.window` 是 `EvidenceSource.access_window` 同形的对象或 `null`。
- `summary.tables`、`summary.estimate` 来自这个源最近一次 `discover` 的清单（按本次范围筛），没做过 discover 为 `null`；
  `masking.sample_rows` 数据库是每表样本行数（sample 时是 `limit`），其他为 0。
- 错误：`source_not_authorized`、`provider_not_installed`、`invalid_argument`。

#### EV-14 发起采集

`POST /api/v1/projects/{id}/evidence/jobs` · 所有者 · 旧：— · 审计：专项 `evidence.collect`（含同意记录） · 依据：5.8.7、㉜、14#5 · 页面：snapshots（「同意并开始采集」）、chat-states（采集确认卡经 `S-11` 回答后由内核发起）

- 请求：同 `EV-13`，加 `confirmation_token?`。
- 生产环境没带有效 token 时返回 `428 confirmation_required`，`details` 同 `EV-13` 的响应。
- 不在访问窗口内的任务进入排队，等窗口开启。
- 连接型（F16）：任务说明带凭据引用、出口白名单、限流、脱敏策略和项目的盐（`design/evidence-collection.md` §8）；登记还没写凭据时任务
  直接失败（`credential_missing`，动作有「联系管理员」）。`discover` 只存清单、不提交快照，`result` 没有 `snapshot_id`，有
  `inventory: {items, skipped}`；`sample` 入库成补充快照（不完整，`scope` 是那张表），网关成功就是 `succeeded`。
- 响应 `202`：`CollectionJob`。进度经项目流 `evidence.job.updated` 推送。
- 错误：`confirmation_required`、`confirmation_expired`、`source_not_authorized`、`quota_exceeded`（采集频率）、`gateway_unavailable`。

#### EV-15 采集任务列表

`GET /api/v1/projects/{id}/evidence/jobs` · 所有者 · 旧：— · 审计：否 · 依据：5.8.2 · 页面：snapshots

- 查询参数：`state?`（`active` 即排队和运行中、`finished`，或具体状态值）；分页。
- 响应：`{items: CollectionJob[], next_cursor}`。

#### EV-16 采集任务详情

`GET /api/v1/projects/{id}/evidence/jobs/{job_id}` · 所有者 · 旧：— · 审计：否 · 依据：5.8.2 · 页面：snapshots

- 响应：`CollectionJob`。

#### EV-17 取消、续采、重试

`POST /api/v1/projects/{id}/evidence/jobs/{job_id}/cancel`、`.../resume`、`.../retry` · 所有者 · 旧：— · 审计：专项 `evidence.collect` · 依据：5.8.2（按实体记断点，可续采） · 页面：snapshots

- `cancel`：排队或运行中可用。
- `resume`：部分完成可用，从断点续采，生成新任务，`resumed_from` 指向原任务。
- `retry`：失败或已取消可用，生成新任务；生产环境要重新确认，返回 `428`。
- 响应：`CollectionJob`。
- 错误：`job_state_invalid`、`confirmation_required`。
- 「联系管理员」（凭据过期）用 `EV-30`。

#### EV-18 快照列表

`GET /api/v1/projects/{id}/evidence/snapshots` · 所有者 · 旧：— · 审计：否 · 依据：5.8.4 · 页面：snapshots、schema（快照下拉）

- 查询参数：`source_id?`；分页。
- 响应：`{items: SnapshotSummary[], next_cursor}`。

#### EV-19 快照详情

`GET /api/v1/projects/{id}/evidence/snapshots/{snapshot_id}` · 所有者 · 旧：— · 审计：否 · 依据：5.8.4 · 页面：snapshots、schema

- 响应：`SnapshotSummary`，加 `manifest: object`（`manifest.json` 去掉哈希和凭据引用之外的内容）、`inventory: {collected, skipped: [{object, reason}]}`。

#### EV-20 清理快照

`DELETE /api/v1/projects/{id}/evidence/snapshots/{snapshot_id}` · 所有者 · 旧：— · 审计：是 · 依据：5.8.4（被已确认产物引用的快照不能清理） · 页面：snapshots（「清理」）

- 响应：`{removed: true, blob_retention_days: 90}`。
- 「引用」只算已确认产物（`ontology/`）：frontmatter `evidence_snapshots` 声明了这份快照的；它是这个源最新的完整快照时，再加上引用这个源的锚点却没声明快照的（F13）。`SnapshotSummary.referenced`、`EV-21` 的 `touching_confirmed` 同一口径。
- 错误：`snapshot_referenced`。

#### EV-21 快照对比

`GET /api/v1/projects/{id}/evidence/snapshots/diff` · 所有者 · 旧：— · 审计：专项 `evidence.query` · 依据：5.8.4（按记录 ID + 哈希比较）、`evidence.diff` · 页面：snapshots（对比卡）、schema（「对比快照」）

- 查询参数：`from`、`to`（快照 ID，同一数据源）、`change?`（`added|removed|modified`）；分页。
- 响应：`{from, to, counts: {added, removed, modified, touching_confirmed}, items: [{change, type, anchor, summary, affected: (EntryRef|ArtifactRef)[]}], next_cursor}`。

#### EV-22 推送建议修订

`POST /api/v1/projects/{id}/evidence/snapshots/diff/suggestions` · 所有者 · 旧：— · 审计：是 · 依据：5.8.8（快照差异驱动演进） · 页面：snapshots（「推送建议修订」）

- 请求：`{from, to}`。
- 变化的锚点进影响分析，生成建议修订，放进待处理。
- 响应：`{created: int, inbox_item_ids: string[]}`。
- 「在对话里分析影响」用 `S-20` 发命令。

#### EV-23 分层摘要

`GET /api/v1/projects/{id}/evidence/summary-tree` · 所有者 · 旧：— · 审计：专项 `evidence.query` · 依据：5.8.4（`summary.md`） · 页面：schema（左栏）

- 查询参数：`snapshot`（必填）。
- 响应：`{snapshot: SnapshotRef, domains: [{name, count, entities: [{anchor, name, label, mapped_to: EntryRef|null}]}]}`。
- 数据库快照（F16）：`domains` 按 `<库>`（MySQL 的库、PostgreSQL 的 schema）分组，`entities` 是表和视图（按表名排），每项多 `type`（`entity`）和 `kind`（`table|view`），`label` 没有时取表注释。
- 非数据库快照（如 REA，F13）：`domains` 按记录的 `attrs.module` 分组，`entities` 列这一组的实体和操作记录（模块记录本身就是组，不再列），每项多 `type`（`entity|operation`）。

#### EV-24 证据检索（evidence.search）

`GET /api/v1/projects/{id}/evidence/search` · 所有者 · 旧：— · 审计：专项 `evidence.query` · 依据：5.8.7、㉟ · 页面：schema（检索框）

- 查询参数：`q`、`type?`（`entity|field|relation|constraint|operation|rule|flow|profile|sample|term`）、`source_id?`、`snapshot?`；分页（默认 50，上限 200）。
- 响应：`{items: [{anchor, type, name, label, parent_anchor, snippet, confidence}], next_cursor, total}`。

#### EV-25 记录详情（evidence.show）

`GET /api/v1/projects/{id}/evidence/record` · 所有者 · 旧：— · 审计：专项 `evidence.query` · 依据：5.8.5 规则 4 · 页面：schema（右栏）、studio（证据卡「查看原文」）

- 查询参数：`anchor`、`snapshot?`（缺省用产物声明的快照，没有则取最新完整快照）、`path?`（引用这个锚点的产物路径，按它 frontmatter 的 `evidence_snapshots` 取快照，F13）。
- 响应：`EvidenceRecord`，即记录本身，加原始上下文：字段、DDL、脱敏样本（每表最多 20 行）、数据画像（低基数 top-k、数值统计量）。
- 数据库记录（F16）：表和视图给 `fields`（全部列，按列序，最多 1000；`type` 取列记录顶层的 `data_type`，`note` 是列注释，`masked` 是
  脱敏标记，多可选的 `pk`、`fk`、`nullable`：`pk` 来自列的标记或主键约束，`fk` 是这一列出现在本表声明外键的 `attrs.columns` 里；视图没有
  列记录，按视图的 `attrs.columns` 列出，`anchor` 为空）、`ddl`（表是生成的建表语句，视图是定义）、`samples`、`profile`、`rows`。
  `samples` 优先取这个源最新的补充样本（sample 任务），没有就用当前快照里的样本，多 `masked_columns`、`snapshot_id`（样本来自哪份快照）；
  单元格都是文字，空值是空串。`profile` 的比例单位是**百分比**（一位小数）：记录里是整数比例（`attrs.pct_scale` 分之几，缺省千分之几），
  `top_k` 记录里是 `{value, count}` 时按画像行数算；每项多 `null_pct`、`distinct`；有统计量时才给 `stats`（`min`、`max` 原样，可能是文字）。
  `rows` 取画像的行数，没有画像取表的估算行数。列记录顶层多 `masked`、`data_type`、`nullable`。
- 代码记录（F16）：同非数据库记录，`attrs` 原样带 `snippet`、`loc`；`fields` 列下级的字段、方法和校验规则（`rule`，按锚点的路径类型区分），
  `type` 先取顶层 `data_type`，再取签名、`attrs.type`、`attrs.kind`；有 `nullable` 时带上。
- 非数据库记录（F13）：`attrs` 带 `loc`、`module` 等（`design/evidence-collection.md` §1）；`fields` 列它的成员（类的字段和方法，最多 50 条，`type` 填签名或 `attrs.kind`）。
  快照清单标了 `untrusted_content` 时，响应顶层带 `untrusted_content: true`（`EV-23`、`EV-24`、`EV-27` 同）。
- 错误：`anchor_invalid`、`anchor_not_found`。

#### EV-26 被谁引用（evidence.where-used）

`GET /api/v1/projects/{id}/evidence/where-used` · 所有者 · 旧：— · 审计：专项 `evidence.query` · 依据：5.8.7 · 页面：schema（「被引用」）、studio（证据卡「被谁引用」）、snapshots（被引用数）

- 查询参数：`anchor`、`prefix?: bool`（true 时匹配锚点前缀，如整张表）。
- 响应：`{items: [{target: EntryRef|ArtifactRef, location, anchor}], total}`，不分页，上限 500。`location` 是 `<文件>:L<行号>`，`anchor` 是命中的锚点（`prefix=true` 时有用，F13）。

#### EV-27 相邻记录（evidence.lineage）

`GET /api/v1/projects/{id}/evidence/lineage` · 所有者 · 旧：— · 审计：专项 `evidence.query` · 依据：5.8.7 · 页面：schema（「相邻记录」）

- 查询参数：`anchor`、`snapshot?`。
- 响应：`{items: [{anchor, label, basis: string（如「外键邻居」「取值重合 98%」「代码调用方」）, confidence}]}`。
- 数据库锚点（F16）：表给外键连着的表（`basis`「外键邻居」，被引用的一侧是「外键邻居（被引用）」）和推断关系连着的表（`basis`：有
  `attrs.overlap_permille` 时「取值重合 98%」，否则按 `attrs.basis`：`naming`「命名推断」）；列先给所在的表（「上级」），再给它参与的
  关系连着的表。每项多 `relation`（关系锚点）、`columns`（`[[本表列, 对方列]]`）。关系记录的 `name` 是 `references`（「引用」）。

#### EV-28 ER 视图

`GET /api/v1/projects/{id}/evidence/er` · 所有者 · 旧：— · 审计：专项 `evidence.query` · 依据：5.6（视图选择：Schema 浏览器、ER 视图） · 页面：schema（中栏）

- 查询参数：`snapshot`、`focus?: anchor`、`depth?: 1–3`（默认 1）、`domain?`。
- 响应：`{tables: [{anchor, name, label, fields: [{name, type, pk: bool, fk: bool, masked: bool}]}], relations: [{from_anchor, to_anchor, confidence: "declared"|"observed"|"inferred", basis}]}`，表数上限 60。
- F16：只列表（不含视图）；`domain` 是 `<库>`；`focus` 可以是表或列的锚点，从它所在的表按关系走 `depth` 步（广度优先，近的在前），
  不给 `focus` 时按表名排。`pk` 来自列的标记或主键约束，`fk` 只算声明的外键（推断关系不算）。响应多 `total`（截断前的表数）、
  `truncated`；`relations` 每项多 `anchor`（关系锚点）、`columns`（`[[本表列, 对方列]]`，前端据此把连线落到列上），`basis` 同 `EV-27`，
  只给两端都在本次表里的。非数据库快照回 `{tables: [], relations: []}`。
- 错误：`snapshot_not_found`、`invalid_argument`（`depth` 不在 1～3）、`anchor_invalid`（`focus` 不是数据库锚点）、`anchor_not_found`（`focus` 的表不在快照里）。

#### EV-29 锚点解析

`GET /api/v1/projects/{id}/anchors/resolve`、`POST /api/v1/projects/{id}/anchors/resolve` · 所有者 · 旧：— · 审计：`db`、`code` 等系统锚点记专项 `evidence.query`；`file` 锚点不记 · 依据：5.8.5 · 页面：全部出现出处角标的页面（wb-l2、wb-materials、inbox、l4-run、studio、studio-table、reports、incremental、scenarios、knowledge 等，悬停看原文）

- GET 查询参数：`anchor`、`path?`。
- POST 请求：`{anchors: string[], path?: string}`，最多 100 个。
- `path`（F13）：引用这些锚点的产物；`bin:` 锚点先按它 frontmatter 声明的快照解析，没声明取这个源最新的完整快照。
  `bin:` 锚点的 `source_label` 是数据源显示名，`location_label` 是记录的 `attrs.loc`（`文件:行号`），`text` 是记录摘要，`page="schema"`；
  快照里没有时 `reason=not_in_snapshot`。`db`、`code` 锚点（F16）：工作区里有这个源的快照时同样走证据索引，`location_label` 数据库是
  `<库>.<表>[.<列>]`、代码是 `文件:行号`；没有快照时仍回 `not_available`。其余系统锚点仍回 `not_available`。
- 响应：`{items: AnchorResolution[]}`。
  - 每项含：来源名、位置、原文（带高亮区间）、置信度、快照、是否可解析。
  - 解析失败的锚点 `resolved=false`，并带 `reason`。

#### EV-30 通知管理员处理数据源

`POST /api/v1/evidence/sources/{source_id}/notify-admin` · 已获授权项目的所有者 · 旧：— · 审计：是 · 依据：5.8.6（凭据由管理员轮换） · 页面：snapshots（失败任务「联系管理员」）

- 请求：`{project_id, job_id?: string, reason: "credential_expired"|"connection_failed"|"other", note?: string}`。
- 管理员收到 `request_pending` 通知。
- 响应 `202`：`{request_id}`。
- 错误：`source_not_authorized`、`source_not_found`。

#### EV-31 开始上传目标程序

`POST /api/v1/evidence/uploads` · 登录 · 旧：— · 审计：否 · 依据：ADR-0082 · 页面：sources（登记抽屉「上传程序」、⋯「上传新版本」）

- 请求：`{filename: string, size: int}`。
- 响应 `201`：`{upload_id, chunk_bytes: 8388608, max_bytes}`。
- 原样存进数据目录的 blob 仓，不进工作区，主容器不解析内容。每人同时最多 2 个没完成的上传，24 小时没完成自动清掉。
- 错误：`upload_too_large`（413，`details.max_bytes`）、`quota_exceeded`（未完成的上传太多）、`invalid_argument`。

#### EV-32 上传一块

`PUT /api/v1/evidence/uploads/{upload_id}/chunks/{index}` · 上传人 · 旧：— · 审计：否 · 依据：ADR-0082

- 请求体是第 `index` 块（从 0 开始）的字节，除最后一块外都是 `chunk_bytes`。同一块可以重传，以最后一次为准。
- 响应：`{received_bytes, received_chunks}`。
- 错误：`upload_not_found`（404）、`invalid_argument`（块大小不对、序号越界）。

#### EV-33 完成上传

`POST /api/v1/evidence/uploads/{upload_id}/complete` · 上传人 · 旧：— · 审计：是 · 依据：ADR-0082

- 请求：`{sha256: string}`。
- 响应：`{artifact: {sha256, size, filename}}`；同一内容已经在 blob 仓里就直接用。
- 错误：`upload_not_found`、`upload_incomplete`（409，`details.missing: int[]`）、`upload_hash_mismatch`（422）。

#### EV-34 取消上传

`DELETE /api/v1/evidence/uploads/{upload_id}` · 上传人 · 旧：— · 审计：否 · 依据：ADR-0082 · 页面：sources（上传中「取消」）

- 删掉没完成的上传的临时目录，释放「每人同时最多 2 个」的名额。
- 响应 `204`。
- 错误：`upload_not_found`（不存在、已过期、不是本人的，或已经完成）。

---

### 3.20 发布

依据详设 5.6（门禁链）、⑱、第 11 章、方案 A8、A9。

- 发布从主线提交出（只读树），变更集永远不发布。
- 发布格式自动判定：启用了求解契约就发求解知识包（schema 0.2），否则发普通知识包（ontology-package v0.1），不再手选。
- 版本号由知识包 Hub 分配（`0.1.N`，ADR-0098），不再按主线计数；包根目录与 manifest `id` 是 Hub 包身份。
  Hub 不可用时发布失败（`503 hub_unavailable`）。同一 `request_id` 重试得到同一个版本与同一份字节。

#### PB-01 发布检查（门禁链）

`GET /api/v1/projects/{id}/publish/check` · 所有者 · 旧：`GET /api/projects/{id}/package/check` · 审计：否 · 依据：5.6、㉖、第 11 章 · 页面：publish、projects（「可发布」状态）

- 查询参数：`commit?`（缺省主线最新）、`refresh?: bool`。
- 响应：

  | 字段 | 类型 | 说明 |
  |---|---|---|
  | `commit` | string | — |
  | `version_label` | string | — |
  | `package_name` | string | — |
  | `format` | string | `ontology\|solver`：自动判定 |
  | `gates` | `GateResult[]` | 按顺序：建模阶段（四个必经阶段没做完时才出现，`id=modeling_stages`，挡发布、不能降级，带 `missing_stages`）、数据绑定（没有问题、但有对象既没绑表也没声明不落库时 `status=warn`，summary「N 个对象没有绑定数据表，数据问答查不到它们」，不挡发布）、求解契约（静态检查；`requires_acknowledgement` 为 true 时发布要确认未验证；当前修订已在评测服务通过求解验证的为 false 并带 `verified_revision`，要统一账号登录才判定）、知识包结构（含 consumer 导入规则）、包声明的门禁（如合规检查）、证据出处检查、流程关卡 |
  | `solver_revision` | string | 求解契约修订（没有求解契约时为空） |
  | `solver` | object | develop 发布检查同名字段：`{enabled, revision, operations, findings, blocking, requires_validation_after_publish, truncated, verified_release?}`；没有求解契约时 `{enabled: false}`；当前修订已通过求解验证时 `requires_validation_after_publish=false` 并带 `verified_release: {release_id, version}`（要统一账号登录才判定） |
  | `structure` | object | consumer 导入规则的结构自检：`{blocking, findings: [{code, message, level}], truncated, skipped}`；求解契约有问题或渲染失败时 `skipped` 说明跳过的原因 |
  | `stages` | object | 发布前必须完成的建模阶段（ADR-0098，与 develop 同规则）：`{ready, stages: [{key, title, finished}], missing_stages: [{key, title}]}`；四个阶段是资料分析 research、对象提取 object、操作提取 operation（含旧 logic 目录）、质量检查 validate |
  | `last_publish` | object \| null | 上次发布：`{at, by, degraded, unbound_count, version}` |
  | `blocking_count` | int | — |
  | `binding` | object | `{issues: BindingIssue[], warnings: string[], tables: string[], table_count, objects, bound_objects, table_resources: string[]（没标 Schema 的表格资料）, truncated}` |
  | `degradable` | bool | 只有数据绑定不通过时为 true |
  | `uncommitted` | object | `{count, paths: string[≤50]}`：发布源路径下的未提交改动（发布不会带上它们） |
  | `last_release` | `ReleaseRecord` \| null | — |
  | `checked_at` | time | — |
  | `duration_ms` | int | — |

#### PB-02 设置对象数据来源

`PUT /api/v1/projects/{id}/objects/{key}/data-source` · 所有者 · 旧：`POST /api/projects/{id}/objects/{key}/data-source` · 审计：是 · 依据：5.8.8 · 页面：publish（数据绑定「改为现有表」「改为「无」」）

- 请求：`{kind: "table"|"anchor"|"none", value?: string, base_revision}`。
  - `table`：首行只写现有数据表名，逐字相同，大小写规范成原名；
  - `anchor`：写数据库锚点，如 `db:pos-prod/pos.store_grade`，表名列名从快照解析；
  - `none`：确实不落库的对象。
- 响应：`{entry: EntryRef, revision, commit, version_label, binding: BindingIssue|null}`。
- 错误：`not_found`、`invalid_argument`（表名不在 Schema 资料和快照里）、`anchor_not_found`、`revision_conflict`、`write_scope_locked`。

#### PB-03 发布

`POST /api/v1/projects/{id}/releases` · 所有者 · 旧：`GET /api/projects/{id}/export?scope=package`（旧接口用 GET 发布，改为 POST） · 审计：专项 `publish.release` · 依据：⑱、㉒、㉓、方案 A8 · 页面：publish（「发布为知识包」「发布」「降级发布」）

- 要统一账号登录（Hub 按登录用户授权）；鉴权关闭的本地模式返回 `401`。
- 建模没做完（PB-01 `stages.ready=false`）先回 `409 modeling_stages_incomplete`，`message` 为「建模未完成，请先完成：资料分析、质量检查」这样的句子，`details.missing_stages` 列出缺的阶段；MCP 发布与 Hub 发布同一规则，不能绕过。
- 请求：`{commit?: string（缺省主线最新）, degrade?: bool, request_id?: string（幂等键，缺省每次新请求）, solver_unverified_acknowledged?: bool}`。
  - `degrade=true` 表示忽略数据绑定、降级发布，其他门禁不放宽；
  - 求解契约需要验证时要 `solver_unverified_acknowledged=true`，当前修订已在早先版本通过求解验证的除外。
- 流程：门禁 → 冻结只读树 → Hub 预留版本号 → 渲染 → 上传、提交到 Hub → 本地留存、打 `release/<版本>` 标签 → 写发布记录。
- 响应 `201`：`ReleaseRecord`，另带 `hub: {ontology_id, release_id, package_digest, committed_at}`；Hub 已提交而本地标签没写成时
  为 `{project_id, version, local_record_pending: true, hub}`，后台恢复。
  另带 `hub_members`（同 HB-11 的 `hub_sync`）：项目第一次发布时初始化共享授权的结果，`status=failed` 时页面提示去「知识包共享」重试，发布照常成功。
  评测服务配好时，发布后带着用户登录回写一次评测优化的结果，成功拿到状态的在响应里带 `evaluation_writeback`（ES-02 同结构），失败不影响发布。
- 错误：
  - `gate_blocked`（`details.gates` 列出不通过的项；consumer 导入规则不通过时 `details.reason=package_structure_invalid`）；
  - `modeling_stages_incomplete`（409）；
  - `hub_package_deleting`（409）：项目关联的 Hub 知识包删除中或已删除（develop !590），共享的改动、MCP 发布预览同一错误码；
  - `solver_validation_acknowledgement_required`（409）；
  - `publish_preview_stale`（409，同一 `request_id` 的内容变了）；
  - `hub_unavailable`（503）、`version_conflict`、`package_deleted`；
  - `invalid_commit`；
  - `project_archived`；
  - `maintenance_read_only`。

#### PB-04 发布记录

`GET /api/v1/projects/{id}/releases` · 所有者 · 旧：— · 审计：否 · 依据：第 11 章 ㉓ · 页面：publish（发布记录）

- 分页。按发布时间从新到旧。
- 响应：`{items: ReleaseRecord[], next_cursor}`。`ReleaseRecord.hub` 是 Hub 包身份 `{ontology_id, release_id, package_digest?}`，
  旧版本地发布的记录为 null。

#### PB-05 发布记录详情

`GET /api/v1/projects/{id}/releases/{version}` · 所有者或服务令牌 `releases.read` · 旧：— · 审计：否 · 依据：㉓ · 页面：publish

- 响应：`ReleaseRecord`。
- 错误：`release_not_found`。

#### PB-06 包清单

`GET /api/v1/projects/{id}/releases/{version}/manifest` · 所有者或服务令牌 `releases.read` · 旧：— · 审计：否 · 依据：㉓（包出：版本化知识包 + 包清单） · 页面：publish

- 响应：`ReleaseManifest`。

#### PB-07 下载发布包

`GET /api/v1/projects/{id}/releases/{version}/download` · 所有者（可用 `?token=`）或服务令牌 `releases.read` · 旧：`GET /api/projects/{id}/export?scope=package` · 审计：专项 `publish.download` · 依据：㉒、㉓、方案第 6 章 #1（图模式留作适配器变体） · 页面：publish

- 查询参数：`variant?`：`standard`（默认）或 `graph`。
  - `graph` 是图模式，给评测服务导入 Neo4j 用，由发布适配器按同一提交渲染，不另打标签。
- 响应：`application/zip`。响应头带 `X-Package-Version`、`X-Package-Commit`、`X-Package-Format`、`X-Package-Variant`。
- 读发布时留存的那份（与 Hub 上的字节相同）；留存缺失时，带登录令牌的请求从 Hub 下回缓存，服务令牌的请求按标签提交重新渲染（记 `backfilled`）。
- 服务令牌下载时，在发布记录上记「已拉取」。
- 错误：`release_not_found`、`invalid_argument`（变体不支持）。

#### HB-01～HB-09 知识包 Hub

ADR-0098。都要统一账号登录（`401 unauthenticated`），Hub 的错误原样透出（`hub_unavailable` 503、`not_found` 404 等）。

| 编号 | 方法与路径 | 说明 |
|---|---|---|
| HB-01 | `GET /api/v1/hub/packages?offset&limit` | 本人可见的 Hub 包，每个包带最新版本 |
| HB-02 | `GET /api/v1/hub/packages/{package_id}/releases?offset&limit` | 包的版本列表 |
| HB-03 | `GET /api/v1/hub/packages/{package_id}/releases/{release_id}/download` | 下载，核对 Hub 给的摘要 |
| HB-04 | `POST /api/v1/hub/imports` | 从 Hub 导入成新项目，请求 `{request_id, ontology_id, release_id, package_digest, name}`，同一 `request_id` 重试得到同一个项目；响应 `201` 项目元数据 |
| HB-05 | `GET /api/v1/projects/{id}/hub/releases?offset&limit` | 项目关联的 Hub 包的全部版本（含别人发布的）；响应 `{items, releases, total, …}` |
| HB-06 | `GET /api/v1/projects/{id}/hub/releases/{release_id}/download` | 下载项目关联包的某个版本 |
| HB-07 | `GET /api/v1/projects/{id}/hub/resources/status`、`GET …/hub/resources?offset&limit` | 共享资料同步状态与 Hub 上的当前集合 |
| HB-08 | `PUT /api/v1/projects/{id}/hub/resources` | 请求 `{paths: string[], confirm_shared: true}`：把选中的资料同步成 Hub 包的共享资料（整个集合替换），要先发布过 |
| HB-09 | `GET /api/v1/projects/{id}/hub/resources/{file_id}/download` | 下载一份共享资料 |
| HB-10 | `GET /api/v1/projects/{id}/hub/delete-preview` | 删除项目前看它的 Hub 包：`{published: false}` 或 `{published: true, ontology_id, name, state?}`（`state` 为 `deleting` / `deleted` 表示上次删到一半或远端已删）；发布状态不明确或关联不一致时 409（develop 的 `hub-delete-preview`） |
| HB-11 | `GET`、`PUT /api/v1/projects/{id}/hub/sharing` | 知识包共享用户（develop 的成员共享，本分支在知识包这一层，ADR-0098）：响应 `{owner, users: string[], hub_sync: {initialized, pending: {用户名: grant\|revoke}, results: {用户名: {action, status: pending\|synced\|failed, error?}}, status: synced\|failed\|not_published}}`；PUT 请求 `{users: string[]}`（最多 200 个用户名），已发布时按差异排授权 / 撤权并立即同步，失败不回滚名单；删除处理中 409 |
| HB-12 | `POST /api/v1/projects/{id}/hub/sharing/retry?username=` | 按用户重试授权 / 撤权（develop 的 `members/hub-retry`），不给 `username` 重试全部；响应同 HB-11 |

外部路径（评测服务调用，不进 `/api/v1`、不改名）：`GET /api/hub/package-projects?ontology_id=…`（1～20 个），用户令牌，
返回 `{items: [{ontology_id, projects: string[≤5], total}]}`：当前用户可见、且关联到这些 Hub 包的建模项目名称。

#### 知识包版本对比（PC 组）

依据 ADR-0095。参数、分页与响应照 develop（`routers/projects.py` 的 `package-comparisons/*`），路径在 `/api/v1` 下。

- 来源：Hub 上的发布版本 `{kind: "release", ontology_id?|hub_package_id?, release_id}`（缺包标识时取项目关联的 Hub 包；当前用户在 Hub 上可见的版本都能选），
  本项目发布版本的简写 `{kind: "release", version}`（按发布记录里的 Hub 身份换成前一种），或这次上传的包 `{kind: "upload", input_id}`。
- 要统一账号登录（Hub 身份，鉴权关闭的本地模式 `401`），再过所有者守卫。比较数据按 Hub 主体（[签发方, sub]）隔离。
- 只读，不改项目内容。发起对比时用发起人的令牌从 Hub 下载发布包并冻结；读结果、文件列表、正文差异，以及同一 `request_id` 重放时，回 Hub 复核读权限（无权时透出 Hub 的错误）。
- 上传输入、对比结果 24 小时过期（最近一次读取起算）。上限与 TTL 见环境变量 `ONTO_COMPARE_*`。时间字段是 epoch 秒。
- ZIP 在子进程里解析，拒绝路径穿越、符号链接、加密条目、异常压缩比、包根外的文件、重复或非字符串的 manifest 键；manifest 必须是
  `kind: ontology-package`，`schema_version` 0.1 或 0.2。
- 两侧 manifest `id`（发布版本取 Hub 回执的 `manifest_id`）不同算跨包，要带 `allow_cross_package`，否则任务失败，`error` 为「两侧属于不同 Hub 知识包，请确认跨包对比」。
- `ComparisonSide`：`{source, package_name, package_version, package_digest, file_count, uncompressed_size, version, committed_at}`；
  `version`、`committed_at` 只有发布版本才有。
- `CompareFile`：`{path, category, status: added|deleted|modified|unchanged, display_name, left: FileInfo|null, right: FileInfo|null}`，
  `FileInfo` 是 `{path, size, digest, category, display_name}`。
  - `category` 取 `objects|operations|rules|metrics|scripts|mappings|indexes|metadata|other`；
  - `display_name` 是 Markdown 头部的 `report_name` 或 `title`，没有时取文件名。

#### PC-01 上传对比输入

`POST /api/v1/projects/{id}/package-comparisons/inputs` · 所有者（统一账号） · 旧：— · 审计：是 · 依据：ADR-0095 · 页面：publish（版本对比「导入知识包 › 选择 ZIP」）

- 请求：`multipart/form-data`，字段 `file`。请求头 `X-Compare-Upload-Id?`（`[A-Za-z0-9_-]{1,128}`）用于取消（PC-02）和防重复提交。
- 响应 `201`：`{input_id, package_id, package_name, package_version, package_digest, content_digest, file_count, uncompressed_size, created_at, expires_at, warnings: []}`。
- 错误：`invalid_argument`（不是 ZIP、路径不安全、manifest 不合规、没有可比内容）、`payload_too_large`、`compare_upload_in_progress`、
  `compare_upload_cancelled`、`compare_upload_busy`、`compare_user_storage_full`、`compare_storage_full`、`compare_timeout`。

#### PC-02 取消上传

`DELETE /api/v1/projects/{id}/package-comparisons/uploads/{request_id}` · 所有者（统一账号） · 旧：— · 审计：是 · 依据：ADR-0095 · 页面：publish

- `request_id` 是 PC-01 的 `X-Compare-Upload-Id`；取消可以先于上传请求到达。
- 响应：`{cancelled: true}`。

#### PC-03 删除对比输入

`DELETE /api/v1/projects/{id}/package-comparisons/inputs/{input_id}` · 所有者（统一账号） · 旧：— · 审计：是 · 依据：ADR-0095 · 页面：publish

- 响应：`{deleted: true}`。
- 错误：`compare_input_in_use`（先删用到它的对比）、`forbidden`、`compare_expired`。

#### PC-04 发起对比

`POST /api/v1/projects/{id}/package-comparisons` · 所有者（统一账号） · 旧：— · 审计：是 · 依据：ADR-0095 · 页面：publish

- 请求：`{left: Source, right: Source, allow_cross_package?: bool, request_id?: string}`。
  - `Source` 见上；发布版本的 `release_id` 与 `version` 二选一，多余字段 400；
  - 同一 `request_id` 带同样的来源重放，先回 Hub 复核读权限，再返回同一个任务（PC-05 的视图）；来源不同返回 `compare_request_conflict`。
- 响应 `202`：`{comparison_id, status: "queued", phase, poll_after_ms}`。
- 错误：`invalid_argument`（两侧相同、来源不合法、发布版本缺少 Hub 身份）、`release_not_found`（`version` 简写找不到）、`payload_too_large`（发布包超过对比上限）、
  `compare_user_busy`、`compare_busy`、`compare_request_conflict`、`compare_user_storage_full`、`compare_storage_full`、`compare_service_unavailable`，以及 Hub 透出的错误。

#### PC-05 对比结果

`GET /api/v1/projects/{id}/package-comparisons/{comparison_id}` · 所有者（统一账号） · 旧：— · 审计：否 · 依据：ADR-0095 · 页面：publish

- 进行中、失败：`{comparison_id, status: queued|running|failed, phase, error?, error_code?}`；服务重启时没跑完的任务改判失败，`error_code="PROCESS_RESTARTED"`。
- 完成：`{comparison_id, status: "ready", created_at, left: ComparisonSide, right: ComparisonSide, summary}`。
  - `summary`：`{files: {added, deleted, modified, unchanged}, categories: {<category>: 同上}, affected_categories[], evidence[≤4], left_file_count, right_file_count}`。
- 错误：`compare_expired`、`forbidden`，以及复核时 Hub 透出的错误。

#### PC-06 差异文件

`GET /api/v1/projects/{id}/package-comparisons/{comparison_id}/files` · 所有者（统一账号） · 旧：— · 审计：否 · 依据：ADR-0095 · 页面：publish

- 查询参数：`status?`（`changed` = 新增、删除、修改，或四种状态之一；缺省全部）、`category?`、`q?`（路径或业务名称，≤200 字）、`offset`（≥0，默认 0）、`limit`（1～200，默认 50）。
- 响应：`{items: CompareFile[], total, filtered_total, overall_total, offset, limit}`；`total`、`filtered_total` 是筛选后的数量，`overall_total` 是全部文件数。
- 错误：`invalid_argument`（分页参数无效）、`compare_not_ready`、`compare_expired`。

#### PC-07 单文件差异

`GET /api/v1/projects/{id}/package-comparisons/{comparison_id}/file-diff` · 所有者（统一账号） · 旧：— · 审计：否 · 依据：ADR-0095 · 页面：publish

- 查询参数：`file_id`（`CompareFile.path`）。
- 响应：`{path, status, category, left: FileText|null, right: FileText|null}`，`null` 表示这一侧没有该文件。
  - `FileText` 是 `{text, encoding, bytes, reason?}`；
  - `encoding` 取 `utf-8|utf-8-sig|utf-16`，或不给正文的 `binary`、`too_large`（超过 1 MiB）、`complex_text`（超过 2 万行或单行 2 万字）。
- 错误：`not_found`、`compare_not_ready`、`compare_expired`。

#### PC-08 删除对比

`DELETE /api/v1/projects/{id}/package-comparisons/{comparison_id}` · 所有者（统一账号） · 旧：— · 审计：是 · 依据：ADR-0095 · 页面：publish

- 还在跑的一并取消。响应：`{deleted: true}`。
- 错误：`compare_expired`、`forbidden`。

---

### 3.21 三份契约（服务令牌）

依据详设第 11 章、方案 A2、A7、第 6 章 #6；ADR-0098 起正式版本由知识包 Hub 分发、求解验证在评测服务里做。

#### X-01 发布列表（跨项目，已退役）

ADR-0098：评测服务、consumer 从 Hub 取正式版本。`GET /api/v1/contract/releases` 已删除。

#### X-02 包清单（服务令牌）

即 `PB-06`。服务令牌可调，令牌限定了项目时校验项目。

#### X-03 下载发布包（服务令牌）

即 `PB-07`。服务令牌可调；每次下载在发布记录上写 `pulls`。

#### X-04 提交验证回执（已退役）

ADR-0098：`POST /api/v1/projects/{id}/solver/receipts` 与求解验证候选包 `GET /api/v1/projects/{id}/solver/candidate` 已删除，
服务令牌权限 `receipts.write` 不再签发。

#### X-05 提交外部反馈（已退役，ADR-0092）

原 `POST /api/v1/projects/{id}/feedback`（服务令牌 `feedback.write`）没有调用方，已删除，`feedback.write` 不再签发。外部反馈批次改由 `ES-07` 评测优化写入，
落盘格式仍是 `onto-feedback/v1`（见 `ES-07`），资料区外部反馈分组、待处理、增量更新的外部反馈卡照旧读它。

#### X-06 服务令牌管理

`GET /api/v1/admin/service-tokens`、`POST /api/v1/admin/service-tokens`、`DELETE /api/v1/admin/service-tokens/{token_id}` · admin · 旧：— · 审计：POST、DELETE 记 · 依据：方案第 6 章 #6 · 页面：—（设计稿没有画，见 8.2）

- GET 响应：`{items: [{id, name, scopes, project_ids: string[]|null, created_at, created_by, expires_at, last_used_at}]}`。
- POST 请求：`{name, scopes: "releases.read"[], project_ids?: string[], expires_in_s?: int|null}`。
  - 响应 `201`：`{token: "onto_svc_...", summary}`，明文只出现一次。
- DELETE 响应 `204`。

---

### 3.21a 独立评测（ADR-0092）

评测在独立的评测服务里跑。onto 只做四件事：给浏览器入口（`ES-01`）、收完成通知（`ES-00`）、以当前用户自己的登录读回结果（`ES-02`～`ES-06`）、
把一轮的结果整理成一批外部反馈交给反馈演进（`ES-07`）。评测记录按发起人私有：同一个 Hub 包由多人评测时，各人只看见自己的轮次。
本地模式（没有统一账号）不能用评测：`ES-02`～`ES-08` 与 develop 一样返回 401（码 `login_required`），页面按「评测状态读取失败」显示为评测服务未配置。

#### ES-00 完成通知（评测服务调用）

`POST /api/internal/evaluation/notifications` · 服务凭据（请求头 `X-Evaluation-Service-Token`，值为 `ONTO_EVALUATION_SERVICE_TOKEN`），不经登录 · 旧：同路径 · 审计：否 · 页面：—

- 路径是评测服务调用的外部路径，一字不改。评测服务重试到收到 2xx 为止：先落盘再应答，重复、迟到的通知同样应答。
- 请求：评测服务的 `Notification`（`event_id = run_id:sequence`、`sequence`、`owner`、`ontology_id`、`release_id`、`version`、`run_id`、`status`、
  `run_type`、`artifacts`、`summary`、`origin`、`optimization`、`solver_summary`），未知字段忽略。
- 响应 `200`：`{accepted: true}`。凭据不对 `401`；请求体不合格 `400 invalid_argument`（评测服务把 400、413、422 都当拒收，不再重试）。

#### ES-01 评测入口

`GET /api/v1/evaluation/entry` · 登录用户 · 旧：`GET /api/settings/evaluation/entry` · 审计：否 · 页面：publish

- 响应：`{url}`，取自部署配置 `ONTO_WEB_EVAL_PUBLIC_URL`；空串表示浏览器按建模页面同主机的 5180 端口打开。没有页面修改入口：这个地址会收到用户登录。
- 浏览器打开 `{url}?ontology_id=&release_id=&package_digest=[&run_type=solver]`，登录经窗口消息交给评测页面的来源，不进地址。
- 错误：配置无效 `500 internal_error`。

#### ES-02 评测状态

`GET /api/v1/projects/{id}/evaluations` · 所有者 · 旧：`GET /api/projects/{id}/package/evaluations` · 审计：否 · 页面：publish

- 查询参数：`reconcile?: 0|1`，1 时先补写未完成的建模回写，再向评测服务对账（评测服务连不上时 `reachable=false`，返回上次同步的状态）。
- 响应：`{configured, reachable: bool|null, message, ontology_id, current_release_id, current_version, active, releases, feedback, solver_releases,
  solver_revisions, writeback, evolution}`。`evolution` 表示反馈演进包启用与否，没启用时页面不出评测优化。`releases`、`feedback`、`solver_releases` 按 `release_id` 给每个版本最近一次已生成报告的评测、用户反馈、求解验证轮次；
  `writeback` 是最近一次评测优化与它的回写状态（`waiting|pending|done|refused`，`mine` 表示是不是当前用户做的）。

#### ES-03 评测报告

`GET /api/v1/projects/{id}/evaluations/{run_id}/report` · 所有者 · 旧：`…/package/evaluations/{run_id}/report` · 审计：否 · 页面：publish

- 响应：`{run_id, release_id, version, revision, content, truncated}`，最多 2 MiB，在行边界截断。只给评测本项目 Hub 包的轮次。
- 错误：`eval_not_found`、`artifacts_not_ready`，以及 `eval_*`。

#### ES-04 优化建议

`GET /api/v1/projects/{id}/evaluations/{run_id}/suggestions` · 所有者 · 旧：`…/suggestions` · 审计：否 · 页面：publish

- 响应：同 `ES-03`，另有 `status: pending|generating|ready|failed`、`error`；不是 `ready` 时 `content` 为空。

#### ES-05 完整回答

`GET /api/v1/projects/{id}/evaluations/{run_id}/answers/{result_id}` · 所有者 · 旧：`…/answers/{result_id}` · 审计：否 · 页面：publish

- `result_id` 是 `case-N`。响应：`{resultId, question, fullAnswer, originalLength, truncated}`。不写工作区。
- 错误：`answer_not_found`、`artifacts_not_ready`。

#### ES-06 重新生成报告与建议

`POST /api/v1/projects/{id}/evaluations/{run_id}/regenerate` · 所有者 · 旧：`…/regenerate` · 审计：是 · 页面：publish

- 响应：评测服务的 `artifacts`（修订号加一、生成中）；本地索引同时标为生成中，结果随通知写入。

#### ES-07 评测优化

`POST /api/v1/projects/{id}/evaluations/{run_id}/optimization` · 所有者（项目没有归档） · 旧：`…/optimization` · 审计：是 · 页面：publish

- 要求反馈演进包已启用，只对项目当前版本的数据评测或用户反馈轮次。以用户登录取同一修订的报告、完整回答、上下文与优化建议，写进工作区并整理成一批外部反馈：
  - `reports/{stem}.md`、`reports/{stem}.answers.json`、`reports/optimization-{…}.md`（有建议时），和反馈批次 `feedback/{接收时间}-eval-{run_id}-r{修订}.json`
    一起提交（只提交这几个文件）；
  - 评测数据源的表头写成只有表头行的 csv，放 `.ontology-state/eval-service/tables/{stem}/`，上下文放 `.ontology-state/eval-service/{stem}.json`（都不进 git）；
  - 批次格式 `onto-feedback/v1`：`source = {service: "评测服务"|"用户反馈", run: run_id}`，`release_version` 是被评测的版本，`summary` 取自评测汇总，
    `items` 是失分（fail、partial、error）用例（标准答案、工具失败取自报告的逐用例明细），另带 `attachments: {report, answers, suggestions?, tables?}`（工作区相对路径）。
  - 同一修订再点不重复下载、不重复成批；人工改判后修订号加一，再点成一批新的。
- 响应：`{feedback_id, report, suggestions, command: "/ontology-evolution", args: {feedback_id}}`；页面拿 `command`、`args` 经 `S-20` 发到工作台对话。
- 之后发布的第一个新版本，以做优化的用户登录回写给评测服务，作为这一轮的处理结果（发布时带登录就立即写，否则下次 `ES-02 reconcile=1` 时补写）。
- 错误：`pack_not_enabled`（反馈演进包没启用）、`version_mismatch`、`artifacts_not_ready`、`artifacts_changed`、`optimization_not_supported`、`login_required`、`eval_*`。

#### ES-08 标记处理失败

`POST /api/v1/projects/{id}/evaluations/{run_id}/modeling-result` · 所有者（项目没有归档） · 旧：`…/modeling-result` · 审计：是 · 页面：publish

- 请求：`{note: string（1～1000 字）}`。告诉评测服务这一轮的建议没能落实；处理成功不用调，发布新版本自动回写。
- 响应：`{optimization}`（评测服务记下的处理状态）。

#### ES-09 旧版评测记录

`GET /api/v1/projects/{id}/legacy-evaluations` · 所有者 · 旧：`GET /api/projects/{id}/eval/status`（410，successor 指这里） · 审计：否 · 页面：publish

- 评测移出 onto 之前最近一轮的「评测执行详情」（develop 的 `GET /eval/status` 里的 `state`、`evolution`），只读；状态读 `.ontology-state/eval.json`，启动迁移不再删它。
- 响应：`{state, evolution, evolution_enabled}`。`state` 没有旧记录时是 `{}`；仍是 `running` 的显示为 `interrupted`（旧运行器已删，不改盘上记录），`resumable` 恒为 false。
  `evolution` 与 develop 的提示条件相同（得分低于 90 或有异常用例、报告出自最近一轮），`evolution_enabled` 是反馈演进包启用与否。

#### ES-10 旧版评测报告

`GET /api/v1/projects/{id}/legacy-evaluations/{id}` · 所有者 · 审计：否 · 页面：publish

- `{id}` 是报告文件名去掉 `.md`（`eval-<日期>-<时间>`）。响应：`{id, path, content, truncated}`，最多 2 MiB。

#### ES-11 旧版报告的完整回答

`GET /api/v1/projects/{id}/legacy-evaluations/{id}/answers/{result_id}` · 所有者 · 审计：否 · 页面：publish

- 读同名 `.answers.json` 附件（develop 报告预览的首选来源）。响应：同 `ES-05`。错误：`answer_not_found`（「完整回答证据不可用」或「完整回答证据不存在或不属于当前评测轮次」）、`not_found`。

#### ES-12 旧版报告的评测优化

`POST /api/v1/projects/{id}/legacy-evaluations/{id}/optimization` · 所有者（项目没有归档） · 审计：是 · 页面：publish

- 与 `ES-07` 同一做法：把旧报告和它的 `.answers.json` 整理成一批外部反馈 `legacy-{id}`（报告最近一轮时带上状态里的得分汇总），只提交这批；同一份报告只成一批。
- 响应：同 `ES-07`（`suggestions` 为 null）。错误：`pack_not_enabled`、`answer_not_found`、`not_found`。

---

### 3.22 自主构建

依据详设 8.5、1.6#2、方案 B12～B14、C8。

- 运行由会话宿主托管，每段一个变更集，检查通过自动合入，产出标「需复核」。
- 每段合入之后是安全点。
- 运行在自己的对话里进行（会话名「自主构建 · 运行 #n」）。
- 运行中用户发的消息按 `deferred` 处理，下一步开始前读取。

#### AB-01 自主构建策略

`GET /api/v1/projects/{id}/autobuild/policy` · 所有者 · 旧：— · 审计：否 · 依据：8.5 策略表 · 页面：proj-settings（构建方式内嵌策略）、l4-run（开始时的策略说明）

- 响应：`AutobuildPolicy`，加：
  - `platform_limits: {max_duration_min, max_tokens, max_cost: Money, allow_production_collection: bool}`；
  - `quota_remaining: {tokens, cost: Money}`；
  - `last_run: {run_id, run_no, ended_at, duration_ms, reviewed: bool}|null`；
  - `revision`。

#### AB-02 保存策略

`PUT /api/v1/projects/{id}/autobuild/policy` · 所有者 · 旧：— · 审计：是 · 依据：8.5、4.10.1（预算只能设在配额之内） · 页面：proj-settings（「保存」「保存并开始自主构建」）

- 请求：`AutobuildPolicy` 去掉只读字段，加 `base_revision`。
- 硬性不做的几项（发布、删除已确认内容、改资料区、采集未授权的生产库）不在策略里，打不开。
- 响应：同 `AB-01`。
- 错误：`budget_exceeds_quota`、`revision_conflict`、`invalid_argument`、`source_not_authorized`（生产库授权引用了本项目没授权的数据源）。

#### AB-03 开始前预览

`POST /api/v1/projects/{id}/autobuild/runs/preview` · 所有者 · 旧：— · 审计：否 · 依据：8.5「开始前会展示这次的范围和预算」 · 页面：l4-run、wb-blueprint（「按蓝图一路建完」）、proj-settings（「保存并开始自主构建」）、⌘K（开始自主构建）

- 请求：`{policy_overrides?: Partial<AutobuildPolicy>}`。开始前可以临时改。
- 响应：`AutobuildPreview`，即：
  - `preview_token`（10 分钟有效）；
  - 范围：分段列表、每段现状；
  - 预算；
  - 生产库授权；
  - 低于阈值时怎么处理；
  - `warnings`，如「有 1 个未决决策会被按策略决定」「建模蓝图里有 1 个问题没回答，运行不会替你答，会留在待处理里」（蓝图的问题按 BP-03 不代答）；
  - `queued`：平台同时运行数满了时为 true；
  - `busy`：别的对话正占着助手时为 `{conversation_id, waiting}`（`waiting` 为 true 表示它停在没回答的问题上），否则为 null。前端据此在开始前说明，并在 AB-04 带 `take_over`。
- 错误：`autobuild_running`（F8 起不再回 `blueprint_not_confirmed`；`budget_exceeds_quota` F6 已删）。

#### AB-04 开始自主构建

`POST /api/v1/projects/{id}/autobuild/runs` · 所有者 · 旧：— · 审计：专项 `autobuild.start` · 依据：④、8.5 · 页面：l4-run、wb-blueprint、proj-settings、chat-states（对话里说「剩下的你自己做完」时由助手发起确认，用户确认后前端调本接口）

- 请求：`{preview_token, take_over?: boolean}`。`take_over` 为 true 且别的对话正占着助手时，先把没回答的问题撤回（EC-34，`reason`「已交给自主构建，按「拿不准时」的策略处理」）、打断那一轮（EC-05，`by: "autobuild"`），等它停下再开始；不带时照旧回 `session_busy`。
- 运行建好之后接手别的对话里没做完的：
  - 撤回的问题按策略定下来，记进这次运行的台账（`reason_text` 以「开始自主构建时没回答的问题：」开头、待复核）或挂起，答案经每一步的注入带给助手；
  - 别的对话里没做完的执行（被接管打断的、等你回复的、之前中断留着续跑的），这一步运行要做、或开始时已经做完的，记为放弃并清掉命令锁，不再进待处理的续跑、建议条不再推「继续」，那条对话里留一行「「对象提取」交给自主构建 · 运行 #n，这里不用再继续」；运行范围外又没做完的照旧能续跑。
  - 被接管时停在干活中途（命令锁不在「等你确认」「等你回复」）的建模那一步，运行照做、接着做完：写了一半的条目合入后按「条目在」会算这一步做完，不能跳过；
  - 开始失败（合入冲突、配额）时问题已撤回，执行留着中断，照常能续跑。
- 响应 `201`：`AutobuildRun`（`state` 为 `queued` 或 `running`），加 `conversation_id`。
- 错误：`preview_expired`、`autobuild_running`、`session_busy`、`quota_exceeded`、`maintenance_no_new_sessions`。

#### AB-05 运行详情

`GET /api/v1/projects/{id}/autobuild/runs/{run_id}` · 所有者 · 旧：— · 审计：否 · 依据：8.5 · 页面：l4-run（运行条、运行详情抽屉、流水线矩阵）

- 响应：`AutobuildRun`，包括：
  - 状态、当前分段与步骤；
  - 指标：用时与预计、token、费用、写入即查错误与提醒、场景通过、修复轮次；
  - `slices`：每段状态、摘要、版本、矩阵单元格；
  - 自动决定数、挂起数；
  - `recovered_from_checkpoint: int`（崩溃后自动续跑次数）；
  - `report_available`。
- 错误：`run_not_found`。

#### AB-06 暂停

`POST /api/v1/projects/{id}/autobuild/runs/{run_id}/pause` · 所有者 · 旧：— · 审计：专项 `autobuild.control` · 依据：8.5 安全点 · 页面：l4-run（「暂停」）

- 停在当前步骤，以后可以续跑。
- 响应：`AutobuildRun`（`state="pausing"`，到点后变 `paused`）。
- 错误：`autobuild_not_running`。

#### AB-07 停在安全点

`POST /api/v1/projects/{id}/autobuild/runs/{run_id}/stop-at-safe-point` · 所有者 · 旧：— · 审计：专项 `autobuild.control` · 依据：8.5 · 页面：l4-run（「停在安全点」）

- 做完这一段再停。
- 响应：`AutobuildRun`（`state="stopping"`）。

#### AB-08 转为陪伴

`POST /api/v1/projects/{id}/autobuild/runs/{run_id}/convert-to-l2` · 所有者 · 旧：— · 审计：专项 `autobuild.control` · 依据：8.5 · 页面：l4-run（「转为陪伴」「转为陪伴继续」）

- 停在安全点之后，剩下的改成逐个确认。项目默认构建方式不变。
- 响应：`AutobuildRun`（`state="converting"`，到点后变 `converted`）。

#### AB-09 续跑

`POST /api/v1/projects/{id}/autobuild/runs/{run_id}/resume` · 所有者 · 旧：— · 审计：专项 `autobuild.control` · 依据：8.4、8.5 · 页面：l4-run

- 只对 `paused`、`interrupted` 可用。预算按剩余额度继续。
- 响应：`AutobuildRun`。

#### AB-10 决策台账

`GET /api/v1/projects/{id}/autobuild/runs/{run_id}/ledger` · 所有者 · 旧：— · 审计：否 · 依据：8.5（台账存 `reports/autobuild/<运行号>/`，进 git） · 页面：l4-run（运行详情抽屉「自动决策台账」）、inbox（需复核）

- 查询参数：`review_state?`（`pending|accepted|rejudged`）、`below_threshold?: bool`；分页。
- 响应：`{items: LedgerEntry[], next_cursor, counts: {total, high_confidence, below_threshold, assumptions}}`。

#### AB-11 接受自动决定

`POST /api/v1/projects/{id}/autobuild/ledger/{entry_id}/accept` · 所有者 · 旧：— · 审计：是 · 依据：8.5 · 页面：inbox（「接受这个决定」）

- 响应：`LedgerEntry`（`review_state="accepted"`）。受影响产物去掉「需复核」标记。

#### AB-12 改判

`POST /api/v1/projects/{id}/autobuild/ledger/{entry_id}/rejudge` · 所有者 · 旧：— · 审计：是 · 依据：8.5「可事后改判」 · 页面：inbox（「改判为选中的选项」）、l4-run（台账「改判」，跳 inbox）

- 请求：`{option_id?: string, custom_text?: string}`。
- 往对话发重做受影响部分的命令，跑完保存新版本。
- 响应 `202`：`{entry: LedgerEntry, command_task: CommandTask}`。
- 错误：`ledger_entry_not_found`、`answer_invalid`。
- 本次运行还在跑时不报错：改判排到运行结束后执行，返回的 `command_task.state` 为 `queued`。

#### AB-13 一键接受高置信

`POST /api/v1/projects/{id}/autobuild/runs/{run_id}/ledger/accept-high-confidence` · 所有者 · 旧：— · 审计：是 · 依据：8.5 · 页面：inbox（汇总条「一键接受高置信 13 项」）

- 响应：`{accepted: int, threshold: number}`。

#### AB-14 挂起的问题

`GET /api/v1/projects/{id}/autobuild/runs/{run_id}/suspensions` · 所有者 · 旧：— · 审计：否 · 依据：8.5「挂起」 · 页面：l4-run（抽屉「挂起的问题」）、inbox

- 响应：`{items: Suspension[]}`，不分页，上限 200。

#### AB-15 回答挂起问题

`POST /api/v1/projects/{id}/autobuild/suspensions/{suspension_id}/answer` · 所有者 · 旧：— · 审计：是 · 依据：8.5 · 页面：inbox、l4-run（「现在回答」跳 inbox）

- 请求：`{option_id?: string, custom_text?: string, action?: "use_generic"|"defer_to_next_run"}`。
- 陪伴模式下助手马上补上；也可以留到下一次自主构建一起做。
- 响应：`{suspension: Suspension, command_task: CommandTask|null}`。
- 错误：`suspension_not_found`、`answer_invalid`。

#### AB-16 运行报告

`GET /api/v1/projects/{id}/autobuild/runs/{run_id}/report` · 所有者 · 旧：— · 审计：否 · 依据：8.5「运行报告卡」 · 页面：l4-run（报告卡）、inbox（「按段结果」弹窗）

- 响应：`AutobuildReport`，即：
  - 起止时间、重启续跑说明；
  - 指标：用时与预算、分段、产出（对象 / 操作 / 规则）、检查、场景推演、自动决策、挂起、费用与 token；
  - 按段结果：结果、产出、场景、运行后版本、能否撤回；
  - 提示文案。

#### AB-17 按段撤回

`POST /api/v1/projects/{id}/autobuild/runs/{run_id}/slices/{slice_id}/revert` · 所有者 · 旧：— · 审计：专项 `autobuild.revert_slice` · 依据：8.5、方案 C8（反向提交） · 页面：inbox（「回退到运行前」）、l4-run（报告卡「回退这一段」）

- 只动这一段：恢复成运行开始前的样子，另存为新版本，其他分段不受影响。
- 响应：`{commit, version_label, reverted_to_version_label}`。
- 错误：`slice_not_revertible`、`autobuild_running`（本次运行还在跑）。

#### AB-18 运行列表

`GET /api/v1/projects/{id}/autobuild/runs` · 所有者 · 旧：— · 审计：否 · 依据：8.5 · 页面：proj-settings（「上次运行 #2」）、projects（卡片，经 `P-01`）

- 查询参数：`state?`；分页。
- 响应：`{items: AutobuildRunSummary[], next_cursor}`。

---

### 3.23 MCP

依据详设 4.1、②、5.6（只读 MCP 工具）、`docs/decisions/onto-local-mcp.md`。

#### M-01 对外 MCP 概况

`GET /api/v1/projects/{id}/mcp` · 所有者 · 旧：`GET /api/projects/{id}/modeling-mcp` · 审计：否 · 依据：② · 页面：proj-settings（对外 MCP 接口）

- 响应：

  | 字段 | 类型 | 说明 |
  |---|---|---|
  | `status` | string | `running\|disabled` |
  | `base_url` | string | 取 `ONTO_WEB_MCP_PUBLIC_BASE_URL`，没配置时取请求的基地址 |
  | `local_only` | bool | 地址只能本机访问时为 true，界面给出提示 |
  | `endpoints` | `[{toolset: "read"\|"change", transport: "streamable-http"\|"sse", url}]` | — |
  | `toolsets` | `[{name, scope_requirement, accepted_scopes, tools: string[]}]` | — |
  | `pack_tools` | `[{name, scope, read_only: true, pack}]` | 包贡献的只读工具，见 `M-08` |
  | `scope_catalog` | `[{scope, description}]` | — |
  | `token_policy` | object | `{min_ttl_s: 300, default_ttl_s: 86400, max_ttl_s: 2592000, supports_non_expiring: true}` |
  | `protocol_versions` | string[] | — |
  | `client_config_templates` | `{read, change, full}` | JSON 字符串，令牌位置是 `<READ_TOKEN>` 这类占位符 |
  | `recommended_order` | string[] | `["server/discover", "tools/list", "tools/call"]` |
  | `limits` | object | `{page_size_max: 200, body_bytes: 1048576}` |

#### M-02 MCP token 列表

`GET /api/v1/projects/{id}/mcp/tokens` · 所有者 · 旧：`GET /api/projects/{id}/modeling-mcp/tokens` · 审计：否 · 依据：② · 页面：proj-settings

- 响应：`{items: McpTokenSummary[]}`。只列未吊销的，按签发时间倒序，不分页，上限 100。

#### M-03 新建 MCP token

`POST /api/v1/projects/{id}/mcp/tokens` · 所有者 · 旧：`POST /api/projects/{id}/modeling-mcp/tokens` · 审计：专项 `mcp.token.issue` · 依据：② · 页面：proj-settings（新建 Token 弹窗）

- 请求：`{name: string 1–120, scopes: string[]（至少一个，取 ontology.read|ontology.change.validate|ontology.change.apply|evidence.read|project.version.revert|package.publish）, expires_in_s: int 300–2592000 | null（永不过期）}`。
- 响应 `201`：`{token: "onto_pat_...", summary: McpTokenSummary, client_config: {read, change, full}}`。`client_config` 已填入本次 token。明文只出现一次。
- 管理员不能代所有者签发（方案 A11）。

#### M-04 吊销 MCP token

`DELETE /api/v1/projects/{id}/mcp/tokens/{token_id}` · 所有者 · 旧：`DELETE /api/projects/{id}/modeling-mcp/tokens/{token_id}` · 审计：专项 `mcp.token.revoke` · 依据：② · 页面：proj-settings

- 响应 `204`。
- 错误：`not_found`。

#### M-05 MCP 传输：streamable-http

`POST /api/v1/onto-mcp/{id}/{toolset}/mcp` · MCP token · 旧：`POST /api/onto-mcp/{id}/{toolset}/mcp`（永久保留） · 审计：写工具专项 `mcp.write` · 依据：② · 页面：—

- `toolset`：`read|change`。请求体最大 1 MiB。
- 方法：`ping`、`initialize`、`server/discover`、`tools/list`、`tools/call`、`notifications/*`。
- 协议版本：`2026-07-28`、`2025-11-25`、`2025-06-18`。
- 行为、JSON-RPC 信封、错误码与现状一致（2.12）。

#### M-06 MCP 传输：SSE

`GET /api/v1/onto-mcp/{id}/{toolset}/sse` · MCP token · 旧：`GET /api/onto-mcp/{id}/{toolset}/sse`（永久保留） · 审计：否 · 依据：② · 页面：—

- 首个事件 `endpoint`，之后是 `message`。
- 每 15 秒重新校验 token 并发 keep-alive。
- 会话总数上限 128，每个会话的队列上限 64。

#### M-07 MCP 传输：SSE 消息

`POST /api/v1/onto-mcp/{id}/{toolset}/sse/messages?session_id=` · MCP token · 旧：同路径去掉 `/v1`（永久保留） · 审计：写工具专项 `mcp.write` · 依据：② · 页面：—

- 响应 `202`。

#### M-08 MCP 工具目录

依据 4.1#5、5.6、5.8.7、方案 E13。

**现有 15 个，保留，参数与返回不变：**

- `list_ontology_objects`、`get_ontology_object`；
- `list_ontology_operations`、`get_ontology_operation`；
- `list_ontology_rules`、`get_ontology_rule`；
- `validate_ontology_object_change`、`update_ontology_object`；
- `create_ontology_operation`、`update_ontology_operation`、`delete_ontology_operation`；
- `create_ontology_rule`、`update_ontology_rule`、`delete_ontology_rule`；
- `validate_ontology_operation_rule_change`。

写工具改为经内核写入路径（⑤）：

- 写入范围内的条目返回 `PROJECT_EDITING_BUSY`，`details.entities` 列出锁定的条目；
- 提交带 trailer `Onto-Source: mcp`；
- 引用、关系目标找不到、字段被别处引用（`REFERENCE_NOT_FOUND`、`REFERENCE_TYPE_INVALID`、`RELATION_TARGET_INVALID`、`FIELD_REFERENCED`）只放进 `warnings`（`level: "warning"`），不挡写入（F3：建模过程中质量检查只提示，发布时才拦）；删除仍被引用的资源（`RESOURCE_REFERENCED`）照旧拒绝。

**新增只读工具（read 工具集）：**

| 工具 | scope | 等价 REST |
|---|---|---|
| `flow_get` | `ontology.read` | `FL-01` |
| `artifact_list` | `ontology.read` | `AR-02` |
| `artifact_get` | `ontology.read` | `AR-03` |
| `evidence_search` | `evidence.read` | `EV-24` |
| `evidence_show` | `evidence.read` | `EV-25` |
| `evidence_where_used` | `evidence.read` | `EV-26` |
| `evidence_lineage` | `evidence.read` | `EV-27` |
| `evidence_diff` | `evidence.read` | `EV-21` |

- 工具名用下划线，因为 MCP 客户端普遍不接受点号；文档和界面上写作 `flow.get`、`evidence.*`。
- 包贡献的只读工具按 `<包>_<名>` 命名，只读，写操作一律走写入路径。

**版本回退工具（ADR-0094，scope 都是 `project.version.revert`）：**

| 工具 | 工具集 | 等价 REST | 说明 |
|---|---|---|---|
| `list_project_versions` | read | `V-01` | `{limit?: 1–100, cursor?}` → `{revision, current_head, current_version, items: [{version, version_label, commit, time, source, author, message, release, current}], total, next_cursor, display}`；`version` 就是界面 `v0.N` 的 N；游标绑定主线版本，主线前进后回 `CURSOR_REVISION_MISMATCH` |
| `preview_project_version_revert` | change（只读） | `V-05` | `{target_version}` → 能回退时 `{ready: true, preview_id, expires_at, target, current, expected_head, changes: {added, modified, deleted, total}, undone_versions, packs_lock_changes, display}`；有挡住的情况时 `{ready: false, target, current, blockers: [{code, message}]}`，不给 `preview_id` |
| `revert_project_version` | change（破坏性、幂等） | `V-06` | `{preview_id, confirm: true}` → `{commit, reverted_to_version, created_version, version_label, previous_head, display}` |

- `preview_id` 绑定项目、Token、用户、目标版本与预览时的主线版本，只在服务进程内存里，十分钟过期，重启作废；执行过的一小时内用同一 `preview_id` 重放返回同一结果。
- 挡住回退的情况与 `V-05` 同一组判断，MCP 码：`PROJECT_EDITING_BUSY`（助手这一轮没结束、有没跑完的消息、Studio 草稿）、`AUTOBUILD_ACTIVE`、`WORKSPACE_DIRTY`。
- 错误码：`VERSION_NOT_FOUND`、`NOTHING_TO_REVERT`、`REVERT_PREVIEW_EXPIRED`、`REVERT_PREVIEW_FORBIDDEN`、`REVERT_IN_PROGRESS`、`REVERT_PREVIEW_CAPACITY_REACHED`（503）、`REVISION_CONFLICT`（预览之后主线前进了，要重新预览）、`CONFIRMATION_REQUIRED`（`confirm` 不是 true）、`INVALID_CURSOR`。

---

### 3.24 系统设置与管理员

以下都是业务面上的 admin 接口（方案 E8：用户管理、模型和 MCP 配置留在管理员）。

#### SY-01 模型接入

`GET /api/v1/admin/settings/model` · admin · 旧：`GET /api/settings/model` · 审计：否 · 依据：⑧ · 页面：sys（模型接入）

- 响应：`{engines: {[engine]: {label, channel, installed, runtime_version, base_url, api_key_masked, api_key_set: bool, model, context: {window, max_output, compact_at_pct}, context_defaults: {...}}}, default_engine, personal_key_allowed: bool}`。
  - `deploy: {configured: bool, model: string}`：界面没配时在用的部署环境变量（稳定引擎看 `ANTHROPIC_BASE_URL`／`ANTHROPIC_AUTH_TOKEN`／`ANTHROPIC_API_KEY`，内测引擎看 `ONTO_WEB_JIUWEN_BASE_URL`／`ONTO_WEB_JIUWEN_API_KEY`）；只给有没有和模型名，网关地址与 Key 不出接口。

#### SY-02 保存引擎模型配置

`PUT /api/v1/admin/settings/model/{engine}` · admin · 旧：`PUT /api/settings/model`（claude）、`PUT /api/settings/model/jiuwen`、`PUT /api/settings/model/context` · 审计：是（不记 Key） · 依据：⑧、4.10.1 · 页面：sys

- 请求：`{base_url?: string, api_key?: string（留空不改）, model?: string, context?: {window?: int, max_output?: int, compact_at_pct?: int 50–95}}`。
- 新会话生效。在线会话在下一条消息前因指纹变化重连。
- 响应：`SY-01` 里对应引擎的那一项。
- 错误：`invalid_argument`。

#### SY-03 默认引擎

`PUT /api/v1/admin/settings/default-engine` · admin · 旧：`PUT /api/settings/model/default-engine` · 审计：是 · 依据：4.2 · 页面：sys

- 请求：`{engine}`。此后新建或导入的项目用它，已有项目不变。
- 响应：`{default_engine}`。
- 错误：`engine_unavailable`。

#### SY-04 个人 Key 策略

`PUT /api/v1/admin/settings/personal-key-policy` · admin · 旧：`PUT /api/settings/model/personal-policy` · 审计：是 · 依据：⑧ · 页面：sys

- 请求：`{allow: bool}`。从每个用户的下一条消息生效。
- 响应：`{allow}`。

#### SY-05 上下文推荐表

`GET /api/v1/admin/settings/context-presets` · admin · 旧：`GET /api/settings/model/context-presets` · 审计：否 · 依据：— · 页面：sys

- 响应：`{rows: [{model, vendor, window, max_output, note, origin: "builtin"|"override"|"custom", builtin: {window, max_output}|null}], custom: {version, status: "ok"|"invalid"|"disabled", error: {row, column, message}|null, overrides: int, added: int, verified_at}}`。

#### SY-06 保存自定义推荐表

`PUT /api/v1/admin/settings/context-presets/custom` · admin · 旧：`PUT /api/settings/model/context-presets/custom` · 审计：是 · 依据：— · 页面：sys（覆盖、编辑、删除、恢复内置、新增一行）

- 请求：`{version: string（读取时的 custom.version）, presets: [{model, vendor, window, max_output, note}], verified_at?}`。整份替换。
- 「恢复内置」就是从 `presets` 里删掉该模型的覆盖行。
- 已保存的模型配置与上下文参数不受影响。
- 响应：同 `SY-05`。
- 错误：`revision_conflict`（version 不符）、`invalid_argument`。

#### SY-07 停用自定义推荐表

`POST /api/v1/admin/settings/context-presets/custom/disable` · admin · 旧：`POST /api/settings/model/context-presets/custom/disable` · 审计：是 · 依据：— · 页面：sys

- 响应：同 `SY-05`。

#### SY-08 按模型查推荐

`GET /api/v1/admin/settings/context-presets/match` · admin · 旧：`GET /api/settings/model/context-presets/match` · 审计：否 · 依据：— · 页面：sys（填模型名时自动带出）

- 查询参数：`model`。
- 响应：`{model, preset: {window, max_output, origin}|null}`。

#### SY-09 会话与权限

`GET /api/v1/admin/settings/session`、`PUT /api/v1/admin/settings/session` · admin · 旧：`GET/PUT /api/settings/runtime` · 审计：PUT 记 · 依据：4.2、4.8#6、14#8 · 页面：sys（会话与权限）

- GET 响应：`{permission_mode: "bypassPermissions"|"default"|"acceptEdits"|"plan", allowed: string[], pool: {max_sessions, idle_ttl_s, permission_timeout_s, source: "env"}}`。
  - 四种模式对应界面上的：全放行（默认）、逐操作确认、仅自动接受编辑、计划模式。
  - `pool` 只读，来自环境变量，改了要重启。
- PUT 请求：`{permission_mode}`。新会话生效。
- 响应：GET 的形状。

#### SY-10 脚本验证沙箱

`GET /api/v1/admin/settings/sandbox` · admin · 旧：`GET /api/settings/sandbox` · 审计：否 · 依据：5.7、㉑㉚ · 页面：sys（脚本验证沙箱）

- 响应：`{connected: bool, gateway: string, isolation: {non_root, readonly_root, memory_mb, max_procs, network: "denied"}, python: string, checked_at, packages: [{name, version: string|null, purpose}]}`。

#### SY-11 重新检测沙箱

`POST /api/v1/admin/settings/sandbox/probe` · admin · 旧：— · 审计：否 · 依据：5.7 · 页面：sys（「重新检测」）

- 响应：同 `SY-10`。

#### SY-12 全局 MCP 服务器

`GET /api/v1/admin/settings/mcp-servers`、`PUT /api/v1/admin/settings/mcp-servers` · admin · 旧：`GET/PUT /api/settings/mcp` · 审计：PUT 记 · 依据：4.2 · 页面：sys（MCP 服务器）

- GET 响应：`{items: [{name, transport: "http"|"stdio", url?: string, command?: string, auth: {kind: "none"|"bearer", secret_ref?: string}, status: "connected"|"failed"|"unknown", status_detail, tools: [{name, description, read_only}], last_test_at, latency_ms}]}`。
- PUT 请求：`{items: [...]}`，整份替换；鉴权密钥只存引用。只影响新建的会话。
- 响应：GET 的形状。
- 错误：`invalid_argument`。

#### SY-13 测试 MCP 服务器

`POST /api/v1/admin/settings/mcp-servers/test` · admin · 旧：`POST /api/settings/mcp/test` · 审计：否 · 依据：— · 页面：sys（「测试连接」）

- 请求：`{server: {...}}`，形状同 `SY-12` 的一项。
- 响应：`{ok: bool, latency_ms, tools: [{name, description, read_only}], error: string|null}`。

#### SY-14 配置注册表

`GET /api/v1/admin/config-registry` · admin（ops 只读可看） · 旧：— · 审计：否 · 依据：4.10.1、方案 E5 · 页面：sys（配置注册表）

- 查询参数：`effect?`（`immediate|next_message|new_session|restart`）、`q?`。
- 响应：`{precedence: ["env", "data_dir", "pack", "default"], items: ConfigItem[]}`，不分页。
- 只读，配置项在各自的设置页修改。

#### SY-15 配额

`GET /api/v1/admin/quotas`、`PUT /api/v1/admin/quotas` · admin · 旧：— · 审计：PUT 记 · 依据：4.10.1、方案 E9 · 页面：sys（配额与 L4 限制）

- 对象：`{per_user: {concurrent_sessions: int, daily_tokens: int, collections_per_source_per_hour: int}, per_project: {online_engines: 1（只读）, monthly_tokens: int, snapshot_git_mb: int}, overrides: QuotaOverride[]}`。
- PUT 只改 `per_user`、`per_project`，立即生效。
- token 按成本口径计：输入 + 缓存写 + 输出 + 缓存读 // 10（ADR-0088）。默认每人每天 10,000,000、每项目每月 100,000,000。

#### SY-16 配额单独设置

`POST /api/v1/admin/quotas/overrides`、`DELETE /api/v1/admin/quotas/overrides/{override_id}` · admin · 旧：— · 审计：是 · 依据：4.10.1 · 页面：sys（「单独设置」）

- POST 请求：`{subject: {kind: "user"|"project", id}, item: string, value: int, reason: string}`。响应 `201`：`QuotaOverride`。
- DELETE 响应 `204`。

#### SY-17 自主构建平台限制

`GET /api/v1/admin/autobuild-limits`、`PUT /api/v1/admin/autobuild-limits` · admin · 旧：— · 审计：PUT 记 · 依据：8.5、4.10.1 · 页面：sys（L4 自主构建限制）

- 对象：`{max_concurrent_runs: int 0–16, max_duration_min, max_tokens, max_cost: Money, allow_production_collection: bool, night_priority: {enabled: bool, start: "20:00", end: "08:00"}, running: int（只读）}`。
- 立即生效。

#### SY-18 模型单价表

`GET /api/v1/admin/settings/pricing`、`PUT /api/v1/admin/settings/pricing` · admin · 旧：— · 审计：PUT 记 · 依据：方案 B14 · 页面：—（设计稿没有画，见 8.2）

- 对象：`{currency, items: [{engine, model, input_per_mtok, output_per_mtok, cache_read_per_mtok}]}`。

#### AD-01 用户列表

`GET /api/v1/admin/users` · admin、ops（只读） · 旧：`GET /api/admin/users` · 审计：否 · 依据：4.10.1 · 页面：ops-users（用户管理）

- 查询参数：`q?`；分页。
- 响应：`{items: AdminUser[], next_cursor, stats: {accounts, by_role: {user, admin, ops}, new_7d, locked, registration_enabled, auth: {alg: "RS256", issuer_checked: true}}, available: bool}`。
  - `available=false` 表示没配置 auth-server 管理令牌，此时 `items` 为空。

#### AD-02 改角色

`PATCH /api/v1/admin/users/{username}` · admin · 旧：`PUT /api/admin/users/{username}` · 审计：专项 `user.role` · 依据：4.10.1 · 页面：ops-users（「改角色」）

- 请求：`{role: "user"|"admin"|"ops"}`。立即生效，用户的现有令牌刷新后带新角色。
- 响应：`AdminUser`。
- 错误：`user_not_found`、`user_admin_unavailable`、`forbidden`（不能降级最后一个管理员，`details.reason="last_admin"`）。

#### AD-03 重置密码

`POST /api/v1/admin/users/{username}/reset-password` · admin · 旧：`PUT /api/admin/users/{username}`（`password` 字段） · 审计：专项 `user.password_reset` · 依据：4.10.1 · 页面：ops-users（重置密码弹窗）

- 请求：`{password?: string, generate?: bool, require_change: bool}`。
  - `generate=true` 时由服务端生成并在响应里返回一次。
- 响应：`{generated_password?: string}`。
- 错误：`weak_password`、`user_not_found`、`user_admin_unavailable`。

#### AD-04 删除用户

`DELETE /api/v1/admin/users/{username}` · admin · 旧：`DELETE /api/admin/users/{username}` · 审计：专项 `user.delete` · 依据：4.10.1、1.6 · 页面：ops-users（删除用户弹窗）

- 请求：`{projects: "transfer"|"trash", transfer_to?: string}`。
  - `transfer`：项目只归新所有者，原用户的个人 Key 配置不随项目走；
  - `trash`：移入回收站，30 天内可恢复。
- 用户的 MCP token 全部吊销，个人模型配置删除。
- 响应：`{deleted: username, transferred: int, trashed: int}`。
- 错误：`user_not_found`、`transfer_target_invalid`、`user_admin_unavailable`。

#### AD-05 用户名下的项目

`GET /api/v1/admin/users/{username}/projects` · admin · 旧：— · 审计：否 · 依据：1.6 · 页面：ops-users（删除用户弹窗的项目表）

- 响应：`{items: ProjectMetaView[]}`。

#### AD-06 无主项目

`GET /api/v1/admin/projects/unowned` · admin · 旧：—（启动日志里报告） · 审计：否 · 依据：方案 A12 · 页面：—（设计稿没有入口，见 8.2）

- 响应：`{items: ProjectMetaView[]}`。

#### AD-07 指定项目所有者

`PUT /api/v1/admin/projects/{id}/owner` · admin · 旧：`PUT /api/admin/projects/{id}/owner` · 审计：专项 `project.set_owner`（记原值与新值） · 依据：方案 A12 · 页面：—（见 8.2）

- 请求：`{owner: string}`，匹配 `^[A-Za-z0-9_.-]{1,64}$`。
- 响应：`ProjectMetaView`。
- 错误：`invalid_owner`、`project_not_found`。

#### AD-08 待管理员处理的申请

`GET /api/v1/admin/requests` · admin · 旧：— · 审计：否 · 依据：5.5（提名审核）、5.8.7、5.8.3 · 页面：plugin-upload（「申请」页签，经通知进入）

- 查询参数：`kind?`（`pack_install|source_registration|knowledge_nomination`）、`state?`；分页。
- 响应：`{items: [{id, kind, state: "pending"|"approved"|"rejected", requested_by, requested_at, project_id?, summary, payload}], next_cursor}`。

#### AD-09 通过申请

`POST /api/v1/admin/requests/{request_id}/approve` · admin · 旧：— · 审计：是 · 依据：同上 · 页面：—

- 请求：`{note?: string, edits?: object, secret?: object}`。
  - 知识提名通过后记下结论、通知提名人；进行业包下一个版本由行业包维护人做；
  - 数据源申请：`secret` 是凭据（字段同 `EV-06`，提供方要凭据时必填，没带回 `400`，`fields.secret=required`）；按申请（叠加 `edits`）走 `EV-02` 的校验登记，授权给申请人的项目。登记没通过时原样回错，申请仍待处理。
- 响应：申请对象。申请人收到通知。

#### AD-10 退回申请

`POST /api/v1/admin/requests/{request_id}/reject` · admin · 旧：— · 审计：是 · 依据：同上 · 页面：—

- 请求：`{reason: string}`。
- 响应：申请对象。

---

### 3.25 运维管理面

前缀 `{ops_base_url}/api/v1/ops`（1.1），依据详设 4.10.2、4.10.3、㊲、方案 E2、E6～E8。

- 「调用方」写 **ops** 的接口，admin 也能调用只读的 GET；写操作只有 ops 能调，另有说明的除外。
- 所有写操作都写审计，类型 `ops`。
- 项目一律只显示元数据（`ProjectMetaView`），不读内容。

#### O-01 运维总览

`GET /api/v1/ops/overview` · ops、admin · 旧：`GET /api/admin/overview` · 审计：否 · 依据：4.10.3、features「运维总览」 · 页面：ops

- 查询参数：`range?`（`today|7d|14d`，默认 14d）。
- 响应：

  | 字段 | 类型 | 说明 |
  |---|---|---|
  | `stats` | object | `{online_users, accounts, sessions: {active, max, queued, idle_ttl_s}, projects: {total, active, archived}, autobuild: {running, limit}, cost: {total: Money, this_month: Money}, disk: {free_pct, free_bytes, threshold_pct, warn: bool}}` |
  | `advice` | `[{level, text}]` | 如磁盘告警、回收站占用 |
  | `usage_daily` | object | `{days: [{date, calls, cost: Money, output_tokens}], totals: {calls, cost, output_tokens}, avg: {...}, peak: {date, calls, cost, output_tokens}, vs_previous_pct: {calls, cost, output_tokens}}` |
  | `alerts` | `Alert[]` | 触发中和最近恢复的 |
  | `autobuild_runs` | `[{project: ProjectMetaView, run_no, elapsed_ms, progress: {done, total, pct}, budget_used_pct, cost: Money, budget: Money, state}]` | 运行中的 L4 |
  | `autobuild_today` | object | `{completed, avg_duration_ms, avg_cost: Money, stopped_by_budget}` |
  | `startup` | object | `{at, items: [{label, result}]}`：最近一次启动自检摘要 |
  | `users` | `[{username, display_name, online, last_seen_at, calls_today}]` | 用户活跃 |
  | `projects` | `[ProjectMetaView & {disk_bytes, last_activity_at}]` | 项目占用，按磁盘占用倒序 |

#### O-02 指标

`GET /api/v1/ops/metrics` · ops、admin · 旧：— · 审计：否 · 依据：4.10.2、方案 E6 · 页面：ops-obs

- 查询参数：`range?`（`1h|24h|7d`，默认 24h）、`labels?`（如 `project=…,pack=core@1.4.2`，公共标签过滤）。
- 响应：`{cards: [{id, label, value, unit, secondary, note, series: number[12], tone: "normal"|"warn"|"error"}]}`。
- `id` 至少包括：

  | id | 指标 |
  |---|---|
  | `pool` | 会话池与排队 |
  | `turn_duration` | 轮次时长 |
  | `model_latency_p95` | 模型耗时 p95 |
  | `output_tokens` | 输出 tokens |
  | `model_fail_rate` | 模型失败 |
  | `guard_denies` | 守卫拒绝 |
  | `check_failures` | 检查失败 |
  | `merges` | 合入 |
  | `collections` | 证据采集 |
  | `reconnects` | 热更新与重连 |

- Prometheus 抓取地址是运维监听上的 `/metrics`，不属于本契约。

#### O-03 按包、按节点归因

`GET /api/v1/ops/attribution` · ops、admin · 旧：— · 审计：否 · 依据：4.10.2 · 页面：ops-obs

- 查询参数：`by`（`pack|node`）、`range?`、`labels?`。
- 响应：`{items: [{key, version_or_source, calls, fail_rate_pct, avg_duration_ms, guard_denies, reruns: int|null, first_accept_pct: number|null}]}`。
  - 按节点时 `version_or_source` 是「节点来自哪个包、哪个版本」。

#### O-04 链路详情

`GET /api/v1/ops/traces/{trace_id}` · ops、admin · 旧：— · 审计：否 · 依据：4.10.2「一条完整链路」 · 页面：ops-obs（链路瀑布）、ops-users（原始错误「在可观测中查看链路」）

- 响应：`{trace_id, total_ms, labels: {project, session, exec, changeset, pack, node}, spans: [{id, parent_id, name, kind: "message"|"turn"|"model"|"subagent"|"tool"|"script"|"guard"|"commit"|"check"|"event", start_offset_ms, duration_ms, status: "ok"|"denied"|"error", attrs: object}]}`。
- `attrs` 不含项目内容，只含路径、条目 key、计数。
- 错误：`trace_not_found`。

#### O-05 链路检索

`GET /api/v1/ops/traces` · ops、admin · 旧：— · 审计：否 · 依据：4.10.2 · 页面：ops-obs

- 查询参数：`labels?`、`range?`、`status?`；分页。
- 响应：`{items: [{trace_id, started_at, total_ms, root_name, labels, status}], next_cursor}`。

#### O-06 结构化日志

`GET /api/v1/ops/logs` · ops、admin · 旧：— · 审计：否 · 依据：4.10.2 · 页面：ops-obs

- 查询参数：`level?`（`INFO|WARN|ERROR`，多值）、`q?`（支持 `key=value OR key=value`）、`labels?`、`range?`；分页（默认 100）。
- 响应：`{items: [{ts, level, event, fields: {[key]: string}}], next_cursor}`。
- 日志里的 `token=` 等敏感字段已脱敏。

#### O-07 日志跟随

`GET /api/v1/ops/logs/stream` · ops、admin（可用 `?token=`） · 旧：— · 审计：否 · 依据：4.10.2 · 页面：ops-obs（「跟随」）

- SSE，查询参数同 `O-06` 的过滤条件。事件 `log`，见 4.5。

#### O-08 告警规则列表

`GET /api/v1/ops/alerts/rules` · ops、admin · 旧：— · 审计：否 · 依据：4.10.2（告警规则 + 处置手册，包可声明） · 页面：ops-obs、ops（「全部规则」）

- 响应：`{items: AlertRule[], firing: int}`。

#### O-09 新建告警规则

`POST /api/v1/ops/alerts/rules` · ops · 旧：— · 审计：是 · 依据：4.10.2 · 页面：ops-obs（「新建规则」）

- 请求：`{name, condition: string（表达式，如 node.fail_rate > 5%）, window: string, level: "critical"|"warning"|"info", runbook_md: string, auto_action?: "breaker"|null}`。
- 响应 `201`：`AlertRule`（`source="kernel"`）。

#### O-10 修改、删除告警规则

`PUT /api/v1/ops/alerts/rules/{rule_id}`、`DELETE /api/v1/ops/alerts/rules/{rule_id}` · ops · 旧：— · 审计：是 · 依据：4.10.2 · 页面：ops-obs

- 包声明的规则只能停用（`PUT {enabled: false}`），不能删，删除返回 `forbidden`。
- 响应：`AlertRule` 或 `204`。

#### O-11 导出器

`GET /api/v1/ops/exporters` · ops、admin · 旧：— · 审计：否 · 依据：5.7、㊱ · 页面：ops-obs（导出器卡、弹窗）

- 响应：`{items: [{id: "otel"|"prometheus"|"file"|"custom", label, implementation: string（如 exporter-otel@0.2.0 或 builtin）, target: string|null, signals: ("logs"|"metrics"|"traces")[], enabled: bool, configured: bool}]}`。

#### O-12 保存导出器设置

`PUT /api/v1/ops/exporters` · ops · 旧：— · 审计：是 · 依据：5.7（H2，滚动重启生效） · 页面：ops-obs

- 请求：`{items: [{id, enabled, target?, signals?, options?: object}]}`。
- 响应：`{items: [...], effective: "rolling_restart"}`。

#### O-13 健康详情

`GET /api/v1/ops/health` · ops、admin · 旧：`GET /api/admin/health` · 审计：否 · 依据：4.10.2、8.3 · 页面：ops-ctl（健康、版本与资源）

- 响应：

  | 字段 | 类型 | 说明 |
  |---|---|---|
  | `live` | object | `{status, path: "/api/health/live", latency_ms, uptime_ms, started_at}` |
  | `ready` | object | `{status: "ready"\|"degraded"\|"not_ready", path: "/api/health/ready", passed, total, summary}` |
  | `dependencies` | `[{name, probe, latency_ms, status: "ready"\|"slow"\|"warn"\|"fail", last_issue: string\|null}]` | 每 30 秒探测一次 |
  | `runtime` | object | `{engines: [{id, label, runtime_version, cli_version, available, note}], disk: {total, used_pct, warn}, load: [1m, 5m, 15m], cpu_cores, memory: {used, total}, process: {cmd, pid, init_managed: bool}, last_exit: {at, reason, code}\|null}` |

#### O-14 立即探测

`POST /api/v1/ops/health/probe` · ops · 旧：— · 审计：否 · 依据：4.10.2 · 页面：ops-ctl（「立即探测」）

- 响应：同 `O-13` 的 `dependencies`。

#### O-15 会话池

`GET /api/v1/ops/sessions` · ops、admin · 旧：`GET /api/admin/overview` 的 `sessions`、`session_pool` · 审计：否 · 依据：4.10.3 · 页面：ops-ctl（会话池）

- 响应：`{config: {max_sessions, idle_ttl_s, overflow_policy: "evict_idle_then_queue"}, active, queued, items: [{project: ProjectMetaView, engine_label, state: "running"|"idle"|"queued", events: int, idle_ms: int|null, queue_position: int|null, autobuild: bool}]}`。

#### O-16 会话池配置

`PUT /api/v1/ops/sessions/config` · ops · 旧：— · 审计：是 · 依据：4.10.3（调整并发上限） · 页面：ops-ctl（会话池「保存」）

- 请求：`{max_sessions: int 1–64, idle_ttl_s: int 60–86400, overflow_policy: "evict_idle_then_queue"|"queue_only"}`。
- 下一个会话起生效，覆盖环境变量默认值，记入配置注册表，来源 `data_dir`。
- 响应：`{config}`。

#### O-17 回收会话

`POST /api/v1/ops/sessions/{project_id}/recycle` · ops · 旧：`POST /api/admin/sessions/{project_id}/recycle` · 审计：是 · 依据：4.10.3 · 页面：ops-ctl（回收弹窗）

- 模型上下文经 resume 保留，用户下次发消息自动恢复。
- 响应：`{recycled: bool, pool: {...}}`。
- 错误：`session_not_found`、`session_running`。

#### O-18 强制断开

`POST /api/v1/ops/sessions/{project_id}/disconnect` · ops · 旧：— · 审计：是（记原因） · 依据：4.10.3、8.4 · 页面：ops-ctl（强制断开弹窗）

- 请求：`{reason: string}`。
- 正在进行的一轮中断：历史标「上一轮未正常结束」，执行记录标「在某阶段中断」，用户回来时可以续跑。
- 响应：`{disconnected: true}`。

#### O-19 排空会话

`POST /api/v1/ops/sessions/drain` · ops · 旧：— · 审计：是 · 依据：4.10.3 · 页面：ops-ctl（排空弹窗）

- 请求：`{max_wait_s: int, 默认 600}`。
- 排空期间拒绝新会话；在线会话跑完当前这一轮后回收；超时的强制断开并标中断；运行中的 L4 在安全点暂停。
- 响应 `202`：`{online: int, running: int, autobuild_running: int, eta_ms: int}`。

#### O-20 维护状态

`GET /api/v1/ops/maintenance` · ops、admin · 旧：— · 审计：否 · 依据：4.10.3（维护模式） · 页面：ops-ctl（维护窗口）

- 响应：`MaintenanceState`，或 `{active: false}`。

#### O-21 进入维护模式

`POST /api/v1/ops/maintenance` · ops · 旧：— · 审计：是 · 依据：4.10.3 · 页面：ops-ctl（「进入维护模式」弹窗）

- 请求：`{mode: "reject_new_sessions"|"read_only", message: string, autobuild: "pause_at_safe_point", target?: {kind: "core_upgrade", pack: "core", version: string}}`。
- 依次走四步：拒绝新会话（或只读）→ 排空 → 升级 → 恢复。
- 响应 `201`：`MaintenanceState`。
- 错误：`maintenance_active`。

#### O-22 维护：继续下一步

`POST /api/v1/ops/maintenance/advance` · ops · 旧：— · 审计：是 · 依据：4.10.3 · 页面：ops-ctl（「继续下一步」）

- 响应：`MaintenanceState`。
- 错误：`maintenance_step_blocked`（排空没完成）、`maintenance_not_active`。

#### O-23 维护：强制断开剩余会话

`POST /api/v1/ops/maintenance/force-disconnect` · ops · 旧：— · 审计：是 · 依据：4.10.3 · 页面：ops-ctl（「跳过等待，强制断开剩余会话」）

- 请求：`{reason: string}`。
- 响应：`MaintenanceState`。

#### O-24 退出维护模式

`DELETE /api/v1/ops/maintenance` · ops · 旧：— · 审计：是 · 依据：4.10.3 · 页面：ops-ctl（「取消维护」，以及第 4 步「恢复」）

- 解除限制，L4 从检查点续跑。
- 响应：`{active: false}`。
- 错误：`maintenance_not_active`。

#### O-25 插件运维

`GET /api/v1/ops/packs` · ops、admin · 旧：— · 审计：否 · 依据：4.10.3、㉙ · 页面：ops-ctl（插件运维）

- 普通用户的私有包也列出来，能禁用（ADR-0079）。
- 响应：`{items: [{name, kind, version, channel: "stable"|"beta"|"retired", rollout_pct, rollout_by, error_rate_pct, breaker: "closed"|"half_open"|"open", projects_using: int}]}`。

#### O-26 调整灰度

`PUT /api/v1/ops/packs/{name}/{version}/rollout` · ops · 旧：— · 审计：是 · 依据：8.1#2 · 页面：ops-ctl（「灰度 ▾」）

- 请求：`{percent: 0–100, by: "project"|"tenant", targets?: string[]}`。
- 响应：`O-25` 的一项。

#### O-27 回滚通道版本

`POST /api/v1/ops/packs/{name}/rollback` · ops · 旧：— · 审计：是 · 依据：4.10.3、㉙ · 页面：ops-ctl（插件行「回滚」）

- 请求：`{channel: "stable"|"beta", to_version: string}`。
- 把通道指针改回旧版本。已锁定版本的项目不受影响；跟随通道的项目下一条消息生效。
- 响应：`O-25` 的一项。

#### O-28 禁用版本

`POST /api/v1/ops/packs/{name}/{version}/disable` · ops · 旧：— · 审计：是 · 依据：4.10.3 · 页面：ops-ctl（「禁用」）

- 请求：`{reason: string}`。
- 该版本不能再被新项目启用。已锁定它的项目进待处理，提示升级或回滚。
- 响应：`O-25` 的一项。

#### O-29 熔断与恢复

`POST /api/v1/ops/packs/{name}/{version}/breaker` · ops · 旧：— · 审计：是 · 依据：4.10.3（出错时熔断）、㊳ · 页面：ops-ctl（「熔断」「恢复」）

- 请求：`{action: "open"|"close", reason?: string}`。
  - 熔断后节点暂时跳过，发布关卡解除，错误留档注明；
  - `close` 先进入半开试探。
- 响应：`O-25` 的一项。
- 错误：`breaker_state_invalid`。

#### O-30 core 默认版本

`GET /api/v1/ops/core-default` · ops、admin · 旧：`GET /api/settings/assets`（全局资产版本） · 审计：否 · 依据：方案 2.3 第 4、5 步 · 页面：ops-ctl（core 默认版本）

- 响应：`{stable: string, beta: {version, rollout_pct}|null, versions: [{version, channel: "beta"|"stable"|"retired", released_at, is_default: bool}]}`。

#### O-31 推广到 stable

`POST /api/v1/ops/core-default/promote` · ops · 旧：— · 审计：是 · 依据：同上 · 页面：ops-ctl（「推广到 stable」）

- 请求：`{version}`。切换是原子的；各项目仍按自己锁定的版本运行，跟随默认的项目下一条消息生效。
- 响应：同 `O-30`。

#### O-32 回滚默认版本

`POST /api/v1/ops/core-default/rollback` · ops · 旧：`POST /api/settings/assets/rollback` · 审计：是 · 依据：同上 · 页面：ops-ctl（回滚确认弹窗）

- 请求：`{version}`。回滚前先自动备份包仓库。
- 响应：同 `O-30`。

#### O-33 包仓库备份列表

`GET /api/v1/ops/pack-repo/backups` · ops、admin · 旧：`GET /api/settings/assets/backups` · 审计：否 · 依据：4.10.3 · 页面：ops-ctl（包仓库备份）

- 响应：`{items: [{name, size, created_at}], keep: 3}`。

#### O-34 立即备份包仓库

`POST /api/v1/ops/pack-repo/backups` · ops · 旧：— · 审计：是 · 依据：4.10.3 · 页面：ops-ctl

- 响应 `202`：`{task_id}`。

#### O-35 启动自检报告

`GET /api/v1/ops/startup-report` · ops、admin · 旧：— · 审计：否 · 依据：8.3、方案 B8 · 页面：ops-ctl（启动自检报告）、ops（摘要）

- 响应：`{at, last_exit: {at, reason, code}|null, items: [{id: "index_lock"|"uncommitted"|"assembly"|"dependencies"|"interrupted_execs"|"history_repair"|"purge", title, detail, result: "cleared"|"committed"|"ok"|"reassembled"|"awaiting_user"|"failed", tone: "fix"|"ok"|"warn"}]}`。

#### O-36 备份列表

`GET /api/v1/ops/backups` · ops、admin · 旧：`GET /api/admin/overview` 的 `backups` · 审计：否 · 依据：8.3、方案 E2 · 页面：ops-ctl（备份与恢复）

- 响应：`{schedule: {daily_at: "02:00", keep: 3, dir_volume: string}, scope: string[], items: [{name, size, created_at, scope: "full"|"workspace_only", scope_items: string[]}], last_drill: Drill|null}`。
- `scope` 取值：`workspaces`、`session_history`、`session_index`、`config`、`mcp_tokens`、`jiuwen_archives`、`studio_drafts`、`audit`、`cli_transcripts`。主密钥和密文分开放。

#### O-37 立即备份

`POST /api/v1/ops/backups` · ops（admin 也可） · 旧：`POST /api/admin/backup` · 审计：是 · 依据：8.3 · 页面：ops-ctl（「立即备份」）

- 响应 `202`：`{task_id}`。
- 错误：`backup_in_progress`。

#### O-38 发起恢复演练

`POST /api/v1/ops/backups/drills` · ops · 旧：— · 审计：是 · 依据：4.9#5、8.3 · 页面：ops-ctl（「发起演练」）

- 请求：`{backup: string, sample_projects?: int, 默认 5}`。恢复到隔离环境，抽检项目能否打开、版本历史是否完整。
- 响应 `202`：`{task_id}`。

#### O-39 演练记录

`GET /api/v1/ops/backups/drills` · ops、admin · 旧：— · 审计：否 · 依据：8.3 · 页面：ops-ctl

- 分页。
- 响应：`{items: Drill[], next_cursor}`。

#### O-40 任务列表

`GET /api/v1/ops/tasks` · ops、admin · 旧：— · 审计：否 · 依据：4.10.3（证据采集、导出、备份、清扫） · 页面：ops-ctl（任务）

- 查询参数：`type?`（`collection|export|backup|sweep|pack_install|drill|pack_repo_backup`）、`state?`；分页。
- 响应：`{items: [{id, type, target: string, project: ProjectMetaView|null, initiator: string（用户名或 system）, state: "queued"|"running"|"succeeded"|"partial"|"failed"|"cancelled", progress_pct, started_at, finished_at, error: string|null}], next_cursor}`。

#### O-41 任务操作

`POST /api/v1/ops/tasks/{task_id}/cancel`、`.../retry`、`.../resume` · ops · 旧：— · 审计：是 · 依据：4.10.3 · 页面：ops-ctl

- 证据采集类任务转调 `EV-17`。生产环境的续采和重试仍要项目所有者确认，运维调用时返回 `428 confirmation_required`，并通知所有者。
- 响应：任务对象。
- 错误：`task_not_found`、`task_state_invalid`。

#### O-42 版本清单

`GET /api/v1/ops/versions` · ops、admin · 旧：— · 审计：否 · 依据：4.10.3 · 页面：ops-ctl（版本清单）

- 响应：`{items: [{category: "kernel"|"capability"|"industry"|"system"|"provider"|"exporter"|"engine"|"data_format"|"lockfile", component, version, note}]}`。
- 至少包括：onto-modeling、kernel_api、page_api、各包、引擎、ontology-package、operation-centric、evidence-snapshot、packs.lock 与 flow.lock 格式版本。

#### O-43 数据保留

`GET /api/v1/ops/retention` · ops、admin · 旧：— · 审计：否 · 依据：4.10.3 · 页面：ops-ctl（数据保留）

- 响应：`{items: [{id: "session_history"|"snapshots"|"logs"|"audit"|"backups"|"trash"|"diagnostics"|"notifications", label, retention: {days?: int, keep_last?: int}, rule, next_run_at, editable: bool}]}`。
- 审计日志 365 天，只追加，`editable=false`。

#### O-44 修改数据保留

`PUT /api/v1/ops/retention` · ops · 旧：— · 审计：是 · 依据：4.10.3 · 页面：ops-ctl（「编辑」）

- 请求：`{items: [{id, retention}]}`。被引用的快照无论保留期多短都不清理。
- 响应：同 `O-43`。

#### O-45 生成诊断包

`POST /api/v1/ops/diagnostics` · ops · 旧：— · 审计：是 · 依据：4.10.3 · 页面：ops-ctl（诊断包弹窗）

- 请求：`{include: ("logs_24h"|"config_refs"|"versions"|"recent_errors"|"startup_report"|"pool_alert_snapshot")[]}`。
- 内容全部脱敏，不含密钥和项目内容，保留 7 天。
- 响应 `201`：`{id, name, size, created_at, created_by}`。

#### O-46 诊断包列表

`GET /api/v1/ops/diagnostics` · ops、admin · 旧：— · 审计：否 · 依据：4.10.3 · 页面：ops-ctl

- 响应：`{items: [{id, name, size, created_at, created_by, expires_at}]}`。

#### O-47 下载诊断包

`GET /api/v1/ops/diagnostics/{id}/download` · ops、admin（可用 `?token=`） · 旧：— · 审计：是 · 依据：4.10.3 · 页面：ops-ctl（「下载」）

- 响应：`application/zip`。
- 错误：`diagnostics_not_found`。

#### O-48 审计日志

`GET /api/v1/ops/audit` · ops、admin · 旧：`GET /api/admin/audit`（旧接口固定 200 条） · 审计：否 · 依据：4.10.1、方案 E4 · 页面：ops-users（审计日志）

- 查询参数：`type?`（`write|mcp|commit|plugin|evidence|ops|auth|publish|changeset|autobuild|user`，多值）、`q?`（操作者、项目、内容）、`from?`、`to?`；分页（默认 100）。
- 响应：`{items: AuditEntry[], next_cursor}`。

#### O-49 导出审计

`GET /api/v1/ops/audit/export` · ops、admin（可用 `?token=`） · 旧：— · 审计：是 · 依据：4.10.1 · 页面：ops-users（「导出」）

- 查询参数：同 `O-48`，加 `format?`（`jsonl|csv`）。
- 响应：附件，上限 10 万条。

#### O-50 错误留档

`GET /api/v1/ops/errors` · ops、admin · 旧：`GET /api/admin/overview` 的 `errors` · 审计：否 · 依据：4.10.1（标出包与节点） · 页面：ops-users（错误留档）

- 分页（默认 20）。
- 响应：`{items: ErrorRecord[], next_cursor, total}`。

#### O-51 错误详情

`GET /api/v1/ops/errors/{error_id}` · ops、admin · 旧：— · 审计：否 · 依据：4.10.1 · 页面：ops-users（原始错误抽屉）、chat-states（第 15 项错误编号）

- 响应：`ErrorRecord`，加 `{stack: string（已脱敏）, context: {project_id, changeset_id, pack, node, exec_id, session_id}, auto_action: string|null}`。
- 错误：`error_record_not_found`。

#### O-52 回收站

`GET /api/v1/ops/trash` · ops、admin · 旧：`GET /api/admin/trash` · 审计：否 · 依据：4.9 · 页面：ops-users（回收站）

- 响应：`{retention_days: 30, items: [{entry, project: ProjectMetaView, deleted_at, size, purge_at}]}`。

#### O-53 恢复回收站项目

`POST /api/v1/ops/trash/{entry}/restore` · ops、admin · 旧：`POST /api/admin/trash/restore` · 审计：是 · 依据：4.9 · 页面：ops-users（「恢复」）

- 恢复到所有者的项目列表；重名时自动改名。
- 响应：`ProjectMetaView`。
- 错误：`trash_entry_not_found`。

#### O-54 永久删除回收站项目

`DELETE /api/v1/ops/trash/{entry}` · ops、admin · 旧：— · 审计：是 · 依据：4.9 · 页面：ops-users（「永久删除」）

- 删除分两段：先移走，再清除；进程中途重启会接着清。
- 响应 `202`。

---

## 4 SSE 事件目录

信封格式和回放规则见 1.11。

- 下表「持久」一列写「是」的事件带 `id:` 行，会写进对话历史或项目流缓冲，可以回放；写「否」的是瞬时事件。
- 载荷字段都在信封的 `data` 里。

### 4.1 四条流共有的事件

| 编号 | type | 持久 | 载荷 | 说明 |
|---|---|---|---|---|
| E-00 | `ping` | 否 | `{}` | 每 15 秒一次 |
| E-01 | `stream.reset` | 否 | `{reason: "epoch_changed"\|"gap"\|"overflow", resync: string[]}` | `resync` 列出要重拉的接口编号，如 `["FL-01", "IN-01", "CS-06"]` |
| E-02 | `auth.expired` | 否 | `{}` | 发完即关流 |

### 4.2 对话流

`S-13 GET /api/v1/projects/{id}/conversations/{cid}/events`

#### 会话状态

| 编号 | type | 持久 | 载荷 | 用在哪 |
|---|---|---|---|---|
| EC-01 | `session.state` | 是 | `{state: "idle"\|"connecting"\|"running"\|"waiting_user"\|"queued"\|"error"\|"closed", since: time, turn_started_at: time\|null, reason: string\|null}` | 顶栏状态 chip、运行计时（chat-states 第 3 项「2:14」）、运行中禁止切换会话（第 1 项） ；`running` 且 `reason: "waiting_materials"`：建模命令在等资料转成文本，还没交给助手（F8） |
| EC-02 | `session.queued` | 是 | `{position: int, ahead: int, pool: {active, max}, progress_pct: number}` | chat-states 第 9 项排队卡 |
| EC-03 | `session.started` | 是 | `{engine_label, resumed: bool, context_kept: bool}` | — |
| EC-04 | `session.error` | 是 | `{error_id: "err-7f3a", message: string（友好提示）, retries: int, retryable: bool, actions: ("retry"\|"redirect")[], reason: "stall_timeout"\|null（运行中长时间没有引擎事件、判定执行超时；ADR-0097）}`。这一轮还没收尾的子任务卡随出错收尾显示「已中断」，`reason` 为 `stall_timeout` 时显示「执行超时」 | chat-states 第 15 项出错卡 |
| EC-05 | `session.interrupted` | 是 | `{by: "user"\|"ops"\|"restart"\|"engine_exit"\|"shutdown"\|"autobuild", at, exec_id: string\|null, position_label: string（如「你在生成第 3 个场景时打断了」）, saved: {files: int, version_label}\|null}` | chat-states 第 4 项；`by` 为 `autobuild` 是开始自主构建时接管：`position_label`「开始了自主构建，「…」交给它接着做」，`saved` 为 null |
| EC-06 | `session.config_pending` | 是 | `{changes: [{kind: "assembly"\|"model"\|"flow"\|"mcp"\|"permission_mode"\|"engine", summary: string}], effective: "next_message"\|"next_safe_point", detail_link: {page: "plugin-detail", pack: string}\|null}` | chat-states 第 14 项「建模能力有更新」提示条 |
| EC-07 | `session.reconnected` | 是 | `{reason: "config_changed"\|"restart"\|"recycled", changes: string[], context_kept: bool, duration_ms: int}` | chat-states 第 14 项时间线「已切换到新版本并重新连接，上下文保留」 |

#### 消息

| 编号 | type | 持久 | 载荷 | 用在哪 |
|---|---|---|---|---|
| EC-10 | `message.user` | 是 | `{message_id, text, attachments: AttachmentRef[], references: (EntryRef\|ArtifactRef\|{kind: "anchor", anchor})[], origin: {kind: "page"\|"inbox"\|"studio"\|"report", page, label, task_id}\|null, delivery: "immediate"\|"steer"\|"deferred"\|"queued"}` | 用户气泡；带 `origin` 时显示「来自「场景模拟」页」（第 19 项）；`steer` 是运行中的插话，送达状态看 EC-15 |
| EC-11 | `message.delta` | 否 | `{message_id, kind: "text"\|"thinking", text}` | 流式输出 |
| EC-12 | `message.assistant` | 是 | `{message_id, text_md, anchors: [{n: int, anchor}], entry_links: EntryRef[], continued: bool}` | 助手正文。正文里 `[[名称]]` 渲染成去 Studio 的链接，出处角标按 `anchors` 编号悬停看原文。`continued=true` 表示与上一条连续，不重复署名 |
| EC-13 | `notice` | 是 | `{kind: "merged"\|"discarded"\|"decided"\|"info"\|"warning", text, version_label: string\|null, changeset_id: string\|null, align: "center"\|"left"}` | 系统提示行，如「分段「订单与配送时限」已确认生效 · 版本 v0.12」「步骤 2 选了 A：…」 |
| EC-14 | `day.marker` | 是 | `{label: string}` | 日期分隔（「今天」） |
| EC-15 | `message.delivery` | 是 | `{message_id, state: "pending"\|"seen"\|"new_turn"\|"returned", at, waiting_on: {kind: "subagent", title, since}\|null}` | 运行中插话（`delivery="steer"`）送到哪了（F7）：等助手看到 → 已看到 / 这一轮结束后作为下一轮开始 / 打断时撤回。主助手在等子任务时 `waiting_on` 写等哪个，子任务开始、结束时再发一次。历史里每条消息只留最后一个状态 |
| EC-16 | `next.updated` | 是 | `{next: NextCommand\|null}` | 平台代存的下一条命令（F7，见 S-05、S-07）：输入框上方的排队条 |

#### 进展与子代理

| 编号 | type | 持久 | 载荷 | 用在哪 |
|---|---|---|---|---|
| EC-20 | `progress` | 是 | `ProgressGroup`：`{group_id, title, desc, state: "running"\|"done"\|"failed"\|"waiting"\|"interrupted", started_at, duration_ms: int\|null, collapsed: bool, steps: [{id, text, meta: string（耗时或状态文字）, state: "done"\|"running"\|"waiting"\|"failed"\|"info", duration_ms}]}` | 「工作过程」折叠块和时间线（wb-l2「已完成 3 步」，wb-materials 读资料进度，chat-states 第 3、4、15、17、19 项） |
| EC-21 | `progress.updated` | 是 | 同 EC-20，同一 `group_id` 整体替换 | 运行中步骤逐条回填耗时 |
| EC-22 | `subagent.updated` | 是 | `{task_id, group_id: string\|null, title: string（业务名，如「语义检查」；依次取描述、任务说明第一行、子代理类型）, status: "started"\|"running"\|"done"\|"failed", progress_text: string（如「第 6 / 11 个操作」）, summary: string\|null（第一行是结论，其后每行「· 」开头一条明细，最多 5 条；子代理交回给主助手的 JSON 由平台换成说法，如规则检查的 `[]` 为「没有发现问题」，认不出的为 null）, step: {id, kind: "tool"\|"text", text: string\|null, state: "running"\|"done"\|"failed"}\|null, step_count: int, brief: string\|null（派给它的任务说明，≤2000 字，只在开始那条里有）, thinking: {since, elapsed_ms}\|null}`（`step` 是子代理自己的一步，同一 `id` 后到的只改状态，`text` 为 null 时沿用先到的，子代理交回的 JSON 原文不算一步；`thinking` 是一次模型调用超过 15 秒还没出结果时每 15 秒的心跳，瞬时，不进历史。步骤明细见 S-21） | 时间线上的子任务卡（沿用旧版对话页样式：第二行是 `progress_text`，做完是 `summary` 的第一行）；`group_id` 只记派出时在哪一段，前端不按它挂 |
| EC-23 | `tool.activity` | 是 | `{tool_use_id, visibility: "internal", tool, summary, state: "started"\|"done"\|"failed"}` | 默认不渲染；内部步骤只进执行日志（第 9 章建议 4），留给排障 |

内部步骤由执行记录的阶段（㊶）和子代理事件归并成业务话的 `progress`。工具调用原文不进用户可见的事件。
一轮里的工作过程按时间分段：助手每说一句话（`message.assistant`），之前做完的步骤收成一组（`state=done`、`collapsed=true`），之后的步骤另起一组（`group_id` 带段号），组和正文按发生的先后排列。助手的话一律发 `message.assistant`，不收进组里。派出子任务（Task）不在组里占一步：子任务的 `subagent.updated` 在时间线上单独成卡，排在派出它的那句话后面，派出之后的步骤另起一组。

#### 确认、决策、问题

| 编号 | type | 持久 | 载荷 | 用在哪 |
|---|---|---|---|---|
| EC-30 | `permission.requested` | 是 | `{request_id, title: string（如「助手请求执行一个操作」）, description: string（业务话）, details_text: string（「查看技术细节」里的内容）, timeout_s: 600, expires_at}` | chat-states 第 6 项确认卡 |
| EC-31 | `permission.resolved` | 是 | `{request_id, behavior: "allow"\|"deny", by: "user"\|"timeout"\|"autobuild_policy", message}` | 确认卡变为已处理 |
| EC-32 | `question.asked` | 是 | `Question`（kind 见 4.6） | 决策卡、模拟分支、请专家确认、交互模拟、采集确认、资料用途确认 |
| EC-33 | `question.answered` | 是 | `{question_id, answer: {option_id?, option_ids?, custom_text?}, by: "user"\|"autobuild_policy", summary: string（如「步骤 2 选了 A：…」）, answered_at}` | 决策卡收起为一行 |
| EC-34 | `question.withdrawn` | 是 | `{question_id, reason: string}` | 问题失效（撤回这一轮、重启等） |

#### 卡片

| 编号 | type | 持久 | 载荷 | 用在哪 |
|---|---|---|---|---|
| EC-40 | `card` | 是 | `Card`：`{card_id, card_type, ...}`，按 `card_type` 取 4.7 的结构 | 产物卡、本步产出卡、这一轮改了什么、续跑卡等 |
| EC-41 | `card.updated` | 是 | 同 EC-40，同一 `card_id` 整体替换 | 卡片状态变化（待确认 → 已确认、清单已保存、执行后结果） |

#### 轮次、上下文、下一步

| 编号 | type | 持久 | 载荷 | 用在哪 |
|---|---|---|---|---|
| EC-50 | `turn.completed` | 是 | `{turn_id, duration_ms, is_error: bool, usage: {input_tokens, output_tokens, cache_read_tokens}, cost: Money}` | 用量统计 |
| EC-51 | `context.usage` | 否 | `ContextUsage`：`{used, window, pct, compact_at_pct, breakdown: {rules, materials, conversation, tool_results}\|null, last_call: {input, output, estimated: bool}\|null}` | 顶栏上下文占用环；chat-states 第 10 项 |
| EC-52 | `context.compacted` | 是 | `{trigger: "auto"\|"manual", pre_tokens, post_tokens, freed_tokens, kept: {decisions: int, artifacts: bool, todos: bool}, at}` | chat-states 第 10 项「已自动压缩上下文」 |
| EC-53 | `suggestions.updated` | 是 | `Suggestions` | 输入框上方建议条（6.10；chat-states 第 18 项；wb-l2 三种状态） |

### 4.3 项目流

`P-15 GET /api/v1/projects/{id}/events`

| 编号 | type | 载荷 | 用在哪 | 依据 |
|---|---|---|---|---|
| EP-01 | `flow.updated` | `{flow_hash, nodes: [{id, state, flags, progress}], slices: [{id, stages, note}], suggestions_changed: bool}`，只含变化的节点和分段 | 主线进度条、建模步骤抽屉、projects 卡片 | ㊴ |
| EP-02 | `inbox.updated` | `{counts: {total, by_filter, by_pack}, added: string[], removed: string[], changed: string[]}` | 左栏工作台角标、「待处理 N」、inbox 列表、插件页角标 | ㊴ |
| EP-03 | `scope.updated` | `ScopeLock` | Studio、流程图弹窗、场景模拟、代码生成、知识沉淀页的锁定条和只读；两态「助手运行中 / 修改待确认」，自主构建下是「这一段自动合入为止」 | 4.3、方案 C10 |
| EP-04 | `changeset.updated` | `{changeset_id, state: "open"\|"pending_confirm"\|"merged"\|"discarded"\|"conflict", source, conversation_id, version_label: string\|null}` | wb-l2「这一轮」卡、inbox | 8.2 |
| EP-05 | `workspace.changed` | `{commit, version_label, source: "assistant"\|"web"\|"studio"\|"mcp"\|"autobuild"\|"import"\|"plugin"\|"revert", changeset_id, by, entries: EntryRef[], artifacts: ArtifactRef[], paths: string[≤200]}` | 外壳知识包版本、Studio 表格与图谱刷新、编辑器「文件已被助手修改」提示 | 方案 C2 |
| EP-06 | `artifacts.updated` | `{type, change: "created"\|"updated"\|"deleted"\|"stale"\|"fresh", keys: string[]}` | 场景模拟、代码生成、知识沉淀页列表；Studio 流程图行 | 4.1、5.9.4 |
| EP-07 | `extensions.updated` | `{active_hash, pending_hash, changed: ("nav"\|"pages"\|"subpages"\|"entries"\|"artifact_types"\|"views"\|"gates")[]}` | 左栏插件页面出现或消失、资料子页、Studio 条目类型 | 5.9.6 |
| EP-08 | `assembly.updated` | `{state: "pending"\|"applied"\|"failed", pending_changes: [{pack, from, to, kind}], effective, error_id: string\|null}` | proj-settings 待生效 chip；与对话流 EC-06 配合 | 8.1 |
| EP-09 | `exec.updated` | `ExecRecord` | 续跑卡、inbox 续跑、插件页进度 | 8.4、㊶ |
| EP-10 | `command.progress` | `CommandTask`（中间百分比瞬时，状态变化持久） | 插件页、报告页、资料页的进度条和「去对话里看」；完成后列表刷新 | 5.9.3 |
| EP-11 | `materials.updated` | `{items: MaterialMapItem[]（只含变化的）, read_pct, problems_count}` | 资料地图卡与弹窗 | 第 9 章建议 5 |
| EP-12 | `materials.candidate` | `{candidate: {id, kind, name, anchor, anchor_label}, total}` | 资料地图弹窗「候选实时流」 | 第 9 章 |
| EP-13 | `evidence.job.updated` | `CollectionJob` | snapshots 采集任务表 | 5.8.7「通过 SSE 推送进度」 |
| EP-14 | `autobuild.run.updated` | `AutobuildRunSummary` | l4-run 运行条（进度、用时、token、费用、自动决定数、挂起数） | 8.5 |
| EP-15 | `autobuild.safe_point` | `{run_id, slice_id, slice_name, version_label, next: "continue"\|"paused"\|"stopped"\|"converted"}` | l4-run | 8.5 |
| EP-16 | `autobuild.budget_warning` | `{run_id, dimension: "duration"\|"tokens"\|"cost", used_pct: number}` | l4-run，到 80% 提醒 | 8.5 |
| EP-17 | `autobuild.finished` | `{run_id, run_no, state: "completed"\|"stopped"\|"budget_exhausted"\|"failed"\|"converted", report_available: bool}` | l4-run、inbox 汇总条 | 8.5 |
| EP-18 | `change_list.updated` | `{change_list_id, revision, impact_ms}` | incremental 清单卡、影响面卡 | 第 9 章建议 6 |
| EP-19 | `release.updated` | `{release: ReleaseRecord}` | publish 发布记录（含评测服务「已拉取」） | ㉓ |
| EP-20 | `project.updated` | `{fields: string[], meta: ProjectMeta}` | 外壳面包屑、构建方式胶囊、引擎、归档状态 | — |
| EP-21 | `conversation.updated` | `{conversation: Conversation, deleted: bool}` | 会话切换菜单 | ADR 0013 |
| EP-22 | `report.updated` | `{report_id, type, items_changed: bool}` | reports 条目状态（修复中 → 已修复） | 5.9.4 |
| EP-23 | `knowledge.updated` | `{ids: string[], change: "created"\|"updated"\|"deleted"}` | 知识沉淀页 | 5.5 |
| EP-24 | `maintenance.updated` | `MaintenanceState` | 全局横幅 | 4.10.3 |
| EP-25 | `materials.text.updated` | `{path, text: MaterialText}` | resources（列表行状态、预览） | ADR-0073 |

### 4.4 用户流

`A-12 GET /api/v1/me/events`

| 编号 | type | 载荷 | 用在哪 |
|---|---|---|---|
| EU-01 | `notification` | `Notification` | 顶栏铃铛未读数、通知抽屉、浏览器系统通知（按 `A-08` 偏好） |
| EU-02 | `project.card.updated` | `{project_id, card: ProjectCard}` | projects 卡片（L4 进度、待处理数、最近活动） |
| EU-03 | `project.removed` | `{project_id, reason: "deleted"\|"transferred"\|"trashed"}` | projects 列表 |
| EU-04 | `maintenance.updated` | `MaintenanceState` | 全局横幅 |

`Notification.kind` 取值：

| kind | 界面示例 |
|---|---|
| `autobuild_finished` | 自主构建完成，需复核 4 项 |
| `autobuild_progress` | 自主构建中 |
| `autobuild_suspension` | 新增挂起问题 |
| `autobuild_stopped` | 运行出错或在安全点停止 |
| `needs_input` | 需要你回答问题或确认操作 |
| `step_done` | 一步跑完 |
| `session_error` | 构建出错 |
| `evidence_conflict` | 需要你处理：证据冲突 |
| `publish_done` | 发布完成 |
| `nomination_reviewed` | 提名审核结果 |
| `request_reviewed` | 数据源登记申请通过或退回 |
| `request_pending` | 给管理员：待处理的申请 |
| `upload_rejected` | 插件包被退回 |

### 4.5 日志跟随流

`O-07 GET /api/v1/ops/logs/stream`

| 编号 | type | 载荷 |
|---|---|---|
| EO-01 | `log` | `{ts, level, event, fields}`（同 `O-06` 的条目） |

### 4.6 `Question` 的 kind

`Question` 的结构见第 7 章。下表列出各 kind 怎么用。

| kind | 界面 | 特有字段 | 回答 |
|---|---|---|---|
| `decision` | 结构化决策卡（wb-l2、wb-materials 资料用途确认、inbox 决策） | `options[].consequence`；`options[].preview: DiffLine[]`（选中后卡内显示「选 X 会这样改」）；最后一项「其他，我来说」`allow_custom_on` | `S-11`；没选不能确认；键盘 1–4、↵ |
| `branch` | 模拟分支选择卡（chat-states 第 7 项） | `scenario: ArtifactRef`、`context_text` | `S-11`，可带 `custom_text` |
| `expert` | 请专家确认卡（chat-states 第 7 项、reports 专家裁决） | `options` 渲染为按钮，含 `dismiss` 型选项「不需要问这个」 | `S-11` |
| `walkthrough` | 交互模拟卡（chat-states 第 20 项） | `scenario: ArtifactRef`、`inputs: [{name, value, type, source: "input"\|"example"}]`、`steps: [{n, label, text, ref: EntryRef\|null, source}]`、判断点 `prompt` | `S-11`；选择记进场景，下次批量验证按这条分支推演 |
| `collection` | 采集确认（陪伴建模下要采生产库时） | `collection: {source: EvidenceSourceRef, summary}`（同 `EV-13` 的 `summary`） | `S-11` 选「同意」后内核发起采集，等价于带确认的 `EV-14` |
| `confirm` | 一般的是 / 否确认 | 两个选项 | `S-11` |

`Question` 的共有字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `question_id` | string | — |
| `kind` | string | 上表的取值 |
| `conversation_id` | string | — |
| `exec_id` | string \| null | — |
| `step_label` | string | 如「步骤 2」 |
| `eyebrow` | string | 如「需要你决定 · 步骤 2」 |
| `title` | string | — |
| `background_md` | string | 带出处角标 |
| `anchors` | `[{n, anchor}]` | — |
| `options` | `Option[]` | `{id, key: "A", label, consequence, recommended: bool, preview: DiffLine[]\|null, custom: bool}` |
| `multi` | bool | — |
| `allow_custom` | bool | — |
| `custom_placeholder` | string | — |
| `state` | string | `open\|answered\|withdrawn` |
| `answer` | object \| null | — |
| `inbox_item_id` | string | — |
| `affected` | `(EntryRef\|ArtifactRef)[]` | 决策卡改动预览里列出的条目，计入写入范围（4.3） |

### 4.7 `Card` 的 card_type

所有卡片的共有字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `card_id` | string | — |
| `card_type` | string | 下表的取值 |
| `eyebrow` | string | 眉题 |
| `title` | string | — |
| `subtitle` | string | — |
| `status` | object \| null | `{key, label}`，key 取第 7 章 `DisplayState` |
| `plugin` | object \| null | `{name, label}`，插件带来的卡片显示插件标记 |
| `open` | object \| null | 双击或按钮打开什么：`{kind: "dialog"\|"page", target: string, ref?}` |

| card_type | 设计稿名称 | 特有字段 | 出现在 |
|---|---|---|---|
| `upload_prompt` | 欢迎与上传卡 | `{accept: string[], max_bytes, sources_available: int, actions: ("upload"\|"connect_system"\|"import_package")[]}` | wb-materials（还没传资料） |
| `material_map` | 资料地图卡 | `{total, read_pct, items: MaterialMapItem[≤8], unverifiable: int, problems: {contradiction, missing}}`；`open={dialog, "material_map"}` | wb-materials、chat-states 产物卡 |
| `research_summary` | 资料分析小结卡 | `{files, coverage_pct, verified_anchors: int, problems_carried: int, verified: bool, report_id}` | wb-blueprint |
| `blueprint` | 建模蓝图卡 | `{version_label: "草案 v2", state: "draft"\|"confirmed", goal_md（项目的建模目标）, goal_anchor（恒为 null）, goal_question: {id, title, answered} \| null（F4b：没答时确认按钮置灰）, slices: BlueprintSlice[], terms: {aligned: int, ambiguous: int}, questions_pending: int, research_verified: bool}`；`open={dialog, "blueprint"}` | wb-blueprint、chat-states |
| `build_choice` | 确认后怎么建 | `{options: [{mode: "L2", label}, {mode: "L4", label}], policy_link: true}` | wb-blueprint |
| `step_output` | 本步产出卡 | `{entry: EntryRef, eyebrow: "操作 · 本轮新抽出", state: "pending"\|"confirmed", inputs: (EntryRef\|{text})[], steps: [{n, text_md, anchors, awaiting_question_id: string\|null}], outputs: (EntryRef\|{text})[], rules: [{ref: EntryRef, state}], sources: [{n, anchor, label, scheme}]}` | wb-l2、chat-states；点条目名去 Studio |
| `flowchart` | 流程图卡 | `{artifact: ArtifactRef, state: "draft"\|"confirmed", summary: "8 个节点、1 个判断", thumbnail: {nodes: [{id, type, label, pending: bool}], edges: [{from, to, label}]}, locked: bool}`；`open={dialog, "flowchart"}` | wb-l2、flowchart、chat-states；只在图示包启用时出现 |
| `round_changes` | 这一轮改了什么 | `{changeset_id, title: "改了 3 处", checks: {errors, warnings, pending}, hints: [{tone: "info"\|"warn", text}]（如「3 处指向第 2 段待建的内容」「1 处链接找不到对象」「2 处结构缺失」；包声明的提交门禁没过时按门禁名说「1 处<门禁名>」，F3b）, groups: [{kind, label, items: [{change: "mod"\|"add"\|"del"\|"test"\|null, target: EntryRef\|ArtifactRef\|null, text, comparable: bool, count?}]}]（`text` 只写业务名：条目「对象 [[门店]]」，其余用 `target.label`；认不出的文件合成 `other` 组最后一行 `{change: null, target: null, text: "另有 N 处改动", count: N}`，F4b-paths）, state: "merged"\|"reverted"\|"conflict"\|"pending", merged_version_label: string\|null, reverted_version_label: string\|null, inbox_item_id: string\|null}`；一个变更集（一轮）一张卡，之后原地更新（F3） | wb-l2、chat-states 第 8 项、incremental；按钮：查看改动（`CS-03`）、撤回这一轮（`CS-05`）；冲突时「去处理」到待处理 |
| `change_list` | 变更点清单卡 | `{change_list_id, source: {path, label}, count, low_confidence: int, rows: ChangePoint[≤10], state: "editing"\|"confirmed"\|"executed"}`；`open={dialog, "change_list"}` | incremental、chat-states |
| `impact` | 影响面卡 | `Impact` 加 `{change_list_id, executed: bool}` | incremental |
| `regression` | 回归结果卡 | `{passed, total, rows: [{scenario: ArtifactRef, type: "affected"\|"guard", result: "pass"\|"fail", reason: string\|null}]}` | incremental |
| `external_feedback` | 外部反馈卡 | `{feedback_id, source: {service, run}, items: [{question, loss_reason, artifacts: (EntryRef\|ArtifactRef)[], related: string\|null}]}`；`plugin={evolution}` | incremental；只在反馈演进包启用时出现 |
| `pack_recommendation` | 助手推荐插件 | `{pack: PackRef, category_label, description, reason}` | chat-states 第 16 项 |
| `resume` | 中断续跑卡 | `{exec_id, title: "上次没做完：操作提取在步骤 3 / 4 中断", reason, position: {step, total, label}, saved: {files, version_label}, resumable: bool, not_resumable_reason: string\|null}` | chat-states 第 5 项；按钮：`S-16`、`S-17` |
| `autobuild_start` | 自主构建开始时的策略说明 | `{run_id, run_no, chips: [{label, value}]}`（范围、预算、生产库、拿不准时） | l4-run |
| `slice_progress` | 按段的进展块 | `{run_id, slice: {id, name}, state: "done"\|"running"\|"queued"\|"partial", summary, version_label: string\|null, timeline: ProgressStep[], decision_ids: string[], suspension_ids: string[]}` | l4-run |
| `auto_decision` | 自动决定卡 | `{run_id, ledger_id, at, question, chosen, anchors, confidence: number, threshold: number, assumption: bool, review_state: "pending"\|"accepted"\|"rejudged", recorded: true}` | l4-run；「改判」去 inbox |
| `suspension` | 挂起卡 | `{run_id, suspension_id, title, reason, inbox_item_id}` | l4-run；「现在回答」去 inbox |
| `run_report` | 运行报告卡 | `AutobuildReport` 摘要：`{run_id, run_no, state, started_at, ended_at, recovered_restarts: int, metrics: [...], slices: [...]}` | l4-run、chat-states 产物卡；按钮「去复核」「转为陪伴继续」 |
| `knowledge_saved` | 沉淀入库结果 | `{knowledge_id, title, conflicts: int}` | chat-states 第 12 项「已入库，后续建模会自动应用」 |

### 4.8 `chat-states` 页全部条目 → 事件与接口

| 设计稿条目 | 事件 | 接口 |
|---|---|---|
| 0 产物卡一览：资料地图、建模蓝图、本步产出、流程图、变更点清单、运行报告 | EC-40/41：`material_map`、`blueprint`、`step_output`、`flowchart`、`change_list`、`run_report` | `R-07`、`BP-01`、`ST-04`、`AR-03`、`INC-01`、`AB-16` |
| 1 会话切换菜单（新建、续聊、重命名、删除；运行中禁止） | EC-01、EP-21 | `S-01`～`S-04` |
| 2 全部命令（按业务名分组） | — | `S-18`；选中后 `S-07` 或 `S-20` |
| 3 运行中：子代理进度与打断 | EC-01、EC-20/21、EC-22 | `S-08` |
| 4 打断之后（继续 / 从打断处继续 / 换个方向） | EC-05、EC-53（`mode="interrupted"`） | 继续：`S-07`；从打断处继续：`S-16`；换个方向：前端清空输入框 |
| 5 中断续跑卡 | EC-40 `resume`、EP-09 | `S-16`、`S-17` |
| 6 权限确认卡（允许 / 拒绝，超时按拒绝） | EC-30、EC-31 | `S-10` |
| 7 模拟分支选择、请专家确认 | EC-32 `branch`、`expert`；EC-33 | `S-11` |
| 8 本轮改了什么（展开明细、对比） | EC-40/41 `round_changes`、EP-03、EP-04 | `CS-02`、`CS-03`、`CS-05` |
| 9 排队中 | EC-01（`queued`）、EC-02 | `S-12` |
| 10 上下文占用与自动压缩、项目累计用量 | EC-51、EC-52 | `S-05`、`P-16` |
| 11 重启会话 | EC-01（`closed`） | `S-09` |
| 12 沉淀为业务知识 | EC-40 `knowledge_saved` | `K-06`（查重提示）、`K-03`（带 `source`） |
| 13 复制 | — | 前端本地完成 |
| 14 能力包升级提示（下一条消息生效） | EC-06、EC-07、EP-08 | `PL-11`；「查看更新内容」去 `PL-02` |
| 15 出错时（友好提示 + 错误编号） | EC-04 | `S-14`（重试）；管理员看 `O-51` |
| 16 助手推荐插件 | EC-40 `pack_recommendation` | `PL-01`、`PL-03`、`PL-06` |
| 17 拖入附件、粘贴截图 | EC-10（`attachments`） | `U-01`、`U-03`、`S-07` |
| 18 下一步建议条三种情况 | EC-53 | `FL-02`；建议项的 `action` 见 `Suggestion` |
| 19 插件页发来的任务 | EC-10（`origin`）、EC-20、EP-10 | `S-20`、`S-19` |
| 20 交互模拟卡 | EC-32 `walkthrough`、EC-33 | `S-11` |

### 4.9 `wb-l2` 页 → 事件与接口

| 界面元素 | 事件 | 接口 |
|---|---|---|
| 顶栏会话名、主线进度条、「1 份新资料」、「待处理 18」、上下文环 | EP-01、EP-02、EC-51 | `S-01`、`FL-01`、`IN-01`、`S-05` |
| 系统提示行「分段已确认生效 · 版本 v0.12」 | EC-13 | — |
| 用户消息、助手消息、出处角标 | EC-10、EC-12 | `EV-29`（悬停看原文） |
| 工作过程（已完成 3 步） | EC-20/21 | — |
| 本步产出卡（条目名去 Studio） | EC-40 `step_output` | `ST-04` |
| 流程图卡（双击打开编辑） | EC-40 `flowchart` | `AR-03`、`AR-05` |
| 决策卡（单选 + 后果 + 改动预览，稍后决定，确认这个方向） | EC-32、EC-33、EC-13 | `S-11`；「稍后决定」去 inbox |
| 写入范围只读（助手运行中 / 有冲突） | EP-03 | `CS-06` |
| 这一轮改了什么卡（查看改动、撤回这一轮） | EC-40/41 `round_changes`、EP-04 | `CS-03`、`CS-05` |
| 建议条三态（先做上面的决定 / 确认生效 / 提取这一段的规则） | EC-53 | `FL-02` |
| 建模步骤抽屉（分段、必经、发布前检查、可选、有需要时；开始、跳过、恢复、调整分段） | EP-01 | `FL-01`、`FL-03`、`FL-04`、`FL-05` |
| 输入框（拖入文件、粘贴截图、@ 引用条目、命令、发送；模式胶囊） | EC-10 | `U-01`、`S-07`、`S-18`、`F-07`（@ 搜条目）、`P-13` |

---

## 5 页面 → 接口对照表

前端按这张表接线。设计稿共 36 页，去掉「设计说明」（cover）和「功能点清单」（features），剩 34 个界面页，下面逐页列出。

### 5.0 外壳（每页都会用到）

| 外壳 | 接口 | 事件 |
|---|---|---|
| 启动 | `C-01`、`A-07`、`A-04`（无感刷新） | — |
| 全局页外壳（顶栏：我的项目、插件中心、运维中心、铃铛、头像） | `A-13`、`A-14` | 用户流 EU-01～04 |
| 项目页外壳（面包屑、知识包版本、左栏内核 4 项 + 插件页面、工作台角标、子导航） | `P-04`、`PL-12`、`IN-01`（只取 `counts`）、`ST-01`（只取 `version_label`） | 项目流 EP-01、EP-02、EP-07、EP-20、EP-24 |
| ⌘K 命令面板 | `F-07`、`S-18`；「开始自主构建」走 `AB-03`；「上传资料」「连接业务系统」等跳页 | — |
| 证据锚点悬停看原文（所有页） | `EV-29` | — |
| 维护横幅 | `C-01` 的 `maintenance` | EP-24、EU-04 |
| 工作台公共件（顶栏、建模步骤抽屉、输入框、建议条） | 见 4.9 | — |

### 5.1 逐页对照

| 页面 id | 页面 | 接口 | 事件 |
|---|---|---|---|
| `login` | 登录与注册 | `C-01`、`A-01`、`A-02`、`A-03`、`A-04` | — |
| `projects` | 我的项目 | `P-01`、`P-03`（重命名弹窗实时检查）、`P-05`、`P-06`、`P-07`、`P-08`、`P-09`、`A-13`、`A-14`、`FL-01`（卡片分段进度已含在 `ProjectCard`，不单独调） | EU-01、EU-02、EU-03 |
| `import` | 导入项目 | `U-01`（`project_import`）、`U-03`、`P-10`、`P-03`、`P-11` | — |
| `create` | 新建项目 | `P-03`、`U-01`（`material`）、`U-03`、`EV-01`（已登记数据源数）、`PL-01`（`category=industry,system,capability`）、`C-06`、`A-09`、`P-02` | — |
| `wb-materials` | 建模初期 · 资料分析 | `S-01`、`S-05`、`S-06`、`S-07`、`S-08`、`S-11`、`U-01`、`R-02`、`R-03`、`R-06`、`R-07`、`R-08`、`R-09`、`R-10`、`P-05`（联网 / 仅本地）、`S-20`（追加分析、继续读取、让助手逐条修正）、`EV-01`、`FL-01`、`FL-02` | EC-01、EC-10、EC-12、EC-20/21、EC-32/33、EC-40/41（`upload_prompt`、`material_map`）、EC-51、EC-53；EP-01、EP-11、EP-12 |
| `wb-blueprint` | 建模初期 · 分段规划 | `BP-01`、`BP-02`、`BP-03`、`RP-02`（资料分析小结）、`AB-03`、`AB-04`、`EV-01`（数据源提示）、`S-07`、`S-20`（重新划分、按章节拆分）、`FL-01`、`FL-02` | EC-40/41（`research_summary`、`blueprint`、`build_choice`）、EC-20、EC-53；EP-01 |
| `wb-l2` | 建模中 · 陪伴（L2） | 见 4.9：`S-01`、`S-05`、`S-06`、`S-07`、`S-11`、`ST-04`、`AR-03`、`CS-02`～`CS-06`、`FL-01`～`FL-05`、`IN-01`、`EV-29`、`U-01`、`F-07`、`S-18`、`P-13` | 对话流全部；EP-01～EP-05 |
| `flowchart` | 流程图编辑弹窗 | `AR-03`、`AR-05`、`AR-08`、`AR-10`、`CS-06`、`PL-12`（图示插件标记）、`S-08`（锁定条「回到对话里打断它」） | EC-40 `flowchart`、EP-03、EP-05、EP-06 |
| `l4-run` | 建模中 · 自主构建（L4） | `AB-01`、`AB-03`、`AB-04`、`AB-05`～`AB-10`、`AB-14`、`AB-16`、`AB-17`、`V-02`（查看改动）、`S-05`、`S-06`、`S-07`（补充说明，`deferred`）、`CS-06`、`FL-01`、`FL-02` | EC-40/41（`autobuild_start`、`slice_progress`、`auto_decision`、`suspension`、`run_report`）、EC-53（`autobuild_controls`）；EP-03、EP-14～EP-17 |
| `incremental` | 增量更新 | `INC-01`～`INC-05`、`R-11`、`R-12`、`CS-02`～`CS-05`、`S-07`、`S-20`（让助手分析、交给助手修）、`FL-01`、`FL-02` | EC-40/41（`change_list`、`impact`、`regression`、`external_feedback`、`round_changes`）、EC-20、EC-53；EP-18、EP-04 |
| `inbox` | 待处理与复核 | `IN-01`～`IN-04`、`AB-10`～`AB-17`、`INC-01`～`INC-04`、`S-11`、`S-16`、`S-17`、`R-11`、`V-02`、`EV-29` | EP-02、EP-09、EP-17 |
| `chat-states` | 对话卡片与状态 | 见 4.8 | 见 4.8 |
| `studio` | 图谱 | `ST-01`、`ST-02`、`ST-04`、`ST-05`、`ST-06`、`CS-06`、`EV-26`（证据卡「被谁引用」）、`AR-09`（场景通过率，已含在 `ST-01`） | EP-03、EP-05 |
| `studio-table` | 表格与条目编辑 | `ST-03`、`ST-04`、`ST-07`、`ST-08`、`ST-09`、`ST-10`、`F-02`、`F-03`（编辑原文）、`F-04`（大文件下载）、`V-04`（刷新后对比）、`AR-02`、`AR-03`、`AR-07`、`AR-08`（流程图条目）、`R-01`、`R-15`（证据列选出处）、`EV-29`、`CS-06`、`C-05`、`PL-12` | EP-03、EP-05、EP-06 |
| `reports` | 报告评审 | `RP-01`～`RP-06`、`S-11`（专家裁决）、`R-09`（资料分析报告核验） | EP-22、EP-10 |
| `resources` | 资料区 | `R-01`～`R-06`、`R-11`、`R-12`、`F-04`、`F-05`、`F-03`（drawio 保存）、`F-02`（drawio、文本）、`PL-12`（子页） | EP-05、EP-07 |
| `sources` | 业务系统数据源 | `EV-01`～`EV-12`、`PL-01`（去插件中心安装提供方） | — |
| `snapshots` | 采集与快照 | `EV-12`、`EV-13`～`EV-22`、`EV-30`（联系管理员）、`S-20`（在对话里分析影响） | EP-13 |
| `schema` | 证据浏览器 | `EV-18`（快照下拉）、`EV-23`～`EV-28` | — |
| `publish` | 发布知识包 | `PB-01`～`PB-07`、`HB-05`～`HB-09`、`P-09`、`FL-05`（合规检查「去运行」）、`RP-06`（修完重新检查）、`V-01`（版本下拉） | EP-19、EP-01 |
| `versions` | 版本历史与对比 | `V-01`～`V-08`、`F-06`、`F-03` | EP-05 |
| `scenarios` | 场景模拟（插件） | `AR-01`、`AR-02`、`AR-03`、`AR-04`、`AR-05`、`AR-06`、`AR-07`、`AR-09`（类型 `trace.scenario`、`trace.mock`）、`S-20`（批量验证、交互模拟、补齐过期场景、按场景补生成、行内 ▶）、`S-19`、`CS-06`、`IN-01`（`pack=trace` 角标）、`PL-12` | EP-03、EP-06、EP-10 |
| `knowledge` | 知识沉淀（插件） | `K-01`～`K-08`、`S-20`（在对话里复核）、`CS-06` | EP-23、EP-03 |
| `scripts` | 代码生成与求解（插件） | `SC-01`、`SC-02`、`SC-03`、`SV-01`～`SV-03`、`ST-04`（脚本只读预览）、`S-20`（扫描、生成脚本、修复、重新生成、按模板重新生成）、`S-19`、`CS-06` | EP-03、EP-06、EP-10 |
| `proj-settings` | 项目设置 | `P-04`、`P-05`、`P-06`、`P-07`、`P-12`、`P-13`、`P-14`、`C-06`、`A-09`、`AB-01`、`AB-02`、`AB-03`、`AB-18`、`BP-01`（范围里的分段清单）、`EV-01`（生产库授权行）、`PL-05`～`PL-11`、`M-01`～`M-04` | EP-08、EP-20 |
| `plugins` | 插件中心 | `PL-01`、`PL-03`、`PL-04`、`PL-06`、`PL-07`、`PL-19`（管理员） | EU-01 |
| `plugin-detail` | 插件详情 | `PL-02`、`PL-03`、`PL-07`、`PL-09`、`PL-05` | — |
| `plugin-upload` | 上传与审核插件 | `PL-13`～`PL-18` | — |
| `me` | 个人设置 | `A-05`～`A-11` | — |
| `sys` | 系统设置 | `SY-01`～`SY-17`、`C-06` | — |
| `ops` | 运维总览 | `O-01`、`O-08`（全部规则）、`O-35`（完整报告） | — |
| `ops-obs` | 可观测 | `O-02`～`O-12` | EO-01 |
| `ops-ctl` | 运维管控与健康 | `O-13`～`O-47`、`C-03`、`C-04`（显示路径） | — |
| `ops-users` | 用户、审计与回收站 | `AD-01`～`AD-05`、`O-48`～`O-54`、`O-04`（在可观测中查看链路） | — |

### 5.2 空缺检查

上面 34 页都已对上接口。逐页核对的结论：

1. **只在前端完成、不需要接口的交互：** 复制消息、复制锚点、复制地址、复制 token、主题切换的即时预览、分段切换、展开收起、键盘快捷键本身（J/K、1–4、E、↵、⌘K）、图谱的缩放和全屏、流程图的撤销和重做、自动布局。保存时才调接口。
2. **设计稿里画了、本契约有意不提供接口的：**
   - 「查看原始最终答复」（features 标为移除）；
   - Studio 力模拟、折叠关系簇（移除）。
3. **本契约提供了接口、设计稿没有画入口的**（实现时由主控决定是否补页面，见 8.2）：
   - `X-06` 服务令牌管理；
   - `SY-18` 模型单价表；
   - `AD-06`～`AD-10`：无主项目、指定所有者、管理员申请审核；
   - `O-05` 链路检索。

---

## 6 旧接口对照

主线 2ad2ad7b 的全部路由，逐条给出 v1 去向。

- 「删除」的旧路径在兼容期返回 `410 endpoint_removed`。
- 「改写」的旧路径经兼容中间件转到 v1，带 Deprecation 头（1.2）。

### 6.1 `routers/projects.py`

| 旧接口 | v1 | 处理 |
|---|---|---|
| `GET /api/projects` | `P-01` | 改写；响应形状改为 `ProjectCard` |
| `POST /api/projects` | `P-02` | 改写；`template`、`background` 改为 `example`、`goal` |
| `POST /api/projects/import` | `P-10`（`dry_run=true`）、`P-11` | 改写；multipart 改为先 `U-01` 再引用 `upload_id` |
| `POST /api/projects/transfer-import` | `P-11` | 改写；格式自动识别 |
| `GET /api/projects/{id}` | `P-04` | 改写 |
| `PATCH /api/projects/{id}` | `P-05` | 改写 |
| `DELETE /api/projects/{id}` | `P-06` | 改写；新增必填 `confirm_name` |
| `POST /api/projects/{id}/archive` | `P-07` | 改写 |
| `POST /api/projects/{id}/clone` | `P-08` | 改写 |
| `GET /api/projects/{id}/export?scope=workspace` | `P-09` | 改写 |
| `GET /api/projects/{id}/export?scope=package` | `PB-03` + `PB-07` | 删除（GET 不再有发布副作用） |
| `GET /api/projects/{id}/export?scope=ontology` | — | 删除（features：导出知识包原始产物，移除） |
| `GET /api/projects/{id}/package/check` | `PB-01` | 改写 |
| `GET /api/projects/{id}/package/solver-contract` | `SV-01` | 改写 |
| `PUT /api/projects/{id}/package/solver-contract` | `SV-02` | 改写；`baseRevision` 改为 `base_revision` |
| `POST /api/projects/{id}/package/solver-contract/check` | `SV-03` | 改写 |
| `GET /api/projects/{id}/package/solver-validations/latest` | —（SV-04 退役，ADR-0098） | 删除 |
| `GET/PUT /api/projects/{id}/package/solver-validations/cases` | — | 删除（第 11 章、14#2） |
| `POST /api/projects/{id}/package/solver-validations` | —（求解验证在评测服务里做，ADR-0098） | 删除 |
| `POST /api/projects/{id}/objects/{key}/data-source` | `PB-02` | 改写；方法改 PUT |
| `GET /api/projects/{id}/files` | `F-01` | 改写；去掉写副作用 |
| `GET /api/projects/{id}/file` | `F-02` | 改写 |
| `PUT /api/projects/{id}/file` | `F-03` | 改写 |
| `GET /api/projects/{id}/excel` | `F-05` | 改写 |
| `GET /api/projects/{id}/file-at` | `F-06` | 改写；`rev` 改为 `commit` |
| `GET /api/projects/{id}/raw` | `F-04` | 改写 |
| `GET /api/projects/{id}/search` | `F-07` | 改写 |
| `GET /api/projects/{id}/metrics` | `ST-01` | 改写（字段不同） |
| `POST /api/projects/{id}/resources` | `R-02` | 改写 |
| `GET /api/projects/{id}/resource-meta` | `R-01` | 改写 |
| `PATCH /api/projects/{id}/resource-meta` | `R-03` | 改写 |
| `DELETE /api/projects/{id}/resources` | `R-04` | 改写 |
| `GET/PUT /api/projects/{id}/mcp` | `P-14` | 改写 |
| `GET/POST/PUT/DELETE /api/projects/{id}/eval/*`（18 个） | GET 的替代是 `ES-09`（旧版评测记录只读） | 删除（第 11 章、ADR-0092） |
| `GET/PUT /api/projects/{id}/engine` | `P-12` | 改写 |
| `GET /api/projects/{id}/studio/graph` | `ST-02` | 改写 |
| `GET /api/projects/{id}/studio/elements/{eid}/flow` | `ST-05` | 改写 |
| `GET /api/projects/{id}/studio/element?path=` | `ST-04` | 改写；按 `path` 定位改为按 `entry_id` 定位，兼容期旧接口按 path 换算 |
| `POST/DELETE /api/projects/{id}/studio/lease` | `CS-06` | 删除（租约取消，详设 4.3） |
| `GET/PUT/DELETE /api/projects/{id}/studio/draft` | `ST-07` | 改写 |
| `POST /api/projects/{id}/studio/submit` | `ST-08` | 改写 |
| `GET /api/projects/{id}/report-items?path=` | `RP-02`、`RP-03` | 改写 |
| `GET /api/projects/{id}/query` | `F-08` | 改写 |
| `GET/POST/DELETE /api/projects/{id}/knowledge` | `K-01`、`K-03`、`K-05` | 改写；按路径改为按 `knowledge_id` |
| `GET /api/projects/{id}/versions` | `V-01` | 改写 |
| `POST /api/projects/{id}/versions/revert` | `V-06` | 改写 |
| `POST /api/projects/{id}/versions/tag` | `V-07` | 改写 |
| `DELETE /api/projects/{id}/versions/tag/{tag}` | `V-08` | 改写 |
| `GET/PUT /api/projects/{id}/members`（54eb190f 已删） | — | 删除（单人项目） |

### 6.2 `routers/sessions.py`

| 旧接口 | v1 | 处理 |
|---|---|---|
| `GET /api/projects/{id}/session/usage` | `P-16` | 改写 |
| `GET /api/projects/{id}/session?conversation=` | `S-05` | 改写 |
| `GET /api/projects/{id}/session/conversations` | `S-01` | 改写 |
| `POST /api/projects/{id}/session/conversations` | `S-02` | 改写 |
| `PATCH /api/projects/{id}/session/conversations/{cid}` | `S-03` | 改写 |
| `DELETE /api/projects/{id}/session/conversations/{cid}` | `S-04` | 改写 |
| `POST /api/projects/{id}/session/messages` | `S-07` | 改写；`conversation_id` 移到路径 |
| `POST /api/projects/{id}/session/permissions/{rid}` | `S-10`；带 `answers` 的改走 `S-11` | 改写 |
| `POST /api/projects/{id}/session/close` | `S-09` | 改写 |
| `POST /api/projects/{id}/session/interrupt` | `S-08` | 改写 |
| `GET /api/projects/{id}/session/stream` | `S-13` | 改写；事件名和载荷按第 4 章重新定义，旧事件名不再发出 |

旧事件到 v1 事件：

| 旧事件 | v1 事件 |
|---|---|
| `user_message` | `message.user` |
| `stream_delta` | `message.delta` |
| `assistant_message` | `message.assistant`；其中工具调用块转为 `tool.activity` |
| `tool_results` | `tool.activity` |
| `result` | `turn.completed` |
| `subagent_started`、`subagent_finished` | `subagent.updated` |
| `permission_request`、`permission_resolved` | `permission.requested`、`permission.resolved` |
| `session_state`、`session_started`、`session_error`、`session_interrupted` | 同名的点号形式 |
| `queue_position` | `session.queued` |
| `context_usage`、`context_compacted` | `context.usage`、`context.compacted` |
| `workspace_changes` | `card` 的 `round_changes`，加上项目流 `workspace.changed` |
| `model_usage` | 并入 `turn.completed` |

### 6.3 `routers/auth_routes.py`

| 旧接口 | v1 | 处理 |
|---|---|---|
| `GET /api/auth/status` | `C-01` | 改写 |
| `POST /api/auth/login` | `A-01` | 改写 |
| `POST /api/auth/register` | `A-03` | 改写 |
| `POST /api/auth/refresh` | `A-04` | 改写 |
| `POST /api/auth/password` | `A-06` | 改写；不再接受请求体里的 `username` |

### 6.4 `routers/settings.py`

| 旧接口 | v1 | 处理 |
|---|---|---|
| `GET /api/settings/model` | `SY-01` | 改写 |
| `PUT /api/settings/model` | `SY-02`（`engine=claude`） | 改写 |
| `PUT /api/settings/model/jiuwen` | `SY-02`（`engine=jiuwen`） | 改写 |
| `PUT /api/settings/model/context` | `SY-02` 的 `context` | 改写 |
| `GET /api/settings/model/context-presets` | `SY-05` | 改写 |
| `PUT /api/settings/model/context-presets/custom` | `SY-06` | 改写 |
| `POST /api/settings/model/context-presets/custom/disable` | `SY-07` | 改写 |
| `GET /api/settings/model/context-presets/match` | `SY-08` | 改写 |
| `PUT /api/settings/model/default-engine` | `SY-03` | 改写 |
| `PUT /api/settings/model/personal-policy` | `SY-04` | 改写 |
| `GET /api/settings/model/me` | `A-09` | 改写 |
| `PUT /api/settings/model/me/{engine}` | `A-10` | 改写 |
| `DELETE /api/settings/model/me/{engine}` | `A-11` | 改写 |
| `GET/PUT /api/settings/runtime` | `SY-09` | 改写 |
| `GET /api/settings/sandbox` | `SY-10` | 改写 |
| `GET /api/settings/assets` | `O-30`、`PL-01` | 删除（全局单版本改为包仓库） |
| `POST /api/settings/assets/upload` | `PL-13` | 改写：上传的资产包当作 core 新版本，走静态检查，发布到 stable（方案 2.3 第 4 步） |
| `GET /api/settings/assets/backups` | `O-33` | 改写 |
| `POST /api/settings/assets/rollback` | `O-32` | 改写 |
| `GET/PUT /api/settings/mcp` | `SY-12` | 改写 |
| `POST /api/settings/mcp/test` | `SY-13` | 改写 |

### 6.5 `routers/admin.py`

| 旧接口 | v1 | 处理 |
|---|---|---|
| `GET /api/admin/overview` | `O-01`、`O-15`、`O-36`、`O-50` | 改写到 `O-01`（运维管理面） |
| `POST /api/admin/backup` | `O-37` | 改写 |
| `POST /api/admin/sessions/{pid}/recycle` | `O-17` | 改写 |
| `GET /api/admin/health` | `O-13` | 改写 |
| `GET /api/admin/trash` | `O-52` | 改写 |
| `POST /api/admin/trash/restore` | `O-53` | 改写；`name` 移到路径 |
| `GET /api/admin/audit` | `O-48` | 改写 |
| `PUT /api/admin/projects/{id}/owner` | `AD-07` | 改写 |
| `GET /api/admin/users` | `AD-01` | 改写 |
| `PUT /api/admin/users/{u}` | `AD-02`（role）、`AD-03`（password） | 改写 |
| `DELETE /api/admin/users/{u}` | `AD-04` | 改写；新增项目去向 |
| `GET /api/admin/modeling-cleanups` | — | 删除（第 11 章） |
| `POST /api/admin/modeling-cleanups/{id}/retry` | — | 删除 |

### 6.6 `routers/onto_mcp.py` 与 `main.py`

| 旧接口 | v1 | 处理 |
|---|---|---|
| `GET /api/projects/{id}/modeling-mcp` | `M-01` | 改写 |
| `GET /api/projects/{id}/modeling-mcp/tokens` | `M-02` | 改写 |
| `POST /api/projects/{id}/modeling-mcp/tokens` | `M-03` | 改写；`expires_in_seconds` 改为 `expires_in_s` |
| `DELETE /api/projects/{id}/modeling-mcp/tokens/{tid}` | `M-04` | 改写 |
| `POST /api/onto-mcp/{id}/{toolset}/mcp` | `M-05` | 永久保留，同时提供 v1 路径 |
| `GET /api/onto-mcp/{id}/{toolset}/sse` | `M-06` | 永久保留 |
| `POST /api/onto-mcp/{id}/{toolset}/sse/messages[/]` | `M-07` | 永久保留 |
| `GET /api/health` | `C-02` | 永久保留 |

---

## 7 数据模型附录

跨接口复用的对象。写法约定：

- 用类 TypeScript 的写法：`?` 表示可省略，`| null` 表示可为空。
- `time` 是 RFC 3339 字符串，`Money` 见 1.9。
- 和详设对得上的格式（节点状态、提交 trailer、包清单、产物登记、快照清单、锚点）以详设原文为准，这里只给 API 上的投影。

### 7.1 通用

```ts
type Money = { amount: number | null; currency: "CNY" | "USD" };

type EntryKind = "object" | "operation" | "rule" | "metric" | "script";
type EntryId = string;            // "<kind>:<key>"，如 "object:hardness_limit"
type EntryRef = { kind: EntryKind | "flowchart"; id: EntryId; key: string; name: string; label?: string };
type ArtifactRef = { type: string; key: string; title: string; label?: string };   // type 如 "trace.scenario"
// label（F4b-paths，ADR-0075）：后端按元模型、产物登记算好的业务名，界面直接显示，不再按路径猜：
// 对象「门店」、流程图「订单流程」、建模蓝图、资料分析报告、检查报告、资料「合同」。
// 变更里认不出的文件 type="file"、title 与 label 为空串，界面合成一行「另有 N 处改动」；路径只在 key 里。
type PackRef = { name: string; version: string; label: string };
type AttachmentRef = { upload_id: string; filename: string; size: number; kind: "doc" | "xls" | "pdf" | "image" | "file" };

type Finding = {
  code: string;                   // 见 2.9 门禁 finding 码，或检查器规则码，如 E-ROUTE-01
  level: "error" | "warning";
  message: string;
  target?: EntryRef | ArtifactRef;
  path?: string;
  field?: string;
  hint?: string;
};

type DiffLine = { kind: "header" | "del" | "add" | "ctx" | "note"; text: string };

// 决策卡、台账、挂起问题共用的选项；Question、Card 的结构见 4.6、4.7
type Option = { id: string; key: string; label: string; consequence: string | null;
                recommended: boolean; preview: DiffLine[] | null; custom: boolean;
                adopted?: boolean };   // adopted = 自主构建采用的选项

// 界面状态色（设计稿 core.js 的 ST），由 API 状态映射而来，前端统一渲染
type DisplayState = "not_ready" | "ready" | "running" | "pending" | "confirmed" | "stale" | "skipped"
  | "blocked" | "needs_review" | "disabled" | "conflict" | "draft" | "none" | "backfill" | "auto"
  | "fail" | "pass";

type AnchorResolution = {
  anchor: string;
  scheme: "file" | "db" | "code" | "bin" | "ui" | "api" | "cli" | "log";
  resolved: boolean;
  reason?: string;                // resolved=false 时：not_in_snapshot | file_missing | invalid
  source_label: string;           // 资料名（原文件名去扩展名，文本版映射回原资料）如 "02-配送规则说明"，或 "业务系统 订单库"
  location_label: string;         // 如 "第 3 页"、"工作表 客户"、"§9.2 超量处理"、"补货标准表 · 标准上限"；
                                  // 行号锚点 L12[-L15] 按前面最近的位置标记（文本版）或小节标题（md 资料）算，都没有时为空串，不显示行号
  material?: { name: string; type: string; path: string } | null;   // file 锚点：原资料（F4b-paths）；type 如 "PDF"、"Word"
  text: string;                   // 原文或记录摘要
  highlights: { start: number; end: number }[];
  confidence: "declared" | "observed" | "inferred" | null;   // 界面叫法：系统元数据 / 数据观察 / 推断
  snapshot_id: string | null;
  page: "resources" | "schema" | null;   // 「查看原文」去哪
};
```

### 7.2 用户与通知

```ts
type UserProfile = {
  username: string;
  display_name: string;
  role: "user" | "admin" | "ops";
  role_label: "建模者" | "管理员" | "运维";
  auth_source: "auth-server" | "local";
};

type Preferences = {
  theme: "light" | "dark";
  notifications: {
    system: boolean;
    l2_step_done: boolean; l2_needs_input: boolean; l2_error: boolean;
    l4_finished: boolean; l4_new_suspension: boolean; l4_error_or_stopped: boolean;
  };
};

type Notification = {
  id: string; kind: string;       // 取值见 4.4
  title: string; body: string;
  project_id: string | null; project_name: string | null;
  action: { label: string; page: string; params?: Record<string, string> } | null;
  created_at: time; read: boolean;
};

type AdminUser = {
  username: string; display_name: string;
  role: "user" | "admin" | "ops";
  online: boolean; last_seen_at: time | null;
  project_count: number; locked_until: time | null;
};
```

### 7.3 项目

```ts
type ProjectMeta = {
  id: string; name: string; package_name: string; goal: string;
  owner: string; created_at: time; archived: boolean; archived_at: time | null;
  engine: string; engine_label: string;
  build_mode: "L2" | "L4";
  research_mode: "web" | "local";
  version_label: string;          // 主线当前版本，如 "v0.12"
  head: string;
  last_release: { version: string; at: time } | null;
  last_activity_at: time | null;
  created_from: "wizard" | "import" | "clone" | "example";
};

// admin、ops 看到的元数据视图（1.4），不含目标、资料、产物
type ProjectMetaView = {
  id: string; name: string; owner: string | null;
  stage_label: string;            // 如 "建模"、"校验"、"可发布"
  build_mode: "L2" | "L4";
  archived: boolean; created_at: time; last_activity_at: time | null;
  disk_bytes?: number;
};

type ProjectCard = ProjectMeta & {
  industry_label: string | null;
  slices: { total: number; confirmed: number; cells: DisplayState[] } | null;   // null = 分段待规划
  inbox_count: number;
  status: ("active" | "autobuild" | "autobuild_done" | "publishable" | "archived")[];
  autobuild: {
    run_id: string; run_no: number;
    state: "queued" | "running" | "pausing" | "paused" | "completed" | "stopped";
    slice_done: number; slice_total: number; progress_pct: number; eta_ms: number | null;
    decisions: number; suspended: number; budget_used_pct: number;
    needs_review: number; duration_ms: number;
  } | null;
  publishable: { gates_passed: true; last_release: { version: string; at: time } | null } | null;
  landing_page: "wb-l2" | "wb-materials" | "l4-run" | "inbox";   // 点卡片打开哪页
};
```

### 7.4 文件、资料、检索

```ts
type FileNode = { name: string; path: string; type: "dir" | "file"; size?: number; children?: FileNode[] };

type SearchHit = {
  group: "entries" | "materials" | "reports" | "knowledge" | "artifacts";
  title: string; snippet: string;
  target: EntryRef | ArtifactRef | { kind: "material"; path: string; anchor?: string }
        | { kind: "report"; report_id: string; item_id?: string } | { kind: "knowledge"; id: string };
  page: string;                   // 点了去哪页
};

type PurposeOption = { value: "goal" | "schema" | "term" | "evidence" | "auto"; label: string; description: string };

type Material = {
  path: string; name: string; group: "diagrams" | "excel" | "others";
  title: string; type_label: string;   // 资料名（去扩展名）与类型（PDF、Word、Excel…），资料列表按它们列、不按 group 分组（F4b-paths）
  kind: "doc" | "xls" | "csv" | "pdf" | "drawio" | "html" | "image" | "text";
  size: number; uploaded_at: time;
  sheets?: number; rows?: number;
  purpose: PurposeOption["value"]; purpose_auto: boolean;
  reading_mode: "on_demand" | "full";
  is_revision: boolean; revision_task: { conversation_id: string; label: string } | null;
  is_template: boolean;           // 如「图示说明定义.drawio」资源模板
  referenced: number;
  coverage: { read_pct: number; label: string } | null;
  preview: "doc" | "xls" | "csv" | "drawio" | "html" | "text" | null;
  revision: string;
  text: MaterialText | null;      // 流程图为 null
  // 数据 Schema 表格（xlsx、xlsm、csv）的 Sheet 范围（ADR-0096，即 develop resource-meta 的 schema_scopes[path]）：结构同 R-16，
  // 范围对不上当前文件时带 error；Excel 还没转出文本版时只有 {pending: true, sheets: [], included_sheets: [], reference_sheets: []}；别的资料为 null
  schema_scope: object | null;
  // 「疑似数据表」（develop 的 schema_candidates[path]）：用途仍是「由助手判断」、结构上像数据表的表格资料，否则 null
  schema_candidate: { tables: string[] } | null;
};

// 资料的文本版（ADR-0073）
type MaterialText = {
  state: "native" | "converting" | "converted" | "unsupported" | "failed" | "waiting";
  // native 本身是文本；converting 转换中（含排队）；waiting 连接器网关不可用，恢复后自动补转
  reason: string | null;          // 暂不支持、失败的原因：「扫描件或图片型 PDF，暂不支持」「转换超时」
  note: string | null;            // 转换说明：「只转了前 1000 页（共 1200 页）」「第 12–23 页的图片里的文字没有转」
  converter: { id: string; label: string; version: string } | null;
  updated_at: number | null;
  has_text: boolean;              // 工作区里有对得上原文件的文本版
};

type MaterialMapItem = {
  path: string; name: string;     // 路径与文件名照留（给发命令、定位用）
  label: string;                  // F4c：界面上显示的资料名（不带扩展名，文本版映射回原资料）
  kind: Material["kind"];
  purpose: PurposeOption["value"]; purpose_auto: boolean;
  read_pct: number;
  status: "done" | "reading" | "queued" | "partial";
  position: string | null;        // 「读到 §10 · 还剩 §10–§12」
  queue_reason: string | null;    // 「读完 02 后开始」
  contradiction: string | null;   // 「与规则 §9.2 有 1 处矛盾」
  blocked: { reason: string; unread_parts: string[] } | null;   // 没读完不能进入下一步
  counts: { objects: number; rules?: number; terms: number; fields?: number };
};

type MaterialMap = {
  total: number; read_pct: number;
  analysis_mode: "web" | "local"; web_available: boolean;
  items: MaterialMapItem[];
  problems: { id: string; kind: "contradiction" | "missing"; text_md: string;
              display_md: string;  // F4c：给人看的正文，资料路径换成「资料名 · 位置」，其余路径换业务名
              anchors: string[]; state: "open" | "promoted" | "resolved" }[];
  verification: { total: number; passed: number; unverifiable: number; pending: number; checked_at: time | null };
  can_generate_blueprint: boolean;
};

type Feedback = {
  id: string; source: { service: string; run: string };
  release_version: string | null; received_at: time;
  state: "open" | "handled" | "dismissed";
  summary: string;                // 「评测失分用例 2 条 · 都和「临时放宽」有关」
  items: { case_id: string; question: string; expected: string; actual: string;
           loss_reason: string | null; suspected: (EntryRef | ArtifactRef)[]; related: string | null }[];
  handled_version_label: string | null;
};
```

### 7.5 Studio 与知识

```ts
type EntryRow = {
  id: EntryId | string; kind: EntryKind | "flowchart"; key: string; name: string;
  refs_out: number; refs_in: number;
  status: "confirmed" | "pending" | "stale" | "draft" | "editing";   // editing = 本人有未提交且未失效（非 stale）的草稿
  needs_review: boolean; locked: boolean;
  plugin: { name: string; label: string } | null;
  updated_at: time | null;                     // 最后更新时间（ST-03）
};

type EvidenceCell = {
  anchor: string | null; label: string;       // 业务说法，如「ERP · 商品品类」，不显示原始锚点
  scheme: AnchorResolution["scheme"] | null;
  confidence: "declared" | "observed" | "inferred" | null;
  expert_confirmed: boolean; missing: boolean;
};

type Entry = {
  id: EntryId; kind: EntryKind; key: string; name: string; path: string;
  description: string;
  status: EntryRow["status"]; needs_review: { source: "autobuild" | "upgrade" | "import"; run_id?: string } | null;
  sections: { level: number; title: string; content_md: string }[];
  fields: { name: string; column: string; type: string; unit?: string; primary_key?: boolean; evidence: EvidenceCell }[];
  relations: {
    declared: { target: EntryRef; label: string }[];
    mentions: { target: EntryRef; where: string }[];   // 「在描述中提到」
  };
  backlinks: { source: EntryRef | ArtifactRef; relation: string; locked: boolean }[];
  data_source: { kind: "table" | "anchor" | "none" | "unset"; value: string | null; label: string };
  evidence_snapshots: Record<string, string>;   // frontmatter 里的 evidence_snapshots
  counts: { refs_out: number; refs_in: number; fields: number; evidence: number };
  revision: string; head: string;
  lock: ScopeLock | null;
  draft: Draft | null;
};

type EntryForm = {
  name: string; key: string; description: string;
  fields: { name: string; column: string; type: string; unit?: string; evidence_anchor?: string | null; expert_confirmed?: boolean }[];
  relations: EntryId[];
  data_source: { kind: "table" | "anchor" | "none"; value?: string };
  sections?: Record<string, string>;            // 操作的步骤、规则的条件等，按元模型章节
};

type Draft = {
  author: string; updated_at: time; base_revision: string; form: EntryForm | null; content: string;
  state: "active" | "stale";   // stale = 基于的修订已不是条目当前修订（develop !608）
};

type KnowledgeEntry = {
  id: string; title: string;
  layer: "project" | "industry";
  pack: string | null;            // 行业层的包名
  type: "correction" | "convention" | "term" | "domain_rule";
  scope: { kind: "global" } | { kind: "entries"; entries: EntryRef[] };
  confidence: "high" | "medium" | "low";
  source: { kind: "conversation" | "material" | "pack"; label: string; at?: time; anchor?: string };
  overrides_industry: boolean; readonly: boolean;
  revision: string; updated_at: time;
};

type ConflictFinding = { kind: "duplicate" | "scope_conflict" | "broken_ref"; with: string; note: string };
```

### 7.6 版本、变更集、写入范围

```ts
type VersionSource = "assistant" | "web" | "studio" | "mcp" | "autobuild" | "import" | "plugin" | "revert";

type Version = {
  commit: string; n: number; version_label: string;   // "v0.{n}"
  at: time; source: VersionSource; source_label: string;
  author: string; summary: string;
  tags: { name: string; label: string }[];
  release: string | null;                             // 如 "0.13.0"
  current: boolean;
  changeset_id: string | null;
  run: { run_id: string; run_no: number; slice_name: string } | null;
  needs_review: number;
  trailer: { source: string; author: string; changeset?: string; packs?: string; node?: string };   // 详设 4.3
};

type Changeset = {
  id: string;                     // "cs-31"
  source: "assistant" | "autobuild" | "studio" | "mcp" | "import" | "plugin";
  build_mode: "L2" | "L4" | null;
  purpose: string;
  state: "open" | "pending_confirm" | "merged" | "discarded" | "conflict";
  base: string;                   // 基准主线提交
  conversation_id: string | null; run_id: string | null; slice_id: string | null;
  created_at: time; updated_at: time;
  merged: { commit: string; version_label: string; at: time } | null;
  entity_count: number;
};

type ScopeLock = {
  state: "none" | "assistant_running" | "pending_confirm" | "autobuild_slice";
  changeset_id: string | null; conversation_id: string | null; run_id: string | null;
  slice: { id: string; name: string } | null;
  node: { id: string; version: string } | null;
  entries: EntryRef[];            // 范围内的本体条目
  artifacts: ArtifactRef[];       // 范围内的插件产物（场景、求解契约、流程图……）
  paths: string[];                // produces 声明的路径模式
  reason_text: string;            // 「助手正在改「超量与审批提示」这一段……」
  since: time | null;
};
```

`ScopeLock` 的范围按详设 4.3 计算：当前节点 `produces` 落到当前分段的条目，加这一轮已经改过的条目，加等用户决定的决策卡在改动预览里列出的条目。节点不按段执行时，范围是 `produces` 声明的整类条目。

### 7.7 会话、执行记录、命令

```ts
type Conversation = {
  id: string; title: string; title_manual: boolean;
  created_at: time; updated_at: time;
  live: boolean; state: ConversationState["state"];
  kind: "chat" | "autobuild";     // 自主构建的对话标题固定「自主构建 · 运行 #n」
  run_id: string | null;
};

type PoolStatus = { max: number; active: number; queued: number; idle_ttl_s: number };

type ContextUsage = {
  used: number; window: number; pct: number; compact_at_pct: number;
  breakdown: { rules: number; materials: number; conversation: number; tool_results: number } | null;
  last_call: { input: number; output: number; estimated: boolean } | null;
};

type ConfigPending = { changes: { kind: string; summary: string }[]; effective: "next_message" | "next_safe_point" };

type ConversationState = {
  conversation_id: string;
  state: "idle" | "connecting" | "running" | "waiting_user" | "queued" | "error" | "closed";
  live: boolean; turn_started_at: time | null; queue_position: number | null;
  last_seq: number; history_count: number;
  context: ContextUsage | null;
  pending_permission: { request_id: string; title: string; expires_at: time } | null;
  open_questions: string[];
  active_exec: ExecRecord | null;
  open_changeset: Changeset | null;
  config_pending: ConfigPending | null;
  autobuild_run_id: string | null;
  pool: PoolStatus;
};

type ExecRecord = {
  id: string;                     // "ex-0412"
  conversation_id: string; engine_instance: string;
  node: { id: string; version: string; title: string };
  slice: { id: string; name: string } | null;
  run_id: string | null;
  command_label: string;          // 业务名，如「操作提取」
  phase: { index: number; total: number; label: string };   // 「写入即查（步骤 3 / 4）」
  state: "running" | "waiting" | "interrupted" | "done" | "abandoned";
  input_fingerprint: string;
  started_at: time; heartbeat_at: time; ended_at: time | null;
  interrupted: { at: time; reason: "user" | "restart" | "recycled" | "engine_exit" | "shutdown" | "ops"; reason_text: string } | null;
  saved: { files: number; version_label: string | null };
  resumable: boolean; not_resumable_reason: "inputs_changed" | "node_upgraded" | null;
};

type CommandItem = {
  id: string; label: string; description: string;
  command: string;                // 不在界面显示
  pack: string; node_id: string | null;
  args_schema: object | null;
  available: boolean; unavailable_reason: string | null;
};

type CommandTask = {
  task_id: string;
  origin: { page: string; label: string };
  title: string;                  // 「助手正在批量推演 23 个场景」
  state: "queued" | "running" | "waiting_user" | "done" | "failed" | "interrupted" | "cancelled";
  percent: number | null; step_label: string | null;   // 「已推演 9 / 23」
  conversation_id: string; message_id: string; exec_id: string | null;
  started_at: time; finished_at: time | null;
  result: { summary: string; refs: (EntryRef | ArtifactRef)[] } | null;
};

type ProgressStep = { id: string; text: string; meta: string; state: "done" | "running" | "waiting" | "failed" | "info"; duration_ms: number | null };
```

### 7.8 流程、建议、蓝图

```ts
// 详设 6.6：8 种状态 + 2 个附加标记
type NodeState = "not_ready" | "ready" | "running" | "pending_confirm" | "confirmed" | "stale" | "skipped" | "blocked";

type FlowNode = {
  id: string; title: string;      // core.validate 的 title 恒为「校验」（11-05 起场景模拟包不再改它的显示名）
  kind: "agent" | "script" | "human" | "external";
  placement: "spine" | "step" | "branch" | "gate" | "on_demand";
  section: "required" | "pre_publish" | "optional" | "on_demand";
  state: NodeState;
  flags: { needs_review: boolean; review_source?: "autobuild" | "upgrade" | "import"; disabled: boolean; backfill: boolean };
  display_state: DisplayState;
  progress: { done: number; total: number; label: string } | null;   // 「3/5 段」
  version: string;                // "core@1.4.2"
  per_slice: boolean;
  note: string | null;            // 「登记了业务系统数据源才有这一步」
  blocks: string[];
  actions: { kind: "start" | "skip" | "restore" | "refresh" | "confirm"; label: string; target?: string }[];
  plugin: { name: string; label: string } | null;
};

type SliceStageState = NodeState | "draft" | "auto";   // auto = 自主完成（已确认 + 需复核）

type Flow = {
  flow_hash: string;
  nodes: FlowNode[];
  edges: { from: string; to: string }[];
  slice_stages: { id: "object" | "operation" | "rule" | "check" | "scenario"; label: string; plugin?: string }[];
  slices: { id: string; name: string; core_flow: string;
            group: "done" | "now" | "wait" | "todo";
            stages: Record<string, SliceStageState>;
            note: string; no_rules: boolean }[];
  suggestions: Suggestions;
  notices: { kind: "new_materials" | "changeset_conflict" | "assembly_pending"; count?: number; files?: string[];
             text: string; action: SuggestionAction }[];
};

type SuggestionAction =
  | { type: "send"; text: string; command?: string; args?: object }
  | { type: "resume_exec" | "abandon_exec"; exec_id: string }
  | { type: "retry" }                                   // F7：重试出错的一轮（S-14）
  | { type: "focus_question"; question_id?: string }    // F7：滚动到等你回答的问题卡
  | { type: "merge_changeset" | "discard_changeset" | "show_changes"; changeset_id: string }
  | { type: "start_node"; node_id: string; slice_id?: string }
  | { type: "navigate"; page: string; params?: Record<string, string> }
  | { type: "autobuild"; op: "pause" | "stop_at_safe_point" | "convert_to_l2" | "details" }
  | { type: "clear_input" };

type Suggestion = { id: string; label: string; icon?: string; primary: boolean; disabled: boolean;
                    reason: string; priority: 1 | 2 | 3 | 4 | 5; action: SuggestionAction };

type Suggestions = {
  mode: "normal" | "running" | "failed" | "interrupted" | "waiting_decision" | "autobuild_controls";   // F3 起没有 pending_confirm；历史里留下的前端不画「确认生效」
  primary: Suggestion | null;
  secondary: Suggestion[];        // 最多 2 个
  all_commands: true;             // 末尾固定有「全部命令」
  note: string | null;            // F7：主推下面的一行说明（出错原因、能力升级后按新版从头做、上游哪里改过）
  rev: number;                    // F7：越大越新；F7 之前的历史记录没有，按 0 算
};

type BlueprintSlice = {
  id: string; name: string; core_flow: string;
  scale: { objects: number; rules: number };
  depends_on: string[]; order: number;
  order_label: string;            // 「推荐先做」「第 2 个」「建议最后做」
  excluded: boolean;
};

type BlueprintSliceInput = Omit<BlueprintSlice, "order" | "order_label" | "scale"> & { merged_from?: string[] };

type Blueprint = {
  version_no: number; version_label: string;   // 「草案 v2」
  state: "draft" | "confirmed";
  goal_md: string;                // 项目的建模目标（P-05 goal），蓝图不另存（F4b）
  goal_anchor: null;              // F4b 起恒为 null；out_of_scope 已取消
  slices: BlueprintSlice[];
  terms: { aligned: string[];
           ambiguous: { id: string; variants: string[]; chosen: string | null }[] };
  questions: { id: string; title: string; background: string;
               kind?: "goal";             // 资料和建模目标对不上时的目标问题，最多一个
               suggested_goal?: string | null;   // 目标问题的建议改写；选项为「改成：…」「保持原目标」
               options: { id: string; label: string }[];
               answer: { option_id?: string; custom_text?: string } | null }[];
  research: { verified: boolean; report_id: string | null };
  evidence_hint: { sources: { id: string; label: string; environment: string }[]; text: string } | null;
  revision: string; updated_at: time;
};
```

### 7.9 待处理

```ts
type InboxItem = {
  id: string;                     // "<kind>:<来源ID>"
  kind: "decision" | "evidence_conflict" | "change_list" | "review" | "suspension" | "stale" | "resumable"
      | "feedback" | "report" | "changeset_conflict" | "format_migration" | "notification"
      | "check_findings";             // F8：平台代存的那一轮检查没过（最近一个合入的变更集 collected 且有错误）
  kind_label: string;
  title: string; meta: string;    // 「4 个选项 · 推荐 A」「置信度 0.72 · 记为假设」
  slice: { id: string; name: string } | null;
  run: { run_id: string; run_no: number; at: time } | null;
  source: "assistant" | "autobuild" | "upgrade" | "import" | "materials" | "restart" | "external" | "plugin";
  confidence: number | null; high_confidence: boolean;
  pack: string | null;            // 归属的插件，插件页角标用
  impact: { label: string; items: string[] }[];   // 右栏「影响范围」
  created_at: time;
  actions: string[];              // 可用的 IN-03 action
  batchable: boolean;             // 低把握、低置信条目为 false
};

type ConflictDetail = {
  question: Question;
  sides: { label: string; type_label: "资料" | "系统元数据" | "发布日期"; value: string; note: string; anchor: string; date?: string }[];
  writes_to: EntryRef;            // 裁决写进这个对象的「证据」章节
};

type StaleDetail = {
  upstream_diff: { title: string; lines: DiffLine[] };
  impact: { expected_before: string; now: string; action: string; eta_ms: number };
  siblings: number;               // 「全部刷新 3 项」
};
```

### 7.10 增量更新与报告

```ts
type ChangePoint = {
  id: string; n: number;
  condition: string; value: string; scope: string;
  anchor: string; anchor_label: string;
  confidence: "high" | "medium" | "low"; confidence_note: string | null;
  state: "pending" | "accepted" | "rewritten";
  rewritten_text: string | null;
  impact: (EntryRef | ArtifactRef)[];
};

type ChangePointInput = Partial<Pick<ChangePoint, "id" | "condition" | "value" | "scope" | "anchor">> & { rewritten_text?: string };

type ChangeList = {
  id: string; source: { path: string; label: string };
  points: ChangePoint[];
  state: "editing" | "confirmed" | "executed";
  changeset_id: string | null;
  impact_summary: { artifacts: number; scenarios: number; duration_ms: number };
  revision: string;
};

type Impact = {
  duration_ms: number;
  phases: { label: string; duration_ms: number }[];   // 定位产物、算下游、挑回归场景
  stale: { target: EntryRef | ArtifactRef; note: string | null }[];
  regression: { affected: ArtifactRef[]; guard: ArtifactRef[] };
  semantic_scope_text: string;    // 「只对这 6 个产物做语义判断，其余不动」
};

type ReportSummary = {
  id: string; name: string; type: string; type_label: string;
  at: time; summary: string;      // 「错误 1 · 警告 2」
  path: string;
};

type ReportItem = {
  id: string; code: string | null;   // 如 E-ROUTE-01
  level: "error" | "warning";
  text: string;
  location: { label: string; target: EntryRef | ArtifactRef | null; page: "studio-table" | "studio" | null };
  status: "expert" | "fixing" | "pending" | "fixed" | "skipped";   // skipped：用户明确跳过（报告里记 ⏭️），算已处理
  status_note: string;            // 「第 2 轮仍未收敛」
};

type Report = ReportSummary & {
  scope_text: string; mode: "regression" | "full" | "research";
  duration_ms: number;
  previous: { at: time; summary: string } | null;
  progress: { handled: number; total: number; note: string };
  items: ReportItem[]; items_next_cursor: string | null;
  repair: { rounds: { n: number; count: number }[]; limit: 2; exhausted: boolean } | null;
  arbitration: Question | null;
  extras: object | null;
};
```

### 7.11 插件、扩展项、产物

```ts
type PackCategory = "capability" | "industry" | "system" | "provider" | "node" | "exporter";

type PackUi =
  | { kind: "kernel"; note: string }
  | { kind: "page"; pages: { id: string; title: string }[]; note: string }
  | { kind: "subpages"; subpages: { parent: string; id: string; title: string }[]; note: string }
  | { kind: "none"; note: string };

type PackCard = {
  name: string; version: string; label: string; description: string;
  category: PackCategory;
  publisher: { kind: "official" | "tenant" | "private"; label: string };   // private = 自己的私有包
  ui: PackUi | null;              // 只有能力包有
  batch: "first" | "second" | "third" | null;
  badges: ("new" | "recommended")[];
  meta: string | null;            // 「22 命令 · 18 子代理」
  latest: string; channel: "stable" | "beta";
  project_status: string | null;  // 见 PL-01
  required: boolean;              // 必装包（core），不带项目也是 true
  locked_version: string | null;
  usage_note: string | null;      // 「mes-prod 在用」
  converter: { types: string[]; default_for: string[]; license: string } | null;   // 多源连接器里的文档转换器（ADR-0073）
  collector: { kind: string; evidence_types: string[]; input: "connection" | "artifact"; self_service: boolean;
               installed: boolean; readonly_method: string; license_note: string } | null;   // 网关里的采集类连接器，包仓库里的包为 null
};

type CompatCheck = { id: "kernel_api" | "engines" | "depends" | "names" | "flow" | "page_api";
                     label: string; ok: boolean; detail: string };

type ProjectPack = {
  name: string; label: string; category: PackCategory;
  ui: PackUi | null;
  locked_version: string; latest_available: string | null; channel: "stable" | "beta";
  status: "required" | "enabled" | "disabled" | "pending";
  pending: { from: string | null; to: string | null; kind: "enable" | "upgrade" | "rollback" | "disable";
             effective: "next_message" | "next_safe_point" | "next_autobuild" } | null;
  enabled_at: time | null;
  actions: ("upgrade" | "rollback" | "disable" | "enable" | "cancel_pending")[];
  rollback_to: string | null;
};

// 详设 5.2 的 manifest 投影
type ManifestSummary = {
  name: string; version: string; kind: "capability" | "knowledge" | "node" | "provider" | "exporter";
  kernel_api: string; engines: string[]; depends: Record<string, string>;
  contributes: { commands: number; agents: number; skills: number; scripts: number; templates: number;
                 hooks: number; nodes: string[]; entries: number; pages: string[];
                 artifacts: string[]; views: string[]; reports: string[]; gates: number;
                 config: number; metrics: number };
  checksums_ok: boolean; size: number; files: number;
};

type StaticCheck = { id: string; label: string; detail: string; result: "pass" | "warn" | "fail" };

type PackUpload = {
  id: string; filename: string; size: number; uploaded_at: time; uploaded_by: string;
  tenant_label: string | null;
  scope: "private" | null;   // private = 普通用户上传（PL-22），只归上传者
  state: "checking" | "passed" | "failed" | "published" | "rejected" | "held" | "installed";
  summary: { pass: number; warn: number; fail: number };
  manifest_ok: boolean; checksums_ok: boolean;
};

type Extensions = {
  active_hash: string; pending_hash: string | null; page_api: string;
  nav: { id: string; title: string; icon: string; kind: "kernel" | "plugin"; pack?: string;
         badge?: { source: "inbox"; count: number }; order: number; overflow: boolean }[];
  subpages: { parent: string; id: string; title: string; pack: string }[];
  pages: { id: string; pack: string; page_api: string;
           data: { artifacts: string[]; reports: string[]; nodes: string[] };
           actions: { id: string; label: string; command: string; hint: string }[];
           pending: boolean }[];   // pending = 下一条消息生效前，按钮显示「下一条消息生效」
  entries: { id: string; kind: "quick_action" | "extra_action" | "source_wizard"; label: string;
             where: string; command?: string; pack: string }[];
  artifact_types: string[];
  views: string[];
  gates: { id: string; label: string; pack: string }[];
  config: { key: string; pack: string }[];
  disabled_pages: { id: string; pack: string; title: string }[];
};

// 详设 5.9.4 的登记投影
type ArtifactType = {
  type: string; title: string; pack: string; pack_version: string;
  format: "md-frontmatter" | "yaml" | "json" | "jsonl" | "csv" | "drawio" | "md-report";
  path: string; key: string;
  schema: { version: string; fields: { name: string; type: "text" | "number" | "enum" | "date" | "ref" | "anchor" | "markdown" | "code" | "table";
                                      label: string; enum?: { value: string; label: string; tone?: string }[]; required: boolean }[] };
  sections: string[];
  refs: { field: string; kind: EntryKind }[];
  status: { field: string; map: Record<string, string> } | null;
  view: { list: { columns: string[]; filters: string[]; group_by?: string };
          detail: { layout: "sections" | "table" | "custom"; side: string[]; component?: string };
          card: { title: string; chips: string[] } };
  studio: boolean;                // true 时出现在 Studio 条目表（流程图）
};

type ArtifactRow = {
  key: string; title: string;
  fields: Record<string, any>;
  status: { value: string; label: string; tone: string } | null;
  stale: boolean; locked: boolean;
  updated_at: time; revision: string;
};

type Artifact = ArtifactRow & {
  type: string; format: string; format_version: string;
  sections: Record<string, string>;
  table: { columns: string[]; rows: string[][]; total_rows: number } | null;
  content: any;                   // yaml/json 原样结构；diagram.flowchart 为 FlowchartModel
  refs: (EntryRef & { field: string })[];
  stale_reason: string | null;
  lock: ScopeLock | null;
  path: string;
};

type FlowchartModel = {
  storage: "standard" | "drawio_compressed";   // 压缩格式只能用 drawio 高级编辑
  process: { name: string; description: string; roles: string[]; inputs: string[]; exceptions: string[]; handoffs: string[] };
  nodes: { id: string; type: "start" | "step" | "decision" | "data" | "end"; label: string; description: string;
           x: number; y: number; refs: EntryRef[]; pending: boolean }[];
  edges: { id: string; type: "seq" | "cond" | "exc" | "data"; from: string; to: string; label: string; condition: string }[];
  drawio_xml: string | null;
  operation: EntryRef | null;
};
```

### 7.12 脚本与求解

```ts
type SandboxSummary = { available: boolean; network: "denied"; python: string | null; checked_at: time };

type ScriptRow = {
  script: EntryRef; operation: EntryRef | null;
  contract: { status: "passed" | "failed"; findings: Finding[] };
  descriptor: "synced" | "stale" | "none";
  sandbox: { status: "passed" | "failed" | "not_run"; duration_ms: number | null; ran_at: time | null };
  actions: ("regenerate_descriptor" | "fix" | "regenerate")[];
};

type SolverContractView = {
  revision: string; revision_no: number; enabled: boolean;
  operations: {
    solver_key: string; operation: EntryRef; enabled: boolean;
    mode: "template" | "manual";
    problem_family: string; capabilities: string[];
    field_units: { field: string; unit: string; note: string }[];
    evaluator: { path: string; kind: "declarative" | "script" } | null;
    option_labels: [string, string] | null;
    result_title: string | null; result_texts: [string, string] | null;
    sidecars: Record<"entry" | "input_schema" | "result_schema" | "fact_snapshot_schema" | "presentation", string>;
    managed: string[];            // 受管的附属文件
  }[];
  findings: Finding[]; gate: Finding[];
  receipt: { status: "pass" | "required" | "stale" | "failed"; latest: Receipt | null };
  available_operations: EntryRef[];
  evaluators: { path: string; file: string }[];
  problem_families: { id: string; label: string; capabilities: string[] }[];
  supported_profiles: string[]; template: object;
  lock: ScopeLock | null;
};

type Receipt = {
  id: string; receipt_id: string;
  revision: string; revision_no: number | null;
  release_version: string | null; solver_keys: string[];
  status: "passed" | "failed"; authoritative: boolean;
  contract_digest: string; request_digest: string; result_digest: string;
  candidate_count: number | null; error_code: string | null; message: string | null;
  completed_at: time; received_at: time; by_token: string;
};
```

### 7.13 证据源

```ts
type EvidenceSourceRef = { id: string; display_name: string; environment: "production" | "test" };

type EvidenceSource = EvidenceSourceRef & {
  provider: { id: string; version: string; label: string };
  connection: Record<string, string | number>;   // 不含密钥
  cred_ref: string | null;        // "cred://mes-prod/3"
  secret_fields: { name: string; set: boolean; written_at: time | null }[];
  scope_allowlist: string[]; scope_summary: string;   // 「mes.* 42 表」
  egress: { host: string; port: number }[];
  access_window: { days: "daily" | string[]; start: string; end: string; tz: string } | null;
  budget: { max_rows: number; max_bytes: number; max_duration_s: number };
  rate_limit: { concurrency: number; qps: number; query_timeout_s: number };
  masking_policy: string;
  authorization_note: string | null; owner: string;
  last_job: { at: time; state: CollectionJob["state"] } | null;
  authorized_for_project: boolean | null;
};

type CollectionJob = {
  id: string; project_id: string; source: EvidenceSourceRef;
  operation: "discover" | "snapshot" | "sample";
  scope_text: string; incremental_base: string | null;
  state: "queued" | "running" | "succeeded" | "partial" | "failed" | "cancelled";
  phase: string | null;           // 「采集字段与约束」
  progress: { done: number; total: number; unit: string; pct: number } | null;
  queue_reason: string | null;    // 「等待访问窗口 22:00，前面还有 1 个任务」
  started_at: time | null; ended_at: time | null; duration_ms: number | null;
  result: { snapshot_id?: string; message: string; commit?: string;
            inventory?: { items: number; skipped: number } } | null;   // inventory：discover 的结果（F16）
  error: { code: string; message: string } | null;   // 「401：凭据已过期」
  resumed_from: string | null; checkpoint: boolean;
  initiated_by: string; consent: { by: string; at: time } | null;
  actions: ("cancel" | "browse" | "resume" | "retry" | "notify_admin")[];
};

// 详设 5.8.4 manifest.json 的投影
type SnapshotSummary = {
  id: string; source: EvidenceSourceRef;
  provider: string;               // "db-mysql@1.0.0"
  complete: boolean; coverage_pct: number;
  records: Record<"entity" | "field" | "relation" | "constraint" | "operation" | "rule" | "flow" | "profile" | "sample" | "term", number>;
  referenced: number;
  masking_policy: string;
  started_at: time; ended_at: time;
  previous: string | null; job_id: string;
  git_bytes: number;
};

type SnapshotRef = { id: string; source_id: string; complete: boolean };

type EvidenceRecord = {
  anchor: string; type: string; name: string; label: string | null;
  parent_anchor: string | null;
  confidence: "declared" | "observed" | "inferred";
  evidence: { provider: string; method: string };
  attrs: Record<string, any>;
  fields?: { name: string; type: string; note: string; confidence: string; masked: boolean; anchor: string;
             pk?: boolean; fk?: boolean; nullable?: boolean }[];   // pk、fk、nullable：数据库列（F16）
  ddl?: string;
  samples?: { columns: string[]; rows: string[][]; shown: number; max: 20;
              masked_columns?: string[]; snapshot_id?: string };   // F16
  profile?: { column: string; null_pct?: number | null; distinct?: number | null;   // F16
              top_k?: { value: string; pct: number }[]; stats?: { min: number; max: number; null_pct: number } }[];
  rows?: number;
  masked?: boolean;               // 数据库列记录的脱敏标记（F16）
  data_type?: string; nullable?: boolean;   // 数据库列记录（F16）
  snapshot: SnapshotRef;
  untrusted_content?: boolean;    // 快照内容来自目标程序（F13）
};
```

### 7.14 发布

```ts
type GateResult = {
  id: string; order: number; title: string;
  source: { kind: "platform" | "pack" | "flow"; label: string; pack?: string };
  status: "pass" | "fail" | "not_run" | "warn";
  summary: string;                // 「3 个对象和数据表对不上」「还没运行 · 挡住发布」
  blocking: boolean; degradable: boolean;
  findings: Finding[];
  action: SuggestionAction | null;   // 如合规检查「去运行」
};

type BindingIssue = {
  object: EntryRef; declared: string | null;
  kind: "declared_missing" | "no_columns" | "declared_unverifiable";
  message: string;
  detail: { attr_columns: string[]; table_columns: string[] } | null;
  candidates: { table: string; matched: number }[];
  fixes: ("set_table" | "set_none" | "set_anchor" | "mark_schema_material")[];
  revision: string;
};

type ReleaseRecord = {
  version: string;                // "0.13.0"
  version_label: string;          // "v0.13"
  commit: string; tag: string;    // "release/0.13.0"
  at: time; by: string;
  format: "ontology" | "solver"; format_label: "普通知识包" | "求解知识包";
  gates: { all_passed: boolean; degraded: boolean; unbound_objects: string[] };
  manifest_attached: true;
  pulls: { at: time; by_token: string; variant: string }[];
  feedback_count: number;
  size: number; sha256: string;
};

type ReleaseManifest = {
  package_name: string; version: string; schema_version: "0.1" | "0.2";
  source_commit: string; released_at: time;
  packs: { name: string; version: string }[];       // 来自 packs.lock，用于版本归因（P4）
  flow_hash: string;
  solver: { revision: string; receipt: { status: string; receipt_id: string } | null } | null;
  degraded: { unbound_objects: string[] } | null;
  files: { path: string; sha256: string; size: number }[];
};
```

### 7.15 自主构建

```ts
type AutobuildPolicy = {
  scope: { mode: "all_pending" | "selected"; slice_ids: string[];
           refresh_stale_first: boolean; generate_scenarios: boolean; include_optional: boolean;
           until_node: string | null };   // 做到哪一步，如 "core.validate"
  budget: { max_duration_min: number; max_tokens: number; max_cost: Money };
  production_collection: { source_id: string; allowed: boolean; window: { start: string; end: string } | null; scope: string[] }[];
  advanced: { confidence_threshold: number;            // 0.5–1.0，默认 0.80
              below_threshold: "conservative_assumption" | "suspend";
              must_suspend_kinds: string[];
              source_priority: string[];                // 资料与证据冲突时的取值顺序
              max_fix_rounds: 1 | 2 | 3 };
  boundary_text: string;          // 硬性不做，只读
};

type AutobuildPreview = {
  preview_token: string; expires_at: time;
  slices: { id: string; name: string; current: string; included: boolean }[];
  budget: AutobuildPolicy["budget"]; production: string; below_threshold_text: string;
  warnings: string[]; queued: boolean; queue_position: number | null;
  busy: { conversation_id: string; waiting: boolean } | null;
};

type AutobuildRunSummary = {
  run_id: string; run_no: number;
  state: "queued" | "running" | "pausing" | "paused" | "stopping" | "converting" | "converted"
       | "completed" | "stopped" | "budget_exhausted" | "failed" | "interrupted";
  current: { slice_id: string; slice_name: string; step_label: string } | null;
  elapsed_ms: number; est_total_ms: number | null;
  tokens: { used: number; limit: number };
  cost: { used: Money; budget: Money };
  slices: { done: number; total: number };
  decisions: number; suspended: number; needs_review: number;
  started_at: time; ended_at: time | null;
};

type AutobuildRun = AutobuildRunSummary & {
  conversation_id: string;
  policy: AutobuildPolicy;
  checks: { errors: number; warnings: number };
  scenarios: { passed: number; total: number };
  fix_rounds: { used: number; limit: number };
  matrix: { slice_id: string; slice_name: string;
            cells: Record<"object" | "operation" | "rule" | "check" | "scenario", "confirmed" | "auto" | "running" | "queued" | "skipped">;
            result: string }[];
  tail_text: string;              // 「跑完全部分段后：合规检查（发布前，留给你）」
  scope_lock_text: string;
  recovered_from_checkpoint: number;
  report_available: boolean;
};

type LedgerEntry = {
  id: string; run_id: string; at: time;
  slice: { id: string; name: string };
  question: string; chosen: string;
  options: Option[];              // 改判用
  confidence: number; threshold: number; assumption: boolean;
  anchors: { n: number; anchor: string; label: string }[];
  affected: (EntryRef | ArtifactRef)[];
  review_state: "pending" | "accepted" | "rejudged";
  rejudged_to: string | null;
  reason_text: string;            // 「低于阈值，按策略采用了保守选项并记为假设，继续往下跑。」
};

type Suspension = {
  id: string; run_id: string; at: time;
  slice: { id: string; name: string };
  title: string; reason: string; handled_as: string;   // 「跳过这一项，先做下一步」
  kind: "question" | "unauthorized_collection" | "unreadable_material";
  options: Option[]; allow_custom: boolean;
  actions: ("upload_text" | "use_generic")[];
  state: "open" | "answered" | "deferred";
};

type AutobuildReport = {
  run_id: string; run_no: number; state: AutobuildRunSummary["state"];
  started_at: time; ended_at: time;
  recovered_restarts: number;     // 「中途服务重启 1 次，已从检查点自动续跑」
  metrics: { duration_ms: number; budget_duration_ms: number;
             slices: { done: number; total: number; partial: string[] };
             produced: { objects: number; operations: number; rules: number };
             checks: { errors: number; warnings: number };
             scenarios: { passed: number; total: number; suspended_failures: number };
             decisions: { total: number; high_confidence: number; needs_look: number };
             suspended: number; cost: Money; tokens: number; budget: Money };
  slices: { slice_id: string; name: string; result: "done" | "partial" | "skipped"; result_text: string;
            produced_text: string; scenarios: { passed: number; total: number };
            version_label: string; revertible: boolean }[];
  note: string;                   // 「产出已确认生效并标「需复核」；复核完之前，发布检查会提醒你」
};
```

### 7.16 MCP、配置、配额、运维

```ts
type McpTokenSummary = {
  id: string; name: string; scopes: string[];
  subject: string; issued_by: string;
  created_at: time; expires_at: time | null; last_used_at: time | null;
  remaining_text: string;         // 「30 天 · 剩 21 天」「永不过期」
};

type ConfigItem = {
  key: string; value: string;     // 密钥只显示引用，如 cred://model/stable/4
  source: "env" | "data_dir" | "pack" | "default"; pack?: string;
  validation: string;             // 「整数 1–64」
  effect: "immediate" | "next_message" | "new_session" | "restart";
  secret: boolean; edit_page: string | null;
};

type QuotaOverride = { id: string; subject: { kind: "user" | "project"; id: string; label: string };
                       item: string; value: number; reason: string; created_at: time; created_by: string };

type Alert = { rule_id: string; name: string; detail: string; level: "critical" | "warning" | "info";
               state: "firing" | "resolved"; since: time; resolved_at: time | null; auto_action: string | null };

type AlertRule = { id: string; name: string; condition: string; window: string;
                   level: "critical" | "warning" | "info"; state: "firing" | "ok"; enabled: boolean;
                   source: "kernel" | string;   // 或包版本，如 "industry-retail@1.0.3"
                   runbook_md: string; auto_action: "breaker" | null };

type MaintenanceState = {
  active: boolean;
  mode: "reject_new_sessions" | "read_only";
  message: string; started_at: time; started_by: string;
  target: { kind: "core_upgrade"; pack: string; version: string } | null;
  steps: { id: "restrict" | "drain" | "upgrade" | "resume"; label: string;
           state: "done" | "current" | "pending"; detail: string; progress_pct: number | null }[];
};

type Drill = { id: string; backup: string; at: time; duration_ms: number;
               result: "passed" | "failed"; detail: string };

type AuditEntry = {
  id: string; ts: time;
  actor: string;                  // 用户名、"agent"、"mcp:<token 名>"、"system"
  actor_kind: "user" | "agent" | "mcp" | "service" | "system";
  type: "write" | "mcp" | "commit" | "plugin" | "evidence" | "ops" | "auth" | "publish" | "changeset"
      | "autobuild" | "user" | "contract";
  action: string;                 // 如 changeset.merge、pack.enable、evidence.collect
  method: string | null; path: string | null; status: number | null;
  project_id: string | null;
  summary: string;                // 不含项目内容
  trailer: Record<string, string> | null;   // agent 提交：Onto-Source、Onto-Changeset、Onto-Packs、Onto-Node
  ip: string | null; trace_id: string | null;
};

type ErrorRecord = {
  id: string;                     // "err-7f3a"
  at: time;
  project: ProjectMetaView | null;
  origin: "session" | "publish" | "collection" | "assembly" | "gate" | "kernel";
  pack: string | null; node: string | null;
  raw_message: string;            // 已脱敏：去掉密钥、连接串、内容摘录
  friendly_message: string;       // 用户看到的那句
  trace_id: string | null;
};
```

### 7.17 提交 trailer 与锚点

本节只引用详设，不重复定义。

- 提交 trailer（详设 4.3）：`Onto-Source`、`Onto-Author`、`Onto-Changeset`、`Onto-Packs`、`Onto-Node`。
  - `Onto-Source` 取 `agent|web|studio|mcp|autobuild|import|plugin|revert`。
  - API 上的 `VersionSource` 是它的界面口径：`agent` 对应 `assistant`。
- 锚点（详设 5.8.5）：`<scheme>:<源ID>/<路径>[#<片段>]`，名字里的 `% / . # @ | [ ]` 和空格一律百分号编码。
  - 文件锚点的片段沿用 `source_extract`：`L12`、`p:3`、`table:1/row:2/cell:3`、`sheet:Sheet1!A1`。

---

## 8 本契约定下的不确定项

详设、方案和设计稿没写清、由本契约定下的点。每条一句理由。实现中要改，按文首的流程走 `contract-changes.md`。

### 8.1 决定清单

**路径与兼容**

1. **运维管理面路径用 `/api/v1/ops`，前端从 `C-01` 取 `ops_base_url`。** 详设只说「独立端口或内网」，前端运维中心和业务页是同一个 SPA，地址必须可配置。
2. **健康检查用 `/api/health/live`、`/api/health/ready`，不带版本号、免鉴权。** 方案 E7 已定这个路径；A14 要求 `/api/health` 保留；设计稿上的 `/livez`、`/readyz` 只当显示文案。
3. **MCP 传输新增 `/api/v1/onto-mcp/...`，旧 `/api/onto-mcp/...` 永久保留、不进兼容期。** 这是外部协议（②），外部客户端已经配好了地址。
4. **兼容期的旧路径只改写路径，不做响应形状兼容，只在错误体里加 `error` 字段。** 旧前端在同一版本里整体替换，外部调用方（MCP、评测服务）另有专门处理。
5. **Sunset 定为 v1 上线后 6 个月（2027-05-01）。** 覆盖一个内核季度发版周期，再加缓冲。

**通用约定**

6. **错误体统一为 `{code, message, details, trace_id}`。** 现状是 `{error}` 和 `{error, code}` 混用，多数错误没有码，前端无法按码分支。
7. **列表统一游标分页，默认 50，上限 200。** 与 MCP 现有约定一致（详设 4.1）。
8. **乐观并发统一用 `revision` / `base_revision` 加 `409 revision_conflict`；求解契约冲突不再用专用码，改用 `details.kind` 区分。** 一套口径，前端只写一个冲突处理。
9. **写入范围锁返回 `423 write_scope_locked`，`details` 是完整的 `ScopeLock`。** 方案 C10 已写 423；带上完整锁信息，前端能直接渲染锁定条。
10. **时间一律 RFC 3339 UTC 毫秒，金额用 `Money`，币种默认 CNY。** 现状混用 epoch 秒和 ISO；设计稿金额全部用 ¥。

**事件流**

11. **SSE 拆成对话流、项目流、用户流三条，另加运维日志流。** Studio 和插件页没有打开对话，也要收写入范围锁、流程、产物事件。方案 D14 说「`flow.updated` 复用会话事件流」，但会话流按对话划分，承载不了页面级状态。
12. **对话流的事件 id 用持久 seq；项目流和用户流用 `epoch.n` 加内存环形缓冲，断档时发 `stream.reset`。** 项目级事件都能由快照接口重建，没必要持久化。
13. **SSE 不写 `event:` 行，统一 `onmessage` 加信封 `type`。** 与现状一致，前端不用为每种事件单独注册监听。
14. **事件名改为点号小写（`message.user`、`question.asked`），旧事件名不再发出。** 详设 4.1 已经用了 `flow.updated`、`inbox.updated`、`artifacts.updated` 这种命名。
15. **工具调用原文只以 `tool.activity`（`visibility: internal`）发出，用户可见的进展由执行记录阶段归并成 `progress`。** 第 9 章建议 4 要求内部步骤只进执行日志，不外泄。

**上传**

16. **单设暂存上传区 `U-01`，24 小时过期。** 新建向导第 2 步、导入、对话附件，都发生在项目或消息还没确定的时候。
17. **上传上限：资料 20 MiB、项目导入 200 MiB、插件包 200 MiB；资料类型用白名单。** 取设计稿标注；现有后端没有上限。

**项目与会话**

18. **引擎 ID 保持 `claude`、`jiuwen`，界面只用 `label` 和 `channel`。** 项目元数据里已经存了这两个值，改名要迁移；设计稿只要求界面不显示底层实现。
19. **删除项目要求请求体带 `confirm_name`，同时吊销该项目的 MCP token。** 设计稿是强确认；盘点发现现在删项目不吊销 token，属于遗漏。
20. **新增实时名称检查 `P-03`。** 设计稿新建和重命名都实时提示重名。
21. **运行中发消息返回 `deferred`（这一步结束后送达），不返回 409。** 设计稿写「运行中也能补充说明，这一步结束后送达」「补充说明不打断运行」。
22. **页面按钮一律走 `S-20 POST /commands`，返回可追踪的 `CommandTask`。** 详设 5.9.3 要求页面只发命令、显示进度、结果回到列表，需要一个能追踪的对象。
23. **命令缺省发到引擎当前绑定的对话，没有就发到最近更新的一条。** 同一项目同一时刻只有一个在线引擎（14#14）。
24. **偏好（主题、通知开关）和通知存服务端，通知保留 30 天。** 设计稿有铃铛未读数和「全部标为已读」，跨设备也要一致。
25. **本地模式加 `A-02` 进入接口，本地用户 `local` 拥有全部角色。** 设计稿本地模式有「进入工作台」按钮；鉴权关闭时全部放开是现状。

**角色与权限**

26. **JWT 的 `role` 是单值，取 `user`、`admin`、`ops`；admin 可以读运维管理面，写操作只有 ops 能做。** 方案 E8 把运维动作和管理员职责分开，审计里也要分得清是谁做的。
27. **用户管理接口（`AD-*`）在业务面，写操作只有 admin 能调，ops 可以读列表。** 方案 E8 要求用户管理留在管理员；设计稿又把它放在运维中心，以运维身份浏览。
28. **回收站 ops 和 admin 都能操作。** 设计稿回收站在运维中心，删除用户弹窗又写「管理员可以恢复」。
29. **运维、管理员看项目只给 `ProjectMetaView`；审计 `summary` 保留路径和条目 key，但不含正文；错误原文脱敏。** 详设 1.6 规定只看元数据；路径和 key 是定位信息，设计稿审计里也有；方案 E3 要求脱敏。

**Studio、产物、知识**

30. **Studio 提交的三路结果都返回 200 加 `result`，`rejected` 不算 422。** 编辑器把三种结果都当正常流程显示，与现有接口一致。
31. **草稿 PUT 接受 `form`（结构化）或 `content`（原文），由服务端按元模型渲染 Markdown。** 前端重写；序列化放在后端，才能和校验器用同一份元模型（详设 4.4）。
32. **流程图是图示包登记的插件产物 `diagram.flowchart`，Studio 经通用产物接口读写。** 理由见 9.1#2。
33. **知识条目用专用的 `K-*` 接口，不走通用产物接口。** 分层、覆盖、提名是 core 的知识逻辑，通用视图表达不了；列表读法和 `core.knowledge` 产物等价。
34. **`ST-10` 删除被引用的条目返回 `409 entry_referenced`，流程图例外。** 避免断链；设计稿删流程图时只提示「流程图缺失」。

**流程、蓝图、待处理**

35. **节点状态只用详设 6.6 的 8 种，加 `flags`（`needs_review`、`disabled`、`backfill`），另给 `display_state` 对应设计稿的 17 种显示态。** 理由见 9.1#3。
36. **蓝图保存是整份分段列表替换；「按章节拆分」走命令。** 拆分要读资料，不是确定性操作。
37. **待处理在设计稿 8 种之外，再加 `report`、`changeset_conflict`、`format_migration`、`notification`，计入「全部」**（F3 去掉了 `pending_changeset`）。 详设 4.7、8.2、5.9.4 列了这些来源，设计稿的筛选只是子集。
38. **报告里的待处理条目，一份报告合成一项。** 逐条计数会把待处理撑爆；设计稿示例总数 18 也不含逐条报告条目。
39. **待处理的「忽略」记录存 `.onto/run/`，不进 git。** 详设 4.7 规定待处理不另存状态，「忽略」是唯一的界面偏好例外。
40. **变更点清单在执行前可编辑，不受写入范围锁约束。** 清单是这一轮的输入；设计稿要求用户在助手等待时逐条改。

**自主构建**

41. **自主构建先 `AB-03` 预览、拿 `preview_token`，再用 `AB-04` 启动。** 详设 8.5 要求开始前展示范围和预算，用户确认后才跑。
42. **预算加 token 上限。** 设计稿有 token 上限；与配额口径（4.10.1）一致。
43. **自主构建的生产库授权放在策略里（`AB-02`），和项目授权（`EV-11`）分开。** 详设 8.5 说「只能用项目设置里事先授权过的数据源」，设计稿也分两处。

**证据源**

44. **「租户级登记」就是平台全局登记。** 方案 E11 指出代码里没有租户概念。
45. **数据源登记接口放在 admin 下，普通用户走 `EV-03` 提交申请。** 设计稿登记抽屉写「需要管理员权限，你可以先填好不含密钥的部分提交」。
46. **测试连接：admin 可测任意源，项目所有者只能测已授权给本项目的源。** 设计稿项目侧有「测试连接」；凭据只在网关，测试不暴露明文。
47. **生产采集先预览、拿 `confirmation_token`，没带 token 返回 `428 confirmation_required`。** 确认弹窗要展示窗口、规模、限流，必须先算出来；428 正好表示「缺前置条件」。

**MCP 与三份契约**

48. **MCP 新工具名用下划线（`flow_get`、`evidence_search`），文档写点号；evidence 工具 5 个，新增 scope `evidence.read`。** 现有 15 个工具都用下划线，MCP 客户端对点号兼容性差；工具数按详设 5.8.7。
49. **服务令牌前缀 `onto_svc_`，可以限定项目，由 admin 在 `X-06` 管理。** 方案第 6 章 #6 定了服务令牌和 MCP token 分开。
50. **验证回执用方案 A2 给的路径 `/api/v1/projects/{id}/solver/receipts`；包清单和下载与所有者共用 `PB-06`、`PB-07`；另加跨项目列表 `/api/v1/contract/releases`。** A2 的路径已定；评测服务要按时间增量拉取。
51. **图模式是 `PB-07` 的 `variant=graph`，按需渲染，不另打标签。** 方案第 6 章 #1：同一提交的格式变体。
52. **回执的修订号未知返回 422；旧修订的回执照常保存，但不放行门禁。** 回执可能晚到，留档便于追溯。
53. **回执和外部反馈都带幂等键（`receipt_id`、`feedback_id`）。** 评测服务会重试。

**发布与版本**

54. **主线版本号 `n` 只计合入提交（方案 C14），显示 `v0.{n}`；发布版本号 `0.{n}.0`，标签 `release/0.{n}.0`。** 设计稿的发布下拉和发布记录都用主线版本；按 semver，`0.n.0`（n ≥ 2）大于旧的 `0.1.N`，下游去重仍然单调。
55. **发布改为 `POST /releases`，下载单独 GET。** 现在用 GET 发布，有副作用，会被预取或缓存。

**运维**

56. **会话池配置可以在运维面在线修改（`O-16`），覆盖环境变量默认值。** 详设 4.10.3 写「调整并发上限」，理由见 9.2#4。
57. **维护模式两种：`reject_new_sessions` 只拒新会话，`read_only` 拒绝一切写请求（运维面和登录除外）。** 详设只说「设为只读或拒绝新会话」，没写对写请求的影响。

### 8.2 有接口、设计稿没有画入口

主控决定是否补页面。在补之前，这些接口只供运维脚本和后续版本使用。

| 接口 | 用途 | 建议入口 |
|---|---|---|
| `X-06` | 服务令牌管理 | sys 页新增「对外契约」一节 |
| `SY-18` | 模型单价表（费用计算） | sys 页「模型接入」卡里加单价 |
| `AD-06`、`AD-07` | 无主项目列表、指定所有者 | ops-users「用户管理」加一个「无主项目」筛选 |
| `AD-08`～`AD-10` | 管理员审核申请：插件安装、数据源登记、知识提名 | 通知进入；plugin-upload 加「申请」页签 |
| `O-05` | 链路检索 | ops-obs 链路卡的搜索框 |

---

## 9 矛盾与取舍

### 9.1 详设与设计稿之间

| # | 矛盾 | 详设 | 设计稿 | 本契约按 |
|---|---|---|---|---|
| 1 | 运维、管理员能看到多少项目信息 | 1.6、4.10.1：只看元数据（名称、所有者、阶段、占用） | ops-users 审计「内容」列有文件名、对象名、表名；错误留档有表名、凭据引用、规则文件行号 | 详设。只保留路径、条目 key 这类定位标识，不含正文；错误原文脱敏 |
| 2 | 流程图归在哪 | 4.4「插件不能新增实体类型」；5.9.3 规则 3「本体条目只在 Studio 编辑，插件产物在插件页面编辑」 | 流程图是 Studio 的「条目类型」：表格筛选、新建、删除、编辑器面板，图示插件决定它出不出现 | 两边都保住。流程图是图示包登记的插件产物 `diagram.flowchart`（5.9.5 把流程图列为专用组件），不算本体实体，不进知识包；Studio 和流程图弹窗经通用产物接口读写 |
| 3 | 节点状态有几种 | 6.6：8 种状态 + 需复核、已停用 2 个标记；6.9 又用了「可补做」 | core.js 状态表有 17 种：多了冲突、草稿、未开始、可补做、自主完成、失败、通过 | 详设。API 只用 8 种加 `flags`；`display_state` 给出设计稿的显示态。「可补做」是 `ready` 加 `backfill` 标记，「自主完成」是分段步骤的 `auto` |
| 4 | evidence 工具有几个 | 5.8.7：search、show、where-used、lineage、diff 共 5 个 | proj-settings 写「evidence.*（6）」，同页 MCP 表列了 5 个 | 详设，5 个 |
| 5 | 待处理有哪些类型 | 4.7 列了「报告里的待处理条目」，8.2 有未确认的变更集提示 | inbox 筛选只有 8 类，没有报告条目 | 详设。保留这些类型，计入「全部」，不单设筛选 |
| 6 | 自主构建预算有哪些维度 | 8.5：费用和时长上限 | 有时长、token、费用三项 | 设计稿。加上 token，与配额口径一致 |
| 7 | 提名回流由谁审核 | 5.5：管理员审核、脱敏后发布 | knowledge 页只有提交，没有管理员审核入口 | 详设。提供 `AD-08`～`AD-10`，页面空缺列在 8.2 |
| 8 | 资料用途有几种 | ADR 0017 与现有代码：goal、schema、term、evidence、auto | features 写 4 种，资料菜单实际 5 种（多「由助手判断」） | 5 种（菜单与代码一致） |

### 9.2 设计稿内部不一致

| # | 不一致 | 本契约按 |
|---|---|---|
| 1 | 登录锁定时长：login 写「连续失败 5 次后需要等 1 分钟」，ops-users 写「锁定 15 分钟」 | 由 auth-server 决定，接口返回 `retry_after_s`，两页都显示实际值 |
| 2 | L4 平台上限：proj-settings 写「4 小时、10M、¥300」，sys 写「4 小时、8,000,000、¥600」 | 单一来源 `SY-17`，proj-settings 经 `AB-01.platform_limits` 读同一份 |
| 3 | 场景模拟更新到 0.10.0：plugins 页直接提示成功，plugin-detail 弹「暂时不能更新」 | 两页都先调 `PL-03` 兼容检查，按结果显示 |
| 4 | 会话池：sys 写「来自环境变量，改了要重建容器」，ops-ctl 却能编辑并保存 | 详设 4.10.3（运维可调整并发上限）。运维面可改（`O-16`）；sys 页只读显示当前值和来源 |
| 5 | 提供方数量：features 写 8 个，插件目录 9 个（MySQL、PostgreSQL 分开），登记抽屉类型格另有 Oracle、达梦 | 按实现的提供方 ID 列出（`EV-10`）；`kind` 分组对应详设的 8 类 |
| 6 | 待处理和锁定条里的数字（如过期场景 2 / 3、通过 19 / 21）前后不一 | 示例数据问题，不影响契约 |

### 9.3 与代码或方案的出入

| # | 出入 | 本契约按 |
|---|---|---|
| 1 | 健康检查路径：设计稿 ops-ctl 写 `/livez`、`/readyz`，方案 E7 写 `/api/health/live`、`/ready` | 方案（8.1#2） |
| 2 | MCP 地址：设计稿写 `/api/onto-mcp/projects/retail-restock/sse`（没有工具集段），代码是 `/api/onto-mcp/{id}/{toolset}/sse` | 代码；`M-01` 返回实际地址 |
| 3 | 求解契约修订号：设计稿显示 r7、r8，代码的修订号是摘要串 | 两者都给：`revision`（摘要，做并发和回执绑定）、`revision_no`（显示用整数） |
| 4 | 发布版本号：设计稿和详设 8.2 都用主线版本（v0.13），代码是 `0.1.N` | `0.{n}.0`（8.1#54） |
| 5 | SSE：方案 D14 说 `flow.updated` 复用会话事件流 | 另设项目流（8.1#11） |
| 6 | 用户管理：方案 E8 说留在管理员，设计稿放在运维中心 | 接口在业务面，写操作只有 admin，ops 只读（8.1#27） |
