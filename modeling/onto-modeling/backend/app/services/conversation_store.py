"""多会话存储：项目的会话索引，以及各会话的历史 / 续接点文件路径。

会话 default 沿用单会话时期的路径 sessions/{pid}.jsonl，存量项目不搬文件；
其余会话存 sessions/{pid}/{cid}.jsonl。索引是"会话是否存在"的唯一依据，
"当前选中哪条"由前端决定，服务端不记（docs/decisions/0013）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path

from .. import config

DEFAULT_ID = "default"
TITLE_MAX = 60
AUTO_TITLE_MAX = 30

_ID_PATTERN = re.compile(r"^(default|[0-9a-f]{12})$")
# 界面上传资料时前端拼在消息最前面的说明行，不该成为会话标题
_UPLOAD_PREFIX = re.compile(r"^（我通过界面上传了资料[^\n]*）\n")
# 同步路由在线程池里跑，send() 在事件循环里跑：索引的读改写必须互斥
_lock = threading.Lock()


class ConversationError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ---- 路径 ----


def _sessions_dir() -> Path:
    path = Path(config.DATA_DIR) / "sessions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _project_dir(project_id: str) -> Path:
    return _sessions_dir() / project_id


def history_path(project_id: str, conversation_id: str = DEFAULT_ID) -> Path:
    if conversation_id == DEFAULT_ID:
        return _sessions_dir() / f"{project_id}.jsonl"
    if not _ID_PATTERN.match(conversation_id):
        raise ConversationError("会话不存在或已删除", 404)
    directory = _project_dir(project_id)
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{conversation_id}.jsonl"


def meta_path(project_id: str, conversation_id: str = DEFAULT_ID) -> Path:
    return history_path(project_id, conversation_id).with_suffix(".meta.json")


def _index_path(project_id: str) -> Path:
    return _project_dir(project_id) / "index.json"


def _deleted_usage_path(project_id: str) -> Path:
    return _project_dir(project_id) / "deleted-usage.jsonl"


# ---- 索引 ----


def auto_title(text: str) -> str:
    """由首条用户消息生成标题：去掉上传说明行，压缩空白后截断。"""
    text = _UPLOAD_PREFIX.sub("", text or "")
    return " ".join(text.split())[:AUTO_TITLE_MAX]


def _seed_entry(project_id: str, conversation_id: str) -> dict:
    """由已有历史推出索引条目：标题取首条用户消息，创建时间取首个事件。"""
    title, created_at = "", None
    try:
        with open(history_path(project_id, conversation_id), encoding="utf-8") as f:
            for line in f:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if created_at is None:
                    created_at = event.get("ts")
                if event.get("type") == "user_message":
                    title = auto_title(event.get("text") or "")
                    break
    except OSError:
        pass
    return {"title": title, "title_manual": False, "created_at": created_at or time.time()}


def _load(project_id: str) -> dict[str, dict]:
    """读索引（调用方持有 _lock）。索引缺失或损坏时由磁盘上的历史文件推出，

    不在读路径落盘：首次变更时才写入，此后以索引为准（删掉的 default 不会复活）。
    """
    try:
        conversations = json.loads(_index_path(project_id).read_text(encoding="utf-8")).get(
            "conversations"
        )
        if isinstance(conversations, dict) and conversations:
            return conversations
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    ids = [DEFAULT_ID]
    directory = _project_dir(project_id)
    if directory.is_dir():
        ids += sorted(
            path.stem
            for path in directory.glob("*.jsonl")
            if path.stem != DEFAULT_ID and _ID_PATTERN.match(path.stem)
        )
    return {cid: _seed_entry(project_id, cid) for cid in ids}


def _write(project_id: str, conversations: dict[str, dict]) -> None:
    directory = _project_dir(project_id)
    directory.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=directory, prefix="index-", suffix=".tmp")
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = None
            json.dump({"conversations": conversations}, f, ensure_ascii=False, indent=2)
        os.replace(temporary_path, _index_path(project_id))
    finally:
        if fd is not None:
            os.close(fd)
        temporary_path.unlink(missing_ok=True)


def _view(project_id: str, conversation_id: str, entry: dict) -> dict:
    created_at = entry.get("created_at") or 0
    try:
        updated_at = history_path(project_id, conversation_id).stat().st_mtime
    except OSError:
        updated_at = created_at
    return {
        "id": conversation_id,
        "title": entry.get("title") or "",
        "title_manual": bool(entry.get("title_manual")),
        "created_at": created_at,
        "updated_at": updated_at,
    }


def _sorted_views(project_id: str, conversations: dict[str, dict]) -> list[dict]:
    views = [_view(project_id, cid, entry) for cid, entry in conversations.items()]
    return sorted(views, key=lambda item: item["updated_at"], reverse=True)


def _new_entry() -> dict:
    return {"title": "", "title_manual": False, "created_at": time.time()}


# ---- 对外操作 ----


def list_conversations(project_id: str) -> list[dict]:
    """全部会话，最近更新的在前；至少有一条。"""
    with _lock:
        return _sorted_views(project_id, _load(project_id))


def exists(project_id: str, conversation_id: str) -> bool:
    if not isinstance(conversation_id, str) or not _ID_PATTERN.match(conversation_id):
        return False
    with _lock:
        return conversation_id in _load(project_id)


def create(project_id: str) -> dict:
    with _lock:
        conversations = _load(project_id)
        conversation_id = uuid.uuid4().hex[:12]
        conversations[conversation_id] = _new_entry()
        _write(project_id, conversations)
        return _view(project_id, conversation_id, conversations[conversation_id])


def rename(project_id: str, conversation_id: str, title: str) -> dict:
    title = " ".join((title or "").split())
    if not 1 <= len(title) <= TITLE_MAX:
        raise ConversationError(f"会话名称须为 1–{TITLE_MAX} 个字符")
    with _lock:
        conversations = _load(project_id)
        entry = conversations.get(conversation_id)
        if entry is None:
            raise ConversationError("会话不存在或已删除", 404)
        entry["title"] = title
        entry["title_manual"] = True
        _write(project_id, conversations)
        return _view(project_id, conversation_id, entry)


def note_message(project_id: str, conversation_id: str, text: str) -> None:
    """首条用户消息落定自动标题；已有标题（含手动命名）不覆盖。"""
    with _lock:
        conversations = _load(project_id)
        entry = conversations.get(conversation_id)
        if entry is None or entry.get("title"):
            return
        title = auto_title(text)
        if not title:
            return
        entry["title"] = title
        try:
            _write(project_id, conversations)
        except OSError:
            pass  # 标题只是展示信息，落盘失败不阻断发送


def delete(project_id: str, conversation_id: str) -> list[dict]:
    """删除会话的历史与续接点，返回剩余会话；删到一条不剩时补一条空会话。"""
    with _lock:
        conversations = _load(project_id)
        if conversation_id not in conversations:
            raise ConversationError("会话不存在或已删除", 404)
        del conversations[conversation_id]
        if not conversations:
            conversations[uuid.uuid4().hex[:12]] = _new_entry()
        # 先改索引再删文件：索引是存在性的依据，文件删到一半失败也不会让会话复活
        _write(project_id, conversations)
        _keep_usage(project_id, conversation_id)
        for path in (
            history_path(project_id, conversation_id),
            meta_path(project_id, conversation_id),
        ):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        return _sorted_views(project_id, conversations)


def _keep_usage(project_id: str, conversation_id: str) -> None:
    """删除前留存 result 事件：只含成本与 token、不含对话内容，

    用量统计（项目累计与运维按日聚合）不因删除会话而减少。
    """
    kept = []
    try:
        with open(history_path(project_id, conversation_id), encoding="utf-8") as f:
            for line in f:
                if '"result"' not in line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "result":
                    kept.append(
                        {
                            "type": "result",
                            "ts": event.get("ts"),
                            "total_cost_usd": event.get("total_cost_usd"),
                            "usage": event.get("usage"),
                        }
                    )
        if kept:
            with open(_deleted_usage_path(project_id), "a", encoding="utf-8") as f:
                for event in kept:
                    f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        pass  # 用量留存是统计口径的补偿，失败不阻断删除


def load_deleted_usage(project_id: str) -> list[dict]:
    events = []
    try:
        for line in _deleted_usage_path(project_id).read_text(encoding="utf-8").splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return events


def purge(project_id: str) -> None:
    """删除项目：清除全部会话的历史、续接点、索引与留存用量，不留可回放的对话。"""
    with _lock:
        for path in (history_path(project_id), meta_path(project_id)):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        shutil.rmtree(_project_dir(project_id), ignore_errors=True)


def clear_all_resume_tokens(project_id: str) -> None:
    """全部会话的续接点一并作废（切换引擎）；直接按文件清，不依赖索引。"""
    paths = [meta_path(project_id)]
    directory = _project_dir(project_id)
    if directory.is_dir():
        paths += list(directory.glob("*.meta.json"))
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
