#!/usr/bin/env python3
"""本体元模型的唯一定义（详设 4.4、方案 C3～C5）：实体类型与目录、章节结构、链接语法、文件名编码。

后端（Studio、MCP、发布、校验）与工具箱脚本（规则检查、调用关系、字段语义、查询）都从这里取，
不再各写一份目录表和维基链接正则。只用标准库：工具箱脚本在工作区 `.claude/scripts/` 下直接运行，
后端按文件加载同一份（`app/kernel/metamodel.py`）。

链接语法：`[[名称#锚点|显示名]]`。

- `#` 一律是锚点分隔符，`|` 是别名分隔符（表格里转义成 `\\|` 的也认）；
- 名称可以带路径前缀与 `.md` 扩展名（`[[ontology/object/订单.md]]`），解析时只取最后一段；
- 解析到实体时先按「名称#锚点」全名匹配，兼容存量文件名里带 `#` 的条目（如导入的 `1#油泵`），
  再按名称匹配，最后按文件名编码后的形式匹配。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from collections.abc import Iterable, Mapping

METAMODEL_VERSION = "1.0"


@dataclass(frozen=True)
class Section:
    title: str
    kind: str  # text | table
    required: bool
    columns: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        data = {"title": self.title, "kind": self.kind, "required": self.required}
        if self.columns:
            data["columns"] = list(self.columns)
        return data


@dataclass(frozen=True)
class EntityType:
    kind: str
    label: str
    directory: str
    suffix: str = ".md"
    # 只读别名目录：旧版目录仍可读，新条目写到 directory（ADR-0004 logic → operation）
    aliases: tuple[str, ...] = ()
    recursive: bool = False
    sections: tuple[Section, ...] = ()

    @property
    def directories(self) -> tuple[str, ...]:
        return (self.directory, *self.aliases)


def _text(title: str, required: bool = True) -> Section:
    return Section(title, "text", required)


def _table(title: str, columns: Iterable[str], required: bool = True) -> Section:
    return Section(title, "table", required, tuple(columns))


ENTITY_TYPES: tuple[EntityType, ...] = (
    EntityType(
        "object", "对象", "ontology/object",
        sections=(
            _text("对象描述"),
            _text("对象别名", False),
            _text("英文名称", False),
            # 条目模板里有这两节（发布时按「数据来源」绑表）：写了不算模板外的章节
            _text("数据来源", False),
            _text("查询边界", False),
            _table("对象属性清单", ("中文名称", "英文名称", "描述", "主键", "类型", "属性别名", "属性规则")),
            _text("关联对象"),
        ),
    ),
    EntityType(
        "operation", "操作", "ontology/operation", aliases=("ontology/logic",),
        sections=(
            _text("业务描述"),
            _text("与operation的关联"),
            _text("与object的关联"),
            _table("与object的语义关联", ("业务对象", "语义关联关系"), False),
            _text("触发条件", False),
            _table("输入", ("参数名称", "描述", "输入源")),
            _table("输出", ("参数名称", "描述")),
            _text("逻辑描述"),
        ),
    ),
    EntityType(
        "rule", "规则", "ontology/rule",
        sections=(
            _text("规则描述"),
            _text("规则类型", False),
            _text("适用对象"),
            _text("触发条件", False),
            _text("约束表达"),
            _text("违反处置", False),
            _text("来源", False),
            _text("原始资料来源", False),
            _text("例外情况", False),
        ),
    ),
    EntityType(
        "metric", "指标", "ontology/metric",
        sections=(
            _text("指标描述"),
            _text("计算公式"),
            _text("数据来源"),
            _text("维度与粒度", False),
            _text("单位", False),
            _text("更新频率", False),
        ),
    ),
    EntityType("script", "脚本", "ontology/scripts", suffix=".py"),
)
ENTITY_KINDS = tuple(entity.kind for entity in ENTITY_TYPES)
BY_KIND: dict[str, EntityType] = {entity.kind: entity for entity in ENTITY_TYPES}
# 各目录里的占位索引不是本体条目
INDEX_FILE = "index.md"
# 旧章节名 → 现行章节名
SECTION_ALIASES = {"与logic的关联": "与operation的关联"}
# 对象之间的关系写在这个章节里（`[[A]] → 关系 → [[B]]`）
RELATION_SECTION = "关联对象"


def kind_dirs() -> tuple[tuple[str, str, str], ...]:
    """(实体类型, 目录, glob) 列表，含只读别名目录；目录顺序即读取顺序（同名先到先得）。"""
    return tuple(
        (entity.kind, directory, f"*{entity.suffix}")
        for entity in ENTITY_TYPES
        for directory in entity.directories
    )


def kind_of_path(rel_path: str) -> str | None:
    """工作区相对路径落在哪类实体目录里（只认目录直下的条目文件，不认 index.md）。"""
    rel = (rel_path or "").replace("\\", "/").lstrip("/")
    directory, _, name = rel.rpartition("/")
    if not name or name.lower() == INDEX_FILE:
        return None
    for entity in ENTITY_TYPES:
        if directory in entity.directories and name.endswith(entity.suffix):
            return entity.kind
    return None


def entry_key(rel_path: str) -> str | None:
    """条目文件 → `<kind>:<文件名去扩展名>`；不是条目时 None。"""
    kind = kind_of_path(rel_path)
    if kind is None:
        return None
    name = rel_path.replace("\\", "/").rpartition("/")[2]
    return f"{kind}:{name[: -len(BY_KIND[kind].suffix)]}"


def sections_of(kind: str) -> tuple[Section, ...]:
    entity = BY_KIND.get(kind)
    return entity.sections if entity else ()


# ---------------------------------------------------------------------------
# 维基链接
# ---------------------------------------------------------------------------

# 需要闭合的 `]]`；内部不跨行、不含方括号
_LINK_RE = re.compile(r"\[\[([^\[\]\n]+?)\]\]")
_MD_SUFFIX_RE = re.compile(r"\.md$", re.IGNORECASE)
# 关系与链接里表示「没有」的占位
EMPTY_TARGETS = frozenset({"无", "-", "—"})


@dataclass(frozen=True)
class Link:
    raw: str  # [[ 与 ]] 之间的原文
    target: str  # 名称部分（去掉锚点与别名，保留路径前缀）
    anchor: str | None
    alias: str | None
    span: tuple[int, int]
    name: str = field(default="")  # 去掉路径前缀与 .md 的名称

    @property
    def full_name(self) -> str:
        """名称连同锚点：存量文件名里带 `#` 时按它匹配。"""
        return f"{self.name}#{self.anchor}" if self.anchor is not None else self.name

    @property
    def label(self) -> str:
        """显示文字：有别名用别名，否则用全名。"""
        return self.alias or self.full_name


def _strip_name(target: str) -> str:
    name = target.strip().split("/")[-1].strip()
    return _MD_SUFFIX_RE.sub("", name).strip()


def parse_link_text(raw: str, span: tuple[int, int] = (0, 0)) -> Link:
    body, alias = raw, None
    # 表格单元格里的别名分隔写成 `\|`
    for separator in ("\\|", "|"):
        if separator in body:
            body, _, alias = body.partition(separator)
            alias = alias.strip() or None
            break
    target, hashmark, anchor = body.partition("#")
    target = target.rstrip("\\").strip()
    return Link(
        raw=raw,
        target=target,
        anchor=anchor.strip() if hashmark else None,
        alias=alias,
        span=span,
        name=_strip_name(target),
    )


def parse_links(text: str) -> list[Link]:
    """正文里全部 `[[...]]` 链接，按出现顺序（含重复）。"""
    return [parse_link_text(match.group(1), match.span()) for match in _LINK_RE.finditer(text or "")]


def parse_link(value: str) -> Link | None:
    """整段就是一个链接（关系端点、表格单元格）时解析它，否则 None。"""
    value = (value or "").strip()
    match = _LINK_RE.fullmatch(value)
    return parse_link_text(match.group(1), match.span()) if match else None


def name_candidates(link: Link | str) -> tuple[str, ...]:
    """解析顺序：全名（含 `#锚点`）→ 名称 → 两者按文件名编码后的形式。"""
    if isinstance(link, str):
        link = parse_link_text(link)
    ordered = []
    for candidate in (link.full_name, link.name, encode_filename(link.full_name), encode_filename(link.name)):
        if candidate and candidate not in ordered:
            ordered.append(candidate)
    return tuple(ordered)


def resolve(link: Link | str, names: Mapping | Iterable[str]) -> str | None:
    """链接指向哪个已有条目（names 是条目名集合或以条目名为键的字典）；指不到时 None。"""
    pool = names if isinstance(names, (Mapping, set, frozenset)) else set(names)
    for candidate in name_candidates(link):
        if candidate in pool:
            return candidate
    return None


def link_names(text: str, names: Mapping | Iterable[str] | None = None) -> list[str]:
    """正文链接到的条目名，按首次出现去重；给了 names 时按它解析，指不到的保留名称部分。"""
    pool = None if names is None else (names if isinstance(names, (Mapping, set, frozenset)) else set(names))
    result: list[str] = []
    for link in parse_links(text):
        if not link.name or link.name in EMPTY_TARGETS:
            continue
        resolved = resolve(link, pool) if pool is not None else None
        value = resolved or link.name
        if value not in result:
            result.append(value)
    return result


def unresolved(text: str, names: Mapping | Iterable[str]) -> list[Link]:
    """指不到任何条目的链接（按首次出现去重，用全名比较）。"""
    pool = names if isinstance(names, (Mapping, set, frozenset)) else set(names)
    seen, missing = set(), []
    for link in parse_links(text):
        if not link.name or link.name in EMPTY_TARGETS or link.full_name in seen:
            continue
        seen.add(link.full_name)
        if resolve(link, pool) is None:
            missing.append(link)
    return missing


def strip_links(text: str, use_alias: bool = True) -> str:
    """把链接换成显示文字：默认用别名（没有别名时用全名）；use_alias=False 时用链接写的名称与锚点。"""

    def replace(match: re.Match) -> str:
        link = parse_link_text(match.group(1))
        if use_alias:
            return link.label
        return f"{link.target}#{link.anchor}" if link.anchor is not None else link.target

    return _LINK_RE.sub(replace, text or "")


def references(text: str, name: str, names: Mapping | Iterable[str] | None = None) -> bool:
    """正文里有没有链接指向 name。"""
    pool = names if names is not None else {name}
    return any(resolve(link, pool) == name for link in parse_links(text))


# ---------------------------------------------------------------------------
# 条目文档结构：章节、表格行、关系（实体级差异与合入冲突检测用，方案 C9）
# ---------------------------------------------------------------------------

# 章节标题：一到三级标题，标题末尾的冒号不算（同 ontology_checker.extract_sections）
_HEADING_RE = re.compile(r"^(#{1,3})[ \t]+(.+?)[ \t]*$")
_TABLE_SPLIT_RE = re.compile(r"(?<!\\)\|")
_TABLE_RULE_RE = re.compile(r"^\s*\|?[\s\-:|]+\|?\s*$")


def _section_title(raw: str) -> str:
    title = raw.strip().rstrip("：:").strip()
    return SECTION_ALIASES.get(title, title)


def split_sections(text: str) -> list[tuple[str, str]]:
    """正文按标题切成 [(章节名, 正文)]，章节名去冒号并换成现行名；第一个标题之前的内容记在 "" 名下。

    只有一个一级标题、下面是二级标题时，一级标题是文档名，不算章节（同 MCP 文档模型的口径）。
    """
    lines = (text or "").split("\n")
    headings = []
    for index, line in enumerate(lines):
        match = _HEADING_RE.match(line)
        if match:
            headings.append((index, len(match.group(1)), match.group(2)))
    ones = [item for item in headings if item[1] == 1]
    skip = {ones[0][0]} if len(ones) == 1 and any(item[1] == 2 for item in headings) else set()
    starts = [item for item in headings if item[0] not in skip]
    sections: list[tuple[str, str]] = []
    first = starts[0][0] if starts else len(lines)
    head = "\n".join(line for index, line in enumerate(lines[:first]) if index not in skip).strip()
    if head:
        sections.append(("", head))
    for position, (index, _level, title) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        sections.append((_section_title(title), "\n".join(lines[index + 1:end]).strip()))
    return sections


def table_rows(body: str) -> list[dict]:
    """章节正文里的 Markdown 表格 → [{列名: 单元格}]（单元格里的 `\\|` 还原成 `|`）；没有表格返回 []。"""
    rows = [line.strip() for line in (body or "").split("\n") if line.strip().startswith("|")]
    if len(rows) < 2 or not _TABLE_RULE_RE.match(rows[1]):
        return []

    def cells(line: str) -> list[str]:
        return [cell.strip().replace("\\|", "|") for cell in _TABLE_SPLIT_RE.split(line.strip().strip("|"))]

    header = cells(rows[0])
    return [dict(zip(header, cells(line))) for line in rows[2:]]


def parse_document(kind: str, text: str) -> dict:
    """条目文档 → {sections: {章节: 正文}, order: [章节], rows: {表格章节: {行键: 行}}, links: {章节: [链接全名]},
    relations: [关系目标]}。

    行键取第一列（去掉链接写法）；关系目标是关系章节（relation_syntax.section）里链接到的条目全名，按出现去重。
    """
    sections: dict[str, str] = {}
    order: list[str] = []
    for title, body in split_sections(text):
        if title in sections:
            sections[title] = f"{sections[title]}\n\n{body}".strip()
        else:
            sections[title] = body
            order.append(title)
    tables = {section.title for section in sections_of(kind) if section.kind == "table"}
    rows: dict[str, dict[str, dict]] = {}
    for title, body in sections.items():
        parsed = table_rows(body) if title in tables or body.lstrip().startswith("|") else []
        if parsed:
            keyed: dict[str, dict] = {}
            for row in parsed:
                first = next(iter(row.values()), "")
                key = strip_links(first, use_alias=False).strip() or f"#{len(keyed) + 1}"
                keyed[key if key not in keyed else f"{key}#{len(keyed) + 1}"] = row
            rows[title] = keyed
    links = {title: [] for title in order}
    for title in order:
        for link in parse_links(sections[title]):
            if link.name and link.name not in EMPTY_TARGETS and link.full_name not in links[title]:
                links[title].append(link.full_name)
    relations = list(links.get(RELATION_SECTION, []))
    return {"sections": sections, "order": order, "rows": rows, "links": links, "relations": relations}


# ---------------------------------------------------------------------------
# 文件名编码（key 规则）
# ---------------------------------------------------------------------------

# 文件系统保留字符，加上和链接语法冲突的 # | [ ] 与 &；同时转义 %，避免 A/B 与字面 A%2FB 重名
_FILENAME_ESCAPES = frozenset('%/\\<>:"|?*#&[]')
_WINDOWS_RESERVED_RE = re.compile(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?")
MAX_KEY_LENGTH = 80


def encode_filename(name: str) -> str:
    """业务名 → 文件名（不含扩展名）：保留字符百分号编码，结尾的空格和点、保留设备名也编码。"""
    escaped = "".join(
        f"%{ord(char):02X}" if char in _FILENAME_ESCAPES or ord(char) < 32 else char
        for char in name or ""
    )
    head = escaped.rstrip(" .")
    escaped = head + "".join(f"%{ord(char):02X}" for char in escaped[len(head):])
    if escaped and (escaped.lower() == "index" or _WINDOWS_RESERVED_RE.fullmatch(escaped)):
        escaped = f"%{ord(escaped[0]):02X}" + escaped[1:]
    return escaped


_PERCENT_RE = re.compile(r"%([0-9A-Fa-f]{2})")


def decode_filename(stem: str) -> str:
    return _PERCENT_RE.sub(lambda match: chr(int(match.group(1), 16)), stem or "")


def key_problem(key: str) -> str | None:
    """新条目 key 是否合法：None 表示合法，否则返回原因（导入遇到非法 key 时进待处理）。"""
    if not key or key != key.strip():
        return "empty_or_padded"
    if len(key) > MAX_KEY_LENGTH:
        return "too_long"
    if ".." in key or any(char in key for char in '/\\#?&|[]') or any(ord(char) < 32 or ord(char) == 127 for char in key):
        return "reserved_character"
    return None


# ---------------------------------------------------------------------------
# 对外视图（GET /api/v1/metamodel）
# ---------------------------------------------------------------------------


def as_dict() -> dict:
    return {
        "version": METAMODEL_VERSION,
        "entity_types": [
            {
                "kind": entity.kind,
                "label": entity.label,
                "directory": entity.directory,
                "readonly_aliases": list(entity.aliases),
                "suffix": entity.suffix,
                "recursive": entity.recursive,
                "sections": [section.as_dict() for section in entity.sections],
            }
            for entity in ENTITY_TYPES
        ],
        "section_aliases": dict(SECTION_ALIASES),
        "link_syntax": {
            "pattern": _LINK_RE.pattern,
            "anchor_separator": "#",
            "alias_separator": "|",
            "escaped_alias_separator": "\\|",
            "path_prefix": "last_segment",
            "resolution": ["full_name", "name", "encoded_full_name", "encoded_name"],
            "empty_targets": sorted(EMPTY_TARGETS),
        },
        "relation_syntax": {
            "section": RELATION_SECTION,
            "arrows": ["→", "->"],
            "current_object": "当前对象",
        },
        "filename_encoding": {
            "scheme": "percent",
            "escaped": sorted(_FILENAME_ESCAPES),
            "max_key_length": MAX_KEY_LENGTH,
        },
        "index_file": INDEX_FILE,
    }
