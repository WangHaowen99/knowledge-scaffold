"""上传资料的原文与出处锚点（契约 R-06 资料原文预览、EV-29 `file:` 锚点、ST-06 证据侧栏）。

抽取复用 core 工具箱的 `source_extract`（经契约库按项目锁定版本加载）：和助手读资料、资料分析验收
用的是同一份抽取器，锚点片段就是它给的位置——文本 `L12`，Word `p:3`、`table:1/row:2/cell:3`，
Excel `sheet:表!B3`。抽取器读不了的格式（pdf、doc 等）没有文字块，出处解析为不可读。

锚点写法：`file:<工作区相对路径>[#<片段>]`（契约 1.10，名字里的特殊字符百分号编码）；资料分析报告和条目里
不带 scheme 的 `resource/…#片段` 同样认。只解析资料区（`resource/`）里的文件，证据快照
（`resource/systems/`）与资料用途清单不算资料。
"""

from __future__ import annotations

import base64
import json
import re
import threading
from collections import OrderedDict
from pathlib import Path
from urllib.parse import unquote

from .workspace.layout import RESOURCE_DIRECTIVES

SCHEME_FILE = "file"
CONTEXT_CHARS = 300          # 前后各带一段上下文，每段最多这么多字
TARGET_CHARS = 1500
BLOCKS_DEFAULT = 200
BLOCKS_MAX = 1000
_CACHE_MAX = 32
_cache: OrderedDict = OrderedDict()
_cache_lock = threading.Lock()

# 正文里的出处：file: 锚点，或资料分析报告、条目里直接写的资料路径（可带 #片段）。
# 片段在收尾的标点、引号、括号、粗体记号前结束（`「…#p:6」`、`**…#p:6**`、`…#p:6？`）；资料名里可以有「」【】
_ANCHOR_END = "\\s|`'\"<>（）()，。；、\\]「」『』【】“”‘’？！：*,;"
ANCHOR_RE = re.compile(rf"(?:file:)?(resource/[^\s|`'\"<>（）()，。；、\]]+?\.[A-Za-z0-9]{{1,6}})(#[^{_ANCHOR_END}]+)?"
                       rf"(?=$|[{_ANCHOR_END}])")
_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_MD_LIST = re.compile(r"^\s*(?:[-*+]|\d+[.)、])\s+")


def name_positions(text: str, name: str) -> list[int]:
    """只写了文件名的引用：文件名前后都不能连着别的文件名字符（「周报告.md」里没有「报告.md」），前面也不能是路径的一部分。"""
    if not name:
        return []
    pattern = re.compile(r"(?<![\w./\\-])" + re.escape(name) + r"(?![\w])")
    return [match.start() for match in pattern.finditer(text or "")]


def is_material(rel: str) -> bool:
    rel = (rel or "").strip("/")
    if "\x00" in rel:
        return False
    return (rel.startswith("resource/") and rel != RESOURCE_DIRECTIVES and not rel.startswith("resource/systems/")
            and ".." not in rel.split("/"))


def parse_anchor(anchor: str) -> dict | None:
    """`<scheme>:<路径>[#<片段>]` → {scheme, path, fragment}；不带 scheme 的资料路径按 file 认。格式不对时 None。"""
    text = str(anchor or "").strip()
    if not text:
        return None
    match = re.match(r"^([a-z]+):(.*)$", text)
    if match and not text.startswith("resource/"):
        scheme, rest = match.group(1), match.group(2)
    elif text.startswith("resource/"):
        scheme, rest = SCHEME_FILE, text
    else:
        return None
    path, _, fragment = rest.partition("#")
    path = unquote(path).strip().lstrip("/")
    fragment = unquote(fragment).strip()
    if not path or "\x00" in path or "\x00" in fragment:
        return None
    return {"scheme": scheme, "path": path, "fragment": fragment}


def file_anchor(path: str, fragment: str = "") -> str:
    return f"{SCHEME_FILE}:{path}" + (f"#{fragment}" if fragment else "")


def find_anchors(text: str) -> list[tuple[str, str, int]]:
    """正文里的资料出处：[(资料路径, 片段, 位置)]（按出现顺序，不去重）。"""
    return [(match.group(1), (match.group(2) or "").lstrip("#"), match.start())
            for match in ANCHOR_RE.finditer(text or "")]


# ---------------------------------------------------------------------------
# 抽取
# ---------------------------------------------------------------------------


def _extractor(root: Path):
    from .packs import contracts

    return contracts.module_for(Path(root), "source_extract")


def extract(root: Path, rel: str) -> dict:
    """{status: ok|unverifiable, segments: [{anchor, text}], reason?}；按文件修订缓存。"""
    path = Path(root) / rel
    stat = path.stat()
    module = _extractor(root)
    key = (str(path.resolve()), stat.st_mtime_ns, stat.st_size, id(module))
    with _cache_lock:
        cached = _cache.get(key)
        if cached is not None:
            _cache.move_to_end(key)
            return cached
    try:
        result = module.extract(path)
    except Exception as exc:  # noqa: BLE001 - 坏文件（解析库各种异常）按不可核验，不让预览、出处、资料地图 500
        result = {"status": "unverifiable", "segments": [], "reason": f"无法核验：{type(exc).__name__}"}
    with _cache_lock:
        _cache[key] = result
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)
    return result


def location_label(fragment: str) -> str:
    """片段 → 位置的界面写法：L12 → 第 12 行；p:3 → 第 3 段；table:1/row:2/cell:3 → 表 1 · 第 2 行 · 第 3 列；sheet:表!B3 → 表 · B3。"""
    fragment = fragment or ""
    if match := re.fullmatch(r"L(\d+)", fragment):
        return f"第 {match.group(1)} 行"
    if match := re.fullmatch(r"p:(.+)", fragment):
        return f"第 {match.group(1)} 段"
    if match := re.fullmatch(r"table:(\d+)(?:/row:(\d+))?(?:/cell:(\d+))?", fragment):
        parts = [f"表 {match.group(1)}"]
        if match.group(2):
            parts.append(f"第 {match.group(2)} 行")
        if match.group(3):
            parts.append(f"第 {match.group(3)} 列")
        return " · ".join(parts)
    if match := re.fullmatch(r"sheet:([^!]+)!([A-Z]+\d+)", fragment):
        return f"{match.group(1)} · {match.group(2)}"
    return fragment


# ---------------------------------------------------------------------------
# EV-29：出处解析
# ---------------------------------------------------------------------------


def _unresolved(anchor: str, scheme: str, reason: str, source_label: str = "", location: str = "") -> dict:
    return {"anchor": anchor, "scheme": scheme, "resolved": False, "reason": reason, "source_label": source_label,
            "location_label": location, "text": "", "highlights": [], "confidence": None, "snapshot_id": None,
            "page": "resources" if scheme == SCHEME_FILE else None}


def resolve(root: Path, anchor: str) -> dict:
    """AnchorResolution（契约 7.1）：file 锚点给原文上下文（前后各一段）与高亮；其余 scheme 本期不可用。

    reason：`invalid`（格式不对、不是资料区的文件）、`file_missing`、`not_found`（片段定位不到）、
    `unreadable`（抽取器读不了这种格式）、`not_available`（业务系统证据源本期不开放）。"""
    parsed = parse_anchor(anchor)
    if parsed is None:
        return _unresolved(str(anchor or ""), SCHEME_FILE, "invalid")
    scheme = parsed["scheme"] if parsed["scheme"] in ("file", "db", "code", "bin", "ui", "api", "cli", "log") else "file"
    if parsed["scheme"] != SCHEME_FILE:
        return _unresolved(anchor, scheme, "not_available" if parsed["scheme"] == scheme else "invalid")
    rel, fragment = parsed["path"], parsed["fragment"]
    name = rel.rsplit("/", 1)[-1]
    location = location_label(fragment)
    if not is_material(rel):
        return _unresolved(anchor, SCHEME_FILE, "invalid", name, location)
    root = Path(root)
    target = (root / rel).resolve()
    if not target.is_relative_to(root.resolve()) or not target.is_file():
        return _unresolved(anchor, SCHEME_FILE, "file_missing", name, location)
    result = extract(root, rel)
    if result.get("status") != "ok":
        return _unresolved(anchor, SCHEME_FILE, "unreadable", name, location)
    segments = result.get("segments") or []
    if not fragment:
        text, highlights = _join([seg["text"] for seg in segments[:3]], None)
    else:
        index = next((i for i, seg in enumerate(segments) if seg["anchor"] == fragment), None)
        if index is None:
            return _unresolved(anchor, SCHEME_FILE, "not_found", name, location)
        before = segments[index - 1]["text"] if index > 0 else None
        after = segments[index + 1]["text"] if index + 1 < len(segments) else None
        text, highlights = _join([before, segments[index]["text"], after], 1)
    return {"anchor": anchor, "scheme": SCHEME_FILE, "resolved": True, "source_label": name,
            "location_label": location, "text": text, "highlights": highlights, "confidence": None,
            "snapshot_id": None, "page": "resources"}


def _clip(text: str, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[:limit] + "…"


def _join(parts: list, target: int | None) -> tuple[str, list[dict]]:
    """前一段、命中段、后一段拼成原文上下文；命中段整段高亮。"""
    out, highlights, pos = [], [], 0
    for index, part in enumerate(parts):
        if part is None:
            continue
        piece = _clip(part, TARGET_CHARS if index == target else CONTEXT_CHARS)
        if out:
            pos += 1  # 换行
        if index == target:
            highlights.append({"start": pos, "end": pos + len(piece)})
        out.append(piece)
        pos += len(piece)
    return "\n".join(out), highlights


def resolve_many(root: Path, anchors: list) -> list[dict]:
    out = []
    for anchor in anchors:
        try:
            out.append(resolve(root, str(anchor)))
        except (OSError, ValueError):  # 一个坏锚点不拖垮整批：这一项判为格式不对
            out.append(_unresolved(str(anchor), SCHEME_FILE, "invalid"))
    return out


# ---------------------------------------------------------------------------
# R-06：带锚点的块
# ---------------------------------------------------------------------------


def _block(segment: dict, markdown: bool) -> dict:
    anchor, text = segment["anchor"], str(segment["text"])
    if anchor.startswith(("table:", "sheet:")):
        return {"anchor": anchor, "kind": "table", "text": text}
    if markdown and (match := _MD_HEADING.match(text)):
        return {"anchor": anchor, "kind": "heading", "level": len(match.group(1)), "text": match.group(2).strip()}
    if markdown and text.lstrip().startswith("|"):
        return {"anchor": anchor, "kind": "table", "text": text}
    if _MD_LIST.match(text):
        return {"anchor": anchor, "kind": "list", "text": text}
    return {"anchor": anchor, "kind": "paragraph", "text": text}


def _encode_cursor(offset: int, rel: str) -> str:
    return base64.urlsafe_b64encode(json.dumps({"o": offset, "p": rel}, ensure_ascii=False).encode()).decode().rstrip("=")


def _decode_cursor(cursor: str, rel: str) -> int | None:
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        offset = int(data["o"])
    except (ValueError, KeyError, TypeError):
        return None
    return offset if offset >= 0 and data.get("p") == rel else None


def blocks(root: Path, rel: str, *, around: str = "", limit: int = 0, cursor: str = "") -> dict:
    """{blocks, next_cursor, readable}；around 定位到那一段前后（那一段放在这一页中间）。cursor 不对时抛 ValueError。"""
    limit = max(1, min(int(limit or BLOCKS_DEFAULT), BLOCKS_MAX))
    result = extract(root, rel)
    if result.get("status") != "ok":
        return {"blocks": [], "next_cursor": None, "readable": False}
    markdown = rel.lower().endswith((".md", ".markdown"))
    segments = result.get("segments") or []
    start = 0
    if cursor:
        offset = _decode_cursor(cursor, rel)
        if offset is None:
            raise ValueError("cursor")
        start = offset
    elif around:
        index = next((i for i, seg in enumerate(segments) if seg["anchor"] == around), None)
        if index is not None:
            start = max(0, index - limit // 2)
    end = start + limit
    return {
        "blocks": [_block(seg, markdown) for seg in segments[start:end]],
        "next_cursor": _encode_cursor(end, rel) if end < len(segments) else None,
        "readable": True,
    }
