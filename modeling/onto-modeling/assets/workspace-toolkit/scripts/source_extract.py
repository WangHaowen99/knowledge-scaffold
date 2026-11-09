#!/usr/bin/env python3
"""Deterministic, bounded UTF-8 source extraction shared by agents and research checks.

CLI（工作区根目录执行）：
  source_extract.py --sources          列出资料和各自要读的文本：md/txt/csv 等读原文件，PDF、Word、Excel
                                       读多源连接器转出的文本版 resource/text/<分组>/<原文件名>.md，没有文本版的标 missing
  source_extract.py FILE [--offset N] [--limit N]
                                       只读文本：FILE 是 PDF、Word 这类原件时不解析，回 use_text_version（附文本版路径）
                                       或 no_text_version，退出码 2（资料分析只读文本，F4b）
Anchors: text L1; DOCX p:1 or table:1/row:1/cell:1; XLSX sheet:Sheet1!A1（后两种只给旧报告核验用，extract() 照常支持）.
No generated summaries, formulas evaluated by us, or lossy encoding fallback.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

VERSION = 1
MAX_BYTES = 8 * 1024 * 1024
MAX_EXPANDED_BYTES = 32 * 1024 * 1024
MAX_SEGMENTS = 50000
TEXT_SUFFIXES = {'.md', '.txt', '.csv', '.json', '.yaml', '.yml'}
SUPPORTED = TEXT_SUFFIXES | {'.docx', '.xlsx'}
W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'


def extract(path: Path) -> dict:
    result = {'status': 'unverifiable', 'extractor_version': VERSION,
              'source_sha256': '', 'segments': []}
    try:
        if path.stat().st_size > MAX_BYTES:
            raise ValueError('原文件超出抽取上限')
        data = path.read_bytes()
        result['source_sha256'] = hashlib.sha256(data).hexdigest()
        suffix = path.suffix.lower()
        if suffix not in SUPPORTED:
            raise ValueError('不支持的来源格式')
        segments = []

        def add(anchor, text):
            if len(segments) >= MAX_SEGMENTS:
                raise ValueError('来源位置数超限；不能以部分抽取充当完整来源')
            if str(text).strip():
                segments.append({'anchor': anchor, 'text': str(text)})

        if suffix in TEXT_SUFFIXES:
            text = data.decode('utf-8-sig')
            if '\x00' in text or '\ufffd' in text:
                raise ValueError('来源文本包含异常编码字符')
            for n, line in enumerate(text.splitlines(), 1):
                add(f'L{n}', line)
        else:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                if sum(i.file_size for i in archive.infolist()) > MAX_EXPANDED_BYTES:
                    raise ValueError('Office 解压内容超限')
                if suffix == '.docx':
                    document = ET.fromstring(archive.read('word/document.xml'))
                    body = document.find(f'{W}body')
                    if body is None:
                        raise ValueError('DOCX 缺少正文')
                    paragraph, table = 0, 0
                    def word_text(node):
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
                    for node in body:
                        if node.tag == W + 'p':
                            paragraph += 1
                            add(f'p:{paragraph}', word_text(node))
                        elif node.tag == W + 'tbl':
                            table += 1
                            for r, row in enumerate(node.findall(W + 'tr'), 1):
                                for c, cell in enumerate(row.findall(W + 'tc'), 1):
                                    add(f'table:{table}/row:{r}/cell:{c}', word_text(cell))
                else:
                    import openpyxl
                    workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=False)
                    try:
                        for sheet in workbook:
                            if sheet.max_row and sheet.max_column and sheet.max_row * sheet.max_column > MAX_SEGMENTS:
                                raise ValueError('工作表范围超限')
                            for row in sheet.iter_rows():
                                for cell in row:
                                    if cell.value is not None:
                                        add(f'sheet:{sheet.title}!{cell.coordinate}', cell.value)
                    finally:
                        workbook.close()
        if not segments:
            raise ValueError('未抽取到正文，无法核验来源')
        result.update(status='ok', segments=segments)
    except Exception as exc:  # 坏文件：解析库会抛各种异常（如 lxml 的 XMLSyntaxError），一律按无法核验
        result['reason'] = f'无法核验：{type(exc).__name__}：{str(exc)[:160]}'
    return result


def located_text(result: dict, anchor: str = '') -> str:
    if result['status'] != 'ok':
        raise ValueError(result.get('reason', '无法核验来源'))
    segments = result['segments']
    if not anchor:
        return '\n'.join(s['text'] for s in segments)
    matches = [s['text'] for s in segments if s['anchor'] == anchor]
    if not matches:
        raise ValueError(f'来源锚点不存在：{anchor}')
    return '\n'.join(matches)


TEXT_DIR = 'resource/text'
SKIP = {'resource/materials.json'}
# 系统快照（ADR-0082）的标准记录是给 evidence.py 检索的机读数据，不是资料：列成「没有文本版」会让助手以为它们还没转换
SYSTEMS_DIR = 'resource/systems'


def text_version(rel: str) -> str:
    """资料的文本版路径（多源连接器转出，ADR-0073）：resource/others/合同.pdf → resource/text/others/合同.pdf.md"""
    return f"{TEXT_DIR}/{rel.removeprefix('resource/')}.md"


def sources(root: Path) -> list[dict]:
    """资料清单：每份资料要读哪个文本。kind：native 原文件就是文本；converted 读文本版；missing 还没有文本版
    （转换中、暂不支持或转换失败，资料区能看到原因），资料分析把它记为未读，不去解析原件。"""
    items = []
    resource = root / 'resource'
    if not resource.is_dir():
        return items
    for path in sorted(resource.rglob('*')):
        rel = path.relative_to(root).as_posix()
        if not path.is_file() or rel in SKIP or rel.startswith(f'{TEXT_DIR}/') \
                or any(part.startswith('.') for part in path.relative_to(resource).parts):
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
    if args.file.suffix.lower() not in TEXT_SUFFIXES:
        # 资料分析只读文本（F4b）：原件交给多源连接器转换，这里不解析
        rel = args.file.as_posix().removeprefix('./')
        if Path(text_version(rel)).is_file():
            _print_json({'status': 'use_text_version', 'text': text_version(rel)})
        else:
            _print_json({'status': 'no_text_version',
                         'reason': '这份资料还没有文本版（转换中、暂不支持或转换失败），按未读记下，不要自己解析'})
        return 2
    result = extract(args.file)
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
