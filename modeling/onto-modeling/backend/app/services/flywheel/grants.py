"""Delegations that keep the flywheel running while its user is away.

Each switch registers with the service it needs, with the user's login and while the user is present:
reading evaluations with the evaluation service (either switch), publication with Hub. Automatic
publication and evaluation also has the evaluation service keep a login of its own for the delegation,
from the refresh token the browser hands over when the switch is turned on, so evaluations of new
versions start while the user is away. For the evaluation service what is kept here is a handle, chosen
here and useless without this service's own credential. A new key (handle or Hub registration ID) is written
to ``revoking`` before registering and leaves it once the service confirmed or refused: a registration whose
answer got lost is revoked like any other, and is revoked before the service registers again. Hub uses the
publication authorization of the MCP publication tokens: a registration ID, and every delegated Hub request
carries a short-lived header signed with this service's Hub credential. Neither reaches the browser. Turning a
switch off destroys its delegation: it stops being used at once, and the service is told to delete it. When the
service cannot be reached the registration is kept for that revocation alone and retried, so nothing goes on
being renewed unused: in the background for the evaluation service, on the user's next visit for Hub, which
deletes a registration only on the user's own login. The user chooses how long work goes on after their last
visit: each visit starts that period again, so a user who stopped coming stops being acted for. Users the
background rounds do not act for (acts_for: a deleted or archived project, a user who is not its owner) hold
no delegations: none are registered for them and those they had are withdrawn. Delegations are not tied to the
login: changing a password does not end them, the switch does. The login the evaluation service keeps is: the
account service voids all of a user's refresh tokens at once, so logging out on any device or changing the
password ends it, and evaluations wait for the user to authorize again (lose_login).
"""
import json
import logging
import secrets
import time

from .. import eval_client, hub_packages, ownership
from ..delegated import Delegated
from ..workspace import WorkspaceError
from . import store

_log = logging.getLogger(__name__)
SERVICES = ('evaluation', 'hub')
LABELS = {'evaluation': '评测服务', 'hub': 'Hub'}
# 每个服务的登记以什么标识：都由本服务生成，评测服务的句柄与 Hub 的登记编号
KEYS = {'evaluation': 'handle', 'hub': 'token_id'}
# “无限制”用各服务接受的最长时长，登记记录因此都有到期时间，代码只有一条路径
UNLIMITED_SECONDS = 3650 * 86400
# 用户在时，已过去这部分时长就重新登记，让“离开后多久”从最近一次访问算起
RENEW_SHARE = 0.1
# 登记失败后的重试间隔：页面轮询很频繁，旧版本服务不支持委托时不能每次都重试
RETRY_SECONDS = 300


def lifetime(state):
    days = state['settings']['delegation_days']
    return days * 86400 if days else UNLIMITED_SECONDS


def label(days):
    return f'{days} 天' if days else '无限制'


def wanted(state):
    settings = state['settings']
    services = []
    if settings['optimize'] or settings['publish']:
        services.append('evaluation')
    if settings['publish']:
        services.append('hub')
    return services


def login_wanted(state):
    return bool(state['settings']['publish'])


# 后台不代其执行的用户在页面上看到的原因
NOT_ACTED_FOR = '后台只代项目所有者执行：项目已归档，或你不是该项目的所有者'


def acts_for(manager, meta, username):
    """Whether delegations may act for the user: the project's owner (single ownership), in a project that is not
    archived, never a global administrator's access. Registering and withdrawing both follow this rule, otherwise
    one would undo the other on every visit."""
    return ownership.owns(meta, username) and not meta.get('archived')


def evaluation_login(state):
    """The evaluation delegation when the evaluation service holds a login for it; evaluations start on it."""
    grant = state['delegation'].get('evaluation') or {}
    return credential(state, 'evaluation') if grant.get('login') else None


def lose_login(state, username):
    """The evaluation service no longer has a valid login (logged out anywhere, a changed password): ask the user
    to renew it."""
    grant = state['delegation'].get('evaluation')
    if grant and grant.get('login'):
        grant['login'] = False
        store.audit(state, username, '后台授权', '评测服务的后台评测登录已失效，重新打开开关后恢复')


def active(state, service):
    grant = state['delegation'].get(service)
    # 早期测试版本的 Hub 登记没有登记编号，视为失效，用户下次访问时重新登记
    return bool(grant) and KEYS[service] in grant and grant['expires_at'] > time.time()


def credential(state, service):
    """What a background round calls the service with: the evaluation handle, or the Hub registration."""
    if not active(state, service):
        return None
    grant = state['delegation'][service]
    if service == 'hub':
        return {'token_id': grant['token_id'], 'issuer': grant['issuer'], 'sub': grant['sub']}
    return Delegated(grant['handle'])


def _action(state, service, package, refresh):
    """register, renew or None: what this service's delegation needs now."""
    grant = state['delegation'].get(service)
    seconds = lifetime(state)
    issue = state['delegation_issues'].get(service)
    # 用户改了有效期：不等失败后的重试间隔，旧时长的授权不能继续用（登记失败时 align 停用它）
    changed = active(state, service) and grant.get('seconds') not in (None, seconds)
    if issue and time.time() - issue['at'] < RETRY_SECONDS and not changed:
        return None
    if not active(state, service) or grant['package'] != package or grant['seconds'] != seconds:
        return 'register'
    if service == 'evaluation' and grant.get('login') != login_wanted(state):
        # 打开时需要转交来的刷新令牌才能让评测服务保存登录；关闭时重新登记即销毁原来的登录
        if not login_wanted(state) or refresh:
            return 'register'
    if time.time() - grant['granted_at'] >= seconds * RENEW_SHARE:
        # 评测服务的续期保留委托带着的登录；Hub 用同一登记编号重新登记，只延长有效期
        return 'renew'
    return None


def _due(state, needed, package, refresh):
    """The registrations each wanted service needs now; none before the project has a Hub package."""
    actions = {}
    if not package:
        return actions
    for service in needed:
        action = _action(state, service, package, refresh)
        if action:
            actions[service] = action
    return actions


def _stale(state, service, needed, due):
    """A registration to revoke: its switch is off; or it still holds the evaluation service's login after the publish
    switch went off and this visit does not register it again (waiting after a failed registration, no Hub package)."""
    if service not in needed:
        return True
    return bool(state['delegation'][service].get('login')) and not login_wanted(state) and service not in due


def _hub_register(caller, token_id, issuer, sub, seconds):
    expires_at = time.time() + seconds
    hub_packages.register_delegation(caller.pid, token_id, caller.token, issuer, sub, expires_at)
    return expires_at


def _new_key(service):
    """A registration's key, chosen here: Hub's registration ID, the evaluation service's handle."""
    return 'flywheel-' + secrets.token_hex(16) if service == 'hub' else secrets.token_urlsafe(32)


def _hold(caller, keys, seconds):
    """Write the new keys down before asking. When an answer gets lost, or the process stops, the service may
    hold a registration nothing here knows of; kept in ``revoking`` until confirmed, it is revoked like any other."""
    if not keys:
        return
    now = time.time()
    with store.update(caller.pid, caller.subject) as state:
        state['revoking'].extend({'service': service, 'key': key, 'expires_at': now + seconds + RETRY_SECONDS,
                                  'at': now, 'unconfirmed': True} for service, key in keys.items())


def _registering(due):
    return [service for service, action in due.items() if action == 'register']


def _refused(exc):
    """The service answered that it registered nothing, or nothing was sent: no registration to revoke later.
    A lost answer, an unreachable service or a server error leave the outcome unknown."""
    return exc.code == 'eval_unconfigured' or exc.status in (400, 401, 403, 404, 422)


def _unresolved(caller, due):
    """Services to register again while an earlier registration of theirs, outcome unknown, still waits for its
    revocation. Revoke those first, with the user's login: registering once more before that would heap up keys
    to revoke. Runs at most once per retry interval, as registering does; the services it fails for wait."""
    # 重新读取：本次请求开头的撤销重试可能刚撤掉了其中一些
    waiting = [item for item in store.load(caller.pid, caller.subject)['revoking'] if item.get('unconfirmed')]
    pending = [item for item in waiting if item['service'] in _registering(due)]
    revoked = {item['key'] for item in pending if _revoke(item['service'], item['key'], caller.token)}
    if revoked:
        with store.update(caller.pid, caller.subject) as current:
            current['revoking'] = [item for item in current['revoking'] if item['key'] not in revoked]
    return {item['service'] for item in pending if item['key'] not in revoked}


def _register(service, caller, package, seconds, login, key):
    if service == 'hub':
        # 委托请求里签名的身份必须与登记时的登录一致：取自用户登录的 [iss, sub]
        issuer, sub = json.loads(caller.subject)
        expires_at = _hub_register(caller, key, issuer, sub, seconds)
        return {'token_id': key, 'issuer': issuer, 'sub': sub, 'expires_at': expires_at, 'package': package,
                'seconds': seconds, 'granted_at': time.time()}
    value = eval_client.delegate(caller.token, package, seconds, key, caller.refresh if login else '')
    if not isinstance(value, dict) or value.get('delegation') != key or not isinstance(value.get('expires_at'), int):
        raise WorkspaceError('评测服务响应格式错误', 502, 'delegation_invalid_response')
    return {'handle': key, 'expires_at': value['expires_at'], 'package': package,
            'seconds': seconds, 'granted_at': time.time(), 'login': bool(value.get('login'))}


def _renew(service, grant, caller, seconds):
    if service == 'hub':
        expires_at = _hub_register(caller, grant['token_id'], grant['issuer'], grant['sub'], seconds)
        return {**grant, 'expires_at': expires_at, 'granted_at': time.time()}
    value = eval_client.renew_delegation(grant['handle'], caller.token, seconds)
    if not isinstance(value, dict) or not isinstance(value.get('expires_at'), int):
        raise WorkspaceError('评测服务响应格式错误', 502, 'delegation_invalid_response')
    return {**grant, 'expires_at': value['expires_at'], 'granted_at': time.time(), 'login': bool(value.get('login'))}


def _revoke(service, key, token=''):
    """Ask the service to delete a registration; Hub accepts that only on the user's own login."""
    if not key:
        return True  # 早期测试版本的 Hub 登记，对方已没有对应接口
    try:
        if service == 'evaluation':
            eval_client.undelegate(key)
        elif token:
            hub_packages.revoke_delegation(key, token)
        else:
            return False
        return True
    except WorkspaceError:
        _log.warning('flywheel delegation revocation failed for %s', service)
        return False


def _pend(state, username, unrevoked):
    """Revocations the service did not confirm: their keys are kept for retrying the revocation only."""
    for service, grant in unrevoked:
        state['revoking'].append({'service': service, 'key': grant.get(KEYS[service]),
                                  'expires_at': grant['expires_at'], 'at': time.time()})
        when = '后台每 5 分钟重试' if service == 'evaluation' else '打开项目页面时每 5 分钟重试'
        store.audit(state, username, '后台授权', f'{LABELS[service]}暂未确认撤销登记，{when}，直到撤销成功')


def _retry_due(item, now, token):
    """A failed revocation is retried RETRY_SECONDS apart while the delegation lasts; Hub's need the login."""
    if item['expires_at'] <= now or now - item['at'] < RETRY_SECONDS:
        return False
    return bool(token) or item['service'] == 'evaluation'


def retry_revocations(pid, state, token=''):
    """Retry revocations that failed, until the service confirms or the delegation has run out there anyway.

    Without the user's login (background rounds) only the evaluation service's are retried."""
    pending = state['revoking']
    if not pending or not state['identity']:
        return
    now = time.time()
    due = [item for item in pending if _retry_due(item, now, token)]
    if not due and all(item['expires_at'] > now for item in pending):
        return
    done = {item['key'] for item in due if _revoke(item['service'], item['key'], token)}
    tried = {item['key'] for item in due}
    identity = state['identity']
    with store.update(pid, identity['subject']) as state:
        kept = []
        for item in state['revoking']:
            if item['key'] in done:
                # 未确认的登记是这里自己记下待清理的，不是用户要撤销的：不写审计
                if not item.get('unconfirmed'):
                    store.audit(state, identity['username'], '后台授权',
                                f"已撤销{LABELS[item['service']]}登记（此前撤销未完成）")
            elif item['expires_at'] > now:
                kept.append({**item, 'at': now} if item['key'] in tried else item)
        state['revoking'] = kept


def _day(seconds):
    return time.strftime('%Y-%m-%d', time.localtime(seconds))


def align(caller):
    """Make the registrations match the switches; needs the user's login, so only runs in their requests."""
    if not caller.present:
        return
    state = store.load(caller.pid, caller.subject)
    # 撤销只需要用户本人的登录：失去编辑权限的用户回来时也补上后台撤销不了的 Hub 登记
    retry_revocations(caller.pid, state, caller.token)
    if not caller.can_write:
        return
    linked = hub_packages.association(caller.pid) or {}
    package = linked.get('ontology_id')
    identity = {'subject': caller.subject, 'username': caller.username}
    # 后台不代其执行的用户（如不是项目成员的管理员）不登记，已有的登记当作关掉开关一样撤销
    acted_for = acts_for(caller.manager, caller.manager.get_project(caller.pid), caller.username)
    needed = wanted(state) if acted_for else []
    seconds = lifetime(state)
    due = _due(state, needed, package, caller.refresh)
    stale = [service for service in state['delegation'] if _stale(state, service, needed, due)]
    # 关掉开关就清掉它的失败记录：用户再打开时立即重新登记，不受重试间隔限制
    settled = [service for service in state['delegation_issues'] if service not in needed]
    matched = not stale and not settled and not due
    if matched and state['identity'] == identity:
        return
    granted, renewed, failed, refused = {}, {}, {}, set()
    unresolved = _unresolved(caller, due)
    keys = {service: _new_key(service) for service in _registering(due) if service not in unresolved}
    _hold(caller, keys, seconds)
    for service, action in due.items():
        try:
            if action == 'renew':
                renewed[service] = _renew(service, state['delegation'][service], caller, seconds)
            elif service in unresolved:
                raise WorkspaceError('上次登记的结果还不确定，撤销后再重新登记', 409, 'delegation_unresolved')
            else:
                granted[service] = _register(service, caller, package, seconds,
                                             service == 'evaluation' and login_wanted(state), keys[service])
        except WorkspaceError as exc:
            # 旧版本服务没有登记接口时返回 404，原文会被译成“记录不存在”，所以写明是登记这一步失败
            failed[service] = {'message': f'{LABELS[service]}登记失败（{exc}）'[:store.TEXT_LIMIT], 'at': time.time()}
            if service in keys and _refused(exc):
                refused.add(keys[service])
    with store.update(caller.pid, caller.subject) as state:
        state['identity'] = identity
        replaced = [(service, state['delegation'][service]) for service in stale + list(granted)
                    if service in state['delegation']]
        for service in stale:
            grant = state['delegation'].pop(service, None)
            if grant is None:
                continue  # 后台刚撤回了它（withdraw）
            state['delegation_issues'].pop(service, None)
            store.audit(state, caller.username, '后台授权',
                        f"已销毁{LABELS[service]}登记" + ('及其保存的后台评测登录' if grant.get('login') else ''))
        for service in settled:
            state['delegation_issues'].pop(service, None)
        for service, grant in renewed.items():
            key = KEYS[service]
            if state['delegation'].get(service, {}).get(key) == grant[key]:
                state['delegation'][service] = grant
        # 已确认的登记不再待撤销；没确认的（失败、答复丢失）留着，之后撤销
        # 明确被拒的登记对方并没有记下，也不再待撤销
        finished = refused | {grant[KEYS[service]] for service, grant in granted.items()}
        state['revoking'] = [item for item in state['revoking'] if item['key'] not in finished]
        for service, grant in granted.items():
            previous = state['delegation'].get(service) or {}
            state['delegation'][service] = grant
            state['delegation_issues'].pop(service, None)
            if not previous:
                until = f"有效期至 {_day(grant['expires_at'])}" if state['settings']['delegation_days'] else '长期有效'
                store.audit(state, caller.username, '后台授权', f'已在{LABELS[service]}登记，{until}')
            if grant.get('login') and not previous.get('login'):
                store.audit(state, caller.username, '后台授权', '评测服务已保存后台评测登录，关闭「自动化确认发布与发起评测」时销毁')
            if previous.get('login') and not grant.get('login'):
                store.audit(state, caller.username, '后台授权', '已销毁评测服务保存的后台评测登录')
        state['delegation_issues'].update(failed)
        for service in failed:
            grant = state['delegation'].get(service, {})
            # 改了时长却没登记成功：不能继续用旧时长的授权，改短时尤其如此
            if grant.get('seconds') not in (None, seconds):
                replaced.append((service, state['delegation'].pop(service)))
            # 关了「自动化确认发布与发起评测」却没能重新登记：旧登记带着评测服务保存的登录，同样不能留
            elif grant.get('login') and not login_wanted(state):
                replaced.append((service, state['delegation'].pop(service)))
                store.audit(state, caller.username, '后台授权',
                            '评测服务重新登记失败：已停用原登记，并请评测服务删除它保存的后台评测登录')
    unrevoked = [(service, grant) for service, grant in replaced
                 if not _revoke(service, grant.get(KEYS[service]), caller.token)]
    if unrevoked:
        with store.update(caller.pid, caller.subject) as state:
            _pend(state, caller.username, unrevoked)


def withdraw(pid, subject, why):
    """Background rounds no longer act for this user (``why``): stop using the delegations and revoke them.

    The evaluation service deletes its registration, and the login it keeps, on the handle alone; Hub needs
    the user's login, so its registration waits in ``revoking`` for the user's next visit or runs out."""
    with store.update(pid, subject) as state:
        withdrawn = list(state['delegation'].items())
        if not withdrawn:
            return
        state['delegation'] = {}
        username = (state['identity'] or {}).get('username', '')
        for service, _ in withdrawn:
            store.audit(state, username, '后台授权', f'{why}：后台不再代你执行，已停用{LABELS[service]}登记')
    unrevoked = [(service, grant) for service, grant in withdrawn if not _revoke(service, grant.get(KEYS[service]))]
    if unrevoked:
        with store.update(pid, subject) as state:
            _pend(state, username, unrevoked)


def reject(state, service, username, key):
    """The service refused the delegation ``key``: stop using it until the user is back to register again.

    A step runs on the registration it started with; one that replaced it meanwhile (the user changed the
    lifetime or the switches) was not refused and stays."""
    grant = state['delegation'].get(service)
    if not grant or grant.get(KEYS[service]) != key:
        return
    state['delegation'].pop(service)
    store.audit(state, username, '后台授权', f'{LABELS[service]}已拒绝登记，下次打开项目页面时重新登记')
    state['delegation_issues'][service] = {'message': f'{LABELS[service]}已拒绝后台授权，打开项目页面后自动重新登记',
                                           'at': 0}


def key_of(caller, service):
    """The registration a background caller acts on, for telling which one a service refused."""
    if service == 'hub':
        return (caller.hub_delegation or {}).get('token_id')
    return caller.token.handle if isinstance(caller.token, Delegated) else None


def summary(state, linked, acted_for=True):
    """What the browser sees: statuses and dates, never handles or registration IDs.

    A delegation names a Hub package, so a project never published has nothing to register yet; it
    registers on the first sync after the first publication. ``acted_for``: see acts_for.
    """
    needed = wanted(state)
    services = {}
    for service in SERVICES:
        issue = state['delegation_issues'].get(service)
        if service not in needed:
            status = 'off'
        elif not acted_for:
            status, issue = 'issue', {'message': NOT_ACTED_FOR}
        elif active(state, service):
            status = 'active'
        elif not linked:
            status = 'unlinked'
        else:
            status = 'issue' if issue else 'pending'
        grant = state['delegation'].get(service) or {}
        services[service] = {'status': status, 'expires_at': grant.get('expires_at'),
                             'message': issue['message'] if issue and status == 'issue' else ''}
    grant = state['delegation'].get('evaluation') or {}
    if not login_wanted(state) or not acted_for:
        login = 'off'  # 后台不代其执行时谈不上后台评测登录：不提示“重新授权”，原因由上面的 issue 说明
    else:
        login = 'active' if grant.get('login') and active(state, 'evaluation') else 'missing'
    background = bool(needed) and all(services[service]['status'] == 'active' for service in needed) and login != 'missing'
    return {**services, 'login': login, 'background': background}
