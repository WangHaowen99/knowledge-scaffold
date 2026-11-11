"""表格资料的 Sheet 范围与表头预检（ADR-0096）。

规则只有一份，在工作区脚本 `workspace-toolkit/scripts/table_sources.py`：助手侧的 table_profile 与这里同口径。
Excel 读文本版、CSV 直接读，不解析 Excel 原件（ADR-0076）。范围对不上当前文件时翻成 409 schema_scope_stale。
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
TABLE, MAYBE, DICTIONARY, TERM, DOCUMENT, META, EMPTY = (
    rules.TABLE, rules.MAYBE, rules.DICTIONARY, rules.TERM, rules.DOCUMENT, rules.META, rules.EMPTY,
)
SheetSelectionError = rules.SheetSelectionError
classify_sheet = rules.classify_sheet
file_revision = rules.file_revision
manual_selection = rules.manual_selection
explicitly_empty = rules.explicitly_empty


def stale(exc: Exception, rel: str):
    """范围对不上当前文件 → 契约错误（409 schema_scope_stale），提示到资料区重新确认。"""
    from .workspace.errors import WorkspaceError

    return WorkspaceError(f"{Path(rel).name}：{exc}", 409, "schema_scope_stale", {"path": rel})


def resolve(path: Path, rel: str, directive: dict | None = None) -> dict:
    try:
        return rules.resolve_selection(path, directive)
    except SheetSelectionError as exc:
        raise stale(exc, rel) from exc


def read_selected_headers(path: Path, rel: str, directive: dict | None = None) -> dict[str, list[str]]:
    try:
        return rules.read_selected_headers(path, directive)
    except SheetSelectionError as exc:
        raise stale(exc, rel) from exc


def preview(path: Path, directive: dict | None) -> dict:
    """R-16：按「标成数据 Schema」算范围，带逐 Sheet 的表头与问题；有表头错误的 Sheet 不进建议范围。

    人工范围对不上当前文件时给现算的结果，原因放在 error。CSV 编码不对等读不了的情况抛 SheetSelectionError。"""
    current = directive if isinstance(directive, dict) and directive.get("purpose") == "schema" else {"purpose": "schema"}
    error = None
    try:
        scope = rules.resolve_selection(path, current)
    except SheetSelectionError as exc:
        error = str(exc)
        scope = rules.resolve_selection(path, {"purpose": "schema"})
    if not scope["pending"]:
        usable = {sheet["name"] for sheet in scope["sheets"] if sheet["schema_selectable"]}
        scope["included_sheets"] = [name for name in scope["included_sheets"] if name in usable]
        scope["reference_sheets"] = [sheet["name"] for sheet in scope["sheets"]
                                     if sheet["name"] not in scope["included_sheets"]]
    return {**scope, "error": error}


def summary(path: Path, directive: dict | None) -> dict:
    """资料列表（R-01）里的范围摘要：不带逐 Sheet 的表头与问题，那些在预检（R-16）里给。"""
    try:
        scope = rules.resolve_selection(path, directive)
    except SheetSelectionError as exc:
        manual = isinstance(directive, dict) and isinstance(directive.get("schema_selection"), dict)
        return {"mode": "manual" if manual else "auto", "pending": False, "included_sheets": [],
                "reference_sheets": [], "error": str(exc)}
    return {"mode": scope["mode"], "pending": scope["pending"], "included_sheets": scope["included_sheets"],
            "reference_sheets": scope["reference_sheets"], "error": None}
