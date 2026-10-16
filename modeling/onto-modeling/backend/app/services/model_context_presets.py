"""模型上下文推荐表：按模型名查窗口与最大输出，供配置页预填。

只做推荐，不参与运行时取值（运行时仍是 配置页 > .env > 内置默认）。分两层：
- 内置表：同目录 model_context_presets.json，随版本发布，只收录厂商官方文档给出的数值；
  最大输出取官方默认值；思考与非思考默认值不同时取思考模式的，因为稳定引擎默认开启思考，
  思考内容同样占用最大输出。
- 自定义表：数据目录下 model-context-presets.json，管理员在配置页逐条编辑或由运维手改，
  同 id 整条替换内置条目、新 id 追加。每次查表前比对文件修改时间与大小，变了就重新加载，
  无需重启；文件有任何一处不合法时整份不生效，沿用上一次合法的结果，错误随推荐表接口返回。
  页面保存带上读取时的版本号（文件内容摘要），文件已被改过或有误时拒绝写入，避免与手改互相覆盖；
  写入前把上一版存为同目录 .prev（只留一份）。
启动时把内置表写到数据目录 model-context-presets.example.json 供复制修改，该文件本身不生效。
配置页可停用自定义表：改名为带时间的 .bak 备份，不删除；重新启用由运维把备份改回原名。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .. import config
from .model_settings import CONTEXT_FIELDS

_log = logging.getLogger(__name__)

_BUILTIN_FILE = Path(__file__).with_name("model_context_presets.json")
CUSTOM_FILE_NAME = "model-context-presets.json"
EXAMPLE_FILE_NAME = "model-context-presets.example.json"
# 停用时的备份名：自定义表名 + .bak-年月日-时分秒，按名字排序即按停用时间排序
_BACKUP_PREFIX = f"{CUSTOM_FILE_NAME}.bak-"
# 页面保存前的上一版，每次覆盖
PREVIOUS_FILE_NAME = f"{CUSTOM_FILE_NAME}.prev"

_EXAMPLE_README = [
    "本文件由服务每次启动时按内置推荐表重写，修改本文件不生效。",
    f"要修改或新增模型：在配置页「查看或编辑推荐表」中编辑（写入同目录的 {CUSTOM_FILE_NAME}），"
    "或把本文件复制为该文件再手工编辑；保存后下次查表即生效，无需重启。",
    "建议只保留要修改或新增的条目：同 id 的条目整条替换内置条目，未列出的仍按内置值，升级后内置表的修正才能生效。",
    "每条必填 id、max_context_tokens、max_output_tokens（十进制整数，最大输出须小于窗口）；"
    "vendor、aliases、note、sources 可选，sources 只填 https 链接。",
    "文件有任何一处不合法时整份不生效，错误显示在配置页「查看或编辑推荐表」中，改正或停用前页面不能编辑。",
    f"页面保存会重写该文件：顶层字段保留，条目按标准格式输出（手工排版与条目里不认识的字段不保留），"
    f"上一版存为 {PREVIOUS_FILE_NAME}。",
]

# 网关常在模型名后追加 6 或 8 位日期版本；YYYY-MM-DD 这类官方快照不剥，
# 同一模型的旧快照参数可能不同，只认表里列出的别名
_DATE_SUFFIX = re.compile(r"-(\d{8}|\d{6})$")

_TOKEN_FIELDS = ("max_context_tokens", "max_output_tokens")


class PresetFileError(ValueError):
    pass


class PresetConflictError(Exception):
    """页面保存时自定义表已被改过或有误，未写入。"""


def normalize_model_name(name: str) -> str:
    """小写、去掉 provider/ 前缀、把 . 和 _ 统一成 -、去掉网关日期后缀。"""
    name = (name or "").strip().lower().rsplit("/", 1)[-1]
    name = re.sub(r"[._]", "-", name)
    return _DATE_SUFFIX.sub("", name)


@lru_cache(maxsize=1)
def _builtin() -> dict:
    return json.loads(_BUILTIN_FILE.read_text(encoding="utf-8"))


def _custom_file() -> Path:
    return Path(config.DATA_DIR) / CUSTOM_FILE_NAME


def _token_field(raw: dict, name: str, where: str) -> int:
    _, low, high = CONTEXT_FIELDS[name]
    value = raw.get(name)
    # JSON 的 true/false 在 Python 里也是 int，要单独排除
    if not isinstance(value, int) or isinstance(value, bool):
        raise PresetFileError(f"{where}：{name} 必须是整数")
    if not low <= value <= high:
        raise PresetFileError(f"{where}：{name} 须在 {low}～{high} 之间")
    return value


def _text_field(raw: dict, name: str, where: str) -> str:
    value = raw.get(name)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise PresetFileError(f"{where}：{name} 必须是字符串")
    return value.strip()


def _text_list(raw: dict, name: str, where: str) -> list[str]:
    value = raw.get(name)
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise PresetFileError(f"{where}：{name} 必须是非空字符串组成的数组")
    return [item.strip() for item in value]


def _custom_entry(raw: object, position: int) -> dict:
    where = f"presets 第 {position} 条"
    if not isinstance(raw, dict):
        raise PresetFileError(f"{where}：必须是对象")
    preset_id = raw.get("id")
    if not isinstance(preset_id, str) or not normalize_model_name(preset_id):
        raise PresetFileError(f"{where}：id 必须是非空字符串")
    preset_id = preset_id.strip()
    where = f"{where}（{preset_id}）"
    window = _token_field(raw, "max_context_tokens", where)
    output = _token_field(raw, "max_output_tokens", where)
    if output >= window:
        raise PresetFileError(f"{where}：max_output_tokens 必须小于 max_context_tokens")
    aliases = _text_list(raw, "aliases", where)
    if not all(normalize_model_name(alias) for alias in aliases):
        raise PresetFileError(f"{where}：aliases 中有无效的模型名")
    sources = _text_list(raw, "sources", where)
    if not all(url.startswith("https://") for url in sources):
        raise PresetFileError(f"{where}：sources 只能填 https:// 开头的链接，其它说明写在 note")
    return {
        "id": preset_id,
        "vendor": _text_field(raw, "vendor", where),
        "aliases": aliases,
        "max_context_tokens": window,
        "max_output_tokens": output,
        "note": _text_field(raw, "note", where),
        "sources": sources,
    }


def _decode(content: bytes) -> str:
    try:
        return content.decode("utf-8-sig")  # 容忍 Windows 编辑器写入的 BOM
    except UnicodeDecodeError:
        raise PresetFileError("文件必须是 UTF-8 编码") from None


def _version(content: bytes | None) -> str:
    """自定义表的版本号：内容摘要，文件不存在时为空串。"""
    return hashlib.sha256(content).hexdigest()[:16] if content is not None else ""


def _parse_custom(text: str) -> tuple[dict, list[dict], str]:
    """自定义表 → (顶层对象, 条目, 核对日期)；未知的顶层字段（如复制来的 _readme）忽略，页面保存时原样保留。"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PresetFileError(f"不是合法的 JSON：第 {exc.lineno} 行第 {exc.colno} 列，{exc.msg}") from None
    if not isinstance(data, dict) or not isinstance(data.get("presets"), list):
        raise PresetFileError("顶层必须是对象，且包含 presets 数组")
    verified_at = data.get("verified_at") or ""
    if not isinstance(verified_at, str):
        raise PresetFileError("verified_at 必须是字符串，如 2026-10-15")
    entries = [_custom_entry(raw, position) for position, raw in enumerate(data["presets"], 1)]
    return data, entries, verified_at.strip()


def _merge(custom: list[dict]) -> list[dict]:
    """自定义条目按规范化 id 整条替换内置条目（保持内置表顺序），新 id 按文件顺序追加。"""
    overrides: dict[str, dict] = {}
    for entry in custom:
        key = normalize_model_name(entry["id"])
        if key in overrides:
            raise PresetFileError(f"id「{entry['id']}」与「{overrides[key]['id']}」重复")
        overrides[key] = entry
    merged = []
    for preset in _builtin()["presets"]:
        entry = overrides.pop(normalize_model_name(preset["id"]), None)
        if entry is None:
            merged.append({**preset, "origin": "builtin"})
            continue
        result = {**entry, "origin": "custom", "overrides_builtin": True}
        # 与内置值不同时带上内置值，升级后内置表修正了数值，配置页能看出被自定义挡住了
        builtin_values = {name: preset[name] for name in _TOKEN_FIELDS}
        if any(entry[name] != value for name, value in builtin_values.items()):
            result["builtin_values"] = builtin_values
        merged.append(result)
    merged.extend({**entry, "origin": "custom"} for entry in overrides.values())
    return merged


def _build_index(presets: list[dict]) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for preset in presets:
        for name in [preset["id"], *preset.get("aliases", [])]:
            key = normalize_model_name(name)
            owner = index.setdefault(key, preset)
            if owner is not preset:
                raise PresetFileError(f"「{name}」与 {owner['id']} 的 id 或别名规范化后重名（都是 {key}）")
    return index


@dataclass(frozen=True)
class _Snapshot:
    file_key: tuple | None  # 自定义表的 (路径, 修改时间, 大小)，文件不存在时后两项为 None
    presets: list[dict]
    index: dict[str, dict]
    custom: list[dict]  # 自定义表的条目，按文件顺序，供页面编辑
    custom_verified_at: str
    loaded_at: float | None  # 自定义表的加载时间；没有生效的自定义表时为 None
    version: str  # 加载时的文件内容摘要，见 _version


_lock = threading.Lock()
_snapshot: _Snapshot | None = None
_error: str | None = None  # 自定义表最近一次加载失败的原因，成功加载后清空


def _file_key(path: Path) -> tuple:
    try:
        stat = path.stat()
    except OSError:
        return (str(path), None, None)
    return (str(path), stat.st_mtime_ns, stat.st_size)


def _build(
    file_key: tuple | None, custom: list[dict], verified_at: str, loaded_at: float | None, version: str = ""
) -> _Snapshot:
    presets = _merge(custom)
    return _Snapshot(file_key, presets, _build_index(presets), custom, verified_at, loaded_at, version)


def _load(path: Path, file_key: tuple) -> _Snapshot:
    if file_key[1] is None:
        return _build(file_key, [], "", None)
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise PresetFileError(f"无法读取：{exc.strerror or exc}") from None
    _, custom, verified_at = _parse_custom(_decode(content))
    return _build(file_key, custom, verified_at, time.time(), _version(content))


def _current() -> tuple[_Snapshot, str | None]:
    global _snapshot, _error
    path = _custom_file()
    file_key = _file_key(path)
    with _lock:
        if _snapshot is not None and _snapshot.file_key == file_key:
            # 文件回到上次成功加载时的状态（如有误的文件被停用或删除），之前的错误不再适用
            _error = None
        else:
            try:
                _snapshot, _error = _load(path, file_key), None
            except PresetFileError as exc:
                if str(exc) != _error:
                    _log.warning("自定义模型上下文推荐表 %s 未生效：%s", path, exc)
                _error = str(exc)
                # 失败不记文件状态，下次查表重试（也避免读到写了一半的文件后一直卡住）
                if _snapshot is None:
                    _snapshot = _build(None, [], "", None)
        return _snapshot, _error


def _latest_backup(path: Path) -> Path | None:
    backups = sorted(path.parent.glob(f"{_BACKUP_PREFIX}*"))
    return backups[-1] if backups else None


def presets() -> dict:
    """合并后的推荐表：内置表核对日期与条目数、条目（origin 标来源）与自定义表状态。"""
    snapshot, error = _current()
    path = _custom_file()
    backup = _latest_backup(path)
    return {
        "verified_at": _builtin()["verified_at"],
        "builtin_count": len(_builtin()["presets"]),
        "presets": snapshot.presets,
        "custom": {
            "path": str(path),
            "example_path": str(path.with_name(EXAMPLE_FILE_NAME)),
            # 文件在但有误时 count 与 loaded_at 可能为空，停用按钮按 exists 显示
            "exists": path.is_file(),
            "count": len(snapshot.custom),
            "verified_at": snapshot.custom_verified_at,
            "loaded_at": snapshot.loaded_at,
            "error": error,
            "backup_path": str(backup) if backup else None,
            # 页面编辑用：有 error 时这两项属于上一次合法的文件，页面不允许编辑
            "entries": snapshot.custom,
            "version": snapshot.version,
        },
    }


def disable_custom() -> dict:
    """停用自定义表：同目录改名备份（不删除），下次查表即只用内置表；文件已不在时直接返回。

    返回推荐表，disabled_backup 为本次生成的备份路径（文件已不在时为 None）；改名失败（如数据目录只读）时抛出 OSError。
    """
    path = _custom_file()
    target = None
    if path.is_file():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = path.with_name(f"{_BACKUP_PREFIX}{stamp}")
        suffix = 1
        while target.exists():  # 同一秒内重复停用
            suffix += 1
            target = path.with_name(f"{_BACKUP_PREFIX}{stamp}-{suffix}")
        path.rename(target)
        _log.info("已停用自定义模型上下文推荐表 %s，备份为 %s", path, target)
    return {**presets(), "disabled_backup": str(target) if target else None}


_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _changed_ids(before: list[dict], after: list[dict]) -> tuple[list[str], list[str], list[str]]:
    """按规范化 id 对比两版条目 → (新增, 修改, 删除) 的 id。"""
    old = {normalize_model_name(entry["id"]): entry for entry in before}
    new = {normalize_model_name(entry["id"]): entry for entry in after}
    added = [entry["id"] for key, entry in new.items() if key not in old]
    changed = [entry["id"] for key, entry in new.items() if key in old and entry != old[key]]
    removed = [entry["id"] for key, entry in old.items() if key not in new]
    return added, changed, removed


def _atomic_write(path: Path, content: str, mode: int) -> None:
    """先写同目录临时文件再替换，热加载不会读到写了一半的文件。"""
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            fd = None
            file.write(content)
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
    finally:
        # 替换成功后临时文件已不在；中途失败时关闭句柄并删掉残留
        if fd is not None:
            os.close(fd)
        temporary_path.unlink(missing_ok=True)


def save_custom(entries: list, version: str, verified_at: str | None = None, user: str = "") -> dict:
    """页面保存自定义表：entries 是整份自定义条目，校验规则与手改文件相同。

    version 须等于读取推荐表时的 custom.version，文件已被改过时抛 PresetConflictError；
    文件有误时同样拒绝，须先改正或停用。顶层其它字段保留；verified_at 为 None 时沿用原值，
    空串表示去掉。条目不合法抛 PresetFileError，写文件失败抛 OSError。
    返回推荐表，previous_path 为本次保存的上一版（原先没有文件时为 None）。
    """
    custom = [_custom_entry(raw, position) for position, raw in enumerate(entries, 1)]
    _build_index(_merge(custom))
    if verified_at is not None:
        verified_at = verified_at.strip()
        if verified_at and not _DATE.fullmatch(verified_at):
            raise PresetFileError("核对日期须为 年-月-日 格式，如 2026-10-15")
    path = _custom_file()
    previous = path.with_name(PREVIOUS_FILE_NAME)
    with _lock:
        try:
            content = path.read_bytes()
        except FileNotFoundError:
            content = None
        if _version(content) != version:
            raise PresetConflictError("自定义表已被修改（运维改了文件，或其他管理员刚保存过），请按最新内容重新操作")
        document, before = {}, []
        if content is not None:
            try:
                document, before, current_verified_at = _parse_custom(_decode(content))
            except PresetFileError as exc:
                raise PresetConflictError(f"自定义表有误，请先改正或停用后再在页面编辑：{exc}") from None
            if verified_at is None:
                verified_at = current_verified_at
        # 其它顶层字段在前，核对日期与条目在后，与内置表的顺序一致
        document = {key: value for key, value in document.items() if key not in ("verified_at", "presets")}
        if verified_at:
            document["verified_at"] = verified_at
        document["presets"] = custom
        if content is not None:
            previous.write_bytes(content)
        mode = path.stat().st_mode & 0o777 if content is not None else 0o644
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, json.dumps(document, ensure_ascii=False, indent=2) + "\n", mode)
    added, changed, removed = _changed_ids(before, custom)
    _log.info(
        "%s 在页面修改了自定义模型上下文推荐表 %s：新增 %s，修改 %s，删除 %s",
        user or "未知用户",
        path,
        added or "无",
        changed or "无",
        removed or "无",
    )
    return {**presets(), "previous_path": str(previous) if content is not None else None}


def match(model: str) -> dict | None:
    """按规范化后的模型名精确匹配，不做前缀或模糊匹配；查不到返回 None。"""
    key = normalize_model_name(model)
    return _current()[0].index.get(key) if key else None


def write_example() -> None:
    """把内置表写到数据目录供复制修改；内容没变就不写，写不了只记日志、不影响启动。"""
    path = Path(config.DATA_DIR) / EXAMPLE_FILE_NAME
    content = json.dumps({"_readme": _EXAMPLE_README, **_builtin()}, ensure_ascii=False, indent=2) + "\n"
    try:
        if path.is_file() and path.read_text(encoding="utf-8") == content:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        _log.warning("写入模型上下文推荐表副本 %s 失败：%s", path, exc)
