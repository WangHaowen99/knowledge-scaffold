"""业务知识（API 契约 3.8）：K-01 列表、K-02 详情、K-03 新增、K-04 修改、K-05 删除。

知识条目存放在 core：工作区 `ontology/knowledge/<type>-<slug>.md`，由工具箱 knowledge_helper
管理（助手加载的也是这一份）；写入经同一个脚本（内核资产里的副本），保存即成版本。
旧版网页写在 `knowledge/<名称>.md` 的条目只读列出（layer 仍为 project），可以删除。
冲突检查、提名（K-06～K-08）随知识沉淀包（B3/B4）。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..api import deps
from ..api.errors import ApiError
from ..kernel.writepath import ChangeTx, Origin
from ..services import mainline
from ..services.workspace.layout import _content_revision

router = APIRouter(prefix="/api/v1/projects/{project_id}/knowledge", tags=["knowledge"])

KNOWLEDGE_DIR = "ontology/knowledge"
LEGACY_DIR = "knowledge"
# 契约的类型名 ↔ 工具箱的类型名
TYPES = {"correction": "correction", "convention": "convention", "term": "terminology", "domain_rule": "domain-rule"}
TYPES_BACK = {value: key for key, value in TYPES.items()}
CONFIDENCE = ("high", "medium", "low")
TITLE_MAX = 60
_SECTION_RE = re.compile(r"^##\s*(背景|规则)\s*$", re.M)
_SLUG_UNSAFE = re.compile(r"[\\/:*?\"<>|.\s]+")


def _helper() -> Path:
    return deps.workspaces.assets_dir / "workspace-toolkit" / "scripts" / "knowledge_helper.py"


def _toolkit():
    from ..services.toolkit import _load

    return _load("knowledge_helper")


def _run_helper(root: Path, *args: str) -> None:
    from ..services.script_env import minimal_env

    result = subprocess.run(
        ["python3", str(_helper()), *args], cwd=root, capture_output=True, text=True, timeout=30, env=minimal_env(),
    )
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip().splitlines()
        raise ApiError("invalid_argument", message[-1] if message else "知识条目保存失败", 400)


def _split_body(body: str) -> tuple[str, str]:
    parts = _SECTION_RE.split(body or "")
    sections = {parts[i]: parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}
    if not sections:
        return "", (body or "").strip()
    return sections.get("背景", ""), sections.get("规则", "")


def _compose_body(background: str, rule: str) -> str:
    text = f"## 规则\n\n{rule.strip()}\n"
    if background.strip():
        text = f"## 背景\n\n{background.strip()}\n\n" + text
    return text


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
    return {
        "id": path.stem,
        "title": str(fm.get("name") or path.stem),
        "layer": "project",
        "pack": None,
        "type": TYPES_BACK.get(str(fm.get("type")), "convention"),
        "scope": _scope_view(list(fm.get("scopes") or [])),
        "confidence": fm.get("confidence") if fm.get("confidence") in CONFIDENCE else "medium",
        "source": {"kind": "conversation" if source.startswith("conversation") else "material", "label": source or "网页",
                   "at": deps.iso(stat.st_mtime)},
        "overrides_industry": False,
        "readonly": False,
        "revision": _content_revision(path.read_bytes()),
        "updated_at": deps.iso(stat.st_mtime),
        "_background": background,
        "_rule": rule,
        "_path": path,
    }


def _public(entry: dict) -> dict:
    return {key: value for key, value in entry.items() if not key.startswith("_")}


def _entries(root: Path) -> list[dict]:
    found = []
    for directory, legacy in ((root / KNOWLEDGE_DIR, False), (root / LEGACY_DIR, True)):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            if path.name == "INDEX.md" or path.is_symlink():
                continue
            try:
                found.append(_entry(path, legacy))
            except (OSError, ValueError, UnicodeDecodeError):
                continue
    return found


def _find(root: Path, knowledge_id: str) -> dict:
    for entry in _entries(root):
        if entry["id"] == knowledge_id:
            return entry
    raise ApiError("not_found", "知识条目不存在", 404)


@router.get("")
def list_knowledge(project_id: str, request: Request, layer: str = "all", type: str = "", q: str = "",
                   cursor: str = "", limit: int = 0):
    root = deps.require_owner(project_id, request)
    entries = [_public(entry) for entry in _entries(root)]
    needle = q.strip().lower()
    items = [
        entry for entry in entries
        if layer in ("all", "project")
        and (not type or entry["type"] in type.split(","))
        and (not needle or needle in entry["title"].lower())
    ]
    page = deps.page(items, cursor, limit, {"layer": layer, "type": type, "q": needle})
    return {
        "items": page["items"],
        "next_cursor": page["next_cursor"],
        "counts": {"all": len(entries), "project": len(entries), "industry": 0},
        "layers": [{"layer": "project", "label": "项目知识", "count": len(entries), "overridden": 0}],
        "loading_rule": "助手按条目的适用范围加载项目知识；没有启用行业包时只有项目层。",
    }


@router.get("/{knowledge_id}")
def get_knowledge(project_id: str, knowledge_id: str, request: Request):
    root = deps.require_owner(project_id, request)
    entry = _find(root, knowledge_id)
    return {
        **_public(entry),
        "background_md": entry["_background"], "rule_md": entry["_rule"],
        "source_quote": None, "overrides": None, "used_by": [],
    }


def _validated(body: dict) -> dict:
    title = str(body.get("title") or "").strip()
    if not 1 <= len(title) <= TITLE_MAX:
        raise ApiError("invalid_argument", f"标题须为 1–{TITLE_MAX} 个字符", 400, {"fields": {"title": "length"}})
    if body.get("type") not in TYPES:
        raise ApiError("invalid_argument", "type 取值不合法", 400, {"fields": {"type": "enum"}})
    rule = str(body.get("rule_md") or "").strip()
    if not rule:
        raise ApiError("invalid_argument", "要填写规则内容", 400, {"fields": {"rule_md": "required"}})
    scope = body.get("scope") or {"kind": "global"}
    if scope.get("kind") == "entries":
        scopes = [str(item.get("id") or item.get("key")) for item in scope.get("entries") or [] if isinstance(item, dict)]
        if not scopes:
            raise ApiError("invalid_argument", "适用条目不能为空", 400, {"fields": {"scope.entries": "required"}})
    else:
        scopes = ["global"]
    confidence = body.get("confidence") or "medium"
    if confidence not in CONFIDENCE:
        raise ApiError("invalid_argument", "confidence 取值不合法", 400, {"fields": {"confidence": "enum"}})
    source = body.get("source") or {}
    source_text = f"conversation:{source.get('conversation_id')}" if source.get("conversation_id") else "web"
    return {
        "title": title, "type": TYPES[body["type"]], "scopes": scopes, "confidence": confidence,
        "source": source_text, "body": _compose_body(str(body.get("background_md") or ""), rule),
    }


def _saved(root: Path, project_id: str, request: Request, entry_id: str, status: int) -> JSONResponse:
    state = mainline.state(root)
    entry = _find(root, entry_id)
    return JSONResponse(status_code=status, content={
        "entry": _public(entry), "commit": state["head"], "version_label": state["version_label"],
        "conflicts": [], "review_task": None,
    })


@router.post("")
async def create_knowledge(project_id: str, request: Request):
    """确认入库：同名同类型查重后写入并保存为新版本。"""
    root = deps.require_writable(project_id, request)
    fields = _validated(await deps.json_body(request))
    slug = _SLUG_UNSAFE.sub("-", fields["title"]).strip("-")[:60] or "entry"
    entry_id = f"{fields['type']}-{slug}"
    if (root / KNOWLEDGE_DIR / f"{entry_id}.md").exists():
        raise ApiError("knowledge_duplicate", "已有同名的知识条目", 409, {"conflicts": [entry_id]})
    with ChangeTx(deps.workspaces, project_id, Origin("web", deps.actor(request)), summary="新增知识条目") as tx:
        tx.touch(KNOWLEDGE_DIR, snapshot=False)
        args = ["add", "--type", fields["type"], "--slug", slug, "--name", fields["title"],
                "--confidence", fields["confidence"], "--source", fields["source"], "--body", fields["body"],
                "--after-conflict-check", "none-found"]
        for scope in fields["scopes"]:
            args += ["--scope", scope]
        _run_helper(root, *args)
        tx.commit(f"knowledge: 新增 {fields['title']} by {deps.actor(request)}")
    return _saved(root, project_id, request, entry_id, 201)


@router.put("/{knowledge_id}")
async def update_knowledge(project_id: str, knowledge_id: str, request: Request):
    """修改：base_revision 必填，正文改动记进条目的修订历史；类型不能改（换类型请新增一条）。"""
    root = deps.require_writable(project_id, request)
    body = await deps.json_body(request)
    fields = _validated(body)
    entry = _find(root, knowledge_id)
    path: Path = entry["_path"]
    if path.parent.name != "knowledge" or path.parent.parent.name != "ontology":
        raise ApiError("artifact_readonly", "旧版知识条目只读，请新增一条后删除它", 403, {"reason": "legacy"})
    if str(body.get("base_revision") or "") != entry["revision"]:
        raise ApiError("revision_conflict", "知识条目已被修改，请刷新后再保存", 409,
                       {"target": knowledge_id, "expected": body.get("base_revision"), "actual": entry["revision"]})
    toolkit = _toolkit()
    fm, _old = toolkit.parse_frontmatter(path.read_text(encoding="utf-8"))
    if fm.get("type") != fields["type"]:
        raise ApiError("invalid_argument", "知识条目的类型不能修改", 400, {"fields": {"type": "immutable"}})
    with ChangeTx(deps.workspaces, project_id, Origin("web", deps.actor(request)), summary="修改知识条目") as tx:
        tx.touch(path.relative_to(root).as_posix())
        tx.touch(KNOWLEDGE_DIR, snapshot=False)
        _run_helper(root, "revise", "--slug", str(fm["slug"]), "--summary", "网页修改", "--source", fields["source"],
                    "--body", fields["body"])
        fm, text = toolkit.parse_frontmatter(path.read_text(encoding="utf-8"))
        fm.update({"name": fields["title"], "scopes": fields["scopes"], "confidence": fields["confidence"]})
        path.write_text(toolkit.dump_frontmatter(fm, text), encoding="utf-8")
        _run_helper(root, "sync-index")
        tx.commit(f"knowledge: 修改 {fields['title']} by {deps.actor(request)}")
    return _saved(root, project_id, request, knowledge_id, 200)


@router.delete("/{knowledge_id}")
async def delete_knowledge(project_id: str, knowledge_id: str, request: Request):
    root = deps.require_writable(project_id, request)
    body = await deps.json_body(request)
    entry = _find(root, knowledge_id)
    base = str(body.get("base_revision") or "")
    if base and base != entry["revision"]:
        raise ApiError("revision_conflict", "知识条目已被修改，请刷新后再删除", 409,
                       {"target": knowledge_id, "expected": base, "actual": entry["revision"]})
    path: Path = entry["_path"]
    rel = path.relative_to(root).as_posix()
    with ChangeTx(deps.workspaces, project_id, Origin("web", deps.actor(request)), summary=f"删除 {rel}") as tx:
        tx.remove(rel)
        if rel.startswith(f"{KNOWLEDGE_DIR}/"):
            tx.touch(f"{KNOWLEDGE_DIR}/INDEX.md")
            _run_helper(root, "sync-index")
        tx.commit(f"knowledge: 删除 {rel} by {deps.actor(request)}")
    state = mainline.state(root)
    return {"commit": state["head"], "version_label": state["version_label"], "affected_artifacts": 0,
            "restored_industry_entry": None}
