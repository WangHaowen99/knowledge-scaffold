"""Immutable Hub input and restricted mapping derivatives."""
import io
import json
from pathlib import Path, PurePosixPath
import stat
import tempfile
import zipfile

import yaml
from .core import Failure, canonical, digest
from . import mapping_rules

MAPPING = 'mappings/object-data-mappings.json'
MAX_ZIP = 256 * 1024 * 1024


def unpack(raw):
    if len(raw) > MAX_ZIP:
        raise Failure('package_limit', '知识包超过限制', 413)
    files = {}
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            if len(archive.infolist()) > 10000:
                raise ValueError()
            roots = set()
            for info in archive.infolist():
                path = PurePosixPath(info.filename)
                if not path.parts or path.is_absolute() or '..' in path.parts or '\\' in info.filename or ':' in info.filename:
                    raise ValueError()
                if str(path) != info.filename.rstrip('/') or stat.S_ISLNK(info.external_attr >> 16):
                    raise ValueError()
                roots.add(path.parts[0])
                total += info.file_size
                if total > 1024*1024*1024 or info.file_size > MAX_ZIP or info.file_size > max(info.compress_size, 1)*1000:
                    raise ValueError()
                if info.is_dir():
                    continue
                name = '/'.join(path.parts[1:])
                if not name or name in files:
                    raise ValueError()
                files[name] = archive.read(info)
            if len(roots) != 1 or len(files.get('manifest.yaml', b'')) > 65536:
                raise ValueError()
        manifest = yaml.safe_load(files['manifest.yaml'])
        if manifest['id'] != next(iter(roots)) or manifest['kind'] != 'ontology-package' or str(manifest['schema_version']) not in ('0.1','0.2'):
            raise ValueError()
        if not isinstance(manifest.get('version'), str):
            raise ValueError()
        return files, manifest
    except (ValueError, KeyError, TypeError, yaml.YAMLError, zipfile.BadZipFile) as exc:
        raise Failure('invalid_package', '知识包结构或压缩内容无效', 422) from exc


def derive(raw, mapping, version):
    files, manifest = unpack(raw)
    manifest['version'] = version
    files['manifest.yaml'] = yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False).encode()
    files[MAPPING] = canonical(mapping).encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in sorted(files.items()):
            info = zipfile.ZipInfo(manifest['id'] + '/' + name, date_time=(1980,1,1,0,0,0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info,value)
    return output.getvalue()


def generate_mapping(raw, tables, graph=False):
    files, _ = unpack(raw)
    with tempfile.TemporaryDirectory(prefix='eval-mapping-') as directory:
        root=Path(directory)
        for name, data in files.items():
            if name.startswith('objects/') and name.endswith('.md'):
                path=root/name
                path.parent.mkdir(parents=True,exist_ok=True)
                path.write_bytes(data)
        resolution=mapping_rules.resolve_object_tables(root, tables=tables)
        if resolution['problems']:
            raise Failure('mapping_conflict', '对象绑定与数据表不一致，请检查映射预览', 422, 'mapping')
        mapping=mapping_rules.object_data_mappings(resolution,graph=graph)
        if not mapping['objects']:
            raise Failure('mapping_empty','没有可绑定的数据对象',422,'mapping')
        if graph and any(item['missing_columns'] for item in resolution['objects'].values() if item['table']):
            raise Failure('mapping_columns','图映射缺少字段',422,'mapping')
        relations, skipped = mapping_rules.graph_relations(mapping_rules.read_object_texts(root), set(mapping['objects'])) if graph else ({}, [])
        return mapping, dict(objects=resolution['objects'], warnings=resolution['warnings'], relations=relations, skipped=skipped)


class Packages:
    def __init__(self, config, transport, store):
        self.config,self.http,self.store=config,transport,store

    def call(self, method, path, token, **kwargs):
        if not self.config.hub_token:
            raise Failure('hub_unconfigured','请配置 evaluation Hub 凭据',503,'configuration')
        return self.http.json(method,self.config.hub_url,path,token=token,service=self.config.hub_token,**kwargs)

    def fetch(self, ref, token):
        from urllib.parse import quote
        path='/api/packages/'+quote(ref['ontology_id'],safe='')+'/releases/'+quote(ref['release_id'],safe='')
        receipt=self.call('GET',path,token)
        if ref.get('package_digest') and ref['package_digest'] != receipt['package_digest']:
            raise Failure('package_changed','知识包摘要不一致',409,'package_fetch')
        raw=self.http.request('GET',self.config.hub_url,path+'/package',token=token,
                              service=self.config.hub_token,limit=MAX_ZIP)
        if digest(raw)!=receipt['package_digest']:
            raise Failure('digest_mismatch','知识包校验失败',409,'package_fetch')
        _,manifest=unpack(raw)
        if (receipt['ontology_id'] != ref['ontology_id'] or receipt['release_id'] != ref['release_id']
                or manifest['id']!=receipt['ontology_id'] or manifest['version']!=receipt['version']):
            raise Failure('manifest_mismatch','包版本与回执不一致',409,'package_fetch')
        return raw, receipt

    def publish(self, owner, preparation, token, request_id, kind):
        raw,base=self.fetch(preparation['base'],token)
        mapping=preparation['mapping']
        if mapping is None:
            raise Failure('mapping_required','没有新的 mapping 可发布',422)
        intent=dict(ontology_id=base['ontology_id'],
                    base_release_id=base['release_id'],base_digest=base['package_digest'],
                    change_type='mapping_update',kind=kind,input_digest=digest([base['package_digest'],mapping]),
                    request_id=request_id)
        task=self.store.create(owner,'publications',dict(status='pending',intent=intent),request_id,identity=intent)
        if task['status']=='committed':
            return task['receipt']
        reservation=self.call('POST','/api/package-publish-requests',token,json=intent)
        path='/api/package-publish-requests/'+request_id
        if reservation.get('status')!='committed':
            package=derive(raw,mapping,reservation['version'])
            self.call('PUT',path+'/content',token,headers={'X-Package-Digest':digest(package)},
                      files={'file':(package_filename(base['ontology_id'], unpack(raw)[1]['name'], reservation['version']),package,'application/zip')})
            reservation=self.call('POST',path+'/commit',token)
        if reservation.get('status')!='committed':
            raise Failure('publish_pending','Hub 尚未确认发布完成',409,'publication')
        self.store.save(owner,task['id'],dict(status='committed',intent=intent,receipt=reservation))
        return reservation

    def snapshot(self, owner, preparation, token):
        """Reuse the committed evaluation snapshot for the same base release and mapping.

        Every snapshot takes a version number from the package's shared sequence, so repeated runs must not
        publish identical ones. Only committed snapshots are reused: an expired Hub reservation cannot be
        retried under its request ID, so a new publication keeps the per-preparation request ID.
        """
        base_digest=preparation['base']['package_digest']
        key=digest([base_digest,preparation['mapping']])
        identity={'base_digest':base_digest,'mapping_digest':key}
        found=self.store.lookup_request(owner,'snapshot-reuse',key,identity)
        if found:
            return found['receipt']
        receipt=self.publish(owner,preparation,token,'snapshot-'+preparation['id'],'evaluation_snapshot')
        self.store.create(owner,'snapshot-reuse',{'receipt':receipt},key,identity=identity)
        return receipt


def package_filename(ontology_id, name, version):
    import re
    safe = re.sub(r'[^\w. -]', '_', name).strip(' .')[:80].strip(' .') or 'knowledge_package'
    while len(safe.encode('utf-8')) > 120:
        safe = safe[:-1]
    return f'{ontology_id.replace("-", "")[:12]}-{safe}-{version}.zip'
