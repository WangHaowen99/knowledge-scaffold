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

    def create_project(self, name: str) -> dict:
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

        meta = {"id": project_id, "name": name, "created_at": int(time.time())}
        (root / META_FILE).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        _git(root, "init")
        _git(root, "config", "user.name", "Onto Web")
        _git(root, "config", "user.email", "onto-web@example.invalid")
        _git(root, "add", "-A")
        _git(root, "commit", "-m", "init: 任务工作区基线", "--allow-empty")
        return meta

    def list_projects(self) -> list[dict]:
        projects = []
        for entry in sorted(self.workspaces_dir.iterdir()):
            meta_path = entry / META_FILE
            if meta_path.is_file():
                try:
                    projects.append(json.loads(meta_path.read_text(encoding="utf-8")))
                except json.JSONDecodeError:
                    continue
        projects.sort(key=lambda p: p.get("created_at", 0), reverse=True)
        return projects

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
