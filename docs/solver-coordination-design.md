# 求解器协同工作台过渡参考

> 状态：过渡性兼容文档。自 2026-08-17 起，求解主路径以
> [Agent Tool 驱动的求解器协调平台技术设计规范](solver-coordination-agent-tool-design.md)
> 和 [Consumer Connector 与 OR-Tools Solver Service 边界校正设计](superpowers/specs/2026-08-19-consumer-connector-ortools-solver-service-design.md)
> 为准。本文保留旧候选集/批量校验工作台的谱系说明，不能作为新
> `solver_workflow` Operation 的接入依据。

当前实现边界：

- 生产求解通过按 Operation 生成的 ToolCard 或兼容的 `/api/solver/runs` 进入同一个 `SolverCoordinationService`；客户端不选择 Solver Binding 或 endpoint。
- 求解输入、事实快照、`ProblemIR`、SolverResult 和验证证据均按 Workspace/Owner 持久化；同一 `tool_call_id` 只恢复已有 Job 与冻结工件。
- `/api/solver/history` 和 `/api/solver/history/<job_id>/evidence` 是只读历史/证据入口。前端旧 Solver surface 不再提供手工求解表单。
- 在统一服务已配置时，旧 `candidate-sets` 入口会转入统一服务，旧独立 `validation-runs` 入口返回 `410 standalone_validation_retired`；未配置统一服务时仅保留它们供迁移兼容测试使用。

合成回归示例（在 `consumer/` 执行）：

```powershell
$env:PYTHONPATH='backend'
python -m pytest -q backend/tests/test_solver_coordination_service.py backend/tests/test_solver_runtime_end_to_end.py
```

## 1. 文档定位

本文档描述当前 `consumer/` 中已经落地的求解器协同能力。它是实现说明，不是下一阶段的愿景方案，所有接口、字段和模块名称均以当前代码为准。

本文覆盖四类内容：

- **设计解释**：为什么求解、校验、事实快照和证据必须分层并绑定 lineage。
- **接口参考**：前端使用的 `/api/solver/*` 契约，以及兼容保留的 agentic 路径。
- **代码适配指南**：知识包、验证 Operation、绑定脚本和前端工作台如何接入。
- **验证与运维**：测试矩阵、Docker 验收链路、错误分类和后续扩展边界。

相关设计输入：

- [联邦式求解器 Connector 架构设计](superpowers/specs/2026-08-18-federated-solver-connectors-design.md)
- [Consumer Connector 与 OR-Tools Solver Service 边界校正设计](superpowers/specs/2026-08-19-consumer-connector-ortools-solver-service-design.md)
- [Consumer 运行说明](../consumer/README.md)
- [后端模块说明](../consumer/backend/README-CN.md)

## 2. 目标、边界与非目标

### 2.1 当前目标

求解器协同要完成一条可以审计和重放的链路：

```text
结构化计划
  -> 确定性候选集
  -> 候选集制品和求解运行账本
  -> 固定知识包版本 + 固定事实快照
  -> 一次批量校验 Operation
  -> 内容寻址的验证证据
  -> 可发布候选与校验结果
```

关键约束是：校验只能消费求解时绑定的事实快照，不能在校验过程中重新访问实时业务数据。

### 2.2 当前非目标

- 不引入新的 OR-Tools、线性规划或其他求解引擎。
- 不在本实现中从实时 Provider 自动创建 `FactSnapshot`。
- 不把业务规则硬编码在 Flask 路由或 React 组件中。
- 不让客户端选择脚本路径、Operation 名称或活动版本。
- 不新增第二个前端入口，也不拆分出新的应用。

## 3. 总体架构

```text
┌─────────────────────────────────────────────────────────────────────┐
│ React AppShell                                                       │
│  solver surface                                                      │
│  plan input -> candidate table -> validation/evidence result         │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ authFetch + X-Workspace-Id
                               v
┌─────────────────────────────────────────────────────────────────────┐
│ Flask routes: /api/solver/*                                         │
│  /runs       -> candidate generation                                │
│  /validations-> idempotent batch validation                          │
│  /runs/<id>  -> ledger + immutable artifact restore                  │
└──────────────┬───────────────────────────┬──────────────────────────┘
               │                           │
               v                           v
┌────────────────────────────┐  ┌─────────────────────────────────────┐
│ SolverRunLedger             │  │ ValidationRunStore                  │
│ SQLite WAL, workspace/owner │  │ atomic JSON, idempotency pointer    │
│ + source_run_id lineage     │  │ workspace/owner scoped              │
└──────────────┬─────────────┘  └──────────────────┬──────────────────┘
               │                                   │
               v                                   v
┌────────────────────────────┐  ┌─────────────────────────────────────┐
│ CandidateSetStore           │  │ ValidationEvidenceStore             │
│ content-addressed artifact  │  │ fsync + atomic rename + ledger row  │
└──────────────┬─────────────┘  └──────────────────┬──────────────────┘
               │                                   │
               └────────────────┬──────────────────┘
                                v
┌─────────────────────────────────────────────────────────────────────┐
│ OperationValidationRunService                                      │
│  exact package revision -> capability -> operation -> bound script   │
│  frozen fact snapshot -> one strict JSON batch invocation             │
└──────────────────────────────┬──────────────────────────────────────┘
                               v
┌─────────────────────────────────────────────────────────────────────┐
│ execute_script / JiuwenBox sandbox                                  │
│  data_access=frozen_snapshot                                         │
│  no live provider environment; script path and digest rechecked      │
└─────────────────────────────────────────────────────────────────────┘
```

### 3.1 技术栈

| 层次 | 当前实现 | 责任 |
| --- | --- | --- |
| 前端 | React + Vite + `lucide-react` | 结构化输入、结果展示、工作区隔离、请求重试 |
| 后端 | Python + Flask | 认证、工作区解析、HTTP 契约、错误映射 |
| 领域契约 | Python `dataclass` + `Enum` | 计划、候选、校验结果、聚合状态 |
| 持久化 | SQLite WAL + 原子 JSON 文件 | 运行账本、候选制品、验证运行、证据 |
| 知识包 | YAML、Markdown frontmatter、脚本目录 | 声明能力、Operation 和绑定脚本 |
| 执行隔离 | 现有 `execute_script` + JiuwenBox | 约束路径、环境变量和脚本执行 |
| 验证 | pytest、Node test、ESLint、架构检查、Vite build | 单元、集成、构建和容器验收 |

## 4. 核心数据契约

### 4.1 计划输入

前端将表单归一化为后端 `create_candidate_set()` 接收的结构。当前问题族是 `binary_ordered_route.v1`，最多 64 个条目，`option_domain` 必须按顺序包含两个选项，`max_transitions` 必须为 `1`。

```json
{
  "knowledge_package_id": "knowledge_package_demo",
  "knowledge_package_revision": "1.0.0",
  "plan": {
    "option_domain": ["OPTION_0", "OPTION_1"],
    "max_transitions": 1,
    "items": [
      {
        "item_order": 1,
        "item_id": "item_demo_001",
        "quantity": "1.0",
        "duration": "2.0"
      }
    ]
  }
}
```

校验规则来自 `consumer/backend/solver_coordination/contracts.py` 和 `candidate_service.py`：

- `item_order` 从 `1` 开始连续递增，不能重复或跳号。
- `item_id` 在一个计划内唯一。
- `quantity`、`duration` 是大于 `0` 的十进制定数字符串。
- 候选必须覆盖全部计划条目，`transition_count` 必须与 assignments 实际变化次数一致。
- 所有未知字段都会被拒绝，避免客户端静默传入未定义语义。

### 4.2 候选集制品

`CandidateSet.create()` 为问题和每个候选生成版本化 SHA-256 摘要：

```python
problem_digest = sha256_digest({
    "problem_family": problem_family,
    "knowledge_package_id": knowledge_package_id,
    "knowledge_package_revision": knowledge_package_revision,
    "plan": plan.to_dict(),
})

candidate_digest = sha256_digest({
    "problem_digest": problem_digest,
    "assignment": candidate.assignment_payload(),
})
```

候选集制品至少包含：

| 字段 | 说明 |
| --- | --- |
| `source_run_id` | 求解运行 ID，后续事实快照和验证运行必须引用它 |
| `problem_family` | 当前为 `binary_ordered_route.v1` |
| `knowledge_package_id` / `knowledge_package_revision` | 知识包身份和精确版本 |
| `plan` | 归一化后的计划 |
| `candidates` | 按确定性顺序排列的候选数组 |
| `problem_digest` | 计划和包身份摘要 |
| `candidate_set_digest` | 候选容器摘要 |
| `candidate_set_ref` | 内容寻址的存储引用 |

### 4.3 事实快照制品

事实快照不是任意 JSON 文件，必须同时满足文件信封、账本行和内容摘要三方一致：

```json
{
  "schema_version": "solver_coordination.fact_snapshot.v1",
  "artifact_type": "fact_snapshot",
  "source_run_id": "solve_demo_001",
  "knowledge_package_id": "knowledge_package_demo",
  "knowledge_package_revision": "1.0.0",
  "rules_revision": "rules_demo_1",
  "fact_payload": {
    "synthetic_threshold": 7
  },
  "fact_snapshot_digest": "sha256:solver-coordination.v1:<64-hex-chars>"
}
```

`FactSnapshotArtifactResolver` 的检查顺序：

1. 按 `workspace_id + owner_id + source_run_id + artifact_type` 查账本。
2. 要求恰好一条 `fact_snapshot` 记录。
3. 要求 `artifact_path` 是工作区 runtime 下的相对路径，禁止绝对路径和 `..`。
4. 校验 JSON 字段白名单、schema、artifact type、source run 和知识包版本。
5. 用 `sha256_digest({"fact_payload": fact_payload})` 重算摘要。
6. 同时比较文件内摘要和 ledger `artifact_digest`。

文件存在但内容不一致属于 `verification_divergence`；文件暂时不存在属于可重试的运行时故障。

### 4.4 知识包能力声明

验证能力放在**精确版本目录**，不依赖可变的活动版本。解析器按以下顺序寻找版本本地能力文件：

```text
<package>/versions/<revision>/solver-capabilities.yaml
<package>/versions/<revision>/capabilities.yaml
<package>/versions/<revision>/manifest.yaml
```

能力条目示例：

```yaml
capabilities:
  solver_operations:
    - operation: solve_demo
      validation_operation: validate_demo
      problem_family: binary_ordered_route.v1
      validator_revision: ontology_operation_batch.v1
      rules_revision: rules_demo_1
```

同一个 `problem_family` 必须恰好匹配一条能力。客户端不能在请求中提交脚本路径或替换 Operation 名称。

### 4.5 Operation、绑定脚本和严格输出

验证 Operation 使用 Markdown frontmatter 声明模式和被验证的求解 Operation：

```markdown
---
resource: operation
key: validate_demo
execution:
  mode: validation_workflow
  validates_operation: solve_demo
---
```

绑定脚本描述包含 `bound_operation` 和 entrypoint：

```markdown
---
resource: script
key: validate_demo_script
bound_operation: validate_demo
entrypoint: validate_demo.py
---
```

脚本必须输出一个且仅一个 JSON 对象：

```json
{
  "schema_version": "solver_coordination.validation_operation.v1",
  "results": [
    {"candidate_id": "ALL_0", "status": "feasible"},
    {"candidate_id": "ALL_1", "status": "infeasible"}
  ]
}
```

允许的结果状态只有 `feasible`、`infeasible`、`validation_error`。当状态为 `validation_error` 时，必须同时提供非空的 `error_code` 和 `error_stage`；其他状态不允许带错误字段。结果顺序必须与候选集完全一致，不能缺项、重复或插入未知字段。

### 4.6 验证结果和证据

`ValidationBatchResult` 聚合为以下状态：

| 条件 | `batch_status` | `selection_status` | 语义 |
| --- | --- | --- | --- |
| 全部结果有效，可能包含 feasible/infeasible | `validation_completed` | `published` | 可以发布可行候选 |
| 响应结构完整，但存在 candidate-level `validation_error` | `validation_failed` | `not_publishable` | 结果可审计，但不能发布 |
| 脚本失败、摘要漂移、输出非法、证据落盘失败 | 不生成成功结果 | HTTP 错误 | 运行级失败，按错误类型重试或修复 |

每个候选结果都带有可信身份字段：

```json
{
  "candidate_id": "ALL_0",
  "candidate_digest": "sha256:solver-coordination.v1:<64-hex-chars>",
  "status": "feasible",
  "knowledge_package_id": "knowledge_package_demo",
  "knowledge_package_revision": "1.0.0",
  "fact_snapshot_digest": "sha256:solver-coordination.v1:<64-hex-chars>",
  "rules_revision": "rules_demo_1",
  "evidence_ref": "validation_evidence_<digest>"
}
```

这些身份字段由平台根据可信制品补齐，不接受脚本自行篡改。

## 5. 端到端运行流程

1. 用户在 Solver 工作台填写知识包 ID、精确版本和有序计划。
2. 前端调用 `POST /api/solver/runs`，后端归一化计划并生成确定性候选集。
3. 后端把求解运行、候选制品和 candidate digest 写入工作区账本。
4. 前端只在候选制品完整时启用“校验候选集”。
5. 前端调用 `POST /api/solver/validations`，同时发送 `Idempotency-Key`。
6. 路由校验候选引用、父运行绑定、候选集摘要和服务端 validator revision。
7. `RunArtifactValidationContextResolver` 解析事实快照、版本能力、Operation 和绑定脚本。
8. `OntologyValidationOperationRunner` 将候选集和事实快照 payload 组合为一次 canonical JSON 请求。
9. `execute_script` 在 `frozen_snapshot` 模式运行，禁止 live provider 环境变量。
10. 解析器验证脚本输出，`ValidationEvidenceStore` 先持久化完整请求/响应证据。
11. 平台补齐候选身份并聚合结果，`ValidationRunStore` 原子写入验证运行和幂等指针。
12. 前端展示检查数量、可行 ID、发布 ID、规则版本、事实快照摘要和证据引用。
13. 刷新页面或后端重启后，前端用工作区 scoped run ID 调用 `GET /api/solver/runs/<run_id>` 恢复完整制品。

## 6. 后端代码适配说明

### 6.1 应用工厂注入生产验证服务

适配文件：[consumer/backend/app.py](../consumer/backend/app.py)。

生产服务由 `build_runtime_services()` 组装，而不是由路由临时创建：

```python
from solver_coordination.runtime import build_operation_validation_service

def build_runtime_services(previous_llm_router=None):
    # 既有 auth、LLM、workspace service 省略
    return {
        "config": config,
        "auth_manager": auth_manager,
        "user_store_manager": user_store_manager,
        "solver_validation_service": build_operation_validation_service(),
    }
```

`create_app()` 和 `refresh_runtime_services()` 都写入同一个配置键：

```python
app.config["SOLVER_COORDINATION_VALIDATION_SERVICE"] = runtime[
    "solver_validation_service"
]
```

刷新时先构造新的 runtime，再替换旧配置，避免构造失败时丢掉当前可用服务。适配新的运行期服务时，应保持这个工厂边界，不要在 Flask route 中保存 workspace 或 snapshot 状态。

### 6.2 路由和错误语义

实现文件：[consumer/backend/api/routes/solver_coordination.py](../consumer/backend/api/routes/solver_coordination.py)。

兼容路径保持不变：

```text
/agentic/solver-coordination/v1/candidate-sets
/agentic/solver-coordination/v1/validation-runs
```

前端使用稳定公共别名：

```text
POST /api/solver/runs
POST /api/solver/validations
GET  /api/solver/runs/<run_id>
```

校验请求必须带：

```http
Authorization: Bearer <session-token>
X-Workspace-Id: workspace_demo
Idempotency-Key: solver-validation-<caller-owned-key>
Content-Type: application/json
```

路由只负责输入校验、权限和错误映射；核心验证交给注入服务：

```python
service = current_app.config.get("SOLVER_COORDINATION_VALIDATION_SERVICE")
if service is None or not callable(getattr(service, "run", None)):
    return jsonify(error_payload("validation_service_unavailable")), 503

result = service.run(
    workspace_id=workspace_id,
    owner_id=user.username,
    candidate_set_ref=candidate_set_ref,
    source_run_id=source_run_id,
    candidate_set_digest=candidate_set_digest,
    validation_run_id=validation_run_id,
    validator_revision=validator_revision,
    parent_solve_run_id=parent_solve_run_id,
)
```

稳定错误分类：

| 错误码 | HTTP | 是否重试 | 典型原因 |
| --- | ---: | --- | --- |
| `invalid_solver_input` | 400 | 否 | 字段缺失、未知字段、计划格式错误 |
| `candidate_not_found` | 404 | 否 | 制品不存在或不属于当前 owner |
| `candidate_digest_mismatch` | 409 | 否 | 候选集、父运行或制品身份不一致 |
| `verification_divergence` | 409 | 否 | 快照、Operation、脚本内容或结果身份发生漂移 |
| `verification_runtime_failed` | 503 | 是 | 超时、沙箱失败、非法 JSON、证据暂时不可写 |
| `idempotency_conflict` | 409 | 否 | 同一个幂等键提交了不同输入 |
| `validation_store_unavailable` | 503 | 是 | 验证结果存储不可用 |

注意：`OntologyValidationOperationRunner` 会直接产生
`VerificationDivergenceError`，但当前 `OperationValidationRunService` 对
`batch_validator.validate()` 的通用异常包装仍可能把部分 runner 级顺序/输出偏差转换为
`verification_runtime_failed`（503）。如果产品要求所有确定性结果偏差都返回 409，需要在
`operation_validation.py` 的批量调用边界显式透传 `VerificationDivergenceError`。

#### HTTP 请求和响应要点

生成候选集：

```json
POST /api/solver/runs
{
  "knowledge_package_id": "knowledge_package_demo",
  "knowledge_package_revision": "1.0.0",
  "plan": {
    "option_domain": ["OPTION_0", "OPTION_1"],
    "max_transitions": 1,
    "items": [
      {"item_order": 1, "item_id": "item_demo_001", "quantity": "1", "duration": "2"}
    ]
  }
}
```

成功响应返回完整候选制品，前端至少保存 `source_run_id`、`candidate_set_ref`、`candidate_set_digest` 和 `candidates`。验证请求只提交制品身份，不提交脚本路径：

```json
POST /api/solver/validations
{
  "source_run_id": "solve_demo_001",
  "parent_solve_run_id": "solve_demo_001",
  "candidate_set_ref": "candidate_set_<digest>",
  "candidate_set_digest": "sha256:solver-coordination.v1:<64-hex-chars>",
  "validator_revision": "ontology_operation_batch.v1"
}
```

成功响应包含 `validation_run_id`、`result_count`、`all_candidate_results`、`feasible_candidate_ids`、`published_candidate_ids`、`batch_status` 和 `selection_status`。`GET /api/solver/runs/<run_id>` 会根据 ledger 中的 `run_type` 自动拼回候选制品或验证结果；这也是前端刷新和后端重启恢复的唯一入口。

### 6.3 运行账本和 lineage 适配

实现文件：[consumer/backend/solver_coordination/run_ledger.py](../consumer/backend/solver_coordination/run_ledger.py)。

运行表以 `(workspace_id, run_id)` 约束运行身份；制品表使用：

```sql
PRIMARY KEY (
  workspace_id,
  owner_id,
  artifact_type,
  artifact_digest,
  source_run_id
)
```

`source_run_id` 被纳入主键是必要的：相同内容摘要可以在两个独立求解运行中出现，但它们的授权 lineage 不相同。初始化时 `_ensure_artifact_lineage_schema()` 会把旧的 digest-only 表迁移到新的 lineage-aware 主键。

所有读取都必须带 `owner_id` 和当前 workspace。不能根据裸 run ID 或裸 digest 返回跨 owner 的存在性信息。

### 6.4 验证上下文解析

实现文件：[consumer/backend/solver_coordination/validation_resolver.py](../consumer/backend/solver_coordination/validation_resolver.py)。

`RunArtifactValidationContextResolver.resolve()` 的职责是把客户端可提交的候选身份转换为平台可信上下文：

```python
snapshot = snapshot_resolver.resolve(
    owner_id=owner,
    source_run_id=parent,
    knowledge_package_id=candidate_set.knowledge_package_id,
    knowledge_package_revision=candidate_set.knowledge_package_revision,
)

capability = self._resolve_capability(
    manifest,
    package_root=package_root,
    problem_family=candidate_set.problem_family,
)

script_path = self._resolve_bound_script(package_root, operation_key)
script_digest = self._digest_script(script_path)
```

解析结果是不可变的 `ResolvedValidationContext`，包含：

- workspace、owner、父求解运行和当前验证运行。
- 知识包 ID、精确 revision、problem family。
- validation Operation、validator revision、rules revision。
- fact snapshot ref、digest、路径和 payload。
- 绑定脚本路径、脚本内容 digest、ontology 目录。

客户端不能选择 `operation_key`、`script_path` 或活动版本。

### 6.5 单次冻结批量执行

实现文件：[consumer/backend/solver_coordination/validation_runner.py](../consumer/backend/solver_coordination/validation_runner.py)。

请求中携带完整的事实快照 payload，保证脚本只依赖这次运行的冻结事实：

```python
request_payload = {
    "schema_version": "solver_coordination.validation_request.v1",
    "candidate_set": candidate_set.to_dict(),
    "fact_snapshot": {
        "ref": context.fact_snapshot_ref,
        "digest": context.fact_snapshot_digest,
        "payload": context.fact_snapshot_payload,
    },
}

raw_output = self.execute_operation(
    ontology_id=context.knowledge_package_id,
    ontology_version=context.knowledge_package_revision,
    operation_key=context.validation_operation,
    request_payload=request_payload,
    data_access="frozen_snapshot",
    context=context,
)
```

默认执行器随后把 `context.validation_script_path` 和 `context.validation_script_digest` 传给 `handle_execute_script()`，在真正启动子进程前再次检查路径和内容摘要。

这里有三个不能省略的约束：

1. 一个候选集只调用脚本一次，不能按候选逐个调用。
2. 脚本输出必须是严格 JSON，字段、状态、数量和候选顺序全部校验。
3. 证据写入成功后，才把 `evidence_ref` 注入候选结果并允许聚合发布。

### 6.6 脚本执行器和 JiuwenBox 适配

实现文件：[execute_script.py](../consumer/backend/agent/tools/execute_script.py) 和 [executor.py](../consumer/backend/integration/sandbox/executor.py)。

新增参数保持向后兼容：

```python
def handle_execute_script(
    *,
    ...,
    data_access: str = "live",
    expected_script_path: Optional[Path] = None,
    expected_script_digest: Optional[str] = None,
):
```

当 `data_access == "frozen_snapshot"` 时：

- 跳过 live business-data connection 的解析和注入。
- 子进程不能通过实时连接环境变量访问 Provider。
- 仍使用现有的工作区路径 containment 和 JiuwenBox sandbox。
- 执行前再次比较解析到的脚本路径和脚本内容 digest。

这层防止了“解析阶段绑定脚本 A，执行阶段实际运行脚本 B”的 TOCTOU 类漂移。

### 6.7 验证运行和幂等存储

实现文件：[validation_store.py](../consumer/backend/solver_coordination/validation_store.py)。

验证结果和幂等指针分别写入：

```text
<workspace-runtime>/solver_coordination/validation_runs/<owner-hash>/<run-id>.json
<workspace-runtime>/solver_coordination/validation_idempotency/<owner-hash>/<key-hash>.json
```

幂等作用域是 `(workspace_id, owner_id, idempotency_key)`。路由在读取已有结果、执行验证和写入结果之间持有同一把进程内锁；持久化结果带 payload digest，重启后读取时会重新校验 envelope、scope、fingerprint 和 digest。

运行时失败不会写入成功幂等指针，因此修复底层原因后可以沿用同一个幂等键重试。已经成功落盘但最终 ledger 更新失败时，接口仍返回已持久化结果，同时记录日志，不把客户端误导为可重试失败。

## 7. 知识包适配指南

### 7.1 推荐目录结构

```text
<package>/
├── manifest.yaml
└── versions/
    └── 1.0.0/
        ├── solver-capabilities.yaml
        ├── operations/
        │   └── validate_demo.md
        └── scripts/
            ├── validate_demo.md
            └── validate_demo.py
```

版本目录中的能力文件、Operation 和脚本必须跟随版本一起发布。不要只修改共享 manifest 后复用旧版本的验证语义。

### 7.2 绑定脚本示例

脚本通过结构化参数接收 `request_json`，而不是从环境变量读取实时业务数据：

```python
import argparse
import json

parser = argparse.ArgumentParser()
parser.add_argument("--request-json", required=True)
request = json.loads(parser.parse_args().request_json)

# 只读取冻结快照和候选集，不连接业务 Provider。
facts = request["fact_snapshot"]["payload"]
candidates = request["candidate_set"]["candidates"]

print(json.dumps({
    "schema_version": "solver_coordination.validation_operation.v1",
    "results": [
        {"candidate_id": candidate["candidate_id"], "status": "feasible"}
        for candidate in candidates
    ],
}))
```

生产脚本应把规则计算封装在包内，并把错误映射为稳定的 `error_code` 和 `error_stage`。不要在脚本中自行生成 `candidate_digest`、`fact_snapshot_digest` 或 `evidence_ref`。

### 7.3 接入检查清单

- [ ] 精确版本目录存在 `solver-capabilities.yaml` 或等价能力文件。
- [ ] `problem_family` 与候选集一致，且只匹配一条能力。
- [ ] `validator_revision` 为当前支持的 `ontology_operation_batch.v1`。
- [ ] `rules_revision` 与事实快照信封一致。
- [ ] Operation frontmatter 的 `mode` 为 `validation_workflow`。
- [ ] `validates_operation` 指向同版本中的求解 Operation。
- [ ] 绑定脚本唯一、路径在包目录内、entrypoint 可执行。
- [ ] 脚本输出严格 JSON，结果顺序与候选集一致。
- [ ] 脚本不使用 live provider、工作区外文件或外部网络依赖。

## 8. 前端代码适配说明

### 8.1 API 适配层

实现文件：[api.js](../consumer/frontend/src/features/solver/api.js)。所有请求复用现有 `authFetch` 和错误归一化：

```javascript
export function createValidationRun(
  payload,
  idempotencyKey,
  { request = authFetch, signal } = {},
) {
  const normalizedKey = cleanText(idempotencyKey);
  if (!normalizedKey) return Promise.reject(new Error("missing idempotency key"));
  return requestSolverJson(
    request,
    "/api/solver/validations",
    jsonOptions(payload, {
      signal,
      headers: { "Idempotency-Key": normalizedKey },
    }),
    "校验候选集失败",
  );
}
```

适配新的后端错误时，优先扩展 `requestSolverJson()` 的稳定字段，不要在工作台组件里解析 HTTP 文本。

### 8.2 计划模型和重试语义

实现文件：[model.js](../consumer/frontend/src/features/solver/model.js)。模型层负责：

- 前端表单到公开 API payload 的归一化。
- `OPTION_0`、`OPTION_1` 和 `max_transitions=1` 的当前问题族约束。
- workspace-scoped run ID 的 localStorage 读写。
- 请求 epoch，拒绝旧 workspace 或旧请求覆盖新状态。
- 验证尝试的幂等键生命周期。

重试必须复用同一个 key，显式发起新验证才创建新 key：

```javascript
const keyManager = createValidationAttemptKeyManager();
const firstKey = keyManager.begin();
const retryKey = keyManager.retry();
// firstKey === retryKey
keyManager.complete();
```

### 8.3 工作台和 AppShell 接入

实现文件：[SolverCoordinationWorkbench.jsx](../consumer/frontend/src/features/solver/SolverCoordinationWorkbench.jsx) 和 [AppShell.jsx](../consumer/frontend/src/app/AppShell.jsx)。

工作台有三个无嵌套卡片的区域：

1. **Plan Input**：知识包 ID、revision、选项域、切换上限、计划条目表。
2. **Candidates**：求解运行、候选引用、摘要、候选 assignment 表。
3. **Validation**：检查数、完成数、异常数、可行 ID、发布 ID、规则 revision、事实快照摘要和证据引用。

工作区切换会中止旧恢复请求并清空旧状态：

```javascript
useEffect(() => {
  restoreControllerRef.current?.abort();
  requestEpochGuardRef.current.invalidate();
  validationAttemptKeyRef.current.reset();
  dispatch({ type: "reset", workspaceId });
}, [workspaceId, resetToken]);
```

求解开始时也会中止恢复请求，避免慢响应覆盖新生成的候选集。刷新页面时只恢复当前 workspace 保存的 run ID，服务端 404 会清理该 workspace 的陈旧引用，不会跨工作区查询。

## 9. 持久化、部署和运行目录

### 9.1 运行目录

求解器运行时制品位于现有 workspace runtime 下：

```text
<workspace-runtime>/solver_coordination/
├── run_ledger.sqlite3
├── candidate_sets/
├── fact_snapshots/
├── validation_runs/
├── validation_idempotency/
└── validation_evidence/
```

SQLite 使用 WAL；JSON 制品写入使用临时文件、`fsync` 和原子 rename。容器部署必须把 workspace runtime 作为持久卷或稳定 bind mount，否则进程重启后只能恢复空状态。

### 9.2 运行时服务

完整 Docker Compose 验收包含 backend、frontend、MCP、认证服务和 JiuwenBox。backend 启动时需要已配置的认证服务地址，JiuwenBox 通过 Unix socket 提供沙箱执行能力。生产环境的认证令牌和模型配置只能通过外部环境注入，不能写进知识包、脚本或文档。

### 9.3 发布前检查

- 确认版本目录和事实快照位于同一 workspace runtime 约束下。
- 确认 backend 和前端使用同一 API contract。
- 确认 `X-Workspace-Id` 被 CORS 和反向代理允许转发。
- 确认 `Idempotency-Key` 没有被代理丢弃或改写。
- 确认容器重启后 SQLite、JSON 制品和证据目录仍可读。

## 10. 测试和验收矩阵

### 10.1 后端测试

| 测试范围 | 覆盖内容 |
| --- | --- |
| `test_solver_contracts.py`、`test_binary_ordered_route.py` | 计划、候选顺序、摘要和问题族不变量 |
| `test_validation_artifacts.py` | 快照 envelope、owner/workspace、路径 containment、摘要篡改、证据原子写入 |
| `test_validation_operation_resolver.py` | 精确版本、能力唯一性、rules revision、Operation 和脚本绑定 |
| `test_validation_operation_runner.py` | 单次批量执行、严格 JSON、顺序/数量/脚本 digest 检查 |
| `test_solver_validation_runtime.py` | app create/refresh 注入和构造失败保护 |
| `test_solver_coordination_validation_route.py` | 路由、幂等、错误映射、GET 恢复、ledger finalize 失败语义 |
| `test_solver_operation_runtime_integration.py` | 真实版本目录、能力文件、脚本、冻结 snapshot、证据落盘的集成闭环 |
| `test_sandbox.py`、`test_execute_script_*` | 冻结模式移除实时连接、结构化参数和沙箱边界 |

### 10.2 前端测试

- `api.test.js`：请求路径、JSON body、幂等 header、网络错误和稳定错误码。
- `model.test.js`：计划归一化、候选完整性、workspace storage、epoch 和幂等 key。
- `SolverCoordinationWorkbench.test.js`：三段式结构、错误状态、可行/发布 ID、可访问标题和响应式样式。
- `npm run lint`、`npm run check:architecture`、`npm run build`：静态、边界和生产构建。

### 10.3 Docker 端到端验收

使用纯合成包和纯合成事实执行：

```text
register/login
  -> create workspace
  -> POST /api/solver/runs
  -> create/register fact_snapshot
  -> POST /api/solver/validations
  -> GET solve run and validation run
  -> restart backend
  -> GET again and compare candidate_set_digest/evidence_ref
```

验收完成条件：所有服务健康、前端代理可访问 backend、验证脚本只读冻结 payload、结果和证据在重启后仍可恢复，并且没有把凭据或真实业务数据写入源代码。

## 11. 扩展路径

### 11.1 新增问题族

新增问题族时，应按以下顺序扩展：

1. 在 `solver_coordination/problem_families/` 增加纯函数枚举器和 shape assertion。
2. 在 `contracts.py` 中复用通用 `PlanItem`、`Candidate`、`CandidateSet`，只增加真正通用的字段。
3. 为新 family 定义版本化 capability 条目和对应 validation Operation。
4. 通过 resolver、runner 和 route 测试验证顺序、数量、digest 和 rules revision。
5. 前端只有在约束稳定后才开放新的 option domain 或 transition 参数。

不要让前端先接受后端尚未支持的约束；当前 `max_transitions` 的精确值校验就是这一原则的实现。

### 11.2 事实快照生产者

下一阶段可以增加独立的 snapshot producer，从 Provider 读取事实、规范化 payload、写入 `fact_snapshot` 制品并登记 ledger。它必须是独立生命周期，不能在验证脚本中临时查询 Provider，否则会破坏 solve/validate 的事实一致性。

### 11.3 异步验证

当批量脚本耗时超过同步 HTTP 的合理窗口时，可以把当前 `OperationValidationRunService.run()` 放到任务队列中，但以下契约不能改变：

- 一个 validation run 仍只对应一次批量 Operation invocation。
- 幂等 key 仍绑定同一输入 fingerprint。
- 运行状态和最终结果仍落在同一 workspace/owner scope。
- 证据必须在发布结果前完成持久化。

### 11.4 证据治理

可以在不改变运行契约的前提下增加保留策略、归档策略和审计查询。归档只能移动或复制已验证的 content-addressed artifact，不能修改其 digest、source lineage 或 evidence reference。

## 12. 当前实现限制与适配注意事项

以下内容是当前代码的真实边界，接入方不应把它们当成已经完成的能力：

1. **事实快照目前只有读取器。** `FactSnapshotArtifactResolver` 负责严格读取和校验快照；当前没有正式的 `FactSnapshotStore.put()` 或 Provider-to-snapshot 生产服务。真实业务接入需要单独实现“读取事实、规范化 payload、原子写入、登记 ledger”的生产者。
2. **求解阶段仍固定使用问题族枚举器。** `create_candidate_set()` 当前调用 `binary_ordered_route.enumerate_ordered_routes()`，并不会执行 capability 中的 `operation`。该字段目前用于验证 Operation 的 `validates_operation` 绑定检查。要支持真正的 Operation-backed solver，需要另行定义求解输入/输出契约和求解执行器。
3. **候选生成阶段不验证包是否存在。** 候选制品可以先记录一个结构化的包 ID/revision；精确包版本、能力和绑定脚本是在验证阶段解析。若业务要求提交求解计划时就拒绝不存在的版本，需要在候选路由增加只读包存在性检查。
4. **幂等锁是进程内锁。** `ValidationRunStore.idempotency_lock()` 使用进程内 `RLock`。单进程或单 worker 下可串行化同一 key；多 worker 部署仍需要数据库唯一约束、分布式锁或任务队列，才能完全避免跨进程重复执行。
5. **冻结模式不是完整的网络隔离。** `frozen_snapshot` 会移除平台管理的实时业务数据连接变量，并把请求 payload 固定为快照；但 sandbox `prefer` 模式仍可能在沙箱不可用时按配置本地回退。本地回退不等价于完整网络阻断，生产环境应把 sandbox 配置为强制模式并单独做网络策略审计。
6. **证据目前没有内容读取 API。** 后端会把完整请求/响应写入 `validation_evidence`，前端当前只展示 `evidence_ref`，不能直接下载或展开证据内容。若需要审计 UI，应新增 owner/workspace-scoped 的证据读取接口，并复用同样的 digest 和权限校验。
7. **前端缺少真实 DOM 流程测试。** 当前 Solver 测试覆盖模型、API 和源码结构；异步恢复/重试的核心竞争条件由模型级测试覆盖，但还没有浏览器级表单提交、慢恢复响应和工作区切换的真实 DOM 测试。
8. **候选集不是发布制品。** `CandidateSet.authoritative` 当前固定为 `False`；候选只表示待验证的确定性枚举结果，只有完整批量校验且无 candidate-level `validation_error` 时，`published_candidate_ids` 才可被视为发布选择。
9. **`candidate_digest` 不会缩小校验范围。** 请求中的 candidate digest 只用于确认候选属于当前候选集；验证仍然执行完整候选批次，不能通过它请求单候选校验。

这些限制不影响当前端到端闭环，但决定了后续扩展的正确切入点：先补齐快照生产和跨进程幂等，再考虑求解 Operation 化、证据查询和浏览器级验收。

## 13. 实现文件地图

| 文件 | 适配职责 |
| --- | --- |
| `consumer/backend/solver_coordination/contracts.py` | 计划、候选和候选集的领域契约 |
| `consumer/backend/solver_coordination/candidate_service.py` | 结构化计划解析和候选集生成入口 |
| `consumer/backend/solver_coordination/run_ledger.py` | workspace/owner/run/artifact lineage 账本 |
| `consumer/backend/solver_coordination/validation_artifacts.py` | 快照解析和验证证据原子存储 |
| `consumer/backend/solver_coordination/validation_resolver.py` | exact revision、capability、Operation、script 解析 |
| `consumer/backend/solver_coordination/operation_validation.py` | 运行期 context port 和批量服务边界 |
| `consumer/backend/solver_coordination/validation_runner.py` | 单次冻结脚本执行和严格 JSON 映射 |
| `consumer/backend/solver_coordination/validation_store.py` | 验证结果、幂等指针和重启恢复 |
| `consumer/backend/api/routes/solver_coordination.py` | HTTP 输入、权限、别名和稳定错误 |
| `consumer/backend/app.py` | 生产服务 create/refresh 注入 |
| `consumer/backend/agent/tools/execute_script.py` | 脚本参数、路径/digest 和 data access 模式 |
| `consumer/backend/integration/sandbox/executor.py` | JiuwenBox / local sandbox 的冻结环境 |
| `consumer/frontend/src/features/solver/api.js` | Solver HTTP 适配层 |
| `consumer/frontend/src/features/solver/model.js` | 计划归一化、storage、epoch、幂等 key |
| `consumer/frontend/src/features/solver/SolverCoordinationWorkbench.jsx` | 工作台状态机和三段式 UI |
| `consumer/frontend/src/app/AppShell.jsx` | lazy surface、导航和 workspace gate |

## 14. 结论

当前实现已经把求解器协同从“候选生成接口”推进到“可冻结、可验证、可审计、可恢复”的端到端能力。后续新增规则时，优先扩展知识包版本和 Operation；后端只负责信任边界、制品一致性和生命周期；前端只负责结构化输入、状态反馈和工作区范围内的恢复。
