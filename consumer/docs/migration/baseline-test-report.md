# 冻结基线测试报告

基线：本地 `develop@1d07c1ee4905b89843b5158e5bbedd91fa3b9537`

执行日期：2026-07-13

## auth-server

命令：

```bash
cd auth-server
/Users/example/vscodePo/onto-agent/venv/bin/python -m pytest tests -q
```

结果：`32 passed in 15.29s`

## consumer 认证与 workspace 权限

命令：

```bash
cd consumer
env PYTHONPATH=backend /Users/example/vscodePo/onto-agent/venv/bin/python -m pytest \
  backend/tests/test_remote_auth.py \
  backend/tests/test_auth_register_proxy.py \
  backend/tests/test_workspace_authz.py -q
```

结果：`47 passed in 31.26s`

首次在受限沙箱内执行时，12 个用例因不能绑定 `127.0.0.1` 临时端口而报 `PermissionError`，另 34 个用例已通过。允许本机回环监听后使用同一源码和同一命令重跑，47 个用例全部通过；该现象属于执行权限限制，不是产品失败。

## 结论

迁移方案声明的两组远端认证基线均已复现：

- auth-server：32/32
- consumer auth/workspace：47/47

后续结构迁移不得删除、跳过或弱化这些场景。

## 本地 main 核心运行测试

在 detached 源码快照 `/tmp/onto-agent-main-source-31fba9db` 中执行：

```bash
env PYTHONPATH=backend /Users/example/vscodePo/onto-agent/venv/bin/python -m pytest \
  backend/tests/test_tool_registry.py \
  backend/tests/test_universal_prompt.py \
  backend/tests/test_routes_stream.py \
  backend/tests/test_session_routes.py -q -rs
```

结果：`87 passed, 1 skipped in 7.55s`。

唯一 skip 为 `test_universal_prompt.py:156`，原因是可选 ontology doc fixture 不存在。该 skip 已在固定源码基线中存在，不是迁移引入，也不用于隐藏迁移失败。

本地 main 的 Phase 1 运行能力测试也已在同一 detached 快照复现：

```bash
env PYTHONPATH=backend /Users/example/vscodePo/onto-agent/venv/bin/python -m pytest \
  backend/tests/test_run_admission_and_replay.py \
  backend/tests/test_stream_protocol_v2.py \
  backend/tests/test_session_routes.py \
  backend/tests/test_orchestrator_runtime.py \
  backend/tests/test_tool_registry.py \
  backend/tests/test_a2a_routes.py \
  backend/tests/test_acp_regressions.py \
  backend/tests/test_platform_connector.py \
  backend/tests/test_web_search_tool.py \
  backend/tests/test_skill_ontology_parser.py \
  backend/tests/test_storage_identifier_security.py -q -rs
```

结果：`162 passed in 7.96s`。
