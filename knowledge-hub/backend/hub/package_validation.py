"""Safe schema 0.1 packages and strictly limited mapping-derived releases."""
import hashlib
import json
from pathlib import PurePosixPath
import zipfile

import yaml

from .package_contract import PackageError

MAPPING = 'mappings/object-data-mappings.json'


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if not isinstance(key, str) or key in result:
            raise ValueError('Duplicate or invalid key')
        result[key] = value
    return result


class StrictLoader(yaml.SafeLoader):
    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise ValueError('Aliases are not supported')
        return super().compose_node(parent, index)


def yaml_mapping(loader, node):
    return unique_pairs([(loader.construct_object(k), loader.construct_object(v)) for k, v in node.value])


StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, yaml_mapping)


def parse_yaml(raw):
    # StrictLoader is a SafeLoader that also rejects aliases and duplicate keys.
    loader = StrictLoader(raw)
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def validate_mapping(raw, files):
    value = json.loads(raw, object_pairs_hook=unique_pairs)
    if not isinstance(value, dict) or set(value) - {'schema_version', 'default_provider_alias', 'objects'}:
        raise ValueError('Invalid mapping structure')
    if value.get('schema_version') != '0.1' or not isinstance(value.get('objects'), dict):
        raise ValueError('Unsupported mapping schema')
    for key, item in value['objects'].items():
        if not isinstance(key, str) or f'objects/{key}.md' not in files or key == 'index':
            raise ValueError('Unknown mapping object')
        if not isinstance(item, dict) or set(item) - {'provider_alias', 'resource', 'primary_key', 'fields', 'sources', 'policy'}:
            raise ValueError('Invalid mapping object')
        if not isinstance(item.get('resource'), str) or not item['resource'] or not isinstance(item.get('fields'), dict):
            raise ValueError('Missing mapping resource or fields')
        for field, rule in item['fields'].items():
            if not isinstance(rule, dict) or set(rule) - {'source', 'source_column', 'type', 'required', 'readable'}:
                raise ValueError('Invalid field mapping')
            if not field or not isinstance(rule.get('source'), str) or not rule['source']:
                raise ValueError('Missing field source')
        primary = item.get('primary_key', [])
        if not isinstance(primary, list) or any(not isinstance(k, str) or k not in item['fields'] for k in primary):
            raise ValueError('Invalid primary key')
        sources = item.get('sources', [])
        if not isinstance(sources, list):
            raise ValueError('Invalid mapping sources')
        for source in sources:
            if not isinstance(source, dict) or set(source) - {'role', 'resource', 'fields'}:
                raise ValueError('Invalid mapping source')
            if not isinstance(source.get('resource'), str) or not isinstance(source.get('fields'), dict):
                raise ValueError('Invalid mapping source fields')
            for key, rule in source['fields'].items():
                if key not in item['fields'] or not isinstance(rule, dict) or set(rule) != {'source_column'}:
                    raise ValueError('Invalid source column')
                if not isinstance(rule['source_column'], str):
                    raise ValueError('Invalid source column')
        policy = item.get('policy', {})
        if not isinstance(policy, dict) or set(policy) - {'read_only', 'max_rows', 'require_user_filter'}:
            raise ValueError('Invalid mapping policy')
    # Runtime bindings and connection material are never part of this contract.
    def portable(node):
        if isinstance(node, dict):
            for key, child in node.items():
                if key.lower() in {'password', 'token', 'secret', 'connection_string', 'url', 'host', 'port'}:
                    raise ValueError('Runtime binding is forbidden')
                portable(child)
        elif isinstance(node, list):
            for child in node:
                portable(child)
        elif isinstance(node, str) and ('://' in node or '-----BEGIN ' in node):
            raise ValueError('Runtime binding is forbidden')
    portable(value)


def inspect_package(store, path, ontology_id, version):
    infos = store.inspect_zip(path)
    try:
        roots = {PurePosixPath(i.filename).parts[0] for i in infos}
        if roots != {ontology_id}:
            raise ValueError('Package root mismatch')
        files = {}
        with zipfile.ZipFile(path) as archive:
            for info in infos:
                if info.is_dir():
                    continue
                if info.file_size > max(info.compress_size, 1) * 1000:
                    raise ValueError('Excessive compression ratio')
                relative = info.filename[len(ontology_id) + 1:]
                with archive.open(info) as stream:
                    files[relative] = hashlib.file_digest(stream, 'sha256').hexdigest()
            manifest_path = f'{ontology_id}/manifest.yaml'
            if archive.getinfo(manifest_path).file_size > 65536:
                raise ValueError('Oversized manifest')
            manifest = parse_yaml(archive.read(manifest_path))
            identity = {'id': ontology_id, 'version': version, 'kind': 'ontology-package'}
            if (not isinstance(manifest, dict) or any(manifest.get(k) != v for k, v in identity.items())
                    or manifest.get('schema_version') not in ('0.1', '0.2')):
                raise ValueError('Unsupported manifest or identity mismatch')
            if manifest['schema_version'] == '0.2' and manifest.get('solver_layout') != 'operation-centric.v1':
                raise ValueError('Unsupported solver layout')
            if any(not isinstance(manifest.get(k), str) for k in ('name', 'description')):
                raise ValueError('Missing manifest fields')
            if not {'objects/index.md', 'operations/index.md'} <= files.keys():
                raise ValueError('Missing package indexes')
            # Schema 0.1 indexes are Markdown. Reject references to absent local files.
            import re
            for name in files:
                if name.endswith('/index.md'):
                    if archive.getinfo(ontology_id + '/' + name).file_size > 1024**2:
                        raise ValueError('Oversized index')
                    text = archive.read(ontology_id + '/' + name).decode('utf-8')
                    for target in re.findall(r'\]\(([^)]+)\)', text):
                        if '://' in target or target.startswith('#'):
                            continue
                        target = target.split('#')[0]
                        relative = (PurePosixPath(name).parent / target).as_posix()
                        if relative not in files:
                            raise ValueError('Invalid index reference')
            if MAPPING in files:
                if archive.getinfo(ontology_id + '/' + MAPPING).file_size > 4 * 1024**2:
                    raise ValueError('Oversized mapping')
                validate_mapping(archive.read(ontology_id + '/' + MAPPING), files)
        return manifest, files
    except (ValueError, TypeError, KeyError, yaml.YAMLError, RecursionError) as exc:
        raise PackageError('invalid_package', 'Invalid or unsupported package structure') from exc


def validate_derivation(store, new_path, receipt, base_path=None, base_receipt=None):
    manifest, files = inspect_package(store, new_path, receipt['ontology_id'], receipt['version'])
    if receipt['change_type'] != 'mapping_update':
        return
    before, previous = inspect_package(store, base_path, receipt['ontology_id'], base_receipt['version'])
    manifest.pop('version')
    before.pop('version')
    # Current 0.1 export has no derived checksum index. All other manifest fields
    # and Markdown indexes remain unchanged; future schemas need explicit rules.
    def unchanged(values):
        return {k: v for k, v in values.items() if k not in (MAPPING, 'manifest.yaml')}

    if MAPPING not in files or manifest != before or unchanged(files) != unchanged(previous):
        raise PackageError('mapping_scope_violation', 'Mapping publication changes knowledge content', 409)
