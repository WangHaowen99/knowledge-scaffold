#!/usr/bin/env python3
"""增量构建的范围文件（ADR-0085）：起草、确认生效、结束一轮，以及范围内修改的收尾核对。

    draft   算传播链，写 incremental/scope.draft.{json,md}，等用户确认（/incremental-scope）
    confirm 把草稿变成这一轮的范围：归档上一轮，写 incremental/scope.{json,md}（/incremental-scope）
    close   结束本轮，status 改成 closed，范围外恢复可写（/incremental-scope）
    applied 核对每个种子都处理了，写 incremental/applied.json（/incremental-apply）

范围要等 scope.json 合入主线才生效，所以确认前只写草稿：等用户回复时这一轮的改动也会合入主线，
没确认的范围不能先生效。上一轮还没结束就重新圈定是在扩大范围：基线沿用，种子和可改条目并进来，
前面改过的内容照样要过传播核对；要另起一轮先「结束本轮」。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path, PurePosixPath

from incremental_chain import (
    LIST_CAP,
    SCOPE_FILE,
    STATE_DIR,
    STDOUT_CAP,
    ChainError,
    Graph,
    baseline_entries,
    capped,
    changes_since,
    clamp_depth,
    clean,
    is_entry,
    mainline_head,
    read_scope,
    require_lock,
    write_json,
)

COMMAND = "incremental-scope"
APPLY_COMMAND = "incremental-apply"
ACTIONS = {"add": "新增", "modify": "修改", "delete": "删除"}
DRAFT_FILE = STATE_DIR / "scope.draft.json"
DRAFT_MD = STATE_DIR / "scope.draft.md"
SCOPE_MD = STATE_DIR / "scope.md"
APPLIED_FILE = STATE_DIR / "applied.json"
HISTORY_DIR = STATE_DIR / "history"
# 新一轮要归档的文件：上一轮的修改记录和核对报告不能留到这一轮
ROUND_FILES = ("scope.json", "scope.md", "applied.json", "check.md", "check.json")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _seeds(raw: list[list[str]], root: Path) -> list[dict]:
    problems, seeds, seen = [], [], set()
    for action, path, reason in raw:
        rel = PurePosixPath(path).as_posix()
        exists = (root / rel).exists()
        if action not in ACTIONS:
            problems.append(f"{rel}：动作只能是 add、modify、delete，不是 {action}")
        elif not is_entry(rel):
            problems.append(f"{rel}：不是本体条目（ontology/ 下的对象、操作、规则、指标、动作、测试数据、用例、脚本）")
        elif action == "add" and exists:
            problems.append(f"{rel}：已经有了，要改用 modify")
        elif action != "add" and not exists:
            problems.append(f"{rel}：不存在，新建用 add")
        elif not reason.strip():
            problems.append(f"{rel}：要写理由")
        elif rel in seen:
            problems.append(f"{rel}：重复了")
        else:
            seen.add(rel)
            seeds.append({"action": action, "path": rel, "reason": reason.strip()})
    if problems:
        raise ChainError("种子有问题：\n" + "\n".join(f"- {item}" for item in problems))
    if not seeds:
        raise ChainError("至少要有一个种子：--seed <add|modify|delete> <条目路径> <理由>")
    return seeds


def _editable(seeds: list[dict], chain: list[dict]) -> list[str]:
    """种子加放进范围的传播链；脚本和它的同名描述符是一起改的。"""
    paths = {seed["path"] for seed in seeds} | {item["path"] for item in chain if item.get("include", True)}
    for rel in list(paths):
        parts = PurePosixPath(rel)
        if parts.parts[1] == "scripts":
            paths |= {parts.with_suffix(".py").as_posix(), parts.with_suffix(".md").as_posix()}
    return sorted(paths)


def _chain_line(item: dict) -> str:
    excluded = "" if item.get("include", True) else "（不放进范围）"
    return f"- 第 {item['depth']} 层：{item['path']}{excluded}：{clean(item['trail'])}"


def _seed_lines(seeds: list[dict]) -> list[str]:
    return [f"- {ACTIONS[seed['action']]} {seed['path']}：{clean(seed['reason'])}" for seed in seeds]


def _render(title: str, head: list[str], seeds: list[dict], chain: list[dict], pointer: str) -> str:
    lines = [f"# {title}", "", *head, "", f"## 种子（共 {len(seeds)} 个）", "", *_seed_lines(seeds), "",
             f"## 传播链（共 {len(chain)} 条）", ""]
    lines += capped(chain, _chain_line, cap=LIST_CAP, pointer=pointer) or ["没有其他条目引用这些种子。"]
    return "\n".join(lines) + "\n"


def _render_scope(root: Path, scope: dict) -> None:
    state = "进行中" if scope["status"] == "active" else "已结束"
    head = [f"目标：{clean(scope['goal'])}",
            f"基线：{scope['baseline']['commit'][:8]}，条目 {scope['baseline']['entries']} 个；传播 {scope['max_depth']} 层以内。",
            f"可改条目 {len(scope['editable'])} 个，基线里的其他条目这一轮只读，新增条目不受限。"]
    (root / SCOPE_MD).write_text(_render(f"增量范围（第 {scope['round']} 轮，{state}）", head, scope["seeds"],
                                         scope["chain"], SCOPE_FILE.as_posix()), encoding="utf-8")


def cmd_draft(args, root: Path) -> int:
    require_lock(root, COMMAND)
    seeds = _seeds(args.seed or [], root)
    depth = clamp_depth(args.depth)
    chain = Graph(root, stubs=[seed["path"] for seed in seeds]).walk([seed["path"] for seed in seeds], depth)
    write_json(root / DRAFT_FILE, {"goal": args.goal, "seeds": seeds, "chain": chain, "max_depth": depth})
    head = [f"目标：{clean(args.goal)}", f"传播 {depth} 层以内。确认后这些条目可改，基线里的其他条目这一轮只读。"]
    (root / DRAFT_MD).write_text(_render("增量范围草稿（等确认）", head, seeds, chain, DRAFT_FILE.as_posix()),
                                 encoding="utf-8")
    print(f"草稿写好了：种子 {len(seeds)} 个，传播链 {len(chain)} 条（{depth} 层以内）。详情见 {DRAFT_MD.as_posix()}。")
    for line in capped(chain, _chain_line, cap=STDOUT_CAP, pointer=DRAFT_FILE.as_posix()):
        print(line)
    return 0


def _archive(root: Path, previous: dict) -> None:
    target = root / HISTORY_DIR / str(previous.get("round") or 0)
    for name in ROUND_FILES:
        source = root / STATE_DIR / name
        if source.is_file():
            target.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(target / name))


def _merge_previous(previous: dict, seeds: list[dict], chain: list[dict]) -> tuple[list[dict], list[dict]]:
    new_seeds = {seed["path"] for seed in seeds}
    merged_seeds = [seed for seed in previous.get("seeds") or [] if seed.get("path") not in new_seeds] + seeds
    known = {item["path"] for item in chain} | {seed["path"] for seed in merged_seeds}
    merged_chain = [item for item in previous.get("chain") or [] if item.get("path") not in known] + chain
    return merged_seeds, merged_chain


def cmd_confirm(args, root: Path) -> int:
    require_lock(root, COMMAND)
    try:
        draft = json.loads((root / DRAFT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ChainError(f"没有可确认的草稿（{DRAFT_FILE.as_posix()}），先跑 draft。") from exc
    excluded = {PurePosixPath(raw).as_posix() for raw in args.exclude or []}
    unknown = excluded - {item["path"] for item in draft["chain"]}
    if unknown:
        raise ChainError("这些条目不在传播链里，不能排除：" + "、".join(sorted(unknown)))
    chain = [{**item, "include": item["path"] not in excluded} for item in draft["chain"]]
    seeds = draft["seeds"]
    previous = read_scope(root)
    enlarging = bool(previous) and previous.get("status") == "active" and isinstance(previous.get("baseline"), dict)
    if enlarging:
        baseline = previous["baseline"]
        seeds, chain = _merge_previous(previous, seeds, chain)
        editable = sorted(set(_editable(seeds, chain)) | set(previous.get("editable") or []))
    else:
        commit = mainline_head(root)
        baseline = {"commit": commit, "entries": len(baseline_entries(root, commit))}
        editable = _editable(seeds, chain)
    scope = {"round": int((previous or {}).get("round") or 0) + 1, "goal": draft["goal"], "baseline": baseline,
             "seeds": seeds, "chain": chain, "editable": editable, "status": "active",
             "max_depth": draft["max_depth"], "confirmed_at": _now()}
    if previous:
        _archive(root, previous)
    write_json(root / SCOPE_FILE, scope)
    _render_scope(root, scope)
    (root / DRAFT_FILE).unlink(missing_ok=True)
    (root / DRAFT_MD).unlink(missing_ok=True)
    note = "（上一轮没结束，这次是扩大范围：基线沿用）" if enlarging else ""
    print(f"第 {scope['round']} 轮范围写好了{note}：可改条目 {len(editable)} 个。这一轮对话结束合入主线后生效。")
    return 0


def cmd_close(_args, root: Path) -> int:
    require_lock(root, COMMAND)
    scope = read_scope(root)
    if scope is None:
        raise ChainError("还没有范围文件，不用结束。")
    if scope.get("status") != "active":
        print(f"第 {scope.get('round')} 轮已经结束了。")
        return 0
    scope["status"] = "closed"
    scope["closed_at"] = _now()
    write_json(root / SCOPE_FILE, scope)
    _render_scope(root, scope)
    print(f"第 {scope['round']} 轮结束了：这一轮对话合入主线后，范围外的条目恢复可写。")
    return 0


def _seed_handled(root: Path, seed: dict, changes: dict[str, str]) -> bool:
    exists = (root / seed["path"]).exists()
    if seed["action"] == "add":
        return exists
    if seed["action"] == "delete":
        return not exists
    return changes.get(seed["path"]) == "modified"


def cmd_applied(args, root: Path) -> int:
    require_lock(root, APPLY_COMMAND)
    scope = read_scope(root)
    if scope is None or scope.get("status") != "active":
        raise ChainError("没有进行中的范围，先 /incremental-scope 圈定。")
    seeds = {seed["path"]: seed for seed in scope.get("seeds") or []}
    skips = {PurePosixPath(path).as_posix(): reason.strip() for path, reason in args.skip or []}
    unknown = [path for path in skips if path not in seeds]
    if unknown:
        raise ChainError("这些不是本轮的种子，不用跳过：" + "、".join(unknown))
    empty = [path for path, reason in skips.items() if not reason]
    if empty:
        raise ChainError("跳过要写理由：" + "、".join(empty))
    changes = changes_since(root, scope["baseline"]["commit"])
    handled, skipped, missing = [], [], []
    for path, seed in seeds.items():
        if path in skips:
            skipped.append({"path": path, "action": seed["action"], "reason": skips[path]})
        elif _seed_handled(root, seed, changes):
            handled.append({"path": path, "action": seed["action"], "status": changes.get(path, "unchanged")})
        else:
            missing.append(f"{ACTIONS[seed['action']]} {path}")
    if missing:
        print(f"还有 {len(missing)} 个种子没处理（处理掉，或用 --skip <条目> <理由> 说明为什么不改）：")
        for line in missing[:STDOUT_CAP]:
            print(f"- {line}")
        if len(missing) > STDOUT_CAP:
            print(f"还有 {len(missing) - STDOUT_CAP} 个没列出，种子清单见 {SCOPE_FILE.as_posix()}。")
        return 1
    write_json(root / APPLIED_FILE, {"round": scope["round"], "at": _now(), "handled": handled, "skipped": skipped})
    print(f"第 {scope['round']} 轮的种子都处理了：已改 {len(handled)} 个，跳过 {len(skipped)} 个。下一步 /incremental-check。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="增量构建的范围文件")
    commands = parser.add_subparsers(dest="command", required=True)
    draft = commands.add_parser("draft", help="算传播链，写草稿")
    draft.add_argument("--goal", required=True)
    draft.add_argument("--seed", action="append", nargs=3, metavar=("动作", "条目", "理由"))
    draft.add_argument("--depth", type=int, default=None)
    draft.set_defaults(run=cmd_draft)
    confirm = commands.add_parser("confirm", help="草稿生效")
    confirm.add_argument("--exclude", action="append", help="传播链里不放进范围的条目，可写多次")
    confirm.set_defaults(run=cmd_confirm)
    commands.add_parser("close", help="结束本轮").set_defaults(run=cmd_close)
    applied = commands.add_parser("applied", help="核对种子都处理了，写修改记录")
    applied.add_argument("--skip", action="append", nargs=2, metavar=("条目", "理由"))
    applied.set_defaults(run=cmd_applied)
    args = parser.parse_args(argv)
    try:
        return args.run(args, Path.cwd().resolve())
    except ChainError as exc:
        print(str(exc))
        return 2


if __name__ == "__main__":
    sys.exit(main())
