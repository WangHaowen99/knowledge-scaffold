import hashlib
import json
import os
import stat
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent.direct_execution.service as service_module

from agent.direct_execution.contracts import (
    AwaitingInput,
    Completed,
    DeterministicRequest,
    Failed,
    NotApplicable,
)
from agent.direct_execution.audit import canonical_sha256
from agent.direct_execution.models import RuntimeContext
from agent.direct_execution.service import DeterministicExecutionService
from ontology.operation_execution import validate_and_lock_deterministic_operations
from shared_capabilities.business_data import AuthSpec, EffectiveConnection


def _write_package(root: Path, *, parser=False, data_access="none") -> Path:
    ontology_dir = root / "ontology"
    (ontology_dir / "operations").mkdir(parents=True)
    (ontology_dir / "scripts").mkdir()
    (ontology_dir / "schemas").mkdir()
    parser_line = "  parser: scripts/query_parser.py\n" if parser else ""
    (ontology_dir / "operations" / "query.md").write_text(
        "---\n"
        "resource: operation\n"
        "key: query\n"
        "aliases:\n"
        "  - Query Exact\n"
        "execution:\n"
        "  type: deterministic\n"
        "{}"
        "  input_schema: schemas/query.input.json\n"
        "  output_schema: schemas/query.output.json\n"
        "---\n".format(parser_line),
        encoding="utf-8",
    )
    (ontology_dir / "operations" / "legacy.md").write_text(
        "---\nresource: operation\nkey: legacy\n---\n",
        encoding="utf-8",
    )
    (ontology_dir / "scripts" / "query.md").write_text(
        "---\n"
        "resource: script\n"
        "key: query_handler\n"
        "bound_operation: query\n"
        "entrypoint: query.py\n"
        "data_access: {}\n"
        "---\n\n"
        "| 参数 key | CLI 参数 | 类型 | 必填 |\n"
        "| --- | --- | --- | --- |\n"
        "| code | --code | string | 是 |\n".format(data_access),
        encoding="utf-8",
    )
    (ontology_dir / "scripts" / "query.py").write_text(
        "import argparse\n"
        "import json\n"
        "from query_helper import normalize_code\n"
        "parser = argparse.ArgumentParser()\n"
        "parser.add_argument('--code', default='ok')\n"
        "args = parser.parse_args()\n"
        "print(json.dumps({'value': normalize_code(args.code)}))\n",
        encoding="utf-8",
    )
    (ontology_dir / "scripts" / "query_helper.py").write_text(
        "def normalize_code(value):\n"
        "    return str(value)\n",
        encoding="utf-8",
    )
    if parser:
        (ontology_dir / "scripts" / "query_parser.py").write_text(
            "import json\n"
            "print(json.dumps({'matched': True, 'arguments': {}, 'issues': []}))\n",
            encoding="utf-8",
        )
    (ontology_dir / "schemas" / "query.input.json").write_text(
        json.dumps(
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["code"],
                "properties": {
                    "code": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Provide code",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (ontology_dir / "schemas" / "query.output.json").write_text(
        json.dumps(
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["value"],
                "properties": {"value": {"type": "string"}},
            }
        ),
        encoding="utf-8",
    )
    validate_and_lock_deterministic_operations(ontology_dir)
    return ontology_dir


def _add_runtime_session_parameter(
    ontology_dir: Path,
    *,
    declare_in_input_schema: bool = False,
) -> None:
    descriptor_path = ontology_dir / "scripts" / "query.md"
    descriptor = descriptor_path.read_text(encoding="utf-8")
    descriptor = descriptor.replace(
        "| 参数 key | CLI 参数 | 类型 | 必填 |\n"
        "| --- | --- | --- | --- |\n"
        "| code | --code | string | 是 |\n",
        "| 参数 key | CLI 参数 | 类型 | 必填 | runtime |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| code | --code | string | 是 | 否 |\n"
        "| session_id | --session-id | string | 是 | 是 |\n",
    )
    descriptor_path.write_text(descriptor, encoding="utf-8")
    handler_path = ontology_dir / "scripts" / "query.py"
    handler_path.write_text(
        handler_path.read_text(encoding="utf-8").replace(
            "parser.add_argument('--code', default='ok')\n",
            "parser.add_argument('--code', default='ok')\n"
            "parser.add_argument('--session-id', required=True)\n",
        ),
        encoding="utf-8",
    )
    if declare_in_input_schema:
        schema_path = ontology_dir / "schemas" / "query.input.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        schema["properties"]["session_id"] = {"type": "string"}
        schema["required"].append("session_id")
        schema_path.write_text(json.dumps(schema), encoding="utf-8")
    validate_and_lock_deterministic_operations(ontology_dir)


def _add_aliased_runtime_session_parameter(ontology_dir: Path) -> None:
    descriptor_path = ontology_dir / "scripts" / "query.md"
    descriptor = descriptor_path.read_text(encoding="utf-8")
    descriptor = descriptor.replace(
        "| 参数 key | CLI 参数 | 类型 | 必填 |\n"
        "| --- | --- | --- | --- |\n"
        "| code | --code | string | 是 |\n",
        "| 参数 key | CLI 参数 | 类型 | 必填 | placeholder |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| code | --code | string | 是 | |\n"
        "| sid | --sid | string | 是 | <runtime.session_id> |\n",
    )
    descriptor_path.write_text(descriptor, encoding="utf-8")
    handler_path = ontology_dir / "scripts" / "query.py"
    handler_path.write_text(
        handler_path.read_text(encoding="utf-8").replace(
            "parser.add_argument('--code', default='ok')\n",
            "parser.add_argument('--code', default='ok')\n"
            "parser.add_argument('--sid', required=True)\n",
        ),
        encoding="utf-8",
    )
    validate_and_lock_deterministic_operations(ontology_dir)


def _request(
    *,
    key="query",
    arguments=None,
    checkpoint=None,
    response=None,
    user_request="Query Exact",
    context=None,
):
    request_context = dict(context or {})
    if key is not None:
        request_context["requested_operation_key"] = key
    return DeterministicRequest(
        user_request=user_request,
        context=request_context,
        arguments=dict(arguments or {}),
        runtime_context=RuntimeContext(
            session_id="session-1",
            run_id="run-1",
            turn_id=1,
            user_id="user-1",
            workspace_id="workspace-1",
            selected_ontology_id="test-ontology",
            selected_ontology_version="1.0.0",
        ),
        checkpoint=checkpoint,
        interaction_response=response,
    )


def _patch_manifest(monkeypatch, ontology_dir):
    from agent.direct_execution import service as service_module

    monkeypatch.setattr(
        service_module,
        "get_ontology_manifest",
        lambda *_args, **_kwargs: (
            SimpleNamespace(
                id="test-ontology",
                version="1.0.0",
                ontology_dir=str(ontology_dir),
            ),
            None,
        ),
    )
    return service_module


def _allow_local_test_runner(monkeypatch, service_module):
    """Exercise a fixture script without weakening the production caller."""
    from agent.tools.execute_script import handle_execute_script_path as local_runner

    def execute(**kwargs):
        return local_runner(**{**kwargs, "sandbox_required": False})

    monkeypatch.setattr(service_module, "handle_execute_script_path", execute)


def test_no_match_and_explicit_legacy_leave_new_service(tmp_path, monkeypatch):
    ontology_dir = _write_package(tmp_path)
    _patch_manifest(monkeypatch, ontology_dir)
    service = DeterministicExecutionService()

    legacy = service.execute(
        _request(key="legacy"),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )
    no_match = service.execute(
        _request(key=None, user_request="unrelated request"),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(legacy, NotApplicable)
    assert legacy.reason == "explicit_legacy_operation"
    assert isinstance(no_match, NotApplicable)
    assert no_match.reason == "no_deterministic_match"


def test_legacy_and_unmatched_requests_skip_complete_package_hash(
    tmp_path,
    monkeypatch,
):
    ontology_dir = _write_package(tmp_path)
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    hash_calls = []
    monkeypatch.setattr(
        service_module,
        "verify_deterministic_package_lock",
        lambda *_args, **_kwargs: hash_calls.append("unexpected"),
    )
    service = DeterministicExecutionService()

    legacy = service.execute(
        _request(key="legacy"),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )
    no_match = service.execute(
        _request(key=None, user_request="unrelated request"),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(legacy, NotApplicable)
    assert isinstance(no_match, NotApplicable)
    assert hash_calls == []


def test_tampered_alias_fails_closed_before_agent_fallback(tmp_path, monkeypatch):
    ontology_dir = _write_package(tmp_path)
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    handler_calls = []
    monkeypatch.setattr(
        service_module,
        "handle_execute_script_path",
        lambda **kwargs: handler_calls.append(kwargs) or json.dumps({"value": "unexpected"}),
    )
    operation_path = ontology_dir / "operations" / "query.md"
    operation_path.write_text(
        operation_path.read_text(encoding="utf-8").replace(
            "Query Exact",
            "Changed Alias",
        ),
        encoding="utf-8",
    )

    outcome = DeterministicExecutionService().execute(
        _request(key=None, user_request="Query Exact", arguments={"code": "A"}),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(outcome, Failed)
    assert outcome.code == "package_integrity_mismatch"
    assert handler_calls == []


def test_cancelled_package_verification_stops_before_handler(tmp_path, monkeypatch):
    ontology_dir = _write_package(tmp_path)
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    calls = []
    monkeypatch.setattr(
        service_module,
        "handle_execute_script_path",
        lambda **kwargs: calls.append(kwargs) or json.dumps({"value": "unexpected"}),
    )
    cancel_event = threading.Event()
    cancel_event.set()

    outcome = DeterministicExecutionService().execute(
        _request(arguments={"code": "A"}),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
        cancel_event=cancel_event,
    )

    assert isinstance(outcome, Failed)
    assert outcome.code == "execution_cancelled"
    assert calls == []


def test_preflight_distinguishes_deterministic_and_legacy_operation_keys(
    tmp_path,
    monkeypatch,
):
    ontology_dir = _write_package(tmp_path)
    _patch_manifest(monkeypatch, ontology_dir)
    service = DeterministicExecutionService()

    assert service.is_potentially_applicable(_request(key="query")) is True
    assert service.is_potentially_applicable(_request(key="legacy")) is False

    without_ontology = _request(key="query")
    without_ontology.runtime_context.selected_ontology_id = None
    without_ontology.context.pop("selected_ontology_id", None)
    assert service.is_potentially_applicable(without_ontology) is False


def test_preflight_routes_malformed_package_to_fail_closed_execution(
    tmp_path,
    monkeypatch,
):
    ontology_dir = _write_package(tmp_path)
    _patch_manifest(monkeypatch, ontology_dir)

    def fail_package_load(*_args, **_kwargs):
        raise service_module.DeterministicPackageError(
            "invalid_deterministic_package",
            "invalid package",
        )

    monkeypatch.setattr(
        service_module,
        "load_deterministic_operation_specs",
        fail_package_load,
    )

    assert DeterministicExecutionService().is_potentially_applicable(
        _request(key=None, user_request="natural request")
    ) is True


def test_invalid_deterministic_checkpoint_never_falls_through_to_legacy(
    tmp_path,
    monkeypatch,
):
    ontology_dir = _write_package(tmp_path)
    _patch_manifest(monkeypatch, ontology_dir)

    outcome = DeterministicExecutionService().execute(
        _request(
            key="legacy",
            checkpoint={"source": "deterministic_execution", "operation_key": "legacy"},
        ),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(outcome, Failed)
    assert outcome.code == "stale_checkpoint"


def test_natural_language_exact_alias_matches(tmp_path, monkeypatch):
    ontology_dir = _write_package(tmp_path)
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    monkeypatch.setattr(
        service_module,
        "handle_execute_script_path",
        lambda **_kwargs: json.dumps({"value": "ok"}),
    )
    request = _request(key=None, arguments={"code": "A"})

    outcome = DeterministicExecutionService().execute(
        request,
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(outcome, Completed)


def test_explicit_alias_stays_on_deterministic_path(tmp_path, monkeypatch):
    ontology_dir = _write_package(tmp_path)
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    monkeypatch.setattr(
        service_module,
        "handle_execute_script_path",
        lambda **_kwargs: json.dumps({"value": "ok"}),
    )

    outcome = DeterministicExecutionService().execute(
        _request(key="Query Exact", arguments={"code": "A"}),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(outcome, Completed)
    assert outcome.operation_key == "query"


def test_natural_language_alias_with_inline_arguments_stays_deterministic(
    tmp_path,
    monkeypatch,
):
    ontology_dir = _write_package(tmp_path)
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    monkeypatch.setattr(
        service_module,
        "handle_execute_script_path",
        lambda **_kwargs: json.dumps({"value": "ok"}),
    )

    outcome = DeterministicExecutionService().execute(
        _request(
            key=None,
            arguments={"code": "CODE-42"},
            user_request="请执行 Query Exact，参数 CODE-42",
        ),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(outcome, Completed)


@pytest.mark.parametrize(
    "user_request",
    (
        "不要执行 Query Exact",
        "Query Exact，参数 CODE-42，但不要执行",
        "do not execute Query Exact",
        "请解释 Query Exact 是什么",
        "文档里提到“Query Exact”",
        "Query Exact 是什么",
        "Query Exact: what does it do?",
        "Query Exact（是什么）",
        "Query Exact, please explain it",
    ),
)
def test_non_command_operation_mentions_do_not_execute(
    tmp_path,
    monkeypatch,
    user_request,
):
    ontology_dir = _write_package(tmp_path)
    _patch_manifest(monkeypatch, ontology_dir)

    outcome = DeterministicExecutionService().execute(
        _request(
            key=None,
            arguments={"code": "CODE-42"},
            user_request=user_request,
        ),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(outcome, NotApplicable)
    assert outcome.reason == "no_deterministic_match"


def test_operation_token_with_key_value_suffix_stays_deterministic(
    tmp_path,
    monkeypatch,
):
    ontology_dir = _write_package(tmp_path)
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    monkeypatch.setattr(
        service_module,
        "handle_execute_script_path",
        lambda **_kwargs: json.dumps({"value": "ok"}),
    )

    outcome = DeterministicExecutionService().execute(
        _request(
            key=None,
            arguments={"code": "CODE-42"},
            user_request="Query Exact, code=CODE-42",
        ),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(outcome, Completed)


@pytest.mark.parametrize(
    "user_request",
    (
        "Query Exact: code=CODE-42",
        "Query Exact（参数 CODE-42）",
        'Query Exact {"code":"CODE-42"}',
    ),
)
def test_operation_token_with_explicit_argument_shape_stays_deterministic(
    tmp_path,
    monkeypatch,
    user_request,
):
    ontology_dir = _write_package(tmp_path)
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    monkeypatch.setattr(
        service_module,
        "handle_execute_script_path",
        lambda **_kwargs: json.dumps({"value": "ok"}),
    )

    outcome = DeterministicExecutionService().execute(
        _request(
            key=None,
            arguments={"code": "CODE-42"},
            user_request=user_request,
        ),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(outcome, Completed)


@pytest.mark.parametrize(
    "user_request",
    (
        'Query Exact 2028-02 [{"code":"CODE-42"}]',
        'Query Exact 2028年2月 [{"code":"CODE-42"}]',
        'Query Exact [{"code":"CODE-42"}]',
    ),
)
def test_operation_alias_with_positional_arguments_stays_deterministic(
    tmp_path,
    monkeypatch,
    user_request,
):
    ontology_dir = _write_package(tmp_path)
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    monkeypatch.setattr(
        service_module,
        "handle_execute_script_path",
        lambda **_kwargs: json.dumps({"value": "ok"}),
    )

    outcome = DeterministicExecutionService().execute(
        _request(
            key=None,
            arguments={"code": "CODE-42"},
            user_request=user_request,
        ),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(outcome, Completed)


@pytest.mark.parametrize(
    "user_request",
    (
        "Query Exact usage notes",
        "Query Exact 使用说明",
        "Query Exact should remain documented",
        "Query Exact 2.0 release notes",
        "Query Exact 2028-02 release notes",
    ),
)
def test_operation_alias_with_explanatory_suffix_does_not_execute(
    tmp_path,
    monkeypatch,
    user_request,
):
    ontology_dir = _write_package(tmp_path)
    _patch_manifest(monkeypatch, ontology_dir)

    outcome = DeterministicExecutionService().execute(
        _request(
            key=None,
            arguments={"code": "CODE-42"},
            user_request=user_request,
        ),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(outcome, NotApplicable)
    assert outcome.reason == "no_deterministic_match"


def test_real_handler_executes_with_schema_mapped_arguments(tmp_path, monkeypatch):
    ontology_dir = _write_package(tmp_path)
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    _allow_local_test_runner(monkeypatch, service_module)

    outcome = DeterministicExecutionService().execute(
        _request(arguments={"code": "CODE-42"}),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(outcome, Completed)
    assert outcome.envelope["structured_result"] == {"value": "CODE-42"}


def test_runtime_revision_change_during_handler_fails_closed(tmp_path, monkeypatch):
    ontology_dir = _write_package(tmp_path)
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    revisions = iter(["runner-a", "runner-b"])
    monkeypatch.setattr(service_module, "runtime_revision", lambda: next(revisions))
    monkeypatch.setattr(
        service_module,
        "handle_execute_script_path",
        lambda **_kwargs: json.dumps({"value": "ok"}),
    )

    outcome = DeterministicExecutionService().execute(
        _request(arguments={"code": "A"}),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(outcome, Failed)
    assert outcome.code == "runtime_revision_changed"
    assert outcome.handler_execution_count == 1


def test_authoritative_envelope_uses_locked_runtime_revision(tmp_path, monkeypatch):
    ontology_dir = _write_package(tmp_path)
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    revision_reads = []

    def locked_revision():
        revision_reads.append("runner-a")
        if len(revision_reads) > 2:
            raise AssertionError("runtime revision must not be re-read by envelope build")
        return "runner-a"

    monkeypatch.setattr(service_module, "runtime_revision", locked_revision)
    monkeypatch.setattr(
        service_module,
        "handle_execute_script_path",
        lambda **_kwargs: json.dumps({"value": "ok"}),
    )

    outcome = DeterministicExecutionService().execute(
        _request(arguments={"code": "A"}),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(outcome, Completed)
    assert outcome.envelope["execution"]["runtime_revision"] == "runner-a"
    assert revision_reads == ["runner-a", "runner-a"]
    assert outcome.envelope["execution"]["handler_execution_count"] == 1


def test_handler_parameters_are_loaded_from_qualified_descriptor_identity(
    tmp_path,
    monkeypatch,
):
    ontology_dir = _write_package(tmp_path)
    nested_dir = ontology_dir / "scripts" / "nested"
    nested_dir.mkdir()
    (ontology_dir / "scripts" / "query.md").rename(nested_dir / "query.md")
    (ontology_dir / "scripts" / "a-legacy.md").write_text(
        "---\n"
        "resource: script\n"
        "key: query_handler\n"
        "bound_operation: legacy\n"
        "entrypoint: legacy.py\n"
        "---\n\n"
        "| 参数 key | CLI 参数 | 类型 | 必填 |\n"
        "| --- | --- | --- | --- |\n"
        "| wrong | --wrong | string | 是 |\n",
        encoding="utf-8",
    )
    (ontology_dir / "scripts" / "legacy.py").write_text(
        "print('{}')\n",
        encoding="utf-8",
    )
    validate_and_lock_deterministic_operations(ontology_dir)
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    calls = []
    monkeypatch.setattr(
        service_module,
        "handle_execute_script_path",
        lambda **kwargs: calls.append(kwargs) or json.dumps({"value": "CODE-42"}),
    )

    outcome = DeterministicExecutionService().execute(
        _request(arguments={"code": "CODE-42"}),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(outcome, Completed)
    assert calls[0]["args"] == ["--code", "CODE-42"]


def test_real_handler_can_repeat_without_mutating_locked_package(tmp_path, monkeypatch):
    ontology_dir = _write_package(tmp_path)
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    _allow_local_test_runner(monkeypatch, service_module)
    service = DeterministicExecutionService()

    outcomes = [
        service.execute(
            _request(arguments={"code": "CODE-42"}),
            enabled=True,
            project_root=str(tmp_path),
            timeout_seconds=30,
        )
        for _index in range(2)
    ]

    assert all(isinstance(outcome, Completed) for outcome in outcomes)
    assert outcomes[0].envelope["execution"]["result_sha256"] == outcomes[1].envelope[
        "execution"
    ]["result_sha256"]
    assert not (ontology_dir / "scripts" / "__pycache__").exists()


def test_handler_package_mutation_fails_before_authoritative_result(tmp_path, monkeypatch):
    ontology_dir = _write_package(tmp_path)
    service_module = _patch_manifest(monkeypatch, ontology_dir)

    def mutate_package(**_kwargs):
        (ontology_dir / "scripts" / "query_helper.py").write_text(
            "def normalize_code(value):\n    return 'mutated'\n",
            encoding="utf-8",
        )
        return json.dumps({"value": "CODE-42"})

    monkeypatch.setattr(service_module, "handle_execute_script_path", mutate_package)

    outcome = DeterministicExecutionService().execute(
        _request(arguments={"code": "CODE-42"}),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(outcome, Failed)
    assert outcome.code == "package_integrity_mismatch"
    assert outcome.handler_execution_count == 1


def test_feature_disabled_fails_without_starting_handler(tmp_path, monkeypatch):
    ontology_dir = _write_package(tmp_path)
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    calls = []
    monkeypatch.setattr(
        service_module,
        "handle_execute_script_path",
        lambda **kwargs: calls.append(kwargs) or "{}",
    )

    outcome = DeterministicExecutionService().execute(
        _request(arguments={"code": "A"}),
        enabled=False,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(outcome, Failed)
    assert outcome.code == "feature_disabled"
    assert calls == []


def test_input_schema_format_is_enforced(tmp_path, monkeypatch):
    ontology_dir = _write_package(tmp_path)
    schema_path = ontology_dir / "schemas" / "query.input.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema["properties"]["code"]["format"] = "email"
    schema_path.write_text(json.dumps(schema), encoding="utf-8")
    validate_and_lock_deterministic_operations(ontology_dir)
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    calls = []
    monkeypatch.setattr(
        service_module,
        "handle_execute_script_path",
        lambda **kwargs: calls.append(kwargs) or json.dumps({"value": "ok"}),
    )

    outcome = DeterministicExecutionService().execute(
        _request(arguments={"code": "not-an-email"}),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(outcome, Failed)
    assert outcome.code == "input_schema_violation"
    assert "not-an-email" not in str(outcome.internal_detail)
    assert "validator=format" in str(outcome.internal_detail)
    assert calls == []


def test_missing_argument_awaits_and_resume_executes_once(tmp_path, monkeypatch):
    ontology_dir = _write_package(tmp_path)
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    calls = []
    monkeypatch.setattr(
        service_module,
        "handle_execute_script_path",
        lambda **kwargs: calls.append(kwargs) or json.dumps({"value": "ok"}),
    )
    service = DeterministicExecutionService()

    awaiting = service.execute(
        _request(),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )
    resumed = service.execute(
        _request(
            checkpoint=awaiting.checkpoint,
            response={"value": "A"},
        ),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(awaiting, AwaitingInput)
    assert awaiting.interaction["type"] == "input"
    assert calls and len(calls) == 1
    assert isinstance(resumed, Completed)
    assert resumed.envelope["structured_result"] == {"value": "ok"}
    assert resumed.envelope["execution"]["handler_execution_count"] == 1


@pytest.mark.parametrize(
    ("property_schema", "response_value"),
    [
        ({"type": "integer"}, "5"),
        ({"type": "number"}, "5.25"),
        ({"type": "boolean"}, "true"),
        ({"type": "object"}, '{"region":"north"}'),
        ({"type": "array"}, '["north","south"]'),
    ],
)
def test_missing_argument_resume_parses_json_value_for_schema_type(
    tmp_path,
    monkeypatch,
    property_schema,
    response_value,
):
    ontology_dir = _write_package(tmp_path)
    schema_path = ontology_dir / "schemas" / "query.input.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema["properties"]["code"] = property_schema
    schema_path.write_text(json.dumps(schema), encoding="utf-8")
    validate_and_lock_deterministic_operations(ontology_dir)
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    calls = []
    monkeypatch.setattr(
        service_module,
        "handle_execute_script_path",
        lambda **kwargs: calls.append(kwargs) or json.dumps({"value": "ok"}),
    )
    service = DeterministicExecutionService()

    awaiting = service.execute(
        _request(),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )
    resumed = service.execute(
        _request(
            checkpoint=awaiting.checkpoint,
            response={"value": response_value},
        ),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(awaiting, AwaitingInput)
    assert isinstance(resumed, Completed)
    assert len(calls) == 1


def test_missing_numeric_enum_uses_typed_single_choice(tmp_path, monkeypatch):
    ontology_dir = _write_package(tmp_path)
    schema_path = ontology_dir / "schemas" / "query.input.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema["properties"]["code"] = {"type": "integer", "enum": [5, 10]}
    schema_path.write_text(json.dumps(schema), encoding="utf-8")
    validate_and_lock_deterministic_operations(ontology_dir)
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    monkeypatch.setattr(
        service_module,
        "handle_execute_script_path",
        lambda **_kwargs: json.dumps({"value": "ok"}),
    )
    service = DeterministicExecutionService()

    awaiting = service.execute(
        _request(),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )
    resumed = service.execute(
        _request(
            checkpoint=awaiting.checkpoint,
            response={"value": 5},
        ),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(awaiting, AwaitingInput)
    assert awaiting.interaction["type"] == "single_choice"
    assert awaiting.interaction["options"] == [
        {"id": "1", "label": "5", "value": 5},
        {"id": "2", "label": "10", "value": 10},
    ]
    assert isinstance(resumed, Completed)


def test_all_of_required_argument_awaits_input(tmp_path, monkeypatch):
    ontology_dir = _write_package(tmp_path)
    schema_path = ontology_dir / "schemas" / "query.input.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema.pop("required")
    schema["allOf"] = [{"required": ["code"]}]
    schema_path.write_text(json.dumps(schema), encoding="utf-8")
    validate_and_lock_deterministic_operations(ontology_dir)
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    calls = []
    monkeypatch.setattr(
        service_module,
        "handle_execute_script_path",
        lambda **kwargs: calls.append(kwargs) or json.dumps({"value": "unexpected"}),
    )

    outcome = DeterministicExecutionService().execute(
        _request(),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(outcome, AwaitingInput)
    assert outcome.interaction["metadata"]["field"] == "code"
    assert outcome.interaction["prompt"] == "Provide code"
    assert calls == []


def test_checkpoint_resume_does_not_rerun_parser_or_override_confirmed_value(
    tmp_path,
    monkeypatch,
):
    ontology_dir = _write_package(tmp_path, parser=True)
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    calls = []

    def execute(**kwargs):
        calls.append(kwargs)
        if kwargs["caller"] == "deterministic_parser":
            return json.dumps({
                "matched": True,
                "arguments": {},
                "issues": [{"field": "code", "message": "Choose a code"}],
            })
        assert "confirmed-code" in kwargs["args"]
        return json.dumps({"value": "confirmed-code"})

    monkeypatch.setattr(service_module, "handle_execute_script_path", execute)
    service = DeterministicExecutionService()

    awaiting = service.execute(
        _request(user_request="Query Exact"),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )
    resumed = service.execute(
        _request(
            checkpoint=awaiting.checkpoint,
            response={"value": "confirmed-code"},
            user_request="confirmed-code",
        ),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(awaiting, AwaitingInput)
    assert isinstance(resumed, Completed)
    assert resumed.envelope["structured_result"] == {"value": "confirmed-code"}
    assert [call["caller"] for call in calls] == [
        "deterministic_parser",
        "deterministic_service",
    ]


def test_checkpoint_preserves_all_parser_issues_and_resumes_them_in_order(
    tmp_path,
    monkeypatch,
):
    ontology_dir = _write_package(tmp_path, parser=True)
    input_schema_path = ontology_dir / "schemas" / "query.input.json"
    input_schema = json.loads(input_schema_path.read_text(encoding="utf-8"))
    input_schema["properties"]["region"] = {"type": "string"}
    input_schema_path.write_text(json.dumps(input_schema), encoding="utf-8")
    descriptor_path = ontology_dir / "scripts" / "query.md"
    descriptor_path.write_text(
        descriptor_path.read_text(encoding="utf-8")
        + "| region | --region | string | 否 |\n",
        encoding="utf-8",
    )
    handler_path = ontology_dir / "scripts" / "query.py"
    handler_path.write_text(
        handler_path.read_text(encoding="utf-8").replace(
            "parser.add_argument('--code', default='ok')\n",
            "parser.add_argument('--code', default='ok')\n"
            "parser.add_argument('--region')\n",
        ),
        encoding="utf-8",
    )
    validate_and_lock_deterministic_operations(ontology_dir)
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    calls = []

    def execute(**kwargs):
        calls.append(kwargs)
        if kwargs["caller"] == "deterministic_parser":
            return json.dumps({
                "matched": True,
                "arguments": {},
                "issues": [
                    {"field": "code", "message": "Choose a code"},
                    {"field": "region", "message": "Choose a region"},
                ],
            })
        return json.dumps({"value": "confirmed-code"})

    monkeypatch.setattr(service_module, "handle_execute_script_path", execute)
    service = DeterministicExecutionService()

    first = service.execute(
        _request(),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )
    second = service.execute(
        _request(
            checkpoint=first.checkpoint,
            response={"value": "confirmed-code"},
        ),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )
    completed = service.execute(
        _request(
            checkpoint=second.checkpoint,
            response={"value": "north"},
        ),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(first, AwaitingInput)
    assert first.interaction["metadata"]["field"] == "code"
    assert isinstance(second, AwaitingInput)
    assert second.interaction["metadata"]["field"] == "region"
    assert isinstance(completed, Completed)
    assert completed.envelope["structured_result"] == {"value": "confirmed-code"}
    assert [call["caller"] for call in calls] == [
        "deterministic_parser",
        "deterministic_service",
    ]


def test_noninteractive_missing_argument_fails_instead_of_awaiting(tmp_path, monkeypatch):
    ontology_dir = _write_package(tmp_path)
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    calls = []
    monkeypatch.setattr(
        service_module,
        "handle_execute_script_path",
        lambda **kwargs: calls.append(kwargs) or json.dumps({"value": "ok"}),
    )

    outcome = DeterministicExecutionService().execute(
        _request(context={"supports_user_interaction": False}),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(outcome, Failed)
    assert outcome.code == "missing_arguments"
    assert calls == []


def test_parser_and_handler_use_isolated_execution_contract(tmp_path, monkeypatch):
    ontology_dir = _write_package(tmp_path, parser=True)
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    calls = []

    def execute(**kwargs):
        calls.append(kwargs)
        if kwargs["caller"] == "deterministic_parser":
            return json.dumps({"matched": True, "arguments": {}, "issues": []})
        return json.dumps({"value": "ok"})

    monkeypatch.setattr(service_module, "handle_execute_script_path", execute)
    registered = []
    monkeypatch.setattr(
        service_module,
        "register_run_sandbox_companion",
        lambda parent_run_id, companion_run_id: registered.append(
            (parent_run_id, companion_run_id)
        ),
    )
    released = []
    monkeypatch.setattr(
        service_module,
        "release_run_sandbox",
        lambda run_id, **kwargs: released.append((run_id, kwargs)),
    )

    outcome = DeterministicExecutionService().execute(
        _request(arguments={"code": "A"}),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(outcome, Completed)
    parser_call, handler_call = calls
    assert parser_call["clean_env"] is True
    assert parser_call["sandbox_prepare_data_connection"] is False
    assert parser_call["sandbox_include_data_connection_env"] is False
    parser_sandbox_run_id = parser_call["sandbox_run_id"]
    assert parser_sandbox_run_id.startswith("det-parser-")
    assert parser_sandbox_run_id != "run-1-det-parser"
    assert parser_call["sandbox_required"] is True
    assert parser_call["sandbox_restrict_filesystem_to_ontology"] is True
    assert "RUN_ARTIFACT_DIR" not in parser_call["env_overrides"]
    assert handler_call["clean_env"] is True
    assert handler_call["include_stderr_on_success"] is False
    assert handler_call["sandbox_prepare_data_connection"] is False
    assert handler_call["sandbox_writable_paths"]
    assert handler_call["sandbox_required"] is True
    assert handler_call["sandbox_restrict_filesystem_to_ontology"] is True
    assert registered == [("run-1", parser_sandbox_run_id)]
    assert released == [
        (parser_sandbox_run_id, {"strict": True}),
        ("run-1", {"strict": True}),
    ]


def test_parser_sandbox_release_failure_preserves_outer_retry_id(tmp_path, monkeypatch):
    ontology_dir = _write_package(tmp_path, parser=True)
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    calls = []
    monkeypatch.setattr(
        service_module,
        "handle_execute_script_path",
        lambda **kwargs: calls.append(kwargs)
        or json.dumps({"matched": True, "arguments": {}, "issues": []}),
    )
    released = []

    def release(run_id, **kwargs):
        released.append((run_id, kwargs))
        if run_id.startswith("det-parser-"):
            raise RuntimeError("temporary delete failure")

    monkeypatch.setattr(service_module, "release_run_sandbox", release)

    outcome = DeterministicExecutionService().execute(
        _request(arguments={"code": "A"}),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(outcome, Failed)
    assert outcome.code == "deterministic_isolation_unavailable"
    assert [call["caller"] for call in calls] == ["deterministic_parser"]
    assert len(released) == 1
    assert released[0][0].startswith("det-parser-")
    assert released[0][1] == {"strict": True}


def test_handler_sandbox_release_failure_fails_closed(tmp_path, monkeypatch):
    ontology_dir = _write_package(tmp_path)
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    monkeypatch.setattr(
        service_module,
        "handle_execute_script_path",
        lambda **_kwargs: json.dumps({"value": "ok"}),
    )
    monkeypatch.setattr(
        service_module,
        "release_run_sandbox",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("delete failed")),
    )

    outcome = DeterministicExecutionService().execute(
        _request(arguments={"code": "A"}),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(outcome, Failed)
    assert outcome.code == "deterministic_isolation_unavailable"
    assert outcome.handler_execution_count == 1


def test_required_sandbox_failure_is_a_stable_deterministic_error(tmp_path, monkeypatch):
    ontology_dir = _write_package(tmp_path)
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    monkeypatch.setattr(
        service_module,
        "handle_execute_script_path",
        lambda **_kwargs: "Error: Sandbox unavailable.",
    )

    outcome = DeterministicExecutionService().execute(
        _request(arguments={"code": "A"}),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(outcome, Failed)
    assert outcome.code == "deterministic_isolation_unavailable"


def test_workspace_provider_binding_is_forwarded_to_sandbox_without_reresolution(
    tmp_path,
    monkeypatch,
):
    ontology_dir = _write_package(tmp_path, data_access="workspace_provider")
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    effective_connection = EffectiveConnection(
        version=1,
        provider="neo4j",
        endpoint="bolt://snapshot.example.com:7687",
        auth=AuthSpec(
            type="mindsdb_password",
            username="reader",
            password="provider-secret",
        ),
        options={"database": "snapshot", "data_revision": "rev-1"},
    )
    monkeypatch.setattr(
        service_module,
        "resolve_deterministic_provider_binding",
        lambda **_kwargs: SimpleNamespace(
            effective_connection=effective_connection,
            source_ref="business_data",
            data_revision="rev-1",
            scope_digest="sha256:scope",
            network_endpoint=effective_connection.endpoint,
        ),
    )
    calls = []
    monkeypatch.setattr(
        service_module,
        "handle_execute_script_path",
        lambda **kwargs: calls.append(kwargs) or json.dumps({"value": "ok"}),
    )

    outcome = DeterministicExecutionService().execute(
        _request(arguments={"code": "A"}),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(outcome, Completed)
    handler_call = calls[0]
    assert handler_call["sandbox_prepare_data_connection"] is False
    assert handler_call["sandbox_network_endpoint"] == effective_connection.endpoint
    connection_payload = json.loads(
        handler_call["env_overrides"]["ONTO_SCRIPT_BUSINESS_DATA_CONNECTION_JSON"]
    )
    assert connection_payload["endpoint"] == effective_connection.endpoint
    assert outcome.envelope["execution"]["dependencies"][0]["revision"] == "rev-1"


def test_injected_short_provider_credential_fails_before_handler(tmp_path, monkeypatch):
    ontology_dir = _write_package(tmp_path, data_access="workspace_provider")
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    effective_connection = EffectiveConnection(
        version=1,
        provider="neo4j",
        endpoint="bolt://snapshot.example.com:7687",
        auth=AuthSpec(type="mindsdb_password", username="reader", password="short"),
        options={"database": "snapshot", "data_revision": "rev-1"},
    )
    monkeypatch.setattr(
        service_module,
        "resolve_deterministic_provider_binding",
        lambda **_kwargs: SimpleNamespace(
            effective_connection=effective_connection,
            source_ref="business_data",
            data_revision="rev-1",
            scope_digest="sha256:scope",
            network_endpoint=effective_connection.endpoint,
        ),
    )
    monkeypatch.setattr(
        service_module,
        "handle_execute_script_path",
        lambda **_kwargs: pytest.fail("handler must not execute"),
    )

    outcome = DeterministicExecutionService().execute(
        _request(arguments={"code": "A"}),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(outcome, Failed)
    assert outcome.code == "provider_credentials_unsafe"


@pytest.mark.parametrize(
    ("handler_output", "expected_code"),
    (
        (json.dumps({"value": "provider-secret"}), "secret_output_violation"),
        ("Error: provider rejected password provider-secret", "handler_failed"),
    ),
)
def test_provider_secret_never_reaches_result_or_failure_detail(
    tmp_path,
    monkeypatch,
    handler_output,
    expected_code,
):
    ontology_dir = _write_package(tmp_path, data_access="workspace_provider")
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    effective_connection = EffectiveConnection(
        version=1,
        provider="neo4j",
        endpoint="bolt://snapshot.example.com:7687",
        auth=AuthSpec(
            type="mindsdb_password",
            username="reader",
            password="provider-secret",
        ),
        options={"database": "snapshot", "data_revision": "rev-1"},
    )
    monkeypatch.setattr(
        service_module,
        "resolve_deterministic_provider_binding",
        lambda **_kwargs: SimpleNamespace(
            effective_connection=effective_connection,
            source_ref="business_data",
            data_revision="rev-1",
            scope_digest="sha256:scope",
            network_endpoint=effective_connection.endpoint,
        ),
    )
    monkeypatch.setattr(
        service_module,
        "handle_execute_script_path",
        lambda **_kwargs: handler_output,
    )

    outcome = DeterministicExecutionService().execute(
        _request(arguments={"code": "A"}),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(outcome, Failed)
    assert outcome.code == expected_code
    assert "provider-secret" not in str(outcome.internal_detail)


def test_provider_secret_in_artifact_is_rejected_before_persistence(tmp_path, monkeypatch):
    ontology_dir = _write_package(tmp_path, data_access="workspace_provider")
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    effective_connection = EffectiveConnection(
        version=1,
        provider="neo4j",
        endpoint="bolt://snapshot.example.com:7687",
        auth=AuthSpec(type="bearer", token="provider-token"),
        options={"database": "snapshot", "data_revision": "rev-1"},
    )
    monkeypatch.setattr(
        service_module,
        "resolve_deterministic_provider_binding",
        lambda **_kwargs: SimpleNamespace(
            effective_connection=effective_connection,
            source_ref="business_data",
            data_revision="rev-1",
            scope_digest="sha256:scope",
            network_endpoint=effective_connection.endpoint,
        ),
    )

    def execute(**kwargs):
        artifact_dir = Path(kwargs["env_overrides"]["RUN_ARTIFACT_DIR"])
        (artifact_dir / "result.txt").write_text("provider-token", encoding="utf-8")
        return json.dumps({"value": "ok"})

    persisted = []
    monkeypatch.setattr(service_module, "handle_execute_script_path", execute)

    outcome = DeterministicExecutionService().execute(
        _request(arguments={"code": "A"}),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
        artifact_writer=lambda *args: persisted.append(args),
    )

    assert isinstance(outcome, Failed)
    assert outcome.code == "secret_output_violation"
    assert persisted == []


@pytest.mark.parametrize("channel", ["result", "failure", "artifact"])
@pytest.mark.parametrize(
    ("endpoint", "embedded_secret"),
    [
        ("bolt://embedded:uri-secret@snapshot.example.com:7687", "uri-secret"),
        ("neo4j:uri-secret@snapshot.example.com:7687", "uri-secret"),
        ("bolt://snapshot.example.com:7687?access_token=query-secret", "query-secret"),
        ("bolt://snapshot.example.com:7687#fragment-secret", "fragment-secret"),
    ],
)
def test_embedded_endpoint_credentials_are_redacted_on_every_output_channel(
    tmp_path,
    monkeypatch,
    channel,
    endpoint,
    embedded_secret,
):
    ontology_dir = _write_package(tmp_path, data_access="workspace_provider")
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    effective_connection = EffectiveConnection(
        version=1,
        provider="neo4j",
        endpoint=endpoint,
        auth=AuthSpec(type="none"),
        options={"database": "snapshot", "data_revision": "rev-1"},
    )
    monkeypatch.setattr(
        service_module,
        "resolve_deterministic_provider_binding",
        lambda **_kwargs: SimpleNamespace(
            effective_connection=effective_connection,
            source_ref="business_data",
            data_revision="rev-1",
            scope_digest="sha256:scope",
            network_endpoint=effective_connection.endpoint,
        ),
    )

    def execute(**kwargs):
        if channel == "artifact":
            artifact_dir = Path(kwargs["env_overrides"]["RUN_ARTIFACT_DIR"])
            (artifact_dir / "result.txt").write_text(embedded_secret, encoding="utf-8")
            return json.dumps({"value": "ok"})
        if channel == "failure":
            return "Error: connection failed for {}".format(embedded_secret)
        return json.dumps({"value": embedded_secret})

    persisted = []
    monkeypatch.setattr(service_module, "handle_execute_script_path", execute)
    outcome = DeterministicExecutionService().execute(
        _request(arguments={"code": "A"}),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
        artifact_writer=lambda *args: persisted.append(args),
    )

    assert isinstance(outcome, Failed)
    assert outcome.code == (
        "handler_failed" if channel == "failure" else "secret_output_violation"
    )
    assert embedded_secret not in str(outcome.internal_detail)
    assert persisted == []


def test_provider_revision_change_invalidates_pending_checkpoint(tmp_path, monkeypatch):
    ontology_dir = _write_package(tmp_path, data_access="workspace_provider")
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    revision = ["rev-1"]
    effective_connection = EffectiveConnection(
        version=1,
        provider="neo4j",
        endpoint="bolt://snapshot.example.com:7687",
        auth=AuthSpec(
            type="mindsdb_password",
            username="reader",
            password="provider-secret",
        ),
        options={"database": "snapshot"},
    )

    def binding(**_kwargs):
        return SimpleNamespace(
            effective_connection=effective_connection,
            source_ref="business_data",
            data_revision=revision[0],
            scope_digest="sha256:{}".format(revision[0]),
            network_endpoint=effective_connection.endpoint,
        )

    monkeypatch.setattr(service_module, "resolve_deterministic_provider_binding", binding)
    calls = []
    monkeypatch.setattr(
        service_module,
        "handle_execute_script_path",
        lambda **kwargs: calls.append(kwargs) or json.dumps({"value": "ok"}),
    )
    service = DeterministicExecutionService()
    awaiting = service.execute(
        _request(),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )
    assert isinstance(awaiting, AwaitingInput)
    assert awaiting.checkpoint["provider_dependency"]["revision"] == "rev-1"

    revision[0] = "rev-2"
    resumed = service.execute(
        _request(checkpoint=awaiting.checkpoint, response={"value": "A"}),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(resumed, Failed)
    assert resumed.code == "stale_checkpoint"
    assert calls == []


def test_success_builds_authoritative_envelope_and_persists_artifact(tmp_path, monkeypatch):
    ontology_dir = _write_package(tmp_path)
    service_module = _patch_manifest(monkeypatch, ontology_dir)

    def execute_handler(**kwargs):
        artifact_dir = Path(kwargs["env_overrides"]["RUN_ARTIFACT_DIR"])
        (artifact_dir / "result.csv").write_text("value\nok\n", encoding="utf-8")
        return json.dumps({"value": "ok"})

    stored = []
    monkeypatch.setattr(service_module, "handle_execute_script_path", execute_handler)

    outcome = DeterministicExecutionService().execute(
        _request(arguments={"code": "A"}),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
        artifact_writer=lambda name, text, media: stored.append((name, text, media))
        or {
            "output_ref": "ref-1",
            "content_type": media,
            "sha256": "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest(),
        },
    )

    assert isinstance(outcome, Completed)
    assert outcome.envelope["execution"]["authoritative"] is True
    assert outcome.envelope["execution"]["qualification"] == "trusted_package_contract_v1"
    assert outcome.envelope["execution"]["arguments_sha256"].startswith("sha256:")
    assert outcome.envelope["execution"]["result_sha256"].startswith("sha256:")
    assert outcome.envelope["artifacts"][0]["output_ref"] == "ref-1"
    assert stored[0][0] == "result.csv"
    assert stored[0][2] == "text/csv"


def test_artifact_monitor_cancels_handler_when_total_limit_is_exceeded(
    tmp_path,
    monkeypatch,
):
    ontology_dir = _write_package(tmp_path)
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    monkeypatch.setattr(service_module, "MAX_ARTIFACT_BYTES", 128)
    monkeypatch.setattr(service_module, "MAX_ARTIFACT_TOTAL_BYTES", 150)
    monkeypatch.setattr(service_module, "ARTIFACT_MONITOR_INTERVAL_SECONDS", 0.001)
    observed = {}

    def execute_handler(**kwargs):
        artifact_dir = Path(kwargs["env_overrides"]["RUN_ARTIFACT_DIR"])
        (artifact_dir / "first.txt").write_text("a" * 100, encoding="utf-8")
        (artifact_dir / "second.txt").write_text("b" * 100, encoding="utf-8")
        deadline = time.monotonic() + 2
        while not kwargs["cancel_event"].is_set() and time.monotonic() < deadline:
            time.sleep(0.001)
        observed["cancelled"] = kwargs["cancel_event"].is_set()
        observed["file_limit"] = kwargs["file_size_limit_bytes"]
        return json.dumps({"value": "must-not-commit"})

    monkeypatch.setattr(service_module, "handle_execute_script_path", execute_handler)

    outcome = DeterministicExecutionService().execute(
        _request(arguments={"code": "A"}),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(outcome, Failed)
    assert outcome.code == "artifact_policy_violation"
    assert outcome.handler_execution_count == 1
    assert observed == {"cancelled": True, "file_limit": 128}


def test_artifact_monitor_fails_closed_when_staging_lease_cannot_refresh(
    tmp_path,
    monkeypatch,
):
    ontology_dir = _write_package(tmp_path)
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    observed = {}

    def fail_lease(_path):
        raise OSError("shared staging unavailable")

    def execute_handler(**kwargs):
        deadline = time.monotonic() + 2
        while not kwargs["cancel_event"].is_set() and time.monotonic() < deadline:
            time.sleep(0.001)
        observed["cancelled"] = kwargs["cancel_event"].is_set()
        return json.dumps({"value": "must-not-commit"})

    monkeypatch.setattr(
        DeterministicExecutionService,
        "_refresh_artifact_staging_lease",
        staticmethod(fail_lease),
    )
    monkeypatch.setattr(service_module, "handle_execute_script_path", execute_handler)

    outcome = DeterministicExecutionService().execute(
        _request(arguments={"code": "A"}),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(outcome, Failed)
    assert outcome.code == "deterministic_isolation_unavailable"
    assert outcome.handler_execution_count == 1
    assert observed == {"cancelled": True}


def test_artifact_staging_is_hidden_then_frozen_before_persistence(
    tmp_path,
    monkeypatch,
):
    ontology_dir = _write_package(tmp_path)
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    observed = {}

    def execute_handler(**kwargs):
        artifact_dir = Path(kwargs["env_overrides"]["RUN_ARTIFACT_DIR"])
        observed["path"] = artifact_dir
        observed["parent_mode"] = stat.S_IMODE(artifact_dir.parent.stat().st_mode)
        observed["handler_mode"] = stat.S_IMODE(artifact_dir.stat().st_mode)
        (artifact_dir / "result.txt").write_text("ok", encoding="utf-8")
        return json.dumps({"value": "ok"})

    def persist_artifact(_name, _text, media_type):
        observed["persistence_mode"] = stat.S_IMODE(observed["path"].stat().st_mode)
        return {
            "output_ref": "ref-1",
            "content_type": media_type,
            "sha256": "sha256:"
            + hashlib.sha256(_text.encode("utf-8")).hexdigest(),
        }

    monkeypatch.setattr(service_module, "handle_execute_script_path", execute_handler)

    outcome = DeterministicExecutionService().execute(
        _request(arguments={"code": "A"}),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
        artifact_writer=persist_artifact,
    )

    assert isinstance(outcome, Completed)
    assert observed["parent_mode"] == 0o711
    assert observed["handler_mode"] == 0o733
    assert observed["persistence_mode"] == 0o700


def test_artifact_staging_uses_configured_shared_root(tmp_path, monkeypatch):
    ontology_dir = _write_package(tmp_path)
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    shared_root = tmp_path / "shared-artifacts"
    observed = {}
    monkeypatch.setenv(
        "DETERMINISTIC_ARTIFACT_STAGING_ROOT",
        str(shared_root),
    )

    def execute_handler(**kwargs):
        artifact_dir = Path(kwargs["env_overrides"]["RUN_ARTIFACT_DIR"])
        observed["artifact_dir"] = artifact_dir
        (artifact_dir / "result.txt").write_text("ok", encoding="utf-8")
        return json.dumps({"value": "ok"})

    monkeypatch.setattr(service_module, "handle_execute_script_path", execute_handler)

    outcome = DeterministicExecutionService().execute(
        _request(arguments={"code": "A"}),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(outcome, Completed)
    assert observed["artifact_dir"].parent.parent == shared_root.resolve()


def test_artifact_staging_cleans_only_stale_owned_directories(tmp_path, monkeypatch):
    shared_root = tmp_path / "shared-artifacts"
    stale = shared_root / "deterministic-artifact-staging-stale"
    leased = shared_root / "deterministic-artifact-staging-leased"
    recent = shared_root / "deterministic-artifact-staging-recent"
    unrelated = shared_root / "operator-data"
    stale.mkdir(parents=True)
    leased.mkdir()
    recent.mkdir()
    unrelated.mkdir()
    (stale / "payload.txt").write_text("stale", encoding="utf-8")
    stale_timestamp = time.time() - service_module.STALE_ARTIFACT_STAGING_SECONDS - 1
    os.utime(stale, (stale_timestamp, stale_timestamp))
    os.utime(leased, (stale_timestamp, stale_timestamp))
    DeterministicExecutionService._refresh_artifact_staging_lease(leased)
    monkeypatch.setenv(
        "DETERMINISTIC_ARTIFACT_STAGING_ROOT",
        str(shared_root),
    )

    parent = DeterministicExecutionService._artifact_staging_parent()
    DeterministicExecutionService._cleanup_stale_artifact_staging(parent)

    assert not stale.exists()
    assert leased.is_dir()
    assert recent.is_dir()
    assert unrelated.is_dir()


def test_artifact_staging_rejects_relative_configured_root(tmp_path, monkeypatch):
    ontology_dir = _write_package(tmp_path)
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    monkeypatch.setenv("DETERMINISTIC_ARTIFACT_STAGING_ROOT", "relative/path")
    calls = []
    monkeypatch.setattr(
        service_module,
        "handle_execute_script_path",
        lambda **kwargs: calls.append(kwargs) or json.dumps({"value": "ok"}),
    )

    outcome = DeterministicExecutionService().execute(
        _request(arguments={"code": "A"}),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(outcome, Failed)
    assert outcome.code == "deterministic_isolation_unavailable"
    assert calls == []


def test_artifact_staging_creation_failure_is_closed(tmp_path, monkeypatch):
    ontology_dir = _write_package(tmp_path)
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    calls = []
    monkeypatch.setattr(
        service_module,
        "handle_execute_script_path",
        lambda **kwargs: calls.append(kwargs) or json.dumps({"value": "ok"}),
    )

    def fail_staging(**_kwargs):
        raise OSError("read-only staging volume")

    monkeypatch.setattr(service_module.tempfile, "TemporaryDirectory", fail_staging)

    outcome = DeterministicExecutionService().execute(
        _request(arguments={"code": "A"}),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(outcome, Failed)
    assert outcome.code == "deterministic_isolation_unavailable"
    assert calls == []


def test_artifact_persistence_failure_rolls_back_prior_artifacts(tmp_path, monkeypatch):
    ontology_dir = _write_package(tmp_path)
    service_module = _patch_manifest(monkeypatch, ontology_dir)

    def execute_handler(**kwargs):
        artifact_dir = Path(kwargs["env_overrides"]["RUN_ARTIFACT_DIR"])
        (artifact_dir / "first.txt").write_text("first", encoding="utf-8")
        (artifact_dir / "second.txt").write_text("second", encoding="utf-8")
        return json.dumps({"value": "ok"})

    class FailingWriter:
        def __init__(self):
            self.calls = []
            self.prepared = []
            self.rolled_back = []

        def prepare_artifacts(self, names):
            self.prepared.extend(names)

        def __call__(self, name, text, media_type):
            self.calls.append((name, text, media_type))
            if len(self.calls) == 2:
                raise OSError("storage unavailable")
            return {
                "output_ref": "ref-first",
                "content_type": media_type,
                "sha256": "sha256:"
                + hashlib.sha256(text.encode("utf-8")).hexdigest(),
            }

        def rollback(self, output_refs):
            self.rolled_back.extend(output_refs)

    writer = FailingWriter()
    monkeypatch.setattr(service_module, "handle_execute_script_path", execute_handler)

    outcome = DeterministicExecutionService().execute(
        _request(arguments={"code": "A"}),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
        artifact_writer=writer,
    )

    assert isinstance(outcome, Failed)
    assert outcome.code == "artifact_persistence_failed"
    assert outcome.handler_execution_count == 1
    assert writer.prepared == ["first.txt", "second.txt"]
    assert writer.rolled_back == ["ref-first"]


def test_artifact_persistence_rejects_and_rolls_back_wrong_digest(tmp_path, monkeypatch):
    ontology_dir = _write_package(tmp_path)
    service_module = _patch_manifest(monkeypatch, ontology_dir)

    def execute_handler(**kwargs):
        artifact_dir = Path(kwargs["env_overrides"]["RUN_ARTIFACT_DIR"])
        (artifact_dir / "result.txt").write_text("authoritative", encoding="utf-8")
        return json.dumps({"value": "ok"})

    class WrongDigestWriter:
        def __init__(self):
            self.rolled_back = []

        def __call__(self, _name, _text, media_type):
            return {
                "output_ref": "wrong-digest-ref",
                "content_type": media_type,
                "sha256": "sha256:" + "0" * 64,
            }

        def rollback(self, output_refs):
            self.rolled_back.extend(output_refs)

    writer = WrongDigestWriter()
    monkeypatch.setattr(service_module, "handle_execute_script_path", execute_handler)

    outcome = DeterministicExecutionService().execute(
        _request(arguments={"code": "A"}),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
        artifact_writer=writer,
    )

    assert isinstance(outcome, Failed)
    assert outcome.code == "artifact_persistence_failed"
    assert outcome.handler_execution_count == 1
    assert writer.rolled_back == ["wrong-digest-ref"]


def test_artifact_names_are_nfkc_unique_before_persistence(tmp_path, monkeypatch):
    ontology_dir = _write_package(tmp_path)
    service_module = _patch_manifest(monkeypatch, ontology_dir)

    def execute_handler(**kwargs):
        artifact_dir = Path(kwargs["env_overrides"]["RUN_ARTIFACT_DIR"])
        (artifact_dir / "Ａ.txt").write_text("first", encoding="utf-8")
        (artifact_dir / "A.txt").write_text("second", encoding="utf-8")
        return json.dumps({"value": "ok"})

    stored = []
    monkeypatch.setattr(service_module, "handle_execute_script_path", execute_handler)

    outcome = DeterministicExecutionService().execute(
        _request(arguments={"code": "A"}),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
        artifact_writer=lambda name, text, media: stored.append((name, text, media))
        or {"output_ref": "unexpected"},
    )

    assert isinstance(outcome, Failed)
    assert outcome.code == "artifact_policy_violation"
    assert outcome.handler_execution_count == 1
    assert stored == []


def test_invalid_handler_json_fails_closed(tmp_path, monkeypatch):
    ontology_dir = _write_package(tmp_path)
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    monkeypatch.setattr(
        service_module,
        "handle_execute_script_path",
        lambda **_kwargs: "not-json",
    )

    outcome = DeterministicExecutionService().execute(
        _request(arguments={"code": "A"}),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(outcome, Failed)
    assert outcome.code == "invalid_json_output"
    assert outcome.handler_execution_count == 1


def test_duplicate_handler_json_keys_fail_closed(tmp_path, monkeypatch):
    ontology_dir = _write_package(tmp_path)
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    monkeypatch.setattr(
        service_module,
        "handle_execute_script_path",
        lambda **_kwargs: '{"value":"first","value":"second"}',
    )

    outcome = DeterministicExecutionService().execute(
        _request(arguments={"code": "A"}),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(outcome, Failed)
    assert outcome.code == "invalid_json_output"
    assert outcome.handler_execution_count == 1


@pytest.mark.parametrize("output", ["NaN", '{"value": NaN}', '{"value": Infinity}'])
def test_non_standard_json_constants_fail_closed(tmp_path, monkeypatch, output):
    ontology_dir = _write_package(tmp_path)
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    monkeypatch.setattr(
        service_module,
        "handle_execute_script_path",
        lambda **_kwargs: output,
    )

    outcome = DeterministicExecutionService().execute(
        _request(arguments={"code": "A"}),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(outcome, Failed)
    assert outcome.code == "invalid_json_output"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_standard_json_argument_numbers_are_rejected_before_scripts(
    tmp_path,
    monkeypatch,
    value,
):
    ontology_dir = _write_package(tmp_path, parser=True)
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    calls = []
    monkeypatch.setattr(
        service_module,
        "handle_execute_script_path",
        lambda **kwargs: calls.append(kwargs) or json.dumps({"value": "unexpected"}),
    )

    outcome = DeterministicExecutionService().execute(
        _request(arguments={"code": value}),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(outcome, Failed)
    assert outcome.code == "invalid_arguments"
    assert calls == []


def test_parser_arguments_are_overridden_by_explicit_arguments(tmp_path, monkeypatch):
    ontology_dir = _write_package(tmp_path, parser=True)
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    calls = []

    def execute(**kwargs):
        calls.append(kwargs)
        if kwargs["caller"] == "deterministic_parser":
            return json.dumps({"matched": True, "arguments": {"code": "parser"}, "issues": []})
        return json.dumps({"value": "ok"})

    monkeypatch.setattr(service_module, "handle_execute_script_path", execute)

    outcome = DeterministicExecutionService().execute(
        _request(arguments={"code": "explicit"}),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(outcome, Completed)
    assert [call["caller"] for call in calls] == ["deterministic_parser", "deterministic_service"]
    assert "explicit" in calls[1]["args"]


def test_parser_receives_explicit_arguments_and_cannot_reopen_resolved_issue(
    tmp_path,
    monkeypatch,
):
    ontology_dir = _write_package(tmp_path, parser=True)
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    calls = []

    def execute(**kwargs):
        calls.append(kwargs)
        if kwargs["caller"] == "deterministic_parser":
            payload = json.loads(kwargs["args"][1])
            assert payload["known_arguments"] == {"code": "explicit"}
            return json.dumps({
                "matched": True,
                "arguments": {},
                "issues": [{"field": "code", "message": "ambiguous"}],
            })
        return json.dumps({"value": "ok"})

    monkeypatch.setattr(service_module, "handle_execute_script_path", execute)

    outcome = DeterministicExecutionService().execute(
        _request(arguments={"code": "explicit"}),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(outcome, Completed)
    assert [call["caller"] for call in calls] == [
        "deterministic_parser",
        "deterministic_service",
    ]


def test_parser_missing_contract_fields_fails_closed(tmp_path, monkeypatch):
    ontology_dir = _write_package(tmp_path, parser=True)
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    calls = []

    def execute(**kwargs):
        calls.append(kwargs)
        return "{}"

    monkeypatch.setattr(service_module, "handle_execute_script_path", execute)

    outcome = DeterministicExecutionService().execute(
        _request(arguments={"code": "explicit"}),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(outcome, Failed)
    assert outcome.code == "parser_invalid_output"
    assert [call["caller"] for call in calls] == ["deterministic_parser"]


def test_runtime_owned_request_argument_is_rejected_before_parser_or_handler(
    tmp_path,
    monkeypatch,
):
    ontology_dir = _write_package(tmp_path, parser=True)
    _add_runtime_session_parameter(ontology_dir)
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    calls = []
    monkeypatch.setattr(
        service_module,
        "handle_execute_script_path",
        lambda **kwargs: calls.append(kwargs) or json.dumps({"value": "unexpected"}),
    )

    outcome = DeterministicExecutionService().execute(
        _request(arguments={"code": "A", "session_id": "forged-session"}),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(outcome, Failed)
    assert outcome.code == "invalid_arguments"
    assert outcome.internal_detail == "session_id"
    assert calls == []


def test_runtime_owned_canonical_field_is_rejected_when_cli_name_is_aliased(
    tmp_path,
    monkeypatch,
):
    ontology_dir = _write_package(tmp_path, parser=True)
    _add_aliased_runtime_session_parameter(ontology_dir)
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    calls = []
    monkeypatch.setattr(
        service_module,
        "handle_execute_script_path",
        lambda **kwargs: calls.append(kwargs) or json.dumps({"value": "unexpected"}),
    )

    outcome = DeterministicExecutionService().execute(
        _request(arguments={"code": "A", "session_id": "forged-session"}),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(outcome, Failed)
    assert outcome.code == "invalid_arguments"
    assert outcome.internal_detail == "session_id"
    assert calls == []


@pytest.mark.parametrize(
    "parser_payload",
    [
        {"matched": True, "arguments": {"session_id": "forged"}, "issues": []},
        {
            "matched": True,
            "arguments": {},
            "issues": [{"field": "session_id", "message": "provide session"}],
        },
    ],
)
def test_runtime_owned_parser_output_is_rejected(
    tmp_path,
    monkeypatch,
    parser_payload,
):
    ontology_dir = _write_package(tmp_path, parser=True)
    _add_runtime_session_parameter(ontology_dir)
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    calls = []

    def execute(**kwargs):
        calls.append(kwargs)
        return json.dumps(parser_payload)

    monkeypatch.setattr(service_module, "handle_execute_script_path", execute)

    outcome = DeterministicExecutionService().execute(
        _request(arguments={"code": "A"}),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(outcome, Failed)
    assert outcome.code == "parser_invalid_output"
    assert outcome.internal_detail == "session_id"
    assert [call["caller"] for call in calls] == ["deterministic_parser"]


def test_runtime_owned_parameter_is_injected_but_excluded_from_arguments_hash(
    tmp_path,
    monkeypatch,
):
    ontology_dir = _write_package(tmp_path)
    _add_runtime_session_parameter(ontology_dir)
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    calls = []

    def execute(**kwargs):
        calls.append(kwargs)
        return json.dumps({"value": "ok"})

    monkeypatch.setattr(service_module, "handle_execute_script_path", execute)

    outcome = DeterministicExecutionService().execute(
        _request(arguments={"code": "A"}),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(outcome, Completed)
    assert calls[0]["args"] == [
        "--code",
        "A",
        "--session-id",
        "session-1",
    ]
    assert outcome.envelope["execution"]["arguments_sha256"] == canonical_sha256(
        {"code": "A"}
    )


def test_runtime_owned_checkpoint_argument_is_rejected_as_stale(tmp_path, monkeypatch):
    ontology_dir = _write_package(tmp_path)
    _add_runtime_session_parameter(ontology_dir)
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    calls = []
    monkeypatch.setattr(
        service_module,
        "handle_execute_script_path",
        lambda **kwargs: calls.append(kwargs) or json.dumps({"value": "unexpected"}),
    )
    service = DeterministicExecutionService()

    awaiting = service.execute(
        _request(context={"supports_user_interaction": True}),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )
    assert isinstance(awaiting, AwaitingInput)
    checkpoint = dict(awaiting.checkpoint)
    checkpoint["arguments"] = {"session_id": "forged-session"}

    resumed = service.execute(
        _request(checkpoint=checkpoint, response={"value": "A"}),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(resumed, Failed)
    assert resumed.code == "stale_checkpoint"
    assert calls == []


def test_runtime_owned_input_schema_field_fails_before_prompt_or_handler(
    tmp_path,
    monkeypatch,
):
    ontology_dir = _write_package(tmp_path)
    _add_runtime_session_parameter(ontology_dir, declare_in_input_schema=True)
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    calls = []
    monkeypatch.setattr(
        service_module,
        "handle_execute_script_path",
        lambda **kwargs: calls.append(kwargs) or json.dumps({"value": "unexpected"}),
    )

    outcome = DeterministicExecutionService().execute(
        _request(arguments={"code": "A"}, context={"supports_user_interaction": True}),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(outcome, Failed)
    assert outcome.code == "execution_schema_invalid"
    assert outcome.internal_detail == "session_id"
    assert calls == []


def test_runtime_owned_input_schema_field_inside_all_of_fails_closed(
    tmp_path,
    monkeypatch,
):
    ontology_dir = _write_package(tmp_path)
    _add_runtime_session_parameter(ontology_dir)
    schema_path = ontology_dir / "schemas" / "query.input.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema["allOf"] = [
        {
            "properties": {"session_id": {"type": "string"}},
            "required": ["session_id"],
        }
    ]
    schema_path.write_text(json.dumps(schema), encoding="utf-8")
    validate_and_lock_deterministic_operations(ontology_dir)
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    calls = []
    monkeypatch.setattr(
        service_module,
        "handle_execute_script_path",
        lambda **kwargs: calls.append(kwargs) or json.dumps({"value": "unexpected"}),
    )

    outcome = DeterministicExecutionService().execute(
        _request(arguments={"code": "A"}),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(outcome, Failed)
    assert outcome.code == "execution_schema_invalid"
    assert outcome.internal_detail == "session_id"
    assert calls == []


def test_runtime_owned_input_schema_field_inside_dynamic_ref_fails_closed(
    tmp_path,
    monkeypatch,
):
    ontology_dir = _write_package(tmp_path)
    _add_runtime_session_parameter(ontology_dir)
    schema_path = ontology_dir / "schemas" / "query.input.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema["allOf"] = [{"$dynamicRef": "#/$defs/runtimeFields"}]
    schema["$defs"] = {
        "runtimeFields": {
            "properties": {"session_id": {"type": "string"}},
            "required": ["session_id"],
        }
    }
    schema_path.write_text(json.dumps(schema), encoding="utf-8")
    validate_and_lock_deterministic_operations(ontology_dir)
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    calls = []
    monkeypatch.setattr(
        service_module,
        "handle_execute_script_path",
        lambda **kwargs: calls.append(kwargs) or json.dumps({"value": "unexpected"}),
    )

    outcome = DeterministicExecutionService().execute(
        _request(arguments={"code": "A"}),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(outcome, Failed)
    assert outcome.code == "execution_schema_invalid"
    assert outcome.internal_detail == "session_id"
    assert calls == []


def test_negated_runtime_owned_field_does_not_become_a_caller_field(
    tmp_path,
    monkeypatch,
):
    ontology_dir = _write_package(tmp_path)
    _add_runtime_session_parameter(ontology_dir)
    schema_path = ontology_dir / "schemas" / "query.input.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema["not"] = {"required": ["session_id"]}
    schema_path.write_text(json.dumps(schema), encoding="utf-8")
    validate_and_lock_deterministic_operations(ontology_dir)
    service_module = _patch_manifest(monkeypatch, ontology_dir)
    calls = []

    def execute(**kwargs):
        calls.append(kwargs)
        return json.dumps({"value": "ok"})

    monkeypatch.setattr(service_module, "handle_execute_script_path", execute)

    outcome = DeterministicExecutionService().execute(
        _request(arguments={"code": "A"}),
        enabled=True,
        project_root=str(tmp_path),
        timeout_seconds=30,
    )

    assert isinstance(outcome, Completed)
    assert calls[0]["args"] == [
        "--code",
        "A",
        "--session-id",
        "session-1",
    ]
