"""表格资料的 Sheet 范围与表头预检（ADR-0096）。

规则只有一份，在工作区脚本 `workspace-toolkit/scripts/table_sources.py`：助手侧的 table_profile 与这里同口径。
Excel 读文本版、CSV 直接读，不解析 Excel 原件（ADR-0076）。范围错误（SheetSelectionError）按契约回
409 schema_selection_invalid（api/errors 认它的 code、status）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from .. import config


def _load_rules():
    path = Path(config.ASSETS_DIR) / "workspace-toolkit" / "scripts" / "table_sources.py"
    spec = importlib.util.spec_from_file_location("onto_table_sources", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rules = _load_rules()

TABLE_SUFFIXES = rules.TABLE_SUFFIXES
SheetSelectionError = rules.SheetSelectionError
TextPending = rules.TextPending
classify_sheet = rules.classify_sheet
