"""Query concurrency uses only synthetic identities and publication records."""
import asyncio
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from hub.package_queries import PackageQueries
from hub.maintenance import Maintenance, QueryMaintenance
from hub.storage import Store, write_json
from test_packages import api, publish, userinfo


def seed(store, package_id='ontology_demo', version='0.1.1', kind='release', stamp='1'):
    write_json(store.root / 'packages' / package_id / 'metadata.json', {
        'ontology_id': package_id, 'state': 'active', 'authorized_users': {'operator_a': {}}})
    row = dict(ontology_id=package_id, status='committed', release_id=stamp,
               version=version, kind=kind, committed_at=stamp)
    write_json(store.root / 'publish-requests' / (package_id + stamp + '.json'), row)
    return row


def test_single_scan_latest_and_snapshot_fallback(tmp_path):
    store = Store(tmp_path)
    seed(store)
    seed(store, version='0.1.2-eval.1', kind='evaluation_snapshot', stamp='2')
    seed(store, 'ontology_other', version='0.1.0-eval.1', kind='evaluation_snapshot')
    original = Path.read_text
    reads = []
    def read(path, *args, **kwargs):
        if path.parent.name == 'publish-requests':
            reads.append(path)
        return original(path, *args, **kwargs)
    with patch.object(Path, 'read_text', read):
        result = PackageQueries(store).packages('operator_a', limit=1)
    assert len(reads) == len(set(reads)) == 3
    assert result['total'] == 2 and result['next_offset'] == 1
    rows = PackageQueries(store).packages('operator_a')['items']
    assert {r['ontology_id']: r['latest_release_version'] for r in rows} == {
        'ontology_demo': '0.1.1', 'ontology_other': '0.1.0-eval.1'}
    assert all(r['version'] == r['latest_release_version'] for r in rows)


def test_missing_file_is_tolerated_but_corruption_is_not(tmp_path):
    store = Store(tmp_path)
    seed(store)
    original = Path.read_text
    def disappearing(path, *args, **kwargs):
        if path.parent.name == 'publish-requests':
            path.unlink(missing_ok=True)
        return original(path, *args, **kwargs)
    with patch.object(Path, 'read_text', disappearing):
        assert PackageQueries(store).packages('operator_a')['items'] == []
    seed(store)
    path = next((store.root / 'publish-requests').glob('*.json'))
    path.write_text('null')
    with pytest.raises(ValueError):
        PackageQueries(store).packages('operator_a')
    with patch.object(Path, 'read_text', side_effect=PermissionError):
        with pytest.raises(PermissionError):
            PackageQueries(store).packages('operator_a')


def test_queries_return_while_background_recovery_holds_lock(api):
    client, headers = api
    from hub import main
    entered, release = threading.Event(), threading.Event()
    def recover():
        entered.set()
        assert release.wait(5)
    worker = main.app.state.hub_maintenance
    with patch.object(main.store, 'recover_all', recover):
        worker.notify()
        assert entered.wait(3)
        try:
            with patch('hub.user_auth.urllib.request.OpenerDirector.open', side_effect=lambda *a, **k: userinfo()):
                assert client.get('/api/packages', headers=headers['consumer']).status_code == 200
                assert client.get('/api/packages/ontology_demo/releases', headers=headers['consumer']).status_code == 404
            assert worker.running
        finally:
            release.set()


def test_notice_is_after_final_body():
    events = []
    class Worker:
        def notify(self):
            events.append('notify')
    from types import SimpleNamespace
    scope = {'hub_query_complete': True, 'app': SimpleNamespace(state=SimpleNamespace(hub_maintenance=Worker()))}
    async def app(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200})
        await send({'type': 'http.response.body', 'body': b'{}'})
    async def send(message):
        events.append(message['type'])
    asyncio.run(QueryMaintenance(app)(scope, None, send))
    assert events == ['http.response.start', 'http.response.body', 'notify']


def test_maintenance_coalesces_and_lock_contention_does_not_queue(tmp_path):
    store = Store(tmp_path)
    first, second = Maintenance(store), Maintenance(store)
    entered, release = threading.Event(), threading.Event()
    calls = []
    def recover():
        calls.append(1)
        entered.set()
        assert release.wait(3)
    with patch.object(store, 'recover_all', recover):
        thread = threading.Thread(target=first.run_once)
        thread.start()
        assert entered.wait(2)
        try:
            second.run_once()
            assert calls == [1]
        finally:
            release.set()
            thread.join(3)
        second.run_once()
        assert calls == [1]


def test_query_observes_recovery_on_next_read(tmp_path):
    from hub.package_store import PackageStore
    store = Store(tmp_path)
    packages = PackageStore(store)
    with patch.object(packages, 'finish', side_effect=RuntimeError):
        with pytest.raises(RuntimeError):
            publish(packages)
    query = PackageQueries(store)
    assert query.packages('synthetic-owner')['items'] == []
    Maintenance(store).run_once()
    assert query.packages('synthetic-owner')['items'][0]['version'] == '0.1.1'


def test_size_limit_counts_alias_and_advances_actual_page(tmp_path):
    store = Store(tmp_path)
    seed(store, version='v' * 16000)
    seed(store, package_id='ontology_other', version='v' * 16000)
    query = PackageQueries(store)
    first = query.packages('operator_a')
    assert len(first['items']) == 1 and first['next_offset'] == 1
    assert len(query.packages('operator_a', offset=1)['items']) == 1


def test_deleted_target_and_invalid_json_responses(api):
    client, headers = api
    from hub import main
    from hub.user_auth import subject_id
    seed(main.store)
    meta = main.store.root / 'packages/ontology_demo/metadata.json'
    subject = subject_id('https://service.example.net', 'operator_a')
    write_json(meta, dict(ontology_id='ontology_demo', state='deleted', authorized_users={subject: {}}))
    with patch('hub.user_auth.urllib.request.OpenerDirector.open', side_effect=lambda *a, **k: userinfo()):
        response = client.get('/api/packages/ontology_demo/releases', headers=headers['consumer'])
        assert response.status_code == 410 and response.json()['code'] == 'package_deleted'
        assert client.get('/api/packages', headers=headers['consumer']).json()['items'] == []
        meta.write_text('null')
        response = client.get('/api/packages', headers=headers['consumer'])
        assert response.status_code == 503 and response.json()['code'] == 'storage_unavailable'


def test_committed_scan_excludes_pending_and_duplicate_rows(tmp_path):
    store = Store(tmp_path)
    row = seed(store)
    write_json(store.root / 'publish-requests/duplicate.json', dict(row, existing=True))
    write_json(store.root / 'publish-requests/pending.json', dict(row, status='committing', version='0.1.2'))
    assert PackageQueries(store).listing('ontology_demo', 'operator_a')['items'] == [row]


def test_failure_releases_maintenance_locks_for_retry(tmp_path):
    store = Store(tmp_path)
    worker = Maintenance(store, interval=0.001)
    with patch.object(store, 'recover_all', side_effect=ValueError):
        with pytest.raises(ValueError):
            worker.run_once()
    (store.root / '.maintenance.lock').write_text('0')
    with patch.object(store, 'recover_all') as recover:
        worker.run_once()
        recover.assert_called_once()


def _hold_maintenance_lock(root, ready, release):
    import fcntl
    with (Path(root) / '.maintenance.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        ready.set()
        release.wait(5)


def test_cross_process_maintenance_deduplication(tmp_path):
    import multiprocessing
    context = multiprocessing.get_context('spawn')
    ready, release = context.Event(), context.Event()
    store = Store(tmp_path)
    process = context.Process(target=_hold_maintenance_lock, args=(str(tmp_path), ready, release))
    process.start()
    try:
        assert ready.wait(3)
        with patch.object(store, 'recover_all') as recover:
            Maintenance(store).run_once()
            recover.assert_not_called()
    finally:
        release.set()
        process.join(5)
    assert process.exitcode == 0
