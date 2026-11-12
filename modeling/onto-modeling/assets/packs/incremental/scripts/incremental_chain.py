#!/usr/bin/env python3
"""增量构建的传播链（ADR-0085）：一个条目变了，还有谁要跟着看。

按引用反向走（谁引用了它），默认 2 层，最多 5 层；每一项都能讲成一条路径，
如「订单 → 下单（引用对象）→ 下单金额上限（引用操作）」。

条目的解析都用 core 的：ontology_checker 读条目、对象引用和测试数据表，onto_metamodel 解析链接，
关系的口径和 impact_analysis 一致（测试核对两边的直接引用方）。这里只多一份反向索引：逐层调
analyze_impact 每层都要重新扫全部条目和测试数据，而且它不看规则、指标。

用法（只看传播链，不写文件）：
    python3 .claude/scripts/incremental_chain.py ontology/object/订单.md [--depth 2] [--json]
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
from collections import deque
from pathlib import Path, PurePosixPath

import onto_metamodel as mm
from ontology_checker import _extract_object_refs, _parse_mock_tables, load_ontology

DEFAULT_DEPTH = 2
MAX_DEPTH = 5
# 给助手读的 md 和标准输出最多列这么多条，完整清单在 json 里
LIST_CAP = 50
STDOUT_CAP = 20

STATE_DIR = Path("incremental")
SCOPE_FILE = STATE_DIR / "scope.json"
# 范围管的条目目录，和内核守卫（_guard_common.CONFINED_ONTOLOGY_DIRS）一致
ENTRY_DIRS = ("object", "operation", "logic", "rule", "metric", "action", "data-mock", "testcase", "scripts")
_DOC_KINDS = {"object": "object", "operation": "operation", "logic": "operation", "rule": "rule",
              "metric": "metric", "action": "action"}
_LINK_RELATIONS = {"object": "引用对象", "operation": "引用操作", "rule": "引用规则", "metric": "引用指标",
                   "action": "引用动作"}
_SUB_OPERATION_RE = re.compile(r"调用\[\[([^\]]+)\]\]逻辑文档")


def _emit(text=""):
    """结果写到标准输出：单独的输出通道，不受应用日志配置影响。"""
    output = logging.Logger("incremental_chain.out", logging.INFO)
    output.addHandler(logging.StreamHandler(sys.stdout))
    output.info(text)


class ChainError(Exception):
    """给助手看的错误：打印后退出，不带调用栈。"""


def clamp_depth(value: int | None) -> int:
    return max(1, min(MAX_DEPTH, DEFAULT_DEPTH if value is None else int(value)))


def is_entry(rel: str) -> bool:
    parts = rel.split("/")
    return (len(parts) >= 3 and parts[0] == "ontology" and parts[1] in ENTRY_DIRS
            and parts[-1].lower() != mm.INDEX_FILE and not parts[-1].startswith("."))


def entry_name(rel: str) -> str:
    return PurePosixPath(rel).stem


def _doc_kind(rel: str) -> str | None:
    parts = rel.split("/")
    return _DOC_KINDS.get(parts[1]) if len(parts) >= 3 else None


class Graph:
    """条目之间的反向引用。

    stubs：文件已经不在（删除的种子）或还没有（新增的种子）的条目，别人的链接照样指得到它。
    """

    def __init__(self, root: Path, stubs=()):
        self.root = root
        self.referrers: dict[str, dict[str, str]] = {}
        ontology = root / "ontology"
        docs = load_ontology(ontology)
        self.paths = {stem: doc.path.relative_to(root).as_posix() for stem, doc in docs.items()}
        self.kinds = {stem: doc.doc_type for stem, doc in docs.items()}
        for rel in stubs:
            kind = _doc_kind(rel)
            if kind:
                self.paths.setdefault(entry_name(rel), rel)
                self.kinds.setdefault(entry_name(rel), kind)
        self._index_documents(docs)
        self._index_testcases(ontology)
        self._index_mocks(ontology)
        self._index_scripts(ontology)

    def _add(self, target: str | None, referrer: str, relation: str) -> None:
        """同一对条目只记一条关系：先记的更具体（调用子操作、字段引用排在普通链接前面）。"""
        path = self.paths.get(target or "")
        if path and path != referrer:
            self.referrers.setdefault(path, {}).setdefault(referrer, relation)

    def _index_documents(self, docs: dict) -> None:
        names = set(self.paths)
        for stem, doc in docs.items():
            source = self.paths[stem]
            if doc.doc_type == "operation":
                for raw in _SUB_OPERATION_RE.findall(doc.sections.get("逻辑描述", "")):
                    self._add(mm.resolve(raw, names), source, "调用子操作")
                for row in doc.action_rows:
                    text = row.get("动作名称", "").strip()
                    link = mm.parse_link(text)
                    self._add(mm.resolve(link, names) if link else mm.resolve(text, names), source, "调用动作")
            elif doc.doc_type == "object":
                for attribute, targets in _extract_object_refs(doc):
                    for target in targets:
                        self._add(mm.resolve(target, names), source, f"引用对象（字段：{attribute}）")
            for link in doc.wiki_links:
                target = mm.resolve(link, names)
                if target:
                    self._add(target, source, _LINK_RELATIONS[self.kinds[target]])

    def _index_testcases(self, ontology: Path) -> None:
        directory = ontology / "testcase"
        for path in sorted(directory.glob("*.md")) if directory.is_dir() else ():
            if self.kinds.get(path.stem) == "operation":
                self._add(path.stem, path.relative_to(self.root).as_posix(), "场景验证")

    def _index_mocks(self, ontology: Path) -> None:
        directory = ontology / "data-mock"
        objects = [stem for stem, kind in self.kinds.items() if kind == "object"]
        for path in sorted(directory.glob("*.md")) if directory.is_dir() else ():
            rel = path.relative_to(self.root).as_posix()
            if self.kinds.get(path.stem) == "object":
                self._add(path.stem, rel, "测试数据")
            try:
                tables = _parse_mock_tables(path)
            except (OSError, UnicodeDecodeError):
                continue
            values = []
            for rows in tables.values():
                for row in rows:
                    values.extend(str(value) for value in row.values())
            cells = "\n".join(values)
            for stem in objects:
                if stem in cells:
                    self._add(stem, rel, "测试数据")

    def _index_scripts(self, ontology: Path) -> None:
        directory = ontology / "scripts"
        if not directory.is_dir():
            return
        try:
            from script_descriptor import load_scripts_state

            bindings = load_scripts_state(ontology).report.bindings
        except Exception:  # noqa: BLE001 - 绑定读不出来时按同名算，传播链不因为一个坏脚本算不出来
            bindings = {}
        for path in sorted(directory.glob("*.py")):
            operation = bindings.get(path.stem) or path.stem
            if self.kinds.get(operation) == "operation":
                self._add(operation, path.relative_to(self.root).as_posix(), "脚本实现")

    def walk(self, seeds, max_depth: int | None = None) -> list[dict]:
        """从种子按引用反向走；一个条目只出现一次（走到它的最短路径），有环也会停。"""
        depth = clamp_depth(max_depth)
        seen: dict[str, tuple[int, str]] = {}
        queue: deque[str] = deque()
        for seed in seeds:
            if seed not in seen:
                seen[seed] = (0, entry_name(seed))
                queue.append(seed)
        chain: list[dict] = []
        while queue:
            current = queue.popleft()
            level, trail = seen[current]
            if level >= depth:
                continue
            for referrer, relation in sorted(self.referrers.get(current, {}).items()):
                if referrer in seen:
                    continue
                arrow = "→" if trail.endswith("）") else " →"
                item_trail = f"{trail}{arrow} {entry_name(referrer)}（{relation}）"
                seen[referrer] = (level + 1, item_trail)
                queue.append(referrer)
                chain.append({"path": referrer, "name": entry_name(referrer), "depth": level + 1, "via": current,
                              "relation": relation, "include": True, "trail": item_trail})
        return chain


# ---- 版本库 ----


def _git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(  # NOSONAR(S4721) 固定的 git 参数列表，不经 shell
            ["git", "-c", "safe.directory=*", *args], cwd=root, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ChainError(f"运行 git 失败：{exc}") from exc
    if result.returncode != 0:
        raise ChainError(f"git {args[0]} 失败：{result.stderr.decode('utf-8', 'replace').strip()[:300]}")
    return result.stdout.decode("utf-8", "replace")


def mainline_head(root: Path) -> str:
    """主线的 head：助手副本是 git worktree，主线是公共目录的 HEAD。"""
    common = (root / _git(root, "rev-parse", "--git-common-dir").strip()).resolve()
    return _git(root, "--git-dir", str(common), "rev-parse", "HEAD").strip()


def baseline_entries(root: Path, commit: str) -> list[str]:
    listed = _git(root, "ls-tree", "-r", "-z", "--name-only", commit, "--", "ontology")
    return sorted(rel for rel in listed.split("\0") if rel and is_entry(rel))


def changes_since(root: Path, commit: str) -> dict[str, str]:
    """基线和当前工作区比：{条目: added|modified|deleted}。"""
    names = {"A": "added", "M": "modified", "D": "deleted", "T": "modified"}
    fields = _git(root, "diff", "--name-status", "--no-renames", "-z", commit, "--", "ontology").split("\0")
    changes = {path: names.get(status[:1], "modified") for status, path in zip(fields[::2], fields[1::2])
               if status and is_entry(path)}
    for rel in _git(root, "ls-files", "--others", "--exclude-standard", "-z", "--", "ontology").split("\0"):
        if rel and is_entry(rel):
            changes[rel] = "added"
    return dict(sorted(changes.items()))


# ---- 本轮范围 ----


def read_scope(root: Path) -> dict | None:
    try:
        data = json.loads((root / SCOPE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def round_open(root: Path, scope: dict | None) -> bool:
    """这一轮还开着：status 是 active，基线之后没发布过（和内核守卫同一口径，ADR-0091）。"""
    if not scope or scope.get("status") != "active":
        return False
    commit = str((scope.get("baseline") or {}).get("commit") or "")
    line = _git(root, "for-each-ref", "--sort=-creatordate", "--count=1", "--format=%(*objectname) %(objectname)",
                "refs/tags/release").split()
    if not commit or not line or line[0].startswith(commit):
        return True
    try:
        _git(root, "merge-base", "--is-ancestor", commit, line[0])
    except ChainError:
        return True
    return False


def uploaded_materials(root: Path) -> list[str]:
    """触发这一轮的新资料（内核记在主线工作区的 .onto/run/activation.json）；读不到返回空。"""
    common = (root / _git(root, "rev-parse", "--git-common-dir").strip()).resolve()
    try:
        data = json.loads((common.parent / ".onto" / "run" / "activation.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    latch = data.get("incremental") if isinstance(data, dict) else None
    return [str(item) for item in latch.get("files") or []] if isinstance(latch, dict) else []


def require_lock(root: Path, command: str) -> None:
    """这些脚本只在对应命令里跑：范围文件能不能写，靠这把锁和内核守卫一起管。"""
    if not (root / ".ontology-state" / f"lock-{command}.json").is_file():
        raise ChainError(f"这一步只能在 /{command} 里运行：先按命令的 Step 0 拿到 {command} 的锁。")


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def clean(text) -> str:
    """写进 md 的文字：`[ ]` 是待处理标记，只能由核对脚本的待确认项产生；不能换行，免得破坏列表。"""
    return " ".join(str(text).split()).replace("[ ]", "[]")


def capped(items: list, render, *, cap: int = LIST_CAP, pointer: str = "") -> list[str]:
    lines = [render(item) for item in items[:cap]]
    if len(items) > cap:
        lines.append(f"还有 {len(items) - cap} 条没列出，完整清单见 {pointer}。")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="算条目的传播链（不写文件）")
    parser.add_argument("paths", nargs="+", help="条目路径，如 ontology/object/订单.md")
    parser.add_argument("--depth", type=int, default=None, help=f"层数，默认 {DEFAULT_DEPTH}，最多 {MAX_DEPTH}")
    parser.add_argument("--json", action="store_true", help="输出完整 json")
    args = parser.parse_args(argv)
    root = Path.cwd().resolve()
    paths = [PurePosixPath(raw).as_posix() for raw in args.paths]
    chain = Graph(root, stubs=paths).walk(paths, args.depth)
    if args.json:
        _emit(json.dumps(chain, ensure_ascii=False, indent=2))
        return 0
    _emit(f"传播链共 {len(chain)} 条（{clamp_depth(args.depth)} 层以内）。")
    for line in capped(chain, lambda item: f"- 第 {item['depth']} 层：{item['path']}：{clean(item['trail'])}",
                       cap=STDOUT_CAP, pointer="--json 的输出"):
        _emit(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
