from pathlib import Path
from typing import Dict

from .modeling_template import build_file_mapping as build_modeling_template_mapping


def build_file_mapping(source_root: Path, target_root: Path) -> Dict[str, object]:
    """Build file mapping from modeling-template source folder to ontology layout."""
    return build_modeling_template_mapping(source_root, target_root)
