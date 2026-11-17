#!/usr/bin/env python3
"""语义检查边查边记：子任务每查完一项就把问题追加进 JSONL、把这一项标做完，主命令最后合并成 JSON 数组。

不让子任务一次返回整份 JSON 的原因：模型会在一次思考里把全部检查项过一遍、再起草整份 JSON，输出撞上限被截断，
已经查出的问题跟着丢掉。逐项落盘以后，一次思考只覆盖一项；子任务中途出错或超时，记下的问题和做完的项都还在，
主命令只补派缺的项。

检查项清单只在这里写一份，ID 是 `.claude/agents/semantic-check-agent.md` 各节检查项标题里的中文名（测试核对两边一致）。

目录 `.ontology-state/semantic/<运行号>/`（不进版本）：
    run.json        运行模式：full / incremental / change（只查改动）/ regression（修复后回归）
    <部分>.jsonl    这一部分记下的问题，一行一条
    <部分>.done     做完的检查项，一行一个；`*` 表示整份做完（PART=all）

子命令：
    init [--mode M]                     建运行目录，打印路径
    add --dir D --part P [--check C]    从 stdin 读一条 JSON、JSON 数组或 JSON Lines，校验后追加
    done --dir D --part P [--check C]   标一项做完（没有问题也要标）；PART=all 不带 --check，标整份做完
    status --dir D --parts a,b,c        每部分做完几项、缺哪几项、记了几条
    merge --dir D [--parts a,b]         输出合并后的 JSON 数组（按 file → 置信度 → 严重程度排，同 file+message 去重）
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import secrets
import shutil
import sys
import time
from pathlib import Path

STATE_DIR = Path('.ontology-state') / 'semantic'
RUN_FILE = 'run.json'
KEEP_RUNS = 20
_RUN_NAME = re.compile(r'\d{8}-\d{6}-[0-9a-f]{4}')

CHECKS = {
    'operation': (
        '逻辑步骤完整性', '输入参数使用', '输出可推导性', '子流程调用合理性', '前提与逻辑一致性', '对象关联分类',
        '流程关联完整性', '兜底完整性', '空值语义区分', 'Data/Operation 内容边界与口径一致', '写作风格',
    ),
    'object': ('描述歧义', '关联合理性', '业务合理性', '关联对象数据支撑'),
    'cross': ('对象生命周期', '字段引用', '语义口径一致性', '派生属性声明完整性', '规则冲突检测'),
}
WHOLE_PART = 'all'
PARTS = (*CHECKS, WHOLE_PART)
PART_LABELS = {'operation': '操作类', 'object': '对象类', 'cross': '跨文档', WHOLE_PART: '整份'}
WHOLE_MARK = '*'

SEVERITIES = ('错误', '警告', '建议')
CONFIDENCES = ('high', 'medium', 'low')
CATEGORIES = (
    '逻辑缺陷', '参数未使用', '输出不可达', '子流程不匹配', '前提不一致', '对象关联不合理', '流程关联不完整',
    '描述歧义', '关联不合理', '业务不合理', '描述不充分', '生命周期缺陷', '字段不一致', '兜底缺失', '空值语义模糊',
    '关系无数据支撑', '关系应转Operation', '规则冲突', '风格不规范', '语义口径不一致', '派生属性未声明', '业务知识冲突',
)
ORIGINS = ('change', 'existing')
MODES = ('full', 'incremental', 'change', 'regression')
REGRESSION_LIMIT = 3
# 记录里只给本脚本用的字段，合并时去掉
_INTERNAL_KEYS = ('part', 'check')


class UsageError(Exception):
    """输入或参数不对：原因给调用方看，退出码 2。"""


def _emit(text: str) -> None:
    output = logging.Logger('semantic_issues', logging.INFO)
    output.addHandler(logging.StreamHandler(sys.stdout))
    output.info(text)


# ---- 运行目录 ----


def init(mode: str) -> str:
    base = Path.cwd() / STATE_DIR
    base.mkdir(parents=True, exist_ok=True)
    _prune(base)
    for _ in range(20):
        name = f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"
        try:
            (base / name).mkdir()
        except FileExistsError:
            continue
        (base / name / RUN_FILE).write_text(json.dumps({'mode': mode}, ensure_ascii=False), encoding='utf-8')
        return f'{STATE_DIR.as_posix()}/{name}'
    raise UsageError('建运行目录失败：重名太多，稍后再试')


def _prune(base: Path) -> None:
    """只留最近的若干次运行，旧的删掉（问题早已写进报告）。"""
    runs = sorted(path for path in base.iterdir() if path.is_dir() and _RUN_NAME.fullmatch(path.name))
    for path in runs[:max(0, len(runs) - KEEP_RUNS + 1)]:
        shutil.rmtree(path, ignore_errors=True)


def _run_dir(raw: str) -> Path:
    path = Path(raw)
    if not (path / RUN_FILE).is_file():
        raise UsageError(f'问题目录不存在或不是 init 建的：{raw}；先运行 init，用它打印的路径')
    return path


def _mode(run_dir: Path) -> str:
    try:
        mode = json.loads((run_dir / RUN_FILE).read_text(encoding='utf-8')).get('mode')
    except (OSError, ValueError, AttributeError):
        return 'full'
    return mode if mode in MODES else 'full'


def _part(raw: str) -> str:
    if raw not in PARTS:
        raise UsageError(f'PART 取值「{raw}」不对，应为 {" / ".join(PARTS)}')
    return raw


# ---- 检查项 ----


def _key(name: str) -> str:
    """检查项名比对时不计空白、引号、前面的编号。"""
    name = re.sub(r'^\s*\d+\s*[.、．)]?\s*', '', str(name))
    return re.sub(r'[\s「」"\'“”]', '', name)


def _check(part: str, raw: str | None) -> str | None:
    if raw is None or not raw.strip():
        return None
    names = CHECKS[part] if part in CHECKS else tuple(name for items in CHECKS.values() for name in items)
    for name in names:
        if _key(name) == _key(raw):
            return name
    raise UsageError(f'检查项「{raw}」不在{PART_LABELS[part]}检查里，应为其中之一：{"、".join(names)}')


def _done_items(run_dir: Path, part: str) -> list[str]:
    path = run_dir / f'{part}.done'
    if not path.is_file():
        return []
    return [line.strip() for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


def _missing(run_dir: Path, part: str) -> list[str]:
    done = set(_done_items(run_dir, part))
    if part == WHOLE_PART:
        return [] if WHOLE_MARK in done else [PART_LABELS[part]]
    return [name for name in CHECKS[part] if name not in done]


def mark_done(run_dir: Path, part: str, raw_check: str | None) -> str:
    check = _check(part, raw_check)
    if part != WHOLE_PART and check is None:
        left = '、'.join(_missing(run_dir, part)) or '无'
        raise UsageError(f'PART={part} 要带 --check <检查项>；还没做完的：{left}')
    item = check or WHOLE_MARK
    if item not in _done_items(run_dir, part):
        with (run_dir / f'{part}.done').open('a', encoding='utf-8') as handle:
            handle.write(item + '\n')
    if part == WHOLE_PART:
        return f'已标做完：{check}' if check else '已标完成：整份检查做完了'
    left = _missing(run_dir, part)
    if not left:
        return f'已标做完「{check}」；{PART_LABELS[part]}检查 {len(CHECKS[part])} 项都做完了'
    return f'已标做完「{check}」；{PART_LABELS[part]}检查还剩 {len(left)} 项：{"、".join(left)}'


# ---- 问题 ----


def _parse(text: str) -> list:
    text = text.strip()
    fence = re.fullmatch(r'```[A-Za-z]*\n(.*?)\n?```', text, re.S)
    if fence:
        text = fence.group(1).strip()
    if not text:
        raise UsageError("stdin 没有内容：用 <<'EOF' … EOF 传问题")
    try:
        data = json.loads(text)
    except ValueError as whole:
        if text.startswith('['):
            raise UsageError(f'不是合法的 JSON 数组：第 {whole.lineno} 行第 {whole.colno} 列 {whole.msg}') from whole
        try:
            return [item for line in text.splitlines() if line.strip() for item in _as_list(json.loads(line))]
        except ValueError:
            raise UsageError(
                f'不是合法的 JSON（一条对象、数组或每行一条）：第 {whole.lineno} 行第 {whole.colno} 列 {whole.msg}'
            ) from whole
    return _as_list(data)


def _as_list(value) -> list:
    return value if isinstance(value, list) else [value]


def _normalize_file(value: str) -> str:
    """统一成 ontology/ 下的相对路径：去掉 `ontology/` 前缀、`./`、绝对路径前段；多个文件用逗号分隔。"""
    pieces = []
    for piece in re.split(r'[,，]', value):
        piece = piece.strip().replace('\\', '/')
        while piece.startswith('./'):
            piece = piece[2:]
        if piece.startswith('ontology/'):
            piece = piece[len('ontology/'):]
        elif '/ontology/' in piece:
            piece = piece.split('/ontology/', 1)[1]
        if piece:
            pieces.append(piece)
    return ', '.join(pieces)


def _shown(value) -> str:
    return '（缺）' if value is None else f'「{value}」'


def _problems(issue, mode: str) -> list[str]:
    if not isinstance(issue, dict):
        return ['不是 JSON 对象']
    problems = []
    if issue.get('severity') not in SEVERITIES:
        problems.append(f'severity 取值{_shown(issue.get("severity"))}不对，应为 {" / ".join(SEVERITIES)}')
    if issue.get('category') not in CATEGORIES:
        problems.append(f'category 取值{_shown(issue.get("category"))}不对，应为其中之一：{"、".join(CATEGORIES)}')
    if issue.get('confidence') not in CONFIDENCES:
        problems.append(f'confidence 取值{_shown(issue.get("confidence"))}不对，应为 {" / ".join(CONFIDENCES)}')
    for field in ('file', 'message'):
        if not isinstance(issue.get(field), str) or not issue[field].strip():
            problems.append(f'{field} 不能为空')
    return problems + _mode_problems(issue, mode)


def _mode_problems(issue: dict, mode: str) -> list[str]:
    origin = issue.get('origin')
    if origin is not None and origin not in ORIGINS:
        return [f'origin 取值{_shown(origin)}不对，应为 change / existing']
    if mode == 'change':
        if origin is None:
            return ['只查改动模式 origin 必填：change（改动引起）或 existing（原有问题）']
        if origin == 'existing' and issue.get('severity') != '错误':
            return ['原有问题（origin=existing）只在 severity 为「错误」时上报']
    if mode == 'regression':
        if origin == 'existing':
            return ['回归模式只报本轮修复引起的问题，origin 应为 change']
        if issue.get('confidence') == 'low':
            return ['回归模式只报 confidence 为 high 或 medium 的问题']
    return []


def _read_issues(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    issues = []
    for line in path.read_text(encoding='utf-8').splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            issues.append(item)
    return issues


def _identity(issue: dict) -> tuple[str, str]:
    return str(issue.get('file') or '').strip(), str(issue.get('message') or '').strip()


def add(run_dir: Path, part: str, raw_check: str | None, text: str) -> str:
    check = _check(part, raw_check)
    mode = _mode(run_dir)
    items = _parse(text)
    errors = [f'第 {index} 条：{problem}' for index, item in enumerate(items, 1) for problem in _problems(item, mode)]
    if errors:
        raise UsageError('\n'.join([f'没有记录：本批 {len(items)} 条里有不合格的，改正后整批重发。', *errors]))
    path = run_dir / f'{part}.jsonl'
    existing = _read_issues(path)
    seen = {_identity(item) for item in existing}
    fresh = []
    for item in items:
        record = {**item, 'file': _normalize_file(item['file']), 'message': item['message'].strip()}
        if mode == 'regression':
            record['origin'] = 'change'
        if _identity(record) in seen:
            continue
        seen.add(_identity(record))
        fresh.append({**record, 'part': part, **({'check': check} if check else {})})
    if mode == 'regression' and len(existing) + len(fresh) > REGRESSION_LIMIT:
        raise UsageError(f'没有记录：回归模式最多记 {REGRESSION_LIMIT} 条，已记 {len(existing)} 条；只留最要紧的')
    with path.open('a', encoding='utf-8') as handle:
        for record in fresh:
            handle.write(json.dumps(record, ensure_ascii=False) + '\n')
    repeated = len(items) - len(fresh)
    note = f'，{repeated} 条与已记的重复没再记' if repeated else ''
    return f'已记录 {len(fresh)} 条{note}（本部分共 {len(existing) + len(fresh)} 条）'


# ---- 汇总 ----


def _parts_arg(raw: str | None) -> list[str]:
    names = [item.strip() for item in re.split(r'[,，、\s]+', raw or '') if item.strip()]
    return [_part(name) for name in names]


def status(run_dir: Path, parts: list[str]) -> str:
    if not parts:
        raise UsageError('--parts 不能为空，如 --parts operation,object,cross')
    lines, pending = [], []
    for part in parts:
        count = len(_read_issues(run_dir / f'{part}.jsonl'))
        missing = _missing(run_dir, part)
        if part == WHOLE_PART:
            state = '已标完成' if not missing else '未标完成'
        else:
            total = len(CHECKS[part])
            state = f'做完 {total - len(missing)}/{total}' + (f'，缺 {"、".join(missing)}' if missing else '')
        lines.append(f'{PART_LABELS[part]}（{part}）：{state}；记 {count} 条')
        if missing:
            pending.append(part)
    lines.append('全部做完' if not pending else f'有缺项：{"、".join(pending)}')
    return '\n'.join(lines)


_CONFIDENCE_RANK = {name: rank for rank, name in enumerate(CONFIDENCES)}
_SEVERITY_RANK = {name: rank for rank, name in enumerate(SEVERITIES)}


def merge(run_dir: Path, parts: list[str]) -> str:
    files = [run_dir / f'{part}.jsonl' for part in parts] if parts else sorted(run_dir.glob('*.jsonl'))
    issues = [
        {key: value for key, value in item.items() if key not in _INTERNAL_KEYS}
        for path in files for item in _read_issues(path)
    ]
    issues.sort(key=lambda item: (str(item.get('file') or ''), _CONFIDENCE_RANK.get(item.get('confidence'), 1),
                                  _SEVERITY_RANK.get(item.get('severity'), 1)))
    merged, seen = [], set()
    for item in issues:
        if _identity(item) not in seen:
            seen.add(_identity(item))
            merged.append(item)
    if not merged:
        return '[]'
    return '[\n' + ',\n'.join(json.dumps(item, ensure_ascii=False) for item in merged) + '\n]'


# ---- 命令行 ----


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='语义检查问题的逐项记录、完成标记与合并')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('init', help='建运行目录，打印路径').add_argument('--mode', choices=MODES, default='full')
    for name in ('add', 'done'):
        sub = commands.add_parser(name)
        sub.add_argument('--dir', required=True)
        sub.add_argument('--part', required=True)
        sub.add_argument('--check', default=None)
    status_parser = commands.add_parser('status')
    status_parser.add_argument('--dir', required=True)
    status_parser.add_argument('--parts', required=True)
    merge_parser = commands.add_parser('merge')
    merge_parser.add_argument('--dir', required=True)
    merge_parser.add_argument('--parts', default=None)
    return parser


def run(args: argparse.Namespace) -> str:
    if args.command == 'init':
        return init(args.mode)
    run_dir = _run_dir(args.dir)
    if args.command == 'add':
        return add(run_dir, _part(args.part), args.check, sys.stdin.read())
    if args.command == 'done':
        return mark_done(run_dir, _part(args.part), args.check)
    if args.command == 'status':
        return status(run_dir, _parts_arg(args.parts))
    return merge(run_dir, _parts_arg(args.parts))


def main(argv: list | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        _emit(run(args))
    except UsageError as error:
        _emit(str(error))
        return 2
    except OSError as error:
        _emit(f'读写问题目录失败：{error}')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
