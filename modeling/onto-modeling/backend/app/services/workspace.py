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
from . import consumer_package_rules, material_detect, package_mapping, package_operations, solver_contract
from .document_metadata import report_name
from .name_conflict import MAX_DISPLAY_NAME_LENGTH, detect_name_conflict
from .toolkit import script_argument_shape as _script_argument_shape
from .toolkit import script_contract as _script_contract
from .toolkit import script_descriptor as _script_descriptor

META_FILE = ".onto-web.json"  # 历史文件名保留：存量项目的识别键（ADR-0006）
PURGE_MARKER = ".onto-web.purging"  # 原地清除兜底：顶替 META_FILE，重启清扫据此认领残留
# 删除时 Windows 上刚被杀的进程要片刻才释放目录句柄，rename/rmtree 短暂重试
_DELETE_RETRIES = 5
_DELETE_RETRY_SECONDS = 0.2
SKIP_DIRS = {".git", ".ontology-state", ".obsidian"}
RESOURCE_SUBDIRS = {"diagrams", "excel", "others"}
# 领域资料的所有页面入口最终都调用 save_resource；这里是不可绕过的服务层契约。
MAX_RESOURCE_FILE_SIZE = 20 * 1024 * 1024
RESOURCE_FILE_SUFFIXES = {
    ".csv",
    ".doc",
    ".docx",
    ".drawio",
    ".jpeg",
    ".jpg",
    ".md",
    ".pdf",
    ".png",
    ".txt",
    ".webp",
    ".xls",
    ".xlsm",
    ".xlsx",
}


def _literal_pathspec(path: str) -> str:
    """Return a Git pathspec that never treats user-controlled names as globs."""
    return f":(literal){path}"


def _literal_pathspecs(paths) -> list[str]:
    return [_literal_pathspec(str(path)) for path in paths]
# 已发布知识包导入时的原包留底区：随 git 版本化（跨环境迁移要带走），
# 但不是用户可编辑内容（隐藏目录，不进文件树）。
IMPORTED_PACKAGE_DIR = ".imported-package"
# 创建时填写的建模目标：pending 待用户在开始建模前确认，confirmed 已确认；
# 未填目标或存量项目无此字段，不出确认卡也不注入提示词。
GOAL_PENDING = "pending"
GOAL_CONFIRMED = "confirmed"


def confirmed_goal(meta: dict) -> str:
    """已确认的创建目标原文；未确认或存量项目返回空串（不注入助手提示词）。"""
    if meta.get("goal_status") != GOAL_CONFIRMED:
        return ""
    return str(meta.get("background") or "").strip()

DEFAULT_GITIGNORE = """.ontology-state/
.claude
CLAUDE.md
README.md
tmp/
"""

# Version rollback restores only project content. Repository plumbing, project
# metadata and runtime state are intentionally kept at their current values.
ROLLBACK_EXCLUDED_TOP = {
    ".git",
    ".gitignore",
    ".gitattributes",
    ".gitmodules",
    ".claude",
    ".ontology-state",
    "tmp",
    META_FILE,
    "CLAUDE.md",
    "README.md",
}


_PROJECT_LOCKS: dict[str, threading.RLock] = {}
_PROJECT_LOCKS_GUARD = threading.Lock()
# 同名校验到项目落盘之间不能被另一个同名请求插队
_NAME_CLAIM_LOCK = threading.Lock()


class WorkspaceError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "workspace_error"):
        super().__init__(message)
        self.status = status
        self.code = code


class NameConflictError(WorkspaceError):
    """任务名称与查重范围内的已有任务重名；conflict 带候选名，供前端改名后重试。"""

    def __init__(self, message: str, conflict: dict):
        super().__init__(message, 409, "project_name_conflict")
        self.conflict = conflict


def _is_auto_detected(directive) -> bool:
    return isinstance(directive, dict) and bool(directive.get("auto_detected"))


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

        background = (background or "").strip()
        meta = {
            "id": project_id,
            "name": name,
            "created_at": int(time.time()),
            "owner": owner or "",
            "background": background,
            "engine": _default_engine(),
        }
        if background:
            # 创建时填了目标：开始建模前须经确认卡确认，确认后才注入助手提示词
            meta["goal_status"] = GOAL_PENDING
        self._atomic_write_text(
            root / META_FILE, json.dumps(meta, ensure_ascii=False, indent=2)
        )

        _git(root, "init")
        _git(root, "config", "user.name", "Onto Modeling")
        _git(root, "config", "user.email", "onto-modeling@example.invalid")
        _git(root, "add", "-A")
        _git(root, "commit", "-m", "init: 任务工作区基线", "--allow-empty")
        return meta

    # 与前端 LeftPane STAGES 一一对应的目录探针；完成数与前端 stageStatuses 的
    # finish 数一致：逐阶段独立计数，必选阶段缺失不截断后续（导入包、空 research/
    # 等后续阶段已有产物的项目不再显示 0）。optional 只决定前端展示，不影响计数。
    # index.md 是目录索引，不算产物。
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
        # 求解适配（可选）：受管目录 ontology/solver/<key>/
        (("ontology/solver",), True),
        # 数据评测（可选，方案 eval-optimization-integration）：探针为报告文件 glob
        (("reports/eval-*.md",), True),
    )
    STAGE_TOTAL = len(STAGES)
    # 资料分析在 STAGES 中的位置：它不按目录探针判完成，改读验收回执（issue #362）
    RESEARCH_STAGE_INDEX = 0
    # 与前端 packagePublish.js 的发布必选阶段保持一致。这里是服务端最终裁决，
    # 前端仍可用同一规则即时提示，但 API 与 MCP 均不能绕过。
    PUBLISH_REQUIRED_STAGES = (
        ("research", "资料分析", ("research",)),
        ("object", "对象提取", ("ontology/object",)),
        ("operation", "操作提取", ("ontology/operation", "ontology/logic")),
        ("validate", "质量检查", ("reports",)),
    )

    @staticmethod
    def _dir_has_entries(directory: Path) -> bool:
        if not directory.is_dir():
            return False
        return any(
            entry
            for entry in directory.iterdir()
            if not entry.name.startswith(".") and entry.name.lower() != "index.md"
        )

    @staticmethod
    def _stage_has_visible_file(root: Path, rel_paths: tuple[str, ...]) -> bool:
        """Match the frontend file-tree probe: visible files except directory indexes count."""
        for rel_path in rel_paths:
            directory = root / rel_path
            if not directory.is_dir() or directory.is_symlink():
                continue
            for current, dirs, files in os.walk(directory, followlinks=False):
                current_path = Path(current)
                visible_dirs = []
                for name in dirs:
                    if not name.startswith(".") and not (current_path / name).is_symlink():
                        visible_dirs.append(name)
                dirs[:] = visible_dirs
                for name in files:
                    path = current_path / name
                    if not name.startswith(".") and name.lower() != "index.md" and not path.is_symlink():
                        return True
        return False

    @classmethod
    def _research_stage_blocking(cls, root: Path, review: dict) -> bool:
        return (
            review["status"] in (_research_review.PENDING, _research_review.FAILED, _research_review.UNVERIFIABLE)
            and not cls._dir_has_entries(root / "ontology" / "object")
        )

    def package_publish_readiness(self, project_id: str) -> dict:
        """Return the same four required-stage decisions used by the current project page."""
        root = self.project_root(project_id)
        review = _research_review.review_status(root)
        research_blocking = self._research_stage_blocking(root, review)
        stages = []
        for key, title, rel_paths in self.PUBLISH_REQUIRED_STAGES:
            finished = self._stage_has_visible_file(root, rel_paths)
            if key == "research" and research_blocking:
                finished = False
            stages.append({"key": key, "title": title, "finished": finished})
        missing = []
        for item in stages:
            if not item["finished"]:
                missing.append({"key": item["key"], "title": item["title"]})
        return {"ready": not missing, "stages": stages, "missing_stages": missing}

    def _ensure_package_publish_stages(self, project_id: str) -> None:
        readiness = self.package_publish_readiness(project_id)
        if readiness["ready"]:
            return
        titles = "、".join(item["title"] for item in readiness["missing_stages"])
        raise WorkspaceError(
            f"建模未完成，请先完成：{titles}",
            409,
            "modeling_stages_incomplete",
        )

    @classmethod
    def _project_progress(cls, root: Path) -> dict:
        review = _research_review.review_status(root)
        # 验收只管"进入对象提取之前"：报告没通过、对象也还没提取时才挡住后续阶段。
        # 对象一旦提取，资料分析按已越过处理，只留待核验的提醒——与 state_helper
        # 的"已到达的最高阶段优先"同口径。否则旧项目升级后进度会整体归零，回执
        # 因打标签、换评测表而失效时也一样。
        research_blocking = cls._research_stage_blocking(root, review)
        done = 0
        for index, (dirs, _optional) in enumerate(cls.STAGES):
            hit = any(
                (next(root.glob(rel), None) is not None)
                if "*" in rel
                else cls._dir_has_entries(root / rel)
                for rel in dirs
            )
            # 资料分析：报告落盘不等于内容已验收，挡着的时候不计完成（与前端 stageHits 同口径）。
            # 有没有报告以回执状态为准，不看目录探针。
            if index == cls.RESEARCH_STAGE_INDEX:
                hit = review["status"] != _research_review.NONE and not research_blocking
            if hit:
                done += 1

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
            "resource_stale": cls._resource_stale(root),
            "objects": count_md("ontology/object"),
            "operations": count_md("ontology/operation") + count_md("ontology/logic"),
        }

    @classmethod
    def _resource_stale(cls, root: Path) -> bool:
        """Whether resources differ from those consumed by the latest research analysis."""
        try:
            review = _research_review.load_review(root)
            inputs = review.get("inputs") if isinstance(review, dict) else None
            if isinstance(inputs, dict):
                resource_inputs = {
                    rel: revision
                    for rel, revision in inputs.items()
                    if rel == "resource/materials.json" or rel.startswith("resource/")
                }
                if resource_inputs:
                    return any(
                        _research_review.file_revision(root / rel) != revision
                        for rel, revision in resource_inputs.items()
                    )

            # 兼容没有分析回执的旧项目：只认真正的资料分析报告提交。
            # ontology、评测报告和派生 index.md 都不能证明新资料已被分析。
            resource_commit = _git(
                root, "log", "-1", "--format=%H", "--", "resource"
            ).strip()
            research_commit = cls._latest_research_content_commit(root)
            if not resource_commit or not research_commit:
                return False
            return int(
                _git(
                    root,
                    "rev-list",
                    "--count",
                    f"{research_commit}..{resource_commit}",
                ).strip()
                or "0"
            ) > 0
        except (ValueError, WorkspaceError):
            return False

    @staticmethod
    def _latest_research_content_commit(root: Path) -> str:
        """Latest commit touching a research artifact other than generated indexes."""
        history = _git(
            root,
            "log",
            "--format=%x1e%H",
            "--name-only",
            "--",
            "research",
        )
        for record in history.split("\x1e"):
            lines = [line.strip() for line in record.splitlines() if line.strip()]
            if not lines:
                continue
            commit, paths = lines[0], lines[1:]
            if any(Path(path).name.lower() != "index.md" for path in paths):
                return commit
        return ""

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

        查重范围是当前用户可见的任务：viewer 为（用户名, 是否管理员），鉴权关闭时
        传 None，全部任务都算。重名抛 NameConflictError（409），附候选名。
        exclude_id 是重命名时的任务自身：只改自己名称的大小写或全半角不算重名。
        """
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
            metas = self.visible_to(metas, *viewer)
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

    def set_members(self, project_id: str, members: dict[str, str]) -> dict:
        for username, role in members.items():
            if role not in ("editor", "viewer"):
                raise WorkspaceError(f"成员 {username} 的角色必须是 editor 或 viewer")
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", username):
                raise WorkspaceError(f"无效的成员用户名：{username}")
        return self._update_meta(project_id, members=members)

    def get_project(self, project_id: str) -> dict:
        with self._project_lock(project_id):
            return self._read_meta_unlocked(self.project_root(project_id))

    @staticmethod
    def _read_meta_unlocked(root: Path) -> dict:
        # 已持有项目锁时用它读元数据：再进 get_project 会在同一锁文件上二次 flock 而自锁
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
        """迁移旧 ontology/logic 目录、按脚本同步描述符并刷新各目录 index.md。

        仅当工作区原本干净时才提交版本；agent 会话中途的改动留给
        agent 自己的提交收编，避免交叉提交打断其暂存状态。
        """
        with self._project_lock(project_id):
            root = self.project_root(project_id)
            managed_was_clean = not self._managed_dirty_paths(root)
            changed = self._migrate_legacy_logic(root)
            changed = bool(
                _script_descriptor.sync_workspace_descriptors(root / "ontology")
            ) or changed
            changed = self._refresh_indexes(root) or changed
            if changed and managed_was_clean:
                generated = sorted(
                    set(self._managed_dirty_paths(root))
                    | set(self._generated_index_dirty_paths(root))
                )
                if generated:
                    pathspecs = _literal_pathspecs(
                        self._minimal_pathspecs(generated)
                    )
                    _git(root, "add", "-A", "--", *pathspecs)
                    _git(
                        root,
                        "commit",
                        "-m",
                        "chore: 知识包目录规范化（operation 目录、脚本描述符与索引）",
                        "--",
                        *pathspecs,
                    )
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

    def preview_schema(self, project_id: str, rel_path: str, *, force: bool = False) -> dict:
        """Inspect a proposed schema tag without changing project metadata."""
        with self._project_lock(project_id):
            root = self.project_root(project_id)
            target = self._resolve_inside(root, rel_path)
            rel = target.relative_to(root.resolve()).as_posix()
            if not rel.startswith("resource/") or not target.is_file():
                raise WorkspaceError("只能检查资料区中存在的文件", 400)
            if target.suffix.lower() not in (".xlsx", ".xlsm", ".csv"):
                raise WorkspaceError("数据 Schema 支持 xlsx、xlsm 和 UTF-8 CSV。请将文件另存为这些格式；说明文档可设为通用业务依据。", 400)
            previous = self.resource_directives(project_id).get(rel, {})
            try:
                scope = material_detect.resolve_selection(target, previous, force=force)
            except material_detect.SheetSelectionError as exc:
                try:
                    scope = material_detect.resolve_selection(target)
                except material_detect.SheetSelectionError:
                    raise WorkspaceError("文件无法解析为数据表：可能已损坏、加密或编码不正确。请用表格软件确认可打开，取消密码保护并另存为 xlsx/xlsm；CSV 请使用 UTF-8 编码后重新上传。", 400) from exc
                scope["error"] = str(exc)
            scope["included_sheets"] = [s["name"] for s in scope["sheets"]
                if s["name"] in scope["included_sheets"] and s["schema_selectable"]]
            scope["reference_sheets"] = [s["name"] for s in scope["sheets"]
                if s["name"] not in scope["included_sheets"]]
            return scope

    def set_resource_directive(
        self, project_id: str, rel_path: str, purpose: str, reading_mode: str = "on_demand",
        schema_selection: dict | None = None, author: str = "",
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
            directives_snapshot = self._snapshot(directives_path)
            meta_path = root / META_FILE
            meta_snapshot = self._snapshot(meta_path)
            directives_rel = directives_path.relative_to(root).as_posix()
            added_intent = False
            commit = ""
            old_head = _git(root, "rev-parse", "HEAD").strip()
            rel_spec = _literal_pathspec(rel)
            directives_spec = _literal_pathspec(directives_rel)
            if not _git(root, "ls-files", "--", rel_spec).strip():
                raise WorkspaceError(
                    "该资料尚未纳入项目版本，请重新上传资料后再保存用途或 Sheet 范围",
                    409,
                    "resource_source_unversioned",
                )
            if _git(root, "status", "--porcelain", "--", rel_spec).strip():
                raise WorkspaceError(
                    "该资料存在尚未保存为项目版本的内容，请重新上传或恢复资料后再修改设置",
                    409,
                    "resource_source_modified",
                )
            if _git(
                root, "diff", "--cached", "--name-only", "--", directives_spec
            ).strip():
                raise WorkspaceError(
                    "资料设置已被其他编辑暂存，请完成或放弃该编辑后重试",
                    409,
                    "resource_directive_conflict",
                )
            try:
                directives = self.resource_directives(project_id)
                previous = directives.get(rel, {})
                directive = {"purpose": purpose, "reading_mode": reading_mode}
                if purpose == "schema" and target.suffix.lower() in package_mapping.TABLE_SUFFIXES:
                    if schema_selection is not None:
                        if schema_selection.get("file_sha256") != material_detect.resolve_selection(target)["file_sha256"]:
                            raise WorkspaceError("文件已变化，请刷新 Sheet 范围后重试", 409)
                        directive["schema_selection"] = {**schema_selection, "version": 1}
                    elif previous.get("schema_selection") is not None:
                        directive["schema_selection"] = previous["schema_selection"]
                    scope = material_detect.resolve_selection(target, directive)
                    if schema_selection is not None:
                        problems = []
                        included_sheets = set(scope["included_sheets"])
                        for sheet in scope["sheets"]:
                            if sheet["name"] not in included_sheets:
                                continue
                            for issue in sheet.get("issues", []):
                                if issue["severity"] == "error":
                                    problems.append(
                                        f"Sheet「{sheet['name']}」：{issue['message']}"
                                    )
                        if problems:
                            raise WorkspaceError("\n".join(problems), 400)
                    directive["schema_selection"] = material_detect.selection_metadata(scope)
                elif schema_selection is not None:
                    raise WorkspaceError("仅数据 Schema 表格支持设置 Sheet 范围", 400)
                changed = previous != directive
                committed_directives = self._resource_directives_at_head(root, directives_rel)
                version_changed = committed_directives.get(rel, {}) != directive
                if changed:
                    directives[rel] = directive
                    self._write_resource_directives(root, directives)
                    # 用途变化会影响派生产物的口径，视同资料更新
                    self._update_meta_unlocked(root, resource_updated_at=int(time.time()))
                if version_changed:
                    committed_directives[rel] = directive
                    self._write_resource_directives(root, committed_directives)
                if version_changed and _git(
                    root, "status", "--porcelain", "--", directives_spec
                ).strip():
                    # --only creates the version from this managed settings file without
                    # including unrelated staged or working-tree edits. A new file needs
                    # an intent-to-add entry before git commit --only can address it.
                    if not _git(root, "ls-files", "--", directives_spec).strip():
                        _git(root, "add", "-N", "--", directives_spec)
                        added_intent = True
                    who = f" by {author}" if author else ""
                    _git(root, "commit", "--only", "-m", f"web: 保存资料设置 {rel}{who}",
                         "--", directives_spec)
                    commit = _git(root, "rev-parse", "HEAD").strip()
                if version_changed:
                    self._write_resource_directives(root, directives)
            except Exception:
                if commit and _git(root, "rev-parse", "HEAD").strip() == commit:
                    _git(root, "reset", "--soft", old_head)
                    _git(root, "reset", "--", directives_spec)
                self._restore_snapshot(directives_path, directives_snapshot)
                self._restore_snapshot(meta_path, meta_snapshot)
                if added_intent:
                    _git(root, "reset", "--", directives_spec)
                raise
            return {"path": rel, **directives[rel], "commit": commit}

    @staticmethod
    def _resource_directives_at_head(root: Path, rel: str) -> dict:
        if not _git(
            root, "ls-tree", "--name-only", "HEAD", "--", _literal_pathspec(rel)
        ).strip():
            return {}
        try:
            data = json.loads(_git(root, "show", f"HEAD:{rel}"))
        except json.JSONDecodeError as exc:
            raise WorkspaceError(
                "已保存版本中的资料用途元数据不可用，请修复后重试",
                503,
                "resource_metadata_unavailable",
            ) from exc
        if not isinstance(data, dict):
            raise WorkspaceError(
                "已保存版本中的资料用途元数据不可用，请修复后重试",
                503,
                "resource_metadata_unavailable",
            )
        return data

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
        author: str = "",
    ) -> dict:
        if subdir not in RESOURCE_SUBDIRS:
            raise WorkspaceError(f"资料分类必须是 {sorted(RESOURCE_SUBDIRS)} 之一")
        safe_name = Path(filename).name
        if not safe_name or safe_name.startswith(".") or safe_name != filename:
            raise WorkspaceError("无效的文件名")
        suffix = Path(safe_name).suffix.lower()
        if suffix not in RESOURCE_FILE_SUFFIXES:
            raise WorkspaceError(
                f"不支持文件“{safe_name}”的类型",
                400,
                "unsupported_resource_file_type",
            )
        if len(data) > MAX_RESOURCE_FILE_SIZE:
            raise WorkspaceError(
                f"文件“{safe_name}”超过 20 MB，无法上传",
                413,
                "resource_file_too_large",
            )
        with self._project_lock(project_id):
            root = self.project_root(project_id)
            dest_dir = root / "resource" / subdir
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / safe_name
            replaced = dest.exists()
            file_snapshot = self._snapshot(dest)
            directives_path = root / "resource" / "materials.json"
            directives_snapshot = self._snapshot(directives_path)
            meta_path = root / META_FILE
            meta_snapshot = self._snapshot(meta_path)
            rel = f"resource/{subdir}/{safe_name}"
            directives_rel = directives_path.relative_to(root).as_posix()
            managed_paths = [rel, directives_rel]
            managed_specs = _literal_pathspecs(managed_paths)
            old_head = _git(root, "rev-parse", "HEAD").strip()
            commit = ""
            intent_paths = []
            staged = _git(
                root, "diff", "--cached", "--name-only", "--", *managed_specs
            ).splitlines()
            if staged:
                raise WorkspaceError(
                    "该资料或资料设置已被其他编辑暂存，请完成或放弃该编辑后重试",
                    409,
                    "resource_upload_conflict",
                )
            try:
                self._atomic_write(dest, data)
                detected = self._update_upload_directive(project_id, rel, purpose, reading_mode)
                upload_changed = file_snapshot != (True, data) or (
                    directives_snapshot != self._snapshot(directives_path)
                )
                if upload_changed:
                    # 变更提醒依据：前端对比该时间与派生产物版本，提示"资料已更新，可让助手增量刷新"
                    self._update_meta_unlocked(root, resource_updated_at=int(time.time()))
                working_directives = self.resource_directives(project_id)
                committed_directives = self._resource_directives_at_head(
                    root, directives_rel
                )
                current_directive = working_directives.get(rel)
                if current_directive is None:
                    committed_directives.pop(rel, None)
                else:
                    committed_directives[rel] = current_directive

                directives_changed = (
                    self._resource_directives_at_head(root, directives_rel)
                    != committed_directives
                )
                if directives_changed:
                    self._write_resource_directives(root, committed_directives)

                commit_paths = [rel]
                if directives_changed:
                    commit_paths.append(directives_rel)
                commit_specs = _literal_pathspecs(commit_paths)
                changed_paths = _git(
                    root, "status", "--porcelain", "--", *commit_specs
                ).strip()
                if changed_paths:
                    for path, pathspec in zip(commit_paths, commit_specs):
                        if not _git(root, "ls-files", "--", pathspec).strip():
                            _git(root, "add", "-N", "--", pathspec)
                            intent_paths.append(path)
                    who = f" by {author}" if author else ""
                    action = "替换" if replaced else "上传"
                    _git(
                        root,
                        "commit",
                        "--only",
                        "-m",
                        f"web: {action}资料 {rel}{who}",
                        "--",
                        *commit_specs,
                    )
                    commit = _git(root, "rev-parse", "HEAD").strip()
                if directives_changed:
                    self._write_resource_directives(root, working_directives)
            except Exception:
                if commit and _git(root, "rev-parse", "HEAD").strip() == commit:
                    _git(root, "reset", "--soft", old_head)
                    _git(root, "reset", "--", *managed_specs)
                self._restore_snapshot(dest, file_snapshot)
                self._restore_snapshot(directives_path, directives_snapshot)
                self._restore_snapshot(meta_path, meta_snapshot)
                if intent_paths:
                    _git(
                        root,
                        "reset",
                        "--",
                        *_literal_pathspecs(intent_paths),
                    )
                raise
            result = {
                "path": rel,
                "size": len(data),
                "replaced": replaced,
                "commit": commit,
            }
            if detected:
                result["detected"] = detected
            return result

    def _update_upload_directive(
        self, project_id: str, rel: str, purpose: str, reading_mode: str
    ) -> dict | None:
        """写入上传时指定的用途；未指定（auto）时按结构识别预填。

        判为数据表或术语表就标 schema / term 并记 auto_detected，用户之后在资料区改用途即覆盖。
        替换文件时只重判没有条目或此前自动标注的，不动人工标注。返回识别结果（未预填为 None）。
        """
        root = self.project_root(project_id)
        directives = self.resource_directives(project_id)
        before = json.dumps(directives, sort_keys=True)
        existing = directives.get(rel)
        reading_mode = reading_mode if reading_mode in self.MATERIAL_READING_MODES else "on_demand"
        if purpose != "auto" or reading_mode != "on_demand":
            directives[rel] = {
                "purpose": purpose if purpose in self.MATERIAL_PURPOSES else "auto",
                "reading_mode": reading_mode,
            }
        detected = None
        redetectable = existing is None or _is_auto_detected(existing)
        if purpose == "auto" and redetectable:
            detected = material_detect.detect_material(root / rel)
        if detected:
            directives[rel] = {
                "purpose": detected["purpose"],
                "reading_mode": reading_mode,
                "auto_detected": True,
            }
        elif _is_auto_detected(directives.get(rel)):
            # 替换后不再像数据表或术语表：撤掉之前的自动标注
            directives.pop(rel)
        directive = directives.get(rel, {})
        if directive.get("purpose") == "schema" and Path(rel).suffix.lower() in package_mapping.TABLE_SUFFIXES:
            previous_scope = (existing or {}).get("schema_selection")
            if isinstance(previous_scope, dict) and previous_scope.get("mode") == "manual":
                directive["schema_selection"] = previous_scope
            stored = directive.get("schema_selection")
            # Replacements invalidate manual scope; preserve it so readers demand confirmation.
            if not isinstance(stored, dict) or stored.get("mode") != "manual":
                scope = material_detect.resolve_selection(root / rel, directive)
                directive["schema_selection"] = material_detect.selection_metadata(scope)
        if json.dumps(directives, sort_keys=True) != before:
            self._write_resource_directives(root, directives)
        return detected

    def schema_scopes(self, project_id: str) -> dict:
        root = self.project_root(project_id)
        scopes = {}
        for rel, directive in self.resource_directives(project_id).items():
            if not isinstance(directive, dict) or directive.get("purpose") != "schema":
                continue
            if Path(rel).suffix.lower() not in package_mapping.TABLE_SUFFIXES:
                continue
            try:
                target = self._resolve_inside(root, rel)
                if not rel.startswith("resource/") or not target.is_file():
                    continue
                try:
                    scopes[rel] = material_detect.resolve_selection(target, directive)
                except material_detect.SheetSelectionError as exc:
                    # Show fresh candidates but never silently apply them to manual selections.
                    scopes[rel] = {**material_detect.resolve_selection(target), "error": str(exc)}
            except (material_detect.SheetSelectionError, WorkspaceError) as exc:
                scopes[rel] = {"error": str(exc), "sheets": [], "included_sheets": []}
        return scopes

    def schema_candidates(self, project_id: str) -> dict:
        """资料区「疑似数据表」：用途仍为「由助手判断」、结构上判为数据表的表格资料。"""
        root = self.project_root(project_id)
        directives = self.resource_directives(project_id)
        candidates = {}
        for rel in self._unmarked_table_resources(project_id, root):
            directive = directives.get(rel)
            if isinstance(directive, dict) and directive.get("purpose") != "auto":
                continue
            detected = material_detect.detect_data_table(root / rel)
            if detected:
                candidates[rel] = detected
        return candidates

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

    def confirm_goal(self, project_id: str, background: str | None = None) -> dict:
        """开始建模前确认创建时填写的目标；传入 background 表示用户改写后再确认。"""
        with self._project_lock(project_id):
            root = self.project_root(project_id)
            meta = self._read_meta_unlocked(root)
            if meta.get("goal_status") != GOAL_PENDING:
                raise WorkspaceError(
                    "建模目标已确认，如需调整请在对话中告诉助手", 409, "goal_not_pending"
                )
            fields = {"goal_status": GOAL_CONFIRMED}
            if background is not None:
                text = background.strip()
                if not text:
                    raise WorkspaceError("请填写建模目标")
                fields["background"] = text
            return self._update_meta_unlocked(root, **fields)

    def set_archived(self, project_id: str, archived: bool) -> dict:
        return self._update_meta(project_id, archived=bool(archived))

    # 克隆/导入时不带过去的顶层条目：资产由本服务重新挂载，git 历史不迁移
    CLONE_SKIP_TOP = {".claude", "CLAUDE.md", "README.md", ".git", ".ontology-state", META_FILE}
    CLONE_NAME_SUFFIX = "-副本"

    def clone_name(self, project_id: str) -> str:
        """克隆的默认名称「原名-副本」；原名过长时截断，保证不超过名称长度上限。"""
        name = self.get_project(project_id)["name"]
        keep = MAX_DISPLAY_NAME_LENGTH - len(self.CLONE_NAME_SUFFIX)
        return name[:keep].rstrip() + self.CLONE_NAME_SUFFIX

    def clone_project(self, project_id: str, owner: str = "", name: str = "") -> dict:
        source_root = self.project_root(project_id)
        meta = self.create_project(name or self.clone_name(project_id), owner=owner)
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
        object_texts: dict[str, str] | None = None,
        version: str | None = None,
        ontology_id: str | None = None,
    ) -> Path:
        """发布为知识包（consumer 可导入的 ontology-package v0.1）。

        对齐 consumer/backend/ontology/package_import.py 的校验契约：
        manifest.yaml 六必填字段、objects/operations 目录 + frontmatter、
        operation 必须声明 uses.objects、index 首列与详情 key 逐字相等。

        binding：评测导入时传入按本轮导入工作簿算出的绑定，mapping 与导入库同源；
        所有模式均将「关联对象」转为 `# 关系` 表，graph 模式额外补齐物理关联字段；
        结构错误在导出前拒绝；未绑定物理表的合法关系保留，图导入提示记进 relation_report["notes"]；
        allow_unbound：降级发布——声明对不上的对象不阻断，来源Sheet 写 UNBOUND、不进 mapping。

        工作区有求解契约（ontology/solver/）时升级为 v0.2 operation-centric.v1：求解
        Operation 改名为 solver_key 并补 execution 声明，sidecar 原字节写到 operations/
        （受管模板的输入 Schema 例外：按评估器补装置与原油品种的可选值），
        manifest 与两级 index 带上求解声明。没有求解契约的包内容保持 v0.1。

        ZIP 按 _ReproducibleZip 确定性写出：同一工作区内容重复导出逐字节相同。
        """
        root = self.project_root(project_id)
        meta = self.get_project(project_id)
        version = version or f"0.1.{len(self.versions(project_id))}"
        pkg = ontology_id or meta["id"]
        if not re.fullmatch(r'[a-zA-Z0-9_][a-zA-Z0-9_-]{0,127}', pkg):
            raise WorkspaceError('知识包标识无效')

        report_names: dict[tuple[str, str], str] = {}

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
                    raw = path.read_text(encoding="utf-8")
                    if path.suffix == ".md":
                        try:
                            original_name = report_name(raw)
                        except ValueError as exc:
                            raise WorkspaceError(f"{rel_dir}/{path.name}: {exc}") from exc
                        if original_name is not None:
                            resource = "operation" if directory.name == "logic" else directory.name
                            report_names[(resource, path.stem)] = original_name
                    items.append((path.stem, _strip_frontmatter(raw)))
            return items

        object_texts = object_texts if object_texts is not None else package_mapping.read_object_texts(root, strict=True)
        objects = []
        for key, raw in sorted(object_texts.items()):
            try:
                original_name = report_name(raw)
            except ValueError as exc:
                raise WorkspaceError(f"ontology/object/{key}.md: {exc}") from exc
            if original_name is not None:
                report_names[("object", key)] = original_name
            objects.append((key, _strip_frontmatter(raw)))
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
        # Keep semantic declarations even when their endpoints have no physical table.
        # Reject structural errors before normalization could discard them.
        graph_edges, relation_problems = package_mapping.package_relations(
            object_texts, reject_duplicate_relations=True, strict_fields=True,
        )
        if relation_problems:
            raise WorkspaceError('对象关系结构无效：' + '；'.join(relation_problems[:20]),
                                 422, 'EVALUATION_PACKAGE_INVALID')
        if graph:
            package_mapping.graph_relations(object_texts, set(mappings["objects"]), relation_report)
        elif relation_report is not None:
            relation_report.update(notes=[])
        rules = entry_items("ontology/rule")
        metrics = entry_items("ontology/metric")
        scripts = entry_items("ontology/scripts", pattern="*.py")
        object_keys = {key for key, _ in objects}
        solver_contracts, solver_problems = solver_contract.load_workspace_solver(
            root, [key for key, _ in operations]
        )
        if solver_problems:
            raise WorkspaceError(
                "求解契约无法发布：" + "；".join(f"{p}（{p.path}）" for p in solver_problems),
                code=solver_problems[0].code,
            )
        solver_evaluators = self.solver_check(project_id).evaluators if solver_contracts else {}
        solver = solver_contract.render_package_solver(
            solver_contracts, {key: root / rel for key, rel in solver_evaluators.items()}
        )
        solver_by_stem = {item.operation: item for item in solver.contracts}

        def package_operation_key(stem: str) -> str:
            item = solver_by_stem.get(stem)
            return item.solver_key if item else stem

        operation_bodies = dict(operations)
        relations, operation_parents = package_operations.relation_view(operations)
        # 脚本契约与对外绑定（每个 operation 只对外一个脚本、子操作与主操作脚本的覆盖关系）
        # 与规则检查 ontology_checker --include-scripts 同一实现；写包之前拦下，不留半成品
        script_contracts = {}
        for key, code in scripts:
            try:
                script_contracts[key] = _script_contract.parse_script(code)
            except ScriptContractError as exc:
                raise WorkspaceError(f"脚本 {key} 契约无效：{exc}") from exc
        script_bindings = _script_descriptor.resolve_script_bindings(
            script_contracts, relations, set(relations)
        )
        if script_bindings.errors:
            raise WorkspaceError(
                "脚本绑定不符合规则，无法发布：" + "；".join(script_bindings.messages()),
                code="script_binding_conflict",
            )
        # 入参形态：会被 consumer 调用前的参数证据校验拒绝的脚本（一拒就可能终止整轮对话）不发布
        object_documents = dict(objects)
        shape_errors = _script_descriptor.argument_shape_errors(
            script_contracts,
            script_bindings.bindings,
            _script_argument_shape.attributes_from_documents(object_documents),
        )
        object_enums = _script_argument_shape.enum_values_from_documents(object_documents)
        if shape_errors:
            raise WorkspaceError(
                "脚本入参会被 consumer 拒绝，无法发布：" + "；".join(message for _name, message in shape_errors),
                code="script_argument_shape",
            )

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
        if solver.contracts:
            manifest = manifest.replace(
                "schema_version: '0.1'\n", "schema_version: '0.2'\n"
            ) + solver_contract.manifest_solver_section(solver)

        def front(resource: str, key: str, title: str, extra: str = "") -> str:
            original_name = report_names.get((resource, key))
            if original_name is not None:
                extra += f"report_name: {q(original_name)}\n"
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

        with _ReproducibleZip(dest_path) as zf:
            zf.writestr(f"{pkg}/manifest.yaml", manifest)
            zf.writestr(
                f"{pkg}/index.md",
                front("root", "root", meta["name"]) + f"# {meta['name']}\n"
                + solver_contract.index_block(solver),
            )
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
                body = package_mapping.rewrite_relations_section(body, graph_edges.get(key, []))
                zf.writestr(
                    f"{pkg}/objects/{key}.md",
                    front("object", key, key, extra) + self._sanitize_placeholder_aliases(body),
                )
            if mappings["objects"]:
                zf.writestr(
                    f"{pkg}/mappings/object-data-mappings.json",
                    json.dumps(mappings, ensure_ascii=False, indent=2) + "\n",
                )
            if solver.contracts:
                # execution 列让 consumer 认出求解行；停用的求解 operation 不进求解索引
                def operation_row(k: str) -> str:
                    item = solver_by_stem.get(k)
                    execution = solver_contract.SOLVER_EXECUTION_MODE if item and item.enabled else ""
                    return package_operations.index_row(
                        k, operation_bodies[k], operation_parents.get(k, []),
                        package_key=package_operation_key(k), execution=execution,
                    )

                operation_headers = ["operation key", "标题", "输入", "输出", "execution"]
            else:
                def operation_row(k: str) -> str:
                    return package_operations.index_row(k, operation_bodies[k], operation_parents.get(k, []))

                operation_headers = ["operation key", "标题", "输入", "输出"]
            zf.writestr(
                f"{pkg}/operations/index.md",
                index_md(
                    "operation_index", "operations", "操作目录", operations,
                    operation_headers, operation_row,
                ),
            )
            for key, body in operations:
                used = sorted({m for m in _WIKILINK_RE.findall(body) if m in object_keys})
                item = solver_by_stem.get(key)
                if item is None:
                    extra = f"uses:\n  objects: {json.dumps(used, ensure_ascii=False)}\n"
                    callees = [
                        package_operation_key(callee) for callee in package_operations.uses_operations(key, relations)
                    ]
                    if callees:
                        # 主操作按顺序调用的子操作：consumer 据此识别主/子操作并路由脚本
                        extra += f"  operations: {json.dumps(callees, ensure_ascii=False)}\n"
                    zf.writestr(f"{pkg}/operations/{key}.md", front("operation", key, key, extra) + body)
                    continue
                zf.writestr(
                    f"{pkg}/operations/{item.solver_key}.md",
                    solver_contract.operation_frontmatter(
                        item, key, used, object_keys, report_names.get(("operation", key))
                    ) + body,
                )
            for path, data in solver.files.items():
                zf.writestr(f"{pkg}/{path}", data)
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
                exposed = [(key, code) for key, code in scripts if key in script_bindings.bindings]
                if exposed:
                    zf.writestr(
                        f"{pkg}/scripts/index.md",
                        index_md(
                            "script_index", "scripts", "脚本目录", exposed,
                            ["script key", "标题"],
                            lambda k: f"| {k} | {(script_contracts[k].title or k).replace('|', '，')} |\n",
                        ),
                    )
                for key, code in scripts:
                    operation = script_bindings.bindings.get(key)
                    # 执行契约：对外脚本的描述符与工作区 ontology/scripts/<脚本>.md 同一渲染器，
                    # 只把 entrypoint 换成包内 ASCII 文件名（modeling 按 scripts/<entrypoint> 取源码
                    # 且只收 ASCII 文件名），bound_operation 换成包内 operation key（求解 operation 为
                    # solver_key）。求解评估器只带源码，求解契约按包内路径引用它（见 _package_scripts），
                    # 文件名须与之一致。「关联operation」为「无」或不是现有操作的脚本只是被复用的
                    # 库模块，不对外调用、没有描述符，保留原文件名，对外脚本按原名 import 它，回导时也还原得了。
                    if operation or solver_contract.is_evaluator(code.encode("utf-8")):
                        filename = _package_script_filename(key)
                    else:
                        filename = f"{key}.py"
                    zf.writestr(f"{pkg}/scripts/{filename}", code)
                    if operation:
                        zf.writestr(
                            f"{pkg}/scripts/{key}.md",
                            _script_descriptor.render_descriptor(
                                key, script_contracts[key], package_operation_key(operation), filename,
                                operation_parents.get(operation, []),
                                enums=object_enums,
                            ),
                        )
        return dest_path

    @staticmethod
    def _read_transfer_meta(zf) -> dict:
        if META_FILE not in zf.namelist():
            raise WorkspaceError("不是有效的项目迁移包（缺少工作区元数据）")
        try:
            meta = json.loads(zf.read(META_FILE).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise WorkspaceError("迁移包元数据无法解析") from exc
        if not isinstance(meta, dict):
            raise WorkspaceError("迁移包元数据无法解析")
        return meta

    @staticmethod
    def _transfer_meta_name(meta: dict) -> str:
        return str(meta.get("name") or "").strip() or "导入项目"

    def transfer_archive_name(self, archive_path: Path) -> str:
        """迁移包里记录的任务名称（导入前查重用），包不合法时与正式导入报同样的错。"""
        import zipfile

        try:
            with zipfile.ZipFile(archive_path) as zf:
                return self._transfer_meta_name(self._read_transfer_meta(zf))
        except zipfile.BadZipFile as exc:
            raise WorkspaceError("不是有效的项目迁移包（压缩包无法解析）") from exc

    def import_transfer_zip(self, archive_path: Path, owner: str = "", name: str = "") -> dict:
        """导入迁移包（workspace 导出 zip）：恢复完整工作区含 git 版本历史。

        与 import_zip 的区别：保留 .git（但剥离 hooks，防止恶意钩子），
        沿用原 meta 的统计，仅重写 id/name/owner/created_at。name 为空时沿用
        迁移包里的任务名；同名冲突后改名导入时传入新名称。
        """
        import zipfile

        with zipfile.ZipFile(archive_path) as zf:
            old_meta = self._read_transfer_meta(zf)
            name = (name or "").strip() or self._transfer_meta_name(old_meta)
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
            "name": name,
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
        solver = self.solver_release_status(project_id)
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
            # 求解契约体检单列：blocking 仍只计数据绑定，普通包这里只有 enabled=False
            "solver": solver,
            # 结构自检单列，同样不计入数据绑定的 blocking，且不能降级发布
            "structure": self.package_structure_check(project_id, solver),
        }

    # ---- 发布前结构自检：consumer 导入时会拒绝的包不能发布 ----

    STRUCTURE_CHECK_VERSION = "0.1.0"  # 只影响 manifest 版本号；正式版本号由 Hub 发布时分配

    def package_structure_findings(
        self,
        project_id: str,
        *,
        binding: dict,
        allow_unbound: bool,
        graph: bool = False,
        ontology_id: str | None = None,
    ) -> list[dict]:
        """按 consumer 的导入规则检查本项目的导出包；除占位版本号外，参数须与正式导出一致。"""
        with tempfile.TemporaryDirectory(prefix="onto-structure-check-") as directory:
            archive = Path(directory) / "package.zip"
            self.export_package_zip(
                project_id, archive, binding=binding, allow_unbound=allow_unbound, graph=graph,
                version=self.STRUCTURE_CHECK_VERSION, ontology_id=ontology_id,
            )
            return consumer_package_rules.check_package_zip(archive)

    def package_structure_check(self, project_id: str, solver: dict | None = None) -> dict:
        """发布检查展示用：按降级口径导出（数据绑定问题另有一栏），导出失败也作为结构问题列出。"""
        solver = solver if solver is not None else self.solver_release_status(project_id)
        if solver.get("findings"):
            # 求解契约问题会让导出直接失败，那一栏已经列出，不重复报
            return {"blocking": 0, "findings": [], "truncated": False, "skipped": "solver_findings"}
        from . import hub_packages  # hub_packages 依赖本模块

        root = self.project_root(project_id)
        try:
            findings = self.package_structure_findings(
                project_id,
                binding=package_mapping.resolve_object_tables(root, strict_declared=False),
                allow_unbound=True,
                ontology_id=hub_packages.ontology_id(project_id),
            )
        except WorkspaceError as exc:
            code = exc.code if exc.code != "workspace_error" else "package_export_failed"
            findings = [{"code": code, "message": str(exc)}]
        limit = self.PACKAGE_CHECK_LIMIT
        return {
            "blocking": len(findings),
            "findings": findings[:limit],
            "truncated": len(findings) > limit,
            "skipped": None,
        }

    # ---- 求解契约静态检查与发布门禁 ----

    @staticmethod
    def _operation_stems(root: Path) -> list[str]:
        stems: list[str] = []
        for rel_dir in ("ontology/operation", "ontology/logic"):
            directory = root / rel_dir
            if directory.is_dir():
                stems.extend(
                    path.stem for path in sorted(directory.glob("*.md"))
                    if path.name != "index.md" and path.stem not in stems
                )
        return stems

    @staticmethod
    def _package_scripts(root: Path) -> dict[str, Path]:
        """包内脚本路径 → 工作区脚本：与 export_package_zip 的脚本文件名规则一致。"""
        directory = root / "ontology" / "scripts"
        if not directory.is_dir():
            return {}
        return {
            f"scripts/{_package_script_filename(path.stem)}": path
            for path in sorted(directory.glob("*.py"))
            if path.is_file() and not path.is_symlink()
        }

    def solver_check(self, project_id: str) -> solver_contract.SolverCheck:
        root = self.project_root(project_id)
        return solver_contract.check_workspace_solver(
            root, self._operation_stems(root), self._package_scripts(root)
        )

    def solver_release_status(self, project_id: str) -> dict:
        """求解契约的静态检查结论；普通包返回 enabled=False。

        端到端验证改在发布后的独立评测中进行（ADR solver-validation-unified-entry），不再拦截发布：
        requires_validation_after_publish 提示发布时须确认求解能力尚未验证。
        """
        check = self.solver_check(project_id)
        if not check.enabled:
            return {"enabled": False}
        limit = self.PACKAGE_CHECK_LIMIT
        return {
            "enabled": True,
            "revision": check.revision,
            "digests": check.digests,
            "operations": [
                {
                    "solver_key": item.solver_key,
                    "operation": item.operation,
                    "decision_model": item.decision_model,
                    "enabled": item.enabled,
                    "evaluator": check.evaluators.get(item.solver_key, ""),
                }
                for item in check.contracts
            ],
            "findings": check.findings[:limit],
            "blocking": len(check.findings),
            "requires_validation_after_publish": check.needs_validation,
            "truncated": len(check.findings) > limit,
        }

    def solver_contract_view(self, project_id: str) -> dict:
        """求解模型面板读取：当前修订、全部契约（binding 全量 + sidecar 原文）与检查结论。"""
        root = self.project_root(project_id)
        check = self.solver_check(project_id)
        # 可选评估器与自动查找同一规则（旧式或声明式），辅助模块、Operation 脚本不列出
        scripts = {
            package_path: path
            for package_path, path in self._package_scripts(root).items()
            if solver_contract.is_evaluator_file(path)
        }
        return {
            "revision": check.revision,
            "digests": check.digests,
            "enabled": check.enabled,
            "contract": {
                "operations": [
                    {
                        **solver_contract.contract_view(item),
                        "evaluator": check.evaluators.get(item.solver_key, ""),
                        "unit_options": solver_contract.evaluator_unit_options(
                            root / check.evaluators[item.solver_key]
                            if item.solver_key in check.evaluators else None
                        ),
                    }
                    for item in check.contracts
                ],
            },
            "findings": check.findings,
            "requires_validation_after_publish": check.needs_validation,
            "available_operations": self._operation_stems(root),
            "evaluators": sorted(str(path.relative_to(root)) for path in scripts.values()),
            # 模板参数 evaluator_path 填包内路径（scripts/…），面板按工作区文件名展示
            "evaluator_options": [
                {"path": package_path, "file": str(path.relative_to(root))}
                for package_path, path in sorted(scripts.items())
            ],
            "supported_profiles": list(solver_contract.SUPPORTED_PROFILES),
            "problem_families": solver_contract.problem_family_catalog(),
            "template": solver_contract.NEW_CONTRACT_TEMPLATE,
        }

    def check_solver_contract_draft(self, project_id: str, operations: list) -> dict:
        """求解模型面板的即时检查：未保存的草稿按保存与发布体检的同一套规则检查，不落盘。

        rejected 是保存会被拒的结构问题，findings 是保存后发布检查仍会拦截的问题，
        rendered 按提交顺序给出模板契约将生成的 sidecar 原文（供预览）；
        验证门禁跟修订走，草稿保存后修订必然改变，这里不给出。
        """
        root = self.project_root(project_id)
        rejected, findings, rendered = solver_contract.check_draft(
            operations, self._operation_stems(root), root, self._package_scripts(root)
        )
        return {"rejected": rejected, "findings": findings, "rendered": rendered}

    def save_solver_contract(
        self, project_id: str, operations: list, base_revision: str,
        author: str = "", summary: str = "",
    ) -> dict:
        """整体保存求解契约（受管目录全量替换），基线修订不一致返回 409。

        结构不合法抛 SolverContractRejected；语义问题（Profile、评估器等）不阻止保存，
        随返回的 findings 给出，发布时再由门禁拦截。
        """
        root = self.project_root(project_id)
        with self._project_lock(project_id):
            current = self.solver_check(project_id).revision
            if (base_revision or "") != current:
                raise WorkspaceError(
                    "求解契约已被他人或助手修改，请刷新对比后再保存", 409,
                    code="solver_contract_revision_conflict",
                )
            staged = solver_contract.stage_contracts(operations, self._operation_stems(root))
            base = root / solver_contract.SOLVER_DIR
            if base.is_dir():
                for directory in base.iterdir():
                    if directory.name not in staged:
                        if directory.is_dir() and not directory.is_symlink():
                            shutil.rmtree(directory)
                        else:
                            directory.unlink()
            for solver_key, files in staged.items():
                directory = base / solver_key
                if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
                    directory.unlink()
                directory.mkdir(parents=True, exist_ok=True)
                for path in directory.iterdir():
                    if path.name not in files:
                        shutil.rmtree(path) if path.is_dir() and not path.is_symlink() else path.unlink()
                for filename, data in files.items():
                    target = directory / filename
                    existing = target.read_bytes() if target.is_file() else None
                    if existing is not None and solver_contract.same_content(filename, existing, data):
                        # 导入时原样保存的 binding 不因一次无改动的保存被重排格式
                        continue
                    self._atomic_write(target, data)
            if base.is_dir() and not any(base.iterdir()):
                base.rmdir()
            rel = solver_contract.SOLVER_DIR
            commit = ""
            if _git(root, "status", "--porcelain", "--", rel).strip():
                _git(root, "add", "-A", "--", rel)
                who = f" by {author}" if author else ""
                note = f"：{' '.join(summary.split())[:80]}" if summary.strip() else ""
                _git(root, "commit", "-m", f"web: 保存求解契约{note}{who}", "--", rel)
                commit = _git(root, "rev-parse", "HEAD").strip()
        return {**self.solver_contract_view(project_id), "commit": commit}

    def _ensure_solver_publishable(self, project_id: str) -> None:
        """发布只受求解契约静态检查约束；端到端验证在发布后的独立评测中进行。"""
        blocking = self.solver_release_status(project_id).get("findings") or []
        if blocking:
            raise WorkspaceError(
                "求解契约未通过发布检查：" + "；".join(item["message"] for item in blocking[:3]),
                code=blocking[0]["code"],
            )

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
        """用户发布知识包。降级发布须可追溯：记录谁、何时发布，以及哪些对象因此未绑定数据表。

        求解知识包另须通过求解契约静态检查；allow_unbound 只放宽数据绑定，不放宽求解静态检查。
        """
        root = self.project_root(project_id)
        self._ensure_solver_publishable(project_id)
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
        with self._project_lock(project_id):
            return self._write_file_unlocked(project_id, rel_path, content, base_revision, author)

    def _write_file_unlocked(self, project_id, rel_path, content, base_revision, author):
        root = self.project_root(project_id)
        target = self._resolve_inside(root, rel_path)
        rel = target.relative_to(root.resolve())
        if not rel.parts or rel.parts[0] in self.WRITE_FORBIDDEN_TOP:
            raise WorkspaceError("该路径不允许编辑", 403)
        if rel.as_posix().startswith(f"{solver_contract.SOLVER_DIR}/"):
            # 求解契约要求多文件一致（binding 与五份 sidecar），单文件编辑无法保证
            raise WorkspaceError("求解契约是受管目录，请在求解模型面板中维护", 403)
        if _is_script_descriptor(rel):
            raise WorkspaceError("脚本描述符由同名 .py 自动生成，不能直接编辑；请修改对应的 .py 脚本", 403)
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
        # Web 端保存即成版本点：只提交本文件，与 agent 的阶段提交互不干扰；
        # 保存脚本时同名描述符随之刷新，并进同一个提交
        rel_posix = rel.as_posix()
        paths = [rel_posix]
        if _is_workspace_script(rel):
            _script_descriptor.sync_workspace_descriptors(root / "ontology")
            descriptor = rel.with_suffix(".md").as_posix()
            if (root / descriptor).exists() or _git(root, "ls-files", "--", descriptor).strip():
                paths.append(descriptor)
        commit = ""
        if _git(root, "status", "--porcelain", "--", *paths).strip():
            _git(root, "add", "-A", "--", *paths)
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

    @classmethod
    def _rollback_path_allowed(cls, rel_path: str) -> bool:
        path = Path(rel_path)
        if not path.parts or path.parts[0] in ROLLBACK_EXCLUDED_TOP:
            return False
        # Directory indexes are deterministic projections of the files beside
        # them. Roll back their source files and regenerate the indexes instead
        # of treating an old projection as an independent business change.
        return rel_path not in {f"{directory}/index.md" for directory, _title in cls.INDEX_DIRS}

    @classmethod
    def _rollback_manifest(cls, root: Path, revision: str) -> dict[str, str]:
        """Return the rollback-managed files and their git modes at revision."""
        manifest = {}
        for line in _git(root, "ls-tree", "-r", "-z", "--full-tree", revision).split("\0"):
            if not line:
                continue
            try:
                meta, rel_path = line.split("\t", 1)
                mode, kind, _object_id = meta.split(" ", 2)
            except ValueError:
                continue
            if kind == "blob" and cls._rollback_path_allowed(rel_path):
                manifest[rel_path] = f"{mode} {_object_id}"
        return manifest

    @classmethod
    def _managed_dirty_paths(cls, root: Path) -> list[str]:
        """List changed managed paths while ignoring metadata and runtime state."""
        raw = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
        records = raw.split("\0")
        paths = []
        index = 0
        while index < len(records):
            record = records[index]
            index += 1
            if not record:
                continue
            if len(record) < 4:
                continue
            status, rel_path = record[:2], record[3:]
            if cls._rollback_path_allowed(rel_path):
                paths.append(rel_path)
            if "R" in status or "C" in status:
                if index < len(records):
                    previous = records[index]
                    index += 1
                    if previous and cls._rollback_path_allowed(previous):
                        paths.append(previous)
        return sorted(set(paths))

    @classmethod
    def _generated_index_dirty_paths(cls, root: Path) -> list[str]:
        """Return changed deterministic indexes without adding them to rollback snapshots."""
        paths = []
        for directory, _title in cls.INDEX_DIRS:
            rel_path = f"{directory}/index.md"
            if _git(root, "status", "--porcelain", "--", rel_path).strip():
                paths.append(rel_path)
        return paths

    @classmethod
    def _rollback_changes(cls, root: Path, commit: str) -> dict:
        current = cls._rollback_manifest(root, "HEAD")
        target = cls._rollback_manifest(root, commit)
        current_paths, target_paths = set(current), set(target)
        added = sorted(target_paths - current_paths)
        deleted = sorted(current_paths - target_paths)
        modified = sorted(
            path
            for path in current_paths & target_paths
            if current[path] != target[path]
        )
        return {
            "added": added,
            "modified": modified,
            "deleted": deleted,
            "total": len(added) + len(modified) + len(deleted),
        }

    @staticmethod
    def _minimal_pathspecs(paths: list[str]) -> list[str]:
        """Collapse descendants when an affected parent path already covers them."""
        result = []
        for path in sorted(set(paths), key=lambda item: (item.count("/"), item)):
            if not any(path.startswith(f"{parent}/") for parent in result):
                result.append(path)
        return result

    @staticmethod
    def _validate_revert_target(root: Path, commit: str) -> str:
        if not re.fullmatch(r"[0-9a-fA-F]{6,40}", commit):
            raise WorkspaceError("无效的版本号")
        try:
            resolved = _git(root, "rev-parse", "--verify", f"{commit}^{{commit}}").strip()
        except WorkspaceError as exc:
            raise WorkspaceError("版本不存在或已不可用", 404) from exc
        head = _git(root, "rev-parse", "HEAD").strip()
        if resolved == head:
            raise WorkspaceError("当前已是该版本，无需回退")
        try:
            ancestry_count = int(
                _git(
                    root,
                    "rev-list",
                    "--ancestry-path",
                    "--count",
                    f"{resolved}..{head}",
                ).strip()
                or "0"
            )
        except WorkspaceError as exc:
            raise WorkspaceError("无法校验目标版本", 500) from exc
        if ancestry_count == 0:
            raise WorkspaceError("只能回退到当前版本历史中的提交", 409)
        return resolved

    def preview_revert(self, project_id: str, commit: str) -> dict:
        with self._project_lock(project_id):
            root = self.project_root(project_id)
            resolved = self._validate_revert_target(root, commit)
            changes = self._rollback_changes(root, resolved)
            if not changes["total"]:
                raise WorkspaceError("当前业务内容与该版本一致，无需回退")
            return {
                "reverted_to": resolved,
                "expected_head": _git(root, "rev-parse", "HEAD").strip(),
                "changes": changes,
            }

    @staticmethod
    def _invalidate_revert_state(root: Path) -> list[str]:
        """Remove conclusions derived from content that has just been replaced."""
        state = root / ".ontology-state"
        failed = []
        for name in (
            "eval.json",
            "progress.json",
            "research-review.json",
            "research-inventory.json",
            "research-baseline.json",
            "script-verifications.json",
            "solver-validations.json",
        ):
            try:
                (state / name).unlink(missing_ok=True)
            except OSError:
                failed.append(name)
        for name in ("artifact-reviews", "studio-review"):
            path = state / name
            try:
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
            except OSError:
                failed.append(name)
        return failed

    def revert_to(
        self,
        project_id: str,
        commit: str,
        expected_head: str = "",
        busy_check=None,
    ) -> dict:
        """Restore managed content as a complete snapshot and create a new version."""
        with self._project_lock(project_id):
            root = self.project_root(project_id)
            if busy_check is not None:
                busy_check(root)
            head = _git(root, "rev-parse", "HEAD").strip()
            if expected_head and expected_head != head:
                raise WorkspaceError("项目已产生新版本，请重新预览后再回退", 409)
            resolved = self._validate_revert_target(root, commit)
            if self._managed_dirty_paths(root):
                raise WorkspaceError("项目存在未提交或暂存的修改，请先完成当前编辑", 409)
            try:
                staged_paths = _git(root, "diff", "--cached", "--name-only", "--")
            except WorkspaceError as exc:
                raise WorkspaceError("无法检查项目暂存状态", 500) from exc
            if staged_paths.strip():
                # Managed staged files were handled above. A staged excluded file
                # must not be swept into the rollback commit either.
                raise WorkspaceError("项目存在已暂存的元数据修改，请先完成当前编辑", 409)
            changes = self._rollback_changes(root, resolved)
            if not changes["total"]:
                raise WorkspaceError("当前业务内容与该版本一致，无需回退")
            target_version = int(
                _git(root, "rev-list", "--count", resolved).strip() or "0"
            )

            affected = changes["added"] + changes["modified"] + changes["deleted"]
            try:
                # Restore files present in the target and remove managed files that
                # were introduced later. Excluded metadata/runtime paths are untouched.
                root_resolved = root.resolve()
                # Remove current-only paths before restoring the target snapshot.
                # This order is required when a path changed between a file and a
                # directory: checkout can otherwise create the target directory and
                # the later delete pass would remove it again (or hit a pathspec error).
                for rel_path in changes["deleted"]:
                    target = root / rel_path
                    # A later symlink may now point outside the project. Delete the
                    # link itself; never resolve and follow it during snapshot restore.
                    if not target.absolute().is_relative_to(root_resolved):
                        raise WorkspaceError("路径越界", 403)
                    if target.is_dir() and not target.is_symlink():
                        shutil.rmtree(target)
                    else:
                        target.unlink(missing_ok=True)
                restore = changes["added"] + changes["modified"]
                if restore:
                    _git(root, "checkout", resolved, "--", *restore)
                # A historical snapshot may use legacy layout. Normalize it before
                # committing so the first file-tree refresh cannot create a second,
                # surprising migration version.
                self._migrate_legacy_logic(root)
                _script_descriptor.sync_workspace_descriptors(root / "ontology")
                self._refresh_indexes(root)
                affected = sorted(
                    set(self._managed_dirty_paths(root))
                    | set(self._generated_index_dirty_paths(root))
                )
                if not affected:
                    raise WorkspaceError("当前业务内容与该版本一致，无需回退")
                pathspecs = self._minimal_pathspecs(affected)
                _git(root, "add", "-A", "--", *pathspecs)
                _git(
                    root,
                    "commit",
                    "-m",
                    f"revert: 回退到版本 {target_version}（{resolved[:8]}）",
                )
            except Exception:
                # Restore only managed files. Project metadata is deliberately
                # allowed to be dirty and must survive a failed rollback attempt.
                dirty = sorted(
                    set(self._managed_dirty_paths(root))
                    | set(self._generated_index_dirty_paths(root))
                )
                if dirty:
                    pathspecs = self._minimal_pathspecs(dirty)
                    try:
                        _git(root, "reset", "HEAD", "--", *pathspecs)
                    except WorkspaceError:
                        pass
                    tracked = [
                        path
                        for path in dirty
                        if _git(root, "ls-files", "--", path).strip()
                    ]
                    untracked = sorted(
                        set(dirty) - set(tracked),
                        key=lambda item: (item.count("/"), item),
                        reverse=True,
                    )
                    for rel_path in untracked:
                        target = root / rel_path
                        if not target.absolute().is_relative_to(root.resolve()):
                            continue
                        try:
                            if target.is_dir() and not target.is_symlink():
                                shutil.rmtree(target)
                            else:
                                target.unlink(missing_ok=True)
                        except (FileNotFoundError, NotADirectoryError):
                            pass
                    if tracked:
                        try:
                            _git(
                                root,
                                "checkout",
                                "HEAD",
                                "--",
                                *self._minimal_pathspecs(tracked),
                            )
                        except WorkspaceError:
                            pass
                raise
            invalidation_failed = self._invalidate_revert_state(root)
            result = {
                "reverted_to": resolved,
                "commit": _git(root, "rev-parse", "HEAD").strip(),
                "changes": changes,
            }
            if invalidation_failed:
                result["warning"] = "部分派生状态未能清理，请重新运行相关检查"
            return result

    def delete_resource(
        self, project_id: str, rel_path: str, author: str = ""
    ) -> dict:
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
            meta_path = root / META_FILE
            meta_snapshot = self._snapshot(meta_path)
            rel_posix = rel.as_posix()
            directives_rel = directives_path.relative_to(root).as_posix()
            managed_paths = [rel_posix, directives_rel]
            managed_specs = _literal_pathspecs(managed_paths)
            old_head = _git(root, "rev-parse", "HEAD").strip()
            commit = ""
            staged = _git(
                root, "diff", "--cached", "--name-only", "--", *managed_specs
            ).splitlines()
            if staged:
                raise WorkspaceError(
                    "该资料或资料设置已被其他编辑暂存，请完成或放弃该编辑后重试",
                    409,
                    "resource_delete_conflict",
                )
            try:
                target.unlink()
                working_directives = self.resource_directives(project_id)
                working_changed = working_directives.pop(rel_posix, None) is not None
                committed_directives = self._resource_directives_at_head(
                    root, directives_rel
                )
                committed_before = dict(committed_directives)
                committed_directives.pop(rel_posix, None)
                directives_changed = committed_directives != committed_before
                if directives_changed:
                    self._write_resource_directives(root, committed_directives)
                self._update_meta_unlocked(root, resource_updated_at=int(time.time()))
                commit_paths = []
                if _git(
                    root, "ls-files", "--", _literal_pathspec(rel_posix)
                ).strip():
                    commit_paths.append(rel_posix)
                if directives_changed:
                    commit_paths.append(directives_rel)
                commit_specs = _literal_pathspecs(commit_paths)
                if commit_paths and _git(
                    root, "status", "--porcelain", "--", *commit_specs
                ).strip():
                    who = f" by {author}" if author else ""
                    _git(
                        root,
                        "commit",
                        "--only",
                        "-m",
                        f"web: 删除资料 {rel_posix}{who}",
                        "--",
                        *commit_specs,
                    )
                    commit = _git(root, "rev-parse", "HEAD").strip()
                if directives_changed or working_changed:
                    self._write_resource_directives(root, working_directives)
            except Exception:
                if commit and _git(root, "rev-parse", "HEAD").strip() == commit:
                    _git(root, "reset", "--soft", old_head)
                    _git(root, "reset", "--", *managed_specs)
                self._restore_snapshot(target, file_snapshot)
                self._restore_snapshot(directives_path, directives_snapshot)
                self._restore_snapshot(meta_path, meta_snapshot)
                raise
            return {"deleted": rel_path, "commit": commit}

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

    def versions(self, project_id: str, limit: int = 50, offset: int = 0) -> list[dict]:
        root = self.project_root(project_id)
        total = int(_git(root, "rev-list", "--count", "HEAD").strip())
        out = _git(
            root, "log", f"--skip={offset}", f"--max-count={limit}", "--pretty=format:%H%x1f%ct%x1f%s"
        )
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
        # 「迭代 N」：N = 第几次提交，最早的提交是 1；不随分页和新保存变化
        for index, entry in enumerate(entries):
            entry["version"] = total - offset - index
            entry["current"] = offset + index == 0
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


def _is_workspace_script(rel: Path) -> bool:
    return len(rel.parts) == 3 and rel.parts[:2] == ("ontology", "scripts") and rel.suffix == ".py"


def _is_script_descriptor(rel: Path) -> bool:
    """ontology/scripts/<脚本>.md：由 .py 派生的 consumer 调用契约（script_descriptor.py）。"""
    return (
        len(rel.parts) == 3
        and rel.parts[:2] == ("ontology", "scripts")
        and rel.suffix.lower() == ".md"
        and rel.name.lower() != "index.md"
    )


def _package_script_filename(key: str) -> str:
    """知识包内的脚本文件名，满足 modeling 的 entrypoint 契约 ^[A-Za-z0-9_.-]+\\.py$。

    ASCII 脚本名原样沿用；中文等其他脚本名按 key 的哈希取稳定文件名，
    脚本 key 与标题仍保留原名。
    """
    if _PACKAGE_SCRIPT_STEM_RE.fullmatch(key) and ".." not in key:
        return f"{key}.py"
    return f"script_{hashlib.sha1(key.encode('utf-8')).hexdigest()[:10]}.py"


class _ReproducibleZip:
    """知识包 ZIP 的确定性写出（REQ-BLD-010）：先收集成员，正常退出时按路径排序写入。

    成员统一 UTF-8 编码，时间戳固定为 ZIP 纪元、权限固定为 0644，同一内容重复导出逐字节相同；
    同一路径写两次说明导出规则冲突，直接报错而不是让后写的静默覆盖。
    """

    EPOCH = (1980, 1, 1, 0, 0, 0)

    def __init__(self, dest_path: Path):
        self.dest_path = dest_path
        self.entries: dict[str, bytes] = {}

    def __enter__(self) -> "_ReproducibleZip":
        return self

    def writestr(self, name: str, data: str | bytes) -> None:
        if name in self.entries:
            raise WorkspaceError(f"知识包内路径重复：{name}")
        self.entries[name] = data.encode("utf-8") if isinstance(data, str) else bytes(data)

    def __exit__(self, exc_type, exc, traceback) -> None:
        if exc_type is not None:
            return
        import zipfile

        with zipfile.ZipFile(self.dest_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in sorted(self.entries):
                info = zipfile.ZipInfo(name, date_time=self.EPOCH)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = 0o644 << 16
                zf.writestr(info, self.entries[name])


def _load_research_review():
    """资料分析验收状态以 workspace-toolkit/scripts/research_review.py 为单一事实源。

    项目进度（本文件）、CLI 阶段前置（state_helper）与报告检查器（research_checker）
    共用同一份失效判定，避免"目录里有报告就算完成"在多处各自实现（issue #362）。
    """
    path = Path(config.ASSETS_DIR) / "workspace-toolkit" / "scripts" / "research_review.py"
    spec = importlib.util.spec_from_file_location("onto_workspace_research_review", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# 脚本契约与描述符以工作区工具箱脚本为单一事实源（toolkit.py），规则检查与导出共用
ScriptContractError = _script_contract.ScriptContractError
_script_header_meta = _script_contract.parse_script_contract

_research_review = _load_research_review()


def _first_heading(path: Path) -> str:
    """index 清单的标题列：frontmatter 的 title（脚本描述符），否则首个一级标题；读不到则为空。"""
    try:
        with open(path, encoding="utf-8") as fh:
            in_frontmatter = False
            for index in range(40):
                line = fh.readline()
                if not line:
                    break
                if index == 0 and line.strip() == "---":
                    in_frontmatter = True
                    continue
                if in_frontmatter:
                    if line.strip() == "---":
                        in_frontmatter = False
                    elif line.startswith("title:"):
                        title = line[len("title:"):].strip()
                        try:
                            title = json.loads(title)
                        except ValueError:
                            pass
                        return str(title)[:80]
                    continue
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
        detail = result.stderr.strip() or result.stdout.strip() or f"退出码 {result.returncode}"
        raise WorkspaceError(f"git {args[0]} 失败: {detail}", 500)
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
