"""节点状态（详设 6.6；方案 D13；契约 7.8 NodeState、FlowNode.flags）：由内核按执行记录、变更集与产物指纹算。

8 种状态：

| 状态 | 怎么算 |
|---|---|
| running 进行中 | 有没结束的执行记录（运行中、等你回答） |
| pending_confirm 待确认 | 执行结束了，节点的改动在没确认的变更集里 |
| confirmed 已确认 | 完成判定在主线上满足，且记下的输入指纹没变 |
| stale 过期 | 已确认之后，`stale_on` 声明的产物在主线上的指纹变了 |
| skipped 已跳过 | 可选节点被你跳过（FL-03） |
| blocked 被挡住 | 挡住它的关卡（`blocks` 指向它的节点）还没确认 |
| ready 可开始 | 输入都已确认（锚点链上的上游节点已确认或过期） |
| not_ready 未就绪 | 其余 |

完成判定只看主线：助手在副本里改的文件要这一轮合入才进主线，进度不再因为文件落盘就打勾。

附加标记 `needs_review`（需复核）：节点升级后完成判定变了（记下的判定签名对不上，`upgrade`）、导入已发布的
知识包带进来的产物（`import`）、自主构建自动合入的段（`autobuild`，P3 经 `mark_needs_review` 标）。专家接受
（待处理 `accept`）后去掉。

状态的依据存在数据目录 `flow-state/<项目>.json`（不进 git）：每个已确认节点记下确认时的输入指纹与完成判定签名、
需复核标记、跳过记录。第一次看到已经满足完成判定的节点（存量项目）直接记成基线，不算过期。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path


# stale_on 里能写的产物类别 → 主线上的目录
KIND_DIRS = {
    "research": ("research",),
    "object": ("ontology/object",),
    "operation": ("ontology/operation", "ontology/logic"),
    "rule": ("ontology/rule",),
    "metric": ("ontology/metric",),
    "script": ("ontology/scripts",),
    "mock": ("ontology/data-mock",),
    "testcase": ("ontology/testcase",),
    "diagram": ("ontology/diagrams",),
    "solver": ("ontology/solver",),
    "knowledge": ("ontology/knowledge",),
}
REVIEW_SOURCES = ("autobuild", "upgrade", "import")
DISPLAY = {"not_ready": "not_ready", "ready": "ready", "running": "running", "pending_confirm": "pending",
           "confirmed": "confirmed", "stale": "stale", "skipped": "skipped", "blocked": "blocked"}

_lock = threading.RLock()


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------


def _store_path(root: Path) -> Path:
    from ...kernel.changesets.worktree import data_dir_of

    root = Path(root)
    return data_dir_of(root) / "flow-state" / f"{root.name}.json"


def load(root: Path) -> dict:
    try:
        data = json.loads(_store_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("nodes", {})
    data.setdefault("skipped", {})
    return data


def save(root: Path, data: dict) -> None:
    path = _store_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def purge(root: Path) -> None:
    """删除项目：节点状态与同一项目的附属记录（`<项目>.*.json`，如蓝图卡登记）一起删。"""
    path = _store_path(root)
    for candidate in [path, *path.parent.glob(f"{path.stem}.*.json")]:
        try:
            candidate.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 指纹与签名
# ---------------------------------------------------------------------------


# 不算进上游指纹的文件：建模蓝图在 research/ 下，调整分段不该让对象提取过期
FINGERPRINT_EXCLUDE = ("research/blueprint.json",)


def _ls_tree(root: Path, dirs: list[str]) -> str:
    try:
        result = subprocess.run(  # NOSONAR(S4721) 固定 git 子命令，参数列表不经 shell
            ["git", "-c", "core.quotepath=false", "ls-tree", "-r", "HEAD", "--", *dirs], cwd=root,
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return "".join(line + "\n" for line in result.stdout.splitlines()
                   if line.split("\t", 1)[-1] not in FINGERPRINT_EXCLUDE)


def tree_listing(root: Path) -> list[str]:
    """主线上 ontology/、research/ 的 ls-tree 行（一次读出，按段的指纹从里面挑）。"""
    return _ls_tree(root, ["ontology", "research"]).splitlines()


def fingerprint(root: Path, kinds: list[str]) -> str:
    """stale_on 里各类产物在主线上的树摘要；没有声明 stale_on 时为空串（不会过期）。"""
    dirs = [directory for kind in kinds for directory in KIND_DIRS.get(str(kind), ())]
    if not dirs:
        return ""
    return hashlib.sha256(_ls_tree(root, dirs).encode("utf-8")).hexdigest()[:16]


def kind_fingerprints(root: Path, kinds: list[str]) -> dict[str, str]:
    """按类别分开的输入指纹：过期时据此说出是哪几类变了（「对象有变化」），不是把 stale_on 整个列出来。"""
    return {str(kind): fingerprint(root, [kind]) for kind in kinds}


def changed_kinds(root: Path, record: dict, kinds: list[str]) -> list[str]:
    """确认之后变了的类别。老记录没存分类指纹：按确认那一刻主线上的提交比到现在；还说不清时返回空（按钮只写「刷新X」）。"""
    saved = record.get("kinds")
    if isinstance(saved, dict):
        return [str(kind) for kind in kinds if saved.get(str(kind)) != fingerprint(root, [kind])]
    return changed_since(root, record.get("at"), kinds) or []


def _git(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(  # NOSONAR(S4721) 固定 git 子命令，参数列表不经 shell
            ["git", "-c", "core.quotepath=false", *args], cwd=root, capture_output=True, text=True,
            timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def changed_since(root: Path, at, kinds: list[str]) -> list[str] | None:
    """主线上从 at（确认时刻）那个提交到现在，哪几类的目录有改动；只沿主线走（不看合入进来的助手副本提交的时间）。"""
    if not isinstance(at, (int, float)):
        return None
    base = (_git(root, "rev-list", "-1", "--first-parent", f"--before=@{int(at)}", "HEAD") or "").strip()
    if not base:
        return None
    out = []
    for kind in kinds:
        dirs = KIND_DIRS.get(str(kind), ())
        if not dirs:
            continue
        names = _git(root, "diff", "--name-only", base, "HEAD", "--", *dirs)
        if names is None:
            return None
        if any(name not in FINGERPRINT_EXCLUDE for name in names.splitlines()):
            out.append(str(kind))
    return out


def done_signature(flow: dict, node: dict) -> str:
    """完成判定签名：节点的 done 与它用到的探针定义。节点升级后判定变了，旧的「已确认」要复核（详设 6.9 第 6 条）。"""
    probes = flow.get("probes") or {}
    names = [str(item).lstrip("!") for item in node.get("done") or []]
    body = {"done": node.get("done") or [], "probes": {name: probes.get(name) for name in names}}
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 计算
# ---------------------------------------------------------------------------


def _open_execs(project_id: str) -> dict[str, dict]:
    from ..session_host import exec_records

    found = {}
    for record in exec_records.query(project_id, list(exec_records.OPEN_STATES)):
        node_id = (record.get("node") or {}).get("id")
        if node_id and node_id not in found:
            found[node_id] = record
    return found


def _pending_nodes(root: Path) -> tuple[set[str], dict | None]:
    from ...kernel.changesets import store

    record = store.current(root)
    if record is None:
        return set(), None
    return set(record.get("nodes") or []), record


def _gates(other: dict, node_id: str, sliced: bool) -> bool:
    """other 挡着 node_id：blocks 里列了它；blocks_in 是 sliced 的只在分段模式下挡。"""
    return node_id in (other.get("blocks") or []) and (other.get("blocks_in") != "sliced" or sliced)


def compute(root: Path, flow: dict, hits: dict[str, bool], *, project_id: str | None = None,
            persist: bool = True, sliced: bool = False) -> dict[str, dict]:
    """全部节点 → {state, flags, display_state}。persist=False 时只算不记（预览、测试）。

    sliced：项目分段建模（建模蓝图多于一段）；声明 `blocks_in: sliced` 的关卡只在这时挡。"""
    root = Path(root)
    project_id = project_id or root.name
    nodes = flow.get("nodes") or []
    by_id = {node["id"]: node for node in nodes}
    running = _open_execs(project_id)
    pending, changeset = _pending_nodes(root)
    with _lock:
        stored = load(root)
        changed = False
        result: dict[str, dict] = {}
        fingerprints: dict[str, str] = {}

        def fp(node: dict) -> str:
            key = node["id"]
            if key not in fingerprints:
                fingerprints[key] = fingerprint(root, list(node.get("stale_on") or []))
            return fingerprints[key]

        for node in nodes:
            node_id = node["id"]
            record = stored["nodes"].get(node_id)
            flags = {"needs_review": False, "disabled": False, "backfill": False}
            if record and record.get("needs_review"):
                flags["needs_review"] = True
                flags["review_source"] = record["needs_review"].get("source")
            if node_id in running:
                state = "running"
            elif node_id in pending and changeset is not None:
                state = "pending_confirm"
            elif node_id in stored["skipped"] and node.get("optional") and not hits.get(node_id):
                state = "skipped"
            elif hits.get(node_id):
                if node_id in stored["skipped"]:
                    # 跳过之后又补做完成：不再算跳过（审查第 14 条）
                    stored["skipped"].pop(node_id)
                    changed = True
                signature = done_signature(flow, node)
                if record is None:
                    record = {"fp": fp(node), "kinds": kind_fingerprints(root, list(node.get("stale_on") or [])),
                              "sig": signature, "at": time.time()}
                    stored["nodes"][node_id] = record
                    changed = True
                elif record.get("sig") and record["sig"] != signature and not record.get("needs_review"):
                    # 节点升级、完成判定变了：旧的「已确认」标需复核，判定签名换成新的
                    record.update(sig=signature, needs_review={"source": "upgrade", "at": time.time()})
                    flags.update(needs_review=True, review_source="upgrade")
                    changed = True
                state = "stale" if record.get("fp", "") != fp(node) else "confirmed"
                if state == "stale":
                    result[node_id] = {"state": state, "flags": flags,
                                       "changed": changed_kinds(root, record, list(node.get("stale_on") or []))}
                    continue
            else:
                if record is not None and not record.get("needs_review"):
                    # 主线上不再满足完成判定（回退、删了产物）：这次确认作废，之后重新满足时重记基线
                    stored["nodes"].pop(node_id, None)
                    changed = True
                state = None
            result[node_id] = {"state": state, "flags": flags}
        # 被挡住与可开始（要知道上游的结论，第二遍算）
        for node in nodes:
            item = result[node["id"]]
            if item["state"] is not None:
                continue
            gates = [other for other in nodes if _gates(other, node["id"], sliced)]
            if any(result[gate["id"]]["state"] not in ("confirmed", "stale", "skipped") for gate in gates):
                item["state"] = "blocked"
                continue
            anchor = node.get("after")
            while anchor and by_id.get(anchor, {}).get("optional") and result.get(anchor, {}).get("state") not in (
                    "confirmed", "stale"):
                anchor = by_id[anchor].get("after")
            upstream = result.get(anchor, {}).get("state") if anchor else "confirmed"
            item["state"] = "ready" if upstream in ("confirmed", "stale") else "not_ready"
        if changed and persist:
            save(root, stored)
    for node_id, item in result.items():
        item["display_state"] = display(item["state"], item["flags"])
    return result


def display(state: str, flags: dict) -> str:
    if state == "confirmed" and flags.get("needs_review"):
        return "auto" if flags.get("review_source") == "autobuild" else "needs_review"
    if flags.get("disabled"):
        return "disabled"
    if flags.get("backfill") and state in ("ready", "not_ready"):
        return "backfill"
    return DISPLAY.get(state, "none")


# ---------------------------------------------------------------------------
# 确认、复核、跳过（流程运行时对外的写操作）
# ---------------------------------------------------------------------------


def confirm(root: Path, flow: dict, node_ids: list[str]) -> None:
    """变更集合入后：这几个节点按主线现状重新记基线（输入指纹、判定签名），不再过期。"""
    root = Path(root)
    by_id = {node["id"]: node for node in flow.get("nodes") or []}
    with _lock:
        stored = load(root)
        for node_id in node_ids:
            node = by_id.get(node_id)
            if node is None:
                continue
            # 人确认过（重做后确认生效）：需复核标记去掉（审查第 21 条）；补做完的可选节点不再算跳过
            kinds = list(node.get("stale_on") or [])
            stored["nodes"][node_id] = {"fp": fingerprint(root, kinds), "kinds": kind_fingerprints(root, kinds),
                                        "sig": done_signature(flow, node), "at": time.time()}
            stored["skipped"].pop(node_id, None)
        save(root, stored)


def mark_needs_review(root: Path, node_ids: list[str], source: str, *, run_id: str | None = None,
                      slice_id: str | None = None, note: str = "") -> None:
    """标「需复核」（自主构建自动合入的段、导入、节点升级）；P3 自主构建合入一段后调。"""
    if source not in REVIEW_SOURCES:
        raise ValueError(f"需复核的来源只能是 {REVIEW_SOURCES}")
    root = Path(root)
    with _lock:
        stored = load(root)
        for node_id in node_ids:
            record = stored["nodes"].setdefault(node_id, {"fp": None, "sig": None, "at": time.time()})
            record["needs_review"] = {"source": source, "at": time.time(), "run_id": run_id, "slice_id": slice_id,
                                      "note": note}
        save(root, stored)


def mark_imported(root: Path) -> list[str]:
    """导入已发布的知识包后：主线上已满足完成判定的节点标「需复核」（来源 import）。返回标了的节点。"""
    from . import model

    root = Path(root)
    flow = model.load_for(root)
    hits = model.flow_state(root).node_hits(flow, root)
    marked = [node_id for node_id, hit in hits.items() if hit]
    if marked:
        compute(root, flow, hits)  # 先记基线，再标复核
        mark_needs_review(root, marked, "import", note="导入的知识包带进来的产物")
    return marked


def accept_review(root: Path, node_id: str) -> bool:
    """专家接受（待处理 `accept`）：去掉需复核标记。返回原来有没有标记。"""
    root = Path(root)
    with _lock:
        stored = load(root)
        record = stored["nodes"].get(node_id)
        if not record or not record.get("needs_review"):
            return False
        record.pop("needs_review")
        save(root, stored)
        return True


def reviews(root: Path) -> list[dict]:
    """需复核的节点 [{node_id, source, at, run_id, slice_id, note}]（待处理汇总用）。"""
    stored = load(root)
    return [{"node_id": node_id, **record["needs_review"]}
            for node_id, record in stored["nodes"].items() if record.get("needs_review")]


def skip(root: Path, node_id: str, *, reason: str = "", slice_id: str | None = None) -> None:
    root = Path(root)
    with _lock:
        stored = load(root)
        stored["skipped"][node_id] = {"at": time.time(), "reason": reason, "slice_id": slice_id}
        save(root, stored)


def restore(root: Path, node_id: str) -> bool:
    root = Path(root)
    with _lock:
        stored = load(root)
        if node_id not in stored["skipped"]:
            return False
        stored["skipped"].pop(node_id)
        save(root, stored)
        return True


def skipped(root: Path) -> dict:
    return load(root)["skipped"]
