"""Modeling-template ontology parser package."""

from .mapping import build_file_mapping
from .modeling_template import compile_modeling_template
from .pipeline import run

__all__ = [
    "build_file_mapping",
    "compile_modeling_template",
    "run",
]
