"""插件目录（ADR-0103）：网页往插件的上传位放文件、从插件目录下载产物。

- 上传位在清单 `contributes.uploads` 里声明；文件落在主线工作区 `ext/<包名>/uploads/<上传位>/<id>/`，经写入事务提交，
  助手读得到（抽取要读），改不了（变更集把 `ext/*/uploads/` 当受保护路径，同资料区）；
- 现在只有一种上传：知识包（`knowledge-package`）。校验复用知识包对比（PC-01）的那套：子进程解析、限时、限大小、
  路径安全、manifest 合规；只留本体文本（manifest、对象、操作、规则、指标、脚本），不留原 ZIP、映射与索引；
- 下载只认 `ext/<包名>/` 下、主线已提交的内容：文件原样给，目录用 `git archive` 打 zip。
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess  # nosec B404 - 固定的 git 子命令，参数列表不经 shell
import tempfile
import threading
import time
import zipfile
from pathlib import Path, PurePosixPath

from .. import package_compare
from ..workspace.errors import WorkspaceError
from . import registry

EXT_DIR = "ext"
UPLOADS_DIR = "uploads"
META_FILE = "upload.json"
# 知识包里留下来的部分（包内一级目录）；manifest.yaml 单独留
KEEP_DIRS = ("objects", "operations", "rules", "metrics", "scripts")
COUNTED = {"objects": "objects", "operations": "operations", "rules": "rules", "metrics": "metrics", "scripts": "scripts"}
# 留下的文本合计上限：进 git 的是它，不是原 ZIP
MAX_KEPT_BYTES = 20 * 1024 * 1024
# 下载打包的上限（目录打成 zip 之后）
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
# 同时在解析的上传：每个解析占一个子进程
_PARSING = threading.BoundedSemaphore(2)
_SLUG_RE = re.compile(r"[^a-z0-9]+")


class PackFileError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------


def pack_dir(pack: str) -> str:
    return f"{EXT_DIR}/{pack}"


def slot_dir(pack: str, slot: str) -> str:
    return f"{EXT_DIR}/{pack}/{UPLOADS_DIR}/{slot}"


def is_upload_path(rel: str) -> bool:
    """仓库相对路径是否在某个插件的上传目录里（`ext/<包>/uploads/…`）：助手改不得。"""
    parts = str(rel).replace("\\", "/").lstrip("/").split("/")
    return len(parts) >= 3 and parts[0] == EXT_DIR and parts[2] == UPLOADS_DIR


def _clean_rel(rel: str) -> str | None:
    """插件目录里的相对路径：不能是绝对路径、不能带 . / .. / 隐藏段 / 反斜杠 / 控制字符；空串表示插件目录本身。"""
    rel = str(rel or "").strip().strip("/")
    if not rel:
        return ""
    if "\\" in rel or re.search(r"[\x00-\x1f\x7f]", rel) or len(rel) > 512:
        return None
    parts = PurePosixPath(rel).parts
    if any(part in (".", "..") or part.startswith(".") for part in parts):
        return None
    return "/".join(parts)


def _upload_id(package_id: str) -> str:
    """同一个知识包（manifest 的 id 相同）重传就替换：id 由包 id 得出，可读部分 + 摘要防撞。"""
    slug = _SLUG_RE.sub("-", package_id.lower()).strip("-")[:40] or "package"
    return f"{slug}-{hashlib.sha256(package_id.encode('utf-8')).hexdigest()[:6]}"


# ---------------------------------------------------------------------------
# 包与上传位
# ---------------------------------------------------------------------------


def _view(manager, project_id: str, pack: str) -> registry.PackView:
    root = manager.project_root(project_id)
    view = next((item for item in registry.desired_packs(manager, root) if item.name == pack), None)
    if view is None:
        raise PackFileError("pack_not_enabled", "本项目没启用这个插件", 409, {"pack": pack})
    return view


def _slot(view: registry.PackView, slot: str) -> dict:
    found = next((item for item in view.manifest.uploads() if item["id"] == slot), None)
    if found is None:
        raise PackFileError("not_found", "插件没有这个上传位", 404, {"pack": view.name, "slot": slot})
    return found


def _read_meta(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("id") else None


def list_uploads(manager, project_id: str, pack: str, slot: str) -> list[dict]:
    """PL-29：上传位里的文件（按上传时间倒序）。"""
    view = _view(manager, project_id, pack)
    _slot(view, slot)
    base = manager.project_root(project_id) / slot_dir(pack, slot)
    if not base.is_dir() or base.is_symlink():
        return []
    items = [meta for child in base.iterdir() if child.is_dir() and not child.is_symlink()
             for meta in [_read_meta(child / META_FILE)] if meta]
    return sorted(items, key=lambda item: item.get("uploaded_at") or 0, reverse=True)


def _copy_to(upload, destination: Path) -> int:
    total = 0
    with destination.open("wb") as target:
        while True:
            chunk = upload.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > package_compare.MAX_ARCHIVE_BYTES:
                raise PackFileError("payload_too_large", "文件超过大小限制", 413,
                                    {"limit": package_compare.MAX_ARCHIVE_BYTES})
            target.write(chunk)
    if not total:
        raise PackFileError("invalid_argument", "文件是空的", 400)
    return total


def _kept(index: dict) -> list[tuple[str, str]]:
    """（包内相对路径, ZIP 成员名）：manifest 与本体各目录的文件，不要各目录的 index.md。"""
    kept = []
    for rel, item in sorted(index["files"].items()):
        parts = rel.split("/")
        if rel == "manifest.yaml" or (len(parts) >= 2 and parts[0] in KEEP_DIRS and parts[-1] != "index.md"):
            kept.append((rel, item["path"]))
    return kept


def put_upload(manager, project_id: str, pack: str, slot: str, upload, filename: str, *, actor: str) -> dict:
    """PL-28：校验知识包，留下本体文本，写进上传位并提交到主线。同一个包（manifest id 相同）重传就替换。"""
    from ...kernel.writepath import ChangeTx, Origin

    view = _view(manager, project_id, pack)
    spec = _slot(view, slot)
    if spec["kind"] != "knowledge-package":
        raise PackFileError("invalid_argument", "这个上传位不收知识包", 400, {"slot": slot})
    if not _PARSING.acquire(blocking=False):
        raise PackFileError("rate_limited", "正在解析的上传较多，请稍后再试", 429)
    try:
        with tempfile.TemporaryDirectory(prefix="onto-pack-upload-") as scratch:
            archive = Path(scratch) / "package.zip"
            size = _copy_to(upload, archive)
            try:
                index = package_compare.index_archive(archive)
            except WorkspaceError as exc:
                code = exc.code if exc.code not in (None, "", "workspace_error") else "package_invalid"
                raise PackFileError(code, str(exc), exc.status or 400) from exc
            kept = _kept(index)
            kept_bytes = sum(index["files"][rel]["size"] for rel, _member in kept)
            if kept_bytes > MAX_KEPT_BYTES:
                raise PackFileError("payload_too_large", "知识包的本体文本太大", 413, {"limit": MAX_KEPT_BYTES})
            manifest = index["manifest"]
            upload_id = _upload_id(str(manifest["id"]))
            existing = [item for item in list_uploads(manager, project_id, pack, slot) if item["id"] != upload_id]
            if len(existing) >= spec["max"]:
                raise PackFileError("upload_slot_full", f"最多放 {spec['max']} 个，先删掉一些", 409,
                                    {"max": spec["max"]})
            counts = {key: 0 for key in COUNTED.values()}
            for rel, _member in kept:
                top = rel.split("/")[0]
                if top in COUNTED:
                    counts[COUNTED[top]] += 1
            meta = {
                "id": upload_id, "slot": slot, "filename": str(filename or "package.zip")[:200],
                "package": {"id": str(manifest["id"]), "name": str(manifest.get("name") or ""),
                            "version": str(manifest.get("version") or "")},
                "counts": counts, "files": len(kept), "size": size, "digest": _file_digest(archive),
                "uploaded_at": time.time(), "uploaded_by": actor,
            }
            target = f"{slot_dir(pack, slot)}/{upload_id}"
            root = manager.project_root(project_id)
            stale = _tracked_files(root, target)
            with zipfile.ZipFile(archive) as zf, \
                    ChangeTx(manager, project_id, Origin("web", actor),
                             summary=f"上传知识包「{meta['package']['name']}」到插件 {pack}") as tx:
                fresh = set()
                for rel, member in kept:
                    tx.write(f"{target}/{rel}", zf.read(member))
                    fresh.add(f"{target}/{rel}")
                for rel in stale - fresh - {f"{target}/{META_FILE}"}:
                    tx.remove(rel)
                tx.write(f"{target}/{META_FILE}", json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
                tx.commit(f"web: 上传知识包「{meta['package']['name']}」{meta['package']['version']} "
                          f"到插件 {pack} by {actor}")
            return meta
    finally:
        _PARSING.release()


def delete_upload(manager, project_id: str, pack: str, slot: str, upload_id: str, *, actor: str) -> None:
    """PL-30：删掉上传位里的一项（整个目录），提交到主线。"""
    from ...kernel.writepath import ChangeTx, Origin

    view = _view(manager, project_id, pack)
    _slot(view, slot)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,60}", upload_id or ""):
        raise PackFileError("not_found", "没有这一项", 404)
    root = manager.project_root(project_id)
    target = f"{slot_dir(pack, slot)}/{upload_id}"
    files = _tracked_files(root, target)
    meta = _read_meta(root / target / META_FILE)
    if not files or meta is None:
        raise PackFileError("not_found", "没有这一项", 404, {"id": upload_id})
    name = (meta.get("package") or {}).get("name") or upload_id
    with ChangeTx(manager, project_id, Origin("web", actor), summary=f"删除插件 {pack} 里的知识包「{name}」") as tx:
        for rel in sorted(files):
            tx.remove(rel)
        tx.commit(f"web: 删除插件 {pack} 里的知识包「{name}」 by {actor}")


def _tracked_files(root: Path, rel: str) -> set[str]:
    """工作区里某目录下的普通文件（仓库相对路径）；不跟随符号链接。"""
    directory = root / rel
    if not directory.is_dir() or directory.is_symlink():
        return set()
    return {f"{rel}/{path.relative_to(directory).as_posix()}" for path in directory.rglob("*")
            if path.is_file() and not path.is_symlink()}


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# 下载
# ---------------------------------------------------------------------------


def _git(root: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(  # nosec B603 NOSONAR(S4721) 固定 git 子命令，参数列表不经 shell
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.quotePath=false", *args],
        cwd=root, capture_output=True, timeout=timeout, check=False)


def download(manager, project_id: str, pack: str, rel: str) -> tuple[bytes, str, str]:
    """PL-31：插件目录里主线已提交的文件或目录 → (内容, 文件名, 媒体类型)。目录打成 zip。"""
    _view(manager, project_id, pack)
    clean = _clean_rel(rel)
    if clean is None:
        raise PackFileError("invalid_argument", "路径不合法", 400)
    target = f"{pack_dir(pack)}/{clean}" if clean else pack_dir(pack)
    root = manager.project_root(project_id)
    kind = _git(root, "cat-file", "-t", f"HEAD:{target}")
    kind_name = kind.stdout.decode().strip() if kind.returncode == 0 else ""
    name = PurePosixPath(target).name
    if kind_name == "blob":
        size = _git(root, "cat-file", "-s", f"HEAD:{target}")
        if size.returncode != 0 or int(size.stdout.decode().strip() or 0) > MAX_DOWNLOAD_BYTES:
            raise PackFileError("payload_too_large", "文件太大，不能下载", 413)
        blob = _git(root, "cat-file", "blob", f"HEAD:{target}")
        if blob.returncode != 0:
            raise PackFileError("not_found", "文件不存在", 404)
        return blob.stdout, name, _media(name)
    if kind_name == "tree":
        archived = _git(root, "archive", "--format=zip", f"--prefix={name}/", f"HEAD:{target}", timeout=120)
        if archived.returncode != 0:
            raise PackFileError("not_found", "目录不存在", 404)
        if len(archived.stdout) > MAX_DOWNLOAD_BYTES:
            raise PackFileError("payload_too_large", "内容太多，不能打包下载", 413)
        return archived.stdout, f"{name}.zip", "application/zip"
    raise PackFileError("not_found", "插件目录里没有这个文件", 404, {"path": clean})


def _media(name: str) -> str:
    suffix = PurePosixPath(name).suffix.lower()
    return {".md": "text/markdown; charset=utf-8", ".json": "application/json", ".yaml": "text/yaml; charset=utf-8",
            ".yml": "text/yaml; charset=utf-8", ".csv": "text/csv; charset=utf-8", ".txt": "text/plain; charset=utf-8",
            ".zip": "application/zip"}.get(suffix, "application/octet-stream")

