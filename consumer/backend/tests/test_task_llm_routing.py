# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Tests for lightweight task LLM configuration and routing."""

import threading
from types import SimpleNamespace

import api.routes as routes_module
from api.testing import TestApplication
from auth_test_backend import AuthManager
from backend_synthetic_credentials import AUTH_SIGNING_SECRET, BOOTSTRAP_ADMIN_PASSWORD
from config.settings import AppConfig, LLMConfig
from fastapi.testclient import TestClient
from llm import LLMPurpose, LLMService, configure_llm_router, reset_llm_router_for_tests
from workspace.user_store_manager import UserStoreManager
from workspace.workspaces import create_workspace
from internal_access import internal


class _FakeStreamingResponse:
    def __init__(self, lines):
        self._lines = lines
        self.encoding = "utf-8"

    def iter_lines(self, decode_unicode=True):
        return iter(self._lines)

    # pylint: disable-next=add-staticmethod-or-classmethod-decorator
    def close(self) -> None:
        return


class _InlinePool:
    async def run(self, function, *args, **kwargs):
        return function(*args, **kwargs)


class _RecordingLLM:
    def __init__(self, model: str, response_text: str) -> None:
        self.config = SimpleNamespace(model=model, api_key=None)
        self.calls = []
        self.response_text = response_text

    def call(self, messages, **kwargs):
        self.calls.append(
            {
                "messages": messages,
                "kwargs": kwargs,
            }
        )
        return SimpleNamespace(content=self.response_text)


class _RecordingRouter:
    def __init__(self, *, main_llm, task_llm) -> None:
        self.main_llm = main_llm
        self.task_llm = task_llm

    def call(self, *, purpose, messages, profile_id=None, **kwargs):
        del profile_id
        if purpose == LLMPurpose.TASK:
            return self.task_llm.call(messages, **kwargs)
        return self.main_llm.call(messages, **kwargs)


def test_task_llm_falls_back_to_main_when_unset(tmp_path, monkeypatch):
    import config.system_config as system_config_module

    for key in (
        "LLM_BASE_URL",
        "LLM_MODEL",
        "LLM_API_KEY",
        "LLM_TIMEOUT",
        "LLM_MAX_TOKENS",
        "LLM_TEMPERATURE",
        "TASK_LLM_BASE_URL",
        "TASK_LLM_MODEL",
        "TASK_LLM_API_KEY",
        "TASK_LLM_TIMEOUT",
        "TASK_LLM_MAX_TOKENS",
        "TASK_LLM_TEMPERATURE",
    ):
        monkeypatch.delenv(key, raising=False)

    isolated_config_dir = tmp_path / "config"
    isolated_config_dir.mkdir(parents=True, exist_ok=True)
    isolated_config_path = isolated_config_dir / "system_config.json"
    monkeypatch.setattr(system_config_module, "CONFIG_DIR", isolated_config_dir)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", isolated_config_path)

    monkeypatch.setenv("LLM_BASE_URL", "https://main.example.net/v1")
    monkeypatch.setenv("LLM_MODEL", "main-model")
    monkeypatch.setenv("LLM_API_KEY", "main-key")
    monkeypatch.setenv("LLM_TIMEOUT", "123")
    monkeypatch.setenv("LLM_MAX_TOKENS", "2048")
    monkeypatch.setenv("LLM_TEMPERATURE", "0.4")

    config = AppConfig.from_env()

    assert config.llm.model == "main-model"
    assert config.task_llm.base_url == "https://main.example.net/v1"
    assert config.task_llm.model == "main-model"
    assert config.task_llm.api_key == "main-key"
    assert config.task_llm.timeout == 123


def test_generate_title_uses_task_llm(tmp_path, monkeypatch):
    monkeypatch.setattr(routes_module, "_orchestrator", None)
    monkeypatch.setattr(routes_module, "_user_store_manager", None)
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))

    create_workspace("Demo Workspace", workspace_id="demo", set_as_default=True)

    app = TestApplication()
    app.state.services["TESTING"] = True

    auth_manager = AuthManager(str(tmp_path / "users.json"), AUTH_SIGNING_SECRET)
    user = auth_manager.authenticate("admin", BOOTSTRAP_ADMIN_PASSWORD)
    assert user is not None
    user = auth_manager.change_password("admin", BOOTSTRAP_ADMIN_PASSWORD, "RootStrong123!")
    app.state.services["AUTH_MANAGER"] = auth_manager
    runtime = SimpleNamespace(short_io=_InlinePool(), external_io=_InlinePool())
    app.state.runtime_container = runtime
    app.state.agent_execution_runtime = runtime
    monkeypatch.setattr(routes_module, "_agent_execution_runtime", runtime)

    user_store_manager = UserStoreManager(
        str(tmp_path / "chats"),
        str(tmp_path / "executions"),
    )
    main_llm = _RecordingLLM("main-model", "不该走这里")
    task_llm = _RecordingLLM("task-model", "排产优化")
    configure_llm_router(_RecordingRouter(main_llm=main_llm, task_llm=task_llm))

    routes_module.register_routes(
        app,
        user_store_manager=user_store_manager,
    )

    chat_store = user_store_manager.get_chat_store(user.username, "demo")
    chat_store.create_session(session_id="session-1")

    client = TestClient(app)
    token = auth_manager.issue_token(user)
    response = client.post(
        "/agentic/sessions/session-1/generate-title",
        json={"message": "请帮我分析当前产线排产如何优化"},
        headers={"Authorization": "Bearer {}".format(token)},
    )

    assert response.status_code == 200
    assert response.json()["title"] == "排产优化"
    assert len(main_llm.calls) == 0
    assert len(task_llm.calls) == 1
    assert chat_store.get_session("session-1").title == "排产优化"
    reset_llm_router_for_tests()


def test_reasoning_summary_uses_task_llm_when_configured(monkeypatch):
    main_llm = LLMService(
        LLMConfig(
            base_url="https://main.example.net/v1",
            model="main-model",
            api_key=None,
            timeout=30,
            max_tokens=512,
            temperature=0.0,
        )
    )
    task_llm = main_llm.with_config(
        LLMConfig(
            base_url="https://task.example.net/v1",
            model="task-model",
            api_key=None,
            timeout=30,
            max_tokens=256,
            temperature=0.0,
        )
    )
    main_llm.summary_llm = task_llm

    monkeypatch.setattr(
        main_llm,
        "call",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("primary llm should not summarize reasoning")),
    )

    task_calls = []

    def fake_task_call(messages, **kwargs):
        task_calls.append(
            {
                "messages": messages,
                "kwargs": kwargs,
            }
        )
        return SimpleNamespace(content="准备检索资料")

    monkeypatch.setattr(task_llm, "call", fake_task_call)

    response = _FakeStreamingResponse(
        [
            (
                "data:"
                ' {"model":"main-model","choices":[{"delta":{"reasoning_content":"先确定范围"},"finish_reason":null}]}'
            ),
            'data: {"model":"main-model","choices":[{"delta":{"content":"done"},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ]
    )

    summaries = []
    summary_ready = threading.Event()
    internal(main_llm).collect_stream_response(
        response,
        reasoning_summary_callback=lambda block_id, summary: (
            summaries.append((block_id, summary)),
            summary_ready.set(),
        ),
    )

    assert summary_ready.wait(timeout=1.0)
    assert len(task_calls) == 1
    assert task_calls[0]["kwargs"]["enable_reasoning"] is False
    assert len(summaries) == 1
    assert summaries[0][0].startswith("reasoning_")
    assert summaries[0][1] == "准备检索资料"


def test_reasoning_summary_falls_back_to_primary_llm(monkeypatch):
    llm = LLMService(
        LLMConfig(
            base_url="https://main.example.net/v1",
            model="main-model",
            api_key=None,
            timeout=30,
            max_tokens=512,
            temperature=0.0,
        )
    )

    calls = []

    def fake_call(messages, **kwargs):
        calls.append(
            {
                "messages": messages,
                "kwargs": kwargs,
            }
        )
        return SimpleNamespace(content="继续读取文件")

    monkeypatch.setattr(llm, "call", fake_call)

    response = _FakeStreamingResponse(
        [
            'data: {"model":"main-model","choices":[{"delta":{"reasoning_content":"先看目录"},"finish_reason":null}]}',
            'data: {"model":"main-model","choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ]
    )

    summaries = []
    summary_ready = threading.Event()
    internal(llm).collect_stream_response(
        response,
        reasoning_summary_callback=lambda block_id, summary: (
            summaries.append((block_id, summary)),
            summary_ready.set(),
        ),
    )

    assert summary_ready.wait(timeout=1.0)
    assert len(calls) == 1
    assert len(summaries) == 1
    assert summaries[0][1] == "继续读取文件"
