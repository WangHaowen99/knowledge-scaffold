# 0032 治理平面：配置注册表、可观测、响应后审计、存活与就绪、三种角色、运维监听

## Context

详设 4.10 与方案 E3～E8：环境变量散在 6 个文件、解析失败直接让启动报错；只有 4 个模块写日志，没有指标
和链路；审计在鉴权中间件里、处理请求之前写，没有状态码，本地模式不记；`/api/health` 恒为 ok；auth-server
只有 admin、user，B2 让 admin 过渡性地放行运维写接口；运维接口和业务接口挂在同一个监听上；没有维护
模式与排空。API 契约 1.1、1.3、1.12、1.13、3.24 SY-14、3.25 定了接口与行为。

## Decision

1. **配置注册表**（`governance/config_registry.py`）：每一项声明默认值、类型与范围、生效时机
   （immediate / next_message / new_session / restart）、是否密钥、编辑页面和来源优先级（默认
   env > data_dir > pack > default；模型接入、权限模式、会话池是设置页优先）。`config.py` 保留同名属性
   当门面；不合法的值告警后回落到下一个来源。插件经 `register(item, pack)` 登记自己的配置项（来源 pack）。
   SY-14 只读列出，密钥只给 `cred://<来源>/<键>` 引用。
2. **可观测**（`governance/context.py`、`telemetry.py`）：公共标签用 contextvars 承载（项目、会话、执行、
   变更集、包版本、节点、trace id）；根 logger 输出 JSON，带标签、脱敏令牌与口令，另写数据目录
   `logs/`（按日轮转 14 天）；进程内计数器与直方图，`/metrics` 输出 Prometheus 文本；每个请求一条链路，
   内核各处用 `span()` 记子段。O-02～O-07 从这里取。导出器（OTel 等）是可替换实现，本阶段只有内置的。
3. **审计**（`services/audit.py`、`governance/http.py`）：治理中间件在响应之后为每个写请求记一条
   AuditEntry（操作者与类型、状态码、trace id、来源 IP，鉴权关闭时操作者是 local）；专项事件（agent 提交、
   登录、指定所有者、运维操作、发布、熔断……）由各模块调 `record()`。按日轮转到 `audit/`，旧版单文件只读
   保留；不记正文。
4. **健康检查**（`governance/health.py`）：`/live` 不探测依赖，容器 HEALTHCHECK 改用它；`/ready` 探测
   模型网关、auth-server（JWKS）、沙箱网关、连接器网关（P3 前报未部署）、磁盘、git、装配目录（插件运行时
   `register_check` 接入），关键项失败或启动恢复没做完返回 503，匿名请求不给详情；O-13 与就绪共用一份探测。
5. **角色**（`governance/roles.py`）：auth-server 加 `ops`；onto 侧 user、admin、ops 三种，本地模式三者都有。
   运维写接口只给 ops（O-37 备份、O-53/O-54 回收站按契约 admin 也可），读接口 ops 与 admin；admin、ops
   都只看别人项目的元数据。B2 的「过渡期 admin 也能写运维面」收紧。
6. **运维监听**（`ops_app.py`、`governance/ops_listener.py`）：同进程第二个 uvicorn（默认 127.0.0.1:8101，
   会话池在内存里所以必须同进程），由 `run.py` 打开，在业务面的 lifespan 里起停，不装信号处理器；运维面同样
   校验 JWT；业务面上的 `/api/v1/ops/*`（含改写后的旧路径）默认 404，`ONTO_WEB_OPS_ON_MAIN=true` 放行；
   C-01 的 `ops_base_url` 取 `ONTO_WEB_OPS_BASE_URL`。
7. **维护、排空与熔断**（`governance/ops_state.py`）：维护模式状态存数据目录，四步（限制 → 排空 → 升级 →
   恢复），只读时写请求 503（运维面与登录除外），两种模式都拒绝新会话；排空让在线会话跑完当前这一轮后
   回收、超时强制断开并在历史标中断；会话池上限、空闲阈值、溢出策略可在线改；熔断按（包, 版本）记状态，
   插件运行时据此跳过节点。

## Alternatives considered

- **引入 OpenTelemetry SDK / prometheus_client**：依赖要进离线 wheels；P1 先用标准库实现内置出口，导出器作为
  可替换实现以后接。
- **运维面单独进程**：会话池、维护状态都在业务进程内存里，跨进程要另做 IPC；方案 E8 已定同进程。
- **审计仍在请求前写**：拿不到状态码，失败的请求和成功的分不开（契约 1.12）。

## Consequences

- 后端本身默认不在业务面处理运维接口；两份 compose 默认设 `ONTO_WEB_OPS_ON_MAIN=true`，没有反代的单端口部署
  才能用运维中心（运维接口仍要 JWT 与 ops 角色）。有反代的部署在 `.env` 关掉，把 `/api/v1/ops/` 转到 8101。
- 指标与链路在进程内，重启清零；O-02 卡片里合入、证据采集在 P2、P3 前恒为 0。
- 告警规则与导出器配置（O-08～O-12）、恢复演练、任务中心、数据保留没做（见 `_plan/gaps.md`）。
