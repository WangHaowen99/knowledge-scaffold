# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Markdown parsing/rendering helpers for standalone skill parser."""

import re
from typing import List, Sequence, Tuple


def parse_header(line: str) -> Tuple[int, str]:
    cleaned = str(line or "").lstrip("\ufeff")
    match = re.match(r"^\s{0,3}(#{1,6})\s*(.*?)\s*$", cleaned)
    if not match:
        return 0, ""
    title = match.group(2).strip()
    if not title:
        return 0, ""
    return len(match.group(1)), title


def parse_header_title(line: str) -> str:
    return parse_header(line)[1]


def extract_section_by_titles(lines: Sequence[str], titles: Sequence[str]) -> List[str]:
    title_set = {str(title or "").strip() for title in titles if str(title or "").strip()}
    if not title_set:
        return []

    start = -1
    start_level = 0
    for idx, line in enumerate(lines):
        level, heading_title = parse_header(line)
        if heading_title in title_set:
            start = idx + 1
            start_level = level
            break
    if start < 0:
        return []

    end = len(lines)
    for idx in range(start, len(lines)):
        level, _ = parse_header(lines[idx])
        if level and level <= start_level:
            end = idx
            break

    return list(lines[start:end])


def extract_section_by_title(lines: Sequence[str], title: str) -> List[str]:
    return extract_section_by_titles(lines, [title])


def is_table_separator_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped.startswith("|"):
        return False
    cells = [cell.strip() for cell in stripped.strip("|").split("|")]
    if not cells:
        return False
    return all(bool(re.match(r"^:?-+:?$", cell.replace(" ", ""))) for cell in cells)


def split_md_row(line: str) -> List[str]:
    stripped = line.strip().strip("|")
    if not stripped:
        return []
    return [cell.strip() for cell in stripped.split("|")]


def extract_first_markdown_table(section_lines: Sequence[str]) -> List[str]:
    for idx in range(0, len(section_lines) - 1):
        head = section_lines[idx].strip()
        sep = section_lines[idx + 1].strip()
        if head.startswith("|") and is_table_separator_line(sep):
            table_lines = [section_lines[idx], section_lines[idx + 1]]
            cursor = idx + 2
            while cursor < len(section_lines) and section_lines[cursor].strip().startswith("|"):
                table_lines.append(section_lines[cursor])
                cursor += 1
            return table_lines
    return []


def build_md_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    if not headers:
        return ""
    header_line = "| " + " | ".join(headers) + " |"
    sep_line = "| " + " | ".join(["---"] * len(headers)) + " |"
    lines = [header_line, sep_line]
    for row in rows:
        escaped = [str(cell).replace("|", "\\|") for cell in row]
        lines.append("| " + " | ".join(escaped) + " |")
    return "\n".join(lines)
