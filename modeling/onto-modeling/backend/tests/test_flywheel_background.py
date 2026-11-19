"""The flywheel while its user is away: delegations, background rounds, and the user's login staying out of them.

Hub, the evaluation service and the assistant are the synthetic stubs of test_flywheel; all data is synthetic.
"""
import base64
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time

import pytest

from app import config
from app.services import auth, eval_client, hub_packages, knowledge_hub
from app.services.delegated import Delegated
from app.services.flywheel import background, grants, publish, snapshots, store
from app.services.flywheel.caller import Caller
from app.services.workspace import WorkspaceError
from test_flywheel import (OBJECT, OTHER_SUBJECT, OWNER, PACKAGE, RUN, NEXT_RUN, SERVICE_TOKEN, SUBJECT, USER, act,
                           another_member_is, env, optimized, run_of, settings, sync)

ALL_PASS = {'cases': {'pass': 4, 'partial': 0, 'fail': 0}}


def background_round(env, reconcile=False):
    background.run_round(env['manager'], env['assistant'], reconcile)


def state(env):
    return store.load(env['pid'], SUBJECT)


def notify(env, run_id=RUN, **fields):
    entry = env['evaluation'].complete(run_id, **fields)
    body = {**entry, 'sequence': 1, 'owner': OWNER, 'ontology_id': PACKAGE, 'event_id': f'{run_id}:1'}
    headers = {'X-Evaluation-Service-Token': SERVICE_TOKEN, 'Authorization': ''}
    assert env['client'].post('/api/internal/evaluation/notifications', json=body, headers=headers).status_code == 200


def calls_since(env, start):
    return env['evaluation'].calls[start:]


def test_switches_register_with_their_own_service_and_revoke_when_off(env):
    view = settings(env, optimize=True)
    assert view['delegation']['evaluation']['status'] == 'active' and view['delegation']['hub']['status'] == 'off'
    assert view['delegation']['login'] == 'off' and env['evaluation'].refreshes == ['']
    assert view['delegation']['background'] is True and env['hub'].issued == 0
    # 打开发布与评测：Hub 登记，评测服务改为带登录的登记（刷新令牌转交过去），旧登记销毁
    view = settings(env, publish=True)
    assert view['delegation']['hub']['status'] == 'active' and view['delegation']['login'] == 'active'
    assert env['evaluation'].refreshes == ['', 'synthetic-refresh'] and env['evaluation'].revoked == ['evaluation-handle-1']
    assert view['delegation']['background'] is True
    # 发布与评测仍要读取评测结果：关掉自动优化，评测服务的登记保留
    view = settings(env, optimize=False)
    assert view['delegation']['evaluation']['status'] == 'active' and env['evaluation'].revoked == ['evaluation-handle-1']
    # Hub 的登记用用户本人的登录完成，签名用的身份就是这个登录
    assert env['hub'].registrations[0][0] == 'user-token'
    assert {key: state(env)['delegation']['hub'][key] for key in ('issuer', 'sub')} == {'issuer': USER['iss'], 'sub': USER['sub']}
    view = settings(env, publish=False)
    assert env['hub'].revoked == env['hub'].ids and env['evaluation'].revoked[-1] == 'evaluation-handle-2'
    assert view['delegation']['background'] is False and state(env)['delegation'] == {}
    assert [item['description'].split('，')[0] for item in view['audit'] if item['action'] == '后台授权'] == [
        '已销毁Hub登记', '已销毁评测服务登记及其保存的后台评测登录', '已在Hub登记',
        '评测服务已保存后台评测登录', '已在评测服务登记']


def test_closing_a_switch_destroys_the_delegation_even_with_the_service_down(env):
    sync(env)
    settings(env, optimize=True)
    env['evaluation'].revoke_failure = WorkspaceError('评测服务不可用，请稍后重试', 503, 'eval_unavailable')
    view = settings(env, optimize=False)
    # 委托立即停用：句柄只留在待撤销列表里重试撤销，不再用于任何调用
    current = state(env)
    assert view['settings']['optimize'] is False and current['delegation'] == {}
    assert [item['key'] for item in current['revoking']] == ['evaluation-handle-1']
    assert any('暂未确认撤销' in item['description'] for item in view['audit'])
    assert 'revoking' not in view and 'evaluation-handle-1' not in json.dumps(view)
    notify(env)
    start = len(env['evaluation'].calls)
    background_round(env)
    assert calls_since(env, start) == [] and env['assistant'].sent == []


def test_the_browser_never_sees_a_handle(env):
    view = settings(env, optimize=True, publish=True)
    token_id = state(env)['delegation']['hub']['token_id']
    assert 'handle' not in json.dumps(view) and token_id not in json.dumps(view) and 'token_id' not in json.dumps(view)
    assert 'evaluation-handle' not in repr(Delegated('evaluation-handle-1'))


def test_background_runs_the_whole_flywheel_on_delegations_alone(env):
    sync(env)  # 首次同步对账，记住当前用户在评测服务的所有者标识
    settings(env, optimize=True, publish=True)
    start = len(env['evaluation'].calls)
    notify(env)
    background_round(env)
    run = store.find(state(env), 'FW-001')
    assert run['status'] == 'optimizing' and run['mode'] == 'auto'
    (env['root'] / 'ontology/object/object_a.md').write_text(OBJECT + '\n- 合成字段说明\n', encoding='utf-8')
    env['assistant'].finish()
    background_round(env)
    publish.wait(env['pid'], SUBJECT, 'FW-001', timeout=30)
    run = store.find(state(env), 'FW-001')
    assert run['status'] == 'waiting_evaluation' and run['publish_mode'] == 'auto' and run['target_version'] == '0.1.2'
    background_round(env)
    run = store.find(state(env), 'FW-001')
    assert run['status'] == 'evaluating' and env['evaluation'].reruns[0]['package_ref']['release_id'] == 'release_2'
    # 新版本全部通过：循环结束，不再优化
    notify(env, NEXT_RUN, release='release_2', version='0.1.2', finished=5000.0, score=100, **ALL_PASS)
    background_round(env)
    current = state(env)
    assert store.find(current, 'FW-001')['status'] == 'published'
    assert store.find(current, 'FW-002')['status'] == 'passed' and store.find(current, 'FW-002')['trigger'] == 'flywheel'
    assert len(env['assistant'].sent) == 1
    # 后台全程没有用户令牌，用的都是登记的委托
    tokens = [token for _, _, token in calls_since(env, start)]
    assert tokens and all(token == Delegated('evaluation-handle-1') for token in tokens)
    token_id = state(env)['delegation']['hub']['token_id']
    assert env['hub'].tokens and all(token == ('delegation', token_id, USER['iss'], USER['sub'])
                                     for token in env['hub'].tokens)


def test_results_of_rounds_run_while_the_user_was_away_are_all_written_back(env):
    sync(env)
    settings(env, optimize=True, publish=True)
    notify(env)
    background_round(env)
    (env['root'] / 'ontology/object/object_a.md').write_text(OBJECT + '\n- 第一轮合成修改\n', encoding='utf-8')
    env['assistant'].finish()
    background_round(env)
    publish.wait(env['pid'], SUBJECT, 'FW-001', timeout=30)
    background_round(env)
    # 新版本仍有用例未通过：下一轮优化开始，项目的评测优化记录换成这一轮
    notify(env, NEXT_RUN, release='release_2', version='0.1.2', finished=5000.0)
    background_round(env)
    assert store.find(state(env), 'FW-002')['status'] == 'optimizing'
    (env['root'] / 'ontology/object/object_a.md').write_text(OBJECT + '\n- 第二轮合成修改\n', encoding='utf-8')
    env['assistant'].finish()
    background_round(env)
    publish.wait(env['pid'], SUBJECT, 'FW-002', timeout=30)
    assert env['evaluation'].writebacks == []  # 后台委托不能代用户回写
    # 用户回来：两轮各自的处理结果都写回，前一轮的没有被后一轮顶掉
    sync(env)
    assert [(item['run_id'], item['release_id']) for item in env['evaluation'].writebacks] == [
        (RUN, 'release_2'), (NEXT_RUN, 'release_3')]
    assert all(item['token'] == 'user-token' for item in env['evaluation'].writebacks)


def test_without_a_delegation_background_starts_nothing(env):
    sync(env)
    notify(env)
    start = len(env['evaluation'].calls)
    background_round(env)
    assert state(env)['runs'] == [] and calls_since(env, start) == [] and env['assistant'].sent == []


def test_a_publication_switch_alone_never_starts_an_optimization(env):
    sync(env)
    settings(env, publish=True)
    notify(env)
    background_round(env)
    run = store.find(state(env), 'FW-001')
    # 发布与评测要读取评测结果，但优化仍由「自动化处理优化建议」决定
    assert run['status'] == 'waiting_optimize' and run['reason'] == 'automation_off' and env['assistant'].sent == []


def test_a_confirmed_optimization_finishes_after_the_user_left(env):
    env['evaluation'].complete(RUN)
    sync(env)
    assert act(env, 'FW-001', 'optimize').status_code == 200
    (env['root'] / 'ontology/object/object_a.md').write_text(OBJECT + '\n- 合成字段说明\n', encoding='utf-8')
    env['assistant'].finish()
    background_round(env)
    run = store.find(state(env), 'FW-001')
    assert run['status'] == 'waiting_publish' and run['publish_hold'] is True and env['hub'].calls == []


def test_failed_registration_keeps_the_switch_and_reports_why(env):
    env['evaluation'].refusal = WorkspaceError('评测服务不可用，请稍后重试', 503, 'eval_unavailable')
    view = settings(env, optimize=True)
    assert view['settings']['optimize'] is True and view['delegation']['background'] is False
    assert view['delegation']['evaluation'] == {'status': 'issue', 'expires_at': None,
                                                'message': '评测服务登记失败（评测服务不可用，请稍后重试）'}
    calls = len(env['evaluation'].calls)
    sync(env, reconcile=False)
    assert not [path for _, path, _ in calls_since(env, calls) if path == '/delegations']  # 不随轮询反复重试
    env['evaluation'].refusal = None
    with store.update(env['pid'], SUBJECT) as current:
        current['delegation_issues']['evaluation']['at'] -= grants.RETRY_SECONDS
    assert sync(env, reconcile=False)['delegation']['evaluation']['status'] == 'active'


def test_a_project_never_published_registers_after_its_first_release(env):
    knowledge_hub.save(knowledge_hub.state_dir() / 'packages' / env['pid'] / 'association.json', {})
    view = settings(env, optimize=True, publish=True)
    # 没有 Hub 包就没有可登记的对象：如实告诉用户，而不是“请稍后重试”
    assert view['delegation']['evaluation']['status'] == 'unlinked' and view['delegation']['hub']['status'] == 'unlinked'
    assert env['evaluation'].issued == 0 and env['hub'].issued == 0
    knowledge_hub.save(knowledge_hub.state_dir() / 'packages' / env['pid'] / 'association.json',
                       {'ontology_id': PACKAGE, 'release_id': 'release_one', 'package_digest': 'b' * 64,
                        'version': '0.1.1', 'committed_at': '2026-10-31T00:00:00+00:00'})
    # 首次发布后，下一次同步自动登记，开关不用重开
    view = sync(env, reconcile=False)
    assert view['delegation']['evaluation']['status'] == 'active' and view['delegation']['hub']['status'] == 'active'


def test_turning_the_switch_off_and_on_retries_at_once(env):
    env['evaluation'].refusal = WorkspaceError('评测服务返回错误（HTTP 500），请稍后重试', 503, 'eval_unavailable')
    assert settings(env, optimize=True)['delegation']['evaluation']['status'] == 'issue'
    env['evaluation'].refusal = None  # 评测服务修好了
    settings(env, optimize=False)
    assert state(env)['delegation_issues'] == {}
    # 不用等重试间隔：用户重新打开开关就立即登记
    assert settings(env, optimize=True)['delegation']['evaluation']['status'] == 'active'


def test_a_refused_delegation_pauses_background_until_the_user_returns(env):
    env['evaluation'].complete(RUN)
    sync(env)
    optimized(env)
    settings(env, publish=True)
    with store.update(env['pid'], SUBJECT) as current:
        store.find(current, 'FW-001')['publish_hold'] = False
    env['hub'].fail = WorkspaceError('知识包 Hub 请求失败（401）', 401, 'hub_error')
    background_round(env)
    publish.wait(env['pid'], SUBJECT, 'FW-001', timeout=30)
    current = state(env)
    assert store.find(current, 'FW-001')['reason'] == 'authorization' and 'hub' not in current['delegation']
    assert '重新登记' in current['delegation_issues']['hub']['message']
    calls = len(env['hub'].calls)
    background_round(env)
    assert len(env['hub'].calls) == calls  # 没有可用委托：后台不再调用 Hub
    view = sync(env, reconcile=False)  # 用户回来：用登录重新登记，运行仍需确认后重试
    assert view['delegation']['hub']['status'] == 'active' and env['hub'].issued == 2
    assert run_of(view, 'FW-001')['status'] == 'blocked'


def test_delegations_renew_while_the_user_keeps_coming_back(env):
    settings(env, optimize=True)
    assert sync(env, reconcile=False) and env['evaluation'].issued == 1
    with store.update(env['pid'], SUBJECT) as current:
        current['delegation']['evaluation']['granted_at'] -= 30 * 86400 * grants.RENEW_SHARE + 60
    sync(env, reconcile=False)
    # 评测服务的登记续期而不是重新登记：带着的登录不用重新转交刷新令牌
    assert env['evaluation'].issued == 1 and env['evaluation'].renewals == [('evaluation-handle-1', 30 * 86400)]
    assert state(env)['delegation']['evaluation']['handle'] == 'evaluation-handle-1'
    # Hub 用同一登记编号重新登记：只延长有效期，不换登记，也不撤销
    settings(env, publish=True)
    with store.update(env['pid'], SUBJECT) as current:
        current['delegation']['hub']['granted_at'] -= 30 * 86400 * grants.RENEW_SHARE + 60
        current['delegation']['hub']['expires_at'] -= 30 * 86400 * grants.RENEW_SHARE + 60
    before = state(env)['delegation']['hub']
    sync(env, reconcile=False)
    after = state(env)['delegation']['hub']
    assert env['hub'].issued == 1 and len(env['hub'].registrations) == 2 and env['hub'].revoked == []
    assert after['token_id'] == before['token_id'] and after['expires_at'] > before['expires_at']


def test_a_hub_registration_without_an_id_is_registered_again(env):
    # 早期测试版本保存的 Hub 登记只有句柄：后台不拿它调用 Hub，用户回来时换成新的登记
    settings(env, publish=True)
    with store.update(env['pid'], SUBJECT) as current:
        grant = current['delegation']['hub']
        current['delegation']['hub'] = {'handle': 'synthetic-old', 'expires_at': grant['expires_at'],
                                         'package': grant['package'], 'seconds': grant['seconds'],
                                         'granted_at': grant['granted_at']}
    calls = len(env['hub'].calls)
    background_round(env)
    assert len(env['hub'].calls) == calls
    view = sync(env, reconcile=False)
    assert view['delegation']['hub']['status'] == 'active' and env['hub'].issued == 2
    assert 'token_id' in state(env)['delegation']['hub']


def test_the_chosen_lifetime_reaches_both_services(env):
    settings(env, delegation_days=7)
    view = settings(env, optimize=True, publish=True)
    assert view['settings']['delegation_days'] == 7
    assert env['evaluation'].lifetimes == [7 * 86400] and env['hub'].lifetimes == [7 * 86400]
    current = state(env)
    assert all(grant['seconds'] == 7 * 86400 for grant in current['delegation'].values())


def test_changing_the_lifetime_registers_again_and_revokes_the_old_one(env):
    settings(env, optimize=True, publish=True)
    view = settings(env, delegation_days=1)
    assert env['evaluation'].lifetimes == [30 * 86400, 86400] and env['hub'].lifetimes == [30 * 86400, 86400]
    assert env['evaluation'].revoked == ['evaluation-handle-1'] and env['hub'].revoked == env['hub'].ids[:1]
    assert env['hub'].issued == 2 and view['delegation']['background'] is True
    assert '有效期：30 天 → 1 天' in [item['description'] for item in view['audit']]


def test_no_limit_uses_the_longest_lifetime_the_services_accept(env):
    view = settings(env, delegation_days=0, optimize=True)
    assert env['evaluation'].lifetimes == [3650 * 86400] and view['delegation']['evaluation']['status'] == 'active'
    assert any(item['description'].endswith('长期有效') for item in view['audit'])
    view = settings(env, delegation_days=3)
    assert env['evaluation'].lifetimes[-1] == 3 * 86400 and '有效期：无限制 → 3 天' in [item['description'] for item in view['audit']]


def test_a_lifetime_outside_the_choices_is_refused(env):
    for days in (2, -1, 31):
        response = env['client'].put(f"/api/projects/{env['pid']}/flywheel/settings", json={'delegation_days': days})
        assert response.status_code == 422
    assert state(env)['settings']['delegation_days'] == 30


def test_a_shorter_lifetime_registers_at_once_even_while_a_failed_registration_waits(env):
    settings(env, optimize=True, publish=True)
    # 刚有一次登记失败，正在重试间隔内：改短有效期不等间隔，旧的 30 天授权不能继续用
    with store.update(env['pid'], SUBJECT) as current:
        current['delegation_issues']['evaluation'] = {'message': '评测服务登记失败（合成原因）', 'at': time.time()}
    view = settings(env, delegation_days=1)
    assert env['evaluation'].lifetimes == [30 * 86400, 86400] and env['evaluation'].revoked == ['evaluation-handle-1']
    assert state(env)['delegation']['evaluation']['seconds'] == 86400 and view['delegation']['login'] == 'active'


def test_failing_to_register_a_new_lifetime_drops_the_old_delegation(env):
    settings(env, optimize=True)
    env['evaluation'].refusal = WorkspaceError('评测服务不可用，请稍后重试', 503, 'eval_unavailable')
    view = settings(env, delegation_days=1)
    # 改短后不能继续留着 30 天的授权：宁可停掉后台，等用户回来重新登记
    assert view['delegation']['evaluation']['status'] == 'issue' and view['delegation']['background'] is False
    assert state(env)['delegation'] == {} and env['evaluation'].revoked == ['evaluation-handle-1']


@pytest.mark.parametrize('revocation', ['confirmed', 'unconfirmed'])
def test_closing_publication_with_the_service_down_still_destroys_the_stored_login(env, revocation):
    settings(env, optimize=True, publish=True)
    assert state(env)['delegation']['evaluation']['login'] is True
    env['evaluation'].refusal = WorkspaceError('评测服务不可用，请稍后重试', 503, 'eval_unavailable')
    if revocation == 'unconfirmed':
        env['evaluation'].revoke_failure = env['evaluation'].refusal
    view = settings(env, publish=False)
    # 去掉登录要重新登记；登记失败时带着登录的旧登记不能留着，与关掉开关一样停用并撤销
    current = state(env)
    assert 'evaluation' not in current['delegation']
    assert view['delegation']['evaluation']['status'] == 'issue' and view['delegation']['login'] == 'off'
    if revocation == 'confirmed':
        assert env['evaluation'].revoked == ['evaluation-handle-1']
    else:
        pending = [item['key'] for item in current['revoking'] if item['service'] == 'evaluation']
        # 旧登记待撤销；没登记成功的新句柄（结果未知）也留着，一并撤销
        assert pending == ['evaluation-handle-2', 'evaluation-handle-1']
    assert any('已停用原登记' in item['description'] for item in view['audit'])


@pytest.mark.parametrize('why', ['waiting_after_a_failure', 'no_hub_package'])
def test_closing_publication_destroys_the_stored_login_even_when_nothing_registers_now(env, monkeypatch, why):
    settings(env, optimize=True, publish=True)
    if why == 'waiting_after_a_failure':
        # 刚才续期失败，还在重试间隔里：这次不会重新登记
        with store.update(env['pid'], SUBJECT) as current:
            current['delegation_issues']['evaluation'] = {'message': '合成的登记失败', 'at': time.time()}
    else:
        monkeypatch.setattr(hub_packages, 'association', lambda pid, subject=None: None)
    settings(env, publish=False)
    # 不重新登记，带着后台评测登录的旧登记也不能留着
    assert 'evaluation' not in state(env)['delegation'] and env['evaluation'].revoked == ['evaluation-handle-1']


def test_a_project_that_cannot_be_read_for_a_moment_keeps_its_delegations(env, monkeypatch):
    settings(env, optimize=True, publish=True)

    def unreadable(pid):
        raise WorkspaceError('项目元数据暂时无法读取', 503, 'project_metadata_unavailable')

    monkeypatch.setattr(env['manager'], 'get_project', unreadable)
    background_round(env)
    # 读不到元数据只是暂时的：本轮跳过，不当作项目已删除去撤销登记
    assert set(state(env)['delegation']) == {'evaluation', 'hub'} and env['evaluation'].revoked == []
    assert state(env)['revoking'] == []


def test_a_request_survives_the_background_withdrawing_the_delegations_meanwhile(env, monkeypatch):
    settings(env, optimize=True, publish=True)

    def withdrawn_meanwhile(manager, meta, username):
        # 这次请求读了状态之后，后台不再代这个用户执行、撤回了登记
        grants.withdraw(env['pid'], SUBJECT, '合成的后台撤回')
        return False

    monkeypatch.setattr(grants, 'acts_for', withdrawn_meanwhile)
    grants.align(Caller(env['manager'], env['pid'], SUBJECT, USER['sub'], 'user-token', True, env['assistant']))
    assert state(env)['delegation'] == {} and env['evaluation'].revoked == ['evaluation-handle-1']


def test_a_refusal_drops_only_the_registration_that_was_refused(env):
    settings(env, optimize=True, publish=True)
    token_id = state(env)['delegation']['hub']['token_id']
    # 拒绝的是一次早已被替换的登记：当前有效的登记留着
    with store.update(env['pid'], SUBJECT) as current:
        grants.reject(current, 'hub', USER['sub'], 'flywheel-' + '0' * 32)
    assert state(env)['delegation']['hub']['token_id'] == token_id
    with store.update(env['pid'], SUBJECT) as current:
        grants.reject(current, 'hub', USER['sub'], token_id)
    assert 'hub' not in state(env)['delegation']
    caller = Caller(env['manager'], env['pid'], SUBJECT, USER['sub'], Delegated('evaluation-handle-1'), True,
                    env['assistant'], hub_delegation={'token_id': token_id})
    assert (grants.key_of(caller, 'evaluation'), grants.key_of(caller, 'hub')) == ('evaluation-handle-1', token_id)


def test_a_background_publication_refused_on_a_replaced_registration_keeps_the_new_one(env, monkeypatch):
    sync(env)
    settings(env, optimize=True, publish=True)
    notify(env)
    background_round(env)
    (env['root'] / 'ontology/object/object_a.md').write_text(OBJECT + '\n- 合成字段说明\n', encoding='utf-8')
    env['assistant'].finish()

    def replaced_meanwhile(method, path, token_id, ontology, issuer, sub, **kwargs):
        # 后台发布进行中，用户改了有效期：页面重新登记了新的 Hub 登记，撤销了发布线程手里的旧登记
        with store.update(env['pid'], SUBJECT) as current:
            current['delegation']['hub'] = {**current['delegation']['hub'], 'token_id': 'flywheel-replacement'}
        raise WorkspaceError('知识包发布授权已失效', 401, 'delegation_revoked')

    monkeypatch.setattr(hub_packages, 'delegated_call', replaced_meanwhile)
    background_round(env)
    publish.wait(env['pid'], SUBJECT, 'FW-001', timeout=30)
    current = state(env)
    assert store.find(current, 'FW-001')['reason'] == 'authorization'
    assert current['delegation']['hub']['token_id'] == 'flywheel-replacement'


@pytest.mark.parametrize('answer', ['received', 'lost'])
def test_a_registration_whose_answer_is_lost_is_revoked_later(env, answer):
    sync(env)
    env['evaluation'].answer_lost = answer == 'lost'
    settings(env, optimize=True)
    current = state(env)
    pending = [item['key'] for item in current['revoking']]
    if answer == 'received':
        # 登记确认后就不再待撤销
        assert current['delegation']['evaluation']['handle'] == 'evaluation-handle-1' and pending == []
        return
    # 评测服务登记了，答复却没送到：建模事先记下了句柄，留在待撤销列表里，之后照样撤销
    assert 'evaluation' not in current['delegation'] and pending == ['evaluation-handle-1']
    overdue(env)
    background_round(env)
    assert env['evaluation'].revoked == ['evaluation-handle-1'] and state(env)['revoking'] == []
    # 这是建模自己记下待清理的登记，不是用户撤销的：不写“此前撤销未完成”的审计
    assert not any('此前撤销未完成' in item['description'] for item in state(env)['audit'])


@pytest.mark.parametrize('refusal', [('评测记录不存在或无权访问', 404, 'eval_not_found'),
                                     ('评测服务未接受当前登录，请重新登录', 401, 'eval_unauthorized'),
                                     ('未配置评测服务通知凭据', 503, 'eval_unconfigured')])
def test_a_registration_refused_outright_leaves_nothing_to_revoke(env, refusal):
    env['evaluation'].refusal = WorkspaceError(*refusal)
    settings(env, optimize=True)
    # 对方明确没有登记（或根本没发出去）：句柄不进待撤销列表
    assert state(env)['revoking'] == [] and 'evaluation' not in state(env)['delegation']


def retry_due(env):
    with store.update(env['pid'], SUBJECT) as current:
        current['delegation_issues']['evaluation']['at'] -= grants.RETRY_SECONDS + 1


def test_an_unresolved_registration_is_revoked_before_registering_again(env):
    down = WorkspaceError('评测服务不可用，请稍后重试', 503, 'eval_unavailable')
    env['evaluation'].refusal, env['evaluation'].revoke_failure = down, down
    settings(env, optimize=True)
    # 服务还没恢复：不再登记新的句柄，待撤销的始终只有结果未知的那一个
    for _ in range(3):
        retry_due(env)
        sync(env, reconcile=False)
    assert [item['key'] for item in state(env)['revoking']] == ['evaluation-handle-1']
    assert [path for _, path, _ in env['evaluation'].calls].count('/delegations') == 1
    # 服务恢复：先撤销结果未知的登记，再登记新的
    env['evaluation'].refusal = env['evaluation'].revoke_failure = None
    retry_due(env)
    view = sync(env, reconcile=False)
    assert env['evaluation'].revoked == ['evaluation-handle-1'] and state(env)['revoking'] == []
    assert view['delegation']['evaluation']['status'] == 'active'
    assert state(env)['delegation']['evaluation']['handle'] == 'evaluation-handle-2'


def test_a_registration_answered_with_something_else_waits_for_the_retry_interval(env):
    env['evaluation'].garbled = True
    view = settings(env, optimize=True)
    # 2xx 却不是 JSON：同样算登记失败、记下原因，页面请求不出错，也不在每次轮询时重来
    assert '响应格式错误' in view['delegation']['evaluation']['message']
    calls = len(env['evaluation'].calls)
    sync(env, reconcile=False)
    assert not [path for _, path, _ in env['evaluation'].calls[calls:] if path.startswith('/delegations')]


def test_a_key_due_for_revocation_is_revoked_once_per_visit(env):
    down = WorkspaceError('评测服务不可用，请稍后重试', 503, 'eval_unavailable')
    env['evaluation'].refusal = down
    settings(env, optimize=True)
    env['evaluation'].refusal = None
    overdue(env)
    retry_due(env)
    calls = len(env['evaluation'].calls)
    sync(env, reconcile=False)
    # 开头的撤销重试已撤掉结果未知的登记：重新登记前不再撤销第二次
    revokes = [path for _, path, _ in env['evaluation'].calls[calls:] if path == '/delegations/current']
    assert revokes == ['/delegations/current'] and env['evaluation'].revoked == ['evaluation-handle-1']
    assert state(env)['delegation']['evaluation']['handle'] == 'evaluation-handle-2'


def test_a_refusal_without_a_registration_changes_nothing(env):
    with store.update(env['pid'], SUBJECT) as current:
        grants.reject(current, 'evaluation', USER['sub'], None)
    assert state(env)['delegation'] == {} and state(env)['delegation_issues'] == {}


def test_a_short_lifetime_is_renewed_sooner_than_a_long_one(env):
    settings(env, delegation_days=1, optimize=True)
    with store.update(env['pid'], SUBJECT) as current:
        current['delegation']['evaluation']['granted_at'] -= 86400 * grants.RENEW_SHARE - 60
    sync(env, reconcile=False)
    assert env['evaluation'].renewals == []
    with store.update(env['pid'], SUBJECT) as current:
        current['delegation']['evaluation']['granted_at'] -= 120
    sync(env, reconcile=False)
    assert env['evaluation'].renewals == [('evaluation-handle-1', 86400)]


def test_a_state_saved_before_the_lifetime_setting_still_loads(env):
    path = store._path(env['pid'], SUBJECT)
    saved = {'settings': {'optimize': False, 'publish': False, 'max_rounds': 3}, 'runs': [], 'audit': []}
    knowledge_hub.save(path, saved)
    assert sync(env, reconcile=False)['settings']['delegation_days'] == 30


def test_an_expired_delegation_stops_background_work(env):
    sync(env)
    settings(env, optimize=True)
    with store.update(env['pid'], SUBJECT) as current:
        current['delegation']['evaluation']['expires_at'] = int(time.time()) - 1
    notify(env)
    start = len(env['evaluation'].calls)
    background_round(env)
    assert calls_since(env, start) == [] and env['assistant'].sent == []


def test_stopping_and_rolling_back_revoke_what_the_switches_registered(env):
    env['evaluation'].complete(RUN)
    sync(env)
    settings(env, optimize=True, publish=True)
    assert act(env, 'FW-001', 'optimize').status_code == 200
    assert act(env, 'FW-001', 'stop').status_code == 200
    # 中止关闭了自动优化；发布与评测仍开着，评测服务的登记与登录保留
    assert state(env)['settings']['optimize'] is False and env['evaluation'].revoked == []
    assert state(env)['delegation']['evaluation']['login'] is True and 'hub' in state(env)['delegation']
    settings(env, publish=False)
    env['evaluation'].complete(NEXT_RUN, finished=3000.0)
    sync(env)
    assert act(env, 'FW-002', 'optimize').status_code == 200
    (env['root'] / 'ontology/object/object_a.md').write_text(OBJECT + '\n- 合成字段说明\n', encoding='utf-8')
    env['assistant'].finish()
    sync(env, reconcile=False)
    settings(env, publish=True)
    assert act(env, 'FW-002', 'rollback', scope='workspace').status_code == 200
    assert state(env)['delegation'] == {}


def test_background_skips_archived_projects_and_users_who_lost_write_access(env, monkeypatch):
    sync(env)
    settings(env, optimize=True)
    notify(env)
    env['manager'].set_archived(env['pid'], True)
    background_round(env)
    assert state(env)['runs'] == []
    env['manager'].set_archived(env['pid'], False)
    monkeypatch.setattr(env['manager'], 'can_write', lambda meta, user, admin: False)
    background_round(env)
    assert env['assistant'].sent == []  # 运行已建档，但不代为执行


def test_background_acts_for_nobody_who_left_the_project(env, monkeypatch):
    sync(env)
    settings(env, optimize=True)
    notify(env)
    start = len(env['evaluation'].calls)
    monkeypatch.setattr(env['manager'], 'can_access', lambda meta, user, admin: False)
    background_round(env)
    # 只请评测服务删除登记，不再读取评测结果，也不建运行
    assert [path for _, path, _ in calls_since(env, start)] == ['/delegations/current'] and state(env)['runs'] == []


@pytest.mark.parametrize('change', ['removed', 'read_only', 'archived', 'deleted'])
def test_background_withdraws_the_delegations_of_users_it_no_longer_acts_for(env, monkeypatch, change):
    settings(env, optimize=True, publish=True)
    if change == 'removed':
        monkeypatch.setattr(env['manager'], 'can_access', lambda meta, user, admin: False)
    if change in ('removed', 'read_only'):
        monkeypatch.setattr(env['manager'], 'can_write', lambda meta, user, admin: False)
    elif change == 'archived':
        env['manager'].set_archived(env['pid'], True)
    elif change == 'deleted':
        env['manager'].delete_project(env['pid'])
    background_round(env)
    current = state(env)
    # 评测服务凭句柄当即删除登记和它保存的登录；Hub 只接受用户本人的登录撤销，留待下次访问或到期
    assert current['delegation'] == {} and env['evaluation'].revoked == ['evaluation-handle-1']
    assert [item['service'] for item in current['revoking']] == ['hub'] and env['hub'].revoked == []
    assert any('后台不再代你执行' in item['description'] for item in current['audit'])
    start = len(env['evaluation'].calls)
    background_round(env)
    assert calls_since(env, start) == []
    if change == 'read_only':
        # 失去编辑权限的用户回来时，用其本人的登录补上 Hub 的撤销
        overdue(env)
        sync(env)
        assert env['hub'].revoked == env['hub'].ids and state(env)['revoking'] == []


def test_an_administrator_outside_the_project_holds_no_delegations(env, monkeypatch):
    settings(env, optimize=True, publish=True)
    # 不是项目成员的全局管理员能打开项目，但后台只代项目成员执行：此前的登记撤销，之后也不再登记
    claims = {**USER, 'role': 'admin', 'exp': time.time() + 3600}
    monkeypatch.setattr(auth, 'verify_access_token', lambda token: dict(claims) if token == 'user-token' else None)
    monkeypatch.setattr(env['manager'], 'can_access', lambda meta, user, admin: admin)
    monkeypatch.setattr(env['manager'], 'can_write', lambda meta, user, admin: admin)
    view = sync(env)
    assert state(env)['delegation'] == {} and env['evaluation'].revoked == ['evaluation-handle-1']
    assert env['hub'].revoked == env['hub'].ids
    assert view['delegation']['evaluation'] == {'status': 'issue', 'expires_at': None, 'message': grants.NOT_ACTED_FOR}
    assert view['delegation']['login'] == 'off' and view['delegation']['background'] is False
    # 页面与后台口径一致：不会一边登记、一边撤销
    for _ in range(2):
        settings(env, optimize=True, publish=True)
        background_round(env)
    assert env['evaluation'].issued == 1 and env['hub'].issued == 1 and state(env)['delegation'] == {}


@pytest.mark.parametrize('archived', [False, True])
@pytest.mark.parametrize('status, ended', [('optimizing', 'no_changes'), ('publishing', 'publish_interrupted')])
def test_background_finishes_work_of_users_it_no_longer_acts_for(env, archived, status, ended):
    env['evaluation'].complete(RUN)
    sync(env)
    # 已离开项目的成员，或归档项目里的成员：后台不再代为执行，但占用工作区的运行照样收尾，否则会一直挡住别人
    snapshot = snapshots.take(env['manager'], env['pid'], 'synthetic snapshot')
    another_member_is(env, status, member=archived, snapshot=snapshot, conversation_id='conversation_demo',
                      history_start=0)
    env['manager'].set_archived(env['pid'], archived)
    assert act(env, 'FW-001', 'optimize').status_code == 409
    start, hub_calls = len(env['evaluation'].calls), len(env['hub'].calls)
    background_round(env)
    other = store.load(env['pid'], OTHER_SUBJECT)['runs'][0]
    # 优化中的结束本轮并检查结果；发布中断（没有发布线程）的停在可重试的阻断态
    assert other['status'] == 'blocked' and other['reason'] == ended
    # 收尾都在本地：不调用评测服务和 Hub，也不启动下一步
    assert calls_since(env, start) == [] and len(env['hub'].calls) == hub_calls
    assert act(env, 'FW-001', 'optimize').status_code == 200


def test_one_unreadable_state_does_not_stop_the_others(env):
    sync(env)
    settings(env, optimize=True)
    notify(env)
    (knowledge_hub.state_dir() / 'flywheel' / env['pid'] / 'unreadable.json').write_text('{', encoding='utf-8')
    background_round(env)
    assert store.find(state(env), 'FW-001')['status'] == 'optimizing'


def test_a_withdrawal_that_fails_does_not_stop_the_others(env, monkeypatch):
    sync(env)
    settings(env, optimize=True)
    notify(env)
    # 已离开项目的另一位成员还留着登记：撤销出错只影响他自己的飞轮，排在他后面的照常推进
    with store.update(env['pid'], OTHER_SUBJECT) as other:
        other['identity'] = {'subject': OTHER_SUBJECT, 'username': 'operator_b'}
        other['delegation']['evaluation'] = {'handle': 'evaluation-handle-other', 'expires_at': time.time() + 3600,
                                             'package': PACKAGE, 'seconds': 3600, 'granted_at': time.time()}

    def broken(*args):
        raise OSError('合成的写入失败')

    monkeypatch.setattr(grants, 'withdraw', broken)
    states = sorted(store.all_states(), key=lambda item: item[1]['identity']['username'] != 'operator_b')
    monkeypatch.setattr(store, 'all_states', lambda: iter(states))
    background_round(env)
    assert store.find(state(env), 'FW-001')['status'] == 'optimizing'


class Recorder(BaseHTTPRequestHandler):
    seen = []

    def _answer(self):
        Recorder.seen.append(dict(self.headers.items()))
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_DELETE = _answer

    def log_message(self, *args):
        pass


@pytest.fixture
def recorder(monkeypatch):
    server = ThreadingHTTPServer(('127.0.0.1', 0), Recorder)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    Recorder.seen = []
    address = f'http://127.0.0.1:{server.server_port}'
    monkeypatch.setattr(config, 'EVAL_URL', address)
    monkeypatch.setenv('ONTO_HUB_URL', address)
    monkeypatch.setenv('ONTO_HUB_SERVICE_TOKEN', 'synthetic-hub-service')
    monkeypatch.setenv('ONTO_EVALUATION_SERVICE_TOKEN', SERVICE_TOKEN)
    yield Recorder
    server.shutdown()


def test_delegated_calls_send_the_handle_and_no_user_token(recorder):
    hub_packages.delegated_call('POST', '/api/package-publish-requests', 'flywheel-demo', PACKAGE,
                                USER['iss'], USER['sub'], body={})
    eval_client.call('GET', '/runs/run_demo', Delegated('evaluation-handle'))
    hub, evaluation = recorder.seen
    # Hub 收到的是本服务凭据签名、只对这一次请求有效的授权，登记编号与 MCP 发布同一格式
    encoded, signature = hub['X-Hub-Delegation'].split('.')
    expected = hmac.new(b'synthetic-hub-service', b'hub-delegation-v1.' + encoded.encode(), hashlib.sha256).hexdigest()
    payload = json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
    assert signature == expected and hub['X-Hub-Service-Token'] == 'synthetic-hub-service'
    assert payload['delegation_id'] == 'mcp-flywheel-demo' and payload['ontology_id'] == PACKAGE
    assert (payload['method'], payload['path'], payload['issuer'], payload['sub']) == (
        'POST', '/api/package-publish-requests', USER['iss'], USER['sub'])
    assert evaluation['X-Evaluation-Delegation'] == 'evaluation-handle'
    assert evaluation['X-Modeling-Service-Token'] == SERVICE_TOKEN
    assert 'Authorization' not in hub and 'Authorization' not in evaluation


def test_user_calls_send_no_service_identity_to_the_evaluation_service(recorder):
    eval_client.call('GET', '/runs/run_demo', 'user-token')
    eval_client.delegate('user-token', PACKAGE, 60, 'evaluation-handle')
    plain, registering = recorder.seen
    assert plain['Authorization'] == 'Bearer user-token' and 'X-Modeling-Service-Token' not in plain
    assert registering['Authorization'] == 'Bearer user-token' and registering['X-Modeling-Service-Token'] == SERVICE_TOKEN
    # 登记时句柄在请求体里交给评测服务，不当作委托调用的凭据
    assert 'X-Evaluation-Delegation' not in registering


def overdue(env):
    with store.update(env['pid'], SUBJECT) as current:
        for item in current['revoking']:
            item['at'] -= grants.RETRY_SECONDS + 1


def test_a_failed_revocation_is_retried_until_the_service_confirms(env):
    settings(env, optimize=True, publish=True)
    failure = WorkspaceError('服务不可用，请稍后重试', 503, 'unavailable')
    env['evaluation'].revoke_failure = env['hub'].revoke_failure = failure
    settings(env, optimize=False, publish=False)
    assert sorted(item['service'] for item in state(env)['revoking']) == ['evaluation', 'hub']
    # 重试有间隔：刚失败时不重试
    background_round(env)
    assert env['evaluation'].revoked == [] and env['hub'].revoked == []
    # 对方仍不可用：继续保留，等下次重试
    overdue(env)
    background_round(env)
    assert len(state(env)['revoking']) == 2
    env['evaluation'].revoke_failure = env['hub'].revoke_failure = None
    overdue(env)
    background_round(env)
    # 对方确认撤销后，句柄彻底删除；评测服务据此同时删掉保存的后台评测登录
    assert env['evaluation'].revoked == ['evaluation-handle-1'] and env['hub'].revoked == []
    # Hub 只接受用户本人的登录撤销：等用户回来时重试
    assert [item['service'] for item in state(env)['revoking']] == ['hub']
    sync(env, reconcile=False)
    assert env['hub'].revoked == env['hub'].ids
    current = state(env)
    assert current['revoking'] == [] and 'handle-' not in json.dumps(current) and 'flywheel-' not in json.dumps(current)
    assert sum('此前撤销未完成' in item['description'] for item in current['audit']) == 2


def test_a_revocation_is_given_up_once_the_delegation_ran_out_there(env):
    settings(env, optimize=True)
    env['evaluation'].revoke_failure = WorkspaceError('评测服务不可用，请稍后重试', 503, 'eval_unavailable')
    settings(env, optimize=False)
    with store.update(env['pid'], SUBJECT) as current:
        current['revoking'][0]['expires_at'] = time.time() - 1
    background_round(env)
    # 对方的记录已经到期、会被自己清理，不必再撤销
    assert state(env)['revoking'] == [] and env['evaluation'].revoked == []
