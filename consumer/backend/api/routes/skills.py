"""Skill management endpoints for reusable external skills."""

import shutil
import stat
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Optional

from flask import Blueprint, request, jsonify

from api.auth.auth import require_auth
from utils.logging import get_logger
from utils.skills import SkillLoader, get_skill_registry, invalidate_skill_registry
from workspace.workspaces import (
    get_workspace_skills_root,
    resolve_workspace_id,
    workspace_scope,
)
from api.routes.errors import not_found_response

logger = get_logger("routes.skills")

skills_bp = Blueprint("skills", __name__, url_prefix="/agentic")


# ============================================================================
# Helpers
# ============================================================================


def _is_safe_path(base_dir: Path, target_path: Path) -> bool:
    base_dir = base_dir.resolve(strict=False)
    target_path = target_path.resolve(strict=False)
    return base_dir == target_path or base_dir in target_path.parents


def _get_skills_dir() -> Path:
    return get_workspace_skills_root()


def _resolve_skill_dir_for_delete(skill_name: str) -> tuple[Optional[Path], Optional[str]]:
    normalized_name = str(skill_name or "").strip()
    if not normalized_name or normalized_name in {".", ".."}:
        return None, "Invalid skill name"
    if "/" in normalized_name or "\\" in normalized_name:
        return None, "Invalid skill name"

    skills_dir = _get_skills_dir().resolve(strict=False)
    skill_dir = (skills_dir / normalized_name).resolve(strict=False)
    if skill_dir == skills_dir or not _is_safe_path(skills_dir, skill_dir):
        return None, "Invalid skill name"

    return skill_dir, None


def _is_zip_symlink(member: zipfile.ZipInfo) -> bool:
    file_type = (member.external_attr >> 16) & stat.S_IFMT
    return file_type == stat.S_IFLNK


def _validate_skill_archive(archive_path: Path, extract_dir: Path) -> tuple[bool, str, Optional[str]]:
    """
    Validate uploaded skill archive.

    Requirements:
    - Must be zip or tar archive
    - Root must contain exactly one folder
    - Folder name is the skill name
    - Folder must contain SKILL.md with YAML frontmatter
    """
    suffix = archive_path.suffix.lower()
    if suffix == ".zip":
        try:
            with zipfile.ZipFile(archive_path, "r") as zf:
                for member in zf.infolist():
                    if _is_zip_symlink(member):
                        return False, "Archive contains unsupported symlink entries", None
                    member_path = extract_dir / member.filename
                    if not _is_safe_path(extract_dir, member_path):
                        return False, "Archive contains unsafe paths", None
                zf.extractall(extract_dir)
        except zipfile.BadZipFile:
            return False, "Invalid zip file", None
    elif suffix in (".tar", ".gz", ".tgz"):
        try:
            with tarfile.open(archive_path, "r:*") as tf:
                for member in tf.getmembers():
                    if member.islnk() or member.issym():
                        return False, "Archive contains unsupported symlink entries", None
                    member_path = extract_dir / member.name
                    if not _is_safe_path(extract_dir, member_path):
                        return False, "Archive contains unsafe paths", None
                tf.extractall(extract_dir)
        except tarfile.TarError:
            return False, "Invalid tar archive", None
    else:
        return False, "Unsupported archive format. Use .zip or .tar/.tar.gz/.tgz", None

    # Check root directory structure
    root_items = list(extract_dir.iterdir())
    root_items = [item for item in root_items if not item.name.startswith("_") and not item.name.startswith(".")]

    if len(root_items) != 1:
        return False, "Archive must contain exactly one root folder", None

    skill_folder = root_items[0]
    if not skill_folder.is_dir():
        return False, "Root item must be a folder (skill name)", None

    skill_name = skill_folder.name

    # Check for SKILL.md
    skill_md_path = skill_folder / "SKILL.md"
    if not skill_md_path.exists():
        return False, "Missing SKILL.md file in skill folder", None

    # Validate SKILL.md has valid YAML frontmatter
    skill = SkillLoader.load_skill(skill_folder)
    if skill is None:
        return False, "Invalid SKILL.md: must have valid YAML frontmatter with 'name' and 'description' fields", None

    # Verify skill name matches folder name
    if skill.name != skill_name:
        return False, "Skill name in SKILL.md must match folder name: '{}' != '{}'".format(skill.name, skill_name), None

    return True, "", skill_name

# ============================================================================
# Skill CRUD Endpoints
# ============================================================================


@skills_bp.route("/skills", methods=["GET"])
@require_auth()
def list_skills():
    """List all available skills."""
    try:
        workspace_id = resolve_workspace_id(request)
    except KeyError as exc:
        return not_found_response(exc)
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 400

    with workspace_scope(workspace_id):
        registry = get_skill_registry(workspace_id=workspace_id)

        skills_list = []
        for skill in registry.snapshot().values():
            skills_list.append({
                "name": skill.name,
                "description": skill.description,
                "file_path": skill.file_path,
                "allowed_tools": skill.allowed_tools,
            })

        return jsonify({"skills": skills_list})


@skills_bp.route("/skills/upload", methods=["POST"])
@require_auth(roles=["admin"])
def upload_skill():
    """
    Upload a new skill archive.

    Expected format:
    - Archive file (zip or tar/tar.gz)
    - Root contains single folder (skill name)
    - Folder contains SKILL.md with YAML frontmatter
    """
    try:
        workspace_id = resolve_workspace_id(request)
    except KeyError as exc:
        return not_found_response(exc)
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 400

    with workspace_scope(workspace_id):
        if "file" not in request.files:
            return jsonify({"error": "No file uploaded"}), 400

        file = request.files["file"]
        if file.filename == "":
            return jsonify({"error": "No file selected"}), 400

        # Check file extension
        filename = Path(file.filename or "").name
        if filename in ("", ".", ".."):
            return jsonify({"error": "Invalid file name"}), 400

        lower_name = filename.lower()
        if not (lower_name.endswith(".zip") or lower_name.endswith(".tar") or
                lower_name.endswith(".tar.gz") or lower_name.endswith(".tgz")):
            return jsonify({
                "error": "Unsupported file format. Use .zip, .tar, .tar.gz, or .tgz"
            }), 400

        # Create temporary directory for extraction
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            # Save uploaded file
            archive_path = temp_path / filename
            file.save(str(archive_path))

            # Create extraction directory
            extract_dir = temp_path / "extracted"
            extract_dir.mkdir()

            # Validate archive
            is_valid, error_msg, skill_name = _validate_skill_archive(archive_path, extract_dir)

            if not is_valid:
                return jsonify({"error": error_msg}), 400

            # Ensure skills directory exists
            skills_dir = _get_skills_dir()
            skills_dir.mkdir(parents=True, exist_ok=True)

            # Check if skill already exists
            target_dir = skills_dir / skill_name
            if target_dir.exists():
                # Remove existing skill
                shutil.rmtree(target_dir)
                logger.info("Replacing existing skill: {}".format(skill_name))

            # Move skill folder to skills directory
            source_dir = extract_dir / skill_name
            shutil.move(str(source_dir), str(target_dir))

            logger.info("Successfully uploaded skill: {}".format(skill_name))

            invalidate_skill_registry(workspace_id=workspace_id)
            registry = get_skill_registry(workspace_id=workspace_id)

            # Get skill info
            skill = registry.get_skill(skill_name)

            return jsonify({
                "success": True,
                "skill": {
                    "name": skill.name,
                    "description": skill.description,
                    "file_path": skill.file_path,
                }
            }), 201


@skills_bp.route("/skills/<skill_name>", methods=["DELETE"])
@require_auth(roles=["admin"])
def delete_skill(skill_name: str):
    """Delete a skill."""
    try:
        workspace_id = resolve_workspace_id(request)
    except KeyError as exc:
        return not_found_response(exc)
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 400

    with workspace_scope(workspace_id):
        skill_dir, validation_error = _resolve_skill_dir_for_delete(skill_name)
        if validation_error:
            return jsonify({"error": validation_error}), 400

        if not skill_dir or not skill_dir.is_dir():
            return jsonify({"error": "Skill not found"}), 404

        try:
            shutil.rmtree(skill_dir)
            logger.info("Deleted skill: {}".format(skill_name))
            invalidate_skill_registry(workspace_id=workspace_id)

            return jsonify({"success": True})
        except Exception as e:
            logger.exception("Failed to delete skill: {}".format(skill_name))
            return jsonify({"error": "Failed to delete skill '{}'".format(skill_name)}), 500


@skills_bp.route("/skills/<skill_name>", methods=["GET"])
@require_auth()
def get_skill(skill_name: str):
    """Get skill details."""
    try:
        workspace_id = resolve_workspace_id(request)
    except KeyError as exc:
        return not_found_response(exc)
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 400

    with workspace_scope(workspace_id):
        registry = get_skill_registry(workspace_id=workspace_id)
        skill = registry.get_skill(skill_name)

        if not skill:
            return jsonify({"error": "Skill not found"}), 404

        return jsonify({
            "name": skill.name,
            "description": skill.description,
            "content": skill.content,
            "file_path": skill.file_path,
            "allowed_tools": skill.allowed_tools,
        })
