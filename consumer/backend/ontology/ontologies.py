"""Ontology loading and registry helpers."""

import logging
import os
import re
import shutil
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml
from pydantic import BaseModel, Field
from utils.atomic_files import staged_bytes
from utils.read_docs import build_read_document_record
from workspace.workspaces import (
    get_workspace_catalog_generation,
    get_workspace_ontologies_root,
    get_workspace_project_root,
)

logger = logging.getLogger("ontology.ontologies")

SELECTED_ONTOLOGY_CONTEXT_START = "<Selected-Ontology>"
SELECTED_ONTOLOGY_CONTEXT_END = "</Selected-Ontology>"
ONTOLOGY_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
ONTOLOGY_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
ONTOLOGY_SHARED_MANIFEST_NAME = "manifest.yaml"
ONTOLOGY_VERSIONS_DIR_NAME = "versions"
SUPPORTED_ONTOLOGY_SCHEMA_VERSIONS = frozenset({"0.1", "0.2"})
_FORBIDDEN_RUNTIME_BINDING_KEY_PARTS = frozenset({"endpoint", "token", "secret", "platform"})


def validate_no_runtime_binding_data(value: Any) -> None:
    """Reject runtime binding data from package-owned declarative contracts."""

    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = str(raw_key or "").strip()
            separated = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key)
            normalized = re.sub(r"[^a-z0-9]+", "_", separated.lower()).strip("_")
            key_parts = frozenset(part for part in normalized.split("_") if part)
            if normalized == "runtime_binding" or key_parts.intersection(_FORBIDDEN_RUNTIME_BINDING_KEY_PARTS):
                raise ValueError("Forbidden runtime binding field in ontology package: {}".format(key or "<empty>"))
            validate_no_runtime_binding_data(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            validate_no_runtime_binding_data(item)


def validate_ontology_manifest_schema(manifest: Dict[str, Any]) -> str:
    """Validate and return the schema revision declared by a package manifest."""
    if not isinstance(manifest, dict):
        raise ValueError("Ontology manifest must be a mapping")
    validate_no_runtime_binding_data(manifest)
    schema_version = str(manifest.get("schema_version") or "").strip()
    if schema_version not in SUPPORTED_ONTOLOGY_SCHEMA_VERSIONS:
        raise ValueError("Unsupported ontology schema version")
    return schema_version


def _coerce_manifest_enabled(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"false", "0", "no", "n", "off", "disabled", "禁用"}:
        return False
    if text in {"true", "1", "yes", "y", "on", "enabled", "启用"}:
        return True
    return bool(value)


class OntologyManifest(BaseModel):
    """Represents a discovered ontology manifest."""

    kind: str = Field(..., description="Manifest type identifier")
    schema_version: str = Field(..., description="Ontology package schema version")
    id: str = Field(..., description="Ontology identifier")
    name: str = Field(..., description="Ontology display name")
    description: str = Field(..., description="Ontology summary")
    version: str = Field(..., description="Ontology version")
    active_version: str = Field(..., description="Active ontology version")
    versions: List[str] = Field(default_factory=list, description="Available ontology versions")
    language: str = Field(..., description="Primary ontology language")
    enabled: bool = Field(True, description="Whether the ontology is available to model-facing runtime paths")
    scope: List[str] = Field(default_factory=list, description="Supported business scope")
    manifest_path: str = Field(..., description="Path to manifest.yaml")
    ontology_dir: str = Field(..., description="Ontology directory path")
    index_path: Optional[str] = Field(None, description="Optional package index.md path")


class OntologyLoader:
    """Load ontology manifests from ontology directories."""

    @staticmethod
    def load_ontology(
        ontology_path: Path,
        *,
        version: Optional[str] = None,
    ) -> Optional[OntologyManifest]:
        manifest_path = ontology_path / "manifest.yaml"
        if not manifest_path.is_file() or manifest_path.is_symlink():
            logger.warning("Ontology manifest not found: %s", manifest_path)
            return None

        try:
            raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            logger.error("Failed to parse ontology manifest %s: %s", manifest_path, exc)
            return None
        if not isinstance(raw, dict):
            logger.error("Ontology manifest must be a mapping: %s", manifest_path)
            return None
        try:
            validate_ontology_manifest_schema(raw)
        except ValueError as exc:
            logger.error("Invalid ontology manifest %s: %s", manifest_path, exc)
            return None

        manifest_version = str(raw.get("version") or "").strip()
        active_version = str(raw.get("active_version") or manifest_version).strip()
        versions = _normalize_manifest_versions(raw.get("versions"), active_version or manifest_version)
        if not active_version and versions:
            active_version = versions[0]
        selected_version = str(version or active_version or manifest_version).strip()

        shared_kind = str(raw.get("kind")).strip()
        shared_schema_version = str(raw.get("schema_version") or "").strip()
        if shared_kind != "ontology-package" or shared_schema_version not in SUPPORTED_ONTOLOGY_SCHEMA_VERSIONS:
            logger.error(
                "Ontology manifest must declare kind=ontology-package and a supported schema version: %s",
                manifest_path,
            )
            return None
        if not is_valid_ontology_version(selected_version) or selected_version not in versions:
            logger.error(
                "Ontology version '%s' is not declared by manifest: %s",
                selected_version,
                manifest_path,
            )
            return None

        try:
            active_dir = get_ontology_version_dir(ontology_path, selected_version, create=False)
            if active_dir is None:
                logger.error(
                    "Ontology version '%s' not found for manifest: %s",
                    selected_version,
                    manifest_path,
                )
                return None
            if shared_schema_version == "0.1":
                _validate_revision_with_legacy_backfill(
                    active_dir,
                    raw,
                    selected_version,
                )

            selected_manifest_path = manifest_path
            package_raw = dict(raw)
            version_manifest_path = active_dir / ONTOLOGY_SHARED_MANIFEST_NAME
            if version_manifest_path.is_file() and not version_manifest_path.is_symlink():
                try:
                    version_raw = yaml.safe_load(version_manifest_path.read_text(encoding="utf-8")) or {}
                except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
                    logger.error(
                        "Failed to parse ontology version manifest %s: %s",
                        version_manifest_path,
                        exc,
                    )
                    return None
                if not isinstance(version_raw, dict):
                    logger.error(
                        "Ontology version manifest must be a mapping: %s",
                        version_manifest_path,
                    )
                    return None
                try:
                    validate_ontology_manifest_schema(version_raw)
                except ValueError:
                    logger.error(
                        "Ontology version manifest declares unsupported schema version: %s",
                        version_manifest_path,
                    )
                    return None
                package_raw.update(version_raw)
                package_raw["enabled"] = raw.get("enabled", True)
                selected_manifest_path = version_manifest_path

            required_fields = ["kind", "id", "name", "description", "language"]
            missing_fields = [field for field in required_fields if not package_raw.get(field)]
            if not active_version:
                missing_fields.append("version")
            if missing_fields:
                logger.error(
                    "Ontology manifest missing fields %s: %s",
                    missing_fields,
                    selected_manifest_path,
                )
                return None

            kind = str(package_raw.get("kind")).strip()
            schema_version = str(package_raw.get("schema_version") or "").strip()
            if kind != "ontology-package" or schema_version not in SUPPORTED_ONTOLOGY_SCHEMA_VERSIONS:
                logger.error(
                    "Ontology version manifest must declare a supported package schema: %s",
                    selected_manifest_path,
                )
                return None

            index_path = active_dir / "index.md"
            return OntologyManifest(
                kind=kind,
                schema_version=schema_version,
                id=str(package_raw.get("id")).strip(),
                name=str(package_raw.get("name")).strip(),
                description=str(package_raw.get("description")).strip(),
                version=selected_version,
                active_version=active_version,
                versions=versions or [active_version],
                language=str(package_raw.get("language")).strip(),
                enabled=_coerce_manifest_enabled(package_raw.get("enabled", True)),
                scope=[str(item).strip() for item in (package_raw.get("scope") or []) if str(item).strip()],
                manifest_path=str(selected_manifest_path),
                ontology_dir=str(active_dir),
                index_path=(str(index_path) if index_path.exists() else None),
            )
        except Exception as exc:
            logger.error("Failed to build ontology manifest model from %s: %s", manifest_path, exc)
            return None


def is_valid_ontology_version(version: str) -> bool:
    return bool(str(version or "").strip() and ONTOLOGY_VERSION_PATTERN.match(str(version).strip()))


def _normalize_manifest_versions(value: Any, default_version: str = "") -> List[str]:
    versions: List[str] = []
    if isinstance(value, list):
        candidates = value
    elif value:
        candidates = [value]
    else:
        candidates = []

    for item in candidates:
        version = str(item or "").strip()
        if version and version not in versions:
            versions.append(version)

    default_version = str(default_version or "").strip()
    if default_version and default_version not in versions:
        versions.insert(0, default_version)
    return versions


def _write_revision_manifest_atomic(manifest_path: Path, manifest: Dict[str, Any]) -> None:
    payload = yaml.safe_dump(
        manifest,
        allow_unicode=True,
        sort_keys=False,
    ).encode("utf-8")
    with staged_bytes(
        payload=payload,
        prefix=".manifest-",
        suffix=".tmp",
        directory=str(manifest_path.parent),
    ) as temporary_path:
        os.replace(temporary_path, manifest_path)


def _validate_revision_with_legacy_backfill(
    revision_root: Path,
    shared_manifest: Dict[str, Any],
    version: str,
) -> None:
    """Backfill pre-revision-manifest v0.1 installs, then validate strictly."""

    if not revision_root.is_dir() or revision_root.is_symlink():
        raise ValueError("Ontology revision directory is missing or unsafe")
    if not is_valid_ontology_version(version):
        raise ValueError("Invalid ontology version")

    revision_manifest_path = revision_root / ONTOLOGY_SHARED_MANIFEST_NAME
    created_manifest = False
    if not revision_manifest_path.is_file() or revision_manifest_path.is_symlink():
        if revision_manifest_path.exists() or revision_manifest_path.is_symlink():
            raise ValueError("Ontology revision manifest is missing or unsafe")
        if str(shared_manifest.get("schema_version") or "").strip() != "0.1":
            raise ValueError("Ontology revision manifest is missing or unsafe")

        revision_manifest = dict(shared_manifest)
        revision_manifest["version"] = version
        revision_manifest.pop("active_version", None)
        revision_manifest.pop("versions", None)
        _write_revision_manifest_atomic(revision_manifest_path, revision_manifest)
        created_manifest = True

    from ontology.package_import import validate_ontology_revision

    try:
        validate_ontology_revision(revision_root)
    except Exception:
        if created_manifest:
            revision_manifest_path.unlink(missing_ok=True)
        raise


def load_ontology_manifest_data(ontology_dir: Path) -> Dict[str, Any]:
    manifest_path = ontology_dir / ONTOLOGY_SHARED_MANIFEST_NAME
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ValueError("Ontology manifest not found")
    raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    schema_version = validate_ontology_manifest_schema(raw)
    versions_dir = ontology_dir / ONTOLOGY_VERSIONS_DIR_NAME
    if versions_dir.exists() or versions_dir.is_symlink():
        if not versions_dir.is_dir() or versions_dir.is_symlink():
            raise ValueError("Ontology versions directory is missing or unsafe")
        active_version = str(raw.get("active_version") or raw.get("version") or "").strip()
        revision_root = versions_dir / active_version
        if schema_version == "0.2":
            _validate_revision_with_legacy_backfill(
                revision_root,
                raw,
                active_version,
            )
        elif not revision_root.is_dir() or revision_root.is_symlink():
            raise ValueError("Ontology revision directory is missing or unsafe")
    elif schema_version == "0.2":
        revision_root = ontology_dir
        from ontology.package_import import validate_ontology_revision

        validate_ontology_revision(revision_root)
    return raw


def save_ontology_manifest_data(ontology_dir: Path, manifest: Dict[str, Any]) -> None:
    (ontology_dir / ONTOLOGY_SHARED_MANIFEST_NAME).write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _copy_version_payload(source_root: Path, target_root: Path) -> None:
    target_root.mkdir(parents=True, exist_ok=True)
    for child in source_root.iterdir():
        if child.name in {ONTOLOGY_SHARED_MANIFEST_NAME, ONTOLOGY_VERSIONS_DIR_NAME}:
            continue
        target_path = target_root / child.name
        if child.is_dir():
            if target_path.exists():
                shutil.rmtree(target_path)
            shutil.copytree(child, target_path)
        else:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(child, target_path)


def _remove_root_version_payload(ontology_dir: Path) -> None:
    for child in ontology_dir.iterdir():
        if child.name in {ONTOLOGY_SHARED_MANIFEST_NAME, ONTOLOGY_VERSIONS_DIR_NAME}:
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def ensure_ontology_versions(ontology_dir: Path) -> Dict[str, Any]:
    manifest = load_ontology_manifest_data(ontology_dir)
    version = str(manifest.get("active_version") or manifest.get("version") or "1.0.0").strip()
    if not is_valid_ontology_version(version):
        raise ValueError("Invalid ontology version")

    versions = _normalize_manifest_versions(manifest.get("versions"), version)
    versions_dir = ontology_dir / ONTOLOGY_VERSIONS_DIR_NAME
    version_dir = versions_dir / version

    migrating = not versions_dir.exists()
    if versions_dir.is_symlink():
        raise ValueError("Ontology versions directory must not be a symbolic link")
    if migrating:
        _copy_version_payload(ontology_dir, version_dir)
    elif not version_dir.is_dir() or version_dir.is_symlink():
        raise ValueError("Ontology revision directory is missing or unsafe")
    version_manifest_path = version_dir / ONTOLOGY_SHARED_MANIFEST_NAME
    if migrating:
        revision_manifest = dict(manifest)
        revision_manifest["version"] = version
        revision_manifest.pop("active_version", None)
        revision_manifest.pop("versions", None)
        _write_revision_manifest_atomic(version_manifest_path, revision_manifest)
    _validate_revision_with_legacy_backfill(version_dir, manifest, version)
    _remove_root_version_payload(ontology_dir)

    manifest.pop("capabilities", None)
    manifest["version"] = version
    manifest["active_version"] = version
    manifest["versions"] = versions
    if "enabled" not in manifest:
        manifest["enabled"] = True
    save_ontology_manifest_data(ontology_dir, manifest)
    return manifest


def get_ontology_version_dir(
    ontology_dir: Path,
    version: Optional[str] = None,
    *,
    create: bool = False,
    manifest: Optional[Dict[str, Any]] = None,
) -> Optional[Path]:
    if manifest is None:
        manifest = load_ontology_manifest_data(ontology_dir)
    active_version = str(manifest.get("active_version") or manifest.get("version") or "").strip()
    target_version = str(version or active_version).strip()
    if not target_version:
        return ontology_dir

    versions_dir = ontology_dir / ONTOLOGY_VERSIONS_DIR_NAME
    if not versions_dir.is_dir():
        if versions_dir.exists() or versions_dir.is_symlink():
            return None
        if create:
            ensure_ontology_versions(ontology_dir)
        else:
            return ontology_dir if target_version == str(manifest.get("version") or "").strip() else None
    if versions_dir.is_symlink():
        return None

    version_dir = ontology_dir / ONTOLOGY_VERSIONS_DIR_NAME / target_version
    if version_dir.is_dir() and not version_dir.is_symlink():
        if str(manifest.get("schema_version") or "").strip() == "0.2":
            _validate_revision_with_legacy_backfill(
                version_dir,
                manifest,
                target_version,
            )
        return version_dir
    return None


def validate_ontology_version_revision(
    ontology_dir: Path,
    version: str,
) -> Path:
    """Validate one installed revision, backfilling legacy v0.1 metadata."""

    manifest = load_ontology_manifest_data(ontology_dir)
    version_dir = get_ontology_version_dir(ontology_dir, version, create=False)
    if version_dir is None:
        raise ValueError("Ontology revision directory is missing or unsafe")
    _validate_revision_with_legacy_backfill(version_dir, manifest, version)
    return version_dir


def set_ontology_active_version(ontology_dir: Path, version: str) -> OntologyManifest:
    if not is_valid_ontology_version(version):
        raise ValueError("Invalid ontology version")
    manifest = ensure_ontology_versions(ontology_dir)
    versions = _normalize_manifest_versions(manifest.get("versions"), str(manifest.get("version") or ""))
    if version not in versions:
        raise ValueError("Ontology version not found")
    try:
        validate_ontology_version_revision(ontology_dir, version)
    except ValueError as exc:
        if "revision directory" in str(exc):
            raise ValueError("Ontology version not found") from exc
        raise
    manifest.pop("capabilities", None)

    manifest["active_version"] = version
    manifest["version"] = version
    manifest["versions"] = versions
    save_ontology_manifest_data(ontology_dir, manifest)

    ontology = OntologyLoader.load_ontology(ontology_dir)
    if ontology is None:
        raise ValueError("Failed to reload ontology manifest")
    return ontology


class OntologyRegistry:
    """Registry for discovering and managing ontologies under configured roots."""

    def __init__(
        self,
        ontology_dirs: Optional[List[Path]] = None,
        *,
        check_interval_seconds: float = 1.5,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._ontologies: Dict[str, OntologyManifest] = {}
        self._refresh_lock = threading.RLock()
        self.ontology_dirs = ontology_dirs or []
        self._source_signature: Optional[Tuple[Any, ...]] = None
        self._check_interval_seconds = max(
            0.0,
            float(check_interval_seconds),
        )
        self._clock = clock
        self._next_source_check_at = 0.0

    @property
    def ontologies(self) -> Dict[str, OntologyManifest]:
        return dict(self._ontologies)

    def _discover_ontologies_snapshot(self) -> Tuple[Dict[str, OntologyManifest], int]:
        ontologies: Dict[str, OntologyManifest] = {}
        loaded_count = 0

        for ontology_dir in self.ontology_dirs:
            if not ontology_dir.exists():
                logger.warning("Ontology directory does not exist: %s", ontology_dir)
                continue

            logger.info("Scanning for ontologies in: %s", ontology_dir)

            for item in ontology_dir.iterdir():
                if not item.is_dir():
                    continue

                ontology = OntologyLoader.load_ontology(item)
                if ontology is None:
                    continue

                ontologies[ontology.id] = ontology
                loaded_count += 1
                logger.info("Registered ontology '%s' from %s", ontology.id, item.name)

        return ontologies, loaded_count

    def refresh(self) -> int:
        with self._refresh_lock:
            stable_signature: Optional[Tuple[Any, ...]] = None
            ontologies: Dict[str, OntologyManifest] = {}
            loaded_count = 0
            for _attempt in range(3):
                before_signature = _build_registry_source_signature(self.ontology_dirs)
                ontologies, loaded_count = self._discover_ontologies_snapshot()
                after_signature = _build_registry_source_signature(self.ontology_dirs)
                if before_signature == after_signature:
                    stable_signature = after_signature
                    break
            self._ontologies = ontologies
            # An unstable final attempt remains stale and will be retried on the
            # next registry lookup instead of pairing old data with a new marker.
            self._source_signature = stable_signature
            self._next_source_check_at = (
                self._clock() + self._check_interval_seconds if stable_signature is not None else self._clock()
            )

        logger.info("Ontology discovery complete: %s ontologies loaded", loaded_count)
        return loaded_count

    def discover_ontologies(self) -> int:
        return self.refresh()

    def is_stale(self) -> bool:
        """Return whether the filesystem backing this registry changed."""
        with self._refresh_lock:
            now = self._clock()
            if self._source_signature is not None and now < self._next_source_check_at:
                return False
            current_signature = _build_registry_source_signature(self.ontology_dirs)
            self._next_source_check_at = now + self._check_interval_seconds
            return self._source_signature != current_signature

    def get_ontology(self, ontology_id: str) -> Optional[OntologyManifest]:
        return self._ontologies.get(ontology_id)

    def list_ontologies(self) -> List[str]:
        return list(self._ontologies.keys())

    def list_enabled_ontologies(self) -> List[str]:
        return [ontology_id for ontology_id, ontology in self._ontologies.items() if ontology.enabled]


_global_registry: Optional[OntologyRegistry] = None
_registry_lock = threading.RLock()
_registries: Dict[Tuple[Any, ...], OntologyRegistry] = {}


def _file_stat_signature(path: Path) -> Tuple[bool, int, int]:
    try:
        stat = path.stat()
    except OSError:
        return False, 0, 0
    return True, int(stat.st_mtime_ns), int(stat.st_size)


def _build_registry_source_signature(ontology_dirs: List[Path]) -> Tuple[Any, ...]:
    """Build a cheap cross-process change marker for ontology discovery roots."""
    signature: List[Any] = []
    for root in ontology_dirs:
        resolved_root = root.resolve()
        children: List[Any] = []
        try:
            entries = sorted(
                (item for item in resolved_root.iterdir() if item.is_dir()),
                key=lambda item: item.name.lower(),
            )
        except OSError:
            entries = []
        for child in entries:
            children.append(
                (
                    child.name,
                    _file_stat_signature(child),
                    _file_stat_signature(child / ONTOLOGY_SHARED_MANIFEST_NAME),
                )
            )
        signature.append(
            (
                str(resolved_root),
                _file_stat_signature(resolved_root),
                tuple(children),
            )
        )
    return tuple(signature)


def _get_backend_root() -> Path:
    this_file = Path(__file__).resolve()
    utils_dir = this_file.parent
    agentic_dir = utils_dir.parent
    backend_dir = agentic_dir.parent
    return backend_dir


def get_ontology_registry(
    base_dir: Optional[Path] = None,
    *,
    workspace_id: Optional[str] = None,
) -> OntologyRegistry:
    global _global_registry

    if base_dir is None:
        ontology_dirs = [get_workspace_ontologies_root(workspace_id)]
    else:
        ontology_dirs = [(base_dir / "ontologies").resolve()]

    root_key = tuple(str(path.resolve()) for path in ontology_dirs)
    workspace_generation = get_workspace_catalog_generation() if base_dir is None else 0
    key: Tuple[Any, ...] = (workspace_generation, *root_key)
    with _registry_lock:
        if _global_registry is None and _registries:
            _registries.clear()

        for stale_key in tuple(_registries):
            if stale_key[1:] == root_key and stale_key != key:
                stale_registry = _registries.pop(stale_key)
                if _global_registry is stale_registry:
                    _global_registry = None

        registry = _registries.get(key)
        if registry is not None:
            _global_registry = registry

    if registry is not None:
        if registry.is_stale():
            registry.refresh()
        return registry

    candidate = OntologyRegistry(ontology_dirs=ontology_dirs)
    candidate.refresh()

    with _registry_lock:
        registry = _registries.get(key)
        if registry is None:
            registry = candidate
            _registries[key] = registry

        _global_registry = registry
        return registry


def invalidate_ontology_registry(
    *,
    base_dir: Optional[Path] = None,
    workspace_id: Optional[str] = None,
) -> None:
    """Evict one cached registry after a known workspace/package mutation."""
    global _global_registry

    if base_dir is None:
        ontology_dirs = [get_workspace_ontologies_root(workspace_id)]
    else:
        ontology_dirs = [(base_dir / "ontologies").resolve()]
    root_key = tuple(str(path.resolve()) for path in ontology_dirs)
    with _registry_lock:
        removed = [_registries.pop(key) for key in tuple(_registries) if key[1:] == root_key]
        if any(_global_registry is registry for registry in removed):
            _global_registry = next(iter(_registries.values()), None)


def get_runtime_ontology_manifest(
    ontology_id: str,
    *,
    version: Optional[str] = None,
    workspace_id: Optional[str] = None,
) -> Optional[OntologyManifest]:
    normalized_id = str(ontology_id or "").strip()
    if not ONTOLOGY_ID_PATTERN.fullmatch(normalized_id):
        return None
    ontologies_root = get_workspace_ontologies_root(workspace_id).resolve()
    ontology_dir = (ontologies_root / normalized_id).resolve()
    try:
        ontology_dir.relative_to(ontologies_root)
    except ValueError:
        return None
    if not ontology_dir.is_dir():
        return None
    ontology = OntologyLoader.load_ontology(ontology_dir, version=version)
    if ontology is None or ontology.id != normalized_id:
        return None
    return ontology


def get_sole_enabled_ontology_id(
    workspace_id: Optional[str] = None,
) -> Optional[str]:
    """Return the ontology id when the workspace has exactly one enabled ontology.

    Used to auto-attach the sole ontology's preload context when the session
    has no explicit selection. Returns None for zero or multiple candidates.
    """
    try:
        enabled_ids = get_ontology_registry(workspace_id=workspace_id).list_enabled_ontologies()
    except Exception:
        logger.warning("Failed to resolve sole enabled ontology", exc_info=True)
        return None
    if len(enabled_ids) == 1:
        return enabled_ids[0]
    return None


def build_ontologies_section(
    selected_ontology_id: Optional[str] = None,
    *,
    workspace_id: Optional[str] = None,
) -> str:
    registry = get_ontology_registry(workspace_id=workspace_id)
    ontologies = registry.ontologies

    selected_id = str(selected_ontology_id or "").strip()
    selected_ontology = ontologies.get(selected_id) if selected_id else None
    if selected_id and selected_ontology is None:
        logger.warning("Selected ontology id not found in registry: %s", selected_id)
    if selected_ontology is not None and not selected_ontology.enabled:
        logger.warning("Selected ontology id is disabled and will not be exposed: %s", selected_id)
        selected_ontology = None

    lines = []

    lines = ["<Available-Ontologies>"]

    if selected_id:
        ontology_ids = [selected_ontology.id] if selected_ontology is not None else []
    else:
        ontology_ids = sorted(ontology_id for ontology_id, ontology in ontologies.items() if ontology.enabled)
    for ontology_id in ontology_ids:
        ontology = ontologies[ontology_id]
        lines.append("- {}: {}".format(ontology.id, ontology.description))

    lines.append("</Available-Ontologies>")

    return "\n".join(lines)


def build_selected_ontology_preload_context(
    selected_ontology_id: Optional[str] = None,
    *,
    selected_ontology_version: Optional[str] = None,
    workspace_id: Optional[str] = None,
) -> Optional[str]:
    selected_id = str(selected_ontology_id or "").strip()
    if not selected_id:
        return None

    ontology = get_runtime_ontology_manifest(
        selected_id,
        version=selected_ontology_version,
        workspace_id=workspace_id,
    )
    if ontology is None:
        logger.warning("Selected ontology id not found in registry for preload: %s", selected_id)
        return None
    if not ontology.enabled:
        logger.warning("Selected ontology id is disabled for preload: %s", selected_id)
        return None

    return build_selected_ontology_preload_context_from_manifest(ontology)


def build_selected_ontology_preload_context_from_manifest(
    ontology: OntologyManifest,
    *,
    root_payload: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Build preload context from an already resolved exact manifest."""

    if not ontology.enabled:
        return None

    lines = [SELECTED_ONTOLOGY_CONTEXT_START]
    try:
        from ontology.ontology_catalog import build_root_payload, format_root_payload_compact

        if root_payload is None:
            root_payload = build_root_payload(
                ontology=ontology,
                ontology_dir=Path(ontology.ontology_dir).resolve(),
            )
        lines.extend(
            [
                *format_root_payload_compact(ontology_id=ontology.id, root_payload=root_payload),
            ]
        )

        lines.append(SELECTED_ONTOLOGY_CONTEXT_END)
    except Exception:
        logger.warning(
            "Failed to build selected ontology root context: %s",
            ontology.id,
            exc_info=True,
        )

    return "\n".join(lines)


def build_selected_ontology_preload_records(
    selected_ontology_id: Optional[str] = None,
    *,
    selected_ontology_version: Optional[str] = None,
    turn_id: int = 0,
    workspace_id: Optional[str] = None,
) -> List[Dict[str, object]]:
    selected_id = str(selected_ontology_id or "").strip()
    if not selected_id:
        return []

    ontology = get_runtime_ontology_manifest(
        selected_id,
        version=selected_ontology_version,
        workspace_id=workspace_id,
    )
    if ontology is None:
        logger.warning("Selected ontology id not found in registry for preload records: %s", selected_id)
        return []
    if not ontology.enabled:
        logger.warning("Selected ontology id is disabled for preload records: %s", selected_id)
        return []

    return build_selected_ontology_preload_records_from_manifest(
        ontology,
        turn_id=turn_id,
        workspace_id=workspace_id,
    )


def build_selected_ontology_preload_records_from_manifest(
    ontology: OntologyManifest,
    *,
    turn_id: int = 0,
    workspace_id: Optional[str] = None,
) -> List[Dict[str, object]]:
    """Build read-ledger records from an already resolved exact manifest."""

    if not ontology.enabled:
        return []

    project_root = get_workspace_project_root(workspace_id)
    records: List[Dict[str, object]] = []
    for _doc_name, doc_path, _required in _resolve_selected_ontology_root_docs(ontology):
        if not doc_path.exists():
            continue
        read_path = _build_read_path(str(doc_path), project_root)
        if not read_path:
            continue
        try:
            content = doc_path.read_text(encoding="utf-8")
        except OSError:
            logger.warning("Failed to preload root doc for read ledger: %s", doc_path, exc_info=True)
            continue
        records.append(
            build_read_document_record(
                file_path=read_path,
                resolved_path=doc_path,
                content=content,
                source="preload",
                turn_id=turn_id,
                iteration=None,
                ontology_version=str(getattr(ontology, "version", "") or "").strip() or None,
            )
        )
    return records


def _resolve_selected_ontology_root_docs(ontology: OntologyManifest) -> List[tuple[str, Path, bool]]:
    ontology_dir = Path(ontology.ontology_dir).resolve()
    return [
        ("manifest.yaml", Path(ontology.manifest_path).resolve(), True),
        ("index.md", (ontology_dir / "index.md").resolve(), False),
        ("objects/index.md", (ontology_dir / "objects" / "index.md").resolve(), False),
        ("operations/index.md", (ontology_dir / "operations" / "index.md").resolve(), False),
        ("rules/index.md", (ontology_dir / "rules" / "index.md").resolve(), False),
        ("metrics/index.md", (ontology_dir / "metrics" / "index.md").resolve(), False),
        ("scripts/index.md", (ontology_dir / "scripts" / "index.md").resolve(), False),
    ]


def _build_read_path(file_path: str, project_root: Path) -> Optional[str]:
    resolved_path = Path(file_path).resolve()
    try:
        return str(resolved_path.relative_to(project_root)).replace("\\", "/")
    except ValueError:
        try:
            backend_root = _get_backend_root().resolve()
            return str(resolved_path.relative_to(backend_root)).replace("\\", "/")
        except ValueError:
            return None
