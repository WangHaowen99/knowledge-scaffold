"""Synthetic data adapters, authorization and deployment boundaries."""
import ast
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import zipfile

import pytest
from cryptography.fernet import Fernet
from openpyxl import Workbook
import yaml

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
    'SELECT id FROM items WHERE id = 1 OR SLEEP(1) = 0',
    "SELECT id FROM items WHERE id = 1 AND (name = 'alpha' OR LOAD_FILE('/tmp/synthetic') IS NULL)",
    'SELECT id FROM items WHERE id = 1 OR EXISTS (SELECT 1 FROM other.items)',
    'SELECT id FROM items WHERE id = 1 AND @secret = 1',
])
def test_query_scope_rejects_writes_and_external_schemas(sql):
    with pytest.raises(Failure): select_only(sql)


def test_query_scope_allows_aggregate():
    assert 'LIMIT 1000' in select_only('SELECT SUM(value) FROM items')


@pytest.mark.parametrize('sql', [
    "SELECT id, name FROM items WHERE name LIKE '%alpha%' OR name LIKE '%beta%'",
    "SELECT id FROM items WHERE name LIKE '%alpha%' AND id IS NOT NULL",
    "SELECT id FROM items WHERE (name LIKE '%alpha%' OR name LIKE '%beta%') AND value > 0",
    "SELECT id FROM items WHERE NOT (name = 'alpha' OR name = 'beta') AND LOWER(name) = 'gamma'",
])
def test_query_scope_allows_combined_readonly_predicates(sql):
    from sqlglot import parse_one

    result = select_only(sql)
    original = parse_one(sql, read='mysql')
    checked = parse_one(result, read='mysql')
    assert checked.args.get('where') == original.args.get('where')
    assert 'LIMIT 1000' in result


def test_connection_snapshot_and_graph_mapping(tmp_path):
    config=Config(tmp_path,encryption_key=Fernet.generate_key().decode())
    store=Store(tmp_path,config.encryption_key)
    data=DataSources(config,store)
    source={'mode':'connection','provider':'neo4j','tables':{'items':['id','value']},
            'connection':store.encrypt({'uri':'bolt://localhost:7687','username':'operator_a','password':'synthetic-invalid-password'})}
    frozen,tables=data.freeze('synthetic-owner',source)
    mapping,preview=generate_mapping(synthetic_package(),{k:v['columns'] for k,v in tables.items()},graph=True)
    assert mapping['objects']['item']['resource']=='item'
    result=data.prepare('a'*32,frozen,mapping)
    assert result['provider']=='neo4j'
    assert result['config']['username']=='operator_a'
    assert scene_token(config,'eval_'+'a'*32)!=scene_token(config,'eval_'+'b'*32)
    store.db.close()


def composite_key_package(primary_key):
    # Packaged object documents carry 「必填」 instead of 「主键」; the key lives in the package mapping.
    doc=('# reading\n\n# 数据来源\nreadings\n\n# 字段\n\n'
         '| 字段 key | 中文名 | 类型 | 必填 | 业务说明 | 别名 | 属性规则 |\n| --- | --- | --- | --- | --- | --- | --- |\n'
         '| entity_id | 条目 | 字符串 | 是 | 条目编号 | 条目编号 | 编码列 |\n'
         '| period_id | 周期 | 字符串 | 是 | 周期编号 | 周期编号 | 编码列 |\n'
         '| amount | 数值 | 小数 | 否 | 数值 | 数值 | 合成数据 |\n')
    mapping={'schema_version':'0.1','default_provider_alias':'data','objects':{'reading':{'primary_key':primary_key}}}
    files={'manifest.yaml':yaml.safe_dump(dict(id='knowledge_package_demo',name='Synthetic package',description='Synthetic data',kind='ontology-package',schema_version='0.1',version='0.1.1')),
           'objects/index.md':'# Synthetic objects','operations/index.md':'# Synthetic operations','objects/reading.md':doc,
           'index.md':'# Synthetic package','mappings/object-data-mappings.json':json.dumps(mapping)}
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w') as z:
        for key,value in files.items(): z.writestr('knowledge_package_demo/'+key,value)
    return output.getvalue()


@pytest.mark.parametrize('graph',[True,False])
def test_packaged_composite_primary_key_is_kept(graph):
    tables={'readings':['entity_id','period_id','amount']}
    mapping,_=generate_mapping(composite_key_package(['entity_id','period_id']),tables,graph=graph)
    assert mapping['objects']['reading']['primary_key']==['entity_id','period_id']


def test_package_primary_key_outside_data_columns_keeps_inferred_key():
    tables={'readings':['entity_id','period_id','amount']}
    mapping,_=generate_mapping(composite_key_package(['entity_id','unknown_id']),tables,graph=True)
    assert mapping['objects']['reading']['primary_key']==['entity_id']


def test_consumer_evaluation_auth_requires_distinct_service_and_user_dependency(monkeypatch):
    root=Path(__file__).resolve().parents[2]
    module=root/'consumer/backend/api/auth/service_auth.py'
    namespace={}
    exec(compile(module.read_text(),str(module),'exec'),namespace)
    monkeypatch.setenv('EVALUATION_SERVICE_TOKEN','synthetic-evaluation-'+'x'*32)
    request=SimpleNamespace(headers={'X-Evaluation-Service-Token':'synthetic-evaluation-'+'x'*32})
    authorized=namespace.get('evaluation_service_authorized')
    assert authorized(request)
    monkeypatch.setenv('MODELING_SERVICE_TOKEN','synthetic-evaluation-'+'x'*32)
    assert not authorized(request)
    tree=ast.parse((root/'consumer/backend/api/routes/workspaces.py').read_text())
    route=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='ensure_evaluation_workspace_api')
    assert 'Depends(require_authenticated_user)' in ast.unparse(route.args)
    assert 'owner=user.username' in ast.unparse(route)


def test_independent_deploy_compose():
    # Root deploy flag combinations are covered by deploy/tests/test_knowledge_hub_deploy.py.
    root=Path(__file__).resolve().parents[2]
    compose=(root/'eval-service/deploy/docker-compose.eval-service.yml').read_text()
    assert 'container_name: knowledge-evaluation' in compose
    assert 'name: ${CONSUMER_NETWORK:-onto-auth-network}' in compose and 'external: true' in compose
    # Only non-secret stack addresses default here; credentials stay in the runtime configuration.
    assert 'EVAL_HUB_URL: ${EVAL_HUB_URL:-http://knowledge-hub:8200}' in compose
    assert 'TOKEN' not in compose and 'ENCRYPTION_KEY' not in compose
    # Judge settings are declared for discoverability but only reference the runtime configuration.
    for key in ('EVAL_JUDGE_BASE_URL','EVAL_JUDGE_API_KEY','EVAL_JUDGE_MODEL','EVAL_JUDGE_CONFIG_REVISION'):
        assert f'{key}: ${{{key}:-}}' in compose
    assert compose.count('API_KEY')==2
    # Package mirrors default in the Dockerfile; compose passes overrides only when they are set.
    dockerfile=(root/'eval-service/Dockerfile').read_text()
    assert 'ARG NPM_REGISTRY=' in dockerfile and '--registry="${NPM_REGISTRY}"' in dockerfile
    assert 'ARG PIP_INDEX_URL=' in dockerfile and '-i "${PIP_INDEX_URL}"' in dockerfile
    assert '- PIP_INDEX_URL' in compose and '- NPM_REGISTRY' in compose
    assert 'depends_on' not in compose
    # Modeling is referenced only as the optional display lookup for project names.
    assert [line.strip() for line in compose.splitlines() if 'onto-modeling' in line and not line.strip().startswith('#')] \
        == ['EVAL_MODELING_URL: ${EVAL_MODELING_URL:-http://onto-modeling:8100}']


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


def test_graph_import_uses_both_configured_scope_values_and_builds_relations(tmp_path,monkeypatch):
    import neo4j
    from evaluation.graph_import import GraphImports
    from test_graph_replacement import Graph
    graph=Graph()
    monkeypatch.setattr(neo4j.GraphDatabase,'driver',graph.driver)
    config=Config(tmp_path,encryption_key=Fernet.generate_key().decode())
    store=Store(tmp_path,config.encryption_key)
    data=DataSources(config,store)
    tables={'items':{'columns':['id','value'],'rows':[['item_0001',2]]}}
    mapping,_=generate_mapping(synthetic_package(),{'items':['id','value']},graph=True)
    connection={'uri':'bolt://localhost:7687','username':'operator_a','password':'synthetic-invalid-password',
                'workspace_property':'evaluation_scope','workspace_value':'workspace_demo',
                'ontology_property':'knowledge_package_id','ontology_value':'knowledge_package_demo'}
    source={'mode':'connection','provider':'neo4j','import_data':True,'tables_snapshot':store.encrypt(tables),
        'connection':store.encrypt(connection)}
    checked=GraphImports(store).inspect('synthetic-owner',source,'knowledge_package_demo','preparation_demo')
    source['graph_confirmation']=GraphImports(store).authorize('synthetic-owner',
        {'id':'preparation_demo','source':source,'package_ref':{'ontology_id':'knowledge_package_demo'}},checked['id'],True)
    relationships={'item':[{'relation':'related','target':'item','sourceJoinProperty':'id','targetJoinProperty':'id'}]}
    result=data.prepare('a'*32,source,mapping,relationships)
    assert result['config']['workspace_value']=='workspace_demo'
    assert result['config']['ontology_value']=='knowledge_package_demo'
    assert graph.commits==1
    assert any('MERGE (a)-[:`related`]->(b)' in sql for sql,params in graph.calls)
    assert graph.edges['related_new_item_0001']==('new_item_0001','new_item_0001')
    assert graph.nodes['new_item_0001']['knowledge_package_id']=='knowledge_package_demo'
    assert 'other_workspace' in graph.nodes and 'other_ontology' in graph.nodes
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
