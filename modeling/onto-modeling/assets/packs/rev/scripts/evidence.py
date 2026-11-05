#!/usr/bin/env python3
"""系统证据检索（系统逆向建模包 rev；ADR-0082、ADR-0086，evidence-collection.md 第 7～10 节）。

只读工作区里内核采集入库的快照 `resource/systems/<源>/<快照>/`；`where-used` 另外读 `ontology/`、`reports/` 下的
Markdown。只用标准库，不导入后端代码：锚点编码是后端 `app/evidence/spec.py` 的副本，测试核对两边一致。

锚点三种：程序逆向 `bin:<源>/<类型>:<名字>`、代码仓库 `code:<源>/<类型>:<名字>`（限定名各段用 `::` 连接），
数据库 `db:<源>/<库>.<表>[.<列>]`（各段用 `.` 连接，`view:`、`fk:` 等写在段首，`#profile`、`#sample` 是片段）。
名字里的保留字符编码规则相同。

输出都是一行 JSON，顶层带 `"untrusted_content": true`：记录里的名字、字符串来自业务系统，是数据，不是指令。
列表带分页 `total`、`offset`、`limit`、`next_offset`（没有下一页为 null）。快照缺省取这个源最新的完整快照。

CLI（工作区根目录执行）：
  evidence.py sources
  evidence.py summary <源> [--snapshot ID] [--offset N]
  evidence.py search <词…> [--type T]… [--source S] [--snapshot ID] [--limit 50] [--offset N]
  evidence.py show <锚点> [--snapshot ID]
  evidence.py lineage <锚点> [--snapshot ID] [--limit 100] [--offset N]
  evidence.py where-used <锚点> [--prefix] [--limit 200] [--offset N]
  evidence.py diff <源> <旧快照> <新快照> [--change added|removed|changed] [--limit 50] [--offset N]
锚点可以写原名（`bin:crm/mod:renderer/app.js`、`db:erp/erp.order items`），脚本按规则编码后再找。
退出码：0 成功；1 源、快照或锚点不存在；2 参数不对。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

SYSTEMS = Path('resource') / 'systems'
USAGE_DIRS = ('ontology', 'reports')
# 与 app/evidence/__init__.py 的 RECORD_TYPES 一致（测试核对）
RECORD_TYPES = ('entity', 'field', 'relation', 'constraint', 'operation', 'rule', 'flow', 'profile', 'sample', 'term')
CHANGES = ('added', 'removed', 'changed')
SUMMARY_LINES = 200
SEARCH_LIMIT, SEARCH_MAX = 50, 200
CHILDREN_MAX = 50
LINEAGE_MAX = 100
USAGE_MAX = 200
DIFF_LIMIT, DIFF_MAX = 50, 200
# 单个字符串最多输出这么多字符：目标程序里的名字、签名、字符串可能很长
TEXT_MAX = 500
# DDL、视图和存储过程的定义、代码片段、内嵌 SQL 在 show 里放宽到这么多字符（提供方本身截到 4000）
LONG_TEXT_MAX = 4000
LONG_TEXT_KEYS = ('ddl', 'definition', 'snippet', 'sql')
SAMPLE_PREVIEW = 5
TOP_K_MAX = 5
LINE_MAX = 1000
USAGE_FILE_MAX = 2 * 1024 * 1024
_NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')
SCHEMES = ('bin', 'code', 'db')
_PREFIX_RE = re.compile(r'^(?:bin|code|db):[A-Za-z0-9][A-Za-z0-9._-]{0,127}/')
# 数据库锚点段首的路径类型（evidence-collection.md §9）和片段
DB_KINDS = ('view', 'proc', 'fk', 'ref', 'uk', 'check', 'trigger')
DB_FRAGMENTS = ('profile', 'sample')
# 锚点在正文里结束的地方：bin:、code: 锚点里的 `.`、`/`、`|`、`[`、`]`、空格都编码过，碰到它们就是锚点外面的字；
# db: 锚点用 `.` 分段，后面紧跟字母、数字、`%` 的 `.` 还在锚点里，句末的 `.` 不在
_ANCHOR_TAIL = r'[^\s`\'"()\[\]<>|,;.*，。；、）】]*'
_DB_TAIL = r'(?:[^\s`\'"()\[\]<>|,;.*，。；、）】]|\.(?=[\w%]))*'
_ANCHOR_END = r'(?![\w%:~\-#])'
_DB_END = r'(?![\w%:~\-#]|\.[\w%])'

# ---- 锚点编码：app/evidence/spec.py 的副本 ----

ANCHOR_ESCAPE = frozenset('%/.#@|[] ')
SEPARATOR = '::'


def canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def record_hash(record: dict) -> str:
    body = canonical_json({key: value for key, value in record.items() if key != 'hash'})
    return hashlib.sha256(body.encode('utf-8')).hexdigest()


def encode_segment(text: str) -> str:
    return ''.join(f'%{ord(char):02X}' if char in ANCHOR_ESCAPE else char for char in str(text))


def qualified(*segments: str) -> str:
    return SEPARATOR.join(encode_segment(segment) for segment in segments)


def bin_anchor(source_id: str, kind: str, *segments: str) -> str:
    return f'bin:{source_id}/{kind}:{qualified(*segments)}'


# ---- 公共 ----


class EvidenceError(Exception):
    def __init__(self, code: str, message: str, **extra):
        super().__init__(message)
        self.code = code
        self.extra = extra


def parse_anchor(anchor: str) -> tuple[str, str, str]:
    """`<scheme>:<源>/<路径>` → (scheme, 源, 路径)。bin:、code: 的路径是 `<类型>:<名字>`，db: 的是 `<库>.<表>…`。"""
    text = str(anchor or '')
    scheme, colon, rest = text.partition(':')
    source, slash, path = rest.partition('/')
    if scheme not in SCHEMES or not colon or not slash or not _NAME_RE.match(source) or not path:
        raise EvidenceError('bad_anchor', f'不是 bin:、code:、db: 锚点：{_clip(text)}')
    if scheme != 'db':
        kind, sep, name = path.partition(':')
        if not sep or not kind or not name:
            raise EvidenceError('bad_anchor', f'{scheme}: 锚点要写成 {scheme}:<源>/<类型>:<名字>：{_clip(text)}')
    elif '.' not in path.partition('#')[0]:
        raise EvidenceError('bad_anchor', f'db: 锚点要写成 db:<源>/<库>.<表>：{_clip(text)}')
    return scheme, source, path


def _split_fragment(path: str, fragments) -> tuple[str, str]:
    """路径末尾认识的片段（`#profile`、`#L12-L30`）单独拿出来，不参与编码。"""
    head, sep, tail = path.rpartition('#')
    if sep and fragments(tail):
        return head, sep + tail
    return path, ''


def canonical_anchor(anchor: str) -> str:
    """原名写的锚点按规则编码（bin:、code: 各段用 `::` 分开，db: 各段用 `.` 分开）；
    已经编码过的（名字里没有要编码的字符）原样返回。"""
    scheme, source, path = parse_anchor(anchor)
    raw = ANCHOR_ESCAPE - {'%'}
    if scheme == 'db':
        body, fragment = _split_fragment(path, lambda tail: tail in DB_FRAGMENTS)
        segments = body.split('.')
        if not any(char in raw - {'.'} for segment in segments for char in segment):
            return anchor
        out = []
        for segment in segments:
            kind, sep, name = segment.partition(':')
            out.append(f'{kind}:{encode_segment(name)}' if sep and kind in DB_KINDS else encode_segment(segment))
        return f'db:{source}/{".".join(out)}{fragment}'
    kind, _sep, name = path.partition(':')
    name, fragment = _split_fragment(name, lambda tail: kind == 'file' and re.fullmatch(r'L\d+(?:-L\d+)?', tail))
    if not any(char in raw for char in name):
        return anchor
    return f'{bin_anchor(source, kind, *name.split(SEPARATOR)).replace("bin:", f"{scheme}:", 1)}{fragment}'


def _clip(value, limit: int = TEXT_MAX):
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + '…'
    if isinstance(value, list):
        return [_clip(item, limit) for item in value[:CHILDREN_MAX]]
    if isinstance(value, dict):
        return {str(key)[:100]: _clip(item, limit) for key, item in list(value.items())[:CHILDREN_MAX]}
    return value


def _safe_name(value: str, what: str) -> str:
    if not _NAME_RE.match(str(value or '')) or '..' in value:
        raise EvidenceError('bad_argument', f'{what}不合法：{_clip(str(value), 100)}')
    return value


def _limit(value: int | None, default: int, maximum: int) -> int:
    return max(1, min(value or default, maximum))


def _page(items: list, offset: int, limit: int) -> dict:
    offset = max(0, offset or 0)
    end = offset + limit
    return {'total': len(items), 'offset': offset, 'limit': limit,
            'next_offset': end if end < len(items) else None, 'items': items[offset:end]}


def _source_dirs(root: Path) -> list[Path]:
    base = root / SYSTEMS
    if not base.is_dir():
        return []
    return sorted(path for path in base.iterdir()
                  if path.is_dir() and not path.is_symlink() and _NAME_RE.match(path.name))


def _manifest(snapshot_dir: Path) -> dict | None:
    path = snapshot_dir / 'manifest.json'
    if path.is_symlink():
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _snapshots(source_dir: Path) -> list[tuple[str, dict]]:
    """[(快照 ID, 清单)]，新的在前：快照 ID 以 UTC 时间开头，按名字倒序就是按时间倒序。没有清单的目录不算。"""
    out = []
    for path in sorted(source_dir.iterdir(), key=lambda item: item.name, reverse=True):
        if path.is_dir() and not path.is_symlink() and _NAME_RE.match(path.name):
            manifest = _manifest(path)
            if manifest is not None:
                out.append((path.name, manifest))
    return out


def _complete(manifest: dict) -> bool:
    return manifest.get('complete') is True


def _pick(root: Path, source: str, snapshot: str | None = None) -> tuple[str, Path, dict]:
    """(快照 ID, 目录, 清单)：给了 --snapshot 用它，否则用最新的完整快照。"""
    source_dir = root / SYSTEMS / _safe_name(source, '源')
    if not source_dir.is_dir() or source_dir.is_symlink():
        raise EvidenceError('source_not_found', f'没有这个源的快照：{source}',
                            sources=[path.name for path in _source_dirs(root)])
    snapshots = _snapshots(source_dir)
    if snapshot:
        _safe_name(snapshot, '快照')
        for snapshot_id, manifest in snapshots:
            if snapshot_id == snapshot:
                return snapshot_id, source_dir / snapshot_id, manifest
        raise EvidenceError('snapshot_not_found', f'{source} 没有快照 {snapshot}',
                            snapshots=[item for item, _ in snapshots])
    for snapshot_id, manifest in snapshots:
        if _complete(manifest):
            return snapshot_id, source_dir / snapshot_id, manifest
    raise EvidenceError('no_complete_snapshot', f'{source} 还没有完整的快照，要用不完整的快照就加 --snapshot',
                        snapshots=[item for item, _ in snapshots])


def _records(snapshot_dir: Path, types=None):
    """快照里的记录：按类型顺序、文件里的顺序；读不了的行跳过（入库时校验过）。"""
    for record_type in RECORD_TYPES:
        if types and record_type not in types:
            continue
        path = snapshot_dir / 'records' / f'{record_type}.jsonl'
        if not path.is_file() or path.is_symlink():
            continue
        with path.open(encoding='utf-8', errors='replace') as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict) and isinstance(record.get('id'), str):
                    yield record


def _attrs(record: dict) -> dict:
    attrs = record.get('attrs')
    return attrs if isinstance(attrs, dict) else {}


def _brief(record: dict, **extra) -> dict:
    """列表里的一条记录：锚点、类型、名字、上级、位置；数据库的列、代码的字段多类型，被遮蔽的列标 masked。"""
    attrs = _attrs(record)
    out = {'anchor': record['id'], 'type': record.get('type'), 'kind': _clip(attrs.get('kind'), 40),
           'name': _clip(record.get('name')), 'label': _clip(record.get('label')),
           'parent': record.get('parent'), 'loc': _clip(attrs.get('loc'))}
    data_type = record.get('data_type') or attrs.get('data_type')
    if data_type:
        out['data_type'] = _clip(data_type, 80)
    if attrs.get('masked'):
        out['masked'] = True
    return {**out, **extra}


def _full(record: dict) -> dict:
    """整条记录（字符串截断，锚点不截；DDL、定义、代码片段、SQL 放宽）。"""
    out = _clip(record)
    for key in ('id', 'parent', 'from', 'to'):
        if key in record:
            out[key] = record[key]
    attrs = _attrs(record)
    for key in LONG_TEXT_KEYS:
        if isinstance(attrs.get(key), str) and isinstance(out.get('attrs'), dict):
            out['attrs'][key] = _clip(attrs[key], LONG_TEXT_MAX)
    return out


def _sample_summary(record: dict) -> dict:
    """表的脱敏样本：列、被遮蔽的列、行数和前几行（取值已脱敏，被遮蔽的列只看结构）。"""
    attrs = _attrs(record)
    rows = attrs.get('rows') if isinstance(attrs.get('rows'), list) else []
    return {'anchor': record['id'], 'columns': _clip(attrs.get('columns') or []),
            'masked_columns': _clip(attrs.get('masked_columns') or []), 'rows': len(rows),
            'preview': [_clip(row, 100) for row in rows[:SAMPLE_PREVIEW]]}


def _profile_summary(record: dict) -> dict:
    """表的画像：每列的空值率、不同取值数，低基数列的 top-k、数值列的最小最大值；百分比是整数千分比。"""
    attrs = _attrs(record)
    columns = [item for item in attrs.get('columns') or [] if isinstance(item, dict)]
    out = []
    for item in columns[:CHILDREN_MAX]:
        entry = {key: _clip(item.get(key), 100) for key in ('column', 'null_pct', 'distinct', 'stats') if key in item}
        if isinstance(item.get('top_k'), list):
            entry['top_k'] = _clip(item['top_k'][:TOP_K_MAX], 100)
        out.append(entry)
    return {'anchor': record['id'], 'rows': attrs.get('rows'), 'percent_unit': 'permille',
            'columns_total': len(columns), 'columns': out}


def _strings(value, out: list, depth: int = 0) -> None:
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict) and depth < 3:
        for item in value.values():
            _strings(item, out, depth + 1)
    elif isinstance(value, list) and depth < 3:
        for item in value[:CHILDREN_MAX]:
            _strings(item, out, depth + 1)


# ---- 子命令 ----


def cmd_sources(root: Path, args) -> dict:
    items = []
    for source_dir in _source_dirs(root):
        snapshots = _snapshots(source_dir)
        latest = snapshots[0][1] if snapshots else {}
        items.append({
            'source': source_dir.name,
            'provider': _clip(latest.get('provider') or (latest.get('source') or {}).get('provider'), 80),
            'latest_complete': next((item for item, manifest in snapshots if _complete(manifest)), None),
            'snapshots': [{'id': snapshot_id, 'complete': _complete(manifest), 'ended_at': _clip(manifest.get('ended_at'), 40),
                           'detected': _clip((manifest.get('artifact') or {}).get('detected'), 40),
                           'records': _clip(manifest.get('records') or {})}
                          for snapshot_id, manifest in snapshots],
        })
    return _page(items, 0, max(1, len(items)))


def cmd_summary(root: Path, args) -> dict:
    snapshot_id, snapshot_dir, manifest = _pick(root, args.source, args.snapshot)
    path = snapshot_dir / 'summary.md'
    if not path.is_file() or path.is_symlink():
        raise EvidenceError('summary_not_found', f'{args.source} 的快照 {snapshot_id} 没有 summary.md')
    lines = [_clip(line, LINE_MAX) for line in path.read_text(encoding='utf-8', errors='replace').splitlines()]
    page = _page(lines, args.offset, SUMMARY_LINES)
    text = '\n'.join(page.pop('items'))
    return {'source': args.source, 'snapshot': snapshot_id, 'complete': _complete(manifest),
            'path': path.as_posix(), 'text': text, **page}


def _search_targets(root: Path, source: str | None, snapshot: str | None) -> tuple[list, list]:
    if source:
        snapshot_id, snapshot_dir, _ = _pick(root, source, snapshot)
        return [(source, snapshot_id, snapshot_dir)], []
    targets, skipped = [], []
    for source_dir in _source_dirs(root):
        try:
            snapshot_id, snapshot_dir, _ = _pick(root, source_dir.name, snapshot)
        except EvidenceError as exc:
            skipped.append({'source': source_dir.name, 'reason': exc.code})
            continue
        targets.append((source_dir.name, snapshot_id, snapshot_dir))
    return targets, skipped


def cmd_search(root: Path, args) -> dict:
    terms = [term.casefold() for term in ' '.join(args.words).split()]
    if not terms:
        raise EvidenceError('bad_argument', '要给检索词')
    query = ' '.join(terms)
    targets, skipped = _search_targets(root, args.source, args.snapshot)
    found = []
    for source, snapshot_id, snapshot_dir in targets:
        for record in _records(snapshot_dir, set(args.type or ())):
            name = str(record.get('name') or '').casefold()
            texts = [record['id'], str(record.get('name') or ''), str(record.get('label') or '')]
            _strings(_attrs(record), texts)
            haystack = '\n'.join(texts).casefold()
            if not all(term in haystack for term in terms):
                continue
            # 名字正好是检索词的排最前，名字里有检索词的其次
            rank = 0 if name == query else 1 if any(term in name for term in terms) else 2
            found.append((rank, len(found), _brief(record, source=source, snapshot=snapshot_id)))
    found.sort(key=lambda item: (item[0], item[1]))
    page = _page([item for _, _, item in found], args.offset, _limit(args.limit, SEARCH_LIMIT, SEARCH_MAX))
    return {'query': _clip(query, 200), 'skipped': skipped, **page}


def _snapshot_of_anchor(root: Path, anchor: str, snapshot: str | None) -> tuple[str, str, Path, list[str]]:
    """(源, 快照 ID, 目录, 候选锚点)：候选是原样的和按规则编码过的（相同时只有一个）。"""
    _scheme, source, _path = parse_anchor(anchor)
    snapshot_id, snapshot_dir, _ = _pick(root, source, snapshot)
    return source, snapshot_id, snapshot_dir, list(dict.fromkeys([anchor, canonical_anchor(anchor)]))


def cmd_show(root: Path, args) -> dict:
    source, snapshot_id, snapshot_dir, candidates = _snapshot_of_anchor(root, args.anchor, args.snapshot)
    # 数据库表的样本、画像是单独的记录：`<表锚点>#sample`、`<表锚点>#profile`
    extras = {f'{anchor}#{fragment}': (anchor, fragment) for anchor in candidates if anchor.startswith('db:')
              for fragment in DB_FRAGMENTS}
    records: dict[str, dict] = {}
    children: dict[str, dict[str, list]] = {anchor: {} for anchor in candidates}
    attached: dict[tuple[str, str], dict] = {}
    for record in _records(snapshot_dir):
        if record['id'] in children and record['id'] not in records:
            records[record['id']] = record
        if record['id'] in extras:
            attached.setdefault(extras[record['id']], record)
        parent = record.get('parent')
        if parent in children:
            children[parent].setdefault(str(record.get('type')), []).append(record)
    anchor = next((item for item in candidates if item in records), None)
    if anchor is None:
        raise EvidenceError('anchor_not_found', f'快照 {snapshot_id} 里没有这个锚点', anchor=args.anchor)
    grouped = {kind: {'total': len(items), 'items': [_brief(item) for item in items[:CHILDREN_MAX]]}
               for kind, items in children[anchor].items() if kind not in DB_FRAGMENTS}
    out = {'source': source, 'snapshot': snapshot_id, 'anchor': anchor, 'record': _full(records[anchor]),
           'children': grouped}
    if (anchor, 'sample') in attached:
        out['sample'] = _sample_summary(attached[(anchor, 'sample')])
    if (anchor, 'profile') in attached:
        out['profile'] = _profile_summary(attached[(anchor, 'profile')])
    return {**out, 'next_offset': None}


def cmd_lineage(root: Path, args) -> dict:
    source, snapshot_id, snapshot_dir, candidates = _snapshot_of_anchor(root, args.anchor, args.snapshot)
    names: dict[str, tuple] = {}
    relations = []
    for record in _records(snapshot_dir):
        names[record['id']] = (record.get('type'), record.get('name'))
        if record.get('type') == 'relation' and (record.get('from') in candidates or record.get('to') in candidates):
            relations.append(record)
    anchor = next((item for item in candidates if item in names), None) or next(
        (item for item in candidates if any(item in (rel.get('from'), rel.get('to')) for rel in relations)), None)
    if anchor is None:
        raise EvidenceError('anchor_not_found', f'快照 {snapshot_id} 里没有这个锚点', anchor=args.anchor)
    items = []
    for relation in relations:
        outgoing = relation.get('from') == anchor
        if not outgoing and relation.get('to') != anchor:
            continue
        other = relation.get('to') if outgoing else relation.get('from')
        other_type, other_name = names.get(other, (None, None)) if other else (None, None)
        attrs = _attrs(relation)
        item = {'relation': relation['id'], 'name': _clip(relation.get('name'), 40),
                'direction': 'out' if outgoing else 'in', 'other': other, 'other_type': other_type,
                'other_name': _clip(other_name), 'url': _clip(attrs.get('url')),
                'confidence': (relation.get('evidence') or {}).get('confidence')}
        # 数据库的外键带列对，推断关系带依据（命名、取值重合）
        for key in ('columns', 'basis'):
            if attrs.get(key):
                item[key] = _clip(attrs[key], 200)
        items.append(item)
    page = _page(items, args.offset, _limit(args.limit, LINEAGE_MAX, LINEAGE_MAX))
    return {'source': source, 'snapshot': snapshot_id, 'anchor': anchor, **page}


def _usage_files(root: Path):
    for base in USAGE_DIRS:
        directory = root / base
        if not directory.is_dir() or directory.is_symlink():
            continue
        for path in sorted(directory.rglob('*.md')):
            rel = path.relative_to(root)
            if any(part.startswith('.') for part in rel.parts) or path.is_symlink() or not path.is_file():
                continue
            if path.stat().st_size <= USAGE_FILE_MAX:
                yield rel, path


def cmd_where_used(root: Path, args) -> dict:
    db = str(args.anchor).startswith('db:')
    if args.prefix:
        if not _PREFIX_RE.match(str(args.anchor)):
            raise EvidenceError('bad_anchor', '前缀要写成 bin:<源>/、code:<源>/ 或 db:<源>/ 开头')
        anchor = args.anchor
        pattern = re.compile(re.escape(anchor) + (_DB_TAIL if db else _ANCHOR_TAIL))
    else:
        anchor = canonical_anchor(args.anchor)
        pattern = re.compile(re.escape(anchor) + (_DB_END if db else _ANCHOR_END))
    items = []
    for rel, path in _usage_files(root):
        for number, line in enumerate(path.read_text(encoding='utf-8', errors='replace').splitlines(), 1):
            for match in pattern.finditer(line):
                found = match.group(0)
                # db: 前缀以 `.` 结尾时，句末的 `.` 会被前缀吃进去：去掉它后短于前缀的不算
                if db and args.prefix and not found.rstrip('.').startswith(anchor):
                    continue
                items.append({'file': rel.as_posix(), 'line': number, 'anchor': found.rstrip('.') if db else found,
                              'text': _clip(line.strip(), 300)})
    page = _page(items, args.offset, _limit(args.limit, USAGE_MAX, USAGE_MAX))
    return {'anchor': anchor, 'prefix': bool(args.prefix), **page}


def cmd_diff(root: Path, args) -> dict:
    old_id, old_dir, _ = _pick(root, args.source, args.old)
    new_id, new_dir, _ = _pick(root, args.source, args.new)

    def index(directory: Path) -> dict:
        return {record['id']: (record.get('hash') or record_hash(record), record.get('type'), record.get('name'))
                for record in _records(directory)}

    old, new = index(old_dir), index(new_dir)
    changes = []
    for anchor in sorted(set(old) | set(new)):
        if anchor not in old:
            change = 'added'
        elif anchor not in new:
            change = 'removed'
        elif old[anchor][0] != new[anchor][0]:
            change = 'changed'
        else:
            continue
        _hash, record_type, name = new.get(anchor) or old[anchor]
        changes.append({'change': change, 'anchor': anchor, 'type': record_type, 'name': _clip(name)})
    counts = {change: sum(1 for item in changes if item['change'] == change) for change in CHANGES}
    if args.change:
        changes = [item for item in changes if item['change'] == args.change]
    changes.sort(key=lambda item: (CHANGES.index(item['change']), item['anchor']))
    page = _page(changes, args.offset, _limit(args.limit, DIFF_LIMIT, DIFF_MAX))
    return {'source': args.source, 'old': old_id, 'new': new_id, 'counts': counts, **page}


HANDLERS = {'sources': cmd_sources, 'summary': cmd_summary, 'search': cmd_search, 'show': cmd_show,
            'lineage': cmd_lineage, 'where-used': cmd_where_used, 'diff': cmd_diff}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('sources', help='各源的快照')
    summary = sub.add_parser('summary', help='快照摘要')
    summary.add_argument('source')
    summary.add_argument('--snapshot')
    summary.add_argument('--offset', type=int, default=0)
    search = sub.add_parser('search', help='按名字、锚点、属性检索记录')
    search.add_argument('words', nargs='+')
    search.add_argument('--type', action='append', choices=RECORD_TYPES)
    search.add_argument('--source')
    search.add_argument('--snapshot')
    search.add_argument('--limit', type=int, default=SEARCH_LIMIT)
    search.add_argument('--offset', type=int, default=0)
    show = sub.add_parser('show', help='一条记录和它的下级')
    show.add_argument('anchor')
    show.add_argument('--snapshot')
    lineage = sub.add_parser('lineage', help='关系里的邻居')
    lineage.add_argument('anchor')
    lineage.add_argument('--snapshot')
    lineage.add_argument('--limit', type=int, default=LINEAGE_MAX)
    lineage.add_argument('--offset', type=int, default=0)
    usage = sub.add_parser('where-used', help='本体与报告里引用锚点的地方')
    usage.add_argument('anchor')
    usage.add_argument('--prefix', action='store_true')
    usage.add_argument('--limit', type=int, default=USAGE_MAX)
    usage.add_argument('--offset', type=int, default=0)
    diff = sub.add_parser('diff', help='两份快照按锚点和哈希比较')
    diff.add_argument('source')
    diff.add_argument('old')
    diff.add_argument('new')
    diff.add_argument('--change', choices=CHANGES)
    diff.add_argument('--limit', type=int, default=DIFF_LIMIT)
    diff.add_argument('--offset', type=int, default=0)
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    args = _parser().parse_args(argv)
    try:
        result, code = HANDLERS[args.command](Path('.'), args), 0
    except EvidenceError as exc:
        result = {'error': exc.code, 'message': str(exc), **exc.extra}
        code = 2 if exc.code in ('bad_argument', 'bad_anchor') else 1
    sys.stdout.write(json.dumps({'untrusted_content': True, 'command': args.command, **result},
                                ensure_ascii=False) + '\n')
    return code


if __name__ == '__main__':
    raise SystemExit(main())
