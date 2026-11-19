"""网关的插件仓库（ADR-0104；design/data-sources.md 第 6 节）：内置目录（镜像里只读）+ 插件卷（内核交付、可写）。

- 内置目录 `ONTO_CONNECTOR_CONVERTERS_DIR`：随镜像交付的 `kind: provider` 包，布局 `<名>/`；
- 插件卷 `ONTO_CONNECTOR_PLUGINS_DIR`：管理员信任后内核交付的包，布局 `<名>/<版本>/`，另有 `.sha256` 记交付 tar 的哈希。

同一个名字有多个版本时用版本号最大的；内置和插件卷里都有时同样比版本号。插件代码只在任务子进程里导入，
这里只读清单。安装先解压到临时目录、全部核对通过后改名进位，半份包不会出现。
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import shutil
import tarfile
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..evidence.sources import SourceInfo, SpecError, parse_spec
from . import registry
from .errors import GatewayError

_log = logging.getLogger(__name__)

PLUGINS_DIR_ENV = "ONTO_CONNECTOR_PLUGINS_DIR"
DEFAULT_PLUGINS_DIR = "/var/lib/connector/plugins"
MAX_TAR_BYTES = 32 * 1024 * 1024
MAX_MEMBERS = 2000
MAX_EXPANDED_BYTES = 64 * 1024 * 1024
CHECKSUMS_FILE = "checksums.sha256"
SHA_FILE = ".sha256"
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,39}$")
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]{1,40})?$")


@dataclass(frozen=True)
class LoadedSource:
    info: SourceInfo
    directory: Path

    @property
    def entry_path(self) -> Path:
        return self.directory / self.info.entry

    @property
    def vendor_path(self) -> Path | None:
        return self.directory / self.info.vendor if self.info.vendor else None


@dataclass(frozen=True)
class Plugin:
    name: str
    version: str
    origin: str                      # builtin / installed
    kind: str                        # converter / source
    directory: Path
    sha256: str = ""
    converter: registry.LoadedConverter | None = None
    source: LoadedSource | None = None

    def to_dict(self) -> dict:
        return {"name": self.name, "version": self.version, "origin": self.origin, "kind": self.kind,
                "sha256": self.sha256}


def version_key(version: str) -> tuple:
    """`1.2.3` < `1.10.0`；带预发布后缀的排在同号正式版前面。"""
    core, _, suffix = str(version).partition("-")
    numbers = []
    for part in core.split("+", 1)[0].split("."):
        numbers.append(int(part) if part.isdigit() else -1)
    return (*numbers, 0 if suffix else 1, suffix)


def _manifest(directory: Path) -> dict:
    data = yaml.safe_load((directory / "manifest.yaml").read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise SpecError("清单要是对象")
    return data


def load_plugin(directory: Path, *, origin: str, name: str, version: str | None = None, sha256: str = "") -> Plugin:
    """读一个包目录；不合法抛 SpecError。`version` 给了就要和清单一致（插件卷里按目录名核对）。"""
    data = _manifest(directory)
    if data.get("kind") != "provider":
        raise SpecError("kind 要是 provider")
    if str(data.get("name") or "") != name or not _NAME_RE.match(name):
        raise SpecError("清单的 name 要和目录名一致")
    manifest_version = str(data.get("version") or "")
    if version is not None and manifest_version != version:
        raise SpecError("清单的 version 要和版本目录一致")
    official = origin == "builtin"
    if isinstance(data.get("source"), dict):
        info = parse_spec(data, official=official)
        if not (directory / info.entry).is_file():
            raise SpecError("source.entry 指向的文件不存在")
        if info.vendor and not (directory / info.vendor).is_dir():
            raise SpecError("source.vendor 指向的目录不存在")
        return Plugin(name=name, version=manifest_version, origin=origin, kind="source", directory=directory,
                      sha256=sha256, source=LoadedSource(info=info, directory=directory))
    converter = registry.load_converter(directory, name)
    if converter is None:
        raise SpecError("要有合法的 converter 段或 source 段")
    return Plugin(name=name, version=manifest_version, origin=origin, kind="converter", directory=directory,
                  sha256=sha256, converter=converter)


class Store:
    def __init__(self, builtin_dir: Path | str | None = None, plugins_dir: Path | str | None = None):
        self.builtin_dir = Path(builtin_dir or os.environ.get(registry.DIR_ENV) or registry.DEFAULT_DIR)
        self.plugins_dir = Path(plugins_dir or os.environ.get(PLUGINS_DIR_ENV) or DEFAULT_PLUGINS_DIR)
        self._lock = threading.Lock()
        self._plugins: list[Plugin] = []
        self.reload()

    # ---- 读 ----

    def _scan(self) -> list[Plugin]:
        found: list[Plugin] = []
        if self.builtin_dir.is_dir():
            for directory in sorted(self.builtin_dir.iterdir()):
                if directory.is_dir() and (directory / "manifest.yaml").is_file():
                    try:
                        found.append(load_plugin(directory, origin="builtin", name=directory.name))
                    except (OSError, ValueError, yaml.YAMLError) as exc:
                        _log.warning("内置插件 %s 读不了：%s", directory.name, exc)
        if self.plugins_dir.is_dir():
            for named in sorted(self.plugins_dir.iterdir()):
                if not named.is_dir() or named.name.startswith("."):
                    continue
                for directory in sorted(named.iterdir()):
                    if not directory.is_dir() or directory.name.startswith("."):
                        continue
                    try:
                        sha = (directory / SHA_FILE).read_text(encoding="ascii").strip()
                        found.append(load_plugin(directory, origin="installed", name=named.name,
                                                 version=directory.name, sha256=sha))
                    except (OSError, ValueError, yaml.YAMLError) as exc:
                        _log.warning("插件 %s@%s 读不了：%s", named.name, directory.name, exc)
        return found

    def reload(self) -> None:
        plugins = self._scan()
        with self._lock:
            self._plugins = plugins

    def plugins(self) -> list[Plugin]:
        with self._lock:
            return list(self._plugins)

    def _current(self) -> dict[str, Plugin]:
        """每个名字用版本号最大的那个（同号时插件卷优先：管理员交付的是更新的修订）。"""
        chosen: dict[str, Plugin] = {}
        for plugin in self.plugins():
            held = chosen.get(plugin.name)
            key = (version_key(plugin.version), plugin.origin == "installed")
            if held is None or key > (version_key(held.version), held.origin == "installed"):
                chosen[plugin.name] = plugin
        return chosen

    def converters(self) -> dict[str, registry.LoadedConverter]:
        return {name: plugin.converter for name, plugin in self._current().items() if plugin.converter is not None}

    def sources(self) -> dict[str, LoadedSource]:
        return {name: plugin.source for name, plugin in self._current().items() if plugin.source is not None}

    # ---- 装、删 ----

    def install(self, name: str, version: str, data: bytes, sha256: str) -> Plugin:
        if not _NAME_RE.match(name or "") or not _VERSION_RE.match(version or ""):
            raise GatewayError(400, "plugin_invalid", "插件名或版本号不对")
        if len(data) > MAX_TAR_BYTES:
            raise GatewayError(413, "plugin_invalid", "插件包超过上限")
        target = self.plugins_dir / name / version
        with self._lock:
            if target.is_dir():
                held = (target / SHA_FILE).read_text(encoding="ascii").strip() if (target / SHA_FILE).is_file() else ""
                if held != sha256:
                    raise GatewayError(409, "plugin_conflict", "已装了同一版本的另一份包")
                return load_plugin(target, origin="installed", name=name, version=version, sha256=sha256)
            try:
                self.plugins_dir.mkdir(parents=True, exist_ok=True)
                staging = Path(tempfile.mkdtemp(prefix=".install-", dir=self.plugins_dir))
            except OSError as exc:
                raise GatewayError(503, "plugin_store_unavailable", f"插件卷写不进去（{type(exc).__name__}）") from None
            try:
                _extract(data, staging)
                _verify_checksums(staging)
                (staging / SHA_FILE).write_text(sha256 + "\n", encoding="ascii")
                plugin = load_plugin(staging, origin="installed", name=name, version=version, sha256=sha256)
                target.parent.mkdir(parents=True, exist_ok=True)
                staging.rename(target)
            except (SpecError, yaml.YAMLError) as exc:
                shutil.rmtree(staging, ignore_errors=True)
                raise GatewayError(400, "plugin_invalid", str(exc)[:300]) from None
            except BaseException:
                shutil.rmtree(staging, ignore_errors=True)
                raise
        self.reload()
        _log.info("插件已安装 %s@%s（%s）", name, version, plugin.kind)
        return next(p for p in self.plugins() if p.directory == target)

    def remove(self, name: str, version: str) -> bool:
        if not _NAME_RE.match(name or "") or not _VERSION_RE.match(version or ""):
            raise GatewayError(400, "plugin_invalid", "插件名或版本号不对")
        target = self.plugins_dir / name / version
        if not target.is_dir():
            if any(p.name == name and p.version == version and p.origin == "builtin" for p in self.plugins()):
                raise GatewayError(409, "plugin_builtin", "内置插件删不了")
            return False
        trash = self.plugins_dir / f".removed-{name}-{version}-{os.getpid()}-{threading.get_ident()}"
        with self._lock:
            target.rename(trash)
        shutil.rmtree(trash, ignore_errors=True)
        try:
            (self.plugins_dir / name).rmdir()
        except OSError:
            pass
        self.reload()
        _log.info("插件已删除 %s@%s", name, version)
        return True


def _extract(data: bytes, staging: Path) -> None:
    """只收普通文件和目录，不许绝对路径、`..`、链接、设备文件；文件数、解压后大小有上限。"""
    try:
        archive = tarfile.open(fileobj=io.BytesIO(data), mode="r:")
    except tarfile.TarError:
        raise SpecError("插件包不是 tar") from None
    with archive:
        members = archive.getmembers()
        if len(members) > MAX_MEMBERS:
            raise SpecError(f"插件包超过 {MAX_MEMBERS} 个文件")
        total = 0
        for member in members:
            parts = Path(member.name).parts
            if member.name.startswith(("/", "\\")) or not parts or ".." in parts or any(":" in part for part in parts):
                raise SpecError(f"不允许的路径：{member.name[:100]}")
            if member.isdir():
                (staging / member.name).mkdir(parents=True, exist_ok=True)
                continue
            if not member.isreg():
                raise SpecError(f"不允许的文件类型：{member.name[:100]}")
            total += member.size
            if total > MAX_EXPANDED_BYTES:
                raise SpecError("插件包解压后超过上限")
            path = staging / member.name
            path.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            with source, open(path, "wb") as handle:
                shutil.copyfileobj(source, handle)
            os.chmod(path, 0o644)
    if not (staging / "manifest.yaml").is_file():
        raise SpecError("插件包缺少 manifest.yaml")


def _verify_checksums(directory: Path) -> None:
    """有 checksums.sha256 时逐个核对，并且包里每个文件都要在清单里。"""
    listing = directory / CHECKSUMS_FILE
    if not listing.is_file():
        return
    expected: dict[str, str] = {}
    for line in listing.read_text(encoding="utf-8").splitlines():
        if line.strip():
            digest, _, rel = line.partition("  ")
            expected[rel.strip()] = digest.strip().lower()
    actual = sorted(str(path.relative_to(directory)) for path in directory.rglob("*")
                    if path.is_file() and path.name not in (CHECKSUMS_FILE, SHA_FILE))
    for rel in actual:
        digest = hashlib.sha256((directory / rel).read_bytes()).hexdigest()
        if expected.get(rel) != digest:
            raise SpecError(f"{rel} 和校验和对不上")
    missing = sorted(set(expected) - set(actual))
    if missing:
        raise SpecError(f"{missing[0]} 在校验和清单里但文件不存在")
