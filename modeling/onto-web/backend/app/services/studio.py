"""Studio 读服务 v1：把 ontology 文档解析成图 / 表 / 条目三种视图的数据。

对象与逻辑文档以 Obsidian [[链接]] 互联；本服务只读解析，
编辑经由既有的文件写入口（中栏编辑器）完成。
"""

from __future__ import annotations

import re
from pathlib import Path

WIKILINK = re.compile(r"\[\[([^\]|#]+)")
SECTION = re.compile(r"^##\s+(.+)$", re.MULTILINE)

KIND_DIRS = (
    ("object", "ontology/object"),
    ("logic", "ontology/logic"),
)


def _docs(root: Path) -> list[dict]:
    docs = []
    for kind, rel_dir in KIND_DIRS:
        directory = root / rel_dir
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            docs.append(
                {
                    "name": path.stem,
                    "kind": kind,
                    "path": f"{rel_dir}/{path.name}",
                    "file": path,
                }
            )
    return docs


def graph(root: Path) -> dict:
    docs = _docs(root)
    by_name = {d["name"]: d for d in docs}
    nodes = []
    edges = []
    seen_edges = set()
    in_degree = {d["name"]: 0 for d in docs}
    out_degree = {d["name"]: 0 for d in docs}

    for doc in docs:
        try:
            text = doc["file"].read_text(encoding="utf-8")
        except OSError:
            text = ""
        for target in WIKILINK.findall(text):
            target = target.strip()
            if target == doc["name"] or target not in by_name:
                continue
            key = (doc["name"], target)
            if key in seen_edges:
                continue
            seen_edges.add(key)
            edges.append({"source": doc["name"], "target": target})
            out_degree[doc["name"]] += 1
            in_degree[target] += 1

    for doc in docs:
        nodes.append(
            {
                "id": doc["name"],
                "kind": doc["kind"],
                "path": doc["path"],
                "out": out_degree[doc["name"]],
                "in": in_degree[doc["name"]],
            }
        )
    return {"nodes": nodes, "edges": edges}


def element(root: Path, rel_path: str) -> dict:
    target = (root / rel_path).resolve()
    if not target.is_file():
        raise FileNotFoundError(rel_path)
    text = target.read_text(encoding="utf-8")
    name = target.stem

    # 按二级标题切分小节
    sections = []
    matches = list(SECTION.finditer(text))
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections.append({"title": match.group(1).strip(), "content": text[start:end].strip()})
    intro = text[: matches[0].start()].strip() if matches else text.strip()

    links = sorted({t.strip() for t in WIKILINK.findall(text) if t.strip() != name})
    backlinks = []
    for doc in _docs(root):
        if doc["name"] == name:
            continue
        try:
            other = doc["file"].read_text(encoding="utf-8")
        except OSError:
            continue
        if f"[[{name}" in other:
            backlinks.append({"name": doc["name"], "kind": doc["kind"], "path": doc["path"]})

    return {
        "name": name,
        "path": rel_path,
        "intro": intro,
        "sections": sections,
        "links": links,
        "backlinks": backlinks,
    }
