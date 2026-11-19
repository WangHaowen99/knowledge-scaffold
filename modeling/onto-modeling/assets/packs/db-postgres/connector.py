"""PostgreSQL 数据源连接器（ADR-0104）。驱动 pg8000（纯 Python，BSD-3-Clause），随连接器网关镜像安装，只在网关子进程里导入。

方言之外的流程在平台的 `dbkit` 里。连接里的 `database` 是库名，表名前缀是 schema；范围里不带点的写法在全部 schema 里
匹配表名；没填范围就采这个库里除系统 schema 外的全部。
"""

from __future__ import annotations

import re

from app.connector_gateway.source import dbkit
from app.connector_gateway.source.dbkit import Column, Constraint, GuardedCursor, Routine, Table, Trigger, as_int, text
from app.connector_gateway.source.sdk import SourceError

_WRITE_ROLES = "('pg_write_all_data', 'pg_write_server_files', 'pg_execute_server_program')"
_ERRORS = {
    "28P01": ("auth_failed", "用户名或密码不对"),
    "28000": ("auth_failed", "账号不允许登录这个库"),
    "3D000": ("database_not_found", "库不存在"),
    "57014": ("query_timeout", "查询超时"),
    "55P03": ("query_timeout", "等锁超时"),
    "25006": ("readonly_violation", "只读事务里不能写"),
    "42501": ("permission_denied", "账号没有权限"),
}


class Postgres(dbkit.Dialect):
    name = "postgres"
    label = "PostgreSQL"
    session_statements_recover = ("ROLLBACK", "BEGIN READ ONLY")

    def connect(self, ctx):
        import pg8000.dbapi

        conn = pg8000.dbapi.connect(
            user=str(ctx.config.get("user") or ""), password=str(ctx.secret.get("password") or ""),
            host=str(ctx.config["host"]), port=int(ctx.config.get("port") or 5432),
            database=str(ctx.config.get("database") or "") or None, timeout=dbkit.CONNECT_TIMEOUT_S,
            application_name="onto-connector")
        # 自己发 BEGIN READ ONLY，不让驱动隐式开读写事务
        conn.autocommit = True
        sock = getattr(conn, "_usock", None)
        if sock is not None:
            # 连上后的读超时放宽到语句超时之外，由服务端的 statement_timeout 先生效
            sock.settimeout(ctx.query_timeout_s() + 10)
        return conn

    def session_statements(self, ctx) -> list[str]:
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
            "SELECT n.nspname, c.relname, c.relkind, obj_description(c.oid, 'pg_class'), "
            "has_schema_privilege(n.oid, 'USAGE') AND has_table_privilege(c.oid, 'SELECT'), "
            "has_table_privilege(c.oid, 'INSERT, UPDATE, DELETE, TRUNCATE, TRIGGER'), c.oid::bigint "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relkind IN ('r', 'p', 'v', 'm') AND NOT c.relispartition "
            "AND n.nspname NOT IN ('pg_catalog', 'information_schema') AND left(n.nspname, 3) <> 'pg_' "
            "ORDER BY n.nspname, c.relname")
        tables = []
        for schema, name, kind, comment, readable, writable, oid in rows:
            view = text(kind) in ("v", "m")
            tables.append(Table(text(schema), text(name), "view" if view else "table", text(comment),
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
            "SELECT n.nspname, c.relname, a.attname, a.attnum, NOT a.attnotnull, format_type(a.atttypid, a.atttypmod), "
            "t.typcategory::text, t.typname, col_description(c.oid, a.attnum) "
            "FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace JOIN pg_type t ON t.oid = a.atttypid "
            "WHERE a.attnum > 0 AND NOT a.attisdropped AND c.relkind IN ('r', 'p', 'v', 'm') "
            "AND n.nspname::text = ANY(%s::text[]) ORDER BY n.nspname, c.relname, a.attnum", [schemas])
        out = []
        for schema, table, name, position, nullable, data_type, category, type_name, comment in rows:
            category = text(category)
            type_class = ("numeric" if category == "N" else "bool" if category == "B"
                          else "binary" if text(type_name) == "bytea" else "temporal" if category == "D" else "text")
            out.append(Column(text(schema), text(table), text(name), as_int(position) or 0, text(data_type),
                              bool(nullable), text(comment), type_class))
        return out

    def constraints(self, cur: GuardedCursor, schemas: list[str]) -> list[Constraint]:
        rows = cur.query(
            "SELECT n.nspname, c.relname, con.conname, con.contype::text, pg_get_constraintdef(con.oid, true), "
            "ARRAY(SELECT a.attname::text FROM unnest(con.conkey) WITH ORDINALITY AS k(num, ord) "
            "JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.num ORDER BY k.ord), "
            "fn.nspname, fc.relname, "
            "ARRAY(SELECT a.attname::text FROM unnest(con.confkey) WITH ORDINALITY AS k(num, ord) "
            "JOIN pg_attribute a ON a.attrelid = con.confrelid AND a.attnum = k.num ORDER BY k.ord) "
            "FROM pg_constraint con JOIN pg_class c ON c.oid = con.conrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace LEFT JOIN pg_class fc ON fc.oid = con.confrelid "
            "LEFT JOIN pg_namespace fn ON fn.oid = fc.relnamespace "
            "WHERE con.contype IN ('p', 'u', 'f', 'c') AND n.nspname::text = ANY(%s::text[]) "
            "ORDER BY n.nspname, c.relname, con.conname", [schemas])
        kinds = {"p": "pk", "u": "uk", "f": "fk", "c": "check"}
        out = []
        for schema, table, name, kind, definition, columns, ref_schema, ref_table, ref_columns in rows:
            code = text(kind)
            if code not in kinds:
                continue
            kind = kinds[code]
            expression = None
            if kind == "check":
                match = re.match(r"CHECK \((.*)\)(?: NOT VALID)?$", text(definition), re.S)
                expression = match.group(1) if match else text(definition)
            out.append(Constraint(text(schema), text(table), text(name), kind, [text(item) for item in columns or []],
                                  expression, text(ref_schema) or None, text(ref_table) or None,
                                  [text(item) for item in ref_columns or []]))
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
            "SELECT n.nspname, c.relname, t.tgname, t.tgtype::integer, pg_get_triggerdef(t.oid, true) "
            "FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE NOT t.tgisinternal AND n.nspname::text = ANY(%s::text[]) ORDER BY n.nspname, c.relname, t.tgname",
            [schemas])
        out = []
        for schema, table, name, kind, definition in rows:
            bits = as_int(kind) or 0
            timing = "INSTEAD OF" if bits & 64 else "BEFORE" if bits & 2 else "AFTER"
            events = [event for bit, event in ((4, "INSERT"), (16, "UPDATE"), (8, "DELETE"), (32, "TRUNCATE"))
                      if bits & bit]
            out.append(Trigger(text(schema), text(table), text(name), timing, events, text(definition)))
        return out

    def routines(self, cur: GuardedCursor, schemas: list[str]) -> list[Routine]:
        rows = cur.query(
            "SELECT n.nspname, p.proname, p.prokind::text, pg_get_function_identity_arguments(p.oid), "
            "pg_get_functiondef(p.oid) "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE p.prokind IN ('f', 'p') AND n.nspname::text = ANY(%s::text[]) AND NOT EXISTS "
            "(SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_proc'::regclass AND d.objid = p.oid AND d.deptype = 'e') "
            "ORDER BY n.nspname, p.proname, p.oid", [schemas])
        return [Routine(text(schema), text(name), "procedure" if text(kind) == "p" else "function",
                        None if definition is None else text(definition), text(arguments))
                for schema, name, kind, arguments, definition in rows]

    def select_expr(self, column: Column) -> str:
        quoted = self.quote(column.name)
        if column.type_class == "binary":
            return f"({quoted} IS NULL)"
        if column.type_class in ("numeric", "bool"):
            return quoted
        return f"left({quoted}::text, {dbkit.TEXT_PREFIX})"

    def error(self, exc: BaseException, ctx) -> SourceError:
        args = getattr(exc, "args", ())
        if type(exc).__module__.startswith("pg8000"):
            info = args[0] if args and isinstance(args[0], dict) else None
            if info is None:
                return SourceError("connect_failed", "连不上数据库（地址、端口或网络）")
            state = str(info.get("C") or "")
            if state.startswith("08"):
                return SourceError("connect_failed", "和数据库的连接出错")
            code, message = _ERRORS.get(state, ("db_error", "数据库报错"))
            detail = dbkit.scrub(info.get("M") or "", ctx)
            return SourceError(code, f"{message}（PostgreSQL {state}：{detail}）" if detail else message)
        return super().error(exc, ctx)


DIALECT = Postgres()


def test(ctx) -> None:
    dbkit.test(ctx, DIALECT)


def collect(ctx) -> None:
    dbkit.collect(ctx, DIALECT)
