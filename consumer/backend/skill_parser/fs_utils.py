"""Filesystem helpers for standalone skill parser."""

from pathlib import Path
from typing import List


def normalize_lines(text: str) -> List[str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.split("\n")


def relative_path(path: Path, base: Path) -> str:
    return str(path.resolve().relative_to(base.resolve())).replace("\\", "/")


def collect_md_files(folder: Path, *, recursive: bool = False) -> List[Path]:
    if not folder.exists() or not folder.is_dir():
        return []

    if recursive:
        items = [item for item in folder.rglob("*.md") if item.is_file()]
        return sorted(
            items,
            key=lambda p: str(p.relative_to(folder)).replace("\\", "/").lower(),
        )

    return sorted(
        [item for item in folder.iterdir() if item.is_file() and item.suffix.lower() == ".md"],
        key=lambda p: p.name,
    )
