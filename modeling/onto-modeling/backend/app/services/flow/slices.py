"""按段的节点状态（详设 6.6「按段的状态」、6.8、4.3；方案 D13；契约 FL-01 `slices`、`slice_stages`、7.8）。

建模节点（`per_slice: true`，现在是 `core.model`）在建模蓝图的每一段上各算一份状态。段里的步骤来自节点声明的
`steps`（core 的对象、操作、规则、写入即查）加上包的钩子（场景走查的「场景」）。

一段一步的状态：

| 状态 | 怎么算 |
|---|---|
| running | 有没结束的执行记录，分段与步骤是这一段这一步 |
| pending_confirm | 没确认的变更集里执行过这一段这一步，或改到了这一段这一步的条目 |
| confirmed | 这一段这一步的条目在主线上都有了，或这一步的变更集合入过（记下了）；需复核（自主构建）显示 auto |
| stale | 确认之后，这一步 `stale_on` 的上游（这一段的条目、资料分析）在主线上变了 |
| skipped | 可选步骤（规则）被跳过，或按段模式下这一段没规划规则（资料里没有就跳过，不挡这一段确认） |
| ready / not_ready | 前面的必经步骤都做了就可开始 |

「写入即查」不单独跑命令：每轮合入时的检查（ADR-0046，F3 起只提示）就是写入时的检查，前面的步骤都确认了它就确认；
这一段还没确认的修改有新错误时是 draft。

条目归属：按段模式下取蓝图里这一段的对象、操作、规则（一轮结束时这一段新建的条目由后端记进这一段）；
全量模式（没有蓝图或只有一段）是一段「全部」，条目就是整类目录。

依据存在节点状态同一份文件（`flow-state/<项目>.json`）的 `slices`、`slice_skips` 两项；第一次看到已经满足的
步骤（存量项目）直接记基线，不算过期。0.2 的节点（对象提取、操作提取、规则与指标）上的已确认状态在第一次算时
映射到全量模式那一段（`migrate_legacy`），进度不往回退。
"""

from __future__ import annotations

import copy
import hashlib
import json
import time
from pathlib import Path

from . import state as node_state

FULL_SLICE = {"id": "all", "name": "全部", "core_flow": ""}
DONE = ("confirmed", "auto")
# 步骤的条目类别 → 目录（操作含旧目录）；testcase 是场景走查的场景文件，一个操作一份
KIND_DIRS = {"object": ("ontology/object",), "operation": ("ontology/operation", "ontology/logic"),
             "rule": ("ontology/rule",), "metric": ("ontology/metric",), "testcase": ("ontology/testcase",)}
LEGACY = {"core.object": "object", "core.operation": "operation", "core.rule_metric": "rule"}


def per_slice_node(flow: dict) -> dict | None:
    return next((node for node in flow.get("nodes") or [] if node.get("per_slice")), None)


def stage_defs(node: dict) -> list[dict]:
    """契约 FL-01 `slice_stages`：插件加的步骤带 `plugin`。"""
    out = []
    for step in node.get("steps") or []:
        item = {"id": step["id"], "label": step["label"]}
        if step.get("pack") and step["pack"] != node.get("pack", "core"):
            item["plugin"] = step["pack"]
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# 输入
# ---------------------------------------------------------------------------


class _Inputs:
    """一次计算用到的东西：主线的树（一次 ls-tree）、蓝图、执行记录、没确认的变更集。"""

    def __init__(self, root: Path, flow: dict, node: dict, doc: dict | None, project_id: str):
        from . import blueprint

        self.root = Path(root)
        self.flow = flow
        self.node = node
        self.module = blueprint.script(self.root)
        self.doc = doc
        self.sliced = self.module.mode(doc) == "sliced"
        self.slices = self.module.live_slices(doc) if self.sliced else [dict(FULL_SLICE)]
        # 全量模式下包加的步骤（场景）不挡建模完成：全量回归另有主线节点（trace.trace），和 0.2 的进度口径一致
        self.steps = [step if self.sliced or step.get("pack", "core") == node.get("pack", "core")
                      else {**step, "optional": True} for step in node.get("steps") or []]
        listing = node_state.tree_listing(self.root)
        self.lines = listing
        self.paths = {line.split("\t", 1)[1] for line in listing if "\t" in line}
        self.running = _running(project_id, node["id"])
        self.record = _open_changeset(self.root)
        self.changed = set((self.record or {}).get("paths") or [])
        self.pending_steps = {tuple(pair) for pair in (self.record or {}).get("slice_steps") or []
                              if isinstance(pair, (list, tuple)) and len(pair) == 2}

    # ---- 条目 ----

    def _entity_paths(self, item: dict, kind: str) -> list[list[str]]:
        """一段里某一类的条目，每个条目可能的路径（操作在新旧两个目录）。"""
        from ...kernel import metamodel

        model = metamodel.mm
        if kind == "testcase":
            names = item.get("operations") or []
            return [[f"ontology/testcase/{model.encode_filename(name)}.md"] for name in names]
        field = self.module.MEMBER_FIELDS.get(kind)
        if not field:
            return []
        entity = model.BY_KIND[kind]
        return [[f"{directory}/{model.encode_filename(name)}{entity.suffix}" for directory in entity.directories]
                for name in item.get(field) or []]

    def _under(self, kind: str, rel: str) -> bool:
        return any(rel.startswith(f"{directory}/") and rel.endswith(".md")
                   and not rel.rsplit("/", 1)[-1].lower() == "index.md" for directory in KIND_DIRS.get(kind, ()))

    def present(self, item: dict, step: dict) -> bool:
        kinds = list(step.get("kinds") or [])
        if not self.sliced:
            return bool(kinds) and all(any(self._under(kind, rel) for rel in self.paths) for kind in kinds)
        groups = [paths for kind in kinds for paths in self._entity_paths(item, kind)]
        return bool(groups) and all(any(rel in self.paths for rel in paths) for paths in groups)

    def planned(self, item: dict, step: dict) -> bool:
        return any(self._entity_paths(item, kind) for kind in step.get("kinds") or [])

    def touches(self, item: dict, step: dict) -> bool:
        """没确认的变更集改到了这一段这一步的条目。"""
        if not self.changed:
            return False
        kinds = list(step.get("kinds") or [])
        if not self.sliced:
            return any(self._under(kind, rel) for kind in kinds for rel in self.changed)
        return any(rel in self.changed for kind in kinds for paths in self._entity_paths(item, kind) for rel in paths)

    def fingerprint(self, item: dict, step: dict) -> str:
        """这一步上游（stale_on）在主线上的摘要：按段模式下对象、操作、规则只取这一段的条目。

        行按 ls-tree 的顺序取：全量模式下和节点的输入指纹（`state.fingerprint`）算法相同，0.2 的记录能接着用。"""
        kinds = [str(kind) for kind in step.get("stale_on") or []]
        if not kinds:
            return ""
        dirs: list[str] = []
        wanted: set[str] = set()
        for kind in kinds:
            if kind == "research" or not self.sliced or kind not in self.module.MEMBER_FIELDS:
                dirs += [f"{directory}/" for directory in node_state.KIND_DIRS.get(kind, ())]
            else:
                wanted |= {rel for paths in self._entity_paths(item, kind) for rel in paths}
        selected = [line for line in self.lines if "\t" in line
                    and (line.split("\t", 1)[1] in wanted or line.split("\t", 1)[1].startswith(tuple(dirs or ["\0"])))]
        return hashlib.sha256("".join(f"{line}\n" for line in selected).encode("utf-8")).hexdigest()[:16]


def _running(project_id: str, node_id: str) -> set[tuple[str, str]]:
    from ..session_host import exec_records

    found = set()
    for record in exec_records.query(project_id, list(exec_records.OPEN_STATES)):
        if (record.get("node") or {}).get("id") != node_id:
            continue
        slice_id = (record.get("slice") or {}).get("id") if isinstance(record.get("slice"), dict) else None
        found.add((slice_id or FULL_SLICE["id"], record.get("step") or ""))
    return found


def _open_changeset(root: Path) -> dict | None:
    from ...kernel.changesets import store

    record = store.current(root)
    return record if record and record.get("state") in ("open", "pending_confirm", "conflict") else None


# ---------------------------------------------------------------------------
# 计算
# ---------------------------------------------------------------------------


def migrate_legacy(stored: dict, flow: dict) -> bool:
    """0.2 的对象提取、操作提取、规则与指标上的记录 → 建模节点与全量模式那一段（只做一次）。"""
    if stored.get("legacy_migrated") or "core.model" not in {node["id"] for node in flow.get("nodes") or []}:
        return False
    nodes = stored.setdefault("nodes", {})
    slices = stored.setdefault("slices", {})
    full = slices.setdefault(FULL_SLICE["id"], {})
    for old, step in LEGACY.items():
        record = nodes.get(old)
        if record and step not in full:
            full[step] = {"fp": record.get("fp"), "at": record.get("at") or time.time(), "merged": True,
                          **({"needs_review": record["needs_review"]} if record.get("needs_review") else {})}
    old_object, old_operation = nodes.get("core.object"), nodes.get("core.operation")
    model = next(node for node in flow["nodes"] if node["id"] == "core.model")
    if old_object and old_operation and "core.model" not in nodes:
        # 建模节点的上游是资料分析，和原来的对象提取一样：沿用它的输入指纹，原来过期的仍显示过期
        review = old_object.get("needs_review") or old_operation.get("needs_review")
        nodes["core.model"] = {"fp": old_object.get("fp"), "sig": node_state.done_signature(flow, model),
                               "at": old_object.get("at") or time.time(), **({"needs_review": review} if review else {})}
    skipped = stored.setdefault("skipped", {})
    if "core.rule_metric" in skipped:
        stored.setdefault("slice_skips", {}).setdefault(FULL_SLICE["id"], {})["rule"] = skipped["core.rule_metric"]
        skipped.setdefault("core.metric", skipped["core.rule_metric"])
    stored["legacy_migrated"] = True
    return True


def _step_state(inputs: _Inputs, item: dict, step: dict, stored: dict, cells: dict, now: float) -> tuple[str, bool]:
    """一段一步的状态；返回 (状态, 依据有没有改)。"""
    slice_id = item["id"]
    records = stored.setdefault("slices", {}).setdefault(slice_id, {})
    if (slice_id, step["id"]) in inputs.running:
        return "running", False
    if (slice_id, step["id"]) in inputs.pending_steps or inputs.touches(item, step):
        return "pending_confirm", False
    record = records.get(step["id"])
    if inputs.present(item, step) or (record and record.get("merged")):
        fingerprint = inputs.fingerprint(item, step)
        if record is None:
            records[step["id"]] = {"fp": fingerprint, "at": now}
            return "confirmed", True
        if record.get("fp") is not None and record.get("fp") != fingerprint:
            return "stale", False
        return ("auto" if (record.get("needs_review") or {}).get("source") == "autobuild" else "confirmed"), False
    changed = False
    if record is not None and not record.get("merged"):
        records.pop(step["id"])  # 主线上不再满足（回退、删了）：基线作废
        changed = True
    skipped = (stored.get("slice_skips") or {}).get(slice_id, {})
    if step.get("optional") and (step["id"] in skipped or (inputs.sliced and not inputs.planned(item, step))):
        return "skipped", changed
    required = [cells[other["id"]] for other in inputs.steps
                if other["id"] in cells and not other.get("optional") and not other.get("derived")]
    return ("ready" if all(value in (*DONE, "stale") for value in required) else "not_ready"), changed


def _check_state(inputs: _Inputs, step_id: str, cells: dict) -> str:
    """写入即查：这一段没确认的修改有新错误是 draft；排在它前面的必经步骤都确认了就确认。"""
    before = []
    for step in inputs.steps:
        if step["id"] == step_id:
            break
        before.append(step)
    required = [cells[step["id"]] for step in before
                if step["id"] in cells and not step.get("derived") and not step.get("optional")]
    if any(value == "pending_confirm" for value in cells.values()):
        errors = int(((inputs.record or {}).get("checks") or {}).get("errors") or 0)
        return "draft" if errors else "pending_confirm"
    if any(value == "running" for value in cells.values()):
        return "not_ready"
    if any(value == "stale" for value in required):
        return "stale"
    if required and all(value in DONE for value in required):
        return "confirmed"
    return "not_ready"


def _summary(inputs: _Inputs, item: dict, cells: dict) -> dict:
    steps = inputs.steps
    required = [step for step in steps if not step.get("optional")]
    labels = {step["id"]: step["label"] for step in steps}
    running = next((sid for sid, value in cells.items() if value == "running"), None)
    pending = any(value in ("pending_confirm", "draft") for value in cells.values())
    stale = [sid for sid, value in cells.items() if value == "stale"]
    done = all(cells.get(step["id"]) in DONE for step in required)
    started = any(value not in ("ready", "not_ready", "skipped") for value in cells.values())
    if running:
        group, note = "now", f"正在做「{labels[running]}」"
    elif pending or stale:
        group = "wait"
        note = "检查没通过" if "draft" in cells.values() else "待确认" if pending else f"{len(stale)} 步过期"
    elif done:
        group, note = "done", ""
    elif started:
        waiting = next((step for step in required if cells.get(step["id"]) not in DONE), None)
        group, note = "now", f"停在「{waiting['label']}」" if waiting else ""
    else:
        group, note = "todo", ""
    no_rules = inputs.sliced and cells.get("rule") == "skipped" and not inputs.planned(
        item, next((step for step in steps if step["id"] == "rule"), {}))
    return {"id": item["id"], "name": item["name"], "core_flow": item.get("core_flow") or "", "group": group,
            "stages": dict(cells), "note": note, "no_rules": bool(no_rules), "_done": done, "_stale": bool(stale),
            "_running": bool(running), "_pending": pending, "_started": started}


def _next_step(inputs: _Inputs, summaries: list[dict]) -> dict | None:
    """主线下一步：按顺序第一段没做完的下一步（过期的先刷新；规则只在这一段规划了规则时推荐）。"""
    if inputs.running:
        return None
    by_id = {item["id"]: item for item in inputs.slices}
    for summary in summaries:
        if summary["_done"] and not summary["_stale"]:
            continue
        item = by_id[summary["id"]]
        for step in inputs.steps:
            value = summary["stages"].get(step["id"])
            if step.get("derived") or not step.get("command") or value in (*DONE, "pending_confirm", "running", "skipped"):
                continue
            if step.get("optional") and value != "stale" and not (inputs.sliced and inputs.planned(item, step)):
                continue
            return {"slice_id": item["id"], "slice_name": item["name"], "step": step["id"],
                    "label": step.get("action") or step["label"], "command": step["command"], "sliced": inputs.sliced}
        if summary["_pending"]:
            return None  # 这一段还有修改等你确认，先不推荐别的段
    return None


def _stale_steps(inputs: _Inputs, summaries: list[dict]) -> list[dict]:
    """过期的那几段那几步（建议条「刷新「段名 · 步骤名」」）：按段、按步骤的顺序；upstream 是这一步上游的说法。"""
    from .. import workspace_labels

    by_id = {item["id"]: item for item in inputs.slices}
    out = []
    for summary in summaries:
        for step in inputs.steps:
            if summary["stages"].get(step["id"]) != "stale" or not step.get("command"):
                continue
            upstream = []
            for kind in step.get("stale_on") or []:
                owner = next((other for other in inputs.steps if kind in (other.get("kinds") or [])), None)
                upstream.append(owner["label"] if owner else workspace_labels.KIND_LABELS.get(str(kind), str(kind)))
            item = by_id[summary["id"]]
            out.append({"slice_id": item["id"], "slice_name": item["name"], "step": step["id"],
                        "step_label": step["label"], "label": step.get("action") or step["label"],
                        "command": step["command"], "sliced": inputs.sliced,
                        "upstream": "、".join(dict.fromkeys(upstream))})
    return out


def compute(root: Path, flow: dict, *, doc: dict | None, project_id: str | None = None,
            persist: bool = True) -> dict | None:
    """建模节点按段的状态：{node_id, stages, slices, state, progress, hit, slice_next, slice_stale, sliced}；
    没有按段的节点时 None。"""
    node = per_slice_node(flow)
    if node is None or not node.get("steps"):
        return None
    root = Path(root)
    inputs = _Inputs(root, flow, node, doc, project_id or root.name)
    now = time.time()
    with node_state._lock:
        stored = node_state.load(root)
        changed = migrate_legacy(stored, flow)
        summaries = []
        for item in inputs.slices:
            cells: dict[str, str] = {}
            for step in inputs.steps:
                if step.get("derived"):
                    continue
                cells[step["id"]], touched = _step_state(inputs, item, step, stored, cells, now)
                changed = changed or touched
            for step in inputs.steps:
                if step.get("derived"):
                    cells[step["id"]] = _check_state(inputs, step["id"], cells)
            ordered = {step["id"]: cells[step["id"]] for step in inputs.steps}
            summary = _summary(inputs, item, ordered)
            summary["_fps"] = {step["id"]: inputs.fingerprint(item, step) for step in inputs.steps
                               if ordered.get(step["id"]) == "stale"}
            summaries.append(summary)
        if changed and persist:
            node_state.save(root, stored)
    total = len(summaries)
    done = sum(1 for item in summaries if item["_done"])
    stale = [(item["id"], step, item["_fps"].get(step, "")) for item in summaries
             for step, value in item["stages"].items() if value == "stale"]
    if total and done == total and not any(item["_stale"] for item in summaries):
        state = "confirmed"
    elif any(item["_stale"] for item in summaries):
        state = "stale"
    elif any(item["_running"] for item in summaries):
        state = "running"
    elif any(item["_pending"] for item in summaries):
        state = "pending_confirm"
    elif any(item["_started"] for item in summaries):
        state = "running"
    else:
        state = None
    return {
        "node_id": node["id"], "sliced": inputs.sliced, "stages": stage_defs(node),
        "slices": [{key: value for key, value in item.items() if not key.startswith("_")} for item in summaries],
        "state": state, "hit": bool(total) and done == total, "running_exec": bool(inputs.running),
        # 不分段（F8）时节点上不写「0/1 段」：只有「全部」一段，节点状态已经说明进度
        "progress": {"done": done, "total": total, "label": f"{done}/{total} 段" if inputs.sliced else None},
        "slice_next": _next_step(inputs, summaries),
        "slice_stale": _stale_steps(inputs, summaries),
        # 过期的那几段那几步与上游现在的指纹：待处理「过期」一项的来源指纹（又一次过期就重新出现）
        "stale_key": hashlib.sha256(json.dumps(stale, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
        if stale else "",
    }


# ---------------------------------------------------------------------------
# 合入、跳过
# ---------------------------------------------------------------------------


def confirm(root: Path, flow: dict, pairs: list, *, doc: dict | None, needs_review: dict | None = None) -> None:
    """变更集合入后：这一段这一步记为确认过（按主线现状记输入指纹）；自主构建合入的带需复核标记。"""
    node = per_slice_node(flow)
    if node is None or not pairs:
        return
    root = Path(root)
    inputs = _Inputs(root, flow, node, doc, root.name)
    items = {item["id"]: item for item in inputs.slices}
    steps = {step["id"]: step for step in node.get("steps") or []}
    with node_state._lock:
        stored = node_state.load(root)
        for slice_id, step_id in pairs:
            item = items.get(slice_id) or (dict(FULL_SLICE) if slice_id == FULL_SLICE["id"] else None)
            step = steps.get(step_id)
            if item is None or step is None:
                continue
            record = {"fp": inputs.fingerprint(item, step), "at": time.time(), "merged": True}
            if needs_review:
                record["needs_review"] = needs_review
            stored.setdefault("slices", {}).setdefault(slice_id, {})[step_id] = record
            stored.setdefault("slice_skips", {}).get(slice_id, {}).pop(step_id, None)
        node_state.save(root, stored)


def forget(root: Path, slice_id: str, step_ids: list[str]) -> None:
    """这一段这几步的确认记录作废（自主构建按段撤回之后）：按主线现状重算，条目没了就回到可开始。"""
    with node_state._lock:
        stored = node_state.load(root)
        records = (stored.get("slices") or {}).get(slice_id) or {}
        for step_id in step_ids:
            records.pop(step_id, None)
        node_state.save(root, stored)


def records_of(root: Path, slice_id: str) -> dict:
    """这一段各步的确认记录（自主构建开始做这一段前记下，按段撤回时放回去）。"""
    return copy.deepcopy((node_state.load(root).get("slices") or {}).get(slice_id) or {})


def put_back(root: Path, slice_id: str, step_ids: list[str], before: dict) -> None:
    """这一段这几步的确认记录放回成 before 里的（按段撤回，审查 K）：原来过期的仍按原来的指纹算过期，
    原来没有的去掉（按主线现状重算）。"""
    with node_state._lock:
        stored = node_state.load(root)
        records = stored.setdefault("slices", {}).setdefault(slice_id, {})
        for step_id in step_ids:
            if step_id in before:
                records[step_id] = before[step_id]
            else:
                records.pop(step_id, None)
        node_state.save(root, stored)


def skip(root: Path, slice_id: str, step_id: str, *, reason: str = "") -> None:
    with node_state._lock:
        stored = node_state.load(root)
        stored.setdefault("slice_skips", {}).setdefault(slice_id, {})[step_id] = {"at": time.time(), "reason": reason}
        node_state.save(root, stored)


def restore(root: Path, slice_id: str, step_id: str) -> bool:
    with node_state._lock:
        stored = node_state.load(root)
        skips = stored.setdefault("slice_skips", {}).get(slice_id) or {}
        if step_id not in skips:
            return False
        skips.pop(step_id)
        node_state.save(root, stored)
        return True


def slice_for_command(root: Path, flow: dict, step_id: str, explicit: str | None) -> dict | None:
    """建模命令执行在哪一段 {id, name}：带了分段就是它（不存在或已排除时 None）；没带时取这一步还没做完的
    第一段，都做完了取第一段没做完的；全量模式是「全部」。root 可以是助手副本（蓝图按副本里的读）。"""
    from ...kernel.changesets import worktree
    from . import blueprint

    module = blueprint.script(Path(root))
    doc = blueprint.load_doc(Path(root))
    if module.mode(doc) != "sliced":
        return {"id": FULL_SLICE["id"], "name": FULL_SLICE["name"]}
    if explicit:
        item = module.find_slice(doc, explicit)
        return {"id": item["id"], "name": item["name"]} if item and not item.get("excluded") else None
    main = worktree.main_root(Path(root)) or Path(root)
    result = compute(main, flow, doc=doc, persist=False)
    summaries = (result or {}).get("slices") or []
    found = next((item for item in summaries if item["stages"].get(step_id) not in (*DONE, "skipped")), None) \
        or next((item for item in summaries if item["group"] != "done"), None)
    return {"id": found["id"], "name": found["name"]} if found else None

