# openJiuwen 0.1.17 Upgrade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade the Consumer backend from `openjiuwen==0.1.14` to `0.1.17` while preserving its current public behavior and keeping all new optional openJiuwen capabilities disabled.

**Architecture:** Keep `consumer/backend/agent/core/jiuwen_engine.py` as the sole compatibility boundary. The platform remains authoritative for sessions, tool policy, observability, memory, and public stream protocols; the adapter explicitly neutralizes changed 0.1.17 defaults without introducing a second runtime path.

**Tech Stack:** Python 3.11/3.12, openJiuwen 0.1.17, Pydantic 2, pytest, FastAPI, FastMCP 2.x.

**Delivery boundary:** This plan produces local worktree changes and verification evidence only. Do not stage, commit, push, or create a PR unless the user explicitly requests it.

---

### Task 1: Pin the dependency contract to openJiuwen 0.1.17

**Files:**
- Modify: `consumer/backend/tests/test_asgi_deployment_contract.py`
- Modify: `consumer/backend/requirements.txt`

- [ ] **Step 1: Write the failing dependency-contract test**

Add this test to `consumer/backend/tests/test_asgi_deployment_contract.py`:

```python
from packaging.requirements import Requirement


def test_backend_pins_plain_openjiuwen_0_1_17_without_feature_extras():
    requirements = (BACKEND_ROOT / "requirements.txt").read_text(encoding="utf-8")
    active_requirement_lines = [
        line.strip()
        for line in requirements.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    normalized_lines = {line.lower() for line in active_requirement_lines}
    parsed_requirements = [Requirement(line) for line in active_requirement_lines]
    openjiuwen_requirements = [
        requirement
        for requirement in parsed_requirements
        if requirement.name.lower() == "openjiuwen"
    ]

    assert "openjiuwen==0.1.17" in normalized_lines
    assert len(openjiuwen_requirements) == 1
    assert not openjiuwen_requirements[0].extras
    assert "fastmcp>=2.14.2,<3.0" in normalized_lines
```

- [ ] **Step 2: Run the test and verify RED**

Run from `consumer/`:

```powershell
$env:PYTHONPATH = "backend"
backend\venv\Scripts\python.exe -m pytest backend/tests/test_asgi_deployment_contract.py::test_backend_pins_plain_openjiuwen_0_1_17_without_feature_extras -q
```

Expected: FAIL because the requirements file still pins `openjiuwen==0.1.14`.

- [ ] **Step 3: Apply the minimal dependency change**

Change the pin and its FastMCP comment in `consumer/backend/requirements.txt`:

```text
openjiuwen==0.1.17
...
# openjiuwen 0.1.17 constrains FastMCP to <3.0.
# FastMCP 3 remains outside this compatibility-only upgrade.
fastmcp>=2.14.2,<3.0
```

Do not install `openjiuwen[observability]`, `openjiuwen[jiuwen-memory]`, or `openjiuwen[all]`.

- [ ] **Step 4: Run the dependency-contract test and verify GREEN**

Run the command from Step 2. Expected: PASS.

### Task 2: Make the 0.1.17 runtime defaults explicit

**Files:**
- Modify: `consumer/backend/tests/test_jiuwen_react_adapter.py`
- Modify: `consumer/backend/agent/core/jiuwen_engine.py`

- [ ] **Step 1: Change the adapter test to express the 0.1.17 contract**

Replace the old KV-cache assertion in `test_factory_passes_explicit_context_engine_config` with:

```python
    assert "enable_kv_cache_release" not in cec.kwargs
    assert created["react_agent_config"]["kv_cache_affinity_config"] == {
        "enable_kv_cache_release": False,
        "enable_kv_cache_affinity": False,
    }
    assert created["react_agent_config"]["context_processors"] == []
    assert created["react_agent_config"]["parallel_tool_calls"] is True
```

- [ ] **Step 2: Run the adapter test and verify RED**

Run from `consumer/`:

```powershell
$env:PYTHONPATH = "backend"
backend\venv\Scripts\python.exe -m pytest backend/tests/test_jiuwen_react_adapter.py::test_factory_passes_explicit_context_engine_config -q
```

Expected: FAIL because the old ContextEngine argument is still present and the new ReAct fields are not passed explicitly.

- [ ] **Step 3: Implement the minimal 0.1.17 adapter change**

Build `ContextEngineConfig` without the removed field:

```python
    context_engine_config = context_engine_config_cls(
        max_context_message_num=None,
        default_window_round_num=_normalize_context_max_rounds(context_max_rounds),
        enable_reload=False,
    )
```

Add these explicit fields to `ReActAgentConfig`:

```python
        kv_cache_affinity_config={
            "enable_kv_cache_release": False,
            "enable_kv_cache_affinity": False,
        },
        context_processors=[],
        parallel_tool_calls=True,
```

This keeps KV-cache release, KV-cache affinity, and ContextEngine processors disabled while pinning the current ReAct parallel-tool behavior. The platform remains authoritative for tool policy and authorization.

- [ ] **Step 4: Run the adapter test and verify GREEN**

Run the command from Step 2. Expected: PASS.

- [ ] **Step 5: Run the focused fake-module adapter suite**

```powershell
$env:PYTHONPATH = "backend"
backend\venv\Scripts\python.exe -m pytest backend/tests/test_jiuwen_react_adapter.py -q
```

Expected: all tests PASS.

### Task 3: Move the real-package ContextEngine regression baseline to 0.1.17

**Files:**
- Modify: `consumer/backend/tests/test_context_engine_model_window.py`

- [ ] **Step 1: Update the real-package version assertion before upgrading the environment**

Change the module description and version assertion from 0.1.14 to 0.1.17:

```python
def test_phase_a_uses_requirements_pinned_openjiuwen_version():
    assert importlib.metadata.version("openjiuwen") == "0.1.17"
```

Remove `enable_kv_cache_release=False` from the `ContextEngineConfig` constructed in `_capture_real_react_model_window`.

- [ ] **Step 2: Run the version test against the 0.1.14 baseline and verify RED**

```powershell
$env:PYTHONPATH = "backend"
backend\venv\Scripts\python.exe -m pytest backend/tests/test_context_engine_model_window.py::test_phase_a_uses_requirements_pinned_openjiuwen_version -q
```

Expected: FAIL with installed version `0.1.14`.

- [ ] **Step 3: Upgrade the isolated environment from requirements**

```powershell
backend\venv\Scripts\python.exe -m pip install --timeout 30 --retries 2 -i https://pypi.tuna.tsinghua.edu.cn/simple -r backend/requirements.txt
backend\venv\Scripts\python.exe -m pip check
```

Expected: install succeeds, `openjiuwen 0.1.17` is present, and `pip check` reports no broken requirements.

- [ ] **Step 4: Add a real-package optional-capability contract test**

Add this test to `consumer/backend/tests/test_context_engine_model_window.py`:

```python
def test_default_factory_configures_openjiuwen_0_1_17_runtime_features():
    from types import SimpleNamespace

    from agent.core.jiuwen_engine import create_default_jiuwen_agent

    agent = create_default_jiuwen_agent(
        llm_config=SimpleNamespace(
            provider="openai_compatible",
            api_key="synthetic-key",
            base_url="https://llm.example.com/v1",
            model="model-a",
            timeout=30,
            max_tokens=128,
            temperature=0.2,
        ),
        max_iterations=1,
        context_max_rounds=3,
    )

    config = agent.config
    assert config.context_processors == []
    assert config.parallel_tool_calls is True
    assert config.kv_cache_affinity_config.enable_kv_cache_release is False
    assert config.kv_cache_affinity_config.enable_kv_cache_affinity is False
```

- [ ] **Step 5: Run the complete real ContextEngine suite and verify GREEN**

```powershell
$env:PYTHONPATH = "backend"
backend\venv\Scripts\python.exe -m pytest backend/tests/test_context_engine_model_window.py -q
```

Expected: all invoke, stream, tool-pairing, ask-user, and large-round tests PASS with the real 0.1.17 package.

### Task 4: Refresh the maintained ContextEngine cutover documentation

**Files:**
- Modify: `consumer/docs/openjiuwen-context-engine-final-cutover-plan.md`

- [ ] **Step 1: Update only active version-dependent statements**

Make these focused replacements:

```text
openJiuwen 0.1.14 -> openJiuwen 0.1.17
```

Change the active factory configuration example to:

```python
ContextEngineConfig(
    max_context_message_num=None,
    default_window_round_num=effective_rounds,
    enable_reload=False,
)
```

Add the matching `ReActAgentConfig` compatibility example after it:

```python
ReActAgentConfig(
    ...,
    context_engine_config=context_engine_config,
    kv_cache_affinity_config={
        "enable_kv_cache_release": False,
        "enable_kv_cache_affinity": False,
    },
    context_processors=[],
    parallel_tool_calls=True,
)
```

Describe this as disabling the openJiuwen KV cache release/affinity integration and new ContextEngine processors during the 0.1.17 compatibility upgrade. Do not claim that general inference-service prefix caching is disabled.

- [ ] **Step 2: Check for stale active-version references**

```powershell
rg -n "0\.1\.14" consumer/docs/openjiuwen-context-engine-final-cutover-plan.md
rg -n -C 5 "ContextEngineConfig|enable_kv_cache_release" consumer/docs/openjiuwen-context-engine-final-cutover-plan.md
```

Expected: no active 0.1.14 reference remains, and `enable_kv_cache_release` appears only in the `ReActAgentConfig.kv_cache_affinity_config` example, never inside `ContextEngineConfig`. Historical changelogs outside this scope may retain old versions.

### Task 5: Run the first-stage regression gates

**Files:**
- Verify only; change production code only after adding a failing regression test for a reproduced incompatibility.

- [ ] **Step 1: Verify installed dependency metadata**

```powershell
backend\venv\Scripts\python.exe -c "import importlib.metadata as m; print(m.version('openjiuwen'))"
backend\venv\Scripts\python.exe -m pip check
```

Expected: `0.1.17` and no broken requirements.

- [ ] **Step 2: Run Jiuwen compatibility and lifecycle suites**

```powershell
$env:PYTHONPATH = "backend"
backend\venv\Scripts\python.exe -m pytest `
  backend/tests/test_context_engine_model_window.py `
  backend/tests/test_context_engine_cutover_probe.py `
  backend/tests/test_jiuwen_react_adapter.py `
  backend/tests/test_jiuwen_async_engine.py `
  backend/tests/test_jiuwen_concurrency_scheduling.py `
  backend/tests/test_jiuwen_runner_lifecycle.py `
  backend/tests/test_jiuwen_runtime_cleanup.py `
  backend/tests/test_jiuwen_runtime_events.py `
  backend/tests/test_jiuwen_runtime_regression.py `
  backend/tests/test_jiuwen_smoke_cleanup.py `
  -q
```

Expected: all selected tests PASS.

- [ ] **Step 3: Run platform-boundary regression suites**

```powershell
$env:PYTHONPATH = "backend"
backend\venv\Scripts\python.exe -m pytest `
  backend/tests/test_execution_store_cancel_turn.py `
  backend/tests/test_stream_protocol_v2.py `
  backend/tests/test_observability_architecture.py `
  backend/tests/test_mcp_transport_mounting.py `
  backend/tests/test_asgi_deployment_contract.py `
  -q
```

Expected on Linux CI: all selected tests PASS with no duplicate observability or protocol regressions. On Windows, first compare any directory `os.open`/`fsync` failure against a clean `origin/develop`; the current develop baseline has four such failures in `test_observability_architecture.py`.

- [ ] **Step 4: Review the final local diff and worktree state**

```powershell
git diff --check
git diff --stat
git status --short --branch
```

Expected: only the dependency pin, compatibility adapter, focused tests, maintained cutover document, and this implementation plan are modified. Nothing is staged or committed.
