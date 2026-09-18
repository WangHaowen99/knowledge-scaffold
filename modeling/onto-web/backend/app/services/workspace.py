"""项目工作区管理：目录 + git + 提示词资产挂载。

对应 CLI 版 init_task.sh 的职责：每个建模任务一个独立目录和独立 git，
.claude / CLAUDE.md / README.md 软链到共享资产目录，升级资产对所有任务生效。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

META_FILE = ".onto-web.json"
SKIP_DIRS = {".git", ".ontology-state", ".obsidian"}
RESOURCE_SUBDIRS = {"diagrams", "excel", "others"}

DEFAULT_GITIGNORE = """.ontology-state/
.claude
CLAUDE.md
README.md
"""


class WorkspaceError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class WorkspaceManager:
    def __init__(self, data_dir: Path, assets_dir: Path):
        self.workspaces_dir = Path(data_dir) / "workspaces"
        self.assets_dir = Path(assets_dir)
        self.workspaces_dir.mkdir(parents=True, exist_ok=True)

    # ---- 项目 CRUD ----

    def create_project(self, name: str, owner: str = "") -> dict:
        name = (name or "").strip()
        if not name:
            raise WorkspaceError("项目名称不能为空")
        project_id = f"{_slugify(name)}-{uuid.uuid4().hex[:8]}"
        root = self.workspaces_dir / project_id
        root.mkdir(parents=True)

        self._mount_assets(root)
        self._copy_resource_template(root)
        self._write_gitignore(root)
        for sub in ("research", "reports"):
            (root / sub).mkdir(exist_ok=True)

        meta = {
            "id": project_id,
            "name": name,
            "created_at": int(time.time()),
            "owner": owner or "",
        }
        (root / META_FILE).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        _git(root, "init")
        _git(root, "config", "user.name", "Onto Web")
        _git(root, "config", "user.email", "onto-web@example.invalid")
        _git(root, "add", "-A")
        _git(root, "commit", "-m", "init: 任务工作区基线", "--allow-empty")
        return meta

    # 与前端阶段定义保持一致的目录探针（顺序推进，遇到第一个未完成即停）
    STAGE_DIRS = (
        "research",
        "ontology/diagrams",
        "ontology/object",
        "ontology/logic",
        "reports",
        "ontology/data-mock",
        "ontology/testcase",
        "ontology/scripts",
    )
    STAGE_TOTAL = len(STAGE_DIRS)

    @classmethod
    def _project_progress(cls, root: Path) -> dict:
        done = 0
        for rel in cls.STAGE_DIRS:
            directory = root / rel
            if directory.is_dir() and any(directory.iterdir()):
                done += 1
            else:
                break

        def count_md(rel: str) -> int:
            directory = root / rel
            if not directory.is_dir():
                return 0
            return sum(1 for f in directory.glob("*.md"))

        return {
            "stages_done": done,
            "stages_total": cls.STAGE_TOTAL,
            "objects": count_md("ontology/object"),
            "logics": count_md("ontology/logic"),
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
    def visible_to(projects: list[dict], username: str, is_admin: bool) -> list[dict]:
        """项目可见性：管理员看全部；普通用户看自己的 + 无主的历史项目。"""
        if is_admin:
            return projects
        return [p for p in projects if p.get("owner", "") in ("", username)]

    @staticmethod
    def can_access(meta: dict, username: str, is_admin: bool) -> bool:
        if is_admin:
            return True
        owner = meta.get("owner", "")
        return owner == "" or owner == username

    def get_project(self, project_id: str) -> dict:
        root = self.project_root(project_id)
        return json.loads((root / META_FILE).read_text(encoding="utf-8"))

    def project_root(self, project_id: str) -> Path:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", project_id):
            raise WorkspaceError("无效的项目 ID", 404)
        root = self.workspaces_dir / project_id
        if not (root / META_FILE).is_file():
            raise WorkspaceError("项目不存在", 404)
        return root

    # ---- 文件 ----

    def file_tree(self, project_id: str) -> list[dict]:
        root = self.project_root(project_id)

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
        }

    def save_resource(self, project_id: str, subdir: str, filename: str, data: bytes) -> dict:
        if subdir not in RESOURCE_SUBDIRS:
            raise WorkspaceError(f"资料分类必须是 {sorted(RESOURCE_SUBDIRS)} 之一")
        safe_name = Path(filename).name
        if not safe_name or safe_name.startswith(".") or safe_name != filename:
            raise WorkspaceError("无效的文件名")
        root = self.project_root(project_id)
        dest_dir = root / "resource" / subdir
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / safe_name
        dest.write_bytes(data)
        return {"path": f"resource/{subdir}/{safe_name}", "size": len(data)}

    # ---- 生命周期扩展 ----

    def rename_project(self, project_id: str, name: str) -> dict:
        name = (name or "").strip()
        if not name:
            raise WorkspaceError("项目名称不能为空")
        root = self.project_root(project_id)
        meta = json.loads((root / META_FILE).read_text(encoding="utf-8"))
        meta["name"] = name
        (root / META_FILE).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return meta

    def delete_project(self, project_id: str) -> dict:
        """软删除：移入回收目录，保留 git 历史，可人工找回。"""
        root = self.project_root(project_id)
        trash = self.workspaces_dir.parent / "trash"
        trash.mkdir(parents=True, exist_ok=True)
        dest = trash / f"{project_id}-{int(time.time())}"
        shutil.move(str(root), str(dest))
        return {"trashed_to": str(dest)}

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

    # 导入时跳过的顶层条目：资产由本服务重新挂载；.git 不合并（v1 不保留导入方历史）
    IMPORT_SKIP_TOP = {".claude", "CLAUDE.md", "README.md", ".git", META_FILE}

    def import_zip(self, name: str, archive_path: Path, owner: str = "") -> dict:
        import zipfile

        meta = self.create_project(name, owner=owner)
        root = self.project_root(meta["id"])
        try:
            with zipfile.ZipFile(archive_path) as zf:
                for info in zf.infolist():
                    parts = Path(info.filename).parts
                    if (
                        not parts
                        or info.filename.startswith("/")
                        or ".." in parts
                        or parts[0] in self.IMPORT_SKIP_TOP
                        or info.is_dir()
                    ):
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

    # ---- 文件写入与历史版本 ----

    WRITE_FORBIDDEN_TOP = {".git", ".claude", ".ontology-state", META_FILE}

    def write_file(self, project_id: str, rel_path: str, content: str) -> dict:
        root = self.project_root(project_id)
        target = self._resolve_inside(root, rel_path)
        rel = target.relative_to(root.resolve())
        if not rel.parts or rel.parts[0] in self.WRITE_FORBIDDEN_TOP:
            raise WorkspaceError("该路径不允许编辑", 403)
        if target.is_symlink():
            raise WorkspaceError("该路径不允许编辑", 403)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {"path": rel_path, "size": len(content.encode("utf-8"))}

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
        if not target.is_file():
            raise WorkspaceError("文件不存在", 404)
        target.unlink()
        return {"deleted": rel_path}

    # ---- 版本 ----

    def versions(self, project_id: str, limit: int = 50) -> list[dict]:
        root = self.project_root(project_id)
        out = _git(root, "log", f"--max-count={limit}", "--pretty=format:%H%x1f%ct%x1f%s")
        entries = []
        for line in out.splitlines():
            parts = line.split("\x1f")
            if len(parts) == 3:
                entries.append(
                    {"commit": parts[0], "time": int(parts[1]), "message": parts[2]}
                )
        # "已保存版本 N"：N = 距基线的序号，最早的提交是版本 1
        total = len(entries)
        for index, entry in enumerate(entries):
            entry["version"] = total - index
        return entries

    # ---- 内部 ----

    def _mount_assets(self, root: Path) -> None:
        links = {
            ".claude": self.assets_dir / "claude-workspace-dotclaude",
            "CLAUDE.md": self.assets_dir / "claude-workspace-CLAUDE.md",
            "README.md": self.assets_dir / "claude-workspace-README.md",
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
            / "claude-workspace-dotclaude"
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


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:24] or "task"


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        raise WorkspaceError(f"git {args[0]} 失败: {result.stderr.strip()}", 500)
    return result.stdout
