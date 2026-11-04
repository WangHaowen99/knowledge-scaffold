"""Real Hub2 storage and diff validation, with synthetic identity/network adapters."""
import io
from pathlib import Path
import sys

import pytest
from cryptography.fernet import Fernet

from evaluation.core import Config, digest
from evaluation.packages import Packages, generate_mapping, unpack
from evaluation.store import Store as EvaluationStore
from test_evaluation import synthetic_package

# Test-only import of the public Hub implementation, never a runtime dependency.
HUB_BACKEND=Path(__file__).resolve().parents[2]/'knowledge-hub/backend'
sys.path.append(str(HUB_BACKEND))
from hub.storage import Store
from hub.package_store import PackageStore
from hub.user_auth import Identity
from hub.package_contract import PublishBody


def test_real_hub_accepts_snapshot_and_release_with_exact_mapping_scope(tmp_path):
    hub=PackageStore(Store(tmp_path/'hub'))
    owner='synthetic-owner'
    actor=Identity(owner,'consumer')
    raw=synthetic_package()
    payload=dict(request_id='base_demo',ontology_id='knowledge_package_demo',change_type='external_import',input_digest=digest(raw),version='0.1.1',kind='release')
    reserved,_=hub.reserve(PublishBody(**payload).model_dump(),actor)
    hub.upload('base_demo',actor,io.BytesIO(raw),digest(raw))
    base=hub.commit('base_demo',actor)
    evaluation=Identity(owner,'evaluation')

    class HubHTTP:
        def request(self,method,url,path,**kwargs):
            pid,rid=path.split('/')[3],path.split('/')[5]
            return hub.release(pid,rid,owner)[1].read_bytes()
        def json(self,method,url,path,**kwargs):
            if path.startswith('/api/packages/'):
                parts=path.split('/')
                return hub.release(parts[3],parts[5],owner)[0]
            if path=='/api/package-publish-requests':
                return hub.reserve(PublishBody(**kwargs['json']).model_dump(),evaluation)[0]
            key=path.split('/')[3]
            if path.endswith('/content'):
                return hub.upload(key,evaluation,io.BytesIO(kwargs['files']['file'][1]),kwargs['headers']['X-Package-Digest'])
            if path.endswith('/commit'): return hub.commit(key,evaluation)
            raise AssertionError(path)

    config=Config(tmp_path/'evaluation',hub_url='https://hub.example.com',hub_token='synthetic-service',encryption_key=Fernet.generate_key().decode())
    store=EvaluationStore(config.data_dir,config.encryption_key)
    client=Packages(config,HubHTTP(),store)
    mapping,_=generate_mapping(raw,{'items':['id','value']})
    prep={'base':base,'mapping':mapping}
    snapshot=client.publish(owner,prep,'synthetic-user','snapshot_demo','evaluation_snapshot')
    release=client.publish(owner,prep,'synthetic-user','publish_demo','release')
    assert snapshot['version']!=release['version']
    assert hub.listing(base['ontology_id'],owner)['total']==2
    assert hub.listing(base['ontology_id'],owner,'evaluation_snapshot')['total']==1
    assert hub.release(base['ontology_id'],base['release_id'],owner)[1].read_bytes()==raw
    assert client.publish(owner,prep,'synthetic-user','publish_demo','release')==release
    assert 'project_id' not in str(release)
    store.db.close()
