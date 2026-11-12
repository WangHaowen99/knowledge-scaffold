"""TypeScript 静态分析（正则加括号配对，不跑编译器；证据采集接口 §10）。

先把注释、字符串、模板串、正则字面量遮成占位符（长度和换行不变），再在遮过的文本上找装饰器、类、成员：
TypeORM `@Entity` / `@Column` / 关联装饰器，NestJS `@Controller` + `@Get` / `@Post` 等，class-validator 装饰器，
枚举，名字以 Service / Repository 结尾的类的公开方法，方法里的 SQL 字面量。写法偏离常见格式（比如返回类型里写对象
字面量类型、属性跨行且不写分号）时可能认错，识别不了的框架写法记进局限。
"""

from __future__ import annotations

import bisect
import posixpath
import re
from dataclasses import dataclass

from .code_model import Sink, doc_label, explicit_label, join_path, looks_like_sql

LANGUAGE = "typescript"
TYPEORM_COLUMNS = {"Column", "PrimaryColumn", "PrimaryGeneratedColumn", "CreateDateColumn", "UpdateDateColumn",
                   "DeleteDateColumn", "VersionColumn", "ObjectIdColumn", "ViewColumn"}
TYPEORM_RELATIONS = {"ManyToOne": "many_to_one", "OneToMany": "one_to_many", "OneToOne": "one_to_one",
                     "ManyToMany": "many_to_many"}
ROUTES = {"Get": "GET", "Post": "POST", "Put": "PUT", "Patch": "PATCH", "Delete": "DELETE", "All": "ANY",
          "Head": "HEAD", "Options": "OPTIONS"}
VALIDATORS = {"Min", "Max", "MinLength", "MaxLength", "Length", "Matches", "ArrayMinSize", "ArrayMaxSize",
              "ArrayNotEmpty", "ArrayUnique", "ValidateNested", "Contains", "NotContains", "Equals", "NotEquals",
              "MinDate", "MaxDate", "ValidateIf", "Validate"}
SERVICE_SUFFIXES = ("Service", "Repository")
MODIFIERS = {"public", "private", "protected", "static", "readonly", "declare", "abstract", "override", "async",
             "accessor", "get", "set"}
KEYWORDS = {"if", "for", "while", "switch", "catch", "return", "function", "constructor", "new", "await"}
_IDENT = r"[A-Za-z_$][\w$]*"
_DECORATOR = re.compile(r"@(" + _IDENT + r"(?:\." + _IDENT + r")*)")
_CLASS = re.compile(r"^(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?class\s+(" + _IDENT
                    + r")(?:\s*<.*?>)?(?:\s+extends\s+(" + _IDENT + r"(?:\." + _IDENT + r")*)(?:\s*<.*?>)?)?"
                    r"(?:\s+implements\s+.+)?$")
_ENUM = re.compile(r"^(?:export\s+)?(?:declare\s+)?(?:const\s+)?enum\s+(" + _IDENT + r")$")
_FUNCTION = re.compile(r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*(" + _IDENT + r")")
_METHOD = re.compile(r"^((?:(?:" + "|".join(sorted(MODIFIERS)) + r")\s+)*)\*?\s*(" + _IDENT
                     + r")\s*[?!]?\s*(?:<.*?>)?\s*\((.*)\)\s*(?::\s*(.+))?$", re.DOTALL)
_PROPERTY = re.compile(r"^((?:(?:public|private|protected|static|readonly|declare|abstract|override|accessor)\s+)*)("
                       + _IDENT + r")\s*([?!])?\s*(?::\s*(.+?))?\s*(?:=\s*(.*))?$", re.DOTALL)
_PARAM_PROPERTY = re.compile(r"(?:(?:public|private|protected|readonly)\s+)+(" + _IDENT + r")\s*[?!]?\s*:\s*("
                             + _IDENT + r")")
_IMPORT = re.compile(r"import\s+(?:type\s+)?([^;'\"`]*?)\s+from\s+(['\"])([^'\"]+)\2")
HEAD_MAX = 2000
_THIS_CALL = re.compile(r"this\.(" + _IDENT + r")\.(" + _IDENT + r")\s*\(")
_ARROW_TARGET = re.compile(r"^\s*(?:\(\s*\w*\s*\)|\w+)\s*=>\s*(" + _IDENT + r")")
_INVERSE = re.compile(r"=>\s*\w+\.(" + _IDENT + r")")
# {key} 留给 format 填
_OPTION = rf"\b{{key}}\s*:\s*(['\"`][^'\"`]*['\"`]|true|false|-?\d+|{_IDENT}(?:\.{_IDENT})*)"
_KEYWORD_BEFORE = re.compile(r"\b(return|typeof|case)\s*$")
_REGEX_BEFORE = set("(,=:[!&|?{};+-*%<>~^") | {""}


class Ref(str):
    """实参不是字面量（常量、表达式）：只留源码文本。"""


@dataclass
class Literal:
    start: int
    end: int
    value: str
    kind: str


@dataclass
class Decorator:
    name: str
    start: int
    args: tuple[int, int] | None


def mask(text: str) -> tuple[str, list[Literal], list[tuple[int, int, str]]]:
    """注释遮成空格，字符串、模板串、正则字面量的内容遮成 `_`；回遮过的文本、字面量、`/** */` 文档注释。"""
    out = list(text)
    literals: list[Literal] = []
    docs: list[tuple[int, int, str]] = []
    n = len(text)
    i = 0
    last = ""

    def blank(start: int, end: int, fill: str) -> None:
        for index in range(start, end):
            if out[index] != "\n":
                out[index] = fill

    while i < n:
        char = text[i]
        pair = text[i:i + 2]
        if pair == "//":
            end = text.find("\n", i)
            end = n if end < 0 else end
            blank(i, end, " ")
            i = end
            continue
        if pair == "/*":
            end = text.find("*/", i + 2)
            end = n if end < 0 else end + 2
            if text.startswith("/**", i):
                docs.append((i, end, text[i + 3:end - 2]))
            blank(i, end, " ")
            i = end
            continue
        if char in "'\"":
            j = i + 1
            while j < n and text[j] != char and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            end = min(j, n)
            literals.append(Literal(i, end + 1, _unescape(text[i + 1:end]), "string"))
            blank(i + 1, end, "_")
            i = end + 1
            last = char
            continue
        if char == "`":
            j = i + 1
            depth = 0
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if depth == 0 and text[j] == "`":
                    break
                if text.startswith("${", j):
                    depth += 1
                    j += 2
                    continue
                if depth and text[j] == "}":
                    depth -= 1
                j += 1
            end = min(j, n)
            literals.append(Literal(i, end + 1, re.sub(r"\$\{[^}]*\}", "{…}", text[i + 1:end]), "template"))
            blank(i + 1, end, "_")
            i = end + 1
            last = "`"
            continue
        if char == "/" and (last in _REGEX_BEFORE or _KEYWORD_BEFORE.search("".join(out[max(0, i - 8):i]))):
            j = i + 1
            in_class = False
            while j < n and text[j] != "\n":
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == "[":
                    in_class = True
                elif text[j] == "]":
                    in_class = False
                elif text[j] == "/" and not in_class:
                    break
                j += 1
            if j < n and text[j] == "/":
                literals.append(Literal(i, j + 1, text[i:j + 1], "regex"))
                blank(i + 1, j, "_")
                i = j + 1
                last = "/"
                continue
        if not char.isspace():
            last = char
        i += 1
    return "".join(out), literals, docs


_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "0": "\0"}


def _unescape(body: str) -> str:
    return re.sub(r"\\(.)", lambda match: _ESCAPES.get(match.group(1), match.group(1)), body)


def match_close(masked: str, index: int, end: int | None = None) -> int:
    """`index` 处是开括号，回配对的闭括号位置；配不上回文本末尾。"""
    pairs = {"(": ")", "{": "}", "[": "]"}
    opener = masked[index]
    closer = pairs.get(opener)
    if closer is None:
        raise ValueError(f"位置 {index} 不是开括号")
    depth = 0
    limit = len(masked) if end is None else end
    for position in range(index, limit):
        char = masked[position]
        if char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return position
    return limit - 1


def split_top(masked: str, start: int, end: int) -> list[tuple[int, int]]:
    """括号外的逗号切分。"""
    parts = []
    depth = 0
    begin = start
    for position in range(start, end):
        char = masked[position]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append((begin, position))
            begin = position + 1
    if masked[begin:end].strip():
        parts.append((begin, end))
    return parts


class TsFile:
    def __init__(self, sink: Sink, path: str, text: str):
        self.sink = sink
        self.path = path
        self.text = text
        self.lines = text.splitlines()
        self.masked, literals, self.docs = mask(text)
        self.literals = sorted(literals, key=lambda item: item.start)
        self._literal_starts = [item.start for item in self.literals]
        self._newlines = [index for index, char in enumerate(text) if char == "\n"]
        self.imports: dict[str, str] = {}
        self.module = posixpath.dirname(path)

    # ---- 位置与字面量 ----

    def line(self, offset: int) -> int:
        return bisect.bisect_left(self._newlines, offset) + 1

    def literals_in(self, start: int, end: int) -> list[Literal]:
        index = bisect.bisect_left(self._literal_starts, start)
        found = []
        while index < len(self.literals) and self.literals[index].start < end:
            found.append(self.literals[index])
            index += 1
        return found

    def value(self, start: int, end: int):
        """一个实参的取值：单个字面量回内容，整数、布尔回原值，其余回源码文本。"""
        raw = self.text[start:end].strip()
        literals = self.literals_in(start, end)
        if len(literals) == 1 and self.text[literals[0].start:literals[0].end].strip() == raw:
            return literals[0].value
        if re.fullmatch(r"-?\d+", raw):
            return int(raw)
        if raw in ("true", "false"):
            return raw == "true"
        return Ref(raw[:200])

    def args(self, decorator: Decorator) -> list:
        if not decorator.args:
            return []
        return [self.value(start, end) for start, end in split_top(self.masked, *decorator.args)]

    def option(self, decorator: Decorator, key: str):
        if not decorator.args:
            return None
        start, end = decorator.args
        match = re.search(_OPTION.format(key=key), self.masked[start:end])
        if not match:
            return None
        return self.value(start + match.start(1), start + match.end(1))

    def doc_before(self, offset: int) -> str | None:
        for start, end, body in reversed(self.docs):
            if end <= offset:
                return body if not self.masked[end:offset].strip() else None
        return None

    def candidates(self, name: str) -> tuple[str, ...]:
        out = []
        spec = self.imports.get(name)
        if spec and spec.startswith("."):
            base = posixpath.normpath(posixpath.join(posixpath.dirname(self.path), spec))
            out.extend(self.sink.anchor("sym", candidate, name) for candidate in (f"{base}.ts", f"{base}/index.ts"))
        out.append(self.sink.anchor("sym", self.path, name))
        return tuple(out)

    # ---- 扫描 ----

    def skip_space(self, pos: int, end: int) -> int:
        while pos < end and (self.masked[pos].isspace() or self.masked[pos] == ";"):
            pos += 1
        return pos

    def statements(self, start: int, end: int):
        """在 [start, end) 里切出语句：(装饰器, 头部起止, 花括号块起止或 None, 语句结束位置)。"""
        masked = self.masked
        pos = start
        decorators: list[Decorator] = []
        while True:
            pos = self.skip_space(pos, end)
            if pos >= end:
                return
            if masked[pos] == "@":
                match = _DECORATOR.match(masked, pos)
                if not match:
                    pos += 1
                    continue
                after = self.skip_space_only(match.end(), end)
                args = None
                if after < end and masked[after] == "(":
                    close = match_close(masked, after, end)
                    args = (after + 1, close)
                    pos = close + 1
                else:
                    pos = match.end()
                decorators.append(Decorator(match.group(1), match.start(), args))
                continue
            if re.match(r"import\b", masked[pos:pos + 7]):
                found = _IMPORT.match(self.text, pos)
                if found and found.end() <= end:
                    self.record_import(found.group(1), found.group(3))
                    pos = found.end()
                else:
                    newline = masked.find("\n", pos)
                    pos = end if newline < 0 else newline
                decorators = []
                continue
            head_end, block, stop = self.statement_end(pos, end)
            yield decorators, (pos, head_end), block, stop
            decorators = []
            pos = stop

    def skip_space_only(self, pos: int, end: int) -> int:
        while pos < end and self.masked[pos].isspace():
            pos += 1
        return pos

    def statement_end(self, pos: int, end: int) -> tuple[int, tuple[int, int] | None, int]:
        masked = self.masked
        depth = 0
        angle = 0
        position = pos
        while position < end:
            char = masked[position]
            outside = depth == 0 and angle == 0  # 不在括号或泛型尖括号里
            closes_angle = char == ">" and angle and masked[position - 1] != "="
            if char in "([":
                depth += 1
            elif char in ")]":
                depth -= 1
            elif depth == 0 and char == "<" and masked[position + 1:position + 2] != "=":
                angle += 1
            elif depth == 0 and closes_angle:
                angle -= 1
            elif char == "{":
                close = match_close(masked, position, end)
                if depth or angle:
                    position = close + 1
                    continue
                return position, (position + 1, close), close + 1
            elif depth == 0 and char == ";":
                return position, None, position + 1
            elif outside and char == "\n" and self._complete(pos, position, end):
                return position, None, position
            position += 1
        return end, None, end

    def _complete(self, start: int, newline: int, end: int) -> bool:
        head = self.masked[start:newline].rstrip()
        if not head or head.endswith(("=", ",", "(", "|", "&", ":", "=>", "+", "-", "?", ".", "<")):
            return False
        following = self.skip_space_only(newline, end)
        return following >= end or self.masked[following] not in ".?:|&=+-*/,)>]{"

    def record_import(self, names: str, spec: str) -> None:
        names = names.strip()
        default, _, rest = names.partition(",") if not names.startswith("{") else ("", "", names)
        if default.strip() and not default.strip().startswith(("{", "*")):
            self.imports[default.strip()] = spec
        for item in re.findall(r"\{([^}]*)\}", rest or names):
            for name in item.split(","):
                original, _, alias = name.strip().partition(" as ")
                key = (alias or original).strip()
                if key:
                    self.imports[key] = spec

    # ---- 文件 ----

    def run(self) -> None:
        if "setGlobalPrefix(" in self.masked:
            self.sink.note("Nest 的 setGlobalPrefix 前缀没有拼进路由", self.path)
        if re.search(r"\b(?:router|app)\.(?:get|post|put|patch|delete)\s*\(\s*['\"`]", self.masked + self.text):
            self.sink.note("Express 风格的路由没有识别", self.path)
        if "new EntitySchema" in self.masked:
            self.sink.note("TypeORM EntitySchema 写法没有识别", self.path)
        self.block(0, len(self.masked))

    def block(self, start: int, end: int) -> None:
        for decorators, (head_start, head_end), block, stop in self.statements(start, end):
            head = " ".join(self.masked[head_start:min(head_end, head_start + HEAD_MAX)].split())
            if block is None:
                continue
            match = _CLASS.match(head)
            if match:
                self.klass(match.group(1), match.group(2), decorators, head_start, block, stop)
                continue
            match = _ENUM.match(head)
            if match:
                self.enum(match.group(1), head_start, block, stop)
                continue
            match = _FUNCTION.match(head)
            if match:
                sql = self.sql_in(*block)
                if sql:
                    self.sink.operation(self.sink.anchor("sym", self.path, match.group(1)), match.group(1),
                                        language=LANGUAGE, file=self.path, lines=self.lines,
                                        line=self.line(head_start), end_line=self.line(stop - 1), kind="function",
                                        module=self.module, sql=sql)
                continue
            if re.match(r"^(?:export\s+)?(?:declare\s+)?(?:namespace|module)\b", head):
                self.block(*block)

    def enum(self, name: str, head_start: int, block: tuple[int, int], stop: int) -> None:
        values = []
        for start, end in split_top(self.masked, *block):
            member = self.masked[start:end].split("=", 1)[0].strip()
            literal = self.literals_in(start, end)
            if member.startswith(("'", "\"")) and literal:
                member = literal[0].value
            if member:
                values.append(member)
        self.sink.entity(self.sink.anchor("sym", self.path, name), name, language=LANGUAGE, file=self.path,
                         lines=self.lines, line=self.line(head_start), end_line=self.line(stop - 1), kind="enum",
                         module=self.module, label=doc_label(self.doc_before(head_start)), values=values)

    # ---- 类 ----

    def klass(self, name: str, base: str | None, decorators: list[Decorator], head_start: int,
              block: tuple[int, int], stop: int) -> None:
        names = {item.name.rsplit(".", 1)[-1]: item for item in decorators}
        members = list(self.members(*block))
        properties = [member for member in members if member["kind"] == "property"]
        property_decorators = [decorator for member in properties for decorator in member["decorators"]]
        validated = any(self.is_validator(decorator.name) for decorator in property_decorators)
        if "Schema" in names and self.imports.get("Schema", "").startswith("@nestjs/mongoose"):
            self.sink.note("Mongoose 的 @Schema / @Prop 模型没有识别", self.path)
        if "Table" in names and self.imports.get("Table", "").startswith("sequelize"):
            self.sink.note("sequelize-typescript 模型没有识别", self.path)
        role = None
        if "Entity" in names:
            role = "entity"
        elif "Controller" in names:
            role = "controller"
        elif name.endswith(SERVICE_SUFFIXES):
            role = "service"
        elif validated:
            role = "dto"
        sql = {id(member): self.sql_in(*member["block"]) if member["block"] else [] for member in members
               if member["kind"] == "method"}
        if role is None and not any(sql.values()):
            return
        start = min([item.start for item in decorators] + [head_start])
        table = None
        if role == "entity":
            args = self.args(names["Entity"])
            table = args[0] if args and isinstance(args[0], str) and not args[0].startswith("{") else \
                self.option(names["Entity"], "name")
        anchor = self.sink.entity(
            self.sink.anchor("sym", self.path, name), name, language=LANGUAGE, file=self.path, lines=self.lines,
            line=self.line(start), end_line=self.line(stop - 1), kind="class", module=self.module,
            label=doc_label(self.doc_before(start)), confidence="declared" if role == "entity" else "observed",
            table=table if isinstance(table, str) else None, orm="typeorm" if role == "entity" else None, role=role,
            decorators=sorted(names) or None)
        if base:
            self.sink.relation(anchor, "inherits", language=LANGUAGE, file=self.path, line=self.line(head_start),
                               target=base.rsplit(".", 1)[-1], candidates=self.candidates(base.rsplit(".", 1)[-1]),
                               confidence="observed", optional=True)
        types: dict[str, str] = {}
        for member in members:
            if member["kind"] == "constructor":
                types.update(member["params"])
            elif member["kind"] == "property" and member["type"]:
                types[member["name"]] = re.split(r"[<\[|\s]", member["type"])[0]
        for member in members:
            if member["kind"] == "property" and role in ("entity", "dto"):
                self.field(anchor, member, role)
        prefixes = [""]
        if role == "controller":
            controller = names["Controller"]
            args = self.args(controller)
            value = self.option(controller, "path") if args and str(args[0]).startswith("{") else \
                (args[0] if args else "")
            prefixes = value if isinstance(value, list) else [value]
        for member in members:
            if member["kind"] != "method":
                continue
            routes = [item for item in member["decorators"] if item.name in ROUTES]
            public = not member["modifiers"] & {"private", "protected"} and not member["name"].startswith("_")
            controller_route = role == "controller" and bool(routes)
            public_service = role == "service" and public
            if controller_route or public_service or sql[id(member)]:
                self.method(anchor, name, member, role, sql[id(member)], types, routes, prefixes)

    def members(self, start: int, end: int):
        for decorators, (head_start, head_end), block, stop in self.statements(start, end):
            head_end = min(head_end, head_start + HEAD_MAX)
            head = " ".join(self.masked[head_start:head_end].split())
            raw_head = self.text[head_start:head_end]
            if head.startswith("constructor"):
                open_paren = self.masked.find("(", head_start, head_end if head_end > head_start else end)
                params = {}
                if open_paren >= 0:
                    close = match_close(self.masked, open_paren, end)
                    for p_start, p_end in split_top(self.masked, open_paren + 1, close):
                        text = re.sub(r"@\w+(?:\([^)]*\))?", " ", self.masked[p_start:p_end])
                        found = _PARAM_PROPERTY.search(" ".join(text.split()))
                        if found:
                            params[found.group(1)] = found.group(2)
                yield {"kind": "constructor", "params": params, "decorators": decorators}
                continue
            match = _METHOD.match(head)
            if match and match.group(2) not in KEYWORDS and "=" not in head.split("(", 1)[0]:
                modifiers = set(match.group(1).split())
                params = re.sub(r"@\w+(?:\([^)]*\))?\s*", "", " ".join(raw_head[raw_head.find("(") + 1:
                                                                               raw_head.rfind(")")].split()))
                returns = (match.group(4) or "").strip()
                yield {"kind": "method", "name": match.group(2), "modifiers": modifiers, "decorators": decorators,
                       "start": min([item.start for item in decorators] + [head_start]), "head": head_start,
                       "stop": stop, "block": block,
                       "signature": f"{match.group(2)}({params})" + (f": {returns}" if returns else "")}
                continue
            match = _PROPERTY.match(head)
            if match and match.group(2) not in KEYWORDS:
                type_text = (match.group(4) or "").strip()
                if match.group(4):
                    type_start = self.masked.find(":", head_start, head_end)
                    type_text = " ".join(self.text[type_start + 1:head_end].split("=", 1)[0].split()) \
                        if type_start >= 0 else type_text
                yield {"kind": "property", "name": match.group(2), "modifiers": set(match.group(1).split()),
                       "optional": match.group(3) == "?", "type": type_text or None, "decorators": decorators,
                       "start": min([item.start for item in decorators] + [head_start]), "head": head_start,
                       "stop": stop, "block": block}

    def is_validator(self, name: str) -> bool:
        spec = self.imports.get(name)
        if spec is not None:
            return spec == "class-validator"
        return name in VALIDATORS or bool(re.match(r"^Is[A-Z]", name))

    # ---- 成员 ----

    def field(self, entity: str, member: dict, role: str) -> None:
        decorators = {item.name: item for item in member["decorators"]}
        line, end = self.line(member["start"]), self.line(max(member["head"], member["stop"] - 1))
        column = next((decorators[name] for name in TYPEORM_COLUMNS if name in decorators), None)
        join = decorators.get("JoinColumn")
        options = {}
        if column is not None:
            args = self.args(column)
            options["column_type"] = args[0] if args and isinstance(args[0], str) and not args[0].startswith("{") \
                else self.option(column, "type")
            for key in ("name", "nullable", "length", "unique", "comment"):
                options[key] = self.option(column, key)
        join_name = self.option(join, "name") if join is not None else None
        nullable = options.get("nullable")
        if column is not None and column.name.startswith("Primary"):
            nullable = False
        elif nullable is None and member["optional"]:
            nullable = True
        validations = [item.name + ("(" + ", ".join(str(arg) for arg in self.args(item)) + ")"
                                    if self.args(item) else "")
                       for item in member["decorators"] if self.is_validator(item.name)]
        label = options.get("comment")
        anchor = self.sink.field(
            entity, member["name"], language=LANGUAGE, file=self.path, line=line, end_line=end,
            data_type=member["type"], module=self.module,
            label=explicit_label(label) if isinstance(label, str) else doc_label(self.doc_before(member["start"])),
            confidence="declared" if role == "entity" else "observed",
            column=options.get("name") or join_name, column_type=options.get("column_type"),
            nullable=nullable if isinstance(nullable, bool) else None, length=options.get("length"),
            pk=True if column is not None and column.name.startswith("Primary") else None,
            validations=validations or None)
        if options.get("unique") is True:
            self.sink.rule(anchor, "unique", language=LANGUAGE, file=self.path, line=line, end_line=end,
                           framework="typeorm", module=self.module)
        for item in member["decorators"]:
            if self.is_validator(item.name):
                args = self.args(item)
                self.sink.rule(anchor, item.name, language=LANGUAGE, file=self.path, line=self.line(item.start),
                               end_line=end, framework="class-validator",
                               params={"args": args} if args else None, module=self.module)
        for name, relation in TYPEORM_RELATIONS.items():
            if name not in decorators:
                continue
            decorator = decorators[name]
            parts = split_top(self.masked, *decorator.args) if decorator.args else []
            target = ""
            inverse = None
            if parts:
                first = self.text[parts[0][0]:parts[0][1]]
                found = _ARROW_TARGET.match(first)
                target = found.group(1) if found else str(self.value(*parts[0])).strip()
            if len(parts) > 1:
                found = _INVERSE.search(self.text[parts[1][0]:parts[1][1]])
                inverse = found.group(1) if found else None
            if not target and member["type"]:
                target = re.split(r"[<\[|\s]", member["type"])[0]
            self.sink.relation(entity, relation, language=LANGUAGE, file=self.path, line=line, target=target,
                               candidates=self.candidates(target), via=anchor, inverse=inverse,
                               join_column=join_name, cascade=self.option(decorator, "cascade"),
                               nullable=self.option(decorator, "nullable"))

    def method(self, entity: str, class_name: str, member: dict, role: str | None, sql: list[str], types: dict,
               routes: list[Decorator], prefixes: list) -> None:
        line, end = self.line(member["start"]), self.line(max(member["head"], member["stop"] - 1))
        modifiers = member["modifiers"]
        visibility = next((item for item in ("private", "protected", "public") if item in modifiers), "public")
        anchor = self.sink.operation(
            self.sink.member(self.sink.anchor("sym", self.path, class_name), member["name"]), member["name"],
            language=LANGUAGE, file=self.path, lines=self.lines, line=line, end_line=end, kind="method",
            parent=entity, module=self.module, label=doc_label(self.doc_before(member["start"])), sql=sql,
            signature=member["signature"][:300], visibility=visibility, role=role,
            asynchronous=True if "async" in modifiers else None)
        if member["block"]:
            body = self.masked[member["block"][0]:member["block"][1]]
            for found in _THIS_CALL.finditer(body):
                target = types.get(found.group(1))
                if target:
                    self.sink.relation(anchor, "calls", language=LANGUAGE, file=self.path,
                                       line=self.line(member["block"][0] + found.start()), target=target,
                                       candidates=self.candidates(target), member=found.group(2),
                                       confidence="inferred")
        for decorator in routes:
            args = self.args(decorator)
            paths = args[0] if args and isinstance(args[0], list) else (args[:1] or [""])
            for prefix in prefixes:
                for path in paths:
                    literal = all(isinstance(item, str) and not isinstance(item, Ref) for item in (prefix, path))
                    if not literal:
                        self.sink.note("路由路径用了常量，没有展开", self.path)
                    url = join_path(*(item if isinstance(item, str) and not isinstance(item, Ref)
                                      else "{" + str(item) + "}" for item in (prefix, path)))
                    self.sink.route(ROUTES[decorator.name], url, anchor, language=LANGUAGE, file=self.path,
                                    lines=self.lines, line=line, end_line=end, module=self.module,
                                    confidence="observed" if literal else "inferred")

    def sql_in(self, start: int, end: int) -> list[str]:
        found = []
        for literal in self.literals_in(start, end):
            value = literal.value.strip()
            if literal.kind != "regex" and looks_like_sql(value) and value not in found:
                found.append(value)
        return found


def analyze(sink: Sink, path: str, text: str) -> None:
    TsFile(sink, path, text).run()
