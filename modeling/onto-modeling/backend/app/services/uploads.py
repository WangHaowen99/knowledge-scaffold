"""暂存上传区（API 契约 3.3 U-01～U-03、1.8；决定清单 #16、#17）。

新建向导、导入、对话附件都要在项目确定之前或消息发出之前先传文件：先存到数据目录的暂存区，
接口再按 upload_id 引用。只有上传者本人能用，24 小时过期（读取时判过期，上传时顺带清扫）。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
import uuid
from pathlib import Path

from .. import config
from ..api.errors import ApiError

TTL_SECONDS = 24 * 3600
PURPOSES = ("material", "attachment", "project_import")
MIB = 1024 * 1024
LIMITS = {
    "json_body_bytes": MIB,
    "file_write_bytes": 4 * MIB,
    "material_upload_bytes": 20 * MIB,
    "attachment_bytes": 20 * MIB,
    "attachments_per_message": 10,
    "project_import_bytes": 200 * MIB,
    "pack_upload_bytes": 200 * MIB,
    "page_size_max": 200,
}
PURPOSE_LIMITS = {
    "material": LIMITS["material_upload_bytes"],
    "attachment": LIMITS["attachment_bytes"],
    "project_import": LIMITS["project_import_bytes"],
}
MATERIAL_TYPES = (
    ".md", ".markdown", ".txt", ".doc", ".docx", ".xls", ".xlsx", ".xlsm", ".csv", ".pdf",
    ".drawio", ".html", ".htm", ".png", ".jpg", ".jpeg",
)
_ID_RE = re.compile(r"^up-[0-9a-f]{20}$")


def _root() -> Path:
    return Path(config.DATA_DIR) / "uploads"


def check_filename(filename: str) -> str:
    name = Path(filename or "").name
    if not name or name.startswith(".") or name != filename or "/" in name or "\\" in name:
        raise ApiError("invalid_filename", "文件名不合法", 400)
    return name


def check_type(filename: str, purpose: str) -> None:
    suffix = Path(filename).suffix.lower()
    allowed = (".zip",) if purpose == "project_import" else MATERIAL_TYPES
    if suffix not in allowed:
        raise ApiError("unsupported_media_type", "不支持的文件类型", 415, {"allowed": list(allowed)})


def check_size(size: int, purpose: str) -> None:
    limit = PURPOSE_LIMITS[purpose]
    if size > limit:
        raise ApiError("payload_too_large", "文件超过大小上限", 413, {"limit_bytes": limit})


def _purge_expired(now: float) -> None:
    root = _root()
    if not root.is_dir():
        return
    for entry in root.iterdir():
        try:
            meta = json.loads((entry / "meta.json").read_text(encoding="utf-8"))
            expired = meta["expires_at"] < now - TTL_SECONDS  # 过期后再留一天，读到时报 upload_expired
        except (OSError, ValueError, KeyError, TypeError):
            expired = True
        if expired:
            shutil.rmtree(entry, ignore_errors=True)


def view(meta: dict) -> dict:
    from ..api.deps import iso

    return {
        "upload_id": meta["upload_id"],
        "filename": meta["filename"],
        "size": meta["size"],
        "content_type": meta["content_type"],
        "sha256": meta["sha256"],
        "expires_at": iso(meta["expires_at"]),
        "detected": meta.get("detected"),
    }


def create(owner: str, purpose: str, filename: str, data: bytes, content_type: str = "", project_id: str = "") -> dict:
    if purpose not in PURPOSES:
        raise ApiError("invalid_argument", "用途必须是 material、attachment 或 project_import", 400,
                       {"fields": {"purpose": "enum"}})
    name = check_filename(filename)
    check_type(name, purpose)
    check_size(len(data), purpose)
    now = time.time()
    _purge_expired(now)
    upload_id = f"up-{uuid.uuid4().hex[:20]}"
    directory = _root() / upload_id
    directory.mkdir(parents=True)
    blob = directory / "blob"
    blob.write_bytes(data)
    detected = None
    if purpose == "material":
        from . import material_detect

        staged = directory / name  # 识别按扩展名判格式，临时放一份同名链接
        try:
            staged.symlink_to(blob)
            found = material_detect.detect_material(staged)
        except OSError:
            found = None
        finally:
            staged.unlink(missing_ok=True)
        detected = {"purpose": (found or {}).get("purpose"), "tables": list((found or {}).get("tables") or [])}
    meta = {
        "upload_id": upload_id,
        "owner": owner,
        "purpose": purpose,
        "project_id": project_id,
        "filename": name,
        "size": len(data),
        "content_type": content_type or "application/octet-stream",
        "sha256": hashlib.sha256(data).hexdigest(),
        "created_at": now,
        "expires_at": now + TTL_SECONDS,
        "detected": detected,
    }
    (directory / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return meta


def get(upload_id: str, owner: str, purpose: str | None = None) -> dict:
    """上传者本人的暂存上传；别人的、不存在的都报 upload_not_found，过期报 upload_expired。"""
    if not _ID_RE.match(upload_id or ""):
        raise ApiError("upload_not_found", "暂存上传不存在", 404)
    try:
        meta = json.loads((_root() / upload_id / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ApiError("upload_not_found", "暂存上传不存在", 404) from None
    if meta.get("owner", "") != owner:
        raise ApiError("upload_not_found", "暂存上传不存在", 404)
    if meta["expires_at"] < time.time():
        raise ApiError("upload_expired", "暂存上传已过期，请重新上传", 410)
    if purpose and meta["purpose"] != purpose:
        raise ApiError("upload_purpose_mismatch", "暂存上传的用途与接口不符", 400,
                       {"expected": purpose, "actual": meta["purpose"]})
    return meta


def path(upload_id: str) -> Path:
    return _root() / upload_id / "blob"


def read(upload_id: str) -> bytes:
    return path(upload_id).read_bytes()


def delete(upload_id: str, owner: str) -> None:
    get(upload_id, owner)
    shutil.rmtree(_root() / upload_id, ignore_errors=True)


def consume(upload_id: str) -> None:
    """用掉的暂存上传（已落进项目）立即删除。"""
    if _ID_RE.match(upload_id or ""):
        shutil.rmtree(_root() / upload_id, ignore_errors=True)


def resource_subdir(filename: str) -> str:
    """资料没指定分类时按扩展名归类（契约 R-02）：表格进 excel，流程图进 diagrams，其余进 others。"""
    suffix = Path(filename).suffix.lower()
    if suffix in (".xls", ".xlsx", ".xlsm", ".csv"):
        return "excel"
    if suffix == ".drawio":
        return "diagrams"
    return "others"
