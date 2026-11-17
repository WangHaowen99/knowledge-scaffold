"""事件触发（ADR-0091）：平台事件发生、当时满足前提时，节点才出现在这个项目的流程里。

事件发生时算一次前提，成立就记下这次触发（主线工作区的 `.onto/run/activation.json`，不进 git，助手写不了）。之后不再看
事件，只看记录。记录分两种，键不会撞（包名不带冒号）：

**内核条件 `materials_added`**（键 `kernel:materials_added`，节点模型重构 3）：内核的增量更新写 `need: {when: materials_added}`。

- 上传资料时主线上已经有对象或操作，就记下 `{commit, at, files}`，不要求有包声明这个事件；
- 这一轮处理完（满足一条就算）：上传之后**开始**的一次增量更新执行完成（上传时已经在跑的那次不算）；触发那次提交之后发布过
  （和 `round_open` 同一口径：最新的 release/* 标签包含触发时的提交）；用户选了「这一轮不处理」（FL-03）。处理完时在记录上
  写 `handled`，之后读流程不再起 git 进程、不再翻执行记录（执行记录只留最近 200 条，不能靠它记住这一轮结束过）；
- 这一轮没处理完时再上传：文件累加，提交和时间换成这一次的（这次的资料也要有一次增量更新看过）；处理完之后再上传开始新的一轮。

**包声明的触发**（键是包名）：包在 flow.yaml 顶层写 `activation: {event: <事件>, requires: [<本包探针>], session: <这一轮的状态文件>}`，
要按它出现的节点写 `need: {when: activated}`。事件是内核给的固定枚举，判断由内核做，包代码不进内核（ADR-0083 约束 4）。

- 这一轮开着（状态文件 `status` 是 active，基线之后没发布过）：出现；
- 触发还没被处理（状态文件的 `covers`，即确认范围时的主线提交，不包含触发时的提交）：出现；
- 其余：不出现。这一轮结束或发布之后节点消失，下次事件再触发。

和内核守卫用同一份判断（`kernel_guards._guard_common.round_open`、`activation_pending`）。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

_log = logging.getLogger(__name__)
_LOCK = threading.Lock()

STATE_REL = Path(".onto") / "run" / "activation.json"
MAX_FILES = 50
MATERIALS_ADDED = "materials_added"
KERNEL_MATERIALS = f"kernel:{MATERIALS_ADDED}"
# 处理新资料的是内核的增量更新：这个节点（或这条命令）上传之后开始、执行完成了，这一轮就算处理完
UPDATE_NODE = "core.update"
UPDATE_COMMAND = "update"
# 「已经有对象或操作」看的目录（同 flow/state.KIND_DIRS 的 object、operation）
_BUILT_DIRS = ("ontology/object", "ontology/operation", "ontology/logic")


def _main(root: Path) -> Path:
    """助手副本 → 主线工作区（触发记录按主线记）。"""
    from ...kernel.changesets import worktree

    return Path(worktree.main_root(Path(root)) or root)


def load(root: Path) -> dict:
    try:
        data = json.loads((_main(root) / STATE_REL).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(root: Path, data: dict) -> None:
    path = _main(root) / STATE_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def _session(main: Path, rel: str):
    try:
        return json.loads((main / rel).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def session_open(root: Path, rel: str) -> bool:
    """这一轮开着（探针 `{kernel: session_open, session: <路径>}`）。"""
    from ...kernel_guards import _guard_common as guard

    main = _main(root)
    return guard.round_open(main / ".git", _session(main, str(rel or "")))


def active(root: Path, pack: str, flow: dict) -> bool:
    """这个包按触发出现的节点此刻该不该出现。"""
    from ...kernel_guards import _guard_common as guard

    spec = (flow.get("activations") or {}).get(pack)
    if not spec:
        return False
    main = _main(root)
    session = _session(main, spec["session"])
    return guard.round_open(main / ".git", session) or guard.activation_pending(main / ".git", load(main).get(pack), session)


# ---- 内核条件 materials_added ----


def _open(item) -> bool:
    """有这一轮的记录、还没记下处理完。"""
    return isinstance(item, dict) and bool(item.get("commit")) and not item.get("handled")


def _built(main: Path) -> bool:
    """主线上已经有对象或操作（隐藏文件、index.md 不算）。"""
    for rel in _BUILT_DIRS:
        base = main / rel
        if not base.is_dir():
            continue
        for path in base.rglob("*.md"):
            parts = path.relative_to(base).parts
            if path.is_file() and path.name.lower() != "index.md" and not any(part.startswith(".") for part in parts):
                return True
    return False


def _updated_since(project_id: str, at: float) -> bool:
    """上传之后开始的增量更新有一次执行完成了。"""
    from ..session_host import exec_records

    for record in exec_records.query(project_id, ["done"]):  # 新的在前
        if float(record.get("started_at") or 0) <= at:
            break
        if record.get("command_name") == UPDATE_COMMAND or exec_records.node_of(record) == UPDATE_NODE:
            return True
    return False


def _handled_by(main: Path, item: dict) -> str | None:
    """这一轮怎么结束的：`update`（做过一次增量更新）、`release`（触发之后发布过）；都没有返回 None。"""
    if _updated_since(main.name, float(item.get("at") or 0)):
        return "update"
    from ...kernel_guards import _guard_common as guard

    common = main / ".git"
    release = guard._latest_release(common)
    # 触发时的提交是上传新建的，发布前不可能有：标签指着它也是上传之后发布的
    if release and guard._is_ancestor(common, str(item["commit"]), release):
        return "release"
    return None


def _mark_handled(main: Path, item: dict, how: str, reason: str = "") -> bool:
    """在这一轮的记录上写下处理完；这期间又上传了（新的一轮）不动，返回 False。"""
    with _LOCK:
        data = load(main)
        current = data.get(KERNEL_MATERIALS)
        if not _open(current) or current.get("at") != item.get("at"):
            return False
        handled = {"by": how, "at": time.time(), **({"reason": reason} if reason else {})}
        data[KERNEL_MATERIALS] = {**current, "handled": handled}
        try:
            _save(main, data)
        except OSError:
            _log.warning("新资料这一轮的结束没记下来", exc_info=True)
            return False
    return True


def materials_pending(root: Path) -> bool:
    """条件 materials_added：上传了新资料、这一轮还没处理完。没有记录时不起 git 进程、不翻执行记录。"""
    main = _main(root)
    item = load(main).get(KERNEL_MATERIALS)
    if not _open(item):
        return False
    how = _handled_by(main, item)
    if how is None:
        return True
    _mark_handled(main, item, how)
    return False


def end_materials_round(root: Path, reason: str = "") -> bool:
    """「这一轮不处理」：新资料这一轮到此结束，节点隐藏；下次上传开始新的一轮。这一轮已经结束（或没有）返回 False。"""
    main = _main(root)
    item = load(main).get(KERNEL_MATERIALS)
    if not _open(item):
        return False
    how = _handled_by(main, item)
    if how is not None:
        _mark_handled(main, item, how)  # 已经处理完了，顺带记下来
        return False
    return _mark_handled(main, item, "skip", reason)


# ---- 事件 ----


def fire(root: Path, event: str, *, commit: str, files: list[str]) -> list[str]:
    """平台事件发生了：内核条件和前提成立的包各记下这次触发，返回记下的键（包名；内核条件是 `kernel:<事件>`）。

    算不出来不影响事件本身（比如上传）：内核条件和包分开算，一边出错不耽误另一边。"""
    root = Path(root)
    fired: list[str] = []
    for part in (_fire_kernel, _fire_packs):
        try:
            fired += part(root, event, commit, files)
        except Exception:  # noqa: BLE001 - 触发是附带动作，不能让上传失败
            _log.warning("事件 %s 的触发没算出来", event, exc_info=True)
    if fired:
        try:
            from .. import project_events

            project_events.schedule_flow_refresh(_main(root).name, delay=0)
        except Exception:  # noqa: BLE001 - 同上
            _log.warning("触发后刷新流程失败", exc_info=True)
    return fired


def _fire_kernel(root: Path, event: str, commit: str, files: list[str]) -> list[str]:
    if event != MATERIALS_ADDED or not commit:
        return []
    main = _main(root)
    if not _built(main):
        return []
    with _LOCK:
        data = load(main)
        previous = data.get(KERNEL_MATERIALS)
        # 这一轮还没处理完：这次的文件并进去，时间换成这次的（这次的资料也要有一次增量更新看过）
        kept = (previous.get("files") or []) if _open(previous) and _handled_by(main, previous) is None else []
        data[KERNEL_MATERIALS] = {"event": event, "commit": commit, "at": time.time(),
                                  "files": list(dict.fromkeys([*kept, *files]))[-MAX_FILES:]}
        _save(main, data)
    return [KERNEL_MATERIALS]


def _holds(names: list, probes: dict) -> bool:
    return all(bool(probes.get(str(name).lstrip("!"))) != str(name).startswith("!") for name in names)


def _fire_packs(root: Path, event: str, commit: str, files: list[str]) -> list[str]:
    from ...kernel_guards import _guard_common as guard
    from . import model

    flow = model.load_for(root)
    wanted = {pack: spec for pack, spec in (flow.get("activations") or {}).items() if spec.get("event") == event}
    if not wanted or not commit:
        return []
    probes = model.flow_state(root).evaluate(flow, root)
    main = _main(root)
    fired = []
    with _LOCK:
        data = load(main)
        for pack, spec in wanted.items():
            if not _holds(spec.get("requires") or [], probes):
                continue
            previous = data.get(pack)
            # 上一次触发还没处理：这次新传的并进去，圈定范围时一起看
            kept = (previous.get("files") or []) if guard.activation_pending(main / ".git", previous, _session(main, spec["session"])) else []
            data[pack] = {"event": event, "commit": commit, "files": list(dict.fromkeys([*kept, *files]))[-MAX_FILES:],
                          "at": time.time()}
            fired.append(pack)
        if fired:
            _save(main, data)
    return fired
