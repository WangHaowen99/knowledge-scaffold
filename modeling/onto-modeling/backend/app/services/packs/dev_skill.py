"""插件开发 skill（PL-27，ADR-0081）：开发者下载后放进编码助手，一句需求做出能过静态检查的插件包。

下载时在内存里现打包：协议常量、平台快照、检查器都取自正在运行的后端，和服务器查上传包的是同一份代码。
静态部分（SKILL.md 模板、分类说明、合成样例、pack_tool.py）在 assets/pack-dev-skill/，不在 assets/packs/ 下，
不会被当成包导入。

最坏大小：静态部分和检查器固定，压缩前约 170 KiB，现在整个 zip 约 70 KiB。platform.json 随公开包增长，
最多记 500 个包，每个包 50 个版本、每类 100 个名字；名字按 40～80 字符实测压缩前约 12 MiB，zip 约 6 MiB。
超出上限的包和名字离线查不到，上传时服务器照查。
"""

from __future__ import annotations

import io
import json
import pprint
import sys
import time
import zipfile
from pathlib import Path

from ...kernel.artifacts import registry as artifact_registry
from ..flow.assemble import SECTIONS
from ..flow.state import KIND_DIRS
from ..uploads import LIMITS
from . import builtin, checks
from . import manifest as mf
from .service import PackError
from .uploads import PRIVATE_MAX_EXTRACTED_BYTES, PRIVATE_MAX_FILES

ASSET_DIR = "pack-dev-skill"
SNAPSHOT_MAX_PACKS = 500
SNAPSHOT_MAX_VERSIONS = 50
SNAPSHOT_MAX_NAMES = 100
# 原样进 zip 的后端代码：路径相对 app/，在 zip 里保持同样的层级，相对导入不用改
CHECKER_SOURCES = (
    "services/packs/manifest.py", "services/packs/repository.py", "services/packs/checks.py",
    "services/packs/extensions.py", "kernel/artifacts/registry.py",
)
_CHECKER_PACKAGES = ("", "services/", "services/packs/", "services/flow/", "services/onto_mcp/", "kernel/",
                     "kernel/artifacts/")
_SKILLS = {
    "capability": {
        "label": "能力包",
        "kinds": ("capability", "node"),
        "trigger": "要给建模助手加命令、子代理、脚本、流程节点或平台内置页面",
        "ask": "看不出这个包要让助手多做哪件事",
        "kind_choice": "能力包写 `capability`（缺省）。只往建模流程里加节点、不带页面和 skill 的写 `node`，`init` 时加 `--kind node`。",
        "init_kind": " [--kind node]",
        "closing": "",
    },
    "industry": {
        "label": "行业包",
        "kinds": ("knowledge",),
        "trigger": "要把一个行业的术语、业务对象、规则、指标沉淀成建模时可用的知识",
        "ask": "看不出是哪个行业，开发者也没给材料",
        "kind_choice": "固定写 `knowledge`，`category: industry`，只有数据，不带命令和脚本。",
        "init_kind": "",
        "closing": "",
    },
    "system": {
        "label": "系统知识包",
        "kinds": ("knowledge",),
        "trigger": "要把一个商用系统的表、字段字典和业务含义做成建模时可查的知识",
        "ask": "看不出是哪个系统，开发者也没给表字段来源",
        "kind_choice": "固定写 `knowledge`，`category: system`，只有数据，不带命令和脚本。",
        "init_kind": "",
        "closing": "",
    },
    "provider": {
        "label": "连接器包",
        "kinds": ("provider",),
        "trigger": "要让平台能解析一种新的资料文件格式（转换器）",
        "ask": "看不出要转换哪种文件格式",
        "kind_choice": "固定写 `provider`，转换器写在清单顶层的 `converter` 段，不放进 `contributes`。",
        "init_kind": "",
        "closing": "   - **这个包导入后转换器不会运行。** 包能上传、能安装，也能在插件中心的「多源连接器」里看到，但转换器在连接器网关里运行，"
                   "网关只认打进镜像的转换器。要真正跑起来，平台管理员得把转换器目录放进网关镜像并重新构建部署。这一条必须原样告诉开发者。",
    },
}
CATEGORIES = tuple(_SKILLS)
# 静态检查项（references/protocol.md 的表）；id 和 checks.run、PL-22 报告里的一致
_CHECK_NOTES = (
    ("manifest", "`manifest.yaml` 能解析，必填字段合法"),
    ("checksums", "`checksums.sha256` 和文件一致，`pack_tool.py` 负责写"),
    ("symlinks", "不含符号链接"),
    ("declared", "`contributes` 声明的命令、子代理、skill、脚本都存在；有文件没声明给警告"),
    ("compat", "`kernel_api`、`page_api` 满足平台接口版本，`depends` 有满足的版本"),
    ("names", "命令带 `<包名>-` 前缀，不和基础建模能力重名，不和已有的包的命令重名，脚本不占内核保留名，同名同版本没有入库"),
    ("entity_types", "产物类型带包名前缀，不用五元组实体类型的名字"),
    ("views", "视图声明格式正确"),
    ("node_pack", "只有 `kind: node` 有这一项：只带允许的组成，节点编号、挂法、四类节点各自的声明都对"),
    ("write_scope", "节点写入范围不碰受保护目录，脚本节点的输出、人工节点的记录落在写入范围里"),
    ("node_entry", "节点入口命令、节点钩子的命令是本包的"),
    ("confine", "`flow.yaml` 声明了 `confine` 时才有这一项：只写 `scope_file`，是具体文件，不在 `ontology/` 和受保护目录下，落在本包节点的 `produces` 里"),
    ("hooks", "不带 `settings.json`，只能追加 `PreToolUse` 守卫，守卫引用的脚本要存在"),
    ("frontend", "页面只从平台内置里选，前端代码只放 `ui/` 下"),
    ("ui", "`ui/` 只能是构建好的静态资源，编辑器入口是 `ui/<id>/index.html`"),
    ("extensions", "声明了界面入口、门禁、配置、指标、告警、MCP 工具时才有这一项：格式正确、名字不撞"),
    ("secrets", "不含疑似凭据"),
    ("syntax", "`scripts/` 下的 `.py`、`.sh` 语法正确"),
    ("prompt_budget", "命令、子代理、skill、规则片段的总体积在预算内"),
    ("private_name", "包名没被占用（只有普通用户上传才有这一项）"),
)


def _names(values) -> list[str]:
    return sorted(values)[:SNAPSHOT_MAX_NAMES]


def _declared_names(manifest, key: str) -> list[str]:
    return _names({str(item.get("name") or item.get("id") or "") for item in manifest.declared(key)})


def _pack_entry(repository, name: str) -> dict:
    versions = repository.versions(name)[:SNAPSHOT_MAX_VERSIONS]
    commands: set[str] = set()
    for version in versions:
        try:
            commands |= set(repository.manifest(name, version).items("commands"))
        except Exception:  # noqa: BLE001 - 坏掉的旧版本不影响下载，和静态检查里的处理一样
            continue
    entry = {"versions": versions, "stable": repository.latest(name, "stable"), "commands": _names(commands),
             "mcp_tools": [], "metrics": []}
    if entry["stable"]:
        try:
            manifest = repository.manifest(name, entry["stable"])
        except Exception:  # noqa: BLE001
            return entry
        entry.update(mcp_tools=_declared_names(manifest, "mcp_tools"), metrics=_declared_names(manifest, "metrics"))
    return entry


def _snapshot_names(repository) -> list[str]:
    # core 排在最前：按字母序可能落到上限之外，而节点包的依赖判断离不开它
    others = [name for name in repository.names() if name != builtin.CORE]
    return [builtin.CORE, *others][:SNAPSHOT_MAX_PACKS]


def platform_snapshot(manager, category: str) -> dict:
    """平台快照。只取公开的包：别人的私有包名不能发出去（ADR-0079 只归本人可见）。"""
    repository = manager.packs.repository
    manager.packs.core_version()  # 没有 stable core 时先从镜像导入
    stable = repository.latest(builtin.CORE, "stable")
    core_dir = repository.version_dir(builtin.CORE, stable)
    core_manifest = repository.manifest(builtin.CORE, stable)
    return {
        "category": category,
        "kinds": list(_SKILLS[category]["kinds"]),
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "kernel_api": mf.KERNEL_API,
        "page_api": mf.PAGE_API,
        "flow_api": mf.FLOW_API,
        "ui_api": mf.UI_API,
        "limits": {"zip_bytes": LIMITS["pack_private_upload_bytes"], "extracted_bytes": PRIVATE_MAX_EXTRACTED_BYTES,
                   "files": PRIVATE_MAX_FILES},
        "core": {
            "version": stable,
            "names": {key: sorted(values) for key, values in checks.declared_names(core_manifest, core_dir).items()},
            "probes": sorted(checks._flow_probes(core_dir, core_manifest)),
        },
        "packs": {name: _pack_entry(repository, name) for name in _snapshot_names(repository)},
    }


def _code(values) -> str:
    return "、".join(f"`{value}`" for value in values)


def _mib(size: int) -> str:
    return f"{size // (1024 * 1024)} MiB"


def protocol_markdown(snapshot: dict) -> str:
    limits = snapshot["limits"]
    rows = "\n".join(f"| `{check_id}` | {text} |" for check_id, text in _CHECK_NOTES)
    return f"""# 插件包协议

下载 skill 时由平台后端生成，平台基础建模能力版本 `{snapshot['core']['version']}`。一个包是一个目录，根下有 `manifest.yaml`，
`contributes` 列出包带的内容，上传时按下面的检查项逐条查。

## 接口版本

| 接口 | 平台当前版本 | 清单字段 |
|---|---|---|
| 内核接口 | `{mf.KERNEL_API}` | `kernel_api`（必填），写 `">=1.0 <2.0"` 这样的范围 |
| 平台页面接口 | `{mf.PAGE_API}` | `page_api`（选页面时写） |
| 流程定义格式 | `{mf.FLOW_API}` | `flow.yaml` 的 `flow_api` |
| 包自带界面接口 | `{mf.UI_API}` | `contributes.editors[].ui_api` |

## 清单字段

| 字段 | 说明 |
|---|---|
| `name` | 必填，2～40 位小写字母、数字、连字符，字母开头。保留名：{_code(checks.RESERVED_PACK_NAMES)} |
| `version` | 必填，语义化版本号，如 `0.1.0` |
| `kind` | 必填，{_code(mf.KINDS)}（`exporter` 没有开发 skill） |
| `category` | {_code(mf.CATEGORIES)}；缺省按 kind 取，`node` 归 `capability`，`knowledge` 缺省 `industry` |
| `label`、`description` | 插件中心显示的名字和一句话说明 |
| `kernel_api` | 必填，兼容的内核接口范围 |
| `page_api` | 兼容的平台页面接口范围 |
| `engines` | {_code(mf.ENGINES)}，缺省全部 |
| `depends` | 「包名: 版本范围」的映射 |
| `publisher` | `{{kind: tenant, label: 发布方}}`；是不是官方包看入库来源，写 `official` 也不算 |
| `changelog` | 这个版本的更新说明，显示在插件详情的版本记录里 |
| `converter` | 只有 `kind: provider` 写，清单顶层的段，不放进 `contributes`：`entry`、`types`、`license`、`default_for` |
| `contributes` | 包带的内容，见下一节 |

## contributes

- 列表字段：{_code(mf.LIST_FIELDS)}。
- 命令放 `commands/<名>.md`，子代理放 `agents/<名>.md`，skill 放 `skills/<名>/`，脚本放 `scripts/<文件名>`，声明的都要存在。
- 指向包内路径的字段：`flow`、`readme`、`knowledge_dir`，以及 `artifacts`、`views`、`docs`、`nodes` 的每一项，只能是包内相对路径。
- `pages`：只能选平台内置页面 {_code(mf.BUILTIN_PAGES)}。图标 `nav.icon` 只能选 {_code(mf.NAV_ICONS)}。
- `subpages`：只能选 {_code(mf.BUILTIN_SUBPAGES)}，挂在内核页面 {_code(mf.KERNEL_PAGES)} 下。
- `hooks`：只能写 `PreToolUse`，每项有 `matcher` 和 `command`。
- 知识包的数据字段 `knowledge_dir`、`concepts`、`checks`、`standards`、`reference_dir`、`library_dir`、`examples`：写法在行业包和系统包 skill 的参考文档里。
- `editors`：包自带界面，入口 `{mf.UI_DIR}/<id>/{mf.EDITOR_ENTRY_IFRAME}`，`{mf.UI_DIR}/` 只能放这些后缀的静态资源：{_code(checks.UI_STATIC_SUFFIXES)}。

## 不能碰的地方

- 写入范围不能碰：{_code(mf.PROTECTED_DIRS)}。
- 插件产物不能用五元组实体类型的名字：{_code(mf.CORE_ENTITY_TYPES)}。产物注册的 `format` 取 {_code(artifact_registry.FORMATS)}，字段 `type` 取 {_code(artifact_registry.FIELD_TYPES)}。
- 脚本名不能以 {_code(checks._KERNEL_SCRIPT_PREFIXES)} 开头，不能以 `_guard.sh` 结尾。
- `{mf.UI_DIR}/` 之外不能有前端代码：后缀 {_code(checks.FRONTEND_SUFFIXES)}，文件名 {_code(checks.FRONTEND_NAMES)}，目录 {_code(checks.FRONTEND_DIRS)}。

## 节点

- 节点挂法 `placement`：{_code(checks.NODE_PLACEMENTS)}；节点包不能挂主线 `spine`，能力包可以。除 `on_demand` 外要写锚点 `after` 或 `before`。
- 范围限定 `confine`（`flow.yaml` 顶层，可选）：`{{scope_file: <路径>}}`。范围文件合入主线、`status` 是 `active` 时，基线提交里有、`editable` 里没有的本体条目助手不能写也不能删，新增条目不限；范围文件只能在它所属节点的命令运行时写。一次装配最多一个包声明。
- `autobuild: false`：自主构建跳过这个节点（要用户给输入才能做的节点）。
- 节点类型 `kind`：`agent`（缺省）、`script`、`human`、`external`。
- 面板分组 `section`：{_code(SECTIONS)}。
- `stale_on`、`requires` 里的产物类别：{_code(KIND_DIRS)}。
- `done` 能用本包的探针和基础建模能力的探针：{_code(snapshot['core']['probes'])}。
- `kind: node` 的 `contributes` 只能有：{_code(checks.NODE_PACK_ALLOWED)}。

## 生效时机

{"、".join(f"`{kind}` {mf.EFFECTIVE[level]}" for kind, level in mf.HOT_LEVELS.items())}。

## 上传限制

zip ≤ {_mib(limits['zip_bytes'])}，解压后 ≤ {_mib(limits['extracted_bytes'])}，≤ {limits['files']} 个文件。
提示词（命令、子代理、skill、规则片段）合计超过 {checks.PROMPT_SOFT_BYTES // 1024} KiB 给警告，超过 {checks.PROMPT_HARD_BYTES // (1024 * 1024)} MiB 失败。

## 静态检查项

| id | 查什么 |
|---|---|
{rows}

`pack_tool.py check` 跑同一份检查，平台已有的包、基础建模能力的命令和探针取自 `platform.json`。

## 离线查不了的

这些项 `pack_tool.py` 单独列成「上传时检查」，不算通过，结果看上传后的报告：

- 包名有没有被别的用户的私有包占用；公开包的名字离线能查。
- 这个版本是不是已经装过。
- `depends` 依赖的包不在公开包里时，它是不是开发者自己装过的私有包。
- 本机 Python 版本和平台（Python {snapshot['python']}）不同时，脚本语法以平台为准；本机没有 bash 时，`.sh` 语法没查。
"""


def _skill_markdown(template: str, category: str) -> str:
    skill = _SKILLS[category]
    text = template.replace("{{category}}", category)
    for key in ("label", "trigger", "ask", "kind_choice", "init_kind", "closing"):
        text = text.replace("{{" + key + "}}", skill[key])
    return text


def _checker_files() -> dict[str, bytes]:
    from ..onto_mcp.definitions import TOOLS

    app_dir = Path(__file__).resolve().parents[2]
    out = {f"scripts/checker/{rel}": (app_dir / rel).read_bytes() for rel in CHECKER_SOURCES}
    out.update({f"scripts/checker/{package}__init__.py": b"" for package in _CHECKER_PACKAGES})
    # checks、extensions 懒加载这两个模块，原模块拖着半个后端；离线只用到这两个常量，下载时生成字面量
    out["scripts/checker/services/flow/state.py"] = (
        "# 下载时生成：平台的产物类别，节点声明的 stale_on、requires 只能用这些\n"
        f"KIND_DIRS = {pprint.pformat(KIND_DIRS, sort_dicts=False)}\n").encode("utf-8")
    out["scripts/checker/services/onto_mcp/definitions.py"] = (
        "# 下载时生成：平台内置 MCP 工具名，包声明的工具不能重名\n"
        f"TOOLS = dict.fromkeys({pprint.pformat(sorted(TOOLS))})\n").encode("utf-8")
    return out


def build(manager, category: str) -> tuple[str, bytes]:
    """打包一个分类的 skill，返回（文件名, zip 内容）。没有 skill 的分类（exporter、不认识的）回 404。"""
    if category not in _SKILLS:
        raise PackError("not_found", "这个分类没有开发 skill", 404)
    static = Path(manager.assets_dir) / ASSET_DIR
    snapshot = platform_snapshot(manager, category)
    files: dict[str, bytes] = {
        "SKILL.md": _skill_markdown((static / "SKILL.md").read_text(encoding="utf-8"), category).encode("utf-8"),
        "platform.json": json.dumps(snapshot, ensure_ascii=False, indent=1).encode("utf-8"),
        "references/protocol.md": protocol_markdown(snapshot).encode("utf-8"),
        f"references/{category}.md": (static / "references" / f"{category}.md").read_bytes(),
        "scripts/pack_tool.py": (static / "scripts" / "pack_tool.py").read_bytes(),
        **_checker_files(),
    }
    examples = static / "examples" / category
    for path in sorted(examples.rglob("*")):
        rel = path.relative_to(examples).as_posix()
        if path.is_file() and "__pycache__" not in rel and path.name != ".DS_Store":
            files[f"references/examples/{rel}"] = path.read_bytes()
    top = f"onto-plugin-dev-{category}"
    now = time.localtime()[:6]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for rel, data in sorted(files.items()):
            info = zipfile.ZipInfo(f"{top}/{rel}", date_time=now)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, data)
    return f"{top}.zip", buffer.getvalue()
