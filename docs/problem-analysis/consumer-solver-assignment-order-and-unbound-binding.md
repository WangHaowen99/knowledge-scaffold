# Consumer 求解链路两个缺陷问题记录

> 代码仓：当前仓库
> 分析基线：`9ca19f5e225c0d85dce3bd00b0237dc8d7305c5f`（`origin/develop`），在分支 `feat/onto-modeling-solver-adaptation` 上发现
> 记录日期：`2026-10-19`
> 发现场景：知识包建模平台（onto-modeling）求解器适配的真实端到端验证，链路为 建模平台 → modeling 求解验证接口 → Consumer 导入与求解 → OR-Tools `binary_ordered_route` 引擎 → 校验器。
> 分析范围：`consumer/backend` 的求解协调与路由、`solvers/ortools/engine`；验证只使用合成知识包与合成数据。两个问题都不在建模平台改动范围内，本分支未修改相关代码。

## 问题 1：Consumer 持久化请求时重排键序，OR-Tools 引擎按键序校验候选而失败

### 1.1 问题是否存在

**结论：** 确认存在，可稳定复现。**置信度：** 高。

**现象：** 当求解条目的 ID 字典序与业务顺序不一致时，例如有 10 个及以上条目，编号为 `B1`…`B10` 且没有补零，求解作业会以 `SOLVER_EXECUTION_FAILED` 失败。对外错误码为 `solver_unavailable`，建模平台的端到端验证因此判为未通过。同一知识包把编号补零成 `B01`…`B10`，或条目数不超过 9 个时，验证正常通过。

**适用条件与影响范围：** 所有走 Consumer 作业编排、并由 OR-Tools `binary_ordered_route` 引擎求解的请求都受影响，只要条目顺序与条目 ID 的字典序不同就会触发。最常见的形态是 10 个及以上未补零的数字编号；即使只有两个条目，只要 ID 逆序（如 `Z`、`A`）也会触发。

**关键证据：**

| 证据 | 位置 | 观察结果 | 证明了什么 |
|---|---|---|---|
| 请求持久化 | `consumer/backend/solver_coordination/job_store.py:77`，`_json_text` | 用 `canonical_json_bytes` 序列化 `request_payload` 后写入 `solve_attempts.request_json` | 落库文本按 canonical JSON 规则排序 |
| canonical 规则 | `consumer/backend/solver_coordination/canonical.py:47` | `json.dumps(..., sort_keys=True)` | 所有对象的键按字典序重排，包括候选的 `assignment` |
| 提交前还原 | `consumer/backend/solver_coordination/job_orchestrator.py:510`，`_restore_problem(attempt["request_payload"])` | 从落库 JSON 重新加载 ProblemIR 再提交给求解后端 | 引擎实际收到的是键序已重排的 `assignment` |
| 引擎校验 | `solvers/ortools/engine/refinery_route.py:543`，`_validate_topology`，第 562～563 行 | `tuple(raw_assignment) != item_ids` 时抛出 `candidate assignments are not canonical` | 引擎把 JSON 对象的键序当作语义，与条目顺序逐位比较 |
| 端到端复现 | 合成知识包，64 个条目，编号 `B1`…`B64` | 作业失败，`SOLVER_EXECUTION_FAILED`；取出落库的 `request_json` 在引擎中单独回放，得到同一异常 | 失败发生在引擎拓扑校验，不是建模平台或校验器的问题 |
| 对照 | 同一知识包，编号改为 `B01`…`B64` | 求解与校验全部通过 | 只差在 ID 字典序是否等于业务顺序 |

### 1.2 根因

RFC 8259 规定 JSON 对象是无序的，Consumer 为计算摘要和持久化统一使用排序键的 canonical JSON，这一做法本身合法。但 OR-Tools 引擎的 `_validate_topology` 依赖 `assignment` 对象的键序与条目顺序一致。两边对「对象是否有序」的假设相互冲突。作业编排还原的是排过序的请求，引擎就把合法的候选判成了「非 canonical」。

### 1.3 建议修复

**推荐：** 在引擎侧去掉对键序的依赖，改为按条目 ID 取值比较：

- 先比较集合：`set(raw_assignment) == set(item_ids)`；
- 再比较取值：`tuple(raw_assignment[item_id] for item_id in item_ids) == assignment`。

这样与 JSON 语义一致，改动最小，也不影响 Consumer 现有的摘要与落库规则。

**配套回归测试：**

- 在 `solvers/ortools` 的测试中构造 `B1`…`B10` 这类 10 个未补零编号的条目，把候选的 `assignment` 按键排序后再求解，断言求解成功；
- 在 Consumer 侧增加一条经 `job_store` 落库、`_restore_problem` 还原的请求往返用例。

**备选方案：**

- Consumer 落库时保留原始键序，即不排序：这会让摘要与存储规则分叉，且所有经 canonical 化的路径都要排查，不推荐。
- ProblemIR 把 `assignment` 改成有序数组：协议改动面大，需要引擎、Consumer 和契约同步升级，可作为长期方案评估。

### 1.4 当前规避方式

端到端验证使用补零编号（`B01`…`B64`），保证 ID 字典序与业务顺序一致。这只是测试侧规避，真实知识包的条目 ID 不应受此约束。

## 问题 2：未配置求解器 Binding 时 Consumer 返回 HTTP 500

### 2.1 问题是否存在

**结论：** 确认存在。**置信度：** 高。

**现象：** 评估工作区（workspace 与用户组合）没有可用的求解器 Binding 时，`POST /api/solver/runs` 返回 HTTP 500，响应体中没有稳定的错误码。建模平台只能把该次验证记为 `http_500`，用户看不出原因是「未配置求解器绑定」。

**适用条件与影响范围：** 在动态 Binding 选择模式下，当前 develop 上不存在 Binding 管理接口，新的评估工作区默认没有 Binding。只要发起求解就必然触发。同一函数的其他 `BindingContractError` 分支也没有映射，包括已停用、不兼容、选择不唯一。

**关键证据：**

| 证据 | 位置 | 观察结果 |
|---|---|---|
| 抛出点 | `consumer/backend/solver_coordination/bindings.py:434` | 没有候选时抛出 `BindingContractError("no compatible solver binding is configured")`；第 426～436 行的其他分支同类 |
| 调用链 | `consumer/backend/solver_coordination/service.py:422`，`start_or_resume` | 动态选择模式下，在冻结事实快照之前调用 `binding_resolver.resolve` |
| 路由 | `consumer/backend/api/routes/solver_coordination.py:572`，`_create_solver_run_through_coordination_service` | 第 664 行直接调用 `service.start_or_resume`，周围没有 `try/except`；外层 `_run_long_solver_task`（第 99 行）只处理 `TimeoutError` |
| 复现 | 合成知识包，在未注册 Binding 的评估工作区发起验证 | Consumer 日志显示上述调用链与未处理的 `BindingContractError`，访问日志为 `POST /api/solver/runs` 500 |

**对照：** 同一文件的重试路由 `_retry_solver_history_response`（第 714 行起）已对 `ValueError`、`JobStoreError` 和兜底异常做了映射。只有新建求解运行这条兼容路由遗漏了。

### 2.2 根因

`_create_solver_run_through_coordination_service` 缺少对协调服务异常的映射。`BindingContractError` 虽然继承自 `ValueError`，但在该路由中没有任何处理，最终由框架转成 500。

### 2.3 建议修复

**推荐：**

- 在 `_create_solver_run_through_coordination_service` 中捕获 `BindingContractError`，返回稳定、不可重试的错误码，如新增 `solver_binding_unavailable`，HTTP 409。错误码在 `consumer/backend/solver_coordination/errors.py` 登记，响应体不回显异常文本；
- 同时参照重试路由，补齐 `JobStoreError`、`JobOrchestrationError` 等的映射，避免同类 500。

建模平台据此提示「Consumer 尚未为该评估工作区配置求解器绑定」，不需要改动建模平台的协议。

**配套测试：** 在 `consumer/backend/tests/test_solver_coordination_route.py` 中，用一个解析失败的 binding resolver 断言返回 409 与该错误码。

**备选方案：** 映射为现有的 `solver_unavailable`（503，可重试）。这会诱导调用方无意义地重试，而且与真正的后端不可用无法区分，不推荐。

### 2.4 当前规避方式

端到端验证前，通过 `SolverJobStore.register_connector_binding_revision` 为评估工作区和验证用户预注册 Binding。develop 上没有 Binding 管理接口，只能这样做。这是部署侧的临时手段，正式环境应由 Binding 管理能力完成配置。
