# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Canonical hashing and authoritative result envelope construction."""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping

from agent.direct_execution.build_revision import (
    APPLICATION_BUILD_REVISION_FILENAME,
    calculate_application_build_revision,
)
from agent.direct_execution.contracts import LockedDependency
from integration.sandbox.runtime_contract import (
    sandbox_runner_protocol_version,
    sandbox_runtime_revision,
)
from ontology.operation_execution import canonical_json_bytes, sha256_bytes


RUNNER_PROTOCOL_VERSION = sandbox_runner_protocol_version()
_BUILD_REVISION_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


def canonical_sha256(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


@lru_cache(maxsize=1)
def _application_build_revision() -> str:
    backend_root = Path(__file__).resolve().parents[2]
    revision_path = backend_root / APPLICATION_BUILD_REVISION_FILENAME
    try:
        baked_revision = revision_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        baked_revision = ""
    except OSError as exc:
        raise RuntimeError("application build revision could not be read") from exc
    else:
        if not _BUILD_REVISION_PATTERN.fullmatch(baked_revision):
            raise RuntimeError("application build revision is invalid")
        return baked_revision

    configured_build_sha = str(os.getenv("APP_BUILD_SHA") or os.getenv("GIT_COMMIT") or "").strip()
    if configured_build_sha:
        build_sha = configured_build_sha
    else:
        build_sha = calculate_application_build_revision(backend_root)
    return build_sha


def runtime_revision() -> str:
    """Bind the application build to the currently attested sandbox image."""
    return "deterministic-runner:{}:{}:{}".format(
        RUNNER_PROTOCOL_VERSION,
        _application_build_revision(),
        sandbox_runtime_revision(),
    )


def build_result_envelope(
    *,
    structured_result: Mapping[str, Any],
    artifacts: Iterable[Mapping[str, Any]],
    ontology_id: str,
    ontology_version: str,
    operation_key: str,
    package_revision: str,
    handler_ref: str,
    handler_sha256: str,
    dependencies: Iterable[LockedDependency],
    arguments: Mapping[str, Any],
    elapsed_ms: int,
    runtime_revision_value: str,
) -> dict[str, Any]:
    locked_runtime_revision = str(runtime_revision_value or "").strip()
    if not locked_runtime_revision:
        raise ValueError("Deterministic runtime revision is required")
    ordered_dependencies = sorted(
        (item.to_dict() for item in dependencies),
        key=lambda item: (
            str(item.get("kind") or "").encode("utf-8"),
            str(item.get("ref") or "").encode("utf-8"),
        ),
    )
    seen_dependencies: set[tuple[str, str]] = set()
    for item in ordered_dependencies:
        key = (str(item["kind"]), str(item["ref"]))
        if key in seen_dependencies:
            raise ValueError("Duplicate deterministic dependency")
        seen_dependencies.add(key)

    ordered_artifacts = sorted(
        (dict(item) for item in artifacts),
        key=lambda item: str(item.get("name") or "").encode("utf-8"),
    )
    names = [str(item.get("name") or "") for item in ordered_artifacts]
    if any(not name for name in names) or len(names) != len(set(names)):
        raise ValueError("Invalid deterministic artifact names")

    result_material = {
        "structured_result": dict(structured_result),
        "artifacts": [
            {
                key: item[key]
                for key in ("name", "media_type", "size", "sha256")
                if key in item
            }
            for item in ordered_artifacts
        ],
    }
    return {
        "status": "success",
        "structured_result": dict(structured_result),
        "artifacts": ordered_artifacts,
        "execution": {
            "type": "deterministic",
            "authoritative": True,
            "qualification": "trusted_package_contract_v1",
            "complete": True,
            "ontology_id": ontology_id,
            "ontology_version": ontology_version,
            "operation_key": operation_key,
            "package_revision": package_revision,
            "runtime_revision": locked_runtime_revision,
            "handler": {
                "type": "script",
                "ref": handler_ref,
                "sha256": handler_sha256,
            },
            "dependencies": ordered_dependencies,
            "arguments_sha256": canonical_sha256(dict(arguments)),
            "result_sha256": canonical_sha256(result_material),
            "handler_execution_count": 1,
            "elapsed_ms": max(0, int(elapsed_ms)),
        },
    }
