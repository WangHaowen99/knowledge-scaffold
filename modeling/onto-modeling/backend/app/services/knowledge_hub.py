"""Hub transport and recoverable Onto publication; never persists user credentials."""
from contextlib import contextmanager
from pathlib import Path
import fcntl
import hashlib
import hmac
import http.client
import json
import logging
import os
import shutil
import tempfile
import urllib.parse
import uuid
import zipfile

from .. import config
from . import package_mapping
from .workspace import WorkspaceError, WorkspaceManager, META_FILE

_log = logging.getLogger(__name__)


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


def call(method, path, token='', body=None, upload=None, headers=None, download=None, upload_name=None):
    """Bounded JSON responses; stream ZIP transfers without encoding them as JSON."""
    url = urllib.parse.urlsplit(os.environ.get('ONTO_HUB_URL', '').rstrip('/'))
    if url.scheme not in ('http', 'https') or not url.hostname or any((url.username, url.password)):
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
        filename = (upload_name or Path(upload).name).replace('"', '_').replace("\r", "_").replace("\n", "_")
        prefix = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
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
                for chunk in iter(lambda: f.read(1024 * 1024), b''):
                    conn.send(chunk)
            conn.send(suffix)
        elif payload:
            conn.send(payload)
        response = conn.getresponse()
        if response.status >= 300:
            raw_error = response.read(65536)
            try:
                code = json.loads(raw_error).get('code', 'hub_error')
            except (ValueError, AttributeError):
                code = 'hub_error'
            messages = {'version_conflict': '包版本冲突：相同版本对应不同内容',
                        'package_deleted': 'Hub 知识包已删除',
                        'not_found': 'Hub 包不存在或当前用户无权访问',
                        'last_authorized_user': '不能撤销最后一名授权用户',
                        'delegation_revoked': '知识包发布授权已失效',
                        'delegation_target_mismatch': '知识包发布授权与目标不匹配',
                        'invalid_delegation': '知识包发布授权签名无效或已过期',
                        'delegation_replayed': '知识包发布授权请求已被使用'}
            raise WorkspaceError(messages.get(code, f'知识包 Hub 请求失败（{response.status}）'),
                                 response.status, code if code in messages else 'hub_error')
        if download is not None:
            target = Path(download)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('wb') as f:
                total = 0
                for chunk in iter(lambda: response.read(1024 * 1024), b''):
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


def delegation_header(method, path, delegation_id, ontology_id, issuer, sub):
    """Five-minute, request-bound user delegation; generated anew for every Hub request."""
    import base64
    import secrets
    import time
    secret = os.environ.get('ONTO_HUB_SERVICE_TOKEN', '')
    if not secret:
        raise WorkspaceError('知识包 Hub 服务身份未配置', 503, 'hub_unavailable')
    issued = time.time()
    base_path = urllib.parse.urlsplit(os.environ.get('ONTO_HUB_URL', '')).path.rstrip('/')
    payload = dict(v=1, method=method, path=base_path + path, delegation_id=identifier(delegation_id),
                   ontology_id=identifier(ontology_id), issuer=issuer, sub=sub,
                   nonce=secrets.token_urlsafe(24), iat=issued, exp=issued + 300)
    encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(',', ':'), ensure_ascii=False).encode()).decode().rstrip('=')
    signature = hmac.new(secret.encode(), b'hub-delegation-v1.' + encoded.encode(), hashlib.sha256).hexdigest()
    return encoded + '.' + signature


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


def resources_archive(manager, pid, dest, selected):
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
    available = {p.relative_to(root).as_posix(): p for p in files}
    if not isinstance(selected, list) or any(not isinstance(rel, str) or rel not in available for rel in selected):
        raise WorkspaceError('请选择存在的共享资料')
    files = [available[rel] for rel in sorted(set(selected))]
    directives = {rel: value for rel, value in directives.items() if rel in selected}
    with zipfile.ZipFile(dest, 'w', zipfile.ZIP_DEFLATED) as z:
        for path in files:
            info = zipfile.ZipInfo(path.relative_to(root).as_posix())
            info.compress_type = zipfile.ZIP_DEFLATED
            with path.open('rb') as src, z.open(info, 'w') as out:
                shutil.copyfileobj(src, out, 1024 * 1024)
        z.writestr(zipfile.ZipInfo('resource/materials.json'), json.dumps(directives, sort_keys=True).encode())
    return file_digest(dest)


def sync_resources(manager, pid, token, selected):
    from .hub_packages import association
    if not token:
        raise WorkspaceError('共享资料需要登录身份', 401)
    with lock(pid):
        linked = association(pid)
        if not linked:
            raise WorkspaceError('请先发布知识包', 409)
        path = f"/api/packages/{linked['ontology_id']}/resources/current"
        current = call('GET', path, token)
        directory = state_dir() / pid
        directory.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='onto-resources-') as temp:
            archive = Path(temp) / 'resources.zip'
            digest = resources_archive(manager, pid, archive, selected)
            receipt = call('PUT', path, token, upload=archive,
                           headers={'X-Resource-Generation': str(current['generation'] + 1),
                                    'X-Resource-Digest': digest})
        save(directory / 'resources.json', dict(receipt, selected=selected, pending=False))
        return receipt


def resource_status(pid, manager=None):
    return load(state_dir() / identifier(pid) / 'resources.json',
                {'pending': False, 'generation': 0, 'selected': [], 'shared': False})


def retry_tasks(manager):
    from . import hub_packages
    from .onto_mcp import package_publish
    hub_packages.maintain(manager)
    package_publish.maintain(manager)


async def scheduler_loop(manager):
    import asyncio
    while True:
        try:
            await asyncio.to_thread(retry_tasks, manager)
        except Exception:
            # The next round retries; one failed round must not stop the scheduler.
            _log.warning('knowledge hub maintenance round failed', exc_info=True)
        await asyncio.sleep(30)


def stage_delete(manager, pid):
    with lock(pid):
        return manager.stage_delete(pid)


def package_filename(ontology_id, name, version):
    import re
    safe = re.sub(r'[^\w. -]', '_', name).strip(' .')[:80].strip(' .') or 'knowledge_package'
    while len(safe.encode('utf-8')) > 120:
        safe = safe[:-1]
    return f'{ontology_id.replace("-", "")[:12]}-{safe}-{version}.zip'
