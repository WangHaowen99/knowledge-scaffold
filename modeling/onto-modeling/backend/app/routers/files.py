"""文件与检索（API 契约 3.5）：F-01～F-08；报告（RP-01～RP-06，见 services/reports.py）。"""

from __future__ import annotations

import csv
import json
import subprocess
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Body, Request
from fastapi.responses import FileResponse, JSONResponse

from ..api import deps
from ..api.errors import ApiError
from ..kernel.metamodel import mm
from ..services import mainline, workspace_labels
from ..services.workspace.versions import version_source

router = APIRouter(prefix="/api/v1/projects/{project_id}", tags=["files"])

TEXT_PREVIEW_BYTES = 512 * 1024
FILE_WRITE_BYTES = 4 * 1024 * 1024
SUMMARY_MAX = 80
# 受保护目录：不能经通用文件接口读写（契约 2.3 path_forbidden）
PROTECTED_TOP = {".git", ".claude", ".onto", ".ontology-state", ".onto-web.json", ".imported-package"}
SEARCH_SUFFIXES = (".md", ".txt", ".py", ".mmd", ".yaml", ".yml", ".csv")
SEARCH_GROUPS = ("entries", "materials", "reports", "knowledge", "artifacts")
ENTRY_DIRS = tuple(directory for _kind, directory, _glob in mm.kind_dirs())
HTML_HEADERS = {
    "Content-Security-Policy": "sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'",
    "X-Content-Type-Options": "nosniff",
}
QUERY_KIND_ARGS = {
    "used_by": lambda term: [["--used-by", term]],
    "orphans": lambda term: [["--orphans"]],
    "search": lambda term: [["--search", term]],
    "unused": lambda term: [["--unused-objects"], ["--unused-actions"]],
    "dangling": lambda term: [["--dangling"]],
    "knowledge": lambda term: [["--knowledge", term]],
}


def _guard_path(path: str) -> str:
    rel = (path or "").strip().lstrip("/")
    if not rel:
        raise ApiError("invalid_argument", "要指定 path", 400, {"fields": {"path": "required"}})
    if "\x00" in rel:
        raise ApiError("invalid_argument", "path 不合法", 400, {"fields": {"path": "pattern"}})
    # 先规范化再判：`ontology/../.git/config`、`./.git/config` 这类写法不能绕过受保护目录
    parts = [part for part in rel.replace("\\", "/").split("/") if part not in ("", ".")]
    if not parts or ".." in parts or parts[0] in PROTECTED_TOP:
        raise ApiError("path_forbidden", "该路径不允许访问", 403, {"path": rel})
    return "/".join(parts)


def worktree_file(root: Path, rel: str) -> Path:
    """工作区里的一个文件（已过 _guard_path）：解析后不能出工作区（_resolve_inside），也不能落进受保护目录
    （根内的符号链接指到 .git、.onto 等）。"""
    target = deps.workspaces._resolve_inside(root, rel)
    resolved = target.relative_to(root.resolve()).parts
    if resolved and resolved[0] in PROTECTED_TOP:
        raise ApiError("path_forbidden", "该路径不允许访问", 403, {"path": rel})
    return target


def _target(project_id: str, request: Request, path: str) -> tuple[Path, Path, str]:
    root = deps.require_owner(project_id, request)
    rel = _guard_path(path)
    target = worktree_file(root, rel)
    if not target.is_file():
        raise ApiError("file_not_found", "文件不存在", 404, {"path": rel})
    return root, target, rel


def last_change(root: Path, rel: str) -> dict | None:
    """文件最近一次提交：版本号、来源与时间（契约 F-02 last_change）。"""
    out = mainline._git(root, "log", "-1", "--format=%H%x1f%ct%x1f%s", "--", rel)
    parts = out.split("\x1f")
    if len(parts) != 3:
        return None
    commit, ts, subject = parts
    return {
        "commit": commit,
        "version_label": mainline.label(mainline.count(root, commit)),
        "source": version_source(subject),
        "at": deps.iso(int(ts)) if ts.isdigit() else None,
    }


# ---- F-01 文件树 ----


def _subtree(nodes: list[dict], root: str) -> list[dict]:
    parts = [part for part in root.strip("/").split("/") if part]
    for part in parts:
        node = next((item for item in nodes if item["type"] == "dir" and item["name"] == part), None)
        if node is None:
            return []
        nodes = node.get("children") or []
    return nodes


@router.get("/files/tree")
def file_tree(project_id: str, request: Request, root: str = ""):
    """只读：跳过 .git、工具状态、运行期文件、以 . 开头的条目和符号链接，不再刷新索引或提交。"""
    deps.require_owner(project_id, request)
    tree = deps.workspaces.file_tree(project_id)
    return {"tree": _subtree(tree, root) if root else tree}


# ---- F-02 读、F-03 写 ----


@router.get("/files/content")
def read_file(project_id: str, request: Request, path: str = ""):
    root = deps.require_owner(project_id, request)
    rel = _guard_path(path)
    worktree_file(root, rel)  # 根内的符号链接指到受保护目录（.git 等）也不读
    data = deps.workspaces.read_file(project_id, rel, TEXT_PREVIEW_BYTES)
    from ..api.compat import entry_id_for_path
    from ..kernel.changesets import service as changesets

    scope = changesets.scope_lock(deps.workspaces, project_id)
    entry = entry_id_for_path(rel, project_id)
    locked = changesets.locked(scope, entry=entry if ":" in entry else None, path=rel)
    return {**data, "last_change": last_change(root, rel), "lock": scope if locked else None}


@router.put("/files/content")
def write_file(project_id: str, request: Request, body: dict = Body(...)):
    """经写入路径保存为一个版本；写 .py 脚本时同步脚本描述符。"""
    root = deps.require_writable(project_id, request)
    rel = _guard_path(str(body.get("path") or ""))
    content = body.get("content")
    if not isinstance(content, str):
        raise ApiError("invalid_argument", "content 必须是字符串", 400, {"fields": {"content": "string"}})
    if len(content.encode("utf-8")) > FILE_WRITE_BYTES:
        raise ApiError("payload_too_large", "文件内容超过 4 MiB", 413, {"limit_bytes": FILE_WRITE_BYTES})
    saved = deps.workspaces.write_file(
        project_id, rel, content, base_revision=str(body.get("base_revision") or ""),
        author=deps.username(request), summary=str(body.get("summary") or "")[:SUMMARY_MAX],
    )
    return {
        "path": rel,
        "size": saved["size"],
        "revision": saved["revision"],
        "commit": saved["commit"] or mainline.state(root)["head"],
        "version_label": mainline.state(root)["version_label"],
    }


# ---- F-04 原文件、F-05 表格预览、F-06 历史版本 ----


@router.get("/files/raw")
def raw_file(project_id: str, request: Request, path: str = "", download: bool = False):
    _root, target, rel = _target(project_id, request, path)
    headers = dict(HTML_HEADERS) if target.suffix.lower() in (".html", ".htm") else {}
    if download:
        headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(target.name)}"
    return FileResponse(target, headers=headers)


def _csv_preview(target: Path) -> dict:
    from ..services import excel_preview

    rows, truncated, total = [], False, 0
    with open(target, encoding="utf-8-sig", errors="replace", newline="") as stream:
        for row in csv.reader(stream):
            total += 1
            if len(rows) >= excel_preview.MAX_ROWS:
                truncated = True
                continue
            rows.append([cell for cell in row[: excel_preview.MAX_COLS]])
            truncated = truncated or len(row) > excel_preview.MAX_COLS
    sheet = {"name": target.stem, "rows": rows, "merges": [], "total_rows": total, "truncated": truncated}
    return {"format": "csv", "sheets": [sheet], "sheet_truncated": False}


@router.get("/files/table-preview")
def table_preview(project_id: str, request: Request, path: str = "", sheet: str = ""):
    from ..services import excel_preview

    _root, target, _rel = _target(project_id, request, path)
    suffix = target.suffix.lower()
    if suffix == ".csv":
        return _csv_preview(target)
    if suffix not in (".xlsx", ".xlsm"):
        raise ApiError("unsupported_media_type", "只支持 xlsx、xlsm、csv 预览；xls 请转存后上传", 415,
                       {"allowed": [".xlsx", ".xlsm", ".csv"]})
    try:
        data = excel_preview.preview(target)
    except excel_preview.ExcelPreviewError as exc:
        code = "preview_unavailable" if exc.status == 501 else "unsupported_media_type"
        raise ApiError(code, str(exc), exc.status, {"reason": str(exc)} if exc.status == 501 else None) from exc
    sheets = data["sheets"]
    if sheet:
        sheets = [item for item in sheets if item["name"] == sheet]
    return {"format": "xlsx", "sheets": sheets, "sheet_truncated": data["sheet_truncated"]}


@router.get("/files/at")
def file_at(project_id: str, request: Request, commit: str = "", path: str = ""):
    root = deps.require_owner(project_id, request)
    rel = _guard_path(path)
    data = deps.workspaces.file_at_revision(project_id, commit, rel, TEXT_PREVIEW_BYTES)
    return {
        "path": data["path"],
        "commit": data["commit"],
        "version_label": mainline.label(mainline.count(root, data["commit"])),
        "content": data["content"],
        "truncated": data["truncated"],
    }


# ---- F-07 全局搜索 ----


def _group_of(rel: str) -> str | None:
    if any(rel.startswith(f"{directory}/") for directory in ENTRY_DIRS):
        return "entries"
    if rel.startswith("resource/"):
        return "materials"
    if rel.startswith(("reports/", "research/")):
        return "reports"
    if rel.startswith("knowledge/"):
        return "knowledge"
    if rel.startswith("ext/"):
        return "artifacts"
    return None


def _hit_target(group: str, rel: str, project_id: str | None = None) -> tuple[dict, str, str]:
    from ..api.compat import entry_id_for_path

    stem = Path(rel).stem
    if group == "entries":
        entry = entry_id_for_path(rel, project_id)
        kind = entry.split(":", 1)[0]
        return {"kind": kind, "id": entry, "key": stem, "name": stem}, "studio-table", stem
    if group == "materials":
        return {"kind": "material", "path": rel}, "resources", Path(rel).name
    if group == "reports":
        from ..services import reports

        return {"kind": "report", "report_id": reports.id_of(rel)}, "reports", stem
    if group == "knowledge":
        return {"kind": "knowledge", "id": stem}, "knowledge", stem
    return {"type": "unknown", "key": stem, "title": stem}, "artifacts", stem


@router.get("/search")
def search(project_id: str, request: Request, q: str = "", groups: str = "", limit: int = 10):
    """条目、资料（文本类）、报告、知识条目的全文检索，不区分大小写；每组最多 limit 条。

    资料只搜文本类文件，docx、pdf 等要经文件提供方抽取（方案 E14，P3）后才进搜索。
    """
    root = deps.require_owner(project_id, request)
    query = (q or "").strip()
    if not 1 <= len(query) <= 200:
        raise ApiError("invalid_argument", "q 须为 1–200 个字符", 400, {"fields": {"q": "length"}})
    wanted = [group for group in (groups.split(",") if groups else SEARCH_GROUPS) if group in SEARCH_GROUPS]
    limit = max(1, min(int(limit or 10), 50))
    needle = query.lower()
    found: dict[str, list[dict]] = {group: [] for group in wanted}
    totals = {group: 0 for group in wanted}
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file() or path.suffix.lower() not in SEARCH_SUFFIXES:
            continue
        rel = path.relative_to(root).as_posix()
        group = _group_of(rel)
        if group not in found or rel.endswith("/index.md"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        lines = [line for line in text.splitlines() if needle in line.lower()]
        if not lines and needle not in path.stem.lower():
            continue
        totals[group] += 1
        if len(found[group]) < limit:
            target, page, title = _hit_target(group, rel, project_id)
            snippet = (lines[0].strip() if lines else title)[:160]
            found[group].append({"group": group, "title": title, "snippet": snippet, "target": target, "page": page})
    result_groups = [{"group": group, "total": totals[group], "items": found[group]} for group in wanted]
    referenced = sum(1 for hit in found.get("entries", []) if hit["title"].lower() != needle)
    return {
        "q": query,
        "groups": result_groups,
        "term_summary": [{
            "term": query,
            "by_group": {group: totals.get(group, 0) for group in SEARCH_GROUPS},
            "referenced_by": referenced,
        }],
    }


# ---- F-08 引用查询 ----


@router.get("/query")
def reverse_query(project_id: str, request: Request, kind: str = "", term: str = ""):
    """六种反查：跑内核资产里的确定性脚本（不跑工作区里可被改写的副本，方案 D19），返回 markdown。"""
    from ..services.script_env import minimal_env

    root = deps.require_owner(project_id, request)
    if kind not in QUERY_KIND_ARGS:
        raise ApiError("invalid_argument", f"kind 必须是 {sorted(QUERY_KIND_ARGS)} 之一", 400, {"fields": {"kind": "enum"}})
    if kind in ("used_by", "search", "knowledge") and not term.strip():
        raise ApiError("invalid_argument", "该反查类型需要提供 term 参数", 400, {"fields": {"term": "required"}})
    from ..services.packs.assembler import trusted_script

    script = trusted_script(root, "query.py", deps.workspaces.assets_dir)
    if not script.is_file():
        raise ApiError("service_unavailable", "查询脚本不存在（资产未挂载）", 503, {"dependency": "assets"})
    outputs = []
    for args in QUERY_KIND_ARGS[kind](term.strip()):
        result = subprocess.run(  # NOSONAR(S4721) 固定参数列表跑受信的查询脚本，不经 shell
            ["python3", str(script), *args], cwd=root, capture_output=True, text=True, timeout=30, env=minimal_env(),
        )
        outputs.append(result.stdout if result.returncode == 0 else f"查询失败：{result.stderr[:200]}")
    return {"markdown": "\n\n".join(outputs)}


# ---- 报告（RP-01～RP-06）：reports/ 下的检查报告、research/ 下的资料分析报告、内核算的链接检查 ----

ITEM_STATUSES = ("expert", "fixing", "pending", "fixed", "skipped")


def _report_call(func, *args, **kwargs):
    from ..services import reports

    try:
        return func(*args, **kwargs)
    except reports.ReportError as exc:
        raise ApiError(exc.code, str(exc), exc.status, exc.details) from exc


@router.get("/reports")
def report_list(project_id: str, request: Request, type: str = "", cursor: str = "", limit: int = 0):
    """RP-01：新的在前；type 可多值（逗号分隔）。"""
    from ..services import reports

    deps.require_owner(project_id, request)
    types = [item for item in type.split(",") if item]
    rows = reports.listing(deps.workspaces, project_id, types)
    page = deps.page(rows, cursor, limit, {"type": sorted(types)}, default=50)
    return {"items": page["items"], "next_cursor": page["next_cursor"]}


@router.post("/reports/recheck")
async def report_recheck(project_id: str, request: Request):
    """RP-06：链接检查同步算（200 {report}），其余发校验命令（202 {command_task}）。"""
    from ..services import reports
    from . import sessions as session_routes

    root = deps.require_writable(project_id, request)
    body = await deps.json_body(request)
    kind = str(body.get("kind") or "")
    mode = str(body.get("mode") or "regression")
    if kind not in (*reports.RECHECK_COMMANDS, "link"):
        raise ApiError("invalid_argument", "kind 只能是 rule、semantic、link、coverage、diagram", 400,
                       {"fields": {"kind": "enum"}})
    if mode not in ("regression", "full"):
        raise ApiError("invalid_argument", "mode 只能是 regression 或 full", 400, {"fields": {"mode": "enum"}})
    if kind == "link":
        return {"report": _report_call(reports.link_check, deps.workspaces, project_id)}
    payload = {"command": reports.RECHECK_COMMANDS[kind], "origin": {"page": "reports", "label": "重新校验"},
               "text": "只查改动" if mode == "regression" else "", "conversation_id": "-"}
    response = await session_routes.dispatch_command(project_id, root, request, payload)
    return JSONResponse(status_code=202, content={"command_task": json.loads(response.body)["task"]})


@router.get("/reports/{report_id:path}/items")
def report_items(project_id: str, report_id: str, request: Request, status: str = "", level: str = "",
                 cursor: str = "", limit: int = 0):
    """RP-03：counts 按全部条目算，不受筛选影响。"""
    from ..services import reports

    deps.require_owner(project_id, request)
    if status and status not in ITEM_STATUSES:
        raise ApiError("invalid_argument", "status 取值不对", 400, {"fields": {"status": "enum"}})
    if level and level not in ("error", "warning"):
        raise ApiError("invalid_argument", "level 只能是 error 或 warning", 400, {"fields": {"level": "enum"}})
    items = _report_call(reports.all_items, deps.workspaces, project_id, report_id)
    counts = {
        "error": sum(1 for item in items if item["level"] == "error"),
        "warning": sum(1 for item in items if item["level"] == "warning"),
        "fixed": sum(1 for item in items if item["status"] == "fixed"),
        "skipped": sum(1 for item in items if item["status"] == "skipped"),
    }
    picked = [item for item in items if (not status or item["status"] == status) and (not level or item["level"] == level)]
    page = deps.page(picked, cursor, limit, {"report": report_id, "status": status, "level": level}, default=100)
    return {"items": page["items"], "next_cursor": page["next_cursor"], "counts": counts}


@router.get("/reports/{report_id:path}/raw")
def report_raw(project_id: str, report_id: str, request: Request):
    """RP-04：报告原文。"""
    from ..services import reports

    deps.require_owner(project_id, request)
    return _report_call(reports.raw, deps.workspaces, project_id, report_id)


@router.post("/reports/{report_id:path}/fix")
async def report_fix(project_id: str, report_id: str, request: Request):
    """RP-05：把还没修复的条目发给助手（同 S-20，发校验命令并带上条目）；不传 item_ids 是全部未修复的。"""
    from ..services import reports
    from . import sessions as session_routes

    root = deps.require_writable(project_id, request)
    body = await deps.json_body(request)
    wanted = body.get("item_ids")
    if wanted is not None and (not isinstance(wanted, list) or not all(isinstance(item, str) for item in wanted)):
        raise ApiError("invalid_argument", "item_ids 要是文字列表", 400, {"fields": {"item_ids": "list"}})
    items = _report_call(reports.all_items, deps.workspaces, project_id, report_id)
    targets = [item for item in items if item["status"] != "fixed" and (wanted is None or item["id"] in wanted)]
    if not targets:
        raise ApiError("invalid_argument", "没有要修复的条目", 400, {"fields": {"item_ids": "fixed"}})
    rel = reports.rel_of(report_id) if report_id != reports.LINK_ID else "链接检查报告"
    lines = "\n".join(f"{n}. {item['code'] + ' ' if item['code'] else ''}{item['text']}" for n, item in enumerate(targets, 1))
    shown = rel if report_id == reports.LINK_ID else workspace_labels.path_label(rel) or "检查报告"
    payload = {"command": "/ontology-validate", "origin": {"page": "reports", "label": "发给助手"},
               "text": f"修复报告 {rel} 里的这些条目，改完重新检查：\n{lines}", "conversation_id": "-",
               "display_text": f"修复{shown}里的这些条目，改完重新检查：\n{lines}"}
    return await session_routes.dispatch_command(project_id, root, request, payload)


@router.get("/reports/{report_id:path}")
def report_detail(project_id: str, report_id: str, request: Request):
    """RP-02：契约 7.10 Report；资料分析报告的 extras 是资料读取结果（MaterialMap 的摘要）。"""
    from ..services import reports

    deps.require_owner(project_id, request)
    return _report_call(reports.detail, deps.workspaces, project_id, report_id)
