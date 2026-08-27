"""Framework-neutral contract for executing one package-bound operation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .contracts import SolverContractError, _freeze_object


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SolverContractError("{} is required".format(field_name))
    return value.strip()


@dataclass(frozen=True)
class OperationInvocation:
    """Immutable operation identity and already-bound structured arguments."""

    ontology_id: str
    ontology_version: str
    operation_key: str
    arguments: dict[str, Any]
    base_path: Path
    workspace_id: str
    run_id: str
    timeout_seconds: int
    data_access: str
    expected_script_path: Path
    expected_script_digest: str

    def __post_init__(self) -> None:
        for field_name in (
            "ontology_id",
            "ontology_version",
            "operation_key",
            "workspace_id",
            "run_id",
            "data_access",
            "expected_script_digest",
        ):
            object.__setattr__(
                self,
                field_name,
                _required_text(getattr(self, field_name), field_name),
            )
        if self.data_access != "frozen_snapshot":
            raise SolverContractError("operation data_access must be frozen_snapshot")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, int)
            or self.timeout_seconds < 1
        ):
            raise SolverContractError("timeout_seconds must be a positive integer")
        object.__setattr__(self, "arguments", _freeze_object(self.arguments, "arguments"))
        object.__setattr__(self, "base_path", Path(self.base_path).resolve())
        object.__setattr__(
            self,
            "expected_script_path",
            Path(self.expected_script_path).resolve(),
        )


class OperationRuntime(Protocol):
    def execute(self, invocation: OperationInvocation) -> str: ...
