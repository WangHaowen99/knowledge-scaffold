"""工作区核心：项目目录与元数据、项目锁与原子写、建项目与进度、改名归档、克隆、删除与清扫、
回收站、资产挂载、启动维护。文件与资料、版本、导入导出分别在 files、versions、transfer 里，
由 WorkspaceManager 组合；发布在 services/publish，求解契约在 services/solver。
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows development fallback
    fcntl = None

from ..name_conflict import MAX_DISPLAY_NAME_LENGTH, detect_name_conflict
from .errors import NameConflictError, WorkspaceError
from .files import FilesMixin
from .gitops import (
    _commit_paths,
    _ensure_gitignore_entries,
    _git,
    _untrack_state_commit,
    remove_git_locks,
)
from .layout import (
    DEFAULT_GITIGNORE,
    META_FILE,
    PURGE_MARKER,
    _default_engine,
    _is_run_state,
    _slugify,
)
from .transfer import TransferMixin
from .versions import VersionsMixin

_log = logging.getLogger(__name__)

# 删除时 Windows 上刚被杀的进程要片刻才释放目录句柄，rename/rmtree 短暂重试
_DELETE_RETRIES = 5
_DELETE_RETRY_SECONDS = 0.2

_PROJECT_LOCKS: dict[str, threading.RLock] = {}
_PROJECT_LOCKS_GUARD = threading.Lock()
# 本线程已持有的项目锁（键同 _PROJECT_LOCKS）与嵌套深度
_HELD_LOCKS = threading.local()
# 同名校验到项目落盘之间不能被另一个同名请求插队
_NAME_CLAIM_LOCK = threading.Lock()


def _research_review_for(root: Path):
    """资料分析验收状态以工具箱的 research_review.py 为单一事实源，按项目锁定的版本加载（方案 A16）。

    项目进度（本文件）、CLI 阶段前置（state_helper）与报告检查器（research_checker）
    共用同一份失效判定，避免"目录里有报告就算完成"在多处各自实现（issue #362）。
    """
    from ..packs import contracts

    return contracts.module_for(root, "research_review")


class WorkspaceCore:
    def __init__(self, data_dir: Path, assets_dir: Path):
        from ..packs.runtime import PackRuntime

        self.data_dir = Path(data_dir)
        self.workspaces_dir = self.data_dir / "workspaces"
        self.assets_dir = Path(assets_dir)
        self.workspaces_dir.mkdir(parents=True, exist_ok=True)
        # 插件运行时：包仓库、装配目录都在同一个数据目录下（方案 D1、D2）
        self.packs = PackRuntime(self.data_dir, self.assets_dir)

    def _lock_path(self, project_id: str) -> Path:
        """Return a lock outside the project so it is never included in exports/git."""
        key = hashlib.sha256(project_id.encode("utf-8")).hexdigest()
        return self.workspaces_dir.parent / ".workspace-locks" / f"{key}.lock"

    @contextmanager
    def _project_lock(self, project_id: str):
        """Serialize project mutations and metadata reads across managers/processes.

        同一线程可以嵌套持锁（写入事务 ChangeTx 在已持锁的调用方里开启）：文件锁只在最外层加，
        内层再对同一文件 flock 会和外层的打开文件描述冲突，自己等自己。
        """
        lock_path = self._lock_path(project_id)
        key = str(lock_path.resolve())
        with _PROJECT_LOCKS_GUARD:
            lock = _PROJECT_LOCKS.setdefault(key, threading.RLock())
        lock.acquire()
        held = _HELD_LOCKS.__dict__.setdefault("depth", {})
        nested = held.get(key, 0) > 0
        held[key] = held.get(key, 0) + 1
        fd = None
        try:
            if not nested:
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            if fd is not None:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
            held[key] -= 1
            if not held[key]:
                del held[key]
            lock.release()

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        """Write a file beside its destination and replace it atomically."""
        path.parent.mkdir(parents=True, exist_ok=True)
        old_mode = path.stat().st_mode & 0o777 if path.is_file() else None
        fd, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary_path = Path(temporary)
        try:
            with os.fdopen(fd, "wb") as stream:
                fd = None
                # mkstemp 建的是 0600：新文件按进程 umask 给权限，agent 用户（同组）才能接着改（方案 E10）
                os.fchmod(stream.fileno(), old_mode if old_mode is not None else 0o666 & ~_current_umask())
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, path)
            try:
                directory_fd = os.open(path.parent, os.O_RDONLY)
            except OSError:
                directory_fd = None
            if directory_fd is not None:
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            if fd is not None:
                os.close(fd)
            temporary_path.unlink(missing_ok=True)

    @classmethod
    def _atomic_write_text(cls, path: Path, content: str) -> None:
        cls._atomic_write(path, content.encode("utf-8"))

    @staticmethod
    def _snapshot(path: Path) -> tuple[bool, bytes]:
        exists = path.is_file()
        return (exists, path.read_bytes() if exists else b"")

    @classmethod
    def _restore_snapshot(cls, path: Path, snapshot: tuple[bool, bytes]) -> None:
        existed, data = snapshot
        if existed:
            cls._atomic_write(path, data)
        else:
            path.unlink(missing_ok=True)

    # ---- 项目 CRUD ----

    def create_project(self, name: str, owner: str = "", background: str = "", *,
                       packs: list[str] | None = None, **extra) -> dict:
        """建项目：目录、资产挂载、gitignore、知识包目录与基线提交。extra 是额外写进元数据的字段
        （goal、build_mode、created_from 等，契约 P-02）；packs 是新建向导选的能力包与行业包
        （None = 缺省启用的能力包，写进锁文件）。"""
        name = (name or "").strip()
        if not name:
            raise WorkspaceError("项目名称不能为空", 400, "project_name_required")
        project_id = f"{_slugify(name)}-{uuid.uuid4().hex[:8]}"
        root = self.workspaces_dir / project_id
        root.mkdir(parents=True)

        self._mount_assets(root, packs)
        self._copy_resource_template(root)
        self._write_gitignore(root)
        # 初始化知识包目录，并写入占位 index，便于前端直接浏览完整结构。
        knowledge_dirs = (
            "research", "reports", "ontology/object", "ontology/operation",
            "ontology/rule", "ontology/metric",
            "ontology/data-mock", "ontology/testcase", "ontology/scripts",
            "ontology/diagrams", "ontology/diagrams-summary", "ontology/mermaid",
            "ontology/excel",
        )
        for rel in knowledge_dirs:
            (root / rel).mkdir(parents=True, exist_ok=True)
        self._refresh_indexes(root)

        meta = {
            "id": project_id,
            "name": name,
            "created_at": int(time.time()),
            "owner": owner or "",
            "background": (background or "").strip(),
            "engine": _default_engine(),
            **{key: value for key, value in extra.items() if value is not None},
        }
        self._atomic_write_text(
            root / META_FILE, json.dumps(meta, ensure_ascii=False, indent=2)
        )

        _git(root, "init")
        _git(root, "config", "user.name", "Onto Modeling")
        _git(root, "config", "user.email", "onto-modeling@example.invalid")
        from ...kernel.writepath import Origin, stamped

        _git(root, "add", "-A")
        _git(root, "commit", "-m", stamped("init: 任务工作区基线", Origin("plugin", owner), root), "--allow-empty")
        return meta

    @staticmethod
    def _dir_has_entries(directory: Path) -> bool:
        if not directory.is_dir():
            return False
        return any(
            entry
            for entry in directory.iterdir()
            if not entry.name.startswith(".") and entry.name.lower() != "index.md"
        )

    @classmethod
    def _project_progress(cls, root: Path) -> dict:
        """项目列表上的阶段进度：节点与完成判定都来自主流程定义 flow.yaml（方案 D12），
        与 state_helper、GET /flow 同一份。"""
        from ..flow import model as flow_model

        _research_review = _research_review_for(root)
        review = _research_review.review_status(root)
        # 验收只管"进入对象提取之前"：报告没通过、对象也还没提取时才挡住后续阶段。
        # 对象一旦提取，资料分析按已越过处理，只留待核验的提醒——与 state_helper
        # 的"已到达的最高阶段优先"同口径（flow.yaml 的 research_done 探针）。
        progress = flow_model.progress(root)
        research_blocking = (
            review["status"] in (_research_review.PENDING, _research_review.FAILED, _research_review.UNVERIFIABLE)
            and not progress["probes"].get("object")
        )

        def count_md(rel: str) -> int:
            directory = root / rel
            if not directory.is_dir():
                return 0
            return sum(
                1 for f in directory.glob("*.md") if f.name.lower() != "index.md"
            )

        return {
            "stages_done": progress["done"],
            "stages_total": progress["total"],
            # 资料分析的验收状态（none/pending/failed/passed）+ 计数与下一步提示。
            # 只给结论和条数，错误明细由 research_checker 在工作区内输出。
            # blocking：未验收是否还挡着后续阶段；前端据此决定阶段完成态，不自行推断。
            "research_review": {
                "status": review["status"],
                "reason": review["reason"],
                "blocking": research_blocking,
                "errors": len(review["errors"]),
                "manual_pending": len(review["manual_pending"]),
                "hint": _research_review.hint(review),
            },
            "objects": count_md("ontology/object"),
            "operations": count_md("ontology/operation") + count_md("ontology/logic"),
        }

    def list_projects(self) -> list[dict]:
        projects = []
        for entry in sorted(self.workspaces_dir.iterdir()):
            meta_path = entry / META_FILE
            if meta_path.is_file():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    continue
                meta.update(self._project_progress(entry))
                projects.append(meta)
        projects.sort(key=lambda p: p.get("created_at", 0), reverse=True)
        return projects

    # ---- 任务名称同名校验 ----

    def _project_metas(self, exclude_id: str = "") -> list[dict]:
        """全部任务的元数据，不算阶段进度（查重只看名称与归属）。"""
        metas = []
        for entry in self.workspaces_dir.iterdir():
            if entry.name == exclude_id:
                continue
            try:
                meta = json.loads((entry / META_FILE).read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(meta, dict):
                metas.append(meta)
        return metas

    def check_name_available(
        self,
        name: str,
        viewer: tuple[str, bool] | None,
        *,
        original_name: str = "",
        importing: bool = False,
        check_length: bool = True,
        exclude_id: str = "",
    ) -> None:
        """新建/导入/重命名/克隆前的同名校验：去首尾空白、全角半角与大小写差异都算同名。

        查重范围是当前用户在列表里看得到的任务（自己的；管理员是全部）：viewer 为
        （用户名, 是否管理员），鉴权关闭时传 None，全部任务都算。重名抛 NameConflictError（409），
        附候选名。exclude_id 是重命名时的任务自身：只改自己名称的大小写或全半角不算重名。
        """
        from .. import ownership  # ownership 依赖工作区错误类型，在函数里导入避免循环

        name = (name or "").strip()
        if not name:
            return  # 空名由 create_project 统一报错
        if check_length and len(name) > MAX_DISPLAY_NAME_LENGTH:
            raise WorkspaceError(
                f"任务名称不能超过 {MAX_DISPLAY_NAME_LENGTH} 个字符",
                400,
                "project_name_too_long",
            )
        metas = self._project_metas(exclude_id)
        if viewer is not None:
            metas = [meta for meta in metas if ownership.visible(meta, *viewer)]
        conflict = detect_name_conflict(
            (meta.get("name") for meta in metas), name, original_name or name
        )
        if conflict is None:
            return
        if importing:
            message = (
                f"任务名称「{name}」已存在，不可重复导入。请选择候选名称或输入新名称后继续导入。"
            )
        else:
            message = f"任务名称「{name}」已存在，请选择候选名称或输入新名称。"
        raise NameConflictError(message, conflict)

    @contextmanager
    def name_claim(self, name: str, viewer: tuple[str, bool] | None, **options):
        """同名校验通过后执行块内的落盘操作；校验与落盘一起串行，同名并发只成功一个。"""
        with _NAME_CLAIM_LOCK:
            self.check_name_available(name, viewer, **options)
            yield

    def set_owner(self, project_id: str, owner: str) -> tuple[str, dict]:
        """改项目所有者（用户名由调用方校验），返回（原所有者, 新元数据）；读写在同一把项目锁内。"""
        with self._project_lock(project_id):
            root = self.project_root(project_id)
            previous = str(self._read_meta(root).get("owner") or "")
            return previous, self._update_meta_unlocked(root, owner=owner)

    def get_project(self, project_id: str) -> dict:
        with self._project_lock(project_id):
            return self._read_meta(self.project_root(project_id))

    @staticmethod
    def _read_meta(root: Path) -> dict:
        """不加锁读元数据：元数据只经原子替换写入，读不到半截文件；已持项目锁的调用方用它。"""
        try:
            return json.loads((root / META_FILE).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkspaceError(
                "项目元数据暂时不可用，请稍后重试",
                503,
                "project_metadata_unavailable",
            ) from exc

    @classmethod
    def _write_meta(cls, root: Path, meta: dict) -> None:
        cls._atomic_write_text(root / META_FILE, json.dumps(meta, ensure_ascii=False, indent=2))

    def project_root(self, project_id: str) -> Path:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", project_id):
            raise WorkspaceError("项目不存在", 404, "project_not_found")
        root = self.workspaces_dir / project_id
        if not (root / META_FILE).is_file():
            raise WorkspaceError("项目不存在", 404, "project_not_found")
        return root

    # ---- 生命周期扩展 ----

    def _update_meta(self, project_id: str, **fields) -> dict:
        with self._project_lock(project_id):
            root = self.project_root(project_id)
            return self._update_meta_unlocked(root, **fields)

    def _update_meta_unlocked(self, root: Path, **fields) -> dict:
        meta = self._read_meta(root)
        meta.update(fields)
        self._write_meta(root, meta)
        return meta

    def rename_project(self, project_id: str, name: str) -> dict:
        name = (name or "").strip()
        if not name:
            raise WorkspaceError("项目名称不能为空")
        return self._update_meta(project_id, name=name)

    def set_archived(self, project_id: str, archived: bool) -> dict:
        return self._update_meta(project_id, archived=bool(archived))

    # 克隆/导入时不带过去的顶层条目：资产由本服务重新挂载，git 历史不迁移
    CLONE_SKIP_TOP = {".claude", "CLAUDE.md", "README.md", ".git", ".ontology-state", META_FILE}
    CLONE_NAME_SUFFIX = "（副本）"  # 契约 P-08：缺省「原名（副本）」

    def clone_name(self, project_id: str) -> str:
        """克隆的默认名称「原名（副本）」；原名过长时截断，保证不超过名称长度上限。"""
        name = self.get_project(project_id)["name"]
        keep = MAX_DISPLAY_NAME_LENGTH - len(self.CLONE_NAME_SUFFIX)
        return name[:keep].rstrip() + self.CLONE_NAME_SUFFIX

    def clone_project(
        self, project_id: str, owner: str = "", name: str = "", *, include_history: bool = False,
    ) -> dict:
        """克隆项目：新项目只归调用者（契约 P-08）。

        include_history：带上原项目的 git 历史（发布标签不带，那是原项目的发布记录）；
        否则只复制当前文件，新项目从一个新的基线提交开始。
        """
        source_root = self.project_root(project_id)
        source_meta = self.get_project(project_id)
        extra = {key: source_meta.get(key) for key in ("goal", "build_mode", "engine")}
        meta = self.create_project(
            name or self.clone_name(project_id), owner=owner, background=source_meta.get("background", ""),
            created_from="clone", **extra,
        )
        dest_root = self.project_root(meta["id"])
        if include_history and (source_root / ".git").is_dir():
            shutil.rmtree(dest_root / ".git")
            shutil.copytree(source_root / ".git", dest_root / ".git", symlinks=True)
            for path in sorted((dest_root / ".git").rglob("*.lock")):
                path.unlink(missing_ok=True)
            from ...kernel.changesets import worktree as changeset_copies

            # 源项目的助手副本登记与副本分支（没确认的改动）不随克隆带走（ADR-0046）
            changeset_copies.strip_copy_metadata(dest_root)
            release_tags = _git(dest_root, "tag", "--list", "release/*").split()
            if release_tags:
                _git(dest_root, "tag", "-d", *release_tags)
            _git(dest_root, "reset", "-q", "--hard", "HEAD")
            _git(dest_root, "clean", "-q", "-fdx", "-e", META_FILE, "-e", ".claude", "-e", "CLAUDE.md", "-e", "README.md")
        for path in sorted(source_root.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            rel = path.relative_to(source_root)
            if rel.parts[0] in self.CLONE_SKIP_TOP or _is_run_state(rel):
                continue
            dest = dest_root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
        # 源项目的 .gitignore 会盖掉新建时的模板，补齐后再 add，元数据才不会被跟踪
        _ensure_gitignore_entries(dest_root)
        # 锁文件随克隆带过来：按源项目锁定的能力包版本重新装配
        self._mount_assets(dest_root)
        from ...kernel.writepath import Origin, stamped

        _git(dest_root, "add", "-A")
        _git(dest_root, "commit", "-q", "-m", stamped(f"clone: 自 {project_id} 克隆", Origin("import", owner), dest_root),
             "--allow-empty")
        return self.get_project(meta["id"])

    def delete_project(self, project_id: str) -> None:
        """永久删除：工作区连同 git 历史一并清除，不进回收站。

        删除接口把两步拆开（stage_delete 后立即响应，purge_staged 放到响应之后），
        这里供同步场景（导入、开通失败的回滚）一次做完。
        """
        self.purge_staged(self.stage_delete(project_id))

    def purge_dir(self) -> Path:
        return self.workspaces_dir.parent / ".purging"

    def stage_delete(self, project_id: str) -> Path:
        """把工作区移出 workspaces/，返回待清除的路径；返回即视为已删除。

        同盘 rename 是原子的：项目立刻从列表和所有接口消失（404），耗时的递归删除
        交给 purge_staged。目录被占用时 rename 会失败（Windows），短暂重试；仍不成
        （或跨文件系统）就原地把元数据换成清除标记——对外同样立即消失，目录原地清除。
        """
        with self._project_lock(project_id):
            root = self.project_root(project_id)
            staged = self.purge_dir() / f"{project_id}-{uuid.uuid4().hex[:8]}"
            staged.parent.mkdir(parents=True, exist_ok=True)
            for attempt in range(_DELETE_RETRIES):
                try:
                    os.replace(root, staged)
                    return staged
                except OSError as exc:
                    if exc.errno == errno.EXDEV or attempt + 1 == _DELETE_RETRIES:
                        break
                    time.sleep(_DELETE_RETRY_SECONDS)
            try:
                os.replace(root / META_FILE, root / PURGE_MARKER)
            except OSError as exc:
                raise WorkspaceError(
                    "项目目录被占用，暂时无法删除，请稍后重试", 409, "project_busy"
                ) from exc
            return root

    def purge_staged(self, path: Path) -> bool:
        """递归删除 stage_delete 交出的目录；删不净（文件仍被占用）时留给重启清扫。"""
        if path.parent not in (self.purge_dir(), self.workspaces_dir):
            raise ValueError(f"拒绝清除工作区之外的路径：{path}")
        for attempt in range(_DELETE_RETRIES):
            if remove_tree(path):
                return True
            if attempt + 1 < _DELETE_RETRIES:
                time.sleep(_DELETE_RETRY_SECONDS)
        if path.parent == self.workspaces_dir:
            # 原地清除删到一半时标记文件可能已被删掉：补回，否则重启清扫认不出这个残留
            try:
                (path / PURGE_MARKER).touch()
            except OSError:
                pass
        return False

    def purge_leftovers(self) -> int:
        """启动清扫：清除上次没删净的残留（.purging/ 下的条目、带清除标记的原地目录）。"""
        try:
            purging = self.purge_dir()
            leftovers = list(purging.iterdir()) if purging.is_dir() else []
            leftovers += [
                entry
                for entry in self.workspaces_dir.iterdir()
                if (entry / PURGE_MARKER).exists() and not (entry / META_FILE).exists()
            ]
        except OSError:
            return 0
        return sum(1 for path in leftovers if self.purge_staged(path))

    # ---- 回收站（仅存量：删除已改为永久删除，这里只管旧版软删除留下的条目） ----

    def trash_dir(self) -> Path:
        return self.workspaces_dir.parent / "trash"

    def list_trash(self) -> list[dict]:
        directory = self.trash_dir()
        if not directory.is_dir():
            return []
        entries = []
        for entry in sorted(directory.iterdir(), reverse=True):
            meta_path = entry / META_FILE
            if not meta_path.is_file():
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                meta = {}
            ts = 0
            tail = entry.name.rsplit("-", 1)[-1]
            if tail.isdigit():
                ts = int(tail)
            entries.append(
                {
                    "name": entry.name,
                    "project_name": meta.get("name", entry.name),
                    "owner": meta.get("owner", ""),
                    "deleted_at": ts,
                }
            )
        return entries

    def restore_trash(self, name: str) -> dict:
        if "/" in name or ".." in name:
            raise WorkspaceError("无效的回收站条目")
        source = self.trash_dir() / name
        if not (source / META_FILE).is_file():
            raise WorkspaceError("回收站条目不存在", 404)
        meta = json.loads((source / META_FILE).read_text(encoding="utf-8"))
        project_id = meta.get("id", "")
        # 原 id 已被占用（同名重建过）时换新 id 恢复
        if not project_id or (self.workspaces_dir / project_id).exists():
            project_id = f"{_slugify(meta.get('name', 'task'))}-{uuid.uuid4().hex[:8]}"
            meta["id"] = project_id
        dest = self.workspaces_dir / project_id
        shutil.move(str(source), str(dest))
        from .. import ownership  # ownership 依赖工作区错误类型，在函数里导入避免循环

        ownership.drop_members(project_id, meta)
        self._write_meta(dest, meta)
        try:
            self.untrack_project_state(project_id)
        except WorkspaceError:
            pass  # 迁移失败不挡恢复，下次启动重试
        return meta

    # ---- 内部 ----

    def _mount_assets(self, root: Path, packs: list[str] | None = None) -> None:
        """补齐能力包锁文件，装配并原子切换 `.claude`（方案 D2）；CLAUDE.md、README.md 是指向
        `.claude/` 里的相对链接。镜像里没有工具箱时什么都不挂（同原行为）。"""
        self.packs.mount(root, packs)

    def _copy_resource_template(self, root: Path) -> None:
        template = self.assets_dir / "resource-template"
        dest = root / "resource"
        if template.is_dir():
            shutil.copytree(template, dest)
        else:
            dest.mkdir()
        # copytree 按镜像里的模板设权限（0755、没有 setgid）：之后上传时新建的子目录归后端的组，agent 读不到；
        # 模板为空（镜像里没有这个目录）时新建的 resource/ 同样补上
        from ...governance import agent_identity

        agent_identity.share_writable(dest)

    def _write_gitignore(self, root: Path) -> None:
        template = (
            self.assets_dir
            / "workspace-toolkit"
            / "templates"
            / "task-gitignore.template"
        )
        if template.is_file():
            shutil.copy(template, root / ".gitignore")
        else:
            (root / ".gitignore").write_text(DEFAULT_GITIGNORE, encoding="utf-8")

    # ---- 启动维护 ----

    def _project_dirs(self) -> list[Path]:
        return [
            entry
            for entry in sorted(self.workspaces_dir.iterdir())
            if entry.is_dir() and (entry / META_FILE).is_file()
        ]

    def untrack_project_state(self, project_id: str) -> bool:
        """存量项目迁移：元数据与工具状态停止跟踪，单独成一个提交；已迁移过时什么都不做。"""
        with self._project_lock(project_id):
            return _untrack_state_commit(self.project_root(project_id))

    def migrate_state_tracking(self) -> list[str]:
        """启动迁移：逐个项目停止跟踪元数据与工具状态，返回本次迁移了的项目。

        放在会话开始之前做：迁移要动真实索引，此时没有 agent 在同一个工作区里跑 git。
        """
        migrated = []
        for root in self._project_dirs():
            try:
                if self.untrack_project_state(root.name):
                    migrated.append(root.name)
            except Exception as exc:  # 单个项目失败（含 git 超时）不能挡住服务启动
                _log.warning("项目 %s 停止跟踪元数据失败，下次启动重试：%s", root.name, exc)
        if migrated:
            _log.warning("已停止跟踪项目元数据与工具状态：%s", ", ".join(migrated))
        return migrated


    def commit_pending_uploads(self) -> list[str]:
        """启动补提交：资料区里还没进 git 的上传（旧版上传不提交，或写盘后提交前进程退出）。

        resource/ 只有网页能写（agent 写资料区被守卫拦下），这里的改动都是用户上传，只提交
        资料区本身，不带 agent 的其他改动。返回补提交过的项目 id；单个项目失败只记日志。
        """
        committed = []
        for root in self._project_dirs():
            project_id = root.name
            try:
                with self._project_lock(project_id):
                    if _commit_paths(root, ["resource"], "web: 补提交未入版本的资料"):
                        committed.append(project_id)
            except Exception as exc:  # 单个项目失败不能挡住服务启动
                _log.warning("项目 %s 补提交资料失败，下次启动重试：%s", project_id, exc)
        if committed:
            _log.info("补提交未入版本的资料：%s", ", ".join(committed))
        return committed

    def clear_git_locks(self) -> list[Path]:
        """启动清理：删除各项目 .git 下残留的锁文件，返回删掉的路径。

        只能在启动时、会话开始之前调用：此时本进程没有 git 子进程，锁都是上次强杀或
        超时留下的；运行期间删锁会破坏正在进行的 git 操作。
        """
        removed: list[Path] = []
        try:
            entries = sorted(self.workspaces_dir.iterdir())
        except OSError:
            return removed
        for entry in entries:
            if entry.is_dir():
                removed.extend(remove_git_locks(entry))
        if removed:
            _log.warning(
                "启动时清理了残留的 git 锁：%s",
                ", ".join(str(path.relative_to(self.workspaces_dir)) for path in removed),
            )
        return removed

    @staticmethod
    def _resolve_inside(root: Path, rel_path: str) -> Path:
        if "\x00" in str(rel_path):
            raise WorkspaceError("路径不合法", 400, "invalid_argument", {"fields": {"path": "pattern"}})
        target = (root / rel_path).resolve()
        root_resolved = root.resolve()
        if not target.is_relative_to(root_resolved):
            raise WorkspaceError("路径越界", 403)
        return target


class WorkspaceManager(FilesMixin, VersionsMixin, TransferMixin, WorkspaceCore):
    """工作区管理器：核心 + 文件与资料 + 版本 + 导入导出。"""


def _current_umask() -> int:
    """进程 umask：Linux 从 /proc 读（不用先改再改回，多线程下没有窗口），其他平台退回 os.umask。"""
    try:
        for line in Path("/proc/self/status").read_text(encoding="ascii").splitlines():
            if line.startswith("Umask:"):
                return int(line.split()[1], 8)
    except (OSError, ValueError):
        pass
    mask = os.umask(0o022)
    os.umask(mask)
    return mask


def remove_tree(path: Path) -> bool:
    """尽力递归删除，返回是否已删净。

    链接（符号链接、Windows 目录联接）只删链接本身：工作区里的 .claude 等指向
    所有项目共用的资产目录，跟进去会把资产一起删掉。只读文件（git 对象）去掉只读位再删。
    """
    try:
        if _is_link(path) or path.is_file():
            os.unlink(path)
        elif path.is_dir():
            shutil.rmtree(path, onerror=_force_remove)
    except OSError:
        pass
    return not os.path.lexists(path)


def _force_remove(func, path, _exc_info) -> None:
    if func not in (os.unlink, os.remove, os.rmdir) or _is_link(path):
        return
    try:
        os.chmod(path, os.lstat(path).st_mode | stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def _is_link(path) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    reparse = getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    return stat.S_ISLNK(st.st_mode) or bool(reparse)
