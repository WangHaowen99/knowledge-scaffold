# Phase 1 分层结构导入记录

状态：第一批已实现，活动入口尚未切换。

## 范围

来源：`onto-agent/main@31fba9db4bd7103f4d3ab54da8f3031e50cef4d2`

目标起点：`meta-wise` 本地 `develop@1d07c1ee4905b89843b5158e5bbedd91fa3b9537`

本批只新增本地 main 的分层路径：

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

导入使用 non-overwrite 规则。冻结目标已有的 `agentic/`、auth、Jiuwen、Graph、MCP、Trace、Redesign 文件没有删除或覆盖。

## 当前活动入口

`consumer/backend/run.py` 与 `wsgi.py` 仍导入 `agentic.create_app`，因此本批不会改变冻结目标当前运行行为。新增 `app.py` 只用于建立新结构和测试依赖；切换入口前必须完成 remote auth、Jiuwen、Graph/MCP 与路由并集。

## 集成修正

固定源码快照在干净环境中发现并复现两个独立 import 循环，本批在目标结构中修正：

1. `agent.skill_evolution` 与 `api.routes` 相互通过包入口导入。`api.routes` 改为直接从 route module 和 registry 导入 blueprint/worker。
2. `integration.platform` 通过 observability 包入口回到 platform manager。platform event/exporter 改为导入 observability event module，provider 对 platform manager 使用延迟导入。

测试 fixture 同时隔离 `agentic.config.system_config` 与新 `config.system_config`，并把 `PROJECT_ROOT` 指向每个测试的临时目录。`test_agent_loop.py` 使用 context-local workspace，不创建会污染 workspace 枚举的实体空间。

## 已执行验证

- 新分层 Python 文件 `compileall`：通过。
- `agent/api/config/integration/llm/ontology/persistence/utils/workspace/app` import smoke：通过。
- 独立 `agent.skill_evolution`、`integration.platform`、`api.routes`、`app` import：通过。
- 新增的 20 个测试文件：`203 passed`。
- 当前目标的 tool/prompt/stream/session 回归：`93 passed, 2 skipped`；两个 skip 均为既有可选 ontology doc fixture。
- import 循环相关回归：`75 passed`。

## 后续工作

本批不标记 M1-1 完成。后续仍需：

- 合并 65 个同路径分叉文件及 9 个同名分叉测试。
- `skill_parser` 按本地最新语义整组迁入，并在 Phase 4 恢复 v0.1 import normalization。
- requirements 取两边并集。
- 接入独立 auth-server 与 Jiuwen 后，再把 `run.py`、`wsgi.py` 切到新 `app.create_app`。
- 切换前持续运行冻结目标的 32/47 认证保护网。
