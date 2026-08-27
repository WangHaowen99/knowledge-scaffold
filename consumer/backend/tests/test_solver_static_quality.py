from __future__ import annotations

import ast
import binascii
import json
from pathlib import Path
from urllib.error import HTTPError, URLError


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]

EXCEPTION_TARGETS = (
    "consumer/backend/api/protocols/mcp/workflow_bridge.py",
    "consumer/backend/api/routes/ontologies.py",
    "consumer/backend/integration/solver_coordination_runtime.py",
    "consumer/backend/integration/solver_fact_providers.py",
    "consumer/backend/scripts/run_controlled_solver_validation.py",
    "consumer/backend/solver_coordination/candidate_store.py",
    "consumer/backend/solver_coordination/connectors/ortools.py",
    "consumer/backend/solver_coordination/job_store.py",
    "consumer/backend/solver_coordination/validation_artifacts.py",
    "consumer/backend/solver_coordination/verification/service.py",
    "contracts/solver/tck/admission.py",
    "contracts/solver/tck/runner.py",
    "contracts/solver/validation.py",
    "contracts/solver_services/ortools/validation.py",
)
ASSERT_TARGETS = (
    "consumer/backend/api/routes/ontologies.py",
    "contracts/solver/tck/runner.py",
    "solvers/ortools/store.py",
)
NAMED_EXPRESSION_TARGETS = (
    "consumer/backend/api/routes/solver_coordination.py",
    "consumer/backend/solver_coordination/compiler.py",
    "consumer/backend/solver_coordination/job_store.py",
)
LAMBDA_ASSIGNMENT_TARGETS = (
    "consumer/backend/solver_coordination/service.py",
)
COMPLEX_COMPREHENSION_OWNERS = (
    (
        "consumer/backend/agent/core/shared/solver_route_guard.py",
        None,
        "resolve_solver_route_requirement",
    ),
    (
        "consumer/backend/agent/core/shared/solver_route_guard.py",
        None,
        "restrict_tools_for_solver_route",
    ),
    (
        "consumer/backend/agent/tools/registry.py",
        None,
        "get_tool_specs_for_caller",
    ),
    ("consumer/backend/agent/tools/registry.py", None, "get_tool_spec"),
    (
        "consumer/backend/agent/runtime/service.py",
        None,
        "_waiting_solver_checkpoint_from_state",
    ),
    (
        "consumer/backend/api/routes/solver_coordination.py",
        None,
        "create_validation_run_api",
    ),
    (
        "consumer/backend/api/routes/solver_coordination.py",
        None,
        "_public_history_item",
    ),
    (
        "consumer/backend/api/routes/solver_coordination.py",
        None,
        "get_solver_history_candidate_results_api",
    ),
    (
        "consumer/backend/integration/solver_coordination_runtime.py",
        "RuntimeSolverCapabilityResolver",
        "resolve",
    ),
    (
        "consumer/backend/integration/solver_coordination_runtime.py",
        "RuntimeSolverCapabilityResolver",
        "_load_model",
    ),
    (
        "consumer/backend/integration/solver_fact_providers.py",
        "PackageEvaluationFactProvider",
        "resolve_facts",
    ),
    (
        "consumer/backend/integration/solver_fact_providers.py",
        "PackageValidationFixtureFactProvider",
        "resolve_facts",
    ),
    (
        "consumer/backend/integration/solver_route_fact_worker.py",
        None,
        "_refinery_route_context",
    ),
    (
        "consumer/backend/persistence/execution_store.py",
        "ExecutionStore",
        "save_waiting_solver_checkpoint",
    ),
    (
        "consumer/backend/persistence/execution_store.py",
        "ExecutionStore",
        "put_solver_tool_result",
    ),
    (
        "consumer/backend/persistence/execution_store.py",
        "ExecutionStore",
        "complete_waiting_solver_checkpoint",
    ),
    (
        "consumer/backend/solver_coordination/candidate_results.py",
        None,
        "build_candidate_results",
    ),
    (
        "consumer/backend/solver_coordination/capability_catalog.py",
        None,
        "load_solver_capability_catalog",
    ),
    (
        "consumer/backend/ontology/ontology_catalog.py",
        None,
        "_format_solver_catalog",
    ),
    (
        "consumer/backend/solver_coordination/connectors/ortools.py",
        None,
        "_supported_family",
    ),
    (
        "consumer/backend/solver_coordination/profiles/ordered_binary_route.py",
        "OrderedBinaryRouteInputAdapter",
        "adapt",
    ),
    (
        "consumer/backend/solver_coordination/validation_resolver.py",
        "RunArtifactValidationContextResolver",
        "resolve",
    ),
    (
        "consumer/backend/solver_coordination/verification/ordered_binary_route.py",
        "OrderedBinaryRouteIndependentVerifier",
        "_validate_domain_certificate",
    ),
    (
        "consumer/backend/scripts/run_controlled_solver_validation.py",
        None,
        "_validate_manifest",
    ),
    (
        "consumer/backend/solver_coordination/validation_store.py",
        None,
        "_content_payload",
    ),
    ("consumer/backend/solver_coordination/service.py", None, "recover_job"),
    (
        "consumer/backend/solver_coordination/service.py",
        None,
        "_durable_authoritative_event_result",
    ),
    (
        "consumer/backend/solver_coordination/service.py",
        None,
        "_load_frozen_artifact",
    ),
    (
        "consumer/backend/solver_coordination/service.py",
        None,
        "_verify_artifact_reference",
    ),
    (
        "contracts/solver/tck/runner.py",
        "ConnectorTckRunner",
        "_check_capabilities",
    ),
    ("solvers/ortools/tests/test_nine_item_golden.py", None, "_request"),
)
CONTROL_COMPLEXITY_TARGETS = (
    "consumer/backend/agent/core/orchestrator.py",
    "consumer/backend/agent/core/shared/solver_route_guard.py",
    "consumer/backend/agent/tools/solver.py",
    "consumer/backend/api/routes/ontologies.py",
    "consumer/backend/api/routes/solver_coordination.py",
    "consumer/backend/api/routes/workflows.py",
    "consumer/backend/api/protocols/mcp/workflow_bridge.py",
    "consumer/backend/integration/solver_coordination_runtime.py",
    "consumer/backend/integration/solver_fact_providers.py",
    "consumer/backend/integration/solver_route_fact_worker.py",
    "consumer/backend/ontology/ontology_catalog.py",
    "consumer/backend/ontology/package_import.py",
    "consumer/backend/persistence/execution_store.py",
    "consumer/backend/scripts/run_controlled_solver_validation.py",
    "consumer/backend/solver_coordination/bindings.py",
    "consumer/backend/solver_coordination/candidate_results.py",
    "consumer/backend/solver_coordination/compiler.py",
    "consumer/backend/solver_coordination/connectors/endpoint_policy.py",
    "consumer/backend/solver_coordination/connectors/http_transport.py",
    "consumer/backend/solver_coordination/connectors/ortools.py",
    "consumer/backend/solver_coordination/input_binding.py",
    "consumer/backend/solver_coordination/job_orchestrator.py",
    "consumer/backend/solver_coordination/job_store.py",
    "consumer/backend/solver_coordination/operation_validation.py",
    "consumer/backend/solver_coordination/presentation.py",
    "consumer/backend/solver_coordination/profiles/input_adapter.py",
    "consumer/backend/solver_coordination/profiles/ordered_binary_route.py",
    "consumer/backend/solver_coordination/service.py",
    "consumer/backend/solver_coordination/validation_resolver.py",
    "consumer/backend/solver_coordination/validation_store.py",
    "consumer/backend/solver_coordination/verification/ordered_binary_route.py",
    "contracts/solver/tck/admission.py",
    "contracts/solver/tck/runner.py",
    "contracts/solver_services/ortools/validation.py",
    "solvers/ortools/config.py",
    "solvers/ortools/engine/refinery_route.py",
)
DIRECT_RETHROW_TARGETS = (
    "consumer/backend/api/routes/solver_coordination.py",
    "consumer/backend/integration/solver_fact_providers.py",
    "consumer/backend/scripts/run_controlled_solver_validation.py",
    "consumer/backend/solver_coordination/candidate_store.py",
    "consumer/backend/solver_coordination/compiler.py",
    "consumer/backend/solver_coordination/operation_validation.py",
    "consumer/backend/solver_coordination/presentation.py",
    "consumer/backend/solver_coordination/profiles/input_adapter.py",
    "consumer/backend/solver_coordination/service.py",
    "consumer/backend/solver_coordination/verification/ordered_binary_route.py",
    "consumer/backend/solver_coordination/verification/service.py",
    "contracts/solver/validation.py",
    "contracts/solver_services/ortools/validation.py",
)
PASS_HANDLER_TARGETS = (
    "consumer/backend/agent/core/orchestrator.py",
    "consumer/backend/persistence/execution_store.py",
    "consumer/backend/solver_coordination/connectors/endpoint_policy.py",
    "consumer/backend/solver_coordination/connectors/ortools.py",
    "consumer/backend/solver_coordination/job_orchestrator.py",
    "consumer/backend/solver_coordination/service.py",
    "consumer/backend/solver_coordination/validation_store.py",
)
PRINT_TARGETS = (
    "consumer/backend/integration/solver_route_fact_worker.py",
    "consumer/backend/scripts/run_controlled_solver_validation.py",
    "contracts/solver/tck/__main__.py",
)
SYS_PATH_TARGETS = (
    "consumer/backend/integration/solver_route_fact_worker.py",
    "solvers/ortools/tests/conftest.py",
)
EXCEPTION_ORDER_TARGETS = (
    "consumer/backend/api/routes/solver_coordination.py",
)
EXCEPTION_SUBCLASS_PAIRS = (
    ("JobStoreError", "ValueError"),
)

EXCEPTION_TYPES = {
    "FileNotFoundError": FileNotFoundError,
    "ImportError": ImportError,
    "KeyError": KeyError,
    "LookupError": LookupError,
    "ModuleNotFoundError": ModuleNotFoundError,
    "OSError": OSError,
    "TimeoutError": TimeoutError,
    "UnicodeError": UnicodeError,
    "ValueError": ValueError,
    "binascii.Error": binascii.Error,
    "json.JSONDecodeError": json.JSONDecodeError,
    "HTTPError": HTTPError,
    "URLError": URLError,
}


def _tree(relative_path: str) -> ast.AST:
    source = (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")
    return ast.parse(source, filename=relative_path)


def _qualified_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        owner = _qualified_name(node.value)
        return "{}.{}".format(owner, node.attr) if owner else node.attr
    return ""


def _boolean_term_count(node: ast.expr) -> int:
    if isinstance(node, ast.BoolOp):
        return sum(_boolean_term_count(value) for value in node.values)
    return 1


def _function(tree: ast.AST, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    ]
    assert len(matches) == 1
    return matches[0]


def _owned_function(
    tree: ast.AST,
    owner_name: str | None,
    function_name: str,
) -> ast.FunctionDef | ast.AsyncFunctionDef:
    if owner_name is None:
        return _function(tree, function_name)
    owners = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == owner_name
    ]
    assert len(owners) == 1
    matches = [
        node
        for node in owners[0].body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == function_name
    ]
    assert len(matches) == 1
    return matches[0]


def test_solver_exception_handlers_do_not_repeat_parent_and_child_types():
    duplicates: list[str] = []
    for relative_path in EXCEPTION_TARGETS:
        for node in ast.walk(_tree(relative_path)):
            if not isinstance(node, ast.ExceptHandler) or not isinstance(
                node.type, ast.Tuple
            ):
                continue
            resolved = [
                (name, EXCEPTION_TYPES[name])
                for item in node.type.elts
                if (name := _qualified_name(item)) in EXCEPTION_TYPES
            ]
            for index, (left_name, left_type) in enumerate(resolved):
                for right_name, right_type in resolved[index + 1 :]:
                    if issubclass(left_type, right_type) or issubclass(
                        right_type, left_type
                    ):
                        duplicates.append(
                            "{}:{} {} / {}".format(
                                relative_path,
                                node.lineno,
                                left_name,
                                right_name,
                            )
                        )
    assert duplicates == []


def test_solver_exception_handlers_catch_subclasses_before_parent_types():
    violations: list[str] = []
    for relative_path in EXCEPTION_ORDER_TARGETS:
        for node in ast.walk(_tree(relative_path)):
            if not isinstance(node, ast.Try):
                continue
            handler_names = [
                {
                    _qualified_name(item)
                    for item in (
                        handler.type.elts
                        if isinstance(handler.type, ast.Tuple)
                        else (handler.type,)
                    )
                    if item is not None
                }
                for handler in node.handlers
            ]
            for child_name, parent_name in EXCEPTION_SUBCLASS_PAIRS:
                child_index = next(
                    (
                        index
                        for index, names in enumerate(handler_names)
                        if child_name in names
                    ),
                    None,
                )
                parent_index = next(
                    (
                        index
                        for index, names in enumerate(handler_names)
                        if parent_name in names
                    ),
                    None,
                )
                if (
                    child_index is not None
                    and parent_index is not None
                    and child_index > parent_index
                ):
                    violations.append(
                        "{}:{} {} after {}".format(
                            relative_path,
                            node.lineno,
                            child_name,
                            parent_name,
                        )
                    )
    assert violations == []


def test_solver_production_paths_do_not_use_assert_statements():
    violations = [
        "{}:{}".format(relative_path, node.lineno)
        for relative_path in ASSERT_TARGETS
        for node in ast.walk(_tree(relative_path))
        if isinstance(node, ast.Assert)
    ]
    assert violations == []


def test_solver_production_paths_avoid_named_expressions():
    violations = [
        "{}:{}".format(relative_path, node.lineno)
        for relative_path in NAMED_EXPRESSION_TARGETS
        for node in ast.walk(_tree(relative_path))
        if isinstance(node, ast.NamedExpr)
    ]
    assert violations == []


def test_solver_production_paths_do_not_assign_lambdas():
    violations = [
        "{}:{}".format(relative_path, node.lineno)
        for relative_path in LAMBDA_ASSIGNMENT_TARGETS
        for node in ast.walk(_tree(relative_path))
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and isinstance(node.value, ast.Lambda)
    ]
    assert violations == []


def test_scanned_functions_use_explicit_loops_instead_of_comprehensions():
    comprehension_types = (
        ast.DictComp,
        ast.GeneratorExp,
        ast.ListComp,
        ast.SetComp,
    )
    violations = [
        "{}:{}:{}".format(relative_path, function_name, node.lineno)
        for relative_path, owner_name, function_name in COMPLEX_COMPREHENSION_OWNERS
        for node in ast.walk(
            _owned_function(_tree(relative_path), owner_name, function_name)
        )
        if isinstance(node, comprehension_types)
    ]
    assert violations == []


def test_validation_result_counts_use_typed_locals():
    function = _function(
        _tree("consumer/backend/solver_coordination/validation_store.py"),
        "_validate_result_payload",
    )
    violations = [
        node.lineno
        for node in ast.walk(function)
        if isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id == "counts"
    ]
    assert violations == []


def test_special_tank_rates_are_not_recovered_through_dict_subscripts():
    function = _function(
        _tree("solvers/ortools/engine/refinery_route.py"),
        "_special_tank_margins",
    )
    violations = [
        node.lineno
        for node in ast.walk(function)
        if isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id == "rates"
    ]
    assert violations == []


def test_recovery_uses_a_typed_fact_payload_local():
    function = _function(
        _tree("consumer/backend/solver_coordination/service.py"),
        "recover_job",
    )
    violations = [
        node.lineno
        for node in ast.walk(function)
        if isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id == "frozen_payloads"
        and isinstance(node.ctx, ast.Load)
    ]
    assert violations == []


def test_solver_service_compile_and_verify_ports_are_typed():
    service = next(
        node
        for node in ast.walk(
            _tree("consumer/backend/solver_coordination/service.py")
        )
        if isinstance(node, ast.ClassDef)
        and node.name == "SolverCoordinationService"
    )
    annotations = {
        node.target.id: _qualified_name(node.annotation)
        for node in service.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
    }
    assert annotations["compiler"] == "ProblemCompiler"
    assert annotations["verifier"] == "IndependentVerifier"


def test_staged_revision_manifest_checks_stay_within_boolean_term_budget():
    function = _function(
        _tree("consumer/backend/api/routes/ontologies.py"),
        "_stage_and_commit_revision_file",
    )
    violations = [
        "{} terms={}".format(node.lineno, _boolean_term_count(node.test))
        for node in ast.walk(function)
        if isinstance(node, ast.If) and _boolean_term_count(node.test) >= 4
    ]
    assert violations == []


def test_solver_control_predicates_stay_within_boolean_term_budget():
    violations = [
        "{}:{} terms={}".format(
            relative_path,
            node.lineno,
            _boolean_term_count(node.test),
        )
        for relative_path in CONTROL_COMPLEXITY_TARGETS
        for node in ast.walk(_tree(relative_path))
        if isinstance(node, (ast.If, ast.IfExp, ast.While))
        and _boolean_term_count(node.test) >= 4
    ]
    assert violations == []


def test_solver_code_does_not_use_general_print_for_protocol_output():
    violations = [
        "{}:{}".format(relative_path, node.lineno)
        for relative_path in PRINT_TARGETS
        for node in ast.walk(_tree(relative_path))
        if isinstance(node, ast.Call) and _qualified_name(node.func) == "print"
    ]
    assert violations == []


def test_solver_code_does_not_mutate_sys_path():
    violations = [
        "{}:{}".format(relative_path, node.lineno)
        for relative_path in SYS_PATH_TARGETS
        for node in ast.walk(_tree(relative_path))
        if isinstance(node, ast.Call)
        and _qualified_name(node.func) in {"sys.path.append", "sys.path.insert"}
    ]
    assert violations == []


def test_solver_code_has_no_direct_exception_rethrows():
    violations = [
        "{}:{}".format(relative_path, node.lineno)
        for relative_path in DIRECT_RETHROW_TARGETS
        for node in ast.walk(_tree(relative_path))
        if isinstance(node, ast.ExceptHandler)
        and len(node.body) == 1
        and isinstance(node.body[0], ast.Raise)
        and node.body[0].exc is None
    ]
    assert violations == []


def test_solver_exception_handlers_do_not_silently_pass():
    violations = [
        "{}:{}".format(relative_path, node.lineno)
        for relative_path in PASS_HANDLER_TARGETS
        for node in ast.walk(_tree(relative_path))
        if isinstance(node, ast.ExceptHandler)
        and len(node.body) == 1
        and isinstance(node.body[0], ast.Pass)
    ]
    assert violations == []


def test_recovery_worker_return_annotation_is_not_a_string_literal():
    node = _function(
        _tree("consumer/backend/integration/solver_coordination_runtime.py"),
        "start",
    )
    assert not isinstance(node.returns, ast.Constant)


def test_controlled_validation_read_only_open_declares_a_safe_mode():
    node = _function(
        _tree("consumer/backend/scripts/run_controlled_solver_validation.py"),
        "_read_bounded_file",
    )
    violations = [
        call.lineno
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and _qualified_name(call.func) == "os.open"
        and len(call.args) < 3
    ]
    assert violations == []


def test_solver_service_uses_a_hardened_app_factory():
    violations = [
        node.lineno
        for node in ast.walk(_tree("solvers/ortools/app.py"))
        if isinstance(node, ast.Call) and _qualified_name(node.func) == "Flask"
    ]
    assert violations == []


def test_package_evaluator_subprocess_declares_isolation_controls():
    calls = [
        node
        for node in ast.walk(
            _tree("consumer/backend/integration/solver_fact_providers.py")
        )
        if isinstance(node, ast.Call)
        and _qualified_name(node.func) == "subprocess.run"
    ]
    assert len(calls) == 1
    call = calls[0]
    keywords = {keyword.arg: keyword.value for keyword in call.keywords}
    assert isinstance(keywords.get("shell"), ast.Constant)
    assert keywords["shell"].value is False
    assert {"input", "env", "timeout", "capture_output", "check"}.issubset(
        keywords
    )
    assert isinstance(call.args[0], ast.List)
    assert _qualified_name(call.args[0].elts[0]) == "sys.executable"
