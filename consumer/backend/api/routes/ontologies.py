"""Ontology management endpoints: list, import, delete and file operations."""

import hashlib
import ctypes
import errno
import io
import os
import re
import shutil
import stat
import tempfile
import unicodedata
import uuid
import zipfile
from pathlib import Path
from typing import Callable, Mapping, Optional

import yaml
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from starlette.datastructures import FormData, UploadFile
from config import LimitsConfig
from integration.platform.assets import export_ontology_snapshot_safely
from utils.atomic_files import staged_bytes
from api.dependencies import (
    require_workspace_id,
    require_workspace_owner_or_admin_id,
)
from api.request_parsing import read_optional_json_object
from api.auth.auth import get_current_user
from utils.logging import get_logger
from ontology.ontologies import (
    OntologyLoader,
    ensure_ontology_versions,
    get_ontology_registry,
    get_ontology_version_dir,
    invalidate_ontology_registry,
    is_valid_ontology_version,
    load_ontology_manifest_data,
    validate_ontology_manifest_schema,
    validate_ontology_version_revision,
    save_ontology_manifest_data,
)
from ontology.ontology_catalog import (
    find_asset_file,
    read_required_asset_text,
    supported_asset_formats_text,
)
from ontology.capabilities import (
    OntologyCapabilityRevisionConflictError,
    OntologyCapabilityStoreError,
    delete_ontology_capabilities,
    get_business_checkpoint_capabilities,
    get_business_checkpoint_capability,
    inspect_business_checkpoint_template,
    reset_business_checkpoint_capability,
    set_business_checkpoint_capability,
)
from ontology.package_browser import (
    EXCLUDED_DIRECTORY_NAMES,
    PackageBrowserError,
    build_package_tree,
    list_package_files,
    read_package_text,
)
from ontology.package_import import (
    PackageNormalizationError,
    SUPPORTED_IMPORT_MANIFEST_KINDS,
    build_versioned_install_manifest,
    has_importable_package_layout,
    inspect_importable_package,
    normalize_imported_package,
    validate_ontology_revision,
    validate_imported_field_aliases,
    validate_imported_object_aliases,
)
from ontology.operation_execution import (
    DeterministicPackageError,
    has_execution_declaration,
    is_deterministic_package_locked,
    validate_and_lock_deterministic_operations,
)
from ontology.mutation_lock import ontology_mutation_lock
from ontology.directory_replacement import portable_replace_directory
from agent.tools.business_data.ontology_sources_store import (
    build_ontology_business_data_summary,
    clear_ontology_data_source_on_delete,
)
from persistence.chat_store import ChatStore
from ontology.ontology_graph import build_ontology_graph
from ontology.change_events import build_digest_hints, empty_digest_hints
from ontology.evidence.identity import build_semantic_manifest_digest
from ontology.evidence.source_reader import PackageSourceReader
from ontology.mutations import (
    OntologyMutationError,
    OntologyMutationConflictError,
    OntologyMutationCoordinator,
    PreparedOntologyMutation,
    read_mutation_state_snapshot,
)
from workspace.workspaces import (
    get_workspace_paths,
    get_workspace_ontologies_root,
    workspace_scope,
)
logger = get_logger("routes.ontologies")
_ontology_install_lock = ontology_mutation_lock

ontologies_router = APIRouter(prefix="/agentic")

ONTOLOGY_FOLDER_LABELS = {
    "operations": "逻辑",
    "objects": "实体",
    "assets": "公共文件",
}
ONTOLOGY_MAX_ZIP_BYTES = 50 * 1024 * 1024
ONTOLOGY_MAX_ZIP_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
ONTOLOGY_MAX_ZIP_MEMBERS = 10_000
ONTOLOGY_MAX_ZIP_PATH_DEPTH = 32
ONTOLOGY_MAX_ZIP_PATH_LENGTH = 4096
ONTOLOGY_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
ONTOLOGY_IMPORT_PREVIEW_MAX_FILES = 80
ONTOLOGY_IMPORT_PREVIEW_MAX_BYTES = 24 * 1024
ONTOLOGY_IMPORT_PREVIEW_TEXT_EXTENSIONS = {
    ".md",
    ".yaml",
    ".yml",
    ".json",
    ".py",
    ".txt",
    ".conf",
    ".cfg",
    ".ini",
    ".env",
    ".properties",
    ".toml",
    ".csv",
    ".tsv",
    ".sql",
    ".xml",
}


def _read_limited_zip_upload(
    uploaded_file: UploadFile,
) -> tuple[Optional[bytes], Optional[str]]:
    try:
        uploaded_bytes = uploaded_file.file.read(ONTOLOGY_MAX_ZIP_BYTES + 1)
    except Exception:
        return None, "Failed to read uploaded file"
    if len(uploaded_bytes) > ONTOLOGY_MAX_ZIP_BYTES:
        return None, "Zip file too large"
    return uploaded_bytes, None


ONTOLOGY_VERSIONED_SKIP_NAMES = {"manifest.yaml", "versions"}
class OntologyImportError(ValueError):
    """A stable client-visible import validation error."""


def _mutation_coordinator(workspace_id: str) -> OntologyMutationCoordinator:
    return OntologyMutationCoordinator(get_workspace_paths(workspace_id).runtime_dir)


def _mutation_audit_fields(request: Optional[Request] = None) -> dict:
    user = get_current_user()
    request_id = None
    if request is not None:
        request_id = str(request.headers.get("X-Request-Id") or "").strip() or None
    return {
        "operator": {"type": "user", "id": user.username} if user is not None else None,
        "request_id": request_id[:128] if request_id else None,
    }


def _manifest_state(ontology_dir: Path) -> tuple[dict, str, bool]:
    manifest = load_ontology_manifest_data(ontology_dir)
    active_version = str(manifest.get("active_version") or manifest.get("version") or "").strip()
    raw_enabled = manifest.get("enabled", True)
    if isinstance(raw_enabled, bool):
        enabled = raw_enabled
    else:
        enabled = str(raw_enabled).strip().lower() not in {"false", "0", "no", "off", "disabled", "禁用"}
    return manifest, active_version, enabled


def _mutation_base_revision(workspace_id: str, ontology_id: str) -> int:
    state = read_mutation_state_snapshot(
        get_workspace_paths(workspace_id).runtime_dir,
        workspace_id=workspace_id,
        ontology_id=ontology_id,
    )
    return int((state or {}).get("mutation_revision") or 0)


def _file_content_precondition(
    target: Path,
    expected_content: bytes | None,
) -> Callable[[], None]:
    """Validate the prepared file snapshot while the ontology mutation lock is held."""

    def validate() -> None:
        if expected_content is None:
            unchanged = not target.exists()
        else:
            try:
                unchanged = target.is_file() and target.read_bytes() == expected_content
            except OSError:
                unchanged = False
        if not unchanged:
            raise OntologyMutationConflictError(
                "MUTATION_CONTENT_CONFLICT",
                "ontology file changed after the mutation was prepared",
            )

    return validate


def _prepare_atomic_bytes_replace(
    target: Path,
    content: bytes,
) -> tuple[Callable[[], None], Callable[[], None], Callable[[], None]]:
    """Prepare a same-filesystem replace with a reversible old-file backup."""
    target.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    staged = target.with_name(f".{target.name}.{token}.mutation")
    backup = target.with_name(f".{target.name}.{token}.backup")
    staged.write_bytes(content)
    existed = target.is_file()
    backup_created = False
    replacement_installed = False

    def commit() -> None:
        nonlocal backup_created, replacement_installed
        if existed:
            os.replace(target, backup)
            backup_created = True
        os.replace(staged, target)
        replacement_installed = True

    def rollback() -> None:
        if replacement_installed and target.exists():
            target.unlink()
        if backup_created and backup.exists():
            os.replace(backup, target)
        staged.unlink(missing_ok=True)

    def finalize() -> None:
        backup.unlink(missing_ok=True)
        staged.unlink(missing_ok=True)

    return commit, rollback, finalize


def _prepare_atomic_directory_delete(
    target: Path,
) -> tuple[Callable[[], None], Callable[[], None], Callable[[], None]]:
    tombstone = target.with_name(f".{target.name}.{uuid.uuid4().hex}.deleted")

    def commit() -> None:
        os.replace(target, tombstone)

    def rollback() -> None:
        if tombstone.exists() and not target.exists():
            os.replace(tombstone, target)

    def finalize() -> None:
        if tombstone.is_dir():
            shutil.rmtree(tombstone)
        else:
            tombstone.unlink(missing_ok=True)

    return commit, rollback, finalize


def _prepare_atomic_directory_replace(
    staged: Path,
    target: Path,
    backup: Path,
) -> tuple[Callable[[], None], Callable[[], None], Callable[[], None]]:
    """Prepare a reversible same-filesystem directory replacement."""
    backup_created = False
    replacement_installed = False

    def commit() -> None:
        nonlocal backup_created, replacement_installed
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            os.replace(target, backup)
            backup_created = True
        os.replace(staged, target)
        replacement_installed = True

    def rollback() -> None:
        if replacement_installed and target.exists():
            shutil.rmtree(target)
        if backup_created and backup.exists():
            os.replace(backup, target)

    def finalize() -> None:
        if backup.is_dir():
            shutil.rmtree(backup)
        else:
            backup.unlink(missing_ok=True)

    return commit, rollback, finalize


def _combine_prepared_steps(
    *steps: tuple[Callable[[], None], Callable[[], None], Callable[[], None]],
) -> tuple[Callable[[], None], Callable[[], None], Callable[[], None]]:
    attempted_count = 0

    def commit() -> None:
        nonlocal attempted_count
        for index, (step_commit, _step_rollback, _step_finalize) in enumerate(steps):
            # A step can mutate the filesystem and then fail (for example, the
            # first os.replace succeeds while the second one fails). Include
            # that in-progress step in rollback before invoking it.
            attempted_count = index + 1
            step_commit()

    def rollback() -> None:
        rollback_errors: list[BaseException] = []
        for _step_commit, step_rollback, _step_finalize in reversed(steps[:attempted_count]):
            try:
                step_rollback()
            except BaseException as exc:
                rollback_errors.append(exc)
        if rollback_errors:
            raise rollback_errors[0]

    def finalize() -> None:
        for _step_commit, _step_rollback, step_finalize in steps:
            step_finalize()

    return commit, rollback, finalize


def _cleanup_mutation_staging_root(staging_root: Path, error: BaseException) -> None:
    """Remove ordinary staging debris while preserving uncertain-state recovery data."""
    if (
        isinstance(error, OntologyMutationError)
        and error.code == "MUTATION_COMMIT_STATE_UNCERTAIN"
    ):
        logger.critical(
            "Preserving ontology mutation recovery data after uncertain commit: path=%s",
            staging_root,
        )
        return
    if staging_root.exists():
        shutil.rmtree(staging_root, ignore_errors=True)


def _is_safe_path(base_dir: Path, target_path: Path) -> bool:
    base_dir = base_dir.resolve(strict=False)
    target_path = target_path.resolve(strict=False)
    return base_dir == target_path or base_dir in target_path.parents


def _get_ontologies_dir() -> Path:
    return get_workspace_ontologies_root()


def _resolve_ontology_dir(ontology_id: str) -> Optional[Path]:
    safe_name = Path(ontology_id or "").name
    if not safe_name or safe_name in (".", ".."):
        return None

    ontologies_dir = _get_ontologies_dir()
    ontology_dir = ontologies_dir / safe_name
    if (
        ontology_dir.is_symlink()
        or not _is_safe_path(ontologies_dir, ontology_dir)
        or not ontology_dir.is_dir()
    ):
        return None
    return ontology_dir


def _resolve_ontology_folder(
    ontology_id: str,
    folder_name: str,
    version: Optional[str] = None,
    *,
    reject_unsafe_version: bool = False,
) -> tuple[Optional[Path], Optional[str]]:
    ontology_dir = _resolve_ontology_dir(ontology_id)
    if ontology_dir is None:
        return None, None

    normalized = (folder_name or "").strip().lower()
    if normalized not in ONTOLOGY_FOLDER_LABELS:
        return None, None

    version_dir = (
        _resolve_package_browser_version_dir(ontology_dir, version)
        if reject_unsafe_version
        else _resolve_existing_version_dir(ontology_dir, version)
    )
    if version_dir is None:
        return None, None

    return version_dir / normalized, normalized


def _resolve_existing_version_dir_checked(
    ontology_dir: Path,
    version: Optional[str] = None,
) -> Optional[Path]:
    manifest = load_ontology_manifest_data(ontology_dir)
    active_version = str(manifest.get("active_version") or manifest.get("version") or "").strip()
    target_version = str(version or active_version).strip()
    if not target_version:
        return ontology_dir

    versions_dir = ontology_dir / "versions"
    if not versions_dir.is_dir():
        legacy_version = str(manifest.get("version") or active_version).strip()
        if target_version != legacy_version:
            return None
        try:
            with _ontology_install_lock(ontology_dir):
                if not versions_dir.is_dir():
                    ensure_ontology_versions(ontology_dir)
        except Exception:
            logger.exception("Failed to migrate legacy ontology directory: %s", ontology_dir)
            return None

    return get_ontology_version_dir(ontology_dir, target_version, create=False)


def _resolve_existing_version_dir(ontology_dir: Path, version: Optional[str] = None) -> Optional[Path]:
    try:
        return _resolve_existing_version_dir_checked(ontology_dir, version)
    except Exception:
        return None


def _resolve_package_browser_version_dir(
    ontology_dir: Path,
    version: Optional[str] = None,
) -> Optional[Path]:
    try:
        return _resolve_existing_version_dir_checked(ontology_dir, version)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise PackageBrowserError("invalid_path", "Invalid file path") from exc


def _get_requested_version(
    request: Request,
    default: str = "",
    *,
    form: FormData | None = None,
) -> str:
    form_version = form.get("version") if form is not None else None
    return str(
        request.query_params.get("version")
        or form_version
        or default
        or ""
    ).strip()


async def _owner_json_payload(
    request: Request,
    _workspace_id: str = Depends(require_workspace_owner_or_admin_id),
) -> dict:
    """Read JSON only after authentication and workspace authorization."""
    return await read_optional_json_object(request)


async def _owner_multipart_form(
    request: Request,
    _workspace_id: str = Depends(require_workspace_owner_or_admin_id),
) -> FormData:
    """Parse multipart data only after authentication and workspace authorization."""
    return await request.form()


def _get_json_requested_version(data: dict, default: str = "") -> str:
    return str((data or {}).get("version") or default or "").strip()


def _version_error_payload(version: str) -> tuple[dict, int]:
    if version and not is_valid_ontology_version(version):
        return {"error": "version不合法，仅支持[A-Za-z0-9][A-Za-z0-9._-]*"}, 400
    return {"error": "Ontology version not found"}, 404


def _package_browser_error_response(exc: PackageBrowserError):
    messages = {
        "invalid_path": "Invalid file path",
        "not_found": "File not found",
        "not_viewable": str(exc),
        "too_large": "File too large to view online",
        "invalid_encoding": "File encoding must be UTF-8",
        "read_failed": "Failed to read file",
    }
    status_code = 404 if exc.code == "not_found" else 500 if exc.code == "read_failed" else 400
    return JSONResponse(content={"error": messages.get(exc.code, str(exc))}, status_code=status_code)


def _manifest_for_download(ontology_dir: Path, version: str) -> str:
    if not is_valid_ontology_version(version):
        raise ValueError("Invalid ontology version")
    manifest = load_ontology_manifest_data(ontology_dir)
    global_enabled = manifest.get("enabled", True)
    version_dir = validate_ontology_version_revision(ontology_dir, version)
    version_manifest_path = version_dir / "manifest.yaml"
    if not version_manifest_path.is_file() or version_manifest_path.is_symlink():
        raise ValueError("Ontology revision manifest is missing or unsafe")
    try:
        version_manifest = yaml.safe_load(version_manifest_path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ValueError("Ontology revision manifest cannot be read") from exc
    if not isinstance(version_manifest, dict):
        raise ValueError("Ontology revision manifest must be a mapping")
    manifest.pop("capabilities", None)
    manifest.update(version_manifest)
    manifest["enabled"] = global_enabled
    validate_ontology_manifest_schema(manifest)
    manifest["version"] = version
    manifest["active_version"] = version
    manifest["versions"] = [version]
    return yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False)


def _build_ontology_zip_buffer(ontology_dir: Path, version_dir: Path, *, version: str) -> io.BytesIO:
    if not version_dir.is_dir() or version_dir.is_symlink():
        raise ValueError("Ontology revision directory is missing or unsafe")
    zip_buffer = io.BytesIO()
    archive_root = ontology_dir.name

    with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("{}/manifest.yaml".format(archive_root), _manifest_for_download(ontology_dir, version))
        file_paths = [
            file_path
            for file_path in version_dir.rglob("*")
            if file_path.is_file() and not file_path.is_symlink()
        ]
        file_paths.sort(key=lambda item: item.relative_to(version_dir).as_posix())

        for file_path in file_paths:
            if not _is_safe_path(version_dir, file_path):
                logger.warning("Skipping unsafe ontology download path: %s", file_path)
                continue
            relative_path = file_path.relative_to(version_dir).as_posix()
            if relative_path == "manifest.yaml":
                continue
            zf.write(file_path, "{}/{}".format(archive_root, relative_path))

    zip_buffer.seek(0)
    return zip_buffer


def _sanitize_md_filename(filename: str) -> Optional[str]:
    safe_name = Path(filename or "").name
    if safe_name in ("", ".", ".."):
        return None
    if Path(safe_name).suffix.lower() != ".md":
        return None
    return safe_name


def _normalize_asset_key(value: str) -> Optional[str]:
    normalized = str(value or "").strip().replace("\\", "/").strip("/")
    if not normalized:
        return None
    requested_path = Path(normalized)
    if requested_path.is_absolute() or ".." in requested_path.parts:
        return None
    return normalized


def _operation_max_chars() -> int:
    return LimitsConfig.from_env().operation_max_chars


def _count_workspace_ontology_dirs(ontologies_dir: Path) -> int:
    if not ontologies_dir.is_dir():
        return 0
    return sum(
        1
        for child in ontologies_dir.iterdir()
        if child.is_dir()
        and not child.name.startswith(".")
        and ((child / "manifest.yaml").exists() or (child / "versions").is_dir())
    )


def _validate_workspace_ontology_count_limit(
    ontologies_dir: Path,
    *,
    ontology_id: str,
    existed: bool,
) -> Optional[str]:
    if existed:
        return None
    max_count = LimitsConfig.from_env().ontology_max_count_per_workspace
    current_count = _count_workspace_ontology_dirs(ontologies_dir)
    if current_count >= max_count:
        return "本体数量已达上限: {}".format(max_count)
    return None


def _validate_md_limits(content: str, folder_key: str) -> Optional[str]:
    if folder_key == "operations":
        max_chars = _operation_max_chars()
        char_count = len(content)
        if char_count > max_chars:
            return "单个逻辑长度超过上限: {} / {} 字符".format(char_count, max_chars)
    if folder_key == "objects":
        max_count = LimitsConfig.from_env().entity_attribute_max_count
        attribute_count = _count_first_markdown_table_rows(content)
        if attribute_count > max_count:
            return "单实体属性数量超过上限: {} / {}".format(attribute_count, max_count)
    return None


def _validate_new_markdown_file_count(
    folder_path: Path,
    folder_key: str,
    target_path: Path,
) -> Optional[str]:
    if target_path.exists():
        return None
    limits = LimitsConfig.from_env()
    current_count = _count_markdown_files(folder_path)
    if folder_key == "objects" and current_count >= limits.ontology_object_max_count:
        return "单本体实体数量已达上限: {}".format(limits.ontology_object_max_count)
    if folder_key == "operations" and current_count >= limits.ontology_operation_max_count:
        return "单本体流程数量已达上限: {}".format(limits.ontology_operation_max_count)
    return None


def _count_first_markdown_table_rows(content: str) -> int:
    table_started = False
    rows = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if line.startswith("|") and line.endswith("|"):
            table_started = True
            rows.append(line)
            continue
        if table_started and rows:
            break
    if len(rows) < 2:
        return 0
    data_rows = rows[2:]
    return len([row for row in data_rows if row.strip("|").strip()])


def _validate_object_attribute_limits(objects_dir: Path) -> Optional[str]:
    if not objects_dir.exists():
        return None
    max_count = LimitsConfig.from_env().entity_attribute_max_count
    for path in sorted(objects_dir.glob("*.md")):
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return "{} 编码必须是 UTF-8".format(path.name)
        attribute_count = _count_first_markdown_table_rows(content)
        if attribute_count > max_count:
            return "{}: 单实体属性数量超过上限: {} / {}".format(
                path.name,
                attribute_count,
                max_count,
            )
    return None


def _validate_ontology_tree_limits(root_dir: Path) -> Optional[str]:
    limits = LimitsConfig.from_env()
    objects_count = _count_markdown_files(root_dir / "objects")
    if objects_count > limits.ontology_object_max_count:
        return "单本体实体数量已达上限: {} / {}".format(
            objects_count,
            limits.ontology_object_max_count,
        )
    operations_count = _count_markdown_files(root_dir / "operations")
    if operations_count > limits.ontology_operation_max_count:
        return "单本体流程数量已达上限: {} / {}".format(
            operations_count,
            limits.ontology_operation_max_count,
        )
    attribute_error = _validate_object_attribute_limits(root_dir / "objects")
    if attribute_error:
        return attribute_error
    operations_dir = root_dir / "operations"
    if not operations_dir.exists():
        return None
    for path in sorted(operations_dir.glob("*.md")):
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return "{} 编码必须是 UTF-8".format(path.name)
        validation_error = _validate_md_limits(
            content,
            "operations",
        )
        if validation_error:
            return "{}: {}".format(path.name, validation_error)
    return None


def _validate_operations_tree_limits(root_dir: Path) -> Optional[str]:
    return _validate_ontology_tree_limits(root_dir)


def _find_sessions_locking_ontology_version(
    *,
    workspace_id: str,
    ontology_id: str,
    version: str,
    limit: int = 20,
) -> tuple[int, list[dict[str, str]]]:
    paths = get_workspace_paths(workspace_id)
    users_runtime_dir = paths.users_runtime_dir
    if not users_runtime_dir.is_dir():
        return 0, []

    matched: list[dict[str, str]] = []
    total = 0
    for user_dir in sorted(
        (item for item in users_runtime_dir.iterdir() if item.is_dir()),
        key=lambda item: item.name,
    ):
        chats_dir = user_dir / "chats"
        if not chats_dir.is_dir():
            continue
        store = ChatStore(str(chats_dir))
        for session in store.list_sessions():
            metadata = dict(getattr(session, "metadata", {}) or {})
            if str(metadata.get("selected_ontology_id") or "").strip() != ontology_id:
                continue
            locked_version = str(
                metadata.get("selected_ontology_version")
                or metadata.get("selected_version")
                or ""
            ).strip()
            if locked_version != version:
                continue
            total += 1
            if len(matched) < limit:
                matched.append({
                    "session_id": str(getattr(session, "id", "") or ""),
                    "title": str(getattr(session, "title", "") or ""),
                    "user_key": user_dir.name,
                })
    return total, matched


@ontologies_router.get("/ontologies")
def list_ontologies(
    workspace_id: str = Depends(require_workspace_id),
):
    with workspace_scope(workspace_id):
        registry = get_ontology_registry(workspace_id=workspace_id)
        registry.refresh()

        try:
            checkpoint_capabilities = get_business_checkpoint_capabilities(
                registry.ontologies.keys(),
                workspace_id=workspace_id,
            )
        except OntologyCapabilityStoreError:
            logger.warning(
                "Failed to read business CheckPoint capabilities: workspace_id=%s",
                workspace_id,
                exc_info=True,
            )
            checkpoint_capabilities = {}

        ontologies = []
        for ontology_id, ontology in registry.ontologies.items():
            checkpoint = checkpoint_capabilities.get(
                ontology_id,
                {"enabled": False, "revision": "", "updated_at": ""},
            )
            template = inspect_business_checkpoint_template(
                ontology.id,
                ontology_version=ontology.active_version,
                workspace_id=workspace_id,
            )
            ontologies.append(
                {
                    "id": ontology.id,
                    "name": ontology.name,
                    "description": ontology.description,
                    "version": ontology.version,
                    "active_version": ontology.active_version,
                    "versions": ontology.versions,
                    "language": ontology.language,
                    "enabled": ontology.enabled,
                    "business_checkpoint": {
                        **checkpoint,
                        "template_available": template["available"],
                        "template_error": template["error"],
                    },
                    "manifest_path": ontology.manifest_path,
                    "index_path": ontology.index_path,
                    "business_data": build_ontology_business_data_summary(
                        ontology.id,
                        workspace_id=workspace_id,
                    ),
                }
            )

        ontologies.sort(key=lambda item: item["id"])
        return JSONResponse(content={"ontologies": ontologies})


@ontologies_router.get("/ontologies/{ontology_id}/capabilities/business-checkpoint")
def get_ontology_business_checkpoint(
    ontology_id: str,
    workspace_id: str = Depends(require_workspace_id),
):
    with workspace_scope(workspace_id):
        ontology_dir = _resolve_ontology_dir(ontology_id)
        if ontology_dir is None:
            return JSONResponse(content={"error": "Ontology not found"}, status_code=404)
        try:
            capability = get_business_checkpoint_capability(
                ontology_id,
                workspace_id=workspace_id,
            )
            template = inspect_business_checkpoint_template(
                ontology_id,
                workspace_id=workspace_id,
            )
        except OntologyCapabilityStoreError as exc:
            return JSONResponse(content={"error": str(exc)}, status_code=500)
        return JSONResponse(content={
            "ontology_id": ontology_id,
            **capability,
            "template_available": template["available"],
            "template_error": template["error"],
        })


@ontologies_router.put("/ontologies/{ontology_id}/capabilities/business-checkpoint")
def update_ontology_business_checkpoint(
    ontology_id: str,
    data: dict = Depends(_owner_json_payload),
    workspace_id: str = Depends(require_workspace_owner_or_admin_id),
):
    if not isinstance(data.get("enabled"), bool):
        return JSONResponse(
            content={"error": "`enabled` must be a boolean"},
            status_code=400,
        )
    expected_revision = str(data.get("expected_revision") or "").strip()

    with workspace_scope(workspace_id):
        ontology_dir = _resolve_ontology_dir(ontology_id)
        if ontology_dir is None:
            return JSONResponse(content={"error": "Ontology not found"}, status_code=404)
        if data["enabled"]:
            template = inspect_business_checkpoint_template(
                ontology_id,
                workspace_id=workspace_id,
            )
            if not template["available"]:
                return JSONResponse(
                    content={
                        "error": "Knowledge package is missing a readable assets/business-checkpoint-template.md",
                        "detail": template["error"],
                    },
                    status_code=409,
                )
        try:
            capability = set_business_checkpoint_capability(
                ontology_id,
                enabled=data["enabled"],
                expected_revision=expected_revision,
                workspace_id=workspace_id,
            )
        except OntologyCapabilityRevisionConflictError as exc:
            return JSONResponse(content={"error": str(exc)}, status_code=409)
        except OntologyCapabilityStoreError as exc:
            return JSONResponse(content={"error": str(exc)}, status_code=400)
        return JSONResponse(content={
            "success": True,
            "ontology_id": ontology_id,
            **capability,
        })


@ontologies_router.put("/ontologies/{ontology_id}/status")
def update_ontology_status(
    ontology_id: str,
    request: Request,
    data: dict = Depends(_owner_json_payload),
    workspace_id: str = Depends(require_workspace_owner_or_admin_id),
):
    if "enabled" not in data:
        return JSONResponse(content={"error": "`enabled` is required"}, status_code=400)
    if not isinstance(data.get("enabled"), bool):
        return JSONResponse(content={"error": "`enabled` must be a boolean"}, status_code=400)

    with workspace_scope(workspace_id):
        ontology_dir = _resolve_ontology_dir(ontology_id)
        if ontology_dir is None:
            return JSONResponse(content={"error": "Ontology not found"}, status_code=404)

        try:
            manifest, active_version, before_enabled = _manifest_state(ontology_dir)
            if before_enabled != data["enabled"]:
                updated_manifest = dict(manifest)
                updated_manifest["enabled"] = data["enabled"]
                commit, rollback, finalize = _prepare_atomic_bytes_replace(
                    ontology_dir / "manifest.yaml",
                    yaml.safe_dump(updated_manifest, allow_unicode=True, sort_keys=False).encode("utf-8"),
                )
                _mutation_coordinator(workspace_id).commit(
                    PreparedOntologyMutation(
                        workspace_id=workspace_id,
                        ontology_id=ontology_id,
                        mutation_type="ONTOLOGY_VISIBILITY_CHANGED",
                        before_version=active_version or None,
                        after_version=active_version or None,
                        before_enabled=before_enabled,
                        after_enabled=data["enabled"],
                        commit=commit,
                        rollback=rollback,
                        finalize=finalize,
                        digest_hints=empty_digest_hints(scope_version=active_version or None),
                        trigger="api",
                        **_mutation_audit_fields(request),
                    )
                )
            ontology = OntologyLoader.load_ontology(ontology_dir)
            if ontology is None:
                raise ValueError("Failed to reload ontology manifest")
            registry = get_ontology_registry(workspace_id=workspace_id)
            registry.refresh()
        except OntologyMutationConflictError as exc:
            return JSONResponse(
                content={"error": str(exc), "code": exc.code},
                status_code=409,
            )
        except Exception:
            logger.exception(
                "Failed to update ontology status: workspace_id=%s ontology_id=%s",
                workspace_id,
                ontology_id,
            )
            return JSONResponse(content={"error": "Failed to update ontology status"}, status_code=500)

        export_ontology_snapshot_safely(workspace_id=workspace_id)
        return JSONResponse(content={
            "success": True,
            "ontology_id": ontology.id,
            "enabled": ontology.enabled,
        })


@ontologies_router.put("/ontologies/{ontology_id}/active-version")
def update_ontology_active_version(
    ontology_id: str,
    request: Request,
    data: dict = Depends(_owner_json_payload),
    workspace_id: str = Depends(require_workspace_owner_or_admin_id),
):
    version = str(data.get("version") or "").strip()
    if not is_valid_ontology_version(version):
        return JSONResponse(content={"error": "version不合法，仅支持[A-Za-z0-9][A-Za-z0-9._-]*"}, status_code=400)

    with workspace_scope(workspace_id):
        ontology_dir = _resolve_ontology_dir(ontology_id)
        if ontology_dir is None:
            return JSONResponse(content={"error": "Ontology not found"}, status_code=404)

        mutation_revision = None
        try:
            with _ontology_install_lock(ontology_dir):
                manifest = ensure_ontology_versions(ontology_dir)
                before_version = str(manifest.get("active_version") or manifest.get("version") or "").strip()
                versions = [str(item).strip() for item in (manifest.get("versions") or []) if str(item).strip()]
                if version not in versions or get_ontology_version_dir(ontology_dir, version, create=False) is None:
                    raise ValueError("Ontology version not found")
                _manifest, _active, enabled = _manifest_state(ontology_dir)
                if before_version != version:
                    updated_manifest = dict(manifest)
                    updated_manifest["active_version"] = version
                    updated_manifest["version"] = version
                    updated_manifest["versions"] = versions
                    commit, rollback, finalize = _prepare_atomic_bytes_replace(
                        ontology_dir / "manifest.yaml",
                        yaml.safe_dump(updated_manifest, allow_unicode=True, sort_keys=False).encode("utf-8"),
                    )
                    mutation_result = _mutation_coordinator(workspace_id).commit(
                        PreparedOntologyMutation(
                            workspace_id=workspace_id,
                            ontology_id=ontology_id,
                            mutation_type="ACTIVE_VERSION_CHANGED",
                            before_version=before_version or None,
                            after_version=version,
                            before_enabled=enabled,
                            after_enabled=enabled,
                            commit=commit,
                            rollback=rollback,
                            finalize=finalize,
                            digest_hints=empty_digest_hints(scope_version=version),
                            trigger="api",
                            **_mutation_audit_fields(request),
                        )
                    )
                    mutation_revision = mutation_result.mutation_revision
                ontology = OntologyLoader.load_ontology(ontology_dir)
                if ontology is None:
                    raise ValueError("Failed to reload ontology manifest")
            registry = get_ontology_registry(workspace_id=workspace_id)
            registry.refresh()
        except ValueError as exc:
            return JSONResponse(content={"error": str(exc)}, status_code=400)
        except OntologyMutationConflictError as exc:
            return JSONResponse(content={"error": str(exc), "code": exc.code}, status_code=409)
        except Exception:
            logger.exception(
                "Failed to update ontology active version: workspace_id=%s ontology_id=%s version=%s",
                workspace_id,
                ontology_id,
                version,
            )
            return JSONResponse(content={"error": "Failed to update ontology active version"}, status_code=500)

        export_ontology_snapshot_safely(workspace_id=workspace_id)
        response = {
            "success": True,
            "ontology_id": ontology.id,
            "active_version": ontology.active_version,
            "version": ontology.version,
            "versions": ontology.versions,
        }
        if mutation_revision is not None:
            response["mutation_revision"] = mutation_revision
        return JSONResponse(content=response)


@ontologies_router.delete("/ontologies/{ontology_id}")
def delete_ontology(
    ontology_id: str,
    request: Request,
    workspace_id: str = Depends(require_workspace_owner_or_admin_id),
):
    with workspace_scope(workspace_id):
        ontology_dir = _resolve_ontology_dir(ontology_id)
        if ontology_dir is None:
            return JSONResponse(content={"error": "Ontology not found"}, status_code=404)

        try:
            with _ontology_install_lock(ontology_dir):
                if not ontology_dir.is_dir():
                    return JSONResponse(
                        content={"error": "Ontology not found"},
                        status_code=404,
                    )
                _manifest, before_version, before_enabled = _manifest_state(ontology_dir)
                commit, rollback, finalize = _prepare_atomic_directory_delete(ontology_dir)
                mutation_result = _mutation_coordinator(workspace_id).commit(
                    PreparedOntologyMutation(
                        workspace_id=workspace_id,
                        ontology_id=ontology_id,
                        mutation_type="ONTOLOGY_DELETED",
                        before_version=before_version or None,
                        after_version=None,
                        before_enabled=before_enabled,
                        after_enabled=False,
                        commit=commit,
                        rollback=rollback,
                        finalize=finalize,
                        digest_hints=empty_digest_hints(scope_version=None),
                        trigger="api",
                        **_mutation_audit_fields(request),
                    )
                )
        except OntologyMutationConflictError as exc:
            return JSONResponse(content={"error": str(exc), "code": exc.code}, status_code=409)
        except Exception:
            logger.exception(
                "Failed to delete ontology: workspace_id=%s ontology_id=%s path=%s",
                workspace_id,
                ontology_id,
                ontology_dir,
            )
            return JSONResponse(content={"error": "Failed to delete ontology"}, status_code=500)

        clear_ontology_data_source_on_delete(ontology_id, workspace_id=workspace_id)
        try:
            delete_ontology_capabilities(ontology_id, workspace_id=workspace_id)
        except OntologyCapabilityStoreError:
            logger.warning(
                "Failed to delete ontology capabilities: workspace_id=%s ontology_id=%s",
                workspace_id,
                ontology_id,
                exc_info=True,
            )

        registry = get_ontology_registry(workspace_id=workspace_id)
        registry.refresh()

        logger.info(
            "Ontology deleted: workspace_id=%s ontology_id=%s path=%s",
            workspace_id,
            ontology_id,
            ontology_dir,
        )

        export_ontology_snapshot_safely(workspace_id=workspace_id)
        return JSONResponse(content={
            "success": True,
            "ontology_id": ontology_id,
            "mutation_revision": mutation_result.mutation_revision,
        })


@ontologies_router.delete("/ontologies/{ontology_id}/versions/{version}")
def delete_ontology_version(
    ontology_id: str,
    version: str,
    request: Request,
    workspace_id: str = Depends(require_workspace_owner_or_admin_id),
):
    if not is_valid_ontology_version(version):
        payload, status_code = _version_error_payload(version)
        return JSONResponse(content=payload, status_code=status_code)

    with workspace_scope(workspace_id):
        ontology_dir = _resolve_ontology_dir(ontology_id)
        if ontology_dir is None:
            return JSONResponse(content={"error": "Ontology not found"}, status_code=404)

        try:
            manifest = ensure_ontology_versions(ontology_dir)
        except Exception:
            logger.exception(
                "Failed to prepare ontology versions before delete: workspace_id=%s ontology_id=%s path=%s",
                workspace_id,
                ontology_id,
                ontology_dir,
            )
            return JSONResponse(content={"error": "Failed to prepare ontology versions"}, status_code=500)

        active_version = str(
            manifest.get("active_version")
            or manifest.get("version")
            or ""
        ).strip()
        versions = [
            str(item).strip()
            for item in (manifest.get("versions") or [])
            if str(item or "").strip()
        ]

        if version not in versions:
            return JSONResponse(content={
                "error": "Ontology version not found",
                "ontology_id": ontology_id,
                "version": version,
                "active_version": active_version,
                "versions": versions,
            }, status_code=404)

        version_dir = get_ontology_version_dir(ontology_dir, version, create=False)
        if version_dir is None or not version_dir.is_dir():
            return JSONResponse(content={
                "error": "Ontology version not found",
                "ontology_id": ontology_id,
                "version": version,
                "active_version": active_version,
                "versions": versions,
            }, status_code=404)

        if version == active_version:
            return JSONResponse(content={
                "error": "不能删除当前激活版本，请先切换激活版本后再删除。",
                "conflict_type": "active_version",
                "ontology_id": ontology_id,
                "version": version,
                "active_version": active_version,
                "versions": versions,
            }, status_code=409)

        reference_count, referenced_sessions = _find_sessions_locking_ontology_version(
            workspace_id=workspace_id,
            ontology_id=ontology_id,
            version=version,
        )
        if reference_count > 0:
            return JSONResponse(content={
                "error": "Locked version is still referenced by existing chat sessions and cannot be deleted.",
                "conflict_type": "session_reference",
                "ontology_id": ontology_id,
                "version": version,
                "active_version": active_version,
                "versions": versions,
                "reference_count": reference_count,
                "referenced_sessions": referenced_sessions,
            }, status_code=409)

        if len(versions) <= 1:
            return JSONResponse(content={
                "error": "最后一个版本不能按版本删除，请删除整个本体。",
                "conflict_type": "last_version",
                "ontology_id": ontology_id,
                "version": version,
                "active_version": active_version,
                "versions": versions,
            }, status_code=409)
        updated_versions = [item for item in versions if item != version]

        try:
            updated_manifest = dict(manifest)
            updated_manifest["version"] = active_version
            updated_manifest["active_version"] = active_version
            updated_manifest["versions"] = updated_versions
            _manifest, _active, enabled = _manifest_state(ontology_dir)
            directory_step = _prepare_atomic_directory_delete(version_dir)
            manifest_step = _prepare_atomic_bytes_replace(
                ontology_dir / "manifest.yaml",
                yaml.safe_dump(updated_manifest, allow_unicode=True, sort_keys=False).encode("utf-8"),
            )
            commit, rollback, finalize = _combine_prepared_steps(directory_step, manifest_step)
            mutation_result = _mutation_coordinator(workspace_id).commit(
                PreparedOntologyMutation(
                    workspace_id=workspace_id,
                    ontology_id=ontology_id,
                    mutation_type="ONTOLOGY_VERSION_DELETED",
                    before_version=active_version or None,
                    after_version=active_version or None,
                    before_enabled=enabled,
                    after_enabled=enabled,
                    commit=commit,
                    rollback=rollback,
                    finalize=finalize,
                    digest_hints=empty_digest_hints(scope_version=active_version or None),
                    trigger="api",
                    **_mutation_audit_fields(request),
                )
            )
        except OntologyMutationConflictError as exc:
            return JSONResponse(content={"error": str(exc), "code": exc.code}, status_code=409)
        except Exception:
            logger.exception(
                "Failed to delete ontology version: workspace_id=%s ontology_id=%s version=%s path=%s",
                workspace_id,
                ontology_id,
                version,
                version_dir,
            )
            return JSONResponse(content={"error": "Failed to delete ontology version"}, status_code=500)

        registry = get_ontology_registry(workspace_id=workspace_id)
        registry.refresh()

        logger.info(
            "Ontology version deleted: workspace_id=%s ontology_id=%s version=%s path=%s",
            workspace_id,
            ontology_id,
            version,
            version_dir,
        )

        export_ontology_snapshot_safely(workspace_id=workspace_id)
        return JSONResponse(content={
            "success": True,
            "ontology_id": ontology_id,
            "deleted_version": version,
            "active_version": active_version,
            "versions": updated_versions,
            "mutation_revision": mutation_result.mutation_revision,
        })


@ontologies_router.get("/ontologies/{ontology_id}/download")
def download_ontology(
    ontology_id: str,
    request: Request,
    workspace_id: str = Depends(require_workspace_id),
):
    with workspace_scope(workspace_id):
        ontology_dir = _resolve_ontology_dir(ontology_id)
        if ontology_dir is None:
            return JSONResponse(content={"error": "Ontology not found"}, status_code=404)

        try:
            version = _get_requested_version(request)
            if version and not is_valid_ontology_version(version):
                payload, status_code = _version_error_payload(version)
                return JSONResponse(content=payload, status_code=status_code)
            version_dir = _resolve_existing_version_dir(ontology_dir, version or None)
            if version_dir is None:
                payload, status_code = _version_error_payload(version)
                return JSONResponse(content=payload, status_code=status_code)
            manifest = load_ontology_manifest_data(ontology_dir)
            resolved_version = version or str(manifest.get("active_version") or manifest.get("version") or "").strip()
            zip_buffer = _build_ontology_zip_buffer(ontology_dir, version_dir, version=resolved_version)
        except Exception:
            logger.exception(
                "Failed to build ontology zip: workspace_id=%s ontology_id=%s path=%s",
                workspace_id,
                ontology_id,
                ontology_dir,
            )
            return JSONResponse(content={"error": "Failed to package ontology"}, status_code=500)

        logger.info(
            "Ontology downloaded: workspace_id=%s ontology_id=%s path=%s",
            workspace_id,
            ontology_id,
            ontology_dir,
        )
        return Response(
            content=zip_buffer.getvalue(),
            media_type="application/zip",
            headers={
                "Cache-Control": "no-cache",
                "Content-Disposition": (
                    'attachment; filename="{}-{}.zip"'.format(
                        ontology_dir.name,
                        resolved_version,
                    )
                ),
            },
        )


@ontologies_router.get("/ontologies/{ontology_id}/graph")
def get_ontology_graph(
    ontology_id: str,
    request: Request,
    workspace_id: str = Depends(require_workspace_id),
):
    with workspace_scope(workspace_id):
        version = _get_requested_version(request)
        if version and not is_valid_ontology_version(version):
            payload, status_code = _version_error_payload(version)
            return JSONResponse(content=payload, status_code=status_code)

        ontology_dir = _resolve_ontology_dir(ontology_id)
        if ontology_dir is None:
            return JSONResponse(content={"error": "Ontology not found"}, status_code=404)

        version_dir = _resolve_existing_version_dir(ontology_dir, version or None)
        if version_dir is None:
            payload, status_code = _version_error_payload(version)
            return JSONResponse(content=payload, status_code=status_code)

        registry = get_ontology_registry(workspace_id=workspace_id)
        registry.refresh()
        ontology = registry.get_ontology(ontology_id)
        if ontology is None:
            return JSONResponse(content={"error": "Ontology not found"}, status_code=404)

        return JSONResponse(content=
            build_ontology_graph(
                ontology=ontology,
                ontology_dir=version_dir,
                version=version or ontology.active_version or ontology.version,
            )
        )


@ontologies_router.get("/ontologies/{ontology_id}/tree")
def get_ontology_tree(
    ontology_id: str,
    request: Request,
    workspace_id: str = Depends(require_workspace_id),
):
    with workspace_scope(workspace_id):
        version = _get_requested_version(request)
        if version and not is_valid_ontology_version(version):
            payload, status_code = _version_error_payload(version)
            return JSONResponse(content=payload, status_code=status_code)
        ontology_dir = _resolve_ontology_dir(ontology_id)
        if ontology_dir is None:
            return JSONResponse(content={"error": "Ontology not found"}, status_code=404)
        try:
            version_dir = _resolve_package_browser_version_dir(ontology_dir, version or None)
        except PackageBrowserError as exc:
            return _package_browser_error_response(exc)
        if version_dir is None:
            return JSONResponse(content={"error": "Ontology version not found"}, status_code=404)

        try:
            entries = build_package_tree(version_dir, trusted_root=ontology_dir)
        except PackageBrowserError as exc:
            return _package_browser_error_response(exc)
        return JSONResponse(content=
            {
                "ontology_id": ontology_id,
                "version": version,
                "excluded": sorted(
                    name
                    for name in EXCLUDED_DIRECTORY_NAMES
                    if name != "__pycache__"
                ),
                "entries": entries,
            }
        )


@ontologies_router.get("/ontologies/{ontology_id}/raw-file")
def get_ontology_raw_file(
    ontology_id: str,
    request: Request,
    workspace_id: str = Depends(require_workspace_id),
):
    with workspace_scope(workspace_id):
        version = _get_requested_version(request)
        if version and not is_valid_ontology_version(version):
            payload, status_code = _version_error_payload(version)
            return JSONResponse(content=payload, status_code=status_code)
        ontology_dir = _resolve_ontology_dir(ontology_id)
        if ontology_dir is None:
            return JSONResponse(content={"error": "Ontology not found"}, status_code=404)
        try:
            version_dir = _resolve_package_browser_version_dir(ontology_dir, version or None)
        except PackageBrowserError as exc:
            return _package_browser_error_response(exc)
        if version_dir is None:
            return JSONResponse(content={"error": "Ontology version not found"}, status_code=404)

        try:
            file_payload = read_package_text(
                version_dir,
                request.query_params.get("path", ""),
                trusted_root=ontology_dir,
            )
        except PackageBrowserError as exc:
            if exc.code == "read_failed":
                logger.exception(
                    "Failed to read ontology package file: workspace_id=%s ontology_id=%s",
                    workspace_id,
                    ontology_id,
                )
            return _package_browser_error_response(exc)
        return JSONResponse(content=
            {
                "ontology_id": ontology_id,
                "version": version,
                **file_payload,
            }
        )


@ontologies_router.get("/ontologies/{ontology_id}/files")
def list_ontology_files(
    ontology_id: str,
    request: Request,
    workspace_id: str = Depends(require_workspace_id),
):
    with workspace_scope(workspace_id):
        version = _get_requested_version(request)
        if version and not is_valid_ontology_version(version):
            payload, status_code = _version_error_payload(version)
            return JSONResponse(content=payload, status_code=status_code)
        ontology_dir = _resolve_ontology_dir(ontology_id)
        try:
            folder_path, folder_key = _resolve_ontology_folder(
                ontology_id,
                request.query_params.get("folder", ""),
                version or None,
                reject_unsafe_version=True,
            )
        except PackageBrowserError as exc:
            return _package_browser_error_response(exc)
        if folder_path is None or folder_key is None:
            return JSONResponse(content={"error": "Invalid ontology or folder. Use 'operations', 'objects', or 'assets'."}, status_code=400)

        if not folder_path.exists():
            return JSONResponse(content={
                "folder": folder_key,
                "label": ONTOLOGY_FOLDER_LABELS[folder_key],
                "version": version,
                "files": [],
            })

        try:
            files = list_package_files(
                folder_path,
                recursive=folder_key == "assets",
                allowed_extensions=None if folder_key == "assets" else {".md"},
                include_hidden_files=folder_key == "assets",
                trusted_root=ontology_dir,
            )
        except PackageBrowserError as exc:
            return _package_browser_error_response(exc)

        return JSONResponse(content={
            "folder": folder_key,
            "label": ONTOLOGY_FOLDER_LABELS[folder_key],
            "version": version,
            "files": files,
        })


@ontologies_router.get("/ontologies/{ontology_id}/file")
def get_ontology_file(
    ontology_id: str,
    request: Request,
    workspace_id: str = Depends(require_workspace_id),
):
    with workspace_scope(workspace_id):
        version = _get_requested_version(request)
        if version and not is_valid_ontology_version(version):
            payload, status_code = _version_error_payload(version)
            return JSONResponse(content=payload, status_code=status_code)
        ontology_dir = _resolve_ontology_dir(ontology_id)
        try:
            folder_path, folder_key = _resolve_ontology_folder(
                ontology_id,
                request.query_params.get("folder", ""),
                version or None,
                reject_unsafe_version=True,
            )
        except PackageBrowserError as exc:
            return _package_browser_error_response(exc)
        if folder_path is None or folder_key is None:
            return JSONResponse(content={"error": "Invalid ontology or folder. Use 'operations', 'objects', or 'assets'."}, status_code=400)

        if folder_key == "assets":
            safe_name = _normalize_asset_key(request.query_params.get("name", ""))
            if not safe_name:
                return JSONResponse(content={"error": "Invalid asset key"}, status_code=400)
            version_dir = folder_path.parent
            file_path, asset_error = find_asset_file(
                version_dir,
                safe_name,
                trusted_root=ontology_dir,
            )
            if asset_error:
                return JSONResponse(content={"error": asset_error}, status_code=400)
            if file_path is None:
                return JSONResponse(
                    content={"error": "Error: Invalid asset key."},
                    status_code=400,
                )
            content, read_error = read_required_asset_text(file_path, "asset '{}'".format(safe_name))
            if read_error:
                return JSONResponse(content={"error": read_error}, status_code=400)
            return JSONResponse(content={
                "folder": folder_key,
                "label": ONTOLOGY_FOLDER_LABELS[folder_key],
                "version": version,
                "name": safe_name,
                "content": content,
                "read_only": True,
                "supported_formats": supported_asset_formats_text(),
            })

        safe_name = _sanitize_md_filename(request.query_params.get("name", ""))
        if not safe_name:
            return JSONResponse(content={"error": "Invalid file name. Only .md files are allowed."}, status_code=400)

        try:
            file_payload = read_package_text(
                folder_path,
                safe_name,
                allowed_extensions={".md"},
                trusted_root=ontology_dir,
            )
        except PackageBrowserError as exc:
            return _package_browser_error_response(exc)

        return JSONResponse(content={
            "folder": folder_key,
            "label": ONTOLOGY_FOLDER_LABELS[folder_key],
            "version": version,
            "name": safe_name,
            "content": file_payload["content"],
        })


@ontologies_router.put("/ontologies/{ontology_id}/file")
def update_ontology_file(
    ontology_id: str,
    request: Request,
    data: dict = Depends(_owner_json_payload),
    workspace_id: str = Depends(require_workspace_owner_or_admin_id),
):
    with workspace_scope(workspace_id):
        version = _get_json_requested_version(data)
        if version and not is_valid_ontology_version(version):
            payload, status_code = _version_error_payload(version)
            return JSONResponse(content=payload, status_code=status_code)
        folder_path, folder_key = _resolve_ontology_folder(ontology_id, data.get("folder", ""), version or None)
        if folder_path is None or folder_key is None:
            return JSONResponse(content={"error": "Invalid ontology or folder. Use 'operations', 'objects', or 'assets'."}, status_code=400)
        if folder_key == "assets":
            return JSONResponse(content={"error": "Asset files are read-only in this editor."}, status_code=400)
        immutable_response = _deterministic_package_immutable_response(folder_path)
        if immutable_response is not None:
            return immutable_response

        safe_name = _sanitize_md_filename(data.get("name", ""))
        if not safe_name:
            return JSONResponse(content={"error": "Invalid file name. Only .md files are allowed."}, status_code=400)

        content = data.get("content")
        if not isinstance(content, str):
            return JSONResponse(content={"error": "Content must be a string"}, status_code=400)
        declaration_response = _deterministic_declaration_immutable_response(
            folder_key,
            content,
        )
        if declaration_response is not None:
            return declaration_response

        validation_error = _validate_md_limits(content, folder_key)
        if validation_error:
            return JSONResponse(content={"error": validation_error}, status_code=400)

        file_path = folder_path / safe_name
        if not _is_safe_path(folder_path, file_path):
            return JSONResponse(content={"error": "Unsafe file path"}, status_code=400)
        if not file_path.exists() or not file_path.is_file():
            return JSONResponse(content={"error": "File not found"}, status_code=404)

        mutation_revision = None
        try:
            content_bytes = content.encode("utf-8")
            current_bytes = file_path.read_bytes()
            if current_bytes != content_bytes:
                _stage_and_commit_revision_file(
                    folder_path.parent,
                    file_path.relative_to(folder_path.parent),
                    content,
                )
                ontology_dir = _resolve_ontology_dir(ontology_id)
                if ontology_dir is None:
                    return JSONResponse(content={"error": "Ontology not found"}, status_code=404)
                _manifest, active_version, enabled = _manifest_state(ontology_dir)
                target_version = version or active_version
                if target_version == active_version:
                    base_revision = _mutation_base_revision(workspace_id, ontology_id)
                    digest_hints = build_digest_hints(
                        scope_version=active_version,
                        completeness="partial",
                        mutation_revision=1,
                        entries={
                            f"{folder_key}/{safe_name}": {
                                "sha256": hashlib.sha256(content_bytes).hexdigest(),
                                "size_bytes": len(content_bytes),
                            }
                        },
                    )
                    mutation_result = _mutation_coordinator(workspace_id).commit(
                        PreparedOntologyMutation(
                            workspace_id=workspace_id,
                            ontology_id=ontology_id,
                            mutation_type="ONTOLOGY_FILE_CHANGED",
                            before_version=active_version,
                            after_version=active_version,
                            before_enabled=enabled,
                            after_enabled=enabled,
                            # The staged revision write above already replaced the
                            # file; the mutation only records the revision and
                            # revokes the evidence index pointer so a worker can
                            # rebuild the ontology revision.
                            commit=lambda: None,
                            rollback=lambda: None,
                            digest_hints=digest_hints,
                            trigger="api",
                            base_mutation_revision=base_revision,
                            **_mutation_audit_fields(request),
                        )
                    )
                    mutation_revision = mutation_result.mutation_revision
        except OntologyMutationConflictError as exc:
            return JSONResponse(content={"error": str(exc), "code": exc.code}, status_code=409)
        except ValueError as exc:
            return JSONResponse(content={"error": str(exc)}, status_code=400)
        except Exception:
            logger.exception("Failed to update ontology file: %s", file_path)
            return JSONResponse(content={"error": "Failed to save file"}, status_code=500)

        response = {
            "success": True,
            "folder": folder_key,
            "label": ONTOLOGY_FOLDER_LABELS[folder_key],
            "version": version,
            "name": safe_name,
        }
        if mutation_revision is not None:
            response["mutation_revision"] = mutation_revision
        return JSONResponse(content=response)


@ontologies_router.post("/ontologies/{ontology_id}/upload")
def upload_ontology_file(
    ontology_id: str,
    request: Request,
    form: FormData = Depends(_owner_multipart_form),
    workspace_id: str = Depends(require_workspace_owner_or_admin_id),
):
    with workspace_scope(workspace_id):
        version = _get_requested_version(request, form=form)
        if version and not is_valid_ontology_version(version):
            payload, status_code = _version_error_payload(version)
            return JSONResponse(content=payload, status_code=status_code)
        folder_path, folder_key = _resolve_ontology_folder(
            ontology_id,
            form.get("folder", ""),
            version or None,
        )
        if folder_path is None or folder_key is None:
            return JSONResponse(content={"error": "Invalid ontology or folder. Use 'operations', 'objects', or 'assets'."}, status_code=400)
        if folder_key == "assets":
            return JSONResponse(content={"error": "Asset upload is not supported from this editor."}, status_code=400)
        immutable_response = _deterministic_package_immutable_response(folder_path)
        if immutable_response is not None:
            return immutable_response

        uploaded_file = form.get("file")
        if not isinstance(uploaded_file, UploadFile):
            return JSONResponse(content={"error": "No file uploaded"}, status_code=400)

        safe_name = _sanitize_md_filename(uploaded_file.filename or "")
        if not safe_name:
            return JSONResponse(content={"error": "Invalid file name. Only .md files are allowed."}, status_code=400)

        try:
            uploaded_bytes = uploaded_file.file.read()
        except Exception:
            return JSONResponse(content={"error": "Failed to read uploaded file"}, status_code=400)

        try:
            content = uploaded_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return JSONResponse(content={"error": "File encoding must be UTF-8"}, status_code=400)
        declaration_response = _deterministic_declaration_immutable_response(
            folder_key,
            content,
        )
        if declaration_response is not None:
            return declaration_response

        validation_error = _validate_md_limits(content, folder_key)
        if validation_error:
            return JSONResponse(content={"error": validation_error}, status_code=400)

        folder_path.mkdir(parents=True, exist_ok=True)
        target_path = folder_path / safe_name
        if not _is_safe_path(folder_path, target_path):
            return JSONResponse(content={"error": "Unsafe file path"}, status_code=400)

        count_error = _validate_new_markdown_file_count(folder_path, folder_key, target_path)
        if count_error:
            return JSONResponse(content={"error": count_error}, status_code=400)
        existed = target_path.exists()
        mutation_revision = None

        try:
            existing_bytes = target_path.read_bytes() if target_path.is_file() else None
            unchanged = existing_bytes is not None and existing_bytes == uploaded_bytes
            if not unchanged:
                _stage_and_commit_revision_file(
                    folder_path.parent,
                    target_path.relative_to(folder_path.parent),
                    content,
                )
                ontology_dir = _resolve_ontology_dir(ontology_id)
                if ontology_dir is None:
                    return JSONResponse(content={"error": "Ontology not found"}, status_code=404)
                _manifest, active_version, enabled = _manifest_state(ontology_dir)
                target_version = version or active_version
                if target_version == active_version:
                    base_revision = _mutation_base_revision(workspace_id, ontology_id)
                    digest_hints = build_digest_hints(
                        scope_version=active_version,
                        completeness="partial",
                        mutation_revision=1,
                        entries={
                            f"{folder_key}/{safe_name}": {
                                "sha256": hashlib.sha256(uploaded_bytes).hexdigest(),
                                "size_bytes": len(uploaded_bytes),
                            }
                        },
                    )
                    mutation_result = _mutation_coordinator(workspace_id).commit(
                        PreparedOntologyMutation(
                            workspace_id=workspace_id,
                            ontology_id=ontology_id,
                            mutation_type="ONTOLOGY_FILE_CHANGED",
                            before_version=active_version,
                            after_version=active_version,
                            before_enabled=enabled,
                            after_enabled=enabled,
                            # The staged revision write above already replaced the
                            # file; the mutation only records the revision and
                            # revokes the evidence index pointer so a worker can
                            # rebuild the ontology revision.
                            commit=lambda: None,
                            rollback=lambda: None,
                            digest_hints=digest_hints,
                            trigger="upload",
                            base_mutation_revision=base_revision,
                            **_mutation_audit_fields(request),
                        )
                    )
                    mutation_revision = mutation_result.mutation_revision
        except OntologyMutationConflictError as exc:
            return JSONResponse(content={"error": str(exc), "code": exc.code}, status_code=409)
        except ValueError as exc:
            return JSONResponse(content={"error": str(exc)}, status_code=400)
        except Exception:
            logger.exception("Failed to upload ontology file: %s", target_path)
            return JSONResponse(content={"error": "Failed to upload file"}, status_code=500)

        response = {
            "success": True,
            "folder": folder_key,
            "label": ONTOLOGY_FOLDER_LABELS[folder_key],
            "version": version,
            "name": safe_name,
            "overwritten": existed,
        }
        if mutation_revision is not None:
            response["mutation_revision"] = mutation_revision
        return JSONResponse(content=response, status_code=201)


def _find_skill_source_root(extract_root: Path) -> Optional[Path]:
    """Find source root containing object/logic after unzip.

    选择策略：优先包含 SKILL.md 的候选目录；否则退回最浅层候选。
    """
    candidates = []
    object_paths = []
    for object_dir in extract_root.rglob("object"):
        if not object_dir.is_dir():
            continue
        parent = object_dir.parent
        logic_dir = parent / "logic"
        object_paths.append(str(object_dir))
        if logic_dir.is_dir():
            candidates.append(parent)

    if not candidates:
        logger.warning(
            "Import zip source root not found: extract_root=%s object_dirs=%s",
            extract_root,
            object_paths[:20],
        )
        return None

    candidates.sort(key=lambda item: len(item.parts))

    selected = None
    for candidate in candidates:
        if _resolve_skill_md_path(candidate) is not None:
            selected = candidate
            break
    if selected is None:
        selected = candidates[0]

    logger.info(
        "Import zip source root selected: extract_root=%s selected=%s candidate_count=%s has_skill_md=%s",
        extract_root,
        selected,
        len(candidates),
        _resolve_skill_md_path(selected) is not None,
    )
    return selected


def _has_converted_ontology_structure(candidate_root: Path) -> bool:
    return has_importable_package_layout(candidate_root)


def _find_converted_ontology_root(
    extract_root: Path,
) -> Optional[tuple[Path, str, str, str, str, str]]:
    """Find a current or v0.1 ontology package root from an extracted zip."""
    candidates: list[tuple[int, Path, str, str, str, str, str]] = []
    manifest_paths = []

    for manifest_path in extract_root.rglob("manifest.yaml"):
        if not manifest_path.is_file():
            continue

        manifest_paths.append(str(manifest_path))
        candidate_root = manifest_path.parent
        if not _has_converted_ontology_structure(candidate_root):
            continue

        package = inspect_importable_package(candidate_root)
        if package is None:
            try:
                raw_manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
            except (OSError, UnicodeDecodeError, yaml.YAMLError):
                raw_manifest = {}
            if (
                isinstance(raw_manifest, dict)
                and str(raw_manifest.get("kind") or "").strip()
                in SUPPORTED_IMPORT_MANIFEST_KINDS
            ):
                raise OntologyImportError("Unsupported or invalid ontology package manifest")
            continue

        if package.source_format == "v0.2":
            try:
                validate_ontology_revision(candidate_root)
            except ValueError as exc:
                raise OntologyImportError(str(exc)) from exc

        candidates.append((
            len(candidate_root.parts),
            candidate_root,
            package.ontology_id,
            package.name,
            package.description,
            package.version,
            package.source_format,
        ))

    if not candidates:
        logger.info(
            "Converted ontology root not found: extract_root=%s manifest_count=%s manifest_samples=%s",
            extract_root,
            len(manifest_paths),
            manifest_paths[:20],
        )
        return None

    candidates.sort(key=lambda item: item[0])
    shallowest_depth = candidates[0][0]
    shallowest_candidates = [item for item in candidates if item[0] == shallowest_depth]
    if len(shallowest_candidates) > 1:
        raise OntologyImportError("Zip contains multiple ontology package roots")
    (
        _,
        selected_root,
        ontology_id,
        ontology_name,
        ontology_description,
        ontology_version,
        source_format,
    ) = candidates[0]
    logger.info(
        "Converted ontology root selected: extract_root=%s selected=%s candidate_count=%s ontology_id=%s",
        extract_root,
        selected_root,
        len(candidates),
        ontology_id,
    )
    return (
        selected_root,
        ontology_id,
        ontology_name,
        ontology_description,
        ontology_version,
        source_format,
    )


def _decode_zip_member_name(member: zipfile.ZipInfo) -> str:
    """Best-effort decode for legacy zip filename encodings.

    - UTF-8 flag set: trust decoded value from zipfile.
    - UTF-8 flag absent: recover raw bytes from cp437 and try common encodings.
    """
    raw_name = member.filename or ""
    if not raw_name:
        return ""

    # General purpose bit flag 11 indicates UTF-8 filenames
    if member.flag_bits & 0x800:
        return raw_name

    try:
        raw_bytes = raw_name.encode("cp437")
    except Exception:
        return raw_name

    for encoding in ("utf-8", "gbk", "gb18030", "big5"):
        try:
            decoded = raw_bytes.decode(encoding)
            if decoded:
                return decoded
        except Exception:
            continue

    return raw_name


def _normalize_zip_member_path(member_name: str) -> str:
    """Normalize separators without changing whether a member is absolute."""
    return str(member_name or "").replace("\\", "/")


def _zip_member_parts(member_name: str, *, is_directory: bool) -> Optional[tuple[str, ...]]:
    normalized = _normalize_zip_member_path(member_name)
    if not normalized or "\x00" in normalized or normalized.startswith("/"):
        return None
    if (
        re.match(r"^[A-Za-z]:", normalized)
        or len(normalized.encode("utf-8")) > ONTOLOGY_MAX_ZIP_PATH_LENGTH
    ):
        return None
    if is_directory and normalized.endswith("/"):
        normalized = normalized[:-1]
    parts = tuple(normalized.split("/"))
    if (
        not parts
        or len(parts) > ONTOLOGY_MAX_ZIP_PATH_DEPTH
        or any(not part or part in {".", ".."} for part in parts)
    ):
        return None
    return parts


def _canonical_zip_path(parts: tuple[str, ...]) -> str:
    return "/".join(unicodedata.normalize("NFC", part).casefold() for part in parts)


def _safe_extract_zip(uploaded_bytes: bytes, extract_root: Path) -> Optional[str]:
    """Extract zip with path traversal, size checks, and filename-encoding recovery."""
    try:
        with zipfile.ZipFile(io.BytesIO(uploaded_bytes)) as zf:
            members = zf.infolist()
            if len(members) > ONTOLOGY_MAX_ZIP_MEMBERS:
                return "Zip contains too many entries"
            total_uncompressed = 0
            renamed_count = 0
            preview_renamed: list[tuple[str, str]] = []
            target_kinds: dict[str, str] = {}

            for member in members:
                decoded_name = _normalize_zip_member_path(_decode_zip_member_name(member))
                is_directory = member.is_dir() or decoded_name.endswith("/")
                parts = _zip_member_parts(decoded_name, is_directory=is_directory)
                if parts is None:
                    return "Zip contains unsafe path"
                member_path = Path(*parts)

                unix_mode = int(member.external_attr or 0) >> 16
                if stat.S_ISLNK(unix_mode):
                    return "Zip contains symbolic link"

                canonical_path = _canonical_zip_path(parts)
                canonical_prefixes = [
                    _canonical_zip_path(parts[:index])
                    for index in range(1, len(parts))
                ]
                if any(target_kinds.get(prefix) == "file" for prefix in canonical_prefixes):
                    return "Zip contains colliding paths"
                if canonical_path in target_kinds:
                    return "Zip contains duplicate paths"
                if not is_directory and any(
                    existing.startswith(canonical_path + "/")
                    for existing in target_kinds
                ):
                    return "Zip contains colliding paths"
                target_kinds[canonical_path] = "dir" if is_directory else "file"

                total_uncompressed += max(0, int(member.file_size or 0))
                if total_uncompressed > ONTOLOGY_MAX_ZIP_UNCOMPRESSED_BYTES:
                    return "Zip uncompressed content is too large"

                target_path = extract_root / member_path
                if not _is_safe_path(extract_root, target_path):
                    return "Zip contains unsafe path"

                original_name = _normalize_zip_member_path(member.filename or "")
                if original_name != decoded_name:
                    renamed_count += 1
                    if len(preview_renamed) < 20:
                        preview_renamed.append((original_name, decoded_name))

                if is_directory:
                    target_path.mkdir(parents=True, exist_ok=True)
                    continue

                target_path.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member, "r") as src, target_path.open("wb") as dst:
                    shutil.copyfileobj(src, dst)

            if renamed_count:
                logger.info(
                    "Zip filename decoding applied: renamed_count=%s samples=%s",
                    renamed_count,
                    preview_renamed,
                )
    except zipfile.BadZipFile:
        return "Invalid zip file"
    except Exception:
        logger.exception("Failed to extract uploaded zip")
        return "Failed to extract zip"

    return None


def _resolve_skill_md_path(source_root: Path) -> Optional[Path]:
    preferred = source_root / "SKILL.md"
    if preferred.is_file():
        return preferred

    for child in source_root.iterdir():
        if child.is_file() and child.name.lower() == "skill.md":
            return child

    return None


def _parse_skill_frontmatter(source_root: Path) -> dict:
    skill_md_path = _resolve_skill_md_path(source_root)
    if skill_md_path is None:
        logger.warning("SKILL.md not found under source_root=%s", source_root)
        return {}

    try:
        # utf-8-sig可兼容带BOM的文件，避免首行---判断失败
        content = skill_md_path.read_text(encoding="utf-8-sig")
    except Exception:
        logger.exception("Failed to read SKILL.md: %s", skill_md_path)
        return {}

    # 允许文件开头存在少量空白/不可见字符，再识别frontmatter
    normalized_content = content.lstrip("\ufeff\r\n\t ")
    lines = normalized_content.splitlines()
    if len(lines) < 3 or lines[0].strip() != "---":
        logger.warning("SKILL.md frontmatter fence not found: %s", skill_md_path)
        return {}

    end_index = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end_index = idx
            break

    if end_index is None:
        logger.warning("SKILL.md frontmatter closing fence missing: %s", skill_md_path)
        return {}

    frontmatter_text = "\n".join(lines[1:end_index]).strip()
    if not frontmatter_text:
        return {}

    try:
        raw = yaml.safe_load(frontmatter_text) or {}
    except Exception:
        logger.exception("Failed to parse SKILL.md frontmatter: %s", skill_md_path)
        return {}

    return raw if isinstance(raw, dict) else {}


def _extract_ontology_name_from_markdown(source_root: Path) -> str:
    skill_md_path = _resolve_skill_md_path(source_root)
    if skill_md_path is not None:
        try:
            content = skill_md_path.read_text(encoding="utf-8-sig")
            for line in content.splitlines():
                match = re.match(r"^\s*#\s+(.+?)\s*$", line)
                if match:
                    return match.group(1).strip()
        except Exception:
            logger.exception("Failed to parse SKILL.md heading for ontology name: %s", skill_md_path)

    return source_root.name.strip() or "未命名本体"


def _extract_skill_metadata(source_root: Path) -> tuple[str, str, str, str]:
    frontmatter = _parse_skill_frontmatter(source_root)

    ontology_name = str(frontmatter.get("name") or "").strip() or _extract_ontology_name_from_markdown(source_root)
    frontmatter_id = str(frontmatter.get("id") or "").strip()
    ontology_id = frontmatter_id
    if not ontology_id:
        # 退避策略：当SKILL.md未提供id时，使用name作为目录名候选
        ontology_id = ontology_name

    description = str(frontmatter.get("description") or "").strip() or f"由上传压缩包自动导入：{ontology_name}"
    ontology_version = str(frontmatter.get("version") or "").strip()

    logger.info(
        "Skill metadata resolved: source_root=%s id=%s id_source=%s name=%s",
        source_root,
        ontology_id,
        "frontmatter.id" if frontmatter_id else "fallback.name_or_heading_or_dir",
        ontology_name,
    )

    return ontology_id, ontology_name, description, ontology_version


def _is_valid_ontology_id(ontology_id: str) -> bool:
    return bool(ontology_id and ONTOLOGY_ID_PATTERN.match(ontology_id))


def _write_default_ontology_meta(
    ontology_dir: Path,
    ontology_id: str,
    ontology_name: str,
    description: str,
    version: str,
) -> None:
    manifest_path = ontology_dir / "manifest.yaml"
    index_path = ontology_dir / "index.md"

    if not manifest_path.exists():
        manifest_path.write_text(
            yaml.safe_dump(
                {
                    "kind": "ontology-package",
                    "schema_version": 0.1,
                    "id": ontology_id,
                    "name": ontology_name,
                    "description": description,
                    "version": version,
                    "language": "zh-CN",
                    "enabled": True,
                    "scope": [],
                },
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )

    if not index_path.exists():
        index_content = "\n".join([
            "---",
            "resource: root",
            "key: root",
            f"title: {ontology_name}",
            "---",
            "",
            f"# {ontology_name}",
            "",
            "## 领域范围",
            description,
            "",
            "## 目录结构",
            "- 基础元数据：`manifest.yaml`",
            "- 操作目录：`operations/index.md`",
            "- 操作文件：`operations/<操作名>.md`",
            "- 实体目录：`objects/index.md`",
            "- 实体定义：`objects/<实体名>.md`",
            "- 脚本目录：`scripts/index.md`",
            "- 脚本描述：`scripts/<脚本名>.md`",
            "- 脚本文件：`scripts/<脚本名>.py`",
            "- 关系图：`graph/nodes.json`、`graph/edges.json`",
            "",
        ])
        index_path.write_text(index_content, encoding="utf-8")


def _set_ontology_enabled(ontology_dir: Path, enabled: bool):
    with _ontology_install_lock(ontology_dir):
        manifest_path = ontology_dir / "manifest.yaml"
        if not manifest_path.is_file() or manifest_path.is_symlink():
            raise ValueError("Ontology manifest not found")

        manifest = load_ontology_manifest_data(ontology_dir)
        manifest["enabled"] = bool(enabled)
        _write_manifest_atomic(manifest_path, manifest)

        ontology = OntologyLoader.load_ontology(ontology_dir)
        if ontology is None:
            raise ValueError("Failed to reload ontology manifest")
        return ontology


def _write_manifest_atomic(manifest_path: Path, manifest: dict) -> None:
    payload = yaml.safe_dump(
        manifest,
        allow_unicode=True,
        sort_keys=False,
    ).encode("utf-8")
    _write_file_atomic(manifest_path, payload, prefix=".manifest-")


def _write_file_atomic(file_path: Path, payload: bytes, *, prefix: str) -> None:
    with staged_bytes(
        payload=payload,
        prefix=prefix,
        suffix=".tmp",
        directory=str(file_path.parent),
    ) as temporary_path:
        os.replace(temporary_path, file_path)


def _stage_and_commit_revision_file(
    version_dir: Path,
    relative_path: Path,
    content: str,
) -> None:
    """Validate a proposed edit in a complete staged revision before commit."""

    version_dir = Path(version_dir)
    relative_path = Path(relative_path)
    if (
        relative_path.is_absolute()
        or not relative_path.parts
        or ".." in relative_path.parts
    ):
        raise ValueError("Unsafe ontology revision path")
    if not version_dir.is_dir() or version_dir.is_symlink():
        raise ValueError("Unsafe ontology revision path")
    live_path = version_dir / relative_path
    if not _is_safe_path(version_dir, live_path):
        raise ValueError("Unsafe ontology revision path")

    with tempfile.TemporaryDirectory(
        prefix=".ontology-revision-edit-",
        dir=str(version_dir.parent),
    ) as temporary_dir:
        staged_root = Path(temporary_dir) / "revision"
        shutil.copytree(version_dir, staged_root, symlinks=True)
        staged_manifest = staged_root / "manifest.yaml"
        shared_manifest = version_dir.parent.parent / "manifest.yaml"
        if (
            not staged_manifest.exists()
            and version_dir.parent.name == "versions"
        ):
            if shared_manifest.is_file() and not shared_manifest.is_symlink():
                shutil.copy2(shared_manifest, staged_manifest)
        staged_path = staged_root / relative_path
        staged_path.parent.mkdir(parents=True, exist_ok=True)
        staged_path.write_text(content, encoding="utf-8")
        validate_ontology_revision(staged_root)

    live_path.parent.mkdir(parents=True, exist_ok=True)
    _write_file_atomic(
        live_path,
        content.encode("utf-8"),
        prefix=".ontology-file-",
    )


def _count_markdown_files(folder_path: Path) -> int:
    if not folder_path.is_dir():
        return 0

    return sum(
        1
        for child in folder_path.iterdir()
        if child.is_file() and child.suffix.lower() == ".md" and child.name.lower() != "index.md"
    )


def _collect_import_preview_files(root: Path) -> dict[str, object]:
    if not root.is_dir():
        return {"files": [], "total_files": 0, "truncated": False}

    all_files = sorted(
        [path for path in root.rglob("*") if path.is_file()],
        key=lambda path: path.relative_to(root).as_posix(),
    )
    preview_files = []

    for path in all_files[:ONTOLOGY_IMPORT_PREVIEW_MAX_FILES]:
        relative_path = path.relative_to(root).as_posix()
        try:
            size = path.stat().st_size
        except OSError:
            size = 0

        item = {
            "path": relative_path,
            "size": size,
            "content": "",
            "truncated": False,
            "binary": path.suffix.lower() not in ONTOLOGY_IMPORT_PREVIEW_TEXT_EXTENSIONS,
        }
        if not item["binary"]:
            try:
                raw_content = path.read_bytes()
                item["truncated"] = len(raw_content) > ONTOLOGY_IMPORT_PREVIEW_MAX_BYTES
                item["content"] = raw_content[:ONTOLOGY_IMPORT_PREVIEW_MAX_BYTES].decode(
                    "utf-8",
                    errors="replace",
                )
            except Exception:
                item["binary"] = True
                item["content"] = ""

        preview_files.append(item)

    return {
        "files": preview_files,
        "total_files": len(all_files),
        "truncated": len(all_files) > ONTOLOGY_IMPORT_PREVIEW_MAX_FILES,
    }


def _build_import_preview(source_root: Path, result_root: Path) -> dict[str, object]:
    return {
        "source": _collect_import_preview_files(source_root),
        "result": _collect_import_preview_files(result_root),
    }


def _remove_path_if_exists(path: Path) -> None:
    path = Path(path)
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def _replace_ontology_dir(*, staged_dir: Path, ontology_dir: Path) -> None:
    """Publish a staged ontology with rollback and startup recovery metadata."""

    ontology_dir.parent.mkdir(parents=True, exist_ok=True)
    if ontology_dir.exists():
        portable_replace_directory(staged_dir=staged_dir, target_dir=ontology_dir)
    else:
        staged_dir.replace(ontology_dir)


def _copy_versioned_package_contents(source_root: Path, target_root: Path) -> None:
    if is_deterministic_package_locked(target_root):
        raise DeterministicPackageError(
            "deterministic_operation_immutable",
            "确定性 Operation 所在版本不可原地覆盖，请发布新的 ontology version。",
        )
    target_root.parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(
        tempfile.mkdtemp(
            prefix=".{}.staged-".format(target_root.name),
            dir=str(target_root.parent),
        )
    )
    staged_target = staging_root / target_root.name
    staged_target.mkdir(parents=True, exist_ok=True)

    try:
        for child in source_root.iterdir():
            if child.name in ONTOLOGY_VERSIONED_SKIP_NAMES:
                continue
            target_path = staged_target / child.name
            if not _is_safe_path(staged_target, target_path):
                raise ValueError("Unsafe target path")
            if child.is_dir():
                shutil.copytree(child, target_path)
            else:
                shutil.copy2(child, target_path)
        source_manifest = source_root / "manifest.yaml"
        if source_manifest.is_file() and not source_manifest.is_symlink():
            shutil.copy2(source_manifest, staged_target / "manifest.yaml")
        _replace_ontology_dir(
            staged_dir=staged_target,
            ontology_dir=target_root,
        )
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)


def _build_complete_package_digest_hints(
    *,
    version_dir: Path,
    ontology_id: str,
    ontology_version: str,
    manifest: Mapping[str, object],
    mutation_revision: int,
) -> dict[str, object]:
    """Build the complete semantic digest set used by OntologyChanged/v1."""
    package_sources = PackageSourceReader().read(
        ontology_dir=version_dir,
        ontology_id=ontology_id,
        ontology_version=ontology_version,
    )
    entries: dict[str, dict[str, object]] = {}
    for source in package_sources.sources:
        raw_bytes = (version_dir / source.source_path).read_bytes()
        entries[source.source_path] = {
            "sha256": hashlib.sha256(raw_bytes).hexdigest(),
            "size_bytes": len(raw_bytes),
        }
    return build_digest_hints(
        scope_version=ontology_version,
        completeness="complete",
        mutation_revision=mutation_revision,
        semantic_manifest_digest=build_semantic_manifest_digest(manifest),
        entries=entries,
    )


def _prepare_shared_manifest(
    *,
    ontology_dir: Path,
    source_root: Path,
    ontology_id: str,
    ontology_name: str,
    ontology_description: str,
    target_version: str,
    existed: bool,
) -> dict:
    source_manifest_path = source_root / "manifest.yaml"
    if source_manifest_path.is_file():
        validate_ontology_revision(source_root)
        source_manifest = yaml.safe_load(source_manifest_path.read_text(encoding="utf-8")) or {}
        if not isinstance(source_manifest, dict):
            source_manifest = {}
    else:
        source_manifest = {}
    existing_manifest = ensure_ontology_versions(ontology_dir) if existed else None
    return build_versioned_install_manifest(
        source_manifest=source_manifest,
        existing_manifest=existing_manifest,
        ontology_id=ontology_id,
        ontology_name=ontology_name,
        ontology_description=ontology_description,
        target_version=target_version,
    )


def _install_ontology_version_package_in_place(
    *,
    source_root: Path,
    ontology_dir: Path,
    ontology_id: str,
    ontology_name: str,
    ontology_description: str,
    target_version: str,
    existed: bool,
    allow_existing_target: bool = False,
) -> Path:
    source_root = Path(source_root)
    ontology_dir = Path(ontology_dir)
    installed_version_dir = ontology_dir / "versions" / target_version
    ontology_dir.parent.mkdir(parents=True, exist_ok=True)
    with _ontology_install_lock(ontology_dir):
        current_existed = ontology_dir.is_dir()
        if is_deterministic_package_locked(installed_version_dir):
            raise DeterministicPackageError(
                "deterministic_operation_immutable",
                "确定性 Operation 所在版本不可原地覆盖，请发布新的 ontology version。",
            )
        if installed_version_dir.exists() and not allow_existing_target:
            raise DeterministicPackageError(
                "ontology_version_conflict",
                "Ontology version '{}' already exists.".format(target_version),
            )

        staged_dir = Path(
            tempfile.mkdtemp(
                prefix=".{}.install-".format(ontology_dir.name),
                dir=str(ontology_dir.parent),
            )
        )
        try:
            if current_existed:
                shutil.copytree(ontology_dir, staged_dir, dirs_exist_ok=True, symlinks=True)
            manifest = _prepare_shared_manifest(
                ontology_dir=staged_dir,
                source_root=source_root,
                ontology_id=ontology_id,
                ontology_name=ontology_name,
                ontology_description=ontology_description,
                target_version=target_version,
                existed=current_existed,
            )
            staged_version_dir = staged_dir / "versions" / target_version
            _copy_versioned_package_contents(source_root, staged_version_dir)
            # A new version may be produced from an evolved snapshot that still
            # carries the previous version's lock.  Always validate the copied
            # payload and generate a lock for the exact bytes being published.
            # Legacy packages remain unchanged because the validator removes a
            # stale lock and returns no specs when no deterministic operations
            # are declared.
            if str(manifest.get("schema_version") or "").strip() != "0.2":
                validate_and_lock_deterministic_operations(staged_version_dir)
            save_ontology_manifest_data(staged_dir, manifest)
            _replace_ontology_dir(staged_dir=staged_dir, ontology_dir=ontology_dir)
        finally:
            _remove_path_if_exists(staged_dir)
    return installed_version_dir


def _deterministic_package_immutable_response(folder_path: Path):
    """Reject in-place edits of an installed deterministic package version."""
    version_dir = Path(folder_path).parent
    if not is_deterministic_package_locked(version_dir):
        return None
    return _deterministic_operation_immutable_response()


def _deterministic_declaration_immutable_response(folder_key: str, content: str):
    """Require a new version when an editor payload declares execution."""
    if folder_key != "operations" or not has_execution_declaration(content):
        return None
    return _deterministic_operation_immutable_response()


def _deterministic_operation_immutable_response():
    return JSONResponse(
        content={
            "error": "Deterministic operations must be published as a new ontology version.",
            "error_code": "deterministic_operation_immutable",
        },
        status_code=409,
    )


def _install_ontology_version_package(
    *,
    source_root: Path,
    ontology_dir: Path,
    ontology_id: str,
    ontology_name: str,
    ontology_description: str,
    target_version: str,
    existed: bool,
    workspace_id: str,
    request: Optional[Request] = None,
    mutation_audit: Optional[Mapping[str, object]] = None,
    mutation_trigger: str = "import",
    allow_existing_target: bool = False,
) -> tuple[Path, int]:
    """Run the staged install and swap under the install lock (serializes
    concurrent installs and other ontology mutations), recording the mutation
    through the coordinator."""
    ontology_dir.parent.mkdir(parents=True, exist_ok=True)
    coordinator = _mutation_coordinator(workspace_id)

    staging_root = Path(
        tempfile.mkdtemp(prefix=f".{ontology_id}-mutation-", dir=str(ontology_dir.parent))
    )
    staged_dir = staging_root / ontology_id
    backup_dir = staging_root / f"{ontology_id}.backup"
    try:
        with _ontology_install_lock(ontology_dir):
            # Re-read the authoritative state while holding the install lock so
            # a stably-serialized concurrent install is not based on a stale
            # mutation revision or before-state captured before lock acquisition.
            state = coordinator.store.get_state(workspace_id=workspace_id, ontology_id=ontology_id)
            base_revision = int(state["mutation_revision"]) if state is not None else 0
            if existed:
                _before_manifest, before_version, before_enabled = _manifest_state(ontology_dir)
            else:
                before_version, before_enabled = "", False
            if existed:
                shutil.copytree(ontology_dir, staged_dir)
            version_dir = _install_ontology_version_package_in_place(
                source_root=source_root,
                ontology_dir=staged_dir,
                ontology_id=ontology_id,
                ontology_name=ontology_name,
                ontology_description=ontology_description,
                target_version=target_version,
                existed=existed,
                allow_existing_target=allow_existing_target,
            )
            staged_manifest, after_version, after_enabled = _manifest_state(staged_dir)
            digest_hints = _build_complete_package_digest_hints(
                version_dir=version_dir,
                ontology_id=ontology_id,
                ontology_version=after_version,
                manifest=staged_manifest,
                mutation_revision=base_revision + 1,
            )

            commit, rollback, finalize_swap = _prepare_atomic_directory_replace(
                staged_dir,
                ontology_dir,
                backup_dir,
            )

            def finalize() -> None:
                finalize_swap()
                if staging_root.exists():
                    shutil.rmtree(staging_root)

            audit_fields = dict(mutation_audit) if mutation_audit is not None else _mutation_audit_fields(request)
            mutation_result = coordinator.commit(
                PreparedOntologyMutation(
                    workspace_id=workspace_id,
                    ontology_id=ontology_id,
                    mutation_type="ONTOLOGY_IMPORTED",
                    before_version=before_version or None,
                    after_version=after_version,
                    before_enabled=before_enabled,
                    after_enabled=after_enabled,
                    commit=commit,
                    rollback=rollback,
                    finalize=finalize,
                    digest_hints=digest_hints,
                    trigger=mutation_trigger,
                    base_mutation_revision=base_revision,
                    **audit_fields,
                )
            )
        return ontology_dir / "versions" / target_version, mutation_result.mutation_revision
    except Exception as exc:
        _cleanup_mutation_staging_root(staging_root, exc)
        raise


def _overwrite_ontology_active_version_package(
    *,
    source_root: Path,
    ontology_dir: Path,
    ontology_id: str,
    workspace_id: str,
    request: Optional[Request] = None,
    mutation_audit: Optional[Mapping[str, object]] = None,
    mutation_trigger: str = "admin",
) -> tuple[Path, int]:
    """Atomically replace the active version and emit ONTOLOGY_FILE_CHANGED.

    Skill evolution's overwrite mode keeps the public version number stable,
    but it still changes the authoritative package bytes.  Routing the swap
    through the mutation coordinator guarantees pointer revocation, revision
    monotonicity and a durable OntologyChanged/v1 outbox event.
    """
    if not ontology_dir.is_dir():
        raise ValueError("Ontology directory not found")

    ontology_dir.parent.mkdir(parents=True, exist_ok=True)
    coordinator = _mutation_coordinator(workspace_id)
    state = coordinator.store.get_state(workspace_id=workspace_id, ontology_id=ontology_id)
    base_revision = int(state["mutation_revision"]) if state is not None else 0
    _before_manifest, active_version, enabled = _manifest_state(ontology_dir)
    if not active_version:
        raise ValueError("Ontology active version is missing")

    staging_root = Path(
        tempfile.mkdtemp(prefix=f".{ontology_id}-mutation-", dir=str(ontology_dir.parent))
    )
    staged_dir = staging_root / ontology_id
    backup_dir = staging_root / f"{ontology_id}.backup"
    try:
        shutil.copytree(ontology_dir, staged_dir)
        staged_version_dir = get_ontology_version_dir(
            staged_dir,
            active_version,
            create=False,
        )
        if staged_version_dir is None:
            raise ValueError("Ontology active version directory not found")
        _copy_versioned_package_contents(source_root, staged_version_dir)

        staged_manifest, after_version, after_enabled = _manifest_state(staged_dir)
        if after_version != active_version or after_enabled != enabled:
            raise ValueError("Overwrite must not change ontology version or visibility")
        digest_hints = _build_complete_package_digest_hints(
            version_dir=staged_version_dir,
            ontology_id=ontology_id,
            ontology_version=active_version,
            manifest=staged_manifest,
            mutation_revision=base_revision + 1,
        )

        commit, rollback, finalize_swap = _prepare_atomic_directory_replace(
            staged_dir,
            ontology_dir,
            backup_dir,
        )

        def finalize() -> None:
            finalize_swap()
            if staging_root.exists():
                shutil.rmtree(staging_root)

        audit_fields = dict(mutation_audit) if mutation_audit is not None else _mutation_audit_fields(request)
        mutation_result = coordinator.commit(
            PreparedOntologyMutation(
                workspace_id=workspace_id,
                ontology_id=ontology_id,
                mutation_type="ONTOLOGY_FILE_CHANGED",
                before_version=active_version,
                after_version=active_version,
                before_enabled=enabled,
                after_enabled=enabled,
                commit=commit,
                rollback=rollback,
                finalize=finalize,
                digest_hints=digest_hints,
                trigger=mutation_trigger,
                base_mutation_revision=base_revision,
                **audit_fields,
            )
        )
        return (
            ontology_dir / "versions" / active_version,
            mutation_result.mutation_revision,
        )
    except Exception as exc:
        _cleanup_mutation_staging_root(staging_root, exc)
        raise


def _optional_path_str(path: Path) -> Optional[str]:
    return str(path) if path.exists() else None


@ontologies_router.post("/ontologies/import-zip")
def import_ontology_zip_with_auto_create(
    request: Request,
    form: FormData = Depends(_owner_multipart_form),
    workspace_id: str = Depends(require_workspace_owner_or_admin_id),
):
    """Import ontology zip and auto-create ontology when none is selected."""
    with workspace_scope(workspace_id):
        uploaded_file = form.get("file")
        if not isinstance(uploaded_file, UploadFile):
            return JSONResponse(content={"error": "No file uploaded"}, status_code=400)

        filename = (uploaded_file.filename or "").strip()
        if not filename.lower().endswith(".zip"):
            return JSONResponse(content={"error": "Only .zip package is supported"}, status_code=400)

        uploaded_bytes, upload_error = _read_limited_zip_upload(uploaded_file)
        if upload_error:
            return JSONResponse(
                content={"error": upload_error},
                status_code=400,
            )
        if uploaded_bytes is None:
            return JSONResponse(
                content={"error": "Failed to read uploaded file"},
                status_code=400,
            )

        if not uploaded_bytes:
            return JSONResponse(content={"error": "Uploaded file is empty"}, status_code=400)

        with tempfile.TemporaryDirectory(prefix="ontology-import-auto-") as temp_dir:
            extract_root = Path(temp_dir) / "unzipped"
            extract_root.mkdir(parents=True, exist_ok=True)

            extraction_error = _safe_extract_zip(uploaded_bytes, extract_root)
            if extraction_error:
                return JSONResponse(content={"error": extraction_error}, status_code=400)

            import_mode = "converted"
            package_format = "v0.1"
            normalization_warnings: list[str] = []
            warnings = {
                "objects": [],
                "objects_index": [],
                "operations": [],
                "object_aliases": [],
                "field_aliases": [],
            }

            try:
                converted_root_info = _find_converted_ontology_root(extract_root)
            except OntologyImportError as exc:
                return JSONResponse(content={"error": str(exc)}, status_code=400)
            if converted_root_info is not None:
                (
                    source_root,
                    ontology_id,
                    ontology_name,
                    ontology_description,
                    parsed_version,
                    package_format,
                ) = converted_root_info
            else:
                return JSONResponse(
                    content={
                        "error": (
                            "Zip中未找到可导入的v0.1本体包。"
                            "请将源格式包转换为v0.1后重新上传。"
                        )
                    },
                    status_code=400,
                )
            if not ontology_id:
                return JSONResponse(
                    content={"error": "manifest.yaml缺少合法的ontology id，无法导入"},
                    status_code=400,
                )
            if not _is_valid_ontology_id(ontology_id):
                return JSONResponse(content={
                    "error": (
                        "ontology id不合法，仅支持字母、数字、点、下划线和短横线，"
                        "且需字母或数字开头"
                    ),
                    "ontology_id": ontology_id,
                }, status_code=400)

            overwrite_flag = str(form.get("overwrite", "")).strip().lower() in {
                "1", "true", "yes", "y", "on"
            }
            action = str(form.get("action") or form.get("import_action") or "").strip()
            requested_new_version = str(form.get("new_version") or form.get("version") or "").strip()
            parsed_version = str(parsed_version or "").strip()
            if parsed_version and not is_valid_ontology_version(parsed_version):
                return JSONResponse(content={
                    "error": "version不合法，仅支持[A-Za-z0-9][A-Za-z0-9._-]*",
                    "ontology_id": ontology_id,
                    "version": parsed_version,
                }, status_code=400)
            if requested_new_version and not is_valid_ontology_version(requested_new_version):
                return JSONResponse(content={
                    "error": "new_version不合法，仅支持[A-Za-z0-9][A-Za-z0-9._-]*",
                    "ontology_id": ontology_id,
                    "version": requested_new_version,
                }, status_code=400)

            ontologies_dir = _get_ontologies_dir()
            ontologies_dir.mkdir(parents=True, exist_ok=True)

            ontology_dir = ontologies_dir / ontology_id
            existed = ontology_dir.is_dir()
            active_version = ""
            existing_versions: list[str] = []
            if existed:
                try:
                    with _ontology_install_lock(ontology_dir):
                        existing_manifest = ensure_ontology_versions(ontology_dir)
                except Exception:
                    logger.exception("Failed to prepare existing ontology versions: %s", ontology_dir)
                    return JSONResponse(content={"error": "Failed to prepare existing ontology versions"}, status_code=500)
                active_version = str(
                    existing_manifest.get("active_version")
                    or existing_manifest.get("version")
                    or ""
                ).strip()
                existing_versions = [
                    str(item).strip()
                    for item in (existing_manifest.get("versions") or [])
                    if str(item or "").strip()
                ]

            count_error = _validate_workspace_ontology_count_limit(
                ontologies_dir,
                ontology_id=ontology_id,
                existed=existed,
            )
            if count_error:
                return JSONResponse(content={"error": count_error}, status_code=400)

            target_version = parsed_version or "1.0.0"
            overwritten = False
            if existed:
                if not parsed_version:
                    if action in {"overwrite_active", "overwrite_version"} or overwrite_flag:
                        target_version = active_version
                        overwritten = True
                    elif action == "create_version":
                        target_version = requested_new_version
                    else:
                        return JSONResponse(content={
                            "error": f"本体 {ontology_id} 已存在，上传包未解析到版本号，请选择覆盖当前激活版本或新增版本号。",
                            "requires_confirmation": True,
                            "conflict_type": "missing_version_existing",
                            "ontology_id": ontology_id,
                            "ontology_name": ontology_name,
                            "active_version": active_version,
                            "versions": existing_versions,
                        }, status_code=409)
                elif parsed_version in existing_versions:
                    if action in {"overwrite_matching", "overwrite_version"} or overwrite_flag:
                        target_version = parsed_version
                        overwritten = True
                    elif action == "create_version":
                        target_version = requested_new_version
                    else:
                        return JSONResponse(content={
                            "error": f"本体 {ontology_id} 的版本 {parsed_version} 已存在，请选择覆盖匹配版本或新增版本号。",
                            "requires_confirmation": True,
                            "conflict_type": "version_exists",
                            "ontology_id": ontology_id,
                            "ontology_name": ontology_name,
                            "version": parsed_version,
                            "active_version": active_version,
                            "versions": existing_versions,
                        }, status_code=409)
                else:
                    target_version = parsed_version

            if not target_version or not is_valid_ontology_version(target_version):
                return JSONResponse(content={
                    "error": "新增版本号不合法，仅支持[A-Za-z0-9][A-Za-z0-9._-]*",
                    "ontology_id": ontology_id,
                }, status_code=400)
            if existed and target_version in existing_versions and action == "create_version":
                return JSONResponse(content={
                    "error": f"版本 {target_version} 已存在，请换一个版本号。",
                    "ontology_id": ontology_id,
                    "version": target_version,
                }, status_code=409)
            target_version_exists = existed and target_version in existing_versions
            incoming_version_is_new = parsed_version not in existing_versions
            if target_version_exists and incoming_version_is_new and not overwritten:
                return JSONResponse(content={
                    "error": f"版本 {target_version} 已存在，请换一个版本号。",
                    "ontology_id": ontology_id,
                    "version": target_version,
                }, status_code=409)

            checkpoint_reset_for_import = False
            try:
                # Process v0.1 conformant package
                with tempfile.TemporaryDirectory(
                    prefix="ontology-normalized-"
                ) as normalized_temp:
                    normalized_root = Path(normalized_temp) / ontology_id
                    normalization = normalize_imported_package(source_root, normalized_root)
                    normalization_warnings = list(normalization.warnings)
                    validation_error = _validate_operations_tree_limits(normalization.root)
                    if validation_error:
                        logger.warning(
                            "Import rejected by tree limits: ontology_id=%s error=%s",
                            ontology_id,
                            validation_error,
                        )
                        return JSONResponse(
                            content={"error": validation_error},
                            status_code=400,
                        )
                    alias_validation = validate_imported_object_aliases(normalization.root)
                    field_alias_validation = validate_imported_field_aliases(
                        normalization.root
                    )
                    alias_errors = tuple(
                        sorted(
                            set(
                                (
                                    *alias_validation.errors,
                                    *field_alias_validation.errors,
                                )
                            )
                        )
)
                    if alias_errors:
                        logger.warning(
                            "Import rejected by alias validation: ontology_id=%s errors=%s",
                            ontology_id,
                            alias_errors,
                        )
                        return JSONResponse(
                            content={"error": "\n".join(alias_errors)},
                            status_code=400,
                        )
                    warnings["object_aliases"] = list(alias_validation.warnings)
                    warnings["field_aliases"] = list(field_alias_validation.warnings)
                    preview = _build_import_preview(source_root, normalization.root)
                    try:
                        reset_business_checkpoint_capability(
                            ontology_id,
                            workspace_id=workspace_id,
                        )
                        checkpoint_reset_for_import = True
                    except OntologyCapabilityStoreError:
                        logger.exception(
                            "Failed to reset business CheckPoint capability before import: workspace_id=%s ontology_id=%s",
                            workspace_id,
                            ontology_id,
                        )
                        return JSONResponse(
                            content={"error": "Failed to initialize CheckPoint capability"},
                            status_code=500,
                        )
                    version_dir, mutation_revision = _install_ontology_version_package(
                        source_root=normalization.root,
                        ontology_dir=ontology_dir,
                        ontology_id=ontology_id,
                        ontology_name=ontology_name,
                        ontology_description=ontology_description,
                        target_version=target_version,
                        existed=existed,
                        workspace_id=workspace_id,
                        allow_existing_target=overwritten,
                    )
                objects_count = _count_markdown_files(version_dir / "objects")
                operations_count = _count_markdown_files(version_dir / "operations")
                objects_index_path = _optional_path_str(version_dir / "objects" / "index.md")
                operations_index_path = _optional_path_str(version_dir / "operations" / "index.md")
            except PackageNormalizationError as exc:
                logger.warning(
                    "Import rejected by normalization: ontology_id=%s error=%s",
                    ontology_id,
                    exc,
                )
                payload = {"error": str(exc)}
                if getattr(exc, "code", "invalid_ontology_package") != "invalid_ontology_package":
                    payload["error_code"] = exc.code
                return JSONResponse(content=payload, status_code=400)
            except DeterministicPackageError as exc:
                return JSONResponse(
                    content={"error": str(exc), "error_code": exc.code},
                    status_code=(
                        409
                        if exc.code
                        in {
                            "deterministic_operation_immutable",
                            "ontology_version_conflict",
                        }
                        else 400
                    ),
                )
            except Exception:
                if checkpoint_reset_for_import and not existed:
                    try:
                        delete_ontology_capabilities(ontology_id, workspace_id=workspace_id)
                    except OntologyCapabilityStoreError:
                        logger.warning(
                            "Failed to clean up CheckPoint capability after new ontology import failure: workspace_id=%s ontology_id=%s",
                            workspace_id,
                            ontology_id,
                            exc_info=True,
                        )
                logger.exception(
                    "Failed to import ontology from uploaded zip: workspace_id=%s filename=%s ontology_id=%s source_root=%s import_mode=%s",
                    workspace_id,
                    filename,
                    ontology_id,
                    source_root,
                    import_mode,
                )
                return JSONResponse(content={"error": "Failed to parse uploaded zip"}, status_code=500)

        export_ontology_snapshot_safely(workspace_id=workspace_id)
        invalidate_ontology_registry(workspace_id=workspace_id)
        business_data_summary = build_ontology_business_data_summary(
            ontology_id,
            workspace_id=workspace_id,
        )
        logger.info(
            "Import zip auto-create success: workspace_id=%s ontology_id=%s created=%s overwritten=%s import_mode=%s objects_count=%s operations_count=%s warnings=%s",
            workspace_id,
            ontology_id,
            (not existed),
            overwritten,
            import_mode,
            objects_count,
            operations_count,
            {key: len(value) for key, value in warnings.items()},
        )

        return JSONResponse(content={
            "success": True,
            "created": not existed,
            "overwritten": overwritten,
            "import_mode": import_mode,
            "package_format": package_format,
            "ontology_id": ontology_id,
            "ontology_name": ontology_name,
            "version": target_version,
            "active_version": target_version,
            "mutation_revision": mutation_revision,
            "source_root": str(source_root),
            "objects_count": objects_count,
            "operations_count": operations_count,
            "objects_index_path": objects_index_path,
            "operations_index_path": operations_index_path,
            "business_data_configured": bool(business_data_summary.get("configured")),
            "warnings": warnings,
            "normalization_warnings": normalization_warnings,
            "preview": preview,
        }, status_code=201)
