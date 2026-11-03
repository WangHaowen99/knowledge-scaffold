"""包清单（详设 5.2）、版本号与兼容范围、热更新分级（详设 3.5）。

清单是包根目录的 manifest.yaml。这里只做格式解析与字段校验，文件是否齐全、名字是否冲突
这类要看包内容和仓库的检查在 checks.py。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

MANIFEST_FILE = "manifest.yaml"
CHECKSUMS_FILE = "checksums.sha256"

# 内核接口版本：包清单 kernel_api 的比对对象，内核随版本发布（H3）时才会变
KERNEL_API = "1.0"
# 平台页面接口版本（详设 5.9.6）：包声明的 page_api 不满足时不让启用、升级
PAGE_API = "1.0"
# 流程定义格式版本（flow.yaml 的 flow_api）
FLOW_API = 1
# 包自带界面的窄接口版本（ADR-0055）：上传包的 iframe 编辑器与平台之间 postMessage 协议的版本
UI_API = "1.0"
# 包自带界面的目录与编辑器入口（ADR-0055）：官方包（镜像内置）的编辑器随平台编译，入口是 ui/<id>/index.jsx；
# 上传包只能是构建好的静态资源，经不同源的 iframe 加载，入口是 ui/<id>/index.html
UI_DIR = "ui"
EDITOR_ENTRY_BUNDLED = "index.jsx"
EDITOR_ENTRY_IFRAME = "index.html"
EDITOR_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,39}$")

KINDS = ("capability", "knowledge", "node", "provider", "exporter")
CATEGORIES = ("capability", "industry", "system", "provider", "node", "exporter")
# 节点包是能力包的特例（用户 2026-11-02 定）：插件目录里归进能力包，清单 kind 仍是 node，用于更严的静态检查
_DEFAULT_CATEGORY = {"capability": "capability", "knowledge": "industry", "node": "capability",
                     "provider": "provider", "exporter": "exporter"}
ENGINES = ("claude", "jiuwen")

# 热更新分级（详设 3.5）：纯数据 H0 下次读取即生效；能力包、节点包 H1 下一条消息生效；
# 可替换实现 H2 要滚动重启或独立进程重载（P3 才开放，项目级启用不支持）
HOT_LEVELS = {"knowledge": "H0", "capability": "H1", "node": "H1", "provider": "H2", "exporter": "H2"}
EFFECTIVE = {"H0": "immediate", "H1": "next_message", "H2": "restart"}

# 平台内置的插件页面（详设 5.9.8；前端页面 ID）：包只能从这里选。代码生成与求解页的 ID 是 scripts
BUILTIN_PAGES = ("scenarios", "knowledge", "scripts")
# 平台内置的子页（详设 5.9.1「给内核页面加子页」，资料页的数据源、采集与快照、证据浏览器）
BUILTIN_SUBPAGES = ("sources", "snapshots", "schema")
# 内核页面（左栏固定的 4 项，也是可以挂子页的父页面）
KERNEL_PAGES = ("wb-l2", "studio", "resources", "publish")
# 左栏图标只能从平台图标集里选（详设 5.9.7）
NAV_ICONS = ("layout", "graph", "folder", "upload", "flask", "book", "code", "database", "chart", "check",
             "layers", "search", "spark", "tool")
# 五元组实体类型：插件产物不能用这些名字（详设 4.4「插件不能新增实体类型」）
CORE_ENTITY_TYPES = ("object", "operation", "rule", "metric", "script")
# 包声明的写入范围不能碰的目录（详设 6.9 静态检查、契约 PL-15）
PROTECTED_DIRS = ("resource", ".claude", ".onto", ".git", ".ontology-state", "CLAUDE.md", "README.md")

# 清单 contributes 里按名字列出的组成；skills 是目录，其余是文件
LIST_FIELDS = ("commands", "agents", "skills", "scripts", "templates", "reference",
               "nodes", "entries", "artifacts", "views", "reports", "gates", "config", "metrics", "rules",
               "prompts", "docs")

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,39}$")
_ITEM_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*$")
_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


class ManifestError(ValueError):
    """清单不合法：problems 是逐条原因（静态检查报告里逐条列出）。"""

    def __init__(self, problems: list[str]):
        super().__init__("；".join(problems))
        self.problems = problems


# ---- 版本号与兼容范围 ----


@dataclass(frozen=True)
class Version:
    major: int
    minor: int
    patch: int
    pre: tuple = ()
    build: str = ""

    @classmethod
    def parse(cls, text: str) -> "Version":
        """semver；兼容范围里允许省略次版本、修订号（"1.4" 即 1.4.0）。"""
        text = str(text or "").strip()
        match = _SEMVER_RE.match(text)
        if not match:
            short = re.fullmatch(r"(0|[1-9]\d*)(?:\.(0|[1-9]\d*))?", text)
            if not short:
                raise ValueError(f"版本号不合法：{text!r}")
            return cls(int(short.group(1)), int(short.group(2) or 0), 0)
        pre = tuple(int(part) if part.isdigit() else part for part in (match.group(4) or "").split(".") if part)
        return cls(int(match.group(1)), int(match.group(2)), int(match.group(3)), pre, match.group(5) or "")

    def key(self) -> tuple:
        """semver 优先级：构建元数据不参与比较，预发布版本低于正式版本。"""
        pre = tuple((0, part) if isinstance(part, int) else (1, part) for part in self.pre)
        return (self.major, self.minor, self.patch, 0 if self.pre else 1, pre)

    def __str__(self) -> str:
        text = f"{self.major}.{self.minor}.{self.patch}"
        if self.pre:
            text += "-" + ".".join(str(part) for part in self.pre)
        if self.build:
            text += "+" + self.build
        return text


def is_version(text: str) -> bool:
    return bool(_SEMVER_RE.match(str(text or "").strip()))


def version_key(text: str) -> tuple:
    try:
        return Version.parse(text).key()
    except ValueError:
        return (-1,)


def _comparator(token: str):
    for op in (">=", "<=", ">", "<", "=", "^", "~"):
        if token.startswith(op):
            return op, Version.parse(token[len(op):])
    return "=", Version.parse(token)


def satisfies(version: str, spec: str) -> bool:
    """version 是否落在兼容范围 spec 里。

    spec 是空格分隔的比较式，全部满足才算：`>=1.0 <2.0`、`>=1.4`、`^1.2`、`~1.2`、`1.2.3`；
    `*` 或空串表示不限。范围写错按不满足处理。
    """
    spec = str(spec or "").strip()
    if spec in ("", "*"):
        return True
    try:
        current = Version.parse(version).key()
        for token in spec.replace(",", " ").split():
            op, bound = _comparator(token)
            key = bound.key()
            if op == ">=" and not current >= key:
                return False
            if op == ">" and not current > key:
                return False
            if op == "<=" and not current <= key:
                return False
            if op == "<" and not current < key:
                return False
            if op == "=" and current[:4] != key[:4]:
                return False
            if op == "^":
                upper = (bound.major + 1, 0, 0) if bound.major else (0, bound.minor + 1, 0)
                if not (current >= key and current[:3] < upper):
                    return False
            if op == "~":
                if not (current >= key and current[:3] < (bound.major, bound.minor + 1, 0)):
                    return False
    except ValueError:
        return False
    return True


def valid_range(spec: str) -> bool:
    spec = str(spec or "").strip()
    if spec in ("", "*"):
        return True
    try:
        for token in spec.replace(",", " ").split():
            _comparator(token)
    except ValueError:
        return False
    return True


# ---- 清单 ----


@dataclass
class Manifest:
    name: str
    version: str
    kind: str
    label: str
    description: str
    category: str
    publisher: dict
    kernel_api: str
    page_api: str
    engines: list[str]
    depends: dict[str, str]
    required: bool
    contributes: dict
    checksums: str
    raw: dict = field(default_factory=dict)

    @property
    def hot_level(self) -> str:
        return HOT_LEVELS.get(self.kind, "H1")

    @property
    def effective(self) -> str:
        return EFFECTIVE[self.hot_level]

    def items(self, kind: str) -> list[str]:
        value = self.contributes.get(kind) or []
        return [str(item.get("id") if isinstance(item, dict) else item) for item in value]

    def hooks(self) -> list[dict]:
        """包追加的守卫：只认 PreToolUse 的 {matcher, command}（详设 4.8：只能追加拒绝型守卫）。"""
        hooks = self.contributes.get("hooks") or {}
        return [dict(item) for item in hooks.get("PreToolUse") or []] if isinstance(hooks, dict) else []

    def pages(self) -> list[dict]:
        pages = []
        for item in self.contributes.get("pages") or []:
            pages.append(dict(item) if isinstance(item, dict) else {"id": str(item)})
        return pages

    def subpages(self) -> list[dict]:
        return [dict(item) for item in self.contributes.get("subpages") or [] if isinstance(item, dict)]

    def editors(self) -> list[dict]:
        """包自带的编辑器（ADR-0055）：`{id, title, ui_api}`；视图 `detail.editor` 按 id 引用。"""
        return [{"id": str(item["id"]), "title": str(item.get("title") or item["id"]),
                 "ui_api": str(item.get("ui_api") or "")}
                for item in self.contributes.get("editors") or [] if isinstance(item, dict) and item.get("id")]

    def declared(self, kind: str) -> list[dict]:
        """entries、gates、config、metrics、prompts 这类按映射声明的扩展项（字符串项当作 id）。"""
        return [dict(item) if isinstance(item, dict) else {"id": str(item)} for item in self.contributes.get(kind) or []]

    def summary(self, *, checksums_ok: bool = True, size: int = 0, files: int = 0) -> dict:
        """契约 7.11 ManifestSummary。"""
        counted = {key: len(self.items(key)) for key in
                   ("commands", "agents", "skills", "scripts", "templates", "entries", "gates", "config", "metrics")}
        return {
            "name": self.name, "version": self.version, "kind": self.kind,
            "kernel_api": self.kernel_api, "engines": list(self.engines), "depends": dict(self.depends),
            "contributes": {
                **counted,
                "hooks": len(self.hooks()),
                "nodes": self.items("nodes"),
                "pages": [page.get("id", "") for page in self.pages()],
                "artifacts": self.items("artifacts"),
                "views": self.items("views"),
                "reports": self.items("reports"),
                "editors": [editor["id"] for editor in self.editors()],
            },
            "checksums_ok": checksums_ok, "size": size, "files": files,
        }

    def to_dict(self) -> dict:
        return dict(self.raw)


def _as_list(value, problems: list[str], key: str) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        problems.append(f"contributes.{key} 必须是列表")
        return []
    return value


def parse(data: dict) -> Manifest:
    """校验清单字段，返回 Manifest；不合法时抛 ManifestError（逐条原因）。"""
    problems: list[str] = []
    if not isinstance(data, dict):
        raise ManifestError(["清单必须是 YAML 映射"])
    name = str(data.get("name") or "")
    if not NAME_RE.match(name):
        problems.append("name 必须是 2～40 位小写字母、数字或连字符，以字母开头")
    version = str(data.get("version") or "")
    if not is_version(version):
        problems.append("version 必须是语义化版本号（如 1.2.0）")
    kind = str(data.get("kind") or "")
    if kind not in KINDS:
        problems.append(f"kind 必须是 {' | '.join(KINDS)} 之一")
    kernel_api = str(data.get("kernel_api") or "")
    if not kernel_api:
        problems.append("缺少 kernel_api（兼容的内核接口版本范围）")
    elif not valid_range(kernel_api):
        problems.append(f"kernel_api 范围写法不合法：{kernel_api}")
    page_api = str(data.get("page_api") or "")
    if page_api and not valid_range(page_api):
        problems.append(f"page_api 范围写法不合法：{page_api}")
    engines = data.get("engines") or list(ENGINES)
    if not isinstance(engines, list) or any(engine not in ENGINES for engine in engines):
        problems.append(f"engines 只能取 {' / '.join(ENGINES)}")
        engines = []
    depends = data.get("depends") or {}
    if not isinstance(depends, dict):
        problems.append("depends 必须是「包名: 版本范围」的映射")
        depends = {}
    for dep, spec in depends.items():
        if not NAME_RE.match(str(dep)) or not valid_range(str(spec)):
            problems.append(f"depends 里 {dep}: {spec} 写法不合法")
    category = str(data.get("category") or _DEFAULT_CATEGORY.get(kind, "capability"))
    if kind == "node" or category == "node":
        category = "capability"
    if category not in CATEGORIES:
        problems.append(f"category 必须是 {' | '.join(CATEGORIES)} 之一")
    # 清单里的发布方只用来显示：是不是官方包看入库来源（镜像内置），不看清单自己怎么写（ADR-0045）
    publisher = data.get("publisher") or {"kind": "tenant", "label": ""}
    if isinstance(publisher, str):
        publisher = {"kind": "official" if publisher == "official" else "tenant", "label": publisher}
    if not isinstance(publisher, dict) or publisher.get("kind") not in ("official", "tenant"):
        problems.append("publisher.kind 必须是 official 或 tenant")
        publisher = {"kind": "tenant", "label": ""}
    contributes = data.get("contributes") or {}
    if not isinstance(contributes, dict):
        problems.append("contributes 必须是映射")
        contributes = {}
    normalized = dict(contributes)
    for key in LIST_FIELDS + ("pages", "subpages", "editors"):
        normalized[key] = _as_list(contributes.get(key), problems, key)
    editor_ids = set()
    for item in normalized["editors"]:
        editor_id = item.get("id") if isinstance(item, dict) else None
        if not isinstance(editor_id, str) or not EDITOR_ID_RE.match(editor_id):
            problems.append(f"contributes.editors 每项要有 id（小写字母开头，字母、数字、连字符）：{item!r}")
        elif editor_id in editor_ids:
            problems.append(f"contributes.editors 里的 id 重复：{editor_id}")
        else:
            editor_ids.add(editor_id)
            if item.get("ui_api") not in (None, "") and not valid_range(str(item["ui_api"])):
                problems.append(f"编辑器 {editor_id} 的 ui_api 范围写法不合法：{item['ui_api']}")
    for key in ("commands", "agents", "skills", "scripts", "templates", "reference", "rules"):
        for item in normalized[key]:
            if not isinstance(item, str) or not _ITEM_NAME_RE.match(item) or ".." in item:
                problems.append(f"contributes.{key} 里的名字不合法：{item!r}")
    hooks = contributes.get("hooks") or {}
    if not isinstance(hooks, dict):
        problems.append("contributes.hooks 必须是映射")
        hooks = {}
    for event in hooks:
        if event != "PreToolUse":
            problems.append(f"包只能追加拒绝型守卫（PreToolUse），不能注册 {event}")
    for item in hooks.get("PreToolUse") or []:
        if not isinstance(item, dict) or not item.get("command") or not isinstance(item.get("command"), str):
            problems.append("contributes.hooks.PreToolUse 每项要有 matcher 与 command")
    normalized["hooks"] = hooks
    # 指向包内文件、目录的字段：只能是包内的相对路径（不能是绝对路径、不能带 ..）
    for key in ("flow", "readme", "knowledge_dir"):
        value = contributes.get(key)
        if value not in (None, "") and not _inside_pack(value):
            problems.append(f"contributes.{key} 必须是包内的相对路径")
    for key in ("artifacts", "views", "docs", "nodes"):  # nodes：节点编号或包内的节点文件（审查第 10 条）
        for item in normalized[key]:
            if not _inside_pack(item):
                problems.append(f"contributes.{key} 里的路径必须在包内：{item!r}")
    if problems:
        raise ManifestError(problems)
    return Manifest(
        name=name, version=version, kind=kind,
        label=str(data.get("label") or name), description=str(data.get("description") or ""),
        category=category, publisher={"kind": publisher["kind"], "label": str(publisher.get("label") or "")},
        kernel_api=kernel_api, page_api=page_api, engines=list(engines),
        depends={str(k): str(v) for k, v in depends.items()},
        required=bool(data.get("required")), contributes=normalized,
        checksums=str(data.get("checksums") or CHECKSUMS_FILE), raw=dict(data),
    )


def workspace_rel(value) -> str | None:
    """包声明的工作区路径（或通配）规范化成相对路径；绝对路径、带 `..`、带反斜杠或控制字符的一律 None。

    节点的 produces / writes、脚本输出、决策记录都先过它，再按规范化后的完整路径判受保护目录、匹配写入范围
    （审查第 1 条：`ext/../.git/config`、`reports/x/../../.onto/packs.lock` 这类写法原来能越过检查）。"""
    if not isinstance(value, str) or not value.strip() or "\\" in value or any(ord(ch) < 32 for ch in value):
        return None
    if value.startswith("/") or ":" in value.split("/", 1)[0]:
        return None
    parts = [part for part in value.split("/") if part not in ("", ".")]
    if not parts or ".." in parts:
        return None
    return "/".join(parts)


def protected_path(value) -> bool:
    """规范化不了，或第一段是受保护目录（含 .git）。"""
    rel = workspace_rel(value)
    return rel is None or rel.split("/", 1)[0] in PROTECTED_DIRS


def _inside_pack(value) -> bool:
    """包内的相对路径：非空字符串，不以 / 或 \\ 开头、不带盘符，没有 .. 这一级。"""
    if not isinstance(value, str) or not value.strip():
        return False
    parts = re.split(r"[\\/]", value)
    return not (value.startswith(("/", "\\")) or ":" in parts[0] or ".." in parts)


def load(directory: Path) -> Manifest:
    path = Path(directory) / MANIFEST_FILE
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ManifestError([f"缺少 {MANIFEST_FILE}"]) from None
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ManifestError([f"{MANIFEST_FILE} 无法解析：{exc}"]) from exc
    return parse(data)


def dump(data: dict) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
