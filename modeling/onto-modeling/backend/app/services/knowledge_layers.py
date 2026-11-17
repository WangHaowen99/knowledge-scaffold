"""业务知识的分层（详设 5.3；契约 3.8 K-01、K-02）。

行业层：本项目启用的行业包（锁文件里的 kind=knowledge 包）带的知识条目，从包仓库里不可修改的版本目录读，
只读；项目里写一条同 slug 的条目就覆盖它（加载优先级：行业 < 项目，与 knowledge_helper 一致）。
"""

from __future__ import annotations

import re
from pathlib import Path

TYPES_BACK = {"correction": "correction", "convention": "convention", "terminology": "term", "domain-rule": "domain_rule"}
CONFIDENCE = ("high", "medium", "low")


def _helper():
    from .toolkit import _load

    return _load("knowledge_helper")


def _iso(value) -> str | None:
    from ..api.deps import iso

    return iso(value)


def _revision(data: bytes) -> str:
    from .workspace.layout import _content_revision

    return _content_revision(data)


def scope_view(scopes: list) -> dict:
    scopes = [str(scope) for scope in scopes or []]
    if not scopes or "global" in scopes:
        return {"kind": "global"}
    entries = []
    for scope in scopes:
        kind, _, key = scope.partition(":")
        if not key:
            kind, key = "object", kind
        entries.append({"kind": kind, "id": f"{kind}:{key}", "key": key, "name": key})
    return {"kind": "entries", "entries": entries}


def split_body(body: str) -> tuple[str, str]:
    parts = re.split(r"^##\s*(背景|规则)\s*$", body or "", flags=re.M)
    sections = {parts[i]: parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}
    if not sections:
        return "", (body or "").strip()
    rule = re.split(r"\n<details>", sections.get("规则", ""), maxsplit=1)[0].strip()
    return sections.get("背景", ""), rule


# ---- 行业层 ----


def industry_packs(manager, root: Path) -> list:
    """本项目启用的行业包（registry.PackView），按名字排。"""
    from .packs import registry

    return [view for view in registry.desired_packs(manager, root) if view.manifest.kind == "knowledge"]


def industry_entries(manager, root: Path) -> list[dict]:
    helper = _helper()
    out = []
    for view in industry_packs(manager, root):
        directory = view.directory / str(view.manifest.contributes.get("knowledge_dir") or "knowledge")
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            try:
                data = path.read_bytes()
                fm, body = helper.parse_frontmatter(data.decode("utf-8"))
            except (OSError, ValueError):  # ValueError 含 UnicodeDecodeError
                continue
            background, rule = split_body(body)
            out.append({
                "id": f"{view.name}:{path.stem}",
                "title": str(fm.get("name") or path.stem),
                "layer": "industry",
                "pack": view.name,
                "type": TYPES_BACK.get(str(fm.get("type")), "convention"),
                "scope": scope_view(list(fm.get("scopes") or [])),
                "confidence": fm.get("confidence") if fm.get("confidence") in CONFIDENCE else "medium",
                "source": {"kind": "pack", "label": f"{view.manifest.label} {view.version.split('+')[0]}"},
                "overrides_industry": False,
                "readonly": True,
                "revision": _revision(data),
                "updated_at": _iso(path.stat().st_mtime),
                "_slug": str(fm.get("slug") or path.stem),
                "_background": background,
                "_rule": rule,
                "_pack_label": view.manifest.label,
            })
    return out


def mark_overrides(project: list[dict], industry: list[dict]) -> dict[str, dict]:
    """项目条目覆盖了同 slug 的行业条目：标 overrides_industry，返回 {项目条目 id: 行业条目}。"""
    by_slug = {entry["_slug"]: entry for entry in industry}
    overridden = {}
    for entry in project:
        match = by_slug.get(entry.get("_slug") or "")
        if match is not None:
            entry["overrides_industry"] = True
            overridden[entry["id"]] = match
    return overridden


def layers_view(project: list[dict], industry: list[dict], packs: list) -> list[dict]:
    """K-01 layers：两层的 overridden 都是「覆盖了行业条目的项目条目数」（契约 F3-A 第 7 条）。"""
    overridden = sum(1 for entry in project if entry.get("overrides_industry"))
    layers = []
    if packs:
        layers.append({"layer": "industry", "label": "、".join(view.manifest.label for view in packs),
                       "count": len(industry), "packs": [view.name for view in packs], "overridden": overridden})
    layers.append({"layer": "project", "label": "项目知识", "count": len(project), "overridden": overridden})
    return layers
