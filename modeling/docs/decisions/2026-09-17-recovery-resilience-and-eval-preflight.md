# 建模服务重启恢复韧性与评测环境门

- 日期：2026-09-17
- 状态：已实现（agent 侧恢复对账、评测环境门与执行期熔断）

## Context

对 cs 生产库（`runtime.sqlite` / `modeling.db`）做了一次全量错误盘点，发现三类系统性问题，均不是单点 bug，而是设计层缺口：

1. **自动恢复整体失效。** 8/30 一次服务重启把一批运行中的项目悬停后，此后每次重启或每日巡检发起的自动恢复，都在一秒内整批撞上「建模核心停止栅栏暂时不可用」（`CORE_CANCELLATION_UNAVAILABLE` / 503），被一次性标记 `resume_failed` 后不再重试。累计 408 次恢复失败、当前 26 个项目卡死最久 18 天。根因有三：
   - **启动竞态**：agent 容器常早于建模核心就绪，恢复在核心可服务前就开跑；
   - **瞬时性判据过窄**：只认单一错误码 `CORE_UNAVAILABLE`，而发布分支新增的停止栅栏 503（`CORE_CANCELLATION_UNAVAILABLE`）不在其列，退避重试对它直接失明；
   - **`resume_failed` 是进程内终态**：首轮失败后没有任何补偿，只能等下一次人工重启，而重启又重复同样的竞态。
2. **评测环境失败污染质量口径。** 一轮评测中 151 条用例全部因「评测访问凭据已过期」判 error、104 条因 `ECONNREFUSED` 判 error、13 轮因缺 `MODELING_SERVICE_TOKEN` 直接失败。这些和知识包质量无关的环境失败，被逐条烧成 error，既浪费算力又把评测通过率口径污染（error 与 fail 几乎等量）。
3. **失败缺可观测终因。** `resume_failed` 只写一句「自动恢复失败」，不带错误码；运维只能翻进程日志才知道为什么。

## Decision

### A. agent 侧恢复对账（`service.py` / `core_client.py`）

- **就绪门**：`startup` 在发起自动恢复前，通过新增的 `HttpCoreGateway.health()`（探 core `/health`，5s 超时）等待核心就绪，上限 `CORE_READY_TIMEOUT_SECONDS=60`；等不到也继续，交给每次恢复自身的退避与对账，不阻塞启动。
- **瞬时性按错误族判断**：`_is_transient_core_error` 用错误码集合（`CORE_UNAVAILABLE`、`CORE_CANCELLATION_UNAVAILABLE`）**并集** HTTP 状态族（502/503/504），而不是单一码。新增的核心 5xx 码天然被覆盖，不再需要逐个补码。
- **`resume_failed` 不再是进程内终态**：瞬时原因失败后，`_schedule_recovery_reconcile` 在进程内按 `RECOVERY_RECONCILE_DELAYS=(60s,300s,900s)` 排 3 次对账重试。对账前重新读状态，仅当仍停在 `resume_failed`、run 未变、且无在跑任务时才重试，避免与人工恢复或新 run 抢占。
- **失败留因**：`_mark_resume_failed` 增加 `detail` 参数，把 `[错误码] 错误信息` 写进状态 `error` 字段并随可见事件下发，运维不必翻日志即可定位。
- 关停时取消所有未完成的对账任务（`shutdown` 遍历 `_recovery_tasks`）。

### B. 评测环境门 + 执行期熔断（`evaluation/engine.ts`）

分两层，各司其职，刻意**不**在开轮热路径放网络探测：

- **开轮前同步门 `preflightEvaluationEnvironment`**：只做零 I/O 的确定性检查——影子工作空间路径下服务令牌是否配齐（≥32 字符）。不就绪就在**创建轮次之前**抛 `EVALUATION_ENVIRONMENT_UNAVAILABLE`，不留下 failed 轮次污染统计。为什么不在这里探连通性：网络探测会阻塞「创建即返回」契约（Agent 评测要求即时回执），且连接失败已由执行期熔断兜底。
- **执行期环境熔断**：用例执行中，按 `isEnvironmentFailureMessage`（凭据/令牌/连接类文案）判定环境失败；连续达到 `ENVIRONMENT_FAILURE_BREAKER_THRESHOLD=3` 条即抛错终止整轮，而不是把剩余用例逐条烧成同一个 error。非环境失败会重置连续计数，不误伤知识包质量问题。
- 鉴权失败终止整轮的判据加了按异常名 `EvaluationAuthenticationError` 的兜底，避免模块重复加载导致 `instanceof` 失真。

## Alternatives considered

- **恢复：无限重试而非有限对账**——放弃。无限重试会在核心长期不可用时无意义地空转、刷爆事件流；有限 3 次（覆盖约 21 分钟）足以吸收启动竞态，仍失败则如实停在 `resume_failed` 等人工介入。
- **评测：开轮前做 Consumer 连通性 ping**——曾实现，因两个真实回归放弃：① 阻塞 Agent 评测的「创建即返回」契约（Consumer 慢响应会拖住开轮）；② 为此把 `startEvaluation` async 化后打乱了微任务时序，使后台执行抢在「开轮即取消」之前运行。连通性检查的价值与执行期熔断重复，故只保留同步令牌门 + 执行期熔断，`startEvaluation` 维持同步返回。

## Consequences

- 重启后卡死的项目会被进程内对账自动救回（瞬时原因），不再依赖下一次人工重启；非瞬时失败如实留因停住。
- 评测不再因环境问题烧掉整轮用例：令牌缺失在开轮前拒绝，连接/凭据失败在连续 3 条后熔断。
- `resume_failed` 状态自带错误码，运维可直接定位。
- 存量 26 个 `resume_failed` 项目需一次人工恢复触发（本次改动只保证此后不再复发）。
- **未覆盖**：核心 `/health` 的具体契约依赖部署；就绪门只在网关实现 `health()` 时生效（`FakeGateway` 无此方法，退化为不等待，测试零影响）。
