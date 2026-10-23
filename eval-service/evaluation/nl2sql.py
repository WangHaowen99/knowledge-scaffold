"""Optional standalone SQL query adapter for service-owned evaluation databases."""
import hashlib
import hmac
import json
import re

from .core import Failure


def scene_token(config, scene):
    if not config.encryption_key:
        raise Failure('encryption_unconfigured','查询服务需要配置密钥',503)
    return hmac.new(config.encryption_key.encode(),('nl2sql:'+scene).encode(),hashlib.sha256).hexdigest()


def select_only(sql):
    """Parse the query, reject cross-schema tables and side-effecting expressions."""
    from sqlglot import parse, exp
    from sqlglot.errors import ParseError
    try:
        statements=parse(sql,read='mysql')
        if len(statements)!=1 or not isinstance(statements[0],exp.Select):
            raise ValueError()
        tree=statements[0]
        if len(sql)>20000 or tree.find(exp.Into) or tree.find(exp.Lock):
            raise ValueError()
        if any(table.db or table.catalog for table in tree.find_all(exp.Table)):
            raise ValueError()
        allowed={'COUNT','SUM','AVG','MIN','MAX','COALESCE','ABS','CAST','DATE','EXTRACT',
                 'ROUND','LOWER','UPPER','LENGTH','SUBSTRING','IF','NULLIF','CONCAT',
                 'YEAR','MONTH','DAY','DATEDIFF','DATE_ADD','DATE_SUB'}
        if any(function.sql_name() not in allowed for function in tree.find_all(exp.Func)):
            raise ValueError()
        if tree.find(exp.Parameter) or tree.find(exp.SessionParameter):
            raise ValueError()
        return tree.limit(1000).sql(dialect='mysql')
    except (ValueError,ParseError) as exc:
        raise Failure('unsafe_query','只允许当前评测库的受限只读查询',422,'data_query') from exc


class QueryService:
    def __init__(self,config,http): self.config,self.http=config,http

    def query(self,body,token,direct=False):
        scene=body.get('scene','')
        if not re.fullmatch(r'eval_[a-f0-9]{32}',scene) or not hmac.compare_digest(token,scene_token(self.config,scene)):
            raise Failure('query_forbidden','数据查询权限不足',403,'data_query')
        import pymysql
        try:
            config=json.loads(self.config.sql_config)
            with pymysql.connect(**{**config,'database':scene,'read_timeout':30,'write_timeout':30}) as connection:
                with connection.cursor() as cursor:
                    cursor.execute('SHOW TABLES')
                    tables=[row[0] for row in cursor.fetchall()]
                    schema=[]
                    for table in tables:
                        cursor.execute('SHOW COLUMNS FROM `'+table.replace('`','``')+'`')
                        schema.append(dict(table=table,columns=[row[0] for row in cursor.fetchall()]))
                    sql=body.get('sql','') if direct else self.generate(body.get('question',''),schema)
                    sql=select_only(sql)
                    # Disallow identifiers naming any schema outside this run, even inside subqueries.
                    tokens=re.findall(r'`([^`]+)`|\b([A-Za-z_][A-Za-z0-9_]*)\s*\.',sql)
                    if any((a or b).lower() in ('mysql','sys','information_schema','performance_schema') or (a or b).startswith('eval_') for a,b in tokens):
                        raise Failure('unsafe_query','不允许跨库标识符',422,'data_query')
                    cursor.execute('SET SESSION MAX_EXECUTION_TIME=30000')
                    cursor.execute('START TRANSACTION READ ONLY')
                    cursor.execute(sql)
                    columns=[col[0] for col in cursor.description]
                    rows=cursor.fetchmany(1000)
                    return {'success':True,'sql':sql,'row_count':len(rows),'data':[dict(zip(columns,row)) for row in rows]}
        except Failure: raise
        except Exception as exc:
            raise Failure('data_query_failed','评测数据库查询失败',503,'data_query') from exc

    def generate(self,question,schema):
        if not question or len(question)>10000: raise Failure('query_invalid','查询问题无效',422)
        c=self.config
        if not c.judge_model or not c.judge_key: raise Failure('model_unconfigured','NL2SQL 模型未配置',503)
        response=self.http.json('POST',c.judge_url,'/chat/completions',token=c.judge_key,json={
            'model':c.judge_model,'messages':[{'role':'system','content':'根据表字段生成单条 MySQL SELECT，只返回 SQL，不使用跨库查询。表结构：'+json.dumps(schema,ensure_ascii=False)},
                                             {'role':'user','content':question}]})
        try: return response['choices'][0]['message']['content'].strip().removeprefix('```sql').removesuffix('```').strip()
        except (KeyError,IndexError,AttributeError) as exc: raise Failure('sql_generation_failed','SQL 生成响应无效',422) from exc
