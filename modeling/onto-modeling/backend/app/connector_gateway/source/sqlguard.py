"""数据库提供方的语句白名单（ADR-0086 只读三重保证之三；ADR-0104 `ctx.readonly`）。

发给业务库的每条语句都先过 `check`：去掉注释、字符串字面量和带引号的标识符后，只能是一条语句，首个关键字是
SELECT / SHOW / DESCRIBE / DESC / EXPLAIN / WITH，且不含写关键字、加锁子句和有副作用的函数。语句都是提供方自己
拼的，但表名、列名来自业务库（不可信），所以按最保守的方式断词：词法上有歧义的写法（MySQL 的 `/*!` 可执行注释、
PostgreSQL 的美元引号、字符串里的反斜杠、未闭合的引号或注释）一律拒绝，不去猜服务端怎么解析。

会话设置（只读会话、超时）是唯一例外，走 `check_session`，只认几条固定写法。
"""

from __future__ import annotations

import re

from .errors import SourceError

DIALECTS = ("mysql", "postgres")

ALLOWED_FIRST = frozenset({"SELECT", "SHOW", "DESCRIBE", "DESC", "EXPLAIN", "WITH"})

DENIED_WORDS = frozenset({
    # 写数据、改结构、改权限
    "INSERT", "UPDATE", "DELETE", "MERGE", "REPLACE", "UPSERT", "CREATE", "ALTER", "DROP", "TRUNCATE", "RENAME",
    "GRANT", "REVOKE", "COMMENT", "IMPORT",
    # 执行别的代码
    "CALL", "EXEC", "EXECUTE", "DO", "PREPARE", "DEALLOCATE", "HANDLER",
    # 导入导出文件
    "COPY", "INTO", "LOAD", "OUTFILE", "DUMPFILE",
    # 锁、会话、事务控制（会话设置只走 check_session）
    "LOCK", "UNLOCK", "SET", "RESET", "DISCARD", "BEGIN", "START", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE",
    # 维护类：会改统计信息或物理结构
    "ANALYZE", "VACUUM", "REINDEX", "CLUSTER", "REFRESH", "OPTIMIZE", "REPAIR", "CHECKPOINT", "FLUSH", "PURGE",
    "KILL", "SHUTDOWN", "NOTIFY", "LISTEN", "UNLISTEN",
})

# 有副作用（睡眠、锁、读写服务器文件、跨库执行、改序列、终止会话）的函数，后面跟 `(` 才算
_DENIED_FUNCTION = re.compile(
    r"\b(?:sleep|pg_sleep\w*|benchmark|get_lock|release_lock|release_all_locks|is_free_lock|is_used_lock"
    r"|pg_(?:try_)?advisory\w*|load_file|pg_read\w*|pg_ls_\w+|pg_stat_file|pg_file_\w+|lo_\w+|dblink\w*"
    r"|pg_terminate_backend|pg_cancel_backend|pg_reload_conf|pg_rotate_logfile|pg_switch_wal|pg_promote"
    r"|pg_create_\w+|pg_drop_\w+|pg_replication_\w+|pg_logical_\w+|pg_notify|set_config|nextval|setval"
    r"|query_to_\w+|cursor_to_\w+|sys_exec|sys_eval)\s*\(",
    re.IGNORECASE,
)

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")

# 会话设置：只认这几条（数字由提供方按超时填）
_SESSION = {
    "mysql": (
        re.compile(r"SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED"),
        re.compile(r"SET SESSION TRANSACTION READ ONLY"),
        re.compile(r"SET SESSION max_execution_time = \d{1,9}"),
        re.compile(r"SET SESSION lock_wait_timeout = \d{1,9}"),
        re.compile(r"START TRANSACTION READ ONLY"),
        re.compile(r"ROLLBACK"),
    ),
    "postgres": (
        re.compile(r"SET default_transaction_read_only = on"),
        re.compile(r"SET statement_timeout = \d{1,9}"),
        re.compile(r"SET lock_timeout = \d{1,9}"),
        re.compile(r"BEGIN READ ONLY"),
        re.compile(r"ROLLBACK"),
    ),
}


def _reject(reason: str) -> SourceError:
    return SourceError("statement_blocked", f"语句不在白名单里：{reason}")


def _skip_quoted(sql: str, start: int, quote: str, *, backslash_ambiguous: bool) -> int:
    """从开引号后一位扫到闭引号后一位；引号写两次是转义。"""
    index = start
    while index < len(sql):
        char = sql[index]
        if char == "\\" and backslash_ambiguous:
            raise _reject("字符串里有反斜杠，服务端怎么解析不确定")
        if char == quote:
            if index + 1 < len(sql) and sql[index + 1] == quote:
                index += 2
                continue
            return index + 1
        index += 1
    raise _reject("引号没有闭合")


def _skip_block_comment(sql: str, start: int, *, nested: bool) -> int:
    depth = 1
    index = start
    while index < len(sql):
        if sql.startswith("/*", index):
            if not nested:
                raise _reject("注释里套注释")
            depth += 1
            index += 2
        elif sql.startswith("*/", index):
            depth -= 1
            index += 2
            if depth == 0:
                return index
        else:
            index += 1
    raise _reject("注释没有闭合")


def _dash_comment_follows(sql: str, after: int, mysql: bool) -> bool:
    """`--` 后面（after 处）能不能算行注释：MySQL 要求后面是空白或到了结尾，其他方言都算。"""
    return not mysql or after >= len(sql) or sql[after] in " \t\r\n\f\v"


def skeleton(sql: str, dialect: str) -> str:
    """去掉注释（换成空格）、字符串（换成 `?`）、带引号的标识符（换成 `_q_`），剩下的就是要断词的骨架。"""
    if dialect not in DIALECTS:
        raise ValueError(f"未知方言：{dialect}")
    mysql = dialect == "mysql"
    out: list[str] = []
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        nxt = sql[index + 1] if index + 1 < length else ""
        if char == "'":
            index = _skip_quoted(sql, index + 1, "'", backslash_ambiguous=True)
            out.append(" ? ")
        elif char == '"':
            # MySQL 里默认是字符串，ANSI_QUOTES 下是标识符；PostgreSQL 里是标识符。两种都不是关键字。
            index = _skip_quoted(sql, index + 1, '"', backslash_ambiguous=mysql)
            out.append(" _q_ ")
        elif char == "`" and mysql:
            index = _skip_quoted(sql, index + 1, "`", backslash_ambiguous=False)
            out.append(" _q_ ")
        elif char == "/" and nxt == "*":
            if mysql and index + 2 < length and sql[index + 2] in "!+":
                raise _reject("MySQL 可执行注释或优化器提示")
            index = _skip_block_comment(sql, index + 2, nested=not mysql)
            out.append(" ")
        elif char == "-" and nxt == "-" and _dash_comment_follows(sql, index + 2, mysql):
            # MySQL 里 `--` 后面要跟空白才是注释，否则是两个减号，照代码继续断词
            end = sql.find("\n", index)
            index = length if end < 0 else end + 1
            out.append(" ")
        elif char == "#" and mysql:
            end = sql.find("\n", index)
            index = length if end < 0 else end + 1
            out.append(" ")
        elif char == "$" and not mysql:
            raise _reject("美元引号或占位符")
        elif char == "\\":
            raise _reject("语句里有反斜杠")
        else:
            out.append(char)
            index += 1
    return "".join(out)


def check(sql: str, dialect: str) -> str:
    """放行就返回语句类型（首个关键字小写），否则抛 `SourceError("statement_blocked")`。"""
    if not isinstance(sql, str) or not sql.strip():
        raise _reject("空语句")
    body = skeleton(sql, dialect)
    head, sep, tail = body.partition(";")
    if sep and tail.strip():
        raise _reject("只能有一条语句")
    words = [word.upper() for word in _WORD.findall(head)]
    if not words or words[0] not in ALLOWED_FIRST:
        raise _reject(f"首个关键字是 {words[0] if words else '空'}")
    denied = sorted({word for word in words if word in DENIED_WORDS})
    if denied:
        raise _reject("含 " + "、".join(denied))
    for first, second in zip(words, words[1:]):
        if first == "FOR" and second in ("SHARE", "KEY", "NO"):
            raise _reject("加锁子句")
    found = _DENIED_FUNCTION.search(head)
    if found:
        raise _reject(f"调用 {found.group(0).rstrip('( ').lower()}")
    return words[0].lower()


def check_session(sql: str, dialect: str) -> str:
    """会话设置语句只认固定写法（只读会话、超时）。"""
    for pattern in _SESSION.get(dialect, ()):
        if pattern.fullmatch(sql):
            return sql.split(None, 1)[0].lower()
    raise _reject("不是认可的会话设置")
