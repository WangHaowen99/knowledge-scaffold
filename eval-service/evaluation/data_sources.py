"""Bounded spreadsheet input and isolated database adapters."""
import csv
import io
import json
import re
import zipfile
from itertools import islice
from .core import Failure, endpoint
from .nl2sql import scene_token

IDENTIFIER=re.compile(r'^[A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]{0,63}$')


def identifier(value):
    if not isinstance(value,str) or not IDENTIFIER.fullmatch(value):
        raise Failure('invalid_identifier','表名和字段名格式无效',422,'data_import')
    return value


def read_tables(raw, name):
    try:
        return _read_tables(raw, name)
    except Failure:
        raise
    except Exception as exc:
        raise Failure('input_format','数据文件无法解析，请检查编码与文件格式',422) from exc


def _read_tables(raw, name):
    if len(raw)>50*1024*1024:
        raise Failure('input_limit','数据文件超过 50 MiB',413)
    if name.lower().endswith('.csv'):
        sheets={name.rsplit('.',1)[0]:list(islice(csv.reader(io.StringIO(raw.decode('utf-8-sig'))),100002))}
    elif name.lower().endswith('.xlsx'):
        from openpyxl import load_workbook
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            if len(archive.infolist())>10000 or sum(i.file_size for i in archive.infolist())>256*1024*1024:
                raise Failure('input_limit','工作簿展开内容超过限制',413)
        workbook=load_workbook(io.BytesIO(raw),read_only=True,data_only=True)
        try:
            sheets={}
            for sheet in workbook:
                if (sheet.max_column or 0)>256 or (sheet.max_row or 0)>100001:
                    raise Failure('table_limit','工作簿行列数超过限制',422)
                sheets[sheet.title]=list(islice(sheet.values,100002))
        finally:
            workbook.close()
    else:
        raise Failure('input_format','请上传 CSV 或 XLSX 文件',422)
    result={}
    for name,rows in sheets.items():
        identifier(name)
        if not rows:
            continue
        headers=[identifier(str(c or '').strip()) for c in rows[0]]
        if len(headers)!=len(set(headers)) or len(headers)>256 or len(rows)>100001:
            raise Failure('table_limit','重复列或表行列数超过限制',422)
        values=[]
        for row in rows[1:]:
            if not any(v is not None and v!='' for v in row):
                continue
            if len(row)!=len(headers):
                raise Failure('row_shape','数据行与表头列数不一致',422)
            values.append([None if v is None else (v.isoformat() if hasattr(v,'isoformat') else v) for v in row])
        result[name]=dict(columns=headers,rows=values)
    if not result:
        raise Failure('empty_data','数据文件为空',422)
    return result


class DataSources:
    def __init__(self, config, store):
        self.config,self.store=config,store

    def freeze(self, owner, definition):
        source=dict(definition)
        tables={}
        total=0
        if len(source.get('input_ids',[]))>20:
            raise Failure('input_limit','每个数据源最多 20 个文件',413)
        for fid in source.get('input_ids',[]):
            record=self.store.get(owner,fid,'inputs')
            total+=record['size']
            if total>50*1024*1024:
                raise Failure('input_limit','合并数据超过 50 MiB',413)
            for name,table in self.store.decrypt(record['content']).items():
                if name in tables:
                    raise Failure('duplicate_table','多个文件含有同名表',422)
                tables[name]=table
        if source['mode']=='dataset' and not tables:
            raise Failure('empty_data','dataset 模式需要上传数据',422)
        if source['mode']=='connection':
            for name,columns in source.get('tables',{}).items():
                identifier(name)
                for col in columns: identifier(col)
                tables.setdefault(name,dict(columns=columns,rows=[]))
            if not tables:
                raise Failure('schema_required','请提供外部连接的表字段定义',422)
        # Runtime input bytes are frozen and encrypted separately from Hub packages.
        source['tables_snapshot']=self.store.encrypt(tables) if tables else None
        return source,tables

    def prepare(self, run_id, source, mapping, relations=None):
        mode=source['mode']
        if mode=='none': return None
        tables=self.store.decrypt(source['tables_snapshot'])
        connection=self.store.decrypt(source['connection']) if source.get('connection') else {}
        if mode=='connection' and not source.get('import_data'):
            return dict(mode='direct_provider',provider=source['provider'],config=connection)
        if source.get('provider')=='neo4j':
            connection={**connection,'workspace_property':connection.get('workspace_property') or 'evaluation_scope','workspace_value':'eval_'+run_id}
            self.graph_import(run_id,connection,tables,mapping,relations or {})
            return dict(mode='direct_provider',provider='neo4j',config=connection)
        if mode=='connection':
            # Never replace arbitrary external SQL tables. Read-only external connections are supported.
            raise Failure('external_import_denied','外部 SQL 连接仅支持查询，请使用 dataset 导入隔离评测库',422,'data_import')
        try:
            config=json.loads(self.config.sql_config)
            endpoint(self.config.nl2sql_url)
        except (ValueError,TypeError) as exc:
            raise Failure('dataset_unconfigured','请配置隔离评测 MySQL 与 NL2SQL',503,'data_import') from exc
        import pymysql
        schema='eval_'+run_id
        with pymysql.connect(**config) as db:
            with db.cursor() as cursor:
                cursor.execute('CREATE DATABASE IF NOT EXISTS `'+schema+'`')
                cursor.execute('USE `'+schema+'`')
                for name,table in tables.items():
                    identifier(name)
                    cursor.execute('DROP TABLE IF EXISTS `'+name+'`')
                    cols=table['columns']
                    declarations=','.join('`'+identifier(c)+'` TEXT' for c in cols)
                    cursor.execute('CREATE TABLE `'+name+'` ('+declarations+')')
                    query='INSERT INTO `'+name+'` VALUES ('+','.join(['%s']*len(cols))+')'
                    for offset in range(0,len(table['rows']),200):
                        cursor.executemany(query,table['rows'][offset:offset+200])
            db.commit()
        return dict(mode='direct_provider',provider='nl2sql_http',config={
            'url':self.config.nl2sql_url,'scene':schema,'token':scene_token(self.config,schema)})

    def graph_import(self, run_id, connection, tables, mapping, relations):
        # Import only after explicit user selection; no graph deletion is performed.
        from neo4j import GraphDatabase
        uri=connection.get('uri','')
        if not connection.get('workspace_property') or not connection.get('workspace_value'):
            raise Failure('graph_scope_required','图导入需要明确 workspace 隔离属性和值',422,'data_import')
        scope_key=identifier(connection['workspace_property'])
        scope_value=connection['workspace_value']
        driver=GraphDatabase.driver(uri,auth=(connection.get('username',''),connection.get('password','')))
        try:
            with driver.session(database=connection.get('database','neo4j')) as session:
                for label,item in mapping['objects'].items():
                    identifier(label)
                    table=tables[item['sources'][0]['resource']]
                    columns=table['columns']
                    fields=item['sources'][0]['fields']
                    rows=[{field:row[columns.index(rule['source_column'])] for field,rule in fields.items()} for row in table['rows']]
                    if any(scope_key in row for row in rows):
                        raise Failure('graph_scope_conflict','图隔离属性不能与业务字段重名',422,'data_import')
                    keys=item['primary_key']
                    if any(any(row.get(k) is None for k in keys) for row in rows):
                        raise Failure('graph_key_missing','图数据缺少主键',422,'data_import')
                    key_expr='`'+scope_key+'`: $scope,'+','.join('`'+identifier(k)+'`: row.`'+k+'`' for k in keys)
                    for offset in range(0,len(rows),200):
                        session.run('UNWIND $rows AS row MERGE (n:`'+label+'` {'+key_expr+'}) SET n += row',rows=rows[offset:offset+200],scope=scope_value).consume()
                for source, edges in relations.items():
                    for edge in edges:
                        target=identifier(edge['target'])
                        label=identifier(edge['relation'])
                        left=edge.get('sourceJoinProperty') or mapping['objects'][source]['primary_key'][0]
                        right=edge.get('targetJoinProperty') or mapping['objects'][target]['primary_key'][0]
                        statement=('MATCH (a:`'+identifier(source)+'`), (b:`'+target+'`) '
                                   'WHERE a.`'+scope_key+'`=$scope AND b.`'+scope_key+'`=$scope '
                                   'AND a.`'+identifier(left)+'`=b.`'+identifier(right)+'` '
                                   'MERGE (a)-[:`'+label+'`]->(b)')
                        session.run(statement,scope=scope_value).consume()
        finally:
            driver.close()

    def cleanup(self, run_id, source):
        if source['mode'] != 'dataset' or source.get('provider') == 'neo4j':
            return
        if not re.fullmatch(r'[a-f0-9]{32}',run_id):
            raise Failure('invalid_run','运行标识无效',422)
        import pymysql
        config=json.loads(self.config.sql_config)
        with pymysql.connect(**config) as db:
            with db.cursor() as cursor:
                cursor.execute('DROP DATABASE IF EXISTS `eval_'+run_id+'`')
            db.commit()
