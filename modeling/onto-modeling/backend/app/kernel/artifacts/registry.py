"""插件产物登记（详设 5.9.4；方案 C13）：包在清单 `contributes.artifacts`、`views` 里登记的产物类型。

登记文件在包仓库里不可修改的版本目录里，内核只读 YAML，不跑包的代码。一个类型：

```yaml
type: trace.scenario              # 带包名前缀，不能和五元组重名
title: 场景
path: ext/trace/scenarios/*.md    # glob（也可写 {key}）；只能在 ext/<包名>/ 下（官方包已有产物见 LEGACY_DIRS）
format: md-frontmatter            # 见 FORMATS
key: filename                     # filename = 文件名去扩展名；也可以是字段名
schema: schemas/scenario.v1.yaml  # 字段定义（带版本号），或直接内联 {version, fields}
sections: [输入, 期望, 推演过程]    # Markdown 正文按标题切段
refs: [{field: operation, kind: operation}]
status: {field: result, map: {pass: 通过, fail: 失败}}
index: ext/trace/index/scenarios.jsonl   # 可选：包脚本生成的标准索引（内置解析器算不出来的字段）
derive: {rows: table_rows}        # 可选：内核从文件算出的字段（stem、h1、table_rows、heading；
                                  #   match:<字段>:<正则>=<值>|<否则> 按另一个字段取值）
defaults: {result: not_run}       # 可选：解析与并入索引之后仍为空的字段取默认值
readonly: managed                 # 可选：只读产物的原因（求解契约在求解页编辑、知识条目走知识接口）
```

视图声明（`views/*.yaml`）按 `artifact:` 挂到类型上：`list`（列、筛选、分组）、`detail`（布局、侧栏）、`card`。

- `card`：对话里的产物卡按它画（ADR-0055）：`title`、`subtitle` 取哪个字段，`chips`、`fields` 各最多 4 个字段；
  `component` 选平台的专用卡片组件（如 `flowchart`），平台不认识时按通用卡画。
- `detail`：双击打开什么：通用详情（`layout`），平台专用组件（`component`），或包自带编辑器（`editor`，
  引用清单 `contributes.editors` 的 id）。编辑器解析成 `{id, title, pack, version, mode, ui_api, entry}`：
  官方包（入库来源 builtin）`mode=bundled`，随平台编译；上传包 `mode=iframe`，只在不同源的 iframe 里跑。
"""

from __future__ import annotations

import copy
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ...services.packs import manifest as mf

_log = logging.getLogger(__name__)

FORMATS = ("md-frontmatter", "yaml", "json", "jsonl", "csv", "drawio", "md-report", "md-sections")
FIELD_TYPES = ("text", "number", "enum", "date", "ref", "anchor", "markdown", "code", "table", "list")
ENTRY_KINDS = ("object", "operation", "rule", "metric", "script")
DERIVATIONS = ("stem", "h1", "h1_suffix", "table_rows", "heading", "file_mtime", "last_source_label", "last_author")
# 官方包拆包前已有的产物目录（详设 5.9.4「旧目录保留只读别名，做法同 ADR 0004」）：拆包这一步
# 不搬存量产物，官方包可以把它们登记在原目录；新包和租户包只能用 ext/<包名>/
LEGACY_DIRS = ("ontology/testcase", "ontology/data-mock", "ontology/diagrams", "ontology/knowledge",
               "ontology/solver", "reports")
_TONES = ("teal", "red", "gray", "dash", "blue", "indigo", "amber", "green", "line")
_COMPONENT_RE = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
CARD_ITEMS_MAX = 4


class RegistrationError(ValueError):
    pass


@dataclass
class ArtifactType:
    type: str
    title: str
    pack: str
    pack_version: str
    format: str
    path: str                       # 规范成 glob（{key} → *）
    key: str
    schema_version: str
    fields: list[dict]
    sections: list[str]
    refs: list[dict]
    status: dict | None
    view: dict
    view_declared: bool
    studio: bool
    index: str | None
    derive: dict
    readonly: str | None
    section_labels: dict
    field_labels: dict
    table: bool
    extras: dict
    defaults: dict
    directory: Path | None = None
    raw: dict = field(default_factory=dict)
    heading: str = ""               # md-sections 新建文件的一级标题，{key} 换成文件键；空时就是文件键

    @property
    def path_template(self) -> str:
        return self.path.replace("*", "{key}", 1)

    def field(self, name: str) -> dict | None:
        return next((item for item in self.fields if item["name"] == name), None)

    def key_from_path(self, rel: str) -> str | None:
        """工作区相对路径落在本类型里时返回 * 匹配到的那段（文件键），否则 None；* 不跨目录。"""
        prefix, _, suffix = self.path.partition("*")
        if not (rel.startswith(prefix) and rel.endswith(suffix)) or len(rel) <= len(prefix) + len(suffix):
            return None
        middle = rel[len(prefix):len(rel) - len(suffix)]
        # 目录索引（index.md、知识库的 INDEX.md）不是产物
        if suffix.lower() == ".md" and middle.lower() == "index":
            return None
        return middle if "/" not in middle and not middle.startswith(".") else None

    def glob_root(self) -> str:
        """* 之前的目录部分（列文件从这里开始）。"""
        prefix = self.path.partition("*")[0]
        return prefix.rsplit("/", 1)[0] if "/" in prefix else ""

    def path_for(self, key: str) -> str:
        return self.path.replace("*", key, 1)

    def public(self) -> dict:
        """契约 7.11 ArtifactType。"""
        return {
            "type": self.type, "title": self.title, "pack": self.pack, "pack_version": self.pack_version,
            "format": self.format, "path": self.path_template, "key": self.key,
            "schema": {"version": self.schema_version, "fields": [dict(item) for item in self.fields]},
            "sections": list(self.sections),
            "refs": [dict(item) for item in self.refs],
            "status": dict(self.status) if self.status else None,
            "view": self.view,
            "studio": self.studio,
            "readonly": self.readonly,
        }


# 包里的登记文件很少变，但左栏、待处理、流程每次请求都要读：按 (路径, 修改时间, 大小) 缓存解析结果，
# 文件一改就失效；返回深拷贝，调用方改动不会污染缓存
_YAML_CACHE: dict[tuple[str, int, int], dict] = {}
_YAML_CACHE_MAX = 512
_YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def _safe_yaml(text: str):
    loader = _YAML_LOADER(text)
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def _load_yaml(path: Path) -> dict:
    try:
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
        data = _YAML_CACHE.get(key)
        if data is None:
            data = _safe_yaml(path.read_text(encoding="utf-8")) or {}
            if len(_YAML_CACHE) >= _YAML_CACHE_MAX:
                _YAML_CACHE.clear()
            _YAML_CACHE[key] = data
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise RegistrationError(f"{path.name} 无法解析：{exc}") from exc
    if not isinstance(data, dict):
        raise RegistrationError(f"{path.name} 必须是映射")
    return copy.deepcopy(data)


def _enum(values, status_map: dict | None) -> list[dict]:
    out = []
    for item in values or []:
        if isinstance(item, dict) and "value" in item:
            entry = {"value": str(item["value"]), "label": str(item.get("label") or item["value"])}
            if item.get("tone") in _TONES:
                entry["tone"] = item["tone"]
        else:
            entry = {"value": str(item), "label": str((status_map or {}).get(str(item)) or item)}
        out.append(entry)
    return out


def _fields_from_json_schema(schema: dict, status_map: dict | None) -> list[dict]:
    required = set(schema.get("required") or [])
    out = []
    for name, spec in (schema.get("properties") or {}).items():
        spec = spec if isinstance(spec, dict) else {}
        kind = spec.get("x-type")
        if kind not in FIELD_TYPES:
            json_type = spec.get("type")
            kind = ("enum" if spec.get("enum") else "number" if json_type in ("number", "integer")
                    else "list" if json_type == "array" else "date" if spec.get("format") in ("date", "date-time") else "text")
        item = {"name": str(name), "type": kind, "label": str(spec.get("title") or spec.get("x-label") or name),
                "required": name in required}
        if kind == "enum":
            item["enum"] = _enum(spec.get("x-enum") or spec.get("enum"), status_map)
        if kind == "ref" and spec.get("x-kind") in ENTRY_KINDS:
            item["kind"] = spec["x-kind"]
        out.append(item)
    return out


def _normalize_fields(raw: list, status_map: dict | None) -> list[dict]:
    out = []
    seen = set()
    for item in raw or []:
        if not isinstance(item, dict) or not item.get("name"):
            raise RegistrationError("字段定义每项要有 name")
        name = str(item["name"])
        if name in seen:
            raise RegistrationError(f"字段 {name} 重复")
        seen.add(name)
        kind = str(item.get("type") or "text")
        if kind not in FIELD_TYPES:
            raise RegistrationError(f"字段 {name} 的类型 {kind} 不认识（只能是 {'、'.join(FIELD_TYPES)}）")
        entry = {"name": name, "type": kind, "label": str(item.get("label") or name), "required": bool(item.get("required"))}
        if kind == "enum":
            entry["enum"] = _enum(item.get("enum"), status_map)
        if kind == "ref" and item.get("kind") in ENTRY_KINDS:
            entry["kind"] = item["kind"]
        out.append(entry)
    return out


def _schema(spec, directory: Path | None, status_map: dict | None) -> tuple[str, list[dict]]:
    if spec is None:
        return "1", []
    if isinstance(spec, str):
        if directory is None or ".." in spec or spec.startswith("/"):
            raise RegistrationError(f"schema 路径不合法：{spec}")
        path = directory / spec
        if path.suffix == ".json":
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise RegistrationError(f"{spec} 无法解析：{exc}") from exc
        else:
            data = _load_yaml(path)
        match = re.search(r"\.v(\d+(?:\.\d+)*)\.", path.name)
        default_version = match.group(1) if match else "1"
    elif isinstance(spec, dict):
        data, default_version = spec, "1"
    else:
        raise RegistrationError("schema 只能是文件路径或映射")
    version = str(data.get("version") or default_version)
    if "properties" in data:
        return version, _fields_from_json_schema(data, status_map)
    return version, _normalize_fields(data.get("fields"), status_map)


def _default_view(fields: list[dict], status: dict | None) -> dict:
    names = [item["name"] for item in fields]
    columns = names[:6]
    filters = [item["name"] for item in fields if item["type"] == "enum"][:3]
    title = "title" if "title" in names else (names[0] if names else "key")
    chips = [status["field"]] if status and status.get("field") in names else []
    return {"list": {"columns": columns, "filters": filters},
            "detail": {"layout": "sections", "side": [name for name in names if name != title][:6]},
            "card": {"title": title, "chips": chips}}


_VIEW_KEYS = {
    "list": {"columns": "names", "filters": "names", "group_by": "name"},
    "detail": {"layout": "name", "side": "names", "component": "name", "editor": "name"},
    "card": {"title": "name", "subtitle": "name", "chips": "names", "fields": "names", "component": "name"},
}


def _well_typed(kind: str, value) -> bool:
    if kind == "name":
        return isinstance(value, str)
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def view_problems(declared) -> list[str]:
    """视图声明的形状问题（静态检查用）：三段都要是映射，各键的值要是字段名或字段名列表。"""
    if not isinstance(declared, dict):
        return ["视图声明必须是映射"]
    problems = []
    for part, keys in _VIEW_KEYS.items():
        spec = declared.get(part)
        if spec is None:
            continue
        if not isinstance(spec, dict):
            problems.append(f"{part} 必须是映射")
            continue
        for key, kind in keys.items():
            if key in spec and not _well_typed(kind, spec[key]):
                problems.append(f"{part}.{key} 必须是{'字段名' if kind == 'name' else '字段名列表'}")
    layout = (declared.get("detail") or {}).get("layout") if isinstance(declared.get("detail"), dict) else None
    if layout is not None and layout not in ("sections", "table", "custom"):
        problems.append(f"detail.layout 只能是 sections、table、custom")
    return problems


def _clean_view(declared: dict) -> dict:
    """运行时的宽松读法：形状不对的段、键直接丢掉，回到缺省（静态检查会先拦住，这里兜住绕过检查入库的旧版本）。"""
    clean = {}
    for part, keys in _VIEW_KEYS.items():
        spec = declared.get(part)
        if isinstance(spec, dict):
            clean[part] = {key: value for key, value in spec.items()
                           if key not in keys or _well_typed(keys[key], value)}
    return clean


def _card(spec: dict, names: set[str]) -> dict:
    """card 声明只留认得的键，字段名都要在类型里。"""
    card = {"title": str(spec.get("title")) if str(spec.get("title") or "") in names else "title",
            "chips": [str(name) for name in spec.get("chips") or [] if str(name) in names][:CARD_ITEMS_MAX]}
    if str(spec.get("subtitle") or "") in names:
        card["subtitle"] = str(spec["subtitle"])
    shown = [str(name) for name in spec.get("fields") or [] if str(name) in names][:CARD_ITEMS_MAX]
    if shown:
        card["fields"] = shown
    if isinstance(spec.get("component"), str) and _COMPONENT_RE.match(spec["component"]):
        card["component"] = spec["component"]
    return card


def _editor(editor_id, editors: dict | None, *, pack: str, version: str, official: bool,
            pack_label: str = "") -> dict | None:
    """detail.editor（编辑器 id）→ 编辑器引用；包没声明这个编辑器时为 None（按通用详情打开）。"""
    declared = (editors or {}).get(str(editor_id or ""))
    if declared is None:
        return None
    entry = mf.EDITOR_ENTRY_BUNDLED if official else mf.EDITOR_ENTRY_IFRAME
    return {"id": declared["id"], "title": declared["title"], "pack": pack, "pack_label": pack_label or pack,
            "version": version, "mode": "bundled" if official else "iframe", "ui_api": declared.get("ui_api") or "",
            "entry": f"{mf.UI_DIR}/{declared['id']}/{entry}"}


def _view(declared: dict | None, fields: list[dict], status: dict | None, *, editors: dict | None = None,
          pack: str = "", version: str = "", official: bool = False, pack_label: str = "") -> dict:
    view = _default_view(fields, status)
    if not isinstance(declared, dict) or not declared:
        return view
    declared = _clean_view(declared)
    names = {item["name"] for item in fields} | {"title", "key", "updated", "updated_at"}
    for part in ("list", "detail", "card"):
        spec = declared.get(part)
        if isinstance(spec, dict):
            view[part] = {**view[part], **spec}
    lst = view["list"]
    lst["columns"] = [str(name) for name in lst.get("columns") or [] if str(name) in names] or view["list"]["columns"]
    lst["filters"] = [str(name) for name in lst.get("filters") or [] if str(name) in names]
    if lst.get("group_by") and lst["group_by"] not in names:
        lst.pop("group_by")
    detail = view["detail"]
    if detail.get("layout") not in ("sections", "table", "custom"):
        detail["layout"] = "sections"
    if "component" in detail and not (isinstance(detail["component"], str) and _COMPONENT_RE.match(detail["component"])):
        detail.pop("component")
    if "editor" in detail:
        editor = _editor(detail.pop("editor"), editors, pack=pack, version=version, official=official,
                         pack_label=pack_label)
        if editor is not None:
            detail["editor"] = editor
    view["card"] = _card(view["card"], names)
    return view


def parse_registration(data: dict, *, pack: str, pack_version: str, official: bool,
                       directory: Path | None = None, view: dict | None = None,
                       editors: dict | None = None, pack_label: str = "") -> ArtifactType:
    """校验一份产物登记，返回 ArtifactType；不合法抛 RegistrationError。"""
    art_type = str(data.get("type") or "")
    if not art_type.startswith(f"{pack}.") or art_type in mf.CORE_ENTITY_TYPES or len(art_type) > 80:
        raise RegistrationError(f"产物类型 {art_type or '（空）'} 必须以 {pack}. 开头，且不能和五元组重名")
    fmt = str(data.get("format") or "")
    if fmt not in FORMATS:
        raise RegistrationError(f"{art_type} 的格式 {fmt} 不认识（只能是 {'、'.join(FORMATS)}）")
    path = str(data.get("path") or "").replace("{key}", "*")
    if not path or path.startswith("/") or ".." in path or path.count("*") != 1 or "**" in path:
        raise RegistrationError(f"{art_type} 的 path 要是工作区相对路径，且只有一个 * 或 {{key}}")
    allowed = path.startswith(f"ext/{pack}/") or (official and any(path.startswith(f"{base}/") for base in LEGACY_DIRS))
    if not allowed:
        raise RegistrationError(f"{art_type} 只能放在 ext/{pack}/ 下")
    if path.split("/", 1)[0] in mf.PROTECTED_DIRS:
        raise RegistrationError(f"{art_type} 的路径碰到受保护目录")
    status = data.get("status")
    if status is not None:
        if not isinstance(status, dict) or not status.get("field") or not isinstance(status.get("map") or {}, dict):
            raise RegistrationError(f"{art_type} 的 status 要有 field 与 map")
        status = {"field": str(status["field"]), "map": {str(k): str(v) for k, v in (status.get("map") or {}).items()}}
        if isinstance(data.get("status", {}).get("tones"), dict):
            status["tones"] = {str(k): str(v) for k, v in data["status"]["tones"].items() if v in _TONES}
    version, fields = _schema(data.get("schema"), directory, (status or {}).get("map"))
    refs = []
    for ref in data.get("refs") or []:
        if not isinstance(ref, dict) or ref.get("kind") not in ENTRY_KINDS or not ref.get("field"):
            raise RegistrationError(f"{art_type} 的 refs 每项要有 field 与 kind（{'、'.join(ENTRY_KINDS)}）")
        refs.append({"field": str(ref["field"]), "kind": ref["kind"]})
        spec = next((item for item in fields if item["name"] == ref["field"]), None)
        if spec is not None and spec["type"] == "ref":
            spec.setdefault("kind", ref["kind"])
    derive = {str(k): str(v) for k, v in (data.get("derive") or {}).items()}
    bad = [value for value in derive.values()
           if value not in DERIVATIONS and not value.startswith(("field:", "match:"))]
    if bad:
        raise RegistrationError(f"{art_type} 的 derive 不认识：{'、'.join(bad)}")
    index = data.get("index")
    if index is not None:
        index = str(index)
        if not (index.startswith(f"ext/{pack}/") and index.endswith(".jsonl")) or ".." in index:
            raise RegistrationError(f"{art_type} 的 index 只能是 ext/{pack}/ 下的 .jsonl")
    extras = {}
    for name, rel in (data.get("extras") or {}).items():
        rel = str(rel)
        if not (rel.startswith(f"ext/{pack}/") and rel.endswith(".jsonl")) or ".." in rel:
            raise RegistrationError(f"{art_type} 的 extras.{name} 只能是 ext/{pack}/ 下的 .jsonl")
        extras[str(name)] = rel
    readonly = data.get("readonly")
    heading = str(data.get("heading") or "")
    if "\n" in heading or heading.startswith("#") or len(heading) > 80:
        raise RegistrationError(f"{art_type} 的 heading 要是一行、不带 #、不超过 80 字")
    return ArtifactType(
        type=art_type, title=str(data.get("title") or art_type), pack=pack, pack_version=pack_version,
        format=fmt, path=path, key=str(data.get("key") or "filename"), schema_version=version, fields=fields,
        sections=[str(item) for item in data.get("sections") or []], refs=refs, status=status,
        view=_view(view, fields, status, editors=editors, pack=pack, version=pack_version, official=official,
                   pack_label=pack_label),
        view_declared=view is not None, studio=bool(data.get("studio")),
        index=index, derive=derive, readonly=str(readonly) if readonly else None,
        section_labels={str(k): str(v) for k, v in (data.get("section_labels") or {}).items()},
        field_labels={str(k): str(v) for k, v in (data.get("field_labels") or {}).items()},
        table=bool(data.get("table")) or fmt in ("csv", "jsonl") or "table_rows" in derive.values(),
        extras=extras, defaults={str(k): v for k, v in (data.get("defaults") or {}).items()},
        directory=directory, raw=dict(data), heading=heading,
    )


def types_for_pack(view) -> list[ArtifactType]:
    """一个包（registry.PackView）登记的产物类型；单个登记不合法只记日志、跳过。"""
    manifest = view.manifest
    declared_views: dict[str, dict] = {}
    for rel in manifest.items("views"):
        try:
            data = _load_yaml(view.directory / rel)
        except RegistrationError as exc:
            _log.warning("包 %s 的视图声明 %s 不合法：%s", view.name, rel, exc)
            continue
        if data.get("artifact"):
            declared_views[str(data["artifact"])] = data
    editors = {editor["id"]: editor for editor in manifest.editors()}
    out = []
    for rel in manifest.items("artifacts"):
        try:
            data = _load_yaml(view.directory / rel)
            out.append(parse_registration(
                data, pack=view.name, pack_version=view.version, official=bool(getattr(view, "official", False)),
                directory=view.directory, view=declared_views.get(str(data.get("type") or "")), editors=editors,
                pack_label=manifest.label,
            ))
        except Exception as exc:  # noqa: BLE001 - 一份登记坏了只跳过它，不连累同包别的类型
            _log.warning("包 %s 的产物登记 %s 不合法：%s", view.name, rel, exc)
    return out


def types_for_packs(views) -> list[ArtifactType]:
    out: list[ArtifactType] = []
    seen: set[str] = set()
    for view in views:
        try:
            items = types_for_pack(view)
        except Exception as exc:  # noqa: BLE001 - 一个包坏了不连累别的包
            _log.warning("包 %s 的产物登记读不出来：%s", view.name, exc)
            continue
        for item in items:
            if item.type not in seen:
                seen.add(item.type)
                out.append(item)
    return out


def types_for_project(manager, root: Path) -> list[ArtifactType]:
    """本项目已启用的包登记的产物类型（跟锁文件走：启用后马上能看到已有产物）。"""
    from ...services.packs import registry

    return types_for_packs(registry.desired_packs(manager, root))


def get_type(manager, root: Path, art_type: str) -> ArtifactType | None:
    return next((item for item in types_for_project(manager, root) if item.type == art_type), None)


def check_views(directory: Path, manifest) -> list[str]:
    """静态检查用（PL-15 views）：每份视图声明能解析、是映射、挂在本包登记的类型上、各段形状对。"""
    types = set()
    for rel in manifest.items("artifacts"):
        try:
            types.add(str(_load_yaml(Path(directory) / rel).get("type") or ""))
        except RegistrationError:
            continue
    problems = []
    for rel in manifest.items("views"):
        try:
            data = yaml.safe_load((Path(directory) / rel).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            problems.append(f"{rel} 无法解析：{exc}")
            continue
        if not isinstance(data, dict):
            problems.append(f"{rel}: 视图声明必须是映射")
            continue
        if str(data.get("artifact") or "") not in types:
            problems.append(f"{rel}: artifact 要是本包登记的产物类型")
        problems += [f"{rel}: {problem}" for problem in view_problems(data)]
    return problems


def check_registration(directory: Path, rel: str, manifest, *, official: bool = False) -> list[str]:
    """静态检查用（PL-15 entity_types）：一份登记的问题清单。"""
    try:
        data = _load_yaml(Path(directory) / rel)
        parse_registration(data, pack=manifest.name, pack_version=manifest.version,
                           official=official, directory=Path(directory))
    except RegistrationError as exc:
        return [f"{rel}: {exc}"]
    return []
