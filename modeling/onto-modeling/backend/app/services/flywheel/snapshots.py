"""Workspace snapshots of flywheel runs: the iteration before optimization, its restore, and the diff.

The diff view stays bounded: at most DIFF_FILES files, DIFF_LINES lines of LINE_CHARS each per file
and DIFF_BUDGET characters in total; counts report what was left out.
"""
from ..workspace import ROLLBACK_EXCLUDED_PREFIXES, ROLLBACK_EXCLUDED_TOP, _commit_paths, _git

DIFF_FILES = 20
DIFF_LINES = 80
LINE_CHARS = 300
DIFF_BUDGET = 96 * 1024
# 外部 diff 驱动与 textconv 由工作区 git 配置决定，可能随导入的项目带入：一律关闭
DIFF = ('-c', 'core.quotepath=false', 'diff', '--no-ext-diff', '--no-textconv', '--no-color')
# 恢复时保持当前值的路径，与版本回退（ADR-0094 rollback_managed）一致：项目元数据（所有者、归档）、
# 运行状态、助手装配与仓库配置不随内容回到过去
KEPT = tuple(f':(exclude){name}' for name in (*sorted(ROLLBACK_EXCLUDED_TOP - {'.git'}), *ROLLBACK_EXCLUDED_PREFIXES))


def _lines(text):
    return [line for line in text.splitlines() if line.strip()]


def take(manager, pid, message):
    """Commit pending changes, so the iteration is exactly the current content (before or after a run).

    Commits go through the write path (_commit_paths): source trailers by the message prefix and a WorkspaceChanged
    event, like every other mainline commit. The assistant's own edits already arrive merged (changesets)."""
    root = manager.project_root(pid)
    with manager._project_lock(pid):
        _commit_paths(root, ['.'], message)
        commit = _git(root, 'rev-parse', 'HEAD').strip()
        iteration = int(_git(root, 'rev-list', '--count', 'HEAD').strip())
    return {'commit': commit, 'iteration': iteration}


def restore(manager, pid, commit, message):
    """Bring the package content back to the snapshot as a new iteration; history is kept.

    Documents added to ontology/ after the snapshot are removed too, so restored content is exactly
    the snapshot. Reports written since then stay as records; project metadata and runtime state (KEPT) stay
    as they are now.
    """
    root = manager.project_root(pid)
    with manager._project_lock(pid):
        _git(root, 'checkout', commit, '--', '.', *KEPT)
        added = _lines(_git(root, *DIFF, '--name-only', '--diff-filter=A', commit, '--', 'ontology'))
        if added:
            _git(root, 'rm', '-q', '-f', '--', *added)
        for rel in _lines(_git(root, '-c', 'core.quotepath=false', 'ls-files', '--others', '--exclude-standard',
                               '--', 'ontology')):
            (root / rel).unlink(missing_ok=True)
        _commit_paths(root, ['.'], message)
        iteration = int(_git(root, 'rev-list', '--count', 'HEAD').strip())
    return {'commit': _git(root, 'rev-parse', 'HEAD').strip(), 'iteration': iteration}


def _file_diff(root, base, head, path, budget):
    patch = _git(root, *DIFF, '--unified=1', base, *([head] if head else []), '--', path)
    lines, added, removed, used = [], 0, 0, 0
    for raw in patch.splitlines():
        if raw.startswith(('+++', '---', 'diff ', 'index ', 'new file', 'deleted file', 'similarity', 'rename')):
            continue
        kind = {'+': 'added', '-': 'removed', '@': 'hunk'}.get(raw[:1], 'context')
        added += kind == 'added'
        removed += kind == 'removed'
        if len(lines) < DIFF_LINES and used < budget:
            text = raw[1:] if kind in ('added', 'removed', 'context') else raw
            lines.append({'kind': kind, 'text': text[:LINE_CHARS]})
            used += len(text[:LINE_CHARS])
    shown = sum(1 for line in lines if line['kind'] in ('added', 'removed'))
    return {'added': added, 'removed': removed, 'lines': lines, 'truncated': shown < added + removed}, used


def changes(manager, pid, base, head=None):
    """Package documents changed from ``base`` to ``head`` (the working tree when head is None)."""
    root = manager.project_root(pid)
    names = []
    for line in _lines(_git(root, *DIFF, '--name-status', base, *([head] if head else []), '--', 'ontology')):
        parts = line.split('\t')
        names.append(({'A': 'added', 'D': 'deleted'}.get(parts[0][:1], 'modified'), parts[-1]))
    files, budget = [], DIFF_BUDGET
    for status, path in names[:DIFF_FILES]:
        detail, used = _file_diff(root, base, head, path, budget)
        budget -= used
        files.append({'path': path, 'status': status, **detail})
    return {'base': base, 'head': head, 'files': files, 'total': len(names), 'truncated': len(names) > DIFF_FILES}
