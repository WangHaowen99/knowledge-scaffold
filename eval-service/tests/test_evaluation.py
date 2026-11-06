"""All identities, data, package bytes and dependency responses are synthetic."""
import io
import json
import threading
import time
import zipfile

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import pytest
import yaml

from evaluation.api import create_app
from evaluation.core import Config, Failure, digest
from evaluation.packages import derive, generate_mapping, unpack
from evaluation import execution
from evaluation.execution import report
from evaluation.store import Store

# Provider fields consumer accepts for PUT /agentic/ontologies/{id}/data-source; it rejects any other key.
CONSUMER_PROVIDER_FIELDS={
    'nl2sql_http':{'url','token','scene','timeout_seconds','max_retries'},
    'nl2sql_template':{'url','token','scene','timeout_seconds','max_retries'},
    'neo4j':{'uri','database','username','password','timeout_seconds','max_retries','max_result_rows','max_iterations',
             'graph_connect_timeout_seconds','graph_query_timeout_seconds','graph_agent_timeout_seconds',
             'workspace_property','workspace_value','ontology_property','ontology_value'},
}


def consumer_data_source(body):
    """Mirror consumer validation so tests fail when evaluation sends fields consumer would reject."""
    if body.get('mode')!='direct_provider' or set(body['config'])-CONSUMER_PROVIDER_FIELDS[body['provider']]:
        raise Failure('dependency_rejected','Unsupported provider field',503,'dependency',upstream_status=400)
    return {'success':True}


def synthetic_package():
    files={'manifest.yaml':yaml.safe_dump(dict(id='knowledge_package_demo',name='Synthetic package',description='Synthetic data',kind='ontology-package',schema_version='0.1',version='0.1.1')),
           'objects/index.md':'# Synthetic objects', 'operations/index.md':'# Synthetic operations',
           'objects/item.md':'# item\n\n# 数据来源\nitems\n\n# 字段\n\n| 英文名称 | 中文名称 | 类型 | 主键 |\n| --- | --- | --- | --- |\n| id | 标识 | string | 是 |\n| value | 值 | string | 否 |\n',
           'index.md':'# Synthetic package'}
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w') as z:
        for key,value in files.items(): z.writestr('knowledge_package_demo/'+key,value)
    return output.getvalue()


class Dependencies:
    def __init__(self):
        self.raw=synthetic_package()
        self.receipt=dict(release_id='release_demo',ontology_id='knowledge_package_demo',version='0.1.1',kind='release',package_digest=digest(self.raw),status='committed')
        self.releases={'release_demo':(self.raw,self.receipt)}
        self.requests={}
        self.submissions={}
        self.judge_fail=False
        self.auth_available=True
        self.running=False
        self.cancelled=False
        self.fail_submit_response=False
        self.fail_commit_response=False
        self.roles={}
        self.judge_calls=[]
        self.lock=threading.Lock()
        self.workspaces={}
        self.submitted_models=[]
        self.events=''
        self.profiles=[
            {'id':'global:synthetic','scope':'global','name':'Synthetic','model':'synthetic','provider':'openai','enabled':True,'is_default':True},
            {'id':'global:synthetic_alt','scope':'global','name':'Synthetic alternative','model':'synthetic-alt','provider':'openai','enabled':True,'is_default':False},
            {'id':'workspace:private_demo','scope':'workspace','name':'Private','model':'private','provider':'openai','enabled':True,'is_default':False},
        ]

    def request(self,method,base,path,**kwargs):
        if path.startswith('/agentic/runs/'):
            return self.events.encode()
        rid=path.split('/')[-2]
        return self.releases[rid][0]

    def json(self,method,base,path,**kwargs):
        token=kwargs.get('token')
        if path=='/userinfo':
            if not self.auth_available: raise Failure('auth_unavailable','Unavailable',503)
            if token not in ('synthetic-user-a','synthetic-user-b'): raise Failure('unauthorized','Unauthorized',401)
            return {'sub':token,'role':self.roles.get(token,'user')}
        if path=='/token': return {'access_token':'synthetic-user-a','refresh_token':'synthetic-refresh'}
        if path.startswith('/api/packages'):
            if token!='synthetic-user-a': raise Failure('not_found','Not found',404)
            if path.count('/')==3:
                # Package summary: its newest release, which carries the package name.
                return dict(self.receipt,name='Synthetic package')
            if '/releases/' in path:
                if path.split('/')[-1] not in self.releases:
                    raise Failure('dependency_rejected','Not found',404,'dependency',upstream_status=404)
                return self.releases[path.split('/')[-1]][1]
            return {'items':[self.receipt], 'total':1,'next_offset':None}
        if path=='/api/package-publish-requests':
            body=kwargs['json']; key=body['request_id']
            if key not in self.requests:
                self.requests[key]={**body,'release_id':'release_'+str(len(self.requests)+2),'version':'0.1.'+str(len(self.requests)+2),'status':'pending'}
            return self.requests[key]
        if path.startswith('/api/package-publish-requests/'):
            key=path.split('/')[3]; row=self.requests[key]
            if path.endswith('/content'):
                raw=kwargs['files']['file'][1]
                row.update(package_digest=digest(raw),status='uploaded')
                self.releases[row['release_id']]=(raw,row)
            if path.endswith('/commit'):
                row['status']='committed'
                if self.fail_commit_response:
                    self.fail_commit_response=False
                    raise Failure('network','Unknown result',503)
            return row
        if path=='/agentic/internal/evaluation/workspaces': return {'workspace':{'id':'eval-'+kwargs['json']['run_id']}}
        if path=='/agentic/workspaces' and method=='GET':
            return {'workspaces':[{'id':'workspace_personal_demo'}],'preferred_workspace_id':'workspace_personal_demo'}
        if path=='/agentic/workspaces' and method=='POST':
            body=kwargs['json']
            self.workspaces[body['id']]={'id':body['id'],'name':body['name'],'role':'admin','members':[]}
            return self.workspaces[body['id']]
        if path.startswith('/agentic/workspaces/') and method=='GET':
            found=self.workspaces.get(path.rsplit('/',1)[1])
            if found is None: raise Failure('dependency_rejected','Not found',404,'dependency',upstream_status=404)
            return found
        if path=='/agentic/model-profiles': return {'default_profile_id':'global:synthetic','profiles':self.profiles}
        if path.endswith('/import-zip'): return {'success':True}
        if path.endswith('/active-version'): return {'active_version':kwargs['json']['version']}
        if path.endswith('/data-source'): return consumer_data_source(kwargs['json'])
        if path.endswith('/data-source/test'): return {'success':True}
        if path=='/agentic/runs':
            key=kwargs['headers']['Idempotency-Key']
            with self.lock:
                self.submitted_models.append(kwargs['json']['model_profile_id'])
                self.submissions.setdefault(key,{'run_id':'remote_'+str(len(self.submissions)+1)})
                if self.fail_submit_response:
                    self.fail_submit_response=False
                    raise Failure('network','Unknown result',503)
            return self.submissions[key]
        if path.startswith('/agentic/runs/'):
            if path.endswith('/cancel'):
                self.cancelled=True
                return {'status':'cancelling'}
            return {'status':'cancelled' if self.cancelled else 'running' if self.running else 'completed','result':{'content':'2'}}
        if path=='/chat/completions':
            self.judge_calls.append((base,token,kwargs['json']['model']))
            if self.judge_fail and '合成协议检查' not in kwargs['json']['messages'][1]['content']:
                raise Failure('judge_unavailable','Unavailable',503)
            return {'choices':[{'message':{'tool_calls':[{'function':{'name':'submit_verdict','arguments':json.dumps({'verdict':'pass','reason':'Synthetic judgement'})}}]}}]}
        if path.startswith('/agentic/workspaces/') and method=='DELETE': return {'deleted':True}
        raise AssertionError((method,path))


@pytest.fixture
def environment(tmp_path):
    deps=Dependencies()
    cfg=Config(tmp_path,auth_url='https://auth.example.com',issuer='synthetic',hub_url='https://hub.example.com',
        hub_token='synthetic-service',consumer_url='https://consumer.example.com',consumer_token='synthetic-evaluation-service',
        encryption_key=Fernet.generate_key().decode(),judge_url='https://judge.example.com',judge_key='synthetic-judge',judge_model='synthetic',judge_revision='1')
    app=create_app(cfg,deps)
    with TestClient(app) as client:
        client.headers['Authorization']='Bearer synthetic-user-a'
        yield client,app,deps,cfg


def preparation(client,request_id='prepare_demo'):
    response=client.post('/evaluation/api/package-preparations',json={'request_id':request_id,'package_ref':{'ontology_id':'knowledge_package_demo','release_id':'release_demo'}})
    assert response.status_code==200,response.text
    return response.json()


def run(client,prep):
    cases=client.post('/evaluation/api/eval-sets',json={'name':'Synthetic set','cases':[{'question':'Synthetic question','reference':'2'}]}).json()
    response=client.post('/evaluation/api/runs',json={'request_id':'run_demo','preparation_id':prep['id'],'eval_set_id':cases['id']})
    assert response.status_code==202,response.text
    return response.json()


def wait(client,rid,statuses=('completed','interrupted','waiting_auth','cancelled')):
    for _ in range(200):
        response=client.get(f'/evaluation/api/runs/{rid}')
        value=response.json()
        if value['status'] in statuses: return value
        time.sleep(.02)
    raise AssertionError(value)


def test_none_independent_run_permissions_and_no_token_storage(environment):
    client,app,deps,cfg=environment
    prep=preparation(client)
    record=run(client,prep)
    result=wait(client,record['id'])
    assert result['status']=='completed',result
    assert result['summary']['counts']['pass']==1
    assert len(deps.submissions)==1
    client.headers['Authorization']='Bearer synthetic-user-b'
    assert client.get(f"/evaluation/api/runs/{record['id']}").status_code==404
    assert client.get('/evaluation/api/runs').json()['total']==0
    assert client.get('/evaluation/api/packages').status_code==404
    rows=app.state.store.db.execute('SELECT body FROM records').fetchall()
    assert all('synthetic-user-a' not in row[0] and 'synthetic-refresh' not in row[0] for row in rows)


def test_mapping_publish_overwrite_and_hub_retry(environment):
    client,app,deps,cfg=environment
    response=client.post('/evaluation/api/inputs',files={'file':('items.csv',b'id,value\nitem_0001,2\n','text/csv')})
    assert response.status_code==200,response.text
    source=client.post('/evaluation/api/data-sources',json={'name':'Synthetic dataset','mode':'dataset','input_ids':[response.json()['id']]}).json()
    payload={'request_id':'mapped_demo','package_ref':{'ontology_id':'knowledge_package_demo','release_id':'release_demo'},'data_source_id':source['id']}
    deps.fail_commit_response=True
    assert client.post('/evaluation/api/package-preparations',json=payload).status_code==503
    response=client.post('/evaluation/api/package-preparations',json=payload)
    assert response.status_code==200,response.text
    prepared=response.json()
    assert prepared['mapping'] is True
    assert prepared['preview']['objects']['item']['table']=='items'
    assert prepared['package_ref']['kind']=='evaluation_snapshot'
    assert len(deps.requests)==1
    body={'request_id':'publish_demo','preparation_id':prepared['id']}
    published=client.post('/evaluation/api/package-publications',json=body).json()
    assert published['kind']=='release'
    assert client.post('/evaluation/api/package-publications',json=body).json()==published
    assert len(deps.requests)==2
    original,manifest=unpack(deps.raw)
    changed,_=unpack(deps.releases[published['release_id']][0])
    for name,data in original.items():
        if name!='manifest.yaml': assert changed[name]==data
    assert original.get('mappings/object-data-mappings.json') is None


def test_judge_retry_does_not_repeat_question(environment):
    client,app,deps,cfg=environment
    deps.judge_fail=True
    record=run(client,preparation(client))
    assert wait(client,record['id'])['status']=='interrupted'
    assert len(deps.submissions)==1
    deps.judge_fail=False
    client.post(f"/evaluation/api/runs/{record['id']}/resume",json={})
    time.sleep(.05)
    result=wait(client,record['id'])
    assert result['status']=='completed',result
    assert len(deps.submissions)==1


def test_unknown_submission_is_resent_under_the_same_id(environment,monkeypatch):
    client,app,deps,cfg=environment
    monkeypatch.setattr(execution,'remote_delay',lambda attempt,hint=None:0)
    deps.fail_submit_response=True
    record=run(client,preparation(client))
    # The outage is retried in place with the same idempotency key: one question, no interruption.
    assert wait(client,record['id'])['status']=='completed'
    assert len(deps.submissions)==1


def rejecting_imports(deps,code):
    """Consumer answers package imports with 409 and the given error code: every time for a name conflict, only
    the first time for an existing version. Returns the form data of each import and a function restoring deps."""
    original,attempts=deps.json,[]
    def json_call(method,base,path,**kwargs):
        if path.endswith('/import-zip'):
            attempts.append(kwargs.get('data'))
            if code or len(attempts)==1:
                raise Failure('dependency_rejected','依赖服务拒绝请求',409,'dependency',upstream_status=409,upstream_code=code)
        return original(method,base,path,**kwargs)
    deps.json=json_call
    return attempts,lambda: setattr(deps,'json',original)


def test_package_name_conflict_interrupts_with_its_reason(environment):
    client,app,deps,cfg=environment
    attempts,restore=rejecting_imports(deps,'ontology_name_conflict')
    record=run(client,preparation(client))
    result=wait(client,record['id'])
    assert result['status']=='interrupted' and result['error']['code']=='package_name_conflict'
    assert '同名但本体 ID 不同的知识包' in result['error']['message'] and result['error']['retryable'] is False
    # Overwriting cannot resolve a name held by another ontology, so it is not attempted.
    assert attempts==[None] and not deps.submissions
    restore()
    client.post(f"/evaluation/api/runs/{record['id']}/resume",json={})
    time.sleep(.05)
    assert wait(client,record['id'])['status']=='completed'


def test_resume_shows_the_run_as_working_at_once(environment):
    client,app,deps,cfg=environment
    deps.judge_fail=True
    record=run(client,preparation(client))
    assert wait(client,record['id'])['status']=='interrupted'
    deps.judge_fail=False
    resumed=client.post(f"/evaluation/api/runs/{record['id']}/resume",json={}).json()
    # Without waiting for preflight and data checks, the page no longer shows the old interruption.
    assert resumed['status']=='preparing' and resumed['error'] is None
    assert wait(client,record['id'])['status']=='completed'


def rejecting(deps,matches,status):
    original=deps.json
    def json_call(method,base,path,**kwargs):
        if matches(path,kwargs):
            raise Failure('dependency_rejected','依赖服务拒绝请求',status,'dependency',upstream_status=status)
        return original(method,base,path,**kwargs)
    deps.json=json_call


def test_a_rejected_login_waits_for_authorization_with_its_reason(environment):
    client,app,deps,cfg=environment
    rejecting(deps,lambda path,kwargs: path=='/agentic/runs',401)
    result=wait(client,run(client,preparation(client))['id'])
    assert result['status']=='waiting_auth' and result['error']['code']=='login_expired'
    assert result['error']['message']=='登录凭证已过期，请重新登录后点击“授权并继续”'


def test_a_rejected_model_api_key_is_not_an_expired_login(environment):
    client,app,deps,cfg=environment
    rejecting(deps,lambda path,kwargs: path=='/chat/completions' and '合成协议检查' in kwargs['json']['messages'][1]['content'],401)
    result=wait(client,run(client,preparation(client))['id'])
    assert result['status']=='interrupted' and result['error']['code']=='judge_test_authentication'
    assert 'API Key' in result['error']['message']


def test_the_refresh_token_is_handed_to_the_engine_and_never_stored(environment):
    client,app,deps,cfg=environment
    handed=[]
    start=app.state.engine.start
    def recording_start(owner,rid,token,refresh=''):
        handed.append(refresh)
        return start(owner,rid,token,refresh)
    app.state.engine.start=recording_start
    prep=preparation(client)
    cases=client.post('/evaluation/api/eval-sets',json={'name':'Synthetic set','cases':[{'question':'Synthetic question','reference':'2'}]}).json()
    response=client.post('/evaluation/api/runs',json={'request_id':'run_refresh','preparation_id':prep['id'],'eval_set_id':cases['id']},
                         headers={'X-Refresh-Token':'synthetic-refresh-secret'})
    assert response.status_code==202,response.text
    assert wait(client,response.json()['id'])['status']=='completed'
    assert handed==['synthetic-refresh-secret']
    tables=[row[0] for row in app.state.store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    dump=' '.join(str(row) for table in tables for row in app.state.store.db.execute(f'SELECT * FROM {table}'))
    assert 'synthetic-refresh-secret' not in dump


def test_existing_version_is_overwritten_with_the_same_bytes(environment):
    client,app,deps,cfg=environment
    attempts,_=rejecting_imports(deps,'')
    record=run(client,preparation(client))
    assert wait(client,record['id'])['status']=='completed'
    assert attempts==[None,{'action':'overwrite_version'}]


def test_cancel_confirms_remote_terminal(environment):
    client,app,deps,cfg=environment
    deps.running=True
    record=run(client,preparation(client))
    for _ in range(100):
        if deps.submissions: break
        time.sleep(.01)
    result=client.post(f"/evaluation/api/runs/{record['id']}/cancel",json={}).json()
    assert result['status']=='cancelling'
    assert wait(client,record['id'])['status']=='cancelled'
    assert deps.cancelled


def test_store_idempotency_recovery_and_encryption(tmp_path):
    store=Store(tmp_path,Fernet.generate_key().decode())
    row=store.create('owner_a','runs',{'status':'running'},'request_demo')
    assert store.create('owner_a','runs',{'status':'running'},'request_demo')['id']==row['id']
    with pytest.raises(Failure): store.create('owner_a','runs',{'status':'other'},'request_demo')
    store.recover()
    assert store.get('owner_a',row['id'])['status']=='interrupted'
    secret={'password':'synthetic-invalid-password'}
    encrypted=store.encrypt(secret)
    assert 'synthetic-invalid-password' not in encrypted
    assert store.decrypt(encrypted)==secret
    store.delete('owner_a',row['id'])
    with pytest.raises(Failure): store.create('owner_a','runs',{'status':'running'},'request_demo')
    store.db.close()


def test_package_zip_escape_and_mapping_replacement():
    raw=synthetic_package()
    mapping,preview=generate_mapping(raw,{'items':['id','value']})
    derived=derive(raw,mapping,'0.1.2')
    mapping['objects']['item']['policy']['max_rows']=25
    replaced=derive(derived,mapping,'0.1.3')
    files,manifest=unpack(replaced)
    assert json.loads(files['mappings/object-data-mappings.json'])==mapping
    assert manifest['version']=='0.1.3'
    data=io.BytesIO()
    with zipfile.ZipFile(data,'w') as z: z.writestr('../escape','Synthetic')
    with pytest.raises(Failure): unpack(data.getvalue())


def test_manual_override_and_zero_denominator(environment):
    client,app,deps,cfg=environment
    record=run(client,preparation(client)); result=wait(client,record['id'])
    body={'verdict':'fail','reason':'Synthetic manual review','revision':result['revision']}
    response=client.post(f"/evaluation/api/runs/{record['id']}/results/0/override",json=body)
    assert response.status_code==200,response.text
    assert client.get(f"/evaluation/api/runs/{record['id']}/report").json()['counts']['fail']==1
    assert client.post(f"/evaluation/api/runs/{record['id']}/results/0/override",json=body).status_code==409
    assert report({'cases':[],'status':'completed'})['judged_pass_rate'] is None


def test_auth_and_request_validation_do_not_echo_secrets(environment):
    client,app,deps,cfg=environment
    response=client.post('/evaluation/api/runs',json={'token':'synthetic-sensitive-value'})
    assert response.status_code==422
    assert 'synthetic-sensitive-value' not in response.text
    deps.auth_available=False
    assert client.get('/evaluation/api/runs').status_code==503


def test_exact_release_lookup_and_retired_project_inputs(environment):
    client,app,deps,cfg=environment
    assert client.get('/evaluation/api/packages/package_demo/releases/release_demo').json()['release_id']=='release_demo'
    response=client.post('/evaluation/api/package-preparations',json={'request_id':'bad_project','project_id':'project_alpha','package_ref':{'ontology_id':'knowledge_package_demo','release_id':'release_demo'}})
    assert response.status_code==422
    response=client.post('/evaluation/api/data-sources',json={'name':'Synthetic','mode':'connection','provider':'neo4j','connection':{'uri':'bolt://localhost:7687'},'tables':{'items':'not-an-array'}})
    assert response.status_code==422


def test_connection_data_source_failure_never_starts_question(environment,monkeypatch):
    client,app,deps,cfg=environment
    original=deps.json
    def call(method,base,path,**kwargs):
        if path.endswith('/data-source/test'): return {'success':False}
        if path.endswith('/data-source'): return {'success':True}
        return original(method,base,path,**kwargs)
    monkeypatch.setattr(deps,'json',call)
    source=client.post('/evaluation/api/data-sources',json={'name':'Synthetic graph','mode':'connection','provider':'neo4j',
        'connection':{'uri':'bolt://localhost:7687','username':'operator_a','password':'synthetic-invalid-password'},'tables':{'items':['id','value']}}).json()
    p=client.post('/evaluation/api/package-preparations',json={'request_id':'connection_demo','package_ref':{'ontology_id':'knowledge_package_demo','release_id':'release_demo'},'data_source_id':source['id']})
    assert p.status_code==200,p.text
    record=run(client,p.json())
    assert wait(client,record['id'])['status']=='interrupted'
    assert not deps.submissions


def test_definition_type_errors_are_client_errors(environment):
    client,_,_,_=environment
    for cases in (None,3,{},'invalid'):
        assert client.post('/evaluation/api/eval-sets',json={'name':'Synthetic','cases':cases}).status_code==422
    for ids in ([None],[{}],[3]):
        assert client.post('/evaluation/api/data-sources',json={'name':'Synthetic','input_ids':ids}).status_code==422
    for filename,raw in [('items.xlsx',b'not-a-workbook'),('items.csv',b'\xff\xfe')]:
        response=client.post('/evaluation/api/inputs',files={'file':(filename,raw)})
        assert response.status_code==422


def test_run_retry_survives_definition_deletion(environment):
    client,app,_,_=environment
    prep=preparation(client)
    cases=client.post('/evaluation/api/eval-sets',json={'name':'Synthetic','cases':[{'question':'Synthetic question'}]}).json()
    payload={'request_id':'retry_demo','preparation_id':prep['id'],'eval_set_id':cases['id']}
    first=client.post('/evaluation/api/runs',json=payload)
    assert first.status_code==202
    wait(client,first.json()['id'])
    assert client.delete(f"/evaluation/api/eval-sets/{cases['id']}").status_code==200
    repeated=client.post('/evaluation/api/runs',json=payload)
    assert repeated.status_code==202
    assert repeated.json()['id']==first.json()['id']
    assert client.post('/evaluation/api/runs',json={**payload,'eval_set_id':'missing'}).status_code==409


def test_queue_rejection_does_not_retain_authorization(environment):
    _,app,_,_=environment
    engine=app.state.engine
    with engine.guard:
        engine.active.add(('owner_demo','active_demo'))
        try:
            with pytest.raises(Failure) as error:
                engine.start('owner_demo','rejected_demo','synthetic-invalid-token')
            assert error.value.status==409
            assert ('owner_demo','rejected_demo') not in engine.tokens
        finally:
            engine.active.remove(('owner_demo','active_demo'))


def test_health_allows_cross_origin_checks_and_client_config_lists_trusted_origins(environment):
    client,_,_,cfg=environment
    response=client.get('/evaluation/api/health')
    assert response.json()=={'status':'ok','service':'evaluation'}
    assert response.headers['access-control-allow-origin']=='*'
    assert client.get('/evaluation/api/client-config').json()=={'trusted_origins':[]}
    cfg.trusted_origins='https://Modeling.Example.com:443/, https://modeling.example.com:5100,https://bad.example.com/path,ftp://x'
    assert client.get('/evaluation/api/client-config').json()=={
        'trusted_origins':['https://modeling.example.com','https://modeling.example.com:5100']}
    # Only the liveness check is readable from other origins.
    assert 'access-control-allow-origin' not in client.get('/evaluation/api/client-config').headers


def test_root_redirects_to_evaluation_page(environment):
    client,_,_,_=environment
    response=client.get('/',follow_redirects=False)
    assert response.status_code==302 and response.headers['location']=='/evaluation/'


def test_judge_settings_are_admin_only_encrypted_and_used_by_new_runs(environment):
    client,app,deps,cfg=environment
    deps.roles['synthetic-user-b']='admin'
    admin={'Authorization':'Bearer synthetic-user-b'}
    url='/evaluation/api/judge-settings'
    assert client.get(url).json()=={'source':'deployment','model':'synthetic','revision':'1','editable':False,'configured':True,'enabled':True}
    body={'base_url':'https://judge-new.example.com/v1/','model':'synthetic-judge-2','api_key':'synthetic-new-key'}
    assert client.put(url,json=body).status_code==403
    assert client.post(f'{url}/test',json=body).status_code==403
    assert client.delete(url).status_code==403
    # The deployment key is never sent to a different gateway.
    rejected=client.put(url,json={**body,'api_key':''},headers=admin)
    assert rejected.status_code==422 and rejected.json()['code']=='judge_key_required'
    assert client.post(f'{url}/test',json=body,headers=admin).json()=={'ok':True}
    assert deps.judge_calls[-1]==('https://judge-new.example.com/v1','synthetic-new-key','synthetic-judge-2')
    saved=client.put(url,json=body,headers=admin).json()
    assert saved['source']=='settings' and saved['base_url']=='https://judge-new.example.com/v1' and saved['api_key_set'] is True
    assert 'synthetic-new-key' not in json.dumps(saved)
    raw=app.state.store.db.execute('SELECT body FROM settings').fetchone()[0]
    assert 'synthetic-new-key' not in raw
    # Re-saving with an empty key keeps the key and the revision for the same gateway.
    again=client.put(url,json={**body,'api_key':''},headers=admin).json()
    assert again['revision']==saved['revision']
    assert client.get('/evaluation/api/session').json()['judge']=={'model':'synthetic-judge-2','revision':saved['revision'],'source':'settings'}
    deps.judge_calls.clear()
    result=wait(client,run(client,preparation(client))['id'])
    assert result['status']=='completed',result
    assert set(deps.judge_calls)=={('https://judge-new.example.com/v1','synthetic-new-key','synthetic-judge-2')}
    reset=client.delete(url,headers=admin).json()
    assert reset['source']=='deployment' and reset['revision']=='1' and reset['base_url']=='https://judge.example.com'


def test_judge_refuses_a_configuration_changed_after_run_creation(environment):
    _,app,_,_=environment
    with pytest.raises(Failure) as exc:
        app.state.engine.judge.score('Synthetic question','2','2',expected={'model':'synthetic','revision':'0','rule_version':'1'})
    assert exc.value.code=='configuration_changed'
