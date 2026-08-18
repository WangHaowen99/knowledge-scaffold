"""Single source of truth for the deterministic JiuwenBox runtime contract."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


_CONTRACT_PATH = Path(__file__).with_name("runtime-contract.json")
_REQUIREMENTS_PATH = Path(__file__).with_name("runtime-requirements.txt")
_REVISION_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _valid_runtime_import_map(value: Any) -> bool:
    if not isinstance(value, dict) or not value:
        return False
    for module, exports in value.items():
        if not str(module or "").strip():
            return False
        if not isinstance(exports, list) or not exports:
            return False
        if any(not str(name or "").strip() for name in exports):
            return False
    return True


@lru_cache(maxsize=1)
def load_sandbox_runtime_contract() -> Mapping[str, Any]:
    """Load and minimally validate the image contract shipped with backend."""
    payload = json.loads(_CONTRACT_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise RuntimeError("sandbox runtime contract version is invalid")
    upstream = payload.get("upstream")
    python_contract = payload.get("python")
    allowed_roots = payload.get("allowed_external_import_roots")
    runtime_import_map = payload.get("allowed_runtime_imports")
    revision_file = str(payload.get("revision_file") or "").strip()
    shared_revision_file = str(payload.get("shared_revision_file") or "").strip()
    if not isinstance(upstream, dict):
        raise RuntimeError("sandbox runtime contract is incomplete")
    if not str(payload.get("runner_protocol") or "").strip():
        raise RuntimeError("sandbox runtime contract is incomplete")
    if not str(upstream.get("commit") or "").strip():
        raise RuntimeError("sandbox runtime contract is incomplete")
    if not isinstance(python_contract, dict):
        raise RuntimeError("sandbox runtime contract is incomplete")
    if not str(python_contract.get("executable") or "").strip():
        raise RuntimeError("sandbox runtime contract is incomplete")
    if not isinstance(allowed_roots, list):
        raise RuntimeError("sandbox runtime contract is incomplete")
    if not all(str(item or "").strip() for item in allowed_roots):
        raise RuntimeError("sandbox runtime contract is incomplete")
    if not _valid_runtime_import_map(runtime_import_map):
        raise RuntimeError("sandbox runtime contract is incomplete")
    if not revision_file.startswith("/app/sandbox-runtime/python/"):
        raise RuntimeError("sandbox runtime contract is incomplete")
    if not shared_revision_file.startswith("/run/onto-runtime-attestation/"):
        raise RuntimeError("sandbox runtime contract is incomplete")
    if payload.get("requirements_file") != _REQUIREMENTS_PATH.name:
        raise RuntimeError("sandbox runtime contract is incomplete")
    return payload


def allowed_external_import_roots() -> frozenset[str]:
    contract = load_sandbox_runtime_contract()
    return frozenset(
        str(item).strip().split(".", 1)[0]
        for item in contract["allowed_external_import_roots"]
    )


@lru_cache(maxsize=1)
def allowed_runtime_imports() -> Mapping[str, frozenset[str]]:
    """Return exact public runtime modules and the exports handlers may import."""
    contract = load_sandbox_runtime_contract()
    return MappingProxyType(
        {
            str(module).strip(): frozenset(str(name).strip() for name in exports)
            for module, exports in contract["allowed_runtime_imports"].items()
        }
    )


def sandbox_runtime_revision_file() -> str:
    return str(load_sandbox_runtime_contract()["revision_file"])


def shared_sandbox_runtime_revision_file() -> str:
    return str(load_sandbox_runtime_contract()["shared_revision_file"])


def sandbox_runner_protocol_version() -> str:
    return str(load_sandbox_runtime_contract()["runner_protocol"])


def _normalized_architecture(value: str | None = None) -> str:
    normalized = str(value or platform.machine() or "unknown").strip().casefold()
    return {
        "aarch64": "arm64",
        "arm64": "arm64",
        "amd64": "amd64",
        "x86_64": "amd64",
    }.get(normalized, normalized)


@lru_cache(maxsize=16)
def _calculated_sandbox_runtime_revision(
    architecture: str | None,
    payload_image_id: str | None,
) -> str:
    """Hash the contract and exact behavior-bearing sandbox payload image."""
    backend_root = Path(__file__).resolve().parents[2]
    source_roots = (
        backend_root / "onto_sandbox_runtime",
        backend_root / "shared_capabilities" / "business_data",
        backend_root / "shared_capabilities" / "ontology_script_sdk",
    )
    source_paths = [backend_root / "shared_capabilities" / "__init__.py"]
    for source_root in source_roots:
        source_paths.extend(sorted(source_root.rglob("*.py")))

    source_entries: list[dict[str, str]] = []
    for source_path in sorted(set(source_paths)):
        try:
            digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
        except OSError:
            digest = "missing"
        source_entries.append(
            {
                "path": source_path.relative_to(backend_root).as_posix(),
                "sha256": digest,
            }
        )

    requirements = [
        line.strip()
        for line in _REQUIREMENTS_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    material = {
        "architecture": _normalized_architecture(architecture),
        "contract": load_sandbox_runtime_contract(),
        "requirements": requirements,
        "runtime_sources": source_entries,
    }
    if payload_image_id:
        material["payload_image_id"] = str(payload_image_id).strip()
    return "sha256:" + hashlib.sha256(_canonical_json_bytes(material)).hexdigest()


def sandbox_runtime_revision(
    architecture: str | None = None,
    payload_image_id: str | None = None,
) -> str:
    """Return the deployed revision, or calculate one for an image build.

    Release images publish their build-specific revision through a read-only
    shared volume.  Local non-container development falls back to the source
    contract revision and therefore still fails closed against a release image.
    """
    if architecture is None and payload_image_id is None:
        shared_path = Path(shared_sandbox_runtime_revision_file())
        try:
            shared_revision = shared_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            shared_revision = ""
        except OSError as exc:
            raise RuntimeError(
                "shared sandbox runtime revision could not be read"
            ) from exc
        else:
            if not _REVISION_PATTERN.fullmatch(shared_revision):
                raise RuntimeError("shared sandbox runtime revision is invalid")
            return shared_revision
        configured = str(os.getenv("SANDBOX_RUNTIME_REVISION") or "").strip()
        if configured:
            if not _REVISION_PATTERN.fullmatch(configured):
                raise RuntimeError("configured sandbox runtime revision is invalid")
            return configured
    return _calculated_sandbox_runtime_revision(architecture, payload_image_id)
