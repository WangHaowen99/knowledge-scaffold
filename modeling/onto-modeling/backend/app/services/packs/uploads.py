"""插件包上传与审核（契约 PL-13～PL-18；详设 6.9 第 1 步、8.1 第 1～2 步；方案 D1）。

    <数据目录>/packs/uploads/<上传 ID>/
      record.json   上传记录：状态、检查结果、审核时间线
      pack/         解压后的包内容（静态检查与发布都读它）
      report.md     静态检查报告（PL-16 下载）

状态：checking →（静态检查）→ passed | failed →（管理员）→ published | rejected；「暂不发布」
不调接口，保持 passed。发布才入包仓库：包仓库里的版本目录不可修改，退回的上传不留下任何版本。

普通用户上传的是私有包（scope=private，ADR-0079）：多一项白名单检查，passed →（本人）→ installed，入库后只有
本人看得见、启用得了。管理员可以把它发布出去，发布后变成公开的包。

旧的资产包上传（`POST /api/settings/assets/upload`，契约 PL-13 的去向）走同一条路：没有清单的
资产包当作 core 的新版本，补上镜像里的工作区规则、说明与流程定义，检查通过直接发布到 stable，
跟随通道的项目下一条消息前升级（方案 2.3 第 4 步，效果同原来，但能按项目回滚）。
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path

from . import builtin, checks
from . import manifest as mf
from .repository import OWNER_FIELD, PackRepository, RepositoryError, _atomic_write_json, pack_files

STATES = ("checking", "passed", "failed", "published", "rejected", "held", "installed")
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
SCOPE_PRIVATE = "private"
# 普通用户的上传不可信：压缩包的大小不说明解压后的大小；老上传超出数量就清掉，一个人占不满磁盘
PRIVATE_MAX_EXTRACTED_BYTES = 64 * 1024 * 1024
PRIVATE_MAX_FILES = 2000
PRIVATE_KEEP_UPLOADS = 20
PRIVATE_MAX_VERSIONS = 50
# 旧资产包里属于内核的东西：守卫与 hooks 基线现在由内核注入，带着会被静态检查当成冒名
_LEGACY_KERNEL_FILES = (
    "settings.json",
    "scripts/resource_write_guard.sh", "scripts/toolset_write_guard.sh", "scripts/lock_file_guard.sh",
    "scripts/ontology_scope_guard.sh", "scripts/pre_commit_check.sh", "scripts/post_commit_hook.sh",
    "scripts/session_start_hook.sh", "scripts/knowledge_signal_probe.sh",
)
# 旧资产包里没有、要从镜像补齐的 core 文件（原来就不在上传范围里）
_LEGACY_IMAGE_FILES = ("workspace-rules.md", "workspace-readme.md", "flow.yaml", "scripts/flow_state.py")
_LOCK = threading.RLock()
_log = logging.getLogger(__name__)


class UploadError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details


class UploadStore:
    def __init__(self, repository: PackRepository, assets_dir: Path):
        self.repository = repository
        self.assets_dir = Path(assets_dir)
        self.root = repository.root / "uploads"

    # ---- 记录 ----

    def _dir(self, upload_id: str) -> Path:
        if not upload_id or not upload_id.replace("-", "").isalnum():
            raise UploadError("not_found", "上传不存在", 404)
        return self.root / upload_id

    def get(self, upload_id: str) -> dict:
        try:
            return json.loads((self._dir(upload_id) / "record.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise UploadError("not_found", "上传不存在", 404) from None

    def _save(self, record: dict) -> dict:
        _atomic_write_json(self._dir(record["id"]) / "record.json", record)
        return record

    def list(self, state: str = "", *, uploaded_by: str = "") -> list[dict]:
        if not self.root.is_dir():
            return []
        records = []
        for entry in self.root.iterdir():
            try:
                record = json.loads((entry / "record.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if (not state or record.get("state") in state.split(",")) \
                    and (not uploaded_by or (record.get("uploaded_by") == uploaded_by
                                             and record.get("scope") == SCOPE_PRIVATE)):
                records.append(record)
        return sorted(records, key=lambda record: record.get("uploaded_at", 0), reverse=True)

    def report(self, upload_id: str) -> str:
        path = self._dir(upload_id) / "report.md"
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            raise UploadError("not_found", "检查报告还没生成", 404) from None

    # ---- 上传与检查 ----

    def create(self, archive: Path, filename: str, *, by: str, tenant_label: str = "", legacy: bool = False,
               private: bool = False) -> dict:
        """收下上传：解压到上传目录，记录状态 checking；格式不对直接拒（pack_upload_invalid）。"""
        if private:
            self._prune_private(by)
        upload_id = f"up-{uuid.uuid4().hex[:10]}"
        directory = self._dir(upload_id)
        directory.mkdir(parents=True)
        try:
            pack = self._extract(archive, directory / "pack", legacy=legacy, private=private)
        except UploadError:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        record = {
            "id": upload_id, "filename": filename, "size": Path(archive).stat().st_size,
            "uploaded_at": time.time(), "uploaded_by": by, "tenant_label": tenant_label or None,
            "scope": SCOPE_PRIVATE if private else None, "state": "checking", "summary": {"pass": 0, "warn": 0, "fail": 0},
            "manifest_ok": False, "checksums_ok": False, "legacy_assets": legacy,
            "pack_dir": str(pack.relative_to(directory)),
            "timeline": [{"at": time.time(), "actor": by, "text": "上传", "state": "checking"}],
        }
        return self._save(record)

    def _prune_private(self, by: str) -> None:
        for record in self.list(uploaded_by=by)[PRIVATE_KEEP_UPLOADS - 1:]:
            if record.get("state") != "checking":
                shutil.rmtree(self._dir(record["id"]), ignore_errors=True)

    def _extract(self, archive: Path, dest: Path, *, legacy: bool, private: bool = False) -> Path:
        try:
            zf = zipfile.ZipFile(archive)
        except (zipfile.BadZipFile, OSError):
            raise UploadError("pack_upload_invalid", "不是有效的 zip 包", 400, {"reason": "not_zip"}) from None
        budget = PRIVATE_MAX_EXTRACTED_BYTES
        with zf:
            if private and len(zf.infolist()) > PRIVATE_MAX_FILES:
                raise UploadError("pack_upload_invalid", f"包里超过 {PRIVATE_MAX_FILES} 个文件", 400, {"reason": "too_large"})
            for info in zf.infolist():
                parts = Path(info.filename).parts
                if info.filename.startswith("/") or ".." in parts:
                    raise UploadError("pack_upload_invalid", f"包里有越界路径：{info.filename}", 400,
                                      {"reason": "path_traversal"})
                if (info.external_attr >> 28) == 0xA:
                    raise UploadError("pack_upload_invalid", f"包里有符号链接：{info.filename}", 400,
                                      {"reason": "symlink"})
                if info.is_dir() or not parts or "__MACOSX" in parts:
                    continue
                target = dest / info.filename
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as source, open(target, "wb") as out:
                    while chunk := source.read(1 << 20):
                        budget -= len(chunk)
                        if private and budget < 0:
                            raise UploadError("pack_upload_invalid", "解压后超过 64 MiB", 400, {"reason": "too_large"})
                        out.write(chunk)
        root = dest
        # 兼容两种打包：zip 根就是包，或包一层目录（旧资产包还可能包一层 .claude/）
        if not (root / mf.MANIFEST_FILE).is_file():
            children = [child for child in dest.iterdir() if child.is_dir()] if dest.is_dir() else []
            nested = [child for child in children if (child / mf.MANIFEST_FILE).is_file()
                      or (legacy and (child / "commands").is_dir())]
            if len(nested) == 1:
                root = nested[0]
        if legacy:
            if not (root / "commands").is_dir() or not (root / "agents").is_dir():
                raise UploadError("pack_upload_invalid", "资产包无效：缺少 commands/ 或 agents/ 目录", 400,
                                  {"reason": "manifest_missing"})
            return self._legacy_core(root, dest.parent / "core")
        if not (root / mf.MANIFEST_FILE).is_file():
            raise UploadError("pack_upload_invalid", "包里缺少 manifest.yaml", 400, {"reason": "manifest_missing"})
        if not (root / mf.CHECKSUMS_FILE).is_file():
            raise UploadError("pack_upload_invalid", "包里缺少 checksums.sha256", 400, {"reason": "checksums_missing"})
        return root

    def _legacy_core(self, source: Path, dest: Path) -> Path:
        """旧资产包 → core 新版本：去掉现在归内核的守卫，补上镜像里的规则、说明与流程定义。"""
        with tempfile.TemporaryDirectory(prefix="onto-legacy-") as scratch:
            work = Path(scratch) / "core"
            shutil.copytree(source, work, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
            for rel in _LEGACY_KERNEL_FILES:
                (work / rel).unlink(missing_ok=True)
            image = self.assets_dir / builtin.TOOLKIT_DIR
            for rel in _LEGACY_IMAGE_FILES:
                origin = (self.assets_dir / rel) if rel.startswith("workspace-") else (image / rel)
                if not (work / rel).exists() and origin.is_file():
                    (work / rel).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(origin, work / rel)
            (work / mf.MANIFEST_FILE).unlink(missing_ok=True)
            stable = self.repository.latest(builtin.CORE, "stable")
            base = (stable or "0.1.0").split("+", 1)[0]
            builtin.stage(work, dest, base_version=base)
        return dest

    def run_checks(self, upload_id: str) -> dict:
        """静态检查（不执行包里的代码），写报告；有失败项为 failed。"""
        record = self.get(upload_id)
        directory = self._dir(upload_id)
        pack = directory / record["pack_dir"]
        private = record.get("scope") == SCOPE_PRIVATE
        core_names = None
        stable = self.repository.latest(builtin.CORE, "stable")
        if stable:
            core_dir = self.repository.version_dir(builtin.CORE, stable)
            core_names = checks.declared_names(self.repository.manifest(builtin.CORE, stable), core_dir)
        try:
            manifest, items = checks.run(pack, self.repository, tenant=private or bool(record.get("tenant_label")),
                                         private=private, core_names=core_names)
        except Exception as exc:  # noqa: BLE001 - 检查本身出错也要落成「失败」，不能一直停在 checking
            _log.exception("静态检查出错：%s", upload_id)
            manifest, items = None, [checks.item("internal", "静态检查", "fail", f"检查过程出错：{type(exc).__name__}")]
        if manifest is not None and manifest.name == builtin.CORE and record.get("tenant_label"):
            items.append(checks.item("tenant_core", "租户包不能替换基础建模能力", "fail", ""))
        if private and manifest is not None and not self.repository.name_free(manifest.name, record["uploaded_by"]):
            items.append(checks.item("private_name", "包名没被占用", "fail", f"{manifest.name} 已被占用，请换一个名字"))
        summary = checks.summary(items)
        size = sum((pack / rel).stat().st_size for rel in pack_files(pack)) if pack.is_dir() else 0
        record.update({
            "state": "failed" if summary["fail"] else "passed",
            "summary": summary,
            "checks": items,
            "manifest_ok": manifest is not None,
            "checksums_ok": next((item["result"] == "pass" for item in items if item["id"] == "checksums"), False),
            "manifest": manifest.summary(checksums_ok=summary["fail"] == 0, size=size,
                                         files=len(pack_files(pack))) if manifest else None,
            "name": manifest.name if manifest else None,
            "version": manifest.version if manifest else None,
        })
        record["timeline"].append({"at": time.time(), "actor": "system",
                                   "text": "静态检查" + ("未通过" if summary["fail"] else "通过"),
                                   "state": record["state"]})
        title = f"{record.get('name') or record['filename']} {record.get('version') or ''}".strip()
        (directory / "report.md").write_text(checks.report_markdown(title, items), encoding="utf-8")
        return self._save(record)

    # ---- 发布与退回 ----

    def publish(self, upload_id: str, *, channel: str, rollout: dict | None, by: str, auto_breaker: bool = False) -> dict:
        with _LOCK:
            record = self.get(upload_id)
            if record["state"] in ("published", "rejected"):
                raise UploadError("pack_upload_state", "这个上传已经发布或退回", 409, {"state": record["state"]})
            owner = self.repository.owner(record.get("name") or "")
            if owner and owner != record["uploaded_by"]:
                raise UploadError("pack_name_taken", f"包名 {record['name']} 是 {owner} 的私有包", 409)
            if record["state"] in ("failed", "checking"):
                failed = [item["label"] for item in record.get("checks") or [] if item["result"] == "fail"]
                raise UploadError("static_check_failed", "静态检查有失败项，不能发布", 422, {"failed": failed})
            if channel not in ("stable", "beta"):
                raise UploadError("invalid_argument", "channel 只能是 stable 或 beta", 400, {"fields": {"channel": "enum"}})
            pack = self._dir(upload_id) / record["pack_dir"]
            try:
                self.repository.install(pack, origin="upload", by=by, note=record["filename"])
                self.repository.set_channel(record["name"], channel, record["version"], by=by,
                                            rollout={**(rollout or {}), "auto_breaker": bool(auto_breaker)})
                if owner:
                    self.repository.set_field(record["name"], OWNER_FIELD, None)  # 管理员把私有包转成公开的
            except RepositoryError as exc:
                raise UploadError(exc.code, str(exc), exc.status, exc.details) from exc
            record["state"] = "published"
            record["channel"] = channel
            record["rollout"] = rollout or {}
            record["timeline"].append({"at": time.time(), "actor": by, "text": f"发布到 {channel}", "state": "published"})
            return self._save(record)

    def install_private(self, upload_id: str, *, by: str) -> dict:
        """普通用户装自己的包：入库、占下包名。别人看不见，只有本人能启用到项目。"""
        with _LOCK:
            record = self.get(upload_id)
            if record["state"] == "installed":
                return record
            if record["state"] in ("failed", "checking"):
                failed = [item["label"] for item in record.get("checks") or [] if item["result"] == "fail"]
                raise UploadError("static_check_failed", "静态检查有失败项，不能安装", 422, {"failed": failed})
            if record["state"] != "passed":
                raise UploadError("pack_upload_state", "这个上传已经发布或退回", 409, {"state": record["state"]})
            name = record["name"]
            used = sum(len(self.repository.versions(item)) for item in self.repository.private_names(by))
            if used >= PRIVATE_MAX_VERSIONS:
                raise UploadError("pack_quota_exceeded", f"私有包最多 {PRIVATE_MAX_VERSIONS} 个版本，请联系管理员清理", 409,
                                  {"limit": PRIVATE_MAX_VERSIONS})
            try:
                self.repository.claim(name, by)
                self.repository.install(self._dir(upload_id) / record["pack_dir"], origin="private", by=by,
                                        note=record["filename"])
            except RepositoryError as exc:
                if not self.repository.versions(name) and self.repository.owner(name) == by:
                    self.repository.set_field(name, OWNER_FIELD, None)
                raise UploadError(exc.code, str(exc), exc.status, exc.details) from exc
            record["state"] = "installed"
            record["timeline"].append({"at": time.time(), "actor": by, "text": "安装", "state": "installed"})
            return self._save(record)

    def reject(self, upload_id: str, *, reason: str, by: str) -> dict:
        with _LOCK:
            record = self.get(upload_id)
            if record["state"] in ("published", "rejected", "installed"):
                raise UploadError("pack_upload_state", "这个上传已经发布或退回", 409, {"state": record["state"]})
            record["state"] = "rejected"
            record["reject_reason"] = reason
            record["timeline"].append({"at": time.time(), "actor": by, "text": f"退回：{reason}", "state": "rejected"})
            return self._save(record)

