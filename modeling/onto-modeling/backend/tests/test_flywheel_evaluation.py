"""Publish, evaluate, optimize again: the loop runs until every case of the evaluation set passes.

Hub, the evaluation service and the assistant are the synthetic stubs of test_flywheel; all data is synthetic.
"""
import re
import time

import pytest

from app.services import hub_packages
from app.services.delegated import Delegated
from app.services.flywheel import background, evaluation, intake, publish, store
from app.services.workspace import WorkspaceError
from test_flywheel import (NEXT_RUN, OBJECT, RUN, SUBJECT, THIRD_RUN, act, env, optimized, published, run_of, settings,
                           sync)

ALL_PASS = {'pass': 4, 'partial': 0, 'fail': 0}
THREE_OF_FOUR = {'pass': 3, 'partial': 0, 'fail': 1}


def revise(env, line):
    """The synthetic assistant revises a document and ends its turn."""
    (env['root'] / 'ontology/object/object_a.md').write_text(OBJECT + f'\n- {line}\n', encoding='utf-8')
    env['assistant'].finish()


def settle_publication(env, run_id):
    sync(env, reconcile=False)
    publish.wait(env['pid'], SUBJECT, run_id, timeout=30)
    return sync(env, reconcile=False)


def history(env, run_id):
    return [item['title'] for item in env['client'].get(f"/api/projects/{env['pid']}/flywheel/runs/{run_id}").json()['history']]


def test_the_loop_runs_until_every_case_passes(env):
    settings(env, optimize=True, publish=True, max_rounds=3)
    env['evaluation'].complete(RUN)
    assert run_of(sync(env), 'FW-001')['status'] == 'optimizing'
    revise(env, '第一轮合成修订')
    view = settle_publication(env, 'FW-001')
    assert run_of(view, 'FW-001')['status'] == 'evaluating'
    # 第一轮新版本仍有失分：结果作为新运行继续优化
    env['evaluation'].complete(NEXT_RUN, release='release_2', version='0.1.2', finished=3000.0, cases=THREE_OF_FOUR)
    view = sync(env)
    first, second = run_of(view, 'FW-001'), run_of(view, 'FW-002')
    assert first['status'] == 'published' and first['evaluation']['passed'] is False and first['after_score'] == 75
    assert second['trigger'] == 'flywheel' and second['source_id'] == 'FW-001' and second['status'] == 'optimizing'
    revise(env, '第二轮合成修订')
    view = settle_publication(env, 'FW-002')
    assert run_of(view, 'FW-002')['status'] == 'evaluating'
    # 第二轮全部通过：循环结束
    env['evaluation'].complete(THIRD_RUN, release='release_3', version='0.1.3', finished=5000.0, score=100, cases=ALL_PASS)
    view = sync(env)
    assert run_of(view, 'FW-002')['status'] == 'published' and run_of(view, 'FW-002')['evaluation']['passed'] is True
    assert run_of(view, 'FW-003')['status'] == 'passed' and view['active_id'] is None
    assert len(env['assistant'].sent) == 2 and view['package']['version'] == '0.1.3'
    # 每一轮都用上一轮的评测配置评测新发布的正式版本
    reruns = env['evaluation'].reruns
    assert [(item['source'], item['package_ref']['release_id']) for item in reruns] == [
        (RUN, 'release_2'), (NEXT_RUN, 'release_3')]
    # 运行记录写明了评测的发起与结果
    titles = history(env, 'FW-001')
    assert '自动发起评测' in titles and '评测完成' in titles
    assert '评测集全部通过' in history(env, 'FW-003')


def test_the_round_limit_stops_the_loop(env):
    settings(env, optimize=True, publish=True, max_rounds=1)
    env['evaluation'].complete(RUN)
    sync(env)
    revise(env, '合成修订')
    settle_publication(env, 'FW-001')
    env['evaluation'].complete(NEXT_RUN, release='release_2', version='0.1.2', finished=3000.0, score=75)
    held = run_of(sync(env), 'FW-002')
    assert held['status'] == 'waiting_optimize' and held['reason'] == 'round_limit' and held['human_hold'] is True
    assert len(env['assistant'].sent) == 1


def test_with_the_switch_off_the_new_version_is_evaluated_once_confirmed(env):
    env['evaluation'].complete(RUN)
    sync(env)
    optimized(env)
    run = run_of(published(env), 'FW-001')
    assert run['status'] == 'waiting_evaluation' and run['human_hold'] is True and env['evaluation'].reruns == []
    response = env['client'].post(f"/api/projects/{env['pid']}/flywheel/runs/FW-001/evaluate",
                                  headers={'X-Refresh-Token': 'browser-refresh'})
    assert response.status_code == 200, response.text
    run = run_of(response.json(), 'FW-001')
    assert run['status'] == 'evaluating' and run['evaluation']['mode'] == 'manual'
    # 人工确认时用浏览器当前的登录发起：评测服务拿到转交的刷新令牌，评测跑完不需要页面开着
    rerun = env['evaluation'].reruns[0]
    assert rerun['token'] == 'user-token' and rerun['refresh'] == 'browser-refresh'
    assert rerun['request_id'] == evaluation.request_id(env['pid'], {'id': 'FW-001'}, 1)
    assert '人工确认评测' in history(env, 'FW-001')


def test_skipping_the_evaluation_ends_the_run(env):
    env['evaluation'].complete(RUN)
    sync(env)
    optimized(env)
    published(env)
    response = env['client'].post(f"/api/projects/{env['pid']}/flywheel/runs/FW-001/skip-evaluation")
    run = run_of(response.json(), 'FW-001')
    assert run['status'] == 'published' and run['evaluation']['status'] == 'skipped'
    assert env['client'].post(f"/api/projects/{env['pid']}/flywheel/runs/FW-001/evaluate").status_code == 409


def test_an_evaluation_that_stops_without_a_result_can_be_retried(env):
    env['evaluation'].complete(RUN)
    sync(env)
    optimized(env)
    published(env)
    env['client'].post(f"/api/projects/{env['pid']}/flywheel/runs/FW-001/evaluate")
    started = store.find(store.load(env['pid'], SUBJECT), 'FW-001')['evaluation']['run_id']
    env['evaluation'].statuses[started] = 'failed'
    with store.update(env['pid'], SUBJECT) as current:
        store.find(current, 'FW-001')['evaluation']['checked_at'] -= evaluation.STATUS_POLL_SECONDS + 1
    run = run_of(sync(env, reconcile=False), 'FW-001')
    assert run['status'] == 'blocked' and run['reason'] == 'evaluation_failed'
    run = run_of(env['client'].post(f"/api/projects/{env['pid']}/flywheel/runs/FW-001/evaluate").json(), 'FW-001')
    assert run['status'] == 'evaluating' and env['evaluation'].reruns[-1]['request_id'] == evaluation.request_id(
        env['pid'], {'id': 'FW-001'}, 2)


def status_due(env, run_id='FW-001'):
    with store.update(env['pid'], SUBJECT) as current:
        store.find(current, run_id)['evaluation']['checked_at'] -= evaluation.STATUS_POLL_SECONDS + 1


def test_an_evaluation_deleted_on_the_evaluation_page_can_be_retried_or_skipped(env):
    env['evaluation'].complete(RUN)
    sync(env)
    optimized(env)
    published(env)
    env['client'].post(f"/api/projects/{env['pid']}/flywheel/runs/FW-001/evaluate")
    # 评测服务里已经没有这次评测（桩对未知的评测返回 404）：用户本人的登录也查不到时，不会再有结果
    status_due(env)
    run = run_of(sync(env, reconcile=False), 'FW-001')
    assert run['status'] == 'blocked' and run['reason'] == 'evaluation_failed'
    assert run['evaluation']['status'] == 'missing' and '评测任务已不存在' in history(env, 'FW-001')
    run = run_of(env['client'].post(f"/api/projects/{env['pid']}/flywheel/runs/FW-001/evaluate").json(), 'FW-001')
    assert run['status'] == 'evaluating'
    status_due(env)
    sync(env, reconcile=False)
    run = run_of(act(env, 'FW-001', 'skip-evaluation').json(), 'FW-001')
    assert run['status'] == 'published' and run['evaluation']['status'] == 'skipped'


def test_a_delegation_that_cannot_find_the_evaluation_keeps_waiting_for_the_user(env):
    settings(env, optimize=True, publish=True)
    env['evaluation'].complete(RUN)
    sync(env)
    revise(env, '合成修订')
    assert run_of(settle_publication(env, 'FW-001'), 'FW-001')['status'] == 'evaluating'
    # 委托只读本知识包的评测：它查不到不能说明评测已不存在，留给用户本人的登录判断
    status_due(env)
    background.run_round(env['manager'], env['assistant'])
    assert store.find(store.load(env['pid'], SUBJECT), 'FW-001')['status'] == 'evaluating'
    # 后台这次没查到不推迟用户的检查；后台自己也不会每一轮都再查
    calls = len(env['evaluation'].calls)
    background.run_round(env['manager'], env['assistant'])
    assert not [path for _, path, _ in env['evaluation'].calls[calls:] if path.startswith('/runs/0')]
    assert run_of(sync(env, reconcile=False), 'FW-001')['reason'] == 'evaluation_failed'


def test_waiting_for_a_running_evaluation_can_end_and_its_result_still_arrives(env):
    env['evaluation'].complete(RUN)
    sync(env)
    optimized(env)
    published(env)
    env['client'].post(f"/api/projects/{env['pid']}/flywheel/runs/FW-001/evaluate")
    response = act(env, 'FW-001', 'skip-evaluation')
    assert response.status_code == 200, response.text
    run = run_of(response.json(), 'FW-001')
    assert run['status'] == 'published' and run['evaluation']['status'] == 'skipped'
    assert response.json()['active_id'] is None
    assert '不再等待评测' in history(env, 'FW-001')
    # 评测服务里的那次评测照常完成：结果作为新一轮进入飞轮
    env['evaluation'].complete(NEXT_RUN, release='release_2', version='0.1.2', finished=3000.0, cases=THREE_OF_FOUR)
    view = sync(env)
    assert run_of(view, 'FW-002')['source_id'] == 'FW-001' and run_of(view, 'FW-002')['status'] == 'waiting_optimize'
    assert run_of(view, 'FW-001')['status'] == 'published' and run_of(view, 'FW-001')['after_score'] == 75


def test_without_a_stored_login_the_automatic_evaluation_waits_for_confirmation(env):
    # 设置时没有转交刷新令牌：评测服务只登记读取，没有登录
    response = env['client'].put(f"/api/projects/{env['pid']}/flywheel/settings", json={'publish': True})
    view = response.json()
    assert view['delegation']['login'] == 'missing' and view['delegation']['background'] is False
    env['evaluation'].complete(RUN)
    sync(env)
    optimized(env)
    with store.update(env['pid'], SUBJECT) as current:
        store.find(current, 'FW-001')['publish_hold'] = False
    run = run_of(settle_publication(env, 'FW-001'), 'FW-001')
    assert run['status'] == 'waiting_evaluation' and run['human_hold'] is True and env['evaluation'].reruns == []
    assert '等待确认评测' in history(env, 'FW-001')


def test_a_voided_login_holds_the_evaluation_and_asks_for_the_switch_again(env):
    settings(env, publish=True)
    env['evaluation'].complete(RUN)
    sync(env)
    optimized(env)
    with store.update(env['pid'], SUBJECT) as current:
        store.find(current, 'FW-001')['publish_hold'] = False
    env['evaluation'].rerun_failure = WorkspaceError('后台评测授权已失效', 401, 'login_expired')
    view = settle_publication(env, 'FW-001')
    run = run_of(view, 'FW-001')
    assert run['status'] == 'waiting_evaluation' and run['human_hold'] is True
    assert view['delegation']['login'] == 'missing'
    assert any('后台评测登录已失效' in item['description'] for item in view['audit'])
    # 重新打开开关时转交新的刷新令牌，登录恢复
    settings(env, publish=False)
    assert settings(env, publish=True)['delegation']['login'] == 'active'


def test_a_newer_version_skips_the_pending_evaluation(env):
    env['evaluation'].complete(RUN)
    sync(env)
    optimized(env)
    published(env)
    hub_packages.publish(env['manager'], env['pid'], 'request_web', 'user-token', SUBJECT, True)
    run = run_of(sync(env, reconcile=False), 'FW-001')
    assert run['status'] == 'published' and '跳过评测' in history(env, 'FW-001')


def test_a_late_record_of_the_runs_own_version_does_not_skip_its_evaluation():
    published = {'id': 'FW-001', 'status': 'waiting_evaluation', 'reason': '', 'history': [],
                 'published_release_id': 'release_2', 'published_committed_at': '2026-11-01T00:10:00+00:00'}
    state = {'runs': [published]}
    # 同步在发布完成前读到了旧的关联（或关联留给维护补记）：它比运行自己发布的版本早，不是“更新的版本”
    intake.expire(state, 'release_one', '2026-10-31T00:00:00+00:00')
    assert published.get('status') == 'waiting_evaluation'
    intake.expire(state, 'release_3', '2026-11-02T00:00:00+00:00')
    assert published.get('status') == 'published'
    # 早于记下提交时间的运行无从比较：版本不同就按原规则跳过
    earlier = {'id': 'FW-002', 'status': 'waiting_evaluation', 'reason': '', 'history': [],
               'published_release_id': 'release_2'}
    intake.expire({'runs': [earlier]}, 'release_one', '2026-10-31T00:00:00+00:00')
    assert earlier.get('status') == 'published'


def test_an_evaluation_that_already_passes_needs_no_optimization(env):
    settings(env, optimize=True)
    env['evaluation'].complete(RUN, score=100, cases=ALL_PASS)
    run = run_of(sync(env), 'FW-001')
    assert run['status'] == 'passed' and env['assistant'].sent == []


def test_automatic_evaluation_uses_the_stored_login_not_the_page_login(env):
    settings(env, optimize=True, publish=True)
    env['evaluation'].complete(RUN)
    sync(env)
    revise(env, '合成修订')
    settle_publication(env, 'FW-001')
    rerun = env['evaluation'].reruns[0]
    # 页面开着也用评测服务保存的登录：页面上的短期登录撑不到评测结束
    assert rerun['token'] == Delegated('evaluation-handle-1') and rerun['refresh'] == ''


def test_old_graph_data_waits_for_the_users_own_confirmation_to_delete_it(env):
    settings(env, optimize=True, publish=True)
    env['evaluation'].complete(RUN)
    sync(env)
    revise(env, '合成修订')
    note = '评测图范围内已有 2 个节点、0 条关系，不是上一轮评测导入后的原样数据，确认删除后才会重新导入'
    env['evaluation'].rerun_failure = graph_question(note, 'ab12')
    run = run_of(settle_publication(env, 'FW-001'), 'FW-001')
    # 自动评测不能代用户确认删除：停下来，记下需要确认的原因和这次提示的确认码
    assert run['status'] == 'waiting_evaluation' and run['human_hold'] is True and run['graph_confirmation'] == note
    assert run['graph_confirmation_code'] == 'ab12'
    env['evaluation'].rerun_failure = None
    response = env['client'].post(f"/api/projects/{env['pid']}/flywheel/runs/FW-001/evaluate",
                                  json={'replace_graph': True, 'graph_confirmation_code': 'ab12'},
                                  headers={'X-Refresh-Token': 'browser-refresh'})
    assert response.status_code == 200, response.text
    run = run_of(response.json(), 'FW-001')
    assert run['status'] == 'evaluating' and 'graph_confirmation' not in run and 'graph_confirmation_code' not in run
    rerun = env['evaluation'].reruns[-1]
    # 带回用户看到的那次提示的确认码：评测服务据此只删除那次提示显示的数据
    assert rerun['replace_existing_graph'] is True and rerun['graph_confirmation_code'] == 'ab12'
    assert rerun['token'] == 'user-token'


def graph_question(note, code):
    question = WorkspaceError(note, 409, 'graph_confirmation_required')
    question.details = {'confirmation_code': code}
    return question


@pytest.mark.parametrize('body', [{'replace_graph': True}, {'replace_graph': False, 'graph_confirmation_code': 'ab12'}])
def test_a_deletion_is_confirmed_only_with_the_questions_code(env, body):
    env['evaluation'].complete(RUN)
    sync(env)
    optimized(env)
    published(env)
    env['evaluation'].rerun_failure = graph_question('合成的图数据提示', 'ab12')
    assert env['client'].post(f"/api/projects/{env['pid']}/flywheel/runs/FW-001/evaluate").status_code == 409
    env['evaluation'].rerun_failure = None
    response = env['client'].post(f"/api/projects/{env['pid']}/flywheel/runs/FW-001/evaluate", json=body)
    assert response.status_code == 200, response.text
    # 没勾选、或没带确认码：不算确认删除，由评测服务重新提示
    rerun = env['evaluation'].reruns[-1]
    assert 'replace_existing_graph' not in rerun and 'graph_confirmation_code' not in rerun
    bad = {'replace_graph': True, 'graph_confirmation_code': 'NOT-HEX'}
    assert env['client'].post(f"/api/projects/{env['pid']}/flywheel/runs/FW-001/evaluate", json=bad).status_code == 422


def test_a_confirmation_without_the_graph_choice_does_not_delete_graph_data(env):
    env['evaluation'].complete(RUN)
    sync(env)
    optimized(env)
    published(env)
    response = env['client'].post(f"/api/projects/{env['pid']}/flywheel/runs/FW-001/evaluate")
    assert response.status_code == 200, response.text
    assert 'replace_existing_graph' not in env['evaluation'].reruns[-1]


def test_the_round_limit_defaults_to_three(env):
    assert sync(env)['settings']['max_rounds'] == 3


def test_a_full_pass_starts_the_count_of_consecutive_rounds_again(env):
    settings(env, optimize=True, publish=True, max_rounds=1)
    env['evaluation'].complete(RUN)
    sync(env)
    revise(env, '合成修订')
    settle_publication(env, 'FW-001')
    env['evaluation'].complete(NEXT_RUN, release='release_2', version='0.1.2', finished=3000.0, score=100, cases=ALL_PASS)
    view = sync(env)
    assert run_of(view, 'FW-002')['status'] == 'passed' and view['auto_rounds'] == 0
    # 循环已成功结束：之后再有失分，这次仍能自动优化，不会被算作已达上限
    env['evaluation'].complete(THIRD_RUN, release='release_2', version='0.1.2', finished=5000.0, score=75)
    run = run_of(sync(env), 'FW-003')
    assert run['status'] == 'optimizing' and run['mode'] == 'auto'


def test_a_full_pass_of_an_old_version_does_not_restart_the_count(env):
    settings(env, optimize=True, publish=True, max_rounds=1)
    env['evaluation'].complete(RUN)
    sync(env)
    revise(env, '合成修订')
    settle_publication(env, 'FW-001')
    env['evaluation'].complete(NEXT_RUN, release='release_old', version='0.1.0', finished=3000.0, score=100, cases=ALL_PASS)
    view = sync(env)
    assert run_of(view, 'FW-002')['reason'] == 'version_mismatch' and view['auto_rounds'] == 1


@pytest.mark.parametrize('cases,rate', [
    ({'total': 4, 'pass': 2, 'partial': 1, 'fail': 1}, 50),
    ({'total': 3, 'pass': 2, 'partial': 0, 'fail': 0, 'manual': 1}, 67),
    ({'total': 16, 'pass': 1, 'partial': 0, 'fail': 15}, 6),
    # 与页面同样按浮点计算：29/200 在页面上是 14%（14.499…），不是精确四舍五入的 15
    ({'total': 200, 'pass': 29, 'partial': 0, 'fail': 171}, 14),
    ({'total': 4, 'pass': 4, 'partial': 0, 'fail': 0}, 100),
    ({'total': 0, 'pass': 0, 'partial': 0, 'fail': 0}, None),
])
def test_the_score_is_the_strict_pass_rate_of_all_planned_cases(cases, rate):
    # 与评测页面「结果概览」的严格通过率一致：部分通过、失败、异常和待人工判定都不算通过，显示整数百分比
    assert intake.strict_rate({'summary': cases}) == rate


def test_the_evaluation_record_reports_the_strict_pass_rate(env):
    settings(env, optimize=True, publish=True)
    env['evaluation'].complete(RUN)
    sync(env)
    revise(env, '合成修订')
    settle_publication(env, 'FW-001')
    env['evaluation'].complete(NEXT_RUN, release='release_2', version='0.1.2', finished=3000.0, cases=THREE_OF_FOUR)
    view = sync(env)
    assert run_of(view, 'FW-001')['evaluation']['score'] == 75 and run_of(view, 'FW-002')['score'] == 75
    detail = env['client'].get(f"/api/projects/{env['pid']}/flywheel/runs/FW-001").json()
    texts = [item['text'] for item in detail['history']]
    assert any(text.startswith('严格通过率 75%') for text in texts)


def test_evaluation_requests_of_two_projects_never_share_an_id():
    # 每个项目的运行编号都从 FW-001 开始，评测服务只按用户区分请求标识：标识里必须带上项目
    run = {'id': 'FW-002'}
    first, second = evaluation.request_id('project_alpha', run, 1), evaluation.request_id('project_beta', run, 1)
    assert first != second and first == evaluation.request_id('project_alpha', run, 1)
    assert re.fullmatch(r'[a-z0-9-]+', first) and len(first + '-prep') <= 96


def test_a_revision_that_passes_every_case_closes_a_waiting_run(env):
    env['evaluation'].complete(RUN)
    assert run_of(sync(env), 'FW-001')['status'] == 'waiting_optimize'
    # 判分超时等技术异常重判后全部通过：重新生成的修订到达，运行直接记为评测通过
    env['evaluation'].complete(RUN, revision=2, score=100, cases=ALL_PASS)
    view = sync(env)
    run = run_of(view, 'FW-001')
    assert run['status'] == 'passed' and view['pending'] == 0
    assert '评测集全部通过' in history(env, 'FW-001')


def test_a_run_left_without_changes_closes_once_its_result_passes_every_case(env):
    env['evaluation'].complete(RUN)
    sync(env)
    assert run_of(optimized(env, change=False), 'FW-001')['reason'] == 'no_changes'
    env['evaluation'].complete(RUN, revision=2, score=100, cases=ALL_PASS)
    view = sync(env)
    assert run_of(view, 'FW-001')['status'] == 'passed' and view['pending'] == 0


def test_an_optimization_without_changes_on_an_all_pass_result_needs_no_handling(env):
    env['evaluation'].complete(RUN)
    sync(env)
    assert act(env, 'FW-001', 'optimize').status_code == 200
    # 优化进行中，评测结果更新为全部通过；助手结束时没有修改任何文档
    env['evaluation'].complete(RUN, revision=2, score=100, cases=ALL_PASS)
    env['assistant'].finish()
    # 同一次同步里先读到新的评测修订，再收尾优化
    view = sync(env)
    run = run_of(view, 'FW-001')
    assert run['status'] == 'passed' and run['changed'] == 0 and view['pending'] == 0 and view['candidate_id'] is None
