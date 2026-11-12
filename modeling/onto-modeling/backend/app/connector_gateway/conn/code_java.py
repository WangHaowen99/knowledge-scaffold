"""Java 静态分析（javalang，纯 Python；证据采集接口 §10）。

认：JPA `@Entity` / `@Table` / `@Column` / `@Id` / 关联注解，Spring `@RestController` + `@RequestMapping` / `@GetMapping` 等，
`@Service` 的公开方法，Spring Data 仓库接口上的 `@Query`，方法体里的 SQL 字面量，Bean Validation 注解，枚举。
javalang 只认到 Java 8 的语法，`record`、文本块、`switch` 表达式会解析失败，由调用方记进局限。
"""

from __future__ import annotations

import re

import javalang

from .code_model import Sink, block_end, doc_label, explicit_label, join_path, looks_like_sql

LANGUAGE = "java"
ASSOCIATIONS = {"ManyToOne": "many_to_one", "OneToMany": "one_to_many", "OneToOne": "one_to_one",
                "ManyToMany": "many_to_many"}
VALIDATIONS = {"NotNull", "NotBlank", "NotEmpty", "Null", "Size", "Min", "Max", "DecimalMin", "DecimalMax",
               "Positive", "PositiveOrZero", "Negative", "NegativeOrZero", "Pattern", "Email", "Past",
               "PastOrPresent", "Future", "FutureOrPresent", "Digits", "AssertTrue", "AssertFalse", "Length",
               "Range", "URL"}
MAPPINGS = {"GetMapping": "GET", "PostMapping": "POST", "PutMapping": "PUT", "DeleteMapping": "DELETE",
            "PatchMapping": "PATCH", "RequestMapping": None}
CONTROLLERS = {"RestController", "Controller"}
REPOSITORY_BASES = {"JpaRepository", "CrudRepository", "PagingAndSortingRepository", "Repository",
                    "ListCrudRepository", "JpaSpecificationExecutor", "ReactiveCrudRepository"}
COLLECTIONS = {"List", "Set", "Collection", "Iterable", "SortedSet", "Optional"}
LABEL_ANNOTATIONS = {"Comment": "value", "Schema": "description", "ApiModel": "value", "ApiModelProperty": "value"}
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\"": "\"", "'": "'", "\\": "\\", "b": "\b", "f": "\f", "0": "\0"}


class Ref(str):
    """注解参数里的常量引用（`类.常量`）：取不到值，只留名字。"""


def _unescape(literal: str) -> str:
    body = literal[1:-1]
    return re.sub(r"\\(.)", lambda match: _ESCAPES.get(match.group(1), match.group(1)), body)


def const(node):
    """注解参数、字面量的取值：字符串、整数、布尔原样，常量引用写成 `类.常量`，数组成列表；拼不出来回 None。"""
    if node is None:
        return None
    if isinstance(node, javalang.tree.Literal):
        value = node.value
        if value.startswith("\""):
            return _unescape(value)
        if value in ("true", "false"):
            return value == "true"
        if re.fullmatch(r"-?\d+", value):
            return int(value)
        return value
    if isinstance(node, javalang.tree.MemberReference):
        return Ref(f"{node.qualifier}.{node.member}" if node.qualifier else node.member)
    if isinstance(node, javalang.tree.ElementArrayValue):
        return [const(item) for item in node.values]
    if isinstance(node, javalang.tree.BinaryOperation) and node.operator == "+":
        left, right = string(node.operandl), string(node.operandr)
        return left + right if left is not None and right is not None else None
    return None


def string(node) -> str | None:
    value = const(node)
    if isinstance(value, str) and isinstance(node, (javalang.tree.Literal, javalang.tree.BinaryOperation)):
        return value
    return None


def type_text(node) -> str:
    if node is None:
        return "void"
    text = node.name
    arguments = getattr(node, "arguments", None)
    if arguments:
        text += "<" + ", ".join(type_text(item.type) if item.type is not None else "?" for item in arguments) + ">"
    if getattr(node, "sub_type", None) is not None:
        text += "." + type_text(node.sub_type)
    return text + "[]" * len(node.dimensions or [])


def target_type(node) -> str:
    """关联字段的目标类型：集合取元素类型，Map 取值类型。"""
    arguments = getattr(node, "arguments", None) or []
    if node.name in COLLECTIONS and arguments and arguments[0].type is not None:
        return arguments[0].type.name
    if node.name == "Map" and len(arguments) == 2 and arguments[1].type is not None:
        return arguments[1].type.name
    return node.name


def _name(annotation) -> str:
    return annotation.name.rsplit(".", 1)[-1]


def params(annotation) -> dict:
    element = annotation.element
    if element is None:
        return {}
    if isinstance(element, list):
        return {pair.name: const(pair.value) for pair in element}
    return {"value": const(element)}


def _annotations(node) -> dict:
    return {_name(item): item for item in getattr(node, "annotations", None) or []}


def _start(node) -> int:
    lines = [item.position.line for item in getattr(node, "annotations", None) or [] if item.position]
    if node.position:
        lines.append(node.position.line)
    return min(lines) if lines else 1


def _label(annotations: dict, documentation: str | None) -> str | None:
    for name, key in LABEL_ANNOTATIONS.items():
        if name in annotations:
            value = params(annotations[name]).get(key)
            if isinstance(value, str) and value.strip():
                return explicit_label(value)
    return doc_label(documentation)


def _paths(value) -> list:
    if value is None or value == "":
        return [""]
    return value if isinstance(value, list) else [value]


def _instance_field(member) -> bool:
    return isinstance(member, javalang.tree.FieldDeclaration) and "static" not in member.modifiers


class JavaFile:
    def __init__(self, sink: Sink, path: str, text: str, tree):
        self.sink = sink
        self.path = path
        self.lines = text.splitlines()
        self.package = tree.package.name if tree.package else ""
        self.imports = {item.path.rsplit(".", 1)[-1]: item.path for item in tree.imports
                        if not item.static and not item.wildcard}
        self.tree = tree

    def candidates(self, simple: str, outer: list[str]) -> tuple[str, ...]:
        out = []
        if simple in self.imports:
            out.append(self.sink.anchor("sym", *self.imports[simple].split(".")))
        for depth in range(len(outer), 0, -1):
            out.append(self.sink.anchor("sym", *self.package.split("."), *outer[:depth], simple))
        if self.package:
            out.append(self.sink.anchor("sym", *self.package.split("."), simple))
        return tuple(out)

    def run(self) -> None:
        for declaration in self.tree.types:
            self.declaration(declaration, [])

    # ---- 类型 ----

    def declaration(self, node, outer: list[str]) -> None:
        if isinstance(node, javalang.tree.EnumDeclaration):
            self.enum(node, outer)
        elif isinstance(node, (javalang.tree.ClassDeclaration, javalang.tree.InterfaceDeclaration)):
            self.type(node, outer)

    def enum(self, node, outer: list[str]) -> None:
        start = _start(node)
        values = [constant.name for constant in node.body.constants] if node.body else []
        self.sink.entity(self.sink.anchor("sym", *self.package.split("."), *outer, node.name), node.name,
                         language=LANGUAGE, file=self.path, lines=self.lines, line=start,
                         end_line=block_end(self.lines, node.position.line if node.position else start),
                         kind="enum", module=self.package, label=_label(_annotations(node), node.documentation),
                         values=values)

    def type(self, node, outer: list[str]) -> None:
        annotations = _annotations(node)
        names = set(annotations)
        is_interface = isinstance(node, javalang.tree.InterfaceDeclaration)
        bases = node.extends if isinstance(node.extends, list) else ([node.extends] if node.extends else [])
        role = None
        if "Entity" in names or "MappedSuperclass" in names or "Embeddable" in names:
            role = "entity"
        elif names & CONTROLLERS:
            role = "controller"
        elif "Service" in names:
            role = "service"
        elif "Repository" in names or (is_interface and any(base.name in REPOSITORY_BASES for base in bases)):
            role = "repository"
        fields = [member for member in node.body if _instance_field(member)]
        methods = [member for member in node.body if isinstance(member, javalang.tree.MethodDeclaration)]
        validated = any(set(_annotations(field)) & VALIDATIONS for field in fields)
        if role is None and validated:
            role = "dto"
        sql_methods = {id(method): self.method_sql(method) for method in methods}
        segments = [*outer, node.name]
        if role or any(sql_methods.values()):
            self.emit_type(node, segments, role, annotations, bases, fields, methods, sql_methods)
        for member in node.body:
            if isinstance(member, (javalang.tree.ClassDeclaration, javalang.tree.InterfaceDeclaration,
                                   javalang.tree.EnumDeclaration)):
                self.declaration(member, segments)

    def emit_type(self, node, segments, role, annotations, bases, fields, methods, sql_methods) -> None:
        sink = self.sink
        orm = role == "entity"
        table = params(annotations["Table"]).get("name") if "Table" in annotations else None
        start = _start(node)
        anchor = sink.entity(
            sink.anchor("sym", *self.package.split("."), *segments), node.name, language=LANGUAGE, file=self.path,
            lines=self.lines, line=start,
            end_line=block_end(self.lines, node.position.line if node.position else start),
            kind="interface" if isinstance(node, javalang.tree.InterfaceDeclaration) else "class",
            module=self.package, label=_label(annotations, node.documentation),
            confidence="declared" if orm else "observed", table=table if isinstance(table, str) else None,
            orm="jpa" if orm else None, role=role, annotations=sorted(annotations) or None)
        for base in bases:
            sink.relation(anchor, "inherits", language=LANGUAGE, file=self.path, line=start, target=base.name,
                          candidates=self.candidates(base.name, segments[:-1]), confidence="observed",
                          optional=True)
        field_types = {}
        for field in fields:
            for declarator in field.declarators:
                field_types[declarator.name] = field.type.name
            if role in ("entity", "dto"):
                self.field(anchor, field, segments, orm)
        prefixes = [""]
        if role == "controller" and "RequestMapping" in annotations:
            mapping = params(annotations["RequestMapping"])
            prefixes = _paths(mapping.get("value", mapping.get("path")))
        for method in methods:
            sql = sql_methods[id(method)]
            mapped = _annotations(method).keys() & MAPPINGS.keys()
            public = "public" in method.modifiers or isinstance(node, javalang.tree.InterfaceDeclaration)
            if role == "controller" and mapped:
                self.method(anchor, method, segments, role, sql, field_types, prefixes=prefixes)
            elif role == "service" and public and "static" not in method.modifiers:
                self.method(anchor, method, segments, role, sql, field_types)
            elif sql or "Query" in _annotations(method):
                self.method(anchor, method, segments, role, sql, field_types)

    # ---- 成员 ----

    def field(self, parent: str, field, segments: list[str], orm: bool) -> None:
        sink = self.sink
        annotations = _annotations(field)
        line = field.position.line if field.position else _start(field)
        start = _start(field)
        end = block_end(self.lines, line)
        column = params(annotations["Column"]) if "Column" in annotations else {}
        join = params(annotations["JoinColumn"]) if "JoinColumn" in annotations else {}
        nullable = column.get("nullable", join.get("nullable"))
        if "Id" in annotations or annotations.keys() & {"NotNull", "NotBlank", "NotEmpty"}:
            nullable = False if nullable is None else nullable
        validations = []
        for name, annotation in annotations.items():
            if name in VALIDATIONS:
                found = params(annotation)
                validations.append(name + ("(" + ", ".join(f"{key}={value}" for key, value in found.items()) + ")"
                                           if found else ""))
        for declarator in field.declarators:
            anchor = sink.field(
                parent, declarator.name, language=LANGUAGE, file=self.path, line=start, end_line=end,
                data_type=type_text(field.type), module=self.package,
                label=_label(annotations, field.documentation), confidence="declared" if orm else "observed",
                column=column.get("name") or join.get("name"), nullable=nullable,
                length=column.get("length"), pk=True if "Id" in annotations else None,
                validations=validations or None)
            for name, annotation in annotations.items():
                if name in VALIDATIONS:
                    sink.rule(anchor, name, language=LANGUAGE, file=self.path, line=annotation.position.line
                              if annotation.position else start, end_line=end, framework="bean-validation",
                              params=params(annotation), module=self.package)
            if column.get("unique") is True:
                sink.rule(anchor, "unique", language=LANGUAGE, file=self.path, line=start, end_line=end,
                          framework="jpa", module=self.package)
            for name, relation in ASSOCIATIONS.items():
                if name in annotations:
                    options = params(annotations[name])
                    target = options.get("targetEntity")
                    target = target.rsplit(".", 2)[-2] if isinstance(target, str) and target.endswith(".class") \
                        else target_type(field.type)
                    sink.relation(parent, relation, language=LANGUAGE, file=self.path, line=start, target=target,
                                  candidates=self.candidates(target, segments), via=anchor,
                                  mapped_by=options.get("mappedBy"), join_column=join.get("name"),
                                  fetch=options.get("fetch"), cascade=options.get("cascade"))

    def method(self, parent: str, method, segments: list[str], role: str | None, sql: list[str],
               field_types: dict, prefixes: list | None = None) -> None:
        sink = self.sink
        annotations = _annotations(method)
        start = _start(method)
        end = block_end(self.lines, method.position.line if method.position else start)
        signature = f"{method.name}(" + ", ".join(f"{type_text(item.type)} {item.name}"
                                                  for item in method.parameters) + f"): {type_text(method.return_type)}"
        query = params(annotations["Query"]) if "Query" in annotations else {}
        if "Query" in annotations:
            value = query.get("value")
            if isinstance(value, str) and value.strip():
                sql = [value, *sql]
            else:
                sink.note("@Query 的语句不是字面量，没有取到", self.path)
        visibility = next((item for item in ("public", "protected", "private") if item in method.modifiers), None)
        anchor = sink.operation(
            sink.member(sink.anchor("sym", *self.package.split("."), *segments), method.name), method.name,
            language=LANGUAGE, file=self.path, lines=self.lines, line=start, end_line=end, kind="method",
            parent=parent, module=self.package, label=_label(annotations, method.documentation), sql=sql,
            signature=signature, visibility=visibility, role=role,
            native=True if query.get("nativeQuery") is True else None)
        for invocation in self._invocations(method):
            if invocation.qualifier in field_types:
                target = field_types[invocation.qualifier]
                sink.relation(anchor, "calls", language=LANGUAGE, file=self.path,
                              line=invocation.position.line if invocation.position else start, target=target,
                              candidates=self.candidates(target, segments[:-1]), member=invocation.member,
                              confidence="inferred")
        if prefixes is None:
            return
        for name in annotations.keys() & MAPPINGS.keys():
            options = params(annotations[name])
            http = MAPPINGS[name]
            if http is None:
                declared = options.get("method")
                declared = declared if isinstance(declared, list) else [declared] if declared else []
                methods = [str(item).rsplit(".", 1)[-1] for item in declared] or ["ANY"]
            else:
                methods = [http]
            paths = _paths(options.get("value", options.get("path")))
            for prefix in prefixes:
                for path in paths:
                    confidence = "observed"
                    if any(isinstance(item, Ref) or not isinstance(item, str) for item in (prefix, path)):
                        sink.note("路由路径用了常量，没有展开", self.path)
                        confidence = "inferred"
                    url = join_path(*("{" + str(item) + "}" if isinstance(item, Ref) or not isinstance(item, str)
                                      else item for item in (prefix, path)))
                    for http_method in methods:
                        sink.route(http_method, url, anchor, language=LANGUAGE, file=self.path, lines=self.lines,
                                   line=start, end_line=end, module=self.package, confidence=confidence)

    @staticmethod
    def _invocations(method):
        if not method.body:
            return []
        found = []
        for statement in method.body:
            for _, node in statement.filter(javalang.tree.MethodInvocation):
                if node.qualifier:
                    found.append(node)
        return found

    @staticmethod
    def method_sql(method) -> list[str]:
        """方法体里像 SQL 的字符串字面量（`+` 拼起来的算一条）。"""
        if not method.body:
            return []
        found: list[str] = []
        consumed: set[int] = set()
        for statement in method.body:
            for _, node in statement.filter(javalang.tree.BinaryOperation):
                if id(node) in consumed or node.operator != "+":
                    continue
                value = string(node)
                if value is not None:
                    for _, inner in node.filter(javalang.tree.Literal):
                        consumed.add(id(inner))
                    if looks_like_sql(value) and value not in found:
                        found.append(value)
            for _, node in statement.filter(javalang.tree.Literal):
                if id(node) in consumed or not node.value.startswith("\""):
                    continue
                value = _unescape(node.value)
                if looks_like_sql(value) and value not in found:
                    found.append(value)
        return found


def analyze(sink: Sink, path: str, text: str) -> None:
    """解析失败抛异常，由调用方记局限。"""
    tree = javalang.parse.parse(text)
    JavaFile(sink, path, text, tree).run()
