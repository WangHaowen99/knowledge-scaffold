"""历史时间线（ADR-0094，移植 develop）：工作区的迭代与 Hub 上的发布版本合并，从新到旧分页。

迭代按页从 git 读、按主线版本号编号，发布版本按页从 Hub 读。一页只覆盖两边都已读到的范围，
后面的页里不会出现本该排在前一页上面的条目。
"""
from datetime import datetime
import re

from . import hub_packages
from .workspace import WorkspaceError

# 「迭代偏移:发布偏移」；这次翻页中 Hub 失败过就写 off
_CURSOR = re.compile(r'(\d{1,9}):(\d{1,9}|off)')
_RELEASE_FIELDS = ('release_id', 'ontology_id', 'version', 'package_digest', 'source_service', 'created_at')


def parse_cursor(cursor):
    if not cursor:
        return 0, 0
    match = _CURSOR.fullmatch(cursor)
    if not match:
        raise WorkspaceError('分页游标无效', 400)
    return int(match.group(1)), None if match.group(2) == 'off' else int(match.group(2))


def _release_entry(row):
    try:
        moment = int(datetime.fromisoformat(row['created_at']).timestamp())
    except (KeyError, TypeError, ValueError):
        moment = 0
    return {**{key: row[key] for key in _RELEASE_FIELDS if key in row}, 'kind': 'release', 'time': moment}


def merge(iterations, iterations_more, releases, releases_more, limit):
    """从新到旧，时间相同时发布版本在前；哪边还有没读到的条目可能排进来，就停在那里。"""
    items, i, r = [], 0, 0
    while len(items) < limit:
        iteration_ready, release_ready = i < len(iterations), r < len(releases)
        iteration_unread = not iteration_ready and iterations_more
        release_unread = not release_ready and releases_more
        if iteration_unread or release_unread:
            break
        if release_ready and (not iteration_ready or releases[r]['time'] >= iterations[i]['time']):
            items.append(releases[r])
            r += 1
        elif iteration_ready:
            items.append(iterations[i])
            i += 1
        else:
            break
    return items, i, r


def page(manager, pid, subject, token, limit=50, cursor='', can_revert=True):
    iteration_offset, release_offset = parse_cursor(cursor)
    iterations = [
        {**entry, 'kind': 'iteration', 'can_revert': can_revert}
        for entry in manager.versions(pid, limit, iteration_offset)
    ]
    # 版本号按提交计数，这一页最新的那条就说明一共有多少条
    iteration_total = iterations[0]['version'] + iteration_offset if iterations else iteration_offset
    iterations_more = iteration_offset + len(iterations) < iteration_total

    releases, release_total, release_error = [], 0, ''
    if release_offset is not None and subject:
        try:
            listing = hub_packages.releases(pid, subject, token, release_offset, limit)
            releases = [_release_entry(row) for row in listing['releases']]
            release_total = listing['total']
        except WorkspaceError as exc:
            # 之后的页不再读 Hub：中途接回来的发布版本会排错位置
            release_offset, release_error = None, str(exc)
    releases_more = release_offset is not None and release_offset + len(releases) < release_total

    items, used_iterations, used_releases = merge(iterations, iterations_more, releases, releases_more, limit)
    next_iteration = iteration_offset + used_iterations
    next_release = None if release_offset is None else release_offset + used_releases
    has_more = bool(items) and (
        next_iteration < iteration_total or (next_release is not None and next_release < release_total))
    return {
        'items': items,
        'next_cursor': f"{next_iteration}:{'off' if next_release is None else next_release}" if has_more else None,
        'release_error': release_error,
    }
