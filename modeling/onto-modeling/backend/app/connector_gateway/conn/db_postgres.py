"""db-postgres 提供方（ADR-0084；证据采集接口 §8、§9）。驱动 pg8000（纯 Python，BSD-3-Clause），只在网关子进程里导入。

锚点里的 `<库>` 是 schema 名；连接里的 `database` 是库名。没写范围时采这个库里除系统 schema 外的全部。
"""

from __future__ import annotations

import re

from . import catalog, db_common
from .context import ConnError, Context
from .db_common import (CONNECT_TIMEOUT_S, Column, Constraint, Dialect, GuardedCursor, Routine, Table, Trigger, as_int,
                        scrub, text)

INFO = dict(catalog.PROVIDERS["db-postgres"])

_WRITE_ROLES = "('pg_write_all_data', 'pg_write_server_files', 'pg_execute_server_program')"
_ACTIONS = {"a": "NO ACTION", "r": "RESTRICT", "c": "CASCADE", "n": "SET NULL", "d": "SET DEFAULT"}
_ERRORS = {
    "28P01": ("auth_failed", "用户名或密码不对"),
    "28000": ("auth_failed", "账号不允许登录这个库"),
    "3D000": ("database_not_found", "库不存在"),
    "57014": ("query_timeout", "查询超时"),
    "55P03": ("query_timeout", "等锁超时"),
    "25006": ("readonly_violation", "只读事务里不能写"),
    "42501": ("permission_denied", "账号没有权限"),
}


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


class Postgres(Dialect):
    name = "postgres"
    label = "PostgreSQL"
    provider_id = INFO["id"]
    version = INFO["version"]
    methods = {
        "table": "pg_catalog.pg_class", "column": "pg_catalog.pg_attribute", "constraint": "pg_catalog.pg_constraint",
        "check": "pg_catalog.pg_constraint", "view": "pg_get_viewdef", "trigger": "pg_catalog.pg_trigger",
        "routine": "pg_catalog.pg_proc", "profile": "select-limit", "sample": "select-limit",
    }
    session_statements_recover = ("ROLLBACK", "BEGIN READ ONLY")

    def connect(self, ctx: Context):
        import pg8000.dbapi

        connection = ctx.connection
        if not connection.get("host"):
            raise ConnError("invalid_connection", "没填主机")
        conn = pg8000.dbapi.connect(
            user=str(connection.get("username") or ""), password=str(ctx.secret.get("password") or ""),
            host=str(connection["host"]), port=int(connection.get("port") or INFO["default_port"]),
            database=str(connection.get("database") or "") or None, timeout=CONNECT_TIMEOUT_S,
            application_name="onto-connector")
        # 自己发 BEGIN READ ONLY，不让驱动隐式开读写事务
        conn.autocommit = True
        sock = getattr(conn, "_usock", None)
        if sock is not None:
            # 连接超时 10 秒；连上后的读超时放宽到语句超时之外，由服务端的 statement_timeout 先生效
            sock.settimeout(ctx.query_timeout_s() + 10)
        return conn

    def session_statements(self, ctx: Context) -> list[str]:
        millis = ctx.query_timeout_s() * 1000
        return [
            "SET default_transaction_read_only = on",
            f"SET statement_timeout = {millis}",
            f"SET lock_timeout = {millis}",
            "BEGIN READ ONLY",
        ]

    def quote(self, ident: str) -> str:
        return '"' + str(ident).replace('"', '""') + '"'

    def version_sql(self) -> str:
        return "SELECT current_setting('server_version')"

    def read_only_probe_sql(self) -> str:
        return "SELECT current_setting('transaction_read_only')"

    def list_tables(self, cur: GuardedCursor) -> list[Table]:
        rows = cur.query(
            "SELECT n.nspname, c.relname, c.relkind, CASE WHEN c.reltuples < 0 THEN NULL ELSE c.reltuples::bigint END, "
            "pg_total_relation_size(c.oid), obj_description(c.oid, 'pg_class'), "
            "has_schema_privilege(n.oid, 'USAGE') AND has_table_privilege(c.oid, 'SELECT'), "
            "has_table_privilege(c.oid, 'INSERT, UPDATE, DELETE, TRUNCATE, TRIGGER'), c.oid::bigint "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relkind IN ('r', 'p', 'v', 'm') AND NOT c.relispartition "
            "AND n.nspname NOT IN ('pg_catalog', 'information_schema') AND left(n.nspname, 3) <> 'pg_' "
            "ORDER BY n.nspname, c.relname")
        tables = []
        for schema, name, kind, rows_estimate, size, comment, readable, writable, oid in rows:
            view = text(kind) in ("v", "m")
            tables.append(Table(text(schema), text(name), "view" if view else "table", text(comment),
                                None if view else as_int(rows_estimate), None if view else as_int(size),
                                bool(readable), bool(writable), as_int(oid)))
        return tables

    def readonly_check(self, cur: GuardedCursor, tables: list[Table]) -> tuple[bool, str]:
        rows = cur.query(
            "SELECT r.rolsuper, r.rolcreaterole, r.rolcreatedb, ARRAY(SELECT g.rolname::text FROM pg_roles g "
            f"WHERE g.rolname IN {_WRITE_ROLES} AND pg_has_role(r.oid, g.oid, 'MEMBER')) "
            "FROM pg_roles r WHERE r.rolname = current_user")
        problems = []
        if rows:
            superuser, create_role, create_db, roles = rows[0]
            if superuser:
                problems.append("是超级用户")
            if create_role:
                problems.append("能建角色")
            if create_db:
                problems.append("能建库")
            if roles:
                problems.append("属于 " + "、".join(sorted(text(role) for role in roles)))
        schemas = sorted({table.schema for table in tables})
        if schemas:
            creatable = cur.query("SELECT nspname::text FROM pg_namespace WHERE nspname::text = ANY(%s::text[]) "
                                  "AND has_schema_privilege(oid, 'CREATE') ORDER BY nspname", [schemas])
            if creatable:
                problems.append("能在 " + "、".join(text(row[0]) for row in creatable) + " 里建对象")
        writable = [table.path for table in tables if table.writable]
        if writable:
            shown = "、".join(writable[:5]) + (f" 等 {len(writable)} 张表" if len(writable) > 5 else " ")
            problems.append(f"对 {shown}有写权限")
        if problems:
            return False, "账号不只读：" + "；".join(problems)
        if not any(table.readable for table in tables):
            return False, "范围内没有能查的表"
        return True, "没有写权限、建对象权限，也不是超级用户"

    def columns(self, cur: GuardedCursor, schemas: list[str]) -> list[Column]:
        rows = cur.query(
            "SELECT n.nspname, c.relname, a.attname, a.attnum, pg_get_expr(d.adbin, d.adrelid), NOT a.attnotnull, "
            "format_type(a.atttypid, a.atttypmod), t.typcategory::text, t.typname, col_description(c.oid, a.attnum), "
            "a.attidentity::text FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace JOIN pg_type t ON t.oid = a.atttypid "
            "LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
            "WHERE a.attnum > 0 AND NOT a.attisdropped AND c.relkind IN ('r', 'p', 'v', 'm') "
            "AND n.nspname::text = ANY(%s::text[]) ORDER BY n.nspname, c.relname, a.attnum", [schemas])
        out = []
        for schema, table, name, position, default, nullable, data_type, category, type_name, comment, identity in rows:
            category = text(category)
            type_class = ("numeric" if category == "N" else "bool" if category == "B"
                          else "binary" if text(type_name) == "bytea" else "temporal" if category == "D" else "text")
            extra = {"a": "GENERATED ALWAYS AS IDENTITY", "d": "GENERATED BY DEFAULT AS IDENTITY"}.get(text(identity), "")
            out.append(Column(text(schema), text(table), text(name), as_int(position) or 0, text(data_type),
                              bool(nullable), None if default is None else text(default), text(comment), type_class,
                              extra))
        return out

    def constraints(self, cur: GuardedCursor, schemas: list[str]) -> list[Constraint]:
        rows = cur.query(
            "SELECT n.nspname, c.relname, con.conname, con.contype::text, pg_get_constraintdef(con.oid, true), "
            "ARRAY(SELECT a.attname::text FROM unnest(con.conkey) WITH ORDINALITY AS k(num, ord) "
            "JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.num ORDER BY k.ord), "
            "fn.nspname, fc.relname, "
            "ARRAY(SELECT a.attname::text FROM unnest(con.confkey) WITH ORDINALITY AS k(num, ord) "
            "JOIN pg_attribute a ON a.attrelid = con.confrelid AND a.attnum = k.num ORDER BY k.ord), "
            "con.confupdtype::text, con.confdeltype::text "
            "FROM pg_constraint con JOIN pg_class c ON c.oid = con.conrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace LEFT JOIN pg_class fc ON fc.oid = con.confrelid "
            "LEFT JOIN pg_namespace fn ON fn.oid = fc.relnamespace "
            "WHERE con.contype IN ('p', 'u', 'f', 'c') AND n.nspname::text = ANY(%s::text[]) "
            "ORDER BY n.nspname, c.relname, con.conname", [schemas])
        kinds = {"p": "pk", "u": "uk", "f": "fk", "c": "check"}
        out = []
        for schema, table, name, kind, definition, columns, ref_schema, ref_table, ref_columns, on_update, on_delete in rows:
            kind = kinds[text(kind)]
            definition = text(definition)
            expression = None
            if kind == "check":
                match = re.match(r"CHECK \((.*)\)(?: NOT VALID)?$", definition, re.S)
                expression = match.group(1) if match else definition
            out.append(Constraint(
                text(schema), text(table), text(name), kind, [text(item) for item in columns or []], expression,
                definition, text(ref_schema) or None, text(ref_table) or None,
                [text(item) for item in ref_columns or []],
                _ACTIONS.get(text(on_update)) if kind == "fk" else None,
                _ACTIONS.get(text(on_delete)) if kind == "fk" else None))
        return out

    def views(self, cur: GuardedCursor, views: list[Table]) -> dict[tuple[str, str], str]:
        oids = [view.key for view in views if view.key is not None]
        if not oids:
            return {}
        rows = cur.query("SELECT n.nspname, c.relname, pg_get_viewdef(c.oid, true) FROM pg_class c "
                         "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.oid::bigint = ANY(%s::bigint[])", [oids])
        return {(text(schema), text(name)): text(definition) or None for schema, name, definition in rows}

    def triggers(self, cur: GuardedCursor, schemas: list[str]) -> list[Trigger]:
        rows = cur.query(
            "SELECT n.nspname, c.relname, t.tgname, t.tgtype::integer, pg_get_triggerdef(t.oid, true), pn.nspname, "
            "p.proname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace JOIN pg_proc p ON p.oid = t.tgfoid "
            "JOIN pg_namespace pn ON pn.oid = p.pronamespace "
            "WHERE NOT t.tgisinternal AND n.nspname::text = ANY(%s::text[]) ORDER BY n.nspname, c.relname, t.tgname",
            [schemas])
        out = []
        for schema, table, name, kind, definition, function_schema, function_name in rows:
            bits = as_int(kind) or 0
            timing = "INSTEAD OF" if bits & 64 else "BEFORE" if bits & 2 else "AFTER"
            events = [event for bit, event in ((4, "INSERT"), (16, "UPDATE"), (8, "DELETE"), (32, "TRUNCATE"))
                      if bits & bit]
            out.append(Trigger(text(schema), text(table), text(name), timing, events, text(definition),
                               (text(function_schema), text(function_name))))
        return out

    def routines(self, cur: GuardedCursor, schemas: list[str]) -> list[Routine]:
        rows = cur.query(
            "SELECT n.nspname, p.proname, p.prokind::text, pg_get_function_identity_arguments(p.oid), "
            "pg_get_functiondef(p.oid), obj_description(p.oid, 'pg_proc'), l.lanname "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace JOIN pg_language l ON l.oid = p.prolang "
            "WHERE p.prokind IN ('f', 'p') AND n.nspname::text = ANY(%s::text[]) AND NOT EXISTS "
            "(SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_proc'::regclass AND d.objid = p.oid AND d.deptype = 'e') "
            "ORDER BY n.nspname, p.proname, p.oid", [schemas])
        return [Routine(text(schema), text(name), "procedure" if text(kind) == "p" else "function",
                        None if definition is None else text(definition), text(comment), text(arguments), text(language))
                for schema, name, kind, arguments, definition, comment, language in rows]

    def select_expr(self, column: Column) -> str:
        quoted = self.quote(column.name)
        if column.type_class == "binary":
            return f"({quoted} IS NULL)"
        if column.type_class in ("numeric", "bool"):
            return quoted
        return f"left({quoted}::text, {db_common.TEXT_PREFIX})"

    def ddl(self, table: Table, columns: list[Column], constraints: list[Constraint]) -> str:
        q = self.quote
        name = f"{q(table.schema)}.{q(table.name)}"
        lines = []
        for column in columns:
            line = f"  {q(column.name)} {column.data_type}"
            if column.extra:
                line += " " + column.extra
            if not column.nullable:
                line += " NOT NULL"
            if column.default is not None:
                line += " DEFAULT " + column.default
            lines.append(line)
        for item in db_common.ddl_order(constraints):
            lines.append(f"  CONSTRAINT {q(item.name)} {item.definition}")
        statements = [f"CREATE TABLE {name} (\n" + ",\n".join(lines) + "\n);"]
        if table.comment:
            statements.append(f"COMMENT ON TABLE {name} IS {_literal(table.comment)};")
        for column in columns:
            if column.comment:
                statements.append(f"COMMENT ON COLUMN {name}.{q(column.name)} IS {_literal(column.comment)};")
        return "\n".join(statements)

    def error(self, exc: BaseException, ctx: Context) -> ConnError:
        args = getattr(exc, "args", ())
        if type(exc).__module__.startswith("pg8000"):
            info = args[0] if args and isinstance(args[0], dict) else None
            if info is None:
                return ConnError("connect_failed", "连不上数据库（地址、端口或网络）")
            state = str(info.get("C") or "")
            if state.startswith("08"):
                return ConnError("connect_failed", "和数据库的连接出错")
            code, message = _ERRORS.get(state, ("db_error", "数据库报错"))
            detail = scrub(info.get("M") or "", ctx)
            return ConnError(code, f"{message}（PostgreSQL {state}：{detail}）" if detail else message)
        if isinstance(exc, (TimeoutError, OSError)):
            return ConnError("connect_failed", "连不上数据库（地址、端口或网络）")
        return ConnError("db_error", f"PostgreSQL 出错：{type(exc).__name__}：{scrub(exc, ctx)}")


DIALECT = Postgres()


def test(ctx: Context) -> None:
    db_common.test(ctx, DIALECT)


def discover(ctx: Context) -> None:
    db_common.discover(ctx, DIALECT)


def snapshot(ctx: Context) -> None:
    db_common.snapshot(ctx, DIALECT)


def sample(ctx: Context) -> None:
    db_common.sample(ctx, DIALECT)
