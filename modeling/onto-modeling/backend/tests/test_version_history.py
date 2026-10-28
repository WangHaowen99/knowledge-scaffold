"""History timeline paging, using only synthetic iterations and releases."""
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app import config
from app.routers import projects
from app.services import auth, hub_packages, version_history
from app.services.workspace import WorkspaceError, WorkspaceManager


def iteration(version, moment):
    return {'commit': f'commit-{version}', 'version': version, 'time': moment, 'message': f'save {version}', 'tags': []}


def release(version, moment):
    stamp = datetime.fromtimestamp(moment, timezone.utc).isoformat()
    return {'release_id': f'release-{version}', 'hub_package_id': 'package-demo', 'version': version,
            'package_digest': 'a' * 64, 'source_service': 'modeling', 'created_at': stamp, 'status': 'committed'}


class FakeManager:
    """Pages a newest-first iteration list the way git log --skip/--max-count does."""
    def __init__(self, iterations):
        self.iterations = iterations

    def versions(self, pid, limit=50, offset=0):
        return [dict(entry) for entry in self.iterations[offset:offset + limit]]


@pytest.fixture
def hub(monkeypatch):
    """Hub listing over a synthetic release list, newest first, honouring offset/limit."""
    state = {'rows': [], 'calls': 0, 'fail': False, 'page_cap': None}

    def releases(pid, subject, token, offset=0, limit=20):
        state['calls'] += 1
        if state['fail']:
            raise WorkspaceError('知识包 Hub 不可用，请稍后重试', 503)
        size = min(limit, state['page_cap'] or limit)
        return {'releases': state['rows'][offset:offset + size], 'total': len(state['rows'])}
    monkeypatch.setattr(hub_packages, 'releases', releases)
    return state


def walk(manager, limit, subject='scope-a'):
    items, cursor, pages = [], '', []
    while True:
        result = version_history.page(manager, 'project-demo', subject, 'token-a', limit, cursor)
        pages.append(result)
        items += result['items']
        cursor = result['next_cursor']
        if not cursor:
            return items, pages


def keys(items):
    return [(item['kind'], item['version']) for item in items]


def test_merge_is_newest_first_and_a_release_wins_a_tie():
    iterations = [{'kind': 'iteration', 'time': t} for t in (30, 20, 10)]
    releases = [{'kind': 'release', 'time': t} for t in (20, 5)]
    merged, used_iterations, used_releases = version_history.merge(iterations, False, releases, False, 10)
    assert [(entry['kind'], entry['time']) for entry in merged] == [
        ('iteration', 30), ('release', 20), ('iteration', 20), ('iteration', 10), ('release', 5)]
    assert (used_iterations, used_releases) == (3, 2)


def test_pages_are_continuous_without_gaps_or_duplicates(hub):
    hub['rows'] = [release('0.1.2', 650), release('0.1.1', 350), release('0.1.0', 150)]
    manager = FakeManager([iteration(n, n * 100) for n in range(7, 0, -1)])
    items, pages = walk(manager, 3)
    assert keys(items) == [('iteration', 7), ('release', '0.1.2'), ('iteration', 6), ('iteration', 5),
                           ('iteration', 4), ('release', '0.1.1'), ('iteration', 3), ('iteration', 2),
                           ('release', '0.1.0'), ('iteration', 1)]
    assert all(len(result['items']) <= 3 for result in pages)
    # Iteration numbering is passed through from the existing window untouched.
    assert [item['commit'] for item in items if item['kind'] == 'iteration'] == [f'commit-{n}' for n in range(7, 0, -1)]


def test_a_short_hub_page_stops_before_entries_it_has_not_read(hub):
    hub['rows'] = [release('0.1.1', 450), release('0.1.0', 250)]
    hub['page_cap'] = 1
    manager = FakeManager([iteration(n, n * 100) for n in range(5, 0, -1)])
    first = version_history.page(manager, 'project-demo', 'scope-a', 'token-a', 10)
    assert keys(first['items']) == [('iteration', 5), ('release', '0.1.1')]
    items, _ = walk(manager, 10)
    assert keys(items) == [('iteration', 5), ('release', '0.1.1'), ('iteration', 4), ('iteration', 3),
                           ('release', '0.1.0'), ('iteration', 2), ('iteration', 1)]


def test_hub_failure_keeps_iterations_and_skips_hub_for_later_pages(hub):
    hub['rows'] = [release('0.1.0', 250)]
    hub['fail'] = True
    manager = FakeManager([iteration(n, n * 100) for n in range(4, 0, -1)])
    first = version_history.page(manager, 'project-demo', 'scope-a', 'token-a', 2)
    assert keys(first['items']) == [('iteration', 4), ('iteration', 3)]
    assert first['release_error'] and first['next_cursor'] == '2:off'
    hub['fail'] = False
    second = version_history.page(manager, 'project-demo', 'scope-a', 'token-a', 2, first['next_cursor'])
    assert keys(second['items']) == [('iteration', 2), ('iteration', 1)]
    assert hub['calls'] == 1 and second['next_cursor'] is None


def test_paging_reaches_the_first_commit_past_fifty_iterations(hub):
    hub['rows'] = [release('0.1.1', 5150), release('0.1.0', 50)]
    items, pages = walk(FakeManager([iteration(n, n * 100) for n in range(60, 0, -1)]), 50)
    assert [item['version'] for item in items if item['kind'] == 'iteration'] == list(range(60, 0, -1))
    assert keys(items)[:11] == [('iteration', n) for n in range(60, 51, -1)] + [('release', '0.1.1'), ('iteration', 51)]
    assert keys(items)[-1] == ('release', '0.1.0') and len(pages) == 2


def test_without_a_hub_identity_only_iterations_are_listed(hub):
    hub['rows'] = [release('0.1.0', 250)]
    items, pages = walk(FakeManager([iteration(2, 200), iteration(1, 100)]), 5, subject='')
    assert keys(items) == [('iteration', 2), ('iteration', 1)]
    assert hub['calls'] == 0 and pages[-1]['release_error'] == ''


@pytest.mark.parametrize('cursor', ['x', '1', '1:', '-1:0', '1:2:3', '1:on'])
def test_invalid_cursor_is_rejected(cursor):
    with pytest.raises(WorkspaceError) as exc:
        version_history.parse_cursor(cursor)
    assert exc.value.status == 400


def test_iteration_numbers_are_commit_ordinals_across_pages_and_new_saves(tmp_path):
    manager = WorkspaceManager(tmp_path / 'modeling', tmp_path / 'assets')
    pid = manager.create_project('project_alpha')['id']
    for n in range(4):
        manager.write_file(pid, 'notes.md', f'synthetic content {n}')
    everything = manager.versions(pid)
    total = len(everything)
    assert [entry['version'] for entry in everything] == list(range(total, 0, -1))
    first, second = manager.versions(pid, limit=3), manager.versions(pid, limit=3, offset=3)
    assert [entry['version'] for entry in first + second] == list(range(total, max(total - 6, 0), -1))
    numbered = {entry['commit']: entry['version'] for entry in everything}
    manager.write_file(pid, 'notes.md', 'synthetic content after')
    later = manager.versions(pid, limit=3)
    assert later[0]['version'] == total + 1
    assert all(numbered[entry['commit']] == entry['version'] for entry in later[1:])


def test_history_route_matches_existing_iteration_numbering(tmp_path, monkeypatch):
    manager = WorkspaceManager(tmp_path / 'modeling', tmp_path / 'assets')
    monkeypatch.setattr(config, 'DATA_DIR', tmp_path / 'modeling')
    monkeypatch.setattr(projects, 'workspaces', manager)
    monkeypatch.setattr(auth, 'ENABLED', False)
    pid = manager.create_project('project_alpha')['id']
    for n in range(3):
        manager.write_file(pid, 'notes.md', f'synthetic content {n}')
    app = FastAPI()
    app.include_router(projects.router)

    @app.exception_handler(WorkspaceError)
    async def error(request, exc):
        return JSONResponse({'error': str(exc)}, status_code=exc.status)
    with TestClient(app) as client:
        expected = client.get(f'/api/projects/{pid}/versions').json()['versions']
        listed, cursor = [], ''
        while True:
            result = client.get(f'/api/projects/{pid}/history', params={'limit': 2, 'cursor': cursor}).json()
            assert len(result['items']) <= 2
            listed += result['items']
            cursor = result['next_cursor']
            if not cursor:
                break
        assert len(expected) >= 4
        assert [(item['commit'], item['version']) for item in listed] == [(v['commit'], v['version']) for v in expected]
        assert {item['kind'] for item in listed} == {'iteration'}
        assert client.get(f'/api/projects/{pid}/history', params={'limit': 51}).status_code == 400
