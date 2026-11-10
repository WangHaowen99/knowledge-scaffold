"""报告（契约 3.15 RP-01～RP-06）：`reports/` 下的检查报告、`research/` 下的资料分析报告、内核算的链接检查。

报告编号：
- `reports/<相对路径>.md` → `<相对路径>`（和待处理里报告事项的来源编号一致）；
- `research/<相对路径>.md` → `research:<相对路径>`；
- 链接检查（RP-06 `kind=link` 由内核确定性计算，结果记在 `.onto/run/link-report.json`）→ `link`。

报告格式登记（详设 5.9.4、方案 C13）之前，条目按 `report_items` 的启发式解析；类型按命令约定的文件名认。
助手这一轮写的报告还没合入时只在变更集副本里：主线上找不到时从副本读（只读）。
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path, PurePosixPath

from . import report_items, research_view, workspace_labels

RESEARCH_PREFIX = "research:"
LINK_ID = "link"
LINK_REL = Path(".onto") / "run" / "link-report.json"
TEXT_BYTES = 512 * 1024
ITEMS_PAGE = 100
TYPE_LABELS = {
    "rule": "规则", "link": "链接", "research": "资料", "semantic": "语义", "trace": "追踪", "candidate": "候选",
    "coverage": "覆盖", "diagram": "流程图校验", "codegen": "代码生成", "consistency": "全局一致性", "other": "报告",
}
# 命令约定的报告文件名（core 与官方包的命令文档）→ 类型
_TYPE_PATTERNS = (
    ("codegen", re.compile(r"codegen-candidates")),
    ("consistency", re.compile(r"^consistency/")),
    ("trace", re.compile(r"-trace-|^trace")),
    ("diagram", re.compile(r"diagrams?-")),
    ("semantic", re.compile(r"-semantic$")),
    ("rule", re.compile(r"-rule$")),
    ("coverage", re.compile(r"coverage")),
)
_CODE = re.compile(r"^([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+)\s+")
_CHECKBOX = re.compile(r"^\[([ xX])\]\s*")
# 处理过的条目：修好的和用户明确跳过的
HANDLED = ("fixed", "skipped")
# 校验命令收尾时把处理过的条目换成标记：✅ 修好、🔧 自动修复、⏭️ 用户明确跳过（只有 `[ ]` 算没处理，同流程探针）
_DONE_MARK = re.compile(r"^(?:✅|🔧)\s*")
_SKIP_MARK = re.compile(r"^⏭\ufe0f?\s*")
_LINK = re.compile(r"\[\[([^\]|#]+)")
RECHECK_COMMANDS = {"rule": "/ontology-validate", "semantic": "/ontology-validate", "coverage": "/ontology-validate",
                    "diagram": "/ontology-validate-diagrams"}


class ReportError(Exception):
    def __init__(self, message: str, status: int, code: str, details: dict | None = None):
        super().__init__(message)
        self.status, self.code, self.details = status, code, details


# ---------------------------------------------------------------------------
# 定位
# ---------------------------------------------------------------------------


def _copy(manager, project_id: str) -> Path | None:
    from ..kernel.changesets import service as changesets, store

    root = manager.project_root(project_id)
    record = store.current(root)
    if record and record.get("state") in ("open", "pending_confirm", "conflict"):
        return changesets.existing_copy(root)
    return None


def rel_of(report_id: str) -> str:
    report_id = (report_id or "").strip().strip("/")
    if report_id.startswith(RESEARCH_PREFIX):
        rel = f"research/{report_id[len(RESEARCH_PREFIX):]}.md"
    else:
        rel = f"reports/{report_id}.md"
    if not report_id or ".." in rel.split("/"):
        raise ReportError("报告不存在", 404, "not_found")
    return rel


def id_of(rel: str) -> str:
    if rel.startswith("research/"):
        return RESEARCH_PREFIX + rel[len("research/"):].removesuffix(".md")
    return rel[len("reports/"):].removesuffix(".md")


def locate(manager, project_id: str, report_id: str) -> tuple[Path, str]:
    """(读哪个目录, 相对路径)：主线上没有、这一轮的副本里有时读副本。"""
    rel = rel_of(report_id)
    root = manager.project_root(project_id)
    places = [research_view.base(manager, project_id)] if rel.startswith("research/") else [root]
    copy = _copy(manager, project_id)
    if copy is not None and copy not in places:
        places.append(copy)
    for where in places:
        if _file(where, rel) is not None:
            return where, rel
    raise ReportError("报告不存在", 404, "not_found", {"report_id": report_id})


def report_type(rel: str) -> str:
    if rel.startswith("research/"):
        return "research"
    name = rel[len("reports/"):].removesuffix(".md")
    return next((kind for kind, pattern in _TYPE_PATTERNS if pattern.search(name)), "other")


def _file(where: Path, rel: str) -> Path | None:
    """报告文件：路径上没有符号链接、仍在这个目录里（助手能在副本里建链接指到任意文件）。"""
    from ..kernel.artifacts import index

    path = index.inside_no_links(where, rel)
    return path if path is not None and path.is_file() else None


def _read(where: Path, rel: str) -> tuple[str, bool]:
    path = _file(where, rel)
    if path is None:
        raise ReportError("报告不存在", 404, "not_found")
    data = path.read_bytes()
    return data[:TEXT_BYTES].decode("utf-8", errors="replace"), len(data) > TEXT_BYTES


def _title(text: str, rel: str) -> str:
    if rel.startswith("research/"):
        # 资料分析报告按模板第一节就是「# 分析主题」，文件名才是主题
        return Path(rel).stem[:120]
    match = re.search(r"^#\s+(.+)$", text, re.M)
    return (match.group(1).strip() if match else Path(rel).stem)[:120]


def _at(where: Path, rel: str) -> str | None:
    from ..api import deps
    from . import mainline

    out = mainline._git(where, "log", "-1", "--format=%ct", "--", rel)
    stamp = int(out) if out.strip().isdigit() else (where / rel).stat().st_mtime
    return deps.iso(stamp)


# ---------------------------------------------------------------------------
# 条目
# ---------------------------------------------------------------------------


def _entry_by_name(root: Path, name: str) -> dict | None:
    from ..kernel.metamodel import mm
    from . import project_events

    for _kind, directory, _glob in mm.kind_dirs():
        rel = f"{directory}/{name.strip()}.md"
        if (Path(root) / rel).is_file():
            refs = project_events.entry_refs(root, [rel])
            if refs:
                return refs[0]
    return None


def items_of(root: Path, text: str) -> list[dict]:
    """ReportItem：`- [x]` 的算已修复；开头的规则码（如 E-ROUTE-01）进 code；正文里第一个 [[条目]] 作定位。"""
    parsed = report_items.parse(text)
    out = []
    for raw in parsed.get("items") or []:
        content = str(raw.get("text") or "")
        status = "pending"
        if match := _CHECKBOX.match(content):
            status = "fixed" if match.group(1) != " " else "pending"
            content = content[match.end():]
        elif match := _DONE_MARK.match(content):
            status, content = "fixed", content[match.end():]
        elif match := _SKIP_MARK.match(content):
            status, content = "skipped", content[match.end():]
        code = None
        if match := _CODE.match(content):
            code, content = match.group(1), content[match.end():]
        level = raw.get("severity", "error")
        # 分在「警告」小节下、或规则码是 W- 开头的是警告
        if (code or "").startswith("W-") or "警告" in str(raw.get("group") or "") or "warning" in str(raw.get("group") or "").lower():
            level = "warning"
        elif (code or "").startswith("E-") or "错误" in str(raw.get("group") or ""):
            level = "error"
        target = None
        if match := _LINK.search(content):
            target = _entry_by_name(root, match.group(1))
        out.append({
            "id": str(raw.get("id")), "code": code, "level": level, "text": content,
            # 小节标题可能是条目文件路径（按文件分组的报告）：界面上换成业务名（F4b-paths）
            "location": {"label": workspace_labels.humanize(raw.get("group") or ""), "target": target, "page": "studio-table" if target else None},
            "status": status, "status_note": "",
        })
    return out


def _summary(items: list[dict]) -> str:
    errors = sum(1 for item in items if item["level"] == "error" and item["status"] not in HANDLED)
    warnings = sum(1 for item in items if item["level"] == "warning" and item["status"] not in HANDLED)
    parts = [f"错误 {errors}" if errors else "", f"警告 {warnings}" if warnings else ""]
    return " · ".join(part for part in parts if part) or "没有问题"


# ---------------------------------------------------------------------------
# 列表与详情
# ---------------------------------------------------------------------------


def _paths(manager, project_id: str) -> list[tuple[Path, str]]:
    root = manager.project_root(project_id)
    found: dict[str, Path] = {}
    folder = root / "reports"
    for path in sorted(folder.rglob("*.md")) if folder.is_dir() and not folder.is_symlink() else []:
        if path.is_file() and not path.is_symlink():
            found.setdefault(path.relative_to(root).as_posix(), root)
    where = research_view.base(manager, project_id)
    for rel in research_view.report_paths(where):
        if _file(where, rel) is not None:
            found.setdefault(rel, where)
    copy = _copy(manager, project_id)
    copy_reports = copy / "reports" if copy is not None else None
    if copy_reports is not None and copy_reports.is_dir() and not copy_reports.is_symlink():
        for path in sorted(copy_reports.rglob("*.md")):
            if path.is_file() and not path.is_symlink():
                found.setdefault(path.relative_to(copy).as_posix(), copy)
    # 各目录里的占位索引（index.md，工作区模板自带）不是报告
    from ..kernel.metamodel import mm

    return [(where, rel) for rel, where in found.items() if PurePosixPath(rel).name != mm.INDEX_FILE]


def summary_of(where: Path, rel: str, manager=None, project_id: str = "") -> dict:
    text, _truncated = _read(where, rel)
    kind = report_type(rel)
    if kind == "research" and manager is not None:
        checked = research_view.verification(manager, project_id)
        summary = f"核验 {checked['passed']}/{checked['total']}" if checked["total"] else "还没有业务项"
    else:
        summary = _summary(items_of(where, text))
    return {"id": id_of(rel), "name": _title(text, rel), "type": kind, "type_label": TYPE_LABELS[kind],
            "at": _at(where, rel), "summary": summary, "path": rel}


def listing(manager, project_id: str, types: list[str]) -> list[dict]:
    rows = [summary_of(where, rel, manager, project_id) for where, rel in _paths(manager, project_id)]
    link = _link_cached(manager.project_root(project_id))
    if link:
        rows.append({key: link[key] for key in ("id", "name", "type", "type_label", "at", "summary", "path")})
    if types:
        rows = [row for row in rows if row["type"] in types]
    return sorted(rows, key=lambda row: row["at"] or "", reverse=True)


def detail(manager, project_id: str, report_id: str) -> dict:
    """契约 7.10 Report（处理进度不带说明，UI 文案口径 ⑤）。"""
    if report_id == LINK_ID:
        cached = _link_cached(manager.project_root(project_id))
        if not cached:
            raise ReportError("报告不存在", 404, "not_found", {"report_id": report_id})
        return {key: value for key, value in cached.items() if key != "content_md"}
    where, rel = locate(manager, project_id, report_id)
    text, _truncated = _read(where, rel)
    summary = summary_of(where, rel, manager, project_id)
    if summary["type"] == "research":
        extras = research_view.report_extras(manager, project_id)
        checked = extras["verification"]
        return {**summary, "scope_text": "资料区", "mode": "research", "duration_ms": None, "previous": None,
                "progress": {"handled": checked["passed"], "total": checked["total"]},
                "items": [], "items_next_cursor": None, "repair": None, "arbitration": None, "extras": extras}
    items = items_of(where, text)
    parsed = report_items.parse(text)
    extras = {key: value for key, value in parsed.items() if key not in ("items", "summary")} or None
    return {**summary, "scope_text": "", "mode": "full", "duration_ms": None, "previous": None,
            "progress": {"handled": sum(1 for item in items if item["status"] in HANDLED), "total": len(items)},
            "items": items[:ITEMS_PAGE], "items_next_cursor": None, "repair": None, "arbitration": None,
            "extras": extras}


def all_items(manager, project_id: str, report_id: str) -> list[dict]:
    if report_id == LINK_ID:
        cached = _link_cached(manager.project_root(project_id))
        if not cached:
            raise ReportError("报告不存在", 404, "not_found", {"report_id": report_id})
        return cached["items"]
    where, rel = locate(manager, project_id, report_id)
    if rel.startswith("research/"):
        return []
    return items_of(where, _read(where, rel)[0])


def raw(manager, project_id: str, report_id: str) -> dict:
    if report_id == LINK_ID:
        cached = _link_cached(manager.project_root(project_id))
        if not cached:
            raise ReportError("报告不存在", 404, "not_found", {"report_id": report_id})
        return {"path": "", "content_md": cached.get("content_md", ""), "truncated": False}
    where, rel = locate(manager, project_id, report_id)
    text, truncated = _read(where, rel)
    return {"path": rel, "content_md": text, "truncated": truncated}


# ---------------------------------------------------------------------------
# 链接检查（内核确定性计算）
# ---------------------------------------------------------------------------


def _link_cached(root: Path) -> dict | None:
    if (Path(root) / LINK_REL).is_symlink():
        return None
    try:
        data = json.loads((Path(root) / LINK_REL).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("id") == LINK_ID else None


def _query(root: Path, script: Path, flag: str):
    from .script_env import minimal_env

    result = subprocess.run(["python3", str(script), flag, "--json"], cwd=root, capture_output=True, text=True,
                            timeout=30, env=minimal_env())
    if result.returncode != 0:
        raise ReportError("链接检查没跑起来", 503, "service_unavailable", {"dependency": "query"})
    return json.loads(result.stdout or "null")


def link_check(manager, project_id: str) -> dict:
    """孤立（没有任何链接进出）、未用（没有被操作引用的对象）、失效链接；同步算完记下，作为报告 `link`。"""
    from .packs.assembler import trusted_script

    root = manager.project_root(project_id)
    previous = _link_cached(root)
    script = trusted_script(root, "query.py", manager.assets_dir)
    if not script.is_file():
        raise ReportError("查询脚本不存在（资产未挂载）", 503, "service_unavailable", {"dependency": "assets"})
    started = time.time()
    if not (root / "ontology").is_dir():
        orphans, unused, dangling = {}, [], []
    else:
        orphans = _query(root, script, "--orphans") or {}
        unused = _query(root, script, "--unused-objects") or []
        dangling = _query(root, script, "--dangling") or []
    items = []
    for stems in orphans.values():
        for stem in stems:
            items.append(("LINK-ORPHAN", f"[[{stem}]]", "没有任何链接进出"))
    for stem in unused:
        items.append(("LINK-UNUSED", f"[[{stem}]]", "没有被任何操作引用"))
    for row in dangling:
        items.append(("LINK-DANGLING", f"[[{row.get('broken')}]]", f"指向的条目不存在（{row.get('source')}）"))
    views = []
    for n, (code, text, label) in enumerate(items, 1):
        target = _entry_by_name(root, text.strip("[]")) if code != "LINK-DANGLING" else None
        views.append({"id": str(n), "code": code, "level": "warning", "text": text,
                      "location": {"label": label, "target": target, "page": "studio" if target else None},
                      "status": "pending", "status_note": ""})
    extras = {"orphans": sum(len(stems) for stems in orphans.values()), "unused": len(unused), "dangling": len(dangling)}
    summary = " · ".join(f"{label} {extras[key]}" for key, label in (("orphans", "孤立"), ("unused", "未用"),
                                                                     ("dangling", "失效"))
                         if extras[key]) or "没有问题"
    from ..api import deps

    report = {
        "id": LINK_ID, "name": "链接检查报告", "type": "link", "type_label": TYPE_LABELS["link"],
        "at": deps.iso(time.time()), "summary": summary, "path": "", "scope_text": "", "mode": "full",
        "duration_ms": round((time.time() - started) * 1000),
        "previous": {"at": previous["at"], "summary": previous["summary"]} if previous else None,
        "progress": {"handled": 0, "total": len(views)}, "items": views[:ITEMS_PAGE], "items_next_cursor": None,
        "repair": None, "arbitration": None, "extras": extras,
        "content_md": "\n".join(["# 链接检查报告", "", *[f"- {code} {text}：{label}" for code, text, label in items]]),
    }
    path = root / LINK_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**report, "items": views}, ensure_ascii=False), encoding="utf-8")
    return {key: value for key, value in report.items() if key != "content_md"}
