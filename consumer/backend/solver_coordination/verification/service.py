# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Validate untrusted solver output through an injected independent verifier."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from utils.atomic_files import staged_bytes

from ..canonical import canonical_json_bytes, sha256_digest
from ..contracts import (
    FactSnapshot,
    NormalizedInput,
    ProblemIR,
    SolverResult,
    VerificationResult,
)
from ..errors import (
    ArtifactPersistError,
    VerificationDivergenceError,
    VerificationRuntimeFailure,
)
from ..ports import IndependentVerifier

_ARTIFACT_TYPE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_ARTIFACT_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class SolverArtifactStoreError(ArtifactPersistError):
    """Raised when a solver artifact cannot be durably stored or verified."""


@dataclass(frozen=True)
class ContentAddressedSolverArtifactStore:
    """Persist canonical solver artifacts by digest and verify every read.

    The artifact URI deliberately contains no workspace or customer identity.
    Authorization remains in ``SolverJobStore``; this class owns only immutable
    content and its integrity boundary.
    """

    root: Path

    def __post_init__(self) -> None:
        root = Path(self.root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        if not root.is_dir():
            raise SolverArtifactStoreError("solver artifact root is not a directory")
        object.__setattr__(self, "root", root)

    def put(self, *, artifact_type: str, payload: Mapping[str, Any]) -> dict[str, str]:
        kind = self._artifact_type(artifact_type)
        if not isinstance(payload, Mapping):
            raise SolverArtifactStoreError("solver artifact payload must be an object")
        value = dict(payload)
        try:
            digest = sha256_digest(value)
            digest_hex = digest.rsplit(":", 1)[-1]
            envelope = {
                "artifact_type": kind,
                "digest": digest,
                "payload": value,
            }
            encoded = canonical_json_bytes(envelope)
        except (TypeError, ValueError) as exc:
            raise SolverArtifactStoreError("solver artifact is not canonical JSON") from exc

        directory = self.root / kind
        target = directory / "{}.json".format(digest_hex)
        reference = "artifact://{}/{}".format(kind, digest_hex)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            if target.exists():
                self._verify_file(target, expected=envelope)
            else:
                with staged_bytes(
                    directory=directory,
                    prefix=".{}-".format(digest_hex),
                    suffix=".tmp",
                    payload=encoded,
                ) as temporary:
                    os.replace(temporary, target)
                    self._verify_file(target, expected=envelope)
                    self._fsync_directory(directory)
        except OSError as exc:
            raise SolverArtifactStoreError("failed to persist solver artifact") from exc
        return {
            "artifact_ref": reference,
            "artifact_digest": digest,
            "artifact_type": kind,
        }

    def get(self, artifact_ref: str) -> dict[str, Any]:
        kind, digest_hex = self._parse_reference(artifact_ref)
        target = self.root / kind / "{}.json".format(digest_hex)
        try:
            envelope = self._read_file(target)
        except OSError as exc:
            raise SolverArtifactStoreError("solver artifact cannot be read") from exc
        if envelope.get("artifact_type") != kind:
            raise SolverArtifactStoreError("solver artifact type does not match reference")
        digest = envelope.get("digest")
        payload = envelope.get("payload")
        if not isinstance(payload, Mapping) or not isinstance(digest, str):
            raise SolverArtifactStoreError("solver artifact envelope is invalid")
        try:
            actual = sha256_digest(payload)
        except ValueError as exc:
            raise SolverArtifactStoreError("solver artifact payload is invalid") from exc
        if actual != digest or digest.rsplit(":", 1)[-1] != digest_hex:
            raise SolverArtifactStoreError("solver artifact integrity check failed")
        return dict(payload)

    @staticmethod
    def _artifact_type(value: object) -> str:
        if not isinstance(value, str) or not _ARTIFACT_TYPE_RE.fullmatch(value):
            raise SolverArtifactStoreError("solver artifact type is invalid")
        return value

    @classmethod
    def _parse_reference(cls, value: object) -> tuple[str, str]:
        if not isinstance(value, str) or not value.startswith("artifact://"):
            raise SolverArtifactStoreError("solver artifact reference is invalid")
        remainder = value[len("artifact://") :]
        parts = remainder.split("/")
        if len(parts) != 2:
            raise SolverArtifactStoreError("solver artifact reference is invalid")
        kind = cls._artifact_type(parts[0])
        digest_hex = parts[1]
        if not _ARTIFACT_DIGEST_RE.fullmatch(digest_hex):
            raise SolverArtifactStoreError("solver artifact reference is invalid")
        return kind, digest_hex

    @staticmethod
    def _read_file(path: Path) -> dict[str, Any]:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            raise SolverArtifactStoreError("solver artifact envelope is invalid")
        return payload

    @classmethod
    def _verify_file(cls, path: Path, *, expected: Mapping[str, Any]) -> None:
        try:
            actual = cls._read_file(path)
        except (OSError, ValueError) as exc:
            raise SolverArtifactStoreError("solver artifact persisted content is invalid") from exc
        if actual != dict(expected):
            raise SolverArtifactStoreError("solver artifact digest is bound to different content")

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


@dataclass(frozen=True)
class IndependentVerificationService:
    """Guard identity and runtime failures around a verifier implementation.

    The verifier is injected separately from the solver backend.  This service
    intentionally passes only frozen contracts and never exposes backend job
    state, credentials, or mutable provider handles.
    """

    verifier: IndependentVerifier

    def verify(
        self,
        *,
        problem: ProblemIR,
        capability: Any,
        normalized_input: NormalizedInput,
        fact_snapshot: FactSnapshot,
        solver_result: SolverResult,
    ) -> VerificationResult:
        if not isinstance(problem, ProblemIR):
            raise VerificationRuntimeFailure("compiled problem is invalid")
        if not isinstance(normalized_input, NormalizedInput):
            raise VerificationRuntimeFailure("normalized input is invalid")
        if not isinstance(fact_snapshot, FactSnapshot):
            raise VerificationRuntimeFailure("fact snapshot is invalid")
        if not isinstance(solver_result, SolverResult):
            raise VerificationRuntimeFailure("solver result is invalid")
        if solver_result.problem_id != problem.problem_id:
            raise VerificationDivergenceError("solver result does not match the compiled problem")

        try:
            verification = self.verifier.verify(
                problem=problem,
                capability=capability,
                normalized_input=normalized_input,
                fact_snapshot=fact_snapshot,
                solver_result=solver_result,
            )
        except Exception as exc:
            if isinstance(exc, (VerificationDivergenceError, VerificationRuntimeFailure)):
                raise
            raise VerificationRuntimeFailure("independent verifier execution failed") from exc

        if not isinstance(verification, VerificationResult):
            raise VerificationRuntimeFailure("independent verifier returned an invalid contract")
        if verification.problem_id != problem.problem_id:
            raise VerificationDivergenceError("verification result does not match the compiled problem")
        return verification
