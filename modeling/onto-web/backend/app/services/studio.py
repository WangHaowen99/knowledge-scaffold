"""Studio 服务：读（图/表/条目三视图）+ 写路径（草稿区/租约/确定性分流，方案 7.2）。

写路径契约：改动先进草稿区，30 分钟租约写锁防并发编辑；提交时跑确定性
结构检查——通过直接经 write_file 落 git 版本，超范围转 agent 复核，
硬伤直接拒绝。语义判断永远不在这里做。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .. import config

WIKILINK = re.compile(r"\[\[([^\]|#]+)")
SECTION = re.compile(r"^(#{1,2})\s+(.+)$", re.MULTILINE)

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
            if path.name == "index.md":  # 工作区占位 index 不是本体元素
                continue
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

    sections = parse_sections(text)
    intro = _intro_of(text)

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
        "schema": schema_for(rel_path),
    }


def parse_sections(text: str) -> list[dict]:
    """按章节标题切分。模板风格用多个一级标题；历史产物是「# 文档名 + ## 章节」，
    自适应判定：一级标题多于一个（或只有一级）→ 章节层级取 1，否则取 2。"""
    matches = list(SECTION.finditer(text))
    level1 = sum(1 for m in matches if len(m.group(1)) == 1)
    level2 = sum(1 for m in matches if len(m.group(1)) == 2)
    section_level = 1 if (level1 > 1 or (level1 >= 1 and level2 == 0)) else 2
    tops = [m for m in matches if len(m.group(1)) <= section_level]
    sections = []
    for index, match in enumerate(tops):
        if len(match.group(1)) != section_level:
            continue  # 更高层级的标题（文档名）只作边界，不算章节
        start = match.end()
        end = tops[index + 1].start() if index + 1 < len(tops) else len(text)
        sections.append(
            {
                "level": section_level,
                "title": match.group(2).strip(),
                "content": text[start:end].strip(),
            }
        )
    return sections


def _intro_of(text: str) -> str:
    sections = parse_sections(text)
    if not sections:
        return text.strip()
    first_title = sections[0]["title"]
    match = re.search(rf"^#{{1,2}}\s+{re.escape(first_title)}\s*$", text, re.MULTILINE)
    return text[: match.start()].strip() if match else ""


def serialize_sections(sections: list[dict]) -> str:
    parts = []
    for section in sections:
        hashes = "#" * int(section.get("level", 1) or 1)
        parts.append(f"{hashes} {section['title']}\n\n{(section.get('content') or '').strip()}\n")
    return "\n".join(parts)


# ---- schema：对准工具集对象/逻辑模板（assets templates） ----

OBJECT_SCHEMA = [
    {"title": "对象描述", "kind": "text", "required": True},
    {"title": "对象别名", "kind": "text", "required": False},
    {"title": "英文名称", "kind": "text", "required": False},
    {
        "title": "对象属性清单",
        "kind": "table",
        "required": True,
        "columns": ["中文名称", "英文名称", "描述", "主键", "类型", "属性别名", "属性规则"],
    },
    {"title": "关联对象", "kind": "text", "required": True},
]

LOGIC_SCHEMA = [
    {"title": "业务描述", "kind": "text", "required": True},
    {"title": "与logic的关联", "kind": "text", "required": True},
    {"title": "与object的关联", "kind": "text", "required": True},
    {
        "title": "与object的语义关联",
        "kind": "table",
        "required": False,
        "columns": ["业务对象", "语义关联关系"],
    },
    {"title": "触发条件", "kind": "text", "required": False},
    {"title": "输入", "kind": "table", "required": True, "columns": ["参数名称", "描述", "输入源"]},
    {"title": "输出", "kind": "table", "required": True, "columns": ["参数名称", "描述"]},
    {"title": "逻辑描述", "kind": "text", "required": True},
]


def schema_for(rel_path: str) -> list[dict] | None:
    if rel_path.startswith("ontology/object/"):
        return OBJECT_SCHEMA
    if rel_path.startswith("ontology/logic/"):
        return LOGIC_SCHEMA
    return None


# ---- 草稿区与租约 ----

LEASE_SECONDS = 1800  # 30 分钟写锁（方案 7.2）


class StudioError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _state_file(project_id: str, name: str) -> Path:
    directory = Path(config.DATA_DIR) / "studio"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{project_id}.{name}.json"


def _load(project_id: str, name: str) -> dict:
    path = _state_file(project_id, name)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save(project_id: str, name: str, data: dict) -> None:
    _state_file(project_id, name).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def lease_status(project_id: str, rel_path: str) -> dict | None:
    leases = _load(project_id, "leases")
    lease = leases.get(rel_path)
    if lease and lease["expires"] > time.time():
        return lease
    if lease:  # 过期租约顺手清理
        leases.pop(rel_path, None)
        _save(project_id, "leases", leases)
    return None


def acquire_lease(project_id: str, rel_path: str, user: str) -> dict:
    current = lease_status(project_id, rel_path)
    if current and current["owner"] != user:
        raise StudioError(
            f"该条目正由 {current['owner']} 编辑（租约剩余 "
            f"{int((current['expires'] - time.time()) / 60)} 分钟），当前只读",
            409,
        )
    leases = _load(project_id, "leases")
    lease = {"owner": user, "expires": int(time.time()) + LEASE_SECONDS}
    leases[rel_path] = lease
    _save(project_id, "leases", leases)
    return lease


def release_lease(project_id: str, rel_path: str, user: str) -> None:
    leases = _load(project_id, "leases")
    lease = leases.get(rel_path)
    if lease and lease["owner"] == user:
        leases.pop(rel_path, None)
        _save(project_id, "leases", leases)


def save_draft(
    project_id: str, rel_path: str, content: str, base_revision: str, user: str
) -> dict:
    acquire_lease(project_id, rel_path, user)  # 持有或续期租约
    drafts = _load(project_id, "drafts")
    drafts[rel_path] = {
        "content": content,
        "base_revision": base_revision,
        "author": user,
        "updated_at": int(time.time()),
    }
    _save(project_id, "drafts", drafts)
    return {"saved": True, "updated_at": drafts[rel_path]["updated_at"]}


def get_draft(project_id: str, rel_path: str) -> dict | None:
    return _load(project_id, "drafts").get(rel_path)


def discard_draft(project_id: str, rel_path: str, user: str) -> None:
    drafts = _load(project_id, "drafts")
    if rel_path in drafts:
        drafts.pop(rel_path)
        _save(project_id, "drafts", drafts)
    release_lease(project_id, rel_path, user)


# ---- 确定性检查分流 ----


def deterministic_check(root: Path, rel_path: str, content: str) -> dict:
    """结构性检查（不经 LLM）：errors = 硬伤拒绝；review = 超范围转 agent 复核。"""
    errors: list[str] = []
    review: list[str] = []
    schema = schema_for(rel_path)
    if schema is None:
        review.append("非对象/逻辑文档，Studio 无法做确定性判定")
        return {"errors": errors, "review": review}

    sections = parse_sections(content)
    titles = {s["title"] for s in sections}
    known_titles = {s["title"] for s in schema}

    for spec in schema:
        if spec["required"] and spec["title"] not in titles:
            errors.append(f"缺少必需章节「{spec['title']}」")
    for title in titles - known_titles:
        review.append(f"新增了模板外的章节「{title}」")

    by_title = {s["title"]: s for s in sections}
    for spec in schema:
        if spec["kind"] != "table" or spec["title"] not in by_title:
            continue
        body = by_title[spec["title"]]["content"]
        if "|" not in body:
            if spec["required"] and body.strip() not in ("无", ""):
                review.append(f"「{spec['title']}」不是表格也不是「无」")
            continue
        header_line = next((l for l in body.splitlines() if "|" in l), "")
        header_cols = [c.strip() for c in header_line.strip("|").split("|")]
        missing = [c for c in spec["columns"] if c not in header_cols]
        if missing:
            errors.append(f"「{spec['title']}」表头缺少列：{'、'.join(missing)}")

    # 主键：对象属性表至少一行主键为"是"
    if rel_path.startswith("ontology/object/") and "对象属性清单" in by_title:
        body = by_title["对象属性清单"]["content"]
        data_rows = [l for l in body.splitlines() if "|" in l][2:]
        if data_rows and not any("是" in r for r in data_rows):
            review.append("对象属性清单没有任何主键行（主键列均非「是」）")

    # 链接目标存在性：指向不存在的文档 → 可能是要新建对象，转复核
    names = {d["name"] for d in _docs(root)}
    stem = Path(rel_path).stem
    for target in {t.strip() for t in WIKILINK.findall(content)}:
        if target and target != stem and target not in names:
            review.append(f"[[{target}]] 指向不存在的文档")

    return {"errors": errors, "review": review}


REVIEW_DIR = ".ontology-state/studio-review"


def stage_for_review(root: Path, rel_path: str, content: str) -> str:
    """把草稿放到 agent 可读的隐藏位置（不进产物树、不进 git），供复核。"""
    stem = Path(rel_path).stem
    directory = root / REVIEW_DIR
    directory.mkdir(parents=True, exist_ok=True)
    staged = directory / f"{stem}.md"
    staged.write_text(content, encoding="utf-8")
    return f"{REVIEW_DIR}/{stem}.md"
