"""节点状态（详设 6.6；方案 D13；契约 7.8 NodeState、FlowNode.flags）：由内核按执行记录、变更集与产物指纹算。

8 种状态：

| 状态 | 怎么算 |
|---|---|
| running 进行中 | 有没结束的执行记录（运行中、等你回答） |
| pending_confirm 待确认 | 执行结束了，节点的改动在没确认的变更集里 |
| confirmed 已确认 | 完成判定在主线上满足，且记下的输入指纹没变 |
| stale 过期 | 已确认之后，`stale_on` 声明的产物在主线上的指纹变了（`object_set`、`operation_set` 只看条目增删改名，ADR-0102） |
| skipped 已跳过 | 可选节点被你跳过（FL-03） |
| blocked 被挡住 | 挡住它的关卡（`blocks` 指向它的节点）还没确认 |
| ready 可开始 | 输入都已确认（锚点链上的上游节点已确认或过期） |
| not_ready 未就绪 | 其余 |

完成判定只看主线：助手在副本里改的文件要这一轮合入才进主线，进度不再因为文件落盘就打勾。

附加标记 `needs_review`（需复核）：节点升级后完成判定变了（记下的判定签名对不上，`upgrade`）、导入已发布的
知识包带进来的产物（`import`）、自主构建自动合入的段（`autobuild`，P3 经 `mark_needs_review` 标）。专家接受
（待处理 `accept`）后去掉。

状态的依据存在数据目录 `flow-state/<项目>.json`（不进 git）：每个已确认节点记下确认时的输入指纹、完成判定签名与
主线提交（`rev`）、需复核标记、跳过记录。第一次看到已经满足完成判定的节点（存量项目）直接记成基线，不算过期。

刷新范围（节点声明 `refresh_scope: changed_entries`）：过期后刷新只查确认之后改动的条目，命令写成
`<入口命令> <条目名…> --since <确认时提交前 12 位>`；删了条目、改了条目目录以外的文件、只改了 index.md、
找不到确认时的提交时算不出范围，照旧发整条入口命令（`refresh_scope`）。
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
    # 只看有哪些条目（新增、删除、改名），不看内容（ADR-0102）：内容修改是一致性问题，交给校验
    "object_set": ("ontology/object",),
    "operation_set": ("ontology/operation", "ontology/logic"),
    "rule": ("ontology/rule",),
    "metric": ("ontology/metric",),
    "script": ("ontology/scripts",),
    "mock": ("ontology/data-mock",),
    "testcase": ("ontology/testcase",),
    "diagram": ("ontology/diagrams",),
    "solver": ("ontology/solver",),
    "knowledge": ("ontology/knowledge",),
}
SET_KINDS = frozenset({"object_set", "operation_set"})
# ADR-0102 之前的 stale_on：没存分类指纹的老记录按它判断定义变没变（键：节点，或「建模节点/步骤」）
STALE_ON_BEFORE_0102 = {"core.metric": ("object", "operation"), "core.model/operation": ("object",),
                        "core.model/rule": ("object", "operation")}
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


def set_prefixes(kinds) -> tuple[str, ...]:
    """只看条目清单的类别（SET_KINDS）占的目录前缀；同一目录也按内容看时（同时声明了 object）以内容为准。"""
    content = {directory for kind in kinds if str(kind) not in SET_KINDS for directory in KIND_DIRS.get(str(kind), ())}
    return tuple(f"{directory}/" for kind in kinds if str(kind) in SET_KINDS
                 for directory in KIND_DIRS.get(str(kind), ()) if directory not in content)


def render_tree(lines, prefixes: tuple[str, ...]) -> str:
    """ls-tree 的行 → 参与摘要的文本：prefixes 下的行只留路径（条目增删改名才变），其余行原样（内容一改就变）。

    没有只看清单的类别时和原来逐字相同，已有的确认记录接着用。"""
    out = []
    for line in lines:
        path = line.split("\t", 1)[-1]
        out.append(path if prefixes and path.startswith(prefixes) else line)
    return "".join(f"{line}\n" for line in out)


def fingerprint(root: Path, kinds: list[str]) -> str:
    """stale_on 里各类产物在主线上的树摘要；没有声明 stale_on 时为空串（不会过期）。"""
    dirs = [directory for kind in kinds for directory in KIND_DIRS.get(str(kind), ())]
    if not dirs:
        return ""
    text = render_tree(_ls_tree(root, list(dict.fromkeys(dirs))).splitlines(), set_prefixes(kinds))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


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


def _head(root: Path) -> str | None:
    return (_git(root, "rev-parse", "HEAD") or "").strip() or None


def _rev_at(root: Path, at) -> str | None:
    """at（确认时刻）那一刻主线上的提交；只沿主线走（不看合入进来的助手副本提交的时间）。"""
    if not isinstance(at, (int, float)):
        return None
    return (_git(root, "rev-list", "-1", "--first-parent", f"--before=@{int(at)}", "HEAD") or "").strip() or None


def baseline_rev(root: Path, record: dict) -> str | None:
    """确认记录对应的主线提交：记下的 rev；老记录没存 rev 时按确认时刻沿主线找。"""
    rev = record.get("rev")
    return rev if isinstance(rev, str) and rev else _rev_at(root, record.get("at"))


def rebaseline_redefined(root: Path, record: dict, kinds: list[str], fingerprint_now: str, kinds_now,
                         before=None) -> bool:
    """记录是按另一套 stale_on 记的（ADR-0102 换了定义，或包升级改了声明）：两边指纹没法直接比。

    按确认那一刻到现在主线上「现在声明的上游」有没有改动来定：没改、或找不到那一刻的提交，按现在的定义重记基线
    （找不到时不判过期：旧指纹本来就比不了，资料变了建模的对象一步照样会过期）；改了的类别指纹记空，判过期并说得出
    是哪几类。before：老记录没存分类指纹时当时的定义（STALE_ON_BEFORE_0102），不知道就不动。返回记录改没改。"""
    saved = record.get("kinds")
    previous = set(saved) if isinstance(saved, dict) else set(before) if before is not None else None
    if previous is None or previous == {str(kind) for kind in kinds}:
        return False
    current = kinds_now()
    changed = changed_since(root, record.get("at"), kinds) or []
    if changed:
        record.update(fp="", kinds={kind: ("" if kind in changed else value) for kind, value in current.items()})
    else:
        record.update(fp=fingerprint_now, kinds=current)
    return True


def changed_since(root: Path, at, kinds: list[str]) -> list[str] | None:
    """主线上从 at（确认时刻）那个提交到现在，哪几类的目录有改动；只沿主线走（不看合入进来的助手副本提交的时间）。"""
    base = _rev_at(root, at)
    if not base:
        return None
    out = []
    for kind in kinds:
        dirs = KIND_DIRS.get(str(kind), ())
        if not dirs:
            continue
        # 只看清单的类别：只算新增、删除、改名
        only = ("--diff-filter=ADR",) if str(kind) in SET_KINDS else ()
        names = _git(root, "diff", "--name-only", *only, base, "HEAD", "--", *dirs)
        if names is None:
            return None
        if any(name not in FINGERPRINT_EXCLUDE for name in names.splitlines()):
            out.append(str(kind))
    return out


# 刷新范围 changed_entries 认的目录：一个 .md 文件是一个条目，文件名（去掉 .md）是条目名
ENTRY_DIRS = ("ontology/object/", "ontology/operation/", "ontology/rule/", "ontology/metric/")
# 改动的条目多于这么多时不列名字，整条入口命令刷新：命令太长，查的量也接近全量
SCOPE_NAMES_MAX = 100


def _entry_name(path: str) -> str | None:
    """条目路径 → 条目名；不是条目（目录外、不是 .md）返回 None，index.md 返回空串（跳过）。"""
    if not path.startswith(ENTRY_DIRS) or not path.lower().endswith(".md"):
        return None
    name = path.rsplit("/", 1)[-1][:-3]
    return "" if name.lower() == "index" else name


def changed_entries(root: Path, record: dict, kinds: list[str]) -> dict | None:
    """确认之后主线上改动（新增、修改）的条目：{since: 确认时的提交, names: [条目名]}。

    算不出就返回 None（调用方整条刷新）：找不到确认时的提交、有删除、改到条目目录以外（如 ontology/logic/、
    非 .md 文件）、条目名带空白或会被当成参数（以 - 开头、带 =）、只改了 index.md、改动多于 SCOPE_NAMES_MAX 个。"""
    base = baseline_rev(root, record)
    dirs = [directory for kind in kinds for directory in KIND_DIRS.get(str(kind), ())]
    if not base or not dirs or _git(root, "cat-file", "-e", f"{base}^{{commit}}") is None:
        return None
    out = _git(root, "diff", "--name-status", "--no-renames", "-z", base, "HEAD", "--", *dirs)
    if out is None:
        return None
    fields = out.split("\0")
    if fields and fields[-1] == "":
        fields.pop()
    if len(fields) % 2:
        return None
    names = set()
    for status, path in zip(fields[0::2], fields[1::2]):
        if path in FINGERPRINT_EXCLUDE:
            continue
        name = _entry_name(path)
        if status not in ("A", "M") or name is None:
            return None
        if not name:
            continue
        if any(char.isspace() for char in name) or name.startswith("-") or "=" in name:
            return None
        names.add(name)
    if not names or len(names) > SCOPE_NAMES_MAX:
        return None
    return {"since": base, "names": sorted(names)}


def refresh_entry(node: dict) -> str:
    """节点的入口命令（没有时取第一个对话命令动作）；带参数的入口返回空串（不往后拼条目名）。"""
    command = str((node.get("entry") or {}).get("command") or "").strip() or next(
        (str(action["command"]).strip() for action in node.get("actions") or []
         if str(action.get("command") or "").startswith("/")), "")
    return command if command.startswith("/") and len(command.split()) == 1 else ""


def refresh_scope(root: Path, node: dict, record: dict | None = None) -> dict | None:
    """声明了 `refresh_scope: changed_entries` 的节点过期后的刷新范围：{since, names, command}，
    command 是 `<入口命令> <条目名…> --since <确认时提交前 12 位>`。节点是否过期由调用方判断。

    算不出范围、节点按段算、节点升级后待复核（判定变了，之前查过的也要按新判定重看）时返回 None：照旧整条刷新。"""
    if node.get("refresh_scope") != "changed_entries" or node.get("per_slice"):
        return None
    entry = refresh_entry(node)
    if not entry:
        return None
    root = Path(root)
    if record is None:
        record = load(root)["nodes"].get(node["id"])
    if not isinstance(record, dict):
        return None
    review = record.get("needs_review")
    if isinstance(review, dict) and review.get("source") == "upgrade":
        return None
    scope = changed_entries(root, record, list(node.get("stale_on") or []))
    if scope is None:
        return None
    return {**scope, "command": f"{entry} {' '.join(scope['names'])} --since {scope['since'][:12]}"}


def current_refresh_scope(root: Path, node: dict) -> dict | None:
    """开始节点（FL-05、待处理里的刷新）用：节点此刻过期且算得出范围才给，否则 None（照旧发整条入口命令）。"""
    from . import model

    if node.get("refresh_scope") != "changed_entries" or node.get("per_slice"):
        return None
    root = Path(root)
    flow = model.load_for(root)
    hits = model.flow_state(root).node_hits(flow, root)
    if (compute(root, flow, hits, persist=False).get(node["id"]) or {}).get("state") != "stale":
        return None
    return refresh_scope(root, node)


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
                              "sig": signature, "at": time.time(), "rev": _head(root)}
                    stored["nodes"][node_id] = record
                    changed = True
                elif record.get("sig") and record["sig"] != signature and not record.get("needs_review"):
                    # 节点升级、完成判定变了：旧的「已确认」标需复核，判定签名换成新的
                    record.update(sig=signature, needs_review={"source": "upgrade", "at": time.time()})
                    flags.update(needs_review=True, review_source="upgrade")
                    changed = True
                stale_on = list(node.get("stale_on") or [])
                if rebaseline_redefined(root, record, stale_on, fp(node), lambda: kind_fingerprints(root, stale_on),
                                        STALE_ON_BEFORE_0102.get(node_id)):
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
                                        "sig": done_signature(flow, node), "at": time.time(), "rev": _head(root)}
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


def produced_by(flow: dict, paths: list[str], exclude: list[str] = ()) -> list[str]:
    """改到这些路径的是哪些节点的产物（按 `produces` 的路径模式）；exclude 里的不算。"""
    import fnmatch

    out = []
    for node in flow.get("nodes") or []:
        if node["id"] in exclude:
            continue
        patterns = [str(pattern) for pattern in node.get("produces") or []]
        if any(fnmatch.fnmatch(path, pattern) for path in paths for pattern in patterns):
            out.append(node["id"])
    return out


def mark_written_by_autobuild(root: Path, flow: dict, node_ids: list[str], hits: dict, review: dict,
                              note: str = "") -> list[str]:
    """自主构建合入时顺带写了产物、已满足完成判定的别的节点（操作提取一起抽的指标）也标「需复核」。

    原来没有基线的按主线记一个（不记的话需复核的空记录会被当成过期）；人确认过的保留原基线，只加标记。返回标了的节点。"""
    done = [node_id for node_id in node_ids if hits.get(node_id)]
    if not done:
        return []
    with _lock:
        known = set(load(root)["nodes"])
    confirm(root, flow, [node_id for node_id in done if node_id not in known])
    mark_needs_review(root, done, "autobuild", run_id=review.get("run_id"), slice_id=review.get("slice_id"),
                      note=note)
    return done


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
