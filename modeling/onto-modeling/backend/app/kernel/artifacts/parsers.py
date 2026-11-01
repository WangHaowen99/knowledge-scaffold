"""内核内置的产物格式解析器（详设 5.9.4「解析不跑包的代码」）。

一份文件解析成一个或多个产物（md-sections 一个二级标题一项），每项给：键、标题、字段、
Markdown 分段、表格预览、结构化内容。写回（AR-05 保存）也在这里，按原格式序列化。

| 格式 | 一份文件 | 字段从哪来 |
|---|---|---|
| md-frontmatter | 一项 | `---` 之间的 YAML；正文按标题切段；登记了 table 时取第一张 Markdown 表 |
| yaml / json | 一项 | 顶层映射；content 是原样结构 |
| jsonl / csv | 一项 | 表格；content（jsonl）是前若干行 |
| drawio | 一项 | 流程图模型（flowchart.parse） |
| md-report | 一项 | 报告条目（吸收原 report_items 的逐条解析） |
| md-sections | 每个二级标题一项 | `- **标签**：值` 列表项，按登记的 field_labels / section_labels 对应 |
"""

from __future__ import annotations

import csv
import io
import json
import re
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .registry import ArtifactType

MAX_FILE_BYTES = 5 * 1024 * 1024
TABLE_ROWS_MAX = 500
_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.S)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_TABLE_LINE_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_BULLET_RE = re.compile(r"^-\s+\*\*(.+?)\*\*\s*[：:]\s*(.*)$")
_WIKI_RE = re.compile(r"^\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]$")
_ITEM_PREFIX_RE = re.compile(r"^[^：:\n]{0,12}?\d+\s*[：:]\s*")


class ParseError(ValueError):
    pass


@dataclass
class Parsed:
    key: str
    title: str
    fields: dict
    sections: dict = field(default_factory=dict)
    table: dict | None = None
    content: object = None
    heading: str = ""


# ---- 公共小件 ----


def split_frontmatter(text: str) -> tuple[dict, str]:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    try:
        data = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise ParseError(f"frontmatter 无法解析：{exc}") from exc
    if not isinstance(data, dict):
        raise ParseError("frontmatter 必须是映射")
    return data, text[match.end():]


def sections_of(body: str) -> dict[str, str]:
    """按标题切段：标题文字 → 段内正文（不含下级标题以外的内容截断，同级或更高级标题结束一段）。"""
    out: dict[str, str] = {}
    current: str | None = None
    level = 0
    buffer: list[str] = []
    fence = False
    for line in body.splitlines():
        if line.strip().startswith("```"):
            fence = not fence
        match = None if fence else _HEADING_RE.match(line)
        if match and (current is None or len(match.group(1)) <= level):
            if current is not None:
                out[current] = "\n".join(buffer).strip()
            current, level, buffer = match.group(2).strip(), len(match.group(1)), []
            continue
        if current is not None:
            buffer.append(line)
    if current is not None:
        out[current] = "\n".join(buffer).strip()
    return out


def first_h1(text: str) -> str:
    for line in text.splitlines():
        match = _HEADING_RE.match(line)
        if match and len(match.group(1)) == 1:
            return match.group(2).strip()
    return ""


def _cells(line: str) -> list[str]:
    inner = line.strip()
    inner = inner[1:] if inner.startswith("|") else inner
    inner = inner[:-1] if inner.endswith("|") else inner
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", inner)]


def markdown_table(text: str) -> dict | None:
    """正文里第一张 Markdown 表：{columns, rows, total_rows}。"""
    lines = text.splitlines()
    for index in range(len(lines) - 1):
        if _TABLE_LINE_RE.match(lines[index]) and _TABLE_SEP_RE.match(lines[index + 1]):
            columns = _cells(lines[index])
            rows = []
            for line in lines[index + 2:]:
                if not _TABLE_LINE_RE.match(line):
                    break
                cells = _cells(line)
                rows.append((cells + [""] * len(columns))[:len(columns)])
            return {"columns": columns, "rows": rows, "total_rows": len(rows)}
    return None


def plain_ref(value) -> str:
    """`[[名称]]` / `名称` → 名称（锚点与别名去掉）。"""
    text = str(value or "").strip()
    match = _WIKI_RE.match(text)
    return (match.group(1) if match else text).strip()


def _scalar(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, list) and all(isinstance(item, (str, int, float, bool)) for item in value):
        return value
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _decode(data: bytes) -> str:
    if len(data) > MAX_FILE_BYTES:
        raise ParseError("文件超过 5 MiB，内置解析器不处理")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ParseError("文件不是 UTF-8 文本") from exc


def _title(fields: dict, atype: ArtifactType, key: str) -> str:
    card_title = (atype.view.get("card") or {}).get("title") or "title"
    for name in ("title", card_title, "name"):
        value = fields.get(name)
        if value not in (None, ""):
            return plain_ref(value) if isinstance(value, str) else str(value)
    return key


def _derive(atype: ArtifactType, fields: dict, *, stem: str, text: str, table: dict | None,
            heading: str = "", mtime: float | None = None) -> None:
    for name, how in atype.derive.items():
        if fields.get(name) not in (None, ""):
            continue
        if how == "stem":
            fields[name] = stem
        elif how == "h1":
            fields[name] = first_h1(text)
        elif how == "h1_suffix":
            h1 = first_h1(text)
            fields[name] = re.split(r"[：:]", h1, maxsplit=1)[-1].strip() if h1 else stem
        elif how == "table_rows":
            fields[name] = (table or {}).get("total_rows", 0)
        elif how == "heading":
            fields[name] = heading
        elif how == "file_mtime" and mtime:
            fields[name] = mtime
        elif how.startswith("field:"):
            fields[name] = fields.get(how[len("field:"):])
        elif how.startswith("match:"):
            # match:<字段>:<正则>=<命中时的值>|<否则的值>
            source, _, rule = how[len("match:"):].partition(":")
            pattern, _, values = rule.rpartition("=")
            hit, _, miss = values.partition("|")
            text = str(fields.get(source) or "")
            try:
                matched = bool(re.search(pattern, text))
            except re.error:
                matched = False
            fields[name] = hit if matched else (miss if text or miss else None)


def _key(atype: ArtifactType, fields: dict, stem: str) -> str:
    if atype.key not in ("filename", "stem", "") and fields.get(atype.key) not in (None, ""):
        return str(fields[atype.key])
    return stem


# ---- 解析 ----


def parse(atype: ArtifactType, rel: str, data: bytes, *, file_key: str, mtime: float | None = None) -> list[Parsed]:
    """一份文件 → 产物列表。file_key 是路径里 * 匹配到的那段（单项格式的默认键）。"""
    fmt = atype.format
    if fmt == "drawio":
        from . import flowchart

        try:
            model = flowchart.parse(data, key=file_key)
        except flowchart.FlowchartError as exc:
            raise ParseError(str(exc)) from exc
        fields = {"title": model["process"].get("name") or file_key, "summary": flowchart.summary(model),
                  "state": "draft" if any(node.get("pending") for node in model["nodes"]) else "confirmed",
                  "operation": (model.get("operation") or {}).get("name") or ""}
        _derive(atype, fields, stem=file_key, text="", table=None, mtime=mtime)
        return [Parsed(file_key, fields["title"], fields, content=model)]
    if fmt == "csv":
        text = _decode(data)
        rows = list(csv.reader(io.StringIO(text)))
        columns = rows[0] if rows else []
        body = [(row + [""] * len(columns))[:len(columns)] for row in rows[1:] if any(cell.strip() for cell in row)]
        table = {"columns": columns, "rows": body, "total_rows": len(body)}
        fields: dict = {}
        _derive(atype, fields, stem=file_key, text="", table=table, mtime=mtime)
        return [Parsed(file_key, _title(fields, atype, file_key), fields, table=table)]
    if fmt == "jsonl":
        text = _decode(data)
        records = []
        for number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except ValueError as exc:
                raise ParseError(f"第 {number} 行不是合法 JSON") from exc
        columns: list[str] = []
        for record in records[:50]:
            for name in (record if isinstance(record, dict) else {}):
                if name not in columns:
                    columns.append(name)
        rows = [[_cell(record.get(name)) if isinstance(record, dict) else "" for name in columns] for record in records]
        table = {"columns": columns, "rows": rows, "total_rows": len(rows)}
        fields = {}
        _derive(atype, fields, stem=file_key, text="", table=table, mtime=mtime)
        return [Parsed(file_key, _title(fields, atype, file_key), fields, table=table, content=records[:TABLE_ROWS_MAX])]
    if fmt in ("yaml", "json"):
        text = _decode(data)
        try:
            content = yaml.safe_load(text) if fmt == "yaml" else json.loads(text)
        except (yaml.YAMLError, ValueError) as exc:
            raise ParseError(f"{fmt} 无法解析：{exc}") from exc
        fields = {key: _scalar(value) for key, value in content.items()} if isinstance(content, dict) else {}
        _derive(atype, fields, stem=file_key, text="", table=None, mtime=mtime)
        key = _key(atype, fields, file_key)
        return [Parsed(key, _title(fields, atype, key), fields, content=content)]
    text = _decode(data)
    if fmt == "md-sections":
        return _parse_sections(atype, text, file_key, mtime)
    meta, body = split_frontmatter(text)
    fields = {key: _scalar(value) for key, value in meta.items()}
    table = markdown_table(body) if atype.table else None
    content = None
    if fmt == "md-report":
        from ...services import report_items

        content = report_items.parse(body)
    _derive(atype, fields, stem=file_key, text=body, table=table, mtime=mtime)
    key = _key(atype, fields, file_key)
    if not fields.get("title") and fmt == "md-report":
        fields["title"] = first_h1(body) or key
    return [Parsed(key, _title(fields, atype, key), fields, sections=sections_of(body), table=table, content=content)]


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _bullets(block: str) -> list[tuple[str, str]]:
    """`- **标签**：值` 列表项（值可以跨行：后续缩进行并进同一项）。"""
    items: list[list] = []
    for line in block.splitlines():
        match = _BULLET_RE.match(line.strip()) if not line.startswith((" ", "\t")) else None
        if match:
            items.append([match.group(1).strip(), [match.group(2).rstrip()]])
        elif items and (line.startswith((" ", "\t")) or not line.strip()):
            items[-1][1].append(line.rstrip())
    out = []
    for label, lines in items:
        first, rest = lines[0].strip(), textwrap.dedent("\n".join(lines[1:])).strip("\n").rstrip()
        out.append((label, "\n".join(part for part in (first, rest) if part)))
    return out


def _section_blocks(text: str) -> list[tuple[str, str, int, int]]:
    """二级标题块：(标题, 块正文, 起始行, 结束行)。HTML 注释里的内容不算。"""
    lines = text.splitlines()
    blocks = []
    current = None
    in_comment = False
    for index, line in enumerate(lines):
        if "<!--" in line and "-->" not in line:
            in_comment = True
        if in_comment:
            if "-->" in line:
                in_comment = False
            continue
        match = _HEADING_RE.match(line)
        if match and len(match.group(1)) <= 2:
            if current is not None:
                blocks.append(current)
                current = None
            if len(match.group(1)) == 2:
                current = [match.group(2).strip(), [], index, index]
            continue
        if current is not None:
            current[1].append(line)
            current[3] = index
    if current is not None:
        blocks.append(current)
    return [(title, "\n".join(body), start, end) for title, body, start, end in blocks]


def _parse_sections(atype: ArtifactType, text: str, file_key: str, mtime: float | None) -> list[Parsed]:
    out = []
    labels_to_field = {label: name for name, label in atype.field_labels.items()}
    labels_to_section = {label: name for name, label in atype.section_labels.items()}
    for position, (heading, block, _, _) in enumerate(_section_blocks(text), 1):
        number = re.search(r"(\d+)", heading.split("：")[0].split(":")[0])
        index = number.group(1) if number else str(position)
        title = _ITEM_PREFIX_RE.sub("", heading).strip() or heading
        fields: dict = {"title": title}
        sections: dict = {}
        for label, value in _bullets(block):
            if label in labels_to_field:
                fields[labels_to_field[label]] = value
            sections[labels_to_section.get(label, label)] = value
        _derive(atype, fields, stem=file_key, text=text, table=None, heading=heading, mtime=mtime)
        key = f"{file_key}--{index}"
        out.append(Parsed(key, title, fields, sections=sections, heading=heading))
    return out


# ---- 写回 ----


def render(atype: ArtifactType, previous: str | None, *, fields: dict | None, sections: dict | None,
           content=None, item_key: str = "") -> str:
    """按原格式写出一份文件内容；previous 是改之前的文件文本（md-sections 只改目标那一块）。"""
    fmt = atype.format
    if fmt in ("yaml", "json"):
        if content is None:
            base = (yaml.safe_load(previous) if fmt == "yaml" else json.loads(previous)) if previous else {}
            content = {**(base if isinstance(base, dict) else {}), **(fields or {})}
        if fmt == "yaml":
            return yaml.safe_dump(content, allow_unicode=True, sort_keys=False)
        return json.dumps(content, ensure_ascii=False, indent=2) + "\n"
    if fmt == "jsonl":
        rows = content if isinstance(content, list) else []
        return "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    if fmt == "csv":
        table = content if isinstance(content, dict) else {}
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(table.get("columns") or [])
        writer.writerows(table.get("rows") or [])
        return buffer.getvalue()
    if fmt == "md-sections":
        return _render_section(atype, previous or "", item_key, fields or {}, sections or {})
    meta, body = split_frontmatter(previous or "")
    meta = {**meta, **(fields or {})}
    if sections:
        body = _replace_sections(body, sections, atype.sections)
    head = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip() if meta else ""
    return (f"---\n{head}\n---\n\n" if head else "") + body.lstrip("\n")


def _replace_sections(body: str, sections: dict, order: list[str]) -> str:
    """改写同名标题下的正文；没有的段按登记顺序追加成二级标题。"""
    lines = body.splitlines()
    remaining = dict(sections)
    out: list[str] = []
    index = 0
    while index < len(lines):
        match = _HEADING_RE.match(lines[index])
        if match and match.group(2).strip() in remaining:
            title = match.group(2).strip()
            level = len(match.group(1))
            out.append(lines[index])
            index += 1
            while index < len(lines):
                nxt = _HEADING_RE.match(lines[index])
                if nxt and len(nxt.group(1)) <= level:
                    break
                index += 1
            out += ["", str(remaining.pop(title)).strip(), ""]
            continue
        out.append(lines[index])
        index += 1
    for title in sorted(remaining, key=lambda name: order.index(name) if name in order else len(order)):
        out += ["", f"## {title}", "", str(remaining[title]).strip(), ""]
    text = "\n".join(out).strip("\n") + "\n"
    return re.sub(r"\n{3,}", "\n\n", text)


def _render_section(atype: ArtifactType, previous: str, item_key: str, fields: dict, sections: dict) -> str:
    lines = previous.splitlines()
    blocks = _section_blocks(previous)
    file_key, _, number = item_key.rpartition("--")
    target = None
    for position, block in enumerate(blocks, 1):
        found = re.search(r"(\d+)", block[0].split("：")[0].split(":")[0])
        if (found.group(1) if found else str(position)) == number:
            target = block
            break
    if target is None:
        raise ParseError(f"文件里找不到 {item_key} 这一项")
    heading, body, start, end = target
    title = fields.get("title")
    if title:
        prefix = heading[: len(heading) - len(_ITEM_PREFIX_RE.sub("", heading))]
        heading = f"{prefix}{title}" if prefix else str(title)
    values = dict(_bullets(body))
    field_by_label = {label: name for name, label in atype.field_labels.items()}
    section_by_label = {label: name for name, label in atype.section_labels.items()}
    for label in list(values):
        if field_by_label.get(label) in fields:
            values[label] = str(fields[field_by_label[label]])
        name = section_by_label.get(label, label)
        if name in sections:
            values[label] = str(sections[name])
    for name, value in sections.items():
        label = atype.section_labels.get(name, name)
        if label not in values and name in atype.sections:
            values[label] = str(value)
    rendered = [f"## {heading}", ""]
    for label, value in values.items():
        value = (value or "").strip("\n")
        if "\n" in value or value.startswith("- "):
            # 多行或列表值：标签单独一行，内容缩进两格挂在下面（与模板的「输入」写法一致）
            rendered.append(f"- **{label}**：")
            rendered += [f"  {line}" if line.strip() else "" for line in value.split("\n")]
        else:
            rendered.append(f"- **{label}**：{value}".rstrip())
    rendered.append("")
    return "\n".join(lines[:start] + rendered + lines[end + 1:]).rstrip("\n") + "\n"


def parse_file(atype: ArtifactType, root: Path, rel: str) -> list[Parsed]:
    path = Path(root) / rel
    file_key = atype.key_from_path(rel) or Path(rel).stem
    stat = path.stat()
    return parse(atype, rel, path.read_bytes(), file_key=file_key, mtime=stat.st_mtime)
