# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Immutable, scenario-neutral contracts for solver coordination."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from enum import Enum
import re
from types import MappingProxyType
from typing import Any
import unicodedata

from .canonical import sha256_digest


MAX_DECIMAL_DIGITS = 28
MIN_DECIMAL_EXPONENT = -12
MAX_DECIMAL_EXPONENT = 18
MAX_PLAN_ITEMS = 64
MAX_TEXT_LENGTH = 256
_DECIMAL_WIRE_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")


class SolverContractError(ValueError):
    """Raised when a solver coordination contract is invalid."""


class Option(str, Enum):
    """The two neutral choices exposed by the first problem-family adapter."""

    OPTION_0 = "OPTION_0"
    OPTION_1 = "OPTION_1"


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise SolverContractError("{} must be a non-empty string".format(field_name))
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized:
        raise SolverContractError("{} is required".format(field_name))
    if len(normalized) > MAX_TEXT_LENGTH:
        raise SolverContractError(
            "{} exceeds the maximum length of {} characters".format(
                field_name, MAX_TEXT_LENGTH
            )
        )
    return normalized


def _positive_decimal(value: object, field_name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, Decimal)):
        raise SolverContractError("{} must be a decimal string".format(field_name))
    try:
        if isinstance(value, Decimal):
            normalized = value
        elif isinstance(value, str) and _DECIMAL_WIRE_RE.fullmatch(value):
            normalized = Decimal(value)
        else:
            raise InvalidOperation
    except (InvalidOperation, ValueError, AttributeError) as exc:
        raise SolverContractError("{} must be a finite positive decimal".format(field_name)) from exc
    if not normalized.is_finite() or normalized <= 0:
        raise SolverContractError("{} must be a finite positive decimal".format(field_name))
    sign, digits, exponent = normalized.as_tuple()
    del sign
    if (
        len(digits) > MAX_DECIMAL_DIGITS
        or exponent < MIN_DECIMAL_EXPONENT
        or exponent > MAX_DECIMAL_EXPONENT
    ):
        raise SolverContractError(
            "{} exceeds the supported decimal precision or exponent".format(field_name)
        )
    return normalized


def _decimal_string(value: Decimal) -> str:
    try:
        normalized = _positive_decimal(value, "decimal value")
        rendered = _canonical_decimal_string(normalized)
    except (SolverContractError, InvalidOperation, ValueError) as exc:
        raise SolverContractError("decimal value cannot be serialized canonically") from exc
    if rendered == "0" or len(rendered) > 128:
        raise SolverContractError("decimal value cannot be serialized canonically")
    return rendered


def _canonical_decimal_string(value: Decimal) -> str:
    """Render a finite Decimal exactly without consulting decimal.Context."""

    sign, digits, exponent = value.as_tuple()
    if not any(digits):
        return "0"
    rendered_digits = "".join(str(digit) for digit in digits)
    if exponent >= 0:
        rendered = rendered_digits + "0" * exponent
    else:
        point = len(rendered_digits) + exponent
        if point > 0:
            rendered = "{}.{}".format(
                rendered_digits[:point], rendered_digits[point:]
            )
        else:
            rendered = "0.{}{}".format("0" * (-point), rendered_digits)
        rendered = rendered.rstrip("0").rstrip(".")
    return "-{}".format(rendered) if sign else rendered


@dataclass(frozen=True)
class PlanItem:
    """One ordered input item in the neutral problem representation."""

    item_order: int
    item_id: str
    quantity: Decimal
    duration: Decimal

    def __post_init__(self) -> None:
        if isinstance(self.item_order, bool) or not isinstance(self.item_order, int):
            raise SolverContractError("item_order must be an integer")
        if self.item_order < 1:
            raise SolverContractError("item_order must be positive")
        object.__setattr__(self, "item_id", _required_text(self.item_id, "item_id"))
        object.__setattr__(self, "quantity", _positive_decimal(self.quantity, "quantity"))
        object.__setattr__(self, "duration", _positive_decimal(self.duration, "duration"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_order": self.item_order,
            "item_id": self.item_id,
            "quantity": _decimal_string(self.quantity),
            "duration": _decimal_string(self.duration),
        }


@dataclass(frozen=True)
class OrderedPlan:
    """An ordered collection and the neutral option-domain constraints."""

    items: tuple[PlanItem, ...]
    option_domain: tuple[Option, ...]
    max_transitions: int

    def __post_init__(self) -> None:
        items = tuple(self.items)
        if not items:
            raise SolverContractError("ordered plan requires at least one item")
        if len(items) > MAX_PLAN_ITEMS:
            raise SolverContractError(
                "ordered plan supports at most {} items".format(MAX_PLAN_ITEMS)
            )
        if any(not isinstance(item, PlanItem) for item in items):
            raise SolverContractError("ordered plan items must contain PlanItem values")
        actual = tuple(item.item_order for item in items)
        expected = tuple(range(1, len(items) + 1))
        if actual != expected:
            raise SolverContractError(
                "item orders must be unique, ordered, and contiguous from 1"
            )
        item_ids = tuple(item.item_id for item in items)
        if len(set(item_ids)) != len(item_ids):
            raise SolverContractError("item_id values must be unique")
        try:
            options = tuple(
                option if isinstance(option, Option) else Option(option)
                for option in self.option_domain
            )
        except (TypeError, ValueError) as exc:
            raise SolverContractError("option_domain contains an unsupported option") from exc
        if options != (Option.OPTION_0, Option.OPTION_1):
            raise SolverContractError(
                "option_domain must contain OPTION_0 and OPTION_1 in that order"
            )
        if isinstance(self.max_transitions, bool) or not isinstance(self.max_transitions, int):
            raise SolverContractError("max_transitions must be an integer")
        if self.max_transitions < 0:
            raise SolverContractError("max_transitions must not be negative")
        object.__setattr__(self, "items", items)
        object.__setattr__(self, "option_domain", options)

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [item.to_dict() for item in self.items],
            "option_domain": [option.value for option in self.option_domain],
            "max_transitions": self.max_transitions,
        }


@dataclass(frozen=True)
class Candidate:
    """One immutable assignment over every item in an ordered plan."""

    candidate_id: str
    assignments: tuple[Option, ...]
    transition_count: int
    candidate_digest: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate_id", _required_text(self.candidate_id, "candidate_id"))
        try:
            assignments = tuple(
                option if isinstance(option, Option) else Option(option)
                for option in self.assignments
            )
        except (TypeError, ValueError) as exc:
            raise SolverContractError("assignments contain an unsupported option") from exc
        if not assignments:
            raise SolverContractError("assignments must not be empty")
        if isinstance(self.transition_count, bool) or not isinstance(self.transition_count, int):
            raise SolverContractError("transition_count must be an integer")
        if self.transition_count < 0:
            raise SolverContractError("transition_count must not be negative")
        actual = sum(left != right for left, right in zip(assignments, assignments[1:]))
        if self.transition_count != actual:
            raise SolverContractError("transition_count does not match assignments")
        digest = self.candidate_digest
        if digest and (not isinstance(digest, str) or not digest.strip()):
            raise SolverContractError("candidate_digest must be a non-empty string")
        object.__setattr__(self, "assignments", assignments)
        object.__setattr__(self, "candidate_digest", digest.strip() if isinstance(digest, str) else "")

    def assignment_payload(self) -> dict[str, Any]:
        return {
            "assignments": [option.value for option in self.assignments],
            "transition_count": self.transition_count,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            **self.assignment_payload(),
            "candidate_digest": self.candidate_digest,
        }


@dataclass(frozen=True)
class CandidateSet:
    source_run_id: str
    problem_family: str
    knowledge_package_id: str
    knowledge_package_revision: str
    plan: OrderedPlan
    candidates: tuple[Candidate, ...]
    problem_digest: str
    candidate_set_digest: str
    authoritative: bool = field(default=False, init=False)

    @classmethod
    def create(
        cls,
        *,
        source_run_id: str,
        problem_family: str,
        knowledge_package_id: str,
        knowledge_package_revision: str,
        plan: OrderedPlan,
        candidates: Iterable[Candidate],
    ) -> "CandidateSet":
        normalized_run_id = _required_text(source_run_id, "source_run_id")
        normalized_problem_family = _required_text(problem_family, "problem_family")
        normalized_package_id = _required_text(knowledge_package_id, "knowledge_package_id")
        normalized_revision = _required_text(
            knowledge_package_revision, "knowledge_package_revision"
        )
        if not isinstance(plan, OrderedPlan):
            raise SolverContractError("plan must be an OrderedPlan")
        normalized_candidates = tuple(candidates)
        if not normalized_candidates:
            raise SolverContractError("candidate set must not be empty")
        if any(not isinstance(candidate, Candidate) for candidate in normalized_candidates):
            raise SolverContractError("candidates must contain Candidate values")
        candidate_ids = tuple(candidate.candidate_id for candidate in normalized_candidates)
        if len(set(candidate_ids)) != len(candidate_ids):
            raise SolverContractError("candidate_id values must be unique")
        if any(len(candidate.assignments) != len(plan.items) for candidate in normalized_candidates):
            raise SolverContractError("every candidate must cover every plan item")
        if any(
            candidate.transition_count > plan.max_transitions
            for candidate in normalized_candidates
        ):
            raise SolverContractError("candidate exceeds plan max_transitions")

        problem_payload = {
            "problem_family": normalized_problem_family,
            "knowledge_package_id": normalized_package_id,
            "knowledge_package_revision": normalized_revision,
            "plan": plan.to_dict(),
        }
        problem_digest = sha256_digest(problem_payload)
        digested_candidates = tuple(
            replace(
                candidate,
                candidate_digest=sha256_digest(
                    {
                        "problem_digest": problem_digest,
                        "assignment": candidate.assignment_payload(),
                    }
                ),
            )
            for candidate in normalized_candidates
        )
        container_payload = {
            # A candidate set is a durable run container.  Its per-candidate
            # identities stay stable, while the container address retains the
            # solve-run lineage needed by validation authorization.
            "source_run_id": normalized_run_id,
            "problem_digest": problem_digest,
            "candidates": [candidate.to_dict() for candidate in digested_candidates],
        }
        return cls(
            source_run_id=normalized_run_id,
            problem_family=normalized_problem_family,
            knowledge_package_id=normalized_package_id,
            knowledge_package_revision=normalized_revision,
            plan=plan,
            candidates=digested_candidates,
            problem_digest=problem_digest,
            candidate_set_digest=sha256_digest(container_payload),
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CandidateSet":
        """Rehydrate and integrity-check a persisted candidate-set artifact."""
        if not isinstance(payload, Mapping):
            raise SolverContractError("candidate set artifact must be an object")
        _reject_unknown_fields(
            payload,
            {
                "source_run_id",
                "problem_family",
                "knowledge_package_id",
                "knowledge_package_revision",
                "plan",
                "problem_digest",
                "candidate_count",
                "candidates",
                "candidate_set_digest",
                "authoritative",
                # Storage envelope metadata is validated by CandidateSetStore;
                # it is deliberately kept outside the digest-bearing domain
                # payload but must still be schema allowlisted.
                "schema_version",
                "artifact_type",
                "candidate_set_ref",
                "workspace_id",
                "owner_id",
                "created_at",
            },
            scope="candidate set artifact",
        )
        plan_payload = payload.get("plan")
        if not isinstance(plan_payload, Mapping):
            raise SolverContractError("candidate set artifact plan must be an object")
        _reject_unknown_fields(
            plan_payload,
            {"items", "option_domain", "max_transitions"},
            scope="candidate set artifact plan",
        )
        raw_items = plan_payload.get("items")
        if not isinstance(raw_items, list):
            raise SolverContractError("candidate set artifact items must be an array")
        if any(not isinstance(item, Mapping) for item in raw_items):
            raise SolverContractError("candidate set artifact item is invalid")
        for raw_item in raw_items:
            _reject_unknown_fields(
                raw_item,
                {"item_order", "item_id", "quantity", "duration"},
                scope="candidate set artifact item",
            )
        try:
            plan = OrderedPlan(
                items=tuple(
                    PlanItem(
                        item_order=item.get("item_order"),
                        item_id=item.get("item_id"),
                        quantity=item.get("quantity"),
                        duration=item.get("duration"),
                    )
                    for item in raw_items
                ),
                option_domain=tuple(plan_payload.get("option_domain") or ()),
                max_transitions=plan_payload.get("max_transitions"),
            )
        except (AttributeError, TypeError) as exc:
            raise SolverContractError("candidate set artifact item is invalid") from exc
        raw_candidates = payload.get("candidates")
        if not isinstance(raw_candidates, list):
            raise SolverContractError("candidate set artifact candidates must be an array")
        candidates = []
        for raw_candidate in raw_candidates:
            if not isinstance(raw_candidate, Mapping):
                raise SolverContractError("candidate set artifact candidate is invalid")
            _reject_unknown_fields(
                raw_candidate,
                {"candidate_id", "assignments", "transition_count", "candidate_digest"},
                scope="candidate set artifact candidate",
            )
            try:
                candidates.append(
                    Candidate(
                        candidate_id=raw_candidate.get("candidate_id"),
                        assignments=tuple(raw_candidate.get("assignments") or ()),
                        transition_count=raw_candidate.get("transition_count"),
                        candidate_digest=raw_candidate.get("candidate_digest", ""),
                    )
                )
            except (KeyError, TypeError) as exc:
                raise SolverContractError("candidate set artifact candidate is invalid") from exc
        candidate_set = cls.create(
            source_run_id=payload.get("source_run_id"),
            problem_family=payload.get("problem_family"),
            knowledge_package_id=payload.get("knowledge_package_id"),
            knowledge_package_revision=payload.get("knowledge_package_revision"),
            plan=plan,
            candidates=tuple(candidates),
        )
        if payload.get("problem_digest") != candidate_set.problem_digest:
            raise SolverContractError("candidate set problem_digest does not match content")
        for supplied, computed in zip(candidates, candidate_set.candidates):
            if supplied.candidate_digest != computed.candidate_digest:
                raise SolverContractError("candidate digest does not match content")
        if payload.get("candidate_set_digest") != candidate_set.candidate_set_digest:
            raise SolverContractError("candidate set digest does not match content")
        if payload.get("candidate_count") != candidate_set.candidate_count:
            raise SolverContractError("candidate set candidate_count does not match content")
        if payload.get("authoritative") is not False:
            raise SolverContractError("candidate set must remain non-authoritative")
        return candidate_set

    @property
    def candidate_count(self) -> int:
        return len(self.candidates)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_run_id": self.source_run_id,
            "problem_family": self.problem_family,
            "knowledge_package_id": self.knowledge_package_id,
            "knowledge_package_revision": self.knowledge_package_revision,
            "plan": self.plan.to_dict(),
            "problem_digest": self.problem_digest,
            "candidate_count": self.candidate_count,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "candidate_set_digest": self.candidate_set_digest,
            "authoritative": self.authoritative,
        }


def _reject_unknown_fields(
    payload: Mapping[str, Any], allowed: set[str], *, scope: str
) -> None:
    unknown = set(payload) - allowed
    if unknown:
        names = ", ".join(sorted(str(name) for name in unknown))
        raise SolverContractError(
            "{} contains unsupported fields: {}".format(scope, names)
        )


NORMALIZED_INPUT_SCHEMA_VERSION = "solver.input.v1"
FACT_SNAPSHOT_SCHEMA_VERSION = "solver.fact-snapshot.v1"
PROBLEM_IR_SCHEMA_VERSION = "solver.problem.v1"
SOLVER_RESULT_SCHEMA_VERSION = "solver.result.v1"
VERIFICATION_RESULT_SCHEMA_VERSION = "solver.verification-result.v1"
AUTHORITATIVE_RESULT_SCHEMA_VERSION = "solver.authoritative-result.v1"

# A proof kind is evidence of specific facts, not a blanket authority marker.
# The projector and result DTO both use this table so a weak feasibility check
# cannot be upgraded into an optimality or infeasibility claim downstream.
AUTHORITATIVE_PROOF_CAPABILITIES = MappingProxyType(
    {
        "constraint_recalculation": frozenset({"feasibility"}),
        "independent_domain_constraint_check": frozenset({"feasibility"}),
        "verifier_exhaustive_check": frozenset(
            {"feasibility", "optimality", "infeasibility"}
        ),
        "exhaustive": frozenset({"feasibility", "optimality", "infeasibility"}),
        "verifier_infeasibility_certificate": frozenset({"infeasibility"}),
    }
)

_RUNTIME_IDENTITY_FIELDS = frozenset(
    {
        "workspace_id",
        "owner_id",
        "run_id",
        "source_run_id",
        "job_id",
        "attempt_id",
        "session_id",
        "turn_id",
        "tool_call_id",
        "knowledge_revision_id",
        "package_id",
        "knowledge_package_id",
        "knowledge_package_revision",
        "package_revision",
        "provider_binding_id",
        "provider_binding_revision",
        "provider_binding_revision_id",
        "solver_binding_id",
        "solver_binding_revision",
        "solver_binding_revision_id",
        "fact_snapshot_ref",
        "fact_snapshot_digest",
        "problem_id",
        "result_artifact_ref",
        "verification_evidence_ref",
        "script_path",
        "selected_ontology_id",
        "selected_ontology_version",
        "selected_ontology_revision",
        "ontology_id",
        "ontology_version",
        "ontology_revision",
        "selected_version",
        "tenant_id",
        "tenant",
        "user_id",
        "account_id",
        "principal_id",
        "organization_id",
    }
)
_CREDENTIAL_FIELDS = frozenset(
    {
        "credential",
        "credentials",
        "password",
        "secret",
        "secret_key",
        "token",
        "api_key",
        "access_key",
        "access_token",
        "client_secret",
        "authorization",
        "private_key",
        "solver_endpoint",
        "endpoint",
        "endpoint_url",
        "base_url",
        "connection_string",
        "database_url",
        "database_uri",
        "database_dsn",
        "db_url",
        "dsn",
        "auth_token",
        "bearer_token",
        "refresh_token",
        "auth_header",
        "cookie",
        "session_token",
    }
)
_BINDING_REVISION_FIELDS = frozenset(
    {
        "provider_binding_revision",
        "provider_binding_revision_id",
        "solver_binding_revision",
        "solver_binding_revision_id",
    }
)
_CREDENTIAL_TERMINAL_TOKENS = frozenset(
    {
        "authorization",
        "cookie",
        "credential",
        "credentials",
        "password",
        "passwd",
        "pwd",
        "secret",
        "token",
    }
)
_CREDENTIAL_DERIVATION_TOKENS = frozenset(
    {
        "cipher",
        "ciphertext",
        "digest",
        "encoded",
        "encrypted",
        "hash",
        "key",
        "value",
    }
)
_CREDENTIAL_QUALIFIER_TOKENS = frozenset(
    {
        "access",
        "api",
        "auth",
        "bearer",
        "client",
        "oauth",
        "private",
        "refresh",
    }
)
_CREDENTIAL_ROOT_TOKENS = frozenset(
    {"credential", "credentials", "password", "passwd", "pwd"}
)
_IDENTITY_REFERENCE_PREFIX_TOKENS = frozenset(
    {
        "account",
        "artifact",
        "attempt",
        "job",
        "knowledge",
        "ontology",
        "org",
        "organization",
        "owner",
        "package",
        "principal",
        "problem",
        "provider",
        "result",
        "run",
        "session",
        "solver",
        "tenant",
        "tool",
        "turn",
        "user",
        "workspace",
    }
)
_IDENTITY_REFERENCE_TERMINAL_TOKENS = frozenset(
    {"digest", "id", "identifier", "ref", "reference", "revision"}
)


def _freeze_json(value: Any, field_name: str) -> Any:
    """Copy JSON-shaped data into recursively immutable containers."""

    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        raise SolverContractError(
            "{} must serialize non-integer numbers as decimal strings".format(field_name)
        )
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise SolverContractError("{} mapping keys must be strings".format(field_name))
            normalized_key = unicodedata.normalize("NFC", key)
            if normalized_key in frozen:
                raise SolverContractError(
                    "{} contains duplicate keys after normalization".format(field_name)
                )
            frozen[normalized_key] = _freeze_json(
                item, "{}.{}".format(field_name, normalized_key)
            )
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(
            _freeze_json(item, "{}[{}]".format(field_name, index))
            for index, item in enumerate(value)
        )
    raise SolverContractError(
        "{} must contain only JSON-shaped values".format(field_name)
    )


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _freeze_object(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SolverContractError("{} must be an object".format(field_name))
    return _freeze_json(value, field_name)


def _freeze_array(value: Any, field_name: str) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes, bytearray, Mapping, set, frozenset)):
        raise SolverContractError("{} must be an array".format(field_name))
    try:
        items = tuple(value)
    except TypeError as exc:
        raise SolverContractError("{} must be an array".format(field_name)) from exc
    return _freeze_json(items, field_name)


def _strict_schema_version(actual: object, expected: str, field_name: str) -> str:
    if actual != expected:
        raise SolverContractError(
            "{} schema_version must be {}".format(field_name, expected)
        )
    return expected


def _assert_authoritative_proof_strength(
    *, feasibility: str, optimality: str, proof_kind: str
) -> None:
    if feasibility == "feasible":
        required = {"feasibility"}
        if optimality == "proven_optimal":
            required.add("optimality")
    elif feasibility == "infeasible":
        required = {"infeasibility"}
    else:
        raise SolverContractError("authoritative result feasibility is unsupported")
    actual = AUTHORITATIVE_PROOF_CAPABILITIES.get(proof_kind, frozenset())
    if not required.issubset(actual):
        raise SolverContractError(
            "authoritative result proof is insufficient for the claimed authority"
        )


def _normalized_field_token(value: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(value).strip()).casefold()
    return re.sub(r"[^a-z0-9]", "", normalized)


def _field_name_tokens(value: object) -> tuple[str, ...]:
    """Split snake, kebab, dotted, and camel-case names into semantic tokens."""

    normalized = unicodedata.normalize("NFKC", str(value).strip())
    separated = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", normalized)
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", separated)
    return tuple(
        token.casefold()
        for token in re.findall(r"[A-Za-z0-9]+", separated)
        if token
    )


def _is_semantic_credential_field(tokens: tuple[str, ...]) -> bool:
    if not tokens:
        return False
    if any(token in _CREDENTIAL_ROOT_TOKENS for token in tokens):
        return True
    if tokens[-1] in _CREDENTIAL_TERMINAL_TOKENS:
        return True
    if tokens[-1] in _CREDENTIAL_DERIVATION_TOKENS and any(
        token in _CREDENTIAL_QUALIFIER_TOKENS
        or token in _CREDENTIAL_TERMINAL_TOKENS
        for token in tokens[:-1]
    ):
        return True
    return False


def _is_semantic_identity_field(tokens: tuple[str, ...]) -> bool:
    return bool(
        len(tokens) >= 2
        and tokens[-1] in _IDENTITY_REFERENCE_TERMINAL_TOKENS
        and any(token in _IDENTITY_REFERENCE_PREFIX_TOKENS for token in tokens[:-1])
    )


def _is_forbidden_field_name(value: object, forbidden: frozenset[str]) -> bool:
    normalized = _normalized_field_token(value)
    if normalized in {_normalized_field_token(field) for field in forbidden}:
        return True
    tokens = _field_name_tokens(value)
    if forbidden & _CREDENTIAL_FIELDS and _is_semantic_credential_field(tokens):
        return True
    return bool(
        forbidden & _RUNTIME_IDENTITY_FIELDS
        and _is_semantic_identity_field(tokens)
    )


def _find_forbidden_key(value: Any, forbidden: frozenset[str]) -> str | None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _is_forbidden_field_name(key, forbidden):
                return str(key)
            nested = _find_forbidden_key(item, forbidden)
            if nested is not None:
                return nested
    elif isinstance(value, (list, tuple)):
        for item in value:
            nested = _find_forbidden_key(item, forbidden)
            if nested is not None:
                return nested
    return None


def _assert_model_payload_is_platform_neutral(value: Any, field_name: str) -> None:
    forbidden = _find_forbidden_key(
        value, _RUNTIME_IDENTITY_FIELDS | _CREDENTIAL_FIELDS
    )
    if forbidden is not None:
        raise SolverContractError(
            "{} contains runtime identity or credential field: {}".format(
                field_name, forbidden
            )
        )


def _assert_field_name_is_platform_neutral(value: object, field_name: str) -> None:
    if _is_forbidden_field_name(
        value, _RUNTIME_IDENTITY_FIELDS | _CREDENTIAL_FIELDS
    ):
        raise SolverContractError(
            "{} contains runtime identity or credential field: {}".format(
                field_name, value
            )
        )


def _digest_contract(payload: Mapping[str, Any], field_name: str) -> str:
    try:
        return sha256_digest(payload)
    except ValueError as exc:
        raise SolverContractError("{} cannot be canonicalized".format(field_name)) from exc


@dataclass(frozen=True)
class NormalizedInput:
    """Canonical model-facing input without platform runtime identity."""

    operation_key: str
    values: Mapping[str, Any]
    source_evidence: tuple[Any, ...] = ()
    normalization: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = NORMALIZED_INPUT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        operation_key = _required_text(self.operation_key, "operation_key")
        _strict_schema_version(
            self.schema_version, NORMALIZED_INPUT_SCHEMA_VERSION, "normalized input"
        )
        values = _freeze_object(self.values, "values")
        _assert_model_payload_is_platform_neutral(values, "values")
        object.__setattr__(self, "operation_key", operation_key)
        object.__setattr__(self, "values", values)
        object.__setattr__(
            self,
            "source_evidence",
            _freeze_array(self.source_evidence, "source_evidence"),
        )
        _assert_model_payload_is_platform_neutral(
            self.source_evidence, "source_evidence"
        )
        object.__setattr__(
            self,
            "normalization",
            _freeze_object(self.normalization, "normalization"),
        )
        _assert_model_payload_is_platform_neutral(
            self.normalization, "normalization"
        )

    @property
    def input_digest(self) -> str:
        return _digest_contract(self.to_dict(), "normalized input")

    @property
    def digest(self) -> str:
        return self.input_digest

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "operation_key": self.operation_key,
            "values": _thaw_json(self.values),
            "source_evidence": _thaw_json(self.source_evidence),
            "normalization": _thaw_json(self.normalization),
        }


@dataclass(frozen=True)
class FactSnapshot:
    """Immutable, scoped facts used by solve, retry, replay, and verification."""

    workspace_id: str
    owner_id: str
    package_id: str
    package_revision: str
    normalized_input_digest: str
    facts: tuple[Mapping[str, Any], ...]
    bindings: Mapping[str, Any]
    as_of: str
    source_evidence: tuple[Any, ...] = ()
    checks: Mapping[str, Any] = field(default_factory=dict)
    run_id: str = ""
    schema_version: str = FACT_SNAPSHOT_SCHEMA_VERSION
    snapshot_digest: str = ""

    def __post_init__(self) -> None:
        _strict_schema_version(
            self.schema_version, FACT_SNAPSHOT_SCHEMA_VERSION, "fact snapshot"
        )
        object.__setattr__(self, "workspace_id", _required_text(self.workspace_id, "workspace_id"))
        object.__setattr__(self, "owner_id", _required_text(self.owner_id, "owner_id"))
        object.__setattr__(self, "package_id", _required_text(self.package_id, "package_id"))
        object.__setattr__(
            self, "package_revision", _required_text(self.package_revision, "package_revision")
        )
        object.__setattr__(
            self,
            "normalized_input_digest",
            _required_text(self.normalized_input_digest, "normalized_input_digest"),
        )
        if self.run_id:
            object.__setattr__(self, "run_id", _required_text(self.run_id, "run_id"))
        object.__setattr__(self, "as_of", _required_text(self.as_of, "as_of"))
        raw_facts = _freeze_array(self.facts, "facts")
        if any(not isinstance(item, Mapping) for item in raw_facts):
            raise SolverContractError("facts must contain objects")
        normalized_facts: list[Mapping[str, Any]] = []
        for item in raw_facts:
            if "name" not in item or not str(item.get("name") or "").strip():
                raise SolverContractError("fact name is required")
            if "value" not in item:
                raise SolverContractError("fact value is required")
            allowed_fact_fields = {
                "name",
                "value",
                "unit",
                "precision",
                "entity_id",
                "business_key",
                "source_evidence",
            }
            if set(item) - allowed_fact_fields:
                raise SolverContractError("fact contains unsupported fields")
            name = _required_text(item.get("name"), "fact name")
            _assert_field_name_is_platform_neutral(name, "fact name")
            source_evidence = _freeze_array(
                item.get("source_evidence", ()),
                "fact {} source_evidence".format(name),
            )
            normalized = _freeze_object(
                {
                    "name": name,
                    "value": item.get("value"),
                    "unit": item.get("unit"),
                    "precision": item.get("precision"),
                    "entity_id": item.get("entity_id"),
                    "business_key": item.get("business_key"),
                    "source_evidence": source_evidence,
                },
                "fact {}".format(name),
            )
            _assert_model_payload_is_platform_neutral(
                normalized, "fact {}".format(name)
            )
            normalized_facts.append(normalized)
        facts = tuple(sorted(normalized_facts, key=lambda item: str(item["name"])))
        names = tuple(str(item.get("name") or "") for item in facts)
        if any(not name for name in names) or len(set(names)) != len(names):
            raise SolverContractError("facts must have unique canonical names")
        bindings = _freeze_object(self.bindings, "bindings")
        invalid_bindings = sorted(set(bindings) - _BINDING_REVISION_FIELDS)
        if invalid_bindings:
            raise SolverContractError(
                "bindings may contain only binding revision references: {}".format(
                    ", ".join(invalid_bindings)
                )
            )
        if any(not isinstance(value, str) or not value.strip() for value in bindings.values()):
            raise SolverContractError("binding revision references must be non-empty strings")
        object.__setattr__(self, "facts", facts)
        object.__setattr__(self, "bindings", bindings)
        source_evidence = _freeze_array(self.source_evidence, "source_evidence")
        _assert_model_payload_is_platform_neutral(
            source_evidence, "source_evidence"
        )
        object.__setattr__(self, "source_evidence", source_evidence)
        checks = _freeze_object(self.checks, "checks")
        required_check_fields = {"required", "missing", "duplicates", "conflicts"}
        if set(checks) != required_check_fields:
            raise SolverContractError(
                "fact snapshot checks must contain the complete required, missing, "
                "duplicates, and conflicts fields"
            )
        normalized_checks: dict[str, tuple[str, ...]] = {}
        for check_name in ("required", "missing", "duplicates", "conflicts"):
            check_value = checks.get(check_name)
            if not isinstance(check_value, tuple):
                raise SolverContractError(
                    "fact snapshot checks {} must be an array".format(check_name)
                )
            normalized_values = tuple(
                sorted(
                    _required_text(value, "fact snapshot checks {} item".format(check_name))
                    for value in check_value
                )
            )
            if len(set(normalized_values)) != len(normalized_values):
                raise SolverContractError(
                    "fact snapshot checks {} contains duplicates".format(check_name)
                )
            normalized_checks[check_name] = normalized_values
            if check_name != "required" and normalized_values:
                raise SolverContractError(
                    "fact snapshot checks contain {} findings".format(check_name)
                )
        required_names = set(normalized_checks["required"])
        if required_names - set(names):
            raise SolverContractError("fact snapshot required facts are missing")
        checks = _freeze_object(normalized_checks, "checks")
        object.__setattr__(self, "checks", checks)
        computed = _digest_contract(self._content_digest_payload(), "fact snapshot")
        if self.snapshot_digest and self.snapshot_digest != computed:
            raise SolverContractError("fact snapshot digest does not match content")
        object.__setattr__(self, "snapshot_digest", computed)

    @property
    def digest(self) -> str:
        return self.snapshot_digest

    @property
    def fact_snapshot_digest(self) -> str:
        return self.snapshot_digest

    @property
    def knowledge_package_revision(self) -> str:
        return self.package_revision

    @property
    def knowledge_package_id(self) -> str:
        return self.package_id

    def _content_digest_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "package_id": self.package_id,
            "package_revision": self.package_revision,
            "normalized_input_digest": self.normalized_input_digest,
            "facts": _thaw_json(self.facts),
            "bindings": _thaw_json(self.bindings),
            "as_of": self.as_of,
            "source_evidence": _thaw_json(self.source_evidence),
            "checks": _thaw_json(self.checks),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "workspace_id": self.workspace_id,
            "owner_id": self.owner_id,
            "run_id": self.run_id,
            "package_id": self.package_id,
            "package_revision": self.package_revision,
            "normalized_input_digest": self.normalized_input_digest,
            "facts": _thaw_json(self.facts),
            "bindings": _thaw_json(self.bindings),
            "as_of": self.as_of,
            "source_evidence": _thaw_json(self.source_evidence),
            "checks": _thaw_json(self.checks),
            "snapshot_digest": self.snapshot_digest,
        }


@dataclass(frozen=True)
class ProblemIR:
    """Engine-neutral, materialized solver problem."""

    problem_id: str
    problem_family: str
    decision_space: Mapping[str, Any]
    parameters: Mapping[str, Any]
    constraints: tuple[Any, ...]
    objectives: tuple[Any, ...]
    limits: Mapping[str, Any]
    schema_version: str = PROBLEM_IR_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _strict_schema_version(self.schema_version, PROBLEM_IR_SCHEMA_VERSION, "problem IR")
        object.__setattr__(self, "problem_id", _required_text(self.problem_id, "problem_id"))
        object.__setattr__(
            self, "problem_family", _required_text(self.problem_family, "problem_family")
        )
        for field_name in ("decision_space", "parameters", "limits"):
            value = _freeze_object(getattr(self, field_name), field_name)
            _assert_model_payload_is_platform_neutral(value, field_name)
            object.__setattr__(self, field_name, value)
        for field_name in ("constraints", "objectives"):
            value = _freeze_array(getattr(self, field_name), field_name)
            _assert_model_payload_is_platform_neutral(value, field_name)
            object.__setattr__(self, field_name, value)

    @property
    def problem_digest(self) -> str:
        return self.problem_id

    @property
    def digest(self) -> str:
        return self.problem_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "problem_id": self.problem_id,
            "problem_family": self.problem_family,
            "decision_space": _thaw_json(self.decision_space),
            "parameters": _thaw_json(self.parameters),
            "constraints": _thaw_json(self.constraints),
            "objectives": _thaw_json(self.objectives),
            "limits": _thaw_json(self.limits),
        }


@dataclass(frozen=True)
class SolverResult:
    problem_id: str
    status: str
    assignment: Mapping[str, Any]
    objective_values: Mapping[str, Any]
    diagnostics: tuple[Any, ...]
    engine: Mapping[str, Any]
    schema_version: str = SOLVER_RESULT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _strict_schema_version(
            self.schema_version, SOLVER_RESULT_SCHEMA_VERSION, "solver result"
        )
        object.__setattr__(self, "problem_id", _required_text(self.problem_id, "problem_id"))
        object.__setattr__(self, "status", _required_text(self.status, "status"))
        for field_name in ("assignment", "objective_values", "engine"):
            value = _freeze_object(getattr(self, field_name), field_name)
            _assert_model_payload_is_platform_neutral(value, field_name)
            object.__setattr__(self, field_name, value)
        diagnostics = _freeze_array(self.diagnostics, "diagnostics")
        _assert_model_payload_is_platform_neutral(diagnostics, "diagnostics")
        object.__setattr__(self, "diagnostics", diagnostics)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "problem_id": self.problem_id,
            "status": self.status,
            "assignment": _thaw_json(self.assignment),
            "objective_values": _thaw_json(self.objective_values),
            "diagnostics": _thaw_json(self.diagnostics),
            "engine": _thaw_json(self.engine),
        }


@dataclass(frozen=True)
class VerificationResult:
    problem_id: str
    status: str
    feasibility: str
    optimality: str
    proof_kind: str
    diagnostics: tuple[Any, ...] = ()
    evidence: tuple[Any, ...] = ()
    schema_version: str = VERIFICATION_RESULT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _strict_schema_version(
            self.schema_version,
            VERIFICATION_RESULT_SCHEMA_VERSION,
            "verification result",
        )
        for field_name in (
            "problem_id",
            "status",
            "feasibility",
            "optimality",
            "proof_kind",
        ):
            object.__setattr__(
                self, field_name, _required_text(getattr(self, field_name), field_name)
            )
        for field_name in ("diagnostics", "evidence"):
            value = _freeze_array(getattr(self, field_name), field_name)
            _assert_model_payload_is_platform_neutral(value, field_name)
            object.__setattr__(self, field_name, value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "problem_id": self.problem_id,
            "status": self.status,
            "feasibility": self.feasibility,
            "optimality": self.optimality,
            "proof_kind": self.proof_kind,
            "diagnostics": _thaw_json(self.diagnostics),
            "evidence": _thaw_json(self.evidence),
        }


@dataclass(frozen=True)
class AuthoritativeResult:
    run_id: str
    job_id: str
    status: str
    feasibility: str
    optimality: str
    proof_kind: str
    authoritative: bool
    summary: Mapping[str, Any]
    result_artifact_ref: str
    verification_evidence_ref: str
    knowledge_revision_id: str
    fact_snapshot_digest: str
    problem_digest: str
    candidate_results_artifact_ref: str | None = None
    candidate_results_artifact_digest: str | None = None
    schema_version: str = AUTHORITATIVE_RESULT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _strict_schema_version(
            self.schema_version,
            AUTHORITATIVE_RESULT_SCHEMA_VERSION,
            "authoritative result",
        )
        for field_name in (
            "run_id",
            "job_id",
            "status",
            "feasibility",
            "optimality",
            "proof_kind",
            "result_artifact_ref",
            "verification_evidence_ref",
            "knowledge_revision_id",
            "fact_snapshot_digest",
            "problem_digest",
        ):
            object.__setattr__(
                self, field_name, _required_text(getattr(self, field_name), field_name)
            )
        if self.status != "completed":
            raise SolverContractError("authoritative result status must be completed")
        if self.feasibility not in {"feasible", "infeasible"}:
            raise SolverContractError(
                "authoritative result feasibility is unsupported"
            )
        if self.feasibility == "feasible" and self.optimality not in {
            "not_proven",
            "proven_optimal",
        }:
            raise SolverContractError(
                "authoritative result optimality is inconsistent with feasibility"
            )
        if self.feasibility == "infeasible" and self.optimality != "not_applicable":
            raise SolverContractError(
                "authoritative result optimality is inconsistent with infeasibility"
            )
        _assert_authoritative_proof_strength(
            feasibility=self.feasibility,
            optimality=self.optimality,
            proof_kind=self.proof_kind,
        )
        if self.authoritative is not True:
            raise SolverContractError("authoritative result must be authoritative")
        candidate_ref = self.candidate_results_artifact_ref
        candidate_digest = self.candidate_results_artifact_digest
        if (candidate_ref is None) != (candidate_digest is None):
            raise SolverContractError(
                "candidate results artifact reference and digest must be provided together"
            )
        if candidate_ref is not None:
            object.__setattr__(
                self,
                "candidate_results_artifact_ref",
                _required_text(candidate_ref, "candidate_results_artifact_ref"),
            )
            object.__setattr__(
                self,
                "candidate_results_artifact_digest",
                _required_text(candidate_digest, "candidate_results_artifact_digest"),
            )
        summary = _freeze_object(self.summary, "summary")
        _assert_model_payload_is_platform_neutral(summary, "summary")
        object.__setattr__(self, "summary", summary)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "job_id": self.job_id,
            "status": self.status,
            "feasibility": self.feasibility,
            "optimality": self.optimality,
            "proof_kind": self.proof_kind,
            "authoritative": self.authoritative,
            "summary": _thaw_json(self.summary),
            "result_artifact_ref": self.result_artifact_ref,
            "verification_evidence_ref": self.verification_evidence_ref,
            "knowledge_revision_id": self.knowledge_revision_id,
            "fact_snapshot_digest": self.fact_snapshot_digest,
            "problem_digest": self.problem_digest,
        }
        if self.candidate_results_artifact_ref is not None:
            payload["candidate_results_artifact_ref"] = (
                self.candidate_results_artifact_ref
            )
            payload["candidate_results_artifact_digest"] = (
                self.candidate_results_artifact_digest
            )
        return payload
