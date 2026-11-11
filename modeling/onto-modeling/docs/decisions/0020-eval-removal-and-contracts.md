# 0020 评测移出 onto-modeling，改由三份契约对接评测服务

> 包出的 X-01 与本地版本号、验证回执入整节已由 [ADR-0098](0098-hub-release-authority.md) 取代（正式版本以知识包 Hub 为准，求解验证移到评测服务）。
> 反馈入 X-05 与「onto 不主动调用评测服务」已由 [ADR-0092](0092-eval-service-integration.md) 取代（以用户登录读回评测结果，评测优化成批反馈）。

## Context

插件化详细设计第 11 章把评测划到 onto-modeling 之外：onto 只管建模和发布，评测服务拿发布的知识包去评，
结论再送回来。移出前的现状（后端改造方案 2.1 节）：

- 后端有 9 个评测相关模块（编排、数据源、上传表、评测集、一键导入、求解端到端验证编排、Modeling 项目
  关联、远端清理队列、知识包同步），20 个评测路由，启动时还有评测编排的收敛与远端清理调度；
- 核心反过来依赖评测：项目进度读评测状态算第 11 个阶段，发布的数据绑定把评测数据源里上传的表也当表来源；
- 求解知识包的发布门禁要一条「当前修订的权威通过」验证记录，这条记录只有求解验证编排写得出来——直接删，
  求解包就再也发布不了；
- 评测数据源配置（含数据库密码）放在数据目录，上传的表放在各工作区 `.ontology-state/eval-data/`；
- onto 主动推包给 Modeling，没有让外部服务来取包、回写结论的入口。

## Decision

### 1. 删除的部分

- 模块（按依赖顺序）：`modeling_package_sync` → `solver_validation_run` → `modeling_eval` → `eval_import`、
  `eval_sets` → `eval_data_sources` → `eval_data_files` → `modeling_cleanup` → `modeling_mcp`；
- 路由：`/api/projects/{id}/eval/*` 共 20 个，求解验证用例与启动（`package/solver-validations/cases`
  的 GET / PUT、`POST package/solver-validations`），管理员的 `modeling-cleanups` 两个；
- 新建 / 导入 / 删除项目时关联、级联删除 Modeling 项目的逻辑（详设第 11 章把 Modeling 关联划到评测侧）；
- 评测阶段：阶段 11 个变 10 个，项目进度不再带评测分数与演进提示；
- 配置：`ONTO_WEB_MODELING_URL`、`ONTO_WEB_MODELING_TOKEN`、`ONTO_WEB_MODELING_ASSOCIATION_URL`、
  `ONTO_WEB_MODELING_ASSOCIATION_TOKEN`、`ONTO_WEB_MODELING_CLEANUP_TOKEN`、
  `ONTO_WEB_MODELING_CLEANUP_INTERVAL`、`ONTO_WEB_EVAL_MAX_CONCURRENT_ROUNDS`，以及部署包里的
  `configure-modeling-association.sh`；
- 工具箱：删 `mock_to_eval_seed.py`；`check_eval_data.py` 拆出表登记 `table_profile.py`（只读 schema 资料），
  旧名保留一个版本转发。

保留：`solver_validation` 的记录与门禁（`gate`、`latest`），对象与数据表映射的发布门禁，`export_package_zip`
的图模式（方案第 6 章第 1 项：留作发布适配器的变体，下一阶段收进适配器；只删了只有评测在用的参数
`relation_report`、`object_texts` 与绑定解析的 `hints`），`check_eval_generalization.py`（随反馈演进包迁出，
包机制落地前先留在工具箱）。

### 2. 先抽两个公共件

- `services/credential_store.py`：凭据文件 0600 原子写（临时文件创建即 0600）、只归凭据的目录 0700、
  对外视图把密钥换成 `has_*`、留空沿用、可靠删除（连同写了一半的临时文件）。个人模型配置与管理员模型
  配置的写盘改用它；服务令牌库用它。
- `services/background_jobs.py`：进程内编排（同键只跑一个、线程执行、强引用任务）、持久记录
  （`JobStore`）、启动收敛注册表（`register_startup_sweep` / `run_startup_sweeps`）。求解验证记录改走
  `JobStore`（文件格式不变），启动时登记它的收敛步骤。

后面的证据源层（数据源登记的凭据、采集任务）复用这两个公共件。

### 3. 三份契约

onto 不主动调用评测服务。接口路径、字段与错误码按 API 契约（`docs/api-v1.md` 的 1.3、3.20、3.21）实现，
都在 `/api/v1` 下，用**服务令牌**鉴权（详设第 14 章、方案第 6 章第 6 项）：

- 管理员在 `/api/v1/admin/service-tokens` 签发、列出、吊销（X-06）；令牌形如 `onto_svc_…`，只存 SHA-256 摘要，
  数据目录 `service-tokens.json`（0600），与 MCP token 的 SQLite 库分开；鉴权中间件看到契约接口就不做登录
  校验，由接口自己验服务令牌；
- 权限范围三选多：`releases.read`、`receipts.write`、`feedback.write`；可用 `project_ids` 限定项目，可设有效期；
- 服务令牌只能调用契约接口，读不到项目内容，也碰不到管理接口，用在别处一律 401；
- 契约接口的错误体已经按 API 契约 1.5（`code`、`message`、`details`，另带 `error` 兼容旧调用方）。

**包出（㉓）**

- X-01 `GET /api/v1/contract/releases`：令牌授权范围内全部项目的发布，按发布时间从新到旧，游标分页；
- PB-05 发布记录、PB-06 包清单、PB-07 下载（`/api/v1/projects/{id}/releases/{version}[/manifest|/download]`），
  服务令牌或项目所有者都能读；PB-04 发布记录列表只给所有者。发布记录 = `release/*` 标签（版本、提交、发布时间、
  发布人、是否降级）+ 留存记录（格式、包摘要、拉取记录）；服务令牌下载时记「已拉取」。
- **下载读发布时留存的包，不按标签重新渲染。** 发布时把包原样存到数据目录 `releases/<项目>/<版本>.zip`
  （带 `.json` 记录：sha256、大小、格式、来源提交、发布人、拉取记录），先留存再打标签，留存失败就不算发布；
  同一版本已有留存时以留存为准，重复发布拿到的也是那一份。理由：渲染器会随版本升级（下一阶段就要把导出
  收进发布适配器），重新渲染同一个提交可能得到不同的字节，下游按版本去重会拿到和第一次不一样的包。
  标准包留存缺失时（数据目录丢失、本功能之前打的标签）按标签提交重新渲染一次并补存，记录标 `backfilled`。
- 图模式变体（`variant=graph`，给评测服务导入图库）第一次下载时按同一标签提交渲染、留存为
  `<版本>.graph.zip`，之后同样读留存。

**验证回执入（㉕）**

- **契约补充** `GET /api/v1/projects/{id}/solver/candidate`（`receipts.write`）：求解验证候选包。验证方要先拿到
  包才能验证，而求解包没有通过的回执就发布不了，API 契约里没有这一步，补在这里（已记入
  `_plan/contract-changes.md`）。按 HEAD 提交渲染、manifest 标 `status: draft`、不打标签；响应头带求解契约
  修订号、契约摘要与发布时会得到的版本号。只跳过验证门禁本身，静态检查与数据绑定照发布同口径拦截。
- X-04 `POST /api/v1/projects/{id}/solver/receipts`：写一条验证记录（仍在 `.ontology-state/solver-validations.json`）。
  修订号必须是当前修订——HEAD 提交的（发布读它）或工作区的（面板读它），否则 `422 receipt_revision_unknown`；
  `solver_keys` 必须正好是该修订启用的求解操作，`contract_digest` 必须一致；`receipt_id` 是幂等键，只生效一次。
  记录只存结论、摘要和诊断引用（`evidence` 不超过 64 KB），消息里的地址脱敏。发布门禁与面板的 `latest` 不变。

**反馈入（㉔）**

- X-05 `POST /api/v1/projects/{id}/feedback`：请求按 API 契约（`feedback_id`、`source{service, run}`、
  `release_version?`、`items[{case_id, question, expected, actual, loss_reason?, suspected_artifacts?}]`、
  `received_at?`，最多 200 条），另收两项补充：`summary` 与条目的 `verdict`（fail / partial / error）、
  `tool_failures`——反馈演进要靠它们区分运行异常和答错（已记入 `_plan/contract-changes.md`）。
- 一批反馈写到工作区 `feedback/<接收时间UTC>-<feedback_id>.json`，只提交这个文件；不在发布读取的目录里，
  不进知识包；随项目克隆、导出、回退。`feedback_id` 只生效一次；`release_version` 给了就必须是已有发布。
- 文件格式 `onto-feedback/v1`：

  ```json
  {
    "format": "onto-feedback/v1",
    "feedback_id": "round-001",
    "source": {"service": "评测服务名", "run": "评测轮次"},
    "release_version": "0.1.12",
    "summary": {"score": 72.5, "total": 40, "pass": 30, "partial": 2, "fail": 6, "error": 2},
    "items": [{
      "case_id": "c1", "question": "…", "expected": "…", "actual": "…",
      "loss_reason": "…", "suspected_artifacts": ["ontology/object/…"],
      "verdict": "fail", "tool_failures": ["工具名 · 错误码 · 问题：问题码 字段路径 · 退出码 · 详情"]
    }],
    "reported_at": "评测侧给的 received_at，可为空",
    "received_at": "2026-11-01T08:00:00.000Z",
    "received_by": "service:评测服务"
  }
  ```

  `/ontology-evolution` 与 `check_eval_generalization.py` 默认读文件名最新的一批（文件名以接收时间开头）。
- 待处理汇总（kind `feedback`）与所有者的反馈列表、状态标记（R-11、R-12）随待处理与反馈演进包在后续阶段做。

### 4. 启动迁移

每次启动执行，一次性、幂等（`services/eval_retirement.py`，下个大版本整体删除）：

1. 各项目 `.ontology-state/eval-data/` 里的上传表，凡有 Sheet 被对象的数据绑定用到（声明或按列推断），
   复制进 `resource/excel/`（同名不同内容时文件名加文件 ID），在 `materials.json` 标为 `schema`，单独提交，
   只提交这些路径；同名 Sheet 已在 schema 资料里的不迁；
2. 删除 `.ontology-state/` 下的评测数据（上传表、`eval.json`、求解验证用例）与元数据里的 Modeling 关联字段；
3. 全部项目处理完，删除数据目录下的评测数据源配置 `eval-data-sources/`（含连接密码）与 Modeling 清理队列。
   单个项目迁移失败只记运维错误日志，它的评测数据留到下次启动重试；数据源配置不等它，照删。

## Alternatives considered

- **按版本下载时按标签重新渲染，不留存包**：省磁盘、不用管备份，但同一版本在渲染器升级前后字节不同，
  违背「同一版本同一份包」；下一阶段改发布适配器时就会出问题。
- **回执入口之外不给候选包，验证方拿已发布的包验证**：求解包没有回执发布不了，发布不了就拿不到包，
  死循环；放宽求解门禁（先发布再验证）又让门禁失去意义。
- **反馈放 `.ontology-state/`（不进 git）**：不会产生提交，但克隆、导出、回退都丢，演进报告引用不到；
  而且会出现在 `git status` 里干扰演进的「落点自检」——放进 `.ontology-state/` 又得另配忽略规则。
  选了进 git、单独提交。
- **服务令牌复用 MCP token 库**：MCP token 归项目所有者、代表一个人读写本体，而服务令牌代表外部服务、
  只能调契约接口；混在一起，吊销、审计和权限边界都说不清。
- **迁移时把全部上传表都迁进资料区**：没被对象引用的表迁过去只会多出用不上的 schema 资料，还会改变
  未声明对象的推断绑定；按引用迁移和移出前的发布口径一致。

## Consequences

- 评测服务改调三个契约接口；onto 侧不再需要任何评测相关的环境变量。仓库根目录的整栈部署脚本仍会往 onto
  的 `.env` 写 `ONTO_WEB_MODELING_URL`（不再读取，无害），由部署线另行清理。
- 发布多了一步写盘（留存包），数据目录多了 `releases/`，计入备份，项目永久删除时一并删除。
- 上传过评测数据表的项目，资料分析验收回执会失效一次（指纹含这些表与 `materials.json`），需重新核验。
- 求解验证回执的写入方从 onto 内部编排变成外部验证方；面板读取与发布门禁不变。
- 前端的评测界面与 Modeling 关联界面还在旧前端里，调用的接口已不存在；前端整体重写时一并去掉。
- ADR-0015（评测专用线程池）随评测编排一起作废。
