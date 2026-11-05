"""db-mysql 提供方（ADR-0084；证据采集接口 §8、§9）。驱动 PyMySQL（纯 Python，MIT），只在网关子进程里导入。

锚点里的 `<库>` 是 MySQL 的数据库名；没写范围时只采连接里那个库。
"""

from __future__ import annotations

import re

from . import catalog, db_common
from .context import ConnError, Context
from .db_common import (CONNECT_TIMEOUT_S, Column, Constraint, Dialect, GuardedCursor, Routine, Table, Trigger, as_int,
                        scrub, text)

INFO = dict(catalog.PROVIDERS["db-mysql"])

_SYSTEM_SCHEMAS = "('mysql', 'information_schema', 'performance_schema', 'sys')"
# 只读账号允许有的权限；其余（含 EXECUTE、TRIGGER、GRANT OPTION、各种 *_ADMIN）都算不只读
_READ_PRIVILEGES = {"USAGE", "SELECT", "SHOW VIEW", "SHOW DATABASES", "SHOW_ROUTINE", "PROCESS", "REPLICATION CLIENT"}
_NUMERIC = {"tinyint", "smallint", "mediumint", "int", "integer", "bigint", "decimal", "numeric", "float", "double",
            "real"}
_TEMPORAL = {"date", "datetime", "timestamp", "time", "year"}
_BINARY = {"binary", "varbinary", "blob", "tinyblob", "mediumblob", "longblob", "bit", "geometry", "point",
           "linestring", "polygon", "multipoint", "multilinestring", "multipolygon", "geometrycollection",
           "geomcollection", "vector"}
_ERRORS = {
    2003: ("connect_failed", "连不上数据库（地址、端口或网络）"),
    2005: ("connect_failed", "连不上数据库（主机名解析不了）"),
    2006: ("connect_failed", "和数据库的连接断了"),
    2013: ("connect_failed", "和数据库的连接断了"),
    1045: ("auth_failed", "用户名或密码不对"),
    1044: ("auth_failed", "账号没有这个库的权限"),
    1049: ("database_not_found", "库不存在"),
    3024: ("query_timeout", "查询超时"),
    1792: ("readonly_violation", "只读事务里不能写"),
}


def _in(values: list[str]) -> str:
    return "(" + ", ".join(["%s"] * len(values)) + ")"


def _literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


class MySQL(Dialect):
    name = "mysql"
    label = "MySQL"
    provider_id = INFO["id"]
    version = INFO["version"]
    methods = {
        "table": "information_schema.tables", "column": "information_schema.columns",
        "constraint": "information_schema.table_constraints", "check": "information_schema.check_constraints",
        "view": "information_schema.views", "trigger": "information_schema.triggers",
        "routine": "information_schema.routines", "profile": "select-limit", "sample": "select-limit",
    }
    session_statements_recover = ("ROLLBACK", "START TRANSACTION READ ONLY")

    def connect(self, ctx: Context):
        import pymysql

        connection = ctx.connection
        if not connection.get("host"):
            raise ConnError("invalid_connection", "没填主机")
        timeout = ctx.query_timeout_s() + 10
        # 不开 MULTI_STATEMENTS（client_flag=0），不开 LOCAL INFILE；服务端支持 TLS 时 PyMySQL 默认就加密
        return pymysql.connect(
            host=str(connection["host"]), port=int(connection.get("port") or INFO["default_port"]),
            user=str(connection.get("username") or ""), password=str(ctx.secret.get("password") or ""),
            database=str(connection.get("database") or "") or None, charset="utf8mb4", autocommit=True,
            connect_timeout=CONNECT_TIMEOUT_S, read_timeout=timeout, write_timeout=timeout, client_flag=0,
            local_infile=False, program_name="onto-connector",
        )

    def session_statements(self, ctx: Context) -> list[str]:
        seconds = ctx.query_timeout_s()
        return [
            "SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED",
            "SET SESSION TRANSACTION READ ONLY",
            f"SET SESSION max_execution_time = {seconds * 1000}",
            f"SET SESSION lock_wait_timeout = {seconds}",
            "START TRANSACTION READ ONLY",
        ]

    def default_scope(self, ctx: Context) -> list[str]:
        database = str(ctx.connection.get("database") or "")
        return [f"{database}.*"] if database else []

    def quote(self, ident: str) -> str:
        return "`" + str(ident).replace("`", "``") + "`"

    def version_sql(self) -> str:
        return "SELECT VERSION()"

    def read_only_probe_sql(self) -> str:
        return "SELECT @@transaction_read_only"

    def list_tables(self, cur: GuardedCursor) -> list[Table]:
        rows = cur.query(
            "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE, TABLE_ROWS, DATA_LENGTH + INDEX_LENGTH, TABLE_COMMENT "
            f"FROM information_schema.TABLES WHERE TABLE_SCHEMA NOT IN {_SYSTEM_SCHEMAS} "
            "ORDER BY TABLE_SCHEMA, TABLE_NAME")
        tables = []
        for schema, name, kind, rows_estimate, size, comment in rows:
            kind = text(kind)
            if kind not in ("BASE TABLE", "VIEW"):
                continue
            view = kind == "VIEW"
            tables.append(Table(text(schema), text(name), "view" if view else "table",
                                "" if view else text(comment), None if view else as_int(rows_estimate),
                                None if view else as_int(size)))
        return tables

    def readonly_check(self, cur: GuardedCursor, tables: list[Table]) -> tuple[bool, str]:
        return grants_readonly([text(row[0]) for row in cur.query("SHOW GRANTS")])

    def columns(self, cur: GuardedCursor, schemas: list[str]) -> list[Column]:
        rows = cur.query(
            "SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, ORDINAL_POSITION, COLUMN_DEFAULT, IS_NULLABLE, DATA_TYPE, "
            "COLUMN_TYPE, EXTRA, COLUMN_COMMENT FROM information_schema.COLUMNS "
            f"WHERE TABLE_SCHEMA IN {_in(schemas)} ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION", schemas)
        out = []
        for schema, table, name, position, default, nullable, data_type, column_type, extra, comment in rows:
            base = text(data_type).lower()
            type_class = ("numeric" if base in _NUMERIC else "temporal" if base in _TEMPORAL
                          else "binary" if base in _BINARY else "text")
            out.append(Column(text(schema), text(table), text(name), as_int(position) or 0, text(column_type),
                              text(nullable).upper() == "YES", None if default is None else text(default),
                              text(comment), type_class, text(extra)))
        return out

    def constraints(self, cur: GuardedCursor, schemas: list[str]) -> list[Constraint]:
        rows = cur.query(
            "SELECT tc.TABLE_SCHEMA, tc.TABLE_NAME, tc.CONSTRAINT_NAME, tc.CONSTRAINT_TYPE, k.COLUMN_NAME, "
            "k.REFERENCED_TABLE_SCHEMA, k.REFERENCED_TABLE_NAME, k.REFERENCED_COLUMN_NAME, r.UPDATE_RULE, r.DELETE_RULE "
            "FROM information_schema.TABLE_CONSTRAINTS tc JOIN information_schema.KEY_COLUMN_USAGE k "
            "ON k.CONSTRAINT_SCHEMA = tc.CONSTRAINT_SCHEMA AND k.CONSTRAINT_NAME = tc.CONSTRAINT_NAME "
            "AND k.TABLE_SCHEMA = tc.TABLE_SCHEMA AND k.TABLE_NAME = tc.TABLE_NAME "
            "LEFT JOIN information_schema.REFERENTIAL_CONSTRAINTS r ON r.CONSTRAINT_SCHEMA = tc.CONSTRAINT_SCHEMA "
            "AND r.CONSTRAINT_NAME = tc.CONSTRAINT_NAME AND r.TABLE_NAME = tc.TABLE_NAME "
            f"WHERE tc.TABLE_SCHEMA IN {_in(schemas)} AND tc.CONSTRAINT_TYPE IN ('PRIMARY KEY', 'UNIQUE', 'FOREIGN KEY') "
            "ORDER BY tc.TABLE_SCHEMA, tc.TABLE_NAME, tc.CONSTRAINT_NAME, k.ORDINAL_POSITION", schemas)
        kinds = {"PRIMARY KEY": "pk", "UNIQUE": "uk", "FOREIGN KEY": "fk"}
        found: dict[tuple, Constraint] = {}
        for schema, table, name, kind, column, ref_schema, ref_table, ref_column, on_update, on_delete in rows:
            key = (text(schema), text(table), text(name), text(kind))
            item = found.get(key)
            if item is None:
                item = Constraint(key[0], key[1], key[2], kinds[key[3]], ref_schema=text(ref_schema) or None,
                                  ref_table=text(ref_table) or None, on_update=text(on_update) or None,
                                  on_delete=text(on_delete) or None)
                found[key] = item
            item.columns.append(text(column))
            if ref_column is not None:
                item.ref_columns.append(text(ref_column))
        out = list(found.values())
        try:
            checks = cur.query(
                "SELECT tc.TABLE_SCHEMA, tc.TABLE_NAME, tc.CONSTRAINT_NAME, cc.CHECK_CLAUSE "
                "FROM information_schema.TABLE_CONSTRAINTS tc JOIN information_schema.CHECK_CONSTRAINTS cc "
                "ON cc.CONSTRAINT_SCHEMA = tc.CONSTRAINT_SCHEMA AND cc.CONSTRAINT_NAME = tc.CONSTRAINT_NAME "
                f"WHERE tc.TABLE_SCHEMA IN {_in(schemas)} AND tc.CONSTRAINT_TYPE = 'CHECK' "
                "ORDER BY tc.TABLE_SCHEMA, tc.TABLE_NAME, tc.CONSTRAINT_NAME", schemas)
        except ConnError as exc:
            if exc.code in ("timeout", "budget_exceeded"):
                raise
            cur.ctx.limitation("读不到检查约束（MySQL 8.0.16 以前没有 CHECK_CONSTRAINTS）")
            checks = []
        for schema, table, name, clause in checks:
            expression = text(clause).replace("\\'", "'")  # MySQL 存的检查表达式里引号带反斜杠
            columns = sorted({match.replace("``", "`") for match in re.findall(r"`((?:[^`]|``)+)`", expression)})
            out.append(Constraint(text(schema), text(table), text(name), "check", columns, expression))
        return out

    def views(self, cur: GuardedCursor, views: list[Table]) -> dict[tuple[str, str], str]:
        schemas = sorted({view.schema for view in views})
        rows = cur.query("SELECT TABLE_SCHEMA, TABLE_NAME, VIEW_DEFINITION FROM information_schema.VIEWS "
                         f"WHERE TABLE_SCHEMA IN {_in(schemas)}", schemas)
        return {(text(schema), text(name)): text(definition) or None for schema, name, definition in rows}

    def triggers(self, cur: GuardedCursor, schemas: list[str]) -> list[Trigger]:
        rows = cur.query(
            "SELECT EVENT_OBJECT_SCHEMA, EVENT_OBJECT_TABLE, TRIGGER_NAME, ACTION_TIMING, EVENT_MANIPULATION, "
            "ACTION_STATEMENT FROM information_schema.TRIGGERS "
            f"WHERE EVENT_OBJECT_SCHEMA IN {_in(schemas)} ORDER BY EVENT_OBJECT_SCHEMA, EVENT_OBJECT_TABLE, ACTION_ORDER",
            schemas)
        if not rows:
            # information_schema.TRIGGERS 只列账号有 TRIGGER 权限的表；TRIGGER 权限能建删触发器，自检按写权限处理
            cur.ctx.limitation("MySQL 只读账号看不到触发器（要 TRIGGER 权限，它能建删触发器），触发器可能没采全")
        out = []
        for schema, table, name, timing, event, statement in rows:
            header = (f"CREATE TRIGGER {self.quote(text(name))} {text(timing)} {text(event)} ON "
                      f"{self.quote(text(table))} FOR EACH ROW ")
            out.append(Trigger(text(schema), text(table), text(name), text(timing), [text(event)],
                               header + text(statement)))
        return out

    def routines(self, cur: GuardedCursor, schemas: list[str]) -> list[Routine]:
        rows = cur.query(
            "SELECT ROUTINE_SCHEMA, ROUTINE_NAME, ROUTINE_TYPE, ROUTINE_DEFINITION, ROUTINE_COMMENT, ROUTINE_BODY "
            f"FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA IN {_in(schemas)} "
            "ORDER BY ROUTINE_SCHEMA, ROUTINE_NAME, ROUTINE_TYPE", schemas)
        if not rows:
            return []
        params = cur.query(
            "SELECT SPECIFIC_SCHEMA, SPECIFIC_NAME, ROUTINE_TYPE, PARAMETER_MODE, PARAMETER_NAME, DTD_IDENTIFIER "
            f"FROM information_schema.PARAMETERS WHERE SPECIFIC_SCHEMA IN {_in(schemas)} AND ORDINAL_POSITION > 0 "
            "ORDER BY SPECIFIC_SCHEMA, SPECIFIC_NAME, ORDINAL_POSITION", schemas)
        arguments: dict[tuple, list[str]] = {}
        for schema, name, kind, mode, param, dtd in params:
            piece = " ".join(part for part in (text(mode), text(param), text(dtd)) if part)
            arguments.setdefault((text(schema), text(name), text(kind)), []).append(piece)
        return [Routine(text(schema), text(name), text(kind).lower(), None if definition is None else text(definition),
                        text(comment), ", ".join(arguments.get((text(schema), text(name), text(kind)), [])),
                        text(body).lower())
                for schema, name, kind, definition, comment, body in rows]

    def select_expr(self, column: Column) -> str:
        quoted = self.quote(column.name)
        if column.type_class == "binary":
            return f"({quoted} IS NULL)"
        if column.type_class == "text":
            return f"SUBSTRING({quoted}, 1, {db_common.TEXT_PREFIX})"
        return quoted

    def ddl(self, table: Table, columns: list[Column], constraints: list[Constraint]) -> str:
        q = self.quote
        lines = []
        for column in columns:
            line = f"  {q(column.name)} {column.data_type}"
            if not column.nullable:
                line += " NOT NULL"
            extra = column.extra.replace("DEFAULT_GENERATED", "").strip()
            if column.default is not None:
                expression = ("DEFAULT_GENERATED" in column.extra or column.type_class == "numeric"
                              or column.default.upper() in ("NULL", "CURRENT_TIMESTAMP"))
                line += " DEFAULT " + (column.default if expression else _literal(column.default))
            if extra:
                line += " " + extra.upper()
            if column.comment:
                line += " COMMENT " + _literal(column.comment)
            lines.append(line)
        for item in db_common.ddl_order(constraints):
            names = ", ".join(q(name) for name in item.columns)
            if item.kind == "pk":
                lines.append(f"  PRIMARY KEY ({names})")
            elif item.kind == "uk":
                lines.append(f"  UNIQUE KEY {q(item.name)} ({names})")
            elif item.kind == "fk":
                refs = ", ".join(q(name) for name in item.ref_columns)
                line = (f"  CONSTRAINT {q(item.name)} FOREIGN KEY ({names}) REFERENCES "
                        f"{q(item.ref_schema or table.schema)}.{q(item.ref_table or '')} ({refs})")
                if item.on_delete and item.on_delete != "NO ACTION":
                    line += f" ON DELETE {item.on_delete}"
                if item.on_update and item.on_update != "NO ACTION":
                    line += f" ON UPDATE {item.on_update}"
                lines.append(line)
            elif item.kind == "check":
                lines.append(f"  CONSTRAINT {q(item.name)} CHECK ({item.expression})")
        tail = f" COMMENT={_literal(table.comment)}" if table.comment else ""
        return f"CREATE TABLE {q(table.schema)}.{q(table.name)} (\n" + ",\n".join(lines) + f"\n){tail};"

    def error(self, exc: BaseException, ctx: Context) -> ConnError:
        args = getattr(exc, "args", ())
        if type(exc).__module__.startswith("pymysql") and args and isinstance(args[0], int):
            code, message = _ERRORS.get(args[0], ("db_error", "数据库报错"))
            detail = scrub(args[1], ctx) if len(args) > 1 else ""
            return ConnError(code, f"{message}（MySQL {args[0]}：{detail}）" if detail else message)
        if isinstance(exc, (TimeoutError, OSError)):
            return ConnError("connect_failed", "连不上数据库（地址、端口或网络）")
        return ConnError("db_error", f"MySQL 出错：{type(exc).__name__}：{scrub(exc, ctx)}")


def grants_readonly(lines: list[str]) -> tuple[bool, str]:
    """按 `SHOW GRANTS` 判断账号只读：只允许 SELECT 类权限；经角色授权的看不到角色里的权限，按不只读处理。"""
    found: set[str] = set()
    bad: set[str] = set()
    roles = False
    for line in lines:
        match = re.match(r"GRANT (.+?) ON .+? TO ", line)
        if not match:
            roles = roles or bool(re.match(r"GRANT .+ TO ", line))
            continue
        for privilege in re.sub(r"\([^)]*\)", "", match.group(1)).split(","):
            privilege = " ".join(privilege.split()).upper()
            if not privilege:
                continue
            found.add(privilege)
            if privilege not in _READ_PRIVILEGES:
                bad.add(privilege)
        if "WITH GRANT OPTION" in line.upper():
            bad.add("GRANT OPTION")
    if bad:
        return False, "账号有写或管理权限：" + "、".join(sorted(bad))
    if roles:
        return False, "账号经角色授权，自检看不到角色里的权限；请直接授 SELECT、SHOW VIEW"
    readable = sorted(found - {"USAGE"})
    if not readable:
        return False, "账号没有查询权限"
    return True, "只有只读权限：" + "、".join(readable)


DIALECT = MySQL()


def test(ctx: Context) -> None:
    db_common.test(ctx, DIALECT)


def discover(ctx: Context) -> None:
    db_common.discover(ctx, DIALECT)


def snapshot(ctx: Context) -> None:
    db_common.snapshot(ctx, DIALECT)


def sample(ctx: Context) -> None:
    db_common.sample(ctx, DIALECT)
