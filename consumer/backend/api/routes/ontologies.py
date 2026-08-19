"""Ontology management endpoints: list, import, delete and file operations."""

import io
import re
import shutil
import stat
import tempfile
import unicodedata
import zipfile
from pathlib import Path
from typing import Optional

import yaml
from flask import Blueprint, jsonify, request, send_file
from werkzeug.datastructures import FileStorage
from config import LimitsConfig
from integration.platform.assets import export_ontology_snapshot_safely
from api.auth.auth import require_auth
from utils.logging import get_logger
from ontology.ontologies import (
    OntologyLoader,
    ensure_ontology_versions,
    get_ontology_registry,
    get_ontology_version_dir,
    invalidate_ontology_registry,
    is_valid_ontology_version,
    load_ontology_manifest_data,
    save_ontology_manifest_data,
    set_ontology_active_version,
)
from ontology.ontology_catalog import (
    find_asset_file,
    read_required_asset_text,
    supported_asset_formats_text,
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
    has_importable_package_layout,
    inspect_importable_package,
    normalize_imported_package,
    validate_imported_field_aliases,
    validate_imported_object_aliases,
)
from agent.tools.business_data.ontology_sources_store import (
    build_ontology_business_data_summary,
    clear_ontology_data_source_on_delete,
)
from persistence.chat_store import ChatStore
from ontology.ontology_graph import build_ontology_graph
from workspace.workspaces import (
    get_workspace_paths,
    get_workspace_ontologies_root,
    resolve_workspace_id,
    resolve_workspace_owner_or_admin_id,
    workspace_scope,
)
from api.routes.errors import not_found_response

logger = get_logger("routes.ontologies")

ontologies_bp = Blueprint("ontologies", __name__, url_prefix="/agentic")

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
    uploaded_file: FileStorage,
) -> tuple[Optional[bytes], Optional[str]]:
    try:
        uploaded_bytes = uploaded_file.stream.read(ONTOLOGY_MAX_ZIP_BYTES + 1)
    except Exception:
        return None, "Failed to read uploaded file"
    if len(uploaded_bytes) > ONTOLOGY_MAX_ZIP_BYTES:
        return None, "Zip file too large"
    return uploaded_bytes, None


ONTOLOGY_VERSIONED_SKIP_NAMES = {"manifest.yaml", "versions"}
ONTOLOGY_INTERNAL_MANIFEST_FIELDS = {
    "kind",
    "schema_version",
    "id",
    "name",
    "description",
    "version",
    "active_version",
    "versions",
    "language",
    "enabled",
    "scope",
}


class OntologyImportError(ValueError):
    """A stable client-visible import validation error."""


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
) -> tuple[Optional[Path], Optional[str]]:
    ontology_dir = _resolve_ontology_dir(ontology_id)
    if ontology_dir is None:
        return None, None

    normalized = (folder_name or "").strip().lower()
    if normalized not in ONTOLOGY_FOLDER_LABELS:
        return None, None

    version_dir = _resolve_existing_version_dir(ontology_dir, version)
    if version_dir is None:
        return None, None

    return version_dir / normalized, normalized


def _resolve_existing_version_dir(ontology_dir: Path, version: Optional[str] = None) -> Optional[Path]:
    try:
        manifest = load_ontology_manifest_data(ontology_dir)
    except Exception:
        return None

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
            ensure_ontology_versions(ontology_dir)
        except Exception:
            logger.exception("Failed to migrate legacy ontology directory: %s", ontology_dir)
            return None

    return get_ontology_version_dir(ontology_dir, target_version, create=False)


def _get_requested_version(default: str = "") -> str:
    return str(request.args.get("version") or request.form.get("version") or default or "").strip()


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
    return jsonify({"error": messages.get(exc.code, str(exc))}), status_code


def _manifest_for_download(ontology_dir: Path, version: str) -> str:
    manifest = load_ontology_manifest_data(ontology_dir)
    manifest["version"] = version
    manifest["active_version"] = version
    manifest["versions"] = [version]
    return yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False)


def _build_ontology_zip_buffer(ontology_dir: Path, version_dir: Path, *, version: str) -> io.BytesIO:
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


@ontologies_bp.route("/ontologies", methods=["GET"])
@require_auth()
def list_ontologies():
    try:
        workspace_id = resolve_workspace_id(request)
    except KeyError as exc:
        return not_found_response(exc)
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 400

    with workspace_scope(workspace_id):
        registry = get_ontology_registry(workspace_id=workspace_id)
        registry.refresh()

        ontologies = []
        for ontology_id, ontology in registry.ontologies.items():
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
                    "manifest_path": ontology.manifest_path,
                    "index_path": ontology.index_path,
                    "business_data": build_ontology_business_data_summary(
                        ontology.id,
                        workspace_id=workspace_id,
                    ),
                }
            )

        ontologies.sort(key=lambda item: item["id"])
        return jsonify({"ontologies": ontologies})


@ontologies_bp.route("/ontologies/<ontology_id>/status", methods=["PUT"])
@require_auth()
def update_ontology_status(ontology_id: str):
    try:
        workspace_id = resolve_workspace_owner_or_admin_id(request)
    except KeyError as exc:
        return not_found_response(exc)
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 400

    data = request.get_json() or {}
    if "enabled" not in data:
        return jsonify({"error": "`enabled` is required"}), 400
    if not isinstance(data.get("enabled"), bool):
        return jsonify({"error": "`enabled` must be a boolean"}), 400

    with workspace_scope(workspace_id):
        ontology_dir = _resolve_ontology_dir(ontology_id)
        if ontology_dir is None:
            return jsonify({"error": "Ontology not found"}), 404

        try:
            ontology = _set_ontology_enabled(ontology_dir, data["enabled"])
            registry = get_ontology_registry(workspace_id=workspace_id)
            registry.refresh()
        except Exception:
            logger.exception(
                "Failed to update ontology status: workspace_id=%s ontology_id=%s",
                workspace_id,
                ontology_id,
            )
            return jsonify({"error": "Failed to update ontology status"}), 500

        export_ontology_snapshot_safely(workspace_id=workspace_id)
        return jsonify({
            "success": True,
            "ontology_id": ontology.id,
            "enabled": ontology.enabled,
        })


@ontologies_bp.route("/ontologies/<ontology_id>/active-version", methods=["PUT"])
@require_auth()
def update_ontology_active_version(ontology_id: str):
    try:
        workspace_id = resolve_workspace_owner_or_admin_id(request)
    except KeyError as exc:
        return not_found_response(exc)
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 400

    data = request.get_json() or {}
    version = str(data.get("version") or "").strip()
    if not is_valid_ontology_version(version):
        return jsonify({"error": "version不合法，仅支持[A-Za-z0-9][A-Za-z0-9._-]*"}), 400

    with workspace_scope(workspace_id):
        ontology_dir = _resolve_ontology_dir(ontology_id)
        if ontology_dir is None:
            return jsonify({"error": "Ontology not found"}), 404

        try:
            ontology = set_ontology_active_version(ontology_dir, version)
            registry = get_ontology_registry(workspace_id=workspace_id)
            registry.refresh()
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception:
            logger.exception(
                "Failed to update ontology active version: workspace_id=%s ontology_id=%s version=%s",
                workspace_id,
                ontology_id,
                version,
            )
            return jsonify({"error": "Failed to update ontology active version"}), 500

        export_ontology_snapshot_safely(workspace_id=workspace_id)
        return jsonify({
            "success": True,
            "ontology_id": ontology.id,
            "active_version": ontology.active_version,
            "version": ontology.version,
            "versions": ontology.versions,
        })


@ontologies_bp.route("/ontologies/<ontology_id>", methods=["DELETE"])
@require_auth()
def delete_ontology(ontology_id: str):
    try:
        workspace_id = resolve_workspace_owner_or_admin_id(request)
    except KeyError as exc:
        return not_found_response(exc)
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 400

    with workspace_scope(workspace_id):
        ontology_dir = _resolve_ontology_dir(ontology_id)
        if ontology_dir is None:
            return jsonify({"error": "Ontology not found"}), 404

        try:
            shutil.rmtree(ontology_dir)
        except Exception:
            logger.exception(
                "Failed to delete ontology: workspace_id=%s ontology_id=%s path=%s",
                workspace_id,
                ontology_id,
                ontology_dir,
            )
            return jsonify({"error": "Failed to delete ontology"}), 500

        clear_ontology_data_source_on_delete(ontology_id, workspace_id=workspace_id)

        registry = get_ontology_registry(workspace_id=workspace_id)
        registry.refresh()

        logger.info(
            "Ontology deleted: workspace_id=%s ontology_id=%s path=%s",
            workspace_id,
            ontology_id,
            ontology_dir,
        )

        export_ontology_snapshot_safely(workspace_id=workspace_id)
        return jsonify({"success": True, "ontology_id": ontology_id})


@ontologies_bp.route("/ontologies/<ontology_id>/versions/<version>", methods=["DELETE"])
@require_auth()
def delete_ontology_version(ontology_id: str, version: str):
    try:
        workspace_id = resolve_workspace_owner_or_admin_id(request)
    except KeyError as exc:
        return not_found_response(exc)
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 400

    if not is_valid_ontology_version(version):
        payload, status_code = _version_error_payload(version)
        return jsonify(payload), status_code

    with workspace_scope(workspace_id):
        ontology_dir = _resolve_ontology_dir(ontology_id)
        if ontology_dir is None:
            return jsonify({"error": "Ontology not found"}), 404

        try:
            manifest = ensure_ontology_versions(ontology_dir)
        except Exception:
            logger.exception(
                "Failed to prepare ontology versions before delete: workspace_id=%s ontology_id=%s path=%s",
                workspace_id,
                ontology_id,
                ontology_dir,
            )
            return jsonify({"error": "Failed to prepare ontology versions"}), 500

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
            return jsonify({
                "error": "Ontology version not found",
                "ontology_id": ontology_id,
                "version": version,
                "active_version": active_version,
                "versions": versions,
            }), 404

        version_dir = get_ontology_version_dir(ontology_dir, version, create=False)
        if version_dir is None or not version_dir.is_dir():
            return jsonify({
                "error": "Ontology version not found",
                "ontology_id": ontology_id,
                "version": version,
                "active_version": active_version,
                "versions": versions,
            }), 404

        if version == active_version:
            return jsonify({
                "error": "不能删除当前激活版本，请先切换激活版本后再删除。",
                "conflict_type": "active_version",
                "ontology_id": ontology_id,
                "version": version,
                "active_version": active_version,
                "versions": versions,
            }), 409

        reference_count, referenced_sessions = _find_sessions_locking_ontology_version(
            workspace_id=workspace_id,
            ontology_id=ontology_id,
            version=version,
        )
        if reference_count > 0:
            return jsonify({
                "error": "Locked version is still referenced by existing chat sessions and cannot be deleted.",
                "conflict_type": "session_reference",
                "ontology_id": ontology_id,
                "version": version,
                "active_version": active_version,
                "versions": versions,
                "reference_count": reference_count,
                "referenced_sessions": referenced_sessions,
            }), 409

        if len(versions) <= 1:
            return jsonify({
                "error": "最后一个版本不能按版本删除，请删除整个本体。",
                "conflict_type": "last_version",
                "ontology_id": ontology_id,
                "version": version,
                "active_version": active_version,
                "versions": versions,
            }), 409

        updated_versions = [item for item in versions if item != version]

        try:
            shutil.rmtree(version_dir)
            manifest["version"] = active_version
            manifest["active_version"] = active_version
            manifest["versions"] = updated_versions
            save_ontology_manifest_data(ontology_dir, manifest)
        except Exception:
            logger.exception(
                "Failed to delete ontology version: workspace_id=%s ontology_id=%s version=%s path=%s",
                workspace_id,
                ontology_id,
                version,
                version_dir,
            )
            return jsonify({"error": "Failed to delete ontology version"}), 500

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
        return jsonify({
            "success": True,
            "ontology_id": ontology_id,
            "deleted_version": version,
            "active_version": active_version,
            "versions": updated_versions,
        })


@ontologies_bp.route("/ontologies/<ontology_id>/download", methods=["GET"])
@require_auth()
def download_ontology(ontology_id: str):
    try:
        workspace_id = resolve_workspace_id(request)
    except KeyError as exc:
        return not_found_response(exc)
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 400

    with workspace_scope(workspace_id):
        ontology_dir = _resolve_ontology_dir(ontology_id)
        if ontology_dir is None:
            return jsonify({"error": "Ontology not found"}), 404

        try:
            version = _get_requested_version()
            if version and not is_valid_ontology_version(version):
                payload, status_code = _version_error_payload(version)
                return jsonify(payload), status_code
            version_dir = _resolve_existing_version_dir(ontology_dir, version or None)
            if version_dir is None:
                payload, status_code = _version_error_payload(version)
                return jsonify(payload), status_code
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
            return jsonify({"error": "Failed to package ontology"}), 500

        logger.info(
            "Ontology downloaded: workspace_id=%s ontology_id=%s path=%s",
            workspace_id,
            ontology_id,
            ontology_dir,
        )
        return send_file(
            zip_buffer,
            mimetype="application/zip",
            as_attachment=True,
            download_name="{}-{}.zip".format(ontology_dir.name, resolved_version),
            max_age=0,
        )


@ontologies_bp.route("/ontologies/<ontology_id>/graph", methods=["GET"])
@require_auth()
def get_ontology_graph(ontology_id: str):
    try:
        workspace_id = resolve_workspace_id(request)
    except KeyError as exc:
        return not_found_response(exc)
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 400

    with workspace_scope(workspace_id):
        version = _get_requested_version()
        if version and not is_valid_ontology_version(version):
            payload, status_code = _version_error_payload(version)
            return jsonify(payload), status_code

        ontology_dir = _resolve_ontology_dir(ontology_id)
        if ontology_dir is None:
            return jsonify({"error": "Ontology not found"}), 404

        version_dir = _resolve_existing_version_dir(ontology_dir, version or None)
        if version_dir is None:
            payload, status_code = _version_error_payload(version)
            return jsonify(payload), status_code

        registry = get_ontology_registry(workspace_id=workspace_id)
        registry.refresh()
        ontology = registry.get_ontology(ontology_id)
        if ontology is None:
            return jsonify({"error": "Ontology not found"}), 404

        return jsonify(
            build_ontology_graph(
                ontology=ontology,
                ontology_dir=version_dir,
                version=version or ontology.active_version or ontology.version,
            )
        )


@ontologies_bp.route("/ontologies/<ontology_id>/tree", methods=["GET"])
@require_auth()
def get_ontology_tree(ontology_id: str):
    try:
        workspace_id = resolve_workspace_id(request)
    except KeyError as exc:
        return not_found_response(exc)
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 400

    with workspace_scope(workspace_id):
        version = _get_requested_version()
        if version and not is_valid_ontology_version(version):
            payload, status_code = _version_error_payload(version)
            return jsonify(payload), status_code
        ontology_dir = _resolve_ontology_dir(ontology_id)
        if ontology_dir is None:
            return jsonify({"error": "Ontology not found"}), 404
        version_dir = _resolve_existing_version_dir(ontology_dir, version or None)
        if version_dir is None:
            return jsonify({"error": "Ontology version not found"}), 404

        try:
            entries = build_package_tree(version_dir, trusted_root=ontology_dir)
        except PackageBrowserError as exc:
            return _package_browser_error_response(exc)
        return jsonify(
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


@ontologies_bp.route("/ontologies/<ontology_id>/raw-file", methods=["GET"])
@require_auth()
def get_ontology_raw_file(ontology_id: str):
    try:
        workspace_id = resolve_workspace_id(request)
    except KeyError as exc:
        return not_found_response(exc)
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 400

    with workspace_scope(workspace_id):
        version = _get_requested_version()
        if version and not is_valid_ontology_version(version):
            payload, status_code = _version_error_payload(version)
            return jsonify(payload), status_code
        ontology_dir = _resolve_ontology_dir(ontology_id)
        if ontology_dir is None:
            return jsonify({"error": "Ontology not found"}), 404
        version_dir = _resolve_existing_version_dir(ontology_dir, version or None)
        if version_dir is None:
            return jsonify({"error": "Ontology version not found"}), 404

        try:
            file_payload = read_package_text(
                version_dir,
                request.args.get("path", ""),
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
        return jsonify(
            {
                "ontology_id": ontology_id,
                "version": version,
                **file_payload,
            }
        )


@ontologies_bp.route("/ontologies/<ontology_id>/files", methods=["GET"])
@require_auth()
def list_ontology_files(ontology_id: str):
    try:
        workspace_id = resolve_workspace_id(request)
    except KeyError as exc:
        return not_found_response(exc)
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 400

    with workspace_scope(workspace_id):
        version = _get_requested_version()
        if version and not is_valid_ontology_version(version):
            payload, status_code = _version_error_payload(version)
            return jsonify(payload), status_code
        ontology_dir = _resolve_ontology_dir(ontology_id)
        folder_path, folder_key = _resolve_ontology_folder(ontology_id, request.args.get("folder", ""), version or None)
        if folder_path is None or folder_key is None:
            return jsonify({"error": "Invalid ontology or folder. Use 'operations', 'objects', or 'assets'."}), 400

        if not folder_path.exists():
            return jsonify({
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

        return jsonify({
            "folder": folder_key,
            "label": ONTOLOGY_FOLDER_LABELS[folder_key],
            "version": version,
            "files": files,
        })


@ontologies_bp.route("/ontologies/<ontology_id>/file", methods=["GET"])
@require_auth()
def get_ontology_file(ontology_id: str):
    try:
        workspace_id = resolve_workspace_id(request)
    except KeyError as exc:
        return not_found_response(exc)
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 400

    with workspace_scope(workspace_id):
        version = _get_requested_version()
        if version and not is_valid_ontology_version(version):
            payload, status_code = _version_error_payload(version)
            return jsonify(payload), status_code
        ontology_dir = _resolve_ontology_dir(ontology_id)
        folder_path, folder_key = _resolve_ontology_folder(ontology_id, request.args.get("folder", ""), version or None)
        if folder_path is None or folder_key is None:
            return jsonify({"error": "Invalid ontology or folder. Use 'operations', 'objects', or 'assets'."}), 400

        if folder_key == "assets":
            safe_name = _normalize_asset_key(request.args.get("name", ""))
            if not safe_name:
                return jsonify({"error": "Invalid asset key"}), 400
            version_dir = folder_path.parent
            file_path, asset_error = find_asset_file(
                version_dir,
                safe_name,
                trusted_root=ontology_dir,
            )
            if asset_error:
                return jsonify({"error": asset_error}), 400
            assert file_path is not None
            content, read_error = read_required_asset_text(file_path, "asset '{}'".format(safe_name))
            if read_error:
                return jsonify({"error": read_error}), 400
            return jsonify({
                "folder": folder_key,
                "label": ONTOLOGY_FOLDER_LABELS[folder_key],
                "version": version,
                "name": safe_name,
                "content": content,
                "read_only": True,
                "supported_formats": supported_asset_formats_text(),
            })

        safe_name = _sanitize_md_filename(request.args.get("name", ""))
        if not safe_name:
            return jsonify({"error": "Invalid file name. Only .md files are allowed."}), 400

        try:
            file_payload = read_package_text(
                folder_path,
                safe_name,
                allowed_extensions={".md"},
                trusted_root=ontology_dir,
            )
        except PackageBrowserError as exc:
            return _package_browser_error_response(exc)

        return jsonify({
            "folder": folder_key,
            "label": ONTOLOGY_FOLDER_LABELS[folder_key],
            "version": version,
            "name": safe_name,
            "content": file_payload["content"],
        })


@ontologies_bp.route("/ontologies/<ontology_id>/file", methods=["PUT"])
@require_auth()
def update_ontology_file(ontology_id: str):
    try:
        workspace_id = resolve_workspace_owner_or_admin_id(request)
    except KeyError as exc:
        return not_found_response(exc)
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 400

    with workspace_scope(workspace_id):
        data = request.get_json() or {}
        version = _get_json_requested_version(data)
        if version and not is_valid_ontology_version(version):
            payload, status_code = _version_error_payload(version)
            return jsonify(payload), status_code
        folder_path, folder_key = _resolve_ontology_folder(ontology_id, data.get("folder", ""), version or None)
        if folder_path is None or folder_key is None:
            return jsonify({"error": "Invalid ontology or folder. Use 'operations', 'objects', or 'assets'."}), 400
        if folder_key == "assets":
            return jsonify({"error": "Asset files are read-only in this editor."}), 400

        safe_name = _sanitize_md_filename(data.get("name", ""))
        if not safe_name:
            return jsonify({"error": "Invalid file name. Only .md files are allowed."}), 400

        content = data.get("content")
        if not isinstance(content, str):
            return jsonify({"error": "Content must be a string"}), 400

        validation_error = _validate_md_limits(content, folder_key)
        if validation_error:
            return jsonify({"error": validation_error}), 400

        file_path = folder_path / safe_name
        if not _is_safe_path(folder_path, file_path):
            return jsonify({"error": "Unsafe file path"}), 400
        if not file_path.exists() or not file_path.is_file():
            return jsonify({"error": "File not found"}), 404

        try:
            file_path.write_text(content, encoding="utf-8")
        except Exception:
            logger.exception("Failed to update ontology file: %s", file_path)
            return jsonify({"error": "Failed to save file"}), 500

        return jsonify({
            "success": True,
            "folder": folder_key,
            "label": ONTOLOGY_FOLDER_LABELS[folder_key],
            "version": version,
            "name": safe_name,
        })


@ontologies_bp.route("/ontologies/<ontology_id>/upload", methods=["POST"])
@require_auth()
def upload_ontology_file(ontology_id: str):
    try:
        workspace_id = resolve_workspace_owner_or_admin_id(request)
    except KeyError as exc:
        return not_found_response(exc)
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 400

    with workspace_scope(workspace_id):
        version = _get_requested_version()
        if version and not is_valid_ontology_version(version):
            payload, status_code = _version_error_payload(version)
            return jsonify(payload), status_code
        folder_path, folder_key = _resolve_ontology_folder(ontology_id, request.form.get("folder", ""), version or None)
        if folder_path is None or folder_key is None:
            return jsonify({"error": "Invalid ontology or folder. Use 'operations', 'objects', or 'assets'."}), 400
        if folder_key == "assets":
            return jsonify({"error": "Asset upload is not supported from this editor."}), 400

        if "file" not in request.files:
            return jsonify({"error": "No file uploaded"}), 400

        uploaded_file = request.files["file"]
        safe_name = _sanitize_md_filename(uploaded_file.filename or "")
        if not safe_name:
            return jsonify({"error": "Invalid file name. Only .md files are allowed."}), 400

        try:
            uploaded_bytes = uploaded_file.read()
        except Exception:
            return jsonify({"error": "Failed to read uploaded file"}), 400

        try:
            content = uploaded_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return jsonify({"error": "File encoding must be UTF-8"}), 400

        validation_error = _validate_md_limits(content, folder_key)
        if validation_error:
            return jsonify({"error": validation_error}), 400

        folder_path.mkdir(parents=True, exist_ok=True)
        target_path = folder_path / safe_name
        if not _is_safe_path(folder_path, target_path):
            return jsonify({"error": "Unsafe file path"}), 400

        count_error = _validate_new_markdown_file_count(folder_path, folder_key, target_path)
        if count_error:
            return jsonify({"error": count_error}), 400

        existed = target_path.exists()

        try:
            target_path.write_text(content, encoding="utf-8")
        except Exception:
            logger.exception("Failed to upload ontology file: %s", target_path)
            return jsonify({"error": "Failed to upload file"}), 500

        return jsonify({
            "success": True,
            "folder": folder_key,
            "label": ONTOLOGY_FOLDER_LABELS[folder_key],
            "version": version,
            "name": safe_name,
            "overwritten": existed,
        }), 201



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
    if (
        not normalized
        or "\x00" in normalized
        or normalized.startswith("/")
        or re.match(r"^[A-Za-z]:", normalized)
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


def _is_valid_ontology_id(ontology_id: str) -> bool:
    return bool(ontology_id and ONTOLOGY_ID_PATTERN.match(ontology_id))


def _set_ontology_enabled(ontology_dir: Path, enabled: bool):
    manifest_path = ontology_dir / "manifest.yaml"
    if not manifest_path.is_file():
        raise ValueError("Ontology manifest not found")

    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    if not isinstance(manifest, dict):
        raise ValueError("Ontology manifest must be a mapping")

    manifest["enabled"] = bool(enabled)
    manifest_path.write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    ontology = OntologyLoader.load_ontology(ontology_dir)
    if ontology is None:
        raise ValueError("Failed to reload ontology manifest")
    return ontology


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


def _copy_versioned_package_contents(source_root: Path, target_root: Path) -> None:
    if target_root.exists():
        shutil.rmtree(target_root)
    target_root.mkdir(parents=True, exist_ok=True)

    for child in source_root.iterdir():
        if child.name in ONTOLOGY_VERSIONED_SKIP_NAMES:
            continue
        target_path = target_root / child.name
        if not _is_safe_path(target_root, target_path):
            raise ValueError("Unsafe target path")
        if child.is_dir():
            shutil.copytree(child, target_path)
        else:
            shutil.copy2(child, target_path)


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
    if existed:
        manifest = ensure_ontology_versions(ontology_dir)
    else:
        source_manifest_path = source_root / "manifest.yaml"
        if source_manifest_path.is_file():
            source_manifest = yaml.safe_load(source_manifest_path.read_text(encoding="utf-8")) or {}
            if not isinstance(source_manifest, dict):
                source_manifest = {}
            manifest = {
                key: value
                for key, value in source_manifest.items()
                if key in ONTOLOGY_INTERNAL_MANIFEST_FIELDS
            }
        else:
            manifest = {}
        manifest.update({
            "kind": "ontology-package",
            "schema_version": 0.1,
            "id": ontology_id,
            "name": ontology_name,
            "description": ontology_description,
        })
        manifest.setdefault("language", "zh-CN")
        manifest.setdefault("enabled", True)
        manifest.setdefault("scope", [])

    manifest["kind"] = "ontology-package"
    manifest["schema_version"] = 0.1
    versions = [
        str(item).strip()
        for item in (manifest.get("versions") or [])
        if str(item or "").strip()
    ]
    if target_version not in versions:
        versions.append(target_version)
    manifest["version"] = target_version
    manifest["active_version"] = target_version
    manifest["versions"] = versions
    if "enabled" not in manifest:
        manifest["enabled"] = True
    return {
        "kind": "ontology-package",
        "schema_version": 0.1,
        **{
            key: value
            for key, value in manifest.items()
            if key not in {"kind", "schema_version"}
        },
    }


def _install_ontology_version_package(
    *,
    source_root: Path,
    ontology_dir: Path,
    ontology_id: str,
    ontology_name: str,
    ontology_description: str,
    target_version: str,
    existed: bool,
) -> Path:
    ontology_dir.mkdir(parents=True, exist_ok=True)
    manifest = _prepare_shared_manifest(
        ontology_dir=ontology_dir,
        source_root=source_root,
        ontology_id=ontology_id,
        ontology_name=ontology_name,
        ontology_description=ontology_description,
        target_version=target_version,
        existed=existed,
    )

    version_dir = ontology_dir / "versions" / target_version
    _copy_versioned_package_contents(source_root, version_dir)
    save_ontology_manifest_data(ontology_dir, manifest)
    return version_dir


def _optional_path_str(path: Path) -> Optional[str]:
    return str(path) if path.exists() else None


@ontologies_bp.route("/ontologies/import-zip", methods=["POST"])
@require_auth()
def import_ontology_zip_with_auto_create():
    """Import ontology zip and auto-create ontology when none is selected."""
    try:
        workspace_id = resolve_workspace_owner_or_admin_id(request)
    except KeyError as exc:
        return not_found_response(exc)
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 400

    with workspace_scope(workspace_id):
        if "file" not in request.files:
            return jsonify({"error": "No file uploaded"}), 400

        uploaded_file = request.files["file"]
        filename = (uploaded_file.filename or "").strip()
        if not filename.lower().endswith(".zip"):
            return jsonify({"error": "Only .zip package is supported"}), 400

        uploaded_bytes, upload_error = _read_limited_zip_upload(uploaded_file)
        if upload_error:
            return jsonify({"error": upload_error}), 400
        assert uploaded_bytes is not None

        if not uploaded_bytes:
            return jsonify({"error": "Uploaded file is empty"}), 400

        with tempfile.TemporaryDirectory(prefix="ontology-import-auto-") as temp_dir:
            extract_root = Path(temp_dir) / "unzipped"
            extract_root.mkdir(parents=True, exist_ok=True)

            extraction_error = _safe_extract_zip(uploaded_bytes, extract_root)
            if extraction_error:
                return jsonify({"error": extraction_error}), 400

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
                return jsonify({"error": str(exc)}), 400
            if converted_root_info is not None:
                (
                    source_root,
                    ontology_id,
                    ontology_name,
                    ontology_description,
                    parsed_version,
                    _,
                ) = converted_root_info
            else:
                return jsonify({
                    "error": (
                        "Zip中未找到可导入的v0.1本体包。"
                        "请将源格式包转换为v0.1后重新上传。"
                    )
                }), 400
            if not ontology_id:
                return jsonify({"error": "manifest.yaml缺少合法的ontology id，无法导入"}), 400
            if not _is_valid_ontology_id(ontology_id):
                return jsonify({
                    "error": (
                        "ontology id不合法，仅支持字母数字下划线短横线，"
                        "且需字母或数字开头"
                    ),
                    "ontology_id": ontology_id,
                }), 400

            overwrite_flag = str(request.form.get("overwrite", "")).strip().lower() in {
                "1", "true", "yes", "y", "on"
            }
            action = str(request.form.get("action") or request.form.get("import_action") or "").strip()
            requested_new_version = str(request.form.get("new_version") or request.form.get("version") or "").strip()
            parsed_version = str(parsed_version or "").strip()
            if parsed_version and not is_valid_ontology_version(parsed_version):
                return jsonify({
                    "error": "version不合法，仅支持[A-Za-z0-9][A-Za-z0-9._-]*",
                    "ontology_id": ontology_id,
                    "version": parsed_version,
                }), 400
            if requested_new_version and not is_valid_ontology_version(requested_new_version):
                return jsonify({
                    "error": "new_version不合法，仅支持[A-Za-z0-9][A-Za-z0-9._-]*",
                    "ontology_id": ontology_id,
                    "version": requested_new_version,
                }), 400

            ontologies_dir = _get_ontologies_dir()
            ontologies_dir.mkdir(parents=True, exist_ok=True)

            ontology_dir = ontologies_dir / ontology_id
            existed = ontology_dir.is_dir()
            active_version = ""
            existing_versions: list[str] = []
            if existed:
                try:
                    existing_manifest = ensure_ontology_versions(ontology_dir)
                except Exception:
                    logger.exception("Failed to prepare existing ontology versions: %s", ontology_dir)
                    return jsonify({"error": "Failed to prepare existing ontology versions"}), 500
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
                return jsonify({"error": count_error}), 400

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
                        return jsonify({
                            "error": f"本体 {ontology_id} 已存在，上传包未解析到版本号，请选择覆盖当前激活版本或新增版本号。",
                            "requires_confirmation": True,
                            "conflict_type": "missing_version_existing",
                            "ontology_id": ontology_id,
                            "ontology_name": ontology_name,
                            "active_version": active_version,
                            "versions": existing_versions,
                        }), 409
                elif parsed_version in existing_versions:
                    if action in {"overwrite_matching", "overwrite_version"} or overwrite_flag:
                        target_version = parsed_version
                        overwritten = True
                    elif action == "create_version":
                        target_version = requested_new_version
                    else:
                        return jsonify({
                            "error": f"本体 {ontology_id} 的版本 {parsed_version} 已存在，请选择覆盖匹配版本或新增版本号。",
                            "requires_confirmation": True,
                            "conflict_type": "version_exists",
                            "ontology_id": ontology_id,
                            "ontology_name": ontology_name,
                            "version": parsed_version,
                            "active_version": active_version,
                            "versions": existing_versions,
                        }), 409
                else:
                    target_version = parsed_version

            if not target_version or not is_valid_ontology_version(target_version):
                return jsonify({
                    "error": "新增版本号不合法，仅支持[A-Za-z0-9][A-Za-z0-9._-]*",
                    "ontology_id": ontology_id,
                }), 400
            if existed and target_version in existing_versions and action == "create_version":
                return jsonify({
                    "error": f"版本 {target_version} 已存在，请换一个版本号。",
                    "ontology_id": ontology_id,
                    "version": target_version,
                }), 409
            if existed and target_version in existing_versions and parsed_version not in existing_versions and not overwritten:
                return jsonify({
                    "error": f"版本 {target_version} 已存在，请换一个版本号。",
                    "ontology_id": ontology_id,
                    "version": target_version,
                }), 409

            try:
                # Process v0.1 conformant package
                with tempfile.TemporaryDirectory(prefix="ontology-normalized-") as normalized_temp:
                    normalized_root = Path(normalized_temp) / ontology_id
                    normalization = normalize_imported_package(source_root, normalized_root)
                    normalization_warnings = list(normalization.warnings)
                    validation_error = _validate_operations_tree_limits(normalization.root)
                    if validation_error:
                        logger.warning("Import rejected by tree limits: ontology_id=%s error=%s", ontology_id, validation_error)
                        return jsonify({"error": validation_error}), 400
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
                        logger.warning("Import rejected by alias validation: ontology_id=%s errors=%s", ontology_id, alias_errors)
                        return jsonify({"error": "\n".join(alias_errors)}), 400
                    warnings["object_aliases"] = list(alias_validation.warnings)
                    warnings["field_aliases"] = list(field_alias_validation.warnings)
                    preview = _build_import_preview(source_root, normalization.root)
                    version_dir = _install_ontology_version_package(
                        source_root=normalization.root,
                        ontology_dir=ontology_dir,
                        ontology_id=ontology_id,
                        ontology_name=ontology_name,
                        ontology_description=ontology_description,
                        target_version=target_version,
                        existed=existed,
                    )
                objects_count = _count_markdown_files(version_dir / "objects")
                operations_count = _count_markdown_files(version_dir / "operations")
                objects_index_path = _optional_path_str(version_dir / "objects" / "index.md")
                operations_index_path = _optional_path_str(version_dir / "operations" / "index.md")
            except PackageNormalizationError as exc:
                logger.warning("Import rejected by normalization: ontology_id=%s error=%s", ontology_id, exc)
                return jsonify({"error": str(exc)}), 400
            except Exception:
                logger.exception(
                    "Failed to import ontology from uploaded zip: workspace_id=%s filename=%s ontology_id=%s source_root=%s",
                    workspace_id,
                    filename,
                    ontology_id,
                    source_root,
                )
                return jsonify({"error": "Failed to parse uploaded zip"}), 500

        export_ontology_snapshot_safely(workspace_id=workspace_id)
        invalidate_ontology_registry(workspace_id=workspace_id)
        business_data_summary = build_ontology_business_data_summary(
            ontology_id,
            workspace_id=workspace_id,
        )
        logger.info(
            "Import zip auto-create success: workspace_id=%s ontology_id=%s created=%s overwritten=%s objects_count=%s operations_count=%s warnings=%s",
            workspace_id,
            ontology_id,
            (not existed),
            overwritten,
            objects_count,
            operations_count,
            {key: len(value) for key, value in warnings.items()},
        )

        return jsonify({
            "success": True,
            "created": not existed,
            "overwritten": overwritten,
            "ontology_id": ontology_id,
            "ontology_name": ontology_name,
            "version": target_version,
            "active_version": target_version,
            "source_root": str(source_root),
            "objects_count": objects_count,
            "operations_count": operations_count,
            "objects_index_path": objects_index_path,
            "operations_index_path": operations_index_path,
            "business_data_configured": bool(business_data_summary.get("configured")),
            "warnings": warnings,
            "normalization_warnings": normalization_warnings,
            "preview": preview,
        }), 201
