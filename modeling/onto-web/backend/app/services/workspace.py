"""项目工作区管理：目录 + git + 提示词资产挂载。

对应 CLI 版 init_task.sh 的职责：每个建模任务一个独立目录和独立 git，
.claude / CLAUDE.md / README.md 软链到共享资产目录，升级资产对所有任务生效。
"""

from __future__ import annotations

import hashlib
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
        # 初始化知识包目录，并写入占位 index，便于前端直接浏览完整结构。
        knowledge_dirs = (
            "research", "reports", "ontology/object", "ontology/logic",
            "ontology/data-mock", "ontology/testcase", "ontology/scripts",
            "ontology/diagrams", "ontology/diagrams-summary", "ontology/mermaid",
            "ontology/excel",
        )
        for rel in knowledge_dirs:
            directory = root / rel
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "index.md").write_text(
                f"# {rel.rsplit('/', 1)[-1]}\n\n待生成知识包内容。\n", encoding="utf-8"
            )

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
            # 乐观并发标记：保存时回传，内容被他人（含 agent）改过则拒绝覆盖
            "revision": _content_revision(target.read_bytes()),
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
        replaced = dest.exists()
        dest.write_bytes(data)
        # 变更提醒依据：前端对比该时间与派生产物版本，提示"资料已更新，可让助手增量刷新"
        self._update_meta(project_id, resource_updated_at=int(time.time()))
        return {
            "path": f"resource/{subdir}/{safe_name}",
            "size": len(data),
            "replaced": replaced,
        }

    # ---- 生命周期扩展 ----

    def _update_meta(self, project_id: str, **fields) -> dict:
        root = self.project_root(project_id)
        meta = json.loads((root / META_FILE).read_text(encoding="utf-8"))
        meta.update(fields)
        (root / META_FILE).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
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

    def export_package_zip(self, project_id: str, dest_path: Path) -> Path:
        """发布为知识包（consumer 可导入的 ontology-package v0.1）。

        对齐 consumer/backend/ontology/package_import.py 的校验契约：
        manifest.yaml 六必填字段、objects/operations 目录 + frontmatter、
        operation 必须声明 uses.objects、index 首列与详情 key 逐字相等。
        """
        import zipfile

        root = self.project_root(project_id)
        meta = self.get_project(project_id)
        version = f"0.1.{len(self.versions(project_id))}"
        pkg = meta["id"]

        def entry_items(rel_dir: str) -> list[tuple[str, str]]:
            directory = root / rel_dir
            items = []
            if directory.is_dir():
                for path in sorted(directory.glob("*.md")):
                    if path.name == "index.md":  # 工作区占位 index 不进包
                        continue
                    items.append((path.stem, _strip_frontmatter(path.read_text(encoding="utf-8"))))
            return items

        objects = entry_items("ontology/object")
        operations = entry_items("ontology/logic")
        object_keys = {key for key, _ in objects}
        q = lambda s: json.dumps(s, ensure_ascii=False)  # JSON 字符串是合法 YAML 标量

        manifest = (
            f"kind: ontology-package\n"
            f"schema_version: '0.1'\n"
            f"id: {pkg}\n"
            f"name: {q(meta['name'])}\n"
            f"description: {q('由 Onto Web 建模项目发布：' + meta['name'])}\n"
            f"version: {version}\n"
            f"language: zh-CN\n"
        )

        def front(resource: str, key: str, title: str, extra: str = "") -> str:
            return f"---\nresource: {resource}\nkey: {q(key)}\ntitle: {q(title)}\n{extra}---\n\n"

        def index_md(resource: str, key: str, title: str, items: list[tuple[str, str]], col: str) -> str:
            rows = "".join(f"| {k} | {k} |\n" for k, _ in items)
            return front(resource, key, title) + f"| {col} | title |\n| --- | --- |\n{rows}"

        with zipfile.ZipFile(dest_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(f"{pkg}/manifest.yaml", manifest)
            zf.writestr(f"{pkg}/index.md", front("root", "root", meta["name"]) + f"# {meta['name']}\n")
            zf.writestr(
                f"{pkg}/objects/index.md",
                index_md("object_index", "objects", "对象目录", objects, "object key"),
            )
            for key, body in objects:
                zf.writestr(f"{pkg}/objects/{key}.md", front("object", key, key) + body)
            zf.writestr(
                f"{pkg}/operations/index.md",
                index_md("operation_index", "operations", "操作目录", operations, "operation key"),
            )
            for key, body in operations:
                used = sorted({m for m in _WIKILINK_RE.findall(body) if m in object_keys})
                extra = f"uses:\n  objects: {json.dumps(used, ensure_ascii=False)}\n"
                zf.writestr(f"{pkg}/operations/{key}.md", front("operation", key, key, extra) + body)
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

    def import_summary(self, archive_path: Path) -> dict:
        """导入向导第二步：不落盘，返回压缩包内容摘要供用户确认。"""
        import zipfile

        top_dirs: dict[str, int] = {}
        skipped: list[str] = []
        objects = logics = files = 0
        total_size = 0
        try:
            with zipfile.ZipFile(archive_path) as zf:
                for info in zf.infolist():
                    parts = Path(info.filename).parts
                    if not parts or info.is_dir():
                        continue
                    files += 1
                    total_size += info.file_size
                    top = parts[0]
                    if top in self.IMPORT_SKIP_TOP or info.filename.startswith("/") or ".." in parts:
                        if top not in skipped:
                            skipped.append(top)
                        continue
                    top_dirs[top] = top_dirs.get(top, 0) + 1
                    posix = "/".join(parts)
                    if posix.startswith("ontology/object/") and posix.endswith(".md"):
                        objects += 1
                    elif posix.startswith("ontology/logic/") and posix.endswith(".md"):
                        logics += 1
        except zipfile.BadZipFile as exc:
            raise WorkspaceError(f"压缩包无法解析：{exc}") from exc
        return {
            "files": files,
            "total_size": total_size,
            "top_dirs": [{"name": k, "files": v} for k, v in sorted(top_dirs.items())],
            "skipped": sorted(skipped),
            "objects": objects,
            "logics": logics,
        }

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
        if not target.is_file():
            raise WorkspaceError("文件不存在", 404)
        target.unlink()
        self._update_meta(project_id, resource_updated_at=int(time.time()))
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

    # ---- 回收站 ----

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
        (dest / META_FILE).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
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


def _content_revision(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


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
