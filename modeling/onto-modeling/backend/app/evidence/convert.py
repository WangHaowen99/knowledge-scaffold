"""文档转换协议（ADR-0073；详设 5.8.3「文件资料」）：连接器里的转换器接口、结果、位置标记。

转换器跑在连接器网关的子进程里，内核不导入转换器代码。这里只有协议、数据结构和纯函数（位置标记的写法与解析、
Markdown 表格、OOXML 解压上限），网关、转换器、内核三边共用，只依赖标准库。

一个转换器 = 一个连接器包（`kind: provider`）：清单里的 `converter` 段声明入口、文件类型、许可证，入口模块提供
`convert(data, filename, limits) -> Conversion`。
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import asdict, dataclass, field
from typing import Literal, Protocol, runtime_checkable

FORMAT = "markdown-locations/1"
# 转换结果的状态：converted 转成了文本；unsupported 暂不支持（扫描件、图片、加密、老格式），不是错
ResultState = Literal["converted", "unsupported"]
# 转换失败的原因码（网关回 state=failed 时带上）；文案给用户看，短
FAILURES = {
    "corrupt": "文件损坏，打不开",
    "too_large": "解压后内容超过上限",
    "timeout": "转换超时",
    "memory": "转换时内存超限",
    "output_too_large": "转换结果超过上限",
    "crashed": "转换出错",
}
# 本身就是文本、不经网关的资料
NATIVE_TEXT_TYPES = (".md", ".markdown", ".txt", ".csv")


@dataclass(frozen=True)
class ConverterInfo:
    """转换器的描述，来自包清单（详设 5.8.3「提供方清单要声明的内容」里和转换相关的几项）。"""

    id: str                          # 包名，如 convert-pdf
    version: str
    label: str
    types: tuple[str, ...]           # 能转的扩展名，小写带点，如 (".pdf",)
    license_note: str
    runtime: Literal["subprocess"] = "subprocess"
    output_format: str = FORMAT

    def to_dict(self) -> dict:
        return {**asdict(self), "types": list(self.types)}


@dataclass(frozen=True)
class ConvertLimits:
    """单次转换的上限：网关按票据预算与自身上限取小的，交给子进程。"""

    max_pages: int = 1000            # PDF 只转前这么多页
    # Excel 的表头总是全部工作表、全部列（对象抽取按表头取表名、列名）；每个工作表连表头最多转这么多行、这么多格，
    # 宽表少转几行数据（50 列的表照旧 2000 行）；全文仍不超过 max_output_chars
    max_rows: int = 2000
    max_sheet_cells: int = 100_000
    max_output_chars: int = 4_000_000
    max_expanded_bytes: int = 100 * 1024 * 1024   # OOXML 解压后的总大小
    max_zip_members: int = 5000

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict | None) -> "ConvertLimits":
        known = {key: int(value) for key, value in (data or {}).items() if key in cls.__dataclass_fields__}
        return cls(**known)


@dataclass(frozen=True)
class Location:
    """位置对照的一项：Markdown 第 line 行（从 1 起）是一个位置标记。"""

    line: int
    kind: Literal["page", "sheet"]
    value: str
    label: str                       # 「第 3 页」「工作表 客户」


@dataclass
class Conversion:
    state: ResultState
    markdown: str = ""
    locations: list[Location] = field(default_factory=list)
    reason: str | None = None        # 暂不支持的原因
    note: str | None = None          # 转换时的说明，如「只转了前 1000 页（共 1200 页）」
    stats: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"state": self.state, "markdown": self.markdown, "locations": [asdict(item) for item in self.locations],
                "reason": self.reason, "note": self.note, "stats": dict(self.stats)}


class ConvertError(Exception):
    """转换失败（不是「暂不支持」）：code 取 FAILURES 的键。"""

    def __init__(self, code: str, message: str | None = None):
        super().__init__(message or FAILURES.get(code, FAILURES["crashed"]))
        self.code = code if code in FAILURES else "crashed"


@runtime_checkable
class DocumentConverter(Protocol):
    """网关把清单和入口模块包成这个接口；内核只经网关调用。"""

    def describe(self) -> ConverterInfo: ...

    def convert(self, data: bytes, filename: str, limits: ConvertLimits) -> Conversion: ...


def unsupported(reason: str, **stats) -> Conversion:
    return Conversion(state="unsupported", reason=reason, stats=stats)


# ---------------------------------------------------------------------------
# 位置标记：PDF 每页前一行、Excel 每个工作表前一行（HTML 注释，渲染时不显示，助手读得到）
# ---------------------------------------------------------------------------

_PAGE_RE = re.compile(r"^<!-- 第 (\d+) 页 -->$")
_SHEET_RE = re.compile(r"^<!-- 工作表：(.+) -->$")
HEADER_PREFIX = "<!-- onto:text "
_HEADER_RE = re.compile(r'^<!-- onto:text source="(?P<source>[^"]*)" sha256="(?P<sha>[0-9a-f]{16,64})" '
                        r'converter="(?P<converter>[^"]*)" -->$')


def _clean(text: str) -> str:
    """标记里不能出现提前结束注释的字符。"""
    return re.sub(r"\s+", " ", str(text).replace("--", "—").replace(">", "＞").replace('"', "'")).strip()


def page_marker(number: int) -> str:
    return f"<!-- 第 {int(number)} 页 -->"


def sheet_marker(name: str) -> str:
    return f"<!-- 工作表：{_clean(name) or '未命名'} -->"


def parse_locations(markdown: str) -> list[Location]:
    found = []
    for number, line in enumerate(str(markdown or "").splitlines(), 1):
        stripped = line.strip()
        page = _PAGE_RE.match(stripped)
        if page:
            found.append(Location(number, "page", page.group(1), f"第 {page.group(1)} 页"))
            continue
        sheet = _SHEET_RE.match(stripped)
        if sheet:
            found.append(Location(number, "sheet", sheet.group(1), f"工作表 {sheet.group(1)}"))
    return found


def location_at(markdown: str, line: int) -> Location | None:
    """第 line 行属于哪个位置：它前面最近的那个标记。"""
    current = None
    for item in parse_locations(markdown):
        if item.line > line:
            break
        current = item
    return current


# ---------------------------------------------------------------------------
# 原件锚点（ADR-0093）：文本版末尾一段注释，记哪一行对应原件的哪个位置，写法和工具箱 source_extract 一致——
# Word 段落 p:3、表格行 table:1/row:2（第 n 列就是 cell:n），Excel 用「sheet:工作表名」开一个工作表、「row:5」
# 记 Excel 行号（第 n 列就是第 n 个列字母），「merge 工作表名 A1:C1 …」记合并区；PDF 的页看页标记，不进这里。
# 行号从正文第一行数起（不含内核加的文件头和空行）。渲染时整段注释不显示，表格、标记的解析都不受影响。
# 工作区里的 source_extract.py 有同口径的一份：助手读文本版、主服务定位都只读这段，不解析原件（ADR-0076）。
# ---------------------------------------------------------------------------

ANCHORS_HEAD = "<!-- onto:anchors 1"
ANCHORS_END = "-->"


def _escape_anchor(text: str) -> str:
    """工作表名里可能有 `-->`、换行：百分号编码 %、>、换行，其余原样（助手读得懂）。"""
    return str(text).replace("%", "%25").replace(">", "%3E").replace("\n", "%0A").replace("\r", "%0D")


def _unescape_anchor(text: str) -> str:
    return text.replace("%0D", "\r").replace("%0A", "\n").replace("%3E", ">").replace("%25", "%")


def _sheet_token(name: str) -> str:
    return _escape_anchor(name).replace(" ", "%20")


def anchor_index(lines: list[tuple[int, str]], merges: dict[str, list[str]] | None = None,
                 cols: dict[str, int] | None = None) -> str:
    """[(正文行号, 锚点)]、合并区、各工作表有值的最右一列 → 文本版末尾的锚点注释；什么都没有时空串。"""
    if not lines and not merges and not cols:
        return ""
    out = [ANCHORS_HEAD]
    out += [f"{number} {_escape_anchor(anchor)}" for number, anchor in lines]
    for sheet, refs in (merges or {}).items():
        if refs:
            out.append(f"merge {_sheet_token(sheet)} {' '.join(refs)}")
    # 整张表有值的最右一列（扫全部行，不受转换行数上限影响；Sheet 范围预检按它报「表格比表头宽」）
    out += [f"cols {_sheet_token(sheet)} {int(width)}" for sheet, width in (cols or {}).items()]
    out.append(ANCHORS_END)
    return "\n".join(out)


def parse_anchor_index(markdown: str) -> dict | None:
    """文本版 → {lines: {文件行号: 锚点}, marks: {文件行号: 转换器加的记号长度}, merges: {工作表: [A1:C1]},
    start: 注释开始的文件行号}；没有这段时 None。

    文件第一行是内核写的文件头时，正文从文件头之后起（内核写文件头 + 空行，正文从第三行起；文件头后面没有空行的
    也认）。段落行可带记号长度（「p:3 2」：标题、列表的「# 」「- 」是转换器加的，读回原文时去掉）。
    另有 cols：{工作表: 有值的最右一列}。"""
    lines = str(markdown or "").splitlines()
    try:
        start = next(i for i in range(len(lines) - 1, -1, -1) if lines[i].strip() == ANCHORS_HEAD)
    except StopIteration:
        return None
    offset = (2 if len(lines) > 1 and not lines[1].strip() else 1) if lines and parse_header(lines[0]) is not None else 0
    found: dict[int, str] = {}
    marks: dict[int, int] = {}
    merges: dict[str, list[str]] = {}
    cols: dict[str, int] = {}
    for raw in lines[start + 1:]:
        line = raw.strip()
        if line == ANCHORS_END:
            break
        number, _, anchor = line.partition(" ")
        if number == "merge":
            sheet, _, refs = anchor.partition(" ")
            merges.setdefault(_unescape_anchor(sheet.replace("%20", " ")), []).extend(refs.split())
        elif number == "cols":
            sheet, _, width = anchor.partition(" ")
            if width.strip().isdigit():
                cols[_unescape_anchor(sheet.replace("%20", " "))] = int(width)
        elif number.isdigit() and anchor:
            mark = re.fullmatch(r"(p:\d+) (\d+)", anchor)
            if mark:
                anchor = mark.group(1)
                marks[int(number) + offset] = int(mark.group(2))
            found[int(number) + offset] = _unescape_anchor(anchor)
    return {"lines": found, "marks": marks, "merges": merges, "cols": cols, "start": start + 1}


def header(source: str, sha256: str, converter: str) -> str:
    """文本版第一行：原文件名、原文件修订、转换器与版本（内核写，转换器不写）。"""
    return f'{HEADER_PREFIX}source="{_clean(source)}" sha256="{sha256}" converter="{_clean(converter)}" -->'


def parse_header(markdown: str) -> dict | None:
    first = str(markdown or "").split("\n", 1)[0].strip()
    match = _HEADER_RE.match(first)
    return match.groupdict() if match else None


def strip_forged_headers(markdown: str) -> str:
    """转换器的输出里不许有内核的文件头（不然能冒充原文件修订）。"""
    return "\n".join(line for line in str(markdown).splitlines() if not line.strip().startswith(HEADER_PREFIX))


# ---------------------------------------------------------------------------
# 转换器共用的小工具
# ---------------------------------------------------------------------------


def md_cell(value) -> str:
    text = "" if value is None else str(value)
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\r\n", "\n").replace("\n", "<br>").strip()


def md_table(rows: list[list]) -> str:
    """第一行当表头；列数按最宽的一行补齐。空表返回空串。"""
    rows = [[md_cell(cell) for cell in row] for row in rows if any(str(cell or "").strip() for cell in row)]
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    lines += ["| " + " | ".join(row) + " |" for row in rows[1:]]
    return "\n".join(lines)


def _md_row(line: str) -> list[str]:
    """一行 Markdown 表格 → 单元格（md_cell 的逆：反斜杠转义、<br> 换行）。"""
    body = line.strip()
    body = body[1:] if body.startswith("|") else body
    cells, current, escaped = [], [], False
    for char in body:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "|":
            cells.append("".join(current))
            current = []
        else:
            current.append(char)
    if current and "".join(current).strip():
        cells.append("".join(current))
    return [cell.strip().replace("<br>", "\n") for cell in cells]


def md_tables(markdown: str) -> dict[str, list[list[str]]]:
    """Excel 文本版 → {工作表名: 行}（每个工作表标记后面第一张表，表头在第一行）。md_table 的逆，
    给 schema 表头与取值画像用（F4c：不再解析 Excel 原件）。工作区里的 table_profile.py 有同口径的一份。"""
    tables: dict[str, list[list[str]]] = {}
    sheet, rows, done = None, [], set()
    for line in str(markdown or "").splitlines() + [""]:
        stripped = line.strip()
        marker = _SHEET_RE.match(stripped)
        if marker or not stripped.startswith("|"):
            if sheet is not None and rows and sheet not in done:
                tables[sheet] = rows
                done.add(sheet)
            rows = []
            if marker:
                sheet = marker.group(1)
            continue
        cells = _md_row(stripped)
        if cells and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
            continue
        rows.append(cells)
    return tables


def check_ooxml(data: bytes, limits: ConvertLimits) -> None:
    """Word、Excel 是 zip：打开前先看成员数和解压后的总大小，挡住 zip 炸弹。"""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        raise ConvertError("corrupt") from exc
    if len(members) > limits.max_zip_members:
        raise ConvertError("too_large")
    if sum(max(item.file_size, 0) for item in members) > limits.max_expanded_bytes:
        raise ConvertError("too_large")


def cap_output(markdown: str, limits: ConvertLimits) -> tuple[str, bool]:
    if len(markdown) <= limits.max_output_chars:
        return markdown, False
    cut = markdown.rfind("\n", 0, limits.max_output_chars)
    return markdown[: cut if cut > 0 else limits.max_output_chars], True
