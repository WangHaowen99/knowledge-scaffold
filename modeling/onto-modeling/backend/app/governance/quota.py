"""配额（详设 4.10.1、方案 E9、契约 SY-15、SY-16）：按用户和项目限制并发、token 预算、采集频率。

- 用量台账：SQLite（DATA_DIR/governance/quota.sqlite），按（项目、所有者、UTC 日、来源）聚合 token 与成本。
  token 按成本口径计（ADR-0088）：输入 + 缓存写 + 输出 + 缓存读 // 10；台账记着口径版本，口径变了从会话历史重建
  （本月和上月，已删除项目的用量随之丢掉）。
  主路径是增量扫会话历史（含已删除会话留存的用量文件）：按文件记读到的字节位置，只读新增的完整行；
  每条用量事件按指纹去重，所以文件被截断或改写后从头重扫、会话删除后用量搬进留存文件，都不会重复计数；
  已入账的用量也不因文件截断、会话或项目删除而减少（删项目不能绕开当日上限）；
- 用量记在项目所有者名下（本地模式是 local）；所有者变更后，已入账的用量不跟着走；
- 台账只管本月和上月：更早的事件不入账，去重指纹也随之清掉；
- 限额：DATA_DIR/governance/quotas.json，默认值加单独设置（覆盖到某个用户或项目），0 表示不限；
- 检查：发消息前（QuotaGate）查并发会话、用户当日 token、项目当月 token；采集按数据源每小时次数；
  自主构建每一步也走这道检查，配额用完时运行暂停（F6，自主构建没有单独的预算）；
- 配额自身出错一律放行并记日志：配额不能拖垮服务。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from starlette.types import ASGIApp, Receive, Scope, Send

from .. import config
from ..api import errors
from ..api.errors import ApiError
from ..services import conversation_store

_log = logging.getLogger(__name__)

LOCAL_USER = "local"  # 与 deps.LOCAL_USER 一致：本地模式的用户与无主项目的用量归属
# 默认值按成本口径定（ADR-0088）：实测一整轮建模（资料分析到发布）折算后约 215 万
PER_USER_DEFAULTS = {"concurrent_sessions": 3, "daily_tokens": 10_000_000, "collections_per_source_per_hour": 6}
PER_PROJECT_DEFAULTS = {"online_engines": 1, "monthly_tokens": 100_000_000, "snapshot_git_mb": 20}
READ_ONLY_ITEMS = frozenset({"online_engines"})  # 一个项目只有一个在线引擎，由会话池保证，不可改
# 证据源本期只留接口（ADR-0056）：这两项保留、能设，但没有地方检查（SY-15 的 not_enforced）
NOT_ENFORCED = ("collections_per_source_per_hour", "snapshot_git_mb")
ITEMS = {
    "user": tuple(PER_USER_DEFAULTS),
    "project": tuple(item for item in PER_PROJECT_DEFAULTS if item not in READ_ONLY_ITEMS),
}
MAX_VALUE = 10**15
SYNC_INTERVAL_S = 1.0
COLLECTION_WINDOW_S = 3600
HEAD_BYTES = 4096
DELETED_USAGE_FILE = "deleted-usage.jsonl"
# 台账的计数口径：和库里记的不一样时清掉用量、去重指纹和读取位置，从会话历史重新入账
COUNTING = "cost-v1"

_MESSAGES = {
    "concurrent_sessions": "同时在线的建模会话已到上限（{limit} 个），请等其他项目的会话结束后再试，或请管理员调整配额",
    "daily_tokens": "今天的模型用量已到上限（{limit} token），明天再试或请管理员调整配额",
    "monthly_tokens": "本项目本月的模型用量已到上限（{limit} token），下个月再试或请管理员调整配额",
    "collections_per_source_per_hour": "这个数据源一小时内的采集次数已到上限（{limit} 次），稍后再试或请管理员调整配额",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
    project_id TEXT NOT NULL, owner TEXT NOT NULL, day TEXT NOT NULL, month TEXT NOT NULL,
    source TEXT NOT NULL, tokens INTEGER NOT NULL DEFAULT 0, cost_usd REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, owner, day, source)
);
CREATE INDEX IF NOT EXISTS usage_owner_day ON usage(owner, day);
CREATE INDEX IF NOT EXISTS usage_project_month ON usage(project_id, month);
CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY, day TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY, offset INTEGER NOT NULL, inode INTEGER NOT NULL,
    head_len INTEGER NOT NULL, head TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS collections (source_id TEXT NOT NULL, owner TEXT NOT NULL, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS collections_lookup ON collections(owner, source_id, ts);
"""


class QuotaExceeded(ApiError):
    """超出配额（契约 2.1 quota_exceeded，429）。details：quota、scope、limit、used、reset_at。"""

    def __init__(self, item: str, limit: int, used: int, subject: dict, reset_at: float | None = None):
        from ..api import deps

        message = _MESSAGES.get(item, "已超出配额（上限 {limit}）").format(limit=f"{limit:,}")
        details = {"quota": item, "scope": subject["kind"], "limit": limit, "used": used,
                   "reset_at": deps.iso(reset_at)}
        super().__init__("quota_exceeded", message, 429, details)
        self.item, self.limit, self.used, self.subject, self.reset_at = item, limit, used, subject, reset_at


# ---- 小工具 ----


def valid_value(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_VALUE


def _count(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _utc(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, timezone.utc)


def _day(ts: float) -> str:
    return _utc(ts).strftime("%Y-%m-%d")


def _next_day(ts: float) -> float:
    moment = _utc(ts)
    return (datetime(moment.year, moment.month, moment.day, tzinfo=timezone.utc) + timedelta(days=1)).timestamp()


def _next_month(ts: float) -> float:
    moment = _utc(ts)
    year, month = (moment.year + 1, 1) if moment.month == 12 else (moment.year, moment.month + 1)
    return datetime(year, month, 1, tzinfo=timezone.utc).timestamp()


def _horizon(ts: float) -> str:
    """台账下限：上月 1 日（只管本月与上月）。"""
    moment = _utc(ts)
    year, month = (moment.year - 1, 12) if moment.month == 1 else (moment.year, moment.month - 1)
    return f"{year:04d}-{month:02d}-01"


def _event_time(event: dict, fallback: float) -> float:
    value = event.get("ts")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return fallback


def _ensure_counting(conn) -> None:
    """台账的口径版本对不上（升级前按「输入 + 缓存读 + 输出」入账的）：清掉用量、去重指纹和读取位置，下次同步重建。"""
    row = conn.execute("SELECT value FROM meta WHERE key = 'counting'").fetchone()
    if row is not None and row[0] == COUNTING:
        return
    conn.execute("DELETE FROM usage")
    conn.execute("DELETE FROM seen")
    conn.execute("DELETE FROM files")
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('counting', ?)", (COUNTING,))


def _usage_of(event: dict) -> tuple[int, float] | None:
    """用量事件的 (token, 成本)；token 按成本口径计（model_pricing.tokens_of，ADR-0088）。"""
    from ..services import model_pricing

    usage = conversation_store.counted_usage(event)
    if usage is None:
        return None
    tokens = model_pricing.tokens_of(usage)
    cost = 0.0
    if event.get("type") == "result":
        try:
            cost = max(0.0, float(event.get("total_cost_usd") or 0))
        except (TypeError, ValueError):
            cost = 0.0
    return tokens, cost


def _fingerprint(project_id: str, event: dict) -> str:
    """用量事件的指纹：取删除会话时留存的那几个字段，原事件与留存副本指纹相同。"""
    raw = json.dumps(
        [project_id, event.get("type"), event.get("ts"), event.get("usage"), event.get("total_cost_usd")],
        sort_keys=True, ensure_ascii=False, default=str,
    )
    return hashlib.sha1(raw.encode()).hexdigest()[:24]


def _holds_engine(session) -> bool:
    """占着（或马上要占）引擎的会话：引擎已连上、正在连接、在跑或排队，或者有一次发送已过了配额检查、
    还没连上（会话池里 sending > 0）。"""
    state = getattr(session, "state", "")
    if state == "closed":
        return False
    connecting = getattr(session, "is_connecting", None)
    return (
        getattr(session, "engine", None) is not None
        or (callable(connecting) and bool(connecting()))
        or state in ("running", "queued")
        or bool(getattr(session, "sending", 0))
    )


# ---- 配额服务 ----


class QuotaService:
    """一个数据目录一份：限额配置、用量台账与各项检查。线程安全（同步路由与闸门都在线程池里调）。"""

    def __init__(self, data_dir: Path | str, workspaces=None, sync_interval: float = SYNC_INTERVAL_S):
        self.data_dir = Path(data_dir)
        self.sync_interval = sync_interval
        self.clock = time.time  # 测试可替换
        self._workspaces = workspaces
        self._lock = threading.RLock()
        self._last_sync = 0.0
        self._pruned_day = ""

    # ---- 路径与依赖 ----

    @property
    def db_path(self) -> Path:
        return self.data_dir / "governance" / "quota.sqlite"

    @property
    def limits_path(self) -> Path:
        return self.data_dir / "governance" / "quotas.json"

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"  # 与 conversation_store 同一目录

    @property
    def workspaces(self):
        if self._workspaces is not None:
            return self._workspaces
        from ..api import deps

        return deps.workspaces

    @contextmanager
    def _db(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10)
        try:
            conn.executescript(_SCHEMA)
            with conn:
                _ensure_counting(conn)
                yield conn
        finally:
            conn.close()

    def project_owner(self, project_id: str) -> str | None:
        """项目元数据里的所有者（无主或本地模式为空串）；项目不存在或元数据读不到时为 None。"""
        from ..services.workspace import WorkspaceError

        try:
            meta = self.workspaces.get_project(project_id)
        except (WorkspaceError, OSError, ValueError):
            return None
        return str(meta.get("owner") or "")

    # ---- 限额配置 ----

    def _raw(self) -> dict:
        try:
            data = json.loads(self.limits_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            _log.warning("配额配置读不到或已损坏，按默认值：%s", self.limits_path, exc_info=True)
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict) -> None:
        path = self.limits_path
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix="quotas-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    @staticmethod
    def _valid_override(item) -> bool:
        if not isinstance(item, dict) or not isinstance(item.get("subject"), dict):
            return False
        kind = item["subject"].get("kind")
        return (kind in ITEMS and isinstance(item["subject"].get("id"), str)
                and item.get("item") in ITEMS[kind] and valid_value(item.get("value")))

    def limits(self) -> dict:
        """生效的限额：{per_user, per_project, overrides}；缺的、不合法的项按默认值。"""
        raw = self._raw()

        def merged(defaults: dict, stored) -> dict:
            stored = stored if isinstance(stored, dict) else {}
            return {key: stored[key] if valid_value(stored.get(key)) else value for key, value in defaults.items()}

        per_project = merged(PER_PROJECT_DEFAULTS, raw.get("per_project"))
        per_project["online_engines"] = 1
        overrides = [item for item in raw.get("overrides") or [] if self._valid_override(item)]
        return {"per_user": merged(PER_USER_DEFAULTS, raw.get("per_user")), "per_project": per_project,
                "overrides": overrides}

    @staticmethod
    def _effective(limits: dict, item: str, kind: str, subject_id: str) -> int:
        for override in limits["overrides"]:
            subject = override["subject"]
            if override["item"] == item and subject["kind"] == kind and subject["id"] == subject_id:
                return override["value"]
        return limits["per_user" if kind == "user" else "per_project"][item]

    def limit(self, item: str, kind: str, subject_id: str) -> int:
        """某个用户或项目某一项的生效限额：有单独设置取单独设置，否则取默认；0 = 不限。"""
        return self._effective(self.limits(), item, kind, subject_id)

    def set_limits(self, per_user: dict, per_project: dict) -> dict:
        """改默认限额（只改给出的项，online_engines 固定 1），立即生效；返回生效的限额。"""
        with self._lock:
            raw = self._raw()
            current = self.limits()
            for group, values in (("per_user", per_user), ("per_project", per_project)):
                merged = {key: value for key, value in current[group].items() if key not in READ_ONLY_ITEMS}
                merged.update({key: value for key, value in (values or {}).items()
                               if key in ITEMS["user" if group == "per_user" else "project"] and valid_value(value)})
                raw[group] = merged
            raw["overrides"] = current["overrides"]
            self._save(raw)
            return self.limits()

    def add_override(self, kind: str, subject_id: str, label: str, item: str, value: int, reason: str,
                     actor: str) -> tuple[dict, str | None]:
        """单独设置：同一对象的同一项只留一条，新的替换旧的；返回 (新条目, 被替换条目的 id)。"""
        with self._lock:
            raw = self._raw()
            overrides = self.limits()["overrides"]
            replaced = next((o for o in overrides if o["item"] == item and o["subject"]["kind"] == kind
                             and o["subject"]["id"] == subject_id), None)
            overrides = [o for o in overrides if o is not replaced]
            ids = {o.get("id") for o in overrides}
            override_id = f"qo-{secrets.token_hex(3)}"
            while override_id in ids:
                override_id = f"qo-{secrets.token_hex(3)}"
            entry = {"id": override_id, "subject": {"kind": kind, "id": subject_id, "label": label},
                     "item": item, "value": value, "reason": reason, "created_at": self.clock(), "created_by": actor}
            raw["overrides"] = overrides + [entry]
            self._save(raw)
            return entry, (replaced or {}).get("id")

    def delete_override(self, override_id: str) -> dict | None:
        """删一条单独设置，返回被删的条目；不存在为 None。"""
        with self._lock:
            raw = self._raw()
            overrides = self.limits()["overrides"]
            target = next((o for o in overrides if o.get("id") == override_id), None)
            if target is None:
                return None
            raw["overrides"] = [o for o in overrides if o is not target]
            self._save(raw)
            return target

    # ---- 用量台账 ----

    def _add(self, conn, key: str | None, project_id: str, owner: str, ts: float, tokens: int, cost: float,
             source: str, horizon: str) -> int:
        day = _day(ts)
        if day < horizon or (tokens <= 0 and cost <= 0):
            return 0
        if key is not None and conn.execute(
            "INSERT OR IGNORE INTO seen(key, day) VALUES (?, ?)", (key, day)
        ).rowcount == 0:
            return 0
        conn.execute(
            "INSERT INTO usage(project_id, owner, day, month, source, tokens, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(project_id, owner, day, source) DO UPDATE SET "
            "tokens = tokens + excluded.tokens, cost_usd = cost_usd + excluded.cost_usd",
            (project_id, owner, day, day[:7], source, tokens, cost),
        )
        return 1

    def record(self, project_id: str, owner: str, tokens: int, cost_usd: float = 0.0, source: str = "engine",
               *, ts: float | None = None, key: str | None = None) -> None:
        """直接记账（会话宿主钩子预留）：只记不落在会话历史里的用量，否则和 sync() 重复；带 key 时按 key 去重。"""
        now = self.clock()
        with self._lock, self._db() as conn:
            self._add(conn, f"{source}:{key}" if key else None, project_id, owner or LOCAL_USER,
                      ts if ts is not None else now, _count(tokens), max(0.0, float(cost_usd or 0)),
                      source, _horizon(now))

    def _history_files(self):
        """(台账里的文件键, 项目 ID, 路径)：各会话历史与已删除会话的留存用量。"""
        root = self.sessions_dir
        if not root.is_dir():
            return
        for path in sorted(root.glob("*.jsonl")):
            yield path.name, path.stem, path
        for path in sorted(root.glob("*/*.jsonl")):
            yield f"{path.parent.name}/{path.name}", path.parent.name, path

    def _ingest_file(self, conn, rel: str, project_id: str, path: Path, known: tuple | None, owners: dict,
                     now: float, horizon: str) -> int:
        try:
            stat = path.stat()
        except OSError:
            return 0
        offset, inode, head_len, head = known or (0, stat.st_ino, 0, "")
        if known is not None and stat.st_ino == inode and stat.st_size == offset:
            return 0
        if project_id not in owners:
            owner = self.project_owner(project_id)
            owners[project_id] = None if owner is None else owner or LOCAL_USER
        owner = owners[project_id]
        if owner is None:
            return 0  # 项目不存在或元数据暂时读不到：不入账、不前移位置，下次再读
        source = "deleted" if path.name == DELETED_USAGE_FILE else "history"
        added = 0
        with open(path, "rb") as handle:
            rewritten = stat.st_ino != inode or stat.st_size < offset
            if not rewritten and head_len:
                rewritten = hashlib.sha1(handle.read(head_len)).hexdigest() != head
            if rewritten:
                offset = 0  # 被截断或改写：从头重扫，已入账的事件按指纹去重
            handle.seek(offset)
            while True:
                line = handle.readline()
                if not line.endswith(b"\n"):
                    break  # 末尾半截行（正在写）下次再读
                position, offset = offset, offset + len(line)
                if b'"model_usage"' not in line and b'"result"' not in line:
                    continue
                try:
                    event = json.loads(line)
                except (UnicodeDecodeError, ValueError):
                    continue
                usage = _usage_of(event) if isinstance(event, dict) else None
                if usage is None:
                    continue
                key = _fingerprint(project_id, event) if event.get("ts") is not None else f"{rel}@{position}"
                added += self._add(conn, key, project_id, owner, _event_time(event, now), *usage, source, horizon)
            if rewritten or head_len < min(offset, HEAD_BYTES):
                head_len = min(offset, HEAD_BYTES)
                handle.seek(0)
                head = hashlib.sha1(handle.read(head_len)).hexdigest()
        conn.execute(
            "INSERT OR REPLACE INTO files(path, offset, inode, head_len, head) VALUES (?, ?, ?, ?, ?)",
            (rel, offset, stat.st_ino, head_len, head),
        )
        return added

    def sync(self, max_age: float = 0.0) -> int:
        """增量入账会话历史里的用量事件，返回本次入账的事件数；距上次同步不到 max_age 秒时跳过。"""
        with self._lock:
            now = self.clock()
            if max_age and 0 <= now - self._last_sync < max_age:
                return 0
            horizon = _horizon(now)
            added = 0
            with self._db() as conn:
                known = {row[0]: tuple(row[1:]) for row in
                         conn.execute("SELECT path, offset, inode, head_len, head FROM files")}
                present, owners = set(), {}
                for rel, project_id, path in self._history_files():
                    present.add(rel)
                    try:
                        added += self._ingest_file(conn, rel, project_id, path, known.get(rel), owners, now, horizon)
                    except OSError:
                        _log.warning("配额台账读会话历史失败，下次重试：%s", path, exc_info=True)
                # 消失的文件（会话或项目删了）只忘掉读取位置，已入账的用量保留
                conn.executemany("DELETE FROM files WHERE path = ?", [(rel,) for rel in set(known) - present])
                today = _day(now)
                if self._pruned_day != today:
                    conn.execute("DELETE FROM seen WHERE day < ?", (horizon,))
                    self._pruned_day = today
            self._last_sync = now
            return added

    def _sum(self, where: str, args: tuple) -> int:
        with self._db() as conn:
            return int(conn.execute(f"SELECT COALESCE(SUM(tokens), 0) FROM usage WHERE {where}", args).fetchone()[0])

    def used_today(self, owner: str) -> int:
        """用户今天（UTC）在名下全部项目上的 token。"""
        return self._sum("owner = ? AND day = ?", (owner, _day(self.clock())))

    def used_month(self, project_id: str) -> int:
        """项目本月（UTC）的 token。"""
        return self._sum("project_id = ? AND month = ?", (project_id, _day(self.clock())[:7]))

    def usage_view(self, project_id: str | None = None, username: str | None = None) -> dict:
        """看板用：今天与本月的 token、成本，及按用户（今天）、按项目（本月）的分布；可按项目、用户筛。"""
        self.sync(self.sync_interval)
        now = self.clock()
        day = _day(now)
        conditions, args = [], []
        if project_id is not None:
            conditions.append("project_id = ?")
            args.append(project_id)
        if username is not None:
            conditions.append("owner = ?")
            args.append(username or LOCAL_USER)
        where = " AND ".join(conditions) or "1 = 1"
        with self._db() as conn:
            def query(sql: str, extra: tuple):
                return conn.execute(sql.format(where=where), (*args, *extra)).fetchall()

            today = query("SELECT COALESCE(SUM(tokens), 0), COALESCE(SUM(cost_usd), 0) FROM usage "
                          "WHERE {where} AND day = ?", (day,))[0]
            month = query("SELECT COALESCE(SUM(tokens), 0), COALESCE(SUM(cost_usd), 0) FROM usage "
                          "WHERE {where} AND month = ?", (day[:7],))[0]
            by_user = query("SELECT owner, SUM(tokens) FROM usage WHERE {where} AND day = ? GROUP BY owner", (day,))
            by_project = query("SELECT project_id, SUM(tokens) FROM usage WHERE {where} AND month = ? "
                               "GROUP BY project_id", (day[:7],))
        return {
            "day": day, "month": day[:7],
            "today_tokens": int(today[0]), "today_cost_usd": round(float(today[1]), 4),
            "month_tokens": int(month[0]), "month_cost_usd": round(float(month[1]), 4),
            "by_user_today": {owner: int(tokens) for owner, tokens in by_user},
            "by_project_month": {pid: int(tokens) for pid, tokens in by_project},
        }

    # ---- 检查 ----

    def live_sessions(self, owner: str, exclude: str = "", sessions: dict | None = None) -> list[str]:
        """用户名下占着引擎的项目（不含 exclude）。只读会话池，不加它的锁；sessions 缺省取全局会话池。"""
        if sessions is None:
            from ..services import agent_sessions

            sessions = agent_sessions.manager._sessions
        sessions = dict(sessions)
        result = []
        for project_id, session in sessions.items():
            if project_id == exclude or not _holds_engine(session) or getattr(session, "run_id", None):
                continue  # 自主构建的会话不算（同时运行数由平台限制管）
            if (self.project_owner(project_id) or LOCAL_USER) == owner:
                result.append(project_id)
        return result

    def check_send(self, project_id: str, username: str) -> None:
        """发消息、跑命令前：用户并发会话 → 用户当日 token → 项目当月 token，超了抛 QuotaExceeded。"""
        self.check_concurrency(project_id, username)
        self.check_tokens(project_id, username)

    def check_concurrency(self, project_id: str, username: str, sessions: dict | None = None) -> None:
        """用户同时在线的建模会话。会话池在绑定会话的同一把锁里调（sessions 是它的会话表），
        两个同时发起的发送不会都过。"""
        owner = self.project_owner(project_id) or username or LOCAL_USER
        concurrent = self.limit("concurrent_sessions", "user", owner)
        if not concurrent:
            return
        if sessions is None:
            from ..services import agent_sessions

            sessions = agent_sessions.manager._sessions
        # 发给已在线的项目不起新引擎，不算并发
        if _holds_engine(sessions.get(project_id)):
            return
        used = len(self.live_sessions(owner, exclude=project_id, sessions=sessions))
        if used >= concurrent:
            raise QuotaExceeded("concurrent_sessions", concurrent, used, {"kind": "user", "id": owner})

    def check_tokens(self, project_id: str, username: str) -> None:
        """用户当日、项目当月的 token。"""
        owner = self.project_owner(project_id) or username or LOCAL_USER
        limits = self.limits()
        user = {"kind": "user", "id": owner}
        daily = self._effective(limits, "daily_tokens", "user", owner)
        monthly = self._effective(limits, "monthly_tokens", "project", project_id)
        if not (daily or monthly):
            return
        self.sync(self.sync_interval)
        now = self.clock()
        if daily:
            used = self.used_today(owner)
            if used >= daily:
                raise QuotaExceeded("daily_tokens", daily, used, user, _next_day(now))
        if monthly:
            used = self.used_month(project_id)
            if used >= monthly:
                raise QuotaExceeded("monthly_tokens", monthly, used, {"kind": "project", "id": project_id},
                                    _next_month(now))

    def remaining(self, project_id: str, username: str) -> dict:
        """剩余 token：用户当日与项目当月剩余取小者；tokens 为 None 表示不限。"""
        owner = self.project_owner(project_id) or username or LOCAL_USER
        limits = self.limits()
        daily = self._effective(limits, "daily_tokens", "user", owner)
        monthly = self._effective(limits, "monthly_tokens", "project", project_id)
        if not (daily or monthly):
            return {"tokens": None, "dimension": None}
        self.sync(self.sync_interval)
        candidates = []
        if daily:
            candidates.append((max(0, daily - self.used_today(owner)), "daily_tokens"))
        if monthly:
            candidates.append((max(0, monthly - self.used_month(project_id)), "monthly_tokens"))
        tokens, dimension = min(candidates, key=lambda candidate: candidate[0])
        return {"tokens": tokens, "dimension": dimension}

    def check_collection(self, source_id: str, username: str, record: bool = True) -> None:
        """证据采集（EV-14）：同一用户对同一数据源每小时的次数；没超时默认记一次。

        本期证据源只留接口，EV-14 回 501，这里没有调用方（NOT_ENFORCED）。"""
        owner = username or LOCAL_USER
        limit = self.limit("collections_per_source_per_hour", "user", owner)
        now = self.clock()
        with self._lock, self._db() as conn:
            conn.execute("DELETE FROM collections WHERE owner = ? AND source_id = ? AND ts <= ?",
                         (owner, source_id, now - COLLECTION_WINDOW_S))
            times = [row[0] for row in conn.execute(
                "SELECT ts FROM collections WHERE owner = ? AND source_id = ? ORDER BY ts", (owner, source_id))]
            if limit and len(times) >= limit:
                raise QuotaExceeded("collections_per_source_per_hour", limit, len(times),
                                    {"kind": "user", "id": owner}, times[0] + COLLECTION_WINDOW_S)
            if record:
                conn.execute("INSERT INTO collections(source_id, owner, ts) VALUES (?, ?, ?)", (source_id, owner, now))


_service: QuotaService | None = None
_service_lock = threading.Lock()


def service() -> QuotaService:
    """按当前数据目录取单例；数据目录变了（测试）时重建。"""
    global _service
    data_dir = Path(config.DATA_DIR)
    with _service_lock:
        if _service is None or _service.data_dir != data_dir:
            _service = QuotaService(data_dir)
        return _service


# ---- 发送闸门 ----

_GATED_PATHS = (
    re.compile(r"^/api/v1/projects/([^/]+)/conversations/[^/]+/messages$"),
    re.compile(r"^/api/v1/projects/([^/]+)/commands$"),
)


def _gated_project(scope: Scope) -> str:
    if scope["type"] != "http" or scope.get("method") != "POST":
        return ""
    path = scope.get("path") or ""
    return next((match.group(1) for match in (pattern.match(path) for pattern in _GATED_PATHS) if match), "")


def _username(scope: Scope) -> str:
    """鉴权中间件写的 request.state.user（即 scope["state"]["user"]）；本地模式没有，记 local。"""
    state = scope.get("state")
    claims = state.get("user") if isinstance(state, dict) else getattr(state, "user", None)
    sub = claims.get("sub") if isinstance(claims, dict) else None
    return str(sub or "") or LOCAL_USER


def _check_request(project_id: str, username: str) -> None:
    quotas = service()
    owner = quotas.project_owner(project_id)
    if owner is None or (owner and owner != username):
        return  # 项目不存在、不是所有者：交给路由回 404、403，不替别人报配额
    quotas.check_send(project_id, username)


def check_before_turn(project_id: str, username: str, part: str, sessions: dict | None = None) -> None:
    """会话池发起一轮之前调（`agent_sessions.SessionManager.send`，所有入口都经它）：part 取 tokens 或
    concurrency。超限抛 QuotaExceeded；配额自身出错放行并记日志。"""
    try:
        quotas = service()
        if part == "tokens":
            quotas.check_tokens(project_id, username)
        else:
            quotas.check_concurrency(project_id, username, sessions)
    except QuotaExceeded:
        raise
    except Exception:  # noqa: BLE001 - 配额自身出错一律放行
        _log.warning("配额检查出错，已放行（项目 %s）", project_id, exc_info=True)


class QuotaGate:
    """发消息（S-07）、跑命令前查配额：超限回 429 quota_exceeded，其余放行。

    这是提前拦：请求还没建任务、没写历史就回 429。真正的关口在会话池（`check_before_turn`），
    重试、续跑、Studio 转助手复核这些不经这两条路径的入口也在那里查。

    纯 ASGI，挂在鉴权之内（读 scope["state"]["user"]）。检查碰磁盘，放到线程里跑；配额自身出错放行并记日志。
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        project_id = _gated_project(scope)
        if project_id:
            try:
                await asyncio.to_thread(_check_request, project_id, _username(scope))
            except QuotaExceeded as exc:
                response = errors.response(exc.code, str(exc), exc.status, exc.details)
                return await response(scope, receive, send)
            except Exception:
                _log.warning("配额检查出错，已放行：%s", scope.get("path"), exc_info=True)
        await self.app(scope, receive, send)
