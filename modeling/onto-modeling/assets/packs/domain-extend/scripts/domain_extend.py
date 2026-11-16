#!/usr/bin/env python3
"""领域包扩展（ADR-0099）：锁定底座、看底座清单、记下新增条目、核对底座有没有被改。

    bind     锁定底座：写 ext/domain-extend/scope.json（内核 confine 读它）、base.json、base.md（/domain-extend-bind）
    status   底座锁定生效没有、底座是哪个包、被改了几处（只读）
    catalog  列底座条目，可按类型、关键字筛（只读）
    added    列出新增条目和它们引用了哪些底座条目，写 ext/domain-extend/added.json（/domain-extend-add）
    check    核对底座，写 ext/domain-extend/check.md、check.json（/domain-extend-check）
    restore  把被改、被删的底座条目写回导入时的内容（/domain-extend-check，用户确认后）

底座 = 导入领域本体包的那次提交（最早带上 .imported-package/ 的提交）里 ontology/ 下的文件，派生的 index.md 和隐藏文件除外。
从 git 历史取，不读任何助手能改的文件；核对按文件内容逐字比，网页、Studio、MCP 上的人工改动也算。
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath

PACK = "domain-extend"
STATE_DIR = Path("ext") / PACK
SCOPE_FILE = STATE_DIR / "scope.json"
BASE_JSON = STATE_DIR / "base.json"
BASE_MD = STATE_DIR / "base.md"
ADDED_JSON = STATE_DIR / "added.json"
CHECK_MD = STATE_DIR / "check.md"
CHECK_JSON = STATE_DIR / "check.json"
IMPORTED_DIR = ".imported-package"
BIND_COMMAND = "domain-extend-bind"
ADD_COMMAND = "domain-extend-add"
CHECK_COMMAND = "domain-extend-check"
# 内核守卫（_guard_common.CONFINED_ONTOLOGY_DIRS）管的条目目录；solver 不在里面，只靠核对
CONFINED_DIRS = ("object", "operation", "logic", "rule", "metric", "action", "data-mock", "testcase", "scripts")
KIND_LABELS = {"object": "对象", "operation": "操作", "logic": "操作", "rule": "规则", "metric": "指标",
               "scripts": "脚本", "solver": "求解契约", "action": "动作", "data-mock": "测试数据", "testcase": "用例"}
KIND_ALIASES = {label: key for key, label in KIND_LABELS.items() if key != "logic"}
# 给助手读的标准输出、md 最多列这么多条，完整清单在 json 里
STDOUT_CAP = 20
LIST_CAP = 50
_MANIFEST_RE = re.compile(r"^(id|name|version):\s*(.+?)\s*$", re.MULTILINE)
_LINK_RE = re.compile(r"\[\[([^\[\]\n]+?)\]\]")

_out = logging.Logger("domain_extend", logging.INFO)
_handler = logging.StreamHandler()
_out.addHandler(_handler)


def say(text: object = "", *, error: bool = False) -> None:
    """结果写标准输出（助手读它），error=True 写标准错误；每次取当前的流，测试替换了也跟着走。"""
    _handler.setStream(sys.stderr if error else sys.stdout)
    _out.info(text)


class DomainError(Exception):
    """给助手看的错误：打印后退出，不带调用栈。"""


# ---- git ----


def _git(root: Path, *args: str, data: bytes | None = None) -> bytes:
    try:
        result = subprocess.run(  # NOSONAR(S4721) 固定的 git 参数列表，不经 shell
            ["git", "-c", "safe.directory=*", "-c", "core.quotepath=false", *args],
            cwd=root, input=data, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise DomainError(f"运行 git 失败：{exc}") from exc
    if result.returncode != 0:
        raise DomainError(f"git {args[0]} 失败：{result.stderr.decode('utf-8', 'replace').strip()[:300]}")
    return result.stdout


def _text(root: Path, *args: str) -> str:
    return _git(root, *args).decode("utf-8", "replace")


def _common(root: Path) -> Path:
    return (root / _text(root, "rev-parse", "--git-common-dir").strip()).resolve()


def mainline_head(root: Path) -> str:
    """主线的 head：助手副本是 git worktree，主线是公共目录的 HEAD。"""
    return _text(root, "--git-dir", str(_common(root)), "rev-parse", "HEAD").strip()


def _is_ancestor(root: Path, older: str, newer: str) -> bool:
    try:
        _git(root, "merge-base", "--is-ancestor", older, newer)
    except DomainError:
        return False
    return True


def latest_release(root: Path) -> str:
    """最近一次发布打的标签指向的提交；没发布过返回空。"""
    line = _text(root, "for-each-ref", "--sort=-creatordate", "--count=1",
                 "--format=%(*objectname) %(objectname)", "refs/tags/release").split()
    return line[0] if line else ""


def released_after(root: Path, commit: str) -> bool:
    """commit 之后发布过（和内核守卫同一口径，ADR-0091：发布就算这一轮结束）。"""
    release = latest_release(root)
    return bool(release) and not release.startswith(commit) and _is_ancestor(root, commit, release)


# ---- 底座 ----


def is_base_file(rel: str) -> bool:
    parts = rel.split("/")
    return (len(parts) >= 3 and parts[0] == "ontology" and parts[-1].lower() != "index.md"
            and not any(part.startswith(".") for part in parts))


def is_confined(rel: str) -> bool:
    return is_base_file(rel) and rel.split("/")[1] in CONFINED_DIRS


def kind_of(rel: str) -> str:
    parts = rel.split("/")
    return KIND_LABELS.get(parts[1], "其他") if len(parts) >= 3 else "其他"


def entry_name(rel: str) -> str:
    return PurePosixPath(rel).stem


def import_commit(root: Path) -> str:
    """导入领域本体包的那次提交：最早带上 .imported-package/ 的提交。"""
    found = _text(root, "log", "--format=%H", "--reverse", "--", IMPORTED_DIR).split()
    if not found:
        raise DomainError("这个项目不是从已发布的领域本体包导入的（没有 .imported-package/），领域包扩展没有底座。"
                          "请从知识包 Hub 导入领域本体包，或上传领域本体包的发布包新建项目。")
    return found[0]


def tree_files(root: Path, commit: str) -> dict[str, str]:
    """提交里 ontology/ 下的条目文件：{路径: blob}。"""
    listed = _text(root, "ls-tree", "-r", "-z", commit, "--", "ontology")
    out = {}
    for row in listed.split("\0"):
        meta, _, rel = row.partition("\t")
        fields = meta.split()
        if len(fields) == 3 and fields[1] == "blob" and is_base_file(rel):
            out[rel] = fields[2]
    return out


def base_files(root: Path) -> tuple[str, dict[str, str]]:
    commit = import_commit(root)
    files = tree_files(root, commit)
    if not files:
        raise DomainError(f"导入提交 {commit[:8]} 里没有本体条目，底座是空的。")
    return commit, files


def package_identity(root: Path, commit: str) -> dict:
    """底座包的 id、名称、版本：读导入提交里留底的清单（发布时写的六个必填字段）。"""
    try:
        text = _text(root, "show", f"{commit}:{IMPORTED_DIR}/manifest.yaml")
    except DomainError:
        return {}
    out = {}
    for key, raw in _MANIFEST_RE.findall(text):
        value = raw
        if raw.startswith('"'):
            try:
                value = json.loads(raw)
            except ValueError:
                value = raw.strip('"')
        out.setdefault(key, str(value).strip("'"))
    return out


def package_text(package: dict) -> str:
    name = package.get("name") or package.get("id") or "（清单里没有名称）"
    return f"「{name}」" + (f" {package['version']}" if package.get("version") else "")


def current_files(root: Path) -> list[str]:
    """工作区里现在的条目文件（含没提交的）。"""
    ontology = root / "ontology"
    if not ontology.is_dir():
        return []
    return sorted(rel for rel in (path.relative_to(root).as_posix() for path in ontology.rglob("*") if path.is_file())
                  if is_base_file(rel))


def _hash(root: Path, paths: list[str]) -> dict[str, str]:
    """工作区文件按内容算的 blob（和 git 里同一口径，逐字比）。"""
    if not paths:
        return {}
    data = "\n".join(paths).encode("utf-8") + b"\n"
    listed = _git(root, "hash-object", "--no-filters", "--stdin-paths", data=data).decode().split()
    return dict(zip(paths, listed))


def mainline_scope(root: Path) -> dict | None:
    """主线上的范围文件（内核只认这一份放宽）。"""
    try:
        raw = _text(root, "--git-dir", str(_common(root)), "show", f"{mainline_head(root)}:{SCOPE_FILE.as_posix()}")
        data = json.loads(raw)
    except (DomainError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def lock_state(root: Path, commit: str) -> tuple[bool, str]:
    """(底座锁定生效没有, 说明)。和内核守卫同一口径：主线上的范围文件 active、基线之后没发布过。"""
    scope = mainline_scope(root)
    if not scope or scope.get("status") != "active":
        return False, "还没锁定（或锁定还没合入主线）"
    if (scope.get("base") or {}).get("import_commit") != commit:
        return False, "范围文件和导入提交对不上，要重新锁定"
    baseline = str((scope.get("baseline") or {}).get("commit") or "")
    if baseline and released_after(root, baseline):
        return False, "发布之后锁定自动解除了，要重新锁定"
    return True, "已锁定"


def require_lock(root: Path, command: str) -> None:
    """写状态文件的子命令只在对应命令里跑：范围文件能不能写，靠这把锁和内核守卫一起管。"""
    if not (root / ".ontology-state" / f"lock-{command}.json").is_file():
        raise DomainError(f"这一步只能在 /{command} 里运行：先按命令的 Step 0 拿到 {command} 的锁。")


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def clean(text) -> str:
    """写进 md 的文字：`[ ]` 是待处理标记，只能由核对产生；不能换行，免得破坏列表。"""
    return " ".join(str(text).split()).replace("[ ]", "[]")


def capped(items: list, render, *, cap: int, pointer: str) -> list[str]:
    lines = [render(item) for item in items[:cap]]
    if len(items) > cap:
        lines.append(f"还有 {len(items) - cap} 条没列出，完整清单见 {pointer}。")
    return lines


def _counts(paths) -> str:
    tally: dict[str, int] = {}
    for rel in paths:
        tally[kind_of(rel)] = tally.get(kind_of(rel), 0) + 1
    return "、".join(f"{kind} {count}" for kind, count in tally.items()) or "无"


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ---- 锁定 ----


def bind(root: Path) -> None:
    require_lock(root, BIND_COMMAND)
    commit, base = base_files(root)
    package = package_identity(root, commit)
    head = mainline_head(root)
    if released_after(root, commit):
        # 发布过：内核把基线之后的发布当作这一轮结束，基线改成主线 head，已有的新增条目列进 editable，只读的还是底座
        baseline = head
        editable = sorted(rel for rel in tree_files(root, head) if is_confined(rel) and rel not in base)
    else:
        baseline, editable = commit, []
    scope = {
        "pack": PACK, "status": "active", "bound_at": _now(), "covers": head,
        "baseline": {"commit": baseline, "entries": len(base)}, "editable": editable,
        "base": {"import_commit": commit, "package": package, "entries": len(base)},
    }
    entries = [{"path": rel, "kind": kind_of(rel), "name": entry_name(rel), "blob": blob} for rel, blob in sorted(base.items())]
    write_json(root / SCOPE_FILE, scope)
    write_json(root / BASE_JSON, {"package": package, "import_commit": commit, "entries": entries})
    lines = [
        "# 底座（领域本体包）", "",
        f"- 领域本体包：{package_text(package)}",
        f"- 导入提交：{commit[:8]}",
        f"- 底座条目：{len(base)} 个（{_counts(base)}）",
        f"- 锁定时间：{scope['bound_at']}", "",
        "底座条目一律不改、不删、不改名，也不加属性、章节或关系；新功能用新增条目实现，用 [[链接]] 引用下面的条目。", "",
        "## 条目", "",
        *capped(entries, lambda item: f"- {item['kind']} {clean(item['name'])}（{item['path']}）", cap=LIST_CAP,
                pointer=BASE_JSON.as_posix()),
    ]
    (root / BASE_MD).write_text("\n".join(lines) + "\n", encoding="utf-8")
    say(f"底座已锁定：领域本体包{package_text(package)}，导入提交 {commit[:8]}，底座条目 {len(base)} 个（{_counts(base)}）。")
    say("这一轮合入主线后锁定对之后的修改一直有效（发布后要重新锁定）；这一轮里也已经不能改底座条目。新增条目不受限。")


# ---- 只读查询 ----


def status(root: Path) -> None:
    commit, base = base_files(root)
    locked, reason = lock_state(root, commit)
    violations = integrity(root, base)
    say(f"领域本体包{package_text(package_identity(root, commit))}，导入提交 {commit[:8]}，底座条目 {len(base)} 个。")
    say(f"底座锁定：{reason}。" + ("" if locked else "运行 /domain-extend-bind 锁定。"))
    say(f"底座被改动：{len(violations)} 处。" + ("运行 /domain-extend-check 核对。" if violations else ""))


def catalog(root: Path, kind: str | None, keyword: str | None, as_json: bool) -> None:
    commit, base = base_files(root)
    wanted = KIND_LABELS.get(kind or "", kind) if kind else None
    if wanted and wanted not in KIND_ALIASES:
        raise DomainError(f"类型只能是：{'、'.join(KIND_ALIASES)}（或 object、operation、rule、metric、scripts、solver）")
    items = [{"path": rel, "kind": kind_of(rel), "name": entry_name(rel)} for rel in sorted(base)
             if (not wanted or kind_of(rel) == wanted) and (not keyword or keyword in rel)]
    if as_json:
        say(json.dumps(items, ensure_ascii=False, indent=2))
        return
    say(f"底座条目 {len(items)} 个（导入提交 {commit[:8]}）。")
    for line in capped(items, lambda item: f"- {item['kind']} {item['name']}（{item['path']}）", cap=STDOUT_CAP,
                       pointer="--json 的输出，或加 --kind、--q 缩小范围"):
        say(line)


# ---- 新增 ----


def _links(root: Path, rel: str) -> list[str]:
    if not rel.endswith(".md"):
        return []
    try:
        text = (root / rel).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    names = []
    for raw in _LINK_RE.findall(text):
        name = PurePosixPath(raw.split("|")[0].split("#")[0].strip().rstrip("\\")).name
        name = re.sub(r"\.md$", "", name, flags=re.IGNORECASE).strip()
        if name and name not in ("无", "-", "—") and name not in names:
            names.append(name)
    return names


def extension(root: Path, base: dict[str, str]) -> list[dict]:
    """新增条目：工作区里有、底座里没有的条目文件，带它对底座的引用和指不到的链接。"""
    present = current_files(root)
    all_names = {entry_name(rel) for rel in present}
    base_names = {entry_name(rel): rel for rel in base}
    out = []
    for rel in present:
        if rel in base:
            continue
        links = _links(root, rel)
        out.append({
            "path": rel, "kind": kind_of(rel), "name": entry_name(rel),
            "refs_to_base": [name for name in links if name in base_names],
            "unresolved": [name for name in links if name not in all_names],
            "shadows": base_names.get(entry_name(rel)),
        })
    return out


def added(root: Path) -> None:
    require_lock(root, ADD_COMMAND)
    commit, base = base_files(root)
    items = extension(root, base)
    violations = integrity(root, base)
    if not items:
        say("还没有新增条目：新功能要落成新建的对象、操作、规则、指标或脚本。")
    else:
        write_json(root / ADDED_JSON, {"at": _now(), "import_commit": commit, "entries": items})
        say(f"新增条目 {len(items)} 个（{_counts(item['path'] for item in items)}），已记到 {ADDED_JSON.as_posix()}。")
        for line in capped(items, _added_line, cap=STDOUT_CAP, pointer=ADDED_JSON.as_posix()):
            say(line)
    if violations:
        say(f"注意：底座有 {len(violations)} 处被改动，发布前要撤回（/domain-extend-check）。")


def _added_line(item: dict) -> str:
    refs = "、".join(item["refs_to_base"][:5]) or "没有引用底座"
    extra = f"；和底座条目 {item['shadows']} 重名" if item["shadows"] else ""
    return f"- {item['kind']} {item['name']}（{item['path']}）→ {refs}{extra}"


# ---- 核对 ----


def integrity(root: Path, base: dict[str, str]) -> list[dict]:
    """底座条目和导入时比：被改的、被删的（内容搬到别处的记下新位置）。"""
    present = current_files(root)
    current = _hash(root, present)
    by_blob: dict[str, str] = {}
    for rel, blob in current.items():
        by_blob.setdefault(blob, rel)
    out = []
    for rel, blob in sorted(base.items()):
        now = current.get(rel)
        if now == blob:
            continue
        if now is None:
            moved = by_blob.get(blob)
            out.append({"path": rel, "kind": kind_of(rel), "change": "moved" if moved else "deleted", "to": moved})
        else:
            out.append({"path": rel, "kind": kind_of(rel), "change": "modified", "to": None})
    return out


_CHANGE_TEXT = {"modified": "被修改", "deleted": "被删除", "moved": "被移动或改名"}


def _violation_line(item: dict) -> str:
    where = f"到 {item['to']}" if item.get("to") else ""
    return f"- [ ] 底座{item['kind']} {item['path']} {_CHANGE_TEXT[item['change']]}{where}：撤回到导入时的内容"


def check(root: Path) -> None:
    require_lock(root, CHECK_COMMAND)
    commit, base = base_files(root)
    package = package_identity(root, commit)
    locked, reason = lock_state(root, commit)
    violations = integrity(root, base)
    items = extension(root, base)
    shadows = [item for item in items if item["shadows"]]
    unresolved = [item for item in items if item["unresolved"]]
    pending = len(violations) + len(shadows) + (0 if locked else 1)
    write_json(root / CHECK_JSON, {
        "at": _now(), "import_commit": commit, "package": package, "locked": locked, "lock_reason": reason,
        "base_entries": len(base), "violations": violations, "extension": items,
    })
    lines = [
        "# 底座核对", "",
        f"- 领域本体包：{package_text(package)}，导入提交 {commit[:8]}，底座条目 {len(base)} 个",
        f"- 新增条目：{len(items)} 个（{_counts(item['path'] for item in items)}）",
        f"- 核对时间：{_now()}", "",
        "## 结论", "",
        "底座和导入时逐字一致，可以发布。" if not pending else f"有 {pending} 项要处理，处理完重新核对才能发布。", "",
        "## 底座锁定", "",
        "已锁定。" if locked else f"- [ ] 底座没有锁定（{reason}）：运行 /domain-extend-bind", "",
        f"## 底座改动（{len(violations)}）", "",
        *(capped(violations, _violation_line, cap=LIST_CAP, pointer=CHECK_JSON.as_posix()) or ["没有。"]), "",
        f"## 和底座重名的新增条目（{len(shadows)}）", "",
        *(capped(shadows, lambda item: f"- [ ] {item['path']} 和底座条目 {item['shadows']} 同名：新增条目换个名字",
                 cap=LIST_CAP, pointer=CHECK_JSON.as_posix()) or ["没有。"]), "",
        f"## 提醒：新增条目里指不到的链接（{len(unresolved)}，不挡发布）", "",
        *(capped(unresolved, lambda item: f"- {item['path']}：{clean('、'.join(item['unresolved'][:5]))}",
                 cap=LIST_CAP, pointer=CHECK_JSON.as_posix()) or ["没有。"]),
    ]
    (root / CHECK_MD).write_text("\n".join(lines) + "\n", encoding="utf-8")
    say(f"底座核对：底座条目 {len(base)} 个，被改动 {len(violations)} 处，新增条目 {len(items)} 个，"
        f"和底座重名 {len(shadows)} 个；底座锁定：{reason}。报告在 {CHECK_MD.as_posix()}。")
    for line in capped(violations, _violation_line, cap=STDOUT_CAP, pointer=CHECK_JSON.as_posix()):
        say(line)


def restore(root: Path) -> None:
    """被改、被删的底座条目写回导入时的字节；不删任何文件（搬到别处的那份留着，核对会列成重名）。"""
    require_lock(root, CHECK_COMMAND)
    commit, base = base_files(root)
    violations = integrity(root, base)
    if not violations:
        say("底座没有被改动，不用撤回。")
        return
    for item in violations:
        data = _git(root, "show", f"{commit}:{item['path']}")
        target = root / item["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    say(f"已把 {len(violations)} 个底座条目写回导入时的内容：")
    for line in capped(violations, lambda item: f"- {item['path']}（{_CHANGE_TEXT[item['change']]}）", cap=STDOUT_CAP,
                       pointer="python3 .claude/scripts/domain_extend.py check 的报告"):
        say(line)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="领域包扩展：底座锁定与核对")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("bind", help="锁定底座（/domain-extend-bind）")
    sub.add_parser("status", help="底座锁定和改动情况")
    listing = sub.add_parser("catalog", help="列底座条目")
    listing.add_argument("--kind", help="对象、操作、规则、指标、脚本、求解契约")
    listing.add_argument("--q", help="路径或名称里的关键字")
    listing.add_argument("--json", action="store_true", help="输出完整 json")
    sub.add_parser("added", help="记下新增条目（/domain-extend-add）")
    sub.add_parser("check", help="核对底座（/domain-extend-check）")
    sub.add_parser("restore", help="把被改的底座条目写回导入时的内容（/domain-extend-check）")
    args = parser.parse_args(argv)
    root = Path.cwd().resolve()
    try:
        if args.action == "bind":
            bind(root)
        elif args.action == "status":
            status(root)
        elif args.action == "catalog":
            catalog(root, args.kind, args.q, args.json)
        elif args.action == "added":
            added(root)
        elif args.action == "check":
            check(root)
        else:
            restore(root)
    except DomainError as exc:
        say(str(exc), error=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
