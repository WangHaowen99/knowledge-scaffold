"""Studio 服务：读（图/表/条目三视图）+ 写路径（草稿区/租约/确定性分流，方案 7.2）。

写路径契约：改动先进草稿区，30 分钟租约写锁防并发编辑；提交时跑确定性
结构检查——通过直接经 write_file 落 git 版本，超范围转 agent 复核，
硬伤直接拒绝。语义判断永远不在这里做。
"""

from __future__ import annotations

import json
import hashlib
import re
import subprocess
import threading
import time
from collections import OrderedDict
from pathlib import Path

from .. import config
from ..kernel import metamodel
from ..kernel.artifacts import index as artifact_index
from ..kernel.metamodel import mm
from . import package_mapping
from .document_metadata import name_frontmatter, report_name
from .workspace import gitops

SECTION = re.compile(r"^(#{1,2})\s+(.+)$", re.MULTILINE)
NUMBERED_STEP = re.compile(r"^\s*\d+[.)、．]\s*(.+)$")

# 实体类型与目录取元模型（含 ontology/logic 只读别名，ADR-0004）
KIND_DIRS = mm.kind_dirs()


def _docs(root: Path) -> list[dict]:
    docs = []
    for kind, rel_dir, pattern in KIND_DIRS:
        directory = root / rel_dir
        if not directory.is_dir() or artifact_index.inside_no_links(root, rel_dir) is None:
            continue  # 目录本身是符号链接：不读
        for path in sorted(directory.glob(pattern)):
            if path.name == mm.INDEX_FILE:  # 工作区占位 index 不是本体元素
                continue
            if not path.is_file() or artifact_index.inside_no_links(root, path.relative_to(root).as_posix()) is None:
                continue  # 符号链接（可以指到工作区外）、设备文件不读
            docs.append(
                {
                    "name": path.stem,
                    "kind": kind,
                    "path": f"{rel_dir}/{path.name}",
                    "file": path,
                }
            )
    return docs


def _is_known_edge(
    source: str,
    target: str,
    key: tuple[str, str, str, str],
    by_name: dict[str, dict],
    seen_edges: set[tuple[str, str, str, str]],
) -> bool:
    if source == target:
        return False
    if source not in by_name or target not in by_name:
        return False
    return key not in seen_edges


def _is_relation_edge(edge: dict, source: str, target: str) -> bool:
    return (
        edge["source"] == source
        and edge["target"] == target
        and edge["kind"] == "object_relation"
    )


def _skip_reference_edge(
    doc: dict, target_doc: dict, section: str, raw_section: str = ""
) -> bool:
    source_kind = doc["kind"]
    target_kind = target_doc["kind"]
    if source_kind == "object" and target_kind == "operation":
        # Legacy object documents used 「与logic的关联」 for operation links.
        return raw_section != "与logic的关联"
    if source_kind == "object" and target_kind in {"metric", "rule"}:
        return True
    if source_kind == "rule" and target_kind == "operation":
        return True
    if source_kind == "script" and target_kind == "operation":
        return True
    if source_kind != "operation":
        return False
    if target_kind != "object":
        return False
    # Legacy operation documents commonly put object links in 「逻辑描述」.
    return section != "与object的关联" and raw_section != "逻辑描述"


def graph(root: Path) -> dict:
    docs = _docs(root)
    by_name = {d["name"]: d for d in docs}
    edges: list[dict] = []
    seen_edges: set[tuple[str, str, str, str]] = set()
    in_degree = {d["name"]: 0 for d in docs}
    out_degree = {d["name"]: 0 for d in docs}

    texts: dict[str, str] = {}
    for doc in docs:
        try:
            texts[doc["name"]] = doc["file"].read_text(encoding="utf-8")
        except OSError:
            texts[doc["name"]] = ""

    def add_edge(source: str, target: str, kind: str, label: str) -> None:
        key = (source, target, kind, label)
        if not _is_known_edge(source, target, key, by_name, seen_edges):
            return
        # Compare endpoints without direction: input/support edges point back
        # toward the operation. Apply the same rule whichever edge arrives first.
        if kind == "references":
            if any({edge["source"], edge["target"]} == {source, target}
                   and edge["kind"] != "references" for edge in edges):
                return
        else:
            for edge in list(edges):
                if ({edge["source"], edge["target"]} == {source, target}
                        and edge["kind"] == "references"):
                    seen_edges.discard((edge["source"], edge["target"], edge["kind"], edge["label"]))
                    edges.remove(edge)
                    out_degree[edge["source"]] -= 1
                    in_degree[edge["target"]] -= 1
        if kind == "object_relation":
            existing = [edge for edge in edges if _is_relation_edge(edge, source, target)]
            if label == "关联对象" and any(edge["label"] != "关联对象" for edge in existing):
                return
            if label != "关联对象":
                for edge in existing:
                    if edge["label"] != "关联对象":
                        return
                    seen_edges.discard((edge["source"], edge["target"], edge["kind"], edge["label"]))
                    edges.remove(edge)
                    out_degree[edge["source"]] -= 1
                    in_degree[edge["target"]] -= 1
        seen_edges.add(key)
        identity = "\0".join(key).encode("utf-8")
        edges.append(
            {
                "id": f"edge-{hashlib.sha256(identity).hexdigest()[:16]}",
                "source": source,
                "target": target,
                "kind": kind,
                "label": label,
            }
        )
        out_degree[source] += 1
        in_degree[target] += 1

    script_names = [d["name"] for d in docs if d["kind"] == "script"]
    # 链接解析按本项目锁定版本的元模型（和 agent 在工作区里用的同一份）
    meta = metamodel.for_project(root)
    for doc in docs:
        text = texts[doc["name"]]
        section = ""
        raw_section = ""
        for line in text.splitlines():
            heading = SECTION.match(line)
            if heading:
                raw_section = heading.group(2).strip()
                section = SECTION_ALIASES.get(raw_section, raw_section)
                continue
            for link in meta.parse_links(line):
                target = meta.resolve(link, by_name)
                if target is None:
                    continue
                target_doc = by_name[target]
                # 操作与对象的关系只认操作条目的「与object的关联」章节；
                # 对象文档里的指标/规则回链、规则文档里的操作来源回链只用于追溯，
                # 不投影为「引用指标」「引用规则」或「适用于操作」边；
                # 其他章节里的对象引用是正文语义，不直接投影为关系边。
                if _skip_reference_edge(doc, target_doc, section, raw_section):
                    continue
                source, destination, kind, label = _relation_projection(
                    doc, target_doc, section, line
                )
                if source is None:
                    continue
                add_edge(source, destination, kind, label)
        # 操作/规则等文档以 `脚本名.py` 引用脚本（codegen 范式），补上这层关系
        if doc["kind"] != "script":
            for script in script_names:
                if f"{script}.py" in text:
                    add_edge(doc["name"], script, "uses_script", "使用脚本")

    nodes = []
    for doc in docs:
        summary = _node_summary(doc, texts[doc["name"]])
        nodes.append(
            {
                "id": doc["name"],
                "kind": doc["kind"],
                "path": doc["path"],
                "label": summary["title"],
                "title": summary["title"],
                "description": summary["description"],
                "fields": summary["fields"],
                "out": out_degree[doc["name"]],
                "in": in_degree[doc["name"]],
            }
        )
    return {
        "workingRevision": _working_revision(docs, texts),
        "nodes": nodes,
        "edges": edges,
    }


def operation_flow(root: Path, element_id: str) -> dict:
    """Project one operation document into the flow-panel contract used by Studio."""
    docs = _docs(root)
    texts: dict[str, str] = {}
    for doc in docs:
        try:
            texts[doc["name"]] = doc["file"].read_text(encoding="utf-8")
        except OSError:
            texts[doc["name"]] = ""
    operation = next(
        (doc for doc in docs if doc["kind"] == "operation" and doc["name"] == element_id),
        None,
    )
    if operation is None:
        raise StudioError("只有操作条目可以查看操作流程", 404)
    steps = _operation_steps(texts[operation["name"]])
    return {
        "workingRevision": _working_revision(docs, texts),
        "operationId": operation["name"],
        "steps": steps,
        "diagrams": [],
        "defaultView": "operation-steps" if steps else "empty",
        "issues": [],
    }


# ---- 最后更新时间（ST-03 `updated_at`） ----

_UPDATES_CACHE_MAX = 32
_updates_cache: OrderedDict = OrderedDict()
_updates_lock = threading.Lock()


def updates(root: Path) -> dict[str, int]:
    """{文档路径: Unix 秒}，取不到的文档不出现在结果里；git 不可用时返回空结果，不报错。

    已提交的文档取最后一次改动它的提交时间（同内容重复保存不产生提交，时间不变）；已写入工作区、未提交的
    取文件修改时间。纯移动或改名（内容不变）不算更新，沿用移动前的时间。Studio 草稿不在文件里，不算。
    遍历历史的结果按 HEAD 和文档集合缓存，条目表每次只多跑一条 `git status`。
    """
    docs = {doc["path"]: doc["file"] for doc in _docs(root)}
    if not docs or not (root / ".git").exists():
        return {}
    try:
        head = _git_text(root, "rev-parse", "-q", "--verify", "HEAD").strip()
        result = dict(_committed_times(root, head, tuple(sorted(docs)))) if head else {}
        status = _git_text(root, "status", "--porcelain", "-z", "--untracked-files=all", "--", "ontology/")
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}
    entries = status.split("\0")
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 4:
            continue
        if entry[0] in "RC":  # 暂存区里的重命名、复制：后面跟一个原路径
            index += 1
        path = entry[3:]
        if path in docs:
            try:
                result[path] = int(docs[path].stat().st_mtime)
            except OSError:
                result.pop(path, None)
    return result


def _committed_times(root: Path, head: str, paths: tuple[str, ...]) -> dict[str, int]:
    key = (str(root.resolve()), head, hashlib.sha256("\0".join(paths).encode("utf-8")).hexdigest())
    with _updates_lock:
        if key in _updates_cache:
            _updates_cache.move_to_end(key)
            return _updates_cache[key]
    # -M100%：只把内容完全相同的移动认作重命名，移动同时改了内容的算更新
    log = _git_text(root, "log", "--format=%x00%ct", "--name-status", "-M100%", "--", "ontology/")
    result: dict[str, int] = {}
    # 从新到旧：历史路径 → 现在的文档路径，遇到纯移动就沿旧路径继续往前找
    pending = {path: path for path in paths}
    timestamp = 0
    for line in log.splitlines():
        if line.startswith("\0"):
            timestamp = int(line[1:])
            continue
        fields = line.split("\t")
        if fields[0].startswith("R") and len(fields) == 3:
            if fields[2] in pending:
                pending[fields[1]] = pending.pop(fields[2])
        elif len(fields) == 2 and fields[1] in pending:
            result[pending.pop(fields[1])] = timestamp
    with _updates_lock:
        _updates_cache[key] = result
        while len(_updates_cache) > _UPDATES_CACHE_MAX:
            _updates_cache.popitem(last=False)
    return result


def _git_text(root: Path, *args: str) -> str:
    # core.quotepath=false：否则中文文件名被转义成 "\346..."，对不上文档路径
    proc = gitops._run_git(root, ("-c", "core.quotepath=false", "-c", "log.showSignature=false", *args))
    if proc.returncode != 0:
        if args[0] == "rev-parse":  # 还没有提交
            return ""
        raise subprocess.SubprocessError(proc.stderr.strip()[:200])
    return proc.stdout


def _working_revision(docs: list[dict], texts: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for doc in sorted(docs, key=lambda item: item["path"]):
        digest.update(doc["path"].encode("utf-8"))
        digest.update(b"\0")
        digest.update(texts.get(doc["name"], "").encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _node_summary(doc: dict, text: str) -> dict:
    sections = {item["title"]: item["content"] for item in parse_sections(text)}
    description_section = {
        "object": "对象描述",
        "operation": "业务描述",
        "rule": "规则描述",
        "metric": "指标描述",
    }.get(doc["kind"])
    if doc["kind"] == "script":
        match = re.search(r"功能描述[：:]\s*([^\n]+)", text)
        description = _plain_summary(match.group(1) if match else "")
    else:
        description = _plain_summary(sections.get(description_section or "", ""))
    fields = _object_fields(sections.get("对象属性清单", "")) if doc["kind"] == "object" else []
    return {"title": _display_name(text, doc["name"]), "description": description, "fields": fields}


def _display_name(text: str, fallback: str) -> str:
    try:
        return report_name(text) or fallback
    except ValueError:
        return fallback


def _plain_summary(text: str) -> str:
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("|", "```", "{待填写", ">")):
            continue
        line = re.sub(r"^[-*+]\s+", "", line)
        line = mm.strip_links(line, use_alias=False)
        line = re.sub(r"[`*_]", "", line).strip()
        if line:
            return line[:500]
    return ""


def _object_fields(table: str) -> list[dict]:
    rows = [_table_cells(line) for line in table.splitlines() if line.strip().startswith("|")]
    if len(rows) < 3:
        return []
    header = rows[0]
    result = []
    for values in rows[2:]:
        record = {column: values[index] if index < len(values) else "" for index, column in enumerate(header)}
        name = record.get("中文名称", "").strip()
        key = record.get("英文名称", "").strip() or name
        if not key or key.startswith("{"):
            continue
        result.append(
            {
                "key": key,
                "name": key,
                "cnName": name or key,
                "label": name or key,
                "description": record.get("描述", "").strip(),
                "type": record.get("类型", "").strip(),
                "primaryKey": record.get("主键", "").strip() == "是",
            }
        )
    return result


def _table_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _relation_projection(
    source: dict,
    target: dict,
    section: str,
    line: str,
) -> tuple[str | None, str | None, str | None, str | None]:
    source_name = source["name"]
    target_name = target["name"]
    source_kind = source["kind"]
    target_kind = target["kind"]

    if source_kind == "object" and target_kind == "object":
        relation = _object_relation_label(line, source_name, target_name)
        if _target_points_to_current(line, source_name, target_name):
            return target_name, source_name, "object_relation", relation
        return source_name, target_name, "object_relation", relation
    if source_kind == "operation" and target_kind == "operation":
        if "被operation调用" in line or "父operation" in line:
            return target_name, source_name, "calls_operation", "调用操作"
        return source_name, target_name, "calls_operation", "调用操作"
    if source_kind == "operation" and target_kind == "object":
        role = _operation_object_role(line)
        if role == "input":
            return target_name, source_name, "operation_input_object", "输入对象"
        if role == "output":
            return source_name, target_name, "operation_output_object", "输出对象"
        if role == "support":
            return target_name, source_name, "operation_support_object", "支撑对象"
        # Legacy operation documents put free-form object links in 「逻辑描述」.
        # Keep those links visible as generic references; the newer association
        # section still requires an explicit input/output/support role.
        if section == "逻辑描述":
            return source_name, target_name, "references", "引用对象"
        return None, None, None, None
    if source_kind == "operation" and target_kind == "rule":
        return source_name, target_name, "uses_rule", "使用规则"
    if source_kind == "operation" and target_kind == "metric":
        return source_name, target_name, "uses_metric", "使用指标"
    if source_kind == "rule" and target_kind == "object":
        return source_name, target_name, "applies_to_object", "适用于对象"
    if source_kind == "metric" and target_kind == "object":
        return source_name, target_name, "uses_object", "使用对象"
    if source_kind == "metric" and target_kind == "operation":
        return target_name, source_name, "uses_metric", "使用指标"
    if source_kind == "script" and target_kind == "object":
        return source_name, target_name, "script_uses_object", "使用对象"
    return source_name, target_name, "references", f"引用{_kind_label(target_kind)}"


def _operation_object_role(line: str) -> str | None:
    if "输入数据的对象" in line or "输入数据对象" in line:
        return "input"
    if "输出数据的对象" in line or "输出数据对象" in line:
        return "output"
    if "支撑数据的对象" in line or "支撑数据对象" in line:
        return "support"
    return None


def _object_relation_label(line: str, source: str, target: str) -> str:
    for relation in package_mapping.parse_relation_definitions("# 关联对象\n" + line, []):
        endpoints = {
            source if relation[key] == package_mapping.CURRENT_OBJECT else relation[key]
            for key in ("source", "target")
        }
        if endpoints == {source, target}:
            return relation["relation"]
    escaped_source = re.escape(source)
    escaped_target = re.escape(target)
    patterns = (
        rf"(?:当前对象名?|{escaped_source})\s*(?:→|->|>)\s*([^→>]+?)\s*(?:→|->|>)\s*\[\[{escaped_target}",
        rf"\[\[{escaped_target}[^\]]*\]\]\s*(?:→|->|>)\s*([^→>]+?)\s*(?:→|->|>)\s*(?:当前对象名?|{escaped_source})",
    )
    for pattern in patterns:
        match = re.search(pattern, line)
        if match:
            label = _plain_summary(match.group(1))
            if label:
                return label
    return "关联对象"


def _target_points_to_current(line: str, source: str, target: str) -> bool:
    return bool(
        re.search(
            rf"\[\[{re.escape(target)}[^\]]*\]\]\s*(?:→|->|>).*?(?:→|->|>)\s*(?:当前对象名?|{re.escape(source)})",
            line,
        )
    )


def _kind_label(kind: str) -> str:
    return {"object": "对象", "operation": "操作", "rule": "规则", "metric": "指标", "script": "脚本"}.get(kind, "元素")


def _operation_steps(text: str) -> list[str]:
    sections = {item["title"]: item["content"] for item in parse_sections(text)}
    content = sections.get("逻辑描述", "")
    if not content.strip():
        return []
    numbered = []
    for line in content.splitlines():
        match = NUMBERED_STEP.match(line)
        if match:
            step = _plain_summary(match.group(1))
            if step:
                numbered.append(step)
    if numbered:
        return numbered
    steps = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("|", "```", ">")):
            continue
        step = _plain_summary(stripped)
        if step and step not in steps:
            steps.append(step)
    return steps[:100]


def element(root: Path, rel_path: str) -> dict:
    target = (root / rel_path).resolve()
    if not target.is_file():
        raise FileNotFoundError(rel_path)
    text = target.read_text(encoding="utf-8")
    name = target.stem

    prefix = ""
    try:
        prefix = name_frontmatter(report_name(text))
    except ValueError:
        pass
    body = text
    if prefix:
        body = text[4:].partition("\n---\n")[2].lstrip("\n")
    sections = parse_sections(body)
    intro = _intro_of(body)

    all_docs = _docs(root)
    names = {doc["name"] for doc in all_docs}
    meta = metamodel.for_project(root)
    links = {target for target in meta.link_names(text, names) if target != name}
    if target.suffix.lower() == ".md":
        for doc in all_docs:
            if doc["kind"] == "script" and f"{doc['name']}.py" in text:
                links.add(doc["name"])
    links = sorted(links)
    is_script = target.suffix.lower() == ".py"
    backlinks = []
    for doc in all_docs:
        if doc["name"] == name:
            continue
        try:
            other = doc["file"].read_text(encoding="utf-8")
        except OSError:
            continue
        hit = meta.references(other, name, names) or (is_script and f"{name}.py" in other)
        if hit:
            backlinks.append({
                "name": doc["name"],
                "label": _display_name(other, doc["name"]),
                "kind": doc["kind"],
                "path": doc["path"],
            })

    return {
        "name": name,
        "label": _display_name(text, name),
        "path": rel_path,
        "intro": intro,
        "name_frontmatter": prefix,
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
        title = match.group(2).strip()
        sections.append(
            {
                "level": section_level,
                "title": SECTION_ALIASES.get(title, title),
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


# ---- schema：对准工具集五元组模板（assets templates，ADR-0004） ----

# 章节名随范式升级迁移；旧文档章节读取时归一为新名
SECTION_ALIASES = mm.SECTION_ALIASES


def _schema(kind: str) -> list[dict]:
    return [section.as_dict() for section in mm.sections_of(kind)]


# 各类条目的章节结构取元模型
OBJECT_SCHEMA = _schema("object")
OPERATION_SCHEMA = _schema("operation")
RULE_SCHEMA = _schema("rule")
METRIC_SCHEMA = _schema("metric")


def schema_for(rel_path: str) -> list[dict] | None:
    kind = mm.kind_of_path(rel_path)
    return _schema(kind) if kind in ("object", "operation", "rule", "metric") else None


# ---- 草稿区（30 分钟租约随写入范围锁取消，方案 C10） ----


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


def purge_project(project_id: str) -> None:
    """删除项目时清除其草稿（leases 是取消租约之前留下的文件，一并删）。"""
    for name in ("leases", "drafts"):
        try:
            _state_file(project_id, name).unlink(missing_ok=True)
        except OSError:
            pass


def save_draft(
    project_id: str, rel_path: str, content: str, base_revision: str, user: str, form: dict | None = None,
    current_revision: str | None = None,
) -> dict:
    """自动暂存草稿。不再占租约：助手在改的条目由写入范围锁挡（提交时 ChangeTx 统一检查，423）。

    结构化编辑器存的草稿另带表单原样（form），重新打开时编辑器按它还原；content 是按表单渲染好的原文。
    current_revision 是正式条目当前的修订：草稿基于的修订已经不是它时拒存（develop !608），免得旧内容盖掉新版本。"""
    if current_revision is not None and base_revision and base_revision != current_revision:
        raise StudioError("正式条目已更新，请刷新后基于最新内容重新编辑", 409)
    drafts = _load(project_id, "drafts")
    drafts[rel_path] = {
        "content": content,
        "form": form,
        "base_revision": base_revision,
        "author": user,
        "updated_at": int(time.time()),
    }
    _save(project_id, "drafts", drafts)
    return {"saved": True, "updated_at": drafts[rel_path]["updated_at"]}


def get_draft(project_id: str, rel_path: str) -> dict | None:
    return _load(project_id, "drafts").get(rel_path)


def draft_paths(project_id: str) -> list[str]:
    """有暂存草稿的文件（版本回退前要先保存或放弃，ADR-0094）；已失效的旧版本草稿不算（develop !608）。"""
    return sorted(path for path, draft in _load(project_id, "drafts").items()
                  if not isinstance(draft, dict) or draft.get("state") != "stale")


def get_active_draft(project_id: str, rel_path: str) -> dict | None:
    draft = get_draft(project_id, rel_path)
    return None if draft and draft.get("state") == "stale" else draft


def ensure_draft_current(draft: dict, current_revision: str) -> None:
    """草稿基于的修订已经不是正式条目当前的修订：拒绝提交。"""
    base_revision = draft.get("base_revision", "")
    if base_revision and base_revision != current_revision:
        raise StudioError("草稿基于旧版本，请刷新后基于最新内容重新编辑", 409)


def reconcile_draft(
    project_id: str, root: Path, rel_path: str, current_content: bytes, current_revision: str,
) -> dict | None:
    """读草稿前对一下正式条目：内容已经一样（复核或别处已合进去）就删掉草稿；基于的修订变了就标 stale，内容留着。

    复核用的暂存稿在助手副本里（方案 C15）时不在 root 下，留在 .ontology-state，不进版本。"""
    drafts = _load(project_id, "drafts")
    draft = drafts.get(rel_path)
    if not draft:
        return None
    content = draft.get("content")
    if isinstance(content, str) and content.encode("utf-8") == current_content:
        drafts.pop(rel_path, None)
        _save(project_id, "drafts", drafts)
        try:
            (root / REVIEW_DIR / f"{Path(rel_path).stem}.md").unlink(missing_ok=True)
        except OSError:
            pass
        return None
    base_revision = draft.get("base_revision", "")
    state = "stale" if base_revision and base_revision != current_revision else "active"
    if draft.get("state") != state:
        draft = {**draft, "state": state}
        drafts[rel_path] = draft
        _save(project_id, "drafts", drafts)
    return {**draft, "state": state}


def discard_draft(project_id: str, rel_path: str, user: str) -> None:
    drafts = _load(project_id, "drafts")
    if rel_path in drafts:
        drafts.pop(rel_path)
        _save(project_id, "drafts", drafts)


# ---- 确定性检查分流 ----


def deterministic_check(root: Path, rel_path: str, content: str) -> dict:
    """结构性检查（不经 LLM）：errors = 解析不了的结构，拒绝；review = 超范围转 agent 复核；
    hints = 链接、主键这类质量问题，只提示、照常保存（F3：建模过程中检查只提示不拦，发布时才拦）。"""
    errors: list[str] = []
    review: list[str] = []
    hints: list[str] = []
    schema = schema_for(rel_path)
    if schema is None:
        review.append("非对象/逻辑文档，Studio 无法做确定性判定")
        return {"errors": errors, "review": review, "hints": hints}

    sections = parse_sections(content)
    titles = {s["title"] for s in sections}
    known_titles = {s["title"] for s in schema}
    baseline_by_title: dict[str, dict] = {}
    target = (root / rel_path).resolve()
    try:
        target.relative_to(root.resolve())
        baseline_by_title = {
            section["title"]: section
            for section in parse_sections(target.read_text(encoding="utf-8"))
        }
    except (OSError, ValueError):
        pass

    for spec in schema:
        if spec["required"] and spec["title"] not in titles:
            errors.append(f"缺少必需章节「{spec['title']}」")
    by_title = {s["title"]: s for s in sections}
    for title in titles - known_titles:
        baseline = baseline_by_title.get(title)
        if baseline is None:
            review.append(f"新增了模板外的章节「{title}」")
        elif baseline["content"] != by_title[title]["content"]:
            review.append(f"修改了模板外的章节「{title}」")

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
            hints.append("对象属性清单没有任何主键行（主键列均非「是」）")

    # 链接目标存在性：指向不存在的文档只提示（可能是还没建的对象），不转复核
    names = {d["name"] for d in _docs(root)}
    stem = Path(rel_path).stem
    # 模板里的填写指引（引用块）、行内代码里的写法示例、模板占位（`[[{待填写：对象A}]]`）不是链接：
    # 按模板新建的条目保存时不因它们提示（规则检查在每轮合入、发布时照常查全文）
    scanned = "\n".join(line for line in content.splitlines() if not line.lstrip().startswith(">"))
    scanned = re.sub(r"`[^`\n]*`", "", scanned)
    scanned = re.sub(r"\{待填写[^{}]*\}", "", scanned)
    for link in metamodel.for_project(root).unresolved(scanned, names | {stem}):
        if "{" in link.full_name or "待填写" in link.full_name:
            continue
        hints.append(f"[[{link.full_name}]] 指向不存在的文档")

    return {"errors": errors, "review": review, "hints": hints}


REVIEW_DIR = ".ontology-state/studio-review"


def stage_for_review(root: Path, rel_path: str, content: str) -> str:
    """把草稿放到 agent 可读的隐藏位置（不进产物树、不进 git），供复核。

    root 是助手的工作目录：变更集副本（方案 C15），建不了副本时是主线工作区。"""
    stem = Path(rel_path).stem
    directory = root / REVIEW_DIR
    directory.mkdir(parents=True, exist_ok=True)
    staged = directory / f"{stem}.md"
    staged.write_text(content, encoding="utf-8")
    return f"{REVIEW_DIR}/{stem}.md"
