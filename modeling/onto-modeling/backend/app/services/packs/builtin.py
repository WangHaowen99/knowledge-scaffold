"""镜像自带的官方包导入包仓库（方案 2.3 第 1 步、D7 前半）。

core 包的内容就是 `assets/workspace-toolkit`（原样迁过来，命令名不变），加上工作区规则
`workspace-rules.md` 与说明 `workspace-readme.md`。守卫与 hooks 基线不在包里，由内核注入
（app/kernel_guards）。

版本号是清单里的版本加内容摘要（如 0.1.0+3f9a1c2d，摘要算清单与全部文件）：镜像里的工具箱内容变了就是一个新版本，
包仓库里已有的版本目录永不改写。新镜像第一次启动时把 stable 通道指向它，跟随通道的项目在
下一条消息前升级；同一镜像重启不动通道指针，运维回滚过的通道不会被改回去。

没有清单的资产目录（旧版资产包、测试里的最小资产）按「core 新版本」合成一份清单。

镜像自带的其他官方包（方案 D7 后半拆出的可选包、第一个行业包）放在 `assets/packs/<名>/`，按同样的
规则入库：版本号是清单版本加内容摘要，镜像换了内容才动 stable 指针。

镜像里去掉了的官方包在入库时下架（retire_missing）：只认全部版本都由镜像入库、不是私有包、也不是 core 的包，
`assets/packs` 里没有它就下架。`assets/packs` 整个不存在（旧版资产、测试里的最小资产）时什么都不下架。
下架只记在通道记录上，版本目录不删：目录、详情、缺省启用、跟随通道、插件运维都看不到它，项目锁文件里的这一项
在下次核对锁文件时去掉（runtime.normalize），项目照常装配。

回滚：换回带这个包的旧镜像，入库时就取消下架，通道指针还是原来的版本。已经从项目锁文件里去掉的条目不会加回来
（锁文件变更已经提交，自动加回等于替用户改能力配置），要用的项目重新启用；新建项目按 default_on 照常带上。
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import tempfile
import threading
from pathlib import Path

import yaml

from . import manifest as mf
from .repository import PackRepository, pack_files, write_checksums

_log = logging.getLogger(__name__)
CORE = "core"
TOOLKIT_DIR = "workspace-toolkit"
PACKS_DIR = "packs"
# 不在工具箱目录里、但属于 core 包的文件（详设 8.1：装配目录的 CLAUDE.md 来自 core 的工作区规则）
EXTRA_FILES = {"workspace-rules.md": "rules", "workspace-readme.md": "readme"}
RETIRE_REASON = "镜像里没有这个官方包"
_CACHE: dict[tuple[str, str], str] = {}
_PACKS_CACHE: dict[tuple[str, str], dict[str, str]] = {}
_CACHE_LOCK = threading.Lock()


def synthesize_manifest(directory: Path, *, version: str = "0.0.0") -> dict:
    """没有清单的资产目录：按目录内容合成 core 清单。"""
    directory = Path(directory)

    def names(sub: str, suffix: str | None) -> list[str]:
        base = directory / sub
        if not base.is_dir():
            return []
        if suffix is None:
            return sorted(p.name for p in base.iterdir() if p.is_file() and not p.name.startswith("."))
        if suffix == "":
            return sorted(p.name for p in base.iterdir() if p.is_dir() and not p.name.startswith((".", "_")))
        return sorted(p.stem for p in base.iterdir() if p.suffix == suffix)

    return {
        "name": CORE, "version": version, "kind": "capability", "category": "capability",
        "label": "基础建模能力", "description": "工作区工具箱（由资产目录合成清单）",
        "publisher": {"kind": "official", "label": "平台"}, "required": True,
        "kernel_api": ">=1.0 <2.0", "engines": list(mf.ENGINES), "depends": {},
        "contributes": {
            "commands": names("commands", ".md"), "agents": names("agents", ".md"),
            "skills": names("skills", ""), "scripts": names("scripts", None),
            "templates": names("templates", None), "reference": names("reference", None),
            "rules": ["workspace-rules.md"] if (directory / "workspace-rules.md").is_file() else [],
            "readme": "workspace-readme.md" if (directory / "workspace-readme.md").is_file() else None,
            "flow": "flow.yaml" if (directory / "flow.yaml").is_file() else None,
            "hooks": {},
        },
    }


def _content_hash(directory: Path, manifest: dict) -> str:
    """内容摘要：清单（去掉版本号）加清单以外的全部文件。只改清单里的说明、依赖也换一个版本（F3b）：原来不算清单，
    摘要不变、版本号不变，入库时撞上「已在包仓库里且内容不同」。"""
    hasher = hashlib.sha256()
    meta = {key: value for key, value in (manifest or {}).items() if key != "version"}
    hasher.update(b"manifest\0" + json.dumps(meta, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8"))
    for rel in pack_files(directory):
        if rel == mf.MANIFEST_FILE:
            continue
        hasher.update(rel.encode("utf-8") + b"\0")
        hasher.update((directory / rel).read_bytes())
    return hasher.hexdigest()[:8]


def stage(source: Path, dest: Path, *, extras_from: Path | None = None, base_version: str | None = None) -> mf.Manifest:
    """把资产目录整理成可入库的 core 包：补规则与说明、清单、带内容摘要的版本号、校验和。"""
    source, dest = Path(source), Path(dest)
    shutil.copytree(source, dest, symlinks=False,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".DS_Store", mf.CHECKSUMS_FILE))
    if extras_from is not None:
        for name in EXTRA_FILES:
            if not (dest / name).exists() and (Path(extras_from) / name).is_file():
                shutil.copy2(Path(extras_from) / name, dest / name)
    manifest_path = dest / mf.MANIFEST_FILE
    if manifest_path.is_file():
        data = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    else:
        data = synthesize_manifest(dest)
    base = base_version or str(data.get("version") or "0.0.0").split("+", 1)[0]
    data["version"] = f"{base}+{_content_hash(dest, data)}"
    manifest_path.write_text(mf.dump(data), encoding="utf-8")
    write_checksums(dest)
    return mf.load(dest)


def import_core(repository: PackRepository, assets_dir: Path) -> str | None:
    """把镜像里的工具箱作为 core 的一个版本入库，返回版本号；资产目录里没有工具箱返回 None。"""
    assets_dir = Path(assets_dir)
    toolkit = assets_dir / TOOLKIT_DIR
    if not toolkit.is_dir():
        return None
    key = (str(repository.root.resolve()), str(toolkit.resolve()))
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached and repository.has(CORE, cached):
            return cached
        version = _install_builtin(repository, toolkit, extras_from=assets_dir)
        _CACHE[key] = version
        return version


def _install_builtin(repository: PackRepository, source: Path, *, extras_from: Path | None = None) -> str:
    """把镜像里的一个官方包目录入库；镜像版本变了就把 stable 指向它（同一镜像重启不动指针）。"""
    with tempfile.TemporaryDirectory(prefix="onto-pack-") as scratch:
        staged = Path(scratch) / source.name
        manifest = stage(source, staged, extras_from=extras_from)
        repository.install(staged, origin="builtin", note=str(source))
    if repository.unretire(manifest.name):
        _log.warning("镜像重新带上了官方包 %s，取消下架；之前从项目锁文件里去掉的不加回来", manifest.name)
    version = manifest.version
    entry = repository.channel_entry(manifest.name)
    if entry.get("builtin") != version or not repository.channel(manifest.name, "stable"):
        repository.set_channel(manifest.name, "stable", version, by="builtin")
        repository.set_field(manifest.name, "builtin", version)
    return version


def import_packs(repository: PackRepository, assets_dir: Path) -> dict[str, str]:
    """把 `assets/packs/<名>/` 下带清单的官方包逐个入库，返回 {包名: 版本}。"""
    base = Path(assets_dir) / PACKS_DIR
    if not base.is_dir():
        return {}
    key = (str(repository.root.resolve()), str(base.resolve()))
    with _CACHE_LOCK:
        cached = _PACKS_CACHE.get(key)
        if cached and all(repository.has(name, version) for name, version in cached.items()):
            return dict(cached)
        versions = {}
        for directory in sorted(base.iterdir()):
            if not directory.is_dir() or not (directory / mf.MANIFEST_FILE).is_file():
                continue
            data = yaml.safe_load((directory / mf.MANIFEST_FILE).read_text(encoding="utf-8")) or {}
            if data.get("name") != directory.name or data.get("name") == CORE:
                continue
            versions[directory.name] = _install_builtin(repository, directory)
        retire_missing(repository, set(versions))
        _PACKS_CACHE[key] = versions
        return dict(versions)


def retire_missing(repository: PackRepository, present: set[str]) -> list[str]:
    """下架镜像里已经没有的官方包（present 是这次从镜像入库的包名），返回这次下架的包名。

    有上传版本的包、私有包不动：它们归上传方管，镜像去掉同名官方包不代表平台不要它。
    """
    retired = []
    for name in repository.names(private=True):
        if name == CORE or name in present or repository.owner(name):
            continue
        versions = repository.versions(name)
        if not versions or not all(repository.is_builtin(name, version) for version in versions):
            continue
        repository.retire(name, reason=RETIRE_REASON)
        _log.warning("镜像里没有官方包 %s 了，下架；项目锁文件里的这一项下次核对时去掉", name)
        retired.append(name)
    return retired


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()
        _PACKS_CACHE.clear()
