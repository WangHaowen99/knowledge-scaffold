"""资料分析的视图（契约 R-07 资料地图、R-08 候选、R-09 核验、R-10 资料问题转待答；RP 资料分析报告的 extras）。

数据都取资料分析自己的产出，不另存一份状态：
- 报告 `research/*.md`（research-agent 的模板：业务项清单、四类候选段、缺口与待决……），按 core 的
  `research_checker` 同一套解析（契约库按项目锁定版本加载）；
- 验收回执 `.ontology-state/research-review.json`（`research_review`）：核验时间、人工待决；
- 资料用途清单 `resource/materials.json`；schema / term 表格的表头。

助手只在一轮里读资料，不记「读到哪」：一份资料在报告里被引用过（路径或文件名）就算读过（100%），
没引用过的算排队中；资料分析在跑（`lock-research`）时排队中的那些显示「正在读」不准确，统一算排队。
资料分析那一轮还没合入（助手在跑、合入冲突）时报告与回执在变更集副本里，读副本；资料文件本身一律读项目工作区（副本里是链接）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path

from . import material_text, workspace_labels
from .workspace.layout import RESOURCE_SUBDIRS, is_auto_detected

KINDS = {
    ".doc": ("doc", "doc"), ".docx": ("doc", "doc"), ".pdf": ("pdf", "doc"),
    ".xls": ("xls", "xls"), ".xlsx": ("xls", "xls"), ".xlsm": ("xls", "xls"), ".csv": ("csv", "csv"),
    ".drawio": ("drawio", "drawio"), ".html": ("html", "html"), ".htm": ("html", "html"),
    ".png": ("image", None), ".jpg": ("image", None), ".jpeg": ("image", None),
    ".md": ("text", "text"), ".markdown": ("text", "text"), ".txt": ("text", "text"),
}
_log = logging.getLogger(__name__)
MATERIALS_MAX = 500
VERIFICATION_MAX = 1000
CANDIDATES_DEFAULT = 50
PROMOTED_REL = Path(".onto") / "run" / "material-problems.json"
_CANDIDATE_KINDS = {"对象": "object", "规则": "rule"}
_ITEM_KINDS = {"对象": "object", "规则": "rule"}


# 存量项目建项目时从资料区模板复制来的图例（F2 起模板里不再放，随 core 包在 .claude/reference/）
LEGACY_TEMPLATES = frozenset({"resource/diagrams/图示说明定义.drawio"})


def is_template(rel: str) -> bool:
    """资源模板（建项目时从 assets/resource-template 复制来的）：不是用户的资料，不进资料地图。"""
    from .. import config

    return rel in LEGACY_TEMPLATES or (Path(config.ASSETS_DIR) / "resource-template" / rel.removeprefix("resource/")).is_file()


def kind_of(rel: str) -> tuple[str, str | None]:
    return KINDS.get(Path(rel).suffix.lower(), ("text", None))


def material_paths(root: Path) -> list[str]:
    found = []
    for subdir in sorted(RESOURCE_SUBDIRS):
        folder = Path(root) / "resource" / subdir
        if not folder.is_dir() or folder.is_symlink() or folder.parent.is_symlink():
            continue  # 资料目录被换成符号链接：不列链接指向的东西
        for path in sorted(folder.rglob("*")):
            if path.is_file() and not path.is_symlink() and not path.name.startswith("."):
                found.append(path.relative_to(root).as_posix())
    return found[:MATERIALS_MAX]


# ---------------------------------------------------------------------------
# 读：报告在哪、解析
# ---------------------------------------------------------------------------


def base(manager, project_id: str) -> Path:
    """报告与回执在哪：资料分析那一轮还没合入（变更集改过 research/）时在副本里。"""
    from ..kernel.changesets import service as changesets, store

    root = manager.project_root(project_id)
    record = store.current(root)
    if record and record.get("state") in ("open", "pending_confirm", "conflict") \
            and any(str(path).startswith("research/") for path in record.get("paths") or []):
        copy = changesets.existing_copy(root)
        if copy is not None and (copy / "research").is_dir():
            return copy
    return root


def _script(root: Path, name: str):
    from .packs import contracts

    return contracts.module_for(Path(root), name)


def report_paths(where: Path) -> list[str]:
    """资料分析报告（core 的列法），去掉路径上有符号链接的（助手在副本里建的链接可以指到任意文件）。"""
    from ..kernel.artifacts import index

    try:
        rels = _script(where, "research_review").report_paths(where)
    except Exception:  # noqa: BLE001 - 契约库加载不了时当没有报告
        return []
    return [rel for rel in rels if index.inside_no_links(where, rel) is not None]


def _gap_lines(body: str) -> list[tuple[bool, str]]:
    out = []
    for line in (body or "").splitlines():
        text = line.strip()
        match = re.match(r"^[-*]\s+\[([ xX])\]\s*(.+)$", text)
        if match and match.group(2).strip():
            out.append((match.group(1) != " ", match.group(2).strip()))
    return out


def parse(where: Path) -> dict:
    """{reports, rows, candidates, gaps, texts}：全部资料分析报告合在一起；解析器加载不了时当没有报告。"""
    try:
        return _parse(where)
    except Exception:  # noqa: BLE001 - 资料分析的视图是附加信息，不能让资料页 500
        _log.exception("资料分析报告解析失败（%s）", Path(where).name)
        return {"reports": [], "rows": [], "candidates": [], "gaps": [], "texts": {}}


def _parse(where: Path) -> dict:
    checker = _script(where, "research_checker")
    rows, candidates, gaps, texts = [], [], [], {}
    for rel in report_paths(where):
        try:
            raw = (where / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        texts[rel] = raw
        sections = checker.extract_sections(raw)
        for row in checker.parse_markdown_table(sections.get(checker.INVENTORY_SECTION, "")):
            rows.append({
                "report": rel,
                "item_id": (row.get("业务项ID") or "").strip(),
                "rule_id": (row.get("规则ID") or "").strip(),
                "name": (row.get("业务项名称") or "").strip(),
                "category": (row.get("分类") or "").strip(),
                "fields": (row.get("引用字段") or "").strip(),
                "location": "" if checker._is_empty(row.get("来源位置") or "") else (row.get("来源位置") or "").strip(),
            })
        for category, section in checker.CATEGORY_SECTIONS.items():
            for name in checker._section_candidates(sections, section):
                candidates.append({"report": rel, "category": category, "name": name})
        for done, text in _gap_lines(sections.get(checker.GAP_SECTION, "")):
            if re.match(r"^新增\s*ID\s*确认\s*[：:]", text):
                continue  # 给检查器的记号（新造编号已确认），不是资料问题
            gaps.append({"report": rel, "done": done, "text": text})
    return {"reports": list(texts), "rows": rows, "candidates": candidates, "gaps": gaps, "texts": texts}


def review(where: Path) -> dict:
    """{status, checked_at(RFC 3339|None), manual_pending}；读不到时 status=none。"""
    try:
        module = _script(where, "research_review")
        status = module.review_status(where)
        receipt = module.load_review(where) or {}
    except Exception:  # noqa: BLE001
        return {"status": "none", "checked_at": None, "manual_pending": []}
    checked = receipt.get("checked_at") or ""
    try:
        checked_at = _iso(time.mktime(time.strptime(checked, "%Y-%m-%d %H:%M:%S"))) if checked else None
    except ValueError:
        checked_at = None
    return {"status": status.get("status") or "none", "checked_at": checked_at,
            "manual_pending": status.get("manual_pending") or []}


def _iso(value) -> str | None:
    from ..api import deps

    return deps.iso(value)


def _locate(where: Path, main: Path, location: str) -> tuple[str, str]:
    """来源位置 → (资料路径, 片段)；定位不到资料区的文件时路径为空。"""
    checker = _script(where, "research_checker")
    _hits, rel = checker._resolve_source(location, checker._locatable_index(main))
    if not rel or not material_text.is_material(rel):
        return "", ""
    return rel, location.partition("#")[2].strip().strip("`")


def anchor_label(main: Path, rel: str, fragment: str) -> str:
    """出处给人看的写法：「资料名 · 位置」（F4c，ADR-0075）。文本版映射回原资料名；行号换成它所在的页、工作表或
    小节标题，算不出来就只写资料名。接口里的路径、锚点照留。"""
    name = workspace_labels.material_name(rel)
    lines = re.fullmatch(r"L(\d+)(?:-L?\d+)?", fragment or "")
    location = (material_text.line_location(main, rel, int(lines.group(1))) if lines
                else material_text.location_label(fragment))
    return f"{name} · {location}" if location else name


def humanize(main: Path, text: str) -> str:
    """给人看的一段话：资料出处换成「资料名 · 位置」，其余工作区路径换成业务名。发给助手的原文不走这里。"""
    text = str(text or "")
    out, last = [], 0
    for match in material_text.ANCHOR_RE.finditer(text):
        out += [text[last:match.start()], anchor_label(main, match.group(1), (match.group(2) or "").lstrip("#"))]
        last = match.end()
    out.append(text[last:])
    return workspace_labels.humanize("".join(out))


# ---------------------------------------------------------------------------
# R-09 核验
# ---------------------------------------------------------------------------


def verification(manager, project_id: str, parsed: dict | None = None) -> dict:
    """逐条核对业务项清单的来源位置：能定位到原文、编号在原文里 → passed；定位不到或编号对不上 → unverifiable；
    新增编号等人工确认 → pending。和资料分析验收（research_checker）同一套抽取与判定。"""
    where = base(manager, project_id)
    main = manager.project_root(project_id)
    parsed = parsed or parse(where)
    checker = _script(where, "research_checker")
    receipt = review(where)
    pending_text = " ".join(str(item.get("item") or "") for item in receipt["manual_pending"])
    items = []
    for row in parsed["rows"][:VERIFICATION_MAX]:
        subject = f"{row['item_id']} {row['name']}".strip()
        kind = _ITEM_KINDS.get(row["category"], "item")
        rel, fragment = _locate(where, main, row["location"]) if row["location"] else ("", "")
        anchor = material_text.file_anchor(rel, fragment) if rel else ""
        item = {"subject": subject, "kind": kind, "anchor": anchor, "anchor_label": anchor_label(main, rel, fragment) if rel else "",
                "status": "passed", "reason": None}
        if not row["location"]:
            item.update(status="unverifiable", reason="没有来源位置")
        elif not rel:
            item.update(status="unverifiable", reason="来源位置指向的资料不存在")
        else:
            resolved = material_text.resolve(main, anchor)
            ids = [value for value in (row["item_id"], row["rule_id"]) if value and value not in ("无", "-", "—")]
            if not resolved["resolved"]:
                item.update(status="unverifiable", reason={"not_found": "原文里找不到这个位置",
                                                          "unreadable": "原文读不出文字"}.get(resolved["reason"],
                                                                                         "来源无法核验"))
            elif any(value in pending_text for value in ids):
                item.update(status="pending", reason="新增编号待确认")
            elif ids:
                text = _segment_text(main, rel, fragment)
                missing = [value for value in ids if not checker._contains_id(text, value)]
                if missing:
                    item.update(status="unverifiable", reason=f"原文里没有编号 {'、'.join(missing)}")
        items.append(item)
    passed = sum(1 for item in items if item["status"] == "passed")
    return {"checked_at": receipt["checked_at"], "total": len(items), "passed": passed,
            "problems": sum(1 for item in items if item["status"] == "unverifiable"),
            "pending": sum(1 for item in items if item["status"] == "pending"), "items": items}


def _segment_text(main: Path, rel: str, fragment: str) -> str:
    result = material_text.extract(main, rel)
    segments = result.get("segments") or []
    if not fragment:
        return "\n".join(str(seg["text"]) for seg in segments)
    return "\n".join(str(seg["text"]) for seg in segments if seg["anchor"] == fragment)


# ---------------------------------------------------------------------------
# R-07 资料地图、R-10 资料问题
# ---------------------------------------------------------------------------


def _promoted(main: Path) -> dict:
    try:
        data = json.loads((main / PROMOTED_REL).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _live_promoted(manager, project_id: str, main: Path) -> dict:
    """转出记录存在 .onto/run/（不随版本走）：只认现在蓝图里还在的问题，回退、撤回之后蓝图里没了的不算转出。"""
    from .flow import blueprint

    doc = blueprint.effective_doc(manager, project_id) or {}
    live = {f"bq-{item.get('id')}" for item in doc.get("questions") or [] if isinstance(item, dict)}
    return {pid: record for pid, record in _promoted(main).items()
            if isinstance(record, dict) and record.get("question_id") in live}


def problems(main: Path, parsed: dict, promoted: dict | None = None) -> list[dict]:
    """「缺口与待决」的条目：写到「矛盾」的算资料矛盾，其余算疑似缺失；勾掉的已解决，转成待答问题的已转出。"""
    promoted = _promoted(main) if promoted is None else promoted
    out, seen = [], set()
    for gap in parsed["gaps"]:
        problem_id = "pb-" + hashlib.sha256(gap["text"].encode("utf-8")).hexdigest()[:10]
        if problem_id in seen:
            continue
        seen.add(problem_id)
        anchors = [material_text.file_anchor(path, fragment) for path, fragment, _ in material_text.find_anchors(gap["text"])]
        state = "resolved" if gap["done"] else "promoted" if problem_id in promoted else "open"
        out.append({"id": problem_id, "kind": "contradiction" if "矛盾" in gap["text"] else "missing",
                    "text_md": gap["text"], "display_md": humanize(main, gap["text"]), "anchors": anchors,
                    "state": state})
    return out


def _mentioned(parsed: dict, rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return any(rel in text or material_text.name_positions(text, name) for text in parsed["texts"].values())


def pack_enabled(main: Path, name: str) -> bool:
    from .packs import lockfile

    try:
        lock = lockfile.read(main)
    except (ValueError, OSError):
        return False
    entry = lock.get(name) if lock else None
    return bool(entry and entry.enabled)


def _table_fields(main: Path, rel: str) -> int:
    """schema / term 表格的列数（按表头）。"""
    if not rel.lower().endswith((".csv", ".xlsx", ".xlsm")):
        return 0
    try:
        tables = _script(main, "table_profile").read_tables(main / rel)
    except Exception:  # noqa: BLE001 - 读不了的表格不计
        return 0
    return sum(len(columns) for columns in (tables or {}).values())


def map_items(manager, project_id: str, parsed: dict, found_problems: list[dict]) -> list[dict]:
    main = manager.project_root(project_id)
    directives = manager.resource_directives(project_id)
    items = []
    for rel in material_paths(main):
        if is_template(rel):
            continue
        directive = directives.get(rel)
        purpose = directive.get("purpose", "auto") if isinstance(directive, dict) else "auto"
        read = _mentioned(parsed, rel)
        cited = [row for row in parsed["rows"] if rel in row["location"] or rel.rsplit("/", 1)[-1] in row["location"]]
        counts = {"objects": sum(1 for row in cited if row["category"] == "对象"), "terms": 0}
        rules = sum(1 for row in cited if row["category"] == "规则")
        if rules:
            counts["rules"] = rules
        if purpose in ("schema", "term"):
            counts["fields"] = _table_fields(main, rel)
        name = rel.rsplit("/", 1)[-1]
        contradictions = sum(1 for item in found_problems if item["kind"] == "contradiction" and item["state"] == "open"
                             and (rel in item["text_md"] or name in item["text_md"]))
        items.append({
            "path": rel, "name": name, "label": workspace_labels.material_name(rel), "kind": kind_of(rel)[0],
            "purpose": purpose,
            "purpose_auto": directive is None or is_auto_detected(directive) or purpose == "auto",
            "read_pct": 100 if read else 0, "status": "done" if read else "queued",
            "position": None, "queue_reason": None,
            "contradiction": f"{contradictions} 处矛盾" if contradictions else None,
            "blocked": None, "counts": counts,
        })
    return items


def material_map(manager, project_id: str) -> dict:
    """R-07 MaterialMap。"""
    where = base(manager, project_id)
    main = manager.project_root(project_id)
    parsed = parse(where)
    found = problems(main, parsed, _live_promoted(manager, project_id, main))
    items = map_items(manager, project_id, parsed, found)
    checked = verification(manager, project_id, parsed)
    receipt = review(where)
    meta = manager.get_project(project_id) or {}
    return {
        "total": len(items),
        "read_pct": round(sum(item["read_pct"] for item in items) / len(items)) if items else 0,
        "analysis_mode": "web" if meta.get("research_mode") == "web" else "local",
        "web_available": pack_enabled(main, "websearch"),
        "items": items,
        "problems": found,
        "verification": {"total": checked["total"], "passed": checked["passed"], "unverifiable": checked["problems"],
                         "pending": checked["pending"], "checked_at": checked["checked_at"]},
        "can_generate_blueprint": receipt["status"] == "passed",
    }


def coverage(manager, project_id: str, rel: str) -> dict | None:
    """R-06 的 coverage：没有资料分析报告时 None；报告引用过这份资料算读过（验收通过才算读全）。"""
    where = base(manager, project_id)
    parsed = parse(where)
    if not parsed["reports"]:
        return None
    read = _mentioned(parsed, rel)
    return {"read_sections": "", "read_pct": 100 if read else 0,
            "complete": bool(read and review(where)["status"] == "passed")}


def material_coverage(manager, project_id: str) -> dict[str, dict | None]:
    """R-01 每份资料的 coverage（{read_pct, label}）：没有报告时全是 None。"""
    where = base(manager, project_id)
    parsed = parse(where)
    main = manager.project_root(project_id)
    if not parsed["reports"]:
        return {}
    return {rel: {"read_pct": 100 if _mentioned(parsed, rel) else 0,
                  "label": "已读" if _mentioned(parsed, rel) else "未读"} for rel in material_paths(main)}


def promote(manager, project_id: str, problem_id: str, target: str, *, actor: str) -> dict:
    """R-10：资料问题记成建模蓝图的待答问题（进待处理的「决策」）；同一个问题再转一次返回原来那个。"""
    from ..services.workspace.errors import WorkspaceError
    from .flow import blueprint

    if target not in ("blueprint_question", "ask_me"):
        raise WorkspaceError("as 只能是 blueprint_question 或 ask_me", 400, "invalid_argument", {"fields": {"as": "enum"}})
    main = manager.project_root(project_id)
    live = _live_promoted(manager, project_id, main)
    found = next((item for item in problems(main, parse(base(manager, project_id)), live)
                  if item["id"] == problem_id), None)
    if found is None:
        raise WorkspaceError("资料问题不存在", 404, "not_found")
    promoted = _promoted(main)
    record = live.get(problem_id)
    if not record:
        question = blueprint.add_question(manager, project_id, found["text_md"], actor=actor)
        record = {"question_id": f"bq-{question}", "as": target, "by": actor, "at": time.time()}
        path = main / PROMOTED_REL
        path.parent.mkdir(parents=True, exist_ok=True)
        promoted[problem_id] = record
        path.write_text(json.dumps(promoted, ensure_ascii=False, indent=2), encoding="utf-8")
    question_id = record["question_id"]
    return {"question_id": question_id, "inbox_item_id": f"decision:{question_id}" if target == "ask_me" else None}


# ---------------------------------------------------------------------------
# R-08 候选
# ---------------------------------------------------------------------------


def candidates(manager, project_id: str, *, after: str = "", limit: int = 0) -> dict:
    """报告里的对象、规则候选（加粗名），术语取建模蓝图里已统一的叫法；出处取业务项清单同名那一行。"""
    from .flow import blueprint

    where = base(manager, project_id)
    main = manager.project_root(project_id)
    parsed = parse(where)
    located = {row["name"]: row["location"] for row in parsed["rows"] if row["location"]}
    out = []
    for item in parsed["candidates"]:
        kind = _CANDIDATE_KINDS.get(item["category"])
        if kind:
            out.append((kind, item["name"], item["report"]))
    doc = blueprint.effective_doc(manager, project_id) or {}
    for term in (doc.get("terms") or {}).get("aligned") or []:
        out.append(("term", str(term), "research/blueprint.json"))
    items, seen = [], set()
    for kind, name, source in out:
        candidate_id = "c-" + hashlib.sha256(f"{kind}:{name}".encode("utf-8")).hexdigest()[:10]
        if candidate_id in seen:
            continue
        seen.add(candidate_id)
        rel, fragment = _locate(where, main, located[name]) if name in located else ("", "")
        try:
            created = (where / source).stat().st_mtime
        except OSError:
            created = None
        items.append({"id": candidate_id, "kind": kind, "name": name,
                      "anchor": material_text.file_anchor(rel, fragment) if rel else "",
                      "anchor_label": anchor_label(main, rel, fragment) if rel else "", "created_at": _iso(created)})
    start = 0
    if after:
        index = next((i for i, item in enumerate(items) if item["id"] == after), None)
        start = index + 1 if index is not None else 0
    limit = max(1, min(int(limit or CANDIDATES_DEFAULT), 500))
    return {"total": len(items), "items": items[start:start + limit]}


# ---------------------------------------------------------------------------
# 资料分析报告（RP 的 research 类型）
# ---------------------------------------------------------------------------


def report_extras(manager, project_id: str) -> dict:
    """报告页「资料分析报告」的 extras：资料读取结果（MaterialMap 的摘要）。"""
    data = material_map(manager, project_id)
    parsed = parse(base(manager, project_id))
    rows = parsed["rows"]
    purposes: dict[str, int] = {}
    for item in data["items"]:
        purposes[item["purpose"]] = purposes.get(item["purpose"], 0) + 1
    contradictions = [item for item in data["problems"] if item["kind"] == "contradiction"]
    doc = None
    try:
        from .flow import blueprint

        doc = blueprint.effective_doc(manager, project_id)
    except Exception:  # noqa: BLE001
        doc = None
    return {
        "total": data["total"], "read_pct": data["read_pct"],
        "found": {"items": len({row["item_id"] or row["name"] for row in rows}),
                  "terms": len(((doc or {}).get("terms") or {}).get("aligned") or []),
                  "rule_candidates": sum(1 for item in parsed["candidates"] if item["category"] == "规则")},
        "candidates": {"objects": sum(1 for item in parsed["candidates"] if item["category"] == "对象"),
                       "rules": sum(1 for item in parsed["candidates"] if item["category"] == "规则")},
        "open_questions": sum(1 for item in (doc or {}).get("questions") or [] if not item.get("answer")),
        "contradictions": {"total": len(contradictions),
                           "resolved": sum(1 for item in contradictions if item["state"] != "open")},
        "purposes": [{"value": key, "count": value} for key, value in purposes.items()],
        "items": data["items"], "problems": data["problems"], "verification": data["verification"],
    }
