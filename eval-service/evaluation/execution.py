"""Durable attempts, remote reconciliation and separate Judge retries."""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

from .core import Failure, digest

TERMINAL={'completed','failed','cancelled','awaiting_input'}
SETTLED={'settled','execution_error','cancelled','manual'}
# Questions of one run sent to consumer at the same time; chosen per run when it is created.
DEFAULT_CONCURRENCY=4
MAX_CONCURRENCY=8
JUDGE_SYSTEM='''你是评测判分员，比较问题、参考答案和智能体回答。
pass=关键结论与数值一致；partial=方向正确但遗漏；fail=关键结论错误。
先对齐比较范围：整体、月度或汇总答案只和同层级汇总比较，不能拿单行局部值替代。
组合或方案编号通常是用例标识，不是批次。百分比绝对误差不超过 0.05 个百分点，
或相对误差不超过 0.1%，任一满足即视为舍入差异；一般数值相对误差不超过 0.1% 可视为一致。
不因尾随零、格式或不矛盾的额外说明降分。用 submit_verdict 提交结论和简短理由。'''


class Judge:
    def __init__(self,settings,http): self.settings,self.http=settings,http

    def profile(self,current=None,owner=None):
        c=current or self.settings.effective(owner)
        if c.get('source')=='disabled':
            raise Failure('judge_default_disabled','管理员已停用默认判分模型，请在“评测判分模型配置”中填写你自己的判分模型',409,'judging')
        if not all((c['url'],c['key'],c['model'],c['revision'])):
            raise Failure('judge_unconfigured','Judge 配置缺失（MODEL-CONFIG-01）',503,'judging')
        return dict(model=c['model'],revision=c['revision'],rule_version='1')

    def score(self, question, reference, answer, expected=None, current=None, owner=None):
        # owner selects that user's personal configuration, falling back to the shared default.
        current=current or self.settings.effective(owner)
        profile=self.profile(current)
        # One run is judged by one configuration; a change mid-run stops judging instead of mixing models.
        if expected and profile!=expected:
            raise Failure('configuration_changed','Judge 配置已变化，请创建新轮次',409,'judging')
        schema={'type':'object','properties':{'verdict':{'type':'string','enum':['pass','partial','fail']},
                 'reason':{'type':'string'}},'required':['verdict','reason'],'additionalProperties':False}
        response=self.http.json('POST',current['url'],'/chat/completions',token=current['key'],
            json={'model':profile['model'],'messages':[{'role':'system','content':JUDGE_SYSTEM},
                  {'role':'user','content':json.dumps(dict(question=question,ground_truth=reference,answer=answer),ensure_ascii=False)}],
                  'tools':[{'type':'function','function':{'name':'submit_verdict','parameters':schema}}],
                  'tool_choice':{'type':'function','function':{'name':'submit_verdict'}}})
        try:
            call=response['choices'][0]['message']['tool_calls'][0]['function']
            value=json.loads(call['arguments'])
            if call['name']!='submit_verdict' or value.get('verdict') not in ('pass','partial','fail') or not isinstance(value.get('reason'),str):
                raise ValueError()
            return {'verdict':value['verdict'],'reason':value['reason'][:4000],'source':'judge'}
        except (KeyError,IndexError,ValueError,TypeError) as exc:
            raise Failure('judge_protocol','Judge 未返回有效结构化判分',422,'judging') from exc

    def preflight(self,current=None,owner=None):
        self.score('合成协议检查：1 加 1 等于几？','2','2',current=current,owner=owner)


class Consumer:
    def __init__(self,config,http): self.config,self.http=config,http

    def call(self,method,path,token,workspace='',**kwargs):
        return self.http.json(method,self.config.consumer_url,'/agentic'+path,token=token,workspace=workspace,**kwargs)

    def ensure(self,token,run_id):
        if not self.config.consumer_token:
            raise Failure('consumer_unconfigured','请配置受限 evaluation 消费凭据',503)
        try:
            result=self.call('POST','/internal/evaluation/workspaces',token,
                             headers={'X-Evaluation-Service-Token':self.config.consumer_token},json={'run_id':run_id})
        except Failure as exc:
            status=exc.upstream_status or exc.status
            messages={
                401:'consumer 未接受用户登录，请重新登录后恢复评测',
                403:'consumer 拒绝创建评测空间，请检查用户授权和评测服务凭据是否匹配',
                404:'consumer 评测空间创建接口不可用，请更新并重新部署 consumer',
                409:'consumer 评测空间创建冲突，请检查空间归属和用户状态',
            }
            raise Failure('consumer_workspace_rejected',messages.get(status,'consumer 评测空间创建失败，请检查 consumer 服务后恢复'),
                          exc.status, 'workspace_prepare',upstream_status=status) from exc
        workspace=result.get('workspace') if isinstance(result,dict) else None
        workspace_id=workspace.get('id') if isinstance(workspace,dict) else None
        if not isinstance(workspace_id,str) or not workspace_id.startswith('eval-') or len(workspace_id)!=37 or any(c not in '0123456789abcdef' for c in workspace_id[5:]):
            raise Failure('consumer_workspace_protocol','consumer 未返回有效的评测空间标识，请检查服务版本',502,'workspace_prepare')
        return workspace_id

    def scope_workspace(self,token,workspace_id):
        """Use the consumer workspace whose ID the data source names, with a name equal to that ID.

        Only the user's own workspace permissions are used: create it when missing, otherwise reuse it only
        when this user owns it and has not shared it, because the run imports and activates a package there.
        """
        path='/workspaces/'+quote(workspace_id,safe='')
        taken=Failure('workspace_not_owned',f'consumer 工作空间 {workspace_id} 不是你独占的空间，评测不会在他人或共享的空间中导入知识包；请更换数据源中的工作空间 ID',409,'workspace_prepare')
        try:
            current=self.call('GET',path,token)
        except Failure as exc:
            status=exc.upstream_status or exc.status
            if status==403: raise taken from exc
            if status!=404:
                raise Failure('consumer_workspace_rejected','consumer 工作空间查询失败，请检查 consumer 服务后恢复',exc.status,'workspace_prepare',upstream_status=status) from exc
            try:
                self.call('POST','/workspaces',token,json={'id':workspace_id,'name':workspace_id,
                                                           'description':'知识包评测按数据源配置的工作空间 ID 创建'})
                return workspace_id
            except Failure as rejected:
                try:
                    current=self.call('GET',path,token)  # A concurrent request may have created it.
                except Failure:
                    raise Failure('workspace_create_failed',f'consumer 无法创建工作空间 {workspace_id}：ID 可能已被其他账号占用，或已达到工作空间数量上限',
                                  409,'workspace_prepare') from rejected
        # consumer reports the owner relationship as role "admin".
        if not isinstance(current,dict) or current.get('role')!='admin' or current.get('members'):
            raise taken
        return workspace_id

    def global_models(self,token):
        """Enabled models of the consumer global pool, at most 100. Listing needs any workspace the user can open."""
        listing=self.call('GET','/workspaces',token)
        ids=[item.get('id') for item in listing.get('workspaces') or [] if isinstance(item,dict)]
        workspace=listing.get('preferred_workspace_id') or (ids[0] if ids else '')
        if not workspace:
            raise Failure('workspace_missing','当前账号在 consumer 中没有可用的工作空间',409)
        value=self.call('GET','/model-profiles',token,workspace)
        return [{'id':item['id'],'name':str(item.get('name') or '')[:200],'model':str(item.get('model') or '')[:200],
                 'provider':str(item.get('provider') or '')[:100],'is_default':bool(item.get('is_default'))}
                for item in value.get('profiles') or []
                if isinstance(item,dict) and item.get('scope')=='global' and isinstance(item.get('id'),str)][:100]

    def profile(self,token,ws,requested=''):
        value=self.call('GET','/model-profiles',token,ws)
        if requested and not any(isinstance(item,dict) and item.get('id')==requested and item.get('scope')=='global'
                                 for item in value.get('profiles') or []):
            raise Failure('model_unavailable','所选问答模型已不在 consumer 全局模型池或已停用，请重新选择后创建新轮次',409,'preparing')
        selected=requested or value.get('default_profile_id')
        if not selected:
            raise Failure('model_unconfigured','consumer 默认问答模型未配置',503,'preparing')
        return dict(id=selected,revision=digest(value),profiles=value)

    def install(self,token,ws,raw,receipt):
        try:
            self.call('POST','/ontologies/import-zip',token,ws,files={'file':('package.zip',raw,'application/zip')})
        except Failure as exc:
            if exc.status!=409: raise
            # Retry the exact immutable Hub bytes of this version, also in a reused graph workspace.
            self.call('POST','/ontologies/import-zip',token,ws,data={'action':'overwrite_version'},files={'file':('package.zip',raw,'application/zip')})
        result=self.call('PUT','/ontologies/'+quote(receipt['manifest_id'],safe='')+'/active-version',token,ws,
                         json={'version':receipt['version']})
        if result.get('active_version')!=receipt['version']:
            raise Failure('activation_mismatch','消费激活版本不一致',409,'package_activate')


def final_answer(remote):
    value=remote.get('result')
    if isinstance(value,dict):
        value=value.get('content') or value.get('text') or value.get('final_answer') or value.get('output')
    return value.strip() if isinstance(value,str) else ''


def consumer_failure(remote):
    """Why a consumer run produced no answer, from its public status and error fields (message <= 1000 chars)."""
    status=remote.get('status')
    if status=='cancelled':
        return {'code':'consumer_cancelled','message':'consumer 运行已取消','phase':'cases'}
    if status=='awaiting_input':
        return {'code':'consumer_awaiting_input','message':'consumer 运行在等待用户补充信息，评测无法交互作答','phase':'cases'}
    if status=='completed':
        return {'code':'consumer_empty_answer','message':'consumer 运行已完成，但未返回可判分的回答','phase':'cases'}
    error=remote.get('error') if isinstance(remote.get('error'),dict) else {}
    code=str(error.get('error_code') or '').strip()[:100] or 'UNKNOWN'
    detail=str(error.get('message') or '').strip()[:1000]
    # consumer reports "Execution failed" when it has no more specific detail.
    suffix=f'：{detail}' if detail and detail!='Execution failed' else ''
    return {'code':'consumer_run_failed','consumer_error_code':code,
            'message':f'consumer 执行失败（{code}）{suffix}','phase':'cases'}


def report(run):
    counts={k:0 for k in ('pass','partial','fail','execution_error','judge_error','manual','cancelled','pending')}
    executed=0
    for case in run['cases']:
        if case.get('answer'): executed+=1
        judgement=case.get('override') or case.get('judgement')
        if judgement:
            counts[judgement['verdict']]+=1
        else:
            counts[case['status'] if case['status'] in counts else 'pending']+=1
    valid=counts['pass']+counts['partial']+counts['fail']
    planned=len(run['cases'])
    return dict(planned=planned,executed=executed,judged=valid,counts=counts,
                strict_pass_rate=counts['pass']/planned if planned else None,
                judged_pass_rate=counts['pass']/valid if valid else None,
                package_ref=run.get('package_ref'),model={k:v for k,v in (run.get('model') or {}).items() if k!='profiles'},judge=run.get('judge'),status=run['status'],
                inputs={'preparation_id':run.get('preparation_id'),'case_revision':run.get('case_revision'),
                        'data_source_id':run.get('source',{}).get('id'),'data_source_revision':run.get('source',{}).get('revision')},
                reproducibility_note='外部连接数据可能变化，固定配置不保证数据完全复现' if run.get('source',{}).get('mode')=='connection' else '')


class Engine:
    def __init__(self,store,identity,packages,data_sources,consumer,judge):
        self.store,self.identity,self.packages,self.data,self.consumer,self.judge=store,identity,packages,data_sources,consumer,judge
        self.pool=ThreadPoolExecutor(max_workers=2,thread_name_prefix='evaluation')
        self.guard=threading.RLock()
        self.tokens={}
        self.active=set()
        self.stopping=False

    def start(self,owner,rid,token):
        with self.guard:
            if self.stopping:
                raise Failure('draining','服务正在停止',503)
            if (owner,rid) not in self.active:
                if len(self.active)>=16 or any(key[0]==owner for key in self.active):
                    raise Failure('evaluation_busy','已有活动评测或服务队列已满，请稍后恢复',409)
                self.tokens[(owner,rid)]=token
                self.active.add((owner,rid))
                self.pool.submit(self.execute,owner,rid)
            else:
                self.tokens[(owner,rid)]=token

    def token(self,owner,rid):
        token=self.tokens.get((owner,rid),'')
        if self.identity.verify(token)!=owner:
            raise Failure('identity_changed','用户身份发生变化',403,'authorization')
        return token

    def update(self,owner,rid,**fields):
        with self.guard:
            run=self.store.get(owner,rid,'runs')
            run.update(fields)
            run['updated_at']=time.time()
            if fields.get('status') in ('completed','cancelled','failed'): run['finished_at']=time.time()
            return self.store.save(owner,rid,run)

    def save_case(self,owner,rid,index,case):
        """Concurrent cases each persist only their own entry."""
        with self.guard:
            run=self.store.get(owner,rid,'runs')
            run['cases'][index]=case
            run['updated_at']=time.time()
            self.store.save(owner,rid,run)

    def configured_workspace(self,run):
        """Consumer workspace named by a connection source: the graph Workspace value or the NL2SQL evaluation workspace ID.

        Graph queries fall back to the workspace ID and the NL2SQL scene is bound to that workspace, so both
        must run there. Legacy connections without the field keep the per-run workspace.
        """
        source=run.get('source') or {}
        if source.get('mode')!='connection' or not source.get('connection'): return ''
        config=self.store.decrypt(source['connection'])
        if not isinstance(config,dict): return ''
        return str(config.get('workspace_value' if source.get('provider')=='neo4j' else 'evaluation_workspace_id') or '')

    def stop(self,owner,rid):
        with self.guard:
            run=self.store.get(owner,rid,'runs')
            if run['status'] in ('completed','cancelled','failed'): return run
            return self.update(owner,rid,status='cancelling',cancel_requested=True)

    def close(self):
        self.stopping=True
        self.pool.shutdown(wait=True,cancel_futures=True)
        self.tokens.clear()

    def execute(self,owner,rid):
        try:
            self._execute(owner,rid)
        except Failure as exc:
            pending_cancel=self.store.get(owner,rid,'runs').get('cancel_requested',False)
            self.update(owner,rid,status='waiting_auth' if exc.status in (401,403) else 'cancelling' if pending_cancel else 'interrupted',error=exc.payload())
        except Exception:
            # No raw dependency exception or connection configuration in reports/logs.
            self.update(owner,rid,status='interrupted',error={'code':'execution_interrupted','message':'执行中断，请检查依赖后恢复'})
        finally:
            with self.guard:
                self.active.discard((owner,rid))
                self.tokens.pop((owner,rid),None)

    def _execute(self,owner,rid):
        run=self.store.get(owner,rid,'runs')
        token=self.token(owner,rid)
        if run['status'] in ('completed','cancelled'): return
        if run.get('cancel_requested'):
            self._cancel(owner,rid,run)
            return
        if not run.get('started_at'): run=self.update(owner,rid,started_at=time.time(),error=None)
        if run['judge']!=self.judge.profile(owner=owner):
            raise Failure('configuration_changed','Judge 配置已变化，请创建新轮次',409)
        self.judge.preflight(owner=owner)
        if not run.get('workspace'):
            scope=self.configured_workspace(run)
            if scope:
                # The configured workspace outlives this run and is never deleted with it.
                run=self.update(owner,rid,workspace=self.consumer.scope_workspace(token,scope),managed_workspace=False,status='preparing')
            else:
                run=self.update(owner,rid,workspace=self.consumer.ensure(token,rid),managed_workspace=True,status='preparing')
        ws=run['workspace']
        profile=self.consumer.profile(token,ws,run.get('model_profile_id',''))
        if run.get('model') and run['model']!=profile:
            raise Failure('configuration_changed','问答模型配置已变化，请创建新轮次',409)
        run=self.update(owner,rid,model=profile)
        if not run.get('installed'):
            self.update(owner,rid,phase='package_fetch')
            raw,receipt=self.packages.fetch(run['package_ref'],token)
            self.consumer.install(token,ws,raw,receipt)
            run=self.update(owner,rid,installed=True,package_ref=receipt)
        if not run.get('data_ready'):
            self.update(owner,rid,phase='data_import')
            source_definition=dict(run['source'])
            if source_definition.get('provider')=='neo4j' and source_definition.get('connection'):
                graph_config=self.store.decrypt(source_definition['connection'])
                if graph_config.get('ontology_property'): graph_config['ontology_value']=run['package_ref']['manifest_id']
                source_definition['connection']=self.store.encrypt(graph_config)
            source=self.data.prepare(rid,source_definition,run.get('mapping'),run.get('relations'))
            if source:
                self.consumer.call('PUT','/ontologies/'+quote(run['package_ref']['manifest_id'],safe='')+'/data-source',token,ws,json=source)
                probe=self.consumer.call('POST','/ontologies/'+quote(run['package_ref']['manifest_id'],safe='')+'/data-source/test',token,ws,json={})
                if probe.get('success') is not True:
                    raise Failure('data_source_unavailable','消费数据源连接检查失败',503,'data_import')
            run=self.update(owner,rid,data_ready=True)
        if run['source'].get('provider')=='neo4j' and run['source'].get('import_data'):
            self.data.graph.ensure_current(rid,run['source'],run['package_ref']['manifest_id'])
        self.update(owner,rid,status='running',phase='cases',error=None)
        halt=threading.Event()
        def work(index):
            if halt.is_set() or self.stopping: return
            try:
                self._case(owner,rid,ws,index,halt)
            except BaseException:
                halt.set()  # Queued cases are skipped; cases already in flight still settle.
                raise
        width=max(1,min(MAX_CONCURRENCY,int(run.get('concurrency') or DEFAULT_CONCURRENCY)))
        with ThreadPoolExecutor(max_workers=width,thread_name_prefix='evaluation-case') as pool:
            futures=[pool.submit(work,index) for index in range(len(run['cases']))]
        for future in futures:
            future.result()
        if self.stopping: return
        run=self.store.get(owner,rid,'runs')
        if run.get('cancel_requested'):
            self._cancel(owner,rid,run)
            return
        self.update(owner,rid,status='interrupted' if any(c['status']=='judge_error' for c in run['cases']) else 'completed',
                    phase='finished')

    def _case(self,owner,rid,ws,index,halt):
        run=self.store.get(owner,rid,'runs')
        case=run['cases'][index]
        if case['status'] in SETTLED: return
        if run.get('cancel_requested'):
            halt.set()  # Unsettled cases are cancelled once, after every worker has stopped.
            return
        token=self.token(owner,rid)
        if not case.get('answer'):
            if not case.get('remote_id'):
                # Consumer /runs persists the session fingerprint and idempotency key.
                # Reconciliation resubmits the exact same request, never a new session.
                if self.consumer.profile(token,ws,run.get('model_profile_id',''))!=run['model']:
                    raise Failure('configuration_changed','问答模型发生变化',409)
                case['status']='unknown'
                self.save_case(owner,rid,index,case)
                response=self.consumer.call('POST','/runs',token,ws,
                    headers={'Idempotency-Key':rid+'-'+str(index)},json={
                        'request':case['question'],'session_id':'eval-'+rid+'-'+str(index),
                        'model_profile_id':run['model']['id'],'include_trace':True})
                case['remote_id']=response['run_id']
                case['status']='executing'
                self.save_case(owner,rid,index,case)
            deadline=time.monotonic()+900
            while True:
                if self.stopping: return
                if time.monotonic()>deadline:
                    raise Failure('remote_timeout','消费运行尚未结束，请对账恢复',504,'cases')
                token=self.token(owner,rid)
                cancelling=self.store.get(owner,rid,'runs').get('cancel_requested',False)
                remote_path='/runs/'+quote(case['remote_id'],safe='')
                if cancelling:
                    self.consumer.call('POST',remote_path+'/cancel',token,ws,json={})
                response=self.consumer.call('GET',remote_path,token,ws)
                if response['status'] in TERMINAL: break
                time.sleep(1)
            case['evidence']={'remote_run_id':case['remote_id'],'status':response['status'],
                              'trace_status':'not_collected'}
            answer=final_answer(response)
            if response['status']!='completed' or not answer:
                case['status']='cancelled' if response['status']=='cancelled' else 'execution_error'
                case['error']=consumer_failure(response)
                self.save_case(owner,rid,index,case)
                return
            case['answer']=answer
            case['status']='answer_ready'
            self.save_case(owner,rid,index,case)
        if not case.get('reference'):
            case['status']='manual'
        else:
            try:
                case['judgement']=self.judge.score(case['question'],case['reference'],case['answer'],expected=run['judge'],owner=owner)
                case['status']='settled'
            except Failure as exc:
                case['status']='judge_error'
                case['error']=exc.payload()
        self.save_case(owner,rid,index,case)

    def _cancel(self,owner,rid,run):
        """Cancellation does not require Judge, mapping, database or model readiness."""
        for index,case in enumerate(run['cases']):
            if case['status'] in SETTLED or case.get('answer'):
                continue
            token=self.token(owner,rid)
            if not case.get('remote_id') and case['status']=='unknown':
                response=self.consumer.call('POST','/runs',token,run['workspace'],
                    headers={'Idempotency-Key':rid+'-'+str(index)},json={
                        'request':case['question'],'session_id':'eval-'+rid+'-'+str(index),
                        'model_profile_id':run['model']['id'],'include_trace':True})
                case['remote_id']=response['run_id']
                self.update(owner,rid,cases=run['cases'])
            if case.get('remote_id'):
                path='/runs/'+quote(case['remote_id'],safe='')
                self.consumer.call('POST',path+'/cancel',token,run['workspace'],json={})
                deadline=time.monotonic()+30
                while True:
                    if self.stopping: return
                    token=self.token(owner,rid)
                    remote=self.consumer.call('GET',path,token,run['workspace'])
                    if remote['status'] in TERMINAL: break
                    if time.monotonic()>deadline:
                        raise Failure('cancel_pending','远端尚未确认取消，请恢复对账',504,'cancelling')
                    time.sleep(1)
                if remote['status']=='completed':
                    case['answer']=final_answer(remote)
                    case['status']='manual'
                else:
                    case['status']='cancelled' if remote['status']=='cancelled' else 'execution_error'
                    case['error']=consumer_failure(remote)
            else:
                case['status']='cancelled'
            self.update(owner,rid,cases=run['cases'])
        self.update(owner,rid,status='cancelled',phase='finished')
