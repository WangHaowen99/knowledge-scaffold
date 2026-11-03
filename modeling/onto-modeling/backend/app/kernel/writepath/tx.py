"""统一变更事务 ChangeTx（详设 4.3、方案 C1、C6）：网页保存、Studio、MCP 写入、导入、回退都走这里。

一次事务：

1. 拿项目锁，记下基准提交（HEAD），写一条写入意图（崩溃后启动时补提交，见 recover_intents）；
2. 校验版本号（文件内容 revision，不符 409 revision_conflict）；
3. 原子写入、删除，或登记由脚本改动的路径；出错时恢复写入前的快照；
4. 同步派生文件：被改动目录的 index.md（不再由读接口顺带刷新）；
5. 只提交本次涉及的路径：在临时索引上从 HEAD 构造提交，提交说明末尾带来源 trailer，
   `update-ref` 比对旧值后再移动 HEAD（agent 在中间提交了就基于新 HEAD 重做一次），最后把真实
   索引里这几个路径对齐到新提交；agent 已暂存的其他改动不会被带进来；
6. 发出 `workspace.changed` 事件。

提交目标是扩展点（ChangeTarget）：P1 只有主线（MainlineTarget）；P2 的变更集把同一套事务写进
变更集副本，换一个 target 即可，事务本身不变。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Protocol

from ... import config
from ...governance import context as _context
from ...services.workspace.errors import WorkspaceError
from ...services.workspace.gitops import STALE_GIT_LOCK_SECONDS, _changed_paths, _git, _run_git
from ...services.workspace.layout import _content_revision, _is_workspace_script
from . import events

_log = logging.getLogger(__name__)

# 提交来源（契约 7.17 Onto-Source）
SOURCES = ("agent", "web", "studio", "mcp", "autobuild", "import", "plugin", "revert")
TRAILER_KEYS = ("Onto-Source", "Onto-Author", "Onto-Changeset", "Onto-Packs", "Onto-Node")
# 旧提交说明前缀 → 来源：兼容入口 _commit_paths 按它补 trailer
_PREFIX_SOURCES = (
    ("revert:", "revert"), ("web:", "web"), ("knowledge:", "web"), ("studio:", "studio"),
    ("mcp:", "mcp"), ("import:", "import"), ("clone:", "import"), ("research:", "plugin"),
    ("init:", "plugin"), ("chore:", "plugin"), ("feedback:", "plugin"), ("recover:", "plugin"),
)
_INDEX_LOCK_RETRIES = 10
_INDEX_LOCK_WAIT_SECONDS = 0.2
_CAS_RETRIES = 3


@dataclass(frozen=True)
class Origin:
    """这次写入从哪来、谁发起：写进提交 trailer，也带进变更事件与审计。"""

    source: str
    author: str = ""
    changeset: str | None = None
    node: str | None = None

    def __post_init__(self):
        if self.source not in SOURCES:
            raise ValueError(f"未知的写入来源：{self.source}")


# trailer 补充项提供方：插件运行时（B3a）注册，按项目返回 {"Onto-Packs": "core@1.0.0, ..."} 之类
_trailer_providers: list[Callable[[Path], dict]] = []


def register_trailer_provider(provider: Callable[[Path], dict]) -> None:
    if provider not in _trailer_providers:
        _trailer_providers.append(provider)


# 门禁链里包声明的规则（详设 4.3 第 3 步、㉖）：插件运行时登记，checker(manager, 项目, 根目录, 路径, Origin)
# → Finding[]；level=error 的放进 TxResult.findings 只提示、照常提交（F3b），发布时由发布门禁链拦。检查本身出错只记日志
_gate_checkers: list[Callable] = []


def register_gate_checker(checker: Callable) -> None:
    if checker not in _gate_checkers:
        _gate_checkers.append(checker)


def _gate_errors(manager, project_id: str, root: Path, paths: list[str], origin: Origin) -> list[dict]:
    errors = []
    for checker in list(_gate_checkers):
        try:
            findings = checker(manager, project_id, root, paths, origin) or []
        except Exception:  # noqa: BLE001
            _log.warning("项目 %s 的门禁检查没跑起来", project_id, exc_info=True)
            continue
        errors += [item for item in findings if item.get("level", "error") == "error"]
    return errors


def trailers(origin: Origin, root: Path) -> dict[str, str]:
    values = {"Onto-Source": origin.source, "Onto-Author": origin.author or "system"}
    if origin.changeset:
        values["Onto-Changeset"] = origin.changeset
    for provider in _trailer_providers:
        try:
            values.update({key: str(value) for key, value in (provider(root) or {}).items() if key in TRAILER_KEYS and value})
        except Exception:
            _log.exception("提交 trailer 提供方出错")
    if origin.node:
        values["Onto-Node"] = origin.node
    return {key: values[key] for key in TRAILER_KEYS if key in values}


def format_message(message: str, values: dict[str, str]) -> str:
    """提交说明正文 + 空行 + trailer（git interpret-trailers 认得的格式）。"""
    body = (message or "").rstrip() or "change"
    return body + "\n\n" + "".join(f"{key}: {value}\n" for key, value in values.items())


def stamped(message: str, origin: Origin, root: Path) -> str:
    """建项目、导入、克隆这类整仓提交（仓库里没有别人的改动）用：提交说明带上来源 trailer。"""
    return format_message(message, trailers(origin, Path(root)))


def origin_from_message(message: str, author: str = "") -> Origin:
    lowered = (message or "").lower()
    source = next((value for prefix, value in _PREFIX_SOURCES if lowered.startswith(prefix)), "web")
    return Origin(source, author)


# ---------------------------------------------------------------------------
# 只提交指定路径
# ---------------------------------------------------------------------------


def _head(root: Path) -> str:
    return _run_git(root, ("rev-parse", "--verify", "-q", "HEAD")).stdout.strip()


def _sync_real_index(root: Path, paths: list[str]) -> None:
    """真实索引里这几个路径对齐到新 HEAD，否则 agent 下一次 `git commit` 会把旧内容提交回去。

    agent 正在用索引（index.lock 新鲜）时短暂等待重试；锁超龄由 _git 当残留清理。
    """
    for attempt in range(_INDEX_LOCK_RETRIES):
        result = _run_git(root, ("--literal-pathspecs", "reset", "-q", "HEAD", "--", *paths))
        if result.returncode == 0:
            return
        if "index.lock" not in (result.stderr or "") or attempt == _INDEX_LOCK_RETRIES - 1:
            try:
                _git(root, "--literal-pathspecs", "reset", "-q", "HEAD", "--", *paths)
                return
            except WorkspaceError:
                _log.warning("提交后对齐真实索引失败（%s）：%s", root.name, (result.stderr or "").strip())
                return
        time.sleep(_INDEX_LOCK_WAIT_SECONDS)


def _wait_index_unlocked(root: Path) -> None:
    """提交前确认真实索引没被别的 git 操作占着：提交后要把索引对齐到新 HEAD，对不齐时 agent 下一次
    提交会把旧内容带回去，所以宁可不提交。锁超龄（没有 git 进程会持锁这么久）当残留删掉。"""
    lock = Path(root) / ".git" / "index.lock"
    for _attempt in range(_INDEX_LOCK_RETRIES):
        try:
            age = time.time() - lock.stat().st_mtime
        except OSError:
            return
        if age > STALE_GIT_LOCK_SECONDS and not lock.is_symlink():
            lock.unlink(missing_ok=True)
            _log.warning("提交前删除残留的 git 索引锁（已存在 %d 秒）：%s", int(age), lock)
            return
        time.sleep(_INDEX_LOCK_WAIT_SECONDS)
    raise WorkspaceError("git 索引正被其他 git 操作占用（index.lock），请稍后重试", 500)


def commit_paths(root: Path, paths: Iterable[str], message: str, values: dict[str, str],
                 expected_head: str | None = None) -> tuple[str, str, list[str]]:
    """只提交 paths 范围内的改动（含新增、删除）；没有改动返回 ("", HEAD, [])。

    返回 (新提交, 原 HEAD, 实际提交的路径)。调用方持项目锁。expected_head 给定且 HEAD 已移动时
    抛 409 head_moved（契约 2.4），不给定时基于新 HEAD 重做。
    """
    root = Path(root)
    paths = list(paths)
    previous = _head(root)
    if not paths:
        # 没有登记路径就是没有改动：空路径交给 git status 等于「全仓」，会把别人没提交的文件扫进来
        return "", previous, []
    changed = _changed_paths(root, paths)
    if not changed:
        return "", previous, []
    if expected_head and previous != expected_head:
        raise WorkspaceError("有新的版本，请刷新后重试", 409, "head_moved", {"head": previous})
    _wait_index_unlocked(root)
    text = format_message(message, values)
    for _attempt in range(_CAS_RETRIES):
        with tempfile.TemporaryDirectory(prefix="onto-tx-") as scratch:
            env = {**os.environ, "GIT_INDEX_FILE": str(Path(scratch) / "index")}
            if previous:
                _git(root, "read-tree", previous, env=env)
            pathspec = Path(scratch) / "paths"
            pathspec.write_bytes(b"\0".join(path.encode("utf-8") for path in changed))
            _git(root, "--literal-pathspecs", "add", "-A", "--pathspec-from-file", str(pathspec),
                 "--pathspec-file-nul", env=env)
            tree = _git(root, "write-tree", env=env).strip()
            message_file = Path(scratch) / "message"
            message_file.write_text(text, encoding="utf-8")
            parents = ("-p", previous) if previous else ()
            commit = _git(root, "commit-tree", tree, *parents, "-F", str(message_file)).strip()
        args = ("update-ref", "-m", text.splitlines()[0][:200], "HEAD", commit)
        if previous:
            args += (previous,)
        result = _run_git(root, args)
        if result.returncode == 0:
            _sync_real_index(root, changed)
            return commit, previous, changed
        # agent 在中间提交了：基于新 HEAD 重做（本次路径的内容取自工作区，不受影响）
        current = _head(root)
        if expected_head or current == previous:
            raise WorkspaceError(f"git update-ref 失败: {result.stderr.strip()}", 500)
        previous = current
    raise WorkspaceError("保存期间版本反复变化，请稍后重试", 409, "head_moved", {"head": _head(root)})


# ---------------------------------------------------------------------------
# 写入意图（崩溃恢复，详设 8.3 措施 2）
# ---------------------------------------------------------------------------


def _intents_dir() -> Path:
    return Path(config.DATA_DIR) / "writepath" / "intents"


def _intent_path(project_id: str, tx_id: str) -> Path:
    return _intents_dir() / f"{project_id}--{tx_id}.json"


def _write_intent(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def recover_intents(manager) -> dict:
    """启动时（会话开始之前）处理上次停机留下的写入意图：已落盘没提交的补提交，其余丢弃。

    返回启动自检报告的一项（O-35）。
    """
    directory = _intents_dir()
    recovered, dropped, failed = [], 0, []
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            project_id = str(data["project_id"])
            root = manager.project_root(project_id)
        except Exception:
            path.unlink(missing_ok=True)
            dropped += 1
            continue
        if data.get("rollback"):
            # 整批写入（一轮的合入、撤回已合入的变更集）：没提交完就整批回到主线，不补提交半截（审查第 6 条）
            try:
                with manager._project_lock(project_id):
                    _restore_from_head(root, data.get("paths") or [])
                dropped += 1
            except Exception as exc:
                failed.append(project_id)
                _log.warning("回滚中断的整批写入失败（%s）：%s", project_id, exc)
                continue
            path.unlink(missing_ok=True)
            continue
        try:
            with manager._project_lock(project_id):
                origin = Origin(data.get("source") or "web", data.get("author") or "")
                values = trailers(origin, root)
                commit, _previous, changed = commit_paths(
                    root, data.get("paths") or [], f"recover: 补提交中断的写入（{data.get('summary') or origin.source}）", values,
                )
            if commit:
                recovered.append({"project_id": project_id, "commit": commit, "paths": changed[:10]})
            else:
                dropped += 1
        except Exception as exc:
            failed.append(project_id)
            _log.warning("补提交中断的写入失败（%s）：%s", project_id, exc)
            continue
        path.unlink(missing_ok=True)
    if failed:
        result, tone = "failed", "warn"
    elif recovered:
        result, tone = "committed", "fix"
    else:
        result, tone = "ok", "ok"
    detail = (
        f"补提交 {len(recovered)} 次中断的写入" if recovered else "没有中断的写入"
    ) + (f"；{len(failed)} 个项目处理失败" if failed else "")
    return {
        "id": "write_intents", "title": "中断的写入", "detail": detail, "result": result, "tone": tone,
        "projects": recovered, "failed": failed,
    }


def _restore_from_head(root: Path, paths: list[str]) -> None:
    """这些路径回到 HEAD 的样子：HEAD 里有的取回来，没有的删掉。已经提交了的（崩在提交之后）就是原样。"""
    root = Path(root)
    keep, gone = [], []
    for rel in paths:
        exists = _run_git(root, ("--literal-pathspecs", "cat-file", "-e", f"HEAD:{rel}")).returncode == 0
        (keep if exists else gone).append(rel)
    if keep:
        _git(root, "--literal-pathspecs", "checkout", "HEAD", "--", *keep)
    for rel in gone:
        target = root / rel
        if target.is_file() or target.is_symlink():
            target.unlink()
    if gone:
        _run_git(root, ("--literal-pathspecs", "reset", "-q", "HEAD", "--", *gone))


# ---------------------------------------------------------------------------
# 提交目标（扩展点）
# ---------------------------------------------------------------------------


class ChangeTarget(Protocol):
    """事务写到哪：P1 是主线；P2 的变更集副本实现同一接口（root 指向副本，提交进变更集分支）。"""

    kind: str
    root: Path

    def head(self) -> str: ...

    def commit(self, paths: list[str], message: str, values: dict[str, str],
               expected_head: str | None) -> tuple[str, str, list[str]]: ...


@dataclass
class MainlineTarget:
    root: Path
    kind: str = "mainline"

    def head(self) -> str:
        return _head(self.root)

    def commit(self, paths, message, values, expected_head=None):
        return commit_paths(self.root, paths, message, values, expected_head)


# ---------------------------------------------------------------------------
# 事务
# ---------------------------------------------------------------------------


@dataclass
class TxResult:
    commit: str
    previous: str
    paths: list[str] = field(default_factory=list)
    # 插件声明的提交门禁没过的问题（只提示，F3b）
    findings: list[dict] = field(default_factory=list)


class ChangeTx:
    """with ChangeTx(manager, project_id, Origin("web", "alice")) as tx: tx.write(...); tx.commit("web: 保存 X")

    事务内的文件操作都经 tx，出错（含提交失败）时恢复到写入前；commit 之后的异常不回滚已提交的内容。
    """

    def __init__(self, manager, project_id: str, origin: Origin, *, summary: str = "",
                 target: ChangeTarget | None = None, expected_head: str | None = None,
                 rollback_on_crash: bool = False):
        self.manager = manager
        self.project_id = project_id
        self.origin = origin
        self.summary = summary
        self._target = target
        self.expected_head = expected_head
        # 整批写入（一轮的合入）：崩溃后启动恢复整批回滚，而不是把已落盘的部分补提交
        self.rollback_on_crash = rollback_on_crash
        self.root: Path | None = None
        self.base_head = ""
        self.result: TxResult | None = None
        self._paths: list[str] = []
        self._snapshots: dict[str, tuple[bool, bytes]] = {}
        self._lock = None
        self._scope = None
        self._intent: Path | None = None
        self._tx_id = f"{int(time.time() * 1000)}-{os.getpid()}-{id(self) & 0xFFFF:x}"

    # ---- 生命周期 ----

    def __enter__(self) -> "ChangeTx":
        self._lock = self.manager._project_lock(self.project_id)
        self._lock.__enter__()
        try:
            self.root = self.manager.project_root(self.project_id)
            self.target = self._target or MainlineTarget(self.root)
            self.base_head = self.target.head()
            from .scope import ScopeGuard

            self._scope = ScopeGuard(self.manager, self.project_id, self.origin)
        except BaseException:
            self._lock.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if exc_type is not None and self.result is None:
                self._rollback()
        finally:
            if self._intent is not None:
                self._intent.unlink(missing_ok=True)
            self._lock.__exit__(exc_type, exc, tb)
        return False

    # ---- 路径与校验 ----

    def resolve(self, rel_path: str) -> tuple[Path, str]:
        target = self.manager._resolve_inside(self.root, rel_path)
        rel = target.relative_to(self.root.resolve()).as_posix()
        # git 自己的目录一律不经写入事务改（审查第 1 条）：.git/config 的 fsmonitor、hooks 会在后端主机上执行命令
        if rel == ".git" or rel.startswith(".git/"):
            raise WorkspaceError("该路径不允许写入", 403, "path_forbidden", {"path": rel_path})
        return target, rel

    def check_revision(self, rel_path: str, base_revision: str, details: Callable[[], dict] | None = None) -> None:
        """base_revision 为空不检查（新建或明确覆盖，契约 1.7）；文件不存在也不检查。"""
        if not base_revision:
            return
        target, rel = self.resolve(rel_path)
        if not target.is_file():
            return
        current = _content_revision(target.read_bytes())
        if current != base_revision:
            raise WorkspaceError(
                "文件已被他人或助手修改，请刷新对比后再保存", 409, "revision_conflict",
                {"target": rel, "expected": base_revision, "actual": current, **(details() if details else {})},
            )

    def _record_intent(self) -> None:
        data = {
            "project_id": self.project_id, "tx": self._tx_id, "source": self.origin.source,
            "author": self.origin.author, "summary": self.summary, "base_head": self.base_head,
            "paths": list(self._paths), "started_at": time.time(),
            **({"rollback": True, "changeset": self.origin.changeset} if self.rollback_on_crash else {}),
        }
        if self._intent is None:
            self._intent = _intent_path(self.project_id, self._tx_id)
        _write_intent(self._intent, data)

    def _check_scope(self, rel: str) -> None:
        """写入范围只读（方案 C10）：落在助手的写入范围里就 423，什么都还没写。"""
        if self._scope is not None and rel not in self._paths:
            self._scope.check([rel])

    def _remember(self, rel: str, path: Path) -> None:
        self._check_scope(rel)
        if rel not in self._snapshots:
            self._snapshots[rel] = self.manager._snapshot(path) if not path.is_symlink() else (False, b"")
        if rel not in self._paths:
            self._paths.append(rel)
            self._record_intent()

    # ---- 文件操作 ----

    def write(self, rel_path: str, data: bytes | str) -> str:
        """原子写入；返回仓库相对路径。"""
        target, rel = self.resolve(rel_path)
        self._remember(rel, target)
        payload = data.encode("utf-8") if isinstance(data, str) else data
        self.manager._atomic_write(target, payload)
        return rel

    def remove(self, rel_path: str) -> str:
        target, rel = self.resolve(rel_path)
        self._remember(rel, target)
        if target.is_file() or target.is_symlink():
            target.unlink()
        return rel

    def touch(self, *rel_paths: str, snapshot: bool = True) -> None:
        """登记将由脚本或助手函数改动的路径（目录也可以）：先记快照（目录不记），提交时一并带上。"""
        for rel_path in rel_paths:
            target, rel = self.resolve(rel_path)
            if snapshot and (target.is_file() or not target.exists()):
                self._remember(rel, target)
            elif rel not in self._paths:
                self._check_scope(rel)
                self._paths.append(rel)
                self._record_intent()

    def keep(self) -> None:
        """不提交、也不回滚：文件留在盘上，由调用方随后在另一个事务里合成一个提交。"""
        self.result = TxResult("", self.base_head, [])

    @property
    def paths(self) -> list[str]:
        return list(self._paths)

    # ---- 派生文件与提交 ----

    def _derive(self) -> None:
        """派生文件跟着改动刷新并进同一个提交（C6）：脚本描述符、被改动目录的 index.md。"""
        self._sync_script_descriptors()
        refresh = getattr(self.manager, "refresh_indexes_for", None)
        if refresh is None:
            return
        for rel in refresh(self.root, self._paths):
            if rel not in self._paths:
                self._paths.append(rel)
        if self._intent is not None:
            self._record_intent()  # 派生文件也记进意图：崩溃回滚时一起回到主线

    def _sync_script_descriptors(self) -> None:
        """改了 ontology/scripts/*.py 时同名描述符跟着刷新、进同一个提交。

        描述符实现取本项目锁定版本的契约库（方案 A16）：和 agent 在工作区里跑的是同一份。
        """
        scripts = [Path(rel) for rel in self._paths if _is_workspace_script(Path(rel))]
        if not scripts:
            return
        from ...services.packs import contracts

        script_descriptor = contracts.module_for(self.root, "script_descriptor")
        for script in scripts:
            self.touch(script.with_suffix(".md").as_posix())
        script_descriptor.sync_workspace_descriptors(self.root / "ontology")

    def commit(self, message: str) -> TxResult:
        if self.result is not None:
            return self.result
        from ...governance import telemetry

        self._derive()
        # 包声明的提交门禁只提示（F3b：建模过程中检查不拦，发布时由发布门禁链拦），问题随结果交给调用方
        errors = _gate_errors(self.manager, self.project_id, self.root, list(self._paths), self.origin)
        values = trailers(self.origin, self.root)
        with telemetry.span("commit", "commit", source=self.origin.source, target=self.target.kind,
                            paths=len(self._paths)):
            commit, previous, changed = self.target.commit(self._paths, message, values, self.expected_head)
        self.result = TxResult(commit, previous, changed, errors[:50])
        if commit:
            telemetry.count("onto_commits_total", source=self.origin.source, target=self.target.kind)
            events.publish(events.WorkspaceChanged(
                project_id=self.project_id, commit=commit, previous=previous, source=self.origin.source,
                author=self.origin.author, paths=tuple(changed), target=self.target.kind,
                changeset=self.origin.changeset, trace_id=_context.get("trace_id") or None,
            ))
        return self.result

    def _rollback(self) -> None:
        for rel, snapshot in reversed(list(self._snapshots.items())):
            try:
                self.manager._restore_snapshot(self.root / rel, snapshot)
            except OSError:
                _log.warning("写入失败后恢复 %s 失败", rel)


def commit_compat(root: Path, paths: list[str], message: str) -> str:
    """旧入口 `_commit_paths` 的实现：调用方已持锁、已写好文件，只按说明前缀补来源 trailer 再提交。"""
    origin = origin_from_message(message, _author_from_message(message))
    commit, _previous, _changed = commit_paths(root, paths, message, trailers(origin, Path(root)))
    if commit:
        events.publish(events.WorkspaceChanged(
            project_id=Path(root).name, commit=commit, previous=_previous, source=origin.source,
            author=origin.author, paths=tuple(_changed), trace_id=_context.get("trace_id") or None,
        ))
    return commit


def _author_from_message(message: str) -> str:
    head = (message or "").splitlines()[0] if message else ""
    marker = " by "
    return head.rsplit(marker, 1)[1].strip() if marker in head else ""
