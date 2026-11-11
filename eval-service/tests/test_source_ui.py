"""All connection metadata, identities, credentials and rows here are synthetic."""
import json
import time

from test_evaluation import consumer_data_source, environment, preparation, run, wait


def sql_connection():
    return {'url':'https://service.example.net','scene':'workspace_demo',
            'evaluation_workspace_id':'workspace_demo','token':'synthetic-invalid-token'}


def test_sql_inspection_readonly_metadata_owner_scope_and_secret_roundtrip(environment):
    client,app,deps,cfg=environment
    original=deps.json
    calls=[]
    def dependency(method,base,path,**kwargs):
        if path=='/api/v1/sql':
            calls.append(kwargs['json'])
            return {'success':True,'data':[{'table_name':'items','column_name':'id','data_type':'varchar(64)','is_nullable':'NO'},
                                         {'table_name':'items','column_name':'value','data_type':'int','is_nullable':'YES'}]}
        return original(method,base,path,**kwargs)
    deps.json=dependency
    connection=sql_connection()
    inspected=client.post('/evaluation/api/data-source-inspections',json={'provider':'nl2sql_http','connection':connection})
    assert inspected.status_code==200,inspected.text
    handle=inspected.json()['inspection_id']
    assert calls and calls[0]['sql'].startswith('SELECT TABLE_NAME')
    assert "TABLE_SCHEMA = 'workspace_demo'" in calls[0]['sql']
    assert inspected.json()['tables']=={'items':['id','value']}
    assert 'synthetic-invalid-token' not in inspected.text
    payload={'name':'data_source_demo','mode':'connection','provider':'nl2sql_http','connection':connection,'inspection_id':handle}
    saved=client.post('/evaluation/api/data-sources',json=payload)
    assert saved.status_code==200,saved.text
    record=saved.json()
    read=client.get(f"/evaluation/api/data-sources/{record['id']}")
    assert read.json()['connection_view']['scene']=='workspace_demo'
    assert read.json()['secret_configured'] is True
    assert 'token' not in read.json()['connection_view']
    assert 'synthetic-invalid-token' not in read.text
    without_token={k:v for k,v in connection.items() if k!='token'}
    updated=client.put(f"/evaluation/api/data-sources/{record['id']}",json={**payload,'connection':without_token,'revision':record['revision']})
    assert updated.status_code==200,updated.text
    stale=client.post('/evaluation/api/data-sources',json={**payload,'connection':{**connection,'scene':'other_demo'}})
    assert stale.status_code==409
    client.headers['Authorization']='Bearer synthetic-user-b'
    assert client.post('/evaluation/api/data-sources',json=payload).status_code==404
    assert client.get(f"/evaluation/api/data-sources/{record['id']}").status_code==404


def test_schema_failure_hides_dependency_body_and_duplicate_pages(environment):
    client,app,deps,cfg=environment
    original=deps.json
    def failing_sql(method,base,path,**kw):
        if path=='/api/v1/sql': return {'success':False,'error':'synthetic dependency diagnostic'}
        return original(method,base,path,**kw)
    deps.json=failing_sql
    response=client.post('/evaluation/api/data-source-inspections',json={'provider':'nl2sql_http','connection':sql_connection()})
    assert response.status_code==422
    assert 'synthetic dependency diagnostic' not in response.text
    assert client.post('/evaluation/api/data-source-inspections',json={'provider':'nl2sql_http','connection':{**sql_connection(),'scene':"demo'; DELETE"}}).status_code==422


def test_neo4j_no_import_needs_no_handwritten_schema_and_binds_ontology(environment):
    client,app,deps,cfg=environment
    connection={'uri':'bolt://service.example.net:7687','database':'neo4j','username':'operator_a','password':'synthetic-invalid-password',
                'workspace_property':'evaluation_scope','workspace_value':'workspace_demo','ontology_property':'knowledge_package_id'}
    saved=client.post('/evaluation/api/data-sources',json={'name':'graph_source_demo','mode':'connection','provider':'neo4j','connection':connection,'import_data':False})
    assert saved.status_code==200,saved.text
    response=client.post('/evaluation/api/package-preparations',json={'request_id':'prepare_graph','package_ref':{'ontology_id':'knowledge_package_demo','release_id':'release_demo'},'data_source_id':saved.json()['id']})
    assert response.status_code==200,response.text
    assert response.json()['mapping'] is False
    captured=[]
    original=deps.json
    def dependency(method,base,path,**kwargs):
        if path.endswith('/data-source'):
            captured.append(kwargs['json']); return consumer_data_source(kwargs['json'])
        if path.endswith('/data-source/test'): return {'success':True}
        return original(method,base,path,**kwargs)
    deps.json=dependency
    record=run(client,response.json()); result=wait(client,record['id'])
    assert result['status']=='completed',result
    assert captured[0]['config']['ontology_value']=='knowledge_package_demo'
    assert captured[0]['config']['workspace_value']=='workspace_demo'
    assert result['data_source_name']=='graph_source_demo'
    assert result['eval_set_name']=='Synthetic set'
    assert result['started_at']<=result['finished_at']


def test_preview_upload_and_active_task_creation_guard(environment):
    client,app,deps,cfg=environment
    uploaded=client.post('/evaluation/api/inputs',files={'file':('items.csv',b'id,value\nitem_0001,2\n','text/csv')}).json()
    source=client.post('/evaluation/api/data-sources',json={'name':'Synthetic data','mode':'dataset','input_ids':[uploaded['id']]}).json()
    detail=client.get(f"/evaluation/api/data-sources/{source['id']}").json()
    assert detail['inputs'][0]['name']=='items.csv'
    preview=client.post('/evaluation/api/data-source-previews',json={'data_source_id':source['id'],'package_ref':{'ontology_id':'knowledge_package_demo','release_id':'release_demo'}})
    assert preview.status_code==200,preview.text
    assert preview.json()['objects'][0]['table']=='items'
    assert preview.json()['instances']==1
    assert len(deps.requests)==0  # A preview never publishes or writes graph data.
    prep=preparation(client)
    deps.running=True
    first=run(client,prep)
    cases=client.post('/evaluation/api/eval-sets',json={'name':'Second synthetic set','cases':[{'question':'Synthetic'}]}).json()
    before=client.get('/evaluation/api/runs').json()['total']
    response=client.post('/evaluation/api/runs',json={'request_id':'run_second_demo','preparation_id':prep['id'],'eval_set_id':cases['id']})
    assert response.status_code==409
    assert client.get('/evaluation/api/runs').json()['total']==before
    deps.running=False
    assert wait(client,first['id'])['status']=='completed'


def test_graph_preview_uses_uploaded_rows_and_rejects_missing_primary_keys(environment):
    client,app,deps,cfg=environment
    connection={'uri':'bolt://service.example.net:7687','database':'neo4j','username':'operator_a','password':'synthetic-invalid-password',
                'workspace_property':'evaluation_scope','workspace_value':'workspace_demo','ontology_property':'knowledge_package_id'}
    item=client.post('/evaluation/api/inputs',files={'file':('items.csv',b'id,value\nitem_0001,2\nitem_0001,2\n','text/csv')}).json()
    payload={'name':'graph_source_demo','mode':'connection','provider':'neo4j','connection':connection,'input_ids':[item['id']],'import_data':True}
    source=client.post('/evaluation/api/data-sources',json=payload).json()
    query={'data_source_id':source['id'],'package_ref':{'ontology_id':'knowledge_package_demo','release_id':'release_demo'}}
    preview=client.post('/evaluation/api/data-source-previews',json=query)
    assert preview.status_code==200,preview.text
    assert preview.json()['graph'] is True
    assert preview.json()['instances']==1
    empty=client.post('/evaluation/api/inputs',files={'file':('items.csv',b'id,value\n,2\n','text/csv')}).json()
    changed=client.put(f"/evaluation/api/data-sources/{source['id']}",json={**payload,'input_ids':[empty['id']],'revision':source['revision']})
    assert changed.status_code==200
    assert client.post('/evaluation/api/data-source-previews',json=query).status_code==422
    assert not deps.requests


def test_sql_pagination_detects_repeated_metadata_and_refuses_secret_retarget(environment):
    client,app,deps,cfg=environment
    original=deps.json
    rows=[{'table_name':'items','column_name':f'field_{i}','data_type':'int','is_nullable':'YES'} for i in range(200)]
    def repeating_sql(method,base,path,**kw):
        if path=='/api/v1/sql': return {'success':True,'data':rows}
        return original(method,base,path,**kw)
    deps.json=repeating_sql
    response=client.post('/evaluation/api/data-source-inspections',json={'provider':'nl2sql_http','connection':sql_connection()})
    assert response.status_code==409
    assert response.json()['code']=='schema_changed'
    from evaluation.source_config import SourceConfig
    config=SourceConfig(app.state.store,deps)
    existing={'provider':'nl2sql_http','connection':app.state.store.encrypt(sql_connection())}
    from evaluation.core import Failure
    import pytest
    with pytest.raises(Failure,match='重新填写'):
        config.connection('synthetic-owner','nl2sql_http',{**sql_connection(),'url':'https://other.example.net','token':''},existing)


def test_start_rejects_changed_case_revision_before_creating_task(environment):
    client,app,deps,cfg=environment
    prep=preparation(client)
    cases=client.post('/evaluation/api/eval-sets',json={'name':'Synthetic set','cases':[{'question':'Synthetic question'}]}).json()
    client.put(f"/evaluation/api/eval-sets/{cases['id']}",json={'name':'Changed synthetic set','cases':[{'question':'Changed synthetic question'}],'revision':cases['revision']})
    response=client.post('/evaluation/api/runs',json={'request_id':'run_changed_demo','preparation_id':prep['id'],'eval_set_id':cases['id'],'eval_set_revision':cases['revision']})
    assert response.status_code==409
    assert client.get('/evaluation/api/runs').json()['total']==0


def test_neo4j_blank_workspace_field_defaults_to_consumer_scope_property(environment):
    client,app,deps,cfg=environment
    connection={'uri':'bolt://service.example.net:7687','database':'','username':'operator_a','password':'synthetic-invalid-password',
                'workspace_property':'','workspace_value':'workspace_demo','ontology_property':''}
    saved=client.post('/evaluation/api/data-sources',json={'name':'graph_default_demo','mode':'connection','provider':'neo4j','connection':connection,'import_data':False})
    assert saved.status_code==200,saved.text
    view=client.get(f"/evaluation/api/data-sources/{saved.json()['id']}").json()['connection_view']
    assert view['workspace_property']=='d2o_workspace_id' and view['database']=='neo4j'
    assert 'password' not in view
