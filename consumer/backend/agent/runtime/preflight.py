"""Build exact, immutable products for one admitted runtime request."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from functools import partial
from pathlib import Path
from typing import Any

import yaml

from .contracts import RunPreflightSnapshot
from .preflight_cache import (
    ImmutablePreflightCache,
    PreflightCacheKey,
    freeze_preflight_product,
)


GenerationResolver = Callable[[str, str], tuple[int, int, int]]
PackageLoader = Callable[[Any, str], Mapping[str, Any]]
ModelProfileLoader = Callable[[], Mapping[str, Any] | None]
OntologyManifestLoader = Callable[[], Any]
DeterministicCandidateLoader = Callable[[Any], bool]

_DEFAULT_CACHE = ImmutablePreflightCache()
_MAX_GENERATION_RETRIES = 3
_CACHED_MANIFEST_KEY = "manifest"
_CACHED_PACKAGE_KEY = "package"


def _default_generation_resolver(
    workspace_id: str,
    ontology_id: str,
) -> tuple[int, int, int]:
    from config.system_config import get_system_config_generation
    from ontology.view_cache import (
        get_ontology_mutation_revision,
        observe_ontology_source_revision,
    )
    from workspace.workspaces import (
        get_workspace_config_generation,
        get_workspace_ontologies_root,
    )

    mutation_revision = 0
    if ontology_id:
        mutation_revision = observe_ontology_source_revision(
            workspace_id=workspace_id,
            ontology_id=ontology_id,
            ontology_root=(
                get_workspace_ontologies_root(workspace_id) / ontology_id
            ).resolve(),
        )

    return (
        get_workspace_config_generation(workspace_id),
        get_system_config_generation(),
        (
            max(
                mutation_revision,
                get_ontology_mutation_revision(workspace_id, ontology_id),
            )
            if ontology_id
            else 0
        ),
    )


def _default_package_loader(
    ontology_manifest: Any,
    workspace_id: str,
) -> Mapping[str, Any]:
    if ontology_manifest is None:
        return {
            "root_payload": None,
            "selected_ontology_context": None,
            "preload_records": (),
            "ontology_source_signature": None,
            "ontology_content_revision": None,
            "script_bindings": {},
            "execute_script_tool_schema": None,
            "solver_capabilities": (),
        }

    import hashlib

    from ontology.ontology_catalog import (
        build_root_payload,
        load_script_catalog,
        resolve_ontology_script_path,
    )
    from solver_coordination.capability_catalog import (
        SolverContractResolver,
    )
    from ontology.ontologies import (
        build_selected_ontology_preload_context_from_manifest,
        build_selected_ontology_preload_records_from_manifest,
    )
    from ontology.view_cache import (
        build_package_content_revision,
        build_package_revision_source_signature,
    )
    from agent.tools.execute_script import (
        build_execute_script_tool_schema_from_catalog,
    )

    ontology_dir = Path(ontology_manifest.ontology_dir).resolve()
    for _attempt in range(_MAX_GENERATION_RETRIES):
        before_signature = build_package_revision_source_signature(ontology_dir)
        before_content_revision = build_package_content_revision(ontology_dir)
        root_payload = build_root_payload(
            ontology=ontology_manifest,
            ontology_dir=ontology_dir,
            include_solver_catalog=False,
        )
        public_script_catalog = root_payload.get("script_catalog") or ()
        if not isinstance(public_script_catalog, (list, tuple)):
            public_script_catalog = ()
        # The root payload is a model-facing discovery surface and contains
        # only ``solver_summary``.  ToolCard construction needs the complete,
        # validated schemas, so load that private projection directly from
        # the exact package revision.  Keep the legacy root-catalog fallback
        # for tests and pre-0.2 adapters that provide only the old payload.
        solver_capabilities: tuple[Mapping[str, Any], ...] = ()
        manifest_path = ontology_dir / "manifest.yaml"
        if manifest_path.is_file() and not manifest_path.is_symlink():
            try:
                manifest_data = yaml.safe_load(
                    manifest_path.read_text(encoding="utf-8")
                ) or {}
            except (OSError, UnicodeError, yaml.YAMLError):
                manifest_data = {}
            if isinstance(manifest_data, Mapping):
                full_catalog = SolverContractResolver(
                    ontology_dir,
                    manifest_data,
                    package_revision=str(
                        getattr(ontology_manifest, "version", "") or ""
                    ).strip()
                    or None,
                ).load_public()
                solver_capabilities = tuple(
                    entry
                    for entry in full_catalog
                    if isinstance(entry, Mapping)
                )
        if not solver_capabilities:
            legacy_solver_catalog = root_payload.get("solver_catalog") or ()
            if not isinstance(legacy_solver_catalog, (list, tuple)):
                legacy_solver_catalog = ()
            solver_capabilities = tuple(
                entry
                for entry in legacy_solver_catalog
                if isinstance(entry, Mapping)
            )
        script_bindings: dict[str, Mapping[str, Any]] = {}
        for entry in load_script_catalog(ontology_dir):
            operation_key = str(entry.get("operation_key") or "").strip()
            if not operation_key or not entry.get("_is_bound_operation"):
                continue
            raw_script_path = str(entry.get("_script_path") or "").strip()
            resolved_script_path = resolve_ontology_script_path(
                ontology_dir,
                raw_script_path,
            )
            if (
                resolved_script_path is None
                or not resolved_script_path.is_file()
                or resolved_script_path.is_symlink()
            ):
                continue
            binding = dict(entry)
            binding["_expected_script_path"] = str(resolved_script_path)
            binding["_expected_script_digest"] = (
                "sha256:"
                + hashlib.sha256(resolved_script_path.read_bytes()).hexdigest()
            )
            script_bindings.setdefault(operation_key, binding)
        admitted_script_catalog = []
        for entry in public_script_catalog:
            if not isinstance(entry, Mapping):
                continue
            operation_key = str(entry.get("operation_key") or "").strip()
            if operation_key in script_bindings:
                admitted_script_catalog.append(entry)
        execute_script_tool_schema = build_execute_script_tool_schema_from_catalog(
            ontology_ids=(
                str(getattr(ontology_manifest, "id", "") or "").strip(),
            ),
            script_catalog=tuple(admitted_script_catalog),
        )
        selected_context = build_selected_ontology_preload_context_from_manifest(
            ontology_manifest,
            root_payload=root_payload,
        )
        preload_records = build_selected_ontology_preload_records_from_manifest(
            ontology_manifest,
            workspace_id=workspace_id,
            turn_id=0,
        )
        after_signature = build_package_revision_source_signature(ontology_dir)
        after_content_revision = build_package_content_revision(ontology_dir)
        if (
            before_signature == after_signature
            and before_content_revision == after_content_revision
        ):
            return {
                "root_payload": root_payload,
                "selected_ontology_context": selected_context,
                "preload_records": preload_records,
                "ontology_source_signature": after_signature,
                "ontology_content_revision": after_content_revision,
                "script_bindings": script_bindings,
                "execute_script_tool_schema": execute_script_tool_schema,
                "solver_capabilities": solver_capabilities,
            }
    raise RuntimeError("ontology package changed repeatedly during preflight")


def _manifest_payload(ontology_manifest: Any) -> Mapping[str, Any] | None:
    if ontology_manifest is None:
        return None
    dump = getattr(ontology_manifest, "model_dump", None)
    if callable(dump):
        return dump(mode="python")
    if isinstance(ontology_manifest, Mapping):
        return ontology_manifest
    raise TypeError("ontology_manifest must be a manifest model or mapping")


def _manifest_identity(ontology_manifest: Any) -> tuple[str, str]:
    if ontology_manifest is None:
        return "", ""
    if isinstance(ontology_manifest, Mapping):
        ontology_id = ontology_manifest.get("id")
        ontology_version = ontology_manifest.get("version")
    else:
        ontology_id = getattr(ontology_manifest, "id", "")
        ontology_version = getattr(ontology_manifest, "version", "")
    return (
        str(ontology_id or "").strip(),
        str(ontology_version or "").strip(),
    )


def _manifest_ontology_dir(ontology_manifest: Any) -> Path | None:
    if ontology_manifest is None:
        return None
    if isinstance(ontology_manifest, Mapping):
        raw_path = ontology_manifest.get("ontology_dir")
    else:
        raw_path = getattr(ontology_manifest, "ontology_dir", None)
    normalized = str(raw_path or "").strip()
    return Path(normalized).resolve() if normalized else None


def _package_source_signature_matches(
    ontology_manifest: Any,
    expected_signature: Any,
) -> bool:
    if expected_signature is None:
        return True
    if not isinstance(expected_signature, tuple):
        return False
    ontology_dir = _manifest_ontology_dir(ontology_manifest)
    if ontology_dir is None:
        # Custom package loaders used by embedders may carry their own opaque
        # signature without exposing a local filesystem path.
        return True
    try:
        from ontology.view_cache import build_package_revision_source_signature

        return (
            build_package_revision_source_signature(ontology_dir)
            == expected_signature
        )
    except Exception:
        return False


def _package_content_revision_matches(
    ontology_manifest: Any,
    expected_revision: Any,
) -> bool:
    revision = str(expected_revision or "").strip()
    if (
        len(revision) != 71
        or not revision.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in revision[7:])
    ):
        return False
    ontology_dir = _manifest_ontology_dir(ontology_manifest)
    if ontology_dir is None:
        return False
    try:
        from ontology.view_cache import build_package_content_revision

        return build_package_content_revision(ontology_dir) == revision
    except Exception:
        return False


def ontology_preflight_revision_matches(snapshot: Any) -> bool:
    """Fail closed when an admitted package revision no longer exists."""

    ontology_id = str(getattr(snapshot, "ontology_id", "") or "").strip()
    if not ontology_id:
        return True
    workspace_id = str(getattr(snapshot, "workspace_id", "") or "").strip()
    mutation_revision = getattr(snapshot, "mutation_revision", None)
    manifest = getattr(snapshot, "ontology_manifest", None)
    source_signature = getattr(snapshot, "ontology_source_signature", None)
    content_revision = getattr(snapshot, "ontology_content_revision", None)
    if not workspace_id:
        return False
    if not isinstance(mutation_revision, int) or isinstance(mutation_revision, bool):
        return False
    if mutation_revision < 0:
        return False
    if not isinstance(manifest, Mapping):
        return False
    if not isinstance(source_signature, tuple):
        return False
    if _manifest_ontology_dir(manifest) is None:
        return False
    try:
        from ontology.view_cache import get_ontology_mutation_revision

        return (
            get_ontology_mutation_revision(workspace_id, ontology_id)
            == mutation_revision
            and _package_source_signature_matches(manifest, source_signature)
            and _package_content_revision_matches(manifest, content_revision)
        )
    except Exception:
        return False


def _generations(
    resolver: GenerationResolver,
    workspace_id: str,
    ontology_id: str,
) -> tuple[int, int, int]:
    resolved = tuple(resolver(workspace_id, ontology_id))
    if len(resolved) != 3:
        raise ValueError("generation_resolver must return exactly three values")
    for value in resolved:
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError("preflight generations must be non-negative integers")
    return resolved  # type: ignore[return-value]


def _load_package_cache_product(
    *,
    package_loader: PackageLoader,
    ontology_manifest: Any,
    workspace_id: str,
    manifest_payload: Any,
) -> Mapping[str, Any]:
    loaded = package_loader(ontology_manifest, workspace_id)
    if not isinstance(loaded, Mapping):
        raise TypeError("package_loader must return a mapping")
    return {
        _CACHED_MANIFEST_KEY: manifest_payload,
        _CACHED_PACKAGE_KEY: loaded,
    }


def build_run_preflight_snapshot(
    *,
    workspace_id: str,
    ontology_manifest: Any = None,
    ontology_id: str = "",
    ontology_version: str = "",
    ontology_manifest_loader: OntologyManifestLoader | None = None,
    deterministic_candidate: bool | None = None,
    deterministic_candidate_loader: DeterministicCandidateLoader | None = None,
    model_profile_loader: ModelProfileLoader | None,
    cache: ImmutablePreflightCache | None = None,
    generation_resolver: GenerationResolver | None = None,
    package_loader: PackageLoader | None = None,
) -> RunPreflightSnapshot:
    """Build one request snapshot without sharing request-local decisions.

    Package-derived products are cached under exact configuration and mutation
    generations.  The deterministic routing decision and resolved model profile
    are attached only after the shared product is loaded.
    """

    normalized_workspace = str(workspace_id or "").strip()
    if not normalized_workspace:
        raise ValueError("workspace_id must not be empty")
    fixed_ontology_id, fixed_ontology_version = _manifest_identity(
        ontology_manifest
    )
    expected_ontology_id = str(ontology_id or fixed_ontology_id or "").strip()
    expected_ontology_version = str(
        ontology_version or fixed_ontology_version or ""
    ).strip()
    resolve_generations = generation_resolver or _default_generation_resolver
    load_package = package_loader or _default_package_loader
    selected_cache = cache or _DEFAULT_CACHE

    for _attempt in range(_MAX_GENERATION_RETRIES):
        before = _generations(
            resolve_generations,
            normalized_workspace,
            expected_ontology_id,
        )
        current_manifest = (
            ontology_manifest_loader()
            if ontology_manifest_loader is not None
            else ontology_manifest
        )
        current_ontology_id, current_ontology_version = _manifest_identity(
            current_manifest
        )
        if expected_ontology_id:
            if not current_ontology_id:
                raise RuntimeError(
                    "ontology manifest became unavailable during preflight"
                )
            if expected_ontology_version and not current_ontology_version:
                raise RuntimeError(
                    "ontology manifest became unavailable during preflight"
                )
        if (
            expected_ontology_id
            and current_ontology_id
            and current_ontology_id != expected_ontology_id
        ):
            raise ValueError("ontology manifest id changed during preflight")
        if (
            expected_ontology_version
            and current_ontology_version
            and current_ontology_version != expected_ontology_version
        ):
            raise ValueError("ontology manifest version changed during preflight")
        key = PreflightCacheKey(
            workspace_id=normalized_workspace,
            workspace_config_generation=before[0],
            system_config_generation=before[1],
            ontology_id=current_ontology_id,
            ontology_version=current_ontology_version,
            mutation_revision=before[2],
        )
        current_manifest_payload = freeze_preflight_product(
            _manifest_payload(current_manifest)
        )

        cached_product = selected_cache.get_or_load(
            key,
            partial(
                _load_package_cache_product,
                package_loader=load_package,
                ontology_manifest=current_manifest,
                workspace_id=normalized_workspace,
                manifest_payload=current_manifest_payload,
            ),
        )
        if not isinstance(cached_product, Mapping):
            raise TypeError("preflight cache returned an invalid product")
        cached_manifest_payload = cached_product.get(_CACHED_MANIFEST_KEY)
        if cached_manifest_payload != current_manifest_payload:
            raise RuntimeError(
                "ontology manifest changed without generation advancement"
            )
        product = cached_product.get(_CACHED_PACKAGE_KEY)
        if not isinstance(product, Mapping):
            raise TypeError("preflight cache package product must be a mapping")
        resolved_deterministic_candidate = (
            deterministic_candidate_loader(current_manifest)
            if deterministic_candidate_loader is not None
            else bool(deterministic_candidate)
        )
        model_profile = (
            model_profile_loader()
            if model_profile_loader is not None
            and not resolved_deterministic_candidate
            else None
        )
        after = _generations(
            resolve_generations,
            normalized_workspace,
            expected_ontology_id,
        )
        if before == after:
            break
    else:
        raise RuntimeError("preflight configuration changed repeatedly")

    manifest_payload = cached_manifest_payload
    preload_records = product.get("preload_records") or ()
    if not isinstance(preload_records, (list, tuple)):
        raise TypeError("preload_records must be a sequence")

    return RunPreflightSnapshot(
        workspace_id=normalized_workspace,
        workspace_config_generation=before[0],
        system_config_generation=before[1],
        ontology_id=current_ontology_id,
        ontology_version=current_ontology_version,
        mutation_revision=before[2],
        ontology_manifest=manifest_payload,
        root_payload=product.get("root_payload"),
        ontology_source_signature=product.get("ontology_source_signature"),
        ontology_content_revision=product.get("ontology_content_revision"),
        script_bindings=product.get("script_bindings") or {},
        execute_script_tool_schema=product.get("execute_script_tool_schema"),
        solver_capabilities=tuple(product.get("solver_capabilities") or ()),
        selected_ontology_context=(
            str(product.get("selected_ontology_context"))
            if product.get("selected_ontology_context") is not None
            else None
        ),
        preload_records=tuple(preload_records),
        model_profile=model_profile,
        deterministic_candidate=resolved_deterministic_candidate,
    )
