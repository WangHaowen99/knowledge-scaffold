"""工作区导入导出：项目迁移包（含版本历史）、工作区压缩包、已发布知识包的反向导入、示例注入。"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from ..script_env import minimal_env
from .errors import WorkspaceError
from .gitops import _ensure_gitignore_entries, _git, _untrack_state_in_index
from .layout import EXPORT_ONTOLOGY_DIRS, IMPORT_SKIP_TOP, META_FILE, _default_engine, _is_run_state, _slugify


class TransferMixin:
    EXPORT_ONTOLOGY_DIRS = EXPORT_ONTOLOGY_DIRS
    IMPORT_SKIP_TOP = IMPORT_SKIP_TOP

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
                    if top == ".ontology-state" or _is_run_state(rel):
                        continue
                zf.write(path, str(rel))
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
                # members 是单人所有权之前的成员表，旧迁移包里可能还带着
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
        # 存量迁移包的索引仍跟踪着元数据：gitignore 管不到已跟踪的文件，导入提交里一并摘除
        _untrack_state_in_index(root)
        _git(root, "add", "-A")
        _git(root, "commit", "-m", "import: 迁移导入", "--allow-empty")
        return meta


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
        _ensure_gitignore_entries(root)  # 包里的 .gitignore 会盖掉模板
        _git(root, "add", "-A")
        _git(root, "commit", "-m", "import: 导入既有工作区内容", "--allow-empty")
        return meta

    def import_summary(self, archive_path: Path) -> dict:
        """导入向导第二步：不落盘，返回压缩包内容摘要供用户确认。

        判别与计数在 package_archive，正式导入前也走同一个函数——预览和导入分别
        实现时，必然出现"预览数出 0、导入照样建个空任务"（Issue #359）。
        """
        from .. import package_archive

        return package_archive.inspect_archive(archive_path)

    def import_package_zip(self, name: str, archive_path: Path, owner: str = "") -> dict:
        """导入已发布知识包（ontology-package v0.1）：按工作区布局物化，原包留底。

        与 import_zip 的区别只在**布局**：import_zip 原样铺文件，于是发布包的
        `<包根>/objects`（复数）躺在工作区里，而本服务读的是 ontology/object
        （单数）——概览计数与阶段探针全不命中，任务建出来是空的（Issue #359）。
        """
        from .. import package_archive

        summary = package_archive.inspect_archive(archive_path)
        if summary["kind"] != package_archive.KIND_KNOWLEDGE_PACKAGE:
            raise WorkspaceError("这不是已发布知识包（缺少知识包清单或 objects 目录）")
        # 门禁放在建项目之前：解析不出对象还把任务建出来，就是本缺陷的表现本身
        if summary["blockers"]:
            raise WorkspaceError("；".join(summary["blockers"]))
        meta = self.create_project(name, owner=owner)
        root = self.project_root(meta["id"])
        try:
            # 反向导入走发布适配器的 materialize（方案 A8 SPI），与发布同一套包格式
            from ..publish.ontology_package import ADAPTER

            ADAPTER.materialize(archive_path, root)
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
            ["python3", str(script)], cwd=root, capture_output=True, text=True, timeout=120,
            env=minimal_env(),
        )
        if result.returncode != 0:
            raise WorkspaceError(f"示例注入失败: {result.stderr.strip()[:300]}", 500)
        status = _git(root, "status", "--porcelain")
        if status.strip():
            _git(root, "add", "-A")
            _git(root, "commit", "-m", "research: 注入外卖订单示例领域")
