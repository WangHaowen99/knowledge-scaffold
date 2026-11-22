"""Knowledge package data flywheel: evaluation results, assistant optimization, checks and Hub release.

The evaluation service, the assistant session and Hub are synthetic in-process stubs; every identifier,
document and score is synthetic.
"""
from datetime import datetime, timedelta, timezone
import json
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import config
from app.main import app
from app.api import deps
from app.routers import flywheel as flywheel_routes
from app.services import auth, eval_client, hub_packages, knowledge_hub, package_evaluations, solver_contract
from app.services.agent_sessions import SessionError
from app.services.delegated import Delegated
from app.services.flywheel import assistant as assistant_module, checks, grants, intake, optimize, publish, store
from app.services.flywheel.assistant import Assistant
from app.services.solver import service as solver_service
from app.services.workspace import WorkspaceError, WorkspaceManager, _commit_paths, _git

SERVICE_TOKEN = 'synthetic-evaluation-service-token-000000'
USER = {'sub': 'operator_a', 'iss': 'https://example.com'}
SUBJECT = json.dumps([USER['iss'], USER['sub']], ensure_ascii=False)
PACKAGE = 'pkg_demo'
OWNER = 'a' * 64
RUN, NEXT_RUN, OLD_RUN, THIRD_RUN = 'c' * 32, 'd' * 32, 'e' * 32, 'f' * 32
FEEDBACK_RUN = '9' * 32
STEMS = {RUN: 'eval-20261031-101010-abc123', NEXT_RUN: 'eval-20261031-111111-abc124',
         OLD_RUN: 'eval-20261030-090909-abc125', THIRD_RUN: 'eval-20261031-121212-abc126',
         FEEDBACK_RUN: 'eval-20261101-080808-abc127'}
SUMMARY = {'total': 4, 'pass': 2, 'partial': 1, 'fail': 1, 'error': 0, 'manual': 0, 'valid': 4,
           'score': 62, 'result_status': 'complete'}
SUGGESTIONS = '\n'.join([
    '# 评测优化建议', '', '## 失分归因与修订建议', '',
    '| # | 用例 | 归因 | 依据 | 建议修订 | 落点 |', '|---|---|---|---|---|---|',
    '| 1 | 1 | A 知识缺失 | 合成依据一 | 合成建议一 | ontology/object/object_a.md |',
    '| 2 | 2、3 | C 数据映射 | 合成依据二 | 合成建议二 | ontology/object/object_a.md |',
    '| 3 | 4 | A 知识缺失 | 合成依据三 | 合成建议三 | ontology/rule/rule_b.md |', ''])
OBJECT = '# 对象 A\n\n## 对象描述\n\n合成对象，仅用于测试。\n'


def artifacts(run_id, revision=1, **changes):
    return {'revision': revision, 'status': 'ready', 'stem': STEMS[run_id], 'generated_at': 1000.0,
            'report': 'ready', 'suggestions': 'ready', 'suggestions_error': None, 'suggestions_model': 'model_demo',
            **changes}


class EvaluationStub:
    """Answers the evaluation API paths modeling uses, for several runs."""

    def __init__(self):
        self.calls = []
        self.runs, self.statuses = {}, {}
        self.listing = {'ontology_id': PACKAGE, 'owner': OWNER, 'active': None, 'releases': []}
        self.issued, self.revoked, self.refusal, self.lifetimes = 0, [], None, []
        self.revoke_failure, self.rerun_failure = None, None
        self.refreshes, self.renewals, self.reruns, self.logins = [], [], [], set()
        self.writebacks, self.writeback_failure, self.listing_failure = [], None, None
        # 登记成功了，答复却没送到建模（超时、断连）
        self.answer_lost = False
        # 登记得到 2xx，内容却不是 JSON（例如地址指向了别的服务器）
        self.garbled = False

    def complete(self, run_id, release='release_one', version='0.1.1', finished=1000.0, score=62, cases=None,
                 origin='evaluation', optimization=None, **changes):
        self.runs[run_id] = {'id': run_id, 'status': 'completed', 'artifacts': artifacts(run_id, **changes),
                             'origin': origin, 'base_ref': {'ontology_id': PACKAGE, 'release_id': release, 'version': version}}
        entry = {'release_id': release, 'version': version, 'run_id': run_id, 'status': 'completed',
                 'created_at': finished - 100, 'finished_at': finished, 'artifacts': artifacts(run_id, **changes),
                 'summary': {**SUMMARY, 'score': score, **(cases or {})}, 'origin': origin, 'optimization': optimization}
        self.listing['releases'] = [item for item in self.listing['releases']
                                    if (item['release_id'], item.get('origin')) != (release, origin)] + [entry]
        return entry

    def feedback(self, release='release_one', version='0.1.1', status='notified', finished=1500.0, **changes):
        """A consumer feedback run a package owner confirmed for modeling (``notified``)."""
        return self.complete(FEEDBACK_RUN, release, version, finished, score=None, origin='consumer_feedback',
                             optimization={'status': status, 'updated_at': finished}, **changes)

    def files(self, run_id):
        return {
            'report.md': '# 数据评测报告\n\n- **问题**：合成问题一\n'.encode(),
            'answers.json': json.dumps({'answers': []}).encode(),
            'suggestions.md': SUGGESTIONS.encode(),
            'context.json': json.dumps({'run_id': run_id, 'tables': {}}).encode(),
        }

    def __call__(self, method, path, token, body=None, limit=eval_client.JSON_LIMIT, service=False, refresh='',
                 delegation=''):
        self.calls.append((method, path, token))
        if path == '/delegations':
            if self.refusal:
                raise self.refusal
            self.issued += 1
            self.lifetimes.append(body['expires_in'])
            self.refreshes.append(refresh)
            if refresh:
                self.logins.add(body['delegation'])
            if self.answer_lost:
                raise WorkspaceError('无法连接评测服务', 503, 'eval_unavailable')
            if self.garbled:
                return b'<html>synthetic page</html>', {}
            return json.dumps({'delegation': body['delegation'], 'login': bool(refresh),
                               'expires_at': int(time.time()) + body['expires_in']}).encode(), {}
        if path == '/delegations/current/renewal':
            self.renewals.append((delegation, body['expires_in']))
            return json.dumps({'delegation': delegation, 'login': delegation in self.logins,
                               'expires_at': int(time.time()) + body['expires_in']}).encode(), {}
        if path.endswith('/reruns'):
            if self.rerun_failure:
                raise self.rerun_failure
            handle = token.handle if isinstance(token, Delegated) else None
            if handle is not None and handle not in self.logins:
                raise WorkspaceError('后台评测授权未登记', 401, 'login_missing')
            self.reruns.append({'source': path.split('/')[2], 'token': token, 'refresh': refresh, **body})
            return json.dumps({'id': f'{len(self.reruns):032x}', 'status': 'queued'}).encode(), {}
        if path == '/delegations/current':
            if self.revoke_failure:
                raise self.revoke_failure
            self.revoked.append(token.handle)
            return b'{"revoked": true}', {}
        if path.startswith('/package-evaluations?'):
            if self.listing_failure:
                raise self.listing_failure
            return json.dumps(self.listing).encode(), {}
        parts = path.strip('/').split('/')
        run = self.runs.get(parts[1]) if len(parts) > 1 and parts[0] == 'runs' else None
        if run and len(parts) == 2:
            return json.dumps(run).encode(), {}
        if run and parts[2] == 'modeling-result':
            self.writebacks.append({'run_id': run['id'], 'token': token, **body})
            if self.writeback_failure:
                raise self.writeback_failure
            result = {'outcome': body['outcome'], 'release_id': body.get('release_id') or None,
                      'version': '0.1.1' if body.get('release_id') else None, 'note': body.get('note'), 'reported_at': 2000.0}
            optimization = {'status': 'modeling_failed' if body['outcome'] == 'failed' else 'modeling_done',
                            'updated_at': 2000.0, 'result': result, 'history': []}
            return json.dumps({'run_id': run['id'], 'optimization': optimization}).encode(), {}
        if len(parts) == 2 and parts[0] == 'runs' and parts[1] in self.statuses:
            return json.dumps({'id': parts[1], 'status': self.statuses[parts[1]]}).encode(), {}
        if run and parts[2] == 'artifacts' and parts[3] in self.files(run['id']):
            headers = {'x-artifact-revision': str(run['artifacts']['revision']), 'x-artifact-stem': run['artifacts']['stem']}
            return self.files(run['id'])[parts[3]], headers
        raise WorkspaceError('评测记录不存在或无权访问', 404, 'eval_not_found')


class FakeAssistant:
    """The project assistant session: records commands; the test decides when the turn ends."""

    def __init__(self):
        self.sent, self.interrupted = [], []
        self.running, self.result = False, 'finished'
        # 飞轮发出的那一轮是否已结束；running 而 ended 表示助手忙的是别人的对话
        self.ended = False

    def busy(self, pid):
        return self.running

    def turn_ended(self, pid, conversation, start):
        return self.ended

    def start(self, pid, cwd, text, username):
        self.sent.append((text, username))
        self.running = True
        return {'conversation_id': 'conversation_demo', 'history_start': 3}

    def outcome(self, pid, conversation, start):
        return 'running' if self.running else self.result

    def finish(self, result='finished'):
        self.running, self.result = False, result

    def interrupt(self, pid, conversation):
        self.interrupted.append(conversation)
        self.finish('interrupted')


class HubStub:
    """In-memory Hub publication protocol and publication registrations; failures are injected once per call.

    Delegated publication calls arrive through hub_packages.delegated_call, which the fixture points at
    delegated(): like Hub, it accepts them only while their registration exists and has not run out.
    """

    def __init__(self, folder):
        self.folder, self.requests, self.calls, self.fail = folder, {}, [], None
        self.registered, self.registrations, self.ids = {}, [], []
        self.revoked, self.refusal, self.tokens, self.lifetimes = [], None, [], []
        self.revoke_failure = None

    @property
    def issued(self):
        return len(self.ids)

    def delegated(self, method, path, token_id, ontology, issuer, sub, **kwargs):
        record = self.registered.get(hub_packages.delegation_id(token_id))
        if not record or record['expires_at'] <= time.time() or record['ontology_id'] != ontology:
            raise WorkspaceError('知识包发布授权已失效', 401, 'delegation_revoked')
        return self(method, path, ('delegation', token_id, issuer, sub), **kwargs)

    def __call__(self, method, path, token='', body=None, upload=None, headers=None, download=None, upload_name=None):
        if path == '/api/package-delegations':
            if self.refusal:
                raise self.refusal
            self.registrations.append((token, body))
            if body['delegation_id'] not in self.registered:
                self.ids.append(body['delegation_id'])
            self.lifetimes.append(round(body['expires_at'] - time.time()))
            self.registered[body['delegation_id']] = body
            return {**body, 'active': True}
        if path.startswith('/api/package-delegations/') and method == 'DELETE':
            if self.revoke_failure:
                raise self.revoke_failure
            delegation_id = path.rsplit('/', 1)[1]
            self.revoked.append(delegation_id)
            self.registered.pop(delegation_id, None)
            return {'delegation_id': delegation_id, 'revoked': True}
        self.calls.append((method, path))
        self.tokens.append(token)
        if self.fail:
            failure, self.fail = self.fail, None
            raise failure
        parts = path.strip('/').split('/')
        if method == 'GET' and parts[:2] == ['api', 'package-publish-requests'] and len(parts) == 3:
            if parts[2] not in self.requests:
                raise WorkspaceError('Hub 包不存在或当前用户无权访问', 404, 'not_found')
            return dict(self.requests[parts[2]])
        if path == '/api/package-publish-requests':
            if body['request_id'] not in self.requests:
                number = len(self.requests) + 2
                self.requests[body['request_id']] = dict(body, status='pending', release_id=f'release_{number}',
                                                         version=f'0.1.{number}')
            return dict(self.requests[body['request_id']])
        row = self.requests[parts[-2]]
        if parts[-1] == 'content':
            row.update(status='uploaded', package_digest=headers['X-Package-Digest'], size=1)
        elif parts[-1] == 'commit':
            committed = datetime(2026, 11, 1, tzinfo=timezone.utc) + timedelta(minutes=len(self.calls))
            row.update(status='committed', committed_at=committed.isoformat())
        return dict(row)


@pytest.fixture
def env(tmp_path, monkeypatch):
    manager = WorkspaceManager(tmp_path / 'modeling', config.ASSETS_DIR)
    monkeypatch.setattr(config, 'DATA_DIR', tmp_path / 'modeling')
    monkeypatch.setattr(config, 'EVAL_URL', 'http://127.0.0.1:8090')
    monkeypatch.setattr(deps, 'workspaces', manager)
    monkeypatch.setattr(auth, 'ENABLED', True)
    claims = {**USER, 'exp': time.time() + 3600}
    monkeypatch.setattr(auth, 'verify_access_token', lambda token: dict(claims) if token == 'user-token' else None)
    monkeypatch.setattr(auth, 'current_user', lambda token, verified: {
        'sub': verified['sub'], 'role': verified.get('role', 'user'),
        'password_change_required': False,
    })
    monkeypatch.setenv('ONTO_EVALUATION_SERVICE_TOKEN', SERVICE_TOKEN)
    evaluation = EvaluationStub()
    monkeypatch.setattr(eval_client, 'request', evaluation)
    hub = HubStub(tmp_path / 'hub')
    monkeypatch.setattr(hub_packages, 'call', hub)
    monkeypatch.setattr(hub_packages, 'delegated_call', hub.delegated)
    # 项目对应的 Hub 包编号是固定的：发布不会换包，否则委托会因“换了包”重新登记
    monkeypatch.setattr(hub_packages, 'ontology_id', lambda pid: PACKAGE)
    # 评测服务的句柄由建模生成：测试里按生成顺序编号，便于断言
    handles = iter(f'evaluation-handle-{number}' for number in range(1, 1000))
    new_key = grants._new_key
    monkeypatch.setattr(grants, '_new_key',
                        lambda service: next(handles) if service == 'evaluation' else new_key(service))
    assistant = FakeAssistant()
    monkeypatch.setattr(flywheel_routes, 'Assistant', lambda loop: assistant)
    scripts = {'ontology_checker.py': [], 'check_eval_generalization.py': []}
    monkeypatch.setattr(checks, '_script', lambda root, name, *args: scripts.get(name))
    pid = manager.create_project('project_alpha', owner='operator_a')['id']
    root = manager.project_root(pid)
    (root / 'ontology/object/object_a.md').write_text(OBJECT, encoding='utf-8')
    (root / 'ontology/operation/operation_a.md').write_text('# 操作 A\n\n合成操作。\n', encoding='utf-8')
    # 发布正式版本要求页面上的必需阶段都有产出：资料分析与质量检查各放一份合成文件
    for rel in ('research/report.md', 'reports/validation.md'):
        (root / rel).parent.mkdir(exist_ok=True)
        (root / rel).write_text('# 合成产出\n', encoding='utf-8')
    _git(root, 'add', '-A')
    _git(root, 'commit', '-m', 'synthetic content')
    hub_packages.record(manager, pid, SUBJECT, {'ontology_id': PACKAGE, 'release_id': 'release_one',
                                               'package_digest': 'b' * 64, 'version': '0.1.1',
                                               'committed_at': '2026-10-31T00:00:00+00:00'})
    client = TestClient(app)
    client.headers['Authorization'] = 'Bearer user-token'
    return dict(manager=manager, pid=pid, root=root, evaluation=evaluation, hub=hub, assistant=assistant,
                scripts=scripts, client=client)


def sync(env, reconcile=True):
    response = env['client'].get(f"/api/v1/projects/{env['pid']}/flywheel", params={'reconcile': reconcile})
    assert response.status_code == 200, response.text
    return response.json()


def act(env, run_id, action, **body):
    return env['client'].post(f"/api/v1/projects/{env['pid']}/flywheel/runs/{run_id}/{action}", json=body or None)


def settings(env, **changes):
    # 前端修改设置时总会转交刷新令牌，供评测服务登记后台评测登录
    response = env['client'].put(f"/api/v1/projects/{env['pid']}/flywheel/settings", json=changes,
                                 headers={'X-Refresh-Token': 'synthetic-refresh'})
    assert response.status_code == 200, response.text
    return response.json()


def run_of(view, run_id):
    return next(run for run in view['runs'] if run['id'] == run_id)


def optimized(env, change=True):
    """Confirm the waiting run, let the synthetic assistant revise a document, and settle the turn."""
    response = act(env, 'FW-001', 'optimize')
    assert response.status_code == 200, response.text
    if change:
        (env['root'] / 'ontology/object/object_a.md').write_text(OBJECT + '\n- 合成字段：空值表示未知\n', encoding='utf-8')
    env['assistant'].finish()
    return sync(env, reconcile=False)


def published(env, run_id='FW-001'):
    assert act(env, run_id, 'publish').status_code == 200
    publish.wait(env['pid'], SUBJECT, run_id, timeout=30)
    return sync(env, reconcile=False)


def test_parse_suggestions_counts_rows_and_names_categories():
    assert intake.parse_suggestions(SUGGESTIONS) == {'count': 3, 'categories': ['知识缺失', '数据映射']}
    assert intake.parse_suggestions('# 评测优化建议\n\n本轮评测没有失败的用例，无需评测优化。\n') == {
        'count': 0, 'categories': []}


def test_first_sync_takes_the_current_version_and_skips_history(env):
    env['evaluation'].complete(OLD_RUN, release='release_zero', version='0.1.0', finished=500.0)
    env['evaluation'].complete(RUN)
    view = sync(env)
    assert view['available'] is True and view['package']['version'] == '0.1.1'
    assert [run['id'] for run in view['runs']] == ['FW-001'] and view['pending'] == 1
    run = view['runs'][0]
    assert run['status'] == 'waiting_optimize' and run['reason'] == 'automation_off' and run['human_hold'] is True
    # 评测得分是严格通过率：4 个用例通过 2 个，不是评测服务按部分通过加权的 62 分
    assert run['eval_run_id'] == RUN and run['score'] == 50
    assert run['suggestions'] == {'count': 3, 'categories': ['知识缺失', '数据映射']}
    # 重复投递与重复同步不建立新的运行
    assert [item['id'] for item in sync(env)['runs']] == ['FW-001']
    assert all(token == 'user-token' for _, _, token in env['evaluation'].calls)


def test_later_results_of_other_versions_and_failed_suggestions_are_blocked(env):
    env['evaluation'].complete(RUN)
    sync(env)
    env['evaluation'].complete(OLD_RUN, release='release_zero', version='0.1.0', finished=2000.0)
    env['evaluation'].complete(NEXT_RUN, finished=3000.0, suggestions='failed', suggestions_error='判分模型未配置')
    view = sync(env)
    stale, failed = run_of(view, 'FW-002'), run_of(view, 'FW-003')
    # 旧版本的评测通知无事可做：直接关闭为已过期，不算待处理
    assert stale['status'] == 'expired' and stale['reason'] == 'version_mismatch'
    assert view['pending'] == 2
    assert failed['status'] == 'blocked' and failed['reason'] == 'suggestions_failed'
    assert failed['suggestions_error'] == '判分模型未配置'
    # 重新生成的修订到达：同一运行转为待确认，不新建运行
    env['evaluation'].complete(NEXT_RUN, finished=3000.0, revision=2)
    view = sync(env)
    regenerated = run_of(view, 'FW-003')
    assert regenerated['status'] == 'waiting_optimize' and regenerated['human_hold'] is True
    assert regenerated['revision'] == 2 and view['total'] == 3


def test_notifications_reach_the_flywheel_without_reconciling(env):
    sync(env)  # 首次同步对账，记住当前用户在评测服务的所有者标识
    entry = env['evaluation'].complete(RUN)
    body = {**entry, 'sequence': 1, 'owner': OWNER, 'ontology_id': PACKAGE, 'event_id': f'{RUN}:1'}
    headers = {'X-Evaluation-Service-Token': SERVICE_TOKEN, 'Authorization': ''}
    assert env['client'].post('/api/internal/evaluation/notifications', json=body, headers=headers).status_code == 200
    view = sync(env, reconcile=False)
    assert [run['eval_run_id'] for run in view['runs']] == [RUN]


def test_settings_changes_are_recorded_and_validated(env):
    view = settings(env, optimize=True, max_rounds=5)
    assert view['settings'] == {'optimize': True, 'publish': False, 'max_rounds': 5, 'delegation_days': 30}
    assert [item['action'] for item in view['audit']] == ['后台授权', '自动优化规则', '自动化处理优化建议']
    assert view['audit'][2]['description'].startswith('关闭 → 开启') and view['audit'][0]['actor'] == 'operator_a'
    response = env['client'].put(f"/api/v1/projects/{env['pid']}/flywheel/settings", json={'max_rounds': 4})
    assert response.status_code == 400 and response.json()['code'] == 'invalid_argument'
    assert response.json()['details'] == {'fields': {'max_rounds': 'one_of'}}


def test_confirmed_optimization_imports_snapshots_and_hands_over_to_the_assistant(env):
    env['evaluation'].complete(RUN)
    sync(env)
    head = _git(env['root'], 'rev-parse', 'HEAD').strip()
    assert act(env, 'FW-001', 'optimize').status_code == 200
    view = sync(env, reconcile=False)
    run = run_of(view, 'FW-001')
    assert run['status'] == 'optimizing' and run['mode'] == 'manual' and run['step'] == 2
    assert view['active_id'] == 'FW-001'
    report = f'{STEMS[RUN]}.md'
    assert env['assistant'].sent == [(f'/ontology-evolution {report} --flywheel', 'operator_a')]
    assert (env['root'] / 'reports' / report).is_file()
    # 快照是导入报告之后、助手修订之前的迭代
    assert run['snapshot']['commit'] != head and run['snapshot']['iteration'] >= 3
    # 优化进行中不能再确认另一个运行，也不能发布
    assert act(env, 'FW-001', 'publish').status_code == 409


def test_finished_optimization_becomes_a_candidate_and_publishes(env):
    env['evaluation'].complete(RUN)
    sync(env)
    view = optimized(env)
    run = run_of(view, 'FW-001')
    assert run['status'] == 'waiting_publish' and run['publish_hold'] is True and view['candidate_id'] == 'FW-001'
    detail = env['client'].get(f"/api/v1/projects/{env['pid']}/flywheel/runs/FW-001").json()
    assert detail['checks']['passed'] is True and detail['checks']['changed'] == ['ontology/object/object_a.md']
    assert {item['key'] for item in detail['checks']['items']} == {
        'content', 'rules', 'generalization', 'binding', 'solver', 'readiness'}
    changes = env['client'].get(f"/api/v1/projects/{env['pid']}/flywheel/runs/FW-001/changes").json()
    assert [item['path'] for item in changes['files']] == ['ontology/object/object_a.md']
    assert {'kind': 'added', 'text': '- 合成字段：空值表示未知'} in changes['files'][0]['lines']
    view = published(env)
    run = run_of(view, 'FW-001')
    # 开关关闭：发布后等待确认评测，不会自己开始
    assert run['status'] == 'waiting_evaluation' and run['human_hold'] is True and run['target_version'] == '0.1.2'
    assert hub_packages.association(env['pid'])['release_id'] == run['published_release_id'] == 'release_2'
    assert view['package']['version'] == '0.1.2' and view['candidate_id'] is None and env['evaluation'].reruns == []
    # 用户在评测页面评测了新版本：结果关联到发布它的运行，并作为新的运行继续
    env['evaluation'].complete(NEXT_RUN, release='release_2', version='0.1.2', finished=5000.0,
                               cases={'pass': 3, 'partial': 0, 'fail': 1})
    view = sync(env)
    published_run, result = run_of(view, 'FW-001'), run_of(view, 'FW-002')
    assert published_run['status'] == 'published' and published_run['after_score'] == 75
    assert published_run['evaluation']['status'] == 'done' and published_run['evaluation']['passed'] is False
    assert result['status'] == 'waiting_optimize' and result['source_id'] == 'FW-001' and result['trigger'] == 'evaluation_page'


def test_failed_checks_and_unchanged_content_block_publication(env):
    env['evaluation'].complete(RUN)
    sync(env)
    env['scripts']['ontology_checker.py'] = [{'severity': '错误', 'file': 'object_a.md', 'message': '合成错误'}]
    view = optimized(env)
    run = run_of(view, 'FW-001')
    assert run['status'] == 'blocked' and run['reason'] == 'checks_failed'
    assert act(env, 'FW-001', 'publish').status_code == 409
    env['scripts']['ontology_checker.py'] = []
    assert act(env, 'FW-001', 'recheck').status_code == 200
    run = run_of(sync(env, reconcile=False), 'FW-001')
    assert run['status'] == 'waiting_publish' and run['publish_hold'] is True


def test_checks_that_cannot_finish_leave_the_run_ready_for_a_recheck(env, monkeypatch):
    env['evaluation'].complete(RUN)
    sync(env)
    evaluate = checks.evaluate

    def broken(*args):
        raise WorkspaceError('合成的检查失败', 500)

    monkeypatch.setattr(checks, 'evaluate', broken)
    run = run_of(optimized(env), 'FW-001')
    assert run['status'] == 'blocked' and run['reason'] == 'checks_failed' and run['error'] == '合成的检查失败'
    # 不再是优化中：之后的同步不会反复重跑检查
    assert run_of(sync(env, reconcile=False), 'FW-001')['status'] == 'blocked'
    monkeypatch.setattr(checks, 'evaluate', evaluate)
    assert act(env, 'FW-001', 'recheck').status_code == 200
    assert run_of(sync(env, reconcile=False), 'FW-001')['status'] == 'waiting_publish'


def test_blocked_runs_of_an_older_version_expire_after_another_publication(env):
    env['evaluation'].complete(RUN)
    sync(env)
    env['scripts']['ontology_checker.py'] = [{'severity': '错误', 'file': 'object_a.md', 'message': '合成错误'}]
    optimized(env)
    # 网页上另行发布了新版本：旧版本上的阻断不再满足当前基线
    hub_packages.publish(env['manager'], env['pid'], 'request_web', 'user-token', SUBJECT, True)
    run = run_of(sync(env, reconcile=False), 'FW-001')
    assert run['status'] == 'expired' and run['reason'] == 'version_mismatch'
    assert act(env, 'FW-001', 'recheck').status_code == 409


def test_optimization_without_changes_is_not_a_candidate(env):
    env['evaluation'].complete(RUN)
    sync(env)
    run = run_of(optimized(env, change=False), 'FW-001')
    assert run['status'] == 'blocked' and run['reason'] == 'no_changes'
    assert act(env, 'FW-001', 'ignore').status_code == 200
    assert run_of(sync(env, reconcile=False), 'FW-001')['status'] == 'ignored'


def test_content_changed_after_the_checks_is_not_published(env):
    env['evaluation'].complete(RUN)
    sync(env)
    optimized(env)
    (env['root'] / 'ontology/object/object_a.md').write_text(OBJECT + '\n- 检查之后的合成修改\n', encoding='utf-8')
    assert act(env, 'FW-001', 'publish').status_code == 200
    run = run_of(sync(env, reconcile=False), 'FW-001')
    assert run['status'] == 'blocked' and run['reason'] == 'content_changed'
    assert env['hub'].calls == []
    assert act(env, 'FW-001', 'recheck').status_code == 200
    assert run_of(sync(env, reconcile=False), 'FW-001')['status'] == 'waiting_publish'
    assert run_of(published(env), 'FW-001')['status'] == 'waiting_evaluation'


def test_hub_failure_keeps_the_frozen_request_for_the_retry(env):
    env['evaluation'].complete(RUN)
    sync(env)
    optimized(env)
    env['hub'].fail = WorkspaceError('知识包 Hub 不可用，请稍后重试', 503, 'hub_unavailable')
    view = published(env)
    run = run_of(view, 'FW-001')
    assert run['status'] == 'blocked' and run['reason'] == 'hub_failed'
    # 冻结后对工作区的修改不影响重试：沿用同一发布请求和已冻结的内容
    (env['root'] / 'ontology/object/object_a.md').write_text(OBJECT + '\n- 冻结之后的合成修改\n', encoding='utf-8')
    view = published(env)
    assert run_of(view, 'FW-001')['status'] == 'waiting_evaluation'
    assert len(env['hub'].requests) == 1


SOLVER_REVISION = 'sha256:' + '1' * 64


def solver_enabled(env, monkeypatch, verified):
    """An enabled solver contract whose revision is ``revision['value']``; ``verified`` holds the revisions whose
    latest solver validation passed."""
    revision = {'value': SOLVER_REVISION}
    contract = SimpleNamespace(solver_key='solver_a', operation='operation_a', decision_model='model_a', enabled=True)
    monkeypatch.setattr(solver_service, 'solver_check_at',
                        lambda root: solver_contract.SolverCheck(contracts=[contract], revision=revision['value']))
    monkeypatch.setattr(package_evaluations, 'verified_solver_release', lambda pid, subject, value: (
        {'release_id': 'release_one', 'version': '0.1.1'} if value in verified else None))
    return revision


def test_an_unverified_solver_revision_is_published_only_once_it_is_verified(env, monkeypatch):
    verified = set()
    solver_enabled(env, monkeypatch, verified)
    env['evaluation'].complete(RUN)
    sync(env)
    assert run_of(optimized(env), 'FW-001')['status'] == 'waiting_publish'
    # 飞轮不代用户确认求解能力尚未验证：不冻结内容，也不在 Hub 建发布请求
    run = run_of(published(env), 'FW-001')
    assert run['status'] == 'blocked' and run['reason'] == 'solver_unverified'
    assert env['hub'].requests == {}
    assert 'publish_request_id' not in store.find(store.load(env['pid'], SUBJECT), 'FW-001')
    # 同一求解契约修订在早先的版本通过了求解验证：重试即发布，并记下发布时的求解契约修订
    verified.add(SOLVER_REVISION)
    run = run_of(published(env), 'FW-001')
    assert run['status'] == 'waiting_evaluation'
    assert hub_packages.solver_revisions(env['pid']) == {run['published_release_id']: SOLVER_REVISION}


def test_a_solver_contract_changed_before_freezing_waits_for_its_own_validation(env, monkeypatch):
    revision = solver_enabled(env, monkeypatch, {SOLVER_REVISION})
    env['evaluation'].complete(RUN)
    sync(env)
    optimized(env)
    verified_release = package_evaluations.verified_solver_release

    def then_changed(pid, subject, value):
        revision['value'] = 'sha256:' + '2' * 64  # 复核之后、冻结之前又改了求解契约
        return verified_release(pid, subject, value)
    monkeypatch.setattr(package_evaluations, 'verified_solver_release', then_changed)
    run = run_of(published(env), 'FW-001')
    # Hub 拒绝没有验证依据的新修订：同样等求解验证，不当作要修内容的检查失败
    assert run['status'] == 'blocked' and run['reason'] == 'solver_unverified' and env['hub'].requests == {}


@pytest.mark.parametrize('code', ['binding_problems', 'script_binding_conflict', 'script_argument_shape',
                                  'script_output_unverified', 'package_structure_invalid', 'release_tree_unsafe',
                                  'invalid_commit'])
def test_a_package_its_export_rejects_waits_for_the_content_to_be_fixed(code):
    # 导出知识包时因内容被拒（如助手改了脚本、输出还没验证）：重试同一请求没用，改好内容并重新检查后再发布
    run = {'history': []}
    publish._failure(run, WorkspaceError('合成的发布条件问题', 422, code))
    assert run.get('status') == 'blocked' and run.get('reason') == 'checks_failed'


def test_automation_optimizes_new_results_and_publishes_checked_candidates(env):
    settings(env, optimize=True, publish=True)
    env['evaluation'].complete(RUN)
    view = sync(env)
    run = run_of(view, 'FW-001')
    assert run['status'] == 'optimizing' and run['mode'] == 'auto' and view['auto_rounds'] == 1
    (env['root'] / 'ontology/object/object_a.md').write_text(OBJECT + '\n- 合成字段说明\n', encoding='utf-8')
    env['assistant'].finish()
    view = sync(env, reconcile=False)
    assert run_of(view, 'FW-001')['status'] in ('publishing', 'waiting_evaluation')
    publish.wait(env['pid'], SUBJECT, 'FW-001', timeout=30)
    run = run_of(sync(env, reconcile=False), 'FW-001')
    # 开关打开：发布后自动用评测服务保存的登录发起评测
    assert run['status'] == 'evaluating' and run['publish_mode'] == 'auto' and run['evaluation']['mode'] == 'auto'
    rerun = env['evaluation'].reruns[0]
    assert rerun['source'] == RUN and rerun['package_ref']['release_id'] == 'release_2' and rerun['refresh'] == ''
    assert rerun['token'] == Delegated('evaluation-handle-1')


def test_round_limit_and_busy_project_hold_new_results(env):
    settings(env, optimize=True, max_rounds=1)
    env['evaluation'].complete(RUN)
    sync(env)
    env['assistant'].finish('interrupted')
    assert run_of(sync(env, reconcile=False), 'FW-001')['reason'] == 'optimization_interrupted'
    with store.update(env['pid'], SUBJECT) as state:
        store.find(state, 'FW-001').update(status='ignored')
    env['evaluation'].complete(NEXT_RUN, finished=3000.0)
    view = sync(env)
    held = run_of(view, 'FW-002')
    assert held['status'] == 'waiting_optimize' and held['reason'] == 'round_limit' and held['human_hold'] is True
    # 已有待确认事项时新到的结果进入待确认
    env['evaluation'].complete(OLD_RUN, finished=4000.0)
    assert run_of(sync(env), 'FW-003')['reason'] == 'project_busy'


def test_switching_automation_on_leaves_waiting_items_for_confirmation(env):
    env['evaluation'].complete(RUN)
    sync(env)
    settings(env, optimize=True)
    run = run_of(sync(env), 'FW-001')
    assert run['status'] == 'waiting_optimize' and run['human_hold'] is True and env['assistant'].sent == []


def test_stop_interrupts_the_assistant_and_restores_the_snapshot(env):
    env['evaluation'].complete(RUN)
    sync(env)
    settings(env, optimize=True)
    assert act(env, 'FW-001', 'optimize').status_code == 200
    (env['root'] / 'ontology/object/object_a.md').write_text('# 未完成的合成修改\n', encoding='utf-8')
    (env['root'] / 'ontology/object/object_new.md').write_text('# 新的合成对象\n', encoding='utf-8')
    assert act(env, 'FW-001', 'stop').status_code == 200
    view = sync(env, reconcile=False)
    assert run_of(view, 'FW-001')['status'] == 'interrupted' and view['settings']['optimize'] is False
    assert env['assistant'].interrupted == ['conversation_demo']
    assert (env['root'] / 'ontology/object/object_a.md').read_text(encoding='utf-8') == OBJECT
    assert not (env['root'] / 'ontology/object/object_new.md').exists()
    # 内容已恢复：之后的发布不回写成这一轮建议的处理结果
    assert package_evaluations.writeback_view(env['pid'], SUBJECT) is None


def test_rollback_restores_content_and_pauses_both_switches(env):
    env['evaluation'].complete(RUN)
    sync(env)
    optimized(env)
    published(env)
    settings(env, optimize=True, publish=True)
    response = act(env, 'FW-001', 'rollback', scope='workspace')
    assert response.status_code == 200, response.text
    view = response.json()
    assert view['settings']['optimize'] is False and view['settings']['publish'] is False
    assert run_of(view, 'FW-001')['status'] == 'rolledback'
    restored = run_of(view, 'FW-002')
    assert restored['kind'] == 'rollback' and restored['status'] == 'restored' and restored['source_id'] == 'FW-001'
    assert (env['root'] / 'ontology/object/object_a.md').read_text(encoding='utf-8') == OBJECT
    assert hub_packages.association(env['pid'])['version'] == '0.1.2'
    actions = [item['action'] for item in view['audit'] if item['action'] != '后台授权']
    assert actions[:3] == ['内容回退', '自动化确认发布与发起评测', '自动化处理优化建议']


def test_rollback_waits_while_the_assistant_is_revising(env):
    env['evaluation'].complete(RUN)
    sync(env)
    optimized(env)
    published(env)
    # 有人正在和建模助手对话，助手改的文件还没提交：回退会把它们一并抹掉
    env['assistant'].running = True
    (env['root'] / 'ontology/object/object_a.md').write_text(OBJECT + '\n- 助手未提交的合成修改\n', encoding='utf-8')
    (env['root'] / 'ontology/object/object_draft.md').write_text('# 助手新写的合成对象\n', encoding='utf-8')
    response = act(env, 'FW-001', 'rollback', scope='workspace')
    assert response.status_code == 409 and '建模助手正在对话中' in response.json()['message']
    assert '助手未提交的合成修改' in (env['root'] / 'ontology/object/object_a.md').read_text(encoding='utf-8')
    assert (env['root'] / 'ontology/object/object_draft.md').exists()
    assert run_of(sync(env, reconcile=False), 'FW-001')['status'] == 'waiting_evaluation'
    # 这一轮对话结束后照常回退
    env['assistant'].finish()
    assert act(env, 'FW-001', 'rollback', scope='workspace').status_code == 200


@pytest.mark.parametrize('action', ['rollback', 'stop'])
def test_restoring_a_snapshot_keeps_the_projects_metadata(env, action):
    env['evaluation'].complete(RUN)
    sync(env)
    assert act(env, 'FW-001', 'optimize').status_code == 200
    # 快照之后所有者改了项目名称：恢复内容不能把项目元数据一并改回快照时的样子
    env['manager'].rename_project(env['pid'], 'project_alpha_renamed')
    (env['root'] / 'ontology/object/object_a.md').write_text('# 合成修改\n', encoding='utf-8')
    if action == 'rollback':
        env['assistant'].finish()
        assert run_of(sync(env, reconcile=False), 'FW-001')['status'] == 'waiting_publish'
        assert act(env, 'FW-001', 'rollback', scope='workspace').status_code == 200
    else:
        assert act(env, 'FW-001', 'stop').status_code == 200
    assert (env['root'] / 'ontology/object/object_a.md').read_text(encoding='utf-8') == OBJECT
    assert env['manager'].get_project(env['pid'])['name'] == 'project_alpha_renamed'
    assert env['manager'].get_project(env['pid'])['owner'] == 'operator_a'


def test_stopping_leaves_another_members_conversation_alone(env):
    env['evaluation'].complete(RUN)
    sync(env)
    assert act(env, 'FW-001', 'optimize').status_code == 200
    # 飞轮这一轮已结束，另一位成员接着在同一对话里让助手改文件，尚未提交
    env['assistant'].ended = True
    (env['root'] / 'ontology/object/object_b.md').write_text('# 另一位成员的合成修改\n', encoding='utf-8')
    response = act(env, 'FW-001', 'stop')
    assert response.status_code == 409 and '其他对话' in response.json()['message']
    assert env['assistant'].interrupted == [] and (env['root'] / 'ontology/object/object_b.md').exists()
    assert run_of(sync(env, reconcile=False), 'FW-001')['status'] == 'optimizing'
    # 读不到对话记录、无从判断忙的是哪一轮时同样不打断
    env['assistant'].ended = None
    assert act(env, 'FW-001', 'stop').status_code == 409 and env['assistant'].interrupted == []


def test_stopping_does_not_restore_while_the_assistant_keeps_running(env, monkeypatch):
    env['evaluation'].complete(RUN)
    sync(env)
    assert act(env, 'FW-001', 'optimize').status_code == 200
    (env['root'] / 'ontology/object/object_b.md').write_text('# 正在写入的合成修改\n', encoding='utf-8')
    # 打断没有及时生效：现在恢复，之后写入的修改会落在恢复的内容上
    monkeypatch.setattr(env['assistant'], 'interrupt', lambda pid, conversation: None)
    monkeypatch.setattr(optimize, 'STOP_WAIT_SECONDS', 0)
    response = act(env, 'FW-001', 'stop')
    assert response.status_code == 409 and '仍在执行' in response.json()['message']
    assert (env['root'] / 'ontology/object/object_b.md').exists()
    assert run_of(sync(env, reconcile=False), 'FW-001')['status'] == 'optimizing'


def test_the_assistant_tells_whether_the_flywheels_own_turn_ended():
    class Sessions:
        def __init__(self, events):
            self.events = events

        def get_history(self, pid, conversation):
            return self.events

    earlier = [{'type': 'user'}, {'type': 'assistant'}, {'type': 'result'}]
    running = earlier + [{'type': 'assistant'}]
    assert Assistant(None, Sessions(running)).turn_ended('pid_a', 'conversation_demo', 3) is False
    ended = earlier + [{'type': 'result'}, {'type': 'user'}]
    assert Assistant(None, Sessions(ended)).turn_ended('pid_a', 'conversation_demo', 3) is True

    class Unreadable:
        def get_history(self, pid, conversation):
            raise SessionError('合成的读取失败', 503)

    assert Assistant(None, Unreadable()).turn_ended('pid_a', 'conversation_demo', 3) is None


def test_rollback_to_hub_publishes_the_restored_content(env):
    env['evaluation'].complete(RUN)
    sync(env)
    optimized(env)
    published(env)
    assert act(env, 'FW-001', 'rollback', scope='hub').status_code == 200
    publish.wait(env['pid'], SUBJECT, 'FW-002', timeout=30)
    restored = run_of(sync(env, reconcile=False), 'FW-002')
    # 回退暂停了两个开关：恢复版本发布后等待确认评测，沿用原运行的评测配置
    assert restored['status'] == 'waiting_evaluation' and restored['target_version'] == '0.1.3'
    assert restored['human_hold'] is True
    # 回写的是优化后发布的版本，不是撤销优化内容的恢复版本
    sync(env)
    assert package_evaluations.writeback_view(env['pid'], SUBJECT)['release_id'] == 'release_2'


def test_archived_projects_show_the_flywheel_but_cannot_act(env):
    env['manager'].set_archived(env['pid'], True)
    settings_response = env['client'].put(f"/api/v1/projects/{env['pid']}/flywheel/settings", json={'optimize': True})
    assert settings_response.status_code == 409 and settings_response.json()['code'] == 'project_archived'
    env['evaluation'].complete(RUN)
    view = sync(env)
    assert view['can_write'] is False and run_of(view, 'FW-001')['status'] == 'waiting_optimize'


def test_flywheel_needs_unified_accounts(env, monkeypatch):
    monkeypatch.setattr(auth, 'ENABLED', False)
    value = env['client'].get(f"/api/v1/projects/{env['pid']}/flywheel").json()
    assert value == {'available': False, 'message': '数据飞轮需要统一账号登录：评测结果和 Hub 发布都按账号隔离'}
    # 其他接口与 ES-02～ES-06 同码，前端不当成登录过期
    for method, path, body in (('put', 'settings', {'optimize': True}), ('get', 'runs', None),
                               ('post', 'runs/FW-001/optimize', None)):
        response = getattr(env['client'], method)(f"/api/v1/projects/{env['pid']}/flywheel/{path}",
                                                  **({'json': body} if body else {}))
        assert response.status_code == 401 and response.json()['code'] == 'login_required'


def test_state_without_hub_package_or_evaluation_service(env, monkeypatch):
    knowledge_hub.save(knowledge_hub.state_dir() / 'packages' / env['pid'] / 'association.json', {})
    assert '尚未发布到 Hub' in sync(env)['message']
    monkeypatch.setattr(config, 'EVAL_URL', '')
    assert sync(env)['message'] == '未配置评测服务地址，无法接收评测反馈'
    assert env['evaluation'].calls == []


def test_publish_with_expected_revision_refuses_drifted_content(env):
    manager, pid = env['manager'], env['pid']
    revision = hub_packages.content_revision(env['root'])
    # 发布读主线最新提交：检查之后主线又进了一个修改
    (env['root'] / 'ontology/object/object_a.md').write_text(OBJECT + '\n- 合成漂移\n', encoding='utf-8')
    _commit_paths(env['root'], ['ontology'], 'chore: 合成漂移')
    with pytest.raises(WorkspaceError) as caught:
        hub_packages.publish(manager, pid, 'request_demo', 'user-token', SUBJECT, expected_revision=revision)
    assert caught.value.code == 'revision_conflict' and env['hub'].calls == []
    assert caught.value.details == {'target': 'ontology', 'expected': revision,
                                    'actual': hub_packages.content_revision(env['root'])}
    assert not (hub_packages.directory(pid, SUBJECT) / 'requests' / 'request_demo' / 'task.json').exists()
    steps = []
    receipt = hub_packages.publish(manager, pid, 'request_demo', 'user-token', SUBJECT,
                                   expected_revision=hub_packages.content_revision(env['root']), progress=steps.append)
    assert receipt['status'] == 'committed' and steps == ['frozen', 'uploading', 'committing']


def test_rule_checker_runs_on_the_workspace(env, monkeypatch):
    monkeypatch.undo()
    item = checks._rules(env['root'])
    assert item['key'] == 'rules' and '未能完成' not in item['message']


def test_records_load_in_batches_from_the_newest(env):
    with store.update(env['pid'], SUBJECT) as current:
        for index in range(25):
            store.new_run(current, status='ignored' if index % 2 else 'waiting_optimize')
        for index in range(30):
            store.audit(current, 'operator_a', '自动优化规则', f'合成变更 {index}')
    url = f"/api/v1/projects/{env['pid']}/flywheel"
    first = env['client'].get(f'{url}/runs').json()
    assert [run['id'] for run in first['items'][:2]] == ['FW-025', 'FW-024'] and len(first['items']) == 20
    assert first['total'] == 25 and first['next_offset'] == 20 and 'history' not in first['items'][0]
    rest = env['client'].get(f'{url}/runs', params={'offset': 20}).json()
    assert [run['id'] for run in rest['items']][-1] == 'FW-001' and rest['next_offset'] is None
    # 状态筛选在服务端进行：还没加载到的旧记录也会被筛出来
    waiting = env['client'].get(f'{url}/runs', params={'filter': 'waiting', 'limit': 50}).json()
    assert waiting['total'] == 13 and all(run['status'] == 'waiting_optimize' for run in waiting['items'])
    audit = env['client'].get(f'{url}/audit', params={'limit': 10}).json()
    assert audit['items'][0]['description'] == '合成变更 29' and audit['total'] == 30 and audit['next_offset'] == 10
    # 每批有上限，筛选只接受下拉框里的分组
    assert env['client'].get(f'{url}/runs', params={'limit': 51}).status_code == 400
    assert env['client'].get(f'{url}/runs', params={'filter': 'unknown'}).status_code == 400


def test_runs_left_waiting_on_an_old_version_are_closed_and_stop_counting_as_pending(env):
    env['evaluation'].complete(RUN)
    sync(env)
    # 早先的版本把旧版本上的运行停在“需处理”：同步时关闭为已过期，待处理数随之消失
    with store.update(env['pid'], SUBJECT) as current:
        store.block(store.find(current, 'FW-001'), 'version_mismatch', '通知已过期', '合成说明')
        store.find(current, 'FW-001')['release_id'] = 'release_zero'
    assert sync(env, reconcile=False)['pending'] == 0
    run = run_of(sync(env, reconcile=False), 'FW-001')
    assert run['status'] == 'expired' and run['human_hold'] is True
    assert act(env, 'FW-001', 'optimize').status_code == 409


def second_candidate(env, manual_edit):
    """Publish FW-001, evaluate the new version, then optimize FW-002 with the publication switch off."""
    env['evaluation'].complete(RUN)
    sync(env)
    first = run_of(optimized(env), 'FW-001')
    # 上次发布早于内容指纹：无法确认
    assert first['status'] == 'waiting_publish' and first['earlier_changes'] is None
    published(env)
    linked = hub_packages.association(env['pid'])
    assert linked['inputs_digest'] == hub_packages.workspace_inputs(env['manager'], env['pid'])
    env['evaluation'].complete(NEXT_RUN, release='release_2', version='0.1.2', finished=3000.0, score=75)
    sync(env)
    if manual_edit:
        # 发布之后、优化之前在工作区里手工改了文档，没有发布
        (env['root'] / 'ontology/operation/operation_a.md').write_text('# 操作 A\n\n合成操作，已补充说明。\n', encoding='utf-8')
    assert act(env, 'FW-002', 'optimize').status_code == 200
    (env['root'] / 'ontology/object/object_a.md').write_text(OBJECT + '\n- 合成字段：第二轮补充\n', encoding='utf-8')
    env['assistant'].finish()
    return run_of(sync(env, reconcile=False), 'FW-002')


def test_a_candidate_reports_no_earlier_changes_when_the_optimization_started_from_the_release(env):
    run = second_candidate(env, manual_edit=False)
    assert run['status'] == 'waiting_publish' and run['earlier_changes'] is False


def test_a_candidate_reports_unpublished_edits_made_before_the_optimization(env):
    run = second_candidate(env, manual_edit=True)
    assert run['status'] == 'waiting_publish' and run['earlier_changes'] is True


def test_one_failed_reconciliation_does_not_report_the_evaluation_service_unavailable(env):
    assert sync(env)['evaluation']['reachable'] is True
    env['evaluation'].listing_failure = WorkspaceError('评测服务响应超时，请稍后重试', 504, 'eval_timeout')
    # 偶发一次失败（如打开弹窗时对方一时没响应）不提示，否则提示会一闪而过
    assert sync(env)['evaluation']['reachable'] is True
    assert sync(env)['evaluation']['reachable'] is False
    # 不对账的同步沿用最近的结论，提示不会被下一次轮询抹掉
    assert sync(env, reconcile=False)['evaluation']['reachable'] is False
    env['evaluation'].listing_failure = None
    assert sync(env)['evaluation']['reachable'] is True


OTHER_SUBJECT = json.dumps([USER['iss'], 'operator_b'], ensure_ascii=False)


def another_member_is(env, status, **fields):
    # 同一项目里另一个用户的飞轮（如项目换所有者之前的所有者）：各自的状态分开存放
    with store.update(env['pid'], OTHER_SUBJECT) as other:
        other['identity'] = {'subject': OTHER_SUBJECT, 'username': 'operator_b'}
        store.new_run(other, status=status, **fields)


@pytest.mark.parametrize('status', ['optimizing', 'publishing'])
def test_another_members_optimization_or_publication_holds_the_optimization(env, status):
    env['evaluation'].complete(RUN)
    sync(env)
    another_member_is(env, status)
    response = act(env, 'FW-001', 'optimize')
    assert response.status_code == 409 and '另一位成员的数据飞轮正在优化或发布' in response.json()['message']
    assert env['assistant'].sent == []


def test_automatic_optimization_waits_for_another_members_optimization_but_not_their_candidate(env):
    settings(env, optimize=True)
    another_member_is(env, 'optimizing')
    env['evaluation'].complete(RUN)
    run = run_of(sync(env), 'FW-001')
    # 等对方的优化结束：不转为人工确认，也不把对方修订到一半的内容并进自己的快照
    assert run['status'] == 'waiting_optimize' and run['human_hold'] is False and env['assistant'].sent == []
    with store.update(env['pid'], OTHER_SUBJECT) as other:
        other['runs'][0]['status'] = 'waiting_publish'
    # 对方的候选内容要等其本人确认发布，可能一直等不到：不再挡住别人的优化
    assert run_of(sync(env), 'FW-001')['status'] == 'optimizing'


@pytest.mark.parametrize('status', ['optimizing', 'publishing'])
def test_publication_recheck_and_rollback_wait_for_another_members_work(env, status):
    env['evaluation'].complete(RUN)
    sync(env)
    assert run_of(optimized(env), 'FW-001')['status'] == 'waiting_publish'
    another_member_is(env, status)
    # 发布会发出对方改到一半的内容或让它白做，回退会抹掉它正在进行的修订
    for action, body in (('publish', {}), ('rollback', {'scope': 'workspace'})):
        response = act(env, 'FW-001', action, **body)
        assert response.status_code == 409 and '另一位成员的数据飞轮正在优化或发布' in response.json()['message']
    with store.update(env['pid'], SUBJECT) as own:
        store.find(own, 'FW-001').update(status='blocked', reason='content_changed')
    # 重新检查会把工作区提交为候选内容，对方改到一半的内容会混进来
    response = act(env, 'FW-001', 'recheck')
    assert response.status_code == 409 and '请等其完成后再重新检查' in response.json()['message']
    assert env['hub'].requests == {}
    assert (env['root'] / 'ontology/object/object_a.md').read_text(encoding='utf-8') != OBJECT
    with store.update(env['pid'], OTHER_SUBJECT) as other:
        other['runs'][0]['status'] = 'blocked'
    assert act(env, 'FW-001', 'recheck').status_code == 200
    assert run_of(published(env), 'FW-001')['status'] == 'waiting_evaluation'


def automatic_candidate(env):
    settings(env, optimize=True, publish=True)
    env['evaluation'].complete(RUN)
    sync(env)
    (env['root'] / 'ontology/object/object_a.md').write_text(OBJECT + '\n- 合成字段说明\n', encoding='utf-8')
    env['assistant'].finish()


def test_automatic_publication_waits_for_another_members_optimization(env):
    automatic_candidate(env)
    another_member_is(env, 'optimizing')
    run = run_of(sync(env, reconcile=False), 'FW-001')
    # 等对方结束再发布，不转为人工确认
    assert run['status'] == 'waiting_publish' and run['publish_hold'] is False and env['hub'].requests == {}
    with store.update(env['pid'], OTHER_SUBJECT) as other:
        other['runs'][0]['status'] = 'blocked'
    assert run_of(sync(env, reconcile=False), 'FW-001')['status'] in ('publishing', 'evaluating', 'waiting_evaluation')
    publish.wait(env['pid'], SUBJECT, 'FW-001', timeout=30)


def test_an_automatic_step_that_loses_the_race_to_another_member_waits_for_the_next_round(env, monkeypatch):
    automatic_candidate(env)
    # 检查时对方还没开始，拿到锁时对方已在优化：这一轮不发布，也不转为人工确认
    answers = iter([False])
    monkeypatch.setattr(store, 'others_working', lambda pid, subject: next(answers, True))
    run = run_of(sync(env, reconcile=False), 'FW-001')
    assert run['status'] == 'waiting_publish' and run['publish_hold'] is False and env['hub'].requests == {}
    history = [step['title'] for step in store.find(store.load(env['pid'], SUBJECT), 'FW-001')['history']]
    assert '自动执行未开始' not in history


def modeling_results(env):
    return [path for _, path, _ in env['evaluation'].calls if path.endswith('/modeling-result')]


def test_a_run_that_changed_nothing_is_written_back_only_for_its_optimizers_release(env):
    env['evaluation'].complete(RUN)
    sync(env)
    assert act(env, 'FW-001', 'optimize').status_code == 200

    def release(subject, release_id, version, kind='release'):
        hub_packages.record(env['manager'], env['pid'], subject, {
            'ontology_id': PACKAGE, 'release_id': release_id, 'package_digest': 'c' * 64, 'version': version,
            'committed_at': f'2026-11-0{version[-1]}T00:00:00+00:00', 'kind': kind})
    # 飞轮还在修订时另一位成员发布了版本：至多含一半修订，对账时也不回写
    release(OTHER_SUBJECT, 'release_two', '0.1.2')
    sync(env)
    assert modeling_results(env) == []
    # 助手没有修改知识包就结束了：那个版本同样不算
    env['assistant'].finish()
    assert run_of(sync(env), 'FW-001')['reason'] == 'no_changes'
    release(OTHER_SUBJECT, 'release_three', '0.1.3')
    release(SUBJECT, 'snapshot_four', '0.1.4', kind='evaluation_snapshot')  # 评测快照不是正式版本
    sync(env)
    assert modeling_results(env) == []
    # 优化者本人手工处理后发布：照常回写
    release(SUBJECT, 'release_five', '0.1.5')
    sync(env)
    assert modeling_results(env) == [f'/runs/{RUN}/modeling-result']
    assert package_evaluations.writeback_view(env['pid'], SUBJECT)['release_id'] == 'release_five'


def test_rolling_back_an_unpublished_run_is_not_written_back(env):
    env['evaluation'].complete(RUN)
    sync(env)
    optimized(env)
    # 优化开始时记为项目最近一次评测优化：之后的第一个新版本会回写成它的处理结果
    assert package_evaluations.writeback_view(env['pid'], SUBJECT)['run_id'] == RUN
    assert act(env, 'FW-001', 'rollback', scope='workspace').status_code == 200
    assert package_evaluations.writeback_view(env['pid'], SUBJECT) is None


def test_an_automatic_optimization_that_loses_the_race_to_another_member_waits_for_the_next_round(env, monkeypatch):
    settings(env, optimize=True)
    answers = iter([False])
    monkeypatch.setattr(store, 'others_working', lambda pid, subject: next(answers, True))
    env['evaluation'].complete(RUN)
    run = run_of(sync(env), 'FW-001')
    assert run['status'] == 'waiting_optimize' and run['human_hold'] is False and env['assistant'].sent == []


def test_an_automatic_optimization_that_finds_the_assistant_busy_waits_for_the_next_round(env, monkeypatch):
    settings(env, optimize=True)
    # 检查时助手还空着，开始时已在别人的对话或别人飞轮的修订中：等下一轮，不转为人工确认
    answers = iter([False])
    monkeypatch.setattr(env['assistant'], 'busy', lambda pid: next(answers, True))
    env['evaluation'].complete(RUN)
    run = run_of(sync(env), 'FW-001')
    assert run['status'] == 'waiting_optimize' and run['human_hold'] is False and env['assistant'].sent == []
    history = [step['title'] for step in store.find(store.load(env['pid'], SUBJECT), 'FW-001')['history']]
    assert '自动执行未开始' not in history


@pytest.mark.parametrize('action, body', [('ignore', {}), ('rollback', {'scope': 'workspace'})])
def test_a_run_without_changes_that_the_user_ignores_or_rolls_back_is_not_written_back(env, action, body):
    env['evaluation'].complete(RUN)
    sync(env)
    assert run_of(optimized(env, change=False), 'FW-001')['reason'] == 'no_changes'
    assert package_evaluations.writeback_view(env['pid'], SUBJECT)['run_id'] == RUN
    assert act(env, 'FW-001', action, **body).status_code == 200
    assert package_evaluations.writeback_view(env['pid'], SUBJECT) is None


def test_rolling_back_an_earlier_run_takes_back_the_later_optimization_too(env):
    env['evaluation'].complete(RUN)
    sync(env)
    optimized(env)
    published(env)
    env['evaluation'].complete(NEXT_RUN, release='release_2', version='0.1.2', finished=3000.0)
    sync(env)
    assert act(env, 'FW-002', 'optimize').status_code == 200
    env['assistant'].finish()
    assert run_of(sync(env, reconcile=False), 'FW-002')['reason'] == 'no_changes'
    assert package_evaluations.writeback_view(env['pid'], SUBJECT)['run_id'] == NEXT_RUN
    # 回退 FW-001 撤回了其后的全部修改：之后的版本（包括恢复版本）不是 FW-002 那一轮建议的处理结果
    assert act(env, 'FW-001', 'rollback', scope='workspace').status_code == 200
    assert package_evaluations.writeback_view(env['pid'], SUBJECT) is None


def test_rolling_back_an_earlier_run_keeps_a_later_optimization_whose_release_is_out(env):
    env['evaluation'].complete(RUN)
    sync(env)
    optimized(env)
    published(env)
    env['evaluation'].complete(NEXT_RUN, release='release_2', version='0.1.2', finished=3000.0)
    sync(env)
    assert act(env, 'FW-002', 'optimize').status_code == 200
    (env['root'] / 'ontology/object/object_a.md').write_text(OBJECT + '\n- 第二轮合成修改\n', encoding='utf-8')
    env['assistant'].finish()
    sync(env, reconcile=False)
    published(env, 'FW-002')
    # FW-002 的版本已发布、只是还没对账回写：回退 FW-001 不能把它的处理结果一起丢掉
    assert act(env, 'FW-001', 'rollback', scope='workspace').status_code == 200
    sync(env)
    assert package_evaluations.writeback_view(env['pid'], SUBJECT)['release_id'] == 'release_3'


def test_a_run_closed_by_a_later_all_pass_result_is_not_written_back(env):
    env['evaluation'].complete(RUN)
    sync(env)
    assert run_of(optimized(env, change=False), 'FW-001')['reason'] == 'no_changes'
    # 判分重判后全部通过：这一轮无需处理，之后的版本不是它的处理结果
    env['evaluation'].complete(RUN, revision=2, cases={'pass': 4, 'partial': 0, 'fail': 0})
    assert run_of(sync(env), 'FW-001')['status'] == 'passed'
    assert package_evaluations.writeback_view(env['pid'], SUBJECT) is None


def test_ignoring_a_run_never_optimized_here_keeps_the_evaluation_pages_optimization(env):
    env['evaluation'].complete(RUN)
    sync(env)
    # 用户在「已发布版本」里直接评测优化了同一轮评测，再在飞轮里忽略这条没开始过的运行
    package_evaluations.optimize(env['manager'], env['pid'], SUBJECT, 'user-token', RUN)
    assert act(env, 'FW-001', 'ignore').status_code == 200
    assert package_evaluations.writeback_view(env['pid'], SUBJECT)['run_id'] == RUN


def assistant_unavailable(*args):
    raise WorkspaceError('助手暂不可用', 503, 'assistant_unavailable')


def test_a_run_whose_start_failed_and_that_is_then_ignored_is_not_written_back(env, monkeypatch):
    env['evaluation'].complete(RUN)
    sync(env)
    monkeypatch.setattr(env['assistant'], 'start', assistant_unavailable)
    # 评测建议已导入（项目记下了这次评测优化），助手却没能启动：运行仍待优化
    assert act(env, 'FW-001', 'optimize').status_code == 503
    assert run_of(sync(env, reconcile=False), 'FW-001')['status'] == 'waiting_optimize'
    assert package_evaluations.writeback_view(env['pid'], SUBJECT)['run_id'] == RUN
    assert act(env, 'FW-001', 'ignore').status_code == 200
    assert package_evaluations.writeback_view(env['pid'], SUBJECT) is None


def release_of(env, subject, release_id, version):
    hub_packages.record(env['manager'], env['pid'], subject, {
        'ontology_id': PACKAGE, 'release_id': release_id, 'package_digest': 'c' * 64, 'version': version,
        'committed_at': f'2026-11-0{version[-1]}T00:00:00+00:00'})


def test_after_a_start_that_failed_only_the_optimizers_release_counts(env, monkeypatch):
    env['evaluation'].complete(RUN)
    sync(env)
    monkeypatch.setattr(env['assistant'], 'start', assistant_unavailable)
    assert act(env, 'FW-001', 'optimize').status_code == 503
    # 没能交给助手，飞轮没有修改内容：别人的版本不算（运行随之过期），记录也不会一直停在修订中
    release_of(env, OTHER_SUBJECT, 'release_two', '0.1.2')
    sync(env)
    assert modeling_results(env) == []
    release_of(env, SUBJECT, 'release_three', '0.1.3')
    sync(env)
    assert modeling_results(env) == [f'/runs/{RUN}/modeling-result']


@pytest.mark.parametrize('ended, reason', [('failed', 'optimization_failed'),
                                           ('interrupted', 'optimization_interrupted')])
def test_an_optimization_that_did_not_finish_counts_the_next_release(env, ended, reason):
    env['evaluation'].complete(RUN)
    sync(env)
    assert act(env, 'FW-001', 'optimize').status_code == 200
    env['assistant'].finish(ended)
    assert run_of(sync(env, reconcile=False), 'FW-001')['reason'] == reason
    # 助手没有正常结束：修订已停，之后发布的版本照常回写
    release_of(env, SUBJECT, 'release_two', '0.1.2')
    sync(env)
    assert modeling_results(env) == [f'/runs/{RUN}/modeling-result']


def test_a_check_that_could_not_run_still_ends_the_revision(env, monkeypatch):
    env['evaluation'].complete(RUN)
    sync(env)
    assert act(env, 'FW-001', 'optimize').status_code == 200
    env['assistant'].finish()

    def unavailable(*args):
        raise WorkspaceError('检查暂不可用', 503)
    with monkeypatch.context() as patched:
        patched.setattr(checks, 'evaluate', unavailable)
        assert run_of(sync(env, reconcile=False), 'FW-001')['reason'] == 'checks_failed'
    release_of(env, SUBJECT, 'release_two', '0.1.2')
    sync(env)
    assert modeling_results(env) == [f'/runs/{RUN}/modeling-result']


def test_ending_without_changes_drops_a_release_someone_else_published_before(env):
    sync(env)
    package_evaluations.remember(env['pid'], SUBJECT, {'id': RUN}, {'release_id': 'release_one', 'version': '0.1.1'},
                                 STEMS[RUN], 'FW-001')
    # 检查没跑完（修订已停）后别人发布了版本，重新检查得出没有修改：那个版本同样不算
    package_evaluations.end_revision(env['pid'], SUBJECT, RUN, 'FW-001', own_release=False)
    release_of(env, OTHER_SUBJECT, 'release_two', '0.1.2')
    package_evaluations.end_revision(env['pid'], SUBJECT, RUN, 'FW-001', own_release=True)
    sync(env)
    assert modeling_results(env) == []


def test_ignoring_a_run_after_its_optimizer_published_keeps_that_release(env):
    env['evaluation'].complete(RUN)
    sync(env)
    assert run_of(optimized(env, change=False), 'FW-001')['reason'] == 'no_changes'
    # 优化者手工处理后发布了版本，再忽略这条运行：已发布的版本仍是处理结果
    release_of(env, SUBJECT, 'release_two', '0.1.2')
    assert act(env, 'FW-001', 'ignore').status_code == 200
    sync(env)
    assert modeling_results(env) == [f'/runs/{RUN}/modeling-result']


def test_a_web_optimization_during_a_revision_is_left_to_its_own_rules(env):
    env['evaluation'].complete(RUN)
    sync(env)
    assert act(env, 'FW-001', 'optimize').status_code == 200
    # 修订中途，用户又在「已发布版本」里评测优化了同一轮评测：记录归那次评测优化，飞轮收尾时不再改动它
    package_evaluations.optimize(env['manager'], env['pid'], SUBJECT, 'user-token', RUN)
    release_of(env, SUBJECT, 'release_two', '0.1.2')
    env['assistant'].finish()
    assert run_of(sync(env), 'FW-001')['reason'] == 'no_changes'
    assert package_evaluations.writeback_view(env['pid'], SUBJECT)['release_id'] == 'release_two'


def test_a_failed_pin_leaves_the_release_for_maintenance_to_record(env, monkeypatch):
    env['evaluation'].complete(RUN)
    sync(env)
    optimized(env)
    with monkeypatch.context() as patched:
        patched.setattr(package_evaluations, 'pin_release',
                        lambda pid, linked: (_ for _ in ()).throw(OSError('synthetic failure')))
        run = run_of(published(env), 'FW-001')
    # 关联没有写下：发布记为本地记录待恢复，定期维护补记关联时一并钉住
    assert run['local_record_pending'] is True and hub_packages.association(env['pid'])['release_id'] == 'release_one'
    # 关联只是晚到：新版本照样等待评测，不当作“已有更新的版本”跳过
    assert run['status'] == 'waiting_evaluation'
    hub_packages.maintain(env['manager'])
    assert hub_packages.association(env['pid'])['release_id'] == 'release_2'
    assert run_of(sync(env), 'FW-001')['status'] == 'waiting_evaluation'
    assert package_evaluations.writeback_view(env['pid'], SUBJECT)['release_id'] == 'release_2'


def test_a_pending_write_back_from_before_pinning_is_retried_with_its_release(env):
    env['evaluation'].complete(RUN)
    sync(env)
    # 本功能之前的记录：回写 release_two 时评测服务暂不可用，之后又记下了新版本
    package_evaluations.remember(env['pid'], SUBJECT, {'id': RUN}, {'release_id': 'release_one', 'version': '0.1.1'},
                                 STEMS[RUN])
    path = package_evaluations._optimization_path(env['pid'])
    pending = {'status': 'pending', 'release_id': 'release_two', 'version': '0.1.2', 'message': '', 'at': 1.0}
    knowledge_hub.save(path, {**knowledge_hub.load(path), 'result': pending})
    release_of(env, SUBJECT, 'release_three', '0.1.3')
    sync(env)
    assert modeling_results(env) == [f'/runs/{RUN}/modeling-result']
    assert package_evaluations.writeback_view(env['pid'], SUBJECT)['release_id'] == 'release_two'


def test_dropping_the_latest_optimization_keeps_an_earlier_one_still_to_write_back(env):
    env['evaluation'].complete(RUN)
    sync(env)
    package_evaluations.remember(env['pid'], SUBJECT, {'id': RUN}, {'release_id': 'release_one', 'version': '0.1.1'},
                                 STEMS[RUN])
    release_of(env, SUBJECT, 'release_two', '0.1.2')  # 钉为 RUN 的处理结果，还没写回
    package_evaluations.remember(env['pid'], SUBJECT, {'id': NEXT_RUN},
                                 {'release_id': 'release_two', 'version': '0.1.2'}, STEMS[NEXT_RUN], 'FW-002')
    # 后一次优化没有结果（被忽略、中止或已全部通过）：只清掉它自己，早先那次的结果仍等写回
    package_evaluations.forget(env['pid'], SUBJECT, NEXT_RUN, 'FW-002')
    assert package_evaluations.writeback_view(env['pid'], SUBJECT) is None
    sync(env)
    assert modeling_results(env) == [f'/runs/{RUN}/modeling-result']
    assert env['evaluation'].writebacks[0]['release_id'] == 'release_two'


def test_the_round_limit_can_be_lifted(env):
    view = settings(env, optimize=True, max_rounds=0)
    assert view['settings']['max_rounds'] == 0
    assert '连续自动处理上限：3 轮 → 无限次' in [item['description'] for item in view['audit']]
    # 不限轮数：连续处理多少轮都照常自动优化，直到评测集全部通过
    with store.update(env['pid'], SUBJECT) as current:
        current['auto_rounds'] = 50
    env['evaluation'].complete(RUN)
    run = run_of(sync(env), 'FW-001')
    assert run['status'] == 'optimizing' and run['mode'] == 'auto'


def test_only_the_owner_uses_the_flywheel(env, monkeypatch):
    # 单人所有权（契约 1.4）：别人（包括不是所有者的管理员）打不开项目内容，数据飞轮也一样
    claims = {'sub': 'operator_b', 'iss': USER['iss'], 'role': 'admin', 'exp': time.time() + 3600}
    monkeypatch.setattr(auth, 'verify_access_token', lambda token: dict(claims) if token == 'user-token' else None)
    url = f"/api/v1/projects/{env['pid']}/flywheel"
    for response in (env['client'].get(url), env['client'].put(f'{url}/settings', json={'optimize': True}),
                     env['client'].post(f'{url}/runs/FW-001/optimize')):
        assert response.status_code == 403 and response.json()['code'] == 'project_owner_only'
    assert store.load(env['pid'], json.dumps([USER['iss'], 'operator_b'], ensure_ascii=False))['runs'] == []


def test_projects_get_the_evolution_command_and_the_generalization_check(env):
    assert (env['root'] / '.claude/commands/ontology-evolution.md').is_file()
    assert (env['root'] / '.claude/scripts/check_eval_generalization.py').is_file()


def test_the_assistant_needs_the_evolution_command(tmp_path):
    class Sessions:
        def resolve_conversation(self, pid):
            raise AssertionError('不应发起对话')

    with pytest.raises(WorkspaceError) as caught:
        Assistant(None, Sessions()).start('pid_a', tmp_path, '/ontology-evolution eval-demo.md --flywheel', 'operator_a')
    assert caught.value.status == 409 and caught.value.code == 'pack_not_enabled'


def test_a_duplicate_submission_or_a_running_autobuild_counts_as_a_busy_assistant():
    from app.api.errors import ApiError

    duplicate = SessionError('合成的重复提交', 409)
    duplicate.code = 'message_duplicate'
    for exc in (duplicate, ApiError('autobuild_running', '合成的自主构建', 409)):
        error = assistant_module._unavailable(exc)
        assert error.status == 409 and error.code == store.ASSISTANT_BUSY_CODE
    other = assistant_module._unavailable(SessionError('合成的引擎不可用', 503))
    assert other.status == 503 and other.code == 'assistant_unavailable'
