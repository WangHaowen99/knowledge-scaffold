"""工作区文件与资料：文件树、读写（Web 保存即成版本点）、资料上传与用途、删除，知识包目录规范化。"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .. import material_detect, package_mapping, solver_contract, table_sources
from ..packs import contracts as _contracts
from .errors import WorkspaceError
from .gitops import _by, _git
from .layout import (
    IMPORTED_PACKAGE_DIR,
    META_FILE,
    RESOURCE_DIRECTIVES,
    RESOURCE_SUBDIRS,
    SKIP_DIRS,
    _content_revision,
    _first_heading,
    _is_script_descriptor,
    is_auto_detected as _is_auto_detected,
    resource_text_path,
)


class FilesMixin:
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
        root = self.project_root(project_id)
        was_clean = not _git(root, "status", "--porcelain").strip()
        changed = self._migrate_legacy_logic(root)
        # 描述符按本项目锁定版本的契约库生成（方案 A16）
        descriptors = _contracts.module_for(root, "script_descriptor")
        changed = bool(descriptors.sync_workspace_descriptors(root / "ontology")) or changed
        changed = self._refresh_indexes(root) or changed
        if changed and was_clean:
            from ...kernel.writepath import Origin, stamped

            _git(root, "add", "-A")
            _git(root, "commit", "-m", stamped("chore: 知识包目录规范化（operation 目录、脚本描述符与索引）",
                                               Origin("plugin"), root))
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
            changed = self._refresh_index_dir(root, rel, title) or changed
        return changed

    def refresh_indexes_for(self, root: Path, paths: list[str]) -> list[str]:
        """写入事务的派生文件（方案 C6）：本次改动落在哪些目录，就刷新那几个目录的 index.md。

        返回内容有变化的 index.md 路径，由事务并进同一个提交。
        """
        touched = {path.rpartition("/")[0] for path in paths} | set(paths)
        changed = []
        for rel, title in self.INDEX_DIRS:
            if rel in touched and self._refresh_index_dir(root, rel, title):
                changed.append(f"{rel}/index.md")
        return changed

    def _refresh_index_dir(self, root: Path, rel: str, title: str) -> bool:
        directory = root / rel
        if not directory.is_dir() or directory.is_symlink():
            return False
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
        existing = index_path.read_text(encoding="utf-8") if index_path.is_file() else None
        if existing == content:
            return False
        index_path.write_text(content, encoding="utf-8")
        return True

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
                    try:
                        size = entry.stat().st_size
                    except OSError:
                        size = 0
                    nodes.append({"name": entry.name, "path": rel_path, "type": "file", "size": size})
            return nodes

        return walk(root, "")

    def read_file(self, project_id: str, rel_path: str, max_bytes: int) -> dict:
        root = self.project_root(project_id)
        target = self._resolve_inside(root, rel_path)
        if not target.is_file():
            raise WorkspaceError("文件不存在", 404, "file_not_found", {"path": rel_path})
        data = target.read_bytes()
        raw = data[: max_bytes + 1]
        truncated = len(raw) > max_bytes
        raw = raw[:max_bytes]
        if b"\x00" in raw:
            raise WorkspaceError("二进制文件不支持预览", 415, "binary_not_previewable")
        return {
            "path": rel_path,
            "content": raw.decode("utf-8", errors="replace"),
            "truncated": truncated,
            "size": len(data),
            # 乐观并发标记：保存时回传，内容被他人（含 agent）改过则拒绝覆盖
            "revision": _content_revision(data),
        }

    # 资料用途标签（对齐 modeling 的 material purpose 体系）；存量的旧用途标签读取时按 auto 处理
    MATERIAL_PURPOSES = ("auto", "goal", "schema", "term", "evidence")
    MATERIAL_READING_MODES = ("on_demand", "full")

    def resource_directives(self, project_id: str) -> dict:
        """resource/materials.json：资料路径 → {purpose, reading_mode}。无文件视为空。"""
        return self._resource_directives_at(self.project_root(project_id))

    @classmethod
    def _resource_directives_at(cls, root: Path) -> dict:
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
            if isinstance(directive, dict) and directive.get("purpose") not in cls.MATERIAL_PURPOSES:
                directive["purpose"] = "auto"
        return data

    def set_resource_directive(
        self,
        project_id: str,
        rel_path: str,
        purpose: str,
        reading_mode: str = "on_demand",
        author: str = "",
        schema_selection: dict | None = None,
    ) -> dict:
        """改用途；数据 Schema 表格可一并定 Sheet 范围（schema_selection：{mode, included_sheets, revision}）。"""
        if purpose not in self.MATERIAL_PURPOSES:
            raise WorkspaceError(f"资料用途必须是 {list(self.MATERIAL_PURPOSES)} 之一")
        if reading_mode not in self.MATERIAL_READING_MODES:
            raise WorkspaceError(f"读取模式必须是 {list(self.MATERIAL_READING_MODES)} 之一")
        from ...kernel.writepath import ChangeTx, Origin

        with ChangeTx(self, project_id, Origin("web", author), summary="设置资料用途") as tx:
            root = tx.root
            target = self._resolve_inside(root, rel_path)
            rel = target.relative_to(root.resolve()).as_posix()
            if not rel.startswith("resource/") or not target.is_file():
                raise WorkspaceError("只能为 resource/ 下的资料设置用途", 400)
            directives = self.resource_directives(project_id)
            directive = {"purpose": purpose, "reading_mode": reading_mode}
            kept = self._schema_selection(target, rel, purpose, directives.get(rel), schema_selection)
            if kept is not None:
                directive["schema_selection"] = kept
            tx.touch(RESOURCE_DIRECTIVES)
            directives[rel] = directive
            self._write_resource_directives(root, directives)
            # 用途变化会影响派生产物的口径，视同资料更新
            self._update_meta_unlocked(root, resource_updated_at=int(time.time()))
            # 资料本身一并提交：旧版上传未进 git 的表格，标成 schema 后发布才读得到
            tx.touch(rel, snapshot=False)
            tx.commit(f"web: 设置资料用途 {rel}{_by(author)}")
            return {"path": rel, **directives[rel]}

    @staticmethod
    def _schema_selection(target: Path, rel: str, purpose: str, previous, requested: dict | None) -> dict | None:
        """要存的人工 Sheet 范围；None 表示按结构现算（ADR-0096）。

        没带范围时沿用原来的人工范围（只改读法不丢勾选），但它对不上当前文件就报 409，先去确认范围；
        mode=auto 撤掉人工范围；mode=manual 要带确认时的文件修订，勾的 Sheet 有表头错误不收。
        """
        is_table = target.suffix.lower() in table_sources.TABLE_SUFFIXES
        if purpose != "schema" or not is_table:
            if requested is not None:
                raise WorkspaceError("只有数据 Schema 表格能设 Sheet 范围", 400, "invalid_argument",
                                     {"fields": {"schema_selection": "not_allowed"}})
            return None
        if requested is None:
            if not isinstance(previous, dict) or previous.get("purpose") != "schema":
                return None
            kept = previous.get("schema_selection")
            if kept is not None:
                table_sources.resolve(target, rel, {"purpose": "schema", "schema_selection": kept})
            return kept
        if requested.get("mode") == "auto":
            return None
        # 同一次检查里算修订和读表头：存下的修订就是检查过的那份文件
        scope = table_sources.resolve(target, rel, {"purpose": "schema"})
        if requested.get("revision") != scope["revision"]:
            raise WorkspaceError("文件已变化，重新检查 Sheet 范围后再保存", 409, "schema_scope_stale", {"path": rel})
        if scope["pending"]:
            raise WorkspaceError("表格还没转出文本，稍后再选 Sheet 范围", 409, "material_text_pending", {"path": rel})
        included = list(requested.get("included_sheets") or [])
        by_name = {sheet["name"]: sheet for sheet in scope["sheets"]}
        problems = [f"没有 Sheet「{name}」" for name in included if name not in by_name]
        if len(set(included)) != len(included):
            problems.append("Sheet 重复")
        for name in included:
            for issue in (by_name.get(name) or {}).get("issues", []):
                if issue["severity"] == "error":
                    problems.append(f"Sheet「{name}」：{issue['message']}")
        if problems:
            raise WorkspaceError("\n".join(problems), 400, "invalid_argument",
                                 {"fields": {"schema_selection.included_sheets": "invalid"}})
        return table_sources.manual_selection(included, scope["revision"])

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
        commit: bool = True,
    ) -> dict:
        """上传即提交（与 Web 保存同口径）：发布按提交打包，没提交的资料进不了包。

        只提交本次的文件与用途清单，agent 别的未提交改动不带进这个提交。
        commit=False 时只落盘（调用方把一批资料合成一个提交，见 import_resources）。
        """
        if subdir not in RESOURCE_SUBDIRS:
            raise WorkspaceError(f"资料分类必须是 {sorted(RESOURCE_SUBDIRS)} 之一")
        safe_name = Path(filename).name
        if not safe_name or safe_name.startswith(".") or safe_name != filename:
            raise WorkspaceError("无效的文件名")
        from ...kernel.writepath import ChangeTx, Origin
        from .. import uploads

        # 资料进资料区都经过这里：类型白名单与 20 MiB 上限在服务层再挡一次，接口层漏了也绕不过去
        uploads.check_type(safe_name, "material")
        uploads.check_size(len(data), "material")
        rel = f"resource/{subdir}/{safe_name}"
        with ChangeTx(self, project_id, Origin("web", author), summary=f"上传资料 {rel}") as tx:
            root = tx.root
            (root / "resource" / subdir).mkdir(parents=True, exist_ok=True)
            replaced = (root / rel).exists()
            tx.write(rel, data)
            # 换了原文件，旧的文本版作废（ADR-0073）：同一个提交里删掉，不让助手读到过期文本，多源连接器再转
            stale_text = resource_text_path(rel)
            if (root / stale_text).is_file():
                tx.remove(stale_text)
            tx.touch(RESOURCE_DIRECTIVES)
            detected = self._update_upload_directive(project_id, rel, purpose, reading_mode)
            # 变更提醒依据：前端对比该时间与派生产物版本，提示"资料已更新，可让助手增量刷新"
            self._update_meta_unlocked(root, resource_updated_at=int(time.time()))
            action = "替换资料" if replaced else "上传资料"
            commit_id = tx.commit(f"web: {action} {rel}{_by(author)}").commit if commit else ""
            if not commit:
                tx.keep()  # 一批资料由调用方合成一个提交
            result = {"path": rel, "size": len(data), "replaced": replaced, "commit": commit_id}
            if detected:
                result["detected"] = detected
            return result

    def import_resources(self, project_id: str, items: list[dict], message: str, author: str = "") -> dict:
        """一批资料落到 resource/ 并合成一个提交（新建项目时的资料，契约 P-02）。

        items 每项 {subdir, filename, data, purpose}；返回 {saved: [...], commit}。
        """
        saved = [
            self.save_resource(
                project_id, item["subdir"], item["filename"], item["data"], item.get("purpose") or "auto",
                author=author, commit=False,
            )
            for item in items
        ]
        from ...kernel.writepath import ChangeTx, Origin

        commit = ""
        if saved:
            with ChangeTx(self, project_id, Origin("import", author), summary="导入资料") as tx:
                tx.touch(*[item["path"] for item in saved], RESOURCE_DIRECTIVES, snapshot=False)
                commit = tx.commit(f"{message}{_by(author)}").commit
        return {"saved": saved, "commit": commit}

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
        # 换文件不丢人工 Sheet 范围：留着它，读的地方发现对不上新文件会要求重新确认，不悄悄换成现算的范围
        kept = existing.get("schema_selection") if isinstance(existing, dict) else None
        current = directives.get(rel)
        if kept is not None and isinstance(current, dict) and current.get("purpose") == "schema":
            current.setdefault("schema_selection", kept)
        if json.dumps(directives, sort_keys=True) != before:
            self._write_resource_directives(root, directives)
        return detected

    def schema_candidates(self, project_id: str) -> dict:
        """资料区「疑似数据表」：用途仍为「由助手判断」、结构上判为数据表的表格资料。"""
        root = self.project_root(project_id)
        directives = self.resource_directives(project_id)
        candidates = {}
        for rel in unmarked_table_resources(root):
            directive = directives.get(rel)
            if isinstance(directive, dict) and directive.get("purpose") != "auto":
                continue
            detected = material_detect.detect_data_table(root / rel)
            if detected:
                candidates[rel] = detected
        return candidates

    # ---- 文件写入与历史版本 ----

    WRITE_FORBIDDEN_TOP = {".git", ".claude", ".ontology-state", IMPORTED_PACKAGE_DIR, META_FILE}

    def write_file(
        self,
        project_id: str,
        rel_path: str,
        content: str,
        base_revision: str = "",
        author: str = "",
        summary: str = "",
        source: str = "web",
    ) -> dict:
        """网页保存与 Studio 提交的写入：经写入事务（ChangeTx）校验版本、原子写入、只提交本文件。"""
        return self._write_file_unlocked(project_id, rel_path, content, base_revision, author, summary, source)

    @staticmethod
    def _changed_by(root: Path, rel: str) -> dict:
        """revision_conflict 的 details（契约 1.7）：谁、何时改的，以及当时的主线版本。

        文件和最近提交里的不一样，说明是还没提交的改动——只有助手会这样写；否则取最近提交的来源。
        """
        from .. import mainline
        from ..service_tokens import rfc3339
        from .versions import version_source

        last = _git(root, "log", "-1", "--format=%H%x1f%ct%x1f%s", "--", rel).strip().split("\x1f")
        dirty = bool(_git(root, "status", "--porcelain", "--", rel).strip())
        if dirty or len(last) != 3:
            try:
                changed_at = (root / rel).stat().st_mtime
            except OSError:
                changed_at = None
            return {"changed_by": "assistant", "changed_at": rfc3339(changed_at),
                    "version_label": mainline.state(root)["version_label"]}
        commit, ts, subject = last
        return {"changed_by": version_source(subject), "changed_at": rfc3339(int(ts)) if ts.isdigit() else None,
                "version_label": mainline.label(mainline.count(root, commit))}

    def _write_file_unlocked(self, project_id, rel_path, content, base_revision, author, summary="", source="web"):
        from ...kernel.writepath import ChangeTx, Origin

        root = self.project_root(project_id)
        target = self._resolve_inside(root, rel_path)
        rel = target.relative_to(root.resolve())
        if not rel.parts or rel.parts[0] in self.WRITE_FORBIDDEN_TOP or rel.parts[0] == ".onto":
            raise WorkspaceError("该路径不允许编辑", 403, "path_forbidden", {"path": rel_path})
        if rel.as_posix().startswith(f"{solver_contract.SOLVER_DIR}/"):
            # 求解契约要求多文件一致（binding 与五份 sidecar），单文件编辑无法保证
            raise WorkspaceError(
                "求解契约是受管目录，请在求解模型面板中维护", 403, "managed_path",
                {"path": rel_path, "use": f"/api/v1/projects/{project_id}/solver/contract"},
            )
        if _is_script_descriptor(rel):
            raise WorkspaceError(
                "脚本描述符由同名 .py 自动生成，不能直接编辑；请修改对应的 .py 脚本", 403, "managed_path",
                {"path": rel_path, "use": f"/api/v1/projects/{project_id}/files/content"},
            )
        if rel.parts[0] == "resource" and target.suffix.lower() != ".drawio":
            # 资料区只能经资料接口上传、替换、删除；drawio 资料允许在资料页编辑后保存（契约 F-03 页面）
            raise WorkspaceError(
                "资料请在资料页上传或替换", 403, "managed_path",
                {"path": rel_path, "use": f"/api/v1/projects/{project_id}/resources"},
            )
        if target.is_symlink():
            raise WorkspaceError("该路径不允许编辑", 403, "path_forbidden", {"path": rel_path})
        rel_posix = rel.as_posix()
        prefix = "studio" if source == "studio" else "web"
        note = f"：{' '.join(summary.split())[:80]}" if summary and summary.strip() else ""
        with ChangeTx(self, project_id, Origin(source, author), summary=f"保存 {rel_posix}") as tx:
            # 乐观并发（方案 7.4 双写合并）：Web 编辑与 agent 写同一文件不互相静默覆盖
            tx.check_revision(rel_posix, base_revision, lambda: self._changed_by(root, rel_posix))
            # 保存脚本时同名描述符由事务的派生步骤刷新，并进同一个提交
            tx.write(rel_posix, content)
            commit = tx.commit(f"{prefix}: 保存 {rel_posix}{note}{_by(author)}").commit
        return {
            "path": rel_path,
            "size": len(content.encode("utf-8")),
            "revision": _content_revision(content.encode("utf-8")),
            "commit": commit,
        }

    def delete_resource(self, project_id: str, rel_path: str, author: str = "") -> dict:
        """删除资料并提交（只含该文件与用途清单），与上传同口径。"""
        root = self.project_root(project_id)
        target = self._resolve_inside(root, rel_path)
        rel = target.relative_to(root.resolve())
        if len(rel.parts) < 2 or rel.parts[0] != "resource":
            raise WorkspaceError("只允许删除 resource/ 下的资料", 403)
        if rel.name == "materials.json" and len(rel.parts) == 2:
            raise WorkspaceError("资料用途清单不可直接删除", 403)
        from ...kernel.writepath import ChangeTx, Origin

        rel_posix = rel.as_posix()
        with ChangeTx(self, project_id, Origin("web", author), summary=f"删除资料 {rel_posix}") as tx:
            if not target.is_file():
                raise WorkspaceError("文件不存在", 404, "file_not_found", {"path": rel_path})
            tx.remove(rel_posix)
            if (root / resource_text_path(rel_posix)).is_file():
                tx.remove(resource_text_path(rel_posix))
            tx.touch(RESOURCE_DIRECTIVES)
            directives = self.resource_directives(project_id)
            if directives.pop(rel_posix, None) is not None:
                self._write_resource_directives(root, directives)
            self._update_meta_unlocked(root, resource_updated_at=int(time.time()))
            commit = tx.commit(f"web: 删除资料 {rel_posix}{_by(author)}").commit
            return {"deleted": rel_path, "commit": commit}

    def delete_file(self, project_id: str, rel_path: str, author: str = "") -> dict:
        """删除知识条目（限 knowledge/ 下）并提交，只含该文件，agent 别的未提交改动不带进来。"""
        root = self.project_root(project_id)
        target = self._resolve_inside(root, rel_path)
        rel = target.relative_to(root.resolve())
        if rel.parts[0] != "knowledge":
            raise WorkspaceError("只允许删除 knowledge/ 下的条目", 403)
        from ...kernel.writepath import ChangeTx, Origin

        rel_posix = rel.as_posix()
        with ChangeTx(self, project_id, Origin("web", author), summary=f"删除 {rel_posix}") as tx:
            if not target.is_file():
                raise WorkspaceError("文件不存在", 404)
            tx.remove(rel_posix)
            commit = tx.commit(f"knowledge: 删除 {rel_posix}{_by(author)}").commit
        return {"deleted": rel_path, "commit": commit}


def unmarked_table_resources(root: Path, limit: int = 50) -> list[str]:
        """已上传但未标 schema 的表格资料：改标用途即可让其中的表参与核对，不必重新上传。"""
        try:
            directives = FilesMixin._resource_directives_at(root)
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
        return found[:limit]
