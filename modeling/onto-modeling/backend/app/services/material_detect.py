"""Shared workbook classification and Sheet scope used by backend and project tools."""

import importlib.util
from pathlib import Path

from .. import config

_path = Path(config.ASSETS_DIR) / "workspace-toolkit" / "scripts" / "table_sources.py"
_spec = importlib.util.spec_from_file_location("onto_table_sources", _path)
_rules = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_rules)

classify_sheet = _rules.classify_sheet
detect_material = _rules.detect_material
detect_data_table = _rules.detect_data_table
resolve_selection = _rules.resolve_selection
selection_metadata = _rules.selection_metadata
read_selected_headers = _rules.read_selected_headers
evaluation_selection = _rules.evaluation_selection
SheetSelectionError = _rules.SheetSelectionError
