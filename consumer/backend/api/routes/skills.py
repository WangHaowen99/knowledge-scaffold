"""Skill management endpoints for reusable external skills."""

import io
import re
import shutil
import stat
import tarfile
import tempfile
import unicodedata
import zipfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile

from api.dependencies import (
    require_admin_workspace_id,
    require_workspace_id,
    run_in_runtime_pool,
)
from utils.logging import get_logger
from utils.skills import SkillLoader, get_skill_registry, invalidate_skill_registry
from workspace.workspaces import get_workspace_skills_root

logger = get_logger("routes.skills")

skills_router = APIRouter(prefix="/agentic")

SKILL_ARCHIVE_MAX_BYTES = 50 * 1024 * 1024
SKILL_ARCHIVE_MAX_EXPANDED_BYTES = 200 * 1024 * 1024
SKILL_ARCHIVE_MAX_MEMBERS = 10_000
SKILL_ARCHIVE_MAX_MEMBER_BYTES = 50 * 1024 * 1024
SKILL_ARCHIVE_MAX_COMPRESSION_RATIO = 100
SKILL_ARCHIVE_MAX_PATH_DEPTH = 32
SKILL_ARCHIVE_MAX_PATH_BYTES = 4096
_ARCHIVE_COPY_CHUNK_BYTES = 1024 * 1024


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


def _skill_archive_parts(name: str, *, is_directory: bool) -> tuple[str, ...]:
    normalized = str(name or "").replace("\\", "/")
    if is_directory:
        normalized = normalized.rstrip("/")
    if (
        not normalized
        or "\x00" in normalized
        or normalized.startswith("/")
        or re.match(r"^[A-Za-z]:", normalized)
        or len(normalized.encode("utf-8")) > SKILL_ARCHIVE_MAX_PATH_BYTES
    ):
        raise ValueError("Archive contains an unsafe path")
    parts = tuple(normalized.split("/"))
    if (
        len(parts) > SKILL_ARCHIVE_MAX_PATH_DEPTH
        or any(not part or part in {".", ".."} for part in parts)
    ):
        raise ValueError("Archive contains an unsafe path")
    return parts


def _register_skill_archive_path(
    targets: dict[str, str],
    parts: tuple[str, ...],
    *,
    is_directory: bool,
) -> None:
    def canonical(value: tuple[str, ...]) -> str:
        return "/".join(unicodedata.normalize("NFC", part).casefold() for part in value)

    target = canonical(parts)
    if target in targets or any(
        targets.get(canonical(parts[:index])) == "file"
        for index in range(1, len(parts))
    ):
        raise ValueError("Archive contains duplicate or colliding paths")
    if not is_directory and any(existing.startswith(target + "/") for existing in targets):
        raise ValueError("Archive contains duplicate or colliding paths")
    targets[target] = "directory" if is_directory else "file"


def _check_skill_archive_budget(
    *,
    member_count: int,
    member_bytes: int,
    total_bytes: int,
) -> None:
    if member_count > SKILL_ARCHIVE_MAX_MEMBERS:
        raise ValueError("Archive contains too many entries")
    if member_bytes < 0 or member_bytes > SKILL_ARCHIVE_MAX_MEMBER_BYTES:
        raise ValueError("Archive member is too large")
    if total_bytes > SKILL_ARCHIVE_MAX_EXPANDED_BYTES:
        raise ValueError("Archive expanded content is too large")


def _check_skill_archive_ratio(expanded_bytes: int, compressed_bytes: int) -> None:
    if expanded_bytes > max(compressed_bytes, 1) * SKILL_ARCHIVE_MAX_COMPRESSION_RATIO:
        raise ValueError("Archive compression ratio is too high")


def _copy_skill_archive_member(
    source,
    *,
    expected_bytes: int,
    total_bytes: int,
    destination=None,
) -> tuple[bytes, int]:
    content = bytearray()
    member_bytes = 0
    while True:
        chunk = source.read(_ARCHIVE_COPY_CHUNK_BYTES)
        if not chunk:
            break
        member_bytes += len(chunk)
        total_bytes += len(chunk)
        _check_skill_archive_budget(
            member_count=0,
            member_bytes=member_bytes,
            total_bytes=total_bytes,
        )
        if destination is None:
            content.extend(chunk)
        else:
            destination.write(chunk)
    if member_bytes != expected_bytes:
        raise ValueError("Archive member size does not match its header")
    return bytes(content), total_bytes


def _validated_skill_zip_members(
    archive: zipfile.ZipFile,
) -> list[tuple[zipfile.ZipInfo, tuple[str, ...], bool]]:
    members = archive.infolist()
    targets: dict[str, str] = {}
    validated = []
    total_bytes = 0
    total_compressed_bytes = 0
    for member_count, member in enumerate(members, start=1):
        is_directory = member.is_dir() or member.filename.endswith("/")
        parts = _skill_archive_parts(member.filename, is_directory=is_directory)
        unix_mode = int(member.external_attr or 0) >> 16
        file_type = stat.S_IFMT(unix_mode)
        if file_type and not (
            (is_directory and stat.S_ISDIR(unix_mode))
            or (not is_directory and stat.S_ISREG(unix_mode))
        ):
            raise ValueError("Archive contains a link or special file")
        _register_skill_archive_path(targets, parts, is_directory=is_directory)
        member_bytes = 0 if is_directory else int(member.file_size or 0)
        compressed_bytes = 0 if is_directory else int(member.compress_size or 0)
        total_bytes += member_bytes
        total_compressed_bytes += compressed_bytes
        _check_skill_archive_budget(
            member_count=member_count,
            member_bytes=member_bytes,
            total_bytes=total_bytes,
        )
        _check_skill_archive_ratio(member_bytes, compressed_bytes)
        validated.append((member, parts, is_directory))
    _check_skill_archive_ratio(total_bytes, total_compressed_bytes)
    return validated


def _extract_skill_zip(archive_bytes: bytes, extract_dir: Path) -> None:
    if len(archive_bytes) > SKILL_ARCHIVE_MAX_BYTES:
        raise ValueError("Archive file is too large")
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        members = _validated_skill_zip_members(archive)
        actual_total_bytes = 0
        for member, parts, is_directory in members:
            target_path = extract_dir.joinpath(*parts)
            if not _is_safe_path(extract_dir, target_path):
                raise ValueError("Archive contains an unsafe path")
            if is_directory:
                target_path.mkdir(parents=True, exist_ok=True)
                continue
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target_path.open("wb") as destination:
                _, actual_total_bytes = _copy_skill_archive_member(
                    source,
                    expected_bytes=int(member.file_size or 0),
                    total_bytes=actual_total_bytes,
                    destination=destination,
                )


def _read_skill_zip_text_members(archive_bytes: bytes) -> dict[str, str]:
    if len(archive_bytes) > SKILL_ARCHIVE_MAX_BYTES:
        raise ValueError("Archive file is too large")
    result: dict[str, str] = {}
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        members = _validated_skill_zip_members(archive)
        actual_total_bytes = 0
        for member, _parts, is_directory in members:
            if is_directory:
                continue
            with archive.open(member) as source:
                content, actual_total_bytes = _copy_skill_archive_member(
                    source,
                    expected_bytes=int(member.file_size or 0),
                    total_bytes=actual_total_bytes,
                )
            try:
                result[member.filename] = content.decode("utf-8")
            except UnicodeDecodeError:
                continue
    return result


def _extract_skill_tar(archive_path: Path, extract_dir: Path) -> None:
    archive_bytes = archive_path.stat().st_size
    if archive_bytes > SKILL_ARCHIVE_MAX_BYTES:
        raise ValueError("Archive file is too large")
    targets: dict[str, str] = {}
    total_bytes = 0
    with tarfile.open(archive_path, "r:*") as archive:
        for member_count, member in enumerate(archive, start=1):
            if not (member.isdir() or member.isfile()):
                raise ValueError("Archive contains a link or special file")
            parts = _skill_archive_parts(member.name, is_directory=member.isdir())
            _register_skill_archive_path(targets, parts, is_directory=member.isdir())
            member_bytes = int(member.size or 0)
            total_bytes += member_bytes
            _check_skill_archive_budget(
                member_count=member_count,
                member_bytes=member_bytes,
                total_bytes=total_bytes,
            )
    _check_skill_archive_ratio(total_bytes, archive_bytes)

    actual_total_bytes = 0
    with tarfile.open(archive_path, "r:*") as archive:
        for member in archive:
            parts = _skill_archive_parts(member.name, is_directory=member.isdir())
            target_path = extract_dir.joinpath(*parts)
            if not _is_safe_path(extract_dir, target_path):
                raise ValueError("Archive contains an unsafe path")
            if member.isdir():
                target_path.mkdir(parents=True, exist_ok=True)
                continue
            source = archive.extractfile(member)
            if source is None:
                raise ValueError("Archive member cannot be read")
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with source, target_path.open("wb") as destination:
                _, actual_total_bytes = _copy_skill_archive_member(
                    source,
                    expected_bytes=int(member.size or 0),
                    total_bytes=actual_total_bytes,
                    destination=destination,
                )


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
            _extract_skill_zip(archive_path.read_bytes(), extract_dir)
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            return False, str(exc) or "Invalid zip file", None
    elif suffix in (".tar", ".gz", ".tgz"):
        try:
            _extract_skill_tar(archive_path, extract_dir)
        except (OSError, ValueError, tarfile.TarError) as exc:
            return False, str(exc) or "Invalid tar archive", None
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


@skills_router.get("/skills")
async def list_skills(
    request: Request,
    workspace_id: str = Depends(require_workspace_id),
):
    """List all available skills."""
    return await run_in_runtime_pool(
        request,
        "short_io",
        _list_skills,
        workspace_id,
    )


def _list_skills(workspace_id: str):
    registry = get_skill_registry(workspace_id=workspace_id)
    skills_list = [
        {
            "name": skill.name,
            "description": skill.description,
            "file_path": skill.file_path,
            "allowed_tools": skill.allowed_tools,
        }
        for skill in registry.snapshot().values()
    ]
    return {"skills": skills_list}


def _install_skill_archive(
    *,
    workspace_id: str,
    filename: str,
    archive_bytes: bytes,
):
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        archive_path = temp_path / filename
        archive_path.write_bytes(archive_bytes)
        extract_dir = temp_path / "extracted"
        extract_dir.mkdir()

        is_valid, error_msg, skill_name = _validate_skill_archive(
            archive_path,
            extract_dir,
        )
        if not is_valid:
            return JSONResponse({"error": error_msg}, status_code=400)

        skills_dir = _get_skills_dir()
        skills_dir.mkdir(parents=True, exist_ok=True)
        target_dir = skills_dir / skill_name
        if target_dir.exists():
            shutil.rmtree(target_dir)
            logger.info("Replacing existing skill: %s", skill_name)

        source_dir = extract_dir / skill_name
        shutil.move(str(source_dir), str(target_dir))
        logger.info("Successfully uploaded skill: %s", skill_name)

        invalidate_skill_registry(workspace_id=workspace_id)
        skill = get_skill_registry(workspace_id=workspace_id).get_skill(skill_name)
        return JSONResponse(
            {
                "success": True,
                "skill": {
                    "name": skill.name,
                    "description": skill.description,
                    "file_path": skill.file_path,
                },
            },
            status_code=201,
        )


@skills_router.post("/skills/upload")
async def upload_skill(
    request: Request,
    workspace_id: str = Depends(require_admin_workspace_id),
):
    """
    Upload a new skill archive.

    Expected format:
    - Archive file (zip or tar/tar.gz)
    - Root contains single folder (skill name)
    - Folder contains SKILL.md with YAML frontmatter
    """
    form = await request.form()
    upload = form.get("file")
    if not isinstance(upload, UploadFile):
        return JSONResponse({"error": "No file uploaded"}, status_code=400)
    if upload.filename == "":
        return JSONResponse({"error": "No file selected"}, status_code=400)

    filename = Path(upload.filename or "").name
    if filename in ("", ".", ".."):
        return JSONResponse({"error": "Invalid file name"}, status_code=400)

    lower_name = filename.lower()
    if not (
        lower_name.endswith(".zip")
        or lower_name.endswith(".tar")
        or lower_name.endswith(".tar.gz")
        or lower_name.endswith(".tgz")
    ):
        return JSONResponse(
            {
                "error": (
                    "Unsupported file format. Use .zip, .tar, .tar.gz, or .tgz"
                )
            },
            status_code=400,
        )

    archive_bytes = await upload.read(SKILL_ARCHIVE_MAX_BYTES + 1)
    if len(archive_bytes) > SKILL_ARCHIVE_MAX_BYTES:
        return JSONResponse({"error": "Archive file is too large"}, status_code=400)

    return await run_in_runtime_pool(
        request,
        "tool_io",
        _install_skill_archive,
        workspace_id=workspace_id,
        filename=filename,
        archive_bytes=archive_bytes,
    )


@skills_router.delete("/skills/{skill_name}")
async def delete_skill(
    skill_name: str,
    request: Request,
    workspace_id: str = Depends(require_admin_workspace_id),
):
    """Delete a skill."""
    return await run_in_runtime_pool(
        request,
        "tool_io",
        _delete_skill,
        skill_name,
        workspace_id,
    )


def _delete_skill(skill_name: str, workspace_id: str):
    skill_dir, validation_error = _resolve_skill_dir_for_delete(skill_name)
    if validation_error:
        return JSONResponse({"error": validation_error}, status_code=400)
    if not skill_dir or not skill_dir.is_dir():
        return JSONResponse({"error": "Skill not found"}, status_code=404)

    try:
        shutil.rmtree(skill_dir)
        logger.info("Deleted skill: %s", skill_name)
        invalidate_skill_registry(workspace_id=workspace_id)
        return {"success": True}
    except Exception:
        logger.exception("Failed to delete skill: %s", skill_name)
        return JSONResponse(
            {"error": "Failed to delete skill '{}'".format(skill_name)},
            status_code=500,
        )


@skills_router.get("/skills/{skill_name}")
async def get_skill(
    skill_name: str,
    request: Request,
    workspace_id: str = Depends(require_workspace_id),
):
    """Get skill details."""
    return await run_in_runtime_pool(
        request,
        "short_io",
        _get_skill,
        skill_name,
        workspace_id,
    )


def _get_skill(skill_name: str, workspace_id: str):
    registry = get_skill_registry(workspace_id=workspace_id)
    skill = registry.get_skill(skill_name)
    if not skill:
        return JSONResponse({"error": "Skill not found"}, status_code=404)
    return {
        "name": skill.name,
        "description": skill.description,
        "content": skill.content,
        "file_path": skill.file_path,
        "allowed_tools": skill.allowed_tools,
    }
