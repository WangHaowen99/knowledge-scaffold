#!/usr/bin/env python3
"""Export matched ontology Excel supplements as compact markdown context."""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path
from typing import NamedTuple

from openpyxl import load_workbook


DEFAULT_EXCEL_DIR = Path("ontology/excel")


class ExcelContextResult(NamedTuple):
    markdown: str
    matched_paths: list[Path]
    missing_summary_stems: list[str]
    orphan_paths: list[Path]


def _clean_text(value: object) -> str:
    text = "" if value is None else str(value)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _escape_markdown_cell(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|")


def _logical_path(path: Path) -> str:
    parts = path.parts
    for marker in ("ontology", "resource", "reports"):
        if marker in parts:
            return str(Path(*parts[parts.index(marker):]))
    return str(path)


def _xlsx_files(excel_dir: Path) -> dict[str, Path]:
    if not excel_dir.is_dir():
        return {}
    return {
        path.stem: path
        for path in sorted(excel_dir.glob("*.xlsx"))
        if path.is_file() and not path.name.startswith("~$")
    }


def _normalize_row(row: tuple[object, ...], max_cols: int) -> list[str]:
    values = [_clean_text(cell) for cell in row[:max_cols]]
    while values and not values[-1]:
        values.pop()
    return values


def _worksheet_to_markdown(path: Path, max_rows: int, max_cols: int) -> list[str]:
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        lines: list[str] = []
        for worksheet in workbook.worksheets:
            raw_rows: list[list[str]] = []
            for row in worksheet.iter_rows(values_only=True):
                values = _normalize_row(row, max_cols)
                if values:
                    raw_rows.append(values)
                if len(raw_rows) >= max_rows + 1:
                    break

            if len(workbook.worksheets) > 1:
                lines.extend([f"### Sheet: {worksheet.title}", ""])

            if not raw_rows:
                lines.extend(["（空表）", ""])
                continue

            width = max(len(row) for row in raw_rows)
            header = raw_rows[0] + [""] * (width - len(raw_rows[0]))
            header = [cell or f"列{index}" for index, cell in enumerate(header, start=1)]
            body = [row + [""] * (width - len(row)) for row in raw_rows[1:]]

            lines.append("| " + " | ".join(_escape_markdown_cell(cell) for cell in header) + " |")
            lines.append("| " + " | ".join("---" for _ in header) + " |")
            if body:
                for row in body:
                    lines.append("| " + " | ".join(_escape_markdown_cell(cell) for cell in row) + " |")
            else:
                lines.append("| " + " | ".join("无" for _ in header) + " |")
            lines.append("")
        return lines
    finally:
        workbook.close()


def collect_excel_context(
    summary_paths: list[Path] | list[str],
    excel_dir: Path | str = DEFAULT_EXCEL_DIR,
    *,
    max_rows: int = 200,
    max_cols: int = 30,
) -> ExcelContextResult:
    summaries = [Path(path) for path in summary_paths]
    summary_stems = [path.stem for path in summaries]
    summary_stem_set = set(summary_stems)
    excel_dir = Path(excel_dir)
    excel_by_stem = _xlsx_files(excel_dir)

    matched_paths = [excel_by_stem[stem] for stem in summary_stems if stem in excel_by_stem]
    missing_summary_stems = [stem for stem in summary_stems if stem not in excel_by_stem]
    orphan_paths = [path for stem, path in excel_by_stem.items() if stem not in summary_stem_set]

    lines: list[str] = []
    if matched_paths:
        lines.extend(["# Excel 补充表上下文", ""])
        for path in matched_paths:
            lines.extend([f"## {path.stem}", "", f"来源：`{_logical_path(path)}`", ""])
            lines.extend(_worksheet_to_markdown(path, max_rows=max_rows, max_cols=max_cols))

    return ExcelContextResult(
        markdown="\n".join(lines).strip(),
        matched_paths=matched_paths,
        missing_summary_stems=missing_summary_stems,
        orphan_paths=orphan_paths,
    )


def _paths_from_args(values: list[str] | None) -> list[Path]:
    return [Path(value) for value in values or []]


def _summaries_from_args(args: argparse.Namespace) -> list[Path]:
    summaries = _paths_from_args(args.summary)
    if args.summary_dir:
        summaries.extend(sorted(Path(args.summary_dir).glob("*.md")))
    return summaries


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export matched Excel supplements as markdown context.")
    parser.add_argument("--summary", action="append", help="Selected flowchart summary path; repeatable")
    parser.add_argument("--summary-dir", help="Directory containing selected flowchart summaries")
    parser.add_argument("--excel-dir", default=str(DEFAULT_EXCEL_DIR), help="Directory containing split .xlsx files")
    parser.add_argument("--max-rows", type=int, default=200, help="Maximum non-empty rows per worksheet")
    parser.add_argument("--max-cols", type=int, default=30, help="Maximum columns per worksheet")
    parser.add_argument("--json", action="store_true", help="Print context and path metadata as JSON")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    result = collect_excel_context(
        _summaries_from_args(args),
        args.excel_dir,
        max_rows=args.max_rows,
        max_cols=args.max_cols,
    )
    if args.json:
        print(json.dumps({
            "markdown": result.markdown,
            "matched_paths": [_logical_path(path) for path in result.matched_paths],
            "missing_summary_stems": result.missing_summary_stems,
            "orphan_paths": [_logical_path(path) for path in result.orphan_paths],
        }, ensure_ascii=False, indent=2))
    else:
        print(result.markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
