"""包仓库入库前的静态检查（详设 6.9 第 1 步、8.1 第 1 步、5.9.6；契约 PL-15；包自带界面见 ADR-0055）。

每项给 pass / warn / fail；有 fail 不能发布。检查只读包内容、清单与仓库，不执行包里的任何代码
（脚本只做语法编译检查，不运行）。
"""

from __future__ import annotations

import fnmatch
import py_compile
import re
import subprocess
import tempfile
from pathlib import Path

from . import manifest as mf
from .manifest import Manifest, ManifestError
from .repository import PackRepository, pack_files, symlinks, verify_checksums

# 前端代码：只能放在 ui/ 下（ADR-0055 修订详设 5.9.1）；其余位置一律不行
FRONTEND_SUFFIXES = (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".vue", ".svelte", ".wasm", ".css", ".scss", ".less")
FRONTEND_NAMES = ("package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "vite.config.js")
FRONTEND_DIRS = ("node_modules", "frontend", "web", "static", "dist", "public")
# 凭据特征（只判有无，不在报告里回显命中的原文）
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{24,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token)\s*[:=]\s*['\"][^'\"\s$<{]{8,}['\"]"),
    re.compile(r"(?i)\b(?:mysql|postgres(?:ql)?|mongodb|redis)://[^\s:/]+:[^\s@/]+@"),
)
_TEXT_SUFFIXES = (".md", ".py", ".sh", ".yaml", ".yml", ".json", ".txt", ".html", ".toml", ".ini", ".cfg", ".env",
                  ".js", ".mjs", ".jsx", ".css")
# 上传包 ui/ 下只能是构建好的静态资源（ADR-0055）：经不同源的 iframe 加载，不能有要编译的源码，也不能有服务端可执行内容
UI_STATIC_SUFFIXES = (".html", ".htm", ".js", ".mjs", ".css", ".json", ".map", ".txt",
                      ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".woff", ".woff2", ".ttf")
# 提示词体积预算：命令、子代理、skill、规则片段合计
PROMPT_SOFT_BYTES = 1536 * 1024
PROMPT_HARD_BYTES = 4 * 1024 * 1024
# 内核占用的脚本名：包不能覆盖内核守卫（详设 4.8：包删不掉内核守卫）
_KERNEL_SCRIPT_PREFIXES = ("_guard_common",)
# 保留的包名：官方命令都是 /ontology-…，叫 ontology 的上传包能冒用整套命令前缀（审查第 1 条）。
# core 只有管理员以旧资产包方式上传 core 新版本时可用（租户不行，见下面的 names 检查）
RESERVED_PACK_NAMES = ("ontology", "onto", "kernel", "platform", "core")


def item(check_id: str, label: str, result: str, detail: str = "") -> dict:
    """一项检查结果（契约 7.11 StaticCheck）。"""
    return {"id": check_id, "label": label, "result": result, "detail": detail}


_check = item


def _names(directory: Path, sub: str, suffix: str) -> set[str]:
    base = directory / sub
    if not base.is_dir():
        return set()
    return {path.stem if suffix else path.name for path in base.iterdir()
            if not path.name.startswith(".") and (path.is_dir() if not suffix else path.suffix == suffix)}


def declared_names(manifest: Manifest, directory: Path) -> dict[str, set[str]]:
    """命令、子代理、skill、脚本、模板、参考：清单声明的与目录实际的取并集（名字冲突按实际内容判）。"""
    directory = Path(directory)
    actual = {
        "commands": _names(directory, "commands", ".md"),
        "agents": _names(directory, "agents", ".md"),
        "skills": _names(directory, "skills", ""),
        "scripts": {p.name for p in (directory / "scripts").glob("*") if p.is_file() and not p.name.startswith(".")}
        if (directory / "scripts").is_dir() else set(),
    }
    return {key: actual[key] | set(manifest.items(key)) for key in actual}


def _installed_commands(repository: PackRepository, own: str) -> dict[str, str]:
    """包仓库里别的包（各个版本）声明的命令 → 包名；同名包的其他版本不算（升级沿用原来的命令）。"""
    taken: dict[str, str] = {}
    for name in repository.names():
        if name == own:
            continue
        for version in repository.versions(name):
            try:
                commands = repository.manifest(name, version).items("commands")
            except Exception:  # noqa: BLE001 - 坏掉的旧版本不影响检查别的包
                continue
            for command in commands:
                taken.setdefault(command, name)
    return taken


def _frontend_files(directory: Path, files: list[str], tenant: bool) -> tuple[list[str], list[str]]:
    """ui/ 之外的前端代码（ui/ 由 _ui_problems 单独检查）。"""
    hard, soft = [], []
    for rel in files:
        parts = Path(rel).parts
        if parts[0] == mf.UI_DIR:
            continue
        name = parts[-1]
        if name.lower().endswith(FRONTEND_SUFFIXES) or name in FRONTEND_NAMES or any(p in FRONTEND_DIRS for p in parts[:-1]):
            hard.append(rel)
        elif name.lower().endswith((".html", ".htm")):
            try:
                text = (directory / rel).read_text(encoding="utf-8", errors="ignore").lower()
            except OSError:
                continue
            if "<script" in text or "javascript:" in text:
                (hard if tenant else soft).append(rel)
    return hard, soft


def _view_editor_refs(directory: Path, manifest: Manifest) -> list[tuple[str, str]]:
    """视图声明里 `detail.editor` 引用的编辑器：[(视图文件, 编辑器 id)]。"""
    import yaml

    refs = []
    for rel in manifest.items("views"):
        try:
            data = yaml.safe_load((directory / rel).read_text(encoding="utf-8")) or {}
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            continue
        detail = data.get("detail") if isinstance(data, dict) else None
        editor = detail.get("editor") if isinstance(detail, dict) else None
        if isinstance(editor, str) and editor:
            refs.append((rel, editor))
    return refs


def _ui_problems(directory: Path, files: list[str], manifest: Manifest, *, official: bool) -> tuple[list[str], list[str]]:
    """包自带界面（ADR-0055）：编辑器入口要在、视图引用的编辑器要声明；上传包的 ui/ 只能是构建好的静态资源。

    官方包（镜像内置）的 ui/ 是随平台一起编译的源码，入口 ui/<id>/index.jsx；上传包的入口是 ui/<id>/index.html，
    只在不同源的 iframe 里跑。返回（失败项, 警告项）。
    """
    hard, soft = [], []
    ui_files = [rel for rel in files if Path(rel).parts[0] == mf.UI_DIR]
    entry_name = mf.EDITOR_ENTRY_BUNDLED if official else mf.EDITOR_ENTRY_IFRAME
    editors = manifest.editors()
    declared = {editor["id"] for editor in editors}
    for editor in editors:
        entry = f"{mf.UI_DIR}/{editor['id']}/{entry_name}"
        if entry not in files:
            hard.append(f"编辑器 {editor['id']} 缺少入口 {entry}")
        if editor["ui_api"] and not mf.satisfies(mf.UI_API, editor["ui_api"]):
            hard.append(f"编辑器 {editor['id']} 需要界面接口 {editor['ui_api']}，平台是 {mf.UI_API}")
    for view_rel, editor_id in _view_editor_refs(directory, manifest):
        if editor_id not in declared:
            hard.append(f"{view_rel} 引用的编辑器 {editor_id} 没有在 contributes.editors 里声明")
    if not official:
        for rel in ui_files:
            parts = Path(rel).parts
            name = parts[-1]
            if ".." in parts or any(part.startswith(".") for part in parts) or "node_modules" in parts:
                hard.append(f"{rel} 不能出现在 ui/ 里")
            elif name in FRONTEND_NAMES or not name.lower().endswith(UI_STATIC_SUFFIXES):
                hard.append(f"{rel} 不是构建好的静态资源（只能是 html、js、css、图片、字体）")
    if ui_files and not editors:
        soft.append("ui/ 没有被任何编辑器引用")
    return hard, soft


def _secrets(directory: Path, files: list[str]) -> list[str]:
    hits = []
    for rel in files:
        if not rel.lower().endswith(_TEXT_SUFFIXES):
            continue
        try:
            text = (directory / rel).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if any(pattern.search(text) for pattern in _SECRET_PATTERNS):
            hits.append(rel)
    return hits


def _syntax_errors(directory: Path, files: list[str]) -> list[str]:
    errors = []
    with tempfile.TemporaryDirectory(prefix="onto-pack-check-") as scratch:
        for rel in files:
            path = directory / rel
            if rel.endswith(".py"):
                try:
                    py_compile.compile(str(path), cfile=str(Path(scratch) / "x.pyc"), doraise=True)
                except py_compile.PyCompileError as exc:
                    errors.append(f"{rel}: {str(exc).splitlines()[-1][:120]}")
            elif rel.endswith(".sh"):
                try:
                    proc = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True, timeout=10)
                except (OSError, subprocess.TimeoutExpired):
                    continue
                if proc.returncode != 0:
                    errors.append(f"{rel}: {(proc.stderr or '').strip().splitlines()[-1:] or ['语法错误']}"[:160])
    return errors


def _touches_protected(pattern: str) -> bool:
    """按规范化后的完整路径判：带 `..`、绝对路径，或第一段是受保护目录（含 .git）都算碰。"""
    return mf.protected_path(str(pattern))


def _declared_nodes(directory: Path, manifest: Manifest) -> tuple[list[tuple[str, dict]], list[str]]:
    """包声明的流程节点：`contributes.flow` 文件里的 nodes，加上 `contributes.nodes` 里列的节点文件；
    nodes 里写节点编号（不是 .yaml 文件）时指 flow 文件里的同名节点。"""
    import yaml

    found: list[tuple[str, dict]] = []
    broken: list[str] = []
    flow_rel = manifest.contributes.get("flow")
    flow_nodes: dict[str, dict] = {}
    if isinstance(flow_rel, str) and (directory / flow_rel).is_file():
        try:
            data = yaml.safe_load((directory / flow_rel).read_text(encoding="utf-8")) or {}
            flow_nodes = {str(node.get("id")): node for node in data.get("nodes") or [] if isinstance(node, dict)}
        except (OSError, yaml.YAMLError):
            broken.append(f"{flow_rel} 无法解析")
    found += [(f"{flow_rel}#{node_id}", node) for node_id, node in flow_nodes.items()]
    for rel in manifest.items("nodes"):
        if not rel.endswith((".yaml", ".yml")):
            if rel not in flow_nodes:
                broken.append(f"节点 {rel} 不在 {flow_rel or 'flow.yaml'} 里")
            continue
        try:
            node = yaml.safe_load((directory / rel).read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            broken.append(f"{rel} 无法解析")
            continue
        found.append((rel, node))
    return found, broken


def _scope_patterns(node: dict) -> list[str]:
    """节点的写入范围：produces（`{path}` 或字符串）加 writes。"""
    out = []
    for item in [*(node.get("produces") or []), *(node.get("writes") or [])]:
        pattern = item.get("path") if isinstance(item, dict) else item
        if pattern:
            out.append(str(pattern))
    return out


def _output_paths(node: dict) -> list[tuple[str, str]]:
    out = []
    if isinstance(node.get("script"), dict) and node["script"].get("output") is not None:
        out.append(("script.output", str(node["script"]["output"])))
    if isinstance(node.get("decision"), dict) and node["decision"].get("record") is not None:
        out.append(("decision.record", str(node["decision"]["record"])))
    return out


def in_scope(target: str, patterns: list[str]) -> bool:
    """规范化后的路径不碰受保护目录，并且匹配某个（规范化后的）写入范围。"""
    rel = mf.workspace_rel(target)
    if rel is None or mf.protected_path(rel):
        return False
    return any(fnmatch.fnmatch(rel, normalized) for normalized in
               (mf.workspace_rel(pattern) for pattern in patterns) if normalized and not mf.protected_path(normalized))


def _node_problems(directory: Path, manifest: Manifest) -> tuple[list[str], list[str]]:
    """节点声明：写入范围不碰受保护目录；入口命令确实存在（详设 6.9）。"""
    protected, missing_commands = [], []
    commands = {f"/{name}" for name in declared_names(manifest, directory)["commands"]}
    nodes, broken = _declared_nodes(directory, manifest)
    missing_commands += broken
    for rel, node in nodes:
        protected += [f"{rel}: {pattern}" for pattern in _scope_patterns(node) if _touches_protected(pattern)]
        # 脚本节点的输出、人工节点的记录：规范化后要落在本节点的写入范围里（所有类型的包，审查第 1 条）
        for label, target in _output_paths(node):
            if not in_scope(target, _scope_patterns(node)):
                protected.append(f"{rel}: {label} {target} 不在节点的写入范围里，或碰到受保护目录")
        entry = (node.get("entry") or {}).get("command")
        if entry and entry.split()[0] not in commands:
            missing_commands.append(f"{rel}: {entry}")
    # 节点钩子（详设 6.4）的命令也要是本包的
    for hook in (_flow_data(directory, manifest) or {}).get("hooks") or []:
        command = str((hook or {}).get("command") or "") if isinstance(hook, dict) else ""
        if not command or command.split()[0] not in commands:
            missing_commands.append(f"节点钩子 {(hook or {}).get('id') if isinstance(hook, dict) else hook}: {command or '（没有命令）'}")
    return protected, missing_commands


# 节点包（kind: node，详设 6.9；用户 2026-11-02 定：能力包的特例）只带节点声明和节点要用的命令、子代理、
# 脚本、模板（参考指南随子代理一起允许）；hooks、skill、页面、产物登记、界面代码等一律不能带
NODE_PACK_ALLOWED = ("commands", "agents", "scripts", "templates", "reference", "nodes", "flow", "readme", "docs")
NODE_PLACEMENTS = ("step", "branch", "gate", "on_demand")


def _flow_probes(directory: Path, manifest: Manifest) -> set[str]:
    return set((_flow_data(directory, manifest) or {}).get("probes") or {})


def _core_probes(repository: PackRepository | None) -> set[str] | None:
    """stable core 的主流程探针（节点完成判定可以直接用）；没有包仓库时不检查。"""
    if repository is None:
        return None
    version = repository.latest("core", "stable")
    if not version:
        return None
    try:
        return _flow_probes(repository.version_dir("core", version), repository.manifest("core", version))
    except Exception:  # noqa: BLE001 - core 读不出来时不拿它挡检查
        return None


def _node_pack_problems(directory: Path, manifest: Manifest, files: list[str],
                        repository: PackRepository | None) -> list[str]:
    """kind: node 的附加检查：只带允许的组成；节点挂法、编号、四类节点各自的声明；不新增产物类别。"""
    from ..flow.state import KIND_DIRS

    problems = []
    for key, value in manifest.contributes.items():
        if key in NODE_PACK_ALLOWED or not value:
            continue
        if key == "hooks" and not manifest.hooks():
            continue
        problems.append(f"节点包不能带 contributes.{key}")
    if any(Path(rel).parts[0] in (mf.UI_DIR, "skills") for rel in files):
        problems.append("节点包不能带界面代码（ui/）或 skill（skills/）")
    if (directory / "settings.json").exists():
        problems.append("节点包不能带 settings.json")
    nodes, _broken = _declared_nodes(directory, manifest)
    if not nodes:
        problems.append("节点包至少要声明一个流程节点")
    own_commands = {f"/{name}" for name in manifest.items("commands")}
    own_scripts = set(manifest.items("scripts"))
    probes = _flow_probes(directory, manifest)
    core_probes = _core_probes(repository)
    for rel, node in nodes:
        node_id = str(node.get("id") or "")
        where = f"{rel}"
        if not node_id.startswith(f"{manifest.name}."):
            problems.append(f"{where}: 节点编号要以 {manifest.name}. 开头")
        placement = node.get("placement")
        if placement not in NODE_PLACEMENTS:
            problems.append(f"{where}: 挂法只能是 {' / '.join(NODE_PLACEMENTS)}（主线节点只能随基础建模能力发布）")
        if placement != "on_demand" and not (node.get("after") or node.get("before")):
            problems.append(f"{where}: 要写锚点 after 或 before")
        for key in ("stale_on", "requires"):
            for item in node.get(key) or []:
                kind = item.get("kind") if isinstance(item, dict) else item
                if str(kind) not in KIND_DIRS:
                    problems.append(f"{where}: {key} 里的 {kind} 不是已有的产物类别（节点包不能新增实体类型）")
        for name in node.get("done") or []:
            probe = str(name).lstrip("!")
            if probe not in probes and (core_probes is not None and probe not in core_probes):
                problems.append(f"{where}: 完成判定用到的探针 {probe} 没有声明")
        kind = node.get("kind", "agent")
        entry = str((node.get("entry") or {}).get("command") or "").split(" ")[0]
        if kind == "agent":
            if not entry:
                problems.append(f"{where}: agent 节点要有入口命令 entry.command")
            elif entry not in own_commands:
                problems.append(f"{where}: 入口命令 {entry} 不是本包声明的命令")
        elif kind == "script":
            script = node.get("script") if isinstance(node.get("script"), dict) else {}
            if str(script.get("name") or "") not in own_scripts:
                problems.append(f"{where}: 脚本节点要声明本包里的脚本 script.name")
            if not str(script.get("output") or ""):
                problems.append(f"{where}: 脚本节点要声明 script.output（落在 produces 里）")
        elif kind == "human":
            decision = node.get("decision") if isinstance(node.get("decision"), dict) else {}
            options = [item for item in decision.get("options") or [] if isinstance(item, dict) and item.get("id")]
            record = str(decision.get("record") or "")
            if not decision.get("title") or len(options) < 2:
                problems.append(f"{where}: 人工节点要有 decision.title 和至少两个选项")
            if not record:
                problems.append(f"{where}: 人工节点要声明 decision.record（落在 produces 里）")
        elif kind == "external":
            provider = node.get("provider") if isinstance(node.get("provider"), dict) else {}
            if not provider.get("kind"):
                problems.append(f"{where}: 外部节点要声明需要的数据接入插件 provider.kind")
    requires = str((_flow_data(directory, manifest) or {}).get("requires_flow") or "")
    if requires and repository is not None:
        spec = requires[len("core"):].strip() if requires.startswith("core") else None
        if spec is None:
            problems.append(f"requires_flow 只能约束 core：{requires}")
        elif not any(mf.satisfies(version, spec) for version in repository.versions("core")):
            problems.append(f"requires_flow {requires} 在包仓库里没有满足的基础建模能力")
    return problems


def _flow_data(directory: Path, manifest: Manifest) -> dict | None:
    import yaml

    flow_rel = manifest.contributes.get("flow")
    if not isinstance(flow_rel, str) or not (directory / flow_rel).is_file():
        return None
    try:
        data = yaml.safe_load((directory / flow_rel).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def run(directory: Path, repository: PackRepository | None = None, *, tenant: bool = False,
        core_names: dict[str, set[str]] | None = None, official: bool = False) -> tuple[Manifest | None, list[dict]]:
    """对解压后的包目录做全部静态检查，返回（清单或 None, 检查项列表）。

    core_names：当前 stable core 的命令、子代理、skill、脚本名，非 core 包不能与之重名。
    official：镜像内置的官方包（只有入库来源是 builtin 的才算，上传的包一律 False，清单里写了 official 也不算）。
    """
    directory = Path(directory)
    checks: list[dict] = []
    try:
        manifest = mf.load(directory)
        checks.append(_check("manifest", "清单格式与必填字段", "pass", f"{manifest.name} {manifest.version}"))
    except ManifestError as exc:
        checks.append(_check("manifest", "清单格式与必填字段", "fail", "；".join(exc.problems)))
        return None, checks

    problems = verify_checksums(directory)
    checks.append(_check("checksums", "校验和一致", "fail" if problems else "pass", "；".join(problems[:10])))
    links = symlinks(directory)
    checks.append(_check("symlinks", "不含符号链接", "fail" if links else "pass", "、".join(links[:10])))
    files = pack_files(directory)

    # 声明的命令、子代理、skill、脚本都要存在；有文件没声明给警告
    missing, undeclared = [], []
    for key, sub, suffix in (("commands", "commands", ".md"), ("agents", "agents", ".md"),
                             ("skills", "skills", ""), ("scripts", "scripts", None)):
        actual = (_names(directory, sub, suffix) if suffix is not None else
                  {p.name for p in (directory / sub).glob("*") if p.is_file()} if (directory / sub).is_dir() else set())
        declared = set(manifest.items(key))
        missing += [f"{key}/{name}" for name in sorted(declared - actual)]
        undeclared += [f"{key}/{name}" for name in sorted(actual - declared)]
    checks.append(_check(
        "declared", "声明的命令、子代理、脚本都存在",
        "fail" if missing else ("warn" if undeclared else "pass"),
        ("缺少：" + "、".join(missing[:10])) if missing else ("未声明：" + "、".join(undeclared[:10]) if undeclared else ""),
    ))

    # 兼容范围
    compat = []
    if not mf.satisfies(mf.KERNEL_API, manifest.kernel_api):
        compat.append(f"内核接口 {mf.KERNEL_API} 不满足 kernel_api {manifest.kernel_api}")
    if manifest.page_api and not mf.satisfies(mf.PAGE_API, manifest.page_api):
        compat.append(f"平台页面接口 {mf.PAGE_API} 不满足 page_api {manifest.page_api}")
    if repository is not None:
        for dep, spec in manifest.depends.items():
            if not any(mf.satisfies(version, spec) for version in repository.versions(dep)):
                compat.append(f"依赖 {dep} {spec} 在包仓库里没有满足的版本")
    checks.append(_check("compat", "兼容范围对得上", "fail" if compat else "pass", "；".join(compat)))

    # 名字：租户包与非 core 包的命令要带包前缀，不能和 core 重名，不能占用内核脚本名
    conflicts = []
    names = declared_names(manifest, directory)
    if manifest.name != "core":
        prefix = f"{manifest.name}-"
        if tenant or not official:
            conflicts += [f"命令 {name} 没有包前缀 {prefix}" for name in sorted(names["commands"]) if not name.startswith(prefix)]
        for key, existing in (core_names or {}).items():
            conflicts += [f"{key} 与基础建模能力重名：{name}" for name in sorted(names.get(key, set()) & existing)]
    if tenant and manifest.name == "core":
        conflicts.append("租户包不能使用 core 这个名字")
    elif not official and manifest.name in RESERVED_PACK_NAMES and manifest.name != "core":
        conflicts.append(f"{manifest.name} 是保留的包名")
    if repository is not None:
        taken = _installed_commands(repository, manifest.name)
        conflicts += [f"命令 {name} 和已入库的 {taken[name]} 重名" for name in sorted(names["commands"]) if name in taken]
    conflicts += [f"脚本 {name} 是内核保留名" for name in sorted(names["scripts"])
                  if name.startswith(_KERNEL_SCRIPT_PREFIXES) or name.endswith("_guard.sh")]
    if repository is not None and repository.has(manifest.name, manifest.version):
        conflicts.append(f"{manifest.name} {manifest.version} 已在包仓库里")
    checks.append(_check("names", "名字不冲突", "fail" if conflicts else "pass", "；".join(conflicts[:10])))

    # 没有新增实体类型：插件产物只能带包名前缀、不能和五元组重名，放在 ext/<包名>/ 下（详设 5.9.4）
    from ...kernel.artifacts import registry as artifact_registry

    entity = []
    for rel in manifest.items("artifacts"):
        entity += artifact_registry.check_registration(directory, rel, manifest, official=official and not tenant)
    checks.append(_check("entity_types", "没有新增实体类型", "fail" if entity else "pass", "；".join(entity[:10])))
    views = artifact_registry.check_views(directory, manifest)
    checks.append(_check("views", "视图声明格式正确", "fail" if views else "pass", "；".join(views[:10])))

    if manifest.kind == "node":
        node_pack = _node_pack_problems(directory, manifest, files, repository)
        checks.append(_check("node_pack", "节点包只带节点与节点要用的命令、子代理、脚本、模板",
                             "fail" if node_pack else "pass", "；".join(node_pack[:10])))

    protected, missing_entry = _node_problems(directory, manifest)
    checks.append(_check("write_scope", "写入范围不碰受保护目录", "fail" if protected else "pass", "；".join(protected[:10])))
    checks.append(_check("node_entry", "节点入口命令存在", "fail" if missing_entry else "pass", "；".join(missing_entry[:10])))

    # 守卫：只能追加拒绝型（PreToolUse），不能带自己的 settings.json
    hooks = []
    if (directory / "settings.json").exists():
        hooks.append("包不能带 settings.json：权限与守卫基线由内核注入，包只能在清单 hooks.PreToolUse 里追加")
    hooks += [f"守卫命令引用的脚本不存在：{item.get('command')}" for item in manifest.hooks()
              if not any(script in str(item.get("command")) for script in names["scripts"])]
    checks.append(_check("hooks", "hooks 只追加拒绝型", "fail" if hooks else "pass", "；".join(hooks)))

    # 页面只选平台内置的，不带前端代码
    pages = [f"页面 {page.get('id')} 不是平台内置页面" for page in manifest.pages()
             if page.get("id") not in mf.BUILTIN_PAGES]
    pages += [f"子页挂在非内核页面 {sub.get('parent')}" for sub in manifest.subpages()
              if sub.get("parent") not in mf.KERNEL_PAGES]
    pages += [f"子页 {sub.get('id')} 不是平台内置子页" for sub in manifest.subpages()
              if sub.get("id") not in mf.BUILTIN_SUBPAGES]
    pages += [f"页面 {page.get('id')} 的图标 {(page.get('nav') or {}).get('icon')} 不在平台图标集里"
              for page in manifest.pages() if (page.get("nav") or {}).get("icon") not in (None, *mf.NAV_ICONS)]
    frontend, frontend_soft = _frontend_files(directory, files, tenant)
    if frontend:
        pages.append("ui/ 之外有前端代码：" + "、".join(frontend[:10]))
    checks.append(_check(
        "frontend", "页面只选用平台内置，前端代码只在 ui/ 下",
        "fail" if pages else ("warn" if frontend_soft else "pass"),
        "；".join(pages) if pages else ("报告模板含脚本：" + "、".join(frontend_soft[:5]) if frontend_soft else ""),
    ))
    ui_hard, ui_soft = _ui_problems(directory, files, manifest, official=official and not tenant)
    checks.append(_check(
        "ui", "包自带界面：上传包只能是静态资源、经隔离 iframe 加载",
        "fail" if ui_hard else ("warn" if ui_soft else "pass"),
        "；".join(ui_hard[:10]) if ui_hard else "；".join(ui_soft),
    ))

    from . import extensions

    if extensions.declares_any(manifest):
        declared = extensions.problems(manifest, repository)
        checks.append(_check("extensions", "扩展项声明格式正确", "fail" if declared else "pass", "；".join(declared[:10])))

    secrets = _secrets(directory, files)
    checks.append(_check("secrets", "不含凭据", "fail" if secrets else "pass",
                         ("疑似凭据：" + "、".join(secrets[:10])) if secrets else ""))

    syntax = _syntax_errors(directory, [rel for rel in files if rel.startswith("scripts/")])
    checks.append(_check("syntax", "脚本语法", "fail" if syntax else "pass", "；".join(syntax[:5])))

    prompt_bytes = sum(
        (directory / rel).stat().st_size for rel in files
        if rel.split("/")[0] in ("commands", "agents", "skills") or rel in manifest.items("rules")
    )
    budget = "fail" if prompt_bytes > PROMPT_HARD_BYTES else ("warn" if prompt_bytes > PROMPT_SOFT_BYTES else "pass")
    checks.append(_check("prompt_budget", "提示词体积预算", budget, f"{prompt_bytes // 1024} KiB"))
    return manifest, checks


def summary(checks: list[dict]) -> dict:
    return {result: sum(1 for item in checks if item["result"] == result) for result in ("pass", "warn", "fail")}


def report_markdown(title: str, checks: list[dict]) -> str:
    marks = {"pass": "通过", "warn": "警告", "fail": "失败"}
    lines = [f"# 静态检查报告：{title}", "", "| 检查项 | 结果 | 说明 |", "|---|---|---|"]
    for item in checks:
        detail = str(item.get("detail") or "").replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {item['label']} | {marks.get(item['result'], item['result'])} | {detail} |")
    return "\n".join(lines) + "\n"
