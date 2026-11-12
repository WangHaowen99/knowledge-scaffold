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
                if not path.parts or path.is_absolute() or '..' in path.parts:
                    raise ValueError()
                if '\\' in info.filename or ':' in info.filename:
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
        manifest = yaml.safe_load(files.get('manifest.yaml', b''))
        if manifest['id'] != next(iter(roots)) or manifest['kind'] != 'ontology-package' or str(manifest['schema_version']) not in ('0.1','0.2'):
            raise ValueError()
        if not isinstance(manifest.get('version'), str):
            raise ValueError()
        return files, manifest
    except (ValueError, KeyError, TypeError, yaml.YAMLError, zipfile.BadZipFile) as exc:
        raise Failure('invalid_package', '知识包结构或压缩内容无效', 422) from exc


RELATION_RULE_VERSION = 1


def validate_relations(raw):
    """Validate original object declarations before mapping can discard or merge them."""
    files, _ = unpack(raw)
    texts = {}
    for name, data in files.items():
        path = PurePosixPath(name)
        is_object = len(path.parts) == 2 and path.parts[0] == 'objects'
        if not is_object or path.suffix != '.md' or path.stem == 'index':
            continue
        try:
            texts[path.stem] = data.decode('utf-8-sig')
        except UnicodeError as exc:
            raise Failure('EVALUATION_PACKAGE_INVALID', f'{name}：无法读取对象文档', 422, 'package_validation') from exc
    _, problems = mapping_rules.package_relations(texts, reject_duplicate_relations=True, strict_fields=True)
    if problems:
        problems = [p.replace('ontology/object/', 'objects/') for p in problems]
        raise Failure('EVALUATION_PACKAGE_INVALID', '对象关系未通过评测前校验：' + '；'.join(problems[:3]),
                      422, 'package_validation', details={'problems': problems[:100], 'total': len(problems)})
    return {'rule_version': RELATION_RULE_VERSION, 'package_digest': digest(raw)}


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


CONFLICTS = 20


def conflicts(resolution):
    """Blocking binding issues of a package: object, kind, the table it names or matched, candidates and the reason.

    Only names from the user's own package and data tables; at most CONFLICTS issues."""
    found=[]
    for issue in resolution.get('issues') or []:
        if not issue.get('blocking'): continue
        found.append({'object':issue.get('object',''),'kind':issue.get('kind',''),
                      'table':issue.get('table') or issue.get('declared') or '',
                      'candidates':[c.get('table') for c in issue.get('candidates') or [] if isinstance(c,dict) and c.get('table')][:3],
                      'message':str(issue.get('message') or '')[:300]})
    return found[:CONFLICTS]


def _valid_primary_key(item):
    """A non-empty list of distinct, non-empty field names."""
    primary_key = item.get('primary_key') if isinstance(item, dict) else None
    if not isinstance(primary_key, list) or not primary_key:
        return False
    named = all(isinstance(field, str) and field for field in primary_key)
    return named and len(set(primary_key)) == len(primary_key)


def _declared_primary_keys(files):
    """Primary keys from the package's own mapping, written by modeling from the workspace documents.

    Packaged object documents replace the key column with 「必填」, so a composite key cannot be
    inferred from them again. Anything unreadable yields no override.
    """
    try:
        objects = json.loads(files[MAPPING])['objects']
        return {key: list(item['primary_key']) for key, item in objects.items() if _valid_primary_key(item)}
    except (KeyError, TypeError, ValueError, AttributeError):
        return {}


def generate_mapping(raw, tables, graph=False):
    validate_relations(raw)
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
            raise Failure('mapping_conflict', f"有 {len(resolution['problems'])} 个对象与数据表不一致，请按下方清单在建模中修改对象的表绑定后重新发布，"
                          '或改用“不使用数据源”评测', 422, 'mapping', details={'issues':conflicts(resolution)})
        mapping=mapping_rules.object_data_mappings(resolution,graph=graph)
        # 主键以发布包 mapping 为准（否则复合主键退化为首个字段，导入时多行合并成一个节点）；
        # 只在主键列都对上本次数据的真实列时采用，其余对象保持原推断
        for key,primary_key in _declared_primary_keys(files).items():
            item=mapping['objects'].get(key)
            if item and set(primary_key)<=set(item['sources'][0]['fields']):
                item['primary_key']=primary_key
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
        path=f"/api/packages/{quote(ref['ontology_id'],safe='')}/releases/{quote(ref['release_id'],safe='')}"
        receipt=self.call('GET',path,token)
        if ref.get('package_digest') and ref['package_digest'] != receipt['package_digest']:
            raise Failure('package_changed','知识包摘要不一致',409,'package_fetch')
        raw=self.http.request('GET',self.config.hub_url,f'{path}/package',token=token,
                              service=self.config.hub_token,limit=MAX_ZIP)
        if digest(raw)!=receipt['package_digest']:
            raise Failure('digest_mismatch','知识包校验失败',409,'package_fetch')
        _,manifest=unpack(raw)
        same_release=receipt['ontology_id']==ref['ontology_id'] and receipt['release_id']==ref['release_id']
        if not same_release or manifest['id']!=receipt['ontology_id'] or manifest['version']!=receipt['version']:
            raise Failure('manifest_mismatch','包版本与回执不一致',409,'package_fetch')
        validate_relations(raw)
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
        path=f'/api/package-publish-requests/{request_id}'
        if reservation.get('status')!='committed':
            package=derive(raw,mapping,reservation['version'])
            self.call('PUT',f'{path}/content',token,headers={'X-Package-Digest':digest(package)},
                      files={'file':(package_filename(base['ontology_id'], unpack(raw)[1]['name'], reservation['version']),package,'application/zip')})
            reservation=self.call('POST',f'{path}/commit',token)
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
