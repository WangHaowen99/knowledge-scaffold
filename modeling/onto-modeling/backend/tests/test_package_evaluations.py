"""已发布版本的独立评测（ADR-0092，ES-01～ES-11）：完成通知、状态索引、以用户登录读回结果、评测优化成批反馈、
建模回写，以及旧版评测记录只读。

评测服务是进程内的合成桩；标识与内容全部为合成数据。
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib
import json
from pathlib import Path
import shutil
import threading

import pytest
from fastapi.testclient import TestClient

from app import config
from app.main import app
from app.api import deps
from app.services import auth, eval_client, feedback_inbox, hub_packages, package_evaluations
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
        self.writebacks, self.writeback_failure, self.optimization = [], None, None
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
            # Like the evaluation service: what modeling wrote back is part of the listing.
            listing = {**self.listing, 'releases': [{**item, 'optimization': self.optimization}
                                                    if self.optimization and item['run_id'] == RUN else item
                                                    for item in self.listing['releases']]}
            return json.dumps(listing).encode(), {}
        if path == f'/runs/{RUN}':
            return json.dumps(self.run).encode(), {}
        if path == f'/runs/{RUN}/modeling-result':
            self.writebacks.append(body)
            if self.writeback_failure:
                raise self.writeback_failure
            result = {key: body.get(key) for key in ('outcome', 'release_id', 'note')}
            status = 'modeling_done' if body['outcome'] == 'done' else 'modeling_failed'
            self.optimization = {'status': status, 'updated_at': 2000.0, 'history': [],
                                 'result': {**result, 'version': '0.1.2' if body.get('release_id') else None, 'reported_at': 2000.0}}
            return json.dumps({'run_id': RUN, 'optimization': self.optimization}).encode(), {}
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
    monkeypatch.setattr(deps, 'workspaces', manager)
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
    # 评测优化交给反馈演进包处理
    assert client.post(f'/api/v1/projects/{pid}/packs', json={'name': 'evo'}).status_code == 201
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


def test_notification_skips_user_login_and_trusts_only_the_service_token(env):
    """登录开着时，评测服务不带用户令牌也能送达；用户令牌代替不了服务凭据。env 只用来开登录、配服务凭据。"""
    bare = TestClient(app)  # 不带 Authorization
    assert auth.ENABLED
    accepted = bare.post('/api/internal/evaluation/notifications', json=notification(),
                         headers={'X-Evaluation-Service-Token': SERVICE_TOKEN})
    assert accepted.status_code == 200 and accepted.json() == {'accepted': True}
    assert list(index()['releases']) == ['release_one']
    wrong = bare.post('/api/internal/evaluation/notifications', json=notification(sequence=2),
                      headers={'X-Evaluation-Service-Token': 'wrong-token'})
    assert wrong.status_code == 401
    user_only = bare.post('/api/internal/evaluation/notifications', json=notification(sequence=3),
                          headers={'Authorization': 'Bearer user-token'})
    assert user_only.status_code == 401
    assert index()['releases']['release_one']['sequence'] == 1


def test_notification_rejects_invalid_bodies(env):
    _, _, _, client = env
    mismatched = {**notification(sequence=2), 'event_id': f'{RUN}:1'}
    # 校验不过一律 400：评测服务把 400、413、422 都当拒收，不再重试
    assert notify(client, mismatched).status_code == 400
    assert notify(client, notification(run='not-a-run-id')).status_code == 400
    assert notify(client, {**notification(), 'summary': {**SUMMARY, 'score': 101}}).status_code == 400
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
    unknown = client.get(f'/api/v1/projects/{pid}/evaluations').json()
    assert unknown['reachable'] is None and unknown['releases'] == {}
    assert unknown['current_release_id'] == 'release_one' and unknown['ontology_id'] == PACKAGE
    stub.listing = {'ontology_id': PACKAGE, 'owner': OWNER, 'releases': [entry(release='release_two', run=OTHER_RUN)],
                    'active': {'run_id': RUN, 'release_id': 'release_one', 'version': '0.1.1', 'status': 'running'}}
    synced = client.get(f'/api/v1/projects/{pid}/evaluations?reconcile=1').json()
    assert synced['reachable'] is True and list(synced['releases']) == ['release_two']
    assert synced['active']['run_id'] == RUN and 'sequence' not in synced['releases']['release_two']
    # 对账之后，本地轮询读到同一用户的索引，包括随后到达的通知
    assert notify(client, notification(sequence=2, finished=3000.0)).status_code == 200
    cached = client.get(f'/api/v1/projects/{pid}/evaluations').json()
    assert cached['reachable'] is None and sorted(cached['releases']) == ['release_one', 'release_two']
    stub.failure = WorkspaceError('评测服务不可用，请稍后重试', 503, 'eval_unavailable')
    offline = client.get(f'/api/v1/projects/{pid}/evaluations?reconcile=1').json()
    assert offline['reachable'] is False and offline['message']
    assert sorted(offline['releases']) == ['release_one', 'release_two'] and offline['active'] is None
    assert all(token == 'user-token' for _, _, token in stub.calls)


SOLVER_SUMMARY = {'total': 1, 'passed': 0, 'failed': 1, 'first_failure_code': 'solver_arguments_mismatch',
                  'first_failure_message': 'Agent 传给求解工具的入参与用例不一致'}


def test_solver_validation_notifications_are_indexed_apart_from_data_evaluations(env):
    _, pid, stub, client = env
    assert client.get(f'/api/v1/projects/{pid}/evaluations?reconcile=1').json()['reachable'] is True
    assert notify(client, notification(sequence=1)).status_code == 200
    solver = notification(sequence=1, run=OTHER_RUN, finished=2000.0, run_type='solver', solver_summary=SOLVER_SUMMARY)
    assert notify(client, solver).status_code == 200
    # 求解验证不覆盖同一版本的数据评测条目
    assert index()['releases']['release_one']['run_id'] == RUN
    assert index()['solver_releases']['release_one']['run_id'] == OTHER_RUN
    view = client.get(f'/api/v1/projects/{pid}/evaluations').json()
    assert view['releases']['release_one']['run_type'] == 'data'
    stored = view['solver_releases']['release_one']
    assert stored['run_type'] == 'solver' and stored['solver_summary']['first_failure_code'] == 'solver_arguments_mismatch'
    assert view['solver_revisions'] == {}


def test_reconcile_reads_solver_releases_and_accepts_older_services(env):
    _, pid, stub, client = env
    stub.listing = {'ontology_id': PACKAGE, 'owner': OWNER, 'releases': [entry()],
                    'solver_releases': [entry(run=OTHER_RUN, run_type='solver', solver_summary=SOLVER_SUMMARY)],
                    'active': {'run_id': RUN, 'release_id': 'release_two', 'version': '0.1.2', 'status': 'running',
                               'run_type': 'solver'}}
    synced = client.get(f'/api/v1/projects/{pid}/evaluations?reconcile=1').json()
    assert synced['releases']['release_one']['run_id'] == RUN
    assert synced['solver_releases']['release_one']['run_id'] == OTHER_RUN
    assert synced['active']['run_type'] == 'solver'
    # 只有数据评测的评测服务不返回 solver_releases，也没有 run_type
    stub.listing = {'ontology_id': PACKAGE, 'owner': OWNER, 'releases': [entry()], 'active': None}
    older = client.get(f'/api/v1/projects/{pid}/evaluations?reconcile=1').json()
    assert older['reachable'] is True and older['solver_releases'] == {}
    assert older['releases']['release_one']['run_type'] == 'data'


def test_index_written_before_solver_validation_is_still_readable(env):
    _, pid, _, client = env
    assert client.get(f'/api/v1/projects/{pid}/evaluations?reconcile=1').json()['reachable'] is True
    legacy = {'ontology_id': PACKAGE, 'active': None, 'reconciled_at': 1.0,
              'releases': {'release_one': {**entry(), 'sequence': 1, 'received_at': 1.0}}}
    package_evaluations.knowledge_hub.save(package_evaluations._index_path(PACKAGE, OWNER), legacy)
    view = client.get(f'/api/v1/projects/{pid}/evaluations').json()
    assert list(view['releases']) == ['release_one'] and view['solver_releases'] == {}
    assert notify(client, notification(sequence=2, run_type='solver', solver_summary=SOLVER_SUMMARY)).status_code == 200
    assert list(index()['solver_releases']) == ['release_one']


def test_solver_validation_runs_cannot_be_optimized(env):
    manager, pid, stub, client = env
    stub.run['run_type'] = 'solver'
    response = client.post(f'/api/v1/projects/{pid}/evaluations/{RUN}/optimization', json={})
    assert response.status_code == 409 and response.json()['code'] == 'optimization_not_supported'
    assert not (manager.project_root(pid) / 'reports' / f'{STEM}.md').exists()


def test_evaluations_of_users_sharing_a_package_stay_separate(env):
    _, pid, stub, client = env
    assert client.get(f'/api/v1/projects/{pid}/evaluations?reconcile=1').json()['reachable'] is True
    # 共享同一知识包的另一位用户完成了评测：只进入该用户的索引
    assert notify(client, notification(sequence=1, owner=OTHER_OWNER)).status_code == 200
    mine = client.get(f'/api/v1/projects/{pid}/evaluations').json()
    assert mine['releases'] == {}
    assert list(index(OTHER_OWNER)['releases']) == ['release_one']
    # 当前用户对账只替换自己的索引
    stub.listing = {**stub.listing, 'releases': [entry(release='release_two')]}
    synced = client.get(f'/api/v1/projects/{pid}/evaluations?reconcile=1').json()
    assert list(synced['releases']) == ['release_two']
    assert list(index(OTHER_OWNER)['releases']) == ['release_one']
    # 评测服务没有给出有效的所有者标识时按响应格式错误处理，不写入任何索引
    stub.listing = {**stub.listing, 'owner': '../escape'}
    invalid = client.get(f'/api/v1/projects/{pid}/evaluations?reconcile=1').json()
    assert invalid['reachable'] is False and list(invalid['releases']) == ['release_two']


def test_overview_without_evaluation_service_address(env, monkeypatch):
    _, pid, stub, client = env
    monkeypatch.setattr(config, 'EVAL_URL', '')
    value = client.get(f'/api/v1/projects/{pid}/evaluations?reconcile=1').json()
    assert value['configured'] is False and value['message'] == '未配置评测服务地址'
    assert value['releases'] == {} and stub.calls == []


def test_documents_only_for_runs_of_the_project_package(env, monkeypatch):
    _, pid, stub, client = env
    report = client.get(f'/api/v1/projects/{pid}/evaluations/{RUN}/report').json()
    assert report['content'].startswith('# 数据评测报告') and report['truncated'] is False
    assert report['release_id'] == 'release_one' and report['revision'] == 1
    monkeypatch.setattr(package_evaluations, 'VIEW_LIMIT', 18)
    stub.files['report.md'] = 'line one\nline two\nline three\n'.encode()
    cut = client.get(f'/api/v1/projects/{pid}/evaluations/{RUN}/report').json()
    assert cut['truncated'] is True and cut['content'] == 'line one\nline two'
    stub.run['artifacts'] = artifacts(suggestions='failed', suggestions_error='未配置判分模型')
    failed = client.get(f'/api/v1/projects/{pid}/evaluations/{RUN}/suggestions').json()
    assert failed['status'] == 'failed' and failed['error'] == '未配置判分模型' and failed['content'] == ''
    stub.run['base_ref'] = {'ontology_id': 'pkg_other', 'release_id': 'release_one', 'version': '0.1.1'}
    assert client.get(f'/api/v1/projects/{pid}/evaluations/{RUN}/report').status_code == 404
    assert client.get(f'/api/v1/projects/{pid}/evaluations/not-a-run/report').status_code == 404


def test_full_answer_preview_reads_the_attachment_without_writing_the_workspace(env):
    manager, pid, stub, client = env
    stub.files['answers.json'] = json.dumps({'roundId': RUN, 'answers': [
        {'resultId': 'case-1', 'question': '合成问题一', 'finalAnswer': '简短回答', 'originalLength': 4, 'truncated': False},
        {'resultId': 'case-2', 'question': '合成问题二', 'finalAnswer': '甲' * 8000, 'originalLength': 9000, 'truncated': True},
    ]}, ensure_ascii=False).encode()
    url = f'/api/v1/projects/{pid}/evaluations/{RUN}/answers'
    value = client.get(f'{url}/case-2').json()
    assert value == {'resultId': 'case-2', 'question': '合成问题二', 'fullAnswer': '甲' * 8000,
                     'originalLength': 9000, 'truncated': True}
    assert stub.calls[-1][1] == f'/runs/{RUN}/artifacts/answers.json'
    assert not list((manager.project_root(pid) / 'reports').glob('*.answers.json'))
    assert client.get(f'{url}/case-3').status_code == 404
    assert client.get(f'{url}/case-0').status_code == 404 and client.get(f'{url}/answers').status_code == 404
    stub.run['artifacts'] = artifacts(report='pending')
    assert client.get(f'{url}/case-1').json()['code'] == 'artifacts_not_ready'
    stub.run['base_ref'] = {'ontology_id': 'pkg_other', 'release_id': 'release_one', 'version': '0.1.1'}
    assert client.get(f'{url}/case-1').status_code == 404


def test_regenerate_marks_index_generating(env):
    _, pid, stub, client = env
    stub.listing = {**stub.listing, 'releases': [entry()]}
    assert client.get(f'/api/v1/projects/{pid}/evaluations?reconcile=1').json()['reachable'] is True
    value = client.post(f'/api/v1/projects/{pid}/evaluations/{RUN}/regenerate', json={}).json()
    assert value['revision'] == 2 and value['status'] == 'generating'
    assert index()['releases']['release_one']['artifacts']['status'] == 'generating'


def test_optimization_requires_the_current_project_version(env):
    manager, pid, stub, client = env
    stub.run['base_ref'] = {'ontology_id': PACKAGE, 'release_id': 'release_old', 'version': '0.1.0'}
    response = client.post(f'/api/v1/projects/{pid}/evaluations/{RUN}/optimization', json={})
    assert response.status_code == 409 and response.json()['code'] == 'version_mismatch'
    assert '0.1.0' in response.json()['message'] and '0.1.1' in response.json()['message']
    assert not (manager.project_root(pid) / 'reports' / f'{STEM}.md').exists()


def test_optimization_requires_the_evolution_pack(env):
    manager, pid, stub, client = env
    assert client.get(f'/api/v1/projects/{pid}/evaluations').json()['evolution'] is True
    assert client.post(f'/api/v1/projects/{pid}/packs/evo/disable', json={}).status_code == 200
    assert client.get(f'/api/v1/projects/{pid}/evaluations').json()['evolution'] is False
    response = client.post(f'/api/v1/projects/{pid}/evaluations/{RUN}/optimization', json={})
    assert response.status_code == 409 and response.json()['code'] == 'pack_not_enabled'
    assert not (manager.project_root(pid) / 'feedback').exists() and stub.calls == []


def test_optimization_requires_a_ready_report(env):
    _, pid, stub, client = env
    stub.run['artifacts'] = artifacts(status='generating', report='pending', suggestions='pending')
    response = client.post(f'/api/v1/projects/{pid}/evaluations/{RUN}/optimization', json={})
    assert response.status_code == 409 and response.json()['code'] == 'artifacts_not_ready'


REPORT_WITH_CASES = '\n'.join([
    '# 数据评测报告', '', '## 逐用例明细', '',
    '### 1. ✅ pass', '', '- **问题**：合成问题一', '', '- **标准答案**：', '', '  > 合成答案一', '',
    '### 2. ❌ fail', '', '- **问题**：合成问题二', '', '- **标准答案**：', '', '  > 第一行', '  >', '  > 第二行', '',
    '- **智能体回答**：', '', '  > 合成回答二', '', '- **判分理由**：', '', '  > 合成理由二', '',
    '- **工具失败**：', '', '  - execute_script · SCRIPT_ARGUMENT_VALIDATION_FAILED · 问题：ungrounded_identity arguments.code', '',
    '### 3. 🛑 error', '', '- **问题**：合成问题三', '', '- **标准答案**：', '', '  > （无）', '',
]).encode()
ANSWERS_WITH_CASES = json.dumps({'roundId': RUN, 'answers': [
    {'resultId': 'case-1', 'question': '合成问题一', 'finalAnswer': '合成回答一', 'verdict': 'pass', 'reason': '一致'},
    {'resultId': 'case-2', 'question': '合成问题二', 'finalAnswer': '合成回答二', 'verdict': 'fail', 'reason': '合成理由二'},
    {'resultId': 'case-3', 'question': '合成问题三', 'finalAnswer': '', 'verdict': 'error', 'reason': '运行失败'},
    {'resultId': 'case-4', 'question': '合成问题四', 'finalAnswer': '合成回答四', 'verdict': 'manual', 'reason': '待人工判定'},
]}, ensure_ascii=False).encode()


def test_optimization_imports_artifacts_once_as_a_feedback_batch(env, monkeypatch):
    manager, pid, stub, client = env
    stub.files.update({'report.md': REPORT_WITH_CASES, 'answers.json': ANSWERS_WITH_CASES})
    response = client.post(f'/api/v1/projects/{pid}/evaluations/{RUN}/optimization', json={})
    assert response.status_code == 200, response.text  # 不在已下线的 /eval/ 前缀下，未被 410 拦截
    suggestions = 'optimization-20261031-101010-abc123.md'
    batch_id = f'eval-{RUN}-r1'
    assert response.json() == {'feedback_id': batch_id, 'report': f'reports/{STEM}.md',
                               'suggestions': f'reports/{suggestions}', 'command': '/ontology-evolution',
                               'args': {'feedback_id': batch_id}}
    root = manager.project_root(pid)
    assert (root / 'reports' / f'{STEM}.md').read_bytes() == stub.files['report.md']
    assert (root / 'reports' / f'{STEM}.answers.json').read_bytes() == stub.files['answers.json']
    assert (root / 'reports' / suggestions).read_bytes() == stub.files['suggestions.md']
    assert (root / '.ontology-state' / 'eval-service' / f'{STEM}.json').read_bytes() == stub.files['context.json']
    tables = root / '.ontology-state' / 'eval-service' / 'tables' / STEM
    assert sorted(p.name for p in tables.iterdir()) == ['订单表.csv']
    assert (tables / '订单表.csv').read_text(encoding='utf-8') == 'order_id,金额\n'
    # 失分用例成为一批外部反馈：反馈演进照常消费；标准答案与工具失败取自报告
    rel = feedback_inbox.batch_path(root, batch_id)
    batch = json.loads((root / rel).read_text(encoding='utf-8'))
    assert batch['format'] == feedback_inbox.FORMAT and batch['received_by'] == USER['sub']
    assert batch['source'] == {'service': '评测服务', 'run': RUN} and batch['release_version'] == '0.1.1'
    assert batch['summary'] == {'score': 50, 'total': 2, 'pass': 1, 'partial': 0, 'fail': 1, 'error': 0}
    assert [item['case_id'] for item in batch['items']] == ['case-2', 'case-3']
    failed, broken = batch['items']
    assert failed['expected'] == '第一行\n\n第二行' and failed['actual'] == '合成回答二' and failed['verdict'] == 'fail'
    assert failed['loss_reason'] == '合成理由二'
    assert failed['tool_failures'] == ['execute_script · SCRIPT_ARGUMENT_VALIDATION_FAILED · 问题：ungrounded_identity arguments.code']
    assert broken['expected'] == '' and broken['verdict'] == 'error' and broken['tool_failures'] == []
    assert batch['attachments'] == {'report': f'reports/{STEM}.md', 'answers': f'reports/{STEM}.answers.json',
                                    'suggestions': f'reports/{suggestions}',
                                    'tables': f'.ontology-state/eval-service/tables/{STEM}'}
    assert feedback_inbox.latest_batch(root) == rel
    log = _git(root, 'log', '-1', '--name-only', '--format=%s')
    assert f'评测优化导入 {STEM}（2 条）' in log and rel in log and '.ontology-state' not in log
    assert _git(root, 'status', '--porcelain', '--', 'reports', 'feedback').strip() == ''
    downloads = len(stub.calls)
    again = client.post(f'/api/v1/projects/{pid}/evaluations/{RUN}/optimization', json={})
    assert again.json() == response.json()
    assert len(stub.calls) == downloads + 1  # 只重新读取轮次，不再下载产物
    assert len(list((root / 'feedback').glob('*.json'))) == 1

    # 三方对账脚本直接读导入的表头
    monkeypatch.syspath_prepend(str(SCRIPTS))
    table_profile = importlib.import_module('table_profile')
    assert table_profile.read_tables(tables / '订单表.csv') == {'订单表': ['order_id', '金额']}


def test_optimization_of_a_new_revision_is_a_new_batch(env):
    manager, pid, stub, client = env
    assert client.post(f'/api/v1/projects/{pid}/evaluations/{RUN}/optimization', json={}).status_code == 200
    root = manager.project_root(pid)
    shutil.rmtree(root / '.ontology-state' / 'eval-service' / 'tables')
    # 表头目录被清掉时补写，不重新下载
    calls = len(stub.calls)
    assert client.post(f'/api/v1/projects/{pid}/evaluations/{RUN}/optimization', json={}).status_code == 200
    assert len(stub.calls) == calls + 1 and (root / '.ontology-state' / 'eval-service' / 'tables' / STEM).is_dir()
    # 人工改判后产物修订号加一：同一轮再成一批，旧批次保留
    stub.run['artifacts'] = artifacts(revision=2)
    second = client.post(f'/api/v1/projects/{pid}/evaluations/{RUN}/optimization', json={}).json()
    assert second['feedback_id'] == f'eval-{RUN}-r2'
    assert sorted(p.name.split('-', 2)[2] for p in (root / 'feedback').glob('*.json')) == [
        f'eval-{RUN}-r1.json', f'eval-{RUN}-r2.json']


def test_optimization_of_a_feedback_run_names_its_source(env):
    manager, pid, stub, client = env
    stub.run['origin'] = 'consumer_feedback'
    batch_id = client.post(f'/api/v1/projects/{pid}/evaluations/{RUN}/optimization', json={}).json()['feedback_id']
    root = manager.project_root(pid)
    batch = json.loads((root / feedback_inbox.batch_path(root, batch_id)).read_text(encoding='utf-8'))
    assert batch['source']['service'] == '用户反馈' and batch['items'] == []


def test_optimization_rejects_malformed_answers(env):
    manager, pid, stub, client = env
    stub.files['answers.json'] = b'[]'
    response = client.post(f'/api/v1/projects/{pid}/evaluations/{RUN}/optimization', json={})
    assert response.status_code == 502 and response.json()['code'] == 'eval_invalid_response'
    assert not (manager.project_root(pid) / 'feedback').exists()


def test_optimization_rejects_artifacts_of_mixed_revisions(env, monkeypatch):
    _, pid, stub, client = env
    original = stub.__call__

    def changing(method, path, token, body=None, limit=eval_client.JSON_LIMIT):
        data, headers = original(method, path, token, body, limit)
        if path.endswith('/context.json'):
            headers = {**headers, 'x-artifact-revision': '2'}
        return data, headers
    monkeypatch.setattr(eval_client, 'request', changing)
    response = client.post(f'/api/v1/projects/{pid}/evaluations/{RUN}/optimization', json={})
    assert response.status_code == 409 and response.json()['code'] == 'artifacts_changed'


def test_legacy_evaluation_mutations_stay_retired(env):
    _, pid, _, client = env
    assert client.post(f'/api/projects/{pid}/eval/run', json={}).status_code == 410
    # 读的旧接口也是 410，替代接口指向旧版评测记录（只读）
    gone = client.get(f'/api/projects/{pid}/eval/status')
    assert gone.status_code == 410 and gone.json()['details']['successor'] == f'/api/v1/projects/{pid}/legacy-evaluations'


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


# ---- 建模回写与两种来源 ----


def optimize(client, pid):
    response = client.post(f'/api/v1/projects/{pid}/evaluations/{RUN}/optimization', json={})
    assert response.status_code == 200, response.text


def publish_next(manager, pid, release='release_two', version='0.1.2'):
    """A newer Hub release becomes the project's base, as after a publication."""
    hub_packages.record(manager, pid, SUBJECT, {'ontology_id': PACKAGE, 'release_id': release, 'package_digest': 'e' * 64,
                                               'version': version, 'committed_at': '2026-11-05T00:00:00+00:00'})


def overview(client, pid):
    response = client.get(f'/api/v1/projects/{pid}/evaluations?reconcile=1')
    assert response.status_code == 200, response.text
    return response.json()


def test_a_release_published_after_optimization_is_written_back_once(env):
    manager, pid, stub, client = env
    stub.listing['releases'] = [entry()]
    optimize(client, pid)
    assert overview(client, pid)['writeback']['status'] == 'waiting' and not stub.writebacks
    publish_next(manager, pid)
    value = overview(client, pid)
    sent, = stub.writebacks
    assert sent['outcome'] == 'done' and sent['release_id'] == 'release_two' and sent['request_id'].startswith('mr-')
    assert '0.1.1' in sent['note'] and '0.1.2' in sent['note']
    assert value['writeback'] == {**value['writeback'], 'status': 'done', 'release_id': 'release_two', 'version': '0.1.2', 'mine': True}
    # Shown at once, before the evaluation service's next notification or reconciliation.
    assert index()['releases']['release_one']['optimization']['status'] == 'modeling_done'
    overview(client, pid)
    publish_next(manager, pid, 'release_three', '0.1.3')
    overview(client, pid)
    assert len(stub.writebacks) == 1  # 同一次优化只回写第一个新版本


def test_right_after_publishing_the_write_back_is_immediate(env):
    manager, pid, stub, client = env
    optimize(client, pid)
    publish_next(manager, pid)
    receipt = {'status': 'committed', 'release_id': 'release_two', 'version': '0.1.2'}
    value = package_evaluations.after_publish(pid, SUBJECT, 'user-token', receipt)
    assert value['status'] == 'done' and len(stub.writebacks) == 1
    assert package_evaluations.after_publish(pid, SUBJECT, 'user-token', {'status': 'pending'}) is None


def test_an_unreachable_evaluation_service_is_retried_and_a_refusal_is_not(env):
    manager, pid, stub, client = env
    optimize(client, pid)
    publish_next(manager, pid)
    stub.writeback_failure = WorkspaceError('评测服务不可用，请稍后重试', 503, 'eval_unavailable')
    assert overview(client, pid)['writeback']['status'] == 'pending'
    stub.writeback_failure = None
    assert overview(client, pid)['writeback']['status'] == 'done' and len(stub.writebacks) == 2
    # A refused write-back for a later optimization is not sent again for the same release.
    stub.run['base_ref'] = {'ontology_id': PACKAGE, 'release_id': 'release_two', 'version': '0.1.2'}
    optimize(client, pid)
    publish_next(manager, pid, 'release_three', '0.1.3')
    stub.writeback_failure = WorkspaceError('这一轮的优化建议还未交给建模', 409, 'invalid_state')
    assert overview(client, pid)['writeback']['status'] == 'refused'
    overview(client, pid)
    assert len(stub.writebacks) == 3


def test_only_the_optimizing_user_writes_back(env):
    manager, pid, stub, client = env
    optimize(client, pid)
    publish_next(manager, pid)
    other = json.dumps(['https://example.com', 'operator_b'])
    state = package_evaluations.settle(pid, other, 'other-token')
    assert not stub.writebacks and state['result'] is None
    assert package_evaluations.writeback_view(pid, other)['mine'] is False


def test_nothing_is_written_back_without_a_new_release_or_an_optimization(env):
    manager, pid, stub, client = env
    overview(client, pid)
    optimize(client, pid)
    overview(client, pid)
    assert not stub.writebacks


def test_a_failure_is_reported_with_its_reason(env):
    _, pid, stub, client = env
    stub.listing['releases'] = [entry()]
    overview(client, pid)
    url = f'/api/v1/projects/{pid}/evaluations/{RUN}/modeling-result'
    assert client.post(url, json={'note': ''}).status_code == 400
    response = client.post(url, json={'note': '合成：建议需要的数据表不存在'})
    assert response.status_code == 200 and response.json()['optimization']['status'] == 'modeling_failed'
    assert stub.writebacks[-1]['outcome'] == 'failed' and stub.writebacks[-1]['note'] == '合成：建议需要的数据表不存在'
    assert index()['releases']['release_one']['optimization']['status'] == 'modeling_failed'
    stub.run['base_ref'] = {'ontology_id': 'pkg_other', 'release_id': 'release_x', 'version': '9.9.9'}
    assert client.post(url, json={'note': '合成'}).status_code == 404


def test_feedback_runs_are_kept_apart_from_evaluation_runs(env):
    _, pid, stub, client = env
    feedback = entry(run=OTHER_RUN, origin='consumer_feedback', summary={**SUMMARY, 'score': None},
                     optimization={'status': 'notified', 'updated_at': 1500.0})
    stub.listing['releases'] = [entry(), feedback, entry(release='release_x', origin='future_origin')]
    value = overview(client, pid)
    assert stub.calls[0][1].endswith('origin=all') or any('origin=all' in call[1] for call in stub.calls)
    assert value['releases']['release_one']['run_id'] == RUN and value['releases']['release_one']['origin'] == 'evaluation'
    assert value['feedback']['release_one']['run_id'] == OTHER_RUN
    assert 'release_x' not in value['releases'] and 'release_x' not in value['feedback']
    # A feedback notification goes to the feedback section and leaves the evaluation entry alone.
    assert notify(client, notification(sequence=3, run=OTHER_RUN, origin='consumer_feedback', finished=2000.0,
                                       optimization={'status': 'modeling_done', 'updated_at': 2100.0,
                                                     'result': {'outcome': 'done', 'release_id': 'release_two', 'version': '0.1.2'},
                                                     'history': []})).status_code == 200
    assert index()['feedback']['release_one']['optimization']['result']['version'] == '0.1.2'
    assert index()['releases']['release_one']['run_id'] == RUN


def test_data_solver_and_feedback_runs_of_one_release_share_the_index(env):
    """One release with all three kinds of runs: each lands in its own section and never replaces another."""
    _, pid, stub, client = env
    data_run = 'e' * 32
    feedback = entry(run=OTHER_RUN, origin='consumer_feedback', summary={**SUMMARY, 'score': None})
    solver = entry(run=RUN, run_type='solver', solver_summary=SOLVER_SUMMARY)
    stub.listing = {'ontology_id': PACKAGE, 'owner': OWNER, 'releases': [entry(run=data_run), feedback],
                    'solver_releases': [solver], 'active': None}
    synced = overview(client, pid)
    assert (synced['releases']['release_one']['run_id'], synced['feedback']['release_one']['run_id'],
            synced['solver_releases']['release_one']['run_id']) == (data_run, OTHER_RUN, RUN)
    passed = {**SOLVER_SUMMARY, 'passed': 1, 'failed': 0, 'first_failure_code': None, 'first_failure_message': None}
    assert notify(client, notification(sequence=2, run=RUN, finished=3000.0, run_type='solver',
                                       solver_summary=passed)).status_code == 200
    assert notify(client, notification(sequence=2, run=OTHER_RUN, finished=3000.0, origin='consumer_feedback',
                                       summary={**SUMMARY, 'score': None})).status_code == 200
    stored = index()
    assert stored['releases']['release_one']['run_id'] == data_run and stored['releases']['release_one']['finished_at'] == 1000.0
    assert stored['feedback']['release_one']['finished_at'] == 3000.0
    assert stored['solver_releases']['release_one']['solver_summary']['failed'] == 0
    # Regenerating a run updates its entry wherever it is indexed and leaves the other sections alone.
    assert client.post(f'/api/v1/projects/{pid}/evaluations/{RUN}/regenerate', json={}).status_code == 200
    after = index()
    assert after['solver_releases']['release_one']['artifacts']['status'] == 'generating'
    assert after['releases']['release_one']['artifacts']['status'] == 'ready'
    assert after['feedback']['release_one']['artifacts']['status'] == 'ready'


def test_notifications_with_new_optimization_statuses_are_accepted(env):
    _, _, _, client = env
    response = notify(client, notification(optimization={'status': 'future_status', 'history': [{'outcome': 'done'}]}))
    assert response.status_code == 200 and index()['releases']['release_one']['optimization']['status'] == 'future_status'


# ---- 权限与本地模式 ----


def test_evaluations_are_owner_only(env, monkeypatch):
    _, pid, _, client = env
    monkeypatch.setattr(auth, 'verify_access_token',
                        lambda token: {'sub': 'operator_b', 'iss': USER['iss']} if token == 'other-token' else dict(USER))
    other = {'Authorization': 'Bearer other-token'}
    assert client.get(f'/api/v1/projects/{pid}/evaluations', headers=other).status_code in (403, 404)
    assert client.get(f'/api/v1/projects/{pid}/evaluations/{RUN}/report', headers=other).status_code in (403, 404)
    assert client.post(f'/api/v1/projects/{pid}/evaluations/{RUN}/optimization', headers=other).status_code in (403, 404)
    assert client.get(f'/api/v1/projects/{pid}/legacy-evaluations', headers=other).status_code in (403, 404)


def test_local_mode_has_no_evaluation(env, monkeypatch):
    _, pid, stub, client = env
    monkeypatch.setattr(auth, 'ENABLED', False)
    # 与 develop 一样要统一账号登录；码不是 unauthenticated，前端不会当成登录过期
    for url in (f'/api/v1/projects/{pid}/evaluations?reconcile=1', f'/api/v1/projects/{pid}/evaluations/{RUN}/report'):
        response = client.get(url)
        assert response.status_code == 401 and response.json()['code'] == 'login_required'
        assert response.json()['message'] == '个人 Hub 包需要统一账号登录'
    assert stub.calls == []


# ---- 评测入口（ES-01） ----


@pytest.mark.parametrize('value, expected', [
    ('', ''),
    ('https://eval.example.com:8090', 'https://eval.example.com:8090/evaluation/'),
    ('https://eval.example.com/evaluation', 'https://eval.example.com/evaluation/'),
    ('https://eval.example.com/evaluation/', 'https://eval.example.com/evaluation/'),
])
def test_evaluation_url_is_normalized(value, expected):
    from app.services import evaluation_entry

    assert evaluation_entry.normalize(value) == expected


@pytest.mark.parametrize('value', [
    '/evaluation', '/evaluation/', '/other/', 'sftp://eval.example.com/evaluation/', 'https://eval.example.com/other/',
    'https://user@eval.example.com/evaluation/', 'https://eval.example.com/evaluation/?token=x',
    'https://eval.example.com/evaluation/#token',
])
def test_evaluation_url_rejects_unsafe_values(value):
    from app.services import evaluation_entry

    with pytest.raises(ValueError):
        evaluation_entry.normalize(value)


def test_evaluation_entry_comes_only_from_deployment(env, monkeypatch):
    _, _, _, client = env
    monkeypatch.setattr(config, 'EVAL_PUBLIC_URL', '')
    assert client.get('/api/v1/evaluation/entry').json() == {'url': ''}
    monkeypatch.setattr(config, 'EVAL_PUBLIC_URL', 'https://eval.example.com:8090')
    assert client.get('/api/v1/evaluation/entry').json() == {'url': 'https://eval.example.com:8090/evaluation/'}
    monkeypatch.setattr(config, 'EVAL_PUBLIC_URL', 'javascript:alert(1)')
    assert client.get('/api/v1/evaluation/entry').status_code == 500
    # 地址只由部署配置决定，没有页面修改入口
    assert client.get('/api/v1/evaluation/entry', headers={'Authorization': ''}).status_code == 401
    writable = {(route.path, method) for route in app.routes for method in getattr(route, 'methods', ())
                if 'evaluation/entry' in getattr(route, 'path', '') and method != 'GET'}
    assert writable == set()


# ---- 旧版评测记录（只读，ES-09～ES-12） ----

LEGACY = 'eval-20261001-090000'
LEGACY_REPORT = '\n'.join([
    '# 数据评测报告 — 2026-10-01 09:00', '', '## 逐用例明细', '',
    '### 1. ✅ pass', '', '- **问题**：合成旧问题一', '', '- **标准答案**：', '', '  > 旧答案一', '',
    '### 2. ❌ fail', '', '- **问题**：合成旧问题二', '', '- **标准答案**：', '', '  > 旧答案二', '',
    '- **智能体回答**：', '', '  > 合成旧回答二', '  >', f'  > - **完整回答**：[按结果 ID 查看附件](reports/{LEGACY}.answers.json#r-2)', '',
])
LEGACY_STATE = {
    'status': 'done', 'phase': 'evaluate', 'message': '评测完成（有效 2/2 条）', 'report': f'reports/{LEGACY}.md',
    'report_round': 'round-1', 'round_id': 'round-1', 'data_source_name': '合成数据源', 'eval_set_name': '合成评测集',
    'summary': {'total': 2, 'pass': 1, 'partial': 0, 'fail': 1, 'error': 0, 'score': 50, 'valid': 2, 'result_status': 'complete'},
    'cases': [{'question': '合成旧问题一', 'verdict': 'pass', 'result_id': 'r-1'},
              {'question': '合成旧问题二', 'verdict': 'fail', 'result_id': 'r-2', 'answer': '合成旧回答二', 'is_truncated': True,
               'original_length': 900}],
}


def legacy_project(manager, pid, state=None):
    root = manager.project_root(pid)
    reports = root / 'reports'
    reports.mkdir(exist_ok=True)
    (reports / f'{LEGACY}.md').write_text(LEGACY_REPORT, encoding='utf-8')
    (reports / f'{LEGACY}.answers.json').write_text(json.dumps({'answers': [
        {'resultId': 'r-1', 'question': '合成旧问题一', 'finalAnswer': '旧回答一', 'verdict': 'pass', 'reason': '一致'},
        {'resultId': 'r-2', 'question': '合成旧问题二', 'finalAnswer': '乙' * 900, 'verdict': 'fail', 'reason': '合成旧理由'},
    ]}, ensure_ascii=False), encoding='utf-8')
    _git(root, 'add', 'reports')
    _git(root, 'commit', '-qm', 'chore: 合成旧版评测报告')
    (root / '.ontology-state').mkdir(exist_ok=True)
    (root / '.ontology-state' / 'eval.json').write_text(json.dumps(state or LEGACY_STATE, ensure_ascii=False), encoding='utf-8')
    return root


def test_legacy_record_shows_the_last_run_like_develop(env):
    manager, pid, stub, client = env
    base = f'/api/v1/projects/{pid}/legacy-evaluations'
    assert client.get(base).json() == {'state': {}, 'evolution': None, 'evolution_enabled': True}
    legacy_project(manager, pid)
    value = client.get(base).json()
    assert value['state']['status'] == 'done' and value['state']['cases'][1]['result_id'] == 'r-2'
    assert value['evolution'] == {'report': f'reports/{LEGACY}.md', 'score': 50, 'errors': 0, 'fails': 1, 'round_failed': False}
    report = client.get(f'{base}/{LEGACY}').json()
    assert report['content'].startswith('# 数据评测报告') and report['path'] == f'reports/{LEGACY}.md'
    answer = client.get(f'{base}/{LEGACY}/answers/r-2').json()
    assert answer == {'resultId': 'r-2', 'question': '合成旧问题二', 'fullAnswer': '乙' * 900, 'originalLength': 900,
                      'truncated': False}
    assert client.get(f'{base}/{LEGACY}/answers/r-9').json()['code'] == 'answer_not_found'
    for stem in (STEM, 'eval-notes', '..%2Fsecret', f'{LEGACY}.answers'):
        assert client.get(f'{base}/{stem}').status_code == 404
    assert stub.calls == []  # 只读工作区，不找评测服务


def test_legacy_running_state_shows_as_interrupted_without_rewriting(env):
    manager, pid, _, client = env
    root = legacy_project(manager, pid, {**LEGACY_STATE, 'status': 'running', 'message': '执行中'})
    state = client.get(f'/api/v1/projects/{pid}/legacy-evaluations').json()['state']
    assert state['status'] == 'interrupted' and state['message'] == '上一轮评测被服务重启中断' and state['resumable'] is False
    assert json.loads((root / '.ontology-state' / 'eval.json').read_text(encoding='utf-8'))['status'] == 'running'


def test_legacy_report_optimization_is_a_feedback_batch(env):
    manager, pid, _, client = env
    root = legacy_project(manager, pid)
    url = f'/api/v1/projects/{pid}/legacy-evaluations/{LEGACY}/optimization'
    value = client.post(url, json={}).json()
    assert value == {'feedback_id': f'legacy-{LEGACY}', 'report': f'reports/{LEGACY}.md', 'suggestions': None,
                     'command': '/ontology-evolution', 'args': {'feedback_id': f'legacy-{LEGACY}'}}
    batch = json.loads((root / feedback_inbox.batch_path(root, f'legacy-{LEGACY}')).read_text(encoding='utf-8'))
    assert [item['case_id'] for item in batch['items']] == ['r-2']
    assert batch['items'][0]['expected'] == '旧答案二' and batch['items'][0]['loss_reason'] == '合成旧理由'
    assert batch['summary']['score'] == 50 and batch['source'] == {'service': '评测服务', 'run': 'round-1'}
    assert batch['attachments'] == {'report': f'reports/{LEGACY}.md', 'answers': f'reports/{LEGACY}.answers.json'}
    head = _git(root, 'rev-parse', 'HEAD')
    assert client.post(url, json={}).json() == value and _git(root, 'rev-parse', 'HEAD') == head
    assert client.post(f'/api/v1/projects/{pid}/packs/evo/disable', json={}).status_code == 200
    assert client.post(url, json={}).json()['code'] == 'pack_not_enabled'


def test_legacy_records_do_not_follow_links(env, tmp_path):
    manager, pid, _, client = env
    root = manager.project_root(pid)
    outside = tmp_path / 'outside.json'
    outside.write_text(json.dumps(LEGACY_STATE), encoding='utf-8')
    (root / '.ontology-state').mkdir(exist_ok=True)
    (root / '.ontology-state' / 'eval.json').symlink_to(outside)
    (root / 'reports').mkdir(exist_ok=True)
    (root / 'reports' / f'{LEGACY}.md').symlink_to(outside)
    base = f'/api/v1/projects/{pid}/legacy-evaluations'
    assert client.get(base).json()['state'] == {}
    assert client.get(f'{base}/{LEGACY}').status_code == 404
