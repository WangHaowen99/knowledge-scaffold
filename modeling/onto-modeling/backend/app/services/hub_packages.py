# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""建模项目与知识包 Hub 的对接（ADR-0091）：项目 ↔ Hub 包的固定身份、可恢复的发布请求、从 Hub 导入。

发布按提交（缺省主线最新）导出只读树并冻结到请求目录，版本号由 Hub 分配；Hub 提交成功后在本地
打 release/<版本> 标签、留存一份包（publish 读它显示发布记录、按版本下载）。"""
import hashlib
import json
import logging
import os
import posixpath
import shutil
import time
import uuid
from pathlib import Path

from . import knowledge_hub as transport
from . import ownership
from .workspace import WorkspaceError

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
        if not isinstance(pid, str) or not ownership.visible(meta, username, is_admin):
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


def download(package_id, release_id, token, dest, *, checkpoint=None, timeout=None, deadline=None):
    def remaining_timeout():
        if deadline is None:
            return timeout
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if checkpoint:
                checkpoint()
            raise WorkspaceError('知识包比较处理超时，请缩小知识包后重试', 408, 'compare_timeout')
        return remaining

    def request(path, **kwargs):
        options = dict(kwargs)
        remaining = remaining_timeout()
        if checkpoint is not None:
            options['checkpoint'] = checkpoint
        if remaining is not None:
            options['timeout'] = remaining
        return call('GET', path, token, **options)

    base = reference_path(package_id, release_id)
    receipt = request(base)
    request(f'{base}/package', download=dest)
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


def freeze(manager, pid, folder, allow_unbound, binding=None, graph=False, commit=None):
    """按提交导出只读树，冻结到请求目录的 input/：之后的门禁与渲染只读这份副本，工作区再改也不影响本次发布。"""
    from .publish.service import resolve_commit
    from .publish.tree import release_tree

    root = manager.project_root(pid)
    commit = resolve_commit(root, commit)
    target = folder / 'input'
    shutil.rmtree(target, ignore_errors=True)
    with release_tree(root, commit) as tree:
        shutil.copytree(tree, target)
    value, source_digest = transport.tree_state(manager, pid, target, allow_unbound, binding, graph)
    return dict(value, commit=commit), source_digest


def render(manager, pid, tree, task, version, dest, ontology_id):
    """按冻结的树渲染一份包：包根目录与 manifest id 是 Hub 包身份，版本号是 Hub 分配的。"""
    from .publish.ontology_package import ADAPTER, VARIANT_GRAPH, VARIANT_STANDARD
    from .publish.spi import ReleaseSource

    src = ReleaseSource(
        project_id=pid, commit=task.get('commit', ''), tree=Path(tree), meta={'id': pid, 'name': task['name']},
        version=version, state_root=manager.project_root(pid),
        variant=VARIANT_GRAPH if task.get('graph') else VARIANT_STANDARD,
        allow_unbound=task['allow_unbound'], binding=task['binding'], package_id=ontology_id,
    )
    return ADAPTER.render(src, Path(dest))


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


STRUCTURE_CHECK_VERSION = '0.1.0'


def structure_findings(manager, pid, tree, task, ontology_id):
    """按 consumer 的导入规则检查这棵树导出的包；除占位版本号外，参数与正式导出一致。"""
    import tempfile

    from . import consumer_package_rules

    with tempfile.TemporaryDirectory(prefix='onto-structure-check-') as directory:
        archive = Path(directory) / 'package.zip'
        render(manager, pid, tree, task, STRUCTURE_CHECK_VERSION, archive, ontology_id)
        return consumer_package_rules.check_package_zip(archive)


def package_structure_findings(manager, pid, *, binding, allow_unbound, graph=False, ontology_id=None, tree=None):
    """consumer 导入规则检查（develop 同名接口）：tree 缺省为工作区当前文件，发布检查传提交导出的只读树。"""
    task = {'name': manager.get_project(pid)['name'], 'binding': binding, 'allow_unbound': allow_unbound,
            'graph': graph}
    return structure_findings(manager, pid, tree or manager.project_root(pid), task, ontology_id or pid)


def ensure_importable(manager, pid, folder, value, allow_unbound, graph):
    """consumer 会拒绝导入的冻结快照在 Hub 分配版本号之前拦下。"""
    task = dict(value, allow_unbound=allow_unbound, graph=graph)
    findings = structure_findings(manager, pid, folder / 'input', task, ontology_id(pid))
    if findings:
        detail = '；'.join(item['message'] for item in findings[:STRUCTURE_MESSAGES])
        raise WorkspaceError(f'知识包结构不符合 consumer 导入规则：{detail}', 400, 'package_structure_invalid')


def _record_local_release(manager, pid, receipt, task, archive):
    """Hub 提交后的本地记录：打 release/<版本> 标签（历史页靠它）、留存包、写最近一次发布。

    Hub 是版本权威，本地记录只用于显示与按版本下载；这里失败只记日志，留存缺失时按需从 Hub 再取。
    """
    if task['payload'].get('kind') != 'release' or not task.get('commit'):
        return
    from .publish import service as publish_service

    try:
        publish_service.record_hub_release(manager, pid, receipt, task, archive)
    except (OSError, ValueError, WorkspaceError):
        logger.warning('Failed to record a Hub release locally', exc_info=True)


def publish(manager, pid, request_id, token, subject, allow_unbound=False, *,
            kind="release", binding=None, graph=False, relation_report=None, object_texts=None,
            delegated=None, expected_input_digest=None, expected_base=None, before_external=None,
            acknowledged_by='', verified_revision='', commit=None, author=''):
    """发布一个 Hub 版本。commit 缺省主线最新；relation_report 由发布适配器自己算，这里只为签名兼容保留。"""
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
            _record_local_release(manager, pid, saved_receipt, task, folder / 'package.zip')
            shutil.rmtree(folder / 'input', ignore_errors=True)
            (folder / 'package.zip').unlink(missing_ok=True)
            return saved_receipt
        if not task:
            from .solver import service as solver_service

            value, input_digest = freeze(manager, pid, folder, allow_unbound, binding, graph, commit)
            if expected_input_digest is not None and input_digest != expected_input_digest:
                raise WorkspaceError('建模内容发生变化，请重新预览发布', 409, 'publish_preview_stale')
            if object_texts is not None:
                if transport.package_mapping.read_object_texts(folder / 'input', strict=True) != object_texts:
                    raise WorkspaceError('评测对象在准备快照时发生变化，请重试', 409)
            if kind == 'release':
                solver_service.ensure_publishable(folder / 'input', manager.project_root(pid))
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
                        binding=value['binding'], files=value['files'], graph=graph,
                        commit=value['commit'], author=author)
            if kind == 'release':
                # 冻结时的求解契约修订：之后据此提示当前修改是否已发布并验证
                check = solver_service.solver_check_at(folder / 'input')
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
            _record_local_release(manager, pid, receipt, task, folder / 'package.zip')
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
                for rel, expected in task['files'].items():
                    source = folder / 'input' / rel
                    if not source.is_file() or transport.file_digest(source) != expected:
                        raise WorkspaceError('发布快照不可用', 503)
                temporary = folder / 'package.tmp'
                render(manager, pid, folder / 'input', task, receipt['version'], temporary, payload['ontology_id'])
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
        _record_local_release(manager, pid, receipt, task, archive)
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
            staging = type(manager)(folder / 'stage', manager.assets_dir)
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
