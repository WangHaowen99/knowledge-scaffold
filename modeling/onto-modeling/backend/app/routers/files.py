"""文件与检索（API 契约 3.5）：F-01～F-08；报告详情与条目（RP-02、RP-03，报告即 reports/ 下的文件）。"""

from __future__ import annotations

import csv
import subprocess
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Body, Request
from fastapi.responses import FileResponse

from ..api import deps
from ..api.errors import ApiError
from ..kernel.metamodel import mm
from ..services import mainline
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
    if rel.split("/", 1)[0] in PROTECTED_TOP:
        raise ApiError("path_forbidden", "该路径不允许访问", 403, {"path": rel})
    return rel


def _target(project_id: str, request: Request, path: str) -> tuple[Path, Path, str]:
    root = deps.require_owner(project_id, request)
    rel = _guard_path(path)
    target = deps.workspaces._resolve_inside(root, rel)
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
    data = deps.workspaces.read_file(project_id, rel, TEXT_PREVIEW_BYTES)
    return {**data, "last_change": last_change(root, rel), "lock": None}


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


def _hit_target(group: str, rel: str) -> tuple[dict, str, str]:
    from ..api.compat import entry_id_for_path

    stem = Path(rel).stem
    if group == "entries":
        entry = entry_id_for_path(rel)
        kind = entry.split(":", 1)[0]
        return {"kind": kind, "id": entry, "key": stem, "name": stem}, "studio-table", stem
    if group == "materials":
        return {"kind": "material", "path": rel}, "resources", Path(rel).name
    if group == "reports":
        return {"kind": "report", "report_id": rel.removeprefix("reports/").removesuffix(".md")}, "reports", stem
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
            target, page, title = _hit_target(group, rel)
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
    script = deps.workspaces.assets_dir / "workspace-toolkit" / "scripts" / "query.py"
    if not script.is_file():
        raise ApiError("service_unavailable", "查询脚本不存在（资产未挂载）", 503, {"dependency": "assets"})
    outputs = []
    for args in QUERY_KIND_ARGS[kind](term.strip()):
        result = subprocess.run(
            ["python3", str(script), *args], cwd=root, capture_output=True, text=True, timeout=30, env=minimal_env(),
        )
        outputs.append(result.stdout if result.returncode == 0 else f"查询失败：{result.stderr[:200]}")
    return {"markdown": "\n\n".join(outputs)}


# ---- RP-02 报告详情、RP-03 报告条目（报告即 reports/ 下的 Markdown） ----


def _report(project_id: str, request: Request, report_id: str) -> tuple[str, dict]:
    from ..services import report_items

    rel = f"reports/{report_id.strip('/')}.md"
    if ".." in rel.split("/"):
        raise ApiError("path_forbidden", "路径越界", 403, {"path": rel})
    data = deps.workspaces.read_file(project_id, rel, TEXT_PREVIEW_BYTES)
    return rel, report_items.parse(data["content"])


def _item_view(item: dict) -> dict:
    return {
        "id": str(item.get("id")),
        "group": item.get("group", ""),
        "text_md": item.get("text", ""),
        "level": item.get("severity", "error"),
        "status": "pending",
    }


@router.get("/reports/{report_id:path}/items")
def report_items(project_id: str, report_id: str, request: Request, cursor: str = "", limit: int = 0):
    deps.require_owner(project_id, request)
    _rel, parsed = _report(project_id, request, report_id)
    items = [_item_view(item) for item in parsed.get("items") or []]
    counts = {
        "error": sum(1 for item in items if item["level"] == "error"),
        "warning": sum(1 for item in items if item["level"] == "warning"),
        "fixed": 0,
    }
    page = deps.page(items, cursor, limit, {"report": report_id}, default=100)
    return {"items": page["items"], "next_cursor": page["next_cursor"], "counts": counts}


@router.get("/reports/{report_id:path}")
def report_detail(project_id: str, report_id: str, request: Request):
    """报告的结构化视图：报告格式登记（C13，P2）之前按现有的启发式解析给摘要与条目。"""
    root = deps.require_owner(project_id, request)
    rel, parsed = _report(project_id, request, report_id)
    items = [_item_view(item) for item in parsed.get("items") or []]
    return {
        "id": report_id,
        "path": rel,
        "title": Path(rel).stem,
        "type": "codegen" if "candidates" in parsed else None,
        "summary": parsed.get("summary", ""),
        "last_change": last_change(root, rel),
        "items": items[:100],
        "item_total": len(items),
        "repair": None,
        "arbitration": None,
        "extras": {key: value for key, value in parsed.items() if key not in ("items", "summary")},
    }
