# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Read-only comparison of immutable Hub releases and temporary package archives.

The comparison store deliberately keeps references to Hub releases instead of copying a
second version history.  Uploaded archives are private, short-lived inputs and are
never imported into a project workspace.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import uuid
import zipfile
import yaml
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .. import config
from . import hub_packages
from .workspace import WorkspaceError, WorkspaceManager

MAX_ARCHIVE_BYTES = int(os.environ.get("ONTO_COMPARE_MAX_ARCHIVE_BYTES", 50 * 1024**2))
MAX_UNCOMPRESSED_BYTES = int(os.environ.get("ONTO_COMPARE_MAX_UNCOMPRESSED_BYTES", 200 * 1024**2))
MAX_FILE_BYTES = int(os.environ.get("ONTO_COMPARE_MAX_FILE_BYTES", 64 * 1024**2))
MAX_TEXT_BYTES = int(os.environ.get("ONTO_COMPARE_MAX_TEXT_BYTES", 1024**2))
MAX_TEXT_LINES = int(os.environ.get("ONTO_COMPARE_MAX_TEXT_LINES", 20_000))
MAX_LINE_CHARS = int(os.environ.get("ONTO_COMPARE_MAX_LINE_CHARS", 20_000))
MAX_MANIFEST_BYTES = 256 * 1024
MAX_FILES = int(os.environ.get("ONTO_COMPARE_MAX_FILES", 2_000))
MAX_ENTRIES = int(os.environ.get("ONTO_COMPARE_MAX_ENTRIES", 4_000))
MAX_PATH_BYTES = int(os.environ.get("ONTO_COMPARE_MAX_PATH_BYTES", 1_024))
MAX_WORKERS = int(os.environ.get("ONTO_COMPARE_MAX_WORKERS", 2))
MAX_QUEUED_TASKS = int(os.environ.get("ONTO_COMPARE_MAX_QUEUED_TASKS", 8))
MAX_USER_PENDING_TASKS = int(os.environ.get("ONTO_COMPARE_MAX_USER_PENDING_TASKS", 2))
MAX_UPLOADS = int(os.environ.get("ONTO_COMPARE_MAX_UPLOADS", 2))
MAX_USER_STORAGE_BYTES = int(os.environ.get("ONTO_COMPARE_MAX_USER_STORAGE_BYTES", 1024**3))
MAX_GLOBAL_STORAGE_BYTES = int(os.environ.get("ONTO_COMPARE_MAX_GLOBAL_STORAGE_BYTES", 5 * 1024**3))
RESULT_RESERVATION_BYTES = int(os.environ.get("ONTO_COMPARE_RESULT_RESERVATION_BYTES", 1024**2))
TASK_TIMEOUT_SECONDS = int(os.environ.get("ONTO_COMPARE_TASK_TIMEOUT_SECONDS", 60))
WORKER_MEMORY_BYTES = int(os.environ.get("ONTO_COMPARE_WORKER_MEMORY_BYTES", 512 * 1024**2))
INPUT_TTL = int(os.environ.get("ONTO_COMPARE_INPUT_TTL_SECONDS", 24 * 3600))
COMPARISON_TTL = int(os.environ.get("ONTO_COMPARE_RESULT_TTL_SECONDS", 24 * 3600))
_EXECUTOR = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="onto-package-compare")
_CANCELLED: set[tuple[str, str]] = set()
_RUNNING: dict[tuple[str, str], object | None] = {}
_DEADLINES: dict[tuple[str, str], float] = {}
_TASK_LOCK = threading.RLock()
_UPLOAD_LIMIT = threading.BoundedSemaphore(MAX_UPLOADS)
_STORAGE_RESERVATIONS: dict[str, int] = {}
_RESULT_RESERVATIONS: set[tuple[str, str]] = set()
_CANCELLED_UPLOADS: dict[tuple[str, str], float] = {}
_ACTIVE_UPLOADS: dict[tuple[str, str], str] = {}
_PROCESS_STARTED_AT = time.time()
_SERVICE_LOCK = None
_SERVICE_AVAILABLE = True
_SERVICE_OWNER = False
_ACTIVE_TASK = threading.local()


def _safe_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise WorkspaceError("比较输入标识无效", 400)
    return value


def _source_ref(value: dict, *, allow_frozen: bool = False) -> dict:
    if not isinstance(value, dict):
        raise WorkspaceError("比较来源参数无效", 400)
    kind = value.get("kind")
    if kind == "upload":
        return {"kind": kind, "input_id": _safe_id(value.get("input_id"))}
    if kind == "release":
        source = {"kind": kind, "release_id": _safe_id(value.get("release_id"))}
        package_id = value.get("ontology_id") or value.get("hub_package_id")
        if package_id is not None:
            source["ontology_id"] = _safe_id(package_id)
        return source
    if allow_frozen and kind == "frozen":
        side = value.get("side")
        if side not in {"left", "right"}:
            raise WorkspaceError("冻结比较来源无效", 400)
        return {"kind": kind, "side": side}
    raise WorkspaceError("比较来源必须是 release 或 upload", 400)


def _scope(subject: str) -> str:
    return hashlib.sha256(subject.encode("utf-8")).hexdigest()


def root(subject: str) -> Path:
    path = Path(config.DATA_DIR) / "package-comparisons" / _scope(subject)
    path.mkdir(parents=True, exist_ok=True)
    return path


def start() -> bool:
    """Own the single-process comparison store without disabling the main application."""
    global _SERVICE_AVAILABLE, _SERVICE_LOCK, _SERVICE_OWNER
    if _SERVICE_LOCK is not None:
        _SERVICE_AVAILABLE = True
        _SERVICE_OWNER = True
        return True
    lock_path = Path(config.DATA_DIR) / "package-comparisons" / ".service.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+")
    try:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (ImportError, OSError):
        handle.close()
        _SERVICE_AVAILABLE = False
        _SERVICE_OWNER = False
        return False
    _SERVICE_LOCK = handle
    _SERVICE_AVAILABLE = True
    _SERVICE_OWNER = True
    return True


def _ensure_service_available() -> None:
    if not _SERVICE_AVAILABLE:
        raise WorkspaceError(
            "知识包版本对比正在由其他服务进程管理，请稍后重试",
            503,
            "compare_service_unavailable",
        )


def _checkpoint() -> None:
    task = getattr(_ACTIVE_TASK, "key", None)
    deadline = getattr(_ACTIVE_TASK, "deadline", None)
    if task:
        _ensure_active(*task)
    upload = getattr(_ACTIVE_TASK, "upload", None)
    if upload:
        _ensure_upload_active(*upload)
    if deadline and time.monotonic() > deadline:
        raise WorkspaceError("知识包比较处理超时，请缩小知识包后重试", 408, "compare_timeout")


def _write_json(path: Path, value: dict) -> None:
    WorkspaceManager._atomic_write(path, json.dumps(value, ensure_ascii=False, sort_keys=True).encode())


def _write_result_json(subject: str, path: Path, value: dict) -> None:
    """Persist a result without allowing result growth to bypass storage quotas."""
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    base = Path(config.DATA_DIR) / "package-comparisons"
    user_root = base / _scope(subject)
    with _TASK_LOCK:
        previous_size = path.stat().st_size if path.is_file() else 0
        growth = max(0, len(payload) - previous_size)
        user_reserved = _STORAGE_RESERVATIONS.get(subject, 0)
        total_reserved = sum(_STORAGE_RESERVATIONS.values())
        reservation_key = (subject, path.parent.name)
        if reservation_key in _RESULT_RESERVATIONS:
            user_reserved = max(0, user_reserved - RESULT_RESERVATION_BYTES)
            total_reserved = max(0, total_reserved - RESULT_RESERVATION_BYTES)
        if _tree_size(user_root) + user_reserved + growth > MAX_USER_STORAGE_BYTES:
            raise WorkspaceError(
                "当前用户的知识包比较临时空间已满，请清理后重试",
                429,
                "compare_user_storage_full",
            )
        if _tree_size(base) + total_reserved + growth > MAX_GLOBAL_STORAGE_BYTES:
            raise WorkspaceError(
                "知识包比较临时空间不足，请稍后重试",
                507,
                "compare_storage_full",
            )
        WorkspaceManager._atomic_write(path, payload)


def _write_input_meta(subject: str, path: Path, value: dict) -> None:
    """Write input metadata without letting it escape storage accounting."""
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    base = Path(config.DATA_DIR) / "package-comparisons"
    user_root = base / _scope(subject)
    with _TASK_LOCK:
        previous_size = path.stat().st_size if path.is_file() else 0
        growth = max(0, len(payload) - previous_size)
        # This input already owns one MAX_ARCHIVE_BYTES reservation. Its
        # archive is now present in _tree_size, so remove only that reservation
        # while retaining reservations belonging to concurrent uploads.
        user_reserved = max(
            0,
            _STORAGE_RESERVATIONS.get(subject, 0) - MAX_ARCHIVE_BYTES,
        )
        total_reserved = max(
            0,
            sum(_STORAGE_RESERVATIONS.values()) - MAX_ARCHIVE_BYTES,
        )
        if _tree_size(user_root) + user_reserved + growth > MAX_USER_STORAGE_BYTES:
            raise WorkspaceError(
                "当前用户的知识包比较临时空间已满，请清理后重试",
                429,
                "compare_user_storage_full",
            )
        if _tree_size(base) + total_reserved + growth > MAX_GLOBAL_STORAGE_BYTES:
            raise WorkspaceError(
                "知识包比较临时空间不足，请稍后重试",
                507,
                "compare_storage_full",
            )
        WorkspaceManager._atomic_write(path, payload)


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WorkspaceError("比较结果已损坏或已过期", 410) from exc


def _tree_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _reserve_upload(subject: str) -> None:
    _reserve_storage(subject, MAX_ARCHIVE_BYTES)


def _reserve_storage(subject: str, amount: int) -> None:
    base = Path(config.DATA_DIR) / "package-comparisons"
    user_root = base / _scope(subject)
    with _TASK_LOCK:
        user_reserved = _STORAGE_RESERVATIONS.get(subject, 0)
        total_reserved = sum(_STORAGE_RESERVATIONS.values())
        if _tree_size(user_root) + user_reserved + amount > MAX_USER_STORAGE_BYTES:
            raise WorkspaceError(
                "当前用户的知识包比较临时空间已满，请清理后重试",
                429,
                "compare_user_storage_full",
            )
        if _tree_size(base) + total_reserved + amount > MAX_GLOBAL_STORAGE_BYTES:
            raise WorkspaceError(
                "知识包比较临时空间不足，请稍后重试", 507, "compare_storage_full",
            )
        _STORAGE_RESERVATIONS[subject] = user_reserved + amount


def _release_upload_reservation(subject: str) -> None:
    _release_storage(subject, MAX_ARCHIVE_BYTES)


def _release_storage(subject: str, amount: int) -> None:
    with _TASK_LOCK:
        remaining = _STORAGE_RESERVATIONS.get(subject, 0) - amount
        if remaining > 0:
            _STORAGE_RESERVATIONS[subject] = remaining
        else:
            _STORAGE_RESERVATIONS.pop(subject, None)


def _ensure_upload_active(subject: str, request_id: str) -> None:
    with _TASK_LOCK:
        if (subject, request_id) in _CANCELLED_UPLOADS:
            raise WorkspaceError("知识包上传已取消", 410, "compare_upload_cancelled")


def _copy_upload(subject: str, upload, destination: Path, request_id: str = "") -> int:
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        total = 0
        with destination.open("wb") as target:
            while True:
                chunk = upload.read(1024 * 1024)
                if not chunk:
                    break
                _checkpoint()
                if request_id:
                    _ensure_upload_active(subject, request_id)
                total += len(chunk)
                if total > MAX_ARCHIVE_BYTES:
                    destination.unlink(missing_ok=True)
                    raise WorkspaceError("上传知识包超过大小限制", 413)
                target.write(chunk)
        if not total:
            destination.unlink(missing_ok=True)
            raise WorkspaceError("上传知识包不能为空", 400)
        return total
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def _member_path(name: str, *, include_directory: bool = False) -> str | None:
    if any(unicodedata.category(char) == "Cc" for char in name):
        raise WorkspaceError("知识包文件路径包含控制字符", 400)
    if "\\" in name:
        raise WorkspaceError(f"知识包文件路径不能包含反斜杠：{name}", 400)
    if not name:
        return None
    if len(name.encode("utf-8")) > MAX_PATH_BYTES:
        raise WorkspaceError("知识包文件路径超过长度限制", 400)
    if name.startswith("/"):
        raise WorkspaceError("知识包包含不安全的绝对路径", 400)
    is_directory = name.endswith("/")
    parts = name.rstrip("/").split("/")
    if any(part in ("", ".", "..") for part in parts) or ":" in parts[0]:
        raise WorkspaceError("知识包包含不安全的文件路径", 400)
    if any(part.lower() == ".git" for part in parts):
        raise WorkspaceError("知识包不能包含 .git 工作区目录", 400)
    if is_directory and not include_directory:
        return None
    return "/".join(parts)


def _category(path: str) -> str:
    categories = {
        "objects": "objects", "operations": "operations", "rules": "rules",
        "metrics": "metrics", "scripts": "scripts", "mappings": "mappings",
        "mapping": "mappings", "indexes": "indexes", "index": "indexes",
        "metadata": "metadata",
    }
    parts = path.lower().split("/")
    if parts[-1] == "manifest.yaml":
        return "metadata"
    for part in parts[:2]:
        if part in categories:
            return categories[part]
    return "other"


def _display_name(path: str, preview: bytes) -> str:
    fallback = path.rsplit("/", 1)[-1]
    if not path.lower().endswith((".md", ".markdown")) or not preview.startswith(b"---"):
        return fallback
    try:
        text = preview.decode("utf-8-sig")
    except UnicodeDecodeError:
        return fallback
    match = re.search(r"(?m)^(?:report_name|title):\s*['\"]?([^\r\n'\"]+)", text[:65536])
    return match.group(1).strip() if match else fallback


def _validate_manifest_keys(node):
    """Reject duplicate/non-string keys before safe YAML construction."""
    if isinstance(node, yaml.MappingNode):
        keys = set()
        for key_node, value_node in node.value:
            if not isinstance(key_node, yaml.ScalarNode) or key_node.tag != "tag:yaml.org,2002:str":
                raise WorkspaceError("manifest 包含重复或无效键", 400)
            if key_node.value in keys:
                raise WorkspaceError("manifest 包含重复或无效键", 400)
            keys.add(key_node.value)
            _validate_manifest_keys(value_node)
    elif isinstance(node, yaml.SequenceNode):
        for value_node in node.value:
            _validate_manifest_keys(value_node)


def _manifest_root(files: dict) -> str:
    """Select the package manifest without treating nested attachment files as roots."""
    wrapped = [name for name in files if name.count("/") == 1 and name.endswith("/manifest.yaml")]
    if "manifest.yaml" in files:
        if wrapped:
            raise WorkspaceError('知识包包含多个包根 manifest.yaml', 400)
        return "manifest.yaml"
    if len(wrapped) != 1:
        raise WorkspaceError('知识包必须包含唯一 manifest.yaml', 400)
    return wrapped[0]


def _strict_manifest(path: Path, files: dict, name: str) -> dict:
    with zipfile.ZipFile(path) as archive:
        try:
            if archive.getinfo(name).file_size > MAX_MANIFEST_BYTES:
                raise WorkspaceError('manifest 超过大小限制', 413)
            raw = archive.read(name)
            unsupported_yaml = any(
                isinstance(event, yaml.events.AliasEvent)
                or getattr(event, "tag", None) not in (None, "!")
                for event in yaml.parse(raw)
            )
            if unsupported_yaml:
                raise WorkspaceError("manifest 不支持 YAML 别名或标签", 400)
            depth = 0
            for event in yaml.parse(raw):
                if isinstance(event, (yaml.events.MappingStartEvent, yaml.events.SequenceStartEvent)):
                    depth += 1
                    if depth > 16:
                        raise WorkspaceError("manifest YAML 嵌套层级超过限制", 400)
                elif isinstance(event, (yaml.events.MappingEndEvent, yaml.events.SequenceEndEvent)):
                    depth -= 1
            _validate_manifest_keys(yaml.compose(raw, Loader=yaml.SafeLoader))
            manifest = yaml.safe_load(raw)
        except WorkspaceError:
            raise
        except yaml.YAMLError as exc:
            raise WorkspaceError('manifest YAML 无效', 400) from exc
    valid_manifest = (
        isinstance(manifest, dict)
        and manifest.get("kind") == "ontology-package"
        and str(manifest.get("schema_version")) in {"0.1", "0.2"}
    )
    if not valid_manifest:
        raise WorkspaceError("知识包 manifest kind 或 schema_version 不受支持", 400)
    if (
        str(manifest.get("schema_version")) == "0.2"
        and manifest.get("solver_layout") != "operation-centric.v1"
    ):
        raise WorkspaceError("知识包 solver_layout 不受支持", 400)
    for key in ("id", "version", "name", "description"):
        if not isinstance(manifest.get(key), str) or not manifest[key].strip():
            raise WorkspaceError("知识包 manifest 缺少必填字段", 400)
    return manifest


def _validate_members(infos: list[zipfile.ZipInfo], files: dict) -> None:
    file_paths = set(files)
    directory_paths: set[str] = set()
    supported_compressions = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
    for info in infos:
        rel = _member_path(info.filename, include_directory=True)
        if not rel:
            continue
        is_directory = info.is_dir() or info.filename.replace("\\", "/").endswith("/")
        if info.flag_bits & 0x1:
            raise WorkspaceError(f"知识包包含加密文件：{rel}", 400)
        if info.compress_type not in supported_compressions:
            raise WorkspaceError(f"知识包包含不支持的压缩格式：{rel}", 400)
        if not is_directory and info.file_size > max(info.compress_size, 1) * 1000:
            raise WorkspaceError(f"知识包包含异常压缩比文件：{rel}", 400)
        if is_directory:
            directory_paths.add(rel)
    if file_paths & directory_paths:
        raise WorkspaceError("知识包包含文件与目录冲突", 400)
    for path in file_paths:
        parts = path.split('/')
        for index in range(1, len(parts)):
            if '/'.join(parts[:index]) in file_paths:
                raise WorkspaceError(f"知识包包含文件与目录冲突：{path}", 400)


def _validate_original_member_names(infos: list[zipfile.ZipInfo]) -> None:
    """Reject NUL before ZipInfo.filename's compatibility truncation."""
    if any("\x00" in info.orig_filename for info in infos):
        raise WorkspaceError("知识包文件路径包含 NUL 控制字符", 400)


def _archive_index_core(path: Path) -> dict:
    files: dict[str, dict] = {}
    total_uncompressed = 0
    try:
        with zipfile.ZipFile(path) as archive:
            all_infos = archive.infolist()
            _validate_original_member_names(all_infos)
            if len(all_infos) > MAX_ENTRIES:
                raise WorkspaceError("知识包 ZIP 条目数量超过限制", 413)
            # Validate every member first: directory entries can expose an otherwise hidden
            # file/directory collision and absolute directory paths are unsafe as well.
            for info in all_infos:
                _member_path(info.filename, include_directory=True)
            infos = [info for info in all_infos if _member_path(info.filename)]
            # Inspect flags and compression methods before opening any member so malformed
            # archives are mapped to an actionable 4xx error instead of leaking zipfile errors.
            preview_files = {_member_path(info.filename): {} for info in infos}
            _validate_members(all_infos, preview_files)
            if len(infos) > MAX_FILES:
                raise WorkspaceError("知识包文件数量超过限制", 413)
            for info in infos:
                _checkpoint()
                rel = _member_path(info.filename)
                if rel in files:
                    raise WorkspaceError(f"知识包包含重复文件：{rel}", 400)
                if (info.external_attr >> 28) == 0xA:
                    raise WorkspaceError(f"知识包包含不安全的符号链接：{rel}", 400)
                if info.file_size > MAX_FILE_BYTES:
                    raise WorkspaceError(f"文件超过单文件大小限制：{rel}", 413)
                total_uncompressed += info.file_size
                if total_uncompressed > MAX_UNCOMPRESSED_BYTES:
                    raise WorkspaceError("知识包解压后超过大小限制", 413)
                digest = hashlib.sha256()
                preview = bytearray()
                try:
                    with archive.open(info) as source:
                        while True:
                            chunk = source.read(1024 * 1024)
                            if not chunk:
                                break
                            _checkpoint()
                            digest.update(chunk)
                            if len(preview) < 65536:
                                preview.extend(chunk[:65536 - len(preview)])
                except RuntimeError as exc:
                    raise WorkspaceError(f"知识包文件无法解压：{rel}", 400) from exc
                files[rel] = {
                    "path": rel, "size": info.file_size, "digest": digest.hexdigest(),
                    "category": _category(rel), "display_name": _display_name(rel, bytes(preview)),
                }
    except zipfile.BadZipFile as exc:
        raise WorkspaceError("知识包不是有效的 ZIP 文件", 400) from exc

    _validate_members(all_infos, files)
    manifest_name = _manifest_root(files)
    manifest = _strict_manifest(path, files, manifest_name)
    prefix = manifest_name[:-len("manifest.yaml")]
    member_paths = [
        _member_path(info.filename, include_directory=True)
        for info in all_infos
    ]
    has_outside_member = any(
        name != prefix.rstrip("/")
        and name != manifest_name
        and not name.startswith(prefix)
        for name in member_paths
        if name
    )
    if has_outside_member:
        raise WorkspaceError("知识包根目录外包含额外文件", 400)
    normalized = {}
    for name, item in files.items():
        relative = name[len(prefix):] if prefix else name
        normalized[relative] = {**item, "path": name, "category": _category(relative)}
    if len(normalized) <= 1:
        raise WorkspaceError("知识包没有可比较内容", 400)
    return {
        "files": normalized,
        "file_count": len(normalized),
        "uncompressed_size": total_uncompressed,
        "package_id": manifest["id"],
        "manifest": manifest,
    }


def _archive_index(path: Path) -> dict:
    """Parse an archive in a process that can be terminated on timeout or cancel."""
    output_fd, output_name = tempfile.mkstemp(
        prefix="onto-package-index-",
        suffix=".json",
        dir=path.parent,
    )
    os.close(output_fd)
    output_path = Path(output_name)
    limits = {
        "max_uncompressed_bytes": MAX_UNCOMPRESSED_BYTES,
        "max_file_bytes": MAX_FILE_BYTES,
        "max_manifest_bytes": MAX_MANIFEST_BYTES,
        "max_files": MAX_FILES,
        "max_entries": MAX_ENTRIES,
        "max_path_bytes": MAX_PATH_BYTES,
        "worker_memory_bytes": WORKER_MEMORY_BYTES,
        "worker_cpu_seconds": TASK_TIMEOUT_SECONDS + 5,
    }
    # Run only this interpreter and a fixed worker module; archive paths are argv data.
    # No shell expansion is allowed, including for spaces/metacharacters in paths.
    process = subprocess.Popen(  # NOSONAR(S4721): fixed executable/module, shell=False.  # noqa: S603
        [
            sys.executable,
            "-m",
            "app.services.package_compare_worker",
            str(path),
            str(output_path),
            json.dumps(limits),
        ],
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        cwd=Path(__file__).resolve().parents[2],
    )
    try:
        while process.poll() is None:
            try:
                _checkpoint()
            except WorkspaceError as error:
                if error.code == "compare_timeout":
                    raise WorkspaceError(
                        "知识包解析超时，请缩小知识包后重试",
                        408,
                        "compare_archive_timeout",
                    ) from error
                raise
            time.sleep(0.02)
        if not output_path.is_file() or not output_path.stat().st_size:
            raise WorkspaceError("知识包解析进程异常退出", 400, "compare_archive_worker_failed")
        response = _read_json(output_path)
        if response.get("ok"):
            return response["index"]
        error = response.get("error", {})
        raise WorkspaceError(
            error.get("message", "知识包解析失败"),
            int(error.get("status", 400)),
            error.get("code", "compare_archive_invalid"),
        )
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        output_path.unlink(missing_ok=True)


def _archive_meta(path: Path, index: dict) -> dict:
    manifest = index.pop("manifest")
    return {**index, "package_name": manifest["name"], "package_version": manifest["version"]}


def _source_meta(path: Path, source: dict, owner: str) -> dict:
    index = _archive_meta(path, _archive_index(path))
    return {"source": source, "owner": owner, "archive": path.name, "created_at": time.time(),
            "package_digest": _digest(path), **index}


def create_input(subject: str, upload, filename: str = "package.zip", project_id: str = "",
                 request_id: str = "") -> dict:
    _ensure_service_available()
    if request_id:
        _safe_id(request_id)
    input_id = "cmpin_" + uuid.uuid4().hex
    folder = root(subject) / input_id
    archive = folder / "package.zip"
    upload_key = request_id or input_id
    previous_deadline = getattr(_ACTIVE_TASK, "deadline", None)
    previous_upload = getattr(_ACTIVE_TASK, "upload", None)
    _ACTIVE_TASK.deadline = time.monotonic() + TASK_TIMEOUT_SECONDS
    acquired = _UPLOAD_LIMIT.acquire(blocking=False)
    if not acquired:
        _ACTIVE_TASK.deadline = previous_deadline
        raise WorkspaceError(
            "知识包正在上传校验的任务较多，请稍后重试", 429, "compare_upload_busy",
        )
    reserved = False
    try:
        with _TASK_LOCK:
            if (subject, upload_key) in _ACTIVE_UPLOADS:
                raise WorkspaceError(
                    "相同标识的知识包上传仍在处理中，请勿重复提交",
                    409,
                    "compare_upload_in_progress",
                )
            _ACTIVE_UPLOADS[(subject, upload_key)] = input_id
        if request_id:
            _ACTIVE_TASK.upload = (subject, request_id)
            _ensure_upload_active(subject, request_id)
        _reserve_upload(subject)
        reserved = True
        _copy_upload(subject, upload, archive, request_id)
        meta = _source_meta(archive, {"kind": "upload", "input_id": input_id, "filename": filename}, subject)
        if request_id:
            _ensure_upload_active(subject, request_id)
        meta["project_id"] = project_id
        _write_input_meta(subject, folder / "meta.json", meta)
        public_keys = (
            "package_id",
            "package_name",
            "package_version",
            "package_digest",
            "file_count",
            "uncompressed_size",
            "created_at",
        )
        return {
            "input_id": input_id,
            **{key: meta[key] for key in public_keys if key in meta},
            "content_digest": meta.get("package_digest", ""),
            "warnings": [],
            "expires_at": meta["created_at"] + INPUT_TTL,
        }
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    finally:
        if reserved:
            _release_upload_reservation(subject)
        with _TASK_LOCK:
            if _ACTIVE_UPLOADS.get((subject, upload_key)) == input_id:
                _ACTIVE_UPLOADS.pop((subject, upload_key), None)
            if request_id and (subject, upload_key) not in _ACTIVE_UPLOADS:
                _CANCELLED_UPLOADS.pop((subject, request_id), None)
        _UPLOAD_LIMIT.release()
        _ACTIVE_TASK.upload = previous_upload
        _ACTIVE_TASK.deadline = previous_deadline


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _input_is_referenced(subject: str, input_id: str) -> bool:
    return _input_is_referenced_in(root(subject), input_id)


def _input_is_referenced_in(user_root: Path, input_id: str) -> bool:
    for state_path in user_root.glob("cmp_*/*.json"):
        if state_path.name not in {"job.json", "result.json"}:
            continue
        try:
            state = _read_json(state_path)
        except WorkspaceError:
            continue
        refs = state.get("input_refs", {})
        sources = [refs.get(side, {}) for side in ("left", "right")]
        sources.extend(state.get(side, {}).get("source", {}) for side in ("left", "right"))
        if any(
            source.get("kind") == "upload" and source.get("input_id") == input_id
            for source in sources
        ):
            return True
    return False


def _load_input(subject: str, input_id: str, project_id: str = "") -> tuple[Path, dict]:
    input_id = _safe_id(input_id)
    folder = root(subject) / input_id
    if not (folder / "meta.json").is_file():
        raise WorkspaceError("无权访问该比较输入", 403)
    meta = _read_json(folder / "meta.json")
    if meta.get("source", {}).get("kind") != "upload" or meta.get("owner") != subject:
        raise WorkspaceError("无权访问该比较输入", 403)
    if meta.get("project_id") and meta.get("project_id") != project_id:
        raise WorkspaceError("无权访问该比较输入", 403)
    if time.time() > float(meta.get("created_at", 0)) + INPUT_TTL:
        with _TASK_LOCK:
            if not _input_is_referenced(subject, input_id):
                shutil.rmtree(folder, ignore_errors=True)
                raise WorkspaceError("比较输入已过期，请重新上传", 410)
    archive = folder / "package.zip"
    if not archive.is_file():
        raise WorkspaceError("比较输入已失效", 410)
    return archive, meta


def _validate_upload_refs(
    subject: str,
    project_id: str,
    left_ref: dict,
    right_ref: dict,
) -> None:
    for ref in (left_ref, right_ref):
        if ref.get("kind") == "upload":
            _load_input(subject, ref["input_id"], project_id)


def _resolve_release_ref(project_id: str, subject: str, ref: dict) -> dict:
    if ref.get("kind") != "release" or ref.get("ontology_id"):
        return ref
    association = hub_packages.association(project_id, subject) or {}
    package_id = association.get("ontology_id") or association.get("hub_package_id")
    if not package_id:
        raise WorkspaceError("发布版本缺少 Hub 包或 release 标识", 400)
    return {**ref, "ontology_id": _safe_id(package_id)}


def _release_source(project_id: str, subject: str, token: str, ref: dict) -> tuple[Path, dict, Path | None]:
    association = hub_packages.association(project_id, subject) or {}
    package_id = (
        ref.get("ontology_id")
        or ref.get("hub_package_id")
        or association.get("ontology_id")
        or association.get("hub_package_id")
    )
    release_id = ref.get("release_id", "")
    if not package_id or not release_id:
        raise WorkspaceError("发布版本缺少 Hub 包或 release 标识", 400)
    package_id = _safe_id(package_id)
    release_id = _safe_id(release_id)
    folder = Path(tempfile.mkdtemp(prefix="onto-package-compare-", dir=root(subject)))
    archive = folder / "package.zip"
    try:
        download = hub_packages.download
        signature = inspect.signature(download)
        parameters = signature.parameters.values()
        supports_keywords = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
        if supports_keywords or "checkpoint" in signature.parameters:
            deadline = getattr(_ACTIVE_TASK, "deadline", None)
            timeout = max(0.001, deadline - time.monotonic()) if deadline else None
            options = {}
            if supports_keywords or "checkpoint" in signature.parameters:
                options["checkpoint"] = _checkpoint
            if supports_keywords or "timeout" in signature.parameters:
                options["timeout"] = timeout
            if supports_keywords or "deadline" in signature.parameters:
                options["deadline"] = deadline
            receipt = download(
                package_id,
                release_id,
                token,
                archive,
                **options,
            )
        else:
            receipt = download(package_id, release_id, token, archive)
        _checkpoint()
        if archive.stat().st_size > MAX_ARCHIVE_BYTES:
            raise WorkspaceError("发布知识包超过版本对比大小限制", 413)
        source = {
            "kind": "release",
            "ontology_id": package_id,
            "release_id": release_id,
        }
        meta = _source_meta(archive, source, subject)
        meta.update({"version": receipt.get("version", ""),
                     "package_id": receipt.get("manifest_id") or meta.get("package_id", ""),
                     "package_digest": receipt.get("package_digest", _digest(archive)),
                     "committed_at": receipt.get("committed_at", "")})
        return archive, meta, folder
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise


def _materialize(project_id: str, subject: str, token: str, ref: dict,
                 comparison_id: str = "") -> tuple[Path, dict, Path | None]:
    kind = ref.get("kind")
    if kind == "upload":
        archive, meta = _load_input(subject, ref.get("input_id", ""), project_id)
        return archive, meta, None
    if kind == "release":
        return _release_source(project_id, subject, token, ref)
    if kind == "frozen":
        return _load_frozen(subject, project_id, comparison_id, ref)
    raise WorkspaceError("比较来源必须是 release、upload 或 frozen", 400)

def _summary(left: dict, right: dict) -> tuple[dict, list[dict]]:
    left_files, right_files = left["files"], right["files"]
    rows = []
    counts = {key: 0 for key in ("added", "deleted", "modified", "unchanged")}
    categories: dict[str, dict[str, int]] = {}
    for path in sorted(set(left_files) | set(right_files)):
        _checkpoint()
        before, after = left_files.get(path), right_files.get(path)
        if before is None:
            status = "added"
        elif after is None:
            status = "deleted"
        elif before["digest"] == after["digest"]:
            status = "unchanged"
        else:
            status = "modified"
        counts[status] += 1
        category = (after or before)["category"]
        bucket = categories.setdefault(category, {key: 0 for key in counts})
        bucket[status] += 1
        rows.append({"path": path, "category": category, "status": status,
                     "display_name": (after or before).get("display_name", path.rsplit("/", 1)[-1]),
                     "left": before, "right": after})
    affected = [category for category, values in categories.items()
                if values["added"] or values["deleted"] or values["modified"]]
    evidence = [
        {"path": row["path"], "display_name": row["display_name"], "status": row["status"]}
        for row in rows if row["status"] != "unchanged"
    ][:4]
    return {"files": counts, "categories": categories, "affected_categories": affected,
            "evidence": evidence, "left_file_count": len(left_files),
            "right_file_count": len(right_files)}, rows


def _comparison_folder(subject: str, comparison_id: str) -> Path:
    return root(subject) / _safe_id(comparison_id)


def _task_key(subject: str, comparison_id: str) -> tuple[str, str]:
    return subject, comparison_id


def _ensure_active(subject: str, comparison_id: str) -> None:
    with _TASK_LOCK:
        if _task_key(subject, comparison_id) in _CANCELLED:
            raise WorkspaceError("比较任务已取消", 410)


def _write_task_json(subject: str, comparison_id: str, path: Path, value: dict) -> None:
    """Write worker state only while cancellation cannot interleave with the write."""
    with _TASK_LOCK:
        _ensure_active(subject, comparison_id)
        if path.name == "result.json":
            _write_result_json(subject, path, value)
        else:
            _write_json(path, value)


def create_comparison(
    project_id: str,
    subject: str,
    token: str,
    left_ref: dict,
    right_ref: dict,
    allow_cross_package: bool = False,
    comparison_id: str | None = None,
) -> dict:
    left_ref = _source_ref(left_ref, allow_frozen=True)
    right_ref = _source_ref(right_ref, allow_frozen=True)
    if left_ref == right_ref:
        raise WorkspaceError("两侧比较来源不能相同", 400)
    owns_folder = comparison_id is None
    comparison_id = comparison_id or "cmp_" + uuid.uuid4().hex
    folder = _comparison_folder(subject, comparison_id)
    folder.mkdir(parents=True, exist_ok=bool(comparison_id))
    temp_folders: list[Path] = []
    try:
        _ensure_active(subject, comparison_id)
        left_path, left, temp = _materialize(
            project_id, subject, token, left_ref, comparison_id,
        )
        if temp:
            temp_folders.append(temp)
        _ensure_active(subject, comparison_id)
        right_path, right, temp = _materialize(
            project_id, subject, token, right_ref, comparison_id,
        )
        if temp:
            temp_folders.append(temp)
        left_package = left.get("package_id")
        right_package = right.get("package_id")
        different_packages = left_package and right_package and left_package != right_package
        if not allow_cross_package and different_packages:
            raise WorkspaceError("两侧属于不同 Hub 知识包，请确认跨包对比", 409)
        fingerprint = hashlib.sha256(json.dumps([
            left.get("source"),
            left.get("package_digest"),
            right.get("source"),
            right.get("package_digest"),
        ], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        for existing in root(subject).glob("cmp_*/result.json"):
            try:
                candidate = _read_json(existing)
                # Frozen releases live below their original comparison folder. Reusing
                # such a result would bind the new result to ZIP files owned by the old
                # task, so only cache comparisons backed by stable upload references.
                stable_refs = candidate.get("input_refs", {})
                stable_uploads = all(
                    stable_refs.get(side, {}).get("kind") == "upload"
                    for side in ("left", "right")
                )
                cache_valid = (
                    candidate.get("project_id") == project_id
                    and candidate.get("source_fingerprint") == fingerprint
                    and stable_uploads
                    and candidate.get("created_at", 0) + COMPARISON_TTL > time.time()
                )
                if cache_valid:
                    cached = {**candidate, "comparison_id": comparison_id, "created_at": time.time(),
                              "input_refs": {"left": left_ref, "right": right_ref}}
                    _write_task_json(subject, comparison_id, folder / "result.json", cached)
                    return _public(cached)
            except (WorkspaceError, ValueError, KeyError):
                continue
        summary, rows = _summary(left, right)
        result = {"comparison_id": comparison_id, "status": "ready", "created_at": time.time(),
                  "project_id": project_id, "owner": subject,
                  "source_fingerprint": fingerprint,
                  "input_refs": {"left": left_ref, "right": right_ref},
                  "left": left, "right": right, "summary": summary, "files": rows}
        _write_task_json(subject, comparison_id, folder / "result.json", result)
        return _public(result)
    finally:
        for temp in temp_folders:
            shutil.rmtree(temp, ignore_errors=True)
        if owns_folder and not (folder / "result.json").exists():
            shutil.rmtree(folder, ignore_errors=True)


def _freeze_ref(project_id: str, subject: str, token: str, comparison_id: str,
                side: str, ref: dict) -> dict:
    if ref.get("kind") != "release":
        return ref
    reserved = False
    temporary = None
    destination = None
    meta_path = None
    try:
        _reserve_upload(subject)
        reserved = True
        path, meta, temporary = _release_source(project_id, subject, token, ref)
        destination = _comparison_folder(subject, comparison_id) / f"{side}.zip"
        meta_path = destination.with_suffix(".json")
        shutil.move(str(path), destination)
        meta["project_id"] = project_id
        _write_input_meta(subject, meta_path, meta)
        return {"kind": "frozen", "side": side}
    except Exception:
        if meta_path is not None:
            meta_path.unlink(missing_ok=True)
        if destination is not None:
            destination.unlink(missing_ok=True)
        raise
    finally:
        if temporary:
            shutil.rmtree(temporary, ignore_errors=True)
        if reserved:
            _release_upload_reservation(subject)


def cancel_upload(subject: str, request_id: str) -> None:
    request_id = _safe_id(request_id)
    with _TASK_LOCK:
        _CANCELLED_UPLOADS[(subject, request_id)] = time.time()


def _load_frozen(subject: str, project_id: str, comparison_id: str, ref: dict) -> tuple[Path, dict, None]:
    side = ref.get("side")
    if side not in {"left", "right"}:
        raise WorkspaceError("冻结比较来源无效", 400)
    folder = _comparison_folder(subject, comparison_id)
    path, meta_path = folder / f"{side}.zip", folder / f"{side}.json"
    if not path.is_file() or not meta_path.is_file():
        raise WorkspaceError("冻结比较来源已失效", 410)
    meta = _read_json(meta_path)
    if meta.get("owner") != subject or meta.get("project_id") != project_id:
        raise WorkspaceError("无权访问冻结比较来源", 403)
    return path, meta, None


def _ensure_frozen_readable(meta: dict, token: str) -> None:
    source = meta.get("source", {})
    if source.get("kind") != "release":
        return
    if not token:
        raise WorkspaceError("Hub 需要登录用户身份", 401)
    package_id = source.get("ontology_id") or source.get("hub_package_id", "")
    hub_packages.call("GET", hub_packages.reference_path(
        package_id, source.get("release_id", "")), token)


def _ensure_release_ref_readable(source: dict, token: str) -> None:
    if not token:
        raise WorkspaceError("Hub 需要登录用户身份", 401)
    package_id = source.get("ontology_id") or source.get("hub_package_id", "")
    hub_packages.call(
        "GET",
        hub_packages.reference_path(
            package_id,
            source.get("release_id", ""),
        ),
        token,
    )

def _run_job(project_id: str, subject: str, token: str, comparison_id: str,
             left_ref: dict, right_ref: dict, allow_cross_package: bool) -> None:
    try:
        _ACTIVE_TASK.key = (subject, comparison_id)
        _ACTIVE_TASK.deadline = time.monotonic() + TASK_TIMEOUT_SECONDS
        _ensure_active(subject, comparison_id)
        folder = _comparison_folder(subject, comparison_id)
        job_path = folder / "job.json"
        if job_path.exists():
            job = _read_json(job_path)
            job.update(status="running", phase="reading")
            _write_task_json(subject, comparison_id, job_path, job)
        create_comparison(project_id, subject, token, left_ref, right_ref,
                          allow_cross_package, comparison_id=comparison_id)
        with _TASK_LOCK:
            _ensure_active(subject, comparison_id)
            job_path.unlink(missing_ok=True)
    except Exception as exc:
        folder = _comparison_folder(subject, comparison_id)
        try:
            with _TASK_LOCK:
                _ensure_active(subject, comparison_id)
                folder.mkdir(parents=True, exist_ok=True)
                _write_json(folder / "job.json", {
                    "comparison_id": comparison_id, "project_id": project_id, "owner": subject,
                    "status": "failed", "phase": "failed", "error": str(exc), "created_at": time.time(),
                })
        except WorkspaceError as cancelled:
            if cancelled.status != 410:
                raise
    finally:
        _ACTIVE_TASK.key = None
        _ACTIVE_TASK.deadline = None


def _task_finished(key: tuple[str, str], future) -> None:
    with _TASK_LOCK:
        if _RUNNING.get(key) is future:
            _RUNNING.pop(key, None)
        _DEADLINES.pop(key, None)
        _CANCELLED.discard(key)
        if key in _RESULT_RESERVATIONS:
            _RESULT_RESERVATIONS.discard(key)
            _release_storage(key[0], RESULT_RESERVATION_BYTES)


def _claim_task_slot(key: tuple[str, str]) -> None:
    subject, _comparison_id = key
    accepted_limit = MAX_WORKERS + MAX_QUEUED_TASKS
    with _TASK_LOCK:
        if sum(1 for owner, _ in _RUNNING if owner == subject) >= MAX_USER_PENDING_TASKS:
            raise WorkspaceError(
                "当前用户的知识包比较任务较多，请等待已有任务完成后重试",
                429,
                "compare_user_busy",
            )
        if len(_RUNNING) >= accepted_limit:
            raise WorkspaceError(
                "知识包比较任务队列已满，请稍后重试",
                429,
                "compare_busy",
            )
        _reserve_storage(subject, RESULT_RESERVATION_BYTES)
        _RESULT_RESERVATIONS.add(key)
        _RUNNING[key] = None


def _release_task_slot(key: tuple[str, str]) -> None:
    with _TASK_LOCK:
        _RUNNING.pop(key, None)
        _DEADLINES.pop(key, None)
        _CANCELLED.discard(key)
        if key in _RESULT_RESERVATIONS:
            _RESULT_RESERVATIONS.discard(key)
            _release_storage(key[0], RESULT_RESERVATION_BYTES)


def start_comparison(
    project_id: str,
    subject: str,
    token: str,
    left_ref: dict,
    right_ref: dict,
    allow_cross_package: bool = False,
    request_id: str = "",
) -> dict:
    _ensure_service_available()
    left_ref = _resolve_release_ref(project_id, subject, _source_ref(left_ref))
    right_ref = _resolve_release_ref(project_id, subject, _source_ref(right_ref))

    def identity(ref: dict) -> tuple:
        kind = ref.get("kind")
        if kind == "upload":
            return kind, ref.get("input_id", "")
        if kind == "release":
            association = hub_packages.association(project_id, subject) or {}
            package_id = (
                ref.get("ontology_id")
                or ref.get("hub_package_id")
                or association.get("ontology_id")
                or association.get("hub_package_id", "")
            )
            return kind, package_id, ref.get("release_id", "")
        return kind, json.dumps(ref, sort_keys=True, ensure_ascii=False)

    if identity(left_ref) == identity(right_ref):
        raise WorkspaceError("两侧比较来源不能相同", 400)
    request_path = None
    request_fingerprint = ""
    if request_id:
        _safe_id(request_id)
        request_key = hashlib.sha256(f"{project_id}:{request_id}".encode()).hexdigest()
        request_path = root(subject) / "requests" / f"{request_key}.json"
        request_fingerprint = hashlib.sha256(
            json.dumps(
                [identity(left_ref), identity(right_ref), bool(allow_cross_package)],
                ensure_ascii=False,
                sort_keys=True,
            ).encode()
        ).hexdigest()

    replay_comparison_id = ""
    with _TASK_LOCK:
        if request_path:
            if request_path.exists():
                record = _read_json(request_path)
                if record.get("fingerprint") != request_fingerprint:
                    raise WorkspaceError(
                        "比较请求标识已绑定其他来源",
                        409,
                        "compare_request_conflict",
                    )
                comparison_id = record.get("comparison_id", "")
                folder = _comparison_folder(subject, comparison_id)
                if (folder / "job.json").exists() or (folder / "result.json").exists():
                    replay_comparison_id = comparison_id
                else:
                    # A stale record may remain after a process crash. Active creation always
                    # writes the task state before publishing the request record below.
                    request_path.unlink(missing_ok=True)
        if not replay_comparison_id:
            _validate_upload_refs(subject, project_id, left_ref, right_ref)
            comparison_id = "cmp_" + uuid.uuid4().hex
            key = _task_key(subject, comparison_id)
            folder = _comparison_folder(subject, comparison_id)
            folder.mkdir(parents=True, exist_ok=False)
            try:
                _claim_task_slot(key)
                _write_json(
                    folder / "job.json",
                    {
                        "comparison_id": comparison_id,
                        "project_id": project_id,
                        "owner": subject,
                        "status": "queued",
                        "phase": "freezing",
                        "created_at": time.time(),
                        "input_refs": {"left": left_ref, "right": right_ref},
                        "request_id": request_id,
                    },
                )
                if request_path:
                    request_path.parent.mkdir(parents=True, exist_ok=True)
                    _write_json(
                        request_path,
                        {
                            "comparison_id": comparison_id,
                            "fingerprint": request_fingerprint,
                            "project_id": project_id,
                            "created_at": time.time(),
                        },
                    )
            except Exception:
                _release_task_slot(key)
                shutil.rmtree(folder, ignore_errors=True)
                raise

    if replay_comparison_id:
        ensure_sources_readable(subject, project_id, token, replay_comparison_id)
        with _TASK_LOCK:
            if not request_path or not request_path.exists():
                raise WorkspaceError("比较请求状态已变化，请重试", 409, "compare_request_changed")
            current_record = _read_json(request_path)
            same_request = current_record.get("fingerprint") == request_fingerprint
            same_comparison = current_record.get("comparison_id") == replay_comparison_id
            if not same_request or not same_comparison:
                raise WorkspaceError("比较请求状态已变化，请重试", 409, "compare_request_changed")
            return get_comparison(subject, project_id, replay_comparison_id)

    previous_key = getattr(_ACTIVE_TASK, "key", None)
    previous_deadline = getattr(_ACTIVE_TASK, "deadline", None)
    _ACTIVE_TASK.key = key
    _ACTIVE_TASK.deadline = time.monotonic() + TASK_TIMEOUT_SECONDS
    try:
        left_ref = _freeze_ref(project_id, subject, token, comparison_id, "left", left_ref)
        right_ref = _freeze_ref(project_id, subject, token, comparison_id, "right", right_ref)
    except Exception:
        with _TASK_LOCK:
            _release_task_slot(key)
            if request_path and request_path.exists():
                record = _read_json(request_path)
                if record.get("comparison_id") == comparison_id:
                    request_path.unlink(missing_ok=True)
        shutil.rmtree(folder, ignore_errors=True)
        raise
    finally:
        _ACTIVE_TASK.key = previous_key
        _ACTIVE_TASK.deadline = previous_deadline
    try:
        with _TASK_LOCK:
            _ensure_active(subject, comparison_id)
            future = _EXECUTOR.submit(
                _run_job,
                project_id,
                subject,
                "",
                comparison_id,
                left_ref,
                right_ref,
                allow_cross_package,
            )
            _RUNNING[key] = future
            future.add_done_callback(lambda completed: _task_finished(key, completed))
    except Exception:
        with _TASK_LOCK:
            _release_task_slot(key)
            if request_path and request_path.exists():
                record = _read_json(request_path)
                if record.get("comparison_id") == comparison_id:
                    request_path.unlink(missing_ok=True)
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return {
        "comparison_id": comparison_id,
        "status": "queued",
        "phase": "queued",
        "poll_after_ms": 500,
    }

def _public(result: dict) -> dict:
    value = {
        key: result[key]
        for key in ("comparison_id", "status", "created_at", "summary", "left", "right")
    }
    source_keys = (
        "source",
        "package_name",
        "package_version",
        "package_digest",
        "file_count",
        "uncompressed_size",
        "version",
        "committed_at",
    )
    for side in ("left", "right"):
        value[side] = {key: result[side].get(key, "") for key in source_keys}
    return value


def _owned_result(subject: str, project_id: str, comparison_id: str) -> dict:
    key = _task_key(subject, comparison_id)
    with _TASK_LOCK:
        if key in _CANCELLED:
            raise WorkspaceError("比较结果已损坏或已过期", 410)
        folder = _comparison_folder(subject, comparison_id)
        result_path = folder / "result.json"
        job_path = folder / "job.json"
        if result_path.exists():
            state_path = result_path
        elif job_path.exists():
            state_path = job_path
        else:
            raise WorkspaceError("比较结果已损坏或已过期", 410)
        result = _read_json(state_path)
        if result.get("owner") != subject or result.get("project_id") != project_id:
            raise WorkspaceError("无权访问该比较结果", 403)
        last_accessed_at = result.get("last_accessed_at", result.get("created_at", 0))
        if time.time() > float(last_accessed_at) + COMPARISON_TTL:
            delete(subject, comparison_id)
            raise WorkspaceError("比较结果已过期", 410)
        if result.get("status") in {"ready", "failed"}:
            result["last_accessed_at"] = time.time()
            _write_json(state_path, result)
        return result


def ensure_sources_readable(subject: str, project_id: str, token: str, comparison_id: str) -> None:
    result = _owned_result(subject, project_id, comparison_id)
    for side in ("left", "right"):
        side_meta = result.get(side, {})
        source = side_meta.get("source", {})
        if not source:
            source = result.get("input_refs", {}).get(side, {})
        if source.get("kind") == "release":
            if side_meta.get("source"):
                _ensure_frozen_readable(side_meta, token)
            else:
                _ensure_release_ref_readable(source, token)
        elif source.get("kind") == "frozen":
            _path, frozen_meta, _temp = _load_frozen(
                subject, project_id, comparison_id, source,
            )
            _ensure_frozen_readable(frozen_meta, token)


def get_comparison(subject: str, project_id: str, comparison_id: str) -> dict:
    result = _owned_result(subject, project_id, comparison_id)
    if result.get("status") != "ready":
        fields = ("comparison_id", "status", "phase", "error", "error_code")
        return {key: result[key] for key in fields if key in result}
    return _public(result)


def list_files(
    subject: str,
    project_id: str,
    comparison_id: str,
    status: str = "",
    category: str = "",
    query: str = "",
    offset: int = 0,
    limit: int = 50,
) -> dict:
    result = _owned_result(subject, project_id, comparison_id)
    if result.get("status") != "ready":
        raise WorkspaceError("比较尚未完成", 409)
    allowed_statuses = {"added", "deleted", "modified", "unchanged", "changed"}
    if status and status not in allowed_statuses:
        raise WorkspaceError("差异状态无效", 400)
    lowered = query.casefold()
    changed_statuses = {"added", "deleted", "modified"}

    def matches(row: dict) -> bool:
        status_matches = not status or (
            row["status"] in changed_statuses
            if status == "changed"
            else row["status"] == status
        )
        category_matches = not category or row["category"] == category
        searchable = (
            row["path"],
            row.get("display_name", ""),
            (row.get("left") or {}).get("display_name", ""),
            (row.get("right") or {}).get("display_name", ""),
        )
        query_matches = not query or any(
            lowered in str(value).casefold() for value in searchable
        )
        return status_matches and category_matches and query_matches

    rows = [row for row in result["files"] if matches(row)]
    return {
        "items": rows[offset:offset + limit],
        "total": len(rows),
        "filtered_total": len(rows),
        "overall_total": len(result["files"]),
        "offset": offset,
        "limit": limit,
    }


def _read_member(path: Path, member: str) -> bytes:
    _member_path(member)
    try:
        with zipfile.ZipFile(path) as archive:
            try:
                info = archive.getinfo(member)
            except KeyError as exc:
                raise WorkspaceError("比较文件不存在", 404) from exc
            if info.file_size > MAX_TEXT_BYTES:
                raise WorkspaceError("文件超过正文差异展示限制", 413)
            return archive.read(info)
    except (zipfile.BadZipFile, OSError) as exc:
        raise WorkspaceError("比较结果已被删除或已过期", 410) from exc


def file_diff(
    project_id: str,
    subject: str,
    token: str,
    comparison_id: str,
    file_id: str,
) -> dict:
    result = _owned_result(subject, project_id, comparison_id)
    if result.get("status") != "ready":
        raise WorkspaceError("比较尚未完成", 409)
    row = next((item for item in result["files"] if item["path"] == file_id), None)
    if row is None:
        raise WorkspaceError("比较文件不存在", 404)
    paths: list[Path] = []
    folders: list[Path] = []
    try:
        for side in ("left", "right"):
            source = result.get("input_refs", {}).get(side) or result[side]["source"]
            path, meta, temp = _materialize(project_id, subject, token, source, comparison_id)
            if source.get("kind") == "frozen":
                _ensure_frozen_readable(meta, token)
            paths.append(path)
            if temp:
                folders.append(temp)
        values = []
        for path, side in zip(paths, ("left", "right")):
            member = row[side]["path"] if row[side] else None
            if not member:
                values.append(None)
                continue
            size = int(row[side].get("size", 0))
            if size > MAX_TEXT_BYTES:
                values.append({"text": None, "encoding": "too_large", "bytes": size,
                               "reason": "文件超过正文差异展示限制"})
                continue
            raw = _read_member(path, member)
            try:
                encoding = "utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else (
                    "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8")
                text = raw.decode(encoding)
                lines = text.splitlines()
                if len(lines) > MAX_TEXT_LINES or any(len(line) > MAX_LINE_CHARS for line in lines):
                    values.append({
                        "text": None,
                        "encoding": "complex_text",
                        "bytes": len(raw),
                        "reason": "文件行数或单行长度超过正文差异展示限制",
                    })
                else:
                    values.append({
                        "text": text,
                        "encoding": encoding,
                        "bytes": len(raw),
                    })
            except UnicodeDecodeError:
                values.append({"text": None, "encoding": "binary", "bytes": len(raw)})
        return {
            "path": file_id,
            "status": row["status"],
            "category": row["category"],
            "left": values[0],
            "right": values[1],
        }
    finally:
        for folder in folders:
            shutil.rmtree(folder, ignore_errors=True)


def delete(subject: str, comparison_id: str) -> None:
    key = _task_key(subject, comparison_id)
    with _TASK_LOCK:
        _CANCELLED.add(key)
        future = _RUNNING.get(key)
        if future is not None:
            future.cancel()
        shutil.rmtree(_comparison_folder(subject, comparison_id), ignore_errors=True)
        if key not in _RUNNING or (future is not None and future.done()):
            _release_task_slot(key)
    for request_path in (root(subject) / "requests").glob("*.json"):
        try:
            if _read_json(request_path).get("comparison_id") != comparison_id:
                continue
        except WorkspaceError:
            continue
        with _TASK_LOCK:
            try:
                if _read_json(request_path).get("comparison_id") == comparison_id:
                    request_path.unlink(missing_ok=True)
            except WorkspaceError:
                request_path.unlink(missing_ok=True)


def delete_input(subject: str, project_id: str, input_id: str) -> None:
    input_id = _safe_id(input_id)
    with _TASK_LOCK:
        archive, _meta = _load_input(subject, input_id, project_id)
        if _input_is_referenced(subject, input_id):
            raise WorkspaceError("比较输入仍被任务使用，请先删除比较任务", 409)
        shutil.rmtree(archive.parent, ignore_errors=True)


def shutdown() -> None:
    global _SERVICE_LOCK, _SERVICE_AVAILABLE, _SERVICE_OWNER
    _EXECUTOR.shutdown(wait=False, cancel_futures=True)
    if _SERVICE_LOCK is not None:
        try:
            import fcntl
            fcntl.flock(_SERVICE_LOCK.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        _SERVICE_LOCK.close()
        _SERVICE_LOCK = None
    _SERVICE_AVAILABLE = False
    _SERVICE_OWNER = False


def maintain() -> None:
    if not _SERVICE_OWNER:
        return
    base = Path(config.DATA_DIR) / "package-comparisons"
    if not base.exists():
        return
    now = time.time()
    with _TASK_LOCK:
        for key, cancelled_at in list(_CANCELLED_UPLOADS.items()):
            if now - cancelled_at > 300:
                _CANCELLED_UPLOADS.pop(key, None)
    referenced_inputs: set[tuple[Path, str]] = set()
    for result_path in base.glob("*/cmp_*/result.json"):
        try:
            result = _read_json(result_path)
            if now > float(result.get("last_accessed_at", result.get("created_at", 0))) + COMPARISON_TTL:
                shutil.rmtree(result_path.parent, ignore_errors=True)
                continue
            for side in ("left", "right"):
                source = result.get(side, {}).get("source", {})
                if source.get("kind") == "upload" and source.get("input_id"):
                    referenced_inputs.add((result_path.parents[1], source["input_id"]))
        except WorkspaceError:
            shutil.rmtree(result_path.parent, ignore_errors=True)
    for job_path in base.glob("*/cmp_*/job.json"):
        try:
            job = _read_json(job_path)
            key = _task_key(job.get("owner", ""), job_path.parent.name)
            with _TASK_LOCK:
                current_process_owns_job = key in _RUNNING
            if (job.get("status") in {"queued", "running"}
                    and not current_process_owns_job):
                job.update(status="failed", phase="failed", error="服务已重启，请重新发起比较",
                           error_code="PROCESS_RESTARTED")
                _write_json(job_path, job)
            if now > float(job.get("last_accessed_at", job.get("created_at", 0))) + COMPARISON_TTL:
                delete(job.get("owner", ""), job_path.parent.name)
                continue
            for side in ("left", "right"):
                source = job.get("input_refs", {}).get(side, {})
                if source.get("kind") == "upload" and source.get("input_id"):
                    referenced_inputs.add((job_path.parents[1], source["input_id"]))
        except WorkspaceError:
            # The worker publishes result.json and then removes job.json. A cleanup
            # scan may retain the old job path across that transition, so coordinate
            # the second check with task completion before treating the folder as
            # corrupt.
            with _TASK_LOCK:
                result_path = job_path.parent / "result.json"
                if result_path.is_file():
                    result = _read_json(result_path)
                    for side in ("left", "right"):
                        source = result.get(side, {}).get("source", {})
                        if source.get("kind") == "upload" and source.get("input_id"):
                            referenced_inputs.add((result_path.parents[1], source["input_id"]))
                    continue
                if job_path.is_file():
                    try:
                        _read_json(job_path)
                    except WorkspaceError:
                        shutil.rmtree(job_path.parent, ignore_errors=True)
                else:
                    shutil.rmtree(job_path.parent, ignore_errors=True)
    for folder in base.glob("*/cmpin_*"):
        with _TASK_LOCK:
            active_inputs = {
                (base / _scope(subject), input_id)
                for (subject, _request_id), input_id in _ACTIVE_UPLOADS.items()
            }
            if (folder.parent, folder.name) in active_inputs:
                continue
            try:
                meta = _read_json(folder / "meta.json")
                is_expired = now > float(meta.get("created_at", 0)) + INPUT_TTL
                currently_referenced = _input_is_referenced_in(folder.parent, folder.name)
                if (is_expired
                        and (folder.parent, folder.name) not in referenced_inputs
                        and not currently_referenced):
                    shutil.rmtree(folder, ignore_errors=True)
            except (WorkspaceError, ValueError, KeyError):
                shutil.rmtree(folder, ignore_errors=True)
    for temporary in base.glob("*/onto-package-compare-*"):
        try:
            if now - temporary.stat().st_mtime > COMPARISON_TTL:
                shutil.rmtree(temporary, ignore_errors=True)
        except OSError:
            continue
    with _TASK_LOCK:
        for request_path in base.glob("*/requests/*.json"):
            try:
                record = _read_json(request_path)
                comparison = request_path.parents[1] / record.get("comparison_id", "invalid")
                if (now - float(record.get("created_at", 0)) > COMPARISON_TTL
                        or not comparison.exists()):
                    request_path.unlink(missing_ok=True)
            except (WorkspaceError, ValueError, KeyError):
                request_path.unlink(missing_ok=True)
