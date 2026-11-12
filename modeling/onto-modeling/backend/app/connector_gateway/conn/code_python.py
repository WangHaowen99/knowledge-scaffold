"""Python 静态分析（标准库 ast，只解析、不导入不执行目标代码；证据采集接口 §10）。

认：SQLAlchemy 声明式模型（`Column` / `mapped_column` / `relationship` / `ForeignKey` / `__table_args__` /
`@validates`）、Django `models.Model`、FastAPI / Flask 路由装饰器、pydantic `BaseModel` 与 `Field` 约束和校验器、
枚举、名字以 Service / Repository 结尾的类的公开方法、函数里的 SQL 字面量。
"""

from __future__ import annotations

import ast

from .code_model import Sink, doc_label, explicit_label, join_path, looks_like_sql

LANGUAGE = "python"
HTTP = {"get", "post", "put", "delete", "patch", "head", "options"}
SA_COLUMNS = {"Column", "mapped_column"}
DJANGO_RELATIONS = {"ForeignKey": "many_to_one", "OneToOneField": "one_to_one", "ManyToManyField": "many_to_many"}
DJANGO_RULES = ("max_length", "max_digits", "decimal_places", "choices")
PYDANTIC_RULES = {"gt", "ge", "lt", "le", "min_length", "max_length", "pattern", "regex", "multiple_of",
                  "max_digits", "decimal_places", "min_items", "max_items"}
PYDANTIC_TYPES = {"constr", "conint", "confloat", "condecimal", "conlist", "conset"}
ENUM_BASES = {"Enum", "IntEnum", "StrEnum", "Flag", "IntFlag"}
SERVICE_SUFFIXES = ("Service", "Repository")
COLLECTIONS = {"list", "List", "set", "Set", "Sequence", "Iterable", "tuple", "Tuple"}
ROUTER_FACTORIES = {"APIRouter": "prefix", "FastAPI": None, "Flask": None, "Blueprint": "url_prefix"}


def module_segments(path: str) -> list[str]:
    parts = path[:-3].split("/") if path.endswith(".py") else path.split("/")
    if len(parts) > 1 and parts[0] == "src":
        parts = parts[1:]
    if len(parts) > 1 and parts[-1] == "__init__":
        parts = parts[:-1]
    return parts


def dotted(node) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Call):
        return dotted(node.func)
    if isinstance(node, ast.Subscript):
        return dotted(node.value)
    return ""


def short(node) -> str:
    return dotted(node).rsplit(".", 1)[-1]


def const(node):
    """字面量取值；不是字面量的写成源码文本（不求值）。"""
    if isinstance(node, ast.Constant):
        return "..." if node.value is Ellipsis else node.value if not isinstance(node.value, bytes) else None
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return [const(item) for item in node.elts]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub) and isinstance(node.operand, ast.Constant):
        value = node.operand.value
        return -value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    return ast.unparse(node)[:200]


def text_of(node) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = text_of(node.left), text_of(node.right)
        return left + right if left is not None and right is not None else None
    if isinstance(node, ast.JoinedStr):
        return "".join(item.value if isinstance(item, ast.Constant) else "{…}" for item in node.values)
    return None


def keywords(call: ast.Call) -> dict:
    return {item.arg: item.value for item in call.keywords if item.arg}


def unwrap_optional(annotation) -> tuple[ast.AST | None, bool]:
    """`Optional[X]`、`X | None` → (X, True)。"""
    if annotation is None:
        return None, False
    if isinstance(annotation, ast.Subscript) and short(annotation.value) == "Optional":
        return annotation.slice, True
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        for side, other in ((annotation.left, annotation.right), (annotation.right, annotation.left)):
            if isinstance(other, ast.Constant) and other.value is None:
                return side, True
    return annotation, False


def inner_type(annotation) -> ast.AST | None:
    """`Mapped[X]` → X。"""
    if isinstance(annotation, ast.Subscript) and short(annotation.value) == "Mapped":
        return annotation.slice
    return annotation


def type_name(node) -> tuple[str, bool]:
    """注解里的目标类名和是不是集合：`list["Order"]` → ("Order", True)。"""
    node, _ = unwrap_optional(node)
    if isinstance(node, ast.Subscript) and short(node.value) in COLLECTIONS:
        name, _ = type_name(node.slice if not isinstance(node.slice, ast.Tuple) else node.slice.elts[0])
        return name, True
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.rsplit(".", 1)[-1], False
    return short(node) if isinstance(node, (ast.Name, ast.Attribute)) else "", False


def _snake(name: str) -> str:
    return "".join(f"_{char.lower()}" if char.isupper() and index else char.lower() for index, char in enumerate(name))


def _assigned_names(body) -> list[str]:
    """类体里直接赋值的名字（枚举的成员）。"""
    names = []
    for statement in body:
        if isinstance(statement, ast.Assign):
            names.extend(target.id for target in statement.targets if isinstance(target, ast.Name))
    return names


def _implied_many(annotation, target: str, fk_columns, options: dict) -> bool:
    """relationship 没写类型注解、本表没有指向对方的外键列、也没给 uselist：按一对多算。"""
    if annotation is not None:
        return False
    return f"{_snake(target)}_id" not in fk_columns and options.get("uselist") is None


def _assigns_call_to_name(item) -> bool:
    """`名字 = 调用(...)`。"""
    if not isinstance(item, ast.Assign) or not isinstance(item.value, ast.Call):
        return False
    return len(item.targets) == 1 and isinstance(item.targets[0], ast.Name)


def _assigns_self_attribute(item) -> bool:
    """`self.x = ...`。"""
    if not isinstance(item, ast.Assign) or len(item.targets) != 1:
        return False
    return isinstance(item.targets[0], ast.Attribute) and dotted(item.targets[0]).startswith("self.")


class PythonFile:
    def __init__(self, sink: Sink, path: str, text: str, tree: ast.Module):
        self.sink = sink
        self.path = path
        self.lines = text.splitlines()
        self.tree = tree
        self.segments = module_segments(path)
        self.module = ".".join(self.segments)
        self.imports: dict[str, list[str]] = {}
        self.routers: dict[str, str] = {}
        self.pydantic: set[str] = set()

    def anchor(self, *names: str) -> str:
        return self.sink.anchor("sym", *self.segments, *names)

    def candidates(self, name: str) -> tuple[str, ...]:
        out = []
        if name in self.imports:
            out.append(self.sink.anchor("sym", *self.imports[name]))
        out.append(self.anchor(name))
        return tuple(out)

    def _resolve_from(self, node: ast.ImportFrom) -> list[str]:
        base = (node.module or "").split(".") if node.module else []
        if node.level:
            package = self.segments if self.path.endswith("__init__.py") else self.segments[:-1]
            package = package[:max(0, len(package) - (node.level - 1))]
            return [*package, *base]
        return base

    def run(self) -> None:
        for node in self.tree.body:
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    self.imports[alias.asname or alias.name] = [*self._resolve_from(node), alias.name]
            elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                factory = short(node.value.func)
                if factory in ROUTER_FACTORIES:
                    key = ROUTER_FACTORIES[factory]
                    prefix = keywords(node.value).get(key) if key else None
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            self.routers[target.id] = const(prefix) if prefix is not None else ""
            if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "urlpatterns"
                                                      for target in node.targets):
                self.sink.note("Django urls.py 的路由没有识别", self.path)
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and short(node.value.func) == "Table":
                self.sink.note("SQLAlchemy 的 Table() 命令式映射没有识别", self.path)
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "include_router" and "prefix" in keywords(node):
                    self.sink.note("include_router 的 prefix 没有拼进路由", self.path)
                elif node.func.attr in ("add_url_rule", "add_api_route", "add_route"):
                    self.sink.note("用 add_url_rule / add_api_route 注册的路由没有识别", self.path)
        for node in self.tree.body:
            if isinstance(node, ast.ClassDef):
                self.klass(node)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.function(node)

    # ---- 类 ----

    def kind_of(self, node: ast.ClassDef) -> str | None:
        bases = {dotted(base) for base in node.bases}
        names = {base.rsplit(".", 1)[-1] for base in bases}
        if names & ENUM_BASES:
            return "enum"
        if "models.Model" in bases or ("Model" in names and "models" in self.imports):
            return "django"
        for statement in node.body:
            if isinstance(statement, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "__tablename__"
                                                         for target in statement.targets):
                return "sqlalchemy"
            value = getattr(statement, "value", None)
            if isinstance(value, ast.Call) and short(value.func) in SA_COLUMNS | {"relationship"}:
                return "sqlalchemy"
            if isinstance(statement, ast.AnnAssign) and short(getattr(statement.annotation, "value", None)) == "Mapped":
                return "sqlalchemy"
        if names & {"BaseModel", "BaseSettings"} or names & self.pydantic:
            self.pydantic.add(node.name)
            return "pydantic"
        if node.name.endswith(SERVICE_SUFFIXES):
            return "service"
        if names & {"Model"}:
            return "django"
        return None

    def klass(self, node: ast.ClassDef) -> None:
        kind = self.kind_of(node)
        methods = [item for item in node.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))]
        sql = {item.name: self.function_sql(item) for item in methods}
        if kind is None and not any(sql.values()):
            return
        start = min([item.lineno for item in node.decorator_list] + [node.lineno])
        label = doc_label(ast.get_docstring(node))
        table = None
        meta = next((item for item in node.body if isinstance(item, ast.ClassDef) and item.name == "Meta"), None)
        meta_values = {}
        if meta is not None:
            for statement in meta.body:
                if isinstance(statement, ast.Assign) and isinstance(statement.targets[0], ast.Name):
                    meta_values[statement.targets[0].id] = statement.value
            table = const(meta_values["db_table"]) if "db_table" in meta_values else None
            if "verbose_name" in meta_values and isinstance(const(meta_values["verbose_name"]), str):
                label = explicit_label(const(meta_values["verbose_name"]))
        for statement in node.body:
            if isinstance(statement, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "__tablename__"
                                                         for target in statement.targets):
                table = const(statement.value)
        orm = kind if kind in ("sqlalchemy", "django") else None
        role = {"pydantic": "schema", "service": "service"}.get(kind or "", "entity" if orm else None)
        anchor = self.sink.entity(
            self.anchor(node.name), node.name, language=LANGUAGE, file=self.path, lines=self.lines, line=start,
            end_line=node.end_lineno or start, kind="enum" if kind == "enum" else "class", module=self.module,
            label=label, confidence="declared" if orm else "observed", table=table if isinstance(table, str) else None,
            orm=orm, role=role,
            values=_assigned_names(node.body) if kind == "enum" else None)
        for base in node.bases:
            name = short(base)
            if name and name not in ("Base", "Model", "BaseModel"):
                self.sink.relation(anchor, "inherits", language=LANGUAGE, file=self.path, line=start, target=name,
                                   candidates=self.candidates(name), confidence="observed", optional=True)
        fields: dict[str, str] = {}
        if kind == "sqlalchemy":
            self.sqlalchemy_fields(node, anchor, fields)
        elif kind == "django":
            self.django_fields(node, anchor, fields, meta_values)
        elif kind == "pydantic":
            self.pydantic_fields(node, anchor, fields)
        self.validators(node, anchor, fields, kind)
        attr_types = self.init_types(node)
        for method in methods:
            public = not method.name.startswith("_")
            if (kind == "service" and public) or sql[method.name]:
                self.operation(method, anchor, [node.name, method.name], "method", sql[method.name], attr_types,
                               role=role)

    # ---- SQLAlchemy ----

    def sqlalchemy_fields(self, node: ast.ClassDef, entity: str, fields: dict) -> None:
        fk_columns = []
        for statement in node.body:
            name, annotation, value = self._assignment(statement)
            if not name or name.startswith("__"):
                if name == "__table_args__":
                    self.table_args(statement, entity)
                continue
            call = value if isinstance(value, ast.Call) else None
            factory = short(call.func) if call else ""
            if factory not in SA_COLUMNS | {"relationship"} and short(getattr(annotation, "value", None)) != "Mapped":
                continue
            options = keywords(call) if call else {}
            mapped, optional = unwrap_optional(inner_type(annotation))
            line, end = statement.lineno, statement.end_lineno or statement.lineno
            if factory == "relationship":
                target, many = type_name(mapped)
                if call and call.args:
                    target = type_name(call.args[0])[0] or target
                if "secondary" in options:
                    relation = "many_to_many"
                elif many or _implied_many(annotation, target, fk_columns, options):
                    relation = "one_to_many"
                elif isinstance(options.get("uselist"), ast.Constant) and options["uselist"].value is False:
                    relation = "one_to_one"
                else:
                    relation = "many_to_one"
                many = relation in ("one_to_many", "many_to_many")
                anchor = self.sink.field(entity, name, language=LANGUAGE, file=self.path, line=line, end_line=end,
                                         data_type=f"list[{target}]" if many else target, module=self.module,
                                         confidence="declared")
                fields[name] = anchor
                self.sink.relation(entity, relation, language=LANGUAGE, file=self.path, line=line, target=target,
                                   candidates=self.candidates(target), via=anchor,
                                   back_populates=const(options["back_populates"]) if "back_populates" in options
                                   else None)
                continue
            sql_type = None
            column = None
            foreign = []
            for arg in call.args if call else []:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    column = arg.value
                elif isinstance(arg, ast.Call) and short(arg.func) == "ForeignKey":
                    foreign.append(arg)
                elif sql_type is None:
                    sql_type = ast.unparse(arg)
            nullable = options.get("nullable")
            nullable = const(nullable) if nullable is not None else (False if "primary_key" in options
                                                                      else (True if optional else None))
            anchor = self.sink.field(
                entity, name, language=LANGUAGE, file=self.path, line=line, end_line=end,
                data_type=sql_type or (ast.unparse(mapped) if mapped is not None else None), module=self.module,
                label=explicit_label(const(options["comment"])) if "comment" in options else None,
                confidence="declared", column=column, nullable=nullable,
                pk=True if "primary_key" in options and const(options["primary_key"]) is True else None)
            fields[name] = anchor
            if "unique" in options and const(options["unique"]) is True:
                self.sink.rule(anchor, "unique", language=LANGUAGE, file=self.path, line=line, end_line=end,
                               framework="sqlalchemy", module=self.module)
            for key in foreign:
                reference = const(key.args[0]) if key.args else ""
                if isinstance(reference, str) and "." in reference:
                    fk_columns.append(column or name)
                    table, _, ref_column = reference.rpartition(".")
                    self.sink.relation(entity, "references", language=LANGUAGE, file=self.path, line=line,
                                       table=table, target=table, via=anchor,
                                       columns=[[column or name, ref_column]])

    def table_args(self, statement, entity: str) -> None:
        _, _, value = self._assignment(statement)
        items = value.elts if isinstance(value, (ast.Tuple, ast.List)) else []
        for item in items:
            if not isinstance(item, ast.Call):
                continue
            factory = short(item.func)
            options = keywords(item)
            name = const(options["name"]) if "name" in options else None
            if factory == "CheckConstraint":
                expression = const(item.args[0]) if item.args else None
                self.sink.rule(entity, name if isinstance(name, str) else "check", language=LANGUAGE, file=self.path,
                               line=item.lineno, end_line=item.end_lineno or item.lineno, framework="sqlalchemy",
                               params={"expression": expression}, module=self.module)
            elif factory == "UniqueConstraint":
                self.sink.rule(entity, name if isinstance(name, str) else "unique", language=LANGUAGE,
                               file=self.path, line=item.lineno, end_line=item.end_lineno or item.lineno,
                               framework="sqlalchemy", params={"columns": [const(arg) for arg in item.args]},
                               module=self.module)

    # ---- Django ----

    def django_fields(self, node: ast.ClassDef, entity: str, fields: dict, meta: dict) -> None:
        for statement in node.body:
            name, _, value = self._assignment(statement)
            if not name or not isinstance(value, ast.Call):
                continue
            factory = short(value.func)
            if not (factory.endswith("Field") or factory in DJANGO_RELATIONS):
                continue
            options = keywords(value)
            line, end = statement.lineno, statement.end_lineno or statement.lineno
            label = options.get("verbose_name")
            if label is None and value.args and factory not in DJANGO_RELATIONS:
                label = value.args[0]
            nullable = const(options["null"]) if "null" in options else None
            target = ""
            if factory in DJANGO_RELATIONS:
                to = options.get("to", value.args[0] if value.args else None)
                target = node.name if isinstance(to, ast.Constant) and to.value == "self" else type_name(to)[0]
            anchor = self.sink.field(
                entity, name, language=LANGUAGE, file=self.path, line=line, end_line=end,
                data_type=f"{factory}({target})" if target else factory, module=self.module,
                label=explicit_label(const(label)) if label is not None and isinstance(const(label), str) else None,
                confidence="declared", column=const(options["db_column"]) if "db_column" in options else None,
                nullable=nullable if isinstance(nullable, bool) else None,
                pk=True if "primary_key" in options and const(options["primary_key"]) is True else None)
            fields[name] = anchor
            for key in DJANGO_RULES:
                if key in options:
                    self.sink.rule(anchor, key, language=LANGUAGE, file=self.path, line=line, end_line=end,
                                   framework="django", params={"value": const(options[key])}, module=self.module)
            if "unique" in options and const(options["unique"]) is True:
                self.sink.rule(anchor, "unique", language=LANGUAGE, file=self.path, line=line, end_line=end,
                               framework="django", module=self.module)
            validators = options.get("validators")
            for item in validators.elts if isinstance(validators, (ast.List, ast.Tuple)) else []:
                rule = short(item) or "validator"
                found = {"value": const(item.args[0])} if isinstance(item, ast.Call) and item.args else None
                self.sink.rule(anchor, rule, language=LANGUAGE, file=self.path, line=item.lineno,
                               end_line=item.end_lineno or item.lineno, framework="django", params=found,
                               module=self.module)
            if target:
                self.sink.relation(entity, DJANGO_RELATIONS[factory], language=LANGUAGE, file=self.path, line=line,
                                   target=target, candidates=self.candidates(target), via=anchor,
                                   on_delete=const(options["on_delete"]) if "on_delete" in options else None,
                                   related_name=const(options["related_name"]) if "related_name" in options else None)
        for item in meta.get("constraints").elts if isinstance(meta.get("constraints"), ast.List) else []:
            if isinstance(item, ast.Call):
                options = keywords(item)
                name = const(options["name"]) if "name" in options else short(item.func)
                self.sink.rule(entity, name if isinstance(name, str) else "constraint", language=LANGUAGE,
                               file=self.path, line=item.lineno, end_line=item.end_lineno or item.lineno,
                               framework="django", params={key: const(value) for key, value in options.items()
                                                           if key != "name"}, module=self.module)

    # ---- pydantic ----

    def pydantic_fields(self, node: ast.ClassDef, entity: str, fields: dict) -> None:
        for statement in node.body:
            if not isinstance(statement, ast.AnnAssign) or not isinstance(statement.target, ast.Name):
                continue
            name = statement.target.id
            annotation, optional = unwrap_optional(statement.annotation)
            constraints: dict = {}
            label = None
            calls = []
            if isinstance(statement.value, ast.Call) and short(statement.value.func) == "Field":
                calls.append(statement.value)
            if isinstance(annotation, ast.Subscript) and short(annotation.value) == "Annotated":
                parts = annotation.slice.elts if isinstance(annotation.slice, ast.Tuple) else [annotation.slice]
                annotation = parts[0]
                calls.extend(item for item in parts[1:] if isinstance(item, ast.Call))
            if isinstance(annotation, ast.Call) and short(annotation.func) in PYDANTIC_TYPES:
                calls.append(annotation)
            for call in calls:
                options = keywords(call)
                for key, value in options.items():
                    if key in PYDANTIC_RULES:
                        constraints[key] = const(value)
                for key in ("description", "title"):
                    if key in options and isinstance(const(options[key]), str) and label is None:
                        label = explicit_label(const(options[key]))
            line, end = statement.lineno, statement.end_lineno or statement.lineno
            anchor = self.sink.field(entity, name, language=LANGUAGE, file=self.path, line=line, end_line=end,
                                     data_type=ast.unparse(statement.annotation), module=self.module, label=label,
                                     nullable=True if optional else None,
                                     validations=[f"{key}={value}" for key, value in constraints.items()] or None)
            fields[name] = anchor
            for key, value in constraints.items():
                self.sink.rule(anchor, key, language=LANGUAGE, file=self.path, line=line, end_line=end,
                               framework="pydantic", params={"value": value}, module=self.module)

    def validators(self, node: ast.ClassDef, entity: str, fields: dict, kind: str | None) -> None:
        """`@validates`（SQLAlchemy）、`@field_validator` / `@validator` / `@model_validator`（pydantic）。"""
        for method in node.body:
            if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in method.decorator_list:
                name = short(decorator)
                if name not in ("validates", "field_validator", "validator", "model_validator", "root_validator"):
                    continue
                targets = [const(arg) for arg in decorator.args] if isinstance(decorator, ast.Call) else []
                anchors = [fields.get(item, entity) for item in targets if isinstance(item, str)] or [entity]
                start = min(item.lineno for item in method.decorator_list)
                for target in dict.fromkeys(anchors):
                    self.sink.rule(target, method.name, language=LANGUAGE, file=self.path, line=start,
                                   end_line=method.end_lineno or start,
                                   framework="sqlalchemy" if name == "validates" else "pydantic",
                                   params={"decorator": name, "fields": [item for item in targets
                                                                         if isinstance(item, str)] or None},
                                   doc=doc_label(ast.get_docstring(method)))

    # ---- 函数与路由 ----

    def function(self, node) -> None:
        routes = []
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                continue
            owner, attr = dotted(decorator.func.value), decorator.func.attr
            if attr not in HTTP | {"route", "api_route", "websocket"}:
                continue
            options = keywords(decorator)
            path = const(decorator.args[0]) if decorator.args else const(options.get("path", ast.Constant("")))
            if not isinstance(path, str):
                continue
            if attr in HTTP:
                methods = [attr.upper()]
            elif attr == "websocket":
                methods = ["WS"]
            else:
                declared = const(options["methods"]) if "methods" in options else ["GET"]
                methods = [str(item).upper() for item in declared] if isinstance(declared, list) else ["GET"]
            prefix = self.routers.get(owner)
            if prefix is None:
                self.sink.note("路由对象不在本文件里定义，前缀没有拼上", self.path)
                prefix = ""
            for method in methods:
                routes.append((method, join_path(prefix if isinstance(prefix, str) else "", path)))
        sql = self.function_sql(node)
        if not routes and not sql:
            return
        anchor = self.operation(node, None, [node.name], "function", sql, {}, role="handler" if routes else None)
        start = min([item.lineno for item in node.decorator_list] + [node.lineno])
        for method, path in routes:
            self.sink.route(method, path, anchor, language=LANGUAGE, file=self.path, lines=self.lines, line=start,
                            end_line=node.end_lineno or start, module=self.module)

    def operation(self, node, parent: str | None, names: list[str], kind: str, sql: list[str], attr_types: dict,
                  role: str | None = None) -> str:
        start = min([item.lineno for item in node.decorator_list] + [node.lineno])
        arguments = [arg for arg in node.args.args if arg.arg not in ("self", "cls")]
        signature = f"{node.name}(" + ", ".join(
            arg.arg + (f": {ast.unparse(arg.annotation)}" if arg.annotation is not None else "") for arg in arguments
        ) + ")" + (f" -> {ast.unparse(node.returns)}" if node.returns is not None else "")
        anchor = self.sink.operation(
            self.anchor(*names), node.name, language=LANGUAGE, file=self.path, lines=self.lines, line=start,
            end_line=node.end_lineno or start, kind=kind, parent=parent, module=self.module,
            label=doc_label(ast.get_docstring(node)), sql=sql, signature=signature, role=role,
            asynchronous=True if isinstance(node, ast.AsyncFunctionDef) else None)
        local = dict(attr_types)
        for arg in arguments:
            if arg.annotation is not None:
                local[arg.arg] = short(arg.annotation)
        for item in ast.walk(node):
            if _assigns_call_to_name(item):
                local.setdefault(item.targets[0].id, short(item.value.func))
        for item in ast.walk(node):
            if not isinstance(item, ast.Call) or not isinstance(item.func, ast.Attribute):
                continue
            target = local.get(dotted(item.func.value))
            if target:
                self.sink.relation(anchor, "calls", language=LANGUAGE, file=self.path, line=item.lineno,
                                   target=target, candidates=self.candidates(target), member=item.func.attr,
                                   confidence="inferred")
        return anchor

    @staticmethod
    def init_types(node: ast.ClassDef) -> dict:
        """`__init__` 里 `self.x = 参数`（参数有类型注解）或 `self.x = 类(...)`：属性 → 类名。"""
        init = next((item for item in node.body if isinstance(item, ast.FunctionDef) and item.name == "__init__"), None)
        if init is None:
            return {}
        params = {arg.arg: short(arg.annotation) for arg in init.args.args if arg.annotation is not None}
        types = {}
        for item in ast.walk(init):
            if _assigns_self_attribute(item):
                key = dotted(item.targets[0])
                if isinstance(item.value, ast.Name) and item.value.id in params:
                    types[key] = params[item.value.id]
                elif isinstance(item.value, ast.Call) and short(item.value.func)[:1].isupper():
                    types[key] = short(item.value.func)
        return types

    @staticmethod
    def function_sql(node) -> list[str]:
        found: list[str] = []
        consumed: set[int] = set()
        for item in ast.walk(node):
            if id(item) in consumed:
                continue
            if isinstance(item, (ast.BinOp, ast.JoinedStr)):
                value = text_of(item)
                if value is None:
                    continue
                consumed.update(id(inner) for inner in ast.walk(item))
            elif isinstance(item, ast.Constant) and isinstance(item.value, str):
                value = item.value
            else:
                continue
            if looks_like_sql(value) and value not in found:
                found.append(value)
        return found

    @staticmethod
    def _assignment(statement):
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1 and isinstance(statement.targets[0],
                                                                                            ast.Name):
            return statement.targets[0].id, None, statement.value
        if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
            return statement.target.id, statement.annotation, statement.value
        return None, None, None


def analyze(sink: Sink, path: str, text: str) -> None:
    """语法错误等异常抛给调用方记局限。"""
    tree = ast.parse(text, filename=path)
    PythonFile(sink, path, text, tree).run()
