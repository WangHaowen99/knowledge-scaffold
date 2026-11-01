"""项目锁文件 `.onto/packs.lock`（详设 4.6 第 3 项、14#1）：项目用哪些包的哪个版本。

进工作区 git：克隆、导出、回退时一起带上，能追溯每份产物是用哪个版本的方法论生成的。
停用的包留一条 enabled=false 的记录，重新启用回到原版本；装配只看 enabled 的条目。
格式是排好序的 JSON，同样的内容总写出同样的字节（锁哈希据此算）。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

LOCK_REL = ".onto/packs.lock"
LOCK_VERSION = 1


@dataclass
class LockEntry:
    name: str
    version: str
    sha256: str
    channel: str = "stable"
    # 跟随通道：通道指针动了，下一条消息前升级到通道版本（方案 2.3 第 4 步）；手动锁定版本后为 false
    follow: bool = True
    enabled: bool = True
    enabled_at: float = 0.0

    def key(self) -> tuple[str, str, str]:
        return (self.name, self.version, self.sha256)


@dataclass
class PackLock:
    packs: list[LockEntry] = field(default_factory=list)

    def get(self, name: str) -> LockEntry | None:
        return next((entry for entry in self.packs if entry.name == name), None)

    def enabled(self) -> list[LockEntry]:
        return [entry for entry in self.packs if entry.enabled]

    def upsert(self, entry: LockEntry) -> None:
        for index, current in enumerate(self.packs):
            if current.name == entry.name:
                self.packs[index] = entry
                return
        self.packs.append(entry)

    def copy(self) -> "PackLock":
        return PackLock([LockEntry(**asdict(entry)) for entry in self.packs])

    def to_dict(self) -> dict:
        # core 永远排第一，其余按名字：装配顺序与锁文件字节都不依赖启用先后
        ordered = sorted(self.packs, key=lambda entry: (entry.name != "core", entry.name))
        return {"lock_version": LOCK_VERSION, "packs": [asdict(entry) for entry in ordered]}

    def render(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_dict(cls, data: dict) -> "PackLock":
        if not isinstance(data, dict) or not isinstance(data.get("packs"), list):
            raise ValueError("锁文件格式不对")
        entries = []
        for item in data["packs"]:
            if not isinstance(item, dict) or not item.get("name") or not item.get("version"):
                raise ValueError("锁文件条目缺 name 或 version")
            entries.append(LockEntry(
                name=str(item["name"]), version=str(item["version"]), sha256=str(item.get("sha256") or ""),
                channel=str(item.get("channel") or "stable"), follow=bool(item.get("follow", True)),
                enabled=bool(item.get("enabled", True)), enabled_at=float(item.get("enabled_at") or 0),
            ))
        return cls(entries)

    def hash(self) -> str:
        """锁哈希：只看启用的包与版本、摘要（跟随标记、启用时间不影响装配）。"""
        keys = sorted(entry.key() for entry in self.enabled())
        return hashlib.sha256(json.dumps(keys).encode("utf-8")).hexdigest()[:16]


def path(root: Path) -> Path:
    return Path(root) / LOCK_REL


def read(root: Path) -> PackLock | None:
    """读锁文件；不存在返回 None，格式坏了抛 ValueError。"""
    try:
        text = path(root).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    return PackLock.from_dict(json.loads(text))


def write(root: Path, lock: PackLock) -> bool:
    """原子写锁文件，返回内容是否变了。"""
    target = path(root)
    text = lock.render()
    try:
        if target.read_text(encoding="utf-8") == text:
            return False
    except (OSError, UnicodeDecodeError):
        pass
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{int(time.time() * 1000)}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(target)
    return True
