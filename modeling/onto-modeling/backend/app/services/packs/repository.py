"""包仓库（详设 8.1 第 1 步；方案 D1）：每个版本一个不可修改的目录，附校验和。

- 入库是原子的：先复制到同目录下的临时目录、核对校验和，再 rename 成版本目录；中途停机只留下
  临时目录，启动时清掉。同名同版本只认第一次入库的内容，内容不同一律拒绝。
- 「不可修改」靠校验和兜底：装配前逐文件核对，被改过的版本不能装配（受保护目录第 2 层）。
- 通道（stable / beta）是指向版本的指针，存在 channels.json；回滚通道只改指针，旧版本一直保留。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path

from . import manifest as mf
from .manifest import CHECKSUMS_FILE, MANIFEST_FILE, Manifest, ManifestError

CHANNELS = ("stable", "beta")
_IGNORED_NAMES = {"__pycache__", ".DS_Store"}
_IGNORED_SUFFIXES = (".pyc", ".pyo")
_LOCK = threading.RLock()


class RepositoryError(Exception):
    def __init__(self, code: str, message: str, status: int = 409, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details


# ---- 校验和 ----


def _ignored(rel: Path) -> bool:
    return any(part in _IGNORED_NAMES for part in rel.parts) or rel.name.endswith(_IGNORED_SUFFIXES)


def pack_files(directory: Path) -> list[str]:
    """包里的文件（相对路径，排序）：不含校验和文件本身、缓存与 .pyc；符号链接不算文件。"""
    directory = Path(directory)
    out = []
    for path in directory.rglob("*"):
        rel = path.relative_to(directory)
        if _ignored(rel) or path.is_symlink() or not path.is_file():
            continue
        if rel.as_posix() == CHECKSUMS_FILE:
            continue
        out.append(rel.as_posix())
    return sorted(out)


def symlinks(directory: Path) -> list[str]:
    return sorted(
        path.relative_to(directory).as_posix() for path in Path(directory).rglob("*") if path.is_symlink()
    )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def render_checksums(directory: Path) -> str:
    return "".join(f"{file_sha256(Path(directory) / rel)}  {rel}\n" for rel in pack_files(directory))


def write_checksums(directory: Path) -> str:
    """生成 checksums.sha256（sha256sum 格式），返回包摘要。"""
    text = render_checksums(directory)
    (Path(directory) / CHECKSUMS_FILE).write_text(text, encoding="utf-8")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_checksums(text: str) -> dict[str, str]:
    entries: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        digest, _, rel = line.partition("  ")
        entries[rel.strip()] = digest.strip().lower()
    return entries


def verify_checksums(directory: Path) -> list[str]:
    """逐文件核对校验和，返回问题清单（空 = 一致）。"""
    directory = Path(directory)
    path = directory / CHECKSUMS_FILE
    try:
        expected = parse_checksums(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return [f"缺少 {CHECKSUMS_FILE}"]
    problems = []
    actual = pack_files(directory)
    for rel in actual:
        if rel not in expected:
            problems.append(f"{rel} 不在校验和清单里")
        elif file_sha256(directory / rel) != expected[rel]:
            problems.append(f"{rel} 内容与校验和不一致")
    for rel in sorted(set(expected) - set(actual)):
        problems.append(f"{rel} 在校验和清单里但文件不存在")
    for rel in symlinks(directory):
        problems.append(f"{rel} 是符号链接")
    return problems


def pack_digest(directory: Path) -> str:
    """包摘要：checksums.sha256 全文的 sha256（锁文件、装配哈希都用它）。"""
    return hashlib.sha256((Path(directory) / CHECKSUMS_FILE).read_bytes()).hexdigest()


def _atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def _now() -> float:
    return time.time()


class PackRepository:
    """<数据目录>/packs 下的包仓库。"""

    def __init__(self, data_dir: Path):
        self.root = Path(data_dir) / "packs"
        self._manifests: dict[tuple[str, str], Manifest] = {}

    # ---- 版本 ----

    def version_dir(self, name: str, version: str) -> Path:
        if not mf.NAME_RE.match(name or "") or not mf.is_version(version or ""):
            raise RepositoryError("pack_version_not_found", "包版本不存在", 404)
        return self.root / name / version

    def has(self, name: str, version: str) -> bool:
        try:
            return (self.version_dir(name, version) / MANIFEST_FILE).is_file()
        except RepositoryError:
            return False

    def names(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(
            entry.name for entry in self.root.iterdir()
            if entry.is_dir() and mf.NAME_RE.match(entry.name) and self.versions(entry.name)
        )

    def versions(self, name: str) -> list[str]:
        """一个包的全部版本：新的在前（semver 优先级，同级按入库时间）。"""
        directory = self.root / name
        if not mf.NAME_RE.match(name or "") or not directory.is_dir():
            return []
        found = [
            entry.name for entry in directory.iterdir()
            if entry.is_dir() and mf.is_version(entry.name) and (entry / MANIFEST_FILE).is_file()
        ]
        return sorted(found, key=lambda v: (mf.version_key(v), self.meta(name, v).get("released_at", 0)), reverse=True)

    def manifest(self, name: str, version: str) -> Manifest:
        key = (name, version)
        cached = self._manifests.get(key)
        if cached is None:
            if not self.has(name, version):
                raise RepositoryError("pack_version_not_found", f"包 {name} 没有版本 {version}", 404)
            cached = mf.load(self.version_dir(name, version))
            self._manifests[key] = cached
        return cached

    def meta(self, name: str, version: str) -> dict:
        path = self.root / name / f"{version}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if "released_at" not in data:
            try:
                data["released_at"] = (self.root / name / version).stat().st_mtime
            except OSError:
                data["released_at"] = 0
        return data

    def digest(self, name: str, version: str) -> str:
        return pack_digest(self.version_dir(name, version))

    def verify(self, name: str, version: str) -> list[str]:
        return verify_checksums(self.version_dir(name, version))

    def size(self, name: str, version: str) -> tuple[int, int]:
        directory = self.version_dir(name, version)
        files = pack_files(directory)
        return sum((directory / rel).stat().st_size for rel in files), len(files)

    def install(self, source: Path, *, origin: str, by: str = "", note: str = "") -> dict:
        """把 source 目录作为一个版本入库（清单、校验和已就绪），返回版本元数据。

        同名同版本已在库里时：内容一致直接返回（幂等），不一致拒绝（版本目录不可修改）。
        """
        source = Path(source)
        manifest = mf.load(source)
        problems = verify_checksums(source)
        if problems:
            raise RepositoryError("pack_upload_invalid", "校验和不一致：" + "；".join(problems[:5]), 400,
                                  {"reason": "checksums"})
        digest = pack_digest(source)
        with _LOCK:
            target = self.version_dir(manifest.name, manifest.version)
            if (target / MANIFEST_FILE).is_file():
                if pack_digest(target) != digest:
                    raise RepositoryError(
                        "pack_version_exists",
                        f"{manifest.name} {manifest.version} 已在包仓库里且内容不同；版本目录不可修改，请换一个版本号",
                        409,
                    )
                return self.meta(manifest.name, manifest.version)
            target.parent.mkdir(parents=True, exist_ok=True)
            staging = target.parent / f".tmp-{uuid.uuid4().hex[:12]}"
            shutil.copytree(source, staging, symlinks=True,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".DS_Store"))
            try:
                problems = verify_checksums(staging)
                if problems:
                    raise RepositoryError("pack_upload_invalid", "复制后校验和不一致", 400, {"reason": "checksums"})
                os.rename(staging, target)
            finally:
                if staging.exists():
                    shutil.rmtree(staging, ignore_errors=True)
            meta = {
                "name": manifest.name, "version": manifest.version, "sha256": digest,
                "origin": origin, "released_at": _now(), "released_by": by, "note": note,
            }
            _atomic_write_json(target.parent / f"{manifest.version}.json", meta)
            self._manifests.pop((manifest.name, manifest.version), None)
            return meta

    def cleanup_staging(self) -> list[Path]:
        """启动清理：上次入库中途停机留下的临时目录。"""
        removed = []
        if not self.root.is_dir():
            return removed
        for directory in self.root.iterdir():
            if not directory.is_dir():
                continue
            for entry in directory.glob(".tmp-*"):
                shutil.rmtree(entry, ignore_errors=True)
                removed.append(entry)
        return removed

    # ---- 通道 ----

    def _channels_path(self) -> Path:
        return self.root / "channels.json"

    def channels(self) -> dict:
        try:
            data = json.loads(self._channels_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        return data if isinstance(data, dict) else {}

    def channel(self, name: str, channel: str = "stable") -> str | None:
        entry = self.channels().get(name) or {}
        value = entry.get(channel)
        if isinstance(value, dict):
            value = value.get("version")
        return value if value and self.has(name, value) else None

    def channel_entry(self, name: str) -> dict:
        return dict(self.channels().get(name) or {})

    def set_channel(self, name: str, channel: str, version: str | None, *, by: str = "",
                    rollout: dict | None = None) -> dict:
        if channel not in CHANNELS:
            raise RepositoryError("invalid_argument", "通道只能是 stable 或 beta", 400)
        if version is not None and not self.has(name, version):
            raise RepositoryError("pack_version_not_found", f"包 {name} 没有版本 {version}", 404)
        if version is not None and version in self.disabled(name):
            raise RepositoryError("pack_unavailable", f"{name} {version} 已被禁用", 409, {"reason": "disabled"})
        with _LOCK:
            data = self.channels()
            entry = dict(data.get(name) or {})
            if channel == "beta" and version is not None:
                entry["beta"] = {"version": version, **(rollout or {"percent": 0, "by": "project", "targets": []})}
            else:
                entry[channel] = version
            history = list(entry.get("history") or [])
            history.append({"at": _now(), "channel": channel, "version": version, "by": by})
            entry["history"] = history[-50:]
            data[name] = entry
            _atomic_write_json(self._channels_path(), data)
            return entry

    def disabled(self, name: str) -> dict:
        return dict((self.channels().get(name) or {}).get("disabled") or {})

    def disable_version(self, name: str, version: str, reason: str, by: str = "") -> None:
        with _LOCK:
            data = self.channels()
            entry = dict(data.get(name) or {})
            disabled = dict(entry.get("disabled") or {})
            disabled[version] = {"reason": reason, "by": by, "at": _now()}
            entry["disabled"] = disabled
            data[name] = entry
            _atomic_write_json(self._channels_path(), data)

    def latest(self, name: str, channel: str = "stable") -> str | None:
        """通道指向的版本；通道没设时取最新的、没被禁用的版本。"""
        pinned = self.channel(name, channel)
        if pinned:
            return pinned
        disabled = self.disabled(name)
        for version in self.versions(name):
            if version not in disabled:
                return version
        return None
