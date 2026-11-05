"""Run artifacts, package-level views and modeling notifications. All identities and data are synthetic."""
import json
import re
import time
from types import SimpleNamespace

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import pytest

from evaluation import suggestions
from evaluation.api import create_app
from evaluation.artifacts import STEM, render_report, summarize
from evaluation.core import Config, Failure
from evaluation.execution import EVIDENCE_CALLS, EVIDENCE_PREVIEW, Consumer
from evaluation.notifications import backoff
from test_evaluation import Dependencies, preparation

# The corpus line check_eval_generalization.py reads from legacy reports.
CASE_FIELD = re.compile(r'^\s*-\s*\*\*(问题|标准答案)\*\*[：:]\s*(.+)$')
EVENTS = '\n\n'.join([
    'retry: 2000',
    'id: 1\nevent: tool.started\ndata: ' + json.dumps({'event_type': 'tool.started', 'seq': 1, 'payload': {
        'name': 'business_data_search', 'tool_call_id': 'call_1', 'input_preview': {'table': 'synthetic_items', 'columns': ['item_code']}}}),
    'id: 2\nevent: tool.finished\ndata: ' + json.dumps({'event_type': 'tool.finished', 'seq': 2, 'payload': {
        'name': 'business_data_search', 'tool_call_id': 'call_1', 'status': 'completed',
        'output_preview': 'Synthetic rows: 0; token=synthetic-leaked-value'}}),
    'id: 3\nevent: tool.started\ndata: ' + json.dumps({'event_type': 'tool.started', 'seq': 3, 'payload': {
        'name': 'execute_script', 'tool_call_id': 'call_2'}}),
    'id: 4\nevent: tool.finished\ndata: ' + json.dumps({'event_type': 'tool.finished', 'seq': 4, 'payload': {
        'name': 'execute_script', 'tool_call_id': 'call_2', 'status': 'error', 'error_code': 'SCRIPT_ARGUMENT_VALIDATION_FAILED',
        'error': json.dumps({'error': {'code': 'SCRIPT_ARGUMENT_VALIDATION_FAILED', 'message': 'Synthetic invalid argument',
                                       'issues': [{'code': 'ungrounded_identity', 'path': 'arguments.item_code'}]}})}}),
]) + '\n\n'


class Modeling:
    """Synthetic onto-modeling receiver answering with the current scripted HTTP status."""
    def __init__(self):
        self.received, self.status = [], 200

    def __call__(self, kwargs):
        if self.status != 200:
            raise Failure('dependency_rejected', 'Synthetic rejection', 503, 'dependency', upstream_status=self.status)
        self.received.append((kwargs['headers'].get('X-Evaluation-Service-Token'), json.loads(kwargs['content'])))
        return {'accepted': True}


@pytest.fixture
def service(tmp_path):
    deps = Dependencies()
    deps.events = EVENTS
    modeling = Modeling()
    verdicts = {}
    original = deps.json

    def transport(method, base, path, **kwargs):
        if path == '/api/internal/evaluation/notifications':
            assert base == 'https://modeling.example.com'
            return modeling(kwargs)
        if path == '/chat/completions':
            tool = kwargs['json']['tool_choice']['function']['name']
            if tool == 'submit_suggestions':
                payload = json.loads(kwargs['json']['messages'][1]['content'])
                deps.suggestion_inputs.append(payload)
                if deps.on_suggestions:
                    deps.on_suggestions()
                arguments = {'summary': 'Synthetic summary of failed cases', 'notes': ['Synthetic note'], 'items': [
                    {'cases': [payload['cases'][0]['number'], 99], 'category': 'C', 'evidence': 'Synthetic evidence | with pipe',
                     'suggestion': 'Synthetic generic revision', 'targets': ['objects/item.md', 'objects/missing.md', '../outside.md']}]}
                return {'choices': [{'message': {'tool_calls': [{'function': {'name': tool, 'arguments': json.dumps(arguments)}}]}}]}
            question = json.loads(kwargs['json']['messages'][1]['content'])['question']
            verdict = verdicts.get(question, 'pass')
            return {'choices': [{'message': {'tool_calls': [{'function': {'name': 'submit_verdict', 'arguments': json.dumps(
                {'verdict': verdict, 'reason': 'Synthetic ' + verdict})}}]}}]}
        return original(method, base, path, **kwargs)

    deps.json = transport
    deps.suggestion_inputs = []
    deps.on_suggestions = None
    cfg = Config(tmp_path, auth_url='https://auth.example.com', issuer='synthetic', hub_url='https://hub.example.com',
                 hub_token='synthetic-service', consumer_url='https://consumer.example.com', consumer_token='synthetic-evaluation-service',
                 encryption_key=Fernet.generate_key().decode(), judge_url='https://judge.example.com', judge_key='synthetic-judge',
                 judge_model='synthetic', judge_revision='1', modeling_url='https://modeling.example.com',
                 modeling_service_token='synthetic-notification-credential')
    app = create_app(cfg, deps)
    with TestClient(app) as client:
        client.headers['Authorization'] = 'Bearer synthetic-user-a'
        yield client, app, deps, modeling, verdicts


def start(client, prep, questions, request_id):
    cases = client.post('/evaluation/api/eval-sets', json={'name': 'Synthetic ' + request_id, 'cases': [
        {'question': q, 'reference': r} for q, r in questions]}).json()
    response = client.post('/evaluation/api/runs', json={'request_id': request_id, 'preparation_id': prep['id'], 'eval_set_id': cases['id']})
    assert response.status_code == 202, response.text
    return response.json()


def until(check, message=''):
    for _ in range(300):
        value = check()
        if value:
            return value
        time.sleep(.02)
    raise AssertionError(message)


def settled(client, rid, suggestions=('ready', 'failed')):
    def done():
        run = client.get(f'/evaluation/api/runs/{rid}').json()
        artifacts = run.get('artifacts') or {}
        return run if run['status'] not in ('queued', 'preparing', 'running') and artifacts.get('suggestions') in suggestions else None
    return until(done, 'artifacts were not generated')


def test_report_keeps_the_legacy_layout_and_score():
    long_answer = 'A' * 700
    run = {'id': 'a' * 32, 'status': 'interrupted', 'error': {'message': 'Synthetic stop'}, 'eval_set_name': 'Synthetic set',
           'base_ref': {'version': '0.1.1'}, 'package_ref': {'version': '0.1.1-eval.1'}, 'cases': [
               {'question': 'Synthetic pass', 'reference': 'r', 'answer': 'ok', 'status': 'settled', 'judgement': {'verdict': 'pass', 'reason': 'ok'}},
               {'question': 'Synthetic\npartial', 'reference': 'r', 'answer': long_answer, 'status': 'settled', 'judgement': {'verdict': 'partial', 'reason': 'half'}},
               {'question': 'Synthetic error', 'reference': 'r', 'status': 'execution_error', 'error': {'message': 'consumer 执行失败'},
                'trace': {'status': 'complete', 'tool_calls': 3, 'tool_failures': [{'tool_name': 'execute_script', 'error': 'Error: Script exited with code 2. token=synthetic-invalid'}]}},
               {'question': 'Synthetic manual', 'reference': '', 'answer': 'maybe', 'status': 'manual'},
               {'question': 'Synthetic pending', 'reference': 'r', 'status': 'pending'}]}
    summary = summarize(run)
    assert summary == dict(total=5, **{'pass': 1, 'partial': 1, 'fail': 0, 'error': 2, 'manual': 1}, valid=2, score=75, result_status='partial')
    stem = 'eval-20261031-101010-aaaaaa'
    report = render_report(run, stem, 1793500000)
    assert STEM.fullmatch(stem)
    assert '## 总览' in report and '## 逐用例明细' in report and '**75**' in report
    assert '实际运行评测快照 0.1.1-eval.1' in report and '⚠️ 轮次异常终止：Synthetic stop（已执行 3/5 条' in report
    assert '### 2. ⚠️ partial' in report and '### 3. 🛑 error' in report and '### 4. 📝 manual' in report
    assert f'[按结果 ID 查看附件](reports/{stem}.answers.json#case-2)' in report
    # Length and attachment follow the answer quote as their own list, not inside it.
    assert (f"  > {'A' * 600}\n\n  - **原始回答长度**：700 字符；是否截断：是\n"
            f'  - **完整回答**：[按结果 ID 查看附件](reports/{stem}.answers.json#case-2)\n') in report
    assert '  > - **原始回答长度**' not in report
    assert '- **问题**：Synthetic partial' in report
    assert '`execute_script` · 退出码 2' in report and 'token=***' in report and 'synthetic-invalid' not in report
    assert '本用例未执行（轮次已结束）' in report
    questions = [m.group(2) for m in map(CASE_FIELD.match, report.splitlines()) if m]
    assert questions == ['Synthetic pass', 'Synthetic partial', 'Synthetic error', 'Synthetic manual', 'Synthetic pending']


def test_failed_run_gets_report_suggestions_trace_and_notification(service):
    client, app, deps, modeling, verdicts = service
    verdicts['Synthetic failing question'] = 'fail'
    waiting = []

    def progress_now():
        with app.state.store.lock:
            waiting.extend(json.loads(body).get('progress') for (body,) in app.state.store.db.execute("SELECT body FROM artifacts WHERE name='meta'"))
    deps.on_suggestions = progress_now
    record = start(client, preparation(client), [('Synthetic passing question', '2'), ('Synthetic failing question', '3')], 'run_failed')
    run = settled(client, record['id'])
    # While the judge model works, the run page sees which batch is waited for and since when; afterwards nothing.
    assert waiting and waiting[0]['batch'] == 1 and waiting[0]['batches'] == 1 and waiting[0]['started_at'] > 0
    assert run['artifacts']['progress'] is None
    assert run['artifacts']['suggestions'] == 'ready' and run['artifacts']['report'] == 'ready', run['artifacts']
    stem = run['artifacts']['stem']
    assert STEM.fullmatch(stem) and stem.endswith(record['id'][:6])
    report = client.get(f"/evaluation/api/runs/{record['id']}/artifacts/report.md")
    assert report.status_code == 200 and report.headers['x-artifact-stem'] == stem and report.headers['x-artifact-revision'] == '1'
    assert '### 2. ❌ fail' in report.text and '`SCRIPT_ARGUMENT_VALIDATION_FAILED`' in report.text
    assert '`ungrounded_identity arguments.item_code`' in report.text
    # Trace stays out of the bounded result listing but is kept with the full case; tool calls live apart.
    assert 'trace' not in client.get(f"/evaluation/api/runs/{record['id']}/results").json()['items'][0]
    trace = client.get(f"/evaluation/api/runs/{record['id']}/results/1/answer").json()['trace']
    assert trace['tool_calls'] == 2 and 'calls' not in trace
    suggestions = client.get(f"/evaluation/api/runs/{record['id']}/artifacts/suggestions.md").text
    assert '| 1 | 2 | C 数据映射 | Synthetic evidence \\| with pipe |' in suggestions
    assert '`ontology/object/item.md`' in suggestions and '`ontology/object/missing.md`（新建）' in suggestions
    assert 'outside' not in suggestions and '其中 1 条附带工具调用记录' in suggestions
    sent = deps.suggestion_inputs[0]
    assert [case['number'] for case in sent['cases']] == [2] and any(doc['path'] == 'objects/item.md' for doc in sent['documents'])
    # The failed case carries its recorded tool calls in call order, redacted before storage and sending.
    calls = sent['cases'][0]['tool_calls']
    assert [(c['n'], c['tool'], c['status']) for c in calls] == [(1, 'business_data_search', 'completed'), (2, 'execute_script', 'failed')]
    assert calls[0]['input'] == '{"table": "synthetic_items", "columns": ["item_code"]}' and 'token=***' in calls[0]['output']
    assert calls[1]['code'] == 'SCRIPT_ARGUMENT_VALIDATION_FAILED' and 'Synthetic invalid argument' in calls[1]['error']
    owner = app.state.store.db.execute('SELECT owner FROM records WHERE id=?', (record['id'],)).fetchone()[0]
    stored = app.state.store.evidence(owner, record['id'])
    assert sorted(stored) == [0, 1] and stored[1] == {'calls': calls, 'omitted': 0}
    assert 'synthetic-leaked-value' not in json.dumps(sent) + json.dumps(stored)
    context = client.get(f"/evaluation/api/runs/{record['id']}/artifacts/context.json").json()
    assert context['stem'] == stem and context['summary']['score'] == 50 and context['tables'] == {}
    answers = client.get(f"/evaluation/api/runs/{record['id']}/artifacts/answers.json").json()
    assert answers['answers'][1]['resultId'] == 'case-2'
    until(lambda: modeling.received and modeling.received[-1][1]['artifacts']['suggestions'] == 'ready', 'not delivered')
    credential, last = modeling.received[-1]
    assert credential == 'synthetic-notification-credential'
    assert last['run_id'] == record['id'] and last['ontology_id'] == 'knowledge_package_demo' and last['release_id'] == 'release_demo'
    assert last['artifacts']['suggestions'] == 'ready' and last['summary']['fail'] == 1
    assert last['event_id'] == record['id'] + ':' + str(last['sequence'])
    assert 'synthetic-user-a' not in json.dumps(last) and 'fingerprint' not in last['artifacts']
    # Another account cannot read the artifacts.
    client.headers['Authorization'] = 'Bearer synthetic-user-b'
    assert client.get(f"/evaluation/api/runs/{record['id']}/artifacts/report.md").status_code == 404
    # Evidence goes with the run, and a restore that replayed the deletion prunes what is left.
    app.state.store.delete(owner, record['id'])
    assert app.state.store.evidence(owner, record['id']) == {}
    with app.state.store.transaction():
        app.state.store.db.execute('INSERT INTO evidence VALUES(?,?,?,?,?)', ('f' * 32, owner, 0, '{}', 0.0))
    app.state.store.prune_orphans()
    assert app.state.store.evidence(owner, 'f' * 32) == {}


def test_all_passing_run_needs_no_model_and_override_rebuilds_only_the_report(service):
    client, app, deps, modeling, verdicts = service
    record = start(client, preparation(client), [('Synthetic passing question', '2')], 'run_passing')
    run = settled(client, record['id'])
    assert run['artifacts']['revision'] == 1 and not deps.suggestion_inputs
    stem = run['artifacts']['stem']
    assert '无需评测优化' in client.get(f"/evaluation/api/runs/{record['id']}/artifacts/suggestions.md").text
    response = client.post(f"/evaluation/api/runs/{record['id']}/results/0/override",
                           json={'verdict': 'fail', 'reason': 'Synthetic manual review', 'revision': run['revision']})
    assert response.status_code == 200, response.text

    def rebuilt_run():
        current = client.get(f"/evaluation/api/runs/{record['id']}").json()
        return current if current['artifacts']['revision'] == 2 else None

    rebuilt = until(rebuilt_run)
    # Only the report: no model request, the same file name, and suggestions waiting for an explicit regeneration.
    assert not deps.suggestion_inputs
    assert rebuilt['artifacts']['stem'] == stem
    assert rebuilt['artifacts']['suggestions'] == 'pending' and rebuilt['artifacts']['suggestions_stale'] is True
    report = client.get(f"/evaluation/api/runs/{record['id']}/artifacts/report.md").text
    assert '❌ fail' in report and 'Synthetic manual review（人工改判）' in report
    assert rebuilt['summary']['counts']['fail'] == 1
    until(lambda: modeling.received and modeling.received[-1][1]['artifacts']['revision'] == 2)
    assert client.post(f"/evaluation/api/runs/{record['id']}/artifacts/regenerate").status_code == 200

    def regenerated_run():
        current = client.get(f"/evaluation/api/runs/{record['id']}").json()
        ready = current['artifacts']['revision'] == 3 and current['artifacts']['suggestions'] == 'ready'
        return current if ready else None

    regenerated = until(regenerated_run)
    assert deps.suggestion_inputs and regenerated['artifacts']['suggestions_stale'] is False
    # Stopping again without new results does not create a revision.
    owner = app.state.store.db.execute('SELECT owner FROM records WHERE id=?', (record['id'],)).fetchone()[0]
    app.state.engine.on_stop(owner, record['id'], 'synthetic-user-a')
    time.sleep(.1)
    assert client.get(f"/evaluation/api/runs/{record['id']}").json()['artifacts']['revision'] == 3


def test_package_groups_history_lock_and_release_view(service):
    client, app, deps, modeling, verdicts = service
    first = start(client, preparation(client, 'prepare_first'), [('Synthetic passing question', '2')], 'run_first')
    settled(client, first['id'])
    second = start(client, preparation(client, 'prepare_second'), [('Synthetic passing question', '2')], 'run_second')
    settled(client, second['id'])
    tasks = client.get('/evaluation/api/package-tasks').json()
    assert tasks['total'] == 1 and tasks['items'][0]['run_count'] == 2 and tasks['items'][0]['latest']['id'] == second['id']
    assert tasks['items'][0]['executing'] is False
    history = client.get('/evaluation/api/runs', params={'ontology_id': 'knowledge_package_demo'}).json()
    assert [item['id'] for item in history['items']] == [second['id'], first['id']]
    paged = client.get('/evaluation/api/runs', params={'ontology_id': 'knowledge_package_demo', 'limit': 1, 'offset': 1}).json()
    assert [item['id'] for item in paged['items']] == [first['id']] and paged['total'] == 2 and paged['next_offset'] is None
    assert client.get('/evaluation/api/runs', params={'ontology_id': 'knowledge_package_demo', 'limit': 21}).status_code == 422
    assert client.get('/evaluation/api/runs', params={'ontology_id': 'package_other'}).json()['total'] == 0
    view = client.get('/evaluation/api/package-evaluations', params={'ontology_id': 'knowledge_package_demo'}).json()
    assert view['active'] is None and len(view['releases']) == 1
    assert view['releases'][0]['run_id'] == second['id'] and view['releases'][0]['summary']['score'] == 100
    # The caller's owner is the one its notifications carry, so onto-modeling files both under the same user.
    until(lambda: modeling.received, 'not delivered')
    assert view['owner'] == modeling.received[-1][1]['owner']
    # Neither a completed nor an interrupted run keeps the package from a new run.
    original = deps.json
    def failing_judge(method, base, path, **kwargs):
        if path == '/chat/completions' and 'submit_verdict' in json.dumps(kwargs['json']['tool_choice']) and '合成协议检查' not in kwargs['json']['messages'][1]['content']:
            raise Failure('judge_unavailable', 'Unavailable', 503)
        return original(method, base, path, **kwargs)
    deps.json = failing_judge
    third = start(client, preparation(client, 'prepare_third'), [('Synthetic passing question', '2')], 'run_third')
    until(lambda: client.get(f"/evaluation/api/runs/{third['id']}").json()['status'] == 'interrupted')
    deps.json = original
    eval_set = client.get('/evaluation/api/eval-sets').json()['items'][0]['id']
    deps.running = True
    fourth = client.post('/evaluation/api/runs', json={'request_id': 'run_fourth', 'preparation_id': preparation(client, 'prepare_fourth')['id'],
                                                       'eval_set_id': eval_set})
    assert fourth.status_code == 202, fourth.text
    until(lambda: client.get(f"/evaluation/api/runs/{fourth.json()['id']}").json()['status'] == 'running')
    assert client.get('/evaluation/api/package-tasks').json()['items'][0]['executing'] is True
    # An executing run blocks a new run and the resumption of an older run of the same package.
    blocked = client.post('/evaluation/api/runs', json={'request_id': 'run_fifth', 'preparation_id': preparation(client, 'prepare_fifth')['id'],
                                                        'eval_set_id': eval_set})
    assert blocked.status_code == 409 and blocked.json()['code'] == 'package_run_active', blocked.text
    resumed = client.post(f"/evaluation/api/runs/{third['id']}/resume", json={})
    assert resumed.status_code == 409 and resumed.json()['code'] == 'package_run_active', resumed.text
    client.post(f"/evaluation/api/runs/{fourth.json()['id']}/cancel")
    deps.running = False
    until(lambda: client.get(f"/evaluation/api/runs/{fourth.json()['id']}").json()['status'] == 'cancelled')


def test_notification_retries_backs_off_and_stops_on_rejection(service):
    client, app, deps, modeling, verdicts = service
    modeling.status = 503
    record = start(client, preparation(client), [('Synthetic passing question', '2')], 'run_notify')
    settled(client, record['id'])
    rid = record['id']
    def attempted():
        notification = client.get(f'/evaluation/api/runs/{rid}/artifacts').json()['notification']
        return notification if notification['attempts'] >= 1 else None

    status = until(attempted)
    assert status['status'] == 'pending' and status['last_error'] == '建模服务返回 503' and status['next_attempt'] > time.time() + 20
    assert 'modeling.example.com' not in json.dumps(status)
    modeling.status = 200
    assert client.post(f'/evaluation/api/runs/{rid}/notifications/retry').status_code == 200
    until(lambda: client.get(f'/evaluation/api/runs/{rid}/artifacts').json()['notification']['status'] == 'delivered')
    assert len(modeling.received) == 1
    assert backoff(0) == 30 and backoff(20) == 3600


def test_rejected_notification_stops_until_sent_again(service):
    client, app, deps, modeling, verdicts = service
    modeling.status = 422
    record = start(client, preparation(client), [('Synthetic passing question', '2')], 'run_rejected')
    settled(client, record['id'])
    rid = record['id']
    until(lambda: client.get(f'/evaluation/api/runs/{rid}/artifacts').json()['notification']['status'] == 'rejected')
    assert app.state.store.db.execute("SELECT count(*) FROM notifications WHERE status='pending'").fetchone()[0] == 0
    modeling.status = 200
    assert client.post(f'/evaluation/api/runs/{rid}/notifications/retry').json()['status'] in ('pending', 'delivered')
    until(lambda: client.get(f'/evaluation/api/runs/{rid}/artifacts').json()['notification']['status'] == 'delivered')


def test_notifications_are_not_queued_without_credentials(tmp_path):
    deps = Dependencies()
    cfg = Config(tmp_path, auth_url='https://auth.example.com', issuer='synthetic', hub_url='https://hub.example.com',
                 hub_token='synthetic-service', consumer_url='https://consumer.example.com', consumer_token='synthetic-evaluation-service',
                 encryption_key=Fernet.generate_key().decode(), judge_url='https://judge.example.com', judge_key='synthetic-judge',
                 judge_model='synthetic', judge_revision='1', modeling_url='https://modeling.example.com')
    with TestClient(create_app(cfg, deps)) as client:
        client.headers['Authorization'] = 'Bearer synthetic-user-a'
        record = start(client, preparation(client), [('Synthetic passing question', '2')], 'run_disabled')
        run = settled(client, record['id'])
        assert run['notification'] == {'status': 'disabled'}
        assert client.post(f"/evaluation/api/runs/{record['id']}/notifications/retry").status_code == 409


def test_restart_reports_abandoned_suggestions_and_resends_pending(tmp_path, service):
    client, app, deps, modeling, verdicts = service
    record = start(client, preparation(client), [('Synthetic passing question', '2')], 'run_restart')
    settled(client, record['id'])
    rid = record['id']
    db = app.state.store.db
    owner = db.execute('SELECT owner FROM records WHERE id=?', (rid,)).fetchone()[0]
    meta = json.loads(db.execute("SELECT body FROM artifacts WHERE run_id=? AND name='meta'", (rid,)).fetchone()[0])
    meta.update(status='generating', suggestions='generating')
    app.state.store.put_artifacts(owner, rid, meta['revision'], {'meta': json.dumps(meta)})
    app.state.jobs.recover()
    current = client.get(f'/evaluation/api/runs/{rid}').json()['artifacts']
    assert current['status'] == 'failed' and current['suggestions'] == 'failed' and '重新生成' in current['suggestions_error']
    assert client.get(f'/evaluation/api/runs/{rid}/artifacts/suggestions.md').status_code == 404
    regenerated = client.post(f'/evaluation/api/runs/{rid}/artifacts/regenerate').json()
    assert regenerated['status'] == 'generating'
    assert until(lambda: client.get(f'/evaluation/api/runs/{rid}').json()['artifacts']['suggestions'] == 'ready')


def test_regenerate_rejects_active_and_empty_runs(service):
    client, app, deps, modeling, verdicts = service
    deps.running = True
    record = start(client, preparation(client), [('Synthetic passing question', '2')], 'run_active')
    response = client.post(f"/evaluation/api/runs/{record['id']}/artifacts/regenerate")
    assert response.status_code == 409 and response.json()['code'] == 'run_active'
    assert client.get(f"/evaluation/api/runs/{record['id']}/artifacts/report.md").status_code == 404
    view = client.get('/evaluation/api/package-evaluations', params={'ontology_id': 'knowledge_package_demo'}).json()
    assert view['active']['run_id'] == record['id'] and view['active']['release_id'] == 'release_demo'
    client.post(f"/evaluation/api/runs/{record['id']}/cancel")
    deps.running = False
    until(lambda: client.get(f"/evaluation/api/runs/{record['id']}").json()['status'] == 'cancelled')
    response = client.post(f"/evaluation/api/runs/{record['id']}/artifacts/regenerate")
    assert response.status_code == 409 and response.json()['code'] == 'artifacts_unavailable'


def test_deleting_a_run_removes_its_artifacts_and_notifications(service):
    client, app, deps, modeling, verdicts = service
    record = start(client, preparation(client), [('Synthetic passing question', '2')], 'run_deleted')
    settled(client, record['id'])
    assert client.delete(f"/evaluation/api/runs/{record['id']}").status_code == 200
    db = app.state.store.db
    assert db.execute('SELECT count(*) FROM artifacts WHERE run_id=?', (record['id'],)).fetchone()[0] == 0
    assert db.execute('SELECT count(*) FROM notifications WHERE run_id=?', (record['id'],)).fetchone()[0] == 0


class Replay:
    """Synthetic consumer event replay for Consumer.trace."""
    def __init__(self, events):
        self.raw = ('\n\n'.join('data: ' + json.dumps(event) for event in events) + '\n\n').encode()

    def request(self, method, base, path, **kwargs):
        return self.raw


def trace_of(events):
    return Consumer(SimpleNamespace(consumer_url='https://consumer.example.com'), Replay(events)).trace(
        'synthetic-user-a', 'eval-workspace', 'remote_demo')


def test_trace_keeps_bounded_redacted_tool_calls():
    events = []
    for n in range(15):
        events.append({'event_type': 'tool.started', 'payload': {
            'name': 'business_data_search', 'tool_call_id': f'call_{n}', 'input_preview': 'Synthetic query ' + 'x' * 500}})
        finished = {'name': 'business_data_search', 'tool_call_id': f'call_{n}', 'status': 'completed',
                    'output_preview': 'Bearer synthetic-secret-token'}
        if n in (2, 13):
            finished.update(status='error', error={'message': 'Synthetic column missing'}, error_code='COLUMN_NOT_FOUND')
        events.append({'event_type': 'tool.finished', 'payload': finished})
    trace = trace_of(events)
    assert trace['tool_calls'] == 15 and len(trace['calls']) == EVIDENCE_CALLS and trace['omitted_calls'] == 3
    # Failed calls survive the cut; the rest keep call order.
    assert [call['n'] for call in trace['calls']] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 14]
    assert {call['n'] for call in trace['calls'] if call['status'] == 'failed'} == {3, 14}
    assert all(len(call['input']) <= EVIDENCE_PREVIEW + len('…（已截断）') for call in trace['calls'])
    assert trace['calls'][2]['code'] == 'COLUMN_NOT_FOUND' and trace['calls'][2]['error'] == 'Synthetic column missing'
    assert 'synthetic-secret-token' not in json.dumps(trace) and trace['calls'][0]['output'] == 'Bearer ***'
    assert [failure['error'] for failure in trace['tool_failures']] == ['Synthetic column missing'] * 2
    # Without call IDs, a finish pairs with the earliest open call of its tool; an unmatched finish stands alone.
    paired = trace_of([{'event_type': 'tool.started', 'payload': {'name': 'execute_script'}},
                       {'event_type': 'tool.finished', 'payload': {'name': 'execute_script', 'status': 'completed'}},
                       {'event_type': 'tool.finished', 'payload': {'name': 'business_data_search', 'status': 'failed'}}])
    assert paired['calls'] == [{'n': 1, 'tool': 'execute_script', 'status': 'completed'},
                               {'n': 2, 'tool': 'business_data_search', 'status': 'failed'}]


def test_case_input_trims_tool_calls_to_fit_its_share():
    case = {'question': 'Synthetic one', 'reference': 'r' * 1000, 'answer': 'a' * 1000, 'status': 'settled'}
    calls = [{'n': n, 'tool': 'business_data_search', 'status': 'failed' if n == 8 else 'completed', 'input': 'x' * 60}
             for n in range(1, 9)]

    def item(stored, limit):
        return suggestions.case_input(1, case, 'fail', 'Synthetic', stored, limit)

    base = suggestions.size(item(None, 10 ** 6))
    # Six calls at most, the failed one kept, while they fit; then failed calls only; then none.
    roomy = item({'calls': calls, 'omitted': 2}, 10 ** 6)
    assert [call['n'] for call in roomy['tool_calls']] == [1, 2, 3, 4, 5, 8] and roomy['tool_calls_omitted'] == 4
    tight = item({'calls': calls, 'omitted': 2}, base + 200)
    assert [call['n'] for call in tight['tool_calls']] == [8] and tight['tool_calls_omitted'] == 9
    full = item({'calls': calls, 'omitted': 2}, base + 50)
    assert 'tool_calls' not in full and full['tool_calls_omitted'] == 10
    # A case larger than its share keeps the question and shortens the long fields.
    assert len(item(None, 1500)['answer']) < 320
    # Recorded without calls: an empty list; nothing recorded: no field at all.
    assert item({'calls': [], 'omitted': 0}, 10 ** 6)['tool_calls'] == []
    assert 'tool_calls' not in item(None, 10 ** 6) and 'tool_calls_omitted' not in item(None, 10 ** 6)
