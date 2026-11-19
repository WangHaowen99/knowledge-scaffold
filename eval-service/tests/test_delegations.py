"""Delegated reads: what a delegation allows, and above all what it does not. All data is synthetic."""
import secrets
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from evaluation.core import Failure
from evaluation.delegations import Delegations
from test_evaluation import preparation
from test_run_artifacts import service, settled, start

PACKAGE = 'knowledge_package_demo'
CREDENTIAL = 'synthetic-notification-credential'
API = '/evaluation/api'


@pytest.fixture
def evaluation(service):
    client, app, deps, _, _ = service
    record = start(client, preparation(client), [('Synthetic question', '2')], 'run_delegated')
    settled(client, record['id'])
    return client, app, deps, record['id']


def new_handle():
    # 句柄由建模生成：登记前先记下，答复丢了也能凭它撤销
    return secrets.token_urlsafe(32)


def register(client, ontology_id=PACKAGE, credential=CREDENTIAL, expires_in=86400, handle=None):
    body = {'ontology_id': ontology_id, 'expires_in': expires_in, 'delegation': handle or new_handle()}
    return client.post(f'{API}/delegations', headers={'X-Modeling-Service-Token': credential}, json=body)


def delegated(handle, credential=CREDENTIAL):
    # 委托调用不带用户令牌：覆盖测试客户端默认的 Authorization
    return {'Authorization': '', 'X-Modeling-Service-Token': credential, 'X-Evaluation-Delegation': handle}


def grant(client, **body):
    response = register(client, **body)
    assert response.status_code == 201, response.text
    return delegated(response.json()['delegation'])


def test_delegation_reads_the_package_evaluations_without_a_user_token(evaluation):
    client, _, _, rid = evaluation
    auth = grant(client)
    listing = client.get(f'{API}/package-evaluations?ontology_id={PACKAGE}', headers=auth)
    assert listing.status_code == 200 and listing.json()['releases'][0]['run_id'] == rid
    assert client.get(f'{API}/runs/{rid}', headers=auth).json()['id'] == rid
    report = client.get(f'{API}/runs/{rid}/artifacts/report.md', headers=auth)
    assert report.status_code == 200 and report.headers['x-artifact-revision'] == '1'


def test_the_handle_needs_the_service_credential(evaluation):
    client, _, _, rid = evaluation
    handle = register(client).json()['delegation']
    url = f'{API}/runs/{rid}'
    assert client.get(url, headers={'Authorization': '', 'X-Evaluation-Delegation': handle}).status_code == 401
    assert client.get(url, headers=delegated(handle, 'wrong-value')).status_code == 401
    assert client.get(url, headers=delegated('unknown-handle')).status_code == 401
    # 服务凭证本身不代表任何用户
    assert client.get(url, headers={'Authorization': '', 'X-Modeling-Service-Token': CREDENTIAL}).status_code == 401


def test_delegation_covers_only_reading_that_package(evaluation):
    client, _, _, rid = evaluation
    auth = grant(client, ontology_id='knowledge_package_other')
    assert client.get(f'{API}/package-evaluations?ontology_id={PACKAGE}', headers=auth).status_code == 404
    assert client.get(f'{API}/runs/{rid}', headers=auth).status_code == 404
    assert client.get(f'{API}/runs/{rid}/artifacts/report.md', headers=auth).status_code == 404


def test_delegation_opens_no_other_route(evaluation):
    client, _, _, rid = evaluation
    auth = grant(client)
    for method, path in (('get', '/package-tasks'), ('get', f'/runs/{rid}/results'), ('get', f'/runs/{rid}/artifacts'),
                         ('post', f'/runs/{rid}/artifacts/regenerate'), ('delete', f'/runs/{rid}')):
        assert getattr(client, method)(API + path, headers=auth).status_code == 401, path
    response = client.post(f'{API}/delegations', headers=auth,
                           json={'ontology_id': PACKAGE, 'expires_in': 60, 'delegation': new_handle()})
    assert response.status_code == 401


def test_delegated_calls_do_not_ask_the_account_service(evaluation):
    client, _, deps, rid = evaluation
    auth = grant(client)
    deps.auth_available = False  # 账号服务不可用时，用户登录的请求都会失败，委托调用不受影响
    assert client.get(f'{API}/runs/{rid}', headers=auth).status_code == 200


def test_expired_and_revoked_delegations_are_refused(evaluation):
    client, _, _, rid = evaluation
    url = f'{API}/runs/{rid}'
    auth = grant(client, expires_in=60)
    with patch('evaluation.delegations.time.time', return_value=time.time() + 120):
        assert client.get(url, headers=auth).status_code == 401
    assert client.delete(f'{API}/delegations/current', headers={**auth, 'X-Modeling-Service-Token': 'wrong-value'}).status_code == 401
    assert client.get(url, headers=auth).status_code == 200
    assert client.delete(f'{API}/delegations/current', headers=auth).json() == {'revoked': True}
    assert client.delete(f'{API}/delegations/current', headers=auth).status_code == 200
    assert client.get(url, headers=auth).status_code == 401


def test_registering_needs_the_user_and_the_service(evaluation):
    client, _, _, _ = evaluation
    assert register(client, credential='wrong-value').status_code == 401
    body = {'ontology_id': PACKAGE, 'expires_in': 60, 'delegation': new_handle()}
    response = client.post(f'{API}/delegations', json=body,
                           headers={'Authorization': '', 'X-Modeling-Service-Token': CREDENTIAL})
    assert response.status_code == 401
    assert register(client, expires_in=3650 * 86400).status_code == 201
    assert register(client, expires_in=3651 * 86400).status_code == 422
    # 时长必须显式给出：上限很长，不能因为漏传就得到它
    response = client.post(f'{API}/delegations', json={'ontology_id': PACKAGE, 'delegation': new_handle()},
                           headers={'X-Modeling-Service-Token': CREDENTIAL})
    assert response.status_code == 422
    # 句柄也必须由建模给出：没有它，答复丢失的登记就没人能撤销
    response = client.post(f'{API}/delegations', json={'ontology_id': PACKAGE, 'expires_in': 60},
                           headers={'X-Modeling-Service-Token': CREDENTIAL})
    assert response.status_code == 422


def test_a_registration_whose_answer_was_lost_can_still_be_revoked(evaluation):
    client, app, _, rid = evaluation
    handle = new_handle()
    assert register(client, handle=handle).status_code == 201  # 假设这个答复没有到达建模
    assert client.get(f'{API}/runs/{rid}', headers=delegated(handle)).status_code == 200
    assert client.delete(f'{API}/delegations/current', headers=delegated(handle)).json() == {'revoked': True}
    assert client.get(f'{API}/runs/{rid}', headers=delegated(handle)).status_code == 401
    # 同一个句柄只能登记一次：不会覆盖已有的登记
    other = new_handle()
    assert register(client, handle=other).status_code == 201
    assert register(client, handle=other, ontology_id='knowledge_package_other').status_code == 409
    assert client.get(f'{API}/runs/{rid}', headers=delegated(other)).status_code == 200


def test_the_handle_is_not_stored_in_clear(evaluation):
    client, app, _, _ = evaluation
    handle = register(client).json()['delegation']
    with app.state.store.lock:
        rows = [dict(row) for row in app.state.store.db.execute('SELECT * FROM delegations')]
    assert rows and not any(handle in str(row) for row in rows)


def test_no_service_credential_configured_means_no_delegation():
    delegations = Delegations(SimpleNamespace(modeling_service_token=''), None, None)
    with pytest.raises(Failure) as caught:
        delegations.require_service(SimpleNamespace(headers={'x-modeling-service-token': ''}))
    assert caught.value.status == 401


# ---- 登录：委托在用户不在时发起评测


class Logins:
    """The account service's refresh grant: which refresh token belongs to whom, and which are void."""

    def __init__(self, deps):
        self.users = {'refresh-of-a': 'synthetic-user-a', 'refresh-of-b': 'synthetic-user-b'}
        self.void, self.issued, self.original = set(), 0, deps.json

    def __call__(self, method, base, path, **kwargs):
        if path != '/token':
            return self.original(method, base, path, **kwargs)
        refresh = kwargs['json']['refresh_token']
        user = self.users.get(refresh)
        if not user or refresh in self.void:
            raise Failure('dependency_rejected', 'Rejected', 503, 'dependency', upstream_status=400)
        self.issued += 1
        renewed = f'refresh-{self.issued}-of-{user[-1]}'
        self.users[renewed] = user
        return {'access_token': user, 'refresh_token': renewed}


@pytest.fixture
def logins(evaluation):
    client, app, deps, rid = evaluation
    accounts = Logins(deps)
    deps.json = accounts
    release = dict(deps.receipt, release_id='release_next')
    deps.releases['release_next'] = (deps.raw, release)
    return client, app, deps, rid, accounts


def register_login(client, refresh='refresh-of-a', **body):
    return client.post(f'{API}/delegations', headers={'X-Modeling-Service-Token': CREDENTIAL, 'X-Refresh-Token': refresh},
                       json={'ontology_id': PACKAGE, 'expires_in': 86400, 'delegation': new_handle(), **body})


def rerun(client, rid, headers, request_id='rerun_demo', release='release_next'):
    return client.post(f'{API}/runs/{rid}/reruns', headers=headers,
                       json={'request_id': request_id, 'package_ref': {'ontology_id': PACKAGE, 'release_id': release}})


def stored_logins(app):
    with app.state.store.lock:
        return [dict(row) for row in app.state.store.db.execute('SELECT * FROM delegation_logins')]


def test_a_delegation_with_a_login_starts_an_evaluation_while_the_user_is_away(logins):
    client, app, _, rid, _ = logins
    response = register_login(client)
    assert response.status_code == 201 and response.json()['login'] is True
    started = rerun(client, rid, delegated(response.json()['delegation']))
    assert started.status_code == 202, started.text
    run = started.json()
    assert run['base_ref']['release_id'] == 'release_next' and run['id'] != rid
    source = client.get(f'{API}/runs/{rid}').json()
    assert run['eval_set_id'] == source['eval_set_id'] and run['concurrency'] == source['concurrency']
    # 同一请求重复发起不会建第二个评测
    assert rerun(client, rid, delegated(response.json()['delegation'])).json()['id'] == run['id']


def test_the_stored_login_is_a_new_encrypted_token(logins):
    client, app, _, _, accounts = logins
    register_login(client)
    rows = stored_logins(app)
    assert len(rows) == 1 and accounts.issued == 1
    # 存的是新换出的令牌，而且加密：交接来的和新换出的都不以明文出现
    assert not any(value in str(rows) for value in ('refresh-of-a', 'refresh-1-of-a'))


def test_a_refresh_token_of_another_account_is_refused(logins):
    client, app, _, _, _ = logins
    response = register_login(client, refresh='refresh-of-b')
    assert response.status_code == 401 and stored_logins(app) == []
    assert register_login(client, refresh='unknown-refresh').status_code == 401


def test_a_delegation_without_a_login_cannot_start_evaluations(logins):
    client, _, _, rid, _ = logins
    auth = grant(client)
    response = rerun(client, rid, auth)
    assert response.status_code == 401 and response.json()['code'] == 'login_missing'


def test_reruns_stay_within_the_delegated_package(logins):
    client, _, _, rid, _ = logins
    auth = delegated(register_login(client).json()['delegation'])
    response = client.post(f'{API}/runs/{rid}/reruns', headers=auth,
                           json={'request_id': 'rerun_other', 'package_ref': {'ontology_id': 'knowledge_package_other', 'release_id': 'release_next'}})
    assert response.status_code == 404


def test_a_voided_login_is_dropped_and_stops_background_evaluations(logins):
    client, app, _, rid, accounts = logins
    auth = delegated(register_login(client).json()['delegation'])
    accounts.void.update(accounts.users)  # 改了密码：所有刷新令牌作废
    response = rerun(client, rid, auth)
    assert response.status_code == 401 and response.json()['code'] == 'login_expired'
    assert stored_logins(app) == []


def test_closing_the_switch_destroys_the_login(logins):
    client, app, _, rid, _ = logins
    auth = delegated(register_login(client).json()['delegation'])
    assert client.delete(f'{API}/delegations/current', headers=auth).status_code == 200
    assert stored_logins(app) == [] and rerun(client, rid, auth).status_code == 401


def test_renewing_extends_the_delegation_and_keeps_its_login(logins):
    client, app, _, _, _ = logins
    handle = register_login(client).json()['delegation']
    auth = {**delegated(handle), 'Authorization': 'Bearer synthetic-user-a'}
    response = client.post(f'{API}/delegations/current/renewal', headers=auth, json={'expires_in': 7 * 86400})
    assert response.status_code == 200 and response.json()['login'] is True
    assert response.json()['expires_at'] > time.time() + 6 * 86400
    # 只有登记时的同一账号才能续期
    other = {**auth, 'Authorization': 'Bearer synthetic-user-b'}
    assert client.post(f'{API}/delegations/current/renewal', headers=other, json={'expires_in': 60}).status_code == 401


def test_maintenance_renews_logins_and_drops_void_ones(logins):
    client, app, _, _, accounts = logins
    register_login(client)
    delegations = app.state.delegations
    delegations.maintain(time.time() + 13 * 3600)
    assert accounts.issued == 2 and len(stored_logins(app)) == 1
    accounts.void.update(accounts.users)
    delegations.maintain(time.time() + 26 * 3600)
    assert stored_logins(app) == []


def test_expired_delegations_take_their_logins_with_them(logins):
    client, app, _, _, _ = logins
    register_login(client, expires_in=60)
    app.state.store.purge_delegations(time.time() + 120)
    assert stored_logins(app) == []


def test_a_user_can_rerun_with_their_own_login(logins):
    client, _, _, rid, _ = logins
    response = rerun(client, rid, {'X-Refresh-Token': 'refresh-of-a'})
    assert response.status_code == 202 and response.json()['base_ref']['release_id'] == 'release_next'


def test_a_delegation_cannot_confirm_deleting_graph_data(logins):
    client, _, _, rid, _ = logins
    auth = delegated(register_login(client).json()['delegation'])
    response = client.post(f'{API}/runs/{rid}/reruns', headers=auth, json={
        'request_id': 'rerun_graph', 'package_ref': {'ontology_id': PACKAGE, 'release_id': 'release_next'},
        'replace_existing_graph': True})
    assert response.status_code == 403
