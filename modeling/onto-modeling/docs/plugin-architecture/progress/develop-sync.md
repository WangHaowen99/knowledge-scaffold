# 吸收 develop 新特性（2026-11-11）

本分支 11-03 从 develop 的 e85ffb61 分出。此后 develop 合入 18 个 MR、115 个提交（到 26ab3d15），其中 onto-modeling 改了 162 个文件。合并提交只合入 onto-modeling 以外的改动，onto-modeling 的功能按下表移植到插件化结构。

## 方向

| 冲突 | develop | 本分支原做法 | 定 |
|---|---|---|---|
| 发布去向与版本号 | 知识包 Hub 是正式版本权威，版本号由 Hub 分配；评测服务、consumer 导入都只认 Hub | 本地 `releases/` 留存，版本号按主线计数；评测服务按 X-01 来取 | 跟 develop。Hub 接在发布流程的留存环节，本地不再自算版本号 |
| 求解验证 | 迁到评测服务；发布时勾选「尚未端到端验证」即可 | 发布门禁要 X-04 验证回执 | 跟 develop。X-01、X-04 和求解候选包接口退役 |
| 主服务读原件 | 来源预览、Excel 选 Sheet、表头预检在主服务解析原件 | ADR-0076：主容器只读网关转出的文本版 | 保留 ADR-0076；网关文本版带原件位置，行为与精度同 develop |
| 旧版评测记录 | 保留查看入口 | 删除的评测接口一律 410 | 只读入口搬进新前端 |

外部服务按原路径调用 onto，路径不能变：

- 评测服务 `GET /api/hub/package-projects`（用户令牌）；
- 评测服务 `POST /api/internal/evaluation/notifications`（服务令牌）。

## 第二轮：行为与界面对齐 develop（2026-11-12）

用户要求：除评测优化外，行为和 develop 一致；develop 页面上的元素都要有，样式按新 UI 调整。第一轮里为贴合插件化结构删掉的
界面元素、改过的行为全部改回，develop 的测试按原断言移植。仍与 develop 不同、且有原因的：

- 评测优化写成一批外部反馈交给反馈演进插件（ADR-0092）；
- 单人所有权（没有 viewer）、插件流程、`/api/v1` 接口风格；
- 已随评测移出的旧任务停止、对账、验证用例存储（ADR-0020）；评测或求解验证在跑时不挡回退（状态在评测服务）；
- 发布按提交冻结只读树（工作区里没提交的改动不进包）。

## 移植清单

路径相对 `modeling/onto-modeling/`。A 为 develop 新增，M 为修改；「已删」表示本分支已经没有这个文件，只能按功能移植。

### P1 Hub 发布与求解门禁（!552、!570、!580 的发布部分）

- 后端：`services/knowledge_hub.py`（A）、`services/hub_packages.py`（A）、`routers/hub_packages.py`（A）、`services/consumer_package_rules/`（A）、`backend/scripts/sync_consumer_package_rules.py`（A）、`services/onto_mcp/package_publish.py`（A）、`services/solver_contract.py`、`services/package_mapping.py`；`routers/projects.py` 的 `package/releases*`、`package/resources*`；`workspace.py`（已删）的发布、求解门禁、资料同步部分；`main.py`、`routers/admin.py` 的 Hub 任务恢复与备份；MCP 的发布预览、发布、发布状态三个工具。
- 前端：`HubPackagesDialog`、`PublishedVersionsDialog`、`PublishCheckDialog`、`SolverModelPanel`（验证界面移出）、`lib/packagePublish.js`、`lib/solver-contract-model.js`。
- 测试：`conftest.py`、`test_hub_packages`、`test_hub_transport`、`test_hub_consumer_import`、`test_knowledge_hub`、`test_consumer_package_rules`、`test_consumer_rules_parity`、`test_publish_binding_gate`、`test_solver_contract_check`、`test_solver_package_roundtrip`；前端 `hubPackages`、`packagePublish`、`packageReleases`。

### P2 评测对接（!552 评测部分、!571、!580 的 onto 侧）

- 后端：`services/eval_client.py`、`services/evaluation_entry.py`、`services/package_evaluations.py`、`routers/package_evaluations.py`（均 A）；`main.py` 的完成通知与服务身份；`routers/settings.py` 的 `/evaluation/entry`；`modeling_eval.py`、`eval_import.py`、`solver_validation_run.py`（均已删）的改动按「旧版评测记录只读」处理；`check_eval_data.py`；evo 包的 `ontology-evolution` 命令。
- 前端：`PackageEvaluationModals`、`lib/evaluation.js`、`lib/packageEvaluation.js`、`CenterPane` 的评测入口。
- 测试：`test_package_evaluations`、`test_modeling_eval`、`test_eval_*`；前端 `evaluationNavigation`、`packageEvaluation`。

### P3 版本历史与回退（!567 的版本部分、!570 的 MCP 回退）

- 后端：`services/version_history.py`、`services/version_revert.py`（均 A）；`routers/projects.py` 的 `/history`、`/versions/revert-preview`；`workspace.py` 的回退部分（资料随回退一起恢复、并发保护）；MCP 的版本列表、回退预览、回退三个工具与确认令牌。
- 前端：`LeftPane` 的历史、`lib/versionHistory.js`、`lib/workspaceDirty.js`、`lib/latestRequest.js`、`lib/mcpClientConfig.js`。
- 测试：`test_version_history`、`test_lifecycle_and_versions`、`test_onto_mcp`（版本部分）；前端 `versionHistory`、`workspaceDirty`、`latestRequest`、`studioRollbackRefresh`、`mcpClientConfig`。

### P4 包版本对比（!585）

- 后端：`services/package_compare.py`、`services/package_compare_worker.py`（均 A）；`routers/projects.py` 的 `package-comparisons/*`。
- 前端：`PackageCompareDialog`、`ReadonlyPackageDiff`、`lib/packageCompareView.js`。
- 测试：`test_package_compare`；前端 `packageCompareView`。

### P5a 资料上传与表格（!567 的资料部分、!589 的上传提示）

- 后端：`assets/workspace-toolkit/scripts/table_sources.py`（A）、`services/material_detect.py`、`services/toolkit.py`；`routers/projects.py` 的 `/resource-meta/schema-preview`；`workspace.py` 的上传校验。
- 前端：`ExcelView`、`home/CreatePage`、`lib/resourceUpload.js`。
- 测试：`test_resource_upload`、`test_schema_sheet_selection`、`test_material_detect`；前端 `resourceUpload`、`schemaScopeRequests`。

### P5b 来源预览、Studio 与规则出处（!589）

- 后端：`services/source_preview.py`（A）、`services/studio.py`（`/studio/updates`）、工具箱 `source_extract.py`、`research_checker.py`、`rule-template.md`、`research-agent.md`、`rule-metric-extraction-agent.md`、`ontology-research.md`、`ontology-extract-object.md`、`workspace-rules.md`。
- 前端：`SourcePreview`、`lib/sourceLocation.js`、`lib/studioUpdates.js`、`StudioView`、`StudioEntryEditor`。
- 测试：`test_source_preview`、`test_studio_and_settings`、`test_studio_write_path`、`test_research_review`、`test_review_integrity`、`test_artifact_verification`；前端 `sourceLocation`、`studioUpdates`。

### P6 会话与流程图（!568、!547）

- 后端：`services/agent_sessions.py`、`services/engines/{base,claude,jiuwen}.py`、`ONTO_WEB_SESSION_STALL_TIMEOUT`。
- 前端：`lib/subagentEvents.js`、`ChatPane`、`DrawioView`、`flowchart/*`。
- 测试：`test_session_pool`、`test_claude_engine`、`test_jiuwen_engine`；前端 `subagentEvents`、`drawioConvert`。

### P7 部署与文档（!558、!572/!574/!576 等）

- `.env.example`、`README.md`、`docker-compose.yml`、`deploy/*`、`services/auth.py`（JWT audience 默认值）、`test_auth`；根目录部署要带上本分支新增的容器。

## 验收

1. develop 新增和修改的 onto 测试改到新结构后全部通过；本分支原有测试不退化。
2. 整栈（账号服务、Hub、评测服务、本分支 onto）走通：建模 → 发布到 Hub → 已发布版本 → 去评测 → 完成通知 → 评测优化 → 从 Hub 导入 → 版本对比 → MCP 发布与回退。
