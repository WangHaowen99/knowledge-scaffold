"""资料的文本版（ADR-0073）：后台排队、经连接器网关转换、走写入路径落盘、状态经项目流推给前端。

- 哪些要转：md、txt、csv 本身就是文本（native）；drawio 是流程图，另有工具（None）；其余资料（PDF、Word、Excel、
  图片、老版 Office、HTML）都算文档，有转换器的转，没有的「暂不支持」；
- 文本版在 `resource/text/<分组>/<原文件名>.md`，第一行记原文件修订与转换器（`convert.header`）；
- 状态：converting 转换中、converted 已转成文本、unsupported 暂不支持、failed 转换失败、waiting 等待转换（网关不可用）；
- 触发：写入事件里有资料变了（上传、替换、恢复版本）就排队；资料上「重新转换」；启动时补转还没有文本版的（升级迁移）；
  等待中的每 30 秒看一次网关，恢复了就补转。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from pathlib import Path

from ...evidence import convert
from ..workspace.layout import (
    RESOURCE_DIRECTIVES, RESOURCE_SUBDIRS, _content_revision, is_resource_text, resource_text_path,
)
from . import gateway, store

_log = logging.getLogger(__name__)

EVENT = "materials.text.updated"
NOT_DOCUMENT = (".drawio",)
IMAGE_TYPES = (".png", ".jpg", ".jpeg", ".webp")
LEGACY_OFFICE = (".doc", ".xls")
ACTIVE = ("converting", "waiting")
WORKERS = 2
WAIT_RETRY_S = 30
CONVERTERS_TTL_S = 30
AUTHOR = "多源连接器"


def _manager():
    from ...api import deps

    return deps.workspaces


# ---------------------------------------------------------------------------
# 分类与转换器
# ---------------------------------------------------------------------------


def suffix_of(rel: str) -> str:
    return Path(rel).suffix.lower()


def kind_of(rel: str) -> str | None:
    """native：本身是文本；document：要转（或暂不支持）；None：不是资料、或流程图。"""
    rel = (rel or "").strip("/")
    parts = rel.split("/")
    if len(parts) < 3 or parts[0] != "resource" or parts[1] not in RESOURCE_SUBDIRS or is_resource_text(rel):
        return None
    suffix = suffix_of(rel)
    if suffix in NOT_DOCUMENT:
        return None
    return "native" if suffix in convert.NATIVE_TEXT_TYPES else "document"


_converters_cache: tuple[float, int, list[dict]] | None = None
_converters_lock = threading.Lock()


def known_converters(force: bool = False) -> list[dict]:
    """平台装了哪些转换器：包仓库里 stable 版本带 `converter` 段的连接器包。

    [{id, label, version, types, license, official}]；版本去掉内容摘要（1.0.0+3f9a… → 1.0.0）。
    """
    global _converters_cache
    manager = _manager()
    key = id(manager)
    with _converters_lock:
        if not force and _converters_cache and _converters_cache[1] == key \
                and time.time() - _converters_cache[0] < CONVERTERS_TTL_S:
            return list(_converters_cache[2])
    repository = manager.packs.repository
    manager.packs.core_version()
    found = []
    for name in repository.names():
        version = repository.latest(name, "stable")
        if not version:
            continue
        manifest = repository.manifest(name, version)
        spec = manifest.raw.get("converter")
        if manifest.kind != "provider" or not isinstance(spec, dict):
            continue
        found.append({
            "id": name, "label": manifest.label, "version": version.split("+", 1)[0],
            "types": [str(item).lower() for item in spec.get("types") or []],
            "license": str(spec.get("license") or ""), "official": repository.is_builtin(name, version),
        })
    with _converters_lock:
        _converters_cache = (time.time(), key, found)
    return list(found)


def candidates(suffix: str) -> list[dict]:
    return [item for item in known_converters() if suffix in item["types"]]


def pick(suffix: str, loaded: set[str] | None = None) -> dict | None:
    """这种文件用哪个转换器：管理员指定的默认（还在）> 官方的 > 按 ID 排第一个；loaded 给了就只在网关已加载的里选。"""
    pool = [item for item in candidates(suffix) if loaded is None or item["id"] in loaded]
    if not pool:
        return None
    chosen = store.defaults().get(suffix)
    for item in pool:
        if item["id"] == chosen:
            return item
    return sorted(pool, key=lambda item: (not item["official"], item["id"]))[0]


def unsupported_reason(suffix: str) -> str:
    if suffix in IMAGE_TYPES:
        return "图片暂不支持"
    if suffix in LEGACY_OFFICE:
        return "老版 Office 格式暂不支持"
    return "这种格式暂不支持"


# ---------------------------------------------------------------------------
# 状态（契约 7.4 MaterialText）
# ---------------------------------------------------------------------------


# 文本版要带原件锚点的格式（ADR-0093）：老的文本版没有锚点时启动迁移补转一次
ANCHORED_SUFFIXES = (".docx", ".xlsx", ".xlsm")


def lacks_anchors(root: Path, rel: str) -> bool:
    """Word、Excel 的文本版还没有原件锚点（ADR-0093 之前转的）。"""
    if suffix_of(rel) not in ANCHORED_SUFFIXES:
        return False
    try:
        text = (Path(root) / resource_text_path(rel)).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return convert.ANCHORS_HEAD not in text


def text_header(root: Path, rel: str) -> dict | None:
    path = Path(root) / resource_text_path(rel)
    if not path.is_file() or path.is_symlink():
        return None
    try:
        with path.open("r", encoding="utf-8") as stream:
            return convert.parse_header(stream.readline())
    except (OSError, UnicodeDecodeError):
        return None


def _converter_view(converter_id: str | None, version: str | None) -> dict | None:
    if not converter_id:
        return None
    label = next((item["label"] for item in known_converters() if item["id"] == converter_id), converter_id)
    return {"id": converter_id, "label": label, "version": version or ""}


def view(project_id: str, root: Path, rel: str, data: bytes | None = None, *, heal: bool = True) -> dict | None:
    """一份资料的文本版状态；不是文档返回 None。记录对不上文件时按文件重判，缺文本版的顺手排队（heal）。"""
    kind = kind_of(rel)
    if kind is None:
        return None
    base = {"state": "native", "reason": None, "note": None, "converter": None, "updated_at": None, "has_text": True}
    if kind == "native":
        return base
    if data is None:
        try:
            data = (Path(root) / rel).read_bytes()
        except OSError:
            return None
    revision = _content_revision(data)
    head = text_header(root, rel)
    has_text = bool(head and head["sha"] == revision)
    entry = store.get(project_id, rel)
    if entry and (entry.get("sha") != revision or (entry.get("state") == "converted" and not has_text)):
        entry = None  # 记录对不上文件（原文件换了、文本版被恢复版本去掉了）：按文件重判
    out = {**base, "has_text": has_text}
    if entry and (entry.get("state") in ACTIVE + ("failed", "unsupported") or has_text):
        out.update(state=entry["state"], reason=entry.get("reason"), note=entry.get("note"),
                   converter=_converter_view(entry.get("converter"), entry.get("version")),
                   updated_at=entry.get("updated_at"))
        return out
    if has_text:
        converter, _, version = (head.get("converter") or "").partition("@")
        out.update(state="converted", converter=_converter_view(converter, version))
        return out
    if not candidates(suffix_of(rel)):
        out.update(state="unsupported", reason=unsupported_reason(suffix_of(rel)))
        return out
    out["state"] = "waiting" if not gateway.base_url() else "converting"
    if heal:
        enqueue(project_id, rel, requested="heal")
    return out


def unready(project_id: str, root: Path, paths: list[str] | None = None, *, heal: bool = False) -> list[dict]:
    """还没有可用文本版、或正在（重新）转换的文档资料：[{path, state, reason, has_text}]，按资料顺序。

    建模命令开始前、等转换时都要调：有文本版、记录也不在转换中的直接跳过，不逐份读原件算摘要；
    其余按 view 判（heal 时缺文本版又没排上的顺手排队）。paths 给定时只看这几份。
    """
    from ..research_view import material_paths

    records = store.items(project_id)
    found = []
    for rel in material_paths(root) if paths is None else paths:
        if kind_of(rel) != "document":
            continue
        if (records.get(rel) or {}).get("state") not in ACTIVE + ("failed", "unsupported") and text_header(root, rel):
            continue
        info = view(project_id, root, rel, heal=heal)
        if info is not None and (info["state"] in ACTIVE or not info["has_text"]):
            found.append({"path": rel, "state": info["state"], "reason": info.get("reason"),
                          "has_text": info["has_text"]})
    return found


def text_file(root: Path, rel: str) -> str | None:
    """文本版全文，含第一行文件头（行号和工作区里的文件一致，出处 `#L<n>` 按它数）；没有或对不上原文件时 None。"""
    path = Path(root) / resource_text_path(rel)
    try:
        text = path.read_text(encoding="utf-8")
        data = (Path(root) / rel).read_bytes()
    except (OSError, UnicodeDecodeError):
        return None
    head = convert.parse_header(text)
    if not head or head["sha"] != _content_revision(data):
        return None
    return text


def text_markdown(root: Path, rel: str) -> str | None:
    """文本版正文（去掉第一行文件头）；没有或对不上原文件时 None。"""
    text = text_file(root, rel)
    if text is None:
        return None
    return text.split("\n", 1)[1].lstrip("\n") if "\n" in text else ""


def _publish(project_id: str, rel: str) -> None:
    try:
        from .. import streams

        root = _manager().project_root(project_id)
        text = view(project_id, root, rel, heal=False)
        if text is not None:
            streams.publish_project(project_id, EVENT, {"path": rel, "text": text})
    except Exception:  # noqa: BLE001 - 事件是增强信息
        _log.exception("资料文本版事件发送失败")


def _record(project_id: str, rel: str, revision: str, state: str, **fields) -> None:
    store.put(project_id, rel, {"state": state, "sha": revision, "updated_at": time.time(),
                                **{key: value for key, value in fields.items() if value is not None}})
    with _waiting_lock:
        if state == "waiting":
            _waiting.add((project_id, rel))
        else:
            _waiting.discard((project_id, rel))
    _publish(project_id, rel)


# ---------------------------------------------------------------------------
# 转换一份
# ---------------------------------------------------------------------------


def _write_text(project_id: str, rel: str, revision: str, markdown: str) -> bool:
    """经写入路径落盘；原文件这期间被换掉了就不写（换上来的那份会另外排队）。内容没变不出新版本。"""
    from ...kernel.writepath import ChangeTx, Origin

    manager = _manager()
    target = resource_text_path(rel)
    payload = markdown.encode("utf-8")
    with ChangeTx(manager, project_id, Origin("plugin", AUTHOR), summary=f"转换资料 {rel}") as tx:
        source = tx.root / rel
        if not source.is_file() or _content_revision(source.read_bytes()) != revision:
            return False
        existing = tx.root / target
        if existing.is_file() and existing.read_bytes() == payload:
            return True
        existing.parent.mkdir(parents=True, exist_ok=True)
        tx.write(target, payload)
        # 标为数据 Schema 的 Excel：上传那刻还没有文本版，Sheet 范围在这里按结构算好、与文本版同一个提交存下（ADR-0096）
        if (tx.root / RESOURCE_DIRECTIVES).is_file():
            tx.touch(RESOURCE_DIRECTIVES)
            manager.record_schema_scope(tx.root, rel)
        # 文本变了也是资料变了：前端「资料已更新」提示按这个时间比
        manager._update_meta_unlocked(tx.root, resource_updated_at=int(time.time()))
        tx.commit(f"plugin: 转换资料 {rel}")
    return True


def process(project_id: str, rel: str, *, force: bool = False) -> str | None:
    """转换一份资料，返回最后的状态；资料已经不在了返回 None。同步执行（后台线程与测试都用它）。"""
    manager = _manager()
    try:
        root = manager.project_root(project_id)
    except Exception:  # noqa: BLE001 - 项目已删除
        return None
    path = root / rel
    if kind_of(rel) != "document" or not path.is_file() or path.is_symlink():
        store.drop(project_id, rel)
        return None
    data = path.read_bytes()
    revision = _content_revision(data)
    suffix = suffix_of(rel)
    head = text_header(root, rel)
    if not force and head and head["sha"] == revision:
        converter, _, version = (head.get("converter") or "").partition("@")
        _record(project_id, rel, revision, "converted", converter=converter or None, version=version or None)
        return "converted"
    if not candidates(suffix):
        _record(project_id, rel, revision, "unsupported", reason=unsupported_reason(suffix))
        return "unsupported"
    loaded = gateway.providers()
    choice = pick(suffix, {item.get("id") for item in loaded or [] if suffix in (item.get("types") or [])}) \
        if loaded is not None else None
    if choice is None:
        _record(project_id, rel, revision, "waiting")
        return "waiting"
    _record(project_id, rel, revision, "converting", converter=choice["id"], version=choice["version"])
    try:
        result = gateway.convert(data, path.name, choice["id"], f"cv-{uuid.uuid4().hex[:12]}")
    except gateway.GatewayUnavailable:
        _record(project_id, rel, revision, "waiting")
        return "waiting"
    used = result.get("converter") if isinstance(result.get("converter"), dict) else {}
    converter_id, version = used.get("id") or choice["id"], used.get("version") or choice["version"]
    state = result.get("state")
    if state == "converted":
        body = convert.strip_forged_headers(str(result.get("markdown") or "")).strip("\n")
        expected = [item.get("label") for item in result.get("locations") or [] if isinstance(item, dict)]
        if [item.label for item in convert.parse_locations(body)] != expected:
            _log.warning("转换器 %s 回的位置对照和文本里的标记对不上（%s），以文本为准", converter_id, rel)
        markdown = convert.header(path.name, revision, f"{converter_id}@{version}") + "\n\n" + body + "\n"
        if not _write_text(project_id, rel, revision, markdown):
            return None
        _record(project_id, rel, revision, "converted", converter=converter_id, version=version,
                note=result.get("note") or None)
        return "converted"
    if state == "unsupported":
        _record(project_id, rel, revision, "unsupported", converter=converter_id, version=version,
                reason=str(result.get("reason") or unsupported_reason(suffix)))
        return "unsupported"
    code = str(result.get("error_code") or "crashed")
    _record(project_id, rel, revision, "failed", converter=converter_id, version=version,
            reason=convert.FAILURES.get(code, convert.FAILURES["crashed"]))
    return "failed"


# ---------------------------------------------------------------------------
# 队列与后台线程
# ---------------------------------------------------------------------------

_queue: "queue.Queue[tuple[str, str, bool] | None]" = queue.Queue()
_pending: set[tuple[str, str]] = set()
_running: set[tuple[str, str]] = set()
_again: dict[tuple[str, str], bool] = {}
_pending_lock = threading.Lock()
_waiting: set[tuple[str, str]] = set()
_waiting_lock = threading.Lock()
_threads: list[threading.Thread] = []
_stop = threading.Event()


def enqueue(project_id: str, rel: str, *, force: bool = False, requested: str = "upload") -> bool:
    """排队转换；已经在排的不重复排，正在转的转完再来一次。返回是否新排进去。"""
    if kind_of(rel) != "document":
        return False
    key = (project_id, rel)
    with _pending_lock:
        if key in _running:
            _again[key] = _again.get(key, False) or force
            return False
        if key in _pending:
            return False
        _pending.add(key)
    _queue.put((project_id, rel, force))
    return True


def _take(timeout: float | None) -> tuple[str, str, bool] | None:
    item = _queue.get(timeout=timeout) if timeout is not None else _queue.get_nowait()
    if item is None:
        return None
    with _pending_lock:
        _pending.discard(item[:2])
        _running.add(item[:2])
    return item


def _run(item: tuple[str, str, bool]) -> None:
    project_id, rel, force = item
    key = (project_id, rel)
    try:
        process(project_id, rel, force=force)
    except Exception:  # noqa: BLE001 - 一份转坏了不能停掉整个队列
        _log.exception("转换资料 %s 出错", rel)
        try:
            data = (_manager().project_root(project_id) / rel).read_bytes()
            _record(project_id, rel, _content_revision(data), "failed", reason=convert.FAILURES["crashed"])
        except Exception:  # noqa: BLE001
            pass
    finally:
        with _pending_lock:
            _running.discard(key)
            again = _again.pop(key, None)
        if again is not None:
            enqueue(project_id, rel, force=again, requested="again")


def drain() -> int:
    """在当前线程里把队列转完（测试、没起后台线程时用）；返回转了几份。"""
    done = 0
    while True:
        try:
            item = _take(None)
        except queue.Empty:
            return done
        if item is None:
            continue
        _run(item)
        done += 1


def _worker() -> None:
    while not _stop.is_set():
        try:
            item = _take(1.0)
        except queue.Empty:
            continue
        if item is None:
            return
        _run(item)


def retry_waiting() -> int:
    """网关恢复了：把等待中的重新排队。"""
    with _waiting_lock:
        waiting = list(_waiting)
    if not waiting or gateway.health(force=True)["status"] != "ok":
        return 0
    return sum(1 for project_id, rel in waiting if enqueue(project_id, rel, requested="retry"))


def _watcher() -> None:
    while not _stop.wait(WAIT_RETRY_S):
        try:
            retry_waiting()
        except Exception:  # noqa: BLE001
            _log.exception("补转等待中的资料出错")


def start() -> None:
    """起后台线程（幂等）：转换线程 WORKERS 个，外加一个看网关恢复的。"""
    if any(thread.is_alive() for thread in _threads):
        return
    _stop.clear()
    _threads.clear()
    for index in range(WORKERS):
        _threads.append(threading.Thread(target=_worker, name=f"material-convert-{index}", daemon=True))
    _threads.append(threading.Thread(target=_watcher, name="material-convert-watch", daemon=True))
    for thread in _threads:
        thread.start()


def stop(timeout: float = 5) -> None:
    _stop.set()
    for _ in range(WORKERS):
        _queue.put(None)
    for thread in _threads:
        thread.join(timeout=timeout)
    _threads.clear()


def reset() -> None:
    """测试用：清空队列与内存状态。"""
    global _converters_cache
    while True:
        try:
            _queue.get_nowait()
        except queue.Empty:
            break
    with _pending_lock:
        _pending.clear()
        _running.clear()
        _again.clear()
    with _waiting_lock:
        _waiting.clear()
    with _converters_lock:
        _converters_cache = None
    gateway.reset()


# ---------------------------------------------------------------------------
# 写入事件、重新转换、启动补转
# ---------------------------------------------------------------------------


def on_write(event) -> None:
    """主线上资料变了（上传、替换、恢复版本、导入）：排队转换；文本版自己的提交不算。"""
    if getattr(event, "target", "mainline") != "mainline":
        return
    for rel in event.paths:
        if kind_of(rel) == "document":
            enqueue(event.project_id, rel)


def reconvert(project_id: str, rel: str) -> bool:
    """资料上的「重新转换」：用当前默认的转换器再转一次，转好了替换文本版。"""
    root = _manager().project_root(project_id)
    data = (root / rel).read_bytes()
    revision = _content_revision(data)
    if not candidates(suffix_of(rel)):
        return False
    store.put(project_id, rel, {"state": "converting", "sha": revision, "updated_at": time.time()})
    _publish(project_id, rel)
    enqueue(project_id, rel, force=True, requested="reconvert")
    return True


def migrate(manager) -> dict:
    """升级迁移（启动后台步骤）：还没有文本版的资料补转一次；上次停在转换中、等待中的接着转。

    已经判过暂不支持、转换失败的不再自动重试（资料上有「重试」）。
    """
    from ..research_view import material_paths

    queued = 0
    for root in manager._project_dirs():
        project_id = root.name
        try:
            records = store.items(project_id)
            for rel in material_paths(root):
                if kind_of(rel) != "document":
                    continue
                entry = records.get(rel) or {}
                if entry.get("state") in ("failed", "unsupported"):
                    try:
                        if entry.get("sha") == _content_revision((root / rel).read_bytes()):
                            continue
                    except OSError:
                        continue
                head = text_header(root, rel)
                stale = False
                if head and entry.get("state") not in ACTIVE:
                    try:
                        if head["sha"] == _content_revision((root / rel).read_bytes()):
                            # 文本版是最新的；没带原件锚点的（ADR-0093 之前转的）重转一次补上
                            if not lacks_anchors(root, rel):
                                continue
                            stale = True
                    except OSError:
                        continue
                if enqueue(project_id, rel, force=stale, requested="migrate"):
                    queued += 1
        except Exception:  # noqa: BLE001 - 一个项目读不了不挡别的
            _log.warning("补转资料文本版时跳过项目 %s", project_id, exc_info=True)
    detail = f"{queued} 份资料排队转成文本" if queued else "资料都有文本版"
    return {"id": "material_text", "title": "资料文本版", "result": "queued" if queued else "ok",
            "tone": "fix" if queued else "ok", "detail": detail}


_installed = False
_install_lock = threading.Lock()


def install() -> None:
    """订阅写入事件（幂等）。"""
    global _installed
    with _install_lock:
        if _installed:
            return
        from ...kernel.writepath import events

        events.subscribe(on_write)
        _installed = True
