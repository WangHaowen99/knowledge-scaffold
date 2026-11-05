"""代码分析的记录构造（证据采集接口 §10）：Java、Python、TypeScript 三个分析器都经 `Sink` 出记录。

锚点、位置、片段、置信度在这里统一；同一快照里撞名的锚点第二个起加 `~2`。关联和调用的目标要等全部文件分析完
才知道（目标类可能在后面的文件里），先挂起，`finish()` 时解析再发。
"""

from __future__ import annotations

import re
from typing import Callable

from ...evidence import spec as evidence_spec

PROVIDER = "code-repo@0.1.0"
SNIPPET_LINES = 30
SQL_MAX = 2000
LABEL_MAX = 60
NOTE_FILES = 5
_SQL = re.compile(r"^\s*(select|insert\s+into|update|delete\s+from|with|merge\s+into)\b", re.IGNORECASE)
_SQL_BODY = re.compile(r"\b(from|into|set|join|values)\b", re.IGNORECASE)
_TABLES = re.compile(r"\b(?:from|join|update|into)\s+([A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)?|[`\"][^`\"]+[`\"])",
                     re.IGNORECASE)
_CJK = re.compile(r"[一-鿿]")
_SQL_WORDS = {"select", "where", "set", "values", "lateral", "unnest"}


def looks_like_sql(text: str) -> bool:
    return len(text) >= 15 and bool(_SQL.match(text)) and bool(_SQL_BODY.search(text))


def sql_tables(sql: str) -> list[str]:
    tables: list[str] = []
    for match in _TABLES.finditer(sql):
        name = match.group(1).strip("`\"")
        if name.lower() not in _SQL_WORDS and name not in tables:
            tables.append(name)
    return tables


def doc_label(text: str | None) -> str | None:
    """注释、文档串的第一行当中文名；不含中文的不算（英文说明不是业务名）。"""
    for line in str(text or "").splitlines():
        line = line.strip().strip("/*").strip()
        if line:
            return line[:LABEL_MAX] if _CJK.search(line) else None
    return None


def explicit_label(text: str | None) -> str | None:
    text = str(text or "").strip()
    return text[:LABEL_MAX] if text else None


def scalar(value):
    """记录里不放浮点数：数字以外的原样，浮点转字符串。"""
    if isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        return value
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return [scalar(item) for item in value]
    if isinstance(value, dict):
        return {str(key): scalar(item) for key, item in value.items()}
    return str(value)


class Sink:
    def __init__(self, source_id: str, emit: Callable[[dict], None]):
        self.prefix = f"code:{source_id}/"
        self._emit = emit
        self.ids: set[str] = set()
        self.entities: dict[str, dict] = {}
        self.operations: set[str] = set()
        self._by_name: dict[tuple[str, str], list[str]] = {}
        self._by_table: dict[str, str] = {}
        self._pending: list[dict] = []
        self.notes: dict[str, list[str]] = {}
        self.counts: dict[str, int] = {}

    # ---- 锚点 ----

    def anchor(self, kind: str, *segments: str) -> str:
        return f"{self.prefix}{kind}:{evidence_spec.qualified(*segments)}"

    def member(self, parent: str, name: str) -> str:
        return f"{parent}{evidence_spec.SEPARATOR}{evidence_spec.encode_segment(name)}"

    def _unique(self, anchor: str) -> str:
        if anchor not in self.ids:
            return anchor
        index = 2
        while f"{anchor}~{index}" in self.ids:
            index += 1
        return f"{anchor}~{index}"

    def note(self, category: str, file: str) -> None:
        """识别不了的写法：按类别汇总，`limitations()` 每类一条。"""
        files = self.notes.setdefault(category, [])
        if file not in files:
            files.append(file)

    def limitations(self) -> list[str]:
        out = []
        for category, files in self.notes.items():
            shown = "、".join(files[:NOTE_FILES]) + (f" 等 {len(files)} 个文件" if len(files) > NOTE_FILES else "")
            out.append(f"{category}：{shown}")
        return out

    # ---- 记录 ----

    def _record(self, anchor: str, type_: str, parent: str | None, name: str, label: str | None, attrs: dict,
                method: str, confidence: str, **extra) -> str:
        anchor = self._unique(anchor)
        record = {"id": anchor, "type": type_, "parent": parent, "name": name, "label": label,
                  "attrs": scalar({key: value for key, value in attrs.items() if value is not None}),
                  "evidence": {"provider": PROVIDER, "method": f"code-repo {method}", "confidence": confidence},
                  **scalar(extra)}
        self._emit(record)
        self.ids.add(anchor)
        self.counts[type_] = self.counts.get(type_, 0) + 1
        return anchor

    @staticmethod
    def loc(file: str, line: int, end_line: int) -> dict:
        return {"file": file, "line": int(line), "end_line": int(max(line, end_line))}

    @staticmethod
    def snippet(lines: list[str], line: int, end_line: int) -> str:
        start = max(1, int(line))
        end = min(int(max(start, end_line)), start + SNIPPET_LINES - 1, len(lines))
        return "\n".join(lines[start - 1:end])

    def entity(self, anchor: str, name: str, *, language: str, file: str, lines: list[str], line: int,
               end_line: int, kind: str = "class", module: str = "", label: str | None = None,
               confidence: str = "observed", table: str | None = None, **attrs) -> str:
        attrs = {"kind": kind, "module": module, "loc": self.loc(file, line, end_line),
                 "snippet": self.snippet(lines, line, end_line), "table": table, **attrs}
        anchor = self._record(anchor, "entity", None, name, label, attrs, language, confidence)
        self.entities[anchor] = {"language": language, "module": module, "name": name}
        self._by_name.setdefault((language, name), []).append(anchor)
        if table:
            self._by_table.setdefault(table.lower(), anchor)
        return anchor

    def field(self, parent: str, name: str, *, language: str, file: str, line: int, end_line: int,
              data_type: str | None, module: str = "", label: str | None = None, confidence: str = "observed",
              **attrs) -> str:
        attrs = {"kind": "field", "module": module, "loc": self.loc(file, line, end_line), **attrs}
        return self._record(self.member(parent, name), "field", parent, name, label, attrs, language, confidence,
                            data_type=data_type)

    def operation(self, anchor: str, name: str, *, language: str, file: str, lines: list[str], line: int,
                  end_line: int, kind: str, parent: str | None = None, module: str = "", label: str | None = None,
                  confidence: str = "observed", sql: list[str] | None = None, **attrs) -> str:
        attrs = {"kind": kind, "module": module, "loc": self.loc(file, line, end_line),
                 "snippet": self.snippet(lines, line, end_line), **attrs}
        if sql:
            joined = "\n;\n".join(sql)
            attrs["sql"] = joined[:SQL_MAX]
            attrs["tables"] = sql_tables(joined)
        anchor = self._record(anchor, "operation", parent, name, label, attrs, language, confidence)
        self.operations.add(anchor)
        return anchor

    def route(self, http_method: str, path: str, handler: str, *, language: str, file: str, lines: list[str],
              line: int, end_line: int, module: str = "", confidence: str = "observed", **attrs) -> str:
        name = f"{http_method} {path}"
        return self.operation(f"{self.prefix}route:{evidence_spec.encode_segment(name)}", name, language=language,
                              file=file, lines=lines, line=line, end_line=end_line, kind="route", module=module,
                              confidence=confidence, method=http_method, url=path, handler=handler, **attrs)

    def rule(self, target: str, rule: str, *, language: str, file: str, line: int, end_line: int,
             framework: str, params: dict | None = None, module: str = "", **attrs) -> str:
        path = target[len(self.prefix):].split(":", 1)[1]
        anchor = f"{self.prefix}rule:{path}{evidence_spec.SEPARATOR}{evidence_spec.encode_segment(rule)}"
        attrs = {"kind": "validation", "module": module, "loc": self.loc(file, line, end_line), "rule": rule,
                 "target": target, "framework": framework, "params": params or None, **attrs}
        return self._record(anchor, "rule", target, rule, None, attrs, language, "declared")

    # ---- 关联：先挂起，全部文件分析完再解析 ----

    def relation(self, from_: str, name: str, *, language: str, file: str, line: int, target: str = "",
                 candidates: tuple[str, ...] = (), table: str = "", via: str = "", member: str = "",
                 confidence: str = "declared", optional: bool = False, **attrs) -> None:
        """`target` 是目标类型的简单名，`candidates` 是按优先级猜的目标锚点（导入、同包），`table` 按表名找；
        `member` 不空时目标是那个类的成员（调用关系）。`member` 或 `optional` 时找不到目标就不发。"""
        self._pending.append({"from": from_, "name": name, "language": language, "file": file, "line": line,
                              "target": target, "candidates": candidates, "table": table, "via": via,
                              "member": member, "confidence": confidence, "optional": optional or bool(member),
                              "attrs": attrs})

    def resolve(self, language: str, target: str, candidates: tuple[str, ...] = (), module: str = "",
                table: str = "") -> str | None:
        for candidate in candidates:
            if candidate in self.entities:
                return candidate
        if table and table.lower() in self._by_table:
            return self._by_table[table.lower()]
        found = self._by_name.get((language, target), [])
        if len(found) == 1:
            return found[0]
        same = [anchor for anchor in found if self.entities[anchor]["module"] == module]
        return same[0] if len(same) == 1 else None

    def finish(self) -> None:
        pending, self._pending = self._pending, []
        for item in pending:
            module = self.entities.get(item["from"], {}).get("module", "")
            to = self.resolve(item["language"], item["target"], item["candidates"], module, item["table"])
            if item["member"] and to is not None:
                to = self.member(to, item["member"]) if self.member(to, item["member"]) in self.operations else None
            if to is None and (item["optional"] or not (item["target"] or item["table"])):
                continue
            key = to or item["target"] or item["table"]
            digest = evidence_spec.sha256_text(f"{item['from']}|{item['name']}|{key}|{item['via']}")[:16]
            attrs = {"loc": {"file": item["file"], "line": int(item["line"]), "end_line": int(item["line"])},
                     "via": item["via"] or None, **item["attrs"]}
            if to is None:
                attrs["target"] = item["target"] or item["table"]
            self._record(f"{self.prefix}rel:{digest}", "relation", None, item["name"], None, attrs,
                         item["language"], item["confidence"], **{"from": item["from"], "to": to})


def block_end(lines: list[str], start: int, *, open_char: str = "{", close_char: str = "}",
              stop_at_semicolon: bool = True) -> int:
    """从第 `start` 行（1 起）找声明的结束行：配对花括号，跳过字符串和注释；花括号前先遇到 `;` 就结束在那一行。"""
    depth = 0
    seen = False
    in_block_comment = False
    quote = ""
    for index in range(max(0, start - 1), len(lines)):
        line = lines[index]
        position = 0
        while position < len(line):
            char = line[position]
            pair = line[position:position + 2]
            if in_block_comment:
                if pair == "*/":
                    in_block_comment = False
                    position += 1
            elif quote:
                if char == "\\":
                    position += 1
                elif char == quote:
                    quote = ""
            elif pair == "//":
                break
            elif pair == "/*":
                in_block_comment = True
                position += 1
            elif char in "\"'`":
                quote = char
            elif char == open_char:
                depth += 1
                seen = True
            elif char == close_char:
                depth -= 1
                if seen and depth <= 0:
                    return index + 1
            elif char == ";" and not seen and stop_at_semicolon:
                return index + 1
            position += 1
        if quote in "\"'":
            quote = ""
    return len(lines) if seen else start


def join_path(*parts: str) -> str:
    """路由前缀和路径拼起来：保证以 `/` 开头、去掉重复和末尾的 `/`。"""
    joined = "/" + "/".join(part.strip("/") for part in parts if part and part.strip("/"))
    return re.sub(r"/{2,}", "/", joined)
