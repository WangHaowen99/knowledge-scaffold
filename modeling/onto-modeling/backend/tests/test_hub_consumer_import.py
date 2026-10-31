"""Actual Hub ZIP through the existing consumer normalizer, using synthetic content."""
import zipfile
from pathlib import Path

import pytest

consumer_import = pytest.importorskip('ontology.package_import')
from hub.storage import Store, digest
from hub.package_store import PackageStore
from hub.user_auth import Identity
from app.services.workspace import WorkspaceManager


def test_hub_archive_is_accepted_by_consumer(tmp_path):
    manager = WorkspaceManager(tmp_path / 'onto', tmp_path / 'assets')
    project = manager.create_project('project_alpha')
    pid = project['id']
    root = manager.project_root(pid)
    (root / 'ontology/object/Item.md').write_text(
        '# 对象描述\n合成示例。\n\n# 数据来源\n无\n\n# 对象属性清单\n'
        '| 中文名称 | 英文名称 | 描述 | 主键 | 类型 |\n| --- | --- | --- | --- | --- |\n'
        '| 标识 | item_id | 合成字段 | 是 | 字符串 |\n', encoding='utf-8')
    store = PackageStore(Store(tmp_path / 'hub'))
    actor = Identity('synthetic-owner', 'modeling')
    receipt, _ = store.reserve({'request_id': 'request-a', 'input_digest': 'a' * 64,
        'ontology_id': pid, 'change_type': 'modeling_content'}, actor)
    package = manager.export_package_zip(pid, tmp_path / 'package.zip', version=receipt['version'])
    with package.open('rb') as content:
        store.upload('request-a', actor, content, digest(package))
    committed = store.commit('request-a', actor)
    _, archived = store.release(committed['hub_package_id'], committed['release_id'], actor.subject)
    assert archived.read_bytes() == package.read_bytes()
    extracted = tmp_path / 'extracted'
    with zipfile.ZipFile(archived) as archive:
        archive.extractall(extracted)
    info = consumer_import.inspect_importable_package(extracted / pid)
    assert info is not None and info.version == committed['version']
    consumer_import.normalize_imported_package(extracted / pid, tmp_path / 'normalized')
    assert (tmp_path / 'normalized' / 'objects' / 'Item.md').is_file()
