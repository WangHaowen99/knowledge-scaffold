"""Synthetic data adapters, authorization and deployment boundaries."""
import ast
import io
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from openpyxl import Workbook

from evaluation.core import Config, Failure
from evaluation.data_sources import DataSources, read_tables
from evaluation.nl2sql import select_only, scene_token
from evaluation.store import Store
from evaluation.packages import generate_mapping
from test_evaluation import synthetic_package


def test_xlsx_csv_limits_and_duplicate_columns():
    workbook=Workbook(); sheet=workbook.active; sheet.title='items'
    sheet.append(['id','value']);sheet.append(['item_0001',2])
    output=io.BytesIO(); workbook.save(output)
    assert read_tables(output.getvalue(),'synthetic.xlsx')['items']['rows']==[['item_0001',2]]
    with pytest.raises(Failure): read_tables(b'id,id\nx,y','items.csv')
    with pytest.raises(Failure): read_tables(b'', 'synthetic.exe')


@pytest.mark.parametrize('sql',[
    'DELETE FROM items', 'SELECT * FROM other.items', 'SELECT * FROM `other`.`items`',
    "SELECT load_file('/tmp/synthetic')", 'SELECT sleep(1)', 'SELECT 1; SELECT 2',
    "SELECT value INTO OUTFILE '/tmp/synthetic' FROM items", 'SELECT @secret',
    'SELECT * FROM items FOR UPDATE', 'SELECT * FROM (SELECT * FROM other.items) q',
])
def test_query_scope_rejects_writes_and_external_schemas(sql):
    with pytest.raises(Failure): select_only(sql)


def test_query_scope_allows_aggregate():
    assert 'LIMIT 1000' in select_only('SELECT SUM(value) FROM items')


def test_connection_snapshot_and_graph_mapping(tmp_path):
    config=Config(tmp_path,encryption_key=Fernet.generate_key().decode())
    store=Store(tmp_path,config.encryption_key)
    data=DataSources(config,store)
    source={'mode':'connection','provider':'neo4j','tables':{'items':['id','value']},
            'connection':store.encrypt({'uri':'bolt://localhost:7687','username':'operator_a','password':'synthetic-invalid'})}
    frozen,tables=data.freeze('synthetic-owner',source)
    mapping,preview=generate_mapping(synthetic_package(),{k:v['columns'] for k,v in tables.items()},graph=True)
    assert mapping['objects']['item']['resource']=='item'
    result=data.prepare('a'*32,frozen,mapping)
    assert result['provider']=='neo4j'
    assert result['config']['username']=='operator_a'
    assert scene_token(config,'eval_'+'a'*32)!=scene_token(config,'eval_'+'b'*32)
    store.db.close()


def test_consumer_evaluation_auth_requires_distinct_service_and_user_dependency(monkeypatch):
    root=Path(__file__).resolve().parents[2]
    module=root/'consumer/backend/api/auth/service_auth.py'
    namespace={}
    exec(compile(module.read_text(),str(module),'exec'),namespace)
    monkeypatch.setenv('EVALUATION_SERVICE_TOKEN','synthetic-evaluation-'+'x'*32)
    request=SimpleNamespace(headers={'X-Evaluation-Service-Token':'synthetic-evaluation-'+'x'*32})
    assert namespace['evaluation_service_authorized'](request)
    monkeypatch.setenv('MODELING_SERVICE_TOKEN','synthetic-evaluation-'+'x'*32)
    assert not namespace['evaluation_service_authorized'](request)
    tree=ast.parse((root/'consumer/backend/api/routes/workspaces.py').read_text())
    route=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='ensure_evaluation_workspace_api')
    assert 'Depends(require_authenticated_user)' in ast.unparse(route.args)
    assert 'owner=user.username' in ast.unparse(route)


def test_independent_deploy_dispatch(tmp_path):
    root=Path(__file__).resolve().parents[2]
    script=root/'deploy/deploy.sh'
    result=subprocess.run(['bash',str(script),'--eval-only','--onto-modeling-only'],capture_output=True,text=True)
    assert result.returncode!=0
    assert 'cannot be combined' in result.stderr
    compose=(root/'eval-service/deploy/compose.yml').read_text()
    assert 'depends_on' not in compose
    assert 'onto-modeling' not in compose


def test_prune_preserves_frozen_references_and_backup_restore(tmp_path):
    store=Store(tmp_path/'live',Fernet.generate_key().decode())
    keep=store.create('synthetic-owner','inputs',{'created':0})
    remove=store.create('synthetic-owner','inputs',{'created':0})
    store.create('synthetic-owner','preparations',{'source':{'input_ids':[keep['id']]}})
    assert store.prune_inputs(now=100000)==1
    assert store.get('synthetic-owner',keep['id'])
    with pytest.raises(Failure): store.get('synthetic-owner',remove['id'])
    record=store.create('synthetic-owner','runs',{'status':'running'})
    snapshot=tmp_path/'backup.sqlite'
    store.backup(snapshot)
    store.delete('synthetic-owner',record['id'])
    store.db.close()
    restored=tmp_path/'restored'
    from evaluation.maintenance import main
    original=sys.argv
    sys.argv=['maintenance','restore','--data-dir',str(restored),'--snapshot',str(snapshot),'--deletion-records',str(tmp_path/'live/evaluation.sqlite')]
    try: main()
    finally: sys.argv=original
    restored_store=Store(restored)
    with pytest.raises(Failure): restored_store.get('synthetic-owner',record['id'])
    restored_store.db.close()


def test_graph_import_uses_run_scope_and_builds_relations(tmp_path,monkeypatch):
    import neo4j
    calls=[]
    class Result:
        def consume(self): pass
    class Session:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def run(self,sql,**params): calls.append((sql,params)); return Result()
    class Driver:
        def session(self,**kwargs): return Session()
        def close(self): pass
    monkeypatch.setattr(neo4j.GraphDatabase,'driver',lambda *args,**kwargs:Driver())
    config=Config(tmp_path,encryption_key=Fernet.generate_key().decode())
    store=Store(tmp_path,config.encryption_key)
    data=DataSources(config,store)
    tables={'items':{'columns':['id','value'],'rows':[['item_0001',2]]}}
    mapping,_=generate_mapping(synthetic_package(),{'items':['id','value']},graph=True)
    source={'mode':'connection','provider':'neo4j','import_data':True,'tables_snapshot':store.encrypt(tables),
        'connection':store.encrypt({'uri':'bolt://localhost:7687','username':'operator_a','password':'synthetic-invalid'})}
    relationships={'item':[{'relation':'related','target':'item','sourceJoinProperty':'id','targetJoinProperty':'id'}]}
    result=data.prepare('a'*32,source,mapping,relationships)
    assert result['config']['workspace_value']=='eval_'+'a'*32
    assert len(calls)==2
    assert all(params['scope']=='eval_'+'a'*32 for sql,params in calls)
    assert 'MERGE (a)-[:`related`]->(b)' in calls[1][0]
    store.db.close()


def test_dataset_import_isolated_and_parameterized(tmp_path,monkeypatch):
    import pymysql
    calls=[]
    class Cursor:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def execute(self,sql,*args): calls.append((sql,args))
        def executemany(self,sql,rows): calls.append((sql,rows))
    class Database:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def cursor(self): return Cursor()
        def commit(self): pass
    monkeypatch.setattr(pymysql,'connect',lambda **kwargs:Database())
    config=Config(tmp_path,encryption_key=Fernet.generate_key().decode(),sql_config=json.dumps({'host':'localhost'}),nl2sql_url='https://service.example.net/evaluation/api/nl2sql')
    store=Store(tmp_path,config.encryption_key)
    source={'mode':'dataset','tables_snapshot':store.encrypt({'items':{'columns':['id','value'],'rows':[['item_0001',2]]}})}
    result=DataSources(config,store).prepare('b'*32,source,None)
    assert result['config']['scene']=='eval_'+'b'*32
    assert any('VALUES (%s,%s)' in sql for sql,_ in calls)
    assert all('item_0001' not in sql for sql,_ in calls)
    store.db.close()
