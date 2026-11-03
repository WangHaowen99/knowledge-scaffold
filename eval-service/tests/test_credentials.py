"""Run logins kept in memory: renewal before expiry, sparse identity checks and clear expiry. Synthetic data only."""
import base64
import json
import time

import pytest

from evaluation.core import Failure
from evaluation.execution import LOGIN_EXPIRED, RENEW_SECONDS, VERIFY_SECONDS, Credential, Engine, expiry

OWNER, RUN = 'owner_demo', 'run_demo'


def jwt(exp, marker='a'):
    payload = base64.urlsafe_b64encode(json.dumps({'exp': exp, 'm': marker}).encode()).decode().rstrip('=')
    return f'header.{payload}.signature'


class Identity:
    """Synthetic account service: knows which access tokens belong to which owner and which refresh tokens work."""

    def __init__(self):
        self.owners, self.refresh, self.checks, self.renewals = {}, {}, [], []

    def verify(self, token):
        self.checks.append(token)
        if token not in self.owners:
            raise Failure('dependency_rejected', '依赖服务拒绝请求', 401, 'dependency', upstream_status=401)
        return self.owners[token]

    def renew(self, refresh):
        self.renewals.append(refresh)
        if refresh not in self.refresh:
            raise Failure('dependency_rejected', '依赖服务拒绝请求', 503, 'dependency', upstream_status=400)
        return self.refresh[refresh]


@pytest.fixture
def engine():
    value = Engine(None, Identity(), None, None, None, None)
    yield value
    value.pool.shutdown(wait=False)


def test_expiry_reads_only_the_exp_claim():
    assert expiry(jwt(1793500000)) == 1793500000
    assert expiry('not-a-jwt') == 0.0 and expiry('a.%%%.c') == 0.0


def test_a_login_is_checked_at_most_once_a_minute(engine):
    access = jwt(time.time() + 900)
    engine.identity.owners[access] = OWNER
    engine.tokens[(OWNER, RUN)] = Credential(access, 'synthetic-refresh')
    assert all(engine.token(OWNER, RUN) == access for _ in range(5))
    assert engine.identity.checks == [access] and engine.identity.renewals == []
    # A minute later (the check time moved back instead of patching the shared clock).
    engine.tokens[(OWNER, RUN)].checked -= VERIFY_SECONDS + 1
    engine.token(OWNER, RUN)
    assert engine.identity.checks == [access, access]


def test_a_token_about_to_expire_is_renewed_and_checked_before_use(engine):
    old, new = jwt(time.time() + RENEW_SECONDS - 30, 'old'), jwt(time.time() + 900, 'new')
    engine.identity.owners.update({old: OWNER, new: OWNER})
    engine.identity.refresh['synthetic-refresh'] = {'access_token': new, 'refresh_token': 'synthetic-refresh-2'}
    engine.tokens[(OWNER, RUN)] = Credential(old, 'synthetic-refresh')
    assert engine.token(OWNER, RUN) == new
    credential = engine.tokens[(OWNER, RUN)]
    assert credential.refresh == 'synthetic-refresh-2' and engine.identity.checks == [new]


def test_a_rejected_refresh_is_dropped_and_the_current_token_used_until_it_expires(engine):
    access = jwt(time.time() + 60)
    engine.identity.owners[access] = OWNER
    engine.tokens[(OWNER, RUN)] = Credential(access, 'synthetic-revoked')
    assert engine.token(OWNER, RUN) == access
    assert engine.tokens[(OWNER, RUN)].refresh == '' and engine.identity.renewals == ['synthetic-revoked']
    engine.token(OWNER, RUN)
    assert engine.identity.renewals == ['synthetic-revoked']


def test_an_expired_login_and_a_changed_identity_are_reported_clearly(engine):
    engine.tokens[(OWNER, RUN)] = Credential(jwt(time.time() - 10))
    with pytest.raises(Failure) as raised:
        engine.token(OWNER, RUN)
    assert raised.value.code == 'login_expired' and raised.value.status == 401 and raised.value.message == LOGIN_EXPIRED
    other = jwt(time.time() + 900, 'other')
    engine.identity.owners[other] = 'owner_other'
    engine.tokens[(OWNER, RUN)] = Credential(other)
    with pytest.raises(Failure) as raised:
        engine.token(OWNER, RUN)
    assert raised.value.code == 'identity_changed'
    with pytest.raises(Failure) as raised:
        engine.token(OWNER, 'run_unknown')
    assert raised.value.code == 'login_expired'


def test_a_token_without_a_readable_expiry_is_not_renewed_on_every_use(engine):
    engine.identity.owners['synthetic-opaque'] = OWNER
    engine.tokens[(OWNER, RUN)] = Credential('synthetic-opaque', 'synthetic-refresh')
    assert engine.token(OWNER, RUN) == 'synthetic-opaque' and engine.identity.renewals == []
