"""Delegated reads and reruns: registered once with the user's login, used later by onto-modeling alone.

Modeling reads a user's evaluation results while the user is away, so these calls carry no user token.
A delegation names one package and covers reading its evaluations; a later call presents the service
credential together with the delegation handle, and neither works alone. Only the digest of a handle is
stored. A delegation ends when the user turns the flywheel switch off, which destroys it, or when it runs
out. It is not tied to the account's login: changing a password does not end it.

A delegation may also hold a login, so that modeling can start evaluations while the user is away: an
evaluation runs as the user against Consumer and Hub. The login is a refresh token of its own, minted from
the one the user handed over when registering, kept encrypted and renewed here. Destroying the delegation
deletes it. The account service cannot void a single refresh token, so a copy that leaked before then stays
valid until it runs out, at most one refresh lifetime after its last renewal. It voids all of a user's refresh
tokens at once instead: logging out on any device, changing the password or deleting the account voids the
login too, and evaluations wait until the user authorizes again; reads go on.
"""
import hashlib
import hmac
import time

from .core import Failure
from .execution import expiry

SERVICE_HEADER = 'x-modeling-service-token'
HEADER = 'x-evaluation-delegation'
MAX_SECONDS = 3650 * 86400
# 保存的登录在剩余有效期过半前续期，续期间隔最长 12 小时
RENEW_EVERY_SECONDS = 12 * 3600


def _key(handle):
    return hashlib.sha256(handle.encode()).hexdigest()


def _rejected(exc):
    # 刷新令牌被拒：账号服务对无效刷新令牌返回 400，被吊销的登录返回 401
    return exc.status == 401 or exc.upstream_status in (400, 401, 403)


def _login_expired():
    return Failure('login_expired', '后台评测授权已失效，请在数据飞轮中重新打开开关', 401, 'authorization')


class Delegations:
    def __init__(self, config, store, identity):
        self.config, self.store, self.identity = config, store, identity

    def require_service(self, request):
        expected = self.config.modeling_service_token
        supplied = request.headers.get(SERVICE_HEADER, '')
        if not expected or not hmac.compare_digest(expected.encode(), supplied.encode()):
            raise Failure('unauthenticated', '建模服务身份无效', 401, 'authorization')

    def _mint(self, refresh, owner):
        """A token pair of its own for this delegation, checked to belong to the delegating user."""
        try:
            value = self.identity.renew(refresh)
        except Failure as exc:
            if _rejected(exc):
                raise _login_expired() from exc
            raise
        if self.identity.profile(value['access_token'])['subject'] != owner:
            raise Failure('identity_mismatch', '刷新令牌与当前登录不是同一账号', 401, 'authorization')
        return value

    def register(self, owner, ontology_id, seconds, handle, refresh=''):
        """Modeling chooses the handle and keeps it before asking, so a registration whose answer got lost can
        still be revoked. Only its digest is stored; it is useless without the modeling service credential.
        A handle already registered is refused."""
        if self.store.delegation(_key(handle)):
            raise Failure('delegation_conflict', '委托句柄已登记过，请重新登记', 409, 'authorization')
        login = self._mint(refresh, owner)['refresh_token'] if refresh else None
        expires_at = int(time.time()) + seconds
        self.store.put_delegation(_key(handle), owner, ontology_id, expires_at, login)
        return {'delegation': handle, 'expires_at': expires_at, 'login': login is not None}

    def extend(self, request, owner, seconds):
        """The user is back: their delegation runs for the chosen period again, its login is kept."""
        self.require_service(request)
        grant = self.store.delegation(_key(request.headers.get(HEADER, '')))
        if not grant or grant['owner'] != owner or grant['expires'] <= time.time():
            raise Failure('delegation_invalid', '委托已失效', 401, 'authorization')
        expires_at = int(time.time()) + seconds
        self.store.extend_delegation(grant['key'], expires_at)
        return {'delegation': request.headers.get(HEADER), 'expires_at': expires_at,
                'login': self.store.delegation_login(grant['key']) is not None}

    def revoke(self, request):
        """Idempotent: a delegation that is gone is destroyed, with its login."""
        self.require_service(request)
        handle = request.headers.get(HEADER, '')
        if handle:
            self.store.drop_delegation(_key(handle))
        return {'revoked': True}

    def verify(self, request):
        """The delegation of this call, or None when it has none and must present a user's login."""
        handle = request.headers.get(HEADER, '')
        if not handle:
            return None
        self.require_service(request)
        grant = self.store.delegation(_key(handle))
        if not grant or grant['expires'] <= time.time():
            raise Failure('delegation_invalid', '委托已失效', 401, 'authorization')
        return grant

    def login(self, grant):
        """A fresh access token and refresh token for one evaluation started on this delegation."""
        stored = self.store.delegation_login(grant['key'])
        if not stored:
            raise Failure('login_missing', '后台评测授权未登记，请在数据飞轮中重新打开开关', 401, 'authorization')
        try:
            value = self.identity.renew(stored)
        except Failure as exc:
            if _rejected(exc):
                self.store.drop_login(grant['key'])
                raise _login_expired() from exc
            raise
        self.store.put_login(grant['key'], value['refresh_token'])
        return value['access_token'], value['refresh_token']

    def maintain(self, now=None):
        """Drop expired delegations; renew stored logins well before they run out."""
        now = now or time.time()
        self.store.purge_delegations(now)
        for key, login, renewed in self.store.logins():
            exp = expiry(login)
            due = now - renewed >= RENEW_EVERY_SECONDS or (exp and exp - now < (exp - renewed) / 2)
            if not due:
                continue
            try:
                self.store.put_login(key, self.identity.renew(login)['refresh_token'])
            except Failure as exc:
                if _rejected(exc):
                    self.store.drop_login(key)  # 任一设备退出登录、改密码或账号删除：登录作废，后台不再发起评测
