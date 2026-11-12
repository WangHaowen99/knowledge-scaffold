"""Incremental sharing; callers hold the Hub project lock."""
from urllib.parse import quote
from . import auth, hub_packages, knowledge_hub as transport
from .workspace import WorkspaceError


def state_path(pid):
    return transport.state_dir() / 'packages' / transport.identifier(pid) / 'members.json'


def status(pid):
    return transport.load(state_path(pid), {'initialized': False, 'pending': {}, 'results': {}})


def deletion(pid):
    return transport.load(state_path(pid).with_name('deletion.json'), {})


def writable(pid):
    if deletion(pid):
        raise WorkspaceError('Hub 知识包删除处理中或已删除，请完成项目删除', 409)


def queue(pid, before, after, initialize=False):
    value = status(pid)
    pending = value['pending']
    if initialize and not value['initialized']:
        for name in {after.get('owner', '')} | {n for n, r in (after.get('members') or {}).items() if r == 'editor'}:
            if name:
                pending[name] = 'grant'
        value['initialized'] = True
    old, new = before.get('members') or {}, after.get('members') or {}
    for name in set(old) | set(new):
        if old.get(name) == new.get(name) or name == after.get('owner'):
            continue
        if new.get(name) == 'editor':
            pending[name] = 'grant'
        elif name not in new or old.get(name) == 'editor':
            pending[name] = 'revoke'
    for name, action in pending.items():
        value['results'][name] = {'action': action, 'status': 'pending'}
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


def sync(manager, pid, token, username=None):
    writable(pid)
    linked = hub_packages.association(pid)
    if not linked:
        return {**status(pid), 'status': 'not_published'}
    value = status(pid)
    ontology = linked['ontology_id']
    for name, action in sorted(list(value['pending'].items()), key=lambda item: item[1] != 'grant'):
        if username is not None and name != username:
            continue
        try:
            users = authorized(ontology, token)
            target = next((u for u in users if u['issuer'] == auth.ISSUER and u['sub'] == name), None)
            if action == 'grant' and target is None:
                target = auth.resolve_hub_member(name)
                hub_packages.call('PUT', f'/api/packages/{ontology}/authorized-users', token, body={'users': [target]})
            elif action == 'revoke' and target is not None:
                hub_packages.call('DELETE', f"/api/packages/{ontology}/authorized-users/{quote(target['subject_id'], safe='')}", token)
            value['pending'].pop(name)
            value['results'][name] = {'action': action, 'status': 'synced'}
        except (WorkspaceError, auth.AuthError, OSError, ValueError, KeyError) as exc:
            value['results'][name] = {'action': action, 'status': 'failed', 'error': str(exc) if isinstance(exc, (WorkspaceError, auth.AuthError)) else 'Hub 同步响应或状态保存失败'}
            transport.save(state_path(pid), value)
            if action == 'grant':
                break
        transport.save(state_path(pid), value)
    return {**value, 'status': 'failed' if value['pending'] else 'synced'}


def _after_publish(manager, pid, token, first):
    if first and not status(pid)['initialized']:
        queue(pid, {}, manager.get_project(pid), initialize=True)
        return sync(manager, pid, token)
    value = status(pid)
    return {**value, 'status': 'failed' if value['pending'] else 'synced'}


def after_publish(manager, pid, token, first):
    try:
        return _after_publish(manager, pid, token, first)
    except (OSError, ValueError, WorkspaceError):
        return {'status': 'failed', 'error': '知识包已发布，成员授权状态保存失败，请重试原发布请求'}


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
        return {'published': True, 'ontology_id': ontology, 'state': progress['state'], 'name': progress.get('name', '')}
    try:
        package = hub_packages.call('GET', f'/api/packages/{ontology}', token)
    except WorkspaceError as exc:
        if exc.code != 'package_deleted':
            raise
        return {'published': True, 'ontology_id': ontology, 'state': 'deleting', 'name': ''}
    return {'published': True, 'ontology_id': ontology, 'name': package['name']}


def delete_package(manager, pid, token):
    preview = delete_preview(manager, pid, token)
    if not preview['published']:
        raise WorkspaceError('项目没有已发布知识包', 409)
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
