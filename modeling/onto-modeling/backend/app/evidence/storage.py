"""证据层在数据目录里的文件（`design/evidence-collection.md` §6）：不在工作区，智能体读不到。

`DATA_DIR/evidence/` 下：`sources.json`、`grants/`、`requests/`、`jobs/<项目>/`、`uploads/`、`blobs/<前两位>/<sha256>`、
`index/<项目>.sqlite`、`tmp/`；连接型提供方另有 `salts/<项目>.key`（项目的脱敏盐）、
`inventories/<项目>/<源>.json`（discover 的清单）。blob 仓按内容寻址，放目标程序和 REA 原始结果，主容器只算哈希、不解析。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import uuid
from pathlib import Path

from .. import config

SHA256 = re.compile(r"^[0-9a-f]{64}$")


def root() -> Path:
    return Path(config.DATA_DIR) / "evidence"


def tmp_dir() -> Path:
    path = root() / "tmp"
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path: Path, data) -> None:
    """原子写：临时文件写完再 os.replace。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


# ---- blob 仓 ----


def blob_path(sha256: str) -> Path:
    if not isinstance(sha256, str) or not SHA256.match(sha256):
        raise ValueError("sha256 格式不对")
    return root() / "blobs" / sha256[:2] / sha256


def blob_size(sha256: str) -> int | None:
    """blob 的字节数；不在仓里（或哈希格式不对）为 None。"""
    try:
        path = blob_path(sha256)
    except ValueError:
        return None
    return path.stat().st_size if path.is_file() else None


def adopt_blob(temporary: Path, sha256: str) -> Path:
    """把已算好哈希的临时文件收进 blob 仓；同一内容已经在仓里就丢掉临时文件。"""
    target = blob_path(sha256)
    if target.is_file():
        Path(temporary).unlink(missing_ok=True)
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(temporary, target)
    return target


# ---- 连接型提供方（spec §8）----


def project_salt(project_id: str) -> str:
    """项目的脱敏盐（关联标识加盐哈希用，hex）：`salts/<项目>.key`，首次用时随机生成 32 字节，0600。
    先写临时文件再硬链接到位：并发的两个任务拿到的是同一把盐。"""
    path = root() / "salts" / f"{project_id}.key"
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(secrets.token_bytes(32))
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
        finally:
            temporary.unlink(missing_ok=True)
    return path.read_bytes().hex()


def inventory_path(project_id: str, source_id: str) -> Path:
    """`discover` 的结果：`inventories/<项目>/<源>.json`（不提交快照，EV-13 预览的估算用它）。"""
    return root() / "inventories" / project_id / f"{source_id}.json"
