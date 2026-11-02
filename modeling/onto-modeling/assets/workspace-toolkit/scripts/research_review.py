#!/usr/bin/env python3
"""资料分析报告的验收回执：读写与失效判定。

单一事实源。工作区 CLI（state_helper / research_checker / pre_commit_check）与
后端项目进度（WorkspaceManager）都从这里取「报告是否已通过内容验收」，
避免"目录里有文件就算完成"在前端、后端、CLI 三处各自实现。

回执落在 `.ontology-state/research-review.json`（已 gitignore，属派生状态）：
    status          passed / failed —— 检查器给出的确定性结论
    checker_version 检查器版本，升级后旧回执自动失效
    inputs          {相对路径: 内容指纹} —— 本次验收读过的全部输入
    errors          确定性错误项（status=failed 时非空）
    manual_pending  需人工确认的条目

对外状态由 `review_status()` 给出，共四种：
    none     还没有资料分析报告
    pending  有报告但没有有效回执（无回执 / 回执失效 / 有人工待决项）
    failed   回执判定存在确定性错误
    passed   回执有效且通过

报告或任一输入被改动后指纹不再匹配，回执立即失效为 pending。失效方向始终是
"退回待核验"，不会把未知状态当作已验收。

ID 基线落在 `.ontology-state/research-baseline.json`：只在报告**完整验收通过**
（无确定性错误、无人工待决）时写入，核验失败不会清掉它。下一轮派遣前的快照从这里
取既有规范 ID——不能直接读当前报告，否则未通过报告里编造的 ID 会被当成已验收的
基线，下一轮就能凭基线放行。
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

STATE_DIR = Path('.ontology-state')
REVIEW_FILE = STATE_DIR / 'research-review.json'
INVENTORY_FILE = STATE_DIR / 'research-inventory.json'
BASELINE_FILE = STATE_DIR / 'research-baseline.json'
RESEARCH_DIR = Path('research')

# 检查项有增减时 +1：旧回执随即失效，退回待核验而不是继续沿用旧结论。
# 2：业务项清单新增「分类」列、候选段拆成四段（ADR-0012）。
# 3：原始文档抽取、锚点核验与新增 ID 人工确认；旧基线不再免检。
CHECKER_VERSION = 3
UNVERIFIABLE = 'unverifiable'

NONE = 'none'
PENDING = 'pending'
FAILED = 'failed'
PASSED = 'passed'

# pending 的具体原因，供前端与 CLI 给出可操作提示。
REASON_NO_REPORT = 'no_report'
REASON_NO_RECEIPT = 'no_receipt'
REASON_CHECKER_UPGRADED = 'checker_upgraded'
REASON_REPORTS_CHANGED = 'reports_changed'
REASON_INPUTS_CHANGED = 'inputs_changed'
REASON_MANUAL_PENDING = 'manual_pending'
REASON_OK = 'ok'


def content_revision(data: bytes) -> str:
    """内容指纹。与后端 workspace._content_revision 同口径（sha256 前 16 位）。"""
    return hashlib.sha256(data).hexdigest()[:16]


# 大文件流式计算指纹；不能以大小代替修订，否则等长修改不会失效。
MAX_HASH_BYTES = 8 * 1024 * 1024


def file_revision(path: Path) -> str:
    """文件指纹；不存在或读不到时为空串（视为与任何已记录指纹不符）。符号链接、设备文件也当读不到。"""
    if path.is_symlink() or not path.is_file():
        return ''
    try:
        size = path.stat().st_size
        if size > MAX_HASH_BYTES:
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(chunk)
            return digest.hexdigest()[:16]
        return content_revision(path.read_bytes())
    except OSError:
        return ''


def report_paths(root: Path) -> list[str]:
    """工作区内的资料分析报告相对路径（排序去重），目录索引 index.md 不算报告。

    相对 `research/` 拼路径而不是 relative_to(root)：root 可能是 `Path('.')`
    （工作区 CLI 的用法），那时 relative_to 会抛 ValueError。
    """
    directory = root / RESEARCH_DIR
    # 符号链接（目录本身或报告文件）不算报告：链接可以指到工作区外
    if not directory.is_dir() or directory.is_symlink():
        return []
    prefix = RESEARCH_DIR.as_posix()
    return sorted(
        f'{prefix}/{path.relative_to(directory).as_posix()}'
        for path in directory.rglob('*.md')
        if path.is_file() and not path.is_symlink() and path.name.lower() != 'index.md'
    )


def _relative(rel) -> bool:
    parts = Path(str(rel)).parts
    return bool(parts) and not Path(str(rel)).is_absolute() and '..' not in parts


def file_revisions(root: Path, rels) -> dict:
    """{相对路径: 内容指纹}；读不到的输入不记录（下次核验必然判失效）。"""
    revisions = {}
    for rel in sorted(set(rels)):
        revision = file_revision(root / rel)
        if revision:
            revisions[rel] = revision
    return revisions


def load_review(root: Path) -> dict | None:
    path = root / REVIEW_FILE
    if path.is_symlink():
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def write_review(root: Path, status: str, inputs: dict, errors: list, manual_pending: list, evidence=None) -> dict:
    data = {
        'status': status,
        'checker_version': CHECKER_VERSION,
        'checked_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'inputs': inputs,
        'errors': errors,
        'manual_pending': manual_pending,
        'source_evidence': evidence or {},
    }
    path = root / REVIEW_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    return data


def load_inventory(root: Path) -> dict:
    """派遣前固化的只读快照（真实表头 + 既有规范清单）。缺失时为空。"""
    try:
        data = json.loads((root / INVENTORY_FILE).read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_inventory(root: Path, data: dict) -> dict:
    path = root / INVENTORY_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    return data


def load_baseline(root: Path) -> dict:
    """上一次完整验收通过时的规范 ID。从未通过过时为空。"""
    try:
        data = json.loads((root / BASELINE_FILE).read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_baseline(root: Path, data: dict) -> dict:
    path = root / BASELINE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    return data


def _result(status: str, reason: str, reports: list[str], review: dict | None = None) -> dict:
    review = review or {}
    return {
        'status': status,
        'reason': reason,
        'reports': len(reports),
        'errors': review.get('errors') or [],
        'manual_pending': review.get('manual_pending') or [],
        'checked_at': review.get('checked_at', ''),
    }


def review_status(root: Path) -> dict:
    """报告的验收状态。回执与当前输入不符时一律退回 pending。"""
    reports = report_paths(root)
    if not reports:
        return _result(NONE, REASON_NO_REPORT, reports)

    review = load_review(root)
    if review is None:
        return _result(PENDING, REASON_NO_RECEIPT, reports)
    if review.get('checker_version') != CHECKER_VERSION:
        return _result(PENDING, REASON_CHECKER_UPGRADED, reports, review)

    inputs = review.get('inputs')
    if not isinstance(inputs, dict) or not inputs:
        return _result(PENDING, REASON_NO_RECEIPT, reports, review)
    # 报告增删（含改名）即失效：回执只对它当时读过的那批报告有效。
    if sorted(rel for rel in inputs if rel.startswith(f'{RESEARCH_DIR.as_posix()}/')) != reports:
        return _result(PENDING, REASON_REPORTS_CHANGED, reports, review)
    # 报告正文、源表格、规范清单任一改动都让旧结论失效。
    # 回执里的路径只认工作区内的相对路径（绝对路径、.. 一律算对不上）
    if any(not _relative(rel) or file_revision(root / rel) != revision for rel, revision in inputs.items()):
        return _result(PENDING, REASON_INPUTS_CHANGED, reports, review)

    if review.get('status') in {FAILED, UNVERIFIABLE}:
        return _result(review['status'], REASON_OK, reports, review)
    if review.get('status') != PASSED:
        return _result(PENDING, REASON_NO_RECEIPT, reports, review)
    if review.get('manual_pending'):
        return _result(PENDING, REASON_MANUAL_PENDING, reports, review)
    return _result(PASSED, REASON_OK, reports, review)


def accepted(root: Path) -> bool:
    """阶段放行的唯一依据：报告存在且回执有效通过。"""
    return review_status(root)['status'] == PASSED


HINTS = {
    REASON_NO_RECEIPT: '报告尚未经过内容验收，运行 python3 .claude/scripts/research_checker.py 核验',
    REASON_CHECKER_UPGRADED: '检查器已升级，需重新核验：python3 .claude/scripts/research_checker.py',
    REASON_REPORTS_CHANGED: '报告有增删或改名，需重新核验：python3 .claude/scripts/research_checker.py',
    REASON_INPUTS_CHANGED: '报告或源资料已改动，旧回执失效，需重新核验：python3 .claude/scripts/research_checker.py',
    REASON_MANUAL_PENDING: '仍有需人工确认的条目，确认并在报告中勾选后重新核验',
}


def hint(result: dict) -> str:
    """给用户的下一步提示；无需提示时为空串。"""
    if result['status'] == UNVERIFIABLE:
        return '来源无法核验，请修正读取或来源位置；不得以新增ID声明代替来源证据'
    if result['status'] == FAILED:
        return '报告存在确定性错误，修正后重新运行 python3 .claude/scripts/research_checker.py'
    return HINTS.get(result['reason'], '')
