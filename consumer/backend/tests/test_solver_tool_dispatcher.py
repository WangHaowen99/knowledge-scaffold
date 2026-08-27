from __future__ import annotations

import json
from types import SimpleNamespace

from agent.tools.solver import (
    SolverToolDispatcher,
    build_solver_tool_specs,
    configure_solver_tool_service,
    recover_solver_tool_result,
)


def _capability():
    return {
        "package_id": "package-synthetic",
        "package_revision": "revision-synthetic-1",
        "operation_key": "solve_synthetic_route",
        "decision_model_id": "model-synthetic",
        "problem_family": "synthetic_family.v1",
        "input_schema": {
            "type": "object",
            "properties": {"item_count": {"type": "integer"}},
            "required": ["item_count"],
            "additionalProperties": False,
        },
        "result_schema": {"type": "object"},
        "contract_digest": "sha256:" + "a" * 64,
        "validator_revision": "verifier-synthetic-1",
        "enabled": True,
    }


def _context(**overrides):
    values = {
        "workspace_id": "workspace-synthetic",
        "user_id": "owner-synthetic",
        "run_id": "run-synthetic",
        "turn_id": 3,
        "selected_ontology_id": "package-synthetic",
        "selected_ontology_version": "revision-synthetic-1",
        "caller": "main",
        "session_id": "session-synthetic",
        "agent_iteration": 1,
        "tool_batch_index": 1,
        "subagent_lineage": {},
        "observer": None,
        "event_emitter": None,
        "protocol_event_observer": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _Service:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or {
            "status": "completed",
            "run_id": "run-synthetic",
            "job_id": "job-synthetic",
            "attempt_id": "attempt-synthetic",
            "authoritative": True,
            "feasibility": "feasible",
            "optimality": "not_proven",
            "proof_kind": "constraint_recalculation",
            "summary": {"candidate_id": "ALL_0"},
            "result_artifact_ref": "artifact://result-synthetic",
            "verification_evidence_ref": "artifact://evidence-synthetic",
        }

    def start_or_resume(self, **kwargs):
        self.calls.append(kwargs)
        progress = kwargs["progress_callback"]
        progress(
            "solver.progress",
            {
                "run_id": "run-synthetic",
                "job_id": "job-synthetic",
                "attempt_id": "attempt-synthetic",
                "event_seq": 4,
                "phase": "solve",
                "message": "Synthetic progress",
                "problem_ir": {"must": "not leak"},
            },
        )
        progress(
            "solver.result.authoritative",
            {
                "run_id": "run-synthetic",
                "job_id": "job-synthetic",
                "attempt_id": "attempt-synthetic",
                "event_seq": 5,
                "status": "completed",
                "authoritative": True,
                "feasibility": "feasible",
                "optimality": "not_proven",
                "proof_kind": "constraint_recalculation",
                "summary": {"candidate_id": "ALL_0"},
                "result_artifact_ref": "artifact://result-synthetic",
                "verification_evidence_ref": "artifact://evidence-synthetic",
                "problem_ir": {"must": "not leak"},
            },
        )
        return self.result


def test_checkpoint_recovery_reports_pending_link_without_redispatch():
    class _Store:
        def get_run_link(self, **_kwargs):
            return {
                "card_name": "solve_synthetic_aaaaaaaa",
                "job_id": "job-synthetic-pending",
                "tool_result": None,
                "tool_result_digest": None,
            }

    class _PendingService:
        store = _Store()

        def recover_job(self, *_args, **_kwargs):
            return {"status": "waiting_solver", "job_id": "job-synthetic-pending"}

    configure_solver_tool_service(_PendingService())
    try:
        recovered = recover_solver_tool_result(
            workspace_id="workspace-synthetic",
            owner_id="owner-synthetic",
            run_id="run-synthetic",
            turn_id=3,
            tool_call_id="tool-call-synthetic",
            card_name="solve_synthetic_aaaaaaaa",
        )
    finally:
        configure_solver_tool_service(None)

    assert recovered == {
        "state": "pending",
        "link_exists": True,
        "job_id": "job-synthetic-pending",
    }


def test_dispatcher_rechecks_scope_and_passes_locked_identity_to_service(monkeypatch):
    (spec,) = build_solver_tool_specs([_capability()])
    service = _Service()
    cancel_probe = lambda: False
    monkeypatch.setattr(
        "agent.tools.solver._run_cancel_probe",
        lambda run_id: cancel_probe if run_id == "run-synthetic" else None,
    )
    dispatcher = SolverToolDispatcher(
        service=service,
        binding_resolver=lambda **kwargs: spec.binding,
    )

    outcome = dispatcher.dispatch(
        binding=spec.binding,
        arguments={"item_count": 2},
        context=_context(),
        tool_call_id="tool-call-synthetic",
    )

    payload = json.loads(outcome.content)
    assert payload["authoritative"] is True
    assert payload["job_id"] == "job-synthetic"
    assert service.calls[0]["tool_call_id"] == "tool-call-synthetic"
    assert service.calls[0]["card_name"] == spec.name
    assert service.calls[0]["contract_digest"] == "sha256:" + "a" * 64
    assert service.calls[0]["arguments"] == {"item_count": 2}
    assert service.calls[0]["cancel_requested"] is cancel_probe
    assert "endpoint" not in service.calls[0]


def test_authoritative_result_is_terminal_and_sets_deterministic_agent_output():
    (spec,) = build_solver_tool_specs([_capability()])
    service = _Service(
        result={
            "status": "completed",
            "run_id": "run-synthetic",
            "job_id": "job-synthetic",
            "authoritative": True,
            "feasibility": "feasible",
            "optimality": "proven_optimal",
            "proof_kind": "verifier_exhaustive_check",
            "summary": {
                "candidate_id": "OPTION_0_TO_OPTION_1_K0001",
                "text": "Synthetic route plan\nItem 1 -> ROUTE_A",
            },
            "result_artifact_ref": "artifact://solver_result/" + "a" * 64,
            "verification_evidence_ref": (
                "artifact://verification_evidence/" + "b" * 64
            ),
            "candidate_results_artifact_ref": (
                "artifact://candidate_results/" + "c" * 64
            ),
            "candidate_results_artifact_digest": "sha256:" + "c" * 64,
        }
    )

    outcome = SolverToolDispatcher(
        service=service,
        binding_resolver=lambda **_kwargs: spec.binding,
    ).dispatch(
        binding=spec.binding,
        arguments={"item_count": 2},
        context=_context(),
        tool_call_id="tool-call-synthetic",
    )

    assert outcome.terminal is True
    assert outcome.state_updates["final_output"] == (
        "Synthetic route plan\nItem 1 -> ROUTE_A"
    )
    assert outcome.state_updates["results"]["output"] == (
        "Synthetic route plan\nItem 1 -> ROUTE_A"
    )
    assert outcome.state_updates["results"]["solver"]["authoritative"] is True
    payload = json.loads(outcome.content)
    assert payload["candidate_results_artifact_ref"].startswith(
        "artifact://candidate_results/"
    )


def test_solver_failure_is_terminal_and_normalizes_internal_verification_code():
    (spec,) = build_solver_tool_specs([_capability()])
    service = _Service(
        result={
            "status": "error",
            "code": "verification_runtime_failed",
            "phase": "verify",
            "retryable": False,
            "run_id": "run-synthetic",
            "job_id": "job-synthetic",
            "authoritative": False,
        }
    )

    outcome = SolverToolDispatcher(
        service=service,
        binding_resolver=lambda **_kwargs: spec.binding,
    ).dispatch(
        binding=spec.binding,
        arguments={"item_count": 2},
        context=_context(),
        tool_call_id="tool-call-synthetic",
    )

    assert outcome.terminal is True
    assert json.loads(outcome.content)["code"] == "verification_failed"
    assert "验证" in outcome.state_updates["final_output"]
    assert "terminal_error" not in outcome.state_updates


def test_running_solver_result_remains_nonterminal():
    (spec,) = build_solver_tool_specs([_capability()])
    service = _Service(
        result={
            "status": "running",
            "phase": "solve",
            "retryable": False,
            "run_id": "run-synthetic",
            "job_id": "job-synthetic",
            "authoritative": False,
        }
    )

    outcome = SolverToolDispatcher(
        service=service,
        binding_resolver=lambda **_kwargs: spec.binding,
    ).dispatch(
        binding=spec.binding,
        arguments={"item_count": 2},
        context=_context(),
        tool_call_id="tool-call-synthetic",
    )

    assert outcome.terminal is False
    assert "final_output" not in outcome.state_updates


def test_dispatcher_rejects_workspace_selection_and_contract_drift_before_service():
    (spec,) = build_solver_tool_specs([_capability()])
    service = _Service()

    wrong_selection = SolverToolDispatcher(
        service=service,
        binding_resolver=lambda **kwargs: spec.binding,
    ).dispatch(
        binding=spec.binding,
        arguments={"item_count": 2},
        context=_context(selected_ontology_id="another-package"),
        tool_call_id="tool-call-synthetic",
    )
    assert json.loads(wrong_selection.content)["code"] == "solver_contract_drift"

    changed = SimpleNamespace(**{**spec.binding.__dict__, "contract_digest": "sha256:" + "b" * 64})
    drift = SolverToolDispatcher(
        service=service,
        binding_resolver=lambda **kwargs: changed,
    ).dispatch(
        binding=spec.binding,
        arguments={"item_count": 2},
        context=_context(),
        tool_call_id="tool-call-synthetic",
    )
    assert json.loads(drift.content)["code"] == "solver_contract_drift"
    assert service.calls == []


def test_dispatcher_emits_sanitized_solver_and_tool_progress_events():
    (spec,) = build_solver_tool_specs([_capability()])
    service = _Service()
    events = []
    context = _context(
        caller="subagent",
        event_emitter=lambda event_type, payload: events.append((event_type, payload)),
    )

    SolverToolDispatcher(
        service=service,
        binding_resolver=lambda **kwargs: spec.binding,
    ).dispatch(
        binding=spec.binding,
        arguments={"item_count": 2},
        context=context,
        tool_call_id="tool-call-synthetic",
    )

    event_types = [event_type for event_type, _payload in events]
    assert "solver.progress" in event_types
    assert "solver.result.authoritative" in event_types
    assert "tool.progress" in event_types
    for _event_type, payload in events:
        assert payload["run_id"] == "run-synthetic"
        assert payload["job_id"] == "job-synthetic"
        assert payload["attempt_id"] == "attempt-synthetic"
        assert payload["event_seq"] in {4, 5}
        assert "problem_ir" not in payload
    authoritative = next(
        payload
        for event_type, payload in events
        if event_type == "solver.result.authoritative"
    )
    assert authoritative["authoritative"] is True
    assert authoritative["feasibility"] == "feasible"
    assert authoritative["optimality"] == "not_proven"
    assert authoritative["proof_kind"] == "constraint_recalculation"
    assert authoritative["result_artifact_ref"] == "artifact://result-synthetic"


def test_dispatcher_rejects_sensitive_or_internal_service_output():
    (spec,) = build_solver_tool_specs([_capability()])
    service = _Service(
        result={
            "status": "completed",
            "run_id": "run-synthetic",
            "job_id": "job-synthetic",
            "summary": {"token": "credential-synthetic"},
            "internal_path": "C:/private/result.json",
        }
    )

    outcome = SolverToolDispatcher(
        service=service,
        binding_resolver=lambda **kwargs: spec.binding,
    ).dispatch(
        binding=spec.binding,
        arguments={"item_count": 2},
        context=_context(),
        tool_call_id="tool-call-synthetic",
    )

    payload = json.loads(outcome.content)
    assert payload == {
        "status": "error",
        "code": "solver_result_rejected",
        "phase": "publish",
        "retryable": False,
        "run_id": "run-synthetic",
        "job_id": "job-synthetic",
    }
