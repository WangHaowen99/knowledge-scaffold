#!/usr/bin/env python3
"""Deterministic, bounded UTF-8 source extraction shared by agents and research checks.

CLI（工作区根目录执行）：
  source_extract.py --sources          列出资料和各自要读的文本：md/txt/csv 等读原文件，PDF、Word、Excel
                                       读多源连接器转出的文本版 resource/text/<分组>/<原文件名>.md，没有文本版的标 missing
  source_extract.py FILE [--offset N] [--limit N]
                                       FILE 是文本版时按它记的原件位置给段和锚点，cite 是出处要写的原件路径（ADR-0093）；
                                       不解析原件
Anchors: text L1; DOCX p:1 or table:1/row:1/cell:1; XLSX sheet:Sheet1!A1; PDF page:1（出处写 cite#锚点）.
No generated summaries, formulas evaluated by us, or lossy encoding fallback.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path
from xml.etree import ElementTree as ET

VERSION = 2
# 与领域资料上传契约一致。该上限保护输入规模，不再把 8 MiB 当成 Office
# 正文能力边界；DOCX/XLSX 直接从文件做受控读取，避免复制整包到内存。
MAX_BYTES = 20 * 1024 * 1024
MAX_RELEVANT_EXPANDED_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_EXPANDED_BYTES = 256 * 1024 * 1024
MAX_SEGMENTS = 50000
TEXT_SUFFIXES = {'.md', '.txt', '.csv', '.json', '.yaml', '.yml'}
SUPPORTED = TEXT_SUFFIXES | {'.docx', '.xlsx'}
W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
COPY_CHUNK_BYTES = 1024 * 1024
LINE_ANCHOR = re.compile(r'L(\d+)')
LINE_RANGE = re.compile(r'L(\d+)-L?(\d+)')


@contextmanager
def _stable_snapshot(path: Path):
    """Copy one bounded source stream for both hashing and extraction."""
    digest = hashlib.sha256()
    size = 0
    with tempfile.TemporaryDirectory(prefix='source-extract-') as temp_dir:
        snapshot = Path(temp_dir) / f'source{path.suffix.lower()}'
        with path.open('rb') as source, snapshot.open('wb') as target:
            initial_stat = os.fstat(source.fileno())
            while True:
                chunk = source.read(COPY_CHUNK_BYTES)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ValueError('原文件超出抽取上限')
                digest.update(chunk)
                target.write(chunk)
            final_stat = os.fstat(source.fileno())
        source_changed = (
            initial_stat.st_size != final_stat.st_size
            or initial_stat.st_mtime_ns != final_stat.st_mtime_ns
        )
        if source_changed:
            raise ValueError('文件读取期间发生变化，请重试')
        yield snapshot, digest.hexdigest(), size


def _archive_inventory(archive: zipfile.ZipFile) -> tuple[int, list[str]]:
    total = 0
    media = []
    for info in archive.infolist():
        total += info.file_size
        if total > MAX_ARCHIVE_EXPANDED_BYTES:
            raise ValueError('Office 解压内容超出安全上限')
        if info.filename.startswith('word/media/') and not info.is_dir():
            media.append(info.filename)
    return total, media


def _word_text(node) -> str:
    pieces = []
    for element in node.iter():
        if element.tag == W + 'p' and pieces:
            pieces.append('\n')
        elif element.tag == W + 't':
            pieces.append(element.text or '')
        elif element.tag == W + 'tab':
            pieces.append('\t')
        elif element.tag in {W + 'br', W + 'cr'}:
            pieces.append('\n')
    return ''.join(pieces)


def _extract_docx(path: Path, add) -> dict:
    with zipfile.ZipFile(path) as archive:
        expanded_size, media = _archive_inventory(archive)
        info = archive.getinfo('word/document.xml')
        if info.file_size > MAX_RELEVANT_EXPANDED_BYTES:
            raise ValueError('DOCX 正文解压内容超出抽取上限')
        document = ET.fromstring(archive.read(info))
    body = document.find(f'{W}body')
    if body is None:
        raise ValueError('DOCX 缺少正文')
    paragraph, table = 0, 0
    for node in body:
        if node.tag == W + 'p':
            paragraph += 1
            add(f'p:{paragraph}', _word_text(node))
        elif node.tag == W + 'tbl':
            table += 1
            for row_number, row in enumerate(node.findall(W + 'tr'), 1):
                for cell_number, cell in enumerate(row.findall(W + 'tc'), 1):
                    add(
                        f'table:{table}/row:{row_number}/cell:{cell_number}',
                        _word_text(cell),
                    )
    return {
        'archive_expanded_size': expanded_size,
        'binary_assets': len(media),
        'binary_assets_extracted': False,
    }


def _extract_xlsx(path: Path, add) -> dict:
    import openpyxl

    with zipfile.ZipFile(path) as archive:
        expanded_size, _ = _archive_inventory(archive)
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=False)
    try:
        for sheet in workbook:
            cells = (sheet.max_row or 0) * (sheet.max_column or 0)
            if cells > MAX_SEGMENTS:
                raise ValueError('工作表范围超限')
            for row in sheet.iter_rows():
                for cell in row:
                    if cell.value is not None:
                        add(f'sheet:{sheet.title}!{cell.coordinate}', cell.value)
    finally:
        workbook.close()
    return {'archive_expanded_size': expanded_size}


def extract(path: Path) -> dict:
    result = {'status': 'unverifiable', 'extractor_version': VERSION,
              'source_sha256': '', 'segments': []}
    suffix = path.suffix.lower()
    material = _material_of(path) if suffix in FROM_TEXT_VERSION else None
    if material is not None:
        return _extract_from_text_version(path, *material)
    try:
        if suffix not in SUPPORTED:
            raise ValueError('不支持的来源格式')
        with _stable_snapshot(path) as (snapshot, source_sha256, source_size):
            result['source_sha256'] = source_sha256
            result['source_size'] = source_size
            segments = []

            def add(anchor, text):
                if len(segments) >= MAX_SEGMENTS:
                    raise ValueError('来源位置数超限；不能以部分抽取充当完整来源')
                if str(text).strip():
                    segments.append({'anchor': anchor, 'text': str(text)})

            if suffix in TEXT_SUFFIXES:
                data = snapshot.read_bytes()
                text = data.decode('utf-8-sig')
                if '\x00' in text or '\ufffd' in text:
                    raise ValueError('来源文本包含异常编码字符')
                for n, line in enumerate(text.splitlines(), 1):
                    add(f'L{n}', line)
            elif suffix == '.docx':
                result.update(_extract_docx(snapshot, add))
            else:
                result.update(_extract_xlsx(snapshot, add))
            if not segments:
                raise ValueError('未抽取到正文，无法核验来源')
            result.update(status='ok', segments=segments)
    except Exception as exc:  # 坏文件：解析库会抛各种异常（如 lxml 的 XMLSyntaxError），一律按无法核验
        result['reason'] = f'无法核验：{type(exc).__name__}：{str(exc)[:160]}'
    return result


# ---------------------------------------------------------------------------
# 资料区里的原件读网关转出的文本版（ADR-0076、ADR-0093）：文本版文末的锚点注释记着每一行在原件里的位置，
# 这里按它还原出和直接读原件同样的段与锚点（Word p:3、table:1/row:2/cell:3，Excel sheet:表!B3），PDF 按页
# 标记给 page:3。不解析原件，只读原件的字节算哈希、核对文本版是不是这一版原件转的。
# 锚点注释的写法见后端 app/evidence/convert.py（anchor_index），两边同口径。
# ---------------------------------------------------------------------------

# .datasource：数据源资料（ADR-0103），出处读采集出的文本版，锚点写法同 Excel
FROM_TEXT_VERSION = {'.docx', '.xlsx', '.xlsm', '.pdf', '.datasource'}
TEXT_HEADER_PREFIX = '<!-- onto:text '
ANCHORS_HEAD = '<!-- onto:anchors 1'
PAGE_MARKER = re.compile(r'^<!-- 第 (\d+) 页 -->$')
HEADER_SHA = re.compile(r'sha256="([0-9a-f]{16,64})"')


def _material_of(path: Path) -> tuple[Path, str] | None:
    """资料区里的原件 → (工作区根, 工作区相对路径)；不在资料区（或是文本版本身）返回 None。"""
    parts = Path(os.path.abspath(path)).parts
    for i in range(len(parts) - 2, 0, -1):
        if parts[i] == 'resource':
            rel = '/'.join(parts[i:])
            return (None if rel.startswith(f'{TEXT_DIR}/') else (Path(*parts[:i]), rel))
    return None


def _unescape_anchor(text: str) -> str:
    return text.replace('%0D', '\r').replace('%0A', '\n').replace('%3E', '>').replace('%25', '%')


def anchor_index(lines: list[str]) -> tuple[dict[int, str], dict[str, list[str]], int]:
    """文本版的行 → ({文件行号: 锚点}, {工作表: [合并区]}, 锚点注释开始的行号)；没有锚点注释时第三项是 0。"""
    start = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].strip() == ANCHORS_HEAD), None)
    if start is None:
        return {}, {}, 0
    # 内核写文件头 + 空行，正文从第三行起；文件头后面没有空行的也认
    offset = (2 if len(lines) > 1 and not lines[1].strip() else 1) if lines and lines[0].startswith(TEXT_HEADER_PREFIX) else 0
    found, merges = {}, {}
    for raw in lines[start + 1:]:
        line = raw.strip()
        if line == '-->':
            break
        number, _, anchor = line.partition(' ')
        if number == 'merge':
            sheet, _, refs = anchor.partition(' ')
            merges.setdefault(_unescape_anchor(sheet.replace('%20', ' ')), []).extend(refs.split())
        elif number.isdigit() and anchor:
            found[int(number) + offset] = _unescape_anchor(anchor)
    return found, merges, start + 1


PARAGRAPH_MARK = re.compile(r'(p:\d+) (\d+)')


def md_cells(line: str) -> list[str]:
    """一行 Markdown 表格 → 单元格（转换器 md_cell 的逆：反斜杠转义、<br> 是格内换行）。"""
    body = line.strip()
    body = body[1:] if body.startswith('|') else body
    cells, current, escaped = [], [], False
    for char in body:
        if escaped:
            current.append(char)
            escaped = False
        elif char == '\\':
            escaped = True
        elif char == '|':
            cells.append(''.join(current))
            current = []
        else:
            current.append(char)
    if ''.join(current).strip():
        cells.append(''.join(current))
    return [cell.strip().replace('<br>', '\n') for cell in cells]


def column_letter(number: int) -> str:
    label = ''
    while number > 0:
        number, rest = divmod(number - 1, 26)
        label = chr(65 + rest) + label
    return label


def text_version_segments(text: str, suffix: str, add) -> dict:
    """文本版全文 → 原件锚点的段（经 add 收集）；返回附带信息（合并区）。Word、Excel 没有锚点注释时 ValueError。"""
    lines = text.splitlines()
    index, merges, stop = anchor_index(lines)
    if suffix != '.pdf' and not index:
        raise ValueError('文本版还没有原件位置（转换后会补上），暂时核对不了')
    body = lines[:stop - 1] if stop else lines
    page = sheet = None
    paragraph = None   # 正在收的段落：(锚点, [行])，软回车展开的续行接在后面
    for number, line in enumerate(body, 1):
        if number == 1 and line.startswith(TEXT_HEADER_PREFIX):
            continue
        anchor = index.get(number, '')
        if paragraph and (anchor or not line.strip()):
            add(paragraph[0], '\n'.join(paragraph[1]))
            paragraph = None
        if suffix == '.pdf':
            marker = PAGE_MARKER.match(line.strip())
            if marker:
                page = marker.group(1)
            elif page and line.strip():
                add(f'page:{page}', line)
        elif anchor.startswith('p:'):
            # 段落原文：去掉转换器加的标题、列表记号（锚点注释里「p:3 2」的 2 是记号长度）
            mark = PARAGRAPH_MARK.fullmatch(anchor)
            paragraph = (mark.group(1), [line[int(mark.group(2)):]]) if mark else (anchor, [line])
        elif anchor.startswith('table:'):
            for column, value in enumerate(md_cells(line), 1):
                add(f'{anchor}/cell:{column}', value)
        elif anchor.startswith('sheet:'):
            sheet = anchor[len('sheet:'):]
        elif anchor.startswith('row:') and sheet is not None:
            for column, value in enumerate(md_cells(line), 1):
                add(f'sheet:{sheet}!{column_letter(column)}{anchor[len("row:"):]}', value)
        elif paragraph and line.strip():
            paragraph[1].append(line)
    if paragraph:
        add(paragraph[0], '\n'.join(paragraph[1]))
    return {'merges': merges} if merges else {}


def _extract_from_text_version(path: Path, root: Path, rel: str) -> dict:
    """资料区里的 Word、Excel、PDF：读它的文本版，给出原件的锚点；不解析原件。"""
    suffix = path.suffix.lower()
    text_rel = text_version(rel)
    result = {'status': 'unverifiable', 'extractor_version': VERSION, 'source_sha256': '', 'segments': [],
              'text_version': text_rel}
    try:
        with _stable_snapshot(path) as (_snapshot, source_sha256, source_size):
            result['source_sha256'] = source_sha256
            result['source_size'] = source_size
        try:
            text = (root / text_rel).read_text(encoding='utf-8')
        except FileNotFoundError:
            raise ValueError('这份资料还没有文本版（转换中、暂不支持或转换失败）') from None
        head = HEADER_SHA.search(text.split('\n', 1)[0])
        if head and not source_sha256.startswith(head.group(1)[:16]):
            raise ValueError('文本版不是这一版原件转的（资料改过，等重新转换）')
        segments = []

        def add(anchor, value):
            if len(segments) >= MAX_SEGMENTS:
                raise ValueError('来源位置数超限；不能以部分抽取充当完整来源')
            if str(value).strip():
                segments.append({'anchor': anchor, 'text': str(value)})

        result.update(text_version_segments(text, suffix, add))
        if not segments:
            raise ValueError('未抽取到正文，无法核验来源')
        result.update(status='ok', segments=segments)
    except Exception as exc:  # 读不了一律按无法核验
        result['reason'] = f'无法核验：{type(exc).__name__}：{str(exc)[:160]}'
    return result


def _line_in(anchor: str, low: int, high: int) -> bool:
    """单行锚点 Ln 的行号落在 [low, high] 里。"""
    found = LINE_ANCHOR.fullmatch(anchor)
    return bool(found) and low <= int(found[1]) <= high


def located_text(result: dict, anchor: str = '') -> str:
    if result['status'] != 'ok':
        raise ValueError(result.get('reason', '无法核验来源'))
    segments = result['segments']
    if not anchor:
        return '\n'.join(s['text'] for s in segments)
    lines = LINE_RANGE.fullmatch(anchor)
    if lines:
        # 行号范围 L40-L46：范围里有正文的各行（空行不出段）
        low, high = int(lines[1]), int(lines[2])
        matches = [s['text'] for s in segments if _line_in(s['anchor'], low, high)]
    else:
        matches = [s['text'] for s in segments if s['anchor'] == anchor]
    if not matches:
        raise ValueError(f'来源锚点不存在：{anchor}')
    return '\n'.join(matches)


TEXT_DIR = 'resource/text'
SKIP = {'resource/materials.json'}
# 旧版系统证据采集（已去掉）留在工作区的快照记录是机读数据，不是资料：列成「没有文本版」会让助手以为它们还没转换
SYSTEMS_DIR = 'resource/systems'


def text_version(rel: str) -> str:
    """资料的文本版路径（多源连接器转出，ADR-0073）：resource/others/合同.pdf → resource/text/others/合同.pdf.md"""
    return f"{TEXT_DIR}/{rel.removeprefix('resource/')}.md"


def original_of(rel: str) -> str | None:
    """文本版 → 它的原件（只认带原件位置的 Word、Excel、PDF）：resource/text/others/合同.docx.md → resource/others/合同.docx"""
    rel = rel.removeprefix('./')
    if not (rel.startswith(f'{TEXT_DIR}/') and rel.endswith('.md')):
        return None
    original = 'resource/' + rel[len(TEXT_DIR) + 1:-3]
    return original if Path(original).suffix.lower() in FROM_TEXT_VERSION else None


def _listed(path: Path, rel: str, resource: Path) -> bool:
    """资料区里要列进清单的文件：不是跳过的、文本版目录里的或隐藏的。"""
    if not path.is_file() or rel in SKIP or rel.startswith(f'{TEXT_DIR}/'):
        return False
    return not any(part.startswith('.') for part in path.relative_to(resource).parts)


def sources(root: Path) -> list[dict]:
    """资料清单：每份资料要读哪个文本。kind：native 原文件就是文本；converted 读文本版；missing 还没有文本版
    （转换中、暂不支持或转换失败，资料区能看到原因），资料分析把它记为未读，不去解析原件。"""
    items = []
    resource = root / 'resource'
    if not resource.is_dir():
        return items
    for path in sorted(resource.rglob('*')):
        rel = path.relative_to(root).as_posix()
        if not _listed(path, rel, resource):
            continue
        if rel.startswith(f'{SYSTEMS_DIR}/') and '/records/' in rel:
            continue
        if path.suffix.lower() in TEXT_SUFFIXES:
            items.append({'material': rel, 'read': rel, 'kind': 'native'})
        elif (root / text_version(rel)).is_file():
            items.append({'material': rel, 'read': text_version(rel), 'kind': 'converted'})
        else:
            items.append({'material': rel, 'read': None, 'kind': 'missing'})
    return items


def _print_json(data: dict) -> None:
    # Keep the CLI JSON protocol independent of application logging configuration.
    output = logging.Logger('source_extract.json', logging.INFO)
    output.addHandler(logging.StreamHandler(sys.stdout))
    output.info(json.dumps(data, ensure_ascii=False))


def main() -> int:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('file', type=Path, nargs='?')
    parser.add_argument('--sources', action='store_true')
    parser.add_argument('--offset', type=int, default=0)
    parser.add_argument('--limit', type=int, default=20)
    parser.add_argument('--anchor', default='')
    parser.add_argument('--text-offset', type=int, default=0)
    args = parser.parse_args()
    if args.sources:
        _print_json({'materials': sources(Path('.'))})
        return 0
    if args.file is None:
        parser.error('要给资料路径，或用 --sources 列出资料')
    rel = args.file.as_posix().removeprefix('./')
    target, cite = args.file, rel
    original = original_of(rel)
    if original is not None:
        # 读的是文本版：给出原件的锚点，出处写原件路径（ADR-0093）
        target, cite = Path(original), original
    elif args.file.suffix.lower() not in TEXT_SUFFIXES | FROM_TEXT_VERSION:
        _print_json({'status': 'no_text_version',
                     'reason': '这份资料没有文本版（暂不支持这种格式），按未读记下，不要自己解析'})
        return 2
    result = extract(target)
    result['cite'] = cite
    segments = result.pop('segments')
    if args.anchor:
        segments = [s for s in segments if s['anchor'] == args.anchor]
        if not segments and result['status'] == 'ok':
            result.update(status='unverifiable', reason='来源锚点不存在')
    offset, limit = max(0, args.offset), max(1, min(args.limit, 50))
    page = segments[offset:offset + limit]
    # Bound individual segments too, explicitly marking incomplete display (not evidence).
    start = max(0, args.text_offset)
    result.update(total=len(segments), offset=offset,
                  segments=[{**s, 'text': s['text'][start:start + 1000], 'text_offset': start,
                             'truncated': start > 0 or len(s['text']) > start + 1000,
                             'next_text_offset': start + 1000 if len(s['text']) > start + 1000 else None} for s in page],
                  next_offset=offset + limit if offset + limit < len(segments) else None)
    _print_json(result)
    return 0 if result['status'] == 'ok' else 2


if __name__ == '__main__':
    raise SystemExit(main())
