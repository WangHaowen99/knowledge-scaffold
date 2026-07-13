# Meta-Wise Consumer 合并人工验收方案

> 编制日期：2026-07-13
> 适用分支：`codex/consumer-main-full-migration` 最终候选版本
> 关联方案：`consumer/docs/migration/full-migration-plan.md`
> 验收目标：确认本地最新 Consumer 能力、远端新增能力和远端前端设计体系在合并后同时可用，且没有权限、数据、性能和视觉回退。

## 1. 验收结论规则

只有同时满足以下条件，人工验收才能判定为“通过”：

1. 冻结唯一候选 commit SHA；验收期间不再混入代码变更。
2. auth-server、consumer backend、MCP、frontend 四个服务健康，Neo4j、HugeGraph、DataAgent、MindsDB、LLM 等本轮要求的真实外部服务可用。
3. 本文 P0 用例、权限用例、数据迁移和回滚用例 100% 通过。
4. P0、P1 缺陷为 0；P2 缺陷必须有明确影响面、规避方式、修复版本和产品负责人书面接受。
5. 性能硬门槛全部通过；外部 LLM 或数据服务耗时必须拆分为“平台开销”和“外部服务耗时”后判断。
6. 1440×900、1920×1080、1366×768 三种桌面视口完成视觉验收；浏览器控制台无持续报错，关键请求无意外 4xx/5xx。
7. 升级、备份和回滚演练完成，账号、workspace、知识包、session/run、MCP token 元数据和签名密钥均可恢复。

缺陷级别：

| 级别 | 定义 | 示例 |
|---|---|---|
| P0 | 阻断发布、数据丢失或严重安全问题 | 跨 workspace 访问、无法登录、迁移覆盖用户数据、无法回滚 |
| P1 | 核心链路不可用且无合理规避 | 对话不能完成、断线无法恢复、知识包无法导入、MCP token 吊销不生效 |
| P2 | 局部功能异常，有明确规避方式 | 某个非关键配置不能保存、局部布局错位 |
| P3 | 轻微文案或视觉瑕疵 | 间距、对齐、非关键提示文案问题 |

## 2. 验收开始前门禁

当前合并仍在执行。满足以下条件后再开始正式人工验收：

- [ ] 迁移分支工作树干净，记录 `git rev-parse HEAD`。
- [ ] 候选分支仍以冻结的本地 `develop@1d07c1ee4905b89843b5158e5bbedd91fa3b9537` 为祖先；验收前后均不得 fetch/pull 或吸收 `origin/develop`。
- [ ] `full-migration-plan.md` 的 Phase 0—8 已完成，C01—C30 均有实现归属。
- [ ] auth-server 全量测试、consumer backend 全量测试、frontend build/lint 通过。
- [ ] 生产代码不存在旧 `agentic` import、local auth fallback 和默认 MCP secret。
- [ ] 验收环境使用独立数据副本，禁止直接拿唯一生产数据做破坏性用例。
- [ ] 浏览器禁用缓存完成一次冷启动测试，再开启正常缓存完成日常操作测试。
- [ ] 服务端、auth-server、MCP 和浏览器时间同步，便于对齐 trace 与日志。

建议在验收记录首页填写：

| 项目 | 记录值 |
|---|---|
| 候选 SHA | |
| 构建/镜像标签 | |
| 验收环境地址 | |
| 浏览器及版本 | |
| 后端 Python / 前端 Node 版本 | |
| 外部服务版本与地址（脱敏） | |
| 验收人 / 日期 | |

## 3. 测试账号、空间和数据准备

### 3.1 账号与权限

准备以下互不复用的账号：

| 账号 | 全局角色 | workspace 角色 | 用途 |
|---|---|---|---|
| `admin_a` | admin | 任意空间可管理 | L1 配置、用户管理、跨空间管理 |
| `owner_a` | user | `private_a` owner | 空间管理、成员管理、资源创建 |
| `member_a` | user | `private_a` member | 成员只读/可运行能力 |
| `owner_b` | user | `private_b` owner | 跨空间隔离验证 |
| `outsider` | user | 非成员 | 403、自愈和越权验证 |
| `force_pwd` | user | 无 | 首次登录强制改密 |

同时准备：一个公共 workspace、两个私有 workspace、一个无 owner 的存量 workspace 副本、一个 owner 已失效的异常副本。

### 3.2 知识包与数据连接

- 当前格式知识包：至少 2 个版本，包含 Markdown、YAML、JSON、Python、CSV、SQL、assets 和可执行脚本。
- v0.1 格式知识包：可导入、浏览并执行一条典型问题。
- 异常包：路径穿越、符号链接、二进制文件、单文件超过 2 MB、缺失 manifest/SKILL 元数据。
- 数据连接：`direct_provider` 和 `connection_ref` 两种模式都要准备。
- Provider：DataAgent、MindsDB、NL2SQL HTTP、NL2SQL Template、Neo4j、HugeGraph。
- Graph：两个 workspace 共用同一个图服务实例，但数据 scope 不同；准备能覆盖 schema、filter、aggregate、pattern、hop、shortest path 的样例数据。
- LLM：主模型和 task 模型各一套；准备一个不可用模型地址用于错误反馈测试。

### 3.3 观测工具

人工验收时同时打开：

1. 浏览器 Network：保留日志，显示 Fetch/XHR 和 EventStream。
2. 浏览器 Console：观察 React、请求、SSE 解析和资源加载错误。
3. backend 日志：按 `user/workspace/session_id/run_id/trace_id` 检索。
4. auth-server 日志：观察 login、refresh、revoke、JWKS，确认不输出密码和完整 token。
5. MCP 日志：观察 token subject、workspace scope、工具名、耗时和错误码。
6. Docker/进程指标：CPU、RSS、容器重启次数、健康状态。
7. 运行时目录只读检查：确认用户数据隔离、文件权限、MCP 明文 token 清理结果。

## 4. 分轮次执行

### 4.1 第一轮：发布阻断冒烟，约 30—45 分钟

以下任一项失败立即停止后续验收并判定为 P0/P1：

- [ ] 四个核心服务启动并通过 health check，前端无白屏。
- [ ] 管理员登录、强制改密、普通用户登录成功。
- [ ] 创建私有 workspace，owner 邀请 member；outsider 看不到该空间。
- [ ] 导入当前格式知识包，激活版本并能浏览文件。
- [ ] 创建新会话，quick 模式完成一次含工具调用的问答。
- [ ] 刷新页面后恢复同一会话，消息和 Trace 不重复、不丢失。
- [ ] 运行中点击停止，状态收敛为 cancelled，后台不再继续调用工具。
- [ ] 创建 MCP token，调用一次 search，吊销后立即拒绝。
- [ ] backend、auth-server、MCP 日志无 traceback、secret 泄露和跨空间数据。

### 4.2 第二轮：核心功能与 UI，约 3—5 小时

执行第 5—11 节所有 P0/P1 用例，并保存截图、网络记录和对应 trace/run id。

### 4.3 第三轮：真实外部服务、性能与恢复，约 0.5—1 天

执行 Graph、DataAgent、MindsDB、MCP chat、20 并发 run、500 短 run、数据迁移、备份和回滚。性能测试使用稳定网络，至少预热 3 次、正式测量 20 次；并发测试按本文单独要求执行。

## 5. 功能人工验收用例

### 5.1 认证、Token 与用户管理

| ID | 操作 | 重点观察 | 通过标准 |
|---|---|---|---|
| AUTH-01 | 使用正确/错误密码登录 | `/auth/login` 状态码、提示文案、token 是否出现在 URL/日志 | 正确密码进入系统；错误密码不给出账号枚举信息；日志无密码/token |
| AUTH-02 | `force_pwd` 首次登录 | 可访问页面和接口范围 | 只能访问个人信息、改密和必要静态资源；改密后要求重新登录 |
| AUTH-03 | 等待 access token 过期后继续操作 | Network 中 refresh 次数、原请求重试次数 | 并发 401 只触发 1 次 refresh；每个原请求最多重试 1 次；页面状态不丢 |
| AUTH-04 | 登出后使用旧 refresh token | `/auth/logout`、`/revoke`、本地存储 | 旧 refresh 立即失效；本地 token 清理；后退不能恢复敏感页面 |
| AUTH-05 | 改密、管理员重置密码、角色降级 | access/refresh 有效期边界 | refresh 立即失效；旧 access 按约定最多存活一个短 TTL；新角色立即影响后续 token |
| AUTH-06 | 开放/关闭注册；合法和非法密码注册 | 注册入口、规则提示、用户数限制 | 开关正确；规则提示完整；无邮箱验证码残留流程；重复用户不创建 |
| AUTH-07 | 管理员创建、编辑、删除用户 | 操作者限制、workspace 关系 | 不能删除/重置自己；owner 未转移时删除返回明确 409；member 删除后权限被清理 |
| AUTH-08 | auth-server 重启和密钥轮换 | JWKS `kid` 刷新、已有 token | 私钥未变时 token 继续可用；未知 `kid` 只强制刷新一次；错误 issuer/audience/alg/typ 全拒绝 |

### 5.2 Workspace 与横向权限

| ID | 操作 | 重点观察 | 通过标准 |
|---|---|---|---|
| WS-01 | owner 创建、改名、设默认、删除 workspace | 列表角色、默认空间、确认弹窗 | owner/全局 admin 可操作；member/outsider 按设计只读或拒绝 |
| WS-02 | 添加/移除 member | 当前页面和另一浏览器的可见性 | 成员加入后可见；移除后下一次请求立即 403，旧页面自动退出当前空间 |
| WS-03 | outsider 枚举 workspace/header/URL | Network、backend ACL 日志 | 列表不泄露私有空间；伪造 `X-Workspace-Id` 或 URL 均 403/404 |
| WS-04 | 使用他人的 session id、run id | status、events、cancel、resume | 无法读取、续跑、取消或获取 metadata；ACL 在打开 store 前执行 |
| WS-05 | 通过 A2A、ACP、MCP 绕过 | 不同入口的错误契约 | 所有入口执行相同 workspace ACL；错误不泄露资源是否存在 |
| WS-06 | 存量无 owner workspace | UI 文案、审计报告 | 保持既定公共语义；不自动归属任意用户；管理员可明确处理 |
| WS-07 | 删除 member 后同名重新注册 | workspace members、实际权限 | 新账号不继承旧账号 membership；reconciliation/audit 可发现异常 |

### 5.3 新手引导与全局导航

- [ ] 新登录自动出现引导；跳过/完成后本次会话不重复弹出，帮助入口可重新打开。
- [ ] admin 引导覆盖模型、数据连接、知识包导入、知识绑定、开始对话。
- [ ] 普通用户引导不引导其进入无权修改的配置；只读状态有清晰说明。
- [ ] “新建对话 / 知识包工作台 / 配置中心”三大入口稳定，浏览器前进/后退不丢 workspace 和选中页。
- [ ] 切换 workspace 后，知识包、会话、配置、Trace 和权限同时刷新，页面不短暂显示上一个空间数据。

### 5.4 对话、Jiuwen、会话恢复与人工交互

| ID | 操作 | 重点观察 | 通过标准 |
|---|---|---|---|
| CHAT-01 | quick 模式普通问答 | 首个事件、token 流、最终消息 | Jiuwen 完成回答；没有 native loop/fallback 痕迹；结束状态唯一 |
| CHAT-02 | thinking 模式 | reasoning 展示、性能字段 | reasoning 与正文不混淆；`had_reasoning` 等指标正确；刷新后仍一致 |
| CHAT-03 | multi_agent 分解 2—4 个任务 | 子任务层级、并发、汇总 | 子 Agent 继承 workspace/模型/本体；结果汇总一次；Trace lineage 正确 |
| CHAT-04 | 触发串行和可并行工具 | 工具开始/结束次序、terminal batch | parallel-safe 工具可并行；barrier/terminal 语义正确；无重复执行 |
| CHAT-05 | 触发 input/select/approval 三种交互 | awaiting 状态、表单、恢复 | 页面显示正确控件；非法响应被拒绝；合法响应只消费一次并继续同一 run |
| CHAT-06 | 运行中切到知识包/配置页再返回 | 后台状态、消息增量 | 运行不中断；返回后不重复 token/工具卡片；滚动位置合理 |
| CHAT-07 | 运行中刷新浏览器或临时断网 | Last-Event-ID、events replay | 3 秒内开始恢复；已接收事件不重复；遗漏事件补齐；同一父子 Trace 不换 key |
| CHAT-08 | 队列已满时提交任务 | queued 提示、run_id、取消 | 2 秒内出现明确排队状态；排队任务可取消；入场后自动继续 |
| CHAT-09 | LLM 流式输出和工具执行中分别取消 | cancel 请求、后台调用、最终状态 | 2 秒内 UI 反馈；后台尽快停止；最终只出现 cancelled，不再追加完成消息 |
| CHAT-10 | pin 会话、自动标题、删除会话 | 历史列表顺序、刷新结果 | pin 与普通历史分组稳定；刷新后保持；删除后不可通过旧 URL 恢复 |
| CHAT-11 | 会话中尝试切换模型/本体/模式 | 锁定提示和新会话行为 | 已开始会话保持锁定；新选择只对新会话生效；不污染旧 run |
| CHAT-12 | 复制、反馈、错误重试 | 剪贴板、反馈持久化、turn 查询 | 操作有可见反馈；刷新后反馈仍在；重试不复制历史消息 |
| CHAT-13 | Jiuwen warm-up 失败后首个真实请求 | 启动日志、工具注册、首问 | warm-up 仅告警；不调用 LLM/业务数据；真实请求独立且可成功 |

### 5.5 Trace 与运行信息

- [ ] Trace 树顺序为“轮次 → LLM/思考 → 工具 → Graph 内部工具/子 Agent”，父子关系可展开。
- [ ] 同一 iteration 的多次 LLM 调用和多个 tool batch 不互相覆盖。
- [ ] 实时流、历史会话和断线重放三种视图的节点数量、状态、父节点一致。
- [ ] running、finished、error、cancelled 状态颜色和文案一致；错误节点能看到脱敏后的原因。
- [ ] Trace 侧栏可展开/收起、拖动宽度；宽度刷新后保持；最小宽度下正文仍可阅读。
- [ ] 外部合法 `traceparent` 能贯穿 run、LLM、工具、Graph、MCP chat；非法或全零值被拒绝/忽略且不破坏请求。
- [ ] 显示的 run id、trace id 能与后端日志对应；界面不展示 token、密码、数据库凭据。

### 5.6 知识包工作台

| ID | 操作 | 重点观察 | 通过标准 |
|---|---|---|---|
| KB-01 | 导入当前格式包 | Import Preview、免责声明、进度、错误 | 预览信息正确；成功后目录树刷新；错误包不产生半成品 |
| KB-02 | 导入 v0.1 包并执行典型问题 | 内部转换、运行时行为 | 能浏览、激活、运行；UI 不暴露两套运行模型 |
| KB-03 | 导入同版本/覆盖活动版本 | 409 确认和数据结果 | 必须二次确认；取消不改数据；确认后版本和 active 状态一致 |
| KB-04 | 启用/停用、切换 active version | 顶部状态、对话可选项 | 状态即时更新；停用包不能用于新 run；历史会话仍可说明原版本 |
| KB-05 | 浏览文件和面包屑 | 目录过滤、首文件选择 | 点目录、dataset/origin_data、缓存目录不展示；选择稳定 |
| KB-06 | Markdown 渲染/源码切换及多格式预览 | 表格、代码块、中文、长行 | `.md/.yaml/.yml/.json/.py/.txt/.csv/.sql` 可读；无脚本执行和 XSS |
| KB-07 | 编辑文件、上传文件、管理 assets | 版本边界、脏状态提示 | 活动版本按设计可编辑；历史版本只读；失败不会覆盖原文件 |
| KB-08 | 下载、删除版本、删除知识包 | 下载内容、active 约束、确认 | 下载可重新导入；不能留下悬空 active version/数据绑定 |
| KB-09 | 路径穿越、符号链接、二进制、大文件 | HTTP 状态和错误文案 | 全部拒绝；不读取工作区外文件；错误不泄露真实服务器路径 |
| KB-10 | 本体结构图和全屏 | 节点、边、缩放、空状态 | 图与当前 active version 一致；全屏和退出正常；大图可操作 |

### 5.7 配置中心 L1/L2/L3

逐项验证可见性、编辑权限、保存、刷新回显、错误回滚和 secret 脱敏。

| 层级 | 必测页面 | 关键通过标准 |
|---|---|---|
| L1 全局 | 模型池、main/task 默认模型、Web Search、平台接入、全局观测、Agent 参数、工作空间管理、用户管理 | 仅 admin 可改；普通用户只看到允许内容；API key 回显脱敏；测试连接给出明确耗时和错误 |
| L2 工作空间 | 空间信息、owner/member、模型选用、数据连接、知识绑定、workspace observability、MCP 接入 | owner/admin 可改，member 按设计只读；切空间后立即换数据；所有保存带正确 workspace id |
| L3 个人 | 默认空间、回复语言、主题、长期记忆、账号安全 | 仅影响当前账号；换号后不串数据；改密后退出；个人记忆 CRUD 可回显 |

补充检查：

- [ ] main/task 模型继承关系清晰，删除被引用 profile 时有阻断或迁移提示。
- [ ] role-bound/workspace 私有 profile 只对授权角色和空间生效。
- [ ] 无效模型地址、错误 API key、超时分别显示可区分的错误。
- [ ] 配置保存失败时恢复旧值，不能显示“已保存”假成功。
- [ ] 前端隐藏按钮不能替代后端权限；直接调用 API 仍会被拒绝。

### 5.8 数据连接、Graph 与业务查询

| ID | 操作 | 重点观察 | 通过标准 |
|---|---|---|---|
| DATA-01 | 新建/测试/修改/删除共享连接 | secret 回显、默认连接、引用关系 | secret 只写不回显；被知识包引用的连接不能静默删除 |
| DATA-02 | 配置 direct provider | `ontology-data-sources.json`、测试结果 | 保存、测试、解析均走当前 workspace/ontology；secret 加密 |
| DATA-03 | 配置 connection reference 并绑定知识包 | 连接和 binding 的唯一真相 | 运行时解析到正确共享连接；切换 binding 后新 run 生效 |
| DATA-04 | 迁移旧 `ontology-bindings.json` | 备份、重复执行 | `--dry-run` 无写入；正式执行幂等；不产生悬空引用 |
| DATA-05 | DataAgent 500/502/503/504 后恢复 | retry/backoff、调用次数、日志 | 按配置重试；成功结果只返回一次；API key 不进日志 |
| DATA-06 | DataAgent artifact/自然语言/SQL rows | 结果归一化 | 三种结果都能被 Agent 正确消费；失败状态不伪装成功 |
| DATA-07 | MindsDB、NL2SQL HTTP/Template 各执行一次 | 查询、行数、错误契约 | 返回结构一致；超时/无效 JSON/大结果有明确限制和错误 |
| GRAPH-01 | Neo4j/HugeGraph 执行同一工具矩阵 | 14 工具名、schema、结果结构 | 两个 backend 能力一致；结果经过 sanitize；内部 scope 字段不外泄 |
| GRAPH-02 | filter/aggregate/pattern/hop/shortest path | 查询正确性、截断、排序 | 与预置答案一致；行数上限和通配符限制有效 |
| GRAPH-03 | 两空间共用同一图实例 | workspace+ontology scope | A 无法查询 B 的节点、边、schema 和数量；伪造 scope 参数无效 |
| GRAPH-04 | GraphQueryAgent 批量子查询 | 去重、超时、重试、Trace | 结果去重且有截断提示；内部工具挂在外层 business data span |

### 5.9 MCP

| ID | 操作 | 重点观察 | 通过标准 |
|---|---|---|---|
| MCP-01 | 打开 L2 MCP 接入页 | 7488 状态、service catalog、endpoint | 在线/离线准确；search/chat 两服务和四种 transport 地址正确 |
| MCP-02 | 创建不同 scope/过期时间的 token | 明文展示、复制、列表 | 完整 token 只在创建响应和一次性 UI 中出现；刷新后仅 masked 值 |
| MCP-03 | 用 search 调用 14 个图工具 | 工具契约、scope、last-used | 全部可调用；不经过 LLM；last-used 更新；结果不跨空间 |
| MCP-04 | 用 chat 发起普通/排队/awaiting-input run | run owner、队列、恢复信息 | 进入 canonical Jiuwen orchestrator；owner 为 token subject；返回 run/session id |
| MCP-05 | wrong scope、过期、吊销、移除 member | 每次调用的动态检查 | 均立即拒绝；无需等待 token 缓存过期；错误不泄露其他空间 |
| MCP-06 | MCP/backend 重启 | token metadata、明文扫描 | 合法 token 行为按设计保持；磁盘无 `token_value` 明文；服务无默认 secret |

### 5.10 记忆、Operation/Skill Evolution 与工具链

- [ ] 长期记忆创建、编辑、删除、注入对话；user A 看不到 user B 的记忆。
- [ ] operation evolution 生成 proposal、查看 diff、approve/reject、版本漂移阻断。
- [ ] skill evolution 启动任务、查看状态/diff、approve/reject/cancel/delete。
- [ ] 本体、技能、脚本导入后，`ontology_context`、`skill`、`execute_script` 可工作。
- [ ] 脚本参数、assets、read ledger 按当前知识包版本解析；非法路径和未登记脚本被拒绝。
- [ ] Web Search 和 onto-platform 在启用、禁用、错误凭据、超时下均有稳定错误契约。

### 5.11 A2A、ACP、SDK 与兼容调用方

该部分可用 Postman/curl/SDK 人工执行，不要求在 UI 中提供入口。

- [ ] A2A/ACP discovery 返回当前服务能力和版本。
- [ ] 创建、流式事件、状态查询、取消、续跑均进入同一 Jiuwen orchestrator。
- [ ] workspace header、用户身份、session/run owner 在协议层和普通 HTTP 路由一致。
- [ ] SDK 能携带 `X-Workspace-Id`，支持 session/run 续传和 SSE cursor。
- [ ] 旧调用方读取 `agent-stream.v2` 时，新增 `span_key/parent_key` 不破坏兼容性。
- [ ] business-frontend 依赖的公共 HTTP/SSE 契约完成一轮实际回归。

## 6. UI 与交互验收标准

### 6.1 视觉一致性

- [ ] 所有活动页面使用 `redesign` 的颜色、字体、圆角、边框、间距和状态色变量。
- [ ] 新增的本体图、数据源、记忆、进化、观测和 MCP 页面没有旧版 CSS 风格孤岛。
- [ ] 页面层级清晰：页标题、区块标题、正文、辅助信息、错误提示可快速区分。
- [ ] 表格、表单、弹窗、空状态、加载态、错误态、禁用态和成功态使用同一套组件语言。
- [ ] 危险操作有明确确认；成功/失败提示不遮挡主要内容，也不会永久残留。
- [ ] 中文、英文、长 ID、长知识包名、长模型名、长 URL 不溢出或遮挡操作按钮。

### 6.2 布局与响应

| 视口 | 验收重点 |
|---|---|
| 1920×1080 | 信息密度合理，主内容不过度拉伸，Trace/三栏工作台可充分利用空间 |
| 1440×900 | 作为主验收基准，Chat、知识包四页签、L1/L2/L3、登录/注册均保存截图 |
| 1366×768 | 顶部、侧栏、底部输入区均可见；无横向页面级滚动；弹窗可完整操作 |
| 1024×768 | 最小兼容检查；双列/多列合理降为单列，重要操作仍可触达 |

必须检查：侧栏折叠、Trace 拖动、知识包三栏滚动、长表格、弹窗高度、浏览器缩放 80%/125%、系统字体缩放、浅色/主题切换。

### 6.3 可用性与无障碍底线

- [ ] Tab 顺序符合视觉顺序；登录、主要导航、表单、弹窗可仅用键盘完成。
- [ ] 焦点态可见；弹窗打开后焦点进入弹窗，关闭后回到触发按钮。
- [ ] 输入框有 label；图标按钮有可理解的 title/aria-label。
- [ ] 状态信息不只依赖颜色；错误、成功、运行中均有文字或图标语义。
- [ ] 正文与背景、按钮与背景达到可读对比；禁用态仍能辨认。
- [ ] loading 有反馈，超过 1 秒的操作显示进度或明确状态，避免重复提交。

截图清单：

1. 登录、注册、强制改密。
2. Chat 空态、运行中、awaiting-input、完成、错误、Trace 展开。
3. 知识包文件与版本、数据源、本体图、记忆进化四个页签。
4. L1、L2、L3 每个子页；admin、owner、member 三种权限视图。
5. MCP token 一次性展示、masked 列表、吊销确认。
6. 1440×900 全量截图，另保存 1366×768 的关键布局截图。

## 7. 性能验收标准

以下标准用于局域网/同地域稳定环境。浏览器关闭性能限速，服务预热后测量；每项至少 20 次，报告中记录 P50、P95、最大值和失败率。

### 7.1 前端与普通 API

| 指标 | 硬门槛 |
|---|---:|
| 登录页冷加载 LCP | ≤ 2.5 秒 |
| 登录后主工作台首次可交互 | ≤ 3 秒 |
| 三大页面切换到可操作 | P95 ≤ 500 毫秒（数据已缓存）；首次取数 P95 ≤ 1.5 秒 |
| 普通 CRUD API（不含外部服务） | P95 ≤ 500 毫秒，最大 ≤ 1.5 秒 |
| auth login/refresh/revoke | P95 ≤ 800 毫秒，最大 ≤ 2 秒 |
| workspace/会话/知识包列表（各 100 条） | P95 ≤ 1 秒 |
| 目录树 1000 个节点首次渲染 | ≤ 2 秒，展开节点 P95 ≤ 200 毫秒 |
| 2 MB 文本文件预览 | ≤ 2 秒，滚动无明显连续掉帧 |

### 7.2 Agent、SSE 与恢复

| 指标 | 硬门槛 |
|---|---:|
| 提交 run 到收到 `turn.started`/queued | P95 ≤ 1 秒 |
| 平台附加首 token 开销 | 相对同模型直连基线 P95 增量 ≤ 1 秒 |
| SSE 断线后的恢复启动 | ≤ 3 秒 |
| 事件重放正确性 | 0 重复、0 丢失、顺序一致 |
| 取消操作 UI 反馈 | ≤ 2 秒；最终状态收敛 ≤ 5 秒（外部不可取消调用需单独标记） |
| awaiting-input 提交到 run 恢复 | P95 ≤ 1 秒 |
| 20 个并发 run | admission 响应 P95 ≤ 2 秒；平台错误率 < 1%；无 event-loop/锁错误 |
| 500 个短 run 稳定性 | 0 资源泄露型失败；完成后 RSS 相对稳定基线增长 ≤ 20%；无持续增长趋势 |

LLM 生成耗时、token/s 和工具耗时单独报告，不用端到端总时长掩盖平台回归。thinking 与 multi-agent 使用同一固定输入，分别记录模型耗时、排队耗时、工具耗时和平台处理耗时。

### 7.3 数据、Graph 与 MCP

| 指标 | 硬门槛 |
|---|---:|
| 数据连接测试的本地校验 | P95 ≤ 500 毫秒 |
| Neo4j/HugeGraph 单个确定性工具 | P95 ≤ 3 秒，超时有结构化错误 |
| Graph 批量查询 | 在配置的超时内结束；截断和重试次数可见 |
| MCP `/health` | P95 ≤ 300 毫秒 |
| MCP search 平台附加开销 | 相对直接 Graph 工具调用 P95 增量 ≤ 500 毫秒 |
| MCP token 创建/列表/吊销 | P95 ≤ 800 毫秒 |
| DataAgent/MindsDB/NL2SQL | 记录外部耗时；平台序列化与归一化 P95 增量 ≤ 500 毫秒 |

### 7.4 前端稳定性

- 连续使用 30 分钟，完成 20 次页面切换、10 次会话切换、5 次 Trace 展开/拖动和 3 次知识包切换。
- 浏览器 Console 无持续错误，Network 无请求风暴。
- JS heap 相对稳定阶段增长不超过 20%，关闭页面/会话后能回落；同一轮次不能重复注册 SSE listener。
- 页面长任务不得持续阻塞主线程超过 200 毫秒；输入和滚动无明显卡顿。

## 8. 安全与数据观察清单

人工验收不做完整渗透测试，但以下项目全部是发布硬门槛：

- [ ] URL、浏览器日志、服务日志、错误响应中无密码、完整 access/refresh/MCP token、LLM/数据库 API key。
- [ ] MCP token 文件只保存 hash/jti、masked value 和元数据；历史 `token_value` 已清理。
- [ ] auth 用户文件和密钥权限为 0600；容器 volume 不向 frontend 暴露。
- [ ] 路径穿越、符号链接、非法 workspace/session/run id 均被拒绝。
- [ ] 所有资源入口执行 workspace ACL，不能依赖前端隐藏按钮。
- [ ] Graph scope、Data source、MCP scope 不能由调用方覆盖内部字段。
- [ ] refresh、revoke、改密、用户删除、成员移除都能在规定边界内使旧权限失效。
- [ ] 错误响应不泄露绝对路径、堆栈、内部服务 URL 和其他租户资源是否存在。

## 9. 升级、备份与回滚验收

使用脱敏的真实结构副本执行：

1. 旧系统保持运行，创建账号、workspace、知识包、会话、MCP token 元数据和 Graph scope 样例。
2. 执行迁移脚本 `--dry-run`，确认无写入并生成字段级报告。
3. 停止旧写入，同时备份 `.onto-runtime`、`.onto-auth/users.json`、`.onto-auth/keys/`、MCP store、binding 和 Graph scope。
4. 正式迁移；再次执行迁移，结果必须幂等。
5. 启动新版本并逐项验证：原密码登录、JWKS、workspace 可见性、知识包、session/run 历史、binding、MCP metadata。
6. 制造两份 users 文件冲突，确认迁移立即失败并保留两侧原文件，禁止静默选择权威源。
7. 回滚 backend、frontend、auth-server、MCP 为同一旧版本；恢复对应数据备份。
8. 再次登录和运行一条旧链路，确认旧系统可用且未写入新 auth 数据目录。

通过标准：升级和回滚各成功一次；数据条数、owner/member、active version、session/run、token metadata 对账一致；签名密钥未丢失；回滚时间和恢复点满足发布要求。

## 10. 验收证据与结果模板

每条失败或关键通过用例至少记录：

| 字段 | 内容 |
|---|---|
| 用例 ID | 如 `CHAT-07` |
| 候选 SHA / 镜像标签 | |
| 账号角色 / workspace | 不记录密码和 token |
| 前置数据 | 知识包、连接、会话、run |
| 操作步骤 | 可复现的编号步骤 |
| 预期 / 实际 | |
| HTTP 状态 / 错误码 | |
| session_id / run_id / trace_id | 可用于查日志 |
| 性能数据 | P50/P95/最大值/样本数 |
| 证据 | 截图、HAR、脱敏日志路径 |
| 结论 | 通过 / 失败 / 阻塞 |
| 缺陷级别与负责人 | |

最终签署表：

| 验收域 | 结论 | 阻断缺陷 | 证据位置 | 验收人 |
|---|---|---:|---|---|
| 部署与健康 | | | | |
| 认证与权限 | | | | |
| 对话与 Jiuwen | | | | |
| 会话恢复与 Trace | | | | |
| 知识包与配置中心 | | | | |
| Data/Graph/MCP | | | | |
| 记忆、进化与协议 | | | | |
| UI/可用性 | | | | |
| 性能与稳定性 | | | | |
| 升级与回滚 | | | | |

最终结论只能填写：

- **通过**：满足第 1 节全部规则，可进入 push/PR/合入审批。
- **有条件通过**：仅存在已书面接受的 P2/P3，附明确修复计划。
- **不通过**：存在 P0/P1、权限用例失败、性能硬门槛失败，或真实外部服务/回滚尚未验证。
