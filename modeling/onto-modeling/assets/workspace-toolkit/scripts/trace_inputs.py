#!/usr/bin/env python3
"""Lossless testcase handoff. Parse once; compare the actual agent inputs before tracing.

CLI: trace_inputs.py SOURCE [--actual JSON] [--output JSON]
Without --output only a bounded summary is printed. No inference from expected paths.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

VERSION = 1
MAX_BYTES = 2 * 1024 * 1024


def _value(raw: str) -> tuple[object, str]:
    raw = raw.strip()
    note = ''
    if raw.startswith('`'):
        fence = re.match(r'`+', raw).group()
        end = raw.find(fence, len(fence))
        if end < 0:
            raise ValueError('输入值的反引号未闭合')
        note, raw = raw[end + len(fence):].strip(), raw[len(fence):end]
        if note and not re.fullmatch(r'（[^\n]*）|\([^\n]*\)|<!--.*-->', note):
            raise ValueError('输入值后存在未识别内容；注释放在括号内，不能静默丢弃')
    # Only JSON determines types. Plain legacy values remain exact strings.
    try:
        return json.loads(raw), note
    except ValueError:
        return raw, note


def parse_source(path: Path) -> dict:
    data = path.read_bytes()
    if len(data) > MAX_BYTES:
        raise ValueError('场景文件超限')
    text = data.decode('utf-8-sig')
    title = re.search(r'^# 测试场景[：:]\s*(.+)$', text, re.M)
    if not title:
        raise ValueError('缺少测试场景标题')
    sections = list(re.finditer(r'^##\s+(.+)$', text, re.M))
    scenarios, ids = [], set()
    for i, heading in enumerate(sections):
        match = re.fullmatch(r'场景\s*(\d+)[：:]\s*(.+)', heading[1])
        if not match:
            raise ValueError(f'无法识别场景标题：{heading[1]}')
        case_id, name = match.groups()
        if case_id in ids:
            raise ValueError(f'场景编号重复：{case_id}')
        ids.add(case_id)
        body = text[heading.end():sections[i + 1].start() if i + 1 < len(sections) else len(text)]
        # Template comments outside inputs do not form additional fields.
        block = re.search(r'^- \*\*输入\*\*[：:]\s*\n(.*?)(?=^- \*\*|\Z)', body, re.M | re.S)
        if not block or len(re.findall(r'^- \*\*输入\*\*[：:]', body, re.M)) != 1:
            raise ValueError(f'场景 {case_id} 缺少输入段')
        values, notes = {}, {}
        for line in block[1].splitlines():
            if not line.strip():
                continue
            field = re.fullmatch(r'\s+-\s+`([^`]+)`\s*=\s*(.*)', line)
            if not field:
                raise ValueError(f'场景 {case_id} 无法解析输入行：{line[:100]}')
            key, raw = field.groups()
            if key in values:
                raise ValueError(f'场景 {case_id} 输入字段重复：{key}')
            values[key], note = _value(raw)
            if note:
                notes[key] = note
        if not values:
            raise ValueError(f'场景 {case_id} 输入为空')
        if len(re.findall(r'^\s+-\s+`[^`]+`\s*=', body, re.M)) != len(values):
            raise ValueError(f'场景 {case_id} 在输入段之外还存在字段，不能静默丢弃')
        metadata = {}
        for label, key in [('预期路径', 'expected_path'), ('数据来源', 'data_source')]:
            found = re.findall(r'^- \*\*' + label + r'\*\*[：:]\s*(.+)$', body, re.M)
            if len(found) != 1:
                raise ValueError(f'场景 {case_id} 缺少或重复{label}')
            metadata[key] = found[0]
        scenarios.append({'id': case_id, 'name': name, 'inputs': values, 'input_notes': notes, **metadata})
    if not scenarios:
        raise ValueError('没有可执行场景')
    return {'version': VERSION, 'source_sha256': hashlib.sha256(data).hexdigest(),
            'operation': title[1].strip(), 'scenarios': scenarios}


def _canonical_json(value: object) -> str:
    # Canonical JSON is type-sensitive (false != 0); equality of Python dicts isn't.
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def verify(source: Path, actual: dict) -> dict:
    expected = parse_source(source)
    if _canonical_json(expected) != _canonical_json(actual):
        raise ValueError('实际交接输入与已保存确认稿不一致（修订、场景、字段、类型或值发生变化）；禁止推演')
    return expected


def verify_results(payload: dict, results: list) -> None:
    if not isinstance(results, list) or len(results) != len(payload['scenarios']):
        raise ValueError('推演结果与确认稿场景数不一致')
    seen = set()
    expected = {s['id']: s['inputs'] for s in payload['scenarios']}
    for result in results:
        if not isinstance(result, dict):
            raise ValueError('推演结果必须包含输入回执')
        case_id = result.get('scenario_id')
        if case_id not in expected or case_id in seen:
            raise ValueError('推演结果场景编号重复或不匹配')
        seen.add(case_id)
        if (result.get('source_sha256') != payload['source_sha256']
                or _canonical_json(result.get('initial_inputs')) != _canonical_json(expected[case_id])):
            raise ValueError(f'场景 {case_id} 实际初始输入不一致，不能计为通过')


def main() -> int:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--actual', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--results', type=Path)
    args = parser.parse_args()
    try:
        result = (verify(args.source, json.loads(args.actual.read_text(encoding='utf-8')))
                  if args.actual else parse_source(args.source))
        if args.results:
            verify_results(result, json.loads(args.results.read_text(encoding='utf-8')))
        if args.output:
            if args.output.resolve() == args.source.resolve():
                raise ValueError('不能覆盖场景确认稿')
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        json.dump({'status': 'passed', 'source_sha256': result['source_sha256'],
                          'scenarios': len(result['scenarios']),
                          'fields': sum(len(s['inputs']) for s in result['scenarios'])}, sys.stdout, ensure_ascii=False)
        sys.stdout.write('\n')
        return 0
    except (OSError, ValueError) as exc:
        json.dump({'status': 'failed', 'reason': str(exc)[:300]}, sys.stdout, ensure_ascii=False)
        sys.stdout.write('\n')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
