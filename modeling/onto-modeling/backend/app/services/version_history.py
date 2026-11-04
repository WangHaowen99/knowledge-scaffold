"""History timeline: workspace iterations merged with Hub releases, paged newest first.

Iterations are read from git page by page, numbered by commit ordinal; releases are read from
Hub page by page. A page only covers the span both sources have already read, so a later page
never holds an entry that belongs above an earlier one.
"""
from datetime import datetime
import re

from . import hub_packages
from .workspace import WorkspaceError

# "<iteration offset>:<release offset>"; "off" once Hub failed during this traversal.
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
    """Newest first; a release wins a tie. Stop where an unread entry of either source could belong."""
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


def page(manager, pid, subject, token, limit=50, cursor=''):
    iteration_offset, release_offset = parse_cursor(cursor)
    iterations = [{**entry, 'kind': 'iteration'} for entry in manager.versions(pid, limit, iteration_offset)]
    # Numbers count commits, so the newest one on a page reveals how many exist in total.
    iteration_total = iterations[0]['version'] + iteration_offset if iterations else iteration_offset
    iterations_more = iteration_offset + len(iterations) < iteration_total

    releases, release_total, release_error = [], 0, ''
    if release_offset is not None and subject:
        try:
            listing = hub_packages.releases(pid, subject, token, release_offset, limit)
            releases = [_release_entry(row) for row in listing['releases']]
            release_total = listing['total']
        except WorkspaceError as exc:
            # Later pages skip Hub: releases resuming mid-traversal would land out of order.
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
