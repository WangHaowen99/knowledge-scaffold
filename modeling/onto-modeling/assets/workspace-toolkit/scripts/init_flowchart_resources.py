#!/usr/bin/env python3
"""
Initialize flowchart source materials into ontology/.

- resource/diagrams/*.drawio -> ontology/diagrams/*.drawio
- resource/excel/*.xlsx sheet tabs -> ontology/excel/{sheet_name}.xlsx
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from openpyxl import load_workbook

sys.path.append(os.path.dirname(__file__))
from summarize_diagram import summarize  # noqa: E402


LEGEND_FILE = "图示说明定义.drawio"
DERIVED_DIAGRAMS = Path("ontology/diagrams")
DERIVED_EXCEL = Path("ontology/excel")
DERIVED_SUMMARIES = Path("ontology/diagrams-summary")
RESOURCE_DIAGRAMS = Path("resource/diagrams")
RESOURCE_EXCEL = Path("resource/excel")


def _drawio_page_count(path: Path) -> int:
    text = path.read_text(encoding="utf-8")
    return len(re.findall(r"<diagram\b", text))


def _safe_name(name: str) -> str:
    return re.sub(r'[/\\:*?"<>|]', "_", name).replace(" ", "")


def _drawio_pages(path: Path) -> tuple[str, list[tuple[str, str]]]:
    content = path.read_text(encoding="utf-8")
    header_match = re.match(r"(<mxfile[^>]*>)", content)
    header = header_match.group(1) if header_match else "<mxfile>"
    blocks = re.findall(r"(<diagram\b[^>]*>.*?</diagram>)", content, re.DOTALL)
    pages = []
    for block in blocks:
        name_match = re.search(r'name="([^"]+)"', block)
        name = name_match.group(1) if name_match else "unnamed"
        pages.append((name, block))
    return header, pages


def _write_drawio_page(header: str, diagram_block: str, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f"{header}\n"
        f"  {diagram_block}\n"
        "</mxfile>\n",
        encoding="utf-8",
    )


def _source_files(src: Path, suffix: str, excluded: set[str] | None = None) -> list[Path]:
    excluded = excluded or set()
    if not src.is_dir():
        return []
    return sorted(path for path in src.glob(f"*{suffix}") if path.name not in excluded and not path.name.startswith("~$"))


def initialize_diagrams(
    src: Path | str = RESOURCE_DIAGRAMS,
    dst: Path | str = DERIVED_DIAGRAMS,
    *,
    page_names: list[str] | None = None,
    source_paths: list[Path] | None = None,
    strict: bool = True,
) -> list[Path]:
    src = Path(src)
    dst = Path(dst)
    if source_paths is None and not src.is_dir():
        print(f"跳过流程图：{src} 不存在")
        return []

    dst.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    wanted = set(page_names or [])
    matched = set()
    seen_pages: dict[str, Path] = {}

    files = source_paths or _source_files(src, ".drawio", {LEGEND_FILE})
    for path in files:
        header, pages = _drawio_pages(path)
        if not pages:
            print(f"跳过流程图：{path} 未找到页签")
            continue
        for page_name, block in pages:
            if wanted and page_name not in wanted:
                continue
            if page_name in seen_pages:
                raise ValueError(f"重复页签名称：{page_name} 同时存在于 {seen_pages[page_name]} 和 {path}")
            seen_pages[page_name] = path
            matched.add(page_name)
            out_path = dst / f"{_safe_name(page_name)}.drawio"
            _write_drawio_page(header, block, out_path)
            outputs.append(out_path)
            print(f"刷新流程图 {path.name}#{page_name} → {out_path}")

    missing = wanted - matched
    if missing and strict:
        raise ValueError("未找到流程图页签：" + "、".join(sorted(missing)))

    if not files:
        print(f"跳过流程图：{src} 下没有业务 .drawio 文件")
    return outputs


def split_excel_workbooks(
    src: Path | str = RESOURCE_EXCEL,
    dst: Path | str = DERIVED_EXCEL,
    *,
    sheet_names: list[str] | None = None,
    source_paths: list[Path] | None = None,
    strict: bool = True,
) -> list[Path]:
    src = Path(src)
    dst = Path(dst)
    if source_paths is None and not src.is_dir():
        print(f"跳过 Excel：{src} 不存在")
        return []

    files = source_paths or _source_files(src, ".xlsx")
    if not files:
        print(f"跳过 Excel：{src} 下没有 .xlsx 文件")
        return []

    dst.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    seen_sheet_names: dict[str, Path] = {}
    wanted = set(sheet_names or [])
    matched = set()

    for path in files:
        workbook = load_workbook(path, read_only=True)
        sheet_names = list(workbook.sheetnames)
        workbook.close()

        for sheet_name in sheet_names:
            if wanted and sheet_name not in wanted:
                continue
            if sheet_name in seen_sheet_names:
                first_source = seen_sheet_names[sheet_name]
                raise ValueError(
                    f"重复 sheet 名称：{sheet_name} 同时存在于 {first_source} 和 {path}，"
                    "无法按 sheet 名直接落盘"
                )
            seen_sheet_names[sheet_name] = path
            matched.add(sheet_name)

            single = load_workbook(path)
            for name in list(single.sheetnames):
                if name != sheet_name:
                    del single[name]

            out_path = dst / f"{sheet_name}.xlsx"
            single.save(out_path)
            single.close()
            outputs.append(out_path)
            print(f"拆分 {path.name}#{sheet_name} → {out_path}")

    missing = wanted - matched
    if missing and strict:
        raise ValueError("未找到 Excel sheet：" + "、".join(sorted(missing)))

    return outputs


def _remove_files(directory: Path, pattern: str) -> None:
    if not directory.is_dir():
        return
    for path in directory.glob(pattern):
        if path.is_file():
            path.unlink()


def _generate_summaries(diagrams: list[Path], root: Path) -> list[Path]:
    if not diagrams:
        return []
    summary_dir = root / DERIVED_SUMMARIES
    summary_dir.mkdir(parents=True, exist_ok=True)
    legend_path = root / RESOURCE_DIAGRAMS / LEGEND_FILE
    outputs = []
    for diagram in diagrams:
        out_path = summary_dir / f"{diagram.stem}.md"
        try:
            text = summarize(str(diagram), str(legend_path))
        except Exception as exc:
            text = f"# {diagram.stem} — 摘要提取失败\n\n错误：{exc}"
        out_path.write_text(text, encoding="utf-8")
        outputs.append(out_path)
        print(f"摘要 {diagram.stem} → {out_path}")
    return outputs


def _paths_from_args(values: list[str] | None, root: Path) -> list[Path] | None:
    if not values:
        return None
    return [Path(value) if Path(value).is_absolute() else root / value for value in values]


def _normalize_names(values: list[str] | None) -> list[str] | None:
    if not values:
        return None
    names = []
    for value in values:
        names.extend(part.strip() for part in re.split(r"[、,，]+", value) if part.strip())
    return names


def refresh_resources(
    *,
    root: Path | str = ".",
    full_refresh: bool = False,
    pages: list[str] | None = None,
    sheets: list[str] | None = None,
    names: list[str] | None = None,
    diagram_sources: list[Path] | None = None,
    excel_sources: list[Path] | None = None,
    update_summaries: bool = True,
) -> dict[str, list[Path]]:
    root = Path(root)
    pages = _normalize_names(pages)
    sheets = _normalize_names(sheets)
    names = _normalize_names(names)
    has_filter = bool(pages or sheets or names)
    if full_refresh and has_filter:
        raise ValueError("--refresh 是全量刷新，不能同时指定 --pages/--sheets/--names")

    target_pages = pages or names
    target_sheets = sheets or names
    strict_pages = pages is not None
    strict_sheets = sheets is not None
    has_filter = bool(pages or sheets or names or diagram_sources or excel_sources)
    refresh_diagrams = full_refresh or not has_filter or pages is not None or names is not None or diagram_sources is not None
    refresh_excels = full_refresh or not has_filter or sheets is not None or names is not None or excel_sources is not None

    if full_refresh:
        _remove_files(root / DERIVED_DIAGRAMS, "*.drawio")
        _remove_files(root / DERIVED_EXCEL, "*.xlsx")
        _remove_files(root / DERIVED_SUMMARIES, "*.md")
        print("已清理流程图相关派生文件。")

    diagrams = (
        initialize_diagrams(
            root / RESOURCE_DIAGRAMS,
            root / DERIVED_DIAGRAMS,
            page_names=target_pages,
            source_paths=diagram_sources,
            strict=strict_pages,
        )
        if refresh_diagrams
        else []
    )
    excels = (
        split_excel_workbooks(
            root / RESOURCE_EXCEL,
            root / DERIVED_EXCEL,
            sheet_names=target_sheets,
            source_paths=excel_sources,
            strict=strict_sheets,
        )
        if refresh_excels
        else []
    )
    summaries = _generate_summaries(diagrams, root) if update_summaries else []

    print("完成。")
    print(f"流程图：{len(diagrams)} 个 → ontology/diagrams/")
    print(f"Excel 表：{len(excels)} 个 → ontology/excel/")
    print(f"摘要：{len(summaries)} 个 → ontology/diagrams-summary/")
    return {"diagrams": diagrams, "excels": excels, "summaries": summaries}


def initialize_resources() -> tuple[list[Path], list[Path]]:
    result = refresh_resources(update_summaries=False)
    return result["diagrams"], result["excels"]


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Initialize or refresh flowchart resource derivatives.")
    parser.add_argument("--refresh", action="store_true", help="Full refresh: remove derived diagrams/excel/summaries first")
    parser.add_argument("--diagram", action="append", help="Source drawio file to refresh; can be repeated")
    parser.add_argument("--excel", action="append", help="Source xlsx file to refresh; can be repeated")
    parser.add_argument("--pages", nargs="+", help="Drawio page names to refresh")
    parser.add_argument("--sheets", nargs="+", help="Excel sheet names to refresh")
    parser.add_argument("--names", nargs="+", help="Names to refresh in both drawio pages and Excel sheets")
    parser.add_argument("--root", default=".", help="Project root containing resource/ and ontology/")
    parser.add_argument("--no-summary", action="store_true", help="Skip summary generation for refreshed drawio files")
    return parser.parse_args(argv)


if __name__ == "__main__":
    try:
        args = parse_args(sys.argv[1:])
        root = Path(args.root)
        refresh_resources(
            root=root,
            full_refresh=args.refresh,
            pages=args.pages,
            sheets=args.sheets,
            names=args.names,
            diagram_sources=_paths_from_args(args.diagram, root),
            excel_sources=_paths_from_args(args.excel, root),
            update_summaries=not args.no_summary,
        )
    except Exception as exc:
        print(f"初始化失败：{exc}", file=sys.stderr)
        sys.exit(1)
