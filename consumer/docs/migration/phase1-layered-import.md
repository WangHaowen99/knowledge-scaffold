# Phase 1 分层结构导入记录

状态：Phase 1 已完成，活动入口按计划尚未切换。

## 范围

来源：`onto-agent/main@31fba9db4bd7103f4d3ab54da8f3031e50cef4d2`

目标起点：`meta-wise` 本地 `develop@1d07c1ee4905b89843b5158e5bbedd91fa3b9537`

Phase 1 已迁入本地 main 的分层路径：

- `consumer/backend/agent/`
- `consumer/backend/api/`
- `consumer/backend/config/*.py`
- `consumer/backend/integration/`
- `consumer/backend/llm/`
- `consumer/backend/ontology/`
- `consumer/backend/persistence/`
- `consumer/backend/utils/`
- `consumer/backend/workspace/`
- `consumer/backend/app.py`
- 20 个目标原先不存在的本地 main 测试文件
- requirements 两边依赖并集
- tool registry、Prompt、SSE、A2A、ACP、Web Search、orchestrator 共享契约
- `skill_parser` 当前 schema 与远端 legacy `action/comment` 输入兼容

导入使用 non-overwrite 规则。冻结目标已有的 `agentic/`、auth、Jiuwen、Graph、MCP、Trace、Redesign 文件没有删除或覆盖。

## 当前活动入口

`consumer/backend/run.py` 与 `wsgi.py` 仍导入 `agentic.create_app`，因此本批不会改变冻结目标当前运行行为。新增 `app.py` 只用于建立新结构和测试依赖；切换入口前必须完成 remote auth、Jiuwen、Graph/MCP 与路由并集。

## 集成修正

固定源码快照在干净环境中发现并复现两个独立 import 循环，本批在目标结构中修正：

1. `agent.skill_evolution` 与 `api.routes` 相互通过包入口导入。`api.routes` 改为直接从 route module 和 registry 导入 blueprint/worker。
2. `integration.platform` 通过 observability 包入口回到 platform manager。platform event/exporter 改为导入 observability event module，provider 对 platform manager 使用延迟导入。

测试 fixture 同时隔离 `agentic.config.system_config` 与新 `config.system_config`，并把 `PROJECT_ROOT` 指向每个测试的临时目录。`test_agent_loop.py` 使用 context-local workspace，不创建会污染 workspace 枚举的实体空间。

## 共享契约处理

- `test_tool_registry.py` 保留 source 56 个节点，追加远端 5 个 Phase 1 节点；Jiuwen `ToolInvocation` 两项延后 Phase 3，v0.1 package 导入节点延后 Phase 4。
- `test_orchestrator_runtime.py` 使用 source 26 个节点。Jiuwen factory 节点延后 Phase 3；旧 `InputTokenLimitError` 与 OpenViking memory hook 节点按目标架构退役，去向见 `test-migration-map.md`。
- `test_skill_ontology_parser.py` 保留 source 17 个节点，并追加远端 4 个 `action/comment` 输入节点。运行时输出只保留当前 `scripts/assets/graph` schema。
- 动态 skill schema 在 registry 明确为空时隐藏；workspace 尚未建立、无法判定 registry 状态时保留基础 schema。

## 已执行验证

- 新分层 Python 文件 `compileall`：通过。
- `agent/api/config/integration/llm/ontology/persistence/utils/workspace/app` import smoke：通过。
- 独立 `agent.skill_evolution`、`integration.platform`、`api.routes`、`app` import：通过。
- 新增的 20 个测试文件：`203 passed`。
- 当前目标的 tool/prompt/stream/session 回归：`93 passed, 2 skipped`；两个 skip 均为既有可选 ontology doc fixture。
- import 循环相关回归：`75 passed`。
- Phase 1 去重后的 13 文件总门禁：`198 passed, 2 skipped`；两个 skip 指向同一个未提交的可选 ontology doc fixture。
- parser 与 architecture 并集：`23 passed`。
- 冻结目标 remote auth、register proxy、workspace authz 保护网：`47 passed`。
- source/target SDK 目录逐文件一致；`.onto-runtime` 约定一致，目标仅额外保留运行日志目录。

## 后续工作

Phase 2 起继续按能力边界迁移剩余分叉文件：

- 接入独立 auth-server、workspace owner/member 与新存储隔离。
- Phase 3 将 Jiuwen 迁入 `agent/core`，并统一 tool invocation 边界。
- Phase 4 迁入 Graph、DataAgent 远端增强和 v0.1 package import normalization。
- Phase 5-6 迁入 MCP、Trace/W3C。
- 上述能力全部进入新路径并通过保护网后，再把 `run.py`、`wsgi.py` 切到 `app.create_app`。
- 切换前持续运行冻结目标的 32/47 认证保护网。
