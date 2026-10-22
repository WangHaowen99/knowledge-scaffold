"""Hub transport and recoverable Onto publication; never persists user credentials."""
from contextlib import contextmanager
from pathlib import Path
import fcntl
import hashlib
import http.client
import json
import os
import shutil
import tempfile
import urllib.parse
import uuid
import zipfile

from .. import config
from . import package_mapping
from .workspace import WorkspaceError, WorkspaceManager, META_FILE


def configured():
    return bool(os.environ.get('ONTO_HUB_URL', '').strip())


def state_dir():
    path = Path(config.DATA_DIR) / 'hub-tasks'
    path.mkdir(parents=True, exist_ok=True)
    return path


def identifier(value):
    import re
    if not re.fullmatch(r'[\w][\w-]{0,127}', value):
        raise WorkspaceError('Hub 标识无效')
    return value


@contextmanager
def lock(key):
    with (state_dir() / f'{identifier(key)}.lock').open('a') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def load(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def save(path, value):
    WorkspaceManager._atomic_write(Path(path), json.dumps(value, ensure_ascii=False, sort_keys=True).encode())


def file_digest(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def token_of(request):
    header = request.headers.get('authorization', '')
    return header[7:] if header.lower().startswith('bearer ') else request.query_params.get('token', '')


def call(method, path, token='', body=None, upload=None, headers=None, download=None):
    """Bounded JSON responses; stream ZIP transfers without encoding them as JSON."""
    url = urllib.parse.urlsplit(os.environ.get('ONTO_HUB_URL', '').rstrip('/'))
    if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password:
        raise WorkspaceError('知识包 Hub 未配置', 503, 'hub_unavailable')
    secret = os.environ.get('ONTO_HUB_SERVICE_TOKEN', '')
    if not secret:
        raise WorkspaceError('知识包 Hub 服务身份未配置', 503, 'hub_unavailable')
    conn_type = http.client.HTTPSConnection if url.scheme == 'https' else http.client.HTTPConnection
    conn = conn_type(url.hostname, url.port, timeout=120)
    request_headers = {'X-Hub-Service-Token': secret, **(headers or {})}
    if token:
        request_headers['Authorization'] = f'Bearer {token}'
    payload = json.dumps(body).encode() if body is not None else b''
    prefix = suffix = b''
    if upload:
        boundary = uuid.uuid4().hex
        prefix = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="archive.zip"\r\n'
                  'Content-Type: application/zip\r\n\r\n').encode()
        suffix = f'\r\n--{boundary}--\r\n'.encode()
        request_headers['Content-Type'] = f'multipart/form-data; boundary={boundary}'
        length = len(prefix) + Path(upload).stat().st_size + len(suffix)
    else:
        request_headers['Content-Type'] = 'application/json'
        length = len(payload)
    request_headers['Content-Length'] = str(length)
    try:
        conn.putrequest(method, urllib.parse.quote(url.path.rstrip('/') + path, safe='/?=&'))
        for key, value in request_headers.items():
            conn.putheader(key, value)
        conn.endheaders()
        if upload:
            conn.send(prefix)
            with Path(upload).open('rb') as f:
                while chunk := f.read(1024 * 1024):
                    conn.send(chunk)
            conn.send(suffix)
        elif payload:
            conn.send(payload)
        response = conn.getresponse()
        if response.status >= 300:
            response.read(65536)
            raise WorkspaceError(f'知识包 Hub 请求失败（{response.status}）', response.status, 'hub_error')
        if download is not None:
            target = Path(download)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('wb') as f:
                total = 0
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > int(os.environ.get('ONTO_HUB_MAX_DOWNLOAD_BYTES', 256 * 1024**2)):
                        target.unlink(missing_ok=True)
                        raise WorkspaceError('Hub 下载超过限制', 413)
                    f.write(chunk)
            expected = response.getheader('X-Package-Digest') or response.getheader('X-Resource-Digest')
            if not expected:
                target.unlink(missing_ok=True)
                raise WorkspaceError('Hub 下载缺少完整性摘要', 502)
            if expected and file_digest(target) != expected:
                target.unlink(missing_ok=True)
                raise WorkspaceError('Hub 下载完整性校验失败', 502)
            return target
        data = response.read(2 * 1024**2 + 1)
        if len(data) > 2 * 1024**2:
            raise WorkspaceError('Hub 响应超过限制', 502)
        return json.loads(data)
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise WorkspaceError('知识包 Hub 不可用，请稍后重试', 503, 'hub_unavailable') from exc
    finally:
        conn.close()


def package_inputs(root):
    """Copy only package source; binding is captured separately as derived metadata."""
    paths = []
    ontology = root / 'ontology'
    if ontology.is_symlink():
        raise WorkspaceError('发布输入不允许符号链接', 400)
    if ontology.exists():
        for p in sorted(ontology.rglob('*')):
            if p.is_symlink():
                raise WorkspaceError('发布输入不允许符号链接', 400)
            if p.is_file():
                paths.append(p)
    return paths


def source_state(manager, pid, allow_unbound, binding=None, graph=False):
    root = manager.project_root(pid)
    resolved = binding if binding is not None else package_mapping.resolve_object_tables(
        root, strict_declared=not allow_unbound)
    if resolved.get('problems'):
        raise WorkspaceError('数据绑定无法发布：' + '；'.join(resolved['problems']), code='binding_problems')
    files = {p.relative_to(root).as_posix(): file_digest(p) for p in package_inputs(root)}
    meta = manager.get_project(pid)
    value = {'files': files, 'binding': resolved, 'name': meta['name'],
             'allow_unbound': allow_unbound, 'graph': graph}
    digest = hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return value, digest


def resources_archive(manager, pid, dest):
    root = manager.project_root(pid)
    if (root / 'resource').is_symlink() or (root / 'resource/materials.json').is_symlink():
        raise WorkspaceError('资料目录和元数据不允许符号链接')
    directives = manager.resource_directives(pid)
    files = []
    for sub in ('diagrams', 'excel', 'others'):
        folder = root / 'resource' / sub
        if folder.is_symlink():
            raise WorkspaceError('资料目录不允许符号链接')
        for p in sorted(folder.rglob('*')):
            if p.is_symlink():
                raise WorkspaceError('资料不允许符号链接')
            if p.is_file():
                files.append(p)
    paths = {p.relative_to(root).as_posix() for p in files}
    if any(rel not in paths for rel in directives):
        raise WorkspaceError('资料用途清单引用了不存在的文件')
    with zipfile.ZipFile(dest, 'w', zipfile.ZIP_DEFLATED) as z:
        for path in files:
            info = zipfile.ZipInfo(path.relative_to(root).as_posix())
            info.compress_type = zipfile.ZIP_DEFLATED
            with path.open('rb') as src, z.open(info, 'w') as out:
                shutil.copyfileobj(src, out, 1024 * 1024)
        z.writestr(zipfile.ZipInfo('resource/materials.json'), json.dumps(directives, sort_keys=True).encode())
    return file_digest(dest)


def sync_resources(manager, pid):
    with lock(pid):
        manager.project_root(pid)
        directory = state_dir() / pid
        directory.mkdir(exist_ok=True)
        task_file = directory / 'resources.json'
        task = load(task_file, {'generation': 0})
        with tempfile.TemporaryDirectory(prefix='onto-resources-') as temp:
            archive = Path(temp) / 'resources.zip'
            current = resources_archive(manager, pid, archive)
            second = Path(temp) / 'check.zip'
            if resources_archive(manager, pid, second) != current:
                raise WorkspaceError('资料发生变化，等待下次同步', 409)
            if current != task.get('digest'):
                task = {'generation': task['generation'] + 1, 'digest': current, 'pending': True}
                save(task_file, task)
            if task.get('pending'):
                call('PUT', f'/api/projects/{pid}/resources/current', upload=archive,
                     headers={'X-Resource-Generation': str(task['generation']), 'X-Resource-Digest': current})
                task['pending'] = resources_archive(manager, pid, second) != current
                save(task_file, task)
        return task


def resource_status(pid, manager=None):
    task = load(state_dir() / identifier(pid) / 'resources.json', {'pending': True, 'generation': 0})
    if manager is not None and not task.get('pending'):
        try:
            with tempfile.TemporaryDirectory(prefix='onto-resource-status-') as temp:
                current = resources_archive(manager, pid, Path(temp) / 'resources.zip')
                task = {**task, 'pending': current != task.get('digest')}
        except (WorkspaceError, OSError):
            task = {**task, 'pending': True}
    return task


def queue_delete(pid):
    identifier(pid)
    save(state_dir() / 'cleanup' / f'{pid}.json', {'project_id': pid})


def retry_tasks(manager):
    from . import hub_packages
    hub_packages.maintain(manager)
    if not configured():
        return
    for path in (state_dir() / 'cleanup').glob('*.json'):
        pid = path.stem
        if load(path, {}).get('completed'):
            continue
        try:
            manager.project_root(pid)
            continue  # The local deletion did not complete; never erase a live project.
        except WorkspaceError as exc:
            if exc.status != 404:
                continue
        try:
            call('DELETE', f'/api/internal/projects/{pid}/artifacts')
            with lock(pid):
                shutil.rmtree(state_dir() / pid, ignore_errors=True)
                # Keep deletion records for recovery and authorization.
                save(path, {'project_id': pid, 'completed': True})
        except WorkspaceError:
            pass
    for meta in manager.list_projects():
        try:
            sync_resources(manager, meta['id'])
        except (WorkspaceError, OSError, ValueError):
            # Persisted tasks are retried; credentials never enter task files.
            continue


async def scheduler_loop(manager):
    import asyncio
    while True:
        try:
            await asyncio.to_thread(retry_tasks, manager)
        except Exception:
            pass
        await asyncio.sleep(30)


def stage_delete(manager, pid):
    with lock(pid):
        queue_delete(pid)
        return manager.stage_delete(pid)


def attempt_resource_sync(manager, pid):
    try:
        sync_resources(manager, pid)
    except (WorkspaceError, OSError, ValueError):
        pass  # The periodic scan retries from the current workspace without a user token.
