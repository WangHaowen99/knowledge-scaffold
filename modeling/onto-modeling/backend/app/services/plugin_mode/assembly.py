"""插件开发装配目录（ADR-0087 做法 3）：`<数据目录>/plugin-dev/<哈希>/`。

插件对话的那一轮，助手副本的 `.claude` 指向这里（`worktree` 的 claude-override 标记），两个引擎按同一套目录约定读：

- `CLAUDE.md`：插件助手规则（`assets/plugin-mode/CLAUDE.md`），作 system prompt；
- `plugin-dev/<分类>/`：能力包、行业包、系统知识包各一份开发 skill，和 PL-27 下载的是同一份生成代码；
- `scripts/`：内核守卫的公共实现、`plugin_scope_guard.sh`（只放行写 `plugins/`）、`resource_read_guard.sh`；
- `settings.json`：拒绝写 `.claude/**`、`CLAUDE.md`、`.onto/**`，PreToolUse 挂上面两个守卫；
- `MANIFEST.json`：`{"kind": "plugin-dev", "hash"}`，Jiuwen 据此不拼建模专用的运行时说明。

哈希按内容算（公开包、平台版本变了就换一份）；先写临时目录再改名，只留最近几份，正被插件对话指着的不删。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import time
import uuid
from pathlib import Path

from ...kernel_guards import GUARDS_DIR
from ..packs import dev_skill

_log = logging.getLogger(__name__)

ASSET_DIR = "plugin-mode"
DIR_NAME = "plugin-dev"
MANIFEST_JSON = "MANIFEST.json"
MANIFEST_KIND = "plugin-dev"
CATEGORIES = ("capability", "industry", "system")
GUARD_SCRIPTS = ("_guard_common.py", "plugin_scope_guard.sh", "resource_read_guard.sh")
KEEP = 4
# 公开包变了最多晚这么久换装配目录：每条消息都要算指纹，平台快照要读全部公开包的清单
CACHE_SECONDS = 30.0

_lock = threading.Lock()
_cache: dict[str, tuple[float, dict[str, bytes], str]] = {}


def _hook(script: str) -> dict:
    return {"type": "command", "command": f'bash "$CLAUDE_PROJECT_DIR/.claude/scripts/{script}" || exit 2'}


def settings() -> dict:
    scope, read = _hook("plugin_scope_guard.sh"), _hook("resource_read_guard.sh")
    denied = ("/.claude/**", "/CLAUDE.md", "/.onto/**")
    return {
        "permissions": {"deny": [f"{tool}({path})" for tool in ("Edit", "Write") for path in denied]},
        "hooks": {"PreToolUse": [
            {"matcher": "Bash", "hooks": [scope, read]},
            {"matcher": "Read|NotebookRead", "hooks": [read]},
            {"matcher": "Edit|Write|MultiEdit|NotebookEdit", "hooks": [scope]},
        ]},
    }


def _render(manager) -> dict[str, bytes]:
    files: dict[str, bytes] = {
        "CLAUDE.md": (Path(manager.assets_dir) / ASSET_DIR / "CLAUDE.md").read_bytes(),
        "settings.json": json.dumps(settings(), ensure_ascii=False, indent=2).encode("utf-8"),
    }
    for name in GUARD_SCRIPTS:
        files[f"scripts/{name}"] = (GUARDS_DIR / name).read_bytes()
    for category in CATEGORIES:
        for rel, data in dev_skill.skill_files(manager, category).items():
            files[f"{DIR_NAME}/{category}/{rel}"] = data
    return files


def _digest(files: dict[str, bytes]) -> str:
    hasher = hashlib.sha256()
    for rel in sorted(files):
        hasher.update(rel.encode("utf-8") + b"\0")
        hasher.update(hashlib.sha256(files[rel]).digest())
    return hasher.hexdigest()[:16]


def _content(manager) -> tuple[dict[str, bytes], str]:
    key = str(Path(manager.data_dir).resolve())
    with _lock:
        cached = _cache.get(key)
        if cached is not None and time.monotonic() - cached[0] < CACHE_SECONDS:
            return cached[1], cached[2]
    files = _render(manager)
    digest = _digest(files)
    with _lock:
        _cache[key] = (time.monotonic(), files, digest)
    return files, digest


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def digest(manager) -> str:
    """当前应当生效的插件开发装配哈希（会话指纹的 plugin_dev 部分）。"""
    return _content(manager)[1]


def root(manager) -> Path:
    return Path(manager.data_dir) / DIR_NAME


def is_plugin_dev(directory: Path | None) -> bool:
    """这个目录（或 `.claude` 链接解析后）是不是插件开发装配目录。"""
    if directory is None:
        return False
    try:
        data = json.loads((Path(os.path.realpath(directory)) / MANIFEST_JSON).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get("kind") == MANIFEST_KIND


def ensure(manager) -> Path:
    """生成（或复用）当前内容的装配目录，返回绝对路径。同步、会写盘，调用方放在线程里。"""
    files, digest_ = _content(manager)
    base = root(manager)
    target = base / digest_
    if (target / MANIFEST_JSON).is_file():
        return target
    base.mkdir(parents=True, exist_ok=True)
    staging = base / f".tmp-{uuid.uuid4().hex[:8]}"
    try:
        for rel, data in files.items():
            path = staging / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (staging / MANIFEST_JSON).write_text(json.dumps({"kind": MANIFEST_KIND, "hash": digest_}), encoding="utf-8")
        _share(staging, base)
        try:
            os.rename(staging, target)
        except OSError:
            if not (target / MANIFEST_JSON).is_file():  # 并发生成同一份：别人先改好名就用别人的
                raise
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    _prune(manager, keep=target)
    return target


def _share(directory: Path, base: Path) -> None:
    """agent 用户只读（同建模装配目录，方案 E10）。"""
    try:
        from ...governance import agent_identity

        agent_identity.share_read_only(directory)
        os.chmod(base, 0o755)
    except Exception:  # noqa: BLE001 - 权限整理失败只影响 agent 用户能否读，记日志
        _log.warning("插件开发装配目录权限整理失败：%s", directory, exc_info=True)


def _in_use(manager) -> set[str]:
    from ...kernel.changesets import worktree

    used = set()
    for marker in (Path(manager.data_dir) / "changesets").glob(f"*/{worktree.OVERRIDE_FILE}"):
        try:
            used.add(Path(marker.read_text(encoding="utf-8").strip()).name)
        except OSError:
            continue
    return used


def _prune(manager, *, keep: Path) -> None:
    base = root(manager)
    try:
        dirs = sorted((path for path in base.iterdir() if path.is_dir() and not path.name.startswith(".")),
                      key=lambda path: path.stat().st_mtime, reverse=True)
        stale = [path for path in dirs[KEEP:] if path != keep and path.name not in _in_use(manager)]
        for path in stale:
            shutil.rmtree(path, ignore_errors=True)
        cutoff = time.time() - 3600
        for path in base.glob(".tmp-*"):  # 崩在半路留下的临时目录
            if path.stat().st_mtime < cutoff:
                shutil.rmtree(path, ignore_errors=True)
    except OSError:
        _log.warning("清理旧的插件开发装配目录失败", exc_info=True)
