"""包声明的触发（ADR-0091）：平台事件发生、当时满足前提时，节点才出现在这个项目的流程里。

包在 flow.yaml 顶层写 `activation: {event: <事件>, requires: [<本包探针>], session: <这一轮的状态文件>}`，要按它出现的
节点写 `visible_if: activated`。事件是内核给的固定枚举，判断由内核做，包代码不进内核（ADR-0083 约束 4）。

事件发生时算一次前提，成立就记下这次触发（主线工作区的 `.onto/run/activation.json`，不进 git，助手写不了）。之后不再看
事件，只看记录和状态文件：

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


def _main(root: Path) -> Path:
    from .conditions import _main_root

    return _main_root(Path(root))


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


def _holds(names: list, probes: dict) -> bool:
    return all(bool(probes.get(str(name).lstrip("!"))) != str(name).startswith("!") for name in names)


def fire(root: Path, event: str, *, commit: str, files: list[str]) -> list[str]:
    """平台事件发生了：前提成立的包记下这次触发，返回触发了的包。算不出来不影响事件本身（比如上传）。"""
    try:
        return _fire(Path(root), event, commit, files)
    except Exception:  # noqa: BLE001 - 触发是附带动作，不能让上传失败
        _log.warning("事件 %s 的触发没算出来", event, exc_info=True)
        return []


def _fire(root: Path, event: str, commit: str, files: list[str]) -> list[str]:
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
    if fired:
        from .. import project_events

        project_events.schedule_flow_refresh(main.name, delay=0)
    return fired
