"""Fail-closed routing for explicitly named ontology Solver Operations."""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.tools.solver import build_solver_tool_specs
from ontology.ontology_catalog import build_root_payload, get_ontology_manifest


REQUIRED_SOLVER_OPERATION_CONTEXT_KEY = "_required_solver_operation_key"
REQUIRED_SOLVER_TOOL_CONTEXT_KEY = "_required_solver_tool_name"
SUPPRESS_SOLVER_TOOLS_CONTEXT_KEY = "_suppress_solver_tools"

_EXPLANATION_MARKERS = (
    "describe",
    "documentation",
    "explain",
    "how do",
    "how does",
    "how is",
    "what is",
    "why",
    "介绍",
    "原理",
    "为什么",
    "什么是",
    "说明",
    "解释",
)
_EXECUTION_MARKERS = (
    "calculate",
    "execute",
    "run solver",
    "solve",
    "候选方案",
    "执行",
    "求解",
    "生产规划",
    "生成方案",
    "调用求解",
    "路线方案",
)
_EXPLICIT_SOLVER_MARKERS = (
    "run solver",
    "solve",
    "solver",
    "候选方案",
    "最优",
    "优化",
    "求解",
    "生成方案",
    "调用求解",
    "路线方案",
    "推荐方案",
)
_EVALUATION_MARKERS = (
    "evaluate",
    "statistics",
    "summarize",
    "validate",
    "check whether",
    "overload",
    "capacity check",
    "统计",
    "汇总",
    "校验",
    "检查",
    "评估",
    "是否过载",
    "是否超容",
    "负荷情况",
    "罐容情况",
)
_PUBLIC_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_SOLVER_TOOL_NAME_RE = re.compile(
    r"^solve(?:_[a-z0-9_]+_[0-9a-f]{8})?$"
)


@dataclass(frozen=True, slots=True)
class SolverRouteRequirement:
    operation_key: str
    tool_name: str
    direct_arguments: dict[str, Any] | None = None


class SolverRouteGuardError(RuntimeError):
    def __init__(
        self,
        code: str,
        public_message: str,
        *,
        phase: str = "",
        retryable: bool = False,
    ) -> None:
        super().__init__(public_message)
        self.code = str(code or "solver_route_guard_failed")
        self.public_message = str(public_message or "Solver route guard failed")
        self.phase = str(phase or "").strip()
        self.retryable = bool(retryable)


class SolverRouteExecutionGuard:
    """Require one authoritative result before a solver-routed turn succeeds."""

    def __init__(self, requirement: SolverRouteRequirement) -> None:
        self.requirement = requirement
        self.authoritative_result: dict[str, Any] | None = None
        self.terminal_error_code: str | None = None
        self.terminal_error_phase = ""
        self.terminal_error_retryable = False

    def observe(self, envelope: Mapping[str, Any] | None) -> None:
        event = envelope if isinstance(envelope, Mapping) else {}
        wrapped = event.get("data")
        if event.get("event") == "agent.v2" and isinstance(wrapped, Mapping):
            event = wrapped
        event_type = str(event.get("type") or "").strip()
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            return
        if str(payload.get("tool_name") or "").strip() != self.requirement.tool_name:
            return
        if event_type == "solver.result.authoritative":
            if payload.get("authoritative") is True:
                self.authoritative_result = dict(payload)
            return
        if event_type == "tool.finished" and str(payload.get("status") or "").strip() == "error":
            terminal_error = _public_solver_error(payload)
            if terminal_error is not None:
                (
                    self.terminal_error_code,
                    self.terminal_error_phase,
                    self.terminal_error_retryable,
                ) = terminal_error

    def ensure_terminal(self, *, allow_awaiting_input: bool) -> None:
        if self.authoritative_result is not None or allow_awaiting_input:
            return
        if self.terminal_error_code is not None:
            raise SolverRouteGuardError(
                self.terminal_error_code,
                "The selected Solver Operation failed with error code {}.".format(
                    self.terminal_error_code
                ),
                phase=self.terminal_error_phase,
                retryable=self.terminal_error_retryable,
            )
        raise SolverRouteGuardError(
            "solver_authoritative_result_missing",
            "The selected Solver Operation did not produce an authoritative result.",
        )


def _is_catalog_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes))


def resolve_solver_route_requirement(
    *,
    user_request: str,
    arguments: Mapping[str, Any] | None = None,
    selected_ontology_id: str | None,
    selected_ontology_version: str | None,
    workspace_id: str | None,
    root_payload: Mapping[str, Any] | None = None,
    solver_capabilities: Sequence[Mapping[str, Any]] | None = None,
) -> SolverRouteRequirement | None:
    package_id = str(selected_ontology_id or "").strip()
    package_revision = str(selected_ontology_version or "").strip()
    workspace = str(workspace_id or "").strip()
    query = str(user_request or "").strip()
    if not package_id or not package_revision:
        return None
    if not workspace or not query:
        return None
    if _is_explanation_only(query):
        return None

    payload = (
        root_payload
        if isinstance(root_payload, Mapping)
        else _load_solver_root_payload(
            package_id=package_id,
            package_revision=package_revision,
            workspace_id=workspace,
        )
    )
    operations = payload.get("operations_catalog")
    # Progressive root contexts expose lightweight summaries.  A preflight
    # snapshot may still carry the legacy/full catalog for compatibility; use
    # either representation for routing, while ToolCard construction remains
    # package-bound and schema-aware.
    solvers = solver_capabilities
    if not _is_catalog_sequence(solvers) or not solvers:
        # A legacy preflight/root payload may still carry the full catalog;
        # prefer it over the summary because it can construct a strict card.
        solvers = payload.get("solver_catalog")
    if not _is_catalog_sequence(solvers) or not solvers:
        solvers = payload.get("solver_summary")
    if not _is_catalog_sequence(operations) or not _is_catalog_sequence(solvers):
        return None

    operation_rows: dict[str, Mapping[str, Any]] = {}
    for row in operations:
        if not isinstance(row, Mapping):
            continue
        operation_key = _operation_key(row)
        if operation_key:
            operation_rows[operation_key] = row
    normalized_query = query.casefold()
    solver_rows: list[tuple[str, Mapping[str, Any]]] = []
    matches: list[tuple[int, str, Mapping[str, Any]]] = []
    for raw_solver in solvers:
        if not isinstance(raw_solver, Mapping) or raw_solver.get("enabled", True) is False:
            continue
        operation_key = str(raw_solver.get("operation_key") or "").strip()
        if not operation_key:
            continue
        solver_rows.append((operation_key, raw_solver))
        operation = operation_rows.get(operation_key, {})
        terms = _operation_match_terms(operation_key, operation)
        matched_lengths: list[int] = []
        for term in terms:
            if _operation_term_matches(normalized_query, term):
                matched_lengths.append(len(term))
        if matched_lengths:
            matches.append((max(matched_lengths), operation_key, raw_solver))

    operation_key: str
    capability: Mapping[str, Any]
    spec: Any
    if matches:
        if not _has_execution_intent(query) and not arguments:
            return None
        matches.sort(key=lambda item: (-item[0], item[1]))
        top_length = matches[0][0]
        top_matches: list[tuple[int, str, Mapping[str, Any]]] = []
        for item in matches:
            if item[0] == top_length:
                top_matches.append(item)
        if len(top_matches) != 1:
            raise SolverRouteGuardError(
                "solver_operation_ambiguous",
                "The request matches more than one Solver Operation.",
            )
        _, operation_key, capability = top_matches[0]
        # Route discovery may use a summary, but a strict ToolCard must come
        # from the admitted/private capability set.  If only summary metadata
        # is available, defer schema construction to the normal tool registry
        # instead of treating the summary as a full contract.
        try:
            spec = _build_solver_spec(capability)
        except SolverRouteGuardError:
            if _is_catalog_sequence(solver_capabilities):
                raise
            return None
        if not _has_execution_intent(query) and not _arguments_match_solver_spec(
            arguments, spec
        ):
            return None
    else:
        argument_matches = _solver_argument_matches(solver_rows, arguments)
        if len(argument_matches) > 1:
            raise SolverRouteGuardError(
                "solver_operation_ambiguous",
                "The structured arguments match more than one Solver Operation.",
            )
        if argument_matches:
            operation_key, capability, spec = argument_matches[0]
        elif _has_implicit_solver_intent(query) and len(solver_rows) == 1:
            operation_key, capability = solver_rows[0]
            try:
                spec = _build_solver_spec(capability)
            except SolverRouteGuardError:
                return None
        else:
            return None

    direct_arguments = None
    if _arguments_match_solver_spec(arguments, spec):
        direct_arguments = copy.deepcopy(dict(arguments or {}))
    return SolverRouteRequirement(
        operation_key=operation_key,
        tool_name=str(spec.name).strip(),
        direct_arguments=direct_arguments,
    )


def bind_solver_route_requirement(
    context: Mapping[str, Any] | None,
    requirement: SolverRouteRequirement | None,
    *,
    suppress_solver_tools: bool = False,
) -> dict[str, Any]:
    result = dict(context or {})
    result.pop(REQUIRED_SOLVER_OPERATION_CONTEXT_KEY, None)
    result.pop(REQUIRED_SOLVER_TOOL_CONTEXT_KEY, None)
    result.pop(SUPPRESS_SOLVER_TOOLS_CONTEXT_KEY, None)
    if requirement is not None:
        result[REQUIRED_SOLVER_OPERATION_CONTEXT_KEY] = requirement.operation_key
        result[REQUIRED_SOLVER_TOOL_CONTEXT_KEY] = requirement.tool_name
    elif suppress_solver_tools:
        result[SUPPRESS_SOLVER_TOOLS_CONTEXT_KEY] = True
    return result


def solver_route_requirement_from_state(
    state: Mapping[str, Any] | None,
) -> SolverRouteRequirement | None:
    state_mapping = state if isinstance(state, Mapping) else {}
    user_context = state_mapping.get("user_context")
    user_context = user_context if isinstance(user_context, Mapping) else {}
    operation_key = str(
        user_context.get(REQUIRED_SOLVER_OPERATION_CONTEXT_KEY) or ""
    ).strip()
    tool_name = str(user_context.get(REQUIRED_SOLVER_TOOL_CONTEXT_KEY) or "").strip()
    if not operation_key or not tool_name:
        return None
    return SolverRouteRequirement(operation_key=operation_key, tool_name=tool_name)


def solver_tools_suppressed_from_state(
    state: Mapping[str, Any] | None,
) -> bool:
    state_mapping = state if isinstance(state, Mapping) else {}
    user_context = state_mapping.get("user_context")
    user_context = user_context if isinstance(user_context, Mapping) else {}
    return user_context.get(SUPPRESS_SOLVER_TOOLS_CONTEXT_KEY) is True


def filter_solver_history_messages(
    messages: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    leading_messages: list[dict[str, Any]] = []
    turns: list[list[dict[str, Any]]] = []
    current_turn: list[dict[str, Any]] | None = None
    for raw_message in messages:
        if not isinstance(raw_message, Mapping):
            continue
        message = copy.deepcopy(dict(raw_message))
        if str(message.get("role") or "").strip() == "user":
            if current_turn is not None:
                turns.append(current_turn)
            current_turn = [message]
        elif current_turn is None:
            leading_messages.append(message)
        else:
            current_turn.append(message)
    if current_turn is not None:
        turns.append(current_turn)

    filtered = _filter_raw_solver_messages(leading_messages)
    for turn in turns:
        if _history_turn_has_solver_activity(turn):
            continue
        filtered.extend(_filter_raw_solver_messages(turn))
    return filtered


def _history_turn_has_solver_activity(messages: Sequence[Mapping[str, Any]]) -> bool:
    for message in messages:
        role = str(message.get("role") or "").strip()
        if role == "assistant" and isinstance(message.get("tool_calls"), list):
            for raw_call in message["tool_calls"]:
                call = raw_call if isinstance(raw_call, Mapping) else {}
                function = call.get("function")
                function = function if isinstance(function, Mapping) else {}
                if _is_solver_tool_name(function.get("name")):
                    return True
        if role == "tool" and _is_solver_tool_name(message.get("name")):
            return True
    return False


def _filter_raw_solver_messages(
    messages: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    removed_tool_call_ids: set[str] = set()
    for message in messages:
        message = copy.deepcopy(dict(message))
        role = str(message.get("role") or "").strip()
        if role == "assistant" and isinstance(message.get("tool_calls"), list):
            retained_calls = []
            removed_solver_call = False
            for raw_call in message["tool_calls"]:
                call = raw_call if isinstance(raw_call, Mapping) else {}
                function = call.get("function")
                function = function if isinstance(function, Mapping) else {}
                if _is_solver_tool_name(function.get("name")):
                    tool_call_id = str(call.get("id") or "").strip()
                    if tool_call_id:
                        removed_tool_call_ids.add(tool_call_id)
                    removed_solver_call = True
                    continue
                retained_calls.append(copy.deepcopy(raw_call))
            if removed_solver_call and not retained_calls:
                continue
            if removed_solver_call:
                message["tool_calls"] = retained_calls
        if role == "tool":
            tool_call_id = str(message.get("tool_call_id") or "").strip()
            if tool_call_id in removed_tool_call_ids or _is_solver_tool_name(
                message.get("name")
            ):
                continue
        filtered.append(message)
    return filtered


def restrict_tools_for_solver_route(
    tools: Sequence[Mapping[str, Any]],
    *,
    requirement: SolverRouteRequirement | None,
) -> list[Mapping[str, Any]]:
    items: list[Mapping[str, Any]] = []
    for item in tools:
        if isinstance(item, Mapping):
            items.append(item)
    if requirement is None:
        return items
    by_name: dict[str, Mapping[str, Any]] = {}
    for item in items:
        name = _tool_name(item)
        if name:
            by_name[name] = item
    required = by_name.get(requirement.tool_name)
    if required is None:
        raise SolverRouteGuardError(
            "solver_tool_unavailable",
            "The selected Solver Operation is currently unavailable.",
        )
    restricted = [required]
    interaction = by_name.get("request_user_interaction")
    if interaction is not None:
        restricted.append(interaction)
    return restricted


def _load_solver_root_payload(
    *,
    package_id: str,
    package_revision: str,
    workspace_id: str,
) -> Mapping[str, Any]:
    ontology, error = get_ontology_manifest(
        package_id,
        version=package_revision,
        workspace_id=workspace_id,
    )
    if error or ontology is None:
        return {}
    return build_root_payload(
        ontology=ontology,
        ontology_dir=Path(ontology.ontology_dir).resolve(),
        include_solver_catalog=False,
    )


def _operation_key(row: Mapping[str, Any]) -> str:
    for key in ("operation_key", "operation key", "key", "canonical_key"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def _operation_match_terms(
    operation_key: str,
    row: Mapping[str, Any],
) -> tuple[str, ...]:
    terms: list[str] = [operation_key]
    for key in ("title", "标题", "name", "名称", "canonical_key"):
        value = str(row.get(key) or "").strip()
        if value and value not in terms:
            terms.append(value)
    aliases = row.get("aliases") or row.get("别名") or []
    if isinstance(aliases, str):
        aliases = [part.strip() for part in aliases.replace("，", ",").split(",")]
    if isinstance(aliases, list):
        for alias in aliases:
            value = str(alias or "").strip()
            if value and value not in terms:
                terms.append(value)
    return tuple(terms)


def _is_explanation_only(query: str) -> bool:
    normalized = query.casefold()
    return any(marker in normalized for marker in _EXPLANATION_MARKERS)


def _has_execution_intent(query: str) -> bool:
    normalized = str(query or "").casefold()
    return any(marker in normalized for marker in _EXECUTION_MARKERS)


def _has_implicit_solver_intent(query: str) -> bool:
    normalized = str(query or "").casefold()
    if any(marker in normalized for marker in _EXPLICIT_SOLVER_MARKERS):
        return True
    if should_suppress_solver_tools(normalized):
        return False
    return _has_execution_intent(normalized)


def should_suppress_solver_tools(query: str) -> bool:
    normalized = str(query or "").casefold()
    if any(marker in normalized for marker in _EXPLICIT_SOLVER_MARKERS):
        return False
    return any(marker in normalized for marker in _EVALUATION_MARKERS)


def _is_solver_tool_name(value: Any) -> bool:
    return _SOLVER_TOOL_NAME_RE.fullmatch(str(value or "").casefold().strip()) is not None


def _operation_term_matches(normalized_query: str, term: str) -> bool:
    normalized_term = str(term or "").casefold().strip()
    if not normalized_term:
        return False
    left_boundary = r"(?<![a-z0-9_])" if _ascii_word_char(normalized_term[0]) else ""
    right_boundary = r"(?![a-z0-9_])" if _ascii_word_char(normalized_term[-1]) else ""
    return (
        re.search(
            "{}{}{}".format(
                left_boundary,
                re.escape(normalized_term),
                right_boundary,
            ),
            normalized_query,
        )
        is not None
    )


def _ascii_word_char(value: str) -> bool:
    return value == "_" or "a" <= value <= "z" or "0" <= value <= "9"


def _build_solver_spec(capability: Mapping[str, Any]) -> Any:
    try:
        specs = build_solver_tool_specs([capability])
    except Exception as exc:
        raise SolverRouteGuardError(
            "solver_tool_schema_unsupported",
            "The selected Solver Operation cannot expose a strict ToolCard.",
        ) from exc
    if len(specs) != 1 or not str(getattr(specs[0], "name", "") or "").strip():
        raise SolverRouteGuardError(
            "solver_tool_schema_unsupported",
            "The selected Solver Operation cannot expose a strict ToolCard.",
        )
    return specs[0]


def _solver_argument_matches(
    solver_rows: Sequence[tuple[str, Mapping[str, Any]]],
    arguments: Mapping[str, Any] | None,
) -> list[tuple[str, Mapping[str, Any], Any]]:
    if not isinstance(arguments, Mapping) or not arguments:
        return []
    matches: list[tuple[str, Mapping[str, Any], Any]] = []
    for operation_key, capability in solver_rows:
        try:
            spec = _build_solver_spec(capability)
        except SolverRouteGuardError:
            continue
        if _arguments_match_solver_spec(arguments, spec):
            matches.append((operation_key, capability, spec))
    return matches


def _arguments_match_solver_spec(
    arguments: Mapping[str, Any] | None,
    spec: Any,
) -> bool:
    if not isinstance(arguments, Mapping) or not arguments:
        return False
    schema = getattr(spec, "schema", None)
    if not isinstance(schema, Mapping):
        return False
    from agent.tools.registry import validate_tool_arguments

    return (
        validate_tool_arguments(
            str(getattr(spec, "name", "") or "solver"),
            dict(arguments),
            tool_schema=dict(schema),
        )
        is None
    )


def _tool_name(item: Mapping[str, Any]) -> str:
    direct = str(item.get("name") or "").strip()
    if direct:
        return direct
    function = item.get("function")
    if isinstance(function, Mapping):
        return str(function.get("name") or "").strip()
    return ""


def _public_solver_error(
    payload: Mapping[str, Any],
) -> tuple[str, str, bool] | None:
    raw: Any = payload.get("error") or payload.get("output_preview")
    parsed: Any = raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return None
    if not isinstance(parsed, Mapping):
        return None
    code = str(parsed.get("code") or "").strip()
    if _PUBLIC_ERROR_CODE_RE.fullmatch(code) is None:
        return None
    phase = str(parsed.get("phase") or "").strip()
    if phase and _PUBLIC_ERROR_CODE_RE.fullmatch(phase) is None:
        phase = ""
    return code, phase, parsed.get("retryable") is True
