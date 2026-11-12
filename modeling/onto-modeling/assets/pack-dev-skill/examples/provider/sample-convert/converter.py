"""键值文本（.kvtxt）转 Markdown：每行「键=值」转成两列表格的一行；# 开头的行是注释，空行跳过。

跑在连接器网关的子进程里（有超时、内存上限，根目录只读，没有网络），只用标准库。
本地自测：python3 converter.py 某个文件.kvtxt（输出和网关子进程写回的 JSON 一样）。
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

try:  # 网关里有平台的转换协议
    from app.evidence.convert import Conversion, ConvertError, ConvertLimits
except ImportError:  # 本地自测用同形状的替身；网关里不会走到这里
    from dataclasses import dataclass, field

    @dataclass
    class Conversion:
        state: str
        markdown: str = ""
        locations: list = field(default_factory=list)
        reason: str | None = None
        note: str | None = None
        stats: dict = field(default_factory=dict)

        def to_dict(self) -> dict:
            return {"state": self.state, "markdown": self.markdown, "locations": list(self.locations),
                    "reason": self.reason, "note": self.note, "stats": dict(self.stats)}

    class ConvertError(Exception):
        def __init__(self, code: str, message: str | None = None):
            super().__init__(message or code)
            self.code = code

    @dataclass(frozen=True)
    class ConvertLimits:
        max_pages: int = 1000
        max_rows: int = 2000
        max_sheet_cells: int = 100_000
        max_output_chars: int = 4_000_000
        max_expanded_bytes: int = 100 * 1024 * 1024
        max_zip_members: int = 5000


# 命令行输出通道：独立 logger，不经根 logger，消息原样（无前缀）
_console = logging.Logger("converter", logging.INFO)
_console_handler = logging.StreamHandler()
_console.addHandler(_console_handler)


def _say(text: object = "", *, error: bool = False) -> None:
    """输出一行：结果写标准输出（助手读它），error=True 写标准错误；每次取当前的流，测试替换了也跟着走。"""
    _console_handler.setStream(sys.stderr if error else sys.stdout)
    _console.info(text)


def _cell(text: str) -> str:
    """表格单元格：转义反斜杠和竖线，不让一格撑破表格。"""
    return text.replace("\\", "\\\\").replace("|", "\\|").strip()


def convert(data: bytes, filename: str, limits: ConvertLimits) -> Conversion:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ConvertError("corrupt") from exc
    pairs, skipped = [], 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, sep, value = stripped.partition("=")
        if not sep or not key.strip():
            skipped += 1
            continue
        pairs.append((key.strip(), value.strip()))
    if not pairs:
        return Conversion(state="unsupported", reason="文件里没有「键=值」行", stats={"rows": 0, "skipped": skipped})

    kept = pairs[: limits.max_rows]
    lines = ["| 键 | 值 |", "|---|---|"] + [f"| {_cell(key)} | {_cell(value)} |" for key, value in kept]
    markdown = "\n".join(lines) + "\n"
    notes = []
    if len(markdown) > limits.max_output_chars:
        cut = markdown.rfind("\n", 0, limits.max_output_chars)
        markdown = markdown[: cut + 1] if cut > 0 else markdown[: limits.max_output_chars]
        notes.append("内容太长，只转了前一部分")
    elif len(pairs) > len(kept):
        notes.append(f"只转了前 {len(kept)} 行（共 {len(pairs)} 行）")
    if skipped:
        notes.append(f"{skipped} 行不是「键=值」，没有转")
    return Conversion(state="converted", markdown=markdown, note="；".join(notes) or None,
                      stats={"rows": len(kept), "skipped": skipped})


if __name__ == "__main__":
    source = Path(sys.argv[1])
    try:
        reply = {"ok": True, "result": convert(source.read_bytes(), source.name, ConvertLimits()).to_dict()}
    except ConvertError as exc:
        reply = {"ok": False, "error": {"code": exc.code, "message": str(exc)}}
    _say(json.dumps(reply, ensure_ascii=False, indent=2))
