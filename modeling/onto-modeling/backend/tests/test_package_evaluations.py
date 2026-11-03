"""Independent-evaluation results of a project's Hub package: notifications, index, reads, optimization import.

The evaluation service is a synthetic in-process stub; all identifiers and contents are synthetic.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib
import json
from pathlib import Path
import threading

import pytest
from fastapi.testclient import TestClient

from app import config
from app.main import app
from app.routers import projects
from app.services import auth, eval_client, hub_packages, package_evaluations
from app.services.workspace import WorkspaceError, WorkspaceManager, _git

SERVICE_TOKEN = 'synthetic-evaluation-service-token-000000'
USER = {'sub': 'operator_a', 'iss': 'https://example.com'}
SUBJECT = json.dumps([USER['iss'], USER['sub']], ensure_ascii=False)
PACKAGE = 'pkg_demo'
# Evaluation-service owners of two users sharing the package; synthetic digests.
OWNER = 'a' * 64
OTHER_OWNER = 'b' * 64
RUN = 'c' * 32
OTHER_RUN = 'd' * 32
STEM = 'eval-20261031-101010-abc123'
SUMMARY = {'total': 2, 'pass': 1, 'partial': 0, 'fail': 1, 'error': 0, 'manual': 0, 'valid': 2,
           'score': 50, 'result_status': 'complete'}
SCRIPTS = Path(config.ASSETS_DIR) / 'workspace-toolkit' / 'scripts'


def artifacts(**changes):
    return {'revision': 1, 'status': 'ready', 'stem': STEM, 'generated_at': 1000.0, 'report': 'ready',
            'suggestions': 'ready', 'suggestions_error': None, 'suggestions_model': 'model_demo', **changes}


def entry(release='release_one', run=RUN, finished=1000.0, **changes):
    return {'release_id': release, 'version': '0.1.1', 'run_id': run, 'status': 'completed', 'created_at': 900.0,
            'finished_at': finished, 'artifacts': artifacts(), 'summary': SUMMARY, **changes}


def notification(sequence=1, owner=OWNER, **changes):
    value = {**entry(**changes), 'sequence': sequence, 'owner': owner, 'ontology_id': PACKAGE}
    value['event_id'] = f"{value.get('run_id')}:{sequence}"
    return value


class EvaluationStub:
    """Answers the evaluation API paths modeling uses; records every request."""

    def __init__(self):
        self.calls = []
        self.failure = None
        self.listing = {'ontology_id': PACKAGE, 'owner': OWNER, 'active': None, 'releases': []}
        self.run = {'id': RUN, 'status': 'completed', 'artifacts': artifacts(),
                    'base_ref': {'ontology_id': PACKAGE, 'release_id': 'release_one', 'version': '0.1.1'},
                    'package_ref': {'ontology_id': PACKAGE, 'release_id': 'snapshot_one', 'version': '0.1.1-eval.1'}}
        self.files = {
            'report.md': '# 数据评测报告\n\n- **问题**：合成问题一\n'.encode(),
            'answers.json': json.dumps({'answers': []}).encode(),
            'suggestions.md': '# 评测优化建议\n'.encode(),
            'context.json': json.dumps({'run_id': RUN, 'revision': 1, 'stem': STEM, 'summary': SUMMARY,
                                        'tables': {'订单表': ['order_id', '金额'], '../bad': ['x'],
                                                   'empty_table': []}}, ensure_ascii=False).encode(),
        }

    def __call__(self, method, path, token, body=None, limit=eval_client.JSON_LIMIT):
        self.calls.append((method, path, token))
        if self.failure:
            raise self.failure
        if path.startswith('/package-evaluations?'):
            return json.dumps(self.listing).encode(), {}
        if path == f'/runs/{RUN}':
            return json.dumps(self.run).encode(), {}
        if path == f'/runs/{RUN}/artifacts/regenerate':
            return json.dumps(artifacts(revision=2, status='generating', report='pending',
                                        suggestions='pending')).encode(), {}
        name = path.rsplit('/', 1)[-1]
        if path.startswith(f'/runs/{RUN}/artifacts/') and name in self.files:
            data = self.files[name]
            assert len(data) <= limit
            revision = self.run['artifacts']['revision']
            return data, {'x-artifact-revision': str(revision), 'x-artifact-stem': self.run['artifacts']['stem']}
        raise WorkspaceError('评测记录不存在或无权访问', 404, 'eval_not_found')


@pytest.fixture
def env(tmp_path, monkeypatch):
    manager = WorkspaceManager(tmp_path / 'modeling', config.ASSETS_DIR)
    monkeypatch.setattr(config, 'DATA_DIR', tmp_path / 'modeling')
    monkeypatch.setattr(config, 'EVAL_URL', 'http://127.0.0.1:8090')
    monkeypatch.setattr(projects, 'workspaces', manager)
    monkeypatch.setattr(auth, 'ENABLED', True)
    monkeypatch.setattr(auth, 'verify_access_token', lambda token: dict(USER) if token == 'user-token' else None)
    monkeypatch.setenv('ONTO_EVALUATION_SERVICE_TOKEN', SERVICE_TOKEN)
    stub = EvaluationStub()
    monkeypatch.setattr(eval_client, 'request', stub)
    pid = manager.create_project('project_alpha', owner='operator_a')['id']
    hub_packages.record(manager, pid, SUBJECT, {'ontology_id': PACKAGE,
                                               'release_id': 'release_one', 'package_digest': 'b' * 64,
                                               'version': '0.1.1', 'committed_at': '2026-10-31T00:00:00+00:00'})
    client = TestClient(app)
    client.headers['Authorization'] = 'Bearer user-token'
    return manager, pid, stub, client


def notify(client, body, token=SERVICE_TOKEN):
    headers = {'Authorization': ''}
    if token is not None:
        headers['X-Evaluation-Service-Token'] = token
    return client.post('/api/internal/evaluation/notifications', json=body, headers=headers)


def index(owner=OWNER):
    return package_evaluations._read_index(PACKAGE, owner)


def test_notification_requires_evaluation_service_identity(env, monkeypatch):
    _, _, _, client = env
    assert notify(client, notification(), token=None).status_code == 401
    assert notify(client, notification(), token='wrong-token').status_code == 401
    monkeypatch.setenv('ONTO_EVALUATION_SERVICE_TOKEN', '')
    assert notify(client, notification(), token='').status_code == 401
    assert index()['releases'] == {}


def test_notification_rejects_invalid_bodies(env):
    _, _, _, client = env
    mismatched = {**notification(sequence=2), 'event_id': f'{RUN}:1'}
    assert notify(client, mismatched).status_code == 422
    assert notify(client, notification(run='not-a-run-id')).status_code == 422
    assert notify(client, {**notification(), 'summary': {**SUMMARY, 'score': 101}}).status_code == 422
    assert index()['releases'] == {}


def test_notification_persists_and_ignores_duplicates_and_late_deliveries(env):
    _, _, _, client = env
    first = notification(sequence=1, artifacts=artifacts(status='generating', suggestions='generating'))
    assert notify(client, first).json() == {'accepted': True}
    assert index()['releases']['release_one']['artifacts']['suggestions'] == 'generating'
    assert notify(client, notification(sequence=2)).status_code == 200
    assert notify(client, first).json() == {'accepted': True}  # 迟到的旧通知不回退
    stored = index()['releases']['release_one']
    assert stored['sequence'] == 2 and stored['artifacts']['suggestions'] == 'ready'
    older = notification(sequence=5, run=OTHER_RUN, finished=500.0)
    assert notify(client, older).status_code == 200
    assert index()['releases']['release_one']['run_id'] == RUN
    newer = notification(sequence=1, run=OTHER_RUN, finished=2000.0)
    assert notify(client, newer).status_code == 200
    assert index()['releases']['release_one']['run_id'] == OTHER_RUN


def test_notification_without_report_clears_active_but_keeps_previous_report(env):
    _, _, _, client = env
    assert notify(client, notification(sequence=1)).status_code == 200
    state = index()
    state['active'] = {'run_id': OTHER_RUN, 'release_id': 'release_one', 'version': '0.1.1', 'status': 'running'}
    package_evaluations.knowledge_hub.save(package_evaluations._index_path(PACKAGE, OWNER), state)
    cancelled = notification(sequence=1, run=OTHER_RUN, finished=2000.0, status='cancelled',
                             artifacts=artifacts(status='failed', report='failed', suggestions='failed'))
    assert notify(client, cancelled).status_code == 200
    assert index()['active'] is None
    assert index()['releases']['release_one']['run_id'] == RUN


def test_overview_reconciles_with_the_evaluation_service(env):
    _, pid, stub, client = env
    assert notify(client, notification(sequence=1)).status_code == 200
    # 对账前不知道当前用户在评测服务的所有者标识，不显示任何人的评测
    unknown = client.get(f'/api/projects/{pid}/package/evaluations').json()
    assert unknown['reachable'] is None and unknown['releases'] == {}
    assert unknown['current_release_id'] == 'release_one' and unknown['ontology_id'] == PACKAGE
    stub.listing = {'ontology_id': PACKAGE, 'owner': OWNER, 'releases': [entry(release='release_two', run=OTHER_RUN)],
                    'active': {'run_id': RUN, 'release_id': 'release_one', 'version': '0.1.1', 'status': 'running'}}
    synced = client.get(f'/api/projects/{pid}/package/evaluations?reconcile=1').json()
    assert synced['reachable'] is True and list(synced['releases']) == ['release_two']
    assert synced['active']['run_id'] == RUN and 'sequence' not in synced['releases']['release_two']
    # 对账之后，本地轮询读到同一用户的索引，包括随后到达的通知
    assert notify(client, notification(sequence=2, finished=3000.0)).status_code == 200
    cached = client.get(f'/api/projects/{pid}/package/evaluations').json()
    assert cached['reachable'] is None and sorted(cached['releases']) == ['release_one', 'release_two']
    stub.failure = WorkspaceError('评测服务不可用，请稍后重试', 503, 'eval_unavailable')
    offline = client.get(f'/api/projects/{pid}/package/evaluations?reconcile=1').json()
    assert offline['reachable'] is False and offline['message']
    assert sorted(offline['releases']) == ['release_one', 'release_two'] and offline['active'] is None
    assert all(token == 'user-token' for _, _, token in stub.calls)


def test_evaluations_of_users_sharing_a_package_stay_separate(env):
    _, pid, stub, client = env
    assert client.get(f'/api/projects/{pid}/package/evaluations?reconcile=1').json()['reachable'] is True
    # 共享同一知识包的另一位用户完成了评测：只进入该用户的索引
    assert notify(client, notification(sequence=1, owner=OTHER_OWNER)).status_code == 200
    mine = client.get(f'/api/projects/{pid}/package/evaluations').json()
    assert mine['releases'] == {}
    assert list(index(OTHER_OWNER)['releases']) == ['release_one']
    # 当前用户对账只替换自己的索引
    stub.listing = {**stub.listing, 'releases': [entry(release='release_two')]}
    synced = client.get(f'/api/projects/{pid}/package/evaluations?reconcile=1').json()
    assert list(synced['releases']) == ['release_two']
    assert list(index(OTHER_OWNER)['releases']) == ['release_one']
    # 评测服务没有给出有效的所有者标识时按响应格式错误处理，不写入任何索引
    stub.listing = {**stub.listing, 'owner': '../escape'}
    invalid = client.get(f'/api/projects/{pid}/package/evaluations?reconcile=1').json()
    assert invalid['reachable'] is False and list(invalid['releases']) == ['release_two']


def test_overview_without_evaluation_service_address(env, monkeypatch):
    _, pid, stub, client = env
    monkeypatch.setattr(config, 'EVAL_URL', '')
    value = client.get(f'/api/projects/{pid}/package/evaluations?reconcile=1').json()
    assert value['configured'] is False and value['message'] == '未配置评测服务地址'
    assert value['releases'] == {} and stub.calls == []


def test_documents_only_for_runs_of_the_project_package(env, monkeypatch):
    _, pid, stub, client = env
    report = client.get(f'/api/projects/{pid}/package/evaluations/{RUN}/report').json()
    assert report['content'].startswith('# 数据评测报告') and report['truncated'] is False
    assert report['release_id'] == 'release_one' and report['revision'] == 1
    monkeypatch.setattr(package_evaluations, 'VIEW_LIMIT', 18)
    stub.files['report.md'] = 'line one\nline two\nline three\n'.encode()
    cut = client.get(f'/api/projects/{pid}/package/evaluations/{RUN}/report').json()
    assert cut['truncated'] is True and cut['content'] == 'line one\nline two'
    stub.run['artifacts'] = artifacts(suggestions='failed', suggestions_error='未配置判分模型')
    failed = client.get(f'/api/projects/{pid}/package/evaluations/{RUN}/suggestions').json()
    assert failed['status'] == 'failed' and failed['error'] == '未配置判分模型' and failed['content'] == ''
    stub.run['base_ref'] = {'ontology_id': 'pkg_other', 'release_id': 'release_one', 'version': '0.1.1'}
    assert client.get(f'/api/projects/{pid}/package/evaluations/{RUN}/report').status_code == 404
    assert client.get(f'/api/projects/{pid}/package/evaluations/not-a-run/report').status_code == 404


def test_regenerate_marks_index_generating(env):
    _, pid, stub, client = env
    stub.listing = {**stub.listing, 'releases': [entry()]}
    assert client.get(f'/api/projects/{pid}/package/evaluations?reconcile=1').json()['reachable'] is True
    value = client.post(f'/api/projects/{pid}/package/evaluations/{RUN}/regenerate', json={}).json()
    assert value['revision'] == 2 and value['status'] == 'generating'
    assert index()['releases']['release_one']['artifacts']['status'] == 'generating'


def test_optimization_requires_the_current_project_version(env):
    manager, pid, stub, client = env
    stub.run['base_ref'] = {'ontology_id': PACKAGE, 'release_id': 'release_old', 'version': '0.1.0'}
    response = client.post(f'/api/projects/{pid}/package/evaluations/{RUN}/optimization', json={})
    assert response.status_code == 409 and response.json()['code'] == 'version_mismatch'
    assert '0.1.0' in response.json()['error'] and '0.1.1' in response.json()['error']
    assert not (manager.project_root(pid) / 'reports' / f'{STEM}.md').exists()


def test_optimization_requires_a_ready_report(env):
    _, pid, stub, client = env
    stub.run['artifacts'] = artifacts(status='generating', report='pending', suggestions='pending')
    response = client.post(f'/api/projects/{pid}/package/evaluations/{RUN}/optimization', json={})
    assert response.status_code == 409 and response.json()['code'] == 'artifacts_not_ready'


def test_optimization_imports_artifacts_once(env, monkeypatch):
    manager, pid, stub, client = env
    response = client.post(f'/api/projects/{pid}/package/evaluations/{RUN}/optimization', json={})
    assert response.status_code == 200  # 不在已下线的 /eval/ 前缀下，未被 410 拦截
    suggestions = 'optimization-20261031-101010-abc123.md'
    assert response.json() == {'report': f'{STEM}.md', 'suggestions': suggestions,
                               'command': f'/ontology-evolution {STEM}.md'}
    root = manager.project_root(pid)
    assert (root / 'reports' / f'{STEM}.md').read_bytes() == stub.files['report.md']
    assert (root / 'reports' / f'{STEM}.answers.json').is_file()
    assert (root / 'reports' / suggestions).read_bytes() == stub.files['suggestions.md']
    assert (root / '.ontology-state' / 'eval-service' / f'{STEM}.json').read_bytes() == stub.files['context.json']
    tables = root / '.ontology-state' / 'eval-data' / 'eval-service'
    assert sorted(p.name for p in tables.iterdir()) == ['订单表.csv']
    assert (tables / '订单表.csv').read_text(encoding='utf-8') == 'order_id,金额\n'
    log = _git(root, 'log', '-1', '--name-only', '--format=%s')
    assert f'导入独立评测报告 {STEM}' in log and '.ontology-state' not in log
    downloads = len(stub.calls)
    again = client.post(f'/api/projects/{pid}/package/evaluations/{RUN}/optimization', json={})
    assert again.json() == response.json()
    assert len(stub.calls) == downloads + 1  # 只重新读取轮次，不再下载产物

    monkeypatch.syspath_prepend(str(SCRIPTS))
    check_eval_data = importlib.import_module('check_eval_data')
    assert check_eval_data.table_headers(root)['订单表'] == ['order_id', '金额']


def test_optimization_rejects_artifacts_of_mixed_revisions(env, monkeypatch):
    _, pid, stub, client = env
    original = stub.__call__

    def changing(method, path, token, body=None, limit=eval_client.JSON_LIMIT):
        data, headers = original(method, path, token, body, limit)
        if path.endswith('/context.json'):
            headers = {**headers, 'x-artifact-revision': '2'}
        return data, headers
    monkeypatch.setattr(eval_client, 'request', changing)
    response = client.post(f'/api/projects/{pid}/package/evaluations/{RUN}/optimization', json={})
    assert response.status_code == 409 and response.json()['code'] == 'artifacts_changed'


def test_legacy_evaluation_mutations_stay_retired(env):
    _, pid, _, client = env
    assert client.post(f'/api/projects/{pid}/eval/run', json={}).status_code == 410


@pytest.fixture
def evaluation_server(monkeypatch):
    """A real loopback HTTP server, so status mapping and bounded reads run against actual responses."""
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers.get('Authorization')))
            status, body = {
                '/evaluation/api/ok': (200, b'{"value": 1}'),
                '/evaluation/api/large': (200, b'x' * (eval_client.JSON_LIMIT + 1)),
                '/evaluation/api/unauthenticated': (401, b'{}'),
                '/evaluation/api/forbidden': (403, b'{}'),
                '/evaluation/api/missing': (404, b'{}'),
                '/evaluation/api/active': (409, '{"code":"run_active","message":"评测进行中"}'.encode()),
                '/evaluation/api/conflict': (409, b'<html>synthetic upstream page</html>'),
                '/evaluation/api/broken': (500, b'synthetic upstream detail'),
                '/evaluation/api/redirect': (302, b''),
            }[self.path]
            self.send_response(status)
            if status == 302:
                self.send_header('Location', '/evaluation/api/ok')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(config, 'EVAL_URL', f'http://127.0.0.1:{server.server_port}')
    yield seen
    server.shutdown()
    server.server_close()


def test_client_sends_the_user_login_and_parses_json(evaluation_server):
    assert eval_client.call('GET', '/ok', 'user-token') == {'value': 1}
    assert evaluation_server == [('/evaluation/api/ok', 'Bearer user-token')]


@pytest.mark.parametrize('path,status,code', [
    ('/unauthenticated', 401, 'eval_unauthorized'),
    ('/forbidden', 404, 'eval_not_found'),
    ('/missing', 404, 'eval_not_found'),
    ('/active', 409, 'run_active'),
    ('/conflict', 409, 'eval_conflict'),
    ('/broken', 503, 'eval_unavailable'),
    ('/redirect', 503, 'eval_unavailable'),
    ('/large', 502, 'eval_response_limit'),
])
def test_client_maps_failures_without_echoing_upstream_bodies(evaluation_server, path, status, code):
    with pytest.raises(WorkspaceError) as caught:
        eval_client.call('GET', path, 'user-token')
    assert (caught.value.status, caught.value.code) == (status, code)
    assert 'synthetic upstream' not in str(caught.value) and '127.0.0.1' not in str(caught.value)
    assert len(evaluation_server) == 1  # 不跟随重定向


def test_client_keeps_the_services_own_conflict_message(evaluation_server):
    with pytest.raises(WorkspaceError, match='评测进行中'):
        eval_client.call('GET', '/active', 'user-token')


def test_client_reports_an_unreachable_service(monkeypatch):
    monkeypatch.setattr(config, 'EVAL_URL', 'http://127.0.0.1:9')
    with pytest.raises(WorkspaceError) as caught:
        eval_client.call('GET', '/ok', 'user-token')
    assert caught.value.status == 503 and caught.value.code == 'eval_unavailable'


def test_client_requires_address_and_login(monkeypatch):
    monkeypatch.setattr(config, 'EVAL_URL', '')
    with pytest.raises(WorkspaceError) as unconfigured:
        eval_client.call('GET', '/ok', 'user-token')
    assert unconfigured.value.code == 'eval_unconfigured'
    with pytest.raises(WorkspaceError) as anonymous:
        eval_client.call('GET', '/ok', '')
    assert anonymous.value.status == 401
