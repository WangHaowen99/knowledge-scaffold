"""凭据存储公共件：数据目录下的 JSON 文件，文件 0600，读接口只回 has_* 标记。

沿用个人模型配置、管理员模型配置与原数据源配置的写法，收成一处：

- 写入先落同目录的临时文件（创建即 0600），写完再原子替换：盘上任何时刻都没有宽权限的
  副本，也不会读到写了一半的文件；
- 只归凭据用的目录（如证据源登记）建成 0700；
- 对外视图把密钥字段换成 has_<字段>，不回显任何字符；更新时密钥字段留空表示沿用已保存的值；
- 删除（项目删除、功能下线）要删净：连同残留的临时文件，删不掉的路径交给调用方记日志。

读改写经 SecretStore.update 串行（进程内一把锁）。后面的证据源层（数据源登记的连接凭据）
复用这里。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
from contextlib import suppress
from pathlib import Path
from typing import Any, Callable, Iterable, TypeVar

FILE_MODE = 0o600
DIR_MODE = 0o700
_TMP_SUFFIX = ".tmp"

T = TypeVar("T")

_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def _lock_for(path: Path) -> threading.RLock:
    key = str(Path(path).absolute())
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = _locks[key] = threading.RLock()
        return lock


def ensure_private_dir(directory: Path) -> Path:
    """只放凭据的目录：不存在时建出，权限收到 0700。"""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(DIR_MODE)
    return directory


def write_private_json(path: Path, data: Any) -> None:
    """原子写入 0600 的 JSON。临时文件由 mkstemp 创建（即 0600），fsync 后替换目标。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=_TMP_SUFFIX)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, FILE_MODE)
        os.replace(temporary, path)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


def read_json(path: Path, default: Any = None) -> Any:
    """读不到（不存在、损坏、不是 UTF-8）时返回 default，不抛异常。"""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def public_view(record: dict, secret_fields: Iterable[str]) -> dict:
    """对外视图：去掉密钥字段，换成 has_<字段>（只表明存在，不回显任何字符）。"""
    view = dict(record)
    for field in secret_fields:
        view[f"has_{field}"] = bool(view.pop(field, ""))
    return view


def keep_secrets(new: dict, previous: dict | None, secret_fields: Iterable[str]) -> dict:
    """更新时密钥字段留空表示沿用已保存的值；返回合并后的新字典，不改入参。"""
    merged = dict(new)
    previous = previous or {}
    for field in secret_fields:
        if not str(merged.get(field) or "").strip() and previous.get(field):
            merged[field] = previous[field]
    return merged


def purge_paths(paths: Iterable[Path]) -> list[str]:
    """可靠删除凭据文件或目录（连同同目录下写了一半的临时文件）。

    返回仍然存在的路径（删不掉时交给调用方记日志），全部删净时为空列表。
    """
    leftovers: list[str] = []
    for path in paths:
        path = Path(path)
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)
        else:
            for candidate in [path, *_temporaries(path)]:
                try:
                    candidate.unlink(missing_ok=True)
                except OSError:
                    pass
        if path.exists() or path.is_symlink() or _temporaries(path):
            leftovers.append(str(path))
    return leftovers


def _temporaries(path: Path) -> list[Path]:
    parent = path.parent
    if not parent.is_dir():
        return []
    return list(parent.glob(f".{path.name}.*{_TMP_SUFFIX}"))


class SecretStore:
    """一份凭据文件（顶层是对象）。路径按调用时的配置取，测试改数据目录后自动跟随。"""

    def __init__(self, path: Path | Callable[[], Path]):
        self._path = path

    @property
    def path(self) -> Path:
        return Path(self._path() if callable(self._path) else self._path)

    def load(self) -> dict:
        data = read_json(self.path, {})
        return data if isinstance(data, dict) else {}

    def save(self, data: dict) -> None:
        with _lock_for(self.path):
            write_private_json(self.path, data)

    def update(self, mutate: Callable[[dict], T]) -> T:
        """读改写串行执行：mutate 就地修改传入的字典，返回值原样交回调用方。"""
        with _lock_for(self.path):
            data = self.load()
            result = mutate(data)
            write_private_json(self.path, data)
            return result

    def purge(self) -> list[str]:
        with _lock_for(self.path):
            return purge_paths([self.path])
