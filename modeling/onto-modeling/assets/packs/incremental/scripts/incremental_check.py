#!/usr/bin/env python3
"""增量构建的传播核对（ADR-0085）：基线到现在改了什么，谁该跟着看却还没看。

    run  [--threshold 0.3]    算差异、范围外改动、待确认项和改动占比，写 incremental/check.{md,json}
    mark <条目>... --reason R  标记「不受影响」并重算

核对的是整个工作区，网页上手工改的条目同样算。每个改动条目从当前内容重算传播链，
链上没改动、也没标「不受影响」的条目就是待确认项（报告里的 `[ ]`）。范围外改动也是 `[ ]`，不能标记，只能并进范围或撤回。节点在报告存在且没有 `[ ]` 时算完成。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path, PurePosixPath

from incremental_chain import (
    STATE_DIR,
    STDOUT_CAP,
    ChainError,
    Graph,
    baseline_entries,
    capped,
    changes_since,
    clean,
    read_scope,
    require_lock,
    write_json,
)

COMMAND = "incremental-check"
CHECK_MD = STATE_DIR / "check.md"
CHECK_JSON = STATE_DIR / "check.json"
DEFAULT_THRESHOLD = 0.3
LABELS = {"added": "新增", "modified": "修改", "deleted": "删除"}


# 命令行输出通道：独立 logger，不经根 logger，消息原样（无前缀）
_console = logging.Logger("incremental_check", logging.INFO)
_console_handler = logging.StreamHandler()
_console.addHandler(_console_handler)


def _say(text: object = "", *, error: bool = False) -> None:
    """输出一行：结果写标准输出（助手读它），error=True 写标准错误；每次取当前的流，测试替换了也跟着走。"""
    _console_handler.setStream(sys.stderr if error else sys.stdout)
    _console.info(text)


def _load_report(root: Path) -> dict:
    try:
        data = json.loads((root / CHECK_JSON).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _members(root: Path, changes: dict[str, str], depth: int) -> dict[str, dict]:
    """各改动条目传播链上的、自己没改动的条目；同一条目被多条链走到时留最短的那条路径。"""
    graph = Graph(root, stubs=[path for path, status in changes.items() if status != "modified"])
    found: dict[str, dict] = {}
    for path in changes:
        for item in graph.walk([path], depth):
            member = item["path"]
            if member in changes or (member in found and found[member]["depth"] <= item["depth"]):
                continue
            found[member] = {key: item[key] for key in ("path", "depth", "via", "relation", "trail")}
    return dict(sorted(found.items()))


def build_report(root: Path, threshold: float, marks: dict[str, str]) -> dict:
    scope = read_scope(root)
    if scope is None or not isinstance(scope.get("baseline"), dict):
        raise ChainError("还没有范围文件，先 /incremental-scope 圈定。")
    commit = scope["baseline"]["commit"]
    baseline = set(baseline_entries(root, commit))
    editable = set(scope.get("editable") or [])
    changes = changes_since(root, commit)
    outside = [{"path": path, "status": status} for path, status in changes.items()
               if path in baseline and path not in editable]
    pending, cleared = [], []
    for path, member in _members(root, changes, scope.get("max_depth")).items():
        if path in marks:
            cleared.append({**member, "reason": marks[path]})
        else:
            pending.append({**member, "in_scope": path in editable or path not in baseline})
    total = len(baseline)
    ratio = len(changes) / total if total else 0.0
    grouped = {status: [path for path, value in changes.items() if value == status] for status in LABELS}
    return {"round": scope.get("round"), "baseline": {"commit": commit, "entries": total}, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "changes": grouped, "outside": outside, "pending": pending, "cleared": cleared, "marks": marks,
            "ratio": round(ratio, 4), "threshold": threshold, "over_threshold": ratio > threshold}


def _warning(report: dict) -> str:
    return (f"注意：改动占基线 {report['ratio']:.0%}，超过 {report['threshold']:.0%}。这么大的改动已经不像增量，"
            "和用户确认要不要拆成几轮，或者回到建模重做。")


def _overview(report: dict) -> str:
    changes = report["changes"]
    count = sum(len(items) for items in changes.values())
    return (f"改动 {count} 个（新增 {len(changes['added'])}、修改 {len(changes['modified'])}、删除 {len(changes['deleted'])}），"
            f"占基线 {report['ratio']:.0%}；范围外改动 {len(report['outside'])} 个；"
            f"待确认 {len(report['pending'])} 个，已确认不受影响 {len(report['cleared'])} 个。")


def _pending_line(item: dict) -> str:
    note = "" if item["in_scope"] else "（范围外，要改先重新圈定）"
    return f"- [ ] {clean(item['path'])}{note}：{clean(item['trail'])}"


def _outside_line(item: dict) -> str:
    return f"- [ ] {LABELS[item['status']]} {clean(item['path'])}（范围外：重新圈定并进范围，或由建模人员撤回）"


def render(report: dict) -> str:
    pointer = CHECK_JSON.as_posix()
    changes = [{"path": path, "status": status} for status, paths in report["changes"].items() for path in paths]
    lines = [f"# 传播核对（第 {report['round']} 轮）", "",
             f"基线 {report['baseline']['commit'][:8]}，条目 {report['baseline']['entries']} 个。{_overview(report)}"]
    if report["over_threshold"]:
        lines += ["", _warning(report)]
    lines += ["", f"## 改动（共 {len(changes)} 个）", ""]
    lines += capped(changes, lambda item: f"- {LABELS[item['status']]} {clean(item['path'])}", pointer=pointer) or ["没有改动。"]
    lines += ["", f"## 范围外改动（共 {len(report['outside'])} 个）", ""]
    lines += capped(report["outside"], _outside_line, pointer=pointer) or ["没有。"]
    lines += ["", f"## 待确认（共 {len(report['pending'])} 个）", ""]
    lines += capped(report["pending"], _pending_line, pointer=pointer) or ["没有。"]
    lines += ["", f"## 已确认不受影响（共 {len(report['cleared'])} 个）", ""]
    lines += capped(report["cleared"], lambda item: f"- {clean(item['path'])}：{clean(item['reason'])}", pointer=pointer) or ["没有。"]
    return "\n".join(lines) + "\n"


def _write(root: Path, report: dict) -> None:
    write_json(root / CHECK_JSON, report)
    (root / CHECK_MD).write_text(render(report), encoding="utf-8")
    _say(_overview(report))
    if report["over_threshold"]:
        _say(_warning(report))
    for line in capped(report["outside"], _outside_line, cap=STDOUT_CAP, pointer=CHECK_JSON.as_posix()):
        _say(line)
    for line in capped(report["pending"], _pending_line, cap=STDOUT_CAP, pointer=CHECK_JSON.as_posix()):
        _say(line)
    _say(f"报告：{CHECK_MD.as_posix()}。" + ("" if report["pending"] or report["outside"] else "没有待确认项了。"))


def _kept_marks(root: Path) -> dict[str, str]:
    """这一轮已经标过的「不受影响」；上一轮的报告已经归档，不会带过来。"""
    previous = _load_report(root)
    scope = read_scope(root) or {}
    marks = previous.get("marks") if previous.get("round") == scope.get("round") else None
    return {str(path): str(reason) for path, reason in marks.items()} if isinstance(marks, dict) else {}


def cmd_run(args, root: Path) -> int:
    require_lock(root, COMMAND)
    _write(root, build_report(root, args.threshold, _kept_marks(root)))
    return 0


def cmd_mark(args, root: Path) -> int:
    require_lock(root, COMMAND)
    reason = " ".join(args.reason.split())
    if not reason:
        raise ChainError("标记「不受影响」要写理由。")
    previous = _load_report(root)
    if not previous:
        raise ChainError("还没有核对报告，先跑 run。")
    known = {item["path"] for key in ("pending", "cleared") for item in previous.get(key) or []}
    paths = [PurePosixPath(raw).as_posix() for raw in args.paths]
    unknown = [path for path in paths if path not in known]
    if unknown:
        raise ChainError("这些条目不在待确认项里：" + "、".join(unknown))
    marks = _kept_marks(root)
    marks.update({path: reason for path in paths})
    _write(root, build_report(root, float(previous.get("threshold") or DEFAULT_THRESHOLD), marks))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="增量构建的传播核对")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="算差异和待确认项，写报告")
    run.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD, help="改动占比超过它就提醒，默认 0.3")
    run.set_defaults(run=cmd_run)
    mark = commands.add_parser("mark", help="标记不受影响")
    mark.add_argument("paths", nargs="+")
    mark.add_argument("--reason", required=True)
    mark.set_defaults(run=cmd_mark)
    args = parser.parse_args(argv)
    try:
        return args.run(args, Path.cwd().resolve())
    except ChainError as exc:
        _say(str(exc))
        return 2


if __name__ == "__main__":
    sys.exit(main())
