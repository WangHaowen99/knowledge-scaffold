#!/usr/bin/env python3
"""Deterministic, bounded UTF-8 source extraction shared by agents and research checks.

CLI: source_extract.py FILE [--offset N] [--limit N]
Anchors: text L1; DOCX p:1 or table:1/row:1/cell:1; XLSX sheet:Sheet1!A1.
No generated summaries, formulas evaluated by us, or lossy encoding fallback.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
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
    try:
        suffix = path.suffix.lower()
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
    except (
        OSError,
        ValueError,
        KeyError,
        RuntimeError,
        ImportError,
        ET.ParseError,
        zipfile.BadZipFile,
    ) as exc:
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
    # Keep the CLI JSON protocol independent of application logging configuration.
    output = logging.Logger('source_extract.json', logging.INFO)
    output.addHandler(logging.StreamHandler(sys.stdout))
    output.info(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] == 'ok' else 2


if __name__ == '__main__':
    raise SystemExit(main())
