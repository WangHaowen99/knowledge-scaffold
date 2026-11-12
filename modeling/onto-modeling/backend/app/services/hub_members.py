# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""知识包共享与 Hub 授权的增量同步（develop !590，ADR-0098）；调用方持有 Hub 项目锁。

develop 把「所有者加建模者」同步成 Hub 授权用户；本分支项目只有所有者，共享在知识包这一层管理：
「所有者加知识包的共享用户」同步成 Hub 授权用户，不碰项目权限。Hub 上所有授权用户权限相同，最后一人不能撤。
首次发布时初始化（所有者加已设的共享用户），之后共享用户增删按增量排队，逐个同步，失败的留在 pending 里按用户重试。
同目录的 deletion.json 记删除项目时一并物理删除 Hub 包的进度：删除中或已删除时 Hub 写操作一律 409。
"""
from urllib.parse import quote

from . import auth, hub_packages, knowledge_hub as transport
from .workspace import WorkspaceError

DELETING_TEXT = 'Hub 知识包删除处理中或已删除，请完成项目删除'


def state_path(pid):
    return transport.state_dir() / 'packages' / transport.identifier(pid) / 'members.json'


def status(pid):
    value = transport.load(state_path(pid), {})
    value.setdefault('initialized', False)
    value.setdefault('pending', {})
    value.setdefault('results', {})
    value.setdefault('shared', [])
    return value


def shared_users(pid):
    return list(status(pid)['shared'])


def deletion(pid):
    return transport.load(state_path(pid).with_name('deletion.json'), {})


CODE_DELETING = 'hub_package_deleting'


def writable(pid):
    if deletion(pid):
        raise WorkspaceError(DELETING_TEXT, 409, CODE_DELETING)


def _mark_pending(value):
    for name, action in value['pending'].items():
        value['results'][name] = {'action': action, 'status': 'pending'}


def queue(pid, before, after, owner='', initialize=False):
    """排同步：before / after 是共享用户名单；initialize 时把所有者和名单整体授权一次（只做一次）。"""
    value = status(pid)
    pending = value['pending']
    if initialize and not value['initialized']:
        for name in {owner, *after}:
            if name:
                pending[name] = 'grant'
        value['initialized'] = True
    old, new = set(before), set(after)
    for name in old ^ new:
        if name == owner:
            continue
        pending[name] = 'grant' if name in new else 'revoke'
    _mark_pending(value)
    transport.save(state_path(pid), value)


def set_shared(pid, users, owner=''):
    """改共享用户名单；已发布时按差异排同步。返回（原名单, 新名单）。"""
    value = status(pid)
    before = list(value['shared'])
    after = sorted({name for name in users if name and name != owner})
    value['shared'] = after
    transport.save(state_path(pid), value)
    if hub_packages.association(pid):
        queue(pid, before, after, owner)
    return before, after


def owner_changed(pid, previous, owner):
    """管理员换所有者：给新所有者排一次授权；旧所有者留作共享用户（可以手动移除）。"""
    value = status(pid)
    shared = set(value['shared'])
    shared.discard(owner)
    if previous and previous != owner:
        shared.add(previous)
    value['shared'] = sorted(shared)
    if hub_packages.association(pid) and owner:
        value['pending'][owner] = 'grant'
        _mark_pending(value)
    transport.save(state_path(pid), value)


def authorized(ontology, token):
    users, offset = [], 0
    while True:
        page = hub_packages.call('GET', f'/api/packages/{ontology}/authorized-users?offset={offset}&limit=100', token)
        users.extend(page['items'])
        following = page.get('next_offset')
        if following is None:
            return users
        if following <= offset:
            raise WorkspaceError('Hub 授权分页响应无效', 502)
        offset = following


def _current(users, name):
    """Hub 授权名单里属于本账号服务、用户名为 name 的那一条。"""
    for user in users:
        if user.get('issuer') == auth.ISSUER and user.get('sub') == name:
            return user
    return None


def _apply(ontology, token, name, action):
    target = _current(authorized(ontology, token), name)
    if action == 'grant' and target is None:
        target = auth.resolve_hub_member(name)
        hub_packages.call('PUT', f'/api/packages/{ontology}/authorized-users', token, body={'users': [target]})
    elif action == 'revoke' and target is not None:
        subject = quote(target['subject_id'], safe='')
        hub_packages.call('DELETE', f'/api/packages/{ontology}/authorized-users/{subject}', token)


def _failure_text(exc):
    if isinstance(exc, (WorkspaceError, auth.AuthError)):
        return str(exc)
    return 'Hub 同步响应或状态保存失败'


def overview(pid):
    """读取时的同步状态：未发布、有待同步的（failed）或已同步，与 sync 的返回同一结构。"""
    if not hub_packages.association(pid):
        return {**status(pid), 'status': 'not_published'}
    return _result(status(pid))


def _result(value):
    return {**value, 'status': 'failed' if value['pending'] else 'synced'}


def sync(manager, pid, token, username=None):
    """逐个同步 pending：授权在前；某个授权失败就停下（后面的撤权仍要靠它之后的重试）。username 只重试这一个。"""
    writable(pid)
    linked = hub_packages.association(pid)
    if not linked:
        return {**status(pid), 'status': 'not_published'}
    value = status(pid)
    ontology = linked['ontology_id']
    ordered = sorted(value['pending'].items(), key=lambda item: item[1] != 'grant')
    for name, action in ordered:
        if username is not None and name != username:
            continue
        try:
            _apply(ontology, token, name, action)
            value['pending'].pop(name, None)
            value['results'][name] = {'action': action, 'status': 'synced'}
        except (WorkspaceError, auth.AuthError, OSError, ValueError, KeyError) as exc:
            value['results'][name] = {'action': action, 'status': 'failed', 'error': _failure_text(exc)}
            transport.save(state_path(pid), value)
            if action == 'grant':
                break
        transport.save(state_path(pid), value)
    return _result(value)


def _after_publish(manager, pid, token, first):
    if first and not status(pid)['initialized']:
        owner = str(manager.get_project(pid).get('owner') or '')
        queue(pid, [], shared_users(pid), owner, initialize=True)
        return sync(manager, pid, token)
    return _result(status(pid))


def after_publish(manager, pid, token, first):
    try:
        return _after_publish(manager, pid, token, first)
    except (OSError, ValueError, WorkspaceError):
        return {'status': 'failed', 'error': '知识包已发布，共享用户授权状态保存失败，请重试原发布请求'}


def delete_preview(manager, pid, token):
    manager.project_root(pid)
    linked = hub_packages.association(pid)
    progress = deletion(pid)
    if not linked:
        if any(state_path(pid).parent.glob('*/requests/*/task.json')):
            raise WorkspaceError('发布状态不明确，请先核实发布回执', 409)
        return {'published': False}
    ontology = hub_packages.ontology_id(pid)
    if linked['ontology_id'] != ontology:
        raise WorkspaceError('项目知识包关联不一致，不能删除', 409)
    if progress:
        return {'published': True, 'ontology_id': ontology, 'state': progress.get('state', 'deleting'),
                'name': progress.get('name', '')}
    try:
        package = hub_packages.call('GET', f'/api/packages/{ontology}', token)
    except WorkspaceError as exc:
        if exc.code != 'package_deleted':
            raise
        return {'published': True, 'ontology_id': ontology, 'state': 'deleting', 'name': ''}
    return {'published': True, 'ontology_id': ontology, 'name': package.get('name', '')}


def delete_package(manager, pid, token):
    """删除项目前物理删除它的 Hub 包：先记进度再删，失败保留进度、项目不删，可以重试。"""
    preview = delete_preview(manager, pid, token)
    if not preview['published']:
        raise WorkspaceError('项目没有已发布知识包', 409, 'hub_package_not_published')
    if deletion(pid).get('state') == 'deleted':
        return
    path = state_path(pid).with_name('deletion.json')
    transport.save(path, {**preview, 'state': 'deleting'})
    try:
        result = hub_packages.call('DELETE', f"/api/packages/{preview['ontology_id']}", token)
    except WorkspaceError as exc:
        raise WorkspaceError('Hub 删除失败或结果待确认，项目尚未删除；请重试', exc.status) from exc
    if result.get('state') != 'deleted' or result.get('ontology_id') != preview['ontology_id']:
        raise WorkspaceError('Hub 删除结果待确认，项目尚未删除', 502)
    transport.save(path, {**preview, 'state': 'deleted'})
