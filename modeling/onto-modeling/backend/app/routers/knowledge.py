"""业务知识（API 契约 3.8）：K-01 列表、K-02 详情，只读。

知识条目存放在 core：工作区 `ontology/knowledge/<type>-<slug>.md`，由工具箱 knowledge_helper
管理（助手加载的也是这一份）。旧版网页写在 `knowledge/<名称>.md` 的条目只读列出（layer 仍为 project）。
启用了行业包时多一个只读的行业层（services/knowledge_layers）；项目里同 slug 的条目覆盖行业条目。
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Request

from ..api import deps
from ..api.errors import ApiError
from ..kernel.artifacts import index as artifact_index
from ..services import knowledge_layers
from ..services.workspace.layout import _content_revision

router = APIRouter(prefix="/api/v1/projects/{project_id}/knowledge", tags=["knowledge"])

KNOWLEDGE_DIR = "ontology/knowledge"
LEGACY_DIR = "knowledge"
# 条目文件里的类型 → 接口里的类型
TYPES_BACK = {"correction": "correction", "convention": "convention", "terminology": "term", "domain-rule": "domain_rule"}
CONFIDENCE = ("high", "medium", "low")
_SECTION_RE = re.compile(r"^##\s*(背景|规则)\s*$", re.M)


def _toolkit():
    from ..services.toolkit import _load

    return _load("knowledge_helper")


def _split_body(body: str) -> tuple[str, str]:
    parts = _SECTION_RE.split(body or "")
    sections = {parts[i]: parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}
    if not sections:
        return "", (body or "").strip()
    return sections.get("背景", ""), sections.get("规则", "")


def _scope_view(scopes: list[str]) -> dict:
    if not scopes or "global" in scopes:
        return {"kind": "global"}
    entries = []
    for scope in scopes:
        kind, _, key = str(scope).partition(":")
        if not key:
            kind, key = "object", kind
        entries.append({"kind": kind, "id": f"{kind}:{key}", "key": key, "name": key})
    return {"kind": "entries", "entries": entries}


def _entry(path: Path, legacy: bool = False) -> dict:
    text = path.read_text(encoding="utf-8")
    stat = path.stat()
    if legacy:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        title = next((line[2:].strip() for line in lines if line.startswith("# ")), path.stem)
        fm, body = {"name": title, "type": "convention", "scopes": ["global"], "confidence": "medium", "source": "web"}, text
    else:
        fm, body = _toolkit().parse_frontmatter(text)
    background, rule = _split_body(body)
    source = str(fm.get("source") or "")
    if source.startswith("conversation"):
        source_view = {"kind": "conversation", "label": "对话"}
    elif source in ("", "web", "manual"):
        source_view = {"kind": "manual", "label": "手动新增"}
    else:
        source_view = {"kind": "material", "label": source}
    return {
        "id": path.stem,
        "title": str(fm.get("name") or path.stem),
        "layer": "project",
        "pack": None,
        "type": TYPES_BACK.get(str(fm.get("type")), "convention"),
        "scope": _scope_view(list(fm.get("scopes") or [])),
        "confidence": fm.get("confidence") if fm.get("confidence") in CONFIDENCE else "medium",
        "source": {**source_view, "at": deps.iso(stat.st_mtime)},
        "overrides_industry": False,
        # 网页没有写入接口：条目由助手经 knowledge_helper 维护
        "readonly": True,
        "revision": _content_revision(path.read_bytes()),
        "updated_at": deps.iso(stat.st_mtime),
        "_background": background,
        "_rule": rule,
        "_slug": str(fm.get("slug") or path.stem),
        "_source": source,
    }


def _public(entry: dict) -> dict:
    return {key: value for key, value in entry.items() if not key.startswith("_")}


def _entries(root: Path) -> list[dict]:
    found = []
    for directory, legacy in ((root / KNOWLEDGE_DIR, False), (root / LEGACY_DIR, True)):
        if not directory.is_dir() or artifact_index.inside_no_links(root, directory.relative_to(root).as_posix()) is None:
            continue  # 目录本身（或上层）是符号链接：不读
        for path in sorted(directory.glob("*.md")):
            if path.name == "INDEX.md" or path.is_symlink() or not path.is_file():
                continue
            try:
                found.append(_entry(path, legacy))
            except (OSError, ValueError):  # ValueError 含 UnicodeDecodeError
                continue
    return found


def _find(root: Path, knowledge_id: str, industry: list[dict] | None = None) -> dict:
    for entry in _entries(root):
        if entry["id"] == knowledge_id:
            return entry
    for entry in industry or []:
        if entry["id"] == knowledge_id:
            return entry
    raise ApiError("not_found", "知识条目不存在", 404)


def _layers(root: Path) -> tuple[list[dict], list[dict], dict, list]:
    """（项目条目, 行业条目, 项目条目 id → 被它覆盖的行业条目, 启用的行业包）。"""
    project = _entries(root)
    packs = knowledge_layers.industry_packs(deps.workspaces, root)
    industry = knowledge_layers.industry_entries(deps.workspaces, root)
    overrides = knowledge_layers.mark_overrides(project, industry)
    return project, industry, overrides, packs


@router.get("")
def list_knowledge(project_id: str, request: Request, layer: str = "all", type: str = "", q: str = "",
                   cursor: str = "", limit: int = 0):
    root = deps.require_owner(project_id, request)
    if layer not in ("all", "project", "industry"):
        raise ApiError("invalid_argument", "layer 只能是 all、project、industry", 400, {"fields": {"layer": "enum"}})
    project, industry, _, packs = _layers(root)
    entries = [_public(entry) for entry in project] + [_public(entry) for entry in industry]
    needle = q.strip().lower()

    def matches(entry: dict) -> bool:
        if layer not in ("all", entry["layer"]) or (type and entry["type"] not in type.split(",")):
            return False
        return not needle or needle in entry["title"].lower()

    items = [entry for entry in entries if matches(entry)]
    page = deps.page(items, cursor, limit, {"layer": layer, "type": type, "q": needle})
    return {
        "items": page["items"],
        "next_cursor": page["next_cursor"],
        "counts": {"all": len(entries), "project": len(project), "industry": len(industry)},
        "layers": knowledge_layers.layers_view(project, industry, packs),
    }


@router.get("/{knowledge_id}")
def get_knowledge(project_id: str, knowledge_id: str, request: Request):
    root = deps.require_owner(project_id, request)
    project, industry, overrides, _ = _layers(root)
    entry = _find(root, knowledge_id, industry)
    overridden = overrides.get(entry["id"])
    source_quote = None
    if entry.get("_source", "").startswith("conversation:"):
        source_quote = {"text": entry["_rule"][:200], "conversation_id": entry["_source"].split(":", 1)[1] or None,
                        "message_id": None, "at": entry["updated_at"], "by": None}
    return {
        **_public(entry),
        "background_md": entry["_background"], "rule_md": entry["_rule"],
        "source_quote": source_quote,
        "overrides": {"pack": overridden["pack"], "entry_id": overridden["id"], "entry_title": overridden["title"],
                      "note": None} if overridden else None,
        "used_by": [],
    }
