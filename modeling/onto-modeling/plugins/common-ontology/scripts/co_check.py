#!/usr/bin/env python3
"""公共本体（common-ontology）：按抽取决定生成页面读的产物，并核对。

    python3 .claude/scripts/co_check.py

核对：
1. 决定覆盖每个输入条目（对象、操作、规则），每个条目只在一个组里，成员都指得到上传区里的条目；
2. 归类合规：实类、虚类至少 min 个包；操作没有虚类；虚类对象有定义；规则模板有模板、参数，每个成员都有参数值；
3. 实类和 ontology/ 一致：每个实类在 ontology/<类型>/ 里有同名条目，上次是实类、这次不是的条目已删掉；
   实类对象主键以外至少一个属性，每个公共属性在每个成员里都对得上（各包叫法写进属性别名），关系每个成员都有；
   实类条目里的链接只指向实类；
4. 工作区里有 .claude/scripts/ontology_checker.py 时，它对实类条目报的错误；
5. 生成的产物符合本插件登记的格式（.claude/artifacts/common-ontology-*.yaml）。
生成（每次重写，多余的旧文件删掉）：ext/common-ontology/result/ 下 real/、glossary/、rule-templates/、mapping/ 与 status.json。
标准输出最多约 40 行；全部问题（最多 200 条）在 status.json。退出码：0 通过，1 没通过。只用标准库和 PyYAML。
"""

from __future__ import annotations

import json
import re
import subprocess  # nosec B404 - 只跑工作区里内核的 ontology_checker，参数列表不经 shell
import sys
import time
from collections import Counter
from pathlib import Path

import yaml

sys.dont_write_bytecode = True  # 同目录导入 co_inventory，不往装配目录里留 __pycache__
sys.path.insert(0, str(Path(__file__).resolve().parent))
import co_inventory as co  # noqa: E402

OUTPUT_DIRS = {"real": co.RESULT / "real", "term": co.RESULT / "glossary", "template": co.RESULT / "rule-templates",
               "mapping": co.RESULT / "mapping"}
CHECKER = Path(".claude") / "scripts" / "ontology_checker.py"
PRINT_PROBLEMS = 25
PRINT_WARNINGS = 5
STATUS_PROBLEMS = 200


class Report:
    def __init__(self):
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, text: str) -> None:
        if text not in self.errors:
            self.errors.append(text)

    def warn(self, text: str) -> None:
        if text not in self.warnings:
            self.warnings.append(text)


def _label(inventory: co.Inventory, pack: str, entry: str) -> str:
    return f"{inventory.pack_name(pack)}/{entry}"


# ---------------------------------------------------------------------------
# 决定本身
# ---------------------------------------------------------------------------


def check_decisions(merge: dict, inventory: co.Inventory, report: Report) -> dict[tuple, dict]:
    """覆盖与归类；返回（包, 类型, 条目）→ 所在组。"""
    minimum = merge["min"]
    seen: dict[tuple, dict] = {}
    names: dict[tuple, str] = {}
    for position, group in enumerate(merge["groups"], 1):
        title = group["name"] or f"第 {position} 组"
        kind, cls = group["kind"], group["class"]
        if kind not in co.KINDS:
            report.error(f"「{title}」的 kind 只能是 object、operation、rule")
            continue
        if cls not in co.CLASSES:
            report.error(f"「{title}」的 class 只能是 real、virtual、none")
            continue
        for member in group["members"]:
            key = (member["pack"], kind, member["entry"])
            if inventory.get(*key) is None:
                other = next((k for k in co.KINDS if inventory.get(member["pack"], k, member["entry"])), None)
                if member["pack"] not in inventory.pack_by_id:
                    report.error(f"「{title}」的成员 {member['pack']}/{member['entry']}：上传区里没有这个包（删掉了？）")
                elif other:
                    report.error(f"「{title}」的成员 {_label(inventory, *key[::2])} 是{co.KIND_LABELS[other]}，"
                                 f"不是{co.KIND_LABELS[kind]}")
                else:
                    report.error(f"「{title}」的成员 {_label(inventory, *key[::2])}：包里没有这个{co.KIND_LABELS[kind]}")
            elif key in seen:
                before = seen[key]["name"] or "没抽出的一组"
                report.error(f"{_label(inventory, *key[::2])} 同时在「{before}」和「{title}」里")
            else:
                seen[key] = group
        if cls == "none":
            continue
        if not group["name"]:
            report.error(f"第 {position} 组是{co.CLASS_LABELS[cls]}，要写 name")
            continue
        if co.safe_name(group["name"]) != group["name"]:
            report.error(f"「{group['name']}」的名称不能带 / \\ : * ? \" < > | 或首尾的点")
        packs = [member["pack"] for member in group["members"]]
        if len(set(packs)) != len(packs):
            report.error(f"「{title}」里同一个包有两个成员：拆开，或把多出的那个改成 none")
        if len(set(packs)) < minimum:
            report.error(f"「{title}」只在 {len(set(packs))} 个包里，门槛是 {minimum}：改成 none")
        # 实类不分类型都不能重名（[[名称]] 跨目录解析）；术语、规则模板各自不能重名
        bucket = ("real", "", group["name"]) if cls == "real" else (cls, kind, group["name"])
        if bucket in names:
            report.error(f"名称「{group['name']}」重复：{names[bucket]}和{co.KIND_LABELS[kind]}都叫它")
        names[bucket] = co.KIND_LABELS[kind]
        if cls == "virtual":
            _check_virtual(group, inventory, report)
    for entry in inventory.entries:
        if (entry["pack"], entry["kind"], entry["name"]) not in seen:
            report.error(f"没有映射：{_label(inventory, entry['pack'], entry['name'])}（{co.KIND_LABELS[entry['kind']]}）"
                         "不在任何组里，没抽出的也要列成 class: none")
    return seen


def _check_virtual(group: dict, inventory: co.Inventory, report: Report) -> None:
    name = group["name"]
    if group["kind"] == "operation":
        report.error(f"「{name}」：操作只有实类，步骤不一致的改成 none")
    elif group["kind"] == "object" and not group["definition"]:
        report.error(f"术语「{name}」要写 definition")
    elif group["kind"] == "rule":
        if not group["template"] or not group["params"]:
            report.error(f"规则模板「{name}」要写 template 和 params")
            return
        for param in group["params"]:
            if "{" + param["name"] + "}" not in group["template"]:
                report.error(f"规则模板「{name}」的参数「{param['name']}」没在 template 里出现（写成 {{{param['name']}}}）")
        for member in group["members"]:
            missing = [param["name"] for param in group["params"] if not member["values"].get(param["name"])]
            if missing:
                report.error(f"规则模板「{name}」的实例 {_label(inventory, member['pack'], member['entry'])} "
                             f"缺参数值：{'、'.join(missing)}")


# ---------------------------------------------------------------------------
# 实类与 ontology/
# ---------------------------------------------------------------------------


def _ontology_entries() -> dict[tuple[str, str], Path]:
    found = {}
    for kind, directory in co.ONTOLOGY_DIRS.items():
        base = Path(directory)
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.md") if kind == "object" else base.glob("*.md")):
            if path.name.lower() != "index.md" and path.is_file() and not path.is_symlink():
                found[(kind, path.stem)] = path
    return found


class RealCheck:
    """实类条目和成员逐个对：属性、关系、链接；顺带算出映射里各成员「多出的部分」与清单里的概要。"""

    def __init__(self, groups: list[dict], inventory: co.Inventory, membership: dict[tuple, dict], report: Report):
        self.inventory = inventory
        self.membership = membership
        self.report = report
        self.real = {group["name"]: group for group in groups if group["class"] == "real" and group["name"]
                     and group["kind"] in co.KINDS}
        self.parsed: dict[str, dict] = {}
        self.summary: dict[str, dict] = {}
        self.extras: dict[tuple, list[str]] = {}
        self.edges: list[dict] = []

    def run(self) -> None:
        on_disk = _ontology_entries()
        for name, group in self.real.items():
            path = on_disk.get((group["kind"], name))
            if path is None:
                self.report.error(f"实类「{name}」在 {co.ONTOLOGY_DIRS[group['kind']]}/ 里没有条目："
                                  "跑 co_inventory.py --apply 写草稿，或按模板写")
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            self.parsed[name] = co.parse_entry(group["kind"], "ontology", name, text, path.as_posix())
        for name, entry in self.parsed.items():
            if entry["kind"] == "object":
                self.edges += entry["edges"]
        for name, entry in self.parsed.items():
            group = self.real[name]
            if entry["kind"] == "object":
                self._object(name, group, entry)
            else:
                steps = len(entry["steps"]) if entry["kind"] == "operation" else 0
                self.summary[name] = {"attributes": 0, "relations": 0, "steps": steps,
                                      "summary": f"步骤 {steps}" if steps else "—"}
            self._links(name, entry)

    def _members(self, group: dict) -> list[tuple[dict, dict]]:
        out = []
        for member in group["members"]:
            entry = self.inventory.get(member["pack"], group["kind"], member["entry"])
            if entry is not None:
                out.append((member, entry))
        return out

    def _common_edges(self, name: str) -> list[tuple[str, str, dict]]:
        seen, out = set(), []
        for edge in self.edges:
            key = (edge["source"], co.norm_name(edge["name"]), edge["target"])
            if key in seen or name not in (edge["source"], edge["target"]):
                continue
            seen.add(key)
            out.append(("out", edge["target"], edge) if edge["source"] == name else ("in", edge["source"], edge))
        return out

    def _group_name(self, pack: str, entry: str) -> str | None:
        group = self.membership.get((pack, "object", entry))
        return group["name"] if group and group["class"] == "real" else None

    def _object(self, name: str, group: dict, entry: dict) -> None:
        common = entry["attributes"]
        if not any(not attr["pk"] for attr in common):
            self.report.error(f"实类对象「{name}」主键以外至少要有一个属性，否则改成虚类（进术语表）")
        holder = {"attributes": common}
        touching = self._common_edges(name)
        for _direction, other, _edge in touching:
            if other not in self.real or self.real[other]["kind"] != "object":
                self.report.error(f"实类「{name}」的关系指向「{other}」，它不是实类对象")
        # 一条关系只在出发的那一端核对：指进来的关系属于另一端的组，那一组的包未必有本组的成员
        common_edges = [item for item in touching if item[0] == "out"]
        for member, source in self._members(group):
            label = _label(self.inventory, member["pack"], member["entry"])
            used: set[int] = set()
            for attr in common:
                index = co.find_attribute(attr, holder, source, used)
                if index is None:
                    self.report.error(f"实类「{name}」的属性「{attr['cn'] or attr['en']}」在 {label} 里对不上："
                                      "属性别名里写上它在这个包里的叫法，或者从公共条目里去掉")
                else:
                    used.add(index)
            extra = []
            rest = co.unmatched_attributes(common, source)
            if rest:
                extra.append("属性：" + "、".join(attr["cn"] or attr["en"] for attr in rest))
            member_edges = [item for item in self.inventory.edges_of(member["pack"], member["entry"]) if item[0] == "out"]
            matched = set()
            for direction, other, edge in common_edges:
                hit = next((i for i, (d, o, _e) in enumerate(member_edges)
                            if i not in matched and d == direction and self._group_name(member["pack"], o) == other), None)
                if hit is None:
                    arrow = f"→{other}" if direction == "out" else f"←{other}"
                    self.report.error(f"实类「{name}」的关系「{edge['name']}{arrow}」在 {label} 里没有")
                else:
                    matched.add(hit)
            rest_edges = [f"{edge['name']}{'→' if d == 'out' else '←'}{o}"
                          for i, (d, o, edge) in enumerate(member_edges) if i not in matched]
            if rest_edges:
                extra.append("关系：" + "、".join(rest_edges))
            self.extras[(member["pack"], "object", member["entry"])] = extra
        relations = len(touching)
        self.summary[name] = {"attributes": len(common), "relations": relations, "steps": 0,
                              "summary": f"属性 {len(common)}" + (f" · 关系 {relations}" if relations else "")}

    def _links(self, name: str, entry: dict) -> None:
        targets = co.links(entry["body"])
        if entry["kind"] == "object":
            targets = [edge["target"] if edge["source"] == name else edge["source"] for edge in entry["edges"]]
        for target in targets:
            if target != name and target not in self.real:
                self.report.error(f"实类「{name}」引用的「{target}」不是实类：换成实类，或把这个条目改成 none")


def check_extra_entries(groups: list[dict], previous: set[tuple[str, str]], report: Report) -> None:
    real = {(group["kind"], group["name"]) for group in groups if group["class"] == "real"}
    for (kind, name), path in _ontology_entries().items():
        if (kind, name) in real:
            continue
        if (kind, name) in previous:
            report.error(f"{path.as_posix()} 上次是实类、这次不是了：删掉它")
        else:
            report.warn(f"{path.as_posix()} 不在实类清单里，发布时会一起发出去")


def run_checker(stems: list[str], report: Report) -> None:
    if not stems or not CHECKER.is_file() or not Path("ontology").is_dir():
        return
    try:
        proc = subprocess.run(  # nosec B603 NOSONAR(S4721) 固定解释器与内核脚本，参数列表不经 shell
            [sys.executable, str(CHECKER), "ontology", "--json", "--files", *stems],
            capture_output=True, text=True, timeout=180, check=False)
        issues = json.loads(proc.stdout or "[]")
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        report.warn(f"ontology_checker 没跑成：{type(exc).__name__}")
        return
    wanted = set(stems)
    for issue in issues if isinstance(issues, list) else []:
        stem = Path(str(issue.get("file") or "")).stem
        if issue.get("severity") == "错误" and stem in wanted:
            report.error(f"ontology_checker：{issue.get('file')}：{issue.get('category')}：{co.clip(issue.get('message'), 120)}")


# ---------------------------------------------------------------------------
# 生成
# ---------------------------------------------------------------------------


def _front(fields: dict) -> str:
    return "---\n" + yaml.safe_dump(fields, allow_unicode=True, sort_keys=False, width=1000) + "---\n\n"


def _row(cells) -> str:
    return "| " + " | ".join(str(cell or "").replace("|", "／").replace("\n", " ") for cell in cells) + " |"


def _table(head: list[str], rows: list[list]) -> str:
    return "\n".join([_row(head), _row(["---"] * len(head)), *(_row(row) for row in rows)])


def _values_text(values: dict) -> str:
    return "，".join(f"{key}={value}" for key, value in values.items())


class Renderer:
    def __init__(self, merge: dict, inventory: co.Inventory, real: RealCheck):
        self.merge = merge
        self.inventory = inventory
        self.real = real

    def _sources(self, group: dict) -> list[str]:
        return list(dict.fromkeys(self.inventory.pack_name(member["pack"]) for member in group["members"]))

    def _extra(self, group: dict, member: dict) -> str:
        parts = list(self.real.extras.get((member["pack"], group["kind"], member["entry"]), []))
        if group["class"] == "virtual" and group["kind"] == "rule" and member["values"]:
            parts.append("参数：" + _values_text(member["values"]))
        if member["extra"]:
            parts.append(member["extra"])
        return "；".join(parts)

    def files(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for group in self.merge["groups"]:
            if group["kind"] not in co.KINDS or group["class"] not in co.CLASSES or not group["name"]:
                continue
            name = co.safe_name(group["name"])
            if group["class"] == "real":
                out[(OUTPUT_DIRS["real"] / f"{name}.md").as_posix()] = self.real_file(group)
            elif group["class"] == "virtual" and group["kind"] == "object":
                out[(OUTPUT_DIRS["term"] / f"{name}.md").as_posix()] = self.term_file(group)
            elif group["class"] == "virtual" and group["kind"] == "rule":
                out[(OUTPUT_DIRS["template"] / f"{name}.md").as_posix()] = self.template_file(group)
        for pack in self.inventory.packs:
            out[(OUTPUT_DIRS["mapping"] / f"{pack['id']}.md").as_posix()] = self.mapping_file(pack)
        return out

    def real_file(self, group: dict) -> str:
        info = self.real.summary.get(group["name"], {"attributes": 0, "relations": 0, "steps": 0, "summary": "—"})
        members = [{"pack": m["pack"], "pack_name": self.inventory.pack_name(m["pack"]), "entry": m["entry"],
                    "extra": self._extra(group, m)} for m in group["members"]]
        fields = {
            "title": group["name"], "kind": group["kind"], "entry": group["name"],
            "path": co.ontology_path(group).as_posix(), "summary": info["summary"],
            "attributes": info["attributes"], "relations": info["relations"], "steps": info["steps"],
            "sources": self._sources(group), "source_count": len(self._sources(group)), "members": members,
        }
        body = _table(["知识包", "条目", "多出的部分"], [[m["pack_name"], m["entry"], m["extra"]] for m in members])
        return _front(fields) + f"# {group['name']}\n\n{body}\n"

    def term_file(self, group: dict) -> str:
        names = [{"pack": m["pack"], "pack_name": self.inventory.pack_name(m["pack"]), "name": m["entry"]}
                 for m in group["members"]]
        fields = {"title": group["name"], "definition": group["definition"], "names": names,
                  "sources": self._sources(group), "source_count": len(self._sources(group))}
        body = _table(["知识包", "叫法"], [[item["pack_name"], item["name"]] for item in names])
        return _front(fields) + f"# {group['name']}\n\n{group['definition']}\n\n{body}\n"

    def template_file(self, group: dict) -> str:
        instances = [{"pack": m["pack"], "pack_name": self.inventory.pack_name(m["pack"]), "rule": m["entry"],
                      "values": dict(m["values"]), "values_text": _values_text(m["values"])} for m in group["members"]]
        fields = {"title": group["name"], "template": group["template"], "params": group["params"],
                  "instances": instances, "sources": self._sources(group), "source_count": len(self._sources(group))}
        body = "\n\n".join([
            f"# {group['name']}", "## 模板", group["template"],
            "## 参数", _table(["参数", "含义"], [[p["name"], p["meaning"]] for p in group["params"]]),
            "## 实例", _table(["知识包", "规则", "参数值"], [[i["pack_name"], i["rule"], i["values_text"]] for i in instances]),
        ])
        return _front(fields) + body + "\n"

    def mapping_file(self, pack: dict) -> str:
        rows = []
        for group in self.merge["groups"]:
            if group["kind"] not in co.KINDS or group["class"] not in co.CLASSES:
                continue
            for member in group["members"]:
                if member["pack"] != pack["id"] or not self.inventory.get(member["pack"], group["kind"], member["entry"]):
                    continue
                note = ""
                if group["class"] == "none":
                    note = group["note"] or ("只有这个包有" if len(group["members"]) == 1 else "")
                rows.append({"kind": group["kind"], "entry": member["entry"],
                             "common": group["name"] if group["class"] != "none" else "",
                             "class": group["class"], "extra": self._extra(group, member), "note": note})
        rows.sort(key=lambda row: (co.KINDS.index(row["kind"]), co.CLASSES.index(row["class"]), row["entry"]))
        counts = Counter(row["class"] for row in rows)
        fields = {"title": pack["name"], "pack": pack["id"], "package_id": pack["package_id"], "version": pack["version"],
                  "real": counts.get("real", 0), "virtual": counts.get("virtual", 0), "none": counts.get("none", 0),
                  "rows": rows}
        body = _table(["类型", "条目", "公共条目", "归类", "多出的部分"],
                      [[co.KIND_LABELS[r["kind"]], r["entry"], r["common"] or "—", co.CLASS_LABELS[r["class"]],
                        r["extra"] or r["note"]] for r in rows])
        return _front(fields) + f"# {pack['name']}\n\n{body}\n"


def write_outputs(files: dict[str, str]) -> None:
    for directory in OUTPUT_DIRS.values():
        if directory.is_dir():
            for path in directory.glob("*.md"):
                if path.as_posix() not in files:
                    path.unlink()
    for rel, text in files.items():
        path = Path(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.is_file() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# 产物格式（按本插件登记的产物类型）
# ---------------------------------------------------------------------------


def registrations() -> list[dict]:
    base = Path(__file__).resolve().parent.parent / "artifacts"
    out = []
    for path in sorted(base.glob(f"{co.PACK}-*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(data, dict) and data.get("path"):
            out.append(data)
    return out


def _matches(pattern: str, rel: str) -> bool:
    regex = "^" + re.escape(pattern.replace("{key}", "*")).replace(r"\*", "[^/]+") + "$"
    return bool(re.match(regex, rel))


def check_formats(files: dict[str, str], report: Report) -> None:
    regs = registrations()
    if not regs:
        report.warn("找不到本插件的产物登记，产物格式没核对")
        return
    for rel, text in files.items():
        reg = next((item for item in regs if _matches(str(item["path"]), rel)), None)
        if reg is None:
            report.error(f"{rel} 不属于任何登记的产物类型")
            continue
        if reg.get("format") == "json":
            try:
                fields = json.loads(text)
            except ValueError:
                report.error(f"{rel} 不是合法的 JSON")
                continue
        else:
            fields, _body = co.split_frontmatter(text)
        for spec in ((reg.get("schema") or {}).get("fields") or []):
            value = fields.get(spec["name"]) if isinstance(fields, dict) else None
            kind = spec.get("type") or "text"
            if value in (None, "", []):
                if spec.get("required"):
                    report.error(f"{rel} 缺字段 {spec['name']}")
                continue
            if kind == "enum" and str(value) not in {str(item.get("value")) for item in spec.get("enum") or []}:
                report.error(f"{rel} 的 {spec['name']} 取值 {value!r} 不在登记的范围里")
            elif kind == "number" and (isinstance(value, bool) or not isinstance(value, (int, float))):
                report.error(f"{rel} 的 {spec['name']} 要是数字")
            elif kind == "list" and not isinstance(value, list):
                report.error(f"{rel} 的 {spec['name']} 要是列表")


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------


def _previous_owned() -> set[tuple[str, str]]:
    """以前当过实类、可能还留在 ontology/ 里的条目（status.json 的 owned）。"""
    try:
        status = json.loads(co.STATUS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    items = (status.get("owned") or []) + (status.get("real") or [])
    return {(str(item.get("kind")), str(item.get("name"))) for item in items if isinstance(item, dict)}


def _owned(groups: list[dict], previous: set[tuple[str, str]]) -> list[dict]:
    """本插件建过、还在 ontology/ 里的条目：这次的实类，加上以前的实类里文件还没删的（删掉之前一直报错）。"""
    current = {(group["kind"], group["name"]) for group in groups
               if group["class"] == "real" and group["name"] and group["kind"] in co.KINDS}
    on_disk = set(_ontology_entries())
    owned = sorted(current | {item for item in previous if item in on_disk})
    return [{"kind": kind, "name": name} for kind, name in owned]


def _status(merge: dict, inventory: co.Inventory, report: Report, previous: set[tuple[str, str]]) -> dict:
    groups = merge["groups"]
    real = [group for group in groups if group["class"] == "real" and group["name"] and group["kind"] in co.KINDS]
    named = lambda cls, kind: sum(1 for g in groups if g["class"] == cls and g["kind"] == kind and g["name"])  # noqa: E731
    return {
        "title": "核对结果", "state": "failed" if report.errors else "passed", "min": merge["min"],
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "packs": [{"id": p["id"], "package_id": p["package_id"], "name": p["name"], "version": p["version"],
                   "digest": p["digest"]} for p in inventory.packs],
        "real": [{"kind": group["kind"], "name": group["name"]} for group in real],
        "owned": _owned(groups, previous),
        "real_count": len(real),
        **{f"real_{kind}_count": sum(1 for group in real if group["kind"] == kind) for kind in co.KINDS},
        "term_count": named("virtual", "object"), "template_count": named("virtual", "rule"),
        "none_count": sum(len(g["members"]) for g in groups if g["class"] == "none"),
        "entry_count": len(inventory.entries),
        "errors": len(report.errors), "warnings": len(report.warnings),
        "problems": (report.errors + report.warnings)[:STATUS_PROBLEMS],
    }


def main() -> int:
    inventory = co.load_inventory()
    merge, problems = co.load_merge()
    if merge is None:
        print("\n".join(problems) or f"还没有抽取决定 {co.MERGE.as_posix()}：先跑 co_inventory.py")
        return 1
    report = Report()
    for problem in problems:
        report.error(problem)
    previous = _previous_owned()
    membership = check_decisions(merge, inventory, report)
    real = RealCheck(merge["groups"], inventory, membership, report)
    real.run()
    check_extra_entries(merge["groups"], previous, report)
    run_checker(sorted(real.parsed), report)
    files = Renderer(merge, inventory, real).files()
    status = _status(merge, inventory, report, previous)
    status_text = json.dumps(status, ensure_ascii=False, indent=1) + "\n"
    check_formats({**files, co.STATUS.as_posix(): status_text}, report)
    if report.errors and status["state"] == "passed":
        status = _status(merge, inventory, report, previous)
        status_text = json.dumps(status, ensure_ascii=False, indent=1) + "\n"
    write_outputs(files)
    co.STATUS.write_text(status_text, encoding="utf-8")

    verdict = f"没通过（{len(report.errors)} 个问题）" if report.errors else "通过"
    print(f"公共本体核对：{verdict}")
    print(f"门槛：至少 {merge['min']} 个包都有；输入 {len(inventory.packs)} 个包、{status['entry_count']} 个条目")
    print(f"实类 {status['real_count']}（对象 {status['real_object_count']}、操作 {status['real_operation_count']}、"
          f"规则 {status['real_rule_count']}） · 术语 {status['term_count']} · 规则模板 {status['template_count']}"
          f" · 没抽出 {status['none_count']}")
    if report.errors:
        print("问题：")
        for line in report.errors[:PRINT_PROBLEMS]:
            print(f"- {line}")
        if len(report.errors) > PRINT_PROBLEMS:
            print(f"（还有 {len(report.errors) - PRINT_PROBLEMS} 条，全部在 {co.STATUS.as_posix()}）")
    if report.warnings:
        print("提醒：")
        for line in report.warnings[:PRINT_WARNINGS]:
            print(f"- {line}")
        if len(report.warnings) > PRINT_WARNINGS:
            print(f"（还有 {len(report.warnings) - PRINT_WARNINGS} 条）")
    print(f"结果：{co.RESULT.as_posix()}/")
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
