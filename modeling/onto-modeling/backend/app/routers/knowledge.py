"""业务知识（API 契约 3.8）：K-01～K-08。

知识条目存放在 core：工作区 `ontology/knowledge/<type>-<slug>.md`，由工具箱 knowledge_helper
管理（助手加载的也是这一份）；写入经同一个脚本（内核资产里的副本），保存即成版本。
旧版网页写在 `knowledge/<名称>.md` 的条目只读列出（layer 仍为 project），可以删除。
启用了行业包时多一个只读的行业层（services/knowledge_layers）；项目里同 slug 的条目覆盖行业条目。
冲突检查（K-06）读接口照常可用；提名（K-07、K-08）要启用知识沉淀包和对应的行业包。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..api import deps
from ..api.errors import ApiError
from ..kernel.artifacts import index as artifact_index
from ..kernel.writepath import ChangeTx, Origin
from ..services import audit, knowledge_layers, mainline, project_events
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


def _helper(root: Path) -> Path:
    from ..services.packs.assembler import trusted_script

    return trusted_script(root, "knowledge_helper.py", deps.workspaces.assets_dir)


def _toolkit():
    from ..services.toolkit import _load

    return _load("knowledge_helper")


def _run_helper(root: Path, *args: str) -> None:
    from ..services.script_env import minimal_env

    result = subprocess.run(
        ["python3", str(_helper(root)), *args], cwd=root, capture_output=True, text=True, timeout=30, env=minimal_env(),
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
        "readonly": False,
        "revision": _content_revision(path.read_bytes()),
        "updated_at": deps.iso(stat.st_mtime),
        "_background": background,
        "_rule": rule,
        "_path": path,
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
            except (OSError, ValueError, UnicodeDecodeError):
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


def _pack_enabled(root: Path, name: str) -> bool:
    from ..services.packs import lockfile

    try:
        lock = lockfile.read(root)
    except (ValueError, OSError):
        return False
    entry = lock.get(name) if lock else None
    return bool(entry and entry.enabled)


def _emit(project_id: str, ids: list[str], change: str) -> None:
    project_events.knowledge_changed(project_id, ids, change)


@router.get("")
def list_knowledge(project_id: str, request: Request, layer: str = "all", type: str = "", q: str = "",
                   cursor: str = "", limit: int = 0):
    root = deps.require_owner(project_id, request)
    if layer not in ("all", "project", "industry"):
        raise ApiError("invalid_argument", "layer 只能是 all、project、industry", 400, {"fields": {"layer": "enum"}})
    project, industry, _, packs = _layers(root)
    entries = [_public(entry) for entry in project] + [_public(entry) for entry in industry]
    needle = q.strip().lower()
    items = [
        entry for entry in entries
        if layer in ("all", entry["layer"])
        and (not type or entry["type"] in type.split(","))
        and (not needle or needle in entry["title"].lower())
    ]
    page = deps.page(items, cursor, limit, {"layer": layer, "type": type, "q": needle})
    return {
        "items": page["items"],
        "next_cursor": page["next_cursor"],
        "counts": {"all": len(entries), "project": len(project), "industry": len(industry)},
        "layers": knowledge_layers.layers_view(project, industry, packs),
    }


@router.get("/conflicts")
def get_conflicts(project_id: str, request: Request):
    """K-06 GET：最近一次冲突检查的结果（没检查过 checked_at 为 null）。"""
    root = deps.require_owner(project_id, request)
    return knowledge_layers.last_conflicts(root)


@router.post("/conflicts")
def run_conflicts(project_id: str, request: Request):
    """K-06 POST：立即跑一次确定性检查（重复、覆盖了口径不同的行业条目、失效引用）。"""
    root = deps.require_owner(project_id, request)
    project, industry, _, _ = _layers(root)
    result = knowledge_layers.check_conflicts(root, project, industry)
    knowledge_layers.save_conflicts(root, result)
    audit.log(deps.actor(request), request.method, request.url.path,
              {"action": "knowledge.conflict_check", "project_id": project_id, "conflicts": len(result["conflicts"])})
    return result


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
    source_text = f"conversation:{source.get('conversation_id')}" if source.get("conversation_id") else "manual"
    return {
        "title": title, "type": TYPES[body["type"]], "scopes": scopes, "confidence": confidence,
        "source": source_text, "body": _compose_body(str(body.get("background_md") or ""), rule),
    }


def _saved(root: Path, project_id: str, request: Request, entry_id: str, status: int) -> JSONResponse:
    state = mainline.state(root)
    project, industry, _, _ = _layers(root)
    entry = _find(root, entry_id)
    entry["overrides_industry"] = any(item["_slug"] == entry["_slug"] for item in industry)
    # 入库后的确定性查重（同名、覆盖了口径不同的行业条目、失效引用）；需要语义判断的冲突在对话里复核
    found = knowledge_layers.check_conflicts(root, project, industry)["conflicts"]
    conflicts = [{"kind": item["kind"], "with": knowledge_layers.conflict_with(item)}
                 for item in found if item["project_entry"]["id"] == entry_id]
    _emit(project_id, [entry_id], "created" if status == 201 else "updated")
    return JSONResponse(status_code=status, content={
        "entry": _public(entry), "commit": state["head"], "version_label": state["version_label"],
        "conflicts": conflicts, "review_task": None,
    })


@router.post("")
async def create_knowledge(project_id: str, request: Request):
    """确认入库：同名同类型查重后写入并保存为新版本。"""
    root = deps.require_writable(project_id, request)
    fields = _validated(await deps.json_body(request))
    slug = _SLUG_UNSAFE.sub("-", fields["title"]).strip("-")[:60] or "entry"
    # 和行业条目同名（标题或 slug 相同）时沿用行业条目的 slug：同 slug 才算覆盖，行业条目的 slug 是英文，
    # 按中文标题生成的永远对不上；同名的有好几条时取类型也相同的
    _, industry, _, _ = _layers(root)
    same = [item for item in industry if item["title"].strip() == fields["title"] or item["_slug"] == slug]
    if same:
        slug = next((item for item in same if TYPES.get(item["type"]) == fields["type"]), same[0])["_slug"]
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
    entry = _find(root, knowledge_id, knowledge_layers.industry_entries(deps.workspaces, root))
    if entry["layer"] == "industry":
        raise ApiError("artifact_readonly", "行业知识只读；要改口径就在项目里写一条同名条目覆盖它", 403,
                       {"reason": "industry"})
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
    industry = knowledge_layers.industry_entries(deps.workspaces, root)
    entry = _find(root, knowledge_id, industry)
    if entry["layer"] == "industry":
        raise ApiError("artifact_readonly", "行业知识只读，不能删除", 403, {"reason": "industry"})
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
    restored = next((item["id"] for item in industry if item["_slug"] == entry.get("_slug")), None)
    _emit(project_id, [knowledge_id], "deleted")
    return {"commit": state["head"], "version_label": state["version_label"], "affected_artifacts": 0,
            "restored_industry_entry": restored}


# ---- K-07、K-08 提名进行业包（知识沉淀包）----


def _nomination_target(root: Path, knowledge_id: str, industry_pack: str) -> tuple[dict, object]:
    if not _pack_enabled(root, "knowledge"):
        raise ApiError("pack_not_enabled", "本项目没有启用知识沉淀能力", 409, {"pack": "knowledge"})
    packs = {view.name: view for view in knowledge_layers.industry_packs(deps.workspaces, root)}
    if industry_pack not in packs:
        raise ApiError("pack_not_enabled", "本项目没有启用这个行业包", 409, {"pack": industry_pack or None})
    entry = _find(root, knowledge_id)
    if entry["layer"] != "project":
        raise ApiError("invalid_argument", "只能提名项目里的知识条目", 400, {"fields": {"knowledge_id": "layer"}})
    return entry, packs[industry_pack]


@router.post("/{knowledge_id}/nomination-preview")
async def nomination_preview(project_id: str, knowledge_id: str, request: Request):
    """K-07：脱敏检查（客户名、内部编号、金额、联系方式与证件），给出替换后的文字。"""
    root = deps.require_owner(project_id, request)
    body = await deps.json_body(request)
    entry, pack = _nomination_target(root, knowledge_id, str(body.get("industry_pack") or ""))
    text = f"{entry['title']}\n\n{entry['_rule']}"
    if entry["_background"]:
        text += f"\n\n背景：{entry['_background']}"
    return {**knowledge_layers.sanitize(text), "pack": {"name": pack.name, "label": pack.manifest.label}}


@router.post("/{knowledge_id}/nominations")
async def submit_nomination(project_id: str, knowledge_id: str, request: Request):
    """K-08：提交提名，进管理员审核（审核接口 AD-09、AD-10）。"""
    root = deps.require_writable(project_id, request)
    body = await deps.json_body(request)
    if body.get("confirm_general") is not True:
        raise ApiError("invalid_argument", "要确认这条知识是通用的、不含单位与个人信息", 400,
                       {"fields": {"confirm_general": "required"}})
    text = str(body.get("sanitized_text") or "").strip()
    if not 1 <= len(text) <= 4000:
        raise ApiError("invalid_argument", "提名内容须为 1–4000 个字符", 400, {"fields": {"sanitized_text": "length"}})
    _, pack = _nomination_target(root, knowledge_id, str(body.get("industry_pack") or ""))
    # 脱敏是硬门槛（ADR-0044）：服务端按 K-07 同一套规则再查一遍，客户端改回去的不收
    remaining = [item for item in knowledge_layers.sanitize(text)["checks"] if item["found"]]
    if remaining:
        raise ApiError("nomination_not_sanitized", "提名内容里还有要脱敏的信息，请按脱敏预览替换后再提交", 422,
                       {"checks": remaining})
    record = knowledge_layers.submit_nomination(project_id, knowledge_id, pack.name, text, deps.actor(request))
    audit.log(deps.actor(request), request.method, request.url.path,
              {"action": "knowledge.nominate", "project_id": project_id, "knowledge_id": knowledge_id,
               "industry_pack": pack.name, "nomination_id": record["nomination_id"]})
    return JSONResponse(status_code=201, content={"nomination_id": record["nomination_id"], "state": record["state"]})
