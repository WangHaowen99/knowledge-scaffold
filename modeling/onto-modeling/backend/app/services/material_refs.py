"""资料被谁引用（契约 R-05；R-01 `referenced`、R-04 `broken_references` 同一份）。

扫本体条目与插件产物的正文（`ontology/`、`ext/` 下的文本文件），认两种写法：
- 出处路径（`resource/…[#片段]`，可带 `file:` 前缀）：资料分析报告、规则「来源」段、属性表里写的那种；
- 只写了文件名（`02-补货规则.docx`）：落不到片段，只算引用。
资料分析报告（`research/`）、检查报告（`reports/`）不是条目也不是产物，不算引用方。
"""

from __future__ import annotations

import re
from pathlib import Path

from . import material_text

SCAN_DIRS = ("ontology", "ext")
SCAN_SUFFIXES = (".md", ".yaml", ".yml", ".json", ".csv", ".txt")
FILE_BYTES_MAX = 1024 * 1024
ITEMS_MAX = 500
_HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*$")


def _scan_files(root: Path) -> list[Path]:
    files = []
    for top in SCAN_DIRS:
        folder = root / top
        if not folder.is_dir():
            continue
        for path in sorted(folder.rglob("*")):
            if path.is_file() and not path.is_symlink() and path.suffix.lower() in SCAN_SUFFIXES \
                    and path.stat().st_size <= FILE_BYTES_MAX:
                files.append(path)
    return files


def _target(root: Path, rel: str) -> dict | None:
    from . import project_events

    entries = project_events.entry_refs(root, [rel])
    if entries:
        return entries[0]
    artifacts = project_events.artifact_refs(root, [rel])
    return artifacts[0] if artifacts else None


def _mentions(text: str, materials: dict[str, str]) -> list[tuple[str, str, int]]:
    """[(资料路径, 片段, 位置)]：出处路径，加上只写了文件名的（前面不是路径的一部分）。"""
    found = [(path, fragment, pos) for path, fragment, pos in material_text.find_anchors(text) if path in materials]
    for rel, name in materials.items():
        start = 0
        while (pos := text.find(name, start)) >= 0:
            start = pos + len(name)
            if pos > 0 and text[pos - 1] in "/\\":
                continue
            found.append((rel, "", pos))
    return found


def scan(root: Path, materials: list[str]) -> dict[str, list[dict]]:
    """{资料路径: [{target, location, anchors}]}，每个引用方一项（同一条目多处引用合并，片段去重）。"""
    root = Path(root)
    names = {rel: rel.rsplit("/", 1)[-1] for rel in materials}
    refs: dict[str, dict[str, dict]] = {rel: {} for rel in materials}
    targets: dict[str, dict | None] = {}
    for path in _scan_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        hits = _mentions(text, names)
        if not hits:
            continue
        rel = path.relative_to(root).as_posix()
        if rel not in targets:
            targets[rel] = _target(root, rel)
        target = targets[rel]
        if target is None:
            continue
        headings = [(match.start(), match.group(1)) for match in re.finditer(_HEADING.pattern, text, re.M)]
        for material, fragment, pos in hits:
            key = target.get("id") or f"{target.get('type')}/{target.get('key')}"
            item = refs[material].setdefault(key, {"target": target, "location": "", "anchors": []})
            if not item["location"]:
                item["location"] = next((title for start, title in reversed(headings) if start <= pos), "")
            if fragment and fragment not in item["anchors"]:
                item["anchors"].append(fragment)
    return {rel: list(items.values())[:ITEMS_MAX] for rel, items in refs.items()}


def references(root: Path, rel: str) -> dict:
    """R-05 的响应。"""
    items = scan(root, [rel])[rel]
    return {"path": rel, "total": len(items), "items": items}


def counts(root: Path, materials: list[str]) -> dict[str, int]:
    return {rel: len(items) for rel, items in scan(root, materials).items()}
