"""Independent API; no modeling project identifiers or runtime imports."""
from contextlib import asynccontextmanager
import fcntl
import json
from pathlib import Path
import time
import uuid

from fastapi import Body as Payload, FastAPI, Request, Response, UploadFile, File, Query
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from .core import Config, Failure, Identity, Transport, digest, trusted_origins
from .store import Store
from .packages import Packages, generate_mapping, MAPPING, unpack
from .data_sources import DataSources, read_tables
from .execution import Consumer, Credential, Judge, Engine, report, DEFAULT_CONCURRENCY, MAX_CONCURRENCY
from .judge_settings import JudgeSettings, connection_test_failure
from .modeling_projects import linked_projects
from .nl2sql import QueryService
from .source_config import SourceConfig
from .graph_import import scope_target
from .artifact_jobs import ArtifactJobs
from .notifications import Notifier
from .suggestions import Suggestions
from .artifacts import ACTIVE
from .feedback.access import PackageAccess
from .feedback.intake import Intake
from .feedback.pipeline import Assistant, Pipeline
from .feedback.store import FeedbackStore, run_owner
from . import modeling_result, package_routes


class Body(BaseModel):
    model_config=ConfigDict(extra='forbid')


class Ref(Body):
    ontology_id: str = Field(min_length=1,max_length=128,pattern=r'^[A-Za-z0-9_-]+$')
    release_id: str = Field(min_length=1,max_length=128,pattern=r'^[A-Za-z0-9_-]+$')
    package_digest: str = Field(default='',max_length=64,pattern=r'^(|[a-f0-9]{64})$')


class Prepare(Body):
    request_id: str = Field(min_length=1,max_length=100,pattern=r'^[a-zA-Z0-9_-]+$')
    package_ref: Ref
    data_source_id: str = ''


class Start(Body):
    request_id: str = Field(min_length=1,max_length=100,pattern=r'^[a-zA-Z0-9_-]+$')
    preparation_id: str
    eval_set_id: str
    eval_set_revision: int | None = Field(default=None,ge=1)
    graph_check_id: str = Field(default='',max_length=128)
    replace_existing_graph: StrictBool = False
    concurrency: int = Field(default=DEFAULT_CONCURRENCY,ge=1,le=MAX_CONCURRENCY)
    # Empty uses the consumer default; otherwise an enabled model of the consumer global pool.
    model_profile_id: str = Field(default='',max_length=200,pattern=r'^(|global:\S{1,192})$')


class Publication(Body):
    request_id: str = Field(min_length=1,max_length=100,pattern=r'^[a-zA-Z0-9_-]+$')
    preparation_id: str


class JudgeConfig(Body):
    base_url: str = Field(max_length=2048)
    model: str = Field(max_length=200)
    api_key: str = Field(default='',max_length=4096)  # Empty keeps the current key for the same gateway.


class JudgeSwitch(Body):
    enabled: bool


class SourceInspection(Body):
    provider: str = Field(pattern=r'^(nl2sql_http|nl2sql_template)$')
    connection: dict | None = None
    data_source_id: str = Field(default='',max_length=128)


class SourcePreview(Body):
    data_source_id: str = Field(min_length=1,max_length=128)
    package_ref: Ref


class GraphInspection(SourcePreview):
    preparation_id: str = Field(default='',max_length=128)


def release_ref(receipt):
    """Non-sensitive reference to the Hub release the user chose."""
    return {k:receipt[k] for k in ('ontology_id','release_id','name','version','package_digest') if k in receipt}


def public(record):
    value={k:v for k,v in record.items() if k not in ('connection','tables_snapshot','content','source','cases','intent','mapping','preview','model','relations','schema_fingerprint','graph_confirmation')}
    if 'cases' in record: value['case_count']=len(record['cases'])
    if 'mapping' in record:
        value['mapping']=bool(record['mapping'])
    if 'preview' in record:
        objects=record['preview'].get('objects',{})
        value['preview']={'total':len(objects),'objects':{
            k:{'table':v['table'],'matched_columns':v['matched_columns'],'missing_columns':v['missing_columns'][:20]}
            for k,v in list(objects.items())[:20]},'truncated':len(objects)>20,
            'warnings':record['preview'].get('warnings',[])[:10]}
    if record.get('model'):
        value['model']={k:v for k,v in record['model'].items() if k!='profiles'}
    return value


def create_app(config=None,transport=None):
    config=config or Config.environment()
    http=transport or Transport()
    store=Store(config.data_dir,config.encryption_key)
    identity=Identity(config,http)
    packages=Packages(config,http,store)
    sources=DataSources(config,store)
    source_config=SourceConfig(store,http)
    consumer=Consumer(config,http)
    judge_settings=JudgeSettings(config,store)
    judge=Judge(judge_settings,http)
    engine=Engine(store,identity,packages,sources,consumer,judge)
    notifier=Notifier(config,store,http)
    jobs=ArtifactJobs(store,Suggestions(judge,packages,http),notifier)
    engine.on_stop=jobs.schedule
    package_access=PackageAccess(packages)
    # Consumer feedback (data flywheel): off unless EVAL_FEEDBACK_ENABLED; then a background thread processes it.
    jobs.notify_feedback=config.flag('notify_feedback_runs')
    pipeline=Pipeline(config,store,Assistant(judge_settings,http),jobs.schedule)
    intake=Intake(config,store,identity,pipeline.wake)
    feedback_store=FeedbackStore(store)

    @asynccontextmanager
    async def lifespan(app):
        lock=open(config.data_dir/'worker.lock','a')
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            store.recover()
            store.prune_inputs()
            store.prune_orphans()
            jobs.recover()
            notifier.start()
            pipeline.start()
            import asyncio
            async def maintenance():
                while True:
                    await asyncio.sleep(60)
                    store.prune_inputs()
                    feedback_store.prune()
            sweeper=asyncio.create_task(maintenance())
            try:
                yield
            finally:
                sweeper.cancel()
                await asyncio.gather(sweeper,return_exceptions=True)
        finally:
            pipeline.stop()
            engine.close()
            jobs.close()
            notifier.stop()
            store.close()
            lock.close()

    app=FastAPI(title='Evaluation service',lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    app.state.store,app.state.engine,app.state.jobs,app.state.feedback=store,engine,jobs,pipeline
    prefix='/evaluation/api'

    @app.exception_handler(Failure)
    async def failure_handler(request,exc):
        return JSONResponse(exc.payload(),status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request, exc):
        return JSONResponse({'code':'invalid_input','message':'输入格式无效，请检查字段'},status_code=422)

    @app.middleware('http')
    async def limits(request,call_next):
        # Bound the actual body, including chunked requests, before multipart parsing.
        maximum=50*1024*1024+65536 if request.url.path==f'{prefix}/inputs' else 1024*1024
        try:
            length=int(request.headers.get('content-length','0'))
        except ValueError:
            return JSONResponse({'message':'请求长度无效'},status_code=400)
        if length>maximum:
            return JSONResponse({'message':'请求体超过限制'},status_code=413)
        original=request._receive
        size=0
        async def receive():
            nonlocal size
            message=await original()
            size+=len(message.get('body',b''))
            if size>maximum:
                raise Failure('request_limit','请求体超过限制',413)
            return message
        request._receive=receive
        response=await call_next(request)
        response.headers['Cache-Control']='no-store'
        response.headers['Referrer-Policy']='no-referrer'
        response.headers['X-Content-Type-Options']='nosniff'
        return response

    def actor(request):
        header=request.headers.get('authorization','')
        token=header[7:] if header.startswith('Bearer ') else ''
        return identity.verify(token),token

    def refresh_of(request):
        """Refresh token handed over so a long run can renew its login; kept in engine memory only."""
        value=request.headers.get('x-refresh-token','')
        return value if len(value)<=8192 else ''

    def load_run(request,rid):
        """A run the caller may use: their own evaluation run, or a run built from feedback on a package they can see in Hub.

        Returns the run's owner, which keys its records and artifacts, the caller, the caller's token and the run."""
        caller,token=actor(request)
        owner,run=store.find(rid,'runs')
        if run.get('origin')=='consumer_feedback':
            package_access.require(caller,token,(run.get('package_ref') or {}).get('ontology_id'))
        elif owner!=caller:
            raise Failure('not_found','记录不存在或无权访问',404)
        return owner,caller,token,run

    def evaluation_run(request,rid):
        """Only evaluation runs execute on consumer; runs built from feedback cannot be stopped or resumed."""
        owner,caller,token,run=load_run(request,rid)
        if run.get('origin')=='consumer_feedback':
            raise Failure('not_applicable','消费反馈轮次不执行评测用例，不能停止或恢复',409)
        return owner,token,run

    def resolve_release(owner,run,token):
        """The Hub release of a feedback run's package version, found with the caller's login and kept on the run.

        Feedback names a version only; reading the package for suggestions and filing the run in modeling need the release."""
        ref=run.get('package_ref') or {}
        if ref.get('release_id'): return run
        from urllib.parse import quote
        offset=0
        while offset is not None:
            page=packages.call('GET',f"/api/packages/{quote(ref['ontology_id'],safe='')}/releases?offset={offset}&limit=100",token)
            found=next((item for item in page.get('items') or [] if item.get('version')==ref.get('version')),None)
            if found:
                receipt=release_ref(found)
                return store.save(owner,run['id'],dict(run,package_ref=receipt,base_ref=dict(receipt)))
            offset=page.get('next_offset')
        raise Failure('release_unpublished',f"知识包版本 {ref.get('version') or '（未注明）'} 还没有发布到 Hub，请先在建模中发布该版本",409)

    def login(request,token):
        """The caller's login for work that outlives the request, renewed with its refresh token when one was sent."""
        return engine.token_source(Credential(token,refresh_of(request)))

    def account(request):
        header=request.headers.get('authorization','')
        return identity.account(header[7:] if header.startswith('Bearer ') else '')

    def administrator(request):
        owner,role=account(request)
        if role!='admin': raise Failure('forbidden','需要管理员权限',403,'authorization')
        return owner

    @app.get('/')
    def root():
        # Pages and API keep the /evaluation prefix so a same-origin gateway can route by path.
        return RedirectResponse('/evaluation/',status_code=302)

    @app.get(f'{prefix}/health')
    def health(response:Response):
        # Liveness only, so any origin may check it before opening the evaluation page.
        response.headers['Access-Control-Allow-Origin']='*'
        return {'status':'ok','service':'evaluation'}

    @app.get(f'{prefix}/client-config')
    def client_config():
        # An empty list means only pages on this host name (loopback names count as one) may hand over a login.
        return {'trusted_origins':trusted_origins(config.trusted_origins)}

    @app.post(f'{prefix}/nl2sql/api/v1/{{operation}}')
    def sql_query(operation:str,body:dict,request:Request):
        if operation not in ('query','sql'): raise Failure('not_found','操作不存在',404)
        header=request.headers.get('authorization','')
        return QueryService(config,http,judge_settings).query(body,header[7:] if header.startswith('Bearer ') else '',operation=='sql')

    @app.get(f'{prefix}/session')
    def session(request:Request):
        header=request.headers.get('authorization','')
        profile=identity.profile(header[7:] if header.startswith('Bearer ') else '')
        current=judge_settings.active(profile['subject'])
        return {'subject':profile['subject'],'user':{'username':profile['username'],'role':profile['role']},
                'judge':{'model':current['model'],'revision':current['revision'],'source':current['source']},
                'judge_configured':current['configured'],'model_configuration':'MODEL-CONFIG-01',
                'feedback_enabled':config.flag('feedback_enabled')}

    @app.get(f'{prefix}/judge-settings')
    def judge_settings_view(request:Request):
        _,role=account(request)
        return judge_settings.view(role=='admin')

    @app.put(f'{prefix}/judge-settings')
    def judge_settings_save(body:JudgeConfig,request:Request):
        judge_settings.save(body.base_url,body.model,body.api_key,administrator(request))
        return judge_settings.view(True)

    @app.delete(f'{prefix}/judge-settings')
    def judge_settings_reset(request:Request):
        administrator(request)
        judge_settings.reset()
        return judge_settings.view(True)

    @app.put(f'{prefix}/judge-settings/enabled')
    def judge_settings_switch(body:JudgeSwitch,request:Request):
        judge_settings.set_default_enabled(body.enabled,administrator(request))
        return judge_settings.view(True)

    @app.post(f'{prefix}/judge-settings/test')
    def judge_settings_test(body:JudgeConfig,request:Request):
        # Runs the same structured-verdict check as run preflight, without saving.
        administrator(request)
        candidate=dict(judge_settings.candidate(body.base_url,body.model,body.api_key),revision='test')
        try:
            judge.preflight(candidate)
        except Failure as exc:
            raise connection_test_failure(exc) from exc
        return {'ok':True}

    # Personal configuration: overrides the shared default for the caller's own new runs only.
    @app.get(f'{prefix}/judge-settings/me')
    def judge_settings_mine(request:Request):
        owner,_=actor(request)
        return judge_settings.personal_view(owner)

    @app.put(f'{prefix}/judge-settings/me')
    def judge_settings_save_mine(body:JudgeConfig,request:Request):
        owner,_=actor(request)
        judge_settings.save_personal(owner,body.base_url,body.model,body.api_key)
        return judge_settings.personal_view(owner)

    @app.delete(f'{prefix}/judge-settings/me')
    def judge_settings_reset_mine(request:Request):
        owner,_=actor(request)
        judge_settings.reset_personal(owner)
        return judge_settings.personal_view(owner)

    @app.post(f'{prefix}/judge-settings/me/test')
    def judge_settings_test_mine(body:JudgeConfig,request:Request):
        owner,_=actor(request)
        candidate=dict(judge_settings.candidate(body.base_url,body.model,body.api_key,owner),revision='test')
        try:
            judge.preflight(candidate)
        except Failure as exc:
            raise connection_test_failure(exc) from exc
        return {'ok':True}

    @app.post(f'{prefix}/auth/{{operation}}')
    def auth(operation:str,body:dict):
        if operation=='refresh':
            payload={'grant_type':'refresh_token','refresh_token':body.get('refresh_token','')}
        elif operation=='login':
            payload={'grant_type':'password','username':body.get('username',''),'password':body.get('password','')}
        else: raise Failure('not_found','操作不存在',404)
        result=http.json('POST',config.auth_url,'/token',json=payload,limit=16384)
        return {k:result[k] for k in ('access_token','refresh_token','expires_in','token_type') if k in result}

    @app.get(f'{prefix}/packages')
    def list_packages(request:Request,offset:int=Query(0,ge=0),limit:int=Query(20,ge=1,le=100)):
        _,token=actor(request)
        return packages.call('GET',f'/api/packages?offset={offset}&limit={limit}',token)

    @app.get(f'{prefix}/packages/{{pid}}/releases')
    def list_releases(pid:str,request:Request,offset:int=Query(0,ge=0)):
        from urllib.parse import quote
        _,token=actor(request)
        return packages.call('GET',f"/api/packages/{quote(pid,safe='')}/releases?offset={offset}&limit=20",token)

    @app.get(f'{prefix}/packages/{{pid}}/releases/{{rid}}')
    def release_receipt(pid:str,rid:str,request:Request):
        from urllib.parse import quote
        _,token=actor(request)
        return packages.call('GET',f"/api/packages/{quote(pid,safe='')}/releases/{quote(rid,safe='')}",token)

    def definition(kind,body,owner,existing=None):
        if not isinstance(body,dict) or not isinstance(body.get('name'),str) or not 0<len(body['name'])<=128:
            raise Failure('definition_invalid','需要有效名称',422)
        if kind=='eval-sets':
            if set(body)-{'name','cases','revision'}: raise Failure('definition_invalid','未知评测集字段',422)
            cases=body.get('cases',[])
            if not isinstance(cases,list) or not 0<len(cases)<=500: raise Failure('case_limit','用例数须为 1–500',422)
            normalized=[]
            for i,case in enumerate(cases):
                if not isinstance(case,dict) or not isinstance(case.get('question'),str) or not 0<len(case['question'])<=10000:
                    raise Failure('case_invalid','用例问题格式无效',422)
                reference=case.get('reference','')
                if not isinstance(reference,str) or len(reference)>20000:
                    raise Failure('case_invalid','参考答案格式无效',422)
                normalized.append(dict(id=str(i),question=case['question'],reference=reference))
            return dict(name=body['name'],cases=normalized)
        if set(body)-{'name','mode','provider','input_ids','connection','tables','import_data','revision','inspection_id'}:
            raise Failure('definition_invalid','未知数据源字段',422)
        if 'import_data' in body and not isinstance(body['import_data'],bool):
            raise Failure('import_flag_invalid','导入选项必须是布尔值',422)
        mode=body.get('mode','none')
        if mode not in ('none','dataset','connection'): raise Failure('mode_invalid','数据源模式无效',422)
        provider=body.get('provider','nl2sql_http')
        if 'connection' in body and not isinstance(body['connection'],dict): raise Failure('connection_invalid','连接配置需要对象',422)
        if mode=='dataset' and provider=='neo4j':
            raise Failure('graph_connection_required','图模式请选择 connection 并显式允许数据导入',422)
        if provider not in ('neo4j','nl2sql_http','nl2sql_template'): raise Failure('provider_invalid','不支持的数据源',422)
        ids=body.get('input_ids',[])
        if not isinstance(ids,list) or len(ids)>20 or any(not isinstance(fid,str) or not 0<len(fid)<=128 for fid in ids):
            raise Failure('input_limit','最多 20 个有效文件句柄',422)
        for fid in ids: store.get(owner,fid,'inputs')
        tables=body.get('tables',{})
        if not isinstance(tables,dict) or len(tables)>100:
            raise Failure('table_limit','表定义超过限制',422)
        if any(not isinstance(cols,list) or len(cols)>256 or any(not isinstance(col,str) or len(col)>64 for col in cols) for cols in tables.values()):
            raise Failure('table_invalid','表字段定义无效',422)
        value=dict(name=body['name'],mode=mode,provider=provider,input_ids=ids,tables=body.get('tables',{}),import_data=bool(body.get('import_data')))
        # Form payloads use a narrow connection schema; old API clients may still submit
        # their existing connection/tables contract without the new inspection handle.
        form=body.get('connection',{})
        graph_form=provider=='neo4j' and 'workspace_value' in form
        form_payload='inspection_id' in body or 'evaluation_workspace_id' in form or graph_form
        if mode=='connection' and form_payload:
            connection=source_config.connection(owner,provider,body.get('connection'),existing)
            value['connection']=store.encrypt(connection)
            if provider!='neo4j':
                inspection_id=body.get('inspection_id')
                if not inspection_id: raise Failure('schema_required','请先读取对应数据库的表与字段',422,'schema')
                value.update(source_config.apply_inspection(owner,provider,connection,inspection_id))
            elif value['import_data'] and not connection.get('ontology_property'):
                raise Failure('graph_scope_required','导入图数据需要 Ontology 隔离字段',422)
        elif 'connection' in body:
            if not isinstance(body['connection'],dict): raise Failure('connection_invalid','连接配置需要对象',422)
            value['connection']=store.encrypt(body['connection'])
        elif existing and existing.get('connection'): value['connection']=existing['connection']
        if mode=='connection' and not value.get('connection'): raise Failure('connection_missing','需要连接配置',422)
        return value

    @app.post(f'{prefix}/data-source-inspections')
    def inspect_source(body:SourceInspection,request:Request):
        owner,_=actor(request)
        existing=store.get(owner,body.data_source_id,'data-sources') if body.data_source_id else None
        connection=source_config.connection(owner,body.provider,body.connection,existing)
        return source_config.inspect(owner,body.provider,connection)

    @app.post(f'{prefix}/graph-import-checks')
    def inspect_graph(body:GraphInspection,request:Request):
        owner,token=actor(request)
        prep=store.get(owner,body.preparation_id,'preparations') if body.preparation_id else None
        source=prep['source'] if prep else store.get(owner,body.data_source_id,'data-sources')
        if prep:
            ref=body.package_ref.model_dump()
            package_changed=any(ref[key]!=prep['base'][key] for key in ('ontology_id','release_id'))
            if prep['status']!='ready' or source.get('id')!=body.data_source_id or package_changed:
                raise Failure('graph_check_stale','准备结果与当前数据源或知识包不一致，请重新准备',409,'data_import')
        _,receipt=packages.fetch(body.package_ref.model_dump(),token)
        return sources.graph.inspect(owner,source,receipt['ontology_id'],body.preparation_id)

    @app.get(f'{prefix}/inputs/{{rid}}')
    def input_metadata(rid:str,request:Request):
        owner,_=actor(request)
        return public(store.get(owner,rid,'inputs'))

    @app.post(f'{prefix}/data-source-previews')
    def source_preview(body:SourcePreview,request:Request):
        owner,token=actor(request)
        source=store.get(owner,body.data_source_id,'data-sources')
        _,tables=sources.freeze(owner,source)
        raw,_=packages.fetch(body.package_ref.model_dump(),token)
        graph=source.get('provider')=='neo4j'
        mapping,preview=generate_mapping(raw,{k:v['columns'] for k,v in tables.items()},graph) if tables else (None,{'objects':{},'warnings':[]})
        objects=[]
        for name,item in preview['objects'].items():
            table=tables.get(item.get('table'),{})
            count=len(table.get('rows',[]))
            if graph and mapping and name in mapping['objects']:
                keys=mapping['objects'][name].get('primary_key',[])
                fields=mapping['objects'][name]['sources'][0]['fields']
                columns=table.get('columns',[])
                indices=[columns.index(fields[k]['source_column']) for k in keys if k in fields and fields[k]['source_column'] in columns]
                if indices:
                    if any(any(row[i] is None or row[i]=='' for i in indices) for row in table.get('rows',[])):
                        raise Failure('graph_key_missing','图数据缺少主键，请补齐后重新预览',422,'data_import')
                    count=len({tuple(row[i] for i in indices) for row in table.get('rows',[])})
            objects.append({'name':name,'table':item.get('table'),'matched_columns':item.get('matched_columns',[]),'missing_columns':item.get('missing_columns',[]),'instances':count})
        relations=[]
        for left,edges in preview.get('relations',{}).items():
            for edge in edges:
                right=edge['target']
                try:
                    l,r=mapping['objects'][left],mapping['objects'][right]
                    lt,rt=tables[l['sources'][0]['resource']],tables[r['sources'][0]['resource']]
                    lk=edge.get('sourceJoinProperty') or l['primary_key'][0]
                    rk=edge.get('targetJoinProperty') or r['primary_key'][0]
                    li=lt['columns'].index(l['sources'][0]['fields'][lk]['source_column'])
                    ri=rt['columns'].index(r['sources'][0]['fields'][rk]['source_column'])
                    # Count distinct primary-key pairs without materializing a potentially
                    # quadratic join; matching MERGE semantics, nulls never join.
                    def keyed(obj,table):
                        indices=[table['columns'].index(obj['sources'][0]['fields'][k]['source_column']) for k in obj['primary_key']]
                        return [(row,tuple(row[i] for i in indices)) for row in table['rows']]
                    targets={}
                    for row,key in keyed(r,rt):
                        if row[ri] is not None: targets.setdefault(row[ri],set()).add(key)
                    origins={}
                    for row,key in keyed(l,lt):
                        if row[li] is not None: origins.setdefault(row[li],set()).add(key)
                    count=sum(len(keys)*len(targets.get(value,set())) for value,keys in origins.items())
                except (KeyError,IndexError,ValueError):
                    count=None
                relations.append({'source':left,'target':right,'relation':edge['relation'],'instances':count})
        return {'objects':objects[:100],'relations':relations[:100],'warnings':preview.get('warnings',[])[:20],
                'skipped':preview.get('skipped',[])[:20],'truncated':len(objects)>100 or len(relations)>100,
                'entity_count':len(objects),'instances':sum(o['instances'] for o in objects),
                'relationship_count':sum(r['instances'] or 0 for r in relations),'graph':graph}

    # Register fixed paths: never let callers read internal record kinds.
    def register_definitions(kind):
        def listing(request:Request,offset:int=Query(0,ge=0)):
            owner,_=actor(request)
            page=store.list(owner,kind,offset)
            page['items']=[public(item) for item in page['items']]
            return page
        def get(rid:str,request:Request):
            owner,_=actor(request)
            item=store.get(owner,rid,kind)
            return source_config.view(owner,item) if kind=='data-sources' else item
        def create(body:dict,request:Request):
            owner,_=actor(request)
            if store.list(owner,kind)['total']>=50: raise Failure('definition_limit','最多 50 个定义',409)
            return public(store.create(owner,kind,definition(kind,body,owner)))
        def update(rid:str,body:dict,request:Request):
            owner,_=actor(request)
            old=store.get(owner,rid,kind)
            if not isinstance(body.get('revision'),int): raise Failure('revision_required','需要记录修订号',422)
            return public(store.save(owner,rid,definition(kind,body,owner,old),body['revision']))
        def delete(rid:str,request:Request):
            owner,_=actor(request)
            store.get(owner,rid,kind)
            store.delete(owner,rid)
            return {'deleted':True}
        app.add_api_route(prefix+'/'+kind,listing,methods=['GET'])
        app.add_api_route(prefix+'/'+kind,create,methods=['POST'])
        app.add_api_route(prefix+'/'+kind+'/{rid}',get,methods=['GET'])
        app.add_api_route(prefix+'/'+kind+'/{rid}',update,methods=['PUT'])
        app.add_api_route(prefix+'/'+kind+'/{rid}',delete,methods=['DELETE'])
    for kind in ('eval-sets','data-sources'): register_definitions(kind)

    @app.post(f'{prefix}/inputs')
    async def upload(request:Request,file:UploadFile=File(...)):
        owner,_=actor(request)
        raw=await file.read(50*1024*1024+1)
        tables=read_tables(raw,file.filename or '')
        record=store.create(owner,'inputs',dict(name=file.filename,size=len(raw),digest=digest(raw),
            tables={k:v['columns'] for k,v in tables.items()},content=store.encrypt(tables),created=time.time()))
        return public(record)

    @app.post(f'{prefix}/package-preparations')
    def prepare(body:Prepare,request:Request):
        owner,token=actor(request)
        intent=body.model_dump()
        # Idempotency is checked before resolving mutable definitions again.
        prep=store.create(owner,'preparations',dict(status='pending',intent=intent),body.request_id,identity=intent)
        if prep['status']=='ready': return public(prep)
        if not prep.get('source'):
            source=store.get(owner,body.data_source_id,'data-sources') if body.data_source_id else {'mode':'none'}
            source,tables=sources.freeze(owner,source)
            raw,base=packages.fetch(body.package_ref.model_dump(),token)
            mapping,preview=generate_mapping(raw,{k:v['columns'] for k,v in tables.items()},source.get('provider')=='neo4j') if tables else (None,{})
            files,_=unpack(raw)
            changed=mapping is not None and (MAPPING not in files or json.loads(files[MAPPING])!=mapping)
            prep=store.save(owner,prep['id'],dict(status='pending',intent=intent,base=base,source=source,
                mapping=mapping,preview=preview,changed=changed))
        ref=packages.snapshot(owner,prep,token) if prep['changed'] else prep['base']
        prep.update(status='ready',package_ref=ref)
        return public(store.save(owner,prep['id'],prep))

    @app.get(f'{prefix}/package-preparations/{{rid}}')
    def preparation(rid:str,request:Request):
        owner,_=actor(request)
        return public(store.get(owner,rid,'preparations'))

    @app.post(f'{prefix}/package-publications')
    def publication(body:Publication,request:Request):
        owner,token=actor(request)
        prep=store.get(owner,body.preparation_id,'preparations')
        if prep['status']!='ready': raise Failure('not_ready','包准备尚未完成',409)
        return packages.publish(owner,prep,token,body.request_id,'release')

    @app.get(f'{prefix}/package-publications')
    def publications(request:Request,offset:int=Query(0,ge=0)):
        owner,_=actor(request)
        page=store.list(owner,'publications',offset)
        page['items']=[public(item) for item in page['items']]
        return page

    @app.get(f'{prefix}/package-publications/{{rid}}')
    def publication_status(rid:str,request:Request):
        owner,_=actor(request)
        return public(store.get(owner,rid,'publications'))

    @app.post(f'{prefix}/runs',status_code=202)
    def start(body:Start,request:Request):
        owner,token=actor(request)
        intent=body.model_dump(exclude={'graph_check_id','replace_existing_graph'})
        if body.graph_check_id or body.replace_existing_graph:
            intent.update(graph_check_id=body.graph_check_id,replace_existing_graph=body.replace_existing_graph)
        previous=store.lookup_request(owner,'runs',body.request_id,intent)
        if previous:
            return public(previous)
        prep=store.get(owner,body.preparation_id,'preparations')
        cases=store.get(owner,body.eval_set_id,'eval-sets')
        if body.eval_set_revision is not None and cases['revision']!=body.eval_set_revision:
            raise Failure('revision_conflict','评测集已变更，请返回上一步重新加载并确认',409)
        if prep['status']!='ready': raise Failure('not_ready','包准备尚未完成',409)
        with engine.guard:
            package_idle(owner,prep['package_ref']['ontology_id'])
            if any(key[0]==owner for key in engine.active): raise Failure('evaluation_busy','已有活动评测，请先查看或停止当前任务',409)
            if len(engine.active)>=16: raise Failure('evaluation_busy','服务队列已满，请稍后再试',409)
            source=dict(prep['source'])
            if source.get('provider')=='neo4j' and source.get('import_data'):
                graph_available(source,prep['package_ref']['ontology_id'])
                source['graph_confirmation']=sources.graph.authorize(owner,prep,body.graph_check_id,body.replace_existing_graph)
            elif body.graph_check_id or body.replace_existing_graph:
                raise Failure('graph_import_required','当前评测未启用 Neo4j 图导入',422,'data_import')
            run=store.create(owner,'runs',dict(status='queued',phase='preparing',preparation_id=prep['id'],
                package_ref=prep['package_ref'],source=source,mapping=prep['mapping'],relations=prep['preview'].get('relations',{}),judge=judge.profile(owner=owner),
                # The release the user chose; package_ref is what runs, an evaluation snapshot when the mapping changed.
                base_ref=release_ref(prep['base']),
                eval_set_id=cases['id'],eval_set_name=cases['name'],data_source_name=prep['source'].get('name','不使用数据源'),
                concurrency=body.concurrency,model_profile_id=body.model_profile_id,
                created_at=time.time(),case_revision=cases['revision'],cases=[dict(**c,status='pending') for c in cases['cases']]),
                body.request_id,identity=intent)
            engine.start(owner,run['id'],token,refresh_of(request))
        return public(run)

    def package_idle(owner, package, current_run=''):
        # Only an executing run blocks its package; completed, stopped, interrupted or waiting runs do not.
        if store.package_run_in_status(owner,package,ACTIVE,exclude=current_run):
            raise Failure('package_run_active','该知识包已有正在执行的评测任务，请等待其结束或先停止后再发起',409)

    def graph_available(source, ontology_id, current_run=''):
        if source.get('provider')!='neo4j' or not source.get('import_data'): return
        target=scope_target(sources.graph.connection(store,source,ontology_id))
        for active_owner,active_id in engine.active:
            if active_id==current_run: continue
            active=store.get(active_owner,active_id,'runs')
            other=active.get('source',{})
            if other.get('provider')=='neo4j' and other.get('import_data') and target==scope_target(sources.graph.connection(store,other,active['package_ref']['ontology_id'])):
                raise Failure('graph_scope_busy','此图数据范围已有活动评测，请等待结束或先停止该评测',409,'data_import')

    def with_base(owner,run):
        """Runs created before base_ref was recorded take it from their preparation, which kept the chosen release.

        Display only: the stored run is not changed. One local lookup per run, at most 20 per page.
        """
        if run.get('base_ref') or not run.get('preparation_id'): return run
        try:
            base=store.get(owner,run['preparation_id'],'preparations').get('base')
        except Failure:
            return run
        return dict(run,base_ref=release_ref(base)) if isinstance(base,dict) else run

    def run_view(owner,run):
        return dict(public(with_base(owner,run)),origin=run.get('origin') or 'evaluation',summary=report(run),
                    artifacts=jobs.view(owner,run['id']),notification=notifier.status(run['id']),
                    optimization=modeling_result.public(modeling_result.state(store,jobs,owner,run)))

    @app.get(f'{prefix}/runs')
    def runs(request:Request,offset:int=Query(0,ge=0),limit:int=Query(20,ge=1,le=20),ontology_id:str=Query('',max_length=128),
             origin:str=Query('evaluation',pattern=r'^(evaluation|consumer_feedback)$')):
        owner,token=actor(request)
        if origin=='consumer_feedback':
            # Feedback runs of one package, for whoever may read that package in Hub.
            if not ontology_id: raise Failure('ontology_required','查看消费反馈轮次需要指定知识包',422)
            package_access.require(owner,token,ontology_id)
            page=store.package_runs(run_owner(ontology_id),ontology_id,offset,limit)
            page['items']=[run_view(run_owner(ontology_id),r) for r in page['items']]
            return page
        page=store.package_runs(owner,ontology_id,offset,limit) if ontology_id else store.list(owner,'runs',offset,limit)
        page['items']=[run_view(owner,r) for r in page['items']]
        return page

    @app.get(f'{prefix}/modeling-projects')
    def modeling_projects(request:Request,ontology_id:list[str]=Query(...)):
        # Display only: names come from onto-modeling with the user's own login and are never stored.
        _,token=actor(request)
        return linked_projects(config,http,token,ontology_id)

    @app.get(f'{prefix}/consumer-models')
    def consumer_models(request:Request):
        # Choices for a new run's answering model; when unavailable the run uses the consumer default.
        _,token=actor(request)
        try:
            return {'available':True,'items':consumer.global_models(token)}
        except Failure:
            return {'available':False,'items':[]}

    @app.get(f'{prefix}/runs/{{rid}}')
    def run(rid:str,request:Request):
        owner,_,_,record=load_run(request,rid)
        return run_view(owner,record)

    @app.get(f'{prefix}/runs/{{rid}}/results')
    def results(rid:str,request:Request,offset:int=Query(0,ge=0)):
        cases=load_run(request,rid)[3]['cases']
        items=[]
        for c in cases[offset:offset+20]:
            item={k:v for k,v in c.items() if k not in ('answer','reference','overrides','judgement','override','trace')}
            item['question']=c['question'][:256]
            for key in ('judgement','override'):
                if c.get(key): item[key]={**c[key],'reason':c[key]['reason'][:256]}
            if isinstance(c.get('error'),dict): item['error']={**c['error'],'message':str(c['error'].get('message',''))[:256]}
            items.append(item)
        return dict(items=items,total=len(cases),next_offset=offset+20 if offset+20<len(cases) else None)

    @app.get(f'{prefix}/runs/{{rid}}/results/{{cid}}/answer')
    def answer(rid:str,cid:int,request:Request):
        cases=load_run(request,rid)[3]['cases']
        if not 0<=cid<len(cases): raise Failure('not_found','用例不存在',404)
        return cases[cid]

    @app.get(f'{prefix}/runs/{{rid}}/results/{{cid}}/evidence')
    def evidence(rid:str,cid:int,request:Request):
        _,_,token,run=load_run(request,rid)
        if not 0<=cid<len(run['cases']): raise Failure('not_found','用例不存在',404)
        case=run['cases'][cid]
        if not case.get('remote_id') or not run.get('workspace') or case['status'] in ('executing','unknown','pending'):
            return {'trace_status':'unavailable','events':[],'message':'消费执行尚未结算'}
        try:
            raw=http.request('GET',config.consumer_url,f"/agentic/runs/{case['remote_id']}/events?after=0",
                             token=token,workspace=run['workspace'],limit=1024*1024)
            events=[]
            for block in raw.decode('utf-8').split('\n\n'):
                data='\n'.join(line[5:].strip() for line in block.splitlines() if line.startswith('data:'))
                if not data: continue
                value=json.loads(data)
                # Consumer owns the full trace. Return bounded public lifecycle evidence only.
                if isinstance(value,dict):
                    events.append({k:value[k] for k in ('type','event','seq','status','timestamp','run_id') if k in value})
                if len(events)>=100: break
            return {'package_ref':run['package_ref'],'consumer':case.get('evidence',{}),
                    'trace_status':'available' if events else 'unavailable','events':events,
                    'truncated':len(events)>=100,'message':'完整工具内容由消费服务管理，此处展示运行事件索引'}
        except (Failure,ValueError):  # also UnicodeDecodeError
            return {'trace_status':'unavailable','events':[],'message':'消费证据暂不可用，未伪造工具轨迹'}

    @app.get(f'{prefix}/runs/{{rid}}/report')
    def get_report(rid:str,request:Request):
        return report(load_run(request,rid)[3])

    @app.post(f'{prefix}/runs/{{rid}}/cancel')
    def cancel(rid:str,request:Request):
        owner,token,_=evaluation_run(request,rid)
        value=engine.stop(owner,rid)
        engine.start(owner,rid,token,refresh_of(request))
        return public(value)

    @app.post(f'{prefix}/runs/{{rid}}/resume')
    @app.post(f'{prefix}/runs/{{rid}}/credentials')
    def resume(rid:str,request:Request):
        owner,token,run=evaluation_run(request,rid)
        if (run.get('error') or {}).get('code')=='configuration_changed' and not run.get('cancel_requested'): raise Failure('configuration_changed','配置已变化，请创建新评测',409)
        if (run.get('error') or {}).get('code') in ('graph_scope_changed','graph_check_required','graph_confirmation_required') and not run.get('cancel_requested'):
            raise Failure('graph_confirmation_required','请重新创建评测并确认当前图数据范围',409,'data_import')
        if run['status'] in ('completed','cancelled','failed'): raise Failure('run_state','此任务当前无需恢复',409)
        with engine.guard:
            if not run.get('cancel_requested'):
                package_idle(owner,run['package_ref']['ontology_id'],rid)
                graph_available(run['source'],run['package_ref']['ontology_id'],rid)
            engine.start(owner,rid,token,refresh_of(request))
            # The engine marks a resumed run as preparing before it works; return that, not the state read above.
            return public(store.get(owner,rid,'runs'))

    @app.post(f'{prefix}/runs/{{rid}}/results/{{cid}}/override')
    def override(rid:str,cid:int,body:dict,request:Request):
        owner,caller,token,_=load_run(request,rid)
        with engine.guard:
            run=store.get(owner,rid,'runs')
            feedback=run.get('origin')=='consumer_feedback'
            if feedback and (store.optimization(rid) or {}).get('status')!='awaiting_review':
                raise Failure('invalid_state','这一轮消费反馈已确认或驳回，不能再修改',409)
            reference=body.get('reference')
            if reference is not None and (not feedback or not isinstance(reference,str) or len(reference)>4000):
                raise Failure('override_invalid','只有消费反馈轮次可以补充标准答案，且不超过 4000 字',422)
            if (owner,rid) in engine.active: raise Failure('run_active','运行中不能改判',409)
            reason=body.get('reason')
            valid_reason=isinstance(reason,str) and 0<len(reason)<=4000
            if not 0<=cid<len(run['cases']) or body.get('verdict') not in ('pass','partial','fail') or not valid_reason:
                raise Failure('override_invalid','请提供判定及原因',422)
            if body.get('revision')!=run['revision']: raise Failure('revision_conflict','请刷新后重试',409)
            case=run['cases'][cid]
            if not case.get('answer'): raise Failure('answer_missing','未获得答案不能改判',409)
            if reference is not None: case['reference']=reference.strip()
            value=dict(verdict=body['verdict'],reason=body['reason'],source='human',actor=caller,time=time.time())
            case.setdefault('overrides',[]).append(value)
            case['override']=value
            saved=public(store.save(owner,rid,run,body['revision']))
        # The verdict changed: rebuild the report and tell onto-modeling; suggestions are regenerated on request only.
        jobs.schedule(owner,rid,login(request,token),suggestions=False)
        return saved

    @app.delete(f'{prefix}/runs/{{rid}}')
    def delete_run(rid:str,request:Request):
        owner,_,token,record=load_run(request,rid)
        if record.get('origin')=='consumer_feedback':
            # Its clusters return and join the next feedback run of the package version.
            if record['status']=='collecting': raise Failure('run_active','反馈轮次仍在收集中，生成报告后才能删除',409)
            pipeline.release(rid)
            store.delete(owner,rid)
            return {'deleted':True}
        with engine.guard:
            run=store.get(owner,rid,'runs')
            if (owner,rid) in engine.active:
                raise Failure('run_active','评测仍在执行，请先停止评测',409)
            # Interrupted runs are deletable once no question may still be running on consumer's side.
            pending=any(c.get('status') in ('executing','unknown') for c in run['cases'])
            if run['status'] not in ('completed','cancelled','failed') and (run['status'] not in ('interrupted','waiting_auth') or pending):
                raise Failure('run_active','consumer 侧仍有未对账的提交，请先停止评测，完成对账后再删除',409)
            task=store.create(owner,'cleanup_tasks',{'run_id':rid,'workspace_done':False,'data_done':False},rid,identity={'run_id':rid})
            if run.get('managed_workspace') and not task['workspace_done']:
                try:
                    consumer.call('DELETE',f"/workspaces/{run['workspace']}",token)
                except Failure as exc:
                    if exc.status!=404: raise
                task['workspace_done']=True
                task=store.save(owner,task['id'],task)
            if not task['data_done']:
                sources.cleanup(rid,run['source'])
                task['data_done']=True
                task=store.save(owner,task['id'],task)
            store.delete(owner,rid)
            return {'deleted':True}

    @app.post(f'{prefix}/internal/feedback-signals',status_code=202)
    def feedback_signals(body:dict,request:Request):
        """Consumer pushes feedback with the submitting user's login and its own service token."""
        header=request.headers.get('authorization','')
        return intake.receive(body,header[7:] if header.startswith('Bearer ') else '',request.headers.get('x-eval-service-token',''))

    @app.post(f'{prefix}/runs/{{rid}}/modeling-result')
    def modeling_result_api(rid:str,body:modeling_result.ModelingResult,request:Request):
        """Modeling reports how it handled this run's suggestions: the new release it published, or why it failed."""
        owner,caller,token,run=load_run(request,rid)
        value=modeling_result.record(store,jobs,packages,owner,run,body,token,caller)
        return {'run_id':rid,'optimization':modeling_result.public(value)}

    def decide(owner,caller,token,run,body):
        """A package owner confirms a feedback run for modeling, or dismisses it with a reason."""
        decision=(body or {}).get('decision')
        state=modeling_result.state(store,jobs,owner,run) or {}
        if state.get('status')!='awaiting_review':
            raise Failure('invalid_state','这一轮消费反馈已确认或驳回',409)
        now=time.time()
        if decision=='dismiss':
            reason=(body or {}).get('reason')
            if not isinstance(reason,str) or not 0<len(reason.strip())<=2000: raise Failure('reason_required','请填写驳回原因',422)
            state=dict(state,status='dismissed',reason=reason.strip(),dismissed_by=caller,updated_at=now)
            store.put_optimization(owner,run['id'],state)
            return state
        if decision!='confirm': raise Failure('decision_required','请确认交给建模，或填写原因后驳回',422)
        run=resolve_release(owner,run,token)
        state=dict(state,status='notified',confirmed_by=caller,notified_at=now,updated_at=now)
        store.put_optimization(owner,run['id'],state)
        meta=jobs.meta(owner,run['id'])
        if meta: jobs.announce(owner,run,meta)
        return state

    package_routes.register(app,prefix,actor,load_run,store,engine,jobs,notifier,run_view,login,
                            dict(access=package_access,store=feedback_store,pipeline=pipeline,decide=decide,resolve=resolve_release))

    assets=Path(__file__).parents[1]/'frontend'/'dist'
    if assets.exists(): app.mount('/evaluation',StaticFiles(directory=assets,html=True),name='evaluation')
    return app
