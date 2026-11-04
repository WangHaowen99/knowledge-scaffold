"""命令执行记录（详设 8.4；方案 B5、B6；契约 7.7 ExecRecord、S-15～S-17、EP-09）。

每条命令一份记录，存在数据目录 `exec/<项目>/<执行 ID>.json`（一条一个文件，原子写），由会话宿主
维护，取代 state_helper 靠进程号推断的锁：

    running ──一轮结束、命令没做完──→ waiting ──下一条消息──→ running
    running / waiting ──打断、引擎结束、回收、停机、启动时还开着──→ interrupted ──续跑──→ running
    running / waiting ──命令做完──→ done          interrupted ──放弃──→ abandoned

- 「命令做完」按工具箱的命令锁判断：state_helper 在命令开头 acquire、结束 release，锁文件
  `.ontology-state/lock-<命令>.json` 还在就是没做完（一个命令常跨几轮问答，不能一轮结束就算完成）；
  锁里的 `phase` 字段就是记录的阶段；
- 中断时记下输入指纹（上游产物与资料的提交树），续跑前比对：输入被别人改过、或者节点升级了，
  不能续跑（`exec_not_resumable`）；
- 已保存的内容用中断时的提交算：执行开始以来改了几个文件、当前主线版本。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path

from ... import config

_log = logging.getLogger(__name__)

STATES = ("running", "waiting", "interrupted", "done", "abandoned")
OPEN_STATES = ("running", "waiting")
REASONS = ("user", "restart", "recycled", "engine_exit", "shutdown", "ops")
REASON_TEXT = {
    "user": "你打断了这一步",
    "restart": "服务重启，这一步没做完",
    "recycled": "会话空闲被回收，这一步没做完",
    "engine_exit": "助手进程意外结束，这一步没做完",
    "shutdown": "服务停机，这一步没做完",
    "ops": "运维断开了会话，这一步没做完",
}
KEEP_PER_PROJECT = 200
HEARTBEAT_EVERY_SECONDS = 10
# 工具箱命令锁的阶段名 → 界面上的说法；没列到的原样显示
PHASE_LABELS = {
    "start": "开始", "source": "读取资料", "objects": "写入对象", "operation": "写入操作",
    "rule-metric": "写入规则与指标", "validate": "校验", "fix": "修复", "commit": "保存版本",
    "analyze": "影响分析", "apply": "执行更新", "check": "检查", "confirm": "等你确认", "awaiting_user": "等你回复",
}
# 命令停下来等你在对话里回复时锁切到的阶段（STEP0_TEMPLATE 第 4 步）：这时锁在是正常的，不算没收尾。
# 命令在流程里声明了 lock_waits 的以声明为准，声明了就表示这条命令遵守这个约定（平台可以代为收尾）
WAITING_PHASES = ("confirm", "awaiting_user")
STATE_HELPER_PREFIX = "/ontology-"

_lock = threading.RLock()


def _dir(project_id: str) -> Path:
    return Path(config.DATA_DIR) / "exec" / project_id


def _path(project_id: str, exec_id: str) -> Path:
    return _dir(project_id) / f"{exec_id}.json"


def _write(record: dict) -> None:
    path = _path(record["project_id"], record["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def get(project_id: str, exec_id: str) -> dict | None:
    if not exec_id or "/" in exec_id or exec_id.startswith("."):
        return None
    try:
        data = json.loads(_path(project_id, exec_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def all_records(project_id: str) -> list[dict]:
    """新的在前。"""
    records = []
    try:
        paths = list(_dir(project_id).glob("ex-*.json"))
    except OSError:
        return []
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            records.append(data)
    records.sort(key=lambda item: item.get("started_at") or 0, reverse=True)
    return records


def query(project_id: str, states: list[str] | None = None, conversation_id: str | None = None) -> list[dict]:
    return [
        record for record in all_records(project_id)
        if (not states or record.get("state") in states)
        and (not conversation_id or record.get("conversation_id") == conversation_id)
    ]


def open_record(project_id: str, conversation_id: str) -> dict | None:
    """这条对话里还开着（运行中或等待）的执行。"""
    found = query(project_id, list(OPEN_STATES), conversation_id)
    return found[0] if found else None


def projects_with_records() -> list[str]:
    root = Path(config.DATA_DIR) / "exec"
    try:
        return sorted(path.name for path in root.iterdir() if path.is_dir())
    except OSError:
        return []


def purge(project_id: str) -> None:
    import shutil

    shutil.rmtree(_dir(project_id), ignore_errors=True)


def _trim(project_id: str) -> None:
    records = all_records(project_id)
    for record in records[KEEP_PER_PROJECT:]:
        if record.get("state") in OPEN_STATES:
            continue
        try:
            _path(project_id, record["id"]).unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 命令识别、输入指纹、已保存的内容
# ---------------------------------------------------------------------------


def parse_command(text: str, flow: dict) -> tuple[str, str] | None:
    """消息开头的斜杠命令 → (斜杠命令, 流程里的命令名)；不是流程登记的命令返回 None。

    流程里的命令名不带斜杠与 `ontology-` 前缀（flow.yaml 的 commands），`/version` 这类不带前缀的
    命令按原名查。
    """
    head = (text or "").strip().split(maxsplit=1)
    if not head or not head[0].startswith("/"):
        return None
    slash = head[0]
    name = slash[len(STATE_HELPER_PREFIX):] if slash.startswith(STATE_HELPER_PREFIX) else slash[1:]
    commands = flow.get("commands") or {}
    if name in commands:
        return slash, name
    return None


def _node(flow: dict, node_id: str | None) -> dict | None:
    return next((node for node in flow.get("nodes") or [] if node.get("id") == node_id), None)


def _input_dirs(flow: dict, node_id: str | None) -> list[str]:
    """节点的输入：资料区，加上游节点（after 链）完成判定看的目录。"""
    probes = flow.get("probes") or {}
    dirs = ["resource"]
    seen = set()
    current = _node(flow, node_id)
    while current is not None and current.get("after") and current["after"] not in seen:
        seen.add(current["after"])
        upstream = _node(flow, current["after"])
        if upstream is None:
            break
        for probe_name in upstream.get("done") or []:
            spec = probes.get(str(probe_name).lstrip("!")) or {}
            for directory in spec.get("files") or []:
                if directory not in dirs:
                    dirs.append(directory)
        current = upstream
    return dirs


def _git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(["git", "-c", "core.quotepath=false", *args], cwd=root, capture_output=True,
                                text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout if result.returncode == 0 else ""


def input_fingerprint(root: Path, flow: dict, node_id: str | None) -> str:
    """输入目录在主线上的提交树摘要；读不出（没有提交）时为空串。输入目录还是空的也有摘要：之后补了资料，
    续跑前能看出输入改过（原来空着就不比，资料区模板去掉图例后新项目一开始资料区是空的）。

    助手在副本里跑时也按主线算：副本只在一轮开始时同步主线，跑着时用户在资料区的改动不进副本，
    按副本算会和重启后按主线算的说法不一致（续跑卡说能续、点了又说输入改过）。"""
    from ...kernel.changesets import worktree

    root = worktree.main_root(Path(root)) or Path(root)
    if not head(root):
        return ""
    listing = _git(root, "ls-tree", "-r", "HEAD", "--", *_input_dirs(flow, node_id))
    return hashlib.sha256(listing.encode("utf-8")).hexdigest()[:16]


def head(root: Path) -> str:
    return _git(Path(root), "rev-parse", "--verify", "-q", "HEAD").strip()


def saved(root: Path, base: str) -> dict:
    """执行开始以来已经保存（提交）的内容：改动的文件数与当前主线版本。

    助手在变更集副本里跑时（方案 C7），文件数按副本里的提交算，版本号取主线的（副本的提交在这一轮结束合入后才进主线）。
    """
    from ...kernel.changesets import worktree
    from .. import mainline

    now = head(root)
    files = 0
    if base and now and base != now:
        files = len([line for line in _git(Path(root), "diff", "--name-only", f"{base}..{now}").splitlines() if line])
    main = worktree.main_root(Path(root))
    return {"files": files, "version_label": mainline.label(mainline.count(main)) if now else None}


def lock_phase(root: Path, command_name: str) -> str | None:
    """工具箱命令锁里的阶段；没有锁（命令没开始或已经释放）返回 None。"""
    path = Path(root) / ".ontology-state" / f"lock-{command_name}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return "unknown"
    return str((data or {}).get("phase") or "unknown") if isinstance(data, dict) else "unknown"


def phase_label(phase: str) -> str:
    return PHASE_LABELS.get(phase, phase)


def _declared_waits(flow: dict | None, command_name: str) -> list[str] | None:
    spec = ((flow or {}).get("commands") or {}).get(command_name) or {}
    waits = spec.get("lock_waits") if isinstance(spec, dict) else None
    return [str(item) for item in waits] if isinstance(waits, list) else None


def waiting_for_reply(phase: str | None, flow: dict | None, command_name: str) -> bool:
    """锁在「等你回复」的阶段。"""
    if phase is None:
        return False
    return phase in (_declared_waits(flow, command_name) or WAITING_PHASES)


def unfinished(root: Path, flow: dict | None, command_name: str) -> bool:
    """锁还在、又不是在等你回复：助手没收尾（正文说做完了没释放锁，或者一轮被截断）。"""
    phase = lock_phase(root, command_name)
    return phase is not None and not waiting_for_reply(phase, flow, command_name)


def _wrapped_up(root: Path, record: dict, flow: dict | None, phase: str | None) -> bool:
    """做完了没放锁：命令声明了等回复的阶段（遵守约定）、锁不在那些阶段、这次执行保存过、副本里没有没提交的改动。

    没保存过（可能一轮被迭代上限截断、还在半路）、有没提交的改动（还在写）都不算，留给「继续」。"""
    from ...kernel.changesets import worktree

    command = record.get("command_name") or ""
    if phase is None or _declared_waits(flow, command) is None or waiting_for_reply(phase, flow, command):
        return False
    if not (record.get("saved") or {}).get("files"):
        return False
    return not worktree.dirty(Path(root))


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------


_SLICE_ARG_RE = re.compile(r"(?:^|\s)slice=([A-Za-z0-9_-]{1,40})(?=\s|$)")


def slice_of(flow: dict, command: str, text: str, root: Path) -> tuple[dict | None, dict | None, str | None]:
    """命令属于按段的节点时 → (节点, 分段 {id, name}, 步骤)；不是时 (None, None, None)。

    带 `slice=<分段>` 的命令按节点的 steps 归到按段的节点（`/ontology-extract-rule-metric slice=s2` 是建模的
    「规则」一步，不带是指标旁支）；不带分段的建模命令取这一步还没做完的第一段；全量模式是「全部」那一段。
    """
    from ..flow import slices

    node = slices.per_slice_node(flow)
    if node is None:
        return None, None, None
    step = next((item for item in node.get("steps") or [] if item.get("command") == command), None)
    if step is None:
        return None, None, None
    match = _SLICE_ARG_RE.search(text or "")
    owner = (flow.get("commands") or {}).get(command.lstrip("/").removeprefix("ontology-"), {}).get("node")
    if match is None and owner != node["id"]:
        return None, None, None
    try:
        return node, slices.slice_for_command(Path(root), flow, step["id"], match.group(1) if match else None), step["id"]
    except Exception:  # noqa: BLE001 - 蓝图读不出时不带分段（写入范围按整类条目）
        _log.warning("执行记录取分段失败", exc_info=True)
        return node, None, step["id"]


def start(project_id: str, conversation_id: str, *, root: Path, flow: dict, command: str, command_name: str,
          engine_instance: str = "", task_id: str | None = None, text: str = "") -> dict:
    node = _node(flow, (flow.get("commands") or {}).get(command_name, {}).get("node")) or {}
    sliced_node, slice_ref, step = slice_of(flow, command, text, root)
    label = node.get("title") or command_name
    if sliced_node is not None:
        node = sliced_node
        # 按段的节点：业务名取这一步的（「对象提取」「操作提取」），节点名「建模」太笼统
        step_spec = next((item for item in node.get("steps") or [] if item.get("id") == step), {})
        label = step_spec.get("title") or step_spec.get("label") or node.get("title") or command_name
    now = time.time()
    record = {
        "id": f"ex-{uuid.uuid4().hex[:8]}",
        "project_id": project_id,
        "conversation_id": conversation_id,
        "engine_instance": engine_instance,
        "command": command,
        "command_name": command_name,
        "command_label": label,
        "node": {"id": node.get("id") or f"command.{command_name}", "version": node.get("version") or "",
                 "title": node.get("title") or command_name},
        "slice": slice_ref,
        "step": step,
        "run_id": None,
        "phase": {"index": 0, "total": 0, "label": "", "raw": None},
        "phases_seen": [],
        "state": "running",
        "input_fingerprint": input_fingerprint(root, flow, node.get("id")),
        "base_commit": head(root),
        "started_at": now,
        "heartbeat_at": now,
        "ended_at": None,
        "interrupted": None,
        "saved": {"files": 0, "version_label": None},
        "task_id": task_id,
    }
    with _lock:
        _write(record)
        _trim(project_id)
    return record


def update(project_id: str, exec_id: str, **fields) -> dict | None:
    with _lock:
        record = get(project_id, exec_id)
        if record is None:
            return None
        record.update(fields)
        _write(record)
        return record


def heartbeat(project_id: str, exec_id: str, *, force: bool = False) -> dict | None:
    with _lock:
        record = get(project_id, exec_id)
        if record is None or record.get("state") not in OPEN_STATES:
            return None
        now = time.time()
        if not force and now - float(record.get("heartbeat_at") or 0) < HEARTBEAT_EVERY_SECONDS:
            return None
        record["heartbeat_at"] = now
        _write(record)
        return record


def observe_phase(project_id: str, exec_id: str, root: Path) -> tuple[dict | None, bool]:
    """读命令锁里的阶段；阶段变了返回 (记录, True)。"""
    with _lock:
        record = get(project_id, exec_id)
        if record is None or record.get("state") not in OPEN_STATES:
            return record, False
        raw = lock_phase(root, record.get("command_name") or "")
        if raw is None or raw == (record.get("phase") or {}).get("raw"):
            return record, False
        seen = list(record.get("phases_seen") or [])
        if raw not in seen:
            seen.append(raw)
        record["phases_seen"] = seen
        record["phase"] = {"index": seen.index(raw) + 1, "total": len(seen), "label": phase_label(raw), "raw": raw}
        record["heartbeat_at"] = time.time()
        _write(record)
        return record, True


def finish_turn(project_id: str, exec_id: str, root: Path, *, waiting_user: bool = False, flow: dict | None = None,
                wrap_up: bool = False) -> dict | None:
    """一轮结束：命令锁还在（或在等你回答）记为等待，否则记为完成。

    wrap_up：这一轮正常结束（没出错、没被打断）。这时命令做完了却没放锁（`_wrapped_up`），平台代为释放，记为完成。
    整个判断持锁做：打断（interrupt）可能在别的线程同时改这份记录，已经中断的不能被改回等待。
    """
    from ...kernel.changesets import worktree

    with _lock:
        record, _ = observe_phase(project_id, exec_id, root)
        if record is None or record.get("state") not in OPEN_STATES:
            return None
        command = record.get("command_name") or ""
        phase = lock_phase(root, command)
        now = time.time()
        record["heartbeat_at"] = now
        record["saved"] = saved(root, record.get("base_commit") or "")
        if wrap_up and not waiting_user and _wrapped_up(root, record, flow, phase):
            worktree.clear_run_state(Path(root), [command])
            record["released_by"] = "platform"
            phase = None
        still_open = waiting_user or phase is not None
        if still_open:
            record["state"] = "waiting"
        else:
            record.update(state="done", ended_at=now)
        _write(record)
        return record


def continue_running(project_id: str, exec_id: str) -> dict | None:
    """等待中的执行收到下一条消息：接着跑。"""
    with _lock:
        record = get(project_id, exec_id)
        if record is None or record.get("state") != "waiting":
            return None
        record.update(state="running", heartbeat_at=time.time())
        _write(record)
        return record


def interrupt(project_id: str, exec_id: str, reason: str, *, root: Path | None = None, flow: dict | None = None,
              reason_text: str = "") -> dict | None:
    """打断、引擎结束、回收、停机、启动扫描：记为在某阶段中断，并记下中断时的输入指纹。"""
    with _lock:
        record = get(project_id, exec_id)
        if record is None or record.get("state") not in OPEN_STATES:
            return None
        now = time.time()
        record["state"] = "interrupted"
        record["ended_at"] = now
        record["interrupted"] = {"at": now, "reason": reason if reason in REASONS else "engine_exit",
                                 "reason_text": reason_text or REASON_TEXT.get(reason, "")}
        if root is not None:
            record["saved"] = saved(root, record.get("base_commit") or "")
            if flow is not None:
                record["input_fingerprint"] = input_fingerprint(root, flow, (record.get("node") or {}).get("id"))
        _write(record)
        return record


def not_resumable_reason(record: dict, root: Path | None, flow: dict | None) -> str | None:
    """续跑前的检查：已结束、输入被改过、节点升级了。"""
    if record.get("state") in ("done", "abandoned"):
        return "already_finished"
    if record.get("state") != "interrupted" or root is None or flow is None:
        return None
    node = _node(flow, (record.get("node") or {}).get("id"))
    recorded = (record.get("node") or {}).get("version") or ""
    if node is not None and recorded and (node.get("version") or "") != recorded:
        return "node_upgraded"
    fingerprint = record.get("input_fingerprint") or ""
    if fingerprint and input_fingerprint(root, flow, (record.get("node") or {}).get("id")) != fingerprint:
        return "inputs_changed"
    return None


def resume(project_id: str, exec_id: str, *, conversation_id: str, engine_instance: str = "") -> dict | None:
    return update(project_id, exec_id, state="running", conversation_id=conversation_id, ended_at=None,
                  interrupted=None, engine_instance=engine_instance or "", heartbeat_at=time.time())


def abandon(project_id: str, exec_id: str) -> dict | None:
    return update(project_id, exec_id, state="abandoned", ended_at=time.time())


def sweep_interrupted() -> list[dict]:
    """启动时（任何会话开始之前）：还开着的执行一律改判为「服务重启中断」。返回改判的记录。"""
    changed = []
    for project_id in projects_with_records():
        for record in query(project_id, list(OPEN_STATES)):
            updated = interrupt(project_id, record["id"], "restart")
            if updated is not None:
                changed.append(updated)
    return changed


# ---------------------------------------------------------------------------
# 接口投影（契约 7.7 ExecRecord）
# ---------------------------------------------------------------------------


def _iso(value) -> str | None:
    from .views import iso

    return iso(value)


def view(record: dict, *, root: Path | None = None, flow: dict | None = None) -> dict:
    reason = not_resumable_reason(record, root, flow) if record.get("state") == "interrupted" else None
    phase = record.get("phase") or {}
    interrupted = record.get("interrupted")
    saved_info = record.get("saved") or {}
    return {
        "id": record["id"],
        "conversation_id": record.get("conversation_id") or "",
        "engine_instance": record.get("engine_instance") or "",
        "node": {key: (record.get("node") or {}).get(key) or "" for key in ("id", "version", "title")},
        "slice": record.get("slice"),
        "run_id": record.get("run_id"),
        "command_label": record.get("command_label") or "",
        "phase": {"index": int(phase.get("index") or 0), "total": int(phase.get("total") or 0),
                  "label": phase.get("label") or ""},
        "state": record.get("state"),
        "input_fingerprint": record.get("input_fingerprint") or "",
        "started_at": _iso(record.get("started_at")),
        "heartbeat_at": _iso(record.get("heartbeat_at")),
        "ended_at": _iso(record.get("ended_at")),
        "interrupted": {"at": _iso(interrupted.get("at")), "reason": interrupted.get("reason"),
                        "reason_text": interrupted.get("reason_text") or ""} if interrupted else None,
        "saved": {"files": int(saved_info.get("files") or 0), "version_label": saved_info.get("version_label")},
        "resumable": record.get("state") == "interrupted" and reason is None,
        "not_resumable_reason": reason if reason in ("inputs_changed", "node_upgraded") else None,
    }


def position_label(record: dict) -> str:
    """「你在生成第 3 个场景时打断了」这类说法：按命令与阶段拼。"""
    label = record.get("command_label") or "这一步"
    phase = (record.get("phase") or {}).get("label")
    return f"你在「{label}」的「{phase}」阶段打断了" if phase else f"你在「{label}」进行中打断了"


def resume_card(record: dict, view_data: dict) -> dict:
    """中断续跑卡（契约 4.7 resume）。"""
    phase = view_data["phase"]
    where = f"「{phase['label']}」" if phase["label"] else "进行中"
    step = phase["index"] or 0
    total = phase["total"] or 0
    position_text = f"步骤 {step} / {total}" if step and total else (phase["label"] or "进行中")
    interrupted = view_data.get("interrupted") or {}
    return {
        "card_id": f"resume-{record['id']}",
        "card_type": "resume",
        "eyebrow": "中断续跑",
        "title": f"上次没做完：{view_data['command_label']}在{where}中断",
        "subtitle": interrupted.get("reason_text") or "",
        "status": {"key": "pending" if view_data["resumable"] else "stale",
                   "label": "可以续跑" if view_data["resumable"] else "要按新版重新做"},
        "plugin": None,
        "open": None,
        "exec_id": record["id"],
        "reason": interrupted.get("reason_text") or "",
        "position": {"step": step, "total": total, "label": position_text},
        "saved": view_data["saved"],
        "resumable": view_data["resumable"],
        "not_resumable_reason": {"inputs_changed": "输入的资料或上游产物改过了", "node_upgraded": "这一步的方法升级了"}.get(
            view_data["not_resumable_reason"] or "", None),
        "state": view_data["state"],
    }
