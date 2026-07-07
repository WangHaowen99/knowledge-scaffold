"""Top-level run pipeline for standalone skill parser."""

from pathlib import Path
from typing import Dict

from .modeling_template import compile_modeling_template


def run(
    source_root: Path,
    target_root: Path,
    *,
    write_mapping_json: bool = False,
) -> Dict[str, object]:
    """Run parsing and generation for the modeling-template layout."""
    return compile_modeling_template(
        source_root=source_root,
        target_root=target_root,
        write_mapping_json=write_mapping_json,
    )
