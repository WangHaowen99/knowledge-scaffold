"""业务知识的分层与知识沉淀包的确定性检查（详设 5.3、5.5；契约 3.8 K-01、K-02、K-06～K-08）。

- 行业层：本项目启用的行业包（锁文件里的 kind=knowledge 包）带的知识条目，从包仓库里不可修改的版本
  目录读，只读；项目里写一条同 slug 的条目就覆盖它（加载优先级：行业 < 项目，与 knowledge_helper 一致）。
- 冲突检查（K-06）只做确定性的部分：同名重复、项目条目覆盖了口径不同的行业条目、适用范围引用了
  不存在的本体条目。需要语义判断的口径冲突，由知识沉淀包的命令发到对话复核。
- 提名（K-07、K-08）：脱敏检查是硬门槛（详设 5.5），这里先做自动检查与替换，提交后进管理员审核。
"""

from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path

from .. import config

KNOWLEDGE_DIR = "ontology/knowledge"
CONFLICTS_REL = ".onto/run/knowledge-conflicts.json"
TYPES_BACK = {"correction": "correction", "convention": "convention", "terminology": "term", "domain-rule": "domain_rule"}
CONFIDENCE = ("high", "medium", "low")
_ENTITY_DIRS = {"object": ("ontology/object",), "operation": ("ontology/operation", "ontology/logic"),
                "rule": ("ontology/rule",), "metric": ("ontology/metric",), "script": ("ontology/scripts",)}

# 脱敏检查（K-07）：只判有无、给替换后的文字，不回显命中的原文
_SANITIZE = (
    ("secret_or_pii", re.compile(r"1[3-9]\d{9}|[\w.+-]+@[\w-]+\.[\w.]+|\b\d{17}[\dXx]\b|https?://\S+"), "〔已隐去〕",
     "去掉手机号、邮箱、证件号与网址"),
    ("customer_name", re.compile(r"[一-龥A-Za-z0-9]{2,12}(?:有限公司|股份公司|集团|公司|分店|门店(?=[A-Z0-9]))"), "某单位",
     "单位名称换成通用叫法"),
    ("internal_id", re.compile(r"\b[A-Z]{2,}[-_]?\d{3,}\b|\b\d{8,}\b"), "〔编号〕", "内部编号换成占位"),
    ("amount", re.compile(r"[¥￥]\s?\d[\d,.]*|\d[\d,.]*\s?(?:万元|元|万)"), "〔金额〕", "金额换成占位"),
)


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


# ---- K-06 冲突检查 ----


def _entity_exists(root: Path, kind: str, key: str) -> bool:
    for directory in _ENTITY_DIRS.get(kind, ()):
        base = Path(root) / directory
        if (base / f"{key}{'.py' if kind == 'script' else '.md'}").is_file():
            return True
        if base.is_dir() and kind == "object" and any(base.rglob(f"{key}.md")):
            return True
    return False


def check_conflicts(root: Path, project: list[dict], industry: list[dict]) -> dict:
    started = time.time()
    conflicts = []
    by_slug = {entry["_slug"]: entry for entry in industry}
    titles: dict[str, dict] = {}
    for entry in project:
        twin = titles.get(entry["title"])
        if twin is not None:
            conflicts.append({"kind": "duplicate", "project_entry": {"id": entry["id"], "title": entry["title"]},
                              "industry_entry": None, "duplicate_of": {"id": twin["id"], "title": twin["title"]},
                              "effective": "project"})
        titles.setdefault(entry["title"], entry)
        industry_twin = by_slug.get(entry.get("_slug") or "")
        if industry_twin is not None and industry_twin["_rule"].strip() != entry.get("_rule", "").strip():
            conflicts.append({"kind": "scope_conflict", "project_entry": {"id": entry["id"], "title": entry["title"]},
                              "industry_entry": {"id": industry_twin["id"], "title": industry_twin["title"]},
                              "effective": "project"})
        scope = entry.get("scope") or {}
        missing = [ref["key"] for ref in scope.get("entries") or []
                   if ref.get("kind") in _ENTITY_DIRS and not _entity_exists(root, ref["kind"], ref["key"])]
        if missing:
            conflicts.append({"kind": "broken_ref", "project_entry": {"id": entry["id"], "title": entry["title"]},
                              "industry_entry": None, "missing": missing, "effective": "project"})
    for index, item in enumerate(conflicts, 1):
        item["id"] = f"kc-{index}"
    unsourced = sum(1 for entry in project if entry["source"]["kind"] == "manual")
    conflicted = {item["project_entry"]["id"] for item in conflicts}

    def sourced_and_clean(entry: dict) -> bool:
        return entry["source"]["kind"] in ("conversation", "material") and entry["id"] not in conflicted

    consistent = sum(1 for entry in project if sourced_and_clean(entry))
    return {
        "checked_at": _iso(time.time()),
        "duration_ms": int((time.time() - started) * 1000),
        "total_entries": len(project) + len(industry),
        "conflicts": conflicts,
        # 来自对话或资料、且没有冲突的项目条目数（「与资料一致」的确定性口径）
        "consistent_with_materials": consistent,
        "unsourced": unsourced,
        "duplicates": sum(1 for item in conflicts if item["kind"] == "duplicate"),
        "broken_refs": sum(1 for item in conflicts if item["kind"] == "broken_ref"),
    }


def save_conflicts(root: Path, result: dict) -> None:
    path = Path(root) / CONFLICTS_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


def conflict_with(item: dict) -> str:
    """冲突的另一方，给 K-03/K-04 的 `conflicts[].with`：行业条目、同名条目或不存在的引用。"""
    other = item.get("industry_entry") or item.get("duplicate_of") or {}
    return other.get("title") or "、".join(item.get("missing") or [])


def last_conflicts(root: Path) -> dict:
    try:
        result = json.loads((Path(root) / CONFLICTS_REL).read_text(encoding="utf-8"))
        # 旧版存的冲突带说明句 note，不再下发（UI 文案口径 ⑤）
        for item in result.get("conflicts") or []:
            item.pop("note", None)
        return result
    except (OSError, ValueError, AttributeError):
        return {"checked_at": None, "duration_ms": 0, "total_entries": 0, "conflicts": [],
                "consistent_with_materials": 0, "unsourced": 0, "duplicates": 0, "broken_refs": 0}


# ---- K-07、K-08 提名 ----


def sanitize(text: str) -> dict:
    """脱敏检查：逐类替换，返回替换后的文字、替换位置（在替换后文字里）与各类命中数。"""
    checks = []
    for item, pattern, replacement, action in _SANITIZE:
        text, found = pattern.subn(replacement, text)
        checks.append({"item": item, "found": found, "action": action if found else "未发现"})
    highlights = []
    for replacement in sorted({entry[2] for entry in _SANITIZE}):
        start = text.find(replacement)
        while start >= 0:
            highlights.append({"start": start, "end": start + len(replacement)})
            start = text.find(replacement, start + 1)
    return {"sanitized_text": text, "highlights": sorted(highlights, key=lambda item: item["start"]), "checks": checks}


def nominations_dir() -> Path:
    return Path(config.DATA_DIR) / "knowledge" / "nominations"


def submit_nomination(project_id: str, knowledge_id: str, industry_pack: str, text: str, by: str) -> dict:
    record = {
        "nomination_id": f"nom-{uuid.uuid4().hex[:10]}", "project_id": project_id, "knowledge_id": knowledge_id,
        "industry_pack": industry_pack, "sanitized_text": text, "state": "pending_review",
        "submitted_by": by, "submitted_at": time.time(),
        "remaining_findings": sum(item["found"] for item in sanitize(text)["checks"]),
    }
    directory = nominations_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{record['nomination_id']}.json").write_text(json.dumps(record, ensure_ascii=False, indent=2),
                                                                encoding="utf-8")
    return record


def list_nominations(state: str = "") -> list[dict]:
    directory = nominations_dir()
    if not directory.is_dir():
        return []
    out = []
    for path in sorted(directory.glob("nom-*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not state or record.get("state") == state:
            out.append(record)
    return sorted(out, key=lambda item: item.get("submitted_at") or 0, reverse=True)
