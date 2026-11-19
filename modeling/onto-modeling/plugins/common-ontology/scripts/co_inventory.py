#!/usr/bin/env python3
"""公共本体（common-ontology）：把上传的知识包读成统一清单，按结构预分组，写抽取决定的草稿。

    python3 .claude/scripts/co_inventory.py --min 2           # 清单 + 决定草稿（保留已有决定）
    python3 .claude/scripts/co_inventory.py --min 2 --apply   # 另把缺条目的实类草稿写进 ontology/（不覆盖已有的）

输入（只读）：ext/common-ontology/uploads/inputs/<上传 id>/ 下的知识包文本与 upload.json。
输出：
- ext/common-ontology/result/merge.yaml：抽取决定。已有的决定原样保留（删掉已删包的成员），新条目能并进已有组的并进去，
  其余按结构预分组、给出建议的归类；每组上方的注释是依据（共有属性、多出的属性、规则原文、步骤），每次重写；
- tmp/common-ontology/inventory.json：全量清单（不进版本）；
- tmp/common-ontology/drafts/<类型>/<名称>.md：还没有 ontology/ 条目的实类的草稿（交集属性、各包叫法写进别名）。
标准输出最多约 30 行；清单和依据都在文件里。只用标准库和 PyYAML。co_check.py 导入本文件复用解析与匹配。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from collections import Counter
from pathlib import Path

import yaml

PACK = "common-ontology"
UPLOADS = Path("ext") / PACK / "uploads" / "inputs"
RESULT = Path("ext") / PACK / "result"
MERGE = RESULT / "merge.yaml"
STATUS = RESULT / "status.json"
WORK = Path("tmp") / PACK
KINDS = ("object", "operation", "rule")
PACKAGE_DIRS = {"object": "objects", "operation": "operations", "rule": "rules"}
ONTOLOGY_DIRS = {"object": "ontology/object", "operation": "ontology/operation", "rule": "ontology/rule"}
KIND_LABELS = {"object": "对象", "operation": "操作", "rule": "规则"}
CLASSES = ("real", "virtual", "none")
CLASS_LABELS = {"real": "实类", "virtual": "虚类", "none": "没抽出"}
DEFAULT_MIN = 2
MAX_MIN = 20
CURRENT_OBJECT = "当前对象"

_FRONT_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.S)
_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*#*\s*$")
_LINK_RE = re.compile(r"\[\[([^\]\n]+)\]\]")
_LIST_SPLIT_RE = re.compile(r"[,，、;；/]")
_EMPTY = {"", "无", "-", "—", "–", "n/a", "none", "null"}
# 名称比较：去掉空白、标点、下划线与连字符，英文转小写
_NAME_NOISE_RE = re.compile(r"[\s\W_]+", re.UNICODE)
# 逻辑比较：去掉空白与不影响含义的标点，保留比较、运算符号
_TEXT_NOISE_RE = re.compile(r"[\s，。、；：,.;:!！?？\"'“”‘’（）()\[\]【】「」《》`*_|]+")
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_LITERAL_RE = re.compile(r"\d+(?:\.\d+)?|「[^」\n]*」|“[^”\n]*”|\"[^\"\n]*\"")
_STEP_RE = re.compile(r"^\s*(?:\d+(?:\.\d+)*\s*[.、)）]|步骤\s*\d+\s*[:：.、]?|[-*])\s*(.+?)\s*$")
_BELOW_RE = re.compile(r"^不到 \d+ 个包$")
# 预分组自己写的没抽出原因（空、门槛、逻辑不一致……）：带这些原因的组每次重新判；助手写了别的原因的保留
_AUTO_NOTE_RE = re.compile(r"^(?:|不到 \d+ 个包|(?:步骤或前置条件|逻辑)(?:和「.*」)?不一致|引用的「.*」不是实类"
                           r"|同一个包里有多个候选，拆开后再判断)$")
_TYPE_FAMILIES = {
    "字符串": "text", "文本": "text", "string": "text", "str": "text", "text": "text",
    "整数": "int", "integer": "int", "int": "int",
    "小数": "number", "数字": "number", "数值": "number", "decimal": "number", "float": "number", "number": "number",
    "布尔": "bool", "boolean": "bool", "bool": "bool",
    "日期": "date", "日期时间": "date", "时间": "date", "date": "date", "datetime": "date",
    "枚举": "enum", "enum": "enum", "对象引用": "ref", "引用": "ref",
}


class InputError(Exception):
    """输入或决定文件有问题：main 打印消息，退出码 1。"""


# ---------------------------------------------------------------------------
# 文本小件
# ---------------------------------------------------------------------------


def split_frontmatter(text: str) -> tuple[dict, str]:
    match = _FRONT_RE.match(text)
    if not match:
        return {}, text
    try:
        data = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        data = {}
    return (data if isinstance(data, dict) else {}), text[match.end():]


def sections(body: str) -> dict[str, str]:
    """一到三级标题 → 段内正文（同名标题取第一个；代码块里的 # 不算标题）。"""
    out: dict[str, str] = {}
    current, buffer, fence = None, [], False
    for line in body.splitlines():
        if line.strip().startswith("```"):
            fence = not fence
        match = None if fence else _HEADING_RE.match(line)
        if match:
            if current is not None:
                out.setdefault(current, "\n".join(buffer).strip())
            current, buffer = match.group(2).strip().rstrip("：:").strip(), []
        elif current is not None:
            buffer.append(line)
    if current is not None:
        out.setdefault(current, "\n".join(buffer).strip())
    return out


def strip_h1(body: str) -> str:
    lines = body.strip().splitlines()
    if lines and re.match(r"^#\s+", lines[0]):
        lines = lines[1:]
    return "\n".join(lines).strip()


def _cells(line: str) -> list[str]:
    inner = line.strip()
    inner = inner[1:] if inner.startswith("|") else inner
    inner = inner[:-1] if inner.endswith("|") else inner
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", inner)]


def table_rows(text: str) -> list[dict[str, str]]:
    """正文里第一张 Markdown 表：每行一个「表头 → 单元格」。"""
    lines = (text or "").splitlines()
    for index in range(len(lines) - 1):
        if lines[index].strip().startswith("|") and re.match(r"^\s*\|?\s*:?-{2,}", lines[index + 1]):
            head = _cells(lines[index])
            rows = []
            for line in lines[index + 2:]:
                if not line.strip().startswith("|"):
                    break
                cells = _cells(line)
                rows.append({name: (cells[i] if i < len(cells) else "") for i, name in enumerate(head)})
            return rows
    return []


def plain(value: str) -> str:
    """`[[名称|别名]]` / `名称` → 名称。"""
    text = str(value or "").strip()
    match = re.fullmatch(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]", text)
    return (match.group(1) if match else text).strip()


def links(text: str) -> list[str]:
    seen: list[str] = []
    for raw in _LINK_RE.findall(text or ""):
        name = raw.split("|", 1)[0].strip()
        if name and not name.startswith("{") and name not in seen:
            seen.append(name)
    return seen


def split_list(value: str) -> list[str]:
    items = [item.strip() for item in _LIST_SPLIT_RE.split(value or "")]
    return [item for item in items if item.lower() not in _EMPTY]


def norm_name(value: str) -> str:
    return _NAME_NOISE_RE.sub("", str(value or "")).lower()


def norm_text(value: str) -> str:
    return _TEXT_NOISE_RE.sub("", str(value or "")).lower()


def first_paragraph(value: str) -> str:
    for block in re.split(r"\n\s*\n", (value or "").strip()):
        block = block.strip()
        if block and not block.startswith(("{", ">")):
            return block
    return ""


def clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def safe_name(name: str) -> str:
    """条目名 → 文件名：去掉路径分隔与控制字符。"""
    cleaned = re.sub(r"[\\/:*?\"<>|\x00-\x1f\x7f]", "-", str(name or "")).strip().strip(".")
    return cleaned[:100]


def quote(value) -> str:
    """YAML 标量：JSON 字符串本身就是合法的 YAML 双引号标量。"""
    return json.dumps("" if value is None else str(value), ensure_ascii=False)


# ---------------------------------------------------------------------------
# 条目解析（发布包写法与工作区写法都认）
# ---------------------------------------------------------------------------


def _cell(row: dict, *names: str) -> str:
    for name in names:
        if name in row:
            return str(row[name] or "").strip()
    return ""


def parse_attribute(row: dict) -> dict | None:
    """属性表一行：工作区「对象属性清单」与发布包「字段」两种表头。发布包里「必填」就是主键。"""
    cn = _cell(row, "中文名称", "中文名")
    en = _cell(row, "英文名称", "字段 key", "字段key")
    if not cn and not en or cn.startswith("{"):
        return None
    return {
        "cn": cn, "en": en, "desc": _cell(row, "描述", "业务说明"),
        "pk": _cell(row, "主键", "必填") == "是", "type": _cell(row, "类型"),
        "aliases": split_list(_cell(row, "属性别名", "别名")), "rule": _cell(row, "属性规则"),
    }


def _split_arrows(text: str) -> list[str]:
    """按最外层的 → 切开；括注里的箭头不算。"""
    parts, depth, current = [], 0, ""
    text = text.replace("->", "→")
    for char in text:
        if char in "（(【[":
            depth += 1
        elif char in "）)】]":
            depth = max(0, depth - 1)
        if char == "→" and depth == 0:
            parts.append(current.strip())
            current = ""
        else:
            current += char
    parts.append(current.strip())
    return parts


def _relation_join(text: str) -> tuple[str, str, str]:
    """`属于（a → b）` → (属于, a, b)。"""
    match = re.match(r"^([^（(]+)[（(]([^）)]*)[）)]\s*$", text.strip())
    if not match:
        return text.strip(), "", ""
    inner = [part.strip() for part in match.group(2).replace("->", "→").split("→")]
    if len(inner) != 2:
        return match.group(1).strip(), "", ""
    return match.group(1).strip(), inner[0], inner[1]


def object_edges(name: str, sec: dict) -> list[dict]:
    """对象里声明的关系：发布包的「关系」表（都是本对象出发），工作区的「关联对象」箭头（两个方向）。"""
    edges = []
    for row in table_rows(sec.get("关系") or ""):
        relation, target = _cell(row, "关系"), plain(_cell(row, "目标"))
        if relation and target:
            edges.append({"name": relation, "source": name, "target": target,
                          "source_field": _cell(row, "源关联字段"), "target_field": _cell(row, "目标关联字段")})
    for line in (sec.get("关联对象") or "").splitlines():
        line = line.strip()
        if not line.startswith(("-", "*")) or "{" in line:
            continue
        parts = _split_arrows(line.lstrip("-* ").strip())
        if len(parts) != 3:
            continue
        relation, source_field, target_field = _relation_join(parts[1])
        source = name if plain(parts[0]) in (CURRENT_OBJECT, name) else plain(parts[0])
        target = name if plain(parts[2]) in (CURRENT_OBJECT, name) else plain(parts[2])
        if relation and name in (source, target):
            edges.append({"name": relation, "source": source, "target": target,
                          "source_field": source_field, "target_field": target_field})
    return edges


def step_lines(text: str) -> list[str]:
    steps = [match.group(1) for line in (text or "").splitlines() if (match := _STEP_RE.match(line))]
    if steps:
        return steps
    return [line.strip() for line in (text or "").splitlines()
            if line.strip() and not line.lstrip().startswith(("#", "|", ">", "{"))]


def parse_entry(kind: str, pack: str, name: str, text: str, rel: str) -> dict:
    meta, body = split_frontmatter(text)
    sec = sections(body)
    entry = {"pack": pack, "kind": kind, "name": name, "path": rel, "sections": sec, "body": body.strip()}
    if kind == "object":
        rows = table_rows(sec.get("对象属性清单") or sec.get("字段") or "")
        entry.update(
            description=first_paragraph(sec.get("对象描述")),
            aliases=split_list(sec.get("对象别名") or ""),
            english=first_paragraph(sec.get("英文名称")),
            attributes=[attr for attr in (parse_attribute(row) for row in rows) if attr],
            edges=object_edges(name, sec),
        )
    elif kind == "operation":
        logic = sec.get("逻辑描述")
        entry.update(
            description=first_paragraph(sec.get("业务描述")),
            trigger=(sec.get("触发条件") or "").strip(),
            steps=step_lines(logic if logic is not None else strip_h1(body)),
            links=links(body),
        )
    else:
        expression = sec.get("约束表达") or strip_h1(body)
        entry.update(
            description=first_paragraph(sec.get("规则描述")),
            expression=expression.strip(),
            links=links(body),
        )
    return entry


# ---------------------------------------------------------------------------
# 清单
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


class Inventory:
    """上传区里的全部输入包与条目。条目键是（上传 id, 类型, 名称）。"""

    def __init__(self, packs: list[dict]):
        self.packs = packs
        self.pack_by_id = {pack["id"]: pack for pack in packs}
        self.entries = [entry for pack in packs for entry in pack["entries"]]
        self.index = {(entry["pack"], entry["kind"], entry["name"]): entry for entry in self.entries}

    def get(self, pack: str, kind: str, name: str) -> dict | None:
        return self.index.get((pack, kind, name))

    def pack_name(self, pack: str) -> str:
        return (self.pack_by_id.get(pack) or {}).get("name") or pack

    def edges_of(self, pack: str, name: str) -> list[tuple[str, str, dict]]:
        """对象在本包里参与的关系：(方向 out/in, 另一端对象名, 关系)。两端都声明的同一条只算一次。"""
        seen, out = set(), []
        for entry in (self.pack_by_id.get(pack) or {}).get("entries", []):
            if entry["kind"] != "object":
                continue
            for edge in entry["edges"]:
                key = (edge["source"], norm_name(edge["name"]), edge["target"])
                if key in seen or name not in (edge["source"], edge["target"]):
                    continue
                seen.add(key)
                if edge["source"] == name:
                    out.append(("out", edge["target"], edge))
                else:
                    out.append(("in", edge["source"], edge))
        return out

    def summary(self) -> dict:
        counts = Counter(entry["kind"] for entry in self.entries)
        return {kind: counts.get(kind, 0) for kind in KINDS}


def load_inventory(root: Path = Path(".")) -> Inventory:
    base = root / UPLOADS
    packs = []
    if base.is_dir() and not base.is_symlink():
        for folder in sorted(path for path in base.iterdir() if path.is_dir() and not path.name.startswith(".")):
            meta = _read_json(folder / "upload.json")
            manifest = _read_yaml(folder / "manifest.yaml")
            package = meta.get("package") if isinstance(meta.get("package"), dict) else {}
            pack = {
                "id": folder.name,
                "package_id": str(package.get("id") or manifest.get("id") or folder.name),
                "name": str(package.get("name") or manifest.get("name") or folder.name),
                "version": str(package.get("version") or manifest.get("version") or ""),
                "digest": str(meta.get("digest") or ""),
                "metrics": len(list((folder / "metrics").glob("*.md"))) if (folder / "metrics").is_dir() else 0,
                "entries": [],
            }
            for kind, sub in PACKAGE_DIRS.items():
                directory = folder / sub
                if not directory.is_dir() or directory.is_symlink():
                    continue
                for path in sorted(directory.rglob("*.md")):
                    if path.name.lower() == "index.md" or path.is_symlink() or not path.is_file():
                        continue
                    text = path.read_text(encoding="utf-8", errors="replace")
                    rel = path.relative_to(root).as_posix() if path.is_absolute() else path.as_posix()
                    pack["entries"].append(parse_entry(kind, folder.name, path.stem, text, rel))
            pack["uploaded_at"] = meta.get("uploaded_at") if isinstance(meta.get("uploaded_at"), (int, float)) else 0
            packs.append(pack)
    # 按上传先后排：组名、代表成员在名字一样多时取先上传的包
    packs.sort(key=lambda pack: (pack["uploaded_at"], pack["id"]))
    return Inventory(packs)


# ---------------------------------------------------------------------------
# 属性与关系的匹配
# ---------------------------------------------------------------------------


def attribute_tokens(attr: dict) -> set[str]:
    tokens = {norm_name(attr.get("cn")), norm_name(attr.get("en"))}
    tokens |= {norm_name(alias) for alias in attr.get("aliases") or []}
    return tokens - {""}


def _type_family(attr: dict) -> str:
    text = str(attr.get("type") or "").strip().lower()
    return _TYPE_FAMILIES.get(text, text)


def types_compatible(a: dict, b: dict) -> bool:
    left, right = _type_family(a), _type_family(b)
    return not left or not right or left == right or {left, right} <= {"int", "number"}


def _single_pk(obj: dict) -> bool:
    return sum(1 for attr in obj.get("attributes") or [] if attr["pk"]) == 1


def find_attribute(attr: dict, owner: dict, other: dict, used: set[int]) -> int | None:
    """other 里和 attr 含义相同的属性下标：名称、英文名、别名有交集且类型相容；两边各只有一个主键时主键对主键。"""
    candidates = list(enumerate(other.get("attributes") or []))
    if attr["pk"] and _single_pk(owner) and _single_pk(other):
        return next((index for index, candidate in candidates if candidate["pk"] and index not in used), None)
    tokens = attribute_tokens(attr)
    for index, candidate in candidates:
        if index not in used and tokens & attribute_tokens(candidate) and types_compatible(attr, candidate):
            return index
    return None


def common_attributes(objects: list[dict], anchor: int = 0) -> list[list[dict]]:
    """每个成员都有的属性：[[各成员的这个属性]]，顺序按 anchor 成员。"""
    base = objects[anchor]
    used = [set() for _ in objects]
    result = []
    for attr in base.get("attributes") or []:
        picks: list[int | None] = []
        for index, obj in enumerate(objects):
            picks.append(None if index == anchor else find_attribute(attr, base, obj, used[index]))
        if any(pick is None for index, pick in enumerate(picks) if index != anchor):
            continue
        row = []
        for index, obj in enumerate(objects):
            if index == anchor:
                row.append(attr)
            else:
                used[index].add(picks[index])
                row.append(obj["attributes"][picks[index]])
        result.append(row)
    return result


def unmatched_attributes(common: list[dict], member: dict) -> list[dict]:
    """member 里没被公共属性对上的属性（继承映射的「多出」）。common 是公共条目的属性列表。"""
    holder = {"attributes": common}
    used: set[int] = set()
    extra = []
    for attr in member.get("attributes") or []:
        index = find_attribute(attr, member, holder, used)
        if index is None:
            extra.append(attr)
        else:
            used.add(index)
    return extra


def structure_match(a: dict, b: dict) -> bool:
    """两个对象结构相近：主键以外至少两个属性对得上，且占较少一方的六成以上。"""
    left = [attr for attr in a.get("attributes") or [] if not attr["pk"]]
    right = {"attributes": [attr for attr in b.get("attributes") or [] if not attr["pk"]]}
    if not left or not right["attributes"]:
        return False
    used: set[int] = set()
    matched = 0
    for attr in left:
        index = find_attribute(attr, a, right, used)
        if index is not None:
            used.add(index)
            matched += 1
    return matched >= 2 and matched / min(len(left), len(right["attributes"])) >= 0.6


def identity_tokens(entry: dict) -> set[str]:
    tokens = {norm_name(entry["name"])}
    tokens |= {norm_name(alias) for alias in entry.get("aliases") or []}
    if entry.get("english"):
        tokens.add(norm_name(entry["english"]))
    return tokens - {""}


def structure_signature(obj: dict) -> tuple:
    """结构签名：属性（英文名或中文名 + 类型族）的集合，主键不看名字。"""
    items = []
    for attr in obj.get("attributes") or []:
        name = "#pk" if attr["pk"] else (norm_name(attr["en"]) or norm_name(attr["cn"]))
        items.append((name, _type_family(attr)))
    return tuple(sorted(items))


# ---------------------------------------------------------------------------
# 预分组与建议
# ---------------------------------------------------------------------------


class _Union:
    def __init__(self, size: int):
        self.parent = list(range(size))

    def find(self, index: int) -> int:
        while self.parent[index] != index:
            self.parent[index] = self.parent[self.parent[index]]
            index = self.parent[index]
        return index

    def join(self, a: int, b: int) -> None:
        self.parent[self.find(a)] = self.find(b)

    def components(self) -> list[list[int]]:
        groups: dict[int, list[int]] = {}
        for index in range(len(self.parent)):
            groups.setdefault(self.find(index), []).append(index)
        return list(groups.values())


def objects_match(a: dict, b: dict) -> bool:
    return bool(identity_tokens(a) & identity_tokens(b)) or structure_match(a, b)


def _letters(number: int) -> str:
    """组号写成字母：签名里的数字会被当成参数换掉，组号不能是数字。"""
    out = ""
    number += 1
    while number:
        number, rest = divmod(number - 1, 26)
        out = chr(ord("a") + rest) + out
    return out


class Signatures:
    """操作、规则的比较签名：正文里的 [[对象]] 换成它所在的组，各包叫法不同也能比。"""

    def __init__(self, object_group: dict[tuple[str, str], int]):
        self.object_group = object_group

    def _mapped(self, pack: str, text: str) -> str:
        def repl(match):
            name = match.group(1).split("|", 1)[0].strip()
            group = self.object_group.get((pack, name))
            return f"«{_letters(group)}»" if group is not None else f"«{norm_name(name)}»"
        return _LINK_RE.sub(repl, text or "")

    def _raw(self, entry: dict) -> list[str]:
        if entry["kind"] == "rule":
            return [self._mapped(entry["pack"], entry["expression"])]
        return [self._mapped(entry["pack"], step) for step in entry["steps"]]

    def text(self, entry: dict) -> str:
        return "\n".join(norm_text(part) for part in self._raw(entry))

    def params(self, entry: dict) -> str:
        """数值与引号里的枚举值换成 #：只差这些的规则签名相同。"""
        return "\n".join(norm_text(_LITERAL_RE.sub("#", part)) for part in self._raw(entry))

    def trigger(self, entry: dict) -> str:
        return norm_text(self._mapped(entry["pack"], entry.get("trigger") or ""))


def entries_match(a: dict, b: dict, signatures: Signatures) -> bool:
    if a["kind"] == "object":
        return objects_match(a, b)
    if norm_name(a["name"]) == norm_name(b["name"]):
        return True
    if a["kind"] == "operation":
        return len(a["steps"]) >= 2 and signatures.text(a) == signatures.text(b)
    return len(signatures.text(a)) >= 4 and (signatures.text(a) == signatures.text(b)
                                              or signatures.params(a) == signatures.params(b))


def suggest_name(entries: list[dict]) -> str:
    """组名：成员名与别名里被最多成员用到的那个（成员名优先），一样多取靠前的。"""
    score: Counter = Counter()
    order: list[str] = []
    for entry in entries:
        for candidate in [entry["name"], *(entry.get("aliases") or [])]:
            if candidate not in order:
                order.append(candidate)
            score[candidate] += 1.5 if candidate == entry["name"] else 1
    return max(order, key=lambda name: (score[name], -order.index(name)))


def anchor_index(group: dict) -> int:
    """组里「代表」成员：条目名就是组名的那个，没有就取第一个。"""
    return next((index for index, member in enumerate(group["members"]) if member["entry"] == group["name"]), 0)


def new_group(kind: str, entries: list[dict], *, name: str = "", cls: str = "none", note: str = "") -> dict:
    return {"name": name, "kind": kind, "class": cls, "definition": "", "template": "", "params": [], "note": note,
            "was": "", "members": [{"pack": entry["pack"], "entry": entry["name"], "values": {}, "extra": ""} for entry in entries]}


def _distinct_packs(entries: list[dict]) -> int:
    return len({entry["pack"] for entry in entries})


def _has_duplicate_pack(entries: list[dict]) -> bool:
    return _distinct_packs(entries) != len(entries)


def decide_objects(entries: list[dict], minimum: int) -> dict:
    if len(entries) == 1:
        return new_group("object", entries)
    name = suggest_name(entries)
    if _has_duplicate_pack(entries):
        return new_group("object", entries, name=name, note="同一个包里有多个候选，拆开后再判断")
    if _distinct_packs(entries) < minimum:
        return new_group("object", entries, name=name, note=f"不到 {minimum} 个包")
    group = new_group("object", entries, name=name)
    common = common_attributes(entries, anchor_index(group))
    group["class"] = "real" if any(not row[0]["pk"] for row in common) else "virtual"
    return group


def _partition(entries: list[dict], key) -> list[list[dict]]:
    buckets: dict[str, list[dict]] = {}
    for entry in entries:
        buckets.setdefault(key(entry), []).append(entry)
    return sorted(buckets.values(), key=lambda bucket: -_distinct_packs(bucket))


def _template(entries: list[dict], rename) -> tuple[str, list[dict], list[dict]]:
    """只差数值或枚举的规则：模板（不同的字面量换成 {参数N}）、参数、各成员的取值。"""
    texts = [rename(entry, entry["expression"]) for entry in entries]
    literals = [_LITERAL_RE.findall(text) for text in texts]
    if len({len(items) for items in literals}) != 1:
        return "", [], [{} for _ in entries]
    differing = [index for index in range(len(literals[0])) if len({items[index] for items in literals}) > 1]
    names = {index: f"参数{number}" for number, index in enumerate(differing, 1)}
    position = iter(range(len(literals[0])))

    def replace(match):
        index = next(position)
        return "{" + names[index] + "}" if index in names else match.group(0)

    template = _LITERAL_RE.sub(replace, texts[0])
    params = [{"name": names[index], "meaning": ""} for index in differing]
    values = [{names[index]: items[index].strip("「」“”\"") for index in differing} for items in literals]
    return template, params, values


def decide_flows(kind: str, entries: list[dict], minimum: int, signatures: Signatures, rename) -> list[dict]:
    """操作与规则：逻辑（步骤 + 前置条件）完全一致的成实类；规则只差参数的成模板；其余没抽出。"""
    if len(entries) == 1:
        return [new_group(kind, entries)]
    name = suggest_name(entries)
    if kind == "operation":
        buckets = _partition(entries, lambda entry: signatures.text(entry) + "\n" + signatures.trigger(entry))
    else:
        buckets = _partition(entries, signatures.text)
    best = buckets[0]
    if _distinct_packs(best) >= minimum and not _has_duplicate_pack(best):
        groups = [new_group(kind, best, name=suggest_name(best), cls="real")]
        rest = [entry for bucket in buckets[1:] for entry in bucket]
        if rest:
            label = "步骤或前置条件" if kind == "operation" else "逻辑"
            groups.append(new_group(kind, rest, note=f"{label}和「{groups[0]['name']}」不一致"))
        return groups
    if kind == "rule":
        params = _partition(entries, signatures.params)[0]
        if len(params) == len(entries) and _distinct_packs(entries) >= minimum and not _has_duplicate_pack(entries):
            group = new_group(kind, entries, name=name, cls="virtual")
            group["template"], group["params"], values = _template(entries, rename)
            for member, value in zip(group["members"], values):
                member["values"] = value
            return [group]
    if _distinct_packs(entries) < minimum:
        return [new_group(kind, entries, name=name, note=f"不到 {minimum} 个包")]
    label = "步骤或前置条件不一致" if kind == "operation" else "逻辑不一致"
    return [new_group(kind, entries, name=name, note=label)]


# ---------------------------------------------------------------------------
# 抽取决定（merge.yaml）
# ---------------------------------------------------------------------------


def _text_value(value) -> str:
    return "" if value is None else str(value).strip()


def _normalize_group(raw, position: int, problems: list[str]) -> dict | None:
    where = f"groups 第 {position} 项"
    if not isinstance(raw, dict):
        problems.append(f"{where} 要是映射")
        return None
    members = []
    for item in raw.get("members") or []:
        if not isinstance(item, dict) or not item.get("pack") or not item.get("entry"):
            problems.append(f"{where}（{raw.get('name') or '未命名'}）的成员要写 pack 和 entry：{item!r}"[:200])
            continue
        values = item.get("values") or {}
        if not isinstance(values, dict):
            problems.append(f"{where} 的成员 {item.get('entry')} 的 values 要是「参数: 值」")
            values = {}
        members.append({"pack": str(item["pack"]).strip(), "entry": str(item["entry"]).strip(),
                        "values": {str(k): _text_value(v) for k, v in values.items()},
                        "extra": _text_value(item.get("extra"))})
    params = []
    for item in raw.get("params") or []:
        if isinstance(item, dict) and item.get("name"):
            params.append({"name": str(item["name"]).strip(), "meaning": _text_value(item.get("meaning"))})
        elif isinstance(item, str) and item.strip():
            params.append({"name": item.strip(), "meaning": ""})
    return {"name": _text_value(raw.get("name")), "kind": _text_value(raw.get("kind")),
            "class": _text_value(raw.get("class")) or "none", "definition": _text_value(raw.get("definition")),
            "template": _text_value(raw.get("template")), "params": params, "note": _text_value(raw.get("note")),
            "was": _text_value(raw.get("was")), "members": members}


def load_merge(path: Path = MERGE) -> tuple[dict | None, list[str]]:
    """读抽取决定：(决定或 None, 格式问题)。文件不存在返回 (None, [])。"""
    if not path.is_file():
        return None, []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f"第 {mark.line + 1} 行" if mark else ""
        return None, [f"{path.as_posix()} 不是合法的 YAML（{where}）：{getattr(exc, 'problem', '') or exc}"]
    except (OSError, UnicodeDecodeError) as exc:
        return None, [f"{path.as_posix()} 读不出来：{exc}"]
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return None, [f"{path.as_posix()} 顶层要是映射（min、groups）"]
    problems: list[str] = []
    minimum = data.get("min", DEFAULT_MIN)
    if isinstance(minimum, bool) or not isinstance(minimum, int) or not 2 <= minimum <= MAX_MIN:
        problems.append(f"min 要是 2～{MAX_MIN} 的整数")
        minimum = DEFAULT_MIN
    raw_groups = data.get("groups") or []
    if not isinstance(raw_groups, list):
        problems.append("groups 要是列表")
        raw_groups = []
    groups = [group for position, raw in enumerate(raw_groups, 1)
              if (group := _normalize_group(raw, position, problems)) is not None]
    return {"min": minimum, "groups": groups}, problems


def _evidence(group: dict, inventory: Inventory) -> list[str]:
    """写在每组上方的依据（每次重写）：对象写共有与多出的属性，操作写步骤，规则写约束原文。"""
    entries = [inventory.get(member["pack"], group["kind"], member["entry"]) for member in group["members"]]
    entries = [entry for entry in entries if entry]
    if not entries:
        return []
    label = lambda entry: f"{inventory.pack_name(entry['pack'])}/{entry['name']}"  # noqa: E731
    lines = []
    if group["kind"] == "object":
        if len(entries) == 1:
            names = [attr["cn"] or attr["en"] for attr in entries[0]["attributes"]]
            return [clip("属性：" + "、".join(names), 160)]
        anchor = next((index for index, entry in enumerate(entries) if entry["name"] == group["name"]), 0)
        common = common_attributes(entries, anchor)
        shown = []
        for row in common:
            names = {attr["cn"] or attr["en"] for attr in row}
            main = row[anchor]["cn"] or row[anchor]["en"]
            others = sorted(names - {main})
            shown.append(main + (f"({'/'.join(others)})" if others else ""))
        same = len({structure_signature(entry) for entry in entries}) == 1
        lines.append(clip(("结构一致；" if same else "") + "共有：" + ("、".join(shown) or "无"), 200))
        extra = []
        holder = [row[anchor] for row in common]
        for entry in entries:
            rest = unmatched_attributes(holder, entry)
            if rest:
                extra.append(f"{label(entry)} " + "、".join(attr["cn"] or attr["en"] for attr in rest))
        if extra:
            lines.append(clip("多出：" + "；".join(extra), 200))
    elif group["kind"] == "operation":
        for entry in entries[:3]:
            lines.append(clip(f"{label(entry)}：{len(entry['steps'])} 步；" + "；".join(entry["steps"]), 150))
    else:
        for entry in entries[:3]:
            lines.append(clip(f"{label(entry)}：{entry['expression']}", 150))
    if len(entries) > 3 and group["kind"] != "object":
        lines.append(f"（还有 {len(entries) - 3} 个成员）")
    return lines


def dump_merge(minimum: int, groups: list[dict], inventory: Inventory, joined: dict[int, list[str]]) -> str:
    lines = [
        "# 公共本体的抽取决定。co_inventory.py 生成草稿并保留已有决定，助手按语义改，co_check.py 据此生成结果并核对。",
        "# class：real 实类（ontology/ 里有同名条目）| virtual 虚类（对象写 definition 进术语表；规则写 template、params 和每个成员的",
        "#   values 成为规则模板；操作不能是虚类）| none 没抽出（note 写原因）。每组上方的注释是依据，每次重新生成。",
        f"min: {minimum}",
        "groups:",
    ]
    for index, group in enumerate(groups):
        lines.append(f"- name: {quote(group['name'])}")
        lines.append(f"  kind: {group['kind']}")
        lines.append(f"  class: {group['class']}")
        for note in _evidence(group, inventory) + [f"新加入：{name}，按依据核对归类" for name in joined.get(index, [])]:
            lines.append(f"  # {note}")
        if (group["kind"] == "object" and group["class"] == "virtual") or group.get("definition"):
            lines.append(f"  definition: {quote(group.get('definition'))}")
        if (group["kind"] == "rule" and group["class"] == "virtual") or group.get("template"):
            lines.append(f"  template: {quote(group.get('template'))}")
        if group.get("params"):
            lines.append("  params:")
            lines += [f"  - {{name: {quote(param['name'])}, meaning: {quote(param.get('meaning'))}}}"
                      for param in group["params"]]
        if group.get("note"):
            lines.append(f"  note: {quote(group['note'])}")
        if group.get("was"):
            lines.append(f"  was: {group['was']}")
        lines.append("  members:")
        for member in group["members"]:
            parts = [f"pack: {member['pack']}", f"entry: {quote(member['entry'])}"]
            if member.get("values"):
                parts.append("values: {" + ", ".join(f"{quote(k)}: {quote(v)}" for k, v in member["values"].items()) + "}")
            if member.get("extra"):
                parts.append(f"extra: {quote(member['extra'])}")
            lines.append("  - {" + ", ".join(parts) + "}")
    return "\n".join(lines) + "\n"


def _group_sort_key(group: dict) -> tuple:
    return (KINDS.index(group["kind"]) if group["kind"] in KINDS else 9, CLASSES.index(group["class"])
            if group["class"] in CLASSES else 9, 0 if group["name"] else 1, group["name"])


def build_groups(inventory: Inventory, existing: dict | None, minimum: int) -> tuple[list[dict], dict[int, list[str]]]:
    """保留已有决定（去掉已删包的成员），新条目先试着并进已有组，其余预分组、给建议。返回（组, 新加入的成员说明）。"""
    kept: list[dict] = []
    covered: set[tuple[str, str, str]] = set()
    for group in (existing or {}).get("groups", []):
        members = [member for member in group["members"]
                   if group["kind"] in KINDS and inventory.get(member["pack"], group["kind"], member["entry"])
                   and (member["pack"], group["kind"], member["entry"]) not in covered]
        if not members:
            continue
        packs = len({member["pack"] for member in members})
        group = {**group, "members": members}
        # 门槛变了的机械调整：够不上门槛的实类、虚类改成没抽出（was 记下原来的归类）；因为门槛没抽出、现在够了的，
        # 原来有归类的恢复（定义、模板都还在），没有的放回去重新建议
        if group["class"] in ("real", "virtual") and packs < minimum:
            group.update({"class": "none", "note": f"不到 {minimum} 个包", "was": group["class"]})
        elif group["class"] == "none" and group.get("was") in ("real", "virtual"):
            if packs >= minimum and _BELOW_RE.match(group["note"]):
                group.update({"class": group["was"], "note": "", "was": ""})
        elif group["class"] == "none" and _AUTO_NOTE_RE.match(group["note"]):
            # 脚本自己判的「没抽出」（单个包独有、步骤不一致、不够门槛……）每次放回去重新判：新上传的包可能和它们一样
            continue
        covered |= {(member["pack"], group["kind"], member["entry"]) for member in members}
        kept.append(group)
    joined: dict[int, list[str]] = {}
    groups = list(kept)

    clashes: dict[tuple[str, str, str], str] = {}

    def same_logic(entry: dict, other: dict, group: dict, signatures: Signatures) -> bool:
        """并进已有的操作、规则组要逻辑一致（实类比步骤与前置条件或约束原文，规则模板比去掉参数后的原文）。"""
        if entry["kind"] == "operation":
            return (signatures.text(entry) == signatures.text(other)
                    and signatures.trigger(entry) == signatures.trigger(other))
        if group["class"] == "virtual":
            return signatures.params(entry) == signatures.params(other)
        return signatures.text(entry) == signatures.text(other)

    def attach(entry: dict, signatures: Signatures) -> bool:
        candidates = []
        for index, group in enumerate(kept):
            # 只并进实类、虚类：没抽出的组是已经判过「不一样」的，新条目单独再判
            if (group["kind"] != entry["kind"] or group["class"] == "none"
                    or any(member["pack"] == entry["pack"] for member in group["members"])):
                continue
            others = [inventory.get(member["pack"], group["kind"], member["entry"]) for member in group["members"]]
            others = [other for other in others if other]
            if entry["kind"] == "object":
                if any(entries_match(entry, other, signatures) for other in others):
                    candidates.append(index)
            elif any(same_logic(entry, other, group, signatures) for other in others):
                candidates.append(index)
            elif any(norm_name(entry["name"]) == norm_name(other["name"]) for other in others):
                clashes[(entry["pack"], entry["kind"], entry["name"])] = group["name"]
        if len(candidates) != 1:
            return False
        kept[candidates[0]]["members"].append({"pack": entry["pack"], "entry": entry["name"], "values": {}, "extra": ""})
        joined.setdefault(candidates[0], []).append(f"{inventory.pack_name(entry['pack'])}/{entry['name']}")
        return True

    object_group: dict[tuple[str, str], int] = {}

    def register_objects() -> None:
        object_group.clear()
        for index, group in enumerate(groups):
            if group["kind"] == "object":
                for member in group["members"]:
                    object_group[(member["pack"], member["entry"])] = index

    def rename(entry: dict, text: str) -> str:
        """把正文里的 [[本包对象]] 换成它所在实类组的名字。"""
        def repl(match):
            name = match.group(1).split("|", 1)[0].strip()
            index = object_group.get((entry["pack"], name))
            target = groups[index] if index is not None else None
            if target and target["class"] == "real" and target["name"]:
                return f"[[{target['name']}]]"
            return match.group(0)
        return _LINK_RE.sub(repl, text or "")

    for kind in KINDS:
        register_objects()
        signatures = Signatures(object_group)
        fresh = [entry for entry in inventory.entries
                 if entry["kind"] == kind and (entry["pack"], kind, entry["name"]) not in covered]
        fresh = [entry for entry in fresh if not attach(entry, signatures)]
        union = _Union(len(fresh))
        for i, left in enumerate(fresh):
            for j in range(i + 1, len(fresh)):
                right = fresh[j]
                if left["pack"] != right["pack"] and entries_match(left, right, signatures):
                    union.join(i, j)
        for component in union.components():
            members = [fresh[index] for index in sorted(component)]
            if kind == "object":
                groups.append(decide_objects(members, minimum))
            else:
                for group in decide_flows(kind, members, minimum, signatures, rename):
                    group = _downgrade_unresolved(group, inventory, groups, object_group)
                    clash = next((clashes[(m["pack"], kind, m["entry"])] for m in group["members"]
                                  if (m["pack"], kind, m["entry"]) in clashes), None)
                    if group["class"] == "none" and not group["note"] and clash:
                        label = "步骤或前置条件" if kind == "operation" else "逻辑"
                        group["note"] = f"{label}和「{clash}」不一致"
                    groups.append(group)
    order = sorted(range(len(groups)), key=lambda index: _group_sort_key(groups[index]))
    remap = {old: new for new, old in enumerate(order)}
    return [groups[index] for index in order], {remap[index]: names for index, names in joined.items()}


def _downgrade_unresolved(group: dict, inventory: Inventory, groups: list[dict],
                          object_group: dict[tuple[str, str], int]) -> dict:
    """建议成实类的操作、规则引用了不是实类的对象时，改成没抽出（实类之间的链接要指得到）。"""
    if group["class"] != "real":
        return group
    for member in group["members"]:
        entry = inventory.get(member["pack"], group["kind"], member["entry"])
        for name in entry["links"] if entry else []:
            index = object_group.get((member["pack"], name))
            if index is not None and groups[index]["class"] != "real":
                return {**group, "class": "none", "note": f"引用的「{name}」不是实类"}
    return group


# ---------------------------------------------------------------------------
# 实类草稿
# ---------------------------------------------------------------------------


class Drafts:
    """按决定给还没有 ontology/ 条目的实类写草稿：对象取交集属性、各包叫法写进别名；操作、规则取代表成员的正文。"""

    def __init__(self, inventory: Inventory, groups: list[dict]):
        self.inventory = inventory
        self.groups = groups
        self.by_member = {(group["kind"], member["pack"], member["entry"]): group
                          for group in groups for member in group["members"]}

    def _entries(self, group: dict) -> list[dict]:
        return [self.inventory.get(member["pack"], group["kind"], member["entry"]) for member in group["members"]]

    def rename(self, pack: str, text: str) -> str:
        def repl(match):
            name = match.group(1).split("|", 1)[0].strip()
            for kind in KINDS:
                group = self.by_member.get((kind, pack, name))
                if group and group["class"] == "real" and group["name"]:
                    return f"[[{group['name']}]]"
            return match.group(0)
        return _LINK_RE.sub(repl, text or "")

    def common(self, group: dict) -> list[list[dict]]:
        return common_attributes(self._entries(group), anchor_index(group))

    def _real_group_of(self, pack: str, name: str) -> dict | None:
        group = self.by_member.get(("object", pack, name))
        return group if group and group["class"] == "real" and group["name"] else None

    def _common_en(self, group: dict, pack: str, member_name: str, field: str) -> str:
        """某成员属性（英文名 field）在公共条目里的英文名；对不上返回空。"""
        index = next((i for i, m in enumerate(group["members"]) if m["pack"] == pack and m["entry"] == member_name), None)
        if index is None or not field:
            return ""
        anchor = anchor_index(group)
        for row in self.common(group):
            if norm_name(row[index]["en"]) == norm_name(field):
                return row[anchor]["en"]
        return ""

    def object(self, group: dict) -> str:
        entries = self._entries(group)
        anchor = anchor_index(group)
        main = entries[anchor]
        aliases = []
        for entry in entries:
            for alias in [entry["name"], *entry["aliases"]]:
                if alias != group["name"] and alias not in aliases:
                    aliases.append(alias)
        rows = []
        for row in self.common(group):
            attr = row[anchor]
            names = [other["cn"] for other in row if other["cn"] and other["cn"] != attr["cn"]]
            names += [alias for alias in attr["aliases"] if alias not in (attr["cn"], attr["en"])]
            alias_text = "、".join(dict.fromkeys(names)) or "无"
            cells = [attr["cn"], attr["en"], attr["desc"] or attr["cn"], "是" if attr["pk"] else "否",
                     attr["type"] or "字符串", alias_text, attr["rule"] or "无"]
            rows.append("| " + " | ".join(cell.replace("|", "／") for cell in cells) + " |")
        relations = []
        for direction, other, edge in self.inventory.edges_of(main["pack"], main["name"]):
            if direction != "out":
                continue
            target = self._real_group_of(main["pack"], other)
            if target is None or not self._edge_in_all(group, target):
                continue
            source_en = self._common_en(group, main["pack"], main["name"], edge["source_field"])
            target_en = self._common_en(target, main["pack"], other, edge["target_field"])
            join = f"（{source_en} → {target_en}）" if source_en and target_en else ""
            line = f"- {CURRENT_OBJECT} → {edge['name']}{join} → [[{target['name']}]]"
            if line not in relations:
                relations.append(line)
        parts = [
            "# 对象描述", self.rename(main["pack"], main["description"]) or "TODO: 待补充对象描述", "",
            "# 对象别名", "、".join(aliases) or "无", "",
            "# 英文名称", main["english"] or "TODO: 待补充英文名称", "",
            "# 数据来源", "无", "",
            "# 对象属性清单",
            "| 中文名称 | 英文名称 | 描述 | 主键 | 类型 | 属性别名 | 属性规则 |",
            "|--------|--------|-----|----|-----|--------|--------|",
            *rows,
        ]
        if relations:
            parts += ["", "# 关联对象", *relations]
        return "\n".join(parts) + "\n"

    def _edge_in_all(self, group: dict, target: dict) -> bool:
        """每个成员都有一条指向目标组成员的关系（方向从本组出发）。"""
        targets = {(member["pack"], member["entry"]) for member in target["members"]}
        for member in group["members"]:
            edges = self.inventory.edges_of(member["pack"], member["entry"])
            if not any(direction == "out" and (member["pack"], other) in targets for direction, other, _ in edges):
                return False
        return True

    def _section(self, entry: dict, name: str, default: str) -> str:
        return self.rename(entry["pack"], (entry["sections"].get(name) or "").strip()) or default

    def _sources(self, group: dict) -> str:
        names = [f"{self.inventory.pack_name(m['pack'])}/{m['entry']}" for m in group["members"]]
        return "公共本体：由 " + "、".join(names) + " 抽取"

    def rule(self, group: dict) -> str:
        entry = self._entries(group)[anchor_index(group)]
        parts = [
            ("规则描述", self._section(entry, "规则描述", "TODO: 待补充规则描述")),
            ("规则类型", self._section(entry, "规则类型", "校验约束")),
            ("适用对象", self._section(entry, "适用对象", "TODO: 待补充适用对象")),
            ("触发条件", self._section(entry, "触发条件", "始终")),
            ("约束表达", self.rename(entry["pack"], entry["expression"]) or "TODO: 待补充约束表达"),
            ("违反处置", self._section(entry, "违反处置", "拒绝执行")),
            ("来源", self._sources(group)),
            ("原始资料来源", "无（公共本体，从上传的知识包抽取）"),
            ("例外情况", self._section(entry, "例外情况", "无")),
        ]
        return "\n\n".join(f"# {title}\n{body}" for title, body in parts) + "\n"

    def operation(self, group: dict) -> str:
        entry = self._entries(group)[anchor_index(group)]
        order = ["业务描述", "与operation的关联", "与object的关联", "与object的语义关联", "触发条件", "输入", "输出", "逻辑描述"]
        parts = []
        for title in order:
            if title == "与object的语义关联" and title not in entry["sections"]:
                continue
            body = self._section(entry, title, "")
            if not body and title == "逻辑描述":
                body = "\n".join(f"{i}. {self.rename(entry['pack'], step)}" for i, step in enumerate(entry["steps"], 1))
            parts.append(f"# {title}\n{body or '无'}")
        return "\n\n".join(parts) + "\n"

    def render(self, group: dict) -> str:
        return {"object": self.object, "operation": self.operation, "rule": self.rule}[group["kind"]](group)


def ontology_path(group: dict) -> Path:
    return Path(ONTOLOGY_DIRS[group["kind"]]) / f"{safe_name(group['name'])}.md"


def write_drafts(inventory: Inventory, groups: list[dict], *, apply: bool) -> tuple[list[str], list[str]]:
    """（草稿路径, 写进 ontology/ 的路径）：只给缺条目的实类写；已有条目一律不动。"""
    drafts_dir = WORK / "drafts"
    if drafts_dir.exists():
        shutil.rmtree(drafts_dir)
    maker = Drafts(inventory, groups)
    drafted, applied = [], []
    for group in groups:
        if group["class"] != "real" or not group["name"] or group["kind"] not in KINDS:
            continue
        if any(inventory.get(m["pack"], group["kind"], m["entry"]) is None for m in group["members"]):
            continue
        target = ontology_path(group)
        if target.exists():
            continue
        draft = drafts_dir / group["kind"] / target.name
        draft.parent.mkdir(parents=True, exist_ok=True)
        draft.write_text(maker.render(group), encoding="utf-8")
        drafted.append(draft.as_posix())
        if apply:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(draft, target)
            applied.append(target.as_posix())
    return drafted, applied


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------


def parse_min(tokens: list[str], default: int | None = None) -> int | None:
    """门槛：`--min N`、`--min=N`、`min=N` 或单独一个数字。"""
    value = default
    rest = list(tokens)
    while rest:
        token = rest.pop(0)
        if token == "--min" and rest:
            token = rest.pop(0)
        token = re.sub(r"^(?:--)?min=", "", token)
        if re.fullmatch(r"\d{1,2}", token):
            value = int(token)
    return value


def _changed_packs(inventory: Inventory) -> list[str]:
    """和上次核对时比，新增、更新、删掉的包。"""
    status = _read_json(STATUS)
    before = {item.get("id"): item for item in status.get("packs") or [] if isinstance(item, dict)}
    if not before:
        return []
    out = []
    for pack in inventory.packs:
        old = before.pop(pack["id"], None)
        if old is None:
            out.append(f"新增 {pack['name']}")
        elif old.get("digest") != pack["digest"] or old.get("version") != pack["version"]:
            out.append(f"更新 {pack['name']}")
    out += [f"删除 {item.get('name') or key}" for key, item in before.items()]
    return out


def _inventory_json(inventory: Inventory) -> dict:
    skip = {"sections", "body"}
    return {"packs": [{**{k: v for k, v in pack.items() if k != "entries"},
                       "entries": [{k: v for k, v in entry.items() if k not in skip} for entry in pack["entries"]]}
                      for pack in inventory.packs]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="公共本体：输入知识包清单与抽取决定草稿")
    parser.add_argument("tokens", nargs="*", help="门槛：min=N、--min N 或数字 N（缺省沿用 merge.yaml，没有就是 2）")
    parser.add_argument("--min", dest="min_value", type=int)
    parser.add_argument("--apply", action="store_true", help="把缺条目的实类草稿写进 ontology/（不覆盖）")
    args = parser.parse_args(argv)
    inventory = load_inventory()
    if len(inventory.packs) < 2:
        print(f"输入知识包只有 {len(inventory.packs)} 个，至少要 2 个：请在「公共本体」页上传。")
        return 1
    existing, problems = load_merge()
    if problems:
        print("抽取决定有格式问题，先改好再跑：")
        for problem in problems[:10]:
            print(f"- {problem}")
        return 1
    minimum = args.min_value or parse_min(args.tokens, (existing or {}).get("min") or DEFAULT_MIN)
    if not 2 <= minimum <= MAX_MIN:
        print(f"门槛要在 2～{MAX_MIN} 之间，收到 {minimum}")
        return 1
    groups, joined = build_groups(inventory, existing, minimum)
    MERGE.parent.mkdir(parents=True, exist_ok=True)
    MERGE.write_text(dump_merge(minimum, groups, inventory, joined), encoding="utf-8")
    WORK.mkdir(parents=True, exist_ok=True)
    (WORK / "inventory.json").write_text(json.dumps(_inventory_json(inventory), ensure_ascii=False, indent=1),
                                         encoding="utf-8")
    drafted, applied = write_drafts(inventory, groups, apply=args.apply)

    counts = inventory.summary()
    print(f"门槛：至少 {minimum} 个包都有")
    print(f"输入 {len(inventory.packs)} 个包：对象 {counts['object']}、操作 {counts['operation']}、规则 {counts['rule']}"
          + ("（指标、脚本不在抽取范围）" if any(pack["metrics"] for pack in inventory.packs) else ""))
    for pack in inventory.packs[:10]:
        print(f"- {pack['id']}：{pack['name']} {pack['version']}".rstrip())
    changed = _changed_packs(inventory)
    if changed:
        print("上次核对之后：" + "、".join(changed[:10]))
    by_class = Counter((group["kind"], group["class"]) for group in groups)
    for kind in KINDS:
        print(f"{KIND_LABELS[kind]}：" + "、".join(f"{CLASS_LABELS[cls]} {by_class.get((kind, cls), 0)}" for cls in CLASSES))
    kept = len((existing or {}).get("groups", []))
    print(f"决定：{MERGE.as_posix()}（{len(groups)} 组，沿用已有 {kept} 组，新加入已有组 {sum(map(len, joined.values()))} 条）")
    if applied:
        print(f"已写进 ontology/ 的实类草稿 {len(applied)} 个：" + "、".join(Path(p).stem for p in applied[:15]))
    elif drafted:
        print(f"缺条目的实类草稿 {len(drafted)} 个在 {(WORK / 'drafts').as_posix()}/（加 --apply 写进 ontology/）")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except InputError as exc:
        print(str(exc))
        sys.exit(1)
