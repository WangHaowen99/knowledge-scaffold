"""Consumer feedback in the flywheel: a package owner hands users' feedback to modeling, the flywheel optimizes it.

Hub, the evaluation service and the assistant are the synthetic stubs of test_flywheel; all data is synthetic.
"""
import json

from app.services.flywheel import optimize, store
from app.services.workspace import WorkspaceError
from test_flywheel import FEEDBACK_RUN, OBJECT, SUBJECT, act, env, run_of, settings, sync
from test_flywheel_background import background_round
from test_flywheel_evaluation import history, revise, settle_publication


def test_confirmed_feedback_is_optimized_and_published_without_an_evaluation(env):
    settings(env, optimize=True, publish=True)
    env['evaluation'].feedback()
    run = run_of(sync(env), 'FW-001')
    assert run['origin'] == 'consumer_feedback' and run['status'] == 'optimizing' and run['mode'] == 'auto'
    assert run['score'] is None and env['assistant'].sent[0][0].endswith('--flywheel')
    revise(env, '按用户反馈补充的合成说明')
    view = settle_publication(env, 'FW-001')
    run = run_of(view, 'FW-001')
    # 用户反馈没有评测集可沿用：发布即处理完成，不等待也不发起评测
    assert run['status'] == 'published' and run['target_version'] == '0.1.2' and env['evaluation'].reruns == []
    assert view['pending'] == 0 and view['auto_rounds'] == 0
    assert '用户反馈处理完成' in history(env, 'FW-001')


def conclude_resolved(env, run_id='FW-001'):
    """The synthetic assistant finds every problem gone in the latest version and says so, as the command asks."""
    report = store.find(store.load(env['pid'], SUBJECT), run_id)['report']
    mark = env['root'].joinpath(*optimize.RECHECK_MARK)
    mark.parent.mkdir(exist_ok=True)
    mark.write_text(json.dumps({'report': report, 'resolved': True}), encoding='utf-8')
    env['assistant'].finish()


def test_feedback_on_an_earlier_version_is_checked_against_the_current_one(env):
    env['evaluation'].feedback(release='release_old', version='0.1.0')
    run = run_of(sync(env), 'FW-001')
    # 旧版本的反馈不过期：等用户确认（开关关着），优化时先核对当前版本
    assert run['status'] == 'waiting_optimize' and run['human_hold'] is True
    assert '反馈针对旧版本' in history(env, 'FW-001')
    assert act(env, 'FW-001', 'optimize').status_code == 200
    assert env['assistant'].sent[0][0].endswith('--flywheel-recheck')
    # 助手核对后确认问题在最新版本都已不存在、没有修改任何文档：运行结束，不留待处理
    conclude_resolved(env)
    view = sync(env, reconcile=False)
    run = run_of(view, 'FW-001')
    assert run['status'] == 'ignored' and run['reason'] == 'already_resolved' and view['pending'] == 0
    # 同一次同步里用用户的登录告诉评测服务：核对的是最新版本，没有发布新版本
    sent, = env['evaluation'].writebacks
    assert sent['run_id'] == FEEDBACK_RUN and sent['outcome'] == 'resolved' and sent['token'] == 'user-token'
    assert sent['release_id'] == 'release_one' and '最新版本 0.1.1 已不存在这些问题' in sent['note']
    assert '已告知评测服务' in history(env, 'FW-001')
    # 已回写的不再重复回写
    sync(env, reconcile=False)
    assert len(env['evaluation'].writebacks) == 1


def test_changing_nothing_without_the_conclusion_is_not_reported_as_resolved(env):
    env['evaluation'].feedback(release='release_old', version='0.1.0')
    sync(env)
    assert act(env, 'FW-001', 'optimize').status_code == 200
    # 没改文件也可能是因为条目要专家拍板、只是运行异常或复检后整体回退：没有结论文件就不算已解决
    env['assistant'].finish()
    view = sync(env, reconcile=False)
    run = run_of(view, 'FW-001')
    assert run['status'] == 'blocked' and run['reason'] == 'no_changes' and view['pending'] == 1
    assert env['evaluation'].writebacks == []


def test_a_conclusion_left_by_an_earlier_check_does_not_count(env):
    env['evaluation'].feedback(release='release_old', version='0.1.0')
    sync(env)
    stale = env['root'].joinpath(*optimize.RECHECK_MARK)
    stale.parent.mkdir(exist_ok=True)
    stale.write_text(json.dumps({'report': 'eval-20261101-080808-abc127.md', 'resolved': True}), encoding='utf-8')
    assert act(env, 'FW-001', 'optimize').status_code == 200
    # 交给助手前清掉旧结论：这一次助手没写，就不算已解决
    assert not stale.exists()
    env['assistant'].finish()
    assert run_of(sync(env, reconcile=False), 'FW-001')['reason'] == 'no_changes'


def resolved_in_background(env):
    """The feedback check ends in a background round, with no user login to write back with."""
    env['evaluation'].feedback(release='release_old', version='0.1.0')
    sync(env)
    assert act(env, 'FW-001', 'optimize').status_code == 200
    conclude_resolved(env)
    background_round(env)
    assert store.find(store.load(env['pid'], SUBJECT), 'FW-001')['reason'] == 'already_resolved'


def test_a_resolution_found_in_the_background_is_written_back_when_the_user_returns(env):
    resolved_in_background(env)
    assert env['evaluation'].writebacks == []
    sync(env, reconcile=False)
    assert [item['outcome'] for item in env['evaluation'].writebacks] == ['resolved']


def test_a_write_back_the_evaluation_service_cannot_take_now_is_retried(env):
    resolved_in_background(env)
    env['evaluation'].writeback_failure = WorkspaceError('评测服务不可用，请稍后重试', 503, 'eval_unavailable')
    sync(env, reconcile=False)
    assert 'writeback' not in store.find(store.load(env['pid'], SUBJECT), 'FW-001')
    env['evaluation'].writeback_failure = None
    sync(env, reconcile=False)
    assert store.find(store.load(env['pid'], SUBJECT), 'FW-001')['writeback']['status'] == 'done'


def test_a_refused_write_back_is_not_retried(env):
    resolved_in_background(env)
    env['evaluation'].writeback_failure = WorkspaceError('这一轮的优化建议还未交给建模', 409, 'invalid_state')
    sync(env, reconcile=False)
    sync(env, reconcile=False)
    assert len(env['evaluation'].writebacks) == 1
    run = store.find(store.load(env['pid'], SUBJECT), 'FW-001')
    assert run['writeback']['status'] == 'refused' and '评测服务未接受处理结果' in history(env, 'FW-001')


def test_feedback_on_an_earlier_version_publishes_on_the_current_one(env):
    env['evaluation'].feedback(release='release_old', version='0.1.0')
    sync(env)
    assert act(env, 'FW-001', 'optimize').status_code == 200
    (env['root'] / 'ontology/object/object_a.md').write_text(OBJECT + '\n- 合成修订\n', encoding='utf-8')
    env['assistant'].finish()
    run = run_of(sync(env, reconcile=False), 'FW-001')
    # 候选内容基于当前版本：可以发布，不因反馈针对旧版本而过期
    assert run['status'] == 'waiting_publish' and run['base_release_id'] == 'release_one'
    assert act(env, 'FW-001', 'publish').status_code == 200
    run = run_of(settle_publication(env, 'FW-001'), 'FW-001')
    assert run['status'] == 'published' and env['evaluation'].reruns == []


def test_only_feedback_handed_over_to_modeling_becomes_a_run(env):
    for status in ('modeling_done', 'modeling_failed'):
        env['evaluation'].feedback(status=status)
        assert sync(env)['runs'] == []


def test_feedback_does_not_count_toward_the_round_limit(env):
    settings(env, optimize=True, max_rounds=1)
    with store.update(env['pid'], SUBJECT) as current:
        current['auto_rounds'] = 1
    env['evaluation'].feedback()
    run = run_of(sync(env), 'FW-001')
    assert run['status'] == 'optimizing' and run['mode'] == 'auto'
    assert store.load(env['pid'], SUBJECT)['auto_rounds'] == 1


def test_the_flywheel_reads_the_feedback_run_it_optimizes(env):
    env['evaluation'].feedback()
    sync(env)
    assert act(env, 'FW-001', 'optimize').status_code == 200
    assert any(path == f'/runs/{FEEDBACK_RUN}/artifacts/suggestions.md' for _, path, _ in env['evaluation'].calls)
