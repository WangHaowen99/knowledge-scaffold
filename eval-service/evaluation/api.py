"""Independent API; no modeling project identifiers or runtime imports."""
from contextlib import asynccontextmanager
import fcntl
import json
from pathlib import Path
import time
import uuid

from fastapi import FastAPI, Request, UploadFile, File, Query
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .core import Config, Failure, Identity, Transport, digest
from .store import Store
from .packages import Packages, generate_mapping, MAPPING, unpack
from .data_sources import DataSources, read_tables
from .execution import Consumer, Judge, Engine, report
from .nl2sql import QueryService


class Body(BaseModel):
    model_config=ConfigDict(extra='forbid')


class Ref(Body):
    hub_package_id: str = Field(min_length=1,max_length=128,pattern=r'^[A-Za-z0-9_-]+$')
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


class Publication(Body):
    request_id: str = Field(min_length=1,max_length=100,pattern=r'^[a-zA-Z0-9_-]+$')
    preparation_id: str


def public(record):
    value={k:v for k,v in record.items() if k not in ('connection','tables_snapshot','content','source','cases','intent','mapping','preview','model','relations')}
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
    consumer=Consumer(config,http)
    judge=Judge(config,http)
    engine=Engine(store,identity,packages,sources,consumer,judge)

    @asynccontextmanager
    async def lifespan(app):
        lock=open(config.data_dir/'worker.lock','a')
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            store.recover()
            store.prune_inputs()
            import asyncio
            async def maintenance():
                while True:
                    await asyncio.sleep(60)
                    store.prune_inputs()
            sweeper=asyncio.create_task(maintenance())
            try:
                yield
            finally:
                sweeper.cancel()
                await asyncio.gather(sweeper,return_exceptions=True)
        finally:
            engine.close()
            store.db.close()
            lock.close()

    app=FastAPI(title='Evaluation service',lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    app.state.store,app.state.engine=store,engine
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
        maximum=50*1024*1024+65536 if request.url.path==prefix+'/inputs' else 1024*1024
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

    @app.get(prefix+'/health')
    def health(): return {'status':'ok','service':'evaluation'}

    @app.post(prefix+'/nl2sql/api/v1/{operation}')
    def sql_query(operation:str,body:dict,request:Request):
        if operation not in ('query','sql'): raise Failure('not_found','操作不存在',404)
        header=request.headers.get('authorization','')
        return QueryService(config,http).query(body,header[7:] if header.startswith('Bearer ') else '',operation=='sql')

    @app.get(prefix+'/session')
    def session(request:Request):
        owner,_=actor(request)
        return {'subject':owner,'judge':{'model':config.judge_model,'revision':config.judge_revision},'model_configuration':'MODEL-CONFIG-01'}

    @app.post(prefix+'/auth/{operation}')
    def auth(operation:str,body:dict):
        if operation=='refresh':
            payload={'grant_type':'refresh_token','refresh_token':body.get('refresh_token','')}
        elif operation=='login':
            payload={'grant_type':'password','username':body.get('username',''),'password':body.get('password','')}
        else: raise Failure('not_found','操作不存在',404)
        result=http.json('POST',config.auth_url,'/token',json=payload,limit=16384)
        return {k:result[k] for k in ('access_token','refresh_token','expires_in','token_type') if k in result}

    @app.get(prefix+'/packages')
    def list_packages(request:Request,offset:int=Query(0,ge=0),limit:int=Query(20,ge=1,le=100)):
        _,token=actor(request)
        return packages.call('GET',f'/api/packages?offset={offset}&limit={limit}',token)

    @app.get(prefix+'/packages/{pid}/releases')
    def list_releases(pid:str,request:Request,offset:int=Query(0,ge=0)):
        from urllib.parse import quote
        _,token=actor(request)
        return packages.call('GET','/api/packages/'+quote(pid,safe='')+f'/releases?offset={offset}&limit=20',token)

    @app.get(prefix+'/packages/{pid}/releases/{rid}')
    def release_receipt(pid:str,rid:str,request:Request):
        from urllib.parse import quote
        _,token=actor(request)
        return packages.call('GET','/api/packages/'+quote(pid,safe='')+'/releases/'+quote(rid,safe=''),token)

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
        if set(body)-{'name','mode','provider','input_ids','connection','tables','import_data','revision'}:
            raise Failure('definition_invalid','未知数据源字段',422)
        if 'import_data' in body and not isinstance(body['import_data'],bool):
            raise Failure('import_flag_invalid','导入选项必须是布尔值',422)
        mode=body.get('mode','none')
        if mode not in ('none','dataset','connection'): raise Failure('mode_invalid','数据源模式无效',422)
        provider=body.get('provider','nl2sql_http')
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
        if 'connection' in body:
            if not isinstance(body['connection'],dict): raise Failure('connection_invalid','连接配置需要对象',422)
            value['connection']=store.encrypt(body['connection'])
        elif existing and existing.get('connection'): value['connection']=existing['connection']
        if mode=='connection' and not value.get('connection'): raise Failure('connection_missing','需要连接配置',422)
        return value

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
            return {k:v for k,v in item.items() if k!='connection'}
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

    @app.post(prefix+'/inputs')
    async def upload(request:Request,file:UploadFile=File(...)):
        owner,_=actor(request)
        raw=await file.read(50*1024*1024+1)
        tables=read_tables(raw,file.filename or '')
        record=store.create(owner,'inputs',dict(name=file.filename,size=len(raw),digest=digest(raw),
            tables={k:v['columns'] for k,v in tables.items()},content=store.encrypt(tables),created=time.time()))
        return public(record)

    @app.post(prefix+'/package-preparations')
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
        ref=packages.publish(owner,prep,token,'snapshot-'+prep['id'],'evaluation_snapshot') if prep['changed'] else prep['base']
        prep.update(status='ready',package_ref=ref)
        return public(store.save(owner,prep['id'],prep))

    @app.get(prefix+'/package-preparations/{rid}')
    def preparation(rid:str,request:Request):
        owner,_=actor(request)
        return public(store.get(owner,rid,'preparations'))

    @app.post(prefix+'/package-publications')
    def publication(body:Publication,request:Request):
        owner,token=actor(request)
        prep=store.get(owner,body.preparation_id,'preparations')
        if prep['status']!='ready': raise Failure('not_ready','包准备尚未完成',409)
        return packages.publish(owner,prep,token,body.request_id,'release')

    @app.get(prefix+'/package-publications')
    def publications(request:Request,offset:int=Query(0,ge=0)):
        owner,_=actor(request)
        page=store.list(owner,'publications',offset)
        page['items']=[public(item) for item in page['items']]
        return page

    @app.get(prefix+'/package-publications/{rid}')
    def publication_status(rid:str,request:Request):
        owner,_=actor(request)
        return public(store.get(owner,rid,'publications'))

    @app.post(prefix+'/runs',status_code=202)
    def start(body:Start,request:Request):
        owner,token=actor(request)
        previous=store.lookup_request(owner,'runs',body.request_id,body.model_dump())
        if previous:
            return public(previous)
        prep=store.get(owner,body.preparation_id,'preparations')
        cases=store.get(owner,body.eval_set_id,'eval-sets')
        if prep['status']!='ready': raise Failure('not_ready','包准备尚未完成',409)
        run=store.create(owner,'runs',dict(status='queued',phase='preparing',preparation_id=prep['id'],
            package_ref=prep['package_ref'],source=prep['source'],mapping=prep['mapping'],relations=prep['preview'].get('relations',{}),judge=judge.profile(),
            case_revision=cases['revision'],cases=[dict(**c,status='pending') for c in cases['cases']]),
            body.request_id,identity=body.model_dump())
        engine.start(owner,run['id'],token)
        return public(run)

    @app.get(prefix+'/runs')
    def runs(request:Request,offset:int=Query(0,ge=0)):
        owner,_=actor(request)
        page=store.list(owner,'runs',offset)
        page['items']=[dict(**public(r),summary=report(r)) for r in page['items']]
        return page

    @app.get(prefix+'/runs/{rid}')
    def run(rid:str,request:Request):
        owner,_=actor(request)
        item=store.get(owner,rid,'runs')
        return dict(**public(item),summary=report(item))

    @app.get(prefix+'/runs/{rid}/results')
    def results(rid:str,request:Request,offset:int=Query(0,ge=0)):
        owner,_=actor(request)
        cases=store.get(owner,rid,'runs')['cases']
        items=[]
        for c in cases[offset:offset+20]:
            item={k:v for k,v in c.items() if k not in ('answer','reference','overrides','judgement','override')}
            item['question']=c['question'][:256]
            for key in ('judgement','override'):
                if c.get(key): item[key]={**c[key],'reason':c[key]['reason'][:256]}
            items.append(item)
        return dict(items=items,total=len(cases),next_offset=offset+20 if offset+20<len(cases) else None)

    @app.get(prefix+'/runs/{rid}/results/{cid}/answer')
    def answer(rid:str,cid:int,request:Request):
        owner,_=actor(request)
        cases=store.get(owner,rid,'runs')['cases']
        if not 0<=cid<len(cases): raise Failure('not_found','用例不存在',404)
        return cases[cid]

    @app.get(prefix+'/runs/{rid}/results/{cid}/evidence')
    def evidence(rid:str,cid:int,request:Request):
        owner,_=actor(request)
        run=store.get(owner,rid,'runs')
        if not 0<=cid<len(run['cases']): raise Failure('not_found','用例不存在',404)
        case=run['cases'][cid]
        if not case.get('remote_id') or case['status'] in ('executing','unknown','pending'):
            return {'trace_status':'unavailable','events':[],'message':'消费执行尚未结算'}
        _,token=actor(request)
        try:
            raw=http.request('GET',config.consumer_url,'/agentic/runs/'+case['remote_id']+'/events?after=0',
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
        except (Failure,ValueError,UnicodeError):
            return {'trace_status':'unavailable','events':[],'message':'消费证据暂不可用，未伪造工具轨迹'}

    @app.get(prefix+'/runs/{rid}/report')
    def get_report(rid:str,request:Request):
        owner,_=actor(request)
        return report(store.get(owner,rid,'runs'))

    @app.post(prefix+'/runs/{rid}/cancel')
    def cancel(rid:str,request:Request):
        owner,token=actor(request)
        value=engine.stop(owner,rid)
        engine.start(owner,rid,token)
        return public(value)

    @app.post(prefix+'/runs/{rid}/resume')
    @app.post(prefix+'/runs/{rid}/credentials')
    def resume(rid:str,request:Request):
        owner,token=actor(request)
        run=store.get(owner,rid,'runs')
        engine.start(owner,rid,token)
        return public(run)

    @app.post(prefix+'/runs/{rid}/results/{cid}/override')
    def override(rid:str,cid:int,body:dict,request:Request):
        owner,_=actor(request)
        with engine.guard:
            run=store.get(owner,rid,'runs')
            if (owner,rid) in engine.active: raise Failure('run_active','运行中不能改判',409)
            if not 0<=cid<len(run['cases']) or body.get('verdict') not in ('pass','partial','fail') or not isinstance(body.get('reason'),str) or not 0<len(body['reason'])<=4000:
                raise Failure('override_invalid','请提供判定及原因',422)
            if body.get('revision')!=run['revision']: raise Failure('revision_conflict','请刷新后重试',409)
            case=run['cases'][cid]
            if not case.get('answer'): raise Failure('answer_missing','未获得答案不能改判',409)
            value=dict(verdict=body['verdict'],reason=body['reason'],source='human',actor=owner,time=time.time())
            case.setdefault('overrides',[]).append(value)
            case['override']=value
            return public(store.save(owner,rid,run,body['revision']))

    @app.delete(prefix+'/runs/{rid}')
    def delete_run(rid:str,request:Request):
        owner,token=actor(request)
        with engine.guard:
            run=store.get(owner,rid,'runs')
            if (owner,rid) in engine.active or run['status'] not in ('completed','cancelled','failed'):
                raise Failure('run_active','请先停止并完成任务对账',409)
            task=store.create(owner,'cleanup_tasks',{'run_id':rid,'workspace_done':False,'data_done':False},rid,identity={'run_id':rid})
            if run.get('managed_workspace') and not task['workspace_done']:
                try:
                    consumer.call('DELETE','/workspaces/'+run['workspace'],token)
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

    assets=Path(__file__).parents[1]/'frontend'/'dist'
    if assets.exists(): app.mount('/evaluation',StaticFiles(directory=assets,html=True),name='evaluation')
    return app
