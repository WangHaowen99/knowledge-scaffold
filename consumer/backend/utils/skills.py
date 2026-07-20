"""Skill loading and management utilities for reusable external skills."""

import os
import re
import logging
import threading
import yaml
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from pydantic import BaseModel, Field

from workspace.workspaces import get_skill_roots, get_workspace_skills_root

logger = logging.getLogger("utils.skills")


# ==================== DATA MODELS ====================

class Skill(BaseModel):
    """
    Represents a loaded skill from SKILL.md file.
    """

    name: str = Field(..., description="Skill identifier (from YAML metadata)")
    description: str = Field(
        ..., description="What this skill does and when to use it"
    )
    content: str = Field(..., description="Full markdown content of the skill")
    file_path: str = Field(..., description="Path to the SKILL.md file")

    # Optional metadata
    allowed_tools: Optional[str] = Field(
        None, description="Comma-separated list of allowed tools"
    )


# ==================== SKILL LOADER ====================

class SkillLoader:
    """
    Loads reusable skills from SKILL.md files.

    Skills have YAML frontmatter with metadata followed by markdown content.
    """

    @staticmethod
    def load_skill(skill_path: Path) -> Optional[Skill]:
        """
        Load a skill from a SKILL.md file.

        Args:
            skill_path: Path to the SKILL.md file or directory containing it

        Returns:
            Skill object if successfully loaded, None otherwise
        """
        # If directory provided, look for SKILL.md inside
        if skill_path.is_dir():
            skill_file = skill_path / "SKILL.md"
        else:
            skill_file = skill_path

        if not skill_file.exists():
            logger.warning(f"Skill file not found: {skill_file}")
            return None

        try:
            content = skill_file.read_text(encoding="utf-8")
            return SkillLoader.parse_skill(content, str(skill_file))
        except Exception as e:
            logger.error(f"Failed to load skill from {skill_file}: {e}")
            return None

    @staticmethod
    def parse_skill(content: str, file_path: str) -> Optional[Skill]:
        """
        Parse skill content with YAML frontmatter.

        Expected format:
        ---
        name: skill-name
        description: Description text
        allowed-tools: tool1, tool2  # optional
        ---

        # Markdown content here

        Args:
            content: Full content of SKILL.md file
            file_path: Path to the file (for reference)

        Returns:
            Skill object if successfully parsed, None otherwise
        """
        # Extract YAML frontmatter
        frontmatter_pattern = r"^---\s*\n(.*?)\n---\s*\n(.*)$"
        match = re.match(frontmatter_pattern, content, re.DOTALL)

        if not match:
            logger.error(f"No valid YAML frontmatter found in {file_path}")
            return None

        yaml_content = match.group(1)
        markdown_content = match.group(2)

        try:
            metadata = yaml.safe_load(yaml_content)
        except yaml.YAMLError as e:
            logger.error(f"Failed to parse YAML in {file_path}: {e}")
            return None

        # Validate required fields
        if "name" not in metadata:
            logger.error(f"Missing 'name' field in {file_path}")
            return None
        if "description" not in metadata:
            logger.error(f"Missing 'description' field in {file_path}")
            return None

        # Create skill object
        try:
            skill = Skill(
                name=metadata["name"],
                description=metadata["description"],
                content=markdown_content.strip(),
                file_path=file_path,
                allowed_tools=metadata.get("allowed-tools"),
            )
            logger.info(f"Successfully loaded skill: {skill.name}")
            return skill
        except Exception as e:
            logger.error(f"Failed to create Skill object from {file_path}: {e}")
            return None


# ==================== SKILL REGISTRY ====================

class SkillRegistry:
    """
    Registry for discovering and managing skills.
    """

    def __init__(self, skill_dirs: Optional[List[Path]] = None):
        """
        Initialize skill registry.

        Args:
            skill_dirs: List of directories to scan for skills.
                       If None, uses the configured workspace skills directory.
        """
        self.skills: Dict[str, Skill] = {}
        self.skill_dirs = skill_dirs or []
        self._refresh_lock = threading.RLock()
        self._source_signature: Optional[Tuple[Any, ...]] = None

    def _discover_skills_snapshot(self) -> Tuple[Dict[str, Skill], int]:
        skills: Dict[str, Skill] = {}
        loaded_count = 0

        for skill_dir in self.skill_dirs:
            if not skill_dir.exists():
                logger.warning(f"Skill directory does not exist: {skill_dir}")
                continue

            logger.info(f"Scanning for skills in: {skill_dir}")

            # Look for subdirectories containing SKILL.md
            for item in sorted(skill_dir.iterdir(), key=lambda path: path.name.lower()):
                if item.is_dir():
                    skill_file = item / "SKILL.md"
                    if skill_file.exists():
                        skill = SkillLoader.load_skill(item)
                        if skill:
                            if skill.name in skills:
                                logger.info(
                                    "Skipping fallback skill '%s' from %s; a higher-priority root already provided it",
                                    skill.name,
                                    item,
                                )
                                continue
                            skills[skill.name] = skill
                            loaded_count += 1
                            logger.info(
                                f"Registered skill '{skill.name}' from {item.name}"
                            )

        return skills, loaded_count

    def refresh(self) -> int:
        """Atomically replace the registry with a stable filesystem snapshot."""
        stable_signature: Optional[Tuple[Any, ...]] = None
        skills: Dict[str, Skill] = {}
        loaded_count = 0

        with self._refresh_lock:
            for _attempt in range(3):
                before_signature = _build_skill_source_signature(self.skill_dirs)
                skills, loaded_count = self._discover_skills_snapshot()
                after_signature = _build_skill_source_signature(self.skill_dirs)
                if before_signature == after_signature:
                    stable_signature = after_signature
                    break

            # Readers retain the previous dictionary until this single swap.
            self.skills = skills
            self._source_signature = stable_signature

        logger.info(f"Skill discovery complete: {loaded_count} skills loaded")
        return loaded_count

    def discover_skills(self) -> int:
        return self.refresh()

    def is_stale(self) -> bool:
        with self._refresh_lock:
            return self._source_signature != _build_skill_source_signature(self.skill_dirs)

    def get_skill(self, name: str) -> Optional[Skill]:
        """
        Get a skill by name.

        Args:
            name: Skill name

        Returns:
            Skill object if found, None otherwise
        """
        return self.skills.get(name)

    def list_skills(self) -> List[str]:
        """
        List all registered skill names.

        Returns:
            List of skill names
        """
        return list(self.skills.keys())

    def snapshot(self) -> Dict[str, Skill]:
        return dict(self.skills)


# Registry instances are keyed by their complete search path.  A single
# process-wide registry is unsafe because concurrent workspace requests can
# replace its contents while another agent is still reading it.
_global_registry: Optional[SkillRegistry] = None
_registry_cache: Dict[Tuple[str, ...], SkillRegistry] = {}
_registry_cache_lock = threading.RLock()


def _file_stat_signature(path: Path) -> Tuple[bool, int, int]:
    try:
        stat = path.stat()
    except OSError:
        return False, 0, 0
    return True, int(stat.st_mtime_ns), int(stat.st_size)


def _build_skill_source_signature(skill_dirs: List[Path]) -> Tuple[Any, ...]:
    signature: List[Any] = []
    for root in skill_dirs:
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
                    _file_stat_signature(child / "SKILL.md"),
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
    """
    Get the backend root directory reliably using __file__.

    This works regardless of the current working directory.

    Returns:
        Path to the backend directory
    """
    # This file is at backend/agentic/utils/skills.py
    # So backend root is 3 levels up
    this_file = Path(__file__).resolve()
    utils_dir = this_file.parent  # backend/agentic/utils
    agentic_dir = utils_dir.parent  # backend/agentic
    backend_dir = agentic_dir.parent  # backend
    return backend_dir


def get_skill_registry(
    base_dir: Optional[Path] = None,
    *,
    workspace_id: Optional[str] = None,
) -> SkillRegistry:
    """
    Get a skill registry scoped to a workspace (plus system skills).

    Args:
        base_dir: Legacy base directory. When supplied, only ``base_dir/skills``
            is searched, preserving the historical helper behavior.
        workspace_id: Explicit workspace scope. The workspace skill directory
            is searched before the system skill directory returned by
            ``get_skill_roots``.

    Returns:
        A stable registry for the requested search roots.
    """
    global _global_registry

    if base_dir is not None and workspace_id is not None:
        raise ValueError("base_dir and workspace_id are mutually exclusive")

    if base_dir is None:
        skill_dirs = get_skill_roots(workspace_id)
    else:
        skill_dirs = [(base_dir / "skills").resolve()]

    cache_key = tuple(str(path.resolve()) for path in skill_dirs)
    while True:
        with _registry_cache_lock:
            # Preserve the historical test/reset contract around this alias.
            if _global_registry is None and _registry_cache:
                _registry_cache.clear()
            registry = _registry_cache.get(cache_key)

        if registry is not None:
            if registry.is_stale():
                registry.refresh()
            with _registry_cache_lock:
                if _registry_cache.get(cache_key) is not registry:
                    continue
                _global_registry = registry
                return registry

        candidate = SkillRegistry(skill_dirs=skill_dirs)
        candidate.refresh()
        with _registry_cache_lock:
            registry = _registry_cache.get(cache_key)
            if registry is None:
                registry = candidate
                _registry_cache[cache_key] = registry
            _global_registry = registry
            return registry


def invalidate_skill_registry(
    base_dir: Optional[Path] = None,
    *,
    workspace_id: Optional[str] = None,
) -> None:
    """Evict one workspace/base-dir registry after a skill lifecycle change."""
    global _global_registry

    if base_dir is not None and workspace_id is not None:
        raise ValueError("base_dir and workspace_id are mutually exclusive")
    if base_dir is None:
        skill_dirs = get_skill_roots(workspace_id)
    else:
        skill_dirs = [(base_dir / "skills").resolve()]

    cache_key = tuple(str(path.resolve()) for path in skill_dirs)
    with _registry_cache_lock:
        registry = _registry_cache.pop(cache_key, None)
        if registry is not None and _global_registry is registry:
            _global_registry = next(iter(_registry_cache.values()), None)


def build_skills_section(*, workspace_id: Optional[str] = None) -> str:
    """
    Build the skills section listing reusable external skills.

    Returns:
        Formatted markdown section describing available skills
    """
    registry = (
        get_skill_registry(workspace_id=workspace_id)
        if workspace_id
        else get_skill_registry()
    )
    skills = registry.snapshot()

    if not skills:
        return ""

    lines = [
        "## Skills",
        "Available skills are listed in the 'Available-Skills' section below. When a skill materially helps the task, load it with `skill(skill_name='<name>', resource='root')`.",
        "<Available-Skills>",
    ]

    for name in sorted(skills):
        skill = skills[name]
        lines.append("- **{}**: {}".format(name, skill.description))

    lines.append("</Available-Skills>")

    return "\n".join(lines)
