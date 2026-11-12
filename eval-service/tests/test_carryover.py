"""Carrying suggestions modeling has not handled into the next push. All runs, questions and packages are synthetic."""
import json
import re

from evaluation import artifacts, carryover
from evaluation.feedback.report import render as render_feedback
from evaluation.core import digest
from evaluation.suggestions import passed
from test_evaluation import preparation, synthetic_package
from test_feedback import PACKAGE, flywheel, push, sealed, signal  # noqa: F401  (fixture)
from test_modeling_result import published, write_back
from test_run_artifacts import service, settled, start, until  # noqa: F401  (fixture)

NOW = 1793930000.0
STEM = 'eval-20261106-095320-a1b2c3'


def case(question, verdict, reason='Synthetic reason', status='settled'):
    value = dict(question=question, reference='Synthetic reference', answer='Synthetic answer', status=status)
    if verdict:
        value['judgement'] = dict(verdict=verdict, reason=reason, source='judge')
    else:
        value['error'] = dict(message='Synthetic service unavailable')
    return value


def run(rid, version, cases, **extra):
    ref = dict(ontology_id='package_demo', release_id='release_' + version, version=version)
    return dict(id=rid * 32, status='completed', package_ref=ref, base_ref=ref, cases=cases, created_at=NOW, **extra)


def row(cases, category='B', suggestions=()):
    return dict(cases=list(cases), suggestions=list(suggestions), category=category, evidence='Synthetic evidence',
                suggestion='Synthetic generic revision', targets=['`ontology/object/item.md`'])


def waiting(handed):
    return dict(status='notified', updated_at=handed)


def blocks(report):
    return [int(n) for n in re.findall(r'^### (\d+)\. ', report, flags=re.M)]


def own_suggestions(own, rows):
    lines = ['# 评测优化建议', '', '> 依据评测报告：synthetic', '', '## 总体判断', '', 'Synthetic', '', '## 失分归因与修订建议', '',
             carryover.HEADER, '|---|---|---|---|---|---|', *[carryover.row(n, item) for n, item in enumerate(rows, 1)],
             '', '## 说明', '', '- Synthetic note', '']
    return '\n'.join(lines)


def test_this_run_and_newer_runs_decide_which_earlier_questions_are_carried():
    own = run('c', '0.1.4', [case('Synthetic fixed', 'pass'), case('Synthetic still wrong', 'fail'),
                             case('Synthetic unanswered', None, status='execution_error')])
    newer = run('b', '0.1.3', [case('Synthetic still wrong', 'fail'), case('Synthetic newer only', 'partial'),
                               case('Synthetic outage', 'fail')])
    older = run('a', '0.1.2', [case('Synthetic fixed', 'fail'), case('Synthetic newer only', 'fail'),
                               case('Synthetic unanswered', 'fail'), case('Synthetic older only', 'fail'),
                               case('Synthetic passed then', 'pass')])
    value = carryover.plan(own, [(newer, [row([1, 2]), row([3], 'E')], waiting(NOW - 10)),
                                 (older, [row([1, 2]), row([3, 4], 'A')], waiting(NOW - 20))])
    carried = [c['question'] for _, c in value['keep']]
    # Fixed here, judged here, or carried from the newer run: not again. An error here says nothing, so it is carried.
    assert carried == ['Synthetic newer only', 'Synthetic unanswered', 'Synthetic older only']
    newer_round, older_round = value['rounds']
    # Only attributed to a runtime error: neither the question nor its row is carried.
    assert [r['cases'] for r in newer_round['rows']] == [[4]] and 'Synthetic outage' not in carried
    assert newer_round['rows'][0]['evidence'].startswith('（原来引用的另外 1 条问题')
    assert [r['cases'] for r in older_round['rows']] == [[5, 6]] and older_round['cases'] == 2


def test_every_waiting_run_is_carried_however_many_there_are():
    own = run('f', '0.1.4', [case('Synthetic own', 'fail')])
    earlier = [(run(chr(ord('0') + i), '0.1.0', [case(f'Synthetic earlier {i}', 'fail')]), [row([1])], waiting(NOW - i))
               for i in range(1, 8)]
    value = carryover.plan(own, earlier)
    shown = carryover.public(value)
    assert [r['run_id'][0] for r in shown['rounds']] == ['1', '2', '3', '4', '5', '6', '7'] and shown['slid_out'] == []
    report = carryover.report(artifacts.render_report(own, STEM, NOW), own, value, STEM, NOW)
    assert blocks(report) == list(range(1, 9)) and '没有带上' not in report


def test_spliced_files_number_on_and_keep_the_overview_of_this_run():
    own = run('b', '0.1.4', [case('Synthetic own pass', 'pass'), case('Synthetic own fail', 'fail')])
    earlier = run('a', '0.1.3', [case('Synthetic earlier fail', 'fail'), case('Synthetic own pass', 'fail')])
    value = carryover.plan(own, [(earlier, [row([1, 2])], waiting(NOW - 60))])
    own_report = artifacts.render_report(own, STEM, NOW)
    report = carryover.report(own_report, own, value, STEM, NOW)
    def overview(text):
        return text.split('## 总览')[1].split('## 逐用例明细')[0]

    assert overview(report) == overview(own_report) and blocks(report) == [1, 2, 3]
    assert '- **问题**：Synthetic earlier fail' in report and '**历史来源**：评测轮次 aaaaaaaaaaaa（版本 0.1.3）' in report
    answers = json.loads(carryover.answers(artifacts.render_answers(own), own, value))['answers']
    assert [a['resultId'] for a in answers] == ['case-1', 'case-2', 'case-3']
    text = carryover.suggestions(own_suggestions(own, [row([2])]), own, value)
    assert '## 历史未处理建议（共 1 条，来自 1 个评测轮次）' in text and '| 2 | 3 | B 语义模糊 |' in text
    assert text.index('## 历史未处理建议') < text.index('## 说明')


def test_a_run_that_passed_everything_still_pushes_what_earlier_runs_wait_for():
    own = run('b', '0.1.4', [case('Synthetic own pass', 'pass')])
    earlier = run('a', '0.1.3', [case('Synthetic earlier fail', 'fail')])
    value = carryover.plan(own, [(earlier, [row([1])], waiting(NOW - 60))])
    text = carryover.suggestions(passed(own, STEM, NOW, artifacts.summarize(own)), own, value)
    assert '本轮无需评测优化；下面带上了历史轮次还没处理的建议。' in text
    assert '| 1 | 2 | B 语义模糊 |' in text and text.rstrip().endswith('不得照抄用例问题或标准答案。')


def test_feedback_runs_carry_questions_and_new_user_suggestions():
    def feedback_run(rid, cases, proposals):
        value = run(rid, '0.1.4', cases, origin='consumer_feedback')
        value['feedback'] = dict(clusters=[], suggestions=proposals, stats={}, excluded={'runtime_error': 1})
        return value
    own = feedback_run('b', [case('Synthetic own question', 'fail')],
                       [dict(cluster_id='c1', content='Synthetic same proposal', scope='entity:order', users=1)])
    earlier = feedback_run('a', [case('Synthetic earlier question', 'partial')],
                           [dict(cluster_id='c2', content='Synthetic same proposal', scope='entity:order', users=2),
                            dict(cluster_id='c3', content='Synthetic new proposal', scope='entity:refund', users=1)])
    value = carryover.plan(own, [(earlier, [row([1], suggestions=[1, 2])], waiting(NOW - 60))])
    assert [item['content'] for _, item in value['proposals']] == ['Synthetic new proposal']
    assert value['rounds'][0]['rows'][0]['suggestions'] == [2]
    report = carryover.report(render_feedback(own, STEM, NOW), own, value, STEM, NOW)
    # Carried questions come before the user suggestions; carried proposals continue their table.
    assert report.index('Synthetic earlier question') < report.index('## 用户建议') < report.index('## 未纳入本次优化')
    assert '| 2 | Synthetic new proposal（历史，版本 0.1.4） | 对象：refund | 1 |' in report
    assert '反馈轮次 aaaaaaaaaaaa（版本 0.1.4）' in report


def test_nothing_waiting_leaves_every_file_as_it_was():
    own = run('b', '0.1.4', [case('Synthetic own fail', 'fail')])
    value = carryover.plan(own, [])
    report = artifacts.render_report(own, STEM, NOW)
    assert carryover.public(value) is None and carryover.report(report, own, value, STEM, NOW) == report
    text = own_suggestions(own, [row([1])])
    assert carryover.suggestions(text, own, value) == text


def owner_of(app, rid):
    return app.state.store.db.execute('SELECT owner FROM records WHERE id=?', (rid,)).fetchone()[0]


def test_an_evaluation_push_carries_what_the_user_s_earlier_runs_wait_for(service):
    client, app, deps, modeling, verdicts = service
    verdicts.update({'Synthetic earlier failure': 'fail', 'Synthetic fixed later': 'fail', 'Synthetic own failure': 'fail'})
    prep = preparation(client)
    first = start(client, prep, [('Synthetic earlier failure', '2'), ('Synthetic fixed later', '2')], 'run_carry_first')
    first_run = settled(client, first['id'])
    # Nothing waited: the files are the run's own, byte for byte.
    store, owner = app.state.store, owner_of(app, first['id'])
    assert first_run['artifacts']['carryover'] is None
    assert store.artifact(owner, first['id'], 'suggestions.md')[1] == store.artifact(owner, first['id'], carryover.OWN)[1]
    verdicts['Synthetic fixed later'] = 'pass'
    second = start(client, prep, [('Synthetic fixed later', '2'), ('Synthetic own failure', '2')], 'run_carry_second')
    second_run = settled(client, second['id'])
    plan = second_run['artifacts']['carryover']
    assert [(r['run_id'], r['cases'], r['items']) for r in plan['rounds']] == [(first['id'], 1, 1)]
    report = client.get(f"/evaluation/api/runs/{second['id']}/artifacts/report.md").text
    assert blocks(report) == [1, 2, 3] and report.count('Synthetic fixed later') == 1
    assert '- **问题**：Synthetic earlier failure' in report and '**历史来源**：评测轮次' in report
    answers = client.get(f"/evaluation/api/runs/{second['id']}/artifacts/answers.json").json()['answers']
    assert answers[-1]['resultId'] == 'case-3' and answers[-1]['question'] == 'Synthetic earlier failure'
    suggestions = client.get(f"/evaluation/api/runs/{second['id']}/artifacts/suggestions.md").text
    assert '## 历史未处理建议（共 1 条，来自 1 个评测轮次）' in suggestions and '| 2 | 3 | C 数据映射 |' in suggestions
    # The internal own copies are not downloadable.
    assert client.get(f"/evaluation/api/runs/{second['id']}/artifacts/{carryover.OWN}").status_code == 404
    earlier = client.get(f"/evaluation/api/runs/{first['id']}").json()['optimization']
    assert earlier['status'] == 'notified' and earlier['carried_into']['run_id'] == second['id']
    # Modeling handled the push: the carried run is handled with it, and retries change nothing.
    published(deps)
    assert write_back(client, second['id'], release_id='release_demo_next').status_code == 200
    earlier = client.get(f"/evaluation/api/runs/{first['id']}").json()['optimization']
    assert earlier['status'] == 'modeling_done' and earlier['result']['via'] == second['id']
    assert re.match(r'^随 \d{2}-\d{2} \d{2}:\d{2} 交付的评测轮次一起处理', earlier['result']['note']) and earlier['result']['version'] == '0.1.2'
    assert write_back(client, second['id'], release_id='release_demo_next').status_code == 200


def test_runs_modeling_failed_to_handle_are_not_carried(service):
    client, app, deps, modeling, verdicts = service
    verdicts.update({'Synthetic failed earlier': 'fail'})
    prep = preparation(client)
    first = start(client, prep, [('Synthetic failed earlier', '2')], 'run_failed_first')
    settled(client, first['id'])
    response = client.post(f"/evaluation/api/runs/{first['id']}/modeling-result",
                           json={'request_id': 'result_failed', 'outcome': 'failed', 'note': 'Synthetic failure'})
    assert response.status_code == 200
    second = start(client, prep, [('Synthetic question', '2')], 'run_failed_second')
    assert settled(client, second['id'])['artifacts']['carryover'] is None
    assert client.get(f"/evaluation/api/runs/{second['id']}/carryover").json() == {'carryover': None, 'preview': False}


def test_runs_handed_over_earliest_are_left_out_until_the_files_fit(service, monkeypatch):
    client, app, deps, modeling, verdicts = service
    verdicts.update({'Synthetic first': 'fail', 'Synthetic second': 'fail'})
    prep = preparation(client)
    first = start(client, prep, [('Synthetic first', '2')], 'run_size_first')
    settled(client, first['id'])
    own = len(client.get(f"/evaluation/api/runs/{first['id']}/artifacts/report.md").content)
    # Room for this run's own report and a note, but not for a carried question as well.
    monkeypatch.setitem(artifacts.LIMITS, 'report.md', own + 400)
    second = start(client, prep, [('Synthetic second', '2')], 'run_size_second')
    plan = settled(client, second['id'])['artifacts']['carryover']
    assert plan['rounds'] == [] and [r['run_id'] for r in plan['slid_out']] == [first['id']]
    assert app.state.store.optimization(first['id'])['slid_out']['run_id'] == second['id']


def test_confirming_a_feedback_run_carries_earlier_confirmed_runs(flywheel):
    env = flywheel
    client = env.client

    def review(run):
        rid = run['id']
        response = client.post(f'/evaluation/api/runs/{rid}/artifacts/regenerate')
        assert response.status_code == 200, response.text
        until(lambda: client.get(f'/evaluation/api/runs/{rid}').json()['artifacts']['suggestions'] == 'ready', 'suggestions')
        return rid

    push(env, [signal(run=f'run_{n}', question=f'Synthetic earlier question {n}', feedback={'comment': 'Synthetic wrong'})
               for n in (1, 2, 3)])
    first = review(sealed(env))
    assert client.post(f'/evaluation/api/runs/{first}/notifications/retry', json={'decision': 'confirm'}).status_code == 200
    first_meta = client.get(f'/evaluation/api/runs/{first}').json()['artifacts']
    assert first_meta['carryover'] is None
    push(env, [signal(run=f'run_{n}', question=f'Synthetic later question {n}', feedback={'comment': 'Synthetic wrong'})
               for n in (4, 5, 6)])
    later = sealed(env)
    assert later['id'] != first
    second = review(later)
    # Before the owner confirms, the run's files are its own and the page can tell what confirming would carry.
    preview = client.get(f'/evaluation/api/runs/{second}/carryover').json()
    assert preview['preview'] is True and [r['run_id'] for r in preview['carryover']['rounds']] == [first]
    before = client.get(f'/evaluation/api/runs/{second}').json()['artifacts']
    assert '拼接历史建议' not in client.get(f'/evaluation/api/runs/{second}/artifacts/report.md').text
    assert client.post(f'/evaluation/api/runs/{second}/notifications/retry', json={'decision': 'confirm'}).status_code == 200
    after = client.get(f'/evaluation/api/runs/{second}').json()
    assert after['artifacts']['revision'] == before['revision'] + 1
    assert [r['run_id'] for r in after['artifacts']['carryover']['rounds']] == [first]
    report = client.get(f'/evaluation/api/runs/{second}/artifacts/report.md').text
    assert '反馈轮次' in report and 'Synthetic earlier question 1' in report and blocks(report) == list(range(1, 7))
    suggestions = client.get(f'/evaluation/api/runs/{second}/artifacts/suggestions.md').text
    assert '## 历史未处理建议（共 1 条，来自 1 个反馈轮次）' in suggestions
    def matching_notification():
        for _, body in env.modeling.received:
            if body['run_id'] == second and body['artifacts']['revision'] == after['artifacts']['revision']:
                return body
        return None

    body = until(matching_notification, 'notification')
    assert body['artifacts']['carryover']['rounds'][0]['run_id'] == first
    assert client.get(f'/evaluation/api/runs/{first}').json()['optimization']['carried_into']['run_id'] == second


def test_only_runs_of_the_same_version_are_carried():
    older, newer = run('a', '0.1.1', []), run('b', '0.1.2', [])
    assert carryover.same_version(older, run('c', '0.1.1', []))
    assert not carryover.same_version(older, newer)
    # Feedback reports a version before its release is resolved; the version decides then.
    reported = dict(older, base_ref={'ontology_id': 'package_demo', 'version': '0.1.1'})
    assert carryover.same_version(reported, older) and not carryover.same_version(reported, newer)
    # An evaluation snapshot counts as the release it was derived from.
    snapshot = dict(older, base_ref=None, package_ref={'kind': 'evaluation_snapshot', 'base_release_id': 'release_0.1.1',
                                                       'release_id': 'release_0.1.1-eval.1', 'version': '0.1.1-eval.1'})
    assert carryover.same_version(snapshot, older)


def test_an_evaluation_push_never_carries_another_version(service):
    client, app, deps, modeling, verdicts = service
    verdicts.update({'Synthetic old version failure': 'fail', 'Synthetic new version failure': 'fail'})
    first = start(client, preparation(client), [('Synthetic old version failure', '2')], 'run_version_old')
    settled(client, first['id'])
    raw = synthetic_package('0.1.2')
    deps.releases['release_demo_next'] = (raw, dict(deps.receipt, release_id='release_demo_next', version='0.1.2', package_digest=digest(raw)))
    prep = client.post('/evaluation/api/package-preparations', json={'request_id': 'prepare_next', 'package_ref': {
        'ontology_id': 'knowledge_package_demo', 'release_id': 'release_demo_next'}}).json()
    second = start(client, prep, [('Synthetic new version failure', '2')], 'run_version_new')
    assert settled(client, second['id'])['artifacts']['carryover'] is None
    report = client.get(f"/evaluation/api/runs/{second['id']}/artifacts/report.md").text
    assert 'Synthetic old version failure' not in report and '拼接历史建议' not in report
    # The older version keeps waiting on its own, and modeling lists each version separately.
    state = client.get(f"/evaluation/api/runs/{first['id']}").json()['optimization']
    assert state['status'] == 'notified' and 'carried_into' not in state
    releases = client.get('/evaluation/api/package-evaluations', params={'ontology_id': 'knowledge_package_demo'}).json()['releases']
    assert {(entry['version'], entry['run_id']) for entry in releases} == {('0.1.1', first['id']), ('0.1.2', second['id'])}


def test_feedback_on_two_versions_reaches_modeling_as_two_versions(flywheel):
    env = flywheel
    client = env.client
    raw = synthetic_package('0.1.2')
    env.deps.releases['release_demo_next'] = (raw, dict(env.deps.receipt, release_id='release_demo_next', version='0.1.2',
                                                        package_digest=digest(raw)))

    def deliver(version, start):
        push(env, [signal(run=f'run_{version}_{n}', question=f'Synthetic {version} question {n}', version=version,
                          feedback={'comment': 'Synthetic wrong'}) for n in range(start, start + 3)])
        env.app.state.feedback.process()
        def sealed_run():
            records = client.get('/evaluation/api/runs', params={
                'origin': 'consumer_feedback', 'ontology_id': PACKAGE}).json()['items']
            for record in records:
                if record['package_ref']['version'] != version:
                    continue
                if record['status'] == 'completed' and record['artifacts']:
                    return record['id']
            return None

        rid = until(sealed_run, 'sealed')
        response = client.post(f'/evaluation/api/runs/{rid}/artifacts/regenerate')
        assert response.status_code == 200, response.text
        until(lambda: client.get(f'/evaluation/api/runs/{rid}').json()['artifacts']['suggestions'] == 'ready', 'suggestions')
        assert client.post(f'/evaluation/api/runs/{rid}/notifications/retry', json={'decision': 'confirm'}).status_code == 200
        return until(lambda: (lambda r: r if r['optimization']['status'] == 'notified' else None)(
            client.get(f'/evaluation/api/runs/{rid}').json()), 'delivered')

    older = deliver('0.1.1', 1)
    newer = deliver('0.1.2', 10)
    # The newer version's push carries nothing of the older one; each waits for modeling on its own version.
    assert newer['artifacts']['carryover'] is None
    assert 'Synthetic 0.1.1' not in client.get(f"/evaluation/api/runs/{newer['id']}/artifacts/report.md").text
    assert 'carried_into' not in client.get(f"/evaluation/api/runs/{older['id']}").json()['optimization']
    listed = client.get('/evaluation/api/package-evaluations', params={'ontology_id': PACKAGE, 'origin': 'all'}).json()['releases']
    assert {(entry['version'], entry['run_id']) for entry in listed if entry['origin'] == 'consumer_feedback'} == {
        ('0.1.1', older['id']), ('0.1.2', newer['id'])}
