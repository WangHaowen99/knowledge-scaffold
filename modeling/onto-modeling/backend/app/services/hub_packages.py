"""Project-shared Hub modeling adapter. No consumer/workspace integration."""
import hashlib
import json
import logging
import os
import posixpath
import shutil
import uuid
from pathlib import Path

from . import knowledge_hub as transport
from .workspace import META_FILE, WorkspaceError, WorkspaceManager

logger = logging.getLogger(__name__)


def scope(subject):
    return hashlib.sha256(subject.encode()).hexdigest()


def directory(pid, subject):
    return transport.state_dir() / 'packages' / transport.identifier(pid) / scope(subject)


# Fixed protocol namespace; never regenerate per deployment or user.
ONTOLOGY_NAMESPACE = uuid.UUID('787d64cc-2e75-5dab-8e72-56dcfa9029a3')
ASSOCIATION_FIELDS = ('ontology_id', 'release_id', 'package_digest', 'version', 'committed_at')


def ontology_id(pid):
    return str(uuid.uuid5(ONTOLOGY_NAMESPACE, pid))


def association(pid, subject=None):
    return transport.load(transport.state_dir() / 'packages' / transport.identifier(pid) / 'association.json')


LINKED_LOOKUP_PACKAGES = 20
LINKED_PROJECT_NAMES = 5


def linked_projects(manager, subject, username, is_admin, package_ids):
    """Names of the caller's visible projects whose own association points at each Hub package.

    Display-only reverse lookup for the evaluation task list. At most 20 packages with 5 names of
    up to 128 characters each, so the response stays below 20 KiB; ``total`` keeps the full count.
    """
    wanted = list(dict.fromkeys(transport.identifier(value) for value in package_ids))
    if not 1 <= len(wanted) <= LINKED_LOOKUP_PACKAGES:
        raise WorkspaceError(f'每次查询 1 到 {LINKED_LOOKUP_PACKAGES} 个 Hub 包', 422)
    names = {package_id: [] for package_id in wanted}
    for meta in manager._project_metas():
        pid = meta.get('id')
        if not isinstance(pid, str) or not manager.can_access(meta, username, is_admin):
            continue
        try:
            linked = association(pid, subject)
        except (OSError, ValueError, WorkspaceError):
            continue
        if isinstance(linked, dict) and linked.get('ontology_id') in names:
            names[linked['ontology_id']].append(str(meta.get('name') or pid))
    return {'items': [dict(ontology_id=package_id, projects=sorted(found)[:LINKED_PROJECT_NAMES], total=len(found))
                      for package_id, found in names.items()]}


def call(method, path, token, **kwargs):
    if not token:
        raise WorkspaceError('Hub 需要登录用户身份', 401)
    return transport.call(method, path, token, **kwargs)


def delegation_id(token_id):
    return 'mcp-' + transport.identifier(token_id)


def register_delegation(pid, token_id, token, issuer, sub, expires_at=None):
    return call('POST', '/api/package-delegations', token, body={
        'delegation_id': delegation_id(token_id), 'ontology_id': ontology_id(pid),
        'expires_at': expires_at,
    })


def revoke_delegation(token_id, token):
    path = posixpath.join('/api/package-delegations', delegation_id(token_id))
    return call('DELETE', path, token)


def delegated_call(method, path, token_id, ontology, issuer, sub, **kwargs):
    header = transport.delegation_header(method, path, delegation_id(token_id), ontology, issuer, sub)
    headers = {'X-Hub-Delegation': header, **(kwargs.pop('headers', None) or {})}
    return transport.call(method, path, headers=headers, **kwargs)


def check_delegation(token_id, pid, issuer, sub):
    ontology = ontology_id(pid)
    path = posixpath.join('/api/package-delegations', delegation_id(token_id))
    return delegated_call('GET', path, token_id, ontology, issuer, sub)


def reference_path(package_id, release_id=''):
    base = f'/api/packages/{transport.identifier(package_id)}/releases'
    return base + ('/' + transport.identifier(release_id) if release_id else '')


def download(package_id, release_id, token, dest):
    base = reference_path(package_id, release_id)
    receipt = call('GET', base, token)
    call('GET', f'{base}/package', token, download=dest)
    if receipt.get('ontology_id') != package_id or receipt.get('release_id') != release_id or (
            transport.file_digest(dest) != receipt.get('package_digest')):
        Path(dest).unlink(missing_ok=True)
        raise WorkspaceError('Hub 包引用或摘要不一致', 502)
    return receipt


def record(manager, pid, subject, receipt, options=None):
    path = directory(pid, subject).parent / 'association.json'
    previous = transport.load(path, {})
    # A replay of an earlier publication must not rewind a newer base reference.
    if previous.get('committed_at', '') > receipt.get('committed_at', ''):
        return
    manager.project_root(pid)
    value = {k: receipt[k] for k in ('ontology_id', 'release_id', 'package_digest', 'version')}
    value['committed_at'] = receipt.get('committed_at', '')
    transport.save(path, value)


def freeze(manager, pid, folder, allow_unbound, binding=None, graph=False):
    value, source_digest = transport.source_state(manager, pid, allow_unbound, binding, graph)
    snapshot = WorkspaceManager(folder / 'input', manager.assets_dir)
    root = snapshot.workspaces_dir / pid
    root.mkdir(parents=True, exist_ok=True)
    transport.save(root / META_FILE, {'id': pid, 'name': manager.get_project(pid)['name']})
    for rel, expected in value['files'].items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(manager.project_root(pid) / rel, target)
        if transport.file_digest(target) != expected:
            raise WorkspaceError('建模内容发生变化，请重试', 409)
    if transport.source_state(manager, pid, allow_unbound, binding, graph)[1] != source_digest:
        raise WorkspaceError('建模内容发生变化，请重试', 409)
    return value, source_digest


def validate_publish_intent(task, expected_input_digest, expected_base):
    """Only replay a durable request when it represents the confirmed publication intent."""
    payload = (task or {}).get('payload', {})
    if expected_input_digest is not None and payload.get('input_digest') != expected_input_digest:
        raise WorkspaceError('发布请求与当前预览不一致，请重新预览并使用新的 request_id',
                             409, 'publish_preview_stale')
    if expected_base is not None and (payload.get('base_release_id'), payload.get('base_digest')) != expected_base:
        raise WorkspaceError('发布基础版本与当前预览不一致，请重新预览并使用新的 request_id',
                             409, 'publish_preview_stale')


STRUCTURE_MESSAGES = 3
SOLVER_RELEASES = 100


def _solver_releases_path(pid):
    return transport.state_dir() / 'packages' / transport.identifier(pid) / 'release-solver-state.json'


def solver_revisions(pid):
    """release_id → 发布时的求解契约修订，只含启用了求解契约的发布，最多最近 100 个。"""
    value = transport.load(_solver_releases_path(pid), {})
    if not isinstance(value, dict):
        return {}
    return {release_id: entry.get('revision') for release_id, entry in value.items() if isinstance(entry, dict)}


def _record_solver_release(pid, receipt, task):
    """发布成功后记下本版本的求解契约修订与未验证确认人；只用于提示，写入失败不影响发布。"""
    if not task.get('solver_revision'):
        return
    try:
        entries = transport.load(_solver_releases_path(pid), {})
        entries = entries if isinstance(entries, dict) else {}
        entries[receipt['release_id']] = dict(revision=task['solver_revision'], version=receipt.get('version'),
                                              acknowledged_by=task.get('acknowledged_by') or '',
                                              published_at=receipt.get('committed_at') or '')
        newest = sorted(entries.items(), key=lambda item: str(item[1].get('published_at') or ''), reverse=True)
        transport.save(_solver_releases_path(pid), dict(newest[:SOLVER_RELEASES]))
    except (OSError, ValueError, KeyError, WorkspaceError):
        logger.warning('Failed to record the solver revision of a release', exc_info=True)


def ensure_importable(manager, pid, folder, value, allow_unbound, graph):
    """Reject a frozen release that consumer would refuse to import, before Hub reserves a version.

    The snapshot is exported with the same arguments as the release archive, except the version.
    """
    snapshot = WorkspaceManager(folder / 'input', manager.assets_dir)
    findings = snapshot.package_structure_findings(
        pid, binding=value['binding'], allow_unbound=allow_unbound, graph=graph, ontology_id=ontology_id(pid))
    if findings:
        detail = '；'.join(item['message'] for item in findings[:STRUCTURE_MESSAGES])
        raise WorkspaceError(f'知识包结构不符合 consumer 导入规则：{detail}', 400, 'package_structure_invalid')


def publish(manager, pid, request_id, token, subject, allow_unbound=False, *,
            kind="release", binding=None, graph=False, relation_report=None, object_texts=None,
            delegated=None, expected_input_digest=None, expected_base=None, before_external=None,
            acknowledged_by='', verified_revision=''):
    transport.identifier(request_id)
    if not token and not delegated:
        raise WorkspaceError('Hub 需要登录用户身份', 401)
    with transport.lock(pid):
        manager.project_root(pid)
        folder = directory(pid, subject) / 'requests' / request_id
        task_file = folder / 'task.json'
        task = transport.load(task_file)
        if task and (task['allow_unbound'] != allow_unbound or task['payload']['kind'] != kind):
            raise WorkspaceError('发布请求参数发生变化', 409)
        saved_receipt = transport.load(folder / 'receipt.json')
        if saved_receipt:
            validate_publish_intent(task, expected_input_digest, expected_base)
            try:
                record(manager, pid, subject, saved_receipt)
            except (OSError, WorkspaceError):
                return {**saved_receipt, 'local_record_pending': True}
            shutil.rmtree(folder / 'input', ignore_errors=True)
            (folder / 'package.zip').unlink(missing_ok=True)
            return saved_receipt
        if not task:
            if kind == 'release':
                manager._ensure_package_publish_stages(pid)
                manager._ensure_solver_publishable(pid)
            shutil.rmtree(folder / 'input', ignore_errors=True)
            value, input_digest = freeze(manager, pid, folder, allow_unbound, binding, graph)
            if expected_input_digest is not None and input_digest != expected_input_digest:
                raise WorkspaceError('建模内容发生变化，请重新预览发布', 409, 'publish_preview_stale')
            if object_texts is not None:
                frozen = WorkspaceManager(folder / 'input', manager.assets_dir)
                if transport.package_mapping.read_object_texts(frozen.project_root(pid), strict=True) != object_texts:
                    raise WorkspaceError('评测对象在准备快照时发生变化，请重试', 409)
            if kind == 'release':
                manager._ensure_solver_publishable(pid)
                ensure_importable(manager, pid, folder, value, allow_unbound, graph)
            base = association(pid, subject)
            if expected_base is not None:
                actual = ((base or {}).get('release_id'), (base or {}).get('package_digest'))
                if actual != expected_base:
                    raise WorkspaceError('Hub 基础版本发生变化，请重新预览发布', 409, 'publish_preview_stale')
            wire_id = hashlib.sha256(json.dumps([pid, subject, request_id]).encode()).hexdigest()
            payload = dict(request_id=wire_id, ontology_id=ontology_id(pid),
                           change_type='modeling_content', kind=kind, input_digest=input_digest)
            if base:
                payload.update(base_release_id=base['release_id'],
                               base_digest=base['package_digest'])
            task = dict(name=value['name'], allow_unbound=allow_unbound, payload=payload,
                        binding=value['binding'], files=value['files'], graph=graph)
            if kind == 'release':
                # 冻结时的求解契约修订：之后据此提示当前修改是否已发布并验证
                check = manager.solver_check(pid)
                if check.needs_validation:
                    # 每个发布入口（网页、MCP）都须确认求解能力尚未验证，或依据已通过求解验证的同一修订免确认；
                    # 冻结时修订与免确认依据不一致（期间改了契约）也回到发布检查重新确认
                    if not acknowledged_by and check.revision != verified_revision:
                        raise WorkspaceError('求解能力尚未经端到端验证：请在发布检查中确认后再发布，'
                                             '发布后在「已发布版本（知识包评测）」中发起求解验证', 409,
                                             'solver_validation_acknowledgement_required')
                    task.update(solver_revision=check.revision, acknowledged_by=acknowledged_by)
            transport.save(task_file, task)
        payload = task['payload']
        validate_publish_intent(task, expected_input_digest, expected_base)
        if delegated:
            def hub_call(method, path, **kwargs):
                return delegated_call(method, path, delegated['token_id'], payload['ontology_id'],
                                      delegated['issuer'], delegated['sub'], **kwargs)
        else:
            def hub_call(method, path, **kwargs):
                return call(method, path, token, **kwargs)
        base_path = posixpath.join('/api/package-publish-requests', payload['request_id'])
        # A commit response may be lost after Hub has made the version durable. Reconcile the
        # original request before comparing the now-advanced local base; only a committed result
        # is authoritative enough to bypass the stale-preview guard.
        if before_external:
            before_external()
        try:
            receipt = hub_call('GET', base_path)
        except WorkspaceError as exc:
            if exc.status != 404:
                raise
            receipt = None
        if receipt and receipt.get('status') == 'committed':
            transport.save(folder / 'receipt.json', receipt)
            try:
                record(manager, pid, subject, receipt)
            except (OSError, WorkspaceError):
                return {**receipt, 'local_record_pending': True}
            shutil.rmtree(folder / 'input', ignore_errors=True)
            (folder / 'package.zip').unlink(missing_ok=True)
            return receipt
        if expected_base is not None:
            base = association(pid, subject)
            if ((base or {}).get('release_id'), (base or {}).get('package_digest')) != expected_base:
                raise WorkspaceError('Hub 基础版本发生变化，请重新预览发布', 409, 'publish_preview_stale')
        if before_external:
            before_external()
        receipt = hub_call('POST', '/api/package-publish-requests', body=payload)
        archive = folder / 'package.zip'
        if receipt['status'] == 'pending':
            if not archive.exists():
                snapshot = WorkspaceManager(folder / 'input', manager.assets_dir)
                for rel, expected in task['files'].items():
                    source = snapshot.project_root(pid) / rel
                    if not source.is_file() or transport.file_digest(source) != expected:
                        raise WorkspaceError('发布快照不可用', 503)
                temporary = folder / 'package.tmp'
                snapshot.export_package_zip(pid, temporary, binding=task['binding'], allow_unbound=allow_unbound,
                                            version=receipt['version'], ontology_id=payload['ontology_id'],
                                            graph=task['graph'], relation_report=relation_report)
                with temporary.open('rb') as handle:
                    os.fsync(handle.fileno())
                os.replace(temporary, archive)
            if before_external:
                before_external()
            hub_call('PUT', posixpath.join(base_path, 'content'), upload=archive,
                     headers={'X-Package-Digest': transport.file_digest(archive)},
                     upload_name=transport.package_filename(payload['ontology_id'], task['name'], receipt['version']))
        if receipt['status'] != 'committed':
            if before_external:
                before_external()
            receipt = hub_call('POST', posixpath.join(base_path, 'commit'))
        transport.save(folder / 'receipt.json', receipt)
        try:
            record(manager, pid, subject, receipt)
        except (OSError, WorkspaceError):
            return {**receipt, 'local_record_pending': True}
        _record_solver_release(pid, receipt, task)
        shutil.rmtree(folder / 'input', ignore_errors=True)
        archive.unlink(missing_ok=True)
        return receipt


def releases(pid, subject, token, offset=0, limit=20):
    base = association(pid, subject)
    if not base:
        return {'releases': [], 'total': 0}
    value = call('GET', reference_path(base['ontology_id']) + f'?offset={offset}&limit={limit}', token)
    return {**value, 'releases': value['items']}


def import_package(manager, body, token, subject, username):
    """Stage a complete imported project before exposing it; retry the same intent."""
    key = hashlib.sha256(json.dumps([subject, body['request_id']]).encode()).hexdigest()
    folder = transport.state_dir() / 'package-imports' / key
    with transport.lock('package-import-' + key):
        folder.mkdir(parents=True, exist_ok=True)
        task_path = folder / 'task.json'
        task = transport.load(task_path)
        if task and task['input'] != body:
            raise WorkspaceError('导入请求参数发生变化', 409)
        path = reference_path(body['ontology_id'], body['release_id'])
        # Recheck Hub access even when a prior request completed locally.
        receipt = call('GET', path, token)
        if receipt['package_digest'] != body['package_digest']:
            raise WorkspaceError('Hub 包摘要发生变化', 409)
        if task and task.get('project_id'):
            pid = task['project_id']
            staged = folder / 'stage' / 'workspaces' / pid
            target = manager.workspaces_dir / pid
            if not target.exists() and staged.exists():
                os.replace(staged, target)
            meta = manager.get_project(pid)
            shutil.rmtree(folder / 'stage', ignore_errors=True)
            return meta
        transport.save(task_path, {'input': body})
        archive = folder / 'package.zip'
        try:
            download(body['ontology_id'], body['release_id'], token, archive)
            shutil.rmtree(folder / 'stage', ignore_errors=True)
            staging = WorkspaceManager(folder / 'stage', manager.assets_dir)
            with manager.name_claim(body['name'], (username, False), importing=True):
                meta = staging.import_package_zip(body['name'], archive, owner=username)
                transport.save(task_path, {'input': body, 'project_id': meta['id']})
                os.replace(staging.project_root(meta['id']), manager.workspaces_dir / meta['id'])
            shutil.rmtree(folder / 'stage', ignore_errors=True)
            return meta
        finally:
            archive.unlink(missing_ok=True)


def maintain(manager):
    """Recover local associations and expire private input caches without tokens."""
    import time
    ttl = int(os.environ.get('ONTO_HUB_PENDING_TTL_SECONDS', 7 * 86400))
    root = transport.state_dir() / 'packages'
    for project in root.glob('*'):
        with transport.lock(project.name):
            try:
                manager.project_root(project.name)
            except WorkspaceError as exc:
                if exc.status == 404:
                    shutil.rmtree(project)
                continue
            for user_scope in project.iterdir():
                if not user_scope.is_dir():
                    continue
                association_path = project / 'association.json'
                current = transport.load(association_path, {})
                for receipt_path in (user_scope / 'requests').glob('*/receipt.json'):
                    receipt = transport.load(receipt_path)
                    if receipt['committed_at'] >= current.get('committed_at', ''):
                        current = {k: receipt[k] for k in ASSOCIATION_FIELDS}
                        transport.save(association_path, current)
                for task_path in user_scope.glob('*/*/task.json'):
                    if (task_path.parent / 'receipt.json').exists() or time.time() - task_path.stat().st_mtime > ttl:
                        shutil.rmtree(task_path.parent / 'input', ignore_errors=True)
                        for name in ('package.zip', 'source.zip', 'package.tmp'):
                            (task_path.parent / name).unlink(missing_ok=True)
