#!/usr/bin/env python3
"""Deterministic, bounded UTF-8 source extraction shared by agents and research checks.

CLI: source_extract.py FILE [--offset N] [--limit N]
Anchors: text L1; DOCX p:1 or table:1/row:1/cell:1; XLSX sheet:Sheet1!A1.
No generated summaries, formulas evaluated by us, or lossy encoding fallback.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
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
    except (OSError, ValueError, KeyError, ImportError, ET.ParseError, zipfile.BadZipFile) as exc:
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


def main() -> int:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('file', type=Path)
    parser.add_argument('--offset', type=int, default=0)
    parser.add_argument('--limit', type=int, default=20)
    parser.add_argument('--anchor', default='')
    parser.add_argument('--text-offset', type=int, default=0)
    args = parser.parse_args()
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
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] == 'ok' else 2


if __name__ == '__main__':
    raise SystemExit(main())
