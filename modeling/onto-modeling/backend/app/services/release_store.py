"""发布留存：每次发布把知识包原样存一份，供发布记录、包清单与按版本下载（三份契约之「包出」，ADR-0020）。

下载读发布时留存的包，不按标签重新渲染：渲染器随版本升级会变，重新渲染同一个提交可能得到
不同的字节，下游按版本去重就会拿到和第一次不一样的包。同一版本、同一变体只认第一次留存的字节。

布局：数据目录 releases/<项目 ID>/ 下，标准包 <版本>.zip 与 <版本>.json（摘要、大小、来源提交、
发布人、格式、拉取记录），图模式变体 <版本>.graph.zip 与 <版本>.graph.json（第一次下载时按同一提交
渲染后留存）。不进工作区、不进 git；随数据备份，项目永久删除时一并删除。各函数的 data_dir 是数据
目录（WorkspaceManager.data_dir），与工作区放在同一个卷上。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import zipfile
from pathlib import Path

import yaml

_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
VARIANTS = ("standard", "graph")
MAX_PULLS = 50
MAX_MANIFEST_FILES = 2000
_lock = threading.Lock()


def root_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "releases"


def _project_dir(data_dir: Path, project_id: str) -> Path:
    if not _PROJECT_ID_RE.match(project_id or ""):
        raise ValueError("项目 ID 不合法")
    return root_dir(data_dir) / project_id


def _paths(data_dir: Path, project_id: str, version: str, variant: str = "standard") -> tuple[Path, Path]:
    if not VERSION_RE.match(version or ""):
        raise ValueError("版本号不合法")
    if variant not in VARIANTS:
        raise ValueError("变体不支持")
    folder = _project_dir(data_dir, project_id)
    stem = version if variant == "standard" else f"{version}.{variant}"
    return folder / f"{stem}.zip", folder / f"{stem}.json"


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _read_record(meta: Path) -> dict | None:
    try:
        record = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def get(
    data_dir: Path, project_id: str, version: str, commit: str = "", variant: str = "standard",
) -> tuple[Path, dict] | None:
    """留存的包与记录；给了 commit 时还要求记录的来源提交一致，否则视为没有留存。"""
    package, meta = _paths(data_dir, project_id, version, variant)
    if not package.is_file():
        return None
    record = _read_record(meta)
    if record is None or (commit and record.get("commit") != commit):
        return None
    return package, record


def save(
    data_dir: Path, project_id: str, version: str, source: Path, record: dict, variant: str = "standard",
) -> dict:
    """留存一份包。同一版本同一变体已有同一提交的留存时不覆盖，返回已有记录。"""
    with _lock:
        existing = get(data_dir, project_id, version, record.get("commit", ""), variant)
        if existing is not None:
            return existing[1]
        data = Path(source).read_bytes()
        package, meta = _paths(data_dir, project_id, version, variant)
        manifest = read_manifest(Path(source)) or {}
        stored = {
            **record,
            "version": version,
            "variant": variant,
            "format": "solver" if str(manifest.get("schema_version")) == "0.2" else "ontology",
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
            "pulls": [],
        }
        _write_atomic(package, data)
        _write_atomic(meta, json.dumps(stored, ensure_ascii=False, indent=2).encode("utf-8"))
        return stored


def digest(path: Path) -> str:
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def record_pull(data_dir: Path, project_id: str, version: str, by: str, variant: str) -> None:
    """服务令牌下载时在发布记录上记「已拉取」（只留最近 MAX_PULLS 次）。"""
    with _lock:
        _, meta = _paths(data_dir, project_id, version)
        record = _read_record(meta)
        if record is None:
            return
        pulls = [*(record.get("pulls") or []), {"by": by, "at": time.time(), "variant": variant}]
        record["pulls"] = pulls[-MAX_PULLS:]
        _write_atomic(meta, json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8"))


def records(data_dir: Path, project_id: str) -> dict[str, dict]:
    """项目全部标准包的留存记录：{版本: 记录}。"""
    try:
        folder = _project_dir(data_dir, project_id)
    except ValueError:
        return {}
    found: dict[str, dict] = {}
    if not folder.is_dir():
        return found
    for meta in folder.glob("*.json"):
        if not VERSION_RE.match(meta.stem) or not (folder / f"{meta.stem}.zip").is_file():
            continue
        record = _read_record(meta)
        if record is not None:
            found[meta.stem] = record
    return found


def read_manifest(package: Path) -> dict | None:
    """包里 <包 ID>/manifest.yaml 的内容；读不出来返回 None。"""
    try:
        with zipfile.ZipFile(package) as archive:
            names = [name for name in archive.namelist() if name.count("/") == 1 and name.endswith("/manifest.yaml")]
            if len(names) != 1:
                return None
            data = yaml.safe_load(archive.read(names[0]))
    except (OSError, zipfile.BadZipFile, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def list_files(package: Path) -> tuple[list[dict], bool]:
    """包内文件清单 [{path, size}]（最多 MAX_MANIFEST_FILES 条）与是否截断。"""
    with zipfile.ZipFile(package) as archive:
        infos = [info for info in archive.infolist() if not info.is_dir()]
    files = [{"path": info.filename, "size": info.file_size} for info in infos[:MAX_MANIFEST_FILES]]
    return files, len(infos) > MAX_MANIFEST_FILES


def purge_project(data_dir: Path, project_id: str) -> bool:
    """项目永久删除时删掉它的全部留存；删不净返回 False（由调用方记运维错误日志）。"""
    try:
        folder = _project_dir(data_dir, project_id)
    except ValueError:
        return True
    shutil.rmtree(folder, ignore_errors=True)
    return not folder.exists()
