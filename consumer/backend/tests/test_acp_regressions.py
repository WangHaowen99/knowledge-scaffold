"""Regression tests for ACP protocol adapter behavior."""

from types import SimpleNamespace

from llm import LLMPurpose
from api.protocols.acp import server as acp_server
import utils.lang as lang_module
from api.protocols.acp.models import RunStatus
from api.protocols.acp.server import _claim_acp_run, _map_orch_event_to_acp_sse
from agent.core.orchestrator import _resolve_run_id
from persistence.run_store import RunStore
from persistence.run_store import RunStatus as StoreRunStatus
from utils.lang import _detect_language_via_llm, detect_language, translate_text


def test_acp_stream_does_not_emit_answer_for_token_delta():
    event = {
        "event": "agent.v2",
        "data": {
            "type": "token.delta",
            "payload": {"channel": "content", "text": "hello"},
        },
    }

    mapped = list(_map_orch_event_to_acp_sse(event, "run_1", {}, []))

    assert mapped == [
        ("message_delta", {"type": "message_delta", "text": "hello", "run_id": "run_1"}),
    ]


def test_acp_suppressed_execute_script_does_not_emit_started_or_finished_events():
    tool_map = {}
    started = {
        "event": "agent.v2",
        "data": {
            "type": "tool.started",
            "payload": {
                "tool_call_id": "tc_1",
                "tool_name": "execute_script",
                "input": {"operation_key": "故障历史"},
            },
        },
    }
    finished = {
        "event": "agent.v2",
        "data": {
            "type": "tool.finished",
            "payload": {
                "tool_call_id": "tc_1",
                "tool_name": "execute_script",
                "status": "completed",
                "output_preview": "故障历史结果",
            },
        },
    }

    assert list(_map_orch_event_to_acp_sse(started, "run_1", tool_map, ["故障历史"])) == []
    assert list(_map_orch_event_to_acp_sse(finished, "run_1", tool_map, ["故障历史"])) == []


def test_acp_completed_run_cannot_be_claimed_for_execution():
    entry = {"status": RunStatus.COMPLETED}

    ok, detail, status_code = _claim_acp_run(entry, allowed_statuses={RunStatus.PENDING})

    assert ok is False
    assert detail == "Run is already completed"
    assert status_code == 409
    assert entry["status"] == RunStatus.COMPLETED


def test_acp_pending_run_is_claimed_for_execution():
    entry = {"status": RunStatus.PENDING}

    ok, detail, status_code = _claim_acp_run(entry, allowed_statuses={RunStatus.PENDING})

    assert ok is True
    assert detail is None
    assert status_code is None
    assert entry["status"] == RunStatus.RUNNING


def test_acp_persist_new_pending_run_keeps_store_status_pending(monkeypatch):
    run_store = RunStore()
    monkeypatch.setattr(
        acp_server,
        "_orchestrator",
        SimpleNamespace(run_store=run_store),
    )

    acp_server._persist_to_runstore(
        "run_pending",
        {
            "status": RunStatus.PENDING,
            "workspace_id": "demo",
            "session_id": "sess_1",
            "user_query": "hello",
            "user_context": {},
            "lang": None,
            "user_id": None,
            "messages": [],
            "error": None,
        },
    )

    stored = run_store.get_run("run_pending")
    assert stored is not None
    assert stored.status == StoreRunStatus.PENDING
    assert stored.metadata["source"] == "acp"
    assert stored.metadata["acp_status"] == "pending"


def test_run_store_preserves_existing_acp_metadata_when_orchestrator_reuses_run_id():
    run_store = RunStore()
    run_store.create_run(
        run_id="run_acp_existing",
        user_request="original",
        metadata={
            "source": "acp",
            "acp_workspace_id": "demo",
            "acp_session_id": "sess_1",
        },
    )
    run_store.update_status("run_acp_existing", StoreRunStatus.PENDING)

    run_store.create_run(
        run_id="run_acp_existing",
        user_request="execute request",
        metadata={
            "session_id": "sess_1",
            "workspace_id": "demo",
            "model_profile_id": "default",
        },
    )

    stored = run_store.get_run("run_acp_existing")
    assert stored is not None
    assert stored.user_request == "execute request"
    assert stored.status == StoreRunStatus.RUNNING
    assert stored.metadata["source"] == "acp"
    assert stored.metadata["acp_workspace_id"] == "demo"
    assert stored.metadata["model_profile_id"] == "default"


def test_acp_claim_rejects_second_claim_after_first_claim():
    entry = {"status": RunStatus.PENDING}

    first_ok, _, _ = _claim_acp_run(entry, allowed_statuses={RunStatus.PENDING})
    second_ok, second_detail, second_status = _claim_acp_run(
        entry,
        allowed_statuses={RunStatus.PENDING},
    )

    assert first_ok is True
    assert second_ok is False
    assert second_detail == "Run is already running"
    assert second_status == 409


def test_acp_requested_run_id_can_reuse_existing_acp_run():
    run_store = RunStore()
    run_store.create_run(
        run_id="run_acp_existing",
        user_request="original",
        metadata={"source": "acp"},
    )

    assert (
        _resolve_run_id(
            "run_acp_existing",
            run_store,
            allow_existing_acp_run=True,
        )
        == "run_acp_existing"
    )


def test_non_acp_entrypoint_does_not_reuse_existing_acp_run_id():
    run_store = RunStore()
    run_store.create_run(
        run_id="run_acp_existing",
        user_request="original",
        metadata={"source": "acp"},
    )

    assert _resolve_run_id("run_acp_existing", run_store) != "run_acp_existing"


def test_non_acp_requested_run_id_still_avoids_existing_run_collision():
    run_store = RunStore()
    run_store.create_run(
        run_id="run_existing",
        user_request="original",
        metadata={"source": "agentic"},
    )

    assert _resolve_run_id("run_existing", run_store) != "run_existing"


def test_ascii_non_english_language_is_not_forced_to_english():
    assert detect_language("Hola como estas, necesito revisar el historial") == "es"


def test_short_ascii_english_is_not_misdetected_as_other_language():
    for text in ("hi", "ok", "hello", "test", "yes", "no"):
        assert detect_language(text) == "en"


def test_language_detection_uses_configured_default_for_empty_text():
    assert detect_language("", default_lang="en") == "en"


def test_language_llm_detection_uses_task_llm_router(monkeypatch):
    calls = []

    class _Router:
        def call(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(content="es")

    monkeypatch.setattr(lang_module, "get_llm_router", lambda: _Router(), raising=False)

    assert _detect_language_via_llm("texto mezclado con technical terms") == "es"
    assert calls
    assert calls[0]["purpose"] == LLMPurpose.TASK
    assert calls[0]["stream"] is False
    assert calls[0]["enable_reasoning"] is False
    assert calls[0]["request_label"] == "language_detect"


def test_language_translation_uses_task_llm_router(monkeypatch):
    calls = []

    class _Router:
        def call(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(content="translated")

    monkeypatch.setattr(lang_module, "get_llm_router", lambda: _Router(), raising=False)

    assert translate_text("需要翻译", "en", source_lang="zh") == "translated"
    assert calls
    assert calls[0]["purpose"] == LLMPurpose.TASK
    assert calls[0]["stream"] is False
    assert calls[0]["enable_reasoning"] is False
    assert calls[0]["request_label"] == "language_translate"
