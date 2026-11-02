"""5.6 扩展项里 B5c 补齐的几类（详设 5.6；ADR-0068）：

| 扩展项 | 清单里怎么写 | 谁来消费 |
|---|---|---|
| 界面入口 | `entries: [{id, kind: quick_action\\|extra_action, label, command, where}]` | PL-12 `entries`；数据源连接向导随证据源留空，不出现 |
| 声明式门禁 | `gates: [{id, label, artifact, on: [save, commit, publish], level, rules, min_count, ratio}]` | 保存（AR-05）、写入事务提交与合入（㉖）、发布门禁链（PB-01） |
| 视图选择 | `view_choices: [er_view \\| schema_browser \\| evidence_sidebar]` | PL-12 `view_choices`；依赖证据源的两种本期不出现 |
| 配置项 | `config: [{key, label, type, default, min, max, choices}]` | 配置注册表（来源 pack，键 `<包名>.<key>`） |
| 指标与告警规则 | `metrics: [{name, help, type}]`、`alerts: [{id, name, condition, window, level, runbook}]` | 可观测：指标说明进 /metrics，规则进 O-08 |
| 只读 MCP 工具 | `mcp_tools: [{name, title, artifact, mode: list\\|get}]` | 本项目的 MCP 读工具集，按本包产物类型列表或取一项 |

只读包仓库里不可修改的版本目录，不跑包的代码。节点钩子写在包的 flow.yaml（`hooks`），由流程组装检查。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from .manifest import Manifest

_log = logging.getLogger(__name__)

ENTRY_KINDS = ("quick_action", "extra_action", "source_wizard")
# 内核内置、包只能选用的视图（详设 5.6「视图选择」）；值为真表示依赖证据源（本期只留接口，ADR-0056）
KERNEL_VIEWS = {"er_view": False, "schema_browser": True, "evidence_sidebar": True}
GATE_RULES = ("required", "in", "max_length", "ref_exists")
GATE_LEVELS = ("error", "warning")
CONFIG_TYPES = {"str": "str", "int": "int", "bool": "bool", "enum": "enum"}
ALERT_LEVELS = ("critical", "warning", "info")
METRIC_TYPES = ("counter", "gauge", "histogram")
MCP_MODES = ("list", "get")
_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
_METRIC_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _declared(manifest: Manifest, key: str) -> list:
    value = manifest.contributes.get(key) or []
    return value if isinstance(value, list) else []


def own_type(manifest: Manifest, art_type) -> bool:
    return isinstance(art_type, str) and art_type.startswith(f"{manifest.name}.")


# ---------------------------------------------------------------------------
# 静态检查
# ---------------------------------------------------------------------------


# 包名与工具名、指标名之间的分隔：包名（小写字母、数字、连字符）里不会有下划线，名字里不许写「__」，生成的全名
# 不会和内置工具（没有「__」）或别的包撞名（审查第 8、9 条）
SEPARATOR = "__"


def _taken_names(repository, own: str) -> tuple[set[str], set[str]]:
    """包仓库里别的包（stable 版本）已经占用的 MCP 工具全名与指标全名。"""
    tools, metrics = set(), set()
    if repository is None:
        return tools, metrics
    for name in repository.names():
        version = repository.latest(name, "stable")
        if name == own or not version:
            continue
        try:
            manifest = repository.manifest(name, version)
        except Exception:  # noqa: BLE001 - 坏掉的包不影响检查别的包
            continue
        tools |= {mcp_tool_name(name, str(item.get("name") or item.get("id") or "")) for item in manifest.declared("mcp_tools")}
        metrics |= {metric_name(name, str(item.get("name") or item.get("id") or "")) for item in manifest.declared("metrics")}
    return tools, metrics


def problems(manifest: Manifest, repository=None) -> list[str]:
    """这几类扩展项的声明格式（包仓库静态检查 `extensions` 项）；给了包仓库时再查工具名、指标名和别的包撞没撞。"""
    out: list[str] = []
    taken_tools, taken_metrics = _taken_names(repository, manifest.name)
    from ..onto_mcp.definitions import TOOLS
    commands = {f"/{name}" for name in manifest.items("commands")}
    for item in manifest.declared("entries"):
        kind = item.get("kind", "quick_action")
        if kind not in ENTRY_KINDS:
            out.append(f"界面入口 {item.get('id')} 的 kind 只能是 {' / '.join(ENTRY_KINDS)}")
        command = str(item.get("command") or "").split(" ")[0]
        if kind != "source_wizard" and command not in commands:
            out.append(f"界面入口 {item.get('id')} 的命令 {command or '（空）'} 不是本包的命令")
    for gate in manifest.declared("gates"):
        where = f"门禁 {gate.get('id') or '（空）'}"
        if not gate.get("id"):
            out.append(f"{where} 缺少 id")
        if not own_type(manifest, gate.get("artifact")):
            out.append(f"{where} 只能管本包登记的产物类型（{manifest.name}.…）")
        scopes = gate.get("on") or ["save"]
        scopes = [scopes] if isinstance(scopes, str) else scopes
        if not isinstance(scopes, list) or any(scope not in ("save", "commit", "publish") for scope in scopes):
            out.append(f"{where} 的 on 只能取 save、commit、publish")
        if gate.get("level", "error") not in GATE_LEVELS:
            out.append(f"{where} 的 level 只能是 error 或 warning")
        for rule in gate.get("rules") or []:
            if not isinstance(rule, dict) or not rule.get("field") or not any(key in rule for key in GATE_RULES):
                out.append(f"{where} 的规则要有 field 和 {' / '.join(GATE_RULES)} 之一")
        ratio = gate.get("ratio")
        if ratio is not None and (not isinstance(ratio, dict) or not ratio.get("field") or not ratio.get("in")
                                  or not 0 < _number(ratio.get("at_least")) <= 1):
            out.append(f"{where} 的 ratio 要有 field、in 和 0～1 之间的 at_least")
        if gate.get("min_count") is not None and (not isinstance(gate["min_count"], int) or gate["min_count"] < 0):
            out.append(f"{where} 的 min_count 要是非负整数")
        if (gate.get("min_count") is not None or ratio is not None) and "publish" not in scopes:
            out.append(f"{where} 的 min_count、ratio 只在发布前检查（on 要含 publish）")
    for choice in _declared(manifest, "view_choices"):
        if choice not in KERNEL_VIEWS:
            out.append(f"视图 {choice} 不是内核内置视图（{' / '.join(KERNEL_VIEWS)}）")
    for item in manifest.declared("config"):
        key = str(item.get("key") or item.get("id") or "")
        if not _ID_RE.match(key):
            out.append(f"配置项 {key or '（空）'} 的 key 只能是小写字母开头的字母、数字、下划线、连字符")
        if str(item.get("type") or "str") not in CONFIG_TYPES:
            out.append(f"配置项 {key} 的 type 只能是 {' / '.join(CONFIG_TYPES)}")
        if item.get("type") == "enum" and not item.get("choices"):
            out.append(f"配置项 {key} 是 enum，要写 choices")
    for item in manifest.declared("metrics"):
        name = str(item.get("name") or item.get("id") or "")
        if not _METRIC_RE.match(name) or SEPARATOR in name:
            out.append(f"指标 {name or '（空）'} 的名字只能是小写字母、数字、单个下划线")
        elif metric_name(manifest.name, name) in taken_metrics:
            out.append(f"指标 {metric_name(manifest.name, name)} 已被别的包占用")
        if str(item.get("type") or "gauge") not in METRIC_TYPES:
            out.append(f"指标 {name} 的 type 只能是 {' / '.join(METRIC_TYPES)}")
    for item in manifest.declared("alerts"):
        if not item.get("id") or not item.get("condition"):
            out.append(f"告警规则 {item.get('id') or '（空）'} 要有 id 和 condition")
        if str(item.get("level") or "warning") not in ALERT_LEVELS:
            out.append(f"告警规则 {item.get('id')} 的 level 只能是 {' / '.join(ALERT_LEVELS)}")
        if item.get("auto_action") not in (None, "breaker"):
            out.append(f"告警规则 {item.get('id')} 的 auto_action 只能是 breaker")
    for item in manifest.declared("mcp_tools"):
        name = str(item.get("name") or item.get("id") or "")
        full = mcp_tool_name(manifest.name, name)
        if not _ID_RE.match(name) or SEPARATOR in name:
            out.append(f"MCP 工具 {name or '（空）'} 的名字只能是小写字母开头的字母、数字、单个下划线、连字符")
        elif full in TOOLS or full in taken_tools:
            out.append(f"MCP 工具 {full} 和内置工具或别的包重名")
        if not own_type(manifest, item.get("artifact")):
            out.append(f"MCP 工具 {name} 只能读本包登记的产物类型（{manifest.name}.…）")
        if str(item.get("mode") or "list") not in MCP_MODES:
            out.append(f"MCP 工具 {name} 的 mode 只能是 list 或 get（只读）")
    return out


def _number(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def declares_any(manifest: Manifest) -> bool:
    return any(manifest.contributes.get(key) for key in
               ("entries", "gates", "view_choices", "config", "metrics", "alerts", "mcp_tools"))


# ---------------------------------------------------------------------------
# 运行时：PL-12 的界面入口与视图选择
# ---------------------------------------------------------------------------


def entries(view) -> list[dict]:
    """包的界面入口：快捷项、附加操作；命令要是本包的。数据源连接向导随证据源留空（ADR-0056），不出现。"""
    commands = {f"/{name}" for name in view.manifest.items("commands")}
    out = []
    for index, entry in enumerate(view.manifest.declared("entries")):
        kind = entry.get("kind") if entry.get("kind") in ENTRY_KINDS else "quick_action"
        command = str(entry.get("command") or "")
        if kind == "source_wizard" or command.split(" ")[0] not in commands:
            continue
        out.append({"id": str(entry.get("id") or f"{view.name}-{index + 1}"), "kind": kind,
                    "label": str(entry.get("label") or entry.get("id") or ""),
                    "where": str(entry.get("where") or "wb-l2"), "pack": view.name, "command": command})
    return out


def view_choices(view) -> list[dict]:
    from ...evidence import available_providers

    evidence = bool(available_providers())
    return [{"id": choice, "pack": view.name} for choice in _declared(view.manifest, "view_choices")
            if choice in KERNEL_VIEWS and (evidence or not KERNEL_VIEWS[choice])]


def gate_view(view, gate: dict) -> dict:
    from ...kernel.artifacts.service import gate_scopes

    return {"id": str(gate.get("id") or ""), "label": str(gate.get("label") or gate.get("id") or ""),
            "pack": view.name, "on": gate_scopes(gate)}


# ---------------------------------------------------------------------------
# 配置项 → 配置注册表
# ---------------------------------------------------------------------------


def config_key(pack: str, key: str) -> str:
    return f"{pack}.{key}"


def config_items(manifest: Manifest) -> list:
    from ...governance.config_registry import ConfigItem

    out = []
    for item in manifest.declared("config"):
        key = str(item.get("key") or item.get("id") or "")
        if not _ID_RE.match(key):
            continue
        kind = CONFIG_TYPES.get(str(item.get("type") or "str"), "str")
        default = item.get("default")
        out.append(ConfigItem(
            key=config_key(manifest.name, key), default="" if default is None else default, kind=kind,
            effect="next_message", description=str(item.get("label") or item.get("description") or key),
            minimum=item.get("min"), maximum=item.get("max"),
            choices=tuple(str(choice) for choice in item.get("choices") or ()),
            precedence=("pack", "default"), pack=manifest.name, pack_value=default,
        ))
    return out


def sync_config(manager) -> None:
    """包仓库里各包 stable 版本声明的配置项登记进配置注册表（启动、发布新版本后调）。"""
    from ...governance import config_registry

    manager.packs.core_version()
    repository = manager.packs.repository
    for name in repository.names():
        version = repository.latest(name, "stable")
        if not version:
            continue
        try:
            items = config_items(repository.manifest(name, version))
        except Exception:  # noqa: BLE001 - 一个包坏了不挡别的包
            _log.warning("读取包 %s 的配置项失败", name, exc_info=True)
            continue
        config_registry.unregister_pack(name)
        for item in items:
            try:
                config_registry.register(item, name)
            except ValueError as exc:
                _log.warning("包 %s 的配置项没有登记：%s", name, exc)


# ---------------------------------------------------------------------------
# 指标与告警规则 → 可观测
# ---------------------------------------------------------------------------


def metric_name(pack: str, name: str) -> str:
    """包的指标统一带前缀，不和内核指标重名；包名与指标名之间用「__」分开（见 SEPARATOR）。"""
    return f"onto_pack_{pack.replace('-', '_')}{SEPARATOR}{name}"


def declared_metrics(manager) -> list[dict]:
    """各包 stable 版本声明的指标，按全名去重（先到先得）。不按「有项目启用」筛：/metrics 是平台级的，
    启用是项目级的，每次抓取都扫一遍项目锁文件不划算；没有样本的 HELP / TYPE 不影响抓取。"""
    out = []
    seen: set[str] = set()
    repository = manager.packs.repository
    for name in repository.names():
        version = repository.latest(name, "stable")
        if not version:
            continue
        for item in repository.manifest(name, version).declared("metrics"):
            metric = str(item.get("name") or item.get("id") or "")
            full = metric_name(name, metric)
            if _METRIC_RE.match(metric) and SEPARATOR not in metric and full not in seen:
                seen.add(full)
                out.append({"name": full, "help": str(item.get("help") or metric),
                            "type": str(item.get("type") or "gauge"), "pack": f"{name}@{version}"})
    return out


def declared_alerts(manager) -> list[dict]:
    """各包 stable 版本声明的告警规则（契约 7.16 AlertRule 的形状，source 是包版本）。"""
    out = []
    repository = manager.packs.repository
    for name in repository.names():
        version = repository.latest(name, "stable")
        if not version:
            continue
        for item in repository.manifest(name, version).declared("alerts"):
            if not item.get("id") or not item.get("condition"):
                continue
            out.append({"id": f"{name}.{item['id']}", "name": str(item.get("name") or item["id"]),
                        "condition": str(item["condition"]), "window": str(item.get("window") or "5m"),
                        "level": str(item.get("level") or "warning"), "runbook_md": str(item.get("runbook") or ""),
                        "auto_action": item.get("auto_action") if item.get("auto_action") == "breaker" else None,
                        "source": f"{name}@{version}"})
    return out


# ---------------------------------------------------------------------------
# 只读 MCP 工具
# ---------------------------------------------------------------------------


def mcp_tool_name(pack: str, name: str) -> str:
    return f"{pack}{SEPARATOR}{name}"


def mcp_tools(manager, project_id: str) -> list[dict]:
    """本项目生效中的包声明的只读 MCP 工具：按本包的产物类型列表（list）或取一项（get）。"""
    from . import registry

    try:
        root = manager.project_root(project_id)
        views = registry.active_packs(manager, root)
    except Exception:  # noqa: BLE001 - 项目不在了：没有包工具
        return []
    from ..onto_mcp.definitions import TEXT, TOOLS, definition

    out = []
    seen: set[str] = set(TOOLS)
    for view in views:
        for item in view.manifest.declared("mcp_tools"):
            name = str(item.get("name") or item.get("id") or "")
            if not _ID_RE.match(name) or SEPARATOR in name or not own_type(view.manifest, item.get("artifact")):
                continue
            if mcp_tool_name(view.name, name) in seen:  # 和内置或前面的包重名：不列（上传检查已经挡住）
                continue
            seen.add(mcp_tool_name(view.name, name))
            mode = str(item.get("mode") or "list")
            props = ({"filters": {"type": "object"}, "query": TEXT, "cursor": TEXT,
                      "limit": {"type": "integer", "minimum": 1, "maximum": 50}} if mode == "list" else {"key": TEXT})
            tool = definition(mcp_tool_name(view.name, name), str(item.get("title") or name), props,
                              [] if mode == "list" else ["key"])
            out.append({**tool, "pack": view.name, "artifact": str(item["artifact"]), "mode": mode})
    return out


def find_mcp_tool(manager, project_id: str, name: str) -> dict | None:
    return next((tool for tool in mcp_tools(manager, project_id) if tool["name"] == name), None)


# ---------------------------------------------------------------------------
# 声明式门禁：写入事务（commit）与发布门禁链（publish）
# ---------------------------------------------------------------------------

# 这些来源的提交不查包的门禁：回退与撤回恢复的是以前的样子，插件变更与导入不是在改产物
_GATE_SKIP_SOURCES = ("revert", "plugin", "import")


def _gated_types(manager, root: Path, scope: str):
    """生效中的包里在 scope 时查的门禁：[(包, 门禁, 产物类型)]。"""
    from ...kernel.artifacts import registry as artifact_registry
    from ...kernel.artifacts.service import gate_scopes
    from . import registry

    out = []
    types = {item.type: item for item in artifact_registry.types_for_project(manager, root)}
    for view in registry.desired_packs(manager, root):
        for gate in view.manifest.declared("gates"):
            atype = types.get(str(gate.get("artifact") or ""))
            if atype is not None and atype.pack == view.name and scope in gate_scopes(gate):
                out.append((view, gate, atype))
    return out


def _item_findings(root: Path, view, gate: dict, atype, rows: list[dict]) -> list[dict]:
    from ...kernel.artifacts.service import rule_problem

    level = gate.get("level") if gate.get("level") in GATE_LEVELS else "error"
    out = []
    for row in rows:
        for rule in gate.get("rules") or []:
            problem = rule_problem(root, atype, rule, row.get("fields") or {})
            if problem:
                out.append({"code": "pack_gate", "level": level, "gate": str(gate.get("id") or ""), "pack": view.name,
                            "message": str(rule.get("message") or f"{row.get('title') or row['key']}：{problem}"),
                            "target": {"kind": "artifact", "type": atype.type, "key": row["key"]}, "path": row["rel"]})
    return out


def commit_findings(manager, project_id: str, root: Path, paths: list[str], origin) -> list[dict]:
    """写入事务提交前（ChangeTx 登记的门禁检查）：这次改到的、门禁 on 含 commit 的产物逐项查。"""
    from ...kernel.artifacts import index

    if getattr(origin, "source", "") in _GATE_SKIP_SOURCES:
        return []
    out = []
    for view, gate, atype in _gated_types(manager, Path(root), "commit"):
        rels = [rel for rel in paths if atype.key_from_path(rel) is not None]
        if rels:
            out += _item_findings(Path(root), view, gate, atype, index.rows_of(atype, Path(root), rels))
    return out


def _export(root: Path, commit: str, dirs: list[str], target: Path) -> None:
    """把提交里这几个目录导出到 target（插件产物不在发布用的只读树里，同一个提交另导一份）。"""
    import subprocess
    import tarfile

    listed = subprocess.run(["git", "-c", "core.quotepath=false", "ls-tree", "--name-only", "-z", commit, "--", *dirs],
                            cwd=root, capture_output=True, timeout=60)
    present = [item for item in listed.stdout.decode("utf-8", "replace").split("\0") if item]
    if not present:
        return
    archive = target.parent / "gates.tar"
    subprocess.run(["git", "archive", "--format=tar", "-o", str(archive), commit, "--", *present], cwd=root,
                   check=True, capture_output=True, timeout=120)
    with tarfile.open(archive) as tar:
        tar.extractall(target, filter="data")


def publish_gates(manager, root: Path, commit: str, start_order: int) -> list[dict]:
    """发布门禁链里包声明的门禁（契约 7.14 GateResult）：按发布的那个提交查，按包、门禁各一道。"""
    import tempfile

    gated = _gated_types(manager, Path(root), "publish")
    if not gated:
        return []
    with tempfile.TemporaryDirectory(prefix="onto-pack-gates-") as scratch:
        tree = Path(scratch) / "tree"
        tree.mkdir()
        # 本体目录一起导出：ref_exists 要在同一个提交里找被引用的条目（审查第 2 条）
        dirs = {atype.glob_root() or "." for _view, _gate, atype in gated} | {"ontology"}
        _export(Path(root), commit, sorted(dirs), tree)
        return _publish_results(gated, tree, start_order)


def _publish_results(gated: list, tree: Path, start_order: int) -> list[dict]:
    from ...kernel.artifacts import index

    out = []
    for view, gate, atype in gated:
        rows = index.rows_of(atype, Path(tree), index._files(atype, Path(tree)))
        findings = _item_findings(Path(tree), view, gate, atype, rows)
        level = gate.get("level") if gate.get("level") in GATE_LEVELS else "error"
        label = str(gate.get("label") or gate.get("id") or "")
        if isinstance(gate.get("min_count"), int) and len(rows) < gate["min_count"]:
            findings.append({"code": "pack_gate", "level": level, "gate": str(gate.get("id") or ""), "pack": view.name,
                             "message": f"{atype.title}至少要 {int(gate['min_count'])} 个，现在 {len(rows)} 个"})
        ratio = gate.get("ratio") if isinstance(gate.get("ratio"), dict) else None
        if ratio and rows:
            allowed = {str(item) for item in ratio.get("in") or []}
            hit = sum(1 for row in rows if str((row.get("fields") or {}).get(ratio["field"])) in allowed)
            if hit / len(rows) < _number(ratio.get("at_least")):
                findings.append({"code": "pack_gate", "level": level, "gate": str(gate.get("id") or ""),
                                 "pack": view.name, "message": f"{label}：{hit}/{len(rows)}，"
                                 f"要求至少 {round(_number(ratio.get('at_least')) * 100)}%"})
        failed = bool(findings)
        out.append({
            "id": f"{view.name}.{gate.get('id')}", "order": start_order + len(out), "title": label,
            "source": {"kind": "pack", "label": view.manifest.label, "pack": view.name},
            "status": ("fail" if level == "error" else "warn") if failed else "pass",
            "summary": findings[0]["message"] if failed else f"{atype.title}符合「{label}」",
            "blocking": failed and level == "error", "degradable": False, "findings": findings[:50], "action": None,
        })
    return out
