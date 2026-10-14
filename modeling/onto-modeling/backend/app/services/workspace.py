"""项目工作区管理：目录 + git + 提示词资产挂载。

对应 CLI 版 init_task.sh 的职责：每个建模任务一个独立目录和独立 git，
.claude / CLAUDE.md / README.md 软链到共享资产目录，升级资产对所有任务生效。
"""

from __future__ import annotations

import errno
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
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

from .. import config
from . import package_mapping

META_FILE = ".onto-web.json"  # 历史文件名保留：存量项目的识别键（ADR-0006）
PURGE_MARKER = ".onto-web.purging"  # 原地清除兜底：顶替 META_FILE，重启清扫据此认领残留
# 删除时 Windows 上刚被杀的进程要片刻才释放目录句柄，rename/rmtree 短暂重试
_DELETE_RETRIES = 5
_DELETE_RETRY_SECONDS = 0.2
SKIP_DIRS = {".git", ".ontology-state", ".obsidian"}
RESOURCE_SUBDIRS = {"diagrams", "excel", "others"}
# 已发布知识包导入时的原包留底区：随 git 版本化（跨环境迁移要带走），
# 但不是用户可编辑内容（隐藏目录，不进文件树）。
IMPORTED_PACKAGE_DIR = ".imported-package"

DEFAULT_GITIGNORE = """.ontology-state/
.claude
CLAUDE.md
README.md
tmp/
"""


_PROJECT_LOCKS: dict[str, threading.RLock] = {}
_PROJECT_LOCKS_GUARD = threading.Lock()


class WorkspaceError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "workspace_error"):
        super().__init__(message)
        self.status = status
        self.code = code


class WorkspaceManager:
    def __init__(self, data_dir: Path, assets_dir: Path):
        self.workspaces_dir = Path(data_dir) / "workspaces"
        self.assets_dir = Path(assets_dir)
        self.workspaces_dir.mkdir(parents=True, exist_ok=True)

    def _lock_path(self, project_id: str) -> Path:
        """Return a lock outside the project so it is never included in exports/git."""
        key = hashlib.sha256(project_id.encode("utf-8")).hexdigest()
        return self.workspaces_dir.parent / ".workspace-locks" / f"{key}.lock"

    @contextmanager
    def _project_lock(self, project_id: str):
        """Serialize project mutations and metadata reads across managers/processes."""
        lock_path = self._lock_path(project_id)
        key = str(lock_path.resolve())
        with _PROJECT_LOCKS_GUARD:
            lock = _PROJECT_LOCKS.setdefault(key, threading.RLock())
        lock.acquire()
        fd = None
        try:
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
                if old_mode is not None:
                    os.fchmod(stream.fileno(), old_mode)
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

    def create_project(self, name: str, owner: str = "", background: str = "") -> dict:
        name = (name or "").strip()
        if not name:
            raise WorkspaceError("项目名称不能为空")
        project_id = f"{_slugify(name)}-{uuid.uuid4().hex[:8]}"
        root = self.workspaces_dir / project_id
        root.mkdir(parents=True)

        self._mount_assets(root)
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
            "background": (background or "").strip()[:200],
            "engine": _default_engine(),
        }
        self._atomic_write_text(
            root / META_FILE, json.dumps(meta, ensure_ascii=False, indent=2)
        )

        _git(root, "init")
        _git(root, "config", "user.name", "Onto Modeling")
        _git(root, "config", "user.email", "onto-modeling@example.invalid")
        _git(root, "add", "-A")
        _git(root, "commit", "-m", "init: 任务工作区基线", "--allow-empty")
        return meta

    # 与前端阶段定义保持一致的目录探针：可选阶段未完成时跳过继续，
    # 必选阶段未完成即停。index.md 是目录索引，不算产物。
    # ontology/logic 是旧目录别名（范式升级 ADR-0004），探针两者取或。
    STAGES = (
        (("research",), False),
        (("ontology/diagrams",), True),
        (("ontology/object",), False),
        (("ontology/operation", "ontology/logic"), False),
        (("ontology/rule", "ontology/metric"), True),
        (("reports",), False),
        (("ontology/data-mock",), False),
        (("ontology/testcase",), False),
        (("ontology/scripts",), True),
        # 数据评测（可选，方案 eval-optimization-integration）：探针为报告文件 glob
        (("reports/eval-*.md",), True),
    )
    STAGE_TOTAL = len(STAGES)

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
        done = 0
        for dirs, optional in cls.STAGES:
            hit = any(
                (next(root.glob(rel), None) is not None)
                if "*" in rel
                else cls._dir_has_entries(root / rel)
                for rel in dirs
            )
            if hit:
                done += 1
            elif optional:
                continue
            else:
                break

        def count_md(rel: str) -> int:
            directory = root / rel
            if not directory.is_dir():
                return 0
            return sum(
                1 for f in directory.glob("*.md") if f.name.lower() != "index.md"
            )

        eval_summary = {}
        try:
            eval_summary = json.loads(
                (root / ".ontology-state" / "eval.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            pass
        from .modeling_eval import evolution_hint

        return {
            "eval_score": (eval_summary.get("summary") or {}).get("score"),
            "eval_status": eval_summary.get("status"),
            # 最近一轮有失分/异常用例时的 /ontology-evolution 提示（无则为 None）
            "eval_evolution": evolution_hint(eval_summary),
            "stages_done": done,
            "stages_total": cls.STAGE_TOTAL,
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

    @staticmethod
    def role_of(meta: dict, username: str, is_admin: bool) -> str | None:
        """项目内角色：admin/owner/editor（建模者）/viewer（只读评审）/None（无权）。

        无主的历史项目对所有人开放为 editor（沿袭多用户上线前的行为）。
        """
        if is_admin:
            return "admin"
        owner = meta.get("owner", "")
        if owner == "":
            return "editor"
        if owner == username:
            return "owner"
        return (meta.get("members") or {}).get(username)

    @classmethod
    def visible_to(cls, projects: list[dict], username: str, is_admin: bool) -> list[dict]:
        """项目可见性：管理员看全部；其余按角色（owner/成员/无主历史项目）。"""
        return [p for p in projects if cls.role_of(p, username, is_admin) is not None]

    @classmethod
    def can_access(cls, meta: dict, username: str, is_admin: bool) -> bool:
        return cls.role_of(meta, username, is_admin) is not None

    WRITE_ROLES = ("admin", "owner", "editor")

    @classmethod
    def can_write(cls, meta: dict, username: str, is_admin: bool) -> bool:
        return cls.role_of(meta, username, is_admin) in cls.WRITE_ROLES

    def set_members(self, project_id: str, members: dict[str, str]) -> dict:
        for username, role in members.items():
            if role not in ("editor", "viewer"):
                raise WorkspaceError(f"成员 {username} 的角色必须是 editor 或 viewer")
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", username):
                raise WorkspaceError(f"无效的成员用户名：{username}")
        return self._update_meta(project_id, members=members)

    def get_project(self, project_id: str) -> dict:
        with self._project_lock(project_id):
            root = self.project_root(project_id)
            try:
                return json.loads((root / META_FILE).read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise WorkspaceError(
                    "项目元数据暂时不可用，请稍后重试",
                    503,
                    "project_metadata_unavailable",
                ) from exc

    def project_root(self, project_id: str) -> Path:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", project_id):
            raise WorkspaceError("无效的项目 ID", 404)
        root = self.workspaces_dir / project_id
        if not (root / META_FILE).is_file():
            raise WorkspaceError("项目不存在", 404)
        return root

    # ---- 知识包目录规范化（范式升级 ADR-0004）----

    # 目录索引标题；index.md 采用 modeling 发布包风格的清单表格
    INDEX_DIRS = (
        ("ontology/object", "对象目录"),
        ("ontology/operation", "操作目录"),
        ("ontology/rule", "规则目录"),
        ("ontology/metric", "指标目录"),
        ("ontology/data-mock", "示例数据目录"),
        ("ontology/testcase", "场景用例目录"),
        ("ontology/scripts", "脚本目录"),
        ("ontology/diagrams", "流程图目录"),
        ("ontology/diagrams-summary", "流程图摘要目录"),
        ("ontology/mermaid", "Mermaid 图目录"),
        ("ontology/excel", "表格资料目录"),
        ("research", "资料分析目录"),
        ("reports", "报告目录"),
    )

    def ensure_package_layout(self, project_id: str) -> bool:
        """迁移旧 ontology/logic 目录并刷新各目录 index.md。

        仅当工作区原本干净时才提交版本；agent 会话中途的改动留给
        agent 自己的提交收编，避免交叉提交打断其暂存状态。
        """
        root = self.project_root(project_id)
        was_clean = not _git(root, "status", "--porcelain").strip()
        changed = self._migrate_legacy_logic(root)
        changed = self._refresh_indexes(root) or changed
        if changed and was_clean:
            _git(root, "add", "-A")
            _git(root, "commit", "-m", "chore: 知识包目录规范化（operation 目录与索引）")
        return changed

    @staticmethod
    def _migrate_legacy_logic(root: Path) -> bool:
        legacy = root / "ontology" / "logic"
        if not legacy.is_dir():
            return False
        target = root / "ontology" / "operation"
        target.mkdir(parents=True, exist_ok=True)
        changed = False
        for entry in sorted(legacy.iterdir()):
            if entry.name.lower() == "index.md":
                entry.unlink()
                changed = True
                continue
            dest = target / entry.name
            if dest.exists():
                continue  # 同名冲突保留原位人工处理，不覆盖
            entry.rename(dest)
            changed = True
        if not any(legacy.iterdir()):
            legacy.rmdir()
            changed = True
        return changed

    def _refresh_indexes(self, root: Path) -> bool:
        changed = False
        for rel, title in self.INDEX_DIRS:
            directory = root / rel
            if not directory.is_dir():
                continue
            rows = []
            for entry in sorted(directory.iterdir()):
                if (
                    entry.name.startswith(".")
                    or entry.is_dir()
                    or entry.name.lower() == "index.md"
                ):
                    continue
                label = (
                    _first_heading(entry) or entry.stem
                    if entry.suffix.lower() == ".md"
                    else entry.stem
                )
                rows.append(f"| {entry.name} | {label} |\n")
            content = f"# {title}\n\n| 文件 | 标题 |\n| --- | --- |\n" + "".join(rows)
            index_path = directory / "index.md"
            existing = (
                index_path.read_text(encoding="utf-8") if index_path.is_file() else None
            )
            if existing != content:
                index_path.write_text(content, encoding="utf-8")
                changed = True
        return changed

    # ---- 文件 ----

    def file_tree(self, project_id: str) -> list[dict]:
        root = self.project_root(project_id)
        try:
            self.ensure_package_layout(project_id)
        except Exception:
            pass  # 规范化失败不阻断浏览（下次访问重试）

        def walk(directory: Path, rel: str) -> list[dict]:
            nodes = []
            for entry in sorted(directory.iterdir(), key=lambda e: (e.is_file(), e.name)):
                if entry.name.startswith(".") or entry.name == META_FILE:
                    continue
                if entry.is_symlink():
                    continue
                rel_path = f"{rel}/{entry.name}" if rel else entry.name
                if entry.is_dir():
                    if entry.name in SKIP_DIRS:
                        continue
                    nodes.append(
                        {
                            "name": entry.name,
                            "path": rel_path,
                            "type": "dir",
                            "children": walk(entry, rel_path),
                        }
                    )
                else:
                    nodes.append({"name": entry.name, "path": rel_path, "type": "file"})
            return nodes

        return walk(root, "")

    def read_file(self, project_id: str, rel_path: str, max_bytes: int) -> dict:
        root = self.project_root(project_id)
        target = self._resolve_inside(root, rel_path)
        if not target.is_file():
            raise WorkspaceError("文件不存在", 404)
        raw = target.read_bytes()[: max_bytes + 1]
        truncated = len(raw) > max_bytes
        raw = raw[:max_bytes]
        if b"\x00" in raw:
            raise WorkspaceError("二进制文件不支持预览", 415)
        return {
            "path": rel_path,
            "content": raw.decode("utf-8", errors="replace"),
            "truncated": truncated,
            # 乐观并发标记：保存时回传，内容被他人（含 agent）改过则拒绝覆盖
            "revision": _content_revision(target.read_bytes()),
        }

    # 资料用途标签（对齐 modeling 的 material purpose 体系）。评测数据不再走资料区打标，
    # 改在评测数据源里上传（eval_data_files）；存量 eval_data 标签读取时按 auto 处理
    MATERIAL_PURPOSES = ("auto", "goal", "schema", "term", "evidence")
    MATERIAL_READING_MODES = ("on_demand", "full")

    def resource_directives(self, project_id: str) -> dict:
        """resource/materials.json：资料路径 → {purpose, reading_mode}。无文件视为空。"""
        root = self.project_root(project_id)
        path = root / "resource" / "materials.json"
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise WorkspaceError(
                "资料用途元数据暂时不可用，请稍后重试",
                503,
                "resource_metadata_unavailable",
            ) from exc
        if not isinstance(data, dict):
            return {}
        for directive in data.values():
            if isinstance(directive, dict) and directive.get("purpose") not in self.MATERIAL_PURPOSES:
                directive["purpose"] = "auto"
        return data

    def set_resource_directive(
        self, project_id: str, rel_path: str, purpose: str, reading_mode: str = "on_demand"
    ) -> dict:
        if purpose not in self.MATERIAL_PURPOSES:
            raise WorkspaceError(f"资料用途必须是 {list(self.MATERIAL_PURPOSES)} 之一")
        if reading_mode not in self.MATERIAL_READING_MODES:
            raise WorkspaceError(f"读取模式必须是 {list(self.MATERIAL_READING_MODES)} 之一")
        with self._project_lock(project_id):
            root = self.project_root(project_id)
            target = self._resolve_inside(root, rel_path)
            rel = target.relative_to(root.resolve()).as_posix()
            if not rel.startswith("resource/") or not target.is_file():
                raise WorkspaceError("只能为 resource/ 下的资料设置用途", 400)
            directives_path = root / "resource" / "materials.json"
            snapshot = self._snapshot(directives_path)
            try:
                directives = self.resource_directives(project_id)
                directives[rel] = {"purpose": purpose, "reading_mode": reading_mode}
                self._write_resource_directives(root, directives)
                # 用途变化会影响派生产物的口径，视同资料更新
                self._update_meta_unlocked(root, resource_updated_at=int(time.time()))
            except Exception:
                self._restore_snapshot(directives_path, snapshot)
                raise
            return {"path": rel, **directives[rel]}

    def _write_resource_directives(self, root: Path, directives: dict) -> None:
        self._atomic_write_text(
            root / "resource" / "materials.json",
            json.dumps(directives, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )

    def save_resource(
        self,
        project_id: str,
        subdir: str,
        filename: str,
        data: bytes,
        purpose: str = "auto",
        reading_mode: str = "on_demand",
    ) -> dict:
        if subdir not in RESOURCE_SUBDIRS:
            raise WorkspaceError(f"资料分类必须是 {sorted(RESOURCE_SUBDIRS)} 之一")
        safe_name = Path(filename).name
        if not safe_name or safe_name.startswith(".") or safe_name != filename:
            raise WorkspaceError("无效的文件名")
        with self._project_lock(project_id):
            root = self.project_root(project_id)
            dest_dir = root / "resource" / subdir
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / safe_name
            replaced = dest.exists()
            file_snapshot = self._snapshot(dest)
            directives_path = root / "resource" / "materials.json"
            directives_snapshot = self._snapshot(directives_path)
            try:
                self._atomic_write(dest, data)
                rel = f"resource/{subdir}/{safe_name}"
                if purpose != "auto" or reading_mode != "on_demand":
                    directives = self.resource_directives(project_id)
                    directives[rel] = {
                        "purpose": purpose if purpose in self.MATERIAL_PURPOSES else "auto",
                        "reading_mode": reading_mode
                        if reading_mode in self.MATERIAL_READING_MODES
                        else "on_demand",
                    }
                    self._write_resource_directives(root, directives)
                # 变更提醒依据：前端对比该时间与派生产物版本，提示"资料已更新，可让助手增量刷新"
                self._update_meta_unlocked(root, resource_updated_at=int(time.time()))
            except Exception:
                self._restore_snapshot(dest, file_snapshot)
                self._restore_snapshot(directives_path, directives_snapshot)
                raise
            return {"path": rel, "size": len(data), "replaced": replaced}

    # ---- 生命周期扩展 ----

    def _update_meta(self, project_id: str, **fields) -> dict:
        with self._project_lock(project_id):
            root = self.project_root(project_id)
            return self._update_meta_unlocked(root, **fields)

    def _update_meta_unlocked(self, root: Path, **fields) -> dict:
        try:
            meta = json.loads((root / META_FILE).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkspaceError(
                "项目元数据暂时不可用，请稍后重试",
                503,
                "project_metadata_unavailable",
            ) from exc
        meta.update(fields)
        self._atomic_write_text(
            root / META_FILE, json.dumps(meta, ensure_ascii=False, indent=2)
        )
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

    def clone_project(self, project_id: str, owner: str = "") -> dict:
        source_root = self.project_root(project_id)
        source_meta = self.get_project(project_id)
        meta = self.create_project(f"{source_meta['name']}-副本", owner=owner)
        dest_root = self.project_root(meta["id"])
        for path in sorted(source_root.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            rel = path.relative_to(source_root)
            if rel.parts[0] in self.CLONE_SKIP_TOP:
                continue
            dest = dest_root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
        _git(dest_root, "add", "-A")
        _git(dest_root, "commit", "-m", f"clone: 自 {project_id} 克隆", "--allow-empty")
        return meta

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

    EXPORT_ONTOLOGY_DIRS = ("ontology", "research", "reports", "knowledge")

    def export_zip(self, project_id: str, scope: str, dest_path: Path) -> Path:
        import zipfile

        root = self.project_root(project_id)
        with zipfile.ZipFile(dest_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(root.rglob("*")):
                if path.is_symlink() or not path.is_file():
                    continue
                rel = path.relative_to(root)
                top = rel.parts[0]
                if scope == "ontology":
                    if top not in self.EXPORT_ONTOLOGY_DIRS:
                        continue
                else:  # workspace：全量含 .git，便于另一环境恢复历史
                    if top == ".ontology-state":
                        continue
                zf.write(path, str(rel))
        return dest_path

    # 属性表"属性别名"列的占位写法（模板约定无别名写"无"）在下游校验器里
    # 可能被当作真实别名，引发"同名指向多字段"冲突（modeling 新版已过滤，
    # consumer 与存量部署未必）；打包时统一清洗为空，单一出口治本。
    _ALIAS_HEADER_NAMES = {"属性别名", "别名", "aliases", "alias"}
    _ALIAS_EMPTY_MARKERS = {"无", "-", "—", "–", "n/a"}

    @classmethod
    def _is_placeholder_alias_cell(cls, cells: list[str], alias_col: int | None) -> bool:
        if alias_col is None or alias_col >= len(cells):
            return False
        if all(set(c) <= set("-: ") for c in cells):  # 跳过分隔行
            return False
        return cells[alias_col].lower() in cls._ALIAS_EMPTY_MARKERS

    @classmethod
    def _sanitize_placeholder_aliases(cls, markdown: str) -> str:
        lines = markdown.split("\n")
        alias_col: int | None = None
        prev_is_row = False
        for i, line in enumerate(lines):
            if not line.lstrip().startswith("|"):
                alias_col = None
                prev_is_row = False
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if not prev_is_row:
                lowered = [c.lower() for c in cells]
                alias_col = next(
                    (idx for idx, c in enumerate(lowered) if c in cls._ALIAS_HEADER_NAMES),
                    None,
                )
            elif cls._is_placeholder_alias_cell(cells, alias_col):
                raw = line.split("|")
                if alias_col + 1 < len(raw):
                    raw[alias_col + 1] = " "
                    lines[i] = "|".join(raw)
            prev_is_row = True
        return "\n".join(lines)

    def export_package_zip(
        self,
        project_id: str,
        dest_path: Path,
        binding: dict | None = None,
        graph: bool = False,
        relation_report: dict | None = None,
        allow_unbound: bool = False,
    ) -> Path:
        """发布为知识包（consumer 可导入的 ontology-package v0.1）。

        对齐 consumer/backend/ontology/package_import.py 的校验契约：
        manifest.yaml 六必填字段、objects/operations 目录 + frontmatter、
        operation 必须声明 uses.objects、index 首列与详情 key 逐字相等。

        binding：评测导入时传入按本轮导入工作簿算出的绑定，mapping 与导入库同源；
        graph：Neo4j 图导入用的包——mapping 按图模式导出，「关联对象」转为 `# 关系` 表；
        allow_unbound：降级发布——声明对不上的对象不阻断，来源Sheet 写 UNBOUND、不进 mapping；
        图模式下，端点因此未绑定的关系也只记原因不阻断（图里本来就没有该类节点）。
        """
        import zipfile

        root = self.project_root(project_id)
        meta = self.get_project(project_id)
        version = f"0.1.{len(self.versions(project_id))}"
        pkg = meta["id"]

        def entry_items(*rel_dirs: str, pattern: str = "*.md") -> list[tuple[str, str]]:
            items = []
            seen = set()
            for rel_dir in rel_dirs:
                directory = root / rel_dir
                if not directory.is_dir():
                    continue
                for path in sorted(directory.glob(pattern)):
                    if path.name == "index.md" or path.stem in seen:
                        continue
                    seen.add(path.stem)
                    items.append((path.stem, _strip_frontmatter(path.read_text(encoding="utf-8"))))
            return items

        objects = entry_items("ontology/object")
        # ontology/logic 为旧目录兼容（范式升级 ADR-0004）
        operations = entry_items("ontology/operation", "ontology/logic")
        # 对象 ↔ 真实数据表：来源Sheet、`# 字段` 段与 mappings 三处同一份绑定结果
        binding = binding or package_mapping.resolve_object_tables(
            root, strict_declared=not allow_unbound
        )
        if binding["problems"]:
            raise WorkspaceError(
                "数据绑定无法发布：" + "；".join(binding["problems"]),
                code="binding_problems",
            )
        bound = binding["objects"]
        mappings = package_mapping.object_data_mappings(binding, graph=graph)
        graph_edges: dict[str, list] = {}
        graph_relation_skips: list[str] = []
        if graph:
            graph_edges, graph_relation_skips = package_mapping.graph_relations(
                package_mapping.read_object_texts(root),
                set(mappings["objects"]), relation_report, allow_unbound=allow_unbound,
            )
            if graph_relation_skips:
                raise WorkspaceError(
                    "图关系转换未完成，请修正关联对象后重试：" + "；".join(graph_relation_skips)
                )
        rules = entry_items("ontology/rule")
        metrics = entry_items("ontology/metric")
        scripts = entry_items("ontology/scripts", pattern="*.py")
        object_keys = {key for key, _ in objects}
        def q(s):
            return json.dumps(s, ensure_ascii=False)  # JSON 字符串是合法 YAML 标量

        manifest = (
            f"kind: ontology-package\n"
            f"schema_version: '0.1'\n"
            f"id: {pkg}\n"
            f"name: {q(meta['name'])}\n"
            f"description: {q('由 Onto Web 建模项目发布：' + meta['name'])}\n"
            f"version: {version}\n"
            # modeling 包契约（package-contract MANIFEST_REQUIRED_FIELDS）必选；
            # consumer 按已知键读取，多余字段无害
            f"status: released\n"
            f"language: zh-CN\n"
        )

        def front(resource: str, key: str, title: str, extra: str = "") -> str:
            return f"---\nresource: {resource}\nkey: {q(key)}\ntitle: {q(title)}\n{extra}---\n\n"

        def index_md(
            resource: str,
            key: str,
            title: str,
            items: list[tuple[str, str]],
            headers: list[str],
            row: "callable",
        ) -> str:
            # 表头对齐 modeling 知识包契约（knowledge-markdown-contract INDEX_CONFIG）；
            # consumer 按表头名取列、容忍多列，两端同一份索引
            head = "| " + " | ".join(headers) + " |\n" + "|" + " --- |" * len(headers) + "\n"
            rows = "".join(row(k) for k, _ in items)
            return front(resource, key, title) + head + rows

        with zipfile.ZipFile(dest_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(f"{pkg}/manifest.yaml", manifest)
            zf.writestr(f"{pkg}/index.md", front("root", "root", meta["name"]) + f"# {meta['name']}\n")
            zf.writestr(
                f"{pkg}/objects/index.md",
                index_md(
                    "object_index", "objects", "对象目录", objects,
                    ["object key", "标题", "说明", "来源Sheet"],
                    lambda k: f"| {k} | {k} | {k} | "
                              f"{bound.get(k, {}).get('table') or package_mapping.UNBOUND} |\n",
                ),
            )
            for key, body in objects:
                item = bound.get(key, {})
                table = item.get("table")
                # 绑定到真实表时补 mapping/sources frontmatter：mappings json 缺失或被裁剪时
                # modeling 仍能从 sources 还原 sourceRefs
                extra = (
                    f"mapping:\n  provider_alias: {package_mapping.PROVIDER_ALIAS}\n"
                    f"  resource: {q(key if graph else table)}\nsources: [{q('表格：' + table)}]\n"
                    if table else ""
                )
                body = package_mapping.rewrite_object_body(body, item.get("fields") or [])
                if graph_edges.get(key):
                    body = body.rstrip("\n") + "\n\n" + package_mapping.render_relations_section(
                        graph_edges[key]
                    )
                zf.writestr(
                    f"{pkg}/objects/{key}.md",
                    front("object", key, key, extra) + self._sanitize_placeholder_aliases(body),
                )
            if mappings["objects"]:
                zf.writestr(
                    f"{pkg}/mappings/object-data-mappings.json",
                    json.dumps(mappings, ensure_ascii=False, indent=2) + "\n",
                )
            zf.writestr(
                f"{pkg}/operations/index.md",
                index_md(
                    "operation_index", "operations", "操作目录", operations,
                    ["operation key", "标题", "输入", "输出"],
                    lambda k: f"| {k} | {k} | {k} | {k} |\n",
                ),
            )
            for key, body in operations:
                used = sorted({m for m in _WIKILINK_RE.findall(body) if m in object_keys})
                extra = f"uses:\n  objects: {json.dumps(used, ensure_ascii=False)}\n"
                zf.writestr(f"{pkg}/operations/{key}.md", front("operation", key, key, extra) + body)
            # 规则/指标/脚本：五元组一等实体（范式升级 ADR-0004），可选目录
            if rules:
                zf.writestr(
                    f"{pkg}/rules/index.md",
                    index_md(
                        "rule_index", "rules", "规则目录", rules,
                        ["rule key", "标题", "说明"],
                        lambda k: f"| {k} | {k} | {k} |\n",
                    ),
                )
                for key, body in rules:
                    used = sorted({m for m in _WIKILINK_RE.findall(body) if m in object_keys})
                    extra = f"uses:\n  objects: {json.dumps(used, ensure_ascii=False)}\n"
                    zf.writestr(f"{pkg}/rules/{key}.md", front("rule", key, key, extra) + body)
            if metrics:
                zf.writestr(
                    f"{pkg}/metrics/index.md",
                    index_md(
                        "metric_index", "metrics", "指标目录", metrics,
                        ["metric key", "标题", "说明"],
                        lambda k: f"| {k} | {k} | {k} |\n",
                    ),
                )
                for key, body in metrics:
                    used = sorted({m for m in _WIKILINK_RE.findall(body) if m in object_keys})
                    extra = f"uses:\n  objects: {json.dumps(used, ensure_ascii=False)}\n"
                    zf.writestr(f"{pkg}/metrics/{key}.md", front("metric", key, key, extra) + body)
            if scripts:
                zf.writestr(
                    f"{pkg}/scripts/index.md",
                    index_md(
                        "script_index", "scripts", "脚本目录", scripts,
                        ["script key", "标题"],
                        lambda k: f"| {k} | {k} |\n",
                    ),
                )
                operation_keys = {key for key, _ in operations}
                for key, code in scripts:
                    filename = _package_script_filename(key)
                    zf.writestr(f"{pkg}/scripts/{filename}", code)
                    # 执行契约：描述符须声明 entrypoint；bound_operation 使脚本
                    # 成为该 operation 的可执行实现；参数表驱动 CLI 参数组装。
                    # entrypoint 是相对 scripts/ 的文件名：modeling 按 scripts/<entrypoint>
                    # 取源码且只收 ASCII 文件名，consumer 缺 scripts/ 前缀时自动补齐。
                    try:
                        bound, params = _script_header_meta(code)
                    except ScriptContractError as exc:
                        raise WorkspaceError(f"脚本 {key} 契约无效：{exc}") from exc
                    extra = f"entrypoint: {filename}\n"
                    if bound and bound in operation_keys:
                        extra += f"bound_operation: {q(bound)}\n"
                    body = f"由 /ontology-codegen 生成的确定性脚本，实现见 `{filename}`。\n"
                    if params:
                        rows = "".join(
                            f"| {p['name']} | {p['flag']} | {p['type']} | {p['required']} | {p['default']} | {p['desc']} | {p['multiple']} | {p['action']} |\n"
                            for p in params
                        )
                        body += (
                            "\n## 输入参数\n\n"
                            "| 参数 key | CLI 参数 | 类型 | 必填 | 默认值 | 说明 | 是否多值 | Action |\n"
                            "| --- | --- | --- | --- | --- | --- | --- | --- |\n" + rows
                        )
                    zf.writestr(f"{pkg}/scripts/{key}.md", front("script", key, key, extra) + body)
        return dest_path

    def import_transfer_zip(self, archive_path: Path, owner: str = "") -> dict:
        """导入迁移包（workspace 导出 zip）：恢复完整工作区含 git 版本历史。

        与 import_zip 的区别：保留 .git（但剥离 hooks，防止恶意钩子），
        沿用原 meta 的名称与统计，仅重写 id/owner/created_at。
        """
        import zipfile

        with zipfile.ZipFile(archive_path) as zf:
            names = set(zf.namelist())
            if META_FILE not in names:
                raise WorkspaceError("不是有效的项目迁移包（缺少工作区元数据）")
            try:
                old_meta = json.loads(zf.read(META_FILE).decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise WorkspaceError("迁移包元数据无法解析") from exc
            name = str(old_meta.get("name") or "").strip() or "导入项目"
            project_id = f"{_slugify(name)}-{uuid.uuid4().hex[:8]}"
            root = self.workspaces_dir / project_id
            root.mkdir(parents=True)
            for info in zf.infolist():
                if self._zip_member_skipped(
                    info, (".claude", "CLAUDE.md", "README.md"), skip_git_hooks=True
                ):
                    continue
                if (info.external_attr >> 28) == 0xA:
                    continue  # 拒绝 zip 内符号链接
                dest = root / info.filename
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)

        self._mount_assets(root)
        self._write_gitignore(root)
        meta = {
            **{
                k: v
                for k, v in old_meta.items()
                if k not in ("members", "modeling_project_id")
            },
            "id": project_id,
            "owner": owner or "",
            "created_at": int(time.time()),
            # 引擎是部署侧配置而非包内容：导入方按本部署的默认引擎跑
            "engine": _default_engine(),
        }
        self._atomic_write_text(
            root / META_FILE, json.dumps(meta, ensure_ascii=False, indent=2)
        )
        if not (root / ".git").is_dir():
            _git(root, "init")
        _git(root, "config", "user.name", "Onto Modeling")
        _git(root, "config", "user.email", "onto-modeling@example.invalid")
        # 保险：禁用可能随包携带的 hooks 路径配置
        _git(root, "config", "core.hooksPath", ".git/hooks-disabled")
        _git(root, "add", "-A")
        _git(root, "commit", "-m", "import: 迁移导入", "--allow-empty")
        return meta

    # 导入时跳过的顶层条目：资产由本服务重新挂载；.git 不合并（v1 不保留导入方历史）
    IMPORT_SKIP_TOP = {".claude", "CLAUDE.md", "README.md", ".git", META_FILE}

    @staticmethod
    def _zip_member_skipped(info, skip_top, skip_git_hooks=False) -> bool:
        """导入包成员过滤：路径穿越/绝对路径/目录项/受管顶层条目一律跳过。"""
        parts = Path(info.filename).parts
        if not parts or info.is_dir():
            return True
        if info.filename.startswith("/") or ".." in parts:
            return True
        if parts[0] in skip_top:
            return True
        if skip_git_hooks and parts[:2] == (".git", "hooks"):
            return True
        return False

    def import_zip(self, name: str, archive_path: Path, owner: str = "") -> dict:
        import zipfile

        meta = self.create_project(name, owner=owner)
        root = self.project_root(meta["id"])
        try:
            with zipfile.ZipFile(archive_path) as zf:
                for info in zf.infolist():
                    if self._zip_member_skipped(info, self.IMPORT_SKIP_TOP):
                        continue
                    # 拒绝 zip 内符号链接（external_attr 高位 0xA 表示 symlink）
                    if (info.external_attr >> 28) == 0xA:
                        continue
                    dest = root / info.filename
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info) as src, open(dest, "wb") as out:
                        shutil.copyfileobj(src, out)
        except zipfile.BadZipFile as exc:
            self.delete_project(meta["id"])
            raise WorkspaceError(f"压缩包无法解析：{exc}") from exc
        _git(root, "add", "-A")
        _git(root, "commit", "-m", "import: 导入既有工作区内容", "--allow-empty")
        return meta

    def import_summary(self, archive_path: Path) -> dict:
        """导入向导第二步：不落盘，返回压缩包内容摘要供用户确认。

        判别与计数在 package_archive，正式导入前也走同一个函数——预览和导入分别
        实现时，必然出现"预览数出 0、导入照样建个空任务"（Issue #359）。
        """
        from . import package_archive

        return package_archive.inspect_archive(archive_path)

    def import_package_zip(self, name: str, archive_path: Path, owner: str = "") -> dict:
        """导入已发布知识包（ontology-package v0.1）：按工作区布局物化，原包留底。

        与 import_zip 的区别只在**布局**：import_zip 原样铺文件，于是发布包的
        `<包根>/objects`（复数）躺在工作区里，而本服务读的是 ontology/object
        （单数）——概览计数与阶段探针全不命中，任务建出来是空的（Issue #359）。
        """
        from . import package_archive

        summary = package_archive.inspect_archive(archive_path)
        if summary["kind"] != package_archive.KIND_KNOWLEDGE_PACKAGE:
            raise WorkspaceError("这不是已发布知识包（缺少知识包清单或 objects 目录）")
        # 门禁放在建项目之前：解析不出对象还把任务建出来，就是本缺陷的表现本身
        if summary["blockers"]:
            raise WorkspaceError("；".join(summary["blockers"]))
        meta = self.create_project(name, owner=owner)
        root = self.project_root(meta["id"])
        try:
            package_archive.materialize(root, archive_path, summary["package_root"])
            # 包里的各级 index.md 是派生物，不搬运；这里按工作区规则重建。
            # 此时工作区已有未跟踪改动，ensure_package_layout 不会另起一个提交。
            self.ensure_package_layout(meta["id"])
            _git(root, "add", "-A")
            _git(root, "commit", "-m", "import: 导入已发布知识包", "--allow-empty")
        except Exception:
            self.delete_project(meta["id"])
            raise
        return meta

    def seed_example(self, project_id: str) -> None:
        """注入外卖订单示例领域（对应 CLI 版 /ontology 示例）。"""
        root = self.project_root(project_id)
        script = root / ".claude" / "scripts" / "seed_example.py"
        if not script.exists():
            raise WorkspaceError("示例脚本不存在（资产未挂载）", 500)
        result = subprocess.run(
            ["python3", str(script)], cwd=root, capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            raise WorkspaceError(f"示例注入失败: {result.stderr.strip()[:300]}", 500)
        status = _git(root, "status", "--porcelain")
        if status.strip():
            _git(root, "add", "-A")
            _git(root, "commit", "-m", "research: 注入外卖订单示例领域")

    # ---- 发布前数据绑定体检与修复 ----

    PACKAGE_CHECK_LIMIT = 50
    PACKAGE_CHECK_TABLE_LIMIT = 100

    def package_binding_check(self, project_id: str) -> dict:
        """发布前体检：阻断项在前，附修复用的表清单、对象文档版本号与最近一次发布记录。

        只返回结论与修复句柄（对象、候选表、文档路径与版本），条目数有上限，超出时 truncated=True。
        """
        root = self.project_root(project_id)
        binding = package_mapping.resolve_object_tables(root)
        issues = sorted(binding["issues"], key=lambda item: not item["blocking"])
        issue_messages = {item["message"] for item in issues}
        warnings = [w for w in binding["warnings"] if w not in issue_messages]
        tables = sorted(binding["tables"])
        limit = self.PACKAGE_CHECK_LIMIT
        shown = []
        for item in issues[:limit]:
            rel = f"ontology/object/{item['object']}.md"
            doc = root / rel
            revision = _content_revision(doc.read_bytes()) if doc.is_file() else ""
            shown.append({**item, "path": rel, "revision": revision})
        objects = binding["objects"]
        return {
            "blocking": sum(1 for item in issues if item["blocking"]),
            "issues": shown,
            "warnings": warnings[:limit],
            "tables": tables[: self.PACKAGE_CHECK_TABLE_LIMIT],
            "table_count": len(tables),
            "objects": len(objects),
            "bound_objects": sum(1 for item in objects.values() if item["table"]),
            "truncated": len(issues) > limit
            or len(warnings) > limit
            or len(tables) > self.PACKAGE_CHECK_TABLE_LIMIT,
            "last_publish": self.get_project(project_id).get("last_package_publish"),
            "table_resources": self._unmarked_table_resources(project_id, root),
        }

    def _unmarked_table_resources(self, project_id: str, root: Path) -> list[str]:
        """已上传但未标 schema 的表格资料：改标用途即可让其中的表参与核对，不必重新上传。"""
        try:
            directives = self.resource_directives(project_id)
        except WorkspaceError:
            directives = {}
        found = []
        for subdir in sorted(RESOURCE_SUBDIRS):
            folder = root / "resource" / subdir
            if not folder.is_dir():
                continue
            for path in sorted(folder.iterdir()):
                rel = f"resource/{subdir}/{path.name}"
                directive = directives.get(rel)
                purpose = directive.get("purpose") if isinstance(directive, dict) else None
                if (
                    path.is_file()
                    and path.suffix.lower() in package_mapping.TABLE_SUFFIXES
                    and purpose not in package_mapping.SOURCE_PURPOSES
                ):
                    found.append(rel)
        return found[: self.PACKAGE_CHECK_LIMIT]

    def set_object_data_source(
        self,
        project_id: str,
        key: str,
        table: str,
        base_revision: str = "",
        author: str = "",
    ) -> dict:
        """修复入口：把对象「# 数据来源」声明改成现有数据表之一，table 为空表示「无」。"""
        root = self.project_root(project_id)
        rel = f"ontology/object/{key}.md"
        if not key or "/" in key or "\\" in key:
            raise WorkspaceError("对象不存在", 404)
        if not self._resolve_inside(root, rel).is_file():
            raise WorkspaceError("对象不存在", 404)
        doc = root / rel
        table = (table or "").strip()
        if table:
            by_lower = {name.lower(): name for name in package_mapping.read_source_tables(root)}
            if table.lower() not in by_lower:
                raise WorkspaceError(f"数据表「{table}」不在 schema 资料或评测数据源里")
            table = by_lower[table.lower()]
        content = package_mapping.set_declared_source(doc.read_text(encoding="utf-8"), table)
        return self.write_file(project_id, rel, content, base_revision=base_revision, author=author)

    def publish_package(
        self,
        project_id: str,
        dest_path: Path,
        allow_unbound: bool = False,
        author: str = "",
    ) -> Path:
        """用户发布知识包。降级发布须可追溯：记录谁、何时发布，以及哪些对象因此未绑定数据表。"""
        root = self.project_root(project_id)
        binding = package_mapping.resolve_object_tables(root, strict_declared=not allow_unbound)
        path = self.export_package_zip(
            project_id, dest_path, binding=binding, allow_unbound=allow_unbound
        )
        degraded = sorted({
            item["object"] for item in binding["issues"]
            if item["kind"] != "declared_unverifiable"
        })
        self._update_meta(project_id, last_package_publish={
            "at": int(time.time()),
            "by": author,
            "degraded": bool(degraded),
            "unbound_count": len(degraded),
            "unbound_objects": degraded[: self.PACKAGE_CHECK_LIMIT],
        })
        return path

    # ---- 文件写入与历史版本 ----

    WRITE_FORBIDDEN_TOP = {".git", ".claude", ".ontology-state", IMPORTED_PACKAGE_DIR, META_FILE}

    def write_file(
        self,
        project_id: str,
        rel_path: str,
        content: str,
        base_revision: str = "",
        author: str = "",
    ) -> dict:
        root = self.project_root(project_id)
        target = self._resolve_inside(root, rel_path)
        rel = target.relative_to(root.resolve())
        if not rel.parts or rel.parts[0] in self.WRITE_FORBIDDEN_TOP:
            raise WorkspaceError("该路径不允许编辑", 403)
        if target.is_symlink():
            raise WorkspaceError("该路径不允许编辑", 403)
        # 乐观并发（方案 7.4 双写合并）：Web 编辑与 agent 写同一文件不互相静默覆盖
        if base_revision and target.is_file():
            current = _content_revision(target.read_bytes())
            if current != base_revision:
                raise WorkspaceError(
                    "文件已被他人或助手修改，请刷新对比后再保存", 409
                )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        # Web 端保存即成版本点：只提交本文件，与 agent 的阶段提交互不干扰
        rel_posix = rel.as_posix()
        commit = ""
        if _git(root, "status", "--porcelain", "--", rel_posix).strip():
            _git(root, "add", "--", rel_posix)
            who = f" by {author}" if author else ""
            _git(root, "commit", "-m", f"web: 保存 {rel_posix}{who}")
            commit = _git(root, "rev-parse", "HEAD").strip()
        return {
            "path": rel_path,
            "size": len(content.encode("utf-8")),
            "revision": _content_revision(content.encode("utf-8")),
            "commit": commit,
        }

    def file_at_revision(self, project_id: str, rev: str, rel_path: str) -> dict:
        if not re.fullmatch(r"[0-9a-fA-F]{6,40}|HEAD(~[0-9]+)?", rev):
            raise WorkspaceError("无效的版本号")
        root = self.project_root(project_id)
        self._resolve_inside(root, rel_path)
        out = _git(root, "show", f"{rev}:{rel_path}")
        return {"path": rel_path, "rev": rev, "content": out}

    def revert_to(self, project_id: str, commit: str) -> dict:
        """把工作区恢复为指定版本的内容并生成新提交（历史保留，可再回退）。

        已知限制：该版本之后新增的文件不会被删除。
        """
        if not re.fullmatch(r"[0-9a-fA-F]{6,40}", commit):
            raise WorkspaceError("无效的版本号")
        root = self.project_root(project_id)
        _git(root, "checkout", commit, "--", ".")
        status = _git(root, "status", "--porcelain")
        if not status.strip():
            raise WorkspaceError("当前内容与该版本一致，无需回退")
        _git(root, "add", "-A")
        _git(root, "commit", "-m", f"revert: 回退到 {commit[:8]}")
        return {"reverted_to": commit}

    def delete_resource(self, project_id: str, rel_path: str) -> dict:
        root = self.project_root(project_id)
        target = self._resolve_inside(root, rel_path)
        rel = target.relative_to(root.resolve())
        if len(rel.parts) < 2 or rel.parts[0] != "resource":
            raise WorkspaceError("只允许删除 resource/ 下的资料", 403)
        if rel.name == "materials.json" and len(rel.parts) == 2:
            raise WorkspaceError("资料用途清单不可直接删除", 403)
        with self._project_lock(project_id):
            if not target.is_file():
                raise WorkspaceError("文件不存在", 404)
            file_snapshot = self._snapshot(target)
            directives_path = root / "resource" / "materials.json"
            directives_snapshot = self._snapshot(directives_path)
            try:
                target.unlink()
                directives = self.resource_directives(project_id)
                if directives.pop(rel.as_posix(), None) is not None:
                    self._write_resource_directives(root, directives)
                self._update_meta_unlocked(root, resource_updated_at=int(time.time()))
            except Exception:
                self._restore_snapshot(target, file_snapshot)
                self._restore_snapshot(directives_path, directives_snapshot)
                raise
            return {"deleted": rel_path}

    def delete_file(self, project_id: str, rel_path: str, author: str = "") -> dict:
        """删除知识条目（限 knowledge/ 下）并记录版本。"""
        root = self.project_root(project_id)
        target = self._resolve_inside(root, rel_path)
        rel = target.relative_to(root.resolve())
        if rel.parts[0] != "knowledge":
            raise WorkspaceError("只允许删除 knowledge/ 下的条目", 403)
        if not target.is_file():
            raise WorkspaceError("文件不存在", 404)
        target.unlink()
        rel_posix = rel.as_posix()
        who = f" by {author}" if author else ""
        _git(root, "add", "-A", "--", rel_posix)
        _git(root, "commit", "-m", f"knowledge: 删除 {rel_posix}{who}", "--allow-empty")
        return {"deleted": rel_path}

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
        self._atomic_write_text(
            dest / META_FILE, json.dumps(meta, ensure_ascii=False, indent=2)
        )
        return meta

    # ---- 版本 ----

    def versions(self, project_id: str, limit: int = 50) -> list[dict]:
        root = self.project_root(project_id)
        out = _git(root, "log", f"--max-count={limit}", "--pretty=format:%H%x1f%ct%x1f%s")
        tags = self._tags_by_commit(root)
        entries = []
        for line in out.splitlines():
            parts = line.split("\x1f")
            if len(parts) == 3:
                entries.append(
                    {
                        "commit": parts[0],
                        "time": int(parts[1]),
                        "message": parts[2],
                        "tags": tags.get(parts[0], []),
                    }
                )
        # "已保存版本 N"：N = 距基线的序号，最早的提交是版本 1
        total = len(entries)
        for index, entry in enumerate(entries):
            entry["version"] = total - index
        return entries

    @staticmethod
    def _tags_by_commit(root: Path) -> dict[str, list[dict]]:
        out = _git(
            root,
            "for-each-ref",
            "refs/tags",
            "--format=%(refname:short)%1f%(*objectname)%(objectname)%1f%(contents:subject)",
        )
        tags: dict[str, list[dict]] = {}
        for line in out.splitlines():
            parts = line.split("\x1f")
            if len(parts) == 3:
                # 注释 tag 时 %(*objectname) 是所指 commit，轻量 tag 时为空、objectname 即 commit
                commit = parts[1][:40]
                tags.setdefault(commit, []).append({"name": parts[0], "label": parts[2]})
        return tags

    def tag_version(self, project_id: str, commit: str, label: str) -> dict:
        label = (label or "").strip()
        if not label:
            raise WorkspaceError("标记名称不能为空")
        if not re.fullmatch(r"[0-9a-fA-F]{6,40}", commit):
            raise WorkspaceError("无效的版本号")
        root = self.project_root(project_id)
        tag_name = f"mark-{int(time.time())}-{uuid.uuid4().hex[:4]}"
        _git(root, "tag", "-a", tag_name, "-m", label[:120], commit)
        return {"name": tag_name, "label": label, "commit": commit}

    def delete_tag(self, project_id: str, tag_name: str) -> dict:
        if not re.fullmatch(r"mark-[0-9]+-[0-9a-f]{4}", tag_name):
            raise WorkspaceError("无效的标记名")
        root = self.project_root(project_id)
        _git(root, "tag", "-d", tag_name)
        return {"deleted": tag_name}

    # ---- 内部 ----

    def _mount_assets(self, root: Path) -> None:
        links = {
            ".claude": self.assets_dir / "workspace-toolkit",
            "CLAUDE.md": self.assets_dir / "workspace-rules.md",
            "README.md": self.assets_dir / "workspace-readme.md",
        }
        for link_name, target in links.items():
            if target.exists():
                (root / link_name).symlink_to(target)

    def _copy_resource_template(self, root: Path) -> None:
        template = self.assets_dir / "resource-template"
        dest = root / "resource"
        if template.is_dir():
            shutil.copytree(template, dest)
        else:
            dest.mkdir()

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

    @staticmethod
    def _resolve_inside(root: Path, rel_path: str) -> Path:
        target = (root / rel_path).resolve()
        root_resolved = root.resolve()
        if not target.is_relative_to(root_resolved):
            raise WorkspaceError("路径越界", 403)
        return target


_PACKAGE_SCRIPT_STEM_RE = re.compile(r"[A-Za-z0-9_.-]+")


def _package_script_filename(key: str) -> str:
    """知识包内的脚本文件名，满足 modeling 的 entrypoint 契约 ^[A-Za-z0-9_.-]+\\.py$。

    ASCII 脚本名原样沿用；中文等其他脚本名按 key 的哈希取稳定文件名，
    脚本 key 与标题仍保留原名。
    """
    if _PACKAGE_SCRIPT_STEM_RE.fullmatch(key) and ".." not in key:
        return f"{key}.py"
    return f"script_{hashlib.sha1(key.encode('utf-8')).hexdigest()[:10]}.py"


def _load_script_contract():
    """脚本契约解析以 workspace-toolkit/scripts/script_contract.py 为单一事实源。

    建模阶段的 ontology_checker.py --include-scripts 与导出时共用同一份实现，
    避免两处规则漂移。
    """
    path = Path(config.ASSETS_DIR) / "workspace-toolkit" / "scripts" / "script_contract.py"
    spec = importlib.util.spec_from_file_location("onto_workspace_script_contract", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_script_contract = _load_script_contract()
ScriptContractError = _script_contract.ScriptContractError
_script_header_meta = _script_contract.parse_script_contract


def _first_heading(path: Path) -> str:
    """markdown 首个一级标题作 index 清单的标题列；读不到则退回文件名。"""
    try:
        with open(path, encoding="utf-8") as fh:
            for _ in range(20):
                line = fh.readline()
                if not line:
                    break
                if line.startswith("# "):
                    return line[2:].strip()[:80]
    except OSError:
        pass
    return ""


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:24] or "task"


def _content_revision(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _default_engine() -> str:
    """新建/导入项目写入 meta 的引擎：取配置中心「模型接入」的默认引擎。

    建项目时定下来而不是读取时回落，存量项目才不会被后续改设置带着走。
    """
    from . import model_settings

    return model_settings.default_engine()


_WIKILINK_RE = re.compile(r"\[\[([^\]|#\n]+)")


def _strip_frontmatter(text: str) -> str:
    """去掉源文件自带的 frontmatter，避免与导出注入的 frontmatter 叠加。"""
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            return text[end + 4 :].lstrip("\n")
    return text


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        raise WorkspaceError(f"git {args[0]} 失败: {result.stderr.strip()}", 500)
    return result.stdout


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
