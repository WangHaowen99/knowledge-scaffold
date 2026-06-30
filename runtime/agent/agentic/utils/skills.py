"""Skill loading and management utilities for reusable external skills."""

import os
import re
import logging
import yaml
from pathlib import Path
from typing import Dict, List, Optional
from pydantic import BaseModel, Field

from agentic.workspaces import get_skill_roots, get_workspace_skills_root

logger = logging.getLogger("agentic.utils.skills")


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

    def discover_skills(self) -> int:
        """
        Discover all skills in configured directories.

        Scans for directories containing SKILL.md files.

        Returns:
            Number of skills successfully loaded
        """
        loaded_count = 0

        for skill_dir in self.skill_dirs:
            if not skill_dir.exists():
                logger.warning(f"Skill directory does not exist: {skill_dir}")
                continue

            logger.info(f"Scanning for skills in: {skill_dir}")

            # Look for subdirectories containing SKILL.md
            for item in skill_dir.iterdir():
                if item.is_dir():
                    skill_file = item / "SKILL.md"
                    if skill_file.exists():
                        skill = SkillLoader.load_skill(item)
                        if skill:
                            self.skills[skill.name] = skill
                            loaded_count += 1
                            logger.info(
                                f"Registered skill '{skill.name}' from {item.name}"
                            )

        logger.info(f"Skill discovery complete: {loaded_count} skills loaded")
        return loaded_count

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


# Global registry instance
_global_registry: Optional[SkillRegistry] = None


def _get_backend_root() -> Path:
    """
    Get the repository root directory reliably using __file__.

    This works regardless of the current working directory.

    Returns:
        Path to the repository root directory
    """
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "platform").is_dir() and (candidate / "runtime").is_dir():
            return candidate
    return Path(__file__).resolve().parents[4]


def get_skill_registry(base_dir: Optional[Path] = None) -> SkillRegistry:
    """
    Get or create the global skill registry.

    Args:
        base_dir: Base directory of the project (auto-detects configured skills root)

    Returns:
        Global SkillRegistry instance
    """
    global _global_registry

    if base_dir is None:
        skill_dirs = get_skill_roots()
    else:
        skill_dirs = [(base_dir / "skills").resolve()]

    if (
        _global_registry is None
        or [path.resolve() for path in _global_registry.skill_dirs]
        != [path.resolve() for path in skill_dirs]
    ):
        _global_registry = SkillRegistry(skill_dirs=skill_dirs)
        _global_registry.discover_skills()

    return _global_registry


def build_skills_section() -> str:
    """
    Build the skills section listing reusable external skills.

    Returns:
        Formatted markdown section describing available skills
    """
    registry = get_skill_registry()
    skills = registry.skills

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
